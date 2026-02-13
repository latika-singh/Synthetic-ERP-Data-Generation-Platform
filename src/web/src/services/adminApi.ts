/**
 * @fileoverview Admin and tenant management API service for the Web Console.
 *
 * Provides fully typed API functions for Platform Admin role endpoints,
 * covering three operational domains:
 *
 * 1. **User Management** — CRUD operations for user accounts, role assignments,
 *    and account activation/deactivation across all five RBAC roles.
 * 2. **Tenant Management** — Multi-tenant namespace creation, configuration,
 *    resource quota management, and usage monitoring.
 * 3. **System Administration** — Platform-wide settings, audit log retrieval
 *    with tamper-evident trail support, and system health monitoring.
 *
 * All functions import the shared Axios client from `api.ts`, which provides
 * automatic JWT injection (R-006), multi-tenant X-Tenant-ID headers (R-007),
 * X-Request-ID correlation (OpenTelemetry), 401 token refresh, and structured
 * error transformation.
 *
 * All endpoints are scoped under `/api/v1/admin` following URL-path API
 * versioning (R-012). Access is restricted to the Platform Admin role.
 *
 * Consumed by: AdminPanel page (S-009), user/tenant management modals,
 * system settings panels, and audit log viewers.
 *
 * @module services/adminApi
 * @version 1.0.0
 */

import { apiClient } from './api';
import type {
  ApiResponse,
  PaginatedResult,
  PaginationParams,
  FilterParams,
} from '@/types/api';
import { type User, type Role } from '@/types/auth';

// ============================================================================
// Constants
// ============================================================================

/**
 * Base URL path for all admin API endpoints.
 * Follows URL-path versioning per R-012.
 */
const ADMIN_BASE_PATH = '/api/v1/admin';

// ============================================================================
// Tenant & Resource Interfaces
// ============================================================================

/**
 * Tenant record representing an isolated namespace within the platform.
 *
 * Each tenant has its own namespace for Kubernetes resource isolation,
 * resource quotas to prevent noisy-neighbor effects, and configurable
 * settings for tenant-specific behavior. Maps to the `tenant_configurations`
 * MongoDB collection on the backend.
 */
export interface Tenant {
  /** Unique tenant identifier (UUID v4) */
  readonly tenant_id: string;

  /** Human-readable tenant display name */
  readonly name: string;

  /** Kubernetes namespace for multi-tenant isolation (R-007) */
  readonly namespace: string;

  /** Resource quota limits assigned to this tenant */
  readonly resource_quota: ResourceQuota;

  /** Tenant-specific configuration settings */
  readonly settings: Record<string, unknown>;

  /** Whether the tenant is currently active; inactive tenants are suspended */
  readonly is_active: boolean;

  /** ISO 8601 timestamp of when the tenant was created */
  readonly created_at: string;

  /** Total number of users associated with this tenant */
  readonly user_count: number;
}

/**
 * Resource quota limits for a tenant namespace.
 *
 * Enforced by the API Gateway rate limiter and Generation Engine batch
 * processor to prevent resource exhaustion by any single tenant.
 */
export interface ResourceQuota {
  /** Maximum number of generation jobs that can run concurrently */
  readonly max_concurrent_jobs: number;

  /** Maximum number of records per individual generation job */
  readonly max_records_per_job: number;

  /** Storage capacity limit in gigabytes for generated artifacts */
  readonly storage_limit_gb: number;

  /** Maximum API requests per minute (tiered: 60/300/1000) */
  readonly api_rate_limit: number;
}

/**
 * Current resource usage statistics for a tenant.
 *
 * Provides real-time consumption data against the tenant's
 * {@link ResourceQuota} allocation, enabling capacity monitoring
 * and quota enforcement in the Admin Panel (S-009).
 */
export interface ResourceUsage {
  /** Number of generation jobs currently running */
  readonly active_jobs: number;

  /** Total count of records generated across all completed jobs */
  readonly total_records_generated: number;

  /** Total storage consumed in gigabytes */
  readonly storage_used_gb: number;

  /** Number of API calls made by this tenant today (UTC) */
  readonly api_calls_today: number;

