/**
 * @fileoverview Comprehensive Vitest unit tests for the generationApi service module.
 *
 * Validates all 13 generation job and template API functions exported by
 * `src/web/src/services/generationApi.ts`, verifying correct URL construction,
 * HTTP method selection, request payload serialisation, query parameter
 * forwarding, response handling, and error propagation.
 *
 * **Test organisation:**
 * - 8 describe blocks for generation job endpoints (createJob, getJobById,
 *   getJobs, cancelJob, getJobProgress, getJobQualityReport, downloadJobOutput,
 *   retryJob)
 * - 5 describe blocks for template CRUD endpoints (getTemplates,
 *   getTemplateById, createTemplate, updateTemplate, deleteTemplate)
 *
 * **Mocking strategy:**
 * - The `@/services/api` module is mocked via `vi.mock()` to provide a
 *   controlled `apiClient` with `vi.fn()` stubs for `get`, `post`, `put`,
 *   and `delete` methods.
 * - The global `setup.ts` (loaded by Vitest setupFiles) already mocks
 *   `axios`, environment variables, and browser APIs.
 * - Each test configures the mock return value, invokes the API function,
 *   and asserts the correct call arguments and resolved value.
 *
 * @module tests/unit/web/services/generationApi.test
 * @see src/web/src/services/generationApi.ts
 */

import { describe, it, expect, vi, beforeEach } from 'vitest';

import {
  createJob,
  getJobById,
  getJobs,
  cancelJob,
  getJobProgress,
  getJobQualityReport,
  downloadJobOutput,
  retryJob,
  getTemplates,
  getTemplateById,
  createTemplate,
  updateTemplate,
  deleteTemplate,
} from '@/services/generationApi';

import type { QualityReport, GenerationTemplate, CreateTemplateRequest } from '@/services/generationApi';

import { apiClient } from '@/services/api';

import type { ApiResponse, PaginatedResult } from '@/types/api';
import type {
  GenerationJob,
  JobConfig,
  BatchProgress,
  TableGenerationConfig,
} from '@/types/generation';
import {
  GenerationMethod,
  JobStatus,
  OutputFormat,
} from '@/types/generation';

// ============================================================================
// Module Mocks
// ============================================================================

/**
 * Mock the api module to intercept all HTTP calls made by generationApi
 * functions. The mocked apiClient exposes vi.fn() stubs for get, post,
 * put, and delete, allowing per-test configuration of resolved/rejected values.
 */
vi.mock('@/services/api', () => ({
  apiClient: {
    get: vi.fn(),
    post: vi.fn(),
    put: vi.fn(),
    delete: vi.fn(),
  },
}));

// ============================================================================
// Test Data Factories
// ============================================================================

/**
 * Creates a mock {@link TableGenerationConfig} entry for test fixtures.
 * Represents a single table configuration within a job config payload.
 */
function createMockTableConfig(overrides: Partial<TableGenerationConfig> = {}): TableGenerationConfig {
  return {
    table_name: 'GL_JOURNAL_ENTRIES',
    record_count: 50000,
    preserve_relationships: true,
    ...overrides,
  };
}

/**
 * Creates a mock {@link JobConfig} matching the POST /api/v1/generation/jobs
 * request body. Includes all required fields: method, schema_id, tables,
 * output_format, batch_size, and quality_threshold.
 */
function createMockJobConfig(overrides: Partial<JobConfig> = {}): JobConfig {
  return {
    method: GenerationMethod.STATISTICAL,
    schema_id: 'schema-abc-123',
    tables: [
      createMockTableConfig(),
      createMockTableConfig({
        table_name: 'AP_INVOICES',
        record_count: 25000,
      }),
    ],
    output_format: OutputFormat.CSV,
    batch_size: 10000,
    quality_threshold: 0.95,
    ...overrides,
  };
}

/**
 * Creates a mock {@link GenerationJob} as returned by API responses.
 * Includes all fields defined in the GenerationJob interface with
 * reasonable test defaults reflecting a submitted job.
 */
function createMockGenerationJob(overrides: Partial<GenerationJob> = {}): GenerationJob {
  return {
    job_id: 'job-uuid-001',
    status: JobStatus.SUBMITTED,
    method: GenerationMethod.STATISTICAL,
    progress: 0,
    total_records: 75000,
    generated_records: 0,
    quality_score: null,
    output_location: null,
    error_message: null,
    created_at: '2025-01-15T10:00:00Z',
    updated_at: '2025-01-15T10:00:00Z',
    completed_at: null,
    tenant_id: 'tenant-001',
    ...overrides,
  };
}

/**
 * Creates a mock {@link BatchProgress} snapshot for progress endpoint tests.
 * Represents an active generation job partway through batch processing.
 */
function createMockBatchProgress(overrides: Partial<BatchProgress> = {}): BatchProgress {
  return {
    current_batch: 3,
    total_batches: 8,
    records_generated: 30000,
    records_total: 75000,
    percentage: 40,
    estimated_remaining_seconds: 180,
    ...overrides,
  };
}

