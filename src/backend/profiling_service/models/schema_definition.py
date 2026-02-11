"""MongoDB document model for the ``schema_definitions`` collection.

Defines Pydantic 2.x models representing ERP schema metadata including table
structures, column definitions with data types and constraints, foreign key
relationships, and ERP module associations.  Core fields include
``schema_id``, ``tenant_id``, ``erp_type`` (SAP, Oracle EBS, Dynamics 365,
JDBC legacy), ``tables`` with ``columns``, ``foreign_keys``, and module
mappings for Financial Accounting, HR, Sales & Distribution, and Material
Management.

This module also provides :class:`SchemaDefinitionRepository` — a thin
persistence layer on top of PyMongo 4.x that enforces **multi-tenant
isolation** on every query (Constraint C-001 / R-007).

The models defined here are the core *data contract* consumed by the
Generation Engine to understand the target schema structure for synthetic
data generation.  Without this file, the platform cannot persist or retrieve
ERP schema metadata and generation would have no schema context.

Note:
    This file captures **metadata only** — no raw production data values are
    ever stored (Constraint C-001).

Usage::

    from profiling_service.models.schema_definition import (
        SchemaDefinition,
        SchemaDefinitionRepository,
        ERPType,
        ERPModule,
    )

    repo = SchemaDefinitionRepository(tenant_id="tenant-abc")
    schema = SchemaDefinition(
        tenant_id="tenant-abc",
        erp_type=ERPType.SAP,
        connection_name="SAP Production Metadata",
    )
    schema_id = repo.create(schema)
"""

from __future__ import annotations

import logging
import uuid
from datetime import datetime, timezone
from enum import Enum
from typing import Any, Optional

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from shared.database.mongodb import COLLECTION_SCHEMA_DEFINITIONS, get_collection

# ---------------------------------------------------------------------------
# Module logger — uses standard ``logging`` (not shared.logging) to avoid
# circular dependency at the model layer.
# ---------------------------------------------------------------------------
logger: logging.Logger = logging.getLogger(__name__)


# ===================================================================
# Enumerations
# ===================================================================


class ERPType(str, Enum):
    """Supported ERP system types for schema discovery.

    Each member corresponds to a distinct ERP platform with its own
    connectivity protocol (RFC/BAPI, OData, JDBC, etc.).
    """

    SAP = "sap"
    ORACLE_EBS = "oracle_ebs"
    DYNAMICS_365 = "dynamics_365"
    JDBC_LEGACY = "jdbc_legacy"


class ERPModule(str, Enum):
    """ERP functional modules supported in the initial release (C-005).

    Limited to four modules for the first release: Financial Accounting,
    Human Resources, Sales & Distribution, and Material Management.
    """

    FINANCIAL_ACCOUNTING = "financial_accounting"
    HUMAN_RESOURCES = "human_resources"
    SALES_DISTRIBUTION = "sales_distribution"
    MATERIAL_MANAGEMENT = "material_management"


class ColumnDataType(str, Enum):
    """Normalised column data types across ERP systems.

    Each member maps to one or more native data types from SAP, Oracle EBS,
    Dynamics 365, or legacy JDBC sources.  The :attr:`native_type` field on
    :class:`ColumnDefinition` preserves the original source system type.
    """

    STRING = "string"
    INTEGER = "integer"
    FLOAT = "float"
    DECIMAL = "decimal"
    BOOLEAN = "boolean"
    DATE = "date"
    DATETIME = "datetime"
    TIMESTAMP = "timestamp"
    TEXT = "text"
    BLOB = "blob"
    CHAR = "char"
    VARCHAR = "varchar"
    NUMERIC = "numeric"
    BIGINT = "bigint"
    SMALLINT = "smallint"
    CLOB = "clob"


class RelationshipType(str, Enum):
    """Types of inter-table relationships discovered in the source schema."""

    ONE_TO_ONE = "one_to_one"
    ONE_TO_MANY = "one_to_many"
    MANY_TO_MANY = "many_to_many"


