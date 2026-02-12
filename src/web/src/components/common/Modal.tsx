/**
 * Modal Component
 *
 * Reusable modal dialog component for the Synthetic ERP Data Generation Platform
 * Web Console. Provides overlay backdrop, configurable title/size/actions,
 * ESC-to-close keyboard handler, and focus trap for accessibility.
 *
 * Renders via React portal into document.body to ensure correct z-index stacking
 * regardless of where the component is mounted in the React tree. Integrates with
 * the global uiStore for centralized modal state management across the application.
 *
 * Features:
 * - Portal rendering into document.body for proper z-index stacking
 * - Keyboard accessibility: ESC to close, Tab/Shift+Tab focus trapping
 * - Body scroll lock while modal is open
 * - Focus restoration to trigger element on close
 * - Five size variants: sm, md, lg, xl, full
 * - Action button support with primary/secondary/danger variants
 * - Loading spinner states for async action buttons
 * - Dark mode support via TailwindCSS dark: prefixes
 * - ARIA attributes (role, aria-modal, aria-labelledby, aria-describedby)
 * - Global modal state synchronization via useUIStore
 *
 * @module components/common/Modal
 */

import React, { useEffect, useRef, useCallback, useMemo } from 'react';
import { createPortal } from 'react-dom';
import { useUIStore } from '@/store/uiStore';

// ---------------------------------------------------------------------------
// Type Definitions
// ---------------------------------------------------------------------------

/**
 * Available modal size variants controlling the maximum width of the dialog.
 * - 'sm': Small dialogs (confirmations, simple prompts)
 * - 'md': Medium dialogs (forms, standard content) — default
 * - 'lg': Large dialogs (complex forms, data tables)
 * - 'xl': Extra-large dialogs (detailed views, multi-column layouts)
 * - 'full': Near full-screen dialogs (editors, dashboards)
 */
export type ModalSize = 'sm' | 'md' | 'lg' | 'xl' | 'full';

/**
 * Configuration for a single action button rendered in the modal footer.
 * Supports primary, secondary, and danger visual variants with optional
 * loading and disabled states for async operations.
 */
export interface ModalAction {
  /** Display text label for the button. */
  label: string;
  /** Click handler invoked when the button is pressed. */
  onClick: () => void;
  /** Visual variant controlling button styling. Defaults to 'secondary'. */
  variant?: 'primary' | 'secondary' | 'danger';
  /** Whether the button is disabled and non-interactive. */
  disabled?: boolean;
  /** Whether the button displays a loading spinner indicating an async operation. */
  loading?: boolean;
}

/**
 * Props interface for the Modal component.
 * Supports content, configuration, action buttons, and styling customization.
 *
 * @example
 * ```tsx
 * <Modal
 *   isOpen={isOpen}
 *   onClose={handleClose}
 *   title="Confirm Deletion"
 *   description="This action cannot be undone."
 *   size="md"
 *   primaryAction={{ label: 'Delete', onClick: handleDelete, variant: 'danger' }}
 *   secondaryAction={{ label: 'Cancel', onClick: handleClose }}
 * >
 *   <p>Are you sure you want to delete this generation profile?</p>
 * </Modal>
 * ```
 */
export interface ModalProps {
  // --- Content ---

  /** Controls visibility of the modal dialog. When false, the modal is not rendered. */
  isOpen: boolean;
  /** Callback invoked when the modal requests to be closed (ESC, overlay click, close button). */
  onClose: () => void;
  /** Optional title text displayed in the modal header. */
  title?: string;
  /** Optional description/subtitle text displayed below the title in the header. */
  description?: string;
  /** Content rendered inside the scrollable modal body area. */
  children: React.ReactNode;

  // --- Configuration ---

  /** Size variant controlling the modal maximum width. Defaults to 'md'. */
  size?: ModalSize;
  /** Whether clicking the backdrop overlay closes the modal. Defaults to true. */
  closeOnOverlay?: boolean;
  /** Whether pressing the ESC key closes the modal. Defaults to true. */
  closeOnEsc?: boolean;
  /** Whether to display the X close button in the modal header. Defaults to true. */
  showCloseButton?: boolean;

