/**
 * @fileoverview TypeScript type definitions for the generation job domain.
 *
 * Defines all enums, interfaces, and types used across the Web Console for
 * synthetic data generation workflows. These types align with the backend
 * Pydantic models in src/backend/api_gateway/schemas/generation.py and are
 * consumed by the Generation Wizard (S-002), Job Monitoring (S-004),
 * Dashboard (S-001), jobStore (Zustand), and generationApi service.
 *
 * @module types/generation
 */

// ---------------------------------------------------------------------------
// Enums
// ---------------------------------------------------------------------------

/**
 * Supported synthetic data generation methods.
 *
 * Each method represents a distinct algorithmic strategy for producing
 * synthetic records that mirror the statistical properties and business
 * constraints of production ERP data.
 *
 * - **AI_ML** — GAN / VAE deep-learning models trained on statistical profiles.
 * - **RULES_BASED** — Deterministic business-rules engine that enforces
 *   domain-specific constraints (e.g., GL balancing, tax calculations).
 * - **STATISTICAL** — Distribution-fitting synthesis via SciPy / NumPy that
 *   reproduces column-level statistical distributions and inter-column
 *   correlations using copula models.
 * - **MASKING** — Intelligent data masking that transforms sensitive fields
 *   while preserving referential integrity and format consistency.
 */
export enum GenerationMethod {
  /** GAN / VAE deep-learning based generation */
  AI_ML = 'ai_ml',
  /** Deterministic business-rules engine */
  RULES_BASED = 'rules_based',
  /** SciPy / NumPy distribution-based statistical synthesis */
  STATISTICAL = 'statistical',
  /** Privacy-preserving intelligent data masking */
  MASKING = 'masking',
}

/**
 * Lifecycle status of a generation job.
 *
 * Jobs transition through a deterministic state machine:
 *
 * ```
 * SUBMITTED → GENERATING → VALIDATING → CERTIFYING → PROVISIONING → COMPLETED
 *       ↘          ↘            ↘             ↘              ↘
 *        └──────────┴────────────┴──────────────┴──────────────→ FAILED
 * ```
 *
 * Any stage may transition to `FAILED` on unrecoverable errors.
 */
export enum JobStatus {
  /** Job has been submitted and is queued for processing */
  SUBMITTED = 'submitted',
  /** Batch generation is actively in progress */
  GENERATING = 'generating',
  /** Generated data is undergoing quality validation (≥95% fidelity target) */
  VALIDATING = 'validating',
  /** Compliance service is certifying zero-PII and regulatory adherence */
  CERTIFYING = 'certifying',
  /** Output is being provisioned to target database or cloud storage */
  PROVISIONING = 'provisioning',
  /** Job finished successfully with all checks passed */
  COMPLETED = 'completed',
  /** Job terminated due to an unrecoverable error */
  FAILED = 'failed',
}

/**
 * Supported output formats for generated synthetic data.
 *
 * Each format is optimised for different consumption patterns:
 *
 * - **SQL** — INSERT / COPY statements for direct database provisioning.
 * - **CSV** — Delimited text for broad tool compatibility.
 * - **JSON** — JSON / JSONL for API and streaming consumers.
 * - **PARQUET** — Apache Parquet columnar format for analytics workloads
 *   with superior compression ratios.
 */
export enum OutputFormat {
  /** SQL INSERT / COPY statement format */
  SQL = 'sql',
  /** Comma-separated values */
  CSV = 'csv',
  /** JSON / JSONL format */
  JSON = 'json',
  /** Apache Parquet columnar format */
  PARQUET = 'parquet',
}

// ---------------------------------------------------------------------------
// Interfaces
// ---------------------------------------------------------------------------

/**
 * Per-table generation configuration.
 *
 * Allows fine-grained control over how many records are generated for each
 * table, which columns receive custom generation overrides, and whether
 * foreign-key relationships should be automatically maintained.
 */
export interface TableGenerationConfig {
  /** Fully-qualified target table name (e.g., "GL_JOURNAL_ENTRIES") */
  table_name: string;

  /**
   * Number of synthetic records to generate for this table.
   * Must be greater than 0.
   */
  record_count: number;

