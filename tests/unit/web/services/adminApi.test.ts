/**
 * @fileoverview Comprehensive Vitest unit test suite for the adminApi service module.
 *
 * Tests all 14 admin and tenant management API functions exported by
 * src/web/src/services/adminApi.ts, organized into three operational domains:
 *
 * 1. **User Management** — getUsers, getUserById, createUser, updateUser, deactivateUser
 * 2. **Tenant Management** — getTenants, getTenantById, createTenant, updateTenantConfig,
 *    getTenantResourceUsage
 * 3. **System Administration** — getAuditLogs, getSystemSettings, updateSystemSettings,
 *    getSystemHealth
 *
 * All tests mock the shared Axios apiClient from api.ts to isolate HTTP
 * communication and verify URL construction (/api/v1/admin/*), HTTP methods
 * (GET, POST, PUT, DELETE), request payloads, query parameter serialization,
 * and typed response handling without making real HTTP requests.
 *
 * Covers requirements:
 * - Admin and tenant management API endpoints (S-009 Admin Panel)
 * - Five user roles with Platform Admin having full system access (RBAC)
 * - Multi-tenant architecture with namespace isolation and resource quotas (R-007)
 * - Audit logging with tamper-evident audit trails and 7-year retention
 * - R-014: Comprehensive testing for core business logic
 *
 * @module tests/unit/web/services/adminApi.test
 */

import { describe, it, expect, vi, beforeEach } from 'vitest';

// ---------------------------------------------------------------------------
// Module Mock — apiClient from @/services/api
// ---------------------------------------------------------------------------
// vi.mock is hoisted to the top of the file by Vitest's transform.
// Provides controlled mock functions for get, post, put, delete methods
// so all adminApi functions can be tested without real HTTP requests.

vi.mock('@/services/api', () => ({
  apiClient: {
    get: vi.fn(),
    post: vi.fn(),
    put: vi.fn(),
    delete: vi.fn(),
  },
}));

// ---------------------------------------------------------------------------
// Imports — Mocked Module
// ---------------------------------------------------------------------------

import { apiClient } from '@/services/api';

// ---------------------------------------------------------------------------
// Imports — Module Under Test (14 API functions + local type definitions)
// ---------------------------------------------------------------------------

import {
  getUsers,
  getUserById,
  createUser,
  updateUser,
  deactivateUser,
  getTenants,
  getTenantById,
  createTenant,
  updateTenantConfig,
  getTenantResourceUsage,
  getAuditLogs,
  getSystemSettings,
  updateSystemSettings,
  getSystemHealth,
} from '@/services/adminApi';

import type {
  Tenant,
  AuditLogEntry,
  SystemSettings,
  SystemHealthStatus,
  ResourceQuota,
  ResourceUsage,
  CreateUserRequest,
  UpdateUserRequest,
  CreateTenantRequest,
  UpdateTenantConfigRequest,
  AuditLogFilterParams,
} from '@/services/adminApi';

// ---------------------------------------------------------------------------
// Imports — Shared Type Definitions
// ---------------------------------------------------------------------------

import type { ApiResponse, PaginatedResult } from '@/types/api';
import { type User, Role, Permission } from '@/types/auth';

// ============================================================================
// Test Data Factories
// ============================================================================
// Reusable mock objects typed to their respective interfaces. Use consistent
// test IDs (user-123, tenant-456, log-789) across all test suites.

/** Mock resource quota allocation used by tenant fixtures. */
const mockResourceQuota: ResourceQuota = {
  max_concurrent_jobs: 10,
  max_records_per_job: 1_000_000,
  storage_limit_gb: 100,
  api_rate_limit: 300,
};

/** Mock user fixture with DATA_ENGINEER role and full permission set. */
const mockUser: User = {
  user_id: 'user-123',
  email: 'test@example.com',
  name: 'Test User',
  role: Role.DATA_ENGINEER,
  permissions: [
    Permission.GENERATION_CREATE,
    Permission.GENERATION_READ,
    Permission.GENERATION_DELETE,
    Permission.PROFILE_CREATE,
    Permission.PROFILE_READ,
    Permission.SCHEMA_DISCOVER,
    Permission.SCHEMA_READ,
    Permission.TEMPLATE_CREATE,
    Permission.TEMPLATE_READ,
    Permission.TEMPLATE_DELETE,
    Permission.EXPORT_CREATE,
    Permission.EXPORT_READ,
    Permission.COMPLIANCE_READ,
    Permission.QUALITY_READ,
  ],
  tenant_id: 'tenant-456',
  avatar_url: null,
  is_active: true,
  last_login: '2025-01-10T08:00:00Z',
  created_at: '2025-01-01T00:00:00Z',
};

