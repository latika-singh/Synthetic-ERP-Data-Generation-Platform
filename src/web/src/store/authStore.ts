/**
 * @fileoverview Zustand state management store for authentication and authorization.
 *
 * Manages the complete authentication lifecycle for the Synthetic ERP Data
 * Generation Platform Web Console, including JWT access/refresh token storage,
 * current user profile with RBAC role and permissions, login/logout flows via
 * Auth0, automatic token refresh with rotation, and role-based permission
 * checking helpers.
 *
 * This is the most foundational store in the application — it is imported by:
 * - `services/api.ts` interceptor for JWT header injection and token refresh
 * - `App.tsx` for auth initialization on application mount
 * - `useAuth` hook for authentication state access
 * - `usePermissions` hook for RBAC enforcement
 * - Route guards for protected route access control
 * - `Header` component for user profile display
 * - `Sidebar` for role-based menu visibility
 * - `AdminPanel` for admin permission checks
 *
 * Circular Dependency Resolution:
 * The authStore imports from authApi which imports from api.ts which accesses
 * authStore. This circular dependency chain (authStore → authApi → api → authStore)
 * is resolved by:
 * 1. `api.ts` lazily loads authStore via dynamic `import()` with caching
 * 2. `authStore` uses dynamic imports for authApi functions that depend on api.ts
 * This ensures no synchronous circular import occurs at module initialization.
 *
 * Persistence:
 * Uses Zustand `persist` middleware to store auth-critical fields (accessToken,
 * refreshToken, tokenExpiresAt, user, isAuthenticated) in localStorage under
 * the key 'synthetic-erp-auth-store'. Transient fields (isLoading, error) are
 * excluded via `partialize`.
 *
 * @module store/authStore
 * @version 1.0.0
 */

import { create } from 'zustand';
import { persist, createJSONStorage } from 'zustand/middleware';
import {
  ROLE_PERMISSIONS,
  type User,
  type Role,
  type Permission,
  type LoginResponse,
  type TokenRefreshResponse,
  type AuthState,
  type TokenPayload,
} from '@/types/auth';

// ============================================================================
// Store State Interface
// ============================================================================

/**
 * Complete state shape and action methods for the authentication store.
 *
 * Divides into three categories:
 * - **State fields**: Reactive values describing current auth status
 * - **Lifecycle actions**: Async operations for login, logout, refresh, callback
 * - **Permission helpers**: Synchronous RBAC checks against user permissions
 */
export interface AuthStoreState {
  // ── State Fields ──────────────────────────────────────────────────────

  /** Whether the user has a valid, non-expired access token */
  isAuthenticated: boolean;

  /** Whether an auth operation (login, refresh, init) is in progress */
  isLoading: boolean;

  /** Authenticated user profile with role, permissions, and tenant context */
  user: User | null;

  /** Current JWT access token for API authentication */
  accessToken: string | null;

  /** Current refresh token for obtaining new access tokens */
  refreshToken: string | null;

  /** Human-readable error message from the last failed auth operation */
  error: string | null;

  /** Unix timestamp (milliseconds) when the access token expires */
  tokenExpiresAt: number | null;

  // ── Lifecycle Actions ─────────────────────────────────────────────────

  /** Store new access and refresh tokens after successful auth or refresh */
  setTokens: (accessToken: string, refreshToken: string, expiresIn: number) => void;

  /** Set the authenticated user profile */
  setUser: (user: User) => void;

  /** Process a login response (set tokens + fetch user profile) */
  login: (loginResponse: LoginResponse) => Promise<void>;

  /** Handle OAuth callback by exchanging code for tokens */
  handleAuthCallback: (code: string, state: string) => Promise<boolean>;

  /** Log out the user (revoke tokens, clear state) */
  logout: () => Promise<void>;

  /** Attempt to refresh the access token using the stored refresh token */
  refreshAccessToken: () => Promise<boolean>;

  /** Fetch the current user profile from the API */
  fetchCurrentUser: () => Promise<void>;

  /** Initialize authentication state on application mount */
  initializeAuth: () => Promise<void>;

  // ── Permission Helpers ────────────────────────────────────────────────

  /** Check if the authenticated user has a specific permission */
  hasPermission: (permission: Permission) => boolean;

  /** Check if the authenticated user has a specific role */
  hasRole: (role: Role) => boolean;

  /** Check if the authenticated user has any of the specified permissions */
  hasAnyPermission: (permissions: Permission[]) => boolean;

