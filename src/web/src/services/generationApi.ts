/**
 * @fileoverview Generation job API service for the Synthetic ERP Data Generation Platform.
 *
 * Provides fully-typed functions for the complete generation job lifecycle and
 * template management. This module is the single integration point between the
 * React Web Console and the backend Generation Engine, Quality Service, and
 * Provisioning Service — all routed through the Flask API Gateway.
 *
 * **Consumers:**
 * - `jobStore` (Zustand) — Centralized generation job state management
 * - `GenerationWizard` (S-002) — Multi-step job creation wizard
 * - `JobMonitoring` (S-004) — Real-time job progress tracking
 * - `Dashboard` (S-001) — Generation statistics and system health
 * - `TemplateLibrary` (S-003) — Browsable template catalog with CRUD
 *
 * **Key Design Decisions:**
 * - All functions delegate HTTP communication to the shared `apiClient` Axios
 *   instance (from `./api`), which automatically handles JWT injection,
 *   multi-tenant header propagation, error transformation, and token refresh.
 * - The apiClient response interceptor unwraps `AxiosResponse.data`, so these
 *   functions receive the API response envelope (`ApiResponse<T>`) directly.
 * - URL-path API versioning (`/api/v1/`) is enforced per requirement R-012.
 * - Binary file downloads use `responseType: 'blob'` with an extended timeout.
 *
 * @module services/generationApi
 * @version 1.0.0
 */

import { apiClient } from './api';
import type {
  ApiResponse,
  PaginatedResult,
  PaginationParams,
  FilterParams,
} from '@/types/api';
import type {
  GenerationJob,
  JobConfig,
  JobStatus,
  BatchProgress,
} from '@/types/generation';

// ============================================================================
// API Base Path Constants
// ============================================================================

/**
 * Base URL path for all generation job endpoints.
 * Follows URL-path versioning (/api/v1/) as required by R-012.
 */
const GENERATION_BASE = '/api/v1/generation';

/**
 * Base URL path for all generation template management endpoints.
 * Templates are a top-level resource under the /api/v1/ namespace.
 */
const TEMPLATES_BASE = '/api/v1/templates';

/**
 * Base URL path for dashboard aggregation endpoints.
 * Provides cross-service statistics for the Dashboard screen (S-001).
 */
const DASHBOARD_BASE = '/api/v1/dashboard';

/**
 * Extended timeout in milliseconds for file download requests (2 minutes).
 * Large dataset exports (Parquet, SQL) can exceed the default 30-second timeout.
 */
const DOWNLOAD_TIMEOUT = 120000;

// ============================================================================
// Exported Interfaces — Quality Report
// ============================================================================

/**
 * Quality validation report for a completed generation job.
 *
 * Returned by the Quality Service after evaluating generated data against
 * the weighted scoring model: 40% statistical fidelity + 30% business rules
 * compliance + 30% referential integrity. The overall target is ≥ 0.95 (95%).
 *
 * Used by the Quality Reports page (S-007) and Job Monitoring page (S-004)
 * to display per-job quality metrics and drill-down details.
 */
export interface QualityReport {
  /** Weighted composite quality score (0.0 – 1.0), targeting ≥ 0.95 */
  overall_score: number;

  /** Statistical fidelity sub-score (40% weight in the composite formula) */
  statistical_score: number;

  /** Business rules compliance sub-score (30% weight in the composite formula) */
  business_rules_score: number;

  /** Referential integrity sub-score (30% weight in the composite formula) */
  referential_integrity_score: number;

  /**
   * Detailed validation metrics including per-table breakdowns,
   * column-level distribution comparisons, and rule violation counts.
   */
  details: Record<string, unknown>;

  /** ISO 8601 timestamp indicating when the quality validation was performed */
  validated_at: string;
}

// ============================================================================
// Exported Interfaces — Generation Template
// ============================================================================

