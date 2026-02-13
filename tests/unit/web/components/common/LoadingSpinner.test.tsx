/**
 * LoadingSpinner Component — Unit Test Suite
 *
 * Comprehensive Vitest + React Testing Library tests for the LoadingSpinner
 * foundational component used throughout the Synthetic ERP Data Generation
 * Platform Web Console.
 *
 * Covers:
 *   - Default rendering behaviour with no props
 *   - Four size variants (sm / md / lg / xl) and their TailwindCSS classes
 *   - Optional loading message text display and absence
 *   - fullScreen mode with fixed positioning and viewport coverage
 *   - Overlay mode with semi-transparent backdrop
 *   - Combined fullScreen + overlay + message rendering
 *   - Custom className passthrough and base class preservation
 *   - WAI-ARIA accessibility attributes (role, aria-label, aria-hidden)
 *
 * @module tests/unit/web/components/common/LoadingSpinner.test
 */

import { describe, it, expect } from 'vitest';
import { render, screen } from '@testing-library/react';
import '@testing-library/jest-dom';
import LoadingSpinner from '@/components/common/LoadingSpinner';

// ---------------------------------------------------------------------------
// Helper Utilities
// ---------------------------------------------------------------------------

/**
 * Default props representing the base configuration when no overrides are
 * applied. Used by the renderSpinner helper to provide a predictable baseline.
 */
const defaultProps = {} as const;

/**
 * Convenience wrapper that renders the LoadingSpinner with default props
 * merged with any test-specific overrides. Returns the render result from
 * React Testing Library for advanced queries when needed.
 *
 * @param overrides - Partial props to merge onto the defaults
 * @returns The React Testing Library RenderResult
 */
function renderSpinner(overrides: Record<string, unknown> = {}) {
  return render(<LoadingSpinner {...defaultProps} {...overrides} />);
}

// ---------------------------------------------------------------------------
// Test Suites
// ---------------------------------------------------------------------------

