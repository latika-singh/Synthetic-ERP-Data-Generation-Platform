/**
 * @fileoverview Profile and schema discovery API service for the Web Console.
 *
 * Provides fully typed functions for statistical profiling and ERP schema
 * discovery operations against the backend API Gateway. This module is consumed
 * by the schemaStore Zustand store, the ProfileViewer page (S-006), the
 * SchemaBrowser page (S-005), and the GenerationWizard page (S-002).
 *
 * All functions import the shared Axios client (`apiClient`) from `./api` and
 * return typed responses using TypeScript interfaces from `types/api.ts`,
 * `types/profile.ts`, and `types/schema.ts`.
 *
 * Endpoints follow URL-path versioning (/api/v1/) per requirement R-012.
 * The Profiling Service captures metadata only — no raw production data is
 * accessed or stored, enforcing Constraint C-001.
 *
 * @module services/profileApi
 * @version 1.0.0
 */

import { apiClient } from './api';
import type { ApiResponse, PaginatedResult, PaginationParams } from '@/types/api';
import type { StatisticalProfile, ProfileRequest, ColumnStatistics } from '@/types/profile';
import type { SchemaDefinition, SchemaDiscoveryRequest, Relationship } from '@/types/schema';

// ============================================================================
// API Base Paths — URL-path versioning per R-012
// ============================================================================

/**
 * Base URL for all statistical profile endpoints.
 * Maps to the API Gateway `/api/v1/profiles` Blueprint route group.
 */
const PROFILES_BASE = '/api/v1/profiles';

/**
 * Base URL for all schema discovery endpoints.
 * Maps to the API Gateway `/api/v1/schemas` Blueprint route group.
 */
const SCHEMAS_BASE = '/api/v1/schemas';

// ============================================================================
// Local Type Definitions — Exported for reuse by stores and components
// ============================================================================

/**
 * Filter parameters specific to statistical profile list queries.
 *
 * Used by `getProfiles()` to narrow results by ERP system type, module scope,
 * and profiling job status. Passed as URL query parameters alongside
 * {@link PaginationParams}.
 *
 * @example
 * ```typescript
 * const filters: ProfileFilterParams = {
 *   erp_type: 'sap',
 *   erp_module: 'financial_accounting',
 *   status: 'completed',
 * };
 * const profiles = await getProfiles({ page: 1, page_size: 20, ...filters });
 * ```
 */
export interface ProfileFilterParams {
  /**
   * Filter by ERP system type.
   * Accepted values: `'sap'`, `'oracle_ebs'`, `'dynamics'`, `'legacy'`.
   */
  erp_type?: string;

  /**
   * Filter by ERP functional module.
   * Accepted values: `'financial_accounting'`, `'hr'`, `'sales_distribution'`, `'material_management'`.
   */
  erp_module?: string;

  /**
   * Filter by profiling job status.
   * Accepted values: `'pending'`, `'in_progress'`, `'completed'`, `'failed'`.
   */
  status?: string;
}

/**
 * Filter parameters specific to schema definition list queries.
 *
 * Used by `getSchemas()` to narrow results by ERP system type, module scope,
 * and discovery status. Passed as URL query parameters alongside
 * {@link PaginationParams}.
 *
 * @example
 * ```typescript
 * const filters: SchemaFilterParams = {
 *   erp_type: 'oracle_ebs',
 *   status: 'completed',
 * };
 * const schemas = await getSchemas({ page: 1, page_size: 10, ...filters });
 * ```
 */
export interface SchemaFilterParams {
  /**
   * Filter by ERP system type.
   * Accepted values: `'sap'`, `'oracle_ebs'`, `'dynamics'`, `'legacy'`.
   */
  erp_type?: string;

  /**
   * Filter by ERP functional module.
   * Accepted values: `'financial_accounting'`, `'hr'`, `'sales_distribution'`, `'material_management'`.
   */
  erp_module?: string;

  /**
   * Filter by schema discovery status.
   * Accepted values: `'pending'`, `'in_progress'`, `'completed'`, `'failed'`.
   */
  status?: string;
}

/**
 * Detailed column-level statistics for a specific table within a profile.
 *
 * Returned by `getProfileStatistics()` and consumed by the ProfileViewer
 * page (S-006) to render per-column distribution charts and statistical
 * summaries. Each column entry includes the column name, data type, and
 * a nullable {@link ColumnStatistics} object containing distribution info.
 */
export interface TableProfileDetail {
  /** Fully-qualified name of the profiled table. */
  table_name: string;

