/**
 * @fileoverview Vitest unit tests for the schemaStore Zustand store.
 *
 * Comprehensive test suite covering all ERP schema and statistical profile
 * state management operations in the schemaStore. Tests validate:
 *   - Paginated list retrieval with ERP type/module/status filtering
 *   - Single item loading for detail views
 *   - Schema discovery and profile creation workflows
 *   - Deletion with cascading state cleanup
 *   - Schema refresh (re-discovery) with in-place updates
 *   - Per-operation loading and error state management
 *   - Synchronous state setters and error clearing utilities
 *   - Full store reset to initial state
 *
 * All profileApi service calls are mocked via vi.mock() to isolate store
 * logic from HTTP communication. Each test resets the store and mocks to
 * prevent cross-test state leakage.
 *
 * Covers 13 describe blocks:
 *  1.  Initial state verification
 *  2.  fetchSchemas (paginated list with filters)
 *  3.  fetchSchemaById (single schema loading)
 *  4.  discoverSchema (discovery request and response)
 *  5.  deleteSchema (removal from list, selectedSchema clearing)
 *  6.  refreshSchema (re-discover and update in-place)
 *  7.  fetchProfiles (paginated list with filters)
 *  8.  fetchProfileById (single profile loading)
 *  9.  createProfile (profiling request and prepend)
 * 10.  deleteProfile (removal from list, selectedProfile clearing)
 * 11.  State setters (setSelectedSchema, setSelectedProfile)
 * 12.  Error clearing (clearSchemaErrors, clearProfileErrors)
 * 13.  Full state reset
 *
 * @module tests/unit/web/store/schemaStore.test
 */

import { describe, it, expect, beforeEach, afterEach, vi } from 'vitest';
import { useSchemaStore } from '@/store/schemaStore';
import type { SchemaDefinition, SchemaDiscoveryRequest } from '@/types/schema';
import { ERPType, ERPModule, DataType } from '@/types/schema';
import type { StatisticalProfile, ProfileRequest } from '@/types/profile';
import type { ApiResponse, PaginatedResult } from '@/types/api';
import {
  getSchemas,
  getSchemaById,
  discoverSchema,
  deleteSchema,
  refreshSchema,
  getSchemaRelationships,
  getProfiles,
  getProfileById,
  createProfile,
  deleteProfile,
} from '@/services/profileApi';

// ============================================================================
// Module Mock — Replace all profileApi exports with vi.fn() stubs
// ============================================================================

/**
 * Globally mock the profileApi module to intercept all API calls made by
 * the schemaStore. Each exported function is replaced with a vi.fn() mock
 * whose return values are configured per test case via mockResolvedValue
 * or mockRejectedValue.
 */
vi.mock('@/services/profileApi', () => ({
  getSchemas: vi.fn(),
  getSchemaById: vi.fn(),
  discoverSchema: vi.fn(),
  deleteSchema: vi.fn(),
  refreshSchema: vi.fn(),
  getSchemaRelationships: vi.fn(),
  getProfiles: vi.fn(),
  getProfileById: vi.fn(),
  createProfile: vi.fn(),
  deleteProfile: vi.fn(),
}));

// Type-safe references to mocked service functions for mock configuration
const mockGetSchemas = vi.mocked(getSchemas);
const mockGetSchemaById = vi.mocked(getSchemaById);
const mockDiscoverSchema = vi.mocked(discoverSchema);
const mockDeleteSchema = vi.mocked(deleteSchema);
const mockRefreshSchema = vi.mocked(refreshSchema);
const mockGetSchemaRelationships = vi.mocked(getSchemaRelationships);
const mockGetProfiles = vi.mocked(getProfiles);
const mockGetProfileById = vi.mocked(getProfileById);
const mockCreateProfile = vi.mocked(createProfile);
const mockDeleteProfile = vi.mocked(deleteProfile);

// ============================================================================
// Test Fixtures
// ============================================================================

/** Mock SAP Financial Accounting schema definition */
const mockSchema: SchemaDefinition = {
  schema_id: 'schema-001',
  erp_type: ERPType.SAP,
  erp_modules: [ERPModule.FINANCIAL_ACCOUNTING],
  tables: [
    {
      name: 'GL_JOURNAL_ENTRIES',
      columns: [
        {
          name: 'entry_id',
          data_type: DataType.INTEGER,
          nullable: false,
          primary_key: true,
        },
        {
          name: 'amount',
          data_type: DataType.DECIMAL,
          nullable: false,
          primary_key: false,
          precision: 18,
          scale: 2,
        },
      ],
      primary_keys: ['entry_id'],
      row_count: 50000,
    },
  ],
  relationships: [],
  total_tables: 1,
  total_relationships: 0,
  discovered_at: '2025-01-15T10:00:00Z',
  status: 'completed',
  tenant_id: 'tenant-001',
};

/** Mock Oracle EBS HR schema definition */
const mockSchema2: SchemaDefinition = {
  schema_id: 'schema-002',
  erp_type: ERPType.ORACLE_EBS,
  erp_modules: [ERPModule.HUMAN_RESOURCES],
  tables: [
    {
      name: 'EMPLOYEES',
      columns: [
        {
          name: 'employee_id',
          data_type: DataType.INTEGER,
          nullable: false,
          primary_key: true,
        },
      ],
      primary_keys: ['employee_id'],
      row_count: 10000,
    },
  ],
  relationships: [
    {
      name: 'fk_emp_dept',
      source_table: 'EMPLOYEES',
      source_column: 'department_id',
      target_table: 'DEPARTMENTS',
      target_column: 'dept_id',
      relationship_type: 'many_to_one',
    },
  ],
  total_tables: 3,
  total_relationships: 2,
  discovered_at: '2025-01-16T10:00:00Z',
  status: 'completed',
  tenant_id: 'tenant-001',
};

/** Mock statistical profile for SAP Financial Accounting */
const mockProfile: StatisticalProfile = {
  profile_id: 'profile-001',
  erp_type: 'sap',
  erp_module: 'financial_accounting',
  tables: [
    {
      table_name: 'GL_JOURNAL_ENTRIES',
      row_count: 50000,
      columns: [
        {
          column_name: 'entry_id',
          data_type: 'integer',
          nullable: false,
          statistics: {
            column_name: 'entry_id',
            data_type: 'integer',
            distribution: {
              type: 'uniform' as any,
              parameters: { low: 1, high: 50000 },
              fit_score: 0.95,
            },
            mean: 25000,
            median: 25001,
            std_dev: 14433,
            min_value: 1,
            max_value: 50000,
            null_percentage: 0,
            unique_count: 50000,
          },
        },
      ],
    },
  ],
  total_tables: 1,
  total_columns: 5,
  created_at: '2025-01-15T12:00:00Z',
  updated_at: '2025-01-15T12:30:00Z',
  status: 'completed',
  tenant_id: 'tenant-001',
};

/** Mock statistical profile for Oracle EBS HR */
const mockProfile2: StatisticalProfile = {
  profile_id: 'profile-002',
  erp_type: 'oracle_ebs',
  erp_module: 'hr',
  tables: [
    {
      table_name: 'EMPLOYEES',
      row_count: 10000,
      columns: [
        {
          column_name: 'employee_id',
          data_type: 'integer',
          nullable: false,
        },
      ],
    },
  ],
  total_tables: 2,
  total_columns: 8,
  created_at: '2025-01-16T12:00:00Z',
  updated_at: '2025-01-16T12:45:00Z',
  status: 'completed',
  tenant_id: 'tenant-001',
};