describe('LoadingSpinner', () => {
  // =========================================================================
  // Default Rendering
  // =========================================================================
  describe('Default Rendering', () => {
    it('renders without crashing with no props', () => {
      renderSpinner();
      const container = screen.getByRole('status');
      expect(container).toBeInTheDocument();
    });

    it('renders an SVG spinner element', () => {
      const { container } = renderSpinner();
      const svg = container.querySelector('svg');
      expect(svg).toBeInTheDocument();
    });

    it('applies animate-spin class on the SVG element', () => {
      const { container } = renderSpinner();
      const svg = container.querySelector('svg');
      expect(svg).toHaveClass('animate-spin');
    });

    it('renders with default md size when size prop not provided', () => {
      const { container } = renderSpinner();
      const svg = container.querySelector('svg');
      expect(svg).toHaveClass('w-8');
      expect(svg).toHaveClass('h-8');
    });

    it('has role="status" on the container for accessibility', () => {
      renderSpinner();
      const container = screen.getByRole('status');
      expect(container).toHaveAttribute('role', 'status');
    });

    it('has aria-label text for screen readers', () => {
      renderSpinner();
      const container = screen.getByRole('status');
      expect(container).toHaveAttribute('aria-label', 'Loading');
    });

    it('does not render message text by default', () => {
      const { container } = renderSpinner();
      // The only text content should be the sr-only span, not a visible <p>
      const paragraph = container.querySelector('p');
      expect(paragraph).not.toBeInTheDocument();
    });
  });

  // =========================================================================
  // Size Variants
  // =========================================================================
  describe('Size Variants', () => {
    it('applies w-4 h-4 classes for size="sm"', () => {
      const { container } = renderSpinner({ size: 'sm' });
      const svg = container.querySelector('svg');
      expect(svg).toHaveClass('w-4');
      expect(svg).toHaveClass('h-4');
    });

    it('applies w-8 h-8 classes for size="md"', () => {
      const { container } = renderSpinner({ size: 'md' });
      const svg = container.querySelector('svg');
      expect(svg).toHaveClass('w-8');
      expect(svg).toHaveClass('h-8');
    });

    it('applies w-12 h-12 classes for size="lg"', () => {
      const { container } = renderSpinner({ size: 'lg' });
      const svg = container.querySelector('svg');
      expect(svg).toHaveClass('w-12');
      expect(svg).toHaveClass('h-12');
    });

    it('applies w-16 h-16 classes for size="xl"', () => {
      const { container } = renderSpinner({ size: 'xl' });
      const svg = container.querySelector('svg');
      expect(svg).toHaveClass('w-16');
      expect(svg).toHaveClass('h-16');
    });

    it('applies default md sizing when size is not provided', () => {
      const { container } = renderSpinner();
      const svg = container.querySelector('svg');
      expect(svg).toHaveClass('w-8');
      expect(svg).toHaveClass('h-8');
    });
  });

  // =========================================================================
  // Loading Message
  // =========================================================================
  describe('Loading Message', () => {
    it('renders message text when message prop is provided', () => {
      renderSpinner({ message: 'Loading data...' });
      expect(screen.getByText('Loading data...')).toBeInTheDocument();
    });

    it('does not render message element when message is undefined', () => {
      const { container } = renderSpinner({ message: undefined });
      const paragraph = container.querySelector('p');
      expect(paragraph).not.toBeInTheDocument();
    });

    it('does not render message element when message is empty string', () => {
      const { container } = renderSpinner({ message: '' });
      const paragraph = container.querySelector('p');
      expect(paragraph).not.toBeInTheDocument();
    });

    it('renders message with correct text content', () => {
      renderSpinner({ message: 'Please wait...' });
      const messageEl = screen.getByText('Please wait...');
      expect(messageEl).toHaveTextContent('Please wait...');
    });

    it('message text appears below the spinner', () => {
      const { container } = renderSpinner({ message: 'Generating records...' });
      const svg = container.querySelector('svg');
      const paragraph = container.querySelector('p');
      expect(svg).toBeInTheDocument();
      expect(paragraph).toBeInTheDocument();

      // Verify DOM order: SVG appears before the message paragraph
      const statusContainer = screen.getByRole('status');
      const children = Array.from(statusContainer.children);
      const svgIndex = children.indexOf(svg as Element);
      const pIndex = children.indexOf(paragraph as Element);
      expect(svgIndex).toBeLessThan(pIndex);
    });
  });

  // =========================================================================
  // FullScreen Mode
  // =========================================================================
  describe('FullScreen Mode', () => {
    it('applies fixed positioning class when fullScreen is true', () => {
      renderSpinner({ fullScreen: true });
      const container = screen.getByRole('status');
      expect(container).toHaveClass('fixed');
    });

    it('applies inset-0 to cover entire viewport when fullScreen is true', () => {
      renderSpinner({ fullScreen: true });
      const container = screen.getByRole('status');
      expect(container).toHaveClass('inset-0');
    });

    it('applies z-50 for stacking above other content', () => {
      renderSpinner({ fullScreen: true });
      const container = screen.getByRole('status');
      expect(container).toHaveClass('z-50');
    });

    it('renders centered spinner in fullScreen mode', () => {
      const { container: wrapper } = renderSpinner({ fullScreen: true });
      const svg = wrapper.querySelector('svg');
      expect(svg).toBeInTheDocument();

      const statusContainer = screen.getByRole('status');
      expect(statusContainer).toHaveClass('items-center');
      expect(statusContainer).toHaveClass('justify-center');
    });

    it('does not apply fixed/inset-0 when fullScreen is false or undefined', () => {
      renderSpinner({ fullScreen: false });
      const container = screen.getByRole('status');
      expect(container).not.toHaveClass('fixed');
      expect(container).not.toHaveClass('inset-0');
      expect(container).not.toHaveClass('z-50');
    });

    it('applies flex items-center justify-center for centering', () => {
      renderSpinner({ fullScreen: true });
      const container = screen.getByRole('status');
      expect(container).toHaveClass('flex');
      expect(container).toHaveClass('items-center');
      expect(container).toHaveClass('justify-center');
    });
  });

  // =========================================================================
  // Overlay Mode
  // =========================================================================
  describe('Overlay Mode', () => {
    it('applies semi-transparent background when overlay is true', () => {
      renderSpinner({ overlay: true });
      const container = screen.getByRole('status');
      expect(container).toHaveClass('bg-white/75');
    });

    it('applies bg-white/75 backdrop class when overlay is true', () => {
      renderSpinner({ overlay: true });
      const container = screen.getByRole('status');
      // The component uses bg-white/75 and backdrop-blur-sm for the overlay
      expect(container).toHaveClass('bg-white/75');
      expect(container).toHaveClass('backdrop-blur-sm');
    });

    it('does not apply overlay backdrop when overlay is false or undefined', () => {
      renderSpinner({ overlay: false });
      const container = screen.getByRole('status');
      expect(container).not.toHaveClass('bg-white/75');
      expect(container).not.toHaveClass('backdrop-blur-sm');
    });

    it('overlay can be combined with fullScreen mode', () => {
      renderSpinner({ overlay: true, fullScreen: true });
      const container = screen.getByRole('status');
      // Verify both fullScreen and overlay classes are present
      expect(container).toHaveClass('fixed');
      expect(container).toHaveClass('inset-0');
      expect(container).toHaveClass('z-50');
      expect(container).toHaveClass('bg-white/75');
      expect(container).toHaveClass('backdrop-blur-sm');
    });
  });

  // =========================================================================
  // Combined Modes
  // =========================================================================
  describe('Combined Modes', () => {
    it('renders fullScreen with message correctly', () => {
      renderSpinner({ fullScreen: true, message: 'Loading page...' });
      const container = screen.getByRole('status');
      expect(container).toHaveClass('fixed');
      expect(container).toHaveClass('inset-0');
      expect(container).toHaveClass('z-50');
      expect(screen.getByText('Loading page...')).toBeInTheDocument();
    });

    it('renders overlay with message correctly', () => {
      renderSpinner({ overlay: true, message: 'Processing...' });
      const container = screen.getByRole('status');
      expect(container).toHaveClass('bg-white/75');
      expect(container).toHaveClass('backdrop-blur-sm');
      expect(screen.getByText('Processing...')).toBeInTheDocument();
    });

    it('renders fullScreen + overlay + message all together', () => {
      renderSpinner({
        fullScreen: true,
        overlay: true,
        message: 'Saving changes...',
      });
      const container = screen.getByRole('status');

      // fullScreen classes
      expect(container).toHaveClass('fixed');
      expect(container).toHaveClass('inset-0');
      expect(container).toHaveClass('z-50');

      // overlay classes
      expect(container).toHaveClass('bg-white/75');
      expect(container).toHaveClass('backdrop-blur-sm');

      // centering classes
      expect(container).toHaveClass('flex');
      expect(container).toHaveClass('items-center');
      expect(container).toHaveClass('justify-center');

      // message text
      expect(screen.getByText('Saving changes...')).toBeInTheDocument();
    });
  });

  // =========================================================================
  // Custom Styling
  // =========================================================================
  describe('Custom Styling', () => {
    it('applies additional className to container', () => {
      renderSpinner({ className: 'my-custom-class' });
      const container = screen.getByRole('status');
      expect(container).toHaveClass('my-custom-class');
    });

    it('preserves base classes when custom className is added', () => {
      renderSpinner({ className: 'extra-padding' });
      const container = screen.getByRole('status');
      // Base layout classes should still be present
      expect(container).toHaveClass('flex');
      expect(container).toHaveClass('flex-col');
      expect(container).toHaveClass('items-center');
      expect(container).toHaveClass('justify-center');
      expect(container).toHaveClass('gap-3');
      // Custom class should also be present
      expect(container).toHaveClass('extra-padding');
    });

    it('spinner uses text-indigo-600 color', () => {
      const { container } = renderSpinner();
      const svg = container.querySelector('svg');
      expect(svg).toHaveClass('text-indigo-600');
    });
  });

  // =========================================================================
  // Accessibility
  // =========================================================================
  describe('Accessibility', () => {
    it('has role="status" attribute on container', () => {
      renderSpinner();
      const container = screen.getByRole('status');
      expect(container).toHaveAttribute('role', 'status');
    });

    it('has aria-label describing loading state', () => {
      renderSpinner();
      const container = screen.getByRole('status');
      expect(container).toHaveAttribute('aria-label', 'Loading');
    });

    it('has aria-label with custom message when message prop is provided', () => {
      renderSpinner({ message: 'Fetching data...' });
      const container = screen.getByRole('status');
      expect(container).toHaveAttribute('aria-label', 'Fetching data...');
    });

    it('SVG has aria-hidden="true" to hide decorative element from screen readers', () => {
      const { container } = renderSpinner();
      const svg = container.querySelector('svg');
      expect(svg).toHaveAttribute('aria-hidden', 'true');
    });

    it('message text is accessible as part of the status region', () => {
      renderSpinner({ message: 'Loading records...' });
      const statusRegion = screen.getByRole('status');
      // Message paragraph is a child of the role="status" container,
      // making it part of the accessible status region
      const messageEl = screen.getByText('Loading records...');
      expect(statusRegion).toContainElement(messageEl);
    });
  });
});