/**
 * Pre-configured generation template available in the Template Library (S-003).
 *
 * Templates store reusable job configurations that can be selected during
 * the Generation Wizard (S-002) to pre-fill method, schema, table, and
 * output settings. Templates support both public (shared across tenants)
 * and private (tenant-scoped) visibility.
 */
export interface GenerationTemplate {
  /** Unique template identifier (UUID) */
  template_id: string;

  /** Human-readable template name displayed in the catalog */
  name: string;

  /** Detailed description of the template's purpose and configuration */
  description: string;

  /**
   * Template category for catalog organization.
   * Examples: 'financial_accounting', 'human_resources', 'sales_distribution', 'material_management'
   */
  category: string;

  /** Pre-configured job settings applied when this template is selected */
  config: JobConfig;

  /** Whether the template is visible to all tenants or restricted to the creator's tenant */
  is_public: boolean;

  /** User ID of the template creator */
  created_by: string;

  /** ISO 8601 creation timestamp */
  created_at: string;

  /** ISO 8601 last modification timestamp */
  updated_at: string;

  /** Running count of how many jobs have been created using this template */
  usage_count: number;
}

/**
 * Request body for creating a new generation template.
 *
 * Sent via `POST /api/v1/templates`. The `config` field contains the
 * full {@link JobConfig} that will be applied as defaults when a user
 * selects this template in the Generation Wizard (S-002).
 */
export interface CreateTemplateRequest {
  /** Human-readable template name */
  name: string;

  /** Detailed description of the template's purpose */
  description: string;

  /** Template category for catalog organization */
  category: string;

  /** Pre-configured job settings */
  config: JobConfig;

  /** Whether the template is visible to all tenants */
  is_public: boolean;
}

// ============================================================================
// Exported Interfaces — Filter Parameters
// ============================================================================

/**
 * Filter parameters specific to generation template queries.
 *
 * Passed as query parameters to `GET /api/v1/templates` to narrow
 * the template catalog by category, ERP system type, and generation method.
 */
export interface TemplateFilterParams {
  /** Filter by template category (e.g., 'financial_accounting', 'human_resources') */
  category?: string;

  /** Filter by target ERP system type (e.g., 'sap', 'oracle_ebs', 'dynamics') */
  erp_type?: string;

  /** Filter by generation method (e.g., 'ai_ml', 'rules_based', 'statistical', 'masking') */
  method?: string;
}

/**
 * Filter parameters specific to generation job queries.
 *
 * Extends the common {@link FilterParams} base interface with job-specific
 * filter fields. Used by `getJobs()` to enable type-safe filtering on the
 * Job Monitoring page (S-004) and Dashboard (S-001).
 */
export interface JobFilterParams extends FilterParams {
  /** Filter by job lifecycle status (e.g., 'submitted', 'generating', 'completed', 'failed') */
  status?: JobStatus;

  /** Filter by generation method (e.g., 'ai_ml', 'rules_based', 'statistical', 'masking') */
  method?: string;
}

// ============================================================================
// Exported Interfaces — Dashboard Types
// ============================================================================

/**
 * Aggregated generation metrics for the Dashboard screen (S-001).
 *
 * Provides a snapshot of platform-wide generation activity including
 * job counts by status, total records generated, and average quality scores.
 */
export interface DashboardMetrics {
  /** Total number of generation jobs across all statuses */
  total_jobs: number;

  /** Number of jobs that have reached 'completed' status */
  completed_jobs: number;

  /** Number of jobs that have reached 'failed' status */
  failed_jobs: number;

  /** Number of jobs currently in an active state (submitted, generating, validating, etc.) */
  active_jobs: number;

  /** Cumulative count of synthetic records generated across all completed jobs */
  total_records_generated: number;

  /** Mean quality score across all completed jobs (0.0 – 1.0) */
  average_quality_score: number;

  /** Most recently created or updated generation jobs for the activity feed */
  recent_jobs: GenerationJob[];
}

/**
 * Individual service health status within the platform.
 */
export interface ServiceHealthStatus {
  /** Service display name (e.g., 'API Gateway', 'Generation Engine') */
  name: string;

