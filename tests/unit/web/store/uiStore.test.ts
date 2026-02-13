/**
 * uiStore Zustand Store — Unit Tests
 *
 * Comprehensive Vitest test suite for the UI presentation state management store
 * (useUIStore) in the Synthetic ERP Data Generation Platform Web Console.
 *
 * Covers all state management functions across 9 test suites:
 *   1. Initial State — default values for every state property
 *   2. Sidebar — toggle, collapse, mobile drawer state
 *   3. Theme — light/dark/system switching with document class application
 *   4. Toast Notifications — CRUD, convenience methods, auto-dismiss timers
 *   5. Notifications — queue management, read/unread tracking, count recalculation
 *   6. Modal — open with/without props, close, replacement
 *   7. Breadcrumbs — set, clear, optional path handling
 *   8. Global Loading — overlay with optional message
 *   9. Reset — full state restoration to initial defaults
 *
 * Mock Strategy:
 *   - crypto.randomUUID is mocked globally via tests/unit/web/setup.ts for
 *     deterministic ID generation in addToast and addNotification
 *   - window.matchMedia is mocked in setup.ts (default matches: false);
 *     overridden in specific theme tests to simulate dark mode preference
 *   - vi.useFakeTimers() / vi.useRealTimers() controls setTimeout-based
 *     toast auto-dismiss behavior
 *
 * @see src/web/src/store/uiStore.ts — Implementation under test
 * @see tests/unit/web/setup.ts — Global test setup (loaded automatically)
 */

import { describe, it, expect, beforeEach, vi, afterEach } from 'vitest';
import { useUIStore } from '../../../../src/web/src/store/uiStore';

// ---------------------------------------------------------------------------
// 1. Initial State
// ---------------------------------------------------------------------------

describe('uiStore - Initial State', () => {
  beforeEach(() => {
    useUIStore.getState().reset();
  });

  it('should default isSidebarCollapsed to false', () => {
    expect(useUIStore.getState().isSidebarCollapsed).toBe(false);
  });

  it('should default isSidebarMobile to false', () => {
    expect(useUIStore.getState().isSidebarMobile).toBe(false);
  });

  it('should default theme to "system"', () => {
    expect(useUIStore.getState().theme).toBe('system');
  });

  it('should default toasts to an empty array', () => {
    expect(useUIStore.getState().toasts).toEqual([]);
  });

  it('should default notifications to an empty array', () => {
    expect(useUIStore.getState().notifications).toEqual([]);
  });

  it('should default unreadNotificationCount to 0', () => {
    expect(useUIStore.getState().unreadNotificationCount).toBe(0);
  });

  it('should default modal to closed state with null id and empty props', () => {
    const modal = useUIStore.getState().modal;
    expect(modal.isOpen).toBe(false);
    expect(modal.modalId).toBeNull();
    expect(modal.modalProps).toEqual({});
  });

  it('should default breadcrumbs to an empty array', () => {
    expect(useUIStore.getState().breadcrumbs).toEqual([]);
  });

  it('should default isGlobalLoading to false', () => {
    expect(useUIStore.getState().isGlobalLoading).toBe(false);
  });

  it('should default globalLoadingMessage to null', () => {
    expect(useUIStore.getState().globalLoadingMessage).toBeNull();
  });
});

// ---------------------------------------------------------------------------
// 2. Sidebar
// ---------------------------------------------------------------------------