  /** The tenant's assigned resource quota for comparison */
  readonly quota: ResourceQuota;
}

// ============================================================================
// System Settings & Health Interfaces
// ============================================================================

/**
 * Platform-wide system configuration settings.
 *
 * Controls global defaults for generation batch processing, quality
 * thresholds, data retention, and maintenance mode. Modifiable only
 * by Platform Admin users via the Admin Panel (S-009).
 */
export interface SystemSettings {
  /** Default batch size for generation jobs (records per batch) */
  readonly default_batch_size: number;

  /** Maximum allowable batch size to prevent OOM conditions */
  readonly max_batch_size: number;

  /** Default quality threshold (0.0–1.0) for the ≥95% fidelity target */
  readonly default_quality_threshold: number;

  /** Data retention period in days for generated artifacts (audit: 2555 days / 7 years) */
  readonly retention_days: number;

  /** Whether the platform is in maintenance mode (read-only, no new jobs) */
  readonly maintenance_mode: boolean;
}

/**
 * Individual service health status entry within the system health report.
 *
 * Each microservice (API Gateway, Generation Engine, Profiling Service,
 * Quality Service, Compliance Service, Provisioning Service) reports its
 * own health status, latency, and optional error details.
 */
interface ServiceHealthEntry {
  /** Service name identifier (e.g., 'api_gateway', 'generation_engine') */
  readonly name: string;

  /** Current health status of the individual service */
  readonly status: 'healthy' | 'degraded' | 'unhealthy';

  /** Response latency of the health check probe in milliseconds */
  readonly latency_ms: number;

  /** ISO 8601 timestamp of the last successful health check */
  readonly last_check: string;

  /** Optional error message if service is degraded or unhealthy */
  readonly error?: string | null;
}

/**
 * Aggregated system health status across all platform microservices.
 *
 * The `overall` status is derived from the individual service statuses:
 * - `'healthy'`   — All services are operational
 * - `'degraded'`  — One or more services report degraded performance
 * - `'unhealthy'` — One or more critical services are down
 */
export interface SystemHealthStatus {
  /** Overall platform health status */
  readonly overall: 'healthy' | 'degraded' | 'unhealthy';

  /** Health status of each individual microservice */
  readonly services: ServiceHealthEntry[];
}

// ============================================================================
// Audit Log Interface
// ============================================================================

/**
 * Tamper-evident audit log entry for SOC 2 Type II compliance.
 *
 * Every significant user action is recorded as an immutable audit entry
 * in the `audit_logs` MongoDB collection with SHA-256 checksums.
 * Audit logs are retained for 7 years (2555 days) per compliance requirements.
 */
export interface AuditLogEntry {
  /** Unique audit log entry identifier */
  readonly log_id: string;

  /** Auth0 user ID of the actor who performed the action */
  readonly user_id: string;

  /** Email address of the actor for human-readable identification */
  readonly user_email: string;

  /** Action performed (e.g., 'user.created', 'job.submitted', 'tenant.updated') */
  readonly action: string;

  /** Type of resource acted upon (e.g., 'user', 'tenant', 'generation_job', 'setting') */
  readonly resource_type: string;

  /** Identifier of the specific resource acted upon */
  readonly resource_id: string;

  /** Additional context and before/after state for the action */
  readonly details: Record<string, unknown>;

  /** IP address of the client that initiated the action */
  readonly ip_address: string;

  /** ISO 8601 timestamp of when the action occurred */
  readonly timestamp: string;

  /** Tenant namespace under which the action was performed */
  readonly tenant_id: string;
}

// ============================================================================
// Request Body Interfaces
// ============================================================================

/**
 * Request body for creating a new user account.
 *
 * Used with {@link createUser} to provision new platform users.
 * The role field is typed as {@link Role} to restrict values to
 * the five valid RBAC roles.
 */
export interface CreateUserRequest {
  /** User email address (must be unique within the tenant) */
  readonly email: string;

  /** Human-readable display name */
  readonly name: string;

  /** RBAC role to assign (platform_admin, data_engineer, developer, qa_engineer, data_analyst) */
  readonly role: Role;

  /** Tenant namespace the user should belong to */
  readonly tenant_id: string;

