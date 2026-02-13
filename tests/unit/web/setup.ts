/**
 * Vitest Global Test Setup Configuration
 *
 * Global test setup file for the Synthetic ERP Data Generation Platform Web Console.
 * Vitest executes this file before every test file in the Web Console test suite,
 * as configured by the `test.setupFiles` entry in vite.config.ts.
 *
 * Responsibilities:
 *   - Registers @testing-library/jest-dom (^6.4.0) custom DOM matchers globally
 *     (toBeInTheDocument, toHaveTextContent, toBeVisible, toHaveClass, etc.)
 *   - Configures automatic DOM cleanup after each test via React Testing Library
 *   - Mocks browser APIs not available in jsdom (matchMedia, IntersectionObserver,
 *     ResizeObserver, URL.createObjectURL, scrollTo, scrollIntoView, crypto)
 *   - Stubs Vite environment variables (import.meta.env.VITE_*) with test defaults
 *   - Provides global module mocks for @auth0/auth0-react and axios to prevent
 *     real authentication and HTTP traffic during unit tests
 *   - Mocks WebSocket for real-time job progress communication testing
 *   - Suppresses noisy console.error/warn output for cleaner test reporting
 *
 * Configuration context:
 *   - Environment: jsdom (set in vite.config.ts test.environment)
 *   - Globals: true (describe/it/expect available without imports, set in vite.config.ts)
 *   - CSS: true (TailwindCSS imports processed during tests)
 *   - TypeScript: Compiled via esbuild with ES2020 target (tsconfig.json)
 *
 * @see src/web/vite.config.ts - test.setupFiles, test.environment, test.globals
 * @see src/web/package.json - devDependencies for test packages
 * @see src/web/tsconfig.json - TypeScript compilation settings
 */

// ============================================================================
// Core Testing Framework Imports
// ============================================================================

/**
 * Side-effect import that globally extends Vitest's expect() with DOM-specific
 * assertion matchers from @testing-library/jest-dom (^6.4.0):
 *
 *   - toBeInTheDocument()   — Assert element exists in the DOM
 *   - toHaveTextContent()   — Assert element contains specific text
 *   - toBeVisible()         — Assert element is visible to the user
 *   - toHaveClass()         — Assert element has specified CSS class(es)
 *   - toHaveAttribute()     — Assert element has specified attribute/value
 *   - toBeDisabled()        — Assert form element is disabled
 *   - toHaveStyle()         — Assert element has specified inline styles
 *   - toBeChecked()         — Assert checkbox/radio is checked
 *   - toHaveFocus()         — Assert element currently has focus
 *   - toBeRequired()        — Assert form element is required
 *   - toBeEmpty()           — Assert element has no content
 *   - toContainElement()    — Assert element contains another element
 *   - toContainHTML()       — Assert element contains specific HTML
 *   - toHaveValue()         — Assert form element has specified value
 *   - toHaveDisplayValue()  — Assert select/input shows specified display value
 *   - toBeInvalid()         — Assert form element is invalid
 *   - toBeValid()           — Assert form element is valid
 *   - toHaveErrorMessage()  — Assert element has accessible error message
 *   - toHaveDescription()   — Assert element has accessible description
 *
 * This import must occur before any test code to ensure matchers are registered.
 * All Web Console component tests can then use these matchers without per-file imports.
 */
import '@testing-library/jest-dom';

/**
 * React Testing Library cleanup function that unmounts rendered React components
 * and clears the DOM container after each test. Called in the afterEach hook to
 * prevent test state leakage between tests and ensure each test starts with a
 * clean DOM environment.
 *
 * @see https://testing-library.com/docs/react-testing-library/api/#cleanup
 */
import { cleanup } from '@testing-library/react';

/**
 * Vitest lifecycle hook and mock utility:
 *   - afterEach: Registers a callback to run after every test for cleanup
 *   - vi: Mock utility providing fn(), mock(), spyOn(), stubEnv(), restoreAllMocks()
 *
 * Used extensively throughout this setup file to create mock functions for browser
 * API stubs, globally mock module imports, spy on console methods, and stub
 * environment variables.
 */
import { afterEach, vi } from 'vitest';


// ============================================================================
// Global Test Cleanup
// ============================================================================

/**
 * Automatically unmount React components and clear the DOM after each test.
 * This prevents test state leakage and ensures each test in the Web Console
 * suite starts with a clean DOM environment. Without this, rendered components
 * from previous tests could interfere with subsequent assertions.
 *
 * React Testing Library's cleanup function:
 *   1. Unmounts React trees rendered via render()
 *   2. Removes the DOM container element
 *   3. Resets internal render references
 */
