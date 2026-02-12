/**
 * @fileoverview Centralized Axios HTTP client for the Synthetic ERP Data Generation Platform.
 *
 * Creates and exports a pre-configured Axios instance (`apiClient`) that serves as
 * the single HTTP communication layer between the React Web Console and the Flask
 * API Gateway. Every API service module (generationApi, profileApi, authApi, adminApi)
 * imports this client, making it the most foundational service file in the frontend.
 *
 * Features:
 * - Base URL configuration via VITE_API_BASE_URL environment variable (R-012)
 * - JWT Bearer token injection from Zustand authStore (R-006)
 * - Multi-tenant X-Tenant-ID header injection (R-007)
 * - X-Request-ID correlation header for distributed tracing
 * - Automatic 401 token refresh with concurrent request queuing
 * - Rate limiting (429) handling with Retry-After support
 * - Structured error transformation to typed ApiError objects
 * - Development-mode request/response logging
 * - 30-second default request timeout
 *
 * Circular Dependency Resolution:
 * This module lazily loads the auth store via dynamic `import()` to break the
 * circular dependency chain: api.ts → authStore → authApi → api.ts.
 * The dynamic import is cached after first resolution, ensuring negligible
 * overhead on subsequent requests.
 *
 * @module services/api
 * @version 1.0.0
 */

import axios from 'axios';
import type {
  AxiosInstance,
  AxiosRequestConfig,
  AxiosResponse,
  AxiosError,
  InternalAxiosRequestConfig,
} from 'axios';
import type { ApiError, ApiResponse } from '@/types/api';

// ============================================================================
// Constants
// ============================================================================

/**
 * Base URL for all API requests.
 *
 * - Development: Empty string (Vite dev server proxy handles /api/v1/ routing)
 * - Production: Full URL (e.g., 'https://api.synthetic-erp.example.com')
 *
 * Configured via the VITE_API_BASE_URL environment variable per 12-factor app
 * methodology. Uses URL-path versioning (/api/v1/) as required by R-012.
 */
const API_BASE_URL: string = import.meta.env.VITE_API_BASE_URL || '';

/**
 * Default request timeout in milliseconds (30 seconds).
 * Individual requests can override this via RequestConfig.timeout.
 */
const REQUEST_TIMEOUT = 30000;

/**
 * Maximum number of retry attempts for failed requests.
 * Used primarily for token refresh retry logic.
 */
const MAX_RETRY_ATTEMPTS = 3;

/**
 * Endpoint for JWT token refresh via Auth0 token rotation.
 * Called automatically on 401 responses to obtain new access/refresh token pairs.
 */
const TOKEN_REFRESH_ENDPOINT = '/api/v1/auth/refresh';

// ============================================================================
// Auth Store Lazy Accessor (Circular Dependency Resolution)
// ============================================================================

/**
 * Local interface matching the subset of AuthStoreState needed by interceptors.
 * Defined locally to avoid importing authStore directly (which would create a
 * circular dependency: api.ts → authStore → authApi → api.ts).
 */
interface AuthStoreSlice {
  /** Current JWT access token, or null if unauthenticated */
  accessToken: string | null;
  /** Current JWT refresh token for token rotation */
  refreshToken: string | null;
  /** Authenticated user profile with tenant context */
  user: { tenant_id?: string } | null;
  /** Updates stored tokens after successful refresh */
  setTokens: (accessToken: string, refreshToken: string, expiresIn: number) => void;
  /** Clears all auth state (triggers re-login) */
  reset: () => void;
}

/**
 * Cached reference to the auth store module, populated on first access.
 * Using `any` for the module type because the authStore module is loaded
 * dynamically and its exact type shape is defined by AuthStoreSlice above.
 */
// eslint-disable-next-line @typescript-eslint/no-explicit-any
let _cachedAuthStoreRef: { getState: () => AuthStoreSlice } | null = null;

/**
 * Lazily loads and caches the Zustand auth store module.
 *
 * Uses dynamic `import()` to break the circular dependency chain:
 *   api.ts → authStore.ts → authApi.ts → api.ts
 *
 * After the first successful import, the store reference is cached in
 * `_cachedAuthStoreRef` so subsequent calls resolve synchronously from cache.
 * This is safe because Zustand stores are module-scoped singletons.
 *
 * @returns The auth store's current state, or a fallback empty state if unavailable
 */
