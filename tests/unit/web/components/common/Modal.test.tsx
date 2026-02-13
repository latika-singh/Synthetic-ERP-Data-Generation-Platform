/**
 * Modal Component Unit Tests
 *
 * Comprehensive Vitest + React Testing Library test suite for the Modal
 * dialog component (src/web/src/components/common/Modal.tsx).
 *
 * Covers:
 * - Open/close state toggling and render behavior
 * - Overlay backdrop click-to-close behavior (closeOnOverlay prop)
 * - ESC key press handling (closeOnEsc prop)
 * - Focus trap cycling through focusable elements with Tab/Shift+Tab
 * - Body scroll lock (document.body.style.overflow='hidden') when open
 * - Action button rendering with primary/secondary/danger variants
 * - Portal rendering via createPortal to document.body
 * - Accessibility attributes (role=dialog, aria-modal, aria-labelledby, aria-describedby)
 * - Title and description rendering
 * - Close button in header
 * - Size variants (sm/md/lg/xl/full)
 * - Header and footer visibility toggles
 * - Focus restoration to trigger element on close
 *
 * @module tests/unit/web/components/common/Modal.test
 */

import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest';
import { render, screen, fireEvent, waitFor, act } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import '@testing-library/jest-dom';
import React from 'react';
import Modal from '@/components/common/Modal';
import type { ModalProps, ModalAction } from '@/components/common/Modal';

// ---------------------------------------------------------------------------
// Mock: @/store/uiStore
// ---------------------------------------------------------------------------

/**
 * Mock the Zustand UI store to isolate the Modal component from global state.
 * The Modal uses useUIStore with selector functions to access:
 *   - modal: { isOpen, modalId, modalProps } — current global modal state
 *   - openModal(): action to register a modal in the global store
 *   - closeModal(): action to clear the globally tracked modal state
 *
 * This mock provides controlled test doubles with a default closed modal state
 * so the component can be tested independently from the real Zustand store.
 */
vi.mock('@/store/uiStore', () => ({
  useUIStore: vi.fn((selector: (state: Record<string, unknown>) => unknown) => {
    const mockState = {
      modal: { isOpen: false, modalId: null, modalProps: {} },
      openModal: vi.fn(),
      closeModal: vi.fn(),
    };
    return selector(mockState);
  }),
}));

// ---------------------------------------------------------------------------
// Test Helper
// ---------------------------------------------------------------------------

/**
 * Renders the Modal component with sensible default props merged with any
 * overrides. Returns the render result, the merged props for assertions,
 * and a convenience `rerenderModal` function for updating props mid-test.
 *
 * Default props:
 *   isOpen: true
 *   onClose: vi.fn()
 *   title: 'Test Modal'
 *   children: <p>Modal content</p>
 *
 * @param overrides - Partial ModalProps to merge with defaults
 * @returns Render result, merged props, and rerenderModal utility
 */
function renderModal(overrides: Partial<ModalProps> = {}) {
  const defaultChildren: React.ReactNode = <p>Modal content</p>;

  const props: ModalProps = {
    isOpen: true,
    onClose: vi.fn(),
    title: 'Test Modal',
    children: defaultChildren,
    ...overrides,
  };

  const result = render(<Modal {...props} />);

  return {
    ...result,
    props,
    /**
     * Re-renders the Modal with updated props while preserving the original
     * defaults. Useful for testing open/close transitions, prop changes, etc.
     */
    rerenderModal: (moreOverrides: Partial<ModalProps> = {}) => {
      const newProps: ModalProps = { ...props, ...moreOverrides };
      result.rerender(<Modal {...newProps} />);
      return newProps;
    },
  };
}

// ---------------------------------------------------------------------------
// Lifecycle Hooks
// ---------------------------------------------------------------------------

beforeEach(() => {
  // Ensure a clean body scroll state before each test
  document.body.style.overflow = '';
  vi.clearAllMocks();
});

afterEach(() => {
  // Remove any portal-rendered modal elements that may have leaked between tests
  document.body
    .querySelectorAll('[role="dialog"]')
    .forEach((el) => el.remove());
  // Reset body scroll state after each test
  document.body.style.overflow = '';
});

// ===========================================================================
// Test Suites
// ===========================================================================

