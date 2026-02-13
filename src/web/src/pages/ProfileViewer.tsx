/**
 * ProfileViewer Page Component (Screen S-006)
 *
 * Statistical profile visualization page for the Synthetic ERP Data Generation
 * Platform Web Console. Displays a list of profiled ERP schemas with their
 * statistical profiles, enabling drill-down to individual table profiles and
 * column-level distribution charts.
 *
 * Features:
 *   - Paginated profile list with search and ERP type filtering
 *   - Profile detail view with table selector tabs
 *   - Column statistics grid with DistributionChart per column
 *   - Alternative tabular view toggle for column statistics
 *   - Deep linking via URL search params (?profile_id=, ?table=)
 *   - Loading and error states with graceful handling
 *   - Responsive TailwindCSS 4.x layout
 *
 * Constraint adherence:
 *   - C-001: Displays metadata only — no raw production data
 *   - C-005: Supports four ERP modules (Financial Accounting, HR,
 *            Sales & Distribution, Material Management)
 *
 * @module pages/ProfileViewer
 * @version 1.0.0
 */

import React, { useState, useEffect, useMemo, useCallback } from 'react';
import { useSearchParams, Link } from 'react-router-dom';

import { useSchemaStore } from '@/store/schemaStore';
import { getProfileStatistics } from '@/services/profileApi';
import type { TableProfileDetail } from '@/services/profileApi';
import DistributionChart from '@/components/charts/DistributionChart';
import DataTable from '@/components/common/DataTable';
import type { ColumnDefinition } from '@/components/common/DataTable';
import LoadingSpinner from '@/components/common/LoadingSpinner';

import type {
  StatisticalProfile,
  ColumnStatistics,
  TableProfile,
  ColumnProfile,
} from '@/types/profile';
import type { SchemaDefinition } from '@/types/schema';
import { ERPType } from '@/types/schema';

// ============================================================================
// Constants
// ============================================================================

/** Human-readable labels for ERP type filter dropdown options. */
const ERP_TYPE_LABELS: Record<string, string> = {
  [ERPType.SAP]: 'SAP',
  [ERPType.ORACLE_EBS]: 'Oracle EBS',
  [ERPType.DYNAMICS]: 'Microsoft Dynamics',
  [ERPType.LEGACY]: 'Legacy System',
};

/** All ERP type values for the filter dropdown. */
const ERP_TYPE_OPTIONS = Object.entries(ERP_TYPE_LABELS);

/** Status badge color map for profile statuses. */
const STATUS_COLORS: Record<string, string> = {
  pending: 'bg-yellow-100 text-yellow-800 dark:bg-yellow-900/30 dark:text-yellow-300',
  in_progress: 'bg-blue-100 text-blue-800 dark:bg-blue-900/30 dark:text-blue-300',
  completed: 'bg-green-100 text-green-800 dark:bg-green-900/30 dark:text-green-300',
  failed: 'bg-red-100 text-red-800 dark:bg-red-900/30 dark:text-red-300',
};

// ============================================================================
// Helper Functions
// ============================================================================

/**
 * Formats an ISO 8601 date string into a human-readable locale date/time string.
 *
 * @param isoDate - ISO 8601 timestamp string (e.g., "2025-01-15T09:30:00Z")
 * @returns Localized date/time string or "N/A" if the input is falsy
 */
function formatDate(isoDate: string | undefined | null): string {
  if (!isoDate) return 'N/A';
  try {
    return new Date(isoDate).toLocaleDateString('en-US', {
      year: 'numeric',
      month: 'short',
      day: 'numeric',
      hour: '2-digit',
      minute: '2-digit',
    });
  } catch {
    return 'Invalid date';
  }
}

/**
 * Formats a numeric value to a fixed number of decimal places, returning
 * a dash for null/undefined values (non-numeric columns).
 *
 * @param value - Numeric value or null/undefined
 * @param decimals - Number of decimal places (default 2)
 * @returns Formatted numeric string or "—"
 */
function formatNumeric(value: number | null | undefined, decimals = 2): string {
  if (value === null || value === undefined) return '—';
  return Number(value).toFixed(decimals);
}

/**
 * Formats a percentage value with one decimal place and a trailing "%".
 *
 * @param value - Percentage value (0–100) or null/undefined
 * @returns Formatted percentage string or "—"
 */
function formatPercentage(value: number | null | undefined): string {
  if (value === null || value === undefined) return '—';
  return `${Number(value).toFixed(1)}%`;
}

/**
 * Renders a status badge element with appropriate coloring.
 *
 * @param status - Profile status string ("pending", "in_progress", "completed", "failed")
 * @returns React element rendering a colored badge
 */
function StatusBadge({ status }: { status: string }): React.JSX.Element {
  const colorClass = STATUS_COLORS[status] ?? 'bg-gray-100 text-gray-800 dark:bg-gray-800 dark:text-gray-300';
  const label = status.replace(/_/g, ' ').replace(/\b\w/g, (c) => c.toUpperCase());
  return (
    <span
      className={`inline-flex items-center px-2.5 py-0.5 rounded-full text-xs font-medium ${colorClass}`}
    >
      {label}
    </span>
  );
}

// ============================================================================
// View Mode Type
// ============================================================================