  /** Whether the account should be immediately active */
  readonly is_active: boolean;
}

/**
 * Request body for updating an existing user account.
 *
 * Used with {@link updateUser} to modify user properties.
 * All fields are optional to support partial updates.
 */
export interface UpdateUserRequest {
  /** Updated display name */
  readonly name?: string;

  /** Updated RBAC role assignment */
  readonly role?: Role;

  /** Updated active status */
  readonly is_active?: boolean;

  /** Updated tenant namespace assignment */
  readonly tenant_id?: string;
}

/**
 * Request body for creating a new tenant namespace.
 *
 * Used with {@link createTenant} to provision isolated tenant environments.
 * The namespace is used for Kubernetes resource isolation (R-007).
 */
export interface CreateTenantRequest {
  /** Human-readable tenant name */
  readonly name: string;

  /** Kubernetes namespace identifier (lowercase, alphanumeric, hyphens) */
  readonly namespace: string;

  /** Initial resource quota allocation for the tenant */
  readonly resource_quota: ResourceQuota;

  /** Initial tenant-specific configuration settings */
  readonly settings: Record<string, unknown>;
}

/**
 * Request body for updating an existing tenant's configuration.
 *
 * Used with {@link updateTenantConfig} to modify tenant settings.
 * All fields are optional to support partial updates.
 */
export interface UpdateTenantConfigRequest {
  /** Updated tenant display name */
  readonly name?: string;

  /** Updated resource quota limits */
  readonly resource_quota?: Partial<ResourceQuota>;

  /** Updated tenant-specific settings */
  readonly settings?: Record<string, unknown>;

  /** Updated active status (set false to suspend tenant) */
  readonly is_active?: boolean;
}

// ============================================================================
// Audit Log Filter Parameters
// ============================================================================

/**
 * Specialized filter parameters for querying audit log entries.
 *
 * Extends the base filtering concept with audit-specific fields.
 * Used with {@link getAuditLogs} to narrow audit log queries by
 * actor, action type, resource, and time range.
 */
export interface AuditLogFilterParams {
  /** Filter by the Auth0 user ID of the actor */
  readonly user_id?: string;

  /** Filter by the action type (e.g., 'user.created', 'job.submitted') */
  readonly action?: string;

  /** Filter by resource type (e.g., 'user', 'tenant', 'generation_job') */
  readonly resource_type?: string;

  /** Filter for entries on or after this ISO 8601 date */
  readonly date_from?: string;

  /** Filter for entries on or before this ISO 8601 date */
  readonly date_to?: string;
}

// ============================================================================
// User Management API Functions
// ============================================================================

/**
 * Retrieves a paginated list of users across all tenants.
 *
 * Supports pagination, sorting, and filtering by search text, status,
 * tenant ID, and date range. Restricted to Platform Admin role.
 *
 * @param pagination - Pagination parameters (page, page_size, sort_by, sort_direction)
 * @param filters - Optional filter criteria to narrow results
 * @returns Paginated list of user profiles
 *
 * @example
 * ```typescript
 * const response = await getUsers(
 *   { page: 1, page_size: 20, sort_by: 'created_at', sort_direction: 'desc' },
 *   { search: 'john', status: 'active', tenant_id: 'tenant-abc' }
 * );
 * console.log(`Found ${response.data.total} users`);
 * response.data.items.forEach(user => console.log(user.email));
 * ```
 */
export async function getUsers(
  pagination: PaginationParams,
  filters?: FilterParams,
): Promise<ApiResponse<PaginatedResult<User>>> {
  const params: Record<string, string | number | boolean | undefined> = {
    page: pagination.page,
    page_size: pagination.page_size,
    sort_by: pagination.sort_by,
    sort_direction: pagination.sort_direction,
    ...filters,
  };

  return apiClient.get<ApiResponse<PaginatedResult<User>>>(
    `${ADMIN_BASE_PATH}/users`,
    { params },
  );
}

/**
 * Retrieves a single user profile by their unique identifier.
 *
 * @param userId - The Auth0 user identifier (e.g., 'auth0|abc123')
 * @returns The full user profile including role, permissions, and tenant context
 *
 * @example
 * ```typescript
 * const response = await getUserById('auth0|abc123');
 * console.log(`${response.data.name} (${response.data.role})`);
 * ```
 */
