/**
 * @fileoverview Zustand state management store for ERP schemas and statistical profiles.
 *
 * This store manages all state related to ERP schema definitions and statistical
 * profiles within the Web Console. It provides typed async actions for CRUD
 * operations on schemas and profiles via the profileApi service layer, along
 * with per-operation loading and error states for optimistic UI rendering.
 *
 * Consumed by:
 *   - SchemaBrowser page (Screen S-005) — browsing and managing schema definitions
 *   - ProfileViewer page (Screen S-006) — viewing statistical profile distributions
 *   - GenerationWizard page (Screen S-002) — selecting schemas for generation config
 *   - Schema/profile dependent components across the application
 *
 * Architecture notes:
 *   - Uses Zustand 4.x `create()` without persistence middleware (schema/profile
 *     data is transient and should be freshly loaded from the API on each session)
 *   - Supports native handling of SAP, Oracle EBS, Microsoft Dynamics, and legacy
 *     system schemas per the ERP support requirements
 *   - Scoped to the four initial ERP modules per Constraint C-005: Financial
 *     Accounting, Human Resources, Sales & Distribution, Material Management
 *
 * @module store/schemaStore
 * @version 1.0.0
 */

import { create } from 'zustand';
import type { SchemaDefinition, SchemaDiscoveryRequest } from '@/types/schema';
import type { StatisticalProfile, ProfileRequest } from '@/types/profile';
import type { PaginationParams } from '@/types/api';
import {
  getSchemas,
  getSchemaById,
  discoverSchema,
  deleteSchema as deleteSchemaApi,
  refreshSchema as refreshSchemaApi,
  getSchemaRelationships,
  getProfiles,
  getProfileById,
  createProfile,
  deleteProfile as deleteProfileApi,
} from '@/services/profileApi';

// ============================================================================
// Error Message Extraction Utility
// ============================================================================

/**
 * Extracts a human-readable error message from an unknown caught error.
 *
 * Handles three error shapes in priority order:
 *   1. Axios errors with a `response.data.message` field from the API Gateway's
 *      structured error envelope (see ApiError in types/api.ts)
 *   2. Standard JavaScript `Error` instances with a `.message` property
 *   3. Raw string errors thrown directly
 *
 * Falls back to the provided default message when the error shape is
 * unrecognized (e.g., `null`, `undefined`, or an opaque object).
 *
 * @param error - The caught error value (typed as `unknown` per TypeScript strict mode)
 * @param fallback - Default message returned when the error cannot be parsed
 * @returns A human-readable error message string
 */
function extractErrorMessage(error: unknown, fallback: string): string {
  // Priority 1: Axios error with structured API error response body
  if (typeof error === 'object' && error !== null && 'response' in error) {
    const axiosError = error as {
      response?: { data?: { message?: string } };
    };
    if (axiosError.response?.data?.message) {
      return axiosError.response.data.message;
    }
  }

  // Priority 2: Standard Error instance
  if (error instanceof Error) {
    return error.message;
  }

  // Priority 3: Raw string error
  if (typeof error === 'string') {
    return error;
  }

  return fallback;
}

// ============================================================================
// Schema State Interface
// ============================================================================

/**
 * Complete state shape and action signatures for the schema/profile store.
 *
 * Divided into four logical sections:
 *   - **Schema state** — Paginated list of discovered schemas plus the currently
 *     selected schema for detail/edit views
 *   - **Profile state** — Paginated list of statistical profiles plus the
 *     currently selected profile for distribution chart rendering
 *   - **Loading/error states** — Per-operation loading flags and nullable error
 *     messages enabling granular UI feedback (e.g., showing a spinner only on
 *     the schema detail panel while the list remains interactive)
 *   - **Actions** — Async data-fetching functions, synchronous setters,
 *     error-clearing utilities, and a full store reset
 */
interface SchemaState {
  // -- Schema state ----------------------------------------------------------

  /** Paginated array of schema definitions loaded from the API. */
  schemas: SchemaDefinition[];

  /** The currently selected/active schema definition, or null if none selected. */
  selectedSchema: SchemaDefinition | null;

  /** Total number of schemas matching the current filter criteria. */
  schemasTotal: number;

  /** Current page index (1-based) for the schemas list. */
  schemasPage: number;