class SchemaStatus(str, Enum):
    """Lifecycle states for a schema discovery/profiling workflow.

    The state machine progresses as follows::

        DISCOVERING → DISCOVERED → PROFILING → PROFILED
                                        ↘ FAILED
        Any state → ARCHIVED
    """

    DISCOVERING = "discovering"
    DISCOVERED = "discovered"
    PROFILING = "profiling"
    PROFILED = "profiled"
    FAILED = "failed"
    ARCHIVED = "archived"


# ===================================================================
# Pydantic Models — Sub-documents
# ===================================================================


class ColumnDefinition(BaseModel):
    """Describes a single column within an ERP table.

    Captures the column's data type, constraints, and ERP-specific labelling
    without storing any actual data values (C-001).

    Attributes:
        column_name: Name of the column in the source database.
        data_type: Normalised data type from :class:`ColumnDataType`.
        native_type: Original data type string from the source system
            (e.g. ``'NVARCHAR2(100)'``, ``'DATS'``).
        is_nullable: Whether the column allows ``NULL`` values.
        is_primary_key: Whether the column participates in the primary key.
        is_unique: Whether the column has a unique constraint.
        is_indexed: Whether the column is indexed in the source system.
        max_length: Maximum character or byte length for string types.
        precision: Numeric precision for decimal/numeric types.
        scale: Numeric scale for decimal/numeric types.
        default_value: Default value expression (if any).
        description: Column description or comment from the source catalog.
        erp_field_label: Human-readable label shown in the ERP user interface.
    """

    model_config = ConfigDict(use_enum_values=True)

    column_name: str
    data_type: ColumnDataType
    native_type: str
    is_nullable: bool = True
    is_primary_key: bool = False
    is_unique: bool = False
    is_indexed: bool = False
    max_length: Optional[int] = None
    precision: Optional[int] = None
    scale: Optional[int] = None
    default_value: Optional[str] = None
    description: Optional[str] = None
    erp_field_label: Optional[str] = None

    @field_validator("column_name")
    @classmethod
    def _column_name_not_empty(cls, value: str) -> str:
        """Ensure column_name is a non-empty string."""
        stripped = value.strip()
        if not stripped:
            raise ValueError("column_name must be a non-empty string")
        return stripped

    @field_validator("max_length", "precision", "scale")
    @classmethod
    def _non_negative_int(cls, value: Optional[int]) -> Optional[int]:
        """Ensure optional numeric constraints are non-negative when set."""
        if value is not None and value < 0:
            raise ValueError("Value must be non-negative")
        return value


class ConstraintDefinition(BaseModel):
    """Describes a table-level constraint discovered in the source schema.

    Attributes:
        constraint_name: Name of the constraint (e.g. ``'CHK_AMOUNT_POS'``).
        constraint_type: Type of constraint — one of ``CHECK``, ``UNIQUE``,
            ``NOT_NULL``, ``DEFAULT``, ``CUSTOM``.
        columns: Column names involved in this constraint.
        expression: Constraint expression or business rule (if applicable).
        description: Human-readable description of the constraint.
    """

    constraint_name: str
    constraint_type: str
    columns: list[str]
    expression: Optional[str] = None
    description: Optional[str] = None

    @field_validator("constraint_name")
    @classmethod
    def _constraint_name_not_empty(cls, value: str) -> str:
        stripped = value.strip()
        if not stripped:
            raise ValueError("constraint_name must be a non-empty string")
        return stripped

    @field_validator("columns")
    @classmethod
    def _columns_not_empty(cls, value: list[str]) -> list[str]:
        if not value:
            raise ValueError("columns list must contain at least one column")
        return value