export async function getUserById(
  userId: string,
): Promise<ApiResponse<User>> {
  return apiClient.get<ApiResponse<User>>(
    `${ADMIN_BASE_PATH}/users/${encodeURIComponent(userId)}`,
  );
}

/**
 * Creates a new user account with the specified role and tenant assignment.
 *
 * Triggers an Auth0 invitation flow for the user to complete account setup.
 * The created user is logged in the audit trail for SOC 2 compliance.
 *
 * @param data - User creation payload with email, name, role, tenant, and status
 * @returns The newly created user profile
 *
 * @example
 * ```typescript
 * const response = await createUser({
 *   email: 'jane.doe@company.com',
 *   name: 'Jane Doe',
 *   role: Role.DATA_ENGINEER,
 *   tenant_id: 'tenant-abc',
 *   is_active: true,
 * });
 * console.log(`Created user: ${response.data.user_id}`);
 * ```
 */
export async function createUser(
  data: CreateUserRequest,
): Promise<ApiResponse<User>> {
  return apiClient.post<ApiResponse<User>>(
    `${ADMIN_BASE_PATH}/users`,
    data,
  );
}

/**
 * Updates an existing user's profile, role, or tenant assignment.
 *
 * Supports partial updates — only provided fields are modified.
 * Role changes are immediately reflected in the user's JWT on next refresh.
 * All modifications are recorded in the audit trail.
 *
 * @param userId - The Auth0 user identifier to update
 * @param data - Partial update payload with fields to modify
 * @returns The updated user profile
 *
 * @example
 * ```typescript
 * const response = await updateUser('auth0|abc123', {
 *   role: Role.PLATFORM_ADMIN,
 *   is_active: true,
 * });
 * console.log(`Updated role to: ${response.data.role}`);
 * ```
 */
export async function updateUser(
  userId: string,
  data: UpdateUserRequest,
): Promise<ApiResponse<User>> {
  return apiClient.put<ApiResponse<User>>(
    `${ADMIN_BASE_PATH}/users/${encodeURIComponent(userId)}`,
    data,
  );
}

/**
 * Deactivates a user account, preventing further authentication.
 *
 * Performs a soft-delete: the user record is retained for audit purposes
 * but marked as inactive. Active sessions are invalidated. This action
 * is recorded in the audit trail and is reversible via {@link updateUser}.
 *
 * @param userId - The Auth0 user identifier to deactivate
 * @returns Confirmation with the deactivated user's final state
 *
 * @example
 * ```typescript
 * const response = await deactivateUser('auth0|abc123');
 * console.log(`Deactivated: ${!response.data.is_active}`);
 * ```
 */
export async function deactivateUser(
  userId: string,
): Promise<ApiResponse<User>> {
  return apiClient.delete<ApiResponse<User>>(
    `${ADMIN_BASE_PATH}/users/${encodeURIComponent(userId)}`,
  );
}

// ============================================================================
// Tenant Management API Functions
// ============================================================================

/**
 * Retrieves a paginated list of all tenants in the platform.
 *
 * Supports pagination, sorting, and filtering by name, active status,
 * and creation date. Used by the Admin Panel (S-009) for tenant overview.
 *
 * @param pagination - Pagination parameters (page, page_size, sort_by, sort_direction)
 * @param filters - Optional filter criteria to narrow results
 * @returns Paginated list of tenant records with resource quota details
 *
 * @example
 * ```typescript
 * const response = await getTenants(
 *   { page: 1, page_size: 10, sort_by: 'name', sort_direction: 'asc' },
 *   { search: 'production', status: 'active' }
 * );
 * response.data.items.forEach(t => console.log(`${t.name}: ${t.user_count} users`));
 * ```
 */
export async function getTenants(
  pagination: PaginationParams,
  filters?: FilterParams,
): Promise<ApiResponse<PaginatedResult<Tenant>>> {
  const params: Record<string, string | number | boolean | undefined> = {
    page: pagination.page,
    page_size: pagination.page_size,
    sort_by: pagination.sort_by,
    sort_direction: pagination.sort_direction,
    ...filters,
  };

  return apiClient.get<ApiResponse<PaginatedResult<Tenant>>>(
    `${ADMIN_BASE_PATH}/tenants`,
    { params },
  );
}