  /** Approximate row count of the table from database catalog statistics. */
  row_count: number;

  /**
   * Column-level statistical details.
   *
   * Each entry provides the column name, its detected data type, and a
   * nullable `ColumnStatistics` object with distribution parameters,
   * aggregates (mean, median, std_dev), null percentage, and unique count.
   */
  columns: Array<{
    /** Column name as reported by the ERP schema. */
    column_name: string;

    /** Detected data type of the column (e.g., "varchar", "integer", "decimal"). */
    data_type: string;

    /**
     * Full statistical summary for the column.
     * Null when the column was not statistically profiled.
     */
    statistics: ColumnStatistics | null;
  }>;
}

/**
 * Foreign key relationships and dependency ordering for a discovered schema.
 *
 * Returned by `getSchemaRelationships()` and used by the SchemaBrowser
 * page (S-005) to render relationship graphs and by the GenerationWizard
 * (S-002) to determine table generation order for referential integrity.
 */
export interface SchemaRelationships {
  /** Array of all discovered foreign key relationships in the schema. */
  relationships: Relationship[];

  /** Total number of relationships discovered. */
  total: number;

  /**
   * Table names in topologically sorted dependency order.
   *
   * Tables appearing earlier in the array have no unsatisfied foreign key
   * dependencies. The Generation Engine uses this ordering to guarantee
   * that parent records exist before child records are generated.
   */
  dependency_order: string[];
}

// ============================================================================
// Statistical Profile Endpoints — /api/v1/profiles
// ============================================================================

/**
 * Initiates a new statistical profiling job against an ERP data source.
 *
 * Sends a `POST /api/v1/profiles` request containing ERP connection parameters
 * and profiling scope options. The backend Profiling Service connects to the
 * specified ERP system, extracts schema metadata (C-001: metadata only, no raw
 * production data), and computes statistical distributions for each column.
 *
 * The returned profile will have an initial `status` of `'pending'` and
 * transition to `'in_progress'` → `'completed'` (or `'failed'`) as the
 * backend profiling job progresses.
 *
 * @param request - Profiling job configuration including ERP connection details,
 *   discovery scope, module selection, and statistical options
 * @returns Promise resolving to the created `StatisticalProfile` with initial
 *   status and assigned `profile_id`
 *
 * @example
 * ```typescript
 * const profile = await createProfile({
 *   erp_type: 'sap',
 *   connection_type: 'rfc',
 *   host: 'erp.example.com',
 *   port: 3300,
 *   username: 'profiler',
 *   password: '***',
 *   erp_module: 'financial_accounting',
 *   include_statistics: true,
 *   include_relationships: true,
 *   sample_size: 10000,
 * });
 * console.log(profile.data.profile_id, profile.data.status); // 'pending'
 * ```
 */
export async function createProfile(
  request: ProfileRequest
): Promise<ApiResponse<StatisticalProfile>> {
  const response = await apiClient.post<ApiResponse<StatisticalProfile>>(
    PROFILES_BASE,
    request
  );
  return response as unknown as ApiResponse<StatisticalProfile>;
}

/**
 * Retrieves a statistical profile by its unique identifier.
 *
 * Sends a `GET /api/v1/profiles/{profileId}` request. Returns the complete
 * profile including all table-level profiles, column statistics, distributions,
 * and the current profiling status.
 *
 * Consumed by the ProfileViewer page (S-006) to render the full profile
 * detail view with distribution charts and statistical summaries.
 *
 * @param profileId - Unique identifier of the profile (UUID v4)
 * @returns Promise resolving to the full `StatisticalProfile` object
 *
 * @example
 * ```typescript
 * const profile = await getProfileById('550e8400-e29b-41d4-a716-446655440000');
 * if (profile.success) {
 *   console.log(`Profile has ${profile.data.total_tables} tables`);
 *   console.log(`Status: ${profile.data.status}`);
 * }
 * ```
 */
export async function getProfileById(
  profileId: string
): Promise<ApiResponse<StatisticalProfile>> {
  const response = await apiClient.get<ApiResponse<StatisticalProfile>>(
    `${PROFILES_BASE}/${encodeURIComponent(profileId)}`
  );
  return response as unknown as ApiResponse<StatisticalProfile>;
}

