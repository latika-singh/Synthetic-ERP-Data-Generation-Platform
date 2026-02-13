/**
 * @fileoverview Comprehensive Vitest unit tests for the profileApi service module.
 *
 * Tests all 11 profile and schema discovery API functions exported from
 * `src/web/src/services/profileApi.ts`, verifying correct HTTP method usage,
 * URL construction (including path parameter interpolation and encoding),
 * request payloads, query parameter serialization, and typed response handling.
 *
 * The base Axios HTTP client (`apiClient`) is mocked at the module level via
 * `vi.mock('@/services/api')`, replacing `.get`, `.post`, and `.delete` with
 * Vitest mock functions. This enables isolated testing of the API service layer
 * without making real HTTP requests.
 *
 * Coverage includes:
 *   - Profile endpoints (/api/v1/profiles/*):
 *     createProfile, getProfiles, getProfileById, deleteProfile, getProfileStatistics
 *   - Schema endpoints (/api/v1/schemas/*):
 *     discoverSchema, getSchemas, getSchemaById, getSchemaRelationships, deleteSchema, refreshSchema
 *   - Error handling for connection errors (502), timeout errors (504), and not-found errors (404)
 *
 * @module tests/unit/web/services/profileApi.test
 * @see src/web/src/services/profileApi.ts — Module under test
 * @see tests/unit/web/setup.ts — Global test setup (implicitly loaded)
 */

import { describe, it, expect, vi, beforeEach } from 'vitest';

import {
  createProfile,
  getProfiles,
  getProfileById,
  deleteProfile,
  getProfileStatistics,
  discoverSchema,
  getSchemas,
  getSchemaById,
  getSchemaRelationships,
  deleteSchema,
  refreshSchema,
} from '@/services/profileApi';

import type { TableProfileDetail, SchemaRelationships } from '@/services/profileApi';
import type { ApiResponse, PaginatedResult } from '@/types/api';
import type { StatisticalProfile, ProfileRequest, ColumnStatistics } from '@/types/profile';
import { DistributionType } from '@/types/profile';
import type { SchemaDefinition, SchemaDiscoveryRequest, Relationship } from '@/types/schema';
import { ERPType, ERPModule, DataType } from '@/types/schema';

import { apiClient } from '@/services/api';

// ============================================================================
// Module-Level Mock — Replace apiClient with controlled mock functions
// ============================================================================

vi.mock('@/services/api', () => ({
  apiClient: {
    get: vi.fn(),
    post: vi.fn(),
    delete: vi.fn(),
  },
}));

// ============================================================================
// Test Data Factories — Reusable typed mock fixtures
// ============================================================================

/**
 * Factory for a complete ProfileRequest fixture with ERP connection parameters.
 * Matches the ProfileRequest interface from types/profile.ts.
 */
const mockProfileRequest: ProfileRequest = {
  erp_type: 'sap',
  connection_type: 'jdbc',
  host: 'erp.example.com',
  port: 3306,
  database: 'SAPDB',
  username: 'profiler',
  password: 'pass123',
  include_statistics: true,
  include_relationships: true,
  sample_size: 10000,
};

/**
 * Factory for a complete StatisticalProfile response fixture.
 * Represents a completed profiling job with tables, column statistics,
 * and distribution information.
 */
const mockStatisticalProfile: StatisticalProfile = {
  profile_id: 'profile-123',
  erp_type: 'sap',
  erp_module: 'financial_accounting',
  tables: [
    {
      table_name: 'GL_ENTRIES',
      row_count: 150000,
      columns: [
        {
          column_name: 'ENTRY_ID',
          data_type: 'integer',
          nullable: false,
          statistics: {
            column_name: 'ENTRY_ID',
            data_type: 'integer',
            distribution: {
              type: DistributionType.UNIFORM,
              parameters: { low: 1, high: 150000 },
              fit_score: 0.97,
            },
            mean: 75000,
            median: 75001,
            std_dev: 43301.27,
            min_value: 1,
            max_value: 150000,
            null_percentage: 0,
            unique_count: 150000,
          },
        },
        {
          column_name: 'AMOUNT',
          data_type: 'decimal',
          nullable: false,
          statistics: {
            column_name: 'AMOUNT',
            data_type: 'decimal',
            distribution: {
              type: DistributionType.LOG_NORMAL,
              parameters: { mu: 7.5, sigma: 1.2 },
              fit_score: 0.93,
            },
            mean: 2500.5,
            median: 1800.0,
            std_dev: 3200.42,
            min_value: 0.01,
            max_value: 99999.99,
            null_percentage: 0,
            unique_count: 145000,
          },
        },
      ],
    },
  ],
  total_tables: 1,
  total_columns: 2,
  created_at: '2025-01-15T10:30:00Z',
  updated_at: '2025-01-15T10:45:00Z',
  status: 'completed',
  tenant_id: 'tenant-1',
};

/**
 * Factory for a StatisticalProfile in pending status (newly created).
 */
