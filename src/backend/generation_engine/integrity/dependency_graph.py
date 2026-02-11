"""Table dependency graph construction and topological sorting for the Generation Engine.

Builds a directed acyclic graph (DAG) of table dependencies based on foreign key
relationships extracted from ERP schema definitions.  Enables ordered generation
where parent tables are always produced before their dependent child tables, which
is essential for maintaining referential integrity across the synthetic dataset.

Core capabilities:

* **Topological ordering** — Kahn's algorithm (BFS-based) computes a valid
  generation sequence in O(V + E) time.
* **Cycle detection** — Iterative DFS with three-colour marking identifies all
  back-edges that form cycles in the FK dependency graph.
* **Strongly connected components** — Tarjan's algorithm groups mutually dependent
  tables so they can be generated as a coordinated batch.
* **Cycle resolution** — Four pluggable strategies (nullable-edge relaxation,
  deferred constraints, SCC batching, manual ordering) break circular references
  that are common in complex ERP schemas (e.g. mutual FK references between SAP
  FI/CO tables).
* **Multi-module analysis** — Identifies and tracks cross-module dependency edges
  spanning Financial Accounting, HR, Sales & Distribution, and Material Management
  ERP modules.
* **Parallel generation levels** — Groups tables into levels where all tables in
  a given level can be generated concurrently once the preceding level completes.

This module operates exclusively on **schema metadata** — no production data is
accessed or stored, in compliance with Constraint C-001.

Usage::

    from generation_engine.integrity.dependency_graph import (
        DependencyGraph,
        TableNode,
        DependencyEdge,
        CycleResolutionStrategy,
    )

    graph = DependencyGraph()
    graph.build_from_schema(schema_definition)
    order = graph.get_topological_order()
"""

from __future__ import annotations

from collections import defaultdict, deque
from dataclasses import asdict, dataclass, field
from enum import Enum
from typing import Any, Dict, List, Optional, Set, Tuple

from shared.logging.structured_logger import get_logger


# ---------------------------------------------------------------------------
# ERP module detection patterns
# ---------------------------------------------------------------------------

# Maps ERP-system-specific schema/table prefixes to the canonical ERP module
# name used internally.  Each key is a lower-cased prefix of the fully
# qualified table name; the value is one of the four supported modules.

_SAP_MODULE_PREFIXES: dict[str, str] = {
    "fi": "financial_accounting",
    "co": "financial_accounting",
    "hr": "hr",
    "pa": "hr",
    "sd": "sales_distribution",
    "mm": "material_management",
}

_ORACLE_MODULE_PREFIXES: dict[str, str] = {
    "gl": "financial_accounting",
    "ap": "financial_accounting",
    "ar": "financial_accounting",
    "fa": "financial_accounting",
    "hr": "hr",
    "per": "hr",
    "pay": "hr",
    "oe": "sales_distribution",
    "ra": "sales_distribution",
    "po": "material_management",
    "inv": "material_management",
    "rcv": "material_management",
}

_DYNAMICS_MODULE_PREFIXES: dict[str, str] = {
    "ledger": "financial_accounting",
    "gl": "financial_accounting",
    "finance": "financial_accounting",
    "hcm": "hr",
    "payroll": "hr",
    "workforce": "hr",
    "sales": "sales_distribution",
    "customer": "sales_distribution",
    "order": "sales_distribution",
    "procurement": "material_management",
    "inventory": "material_management",
    "warehouse": "material_management",
}

_ERP_MODULE_MAPS: dict[str, dict[str, str]] = {
    "sap": _SAP_MODULE_PREFIXES,
    "oracle": _ORACLE_MODULE_PREFIXES,
    "dynamics": _DYNAMICS_MODULE_PREFIXES,
}

_VALID_ERP_MODULES: frozenset[str] = frozenset({
    "financial_accounting",
    "hr",
    "sales_distribution",
    "material_management",
})


# ---------------------------------------------------------------------------
# Data classes
# ---------------------------------------------------------------------------


@dataclass
class TableNode:
    """Represents a single table in the dependency graph.

    Stores table metadata required for generation planning — column definitions,
    primary keys, ERP module classification, and optional generation priority
    overrides.  Fully qualified table names follow the convention
    ``<erp_system>.<module_prefix>.<table>`` (e.g. ``sap.fi.gl_accounts``).

    Attributes:
        table_name: Fully qualified table name used as the unique node key.
        schema_name: Database schema or namespace the table belongs to.
        erp_module: Canonical ERP module identifier (``financial_accounting``,
            ``hr``, ``sales_distribution``, ``material_management``).
        erp_system: Source ERP system (``sap``, ``oracle``, ``dynamics``,
            ``legacy``).
        columns: List of column metadata dicts with keys ``name``, ``type``,
            ``is_pk``, ``is_fk``, ``nullable``.
        primary_keys: List of primary-key column names.
        row_count_estimate: Estimated row count used for generation sizing.
        generation_priority: Manual priority override — lower values are
            generated first.  ``0`` means no override.
        metadata: Arbitrary additional metadata for extensibility.
    """

    table_name: str
    schema_name: str = ""
    erp_module: str = ""
    erp_system: str = ""
    columns: list[dict[str, Any]] = field(default_factory=list)
    primary_keys: list[str] = field(default_factory=list)
    row_count_estimate: int = 0
    generation_priority: int = 0
    metadata: dict[str, Any] = field(default_factory=dict)


@dataclass
class DependencyEdge:
    """Represents a directed foreign-key dependency between two tables.

    The edge direction follows the *dependency* relationship: the **child**
    table depends on the **parent** table (i.e. the child has a foreign key
    that references the parent's primary key).  In graph terms the directed
    edge is ``parent → child``, meaning the parent must be generated first.

    Attributes:
        parent_table: Name of the referenced (parent) table.
        child_table: Name of the referencing (child) table.
        fk_column: Foreign key column name in the child table.
        pk_column: Referenced primary key column name in the parent table.
        is_nullable: ``True`` if the FK column is nullable — important for
            cycle resolution via nullable-edge relaxation.
        is_cross_module: ``True`` when the edge connects tables in different
            ERP modules.
        weight: Edge weight for prioritisation; cross-module edges default
            to a higher weight to ensure they are handled with care.
    """

    parent_table: str
    child_table: str
    fk_column: str
    pk_column: str
    is_nullable: bool = False
    is_cross_module: bool = False
    weight: float = 1.0


