/**
 * Loading Spinner Component
 *
 * Animated loading indicator for the Synthetic ERP Data Generation Platform
 * Web Console. Used as:
 *   - React Suspense fallback while lazy-loaded page components load
 *   - Inline loading state for data fetching operations
 *   - Full-page loading state during authentication redirects
 *
 * Features:
 *   - Configurable size (sm, md, lg, xl)
 *   - Optional loading message text
 *   - Full-page or inline display modes
 *   - Accessible with role="status" and aria-label
 *   - Uses TailwindCSS animation utilities
 *
 * Reference: Agent Action Plan Section 0.3.1 — src/web/src/components/common/LoadingSpinner.tsx
 */

import React from 'react';

/**
 * Size variants for the loading spinner.
 * Maps to TailwindCSS width/height classes.
 */
type SpinnerSize = 'sm' | 'md' | 'lg' | 'xl';

/**
 * Props for the LoadingSpinner component.
 */
interface LoadingSpinnerProps {
  /** Size of the spinner animation. Defaults to 'lg'. */
  size?: SpinnerSize;
  /** Optional message displayed below the spinner. */
  message?: string;
  /** Whether to render as full-page centered overlay. Defaults to true. */
  fullPage?: boolean;
  /** Additional CSS class names for the container. */
  className?: string;
}

/**
 * Map spinner size to TailwindCSS dimension classes.
 */
const sizeClasses: Record<SpinnerSize, string> = {
  sm: 'w-5 h-5',
  md: 'w-8 h-8',
  lg: 'w-12 h-12',
  xl: 'w-16 h-16',
};

/**
 * LoadingSpinner — Animated loading indicator component.
 *
 * Renders a spinning SVG circle animation with optional text message.
 * Supports both full-page centered display (for Suspense fallback) and
 * inline display (for component-level loading states).
 *
 * @param props - Component configuration
 * @returns The loading spinner element
 *
 * @example
 * // Full-page Suspense fallback (default)
 * <Suspense fallback={<LoadingSpinner />}>
 *   <LazyComponent />
 * </Suspense>
 *
 * @example
 * // Inline loading with custom message
 * <LoadingSpinner size="sm" fullPage={false} message="Loading data..." />
 */
function LoadingSpinner({
  size = 'lg',
  message,
  fullPage = true,
  className = '',
}: LoadingSpinnerProps): React.JSX.Element {
  const spinner = (
    <div
      role="status"
      aria-label="Loading"
      className={`flex flex-col items-center justify-center gap-3 ${className}`}
    >
      <svg
        className={`animate-spin text-primary-600 ${sizeClasses[size]}`}
        xmlns="http://www.w3.org/2000/svg"
        fill="none"
        viewBox="0 0 24 24"
        aria-hidden="true"
      >
        <circle
          className="opacity-25"
          cx="12"
          cy="12"
          r="10"
          stroke="currentColor"
          strokeWidth="4"
        />
        <path
          className="opacity-75"
          fill="currentColor"
          d="M4 12a8 8 0 018-8V0C5.373 0 0 5.373 0 12h4zm2 5.291A7.962 7.962 0 014 12H0c0 3.042 1.135 5.824 3 7.938l3-2.647z"
        />
      </svg>
      {message && (
        <p className="text-sm text-neutral-500 font-medium">{message}</p>
      )}
      <span className="sr-only">Loading...</span>
    </div>
  );

  if (fullPage) {
    return (
      <div className="flex items-center justify-center min-h-screen bg-neutral-50">
        {spinner}
      </div>
    );
  }

  return spinner;
}

export default LoadingSpinner;
