/**
 * DataTable Component
 *
 * Generic, fully-typed reusable data table for the Synthetic ERP Data Generation
 * Platform Web Console. Provides server-side pagination, column sorting with
 * direction indicators, text search filtering, configurable column definitions
 * with custom renderers, checkbox-based row selection, and responsive design
 * with horizontal scroll on small screens.
 *
 * Usage contexts:
 *   - JobMonitoring page: Displays generation job lists with status columns
 *   - SchemaBrowser page: Shows ERP table/column schema definitions
 *   - TemplateLibrary page: Lists generation templates with actions
 *   - AdminPanel page: User and tenant management tables
 *   - ComplianceDashboard page: Compliance audit and certification records
 *
 * Features:
 *   - Generic TypeScript typing with <T extends Record<string, unknown>>
 *   - Server-side pagination with page/pageSize/totalItems controls
 *   - Sortable columns with ascending/descending toggle and visual indicators
 *   - Integrated search input with magnifying glass icon
 *   - Checkbox-based row selection with select-all header toggle
 *   - Dot-notation nested value access for complex data objects
 *   - Custom cell renderers and header renderers per column
 *   - Loading state with LoadingSpinner integration
 *   - Empty state with configurable icon and message
 *   - Striped, hoverable, compact, and bordered styling variants
 *   - Full dark mode support via TailwindCSS dark: variant classes
 *   - Responsive horizontal scroll container for narrow viewports
 *   - WAI-ARIA accessibility: proper table semantics, aria-labels, focus outlines
 *
 * @module components/common/DataTable
 * @version 1.0.0
 */

import React, { useCallback, useMemo } from 'react';
import type { SortDirection } from '@/types/api';
import LoadingSpinner from './LoadingSpinner';

// ============================================================================
// Inline SVG Icon Components
// ============================================================================

/**
 * SearchIcon — Magnifying glass SVG icon for the search input field.
 * Rendered at 16×16px (w-4 h-4) inside the search input's left padding area.
 *
 * @param props - Standard SVG element props including className
 * @returns SVG element rendering a magnifying glass icon
 */
function SearchIcon({ className }: { className?: string }): React.JSX.Element {
  return (
    <svg
      className={className ?? 'w-4 h-4'}
      xmlns="http://www.w3.org/2000/svg"
      fill="none"
      viewBox="0 0 24 24"
      strokeWidth={2}
      stroke="currentColor"
      aria-hidden="true"
    >
      <path
        strokeLinecap="round"
        strokeLinejoin="round"
        d="M21 21l-5.197-5.197m0 0A7.5 7.5 0 105.196 5.196a7.5 7.5 0 0010.607 10.607z"
      />
    </svg>
  );
}

/**
 * SortIcon — Up/down arrow SVG icon for column sort direction indication.
 * Displays an up arrow (asc), down arrow (desc), or neutral up-down arrows
 * depending on the current sort state.
 *
 * @param props.active - Whether this column is currently the active sort column
 * @param props.direction - Current sort direction if active; undefined if not sorted
 * @returns SVG element rendering sort direction arrows
 */
function SortIcon({
  active,
  direction,
}: {
  active: boolean;
  direction: SortDirection | undefined;
}): React.JSX.Element {
  if (active && direction === 'asc') {
    return (
      <svg
        className="w-4 h-4 text-indigo-600 dark:text-indigo-400"
        xmlns="http://www.w3.org/2000/svg"
        fill="none"
        viewBox="0 0 24 24"
        strokeWidth={2}
        stroke="currentColor"
        aria-hidden="true"
      >
        <path strokeLinecap="round" strokeLinejoin="round" d="M4.5 15.75l7.5-7.5 7.5 7.5" />
      </svg>
    );
  }

  if (active && direction === 'desc') {
    return (
      <svg
        className="w-4 h-4 text-indigo-600 dark:text-indigo-400"
        xmlns="http://www.w3.org/2000/svg"
        fill="none"
        viewBox="0 0 24 24"
        strokeWidth={2}
        stroke="currentColor"
        aria-hidden="true"
      >
        <path strokeLinecap="round" strokeLinejoin="round" d="M19.5 8.25l-7.5 7.5-7.5-7.5" />
      </svg>
    );
  }

  /* Neutral state: both up and down arrows in muted color */
  return (
    <svg
      className="w-4 h-4 text-gray-300 dark:text-gray-600"
      xmlns="http://www.w3.org/2000/svg"
      fill="none"
      viewBox="0 0 24 24"
      strokeWidth={2}
      stroke="currentColor"
      aria-hidden="true"
    >
      <path strokeLinecap="round" strokeLinejoin="round" d="M8.25 15L12 18.75 15.75 15" />
      <path strokeLinecap="round" strokeLinejoin="round" d="M8.25 9L12 5.25 15.75 9" />
    </svg>
  );
}

