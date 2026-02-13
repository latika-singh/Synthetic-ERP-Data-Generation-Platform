/**
 * @fileoverview Custom React hook wrapping @auth0/auth0-react for unified
 * authentication state management in the Synthetic ERP Data Generation
 * Platform Web Console.
 *
 * Bridges the Auth0 React SDK's `useAuth0` hook with the Zustand `authStore`,
 * providing a single React-idiomatic API surface for:
 * - Login initiation via Auth0 redirect flow (OAuth 2.0 / OpenID Connect)
 * - Logout with token revocation and Auth0 session termination
 * - Silent access token retrieval for API calls (getAccessTokenSilently)
 * - User profile enrichment: email, name, role, tenant_id, permissions from
 *   JWT custom claims and Auth0 user metadata
 * - Automatic synchronization of Auth0 SDK state with the Zustand authStore
 *   on login/logout transitions
 * - Authentication loading state and error propagation
 *
 * Consumed by:
 * - `App.tsx` for auth initialization on application mount
 * - `routes.tsx` ProtectedRoute component for route guards
 * - `Login.tsx` page for redirect handling
 * - `Header.tsx` component for user display and logout
 * - Any component requiring authentication context
 *
 * Security considerations:
 * - R-006: All endpoints require JWT authentication except health checks
 * - R-007: Multi-tenant user context with tenant_id for namespace isolation
 * - JWT RS256 tokens with short expiry; Auth0 SDK handles refresh internally
 *
 * @module hooks/useAuth
 * @version 1.0.0
 */

import { useCallback, useEffect, useMemo } from 'react';
import { useAuth0, User as Auth0User } from '@auth0/auth0-react';
import { useAuthStore } from '@/store/authStore';
import type { User, Role, Permission } from '@/types/auth';
import { ROLE_PERMISSIONS } from '@/types/auth';

// ============================================================================
// Constants
// ============================================================================

/**
 * Auth0 custom claim namespace URI.
 *
 * Auth0 requires custom claims to use a namespace URI to avoid collisions
 * with standard OIDC claims. Custom claims (role, tenant_id, permissions)
 * are injected into the ID token and access token via Auth0 Actions / Rules
 * under this namespace prefix.
 *
 * Example claim structure in JWT payload:
 * ```json
 * {
 *   "https://synthetic-erp.com/role": "data_engineer",
 *   "https://synthetic-erp.com/tenant_id": "tenant_abc123",
 *   "https://synthetic-erp.com/permissions": ["generation:create", "generation:read"]
 * }
 * ```
 */
const AUTH0_NAMESPACE = 'https://synthetic-erp.com';

/**
 * Default token expiry duration in seconds used when the actual expiry
 * cannot be determined from the token. Defaults to 1 hour (3600 seconds)
 * which is Auth0's standard access token lifetime.
 */
const DEFAULT_TOKEN_EXPIRY_SECONDS = 3600;

// ============================================================================
// Return Type Interface
// ============================================================================

/**
 * Return type of the {@link useAuth} hook.
 *
 * Provides a comprehensive API surface covering authentication state,
 * lifecycle actions, and user context properties. All state values are
 * reactive — components re-render when any value changes.
 */
export interface UseAuthReturn {
  // ── Authentication State ────────────────────────────────────────────

  /** Whether the user is authenticated (Auth0 SDK or Zustand store). */
  isAuthenticated: boolean;

  /** Whether an authentication operation is in progress. */
  isLoading: boolean;

  /**
   * Enriched user profile from the Zustand authStore.
   * Includes role, permissions, and tenant_id from JWT custom claims.
   * Null when unauthenticated.
   */
  user: User | null;

  /**
   * Raw Auth0 user profile object for advanced use cases.
   * Contains Auth0-specific fields (sub, picture, nickname, etc.)
   * that are not part of the enriched User interface.
   * Undefined when unauthenticated.
   */
  auth0User: Auth0User | undefined;

  /** Current JWT access token from Zustand store, or null. */
  accessToken: string | null;

  /** Human-readable error message from the most recent failed auth operation, or null. */
  error: string | null;

  // ── Authentication Actions ──────────────────────────────────────────