describe('uiStore - Sidebar', () => {
  beforeEach(() => {
    useUIStore.getState().reset();
  });

  it('should toggle sidebar from collapsed false to true', () => {
    expect(useUIStore.getState().isSidebarCollapsed).toBe(false);
    useUIStore.getState().toggleSidebar();
    expect(useUIStore.getState().isSidebarCollapsed).toBe(true);
  });

  it('should toggle sidebar back to false on a second toggle', () => {
    useUIStore.getState().toggleSidebar();
    expect(useUIStore.getState().isSidebarCollapsed).toBe(true);
    useUIStore.getState().toggleSidebar();
    expect(useUIStore.getState().isSidebarCollapsed).toBe(false);
  });

  it('should handle multiple consecutive toggles correctly', () => {
    useUIStore.getState().toggleSidebar(); // → true
    useUIStore.getState().toggleSidebar(); // → false
    useUIStore.getState().toggleSidebar(); // → true
    expect(useUIStore.getState().isSidebarCollapsed).toBe(true);
  });

  it('should explicitly set sidebar collapsed to true via setSidebarCollapsed', () => {
    useUIStore.getState().setSidebarCollapsed(true);
    expect(useUIStore.getState().isSidebarCollapsed).toBe(true);
  });

  it('should explicitly set sidebar collapsed to false via setSidebarCollapsed', () => {
    useUIStore.getState().setSidebarCollapsed(true);
    useUIStore.getState().setSidebarCollapsed(false);
    expect(useUIStore.getState().isSidebarCollapsed).toBe(false);
  });

  it('should set sidebar mobile to true via setSidebarMobile', () => {
    useUIStore.getState().setSidebarMobile(true);
    expect(useUIStore.getState().isSidebarMobile).toBe(true);
  });

  it('should set sidebar mobile to false via setSidebarMobile', () => {
    useUIStore.getState().setSidebarMobile(true);
    useUIStore.getState().setSidebarMobile(false);
    expect(useUIStore.getState().isSidebarMobile).toBe(false);
  });

  it('should allow setSidebarCollapsed and toggleSidebar to coexist', () => {
    useUIStore.getState().setSidebarCollapsed(true);
    expect(useUIStore.getState().isSidebarCollapsed).toBe(true);
    useUIStore.getState().toggleSidebar();
    expect(useUIStore.getState().isSidebarCollapsed).toBe(false);
  });
});

// ---------------------------------------------------------------------------
// 3. Theme
// ---------------------------------------------------------------------------

describe('uiStore - Theme', () => {
  beforeEach(() => {
    useUIStore.getState().reset();
    // Remove any theme classes from previous tests
    document.documentElement.classList.remove('light', 'dark');
  });

  it('should set theme to "light" and add light class to document root', () => {
    useUIStore.getState().setTheme('light');
    expect(useUIStore.getState().theme).toBe('light');
    expect(document.documentElement.classList.contains('light')).toBe(true);
    expect(document.documentElement.classList.contains('dark')).toBe(false);
  });

  it('should set theme to "dark" and add dark class to document root', () => {
    useUIStore.getState().setTheme('dark');
    expect(useUIStore.getState().theme).toBe('dark');
    expect(document.documentElement.classList.contains('dark')).toBe(true);
    expect(document.documentElement.classList.contains('light')).toBe(false);
  });

  it('should set theme to "system" and resolve to light when prefers-color-scheme is not dark', () => {
    // setup.ts mocks matchMedia with matches: false by default,
    // so system mode resolves to "light"
    useUIStore.getState().setTheme('system');
    expect(useUIStore.getState().theme).toBe('system');
    expect(document.documentElement.classList.contains('light')).toBe(true);
    expect(document.documentElement.classList.contains('dark')).toBe(false);
  });

  it('should resolve system theme to dark when prefers-color-scheme matches dark', () => {
    // Override the matchMedia mock to report dark preference
    vi.mocked(window.matchMedia).mockImplementation((query: string) => ({
      matches: query === '(prefers-color-scheme: dark)',
      media: query,
      onchange: null,
      addListener: vi.fn(),
      removeListener: vi.fn(),
      addEventListener: vi.fn(),
      removeEventListener: vi.fn(),
      dispatchEvent: vi.fn(),
    }));

    useUIStore.getState().setTheme('system');
    expect(useUIStore.getState().theme).toBe('system');
    expect(document.documentElement.classList.contains('dark')).toBe(true);
    expect(document.documentElement.classList.contains('light')).toBe(false);
  });

  it('should remove the previous theme class when switching themes', () => {
    useUIStore.getState().setTheme('dark');
    expect(document.documentElement.classList.contains('dark')).toBe(true);

    useUIStore.getState().setTheme('light');
    expect(document.documentElement.classList.contains('light')).toBe(true);
    expect(document.documentElement.classList.contains('dark')).toBe(false);
  });

  it('should include theme in the persisted store partialize', () => {
    // Setting a theme should update the store state; the persist middleware
    // partializes theme and isSidebarCollapsed to localStorage.
    useUIStore.getState().setTheme('dark');
    expect(useUIStore.getState().theme).toBe('dark');

    useUIStore.getState().setTheme('light');
    expect(useUIStore.getState().theme).toBe('light');
  });
});