/**
 * ChevronLeftIcon — Left-pointing chevron SVG icon for previous page navigation.
 * @returns SVG element rendering a left chevron arrow
 */
function ChevronLeftIcon(): React.JSX.Element {
  return (
    <svg
      className="w-4 h-4"
      xmlns="http://www.w3.org/2000/svg"
      fill="none"
      viewBox="0 0 24 24"
      strokeWidth={2}
      stroke="currentColor"
      aria-hidden="true"
    >
      <path strokeLinecap="round" strokeLinejoin="round" d="M15.75 19.5L8.25 12l7.5-7.5" />
    </svg>
  );
}

/**
 * ChevronRightIcon — Right-pointing chevron SVG icon for next page navigation.
 * @returns SVG element rendering a right chevron arrow
 */
function ChevronRightIcon(): React.JSX.Element {
  return (
    <svg
      className="w-4 h-4"
      xmlns="http://www.w3.org/2000/svg"
      fill="none"
      viewBox="0 0 24 24"
      strokeWidth={2}
      stroke="currentColor"
      aria-hidden="true"
    >
      <path strokeLinecap="round" strokeLinejoin="round" d="M8.25 4.5l7.5 7.5-7.5 7.5" />
    </svg>
  );
}

// ============================================================================
// Type Definitions
// ============================================================================

/**
 * Column definition for configuring individual DataTable columns.
 *
 * Defines how each column renders its header and cell content, whether it
 * supports sorting, its width and alignment, and optional custom renderers
 * for both header and cell content.
 *
 * @typeParam T - The row data type, constrained to Record<string, unknown>
 *
 * @example
 * ```typescript
 * const columns: ColumnDefinition<GenerationJob>[] = [
 *   { key: 'job_id', header: 'Job ID', sortable: true, width: 'w-40' },
 *   { key: 'status', header: 'Status', sortable: true, align: 'center',
 *     render: (value) => <StatusBadge status={value as string} /> },
 *   { key: 'config.method', header: 'Method' }, // dot-notation access
 *   { key: 'created_at', header: 'Created', sortable: true, align: 'right',
 *     render: (value) => formatDate(value as string) },
 * ];
 * ```
 */
interface ColumnDefinition<T> {
  /** Property key in the data object. Supports dot-notation for nested access (e.g., 'config.method'). */
  key: string;

  /** Column header text displayed in the thead row. */
  header: string;

  /** Whether the column supports click-to-sort. Defaults to false. */
  sortable?: boolean;

  /** TailwindCSS width class applied to the th/td elements (e.g., 'w-40', 'w-1/4', 'min-w-[200px]'). */
  width?: string;

  /** Text alignment for both header and cell content. Defaults to 'left'. */
  align?: 'left' | 'center' | 'right';

  /** Custom cell renderer function. Receives the cell value, the full row data, and the row index. */
  render?: (value: unknown, row: T, rowIndex: number) => React.ReactNode;

  /** Custom header renderer function. Overrides the default header text display. */
  headerRender?: () => React.ReactNode;

  /** Additional CSS class names applied to each td cell in this column. */
  className?: string;
}

/**
 * Props for the DataTable component.
 *
 * Configures all aspects of the data table including data binding, pagination,
 * sorting, search filtering, row selection, loading/empty states, row click
 * interaction, and visual styling variants.
 *
 * @typeParam T - The row data type, constrained to Record<string, unknown>
 *
 * @example
 * ```tsx
 * <DataTable<GenerationJob>
 *   data={jobs}
 *   columns={jobColumns}
 *   keyField="job_id"
 *   totalItems={totalJobs}
 *   currentPage={page}
 *   pageSize={pageSize}
 *   onPageChange={setPage}
 *   onPageSizeChange={setPageSize}
 *   sortBy={sortBy}
 *   sortDirection={sortDirection}
 *   onSort={handleSort}
 *   searchable
 *   searchValue={search}
 *   onSearchChange={setSearch}
 *   isLoading={loading}
 *   onRowClick={handleRowClick}
 * />
 * ```
 */