/** Mock tenant fixture with standard resource quota and settings. */
const mockTenant: Tenant = {
  tenant_id: 'tenant-456',
  name: 'Test Tenant',
  namespace: 'test-tenant',
  resource_quota: mockResourceQuota,
  settings: { default_export_format: 'parquet' },
  is_active: true,
  created_at: '2025-01-01T00:00:00Z',
  user_count: 5,
};

/** Mock audit log entry for SOC 2 Type II compliance tests. */
const mockAuditLogEntry: AuditLogEntry = {
  log_id: 'log-789',
  user_id: 'user-123',
  user_email: 'admin@example.com',
  action: 'user.created',
  resource_type: 'user',
  resource_id: 'user-456',
  details: { role: 'data_engineer' },
  ip_address: '192.168.1.1',
  timestamp: '2025-01-15T10:30:00Z',
  tenant_id: 'tenant-456',
};

/** Mock system settings with default generation and retention config. */
const mockSystemSettings: SystemSettings = {
  default_batch_size: 10000,
  max_batch_size: 100000,
  default_quality_threshold: 0.95,
  retention_days: 2555,
  maintenance_mode: false,
};

/**
 * Mock system health status with all six microservices reporting.
 * Includes one degraded service (compliance_service) for coverage
 * of non-healthy states and error detail assertions.
 */
const mockSystemHealthStatus: SystemHealthStatus = {
  overall: 'healthy',
  services: [
    {
      name: 'api_gateway',
      status: 'healthy',
      latency_ms: 12,
      last_check: '2025-01-15T10:30:00Z',
    },
    {
      name: 'generation_engine',
      status: 'healthy',
      latency_ms: 25,
      last_check: '2025-01-15T10:30:00Z',
    },
    {
      name: 'profiling_service',
      status: 'healthy',
      latency_ms: 18,
      last_check: '2025-01-15T10:30:00Z',
    },
    {
      name: 'quality_service',
      status: 'healthy',
      latency_ms: 15,
      last_check: '2025-01-15T10:30:00Z',
    },
    {
      name: 'compliance_service',
      status: 'degraded',
      latency_ms: 150,
      last_check: '2025-01-15T10:30:00Z',
      error: 'spaCy model loading delayed',
    },
    {
      name: 'provisioning_service',
      status: 'healthy',
      latency_ms: 20,
      last_check: '2025-01-15T10:30:00Z',
    },
  ],
};

/** Mock resource usage statistics for tenant capacity monitoring. */
const mockResourceUsage: ResourceUsage = {
  active_jobs: 3,
  total_records_generated: 500000,
  storage_used_gb: 45.2,
  api_calls_today: 1250,
  quota: mockResourceQuota,
};

// ============================================================================
// Global Test Lifecycle
// ============================================================================

/**
 * Reset all mock function call history and implementations before each test
 * to ensure complete test isolation. This prevents mock state leakage between
 * test cases within and across describe blocks.
 */
beforeEach(() => {
  vi.clearAllMocks();
});

// ============================================================================
// Test Suites — 14 describe blocks covering all admin API functions
// ============================================================================

// ---------------------------------------------------------------------------
// 1. User Management: getUsers
// ---------------------------------------------------------------------------
describe('getUsers', () => {
  it('should call GET /api/v1/admin/users with default params', async () => {
    const mockResponse: ApiResponse<PaginatedResult<User>> = {
      success: true,
      data: {
        items: [mockUser],
        total: 1,
        page: 1,
        page_size: 20,
        total_pages: 1,
      },
      timestamp: '2025-01-15T10:00:00Z',
    };

    vi.mocked(apiClient.get).mockResolvedValue(mockResponse);

    const result = await getUsers({ page: 1, page_size: 20 });

    expect(apiClient.get).toHaveBeenCalledWith(
      '/api/v1/admin/users',
      {
        params: {
          page: 1,
          page_size: 20,
          sort_by: undefined,
          sort_direction: undefined,
        },
      },
    );
    expect(result).toEqual(mockResponse);
  });

  it('should pass pagination and filter params as query params', async () => {
    vi.mocked(apiClient.get).mockResolvedValue({
      success: true,
      data: { items: [], total: 0, page: 2, page_size: 25, total_pages: 0 },
      timestamp: '2025-01-15T10:00:00Z',
    });

    await getUsers(
      { page: 2, page_size: 25, sort_by: 'created_at', sort_direction: 'desc' },
      { search: 'john', status: 'active', role: 'data_engineer' } as Record<string, string>,
    );

    expect(apiClient.get).toHaveBeenCalledWith(
      '/api/v1/admin/users',
      {
        params: {
          page: 2,
          page_size: 25,
          sort_by: 'created_at',
          sort_direction: 'desc',
          search: 'john',
          status: 'active',
          role: 'data_engineer',
        },
      },
    );
  });

  it('should return paginated user list', async () => {
    const secondUser: User = {
      ...mockUser,
      user_id: 'user-456',
      email: 'other@example.com',
      name: 'Other User',
    };

    const paginatedResponse: ApiResponse<PaginatedResult<User>> = {
      success: true,
      data: {
        items: [mockUser, secondUser],
        total: 2,
        page: 1,
        page_size: 20,
        total_pages: 1,
      },
      timestamp: '2025-01-15T10:00:00Z',
    };

    vi.mocked(apiClient.get).mockResolvedValue(paginatedResponse);

    const result = await getUsers({ page: 1, page_size: 20 });

    expect(result.success).toBe(true);
    expect(result.data.items).toHaveLength(2);
    expect(result.data.total).toBe(2);
    expect(result.data.page).toBe(1);
    expect(result.data.items[0].user_id).toBe('user-123');
    expect(result.data.items[1].user_id).toBe('user-456');
  });

  it('should handle error responses', async () => {
    const error = new Error('Network Error');
    vi.mocked(apiClient.get).mockRejectedValue(error);

    await expect(
      getUsers({ page: 1, page_size: 20 }),
    ).rejects.toThrow('Network Error');
  });
});