/** Toggle between chart grid view and tabular view for column statistics. */
type ViewMode = 'chart' | 'table';

// ============================================================================
// ProfileViewer Component
// ============================================================================

/**
 * ProfileViewer — Statistical profile visualization page (Screen S-006).
 *
 * Implements a two-level navigation pattern:
 *   1. **Profile List** — Paginated table of all statistical profiles with
 *      search, ERP type filter, and click-to-select interaction.
 *   2. **Profile Detail** — Selected profile's tables with a tab bar for
 *      table switching and a grid/table of per-column statistics with
 *      DistributionChart rendering for each column.
 *
 * Supports deep linking via URL search params:
 *   - `?profile_id=<uuid>` — Auto-selects and loads a specific profile
 *   - `?table=<table_name>` — Auto-selects a specific table within the profile
 *
 * @returns React JSX element rendering the ProfileViewer page
 */
function ProfileViewer(): React.JSX.Element {
  // --------------------------------------------------------------------------
  // URL search params for deep linking
  // --------------------------------------------------------------------------
  const [searchParams, setSearchParams] = useSearchParams();
  const urlProfileId = searchParams.get('profile_id');
  const urlTable = searchParams.get('table');

  // --------------------------------------------------------------------------
  // Zustand store bindings — profile state and actions
  // --------------------------------------------------------------------------
  const profiles = useSchemaStore((s) => s.profiles);
  const selectedProfile = useSchemaStore((s) => s.selectedProfile);
  const profilesTotal = useSchemaStore((s) => s.profilesTotal);
  const profilesPage = useSchemaStore((s) => s.profilesPage);
  const profilesPageSize = useSchemaStore((s) => s.profilesPageSize);
  const isProfilesLoading = useSchemaStore((s) => s.isProfilesLoading);
  const isProfileDetailLoading = useSchemaStore((s) => s.isProfileDetailLoading);
  const profilesError = useSchemaStore((s) => s.profilesError);
  const profileDetailError = useSchemaStore((s) => s.profileDetailError);
  const fetchProfiles = useSchemaStore((s) => s.fetchProfiles);
  const fetchProfileById = useSchemaStore((s) => s.fetchProfileById);
  const setSelectedProfile = useSchemaStore((s) => s.setSelectedProfile);
  const clearProfileErrors = useSchemaStore((s) => s.clearProfileErrors);

  // --------------------------------------------------------------------------
  // Local UI state
  // --------------------------------------------------------------------------

  /** Text search query for filtering profiles by ERP type or profile ID. */
  const [searchQuery, setSearchQuery] = useState<string>('');

  /** ERP type filter value (empty string = all types). */
  const [filterErpType, setFilterErpType] = useState<string>('');

  /** Currently selected table name within the profile detail view. */
  const [selectedTable, setSelectedTable] = useState<string>('');

  /** Toggle between chart grid view and tabular view. */
  const [viewMode, setViewMode] = useState<ViewMode>('chart');

  /** Detailed table statistics loaded from the drill-down API. */
  const [tableDetail, setTableDetail] = useState<TableProfileDetail | null>(null);

  /** Loading state for table detail drill-down. */
  const [isTableDetailLoading, setIsTableDetailLoading] = useState<boolean>(false);

  /** Error state for table detail drill-down. */
  const [tableDetailError, setTableDetailError] = useState<string | null>(null);

  // --------------------------------------------------------------------------
  // Data Fetching — Initial profile list load
  // --------------------------------------------------------------------------

  useEffect(() => {
    clearProfileErrors();
    fetchProfiles({
      page: profilesPage,
      page_size: profilesPageSize,
      ...(filterErpType ? { erp_type: filterErpType } : {}),
    });
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [profilesPage, profilesPageSize, filterErpType]);

  // --------------------------------------------------------------------------
  // Deep Linking — Auto-select profile from URL ?profile_id= param
  // --------------------------------------------------------------------------

  useEffect(() => {
    if (urlProfileId && (!selectedProfile || selectedProfile.profile_id !== urlProfileId)) {
      fetchProfileById(urlProfileId);
    }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [urlProfileId]);

  // --------------------------------------------------------------------------
  // Deep Linking — Auto-select table from URL ?table= param
  // --------------------------------------------------------------------------

  useEffect(() => {
    if (
      selectedProfile &&
      urlTable &&
      selectedProfile.tables.some((t: TableProfile) => t.table_name === urlTable)
    ) {
      setSelectedTable(urlTable);
    } else if (
      selectedProfile &&
      selectedProfile.tables.length > 0 &&
      !selectedTable
    ) {
      // Default to first table when no URL param and no selection yet
      setSelectedTable(selectedProfile.tables[0].table_name);
    }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [selectedProfile, urlTable]);

  // --------------------------------------------------------------------------
  // Data Fetching — Table detail drill-down when selected table changes
  // --------------------------------------------------------------------------

  useEffect(() => {
    if (!selectedProfile || !selectedTable) {
      setTableDetail(null);
      return;
    }

    let cancelled = false;
    setIsTableDetailLoading(true);
    setTableDetailError(null);

    getProfileStatistics(selectedProfile.profile_id, selectedTable)
      .then((response) => {
        if (!cancelled) {
          if (response.success && response.data) {
            setTableDetail(response.data);
          } else {
            setTableDetailError('Failed to load table statistics.');
          }
        }
      })
      .catch((err: unknown) => {
        if (!cancelled) {
          const msg =
            err instanceof Error ? err.message : 'An error occurred loading table statistics.';
          setTableDetailError(msg);
        }
      })
      .finally(() => {
        if (!cancelled) {
          setIsTableDetailLoading(false);
        }
      });

    return () => {
      cancelled = true;
    };
  }, [selectedProfile, selectedTable]);

  // --------------------------------------------------------------------------
  // Event Handlers
  // --------------------------------------------------------------------------

  /**
   * Handles profile selection from the list view. Updates the URL search
   * params and loads the full profile detail via the store.
   */
  const handleProfileSelect = useCallback(
    (profile: StatisticalProfile) => {
      setSelectedProfile(profile);
      setSelectedTable('');
      setTableDetail(null);
      setTableDetailError(null);
      setSearchParams({ profile_id: profile.profile_id });
      fetchProfileById(profile.profile_id);
    },
    [setSelectedProfile, setSearchParams, fetchProfileById],
  );

  /**
   * Handles table tab selection within the profile detail view.
   * Updates the URL search params and triggers the drill-down API call.
   */
  const handleTableSelect = useCallback(
    (tableName: string) => {
      setSelectedTable(tableName);
      setTableDetail(null);
      setTableDetailError(null);
      if (selectedProfile) {
        setSearchParams({
          profile_id: selectedProfile.profile_id,
          table: tableName,
        });
      }
    },
    [selectedProfile, setSearchParams],
  );

  /**
   * Handles search query changes for the profile list.
   */
  const handleSearch = useCallback((value: string) => {
    setSearchQuery(value);
  }, []);

  /**
   * Handles ERP type filter dropdown changes.
   */
  const handleFilterChange = useCallback((value: string) => {
    setFilterErpType(value);
  }, []);

  /**
   * Navigates back to the profile list from the detail view.
   */
  const handleBackToList = useCallback(() => {
    setSelectedProfile(null);
    setSelectedTable('');
    setTableDetail(null);
    setTableDetailError(null);
    setSearchParams({});
  }, [setSelectedProfile, setSearchParams]);

  /**
   * Handles pagination page changes for the profile list.
   */
  const handlePageChange = useCallback(
    (page: number) => {
      fetchProfiles({
        page,
        page_size: profilesPageSize,
        ...(filterErpType ? { erp_type: filterErpType } : {}),
      });
    },
    [fetchProfiles, profilesPageSize, filterErpType],
  );

  // --------------------------------------------------------------------------
  // Memoized Computations
  // --------------------------------------------------------------------------

  /**
   * Filtered profiles list based on the search query. Filters by profile_id
   * and erp_type string containment to support quick-find in the UI.
   */
  const filteredProfiles = useMemo<StatisticalProfile[]>(() => {
    if (!searchQuery.trim()) return profiles;
    const query = searchQuery.toLowerCase().trim();
    return profiles.filter(
      (p: StatisticalProfile) =>
        p.profile_id.toLowerCase().includes(query) ||
        p.erp_type.toLowerCase().includes(query) ||
        (p.status && p.status.toLowerCase().includes(query)),
    );
  }, [profiles, searchQuery]);

  /**
   * Extracts column statistics from the loaded table detail for rendering
   * in the chart grid or tabular view. Returns only columns that have
   * a non-null statistics object.
   */
  const columnStatistics = useMemo<ColumnStatistics[]>(() => {
    if (!tableDetail) return [];
    return tableDetail.columns
      .filter((col) => col.statistics !== null && col.statistics !== undefined)
      .map((col) => col.statistics as ColumnStatistics);
  }, [tableDetail]);

  /**
   * Finds the currently selected TableProfile from the selectedProfile's tables
   * array for displaying table-level metadata (row count, column count).
   */
  const activeTableProfile = useMemo<TableProfile | null>(() => {
    if (!selectedProfile || !selectedTable) return null;
    return (
      selectedProfile.tables.find(
        (t: TableProfile) => t.table_name === selectedTable,
      ) ?? null
    );
  }, [selectedProfile, selectedTable]);

  /**
   * Extracts ColumnProfile entries from the active table for iteration in the
   * detail view, providing access to column_name, data_type, and statistics.
   */
  const activeColumns = useMemo<ColumnProfile[]>(() => {
    if (!activeTableProfile) return [];
    return activeTableProfile.columns.filter(
      (col: ColumnProfile) =>
        col.column_name !== undefined && col.data_type !== undefined,
    );
  }, [activeTableProfile]);

  // --------------------------------------------------------------------------
  // Column Definitions — Profile List DataTable
  // --------------------------------------------------------------------------

  const profileListColumns = useMemo<ColumnDefinition<Record<string, unknown>>[]>(
    () => [
      {
        key: 'profile_id',
        header: 'Profile ID',
        sortable: true,
        width: 'min-w-[200px]',
        render: (value: unknown) => (
          <span className="font-mono text-sm text-indigo-600 dark:text-indigo-400 truncate max-w-[200px] block">
            {String(value).slice(0, 12)}…
          </span>
        ),
      },
      {
        key: 'erp_type',
        header: 'ERP Type',
        sortable: true,
        width: 'w-36',
        render: (value: unknown) => (
          <span className="text-sm">
            {ERP_TYPE_LABELS[String(value)] ?? String(value)}
          </span>
        ),
      },
      {
        key: 'total_tables',
        header: 'Tables',
        sortable: true,
        align: 'center' as const,
        width: 'w-24',
        render: (value: unknown) => (
          <span className="font-medium">{String(value ?? 0)}</span>
        ),
      },
      {
        key: 'total_columns',
        header: 'Columns',
        sortable: true,
        align: 'center' as const,
        width: 'w-24',
        render: (value: unknown) => (
          <span className="font-medium">{String(value ?? 0)}</span>
        ),
      },
      {
        key: 'created_at',
        header: 'Created At',
        sortable: true,
        width: 'w-48',
        render: (value: unknown) => (
          <span className="text-sm text-gray-600 dark:text-gray-400">
            {formatDate(value as string)}
          </span>
        ),
      },
      {
        key: 'status',
        header: 'Status',
        sortable: true,
        align: 'center' as const,
        width: 'w-32',
        render: (value: unknown) => <StatusBadge status={String(value)} />,
      },
      {
        key: 'profile_id',
        header: 'Action',
        width: 'w-24',
        align: 'center' as const,
        render: (_value: unknown, row: Record<string, unknown>) => (
          <button
            type="button"
            className="text-indigo-600 hover:text-indigo-800 dark:text-indigo-400 dark:hover:text-indigo-300 text-sm font-medium transition-colors"
            onClick={(e) => {
              e.stopPropagation();
              handleProfileSelect(row as unknown as StatisticalProfile);
            }}
          >
            View
          </button>
        ),
      },
    ],
    [handleProfileSelect],
  );

  // --------------------------------------------------------------------------
  // Column Definitions — Column Statistics DataTable (tabular view mode)
  // --------------------------------------------------------------------------

  const columnStatsTableColumns = useMemo<ColumnDefinition<Record<string, unknown>>[]>(
    () => [
      {
        key: 'column_name',
        header: 'Column Name',
        sortable: true,
        width: 'min-w-[160px]',
        render: (value: unknown) => (
          <span className="font-mono text-sm font-medium">{String(value)}</span>
        ),
      },
      {
        key: 'data_type',
        header: 'Data Type',
        sortable: true,
        width: 'w-28',
        render: (value: unknown) => (
          <span className="inline-flex px-2 py-0.5 rounded bg-gray-100 dark:bg-gray-700 text-xs font-medium">
            {String(value)}
          </span>
        ),
      },
      {
        key: 'distribution.type',
        header: 'Distribution',
        sortable: true,
        width: 'w-32',
        render: (value: unknown, row: Record<string, unknown>) => {
          const dist = (row as unknown as ColumnStatistics).distribution;
          return (
            <span className="text-sm capitalize">{dist?.type?.replace(/_/g, ' ') ?? '—'}</span>
          );
        },
      },
      {
        key: 'mean',
        header: 'Mean',
        align: 'right' as const,
        width: 'w-24',
        render: (value: unknown) => (
          <span className="font-mono text-sm">{formatNumeric(value as number | null)}</span>
        ),
      },
      {
        key: 'std_dev',
        header: 'Std Dev',
        align: 'right' as const,
        width: 'w-24',
        render: (value: unknown) => (
          <span className="font-mono text-sm">{formatNumeric(value as number | null)}</span>
        ),
      },
      {
        key: 'min_value',
        header: 'Min',
        align: 'right' as const,
        width: 'w-24',
        render: (value: unknown) => (
          <span className="font-mono text-sm">{formatNumeric(value as number | null)}</span>
        ),
      },
      {
        key: 'max_value',
        header: 'Max',
        align: 'right' as const,
        width: 'w-24',
        render: (value: unknown) => (
          <span className="font-mono text-sm">{formatNumeric(value as number | null)}</span>
        ),
      },
      {
        key: 'null_percentage',
        header: 'Null %',
        align: 'right' as const,
        width: 'w-24',
        render: (value: unknown) => (
          <span className="font-mono text-sm">{formatPercentage(value as number | null)}</span>
        ),
      },
      {
        key: 'unique_count',
        header: 'Unique',
        align: 'right' as const,
        width: 'w-24',
        render: (value: unknown) => (
          <span className="font-mono text-sm">{value !== null && value !== undefined ? String(value) : '—'}</span>
        ),
      },
      {
        key: 'distribution.fit_score',
        header: 'Fit Score',
        align: 'right' as const,
        width: 'w-24',
        render: (value: unknown, row: Record<string, unknown>) => {
          const dist = (row as unknown as ColumnStatistics).distribution;
          const score = dist?.fit_score;
          if (score === null || score === undefined) return <span className="text-sm">—</span>;
          const color =
            score >= 0.9
              ? 'text-green-600 dark:text-green-400'
              : score >= 0.7
                ? 'text-yellow-600 dark:text-yellow-400'
                : 'text-red-600 dark:text-red-400';
          return (
            <span className={`font-mono text-sm font-medium ${color}`}>
              {(score * 100).toFixed(1)}%
            </span>
          );
        },
      },
    ],
    [],
  );

  // --------------------------------------------------------------------------
  // Render — Loading state for initial profile list
  // --------------------------------------------------------------------------

  if (isProfilesLoading && profiles.length === 0) {
    return (
      <div className="flex items-center justify-center min-h-[400px]">
        <LoadingSpinner size="lg" message="Loading profiles..." />
      </div>
    );
  }

  // --------------------------------------------------------------------------
  // Render — Profile Detail View (when a profile is selected)
  // --------------------------------------------------------------------------

  if (selectedProfile) {
    return (
      <div className="space-y-6">
        {/* Profile Detail Header */}
        <div className="flex flex-col sm:flex-row sm:items-center sm:justify-between gap-4">
          <div className="flex items-center gap-3">
            <button
              type="button"
              onClick={handleBackToList}
              className="inline-flex items-center gap-1 text-sm text-gray-600 hover:text-gray-900 dark:text-gray-400 dark:hover:text-gray-200 transition-colors"
              aria-label="Back to profile list"
            >
              <svg className="w-4 h-4" fill="none" viewBox="0 0 24 24" strokeWidth={2} stroke="currentColor" aria-hidden="true">
                <path strokeLinecap="round" strokeLinejoin="round" d="M15.75 19.5L8.25 12l7.5-7.5" />
              </svg>
              Back to Profiles
            </button>
          </div>
          <div className="flex items-center gap-3">
            <StatusBadge status={selectedProfile.status} />
          </div>
        </div>

        {/* Profile Summary Card */}
        <div className="bg-white dark:bg-gray-800 rounded-lg shadow-sm border border-gray-200 dark:border-gray-700 p-6">
          <div className="flex flex-col lg:flex-row lg:items-start lg:justify-between gap-4">
            <div>
              <h1 className="text-2xl font-bold text-gray-900 dark:text-white">
                Statistical Profile
              </h1>
              <p className="mt-1 text-sm text-gray-500 dark:text-gray-400 font-mono">
                {selectedProfile.profile_id}
              </p>
            </div>
            <div className="grid grid-cols-2 sm:grid-cols-4 gap-4 text-center">
              <div className="bg-gray-50 dark:bg-gray-700/50 rounded-lg px-4 py-3">
                <p className="text-xs text-gray-500 dark:text-gray-400 uppercase tracking-wider">ERP Type</p>
                <p className="mt-1 text-lg font-semibold text-gray-900 dark:text-white">
                  {ERP_TYPE_LABELS[selectedProfile.erp_type] ?? selectedProfile.erp_type}
                </p>
              </div>
              <div className="bg-gray-50 dark:bg-gray-700/50 rounded-lg px-4 py-3">
                <p className="text-xs text-gray-500 dark:text-gray-400 uppercase tracking-wider">Tables</p>
                <p className="mt-1 text-lg font-semibold text-gray-900 dark:text-white">
                  {selectedProfile.total_tables}
                </p>
              </div>
              <div className="bg-gray-50 dark:bg-gray-700/50 rounded-lg px-4 py-3">
                <p className="text-xs text-gray-500 dark:text-gray-400 uppercase tracking-wider">Columns</p>
                <p className="mt-1 text-lg font-semibold text-gray-900 dark:text-white">
                  {selectedProfile.total_columns}
                </p>
              </div>
              <div className="bg-gray-50 dark:bg-gray-700/50 rounded-lg px-4 py-3">
                <p className="text-xs text-gray-500 dark:text-gray-400 uppercase tracking-wider">Created</p>
                <p className="mt-1 text-sm font-semibold text-gray-900 dark:text-white">
                  {formatDate(selectedProfile.created_at)}
                </p>
              </div>
            </div>
          </div>
        </div>

        {/* Profile Detail Loading */}
        {isProfileDetailLoading && (
          <div className="flex items-center justify-center py-12">
            <LoadingSpinner size="lg" message="Loading profile details..." />
          </div>
        )}

        {/* Profile Detail Error */}
        {profileDetailError && !isProfileDetailLoading && (
          <div className="bg-red-50 dark:bg-red-900/20 border border-red-200 dark:border-red-800 rounded-lg p-4">
            <div className="flex items-center gap-2">
              <svg className="w-5 h-5 text-red-500" fill="none" viewBox="0 0 24 24" strokeWidth={2} stroke="currentColor" aria-hidden="true">
                <path strokeLinecap="round" strokeLinejoin="round" d="M12 9v3.75m9-.75a9 9 0 11-18 0 9 9 0 0118 0zm-9 3.75h.008v.008H12v-.008z" />
              </svg>
              <p className="text-sm text-red-700 dark:text-red-300">{profileDetailError}</p>
            </div>
          </div>
        )}

        {/* Table Selector Tabs */}
        {!isProfileDetailLoading && !profileDetailError && selectedProfile.tables.length > 0 && (
          <div className="bg-white dark:bg-gray-800 rounded-lg shadow-sm border border-gray-200 dark:border-gray-700">
            {/* Tab Bar */}
            <div className="border-b border-gray-200 dark:border-gray-700 overflow-x-auto">
              <nav className="flex -mb-px px-4" aria-label="Profile tables">
                {selectedProfile.tables.map((table: TableProfile) => {
                  const isActive = table.table_name === selectedTable;
                  return (
                    <button
                      key={table.table_name}
                      type="button"
                      onClick={() => handleTableSelect(table.table_name)}
                      className={`
                        shrink-0 px-4 py-3 text-sm font-medium border-b-2 transition-colors whitespace-nowrap
                        ${
                          isActive
                            ? 'border-indigo-500 text-indigo-600 dark:text-indigo-400'
                            : 'border-transparent text-gray-500 hover:text-gray-700 hover:border-gray-300 dark:text-gray-400 dark:hover:text-gray-300'
                        }
                      `}
                      aria-selected={isActive}
                      role="tab"
                    >
                      {table.table_name}
                      <span className="ml-2 text-xs text-gray-400 dark:text-gray-500">
                        ({table.columns.length} cols)
                      </span>
                    </button>
                  );
                })}
              </nav>
            </div>

            {/* View Mode Toggle + Table Metadata */}
            <div className="px-6 pt-4 pb-2 flex flex-col sm:flex-row sm:items-center sm:justify-between gap-3">
              <div className="flex items-center gap-4 text-sm text-gray-600 dark:text-gray-400">
                {activeTableProfile && (
                  <>
                    <span>
                      <strong className="text-gray-900 dark:text-white">{activeTableProfile.table_name}</strong>
                    </span>
                    <span>
                      Rows: <strong className="text-gray-900 dark:text-white">{activeTableProfile.row_count.toLocaleString()}</strong>
                    </span>
                    <span>
                      Columns: <strong className="text-gray-900 dark:text-white">{activeColumns.length}</strong>
                    </span>
                    <span>
                      Profiled: <strong className="text-gray-900 dark:text-white">
                        {activeColumns.filter((col: ColumnProfile) => col.statistics !== null && col.statistics !== undefined).length}
                      </strong>
                    </span>
                  </>
                )}
              </div>

              {/* View Toggle Buttons */}
              <div className="flex items-center gap-1 bg-gray-100 dark:bg-gray-700 rounded-lg p-0.5">
                <button
                  type="button"
                  onClick={() => setViewMode('chart')}
                  className={`
                    px-3 py-1.5 text-xs font-medium rounded-md transition-colors
                    ${
                      viewMode === 'chart'
                        ? 'bg-white dark:bg-gray-600 text-gray-900 dark:text-white shadow-sm'
                        : 'text-gray-600 dark:text-gray-400 hover:text-gray-900 dark:hover:text-white'
                    }
                  `}
                  aria-pressed={viewMode === 'chart'}
                >
                  Charts
                </button>
                <button
                  type="button"
                  onClick={() => setViewMode('table')}
                  className={`
                    px-3 py-1.5 text-xs font-medium rounded-md transition-colors
                    ${
                      viewMode === 'table'
                        ? 'bg-white dark:bg-gray-600 text-gray-900 dark:text-white shadow-sm'
                        : 'text-gray-600 dark:text-gray-400 hover:text-gray-900 dark:hover:text-white'
                    }
                  `}
                  aria-pressed={viewMode === 'table'}
                >
                  Table
                </button>
              </div>
            </div>

            {/* Table Detail Loading */}
            {isTableDetailLoading && (
              <div className="flex items-center justify-center py-12 px-6">
                <LoadingSpinner size="lg" message="Loading table statistics..." />
              </div>
            )}

            {/* Table Detail Error */}
            {tableDetailError && !isTableDetailLoading && (
              <div className="px-6 pb-4">
                <div className="bg-red-50 dark:bg-red-900/20 border border-red-200 dark:border-red-800 rounded-lg p-4">
                  <p className="text-sm text-red-700 dark:text-red-300">{tableDetailError}</p>
                </div>
              </div>
            )}

            {/* Column Statistics — Chart Grid View */}
            {!isTableDetailLoading && !tableDetailError && viewMode === 'chart' && columnStatistics.length > 0 && (
              <div className="px-6 pb-6">
                <div className="grid grid-cols-1 md:grid-cols-2 xl:grid-cols-3 gap-6 mt-4">
                  {columnStatistics.map((colStat: ColumnStatistics) => (
                    <div
                      key={colStat.column_name}
                      className="bg-gray-50 dark:bg-gray-700/50 rounded-lg border border-gray-200 dark:border-gray-600 p-4"
                    >
                      {/* Column Header */}
                      <div className="flex items-center justify-between mb-3">
                        <div>
                          <h4 className="font-mono text-sm font-semibold text-gray-900 dark:text-white">
                            {colStat.column_name}
                          </h4>
                          <span className="text-xs text-gray-500 dark:text-gray-400">
                            {colStat.data_type}
                          </span>
                        </div>
                        <span className="inline-flex px-2 py-0.5 rounded bg-indigo-100 dark:bg-indigo-900/30 text-xs font-medium text-indigo-700 dark:text-indigo-300 capitalize">
                          {colStat.distribution?.type?.replace(/_/g, ' ') ?? 'unknown'}
                        </span>
                      </div>

                      {/* Distribution Chart */}
                      <DistributionChart
                        columnStats={colStat}
                        height={200}
                        width="100%"
                        showParameters={true}
                        showFitScore={true}
                      />

                      {/* Column Statistics Summary */}
                      <div className="mt-3 grid grid-cols-2 gap-x-4 gap-y-1 text-xs">
                        <div className="flex justify-between">
                          <span className="text-gray-500 dark:text-gray-400">Mean:</span>
                          <span className="font-mono font-medium text-gray-900 dark:text-white">
                            {formatNumeric(colStat.mean)}
                          </span>
                        </div>
                        <div className="flex justify-between">
                          <span className="text-gray-500 dark:text-gray-400">Std Dev:</span>
                          <span className="font-mono font-medium text-gray-900 dark:text-white">
                            {formatNumeric(colStat.std_dev)}
                          </span>
                        </div>
                        <div className="flex justify-between">
                          <span className="text-gray-500 dark:text-gray-400">Min:</span>
                          <span className="font-mono font-medium text-gray-900 dark:text-white">
                            {formatNumeric(colStat.min_value)}
                          </span>
                        </div>
                        <div className="flex justify-between">
                          <span className="text-gray-500 dark:text-gray-400">Max:</span>
                          <span className="font-mono font-medium text-gray-900 dark:text-white">
                            {formatNumeric(colStat.max_value)}
                          </span>
                        </div>
                        <div className="flex justify-between">
                          <span className="text-gray-500 dark:text-gray-400">Null %:</span>
                          <span className="font-mono font-medium text-gray-900 dark:text-white">
                            {formatPercentage(colStat.null_percentage)}
                          </span>
                        </div>
                        <div className="flex justify-between">
                          <span className="text-gray-500 dark:text-gray-400">Unique:</span>
                          <span className="font-mono font-medium text-gray-900 dark:text-white">
                            {colStat.unique_count !== null && colStat.unique_count !== undefined
                              ? colStat.unique_count.toLocaleString()
                              : '—'}
                          </span>
                        </div>
                      </div>
                    </div>
                  ))}
                </div>
              </div>
            )}

            {/* Column Statistics — Tabular View */}
            {!isTableDetailLoading && !tableDetailError && viewMode === 'table' && columnStatistics.length > 0 && (
              <div className="px-6 pb-6 mt-2">
                <DataTable<Record<string, unknown>>
                  data={columnStatistics as unknown as Record<string, unknown>[]}
                  columns={columnStatsTableColumns}
                  keyField="column_name"
                  compact
                  bordered
                  striped
                  emptyMessage="No column statistics available for this table."
                />
              </div>
            )}

            {/* Empty State — No columns with statistics */}
            {!isTableDetailLoading && !tableDetailError && columnStatistics.length === 0 && tableDetail && (
              <div className="px-6 pb-6">
                <div className="text-center py-12">
                  <svg className="mx-auto w-12 h-12 text-gray-400 dark:text-gray-500" fill="none" viewBox="0 0 24 24" strokeWidth={1.5} stroke="currentColor" aria-hidden="true">
                    <path strokeLinecap="round" strokeLinejoin="round" d="M3.75 3v11.25A2.25 2.25 0 006 16.5h2.25M3.75 3h-1.5m1.5 0h16.5m0 0h1.5m-1.5 0v11.25A2.25 2.25 0 0118 16.5h-2.25m-7.5 0h7.5m-7.5 0l-1 3m8.5-3l1 3m0 0l.5 1.5m-.5-1.5h-9.5m0 0l-.5 1.5m.75-9l3-1.5L12 12m0 0l3-1.5M12 12l-3-1.5M12 12l3 1.5" />
                  </svg>
                  <h3 className="mt-2 text-sm font-medium text-gray-900 dark:text-white">
                    No statistical data available
                  </h3>
                  <p className="mt-1 text-sm text-gray-500 dark:text-gray-400">
                    This table has no columns with statistical profiles. The profile may have been created without the statistical analysis option enabled.
                  </p>
                </div>
              </div>
            )}
          </div>
        )}

        {/* Empty State — No tables in profile */}
        {!isProfileDetailLoading && !profileDetailError && selectedProfile.tables.length === 0 && (
          <div className="bg-white dark:bg-gray-800 rounded-lg shadow-sm border border-gray-200 dark:border-gray-700 p-8 text-center">
            <svg className="mx-auto w-12 h-12 text-gray-400 dark:text-gray-500" fill="none" viewBox="0 0 24 24" strokeWidth={1.5} stroke="currentColor" aria-hidden="true">
              <path strokeLinecap="round" strokeLinejoin="round" d="M20.25 6.375c0 2.278-3.694 4.125-8.25 4.125S3.75 8.653 3.75 6.375m16.5 0c0-2.278-3.694-4.125-8.25-4.125S3.75 4.097 3.75 6.375m16.5 0v11.25c0 2.278-3.694 4.125-8.25 4.125s-8.25-1.847-8.25-4.125V6.375m16.5 0v3.75m-16.5-3.75v3.75m16.5 0v3.75C20.25 16.153 16.556 18 12 18s-8.25-1.847-8.25-4.125v-3.75m16.5 0c0 2.278-3.694 4.125-8.25 4.125s-8.25-1.847-8.25-4.125" />
            </svg>
            <h3 className="mt-2 text-sm font-medium text-gray-900 dark:text-white">
              No tables in this profile
            </h3>
            <p className="mt-1 text-sm text-gray-500 dark:text-gray-400">
              This profile does not contain any table metadata. The profiling operation may still be in progress or the discovery scope was empty.
            </p>
          </div>
        )}
      </div>
    );
  }

  // --------------------------------------------------------------------------
  // Render — Profile List View (default view)
  // --------------------------------------------------------------------------

  return (
    <div className="space-y-6">
      {/* Page Header */}
      <div className="flex flex-col sm:flex-row sm:items-center sm:justify-between gap-4">
        <div>
          <h1 className="text-2xl font-bold text-gray-900 dark:text-white">
            Statistical Profiles
          </h1>
          <p className="mt-1 text-sm text-gray-500 dark:text-gray-400">
            Browse and explore statistical profiles captured from ERP source systems.
            Profiles contain schema metadata and distribution analysis only — no raw production data.
          </p>
        </div>
      </div>

      {/* Profiles Error */}
      {profilesError && (
        <div className="bg-red-50 dark:bg-red-900/20 border border-red-200 dark:border-red-800 rounded-lg p-4">
          <div className="flex items-center gap-2">
            <svg className="w-5 h-5 text-red-500 shrink-0" fill="none" viewBox="0 0 24 24" strokeWidth={2} stroke="currentColor" aria-hidden="true">
              <path strokeLinecap="round" strokeLinejoin="round" d="M12 9v3.75m9-.75a9 9 0 11-18 0 9 9 0 0118 0zm-9 3.75h.008v.008H12v-.008z" />
            </svg>
            <p className="text-sm text-red-700 dark:text-red-300">{profilesError}</p>
          </div>
        </div>
      )}

      {/* Filter Bar */}
      <div className="bg-white dark:bg-gray-800 rounded-lg shadow-sm border border-gray-200 dark:border-gray-700 p-4">
        <div className="flex flex-col sm:flex-row gap-4">
          {/* ERP Type Filter Dropdown */}
          <div className="sm:w-48">
            <label
              htmlFor="erp-type-filter"
              className="block text-xs font-medium text-gray-700 dark:text-gray-300 mb-1"
            >
              ERP Type
            </label>
            <select
              id="erp-type-filter"
              value={filterErpType}
              onChange={(e) => handleFilterChange(e.target.value)}
              className="block w-full rounded-md border border-gray-300 dark:border-gray-600 bg-white dark:bg-gray-700 px-3 py-2 text-sm text-gray-900 dark:text-white shadow-sm focus:border-indigo-500 focus:ring-1 focus:ring-indigo-500"
            >
              <option value="">All Types</option>
              {ERP_TYPE_OPTIONS.map(([value, label]) => (
                <option key={value} value={value}>
                  {label}
                </option>
              ))}
            </select>
          </div>
        </div>
      </div>

      {/* Profile List DataTable */}
      <div className="bg-white dark:bg-gray-800 rounded-lg shadow-sm border border-gray-200 dark:border-gray-700">
        <DataTable<Record<string, unknown>>
          data={filteredProfiles as unknown as Record<string, unknown>[]}
          columns={profileListColumns}
          keyField="profile_id"
          totalItems={profilesTotal}
          currentPage={profilesPage}
          pageSize={profilesPageSize}
          onPageChange={handlePageChange}
          searchable
          searchPlaceholder="Search profiles by ID or ERP type..."
          searchValue={searchQuery}
          onSearchChange={handleSearch}
          isLoading={isProfilesLoading}
          onRowClick={(row) => handleProfileSelect(row as unknown as StatisticalProfile)}
          emptyMessage="No statistical profiles found."
          emptyIcon={
            <div className="flex flex-col items-center gap-3 py-4">
              <svg className="w-12 h-12 text-gray-400 dark:text-gray-500" fill="none" viewBox="0 0 24 24" strokeWidth={1.5} stroke="currentColor" aria-hidden="true">
                <path strokeLinecap="round" strokeLinejoin="round" d="M3.75 3v11.25A2.25 2.25 0 006 16.5h2.25M3.75 3h-1.5m1.5 0h16.5m0 0h1.5m-1.5 0v11.25A2.25 2.25 0 0118 16.5h-2.25m-7.5 0h7.5m-7.5 0l-1 3m8.5-3l1 3m0 0l.5 1.5m-.5-1.5h-9.5m0 0l-.5 1.5m.75-9l3-1.5L12 12m0 0l3-1.5M12 12l-3-1.5M12 12l3 1.5" />
              </svg>
              <p className="text-sm text-gray-500 dark:text-gray-400">
                No profiles have been created yet. Navigate to the{' '}
                <Link
                  to="/schemas"
                  className="text-indigo-600 hover:text-indigo-800 dark:text-indigo-400 dark:hover:text-indigo-300 font-medium"
                >
                  Schema Browser
                </Link>{' '}
                to initiate profiling.
              </p>
            </div>
          }
          striped
          hoverable
        />
      </div>
    </div>
  );
}

export default ProfileViewer;
