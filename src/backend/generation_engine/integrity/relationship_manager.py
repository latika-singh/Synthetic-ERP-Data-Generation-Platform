"""Foreign key relationship tracking and enforcement for synthetic data generation.

This module is the referential-integrity backbone of the Generation Engine.
During batch generation it maintains a live registry of every parent–child
foreign-key relationship discovered from ERP schema metadata, tracks all
primary key values produced so far, and resolves valid foreign-key values
for child tables on demand.

Core capabilities:

* **FK relationship registry** — Stores ``ForeignKeyRelationship`` Pydantic
  models keyed by a unique ``relationship_id``.  Relationships can be
  registered individually or bulk-loaded from a schema definition dict.
* **Generated key tracking** — ``GeneratedKeyMapping`` dataclasses cache
  every primary-key value emitted during generation together with a reverse
  lookup index for O(1) existence checks.
* **Foreign-key resolution** — Four pluggable selection strategies
  (``random``, ``sequential``, ``weighted``, ``uniform``) pick valid FK
  values from parent key pools.  Nullable FK columns are handled via a
  configurable null ratio.
* **Cross-module reference resolution** — Dedicated logic for FK
  relationships that span ERP modules (e.g. HR payroll → Financial
  Accounting GL accounts).
* **Batch & dataset validation** — ``validate_batch()`` checks a single
  table's batch; ``validate_complete_dataset()`` scores the entire
  generated output.  The resulting *referential integrity score* feeds
  directly into the 30 % weight of the quality scoring model
  (``Q = 0.4 * statistical + 0.3 * business_rules + 0.3 * referential``).
* **Thread safety** — A ``threading.RLock`` protects key allocation and
  registration so multiple Gunicorn worker threads can generate records
  concurrently.

This module operates exclusively on **schema metadata** — no production
data is accessed or stored, in compliance with Constraint C-001.

Usage::

    from generation_engine.integrity.relationship_manager import (
        RelationshipManager,
        ForeignKeyRelationship,
    )

    manager = RelationshipManager(dependency_graph=graph)
    manager.register_relationships_from_schema(schema_def)
    keys = manager.allocate_primary_key("sap.fi.gl_accounts", "account_id", count=100)
    fk_vals = manager.resolve_foreign_key(rel_id, count=50, strategy="random")
    score, violations = manager.validate_complete_dataset(dataset)
"""

from __future__ import annotations

import hashlib
from collections import defaultdict
from dataclasses import dataclass, field
from threading import RLock
from typing import Any, Dict, List, Optional, Set, Tuple

import numpy as np
from pydantic import BaseModel, Field

from generation_engine.integrity.dependency_graph import DependencyGraph, TableNode
from shared.logging.structured_logger import get_logger

# ---------------------------------------------------------------------------
# ERP Module Constants
# ---------------------------------------------------------------------------

ERP_MODULE_FINANCIAL_ACCOUNTING: str = "financial_accounting"
"""Canonical identifier for the Financial Accounting ERP module (GL, AP, AR)."""

ERP_MODULE_HR: str = "hr"
"""Canonical identifier for the Human Resources ERP module (payroll, benefits)."""

ERP_MODULE_SALES_DISTRIBUTION: str = "sales_distribution"
"""Canonical identifier for the Sales & Distribution ERP module (orders, pricing)."""

ERP_MODULE_MATERIAL_MANAGEMENT: str = "material_management"
"""Canonical identifier for the Material Management ERP module (inventory, PO)."""

_VALID_ERP_MODULES: frozenset[str] = frozenset({
    ERP_MODULE_FINANCIAL_ACCOUNTING,
    ERP_MODULE_HR,
    ERP_MODULE_SALES_DISTRIBUTION,
    ERP_MODULE_MATERIAL_MANAGEMENT,
})

# ---------------------------------------------------------------------------
# Module Table Prefix Mapping
# ---------------------------------------------------------------------------

MODULE_TABLE_PREFIXES: Dict[str, Dict[str, str]] = {
    "sap": {
        "fi": ERP_MODULE_FINANCIAL_ACCOUNTING,
        "co": ERP_MODULE_FINANCIAL_ACCOUNTING,
        "hr": ERP_MODULE_HR,
        "pa": ERP_MODULE_HR,
        "sd": ERP_MODULE_SALES_DISTRIBUTION,
        "mm": ERP_MODULE_MATERIAL_MANAGEMENT,
    },
    "oracle": {
        "gl": ERP_MODULE_FINANCIAL_ACCOUNTING,
        "ap": ERP_MODULE_FINANCIAL_ACCOUNTING,
        "ar": ERP_MODULE_FINANCIAL_ACCOUNTING,
        "fa": ERP_MODULE_FINANCIAL_ACCOUNTING,
        "hr": ERP_MODULE_HR,
        "per": ERP_MODULE_HR,
        "pay": ERP_MODULE_HR,
        "oe": ERP_MODULE_SALES_DISTRIBUTION,
        "ra": ERP_MODULE_SALES_DISTRIBUTION,
        "po": ERP_MODULE_MATERIAL_MANAGEMENT,
        "inv": ERP_MODULE_MATERIAL_MANAGEMENT,
        "rcv": ERP_MODULE_MATERIAL_MANAGEMENT,
    },
    "dynamics": {
        "ledger": ERP_MODULE_FINANCIAL_ACCOUNTING,
        "gl": ERP_MODULE_FINANCIAL_ACCOUNTING,
        "finance": ERP_MODULE_FINANCIAL_ACCOUNTING,
        "hcm": ERP_MODULE_HR,
        "payroll": ERP_MODULE_HR,
        "workforce": ERP_MODULE_HR,
        "sales": ERP_MODULE_SALES_DISTRIBUTION,
        "customer": ERP_MODULE_SALES_DISTRIBUTION,
        "order": ERP_MODULE_SALES_DISTRIBUTION,
        "procurement": ERP_MODULE_MATERIAL_MANAGEMENT,
        "inventory": ERP_MODULE_MATERIAL_MANAGEMENT,
        "warehouse": ERP_MODULE_MATERIAL_MANAGEMENT,
    },
}
"""Maps ``{erp_system: {table_prefix: erp_module}}`` for auto-detection."""