  /** Check if the authenticated user has all of the specified permissions */
  hasAllPermissions: (permissions: Permission[]) => boolean;

  // ── State Management ──────────────────────────────────────────────────

  /** Set or clear the error message */
  setError: (error: string | null) => void;

  /** Clear the current error message */
  clearError: () => void;

  /** Reset all authentication state to initial values */
  reset: () => void;
}

// ============================================================================
// Lazy Auth API Accessor (Circular Dependency Resolution)
// ============================================================================

/**
 * Dynamically imports the auth API service module to avoid circular dependency.
 *
 * The import chain authStore → authApi → api → authStore would cause a
 * circular initialization error if all imports were static. By using dynamic
 * `import()`, the authApi module is loaded on-demand when actions are invoked,
 * well after all modules have finished initialization.
 *
 * @returns Promise resolving to the authApi module exports, or null if unavailable
 */
async function getAuthApi(): Promise<typeof import('@/services/authApi') | null> {
  try {
    const mod = await import('@/services/authApi');
    return mod;
  } catch {
    // authApi module not yet available — return null to signal unavailability
    return null;
  }
}

// ============================================================================
// JWT Token Decode Utility
// ============================================================================

/**
 * Decodes a JWT access token payload without verifying its signature.
 *
 * Extracts the standard and custom claims (sub, email, role, tenant_id,
 * permissions, iat, exp, iss, aud) from the token's base64url-encoded
 * payload segment. Used for local token expiry checking during auth
 * initialization without requiring an API round-trip.
 *
 * Note: This is NOT a security operation — signature verification happens
 * server-side via Auth0 RS256 public key validation. This is purely for
 * local UX optimization (checking expiry before making API calls).
 *
 * @param token - JWT access token string (header.payload.signature format)
 * @returns Decoded {@link TokenPayload}, or null if token format is invalid
 */
function decodeTokenPayload(token: string): TokenPayload | null {
  try {
    const parts = token.split('.');
    if (parts.length !== 3) return null;

    // Convert base64url to standard base64 and add padding
    const base64 = parts[1].replace(/-/g, '+').replace(/_/g, '/');
    const padded = base64 + '='.repeat((4 - (base64.length % 4)) % 4);

    // Decode base64 and handle multi-byte UTF-8 characters properly
    const jsonPayload = decodeURIComponent(
      atob(padded)
        .split('')
        .map((c: string) => '%' + ('00' + c.charCodeAt(0).toString(16)).slice(-2))
        .join(''),
    );

    return JSON.parse(jsonPayload) as TokenPayload;
  } catch {
    // Malformed token — return null rather than throwing
    return null;
  }
}

// ============================================================================
// Initial State
// ============================================================================

/**
 * Default state values used for initialization and reset.
 *
 * Extends the core {@link AuthState} shape with the `tokenExpiresAt`
 * field for local token expiry tracking. `isLoading` starts as `true`
 * to prevent premature UI rendering before {@link AuthStoreState.initializeAuth}
 * completes its validation cycle.
 */
const initialState: AuthState & { tokenExpiresAt: number | null } = {
  isAuthenticated: false,
  isLoading: true, // true until initializeAuth completes
  user: null,
  accessToken: null,
  refreshToken: null,
  error: null,
  tokenExpiresAt: null,
};

// ============================================================================
// Persistence Storage Key
// ============================================================================

/** localStorage key for persisted auth state across page refreshes */
const STORAGE_KEY = 'synthetic-erp-auth-store';

// ============================================================================
// Store Implementation
// ============================================================================

/**
 * Zustand authentication store with localStorage persistence.
 *
 * Persists auth-critical fields (accessToken, refreshToken, tokenExpiresAt,
 * user, isAuthenticated) across page refreshes. Transient fields (isLoading,
 * error) are excluded from persistence via the `partialize` option.
 *
 * Access patterns:
 * - React components: `const { user, isAuthenticated } = useAuthStore()`
 * - Outside React (e.g., api.ts interceptor): `useAuthStore.getState().accessToken`
 * - Selective subscription: `useAuthStore((s) => s.isAuthenticated)`
 */