/**
 * Retrieves a single tenant by its unique identifier.
 *
 * @param tenantId - The tenant UUID to retrieve
 * @returns The full tenant record with configuration and quota details
 *
 * @example
 * ```typescript
 * const response = await getTenantById('tenant-abc-123');
 * console.log(`Namespace: ${response.data.namespace}`);
 * console.log(`Quota: ${response.data.resource_quota.max_concurrent_jobs} concurrent jobs`);
 * ```
 */
export async function getTenantById(
  tenantId: string,
): Promise<ApiResponse<Tenant>> {
  return apiClient.get<ApiResponse<Tenant>>(
    `${ADMIN_BASE_PATH}/tenants/${encodeURIComponent(tenantId)}`,
  );
}

/**
 * Creates a new tenant with the specified namespace, quota, and settings.
 *
 * Provisions a new isolated namespace in Kubernetes (R-007), creates the
 * tenant record in MongoDB, and applies the specified resource quota.
 * Recorded in the audit trail for SOC 2 compliance.
 *
 * @param data - Tenant creation payload with name, namespace, quota, and settings
 * @returns The newly created tenant record
 *
 * @example
 * ```typescript
 * const response = await createTenant({
 *   name: 'Production Team',
 *   namespace: 'prod-team',
 *   resource_quota: {
 *     max_concurrent_jobs: 10,
 *     max_records_per_job: 1000000,
 *     storage_limit_gb: 100,
 *     api_rate_limit: 1000,
 *   },
 *   settings: { default_export_format: 'parquet' },
 * });
 * console.log(`Created tenant: ${response.data.tenant_id}`);
 * ```
 */
export async function createTenant(
  data: CreateTenantRequest,
): Promise<ApiResponse<Tenant>> {
  return apiClient.post<ApiResponse<Tenant>>(
    `${ADMIN_BASE_PATH}/tenants`,
    data,
  );
}

/**
 * Updates an existing tenant's configuration, quota, or active status.
 *
 * Supports partial updates — only provided fields are modified.
 * Quota changes take effect immediately for new jobs. Setting
 * `is_active` to false suspends the tenant's operations.
 * All modifications are recorded in the audit trail.
 *
 * @param tenantId - The tenant UUID to update
 * @param data - Partial update payload with fields to modify
 * @returns The updated tenant record
 *
 * @example
 * ```typescript
 * const response = await updateTenantConfig('tenant-abc-123', {
 *   resource_quota: {
 *     max_concurrent_jobs: 20,
 *     storage_limit_gb: 200,
 *   },
 *   is_active: true,
 * });
 * console.log(`Updated quota: ${response.data.resource_quota.max_concurrent_jobs} jobs`);
 * ```
 */
export async function updateTenantConfig(
  tenantId: string,
  data: UpdateTenantConfigRequest,
): Promise<ApiResponse<Tenant>> {
  return apiClient.put<ApiResponse<Tenant>>(
    `${ADMIN_BASE_PATH}/tenants/${encodeURIComponent(tenantId)}`,
    data,
  );
}

/**
 * Retrieves the current resource usage statistics for a tenant.
 *
 * Returns real-time consumption data compared against the tenant's
 * {@link ResourceQuota} allocation, enabling capacity planning and
 * quota enforcement monitoring in the Admin Panel (S-009).
 *
 * @param tenantId - The tenant UUID to query usage for
 * @returns Current resource usage with quota for comparison
 *
 * @example
 * ```typescript
 * const response = await getTenantResourceUsage('tenant-abc-123');
 * const usage = response.data;
 * const storagePercent = (usage.storage_used_gb / usage.quota.storage_limit_gb) * 100;
 * console.log(`Storage: ${storagePercent.toFixed(1)}% used`);
 * ```
 */
export async function getTenantResourceUsage(
  tenantId: string,
): Promise<ApiResponse<ResourceUsage>> {
  return apiClient.get<ApiResponse<ResourceUsage>>(
    `${ADMIN_BASE_PATH}/tenants/${encodeURIComponent(tenantId)}/usage`,
  );
}

