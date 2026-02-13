"""Foreign-key and relationship discovery for the Profiling Service.

Provides the :class:`RelationshipMapper` that discovers FK relationships
across tables and ERP modules, converting raw connector-level
:class:`~profiling_service.connectors.base.RelationshipMetadata` into
domain-model :class:`~profiling_service.models.schema_definition.RelationshipDefinition`
objects suitable for persistence and consumption by the Generation Engine.

**Referential Integrity**

Without discovered relationships the Generation Engine cannot maintain
FK integrity during synthetic data generation — child records would
contain invalid references to non-existent parent records.  This module
ensures:

* **Intra-module** relationships (e.g. ``GL_JE_HEADERS → GL_JE_LINES``
  within Financial Accounting) are captured.
* **Cross-module** relationships (e.g. ``SALES_ORDER → MATERIAL_MASTER``)
  are explicitly identified so the Generation Engine can schedule them
  across module boundaries.

**Constraint C-001 compliance:**
    Only FK *metadata* (table names, column names, cardinality, referential
    actions) is extracted.  No production data is accessed or stored.
    All connector calls are limited to :meth:`BaseConnector.discover_relationships`
    which queries catalog/dictionary metadata only.

**Constraint C-005:**
    Supports the four initial-release ERP modules — Financial Accounting,
    Human Resources, Sales & Distribution, Material Management.

**Multi-tenant isolation (R-007):**
    All MongoDB persistence operations are scoped by ``tenant_id`` through
    :class:`SchemaDefinitionRepository`.

Usage::

    from profiling_service.discovery.relationship_mapper import (
        RelationshipMapper,
        RELATIONSHIP_TYPE_MAPPING,
    )

    mapper = RelationshipMapper(tenant_id="tenant-001")
    relationships = mapper.discover_relationships(connector, schema)
    mapper.update_schema_relationships(schema.schema_id, relationships)
"""

from __future__ import annotations

import uuid
from collections import defaultdict
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any

from profiling_service.connectors import get_connector
from profiling_service.models.schema_definition import (
    RelationshipDefinition,
    RelationshipType,
    SchemaDefinition,
    SchemaDefinitionRepository,
)
from shared.logging.structured_logger import get_logger
from shared.middleware.circuit_breaker import circuit_breaker_decorator


if TYPE_CHECKING:
    from profiling_service.connectors.base import (
        BaseConnector,
        ConnectionConfig,
        RelationshipMetadata as ConnectorRelationship,
    )


# ---------------------------------------------------------------------------
# Module-level logger
# ---------------------------------------------------------------------------

_logger = get_logger(__name__)


# ---------------------------------------------------------------------------
# Constant: connector relationship_type → domain RelationshipType
# ---------------------------------------------------------------------------

RELATIONSHIP_TYPE_MAPPING: dict[str, RelationshipType | None] = {
    "FOREIGN_KEY": RelationshipType.ONE_TO_MANY,
    "ONE_TO_ONE": RelationshipType.ONE_TO_ONE,
    "ONE_TO_MANY": RelationshipType.ONE_TO_MANY,
    "MANY_TO_ONE": RelationshipType.ONE_TO_MANY,
    "MANY_TO_MANY": RelationshipType.MANY_TO_MANY,
    "CHECK": None,
    "NAVIGATION": RelationshipType.ONE_TO_MANY,
}
"""Maps connector-level relationship type strings to domain
:class:`RelationshipType` enums.

* ``FOREIGN_KEY`` → ONE_TO_MANY (default FK cardinality)
* ``ONE_TO_ONE`` / ``ONE_TO_MANY`` / ``MANY_TO_MANY`` → direct mapping
* ``MANY_TO_ONE`` → stored as ONE_TO_MANY (normalised direction)
* ``CHECK`` → ``None`` (skip — not an FK constraint)
* ``NAVIGATION`` → ONE_TO_MANY (OData navigation properties)
"""


