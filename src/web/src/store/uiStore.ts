/**
 * UI State Management Store
 *
 * Zustand 4.x state management store for UI presentation state in the
 * Synthetic ERP Data Generation Platform Web Console.
 *
 * Manages:
 * - Sidebar collapsed/expanded state (persisted to localStorage)
 * - Active color theme: light/dark/system (persisted to localStorage)
 * - Toast notification queue with auto-dismiss timers
 * - Notification center with read/unread tracking
 * - Modal dialog state (open/close with content tracking)
 * - Breadcrumb navigation trail
 * - Global loading overlay
 *
 * Consumed by:
 * - Sidebar component for collapse toggle
 * - Header component for theme switching and notifications
 * - All pages for toast notifications and modal dialogs
 * - App.tsx for global UI state initialization
 */

import { create } from 'zustand';
import { persist, createJSONStorage } from 'zustand/middleware';

// ---------------------------------------------------------------------------
// Type Definitions
// ---------------------------------------------------------------------------

/** Supported color theme modes for the application. */
export type ThemeMode = 'light' | 'dark' | 'system';

/** Classification categories for toast notifications. */
export type ToastType = 'success' | 'error' | 'warning' | 'info';

/**
 * Represents a transient toast notification displayed to the user.
 * Toasts auto-dismiss after their configured duration.
 */
export interface Toast {
  /** Unique identifier for the toast instance. */
  id: string;
  /** Visual classification of the toast (success, error, warning, info). */
  type: ToastType;
  /** Primary text content of the toast message. */
  message: string;
  /** Optional heading displayed above the message. */
  title?: string;
  /** Time in milliseconds before the toast is automatically removed. */
  duration: number;
  /** Whether the user can manually dismiss this toast. */
  dismissible: boolean;
  /** Unix timestamp (Date.now()) when the toast was created. */
  createdAt: number;
}

/**
 * Represents a persistent notification in the notification center.
 * Notifications persist until explicitly dismissed or cleared.
 */
export interface Notification {
  /** Unique identifier for the notification. */
  id: string;
  /** Short headline for the notification. */
  title: string;
  /** Detailed notification message body. */
  message: string;
  /** Visual classification of the notification. */
  type: 'info' | 'warning' | 'error' | 'success';
  /** Whether this notification has been viewed by the user. */
  read: boolean;
  /** ISO 8601 timestamp of when the notification was created. */
  createdAt: string;
  /** Optional navigation link associated with this notification. */
  link?: string;
}

/**
 * Tracks the open/close state and content of the global modal dialog.
 */
export interface ModalState {
  /** Whether a modal dialog is currently visible. */
  isOpen: boolean;
  /** Identifier of the currently displayed modal, or null if closed. */
  modalId: string | null;
  /** Arbitrary props passed to the active modal component. */
  modalProps: Record<string, unknown>;
}

/**
 * A single segment in the breadcrumb navigation trail.
 */
export interface Breadcrumb {
  /** Display text for the breadcrumb segment. */
  label: string;
  /** Optional route path — if provided, the breadcrumb is clickable. */
  path?: string;
}

// ---------------------------------------------------------------------------
// UIState Interface — Full Store Shape
// ---------------------------------------------------------------------------

/**
 * Complete state interface for the UI presentation store.
 * Includes both state properties and action methods.
 */
export interface UIState {
  // -- Sidebar State --------------------------------------------------------
  /** Whether the navigation sidebar is in its collapsed (icon-only) state. */
  isSidebarCollapsed: boolean;
  /** Whether the sidebar is rendered as a mobile drawer overlay. */
  isSidebarMobile: boolean;

  // -- Theme State (persisted) ----------------------------------------------
  /** Active color theme mode. */
  theme: ThemeMode;

  // -- Toast Notifications --------------------------------------------------
  /** Ordered queue of active toast notifications (newest last). */
  toasts: Toast[];

  // -- Notification Center --------------------------------------------------
  /** Ordered list of persistent notifications (newest first). */
  notifications: Notification[];
  /** Cached count of unread notifications for badge display. */
  unreadNotificationCount: number;