interface DataTableProps<T extends Record<string, unknown>> {
  // -- Data ------------------------------------------------------------------

  /** Array of row data objects to display in the table body. */
  data: T[];

  /** Column configuration array defining each visible column. */
  columns: ColumnDefinition<T>[];

  /** Unique identifier field in T used as the React key for each row (e.g., 'job_id', 'schema_id'). */
  keyField: string;

  // -- Pagination (server-side) -----------------------------------------------

  /** Total number of items across all pages. When provided, enables the pagination footer. */
  totalItems?: number;

  /** Current page number (1-indexed). Defaults to 1. */
  currentPage?: number;

  /** Number of items per page. Defaults to 20. */
  pageSize?: number;

  /** Available page size options for the page size selector. Defaults to [10, 20, 50, 100]. */
  pageSizeOptions?: number[];

  /** Callback invoked when the user navigates to a different page. */
  onPageChange?: (page: number) => void;

  /** Callback invoked when the user selects a different page size. */
  onPageSizeChange?: (pageSize: number) => void;

  // -- Sorting ----------------------------------------------------------------

  /** Currently sorted column key. */
  sortBy?: string;

  /** Current sort direction for the active sort column. */
  sortDirection?: SortDirection;

  /** Callback invoked when a sortable column header is clicked. */
  onSort?: (sortBy: string, direction: SortDirection) => void;

  // -- Search / Filtering -----------------------------------------------------

  /** Whether to show the search input above the table. Defaults to false. */
  searchable?: boolean;

  /** Placeholder text for the search input. Defaults to 'Search...'. */
  searchPlaceholder?: string;

  /** Controlled value of the search input field. */
  searchValue?: string;

  /** Callback invoked when the search input value changes. */
  onSearchChange?: (value: string) => void;

  // -- Row Selection ----------------------------------------------------------

  /** Whether to show row selection checkboxes. Defaults to false. */
  selectable?: boolean;

  /** Array of selected row keyField values. */
  selectedRows?: string[];

  /** Callback invoked when the selection changes. Receives the updated array of selected keys. */
  onSelectionChange?: (selectedKeys: string[]) => void;

  // -- Row Interaction --------------------------------------------------------

  /** Callback invoked when a table row is clicked. Receives the full row data object. */
  onRowClick?: (row: T) => void;

  // -- Loading / Empty States -------------------------------------------------

  /** When true, displays a loading spinner instead of table rows. */
  isLoading?: boolean;

  /** Message displayed when the data array is empty. Defaults to 'No data found'. */
  emptyMessage?: string;

  /** Optional icon or ReactNode displayed above the empty message. */
  emptyIcon?: React.ReactNode;

  // -- Styling ----------------------------------------------------------------

  /** Additional CSS class names applied to the outermost container div. */
  className?: string;

  /** When true, reduces cell padding for a more compact layout. Defaults to false. */
  compact?: boolean;

  /** When true, applies alternating row background colors. Defaults to true. */
  striped?: boolean;

  /** When true, highlights rows on hover with a cursor pointer. Defaults to true. */
  hoverable?: boolean;

  /** When true, adds visible borders between cells. Defaults to false. */
  bordered?: boolean;
}

// ============================================================================
// Component Implementation
// ============================================================================

/**
 * DataTable — Generic reusable data table component.
 *
 * Renders a fully-featured data table with server-side pagination, sortable
 * columns, text search, row selection, and multiple styling variants. Designed
 * to be the single data table implementation shared across all pages of the
 * Synthetic ERP Data Generation Platform Web Console.
 *
 * The component is generic, accepting a type parameter T that extends
 * Record<string, unknown>, enabling full type safety for column definitions,
 * cell renderers, and row click handlers while supporting any data shape.
 *
 * @typeParam T - The row data type, constrained to Record<string, unknown>
 * @param props - DataTable configuration props
 * @returns A React JSX element rendering the complete data table
 */