// ---------------------------------------------------------------------------
// 2. User Management: getUserById
// ---------------------------------------------------------------------------
describe('getUserById', () => {
  it('should call GET /api/v1/admin/users/{userId}', async () => {
    vi.mocked(apiClient.get).mockResolvedValue({
      success: true,
      data: mockUser,
      timestamp: '2025-01-15T10:00:00Z',
    });

    await getUserById('user-123');

    expect(apiClient.get).toHaveBeenCalledWith('/api/v1/admin/users/user-123');
  });

  it('should return user details', async () => {
    const mockResponse: ApiResponse<User> = {
      success: true,
      data: mockUser,
      timestamp: '2025-01-15T10:00:00Z',
    };

    vi.mocked(apiClient.get).mockResolvedValue(mockResponse);

    const result = await getUserById('user-123');

    expect(result.success).toBe(true);
    expect(result.data.user_id).toBe('user-123');
    expect(result.data.email).toBe('test@example.com');
    expect(result.data.name).toBe('Test User');
    expect(result.data.role).toBe(Role.DATA_ENGINEER);
    expect(result.data.tenant_id).toBe('tenant-456');
    expect(result.data.is_active).toBe(true);
    expect(result.data.permissions).toContain(Permission.GENERATION_CREATE);
  });

  it('should encode special characters in userId', async () => {
    vi.mocked(apiClient.get).mockResolvedValue({
      success: true,
      data: mockUser,
      timestamp: '2025-01-15T10:00:00Z',
    });

    await getUserById('auth0|abc123');

    // adminApi.ts uses encodeURIComponent for userId interpolation
    expect(apiClient.get).toHaveBeenCalledWith(
      `/api/v1/admin/users/${encodeURIComponent('auth0|abc123')}`,
    );
  });
});

// ---------------------------------------------------------------------------
// 3. User Management: createUser
// ---------------------------------------------------------------------------
describe('createUser', () => {
  it('should call POST /api/v1/admin/users with user data', async () => {
    const createRequest: CreateUserRequest = {
      email: 'new@test.com',
      name: 'New User',
      role: Role.DEVELOPER,
      tenant_id: 'tenant-1',
      is_active: true,
    };

    vi.mocked(apiClient.post).mockResolvedValue({
      success: true,
      data: {
        ...mockUser,
        user_id: 'user-new-1',
        email: 'new@test.com',
        name: 'New User',
        role: Role.DEVELOPER,
        tenant_id: 'tenant-1',
      },
      timestamp: '2025-01-15T10:00:00Z',
    });

    await createUser(createRequest);

    expect(apiClient.post).toHaveBeenCalledWith(
      '/api/v1/admin/users',
      createRequest,
    );
  });

  it('should return created user', async () => {
    const createRequest: CreateUserRequest = {
      email: 'new@test.com',
      name: 'New User',
      role: Role.DEVELOPER,
      tenant_id: 'tenant-1',
      is_active: true,
    };

    const createdUser: User = {
      user_id: 'user-new-1',
      email: 'new@test.com',
      name: 'New User',
      role: Role.DEVELOPER,
      permissions: [
        Permission.GENERATION_CREATE,
        Permission.GENERATION_READ,
        Permission.PROFILE_READ,
        Permission.SCHEMA_READ,
        Permission.TEMPLATE_READ,
        Permission.EXPORT_CREATE,
        Permission.EXPORT_READ,
      ],
      tenant_id: 'tenant-1',
      is_active: true,
      created_at: '2025-01-15T10:00:00Z',
    };

    const mockResponse: ApiResponse<User> = {
      success: true,
      data: createdUser,
      timestamp: '2025-01-15T10:00:00Z',
    };

    vi.mocked(apiClient.post).mockResolvedValue(mockResponse);

    const result = await createUser(createRequest);

    expect(result.success).toBe(true);
    expect(result.data.user_id).toBe('user-new-1');
    expect(result.data.email).toBe('new@test.com');
    expect(result.data.role).toBe(Role.DEVELOPER);
    expect(result.data.tenant_id).toBe('tenant-1');
  });
});

