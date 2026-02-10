/**
 * @fileoverview TypeScript type definitions for the ERP schema representation domain.
 *
 * This module provides the canonical type system used across the entire Web Console
 * for ERP schema discovery, browsing, and generation configuration. Every interface
 * and enum mirrors its corresponding backend Pydantic model defined in
 * `src/backend/api_gateway/schemas/schema.py`, ensuring type-safe communication
 * between the frontend and the API Gateway.
 *
 * Key consumers:
 *   - Schema Browser page (Screen S-005)
 *   - Generation Wizard (Screen S-002)
 *   - schemaStore (Zustand store)
 *   - profileApi service
 *
 * Constraints enforced:
 *   - C-001: Only schema metadata is represented — no raw production data types.
 *   - C-005: Initial release limited to four ERP modules (Financial Accounting,
 *            Human Resources, Sales & Distribution, Material Management).
 *
 * @module types/schema
 */

// ---------------------------------------------------------------------------
// Enumeration Types
// ---------------------------------------------------------------------------

/**
 * Supported ERP system types for schema discovery.
 *
 * Each member corresponds to a specific ERP platform connector within the
 * Profiling Service. The string values are transmitted in JSON payloads and
 * must exactly match the backend `ERPType` StrEnum.
 *
 * - `SAP` — SAP ERP accessed via RFC/BAPI connectivity.
 * - `ORACLE_EBS` — Oracle E-Business Suite accessed via OData/JDBC.
 * - `DYNAMICS` — Microsoft Dynamics 365 accessed via Web API/OData.
 * - `LEGACY` — Generic legacy systems accessed via JDBC connectors.
 */
export enum ERPType {
  /** SAP ERP (RFC/BAPI connectivity). */
  SAP = 'sap',
  /** Oracle E-Business Suite (OData/JDBC connectivity). */
  ORACLE_EBS = 'oracle_ebs',
  /** Microsoft Dynamics 365 (Web API/OData connectivity). */
  DYNAMICS = 'dynamics',
  /** Generic legacy systems (JDBC connector). */
  LEGACY = 'legacy',
}

/**
 * ERP functional modules available for schema discovery.
 *
 * Per Constraint C-005, the initial release is limited to exactly these four
 * modules. The Generation Wizard (S-002) and Schema Browser (S-005) use this
 * enum to scope module selection and filtering.
 *
 * - `FINANCIAL_ACCOUNTING` — GL entries, invoices, payments, AP/AR.
 * - `HUMAN_RESOURCES` — Employee records, payroll, benefits, org data.
 * - `SALES_DISTRIBUTION` — Sales orders, customers, pricing, billing.
 * - `MATERIAL_MANAGEMENT` — Inventory, purchase orders, vendors, goods movements.
 */
export enum ERPModule {
  /** General Ledger, Accounts Payable/Receivable, invoices, and payments. */
  FINANCIAL_ACCOUNTING = 'financial_accounting',
  /** Employee records, payroll, benefits, and organizational data. */
  HUMAN_RESOURCES = 'hr',
  /** Sales orders, customer master data, pricing, and billing documents. */
  SALES_DISTRIBUTION = 'sales_distribution',
  /** Inventory, purchase orders, vendor master data, and goods movements. */
  MATERIAL_MANAGEMENT = 'material_management',
}

/**
 * Canonical column data types discovered in ERP database schemas.
 *
 * These values represent the normalized data types that the Profiling Service
 * maps from native RDBMS types. The Generation Engine uses this information
 * to select the correct synthesis strategy per column. The Schema Browser
 * (S-005) renders these as human-readable type labels in the column details.
 */
export enum DataType {
  /** Variable-length character string. */
  VARCHAR = 'varchar',
  /** Whole number (32-bit or 64-bit depending on source). */
  INTEGER = 'integer',
  /** Fixed-point decimal number with precision and scale. */
  DECIMAL = 'decimal',
  /** Calendar date without time component. */
  DATE = 'date',
  /** Date and time combined (without timezone). */
  DATETIME = 'datetime',
  /** True/false logical value. */
  BOOLEAN = 'boolean',
  /** Unbounded character large object. */
  TEXT = 'text',
  /** Binary large object. */
  BLOB = 'blob',
  /** Character large object. */
  CLOB = 'clob',
  /** Exact numeric with configurable precision. */
  NUMERIC = 'numeric',
  /** IEEE 754 floating-point number. */
  FLOAT = 'float',
  /** Date, time, and optional timezone information. */
  TIMESTAMP = 'timestamp',
}