afterEach(() => {
  cleanup();
});


// ============================================================================
// Vite Environment Variable Mocks
// ============================================================================

/**
 * Mock Vite environment variables accessed via import.meta.env.VITE_* throughout
 * the Web Console application. vi.stubEnv() sets these values on import.meta.env
 * for the duration of the test suite, providing consistent test defaults.
 *
 * These match the environment variables defined in .env.example and consumed by:
 *   - src/web/src/services/api.ts (VITE_API_BASE_URL)
 *   - src/web/src/services/authApi.ts (VITE_AUTH0_DOMAIN, VITE_AUTH0_CLIENT_ID)
 *   - src/web/src/hooks/useAuth.ts (VITE_AUTH0_AUDIENCE, VITE_AUTH0_REDIRECT_URI)
 *   - src/web/src/hooks/useWebSocket.ts (VITE_WS_URL)
 */
vi.stubEnv('VITE_API_BASE_URL', 'http://localhost:5000');
vi.stubEnv('VITE_AUTH0_DOMAIN', 'test.auth0.com');
vi.stubEnv('VITE_AUTH0_CLIENT_ID', 'test-client-id');
vi.stubEnv('VITE_AUTH0_AUDIENCE', 'https://api.test.com');
vi.stubEnv('VITE_AUTH0_REDIRECT_URI', 'http://localhost:3000/callback');
vi.stubEnv('VITE_WS_URL', 'ws://localhost:8000/ws');


// ============================================================================
// Browser API Mocks (jsdom gaps)
// ============================================================================

/**
 * 1. window.matchMedia mock
 *
 * jsdom does not implement window.matchMedia. This API is required by:
 *   - TailwindCSS responsive utility detection
 *   - Recharts chart components for responsive sizing
 *   - Dark mode detection via prefers-color-scheme media query
 *   - Sidebar collapse behavior based on viewport width
 *
 * Returns a MediaQueryList-compatible object with all standard methods stubbed.
 * By default, matches is false — individual tests can override via
 * vi.mocked(window.matchMedia).mockImplementation(...) to test responsive behavior.
 */
Object.defineProperty(window, 'matchMedia', {
  writable: true,
  value: vi.fn().mockImplementation((query: string) => ({
    matches: false,
    media: query,
    onchange: null,
    addListener: vi.fn(),
    removeListener: vi.fn(),
    addEventListener: vi.fn(),
    removeEventListener: vi.fn(),
    dispatchEvent: vi.fn(),
  })),
});

/**
 * 2. IntersectionObserver mock
 *
 * jsdom does not implement IntersectionObserver. This API is used by:
 *   - Lazy-loading components in the template library
 *   - Infinite scroll in the DataTable component
 *   - Visibility-triggered animations in dashboard charts
 *   - Virtual scrolling in the SchemaBrowser table lists
 *
 * Provides a complete IntersectionObserver interface with all methods stubbed.
 * The observe/unobserve/disconnect methods are no-ops; individual tests can
 * trigger intersection callbacks by capturing the constructor callback argument.
 */
const MockIntersectionObserver = vi.fn().mockImplementation(() => ({
  observe: vi.fn(),
  unobserve: vi.fn(),
  disconnect: vi.fn(),
  root: null,
  rootMargin: '',
  thresholds: [] as number[],
  takeRecords: vi.fn().mockReturnValue([]),
}));
global.IntersectionObserver = MockIntersectionObserver as unknown as typeof IntersectionObserver;

/**
 * 3. ResizeObserver mock
 *
 * jsdom does not implement ResizeObserver. This API is required by:
 *   - Recharts responsive container for chart auto-sizing
 *   - Sidebar component for detecting available width
 *   - DataTable component for column width calculations
 *   - Modal component for content overflow detection
 *
 * Provides observe/unobserve/disconnect stubs; individual tests can simulate
 * resize events by capturing the constructor callback and invoking it.
 */
const MockResizeObserver = vi.fn().mockImplementation(() => ({
  observe: vi.fn(),
  unobserve: vi.fn(),
  disconnect: vi.fn(),
}));
global.ResizeObserver = MockResizeObserver as unknown as typeof ResizeObserver;

/**
 * 4. URL.createObjectURL / URL.revokeObjectURL mocks
 *
 * jsdom provides a partial URL implementation but lacks blob URL support.
 * These are used by:
 *   - File export download (CSV, JSON, Parquet, SQL formats)
 *   - Quality report PDF generation
 *   - Schema definition file download
 *
 * createObjectURL returns a deterministic 'blob:mock-url' string for assertion.
 * revokeObjectURL is a no-op stub to prevent memory leak warnings in tests.
 */
