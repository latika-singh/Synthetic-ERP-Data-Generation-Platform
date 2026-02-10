/**
 * ErrorBoundary — React class component for global error handling.
 *
 * Implements React's error boundary pattern using componentDidCatch and
 * getDerivedStateFromError lifecycle methods to gracefully catch unhandled
 * JavaScript errors anywhere in the child component tree, preventing
 * white-screen crashes and displaying a user-friendly fallback UI.
 *
 * Features:
 *   - Catches render errors, lifecycle errors, and constructor errors
 *   - Displays user-friendly error message with retry and navigation buttons
 *   - Shows detailed error info (message + component stack) in development only
 *   - Supports custom fallback UI via the `fallback` prop (ReactNode or render function)
 *   - Provides `onError` callback for external error reporting (Sentry, Datadog, etc.)
 *   - Provides `onReset` callback invoked when user clicks "Try Again"
 *   - Full dark mode support via TailwindCSS dark: variants
 *   - Accessible: semantic headings, role="alert", aria-hidden decorative icons
 *
 * Limitations (by React design):
 *   - Does NOT catch errors in event handlers (use try/catch)
 *   - Does NOT catch errors in async code (use Promise.catch())
 *   - Does NOT catch server-side rendering errors
 *
 * Usage:
 *   // Basic usage wrapping the entire app tree
 *   <ErrorBoundary>
 *     <App />
 *   </ErrorBoundary>
 *
 *   // With custom fallback render prop
 *   <ErrorBoundary fallback={(error, resetError) => (
 *     <div>
 *       <p>Error: {error.message}</p>
 *       <button onClick={resetError}>Retry</button>
 *     </div>
 *   )}>
 *     <App />
 *   </ErrorBoundary>
 *
 *   // With error reporting callback
 *   <ErrorBoundary onError={(error, info) => sentryReport(error, info)}>
 *     <App />
 *   </ErrorBoundary>
 *
 * @module components/common/ErrorBoundary
 */

import { Component, type ErrorInfo, type ReactNode } from 'react';

// ---------------------------------------------------------------------------
// TypeScript Interfaces
// ---------------------------------------------------------------------------

/**
 * Props for the ErrorBoundary component.
 *
 * @property children   - Child components to wrap with error handling.
 * @property fallback   - Optional custom fallback UI. Can be a static ReactNode
 *                        or a render function receiving the caught error and a
 *                        resetError callback for flexible error UI composition.
 * @property onError    - Optional callback invoked when an error is caught.
 *                        Use this to report errors to external services
 *                        (e.g., Sentry, Datadog, OpenTelemetry).
 * @property onReset    - Optional callback invoked when the user triggers a
 *                        retry via the "Try Again" button, allowing parent
 *                        components to perform cleanup before re-render.
 */
export interface ErrorBoundaryProps {
  children: ReactNode;
  fallback?: ReactNode | ((error: Error, resetError: () => void) => ReactNode);
  onError?: (error: Error, errorInfo: ErrorInfo) => void;
  onReset?: () => void;
}

/**
 * Internal state for the ErrorBoundary component.
 *
 * @property hasError   - Whether an error has been caught in the subtree.
 * @property error      - The caught Error object, or null if no error.
 * @property errorInfo  - React ErrorInfo containing the component stack trace,
 *                        or null if componentDidCatch has not yet fired.
 */
export interface ErrorBoundaryState {
  hasError: boolean;
  error: Error | null;
  errorInfo: ErrorInfo | null;
}

// ---------------------------------------------------------------------------
// ErrorBoundary Class Component
// ---------------------------------------------------------------------------

/**
 * ErrorBoundary — React class component implementing the error boundary pattern.
 *
 * Wraps the entire application (or a subtree) to catch rendering errors and
 * present a recoverable fallback UI instead of a blank white screen.
 */
class ErrorBoundary extends Component<ErrorBoundaryProps, ErrorBoundaryState> {
  constructor(props: ErrorBoundaryProps) {
    super(props);
    this.state = {
      hasError: false,
      error: null,
      errorInfo: null,
    };
  }

  /**
   * Derive error state from a caught error.
   *
   * Called during the "render" phase — no side effects are permitted.
   * Updates state so the next render displays the fallback UI.
   *
   * @param error - The error thrown during rendering.
   * @returns Partial state update setting hasError and capturing the error.
   */
  static getDerivedStateFromError(error: Error): Partial<ErrorBoundaryState> {
    return {
      hasError: true,
      error,
    };
  }

  /**
   * Capture error details after an error has been caught.
   *
   * Called during the "commit" phase — side effects (logging, reporting)
   * are allowed here. Stores the component stack trace in state and
   * invokes the optional onError callback for external error reporting.
   *
   * In development mode, detailed error information is logged to the console
   * to aid debugging. In production, logging is suppressed to avoid leaking
   * internal stack traces; errors should be routed to an observability
   * platform via the onError callback.
   *
   * @param error     - The error that was thrown.
   * @param errorInfo - React metadata including the component stack trace.
   */
  componentDidCatch(error: Error, errorInfo: ErrorInfo): void {
    // Persist the component stack trace in state for the fallback UI
    this.setState({ errorInfo });

    // Invoke the optional external error reporting callback
    this.props.onError?.(error, errorInfo);

    // Log detailed diagnostics in development builds only
    if (import.meta.env.DEV) {
      console.error('ErrorBoundary caught an error:', error);
      console.error('Component stack:', errorInfo.componentStack);
    }
  }