  /** Number of schemas per page. */
  schemasPageSize: number;

  // -- Profile state ---------------------------------------------------------

  /** Paginated array of statistical profiles loaded from the API. */
  profiles: StatisticalProfile[];

  /** The currently selected/active statistical profile, or null if none selected. */
  selectedProfile: StatisticalProfile | null;

  /** Total number of profiles matching the current filter criteria. */
  profilesTotal: number;

  /** Current page index (1-based) for the profiles list. */
  profilesPage: number;

  /** Number of profiles per page. */
  profilesPageSize: number;

  // -- Loading states --------------------------------------------------------

  /** True while the paginated schemas list is being fetched. */
  isSchemasLoading: boolean;

  /** True while a single schema's detail data is being fetched. */
  isSchemaDetailLoading: boolean;

  /** True while a schema discovery operation is in progress. */
  isDiscovering: boolean;

  /** True while the paginated profiles list is being fetched. */
  isProfilesLoading: boolean;

  /** True while a single profile's detail data is being fetched. */
  isProfileDetailLoading: boolean;

  /** True while a profiling/create-profile operation is in progress. */
  isProfiling: boolean;

  // -- Error states ----------------------------------------------------------

  /** Error message from the last failed schemas list fetch, or null. */
  schemasError: string | null;

  /** Error message from the last failed schema detail fetch, or null. */
  schemaDetailError: string | null;

  /** Error message from the last failed schema discovery, or null. */
  discoveryError: string | null;

  /** Error message from the last failed profiles list fetch, or null. */
  profilesError: string | null;

  /** Error message from the last failed profile detail fetch, or null. */
  profileDetailError: string | null;

  /** Error message from the last failed profiling operation, or null. */
  profilingError: string | null;

  // -- Schema actions --------------------------------------------------------

  /**
   * Fetches a paginated list of schema definitions from the API.
   *
   * Supports optional pagination (page, page_size, sort_by, sort_direction)
   * and filter parameters (erp_type, erp_module, status). Updates the schemas
   * array, pagination metadata, and schemasError on the store.
   *
   * @param params - Combined pagination and filter parameters; all optional
   */
  fetchSchemas: (
    params?: PaginationParams & {
      erp_type?: string;
      erp_module?: string;
      status?: string;
    }
  ) => Promise<void>;

  /**
   * Fetches a single schema definition by its unique identifier.
   *
   * Sets `selectedSchema` on success. Additionally pre-fetches FK relationship
   * data for SchemaBrowser (S-005) rendering optimization.
   *
   * @param schemaId - MongoDB ObjectId string identifying the schema
   */
  fetchSchemaById: (schemaId: string) => Promise<void>;

  /**
   * Initiates ERP schema discovery against a source system.
   *
   * Supports native handling of SAP, Oracle EBS, Microsoft Dynamics, and
   * legacy system schemas. Returns the newly created SchemaDefinition on
   * success (prepended to the schemas list) or null on failure.
   *
   * @param request - Schema discovery configuration with ERP connection details
   * @returns The newly created SchemaDefinition, or null on error
   */
  discoverSchema: (
    request: SchemaDiscoveryRequest
  ) => Promise<SchemaDefinition | null>;

  /**
   * Deletes a schema definition by its unique identifier.
   *
   * Removes the schema from the local schemas array and clears
   * selectedSchema if it matches the deleted schema.
   *
   * @param schemaId - MongoDB ObjectId string identifying the schema to delete
   * @returns True if deletion succeeded, false otherwise
   */
  deleteSchema: (schemaId: string) => Promise<boolean>;

  /**
   * Re-discovers a schema from its original ERP source, updating the
   * existing schema definition in-place.
   *
   * Updates both the schemas array entry and selectedSchema if it matches
   * the refreshed schema ID.
   *
   * @param schemaId - MongoDB ObjectId string identifying the schema to refresh
   */
  refreshSchema: (schemaId: string) => Promise<void>;

  /**
   * Synchronously sets the currently selected schema definition.
   *
   * @param schema - The SchemaDefinition to select, or null to deselect
   */
  setSelectedSchema: (schema: SchemaDefinition | null) => void;