global.URL.createObjectURL = vi.fn(() => 'blob:mock-url');
global.URL.revokeObjectURL = vi.fn();

/**
 * 5. window.scrollTo mock
 *
 * jsdom does not implement window.scrollTo. This is called by:
 *   - React Router on route transitions (scroll restoration)
 *   - GenerationWizard step navigation (scroll to top on step change)
 *   - Dashboard on initial load (scroll to top)
 *   - Error display components (scroll to error message)
 */
Object.defineProperty(window, 'scrollTo', {
  value: vi.fn(),
  writable: true,
});

/**
 * 6. Element.prototype.scrollIntoView mock
 *
 * jsdom does not implement scrollIntoView. This is used by:
 *   - Form validation to scroll to the first error field
 *   - SchemaBrowser to scroll to the selected table in the tree view
 *   - JobMonitoring to auto-scroll the log viewer to the latest entry
 *   - Sidebar navigation to scroll active item into view
 */
Element.prototype.scrollIntoView = vi.fn();

/**
 * 7. crypto.randomUUID and crypto.getRandomValues mocks
 *
 * Provides UUID generation and random byte filling for tests.
 * Used by:
 *   - Correlation ID generation in API request interceptors
 *   - Unique key generation for dynamically rendered list items
 *   - Nonce generation for Content Security Policy headers
 *   - File naming for export downloads
 *
 * randomUUID produces semi-unique strings prefixed with 'test-uuid-' for easy
 * identification in test assertions. getRandomValues fills typed arrays with
 * pseudo-random bytes using Math.random() (sufficient for test purposes).
 */
Object.defineProperty(global, 'crypto', {
  value: {
    randomUUID: vi.fn(
      () => 'test-uuid-' + Math.random().toString(36).substring(7)
    ),
    getRandomValues: vi.fn((arr: Uint8Array) => {
      for (let i = 0; i < arr.length; i++) {
        arr[i] = Math.floor(Math.random() * 256);
      }
      return arr;
    }),
  },
});


// ============================================================================
// Auth0 Provider Mock (@auth0/auth0-react ^2.2.0)
// ============================================================================

/**
 * Globally mock @auth0/auth0-react to provide test doubles for authentication
 * across all Web Console component tests. This prevents real Auth0 SDK
 * initialization, network calls to Auth0 servers, and OAuth redirect flows.
 *
 * Mocked exports:
 *   - Auth0Provider: Passthrough wrapper that renders children directly without
 *     initializing the Auth0 SDK or creating authentication context
 *   - useAuth0: Returns a configurable mock auth state with default values for
 *     an unauthenticated user. Tests can override via:
 *       vi.mocked(useAuth0).mockReturnValue({
 *         isAuthenticated: true,
 *         user: { sub: 'auth0|123', name: 'Test User', email: 'test@example.com' },
 *         ...
 *       })
 *   - withAuthenticationRequired: Passthrough HOC that returns the wrapped
 *     component unchanged, bypassing authentication guards in tests
 *
 * Default mock state (unauthenticated):
 *   - isAuthenticated: false
 *   - isLoading: false
 *   - user: undefined
 *   - error: null
 *   - loginWithRedirect: vi.fn() (callable, no-op)
 *   - logout: vi.fn() (callable, no-op)
 *   - getAccessTokenSilently: resolves to 'mock-access-token'
 */
vi.mock('@auth0/auth0-react', () => ({
  Auth0Provider: ({ children }: { children: React.ReactNode }) => children,
  useAuth0: vi.fn(() => ({
    isAuthenticated: false,
    isLoading: false,
    user: undefined,
    loginWithRedirect: vi.fn(),
    logout: vi.fn(),
    getAccessTokenSilently: vi.fn().mockResolvedValue('mock-access-token'),
    error: null,
  })),
  withAuthenticationRequired: (component: unknown) => component,
}));


// ============================================================================
// Axios HTTP Client Mock (axios ^1.7.0)
// ============================================================================