// ---------------------------------------------------------------------------
// 4. User Management: updateUser
// ---------------------------------------------------------------------------
describe('updateUser', () => {
  it('should call PUT /api/v1/admin/users/{userId} with update data', async () => {
    const updateRequest: UpdateUserRequest = {
      name: 'Updated Name',
      role: Role.DATA_ENGINEER,
    };

    vi.mocked(apiClient.put).mockResolvedValue({
      success: true,
      data: { ...mockUser, name: 'Updated Name' },
      timestamp: '2025-01-15T10:00:00Z',
    });

    await updateUser('user-123', updateRequest);

    expect(apiClient.put).toHaveBeenCalledWith(
      '/api/v1/admin/users/user-123',
      updateRequest,
    );
  });

  it('should return updated user', async () => {
    const updateRequest: UpdateUserRequest = {
      name: 'Updated Name',
      role: Role.DATA_ENGINEER,
    };

    const updatedUser: User = {
      ...mockUser,
      name: 'Updated Name',
      role: Role.DATA_ENGINEER,
    };

    const mockResponse: ApiResponse<User> = {
      success: true,
      data: updatedUser,
      timestamp: '2025-01-15T10:00:00Z',
    };

    vi.mocked(apiClient.put).mockResolvedValue(mockResponse);

    const result = await updateUser('user-123', updateRequest);

    expect(result.success).toBe(true);
    expect(result.data.name).toBe('Updated Name');
    expect(result.data.role).toBe(Role.DATA_ENGINEER);
    expect(result.data.user_id).toBe('user-123');
  });

  it('should encode special characters in userId for update', async () => {
    vi.mocked(apiClient.put).mockResolvedValue({
      success: true,
      data: mockUser,
      timestamp: '2025-01-15T10:00:00Z',
    });

    await updateUser('auth0|xyz789', { name: 'New Name' });

    expect(apiClient.put).toHaveBeenCalledWith(
      `/api/v1/admin/users/${encodeURIComponent('auth0|xyz789')}`,
      { name: 'New Name' },
    );
  });
});

// ---------------------------------------------------------------------------
// 5. User Management: deactivateUser
// ---------------------------------------------------------------------------
describe('deactivateUser', () => {
  it('should call DELETE /api/v1/admin/users/{userId}', async () => {
    vi.mocked(apiClient.delete).mockResolvedValue({
      success: true,
      data: { ...mockUser, is_active: false },
      timestamp: '2025-01-15T10:00:00Z',
    });

    await deactivateUser('user-123');

    expect(apiClient.delete).toHaveBeenCalledWith(
      '/api/v1/admin/users/user-123',
    );
  });

  it('should handle successful deactivation', async () => {
    const deactivatedUser: User = {
      ...mockUser,
      is_active: false,
    };

    const mockResponse: ApiResponse<User> = {
      success: true,
      data: deactivatedUser,
      timestamp: '2025-01-15T10:00:00Z',
    };

    vi.mocked(apiClient.delete).mockResolvedValue(mockResponse);

    const result = await deactivateUser('user-123');

    expect(result.success).toBe(true);
    expect(result.data.is_active).toBe(false);
    expect(result.data.user_id).toBe('user-123');
  });

  it('should encode special characters in userId for deactivation', async () => {
    vi.mocked(apiClient.delete).mockResolvedValue({
      success: true,
      data: { ...mockUser, is_active: false },
      timestamp: '2025-01-15T10:00:00Z',
    });

    await deactivateUser('auth0|user456');

    expect(apiClient.delete).toHaveBeenCalledWith(
      `/api/v1/admin/users/${encodeURIComponent('auth0|user456')}`,
    );
  });
});