  /**
   * Clears all schema-related error states (schemasError, schemaDetailError,
   * discoveryError) to null.
   */
  clearSchemaErrors: () => void;

  // -- Profile actions -------------------------------------------------------

  /**
   * Fetches a paginated list of statistical profiles from the API.
   *
   * Supports optional pagination and filter parameters. Updates the profiles
   * array, pagination metadata, and profilesError on the store.
   *
   * @param params - Combined pagination and filter parameters; all optional
   */
  fetchProfiles: (
    params?: PaginationParams & {
      erp_type?: string;
      erp_module?: string;
      status?: string;
    }
  ) => Promise<void>;

  /**
   * Fetches a single statistical profile by its unique identifier.
   *
   * Sets `selectedProfile` on success with the full profile including
   * table-level statistics and column distribution data for rendering
   * in ProfileViewer (S-006).
   *
   * @param profileId - UUID v4 string identifying the profile
   */
  fetchProfileById: (profileId: string) => Promise<void>;

  /**
   * Initiates a new statistical profiling job against an ERP data source.
   *
   * Returns the newly created StatisticalProfile on success (prepended to
   * the profiles list) or null on failure. Per Constraint C-001, the
   * profiling process captures metadata only — no raw production data.
   *
   * @param request - Profiling job configuration with ERP connection details
   * @returns The newly created StatisticalProfile, or null on error
   */
  createProfile: (
    request: ProfileRequest
  ) => Promise<StatisticalProfile | null>;

  /**
   * Deletes a statistical profile by its unique identifier.
   *
   * Removes the profile from the local profiles array and clears
   * selectedProfile if it matches the deleted profile.
   *
   * @param profileId - UUID v4 string identifying the profile to delete
   * @returns True if deletion succeeded, false otherwise
   */
  deleteProfile: (profileId: string) => Promise<boolean>;

  /**
   * Synchronously sets the currently selected statistical profile.
   *
   * @param profile - The StatisticalProfile to select, or null to deselect
   */
  setSelectedProfile: (profile: StatisticalProfile | null) => void;

  /**
   * Clears all profile-related error states (profilesError,
   * profileDetailError, profilingError) to null.
   */
  clearProfileErrors: () => void;

  // -- Global actions --------------------------------------------------------

  /**
   * Resets the entire store to its initial default state.
   *
   * All data arrays are emptied, selections are cleared, loading flags are
   * set to false, and error states are set to null. Action functions are
   * preserved (they are not part of the resettable initial state).
   */
  reset: () => void;
}

// ============================================================================
// Initial State Constants
// ============================================================================

/**
 * Default values for all non-function state properties in the schema store.
 *
 * Used during store creation and by the `reset()` action to restore the
 * store to a clean baseline. Action functions are defined separately in the
 * store factory callback and are preserved across resets via Zustand's
 * shallow merge behavior.
 */
const initialState = {
  // Schema state
  schemas: [] as SchemaDefinition[],
  selectedSchema: null as SchemaDefinition | null,
  schemasTotal: 0,
  schemasPage: 1,
  schemasPageSize: 20,

  // Profile state
  profiles: [] as StatisticalProfile[],
  selectedProfile: null as StatisticalProfile | null,
  profilesTotal: 0,
  profilesPage: 1,
  profilesPageSize: 20,

  // Loading states — all initially idle
  isSchemasLoading: false,
  isSchemaDetailLoading: false,
  isDiscovering: false,
  isProfilesLoading: false,
  isProfileDetailLoading: false,
  isProfiling: false,

  // Error states — all initially clear
  schemasError: null as string | null,
  schemaDetailError: null as string | null,
  discoveryError: null as string | null,
  profilesError: null as string | null,
  profileDetailError: null as string | null,
  profilingError: null as string | null,
};

// ============================================================================
// Store Creation
// ============================================================================