class RelationshipDefinition(BaseModel):
    """Describes a foreign-key or logical relationship between two tables.

    Used by the Generation Engine to enforce referential integrity during
    synthetic data generation — records in the *source* table will contain
    valid references to records in the *target* table.

    Attributes:
        relationship_id: Globally unique identifier for this relationship.
        relationship_type: Cardinality of the relationship.
        source_table: Fully qualified name of the referencing table.
        source_columns: Column(s) participating in the foreign key.
        target_table: Fully qualified name of the referenced table.
        target_columns: Column(s) referenced in the target table.
        is_enforced: Whether the FK constraint is enforced in the source DB.
        on_delete: Referential action on delete (``CASCADE``, ``SET NULL``,
            ``RESTRICT``, ``NO ACTION``).
        on_update: Referential action on update.
        description: Optional description or documentation for the FK.
    """

    model_config = ConfigDict(use_enum_values=True)

    relationship_id: str = Field(default_factory=lambda: str(uuid.uuid4()))
    relationship_type: RelationshipType
    source_table: str
    source_columns: list[str]
    target_table: str
    target_columns: list[str]
    is_enforced: bool = True
    on_delete: Optional[str] = None
    on_update: Optional[str] = None
    description: Optional[str] = None

    @field_validator("source_columns", "target_columns")
    @classmethod
    def _fk_columns_not_empty(cls, value: list[str]) -> list[str]:
        if not value:
            raise ValueError("Foreign key column list must not be empty")
        return value


class IndexDefinition(BaseModel):
    """Describes an index discovered on a source table.

    Attributes:
        index_name: Name of the index in the source database.
        columns: Ordered list of indexed column names.
        is_unique: Whether the index enforces uniqueness.
        is_clustered: Whether this is a clustered (physical ordering) index.
        description: Optional description of the index purpose.
    """

    index_name: str
    columns: list[str]
    is_unique: bool = False
    is_clustered: bool = False
    description: Optional[str] = None

    @field_validator("index_name")
    @classmethod
    def _index_name_not_empty(cls, value: str) -> str:
        stripped = value.strip()
        if not stripped:
            raise ValueError("index_name must be a non-empty string")
        return stripped

    @field_validator("columns")
    @classmethod
    def _index_columns_not_empty(cls, value: list[str]) -> list[str]:
        if not value:
            raise ValueError("Index must reference at least one column")
        return value


class TableDefinition(BaseModel):
    """Describes an ERP table with its columns, constraints, and indexes.

    Each table is associated with exactly one :class:`ERPModule`, enabling
    the Generation Engine to generate data per-module and maintain
    cross-module referential integrity.

    Attributes:
        table_name: Fully qualified table name in the source system.
        schema_name: Database schema or namespace (e.g. ``'SAPSR3'``).
        erp_module: The ERP functional module this table belongs to.
        columns: Ordered list of column definitions.
        constraints: Table-level constraints (CHECK, UNIQUE, etc.).
        indexes: Indexes defined on this table.
        primary_key_columns: Names of columns composing the primary key.
        estimated_row_count: Estimated row count from database statistics.
        description: Table description or comment from the catalog.
        erp_table_label: Human-readable label shown in the ERP interface.
    """

    model_config = ConfigDict(use_enum_values=True)

    table_name: str
    schema_name: Optional[str] = None
    erp_module: ERPModule
    columns: list[ColumnDefinition]
    constraints: list[ConstraintDefinition] = []
    indexes: list[IndexDefinition] = []
    primary_key_columns: list[str] = []
    estimated_row_count: Optional[int] = None
    description: Optional[str] = None
    erp_table_label: Optional[str] = None

    @field_validator("table_name")
    @classmethod
    def _table_name_not_empty(cls, value: str) -> str:
        stripped = value.strip()
        if not stripped:
            raise ValueError("table_name must be a non-empty string")
        return stripped

    @field_validator("columns")
    @classmethod
    def _columns_not_empty(cls, value: list[ColumnDefinition]) -> list[ColumnDefinition]:
        if not value:
            raise ValueError("A table must have at least one column")
        return value

    @field_validator("estimated_row_count")
    @classmethod
    def _row_count_non_negative(cls, value: Optional[int]) -> Optional[int]:
        if value is not None and value < 0:
            raise ValueError("estimated_row_count must be non-negative")
        return value


# ===================================================================
# Pydantic Models — Top-Level Documents
# ===================================================================