# ---------------------------------------------------------------------------
# Cross-Module Relationship Definitions
# ---------------------------------------------------------------------------

CROSS_MODULE_RELATIONSHIPS: List[Dict[str, str]] = [
    # HR → Financial Accounting: payroll entries reference GL accounts
    {
        "source_module": ERP_MODULE_HR,
        "target_module": ERP_MODULE_FINANCIAL_ACCOUNTING,
        "description": "Payroll entries reference GL account codes and cost centers",
        "typical_parent_table_pattern": "gl_accounts",
        "typical_child_table_pattern": "payroll_entries",
        "typical_fk_column": "gl_account_id",
    },
    {
        "source_module": ERP_MODULE_HR,
        "target_module": ERP_MODULE_FINANCIAL_ACCOUNTING,
        "description": "Employee cost allocations reference cost centers",
        "typical_parent_table_pattern": "cost_centers",
        "typical_child_table_pattern": "employee_costs",
        "typical_fk_column": "cost_center_id",
    },
    # Sales & Distribution → Material Management: orders reference materials
    {
        "source_module": ERP_MODULE_SALES_DISTRIBUTION,
        "target_module": ERP_MODULE_MATERIAL_MANAGEMENT,
        "description": "Sales order line items reference material/inventory records",
        "typical_parent_table_pattern": "materials",
        "typical_child_table_pattern": "sales_order_items",
        "typical_fk_column": "material_id",
    },
    # Sales & Distribution → Financial Accounting: invoices reference AR accounts
    {
        "source_module": ERP_MODULE_SALES_DISTRIBUTION,
        "target_module": ERP_MODULE_FINANCIAL_ACCOUNTING,
        "description": "Sales invoices reference accounts receivable entries",
        "typical_parent_table_pattern": "ar_accounts",
        "typical_child_table_pattern": "sales_invoices",
        "typical_fk_column": "ar_account_id",
    },
    # Material Management → Financial Accounting: POs reference AP accounts
    {
        "source_module": ERP_MODULE_MATERIAL_MANAGEMENT,
        "target_module": ERP_MODULE_FINANCIAL_ACCOUNTING,
        "description": "Purchase orders reference accounts payable entries",
        "typical_parent_table_pattern": "ap_accounts",
        "typical_child_table_pattern": "purchase_orders",
        "typical_fk_column": "ap_account_id",
    },
]
"""Known cross-module FK patterns used as templates for auto-detection."""


# ---------------------------------------------------------------------------
# Pydantic model — ForeignKeyRelationship
# ---------------------------------------------------------------------------


class ForeignKeyRelationship(BaseModel):
    """Describes a single foreign-key relationship between two tables.

    Attributes:
        relationship_id: Unique identifier for this FK relationship.
        parent_table: Fully qualified parent table name
            (e.g. ``'sap.fi.gl_accounts'``).
        parent_column: Primary key column in the parent table.
        child_table: Fully qualified child table name.
        child_column: Foreign key column in the child table.
        erp_module: ERP module that owns this relationship.
        cardinality: Relationship cardinality — ``'one_to_one'``,
            ``'one_to_many'``, or ``'many_to_many'``.
        is_nullable: ``True`` if the FK column allows ``NULL`` values.
        cascade_type: Cascade behaviour on delete/update —
            ``'restrict'``, ``'cascade'``, or ``'set_null'``.
        cross_module: ``True`` if the relationship spans ERP module
            boundaries.
    """

    relationship_id: str
    parent_table: str
    parent_column: str
    child_table: str
    child_column: str
    erp_module: str = ""
    cardinality: str = "one_to_many"
    is_nullable: bool = Field(default=False)
    cascade_type: str = Field(default="restrict")
    cross_module: bool = Field(default=False)


# ---------------------------------------------------------------------------
# Dataclass — GeneratedKeyMapping
# ---------------------------------------------------------------------------


@dataclass
class GeneratedKeyMapping:
    """Tracks all generated primary-key values for a single table column.

    Attributes:
        table_name: Table that owns the generated keys.
        column_name: Column name for the key.
        generated_keys: Ordered list of generated PK values.
        key_index: Reverse-lookup mapping from key value to its position
            in ``generated_keys``.
        next_key_value: Next sequential auto-increment value.
    """

    table_name: str
    column_name: str
    generated_keys: list[Any] = field(default_factory=list)
    key_index: dict[Any, int] = field(default_factory=dict)
    next_key_value: int = 1


# ---------------------------------------------------------------------------
# Dataclass — IntegrityViolation
# ---------------------------------------------------------------------------


@dataclass
class IntegrityViolation:
    """Represents a single referential-integrity violation found during
    validation.

    Attributes:
        violation_type: Category — ``'orphaned_fk'``, ``'duplicate_pk'``,
            ``'null_required_fk'``, ``'cardinality_violation'``, or
            ``'cross_module_mismatch'``.
        table_name: Table where the violation was detected.
        column_name: Column with the violation.
        record_index: Row index of the violating record.
        expected_value: Expected value or range.
        actual_value: Actual value found.
        relationship_id: Identifier of the FK relationship involved.
        severity: ``'error'`` or ``'warning'``.
        message: Human-readable description.
    """

    violation_type: str
    table_name: str
    column_name: str
    record_index: int
    expected_value: Any
    actual_value: Any
    relationship_id: str
    severity: str = "error"
    message: str = ""


# ---------------------------------------------------------------------------
# RelationshipManager
# ---------------------------------------------------------------------------