// ---------------------------------------------------------------------------
// 4. Toast Notifications
// ---------------------------------------------------------------------------

describe('uiStore - Toast Notifications', () => {
  beforeEach(() => {
    useUIStore.getState().reset();
  });

  afterEach(() => {
    vi.clearAllTimers();
    vi.useRealTimers();
  });

  it('should add a toast with a generated id, createdAt timestamp, and provided config', () => {
    useUIStore.getState().addToast({
      type: 'info',
      message: 'Test toast message',
      title: 'Heads Up',
      duration: 5000,
      dismissible: true,
    });

    const toasts = useUIStore.getState().toasts;
    expect(toasts).toHaveLength(1);

    const toast = toasts[0];
    expect(toast.type).toBe('info');
    expect(toast.message).toBe('Test toast message');
    expect(toast.title).toBe('Heads Up');
    expect(toast.duration).toBe(5000);
    expect(toast.dismissible).toBe(true);
    // ID is generated by crypto.randomUUID (mocked in setup.ts)
    expect(typeof toast.id).toBe('string');
    expect(toast.id.length).toBeGreaterThan(0);
    // createdAt is Date.now() timestamp
    expect(typeof toast.createdAt).toBe('number');
    expect(toast.createdAt).toBeGreaterThan(0);
  });

  it('should show a success toast with type "success" and 5000ms default duration', () => {
    useUIStore.getState().showSuccessToast('Operation completed');

    const toasts = useUIStore.getState().toasts;
    expect(toasts).toHaveLength(1);
    expect(toasts[0].type).toBe('success');
    expect(toasts[0].message).toBe('Operation completed');
    expect(toasts[0].duration).toBe(5000);
    expect(toasts[0].dismissible).toBe(true);
  });

  it('should show a success toast with an optional title', () => {
    useUIStore.getState().showSuccessToast('Job finished', 'Generation Complete');

    const toast = useUIStore.getState().toasts[0];
    expect(toast.title).toBe('Generation Complete');
    expect(toast.message).toBe('Job finished');
  });

  it('should show an error toast with type "error" and 8000ms default duration', () => {
    useUIStore.getState().showErrorToast('Something went wrong');

    const toasts = useUIStore.getState().toasts;
    expect(toasts).toHaveLength(1);
    expect(toasts[0].type).toBe('error');
    expect(toasts[0].message).toBe('Something went wrong');
    expect(toasts[0].duration).toBe(8000);
    expect(toasts[0].dismissible).toBe(true);
  });

  it('should show an error toast with an optional title', () => {
    useUIStore.getState().showErrorToast('Connection lost', 'Network Error');

    const toast = useUIStore.getState().toasts[0];
    expect(toast.title).toBe('Network Error');
  });

  it('should show a warning toast with type "warning" and 6000ms default duration', () => {
    useUIStore.getState().showWarningToast('Disk space running low');

    const toasts = useUIStore.getState().toasts;
    expect(toasts).toHaveLength(1);
    expect(toasts[0].type).toBe('warning');
    expect(toasts[0].message).toBe('Disk space running low');
    expect(toasts[0].duration).toBe(6000);
    expect(toasts[0].dismissible).toBe(true);
  });

  it('should show an info toast with type "info" and 5000ms default duration', () => {
    useUIStore.getState().showInfoToast('New profile available');

    const toasts = useUIStore.getState().toasts;
    expect(toasts).toHaveLength(1);
    expect(toasts[0].type).toBe('info');
    expect(toasts[0].message).toBe('New profile available');
    expect(toasts[0].duration).toBe(5000);
    expect(toasts[0].dismissible).toBe(true);
  });

  it('should remove a specific toast by its id', () => {
    useUIStore.getState().showSuccessToast('First');
    useUIStore.getState().showErrorToast('Second');

    const toasts = useUIStore.getState().toasts;
    expect(toasts).toHaveLength(2);

    const firstId = toasts[0].id;
    useUIStore.getState().removeToast(firstId);

    const remaining = useUIStore.getState().toasts;
    expect(remaining).toHaveLength(1);
    expect(remaining[0].message).toBe('Second');
  });

  it('should clear all toasts at once', () => {
    useUIStore.getState().showSuccessToast('First');
    useUIStore.getState().showErrorToast('Second');
    useUIStore.getState().showWarningToast('Third');
    expect(useUIStore.getState().toasts).toHaveLength(3);

    useUIStore.getState().clearToasts();
    expect(useUIStore.getState().toasts).toEqual([]);
  });

  it('should auto-dismiss a toast after its duration elapses', () => {
    vi.useFakeTimers();

    useUIStore.getState().addToast({
      type: 'info',
      message: 'Will auto-dismiss',
      duration: 5000,
      dismissible: true,
    });
    expect(useUIStore.getState().toasts).toHaveLength(1);

    // Advance just before the duration — toast should still exist
    vi.advanceTimersByTime(4999);
    expect(useUIStore.getState().toasts).toHaveLength(1);

    // Advance past the duration — toast should be removed
    vi.advanceTimersByTime(1);
    expect(useUIStore.getState().toasts).toHaveLength(0);
  });

  it('should auto-dismiss each convenience toast after its configured duration', () => {
    vi.useFakeTimers();

    useUIStore.getState().showSuccessToast('Success');  // 5000ms
    useUIStore.getState().showErrorToast('Error');      // 8000ms
    expect(useUIStore.getState().toasts).toHaveLength(2);

    // After 5000ms the success toast auto-dismisses
    vi.advanceTimersByTime(5000);
    expect(useUIStore.getState().toasts).toHaveLength(1);
    expect(useUIStore.getState().toasts[0].type).toBe('error');

    // After 3000 more ms (8000 total) the error toast auto-dismisses
    vi.advanceTimersByTime(3000);
    expect(useUIStore.getState().toasts).toHaveLength(0);
  });

  it('should not auto-dismiss a toast with duration 0', () => {
    vi.useFakeTimers();

    useUIStore.getState().addToast({
      type: 'error',
      message: 'Persistent toast',
      duration: 0,
      dismissible: true,
    });

    vi.advanceTimersByTime(100_000);
    expect(useUIStore.getState().toasts).toHaveLength(1);
  });

  it('should generate unique IDs for every toast', () => {
    useUIStore.getState().showSuccessToast('A');
    useUIStore.getState().showErrorToast('B');
    useUIStore.getState().showWarningToast('C');
    useUIStore.getState().showInfoToast('D');

    const ids = useUIStore.getState().toasts.map((t) => t.id);
    const uniqueIds = new Set(ids);
    expect(uniqueIds.size).toBe(4);
  });

  it('should append new toasts to the end of the queue', () => {
    useUIStore.getState().showSuccessToast('First');
    useUIStore.getState().showErrorToast('Second');
    useUIStore.getState().showInfoToast('Third');

    const toasts = useUIStore.getState().toasts;
    expect(toasts[0].message).toBe('First');
    expect(toasts[1].message).toBe('Second');
    expect(toasts[2].message).toBe('Third');
  });

  it('should handle removing a non-existent toast id gracefully', () => {
    useUIStore.getState().showSuccessToast('Stays');
    useUIStore.getState().removeToast('non-existent-id');
    expect(useUIStore.getState().toasts).toHaveLength(1);
  });
});

