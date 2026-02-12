/**
 * @fileoverview Authentication API service for Auth0 integration.
 *
 * Provides typed API functions for all authentication and authorization
 * endpoints exposed by the Flask API Gateway at `/api/v1/auth/*`. Every
 * function uses the shared `apiClient` Axios instance for consistent
 * JWT injection, error handling, and response unwrapping.
 *
 * Consumed by:
 * - `authStore` (Zustand) for login, logout, token refresh, and user fetching
 * - `useAuth` hook for authentication state management
 * - `Login` page for OAuth redirect initiation
 * - `App.tsx` for Auth0 provider configuration
 *
 * Circular Dependency Note:
 * This module imports `apiClient` from `./api`, and `authStore` imports functions
 * from this module. The chain (authStore → authApi → api → authStore) is resolved
 * by `api.ts` lazily loading `authStore` via dynamic `import()`.
 *
 * @module services/authApi
 * @version 1.0.0
 */

import { apiClient } from './api';
import type { ApiResponse } from '@/types/api';
import type {
  User,
  LoginResponse,
  TokenRefreshRequest,
  TokenRefreshResponse,
} from '@/types/auth';

// ============================================================================
// Constants
// ============================================================================

/** Base path for all authentication endpoints (R-012: URL-path API versioning) */
const AUTH_BASE = '/api/v1/auth';

// ============================================================================
// Local Type Definitions
// ============================================================================

/**
 * Request payload for direct email/password login.
 *
 * Used as an alternative to the OAuth redirect flow for API-based
 * authentication (e.g., testing, CLI tools, or service accounts).
 */
export interface LoginRequest {
  /** User email address */
  readonly email: string;
  /** User password */
  readonly password: string;
}

/**
 * Request payload for updating the authenticated user's profile.
 *
 * Only specified fields are updated; omitted fields retain their current values.
 */
export interface UpdateProfileRequest {
  /** Updated display name */
  readonly name?: string;
  /** Updated avatar image URL */
  readonly avatar_url?: string;
}

// ============================================================================
// Authentication Endpoint Functions
// ============================================================================

/**
 * Authenticates a user via direct email/password credentials.
 *
 * Primary auth flow is Auth0 redirect; this endpoint supports API-based
 * authentication for testing, CLI tools, or service account scenarios.
 *
 * @param credentials - Email and password for authentication
 * @returns Login response containing access and refresh tokens
 *
 * @example
 * ```typescript
 * const response = await login({ email: 'user@example.com', password: 'secret' });
 * authStore.getState().setTokens(
 *   response.data.access_token,
 *   response.data.refresh_token,
 *   response.data.expires_in,
 * );
 * ```
 */
export async function login(
  credentials: LoginRequest,
): Promise<ApiResponse<LoginResponse>> {
  return apiClient.post(`${AUTH_BASE}/login`, credentials);
}

/**
 * Retrieves the Auth0 authorization URL for initiating the OAuth redirect flow.
 *
 * The returned URL should be used to redirect the user's browser to Auth0
 * for authentication. After successful auth, Auth0 redirects back to the
 * specified `redirectUri` with an authorization code.
 *
 * @param redirectUri - OAuth callback URI (defaults to `window.location.origin + '/callback'`)
 * @returns Object containing the Auth0 authorize URL
 *
 * @example
 * ```typescript
 * const response = await getLoginUrl();
 * window.location.href = response.data.authorize_url;
 * ```
 */
export async function getLoginUrl(
  redirectUri?: string,
): Promise<ApiResponse<{ authorize_url: string }>> {
  const params = redirectUri ? { redirect_uri: redirectUri } : {};
  return apiClient.get(`${AUTH_BASE}/login`, { params });
}

/**
 * Exchanges an OAuth authorization code for access and refresh tokens.
 *
 * Called after Auth0 redirects back to the application with an authorization
 * code. The backend exchanges this code with Auth0 for JWT tokens.
 *
 * @param code - OAuth authorization code from Auth0 redirect
 * @param state - OAuth state parameter for CSRF protection
 * @returns Login response containing access and refresh tokens
 *
 * @example
 * ```typescript
 * // In the OAuth callback handler
 * const urlParams = new URLSearchParams(window.location.search);
 * const response = await handleCallback(urlParams.get('code')!, urlParams.get('state')!);
 * authStore.getState().setTokens(
 *   response.data.access_token,
 *   response.data.refresh_token,
 *   response.data.expires_in,
 * );
 * ```
 */
export async function handleCallback(
  code: string,
  state: string,
): Promise<ApiResponse<LoginResponse>> {
  return apiClient.post(`${AUTH_BASE}/callback`, { code, state });
}

/**
 * Logs out the user by revoking the refresh token server-side.
 *
 * Sends a fire-and-forget request to the backend to revoke the refresh
 * token on Auth0. Local state cleanup is handled by `authStore.logout()`.
 *
 * @param refreshToken - Optional refresh token to revoke (omit if already cleared)
 * @returns Void response indicating successful logout
 */
export async function logout(
  refreshToken?: string,
): Promise<ApiResponse<void>> {
  return apiClient.post(`${AUTH_BASE}/logout`, {
    refresh_token: refreshToken,
  });
}

/**
 * Refreshes the access token using a valid refresh token.
 *
 * Implements OAuth2 refresh token rotation: the provided refresh token is
 * consumed and a new access/refresh token pair is returned. The previous
 * refresh token is invalidated.
 *
 * Used by the Axios response interceptor in `api.ts` on 401 responses
 * for automatic, transparent token refresh.
 *
 * @param request - Object containing the refresh token to exchange
 * @returns New access and refresh tokens with expiry information
 */
export async function refreshToken(
  request: TokenRefreshRequest,
): Promise<ApiResponse<TokenRefreshResponse>> {
  return apiClient.post(`${AUTH_BASE}/refresh`, request);
}

/**
 * Fetches the authenticated user's profile from their JWT claims.
 *
 * Returns the full user record including role, permissions, and tenant
 * context. Used by `authStore.fetchCurrentUser()` during initialization
 * and after token refresh.
 *
 * @returns Current user profile with role and permissions
 */
export async function getCurrentUser(): Promise<ApiResponse<User>> {
  return apiClient.get(`${AUTH_BASE}/me`);
}

/**
 * Updates the authenticated user's profile fields.
 *
 * Only the provided fields are modified; omitted fields retain their
 * current values. Currently supports updating `name` and `avatar_url`.
 *
 * @param updates - Object containing the fields to update
 * @returns Updated user profile
 */
export async function updateProfile(
  updates: UpdateProfileRequest,
): Promise<ApiResponse<User>> {
  return apiClient.put(`${AUTH_BASE}/me`, updates);
}

/**
 * Initiates a password reset email via Auth0.
 *
 * Sends a password reset request to the Auth0 Management API, which
 * dispatches a reset email to the specified address if an account exists.
 *
 * @param email - Email address of the user requesting password reset
 * @returns Confirmation message
 */
export async function requestPasswordReset(
  email: string,
): Promise<ApiResponse<{ message: string }>> {
  return apiClient.post(`${AUTH_BASE}/password-reset`, { email });
}