// ---------------------------------------------------------------------------
// 6. Tenant Management: getTenants
// ---------------------------------------------------------------------------
describe('getTenants', () => {
  it('should call GET /api/v1/admin/tenants', async () => {
    vi.mocked(apiClient.get).mockResolvedValue({
      success: true,
      data: {
        items: [mockTenant],
        total: 1,
        page: 1,
        page_size: 20,
        total_pages: 1,
      },
      timestamp: '2025-01-15T10:00:00Z',
    });

    await getTenants({ page: 1, page_size: 20 });

    expect(apiClient.get).toHaveBeenCalledWith(
      '/api/v1/admin/tenants',
      {
        params: {
          page: 1,
          page_size: 20,
          sort_by: undefined,
          sort_direction: undefined,
        },
      },
    );
  });

  it('should pass pagination params', async () => {
    vi.mocked(apiClient.get).mockResolvedValue({
      success: true,
      data: { items: [], total: 0, page: 1, page_size: 20, total_pages: 0 },
      timestamp: '2025-01-15T10:00:00Z',
    });

    await getTenants(
      { page: 1, page_size: 20, sort_by: 'name', sort_direction: 'asc' },
      { search: 'production', status: 'active' },
    );

    expect(apiClient.get).toHaveBeenCalledWith(
      '/api/v1/admin/tenants',
      {
        params: {
          page: 1,
          page_size: 20,
          sort_by: 'name',
          sort_direction: 'asc',
          search: 'production',
          status: 'active',
        },
      },
    );
  });

  it('should return paginated tenant list', async () => {
    const paginatedResponse: ApiResponse<PaginatedResult<Tenant>> = {
      success: true,
      data: {
        items: [mockTenant],
        total: 1,
        page: 1,
        page_size: 20,
        total_pages: 1,
      },
      timestamp: '2025-01-15T10:00:00Z',
    };

    vi.mocked(apiClient.get).mockResolvedValue(paginatedResponse);

    const result = await getTenants({ page: 1, page_size: 20 });

    expect(result.success).toBe(true);
    expect(result.data.items).toHaveLength(1);
    expect(result.data.items[0].tenant_id).toBe('tenant-456');
    expect(result.data.items[0].namespace).toBe('test-tenant');
    expect(result.data.items[0].resource_quota.max_concurrent_jobs).toBe(10);
    expect(result.data.items[0].user_count).toBe(5);
  });
});

// ---------------------------------------------------------------------------
// 7. Tenant Management: getTenantById
// ---------------------------------------------------------------------------
describe('getTenantById', () => {
  it('should call GET /api/v1/admin/tenants/{tenantId}', async () => {
    vi.mocked(apiClient.get).mockResolvedValue({
      success: true,
      data: mockTenant,
      timestamp: '2025-01-15T10:00:00Z',
    });

    await getTenantById('tenant-456');

    expect(apiClient.get).toHaveBeenCalledWith(
      '/api/v1/admin/tenants/tenant-456',
    );
  });

  it('should return tenant details with resource quota', async () => {
    const mockResponse: ApiResponse<Tenant> = {
      success: true,
      data: mockTenant,
      timestamp: '2025-01-15T10:00:00Z',
    };

    vi.mocked(apiClient.get).mockResolvedValue(mockResponse);

    const result = await getTenantById('tenant-456');

    expect(result.success).toBe(true);
    expect(result.data.tenant_id).toBe('tenant-456');
    expect(result.data.name).toBe('Test Tenant');
    expect(result.data.namespace).toBe('test-tenant');
    expect(result.data.is_active).toBe(true);
    expect(result.data.user_count).toBe(5);
    expect(result.data.resource_quota).toEqual(mockResourceQuota);
  });
});

// ---------------------------------------------------------------------------
// 8. Tenant Management: createTenant
// ---------------------------------------------------------------------------
describe('createTenant', () => {
  it('should call POST /api/v1/admin/tenants with tenant data', async () => {
    const createRequest: CreateTenantRequest = {
      name: 'New Tenant',
      namespace: 'new-tenant',
      resource_quota: {
        max_concurrent_jobs: 10,
        max_records_per_job: 1_000_000,
        storage_limit_gb: 100,
        api_rate_limit: 300,
      },
      settings: { default_export_format: 'csv' },
    };

    vi.mocked(apiClient.post).mockResolvedValue({
      success: true,
      data: {
        tenant_id: 'tenant-new-1',
        ...createRequest,
        is_active: true,
        created_at: '2025-01-15T10:00:00Z',
        user_count: 0,
      },
      timestamp: '2025-01-15T10:00:00Z',
    });

    await createTenant(createRequest);

    expect(apiClient.post).toHaveBeenCalledWith(
      '/api/v1/admin/tenants',
      createRequest,
    );
  });

  it('should return created tenant', async () => {
    const createRequest: CreateTenantRequest = {
      name: 'New Tenant',
      namespace: 'new-tenant',
      resource_quota: mockResourceQuota,
      settings: {},
    };

    const createdTenant: Tenant = {
      tenant_id: 'tenant-new-1',
      name: 'New Tenant',
      namespace: 'new-tenant',
      resource_quota: mockResourceQuota,
      settings: {},
      is_active: true,
      created_at: '2025-01-15T10:00:00Z',
      user_count: 0,
    };

    const mockResponse: ApiResponse<Tenant> = {
      success: true,
      data: createdTenant,
      timestamp: '2025-01-15T10:00:00Z',
    };

    vi.mocked(apiClient.post).mockResolvedValue(mockResponse);

    const result = await createTenant(createRequest);

    expect(result.success).toBe(true);
    expect(result.data.tenant_id).toBe('tenant-new-1');
    expect(result.data.name).toBe('New Tenant');
    expect(result.data.namespace).toBe('new-tenant');
    expect(result.data.resource_quota.max_concurrent_jobs).toBe(10);
    expect(result.data.resource_quota.max_records_per_job).toBe(1_000_000);
    expect(result.data.resource_quota.storage_limit_gb).toBe(100);
    expect(result.data.resource_quota.api_rate_limit).toBe(300);
    expect(result.data.user_count).toBe(0);
  });
});