async function resolveAuthStore(): Promise<AuthStoreSlice> {
  if (!_cachedAuthStoreRef) {
    try {
      const mod = await import('../store/authStore');
      // Support both named export (useAuthStore) and default export
      _cachedAuthStoreRef = mod.useAuthStore || mod.default;
    } catch {
      // Auth store not yet available (e.g., during initial module load)
      // Return a safe default — interceptors will function without auth headers
      return {
        accessToken: null,
        refreshToken: null,
        user: null,
        setTokens: () => {},
        reset: () => {},
      };
    }
  }
  return _cachedAuthStoreRef!.getState();
}

/**
 * Synchronous accessor for the cached auth store state.
 *
 * Returns null if the auth store module has not been loaded yet (i.e.,
 * `resolveAuthStore()` has not been called). Used by `getAuthHeaders()`
 * for synchronous header retrieval (e.g., WebSocket connections).
 *
 * @returns Current auth state from cached store, or null if not yet loaded
 */
function getAuthStateSync(): AuthStoreSlice | null {
  if (!_cachedAuthStoreRef) {
    return null;
  }
  return _cachedAuthStoreRef.getState();
}

// ============================================================================
// Token Refresh Queue
// ============================================================================

/**
 * Flag indicating whether a token refresh is currently in progress.
 * Prevents multiple concurrent refresh requests when several API calls
 * simultaneously receive 401 responses.
 */
let isRefreshing = false;

/**
 * Queue of pending requests waiting for token refresh completion.
 * When a 401 is received and a refresh is already in progress, subsequent
 * 401 responses queue their retry callbacks here instead of triggering
 * additional refresh requests.
 */
let failedQueue: Array<{
  resolve: (value: string | null) => void;
  reject: (reason?: unknown) => void;
}> = [];

/**
 * Processes all queued requests after a token refresh attempt completes.
 *
 * On success (token is provided): resolves each queued promise with the new token,
 * causing the original requests to retry with updated Authorization headers.
 *
 * On failure (error is provided): rejects each queued promise, propagating the
 * refresh failure to all pending requests.
 *
 * @param error - The refresh error (null if refresh succeeded)
 * @param token - The new access token (null if refresh failed)
 */
function processQueue(error: AxiosError | null, token: string | null): void {
  failedQueue.forEach(({ resolve, reject }) => {
    if (error) {
      reject(error);
    } else {
      resolve(token);
    }
  });
  failedQueue = [];
}

// ============================================================================
// Request ID Generation
// ============================================================================

/**
 * Generates a UUID v4 string for the X-Request-ID correlation header.
 *
 * Prefers the native `crypto.randomUUID()` API when available (modern browsers).
 * Falls back to a Math.random()-based implementation for older environments.
 *
 * @returns A UUID v4 string (e.g., '550e8400-e29b-41d4-a716-446655440000')
 */
function generateRequestId(): string {
  if (typeof crypto !== 'undefined' && typeof crypto.randomUUID === 'function') {
    return crypto.randomUUID();
  }
  // RFC 4122 v4 UUID fallback using Math.random()
  return 'xxxxxxxx-xxxx-4xxx-yxxx-xxxxxxxxxxxx'.replace(/[xy]/g, (char) => {
    const random = (Math.random() * 16) | 0;
    const value = char === 'x' ? random : (random & 0x3) | 0x8;
    return value.toString(16);
  });
}

// ============================================================================
// Error Transformation
// ============================================================================

/**
 * Transforms an Axios error into a typed ApiError object matching the backend
 * error_handler.py middleware response format.
 *
 * Extracts structured error fields from the response data when available,
 * falling back to sensible defaults for network errors and timeouts.
 *
 * @param error - The Axios error to transform
 * @returns A typed ApiError object with status_code, error_code, message, details, timestamp, request_id
 */
