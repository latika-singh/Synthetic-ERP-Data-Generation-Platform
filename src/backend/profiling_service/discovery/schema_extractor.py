"""Schema metadata extraction for the Profiling Service.

Provides the :class:`SchemaExtractor` that orchestrates table and column
metadata extraction from ERP systems via the
:mod:`profiling_service.connectors` abstraction layer.

**Extraction workflow:**

1. Instantiate a connector via :func:`get_connector`.
2. Call :meth:`BaseConnector.connect`.
3. For each ERP module: discover tables → discover columns per table.
4. Convert connector-level metadata (``TableMetadata``,
   ``ColumnMetadata``) into domain model objects (``TableDefinition``,
   ``ColumnDefinition``).
5. Persist the resulting :class:`SchemaDefinition` to the
   ``schema_definitions`` MongoDB collection.

**Constraint C-001 compliance:**
    Only *structural metadata* (table names, column names, data types,
    constraints, indexes, estimated row counts) is extracted.  No raw
    production data is ever accessed or stored.

**Constraint C-005:**
    Supports the four initial-release ERP modules — Financial Accounting,
    Human Resources, Sales & Distribution, Material Management.

Usage::

    from profiling_service.discovery.schema_extractor import (
        SchemaExtractor,
    )

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

from profiling_service.connectors.base import (
    BaseConnector,
    ColumnMetadata,
    ConnectionConfig,
    RelationshipMetadata,
    TableMetadata,
)
from profiling_service.connectors import get_connector
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
from shared.logging.structured_logger import get_logger
from shared.middleware.circuit_breaker import circuit_breaker_decorator
from shared.database.mongodb import get_mongo_db


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

# Default ERP modules to discover when the caller doesn't specify.
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

    Attributes:
        tenant_id: Tenant scope for multi-tenant isolation.
    """

    # ---------------------------------------------------------------
    # Initialisation
    # ---------------------------------------------------------------

    def __init__(
        self,
        tenant_id: str,
        config: Optional[dict[str, Any]] = None,
    ) -> None:
        """Initialise the extractor.

        Args:
            tenant_id: Tenant identifier for all MongoDB operations.
            config: Optional overrides for extraction parameters.  Keys:
                ``max_tables`` (int, default 500),
                ``batch_size`` (int, default 50),
                ``timeout`` (int, default 300).
        """
        self.tenant_id: str = tenant_id
        self._logger = get_logger(__name__)
        self._repo = SchemaDefinitionRepository(tenant_id)

        effective_config = config or {}
        self._max_tables: int = int(effective_config.get("max_tables", 500))
        self._batch_size: int = int(effective_config.get("batch_size", 50))
        self._timeout: int = int(effective_config.get("timeout", 300))

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

    def extract_schema(
        self,
        erp_type: str,
        connection_config: ConnectionConfig,
        modules: list[str] | None = None,
        connection_name: str = "default",
        tags: list[str] | None = None,
    ) -> SchemaDefinition:
        """Extract a complete schema from an ERP system.

        Creates an initial :class:`SchemaDefinition` (status =
        ``DISCOVERING``), performs metadata extraction, then updates the
        document to ``DISCOVERED``.  On error the status is set to
        ``FAILED`` and the exception is re-raised.

        Args:
            erp_type: ERP system identifier (e.g. ``"sap"``,
                ``"oracle_ebs"``, ``"dynamics_365"``, ``"jdbc"``).
            connection_config: Connection parameters for the ERP system.
            modules: ERP modules to discover.  ``None`` = all four
                default modules.
            connection_name: Human-friendly name for this connection.
            tags: Optional user-defined tags.

        Returns:
            The completed :class:`SchemaDefinition` with tables and
            columns populated.

        Raises:
            Exception: Propagated from the connector or MongoDB on
                unrecoverable errors.
        """
        start = time.time()

        # Resolve the ERPType enum.
        try:
            erp_type_enum = ERPType(erp_type.lower())
        except ValueError:
            erp_type_enum = ERPType(erp_type)

        # Create initial document.
        schema = SchemaDefinition(
            tenant_id=self.tenant_id,
            erp_type=erp_type_enum,
            connection_name=connection_name,
            status=SchemaStatus.DISCOVERING,
            tags=tags or [],
        )

        try:
            schema_id = self._repo.create(schema)
            schema.schema_id = schema_id
        except Exception:
            self._logger.error(
                "schema_create_failed",
                erp_type=erp_type,
                exc_info=True,
            )
            raise

        try:
            # Instantiate and connect to the ERP system.
            connector = get_connector(erp_type, connection_config)
            with connector:
                connector.connect()

                # Determine which modules to extract.
                target_modules = modules or list(_DEFAULT_MODULES)

                all_tables: list[TableDefinition] = []
                all_module_enums: list[ERPModule] = []

                for module_str in target_modules:
                    try:
                        module_enum = ERPModule(module_str)
                    except ValueError:
                        module_enum = ERPModule(module_str.lower())

                    self._logger.info(
                        "extracting_module",
                        module=module_str,
                        erp_type=erp_type,
                    )

                    tables = self._extract_tables_for_module(
                        connector, module_enum
                    )
                    all_tables.extend(tables)
                    if tables:
                        all_module_enums.append(module_enum)

                    self._logger.info(
                        "module_extraction_complete",
                        module=module_str,
                        tables_extracted=len(tables),
                    )

                # Compute totals.
                total_cols = sum(len(t.columns) for t in all_tables)

                # Update domain object.
                schema.tables = all_tables
                schema.modules = all_module_enums
                schema.total_tables = len(all_tables)
                schema.total_columns = total_cols
                schema.status = SchemaStatus.DISCOVERED

                duration = round(time.time() - start, 3)
                schema.discovery_metadata = {
                    "erp_type": erp_type,
                    "connection_name": connection_name,
                    "modules_discovered": [str(m) for m in all_module_enums],
                    "duration_seconds": duration,
                }

                # Persist the updated schema.
                self._repo.update(
                    schema.schema_id,
                    {
                        "tables": [t.model_dump() for t in all_tables],
                        "modules": [str(m) for m in all_module_enums],
                        "total_tables": len(all_tables),
                        "total_columns": total_cols,
                        "status": SchemaStatus.DISCOVERED.value,
                        "discovery_metadata": schema.discovery_metadata,
                    },
                )

            self._logger.info(
                "schema_extraction_complete",
                schema_id=schema.schema_id,
                total_tables=len(all_tables),
                total_columns=total_cols,
                modules=len(all_module_enums),
                duration_seconds=duration,
            )
            return schema

        except Exception:
            # Mark the schema as FAILED.
            try:
                self._repo.update(
                    schema.schema_id,
                    {"status": SchemaStatus.FAILED.value},
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
                exc_info=True,
            )
            raise

    # ---------------------------------------------------------------
    # Public — CRUD convenience wrappers
    # ---------------------------------------------------------------

    def get_schema_by_id(self, schema_id: str) -> Optional[SchemaDefinition]:
        """Retrieve a schema by its unique ID (tenant-scoped).

        Args:
            schema_id: The ``schema_id`` to look up.

        Returns:
            The :class:`SchemaDefinition` or ``None`` if not found.
        """
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

        Args:
            erp_type: Filter by ERP type string.
            module: Filter by ERP module string.
            status: Filter by schema status string.
            skip: Documents to skip.
            limit: Maximum documents to return.

        Returns:
            List of matching :class:`SchemaDefinition` instances.
        """
        erp_type_enum: ERPType | None = None
        module_enum: ERPModule | None = None
        status_enum: SchemaStatus | None = None

        if erp_type:
            try:
                erp_type_enum = ERPType(erp_type)
            except ValueError:
                pass
        if module:
            try:
                module_enum = ERPModule(module)
            except ValueError:
                pass
        if status:
            try:
                status_enum = SchemaStatus(status)
            except ValueError:
                pass

        return self._repo.list_schemas(
            erp_type=erp_type_enum,
            module=module_enum,
            status=status_enum,
            skip=skip,
            limit=limit,
        )

    def delete_schema(self, schema_id: str) -> bool:
        """Delete a schema by its unique ID (tenant-scoped).

        Args:
            schema_id: The ``schema_id`` to delete.

        Returns:
            ``True`` if the document was deleted.
        """
        return self._repo.delete(schema_id)

    # ---------------------------------------------------------------
    # Private — module-level extraction
    # ---------------------------------------------------------------

    def _extract_tables_for_module(
        self,
        connector: BaseConnector,
        module: ERPModule,
    ) -> list[TableDefinition]:
        """Extract table definitions for a single ERP module.

        Calls the connector's :meth:`discover_tables` and then
        :meth:`discover_columns` for each discovered table.

        Args:
            connector: An active, connected ERP connector.
            module: The ERP module to discover.

        Returns:
            List of :class:`TableDefinition` instances.
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

        for tmeta in table_metas:
            if len(table_defs) >= self._max_tables:
                self._logger.warning(
                    "max_tables_reached",
                    module=module.value,
                    max_tables=self._max_tables,
                )
                break

            columns = self._extract_columns(connector, tmeta.table_name)

            pk_cols = [c.column_name for c in columns if c.is_primary_key]

            table_def = TableDefinition(
                table_name=tmeta.table_name,
                schema_name=tmeta.schema_name,
                erp_module=module,
                columns=columns,
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

        Args:
            connector: An active, connected ERP connector.
            table_name: The table to discover columns for.

        Returns:
            List of :class:`ColumnDefinition` instances.
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
            col_type = self._map_column_data_type(cmeta.standard_type)
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

    def _map_column_data_type(self, standard_type: str | None) -> ColumnDataType:
        """Convert a connector-provided standard_type string to a
        :class:`ColumnDataType` enum member.

        Falls back to :attr:`ColumnDataType.STRING` if the type is
        unknown or ``None``.

        Args:
            standard_type: Normalised type string from the connector
                (e.g. ``"VARCHAR"``, ``"INTEGER"``).

        Returns:
            The corresponding :class:`ColumnDataType`.
        """
        if standard_type is None:
            return ColumnDataType.STRING
        key = standard_type.upper().strip()
        return STANDARD_TYPE_TO_COLUMN_DATA_TYPE.get(key, ColumnDataType.STRING)