// ---------------------------------------------------------------------------
// 9. Tenant Management: updateTenantConfig
// ---------------------------------------------------------------------------
describe('updateTenantConfig', () => {
  it('should call PUT /api/v1/admin/tenants/{tenantId} with config', async () => {
    const updateRequest: UpdateTenantConfigRequest = {
      resource_quota: {
        max_concurrent_jobs: 20,
        storage_limit_gb: 200,
      },
      settings: { default_export_format: 'parquet' },
      is_active: true,
    };

    vi.mocked(apiClient.put).mockResolvedValue({
      success: true,
      data: mockTenant,
      timestamp: '2025-01-15T10:00:00Z',
    });

    await updateTenantConfig('tenant-456', updateRequest);

    expect(apiClient.put).toHaveBeenCalledWith(
      '/api/v1/admin/tenants/tenant-456',
      updateRequest,
    );
  });

  it('should return updated tenant', async () => {
    const updateRequest: UpdateTenantConfigRequest = {
      resource_quota: {
        max_concurrent_jobs: 20,
      },
    };

    const updatedTenant: Tenant = {
      ...mockTenant,
      resource_quota: {
        ...mockResourceQuota,
        max_concurrent_jobs: 20,
      },
    };

    const mockResponse: ApiResponse<Tenant> = {
      success: true,
      data: updatedTenant,
      timestamp: '2025-01-15T10:00:00Z',
    };

    vi.mocked(apiClient.put).mockResolvedValue(mockResponse);

    const result = await updateTenantConfig('tenant-456', updateRequest);

    expect(result.success).toBe(true);
    expect(result.data.resource_quota.max_concurrent_jobs).toBe(20);
    expect(result.data.resource_quota.storage_limit_gb).toBe(100); // unchanged
    expect(result.data.tenant_id).toBe('tenant-456');
  });

  it('should encode special characters in tenantId', async () => {
    vi.mocked(apiClient.put).mockResolvedValue({
      success: true,
      data: mockTenant,
      timestamp: '2025-01-15T10:00:00Z',
    });

    await updateTenantConfig('tenant/special&chars', { name: 'Updated' });

    expect(apiClient.put).toHaveBeenCalledWith(
      `/api/v1/admin/tenants/${encodeURIComponent('tenant/special&chars')}`,
      { name: 'Updated' },
    );
  });
});

// ---------------------------------------------------------------------------
// 10. Tenant Management: getTenantResourceUsage
// ---------------------------------------------------------------------------
describe('getTenantResourceUsage', () => {
  it('should call GET /api/v1/admin/tenants/{tenantId}/usage', async () => {
    vi.mocked(apiClient.get).mockResolvedValue({
      success: true,
      data: mockResourceUsage,
      timestamp: '2025-01-15T10:00:00Z',
    });

    await getTenantResourceUsage('tenant-456');

    expect(apiClient.get).toHaveBeenCalledWith(
      '/api/v1/admin/tenants/tenant-456/usage',
    );
  });

  it('should return resource usage with quota comparison', async () => {
    const mockResponse: ApiResponse<ResourceUsage> = {
      success: true,
      data: mockResourceUsage,
      timestamp: '2025-01-15T10:00:00Z',
    };

    vi.mocked(apiClient.get).mockResolvedValue(mockResponse);

    const result = await getTenantResourceUsage('tenant-456');

    expect(result.success).toBe(true);
    expect(result.data.active_jobs).toBe(3);
    expect(result.data.total_records_generated).toBe(500000);
    expect(result.data.storage_used_gb).toBe(45.2);
    expect(result.data.api_calls_today).toBe(1250);
    expect(result.data.quota).toEqual(mockResourceQuota);
    expect(result.data.quota.max_concurrent_jobs).toBe(10);
  });
});