const mockPendingProfile: StatisticalProfile = {
  profile_id: 'profile-456',
  erp_type: 'sap',
  erp_module: null,
  tables: [],
  total_tables: 0,
  total_columns: 0,
  created_at: '2025-01-20T08:00:00Z',
  updated_at: '2025-01-20T08:00:00Z',
  status: 'pending',
  tenant_id: 'tenant-1',
};

/**
 * Factory for a complete SchemaDiscoveryRequest fixture.
 * Matches the SchemaDiscoveryRequest interface from types/schema.ts.
 */
const mockSchemaDiscoveryRequest: SchemaDiscoveryRequest = {
  erp_type: ERPType.SAP,
  connection_params: {
    host: 'erp.example.com',
    port: 3306,
    username: 'discover',
    password: 'pass',
    connection_type: 'jdbc',
  },
  modules: [ERPModule.FINANCIAL_ACCOUNTING, ERPModule.HUMAN_RESOURCES],
  include_relationships: true,
  include_indexes: false,
};

/**
 * Factory for a complete SchemaDefinition response fixture.
 * Includes tables with columns, primary keys, and relationships.
 */
const mockSchemaDefinition: SchemaDefinition = {
  schema_id: 'schema-123',
  erp_type: ERPType.SAP,
  erp_modules: [ERPModule.FINANCIAL_ACCOUNTING, ERPModule.HUMAN_RESOURCES],
  tables: [
    {
      name: 'GL_ENTRIES',
      schema_name: 'public',
      columns: [
        {
          name: 'ENTRY_ID',
          data_type: DataType.INTEGER,
          nullable: false,
          primary_key: true,
          max_length: null,
          precision: null,
          scale: null,
        },
        {
          name: 'ACCOUNT_ID',
          data_type: DataType.INTEGER,
          nullable: false,
          primary_key: false,
          max_length: null,
          precision: null,
          scale: null,
        },
        {
          name: 'AMOUNT',
          data_type: DataType.DECIMAL,
          nullable: false,
          primary_key: false,
          max_length: null,
          precision: 18,
          scale: 2,
        },
        {
          name: 'POSTING_DATE',
          data_type: DataType.DATE,
          nullable: false,
          primary_key: false,
          max_length: null,
          precision: null,
          scale: null,
        },
      ],
      primary_keys: ['ENTRY_ID'],
      row_count: 150000,
      erp_module: ERPModule.FINANCIAL_ACCOUNTING,
    },
    {
      name: 'GL_ACCOUNTS',
      schema_name: 'public',
      columns: [
        {
          name: 'ACCOUNT_ID',
          data_type: DataType.INTEGER,
          nullable: false,
          primary_key: true,
          max_length: null,
          precision: null,
          scale: null,
        },
        {
          name: 'ACCOUNT_NAME',
          data_type: DataType.VARCHAR,
          nullable: false,
          primary_key: false,
          max_length: 255,
          precision: null,
          scale: null,
        },
      ],
      primary_keys: ['ACCOUNT_ID'],
      row_count: 500,
      erp_module: ERPModule.FINANCIAL_ACCOUNTING,
    },
  ],
  relationships: [
    {
      name: 'FK_GL_ENTRIES_ACCOUNT',
      source_table: 'GL_ENTRIES',
      source_column: 'ACCOUNT_ID',
      target_table: 'GL_ACCOUNTS',
      target_column: 'ACCOUNT_ID',
      relationship_type: 'many_to_one',
    },
  ],
  total_tables: 2,
  total_relationships: 1,
  discovered_at: '2025-01-15T09:00:00Z',
  status: 'completed',
  tenant_id: 'tenant-1',
};

/**
 * Factory for a SchemaRelationships response fixture.
 * Includes FK relationships and topologically sorted dependency ordering.
 */
const mockSchemaRelationshipsData: SchemaRelationships = {
  relationships: [
    {
      name: 'FK_GL_ENTRIES_ACCOUNT',
      source_table: 'GL_ENTRIES',
      source_column: 'ACCOUNT_ID',
      target_table: 'GL_ACCOUNTS',
      target_column: 'ACCOUNT_ID',
      relationship_type: 'many_to_one',
    },
    {
      name: 'FK_INVOICES_CUSTOMER',
      source_table: 'INVOICES',
      source_column: 'CUSTOMER_ID',
      target_table: 'CUSTOMERS',
      target_column: 'CUSTOMER_ID',
      relationship_type: 'many_to_one',
    },
  ],
  total: 2,
  dependency_order: ['GL_ACCOUNTS', 'CUSTOMERS', 'GL_ENTRIES', 'INVOICES'],
};

/**
 * Factory for a TableProfileDetail response fixture.
 * Contains column-level statistics for a specific table.
 */