class SchemaDefinition(BaseModel):
    """Root document model for the ``schema_definitions`` MongoDB collection.

    Represents the complete discovered schema metadata for a single ERP
    connection.  Contains all tables, columns, relationships, and module
    associations.

    The ``@model_validator`` auto-computes ``total_tables``,
    ``total_columns``, and ``total_relationships`` from the nested lists
    so that consumers can inspect counts without traversing the hierarchy.

    Attributes:
        schema_id: Globally unique identifier (UUID4).
        tenant_id: Tenant identifier — all queries are scoped to this value
            for multi-tenant isolation (R-007).
        erp_type: The type of ERP system this schema was discovered from.
        erp_version: Version string of the source ERP system.
        connection_name: User-friendly name for this connection/schema.
        status: Current lifecycle status of the schema.
        tables: List of discovered table definitions.
        relationships: Cross-table foreign-key relationships.
        modules: ERP modules covered by this schema.
        total_tables: Auto-computed count of discovered tables.
        total_columns: Auto-computed total columns across all tables.
        total_relationships: Auto-computed count of relationships.
        discovery_metadata: Arbitrary discovery context (connector info,
            discovery duration, warnings, etc.).
        created_at: UTC timestamp when this schema was first created.
        updated_at: UTC timestamp of the last modification.
        created_by: Identifier of the user who initiated discovery.
        tags: User-defined tags for organisation and filtering.
    """

    model_config = ConfigDict(use_enum_values=True, populate_by_name=True)

    schema_id: str = Field(default_factory=lambda: str(uuid.uuid4()))
    tenant_id: str
    erp_type: ERPType
    erp_version: Optional[str] = None
    connection_name: str
    status: SchemaStatus = SchemaStatus.DISCOVERING
    tables: list[TableDefinition] = []
    relationships: list[RelationshipDefinition] = []
    modules: list[ERPModule] = []
    total_tables: int = 0
    total_columns: int = 0
    total_relationships: int = 0
    discovery_metadata: dict[str, Any] = {}
    created_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))
    updated_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))
    created_by: Optional[str] = None
    tags: list[str] = []

    @model_validator(mode="before")
    @classmethod
    def _compute_totals(cls, data: Any) -> Any:
        """Auto-compute aggregate counts from nested table/relationship lists.

        When ``tables`` or ``relationships`` are present in the input data
        the validator automatically populates ``total_tables``,
        ``total_columns``, and ``total_relationships`` so callers do not
        need to set them manually.
        """
        if isinstance(data, dict):
            tables = data.get("tables", [])
            relationships = data.get("relationships", [])

            # Only auto-compute when the caller hasn't explicitly set them
            # or when the lists are populated and counts are still zero.
            if tables:
                computed_tables = len(tables)
                computed_columns = 0
                for table in tables:
                    if isinstance(table, dict):
                        computed_columns += len(table.get("columns", []))
                    elif isinstance(table, TableDefinition):
                        computed_columns += len(table.columns)

                # Only overwrite if caller hasn't explicitly set a non-zero value
                if data.get("total_tables", 0) == 0:
                    data["total_tables"] = computed_tables
                if data.get("total_columns", 0) == 0:
                    data["total_columns"] = computed_columns

            if relationships:
                if data.get("total_relationships", 0) == 0:
                    data["total_relationships"] = len(relationships)

        return data

    @field_validator("tenant_id")
    @classmethod
    def _tenant_id_not_empty(cls, value: str) -> str:
        stripped = value.strip()
        if not stripped:
            raise ValueError("tenant_id must be a non-empty string")
        return stripped

    @field_validator("connection_name")
    @classmethod
    def _connection_name_not_empty(cls, value: str) -> str:
        stripped = value.strip()
        if not stripped:
            raise ValueError("connection_name must be a non-empty string")
        return stripped


# ===================================================================
# Request / Response Models
# ===================================================================