// ---------------------------------------------------------------------------
// 11. System Administration: getAuditLogs
// ---------------------------------------------------------------------------
describe('getAuditLogs', () => {
  it('should call GET /api/v1/admin/audit-logs with filter params', async () => {
    vi.mocked(apiClient.get).mockResolvedValue({
      success: true,
      data: {
        items: [mockAuditLogEntry],
        total: 1,
        page: 1,
        page_size: 50,
        total_pages: 1,
      },
      timestamp: '2025-01-15T10:00:00Z',
    });

    const filters: AuditLogFilterParams = {
      user_id: 'user-123',
      action: 'generation.create',
      resource_type: 'generation_job',
      date_from: '2025-01-01T00:00:00Z',
      date_to: '2025-01-31T23:59:59Z',
    };

    await getAuditLogs(
      { page: 1, page_size: 50, sort_by: 'timestamp', sort_direction: 'desc' },
      filters,
    );

    expect(apiClient.get).toHaveBeenCalledWith(
      '/api/v1/admin/audit-logs',
      {
        params: {
          page: 1,
          page_size: 50,
          sort_by: 'timestamp',
          sort_direction: 'desc',
          user_id: 'user-123',
          action: 'generation.create',
          resource_type: 'generation_job',
          date_from: '2025-01-01T00:00:00Z',
          date_to: '2025-01-31T23:59:59Z',
        },
      },
    );
  });

  it('should return paginated audit log entries', async () => {
    const secondEntry: AuditLogEntry = {
      ...mockAuditLogEntry,
      log_id: 'log-790',
      action: 'tenant.updated',
      resource_type: 'tenant',
      resource_id: 'tenant-456',
      timestamp: '2025-01-15T11:00:00Z',
    };

    const paginatedResponse: ApiResponse<PaginatedResult<AuditLogEntry>> = {
      success: true,
      data: {
        items: [mockAuditLogEntry, secondEntry],
        total: 2,
        page: 1,
        page_size: 50,
        total_pages: 1,
      },
      timestamp: '2025-01-15T11:30:00Z',
    };

    vi.mocked(apiClient.get).mockResolvedValue(paginatedResponse);

    const result = await getAuditLogs({ page: 1, page_size: 50 });

    expect(result.success).toBe(true);
    expect(result.data.items).toHaveLength(2);
    expect(result.data.total).toBe(2);
    expect(result.data.items[0].log_id).toBe('log-789');
    expect(result.data.items[0].action).toBe('user.created');
    expect(result.data.items[0].user_email).toBe('admin@example.com');
    expect(result.data.items[0].timestamp).toBe('2025-01-15T10:30:00Z');
    expect(result.data.items[1].action).toBe('tenant.updated');
  });

  it('should call with default params when no filters provided', async () => {
    vi.mocked(apiClient.get).mockResolvedValue({
      success: true,
      data: { items: [], total: 0, page: 1, page_size: 50, total_pages: 0 },
      timestamp: '2025-01-15T10:00:00Z',
    });

    await getAuditLogs({ page: 1, page_size: 50 });

    expect(apiClient.get).toHaveBeenCalledWith(
      '/api/v1/admin/audit-logs',
      {
        params: {
          page: 1,
          page_size: 50,
          sort_by: undefined,
          sort_direction: undefined,
        },
      },
    );
  });
});

// ---------------------------------------------------------------------------
// 12. System Administration: getSystemSettings
// ---------------------------------------------------------------------------
describe('getSystemSettings', () => {
  it('should call GET /api/v1/admin/settings', async () => {
    vi.mocked(apiClient.get).mockResolvedValue({
      success: true,
      data: mockSystemSettings,
      timestamp: '2025-01-15T10:00:00Z',
    });

    await getSystemSettings();

    expect(apiClient.get).toHaveBeenCalledWith('/api/v1/admin/settings');
  });

  it('should return system settings', async () => {
    const mockResponse: ApiResponse<SystemSettings> = {
      success: true,
      data: mockSystemSettings,
      timestamp: '2025-01-15T10:00:00Z',
    };

    vi.mocked(apiClient.get).mockResolvedValue(mockResponse);

    const result = await getSystemSettings();

    expect(result.success).toBe(true);
    expect(result.data.default_batch_size).toBe(10000);
    expect(result.data.max_batch_size).toBe(100000);
    expect(result.data.default_quality_threshold).toBe(0.95);
    expect(result.data.retention_days).toBe(2555);
    expect(result.data.maintenance_mode).toBe(false);
  });
});

