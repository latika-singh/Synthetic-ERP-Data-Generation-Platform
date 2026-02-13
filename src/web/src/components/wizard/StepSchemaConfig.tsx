/**
 * @fileoverview Wizard Step 2: ERP Schema & Table Selection Component
 *
 * This component implements the second step of the multi-step generation wizard
 * (Screen S-002). It allows users to:
 *   1. Browse and filter discovered ERP schemas by type, module, and search query
 *   2. Select a source schema for synthetic data generation
 *   3. Choose individual tables (or select/deselect all) from the chosen schema
 *   4. Configure per-table record counts for generation
 *   5. Preserve referential integrity (FK relationships) by default
 *
 * Supported ERP types per requirements:
 *   - SAP (RFC/BAPI)
 *   - Oracle EBS (OData/JDBC)
 *   - Microsoft Dynamics (Web API/OData)
 *   - Legacy Systems (Generic JDBC)
 *
 * Supported ERP modules per Constraint C-005 (initial release):
 *   - Financial Accounting
 *   - Human Resources
 *   - Sales & Distribution
 *   - Material Management
 *
 * State management:
 *   - Schema data is fetched from the Zustand schemaStore on mount
 *   - Wizard selections are lifted to the parent GenerationWizard via props
 *   - Local filter state is managed with React useState hooks
 *
 * @module components/wizard/StepSchemaConfig
 */

import React, { useState, useCallback, useEffect, useMemo } from 'react';
import type { SchemaDefinition, ERPType, ERPModule, TableDefinition } from '@/types/schema';
import type { TableGenerationConfig } from '@/types/generation';
import { useSchemaStore } from '@/store/schemaStore';

// ---------------------------------------------------------------------------
// Constants
// ---------------------------------------------------------------------------

/**
 * ERP system type filter options.
 *
 * The `value` fields correspond to the string representations of the ERPType
 * enum defined in types/schema.ts. The 'all' sentinel disables filtering.
 */
const ERP_TYPES: ReadonlyArray<{ readonly value: string; readonly label: string }> = [
  { value: 'all', label: 'All Types' },
  { value: 'sap', label: 'SAP' },
  { value: 'oracle_ebs', label: 'Oracle EBS' },
  { value: 'dynamics', label: 'Microsoft Dynamics' },
  { value: 'legacy', label: 'Legacy Systems' },
] as const;

/**
 * ERP functional module filter options.
 *
 * Per Constraint C-005 the initial release is limited to exactly these four
 * modules. Values match the ERPModule enum in types/schema.ts.
 */
const ERP_MODULES: ReadonlyArray<{ readonly value: string; readonly label: string }> = [
  { value: 'all', label: 'All Modules' },
  { value: 'financial_accounting', label: 'Financial Accounting' },
  { value: 'hr', label: 'Human Resources' },
  { value: 'sales_distribution', label: 'Sales & Distribution' },
  { value: 'material_management', label: 'Material Management' },
] as const;

/** Default number of synthetic records to generate per table. */
const DEFAULT_RECORD_COUNT = 1000;

/** Default flag for preserving FK relationships during generation. */
const DEFAULT_PRESERVE_RELATIONSHIPS = true;

// ---------------------------------------------------------------------------
// Props Interface
// ---------------------------------------------------------------------------

/**
 * Props for the StepSchemaConfig wizard step component.
 *
 * State is lifted to the parent GenerationWizard page. The component reads
 * current selections and dispatches changes through the provided callbacks.
 */
export interface StepSchemaConfigProps {
  /** The currently selected ERP schema definition, or null if none is chosen. */
  selectedSchema: SchemaDefinition | null;

  /** Per-table generation configurations for all currently selected tables. */
  selectedTables: TableGenerationConfig[];

  /** Callback invoked when the user selects or deselects a schema. */
  onSchemaSelect: (schema: SchemaDefinition | null) => void;

  /** Callback invoked when the table selection or per-table config changes. */
  onTablesChange: (tables: TableGenerationConfig[]) => void;

  /** Advances the wizard to the next step (Parameters). */
  onNext: () => void;

  /** Returns the wizard to the previous step (Method Selection). */
  onBack: () => void;

  /** Optional additional CSS class names for the root container. */
  className?: string;
}

// ---------------------------------------------------------------------------
// Component
// ---------------------------------------------------------------------------