/**
 * Creates a mock {@link QualityReport} for quality endpoint tests.
 * Contains weighted sub-scores targeting the ≥95% fidelity requirement.
 */
function createMockQualityReport(overrides: Partial<QualityReport> = {}): QualityReport {
  return {
    overall_score: 0.97,
    statistical_score: 0.98,
    business_rules_score: 0.96,
    referential_integrity_score: 0.95,
    details: {
      per_table_scores: { GL_JOURNAL_ENTRIES: 0.97, AP_INVOICES: 0.96 },
      rule_violations: 0,
    },
    validated_at: '2025-01-15T10:30:00Z',
    ...overrides,
  };
}

/**
 * Creates a mock {@link GenerationTemplate} for template CRUD endpoint tests.
 * Represents a pre-configured template in the Template Library (S-003).
 */
function createMockTemplate(overrides: Partial<GenerationTemplate> = {}): GenerationTemplate {
  return {
    template_id: 'tmpl-uuid-001',
    name: 'SAP Financial Accounting - Standard',
    description: 'Standard GL entries with balanced debits/credits',
    category: 'financial_accounting',
    config: createMockJobConfig({ method: GenerationMethod.RULES_BASED }),
    is_public: true,
    created_by: 'user-001',
    created_at: '2025-01-10T08:00:00Z',
    updated_at: '2025-01-10T08:00:00Z',
    usage_count: 42,
    ...overrides,
  };
}

/**
 * Creates a mock {@link CreateTemplateRequest} for create/update template tests.
 * Contains only the fields submitted by the client (no server-assigned fields).
 */
function createMockCreateTemplateRequest(
  overrides: Partial<CreateTemplateRequest> = {},
): CreateTemplateRequest {
  return {
    name: 'HR Payroll Template',
    description: 'Employee payroll data generation template',
    category: 'human_resources',
    config: createMockJobConfig({
      method: GenerationMethod.AI_ML,
      schema_id: 'schema-hr-456',
    }),
    is_public: false,
    ...overrides,
  };
}

/**
 * Wraps a data payload in the standard {@link ApiResponse} envelope
 * structure for mock resolved values.
 */
function wrapApiResponse<T>(data: T, message?: string): ApiResponse<T> {
  return {
    success: true,
    data,
    message: message ?? null,
    timestamp: '2025-01-15T10:00:00Z',
  };
}

/**
 * Wraps a list of items in a {@link PaginatedResult} envelope for
 * list endpoint mock responses.
 */
function wrapPaginatedResult<T>(
  items: T[],
  total: number = items.length,
  page: number = 1,
  page_size: number = 20,
): PaginatedResult<T> {
  return {
    items,
    total,
    page,
    page_size,
    total_pages: Math.ceil(total / page_size),
  };
}

// ============================================================================
// Test Suite
// ============================================================================