// ---------------------------------------------------------------------------
// 13. System Administration: updateSystemSettings
// ---------------------------------------------------------------------------
describe('updateSystemSettings', () => {
  it('should call PUT /api/v1/admin/settings with partial settings', async () => {
    const partialUpdate: Partial<SystemSettings> = {
      default_batch_size: 20000,
      maintenance_mode: true,
    };

    vi.mocked(apiClient.put).mockResolvedValue({
      success: true,
      data: { ...mockSystemSettings, ...partialUpdate },
      timestamp: '2025-01-15T10:00:00Z',
    });

    await updateSystemSettings(partialUpdate);

    expect(apiClient.put).toHaveBeenCalledWith(
      '/api/v1/admin/settings',
      partialUpdate,
    );
  });

  it('should return updated settings', async () => {
    const partialUpdate: Partial<SystemSettings> = {
      default_batch_size: 20000,
      maintenance_mode: true,
    };

    const updatedSettings: SystemSettings = {
      ...mockSystemSettings,
      default_batch_size: 20000,
      maintenance_mode: true,
    };

    const mockResponse: ApiResponse<SystemSettings> = {
      success: true,
      data: updatedSettings,
      timestamp: '2025-01-15T10:00:00Z',
    };

    vi.mocked(apiClient.put).mockResolvedValue(mockResponse);

    const result = await updateSystemSettings(partialUpdate);

    expect(result.success).toBe(true);
    expect(result.data.default_batch_size).toBe(20000);
    expect(result.data.maintenance_mode).toBe(true);
    // Verify unchanged fields remain intact
    expect(result.data.max_batch_size).toBe(100000);
    expect(result.data.default_quality_threshold).toBe(0.95);
    expect(result.data.retention_days).toBe(2555);
  });
});

// ---------------------------------------------------------------------------
// 14. System Administration: getSystemHealth
// ---------------------------------------------------------------------------
describe('getSystemHealth', () => {
  it('should call GET /api/v1/admin/health', async () => {
    vi.mocked(apiClient.get).mockResolvedValue({
      success: true,
      data: mockSystemHealthStatus,
      timestamp: '2025-01-15T10:00:00Z',
    });

    await getSystemHealth();

    expect(apiClient.get).toHaveBeenCalledWith('/api/v1/admin/health');
  });

  it('should return system health status with per-service details', async () => {
    const mockResponse: ApiResponse<SystemHealthStatus> = {
      success: true,
      data: mockSystemHealthStatus,
      timestamp: '2025-01-15T10:00:00Z',
    };

    vi.mocked(apiClient.get).mockResolvedValue(mockResponse);

    const result = await getSystemHealth();

    expect(result.success).toBe(true);
    expect(result.data.overall).toBe('healthy');
    expect(result.data.services).toHaveLength(6);

    // Verify API Gateway service health entry
    const apiGw = result.data.services.find(
      (s: { name: string }) => s.name === 'api_gateway',
    );
    expect(apiGw).toBeDefined();
    expect(apiGw!.status).toBe('healthy');
    expect(apiGw!.latency_ms).toBe(12);
    expect(apiGw!.last_check).toBe('2025-01-15T10:30:00Z');

    // Verify Generation Engine service health entry
    const genEngine = result.data.services.find(
      (s: { name: string }) => s.name === 'generation_engine',
    );
    expect(genEngine).toBeDefined();
    expect(genEngine!.status).toBe('healthy');
    expect(genEngine!.latency_ms).toBe(25);

    // Verify Profiling Service health entry
    const profiling = result.data.services.find(
      (s: { name: string }) => s.name === 'profiling_service',
    );
    expect(profiling).toBeDefined();
    expect(profiling!.status).toBe('healthy');

    // Verify Quality Service health entry
    const quality = result.data.services.find(
      (s: { name: string }) => s.name === 'quality_service',
    );
    expect(quality).toBeDefined();
    expect(quality!.status).toBe('healthy');

    // Verify degraded Compliance Service has error details
    const compliance = result.data.services.find(
      (s: { name: string }) => s.name === 'compliance_service',
    );
    expect(compliance).toBeDefined();
    expect(compliance!.status).toBe('degraded');
    expect(compliance!.latency_ms).toBe(150);
    expect(compliance!.error).toBe('spaCy model loading delayed');

    // Verify Provisioning Service health entry
    const provisioning = result.data.services.find(
      (s: { name: string }) => s.name === 'provisioning_service',
    );
    expect(provisioning).toBeDefined();
    expect(provisioning!.status).toBe('healthy');
    expect(provisioning!.latency_ms).toBe(20);
  });
});