class RelationshipManager:
    """Central orchestrator for FK relationship tracking and enforcement.

    Maintains a live registry of foreign-key relationships parsed from ERP
    schema definitions, tracks every primary-key value produced during
    generation, resolves valid FK values for child tables, and validates
    referential integrity across the entire generated dataset.

    Thread-safety:
        All key-mutation methods (``allocate_primary_key``,
        ``register_generated_keys``) are guarded by a reentrant lock so
        that multiple Gunicorn worker threads can generate records
        concurrently without data races.

    Args:
        dependency_graph: Optional :class:`DependencyGraph` instance used
            for topological ordering and cycle detection.  If ``None``, a
            fresh empty graph is created.

    Example::

        manager = RelationshipManager(dependency_graph=graph)
        manager.register_relationships_from_schema(schema_def)
        order = manager.get_generation_order()

        for table in order:
            pks = manager.allocate_primary_key(table, "id", count=10_000)
            # ... generate records using pks ...
            fk_vals = manager.resolve_foreign_key(rel_id, count=10_000)
            # ... assign FK values to generated records ...

        score, violations = manager.validate_complete_dataset(dataset)
    """

    # ------------------------------------------------------------------
    # Construction
    # ------------------------------------------------------------------

    def __init__(self, dependency_graph: DependencyGraph | None = None) -> None:
        """Initialise the RelationshipManager.

        Args:
            dependency_graph: An existing :class:`DependencyGraph` to use
                for ordering and cycle validation.  A new empty instance
                is created when ``None``.
        """
        self._relationships: Dict[str, ForeignKeyRelationship] = {}
        self._key_mappings: Dict[str, GeneratedKeyMapping] = {}
        self._fk_value_pools: Dict[str, list] = defaultdict(list)
        self._cross_module_refs: Dict[str, Dict[str, list]] = defaultdict(
            lambda: defaultdict(list),
        )
        self._violations: list[IntegrityViolation] = []
        self._dependency_graph: DependencyGraph = (
            dependency_graph if dependency_graph is not None else DependencyGraph()
        )
        self._erp_module_registry: Dict[str, list[str]] = defaultdict(list)
        self._lock: RLock = RLock()
        self._sequential_counters: Dict[str, int] = defaultdict(int)
        self.logger = get_logger(__name__)

    # ------------------------------------------------------------------
    # Relationship Registration
    # ------------------------------------------------------------------

    def register_relationship(self, relationship: ForeignKeyRelationship) -> None:
        """Add a foreign-key relationship to the internal registry.

        If the relationship is cross-module it is additionally registered
        in the cross-module reference map.  The dependency graph is updated
        with the corresponding edge so that topological ordering remains
        correct.

        Args:
            relationship: The :class:`ForeignKeyRelationship` to register.

        Raises:
            ValueError: If a relationship with the same
                ``relationship_id`` is already registered with different
                endpoints.
        """
        rel_id = relationship.relationship_id

        # Duplicate-check: same ID must reference the same tables.
        if rel_id in self._relationships:
            existing = self._relationships[rel_id]
            if (
                existing.parent_table != relationship.parent_table
                or existing.child_table != relationship.child_table
            ):
                raise ValueError(
                    f"Relationship '{rel_id}' already registered with "
                    f"different endpoints: "
                    f"{existing.parent_table}->{existing.child_table} vs "
                    f"{relationship.parent_table}->{relationship.child_table}",
                )
            # Idempotent re-registration — silently accept.
            return

        self._relationships[rel_id] = relationship

        # Register in cross-module map when appropriate.
        if relationship.cross_module:
            target_key = (
                f"{relationship.parent_table}.{relationship.parent_column}"
            )
            self._cross_module_refs[relationship.erp_module][target_key] = []

        # Register ERP module associations.
        if relationship.erp_module:
            if relationship.child_table not in self._erp_module_registry.get(
                relationship.erp_module, [],
            ):
                self._erp_module_registry[relationship.erp_module].append(
                    relationship.child_table,
                )
            if relationship.parent_table not in self._erp_module_registry.get(
                relationship.erp_module, [],
            ):
                self._erp_module_registry[relationship.erp_module].append(
                    relationship.parent_table,
                )

        # Synchronise dependency graph — ensure both table nodes exist.
        try:
            self._dependency_graph.get_dependencies(relationship.parent_table)
        except KeyError:
            self._dependency_graph.add_table(
                TableNode(
                    table_name=relationship.parent_table,
                    erp_module=relationship.erp_module,
                ),
            )
        try:
            self._dependency_graph.get_dependencies(relationship.child_table)
        except KeyError:
            self._dependency_graph.add_table(
                TableNode(
                    table_name=relationship.child_table,
                    erp_module=relationship.erp_module,
                ),
            )

        # Import DependencyEdge lazily to avoid circular import at module load.
        from generation_engine.integrity.dependency_graph import DependencyEdge

        try:
            self._dependency_graph.add_dependency(
                DependencyEdge(
                    parent_table=relationship.parent_table,
                    child_table=relationship.child_table,
                    fk_column=relationship.child_column,
                    pk_column=relationship.parent_column,
                    is_nullable=relationship.is_nullable,
                    is_cross_module=relationship.cross_module,
                ),
            )
        except ValueError:
            # Edge may already exist if graph was pre-populated.
            pass

        # Validate that no circular FK references were introduced.
        cycles = self._dependency_graph.detect_cycles()
        if cycles:
            self.logger.warning(
                "circular_fk_detected_after_registration",
                relationship_id=rel_id,
                cycles=[c[:5] for c in cycles],
            )

        self.logger.info(
            "relationship_registered",
            relationship_id=rel_id,
            parent=relationship.parent_table,
            child=relationship.child_table,
            cross_module=relationship.cross_module,
            cardinality=relationship.cardinality,
        )

    def register_relationships_from_schema(
        self,
        schema_definition: dict,
    ) -> int:
        """Bulk-register FK relationships from a schema definition dict.

        The *schema_definition* follows the same structure used by
        :meth:`DependencyGraph.build_from_schema`::

            {
                "erp_system": "sap",
                "tables": [ { "name": "...", "columns": [...], ... }, ... ],
                "foreign_keys": [
                    {
                        "parent_table": "...",
                        "child_table": "...",
                        "fk_column": "...",
                        "pk_column": "...",
                        "is_nullable": false
                    },
                    ...
                ]
            }

        Args:
            schema_definition: Dictionary conforming to the schema above.

        Returns:
            The number of relationships successfully registered.
        """
        erp_system: str = schema_definition.get("erp_system", "legacy").lower()
        tables: list[dict[str, Any]] = schema_definition.get("tables", [])
        foreign_keys: list[dict[str, Any]] = schema_definition.get(
            "foreign_keys", [],
        )

        if not tables and not foreign_keys:
            self.logger.warning(
                "schema_import_empty",
                erp_system=erp_system,
            )
            return 0

        # Build a quick lookup of table names to detect ERP module.
        table_module_map: Dict[str, str] = {}
        for tbl in tables:
            tbl_name: str = tbl.get("name", "")
            if not tbl_name:
                continue
            erp_module = tbl.get("erp_module", "")
            if not erp_module:
                erp_module = self._detect_erp_module(tbl_name, erp_system)
            table_module_map[tbl_name] = erp_module

            # Ensure the table node exists in the dependency graph.
            try:
                self._dependency_graph.get_dependencies(tbl_name)
            except KeyError:
                columns: list[dict[str, Any]] = tbl.get("columns", [])
                primary_keys: list[str] = tbl.get("primary_keys", [])
                if not primary_keys:
                    primary_keys = [
                        col["name"]
                        for col in columns
                        if col.get("is_pk", False)
                    ]
                self._dependency_graph.add_table(
                    TableNode(
                        table_name=tbl_name,
                        schema_name=tbl.get("schema", ""),
                        erp_module=erp_module,
                        erp_system=erp_system,
                        columns=columns,
                        primary_keys=primary_keys,
                    ),
                )

        registered_count: int = 0

        for fk in foreign_keys:
            parent_table: str = fk.get("parent_table", "")
            child_table: str = fk.get("child_table", "")
            fk_column: str = fk.get("fk_column", "")
            pk_column: str = fk.get("pk_column", "")

            if not parent_table or not child_table:
                self.logger.warning(
                    "fk_missing_tables",
                    parent=parent_table,
                    child=child_table,
                )
                continue

            parent_module = table_module_map.get(parent_table, "")
            child_module = table_module_map.get(child_table, "")
            is_cross_module = bool(
                parent_module
                and child_module
                and parent_module != child_module,
            )
            erp_module = child_module or parent_module

            # Deterministic relationship ID from table + column names.
            rel_id = self._generate_relationship_id(
                parent_table, pk_column, child_table, fk_column,
            )

            cardinality = fk.get("cardinality", "one_to_many")
            is_nullable = fk.get("is_nullable", False)
            cascade_type = fk.get("cascade_type", "restrict")

            relationship = ForeignKeyRelationship.model_validate(
                {
                    "relationship_id": rel_id,
                    "parent_table": parent_table,
                    "parent_column": pk_column,
                    "child_table": child_table,
                    "child_column": fk_column,
                    "erp_module": erp_module,
                    "cardinality": cardinality,
                    "is_nullable": is_nullable,
                    "cascade_type": cascade_type,
                    "cross_module": is_cross_module,
                },
            )

            try:
                self.register_relationship(relationship)
                registered_count += 1
            except ValueError as exc:
                self.logger.error(
                    "relationship_registration_failed",
                    relationship_id=rel_id,
                    error=str(exc),
                )

        self.logger.info(
            "schema_relationships_imported",
            erp_system=erp_system,
            total_fks=len(foreign_keys),
            registered=registered_count,
        )
        return registered_count

    # ------------------------------------------------------------------
    # Key Management
    # ------------------------------------------------------------------

    def register_generated_keys(
        self,
        table_name: str,
        column_name: str,
        keys: list[Any],
    ) -> None:
        """Register a batch of generated primary-key values.

        Creates or extends the :class:`GeneratedKeyMapping` for the given
        ``table_name.column_name`` pair.  The reverse-lookup index is
        rebuilt and all FK value pools that reference this table/column as
        a parent are refreshed.

        This method is **thread-safe** (guarded by ``RLock``).

        Args:
            table_name: Fully qualified table name.
            column_name: Primary-key column name.
            keys: List of generated PK values.
        """
        if not keys:
            return

        mapping_key = f"{table_name}.{column_name}"

        with self._lock:
            if mapping_key not in self._key_mappings:
                self._key_mappings[mapping_key] = GeneratedKeyMapping(
                    table_name=table_name,
                    column_name=column_name,
                )

            mapping = self._key_mappings[mapping_key]
            start_idx = len(mapping.generated_keys)
            mapping.generated_keys.extend(keys)

            # Rebuild reverse index for new keys.
            for i, key_val in enumerate(keys):
                mapping.key_index[key_val] = start_idx + i

            # Update next_key_value if numeric keys are being tracked.
            if keys and isinstance(keys[-1], (int, float)):
                candidate = int(keys[-1]) + 1
                if candidate > mapping.next_key_value:
                    mapping.next_key_value = candidate

        # Refresh FK value pools referencing this parent table/column.
        for rel_id, rel in self._relationships.items():
            if rel.parent_table == table_name and rel.parent_column == column_name:
                self._build_fk_value_pool(rel_id)

        self.logger.debug(
            "keys_registered",
            table=table_name,
            column=column_name,
            count=len(keys),
            total=len(self._key_mappings[mapping_key].generated_keys),
        )

    def allocate_primary_key(
        self,
        table_name: str,
        column_name: str,
        count: int = 1,
    ) -> list[Any]:
        """Allocate sequential auto-increment primary-key values.

        Generates *count* new integer PK values starting from the current
        ``next_key_value`` of the mapping, registers them, and returns the
        list.

        This method is **thread-safe** (guarded by ``RLock``).

        Args:
            table_name: Fully qualified table name.
            column_name: Primary-key column name.
            count: Number of keys to allocate (default ``1``).

        Returns:
            List of allocated integer key values.
        """
        if count < 1:
            return []

        mapping_key = f"{table_name}.{column_name}"

        with self._lock:
            if mapping_key not in self._key_mappings:
                self._key_mappings[mapping_key] = GeneratedKeyMapping(
                    table_name=table_name,
                    column_name=column_name,
                )

            mapping = self._key_mappings[mapping_key]
            start = mapping.next_key_value
            new_keys = list(range(start, start + count))
            mapping.next_key_value = start + count

            # Register the new keys (re-entrant lock allows nested call).
            self.register_generated_keys(table_name, column_name, new_keys)

        self.logger.debug(
            "primary_keys_allocated",
            table=table_name,
            column=column_name,
            count=count,
            range_start=start,
            range_end=start + count - 1,
        )
        return new_keys

    # ------------------------------------------------------------------
    # Foreign-Key Resolution
    # ------------------------------------------------------------------

    def resolve_foreign_key(
        self,
        relationship_id: str,
        count: int = 1,
        strategy: str = "random",
        null_ratio: float = 0.0,
    ) -> list[Any]:
        """Select valid foreign-key values from the parent key pool.

        Picks *count* FK values according to the chosen *strategy*.
        Nullable FK columns can include ``None`` values controlled by
        *null_ratio*.

        Args:
            relationship_id: Identifier of the FK relationship.
            count: Number of FK values to produce.
            strategy: Selection strategy — ``'random'``, ``'sequential'``,
                ``'weighted'``, or ``'uniform'``.
            null_ratio: Fraction of values to set to ``None`` for nullable
                FK columns (``0.0``–``1.0``).

        Returns:
            List of FK values (possibly including ``None`` for nullable
            columns).

        Raises:
            KeyError: If the *relationship_id* is not registered.
            ValueError: If the parent key pool is empty and the FK is
                non-nullable.
        """
        if relationship_id not in self._relationships:
            raise KeyError(
                f"Relationship '{relationship_id}' not found in registry.",
            )

        relationship = self._relationships[relationship_id]
        pool = self._fk_value_pools.get(relationship_id, [])

        # Refresh pool if empty but parent keys exist.
        if not pool:
            pool = self._build_fk_value_pool(relationship_id)

        if not pool:
            if relationship.is_nullable:
                self.logger.warning(
                    "empty_parent_pool_nullable",
                    relationship_id=relationship_id,
                    parent=relationship.parent_table,
                )
                return [None] * count

            violation = IntegrityViolation(
                violation_type="orphaned_fk",
                table_name=relationship.child_table,
                column_name=relationship.child_column,
                record_index=-1,
                expected_value="non-empty parent key pool",
                actual_value="empty pool",
                relationship_id=relationship_id,
                severity="error",
                message=(
                    f"No parent keys available for "
                    f"'{relationship.parent_table}.{relationship.parent_column}' "
                    f"referenced by "
                    f"'{relationship.child_table}.{relationship.child_column}'"
                ),
            )
            self._violations.append(violation)
            raise ValueError(violation.message)

        # Convert pool to numpy array for efficient selection.
        pool_array = np.array(pool)

        # Select FK values using the requested strategy.
        if strategy == "random":
            selected = np.random.choice(pool_array, size=count, replace=True).tolist()
        elif strategy == "sequential":
            counter_key = relationship_id
            start = self._sequential_counters[counter_key]
            indices = [(start + i) % len(pool) for i in range(count)]
            selected = [pool[idx] for idx in indices]
            self._sequential_counters[counter_key] = (start + count) % len(pool)
        elif strategy == "weighted":
            # Weighted: earlier parent keys are slightly more likely,
            # simulating natural data skew where older records accumulate
            # more references.
            weights = np.arange(1, len(pool) + 1, dtype=float)
            weights = weights / weights.sum()
            selected = np.random.choice(
                pool_array, size=count, replace=True, p=weights,
            ).tolist()
        elif strategy == "uniform":
            # Uniform: distribute child records as evenly as possible
            # across parent keys.
            base_per_parent = count // len(pool)
            remainder = count % len(pool)
            selected_list: list[Any] = []
            for i, key in enumerate(pool):
                reps = base_per_parent + (1 if i < remainder else 0)
                selected_list.extend([key] * reps)
            np.random.shuffle(np.array(selected_list, dtype=object))
            selected = selected_list[:count]
        else:
            # Fall back to random for unknown strategies.
            self.logger.warning(
                "unknown_fk_strategy_fallback_random",
                strategy=strategy,
                relationship_id=relationship_id,
            )
            selected = np.random.choice(pool_array, size=count, replace=True).tolist()

        # Apply null ratio for nullable FK columns.
        if relationship.is_nullable and null_ratio > 0.0:
            null_mask = np.random.random(count) < null_ratio
            for i in range(count):
                if null_mask[i]:
                    selected[i] = None

        self.logger.debug(
            "foreign_keys_resolved",
            relationship_id=relationship_id,
            count=count,
            strategy=strategy,
            null_count=sum(1 for v in selected if v is None),
        )
        return selected

    def resolve_cross_module_reference(
        self,
        source_module: str,
        target_module: str,
        target_table: str,
        target_column: str,
        count: int = 1,
    ) -> list[Any]:
        """Resolve FK values that cross ERP module boundaries.

        Looks up the generated keys for ``target_table.target_column`` in
        the *target_module* and selects *count* values using random
        sampling.

        Example: HR payroll entries referencing Financial Accounting GL
        account codes.

        Args:
            source_module: ERP module of the child table requesting the
                FK values.
            target_module: ERP module that owns the parent table.
            target_table: Fully qualified parent table name.
            target_column: Primary-key column in the parent table.
            count: Number of FK values to produce.

        Returns:
            List of FK values drawn from the target module's generated
            keys.  Returns a list of ``None`` values if no keys are
            available.
        """
        mapping_key = f"{target_table}.{target_column}"
        mapping = self._key_mappings.get(mapping_key)

        if mapping is None or not mapping.generated_keys:
            self.logger.warning(
                "cross_module_ref_empty_pool",
                source_module=source_module,
                target_module=target_module,
                target_table=target_table,
                target_column=target_column,
            )
            return [None] * count

        pool_array = np.array(mapping.generated_keys)
        selected = np.random.choice(pool_array, size=count, replace=True).tolist()

        # Track the cross-module reference for audit.
        ref_key = f"{target_table}.{target_column}"
        self._cross_module_refs[source_module][ref_key].append(
            {
                "target_module": target_module,
                "count": count,
                "target_table": target_table,
                "target_column": target_column,
            },
        )

        self.logger.info(
            "cross_module_reference_resolved",
            source_module=source_module,
            target_module=target_module,
            target_table=target_table,
            count=count,
        )
        return selected

    # ------------------------------------------------------------------
    # Validation
    # ------------------------------------------------------------------

    def validate_batch(
        self,
        table_name: str,
        batch_data: np.ndarray | dict,
        column_names: list[str],
    ) -> tuple[bool, list[IntegrityViolation]]:
        """Validate FK and PK constraints for a single batch of records.

        Checks performed:

        1. **Orphaned FK** — FK values that do not exist in the parent
           table's generated keys.
        2. **Null required FK** — ``NULL`` in a non-nullable FK column.
        3. **Duplicate PK** — Duplicate values in PK columns.
        4. **Cardinality violation** — Uniqueness constraint for
           ``one_to_one`` relationships.

        Args:
            table_name: Table to which the batch belongs.
            batch_data: Record data — either a 2-D ``np.ndarray`` (rows ×
                columns) or a column-oriented ``dict`` mapping column name
                to list of values.
            column_names: Ordered list of column names corresponding to
                the array columns or dict keys.

        Returns:
            A 2-tuple ``(is_valid, violations)`` where *is_valid* is
            ``True`` when no error-severity violations were found.
        """
        violations: list[IntegrityViolation] = []

        # Normalise batch_data into column-oriented dict.
        col_data: Dict[str, list] = {}
        if isinstance(batch_data, np.ndarray):
            for idx, col_name in enumerate(column_names):
                col_data[col_name] = batch_data[:, idx].tolist()
        elif isinstance(batch_data, dict):
            col_data = {k: list(v) for k, v in batch_data.items()}
        else:
            self.logger.error(
                "unsupported_batch_data_type",
                table=table_name,
                data_type=type(batch_data).__name__,
            )
            return False, [
                IntegrityViolation(
                    violation_type="orphaned_fk",
                    table_name=table_name,
                    column_name="",
                    record_index=-1,
                    expected_value="np.ndarray or dict",
                    actual_value=type(batch_data).__name__,
                    relationship_id="",
                    severity="error",
                    message=f"Unsupported batch_data type: {type(batch_data).__name__}",
                ),
            ]

        # --- PK duplicate check ---
        pk_relationships = self._get_relationships_for_table(
            table_name, role="parent",
        )
        pk_columns_checked: Set[str] = set()
        for rel in pk_relationships:
            pk_col = rel.parent_column
            if pk_col in pk_columns_checked or pk_col not in col_data:
                continue
            pk_columns_checked.add(pk_col)
            values = col_data[pk_col]
            arr = np.array(values)
            unique_vals, counts = np.unique(arr, return_counts=True)
            dup_mask = counts > 1
            if np.any(dup_mask):
                for dup_val in unique_vals[dup_mask]:
                    indices = [i for i, v in enumerate(values) if v == dup_val]
                    for idx in indices[1:]:
                        violations.append(
                            IntegrityViolation(
                                violation_type="duplicate_pk",
                                table_name=table_name,
                                column_name=pk_col,
                                record_index=idx,
                                expected_value="unique",
                                actual_value=dup_val,
                                relationship_id=rel.relationship_id,
                                severity="error",
                                message=(
                                    f"Duplicate PK value '{dup_val}' in "
                                    f"'{table_name}.{pk_col}' at row {idx}"
                                ),
                            ),
                        )

        # --- FK validation ---
        fk_relationships = self._get_relationships_for_table(
            table_name, role="child",
        )
        for rel in fk_relationships:
            fk_col = rel.child_column
            if fk_col not in col_data:
                continue

            values = col_data[fk_col]
            parent_key = f"{rel.parent_table}.{rel.parent_column}"
            parent_mapping = self._key_mappings.get(parent_key)
            parent_keys_arr: np.ndarray | None = None
            if parent_mapping is not None and parent_mapping.generated_keys:
                parent_keys_arr = np.array(parent_mapping.generated_keys)

            # Identify null positions first.
            null_indices: list[int] = []
            non_null_indices: list[int] = []
            non_null_values: list[Any] = []
            for row_idx, val in enumerate(values):
                if val is None or (isinstance(val, float) and np.isnan(val)):
                    null_indices.append(row_idx)
                else:
                    non_null_indices.append(row_idx)
                    non_null_values.append(val)

            # Report null violations for non-nullable FK columns.
            if not rel.is_nullable:
                for row_idx in null_indices:
                    violations.append(
                        IntegrityViolation(
                            violation_type="null_required_fk",
                            table_name=table_name,
                            column_name=fk_col,
                            record_index=row_idx,
                            expected_value="non-null",
                            actual_value=values[row_idx],
                            relationship_id=rel.relationship_id,
                            severity="error",
                            message=(
                                f"NULL in non-nullable FK "
                                f"'{table_name}.{fk_col}' at row {row_idx}"
                            ),
                        ),
                    )

            # Vectorised orphaned FK detection using np.isin().
            if parent_keys_arr is not None and non_null_values:
                fk_arr = np.array(non_null_values)
                valid_mask = np.isin(fk_arr, parent_keys_arr)
                orphan_positions = np.where(~valid_mask)[0]
                for pos in orphan_positions:
                    row_idx = non_null_indices[int(pos)]
                    violations.append(
                        IntegrityViolation(
                            violation_type="orphaned_fk",
                            table_name=table_name,
                            column_name=fk_col,
                            record_index=row_idx,
                            expected_value=(
                                f"value in {rel.parent_table}.{rel.parent_column}"
                            ),
                            actual_value=values[row_idx],
                            relationship_id=rel.relationship_id,
                            severity="error",
                            message=(
                                f"Orphaned FK value '{values[row_idx]}' in "
                                f"'{table_name}.{fk_col}' at row {row_idx} — "
                                f"no matching PK in "
                                f"'{rel.parent_table}.{rel.parent_column}'"
                            ),
                        ),
                    )

            # Cardinality check for one-to-one relationships.
            if rel.cardinality == "one_to_one":
                non_null_vals = [
                    v for v in values
                    if v is not None and not (isinstance(v, float) and np.isnan(v))
                ]
                arr = np.array(non_null_vals)
                if len(arr) > 0:
                    unique_vals, counts = np.unique(arr, return_counts=True)
                    dup_mask = counts > 1
                    if np.any(dup_mask):
                        for dup_val in unique_vals[dup_mask]:
                            violations.append(
                                IntegrityViolation(
                                    violation_type="cardinality_violation",
                                    table_name=table_name,
                                    column_name=fk_col,
                                    record_index=-1,
                                    expected_value="unique FK values (one-to-one)",
                                    actual_value=dup_val,
                                    relationship_id=rel.relationship_id,
                                    severity="error",
                                    message=(
                                        f"Cardinality violation: duplicate FK "
                                        f"'{dup_val}' in one-to-one relationship "
                                        f"'{table_name}.{fk_col}'"
                                    ),
                                ),
                            )

        # Accumulate global violations.
        self._violations.extend(violations)

        is_valid = all(v.severity != "error" for v in violations)

        self.logger.info(
            "batch_validated",
            table=table_name,
            row_count=len(next(iter(col_data.values()), [])),
            violation_count=len(violations),
            is_valid=is_valid,
        )
        return is_valid, violations

    def validate_complete_dataset(
        self,
        dataset: Dict[str, Any],
    ) -> tuple[float, list[IntegrityViolation]]:
        """Validate referential integrity across the entire generated dataset.

        Iterates over every table in *dataset*, runs
        :meth:`validate_batch` on each, and computes an aggregate
        **referential integrity score** defined as::

            score = valid_references / total_references

        This score feeds directly into the 30 % weight in the quality
        scoring model:
        ``Q = 0.4 * statistical + 0.3 * business_rules + 0.3 * referential``.

        Args:
            dataset: Mapping of ``table_name`` to table payload.  Each
                payload is expected to be a ``dict`` with keys
                ``"column_names"`` (``list[str]``) and ``"data"``
                (``np.ndarray`` or column-oriented ``dict``).

        Returns:
            A 2-tuple ``(integrity_score, all_violations)`` where
            *integrity_score* is in ``[0.0, 1.0]``.
        """
        all_violations: list[IntegrityViolation] = []
        total_references: int = 0
        valid_references: int = 0

        for table_name, table_payload in dataset.items():
            if isinstance(table_payload, dict):
                column_names = table_payload.get("column_names", [])
                data = table_payload.get("data", {})
            else:
                self.logger.warning(
                    "skipping_unsupported_table_payload",
                    table=table_name,
                    payload_type=type(table_payload).__name__,
                )
                continue

            if not column_names or (isinstance(data, dict) and not data):
                continue

            _, batch_violations = self.validate_batch(
                table_name, data, column_names,
            )
            all_violations.extend(batch_violations)

            # Count total FK references for this table.
            fk_rels = self._get_relationships_for_table(table_name, role="child")
            col_data: Dict[str, list] = {}
            if isinstance(data, np.ndarray):
                for idx, col_name in enumerate(column_names):
                    col_data[col_name] = data[:, idx].tolist()
            elif isinstance(data, dict):
                col_data = {k: list(v) for k, v in data.items()}

            for rel in fk_rels:
                fk_col = rel.child_column
                if fk_col not in col_data:
                    continue
                values = col_data[fk_col]
                non_null_count = sum(
                    1 for v in values
                    if v is not None and not (isinstance(v, float) and np.isnan(v))
                )
                total_references += non_null_count

            # Valid references = total minus orphaned/null violations for
            # this table.
            table_error_count = sum(
                1 for v in batch_violations
                if v.severity == "error"
                and v.violation_type in ("orphaned_fk", "null_required_fk")
            )
            valid_references += (
                sum(
                    sum(
                        1 for v in col_data.get(rel.child_column, [])
                        if v is not None
                        and not (isinstance(v, float) and np.isnan(v))
                    )
                    for rel in fk_rels
                    if rel.child_column in col_data
                )
                - table_error_count
            )

        integrity_score: float = 1.0
        if total_references > 0:
            integrity_score = max(0.0, min(1.0, valid_references / total_references))

        self.logger.info(
            "dataset_validation_complete",
            total_tables=len(dataset),
            total_references=total_references,
            valid_references=valid_references,
            integrity_score=round(integrity_score, 6),
            total_violations=len(all_violations),
        )
        return integrity_score, all_violations

    # ------------------------------------------------------------------
    # Generation Ordering
    # ------------------------------------------------------------------

    def get_generation_order(self) -> list[str]:
        """Return the topological generation order for all registered tables.

        Delegates to :meth:`DependencyGraph.get_topological_order` so that
        parent tables are generated before their children.

        Returns:
            Ordered list of fully qualified table names.
        """
        order = self._dependency_graph.get_topological_order()
        self.logger.debug(
            "generation_order_retrieved",
            table_count=len(order),
        )
        return order

    # ------------------------------------------------------------------
    # Reporting
    # ------------------------------------------------------------------

    def get_integrity_report(self) -> dict:
        """Produce a comprehensive referential-integrity report.

        Returns:
            Dictionary with the following structure::

                {
                    "total_relationships": int,
                    "total_keys_generated": int,
                    "total_violations": int,
                    "violations_by_type": { type_str: count, ... },
                    "violations_by_table": { table_name: count, ... },
                    "cross_module_references": { module: { ref_key: [...] } },
                    "integrity_score": float,
                    "erp_module_coverage": { module: [table, ...], ... },
                }
        """
        violations_by_type: Dict[str, int] = defaultdict(int)
        violations_by_table: Dict[str, int] = defaultdict(int)

        for v in self._violations:
            violations_by_type[v.violation_type] += 1
            violations_by_table[v.table_name] += 1

        total_keys = sum(
            len(m.generated_keys) for m in self._key_mappings.values()
        )

        # Compute approximate integrity score from accumulated violations.
        total_refs = 0
        error_violations = sum(
            1 for v in self._violations
            if v.severity == "error"
            and v.violation_type in ("orphaned_fk", "null_required_fk")
        )
        for mapping in self._key_mappings.values():
            child_rels = self._get_relationships_for_table(
                mapping.table_name, role="parent",
            )
            for rel in child_rels:
                child_key = f"{rel.child_table}.{rel.child_column}"
                child_mapping = self._key_mappings.get(child_key)
                if child_mapping:
                    total_refs += len(child_mapping.generated_keys)

        integrity_score = 1.0
        if total_refs > 0:
            integrity_score = max(
                0.0, min(1.0, (total_refs - error_violations) / total_refs),
            )

        # Serialise relationship definitions via model_dump() for portability.
        relationship_details: list[dict] = [
            rel.model_dump() for rel in self._relationships.values()
        ]

        # Gather downstream dependency information from the graph.
        dependents_map: Dict[str, list[str]] = {}
        for table_key in self._key_mappings:
            table_name_part = table_key.rsplit(".", 1)[0] if "." in table_key else table_key
            try:
                deps = self._dependency_graph.get_dependents(table_name_part)
                if deps:
                    dependents_map[table_name_part] = deps
            except (KeyError, AttributeError):
                pass

        # Export the FK relationship JSON schema for documentation.
        fk_schema = ForeignKeyRelationship.model_json_schema()

        report = {
            "total_relationships": len(self._relationships),
            "total_keys_generated": total_keys,
            "total_violations": len(self._violations),
            "violations_by_type": dict(violations_by_type),
            "violations_by_table": dict(violations_by_table),
            "cross_module_references": {
                module: {k: v for k, v in refs.items()}
                for module, refs in self._cross_module_refs.items()
            },
            "integrity_score": round(integrity_score, 6),
            "erp_module_coverage": dict(self._erp_module_registry),
            "relationship_details": relationship_details,
            "downstream_dependents": dependents_map,
            "relationship_schema": fk_schema,
        }

        self.logger.info(
            "integrity_report_generated",
            total_relationships=report["total_relationships"],
            total_keys=report["total_keys_generated"],
            total_violations=report["total_violations"],
            integrity_score=report["integrity_score"],
        )
        return report

    # ------------------------------------------------------------------
    # State Management
    # ------------------------------------------------------------------

    def reset(self) -> None:
        """Clear all transient generation state while retaining schema.

        Clears generated key mappings, accumulated violations, FK value
        pools, cross-module reference audit records, and sequential
        counters.  Relationship definitions (which originate from schema
        metadata) are preserved.
        """
        with self._lock:
            self._key_mappings.clear()
            self._fk_value_pools.clear()
            self._violations.clear()
            self._cross_module_refs = defaultdict(lambda: defaultdict(list))
            self._sequential_counters.clear()

        self.logger.info(
            "relationship_manager_reset",
            retained_relationships=len(self._relationships),
        )

    # ------------------------------------------------------------------
    # Private Helpers
    # ------------------------------------------------------------------

    def _get_relationships_for_table(
        self,
        table_name: str,
        role: str = "child",
    ) -> list[ForeignKeyRelationship]:
        """Return all relationships involving *table_name* in the given role.

        Args:
            table_name: Fully qualified table name.
            role: ``'child'`` to find relationships where the table holds
                the FK, or ``'parent'`` to find relationships where the
                table holds the referenced PK.

        Returns:
            List of matching :class:`ForeignKeyRelationship` instances.
        """
        results: list[ForeignKeyRelationship] = []
        for rel in self._relationships.values():
            if role == "child" and rel.child_table == table_name:
                results.append(rel)
            elif role == "parent" and rel.parent_table == table_name:
                results.append(rel)
        return results

    def _build_fk_value_pool(self, relationship_id: str) -> list:
        """Build or refresh the FK value pool for a relationship.

        The pool is a flat list of all parent-side PK values that child
        records may reference.

        Args:
            relationship_id: Identifier of the FK relationship.

        Returns:
            The (possibly empty) value pool list.
        """
        rel = self._relationships.get(relationship_id)
        if rel is None:
            return []

        parent_key = f"{rel.parent_table}.{rel.parent_column}"
        mapping = self._key_mappings.get(parent_key)

        if mapping is None or not mapping.generated_keys:
            self._fk_value_pools[relationship_id] = []
            return []

        pool = list(mapping.generated_keys)
        self._fk_value_pools[relationship_id] = pool
        return pool

    @staticmethod
    def _detect_erp_module(table_name: str, erp_system: str) -> str:
        """Auto-detect the ERP module from a table name and ERP system.

        Applies prefix-matching rules defined in
        :data:`MODULE_TABLE_PREFIXES`.  The logic mirrors
        :meth:`DependencyGraph._detect_erp_module` to maintain consistency.

        Args:
            table_name: Fully qualified table name.
            erp_system: ERP system identifier.

        Returns:
            Canonical ERP module string, or ``""`` if no match.
        """
        lower_name = table_name.lower()

        # Strip ERP system prefix if present.
        for prefix in (f"{erp_system}.", f"{erp_system}_"):
            if lower_name.startswith(prefix):
                lower_name = lower_name[len(prefix):]
                break

        module_map = MODULE_TABLE_PREFIXES.get(erp_system, {})

        if not module_map:
            # For unknown ERP systems try all known prefix maps.
            for sys_map in MODULE_TABLE_PREFIXES.values():
                for mod_prefix, module_name in sorted(
                    sys_map.items(), key=lambda kv: -len(kv[0]),
                ):
                    if lower_name.startswith((f"{mod_prefix}.", f"{mod_prefix}_")):
                        return module_name
            return ""

        # Match longest prefix first.
        for mod_prefix, module_name in sorted(
            module_map.items(), key=lambda kv: -len(kv[0]),
        ):
            if lower_name.startswith((f"{mod_prefix}.", f"{mod_prefix}_")):
                return module_name

        return ""

    @staticmethod
    def _generate_relationship_id(
        parent_table: str,
        parent_column: str,
        child_table: str,
        child_column: str,
    ) -> str:
        """Create a deterministic, unique relationship ID.

        Uses a SHA-256 digest of the four endpoint identifiers so that the
        same FK always maps to the same relationship ID regardless of
        registration order.

        Args:
            parent_table: Parent table name.
            parent_column: Parent PK column name.
            child_table: Child table name.
            child_column: Child FK column name.

        Returns:
            A 16-character hexadecimal string prefixed with ``"fk_"``.
        """
        raw = f"{parent_table}|{parent_column}|{child_table}|{child_column}"
        digest = hashlib.sha256(raw.encode("utf-8")).hexdigest()[:16]
        return f"fk_{digest}"