  // -- Modal State ----------------------------------------------------------
  /** Current modal dialog state. */
  modal: ModalState;

  // -- Breadcrumb Navigation ------------------------------------------------
  /** Current breadcrumb trail segments. */
  breadcrumbs: Breadcrumb[];

  // -- Global Loading Overlay -----------------------------------------------
  /** Whether the full-page loading overlay is visible. */
  isGlobalLoading: boolean;
  /** Optional message displayed on the global loading overlay. */
  globalLoadingMessage: string | null;

  // -- Sidebar Actions ------------------------------------------------------
  /** Toggle the sidebar between collapsed and expanded states. */
  toggleSidebar: () => void;
  /** Explicitly set the sidebar collapsed state. */
  setSidebarCollapsed: (collapsed: boolean) => void;
  /** Set whether the sidebar is in responsive mobile-drawer mode. */
  setSidebarMobile: (isMobile: boolean) => void;

  // -- Theme Actions --------------------------------------------------------
  /** Change the active color theme and apply it to the document root. */
  setTheme: (theme: ThemeMode) => void;

  // -- Toast Actions --------------------------------------------------------
  /** Enqueue a new toast notification with auto-dismiss. */
  addToast: (toast: Omit<Toast, 'id' | 'createdAt'>) => void;
  /** Show a success toast with default 5 000 ms duration. */
  showSuccessToast: (message: string, title?: string) => void;
  /** Show an error toast with default 8 000 ms duration. */
  showErrorToast: (message: string, title?: string) => void;
  /** Show a warning toast with default 6 000 ms duration. */
  showWarningToast: (message: string, title?: string) => void;
  /** Show an informational toast with default 5 000 ms duration. */
  showInfoToast: (message: string, title?: string) => void;
  /** Remove a specific toast by its unique ID. */
  removeToast: (id: string) => void;
  /** Remove all active toasts at once. */
  clearToasts: () => void;

  // -- Notification Actions -------------------------------------------------
  /** Add a new notification to the notification center. */
  addNotification: (notification: Omit<Notification, 'id' | 'read' | 'createdAt'>) => void;
  /** Mark a single notification as read by its ID. */
  markNotificationRead: (id: string) => void;
  /** Mark every notification as read. */
  markAllNotificationsRead: () => void;
  /** Remove a single notification by its ID. */
  removeNotification: (id: string) => void;
  /** Remove all notifications and reset the unread count. */
  clearNotifications: () => void;

  // -- Modal Actions --------------------------------------------------------
  /** Open a modal dialog identified by modalId with optional props. */
  openModal: (modalId: string, props?: Record<string, unknown>) => void;
  /** Close the currently open modal dialog. */
  closeModal: () => void;

  // -- Breadcrumb Actions ---------------------------------------------------
  /** Replace the entire breadcrumb trail. */
  setBreadcrumbs: (breadcrumbs: Breadcrumb[]) => void;
  /** Clear all breadcrumb segments. */
  clearBreadcrumbs: () => void;

  // -- Global Loading Actions -----------------------------------------------
  /** Show or hide the global loading overlay with an optional message. */
  setGlobalLoading: (loading: boolean, message?: string | null) => void;

  // -- Reset ----------------------------------------------------------------
  /** Reset the entire UI store to its initial default state. */
  reset: () => void;
}

// ---------------------------------------------------------------------------
// Initial State — Shared between store creation and reset()
// ---------------------------------------------------------------------------

const INITIAL_STATE = {
  isSidebarCollapsed: false,
  isSidebarMobile: false,
  theme: 'system' as ThemeMode,
  toasts: [] as Toast[],
  notifications: [] as Notification[],
  unreadNotificationCount: 0,
  modal: { isOpen: false, modalId: null, modalProps: {} } as ModalState,
  breadcrumbs: [] as Breadcrumb[],
  isGlobalLoading: false,
  globalLoadingMessage: null as string | null,
};

// ---------------------------------------------------------------------------
// Helper — Generate Unique IDs
// ---------------------------------------------------------------------------