  /** Current health status of the service */
  status: 'healthy' | 'degraded' | 'unhealthy';

  /** Response latency in milliseconds from the last health check */
  latency_ms: number;

  /** ISO 8601 timestamp of the last health check */
  last_check: string;
}

/**
 * Aggregate system health report for the Dashboard screen (S-001).
 *
 * Consolidates health check results from all backend microservices
 * (API Gateway, Generation Engine, Profiling, Quality, Compliance, Provisioning)
 * plus data stores (MongoDB, Redis).
 */
export interface SystemHealth {
  /** Overall platform health derived from individual service statuses */
  overall_status: 'healthy' | 'degraded' | 'unhealthy';

  /** Per-service health check results */
  services: ServiceHealthStatus[];

  /** Platform uptime in seconds since the last restart */
  uptime_seconds: number;

  /** ISO 8601 timestamp when this health snapshot was captured */
  timestamp: string;
}

/**
 * Time-series data point for the throughput chart on the Dashboard (S-001).
 *
 * Represents generation throughput at a specific point in time, used by
 * the ThroughputChart component to render records-per-minute over time.
 */
export interface ThroughputDataPoint {
  /** ISO 8601 timestamp for this data point */
  timestamp: string;

  /** Number of records generated per minute at this time */
  records_per_minute: number;

  /** Number of concurrently active generation jobs at this time */
  active_jobs: number;
}

// ============================================================================
// Generation Job Endpoints
// ============================================================================

/**
 * Creates a new generation job with the specified configuration.
 *
 * Submits a job request to the API Gateway which persists it in MongoDB
 * with status 'submitted' and dispatches it to the Generation Engine
 * for asynchronous batch processing.
 *
 * **Endpoint:** `POST /api/v1/generation/jobs`
 *
 * @param config - Complete job configuration including generation method,
 *   target schema, table specifications, output format, batch size, and
 *   quality threshold. See {@link JobConfig} for field details.
 * @returns Promise resolving to the created job with assigned `job_id`
 *   and initial status `'submitted'`
 *
 * @example
 * ```typescript
 * const job = await createJob({
 *   method: GenerationMethod.STATISTICAL,
 *   schema_id: 'schema-abc-123',
 *   tables: [{ table_name: 'GL_JOURNAL_ENTRIES', record_count: 50000, preserve_relationships: true }],
 *   output_format: OutputFormat.CSV,
 *   batch_size: 10000,
 *   quality_threshold: 0.95,
 * });
 * console.log(`Job created: ${job.data.job_id}`);
 * ```
 */
export async function createJob(
  config: JobConfig
): Promise<ApiResponse<GenerationJob>> {
  return apiClient.post(`${GENERATION_BASE}/jobs`, config);
}

/**
 * Retrieves full details for a single generation job by its unique identifier.
 *
 * Returns the complete job state including current lifecycle status, progress
 * percentage, quality score (if validated), output location (if provisioned),
 * and error details (if failed).
 *
 * **Endpoint:** `GET /api/v1/generation/jobs/{jobId}`
 *
 * @param jobId - Unique identifier of the generation job (UUID)
 * @returns Promise resolving to the full generation job details
 *
 * @example
 * ```typescript
 * const response = await getJobById('job-uuid-123');
 * console.log(`Status: ${response.data.status}, Progress: ${response.data.progress}%`);
 * ```
 */
export async function getJobById(
  jobId: string
): Promise<ApiResponse<GenerationJob>> {
  return apiClient.get(`${GENERATION_BASE}/jobs/${encodeURIComponent(jobId)}`);
}