describe('Modal', () => {
  // -------------------------------------------------------------------------
  // Open/Close State
  // -------------------------------------------------------------------------
  describe('Open/Close State', () => {
    it('renders modal content when isOpen is true', () => {
      renderModal();

      expect(screen.getByRole('dialog')).toBeInTheDocument();
      expect(screen.getByText('Modal content')).toBeInTheDocument();
    });

    it('does not render anything when isOpen is false', () => {
      renderModal({ isOpen: false });

      expect(screen.queryByRole('dialog')).not.toBeInTheDocument();
      expect(screen.queryByText('Modal content')).not.toBeInTheDocument();
    });

    it('calls onClose when close button (X) is clicked', () => {
      const { props } = renderModal();

      fireEvent.click(screen.getByLabelText('Close modal'));

      expect(props.onClose).toHaveBeenCalledTimes(1);
    });

    it('hides close button when showCloseButton is false', () => {
      renderModal({ showCloseButton: false });

      expect(screen.getByText('Test Modal')).toBeInTheDocument();
      expect(screen.queryByLabelText('Close modal')).not.toBeInTheDocument();
    });

    it('renders title text in modal header', () => {
      renderModal({ title: 'My Custom Title' });

      const titleElement = screen.getByText('My Custom Title');
      expect(titleElement).toBeInTheDocument();
      expect(titleElement.tagName.toLowerCase()).toBe('h2');
    });

    it('renders description text below title', () => {
      renderModal({ description: 'Some description text' });

      const descElement = screen.getByText('Some description text');
      expect(descElement).toBeInTheDocument();
      expect(descElement.tagName.toLowerCase()).toBe('p');
    });

    it('renders children in modal body', () => {
      renderModal({
        children: <span data-testid="custom-child">Custom Child Content</span>,
      });

      expect(screen.getByTestId('custom-child')).toBeInTheDocument();
      expect(screen.getByText('Custom Child Content')).toBeInTheDocument();
    });
  });

  // -------------------------------------------------------------------------
  // Overlay Backdrop Click-to-Close
  // -------------------------------------------------------------------------
  describe('Overlay Backdrop Click-to-Close', () => {
    it('calls onClose when clicking the overlay backdrop', () => {
      const { props } = renderModal();

      // The click handler on role="dialog" checks e.target === e.currentTarget.
      // Clicking the dialog element directly simulates clicking the overlay area.
      const dialog = screen.getByRole('dialog');
      fireEvent.click(dialog);

      expect(props.onClose).toHaveBeenCalledTimes(1);
    });

    it('does NOT call onClose when clicking inside the modal panel', () => {
      const { props } = renderModal();

      // Clicking a child: event.target is the child, event.currentTarget is
      // the dialog, so e.target !== e.currentTarget → no close.
      fireEvent.click(screen.getByText('Modal content'));

      expect(props.onClose).not.toHaveBeenCalled();
    });

    it('does NOT call onClose on overlay click when closeOnOverlay is false', () => {
      const { props } = renderModal({ closeOnOverlay: false });

      const dialog = screen.getByRole('dialog');
      fireEvent.click(dialog);

      expect(props.onClose).not.toHaveBeenCalled();
    });

    it('renders semi-transparent backdrop (bg-black/50)', () => {
      renderModal();

      const dialog = screen.getByRole('dialog');
      const backdrop = dialog.querySelector('[aria-hidden="true"]');

      expect(backdrop).toBeInTheDocument();
      expect(backdrop).toHaveClass('bg-black/50');
    });
  });

  // -------------------------------------------------------------------------
  // ESC Key Handling
  // -------------------------------------------------------------------------
  describe('ESC Key Handling', () => {
    it('calls onClose when ESC key is pressed', () => {
      const { props } = renderModal();

      // ESC handler is attached to document via addEventListener
      fireEvent.keyDown(document, { key: 'Escape' });

      expect(props.onClose).toHaveBeenCalledTimes(1);
    });

    it('does NOT call onClose on ESC when closeOnEsc is false', () => {
      const { props } = renderModal({ closeOnEsc: false });

      fireEvent.keyDown(document, { key: 'Escape' });

      expect(props.onClose).not.toHaveBeenCalled();
    });

    it('removes keydown listener when modal closes', () => {
      const { props, rerenderModal } = renderModal();

      // Close the modal via prop change
      rerenderModal({ isOpen: false });

      // Pressing ESC now should NOT invoke onClose because the listener
      // was removed by the effect cleanup.
      fireEvent.keyDown(document, { key: 'Escape' });

      expect(props.onClose).not.toHaveBeenCalled();
    });

    it('prevents default on ESC key event', () => {
      renderModal();

      const event = new KeyboardEvent('keydown', {
        key: 'Escape',
        bubbles: true,
        cancelable: true,
      });

      document.dispatchEvent(event);

      expect(event.defaultPrevented).toBe(true);
    });
  });

  // -------------------------------------------------------------------------
  // Focus Trap
  // -------------------------------------------------------------------------
  describe('Focus Trap', () => {
    it('focuses the modal container when opened', async () => {
      renderModal();

      const dialog = screen.getByRole('dialog');
      const panel = dialog.querySelector('[tabindex="-1"]') as HTMLElement;

      // The focus is set inside a setTimeout(…, 0). Wait for it.
      await waitFor(() => {
        expect(document.activeElement).toBe(panel);
      });
    });

    it('traps focus within modal — Tab from last focusable wraps to first', () => {
      renderModal({
        children: <input data-testid="inner-input" placeholder="Inner" />,
        primaryAction: { label: 'OK', onClick: vi.fn() },
      });

      // Focusable elements in DOM order inside the panel:
      //   1. Close button  (first)
      //   2. Inner input
      //   3. OK button     (last)
      const okButton = screen.getByRole('button', { name: 'OK' });
      act(() => {
        okButton.focus();
      });
      expect(document.activeElement).toBe(okButton);

      // Simulate Tab from the last focusable — should wrap to first
      fireEvent.keyDown(okButton, { key: 'Tab' });

      const closeButton = screen.getByLabelText('Close modal');
      expect(document.activeElement).toBe(closeButton);
    });

    it('traps focus within modal — Shift+Tab from first focusable wraps to last', () => {
      renderModal({
        children: <input data-testid="inner-input" placeholder="Inner" />,
        primaryAction: { label: 'OK', onClick: vi.fn() },
      });

      const closeButton = screen.getByLabelText('Close modal');
      act(() => {
        closeButton.focus();
      });
      expect(document.activeElement).toBe(closeButton);

      // Simulate Shift+Tab from the first focusable — should wrap to last
      fireEvent.keyDown(closeButton, { key: 'Tab', shiftKey: true });

      const okButton = screen.getByRole('button', { name: 'OK' });
      expect(document.activeElement).toBe(okButton);
    });

    it('restores focus to previously focused element on close', () => {
      // Place a trigger button in the DOM and focus it
      const trigger = document.createElement('button');
      trigger.textContent = 'Open Modal';
      document.body.appendChild(trigger);
      trigger.focus();
      expect(document.activeElement).toBe(trigger);

      // Open the modal — previousFocusRef captures the trigger
      const { rerenderModal } = renderModal();
      expect(screen.getByRole('dialog')).toBeInTheDocument();

      // Close the modal — cleanup should restore focus to trigger
      rerenderModal({ isOpen: false });
      expect(document.activeElement).toBe(trigger);

      // Cleanup DOM
      trigger.remove();
    });
  });

  // -------------------------------------------------------------------------
  // Body Scroll Lock
  // -------------------------------------------------------------------------
  describe('Body Scroll Lock', () => {
    it('sets document.body.style.overflow to "hidden" when modal opens', () => {
      expect(document.body.style.overflow).toBe('');

      renderModal();

      expect(document.body.style.overflow).toBe('hidden');
    });

    it('restores document.body.style.overflow to "" when modal closes', () => {
      const { rerenderModal } = renderModal();
      expect(document.body.style.overflow).toBe('hidden');

      rerenderModal({ isOpen: false });

      expect(document.body.style.overflow).toBe('');
    });

    it('restores scroll on unmount', () => {
      const { unmount } = renderModal();
      expect(document.body.style.overflow).toBe('hidden');

      unmount();

      expect(document.body.style.overflow).toBe('');
    });
  });

  // -------------------------------------------------------------------------
  // Action Buttons
  // -------------------------------------------------------------------------
  describe('Action Buttons', () => {
    it('renders primary action button', () => {
      renderModal({
        primaryAction: { label: 'Confirm', onClick: vi.fn() },
      });

      expect(screen.getByRole('button', { name: 'Confirm' })).toBeInTheDocument();
    });

    it('renders secondary action button', () => {
      renderModal({
        secondaryAction: { label: 'Cancel', onClick: vi.fn() },
      });

      expect(screen.getByRole('button', { name: 'Cancel' })).toBeInTheDocument();
    });

    it('renders multiple action buttons from actions array', () => {
      renderModal({
        actions: [
          { label: 'Action A', onClick: vi.fn() },
          { label: 'Action B', onClick: vi.fn() },
          { label: 'Action C', onClick: vi.fn() },
        ],
      });

      expect(screen.getByRole('button', { name: 'Action A' })).toBeInTheDocument();
      expect(screen.getByRole('button', { name: 'Action B' })).toBeInTheDocument();
      expect(screen.getByRole('button', { name: 'Action C' })).toBeInTheDocument();
    });

    it('calls action onClick when button is clicked', () => {
      const handleClick = vi.fn();
      renderModal({
        actions: [{ label: 'Do Something', onClick: handleClick, variant: 'primary' }],
      });

      fireEvent.click(screen.getByRole('button', { name: 'Do Something' }));

      expect(handleClick).toHaveBeenCalledTimes(1);
    });

    it('applies primary variant styles (bg-indigo-600)', () => {
      renderModal({
        primaryAction: { label: 'Submit', onClick: vi.fn() },
      });

      const btn = screen.getByRole('button', { name: 'Submit' });
      expect(btn).toHaveClass('bg-indigo-600');
    });

    it('applies secondary variant styles (bg-white border)', () => {
      renderModal({
        secondaryAction: { label: 'Cancel', onClick: vi.fn() },
      });

      const btn = screen.getByRole('button', { name: 'Cancel' });
      expect(btn).toHaveClass('bg-white');
      expect(btn).toHaveClass('border');
    });

    it('applies danger variant styles (bg-red-600)', () => {
      renderModal({
        actions: [{ label: 'Delete', onClick: vi.fn(), variant: 'danger' }],
      });

      const btn = screen.getByRole('button', { name: 'Delete' });
      expect(btn).toHaveClass('bg-red-600');
    });

    it('disables action button when disabled is true', () => {
      renderModal({
        actions: [
          { label: 'Disabled Btn', onClick: vi.fn(), variant: 'primary', disabled: true },
        ],
      });

      expect(screen.getByRole('button', { name: 'Disabled Btn' })).toBeDisabled();
    });

    it('shows loading spinner in button when loading is true', () => {
      renderModal({
        actions: [
          { label: 'Loading Btn', onClick: vi.fn(), variant: 'primary', loading: true },
        ],
      });

      const btn = screen.getByRole('button', { name: 'Loading Btn' });
      // Button is also disabled while loading
      expect(btn).toBeDisabled();
      // Spinner SVG has the animate-spin class
      const spinner = btn.querySelector('.animate-spin');
      expect(spinner).toBeInTheDocument();
    });

    it('hides footer when hideFooter is true', () => {
      renderModal({
        hideFooter: true,
        primaryAction: { label: 'Hidden Action', onClick: vi.fn() },
      });

      expect(
        screen.queryByRole('button', { name: 'Hidden Action' }),
      ).not.toBeInTheDocument();
    });

    it('hides footer when no actions provided', () => {
      renderModal();

      const dialog = screen.getByRole('dialog');
      // Only the close button should exist — no footer action buttons
      const allButtons = dialog.querySelectorAll('button');
      expect(allButtons.length).toBe(1);
      expect(allButtons[0]).toHaveAttribute('aria-label', 'Close modal');
    });
  });

  // -------------------------------------------------------------------------
  // Portal Rendering
  // -------------------------------------------------------------------------
  describe('Portal Rendering', () => {
    it('renders modal into document.body via createPortal', () => {
      const { container } = renderModal();
      const dialog = screen.getByRole('dialog');

      // The dialog should NOT be inside the render container (portaled out)
      expect(container.contains(dialog)).toBe(false);
      // The dialog SHOULD be a descendant of document.body
      expect(document.body.contains(dialog)).toBe(true);
    });

    it('does not render portal when isOpen is false', () => {
      renderModal({ isOpen: false });

      // No dialog element should be present anywhere in the document
      expect(document.querySelector('[role="dialog"]')).toBeNull();
    });

    it('modal is not rendered inside parent component DOM tree', () => {
      const { container } = renderModal();

      // The render container should have no direct child content from the
      // portal — Modal returns null for its in-tree render and uses a portal.
      expect(container.querySelector('[role="dialog"]')).toBeNull();

      // But the dialog exists in the document
      expect(screen.getByRole('dialog')).toBeInTheDocument();
    });
  });

  // -------------------------------------------------------------------------
  // Accessibility
  // -------------------------------------------------------------------------
  describe('Accessibility', () => {
    it('has role="dialog" attribute', () => {
      renderModal();

      const dialog = screen.getByRole('dialog');
      expect(dialog).toHaveAttribute('role', 'dialog');
    });

    it('has aria-modal="true" attribute', () => {
      renderModal();

      const dialog = screen.getByRole('dialog');
      expect(dialog).toHaveAttribute('aria-modal', 'true');
    });

    it('has aria-labelledby pointing to modal title', () => {
      renderModal({ title: 'Accessible Title' });

      const dialog = screen.getByRole('dialog');
      expect(dialog).toHaveAttribute('aria-labelledby', 'modal-title');

      // Verify the title element has the matching id
      const titleEl = screen.getByText('Accessible Title');
      expect(titleEl).toHaveAttribute('id', 'modal-title');
    });

    it('has aria-describedby pointing to modal description when present', () => {
      renderModal({ description: 'Helpful description' });

      const dialog = screen.getByRole('dialog');
      expect(dialog).toHaveAttribute('aria-describedby', 'modal-description');

      // Verify the description element has the matching id
      const descEl = screen.getByText('Helpful description');
      expect(descEl).toHaveAttribute('id', 'modal-description');
    });

    it('does not have aria-describedby when no description', () => {
      renderModal(); // no description prop

      const dialog = screen.getByRole('dialog');
      expect(dialog).not.toHaveAttribute('aria-describedby');
    });

    it('close button has aria-label="Close modal"', () => {
      renderModal();

      const closeBtn = screen.getByLabelText('Close modal');
      expect(closeBtn).toBeInTheDocument();
      expect(closeBtn).toHaveAttribute('aria-label', 'Close modal');
    });

    it('modal container has tabIndex={-1} for programmatic focus', () => {
      renderModal();

      const dialog = screen.getByRole('dialog');
      const panel = dialog.querySelector('[tabindex="-1"]');
      expect(panel).toBeInTheDocument();
      expect(panel).toHaveAttribute('tabindex', '-1');
    });
  });

  // -------------------------------------------------------------------------
  // Size Variants
  // -------------------------------------------------------------------------
  describe('Size Variants', () => {
    it('applies max-w-sm for size="sm"', () => {
      renderModal({ size: 'sm' });

      const dialog = screen.getByRole('dialog');
      const panel = dialog.querySelector('[tabindex="-1"]') as HTMLElement;
      expect(panel).toHaveClass('max-w-sm');
    });

    it('applies max-w-lg for size="md" (default)', () => {
      renderModal(); // default size is 'md'

      const dialog = screen.getByRole('dialog');
      const panel = dialog.querySelector('[tabindex="-1"]') as HTMLElement;
      expect(panel).toHaveClass('max-w-lg');
    });

    it('applies max-w-2xl for size="lg"', () => {
      renderModal({ size: 'lg' });

      const dialog = screen.getByRole('dialog');
      const panel = dialog.querySelector('[tabindex="-1"]') as HTMLElement;
      expect(panel).toHaveClass('max-w-2xl');
    });

    it('applies max-w-4xl for size="xl"', () => {
      renderModal({ size: 'xl' });

      const dialog = screen.getByRole('dialog');
      const panel = dialog.querySelector('[tabindex="-1"]') as HTMLElement;
      expect(panel).toHaveClass('max-w-4xl');
    });

    it('applies max-w-[90vw] and h-[90vh] for size="full"', () => {
      renderModal({ size: 'full' });

      const dialog = screen.getByRole('dialog');
      const panel = dialog.querySelector('[tabindex="-1"]') as HTMLElement;
      expect(panel).toHaveClass('max-w-[90vw]');
      expect(panel).toHaveClass('h-[90vh]');
    });
  });

  // -------------------------------------------------------------------------
  // Header and Footer Visibility
  // -------------------------------------------------------------------------
  describe('Header and Footer Visibility', () => {
    it('hides header when hideHeader is true', () => {
      renderModal({ hideHeader: true, title: 'Hidden Title' });

      // Title and close button should not be rendered
      expect(screen.queryByText('Hidden Title')).not.toBeInTheDocument();
      expect(screen.queryByLabelText('Close modal')).not.toBeInTheDocument();
      // Modal body content should still be visible
      expect(screen.getByText('Modal content')).toBeInTheDocument();
    });

    it('hides footer when hideFooter is true', () => {
      renderModal({
        hideFooter: true,
        primaryAction: { label: 'Primary', onClick: vi.fn() },
        secondaryAction: { label: 'Secondary', onClick: vi.fn() },
      });

      // Neither action button should be rendered
      expect(
        screen.queryByRole('button', { name: 'Primary' }),
      ).not.toBeInTheDocument();
      expect(
        screen.queryByRole('button', { name: 'Secondary' }),
      ).not.toBeInTheDocument();
      // Body and header should still be visible
      expect(screen.getByText('Modal content')).toBeInTheDocument();
      expect(screen.getByText('Test Modal')).toBeInTheDocument();
    });
  });
});