  /**
   * Initiates the Auth0 login redirect flow.
   * @param options - Optional configuration including returnTo URL for post-login redirect
   */
  login: (options?: { returnTo?: string }) => Promise<void>;

  /**
   * Logs out the user by clearing Zustand store and redirecting to Auth0 logout.
   * @param options - Optional configuration including returnTo URL for post-logout redirect
   */
  logout: (options?: { returnTo?: string }) => Promise<void>;

  /**
   * Obtains a fresh access token via Auth0's getAccessTokenSilently.
   * Falls back to the stored token if silent retrieval fails.
   * @returns The JWT access token, or null if unavailable
   */
  getAccessToken: () => Promise<string | null>;

  // ── User Context ────────────────────────────────────────────────────

  /** Auth0 subject identifier (e.g., "auth0|abc123"), or null if unauthenticated. */
  userId: string | null;

  /** User's email address, or null if unauthenticated. */
  email: string | null;

  /** User's display name, or null if unauthenticated. */
  name: string | null;

  /** User's assigned RBAC role, or null if unauthenticated. */
  role: Role | null;

  /** Tenant namespace ID for multi-tenant isolation (R-007), or null. */
  tenantId: string | null;

  /** User's effective permissions derived from role assignment. */
  permissions: Permission[];

  /** URL to the user's profile picture (from Auth0 or uploaded), or null. */
  avatarUrl: string | null;
}

// ============================================================================
// Helper Functions
// ============================================================================

/**
 * Extracts a custom claim from an Auth0 user profile object.
 *
 * Auth0 custom claims are injected via Auth0 Actions or Rules under a
 * namespace URI prefix. This function looks for the claim in both the
 * namespaced key (e.g., `https://synthetic-erp.com/role`) and common
 * alternative locations (`app_metadata`, direct user property).
 *
 * @param auth0User - Auth0 user profile object (may contain arbitrary keys)
 * @param claimName - Name of the custom claim to extract (e.g., "role", "tenant_id")
 * @returns The claim value if found, or undefined
 */
function extractCustomClaim<T>(
  auth0User: Auth0User,
  claimName: string,
): T | undefined {
  // Check namespaced claim first (standard Auth0 custom claims location)
  const namespacedKey = `${AUTH0_NAMESPACE}/${claimName}`;
  if (namespacedKey in auth0User) {
    return auth0User[namespacedKey] as T;
  }

  // Check app_metadata (Auth0 Management API enrichment)
  const appMetadata = auth0User['app_metadata'] as
    | Record<string, unknown>
    | undefined;
  if (appMetadata && claimName in appMetadata) {
    return appMetadata[claimName] as T;
  }

  // Check direct property (some Auth0 configurations flatten claims)
  if (claimName in auth0User) {
    return auth0User[claimName] as T;
  }

  return undefined;
}

/**
 * Validates and normalizes a role string against the known Role enum values.
 *
 * Ensures that only recognized role values are accepted, preventing
 * injection of arbitrary role strings from malformed tokens.
 *
 * @param roleValue - Raw role string extracted from JWT custom claims
 * @returns Validated Role enum value, or null if the value is not a valid role
 */
function validateRole(roleValue: string | undefined | null): Role | null {
  if (!roleValue) return null;

  const validRoles: readonly string[] = [
    'platform_admin',
    'data_engineer',
    'developer',
    'qa_engineer',
    'data_analyst',
  ];

  if (validRoles.includes(roleValue)) {
    return roleValue as Role;
  }

  return null;
}

/**
 * Resolves the effective permissions for a given role.
 *
 * Uses the authoritative ROLE_PERMISSIONS mapping from auth types as the
 * single source of truth. If custom permissions are provided from the token,
 * they are merged with role-derived permissions (union).
 *
 * @param role - Validated user role
 * @param tokenPermissions - Permissions array extracted from the token, if any
 * @returns Deduplicated array of effective Permission values
 */
