/**
 * TypeScript type definitions for the statistical profiling domain.
 *
 * These types model the output of the Profiling Service which captures
 * schema metadata and statistical distribution information from source
 * ERP systems. Critically, the profiling process extracts metadata only —
 * no raw production data is accessed or stored (Constraint C-001).
 *
 * Types are consumed by:
 *  - Profile Viewer page (Screen S-006)
 *  - schemaStore Zustand store
 *  - profileApi service
 *  - DistributionChart component
 *
 * Aligned with backend Pydantic models in:
 *  - src/backend/api_gateway/schemas/profile.py
 *  - src/backend/profiling_service/profilers/statistical_profiler.py
 */

// ---------------------------------------------------------------------------
// Enumerations
// ---------------------------------------------------------------------------

/**
 * Statistical distribution types detected by the SciPy/NumPy profiling engine.
 *
 * Each value corresponds to a probability distribution that may best fit
 * a numeric or categorical column's value spread. The profiler performs
 * goodness-of-fit testing across these candidates and selects the one
 * with the highest fit score.
 */
export enum DistributionType {
  /** Gaussian / bell-curve distribution */
  NORMAL = 'normal',

  /** Log-normal distribution (log of values is normally distributed) */
  LOG_NORMAL = 'log_normal',

  /** Poisson distribution (discrete events per interval) */
  POISSON = 'poisson',

  /** Uniform distribution (equal probability across range) */
  UNIFORM = 'uniform',

  /** Categorical distribution (discrete unordered categories) */
  CATEGORICAL = 'categorical',

  /** Exponential distribution (memoryless inter-arrival times) */
  EXPONENTIAL = 'exponential',

  /** Binomial distribution (number of successes in n trials) */
  BINOMIAL = 'binomial',

  /** Fallback when no known distribution fits the data adequately */
  UNKNOWN = 'unknown',
}

// ---------------------------------------------------------------------------
// Distribution & Statistics Interfaces
// ---------------------------------------------------------------------------

/**
 * Represents the best-fit statistical distribution for a profiled column.
 *
 * The profiling engine evaluates each candidate {@link DistributionType} and
 * returns the one with the highest goodness-of-fit score along with the
 * distribution-specific parameters (e.g., `{ mean: 50, std_dev: 10 }` for a
 * normal distribution).
 */
export interface Distribution {
  /** The best-fit distribution type identified by the profiler. */
  type: DistributionType;

  /**
   * Distribution-specific parameters.
   *
   * Examples by distribution type:
   *  - normal:      `{ mean: number, std_dev: number }`
   *  - log_normal:  `{ mu: number, sigma: number }`
   *  - poisson:     `{ lambda: number }`
   *  - uniform:     `{ low: number, high: number }`
   *  - categorical: `{ category_count: number }`
   *  - exponential: `{ lambda: number }`
   *  - binomial:    `{ n: number, p: number }`
   */
  parameters: Record<string, number>;

  /**
   * Goodness-of-fit score between 0.0 and 1.0 indicating how well the
   * chosen distribution matches the observed data. A score of 1.0 is a
   * perfect fit. Null when the fit could not be determined.
   */
  fit_score?: number | null;
}

/**
 * Comprehensive statistical summary for a single profiled column.
 *
 * Maps to the `StatisticalSummary` Pydantic model on the backend.
 * Numeric aggregate fields (`mean`, `median`, etc.) are nullable because
 * they are only meaningful for numeric column types.
 */
export interface ColumnStatistics {
  /** Column name as reported by the ERP schema. */
  column_name: string;

  /** Detected data type of the column (e.g., "varchar", "integer", "decimal"). */
  data_type: string;

  /** Best-fit statistical distribution with fitted parameters. */
  distribution: Distribution;

  /** Arithmetic mean value. Null for non-numeric columns. */
  mean?: number | null;

  /** Median (50th percentile) value. Null for non-numeric columns. */
  median?: number | null;

  /**
   * Standard deviation of the column's values.
   * Always >= 0 when present. Null for non-numeric columns.
   */
  std_dev?: number | null;

  /** Minimum observed value. Null for non-numeric columns. */
  min_value?: number | null;

  /** Maximum observed value. Null for non-numeric columns. */
  max_value?: number | null;

  /**
   * Percentage of null values in the column, ranging from 0 to 100.
   * A value of 0 indicates no nulls; 100 means entirely null.
   */
  null_percentage: number;

  /** Count of distinct (unique) values in the column. */
  unique_count?: number | null;

  /**
   * A small set of representative sample values from the column.
   * Used for preview purposes in the Profile Viewer (S-006).
   * Typed as `unknown[]` to accommodate any column data type.
   */
  sample_values?: unknown[];

  /**
   * Detected regex pattern for string-typed columns (e.g., date formats,
   * phone numbers, identifiers). Null for non-string columns or when no
   * dominant pattern is detected.
   */
  pattern?: string | null;
}

// ---------------------------------------------------------------------------
// Pattern Analysis
// ---------------------------------------------------------------------------

/**
 * Describes a detected regex pattern within a string-typed column.
 *
 * The pattern analyzer scans string columns for recurring formats such
 * as date patterns, phone numbers, ZIP codes, and custom identifiers.
 * Multiple patterns may be detected per column, ranked by frequency.
 */
export interface PatternAnalysis {
  /** Regular expression that captures the detected pattern. */
  pattern_regex: string;