function DataTable<T extends Record<string, unknown>>({
  data,
  columns,
  keyField,
  totalItems,
  currentPage,
  pageSize,
  pageSizeOptions,
  onPageChange,
  onPageSizeChange,
  sortBy,
  sortDirection,
  onSort,
  searchable = false,
  searchPlaceholder,
  searchValue,
  onSearchChange,
  selectable = false,
  selectedRows,
  onSelectionChange,
  onRowClick,
  isLoading = false,
  emptyMessage,
  emptyIcon,
  className,
  compact = false,
  striped = true,
  hoverable = true,
  bordered = false,
}: DataTableProps<T>): React.JSX.Element {
  // --------------------------------------------------------------------------
  // Computed pagination values
  // --------------------------------------------------------------------------

  /** Available page size options, defaulting to standard set */
  const pageSizes = useMemo<number[]>(
    () => pageSizeOptions ?? [10, 20, 50, 100],
    [pageSizeOptions],
  );

  /** Effective items per page with fallback default */
  const effectivePageSize = pageSize ?? 20;

  /** Total number of pages calculated from totalItems or data.length */
  const totalPages = useMemo<number>(
    () => Math.ceil((totalItems ?? data.length) / effectivePageSize) || 1,
    [totalItems, data.length, effectivePageSize],
  );

  /** Current page clamped to valid bounds [1, totalPages] */
  const currentPageClamped = useMemo<number>(
    () => Math.max(1, Math.min(currentPage ?? 1, totalPages)),
    [currentPage, totalPages],
  );

  /** Total column span including the optional selection checkbox column */
  const totalColSpan = columns.length + (selectable ? 1 : 0);

  // --------------------------------------------------------------------------
  // Utility: Nested value accessor
  // --------------------------------------------------------------------------

  /**
   * Retrieves a value from a nested object using dot-notation path.
   *
   * Supports paths like 'config.method' or 'metadata.stats.count' by
   * splitting on '.' and traversing each level. Returns undefined if any
   * intermediate segment is not an object or the key is missing.
   *
   * @param obj - The source data row object
   * @param path - Dot-notation property path (e.g., 'config.method')
   * @returns The resolved value at the path, or undefined if not found
   */
  const getNestedValue = useCallback((obj: T, path: string): unknown => {
    return path.split('.').reduce((acc: unknown, part: string) => {
      if (acc && typeof acc === 'object' && part in (acc as Record<string, unknown>)) {
        return (acc as Record<string, unknown>)[part];
      }
      return undefined;
    }, obj);
  }, []);

  // --------------------------------------------------------------------------
  // Event handlers
  // --------------------------------------------------------------------------

  /**
   * Handles a sortable column header click by toggling the sort direction.
   *
   * If the clicked column is already the active sort column with ascending
   * order, it switches to descending. Otherwise, it sets the column to
   * ascending order. Only fires if onSort callback is provided.
   *
   * @param columnKey - The key of the clicked column
   */
  const handleSort = useCallback(
    (columnKey: string): void => {
      if (!onSort) return;
      const newDirection: SortDirection =
        sortBy === columnKey && sortDirection === 'asc' ? 'desc' : 'asc';
      onSort(columnKey, newDirection);
    },
    [onSort, sortBy, sortDirection],
  );

  /**
   * Handles the "select all" checkbox toggle in the table header.
   *
   * When all currently visible rows are selected, deselects all. Otherwise,
   * selects all visible rows by extracting their keyField values.
   */
  const handleSelectAll = useCallback((): void => {
    if (!onSelectionChange) return;

    const allKeys = data.map((row) => String(getNestedValue(row, keyField)));
    const allSelected =
      selectedRows !== undefined &&
      selectedRows.length === data.length &&
      data.length > 0;

    if (allSelected) {
      /* Deselect all visible rows */
      onSelectionChange([]);
    } else {
      /* Select all visible rows */
      onSelectionChange(allKeys);
    }
  }, [data, keyField, selectedRows, onSelectionChange, getNestedValue]);

  /**
   * Handles an individual row checkbox toggle.
   *
   * Adds the row key to selectedRows if not present, or removes it if already
   * selected. Preserves existing selections from other pages.
   *
   * @param rowKey - The keyField value of the toggled row
   */
  const handleSelectRow = useCallback(
    (rowKey: string): void => {
      if (!onSelectionChange) return;

      const currentSelection = selectedRows ?? [];
      const isCurrentlySelected = currentSelection.includes(rowKey);

      if (isCurrentlySelected) {
        onSelectionChange(currentSelection.filter((key) => key !== rowKey));
      } else {
        onSelectionChange([...currentSelection, rowKey]);
      }
    },
    [selectedRows, onSelectionChange],
  );

  // --------------------------------------------------------------------------
  // Computed: "select all" checkbox state
  // --------------------------------------------------------------------------

  /** Whether all visible rows are currently selected */
  const allVisibleSelected = useMemo<boolean>(() => {
    if (!selectedRows || data.length === 0) return false;
    const allKeys = data.map((row) => String(getNestedValue(row, keyField)));
    return allKeys.every((key) => selectedRows.includes(key));
  }, [data, keyField, selectedRows, getNestedValue]);

  /** Whether some but not all visible rows are selected (indeterminate state) */
  const someVisibleSelected = useMemo<boolean>(() => {
    if (!selectedRows || data.length === 0) return false;
    const allKeys = data.map((row) => String(getNestedValue(row, keyField)));
    const selectedCount = allKeys.filter((key) => selectedRows.includes(key)).length;
    return selectedCount > 0 && selectedCount < data.length;
  }, [data, keyField, selectedRows, getNestedValue]);

  // --------------------------------------------------------------------------
  // Render: Search bar
  // --------------------------------------------------------------------------

  const renderSearchBar = (): React.ReactNode => {
    if (!searchable) return null;

    return (
      <div className="px-4 py-3 border-b border-gray-200 dark:border-gray-700">
        <div className="relative">
          <SearchIcon className="absolute left-3 top-1/2 -translate-y-1/2 w-4 h-4 text-gray-400" />
          <input
            type="text"
            value={searchValue ?? ''}
            onChange={(e: React.ChangeEvent<HTMLInputElement>) =>
              onSearchChange?.(e.target.value)
            }
            placeholder={searchPlaceholder ?? 'Search...'}
            className="w-full pl-10 pr-4 py-2 text-sm border border-gray-300 dark:border-gray-600 rounded-lg bg-white dark:bg-gray-800 text-gray-900 dark:text-white placeholder-gray-400 focus:ring-2 focus:ring-indigo-500 focus:border-indigo-500 focus:outline-none transition-colors"
            aria-label={searchPlaceholder ?? 'Search table data'}
          />
        </div>
      </div>
    );
  };

  // --------------------------------------------------------------------------
  // Render: Table header
  // --------------------------------------------------------------------------

  const renderHeader = (): React.ReactNode => (
    <thead className="text-xs text-gray-500 dark:text-gray-400 uppercase bg-gray-50 dark:bg-gray-800/50">
      <tr>
        {selectable && (
          <th className="w-10 px-4 py-3" scope="col">
            <input
              type="checkbox"
              checked={allVisibleSelected}
              ref={(el) => {
                if (el) {
                  el.indeterminate = someVisibleSelected;
                }
              }}
              onChange={handleSelectAll}
              className="w-4 h-4 rounded border-gray-300 text-indigo-600 focus:ring-indigo-500"
              aria-label="Select all rows"
            />
          </th>
        )}
        {columns.map((col) => {
          const alignClass =
            col.align === 'center'
              ? 'text-center'
              : col.align === 'right'
                ? 'text-right'
                : 'text-left';
          const sortableClass = col.sortable
            ? 'cursor-pointer select-none hover:bg-gray-100 dark:hover:bg-gray-700/50'
            : '';

          return (
            <th
              key={col.key}
              scope="col"
              className={`px-4 py-3 ${col.width ?? ''} ${alignClass} ${sortableClass} ${bordered ? 'border-x border-gray-200 dark:border-gray-700' : ''}`}
              onClick={() => col.sortable && handleSort(col.key)}
              aria-sort={
                col.sortable && sortBy === col.key
                  ? sortDirection === 'asc'
                    ? 'ascending'
                    : 'descending'
                  : col.sortable
                    ? 'none'
                    : undefined
              }
            >
              <div
                className={`flex items-center gap-1 ${col.align === 'center' ? 'justify-center' : col.align === 'right' ? 'justify-end' : ''}`}
              >
                {col.headerRender ? col.headerRender() : col.header}
                {col.sortable && (
                  <SortIcon
                    active={sortBy === col.key}
                    direction={sortBy === col.key ? sortDirection : undefined}
                  />
                )}
              </div>
            </th>
          );
        })}
      </tr>
    </thead>
  );

  // --------------------------------------------------------------------------
  // Render: Table body — loading state
  // --------------------------------------------------------------------------

  const renderLoadingBody = (): React.ReactNode => (
    <tr>
      <td colSpan={totalColSpan} className="px-4 py-12 text-center">
        <LoadingSpinner size="md" message="Loading data..." />
      </td>
    </tr>
  );

  // --------------------------------------------------------------------------
  // Render: Table body — empty state
  // --------------------------------------------------------------------------

  const renderEmptyBody = (): React.ReactNode => (
    <tr>
      <td
        colSpan={totalColSpan}
        className="px-4 py-12 text-center text-gray-500 dark:text-gray-400"
      >
        {emptyIcon && <div className="mb-2 flex justify-center">{emptyIcon}</div>}
        <p>{emptyMessage ?? 'No data found'}</p>
      </td>
    </tr>
  );

  // --------------------------------------------------------------------------
  // Render: Table body — data rows
  // --------------------------------------------------------------------------

  const renderDataRows = (): React.ReactNode =>
    data.map((row, rowIndex) => {
      const rowKey = String(getNestedValue(row, keyField));
      const isSelected = selectedRows?.includes(rowKey) ?? false;

      /* Build row class string from styling variant props */
      const rowClasses = [
        striped && rowIndex % 2 === 1 ? 'bg-gray-50/50 dark:bg-gray-800/25' : '',
        hoverable ? 'hover:bg-gray-50 dark:hover:bg-gray-800/50' : '',
        onRowClick || hoverable ? 'cursor-pointer' : '',
        isSelected ? 'bg-indigo-50 dark:bg-indigo-900/10' : '',
        bordered ? 'border-b border-gray-200 dark:border-gray-700' : '',
        'transition-colors',
      ]
        .filter(Boolean)
        .join(' ');

      return (
        <tr key={rowKey} onClick={() => onRowClick?.(row)} className={rowClasses}>
          {selectable && (
            <td
              className={`w-10 px-4 ${compact ? 'py-2' : 'py-3'}`}
              onClick={(e: React.MouseEvent) => e.stopPropagation()}
            >
              <input
                type="checkbox"
                checked={isSelected}
                onChange={() => handleSelectRow(rowKey)}
                className="w-4 h-4 rounded border-gray-300 text-indigo-600 focus:ring-indigo-500"
                aria-label={`Select row ${rowKey}`}
              />
            </td>
          )}
          {columns.map((col) => {
            const cellValue = getNestedValue(row, col.key);
            const alignClass =
              col.align === 'center'
                ? 'text-center'
                : col.align === 'right'
                  ? 'text-right'
                  : '';

            return (
              <td
                key={col.key}
                className={`px-4 ${compact ? 'py-2' : 'py-3'} text-gray-900 dark:text-gray-100 ${alignClass} ${col.className ?? ''} ${bordered ? 'border-x border-gray-200 dark:border-gray-700' : ''}`}
              >
                {col.render
                  ? col.render(cellValue, row, rowIndex)
                  : String(cellValue ?? '\u2014')}
              </td>
            );
          })}
        </tr>
      );
    });

  // --------------------------------------------------------------------------
  // Render: Pagination footer
  // --------------------------------------------------------------------------

  const renderPagination = (): React.ReactNode => {
    if (totalItems === undefined || totalItems <= 0) return null;

    /* Compute display range: start item – end item of totalItems */
    const rangeStart = (currentPageClamped - 1) * effectivePageSize + 1;
    const rangeEnd = Math.min(currentPageClamped * effectivePageSize, totalItems);

    return (
      <div className="flex flex-col sm:flex-row items-center justify-between px-4 py-3 border-t border-gray-200 dark:border-gray-700 text-sm gap-3">
        <div className="flex items-center gap-2 text-gray-500 dark:text-gray-400">
          <span>Rows per page:</span>
          <select
            value={effectivePageSize}
            onChange={(e: React.ChangeEvent<HTMLSelectElement>) =>
              onPageSizeChange?.(Number(e.target.value))
            }
            className="border border-gray-300 dark:border-gray-600 rounded px-2 py-1 text-sm bg-white dark:bg-gray-800 text-gray-900 dark:text-white focus:ring-2 focus:ring-indigo-500 focus:border-indigo-500 focus:outline-none"
            aria-label="Rows per page"
          >
            {pageSizes.map((size) => (
              <option key={size} value={size}>
                {size}
              </option>
            ))}
          </select>
          <span>
            {rangeStart}&ndash;{rangeEnd} of {totalItems}
          </span>
        </div>
        <div className="flex items-center gap-1">
          <button
            onClick={() => onPageChange?.(1)}
            disabled={currentPageClamped <= 1}
            className="p-1.5 rounded hover:bg-gray-100 dark:hover:bg-gray-800 disabled:opacity-50 disabled:cursor-not-allowed focus:outline-none focus:ring-2 focus:ring-indigo-500"
            aria-label="First page"
          >
            <svg
              className="w-4 h-4"
              xmlns="http://www.w3.org/2000/svg"
              fill="none"
              viewBox="0 0 24 24"
              strokeWidth={2}
              stroke="currentColor"
              aria-hidden="true"
            >
              <path strokeLinecap="round" strokeLinejoin="round" d="M18.75 19.5l-7.5-7.5 7.5-7.5m-6 15L5.25 12l7.5-7.5" />
            </svg>
          </button>
          <button
            onClick={() => onPageChange?.(currentPageClamped - 1)}
            disabled={currentPageClamped <= 1}
            className="p-1.5 rounded hover:bg-gray-100 dark:hover:bg-gray-800 disabled:opacity-50 disabled:cursor-not-allowed focus:outline-none focus:ring-2 focus:ring-indigo-500"
            aria-label="Previous page"
          >
            <ChevronLeftIcon />
          </button>
          <span className="px-2 text-gray-700 dark:text-gray-300">
            Page {currentPageClamped} of {totalPages}
          </span>
          <button
            onClick={() => onPageChange?.(currentPageClamped + 1)}
            disabled={currentPageClamped >= totalPages}
            className="p-1.5 rounded hover:bg-gray-100 dark:hover:bg-gray-800 disabled:opacity-50 disabled:cursor-not-allowed focus:outline-none focus:ring-2 focus:ring-indigo-500"
            aria-label="Next page"
          >
            <ChevronRightIcon />
          </button>
          <button
            onClick={() => onPageChange?.(totalPages)}
            disabled={currentPageClamped >= totalPages}
            className="p-1.5 rounded hover:bg-gray-100 dark:hover:bg-gray-800 disabled:opacity-50 disabled:cursor-not-allowed focus:outline-none focus:ring-2 focus:ring-indigo-500"
            aria-label="Last page"
          >
            <svg
              className="w-4 h-4"
              xmlns="http://www.w3.org/2000/svg"
              fill="none"
              viewBox="0 0 24 24"
              strokeWidth={2}
              stroke="currentColor"
              aria-hidden="true"
            >
              <path strokeLinecap="round" strokeLinejoin="round" d="M5.25 4.5l7.5 7.5-7.5 7.5m6-15l7.5 7.5-7.5 7.5" />
            </svg>
          </button>
        </div>
      </div>
    );
  };

  // --------------------------------------------------------------------------
  // Main render
  // --------------------------------------------------------------------------

  return (
    <div
      className={`bg-white dark:bg-gray-900 rounded-lg border border-gray-200 dark:border-gray-700 overflow-hidden ${className ?? ''}`}
    >
      {/* Search bar (if searchable) */}
      {renderSearchBar()}

      {/* Table container with horizontal scroll for responsive design */}
      <div className="overflow-x-auto">
        <table className={`w-full text-sm text-left ${bordered ? 'border-collapse' : ''}`}>
          {renderHeader()}
          <tbody className="divide-y divide-gray-200 dark:divide-gray-700">
            {isLoading
              ? renderLoadingBody()
              : data.length === 0
                ? renderEmptyBody()
                : renderDataRows()}
          </tbody>
        </table>
      </div>

      {/* Pagination footer */}
      {renderPagination()}
    </div>
  );
}

// ============================================================================
// Exports
// ============================================================================

export default DataTable;
export { DataTable };
export type { DataTableProps, ColumnDefinition };