function transformError(error: AxiosError): ApiError {
  const responseData = error.response?.data as Record<string, unknown> | undefined;
  const responseHeaders = error.response?.headers as Record<string, string> | undefined;

  // Determine appropriate error code based on error type
  let errorCode = 'UNKNOWN_ERROR';
  if (responseData?.error_code) {
    errorCode = String(responseData.error_code);
  } else if (!error.response) {
    errorCode = error.code === 'ECONNABORTED' ? 'REQUEST_TIMEOUT' : 'NETWORK_ERROR';
  } else if (error.response.status === 401) {
    errorCode = 'AUTHENTICATION_REQUIRED';
  } else if (error.response.status === 403) {
    errorCode = 'FORBIDDEN';
  } else if (error.response.status === 404) {
    errorCode = 'RESOURCE_NOT_FOUND';
  } else if (error.response.status === 422) {
    errorCode = 'VALIDATION_ERROR';
  } else if (error.response.status === 429) {
    errorCode = 'RATE_LIMIT_EXCEEDED';
  } else if (error.response.status === 503) {
    errorCode = 'SERVICE_UNAVAILABLE';
  } else if (error.response.status >= 500) {
    errorCode = 'INTERNAL_ERROR';
  }

  // Determine human-readable message
  let message = 'An unexpected error occurred';
  if (responseData?.message && typeof responseData.message === 'string') {
    message = responseData.message;
  } else if (error.code === 'ECONNABORTED') {
    message = 'Request timed out. Please try again.';
  } else if (!error.response) {
    message = 'Unable to connect to the server. Please check your network connection.';
  } else if (error.message) {
    message = error.message;
  }

  const apiError: ApiError = {
    status_code: error.response?.status || 500,
    error_code: errorCode,
    message,
    details: (responseData?.details as Record<string, unknown>) || null,
    timestamp: (responseData?.timestamp as string) || new Date().toISOString(),
    request_id: responseHeaders?.['x-request-id'] || null,
  };

  return apiError;
}

// ============================================================================
// Axios Instance Creation
// ============================================================================

/**
 * Pre-configured Axios HTTP client instance for the Synthetic ERP Data Generation Platform.
 *
 * This is the primary export of this module. All API service modules (generationApi,
 * profileApi, authApi, adminApi) import this instance to make HTTP requests to the
 * backend API Gateway.
 *
 * Configuration:
 * - `baseURL`: Configurable via VITE_API_BASE_URL (empty string for Vite dev proxy)
 * - `timeout`: 30 seconds default
 * - `headers`: JSON Content-Type and Accept headers
 * - `withCredentials`: false (JWT tokens sent via Authorization header, not cookies)
 *
 * Interceptors:
 * - Request: JWT injection, tenant ID, request correlation ID, dev logging
 * - Response: Data unwrapping, dev logging, 401 refresh, 429 handling, error transform
 */
export const apiClient: AxiosInstance = axios.create({
  baseURL: API_BASE_URL,
  timeout: REQUEST_TIMEOUT,
  headers: {
    'Content-Type': 'application/json',
    'Accept': 'application/json',
  },
  withCredentials: false,
});

// ============================================================================
// Request Interceptor — JWT Injection, Tenant Context, Correlation ID
// ============================================================================

apiClient.interceptors.request.use(
  async (config: InternalAxiosRequestConfig): Promise<InternalAxiosRequestConfig> => {
    // Resolve auth store (lazy dynamic import, cached after first call)
    const authState = await resolveAuthStore();

    // Inject JWT Bearer token for authenticated API requests (R-006)
    if (authState.accessToken) {
      config.headers.set('Authorization', `Bearer ${authState.accessToken}`);
    }

    // Inject tenant namespace for multi-tenant isolation (R-007)
    if (authState.user?.tenant_id) {
      config.headers.set('X-Tenant-ID', authState.user.tenant_id);
    }

    // Inject unique request correlation ID for distributed tracing (OpenTelemetry)
    config.headers.set('X-Request-ID', generateRequestId());

    // Development-mode request logging
    if (import.meta.env.DEV) {
      const method = (config.method || 'GET').toUpperCase();
      const url = config.url || '';
      const params = config.params ? JSON.stringify(config.params) : '';
      console.log(`[API Request] ${method} ${url}`, params);
    }

    return config;
  },
  (error: unknown): Promise<never> => {
    return Promise.reject(error);
  }
);

// ============================================================================
// Response Interceptor — Data Unwrapping, Token Refresh, Error Handling
// ============================================================================