function resolvePermissions(
  role: Role | null,
  tokenPermissions: string[] | undefined,
): Permission[] {
  const permissionSet = new Set<Permission>();

  // Add role-derived permissions from the authoritative mapping
  if (role && ROLE_PERMISSIONS[role]) {
    for (const perm of ROLE_PERMISSIONS[role]) {
      permissionSet.add(perm);
    }
  }

  // Merge any explicit token permissions (Auth0 may include custom permissions)
  // Validate each permission against the known set from ROLE_PERMISSIONS
  if (tokenPermissions && Array.isArray(tokenPermissions)) {
    // Collect all valid permission strings from the authoritative ROLE_PERMISSIONS map
    const allValidPermissions = new Set<string>();
    for (const rolePerms of Object.values(ROLE_PERMISSIONS)) {
      for (const p of rolePerms) {
        allValidPermissions.add(p);
      }
    }

    // Only add token permissions that match a recognized Permission value
    for (const perm of tokenPermissions) {
      if (allValidPermissions.has(perm)) {
        permissionSet.add(perm as Permission);
      }
    }
  }

  return Array.from(permissionSet);
}

/**
 * Constructs an enriched User object from Auth0 profile data and JWT custom claims.
 *
 * Merges standard OIDC claims (email, name, picture) with custom platform
 * claims (role, tenant_id, permissions) to create the complete User profile
 * required by the application.
 *
 * @param auth0User - Raw Auth0 user profile
 * @param role - Validated role extracted from custom claims
 * @param tenantId - Tenant ID extracted from custom claims
 * @param permissions - Resolved effective permissions
 * @returns Fully-populated User object
 */
function buildUserFromAuth0(
  auth0User: Auth0User,
  role: Role,
  tenantId: string,
  permissions: Permission[],
): User {
  return {
    user_id: auth0User.sub || '',
    email: auth0User.email || '',
    name: auth0User.name || auth0User.nickname || '',
    role,
    permissions,
    tenant_id: tenantId,
    avatar_url: auth0User.picture || null,
    is_active: true,
    last_login: new Date().toISOString(),
    created_at: auth0User.updated_at || new Date().toISOString(),
  };
}

// ============================================================================
// Hook Implementation
// ============================================================================

/**
 * Custom React hook providing unified authentication state management.
 *
 * Bridges the Auth0 React SDK (`useAuth0`) with the Zustand `authStore` to
 * provide a single, React-idiomatic API for all authentication operations.
 * Automatically synchronizes Auth0 authentication state changes with the
 * Zustand store, enriches user profiles with JWT custom claims, and provides
 * memoized computed state and callback functions.
 *
 * @example Basic usage in a component:
 * ```tsx
 * function MyComponent() {
 *   const { isAuthenticated, user, login, logout } = useAuth();
 *
 *   if (!isAuthenticated) {
 *     return <button onClick={() => login()}>Log In</button>;
 *   }
 *
 *   return (
 *     <div>
 *       <p>Welcome, {user?.name}!</p>
 *       <p>Role: {user?.role}</p>
 *       <button onClick={() => logout()}>Log Out</button>
 *     </div>
 *   );
 * }
 * ```
 *
 * @example Using with route guards:
 * ```tsx
 * function ProtectedRoute({ children }: { children: React.ReactNode }) {
 *   const { isAuthenticated, isLoading, login } = useAuth();
 *
 *   if (isLoading) return <LoadingSpinner />;
 *   if (!isAuthenticated) {
 *     login({ returnTo: window.location.pathname });
 *     return null;
 *   }
 *
 *   return <>{children}</>;
 * }
 * ```
 *
 * @returns {UseAuthReturn} Complete authentication state, actions, and user context
 */