/**
 * Retrieves a paginated list of generation jobs with optional filtering.
 *
 * Supports server-side pagination, sorting, and filtering by job status,
 * generation method, search text, date range, and tenant scope. Used by
 * the Job Monitoring page (S-004) and Dashboard (S-001) recent jobs list.
 *
 * **Endpoint:** `GET /api/v1/generation/jobs`
 *
 * @param params - Optional combined pagination and filter parameters.
 *   Pagination fields: `page`, `page_size`, `sort_by`, `sort_direction`.
 *   Filter fields: `status`, `method`, `search`, `date_from`, `date_to`.
 * @returns Promise resolving to a paginated result set of generation jobs
 *
 * @example
 * ```typescript
 * // Fetch the first page of completed jobs, sorted by newest first
 * const response = await getJobs({
 *   page: 1,
 *   page_size: 20,
 *   sort_by: 'created_at',
 *   sort_direction: 'desc',
 *   status: JobStatus.COMPLETED,
 * });
 * console.log(`${response.data.total} total jobs, showing page ${response.data.page}`);
 * ```
 */
export async function getJobs(
  params?: PaginationParams & JobFilterParams
): Promise<ApiResponse<PaginatedResult<GenerationJob>>> {
  return apiClient.get(`${GENERATION_BASE}/jobs`, { params });
}

/**
 * Cancels an in-progress generation job.
 *
 * Sends a cancellation request to the API Gateway which signals the
 * Generation Engine to halt batch processing. The job transitions to
 * 'failed' status with a cancellation reason in the error details.
 * Only jobs in an active state (submitted, generating, validating) can
 * be cancelled.
 *
 * **Endpoint:** `POST /api/v1/generation/jobs/{jobId}/cancel`
 *
 * @param jobId - Unique identifier of the job to cancel (UUID)
 * @returns Promise resolving to the updated job with 'failed' status
 *   and cancellation details
 *
 * @example
 * ```typescript
 * const response = await cancelJob('job-uuid-123');
 * console.log(`Job cancelled. Status: ${response.data.status}`);
 * ```
 */
export async function cancelJob(
  jobId: string
): Promise<ApiResponse<GenerationJob>> {
  return apiClient.post(
    `${GENERATION_BASE}/jobs/${encodeURIComponent(jobId)}/cancel`
  );
}

/**
 * Retrieves real-time batch progress for an active generation job.
 *
 * Returns a snapshot of the current generation progress including the
 * active batch number, total batches, cumulative record count, completion
 * percentage, and estimated time remaining. Used by the Job Monitoring
 * page (S-004) to render progress bars and ETA indicators.
 *
 * Progress data is backed by Redis for low-latency polling. The
 * `estimated_remaining_seconds` field may be `null` during the first
 * batch when insufficient data is available for a reliable estimate.
 *
 * **Endpoint:** `GET /api/v1/generation/jobs/{jobId}/progress`
 *
 * @param jobId - Unique identifier of the generation job (UUID)
 * @returns Promise resolving to the current batch progress snapshot
 *
 * @example
 * ```typescript
 * const response = await getJobProgress('job-uuid-123');
 * const { percentage, records_generated, estimated_remaining_seconds } = response.data;
 * console.log(`${percentage}% complete (${records_generated} records), ETA: ${estimated_remaining_seconds}s`);
 * ```
 */
export async function getJobProgress(
  jobId: string
): Promise<ApiResponse<BatchProgress>> {
  return apiClient.get(
    `${GENERATION_BASE}/jobs/${encodeURIComponent(jobId)}/progress`
  );
}

/**
 * Retrieves the quality validation report for a generation job.
 *
 * Returns the weighted quality scores computed by the Quality Service after
 * the generation phase completes. The composite score is calculated as:
 * `Q = 0.4 × statistical + 0.3 × business_rules + 0.3 × referential_integrity`
 *
 * Only available for jobs that have reached the 'validating' stage or later.
 * Returns a 404 error for jobs still in 'submitted' or 'generating' status.
 *
 * **Endpoint:** `GET /api/v1/generation/jobs/{jobId}/quality`
 *
 * @param jobId - Unique identifier of the generation job (UUID)
 * @returns Promise resolving to the quality report with composite and sub-scores
 *
 * @example
 * ```typescript
 * const response = await getJobQualityReport('job-uuid-123');
 * const { overall_score, statistical_score } = response.data;
 * console.log(`Quality: ${(overall_score * 100).toFixed(1)}%`);
 * ```
 */