export const useAuthStore = create<AuthStoreState>()(
  persist(
    (set, get) => ({
      // ── Initial State ───────────────────────────────────────────────
      ...initialState,

      // ── setTokens ───────────────────────────────────────────────────
      /**
       * Stores new JWT access and refresh tokens with computed expiry.
       *
       * Calculates the absolute expiry timestamp from the relative `expiresIn`
       * value (in seconds) and sets isAuthenticated to true. Clears any
       * previous error state.
       */
      setTokens: (accessToken: string, refreshToken: string, expiresIn: number): void => {
        set({
          accessToken,
          refreshToken,
          tokenExpiresAt: Date.now() + expiresIn * 1000,
          isAuthenticated: true,
          error: null,
        });
      },

      // ── setUser ─────────────────────────────────────────────────────
      /**
       * Sets the authenticated user profile and marks loading as complete.
       *
       * The provided {@link User} object includes role, permissions, and
       * tenant_id for multi-tenant context (R-007).
       */
      setUser: (user: User): void => {
        set({
          user,
          isAuthenticated: true,
          isLoading: false,
        });
      },

      // ── login ───────────────────────────────────────────────────────
      /**
       * Processes a login response by storing tokens and fetching the user profile.
       *
       * Called after successful authentication (direct login or OAuth callback).
       * Extracts access_token, refresh_token, and expires_in from the
       * {@link LoginResponse} and then fetches the full user profile from
       * the /api/v1/auth/me endpoint.
       */
      login: async (loginResponse: LoginResponse): Promise<void> => {
        set({ isLoading: true, error: null });
        try {
          get().setTokens(
            loginResponse.access_token,
            loginResponse.refresh_token,
            loginResponse.expires_in,
          );
          await get().fetchCurrentUser();
        } catch (err: unknown) {
          const message =
            err instanceof Error ? err.message : 'Login failed';
          set({ error: message });
        } finally {
          set({ isLoading: false });
        }
      },

      // ── handleAuthCallback ──────────────────────────────────────────
      /**
       * Handles the OAuth2 authorization code callback from Auth0.
       *
       * Exchanges the authorization code and state parameter for JWT tokens
       * via POST /api/v1/auth/callback, then fetches the user profile.
       *
       * @returns true if authentication succeeded, false otherwise
       */
      handleAuthCallback: async (code: string, state: string): Promise<boolean> => {
        set({ isLoading: true, error: null });
        try {
          const authApi = await getAuthApi();
          if (!authApi) {
            set({ error: 'Authentication service unavailable' });
            return false;
          }

          // Exchange authorization code for tokens via the API Gateway
          const response = await authApi.handleCallback(code, state);
          // Response is ApiResponse<LoginResponse> — access .data for the login payload
          const loginData = response.data;
          get().setTokens(
            loginData.access_token,
            loginData.refresh_token,
            loginData.expires_in,
          );
          await get().fetchCurrentUser();
          return true;
        } catch (err: unknown) {
          const message =
            err instanceof Error ? err.message : 'Authentication callback failed';
          set({ error: message });
          return false;
        } finally {
          set({ isLoading: false });
        }
      },

      // ── logout ──────────────────────────────────────────────────────
      /**
       * Logs out the user by revoking the refresh token and clearing all state.
       *
       * Server-side token revocation is fire-and-forget — local state is
       * cleared immediately regardless of whether the server-side revocation
       * succeeds. This ensures instant UI response on logout.
       */
      logout: async (): Promise<void> => {
        const currentRefreshToken = get().refreshToken;

        // Fire-and-forget server-side token revocation via POST /api/v1/auth/logout
        // Do not block on the server response — local cleanup takes priority
        if (currentRefreshToken) {
          getAuthApi()
            .then((authApi) => {
              if (authApi) {
                authApi.logout(currentRefreshToken).catch(() => {
                  // Server-side logout failure is non-critical; local state is cleared below
                });
              }
            })
            .catch(() => {
              // Dynamic import failure is non-critical during logout
            });
        }

        // Clear all auth state immediately (do not wait for server response)
        set({
          isAuthenticated: false,
          user: null,
          accessToken: null,
          refreshToken: null,
          tokenExpiresAt: null,
          error: null,
          isLoading: false,
        });

        // Explicitly clear persisted storage to guarantee clean logout state
        try {
          localStorage.removeItem(STORAGE_KEY);
        } catch {
          // localStorage may be unavailable in certain environments (e.g., SSR)
        }
      },

      // ── refreshAccessToken ──────────────────────────────────────────
      /**
       * Attempts to refresh the JWT access token using the stored refresh token.
       *
       * Implements OAuth2 refresh token rotation: the current refresh token is
       * consumed and a new access/refresh pair is returned. On failure
       * (expired/revoked token), resets all auth state to force re-login.
       *
       * @returns true if refresh succeeded, false if re-login is required
       */
      refreshAccessToken: async (): Promise<boolean> => {
        const currentRefreshToken = get().refreshToken;
        if (!currentRefreshToken) {
          get().reset();
          return false;
        }

        try {
          const authApi = await getAuthApi();
          if (!authApi) {
            get().reset();
            return false;
          }

          // Exchange refresh token for new token pair via POST /api/v1/auth/refresh
          const response = await authApi.refreshToken({
            refresh_token: currentRefreshToken,
          });
          // Response is ApiResponse<TokenRefreshResponse> — access .data for the token payload
          const tokenData: TokenRefreshResponse = response.data;
          get().setTokens(
            tokenData.access_token,
            tokenData.refresh_token,
            tokenData.expires_in,
          );
          return true;
        } catch {
          // Refresh token is expired or revoked — force re-login
          get().reset();
          return false;
        }
      },

      // ── fetchCurrentUser ────────────────────────────────────────────
      /**
       * Fetches the current user profile from the API Gateway.
       *
       * Calls GET /api/v1/auth/me to retrieve the full user record including
       * role, permissions, and tenant_id. Validates that tenant context is
       * present to enforce multi-tenant isolation (R-007).
       */
      fetchCurrentUser: async (): Promise<void> => {
        set({ isLoading: true });
        try {
          const authApi = await getAuthApi();
          if (!authApi) {
            set({ error: 'Authentication service unavailable', isLoading: false });
            return;
          }

          // Fetch authenticated user profile via GET /api/v1/auth/me
          const response = await authApi.getCurrentUser();
          // Response is ApiResponse<User> — access .data for the user object
          const user: User = response.data;

          // Validate tenant context for multi-tenant namespace isolation (R-007)
          if (!user.tenant_id) {
            set({
              error: 'User profile missing required tenant context',
              isLoading: false,
            });
            return;
          }

          set({ user, isAuthenticated: true });
        } catch (err: unknown) {
          const message =
            err instanceof Error ? err.message : 'Failed to fetch user profile';
          set({ error: message });
        } finally {
          set({ isLoading: false });
        }
      },

      // ── initializeAuth ──────────────────────────────────────────────
      /**
       * Initializes authentication state on application mount.
       *
       * Called once in App.tsx's useEffect to restore auth from persisted
       * state. Validates token expiry (using both stored expiry and decoded
       * JWT payload via {@link decodeTokenPayload}), attempts refresh if
       * expired, and fetches the current user profile to validate the token.
       *
       * If all recovery attempts fail, resets to unauthenticated state.
       */
      initializeAuth: async (): Promise<void> => {
        const { accessToken, tokenExpiresAt } = get();

        // No stored token — nothing to initialize
        if (!accessToken) {
          set({ isLoading: false });
          return;
        }

        // Determine if the stored token has expired
        // Prefer the stored tokenExpiresAt; fall back to decoding the JWT payload
        let isExpired = false;
        if (tokenExpiresAt) {
          isExpired = Date.now() >= tokenExpiresAt;
        } else {
          // Attempt to extract expiry from the JWT payload directly
          const payload: TokenPayload | null = decodeTokenPayload(accessToken);
          if (payload && payload.exp) {
            // JWT exp is in seconds; convert to milliseconds for comparison
            isExpired = Date.now() >= payload.exp * 1000;
          }
        }

        // Token expired — attempt refresh before fetching user
        if (isExpired) {
          const refreshed = await get().refreshAccessToken();
          if (!refreshed) {
            set({ isLoading: false });
            return;
          }
        }

        // Validate token by fetching current user profile
        try {
          await get().fetchCurrentUser();
        } catch {
          // Token validation failed (e.g., revoked server-side) — attempt one refresh
          const refreshed = await get().refreshAccessToken();
          if (refreshed) {
            try {
              await get().fetchCurrentUser();
            } catch {
              // Final recovery attempt failed — reset to unauthenticated
              get().reset();
            }
          }
        }

        set({ isLoading: false });
      },

      // ── Permission Helpers ──────────────────────────────────────────
      //
      // These helpers check the authenticated user's permissions for RBAC
      // enforcement across the Web Console. They check the user's explicit
      // permissions array first, then fall back to the authoritative
      // ROLE_PERMISSIONS mapping as a safety net in case the server returns
      // an incomplete permissions set for the user's role.

      /**
       * Checks if the authenticated user has a specific permission.
       *
       * First checks the user's explicit permissions array. If not found,
       * falls back to the {@link ROLE_PERMISSIONS} mapping for the user's
       * assigned role to handle cases where the server omits permissions.
       *
       * @param permission - The {@link Permission} to check
       * @returns true if the user has the permission, false otherwise
       */
      hasPermission: (permission: Permission): boolean => {
        const user = get().user;
        if (!user) return false;
        // Check user's explicit permissions first
        if (user.permissions.includes(permission)) return true;
        // Fallback: derive permissions from role using the authoritative mapping
        const rolePerms = ROLE_PERMISSIONS[user.role];
        return rolePerms ? rolePerms.includes(permission) : false;
      },

      /**
       * Checks if the authenticated user has a specific role.
       *
       * Performs direct equality comparison against the user's assigned
       * {@link Role}. Does not check role hierarchy — use `hasPermission`
       * for graduated access control.
       *
       * @param role - The {@link Role} to check
       * @returns true if the user has the exact role, false otherwise
       */
      hasRole: (role: Role): boolean => {
        const user = get().user;
        if (!user) return false;
        return user.role === role;
      },

      /**
       * Checks if the authenticated user has ANY of the specified permissions.
       *
       * Combines the user's explicit permissions with role-derived permissions
       * from {@link ROLE_PERMISSIONS} for comprehensive checking.
       *
       * @param permissions - Array of {@link Permission} values to check
       * @returns true if the user has at least one of the permissions
       */
      hasAnyPermission: (permissions: Permission[]): boolean => {
        const user = get().user;
        if (!user) return false;
        const rolePerms = ROLE_PERMISSIONS[user.role] || [];
        return permissions.some(
          (p) => user.permissions.includes(p) || rolePerms.includes(p),
        );
      },

      /**
       * Checks if the authenticated user has ALL of the specified permissions.
       *
       * Combines the user's explicit permissions with role-derived permissions
       * from {@link ROLE_PERMISSIONS} for comprehensive checking.
       *
       * @param permissions - Array of {@link Permission} values to check
       * @returns true if the user has every listed permission
       */
      hasAllPermissions: (permissions: Permission[]): boolean => {
        const user = get().user;
        if (!user) return false;
        const rolePerms = ROLE_PERMISSIONS[user.role] || [];
        return permissions.every(
          (p) => user.permissions.includes(p) || rolePerms.includes(p),
        );
      },

      // ── State Management ────────────────────────────────────────────

      /**
       * Sets or clears the error message displayed in the UI.
       *
       * @param error - Human-readable error message, or null to clear
       */
      setError: (error: string | null): void => {
        set({ error });
      },

      /**
       * Clears the current error message.
       *
       * Convenience method equivalent to `setError(null)`.
       */
      clearError: (): void => {
        set({ error: null });
      },

      /**
       * Resets all authentication state to initial values.
       *
       * Clears tokens, user profile, and authentication flags, then removes
       * persisted state from localStorage. After reset, `isLoading` is set
       * to false (unlike initial state which starts with isLoading: true).
       *
       * Called when:
       * - Refresh token is expired or revoked
       * - Token validation fails irrecoverably
       * - Admin deactivates a user session
       */
      reset: (): void => {
        set({
          ...initialState,
          isLoading: false, // After reset, we are not loading
        });
        // Explicitly clear persisted storage to ensure clean state
        try {
          localStorage.removeItem(STORAGE_KEY);
        } catch {
          // localStorage may be unavailable in certain environments
        }
      },
    }),
    {
      name: STORAGE_KEY,
      storage: createJSONStorage(() => localStorage),
      /**
       * Selects which state fields to persist to localStorage.
       *
       * Persists auth-critical fields only:
       * - accessToken, refreshToken: JWT tokens for API authentication
       * - tokenExpiresAt: Token expiry for local validation
       * - user: Cached user profile with role, permissions, and tenant_id
       * - isAuthenticated: Auth status flag
       *
       * Excludes transient fields:
       * - isLoading: Reset to true on mount (initializeAuth handles it)
       * - error: Errors are not persisted across page loads
       */
      partialize: (state: AuthStoreState) => ({
        accessToken: state.accessToken,
        refreshToken: state.refreshToken,
        tokenExpiresAt: state.tokenExpiresAt,
        user: state.user,
        isAuthenticated: state.isAuthenticated,
      }),
    },
  ),
);

// ── Default Export ───────────────────────────────────────────────────────────

export default useAuthStore;
