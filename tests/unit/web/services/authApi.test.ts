/**
 * @fileoverview Vitest unit tests for the authentication API service module.
 *
 * Covers all eight authentication API endpoint functions exported by
 * `src/web/src/services/authApi.ts`:
 *   - login             — POST /api/v1/auth/login (email/password credentials)
 *   - getLoginUrl       — GET  /api/v1/auth/login (Auth0 OAuth redirect URL)
 *   - handleCallback    — POST /api/v1/auth/callback (authorization code exchange)
 *   - logout            — POST /api/v1/auth/logout (refresh token revocation)
 *   - refreshToken      — POST /api/v1/auth/refresh (JWT token rotation)
 *   - getCurrentUser    — GET  /api/v1/auth/me (user profile from JWT)
 *   - updateProfile     — PUT  /api/v1/auth/me (display name / avatar update)
 *   - requestPasswordReset — POST /api/v1/auth/password-reset (Auth0 email trigger)
 *
 * Test strategy:
 *   - The shared Axios `apiClient` is mocked via `vi.mock` so no real HTTP
 *     requests are made.
 *   - Each test verifies the correct HTTP method, URL path, request payload,
 *     and typed response handling (LoginResponse, User, TokenRefreshResponse).
 *   - Error paths verify that rejected promises propagate correctly.
 *
 * @module tests/unit/web/services/authApi.test
 * @see src/web/src/services/authApi.ts
 */

import { describe, it, expect, vi, beforeEach } from 'vitest';

import type { ApiResponse } from '@/types/api';
import type { User, LoginResponse, TokenRefreshResponse } from '@/types/auth';
import { Permission, Role } from '@/types/auth';

// ============================================================================
// Module Mock — apiClient
// ============================================================================

/**
 * Mock the `@/services/api` module so that every call to `apiClient.get`,
 * `apiClient.post`, or `apiClient.put` is intercepted by a `vi.fn()` spy.
 *
 * The mock factory returns an object whose `apiClient` property exposes
 * three mock methods. This allows the authApi functions (which import
 * `apiClient` from `./api`) to invoke mocked HTTP calls while tests
 * inspect call arguments and control resolved/rejected values.
 */
vi.mock('@/services/api', () => ({
  apiClient: {
    get: vi.fn(),
    post: vi.fn(),
    put: vi.fn(),
  },
}));

/**
 * Import the mocked apiClient AFTER vi.mock hoisting so that the import
 * resolves to the mocked version rather than the real Axios instance.
 */
import { apiClient } from '@/services/api';

/**
 * Import all eight authApi functions and the two exported local type
 * interfaces (LoginRequest, UpdateProfileRequest) that are used to
 * construct typed test request payloads.
 */
import {
  login,
  getLoginUrl,
  handleCallback,
  logout,
  refreshToken,
  getCurrentUser,
  updateProfile,
  requestPasswordReset,
} from '@/services/authApi';
import type { LoginRequest, UpdateProfileRequest } from '@/services/authApi';

// ============================================================================
// Test Data Factories
// ============================================================================

/**
 * Constructs a mock LoginResponse matching the backend `LoginResponse`
 * Pydantic model. Used by login and handleCallback test suites.
 */
const mockLoginResponse: LoginResponse = {
  access_token: 'at-token-123',
  refresh_token: 'rt-token-456',
  token_type: 'Bearer',
  expires_in: 3600,
};

/**
 * Constructs a mock User matching the backend `UserProfile` Pydantic model.
 * Represents a DATA_ENGINEER-role user in tenant-1.
 */
const mockUser: User = {
  user_id: 'user-123',
  email: 'user@test.com',
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
  tenant_id: 'tenant-1',
  is_active: true,
  created_at: '2025-01-01T00:00:00Z',
};

/**
 * Constructs a mock TokenRefreshResponse with rotated tokens.
 * The access_token and refresh_token are intentionally different from the
 * request refresh_token to demonstrate token rotation.
 */