/**
 * Retrieves a paginated list of statistical profiles with optional filtering.
 *
 * Sends a `GET /api/v1/profiles` request with pagination and filter query
 * parameters. Supports filtering by ERP type, module, and profiling status.
 *
 * Used by the ProfileViewer page (S-006) for the profile list view and by
 * the GenerationWizard (S-002) for profile selection during configuration.
 *
 * @param params - Combined pagination parameters (page, page_size, sort_by,
 *   sort_direction) and profile-specific filter parameters (erp_type,
 *   erp_module, status). All fields are optional; defaults are applied
 *   server-side (page=1, page_size=20).
 * @returns Promise resolving to a paginated result set of `StatisticalProfile` items
 *
 * @example
 * ```typescript
 * const result = await getProfiles({
 *   page: 1,
 *   page_size: 20,
 *   sort_by: 'created_at',
 *   sort_direction: 'desc',
 *   erp_type: 'sap',
 *   status: 'completed',
 * });
 * console.log(`Page ${result.data.page} of ${result.data.total_pages}`);
 * result.data.items.forEach(p => console.log(p.profile_id, p.status));
 * ```
 */
export async function getProfiles(
  params?: PaginationParams & ProfileFilterParams
): Promise<ApiResponse<PaginatedResult<StatisticalProfile>>> {
  // Build query parameters, filtering out undefined values to keep the URL clean
  const queryParams: Record<string, string | number | boolean | undefined> = {};

  if (params) {
    if (params.page !== undefined) queryParams.page = params.page;
    if (params.page_size !== undefined) queryParams.page_size = params.page_size;
    if (params.sort_by !== undefined) queryParams.sort_by = params.sort_by;
    if (params.sort_direction !== undefined) queryParams.sort_direction = params.sort_direction;
    if (params.erp_type !== undefined) queryParams.erp_type = params.erp_type;
    if (params.erp_module !== undefined) queryParams.erp_module = params.erp_module;
    if (params.status !== undefined) queryParams.status = params.status;
  }

  const response = await apiClient.get<ApiResponse<PaginatedResult<StatisticalProfile>>>(
    PROFILES_BASE,
    { params: queryParams }
  );
  return response as unknown as ApiResponse<PaginatedResult<StatisticalProfile>>;
}

/**
 * Deletes a statistical profile by its unique identifier.
 *
 * Sends a `DELETE /api/v1/profiles/{profileId}` request. Permanently removes
 * the profile and its associated statistical data from the metadata repository.
 * This operation cannot be undone.
 *
 * @param profileId - Unique identifier of the profile to delete (UUID v4)
 * @returns Promise resolving to a void success response
 *
 * @example
 * ```typescript
 * const result = await deleteProfile('550e8400-e29b-41d4-a716-446655440000');
 * if (result.success) {
 *   console.log('Profile deleted successfully');
 * }
 * ```
 */
export async function deleteProfile(
  profileId: string
): Promise<ApiResponse<void>> {
  const response = await apiClient.delete<ApiResponse<void>>(
    `${PROFILES_BASE}/${encodeURIComponent(profileId)}`
  );
  return response as unknown as ApiResponse<void>;
}

/**
 * Retrieves detailed column-level statistics for a specific table within a profile.
 *
 * Sends a `GET /api/v1/profiles/{profileId}/tables/{tableName}` request.
 * Returns granular per-column distribution analysis including distribution type,
 * fitted parameters, aggregate statistics (mean, median, std_dev, min, max),
 * null percentage, unique count, and sample values.
 *
 * Consumed by the ProfileViewer page (S-006) to render the table detail panel
 * with per-column distribution charts via the DistributionChart component.
 *
 * @param profileId - Unique identifier of the parent profile (UUID v4)
 * @param tableName - Name of the table to retrieve statistics for
 * @returns Promise resolving to a `TableProfileDetail` with column-level statistics
 *
 * @example
 * ```typescript
 * const detail = await getProfileStatistics(
 *   '550e8400-e29b-41d4-a716-446655440000',
 *   'GL_JOURNAL_ENTRIES'
 * );
 * detail.data.columns.forEach(col => {
 *   if (col.statistics) {
 *     console.log(`${col.column_name}: ${col.statistics.distribution.type}`);
 *   }
 * });
 * ```
 */
export async function getProfileStatistics(
  profileId: string,
  tableName: string
): Promise<ApiResponse<TableProfileDetail>> {
  const response = await apiClient.get<ApiResponse<TableProfileDetail>>(
    `${PROFILES_BASE}/${encodeURIComponent(profileId)}/tables/${encodeURIComponent(tableName)}`
  );
  return response as unknown as ApiResponse<TableProfileDetail>;
}

// ============================================================================
// Schema Discovery Endpoints — /api/v1/schemas
// ============================================================================

