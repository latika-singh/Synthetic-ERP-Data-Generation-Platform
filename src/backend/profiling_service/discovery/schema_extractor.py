"""Schema metadata extraction module for the Profiling Service discovery package.

Provides the :class:`SchemaExtractor` class that orchestrates table and column
metadata extraction from ERP systems using the connector abstraction layer
(:mod:`profiling_service.connectors`).

**Extraction workflow:**

1. Instantiate the appropriate connector via :func:`get_connector`.
2. Open a context-managed connection (:meth:`BaseConnector.__enter__` calls
   :meth:`BaseConnector.connect`).
3. For each ERP module: discover tables → discover columns per table.
4. Convert connector-level metadata (:class:`TableMetadata`,
   :class:`ColumnMetadata`, :class:`RelationshipMetadata`) into domain model
   objects (:class:`TableDefinition`, :class:`ColumnDefinition`,
   :class:`ConstraintDefinition`, :class:`IndexDefinition`).
5. Persist the resulting :class:`SchemaDefinition` to the
   ``schema_definitions`` MongoDB collection.

**Constraint C-001 compliance:**
    Only *structural metadata* (table names, column names, data types,
    constraints, indexes, estimated row counts) is extracted.  **No raw
    production data is ever accessed or stored.**

**Constraint C-005:**
    Supports the four initial-release ERP modules — Financial Accounting,
    Human Resources, Sales & Distribution, Material Management.

Usage::

    from profiling_service.discovery.schema_extractor import SchemaExtractor

    extractor = SchemaExtractor(tenant_id="tenant-001")
    schema = extractor.extract_schema(
        erp_type="sap",
        connection_config=config,
    )
"""

from __future__ import annotations

import time
from datetime import datetime, timezone
from typing import Any, Optional

from profiling_service.connectors import get_connector
from profiling_service.connectors.base import (
    BaseConnector,
    ColumnMetadata,
    ConnectionConfig,
    RelationshipMetadata,
    TableMetadata,
)
from profiling_service.models.schema_definition import (
    ColumnDataType,
    ColumnDefinition,
    ConstraintDefinition,
    ERPModule,
    ERPType,
    IndexDefinition,
    SchemaDefinition,
    SchemaDefinitionRepository,
    SchemaStatus,
    TableDefinition,
)
from shared.database.mongodb import get_mongo_db
from shared.logging.structured_logger import get_logger
from shared.middleware.circuit_breaker import circuit_breaker_decorator


# ---------------------------------------------------------------------------
# Module-level logger
# ---------------------------------------------------------------------------

_logger = get_logger(__name__)


# ---------------------------------------------------------------------------
# Constant: connector standard_type → domain ColumnDataType
# ---------------------------------------------------------------------------

STANDARD_TYPE_TO_COLUMN_DATA_TYPE: dict[str, ColumnDataType] = {
    "VARCHAR": ColumnDataType.VARCHAR,
    "STRING": ColumnDataType.STRING,
    "INTEGER": ColumnDataType.INTEGER,
    "INT": ColumnDataType.INTEGER,
    "DECIMAL": ColumnDataType.DECIMAL,
    "FLOAT": ColumnDataType.FLOAT,
    "DATE": ColumnDataType.DATE,
    "TIMESTAMP": ColumnDataType.TIMESTAMP,
    "DATETIME": ColumnDataType.DATETIME,
    "BOOLEAN": ColumnDataType.BOOLEAN,
    "BOOL": ColumnDataType.BOOLEAN,
    "TEXT": ColumnDataType.TEXT,
    "BLOB": ColumnDataType.BLOB,
    "CHAR": ColumnDataType.CHAR,
    "BIGINT": ColumnDataType.BIGINT,
    "SMALLINT": ColumnDataType.SMALLINT,
    "NUMERIC": ColumnDataType.NUMERIC,
    "CLOB": ColumnDataType.CLOB,
}