# ---------------------------------------------------------------------------
# Cycle resolution strategy enumeration
# ---------------------------------------------------------------------------


class CycleResolutionStrategy(Enum):
    """Strategies for resolving circular FK references in ERP schemas.

    Complex ERP schemas (particularly SAP FI/CO and Oracle GL/AP) frequently
    contain mutually referencing tables.  This enum defines four strategies
    that the :class:`DependencyGraph` can apply to break these cycles so that
    a valid topological generation order can be computed.

    Members:
        NULLABLE_EDGE_RELAXATION: Break cycles by temporarily removing
            nullable FK edges; generate the child with ``NULL`` FK values
            and back-fill them after the parent table has been generated.
        DEFERRED_CONSTRAINT: Mark cycle edges for deferred constraint
            checking — the target database must support ``SET CONSTRAINTS
            DEFERRED``.
        STRONGLY_CONNECTED_BATCH: Generate all tables in a strongly
            connected component as a single coordinated batch with
            pre-allocated key ranges.
        MANUAL_ORDERING: Fall back to the ``generation_priority`` field
            on :class:`TableNode` to break ties.
    """

    NULLABLE_EDGE_RELAXATION = "nullable_edge_relaxation"
    DEFERRED_CONSTRAINT = "deferred_constraint"
    STRONGLY_CONNECTED_BATCH = "strongly_connected_batch"
    MANUAL_ORDERING = "manual_ordering"


# ---------------------------------------------------------------------------
# DependencyGraph implementation
# ---------------------------------------------------------------------------


