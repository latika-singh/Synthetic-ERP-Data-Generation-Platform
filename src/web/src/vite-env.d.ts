/// <reference types="vite/client" />

/**
 * Vite Environment Type Declarations
 *
 * Provides TypeScript type information for Vite-specific features:
 *   - import.meta.env environment variables
 *   - Module resolution for non-TypeScript assets (CSS, SVG, etc.)
 *
 * This file is automatically included by TypeScript via the tsconfig.json
 * include pattern for src/*.d.ts.
 */

/**
 * Extend Vite ImportMetaEnv with application-specific environment variables.
 * All VITE_-prefixed variables defined in .env files are exposed here.
 */
interface ImportMetaEnv {
  /** Auth0 tenant domain */
  readonly VITE_AUTH0_DOMAIN: string;
  /** Auth0 application client ID */
  readonly VITE_AUTH0_CLIENT_ID: string;
  /** Auth0 API audience identifier */
  readonly VITE_AUTH0_AUDIENCE: string;
  /** Auth0 OAuth callback URL */
  readonly VITE_AUTH0_REDIRECT_URI: string;
  /** API Gateway base URL */
  readonly VITE_API_BASE_URL: string;
  /** WebSocket URL for real-time updates */
  readonly VITE_WS_URL: string;
  /** Current deployment environment */
  readonly MODE: string;
  /** Whether running in development mode */
  readonly DEV: boolean;
  /** Whether running in production mode */
  readonly PROD: boolean;
}

interface ImportMeta {
  readonly env: ImportMetaEnv;
}