  // --- Actions (footer buttons) ---

  /** Array of action buttons rendered in the modal footer between secondary and primary. */
  actions?: ModalAction[];
  /** Convenience prop for a single primary action button (rendered rightmost in footer). */
  primaryAction?: ModalAction;
  /** Convenience prop for a cancel/secondary action button (rendered leftmost in footer). */
  secondaryAction?: ModalAction;

  // --- Styling ---

  /** Additional CSS classes applied to the modal content panel. */
  className?: string;
  /** Additional CSS classes applied to the backdrop overlay container. */
  overlayClassName?: string;
  /** When true, hides the modal header bar entirely (title + close button). Defaults to false. */
  hideHeader?: boolean;
  /** When true, hides the modal footer/action bar entirely. Defaults to false. */
  hideFooter?: boolean;
}

// ---------------------------------------------------------------------------
// Constants
// ---------------------------------------------------------------------------

/**
 * TailwindCSS max-width classes mapped to each modal size variant.
 * The 'full' variant uses viewport-relative units for near full-screen display.
 */
const MODAL_SIZE_CLASSES: Record<ModalSize, string> = {
  sm: 'max-w-sm',
  md: 'max-w-lg',
  lg: 'max-w-2xl',
  xl: 'max-w-4xl',
  full: 'max-w-[90vw] h-[90vh]',
};

/**
 * CSS selector matching all focusable elements within the modal for focus trapping.
 * Excludes disabled elements and elements with tabindex="-1".
 */
const FOCUSABLE_SELECTOR =
  'button:not([disabled]), [href], input:not([disabled]), select:not([disabled]), textarea:not([disabled]), [tabindex]:not([tabindex="-1"])';

// ---------------------------------------------------------------------------
// Inline SVG Helper Components
// ---------------------------------------------------------------------------

/**
 * Close (X) icon SVG component rendered in the modal header close button.
 * Uses currentColor for stroke so it inherits the parent's text color.
 */
const CloseIcon: React.FC = () => (
  <svg
    className="w-5 h-5"
    xmlns="http://www.w3.org/2000/svg"
    fill="none"
    viewBox="0 0 24 24"
    stroke="currentColor"
    strokeWidth={2}
    aria-hidden="true"
  >
    <path strokeLinecap="round" strokeLinejoin="round" d="M6 18L18 6M6 6l12 12" />
  </svg>
);

/**
 * Animated loading spinner SVG component displayed inside action buttons
 * when their `loading` prop is true. Provides visual feedback for async operations.
 */
const LoadingSpinner: React.FC = () => (
  <svg
    className="animate-spin -ml-1 mr-2 h-4 w-4"
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
      d="M4 12a8 8 0 018-8V0C5.373 0 0 5.373 0 12h4z"
    />
  </svg>
);

// ---------------------------------------------------------------------------
// Helper Functions
// ---------------------------------------------------------------------------

/**
 * Returns TailwindCSS classes for an action button based on its visual variant.
 * Supports light and dark mode styling for all three variants.
 *
 * @param variant - The button variant: 'primary', 'secondary', or 'danger'.
 * @returns Space-separated TailwindCSS class string for the button.
 */
function getActionClasses(variant: string = 'secondary'): string {
  switch (variant) {
    case 'primary':
      return 'bg-indigo-600 text-white hover:bg-indigo-700 focus:ring-indigo-500 disabled:bg-indigo-400';
    case 'danger':
      return 'bg-red-600 text-white hover:bg-red-700 focus:ring-red-500 disabled:bg-red-400';
    case 'secondary':
    default:
      return 'bg-white dark:bg-gray-700 text-gray-700 dark:text-gray-200 border border-gray-300 dark:border-gray-600 hover:bg-gray-50 dark:hover:bg-gray-600 focus:ring-gray-500';
  }
}

// ---------------------------------------------------------------------------
// Modal Component
// ---------------------------------------------------------------------------