export async function getJobQualityReport(
  jobId: string
): Promise<ApiResponse<QualityReport>> {
  return apiClient.get(
    `${GENERATION_BASE}/jobs/${encodeURIComponent(jobId)}/quality`
  );
}

/**
 * Downloads the generated output for a completed job as a binary blob.
 *
 * Returns the generated synthetic data file in the requested format. If no
 * format override is specified, the job's configured `output_format` is used.
 * Supported formats: SQL, CSV, JSON, Parquet.
 *
 * Uses `responseType: 'blob'` to receive the binary payload directly, and
 * an extended timeout (2 minutes) to accommodate large dataset downloads.
 *
 * **Endpoint:** `GET /api/v1/generation/jobs/{jobId}/download`
 *
 * @param jobId - Unique identifier of the completed generation job (UUID)
 * @param format - Optional output format override ('sql', 'csv', 'json', 'parquet').
 *   Defaults to the format specified in the job's original configuration.
 * @returns Promise resolving to a Blob containing the generated data file
 *
 * @example
 * ```typescript
 * const blob = await downloadJobOutput('job-uuid-123', 'csv');
 * const url = URL.createObjectURL(blob);
 * const link = document.createElement('a');
 * link.href = url;
 * link.download = 'synthetic_data.csv';
 * link.click();
 * URL.revokeObjectURL(url);
 * ```
 */
export async function downloadJobOutput(
  jobId: string,
  format?: string
): Promise<Blob> {
  const params: Record<string, string> = {};
  if (format) {
    params.format = format;
  }

  return apiClient.get(
    `${GENERATION_BASE}/jobs/${encodeURIComponent(jobId)}/download`,
    {
      params,
      responseType: 'blob',
      timeout: DOWNLOAD_TIMEOUT,
    }
  );
}

/**
 * Retries a previously failed generation job.
 *
 * Creates a new generation job using the same configuration as the original
 * failed job. The original job's status and error history are preserved for
 * audit trail purposes. The new job receives a fresh `job_id` and starts
 * from 'submitted' status.
 *
 * **Endpoint:** `POST /api/v1/generation/jobs/{jobId}/retry`
 *
 * @param jobId - Unique identifier of the failed generation job to retry (UUID)
 * @returns Promise resolving to the newly created retry job
 *
 * @example
 * ```typescript
 * const response = await retryJob('failed-job-uuid-123');
 * console.log(`Retry job created: ${response.data.job_id}`);
 * ```
 */
export async function retryJob(
  jobId: string
): Promise<ApiResponse<GenerationJob>> {
  return apiClient.post(
    `${GENERATION_BASE}/jobs/${encodeURIComponent(jobId)}/retry`
  );
}

// ============================================================================
// Template Endpoints
// ============================================================================

/**
 * Retrieves a paginated list of generation templates with optional filtering.
 *
 * Returns templates visible to the current tenant, filtered by category,
 * ERP type, and/or generation method. Used by the Template Library page
 * (S-003) and the template selection step in the Generation Wizard (S-002).
 *
 * **Endpoint:** `GET /api/v1/templates`
 *
 * @param params - Optional combined pagination and template filter parameters.
 *   Pagination: `page`, `page_size`, `sort_by`, `sort_direction`.
 *   Filters: `category`, `erp_type`, `method`.
 * @returns Promise resolving to a paginated result set of generation templates
 *
 * @example
 * ```typescript
 * const response = await getTemplates({
 *   page: 1,
 *   page_size: 12,
 *   category: 'financial_accounting',
 *   sort_by: 'usage_count',
 *   sort_direction: 'desc',
 * });
 * console.log(`${response.data.total} templates found`);
 * ```
 */
export async function getTemplates(
  params?: PaginationParams & TemplateFilterParams
): Promise<ApiResponse<PaginatedResult<GenerationTemplate>>> {
  return apiClient.get(TEMPLATES_BASE, { params });
}