class SchemaDiscoveryRequest(BaseModel):
    """Inbound request model for initiating an ERP schema discovery.

    Attributes:
        erp_type: Target ERP system type.
        connection_config: Connection parameters (host, port, credentials
            reference — **never** raw passwords).
        connection_name: Human-friendly name for this connection.
        modules: ERP modules to discover.  Defaults to all four initial
            release modules.
        max_tables: Optional upper limit on the number of tables to discover.
        include_indexes: Whether to discover table indexes.
        include_constraints: Whether to discover table constraints.
        tags: Optional user-defined tags for the resulting schema.
    """

    model_config = ConfigDict(use_enum_values=True)

    erp_type: ERPType
    connection_config: dict[str, Any]
    connection_name: str
    modules: list[ERPModule] = [
        ERPModule.FINANCIAL_ACCOUNTING,
        ERPModule.HUMAN_RESOURCES,
        ERPModule.SALES_DISTRIBUTION,
        ERPModule.MATERIAL_MANAGEMENT,
    ]
    max_tables: Optional[int] = None
    include_indexes: bool = True
    include_constraints: bool = True
    tags: list[str] = []

    @field_validator("connection_name")
    @classmethod
    def _conn_name_not_empty(cls, value: str) -> str:
        stripped = value.strip()
        if not stripped:
            raise ValueError("connection_name must be a non-empty string")
        return stripped

    @field_validator("connection_config")
    @classmethod
    def _connection_config_not_empty(cls, value: dict[str, Any]) -> dict[str, Any]:
        if not value:
            raise ValueError("connection_config must not be empty")
        return value

    @field_validator("max_tables")
    @classmethod
    def _max_tables_positive(cls, value: Optional[int]) -> Optional[int]:
        if value is not None and value <= 0:
            raise ValueError("max_tables must be a positive integer")
        return value


class SchemaDiscoveryResponse(BaseModel):
    """Outbound response model returned after a schema discovery is queued.

    Attributes:
        schema_id: Unique identifier for the newly created schema record.
        status: Initial status (typically ``DISCOVERING``).
        erp_type: ERP system type for the discovery.
        total_tables: Number of tables discovered so far (0 if async).
        total_columns: Number of columns discovered so far.
        total_relationships: Number of relationships discovered so far.
        modules: ERP modules targeted for discovery.
        created_at: Timestamp when the discovery was initiated.
        message: Human-readable status message.
    """

    model_config = ConfigDict(use_enum_values=True)

    schema_id: str
    status: SchemaStatus
    erp_type: ERPType
    total_tables: int
    total_columns: int
    total_relationships: int
    modules: list[ERPModule]
    created_at: datetime
    message: str = "Schema discovery initiated"


# ===================================================================
# Repository — MongoDB CRUD with Multi-Tenant Isolation
# ===================================================================