# Default ERP modules to discover when the caller does not specify (C-005).
_DEFAULT_MODULES: list[str] = [
    ERPModule.FINANCIAL_ACCOUNTING,
    ERPModule.HUMAN_RESOURCES,
    ERPModule.SALES_DISTRIBUTION,
    ERPModule.MATERIAL_MANAGEMENT,
]


# ===================================================================
# SchemaExtractor
# ===================================================================


class SchemaExtractor:
    """Orchestrates ERP schema metadata extraction and persistence.

    Extracts table / column / constraint / index metadata from an ERP
    system using the connector abstraction layer.  The results are stored
    in the ``schema_definitions`` MongoDB collection as
    :class:`SchemaDefinition` documents.

    The extraction pipeline converts connector-level objects
    (:class:`TableMetadata`, :class:`ColumnMetadata`, and
    :class:`RelationshipMetadata`) into domain model objects
    (:class:`TableDefinition`, :class:`ColumnDefinition`,
    :class:`ConstraintDefinition`, :class:`IndexDefinition`).

    Attributes:
        tenant_id: Tenant scope for multi-tenant isolation.
    """

    # Connector-level metadata type for FK relationship tracking.
    # Relationship discovery is orchestrated by the relationship_mapper
    # module, which converts RelationshipMetadata into domain
    # RelationshipDefinition objects for cross-table FK tracking.
    _relationship_metadata_type: type[RelationshipMetadata] = RelationshipMetadata

    # ---------------------------------------------------------------
    # Initialisation
    # ---------------------------------------------------------------

    def __init__(
        self,
        tenant_id: str,
        config: Optional[dict[str, Any]] = None,
    ) -> None:
        """Initialise the schema extractor.

        Args:
            tenant_id: Tenant identifier used for all MongoDB operations
                to enforce multi-tenant isolation.
            config: Optional overrides for extraction parameters.  Accepted
                keys:

                - ``max_tables`` (int, default 500): Maximum tables to
                  extract across all modules.
                - ``batch_size`` (int, default 50): Tables to process per
                  batch during extraction.
                - ``timeout`` (int, default 300): Extraction timeout in
                  seconds.

        Raises:
            RuntimeError: If the MongoDB health check fails at
                initialisation.
        """
        self.tenant_id: str = tenant_id
        self._logger = get_logger(__name__)
        self._repo = SchemaDefinitionRepository(tenant_id)

        effective_config: dict[str, Any] = config or {}
        self._max_tables: int = int(effective_config.get("max_tables", 500))
        self._batch_size: int = int(effective_config.get("batch_size", 50))
        self._timeout: int = int(effective_config.get("timeout", 300))

        # Verify MongoDB connectivity before accepting extraction requests.
        try:
            self._db = get_mongo_db()
            self._logger.debug(
                "database_health_check_passed",
                tenant_id=tenant_id,
            )
        except Exception as exc:
            self._logger.error(
                "database_health_check_failed",
                tenant_id=tenant_id,
                error=str(exc),
                exc_info=True,
            )
            raise RuntimeError(
                f"MongoDB health verification failed for tenant "
                f"'{tenant_id}': {exc}"
            ) from exc

        self._logger.info(
            "schema_extractor_initialised",
            tenant_id=tenant_id,
            max_tables=self._max_tables,
            batch_size=self._batch_size,
            timeout=self._timeout,
        )

    # ---------------------------------------------------------------
    # Public — extract_schema  (main entry point)
    # ---------------------------------------------------------------

    @circuit_breaker_decorator(
        name="erp_schema_extraction",
        failure_threshold=3,
        recovery_timeout=60,
        max_retries=2,
    )
    def extract_schema(
        self,
        erp_type: str,
        connection_config: ConnectionConfig,
        modules: list[str] | None = None,
        connection_name: str = "default",
        tags: list[str] | None = None,
    ) -> SchemaDefinition:
        """Extract a complete schema from an ERP system.

        Creates an initial :class:`SchemaDefinition` document with
        status ``DISCOVERING``, performs metadata extraction for all
        requested modules, then updates the document to ``DISCOVERED``.
        On any error the status is set to ``FAILED`` and the exception
        is re-raised.

        Protected by the circuit breaker pattern to prevent cascade
        failures when ERP systems are unavailable or responding slowly.

        Args:
            erp_type: ERP system identifier (e.g. ``"sap"``,
                ``"oracle_ebs"``, ``"dynamics_365"``, ``"jdbc"``).
            connection_config: :class:`ConnectionConfig` holding
                connection parameters for the ERP system.
            modules: ERP modules to discover.  ``None`` defaults to all
                four initial-release modules (C-005).
            connection_name: Human-friendly label for this connection.
            tags: Optional user-defined tags for categorisation.

        Returns:
            The completed :class:`SchemaDefinition` with tables, columns,
            constraints, and indexes populated.

        Raises:
            ValueError: If *erp_type* cannot be resolved to a valid
                :class:`ERPType` enum member.
            RuntimeError: On unrecoverable connector or MongoDB failures.
        """
        start: float = time.time()
        extraction_start: datetime = datetime.now(timezone.utc)

        # ----- Resolve ERPType enum -----
        try:
            erp_type_enum = ERPType(erp_type.lower())
        except ValueError:
            try:
                erp_type_enum = ERPType(erp_type)
            except ValueError as exc:
                self._logger.error(
                    "invalid_erp_type",
                    erp_type=erp_type,
                    supported=[e.value for e in ERPType],
                )
                raise ValueError(
                    f"Unsupported ERP type: '{erp_type}'. "
                    f"Supported: {[e.value for e in ERPType]}"
                ) from exc

        # ----- Create initial DISCOVERING document -----
        schema = SchemaDefinition(
            tenant_id=self.tenant_id,
            erp_type=erp_type_enum,
            connection_name=connection_name,
            status=SchemaStatus.DISCOVERING,
            tags=tags or [],
            created_at=extraction_start,
            updated_at=extraction_start,
        )

        try:
            schema_id: str = self._repo.create(schema)
            schema.schema_id = schema_id
        except Exception:
            self._logger.error(
                "schema_create_failed",
                erp_type=erp_type,
                tenant_id=self.tenant_id,
                exc_info=True,
            )
            raise

        self._logger.info(
            "schema_extraction_started",
            schema_id=schema.schema_id,
            erp_type=erp_type,
            connection_name=connection_name,
            tenant_id=self.tenant_id,
        )

        try:
            # Instantiate the appropriate ERP connector.
            connector: BaseConnector = get_connector(
                erp_type, connection_config
            )

            # Context manager: __enter__() → connect(), __exit__() → close().
            with connector:
                # Determine target modules (default: all four per C-005).
                target_modules: list[str] = modules or list(_DEFAULT_MODULES)

                all_tables: list[TableDefinition] = []
                all_module_enums: list[ERPModule] = []

                for module_str in target_modules:
                    # Resolve module string to ERPModule enum.
                    try:
                        module_enum = ERPModule(module_str)
                    except ValueError:
                        try:
                            module_enum = ERPModule(module_str.lower())
                        except ValueError:
                            self._logger.warning(
                                "unknown_module_skipped",
                                module=module_str,
                                schema_id=schema.schema_id,
                            )
                            continue

                    self._logger.info(
                        "extracting_module",
                        module=module_str,
                        erp_type=erp_type,
                        schema_id=schema.schema_id,
                    )

                    tables: list[TableDefinition] = (
                        self._extract_tables_for_module(
                            connector, module_enum
                        )
                    )
                    all_tables.extend(tables)
                    if tables:
                        all_module_enums.append(module_enum)

                    self._logger.info(
                        "module_extraction_complete",
                        module=module_str,
                        tables_extracted=len(tables),
                        columns_extracted=sum(
                            len(t.columns) for t in tables
                        ),
                    )

                # ----- Compute aggregate totals -----
                total_cols: int = sum(
                    len(t.columns) for t in all_tables
                )
                total_constraints: int = sum(
                    len(t.constraints) for t in all_tables
                )
                total_indexes: int = sum(
                    len(t.indexes) for t in all_tables
                )

                # ----- Compute timing -----
                duration: float = round(time.time() - start, 3)
                completion_time: datetime = datetime.now(timezone.utc)

                # ----- Update domain object -----
                schema.tables = all_tables
                schema.modules = all_module_enums
                schema.total_tables = len(all_tables)
                schema.total_columns = total_cols
                schema.status = SchemaStatus.DISCOVERED
                schema.updated_at = completion_time
                schema.discovery_metadata = {
                    "erp_type": erp_type,
                    "connection_name": connection_name,
                    "modules_discovered": [
                        str(m) for m in all_module_enums
                    ],
                    "total_constraints": total_constraints,
                    "total_indexes": total_indexes,
                    "duration_seconds": duration,
                    "started_at": extraction_start.isoformat(),
                    "completed_at": completion_time.isoformat(),
                }

                # ----- Persist updated schema to MongoDB -----
                self._repo.update(
                    schema.schema_id,
                    {
                        "tables": [
                            t.model_dump() for t in all_tables
                        ],
                        "modules": [
                            str(m) for m in all_module_enums
                        ],
                        "total_tables": len(all_tables),
                        "total_columns": total_cols,
                        "status": SchemaStatus.DISCOVERED.value,
                        "updated_at": completion_time,
                        "discovery_metadata": schema.discovery_metadata,
                    },
                )

            # Log completion outside the context manager.
            self._logger.info(
                "schema_extraction_complete",
                schema_id=schema.schema_id,
                total_tables=len(all_tables),
                total_columns=total_cols,
                total_constraints=total_constraints,
                total_indexes=total_indexes,
                modules=len(all_module_enums),
                duration_seconds=duration,
                tenant_id=self.tenant_id,
            )
            return schema

        except Exception:
            # ----- Mark schema as FAILED -----
            error_time: datetime = datetime.now(timezone.utc)
            try:
                self._repo.update(
                    schema.schema_id,
                    {
                        "status": SchemaStatus.FAILED.value,
                        "updated_at": error_time,
                        "discovery_metadata": {
                            "erp_type": erp_type,
                            "connection_name": connection_name,
                            "failed_at": error_time.isoformat(),
                            "duration_seconds": round(
                                time.time() - start, 3
                            ),
                        },
                    },
                )
            except Exception:
                self._logger.error(
                    "schema_status_update_to_failed_error",
                    schema_id=schema.schema_id,
                    exc_info=True,
                )
            self._logger.error(
                "schema_extraction_failed",
                schema_id=schema.schema_id,
                erp_type=erp_type,
                tenant_id=self.tenant_id,
                exc_info=True,
            )
            raise

    # ---------------------------------------------------------------
    # Public — CRUD convenience wrappers
    # ---------------------------------------------------------------

    def get_schema_by_id(
        self, schema_id: str
    ) -> Optional[SchemaDefinition]:
        """Retrieve a schema by its unique ID (tenant-scoped).

        Delegates to :meth:`SchemaDefinitionRepository.get_by_id`.

        Args:
            schema_id: The ``schema_id`` to look up.

        Returns:
            The :class:`SchemaDefinition` if found, else ``None``.
        """
        self._logger.debug(
            "get_schema_by_id",
            schema_id=schema_id,
            tenant_id=self.tenant_id,
        )
        return self._repo.get_by_id(schema_id)

    def list_schemas(
        self,
        erp_type: Optional[str] = None,
        module: Optional[str] = None,
        status: Optional[str] = None,
        skip: int = 0,
        limit: int = 20,
    ) -> list[SchemaDefinition]:
        """List schemas with optional filters and pagination.

        Converts string filter values to their enum counterparts.
        Invalid filter values are logged as warnings and silently
        ignored so the query proceeds without the invalid filter.

        Args:
            erp_type: Filter by ERP type (e.g. ``"sap"``).
            module: Filter by ERP module (e.g. ``"financial_accounting"``).
            status: Filter by schema status (e.g. ``"discovered"``).
            skip: Number of documents to skip for pagination.
            limit: Maximum number of documents to return.

        Returns:
            List of matching :class:`SchemaDefinition` instances.
        """
        erp_type_enum: Optional[ERPType] = None
        module_enum: Optional[ERPModule] = None
        status_enum: Optional[SchemaStatus] = None

        if erp_type is not None:
            try:
                erp_type_enum = ERPType(erp_type)
            except ValueError:
                self._logger.warning(
                    "invalid_erp_type_filter",
                    erp_type=erp_type,
                )

        if module is not None:
            try:
                module_enum = ERPModule(module)
            except ValueError:
                self._logger.warning(
                    "invalid_module_filter",
                    module=module,
                )

        if status is not None:
            try:
                status_enum = SchemaStatus(status)
            except ValueError:
                self._logger.warning(
                    "invalid_status_filter",
                    status=status,
                )

        self._logger.debug(
            "list_schemas",
            erp_type=erp_type,
            module=module,
            status=status,
            skip=skip,
            limit=limit,
            tenant_id=self.tenant_id,
        )

        return self._repo.list_schemas(
            erp_type=erp_type_enum,
            module=module_enum,
            status=status_enum,
            skip=skip,
            limit=limit,
        )

    def delete_schema(self, schema_id: str) -> bool:
        """Delete a schema by its unique ID (tenant-scoped).

        Delegates to :meth:`SchemaDefinitionRepository.delete`.

        Args:
            schema_id: The ``schema_id`` to delete.

        Returns:
            ``True`` if the document was deleted, ``False`` otherwise.
        """
        self._logger.info(
            "delete_schema_requested",
            schema_id=schema_id,
            tenant_id=self.tenant_id,
        )
        result: bool = self._repo.delete(schema_id)
        if result:
            self._logger.info(
                "schema_deleted",
                schema_id=schema_id,
                tenant_id=self.tenant_id,
            )
        else:
            self._logger.warning(
                "schema_delete_not_found",
                schema_id=schema_id,
                tenant_id=self.tenant_id,
            )
        return result

    # ---------------------------------------------------------------
    # Private — module-level extraction
    # ---------------------------------------------------------------

    def _extract_tables_for_module(
        self,
        connector: BaseConnector,
        module: ERPModule,
    ) -> list[TableDefinition]:
        """Extract table definitions for a single ERP module.

        For each table discovered by the connector this method:

        1. Calls :meth:`BaseConnector.discover_tables` for the module.
        2. Calls :meth:`_extract_columns` for every table.
        3. Derives :class:`ConstraintDefinition` entries (primary key,
           not-null) and :class:`IndexDefinition` entries from the
           extracted column metadata.
        4. Assembles a :class:`TableDefinition` with all sub-objects.

        Args:
            connector: An active, context-managed ERP connector.
            module: The ERP module to discover tables for.

        Returns:
            List of fully populated :class:`TableDefinition` instances.
        """
        table_defs: list[TableDefinition] = []

        try:
            table_metas: list[TableMetadata] = connector.discover_tables(
                module=module.value,
            )
        except Exception:
            self._logger.error(
                "table_discovery_failed",
                module=module.value,
                exc_info=True,
            )
            return table_defs

        self._logger.debug(
            "tables_discovered",
            module=module.value,
            count=len(table_metas),
        )

        for tmeta in table_metas:
            if len(table_defs) >= self._max_tables:
                self._logger.warning(
                    "max_tables_reached",
                    module=module.value,
                    max_tables=self._max_tables,
                )
                break

            # Extract column metadata → ColumnDefinition list.
            columns: list[ColumnDefinition] = self._extract_columns(
                connector, tmeta.table_name
            )

            # Derive primary-key column names.
            pk_cols: list[str] = [
                c.column_name for c in columns if c.is_primary_key
            ]

            # ----- Build ConstraintDefinition entries -----
            constraints: list[ConstraintDefinition] = []

            # Primary key constraint (if any PK columns exist).
            if pk_cols:
                constraints.append(
                    ConstraintDefinition(
                        constraint_name=f"pk_{tmeta.table_name}",
                        constraint_type="PRIMARY_KEY",
                        columns=pk_cols,
                        description=(
                            f"Primary key constraint on "
                            f"{tmeta.table_name}"
                        ),
                    )
                )

            # NOT NULL constraints for non-nullable, non-PK columns.
            for col in columns:
                if not col.is_nullable and not col.is_primary_key:
                    constraints.append(
                        ConstraintDefinition(
                            constraint_name=(
                                f"nn_{tmeta.table_name}"
                                f"_{col.column_name}"
                            ),
                            constraint_type="NOT_NULL",
                            columns=[col.column_name],
                            description=(
                                f"Not-null constraint on "
                                f"{col.column_name}"
                            ),
                        )
                    )

            # ----- Build IndexDefinition entries -----
            indexes: list[IndexDefinition] = []

            if pk_cols:
                indexes.append(
                    IndexDefinition(
                        index_name=f"idx_pk_{tmeta.table_name}",
                        columns=pk_cols,
                        is_unique=True,
                        is_clustered=True,
                        description=(
                            f"Primary key index on {tmeta.table_name}"
                        ),
                    )
                )

            # Assemble the table definition.
            table_def = TableDefinition(
                table_name=tmeta.table_name,
                schema_name=tmeta.schema_name,
                erp_module=module,
                columns=columns,
                constraints=constraints,
                indexes=indexes,
                primary_key_columns=pk_cols,
                estimated_row_count=tmeta.estimated_row_count,
                description=tmeta.description,
            )
            table_defs.append(table_def)

        return table_defs

    # ---------------------------------------------------------------
    # Private — column-level extraction
    # ---------------------------------------------------------------

    def _extract_columns(
        self,
        connector: BaseConnector,
        table_name: str,
    ) -> list[ColumnDefinition]:
        """Extract column definitions for a single table.

        Calls :meth:`BaseConnector.discover_columns` and converts each
        :class:`ColumnMetadata` object into a :class:`ColumnDefinition`
        domain model.

        Args:
            connector: An active, context-managed ERP connector.
            table_name: The fully-qualified table name to discover
                columns for.

        Returns:
            List of :class:`ColumnDefinition` instances.  Returns an
            empty list on discovery failure.
        """
        columns: list[ColumnDefinition] = []

        try:
            col_metas: list[ColumnMetadata] = connector.discover_columns(
                table_name
            )
        except Exception:
            self._logger.error(
                "column_discovery_failed",
                table=table_name,
                exc_info=True,
            )
            return columns

        for cmeta in col_metas:
            col_type: ColumnDataType = self._map_column_data_type(
                cmeta.standard_type
            )
            col_def = ColumnDefinition(
                column_name=cmeta.column_name,
                data_type=col_type,
                native_type=cmeta.native_type,
                is_nullable=cmeta.is_nullable,
                is_primary_key=cmeta.is_primary_key,
                max_length=cmeta.max_length,
                precision=cmeta.precision,
                scale=cmeta.scale,
                default_value=cmeta.default_value,
                description=cmeta.description,
            )
            columns.append(col_def)

        return columns

    # ---------------------------------------------------------------
    # Private — type mapping
    # ---------------------------------------------------------------

    def _map_column_data_type(
        self, standard_type: Optional[str]
    ) -> ColumnDataType:
        """Convert a connector-provided ``standard_type`` string to a
        :class:`ColumnDataType` enum member.

        Falls back to :attr:`ColumnDataType.STRING` for unknown or
        ``None`` input values.

        Args:
            standard_type: Normalised type string from the connector
                (e.g. ``"VARCHAR"``, ``"INTEGER"``).

        Returns:
            The corresponding :class:`ColumnDataType` enum value.
        """
        if standard_type is None:
            return ColumnDataType.STRING
        key: str = standard_type.upper().strip()
        return STANDARD_TYPE_TO_COLUMN_DATA_TYPE.get(
            key, ColumnDataType.STRING
        )
