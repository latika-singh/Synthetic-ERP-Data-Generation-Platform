/**
 * @fileoverview Custom React hook for role-based permission checking across the Web Console.
 *
 * Wraps the authStore's user state to provide a convenient, React-idiomatic API
 * for checking whether the current authenticated user has specific permissions
 * or roles. All permission/role check functions are memoized via `useCallback`
 * and all derived boolean flags are memoized via `useMemo` to prevent
 * unnecessary re-renders in consuming components.
 *
 * Supports five graduated user roles with fine-grained permissions:
 *   - **Platform Admin** — unrestricted access to all operations and admin functions
 *   - **Data Engineer** — broad operational access (generation, profiling, schemas, exports, quality, compliance read)
 *   - **Developer** — create/read generation, basic schema/template read, export access
 *   - **QA Engineer** — read-only generation, templates, compliance, and quality
 *   - **Data Analyst** — read-only generation, profiles, schemas, and quality
 *
 * Consumed by:
 *   - `routes.tsx` — route guards for role-based page access
 *   - `Sidebar.tsx` — role-based menu item visibility
 *   - `AdminPanel.tsx` — Platform Admin restriction enforcement
 *   - Various page components — conditional rendering of RBAC-protected actions
 *
 * @module hooks/usePermissions
 * @version 1.0.0
 */

import { useMemo, useCallback } from 'react';
import { useAuthStore } from '@/store/authStore';
import { Role, Permission, ROLE_PERMISSIONS } from '@/types/auth';
import type { User } from '@/types/auth';

// ============================================================================
// Return Type Interface
// ============================================================================

/**
 * Shape of the object returned by the {@link usePermissions} hook.
 *
 * Contains the current user context, permission/role checking functions,
 * role hierarchy comparison, and memoized convenience boolean flags for
 * every permission and role defined in the RBAC model.
 */
export interface UsePermissionsReturn {
  // ── Current User Context ────────────────────────────────────────────

  /** Authenticated user profile, or `null` when not logged in. */
  user: User | null;

  /** The user's assigned RBAC role, or `null` when not authenticated. */
  role: Role | null;

  /** Effective permission set for the current user (empty array when unauthenticated). */
  permissions: Permission[];

  /** Whether the user has a valid, non-expired access token. */
  isAuthenticated: boolean;

  // ── Permission Check Functions ──────────────────────────────────────

  /** Returns `true` if the authenticated user holds the specified permission. */
  hasPermission: (permission: Permission) => boolean;

  /** Returns `true` if the authenticated user has the exact specified role. */
  hasRole: (role: Role) => boolean;

  /** Returns `true` if the user holds **at least one** of the specified permissions. */
  hasAnyPermission: (permissions: Permission[]) => boolean;

  /** Returns `true` if the user holds **all** of the specified permissions. */
  hasAllPermissions: (permissions: Permission[]) => boolean;

  /**
   * Returns `true` if the user's role is at or above the specified minimum role
   * in the graduated hierarchy:
   *   Platform Admin (5) > Data Engineer (4) > Developer (3) > QA Engineer (2) > Data Analyst (1)
   */
  hasRoleOrHigher: (minimumRole: Role) => boolean;

  // ── Convenience Boolean Flags: Generation ───────────────────────────

  /** User can create new generation jobs (generation:create). */
  canCreateGeneration: boolean;

  /** User can view generation jobs and results (generation:read). */
  canReadGeneration: boolean;

  /** User can delete generation jobs and artifacts (generation:delete). */
  canDeleteGeneration: boolean;

  // ── Convenience Boolean Flags: Profile ──────────────────────────────

  /** User can initiate statistical profiling (profile:create). */
  canCreateProfile: boolean;

  /** User can view statistical profiles (profile:read). */
  canReadProfile: boolean;

  // ── Convenience Boolean Flags: Schema ───────────────────────────────

  /** User can trigger ERP schema discovery (schema:discover). */
  canDiscoverSchema: boolean;