// ---------------------------------------------------------------------------
// Column & Relationship Interfaces
// ---------------------------------------------------------------------------

/**
 * Definition of a single column within a discovered ERP table.
 *
 * Maps to the backend `ColumnDefinition` Pydantic model. Captures column
 * metadata extracted by the Profiling Service without accessing actual row
 * data (Constraint C-001). The Generation Engine uses this metadata to
 * configure per-column synthesis strategies.
 */
export interface TableColumn {
  /** Column name as defined in the source ERP schema. */
  readonly name: string;

  /** Canonical data type mapped from the source RDBMS type. */
  readonly data_type: DataType;

  /**
   * Maximum character length for string-type columns.
   * `null` or `undefined` for non-string types.
   */
  readonly max_length?: number | null;

  /**
   * Total number of significant digits for numeric columns.
   * `null` or `undefined` for non-numeric types.
   */
  readonly precision?: number | null;

  /**
   * Number of digits after the decimal point for numeric columns.
   * `null` or `undefined` for non-numeric types.
   */
  readonly scale?: number | null;

  /** Whether the column accepts NULL values. */
  readonly nullable: boolean;

  /** Whether this column is part of the table's primary key. */
  readonly primary_key: boolean;

  /**
   * Default value expression as defined in the source catalog.
   * `null` or `undefined` when no default is defined.
   */
  readonly default_value?: string | null;

  /**
   * Column description or catalog comment from the ERP metadata.
   * `null` or `undefined` when no description is available.
   */
  readonly description?: string | null;
}

/**
 * Definition of a foreign key relationship between two ERP tables.
 *
 * Maps to the backend `RelationshipDefinition` Pydantic model. Relationships
 * are critical for the Generation Engine's referential integrity enforcement —
 * they determine the order in which tables are generated and ensure that
 * foreign key values always reference valid primary key records.
 */
export interface Relationship {
  /** Constraint name as defined in the source schema. */
  readonly name: string;

  /** Name of the referencing (child) table containing the foreign key. */
  readonly source_table: string;

  /** Column in the source table holding the foreign key value. */
  readonly source_column: string;

  /** Name of the referenced (parent) table. */
  readonly target_table: string;

  /** Column in the target table being referenced (typically its primary key). */
  readonly target_column: string;

  /**
   * Cardinality type of the relationship.
   *
   * Allowed values: `'one_to_one'`, `'many_to_one'`, `'one_to_many'`, `'many_to_many'`.
   */
  readonly relationship_type: string;

  /**
   * Referential action on delete of the parent record.
   *
   * Allowed values: `'cascade'`, `'set_null'`, `'restrict'`, `'no_action'`.
   * `null` or `undefined` when no explicit delete rule is defined.
   */
  readonly on_delete?: string | null;
}

// ---------------------------------------------------------------------------
// Table & Schema Definition Interfaces
// ---------------------------------------------------------------------------

/**
 * Definition of a single table within a discovered ERP schema.
 *
 * Maps to the backend `TableDefinition` Pydantic model. Aggregates column
 * definitions and metadata for one table, including its assignment to an
 * ERP functional module. The Schema Browser (S-005) renders these as
 * expandable tree nodes with column detail panels.
 */
export interface TableDefinition {
  /** Table name as defined in the source ERP schema. */
  readonly name: string;

  /**
   * Database schema or namespace containing the table (e.g., `"dbo"`, `"public"`).
   * `null` or `undefined` when the table resides in the default schema.
   */
  readonly schema_name?: string | null;

  /** Ordered list of column definitions for this table. */
  readonly columns: TableColumn[];

  /** Column names that form the table's composite or single-column primary key. */
  readonly primary_keys: string[];

  /**
   * Approximate row count in the source table.
   *
   * Used for calibrating generation volume. `null` or `undefined` when
   * the count could not be determined without accessing row data (C-001).
   */
  readonly row_count?: number | null;

  /**
   * The ERP functional module this table belongs to.
   * `null` or `undefined` when module assignment is unavailable.
   */
  readonly erp_module?: ERPModule | null;

  /**
   * Table description or catalog comment from the ERP metadata.
   * `null` or `undefined` when no description is available.
   */
  readonly description?: string | null;
}

/**
 * Complete schema definition produced by ERP schema discovery.
 *
 * Maps to the backend `SchemaDefinition` Pydantic model. Returned by
 * `GET /api/v1/schemas/{id}` after a successful discovery operation.
 * Contains all tables, columns, and relationships extracted from the
 * source ERP system's metadata catalog.
 *
 * This is the primary data structure consumed by the Schema Browser (S-005)
 * for rendering the schema tree and by the Generation Wizard (S-002) for
 * table/column selection during generation configuration.
 */
