/**
 * LoadingSpinner Component
 *
 * Animated loading state indicator for the Synthetic ERP Data Generation Platform
 * Web Console. Provides a configurable spinning SVG animation with optional
 * message text, full-screen centering, and overlay backdrop support.
 *
 * Usage contexts:
 *   - React Suspense fallback for lazy-loaded page components in App.tsx
 *   - Inline loading state within DataTable during data fetching
 *   - Full-screen overlay during authentication redirects and heavy operations
 *   - Component-level loading indicators throughout the application
 *
 * Features:
 *   - Four size variants: sm (16px), md (32px), lg (48px), xl (64px)
 *   - Proportionally scaled message text per size variant
 *   - Customizable spinner color via TailwindCSS text color classes
 *   - Full-viewport fixed centering mode for page-level loading
 *   - Semi-transparent backdrop overlay with blur effect
 *   - Dark mode support for message text and overlay backgrounds
 *   - Full WAI-ARIA accessibility: role="status", aria-live, aria-label, sr-only
 *   - TailwindCSS 4.x animate-spin utility for smooth CSS animation
 *
 * @module components/common/LoadingSpinner
 */

import React from 'react';

// ---------------------------------------------------------------------------
// Type Definitions
// ---------------------------------------------------------------------------

/**
 * Available size variants for the loading spinner.
 * Each size maps to specific TailwindCSS width/height dimension classes
 * and corresponding message text size classes.
 */
type SpinnerSize = 'sm' | 'md' | 'lg' | 'xl';

/**
 * Props for the LoadingSpinner component.
 *
 * All props are optional with sensible defaults, allowing the component
 * to be used with zero configuration as a simple inline spinner:
 *
 * @example
 * ```tsx
 * // Minimal usage — medium spinner, no message
 * <LoadingSpinner />
 *
 * // Suspense fallback with full-screen centering
 * <Suspense fallback={<LoadingSpinner fullScreen message="Loading page..." />}>
 *   <LazyPage />
 * </Suspense>
 *
 * // Inline small spinner with custom color
 * <LoadingSpinner size="sm" color="text-blue-500" message="Fetching rows..." />
 *
 * // Overlay mode for blocking interactions during a save
 * <LoadingSpinner fullScreen overlay size="lg" message="Saving changes..." />
 * ```
 */
interface LoadingSpinnerProps {
  /** Size of the spinner animation. Controls both SVG dimensions and message text size. Defaults to 'md'. */
  size?: SpinnerSize;
  /** Optional descriptive message displayed below the spinner animation. */
  message?: string;
  /** Additional CSS class names merged onto the outermost container element. */
  className?: string;
  /** TailwindCSS text color class applied to the spinning SVG. Defaults to 'text-indigo-600'. */
  color?: string;
  /** When true, positions the spinner fixed and centered in the full viewport with z-50 stacking. Defaults to false. */
  fullScreen?: boolean;
  /** When true, renders a semi-transparent backdrop with blur behind the spinner. Best combined with fullScreen. Defaults to false. */
  overlay?: boolean;
}

// ---------------------------------------------------------------------------
// Size Configuration Constants
// ---------------------------------------------------------------------------

/**
 * Maps each spinner size variant to TailwindCSS width and height classes
 * that control the SVG element dimensions.
 *
 * sm  → 16×16px  (w-4 h-4)
 * md  → 32×32px  (w-8 h-8)
 * lg  → 48×48px  (w-12 h-12)
 * xl  → 64×64px  (w-16 h-16)
 */
const SPINNER_SIZE_CLASSES: Record<SpinnerSize, string> = {
  sm: 'w-4 h-4',
  md: 'w-8 h-8',
  lg: 'w-12 h-12',
  xl: 'w-16 h-16',
};

/**
 * Maps each spinner size variant to proportionally scaled TailwindCSS
 * font-size classes for the optional message text.
 *
 * sm  → text-xs  (12px)
 * md  → text-sm  (14px)
 * lg  → text-base (16px)
 * xl  → text-lg  (18px)
 */
const MESSAGE_SIZE_CLASSES: Record<SpinnerSize, string> = {
  sm: 'text-xs',
  md: 'text-sm',
  lg: 'text-base',
  xl: 'text-lg',
};

// ---------------------------------------------------------------------------
// Component Implementation
// ---------------------------------------------------------------------------

/**
 * LoadingSpinner — Animated loading state indicator component.
 *
 * Renders a spinning SVG circle animation (using TailwindCSS `animate-spin`)
 * with an optional descriptive message below. Supports inline display,
 * full-viewport centering, and semi-transparent overlay backdrop modes.
 *
 * The SVG consists of two paths:
 *   1. A faint (opacity-25) full circle providing the track
 *   2. A bold (opacity-75) quarter-arc that rotates via the spin animation
 *
 * Accessibility is implemented via:
 *   - `role="status"` marking the container as a live region
 *   - `aria-live="polite"` for non-intrusive screen reader announcements
 *   - `aria-label` with the loading message or a default "Loading" label
 *   - `aria-hidden="true"` on the decorative SVG element
 *   - A visually-hidden `<span className="sr-only">` for screen readers
 *
 * @param props - Component configuration options
 * @returns A React JSX element rendering the animated loading spinner
 */
function LoadingSpinner({
  size = 'md',
  message,
  className,
  color = 'text-indigo-600',
  fullScreen = false,
  overlay = false,
}: LoadingSpinnerProps): React.JSX.Element {
  /* Build the container class string from the combination of layout mode flags */
  const containerClasses = [
    'flex flex-col items-center justify-center gap-3',
    fullScreen ? 'fixed inset-0 z-50' : '',
    overlay ? 'bg-white/75 dark:bg-gray-900/75 backdrop-blur-sm' : '',
    className ?? '',
  ]
    .filter(Boolean)
    .join(' ');

  const spinner = (
    <div
      className={containerClasses}
      role="status"
      aria-live="polite"
      aria-label={message ?? 'Loading'}
    >
      {/* Spinning SVG circle — decorative, hidden from assistive technology */}
      <svg
        className={`animate-spin ${SPINNER_SIZE_CLASSES[size]} ${color}`}
        xmlns="http://www.w3.org/2000/svg"
        fill="none"
        viewBox="0 0 24 24"
        aria-hidden="true"
      >
        {/* Background track circle at reduced opacity */}
        <circle
          className="opacity-25"
          cx="12"
          cy="12"
          r="10"
          stroke="currentColor"
          strokeWidth="4"
        />
        {/* Foreground arc segment that creates the spinning visual */}
        <path
          className="opacity-75"
          fill="currentColor"
          d="M4 12a8 8 0 018-8V0C5.373 0 0 5.373 0 12h4zm2 5.291A7.962 7.962 0 014 12H0c0 3.042 1.135 5.824 3 7.938l3-2.647z"
        />
      </svg>

      {/* Optional loading message displayed below the spinner */}
      {message && (
        <p
          className={`${MESSAGE_SIZE_CLASSES[size]} text-gray-500 dark:text-gray-400 font-medium`}
        >
          {message}
        </p>
      )}

      {/* Screen reader only text providing accessible loading status */}
      <span className="sr-only">{message ?? 'Loading...'}</span>
    </div>
  );

  return spinner;
}

// ---------------------------------------------------------------------------
// Exports
// ---------------------------------------------------------------------------

export default LoadingSpinner;
export { LoadingSpinner };
export type { LoadingSpinnerProps, SpinnerSize };
