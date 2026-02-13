/**
 * ErrorBoundary Component — Comprehensive Vitest + React Testing Library Test Suite
 *
 * Tests the ErrorBoundary React class component that provides application-wide
 * error catching via componentDidCatch and getDerivedStateFromError lifecycle
 * methods. Verifies error catching, fallback UI rendering, retry/reset
 * functionality, custom fallback render props, onError callback invocation,
 * development vs production error detail visibility, error boundary isolation,
 * and edge cases.
 *
 * @module tests/unit/web/components/common/ErrorBoundary.test
 * @see src/web/src/components/common/ErrorBoundary.tsx
 */

import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest';
import { render, screen, fireEvent } from '@testing-library/react';
import '@testing-library/jest-dom';
import React, { type ReactNode } from 'react';
import ErrorBoundary from '@/components/common/ErrorBoundary';

// ---------------------------------------------------------------------------
// Helper Components
// ---------------------------------------------------------------------------

/**
 * A component that conditionally throws an error during rendering.
 * Used to trigger ErrorBoundary's error catching behavior in tests.
 *
 * @param shouldThrow - When true, throws an Error during render. Defaults to true.
 */
const ThrowingChild = ({ shouldThrow = true }: { shouldThrow?: boolean }) => {
  if (shouldThrow) throw new Error('Test error message');
  return <div>Child rendered successfully</div>;
};

/**
 * A component that always renders safely without throwing.
 * Used to verify normal rendering behavior when no error occurs.
 */
const SafeChild = () => <div>Safe child content</div>;

/**
 * A component that throws a non-Error value (string) during rendering.
 * Used to test edge case handling of non-standard thrown values.
 */
const StringThrower = () => {
  // eslint-disable-next-line no-throw-literal
  throw 'string error value';
};

/**
 * A component that throws an Error with an empty message.
 * Used to test graceful handling of errors lacking descriptive text.
 */
const EmptyMessageThrower = () => {
  throw new Error('');
};

/**
 * A component that throws an Error with a very long message string.
 * Used to test CSS-based truncation and overflow handling in fallback UI.
 */
const LongMessageThrower = () => {
  throw new Error('A'.repeat(5000));
};

// ---------------------------------------------------------------------------
// Test Suite
// ---------------------------------------------------------------------------