export interface SchemaDefinition {
  /** Unique identifier for this schema definition (MongoDB ObjectId string). */
  readonly schema_id: string;

  /** The ERP system type that was discovered. */
  readonly erp_type: ERPType;

  /** ERP functional modules included in this discovery. */
  readonly erp_modules: ERPModule[];

  /** Discovered table definitions with column metadata. */
  readonly tables: TableDefinition[];

  /** Foreign key relationships between the discovered tables. */
  readonly relationships: Relationship[];

  /** Total number of tables discovered in the schema. */
  readonly total_tables: number;

  /** Total number of foreign key relationships discovered. */
  readonly total_relationships: number;

  /** ISO 8601 timestamp indicating when the schema discovery was performed. */
  readonly discovered_at: string;

  /**
   * Discovery status.
   *
   * Allowed values: `'pending'`, `'in_progress'`, `'completed'`, `'failed'`.
   */
  readonly status: string;

  /**
   * Tenant namespace identifier for multi-tenant isolation.
   * `undefined` when operating outside a multi-tenant context.
   */
  readonly tenant_id?: string;
}

// ---------------------------------------------------------------------------
// Connection & Discovery Request Interfaces
// ---------------------------------------------------------------------------

/**
 * Connection parameters for establishing a link to a source ERP system.
 *
 * Maps to the backend `ConnectionParams` Pydantic model. Used exclusively
 * by the Profiling Service for schema metadata extraction. Per Constraint
 * C-001, connections are used *only* for reading catalog/dictionary metadata —
 * no production data rows are accessed or stored.
 *
 * The Generation Wizard (S-002) collects these parameters in the schema
 * configuration step before submitting a discovery request.
 */
export interface ConnectionParams {
  /** Hostname or IP address of the ERP system or database server. */
  readonly host: string;

  /** Network port for the connection (1–65535). */
  readonly port: number;

  /**
   * Database or schema name to scope discovery.
   * `undefined` when the entire instance should be scanned.
   */
  readonly database?: string;

  /** Authentication username for the ERP/database connection. */
  readonly username: string;

  /** Authentication password (transmitted encrypted via TLS 1.3). */
  readonly password: string;

  /**
   * Connection protocol type.
   *
   * Allowed values: `'jdbc'`, `'odata'`, `'rfc'`, `'web_api'`.
   */
  readonly connection_type: string;

  /**
   * JDBC driver class name or connector identifier.
   * `undefined` when using a protocol that does not require a driver specification.
   */
  readonly driver?: string;

  /**
   * Driver-specific key-value connection settings.
   *
   * Examples: `{ ssl: "true", timeout: "30" }`.
   * `undefined` when no additional settings are needed.
   */
  readonly additional_params?: Record<string, string>;
}

/**
 * Request payload for initiating ERP schema discovery.
 *
 * Maps to the backend `SchemaDiscoveryRequest` Pydantic model. Submitted
 * via `POST /api/v1/schemas/discover`. The Generation Wizard (S-002)
 * constructs this payload after the user completes the connection and
 * module selection steps.
 *
 * Validation on the backend enforces:
 *   - At least one module must be specified.
 *   - Modules must fall within Constraint C-005 scope.
 *   - Table filter entries must be non-empty when provided.
 */
export interface SchemaDiscoveryRequest {
  /** The type of ERP system to discover. */
  readonly erp_type: ERPType;

  /** Connection credentials and protocol settings for the target ERP. */
  readonly connection_params: ConnectionParams;

  /**
   * Specific ERP modules to include in the discovery scope.
   * When `undefined`, the backend may default to all available modules.
   */
  readonly modules?: ERPModule[];

  /**
   * Whether to discover foreign key relationships between tables.
   * Defaults to `true` on the backend when not provided.
   */
  readonly include_relationships: boolean;

  /**
   * Whether to discover index definitions on tables.
   * Defaults to `false` on the backend when not provided.
   */
  readonly include_indexes: boolean;

  /**
   * Optional list of specific table names to limit the discovery scope.
   * When `undefined` or empty, all tables in the specified modules are discovered.
   */
  readonly table_filter?: string[];

  /**
   * Tenant namespace identifier for multi-tenant isolation.
   * `undefined` when operating outside a multi-tenant context.
   */
  readonly tenant_id?: string;
}
