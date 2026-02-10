/**
 * Authentication and Authorization Type Definitions
 *
 * Defines the complete TypeScript type system for the authentication and
 * role-based access control (RBAC) domain used across the Web Console.
 * Aligns with backend Pydantic models in src/backend/api_gateway/schemas/auth.py.
 *
 * Consumed by: authStore (Zustand), useAuth hook, usePermissions hook,
 * Login page, AdminPanel, Header, Sidebar, and route guards.
 *
 * @module types/auth
 */

// ---------------------------------------------------------------------------
// Enums
// ---------------------------------------------------------------------------

/**
 * Graduated user roles for role-based access control (RBAC).
 *
 * Roles follow a graduated permission model where higher-privilege roles
 * include broader access. Matches the backend `UserRole` enum exactly.
 *
 * @enum {string}
 */
export enum Role {
  /** Full system access — manages users, tenants, and system configuration. */
  PLATFORM_ADMIN = 'platform_admin',

  /** Generation, profiling, schemas, templates, export, quality, and compliance read access. */
  DATA_ENGINEER = 'data_engineer',

  /** Generation create/read, basic schema and template read access, export access. */
  DEVELOPER = 'developer',

  /** Read-only access to generation jobs, quality reports, compliance status, and templates. */
  QA_ENGINEER = 'qa_engineer',

  /** Read-only access to generation jobs, profiles, schemas, and quality reports. */
  DATA_ANALYST = 'data_analyst',
}

/**
 * Fine-grained RBAC permissions following the `domain:action` convention.
 *
 * Each permission controls access to a specific operation within a domain.
 * Permissions are assigned to roles via the {@link ROLE_PERMISSIONS} mapping.
 *
 * @enum {string}
 */
export enum Permission {
  /** Create new generation jobs. */
  GENERATION_CREATE = 'generation:create',

  /** View generation jobs and their results. */
  GENERATION_READ = 'generation:read',

  /** Delete generation jobs and their output artifacts. */
  GENERATION_DELETE = 'generation:delete',

  /** Initiate statistical profiling of ERP data sources. */
  PROFILE_CREATE = 'profile:create',

  /** View statistical profiles and distribution data. */
  PROFILE_READ = 'profile:read',

  /** Trigger ERP schema discovery via connectors. */
  SCHEMA_DISCOVER = 'schema:discover',

  /** View discovered ERP schema definitions. */
  SCHEMA_READ = 'schema:read',

  /** Create and edit generation templates. */
  TEMPLATE_CREATE = 'template:create',

  /** View generation templates in the template library. */
  TEMPLATE_READ = 'template:read',

  /** Delete generation templates. */
  TEMPLATE_DELETE = 'template:delete',

  /** Initiate data export or database provisioning. */
  EXPORT_CREATE = 'export:create',

  /** View export history and provisioning status. */
  EXPORT_READ = 'export:read',

  /** Manage user accounts (create, update, deactivate). */
  ADMIN_USERS = 'admin:users',

  /** Manage tenant configurations and namespace isolation. */
  ADMIN_TENANTS = 'admin:tenants',

  /** Modify system-wide settings and platform configuration. */
  ADMIN_SYSTEM = 'admin:system',

  /** View compliance scan results and certification history. */
  COMPLIANCE_READ = 'compliance:read',

  /** Manage compliance rules, trigger re-scans, and issue certifications. */
  COMPLIANCE_MANAGE = 'compliance:manage',

  /** View quality score reports and validation results. */
  QUALITY_READ = 'quality:read',
}

// ---------------------------------------------------------------------------
// Role ↔ Permission Mapping
// ---------------------------------------------------------------------------

/**
 * Authoritative mapping of each {@link Role} to its allowed {@link Permission} set.
 *
 * This constant is the single source of truth for determining what actions a
 * given role can perform. It is consumed by the `usePermissions` hook and
 * route-guard utilities to enforce RBAC across the Web Console.
 *
 * Permission assignments follow the graduated model specified in the
 * technical specification:
 * - **PLATFORM_ADMIN** — unrestricted; receives every defined permission.
 * - **DATA_ENGINEER** — broad operational access without admin privileges.
 * - **DEVELOPER** — create/read generation jobs with limited schema and template access.
 * - **QA_ENGINEER** — read-only validation and compliance oversight.
 * - **DATA_ANALYST** — read-only analytical access to profiles and quality data.
 */