const mockTokenRefreshResponse: TokenRefreshResponse = {
  access_token: 'at-new-token-789',
  refresh_token: 'rt-new-token-012',
  token_type: 'Bearer',
  expires_in: 3600,
};

/**
 * Helper to wrap a payload in the standard ApiResponse envelope.
 *
 * @param data - The payload to wrap
 * @returns A fully-formed ApiResponse object
 */
function wrapResponse<T>(data: T): ApiResponse<T> {
  return {
    success: true,
    data,
    message: null,
    timestamp: '2025-01-01T00:00:00Z',
  };
}

// ============================================================================
// Test Lifecycle
// ============================================================================

/**
 * Reset all mock call history and implementation before each test to
 * ensure complete isolation between test cases.
 */
beforeEach(() => {
  vi.clearAllMocks();
});

// ============================================================================
// Test Suites
// ============================================================================

describe('authApi', () => {
  // --------------------------------------------------------------------------
  // 1. login
  // --------------------------------------------------------------------------

  describe('login', () => {
    it('should call POST /api/v1/auth/login with credentials', async () => {
      const credentials: LoginRequest = {
        email: 'user@test.com',
        password: 'securepass123',
      };

      const apiResponse = wrapResponse(mockLoginResponse);
      vi.mocked(apiClient.post).mockResolvedValueOnce(apiResponse);

      await login(credentials);

      expect(apiClient.post).toHaveBeenCalledTimes(1);
      expect(apiClient.post).toHaveBeenCalledWith('/api/v1/auth/login', {
        email: 'user@test.com',
        password: 'securepass123',
      });
    });

    it('should return LoginResponse with tokens', async () => {
      const credentials: LoginRequest = {
        email: 'user@test.com',
        password: 'securepass123',
      };

      const apiResponse = wrapResponse(mockLoginResponse);
      vi.mocked(apiClient.post).mockResolvedValueOnce(apiResponse);

      const result = await login(credentials);

      expect(result).toEqual(apiResponse);
      expect(result.success).toBe(true);
      expect(result.data.access_token).toBe('at-token-123');
      expect(result.data.refresh_token).toBe('rt-token-456');
      expect(result.data.token_type).toBe('Bearer');
      expect(result.data.expires_in).toBe(3600);
    });

    it('should handle invalid credentials error', async () => {
      const credentials: LoginRequest = {
        email: 'user@test.com',
        password: 'wrongpassword',
      };

      const error = {
        response: { status: 401, data: { message: 'Invalid credentials' } },
        message: 'Request failed with status code 401',
      };
      vi.mocked(apiClient.post).mockRejectedValueOnce(error);

      await expect(login(credentials)).rejects.toEqual(error);
      expect(apiClient.post).toHaveBeenCalledWith('/api/v1/auth/login', {
        email: 'user@test.com',
        password: 'wrongpassword',
      });
    });
  });

  // --------------------------------------------------------------------------
  // 2. getLoginUrl
  // --------------------------------------------------------------------------

  describe('getLoginUrl', () => {
    it('should call GET /api/v1/auth/login with default redirectUri', async () => {
      const apiResponse = wrapResponse({
        authorize_url: 'https://test.auth0.com/authorize?redirect_uri=http%3A%2F%2Flocalhost%2Fcallback',
      });
      vi.mocked(apiClient.get).mockResolvedValueOnce(apiResponse);

      await getLoginUrl();

      expect(apiClient.get).toHaveBeenCalledTimes(1);
      expect(apiClient.get).toHaveBeenCalledWith(
        '/api/v1/auth/login',
        expect.objectContaining({
          params: expect.any(Object),
        }),
      );
    });

    it('should call GET /api/v1/auth/login with custom redirectUri', async () => {
      const customRedirectUri = 'https://custom.com/callback';
      const apiResponse = wrapResponse({
        authorize_url: `https://test.auth0.com/authorize?redirect_uri=${encodeURIComponent(customRedirectUri)}`,
      });
      vi.mocked(apiClient.get).mockResolvedValueOnce(apiResponse);

      await getLoginUrl(customRedirectUri);

      expect(apiClient.get).toHaveBeenCalledTimes(1);
      expect(apiClient.get).toHaveBeenCalledWith('/api/v1/auth/login', {
        params: { redirect_uri: 'https://custom.com/callback' },
      });
    });

    it('should return authorize URL', async () => {
      const expectedUrl = 'https://test.auth0.com/authorize?client_id=test&redirect_uri=http%3A%2F%2Flocalhost%2Fcallback';
      const apiResponse = wrapResponse({ authorize_url: expectedUrl });
      vi.mocked(apiClient.get).mockResolvedValueOnce(apiResponse);

      const result = await getLoginUrl('http://localhost/callback');

      expect(result).toEqual(apiResponse);
      expect(result.success).toBe(true);
      expect(result.data.authorize_url).toBe(expectedUrl);
    });
  });

  // --------------------------------------------------------------------------
  // 3. handleCallback
  // --------------------------------------------------------------------------

  describe('handleCallback', () => {
    it('should call POST /api/v1/auth/callback with code and state', async () => {
      const apiResponse = wrapResponse(mockLoginResponse);
      vi.mocked(apiClient.post).mockResolvedValueOnce(apiResponse);

      await handleCallback('auth-code-123', 'state-xyz');

      expect(apiClient.post).toHaveBeenCalledTimes(1);
      expect(apiClient.post).toHaveBeenCalledWith('/api/v1/auth/callback', {
        code: 'auth-code-123',
        state: 'state-xyz',
      });
    });

    it('should return LoginResponse with tokens', async () => {
      const apiResponse = wrapResponse(mockLoginResponse);
      vi.mocked(apiClient.post).mockResolvedValueOnce(apiResponse);

      const result = await handleCallback('auth-code-123', 'state-xyz');

      expect(result).toEqual(apiResponse);
      expect(result.success).toBe(true);
      expect(result.data.access_token).toBe('at-token-123');
      expect(result.data.refresh_token).toBe('rt-token-456');
      expect(result.data.token_type).toBe('Bearer');
      expect(result.data.expires_in).toBe(3600);
    });

    it('should handle invalid/expired authorization code', async () => {
      const error = {
        response: { status: 400, data: { message: 'Invalid or expired authorization code' } },
        message: 'Request failed with status code 400',
      };
      vi.mocked(apiClient.post).mockRejectedValueOnce(error);

      await expect(handleCallback('expired-code', 'state-xyz')).rejects.toEqual(error);
      expect(apiClient.post).toHaveBeenCalledWith('/api/v1/auth/callback', {
        code: 'expired-code',
        state: 'state-xyz',
      });
    });
  });

  // --------------------------------------------------------------------------
  // 4. logout
  // --------------------------------------------------------------------------

  describe('logout', () => {
    it('should call POST /api/v1/auth/logout with refresh token', async () => {
      const apiResponse = wrapResponse(undefined as unknown as void);
      vi.mocked(apiClient.post).mockResolvedValueOnce(apiResponse);

      await logout('rt-token-123');

      expect(apiClient.post).toHaveBeenCalledTimes(1);
      expect(apiClient.post).toHaveBeenCalledWith('/api/v1/auth/logout', {
        refresh_token: 'rt-token-123',
      });
    });

    it('should call POST /api/v1/auth/logout without refresh token', async () => {
      const apiResponse = wrapResponse(undefined as unknown as void);
      vi.mocked(apiClient.post).mockResolvedValueOnce(apiResponse);

      await logout();

      expect(apiClient.post).toHaveBeenCalledTimes(1);
      expect(apiClient.post).toHaveBeenCalledWith('/api/v1/auth/logout', {
        refresh_token: undefined,
      });
    });

    it('should handle successful logout', async () => {
      const apiResponse: ApiResponse<void> = {
        success: true,
        data: undefined as unknown as void,
        message: 'Logged out successfully',
        timestamp: '2025-01-01T00:00:00Z',
      };
      vi.mocked(apiClient.post).mockResolvedValueOnce(apiResponse);

      const result = await logout('rt-token-123');

      expect(result).toEqual(apiResponse);
      expect(result.success).toBe(true);
    });
  });

  // --------------------------------------------------------------------------
  // 5. refreshToken
  // --------------------------------------------------------------------------

  describe('refreshToken', () => {
    it('should call POST /api/v1/auth/refresh with refresh token', async () => {
      const apiResponse = wrapResponse(mockTokenRefreshResponse);
      vi.mocked(apiClient.post).mockResolvedValueOnce(apiResponse);

      await refreshToken({ refresh_token: 'rt-token-456' });

      expect(apiClient.post).toHaveBeenCalledTimes(1);
      expect(apiClient.post).toHaveBeenCalledWith('/api/v1/auth/refresh', {
        refresh_token: 'rt-token-456',
      });
    });

    it('should return new tokens with rotation', async () => {
      const apiResponse = wrapResponse(mockTokenRefreshResponse);
      vi.mocked(apiClient.post).mockResolvedValueOnce(apiResponse);

      const result = await refreshToken({ refresh_token: 'rt-token-456' });

      expect(result).toEqual(apiResponse);
      expect(result.success).toBe(true);
      // New tokens must differ from the request refresh token to prove rotation
      expect(result.data.access_token).toBe('at-new-token-789');
      expect(result.data.refresh_token).toBe('rt-new-token-012');
      expect(result.data.refresh_token).not.toBe('rt-token-456');
      expect(result.data.token_type).toBe('Bearer');
      expect(result.data.expires_in).toBe(3600);
    });

    it('should handle expired refresh token', async () => {
      const error = {
        response: { status: 401, data: { message: 'Refresh token expired or revoked' } },
        message: 'Request failed with status code 401',
      };
      vi.mocked(apiClient.post).mockRejectedValueOnce(error);

      await expect(
        refreshToken({ refresh_token: 'expired-rt-token' }),
      ).rejects.toEqual(error);
      expect(apiClient.post).toHaveBeenCalledWith('/api/v1/auth/refresh', {
        refresh_token: 'expired-rt-token',
      });
    });
  });

  // --------------------------------------------------------------------------
  // 6. getCurrentUser
  // --------------------------------------------------------------------------

  describe('getCurrentUser', () => {
    it('should call GET /api/v1/auth/me', async () => {
      const apiResponse = wrapResponse(mockUser);
      vi.mocked(apiClient.get).mockResolvedValueOnce(apiResponse);

      await getCurrentUser();

      expect(apiClient.get).toHaveBeenCalledTimes(1);
      expect(apiClient.get).toHaveBeenCalledWith('/api/v1/auth/me');
    });

    it('should return User profile', async () => {
      const apiResponse = wrapResponse(mockUser);
      vi.mocked(apiClient.get).mockResolvedValueOnce(apiResponse);

      const result = await getCurrentUser();

      expect(result).toEqual(apiResponse);
      expect(result.success).toBe(true);
      expect(result.data.user_id).toBe('user-123');
      expect(result.data.email).toBe('user@test.com');
      expect(result.data.name).toBe('Test User');
      expect(result.data.role).toBe('data_engineer');
      expect(result.data.permissions).toBeInstanceOf(Array);
      expect(result.data.permissions.length).toBeGreaterThan(0);
      expect(result.data.permissions).toContain(Permission.GENERATION_CREATE);
      expect(result.data.permissions).toContain(Permission.GENERATION_READ);
      expect(result.data.tenant_id).toBe('tenant-1');
      expect(result.data.is_active).toBe(true);
      expect(result.data.created_at).toBe('2025-01-01T00:00:00Z');
    });

    it('should handle unauthorized error', async () => {
      const error = {
        response: { status: 401, data: { message: 'Authentication required' } },
        message: 'Request failed with status code 401',
      };
      vi.mocked(apiClient.get).mockRejectedValueOnce(error);

      await expect(getCurrentUser()).rejects.toEqual(error);
      expect(apiClient.get).toHaveBeenCalledWith('/api/v1/auth/me');
    });
  });

  // --------------------------------------------------------------------------
  // 7. updateProfile
  // --------------------------------------------------------------------------

  describe('updateProfile', () => {
    it('should call PUT /api/v1/auth/me with profile updates', async () => {
      const updates: UpdateProfileRequest = {
        name: 'Updated Name',
        avatar_url: 'https://example.com/avatar.png',
      };

      const updatedUser: User = {
        ...mockUser,
        name: 'Updated Name',
        avatar_url: 'https://example.com/avatar.png',
      };
      const apiResponse = wrapResponse(updatedUser);
      vi.mocked(apiClient.put).mockResolvedValueOnce(apiResponse);

      await updateProfile(updates);

      expect(apiClient.put).toHaveBeenCalledTimes(1);
      expect(apiClient.put).toHaveBeenCalledWith('/api/v1/auth/me', {
        name: 'Updated Name',
        avatar_url: 'https://example.com/avatar.png',
      });
    });

    it('should return updated User', async () => {
      const updates: UpdateProfileRequest = {
        name: 'Updated Name',
        avatar_url: 'https://example.com/avatar.png',
      };

      const updatedUser: User = {
        ...mockUser,
        name: 'Updated Name',
        avatar_url: 'https://example.com/avatar.png',
      };
      const apiResponse = wrapResponse(updatedUser);
      vi.mocked(apiClient.put).mockResolvedValueOnce(apiResponse);

      const result = await updateProfile(updates);

      expect(result).toEqual(apiResponse);
      expect(result.success).toBe(true);
      expect(result.data.name).toBe('Updated Name');
      expect(result.data.avatar_url).toBe('https://example.com/avatar.png');
      expect(result.data.user_id).toBe('user-123');
      expect(result.data.email).toBe('user@test.com');
      expect(result.data.role).toBe('data_engineer');
    });

    it('should support partial updates with only name', async () => {
      const updates: UpdateProfileRequest = {
        name: 'Only Name Updated',
      };

      const updatedUser: User = {
        ...mockUser,
        name: 'Only Name Updated',
      };
      const apiResponse = wrapResponse(updatedUser);
      vi.mocked(apiClient.put).mockResolvedValueOnce(apiResponse);

      await updateProfile(updates);

      expect(apiClient.put).toHaveBeenCalledWith('/api/v1/auth/me', {
        name: 'Only Name Updated',
      });
    });
  });

  // --------------------------------------------------------------------------
  // 8. requestPasswordReset
  // --------------------------------------------------------------------------

  describe('requestPasswordReset', () => {
    it('should call POST /api/v1/auth/password-reset with email', async () => {
      const apiResponse = wrapResponse({ message: 'Password reset email sent' });
      vi.mocked(apiClient.post).mockResolvedValueOnce(apiResponse);

      await requestPasswordReset('user@test.com');

      expect(apiClient.post).toHaveBeenCalledTimes(1);
      expect(apiClient.post).toHaveBeenCalledWith('/api/v1/auth/password-reset', {
        email: 'user@test.com',
      });
    });

    it('should return confirmation message', async () => {
      const apiResponse = wrapResponse({ message: 'Password reset email sent' });
      vi.mocked(apiClient.post).mockResolvedValueOnce(apiResponse);

      const result = await requestPasswordReset('user@test.com');

      expect(result).toEqual(apiResponse);
      expect(result.success).toBe(true);
      expect(result.data.message).toBe('Password reset email sent');
    });

    it('should handle non-existent email gracefully', async () => {
      // The backend should return a success response regardless of whether the
      // email exists to prevent user enumeration (no information leakage).
      const apiResponse = wrapResponse({ message: 'Password reset email sent' });
      vi.mocked(apiClient.post).mockResolvedValueOnce(apiResponse);

      const result = await requestPasswordReset('nonexistent@test.com');

      expect(result).toEqual(apiResponse);
      expect(result.success).toBe(true);
      expect(result.data.message).toBe('Password reset email sent');
      expect(apiClient.post).toHaveBeenCalledWith('/api/v1/auth/password-reset', {
        email: 'nonexistent@test.com',
      });
    });
  });
});