function useAuth(): UseAuthReturn {
  // ── Auth0 SDK State ───────────────────────────────────────────────────
  const {
    isAuthenticated: auth0IsAuthenticated,
    isLoading: auth0IsLoading,
    user: auth0User,
    loginWithRedirect,
    logout: auth0Logout,
    getAccessTokenSilently,
    error: auth0Error,
  } = useAuth0();

  // ── Zustand Auth Store State ──────────────────────────────────────────
  const {
    user: storedUser,
    accessToken,
    isAuthenticated: storeIsAuthenticated,
    isLoading: storeIsLoading,
    error: storeError,
    setUser,
    setTokens,
    logout: storeLogout,
    initializeAuth,
    fetchCurrentUser,
  } = useAuthStore();

  // ── Initial Auth Check (runs once on mount) ───────────────────────────
  //
  // Restores authentication state from persisted tokens in localStorage.
  // If Auth0 reports authenticated but no user is in the Zustand store
  // (e.g., after page refresh with valid Auth0 session), fetches the user
  // profile from the API Gateway.
  useEffect(() => {
    const initialize = async (): Promise<void> => {
      // Initialize Zustand store from persisted localStorage tokens
      await initializeAuth();

      // If Auth0 reports authenticated but Zustand has no user yet,
      // fetch the user profile to sync state
      if (auth0IsAuthenticated && !storedUser) {
        try {
          await fetchCurrentUser();
        } catch {
          // fetchCurrentUser failure is handled internally by the store
          // (sets error state). No additional handling needed here.
        }
      }
    };

    initialize();
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  // ── Auth0 ↔ Zustand State Synchronization ─────────────────────────────
  //
  // Watches for Auth0 authentication state changes and propagates them to
  // the Zustand store. When Auth0 reports a successful authentication,
  // extracts custom claims (role, tenant_id, permissions) from the JWT
  // and constructs an enriched User object for the store. When Auth0
  // reports logout, clears the Zustand store.
  useEffect(() => {
    const syncAuth0State = async (): Promise<void> => {
      if (auth0IsAuthenticated && auth0User) {
        try {
          // Obtain JWT access token from Auth0 SDK (silent renewal)
          const token = await getAccessTokenSilently();

          // Extract custom claims from Auth0 user profile / JWT
          const roleValue = extractCustomClaim<string>(auth0User, 'role');
          const tenantIdValue = extractCustomClaim<string>(
            auth0User,
            'tenant_id',
          );
          const tokenPermissions = extractCustomClaim<string[]>(
            auth0User,
            'permissions',
          );

          // Validate the extracted role against known Role enum values
          const validatedRole = validateRole(roleValue);

          // Resolve effective permissions from role + token claims
          const resolvedPermissions = resolvePermissions(
            validatedRole,
            tokenPermissions,
          );

          // Only proceed if we have the minimum required claims for
          // multi-tenant isolation (R-007)
          if (validatedRole && tenantIdValue) {
            // Construct the enriched User object
            const enrichedUser = buildUserFromAuth0(
              auth0User,
              validatedRole,
              tenantIdValue,
              resolvedPermissions,
            );

            // Update Zustand store with enriched user profile
            setUser(enrichedUser);

            // Store the access token for API interceptor usage
            // Auth0 SDK handles refresh internally, so we pass empty string
            // for refreshToken. The SDK manages the refresh cycle.
            setTokens(token, '', DEFAULT_TOKEN_EXPIRY_SECONDS);
          } else {
            // Missing required claims — attempt to fetch full profile from API
            // The API Gateway returns a complete User with server-validated claims
            try {
              await fetchCurrentUser();
            } catch {
              // fetchCurrentUser sets error in the store internally
            }

            // Still store the token even if claim extraction partially failed
            if (token) {
              setTokens(token, '', DEFAULT_TOKEN_EXPIRY_SECONDS);
            }
          }
        } catch (tokenError: unknown) {
          // Token retrieval failed — log and continue with degraded state.
          // The user may need to re-authenticate.
          console.error(
            'Failed to synchronize Auth0 state with store:',
            tokenError,
          );
        }
      } else if (!auth0IsAuthenticated && storeIsAuthenticated) {
        // Auth0 reports unauthenticated but store still shows authenticated.
        // This happens when the Auth0 session expires or is revoked externally.
        // Clear the Zustand store to reflect the actual authentication state.
        storeLogout();
      }
    };

    // Only run synchronization when Auth0 has finished loading
    if (!auth0IsLoading) {
      syncAuth0State();
    }
    // Dependency array targets Auth0 state changes that should trigger sync
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [auth0IsAuthenticated, auth0User, auth0IsLoading]);

  // ── Login Action ──────────────────────────────────────────────────────
  //
  // Initiates the Auth0 Universal Login redirect flow with OpenID Connect
  // scopes for profile and email access. Preserves the current URL path
  // as the returnTo destination for post-login redirect.
  const login = useCallback(
    async (options?: { returnTo?: string }): Promise<void> => {
      await loginWithRedirect({
        appState: {
          returnTo: options?.returnTo || window.location.pathname,
        },
        authorizationParams: {
          scope: 'openid profile email',
        },
      });
    },
    [loginWithRedirect],
  );

  // ── Logout Action ─────────────────────────────────────────────────────
  //
  // Clears the Zustand authStore first (immediate local cleanup) and then
  // redirects to Auth0's logout endpoint to terminate the Auth0 session.
  // The store is cleared before the redirect to ensure instant UI feedback
  // regardless of Auth0 redirect latency.
  const logout = useCallback(
    async (options?: { returnTo?: string }): Promise<void> => {
      // Clear Zustand store first for immediate local state cleanup
      // Fire-and-forget: storeLogout is async but we don't await it here
      // because its state clearing (set()) is synchronous; only the
      // server-side token revocation is async (and non-critical).
      storeLogout();

      // Redirect to Auth0 logout to terminate the Auth0 session
      await auth0Logout({
        logoutParams: {
          returnTo:
            options?.returnTo || window.location.origin + '/login',
        },
      });
    },
    [auth0Logout, storeLogout],
  );

  // ── Access Token Retrieval ────────────────────────────────────────────
  //
  // Obtains a fresh access token via Auth0's silent token renewal.
  // Falls back to the stored token if silent retrieval fails (e.g.,
  // when Auth0 session has expired but stored token is still valid).
  const getAccessToken = useCallback(async (): Promise<string | null> => {
    try {
      const token = await getAccessTokenSilently();
      return token;
    } catch (error: unknown) {
      console.error('Failed to get access token:', error);
      // Fall back to the stored token from the Zustand store
      return accessToken;
    }
  }, [getAccessTokenSilently, accessToken]);

  // ── Computed State (Memoized) ─────────────────────────────────────────
  //
  // Derives UI-ready state values from Auth0 SDK and Zustand store.
  // The Zustand store is preferred as source of truth for enriched user
  // data, while Auth0 fields serve as fallbacks for basic profile info.
  const computedState = useMemo(
    () => ({
      // Authentication state — true if either Auth0 or store reports authenticated
      isAuthenticated: auth0IsAuthenticated || storeIsAuthenticated,

      // Loading state — true if either Auth0 or store is processing
      isLoading: auth0IsLoading || storeIsLoading,

      // Enriched user from Zustand store (includes role, tenant_id, permissions)
      user: storedUser,

      // Raw Auth0 user for advanced use cases (e.g., accessing Auth0-specific fields)
      auth0User,

      // Access token from Zustand store for API interceptor usage
      accessToken,

      // Error message — prefer Auth0 error, then store error
      error: auth0Error?.message || storeError || null,

      // ── User Context Properties ─────────────────────────────────────
      // Convenience accessors for commonly used user fields.
      // Prefer Zustand stored user, fall back to Auth0 user for basic fields.

      /** Auth0 subject identifier (unique user key) */
      userId: storedUser?.user_id ?? null,

      /** User email — prefer stored, fall back to Auth0 */
      email: storedUser?.email ?? auth0User?.email ?? null,

      /** User display name — prefer stored, fall back to Auth0 */
      name: storedUser?.name ?? auth0User?.name ?? null,

      /** RBAC role from stored user profile */
      role: storedUser?.role ?? null,

      /** Tenant namespace ID for multi-tenant isolation (R-007) */
      tenantId: storedUser?.tenant_id ?? null,

      /** Effective permissions derived from role assignment */
      permissions: storedUser?.permissions ?? [],

      /** Profile picture URL — prefer stored, fall back to Auth0 picture */
      avatarUrl: storedUser?.avatar_url ?? auth0User?.picture ?? null,
    }),
    [
      auth0IsAuthenticated,
      storeIsAuthenticated,
      auth0IsLoading,
      storeIsLoading,
      storedUser,
      auth0User,
      accessToken,
      auth0Error,
      storeError,
    ],
  );

  // ── Return Value ──────────────────────────────────────────────────────
  //
  // Combines memoized computed state with memoized callback actions into
  // a single return object. Destructuring at call sites enables selective
  // subscription to only the needed values.
  return {
    ...computedState,
    login,
    logout,
    getAccessToken,
  };
}

// ============================================================================
// Exports
// ============================================================================

export type { UseAuthReturn };
export default useAuth;
export { useAuth };