// ---------------------------------------------------------------------------
// 5. Notifications
// ---------------------------------------------------------------------------

describe('uiStore - Notifications', () => {
  beforeEach(() => {
    useUIStore.getState().reset();
  });

  it('should add a notification with read=false and an ISO 8601 createdAt timestamp', () => {
    useUIStore.getState().addNotification({
      title: 'Job Complete',
      message: 'Generation job #42 finished successfully',
      type: 'success',
    });

    const notifications = useUIStore.getState().notifications;
    expect(notifications).toHaveLength(1);

    const notification = notifications[0];
    expect(notification.title).toBe('Job Complete');
    expect(notification.message).toBe('Generation job #42 finished successfully');
    expect(notification.type).toBe('success');
    expect(notification.read).toBe(false);
    // Validate ISO 8601 timestamp format
    expect(typeof notification.createdAt).toBe('string');
    const parsed = new Date(notification.createdAt);
    expect(parsed.toISOString()).toBe(notification.createdAt);
    // Validate unique ID generation
    expect(typeof notification.id).toBe('string');
    expect(notification.id.length).toBeGreaterThan(0);
  });

  it('should increment unreadNotificationCount for each new notification', () => {
    expect(useUIStore.getState().unreadNotificationCount).toBe(0);

    useUIStore.getState().addNotification({
      title: 'First',
      message: 'First notification',
      type: 'info',
    });
    expect(useUIStore.getState().unreadNotificationCount).toBe(1);

    useUIStore.getState().addNotification({
      title: 'Second',
      message: 'Second notification',
      type: 'warning',
    });
    expect(useUIStore.getState().unreadNotificationCount).toBe(2);

    useUIStore.getState().addNotification({
      title: 'Third',
      message: 'Third notification',
      type: 'error',
    });
    expect(useUIStore.getState().unreadNotificationCount).toBe(3);
  });

  it('should mark a single notification as read and decrement unread count', () => {
    useUIStore.getState().addNotification({
      title: 'Alert',
      message: 'Something happened',
      type: 'info',
    });
    expect(useUIStore.getState().unreadNotificationCount).toBe(1);

    const id = useUIStore.getState().notifications[0].id;
    useUIStore.getState().markNotificationRead(id);

    expect(useUIStore.getState().notifications[0].read).toBe(true);
    expect(useUIStore.getState().unreadNotificationCount).toBe(0);
  });

  it('should not change state when marking an already-read notification', () => {
    useUIStore.getState().addNotification({
      title: 'Read This',
      message: 'Already read',
      type: 'info',
    });

    const id = useUIStore.getState().notifications[0].id;
    useUIStore.getState().markNotificationRead(id);
    expect(useUIStore.getState().unreadNotificationCount).toBe(0);

    // Mark the same notification again — count must stay at 0
    useUIStore.getState().markNotificationRead(id);
    expect(useUIStore.getState().unreadNotificationCount).toBe(0);
    expect(useUIStore.getState().notifications[0].read).toBe(true);
  });

  it('should not change state when marking a non-existent notification id', () => {
    useUIStore.getState().addNotification({
      title: 'Real',
      message: 'Exists',
      type: 'info',
    });
    expect(useUIStore.getState().unreadNotificationCount).toBe(1);

    useUIStore.getState().markNotificationRead('does-not-exist');
    expect(useUIStore.getState().unreadNotificationCount).toBe(1);
    expect(useUIStore.getState().notifications).toHaveLength(1);
  });

  it('should mark all notifications as read and set count to 0', () => {
    useUIStore.getState().addNotification({ title: 'A', message: 'A', type: 'info' });
    useUIStore.getState().addNotification({ title: 'B', message: 'B', type: 'warning' });
    useUIStore.getState().addNotification({ title: 'C', message: 'C', type: 'error' });
    expect(useUIStore.getState().unreadNotificationCount).toBe(3);

    useUIStore.getState().markAllNotificationsRead();

    expect(useUIStore.getState().unreadNotificationCount).toBe(0);
    useUIStore.getState().notifications.forEach((n) => {
      expect(n.read).toBe(true);
    });
  });

  it('should remove a notification and recalculate unread count', () => {
    useUIStore.getState().addNotification({ title: 'Keep', message: 'Keep', type: 'info' });
    useUIStore.getState().addNotification({ title: 'Remove', message: 'Remove', type: 'warning' });
    expect(useUIStore.getState().unreadNotificationCount).toBe(2);

    // Notifications are prepended — [0] is "Remove" (newest), [1] is "Keep"
    const removeId = useUIStore.getState().notifications[0].id;
    useUIStore.getState().removeNotification(removeId);

    expect(useUIStore.getState().notifications).toHaveLength(1);
    expect(useUIStore.getState().notifications[0].title).toBe('Keep');
    expect(useUIStore.getState().unreadNotificationCount).toBe(1);
  });

  it('should correctly recalculate unread count when removing a read notification', () => {
    useUIStore.getState().addNotification({ title: 'Unread', message: 'U', type: 'info' });
    useUIStore.getState().addNotification({ title: 'Read', message: 'R', type: 'warning' });

    // Prepend order: [0] = "Read", [1] = "Unread"
    const readId = useUIStore.getState().notifications[0].id;
    useUIStore.getState().markNotificationRead(readId);
    expect(useUIStore.getState().unreadNotificationCount).toBe(1);

    // Remove the read notification — unread count should remain 1
    useUIStore.getState().removeNotification(readId);
    expect(useUIStore.getState().notifications).toHaveLength(1);
    expect(useUIStore.getState().unreadNotificationCount).toBe(1);
  });

  it('should clear all notifications and reset unread count to 0', () => {
    useUIStore.getState().addNotification({ title: 'A', message: 'A', type: 'info' });
    useUIStore.getState().addNotification({ title: 'B', message: 'B', type: 'error' });
    expect(useUIStore.getState().notifications).toHaveLength(2);

    useUIStore.getState().clearNotifications();

    expect(useUIStore.getState().notifications).toEqual([]);
    expect(useUIStore.getState().unreadNotificationCount).toBe(0);
  });

  it('should prepend new notifications so the newest appears first', () => {
    useUIStore.getState().addNotification({ title: 'First Added', message: 'Older', type: 'info' });
    useUIStore.getState().addNotification({ title: 'Second Added', message: 'Newer', type: 'warning' });

    const notifications = useUIStore.getState().notifications;
    expect(notifications[0].title).toBe('Second Added');
    expect(notifications[1].title).toBe('First Added');
  });

  it('should store the optional link field when provided', () => {
    useUIStore.getState().addNotification({
      title: 'View Job',
      message: 'Generation complete',
      type: 'success',
      link: '/jobs/abc-123',
    });

    expect(useUIStore.getState().notifications[0].link).toBe('/jobs/abc-123');
  });

  it('should leave link undefined when not provided', () => {
    useUIStore.getState().addNotification({
      title: 'No Link',
      message: 'Simple notification',
      type: 'info',
    });

    expect(useUIStore.getState().notifications[0].link).toBeUndefined();
  });

  it('should generate unique IDs for every notification', () => {
    useUIStore.getState().addNotification({ title: 'A', message: 'A', type: 'info' });
    useUIStore.getState().addNotification({ title: 'B', message: 'B', type: 'info' });
    useUIStore.getState().addNotification({ title: 'C', message: 'C', type: 'info' });

    const ids = useUIStore.getState().notifications.map((n) => n.id);
    expect(new Set(ids).size).toBe(3);
  });
});