/**
 * Reusable modal dialog component with comprehensive accessibility support.
 *
 * Renders as a portal into document.body to ensure correct z-index stacking
 * regardless of where the component is mounted in the React component tree.
 * Provides focus trapping, keyboard navigation (ESC and Tab cycling), body
 * scroll lock, and integration with the global UI store for centralized
 * modal state management.
 *
 * The component supports both local prop-driven usage and global state
 * integration via useUIStore. When a modal closes, the global store state
 * is automatically synchronized to ensure consistency across the application.
 *
 * @param props - ModalProps configuration object.
 * @returns Rendered modal portal or null when closed.
 */
const Modal: React.FC<ModalProps> = ({
  isOpen,
  onClose,
  title,
  description,
  children,
  size = 'md',
  closeOnOverlay = true,
  closeOnEsc = true,
  showCloseButton = true,
  actions,
  primaryAction,
  secondaryAction,
  className,
  overlayClassName,
  hideHeader = false,
  hideFooter = false,
}) => {
  // -------------------------------------------------------------------------
  // Refs
  // -------------------------------------------------------------------------

  /** Reference to the modal panel container for focus management. */
  const modalRef = useRef<HTMLDivElement>(null);

  /** Stores the element that had focus before the modal opened, for restoration on close. */
  const previousFocusRef = useRef<HTMLElement | null>(null);

  // -------------------------------------------------------------------------
  // Global UI Store Integration
  // -------------------------------------------------------------------------

  /**
   * Access global modal state and actions from the centralized UI store.
   * Enables synchronization between locally-controlled modals (via props)
   * and the application-wide modal management system used across the
   * Web Console for showing/hiding modals globally.
   *
   * - modal: Current global modal state (isOpen, modalId, modalProps)
   * - openModal: Action to register a modal in the global store
   * - closeModal: Action to clear the globally tracked modal state
   *
   * Using individual selectors for optimal re-render performance —
   * each selector returns a stable reference and only triggers re-renders
   * when its specific slice of state changes.
   */
  const globalModalState = useUIStore((state) => state.modal);
  const storeOpenModal = useUIStore((state) => state.openModal);
  const storeCloseModal = useUIStore((state) => state.closeModal);

  // -------------------------------------------------------------------------
  // Close Handler with Global Store Synchronization
  // -------------------------------------------------------------------------

  /**
   * Unified close handler that:
   * 1. Invokes the parent's onClose callback (local state management)
   * 2. Synchronizes with the global UI store if a modal is tracked there
   *
   * This ensures consistency when modals are managed both via direct props
   * (local component state) and via the centralized uiStore (global state).
   * The closeModal call is idempotent, so double-calling is safe.
   */
  const handleCloseModal = useCallback((): void => {
    onClose();
    // Synchronize global store state — if a modal is tracked globally, clear it
    if (globalModalState.isOpen) {
      storeCloseModal();
    }
  }, [onClose, globalModalState.isOpen, storeCloseModal]);

  // -------------------------------------------------------------------------
  // ESC Key Handler
  // -------------------------------------------------------------------------

  /**
   * Registers a document-level keydown listener that closes the modal
   * when the Escape key is pressed. Only active when the modal is open
   * and closeOnEsc is not disabled.
   */
  useEffect(() => {
    if (!isOpen || !closeOnEsc) return undefined;

    const handleKeyDown = (e: KeyboardEvent): void => {
      if (e.key === 'Escape') {
        e.preventDefault();
        handleCloseModal();
      }
    };

    document.addEventListener('keydown', handleKeyDown);
    return () => {
      document.removeEventListener('keydown', handleKeyDown);
    };
  }, [isOpen, handleCloseModal, closeOnEsc]);

  // -------------------------------------------------------------------------
  // Focus Management & Body Scroll Lock
  // -------------------------------------------------------------------------

  /**
   * Manages focus lifecycle when the modal opens and closes:
   * - Saves the currently focused element for later restoration
   * - Moves focus into the modal container
   * - Locks body scrolling to prevent background scroll-through
   * - On cleanup: restores scroll and returns focus to the trigger element
   */
  useEffect(() => {
    if (!isOpen) return undefined;

    // Preserve the currently focused element for restoration on close
    previousFocusRef.current = document.activeElement as HTMLElement;

    // Focus the modal container after the portal render completes
    const focusTimer = setTimeout(() => {
      modalRef.current?.focus();
    }, 0);

    // Lock body scrolling while the modal is visible
    const previousOverflow = document.body.style.overflow;
    document.body.style.overflow = 'hidden';

    return () => {
      clearTimeout(focusTimer);
      // Restore body scroll behavior
      document.body.style.overflow = previousOverflow;
      // Restore focus to the element that triggered the modal opening
      previousFocusRef.current?.focus();
    };
  }, [isOpen]);

  // -------------------------------------------------------------------------
  // Global Store Sync on Open
  // -------------------------------------------------------------------------

  /**
   * Synchronizes the modal open state with the global UI store.
   * When this modal opens via props and the global store does not already
   * track an active modal, registers it in the store for cross-component
   * visibility. This enables other components (e.g., navigation guards)
   * to detect that a modal dialog is currently active in the application.
   */
  useEffect(() => {
    if (isOpen && !globalModalState.isOpen) {
      storeOpenModal(title ?? 'modal-dialog', { size, title: title ?? '' });
    }
  }, [isOpen, globalModalState.isOpen, storeOpenModal, title, size]);

  // -------------------------------------------------------------------------
  // Focus Trap — Tab/Shift+Tab Cycling
  // -------------------------------------------------------------------------

  /**
   * Handles Tab and Shift+Tab key presses to trap focus within the modal.
   * When the user tabs past the last focusable element, focus cycles back
   * to the first. Shift+Tab from the first element cycles to the last.
   * This ensures keyboard-only users cannot tab out of the modal while
   * it is open, meeting WCAG 2.1 dialog accessibility requirements.
   */
  const handleTabKey = useCallback((e: React.KeyboardEvent): void => {
    if (e.key !== 'Tab' || !modalRef.current) return;

    const focusableElements = modalRef.current.querySelectorAll(FOCUSABLE_SELECTOR);

    // If there are no focusable elements, prevent tab from escaping the modal
    if (focusableElements.length === 0) {
      e.preventDefault();
      return;
    }

    const firstFocusable = focusableElements[0] as HTMLElement;
    const lastFocusable = focusableElements[focusableElements.length - 1] as HTMLElement;

    if (e.shiftKey) {
      // Shift+Tab: cycling backwards — wrap from first to last
      if (document.activeElement === firstFocusable) {
        e.preventDefault();
        lastFocusable.focus();
      }
    } else {
      // Tab: cycling forwards — wrap from last to first
      if (document.activeElement === lastFocusable) {
        e.preventDefault();
        firstFocusable.focus();
      }
    }
  }, []);

  // -------------------------------------------------------------------------
  // Overlay Click Handler
  // -------------------------------------------------------------------------

  /**
   * Closes the modal when the user clicks on the backdrop overlay.
   * Only triggers when the click target is the overlay container itself
   * (not the modal panel or its children), using event.target identity check.
   */
  const handleOverlayClick = useCallback(
    (e: React.MouseEvent): void => {
      if (closeOnOverlay && e.target === e.currentTarget) {
        handleCloseModal();
      }
    },
    [closeOnOverlay, handleCloseModal],
  );

  // -------------------------------------------------------------------------
  // Collected Action Buttons
  // -------------------------------------------------------------------------

  /**
   * Merges secondaryAction, custom actions array, and primaryAction into
   * a single ordered array for rendering in the footer. The ordering is:
   *   1. Secondary action (leftmost — typically "Cancel")
   *   2. Custom actions (middle)
   *   3. Primary action (rightmost — typically "Confirm"/"Submit")
   *
   * Default variants are applied when not explicitly set:
   * - secondaryAction defaults to 'secondary' variant
   * - primaryAction defaults to 'primary' variant
   */
  const allActions = useMemo((): ModalAction[] => {
    const result: ModalAction[] = [];

    if (secondaryAction) {
      result.push({
        ...secondaryAction,
        variant: secondaryAction.variant ?? 'secondary',
      });
    }

    if (actions) {
      result.push(...actions);
    }

    if (primaryAction) {
      result.push({
        ...primaryAction,
        variant: primaryAction.variant ?? 'primary',
      });
    }

    return result;
  }, [actions, primaryAction, secondaryAction]);

  // -------------------------------------------------------------------------
  // Render Guard — Return null when closed
  // -------------------------------------------------------------------------

  if (!isOpen) return null;

  // -------------------------------------------------------------------------
  // Portal Render into document.body
  // -------------------------------------------------------------------------

  return createPortal(
    <div
      className={`fixed inset-0 z-[100] flex items-center justify-center p-4 ${overlayClassName ?? ''}`}
      onClick={handleOverlayClick}
      role="dialog"
      aria-modal="true"
      aria-labelledby={title ? 'modal-title' : undefined}
      aria-describedby={description ? 'modal-description' : undefined}
    >
      {/* Backdrop overlay with semi-transparent background and blur effect */}
      <div
        className="absolute inset-0 bg-black/50 backdrop-blur-sm transition-opacity"
        aria-hidden="true"
      />

      {/* Modal panel container */}
      <div
        ref={modalRef}
        tabIndex={-1}
        onKeyDown={handleTabKey}
        className={[
          'relative w-full',
          MODAL_SIZE_CLASSES[size],
          'bg-white dark:bg-gray-800 rounded-xl shadow-2xl',
          'flex flex-col max-h-[90vh]',
          size === 'full' ? 'h-[90vh]' : '',
          className ?? '',
        ]
          .filter(Boolean)
          .join(' ')}
      >
        {/* ----- Header ----- */}
        {!hideHeader && (title || showCloseButton) && (
          <div className="flex items-center justify-between px-6 py-4 border-b border-gray-200 dark:border-gray-700 flex-shrink-0">
            <div>
              {title && (
                <h2
                  id="modal-title"
                  className="text-lg font-semibold text-gray-900 dark:text-white"
                >
                  {title}
                </h2>
              )}
              {description && (
                <p
                  id="modal-description"
                  className="mt-1 text-sm text-gray-500 dark:text-gray-400"
                >
                  {description}
                </p>
              )}
            </div>
            {showCloseButton && (
              <button
                type="button"
                onClick={handleCloseModal}
                className="p-1.5 rounded-lg text-gray-400 hover:text-gray-600 hover:bg-gray-100 dark:hover:text-gray-300 dark:hover:bg-gray-700 transition-colors"
                aria-label="Close modal"
              >
                <CloseIcon />
              </button>
            )}
          </div>
        )}

        {/* ----- Body (scrollable content area) ----- */}
        <div className="flex-1 overflow-y-auto px-6 py-4">{children}</div>

        {/* ----- Footer (action buttons) ----- */}
        {!hideFooter && allActions.length > 0 && (
          <div className="flex items-center justify-end gap-3 px-6 py-4 border-t border-gray-200 dark:border-gray-700 flex-shrink-0">
            {allActions.map((action, idx) => (
              <button
                key={`modal-action-${idx}-${action.label}`}
                type="button"
                onClick={action.onClick}
                disabled={action.disabled || action.loading}
                className={[
                  'inline-flex items-center justify-center px-4 py-2 text-sm font-medium rounded-lg',
                  'transition-colors focus:outline-none focus:ring-2 focus:ring-offset-2',
                  'disabled:cursor-not-allowed',
                  getActionClasses(action.variant),
                ].join(' ')}
              >
                {action.loading && <LoadingSpinner />}
                {action.label}
              </button>
            ))}
          </div>
        )}
      </div>
    </div>,
    document.body,
  );
};

// ---------------------------------------------------------------------------
// Exports
// ---------------------------------------------------------------------------

export { Modal };
export default Modal;