/**
 * Retrieves full details for a single generation template by its identifier.
 *
 * Returns the complete template including its pre-configured {@link JobConfig},
 * metadata, and usage statistics. Used when a user selects a template in the
 * Generation Wizard (S-002) to populate the wizard form fields.
 *
 * **Endpoint:** `GET /api/v1/templates/{templateId}`
 *
 * @param templateId - Unique identifier of the template (UUID)
 * @returns Promise resolving to the full template details
 *
 * @example
 * ```typescript
 * const response = await getTemplateById('tmpl-uuid-456');
 * console.log(`Template: ${response.data.name}, Used ${response.data.usage_count} times`);
 * ```
 */
export async function getTemplateById(
  templateId: string
): Promise<ApiResponse<GenerationTemplate>> {
  return apiClient.get(
    `${TEMPLATES_BASE}/${encodeURIComponent(templateId)}`
  );
}

/**
 * Creates a new generation template from the provided configuration.
 *
 * Persists a reusable job configuration as a named template that appears
 * in the Template Library (S-003). The `is_public` flag controls whether
 * the template is visible to all tenants or restricted to the creator's
 * tenant namespace.
 *
 * **Endpoint:** `POST /api/v1/templates`
 *
 * @param template - Template creation payload with name, description,
 *   category, job config, and visibility settings
 * @returns Promise resolving to the created template with assigned `template_id`
 *
 * @example
 * ```typescript
 * const response = await createTemplate({
 *   name: 'SAP Financial Accounting - Standard',
 *   description: 'Standard GL entries with balanced debits/credits',
 *   category: 'financial_accounting',
 *   config: { method: GenerationMethod.RULES_BASED, ... },
 *   is_public: true,
 * });
 * console.log(`Template created: ${response.data.template_id}`);
 * ```
 */
export async function createTemplate(
  template: CreateTemplateRequest
): Promise<ApiResponse<GenerationTemplate>> {
  return apiClient.post(TEMPLATES_BASE, template);
}

/**
 * Updates an existing generation template with partial modifications.
 *
 * Accepts a partial update payload — only the provided fields will be
 * modified; omitted fields retain their current values. The `updated_at`
 * timestamp is automatically refreshed server-side.
 *
 * **Endpoint:** `PUT /api/v1/templates/{templateId}`
 *
 * @param templateId - Unique identifier of the template to update (UUID)
 * @param updates - Partial template fields to modify. Any combination of
 *   name, description, category, config, and is_public can be updated.
 * @returns Promise resolving to the fully updated template
 *
 * @example
 * ```typescript
 * const response = await updateTemplate('tmpl-uuid-456', {
 *   name: 'SAP GL Entries - Updated',
 *   config: { ...existingConfig, batch_size: 20000 },
 * });
 * console.log(`Template updated: ${response.data.updated_at}`);
 * ```
 */
export async function updateTemplate(
  templateId: string,
  updates: Partial<CreateTemplateRequest>
): Promise<ApiResponse<GenerationTemplate>> {
  return apiClient.put(
    `${TEMPLATES_BASE}/${encodeURIComponent(templateId)}`,
    updates
  );
}

/**
 * Soft-deletes a generation template by its identifier.
 *
 * Marks the template as deleted in MongoDB without physically removing the
 * document, preserving audit trail integrity. The template will no longer
 * appear in the Template Library (S-003) or be selectable in the wizard.
 *
 * **Endpoint:** `DELETE /api/v1/templates/{templateId}`
 *
 * @param templateId - Unique identifier of the template to delete (UUID)
 * @returns Promise resolving to a void success response
 *
 * @example
 * ```typescript
 * await deleteTemplate('tmpl-uuid-456');
 * console.log('Template deleted successfully');
 * ```
 */
export async function deleteTemplate(
  templateId: string
): Promise<ApiResponse<void>> {
  return apiClient.delete(
    `${TEMPLATES_BASE}/${encodeURIComponent(templateId)}`
  );
}