/** Mock paginated schema list API response */
const mockPaginatedSchemas: ApiResponse<PaginatedResult<SchemaDefinition>> = {
  success: true,
  data: {
    items: [mockSchema, mockSchema2],
    total: 2,
    page: 1,
    page_size: 20,
    total_pages: 1,
  },
  message: null,
  timestamp: '2025-01-15T10:00:00Z',
};

/** Mock paginated profile list API response */
const mockPaginatedProfiles: ApiResponse<PaginatedResult<StatisticalProfile>> = {
  success: true,
  data: {
    items: [mockProfile, mockProfile2],
    total: 2,
    page: 1,
    page_size: 20,
    total_pages: 1,
  },
  message: null,
  timestamp: '2025-01-15T12:00:00Z',
};

/** Mock schema discovery request fixture */
const mockDiscoveryRequest: SchemaDiscoveryRequest = {
  erp_type: ERPType.SAP,
  connection_params: {
    host: 'erp.example.com',
    port: 3300,
    username: 'discoverer',
    password: 'secure-pass',
    connection_type: 'rfc',
  },
  modules: [ERPModule.FINANCIAL_ACCOUNTING, ERPModule.HUMAN_RESOURCES],
  include_relationships: true,
  include_indexes: false,
};

/** Mock profile creation request fixture */
const mockProfileRequest: ProfileRequest = {
  erp_type: 'sap',
  connection_type: 'rfc',
  host: 'erp.example.com',
  port: 3300,
  username: 'profiler',
  password: 'secure-pass',
  erp_module: 'financial_accounting',
  include_statistics: true,
  include_relationships: true,
  sample_size: 10000,
};

// ============================================================================
// Helper Utilities
// ============================================================================

/**
 * Creates a typed ApiResponse envelope around the given data payload.
 * Used for configuring mockResolvedValue on mocked service functions.
 *
 * @param data - The response payload to wrap
 * @returns A fully-formed ApiResponse object with success=true
 */
function apiResponse<T>(data: T): ApiResponse<T> {
  return {
    success: true,
    data,
    message: null,
    timestamp: '2025-01-15T10:00:00Z',
  };
}

// ============================================================================
// Test Suite
// ============================================================================