/**
 * Initiates an ERP schema discovery operation against a source system.
 *
 * Sends a `POST /api/v1/schemas/discover` request containing ERP connection
 * parameters, target modules, and discovery options. The backend Profiling
 * Service connects to the specified ERP system and extracts table/column
 * metadata, foreign key relationships, and index definitions.
 *
 * Per Constraint C-001, only schema metadata is extracted — no production
 * data rows are accessed or stored. Supports native handling of SAP, Oracle
 * EBS, Microsoft Dynamics, and legacy system schemas.
 *
 * The returned schema will have an initial `status` of `'pending'` and
 * transition through `'in_progress'` → `'completed'` (or `'failed'`).
 *
 * @param request - Schema discovery configuration including ERP type,
 *   connection parameters, modules to discover, relationship and index flags,
 *   and optional table name filter
 * @returns Promise resolving to the created `SchemaDefinition` with initial
 *   discovery status and assigned `schema_id`
 *
 * @example
 * ```typescript
 * const schema = await discoverSchema({
 *   erp_type: ERPType.SAP,
 *   connection_params: {
 *     host: 'erp.example.com',
 *     port: 3300,
 *     username: 'discoverer',
 *     password: '***',
 *     connection_type: 'rfc',
 *   },
 *   modules: [ERPModule.FINANCIAL_ACCOUNTING, ERPModule.HUMAN_RESOURCES],
 *   include_relationships: true,
 *   include_indexes: false,
 * });
 * console.log(schema.data.schema_id, schema.data.status); // 'pending'
 * ```
 */
export async function discoverSchema(
  request: SchemaDiscoveryRequest
): Promise<ApiResponse<SchemaDefinition>> {
  const response = await apiClient.post<ApiResponse<SchemaDefinition>>(
    `${SCHEMAS_BASE}/discover`,
    request
  );
  return response as unknown as ApiResponse<SchemaDefinition>;
}

/**
 * Retrieves a schema definition by its unique identifier.
 *
 * Sends a `GET /api/v1/schemas/{schemaId}` request. Returns the complete
 * schema definition including all discovered tables, columns, relationships,
 * and the current discovery status.
 *
 * Consumed by the SchemaBrowser page (S-005) to render the schema tree with
 * expandable table nodes and column detail panels.
 *
 * @param schemaId - Unique identifier of the schema definition (MongoDB ObjectId string)
 * @returns Promise resolving to the full `SchemaDefinition` object
 *
 * @example
 * ```typescript
 * const schema = await getSchemaById('64a1b2c3d4e5f6a7b8c9d0e1');
 * if (schema.success) {
 *   console.log(`Discovered ${schema.data.total_tables} tables`);
 *   schema.data.tables.forEach(t => console.log(t.name, t.columns.length));
 * }
 * ```
 */
export async function getSchemaById(
  schemaId: string
): Promise<ApiResponse<SchemaDefinition>> {
  const response = await apiClient.get<ApiResponse<SchemaDefinition>>(
    `${SCHEMAS_BASE}/${encodeURIComponent(schemaId)}`
  );
  return response as unknown as ApiResponse<SchemaDefinition>;
}

/**
 * Retrieves a paginated list of schema definitions with optional filtering.
 *
 * Sends a `GET /api/v1/schemas` request with pagination and filter query
 * parameters. Supports filtering by ERP type, module, and discovery status.
 *
 * Used by the SchemaBrowser page (S-005) for the schema list view and by
 * the GenerationWizard (S-002) for schema selection during configuration.
 *
 * @param params - Combined pagination parameters (page, page_size, sort_by,
 *   sort_direction) and schema-specific filter parameters (erp_type,
 *   erp_module, status). All fields are optional; defaults are applied
 *   server-side (page=1, page_size=20).
 * @returns Promise resolving to a paginated result set of `SchemaDefinition` items
 *
 * @example
 * ```typescript
 * const result = await getSchemas({
 *   page: 1,
 *   page_size: 10,
 *   erp_type: 'oracle_ebs',
 *   status: 'completed',
 * });
 * console.log(`Found ${result.data.total} schemas`);
 * ```
 */