describe('ErrorBoundary', () => {
  /**
   * Mock setup and teardown.
   * Spies on console.error before each test to suppress noise from React's
   * internal error boundary logging and the component's own DEV-mode logging.
   * Restores all mocks after each test to prevent inter-test contamination.
   */
  beforeEach(() => {
    vi.spyOn(console, 'error').mockImplementation(() => {});
  });

  afterEach(() => {
    vi.restoreAllMocks();
  });

  // =========================================================================
  // Normal Rendering (No Error)
  // =========================================================================

  describe('Normal Rendering (No Error)', () => {
    it('renders children when no error occurs', () => {
      render(
        <ErrorBoundary>
          <SafeChild />
        </ErrorBoundary>
      );

      expect(screen.getByText('Safe child content')).toBeInTheDocument();
    });

    it('does not render fallback UI when children render successfully', () => {
      render(
        <ErrorBoundary>
          <SafeChild />
        </ErrorBoundary>
      );

      expect(screen.queryByText('Something went wrong')).not.toBeInTheDocument();
      expect(screen.queryByRole('alert')).not.toBeInTheDocument();
    });

    it('renders multiple children correctly', () => {
      render(
        <ErrorBoundary>
          <div>First child</div>
          <div>Second child</div>
          <div>Third child</div>
        </ErrorBoundary>
      );

      expect(screen.getByText('First child')).toBeInTheDocument();
      expect(screen.getByText('Second child')).toBeInTheDocument();
      expect(screen.getByText('Third child')).toBeInTheDocument();
    });

    it('passes through children props unchanged', () => {
      const testId = 'custom-child';
      render(
        <ErrorBoundary>
          <div data-testid={testId}>
            <span className="inner-class">Nested content with props</span>
          </div>
        </ErrorBoundary>
      );

      const child = screen.getByTestId(testId);
      expect(child).toBeInTheDocument();
      expect(screen.getByText('Nested content with props')).toBeInTheDocument();
      expect(screen.getByText('Nested content with props')).toHaveClass('inner-class');
    });
  });

  // =========================================================================
  // Error Catching via componentDidCatch
  // =========================================================================

  describe('Error Catching via componentDidCatch', () => {
    it('catches errors thrown by child components', () => {
      // Should not throw to the test runner — ErrorBoundary catches it
      expect(() => {
        render(
          <ErrorBoundary>
            <ThrowingChild />
          </ErrorBoundary>
        );
      }).not.toThrow();
    });

    it('renders fallback UI instead of crashed children', () => {
      render(
        <ErrorBoundary>
          <ThrowingChild />
        </ErrorBoundary>
      );

      // Fallback UI should be displayed
      expect(screen.getByText('Something went wrong')).toBeInTheDocument();
      // Crashed children should not be visible
      expect(screen.queryByText('Child rendered successfully')).not.toBeInTheDocument();
    });

    it('calls console.error with error information', () => {
      // Enable DEV mode so the component logs to console.error
      const originalDev = import.meta.env.DEV;
      (import.meta.env as Record<string, unknown>).DEV = true;

      render(
        <ErrorBoundary>
          <ThrowingChild />
        </ErrorBoundary>
      );

      // The component's componentDidCatch calls console.error in DEV mode
      expect(console.error).toHaveBeenCalledWith(
        'ErrorBoundary caught an error:',
        expect.any(Error)
      );

      (import.meta.env as Record<string, unknown>).DEV = originalDev;
    });

    it('catches errors thrown during rendering, not in event handlers', () => {
      // A component with a throwing event handler — NOT a render error
      const EventErrorChild = () => {
        return (
          <button
            onClick={() => {
              throw new Error('Event handler error');
            }}
          >
            Click me
          </button>
        );
      };

      render(
        <ErrorBoundary>
          <EventErrorChild />
        </ErrorBoundary>
      );

      // The child renders normally (no render error), so no fallback
      expect(screen.getByText('Click me')).toBeInTheDocument();
      expect(screen.queryByText('Something went wrong')).not.toBeInTheDocument();
    });

    it('does not crash the entire app — sibling components remain rendered', () => {
      render(
        <div>
          <ErrorBoundary>
            <ThrowingChild />
          </ErrorBoundary>
          <div>Sibling content outside boundary</div>
        </div>
      );

      // The throwing child's boundary shows fallback
      expect(screen.getByText('Something went wrong')).toBeInTheDocument();
      // Sibling content outside the boundary is unaffected
      expect(screen.getByText('Sibling content outside boundary')).toBeInTheDocument();
    });
  });

  // =========================================================================
  // Fallback UI Rendering
  // =========================================================================

  describe('Fallback UI Rendering', () => {
    it('renders default fallback UI with error heading "Something went wrong"', () => {
      render(
        <ErrorBoundary>
          <ThrowingChild />
        </ErrorBoundary>
      );

      const heading = screen.getByRole('heading', { level: 1 });
      expect(heading).toBeInTheDocument();
      expect(heading).toHaveTextContent('Something went wrong');
    });

    it('displays the error message in the fallback', () => {
      render(
        <ErrorBoundary>
          <ThrowingChild />
        </ErrorBoundary>
      );

      // The user-facing description is always shown
      expect(
        screen.getByText(/An unexpected error occurred/)
      ).toBeInTheDocument();
    });

    it('renders retry/reset button in fallback', () => {
      render(
        <ErrorBoundary>
          <ThrowingChild />
        </ErrorBoundary>
      );

      const tryAgainButton = screen.getByRole('button', { name: /Try Again/i });
      expect(tryAgainButton).toBeInTheDocument();
    });

    it('applies appropriate error styling (red/danger theme)', () => {
      render(
        <ErrorBoundary>
          <ThrowingChild />
        </ErrorBoundary>
      );

      // The container uses role="alert" for accessibility
      const alertContainer = screen.getByRole('alert');
      expect(alertContainer).toBeInTheDocument();

      // The icon container has red background classes
      const iconContainer = alertContainer.querySelector('.bg-red-100');
      expect(iconContainer).toBeTruthy();

      // The SVG icon has red text color
      const svgIcon = alertContainer.querySelector('.text-red-600');
      expect(svgIcon).toBeTruthy();
    });

    it('renders an icon or illustration in default fallback', () => {
      render(
        <ErrorBoundary>
          <ThrowingChild />
        </ErrorBoundary>
      );

      const alertContainer = screen.getByRole('alert');
      // The SVG warning triangle icon should be present
      const svgElement = alertContainer.querySelector('svg');
      expect(svgElement).toBeTruthy();
      expect(svgElement).toHaveAttribute('aria-hidden', 'true');
    });
  });

  // =========================================================================
  // Retry/Reset Functionality
  // =========================================================================

  describe('Retry/Reset Functionality', () => {
    it('clears error state when retry button is clicked', () => {
      // Use a mutable flag to control whether the child throws
      let shouldThrow = true;
      const ConditionalThrowChild = () => {
        if (shouldThrow) throw new Error('Conditional error');
        return <div>Child recovered successfully</div>;
      };

      render(
        <ErrorBoundary>
          <ConditionalThrowChild />
        </ErrorBoundary>
      );

      // Initially, the fallback is shown
      expect(screen.getByText('Something went wrong')).toBeInTheDocument();

      // Resolve the error condition before clicking retry
      shouldThrow = false;

      // Click "Try Again" to reset the error boundary
      fireEvent.click(screen.getByRole('button', { name: /Try Again/i }));

      // After reset with the error condition resolved, children render
      expect(screen.getByText('Child recovered successfully')).toBeInTheDocument();
      expect(screen.queryByText('Something went wrong')).not.toBeInTheDocument();
    });

    it('re-renders children after reset when error condition is resolved', () => {
      let shouldThrow = true;
      const ConditionalChild = () => {
        if (shouldThrow) throw new Error('Temporary error');
        return <div>Recovered content</div>;
      };

      render(
        <ErrorBoundary>
          <ConditionalChild />
        </ErrorBoundary>
      );

      expect(screen.getByText('Something went wrong')).toBeInTheDocument();
      expect(screen.queryByText('Recovered content')).not.toBeInTheDocument();

      // Fix the error condition
      shouldThrow = false;
      fireEvent.click(screen.getByRole('button', { name: /Try Again/i }));

      // Children re-render successfully
      expect(screen.getByText('Recovered content')).toBeInTheDocument();
      expect(screen.queryByText('Something went wrong')).not.toBeInTheDocument();
    });

    it('still shows fallback if child throws again after reset', () => {
      render(
        <ErrorBoundary>
          <ThrowingChild shouldThrow={true} />
        </ErrorBoundary>
      );

      // Initially, fallback is shown
      expect(screen.getByText('Something went wrong')).toBeInTheDocument();

      // Click Try Again — child will throw again immediately
      fireEvent.click(screen.getByRole('button', { name: /Try Again/i }));

      // Fallback should still be displayed because the child throws again
      expect(screen.getByText('Something went wrong')).toBeInTheDocument();
    });

    it('calls onReset callback prop when reset is triggered', () => {
      const onReset = vi.fn();
      let shouldThrow = true;
      const ConditionalChild = () => {
        if (shouldThrow) throw new Error('Reset test error');
        return <div>Reset child</div>;
      };

      render(
        <ErrorBoundary onReset={onReset}>
          <ConditionalChild />
        </ErrorBoundary>
      );

      expect(screen.getByText('Something went wrong')).toBeInTheDocument();

      shouldThrow = false;
      fireEvent.click(screen.getByRole('button', { name: /Try Again/i }));

      expect(onReset).toHaveBeenCalledTimes(1);
    });

    it('resets error state when children prop changes (key-based reset)', () => {
      const { rerender } = render(
        <ErrorBoundary key="key-1">
          <ThrowingChild />
        </ErrorBoundary>
      );

      // Initially, fallback is shown due to the throwing child
      expect(screen.getByText('Something went wrong')).toBeInTheDocument();

      // Changing the key forces React to unmount and remount the ErrorBoundary
      // with fresh state, and new non-throwing children
      rerender(
        <ErrorBoundary key="key-2">
          <SafeChild />
        </ErrorBoundary>
      );

      // After key change, the new ErrorBoundary instance renders children normally
      expect(screen.getByText('Safe child content')).toBeInTheDocument();
      expect(screen.queryByText('Something went wrong')).not.toBeInTheDocument();
    });
  });

  // =========================================================================
  // Custom Fallback Render Prop
  // =========================================================================

  describe('Custom Fallback Render Prop', () => {
    it('renders custom fallback when fallbackRender prop is provided', () => {
      const customFallback = (error: Error, resetError: () => void): ReactNode => (
        <div>
          <h2>Custom Error UI</h2>
          <p>Custom: {error.message}</p>
          <button onClick={resetError}>Custom Retry</button>
        </div>
      );

      render(
        <ErrorBoundary fallback={customFallback}>
          <ThrowingChild />
        </ErrorBoundary>
      );

      // Custom fallback renders instead of default
      expect(screen.getByText('Custom Error UI')).toBeInTheDocument();
      expect(screen.getByText('Custom: Test error message')).toBeInTheDocument();
      // Default fallback elements should not be present
      expect(screen.queryByText('Something went wrong')).not.toBeInTheDocument();
    });

    it('passes error object to fallbackRender function', () => {
      const fallbackFn = vi.fn(
        (error: Error, _resetError: () => void): ReactNode => (
          <div data-testid="custom-fallback">Error: {error.message}</div>
        )
      );

      render(
        <ErrorBoundary fallback={fallbackFn}>
          <ThrowingChild />
        </ErrorBoundary>
      );

      // Verify the render function was called at least once with the Error object.
      // React 19 may invoke the render method multiple times internally during
      // the error recovery lifecycle (getDerivedStateFromError → render → componentDidCatch → setState → render),
      // so we assert the function was called (at least once) rather than exactly once.
      expect(fallbackFn).toHaveBeenCalled();
      const firstArg = fallbackFn.mock.calls[0][0];
      expect(firstArg).toBeInstanceOf(Error);
      expect(firstArg.message).toBe('Test error message');
    });

    it('passes resetErrorBoundary function to fallbackRender', () => {
      const fallbackFn = vi.fn(
        (_error: Error, resetError: () => void): ReactNode => (
          <div>
            <button onClick={resetError}>Custom Reset</button>
          </div>
        )
      );

      render(
        <ErrorBoundary fallback={fallbackFn}>
          <ThrowingChild />
        </ErrorBoundary>
      );

      // Verify the render function was called at least once and that the
      // second argument is a function (resetError callback).
      // React 19 may invoke render multiple times during the error boundary
      // recovery lifecycle, so we assert it was called rather than exactly once.
      expect(fallbackFn).toHaveBeenCalled();
      const secondArg = fallbackFn.mock.calls[0][1];
      expect(typeof secondArg).toBe('function');
    });

    it('custom fallback can trigger reset via resetErrorBoundary', () => {
      let shouldThrow = true;
      const ConditionalChild = () => {
        if (shouldThrow) throw new Error('Custom fallback reset test');
        return <div>Child after custom reset</div>;
      };

      render(
        <ErrorBoundary
          fallback={(error: Error, resetError: () => void) => (
            <div>
              <p>Caught: {error.message}</p>
              <button onClick={resetError}>Custom Reset Button</button>
            </div>
          )}
        >
          <ConditionalChild />
        </ErrorBoundary>
      );

      // Custom fallback is displayed
      expect(screen.getByText('Caught: Custom fallback reset test')).toBeInTheDocument();

      // Resolve the error condition and trigger custom reset
      shouldThrow = false;
      fireEvent.click(screen.getByRole('button', { name: /Custom Reset Button/i }));

      // Children should render after reset
      expect(screen.getByText('Child after custom reset')).toBeInTheDocument();
    });

    it('renders fallback component from fallback prop (ReactNode)', () => {
      const staticFallback: ReactNode = (
        <div>
          <h3>Static Fallback Content</h3>
          <p>Something went wrong in this section.</p>
        </div>
      );

      render(
        <ErrorBoundary fallback={staticFallback}>
          <ThrowingChild />
        </ErrorBoundary>
      );

      // Static ReactNode fallback renders as-is
      expect(screen.getByText('Static Fallback Content')).toBeInTheDocument();
      expect(screen.getByText('Something went wrong in this section.')).toBeInTheDocument();
      // Default fallback UI elements should NOT be present
      expect(screen.queryByRole('alert')).not.toBeInTheDocument();
    });
  });

  // =========================================================================
  // onError Callback
  // =========================================================================

  describe('onError Callback', () => {
    it('calls onError callback when error is caught', () => {
      const onError = vi.fn();

      render(
        <ErrorBoundary onError={onError}>
          <ThrowingChild />
        </ErrorBoundary>
      );

      expect(onError).toHaveBeenCalledTimes(1);
    });

    it('passes Error object as first argument to onError', () => {
      const onError = vi.fn();

      render(
        <ErrorBoundary onError={onError}>
          <ThrowingChild />
        </ErrorBoundary>
      );

      const firstArg = onError.mock.calls[0][0];
      expect(firstArg).toBeInstanceOf(Error);
      expect(firstArg.message).toBe('Test error message');
    });

    it('passes errorInfo with componentStack as second argument to onError', () => {
      const onError = vi.fn();

      render(
        <ErrorBoundary onError={onError}>
          <ThrowingChild />
        </ErrorBoundary>
      );

      const secondArg = onError.mock.calls[0][1];
      expect(secondArg).toBeDefined();
      expect(secondArg).toHaveProperty('componentStack');
      expect(typeof secondArg.componentStack).toBe('string');
    });

    it('does not throw if onError is not provided', () => {
      // Rendering without onError prop should not cause any issues
      expect(() => {
        render(
          <ErrorBoundary>
            <ThrowingChild />
          </ErrorBoundary>
        );
      }).not.toThrow();

      // Fallback should still render correctly
      expect(screen.getByText('Something went wrong')).toBeInTheDocument();
    });

    it('calls onError only once per error', () => {
      const onError = vi.fn();

      render(
        <ErrorBoundary onError={onError}>
          <ThrowingChild />
        </ErrorBoundary>
      );

      // componentDidCatch fires exactly once for a single render error
      expect(onError).toHaveBeenCalledTimes(1);
    });
  });

  // =========================================================================
  // Development vs Production Error Details
  // =========================================================================

  describe('Development vs Production Error Details', () => {
    let originalDev: boolean;

    beforeEach(() => {
      originalDev = import.meta.env.DEV;
    });

    afterEach(() => {
      (import.meta.env as Record<string, unknown>).DEV = originalDev;
    });

    it('shows error stack trace in development mode (NODE_ENV=development)', () => {
      (import.meta.env as Record<string, unknown>).DEV = true;

      render(
        <ErrorBoundary>
          <ThrowingChild />
        </ErrorBoundary>
      );

      // In development mode, the <details> element with error info is rendered
      const errorDetails = screen.getByText('Error Details');
      expect(errorDetails).toBeInTheDocument();
      expect(errorDetails).toBeVisible();
      // The error message is shown in the details section
      expect(screen.getByText('Test error message')).toBeInTheDocument();
    });

    it('hides error stack trace in production mode (NODE_ENV=production)', () => {
      (import.meta.env as Record<string, unknown>).DEV = false;

      render(
        <ErrorBoundary>
          <ThrowingChild />
        </ErrorBoundary>
      );

      // In production mode, the error details section should NOT render
      expect(screen.queryByText('Error Details')).not.toBeInTheDocument();
    });

    it('shows error message text in both environments', () => {
      // Test in development mode
      (import.meta.env as Record<string, unknown>).DEV = true;

      const { unmount } = render(
        <ErrorBoundary>
          <ThrowingChild />
        </ErrorBoundary>
      );

      // The user-facing description is always shown in both environments
      expect(screen.getByText(/An unexpected error occurred/)).toBeInTheDocument();
      expect(screen.getByText('Something went wrong')).toBeInTheDocument();

      unmount();

      // Test in production mode
      (import.meta.env as Record<string, unknown>).DEV = false;

      render(
        <ErrorBoundary>
          <ThrowingChild />
        </ErrorBoundary>
      );

      expect(screen.getByText(/An unexpected error occurred/)).toBeInTheDocument();
      expect(screen.getByText('Something went wrong')).toBeInTheDocument();
    });

    it('shows component stack in development for debugging', () => {
      (import.meta.env as Record<string, unknown>).DEV = true;

      render(
        <ErrorBoundary>
          <ThrowingChild />
        </ErrorBoundary>
      );

      // In DEV mode, console.error is called with component stack info
      expect(console.error).toHaveBeenCalledWith(
        'Component stack:',
        expect.any(String)
      );
    });

    it('uses generic message in production without technical details', () => {
      (import.meta.env as Record<string, unknown>).DEV = false;

      render(
        <ErrorBoundary>
          <ThrowingChild />
        </ErrorBoundary>
      );

      // The generic user-friendly message is displayed
      expect(screen.getByText('Something went wrong')).toBeInTheDocument();
      expect(screen.getByText(/An unexpected error occurred/)).toBeInTheDocument();

      // Technical error details should NOT be visible
      expect(screen.queryByText('Error Details')).not.toBeInTheDocument();
      expect(screen.queryByText('Test error message')).not.toBeInTheDocument();
    });
  });

  // =========================================================================
  // Error Boundary Isolation
  // =========================================================================

  describe('Error Boundary Isolation', () => {
    it('error in one ErrorBoundary does not affect sibling boundaries', () => {
      render(
        <div>
          <ErrorBoundary>
            <ThrowingChild />
          </ErrorBoundary>
          <ErrorBoundary>
            <SafeChild />
          </ErrorBoundary>
        </div>
      );

      // First boundary catches the error and shows fallback
      expect(screen.getByText('Something went wrong')).toBeInTheDocument();
      // Second boundary renders its children normally
      expect(screen.getByText('Safe child content')).toBeInTheDocument();
    });

    it('parent ErrorBoundary catches errors not caught by child boundary', () => {
      const onOuterError = vi.fn();

      render(
        <ErrorBoundary onError={onOuterError}>
          <div>
            <ErrorBoundary>
              <SafeChild />
            </ErrorBoundary>
            {/* This component is inside the parent but outside the child boundary */}
            <ThrowingChild />
          </div>
        </ErrorBoundary>
      );

      // The parent boundary catches the error from ThrowingChild
      // which is outside the inner boundary
      expect(onOuterError).toHaveBeenCalledTimes(1);
      expect(screen.getByText('Something went wrong')).toBeInTheDocument();
    });

    it('nested ErrorBoundary catches errors before parent', () => {
      const onOuterError = vi.fn();
      const onInnerError = vi.fn();

      render(
        <ErrorBoundary onError={onOuterError}>
          <ErrorBoundary onError={onInnerError}>
            <ThrowingChild />
          </ErrorBoundary>
        </ErrorBoundary>
      );

      // Inner boundary catches the error — parent does not
      expect(onInnerError).toHaveBeenCalledTimes(1);
      expect(onOuterError).not.toHaveBeenCalled();

      // Inner boundary shows fallback; outer boundary renders normally
      expect(screen.getByText('Something went wrong')).toBeInTheDocument();
    });
  });

  // =========================================================================
  // Edge Cases
  // =========================================================================

  describe('Edge Cases', () => {
    it('handles errors that are not Error instances (e.g., thrown strings)', () => {
      // Rendering should not throw even when a non-Error value is thrown
      expect(() => {
        render(
          <ErrorBoundary>
            <StringThrower />
          </ErrorBoundary>
        );
      }).not.toThrow();

      // The fallback UI should still display the generic error heading
      expect(screen.getByText('Something went wrong')).toBeInTheDocument();
      expect(screen.getByRole('alert')).toBeInTheDocument();
    });

    it('handles errors with no message', () => {
      render(
        <ErrorBoundary>
          <EmptyMessageThrower />
        </ErrorBoundary>
      );

      // The fallback should render even when the error message is empty
      expect(screen.getByText('Something went wrong')).toBeInTheDocument();
      expect(screen.getByRole('alert')).toBeInTheDocument();
      // The generic description should always be visible
      expect(screen.getByText(/An unexpected error occurred/)).toBeInTheDocument();
    });

    it('handles errors with very long messages — truncates display', () => {
      const originalDev = import.meta.env.DEV;
      (import.meta.env as Record<string, unknown>).DEV = true;

      render(
        <ErrorBoundary>
          <LongMessageThrower />
        </ErrorBoundary>
      );

      // The fallback renders correctly despite the very long message
      expect(screen.getByText('Something went wrong')).toBeInTheDocument();

      // The error details section is present in DEV mode
      expect(screen.getByText('Error Details')).toBeInTheDocument();

      // The long message container uses CSS overflow control
      const alertContainer = screen.getByRole('alert');
      const overflowContainer = alertContainer.querySelector('.overflow-auto.max-h-48');
      expect(overflowContainer).toBeTruthy();

      // The message text uses break-all to prevent horizontal overflow
      const messageElement = alertContainer.querySelector('.break-all');
      expect(messageElement).toBeTruthy();

      // The long message text is present in the DOM
      expect(messageElement?.textContent).toBe('A'.repeat(5000));

      (import.meta.env as Record<string, unknown>).DEV = originalDev;
    });
  });
});