  /** User can view discovered schema definitions (schema:read). */
  canReadSchema: boolean;

  // ── Convenience Boolean Flags: Template ─────────────────────────────

  /** User can create and edit generation templates (template:create). */
  canCreateTemplate: boolean;

  /** User can view generation templates (template:read). */
  canReadTemplate: boolean;

  /** User can delete generation templates (template:delete). */
  canDeleteTemplate: boolean;

  // ── Convenience Boolean Flags: Export ────────────────────────────────

  /** User can initiate data export or database provisioning (export:create). */
  canCreateExport: boolean;

  /** User can view export history and provisioning status (export:read). */
  canReadExport: boolean;

  // ── Convenience Boolean Flags: Admin ────────────────────────────────

  /** User can manage user accounts (admin:users). */
  canManageUsers: boolean;

  /** User can manage tenant configurations (admin:tenants). */
  canManageTenants: boolean;

  /** User can modify system-wide settings (admin:system). */
  canManageSystem: boolean;

  // ── Convenience Boolean Flags: Compliance ───────────────────────────

  /** User can view compliance scan results (compliance:read). */
  canReadCompliance: boolean;

  /** User can manage compliance rules and certifications (compliance:manage). */
  canManageCompliance: boolean;

  // ── Convenience Boolean Flags: Quality ──────────────────────────────

  /** User can view quality score reports (quality:read). */
  canReadQuality: boolean;

  // ── Role Identity Flags ─────────────────────────────────────────────

  /** User is a Platform Admin (full system access). */
  isAdmin: boolean;

  /** User is a Data Engineer. */
  isDataEngineer: boolean;

  /** User is a Developer. */
  isDeveloper: boolean;

  /** User is a QA Engineer. */
  isQAEngineer: boolean;

  /** User is a Data Analyst. */
  isDataAnalyst: boolean;
}

// ============================================================================
// Role Hierarchy Definition
// ============================================================================

/**
 * Numeric precedence for each role, used by {@link usePermissions}'s
 * `hasRoleOrHigher` function to compare roles in the graduated hierarchy.
 *
 * Higher numeric value indicates higher privilege level:
 *   Platform Admin (5) > Data Engineer (4) > Developer (3) > QA Engineer (2) > Data Analyst (1)
 */
const ROLE_HIERARCHY: Readonly<Record<Role, number>> = {
  [Role.PLATFORM_ADMIN]: 5,
  [Role.DATA_ENGINEER]: 4,
  [Role.DEVELOPER]: 3,
  [Role.QA_ENGINEER]: 2,
  [Role.DATA_ANALYST]: 1,
} as const;

// ============================================================================
// Hook Implementation
// ============================================================================

/**
 * Custom React hook for role-based permission checking.
 *
 * Reads the authenticated user's role and permissions from the Zustand
 * auth store and provides memoized check functions and convenience flags
 * for use in route guards, conditional rendering, and component logic.
 *
 * All callback functions are stable references (via `useCallback`) and
 * all boolean flags are derived values (via `useMemo`) to minimize
 * unnecessary re-renders in consuming components.
 *
 * @example
 * ```tsx
 * function GenerationPage() {
 *   const { canCreateGeneration, canDeleteGeneration, isAdmin } = usePermissions();
 *
 *   return (
 *     <div>
 *       {canCreateGeneration && <CreateJobButton />}
 *       {canDeleteGeneration && <DeleteJobButton />}
 *       {isAdmin && <AdminSettings />}
 *     </div>
 *   );
 * }
 * ```
 *
 * @example
 * ```tsx
 * function RouteGuard({ minimumRole, children }: Props) {
 *   const { hasRoleOrHigher } = usePermissions();
 *   if (!hasRoleOrHigher(minimumRole)) return <Navigate to="/unauthorized" />;
 *   return children;
 * }
 * ```
 *
 * @returns {UsePermissionsReturn} Object containing user context, check functions, and convenience flags
 */