apiClient.interceptors.response.use(
  /**
   * Success handler: Unwraps Axios response to return `response.data` directly.
   *
   * This means all API calls via `apiClient` receive the response body as their
   * resolved value (typically an `ApiResponse<T>`), rather than the full AxiosResponse
   * wrapper. This simplifies consumer code:
   *
   *   // Instead of: const result = response.data.data
   *   // Consumers get: const result = response.data
   */
  (response: AxiosResponse): ApiResponse<unknown> => {
    // Development-mode response logging
    if (import.meta.env.DEV) {
      const status = response.status;
      const url = response.config.url || '';
      console.log(`[API Response] ${status} ${url}`);
    }

    // Unwrap AxiosResponse to return the payload directly
    return response.data;
  },

  /**
   * Error handler: Automatic token refresh on 401, rate limit handling on 429,
   * and structured error transformation for all other error responses.
   */
  async (error: AxiosError): Promise<never> => {
    const originalRequest = error.config as
      | (InternalAxiosRequestConfig & { _retry?: boolean; _retryCount?: number })
      | undefined;

    // ── 401 Unauthorized — Attempt Token Refresh ────────────────────────
    const retryCount = originalRequest?._retryCount ?? 0;
    if (
      error.response?.status === 401 &&
      originalRequest &&
      !originalRequest._retry &&
      retryCount < MAX_RETRY_ATTEMPTS
    ) {
      // If another refresh is already in progress, queue this request
      if (isRefreshing) {
        return new Promise<string | null>((resolve, reject) => {
          failedQueue.push({ resolve, reject });
        }).then((newToken) => {
          if (newToken && originalRequest) {
            originalRequest.headers.set('Authorization', `Bearer ${newToken}`);
            return apiClient.request(originalRequest);
          }
          return Promise.reject(transformError(error));
        }) as Promise<never>;
      }

      // Mark as retrying and increment count to prevent infinite refresh loops
      originalRequest._retry = true;
      originalRequest._retryCount = retryCount + 1;
      isRefreshing = true;

      try {
        const authState = await resolveAuthStore();
        const currentRefreshToken = authState.refreshToken;

        // No refresh token available — force re-login
        if (!currentRefreshToken) {
          authState.reset();
          processQueue(error, null);
          redirectToLogin();
          return Promise.reject(transformError(error));
        }

        // Call the refresh endpoint directly using a plain axios call
        // (bypasses this interceptor to avoid infinite loops)
        const refreshResponse = await axios.post<{
          data: { access_token: string; refresh_token: string; expires_in: number };
        }>(
          `${API_BASE_URL}${TOKEN_REFRESH_ENDPOINT}`,
          { refresh_token: currentRefreshToken },
          {
            headers: {
              'Content-Type': 'application/json',
              'Accept': 'application/json',
            },
            timeout: REQUEST_TIMEOUT,
          }
        );

        // Extract new tokens from response (support both wrapped and unwrapped formats)
        const tokenData = refreshResponse.data?.data || refreshResponse.data;
        const {
          access_token: newAccessToken,
          refresh_token: newRefreshToken,
          expires_in: expiresIn,
        } = tokenData as { access_token: string; refresh_token: string; expires_in: number };

        // Update the auth store with refreshed tokens
        authState.setTokens(newAccessToken, newRefreshToken, expiresIn);

        // Resolve all queued requests with the new token
        processQueue(null, newAccessToken);

        // Retry the original request with the new access token
        originalRequest.headers.set('Authorization', `Bearer ${newAccessToken}`);
        return apiClient.request(originalRequest) as Promise<never>;
      } catch (refreshError: unknown) {
        // Token refresh failed — clear auth and redirect to login
        processQueue(error, null);

        try {
          const authState = await resolveAuthStore();
          authState.reset();
        } catch {
          // Auth store unavailable — continue with redirect
        }

        redirectToLogin();
        return Promise.reject(transformError(error));
      } finally {
        isRefreshing = false;
      }
    }

    // ── 429 Rate Limited — Extract Retry-After ──────────────────────────
    if (error.response?.status === 429) {
      const apiError = transformError(error);
      const retryAfterHeader = error.response.headers['retry-after'];

      if (retryAfterHeader) {
        const retryAfterSeconds = parseInt(String(retryAfterHeader), 10);
        apiError.details = {
          ...(apiError.details || {}),
          retry_after_seconds: Number.isNaN(retryAfterSeconds) ? 60 : retryAfterSeconds,
        };
      }

      if (import.meta.env.DEV) {
        console.warn(
          `[API Rate Limited] ${error.config?.url} — Retry after ${
            (apiError.details as Record<string, unknown>)?.retry_after_seconds ?? 'unknown'
          }s`
        );
      }

      return Promise.reject(apiError);
    }

    // ── All Other Errors — Transform and Reject ─────────────────────────
    const apiError = transformError(error);

    if (import.meta.env.DEV) {
      console.error(
        `[API Error] ${apiError.status_code} ${apiError.error_code}: ${apiError.message}`,
        apiError.details || ''
      );
    }

    return Promise.reject(apiError);
  }
);