/**
 * Generates a unique string identifier.
 * Prefers the native `crypto.randomUUID()` API when available in the
 * browser, falling back to a combination of base-36 timestamp and
 * random suffix for environments where the Crypto API is unavailable.
 */
function generateId(): string {
  if (typeof crypto !== 'undefined' && typeof crypto.randomUUID === 'function') {
    return crypto.randomUUID();
  }
  return Date.now().toString(36) + '-' + Math.random().toString(36).substring(2, 11);
}

// ---------------------------------------------------------------------------
// Helper — Apply Theme to Document
// ---------------------------------------------------------------------------

/**
 * Applies the resolved theme class (`light` or `dark`) to
 * `document.documentElement` so that TailwindCSS `dark:` variants
 * activate correctly.
 *
 * When `mode` is `'system'`, the function reads the user's OS-level
 * `prefers-color-scheme` media query to determine the effective theme.
 */
function applyThemeToDocument(mode: ThemeMode): void {
  if (typeof document === 'undefined') {
    return; // SSR / test-environment safety guard
  }

  const root = document.documentElement;
  root.classList.remove('light', 'dark');

  if (mode === 'system') {
    const prefersDark =
      typeof window !== 'undefined' &&
      window.matchMedia &&
      window.matchMedia('(prefers-color-scheme: dark)').matches;
    root.classList.add(prefersDark ? 'dark' : 'light');
  } else {
    root.classList.add(mode);
  }
}

// ---------------------------------------------------------------------------
// Store Creation
// ---------------------------------------------------------------------------

/**
 * Primary Zustand store for all UI presentation state.
 *
 * Uses the `persist` middleware with `partialize` to selectively persist
 * only the `theme` and `isSidebarCollapsed` fields to `localStorage`
 * under the key `synthetic-erp-ui-store`. All other state (toasts,
 * notifications, modals, breadcrumbs, loading) is transient and resets
 * on page reload.
 */