/**
 * Creates a copy of an existing generation template.
 *
 * Clones the specified template's configuration into a new template
 * with a fresh `template_id`. The cloned template inherits the original's
 * config, category, and description, but receives the current user as
 * `created_by` and resets `usage_count` to zero. The name is suffixed
 * with " (Copy)" to distinguish it from the original.
 *
 * **Endpoint:** `POST /api/v1/templates/{templateId}/clone`
 *
 * @param templateId - Unique identifier of the template to clone (UUID)
 * @returns Promise resolving to the newly created cloned template
 *
 * @example
 * ```typescript
 * const response = await cloneTemplate('tmpl-uuid-456');
 * console.log(`Cloned template: ${response.data.template_id} (${response.data.name})`);
 * ```
 */
export async function cloneTemplate(
  templateId: string
): Promise<ApiResponse<GenerationTemplate>> {
  return apiClient.post(
    `${TEMPLATES_BASE}/${encodeURIComponent(templateId)}/clone`
  );
}

// ============================================================================
// Dashboard Endpoints
// ============================================================================

/**
 * Retrieves aggregated generation metrics for the Dashboard screen (S-001).
 *
 * Returns a snapshot of platform-wide generation activity including total
 * job counts by status, cumulative records generated, average quality scores,
 * and a list of recent jobs for the activity feed.
 *
 * **Endpoint:** `GET /api/v1/dashboard/metrics`
 *
 * @returns Promise resolving to aggregated dashboard metrics
 *
 * @example
 * ```typescript
 * const response = await getDashboardMetrics();
 * const { total_jobs, active_jobs, average_quality_score } = response.data;
 * console.log(`${total_jobs} total jobs, ${active_jobs} active, avg quality: ${average_quality_score}`);
 * ```
 */
export async function getDashboardMetrics(): Promise<ApiResponse<DashboardMetrics>> {
  return apiClient.get(`${DASHBOARD_BASE}/metrics`);
}

/**
 * Retrieves the current system health status for the Dashboard screen (S-001).
 *
 * Aggregates health check results from all backend microservices (API Gateway,
 * Generation Engine, Profiling Service, Quality Service, Compliance Service,
 * Provisioning Service) and data stores (MongoDB, Redis). The overall status
 * is 'healthy' only when all services report healthy status.
 *
 * **Endpoint:** `GET /api/v1/dashboard/health`
 *
 * @returns Promise resolving to the aggregate system health report
 *
 * @example
 * ```typescript
 * const response = await getSystemHealth();
 * const { overall_status, services } = response.data;
 * const degraded = services.filter(s => s.status !== 'healthy');
 * console.log(`System: ${overall_status}, ${degraded.length} degraded services`);
 * ```
 */
export async function getSystemHealth(): Promise<ApiResponse<SystemHealth>> {
  return apiClient.get(`${DASHBOARD_BASE}/health`);
}

/**
 * Retrieves generation throughput time-series data for the Dashboard (S-001).
 *
 * Returns an array of data points representing records-per-minute throughput
 * and active job counts over time. Used by the ThroughputChart component to
 * render a time-series visualization. Supports configurable time periods
 * and data aggregation intervals via query parameters.
 *
 * **Endpoint:** `GET /api/v1/dashboard/throughput`
 *
 * @param params - Optional query parameters for time range and granularity.
 *   `period` — Time window to fetch (e.g., '1h', '24h', '7d', '30d').
 *   `interval` — Data point aggregation interval (e.g., '1m', '5m', '1h').
 * @returns Promise resolving to an array of throughput data points
 *
 * @example
 * ```typescript
 * // Fetch last 24 hours of throughput data at 5-minute intervals
 * const response = await getThroughputData({ period: '24h', interval: '5m' });
 * const dataPoints = response.data;
 * console.log(`${dataPoints.length} throughput data points retrieved`);
 * ```
 */
export async function getThroughputData(
  params?: { period?: string; interval?: string }
): Promise<ApiResponse<ThroughputDataPoint[]>> {
  return apiClient.get(`${DASHBOARD_BASE}/throughput`, { params });
}
