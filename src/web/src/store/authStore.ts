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
import type {
  User,
  Role,
  Permission,
  LoginResponse,
  TokenRefreshResponse,
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
 * @returns Promise resolving to the authApi module exports
 */
async function getAuthApi() {
  try {
    const mod = await import('@/services/authApi');
    return mod;
  } catch {
    // authApi module not yet available — return null to signal unavailability
    return null;
  }
}

// ============================================================================
// Initial State
// ============================================================================

/** Default state values used for initialization and reset */
const initialState = {
  isAuthenticated: false,
  isLoading: true, // true until initializeAuth completes
  user: null as User | null,
  accessToken: null as string | null,
  refreshToken: null as string | null,
  error: null as string | null,
  tokenExpiresAt: null as number | null,
};

// ============================================================================
// Store Implementation
// ============================================================================

/**
 * Zustand authentication store with localStorage persistence.
 *
 * Persists auth-critical fields (accessToken, refreshToken, tokenExpiresAt,
 * user, isAuthenticated) across page refreshes. Transient fields (isLoading,
 * error) are excluded from persistence.
 *
 * Access patterns:
 * - React components: `const { user, isAuthenticated } = useAuthStore()`
 * - Outside React: `useAuthStore.getState().accessToken`
 * - Selective subscription: `useAuthStore((s) => s.isAuthenticated)`
 */
export const useAuthStore = create<AuthStoreState>()(
  persist(
    (set, get) => ({
      // ── Initial State ───────────────────────────────────────────────
      ...initialState,

      // ── setTokens ───────────────────────────────────────────────────
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
      setUser: (user: User): void => {
        set({
          user,
          isAuthenticated: true,
          isLoading: false,
        });
      },

      // ── login ───────────────────────────────────────────────────────
      login: async (loginResponse: LoginResponse): Promise<void> => {
        set({ isLoading: true, error: null });
        try {
          get().setTokens(
            loginResponse.access_token,
            loginResponse.refresh_token,
            loginResponse.expires_in,
          );
          await get().fetchCurrentUser();
        } catch (err) {
          const message =
            err instanceof Error ? err.message : 'Login failed';
          set({ error: message });
        } finally {
          set({ isLoading: false });
        }
      },

      // ── handleAuthCallback ──────────────────────────────────────────
      handleAuthCallback: async (code: string, state: string): Promise<boolean> => {
        set({ isLoading: true, error: null });
        try {
          const authApi = await getAuthApi();
          if (!authApi) {
            set({ error: 'Authentication service unavailable' });
            return false;
          }
          const response = await authApi.handleCallback(code, state);
          const data = response as unknown as LoginResponse;
          get().setTokens(data.access_token, data.refresh_token, data.expires_in);
          await get().fetchCurrentUser();
          return true;
        } catch (err) {
          const message =
            err instanceof Error ? err.message : 'Authentication callback failed';
          set({ error: message });
          return false;
        } finally {
          set({ isLoading: false });
        }
      },

      // ── logout ──────────────────────────────────────────────────────
      logout: async (): Promise<void> => {
        const currentRefreshToken = get().refreshToken;
        // Fire-and-forget server-side token revocation — do not block on response
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
      },

      // ── refreshAccessToken ──────────────────────────────────────────
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
          const response = await authApi.refreshToken({ refresh_token: currentRefreshToken });
          const data = response as unknown as TokenRefreshResponse;
          get().setTokens(data.access_token, data.refresh_token, data.expires_in);
          return true;
        } catch {
          // Refresh token is expired or revoked — force re-login
          get().reset();
          return false;
        }
      },

      // ── fetchCurrentUser ────────────────────────────────────────────
      fetchCurrentUser: async (): Promise<void> => {
        set({ isLoading: true });
        try {
          const authApi = await getAuthApi();
          if (!authApi) {
            set({ error: 'Authentication service unavailable', isLoading: false });
            return;
          }
          const response = await authApi.getCurrentUser();
          const user = (response as unknown as { data: User }).data;
          set({ user, isAuthenticated: true });
        } catch (err) {
          const message =
            err instanceof Error ? err.message : 'Failed to fetch user profile';
          set({ error: message });
        } finally {
          set({ isLoading: false });
        }
      },

      // ── initializeAuth ──────────────────────────────────────────────
      initializeAuth: async (): Promise<void> => {
        const { accessToken, tokenExpiresAt } = get();

        // No stored token — nothing to initialize
        if (!accessToken) {
          set({ isLoading: false });
          return;
        }

        // Token expired — attempt refresh
        if (tokenExpiresAt && Date.now() >= tokenExpiresAt) {
          const refreshed = await get().refreshAccessToken();
          if (!refreshed) {
            set({ isLoading: false });
            return;
          }
        }

        // Validate token and fetch current user profile
        try {
          await get().fetchCurrentUser();
        } catch {
          // Token validation failed — attempt one refresh
          const refreshed = await get().refreshAccessToken();
          if (refreshed) {
            try {
              await get().fetchCurrentUser();
            } catch {
              get().reset();
            }
          }
        }
        set({ isLoading: false });
      },

      // ── Permission Helpers ──────────────────────────────────────────

      hasPermission: (permission: Permission): boolean => {
        const user = get().user;
        if (!user) return false;
        return user.permissions.includes(permission);
      },

      hasRole: (role: Role): boolean => {
        const user = get().user;
        if (!user) return false;
        return user.role === role;
      },

      hasAnyPermission: (permissions: Permission[]): boolean => {
        const user = get().user;
        if (!user) return false;
        return permissions.some((p) => user.permissions.includes(p));
      },

      hasAllPermissions: (permissions: Permission[]): boolean => {
        const user = get().user;
        if (!user) return false;
        return permissions.every((p) => user.permissions.includes(p));
      },

      // ── State Management ────────────────────────────────────────────

      setError: (error: string | null): void => {
        set({ error });
      },

      clearError: (): void => {
        set({ error: null });
      },

      reset: (): void => {
        set({
          ...initialState,
          isLoading: false, // After reset, we are not loading
        });
      },
    }),
    {
      name: 'synthetic-erp-auth-store',
      storage: createJSONStorage(() => localStorage),
      partialize: (state) => ({
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