  /** Number of column values that match this pattern. */
  frequency: number;

  /**
   * Percentage of non-null column values that match the pattern,
   * ranging from 0 to 100.
   */
  coverage_percentage: number;

  /** A small set of example values that match the pattern. */
  sample_matches: string[];
}

// ---------------------------------------------------------------------------
// Column & Table Profile Interfaces
// ---------------------------------------------------------------------------

/**
 * Profile metadata for a single column within a profiled table.
 *
 * Contains structural metadata (name, type, nullability) plus an optional
 * statistical summary that is populated when profiling is enabled.
 */
export interface ColumnProfile {
  /** Column name as reported by the ERP schema. */
  column_name: string;

  /** Data type of the column (e.g., "varchar", "integer", "decimal"). */
  data_type: string;

  /** Whether the column allows NULL values. */
  nullable: boolean;

  /**
   * Full statistical summary for the column.
   * Null when the column has not been statistically profiled (e.g.,
   * if `include_statistics` was false in the profile request).
   */
  statistics?: ColumnStatistics | null;
}

/**
 * Profile of a single database table within an ERP schema.
 *
 * Aggregates column-level profiles and any discovered foreign-key
 * relationships that originate from this table.
 */
export interface TableProfile {
  /** Fully-qualified table name as discovered in the ERP schema. */
  table_name: string;

  /**
   * Approximate row count of the table.
   * Sourced from database catalog statistics; always >= 0.
   */
  row_count: number;

  /** Column-level profiles including optional statistics. */
  columns: ColumnProfile[];

  /**
   * Foreign-key relationships originating from columns in this table.
   * Each entry identifies the local column and the referenced
   * (target) table and column. Null when relationship discovery
   * was not included in the profile request.
   */
  relationships?: Array<{
    /** Source (foreign-key) column in this table. */
    source_column: string;
    /** Referenced parent table. */
    target_table: string;
    /** Referenced column in the parent table. */
    target_column: string;
  }> | null;
}

// ---------------------------------------------------------------------------
// Top-Level Profile Interfaces
// ---------------------------------------------------------------------------

/**
 * Complete statistical profile for an ERP data source.
 *
 * Maps to the `ProfileResponse` Pydantic model on the backend. Each profile
 * captures the result of a profiling job — structural metadata plus optional
 * statistical distributions for every profiled table and column.
 *
 * Profiles are tenant-scoped and immutable once completed. The `status` field
 * tracks the profiling lifecycle from submission to completion or failure.
 */
export interface StatisticalProfile {
  /** Unique identifier for this profile (UUID v4). */
  profile_id: string;

  /**
   * Source ERP system type.
   * One of: `'sap'`, `'oracle_ebs'`, `'dynamics'`, `'legacy'`.
   */
  erp_type: string;

  /**
   * Optional ERP module scope. When specified, the profile is limited
   * to tables belonging to that module (e.g., `'financial_accounting'`).
   * Null when the profile spans all modules.
   */
  erp_module?: string | null;

  /** Table-level profiles including column statistics and relationships. */
  tables: TableProfile[];

  /** Total number of tables included in this profile. */
  total_tables: number;

  /** Total number of columns across all profiled tables. */
  total_columns: number;

  /** ISO 8601 timestamp when the profile was initially created. */
  created_at: string;

  /** ISO 8601 timestamp of the most recent update to this profile. */
  updated_at: string;

  /**
   * Current profiling status.
   * One of: `'pending'`, `'in_progress'`, `'completed'`, `'failed'`.
   */
  status: string;

  /** Tenant namespace that owns this profile. */
  tenant_id?: string;
}

/**
 * Request payload for initiating a new profiling job.
 *
 * Maps to the `ProfileRequest` Pydantic model on the backend. The caller
 * provides connection details for the source ERP system and optional
 * scoping parameters to control which tables are profiled.
 *
 * **Security note:** Credentials (`username`, `password`) are encrypted
 * in transit (TLS 1.3) and are never persisted in the metadata store.
 */
export interface ProfileRequest {
  /**
   * Source ERP system type.
   * One of: `'sap'`, `'oracle_ebs'`, `'dynamics'`, `'legacy'`.
   */
  erp_type: string;

  /**
   * Connection protocol / method.
   * One of: `'jdbc'`, `'odata'`, `'rfc'`, `'web_api'`.
   */
  connection_type: string;

  /** Hostname or IP address of the source ERP system. */
  host: string;

  /** Port number for the connection. */
  port: number;

  /** Database / schema name. Optional for some connection types. */
  database?: string;

  /** Authentication username. */
  username: string;

  /** Authentication password. */
  password: string;

  /**
   * Optional list of specific table names to profile.
   * When omitted, all discoverable tables are profiled.
   */
  discovery_scope?: string[];

  /**
   * Optional ERP module to restrict profiling scope
   * (e.g., `'financial_accounting'`, `'hr'`).
   */
  erp_module?: string;

  /**
   * Whether to compute statistical distributions for each column.
   * Defaults to `true`.
   */
  include_statistics: boolean;

  /**
   * Whether to discover and include foreign-key relationships.
   * Defaults to `true`.
   */
  include_relationships: boolean;

  /**
   * Number of rows to sample for statistical profiling.
   * Valid range: 100 – 1,000,000. When omitted, the profiler
   * uses a service-defined default.
   */
  sample_size?: number;
}
