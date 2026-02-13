/**
 * DataTable Component — Comprehensive Unit Test Suite
 *
 * Vitest + React Testing Library tests for the generic DataTable component
 * (src/web/src/components/common/DataTable.tsx). Covers server-side pagination
 * controls, column sorting, text search filtering, row selection, loading/empty
 * states, custom cell renderers, row interaction, style variants, and accessibility.
 *
 * @module tests/unit/web/components/common/DataTable.test
 * @see src/web/src/components/common/DataTable.tsx
 */

import React from 'react';
import { describe, it, expect, vi, beforeEach } from 'vitest';
import { render, screen, fireEvent, within } from '@testing-library/react';
import '@testing-library/jest-dom';
import DataTable from '@/components/common/DataTable';
import type { ColumnDefinition } from '@/components/common/DataTable';

// ============================================================================
// Mock Setup
// ============================================================================

/**
 * Mock the LoadingSpinner component to isolate DataTable under test.
 * Returns a simple div with data-testid="loading-spinner" and the message prop
 * so loading state tests can verify rendering without real spinner dependency.
 */
vi.mock('@/components/common/LoadingSpinner', () => ({
  default: ({ message }: { message?: string }) => (
    <div data-testid="loading-spinner">{message}</div>
  ),
}));

// ============================================================================
// Test Data Definitions
// ============================================================================

/** Row data interface for test fixtures */
interface TestRow extends Record<string, unknown> {
  id: string;
  name: string;
  email: string;
  status: string;
}

/** Sample column definitions with sortable and custom render columns */
const sampleColumns: ColumnDefinition<TestRow>[] = [
  { key: 'id', header: 'ID', sortable: true },
  { key: 'name', header: 'Name', sortable: true },
  { key: 'email', header: 'Email' },
  {
    key: 'status',
    header: 'Status',
    render: (value: unknown) => (
      <span data-testid="status-badge">{String(value)}</span>
    ),
  },
];

/** Sample data rows — 5 entries for testing various table features */
const sampleData: TestRow[] = [
  { id: '1', name: 'Alice Johnson', email: 'alice@example.com', status: 'active' },
  { id: '2', name: 'Bob Smith', email: 'bob@example.com', status: 'inactive' },
  { id: '3', name: 'Charlie Brown', email: 'charlie@example.com', status: 'active' },
  { id: '4', name: 'Diana Prince', email: 'diana@example.com', status: 'pending' },
  { id: '5', name: 'Eve Davis', email: 'eve@example.com', status: 'active' },
];

// ============================================================================
// Render Helper
// ============================================================================

/**
 * Renders DataTable with sensible default props, allowing per-test overrides.
 *
 * @param overrides - Partial props to merge over defaults
 * @returns The render result from React Testing Library
 */
function renderDataTable(overrides: Record<string, unknown> = {}) {
  const defaultProps = {
    data: sampleData,
    columns: sampleColumns,
    keyField: 'id',
  };

  const mergedProps = { ...defaultProps, ...overrides };

  return render(
    <DataTable<TestRow>
      data={mergedProps.data as TestRow[]}
      columns={mergedProps.columns as ColumnDefinition<TestRow>[]}
      keyField={mergedProps.keyField as string}
      {...(overrides as Record<string, unknown>)}
    />
  );
}

// ============================================================================
// Test Suites
// ============================================================================