  /**
   * Reset the error state to allow the user to retry rendering.
   *
   * Clears the caught error and component stack trace, then invokes the
   * optional onReset callback so parent components can perform any
   * necessary cleanup (e.g., clearing caches, resetting stores) before
   * the children are re-mounted.
   */
  resetError = (): void => {
    this.setState({
      hasError: false,
      error: null,
      errorInfo: null,
    });
    this.props.onReset?.();
  };

  /**
   * Render the children or the fallback UI depending on error state.
   *
   * Rendering priority:
   * 1. If no error → render children normally.
   * 2. If error + custom fallback function → call it with (error, resetError).
   * 3. If error + custom fallback ReactNode → render it directly.
   * 4. If error + no custom fallback → render the built-in default fallback UI.
   */
  render(): ReactNode {
    if (this.state.hasError && this.state.error) {
      // Custom fallback: render function form
      if (typeof this.props.fallback === 'function') {
        return this.props.fallback(this.state.error, this.resetError);
      }

      // Custom fallback: static ReactNode form
      if (this.props.fallback) {
        return this.props.fallback;
      }

      // Default fallback UI with TailwindCSS styling
      return (
        <div
          role="alert"
          className="min-h-screen flex items-center justify-center bg-gray-50 dark:bg-gray-900 px-4"
        >
          <div className="max-w-md w-full bg-white dark:bg-gray-800 rounded-xl shadow-lg p-8 text-center">
            {/* Decorative error icon */}
            <div className="mx-auto w-16 h-16 flex items-center justify-center rounded-full bg-red-100 dark:bg-red-900/20 mb-6">
              <svg
                className="w-8 h-8 text-red-600 dark:text-red-400"
                xmlns="http://www.w3.org/2000/svg"
                fill="none"
                viewBox="0 0 24 24"
                strokeWidth={1.5}
                stroke="currentColor"
                aria-hidden="true"
              >
                <path
                  strokeLinecap="round"
                  strokeLinejoin="round"
                  d="M12 9v3.75m-9.303 3.376c-.866 1.5.217 3.374 1.948 3.374h14.71c1.73 0 2.813-1.874 1.948-3.374L13.949 3.378c-.866-1.5-3.032-1.5-3.898 0L2.697 16.126zM12 15.75h.007v.008H12v-.008z"
                />
              </svg>
            </div>

            {/* Error heading */}
            <h1 className="text-xl font-semibold text-gray-900 dark:text-white mb-2">
              Something went wrong
            </h1>

            {/* User-friendly error description */}
            <p className="text-sm text-gray-500 dark:text-gray-400 mb-4">
              An unexpected error occurred. Please try again or contact support
              if the problem persists.
            </p>

            {/* Development-only error details (hidden in production) */}
            {import.meta.env.DEV && this.state.error && (
              <details className="mb-6 text-left">
                <summary className="cursor-pointer text-sm font-medium text-gray-700 dark:text-gray-300 hover:text-gray-900 dark:hover:text-white">
                  Error Details
                </summary>
                <div className="mt-2 p-3 bg-gray-100 dark:bg-gray-700 rounded-lg overflow-auto max-h-48">
                  <p className="text-xs font-mono text-red-600 dark:text-red-400 break-all">
                    {this.state.error.message}
                  </p>
                  {this.state.errorInfo?.componentStack && (
                    <pre className="mt-2 text-[10px] font-mono text-gray-500 dark:text-gray-400 whitespace-pre-wrap">
                      {this.state.errorInfo.componentStack}
                    </pre>
                  )}
                </div>
              </details>
            )}

            {/* Action buttons */}
            <div className="flex items-center justify-center gap-3">
              {/* Try Again — resets error state and re-renders children */}
              <button
                type="button"
                onClick={this.resetError}
                className="inline-flex items-center px-4 py-2 text-sm font-medium text-white bg-indigo-600 rounded-lg hover:bg-indigo-700 focus:outline-none focus:ring-2 focus:ring-offset-2 focus:ring-indigo-500 transition-colors"
              >
                <svg
                  className="w-4 h-4 mr-2"
                  xmlns="http://www.w3.org/2000/svg"
                  fill="none"
                  viewBox="0 0 24 24"
                  strokeWidth={1.5}
                  stroke="currentColor"
                  aria-hidden="true"
                >
                  <path
                    strokeLinecap="round"
                    strokeLinejoin="round"
                    d="M16.023 9.348h4.992v-.001M2.985 19.644v-4.992m0 0h4.992m-4.993 0l3.181 3.183a8.25 8.25 0 0013.803-3.7M4.031 9.865a8.25 8.25 0 0113.803-3.7l3.181 3.182"
                  />
                </svg>
                Try Again
              </button>

              {/* Go to Dashboard — hard-navigates to root to escape error state */}
              <button
                type="button"
                onClick={(): void => {
                  window.location.href = '/';
                }}
                className="inline-flex items-center px-4 py-2 text-sm font-medium text-gray-700 dark:text-gray-200 bg-white dark:bg-gray-700 border border-gray-300 dark:border-gray-600 rounded-lg hover:bg-gray-50 dark:hover:bg-gray-600 focus:outline-none focus:ring-2 focus:ring-offset-2 focus:ring-gray-500 transition-colors"
              >
                Go to Dashboard
              </button>
            </div>
          </div>
        </div>
      );
    }

    // No error — render children normally
    return this.props.children;
  }
}

export default ErrorBoundary;
export { ErrorBoundary };