function usePermissions(): UsePermissionsReturn {
  // ── Read auth state from Zustand store ────────────────────────────────
  const { user, isAuthenticated } = useAuthStore();

  // ── Derive role and permissions (null-safe) ───────────────────────────
  const role: Role | null = user?.role ?? null;
  const permissions: Permission[] = user?.permissions ?? [];

  // ── Permission check: single permission ───────────────────────────────
  /**
   * Checks if the authenticated user holds the specified permission.
   *
   * @param permission - The permission to check (e.g., Permission.GENERATION_CREATE)
   * @returns `true` if the user is authenticated and holds the permission
   */
  const hasPermission = useCallback(
    (permission: Permission): boolean => {
      if (!user) return false;
      return user.permissions.includes(permission);
    },
    [user],
  );

  // ── Role check: exact role match ──────────────────────────────────────
  /**
   * Checks if the authenticated user has the exact specified role.
   *
   * @param targetRole - The role to check against (e.g., Role.PLATFORM_ADMIN)
   * @returns `true` if the user is authenticated and has the specified role
   */
  const hasRole = useCallback(
    (targetRole: Role): boolean => {
      if (!user) return false;
      return user.role === targetRole;
    },
    [user],
  );

  // ── Permission check: any of multiple permissions ─────────────────────
  /**
   * Checks if the authenticated user holds at least one of the specified permissions.
   *
   * Useful for UI elements that should be visible when a user has any of
   * several related capabilities (e.g., any generation permission).
   *
   * @param requiredPermissions - Array of permissions to check (OR logic)
   * @returns `true` if the user holds at least one of the specified permissions
   */
  const hasAnyPermission = useCallback(
    (requiredPermissions: Permission[]): boolean => {
      if (!user) return false;
      return requiredPermissions.some((p: Permission) => user.permissions.includes(p));
    },
    [user],
  );

  // ── Permission check: all of multiple permissions ─────────────────────
  /**
   * Checks if the authenticated user holds all of the specified permissions.
   *
   * Useful for actions that require a combination of capabilities
   * (e.g., both creating and deleting generation jobs).
   *
   * @param requiredPermissions - Array of permissions to check (AND logic)
   * @returns `true` if the user holds every one of the specified permissions
   */
  const hasAllPermissions = useCallback(
    (requiredPermissions: Permission[]): boolean => {
      if (!user) return false;
      return requiredPermissions.every((p: Permission) => user.permissions.includes(p));
    },
    [user],
  );

  // ── Role hierarchy check ──────────────────────────────────────────────
  /**
   * Checks if the user's role is at or above the specified minimum in the
   * graduated role hierarchy.
   *
   * Hierarchy (highest to lowest):
   *   Platform Admin (5) > Data Engineer (4) > Developer (3) > QA Engineer (2) > Data Analyst (1)
   *
   * @param minimumRole - The minimum role required
   * @returns `true` if the user's role precedence >= the minimum role precedence
   */
  const hasRoleOrHigher = useCallback(
    (minimumRole: Role): boolean => {
      if (!user || !user.role) return false;
      return ROLE_HIERARCHY[user.role] >= ROLE_HIERARCHY[minimumRole];
    },
    [user],
  );

  // ── Memoized convenience boolean flags ────────────────────────────────
  // All flags are derived from the user's permissions and role, recomputed
  // only when the underlying permissions or role reference changes.

  // Generation permissions
  const canCreateGeneration = useMemo(
    () => hasPermission(Permission.GENERATION_CREATE),
    [hasPermission],
  );

  const canReadGeneration = useMemo(
    () => hasPermission(Permission.GENERATION_READ),
    [hasPermission],
  );

  const canDeleteGeneration = useMemo(
    () => hasPermission(Permission.GENERATION_DELETE),
    [hasPermission],
  );

  // Profile permissions
  const canCreateProfile = useMemo(
    () => hasPermission(Permission.PROFILE_CREATE),
    [hasPermission],
  );

  const canReadProfile = useMemo(
    () => hasPermission(Permission.PROFILE_READ),
    [hasPermission],
  );

  // Schema permissions
  const canDiscoverSchema = useMemo(
    () => hasPermission(Permission.SCHEMA_DISCOVER),
    [hasPermission],
  );

  const canReadSchema = useMemo(
    () => hasPermission(Permission.SCHEMA_READ),
    [hasPermission],
  );

  // Template permissions
  const canCreateTemplate = useMemo(
    () => hasPermission(Permission.TEMPLATE_CREATE),
    [hasPermission],
  );

  const canReadTemplate = useMemo(
    () => hasPermission(Permission.TEMPLATE_READ),
    [hasPermission],
  );

  const canDeleteTemplate = useMemo(
    () => hasPermission(Permission.TEMPLATE_DELETE),
    [hasPermission],
  );

  // Export permissions
  const canCreateExport = useMemo(
    () => hasPermission(Permission.EXPORT_CREATE),
    [hasPermission],
  );

  const canReadExport = useMemo(
    () => hasPermission(Permission.EXPORT_READ),
    [hasPermission],
  );

  // Admin permissions
  const canManageUsers = useMemo(
    () => hasPermission(Permission.ADMIN_USERS),
    [hasPermission],
  );

  const canManageTenants = useMemo(
    () => hasPermission(Permission.ADMIN_TENANTS),
    [hasPermission],
  );

  const canManageSystem = useMemo(
    () => hasPermission(Permission.ADMIN_SYSTEM),
    [hasPermission],
  );

  // Compliance permissions
  const canReadCompliance = useMemo(
    () => hasPermission(Permission.COMPLIANCE_READ),
    [hasPermission],
  );

  const canManageCompliance = useMemo(
    () => hasPermission(Permission.COMPLIANCE_MANAGE),
    [hasPermission],
  );

  // Quality permissions
  const canReadQuality = useMemo(
    () => hasPermission(Permission.QUALITY_READ),
    [hasPermission],
  );

  // Role identity flags
  const isAdmin = useMemo(
    () => hasRole(Role.PLATFORM_ADMIN),
    [hasRole],
  );

  const isDataEngineer = useMemo(
    () => hasRole(Role.DATA_ENGINEER),
    [hasRole],
  );

  const isDeveloper = useMemo(
    () => hasRole(Role.DEVELOPER),
    [hasRole],
  );

  const isQAEngineer = useMemo(
    () => hasRole(Role.QA_ENGINEER),
    [hasRole],
  );

  const isDataAnalyst = useMemo(
    () => hasRole(Role.DATA_ANALYST),
    [hasRole],
  );

  // ── Return composite result ───────────────────────────────────────────
  return {
    // Current user context
    user,
    role,
    permissions,
    isAuthenticated,

    // Permission and role check functions
    hasPermission,
    hasRole,
    hasAnyPermission,
    hasAllPermissions,
    hasRoleOrHigher,

    // Generation convenience flags
    canCreateGeneration,
    canReadGeneration,
    canDeleteGeneration,

    // Profile convenience flags
    canCreateProfile,
    canReadProfile,

    // Schema convenience flags
    canDiscoverSchema,
    canReadSchema,

    // Template convenience flags
    canCreateTemplate,
    canReadTemplate,
    canDeleteTemplate,

    // Export convenience flags
    canCreateExport,
    canReadExport,

    // Admin convenience flags
    canManageUsers,
    canManageTenants,
    canManageSystem,

    // Compliance convenience flags
    canReadCompliance,
    canManageCompliance,

    // Quality convenience flags
    canReadQuality,

    // Role identity flags
    isAdmin,
    isDataEngineer,
    isDeveloper,
    isQAEngineer,
    isDataAnalyst,
  };
}

// ============================================================================
// Exports
// ============================================================================

export type { UsePermissionsReturn };
export { usePermissions };
export default usePermissions;