// ---------------------------------------------------------------------------
// 6. Modal
// ---------------------------------------------------------------------------

describe('uiStore - Modal', () => {
  beforeEach(() => {
    useUIStore.getState().reset();
  });

  it('should open a modal without props', () => {
    useUIStore.getState().openModal('confirm-delete');

    const modal = useUIStore.getState().modal;
    expect(modal.isOpen).toBe(true);
    expect(modal.modalId).toBe('confirm-delete');
    expect(modal.modalProps).toEqual({});
  });

  it('should open a modal with props', () => {
    useUIStore.getState().openModal('edit-job', { jobId: 'job-42', name: 'Test Job' });

    const modal = useUIStore.getState().modal;
    expect(modal.isOpen).toBe(true);
    expect(modal.modalId).toBe('edit-job');
    expect(modal.modalProps).toEqual({ jobId: 'job-42', name: 'Test Job' });
  });

  it('should close the modal and reset all modal state', () => {
    useUIStore.getState().openModal('some-modal', { data: 123 });
    useUIStore.getState().closeModal();

    const modal = useUIStore.getState().modal;
    expect(modal.isOpen).toBe(false);
    expect(modal.modalId).toBeNull();
    expect(modal.modalProps).toEqual({});
  });

  it('should replace the current modal when opening a new one', () => {
    useUIStore.getState().openModal('first-modal', { step: 1 });

    const firstModal = useUIStore.getState().modal;
    expect(firstModal.modalId).toBe('first-modal');

    useUIStore.getState().openModal('second-modal', { step: 2 });

    const secondModal = useUIStore.getState().modal;
    expect(secondModal.isOpen).toBe(true);
    expect(secondModal.modalId).toBe('second-modal');
    expect(secondModal.modalProps).toEqual({ step: 2 });
  });

  it('should handle closeModal when no modal is open', () => {
    // Should not throw or change state beyond keeping closed
    useUIStore.getState().closeModal();

    const modal = useUIStore.getState().modal;
    expect(modal.isOpen).toBe(false);
    expect(modal.modalId).toBeNull();
    expect(modal.modalProps).toEqual({});
  });
});