/**
 * Globally mock axios to intercept all HTTP API calls in Web Console component
 * and service tests. This prevents real HTTP requests to the Flask API Gateway
 * during unit tests while preserving the full Axios API surface for assertions.
 *
 * Mocked structure:
 *   - default.create() / create(): Factory functions returning a mock Axios instance
 *   - Mock instance methods: get, post, put, delete, patch (all vi.fn())
 *   - Mock interceptors: request.use/eject, response.use/eject
 *   - Mock defaults.headers.common: Empty object for JWT header injection tests
 *
 * The mock is designed to match how src/web/src/services/api.ts uses axios:
 *   const apiClient = axios.create({ baseURL: import.meta.env.VITE_API_BASE_URL });
 *   apiClient.interceptors.request.use((config) => { ... });
 *   apiClient.interceptors.response.use((response) => { ... }, (error) => { ... });
 *
 * Individual tests can configure mock responses:
 *   import axios from 'axios';
 *   const mockInstance = axios.create();
 *   vi.mocked(mockInstance.get).mockResolvedValueOnce({ data: { jobs: [] } });
 */
vi.mock('axios', () => {
  const mockAxiosInstance = {
    get: vi.fn(),
    post: vi.fn(),
    put: vi.fn(),
    delete: vi.fn(),
    patch: vi.fn(),
    interceptors: {
      request: { use: vi.fn(), eject: vi.fn() },
      response: { use: vi.fn(), eject: vi.fn() },
    },
    defaults: { headers: { common: {} } },
  };
  return {
    default: { create: vi.fn(() => mockAxiosInstance) },
    create: vi.fn(() => mockAxiosInstance),
  };
});


// ============================================================================
// WebSocket Mock
// ============================================================================

/**
 * Mock the global WebSocket constructor for real-time job progress updates
 * and monitoring features in the Web Console.
 *
 * WebSocket is used by:
 *   - src/web/src/hooks/useWebSocket.ts — Custom hook for real-time progress
 *   - JobMonitoring page — Live job progress updates and log streaming
 *   - Dashboard — Real-time system health indicators
 *
 * The mock provides:
 *   - send(): Stub for sending messages to the server
 *   - close(): Stub for closing the WebSocket connection
 *   - addEventListener/removeEventListener: Stubs for event binding
 *   - readyState: Defaults to OPEN (1) for immediate message sending in tests
 *   - Static constants: CONNECTING(0), OPEN(1), CLOSING(2), CLOSED(3)
 *
 * Individual tests can simulate incoming messages:
 *   const wsInstance = new WebSocket('ws://localhost:8000/ws');
 *   const onMessageCalls = vi.mocked(wsInstance.addEventListener).mock.calls;
 *   const messageHandler = onMessageCalls.find(([evt]) => evt === 'message')?.[1];
 *   messageHandler?.({ data: JSON.stringify({ jobId: '123', progress: 75 }) });
 *
 * To test connection states:
 *   const wsInstance = new WebSocket('...');
 *   Object.defineProperty(wsInstance, 'readyState', { value: 3 }); // CLOSED
 */
const MockWebSocket = vi.fn().mockImplementation(() => ({
  send: vi.fn(),
  close: vi.fn(),
  addEventListener: vi.fn(),
  removeEventListener: vi.fn(),
  readyState: 1,
  CONNECTING: 0,
  OPEN: 1,
  CLOSING: 2,
  CLOSED: 3,
}));

/**
 * Attach static WebSocket constants to the constructor function itself,
 * matching the native WebSocket API where WebSocket.OPEN === 1, etc.
 * This allows test code to reference WebSocket.OPEN, WebSocket.CLOSED, etc.
 */
Object.assign(MockWebSocket, {
  CONNECTING: 0,
  OPEN: 1,
  CLOSING: 2,
  CLOSED: 3,
});

global.WebSocket = MockWebSocket as unknown as typeof WebSocket;


// ============================================================================
// Console Suppression
// ============================================================================

/**
 * Suppress console.error and console.warn output during test execution for
 * cleaner test output. React and third-party libraries frequently emit warnings
 * in test environments that are expected and non-actionable:
 *
 *   - React act() warnings from asynchronous state updates
 *   - React Router future flag deprecation notices
 *   - Recharts prop-type warnings for test-only configurations
 *   - Auth0 SDK initialization warnings in mocked environments
 *   - TailwindCSS PostCSS plugin messages
 *
 * These spies capture all console.error/warn calls so they can be asserted
 * against if a test specifically wants to verify error logging behavior:
 *   expect(console.error).toHaveBeenCalledWith(expect.stringContaining('...'));
 *
 * Individual tests can restore normal console output if needed:
 *   vi.restoreAllMocks();
 *
 * Or selectively restore a single method:
 *   vi.mocked(console.error).mockRestore();
 *   vi.mocked(console.warn).mockRestore();
 */
vi.spyOn(console, 'error').mockImplementation(() => {});
vi.spyOn(console, 'warn').mockImplementation(() => {});
