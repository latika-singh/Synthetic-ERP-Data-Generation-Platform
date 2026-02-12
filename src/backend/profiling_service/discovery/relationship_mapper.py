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

**Constraint C-005:**
    Supports the four initial-release ERP modules — Financial Accounting,
    Human Resources, Sales & Distribution, Material Management.

Usage::

    from profiling_service.discovery.relationship_mapper import (
        RelationshipMapper,
    )

    mapper = RelationshipMapper(tenant_id="tenant-001")
    relationships = mapper.discover_relationships(connector, schema)
"""

from __future__ import annotations

import uuid
from collections import defaultdict
from datetime import datetime, timezone
from typing import Any, Optional

from profiling_service.connectors.base import (
    BaseConnector,
    ConnectionConfig,
    RelationshipMetadata as ConnectorRelationship,
)
from profiling_service.connectors import get_connector
from profiling_service.models.schema_definition import (
    RelationshipDefinition,
    RelationshipType,
    SchemaDefinition,
    SchemaDefinitionRepository,
)
from shared.logging.structured_logger import get_logger
from shared.middleware.circuit_breaker import circuit_breaker_decorator


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
    "MANY_TO_ONE": RelationshipType.ONE_TO_MANY,   # reverse is stored same
    "MANY_TO_MANY": RelationshipType.MANY_TO_MANY,
    "CHECK": None,                                   # skip non-FK constraints
    "NAVIGATION": RelationshipType.ONE_TO_MANY,      # OData navigation props
}


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

    Attributes:
        tenant_id: Tenant identifier for all database operations.
    """

    # ---------------------------------------------------------------
    # Initialisation
    # ---------------------------------------------------------------

    def __init__(self, tenant_id: str) -> None:
        """Initialise the mapper.

        Args:
            tenant_id: Tenant scope for MongoDB isolation.
        """
        self.tenant_id: str = tenant_id
        self._logger = get_logger(__name__)
        self._repo = SchemaDefinitionRepository(tenant_id)
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

        Iterates over every table in ``schema.tables``, calls the
        connector's :meth:`discover_relationships` for each table, and
        converts the returned
        :class:`~profiling_service.connectors.base.RelationshipMetadata`
        objects into :class:`RelationshipDefinition` domain models.

        The results are de-duplicated and validated (both source and
        target tables must exist in the schema).

        Args:
            connector: An active ERP connector instance.
            schema: The :class:`SchemaDefinition` containing the tables
                to inspect.

        Returns:
            A de-duplicated, validated list of :class:`RelationshipDefinition`.
        """
        start = datetime.now(timezone.utc)
        raw_relationships: list[RelationshipDefinition] = []
        table_names = {t.table_name for t in schema.tables}

        for table_def in schema.tables:
            try:
                connector_rels: list[ConnectorRelationship] = (
                    connector.discover_relationships(table_def.table_name)
                )
                for crel in connector_rels:
                    domain_rel = self._convert_relationship(crel, schema)
                    if domain_rel is not None:
                        raw_relationships.append(domain_rel)
            except Exception:
                self._logger.warning(
                    "relationship_discovery_table_error",
                    table=table_def.table_name,
                    exc_info=True,
                )

        # De-duplicate and validate.
        deduped = self._deduplicate_relationships(raw_relationships)
        valid = [
            r for r in deduped
            if self._validate_relationship_endpoints(r, schema)
        ]

        cross_module_count = sum(
            1 for r in valid
            if self._is_cross_module(r, schema)
        )

        duration = (datetime.now(timezone.utc) - start).total_seconds()
        self._logger.info(
            "relationship_discovery_complete",
            total_discovered=len(raw_relationships),
            after_dedup=len(deduped),
            after_validation=len(valid),
            cross_module_count=cross_module_count,
            duration_seconds=round(duration, 3),
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
        preserving referential integrity.

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

        Rules:

        * If ``source_columns`` match the *entire* primary key of the
          source table → **ONE_TO_ONE** (the FK is a PK-based identifier
          link).
        * If a *junction table pattern* is detected (source table has
          exactly two FK relationships and its PK is a composite of both
          FK columns) → **MANY_TO_MANY**.
        * Otherwise → **ONE_TO_MANY** (the standard FK pattern).

        Args:
            source_table: FK-holding (child) table name.
            source_columns: FK column(s) in the source table.
            target_table: Referenced (parent) table name.
            target_columns: Referenced column(s) in the target table.
            schema: The full schema definition for PK look-up.

        Returns:
            The inferred :class:`RelationshipType`.
        """
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

    def update_schema_relationships(
        self,
        schema_id: str,
        relationships: list[RelationshipDefinition],
    ) -> bool:
        """Persist discovered relationships into MongoDB.

        Updates the :class:`SchemaDefinition` document identified by
        ``schema_id`` with the provided relationships list and updates
        the ``total_relationships`` count.

        Args:
            schema_id: Unique identifier of the schema to update.
            relationships: The discovered relationships to store.

        Returns:
            ``True`` if the document was successfully updated.
        """
        updates: dict[str, Any] = {
            "relationships": [r.model_dump() for r in relationships],
            "total_relationships": len(relationships),
        }
        success = self._repo.update(schema_id, updates)
        self._logger.info(
            "schema_relationships_persisted",
            schema_id=schema_id,
            relationship_count=len(relationships),
            success=success,
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

        Args:
            schema: The schema containing relationships.
            table_name: Table name to filter on.

        Returns:
            Filtered relationship list.
        """
        return [
            r for r in schema.relationships
            if r.source_table == table_name or r.target_table == table_name
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
        for topological sorting.

        Args:
            schema: The schema with populated relationships.

        Returns:
            Adjacency list mapping table names to lists of related
            table names.
        """
        graph: dict[str, list[str]] = defaultdict(list)

        # Ensure every table appears (even isolated ones).
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
    # Private — conversion helpers
    # ---------------------------------------------------------------

    def _convert_relationship(
        self,
        connector_rel: ConnectorRelationship,
        schema: SchemaDefinition,
    ) -> Optional[RelationshipDefinition]:
        """Convert a connector-level relationship to a domain model.

        Skips non-FK constraint types (e.g. ``CHECK``).

        Args:
            connector_rel: Raw metadata from the ERP connector.
            schema: The schema definition for type inference.

        Returns:
            A :class:`RelationshipDefinition`, or ``None`` if the
            connector relationship should be skipped.
        """
        # Map the relationship type string to the domain enum.
        rel_type_str = (connector_rel.relationship_type or "FOREIGN_KEY").upper()
        mapped_type = RELATIONSHIP_TYPE_MAPPING.get(rel_type_str)

        if mapped_type is None:
            self._logger.debug(
                "relationship_skipped_non_fk",
                constraint_name=connector_rel.constraint_name,
                type=rel_type_str,
            )
            return None

        # Try to infer more precise cardinality when possible.
        source_columns = [connector_rel.source_column]
        target_columns = [connector_rel.target_column]

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
            # Fall back to the statically mapped type.
            pass

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
        3. ``source_columns`` exist in the source table's columns.
        4. ``target_columns`` exist in the target table's columns.

        Args:
            relationship: The relationship to validate.
            schema: The schema containing table and column definitions.

        Returns:
            ``True`` if the relationship is valid, ``False`` otherwise.
        """
        table_map: dict[str, set[str]] = {}
        for table_def in schema.tables:
            table_map[table_def.table_name] = {
                col.column_name for col in table_def.columns
            }

        # Check source table exists.
        if relationship.source_table not in table_map:
            self._logger.warning(
                "relationship_invalid_source_table",
                source_table=relationship.source_table,
                target_table=relationship.target_table,
            )
            return False

        # Check target table exists.
        if relationship.target_table not in table_map:
            self._logger.warning(
                "relationship_invalid_target_table",
                source_table=relationship.source_table,
                target_table=relationship.target_table,
            )
            return False

        # Check source columns exist.
        src_cols = table_map[relationship.source_table]
        for col in relationship.source_columns:
            if col not in src_cols:
                self._logger.warning(
                    "relationship_invalid_source_column",
                    column=col,
                    table=relationship.source_table,
                )
                return False

        # Check target columns exist.
        tgt_cols = table_map[relationship.target_table]
        for col in relationship.target_columns:
            if col not in tgt_cols:
                self._logger.warning(
                    "relationship_invalid_target_column",
                    column=col,
                    table=relationship.target_table,
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

        Args:
            relationship: The relationship to inspect.
            schema: The schema containing table definitions with module
                assignments.

        Returns:
            ``True`` if source and target tables belong to different
            ERP modules.
        """
        module_map: dict[str, str | None] = {}
        for table_def in schema.tables:
            module_map[table_def.table_name] = getattr(
                table_def, "module", None
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

        Args:
            table_name: Name of the table.
            schema: The schema containing table definitions.

        Returns:
            List of PK column names, or empty list if not found.
        """
        for table_def in schema.tables:
            if table_def.table_name == table_name:
                return [
                    col.column_name
                    for col in table_def.columns
                    if getattr(col, "is_primary_key", False)
                ]
        return []

    def _is_junction_table(
        self,
        table_name: str,
        schema: SchemaDefinition,
    ) -> bool:
        """Detect if a table is a junction (bridge / associative) table.

        A junction table pattern is detected when:

        1. The table has exactly two FK relationships as the source.
        2. The table's PK is a composite of both FK column sets.

        Args:
            table_name: Table to check.
            schema: The schema with relationships.

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