export async function getSchemas(
  params?: PaginationParams & SchemaFilterParams
): Promise<ApiResponse<PaginatedResult<SchemaDefinition>>> {
  // Build query parameters, filtering out undefined values to keep the URL clean
  const queryParams: Record<string, string | number | boolean | undefined> = {};

  if (params) {
    if (params.page !== undefined) queryParams.page = params.page;
    if (params.page_size !== undefined) queryParams.page_size = params.page_size;
    if (params.sort_by !== undefined) queryParams.sort_by = params.sort_by;
    if (params.sort_direction !== undefined) queryParams.sort_direction = params.sort_direction;
    if (params.erp_type !== undefined) queryParams.erp_type = params.erp_type;
    if (params.erp_module !== undefined) queryParams.erp_module = params.erp_module;
    if (params.status !== undefined) queryParams.status = params.status;
  }

  const response = await apiClient.get<ApiResponse<PaginatedResult<SchemaDefinition>>>(
    SCHEMAS_BASE,
    { params: queryParams }
  );
  return response as unknown as ApiResponse<PaginatedResult<SchemaDefinition>>;
}

/**
 * Retrieves all foreign key relationships and the table dependency ordering
 * for a specific schema.
 *
 * Sends a `GET /api/v1/schemas/{schemaId}/relationships` request. Returns
 * a comprehensive relationship map including all FK constraints and a
 * topologically sorted dependency order for generation sequencing.
 *
 * Used by the SchemaBrowser page (S-005) to visualize relationship graphs
 * and by the GenerationWizard (S-002) to determine safe generation order
 * that preserves referential integrity.
 *
 * @param schemaId - Unique identifier of the schema definition (MongoDB ObjectId string)
 * @returns Promise resolving to a `SchemaRelationships` object containing
 *   all relationships, their count, and the dependency-ordered table list
 *
 * @example
 * ```typescript
 * const rels = await getSchemaRelationships('64a1b2c3d4e5f6a7b8c9d0e1');
 * console.log(`${rels.data.total} relationships discovered`);
 * console.log('Generation order:', rels.data.dependency_order.join(' → '));
 * rels.data.relationships.forEach(r =>
 *   console.log(`${r.source_table}.${r.source_column} → ${r.target_table}.${r.target_column}`)
 * );
 * ```
 */
export async function getSchemaRelationships(
  schemaId: string
): Promise<ApiResponse<SchemaRelationships>> {
  const response = await apiClient.get<ApiResponse<SchemaRelationships>>(
    `${SCHEMAS_BASE}/${encodeURIComponent(schemaId)}/relationships`
  );
  return response as unknown as ApiResponse<SchemaRelationships>;
}

/**
 * Deletes a schema definition by its unique identifier.
 *
 * Sends a `DELETE /api/v1/schemas/{schemaId}` request. Permanently removes
 * the schema definition and its associated table/column/relationship metadata
 * from the metadata repository. This operation cannot be undone.
 *
 * @param schemaId - Unique identifier of the schema definition to delete
 *   (MongoDB ObjectId string)
 * @returns Promise resolving to a void success response
 *
 * @example
 * ```typescript
 * const result = await deleteSchema('64a1b2c3d4e5f6a7b8c9d0e1');
 * if (result.success) {
 *   console.log('Schema definition deleted');
 * }
 * ```
 */
export async function deleteSchema(
  schemaId: string
): Promise<ApiResponse<void>> {
  const response = await apiClient.delete<ApiResponse<void>>(
    `${SCHEMAS_BASE}/${encodeURIComponent(schemaId)}`
  );
  return response as unknown as ApiResponse<void>;
}

/**
 * Re-discovers a schema from its original ERP source, updating the existing
 * schema definition in-place.
 *
 * Sends a `POST /api/v1/schemas/{schemaId}/refresh` request. The backend
 * uses the stored connection parameters to re-connect to the ERP system and
 * perform a fresh schema extraction. Any new tables, columns, or relationships
 * are added; removed items are flagged or pruned depending on server policy.
 *
 * Useful when the source ERP schema has been modified (e.g., after a system
 * upgrade or customization) and the local schema definition needs to reflect
 * the current state.
 *
 * @param schemaId - Unique identifier of the schema definition to refresh
 *   (MongoDB ObjectId string)
 * @returns Promise resolving to the updated `SchemaDefinition` with refreshed
 *   tables, columns, and relationships
 *
 * @example
 * ```typescript
 * const refreshed = await refreshSchema('64a1b2c3d4e5f6a7b8c9d0e1');
 * console.log(`Refreshed: ${refreshed.data.total_tables} tables found`);
 * console.log(`Status: ${refreshed.data.status}`);
 * ```
 */
export async function refreshSchema(
  schemaId: string
): Promise<ApiResponse<SchemaDefinition>> {
  const response = await apiClient.post<ApiResponse<SchemaDefinition>>(
    `${SCHEMAS_BASE}/${encodeURIComponent(schemaId)}/refresh`
  );
  return response as unknown as ApiResponse<SchemaDefinition>;
}