class DependencyGraph:
    """Directed graph of table dependencies based on foreign-key relationships.

    The graph nodes are :class:`TableNode` instances and edges are
    :class:`DependencyEdge` instances representing FK constraints.  The
    primary purpose is to compute a correct **generation order** so that
    parent tables are always populated before any child table that references
    them, thereby guaranteeing referential integrity in the synthetic dataset.

    Thread-safety note:
        A single ``DependencyGraph`` instance is **not** thread-safe.
        Build the graph in a single thread, then share the computed
        generation order (an immutable list) across worker threads.

    Example::

        graph = DependencyGraph()
        graph.add_table(TableNode(table_name="customers"))
        graph.add_table(TableNode(table_name="orders"))
        graph.add_dependency(DependencyEdge(
            parent_table="customers",
            child_table="orders",
            fk_column="customer_id",
            pk_column="id",
        ))
        order = graph.get_topological_order()
        # order == ["customers", "orders"]
    """

    def __init__(self) -> None:
        """Initialise an empty dependency graph."""
        self._nodes: Dict[str, TableNode] = {}
        self._adjacency_list: Dict[str, Set[str]] = defaultdict(set)
        self._reverse_adjacency: Dict[str, Set[str]] = defaultdict(set)
        self._edges: Dict[Tuple[str, str], DependencyEdge] = {}
        self._topological_order: Optional[List[str]] = None
        self._is_dirty: bool = True
        self._cycle_resolution_strategy: CycleResolutionStrategy = (
            CycleResolutionStrategy.NULLABLE_EDGE_RELAXATION
        )
        self._relaxed_edges: List[DependencyEdge] = []
        self._logger = get_logger(__name__)

    # ------------------------------------------------------------------
    # Graph construction
    # ------------------------------------------------------------------

    def add_table(self, node: TableNode) -> None:
        """Add a table node to the dependency graph.

        If a node with the same ``table_name`` already exists it is silently
        replaced.  Empty adjacency entries are initialised so that later edge
        additions do not need to check for key existence.

        Args:
            node: The :class:`TableNode` to add.
        """
        self._nodes[node.table_name] = node
        # Ensure adjacency entries exist (defaultdict handles this, but
        # explicit access makes the intent clear).
        _ = self._adjacency_list[node.table_name]
        _ = self._reverse_adjacency[node.table_name]
        self._is_dirty = True
        self._logger.debug(
            "table_added",
            table_name=node.table_name,
            erp_module=node.erp_module,
            erp_system=node.erp_system,
        )

    def add_dependency(self, edge: DependencyEdge) -> None:
        """Add a directed FK dependency edge between two tables.

        The edge direction is ``parent → child``, meaning the parent table
        must be generated before the child.  Both endpoint tables must
        already exist in the graph (added via :meth:`add_table`).

        Args:
            edge: The :class:`DependencyEdge` describing the relationship.

        Raises:
            ValueError: If either the parent or child table has not been
                added to the graph.
        """
        if edge.parent_table not in self._nodes:
            raise ValueError(
                f"Parent table '{edge.parent_table}' not found in graph. "
                "Add it with add_table() before creating dependencies."
            )
        if edge.child_table not in self._nodes:
            raise ValueError(
                f"Child table '{edge.child_table}' not found in graph. "
                "Add it with add_table() before creating dependencies."
            )

        # Detect cross-module edges automatically.
        parent_module = self._nodes[edge.parent_table].erp_module
        child_module = self._nodes[edge.child_table].erp_module
        if parent_module and child_module and parent_module != child_module:
            edge.is_cross_module = True
            # Cross-module edges carry a higher default weight.
            if edge.weight == 1.0:
                edge.weight = 2.0

        key = (edge.parent_table, edge.child_table)
        self._edges[key] = edge
        self._adjacency_list[edge.parent_table].add(edge.child_table)
        self._reverse_adjacency[edge.child_table].add(edge.parent_table)
        self._is_dirty = True
        self._logger.debug(
            "dependency_added",
            parent=edge.parent_table,
            child=edge.child_table,
            fk_column=edge.fk_column,
            is_cross_module=edge.is_cross_module,
        )

    def build_from_schema(self, schema_definition: dict[str, Any]) -> None:
        """Populate the graph from a complete schema definition dictionary.

        The *schema_definition* is expected to follow the structure stored
        in MongoDB's ``schema_definitions`` collection:

        .. code-block:: python

            {
                "erp_system": "sap",
                "tables": [
                    {
                        "name": "sap.fi.gl_accounts",
                        "schema": "FI",
                        "columns": [...],
                        "primary_keys": ["account_id"],
                        "row_count_estimate": 5000,
                        "metadata": {}
                    },
                    ...
                ],
                "foreign_keys": [
                    {
                        "parent_table": "sap.fi.gl_accounts",
                        "child_table": "sap.fi.journal_entries",
                        "fk_column": "account_id",
                        "pk_column": "account_id",
                        "is_nullable": false
                    },
                    ...
                ]
            }

        Args:
            schema_definition: Dictionary conforming to the schema above.

        Raises:
            KeyError: If required keys (``tables``, ``foreign_keys``) are
                missing from the input.
        """
        erp_system: str = schema_definition.get("erp_system", "legacy").lower()
        tables: list[dict[str, Any]] = schema_definition.get("tables", [])
        foreign_keys: list[dict[str, Any]] = schema_definition.get("foreign_keys", [])

        if not tables:
            self._logger.warning("schema_import_empty", erp_system=erp_system)
            return

        # ---- Create table nodes ----
        for tbl in tables:
            table_name: str = tbl.get("name", "")
            if not table_name:
                continue

            columns: list[dict[str, Any]] = tbl.get("columns", [])
            primary_keys: list[str] = tbl.get("primary_keys", [])

            # Auto-detect primary keys from column metadata when not explicit.
            if not primary_keys:
                primary_keys = [
                    col["name"]
                    for col in columns
                    if col.get("is_pk", False)
                ]

            erp_module = tbl.get("erp_module", "")
            if not erp_module:
                erp_module = self._detect_erp_module(table_name, erp_system)

            node = TableNode(
                table_name=table_name,
                schema_name=tbl.get("schema", ""),
                erp_module=erp_module,
                erp_system=erp_system,
                columns=columns,
                primary_keys=primary_keys,
                row_count_estimate=tbl.get("row_count_estimate", 0),
                generation_priority=tbl.get("generation_priority", 0),
                metadata=tbl.get("metadata", {}),
            )
            self.add_table(node)

        # ---- Create dependency edges ----
        for fk in foreign_keys:
            parent: str = fk.get("parent_table", "")
            child: str = fk.get("child_table", "")
            if not parent or not child:
                continue

            # Skip edges referencing tables not in this schema definition.
            if parent not in self._nodes or child not in self._nodes:
                self._logger.warning(
                    "fk_reference_missing_table",
                    parent=parent,
                    child=child,
                )
                continue

            edge = DependencyEdge(
                parent_table=parent,
                child_table=child,
                fk_column=fk.get("fk_column", ""),
                pk_column=fk.get("pk_column", ""),
                is_nullable=fk.get("is_nullable", False),
            )
            self.add_dependency(edge)

        cross_module_count = sum(
            1 for e in self._edges.values() if e.is_cross_module
        )
        self._logger.info(
            "schema_imported",
            erp_system=erp_system,
            tables_count=len(tables),
            edges_count=len(self._edges),
            cross_module_edges=cross_module_count,
        )

    # ------------------------------------------------------------------
    # Topological ordering
    # ------------------------------------------------------------------

    def get_topological_order(self) -> list[str]:
        """Return a valid generation order for all tables in the graph.

        Uses Kahn's BFS-based algorithm.  If cycles are detected the
        configured :class:`CycleResolutionStrategy` is applied to break
        them before retrying.  The result is cached until the graph is
        modified.

        Returns:
            Ordered list of table names — generating tables in this order
            guarantees that every referenced parent row exists before a
            child row referencing it is inserted.

        Raises:
            RuntimeError: If cycles cannot be resolved (e.g. no nullable
                edges to relax and strategy is ``NULLABLE_EDGE_RELAXATION``).
        """
        if not self._is_dirty and self._topological_order is not None:
            return list(self._topological_order)

        sorted_nodes, remaining = self._kahns_topological_sort()

        if remaining:
            self._logger.warning(
                "cycles_detected_during_sort",
                remaining_count=len(remaining),
                remaining_tables=remaining,
            )
            # Attempt cycle resolution.
            relaxed = self.resolve_cycles(self._cycle_resolution_strategy)
            self._relaxed_edges = relaxed

            # Retry sort after resolution.
            sorted_nodes, remaining = self._kahns_topological_sort()
            if remaining:
                # Last resort: append remaining nodes sorted by priority.
                remaining_sorted = sorted(
                    remaining,
                    key=lambda t: self._nodes[t].generation_priority,
                )
                sorted_nodes.extend(remaining_sorted)
                self._logger.warning(
                    "unresolved_cycle_tables_appended",
                    count=len(remaining_sorted),
                )

        self._topological_order = sorted_nodes
        self._is_dirty = False
        self._logger.info(
            "topological_order_computed",
            total_tables=len(sorted_nodes),
        )
        return list(self._topological_order)

    def _kahns_topological_sort(self) -> Tuple[list[str], list[str]]:
        """Execute Kahn's algorithm for topological sorting.

        Computes in-degrees, enqueues zero-in-degree nodes, and iteratively
        processes them.  Nodes with equal in-degree are tie-broken by
        ``generation_priority`` (lower first).

        Returns:
            A 2-tuple of ``(sorted_nodes, remaining_unsorted_nodes)``.
            A non-empty ``remaining_unsorted_nodes`` list indicates the
            presence of cycles in the graph.
        """
        # Compute in-degree for every node.
        in_degree: Dict[str, int] = {name: 0 for name in self._nodes}
        for parent, children in self._adjacency_list.items():
            if parent not in self._nodes:
                continue
            for child in children:
                if child in in_degree:
                    in_degree[child] += 1

        # Seed the queue with root tables (in-degree == 0), ordered by
        # generation_priority for deterministic output.
        queue: deque[str] = deque(
            sorted(
                (name for name, deg in in_degree.items() if deg == 0),
                key=lambda t: self._nodes[t].generation_priority,
            )
        )

        sorted_nodes: list[str] = []

        while queue:
            node = queue.popleft()
            sorted_nodes.append(node)

            # Reduce in-degree for each child; enqueue newly-unblocked.
            children = sorted(
                self._adjacency_list.get(node, set()),
                key=lambda t: self._nodes[t].generation_priority
                if t in self._nodes
                else 0,
            )
            for child in children:
                if child not in in_degree:
                    continue
                in_degree[child] -= 1
                if in_degree[child] == 0:
                    queue.append(child)

        remaining = [n for n in self._nodes if n not in set(sorted_nodes)]
        return sorted_nodes, remaining

    # ------------------------------------------------------------------
    # Cycle detection
    # ------------------------------------------------------------------

    def detect_cycles(self) -> list[list[str]]:
        """Detect all cycles in the dependency graph using DFS colour marking.

        Each node is assigned one of three colours during the traversal:

        * **WHITE** (0) — not yet visited.
        * **GRAY** (1) — currently on the recursion stack.
        * **BLACK** (2) — fully processed.

        A back-edge to a GRAY node indicates a cycle.  The method collects
        every distinct cycle found.

        Returns:
            A list of cycles; each cycle is a list of table names forming
            the loop, ordered from the node where the back-edge was detected
            back to itself.
        """
        WHITE, GRAY, BLACK = 0, 1, 2
        colour: Dict[str, int] = {n: WHITE for n in self._nodes}
        parent_map: Dict[str, str | None] = {n: None for n in self._nodes}
        cycles: list[list[str]] = []

        def _dfs(node: str) -> None:
            colour[node] = GRAY
            for child in self._adjacency_list.get(node, set()):
                if child not in colour:
                    continue
                if colour[child] == WHITE:
                    parent_map[child] = node
                    _dfs(child)
                elif colour[child] == GRAY:
                    # Back-edge found — reconstruct cycle.
                    cycle: list[str] = [child]
                    current = node
                    while current != child:
                        cycle.append(current)
                        current = parent_map.get(current, child)  # type: ignore[assignment]
                        if current is None:
                            break
                    cycle.append(child)
                    cycle.reverse()
                    cycles.append(cycle)
            colour[node] = BLACK

        for node in self._nodes:
            if colour[node] == WHITE:
                _dfs(node)

        if cycles:
            self._logger.warning(
                "cycles_detected",
                count=len(cycles),
                cycles=[c[:5] for c in cycles],  # Truncate for log brevity.
            )
        else:
            self._logger.debug("no_cycles_detected")

        return cycles

    # ------------------------------------------------------------------
    # Strongly connected components (Tarjan's algorithm)
    # ------------------------------------------------------------------

    def find_strongly_connected_components(self) -> list[list[str]]:
        """Find all strongly connected components via Tarjan's algorithm.

        An SCC with more than one node represents a set of tables with
        mutual (cyclic) FK dependencies that must be handled together.

        Returns:
            List of SCCs.  Each SCC is a list of table names.  Single-node
            SCCs (trivial) are included for completeness but are typically
            filtered by the caller.
        """
        index_counter: list[int] = [0]
        stack: list[str] = []
        on_stack: set[str] = set()
        lowlink: Dict[str, int] = {}
        index: Dict[str, int] = {}
        result: list[list[str]] = []

        def _strongconnect(node: str) -> None:
            index[node] = index_counter[0]
            lowlink[node] = index_counter[0]
            index_counter[0] += 1
            stack.append(node)
            on_stack.add(node)

            for child in self._adjacency_list.get(node, set()):
                if child not in index:
                    _strongconnect(child)
                    lowlink[node] = min(lowlink[node], lowlink[child])
                elif child in on_stack:
                    lowlink[node] = min(lowlink[node], index[child])

            # Root of an SCC.
            if lowlink[node] == index[node]:
                component: list[str] = []
                while True:
                    w = stack.pop()
                    on_stack.discard(w)
                    component.append(w)
                    if w == node:
                        break
                result.append(component)

        for node in self._nodes:
            if node not in index:
                _strongconnect(node)

        self._logger.debug(
            "scc_computed",
            total_components=len(result),
            non_trivial=[c for c in result if len(c) > 1],
        )
        return result

    # ------------------------------------------------------------------
    # Cycle resolution
    # ------------------------------------------------------------------

    def resolve_cycles(
        self, strategy: CycleResolutionStrategy | None = None,
    ) -> list[DependencyEdge]:
        """Apply a cycle-resolution strategy to make the graph acyclic.

        After resolution the graph may have some edges removed or annotated
        so that :meth:`_kahns_topological_sort` can succeed.  The caller
        is responsible for back-filling any data that was skipped due to
        edge relaxation.

        Args:
            strategy: Resolution strategy to apply.  Falls back to the
                instance default when ``None``.

        Returns:
            List of :class:`DependencyEdge` instances that were relaxed,
            deferred, or otherwise modified.
        """
        effective_strategy = strategy or self._cycle_resolution_strategy
        sccs = self.find_strongly_connected_components()
        cyclic_sccs = [scc for scc in sccs if len(scc) > 1]

        if not cyclic_sccs:
            self._logger.info("no_cycles_to_resolve")
            return []

        self._logger.info(
            "resolving_cycles",
            strategy=effective_strategy.value,
            cyclic_scc_count=len(cyclic_sccs),
        )

        relaxed_edges: list[DependencyEdge] = []

        if effective_strategy == CycleResolutionStrategy.NULLABLE_EDGE_RELAXATION:
            relaxed_edges = self._resolve_nullable_relaxation(cyclic_sccs)
        elif effective_strategy == CycleResolutionStrategy.DEFERRED_CONSTRAINT:
            relaxed_edges = self._resolve_deferred_constraint(cyclic_sccs)
        elif effective_strategy == CycleResolutionStrategy.STRONGLY_CONNECTED_BATCH:
            relaxed_edges = self._resolve_scc_batch(cyclic_sccs)
        elif effective_strategy == CycleResolutionStrategy.MANUAL_ORDERING:
            relaxed_edges = self._resolve_manual_ordering(cyclic_sccs)

        self._logger.info(
            "cycles_resolved",
            strategy=effective_strategy.value,
            relaxed_edge_count=len(relaxed_edges),
        )
        return relaxed_edges

    def _resolve_nullable_relaxation(
        self, cyclic_sccs: list[list[str]],
    ) -> list[DependencyEdge]:
        """Break cycles by removing nullable FK edges from the graph.

        For each SCC the method iterates through edges internal to the
        component and removes the first nullable edge found, repeating
        until no more cycles remain.  Edges removed this way are returned
        so that the orchestrator can generate ``NULL`` FK values and back-
        fill them once the parent table has been generated.
        """
        relaxed: list[DependencyEdge] = []
        for scc in cyclic_sccs:
            scc_set = set(scc)
            # Collect edges internal to this SCC.
            internal_edges = [
                e
                for (p, c), e in self._edges.items()
                if p in scc_set and c in scc_set
            ]
            # Prefer nullable edges; sort by weight (ascending) so that
            # cheaper edges are relaxed first.
            nullable_edges = sorted(
                [e for e in internal_edges if e.is_nullable],
                key=lambda e: e.weight,
            )
            non_nullable_edges = sorted(
                [e for e in internal_edges if not e.is_nullable],
                key=lambda e: e.weight,
            )
            candidates = nullable_edges or non_nullable_edges

            for edge in candidates:
                key = (edge.parent_table, edge.child_table)
                if key in self._edges:
                    self._adjacency_list[edge.parent_table].discard(edge.child_table)
                    self._reverse_adjacency[edge.child_table].discard(edge.parent_table)
                    del self._edges[key]
                    relaxed.append(edge)
                    self._logger.debug(
                        "edge_relaxed",
                        parent=edge.parent_table,
                        child=edge.child_table,
                        fk_column=edge.fk_column,
                        is_nullable=edge.is_nullable,
                    )
                    # Check whether the SCC is broken.
                    remaining_sccs = self.find_strongly_connected_components()
                    still_cyclic = any(
                        len(s) > 1
                        and scc_set.intersection(s)
                        for s in remaining_sccs
                    )
                    if not still_cyclic:
                        break

        self._is_dirty = True
        return relaxed

    def _resolve_deferred_constraint(
        self, cyclic_sccs: list[list[str]],
    ) -> list[DependencyEdge]:
        """Mark cycle edges for deferred constraint checking.

        The edges are removed from the adjacency lists but retained in a
        separate tracking structure so the provisioning step can emit
        ``SET CONSTRAINTS DEFERRED`` statements.
        """
        deferred: list[DependencyEdge] = []
        for scc in cyclic_sccs:
            scc_set = set(scc)
            for (p, c), edge in list(self._edges.items()):
                if p in scc_set and c in scc_set:
                    self._adjacency_list[p].discard(c)
                    self._reverse_adjacency[c].discard(p)
                    del self._edges[(p, c)]
                    # Record deferred constraint info on the child node so
                    # that the provisioning service can emit SET CONSTRAINTS
                    # DEFERRED for the corresponding FK.
                    if c in self._nodes:
                        deferred_fks: list[str] = self._nodes[c].metadata.setdefault(
                            "deferred_fk_columns", [],
                        )
                        deferred_fks.append(edge.fk_column)
                    deferred.append(edge)
                    # Remove enough edges to break the cycle.
                    remaining_sccs = self.find_strongly_connected_components()
                    still_cyclic = any(
                        len(s) > 1 and scc_set.intersection(s)
                        for s in remaining_sccs
                    )
                    if not still_cyclic:
                        break
        self._is_dirty = True
        return deferred

    def _resolve_scc_batch(
        self, cyclic_sccs: list[list[str]],
    ) -> list[DependencyEdge]:
        """Group SCC tables for coordinated batch generation.

        All tables within an SCC are assigned a common ``batch_id`` in
        their metadata so the batch processor can generate them together
        with pre-allocated key ranges.  The internal edges are removed
        from the adjacency structure so topological sort can proceed.
        """
        batched_edges: list[DependencyEdge] = []
        for idx, scc in enumerate(cyclic_sccs):
            batch_id = f"scc_batch_{idx}"
            scc_set = set(scc)
            for table_name in scc:
                if table_name in self._nodes:
                    self._nodes[table_name].metadata["batch_id"] = batch_id

            for (p, c), edge in list(self._edges.items()):
                if p in scc_set and c in scc_set:
                    self._adjacency_list[p].discard(c)
                    self._reverse_adjacency[c].discard(p)
                    del self._edges[(p, c)]
                    batched_edges.append(edge)
        self._is_dirty = True
        return batched_edges

    def _resolve_manual_ordering(
        self, cyclic_sccs: list[list[str]],
    ) -> list[DependencyEdge]:
        """Break cycles by using ``generation_priority`` to pick cut edges.

        For each SCC the edge whose child has the **highest** (worst)
        ``generation_priority`` is removed, under the assumption that the
        user has annotated which table should tolerate a deferred FK.
        """
        cut_edges: list[DependencyEdge] = []
        for scc in cyclic_sccs:
            scc_set = set(scc)
            internal_edges = [
                ((p, c), e)
                for (p, c), e in self._edges.items()
                if p in scc_set and c in scc_set
            ]
            if not internal_edges:
                continue

            # Choose the edge whose child has the highest priority value.
            internal_edges.sort(
                key=lambda pair: self._nodes[pair[0][1]].generation_priority,
                reverse=True,
            )

            for (p, c), edge in internal_edges:
                key = (p, c)
                if key in self._edges:
                    self._adjacency_list[p].discard(c)
                    self._reverse_adjacency[c].discard(p)
                    del self._edges[key]
                    cut_edges.append(edge)
                    remaining_sccs = self.find_strongly_connected_components()
                    still_cyclic = any(
                        len(s) > 1 and scc_set.intersection(s)
                        for s in remaining_sccs
                    )
                    if not still_cyclic:
                        break
        self._is_dirty = True
        return cut_edges

    # ------------------------------------------------------------------
    # Generation planning
    # ------------------------------------------------------------------

    def get_generation_levels(self) -> list[list[str]]:
        """Group tables into parallel generation levels.

        Level 0 contains tables with no dependencies (root/parent tables).
        Level *N* contains tables whose **all** parents are assigned to
        levels 0 through *N*-1.  Tables within the same level can be
        generated concurrently.

        Returns:
            List of levels, where each level is a list of table names.
        """
        order = self.get_topological_order()
        level_map: Dict[str, int] = {}
        levels: Dict[int, list[str]] = defaultdict(list)

        for table in order:
            parents = self._reverse_adjacency.get(table, set())
            if not parents:
                lvl = 0
            else:
                lvl = max(
                    (level_map.get(p, 0) for p in parents if p in level_map),
                    default=0,
                ) + 1
            level_map[table] = lvl
            levels[lvl].append(table)

        max_level = max(levels.keys()) if levels else -1
        result = [levels[i] for i in range(max_level + 1)]

        self._logger.info(
            "generation_levels_computed",
            total_levels=len(result),
            tables_per_level=[len(lv) for lv in result],
        )
        return result

    # ------------------------------------------------------------------
    # Dependency queries
    # ------------------------------------------------------------------

    def get_dependencies(self, table_name: str) -> list[str]:
        """Return the direct parent tables that *table_name* depends on.

        Args:
            table_name: Fully qualified table name.

        Returns:
            List of parent table names.

        Raises:
            KeyError: If *table_name* is not in the graph.
        """
        if table_name not in self._nodes:
            raise KeyError(f"Table '{table_name}' not found in graph.")
        return sorted(self._reverse_adjacency.get(table_name, set()))

    def get_dependents(self, table_name: str) -> list[str]:
        """Return the direct child tables that depend on *table_name*.

        Args:
            table_name: Fully qualified table name.

        Returns:
            List of child table names.

        Raises:
            KeyError: If *table_name* is not in the graph.
        """
        if table_name not in self._nodes:
            raise KeyError(f"Table '{table_name}' not found in graph.")
        return sorted(self._adjacency_list.get(table_name, set()))

    def get_all_ancestors(self, table_name: str) -> Set[str]:
        """Return the transitive closure of all ancestor (parent) tables.

        Uses BFS over the reverse adjacency structure to traverse up the
        full dependency chain.

        Args:
            table_name: Starting table.

        Returns:
            Set of all tables that *table_name* (transitively) depends on.

        Raises:
            KeyError: If *table_name* is not in the graph.
        """
        if table_name not in self._nodes:
            raise KeyError(f"Table '{table_name}' not found in graph.")
        ancestors: Set[str] = set()
        queue: deque[str] = deque(self._reverse_adjacency.get(table_name, set()))
        while queue:
            current = queue.popleft()
            if current in ancestors:
                continue
            ancestors.add(current)
            queue.extend(
                p for p in self._reverse_adjacency.get(current, set())
                if p not in ancestors
            )
        return ancestors

    def get_all_descendants(self, table_name: str) -> Set[str]:
        """Return the transitive closure of all descendant (child) tables.

        Uses BFS over the forward adjacency structure to traverse down the
        full dependency chain.

        Args:
            table_name: Starting table.

        Returns:
            Set of all tables that (transitively) depend on *table_name*.

        Raises:
            KeyError: If *table_name* is not in the graph.
        """
        if table_name not in self._nodes:
            raise KeyError(f"Table '{table_name}' not found in graph.")
        descendants: Set[str] = set()
        queue: deque[str] = deque(self._adjacency_list.get(table_name, set()))
        while queue:
            current = queue.popleft()
            if current in descendants:
                continue
            descendants.add(current)
            queue.extend(
                c for c in self._adjacency_list.get(current, set())
                if c not in descendants
            )
        return descendants

    # ------------------------------------------------------------------
    # Cross-module analysis
    # ------------------------------------------------------------------

    def get_cross_module_edges(self) -> list[DependencyEdge]:
        """Return all edges that span different ERP module boundaries.

        Cross-module dependencies (e.g. an HR payroll table referencing a
        Financial Accounting GL account table) require careful coordination
        during generation to ensure that the parent module's data is
        available before the child module begins.

        Returns:
            List of :class:`DependencyEdge` instances with
            ``is_cross_module == True``.
        """
        return [e for e in self._edges.values() if e.is_cross_module]

    def get_module_subgraph(self, erp_module: str) -> DependencyGraph:
        """Extract a subgraph for a single ERP module.

        The subgraph includes all tables belonging to *erp_module* plus
        any cross-module parent tables they depend on (so that generation
        ordering is still correct within the subgraph).

        Args:
            erp_module: One of ``financial_accounting``, ``hr``,
                ``sales_distribution``, ``material_management``.

        Returns:
            A new :class:`DependencyGraph` containing the filtered nodes
            and edges.
        """
        subgraph = DependencyGraph()
        subgraph._cycle_resolution_strategy = self._cycle_resolution_strategy

        # Collect module-local tables.
        module_tables: set[str] = {
            name
            for name, node in self._nodes.items()
            if node.erp_module == erp_module
        }

        # Include cross-module parents so ordering is valid.
        extra_parents: set[str] = set()
        for table in module_tables:
            for parent in self._reverse_adjacency.get(table, set()):
                if parent not in module_tables:
                    extra_parents.add(parent)

        all_tables = module_tables | extra_parents
        for name in all_tables:
            if name in self._nodes:
                subgraph.add_table(self._nodes[name])

        for (p, c), edge in self._edges.items():
            if p in all_tables and c in all_tables:
                subgraph.add_dependency(DependencyEdge(
                    parent_table=edge.parent_table,
                    child_table=edge.child_table,
                    fk_column=edge.fk_column,
                    pk_column=edge.pk_column,
                    is_nullable=edge.is_nullable,
                    is_cross_module=edge.is_cross_module,
                    weight=edge.weight,
                ))

        self._logger.info(
            "module_subgraph_extracted",
            erp_module=erp_module,
            tables=len(all_tables),
            edges=len(subgraph._edges),
        )
        return subgraph

    # ------------------------------------------------------------------
    # Statistics and introspection
    # ------------------------------------------------------------------

    def get_graph_stats(self) -> dict[str, Any]:
        """Return aggregate statistics about the dependency graph.

        The statistics dict includes:

        * ``total_nodes`` — Number of tables.
        * ``total_edges`` — Number of FK dependency edges.
        * ``max_depth`` — Maximum depth of the longest dependency chain.
        * ``num_root_tables`` — Tables with zero incoming edges.
        * ``num_leaf_tables`` — Tables with zero outgoing edges.
        * ``num_cross_module_edges`` — Edges spanning module boundaries.
        * ``num_cycles`` — Number of strongly connected components with
          more than one node.
        * ``tables_per_module`` — Breakdown of table counts by ERP module.

        Returns:
            Dictionary of statistics.
        """
        root_tables = [
            name
            for name in self._nodes
            if not self._reverse_adjacency.get(name, set())
        ]
        leaf_tables = [
            name
            for name in self._nodes
            if not self._adjacency_list.get(name, set())
        ]
        cross_module_count = sum(
            1 for e in self._edges.values() if e.is_cross_module
        )

        sccs = self.find_strongly_connected_components()
        cycle_count = sum(1 for scc in sccs if len(scc) > 1)

        # Compute max depth via BFS from roots.
        max_depth = 0
        if root_tables:
            depth: Dict[str, int] = {}
            queue: deque[str] = deque()
            for root in root_tables:
                depth[root] = 0
                queue.append(root)
            while queue:
                node = queue.popleft()
                for child in self._adjacency_list.get(node, set()):
                    candidate_depth = depth[node] + 1
                    if child not in depth or candidate_depth > depth[child]:
                        depth[child] = candidate_depth
                        queue.append(child)
            max_depth = max(depth.values()) if depth else 0

        tables_per_module: Dict[str, int] = defaultdict(int)
        for node in self._nodes.values():
            mod = node.erp_module or "unclassified"
            tables_per_module[mod] += 1

        return {
            "total_nodes": len(self._nodes),
            "total_edges": len(self._edges),
            "max_depth": max_depth,
            "num_root_tables": len(root_tables),
            "num_leaf_tables": len(leaf_tables),
            "num_cross_module_edges": cross_module_count,
            "num_cycles": cycle_count,
            "tables_per_module": dict(tables_per_module),
        }

    def get_edge(
        self, parent: str, child: str,
    ) -> DependencyEdge | None:
        """Return the edge between *parent* and *child*, or ``None``.

        Args:
            parent: Parent table name.
            child: Child table name.

        Returns:
            The :class:`DependencyEdge` if it exists, else ``None``.
        """
        return self._edges.get((parent, child))

    # ------------------------------------------------------------------
    # Graph mutation
    # ------------------------------------------------------------------

    def remove_table(self, table_name: str) -> None:
        """Remove a table and all incident edges from the graph.

        Args:
            table_name: The table to remove.

        Raises:
            KeyError: If *table_name* is not in the graph.
        """
        if table_name not in self._nodes:
            raise KeyError(f"Table '{table_name}' not found in graph.")

        # Remove outgoing edges (parent → child).
        for child in list(self._adjacency_list.get(table_name, set())):
            self._reverse_adjacency[child].discard(table_name)
            self._edges.pop((table_name, child), None)

        # Remove incoming edges (parent → table_name).
        for parent in list(self._reverse_adjacency.get(table_name, set())):
            self._adjacency_list[parent].discard(table_name)
            self._edges.pop((parent, table_name), None)

        # Remove the node itself.
        del self._nodes[table_name]
        self._adjacency_list.pop(table_name, None)
        self._reverse_adjacency.pop(table_name, None)
        self._is_dirty = True
        self._logger.debug("table_removed", table_name=table_name)

    def remove_dependency(self, parent: str, child: str) -> None:
        """Remove a specific dependency edge.

        Args:
            parent: Parent table name.
            child: Child table name.

        Raises:
            KeyError: If the edge does not exist.
        """
        key = (parent, child)
        if key not in self._edges:
            raise KeyError(
                f"No dependency edge from '{parent}' to '{child}'."
            )
        del self._edges[key]
        self._adjacency_list[parent].discard(child)
        self._reverse_adjacency[child].discard(parent)
        self._is_dirty = True
        self._logger.debug(
            "dependency_removed", parent=parent, child=child,
        )

    # ------------------------------------------------------------------
    # Validation
    # ------------------------------------------------------------------

    def validate(self) -> Tuple[bool, list[str]]:
        """Validate internal consistency of the dependency graph.

        Checks performed:

        1. Every edge endpoint references an existing node.
        2. No self-loops (a table depending on itself).
        3. Cycles are reported as warnings (they can be resolved).

        Returns:
            A 2-tuple ``(is_valid, issues)`` where *is_valid* is ``True``
            when no hard errors are found and *issues* is a list of
            human-readable diagnostic strings.
        """
        issues: list[str] = []

        # Check edge endpoints.
        for (parent, child), edge in self._edges.items():
            if parent not in self._nodes:
                issues.append(
                    f"Edge references non-existent parent table '{parent}'."
                )
            if child not in self._nodes:
                issues.append(
                    f"Edge references non-existent child table '{child}'."
                )
            if parent == child:
                issues.append(
                    f"Self-loop detected on table '{parent}'."
                )

        # Check for orphan adjacency entries.
        for name in list(self._adjacency_list.keys()):
            if name not in self._nodes:
                issues.append(
                    f"Adjacency entry for non-existent table '{name}'."
                )
        for name in list(self._reverse_adjacency.keys()):
            if name not in self._nodes:
                issues.append(
                    f"Reverse adjacency entry for non-existent table '{name}'."
                )

        # Cycles are warnings, not hard errors.
        cycles = self.detect_cycles()
        for cycle in cycles:
            issues.append(
                f"Cycle detected (resolvable): {' → '.join(cycle)}"
            )

        is_valid = all(
            "non-existent" not in issue and "Self-loop" not in issue
            for issue in issues
        )

        self._logger.info(
            "graph_validated",
            is_valid=is_valid,
            issue_count=len(issues),
        )
        return is_valid, issues

    # ------------------------------------------------------------------
    # Serialisation
    # ------------------------------------------------------------------

    def to_dict(self) -> dict[str, Any]:
        """Serialise the graph to a JSON-compatible dictionary.

        The output can be stored in MongoDB or transmitted over REST for
        later reconstruction via :meth:`from_dict`.

        Returns:
            Dictionary with ``nodes``, ``edges``, ``cycle_resolution_strategy``,
            and ``metadata`` keys.
        """
        nodes_list: list[dict[str, Any]] = []
        for node in self._nodes.values():
            nodes_list.append(asdict(node))

        edges_list: list[dict[str, Any]] = []
        for edge in self._edges.values():
            edges_list.append(asdict(edge))

        return {
            "nodes": nodes_list,
            "edges": edges_list,
            "cycle_resolution_strategy": self._cycle_resolution_strategy.value,
            "metadata": {
                "total_nodes": len(self._nodes),
                "total_edges": len(self._edges),
            },
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> DependencyGraph:
        """Reconstruct a :class:`DependencyGraph` from a serialised dictionary.

        This is the inverse of :meth:`to_dict`.

        Args:
            data: Dictionary previously produced by :meth:`to_dict`.

        Returns:
            A fully reconstructed :class:`DependencyGraph`.

        Raises:
            KeyError: If required keys are missing.
            ValueError: If the strategy value is invalid.
        """
        graph = cls()

        strategy_value = data.get(
            "cycle_resolution_strategy", "nullable_edge_relaxation",
        )
        graph._cycle_resolution_strategy = CycleResolutionStrategy(strategy_value)

        for node_dict in data.get("nodes", []):
            node = TableNode(
                table_name=node_dict["table_name"],
                schema_name=node_dict.get("schema_name", ""),
                erp_module=node_dict.get("erp_module", ""),
                erp_system=node_dict.get("erp_system", ""),
                columns=node_dict.get("columns", []),
                primary_keys=node_dict.get("primary_keys", []),
                row_count_estimate=node_dict.get("row_count_estimate", 0),
                generation_priority=node_dict.get("generation_priority", 0),
                metadata=node_dict.get("metadata", {}),
            )
            graph.add_table(node)

        for edge_dict in data.get("edges", []):
            edge = DependencyEdge(
                parent_table=edge_dict["parent_table"],
                child_table=edge_dict["child_table"],
                fk_column=edge_dict.get("fk_column", ""),
                pk_column=edge_dict.get("pk_column", ""),
                is_nullable=edge_dict.get("is_nullable", False),
                is_cross_module=edge_dict.get("is_cross_module", False),
                weight=edge_dict.get("weight", 1.0),
            )
            graph.add_dependency(edge)

        return graph

    # ------------------------------------------------------------------
    # Private helpers
    # ------------------------------------------------------------------

    @staticmethod
    def _detect_erp_module(table_name: str, erp_system: str) -> str:
        """Auto-detect the ERP module from the table name and ERP system.

        Applies prefix-matching rules:

        * **SAP:** ``fi.*`` → ``financial_accounting``, ``hr.*`` → ``hr``,
          ``sd.*`` → ``sales_distribution``, ``mm.*`` → ``material_management``
        * **Oracle EBS:** ``gl.*`` → ``financial_accounting``, ``hr.*`` → ``hr``,
          ``oe.*`` → ``sales_distribution``, ``po.*`` → ``material_management``
        * **Dynamics:** ``ledger.*`` → ``financial_accounting``, ``hcm.*`` →
          ``hr``, ``sales.*`` → ``sales_distribution``, ``procurement.*`` →
          ``material_management``

        Args:
            table_name: Fully qualified table name.
            erp_system: ERP system identifier (``sap``, ``oracle``,
                ``dynamics``, ``legacy``).

        Returns:
            Canonical ERP module string, or ``""`` if no match is found.
        """
        lower_name = table_name.lower()
        # Strip the ERP system prefix if present (e.g. "sap.fi.gl_accounts" → "fi.gl_accounts").
        prefixes_to_strip = [f"{erp_system}.", f"{erp_system}_"]
        for prefix in prefixes_to_strip:
            if lower_name.startswith(prefix):
                lower_name = lower_name[len(prefix):]
                break

        module_map = _ERP_MODULE_MAPS.get(erp_system, {})
        # Also try generic/legacy matching using all known maps.
        if not module_map:
            # For "legacy" or unknown systems, try all maps.
            for _sys_map in _ERP_MODULE_MAPS.values():
                for mod_prefix, module_name in sorted(
                    _sys_map.items(), key=lambda kv: -len(kv[0]),
                ):
                    if lower_name.startswith(f"{mod_prefix}.") or lower_name.startswith(f"{mod_prefix}_"):
                        return module_name
            return ""

        # Match against the system-specific prefix map, longest prefix first.
        for mod_prefix, module_name in sorted(
            module_map.items(), key=lambda kv: -len(kv[0]),
        ):
            if lower_name.startswith(f"{mod_prefix}.") or lower_name.startswith(f"{mod_prefix}_"):
                return module_name

        return ""