/**
 * StepSchemaConfig — Generation Wizard Step 2.
 *
 * Renders a two-panel layout:
 *   - **Schema Selection Panel**: Filterable grid of discovered ERP schemas
 *   - **Table Selection Panel**: Checklist of tables within the chosen schema
 *     with per-table record count inputs
 *
 * Validation rules enforced before allowing navigation to the next step:
 *   - A schema must be selected
 *   - At least one table must be checked
 *   - Every selected table must have a record count ≥ 1
 */
const StepSchemaConfig: React.FC<StepSchemaConfigProps> = ({
  selectedSchema,
  selectedTables,
  onSchemaSelect,
  onTablesChange,
  onNext,
  onBack,
  className,
}) => {
  // ---------- Store access --------------------------------------------------

  const { schemas, isSchemasLoading, schemasError, fetchSchemas } = useSchemaStore();

  // ---------- Local filter state --------------------------------------------

  const [erpTypeFilter, setErpTypeFilter] = useState<string>('all');
  const [erpModuleFilter, setErpModuleFilter] = useState<string>('all');
  const [searchQuery, setSearchQuery] = useState<string>('');
  const [tableSearch, setTableSearch] = useState<string>('');

  // ---------- Side effects --------------------------------------------------

  /**
   * Fetch the available schemas from the API on initial mount.
   * The store's fetchSchemas action handles loading/error state internally.
   */
  useEffect(() => {
    fetchSchemas();
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  // ---------- Derived / memoised values -------------------------------------

  /**
   * Schemas filtered by the current ERP type, module, and search query.
   *
   * Filtering logic:
   *   - ERP type: exact match against `schema.erp_type` (skipped when 'all')
   *   - ERP module: inclusion check against `schema.erp_modules` (skipped when 'all')
   *   - Search query: case-insensitive substring match on schema_id or erp_type
   */
  const filteredSchemas: SchemaDefinition[] = useMemo(() => {
    let result = schemas;

    if (erpTypeFilter !== 'all') {
      result = result.filter(
        (schema: SchemaDefinition) => schema.erp_type === erpTypeFilter
      );
    }

    if (erpModuleFilter !== 'all') {
      result = result.filter((schema: SchemaDefinition) =>
        schema.erp_modules.some(
          (mod: ERPModule) => (mod as string) === erpModuleFilter
        )
      );
    }

    if (searchQuery.trim().length > 0) {
      const lowerQuery = searchQuery.trim().toLowerCase();
      result = result.filter(
        (schema: SchemaDefinition) =>
          schema.schema_id.toLowerCase().includes(lowerQuery) ||
          (schema.erp_type as string).toLowerCase().includes(lowerQuery)
      );
    }

    return result;
  }, [schemas, erpTypeFilter, erpModuleFilter, searchQuery]);

  /**
   * Tables from the currently selected schema filtered by the table search query.
   * Returns an empty array when no schema is selected.
   */
  const filteredTables: TableDefinition[] = useMemo(() => {
    if (!selectedSchema) {
      return [];
    }

    if (tableSearch.trim().length === 0) {
      return selectedSchema.tables;
    }

    const lowerTableSearch = tableSearch.trim().toLowerCase();
    return selectedSchema.tables.filter((table: TableDefinition) =>
      table.name.toLowerCase().includes(lowerTableSearch)
    );
  }, [selectedSchema, tableSearch]);

  /**
   * Step validation:
   *   - A schema must be selected
   *   - At least one table must be selected
   *   - Every selected table must have a record_count > 0
   */
  const isValid: boolean = useMemo(() => {
    return (
      selectedSchema !== null &&
      selectedTables.length > 0 &&
      selectedTables.every(
        (t: TableGenerationConfig) => t.record_count > 0
      )
    );
  }, [selectedSchema, selectedTables]);

  // ---------- Event handlers ------------------------------------------------

  /**
   * Handles selection of a schema card.
   *
   * When a schema is selected:
   *   1. The parent is notified via `onSchemaSelect`
   *   2. All tables in the schema are pre-selected with default config
   *      (record_count = 1000, preserve_relationships = true)
   *   3. The table search is reset
   */
  const handleSchemaSelect = useCallback(
    (schema: SchemaDefinition): void => {
      onSchemaSelect(schema);

      const initialTables: TableGenerationConfig[] = schema.tables.map(
        (table: TableDefinition) => ({
          table_name: table.name,
          record_count: DEFAULT_RECORD_COUNT,
          preserve_relationships: DEFAULT_PRESERVE_RELATIONSHIPS,
        })
      );

      onTablesChange(initialTables);
      setTableSearch('');
    },
    [onSchemaSelect, onTablesChange]
  );

  /**
   * Toggles the selection state of a single table.
   *
   * - If the table is already selected → remove it from selectedTables
   * - If the table is not selected → add it with default config
   */
  const handleTableToggle = useCallback(
    (tableName: string): void => {
      const isCurrentlySelected = selectedTables.some(
        (t: TableGenerationConfig) => t.table_name === tableName
      );

      if (isCurrentlySelected) {
        onTablesChange(
          selectedTables.filter(
            (t: TableGenerationConfig) => t.table_name !== tableName
          )
        );
      } else {
        onTablesChange([
          ...selectedTables,
          {
            table_name: tableName,
            record_count: DEFAULT_RECORD_COUNT,
            preserve_relationships: DEFAULT_PRESERVE_RELATIONSHIPS,
          },
        ]);
      }
    },
    [selectedTables, onTablesChange]
  );

  /**
   * Selects all currently visible (filtered) tables with default configuration.
   *
   * Merges with existing selections: tables already selected retain their
   * current record_count and preserve_relationships values.
   */
  const handleSelectAllTables = useCallback((): void => {
    if (!selectedSchema) return;

    const tablesToSelect = filteredTables;
    const existingMap = new Map<string, TableGenerationConfig>(
      selectedTables.map((t: TableGenerationConfig) => [t.table_name, t])
    );

    const merged: TableGenerationConfig[] = tablesToSelect.map(
      (table: TableDefinition) => {
        const existing = existingMap.get(table.name);
        if (existing) {
          return existing;
        }
        return {
          table_name: table.name,
          record_count: DEFAULT_RECORD_COUNT,
          preserve_relationships: DEFAULT_PRESERVE_RELATIONSHIPS,
        };
      }
    );

    // Include any previously selected tables not in the current filtered view
    const filteredNames = new Set(tablesToSelect.map((t: TableDefinition) => t.name));
    const outsideFilter = selectedTables.filter(
      (t: TableGenerationConfig) => !filteredNames.has(t.table_name)
    );

    onTablesChange([...merged, ...outsideFilter]);
  }, [selectedSchema, filteredTables, selectedTables, onTablesChange]);

  /**
   * Deselects all currently visible (filtered) tables.
   *
   * Tables outside the current filter remain selected.
   */
  const handleDeselectAllTables = useCallback((): void => {
    const filteredNames = new Set(
      filteredTables.map((t: TableDefinition) => t.name)
    );

    const remaining = selectedTables.filter(
      (t: TableGenerationConfig) => !filteredNames.has(t.table_name)
    );

    onTablesChange(remaining);
  }, [filteredTables, selectedTables, onTablesChange]);

  /**
   * Updates the record count for a specific table.
   *
   * The count is clamped to a minimum of 1; no upper bound is enforced
   * since the user decides the generation volume.
   */
  const handleRecordCountChange = useCallback(
    (tableName: string, count: number): void => {
      const safeCount = Math.max(1, count);

      onTablesChange(
        selectedTables.map((t: TableGenerationConfig) =>
          t.table_name === tableName
            ? { ...t, record_count: safeCount }
            : t
        )
      );
    },
    [selectedTables, onTablesChange]
  );

  // ---------- Render --------------------------------------------------------

  return (
    <div className={`space-y-6 ${className ?? ''}`}>
      {/* ---- Header ---- */}
      <div>
        <h2 className="text-2xl font-bold text-gray-900">
          Schema &amp; Table Selection
        </h2>
        <p className="mt-1 text-sm text-gray-500">
          Select an ERP schema and choose the tables for data generation.
        </p>
      </div>

      {/* ---- Schema Selection Panel ---- */}
      <div className="bg-white rounded-lg border border-gray-200 p-6">
        <h3 className="text-lg font-semibold text-gray-900 mb-4">
          Source Schema
        </h3>

        {/* Filters Row */}
        <div className="flex flex-wrap gap-3 mb-4">
          {/* ERP Type filter */}
          <select
            value={erpTypeFilter}
            onChange={(e: React.ChangeEvent<HTMLSelectElement>) =>
              setErpTypeFilter(e.target.value)
            }
            className="rounded-lg border border-gray-300 px-3 py-2 text-sm focus:outline-none focus:ring-2 focus:ring-blue-500 focus:border-blue-500"
            aria-label="Filter by ERP type"
          >
            {ERP_TYPES.map((t) => (
              <option key={t.value} value={t.value}>
                {t.label}
              </option>
            ))}
          </select>

          {/* ERP Module filter */}
          <select
            value={erpModuleFilter}
            onChange={(e: React.ChangeEvent<HTMLSelectElement>) =>
              setErpModuleFilter(e.target.value)
            }
            className="rounded-lg border border-gray-300 px-3 py-2 text-sm focus:outline-none focus:ring-2 focus:ring-blue-500 focus:border-blue-500"
            aria-label="Filter by ERP module"
          >
            {ERP_MODULES.map((m) => (
              <option key={m.value} value={m.value}>
                {m.label}
              </option>
            ))}
          </select>

          {/* Schema search input */}
          <input
            type="text"
            value={searchQuery}
            onChange={(e: React.ChangeEvent<HTMLInputElement>) =>
              setSearchQuery(e.target.value)
            }
            placeholder="Search schemas..."
            className="flex-1 min-w-[12rem] rounded-lg border border-gray-300 px-3 py-2 text-sm focus:outline-none focus:ring-2 focus:ring-blue-500 focus:border-blue-500"
            aria-label="Search schemas"
          />
        </div>

        {/* Schema List — conditional rendering based on store state */}
        {isSchemasLoading ? (
          <div className="flex items-center justify-center py-8">
            <svg
              className="animate-spin h-6 w-6 text-blue-600"
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
                d="M4 12a8 8 0 018-8V0C5.373 0 0 5.373 0 12h4zm2 5.291A7.962 7.962 0 014 12H0c0 3.042 1.135 5.824 3 7.938l3-2.647z"
              />
            </svg>
            <span className="ml-2 text-sm text-gray-500">
              Loading schemas...
            </span>
          </div>
        ) : schemasError ? (
          <div
            className="rounded-md bg-red-50 p-4 text-sm text-red-800"
            role="alert"
          >
            <p className="font-medium">Failed to load schemas</p>
            <p className="mt-1">{schemasError}</p>
          </div>
        ) : filteredSchemas.length === 0 ? (
          <p className="text-sm text-gray-400 py-4 text-center">
            No schemas found. Discover schemas first.
          </p>
        ) : (
          <div className="grid grid-cols-1 md:grid-cols-2 gap-3 max-h-64 overflow-y-auto">
            {filteredSchemas.map((schema: SchemaDefinition) => {
              const isActive =
                selectedSchema?.schema_id === schema.schema_id;
              return (
                <button
                  key={schema.schema_id}
                  type="button"
                  onClick={() => handleSchemaSelect(schema)}
                  className={`p-4 rounded-lg border-2 text-left transition-colors ${
                    isActive
                      ? 'border-blue-500 bg-blue-50 ring-1 ring-blue-500'
                      : 'border-gray-200 hover:border-gray-300'
                  }`}
                  aria-pressed={isActive}
                  aria-label={`Select schema ${schema.schema_id}`}
                >
                  <div className="flex items-center gap-2">
                    <span className="text-xs font-medium px-2 py-0.5 bg-gray-100 rounded">
                      {(schema.erp_type as string).toUpperCase()}
                    </span>
                    <span className="text-xs text-gray-500 truncate">
                      {schema.erp_modules.join(', ')}
                    </span>
                  </div>
                  <p className="text-sm font-medium text-gray-900 mt-1">
                    {schema.total_tables} tables &middot;{' '}
                    {schema.total_relationships} relationships
                  </p>
                  <p className="text-xs text-gray-400">
                    Discovered:{' '}
                    {new Date(schema.discovered_at).toLocaleDateString()}
                  </p>
                </button>
              );
            })}
          </div>
        )}
      </div>

      {/* ---- Table Selection Panel (visible only after schema selection) ---- */}
      {selectedSchema && (
        <div className="bg-white rounded-lg border border-gray-200 p-6">
          {/* Panel header with selection count and bulk actions */}
          <div className="flex items-center justify-between mb-4">
            <h3 className="text-lg font-semibold text-gray-900">
              Tables{' '}
              <span className="text-sm font-normal text-gray-500">
                ({selectedTables.length} of {selectedSchema.tables.length}{' '}
                selected)
              </span>
            </h3>
            <div className="flex gap-2">
              <button
                type="button"
                onClick={handleSelectAllTables}
                className="text-xs text-blue-600 hover:text-blue-800 font-medium"
              >
                Select All
              </button>
              <span className="text-gray-300">|</span>
              <button
                type="button"
                onClick={handleDeselectAllTables}
                className="text-xs text-gray-500 hover:text-gray-700 font-medium"
              >
                Deselect All
              </button>
            </div>
          </div>

          {/* Table filter input */}
          <input
            type="text"
            value={tableSearch}
            onChange={(e: React.ChangeEvent<HTMLInputElement>) =>
              setTableSearch(e.target.value)
            }
            placeholder="Filter tables..."
            className="w-full rounded-lg border border-gray-300 px-3 py-2 text-sm mb-3 focus:outline-none focus:ring-2 focus:ring-blue-500 focus:border-blue-500"
            aria-label="Filter tables"
          />

          {/* Table checklist */}
          {filteredTables.length === 0 ? (
            <p className="text-sm text-gray-400 py-4 text-center">
              No tables match the current filter.
            </p>
          ) : (
            <div className="divide-y divide-gray-100 max-h-80 overflow-y-auto">
              {filteredTables.map((table: TableDefinition) => {
                const isSelected = selectedTables.some(
                  (t: TableGenerationConfig) =>
                    t.table_name === table.name
                );
                const tableConfig = selectedTables.find(
                  (t: TableGenerationConfig) =>
                    t.table_name === table.name
                );

                return (
                  <div
                    key={table.name}
                    className="flex items-center gap-3 py-3"
                  >
                    {/* Checkbox */}
                    <input
                      type="checkbox"
                      checked={isSelected}
                      onChange={() => handleTableToggle(table.name)}
                      className="h-4 w-4 rounded border-gray-300 text-blue-600 focus:ring-blue-500"
                      aria-label={`Select table ${table.name}`}
                    />

                    {/* Table metadata */}
                    <div className="flex-1 min-w-0">
                      <span className="text-sm font-medium text-gray-900">
                        {table.name}
                      </span>
                      <span className="text-xs text-gray-500 ml-2">
                        {table.columns.length} columns
                        {table.row_count != null
                          ? ` · ~${table.row_count.toLocaleString()} rows`
                          : ''}
                      </span>
                    </div>

                    {/* Record count input (shown only when table is selected) */}
                    {isSelected && (
                      <input
                        type="number"
                        value={tableConfig?.record_count ?? DEFAULT_RECORD_COUNT}
                        onChange={(
                          e: React.ChangeEvent<HTMLInputElement>
                        ) =>
                          handleRecordCountChange(
                            table.name,
                            parseInt(e.target.value, 10) || DEFAULT_RECORD_COUNT
                          )
                        }
                        min={1}
                        className="w-28 rounded border border-gray-300 px-2 py-1 text-sm text-right focus:outline-none focus:ring-2 focus:ring-blue-500 focus:border-blue-500"
                        placeholder="Records"
                        aria-label={`Record count for ${table.name}`}
                      />
                    )}
                  </div>
                );
              })}
            </div>
          )}
        </div>
      )}

      {/* ---- Navigation ---- */}
      <div className="flex justify-between pt-4 border-t border-gray-200">
        <button
          type="button"
          onClick={onBack}
          className="px-6 py-2.5 text-sm font-medium text-gray-700 bg-white border border-gray-300 rounded-lg hover:bg-gray-50 transition-colors focus:outline-none focus:ring-2 focus:ring-gray-300"
        >
          Back
        </button>
        <button
          type="button"
          onClick={onNext}
          disabled={!isValid}
          className="px-6 py-2.5 text-sm font-medium text-white bg-blue-600 rounded-lg hover:bg-blue-700 disabled:opacity-50 disabled:cursor-not-allowed transition-colors focus:outline-none focus:ring-2 focus:ring-blue-500"
        >
          Next: Parameters
        </button>
      </div>
    </div>
  );
};

// ---------------------------------------------------------------------------
// Exports
// ---------------------------------------------------------------------------

export type { StepSchemaConfigProps };
export { StepSchemaConfig };
export default StepSchemaConfig;