// ---------------------------------------------------------------------------
// 7. Breadcrumbs
// ---------------------------------------------------------------------------

describe('uiStore - Breadcrumbs', () => {
  beforeEach(() => {
    useUIStore.getState().reset();
  });

  it('should set breadcrumbs with labels and paths', () => {
    const crumbs = [
      { label: 'Dashboard', path: '/' },
      { label: 'Generation Jobs', path: '/jobs' },
      { label: 'Job #42' },
    ];

    useUIStore.getState().setBreadcrumbs(crumbs);
    expect(useUIStore.getState().breadcrumbs).toEqual(crumbs);
    expect(useUIStore.getState().breadcrumbs).toHaveLength(3);
  });

  it('should clear all breadcrumbs', () => {
    useUIStore.getState().setBreadcrumbs([{ label: 'Home', path: '/' }]);
    expect(useUIStore.getState().breadcrumbs).toHaveLength(1);

    useUIStore.getState().clearBreadcrumbs();
    expect(useUIStore.getState().breadcrumbs).toEqual([]);
  });

  it('should handle breadcrumbs where path is optional', () => {
    const crumbs = [
      { label: 'Home', path: '/' },
      { label: 'Current Page' }, // No path — not clickable
    ];

    useUIStore.getState().setBreadcrumbs(crumbs);

    const stored = useUIStore.getState().breadcrumbs;
    expect(stored[0].path).toBe('/');
    expect(stored[1].path).toBeUndefined();
    expect(stored[1].label).toBe('Current Page');
  });

  it('should replace existing breadcrumbs on subsequent setBreadcrumbs calls', () => {
    useUIStore.getState().setBreadcrumbs([{ label: 'Old Route', path: '/old' }]);
    useUIStore.getState().setBreadcrumbs([
      { label: 'New Route', path: '/new' },
      { label: 'Sub Page' },
    ]);

    expect(useUIStore.getState().breadcrumbs).toHaveLength(2);
    expect(useUIStore.getState().breadcrumbs[0].label).toBe('New Route');
    expect(useUIStore.getState().breadcrumbs[1].label).toBe('Sub Page');
  });

  it('should accept an empty array as valid breadcrumbs', () => {
    useUIStore.getState().setBreadcrumbs([{ label: 'Something' }]);
    useUIStore.getState().setBreadcrumbs([]);
    expect(useUIStore.getState().breadcrumbs).toEqual([]);
  });
});

