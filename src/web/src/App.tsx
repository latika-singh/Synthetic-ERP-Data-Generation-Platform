/**
 * Root React Component — Application Shell
 *
 * Initializes all application-level providers and routing for the
 * Synthetic ERP Data Generation Platform Web Console:
 *   - React.StrictMode for development warnings and double-render detection
 *   - ErrorBoundary for graceful error handling with fallback UI
 *   - Auth0Provider for OAuth 2.0 / OpenID Connect authentication
 *   - React Suspense with loading fallback for lazy-loaded page components
 *   - React Router 6.x RouterProvider for client-side navigation
 *
 * Environment variables (via import.meta.env):
 *   - VITE_AUTH0_DOMAIN: Auth0 tenant domain
 *   - VITE_AUTH0_CLIENT_ID: Auth0 application client ID
 *   - VITE_AUTH0_AUDIENCE: Auth0 API audience identifier
 *   - VITE_AUTH0_REDIRECT_URI: OAuth callback URL (defaults to window.location.origin)
 *
 * Reference: Agent Action Plan Section 0.3.1 — src/web/src/App.tsx
 */

import React, { Suspense } from 'react';
import { createBrowserRouter, RouterProvider } from 'react-router-dom';
import { Auth0Provider } from '@auth0/auth0-react';
import { routes } from './routes';
import ErrorBoundary from './components/common/ErrorBoundary';
import LoadingSpinner from './components/common/LoadingSpinner';

/**
 * Auth0 configuration from environment variables.
 * These are injected by Vite at build time via import.meta.env.
 * Default values are provided for local development without Auth0.
 */
const auth0Domain = import.meta.env.VITE_AUTH0_DOMAIN || 'dev-placeholder.auth0.com';
const auth0ClientId = import.meta.env.VITE_AUTH0_CLIENT_ID || 'placeholder-client-id';
const auth0Audience = import.meta.env.VITE_AUTH0_AUDIENCE || 'https://api.synthetic-erp.local';
const auth0RedirectUri =
  import.meta.env.VITE_AUTH0_REDIRECT_URI || window.location.origin;

/**
 * Create the React Router 6.x data router from the route configuration.
 * Uses createBrowserRouter for the newer data router API which supports:
 *   - Data loading (loaders) and mutations (actions) per route
 *   - Automatic error boundaries per route segment
 *   - Pending UI states during navigation
 */
const router = createBrowserRouter(routes);

/**
 * App Component
 *
 * Root component rendered by main.tsx into the #root DOM element.
 * Provides the complete provider hierarchy for the application:
 *
 * ```
 * <StrictMode>
 *   <ErrorBoundary>
 *     <Auth0Provider>
 *       <Suspense fallback={<LoadingSpinner />}>
 *         <RouterProvider router={router} />
 *       </Suspense>
 *     </Auth0Provider>
 *   </ErrorBoundary>
 * </StrictMode>
 * ```
 *
 * @returns The root application component tree
 */
function App(): React.JSX.Element {
  return (
    <React.StrictMode>
      <ErrorBoundary>
        <Auth0Provider
          domain={auth0Domain}
          clientId={auth0ClientId}
          authorizationParams={{
            redirect_uri: auth0RedirectUri,
            audience: auth0Audience,
            scope: 'openid profile email',
          }}
          cacheLocation="localstorage"
        >
          <Suspense fallback={<LoadingSpinner />}>
            <RouterProvider router={router} />
          </Suspense>
        </Auth0Provider>
      </ErrorBoundary>
    </React.StrictMode>
  );
}

export default App;