// ============================================================================
// Navigation Helper
// ============================================================================

/**
 * Redirects the browser to the login page.
 *
 * Used when token refresh fails or no refresh token is available,
 * forcing the user to re-authenticate via Auth0.
 */
function redirectToLogin(): void {
  if (typeof window !== 'undefined') {
    window.location.href = '/login';
  }
}

// ============================================================================
// Exported Helper Functions
// ============================================================================

/**
 * Factory function for creating typed ApiError objects.
 *
 * Useful for creating consistent error objects in API service modules
 * when custom error conditions are detected (e.g., client-side validation
 * failures, data format mismatches).
 *
 * @param status_code - HTTP status code for the error
 * @param message - Human-readable error description
 * @param error_code - Machine-readable error code (defaults to 'UNKNOWN_ERROR')
 * @returns A fully populated ApiError object
 *
 * @example
 * ```typescript
 * throw createApiError(400, 'Invalid generation parameters', 'VALIDATION_ERROR');
 * ```
 */
export function createApiError(
  status_code: number,
  message: string,
  error_code?: string
): ApiError {
  return {
    status_code,
    error_code: error_code || 'UNKNOWN_ERROR',
    message,
    details: null,
    timestamp: new Date().toISOString(),
    request_id: null,
  };
}

/**
 * Type guard function for checking if an unknown error is a typed ApiError.
 *
 * Validates the structural shape of the error object against the ApiError
 * interface, ensuring all required fields (status_code, error_code, message)
 * are present with correct types.
 *
 * @param error - The unknown error to type-check
 * @returns True if the error conforms to the ApiError interface
 *
 * @example
 * ```typescript
 * try {
 *   await apiClient.get('/api/v1/generation/jobs');
 * } catch (error) {
 *   if (isApiError(error)) {
 *     console.error(`API Error [${error.error_code}]: ${error.message}`);
 *   }
 * }
 * ```
 */
export function isApiError(error: unknown): error is ApiError {
  if (typeof error !== 'object' || error === null) {
    return false;
  }

  const candidate = error as Record<string, unknown>;

  return (
    typeof candidate.status_code === 'number' &&
    typeof candidate.error_code === 'string' &&
    typeof candidate.message === 'string'
  );
}

/**
 * Returns current authentication headers for manual HTTP requests.
 *
 * Primarily used for connections that bypass the Axios client, such as
 * WebSocket connections for real-time job progress tracking, or manual
 * fetch() calls for streaming responses.
 *
 * Uses the synchronously cached auth store reference. If the auth store
 * has not been loaded yet (no prior API calls), returns empty headers.
 *
 * @returns A record of authentication headers (Authorization, X-Tenant-ID)
 *
 * @example
 * ```typescript
 * const ws = new WebSocket('wss://api.example.com/ws/jobs');
 * const headers = getAuthHeaders();
 * // Use headers for WebSocket authentication handshake
 * ```
 */
export function getAuthHeaders(): Record<string, string> {
  const headers: Record<string, string> = {};
  const authState = getAuthStateSync();

  if (authState?.accessToken) {
    headers['Authorization'] = `Bearer ${authState.accessToken}`;
  }

  if (authState?.user?.tenant_id) {
    headers['X-Tenant-ID'] = authState.user.tenant_id;
  }

  return headers;
}

// ============================================================================
// Default Export
// ============================================================================

export default apiClient;