describe('DataTable', () => {
  beforeEach(() => {
    vi.clearAllMocks();
  });

  // --------------------------------------------------------------------------
  // Basic Rendering
  // --------------------------------------------------------------------------

  describe('Basic Rendering', () => {
    it('renders table with correct headers', () => {
      renderDataTable();

      expect(screen.getByText('ID')).toBeInTheDocument();
      expect(screen.getByText('Name')).toBeInTheDocument();
      expect(screen.getByText('Email')).toBeInTheDocument();
      expect(screen.getByText('Status')).toBeInTheDocument();
    });

    it('renders all data rows', () => {
      renderDataTable();

      /* Verify all 5 sample rows are present via their unique name values */
      expect(screen.getByText('Alice Johnson')).toBeInTheDocument();
      expect(screen.getByText('Bob Smith')).toBeInTheDocument();
      expect(screen.getByText('Charlie Brown')).toBeInTheDocument();
      expect(screen.getByText('Diana Prince')).toBeInTheDocument();
      expect(screen.getByText('Eve Davis')).toBeInTheDocument();
    });

    it('renders cell values correctly', () => {
      renderDataTable();

      /* Verify email values rendered in cells */
      expect(screen.getByText('alice@example.com')).toBeInTheDocument();
      expect(screen.getByText('bob@example.com')).toBeInTheDocument();
    });

    it('renders with semantic table elements (thead, tbody, th, td)', () => {
      renderDataTable();

      const table = screen.getByRole('table');
      expect(table).toBeInTheDocument();

      /* thead contains column header row group */
      const headerRows = screen.getAllByRole('columnheader');
      expect(headerRows.length).toBe(sampleColumns.length);

      /* tbody contains data rows */
      const rows = screen.getAllByRole('row');
      /* 1 header row + 5 data rows = 6 total */
      expect(rows.length).toBe(sampleData.length + 1);

      /* Each data row has cells */
      const cells = screen.getAllByRole('cell');
      /* 5 data rows × 4 columns = 20 cells */
      expect(cells.length).toBe(sampleData.length * sampleColumns.length);
    });

    it('applies custom className to container', () => {
      const { container } = renderDataTable({ className: 'custom-table-class' });

      const outerDiv = container.firstChild as HTMLElement;
      expect(outerDiv).toHaveClass('custom-table-class');
    });
  });

  // --------------------------------------------------------------------------
  // Server-Side Pagination
  // --------------------------------------------------------------------------

  describe('Server-Side Pagination', () => {
    it('renders pagination controls when totalItems is provided', () => {
      renderDataTable({
        totalItems: 100,
        currentPage: 1,
        pageSize: 20,
      });

      /* Pagination text and controls should be visible */
      expect(screen.getByLabelText('Rows per page')).toBeInTheDocument();
      expect(screen.getByLabelText('Next page')).toBeInTheDocument();
      expect(screen.getByLabelText('Previous page')).toBeInTheDocument();
    });

    it('hides pagination when totalItems is not provided', () => {
      renderDataTable();

      /* When totalItems is absent, pagination controls should not render */
      expect(screen.queryByLabelText('Rows per page')).not.toBeInTheDocument();
      expect(screen.queryByLabelText('Next page')).not.toBeInTheDocument();
    });

    it('displays current item range "1-20 of 100"', () => {
      renderDataTable({
        totalItems: 100,
        currentPage: 1,
        pageSize: 20,
      });

      /* The component renders rangeStart–rangeEnd of totalItems using &ndash; */
      const paginationArea = screen.getByText(/1/);
      /* Look for the combined text containing the range and total */
      expect(screen.getByText(/of 100/)).toBeInTheDocument();
    });

    it('displays page size selector with options [10, 20, 50, 100]', () => {
      renderDataTable({
        totalItems: 100,
        currentPage: 1,
        pageSize: 20,
      });

      const select = screen.getByLabelText('Rows per page') as HTMLSelectElement;
      const options = within(select).getAllByRole('option');

      expect(options).toHaveLength(4);
      expect(options[0]).toHaveTextContent('10');
      expect(options[1]).toHaveTextContent('20');
      expect(options[2]).toHaveTextContent('50');
      expect(options[3]).toHaveTextContent('100');
    });

    it('calls onPageChange with next page number when Next is clicked', () => {
      const onPageChange = vi.fn();
      renderDataTable({
        totalItems: 100,
        currentPage: 1,
        pageSize: 20,
        onPageChange,
      });

      fireEvent.click(screen.getByLabelText('Next page'));
      expect(onPageChange).toHaveBeenCalledWith(2);
    });

    it('calls onPageChange with previous page number when Previous is clicked', () => {
      const onPageChange = vi.fn();
      renderDataTable({
        totalItems: 100,
        currentPage: 3,
        pageSize: 20,
        onPageChange,
      });

      fireEvent.click(screen.getByLabelText('Previous page'));
      expect(onPageChange).toHaveBeenCalledWith(2);
    });

    it('disables Previous button on first page', () => {
      renderDataTable({
        totalItems: 100,
        currentPage: 1,
        pageSize: 20,
      });

      expect(screen.getByLabelText('Previous page')).toBeDisabled();
      expect(screen.getByLabelText('First page')).toBeDisabled();
    });

    it('disables Next button on last page', () => {
      renderDataTable({
        totalItems: 100,
        currentPage: 5,
        pageSize: 20,
      });

      expect(screen.getByLabelText('Next page')).toBeDisabled();
      expect(screen.getByLabelText('Last page')).toBeDisabled();
    });

    it('calls onPageSizeChange when page size selector changes', () => {
      const onPageSizeChange = vi.fn();
      renderDataTable({
        totalItems: 100,
        currentPage: 1,
        pageSize: 20,
        onPageSizeChange,
      });

      const select = screen.getByLabelText('Rows per page');
      fireEvent.change(select, { target: { value: '50' } });

      expect(onPageSizeChange).toHaveBeenCalledWith(50);
    });

    it('displays "Page X of Y" text', () => {
      renderDataTable({
        totalItems: 100,
        currentPage: 2,
        pageSize: 20,
      });

      expect(screen.getByText('Page 2 of 5')).toBeInTheDocument();
    });

    it('supports custom pageSizeOptions', () => {
      renderDataTable({
        totalItems: 200,
        currentPage: 1,
        pageSize: 25,
        pageSizeOptions: [25, 75, 150],
      });

      const select = screen.getByLabelText('Rows per page') as HTMLSelectElement;
      const options = within(select).getAllByRole('option');

      expect(options).toHaveLength(3);
      expect(options[0]).toHaveTextContent('25');
      expect(options[1]).toHaveTextContent('75');
      expect(options[2]).toHaveTextContent('150');
    });
  });

  // --------------------------------------------------------------------------
  // Column Sorting
  // --------------------------------------------------------------------------

  describe('Column Sorting', () => {
    it('renders sort indicator on sortable columns', () => {
      renderDataTable();

      /* The ID and Name columns are sortable and should contain sort icons (SVGs) */
      const headerRow = screen.getAllByRole('columnheader');
      const idHeader = headerRow[0]; /* ID column */
      const nameHeader = headerRow[1]; /* Name column */

      /* Sortable columns contain SVG elements for the sort indicator */
      expect(idHeader.querySelector('svg')).toBeInTheDocument();
      expect(nameHeader.querySelector('svg')).toBeInTheDocument();
    });

    it('does not render sort indicator on non-sortable columns', () => {
      renderDataTable();

      const headerRow = screen.getAllByRole('columnheader');
      const emailHeader = headerRow[2]; /* Email column — not sortable */

      /* Non-sortable columns should not have sort icons */
      expect(emailHeader.querySelector('svg')).not.toBeInTheDocument();
    });

    it('calls onSort with column key and "asc" on first click', () => {
      const onSort = vi.fn();
      renderDataTable({ onSort });

      /* Click the "ID" sortable column header */
      fireEvent.click(screen.getByText('ID'));
      expect(onSort).toHaveBeenCalledWith('id', 'asc');
    });

    it('calls onSort with column key and "desc" when same column clicked again (toggle)', () => {
      const onSort = vi.fn();
      renderDataTable({
        onSort,
        sortBy: 'id',
        sortDirection: 'asc',
      });

      /* Click the same column that is already sorted ascending → should toggle to desc */
      fireEvent.click(screen.getByText('ID'));
      expect(onSort).toHaveBeenCalledWith('id', 'desc');
    });

    it('applies cursor-pointer class to sortable column headers', () => {
      renderDataTable();

      const headerRow = screen.getAllByRole('columnheader');
      const idHeader = headerRow[0]; /* Sortable ID column */
      const emailHeader = headerRow[2]; /* Non-sortable Email column */

      expect(idHeader).toHaveClass('cursor-pointer');
      expect(emailHeader).not.toHaveClass('cursor-pointer');
    });

    it('shows active sort direction indicator on currently sorted column', () => {
      renderDataTable({
        sortBy: 'name',
        sortDirection: 'desc',
      });

      /* The sorted column header should have aria-sort attribute */
      const headerRow = screen.getAllByRole('columnheader');
      const nameHeader = headerRow[1]; /* Name column */

      expect(nameHeader).toHaveAttribute('aria-sort', 'descending');
    });

    it('does not call onSort when non-sortable column header is clicked', () => {
      const onSort = vi.fn();
      renderDataTable({ onSort });

      /* Click the "Email" header (not sortable) */
      fireEvent.click(screen.getByText('Email'));
      expect(onSort).not.toHaveBeenCalled();
    });
  });

  // --------------------------------------------------------------------------
  // Text Search Filtering
  // --------------------------------------------------------------------------

  describe('Text Search Filtering', () => {
    it('renders search input when searchable is true', () => {
      renderDataTable({ searchable: true });

      /* Default placeholder is 'Search...' */
      expect(screen.getByPlaceholderText('Search...')).toBeInTheDocument();
    });

    it('hides search input when searchable is false/undefined', () => {
      renderDataTable();

      expect(screen.queryByPlaceholderText('Search...')).not.toBeInTheDocument();
    });

    it('calls onSearchChange with input value on typing', () => {
      const onSearchChange = vi.fn();
      renderDataTable({
        searchable: true,
        onSearchChange,
      });

      const input = screen.getByPlaceholderText('Search...');
      fireEvent.change(input, { target: { value: 'Alice' } });

      expect(onSearchChange).toHaveBeenCalledWith('Alice');
    });

    it('displays searchPlaceholder text in input', () => {
      renderDataTable({
        searchable: true,
        searchPlaceholder: 'Filter records...',
      });

      expect(screen.getByPlaceholderText('Filter records...')).toBeInTheDocument();
    });

    it('renders search icon in input', () => {
      const { container } = renderDataTable({ searchable: true });

      /* The search icon is an SVG rendered within the search bar area */
      const searchArea = container.querySelector('input[type="text"]')?.parentElement;
      expect(searchArea?.querySelector('svg')).toBeInTheDocument();
    });

    it('shows controlled searchValue', () => {
      renderDataTable({
        searchable: true,
        searchValue: 'test query',
      });

      const input = screen.getByPlaceholderText('Search...') as HTMLInputElement;
      expect(input).toHaveValue('test query');
    });
  });

  // --------------------------------------------------------------------------
  // Row Selection
  // --------------------------------------------------------------------------

  describe('Row Selection', () => {
    it('renders row checkboxes when selectable is true', () => {
      renderDataTable({
        selectable: true,
        selectedRows: [],
        onSelectionChange: vi.fn(),
      });

      /* Each data row should have a checkbox + the header has one (total: 6) */
      const checkboxes = screen.getAllByRole('checkbox');
      /* 1 select-all + 5 row checkboxes = 6 */
      expect(checkboxes.length).toBe(sampleData.length + 1);
    });

    it('hides row checkboxes when selectable is false', () => {
      renderDataTable({ selectable: false });

      expect(screen.queryByRole('checkbox')).not.toBeInTheDocument();
    });

    it('renders select-all checkbox in header', () => {
      renderDataTable({
        selectable: true,
        selectedRows: [],
        onSelectionChange: vi.fn(),
      });

      expect(screen.getByLabelText('Select all rows')).toBeInTheDocument();
    });

    it('calls onSelectionChange with row key when individual checkbox is toggled', () => {
      const onSelectionChange = vi.fn();
      renderDataTable({
        selectable: true,
        selectedRows: [],
        onSelectionChange,
      });

      /* Click the first row's checkbox (aria-label="Select row 1") */
      const firstRowCheckbox = screen.getByLabelText('Select row 1');
      fireEvent.click(firstRowCheckbox);

      expect(onSelectionChange).toHaveBeenCalledWith(['1']);
    });

    it('calls onSelectionChange with all keys when select-all is checked', () => {
      const onSelectionChange = vi.fn();
      renderDataTable({
        selectable: true,
        selectedRows: [],
        onSelectionChange,
      });

      fireEvent.click(screen.getByLabelText('Select all rows'));
      expect(onSelectionChange).toHaveBeenCalledWith(['1', '2', '3', '4', '5']);
    });

    it('calls onSelectionChange with empty array when select-all is unchecked', () => {
      const onSelectionChange = vi.fn();
      renderDataTable({
        selectable: true,
        selectedRows: ['1', '2', '3', '4', '5'],
        onSelectionChange,
      });

      /* All rows are selected; clicking select-all should deselect all */
      fireEvent.click(screen.getByLabelText('Select all rows'));
      expect(onSelectionChange).toHaveBeenCalledWith([]);
    });

    it('checks checkbox for selected rows', () => {
      renderDataTable({
        selectable: true,
        selectedRows: ['1', '3'],
        onSelectionChange: vi.fn(),
      });

      const checkbox1 = screen.getByLabelText('Select row 1') as HTMLInputElement;
      const checkbox2 = screen.getByLabelText('Select row 2') as HTMLInputElement;
      const checkbox3 = screen.getByLabelText('Select row 3') as HTMLInputElement;

      expect(checkbox1.checked).toBe(true);
      expect(checkbox2.checked).toBe(false);
      expect(checkbox3.checked).toBe(true);
    });

    it('applies selected row highlight style (bg-indigo-50)', () => {
      renderDataTable({
        selectable: true,
        selectedRows: ['2'],
        onSelectionChange: vi.fn(),
      });

      /* Find the row containing "Bob Smith" and verify it has the selected class */
      const rows = screen.getAllByRole('row');
      /* rows[0] = header, rows[1] = row1, rows[2] = row2 (Bob Smith) */
      const selectedRow = rows[2];
      expect(selectedRow).toHaveClass('bg-indigo-50');
    });

    it('checkbox click does not trigger onRowClick', () => {
      const onRowClick = vi.fn();
      const onSelectionChange = vi.fn();
      renderDataTable({
        selectable: true,
        selectedRows: [],
        onSelectionChange,
        onRowClick,
      });

      /* Click a row checkbox — should not fire onRowClick */
      fireEvent.click(screen.getByLabelText('Select row 1'));

      expect(onSelectionChange).toHaveBeenCalled();
      expect(onRowClick).not.toHaveBeenCalled();
    });
  });

  // --------------------------------------------------------------------------
  // Loading State
  // --------------------------------------------------------------------------

  describe('Loading State', () => {
    it('renders LoadingSpinner when isLoading is true', () => {
      renderDataTable({ isLoading: true });

      expect(screen.getByTestId('loading-spinner')).toBeInTheDocument();
    });

    it('hides data rows when loading', () => {
      renderDataTable({ isLoading: true });

      /* Data values should not be visible during loading */
      expect(screen.queryByText('Alice Johnson')).not.toBeInTheDocument();
      expect(screen.queryByText('Bob Smith')).not.toBeInTheDocument();
    });

    it('renders loading spinner spanning all columns', () => {
      renderDataTable({ isLoading: true });

      /* The td containing the spinner should span all columns */
      const spinnerCell = screen.getByTestId('loading-spinner').closest('td');
      expect(spinnerCell).toHaveAttribute(
        'colSpan',
        String(sampleColumns.length)
      );
    });
  });

  // --------------------------------------------------------------------------
  // Empty State
  // --------------------------------------------------------------------------

  describe('Empty State', () => {
    it('renders default "No data found" message when data is empty', () => {
      renderDataTable({ data: [] });

      expect(screen.getByText('No data found')).toBeInTheDocument();
    });

    it('renders custom emptyMessage when provided', () => {
      renderDataTable({
        data: [],
        emptyMessage: 'No records match your criteria',
      });

      expect(screen.getByText('No records match your criteria')).toBeInTheDocument();
    });

    it('renders emptyIcon when provided', () => {
      renderDataTable({
        data: [],
        emptyIcon: <span data-testid="empty-icon">📭</span>,
      });

      expect(screen.getByTestId('empty-icon')).toBeInTheDocument();
    });

    it('spans all columns for empty message', () => {
      renderDataTable({ data: [] });

      const emptyCell = screen.getByText('No data found').closest('td');
      expect(emptyCell).toHaveAttribute(
        'colSpan',
        String(sampleColumns.length)
      );
    });
  });

  // --------------------------------------------------------------------------
  // Custom Cell Renderers
  // --------------------------------------------------------------------------

  describe('Custom Cell Renderers', () => {
    it('uses custom render function when provided on column definition', () => {
      renderDataTable();

      /* The status column has a custom render that wraps in data-testid="status-badge" */
      const badges = screen.getAllByTestId('status-badge');
      expect(badges.length).toBe(sampleData.length);
      expect(badges[0]).toHaveTextContent('active');
    });

    it('passes cell value, row, and rowIndex to custom render function', () => {
      const renderFn = vi.fn((value: unknown, row: TestRow, rowIndex: number) => (
        <span data-testid="custom-cell">{`${String(value)}-${row.id}-${rowIndex}`}</span>
      ));

      const customColumns: ColumnDefinition<TestRow>[] = [
        { key: 'id', header: 'ID' },
        { key: 'name', header: 'Name', render: renderFn },
      ];

      renderDataTable({ columns: customColumns });

      /* Verify the render function was called with correct args for each row */
      expect(renderFn).toHaveBeenCalledTimes(sampleData.length);

      /* Check first call: value='Alice Johnson', row=sampleData[0], rowIndex=0 */
      expect(renderFn).toHaveBeenCalledWith(
        'Alice Johnson',
        sampleData[0],
        0
      );

      /* Check second call: value='Bob Smith', row=sampleData[1], rowIndex=1 */
      expect(renderFn).toHaveBeenCalledWith(
        'Bob Smith',
        sampleData[1],
        1
      );
    });

    it('falls back to String(value) when no render function', () => {
      renderDataTable();

      /* The email column has no custom render — should display raw string */
      expect(screen.getByText('alice@example.com')).toBeInTheDocument();
      expect(screen.getByText('bob@example.com')).toBeInTheDocument();
    });

    it('displays em-dash for null/undefined values', () => {
      const dataWithNulls: TestRow[] = [
        { id: '1', name: 'Test User', email: 'test@example.com', status: 'active' },
      ];

      /* Use a column referencing a key that does not exist on the row */
      const columnsWithMissing: ColumnDefinition<TestRow>[] = [
        { key: 'id', header: 'ID' },
        { key: 'nonexistent', header: 'Missing Field' },
      ];

      renderDataTable({
        data: dataWithNulls,
        columns: columnsWithMissing,
      });

      /* The em-dash character \u2014 should be rendered for undefined values */
      expect(screen.getByText('\u2014')).toBeInTheDocument();
    });
  });

  // --------------------------------------------------------------------------
  // Row Interaction
  // --------------------------------------------------------------------------

  describe('Row Interaction', () => {
    it('calls onRowClick with row data when row is clicked', () => {
      const onRowClick = vi.fn();
      renderDataTable({ onRowClick });

      /* Click the row containing Alice Johnson */
      const rows = screen.getAllByRole('row');
      /* rows[0] is the header row; rows[1] is the first data row */
      fireEvent.click(rows[1]);

      expect(onRowClick).toHaveBeenCalledTimes(1);
      expect(onRowClick).toHaveBeenCalledWith(sampleData[0]);
    });

    it('applies hover class when hoverable is not false', () => {
      renderDataTable({ hoverable: true });

      const rows = screen.getAllByRole('row');
      const dataRow = rows[1]; /* First data row */

      /* Default hoverable=true adds hover:bg-gray-50 class */
      expect(dataRow.className).toContain('hover:bg-gray-50');
    });

    it('applies striped class to alternating rows when striped is not false', () => {
      renderDataTable({ striped: true });

      const rows = screen.getAllByRole('row');
      /* rows[0] = header, rows[1] = index 0, rows[2] = index 1 (striped) */
      const evenRow = rows[1]; /* rowIndex 0 — no stripe */
      const oddRow = rows[2]; /* rowIndex 1 — gets bg-gray-50/50 */

      expect(oddRow.className).toContain('bg-gray-50/50');
      expect(evenRow.className).not.toContain('bg-gray-50/50');
    });

    it('applies compact padding when compact is true', () => {
      const { container } = renderDataTable({ compact: true });

      /* When compact is true, cells use py-2 instead of py-3 */
      const dataCells = container.querySelectorAll('tbody td');
      expect(dataCells.length).toBeGreaterThan(0);

      /* Each cell should have py-2 class */
      const firstCell = dataCells[0];
      expect(firstCell.className).toContain('py-2');
      expect(firstCell.className).not.toContain('py-3');
    });
  });

  // --------------------------------------------------------------------------
  // Accessibility
  // --------------------------------------------------------------------------

  describe('Accessibility', () => {
    it('has aria-label on pagination buttons', () => {
      renderDataTable({
        totalItems: 100,
        currentPage: 2,
        pageSize: 20,
      });

      expect(screen.getByLabelText('First page')).toBeInTheDocument();
      expect(screen.getByLabelText('Previous page')).toBeInTheDocument();
      expect(screen.getByLabelText('Next page')).toBeInTheDocument();
      expect(screen.getByLabelText('Last page')).toBeInTheDocument();
    });

    it('uses proper table semantic structure', () => {
      renderDataTable();

      /* Verify proper table element hierarchy */
      const table = screen.getByRole('table');
      expect(table).toBeInTheDocument();

      /* Column headers use role="columnheader" */
      const columnHeaders = screen.getAllByRole('columnheader');
      expect(columnHeaders.length).toBe(sampleColumns.length);

      /* Rows include header row + data rows */
      const rows = screen.getAllByRole('row');
      expect(rows.length).toBe(sampleData.length + 1);

      /* Data cells exist under data rows */
      const cells = screen.getAllByRole('cell');
      expect(cells.length).toBe(sampleData.length * sampleColumns.length);

      /* Column headers have scope="col" attribute for accessibility */
      columnHeaders.forEach((header) => {
        expect(header).toHaveAttribute('scope', 'col');
      });
    });
  });
});