  /**
   * Optional column-specific generation overrides.
   *
   * Keys are column names; values are generator-specific configuration
   * objects whose shape depends on the chosen {@link GenerationMethod}.
   *
   * @example
   * ```ts
   * {
   *   "currency_code": { "fixed_value": "USD" },
   *   "amount":        { "distribution": "log_normal", "mean": 5000, "std": 1200 }
   * }
   * ```
   */
  column_overrides?: Record<string, unknown>;

  /**
   * When `true` (the default), the generation engine automatically
   * maintains foreign-key relationships across tables and ERP modules.
   */
  preserve_relationships: boolean;
}

/**
 * Complete configuration for a new generation job request.
 *
 * Maps to the backend `GenerationJobRequest` Pydantic model sent via
 * `POST /api/v1/generation/jobs`.
 */
export interface JobConfig {
  /** Generation strategy to employ */
  method: GenerationMethod;

  /** Reference ID of the discovered ERP schema to generate data for */
  schema_id: string;

  /** Per-table generation specifications */
  tables: TableGenerationConfig[];

  /**
   * Desired output format.
   * @default OutputFormat.CSV
   */
  output_format: OutputFormat;

  /**
   * Number of records processed per batch during generation.
   * Valid range: 1 000 – 100 000.
   * @default 10000
   */
  batch_size: number;

  /**
   * Minimum acceptable quality score (0.0 – 1.0).
   * The weighted scoring model targets ≥ 0.95 (95 %) fidelity using
   * 40 % statistical + 30 % business rules + 30 % referential integrity.
   * @default 0.95
   */
  quality_threshold: number;

  /**
   * Optional reference to a pre-configured generation template.
   * When provided, template defaults are merged with explicit settings.
   */
  template_id?: string;

  /** Tenant namespace for multi-tenant isolation */
  tenant_id?: string;

  /**
   * Arbitrary key-value metadata attached to the job.
   * Useful for CI/CD pipeline context, tagging, or custom labelling.
   */
  metadata?: Record<string, unknown>;
}

/**
 * Representation of a generation job returned by the API.
 *
 * Maps to the backend `GenerationJobResponse` Pydantic model returned by
 * `GET /api/v1/generation/jobs/{id}`.
 */
export interface GenerationJob {
  /** Unique identifier for the generation job */
  job_id: string;

  /** Current lifecycle status */
  status: JobStatus;

  /** Generation method used for this job */
  method: GenerationMethod;

  /** Overall completion percentage (0 – 100) */
  progress: number;

  /** Total number of records requested across all tables */
  total_records: number;

  /** Number of records generated so far */
  generated_records: number;

  /**
   * Composite quality score assigned after the validation stage.
   * Range 0.0 – 1.0.  `null` until validation completes.
   */
  quality_score?: number | null;

  /**
   * URI or path to the generated output (file system, S3, Azure Blob, etc.).
   * `null` until provisioning completes.
   */
  output_location?: string | null;

  /**
   * Human-readable error description when the job has `FAILED` status.
   * `null` for non-failed jobs.
   */
  error_message?: string | null;

  /** ISO 8601 timestamp of job creation */
  created_at: string;

  /** ISO 8601 timestamp of the last status update */
  updated_at: string;

  /**
   * ISO 8601 timestamp of successful completion.
   * `null` until the job reaches `COMPLETED` status.
   */
  completed_at?: string | null;

  /** Tenant namespace the job belongs to */
  tenant_id?: string;
}

/**
 * Real-time progress snapshot for an active generation job.
 *
 * Emitted via WebSocket or polled from the progress endpoint to drive
 * the Job Monitoring screen (S-004) progress bars and ETA indicators.
 */
export interface BatchProgress {
  /** Ordinal number of the batch currently being processed (1-based) */
  current_batch: number;

  /** Total number of batches that will be processed */
  total_batches: number;

  /** Cumulative count of records generated so far */
  records_generated: number;

  /** Total number of records to generate across all batches */
  records_total: number;

  /** Overall completion percentage (0 – 100) */
  percentage: number;

  /**
   * Estimated seconds remaining until generation completes.
   * `null` when insufficient data is available for a reliable estimate
   * (e.g., during the first batch).
   */
  estimated_remaining_seconds?: number | null;
}