/**
 * Zustand store hook for ERP schema definitions and statistical profiles.
 *
 * Provides reactive state and async actions for:
 *   - Schema definition CRUD (list, get, discover, delete, refresh)
 *   - Statistical profile CRUD (list, get, create, delete)
 *   - Per-operation loading and error state tracking
 *   - Paginated result management with ERP-type/module/status filter support
 *
 * Uses plain `create()` without persistence middleware — schema and profile
 * data is transient and should be freshly loaded from the API on each session.
 *
 * @example
 * ```tsx
 * // In a React component
 * import { useSchemaStore } from '@/store/schemaStore';
 *
 * function SchemaBrowser() {
 *   const { schemas, fetchSchemas, isSchemasLoading, schemasError } = useSchemaStore();
 *
 *   useEffect(() => {
 *     fetchSchemas({ page: 1, page_size: 20, erp_type: 'sap' });
 *   }, [fetchSchemas]);
 *
 *   if (isSchemasLoading) return <LoadingSpinner />;
 *   if (schemasError) return <ErrorAlert message={schemasError} />;
 *   return schemas.map(s => <SchemaCard key={s.schema_id} schema={s} />);
 * }
 * ```
 */
export const useSchemaStore = create<SchemaState>()((set) => ({
  // Spread initial state values into the store
  ...initialState,

  // --------------------------------------------------------------------------
  // Schema Actions
  // --------------------------------------------------------------------------

  fetchSchemas: async (
    params?: PaginationParams & {
      erp_type?: string;
      erp_module?: string;
      status?: string;
    }
  ): Promise<void> => {
    set({ isSchemasLoading: true, schemasError: null });
    try {
      const response = await getSchemas(params);
      set({
        schemas: response.data.items,
        schemasTotal: response.data.total,
        schemasPage: response.data.page,
        schemasPageSize: response.data.page_size,
      });
    } catch (error: unknown) {
      set({
        schemasError: extractErrorMessage(error, 'Failed to fetch schemas'),
      });
    } finally {
      set({ isSchemasLoading: false });
    }
  },

  fetchSchemaById: async (schemaId: string): Promise<void> => {
    set({ isSchemaDetailLoading: true, schemaDetailError: null });
    try {
      const response = await getSchemaById(schemaId);
      set({ selectedSchema: response.data });

      // Pre-fetch FK relationship data for SchemaBrowser (S-005) optimization.
      // This warms the browser/service cache so relationship graphs render
      // without an additional loading delay when the user expands the schema
      // detail view. The result is consumed by SchemaBrowser components that
      // call getSchemaRelationships directly for dependency ordering.
      getSchemaRelationships(schemaId).catch(() => {
        // Silently swallow relationship pre-fetch errors — the primary schema
        // data is already loaded and relationships are not critical for the
        // initial detail view. Components can retry independently on demand.
      });
    } catch (error: unknown) {
      set({
        schemaDetailError: extractErrorMessage(
          error,
          'Failed to fetch schema details'
        ),
      });
    } finally {
      set({ isSchemaDetailLoading: false });
    }
  },

  discoverSchema: async (
    request: SchemaDiscoveryRequest
  ): Promise<SchemaDefinition | null> => {
    set({ isDiscovering: true, discoveryError: null });
    try {
      const response = await discoverSchema(request);
      const newSchema = response.data;

      // Prepend the newly discovered schema to the front of the list so it
      // appears at the top of the SchemaBrowser (S-005) for immediate access.
      set((state) => ({
        schemas: [newSchema, ...state.schemas],
      }));

      return newSchema;
    } catch (error: unknown) {
      set({
        discoveryError: extractErrorMessage(error, 'Schema discovery failed'),
      });
      return null;
    } finally {
      set({ isDiscovering: false });
    }
  },

  deleteSchema: async (schemaId: string): Promise<boolean> => {
    try {
      await deleteSchemaApi(schemaId);

      // Remove the deleted schema from the local list and clear the selected
      // schema if it was the one that was deleted, preventing stale UI state.
      set((state) => ({
        schemas: state.schemas.filter(
          (schema) => schema.schema_id !== schemaId
        ),
        selectedSchema:
          state.selectedSchema?.schema_id === schemaId
            ? null
            : state.selectedSchema,
      }));

      return true;
    } catch (error: unknown) {
      set({
        schemasError: extractErrorMessage(error, 'Failed to delete schema'),
      });
      return false;
    }
  },

  refreshSchema: async (schemaId: string): Promise<void> => {
    set({ isSchemaDetailLoading: true, schemaDetailError: null });
    try {
      const response = await refreshSchemaApi(schemaId);
      const refreshedSchema = response.data;

      // Update the schema in-place within the schemas list and also update
      // selectedSchema if it matches, ensuring both list and detail views
      // reflect the refreshed ERP schema metadata.
      set((state) => ({
        schemas: state.schemas.map((schema) =>
          schema.schema_id === schemaId ? refreshedSchema : schema
        ),
        selectedSchema:
          state.selectedSchema?.schema_id === schemaId
            ? refreshedSchema
            : state.selectedSchema,
      }));
    } catch (error: unknown) {
      set({
        schemaDetailError: extractErrorMessage(
          error,
          'Failed to refresh schema'
        ),
      });
    } finally {
      set({ isSchemaDetailLoading: false });
    }
  },

  setSelectedSchema: (schema: SchemaDefinition | null): void => {
    set({ selectedSchema: schema });
  },

  clearSchemaErrors: (): void => {
    set({
      schemasError: null,
      schemaDetailError: null,
      discoveryError: null,
    });
  },

  // --------------------------------------------------------------------------
  // Profile Actions
  // --------------------------------------------------------------------------

  fetchProfiles: async (
    params?: PaginationParams & {
      erp_type?: string;
      erp_module?: string;
      status?: string;
    }
  ): Promise<void> => {
    set({ isProfilesLoading: true, profilesError: null });
    try {
      const response = await getProfiles(params);
      set({
        profiles: response.data.items,
        profilesTotal: response.data.total,
        profilesPage: response.data.page,
        profilesPageSize: response.data.page_size,
      });
    } catch (error: unknown) {
      set({
        profilesError: extractErrorMessage(error, 'Failed to fetch profiles'),
      });
    } finally {
      set({ isProfilesLoading: false });
    }
  },

  fetchProfileById: async (profileId: string): Promise<void> => {
    set({ isProfileDetailLoading: true, profileDetailError: null });
    try {
      const response = await getProfileById(profileId);
      set({ selectedProfile: response.data });
    } catch (error: unknown) {
      set({
        profileDetailError: extractErrorMessage(
          error,
          'Failed to fetch profile details'
        ),
      });
    } finally {
      set({ isProfileDetailLoading: false });
    }
  },

  createProfile: async (
    request: ProfileRequest
  ): Promise<StatisticalProfile | null> => {
    set({ isProfiling: true, profilingError: null });
    try {
      const response = await createProfile(request);
      const newProfile = response.data;

      // Prepend the newly created profile to the front of the list so it
      // appears at the top of the ProfileViewer (S-006) for immediate access.
      set((state) => ({
        profiles: [newProfile, ...state.profiles],
      }));

      return newProfile;
    } catch (error: unknown) {
      set({
        profilingError: extractErrorMessage(
          error,
          'Profiling request failed'
        ),
      });
      return null;
    } finally {
      set({ isProfiling: false });
    }
  },

  deleteProfile: async (profileId: string): Promise<boolean> => {
    try {
      await deleteProfileApi(profileId);

      // Remove the deleted profile from the local list and clear the selected
      // profile if it was the one that was deleted, preventing stale UI state.
      set((state) => ({
        profiles: state.profiles.filter(
          (profile) => profile.profile_id !== profileId
        ),
        selectedProfile:
          state.selectedProfile?.profile_id === profileId
            ? null
            : state.selectedProfile,
      }));

      return true;
    } catch (error: unknown) {
      set({
        profilesError: extractErrorMessage(
          error,
          'Failed to delete profile'
        ),
      });
      return false;
    }
  },

  setSelectedProfile: (profile: StatisticalProfile | null): void => {
    set({ selectedProfile: profile });
  },

  clearProfileErrors: (): void => {
    set({
      profilesError: null,
      profileDetailError: null,
      profilingError: null,
    });
  },

  // --------------------------------------------------------------------------
  // Global Actions
  // --------------------------------------------------------------------------

  reset: (): void => {
    // Shallow-merge initial state values into the store. Since initialState
    // contains only data properties (not action functions), the Zustand
    // shallow merge preserves all action functions while resetting all data
    // properties to their defaults.
    set(initialState);
  },
}));

// ============================================================================
// Type Export
// ============================================================================

export type { SchemaState };

// ============================================================================
// Default Export
// ============================================================================

export default useSchemaStore;