export const ROLE_PERMISSIONS: Readonly<Record<Role, readonly Permission[]>> = {
  [Role.PLATFORM_ADMIN]: [
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
    Permission.ADMIN_USERS,
    Permission.ADMIN_TENANTS,
    Permission.ADMIN_SYSTEM,
    Permission.COMPLIANCE_READ,
    Permission.COMPLIANCE_MANAGE,
    Permission.QUALITY_READ,
  ],

  [Role.DATA_ENGINEER]: [
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

  [Role.DEVELOPER]: [
    Permission.GENERATION_CREATE,
    Permission.GENERATION_READ,
    Permission.PROFILE_READ,
    Permission.SCHEMA_READ,
    Permission.TEMPLATE_READ,
    Permission.EXPORT_CREATE,
    Permission.EXPORT_READ,
  ],

  [Role.QA_ENGINEER]: [
    Permission.GENERATION_READ,
    Permission.TEMPLATE_READ,
    Permission.COMPLIANCE_READ,
    Permission.QUALITY_READ,
  ],

  [Role.DATA_ANALYST]: [
    Permission.GENERATION_READ,
    Permission.PROFILE_READ,
    Permission.SCHEMA_READ,
    Permission.QUALITY_READ,
  ],
} as const;

// ---------------------------------------------------------------------------
// Interfaces
// ---------------------------------------------------------------------------

/**
 * Authenticated user profile returned by the API Gateway.
 *
 * Maps to the backend `UserProfile` Pydantic model. Represents a fully
 * resolved user record including role-derived permissions and multi-tenant
 * context.
 */
export interface User {
  /** Unique user identifier sourced from Auth0 (e.g., `auth0|abc123`). */
  readonly user_id: string;

  /** User email address (unique per tenant). */
  readonly email: string;

  /** Human-readable display name. */
  readonly name: string;

  /** Assigned RBAC role governing permission scope. */
  readonly role: Role;

  /** Effective permissions derived from the assigned {@link Role}. */
  readonly permissions: Permission[];

  /** Tenant namespace the user belongs to for multi-tenant isolation. */
  readonly tenant_id: string;

  /** Optional URL to the user's profile picture (from Auth0 or uploaded). */
  readonly avatar_url?: string | null;

  /** Whether the user account is currently active. Inactive accounts cannot authenticate. */
  readonly is_active: boolean;

  /** ISO 8601 timestamp of the user's most recent successful login, or `null` if never logged in. */
  readonly last_login?: string | null;

  /** ISO 8601 timestamp of when the user account was created. */
  readonly created_at: string;
}

/**
 * Decoded JWT token payload containing Auth0 claims.
 *
 * Represents the decoded content of an RS256-signed JWT issued by Auth0.
 * Custom claims (`role`, `tenant_id`, `permissions`) are injected via
 * Auth0 Actions / Rules.
 */
export interface TokenPayload {
  /** Auth0 subject identifier (unique user key). */
  readonly sub: string;

  /** User email address claim. */
  readonly email: string;

  /** User display name claim. */
  readonly name: string;

  /** Custom claim — assigned RBAC role. */
  readonly role: Role;

  /** Custom claim — tenant namespace for multi-tenant isolation. */
  readonly tenant_id: string;

  /** Custom claim — effective permission set for the token. */
  readonly permissions: Permission[];

  /** Issued-at Unix timestamp (seconds since epoch). */
  readonly iat: number;

  /** Expiration Unix timestamp (seconds since epoch). */
  readonly exp: number;

  /** Token issuer — the Auth0 domain (e.g., `https://your-tenant.auth0.com/`). */
  readonly iss: string;

  /** Token audience — API identifier(s) the token is valid for. */
  readonly aud: string | string[];
}

/**
 * Authentication state managed by the Zustand `authStore`.
 *
 * Tracks the current authentication lifecycle including loading states,
 * the resolved user object, tokens, and any authentication errors.
 */
export interface AuthState {
  /** Whether the user has a valid, non-expired access token. */
  isAuthenticated: boolean;

  /** Whether an authentication operation (login, refresh, logout) is in progress. */
  isLoading: boolean;

  /** The fully-resolved authenticated user profile, or `null` when unauthenticated. */
  user: User | null;

  /** The current JWT access token, or `null` when unauthenticated. */
  accessToken: string | null;

  /** The current refresh token used to obtain new access tokens, or `null`. */
  refreshToken: string | null;

  /** Human-readable error message from the most recent failed auth operation, or `null`. */
  error: string | null;
}

/**
 * Response payload returned by the login endpoint after successful OAuth2 authentication.
 *
 * Maps to the backend `LoginResponse` Pydantic model.
 */
export interface LoginResponse {
  /** JWT access token for authenticating subsequent API requests. */
  readonly access_token: string;

  /** Refresh token for obtaining new access tokens without re-authentication. */
  readonly refresh_token: string;

  /** Token type — always `'Bearer'` per OAuth2 spec. */
  readonly token_type: string;

  /** Access token lifetime in seconds. */
  readonly expires_in: number;
}

/**
 * Request payload for the token refresh endpoint.
 *
 * Sent to exchange a valid refresh token for a new access/refresh token pair.
 */
export interface TokenRefreshRequest {
  /** The refresh token previously issued during login or a prior refresh. */
  readonly refresh_token: string;
}

/**
 * Response payload returned by the token refresh endpoint.
 *
 * Contains the rotated access and refresh tokens per OAuth2 refresh token rotation.
 * Maps to the backend `TokenRefreshResponse` Pydantic model.
 */
export interface TokenRefreshResponse {
  /** Newly issued JWT access token. */
  readonly access_token: string;

  /** Rotated refresh token (the previous refresh token is invalidated). */
  readonly refresh_token: string;

  /** Token type — always `'Bearer'` per OAuth2 spec. */
  readonly token_type: string;

  /** New access token lifetime in seconds. */
  readonly expires_in: number;
}