# ===================================================================
# RelationshipMapper
# ===================================================================


class RelationshipMapper:
    """Discovers and maps FK relationships from ERP connectors into domain
    :class:`RelationshipDefinition` objects.

    *Lifecycle*:

    1. Instantiate with a ``tenant_id`` for multi-tenant isolation.
    2. Call :meth:`discover_relationships` with a connector and schema.
    3. Optionally call :meth:`update_schema_relationships` to persist
       the discovered relationships back into MongoDB.

    The mapper supports four ERP connector types (SAP, Oracle EBS,
    Dynamics 365, generic JDBC) through the :class:`BaseConnector`
    abstraction.  Connector instances can be created externally or
    via the convenience method :meth:`discover_relationships_from_config`.

    Attributes:
        tenant_id: Tenant identifier for all database operations.
    """

    # ---------------------------------------------------------------
    # Initialisation
    # ---------------------------------------------------------------

    def __init__(self, tenant_id: str) -> None:
        """Initialise the mapper for a specific tenant.

        Args:
            tenant_id: Tenant scope for MongoDB isolation.  All
                persistence operations via :class:`SchemaDefinitionRepository`
                are restricted to this tenant.

        Raises:
            ValueError: Propagated from :class:`SchemaDefinitionRepository`
                if ``tenant_id`` is empty.
        """
        self.tenant_id: str = tenant_id
        self._logger = get_logger(__name__)
        self._repo = SchemaDefinitionRepository(tenant_id)
        # Cache connector results per schema_name within a single
        # discovery session to avoid redundant ERP queries.
        self._schema_cache: dict[str | None, list[ConnectorRelationship]] = {}
        self._logger.info(
            "relationship_mapper_initialised",
            tenant_id=tenant_id,
        )

    # ---------------------------------------------------------------
    # Public — discover_relationships
    # ---------------------------------------------------------------

    def discover_relationships(
        self,
        connector: BaseConnector,
        schema: SchemaDefinition,
    ) -> list[RelationshipDefinition]:
        """Discover FK relationships across all tables in a schema.

        Queries the ERP connector for relationship metadata per unique
        database schema (``schema_name``), converts each returned
        :class:`~profiling_service.connectors.base.RelationshipMetadata`
        into a :class:`RelationshipDefinition` domain model, then
        deduplicates and validates all endpoints.

        **C-001:** Only :meth:`BaseConnector.discover_relationships` is
        called — a metadata-only operation.

        Args:
            connector: An active ERP connector instance (must already
                be connected).
            schema: The :class:`SchemaDefinition` containing the tables
                to inspect.

        Returns:
            A de-duplicated, validated list of
            :class:`RelationshipDefinition` objects.
        """
        start = datetime.now(UTC)
        raw_relationships: list[RelationshipDefinition] = []
        table_names: set[str] = {t.table_name for t in schema.tables}

        # Clear per-session cache so stale data from previous runs does
        # not leak into this discovery session.
        self._schema_cache.clear()

        # Collect unique schema_name values from the tables.  The
        # connector's discover_relationships() operates at the database-
        # schema level, so we call it once per unique schema_name.
        unique_schema_names: set[str | None] = {
            t.schema_name for t in schema.tables
        }

        for schema_name in unique_schema_names:
            try:
                connector_rels = self._call_connector_discover(
                    connector, schema_name,
                )
                for crel in connector_rels:
                    # Only include relationships where at least one
                    # endpoint belongs to our discovered table set.
                    if (
                        crel.source_table in table_names
                        or crel.target_table in table_names
                    ):
                        domain_rel = self._convert_relationship(crel, schema)
                        if domain_rel is not None:
                            raw_relationships.append(domain_rel)
            except Exception:
                self._logger.warning(
                    "relationship_discovery_schema_error",
                    schema_name=schema_name,
                    exc_info=True,
                )

        # De-duplicate and validate.
        deduped = self._deduplicate_relationships(raw_relationships)
        valid = [
            r for r in deduped
            if self._validate_relationship_endpoints(r, schema)
        ]

        cross_module_count = sum(
            1 for r in valid if self._is_cross_module(r, schema)
        )

        duration = (datetime.now(UTC) - start).total_seconds()
        self._logger.info(
            "relationship_discovery_complete",
            total_discovered=len(raw_relationships),
            after_dedup=len(deduped),
            after_validation=len(valid),
            cross_module_count=cross_module_count,
            duration_seconds=round(duration, 3),
            tenant_id=self.tenant_id,
        )
        return valid

    # ---------------------------------------------------------------
    # Public — discover_cross_module_relationships
    # ---------------------------------------------------------------

    def discover_cross_module_relationships(
        self,
        connector: BaseConnector,
        schema: SchemaDefinition,
    ) -> list[RelationshipDefinition]:
        """Identify relationships spanning ERP module boundaries.

        These cross-module relationships are critical for the Generation
        Engine to schedule generation across module boundaries while
        preserving referential integrity (e.g. a Sales Order referencing
        a Material Master record in a different module).

        Args:
            connector: An active ERP connector instance.
            schema: The :class:`SchemaDefinition` containing the tables.

        Returns:
            Filtered list containing *only* cross-module relationships.
        """
        all_rels = self.discover_relationships(connector, schema)
        cross = [r for r in all_rels if self._is_cross_module(r, schema)]
        self._logger.info(
            "cross_module_relationships_discovered",
            cross_module_count=len(cross),
            total_relationships=len(all_rels),
            tenant_id=self.tenant_id,
        )
        return cross

    # ---------------------------------------------------------------
    # Public — infer_relationship_type
    # ---------------------------------------------------------------

    def infer_relationship_type(
        self,
        source_table: str,
        source_columns: list[str],
        target_table: str,
        target_columns: list[str],
        schema: SchemaDefinition,
    ) -> RelationshipType:
        """Infer cardinality from structural metadata.

        Rules applied in order:

        1. If ``source_columns`` match the *entire* primary key of the
           source table → **ONE_TO_ONE** (the FK is a PK-based identifier
           link).
        2. If a *junction table pattern* is detected (source table has
           exactly two outgoing FK relationships and its PK is a
           composite of both FK column sets) → **MANY_TO_MANY**.
        3. Otherwise → **ONE_TO_MANY** (the standard FK pattern).

        Args:
            source_table: FK-holding (child) table name.
            source_columns: FK column(s) in the source table.
            target_table: Referenced (parent) table name.
            target_columns: Referenced column(s) in the target table.
            schema: The full schema definition for PK look-up.

        Returns:
            The inferred :class:`RelationshipType`.
        """
        # Suppress unused-argument lint — target_table and target_columns
        # are part of the public interface for future inference expansion.
        _ = target_table
        _ = target_columns

        # Look-up source table PK columns.
        src_pk_columns = self._get_primary_key_columns(source_table, schema)

        # Rule 1: if source FK columns == source PK → ONE_TO_ONE.
        if src_pk_columns and set(source_columns) == set(src_pk_columns):
            return RelationshipType.ONE_TO_ONE

        # Rule 2: junction table detection.
        if self._is_junction_table(source_table, schema):
            return RelationshipType.MANY_TO_MANY

        # Default: ONE_TO_MANY.
        return RelationshipType.ONE_TO_MANY

    # ---------------------------------------------------------------
    # Public — update_schema_relationships
    # ---------------------------------------------------------------

    @circuit_breaker_decorator(
        name="mongodb_relationship_update",
        failure_threshold=5,
        recovery_timeout=30,
    )
    def update_schema_relationships(
        self,
        schema_id: str,
        relationships: list[RelationshipDefinition],
    ) -> bool:
        """Persist discovered relationships into MongoDB.

        Updates the :class:`SchemaDefinition` document identified by
        ``schema_id`` with the provided relationships list and updates
        the ``total_relationships`` count.  The update is tenant-scoped
        via :class:`SchemaDefinitionRepository`.

        This method is protected by a circuit breaker to guard against
        MongoDB connectivity failures.

        Args:
            schema_id: Unique identifier of the schema to update.
            relationships: The discovered relationships to store.

        Returns:
            ``True`` if the document was successfully updated,
            ``False`` otherwise.

        Raises:
            CircuitBreakerError: If the MongoDB circuit breaker is open
                due to repeated failures.
        """
        serialised_relationships = [r.model_dump() for r in relationships]
        updates: dict[str, Any] = {
            "relationships": serialised_relationships,
            "total_relationships": len(relationships),
        }
        success = self._repo.update(schema_id, updates)
        self._logger.info(
            "schema_relationships_persisted",
            schema_id=schema_id,
            relationship_count=len(relationships),
            success=success,
            tenant_id=self.tenant_id,
        )
        return success

    # ---------------------------------------------------------------
    # Public — get_relationships_for_table
    # ---------------------------------------------------------------

    def get_relationships_for_table(
        self,
        schema: SchemaDefinition,
        table_name: str,
    ) -> list[RelationshipDefinition]:
        """Filter relationships involving a specific table.

        Returns relationships where ``table_name`` is either the
        *source* (FK-holding child) or the *target* (referenced parent).
        This is useful for the Generation Engine's dependency resolution
        when generating data for a single table.

        Args:
            schema: The schema containing relationships.
            table_name: Table name to filter on.

        Returns:
            Filtered list of :class:`RelationshipDefinition` where the
            table participates as source or target.
        """
        return [
            r for r in schema.relationships
            if table_name in (r.source_table, r.target_table)
        ]

    # ---------------------------------------------------------------
    # Public — build_relationship_graph
    # ---------------------------------------------------------------

    def build_relationship_graph(
        self,
        schema: SchemaDefinition,
    ) -> dict[str, list[str]]:
        """Build an adjacency-list graph from schema relationships.

        Each key is a table name and its value is a list of tables that
        it is related to (both directions).  This graph is consumed by
        :class:`~profiling_service.discovery.dependency_analyzer.DependencyAnalyzer`
        for topological sorting to determine generation order.

        Isolated tables (no relationships) are included with empty
        adjacency lists.

        Args:
            schema: The schema with populated relationships.

        Returns:
            Adjacency list mapping table names to lists of related
            table names.
        """
        graph: dict[str, list[str]] = defaultdict(list)

        # Ensure every table appears, even isolated ones.
        for table_def in schema.tables:
            if table_def.table_name not in graph:
                graph[table_def.table_name] = []

        for rel in schema.relationships:
            if rel.target_table not in graph[rel.source_table]:
                graph[rel.source_table].append(rel.target_table)
            if rel.source_table not in graph[rel.target_table]:
                graph[rel.target_table].append(rel.source_table)

        return dict(graph)

    # ---------------------------------------------------------------
    # Convenience — discover from ConnectionConfig
    # ---------------------------------------------------------------

    def discover_relationships_from_config(
        self,
        connection_config: ConnectionConfig,
        schema: SchemaDefinition,
    ) -> list[RelationshipDefinition]:
        """Discover relationships using a connection configuration.

        Convenience method that creates an ERP connector from the
        provided :class:`ConnectionConfig`, connects, runs relationship
        discovery, and cleans up the connector automatically.

        Args:
            connection_config: ERP connection parameters used to
                instantiate the appropriate connector via
                :func:`~profiling_service.connectors.get_connector`.
            schema: The :class:`SchemaDefinition` containing the tables
                to inspect.

        Returns:
            A de-duplicated, validated list of
            :class:`RelationshipDefinition` objects.

        Raises:
            ValueError: If the ERP type in *connection_config* is not
                supported.
            ConnectionError: If the connector cannot establish a
                connection.
        """
        connector = get_connector(
            connection_config.erp_type, connection_config,
        )
        self._logger.info(
            "relationship_discovery_from_config",
            erp_type=connection_config.erp_type,
            tenant_id=self.tenant_id,
        )
        with connector:
            return self.discover_relationships(connector, schema)

    # ---------------------------------------------------------------
    # Private - circuit-breaker-protected connector call
    # ---------------------------------------------------------------

    @circuit_breaker_decorator(
        name="erp_relationship_discovery",
        failure_threshold=3,
        recovery_timeout=60,
    )
    def _call_connector_discover(
        self,
        connector: BaseConnector,
        schema_name: str | None = None,
    ) -> list[ConnectorRelationship]:
        """Call the connector's relationship discovery with circuit breaker.

        Wraps :meth:`BaseConnector.discover_relationships` with a
        circuit breaker to guard against ERP system failures.  When the
        circuit opens after repeated failures, subsequent calls fail fast
        with :class:`CircuitBreakerError` until the recovery timeout
        elapses.

        **C-001:** Only metadata-level discovery is invoked.

        Args:
            connector: An active ERP connector instance.
            schema_name: Optional database schema/namespace filter.

        Returns:
            List of :class:`RelationshipMetadata` from the connector.

        Raises:
            CircuitBreakerError: If the circuit is open.
            DiscoveryError: Propagated from the connector.
        """
        # Check the per-session cache to avoid redundant ERP round-trips.
        if schema_name in self._schema_cache:
            self._logger.debug(
                "relationship_discovery_cache_hit",
                schema_name=schema_name,
            )
            return self._schema_cache[schema_name]

        results = connector.discover_relationships(schema_name=schema_name)

        # Cache the results for the remainder of this discovery session.
        self._schema_cache[schema_name] = results
        self._logger.debug(
            "relationship_discovery_connector_complete",
            schema_name=schema_name,
            relationships_found=len(results),
        )
        return results

    # ---------------------------------------------------------------
    # Private — conversion helpers
    # ---------------------------------------------------------------

    def _convert_relationship(
        self,
        connector_rel: ConnectorRelationship,
        schema: SchemaDefinition,
    ) -> RelationshipDefinition | None:
        """Convert a connector-level relationship to a domain model.

        Skips non-FK constraint types (e.g. ``CHECK``).  Attempts to
        infer a more precise cardinality using
        :meth:`infer_relationship_type` when structural metadata is
        available.

        Args:
            connector_rel: Raw metadata from the ERP connector.  Accesses
                ``source_table``, ``source_column``, ``target_table``,
                ``target_column``, ``relationship_type``,
                ``constraint_name``, ``on_delete``, ``on_update``.
            schema: The schema definition for type inference.

        Returns:
            A :class:`RelationshipDefinition`, or ``None`` if the
            connector relationship should be skipped (e.g. CHECK
            constraints).
        """
        # Map the relationship type string to the domain enum.
        rel_type_str = (
            connector_rel.relationship_type or "FOREIGN_KEY"
        ).upper()
        mapped_type = RELATIONSHIP_TYPE_MAPPING.get(rel_type_str)

        if mapped_type is None:
            self._logger.debug(
                "relationship_skipped_non_fk",
                constraint_name=connector_rel.constraint_name,
                type=rel_type_str,
            )
            return None

        # Build source/target column lists from the connector's single-
        # column representation.
        source_columns: list[str] = [connector_rel.source_column]
        target_columns: list[str] = [connector_rel.target_column]

        # Attempt more precise cardinality inference from schema structure.
        try:
            inferred = self.infer_relationship_type(
                source_table=connector_rel.source_table,
                source_columns=source_columns,
                target_table=connector_rel.target_table,
                target_columns=target_columns,
                schema=schema,
            )
            mapped_type = inferred
        except Exception:
            # Fall back to the statically mapped type on any error.
            self._logger.debug(
                "relationship_type_inference_fallback",
                source_table=connector_rel.source_table,
                target_table=connector_rel.target_table,
            )

        # Build a human-readable description.
        description = (
            f"FK: {connector_rel.source_table}.{connector_rel.source_column}"
            f" → {connector_rel.target_table}.{connector_rel.target_column}"
        )
        if connector_rel.constraint_name:
            description = f"[{connector_rel.constraint_name}] {description}"

        return RelationshipDefinition(
            relationship_id=str(uuid.uuid4()),
            relationship_type=mapped_type,
            source_table=connector_rel.source_table,
            source_columns=source_columns,
            target_table=connector_rel.target_table,
            target_columns=target_columns,
            is_enforced=True,
            on_delete=connector_rel.on_delete,
            on_update=connector_rel.on_update,
            description=description,
        )

    # ---------------------------------------------------------------
    # Private — de-duplication
    # ---------------------------------------------------------------

    def _deduplicate_relationships(
        self,
        relationships: list[RelationshipDefinition],
    ) -> list[RelationshipDefinition]:
        """Remove duplicate relationships (same source/target columns).

        A relationship is considered duplicate if it has the same
        ``(source_table, source_columns, target_table, target_columns)``
        tuple as another relationship in the list.  The first occurrence
        is retained.

        Args:
            relationships: List with potential duplicates.

        Returns:
            De-duplicated list preserving insertion order.
        """
        seen: set[tuple[str, ...]] = set()
        unique: list[RelationshipDefinition] = []
        removed = 0

        for rel in relationships:
            key = (
                rel.source_table,
                tuple(sorted(rel.source_columns)),
                rel.target_table,
                tuple(sorted(rel.target_columns)),
            )
            if key not in seen:
                seen.add(key)
                unique.append(rel)
            else:
                removed += 1

        if removed > 0:
            self._logger.debug(
                "relationships_deduplicated",
                original_count=len(relationships),
                duplicates_removed=removed,
            )
        return unique

    # ---------------------------------------------------------------
    # Private — validation
    # ---------------------------------------------------------------

    def _validate_relationship_endpoints(
        self,
        relationship: RelationshipDefinition,
        schema: SchemaDefinition,
    ) -> bool:
        """Validate that both endpoints of a relationship exist in the schema.

        Checks:
        1. ``source_table`` exists in ``schema.tables``.
        2. ``target_table`` exists in ``schema.tables``.
        3. ``source_columns`` exist in the source table's column list.
        4. ``target_columns`` exist in the target table's column list.

        Args:
            relationship: The relationship to validate.
            schema: The schema containing table and column definitions.

        Returns:
            ``True`` if the relationship is valid, ``False`` otherwise.
        """
        # Build a map of table_name → set of column names for fast look-up.
        table_column_map: dict[str, set[str]] = {}
        for table_def in schema.tables:
            table_column_map[table_def.table_name] = {
                col.column_name for col in table_def.columns
            }

        # Check source table exists.
        if relationship.source_table not in table_column_map:
            self._logger.warning(
                "relationship_invalid_source_table",
                source_table=relationship.source_table,
                target_table=relationship.target_table,
                relationship_id=relationship.relationship_id,
            )
            return False

        # Check target table exists.
        if relationship.target_table not in table_column_map:
            self._logger.warning(
                "relationship_invalid_target_table",
                source_table=relationship.source_table,
                target_table=relationship.target_table,
                relationship_id=relationship.relationship_id,
            )
            return False

        # Check source columns exist.
        src_cols = table_column_map[relationship.source_table]
        for col in relationship.source_columns:
            if col not in src_cols:
                self._logger.warning(
                    "relationship_invalid_source_column",
                    column=col,
                    table=relationship.source_table,
                    relationship_id=relationship.relationship_id,
                )
                return False

        # Check target columns exist.
        tgt_cols = table_column_map[relationship.target_table]
        for col in relationship.target_columns:
            if col not in tgt_cols:
                self._logger.warning(
                    "relationship_invalid_target_column",
                    column=col,
                    table=relationship.target_table,
                    relationship_id=relationship.relationship_id,
                )
                return False

        return True

    # ---------------------------------------------------------------
    # Private — cross-module detection
    # ---------------------------------------------------------------

    def _is_cross_module(
        self,
        relationship: RelationshipDefinition,
        schema: SchemaDefinition,
    ) -> bool:
        """Determine whether a relationship spans ERP module boundaries.

        A cross-module relationship exists when the source and target
        tables belong to different :class:`ERPModule` values (e.g.
        a Sales & Distribution table referencing a Material Management
        table).

        Args:
            relationship: The relationship to inspect.
            schema: The schema containing table definitions with module
                assignments (``erp_module`` field on
                :class:`TableDefinition`).

        Returns:
            ``True`` if source and target tables belong to different
            ERP modules; ``False`` if they share the same module or if
            either table's module cannot be determined.
        """
        module_map: dict[str, str | None] = {}
        for table_def in schema.tables:
            # TableDefinition exposes the module as ``erp_module``.
            module_map[table_def.table_name] = (
                table_def.erp_module
                if hasattr(table_def, "erp_module")
                else None
            )

        src_mod = module_map.get(relationship.source_table)
        tgt_mod = module_map.get(relationship.target_table)

        if src_mod is None or tgt_mod is None:
            return False
        return src_mod != tgt_mod

    # ---------------------------------------------------------------
    # Private — PK / junction table helpers
    # ---------------------------------------------------------------

    def _get_primary_key_columns(
        self,
        table_name: str,
        schema: SchemaDefinition,
    ) -> list[str]:
        """Return primary-key column names for a table.

        Inspects the :class:`TableDefinition` for columns with
        ``is_primary_key == True``.  Falls back to the table's
        ``primary_key_columns`` list if no column is explicitly flagged.

        Args:
            table_name: Name of the table to inspect.
            schema: The schema containing table definitions.

        Returns:
            List of PK column names, or an empty list if the table
            is not found or has no declared primary key.
        """
        for table_def in schema.tables:
            if table_def.table_name == table_name:
                # First, try column-level flags.
                pk_from_columns = [
                    col.column_name
                    for col in table_def.columns
                    if col.is_primary_key
                ]
                if pk_from_columns:
                    return pk_from_columns
                # Fall back to explicit PK list on the table.
                if table_def.primary_key_columns:
                    return list(table_def.primary_key_columns)
                return []
        return []

    def _is_junction_table(
        self,
        table_name: str,
        schema: SchemaDefinition,
    ) -> bool:
        """Detect if a table is a junction (bridge / associative) table.

        A junction table pattern is detected when:

        1. The table has exactly two FK relationships as the *source*
           (outgoing foreign keys).
        2. The table's primary key is a composite key consisting of
           exactly the FK columns from both relationships.

        This heuristic identifies many-to-many association tables such
        as ``ORDER_ITEM_MATERIAL`` linking orders to materials.

        Args:
            table_name: Table to check.
            schema: The schema with populated relationships.

        Returns:
            ``True`` if the table matches the junction table pattern.
        """
        # Count outgoing FK relationships from this table.
        outgoing = [
            r for r in schema.relationships
            if r.source_table == table_name
        ]
        if len(outgoing) != 2:
            return False

        # Get PK columns.
        pk_cols = set(self._get_primary_key_columns(table_name, schema))
        if not pk_cols:
            return False

        # Check if PK == union of both FK source column sets.
        fk_cols: set[str] = set()
        for rel in outgoing:
            fk_cols.update(rel.source_columns)

        return pk_cols == fk_cols