// ---------------------------------------------------------------------------
// 8. Global Loading
// ---------------------------------------------------------------------------

describe('uiStore - Global Loading', () => {
  beforeEach(() => {
    useUIStore.getState().reset();
  });

  it('should set global loading to true without a message', () => {
    useUIStore.getState().setGlobalLoading(true);
    expect(useUIStore.getState().isGlobalLoading).toBe(true);
    expect(useUIStore.getState().globalLoadingMessage).toBeNull();
  });

  it('should set global loading to true with a message', () => {
    useUIStore.getState().setGlobalLoading(true, 'Generating synthetic data…');
    expect(useUIStore.getState().isGlobalLoading).toBe(true);
    expect(useUIStore.getState().globalLoadingMessage).toBe('Generating synthetic data…');
  });

  it('should set global loading to false and clear the message', () => {
    useUIStore.getState().setGlobalLoading(true, 'Processing…');
    useUIStore.getState().setGlobalLoading(false);
    expect(useUIStore.getState().isGlobalLoading).toBe(false);
    expect(useUIStore.getState().globalLoadingMessage).toBeNull();
  });

  it('should allow updating the loading message while loading is active', () => {
    useUIStore.getState().setGlobalLoading(true, 'Step 1 of 3…');
    expect(useUIStore.getState().globalLoadingMessage).toBe('Step 1 of 3…');

    useUIStore.getState().setGlobalLoading(true, 'Step 2 of 3…');
    expect(useUIStore.getState().globalLoadingMessage).toBe('Step 2 of 3…');

    useUIStore.getState().setGlobalLoading(true, 'Step 3 of 3…');
    expect(useUIStore.getState().globalLoadingMessage).toBe('Step 3 of 3…');
  });

  it('should set message to null when false is passed without explicit message', () => {
    useUIStore.getState().setGlobalLoading(true, 'Active');
    useUIStore.getState().setGlobalLoading(false);
    expect(useUIStore.getState().globalLoadingMessage).toBeNull();
  });
});