const mockTableProfileDetail: TableProfileDetail = {
  table_name: 'GL_ENTRIES',
  row_count: 150000,
  columns: [
    {
      column_name: 'ENTRY_ID',
      data_type: 'integer',
      statistics: {
        column_name: 'ENTRY_ID',
        data_type: 'integer',
        distribution: {
          type: DistributionType.UNIFORM,
          parameters: { low: 1, high: 150000 },
          fit_score: 0.97,
        },
        mean: 75000,
        median: 75001,
        std_dev: 43301.27,
        null_percentage: 0,
        unique_count: 150000,
      },
    },
    {
      column_name: 'AMOUNT',
      data_type: 'decimal',
      statistics: {
        column_name: 'AMOUNT',
        data_type: 'decimal',
        distribution: {
          type: DistributionType.LOG_NORMAL,
          parameters: { mu: 7.5, sigma: 1.2 },
          fit_score: 0.93,
        },
        mean: 2500.5,
        median: 1800.0,
        std_dev: 3200.42,
        null_percentage: 0,
        unique_count: 145000,
      },
    },
    {
      column_name: 'DESCRIPTION',
      data_type: 'varchar',
      statistics: null,
    },
  ],
};

// ============================================================================
// Helper — Wrap payload in standard ApiResponse envelope
// ============================================================================

/**
 * Creates a typed ApiResponse envelope wrapping the provided data payload.
 * Mirrors the response envelope shape returned by the API Gateway.
 */
function wrapResponse<T>(data: T, message?: string): ApiResponse<T> {
  return {
    success: true,
    data,
    message: message ?? null,
    timestamp: '2025-01-20T12:00:00Z',
  };
}

// ============================================================================
// Test Suite — Profile and Schema API Service
// ============================================================================