describe('generationApi', () => {
  /**
   * Reset all mock function call history before each test to ensure
   * complete test isolation. This prevents assertions from being
   * polluted by calls from previous test cases.
   */
  beforeEach(() => {
    vi.clearAllMocks();
  });

  // ==========================================================================
  // Generation Job Endpoints
  // ==========================================================================

  describe('createJob', () => {
    it('should POST the job config to /api/v1/generation/jobs', async () => {
      const mockConfig = createMockJobConfig();
      const mockJob = createMockGenerationJob();
      const mockResponse = wrapApiResponse(mockJob);

      vi.mocked(apiClient.post).mockResolvedValueOnce(mockResponse);

      const result = await createJob(mockConfig);

      expect(apiClient.post).toHaveBeenCalledTimes(1);
      expect(apiClient.post).toHaveBeenCalledWith(
        '/api/v1/generation/jobs',
        mockConfig,
      );
      expect(result).toEqual(mockResponse);
    });

    it('should pass through the complete job config payload', async () => {
      const mockConfig = createMockJobConfig({
        method: GenerationMethod.AI_ML,
        output_format: OutputFormat.PARQUET,
        batch_size: 50000,
        quality_threshold: 0.98,
        template_id: 'tmpl-ref-001',
        tenant_id: 'tenant-custom',
        metadata: { pipeline: 'ci', run_id: '12345' },
      });
      const mockJob = createMockGenerationJob({
        method: GenerationMethod.AI_ML,
      });

      vi.mocked(apiClient.post).mockResolvedValueOnce(wrapApiResponse(mockJob));

      await createJob(mockConfig);

      expect(apiClient.post).toHaveBeenCalledWith(
        '/api/v1/generation/jobs',
        expect.objectContaining({
          method: GenerationMethod.AI_ML,
          output_format: OutputFormat.PARQUET,
          batch_size: 50000,
          quality_threshold: 0.98,
          template_id: 'tmpl-ref-001',
          tenant_id: 'tenant-custom',
          metadata: { pipeline: 'ci', run_id: '12345' },
        }),
      );
    });

    it('should propagate API errors on failure', async () => {
      const apiError = new Error('Network Error');
      vi.mocked(apiClient.post).mockRejectedValueOnce(apiError);

      await expect(createJob(createMockJobConfig())).rejects.toThrow('Network Error');
    });

    it('should handle multi-table job configs correctly', async () => {
      const tables: TableGenerationConfig[] = [
        createMockTableConfig({ table_name: 'GL_JOURNAL_ENTRIES', record_count: 100000 }),
        createMockTableConfig({ table_name: 'AP_INVOICES', record_count: 50000 }),
        createMockTableConfig({ table_name: 'AR_RECEIPTS', record_count: 30000, preserve_relationships: false }),
      ];
      const mockConfig = createMockJobConfig({ tables });

      vi.mocked(apiClient.post).mockResolvedValueOnce(
        wrapApiResponse(createMockGenerationJob({ total_records: 180000 })),
      );

      await createJob(mockConfig);

      expect(apiClient.post).toHaveBeenCalledWith(
        '/api/v1/generation/jobs',
        expect.objectContaining({ tables }),
      );
    });
  });

  // --------------------------------------------------------------------------

  describe('getJobById', () => {
    it('should GET the job from /api/v1/generation/jobs/{jobId}', async () => {
      const mockJob = createMockGenerationJob({ job_id: 'job-uuid-999' });
      const mockResponse = wrapApiResponse(mockJob);

      vi.mocked(apiClient.get).mockResolvedValueOnce(mockResponse);

      const result = await getJobById('job-uuid-999');

      expect(apiClient.get).toHaveBeenCalledTimes(1);
      expect(apiClient.get).toHaveBeenCalledWith(
        '/api/v1/generation/jobs/job-uuid-999',
      );
      expect(result).toEqual(mockResponse);
    });

    it('should encode special characters in the job ID', async () => {
      vi.mocked(apiClient.get).mockResolvedValueOnce(
        wrapApiResponse(createMockGenerationJob()),
      );

      await getJobById('job/with special&chars');

      expect(apiClient.get).toHaveBeenCalledWith(
        '/api/v1/generation/jobs/job%2Fwith%20special%26chars',
      );
    });

    it('should return a completed job with quality score and output location', async () => {
      const completedJob = createMockGenerationJob({
        job_id: 'job-completed-001',
        status: JobStatus.COMPLETED,
        progress: 100,
        generated_records: 75000,
        quality_score: 0.97,
        output_location: 's3://synthetic-data/job-completed-001/output.parquet',
        completed_at: '2025-01-15T11:00:00Z',
      });
      const mockResponse = wrapApiResponse(completedJob);

      vi.mocked(apiClient.get).mockResolvedValueOnce(mockResponse);

      const result = await getJobById('job-completed-001');

      expect(result.data.status).toBe(JobStatus.COMPLETED);
      expect(result.data.quality_score).toBe(0.97);
      expect(result.data.output_location).toBe('s3://synthetic-data/job-completed-001/output.parquet');
    });

    it('should propagate errors for non-existent jobs', async () => {
      const notFoundError = new Error('Not Found');
      vi.mocked(apiClient.get).mockRejectedValueOnce(notFoundError);

      await expect(getJobById('nonexistent-id')).rejects.toThrow('Not Found');
    });
  });

  // --------------------------------------------------------------------------

  describe('getJobs', () => {
    it('should GET jobs from /api/v1/generation/jobs with pagination params', async () => {
      const mockJobs = [
        createMockGenerationJob({ job_id: 'job-001' }),
        createMockGenerationJob({ job_id: 'job-002' }),
      ];
      const paginatedResult = wrapPaginatedResult(mockJobs, 50, 1, 20);
      const mockResponse = wrapApiResponse(paginatedResult);

      vi.mocked(apiClient.get).mockResolvedValueOnce(mockResponse);

      const params = {
        page: 1,
        page_size: 20,
        sort_by: 'created_at',
        sort_direction: 'desc' as const,
      };

      const result = await getJobs(params);

      expect(apiClient.get).toHaveBeenCalledTimes(1);
      expect(apiClient.get).toHaveBeenCalledWith(
        '/api/v1/generation/jobs',
        { params },
      );
      expect(result.data.items).toHaveLength(2);
      expect(result.data.total).toBe(50);
      expect(result.data.page).toBe(1);
      expect(result.data.total_pages).toBe(3);
    });

    it('should call without params when none are provided', async () => {
      const emptyResult = wrapPaginatedResult<GenerationJob>([], 0);
      vi.mocked(apiClient.get).mockResolvedValueOnce(wrapApiResponse(emptyResult));

      await getJobs();

      expect(apiClient.get).toHaveBeenCalledWith(
        '/api/v1/generation/jobs',
        { params: undefined },
      );
    });

    it('should forward status and method filter parameters', async () => {
      const filteredResult = wrapPaginatedResult([createMockGenerationJob()], 1);
      vi.mocked(apiClient.get).mockResolvedValueOnce(wrapApiResponse(filteredResult));

      const params = {
        page: 1,
        page_size: 10,
        status: JobStatus.COMPLETED,
        method: 'ai_ml',
      };

      await getJobs(params);

      expect(apiClient.get).toHaveBeenCalledWith(
        '/api/v1/generation/jobs',
        {
          params: expect.objectContaining({
            status: JobStatus.COMPLETED,
            method: 'ai_ml',
            page: 1,
            page_size: 10,
          }),
        },
      );
    });

    it('should handle pagination with multiple pages', async () => {
      const jobs = Array.from({ length: 20 }, (_, i) =>
        createMockGenerationJob({ job_id: `job-page2-${i}` }),
      );
      const paginatedResult = wrapPaginatedResult(jobs, 100, 2, 20);
      vi.mocked(apiClient.get).mockResolvedValueOnce(wrapApiResponse(paginatedResult));

      const result = await getJobs({ page: 2, page_size: 20 });

      expect(result.data.page).toBe(2);
      expect(result.data.total_pages).toBe(5);
      expect(result.data.items).toHaveLength(20);
    });

    it('should support sort_by and sort_direction parameters', async () => {
      vi.mocked(apiClient.get).mockResolvedValueOnce(
        wrapApiResponse(wrapPaginatedResult<GenerationJob>([], 0)),
      );

      const params = {
        page: 1,
        page_size: 50,
        sort_by: 'quality_score',
        sort_direction: 'asc' as const,
      };

      await getJobs(params);

      expect(apiClient.get).toHaveBeenCalledWith(
        '/api/v1/generation/jobs',
        {
          params: expect.objectContaining({
            sort_by: 'quality_score',
            sort_direction: 'asc',
          }),
        },
      );
    });
  });

  // --------------------------------------------------------------------------

  describe('cancelJob', () => {
    it('should POST to /api/v1/generation/jobs/{jobId}/cancel', async () => {
      const cancelledJob = createMockGenerationJob({
        job_id: 'job-to-cancel',
        status: JobStatus.FAILED,
        error_message: 'Cancelled by user',
      });
      const mockResponse = wrapApiResponse(cancelledJob);

      vi.mocked(apiClient.post).mockResolvedValueOnce(mockResponse);

      const result = await cancelJob('job-to-cancel');

      expect(apiClient.post).toHaveBeenCalledTimes(1);
      expect(apiClient.post).toHaveBeenCalledWith(
        '/api/v1/generation/jobs/job-to-cancel/cancel',
      );
      expect(result.data.status).toBe(JobStatus.FAILED);
    });

    it('should encode special characters in the job ID for cancel', async () => {
      vi.mocked(apiClient.post).mockResolvedValueOnce(
        wrapApiResponse(createMockGenerationJob()),
      );

      await cancelJob('job/special&id');

      expect(apiClient.post).toHaveBeenCalledWith(
        '/api/v1/generation/jobs/job%2Fspecial%26id/cancel',
      );
    });

    it('should propagate errors when cancellation fails', async () => {
      vi.mocked(apiClient.post).mockRejectedValueOnce(
        new Error('Job cannot be cancelled in current state'),
      );

      await expect(cancelJob('job-finished')).rejects.toThrow(
        'Job cannot be cancelled in current state',
      );
    });
  });

  // --------------------------------------------------------------------------

  describe('getJobProgress', () => {
    it('should GET progress from /api/v1/generation/jobs/{jobId}/progress', async () => {
      const mockProgress = createMockBatchProgress();
      const mockResponse = wrapApiResponse(mockProgress);

      vi.mocked(apiClient.get).mockResolvedValueOnce(mockResponse);

      const result = await getJobProgress('job-active-001');

      expect(apiClient.get).toHaveBeenCalledTimes(1);
      expect(apiClient.get).toHaveBeenCalledWith(
        '/api/v1/generation/jobs/job-active-001/progress',
      );
      expect(result.data.current_batch).toBe(3);
      expect(result.data.total_batches).toBe(8);
      expect(result.data.records_generated).toBe(30000);
      expect(result.data.records_total).toBe(75000);
      expect(result.data.percentage).toBe(40);
      expect(result.data.estimated_remaining_seconds).toBe(180);
    });

    it('should handle progress with null estimated_remaining_seconds (first batch)', async () => {
      const firstBatchProgress = createMockBatchProgress({
        current_batch: 1,
        records_generated: 10000,
        percentage: 13,
        estimated_remaining_seconds: null,
      });

      vi.mocked(apiClient.get).mockResolvedValueOnce(
        wrapApiResponse(firstBatchProgress),
      );

      const result = await getJobProgress('job-starting-001');

      expect(result.data.estimated_remaining_seconds).toBeNull();
      expect(result.data.current_batch).toBe(1);
    });

    it('should handle completed job progress (100%)', async () => {
      const completedProgress = createMockBatchProgress({
        current_batch: 8,
        total_batches: 8,
        records_generated: 75000,
        records_total: 75000,
        percentage: 100,
        estimated_remaining_seconds: 0,
      });

      vi.mocked(apiClient.get).mockResolvedValueOnce(
        wrapApiResponse(completedProgress),
      );

      const result = await getJobProgress('job-done-001');

      expect(result.data.percentage).toBe(100);
      expect(result.data.records_generated).toBe(result.data.records_total);
    });

    it('should encode special characters in the job ID for progress', async () => {
      vi.mocked(apiClient.get).mockResolvedValueOnce(
        wrapApiResponse(createMockBatchProgress()),
      );

      await getJobProgress('job/with%special');

      expect(apiClient.get).toHaveBeenCalledWith(
        '/api/v1/generation/jobs/job%2Fwith%25special/progress',
      );
    });
  });

  // --------------------------------------------------------------------------

  describe('getJobQualityReport', () => {
    it('should GET quality from /api/v1/generation/jobs/{jobId}/quality', async () => {
      const mockReport = createMockQualityReport();
      const mockResponse = wrapApiResponse(mockReport);

      vi.mocked(apiClient.get).mockResolvedValueOnce(mockResponse);

      const result = await getJobQualityReport('job-validated-001');

      expect(apiClient.get).toHaveBeenCalledTimes(1);
      expect(apiClient.get).toHaveBeenCalledWith(
        '/api/v1/generation/jobs/job-validated-001/quality',
      );
      expect(result.data.overall_score).toBe(0.97);
      expect(result.data.statistical_score).toBe(0.98);
      expect(result.data.business_rules_score).toBe(0.96);
      expect(result.data.referential_integrity_score).toBe(0.95);
      expect(result.data.validated_at).toBe('2025-01-15T10:30:00Z');
    });

    it('should return detailed quality metrics in the details field', async () => {
      const detailedReport = createMockQualityReport({
        details: {
          per_table_scores: {
            GL_JOURNAL_ENTRIES: 0.98,
            AP_INVOICES: 0.96,
            AR_RECEIPTS: 0.94,
          },
          rule_violations: 2,
          distribution_comparisons: {
            ks_test_results: { amount: 0.03, date: 0.01 },
          },
        },
      });

      vi.mocked(apiClient.get).mockResolvedValueOnce(
        wrapApiResponse(detailedReport),
      );

      const result = await getJobQualityReport('job-detailed-001');

      expect(result.data.details).toBeDefined();
      expect(result.data.details.per_table_scores).toBeDefined();
      expect(result.data.details.rule_violations).toBe(2);
    });

    it('should verify the weighted scoring model fields are present', async () => {
      const report = createMockQualityReport({
        overall_score: 0.96,
        statistical_score: 0.97,
        business_rules_score: 0.95,
        referential_integrity_score: 0.96,
      });

      vi.mocked(apiClient.get).mockResolvedValueOnce(wrapApiResponse(report));

      const result = await getJobQualityReport('job-score-check');

      // Verify all weighted scoring model components are present
      const { overall_score, statistical_score, business_rules_score, referential_integrity_score } = result.data;
      expect(typeof overall_score).toBe('number');
      expect(typeof statistical_score).toBe('number');
      expect(typeof business_rules_score).toBe('number');
      expect(typeof referential_integrity_score).toBe('number');

      // Verify scores are within valid range (0.0 - 1.0)
      expect(overall_score).toBeGreaterThanOrEqual(0);
      expect(overall_score).toBeLessThanOrEqual(1);
    });

    it('should propagate errors for jobs not yet validated', async () => {
      vi.mocked(apiClient.get).mockRejectedValueOnce(
        new Error('Quality report not available'),
      );

      await expect(getJobQualityReport('job-not-validated')).rejects.toThrow(
        'Quality report not available',
      );
    });
  });

  // --------------------------------------------------------------------------

  describe('downloadJobOutput', () => {
    it('should GET download from /api/v1/generation/jobs/{jobId}/download with blob responseType', async () => {
      const mockBlob = new Blob(['mock,csv,data'], { type: 'text/csv' });

      vi.mocked(apiClient.get).mockResolvedValueOnce(mockBlob);

      const result = await downloadJobOutput('job-download-001');

      expect(apiClient.get).toHaveBeenCalledTimes(1);
      expect(apiClient.get).toHaveBeenCalledWith(
        '/api/v1/generation/jobs/job-download-001/download',
        {
          params: {},
          responseType: 'blob',
          timeout: 120000,
        },
      );
      expect(result).toBe(mockBlob);
    });

    it('should include format query parameter when override is specified', async () => {
      const mockBlob = new Blob(['{}'], { type: 'application/json' });

      vi.mocked(apiClient.get).mockResolvedValueOnce(mockBlob);

      await downloadJobOutput('job-download-002', 'json');

      expect(apiClient.get).toHaveBeenCalledWith(
        '/api/v1/generation/jobs/job-download-002/download',
        {
          params: { format: 'json' },
          responseType: 'blob',
          timeout: 120000,
        },
      );
    });

    it('should support CSV format override', async () => {
      vi.mocked(apiClient.get).mockResolvedValueOnce(
        new Blob(['col1,col2\nval1,val2'], { type: 'text/csv' }),
      );

      await downloadJobOutput('job-csv-001', 'csv');

      expect(apiClient.get).toHaveBeenCalledWith(
        expect.stringContaining('/download'),
        expect.objectContaining({
          params: { format: 'csv' },
          responseType: 'blob',
        }),
      );
    });

    it('should support SQL format override', async () => {
      vi.mocked(apiClient.get).mockResolvedValueOnce(
        new Blob(['INSERT INTO...'], { type: 'application/sql' }),
      );

      await downloadJobOutput('job-sql-001', 'sql');

      expect(apiClient.get).toHaveBeenCalledWith(
        expect.stringContaining('/download'),
        expect.objectContaining({
          params: { format: 'sql' },
        }),
      );
    });

    it('should support Parquet format override', async () => {
      vi.mocked(apiClient.get).mockResolvedValueOnce(
        new Blob([new ArrayBuffer(128)], { type: 'application/octet-stream' }),
      );

      await downloadJobOutput('job-parquet-001', 'parquet');

      expect(apiClient.get).toHaveBeenCalledWith(
        expect.stringContaining('/download'),
        expect.objectContaining({
          params: { format: 'parquet' },
        }),
      );
    });

    it('should use extended timeout of 120000ms for downloads', async () => {
      vi.mocked(apiClient.get).mockResolvedValueOnce(new Blob([]));

      await downloadJobOutput('job-large-001');

      expect(apiClient.get).toHaveBeenCalledWith(
        expect.any(String),
        expect.objectContaining({ timeout: 120000 }),
      );
    });

    it('should not include format param when no format override is provided', async () => {
      vi.mocked(apiClient.get).mockResolvedValueOnce(new Blob([]));

      await downloadJobOutput('job-default-fmt');

      const callArgs = vi.mocked(apiClient.get).mock.calls[0];
      const requestConfig = callArgs[1] as Record<string, unknown>;
      expect(requestConfig.params).toEqual({});
    });

    it('should propagate errors for download failures', async () => {
      vi.mocked(apiClient.get).mockRejectedValueOnce(
        new Error('Download timed out'),
      );

      await expect(downloadJobOutput('job-timeout')).rejects.toThrow(
        'Download timed out',
      );
    });

    it('should encode special characters in job ID for download', async () => {
      vi.mocked(apiClient.get).mockResolvedValueOnce(new Blob([]));

      await downloadJobOutput('job/special&chars');

      expect(apiClient.get).toHaveBeenCalledWith(
        '/api/v1/generation/jobs/job%2Fspecial%26chars/download',
        expect.any(Object),
      );
    });
  });

  // --------------------------------------------------------------------------

  describe('retryJob', () => {
    it('should POST to /api/v1/generation/jobs/{jobId}/retry', async () => {
      const retriedJob = createMockGenerationJob({
        job_id: 'job-retry-001',
        status: JobStatus.SUBMITTED,
      });
      const mockResponse = wrapApiResponse(retriedJob);

      vi.mocked(apiClient.post).mockResolvedValueOnce(mockResponse);

      const result = await retryJob('job-failed-001');

      expect(apiClient.post).toHaveBeenCalledTimes(1);
      expect(apiClient.post).toHaveBeenCalledWith(
        '/api/v1/generation/jobs/job-failed-001/retry',
      );
      expect(result.data.job_id).toBe('job-retry-001');
      expect(result.data.status).toBe(JobStatus.SUBMITTED);
    });

    it('should encode special characters in job ID for retry', async () => {
      vi.mocked(apiClient.post).mockResolvedValueOnce(
        wrapApiResponse(createMockGenerationJob()),
      );

      await retryJob('job/retry&special');

      expect(apiClient.post).toHaveBeenCalledWith(
        '/api/v1/generation/jobs/job%2Fretry%26special/retry',
      );
    });

    it('should propagate errors when retry is not allowed', async () => {
      vi.mocked(apiClient.post).mockRejectedValueOnce(
        new Error('Only failed jobs can be retried'),
      );

      await expect(retryJob('job-active-001')).rejects.toThrow(
        'Only failed jobs can be retried',
      );
    });
  });

  // ==========================================================================
  // Template Endpoints
  // ==========================================================================

  describe('getTemplates', () => {
    it('should GET templates from /api/v1/templates with pagination params', async () => {
      const mockTemplates = [
        createMockTemplate({ template_id: 'tmpl-001' }),
        createMockTemplate({ template_id: 'tmpl-002', name: 'HR Template' }),
      ];
      const paginatedResult = wrapPaginatedResult(mockTemplates, 25, 1, 12);
      const mockResponse = wrapApiResponse(paginatedResult);

      vi.mocked(apiClient.get).mockResolvedValueOnce(mockResponse);

      const params = {
        page: 1,
        page_size: 12,
        sort_by: 'usage_count',
        sort_direction: 'desc' as const,
      };

      const result = await getTemplates(params);

      expect(apiClient.get).toHaveBeenCalledTimes(1);
      expect(apiClient.get).toHaveBeenCalledWith(
        '/api/v1/templates',
        { params },
      );
      expect(result.data.items).toHaveLength(2);
      expect(result.data.total).toBe(25);
    });

    it('should call without params when none are provided', async () => {
      vi.mocked(apiClient.get).mockResolvedValueOnce(
        wrapApiResponse(wrapPaginatedResult<GenerationTemplate>([], 0)),
      );

      await getTemplates();

      expect(apiClient.get).toHaveBeenCalledWith(
        '/api/v1/templates',
        { params: undefined },
      );
    });

    it('should forward category filter parameter', async () => {
      vi.mocked(apiClient.get).mockResolvedValueOnce(
        wrapApiResponse(wrapPaginatedResult([createMockTemplate()], 1)),
      );

      const params = {
        page: 1,
        page_size: 20,
        category: 'financial_accounting',
      };

      await getTemplates(params);

      expect(apiClient.get).toHaveBeenCalledWith(
        '/api/v1/templates',
        {
          params: expect.objectContaining({
            category: 'financial_accounting',
          }),
        },
      );
    });

    it('should forward erp_type and method filter parameters', async () => {
      vi.mocked(apiClient.get).mockResolvedValueOnce(
        wrapApiResponse(wrapPaginatedResult<GenerationTemplate>([], 0)),
      );

      const params = {
        page: 1,
        page_size: 20,
        erp_type: 'sap',
        method: 'rules_based',
      };

      await getTemplates(params);

      expect(apiClient.get).toHaveBeenCalledWith(
        '/api/v1/templates',
        {
          params: expect.objectContaining({
            erp_type: 'sap',
            method: 'rules_based',
          }),
        },
      );
    });

    it('should handle empty template list', async () => {
      vi.mocked(apiClient.get).mockResolvedValueOnce(
        wrapApiResponse(wrapPaginatedResult<GenerationTemplate>([], 0)),
      );

      const result = await getTemplates({ page: 1, page_size: 20 });

      expect(result.data.items).toHaveLength(0);
      expect(result.data.total).toBe(0);
      expect(result.data.total_pages).toBe(0);
    });
  });

  // --------------------------------------------------------------------------

  describe('getTemplateById', () => {
    it('should GET template from /api/v1/templates/{templateId}', async () => {
      const mockTemplate = createMockTemplate({ template_id: 'tmpl-uuid-999' });
      const mockResponse = wrapApiResponse(mockTemplate);

      vi.mocked(apiClient.get).mockResolvedValueOnce(mockResponse);

      const result = await getTemplateById('tmpl-uuid-999');

      expect(apiClient.get).toHaveBeenCalledTimes(1);
      expect(apiClient.get).toHaveBeenCalledWith(
        '/api/v1/templates/tmpl-uuid-999',
      );
      expect(result.data.template_id).toBe('tmpl-uuid-999');
      expect(result.data.name).toBe('SAP Financial Accounting - Standard');
      expect(result.data.config).toBeDefined();
    });

    it('should encode special characters in template ID', async () => {
      vi.mocked(apiClient.get).mockResolvedValueOnce(
        wrapApiResponse(createMockTemplate()),
      );

      await getTemplateById('tmpl/special&chars');

      expect(apiClient.get).toHaveBeenCalledWith(
        '/api/v1/templates/tmpl%2Fspecial%26chars',
      );
    });

    it('should return template with full config including tables and method', async () => {
      const template = createMockTemplate({
        config: createMockJobConfig({
          method: GenerationMethod.AI_ML,
          tables: [
            createMockTableConfig({ table_name: 'HR_EMPLOYEES', record_count: 10000 }),
          ],
        }),
      });

      vi.mocked(apiClient.get).mockResolvedValueOnce(wrapApiResponse(template));

      const result = await getTemplateById('tmpl-hr-001');

      expect(result.data.config.method).toBe(GenerationMethod.AI_ML);
      expect(result.data.config.tables).toHaveLength(1);
      expect(result.data.config.tables[0].table_name).toBe('HR_EMPLOYEES');
    });

    it('should propagate errors for non-existent templates', async () => {
      vi.mocked(apiClient.get).mockRejectedValueOnce(new Error('Not Found'));

      await expect(getTemplateById('nonexistent')).rejects.toThrow('Not Found');
    });
  });

  // --------------------------------------------------------------------------

  describe('createTemplate', () => {
    it('should POST template to /api/v1/templates', async () => {
      const request = createMockCreateTemplateRequest();
      const createdTemplate = createMockTemplate({
        template_id: 'tmpl-new-001',
        name: request.name,
        description: request.description,
        category: request.category,
        is_public: request.is_public,
        usage_count: 0,
      });
      const mockResponse = wrapApiResponse(createdTemplate);

      vi.mocked(apiClient.post).mockResolvedValueOnce(mockResponse);

      const result = await createTemplate(request);

      expect(apiClient.post).toHaveBeenCalledTimes(1);
      expect(apiClient.post).toHaveBeenCalledWith(
        '/api/v1/templates',
        request,
      );
      expect(result.data.template_id).toBe('tmpl-new-001');
      expect(result.data.name).toBe('HR Payroll Template');
      expect(result.data.usage_count).toBe(0);
    });

    it('should pass through the complete template request payload', async () => {
      const request = createMockCreateTemplateRequest({
        name: 'Sales Template - Custom',
        category: 'sales_distribution',
        is_public: true,
        config: createMockJobConfig({
          method: GenerationMethod.MASKING,
          output_format: OutputFormat.JSON,
        }),
      });

      vi.mocked(apiClient.post).mockResolvedValueOnce(
        wrapApiResponse(createMockTemplate()),
      );

      await createTemplate(request);

      expect(apiClient.post).toHaveBeenCalledWith(
        '/api/v1/templates',
        expect.objectContaining({
          name: 'Sales Template - Custom',
          category: 'sales_distribution',
          is_public: true,
          config: expect.objectContaining({
            method: GenerationMethod.MASKING,
            output_format: OutputFormat.JSON,
          }),
        }),
      );
    });

    it('should propagate validation errors', async () => {
      vi.mocked(apiClient.post).mockRejectedValueOnce(
        new Error('Validation failed: name is required'),
      );

      await expect(
        createTemplate(createMockCreateTemplateRequest({ name: '' })),
      ).rejects.toThrow('Validation failed');
    });
  });

  // --------------------------------------------------------------------------

  describe('updateTemplate', () => {
    it('should PUT partial updates to /api/v1/templates/{templateId}', async () => {
      const updates: Partial<CreateTemplateRequest> = {
        name: 'Updated Template Name',
        description: 'Updated description',
      };
      const updatedTemplate = createMockTemplate({
        template_id: 'tmpl-update-001',
        name: 'Updated Template Name',
        description: 'Updated description',
        updated_at: '2025-01-16T09:00:00Z',
      });
      const mockResponse = wrapApiResponse(updatedTemplate);

      vi.mocked(apiClient.put).mockResolvedValueOnce(mockResponse);

      const result = await updateTemplate('tmpl-update-001', updates);

      expect(apiClient.put).toHaveBeenCalledTimes(1);
      expect(apiClient.put).toHaveBeenCalledWith(
        '/api/v1/templates/tmpl-update-001',
        updates,
      );
      expect(result.data.name).toBe('Updated Template Name');
      expect(result.data.updated_at).toBe('2025-01-16T09:00:00Z');
    });

    it('should support updating config and is_public fields', async () => {
      const updates: Partial<CreateTemplateRequest> = {
        config: createMockJobConfig({ batch_size: 20000 }),
        is_public: false,
      };

      vi.mocked(apiClient.put).mockResolvedValueOnce(
        wrapApiResponse(createMockTemplate({
          config: createMockJobConfig({ batch_size: 20000 }),
          is_public: false,
        })),
      );

      await updateTemplate('tmpl-config-update', updates);

      expect(apiClient.put).toHaveBeenCalledWith(
        '/api/v1/templates/tmpl-config-update',
        expect.objectContaining({
          config: expect.objectContaining({ batch_size: 20000 }),
          is_public: false,
        }),
      );
    });

    it('should encode special characters in template ID for updates', async () => {
      vi.mocked(apiClient.put).mockResolvedValueOnce(
        wrapApiResponse(createMockTemplate()),
      );

      await updateTemplate('tmpl/special&id', { name: 'New Name' });

      expect(apiClient.put).toHaveBeenCalledWith(
        '/api/v1/templates/tmpl%2Fspecial%26id',
        { name: 'New Name' },
      );
    });

    it('should propagate errors on update failure', async () => {
      vi.mocked(apiClient.put).mockRejectedValueOnce(
        new Error('Forbidden: insufficient permissions'),
      );

      await expect(
        updateTemplate('tmpl-no-access', { name: 'Fail' }),
      ).rejects.toThrow('Forbidden');
    });
  });

  // --------------------------------------------------------------------------

  describe('deleteTemplate', () => {
    it('should DELETE template at /api/v1/templates/{templateId}', async () => {
      const mockResponse: ApiResponse<void> = {
        success: true,
        data: undefined as unknown as void,
        message: 'Template deleted successfully',
        timestamp: '2025-01-16T10:00:00Z',
      };

      vi.mocked(apiClient.delete).mockResolvedValueOnce(mockResponse);

      const result = await deleteTemplate('tmpl-delete-001');

      expect(apiClient.delete).toHaveBeenCalledTimes(1);
      expect(apiClient.delete).toHaveBeenCalledWith(
        '/api/v1/templates/tmpl-delete-001',
      );
      expect(result.success).toBe(true);
    });

    it('should encode special characters in template ID for deletion', async () => {
      vi.mocked(apiClient.delete).mockResolvedValueOnce({
        success: true,
        data: undefined as unknown as void,
        timestamp: '2025-01-16T10:00:00Z',
      });

      await deleteTemplate('tmpl/special&id');

      expect(apiClient.delete).toHaveBeenCalledWith(
        '/api/v1/templates/tmpl%2Fspecial%26id',
      );
    });

    it('should propagate errors when template deletion fails', async () => {
      vi.mocked(apiClient.delete).mockRejectedValueOnce(
        new Error('Template not found'),
      );

      await expect(deleteTemplate('tmpl-nonexistent')).rejects.toThrow(
        'Template not found',
      );
    });

    it('should propagate permission errors', async () => {
      vi.mocked(apiClient.delete).mockRejectedValueOnce(
        new Error('Forbidden: only template owner can delete'),
      );

      await expect(deleteTemplate('tmpl-not-owner')).rejects.toThrow(
        'Forbidden',
      );
    });
  });
});