describe('schemaStore', () => {
  /**
   * Before each test:
   *  1. Reset all mock implementations (prevents state leakage between tests)
   *  2. Reset the Zustand store to initial values
   *  3. Set up default mock for getSchemaRelationships (fire-and-forget in fetchSchemaById)
   */
  beforeEach(() => {
    vi.resetAllMocks();
    useSchemaStore.getState().reset();
    // Default mock for the fire-and-forget relationship pre-fetch in fetchSchemaById.
    // Without this, calling .catch() on undefined (vi.fn() default return) would throw.
    mockGetSchemaRelationships.mockResolvedValue(
      apiResponse({
        relationships: [],
        total: 0,
        dependency_order: [],
      }) as any
    );
  });

  afterEach(() => {
    vi.restoreAllMocks();
  });

  // ==========================================================================
  // 1. Initial State
  // ==========================================================================

  describe('initial state', () => {
    it('should have empty schemas array', () => {
      expect(useSchemaStore.getState().schemas).toEqual([]);
    });

    it('should have null selectedSchema', () => {
      expect(useSchemaStore.getState().selectedSchema).toBeNull();
    });

    it('should have zero schemasTotal', () => {
      expect(useSchemaStore.getState().schemasTotal).toBe(0);
    });

    it('should have default schemasPage of 1', () => {
      expect(useSchemaStore.getState().schemasPage).toBe(1);
    });

    it('should have default schemasPageSize of 20', () => {
      expect(useSchemaStore.getState().schemasPageSize).toBe(20);
    });

    it('should have empty profiles array', () => {
      expect(useSchemaStore.getState().profiles).toEqual([]);
    });

    it('should have null selectedProfile', () => {
      expect(useSchemaStore.getState().selectedProfile).toBeNull();
    });

    it('should have zero profilesTotal', () => {
      expect(useSchemaStore.getState().profilesTotal).toBe(0);
    });

    it('should have all loading states set to false', () => {
      const state = useSchemaStore.getState();
      expect(state.isSchemasLoading).toBe(false);
      expect(state.isSchemaDetailLoading).toBe(false);
      expect(state.isDiscovering).toBe(false);
      expect(state.isProfilesLoading).toBe(false);
      expect(state.isProfileDetailLoading).toBe(false);
      expect(state.isProfiling).toBe(false);
    });

    it('should have all error states set to null', () => {
      const state = useSchemaStore.getState();
      expect(state.schemasError).toBeNull();
      expect(state.schemaDetailError).toBeNull();
      expect(state.discoveryError).toBeNull();
      expect(state.profilesError).toBeNull();
      expect(state.profileDetailError).toBeNull();
      expect(state.profilingError).toBeNull();
    });
  });

  // ==========================================================================
  // 2. fetchSchemas
  // ==========================================================================

  describe('fetchSchemas', () => {
    it('should set isSchemasLoading to true while fetching', async () => {
      let loadingDuringFetch = false;
      mockGetSchemas.mockImplementation(() => {
        loadingDuringFetch = useSchemaStore.getState().isSchemasLoading;
        return Promise.resolve(mockPaginatedSchemas);
      });

      await useSchemaStore.getState().fetchSchemas();

      expect(loadingDuringFetch).toBe(true);
    });

    it('should set isSchemasLoading to false after successful fetch', async () => {
      mockGetSchemas.mockResolvedValue(mockPaginatedSchemas);

      await useSchemaStore.getState().fetchSchemas();

      expect(useSchemaStore.getState().isSchemasLoading).toBe(false);
    });

    it('should update schemas array and pagination from paginated response', async () => {
      mockGetSchemas.mockResolvedValue(mockPaginatedSchemas);

      await useSchemaStore.getState().fetchSchemas();

      const state = useSchemaStore.getState();
      expect(state.schemas).toEqual([mockSchema, mockSchema2]);
      expect(state.schemasTotal).toBe(2);
      expect(state.schemasPage).toBe(1);
      expect(state.schemasPageSize).toBe(20);
    });

    it('should pass pagination and filter parameters to getSchemas', async () => {
      mockGetSchemas.mockResolvedValue(mockPaginatedSchemas);
      const params = {
        page: 2,
        page_size: 10,
        erp_type: 'sap',
        erp_module: 'financial_accounting',
        status: 'completed',
      };

      await useSchemaStore.getState().fetchSchemas(params);

      expect(mockGetSchemas).toHaveBeenCalledWith(params);
    });

    it('should call getSchemas with undefined when no params provided', async () => {
      mockGetSchemas.mockResolvedValue(mockPaginatedSchemas);

      await useSchemaStore.getState().fetchSchemas();

      expect(mockGetSchemas).toHaveBeenCalledWith(undefined);
    });

    it('should set schemasError on failure with Error instance', async () => {
      mockGetSchemas.mockRejectedValue(new Error('Network error'));

      await useSchemaStore.getState().fetchSchemas();

      expect(useSchemaStore.getState().schemasError).toBe('Network error');
    });

    it('should extract API error message from axios error response', async () => {
      mockGetSchemas.mockRejectedValue({
        response: { data: { message: 'Unauthorized access' } },
      });

      await useSchemaStore.getState().fetchSchemas();

      expect(useSchemaStore.getState().schemasError).toBe('Unauthorized access');
    });

    it('should use fallback error message for unknown error types', async () => {
      mockGetSchemas.mockRejectedValue(42);

      await useSchemaStore.getState().fetchSchemas();

      expect(useSchemaStore.getState().schemasError).toBe('Failed to fetch schemas');
    });

    it('should clear schemasError before fetching', async () => {
      // First call: produce an error
      mockGetSchemas.mockRejectedValue(new Error('First error'));
      await useSchemaStore.getState().fetchSchemas();
      expect(useSchemaStore.getState().schemasError).toBe('First error');

      // Second call: successful fetch clears error
      mockGetSchemas.mockResolvedValue(mockPaginatedSchemas);
      await useSchemaStore.getState().fetchSchemas();
      expect(useSchemaStore.getState().schemasError).toBeNull();
    });

    it('should set isSchemasLoading to false after failure', async () => {
      mockGetSchemas.mockRejectedValue(new Error('Server error'));

      await useSchemaStore.getState().fetchSchemas();

      expect(useSchemaStore.getState().isSchemasLoading).toBe(false);
    });

    it('should handle string error values', async () => {
      mockGetSchemas.mockRejectedValue('Raw string error');

      await useSchemaStore.getState().fetchSchemas();

      expect(useSchemaStore.getState().schemasError).toBe('Raw string error');
    });
  });

  // ==========================================================================
  // 3. fetchSchemaById
  // ==========================================================================

  describe('fetchSchemaById', () => {
    it('should set isSchemaDetailLoading to true while fetching', async () => {
      let loadingDuringFetch = false;
      mockGetSchemaById.mockImplementation(() => {
        loadingDuringFetch = useSchemaStore.getState().isSchemaDetailLoading;
        return Promise.resolve(apiResponse(mockSchema));
      });

      await useSchemaStore.getState().fetchSchemaById('schema-001');

      expect(loadingDuringFetch).toBe(true);
    });

    it('should set selectedSchema on success', async () => {
      mockGetSchemaById.mockResolvedValue(apiResponse(mockSchema));

      await useSchemaStore.getState().fetchSchemaById('schema-001');

      expect(useSchemaStore.getState().selectedSchema).toEqual(mockSchema);
    });

    it('should pre-fetch schema relationships via getSchemaRelationships', async () => {
      mockGetSchemaById.mockResolvedValue(apiResponse(mockSchema));

      await useSchemaStore.getState().fetchSchemaById('schema-001');

      expect(mockGetSchemaRelationships).toHaveBeenCalledWith('schema-001');
    });

    it('should not affect selectedSchema if relationship pre-fetch fails', async () => {
      mockGetSchemaById.mockResolvedValue(apiResponse(mockSchema));
      mockGetSchemaRelationships.mockRejectedValue(
        new Error('Relationships fetch failed')
      );

      await useSchemaStore.getState().fetchSchemaById('schema-001');

      // Allow microtask queue to flush for the .catch() handler
      await new Promise((resolve) => setTimeout(resolve, 0));

      // Primary schema data should still be loaded successfully
      expect(useSchemaStore.getState().selectedSchema).toEqual(mockSchema);
      expect(useSchemaStore.getState().schemaDetailError).toBeNull();
    });

    it('should set schemaDetailError on failure', async () => {
      mockGetSchemaById.mockRejectedValue(new Error('Schema not found'));

      await useSchemaStore.getState().fetchSchemaById('nonexistent');

      expect(useSchemaStore.getState().schemaDetailError).toBe('Schema not found');
    });

    it('should clear schemaDetailError before fetching', async () => {
      // First: set error state
      mockGetSchemaById.mockRejectedValue(new Error('First error'));
      await useSchemaStore.getState().fetchSchemaById('bad-id');
      expect(useSchemaStore.getState().schemaDetailError).toBe('First error');

      // Second: new fetch should clear previous error
      mockGetSchemaById.mockResolvedValue(apiResponse(mockSchema));
      await useSchemaStore.getState().fetchSchemaById('schema-001');
      expect(useSchemaStore.getState().schemaDetailError).toBeNull();
    });

    it('should set isSchemaDetailLoading to false after success', async () => {
      mockGetSchemaById.mockResolvedValue(apiResponse(mockSchema));

      await useSchemaStore.getState().fetchSchemaById('schema-001');

      expect(useSchemaStore.getState().isSchemaDetailLoading).toBe(false);
    });

    it('should set isSchemaDetailLoading to false after failure', async () => {
      mockGetSchemaById.mockRejectedValue(new Error('Failed'));

      await useSchemaStore.getState().fetchSchemaById('bad-id');

      expect(useSchemaStore.getState().isSchemaDetailLoading).toBe(false);
    });

    it('should extract API error message from structured axios error', async () => {
      mockGetSchemaById.mockRejectedValue({
        response: { data: { message: 'Access denied' } },
      });

      await useSchemaStore.getState().fetchSchemaById('schema-001');

      expect(useSchemaStore.getState().schemaDetailError).toBe('Access denied');
    });

    it('should use fallback message for unrecognized error shapes', async () => {
      mockGetSchemaById.mockRejectedValue(null);

      await useSchemaStore.getState().fetchSchemaById('schema-001');

      expect(useSchemaStore.getState().schemaDetailError).toBe(
        'Failed to fetch schema details'
      );
    });
  });

  // ==========================================================================
  // 4. discoverSchema
  // ==========================================================================

  describe('discoverSchema', () => {
    it('should set isDiscovering to true while discovering', async () => {
      let discoveringDuringCall = false;
      mockDiscoverSchema.mockImplementation(() => {
        discoveringDuringCall = useSchemaStore.getState().isDiscovering;
        return Promise.resolve(apiResponse(mockSchema));
      });

      await useSchemaStore.getState().discoverSchema(mockDiscoveryRequest);

      expect(discoveringDuringCall).toBe(true);
    });

    it('should prepend newly discovered schema to schemas list', async () => {
      // Pre-populate schemas with fetchSchemas
      mockGetSchemas.mockResolvedValue(mockPaginatedSchemas);
      await useSchemaStore.getState().fetchSchemas();
      expect(useSchemaStore.getState().schemas).toHaveLength(2);

      // Discover a new schema
      const newSchema: SchemaDefinition = {
        ...mockSchema,
        schema_id: 'schema-003',
        discovered_at: '2025-01-17T10:00:00Z',
      };
      mockDiscoverSchema.mockResolvedValue(apiResponse(newSchema));

      await useSchemaStore.getState().discoverSchema(mockDiscoveryRequest);

      const schemas = useSchemaStore.getState().schemas;
      expect(schemas).toHaveLength(3);
      expect(schemas[0].schema_id).toBe('schema-003');
    });

    it('should return the newly created SchemaDefinition on success', async () => {
      mockDiscoverSchema.mockResolvedValue(apiResponse(mockSchema));

      const result = await useSchemaStore.getState().discoverSchema(
        mockDiscoveryRequest
      );

      expect(result).toEqual(mockSchema);
    });

    it('should call discoverSchema API with the request payload', async () => {
      mockDiscoverSchema.mockResolvedValue(apiResponse(mockSchema));

      await useSchemaStore.getState().discoverSchema(mockDiscoveryRequest);

      expect(mockDiscoverSchema).toHaveBeenCalledWith(mockDiscoveryRequest);
    });

    it('should set discoveryError on failure', async () => {
      mockDiscoverSchema.mockRejectedValue(new Error('Connection refused'));

      await useSchemaStore.getState().discoverSchema(mockDiscoveryRequest);

      expect(useSchemaStore.getState().discoveryError).toBe('Connection refused');
    });

    it('should return null on failure', async () => {
      mockDiscoverSchema.mockRejectedValue(new Error('Timeout'));

      const result = await useSchemaStore.getState().discoverSchema(
        mockDiscoveryRequest
      );

      expect(result).toBeNull();
    });

    it('should set isDiscovering to false after success', async () => {
      mockDiscoverSchema.mockResolvedValue(apiResponse(mockSchema));

      await useSchemaStore.getState().discoverSchema(mockDiscoveryRequest);

      expect(useSchemaStore.getState().isDiscovering).toBe(false);
    });

    it('should set isDiscovering to false after failure', async () => {
      mockDiscoverSchema.mockRejectedValue(new Error('Error'));

      await useSchemaStore.getState().discoverSchema(mockDiscoveryRequest);

      expect(useSchemaStore.getState().isDiscovering).toBe(false);
    });

    it('should clear discoveryError before starting', async () => {
      // First: set error
      mockDiscoverSchema.mockRejectedValue(new Error('Previous error'));
      await useSchemaStore.getState().discoverSchema(mockDiscoveryRequest);
      expect(useSchemaStore.getState().discoveryError).toBe('Previous error');

      // Second: new discovery clears previous error
      mockDiscoverSchema.mockResolvedValue(apiResponse(mockSchema));
      await useSchemaStore.getState().discoverSchema(mockDiscoveryRequest);
      expect(useSchemaStore.getState().discoveryError).toBeNull();
    });

    it('should prepend to empty schemas list', async () => {
      expect(useSchemaStore.getState().schemas).toHaveLength(0);

      mockDiscoverSchema.mockResolvedValue(apiResponse(mockSchema));

      await useSchemaStore.getState().discoverSchema(mockDiscoveryRequest);

      const schemas = useSchemaStore.getState().schemas;
      expect(schemas).toHaveLength(1);
      expect(schemas[0].schema_id).toBe('schema-001');
    });

    it('should extract API error message from axios error', async () => {
      mockDiscoverSchema.mockRejectedValue({
        response: { data: { message: 'Invalid ERP credentials' } },
      });

      await useSchemaStore.getState().discoverSchema(mockDiscoveryRequest);

      expect(useSchemaStore.getState().discoveryError).toBe(
        'Invalid ERP credentials'
      );
    });
  });

  // ==========================================================================
  // 5. deleteSchema
  // ==========================================================================

  describe('deleteSchema', () => {
    beforeEach(async () => {
      // Pre-populate schemas list for deletion tests
      mockGetSchemas.mockResolvedValue(mockPaginatedSchemas);
      await useSchemaStore.getState().fetchSchemas();
    });

    it('should remove schema from schemas list on success', async () => {
      mockDeleteSchema.mockResolvedValue(apiResponse(undefined as any));

      await useSchemaStore.getState().deleteSchema('schema-001');

      const schemas = useSchemaStore.getState().schemas;
      expect(schemas).toHaveLength(1);
      expect(schemas[0].schema_id).toBe('schema-002');
    });

    it('should return true on successful deletion', async () => {
      mockDeleteSchema.mockResolvedValue(apiResponse(undefined as any));

      const result = await useSchemaStore.getState().deleteSchema('schema-001');

      expect(result).toBe(true);
    });

    it('should clear selectedSchema if it matches the deleted schema', async () => {
      // Select the schema that will be deleted
      useSchemaStore.getState().setSelectedSchema(mockSchema);
      expect(useSchemaStore.getState().selectedSchema?.schema_id).toBe(
        'schema-001'
      );

      mockDeleteSchema.mockResolvedValue(apiResponse(undefined as any));

      await useSchemaStore.getState().deleteSchema('schema-001');

      expect(useSchemaStore.getState().selectedSchema).toBeNull();
    });

    it('should not clear selectedSchema if it does not match deleted schema', async () => {
      // Select a different schema from the one being deleted
      useSchemaStore.getState().setSelectedSchema(mockSchema2);
      expect(useSchemaStore.getState().selectedSchema?.schema_id).toBe(
        'schema-002'
      );

      mockDeleteSchema.mockResolvedValue(apiResponse(undefined as any));

      await useSchemaStore.getState().deleteSchema('schema-001');

      expect(useSchemaStore.getState().selectedSchema?.schema_id).toBe(
        'schema-002'
      );
    });

    it('should set schemasError on failure', async () => {
      mockDeleteSchema.mockRejectedValue(new Error('Deletion failed'));

      await useSchemaStore.getState().deleteSchema('schema-001');

      expect(useSchemaStore.getState().schemasError).toBe('Deletion failed');
    });

    it('should return false on failure', async () => {
      mockDeleteSchema.mockRejectedValue(new Error('Forbidden'));

      const result = await useSchemaStore.getState().deleteSchema('schema-001');

      expect(result).toBe(false);
    });

    it('should not remove schema from list on failure', async () => {
      mockDeleteSchema.mockRejectedValue(new Error('Server error'));

      await useSchemaStore.getState().deleteSchema('schema-001');

      expect(useSchemaStore.getState().schemas).toHaveLength(2);
    });

    it('should call deleteSchema API with schema ID', async () => {
      mockDeleteSchema.mockResolvedValue(apiResponse(undefined as any));

      await useSchemaStore.getState().deleteSchema('schema-001');

      expect(mockDeleteSchema).toHaveBeenCalledWith('schema-001');
    });

    it('should handle deletion when selectedSchema is null', async () => {
      expect(useSchemaStore.getState().selectedSchema).toBeNull();

      mockDeleteSchema.mockResolvedValue(apiResponse(undefined as any));

      await useSchemaStore.getState().deleteSchema('schema-001');

      expect(useSchemaStore.getState().selectedSchema).toBeNull();
      expect(useSchemaStore.getState().schemas).toHaveLength(1);
    });

    it('should use fallback error message for non-Error rejection', async () => {
      mockDeleteSchema.mockRejectedValue(undefined);

      await useSchemaStore.getState().deleteSchema('schema-001');

      expect(useSchemaStore.getState().schemasError).toBe(
        'Failed to delete schema'
      );
    });
  });

  // ==========================================================================
  // 6. refreshSchema
  // ==========================================================================

  describe('refreshSchema', () => {
    const refreshedSchema: SchemaDefinition = {
      ...mockSchema,
      total_tables: 5,
      total_relationships: 3,
      discovered_at: '2025-01-20T10:00:00Z',
      status: 'completed',
    };

    beforeEach(async () => {
      // Pre-populate schemas list for refresh tests
      mockGetSchemas.mockResolvedValue(mockPaginatedSchemas);
      await useSchemaStore.getState().fetchSchemas();
    });

    it('should set isSchemaDetailLoading to true while refreshing', async () => {
      let loadingDuringRefresh = false;
      mockRefreshSchema.mockImplementation(() => {
        loadingDuringRefresh = useSchemaStore.getState().isSchemaDetailLoading;
        return Promise.resolve(apiResponse(refreshedSchema));
      });

      await useSchemaStore.getState().refreshSchema('schema-001');

      expect(loadingDuringRefresh).toBe(true);
    });

    it('should update schema in-place within the schemas list', async () => {
      mockRefreshSchema.mockResolvedValue(apiResponse(refreshedSchema));

      await useSchemaStore.getState().refreshSchema('schema-001');

      const schemas = useSchemaStore.getState().schemas;
      expect(schemas).toHaveLength(2);
      const updated = schemas.find((s) => s.schema_id === 'schema-001');
      expect(updated?.total_tables).toBe(5);
      expect(updated?.total_relationships).toBe(3);
      expect(updated?.discovered_at).toBe('2025-01-20T10:00:00Z');
    });

    it('should preserve other schemas in the list', async () => {
      mockRefreshSchema.mockResolvedValue(apiResponse(refreshedSchema));

      await useSchemaStore.getState().refreshSchema('schema-001');

      const schemas = useSchemaStore.getState().schemas;
      const unchanged = schemas.find((s) => s.schema_id === 'schema-002');
      expect(unchanged).toEqual(mockSchema2);
    });

    it('should update selectedSchema if it matches the refreshed schema', async () => {
      // Select the schema being refreshed
      useSchemaStore.getState().setSelectedSchema(mockSchema);
      expect(useSchemaStore.getState().selectedSchema?.total_tables).toBe(1);

      mockRefreshSchema.mockResolvedValue(apiResponse(refreshedSchema));

      await useSchemaStore.getState().refreshSchema('schema-001');

      expect(useSchemaStore.getState().selectedSchema?.total_tables).toBe(5);
      expect(useSchemaStore.getState().selectedSchema?.discovered_at).toBe(
        '2025-01-20T10:00:00Z'
      );
    });

    it('should not update selectedSchema if it does not match', async () => {
      // Select a different schema
      useSchemaStore.getState().setSelectedSchema(mockSchema2);

      mockRefreshSchema.mockResolvedValue(apiResponse(refreshedSchema));

      await useSchemaStore.getState().refreshSchema('schema-001');

      expect(useSchemaStore.getState().selectedSchema?.schema_id).toBe(
        'schema-002'
      );
    });

    it('should set schemaDetailError on failure', async () => {
      mockRefreshSchema.mockRejectedValue(new Error('Refresh failed'));

      await useSchemaStore.getState().refreshSchema('schema-001');

      expect(useSchemaStore.getState().schemaDetailError).toBe('Refresh failed');
    });

    it('should clear schemaDetailError before refreshing', async () => {
      // First: set error
      mockRefreshSchema.mockRejectedValue(new Error('Previous error'));
      await useSchemaStore.getState().refreshSchema('schema-001');
      expect(useSchemaStore.getState().schemaDetailError).toBe('Previous error');

      // Second: new refresh clears error
      mockRefreshSchema.mockResolvedValue(apiResponse(refreshedSchema));
      await useSchemaStore.getState().refreshSchema('schema-001');
      expect(useSchemaStore.getState().schemaDetailError).toBeNull();
    });

    it('should set isSchemaDetailLoading to false after success', async () => {
      mockRefreshSchema.mockResolvedValue(apiResponse(refreshedSchema));

      await useSchemaStore.getState().refreshSchema('schema-001');

      expect(useSchemaStore.getState().isSchemaDetailLoading).toBe(false);
    });

    it('should set isSchemaDetailLoading to false after failure', async () => {
      mockRefreshSchema.mockRejectedValue(new Error('Error'));

      await useSchemaStore.getState().refreshSchema('schema-001');

      expect(useSchemaStore.getState().isSchemaDetailLoading).toBe(false);
    });

    it('should call refreshSchema API with schema ID', async () => {
      mockRefreshSchema.mockResolvedValue(apiResponse(refreshedSchema));

      await useSchemaStore.getState().refreshSchema('schema-001');

      expect(mockRefreshSchema).toHaveBeenCalledWith('schema-001');
    });

    it('should handle refresh when selectedSchema is null', async () => {
      expect(useSchemaStore.getState().selectedSchema).toBeNull();

      mockRefreshSchema.mockResolvedValue(apiResponse(refreshedSchema));

      await useSchemaStore.getState().refreshSchema('schema-001');

      // selectedSchema should remain null since it didn't match
      expect(useSchemaStore.getState().selectedSchema).toBeNull();
      // But the list should still be updated
      const updated = useSchemaStore
        .getState()
        .schemas.find((s) => s.schema_id === 'schema-001');
      expect(updated?.total_tables).toBe(5);
    });
  });

  // ==========================================================================
  // 7. fetchProfiles
  // ==========================================================================

  describe('fetchProfiles', () => {
    it('should set isProfilesLoading to true while fetching', async () => {
      let loadingDuringFetch = false;
      mockGetProfiles.mockImplementation(() => {
        loadingDuringFetch = useSchemaStore.getState().isProfilesLoading;
        return Promise.resolve(mockPaginatedProfiles);
      });

      await useSchemaStore.getState().fetchProfiles();

      expect(loadingDuringFetch).toBe(true);
    });

    it('should set isProfilesLoading to false after successful fetch', async () => {
      mockGetProfiles.mockResolvedValue(mockPaginatedProfiles);

      await useSchemaStore.getState().fetchProfiles();

      expect(useSchemaStore.getState().isProfilesLoading).toBe(false);
    });

    it('should update profiles array and pagination from response', async () => {
      mockGetProfiles.mockResolvedValue(mockPaginatedProfiles);

      await useSchemaStore.getState().fetchProfiles();

      const state = useSchemaStore.getState();
      expect(state.profiles).toEqual([mockProfile, mockProfile2]);
      expect(state.profilesTotal).toBe(2);
    });

    it('should pass pagination and filter parameters to getProfiles', async () => {
      mockGetProfiles.mockResolvedValue(mockPaginatedProfiles);
      const params = {
        page: 1,
        page_size: 10,
        erp_type: 'sap',
        erp_module: 'financial_accounting',
        status: 'completed',
      };

      await useSchemaStore.getState().fetchProfiles(params);

      expect(mockGetProfiles).toHaveBeenCalledWith(params);
    });

    it('should call getProfiles with undefined when no params provided', async () => {
      mockGetProfiles.mockResolvedValue(mockPaginatedProfiles);

      await useSchemaStore.getState().fetchProfiles();

      expect(mockGetProfiles).toHaveBeenCalledWith(undefined);
    });

    it('should set profilesError on failure', async () => {
      mockGetProfiles.mockRejectedValue(new Error('Failed to load'));

      await useSchemaStore.getState().fetchProfiles();

      expect(useSchemaStore.getState().profilesError).toBe('Failed to load');
    });

    it('should clear profilesError before fetching', async () => {
      // First: set error
      mockGetProfiles.mockRejectedValue(new Error('First error'));
      await useSchemaStore.getState().fetchProfiles();
      expect(useSchemaStore.getState().profilesError).toBe('First error');

      // Second: clear error on new fetch
      mockGetProfiles.mockResolvedValue(mockPaginatedProfiles);
      await useSchemaStore.getState().fetchProfiles();
      expect(useSchemaStore.getState().profilesError).toBeNull();
    });

    it('should set isProfilesLoading to false after failure', async () => {
      mockGetProfiles.mockRejectedValue(new Error('Error'));

      await useSchemaStore.getState().fetchProfiles();

      expect(useSchemaStore.getState().isProfilesLoading).toBe(false);
    });

    it('should use fallback error message for non-Error rejections', async () => {
      mockGetProfiles.mockRejectedValue(null);

      await useSchemaStore.getState().fetchProfiles();

      expect(useSchemaStore.getState().profilesError).toBe(
        'Failed to fetch profiles'
      );
    });

    it('should extract API error message from axios error', async () => {
      mockGetProfiles.mockRejectedValue({
        response: { data: { message: 'Rate limit exceeded' } },
      });

      await useSchemaStore.getState().fetchProfiles();

      expect(useSchemaStore.getState().profilesError).toBe(
        'Rate limit exceeded'
      );
    });
  });

  // ==========================================================================
  // 8. fetchProfileById
  // ==========================================================================

  describe('fetchProfileById', () => {
    it('should set isProfileDetailLoading to true while fetching', async () => {
      let loadingDuringFetch = false;
      mockGetProfileById.mockImplementation(() => {
        loadingDuringFetch = useSchemaStore.getState().isProfileDetailLoading;
        return Promise.resolve(apiResponse(mockProfile));
      });

      await useSchemaStore.getState().fetchProfileById('profile-001');

      expect(loadingDuringFetch).toBe(true);
    });

    it('should set selectedProfile on success', async () => {
      mockGetProfileById.mockResolvedValue(apiResponse(mockProfile));

      await useSchemaStore.getState().fetchProfileById('profile-001');

      expect(useSchemaStore.getState().selectedProfile).toEqual(mockProfile);
    });

    it('should set profileDetailError on failure', async () => {
      mockGetProfileById.mockRejectedValue(new Error('Profile not found'));

      await useSchemaStore.getState().fetchProfileById('nonexistent');

      expect(useSchemaStore.getState().profileDetailError).toBe(
        'Profile not found'
      );
    });

    it('should clear profileDetailError before fetching', async () => {
      // First: set error
      mockGetProfileById.mockRejectedValue(new Error('Old error'));
      await useSchemaStore.getState().fetchProfileById('bad-id');
      expect(useSchemaStore.getState().profileDetailError).toBe('Old error');

      // Second: new fetch clears error
      mockGetProfileById.mockResolvedValue(apiResponse(mockProfile));
      await useSchemaStore.getState().fetchProfileById('profile-001');
      expect(useSchemaStore.getState().profileDetailError).toBeNull();
    });

    it('should set isProfileDetailLoading to false after success', async () => {
      mockGetProfileById.mockResolvedValue(apiResponse(mockProfile));

      await useSchemaStore.getState().fetchProfileById('profile-001');

      expect(useSchemaStore.getState().isProfileDetailLoading).toBe(false);
    });

    it('should set isProfileDetailLoading to false after failure', async () => {
      mockGetProfileById.mockRejectedValue(new Error('Error'));

      await useSchemaStore.getState().fetchProfileById('bad-id');

      expect(useSchemaStore.getState().isProfileDetailLoading).toBe(false);
    });

    it('should call getProfileById with profile ID', async () => {
      mockGetProfileById.mockResolvedValue(apiResponse(mockProfile));

      await useSchemaStore.getState().fetchProfileById('profile-001');

      expect(mockGetProfileById).toHaveBeenCalledWith('profile-001');
    });

    it('should extract API error message from axios error', async () => {
      mockGetProfileById.mockRejectedValue({
        response: { data: { message: 'Forbidden' } },
      });

      await useSchemaStore.getState().fetchProfileById('profile-001');

      expect(useSchemaStore.getState().profileDetailError).toBe('Forbidden');
    });

    it('should use fallback message for unrecognized error shapes', async () => {
      mockGetProfileById.mockRejectedValue({});

      await useSchemaStore.getState().fetchProfileById('profile-001');

      expect(useSchemaStore.getState().profileDetailError).toBe(
        'Failed to fetch profile details'
      );
    });
  });

  // ==========================================================================
  // 9. createProfile
  // ==========================================================================

  describe('createProfile', () => {
    it('should set isProfiling to true while creating', async () => {
      let profilingDuringCreate = false;
      mockCreateProfile.mockImplementation(() => {
        profilingDuringCreate = useSchemaStore.getState().isProfiling;
        return Promise.resolve(apiResponse(mockProfile));
      });

      await useSchemaStore.getState().createProfile(mockProfileRequest);

      expect(profilingDuringCreate).toBe(true);
    });

    it('should prepend new profile to profiles list on success', async () => {
      // Pre-populate profiles
      mockGetProfiles.mockResolvedValue(mockPaginatedProfiles);
      await useSchemaStore.getState().fetchProfiles();
      expect(useSchemaStore.getState().profiles).toHaveLength(2);

      // Create a new profile
      const newProfile: StatisticalProfile = {
        ...mockProfile,
        profile_id: 'profile-003',
        created_at: '2025-01-17T12:00:00Z',
      };
      mockCreateProfile.mockResolvedValue(apiResponse(newProfile));

      await useSchemaStore.getState().createProfile(mockProfileRequest);

      const profiles = useSchemaStore.getState().profiles;
      expect(profiles).toHaveLength(3);
      expect(profiles[0].profile_id).toBe('profile-003');
    });

    it('should return the newly created StatisticalProfile on success', async () => {
      mockCreateProfile.mockResolvedValue(apiResponse(mockProfile));

      const result = await useSchemaStore.getState().createProfile(
        mockProfileRequest
      );

      expect(result).toEqual(mockProfile);
    });

    it('should call createProfile API with the request payload', async () => {
      mockCreateProfile.mockResolvedValue(apiResponse(mockProfile));

      await useSchemaStore.getState().createProfile(mockProfileRequest);

      expect(mockCreateProfile).toHaveBeenCalledWith(mockProfileRequest);
    });

    it('should set profilingError on failure', async () => {
      mockCreateProfile.mockRejectedValue(new Error('Connection timeout'));

      await useSchemaStore.getState().createProfile(mockProfileRequest);

      expect(useSchemaStore.getState().profilingError).toBe(
        'Connection timeout'
      );
    });

    it('should return null on failure', async () => {
      mockCreateProfile.mockRejectedValue(new Error('Error'));

      const result = await useSchemaStore.getState().createProfile(
        mockProfileRequest
      );

      expect(result).toBeNull();
    });

    it('should set isProfiling to false after success', async () => {
      mockCreateProfile.mockResolvedValue(apiResponse(mockProfile));

      await useSchemaStore.getState().createProfile(mockProfileRequest);

      expect(useSchemaStore.getState().isProfiling).toBe(false);
    });

    it('should set isProfiling to false after failure', async () => {
      mockCreateProfile.mockRejectedValue(new Error('Error'));

      await useSchemaStore.getState().createProfile(mockProfileRequest);

      expect(useSchemaStore.getState().isProfiling).toBe(false);
    });

    it('should clear profilingError before creating', async () => {
      // First: set error
      mockCreateProfile.mockRejectedValue(new Error('Previous failure'));
      await useSchemaStore.getState().createProfile(mockProfileRequest);
      expect(useSchemaStore.getState().profilingError).toBe(
        'Previous failure'
      );

      // Second: new create clears previous error
      mockCreateProfile.mockResolvedValue(apiResponse(mockProfile));
      await useSchemaStore.getState().createProfile(mockProfileRequest);
      expect(useSchemaStore.getState().profilingError).toBeNull();
    });

    it('should prepend to empty profiles list', async () => {
      expect(useSchemaStore.getState().profiles).toHaveLength(0);

      mockCreateProfile.mockResolvedValue(apiResponse(mockProfile));

      await useSchemaStore.getState().createProfile(mockProfileRequest);

      const profiles = useSchemaStore.getState().profiles;
      expect(profiles).toHaveLength(1);
      expect(profiles[0].profile_id).toBe('profile-001');
    });

    it('should use fallback error message for non-standard errors', async () => {
      mockCreateProfile.mockRejectedValue(42);

      await useSchemaStore.getState().createProfile(mockProfileRequest);

      expect(useSchemaStore.getState().profilingError).toBe(
        'Profiling request failed'
      );
    });
  });

  // ==========================================================================
  // 10. deleteProfile
  // ==========================================================================

  describe('deleteProfile', () => {
    beforeEach(async () => {
      // Pre-populate profiles list for deletion tests
      mockGetProfiles.mockResolvedValue(mockPaginatedProfiles);
      await useSchemaStore.getState().fetchProfiles();
    });

    it('should remove profile from profiles list on success', async () => {
      mockDeleteProfile.mockResolvedValue(apiResponse(undefined as any));

      await useSchemaStore.getState().deleteProfile('profile-001');

      const profiles = useSchemaStore.getState().profiles;
      expect(profiles).toHaveLength(1);
      expect(profiles[0].profile_id).toBe('profile-002');
    });

    it('should return true on successful deletion', async () => {
      mockDeleteProfile.mockResolvedValue(apiResponse(undefined as any));

      const result = await useSchemaStore.getState().deleteProfile(
        'profile-001'
      );

      expect(result).toBe(true);
    });

    it('should clear selectedProfile if it matches deleted profile', async () => {
      useSchemaStore.getState().setSelectedProfile(mockProfile);
      expect(useSchemaStore.getState().selectedProfile?.profile_id).toBe(
        'profile-001'
      );

      mockDeleteProfile.mockResolvedValue(apiResponse(undefined as any));

      await useSchemaStore.getState().deleteProfile('profile-001');

      expect(useSchemaStore.getState().selectedProfile).toBeNull();
    });

    it('should not clear selectedProfile if it does not match', async () => {
      useSchemaStore.getState().setSelectedProfile(mockProfile2);
      expect(useSchemaStore.getState().selectedProfile?.profile_id).toBe(
        'profile-002'
      );

      mockDeleteProfile.mockResolvedValue(apiResponse(undefined as any));

      await useSchemaStore.getState().deleteProfile('profile-001');

      expect(useSchemaStore.getState().selectedProfile?.profile_id).toBe(
        'profile-002'
      );
    });

    it('should set profilesError on failure', async () => {
      mockDeleteProfile.mockRejectedValue(new Error('Cannot delete'));

      await useSchemaStore.getState().deleteProfile('profile-001');

      expect(useSchemaStore.getState().profilesError).toBe('Cannot delete');
    });

    it('should return false on failure', async () => {
      mockDeleteProfile.mockRejectedValue(new Error('Forbidden'));

      const result = await useSchemaStore.getState().deleteProfile(
        'profile-001'
      );

      expect(result).toBe(false);
    });

    it('should not remove profile from list on failure', async () => {
      mockDeleteProfile.mockRejectedValue(new Error('Error'));

      await useSchemaStore.getState().deleteProfile('profile-001');

      expect(useSchemaStore.getState().profiles).toHaveLength(2);
    });

    it('should call deleteProfile API with profile ID', async () => {
      mockDeleteProfile.mockResolvedValue(apiResponse(undefined as any));

      await useSchemaStore.getState().deleteProfile('profile-001');

      expect(mockDeleteProfile).toHaveBeenCalledWith('profile-001');
    });

    it('should handle deletion when selectedProfile is null', async () => {
      expect(useSchemaStore.getState().selectedProfile).toBeNull();

      mockDeleteProfile.mockResolvedValue(apiResponse(undefined as any));

      await useSchemaStore.getState().deleteProfile('profile-001');

      expect(useSchemaStore.getState().selectedProfile).toBeNull();
      expect(useSchemaStore.getState().profiles).toHaveLength(1);
    });

    it('should use fallback error message for non-Error rejection', async () => {
      mockDeleteProfile.mockRejectedValue(undefined);

      await useSchemaStore.getState().deleteProfile('profile-001');

      expect(useSchemaStore.getState().profilesError).toBe(
        'Failed to delete profile'
      );
    });
  });

  // ==========================================================================
  // 11. State Setters (setSelectedSchema / setSelectedProfile)
  // ==========================================================================

  describe('setSelectedSchema and setSelectedProfile', () => {
    it('should set selectedSchema to a schema definition', () => {
      useSchemaStore.getState().setSelectedSchema(mockSchema);

      expect(useSchemaStore.getState().selectedSchema).toEqual(mockSchema);
    });

    it('should clear selectedSchema when set to null', () => {
      useSchemaStore.getState().setSelectedSchema(mockSchema);
      expect(useSchemaStore.getState().selectedSchema).not.toBeNull();

      useSchemaStore.getState().setSelectedSchema(null);

      expect(useSchemaStore.getState().selectedSchema).toBeNull();
    });

    it('should replace selectedSchema with a different schema', () => {
      useSchemaStore.getState().setSelectedSchema(mockSchema);
      expect(useSchemaStore.getState().selectedSchema?.schema_id).toBe(
        'schema-001'
      );

      useSchemaStore.getState().setSelectedSchema(mockSchema2);

      expect(useSchemaStore.getState().selectedSchema?.schema_id).toBe(
        'schema-002'
      );
    });

    it('should set selectedProfile to a statistical profile', () => {
      useSchemaStore.getState().setSelectedProfile(mockProfile);

      expect(useSchemaStore.getState().selectedProfile).toEqual(mockProfile);
    });

    it('should clear selectedProfile when set to null', () => {
      useSchemaStore.getState().setSelectedProfile(mockProfile);
      expect(useSchemaStore.getState().selectedProfile).not.toBeNull();

      useSchemaStore.getState().setSelectedProfile(null);

      expect(useSchemaStore.getState().selectedProfile).toBeNull();
    });

    it('should replace selectedProfile with a different profile', () => {
      useSchemaStore.getState().setSelectedProfile(mockProfile);
      expect(useSchemaStore.getState().selectedProfile?.profile_id).toBe(
        'profile-001'
      );

      useSchemaStore.getState().setSelectedProfile(mockProfile2);

      expect(useSchemaStore.getState().selectedProfile?.profile_id).toBe(
        'profile-002'
      );
    });

    it('should not affect schemas list when setting selectedSchema', () => {
      mockGetSchemas.mockResolvedValue(mockPaginatedSchemas);
      useSchemaStore.getState().fetchSchemas();

      useSchemaStore.getState().setSelectedSchema(mockSchema);

      // Schemas list should be independent of selectedSchema
      expect(useSchemaStore.getState().selectedSchema).toEqual(mockSchema);
    });

    it('should not affect profiles list when setting selectedProfile', () => {
      mockGetProfiles.mockResolvedValue(mockPaginatedProfiles);
      useSchemaStore.getState().fetchProfiles();

      useSchemaStore.getState().setSelectedProfile(mockProfile);

      // Profiles list should be independent of selectedProfile
      expect(useSchemaStore.getState().selectedProfile).toEqual(mockProfile);
    });
  });

  // ==========================================================================
  // 12. Error Clearing (clearSchemaErrors / clearProfileErrors)
  // ==========================================================================

  describe('clearSchemaErrors and clearProfileErrors', () => {
    it('should clear all schema-related error states', async () => {
      // Set multiple schema errors through different operations
      mockGetSchemas.mockRejectedValue(new Error('List error'));
      await useSchemaStore.getState().fetchSchemas();

      mockGetSchemaById.mockRejectedValue(new Error('Detail error'));
      await useSchemaStore.getState().fetchSchemaById('id');

      mockDiscoverSchema.mockRejectedValue(new Error('Discovery error'));
      await useSchemaStore.getState().discoverSchema(mockDiscoveryRequest);

      // Verify all errors are set
      const preState = useSchemaStore.getState();
      expect(preState.schemasError).toBe('List error');
      expect(preState.schemaDetailError).toBe('Detail error');
      expect(preState.discoveryError).toBe('Discovery error');

      // Clear all schema errors
      useSchemaStore.getState().clearSchemaErrors();

      // Verify all schema errors are cleared
      const postState = useSchemaStore.getState();
      expect(postState.schemasError).toBeNull();
      expect(postState.schemaDetailError).toBeNull();
      expect(postState.discoveryError).toBeNull();
    });

    it('should not affect profile errors when clearing schema errors', async () => {
      // Set both schema and profile errors
      mockGetSchemas.mockRejectedValue(new Error('Schema error'));
      await useSchemaStore.getState().fetchSchemas();

      mockGetProfiles.mockRejectedValue(new Error('Profile list error'));
      await useSchemaStore.getState().fetchProfiles();

      // Clear only schema errors
      useSchemaStore.getState().clearSchemaErrors();

      // Profile errors should remain
      expect(useSchemaStore.getState().schemasError).toBeNull();
      expect(useSchemaStore.getState().profilesError).toBe(
        'Profile list error'
      );
    });

    it('should clear all profile-related error states', async () => {
      // Set multiple profile errors through different operations
      mockGetProfiles.mockRejectedValue(new Error('Profile list error'));
      await useSchemaStore.getState().fetchProfiles();

      mockGetProfileById.mockRejectedValue(new Error('Profile detail error'));
      await useSchemaStore.getState().fetchProfileById('id');

      mockCreateProfile.mockRejectedValue(new Error('Profiling error'));
      await useSchemaStore.getState().createProfile(mockProfileRequest);

      // Verify all errors are set
      const preState = useSchemaStore.getState();
      expect(preState.profilesError).toBe('Profile list error');
      expect(preState.profileDetailError).toBe('Profile detail error');
      expect(preState.profilingError).toBe('Profiling error');

      // Clear all profile errors
      useSchemaStore.getState().clearProfileErrors();

      // Verify all profile errors are cleared
      const postState = useSchemaStore.getState();
      expect(postState.profilesError).toBeNull();
      expect(postState.profileDetailError).toBeNull();
      expect(postState.profilingError).toBeNull();
    });

    it('should not affect schema errors when clearing profile errors', async () => {
      // Set both schema and profile errors
      mockGetSchemas.mockRejectedValue(new Error('Schema list error'));
      await useSchemaStore.getState().fetchSchemas();

      mockGetProfiles.mockRejectedValue(new Error('Profile error'));
      await useSchemaStore.getState().fetchProfiles();

      // Clear only profile errors
      useSchemaStore.getState().clearProfileErrors();

      // Schema errors should remain
      expect(useSchemaStore.getState().profilesError).toBeNull();
      expect(useSchemaStore.getState().schemasError).toBe('Schema list error');
    });

    it('should be safe to call clearSchemaErrors when no errors exist', () => {
      // All errors start as null
      expect(useSchemaStore.getState().schemasError).toBeNull();

      useSchemaStore.getState().clearSchemaErrors();

      // Should remain null without throwing
      expect(useSchemaStore.getState().schemasError).toBeNull();
      expect(useSchemaStore.getState().schemaDetailError).toBeNull();
      expect(useSchemaStore.getState().discoveryError).toBeNull();
    });

    it('should be safe to call clearProfileErrors when no errors exist', () => {
      // All errors start as null
      expect(useSchemaStore.getState().profilesError).toBeNull();

      useSchemaStore.getState().clearProfileErrors();

      // Should remain null without throwing
      expect(useSchemaStore.getState().profilesError).toBeNull();
      expect(useSchemaStore.getState().profileDetailError).toBeNull();
      expect(useSchemaStore.getState().profilingError).toBeNull();
    });
  });

  // ==========================================================================
  // 13. Reset
  // ==========================================================================

  describe('reset', () => {
    it('should reset all schema data state to initial values', async () => {
      // Populate schemas
      mockGetSchemas.mockResolvedValue(mockPaginatedSchemas);
      await useSchemaStore.getState().fetchSchemas();
      useSchemaStore.getState().setSelectedSchema(mockSchema);

      // Verify populated
      expect(useSchemaStore.getState().schemas).toHaveLength(2);
      expect(useSchemaStore.getState().selectedSchema).not.toBeNull();
      expect(useSchemaStore.getState().schemasTotal).toBe(2);

      // Reset
      useSchemaStore.getState().reset();

      // Verify reset
      const state = useSchemaStore.getState();
      expect(state.schemas).toEqual([]);
      expect(state.selectedSchema).toBeNull();
      expect(state.schemasTotal).toBe(0);
      expect(state.schemasPage).toBe(1);
      expect(state.schemasPageSize).toBe(20);
    });

    it('should reset all profile data state to initial values', async () => {
      // Populate profiles
      mockGetProfiles.mockResolvedValue(mockPaginatedProfiles);
      await useSchemaStore.getState().fetchProfiles();
      useSchemaStore.getState().setSelectedProfile(mockProfile);

      // Verify populated
      expect(useSchemaStore.getState().profiles).toHaveLength(2);
      expect(useSchemaStore.getState().selectedProfile).not.toBeNull();
      expect(useSchemaStore.getState().profilesTotal).toBe(2);

      // Reset
      useSchemaStore.getState().reset();

      // Verify reset
      const state = useSchemaStore.getState();
      expect(state.profiles).toEqual([]);
      expect(state.selectedProfile).toBeNull();
      expect(state.profilesTotal).toBe(0);
    });

    it('should reset all loading states to false', () => {
      useSchemaStore.getState().reset();

      const state = useSchemaStore.getState();
      expect(state.isSchemasLoading).toBe(false);
      expect(state.isSchemaDetailLoading).toBe(false);
      expect(state.isDiscovering).toBe(false);
      expect(state.isProfilesLoading).toBe(false);
      expect(state.isProfileDetailLoading).toBe(false);
      expect(state.isProfiling).toBe(false);
    });

    it('should reset all error states to null', async () => {
      // Set various errors
      mockGetSchemas.mockRejectedValue(new Error('Error 1'));
      await useSchemaStore.getState().fetchSchemas();

      mockGetProfileById.mockRejectedValue(new Error('Error 2'));
      await useSchemaStore.getState().fetchProfileById('id');

      mockDiscoverSchema.mockRejectedValue(new Error('Error 3'));
      await useSchemaStore.getState().discoverSchema(mockDiscoveryRequest);

      // Verify errors exist
      expect(useSchemaStore.getState().schemasError).not.toBeNull();
      expect(useSchemaStore.getState().profileDetailError).not.toBeNull();
      expect(useSchemaStore.getState().discoveryError).not.toBeNull();

      // Reset
      useSchemaStore.getState().reset();

      // Verify all errors cleared
      const state = useSchemaStore.getState();
      expect(state.schemasError).toBeNull();
      expect(state.schemaDetailError).toBeNull();
      expect(state.discoveryError).toBeNull();
      expect(state.profilesError).toBeNull();
      expect(state.profileDetailError).toBeNull();
      expect(state.profilingError).toBeNull();
    });

    it('should preserve action functions after reset', () => {
      useSchemaStore.getState().reset();

      const state = useSchemaStore.getState();
      expect(typeof state.fetchSchemas).toBe('function');
      expect(typeof state.fetchSchemaById).toBe('function');
      expect(typeof state.discoverSchema).toBe('function');
      expect(typeof state.deleteSchema).toBe('function');
      expect(typeof state.refreshSchema).toBe('function');
      expect(typeof state.fetchProfiles).toBe('function');
      expect(typeof state.fetchProfileById).toBe('function');
      expect(typeof state.createProfile).toBe('function');
      expect(typeof state.deleteProfile).toBe('function');
      expect(typeof state.setSelectedSchema).toBe('function');
      expect(typeof state.setSelectedProfile).toBe('function');
      expect(typeof state.clearSchemaErrors).toBe('function');
      expect(typeof state.clearProfileErrors).toBe('function');
      expect(typeof state.reset).toBe('function');
    });

    it('should allow normal operations after reset', async () => {
      // Reset then perform operations to verify actions still work
      useSchemaStore.getState().reset();

      mockGetSchemas.mockResolvedValue(mockPaginatedSchemas);
      await useSchemaStore.getState().fetchSchemas();

      expect(useSchemaStore.getState().schemas).toHaveLength(2);
      expect(useSchemaStore.getState().schemasTotal).toBe(2);
    });

    it('should be callable multiple times without errors', () => {
      useSchemaStore.getState().reset();
      useSchemaStore.getState().reset();
      useSchemaStore.getState().reset();

      const state = useSchemaStore.getState();
      expect(state.schemas).toEqual([]);
      expect(state.profiles).toEqual([]);
    });
  });
});