describe('profileApi', () => {
  // Reset all mock call history before each test for isolation
  beforeEach(() => {
    vi.clearAllMocks();
  });

  // ==========================================================================
  // Profile Endpoints — /api/v1/profiles
  // ==========================================================================

  describe('createProfile', () => {
    it('should call POST /api/v1/profiles with profile request', async () => {
      const mockResponse = wrapResponse(mockPendingProfile);
      vi.mocked(apiClient.post).mockResolvedValue(mockResponse);

      await createProfile(mockProfileRequest);

      expect(apiClient.post).toHaveBeenCalledTimes(1);
      expect(apiClient.post).toHaveBeenCalledWith(
        '/api/v1/profiles',
        mockProfileRequest
      );
    });

    it('should send correct ERP connection parameters in request body', async () => {
      const mockResponse = wrapResponse(mockPendingProfile);
      vi.mocked(apiClient.post).mockResolvedValue(mockResponse);

      await createProfile(mockProfileRequest);

      const callArgs = vi.mocked(apiClient.post).mock.calls[0];
      const requestBody = callArgs[1] as ProfileRequest;
      expect(requestBody.erp_type).toBe('sap');
      expect(requestBody.connection_type).toBe('jdbc');
      expect(requestBody.host).toBe('erp.example.com');
      expect(requestBody.port).toBe(3306);
      expect(requestBody.database).toBe('SAPDB');
      expect(requestBody.username).toBe('profiler');
      expect(requestBody.password).toBe('pass123');
      expect(requestBody.include_statistics).toBe(true);
      expect(requestBody.include_relationships).toBe(true);
      expect(requestBody.sample_size).toBe(10000);
    });

    it('should return profile with pending status', async () => {
      const mockResponse = wrapResponse(mockPendingProfile);
      vi.mocked(apiClient.post).mockResolvedValue(mockResponse);

      const result = await createProfile(mockProfileRequest);

      expect(result.success).toBe(true);
      expect(result.data.profile_id).toBe('profile-456');
      expect(result.data.status).toBe('pending');
      expect(result.data.erp_type).toBe('sap');
      expect(result.data.tables).toEqual([]);
      expect(result.data.total_tables).toBe(0);
    });

    it('should handle connection errors', async () => {
      const connectionError = {
        response: {
          status: 502,
          data: {
            status_code: 502,
            error_code: 'BAD_GATEWAY',
            message: 'Failed to connect to ERP system',
            timestamp: '2025-01-20T12:00:00Z',
          },
        },
      };
      vi.mocked(apiClient.post).mockRejectedValue(connectionError);

      await expect(createProfile(mockProfileRequest)).rejects.toEqual(connectionError);
      expect(apiClient.post).toHaveBeenCalledTimes(1);
    });
  });

  describe('getProfiles', () => {
    it('should call GET /api/v1/profiles with default params', async () => {
      const mockResponse = wrapResponse<PaginatedResult<StatisticalProfile>>({
        items: [mockStatisticalProfile],
        total: 1,
        page: 1,
        page_size: 20,
        total_pages: 1,
      });
      vi.mocked(apiClient.get).mockResolvedValue(mockResponse);

      await getProfiles();

      expect(apiClient.get).toHaveBeenCalledTimes(1);
      expect(apiClient.get).toHaveBeenCalledWith(
        '/api/v1/profiles',
        { params: {} }
      );
    });

    it('should pass pagination and filter params', async () => {
      const mockResponse = wrapResponse<PaginatedResult<StatisticalProfile>>({
        items: [mockStatisticalProfile],
        total: 1,
        page: 1,
        page_size: 20,
        total_pages: 1,
      });
      vi.mocked(apiClient.get).mockResolvedValue(mockResponse);

      await getProfiles({
        page: 1,
        page_size: 20,
        erp_type: 'oracle_ebs',
        erp_module: 'financial_accounting',
        status: 'completed',
      });

      expect(apiClient.get).toHaveBeenCalledWith(
        '/api/v1/profiles',
        {
          params: {
            page: 1,
            page_size: 20,
            erp_type: 'oracle_ebs',
            erp_module: 'financial_accounting',
            status: 'completed',
          },
        }
      );
    });

    it('should return paginated profile list', async () => {
      const paginatedData: PaginatedResult<StatisticalProfile> = {
        items: [mockStatisticalProfile],
        total: 50,
        page: 1,
        page_size: 20,
        total_pages: 3,
      };
      const mockResponse = wrapResponse(paginatedData);
      vi.mocked(apiClient.get).mockResolvedValue(mockResponse);

      const result = await getProfiles({ page: 1, page_size: 20 });

      expect(result.success).toBe(true);
      expect(result.data.items).toHaveLength(1);
      expect(result.data.items[0].profile_id).toBe('profile-123');
      expect(result.data.total).toBe(50);
      expect(result.data.page).toBe(1);
      expect(result.data.page_size).toBe(20);
      expect(result.data.total_pages).toBe(3);
    });

    it('should omit undefined filter params from query', async () => {
      const mockResponse = wrapResponse<PaginatedResult<StatisticalProfile>>({
        items: [],
        total: 0,
        page: 1,
        page_size: 20,
        total_pages: 0,
      });
      vi.mocked(apiClient.get).mockResolvedValue(mockResponse);

      await getProfiles({ page: 1, page_size: 20 });

      const callArgs = vi.mocked(apiClient.get).mock.calls[0];
      const queryParams = (callArgs[1] as { params: Record<string, unknown> }).params;
      expect(queryParams).toEqual({ page: 1, page_size: 20 });
      expect(queryParams.erp_type).toBeUndefined();
      expect(queryParams.erp_module).toBeUndefined();
      expect(queryParams.status).toBeUndefined();
    });
  });

  describe('getProfileById', () => {
    it('should call GET /api/v1/profiles/{profileId}', async () => {
      const mockResponse = wrapResponse(mockStatisticalProfile);
      vi.mocked(apiClient.get).mockResolvedValue(mockResponse);

      await getProfileById('profile-123');

      expect(apiClient.get).toHaveBeenCalledTimes(1);
      expect(apiClient.get).toHaveBeenCalledWith('/api/v1/profiles/profile-123');
    });

    it('should return full statistical profile', async () => {
      const mockResponse = wrapResponse(mockStatisticalProfile);
      vi.mocked(apiClient.get).mockResolvedValue(mockResponse);

      const result = await getProfileById('profile-123');

      expect(result.success).toBe(true);
      expect(result.data.profile_id).toBe('profile-123');
      expect(result.data.erp_type).toBe('sap');
      expect(result.data.status).toBe('completed');
      expect(result.data.tables).toHaveLength(1);
      expect(result.data.tables[0].table_name).toBe('GL_ENTRIES');
      expect(result.data.tables[0].row_count).toBe(150000);
      expect(result.data.tables[0].columns).toHaveLength(2);

      // Verify column-level statistics and distributions
      const entryIdCol = result.data.tables[0].columns[0];
      expect(entryIdCol.column_name).toBe('ENTRY_ID');
      expect(entryIdCol.statistics).toBeDefined();
      expect(entryIdCol.statistics!.distribution.type).toBe('uniform');
      expect(entryIdCol.statistics!.mean).toBe(75000);

      const amountCol = result.data.tables[0].columns[1];
      expect(amountCol.column_name).toBe('AMOUNT');
      expect(amountCol.statistics).toBeDefined();
      expect(amountCol.statistics!.distribution.type).toBe('log_normal');
      expect(amountCol.statistics!.distribution.parameters).toEqual({ mu: 7.5, sigma: 1.2 });
    });

    it('should encode special characters in profileId', async () => {
      const mockResponse = wrapResponse(mockStatisticalProfile);
      vi.mocked(apiClient.get).mockResolvedValue(mockResponse);

      await getProfileById('profile/special&id');

      expect(apiClient.get).toHaveBeenCalledWith(
        `/api/v1/profiles/${encodeURIComponent('profile/special&id')}`
      );
    });
  });

  describe('deleteProfile', () => {
    it('should call DELETE /api/v1/profiles/{profileId}', async () => {
      const mockResponse = wrapResponse<void>(undefined as unknown as void, 'Profile deleted');
      vi.mocked(apiClient.delete).mockResolvedValue(mockResponse);

      await deleteProfile('profile-123');

      expect(apiClient.delete).toHaveBeenCalledTimes(1);
      expect(apiClient.delete).toHaveBeenCalledWith('/api/v1/profiles/profile-123');
    });

    it('should handle successful deletion', async () => {
      const mockResponse: ApiResponse<void> = {
        success: true,
        data: undefined as unknown as void,
        message: 'Profile deleted successfully',
        timestamp: '2025-01-20T12:00:00Z',
      };
      vi.mocked(apiClient.delete).mockResolvedValue(mockResponse);

      const result = await deleteProfile('profile-123');

      expect(result.success).toBe(true);
      expect(result.message).toBe('Profile deleted successfully');
    });

    it('should propagate errors on failed deletion', async () => {
      const notFoundError = {
        response: {
          status: 404,
          data: {
            status_code: 404,
            error_code: 'RESOURCE_NOT_FOUND',
            message: 'Profile not found',
            timestamp: '2025-01-20T12:00:00Z',
          },
        },
      };
      vi.mocked(apiClient.delete).mockRejectedValue(notFoundError);

      await expect(deleteProfile('nonexistent-profile')).rejects.toEqual(notFoundError);
    });
  });

  describe('getProfileStatistics', () => {
    it('should call GET /api/v1/profiles/{profileId}/tables/{tableName}', async () => {
      const mockResponse = wrapResponse(mockTableProfileDetail);
      vi.mocked(apiClient.get).mockResolvedValue(mockResponse);

      await getProfileStatistics('profile-123', 'GL_ENTRIES');

      expect(apiClient.get).toHaveBeenCalledTimes(1);
      expect(apiClient.get).toHaveBeenCalledWith(
        '/api/v1/profiles/profile-123/tables/GL_ENTRIES'
      );
    });

    it('should return table-level column statistics', async () => {
      const mockResponse = wrapResponse(mockTableProfileDetail);
      vi.mocked(apiClient.get).mockResolvedValue(mockResponse);

      const result = await getProfileStatistics('profile-123', 'GL_ENTRIES');

      expect(result.success).toBe(true);
      expect(result.data.table_name).toBe('GL_ENTRIES');
      expect(result.data.row_count).toBe(150000);
      expect(result.data.columns).toHaveLength(3);

      // Verify column with statistics
      const entryIdCol = result.data.columns[0];
      expect(entryIdCol.column_name).toBe('ENTRY_ID');
      expect(entryIdCol.data_type).toBe('integer');
      expect(entryIdCol.statistics).not.toBeNull();
      expect(entryIdCol.statistics!.distribution.type).toBe('uniform');
      expect(entryIdCol.statistics!.mean).toBe(75000);
      expect(entryIdCol.statistics!.median).toBe(75001);
      expect(entryIdCol.statistics!.std_dev).toBe(43301.27);
      expect(entryIdCol.statistics!.null_percentage).toBe(0);
      expect(entryIdCol.statistics!.unique_count).toBe(150000);

      // Verify column with null statistics
      const descCol = result.data.columns[2];
      expect(descCol.column_name).toBe('DESCRIPTION');
      expect(descCol.data_type).toBe('varchar');
      expect(descCol.statistics).toBeNull();
    });

    it('should encode table names with special characters', async () => {
      const mockResponse = wrapResponse(mockTableProfileDetail);
      vi.mocked(apiClient.get).mockResolvedValue(mockResponse);

      await getProfileStatistics('profile-123', 'GL ENTRIES/2025');

      expect(apiClient.get).toHaveBeenCalledWith(
        `/api/v1/profiles/profile-123/tables/${encodeURIComponent('GL ENTRIES/2025')}`
      );
    });
  });

  // ==========================================================================
  // Schema Discovery Endpoints — /api/v1/schemas
  // ==========================================================================

  describe('discoverSchema', () => {
    it('should call POST /api/v1/schemas/discover with discovery request', async () => {
      const pendingSchema: SchemaDefinition = {
        ...mockSchemaDefinition,
        schema_id: 'schema-new',
        tables: [],
        relationships: [],
        total_tables: 0,
        total_relationships: 0,
        status: 'pending',
      };
      const mockResponse = wrapResponse(pendingSchema);
      vi.mocked(apiClient.post).mockResolvedValue(mockResponse);

      await discoverSchema(mockSchemaDiscoveryRequest);

      expect(apiClient.post).toHaveBeenCalledTimes(1);
      expect(apiClient.post).toHaveBeenCalledWith(
        '/api/v1/schemas/discover',
        mockSchemaDiscoveryRequest
      );
    });

    it('should send correct connection params and module selection', async () => {
      const pendingSchema: SchemaDefinition = {
        ...mockSchemaDefinition,
        status: 'pending',
      };
      const mockResponse = wrapResponse(pendingSchema);
      vi.mocked(apiClient.post).mockResolvedValue(mockResponse);

      await discoverSchema(mockSchemaDiscoveryRequest);

      const callArgs = vi.mocked(apiClient.post).mock.calls[0];
      const requestBody = callArgs[1] as SchemaDiscoveryRequest;
      expect(requestBody.erp_type).toBe('sap');
      expect(requestBody.connection_params.host).toBe('erp.example.com');
      expect(requestBody.connection_params.port).toBe(3306);
      expect(requestBody.connection_params.username).toBe('discover');
      expect(requestBody.connection_params.password).toBe('pass');
      expect(requestBody.connection_params.connection_type).toBe('jdbc');
      expect(requestBody.modules).toEqual(['financial_accounting', 'hr']);
      expect(requestBody.include_relationships).toBe(true);
      expect(requestBody.include_indexes).toBe(false);
    });

    it('should return schema definition with initial status', async () => {
      const pendingSchema: SchemaDefinition = {
        schema_id: 'schema-new',
        erp_type: ERPType.SAP,
        erp_modules: [ERPModule.FINANCIAL_ACCOUNTING, ERPModule.HUMAN_RESOURCES],
        tables: [],
        relationships: [],
        total_tables: 0,
        total_relationships: 0,
        discovered_at: '2025-01-20T08:00:00Z',
        status: 'pending',
        tenant_id: 'tenant-1',
      };
      const mockResponse = wrapResponse(pendingSchema);
      vi.mocked(apiClient.post).mockResolvedValue(mockResponse);

      const result = await discoverSchema(mockSchemaDiscoveryRequest);

      expect(result.success).toBe(true);
      expect(result.data.schema_id).toBe('schema-new');
      expect(result.data.erp_type).toBe('sap');
      expect(result.data.status).toBe('pending');
      expect(result.data.tables).toEqual([]);
      expect(result.data.relationships).toEqual([]);
    });

    it('should handle timeout errors', async () => {
      const timeoutError = {
        response: {
          status: 504,
          data: {
            status_code: 504,
            error_code: 'GATEWAY_TIMEOUT',
            message: 'ERP schema discovery timed out',
            timestamp: '2025-01-20T12:05:00Z',
          },
        },
      };
      vi.mocked(apiClient.post).mockRejectedValue(timeoutError);

      await expect(discoverSchema(mockSchemaDiscoveryRequest)).rejects.toEqual(timeoutError);
      expect(apiClient.post).toHaveBeenCalledTimes(1);
    });
  });

  describe('getSchemas', () => {
    it('should call GET /api/v1/schemas with filter params', async () => {
      const mockResponse = wrapResponse<PaginatedResult<SchemaDefinition>>({
        items: [mockSchemaDefinition],
        total: 1,
        page: 1,
        page_size: 20,
        total_pages: 1,
      });
      vi.mocked(apiClient.get).mockResolvedValue(mockResponse);

      await getSchemas({
        erp_type: 'dynamics',
        erp_module: 'sales_distribution',
        status: 'completed',
      });

      expect(apiClient.get).toHaveBeenCalledWith(
        '/api/v1/schemas',
        {
          params: {
            erp_type: 'dynamics',
            erp_module: 'sales_distribution',
            status: 'completed',
          },
        }
      );
    });

    it('should return paginated schema list', async () => {
      const paginatedData: PaginatedResult<SchemaDefinition> = {
        items: [mockSchemaDefinition],
        total: 25,
        page: 2,
        page_size: 10,
        total_pages: 3,
      };
      const mockResponse = wrapResponse(paginatedData);
      vi.mocked(apiClient.get).mockResolvedValue(mockResponse);

      const result = await getSchemas({ page: 2, page_size: 10 });

      expect(result.success).toBe(true);
      expect(result.data.items).toHaveLength(1);
      expect(result.data.items[0].schema_id).toBe('schema-123');
      expect(result.data.items[0].erp_type).toBe('sap');
      expect(result.data.total).toBe(25);
      expect(result.data.page).toBe(2);
      expect(result.data.page_size).toBe(10);
      expect(result.data.total_pages).toBe(3);
    });

    it('should call GET /api/v1/schemas with default params when no args provided', async () => {
      const mockResponse = wrapResponse<PaginatedResult<SchemaDefinition>>({
        items: [],
        total: 0,
        page: 1,
        page_size: 20,
        total_pages: 0,
      });
      vi.mocked(apiClient.get).mockResolvedValue(mockResponse);

      await getSchemas();

      expect(apiClient.get).toHaveBeenCalledWith(
        '/api/v1/schemas',
        { params: {} }
      );
    });

    it('should pass sort parameters when provided', async () => {
      const mockResponse = wrapResponse<PaginatedResult<SchemaDefinition>>({
        items: [],
        total: 0,
        page: 1,
        page_size: 20,
        total_pages: 0,
      });
      vi.mocked(apiClient.get).mockResolvedValue(mockResponse);

      await getSchemas({
        page: 1,
        page_size: 20,
        sort_by: 'discovered_at',
        sort_direction: 'desc',
      });

      expect(apiClient.get).toHaveBeenCalledWith(
        '/api/v1/schemas',
        {
          params: {
            page: 1,
            page_size: 20,
            sort_by: 'discovered_at',
            sort_direction: 'desc',
          },
        }
      );
    });
  });

  describe('getSchemaById', () => {
    it('should call GET /api/v1/schemas/{schemaId}', async () => {
      const mockResponse = wrapResponse(mockSchemaDefinition);
      vi.mocked(apiClient.get).mockResolvedValue(mockResponse);

      await getSchemaById('schema-123');

      expect(apiClient.get).toHaveBeenCalledTimes(1);
      expect(apiClient.get).toHaveBeenCalledWith('/api/v1/schemas/schema-123');
    });

    it('should return full schema definition with tables and relationships', async () => {
      const mockResponse = wrapResponse(mockSchemaDefinition);
      vi.mocked(apiClient.get).mockResolvedValue(mockResponse);

      const result = await getSchemaById('schema-123');

      expect(result.success).toBe(true);
      expect(result.data.schema_id).toBe('schema-123');
      expect(result.data.erp_type).toBe('sap');
      expect(result.data.status).toBe('completed');

      // Verify tables with columns and primary keys
      expect(result.data.tables).toHaveLength(2);
      const glEntries = result.data.tables[0];
      expect(glEntries.name).toBe('GL_ENTRIES');
      expect(glEntries.columns).toHaveLength(4);
      expect(glEntries.primary_keys).toEqual(['ENTRY_ID']);
      expect(glEntries.row_count).toBe(150000);

      const glAccounts = result.data.tables[1];
      expect(glAccounts.name).toBe('GL_ACCOUNTS');
      expect(glAccounts.columns).toHaveLength(2);
      expect(glAccounts.primary_keys).toEqual(['ACCOUNT_ID']);

      // Verify relationships
      expect(result.data.relationships).toHaveLength(1);
      const rel = result.data.relationships[0];
      expect(rel.name).toBe('FK_GL_ENTRIES_ACCOUNT');
      expect(rel.source_table).toBe('GL_ENTRIES');
      expect(rel.source_column).toBe('ACCOUNT_ID');
      expect(rel.target_table).toBe('GL_ACCOUNTS');
      expect(rel.target_column).toBe('ACCOUNT_ID');
      expect(rel.relationship_type).toBe('many_to_one');
    });

    it('should encode special characters in schemaId', async () => {
      const mockResponse = wrapResponse(mockSchemaDefinition);
      vi.mocked(apiClient.get).mockResolvedValue(mockResponse);

      await getSchemaById('schema/special&id');

      expect(apiClient.get).toHaveBeenCalledWith(
        `/api/v1/schemas/${encodeURIComponent('schema/special&id')}`
      );
    });
  });

  describe('getSchemaRelationships', () => {
    it('should call GET /api/v1/schemas/{schemaId}/relationships', async () => {
      const mockResponse = wrapResponse(mockSchemaRelationshipsData);
      vi.mocked(apiClient.get).mockResolvedValue(mockResponse);

      await getSchemaRelationships('schema-123');

      expect(apiClient.get).toHaveBeenCalledTimes(1);
      expect(apiClient.get).toHaveBeenCalledWith(
        '/api/v1/schemas/schema-123/relationships'
      );
    });

    it('should return relationships with dependency order', async () => {
      const mockResponse = wrapResponse(mockSchemaRelationshipsData);
      vi.mocked(apiClient.get).mockResolvedValue(mockResponse);

      const result = await getSchemaRelationships('schema-123');

      expect(result.success).toBe(true);
      expect(result.data.total).toBe(2);

      // Verify individual FK relationships
      expect(result.data.relationships).toHaveLength(2);

      const firstRel = result.data.relationships[0];
      expect(firstRel.name).toBe('FK_GL_ENTRIES_ACCOUNT');
      expect(firstRel.source_table).toBe('GL_ENTRIES');
      expect(firstRel.source_column).toBe('ACCOUNT_ID');
      expect(firstRel.target_table).toBe('GL_ACCOUNTS');
      expect(firstRel.target_column).toBe('ACCOUNT_ID');
      expect(firstRel.relationship_type).toBe('many_to_one');

      const secondRel = result.data.relationships[1];
      expect(secondRel.name).toBe('FK_INVOICES_CUSTOMER');
      expect(secondRel.source_table).toBe('INVOICES');
      expect(secondRel.source_column).toBe('CUSTOMER_ID');
      expect(secondRel.target_table).toBe('CUSTOMERS');
      expect(secondRel.target_column).toBe('CUSTOMER_ID');
      expect(secondRel.relationship_type).toBe('many_to_one');

      // Verify topological dependency ordering (parents before children)
      expect(result.data.dependency_order).toEqual([
        'GL_ACCOUNTS',
        'CUSTOMERS',
        'GL_ENTRIES',
        'INVOICES',
      ]);

      // Parent tables should appear before their child tables
      const glAccountsIdx = result.data.dependency_order.indexOf('GL_ACCOUNTS');
      const glEntriesIdx = result.data.dependency_order.indexOf('GL_ENTRIES');
      expect(glAccountsIdx).toBeLessThan(glEntriesIdx);

      const customersIdx = result.data.dependency_order.indexOf('CUSTOMERS');
      const invoicesIdx = result.data.dependency_order.indexOf('INVOICES');
      expect(customersIdx).toBeLessThan(invoicesIdx);
    });

    it('should handle empty relationships list', async () => {
      const emptyRelationships: SchemaRelationships = {
        relationships: [],
        total: 0,
        dependency_order: ['STANDALONE_TABLE'],
      };
      const mockResponse = wrapResponse(emptyRelationships);
      vi.mocked(apiClient.get).mockResolvedValue(mockResponse);

      const result = await getSchemaRelationships('schema-no-rels');

      expect(result.data.relationships).toHaveLength(0);
      expect(result.data.total).toBe(0);
      expect(result.data.dependency_order).toEqual(['STANDALONE_TABLE']);
    });
  });

  describe('deleteSchema', () => {
    it('should call DELETE /api/v1/schemas/{schemaId}', async () => {
      const mockResponse: ApiResponse<void> = {
        success: true,
        data: undefined as unknown as void,
        message: 'Schema deleted',
        timestamp: '2025-01-20T12:00:00Z',
      };
      vi.mocked(apiClient.delete).mockResolvedValue(mockResponse);

      await deleteSchema('schema-123');

      expect(apiClient.delete).toHaveBeenCalledTimes(1);
      expect(apiClient.delete).toHaveBeenCalledWith('/api/v1/schemas/schema-123');
    });

    it('should handle not found error', async () => {
      const notFoundError = {
        response: {
          status: 404,
          data: {
            status_code: 404,
            error_code: 'RESOURCE_NOT_FOUND',
            message: 'Schema definition not found',
            timestamp: '2025-01-20T12:00:00Z',
          },
        },
      };
      vi.mocked(apiClient.delete).mockRejectedValue(notFoundError);

      await expect(deleteSchema('nonexistent-schema')).rejects.toEqual(notFoundError);
      expect(apiClient.delete).toHaveBeenCalledTimes(1);
    });

    it('should handle successful deletion', async () => {
      const mockResponse: ApiResponse<void> = {
        success: true,
        data: undefined as unknown as void,
        message: 'Schema deleted successfully',
        timestamp: '2025-01-20T12:00:00Z',
      };
      vi.mocked(apiClient.delete).mockResolvedValue(mockResponse);

      const result = await deleteSchema('schema-123');

      expect(result.success).toBe(true);
      expect(result.message).toBe('Schema deleted successfully');
    });
  });

  describe('refreshSchema', () => {
    it('should call POST /api/v1/schemas/{schemaId}/refresh', async () => {
      const refreshedSchema: SchemaDefinition = {
        ...mockSchemaDefinition,
        discovered_at: '2025-01-20T14:00:00Z',
        status: 'pending',
      };
      const mockResponse = wrapResponse(refreshedSchema);
      vi.mocked(apiClient.post).mockResolvedValue(mockResponse);

      await refreshSchema('schema-123');

      expect(apiClient.post).toHaveBeenCalledTimes(1);
      expect(apiClient.post).toHaveBeenCalledWith(
        '/api/v1/schemas/schema-123/refresh'
      );
    });

    it('should return updated schema definition', async () => {
      const refreshedSchema: SchemaDefinition = {
        ...mockSchemaDefinition,
        discovered_at: '2025-01-20T14:00:00Z',
        total_tables: 3,
        status: 'completed',
      };
      const mockResponse = wrapResponse(refreshedSchema);
      vi.mocked(apiClient.post).mockResolvedValue(mockResponse);

      const result = await refreshSchema('schema-123');

      expect(result.success).toBe(true);
      expect(result.data.schema_id).toBe('schema-123');
      expect(result.data.discovered_at).toBe('2025-01-20T14:00:00Z');
      expect(result.data.total_tables).toBe(3);
      expect(result.data.status).toBe('completed');
    });

    it('should not send a request body', async () => {
      const refreshedSchema: SchemaDefinition = {
        ...mockSchemaDefinition,
        discovered_at: '2025-01-20T14:00:00Z',
      };
      const mockResponse = wrapResponse(refreshedSchema);
      vi.mocked(apiClient.post).mockResolvedValue(mockResponse);

      await refreshSchema('schema-123');

      // refreshSchema calls apiClient.post with URL only, no body
      const callArgs = vi.mocked(apiClient.post).mock.calls[0];
      expect(callArgs[0]).toBe('/api/v1/schemas/schema-123/refresh');
      expect(callArgs[1]).toBeUndefined();
    });

    it('should encode special characters in schemaId', async () => {
      const refreshedSchema: SchemaDefinition = {
        ...mockSchemaDefinition,
        discovered_at: '2025-01-20T14:00:00Z',
      };
      const mockResponse = wrapResponse(refreshedSchema);
      vi.mocked(apiClient.post).mockResolvedValue(mockResponse);

      await refreshSchema('schema/special&id');

      expect(apiClient.post).toHaveBeenCalledWith(
        `/api/v1/schemas/${encodeURIComponent('schema/special&id')}/refresh`
      );
    });
  });
});