export const useUIStore = create<UIState>()(
  persist(
    (set, get) => ({
      // ---------------------------------------------------------------
      // State Properties
      // ---------------------------------------------------------------
      ...INITIAL_STATE,

      // ---------------------------------------------------------------
      // Sidebar Actions
      // ---------------------------------------------------------------

      toggleSidebar: (): void => {
        set((state) => ({ isSidebarCollapsed: !state.isSidebarCollapsed }));
      },

      setSidebarCollapsed: (collapsed: boolean): void => {
        set({ isSidebarCollapsed: collapsed });
      },

      setSidebarMobile: (isMobile: boolean): void => {
        set({ isSidebarMobile: isMobile });
      },

      // ---------------------------------------------------------------
      // Theme Actions
      // ---------------------------------------------------------------

      setTheme: (theme: ThemeMode): void => {
        set({ theme });
        applyThemeToDocument(theme);
      },

      // ---------------------------------------------------------------
      // Toast Actions
      // ---------------------------------------------------------------

      addToast: (toast: Omit<Toast, 'id' | 'createdAt'>): void => {
        const id = generateId();
        const newToast: Toast = {
          ...toast,
          id,
          createdAt: Date.now(),
        };

        set((state) => ({ toasts: [...state.toasts, newToast] }));

        // Schedule automatic removal after the configured duration.
        if (newToast.duration > 0) {
          setTimeout(() => {
            const currentState = get();
            if (currentState.toasts.some((t) => t.id === id)) {
              set((state) => ({
                toasts: state.toasts.filter((t) => t.id !== id),
              }));
            }
          }, newToast.duration);
        }
      },

      showSuccessToast: (message: string, title?: string): void => {
        get().addToast({
          type: 'success',
          message,
          title,
          duration: 5000,
          dismissible: true,
        });
      },

      showErrorToast: (message: string, title?: string): void => {
        get().addToast({
          type: 'error',
          message,
          title,
          duration: 8000,
          dismissible: true,
        });
      },

      showWarningToast: (message: string, title?: string): void => {
        get().addToast({
          type: 'warning',
          message,
          title,
          duration: 6000,
          dismissible: true,
        });
      },

      showInfoToast: (message: string, title?: string): void => {
        get().addToast({
          type: 'info',
          message,
          title,
          duration: 5000,
          dismissible: true,
        });
      },

      removeToast: (id: string): void => {
        set((state) => ({
          toasts: state.toasts.filter((t) => t.id !== id),
        }));
      },

      clearToasts: (): void => {
        set({ toasts: [] });
      },

      // ---------------------------------------------------------------
      // Notification Actions
      // ---------------------------------------------------------------

      addNotification: (
        notification: Omit<Notification, 'id' | 'read' | 'createdAt'>,
      ): void => {
        const newNotification: Notification = {
          ...notification,
          id: generateId(),
          read: false,
          createdAt: new Date().toISOString(),
        };

        set((state) => ({
          notifications: [newNotification, ...state.notifications],
          unreadNotificationCount: state.unreadNotificationCount + 1,
        }));
      },

      markNotificationRead: (id: string): void => {
        set((state) => {
          const target = state.notifications.find((n) => n.id === id);
          if (!target || target.read) {
            // Already read or not found — no state change needed.
            return state;
          }

          return {
            notifications: state.notifications.map((n) =>
              n.id === id ? { ...n, read: true } : n,
            ),
            unreadNotificationCount: Math.max(0, state.unreadNotificationCount - 1),
          };
        });
      },

      markAllNotificationsRead: (): void => {
        set((state) => ({
          notifications: state.notifications.map((n) => ({ ...n, read: true })),
          unreadNotificationCount: 0,
        }));
      },

      removeNotification: (id: string): void => {
        set((state) => {
          const filtered = state.notifications.filter((n) => n.id !== id);
          const unreadCount = filtered.filter((n) => !n.read).length;

          return {
            notifications: filtered,
            unreadNotificationCount: unreadCount,
          };
        });
      },

      clearNotifications: (): void => {
        set({
          notifications: [],
          unreadNotificationCount: 0,
        });
      },

      // ---------------------------------------------------------------
      // Modal Actions
      // ---------------------------------------------------------------

      openModal: (modalId: string, props?: Record<string, unknown>): void => {
        set({
          modal: {
            isOpen: true,
            modalId,
            modalProps: props ?? {},
          },
        });
      },

      closeModal: (): void => {
        set({
          modal: {
            isOpen: false,
            modalId: null,
            modalProps: {},
          },
        });
      },

      // ---------------------------------------------------------------
      // Breadcrumb Actions
      // ---------------------------------------------------------------

      setBreadcrumbs: (breadcrumbs: Breadcrumb[]): void => {
        set({ breadcrumbs });
      },

      clearBreadcrumbs: (): void => {
        set({ breadcrumbs: [] });
      },

      // ---------------------------------------------------------------
      // Global Loading Actions
      // ---------------------------------------------------------------

      setGlobalLoading: (loading: boolean, message?: string | null): void => {
        set({
          isGlobalLoading: loading,
          globalLoadingMessage: message ?? null,
        });
      },

      // ---------------------------------------------------------------
      // Reset
      // ---------------------------------------------------------------

      /**
       * Resets the entire store to its initial default state.
       * The persisted `theme` and `isSidebarCollapsed` values are
       * preserved across resets because the persist middleware will
       * re-hydrate them from localStorage on the next read.
       */
      reset: (): void => {
        set({ ...INITIAL_STATE });
      },
    }),

    // -----------------------------------------------------------------
    // Persist Middleware Configuration
    // -----------------------------------------------------------------
    {
      name: 'synthetic-erp-ui-store',
      storage: createJSONStorage(() => localStorage),
      partialize: (state: UIState) => ({
        theme: state.theme,
        isSidebarCollapsed: state.isSidebarCollapsed,
      }),
      /**
       * After rehydration from localStorage, apply the persisted theme
       * to the document root so that TailwindCSS dark-mode classes
       * activate immediately.
       */
      onRehydrateStorage: () => {
        return (state: UIState | undefined): void => {
          if (state) {
            applyThemeToDocument(state.theme);
          }
        };
      },
    },
  ),
);

// ---------------------------------------------------------------------------
// Default Export
// ---------------------------------------------------------------------------

export default useUIStore;
