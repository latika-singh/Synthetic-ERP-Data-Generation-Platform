"""Table dependency ordering analysis for the Profiling Service.

Provides the :class:`DependencyAnalyzer` that performs topological sorting on
table relationships to determine a valid synthetic-data **generation order**.
Using foreign-key relationships discovered by
:class:`~profiling_service.discovery.relationship_mapper.RelationshipMapper`,
the analyser builds a directed acyclic graph (DAG) of table dependencies and
applies **Kahn's algorithm** (BFS-based topological sort) to produce an
ordering where *parent / referenced tables* are generated **before** their
*child / referencing tables*.

Without correct generation ordering, foreign-key constraints would be violated
because child records would reference non-existent parent records.

**Result models** (all Pydantic 2.x):

* :class:`DependencyLevel` — tables grouped by depth in the dependency tree.
* :class:`CycleInfo` — circular dependency detection result with a suggested
  break-point.
* :class:`DependencyAnalysisResult` — the complete analysis output consumed
  by the Generation Engine to drive ordered, batch-parallel generation.

**Constraint C-001 compliance:**
    This module operates solely on structural metadata (table names and
    relationship definitions).  No production data is accessed or stored.

**Constraint C-005:**
    Supports the four initial-release ERP modules — Financial Accounting,
    Human Resources, Sales & Distribution, Material Management.

Algorithms:
    * **Kahn's algorithm** — BFS topological sort with O(V + E) complexity.
    * **Tarjan-style DFS** — back-edge detection for cycle discovery.
    * **Greedy cycle-breaking** — removes the edge entering the node with
      the highest in-degree in the cycle, minimising generation-order impact.

Usage::

    from profiling_service.discovery.dependency_analyzer import (
        DependencyAnalyzer,
        DependencyAnalysisResult,
    )

    analyzer = DependencyAnalyzer()
    result = analyzer.analyze(schema_definition)
    for table in result.generation_order:
        generate(table)
"""

from __future__ import annotations

import time
from collections import defaultdict, deque
from typing import TYPE_CHECKING

from pydantic import BaseModel, ConfigDict, Field

from shared.logging.structured_logger import get_logger


if TYPE_CHECKING:
    from profiling_service.models.schema_definition import SchemaDefinition


# ---------------------------------------------------------------------------
# Module-level logger
# ---------------------------------------------------------------------------

_logger = get_logger(__name__)


# ===================================================================
# Pydantic result models
# ===================================================================


class DependencyLevel(BaseModel):
    """Tables grouped at the same dependency depth.

    Tables at a given level depend *only* on tables at lower levels.
    This means all tables within the same level can safely be generated
    **in parallel** without violating foreign-key constraints.

    Attributes:
        level: Zero-based depth.  ``0`` = root / independent tables.
        tables: Table names at this level.
        description: Human-readable explanation of the level.
    """

    level: int = Field(..., ge=0, description="Zero-based dependency depth")
    tables: list[str] = Field(default_factory=list, description="Tables at this level")
    description: str = Field(
        default="",
        description="Human-readable description of this dependency level",
    )


class CycleInfo(BaseModel):
    """Details of a detected circular dependency among tables.

    When the dependency graph contains cycles, generation cannot proceed
    with a simple topological order.  The analyser detects cycles and
    suggests a *break-point* — the table whose incoming edge should be
    deferred (nullable FK, post-generation back-fill) to convert the
    cyclic sub-graph into a DAG.

    Attributes:
        cycle_tables: Ordered list of table names forming the cycle.
        suggested_break_point: Table name where the cycle should be broken.
        reason: Explanation of why this break-point was chosen.
    """

    cycle_tables: list[str] = Field(
        ..., min_length=1, description="Tables forming the circular dependency"
    )
    suggested_break_point: str = Field(
        ..., description="Table where the cycle should be broken"
    )
    reason: str = Field(
        default="",
        description="Explanation of the cycle and why this break-point was chosen",
    )