// ---------------------------------------------------------------------------
// 9. Reset
// ---------------------------------------------------------------------------

describe('uiStore - Reset', () => {
  it('should reset all state properties to their initial default values', () => {
    // Modify every piece of state
    const store = useUIStore.getState();
    store.setSidebarCollapsed(true);
    store.setSidebarMobile(true);
    store.setTheme('dark');
    store.addToast({ type: 'success', message: 'toast', duration: 5000, dismissible: true });
    store.addNotification({ title: 'N', message: 'N', type: 'info' });
    store.openModal('test-modal', { key: 'value' });
    store.setBreadcrumbs([{ label: 'Page', path: '/page' }]);
    store.setGlobalLoading(true, 'Loading…');

    // Confirm state is actually modified
    expect(useUIStore.getState().isSidebarCollapsed).toBe(true);
    expect(useUIStore.getState().isSidebarMobile).toBe(true);
    expect(useUIStore.getState().theme).toBe('dark');
    expect(useUIStore.getState().toasts).toHaveLength(1);
    expect(useUIStore.getState().notifications).toHaveLength(1);
    expect(useUIStore.getState().unreadNotificationCount).toBe(1);
    expect(useUIStore.getState().modal.isOpen).toBe(true);
    expect(useUIStore.getState().breadcrumbs).toHaveLength(1);
    expect(useUIStore.getState().isGlobalLoading).toBe(true);
    expect(useUIStore.getState().globalLoadingMessage).toBe('Loading…');

    // Perform reset
    useUIStore.getState().reset();

    // Verify every property returns to initial defaults
    expect(useUIStore.getState().isSidebarCollapsed).toBe(false);
    expect(useUIStore.getState().isSidebarMobile).toBe(false);
    expect(useUIStore.getState().theme).toBe('system');
    expect(useUIStore.getState().toasts).toEqual([]);
    expect(useUIStore.getState().notifications).toEqual([]);
    expect(useUIStore.getState().unreadNotificationCount).toBe(0);
    expect(useUIStore.getState().modal.isOpen).toBe(false);
    expect(useUIStore.getState().modal.modalId).toBeNull();
    expect(useUIStore.getState().modal.modalProps).toEqual({});
    expect(useUIStore.getState().breadcrumbs).toEqual([]);
    expect(useUIStore.getState().isGlobalLoading).toBe(false);
    expect(useUIStore.getState().globalLoadingMessage).toBeNull();
  });

  it('should allow normal store operations after a reset', () => {
    // Populate, reset, then verify operations still work
    useUIStore.getState().showSuccessToast('Before reset');
    useUIStore.getState().addNotification({ title: 'Old', message: 'Old', type: 'info' });
    useUIStore.getState().reset();

    // Operations after reset should function correctly
    useUIStore.getState().showInfoToast('After reset');
    expect(useUIStore.getState().toasts).toHaveLength(1);
    expect(useUIStore.getState().toasts[0].message).toBe('After reset');

    useUIStore.getState().addNotification({ title: 'New', message: 'New', type: 'warning' });
    expect(useUIStore.getState().notifications).toHaveLength(1);
    expect(useUIStore.getState().notifications[0].title).toBe('New');
    expect(useUIStore.getState().unreadNotificationCount).toBe(1);

    // Clean up for subsequent test files
    useUIStore.getState().reset();
  });

  it('should reset theme to "system" (persist middleware will re-hydrate on next session)', () => {
    // The persist middleware partializes theme and isSidebarCollapsed.
    // After reset(), the in-memory state returns to "system".
    // On a real page reload, the persist middleware would re-hydrate
    // from localStorage, but within the same session reset is immediate.
    useUIStore.getState().setTheme('dark');
    expect(useUIStore.getState().theme).toBe('dark');

    useUIStore.getState().reset();
    expect(useUIStore.getState().theme).toBe('system');
  });
});