// ============================================================================
// System Administration API Functions
// ============================================================================

/**
 * Retrieves the current platform-wide system settings.
 *
 * Returns global configuration values including batch sizes, quality
 * thresholds, retention periods, and maintenance mode status.
 *
 * @returns Current system settings
 *
 * @example
 * ```typescript
 * const response = await getSystemSettings();
 * console.log(`Quality threshold: ${response.data.default_quality_threshold}`);
 * console.log(`Maintenance mode: ${response.data.maintenance_mode ? 'ON' : 'OFF'}`);
 * ```
 */
export async function getSystemSettings(): Promise<ApiResponse<SystemSettings>> {
  return apiClient.get<ApiResponse<SystemSettings>>(
    `${ADMIN_BASE_PATH}/settings`,
  );
}

/**
 * Updates platform-wide system settings.
 *
 * Supports partial updates — only provided fields are modified.
 * Setting `maintenance_mode` to true prevents new generation jobs
 * from being submitted. All changes are recorded in the audit trail.
 *
 * @param data - Partial settings object with fields to update
 * @returns The updated system settings reflecting all applied changes
 *
 * @example
 * ```typescript
 * const response = await updateSystemSettings({
 *   default_batch_size: 20000,
 *   maintenance_mode: true,
 * });
 * console.log(`Batch size updated to: ${response.data.default_batch_size}`);
 * ```
 */
export async function updateSystemSettings(
  data: Partial<SystemSettings>,
): Promise<ApiResponse<SystemSettings>> {
  return apiClient.put<ApiResponse<SystemSettings>>(
    `${ADMIN_BASE_PATH}/settings`,
    data,
  );
}

/**
 * Retrieves a paginated list of audit log entries.
 *
 * Supports filtering by actor, action type, resource type, and time range.
 * Audit logs are tamper-evident with SHA-256 checksums and retained for
 * 7 years (2555 days) per SOC 2 Type II compliance requirements.
 *
 * @param pagination - Pagination parameters (page, page_size, sort_by, sort_direction)
 * @param filters - Optional audit-specific filter criteria
 * @returns Paginated list of audit log entries
 *
 * @example
 * ```typescript
 * const response = await getAuditLogs(
 *   { page: 1, page_size: 50, sort_by: 'timestamp', sort_direction: 'desc' },
 *   { action: 'user.created', date_from: '2025-01-01T00:00:00Z' }
 * );
 * response.data.items.forEach(entry =>
 *   console.log(`[${entry.timestamp}] ${entry.user_email}: ${entry.action}`)
 * );
 * ```
 */
export async function getAuditLogs(
  pagination: PaginationParams,
  filters?: AuditLogFilterParams,
): Promise<ApiResponse<PaginatedResult<AuditLogEntry>>> {
  const params: Record<string, string | number | boolean | undefined> = {
    page: pagination.page,
    page_size: pagination.page_size,
    sort_by: pagination.sort_by,
    sort_direction: pagination.sort_direction,
    ...filters,
  };

  return apiClient.get<ApiResponse<PaginatedResult<AuditLogEntry>>>(
    `${ADMIN_BASE_PATH}/audit-logs`,
    { params },
  );
}

/**
 * Retrieves the current system health status across all microservices.
 *
 * Performs a real-time health check against all platform services
 * (API Gateway, Generation Engine, Profiling Service, Quality Service,
 * Compliance Service, Provisioning Service) and their dependencies
 * (MongoDB, Redis). Returns an aggregate status and per-service details.
 *
 * @returns System health status with overall assessment and per-service breakdown
 *
 * @example
 * ```typescript
 * const response = await getSystemHealth();
 * console.log(`System: ${response.data.overall}`);
 * response.data.services.forEach(svc =>
 *   console.log(`  ${svc.name}: ${svc.status} (${svc.latency_ms}ms)`)
 * );
 * ```
 */
export async function getSystemHealth(): Promise<ApiResponse<SystemHealthStatus>> {
  return apiClient.get<ApiResponse<SystemHealthStatus>>(
    `${ADMIN_BASE_PATH}/health`,
  );
}