class DependencyAnalysisResult(BaseModel):
    """Complete output of a dependency analysis run.

    Consumed by the **Generation Engine** to drive ordered, batch-parallel
    synthetic data generation, and by the **Profiling Service** routes to
    expose dependency metadata via the REST API.

    Attributes:
        generation_order: Flat list of table names in a valid topological
            generation sequence (parent tables first).
        dependency_levels: Tables grouped by dependency depth.
        root_tables: Tables with **no** incoming FK dependencies
            (generate first).
        leaf_tables: Tables with **no** outgoing FK references
            (generate last).
        has_cycles: Whether circular dependencies were detected.
        cycles: Details of detected cycles, if any.
        total_tables: Total table count in the analysed schema.
        total_relationships: Total FK relationship count.
        max_depth: Maximum depth in the dependency tree.
        analysis_duration_seconds: Wall-clock time of the analysis.
    """

    model_config = ConfigDict(frozen=False)

    generation_order: list[str] = Field(default_factory=list)
    dependency_levels: list[DependencyLevel] = Field(default_factory=list)
    root_tables: list[str] = Field(default_factory=list)
    leaf_tables: list[str] = Field(default_factory=list)
    has_cycles: bool = False
    cycles: list[CycleInfo] = Field(default_factory=list)
    total_tables: int = 0
    total_relationships: int = 0
    max_depth: int = 0
    analysis_duration_seconds: float | None = None


# ===================================================================
# DependencyAnalyzer
# ===================================================================


