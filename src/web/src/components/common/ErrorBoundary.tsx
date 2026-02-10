/**
 * React Error Boundary Component
 *
 * Implements React's componentDidCatch lifecycle for graceful error handling.
 * Wraps the entire application component tree to catch unhandled JavaScript
 * errors in the React render tree, preventing white-screen crashes and
 * displaying a user-friendly fallback UI with retry capability.
 *
 * Usage:
 *   <ErrorBoundary>
 *     <App />
 *   </ErrorBoundary>
 *
 * Reference: Agent Action Plan Section 0.3.1 — src/web/src/components/common/ErrorBoundary.tsx
 */

import { Component, type ErrorInfo, type ReactNode } from 'react';

/**
 * Props for the ErrorBoundary component.
 */
interface ErrorBoundaryProps {
  /** Child components to wrap with error handling */
  children: ReactNode;
  /** Optional custom fallback UI to render on error */
  fallback?: ReactNode;
}

/**
 * Internal state for the ErrorBoundary component.
 */
interface ErrorBoundaryState {
  /** Whether an error has been caught */
  hasError: boolean;
  /** The caught error object, if any */
  error: Error | null;
  /** Component stack trace from React, if available */
  errorInfo: ErrorInfo | null;
}

/**
 * ErrorBoundary — React class component for global error handling.
 *
 * Catches JavaScript errors anywhere in the child component tree,
 * logs them for diagnostics, and renders a fallback UI instead of
 * the component tree that crashed.
 *
 * Features:
 *   - Catches render errors, lifecycle errors, and constructor errors
 *   - Displays user-friendly error message with retry button
 *   - Logs error details to console for developer diagnostics
 *   - Supports custom fallback UI via the `fallback` prop
 *   - Provides "Try Again" button that resets error state
 *
 * Limitations (by React design):
 *   - Does NOT catch errors in event handlers (use try/catch)
 *   - Does NOT catch errors in async code (use .catch())
 *   - Does NOT catch server-side rendering errors
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
   * Called during the "render" phase — no side effects allowed.
   */
  static getDerivedStateFromError(error: Error): Partial<ErrorBoundaryState> {
    return { hasError: true, error };
  }

  /**
   * Log error details after an error has been caught.
   * Called during the "commit" phase — side effects are allowed.
   *
   * In production, this would send errors to an observability platform
   * (e.g., Sentry, Datadog) for monitoring and alerting.
   */
  componentDidCatch(error: Error, errorInfo: ErrorInfo): void {
    this.setState({ errorInfo });

    // Log error details for developer diagnostics
    console.error('[ErrorBoundary] Uncaught error in component tree:', error);
    console.error('[ErrorBoundary] Component stack:', errorInfo.componentStack);
  }

  /**
   * Reset the error state to allow the user to retry rendering.
   * Called when the user clicks the "Try Again" button.
   */
  handleRetry = (): void => {
    this.setState({ hasError: false, error: null, errorInfo: null });
  };

  render(): ReactNode {
    if (this.state.hasError) {
      // Render custom fallback if provided
      if (this.props.fallback) {
        return this.props.fallback;
      }

      // Default fallback UI
      return (
        <div
          role="alert"
          style={{
            display: 'flex',
            alignItems: 'center',
            justifyContent: 'center',
            minHeight: '100vh',
            fontFamily: 'system-ui, -apple-system, sans-serif',
            padding: '2rem',
            textAlign: 'center',
            backgroundColor: '#fafafa',
          }}
        >
          <div style={{ maxWidth: '32rem' }}>
            <div
              style={{
                width: '4rem',
                height: '4rem',
                margin: '0 auto 1.5rem',
                borderRadius: '50%',
                backgroundColor: '#fee2e2',
                display: 'flex',
                alignItems: 'center',
                justifyContent: 'center',
              }}
            >
              <svg
                width="24"
                height="24"
                viewBox="0 0 24 24"
                fill="none"
                stroke="#dc2626"
                strokeWidth="2"
                strokeLinecap="round"
                strokeLinejoin="round"
              >
                <circle cx="12" cy="12" r="10" />
                <line x1="12" y1="8" x2="12" y2="12" />
                <line x1="12" y1="16" x2="12.01" y2="16" />
              </svg>
            </div>
            <h1
              style={{
                fontSize: '1.5rem',
                fontWeight: 600,
                marginBottom: '0.75rem',
                color: '#171717',
              }}
            >
              Something went wrong
            </h1>
            <p
              style={{
                color: '#525252',
                marginBottom: '1.5rem',
                lineHeight: 1.6,
              }}
            >
              The Synthetic ERP Data Generation Platform encountered an unexpected error.
              Please try again or contact your system administrator if the problem persists.
            </p>
            {this.state.error && (
              <p
                style={{
                  fontFamily: 'monospace',
                  fontSize: '0.875rem',
                  color: '#dc2626',
                  backgroundColor: '#fef2f2',
                  padding: '0.75rem',
                  borderRadius: '0.375rem',
                  marginBottom: '1.5rem',
                  wordBreak: 'break-word',
                }}
              >
                {this.state.error.message}
              </p>
            )}
            <button
              onClick={this.handleRetry}
              type="button"
              style={{
                padding: '0.625rem 1.5rem',
                backgroundColor: '#1e40af',
                color: '#ffffff',
                border: 'none',
                borderRadius: '0.375rem',
                fontSize: '0.875rem',
                fontWeight: 500,
                cursor: 'pointer',
                transition: 'background-color 0.15s ease',
              }}
              onMouseOver={(e): void => {
                (e.target as HTMLButtonElement).style.backgroundColor = '#1d4ed8';
              }}
              onMouseOut={(e): void => {
                (e.target as HTMLButtonElement).style.backgroundColor = '#1e40af';
              }}
            >
              Try Again
            </button>
          </div>
        </div>
      );
    }

    return this.props.children;
  }
}

export default ErrorBoundary;