class SchemaDefinitionRepository:
    """Persistence layer for :class:`SchemaDefinition` documents.

    Every method enforces **tenant isolation** by including
    ``tenant_id`` in all MongoDB queries.  This guarantees that tenants
    can never access each other's schema metadata (R-007).

    Args:
        tenant_id: The tenant identifier used to scope all operations.

    Example::

        repo = SchemaDefinitionRepository(tenant_id="acme-corp")
        schema_id = repo.create(schema)
        found = repo.get_by_id(schema_id)
    """

    def __init__(self, tenant_id: str) -> None:
        """Initialise the repository for a specific tenant.

        Args:
            tenant_id: Non-empty tenant identifier.  All subsequent
                operations are scoped to this tenant.

        Raises:
            ValueError: If ``tenant_id`` is empty or whitespace-only.
        """
        if not tenant_id or not tenant_id.strip():
            raise ValueError("tenant_id must be a non-empty string")
        self._tenant_id: str = tenant_id.strip()
        self._collection = get_collection(COLLECTION_SCHEMA_DEFINITIONS)
        logger.debug(
            "SchemaDefinitionRepository initialised",
            extra={"tenant_id": self._tenant_id},
        )

    # ------------------------------------------------------------------
    # Create
    # ------------------------------------------------------------------

    def create(self, schema: SchemaDefinition) -> str:
        """Insert a new schema definition document into MongoDB.

        The ``tenant_id`` on the document is forcefully set to the
        repository's tenant to prevent cross-tenant writes.

        Args:
            schema: A :class:`SchemaDefinition` instance to persist.

        Returns:
            The ``schema_id`` of the newly inserted document.

        Raises:
            Exception: Propagated from PyMongo if the insert fails.
        """
        try:
            doc = schema.model_dump()
            # Enforce tenant isolation on writes
            doc["tenant_id"] = self._tenant_id
            # Ensure timestamps are current
            now = datetime.now(timezone.utc)
            doc["created_at"] = now
            doc["updated_at"] = now

            self._collection.insert_one(doc)
            logger.info(
                "Schema definition created",
                extra={
                    "schema_id": doc["schema_id"],
                    "tenant_id": self._tenant_id,
                    "erp_type": doc.get("erp_type"),
                    "total_tables": doc.get("total_tables", 0),
                },
            )
            return str(doc["schema_id"])
        except Exception:
            logger.error(
                "Failed to create schema definition",
                extra={"tenant_id": self._tenant_id},
                exc_info=True,
            )
            raise

    # ------------------------------------------------------------------
    # Read
    # ------------------------------------------------------------------

    def get_by_id(self, schema_id: str) -> Optional[SchemaDefinition]:
        """Retrieve a schema definition by its ``schema_id``.

        The query is always scoped to the repository's ``tenant_id`` so
        that a tenant cannot access another tenant's schemas.

        Args:
            schema_id: The unique identifier of the schema to retrieve.

        Returns:
            A :class:`SchemaDefinition` instance if found, otherwise
            ``None``.
        """
        try:
            doc = self._collection.find_one(
                {"schema_id": schema_id, "tenant_id": self._tenant_id}
            )
            if doc is None:
                logger.debug(
                    "Schema definition not found",
                    extra={
                        "schema_id": schema_id,
                        "tenant_id": self._tenant_id,
                    },
                )
                return None

            # Remove MongoDB internal _id before constructing the model
            doc.pop("_id", None)
            return SchemaDefinition(**doc)
        except Exception:
            logger.error(
                "Failed to retrieve schema definition",
                extra={
                    "schema_id": schema_id,
                    "tenant_id": self._tenant_id,
                },
                exc_info=True,
            )
            raise

    def list_schemas(
        self,
        erp_type: Optional[ERPType] = None,
        module: Optional[ERPModule] = None,
        status: Optional[SchemaStatus] = None,
        skip: int = 0,
        limit: int = 20,
    ) -> list[SchemaDefinition]:
        """List schema definitions with optional filters and pagination.

        Results are sorted by ``created_at`` descending (newest first).

        Args:
            erp_type: Filter by ERP system type.
            module: Filter by ERP module (matches schemas containing this
                module in their ``modules`` list).
            status: Filter by current schema status.
            skip: Number of documents to skip for pagination.
            limit: Maximum number of documents to return (capped at 100).

        Returns:
            A list of :class:`SchemaDefinition` instances matching the
            filters.
        """
        try:
            # Cap the limit to prevent excessive reads
            effective_limit = min(max(limit, 1), 100)

            query: dict[str, Any] = {"tenant_id": self._tenant_id}
            if erp_type is not None:
                query["erp_type"] = erp_type.value if isinstance(erp_type, ERPType) else erp_type
            if module is not None:
                module_value = module.value if isinstance(module, ERPModule) else module
                query["modules"] = {"$in": [module_value]}
            if status is not None:
                query["status"] = status.value if isinstance(status, SchemaStatus) else status

            cursor = (
                self._collection.find(query)
                .sort("created_at", -1)
                .skip(skip)
                .limit(effective_limit)
            )

            results: list[SchemaDefinition] = []
            for doc in cursor:
                doc.pop("_id", None)
                results.append(SchemaDefinition(**doc))

            logger.debug(
                "Listed schema definitions",
                extra={
                    "tenant_id": self._tenant_id,
                    "count": len(results),
                    "skip": skip,
                    "limit": effective_limit,
                    "filters": {
                        "erp_type": query.get("erp_type"),
                        "module": query.get("modules"),
                        "status": query.get("status"),
                    },
                },
            )
            return results
        except Exception:
            logger.error(
                "Failed to list schema definitions",
                extra={"tenant_id": self._tenant_id},
                exc_info=True,
            )
            raise

    # ------------------------------------------------------------------
    # Update
    # ------------------------------------------------------------------

    def update(self, schema_id: str, updates: dict[str, Any]) -> bool:
        """Apply a partial update to an existing schema definition.

        The ``updated_at`` timestamp is automatically set to the current
        UTC time.  The ``tenant_id`` and ``schema_id`` fields cannot be
        modified through this method.

        Args:
            schema_id: The unique identifier of the schema to update.
            updates: Dictionary of field names to new values.

        Returns:
            ``True`` if the document was found and updated, ``False``
            otherwise.

        Raises:
            Exception: Propagated from PyMongo on database errors.
        """
        try:
            # Prevent modification of identity fields
            sanitised = {
                k: v for k, v in updates.items() if k not in ("schema_id", "tenant_id", "_id")
            }
            sanitised["updated_at"] = datetime.now(timezone.utc)

            result = self._collection.update_one(
                {"schema_id": schema_id, "tenant_id": self._tenant_id},
                {"$set": sanitised},
            )
            updated = result.modified_count > 0

            if updated:
                logger.info(
                    "Schema definition updated",
                    extra={
                        "schema_id": schema_id,
                        "tenant_id": self._tenant_id,
                        "fields_updated": list(sanitised.keys()),
                    },
                )
            else:
                logger.warning(
                    "Schema definition update matched no documents",
                    extra={
                        "schema_id": schema_id,
                        "tenant_id": self._tenant_id,
                    },
                )
            return updated
        except Exception:
            logger.error(
                "Failed to update schema definition",
                extra={
                    "schema_id": schema_id,
                    "tenant_id": self._tenant_id,
                },
                exc_info=True,
            )
            raise

    def update_status(self, schema_id: str, status: SchemaStatus) -> bool:
        """Convenience method to update only the schema status.

        Equivalent to ``update(schema_id, {"status": status})`` but
        provides a clearer API for status transitions.

        Args:
            schema_id: The unique identifier of the schema to update.
            status: The new :class:`SchemaStatus` value.

        Returns:
            ``True`` if the status was successfully updated, ``False``
            if the schema was not found within this tenant.
        """
        status_value = status.value if isinstance(status, SchemaStatus) else status
        return self.update(schema_id, {"status": status_value})

    # ------------------------------------------------------------------
    # Delete
    # ------------------------------------------------------------------

    def delete(self, schema_id: str) -> bool:
        """Delete a schema definition document by ``schema_id``.

        The delete is always scoped to the repository's ``tenant_id``.

        Args:
            schema_id: The unique identifier of the schema to delete.

        Returns:
            ``True`` if a document was deleted, ``False`` if no matching
            document was found.
        """
        try:
            result = self._collection.delete_one(
                {"schema_id": schema_id, "tenant_id": self._tenant_id}
            )
            deleted = result.deleted_count > 0

            if deleted:
                logger.info(
                    "Schema definition deleted",
                    extra={
                        "schema_id": schema_id,
                        "tenant_id": self._tenant_id,
                    },
                )
            else:
                logger.warning(
                    "Schema definition delete matched no documents",
                    extra={
                        "schema_id": schema_id,
                        "tenant_id": self._tenant_id,
                    },
                )
            return deleted
        except Exception:
            logger.error(
                "Failed to delete schema definition",
                extra={
                    "schema_id": schema_id,
                    "tenant_id": self._tenant_id,
                },
                exc_info=True,
            )
            raise

    # ------------------------------------------------------------------
    # Count
    # ------------------------------------------------------------------

    def count(self, erp_type: Optional[ERPType] = None) -> int:
        """Count schema definitions for this tenant.

        Args:
            erp_type: Optional filter by ERP system type.

        Returns:
            The number of matching schema definition documents.
        """
        try:
            query: dict[str, Any] = {"tenant_id": self._tenant_id}
            if erp_type is not None:
                query["erp_type"] = erp_type.value if isinstance(erp_type, ERPType) else erp_type

            total = self._collection.count_documents(query)
            logger.debug(
                "Counted schema definitions",
                extra={
                    "tenant_id": self._tenant_id,
                    "erp_type": query.get("erp_type"),
                    "count": total,
                },
            )
            return total
        except Exception:
            logger.error(
                "Failed to count schema definitions",
                extra={"tenant_id": self._tenant_id},
                exc_info=True,
            )
            raise