class DependencyAnalyzer:
    """Builds a dependency graph from schema relationships and computes a
    valid generation order via Kahn's topological-sort algorithm.

    The analyser also detects circular dependencies (back-edges) and
    applies a greedy cycle-breaking strategy so that a valid topological
    order can always be produced.

    Usage::

        analyzer = DependencyAnalyzer()
        result = analyzer.analyze(schema)
        # result.generation_order -> ['COUNTRIES', 'CUSTOMERS', 'ORDERS', ...]
    """

    # ---------------------------------------------------------------
    # Initialisation
    # ---------------------------------------------------------------

    def __init__(self) -> None:
        """Initialise internal graph structures."""
        self._logger = get_logger(__name__)

        # Adjacency: table → set of tables it **depends on** (FK targets).
        self._adjacency: dict[str, set[str]] = defaultdict(set)
        # Reverse adjacency: table → set of tables that **depend on it**.
        self._reverse_adjacency: dict[str, set[str]] = defaultdict(set)
        # In-degree: incoming edge count per table.
        self._in_degree: dict[str, int] = defaultdict(int)
        # Set of all known table names (including isolated ones).
        self._all_tables: set[str] = set()

        self._logger.info("dependency_analyzer_initialised")

    # ---------------------------------------------------------------
    # Public API — analyze
    # ---------------------------------------------------------------

    def analyze(self, schema: SchemaDefinition) -> DependencyAnalysisResult:
        """Analyse table dependencies and compute generation order.

        This is the **main entry point**.  It performs the following steps:

        1. Build a directed adjacency graph from the schema's
           :class:`RelationshipDefinition` entries.
        2. Detect cycles via DFS back-edge detection.
        3. Break detected cycles by removing edges entering the
           highest-in-degree node in each cycle.
        4. Run Kahn's topological sort to produce ``generation_order``.
        5. Compute ``dependency_levels`` (BFS from root tables).
        6. Identify ``root_tables`` and ``leaf_tables``.

        Args:
            schema: A :class:`SchemaDefinition` containing tables and
                relationships.

        Returns:
            A :class:`DependencyAnalysisResult` with the complete
            analysis output.
        """
        start = time.time()

        # Step 1 — build graph
        self._build_adjacency_graph(schema)

        # Step 2 — detect cycles
        cycles = self._detect_cycles()

        # Step 3 — break cycles if any
        if cycles:
            self._break_cycles(cycles)
            self._logger.warning(
                "dependency_cycles_broken",
                cycle_count=len(cycles),
                cycles=[c.model_dump() for c in cycles],
            )

        # Step 4 — topological sort
        generation_order = self._topological_sort()

        # Step 5 — dependency levels
        dependency_levels = self._compute_dependency_levels(generation_order)

        # Step 6 — root / leaf tables
        root_tables = [
            t for t in self._all_tables
            if self._in_degree.get(t, 0) == 0
        ]
        leaf_tables = [
            t for t in self._all_tables
            if not self._reverse_adjacency.get(t)
        ]

        max_depth = max(
            (dl.level for dl in dependency_levels), default=0
        )

        total_edges = sum(len(deps) for deps in self._adjacency.values())

        duration = round(time.time() - start, 6)

        result = DependencyAnalysisResult(
            generation_order=generation_order,
            dependency_levels=dependency_levels,
            root_tables=sorted(root_tables),
            leaf_tables=sorted(leaf_tables),
            has_cycles=len(cycles) > 0,
            cycles=cycles,
            total_tables=len(self._all_tables),
            total_relationships=total_edges,
            max_depth=max_depth,
            analysis_duration_seconds=duration,
        )

        self._logger.info(
            "dependency_analysis_complete",
            total_tables=result.total_tables,
            total_relationships=result.total_relationships,
            generation_order_length=len(generation_order),
            root_tables=len(root_tables),
            leaf_tables=len(leaf_tables),
            max_depth=max_depth,
            has_cycles=result.has_cycles,
            duration_seconds=duration,
        )
        return result

    # ---------------------------------------------------------------
    # Public API — convenience queries
    # ---------------------------------------------------------------

    def get_tables_at_level(
        self,
        result: DependencyAnalysisResult,
        level: int,
    ) -> list[str]:
        """Return tables at a specific dependency level.

        Tables at the same level can be generated **in parallel** without
        violating foreign-key constraints.

        Args:
            result: A previously computed analysis result.
            level: The zero-based dependency depth.

        Returns:
            List of table names at the requested level, or an empty list
            if the level does not exist.
        """
        for dl in result.dependency_levels:
            if dl.level == level:
                return list(dl.tables)
        return []

    def get_dependencies_for_table(
        self,
        schema: SchemaDefinition,
        table_name: str,
    ) -> list[str]:
        """Return tables that must be generated *before* ``table_name``.

        These are the tables that ``table_name`` references via foreign keys
        (i.e. its parent / target tables).

        Args:
            schema: The schema definition (used to ensure graph is built).
            table_name: The table to query.

        Returns:
            Sorted list of dependency table names.
        """
        # Ensure the graph is populated for this schema.
        if not self._all_tables:
            self._build_adjacency_graph(schema)
        return sorted(self._adjacency.get(table_name, set()))

    def get_dependents_for_table(
        self,
        schema: SchemaDefinition,
        table_name: str,
    ) -> list[str]:
        """Return tables that **depend on** ``table_name``.

        These are the tables whose foreign keys reference ``table_name``
        (i.e. its child / source tables).

        Args:
            schema: The schema definition (used to ensure graph is built).
            table_name: The table to query.

        Returns:
            Sorted list of dependent table names.
        """
        if not self._all_tables:
            self._build_adjacency_graph(schema)
        return sorted(self._reverse_adjacency.get(table_name, set()))

    def get_generation_batches(
        self,
        result: DependencyAnalysisResult,
        batch_size: int = 10,
    ) -> list[list[str]]:
        """Group the generation order into batches respecting dependency levels.

        Tables from different dependency levels are **never** placed in the
        same batch, ensuring that all parent records exist before child
        records are generated.

        Args:
            result: A previously computed analysis result.
            batch_size: Maximum number of tables per batch.

        Returns:
            Nested list of table-name batches.
        """
        batches: list[list[str]] = []
        for dl in result.dependency_levels:
            level_tables = list(dl.tables)
            for i in range(0, len(level_tables), batch_size):
                batches.append(level_tables[i : i + batch_size])
        return batches

    # ---------------------------------------------------------------
    # Private — graph construction
    # ---------------------------------------------------------------

    def _build_adjacency_graph(self, schema: SchemaDefinition) -> None:
        """Build a directed graph from the schema's relationships.

        Edge semantics:

        * ``A → B`` means *table A depends on table B* (A has an FK
          referencing B).
        * ``_adjacency[A]`` contains all tables A depends on.
        * ``_reverse_adjacency[B]`` contains all tables that depend on B.

        All tables from ``schema.tables`` are registered — even those
        with no relationships — so they appear in the final generation
        order.

        Args:
            schema: A :class:`SchemaDefinition` with ``tables`` and
                ``relationships``.
        """
        # Reset internal state.
        self._adjacency = defaultdict(set)
        self._reverse_adjacency = defaultdict(set)
        self._in_degree = defaultdict(int)
        self._all_tables = set()

        # Register all tables (including isolated ones).
        for table_def in schema.tables:
            name = table_def.table_name
            self._all_tables.add(name)
            # Ensure every table has an entry in in_degree.
            if name not in self._in_degree:
                self._in_degree[name] = 0

        # Build edges from relationships.
        for rel in schema.relationships:
            src = rel.source_table  # the FK-holding (child) table
            tgt = rel.target_table  # the referenced (parent) table

            # Ensure both endpoints exist in the graph.
            self._all_tables.add(src)
            self._all_tables.add(tgt)
            if src not in self._in_degree:
                self._in_degree[src] = 0
            if tgt not in self._in_degree:
                self._in_degree[tgt] = 0

            # Skip self-references — they don't affect topological order.
            if src == tgt:
                self._logger.debug(
                    "self_reference_skipped",
                    table=src,
                )
                continue

            # Edge: src depends on tgt → src → tgt in the adjacency.
            if tgt not in self._adjacency[src]:
                self._adjacency[src].add(tgt)
                self._reverse_adjacency[tgt].add(src)
                self._in_degree[src] += 1

        total_edges = sum(len(deps) for deps in self._adjacency.values())
        self._logger.info(
            "adjacency_graph_built",
            total_nodes=len(self._all_tables),
            total_edges=total_edges,
        )

    # ---------------------------------------------------------------
    # Private — topological sort (Kahn's algorithm)
    # ---------------------------------------------------------------

    def _topological_sort(self) -> list[str]:
        """Compute topological order using Kahn's BFS algorithm.

        Nodes with ``in_degree == 0`` are enqueued first (root / parent
        tables with no dependencies).  For each dequeued node, all
        dependent nodes have their in-degree decremented; when a
        dependent's in-degree reaches zero it is enqueued.

        The result is a generation order where every parent table
        appears **before** the child tables that reference it.

        Returns:
            Ordered list of table names.
        """
        # Work on a mutable copy of in-degree.
        in_deg = dict(self._in_degree)

        queue: deque[str] = deque()
        for table in sorted(self._all_tables):
            if in_deg.get(table, 0) == 0:
                queue.append(table)

        order: list[str] = []
        while queue:
            current = queue.popleft()
            order.append(current)

            # For each table that depends on `current`, decrement in-degree.
            for dependent in sorted(self._reverse_adjacency.get(current, set())):
                in_deg[dependent] -= 1
                if in_deg[dependent] == 0:
                    queue.append(dependent)

        if len(order) != len(self._all_tables):
            # Remaining nodes form residual cycles (should not happen
            # after _break_cycles, but handle gracefully).
            missing = self._all_tables - set(order)
            self._logger.warning(
                "topological_sort_incomplete",
                sorted_count=len(order),
                total_count=len(self._all_tables),
                missing_tables=sorted(missing),
            )
            # Append missing tables in alphabetical order as a fallback.
            order.extend(sorted(missing))

        return order

    # ---------------------------------------------------------------
    # Private — dependency level computation
    # ---------------------------------------------------------------

    def _compute_dependency_levels(
        self,
        generation_order: list[str],
    ) -> list[DependencyLevel]:
        """Group tables by their dependency depth using BFS from roots.

        * **Level 0:** Tables with no incoming dependencies (root tables).
        * **Level N:** Tables whose *all* dependencies are at level < N.

        Args:
            generation_order: The topological order produced by Kahn's
                algorithm (used as fallback ordering).

        Returns:
            List of :class:`DependencyLevel` instances.
        """
        # Compute level per table via BFS.
        levels: dict[str, int] = {}

        # Initialise roots at level 0.
        queue: deque[str] = deque()
        for table in sorted(self._all_tables):
            if self._in_degree.get(table, 0) == 0:
                levels[table] = 0
                queue.append(table)

        while queue:
            current = queue.popleft()
            current_level = levels[current]
            for dependent in sorted(self._reverse_adjacency.get(current, set())):
                # A dependent's level is the max of all its dependency
                # levels + 1.
                new_level = current_level + 1
                if dependent not in levels or levels[dependent] < new_level:
                    levels[dependent] = new_level
                    queue.append(dependent)

        # Any table not reached by BFS (isolated or cycle remnant) gets
        # appended at the next level.
        max_existing = max(levels.values(), default=-1)
        for table in generation_order:
            if table not in levels:
                levels[table] = max_existing + 1

        # Group by level.
        level_groups: dict[int, list[str]] = defaultdict(list)
        for table, lvl in levels.items():
            level_groups[lvl].append(table)

        result: list[DependencyLevel] = []
        for lvl in sorted(level_groups):
            tables = sorted(level_groups[lvl])
            if lvl == 0:
                desc = "Root tables with no foreign-key dependencies (generate first)"
            else:
                desc = f"Tables depending on level {lvl - 1} or lower"
            result.append(DependencyLevel(level=lvl, tables=tables, description=desc))

        return result

    # ---------------------------------------------------------------
    # Private — cycle detection (DFS back-edge)
    # ---------------------------------------------------------------

    def _detect_cycles(self) -> list[CycleInfo]:
        """Detect circular dependencies via DFS back-edge detection.

        For every unvisited node a DFS is started.  If a node on the
        current recursion stack is revisited, a back-edge (cycle) has
        been found.  The cycle path is extracted and recorded as a
        :class:`CycleInfo`.

        Returns:
            List of :class:`CycleInfo` — empty if the graph is acyclic.
        """
        WHITE, GREY, BLACK = 0, 1, 2  # noqa: N806
        colour: dict[str, int] = dict.fromkeys(self._all_tables, WHITE)
        parent: dict[str, str | None] = dict.fromkeys(self._all_tables)
        cycles: list[CycleInfo] = []

        def _dfs(node: str) -> None:
            colour[node] = GREY
            for dep in sorted(self._adjacency.get(node, set())):
                if colour.get(dep, WHITE) == GREY:
                    # Back-edge → cycle detected.
                    cycle_path = _extract_cycle_path(node, dep, parent)
                    if cycle_path:
                        break_point = self._choose_break_point(cycle_path)
                        cycles.append(CycleInfo(
                            cycle_tables=cycle_path,
                            suggested_break_point=break_point,
                            reason=(
                                f"Circular dependency detected: "
                                f"{' → '.join(cycle_path)} → {cycle_path[0]}. "
                                f"Suggested break at '{break_point}' (highest "
                                f"in-degree in the cycle)."
                            ),
                        ))
                        self._logger.warning(
                            "cycle_detected",
                            cycle=cycle_path,
                            break_point=break_point,
                        )
                elif colour.get(dep, WHITE) == WHITE:
                    parent[dep] = node
                    _dfs(dep)
            colour[node] = BLACK

        def _extract_cycle_path(
            start: str,
            end: str,
            parent_map: dict[str, str | None],
        ) -> list[str]:
            """Trace the parent chain from *start* back to *end*."""
            path = [end]
            current: str | None = start
            while current is not None and current != end:
                path.append(current)
                current = parent_map.get(current)
            path.append(end)
            path.reverse()
            # Remove duplicate trailing entry.
            if len(path) > 1 and path[0] == path[-1]:
                path = path[:-1]
            return path

        for table in sorted(self._all_tables):
            if colour.get(table, WHITE) == WHITE:
                _dfs(table)

        return cycles

    # ---------------------------------------------------------------
    # Private — cycle breaking
    # ---------------------------------------------------------------

    def _choose_break_point(self, cycle_tables: list[str]) -> str:
        """Choose the best table to break a cycle at.

        Strategy: select the table with the **highest in-degree** within
        the cycle.  Breaking an edge entering a highly-referenced table
        minimises disruption to the rest of the dependency graph because
        the table is likely a "hub" that many other tables depend on.

        Args:
            cycle_tables: Ordered list of table names in the cycle.

        Returns:
            The table name chosen as the break-point.
        """
        best_table = cycle_tables[0]
        best_in_deg = self._in_degree.get(best_table, 0)
        for table in cycle_tables[1:]:
            in_deg = self._in_degree.get(table, 0)
            if in_deg > best_in_deg:
                best_in_deg = in_deg
                best_table = table
        return best_table

    def _break_cycles(self, cycles: list[CycleInfo]) -> None:
        """Remove edges to break all detected cycles.

        For each cycle, the edge **entering** the suggested break-point
        from the preceding table in the cycle is removed from the
        adjacency graph.

        Args:
            cycles: List of :class:`CycleInfo` with break-point suggestions.
        """
        for cycle_info in cycles:
            bp = cycle_info.suggested_break_point
            cycle_tables = cycle_info.cycle_tables

            # Find the predecessor of the break-point in the cycle.
            bp_idx = None
            for i, t in enumerate(cycle_tables):
                if t == bp:
                    bp_idx = i
                    break

            if bp_idx is None:
                continue

            # The predecessor is the table that has an edge TO the
            # break-point (i.e. break-point depends on predecessor).
            # In the adjacency graph edge semantics: bp → predecessor.
            # So we remove `predecessor` from `_adjacency[bp]`.
            predecessor_idx = (bp_idx - 1) % len(cycle_tables)
            predecessor = cycle_tables[predecessor_idx]

            if predecessor in self._adjacency.get(bp, set()):
                self._adjacency[bp].discard(predecessor)
                self._reverse_adjacency[predecessor].discard(bp)
                self._in_degree[bp] = max(self._in_degree.get(bp, 1) - 1, 0)
                self._logger.info(
                    "cycle_edge_removed",
                    from_table=bp,
                    to_table=predecessor,
                    reason=f"Breaking cycle at '{bp}'",
                )
            # Try the reverse direction: predecessor → bp.
            # (adjacency semantics: predecessor depends on bp.)
            elif bp in self._adjacency.get(predecessor, set()):
                self._adjacency[predecessor].discard(bp)
                self._reverse_adjacency[bp].discard(predecessor)
                self._in_degree[predecessor] = max(
                    self._in_degree.get(predecessor, 1) - 1, 0
                )
                self._logger.info(
                    "cycle_edge_removed",
                    from_table=predecessor,
                    to_table=bp,
                    reason=f"Breaking cycle at '{bp}' (reverse direction)",
                )
