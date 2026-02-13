"""Generic JDBC schema discovery connector for legacy ERP systems.

Implements :class:`JDBCConnector`, a concrete subclass of
:class:`~profiling_service.connectors.base.BaseConnector` that discovers
schema metadata from any JDBC-compliant relational database via the
standard ``java.sql.DatabaseMetaData`` API.

**Connection strategy:**
    Uses ``jaydebeapi`` for JDBC connectivity with a configurable driver
    class and JAR path.  When ``jaydebeapi`` is not available the connector
    falls back to an offline simulation mode that returns a minimal generic
    table set suitable for integration testing.

**Privacy Guarantee (Constraint C-001):**
    All discovery operations use the JDBC ``DatabaseMetaData`` API
    (``getTables``, ``getColumns``, ``getPrimaryKeys``,
    ``getImportedKeys`` / ``getExportedKeys``).  **No** ``SELECT``
    statements are executed against business data tables.

**Supported ERP Modules (Constraint C-005):**

- Financial Accounting
- Human Resources
- Sales & Distribution
- Material Management

Design Patterns:
    - **Strategy** — JDBCConnector encapsulates generic JDBC discovery
      behind the ``BaseConnector`` abstract interface.
    - **Circuit Breaker** — External JDBC calls are wrapped with the
      shared circuit-breaker decorator.
"""

from __future__ import annotations

from typing import Any

from profiling_service.connectors.base import (
    BaseConnector,
    ColumnMetadata,
    ConnectionConfig,
    ConnectionError,  # noqa: A004
    ConnectorError,
    DiscoveryError,
    ERPModule,
    RelationshipMetadata,
    TableMetadata,
)
from shared.logging.structured_logger import get_logger
from shared.middleware.circuit_breaker import circuit_breaker_decorator


# ---------------------------------------------------------------------------
# Module-level logger
# ---------------------------------------------------------------------------

_logger = get_logger(__name__)

# ---------------------------------------------------------------------------
# JDBC SQL Type Codes → Standard Types
# ---------------------------------------------------------------------------

JDBC_TYPE_MAPPING: dict[int, str] = {
    # Character types
    1:    "VARCHAR",     # CHAR
    12:   "VARCHAR",     # VARCHAR
    -1:   "TEXT",        # LONGVARCHAR
    -15:  "VARCHAR",     # NCHAR
    -9:   "VARCHAR",     # NVARCHAR
    -16:  "TEXT",        # LONGNVARCHAR

    # Numeric types
    -6:   "INTEGER",     # TINYINT
    5:    "INTEGER",     # SMALLINT
    4:    "INTEGER",     # INTEGER
    -5:   "BIGINT",      # BIGINT
    7:    "FLOAT",       # REAL
    6:    "FLOAT",       # FLOAT
    8:    "FLOAT",       # DOUBLE
    2:    "DECIMAL",     # NUMERIC
    3:    "DECIMAL",     # DECIMAL

    # Date / Time types
    91:   "DATE",        # DATE
    92:   "TIME",        # TIME
    93:   "TIMESTAMP",   # TIMESTAMP
    2013: "TIME",        # TIME_WITH_TIMEZONE
    2014: "TIMESTAMP",   # TIMESTAMP_WITH_TIMEZONE

    # Boolean / Bit
    16:   "BOOLEAN",     # BOOLEAN
    -7:   "BOOLEAN",     # BIT

    # Binary types
    -2:   "BINARY",      # BINARY
    -3:   "BINARY",      # VARBINARY
    -4:   "BINARY",      # LONGVARBINARY
    2004: "BINARY",      # BLOB

    # LOB text types
    2005: "TEXT",         # CLOB
    2011: "TEXT",         # NCLOB

    # Special types
    1111: "VARCHAR",     # OTHER
    2009: "TEXT",        # SQLXML
    -8:   "VARCHAR",     # ROWID
}

# Human-readable JDBC type name → standard fallback
JDBC_TYPE_NAME_MAPPING: dict[str, str] = {
    "VARCHAR":      "VARCHAR",
    "VARCHAR2":     "VARCHAR",
    "CHAR":         "VARCHAR",
    "NCHAR":        "VARCHAR",
    "NVARCHAR":     "VARCHAR",
    "NVARCHAR2":    "VARCHAR",
    "TEXT":         "TEXT",
    "CLOB":         "TEXT",
    "NCLOB":        "TEXT",
    "INT":          "INTEGER",
    "INT4":         "INTEGER",
    "INTEGER":      "INTEGER",
    "SMALLINT":     "INTEGER",
    "TINYINT":      "INTEGER",
    "BIGINT":       "BIGINT",
    "INT8":         "BIGINT",
    "SERIAL":       "INTEGER",
    "BIGSERIAL":    "BIGINT",
    "FLOAT":        "FLOAT",
    "DOUBLE":       "FLOAT",
    "REAL":         "FLOAT",
    "NUMERIC":      "DECIMAL",
    "DECIMAL":      "DECIMAL",
    "NUMBER":       "DECIMAL",
    "MONEY":        "DECIMAL",
    "DATE":         "DATE",
    "TIME":         "TIME",
    "TIMESTAMP":    "TIMESTAMP",
    "DATETIME":     "TIMESTAMP",
    "DATETIME2":    "TIMESTAMP",
    "BOOLEAN":      "BOOLEAN",
    "BOOL":         "BOOLEAN",
    "BIT":          "BOOLEAN",
    "BYTEA":        "BINARY",
    "BLOB":         "BINARY",
    "BINARY":       "BINARY",
    "VARBINARY":    "BINARY",
    "RAW":          "BINARY",
    "UUID":         "UUID",
    "UNIQUEIDENTIFIER": "UUID",
    "XML":          "TEXT",
    "JSON":         "TEXT",
    "JSONB":        "TEXT",
}

# ---------------------------------------------------------------------------
# Generic Legacy Module → Table-Name-Prefix Heuristics
# ---------------------------------------------------------------------------

LEGACY_MODULE_PREFIXES: dict[str, list[str]] = {
    ERPModule.FINANCIAL_ACCOUNTING: [
        "GL_", "AP_", "AR_", "FA_", "JOURNAL", "LEDGER", "INVOICE",
        "PAYMENT", "RECEIPT", "ACCOUNT",
    ],
    ERPModule.HUMAN_RESOURCES: [
        "HR_", "EMP", "PAY", "BENEFIT", "POSITION", "WORKER",
        "PERSON", "SALARY", "LEAVE",
    ],
    ERPModule.SALES_DISTRIBUTION: [
        "SO_", "SALES", "ORDER", "CUSTOMER", "CUST_", "PRICING",
        "DELIVERY", "SHIPMENT",
    ],
    ERPModule.MATERIAL_MANAGEMENT: [
        "PO_", "PURCH", "INV_", "INVENTORY", "MATERIAL", "VENDOR",
        "SUPPLIER", "ITEM", "STOCK", "WAREHOUSE",
    ],
}

# ---------------------------------------------------------------------------
# Offline generic table dictionary (for test / offline mode)
# ---------------------------------------------------------------------------

_GENERIC_DD_TABLES: list[dict[str, Any]] = [
    {"name": "GL_JOURNAL_HEADER",  "schema": "ERP", "desc": "General Ledger Journal Header", "module": ERPModule.FINANCIAL_ACCOUNTING},
    {"name": "GL_JOURNAL_LINE",    "schema": "ERP", "desc": "General Ledger Journal Line",   "module": ERPModule.FINANCIAL_ACCOUNTING},
    {"name": "AP_INVOICE",         "schema": "ERP", "desc": "Accounts Payable Invoice",      "module": ERPModule.FINANCIAL_ACCOUNTING},
    {"name": "HR_EMPLOYEE",        "schema": "ERP", "desc": "Employee Master",               "module": ERPModule.HUMAN_RESOURCES},
    {"name": "HR_PAYROLL",         "schema": "ERP", "desc": "Payroll Record",                "module": ERPModule.HUMAN_RESOURCES},
    {"name": "SALES_ORDER_HEADER", "schema": "ERP", "desc": "Sales Order Header",            "module": ERPModule.SALES_DISTRIBUTION},
    {"name": "SALES_ORDER_LINE",   "schema": "ERP", "desc": "Sales Order Line",              "module": ERPModule.SALES_DISTRIBUTION},
    {"name": "PO_HEADER",          "schema": "ERP", "desc": "Purchase Order Header",         "module": ERPModule.MATERIAL_MANAGEMENT},
    {"name": "PO_LINE",            "schema": "ERP", "desc": "Purchase Order Line",            "module": ERPModule.MATERIAL_MANAGEMENT},
    {"name": "INVENTORY_ITEM",     "schema": "ERP", "desc": "Inventory Item Master",         "module": ERPModule.MATERIAL_MANAGEMENT},
]

_GENERIC_DD_COLUMNS: dict[str, list[dict[str, Any]]] = {
    "GL_JOURNAL_HEADER": [
        {"name": "JOURNAL_ID",   "type_code": 4,  "type_name": "INTEGER",   "size": 10,  "digits": 0,  "nullable": False, "key": True,  "desc": "Journal Entry ID"},
        {"name": "JOURNAL_DATE", "type_code": 91, "type_name": "DATE",      "size": None, "digits": None, "nullable": True,  "key": False, "desc": "Journal Date"},
        {"name": "PERIOD",       "type_code": 12, "type_name": "VARCHAR",   "size": 10,  "digits": None, "nullable": True,  "key": False, "desc": "Accounting Period"},
        {"name": "STATUS",       "type_code": 12, "type_name": "VARCHAR",   "size": 20,  "digits": None, "nullable": True,  "key": False, "desc": "Post Status"},
        {"name": "CURRENCY",     "type_code": 12, "type_name": "VARCHAR",   "size": 3,   "digits": None, "nullable": True,  "key": False, "desc": "Currency Code"},
        {"name": "CREATED_BY",   "type_code": 12, "type_name": "VARCHAR",   "size": 30,  "digits": None, "nullable": True,  "key": False, "desc": "Created By User"},
    ],
    "HR_EMPLOYEE": [
        {"name": "EMPLOYEE_ID",  "type_code": 4,  "type_name": "INTEGER",   "size": 10,  "digits": 0,  "nullable": False, "key": True,  "desc": "Employee ID"},
        {"name": "EMP_NUMBER",   "type_code": 12, "type_name": "VARCHAR",   "size": 30,  "digits": None, "nullable": False, "key": False, "desc": "Employee Number"},
        {"name": "HIRE_DATE",    "type_code": 91, "type_name": "DATE",      "size": None, "digits": None, "nullable": True,  "key": False, "desc": "Hire Date"},
        {"name": "DEPARTMENT",   "type_code": 12, "type_name": "VARCHAR",   "size": 50,  "digits": None, "nullable": True,  "key": False, "desc": "Department"},
        {"name": "STATUS",       "type_code": 12, "type_name": "VARCHAR",   "size": 10,  "digits": None, "nullable": True,  "key": False, "desc": "Employment Status"},
    ],
    "SALES_ORDER_HEADER": [
        {"name": "ORDER_ID",     "type_code": 4,  "type_name": "INTEGER",   "size": 10,  "digits": 0,  "nullable": False, "key": True,  "desc": "Order ID"},
        {"name": "ORDER_NUMBER", "type_code": 12, "type_name": "VARCHAR",   "size": 20,  "digits": None, "nullable": False, "key": False, "desc": "Order Number"},
        {"name": "CUSTOMER_ID",  "type_code": 4,  "type_name": "INTEGER",   "size": 10,  "digits": 0,  "nullable": True,  "key": False, "desc": "Customer ID"},
        {"name": "ORDER_DATE",   "type_code": 91, "type_name": "DATE",      "size": None, "digits": None, "nullable": True,  "key": False, "desc": "Order Date"},
        {"name": "TOTAL_AMOUNT", "type_code": 3,  "type_name": "DECIMAL",   "size": 15,  "digits": 2,  "nullable": True,  "key": False, "desc": "Total Amount"},
        {"name": "CURRENCY",     "type_code": 12, "type_name": "VARCHAR",   "size": 3,   "digits": None, "nullable": True,  "key": False, "desc": "Currency Code"},
    ],
    "PO_HEADER": [
        {"name": "PO_ID",       "type_code": 4,  "type_name": "INTEGER",   "size": 10,  "digits": 0,  "nullable": False, "key": True,  "desc": "PO ID"},
        {"name": "PO_NUMBER",   "type_code": 12, "type_name": "VARCHAR",   "size": 20,  "digits": None, "nullable": False, "key": False, "desc": "PO Number"},
        {"name": "VENDOR_ID",   "type_code": 4,  "type_name": "INTEGER",   "size": 10,  "digits": 0,  "nullable": True,  "key": False, "desc": "Vendor ID"},
        {"name": "PO_DATE",     "type_code": 91, "type_name": "DATE",      "size": None, "digits": None, "nullable": True,  "key": False, "desc": "PO Date"},
        {"name": "STATUS",      "type_code": 12, "type_name": "VARCHAR",   "size": 20,  "digits": None, "nullable": True,  "key": False, "desc": "PO Status"},
    ],
}

_GENERIC_RELATIONSHIPS: list[dict[str, str]] = [
    {"constraint": "GL_LINE_HEADER_FK",   "source_table": "GL_JOURNAL_LINE",   "source_column": "JOURNAL_ID",  "target_table": "GL_JOURNAL_HEADER",  "target_column": "JOURNAL_ID",  "type": "MANY_TO_ONE"},
    {"constraint": "SO_LINE_HEADER_FK",   "source_table": "SALES_ORDER_LINE",  "source_column": "ORDER_ID",    "target_table": "SALES_ORDER_HEADER", "target_column": "ORDER_ID",    "type": "MANY_TO_ONE"},
    {"constraint": "PO_LINE_HEADER_FK",   "source_table": "PO_LINE",           "source_column": "PO_ID",       "target_table": "PO_HEADER",          "target_column": "PO_ID",       "type": "MANY_TO_ONE"},
]


# ---------------------------------------------------------------------------
# JDBCConnector
# ---------------------------------------------------------------------------


class JDBCConnector(BaseConnector):
    """Generic JDBC schema discovery connector for legacy ERP systems.

    Uses the standard ``java.sql.DatabaseMetaData`` API via ``jaydebeapi``
    to discover table, column, and relationship metadata from any
    JDBC-compliant database.

    **Privacy (C-001):**
        Only ``DatabaseMetaData`` API calls are used.  No ``SELECT``
        statements are ever executed against data tables.

    **Supported drivers:**
        PostgreSQL (``org.postgresql.Driver``), MySQL (``com.mysql.cj.jdbc.Driver``),
        DB2 (``com.ibm.db2.jcc.DB2Driver``), Informix, and any standard
        JDBC-compliant driver.

    Args:
        config: A :class:`ConnectionConfig` with ``erp_type="jdbc_legacy"``
            and JDBC connection details.
    """

    def __init__(self, config: ConnectionConfig) -> None:
        """Initialise the generic JDBC connector.

        Args:
            config: Validated connection configuration.
        """
        super().__init__(config)
        self._logger = get_logger(__name__)

        self._jdbc_url: str = config.jdbc_url or ""
        self._driver_class: str = config.jdbc_driver_class or ""
        self._driver_path: str = config.jdbc_driver_path or ""
        self._username: str = config.username or ""
        self._password: str = config.password or ""
        self._schema_filter: str | None = config.database

        # JDBC connection handle — ``None`` until ``connect()`` succeeds.
        self._connection: Any | None = None
        self._use_live_jdbc: bool = False

        self._logger.info(
            "jdbc_connector_initialised",
            jdbc_url=self._jdbc_url,
            driver_class=self._driver_class,
        )

    # -- BaseConnector Interface ------------------------------------------

    @circuit_breaker_decorator(name="jdbc_generic_connect", failure_threshold=5, recovery_timeout=30)
    def connect(self) -> None:
        """Establish a generic JDBC connection.

        Falls back to offline mode when ``jaydebeapi`` is not installed.

        Raises:
            ConnectionError: If the connection cannot be established.
        """
        try:
            try:
                import jaydebeapi  # type: ignore[import-untyped]  # noqa: PLC0415

                driver_args: list[str] = [self._username, self._password]
                jar_path = self._driver_path if self._driver_path else None
                self._connection = jaydebeapi.connect(
                    self._driver_class,
                    self._jdbc_url,
                    driver_args,
                    jar_path,
                )
                self._use_live_jdbc = True
                self._logger.info(
                    "jdbc_connected",
                    jdbc_url=self._jdbc_url,
                    driver_class=self._driver_class,
                    mode="live_jdbc",
                )
            except ImportError:
                self._use_live_jdbc = False
                self._logger.info(
                    "jdbc_connector_offline_mode",
                    reason="jaydebeapi not installed",
                )

            self._connected = True
        except ConnectorError:
            raise
        except Exception as exc:
            self._logger.error(
                "jdbc_connection_failed",
                jdbc_url=self._jdbc_url,
                error=str(exc),
                error_type=type(exc).__name__,
            )
            raise ConnectionError(
                message=f"Failed to connect via JDBC to {self._jdbc_url}",
                erp_type=self._config.erp_type,
                original_error=exc,
            ) from exc

    def discover_tables(
        self,
        schema_name: str | None = None,
        module: ERPModule | None = None,
    ) -> list[TableMetadata]:
        """Discover tables from a generic JDBC source.

        **Privacy (C-001):** Uses ``DatabaseMetaData.getTables()`` only.
        Estimated row counts are obtained from database statistics
        (metadata-safe), never via ``SELECT COUNT(*)``.

        For live JDBC connections the discovery call is wrapped with
        :meth:`~BaseConnector._retry_with_backoff` to tolerate transient
        JDBC errors (network blips, busy metadata locks, etc.).

        Args:
            schema_name: Optional schema/catalog filter.
            module: Optional ERP module filter (prefix-based heuristic).

        Returns:
            List of :class:`TableMetadata`.

        Raises:
            DiscoveryError: If metadata extraction fails.
            ConnectionError: If not connected.
        """
        self._validate_connected()

        self._logger.debug(
            "jdbc_discover_tables_start",
            connected=self.connected,
            schema_name=schema_name,
            module=module.value if module else None,
        )

        try:
            effective_schema = schema_name or self._schema_filter

            if self._use_live_jdbc and self._connection is not None:
                return self._retry_with_backoff(
                    self._discover_tables_live,
                    effective_schema,
                    module,
                    operation_name="discover_tables",
                )

            return self._discover_tables_offline(effective_schema, module)

        except ConnectorError:
            raise
        except Exception as exc:
            self._logger.error(
                "jdbc_discover_tables_failed",
                error=str(exc),
                error_type=type(exc).__name__,
            )
            raise DiscoveryError(
                message=f"Failed to discover JDBC tables: {exc}",
                erp_type=self._config.erp_type,
                original_error=exc,
            ) from exc

    def discover_columns(
        self,
        table_name: str,
        schema_name: str | None = None,
    ) -> list[ColumnMetadata]:
        """Discover columns via JDBC ``DatabaseMetaData.getColumns()``.

        **Privacy (C-001):** Only column metadata is queried.  Primary-key
        identification is performed via ``getPrimaryKeys()`` — no data
        rows are inspected.

        For live JDBC connections the discovery call is wrapped with
        :meth:`~BaseConnector._retry_with_backoff` to tolerate transient
        JDBC errors.

        Args:
            table_name: Table name.
            schema_name: Optional schema qualifier.

        Returns:
            List of :class:`ColumnMetadata`, ordered by ordinal position.

        Raises:
            DiscoveryError: If column metadata cannot be extracted.
            ConnectionError: If not connected.
        """
        self._validate_connected()

        self._logger.debug(
            "jdbc_discover_columns_start",
            table_name=table_name,
            connected=self.connected,
        )

        try:
            if self._use_live_jdbc and self._connection is not None:
                return self._retry_with_backoff(
                    self._discover_columns_live,
                    table_name,
                    schema_name,
                    operation_name="discover_columns",
                )

            return self._discover_columns_offline(table_name)

        except ConnectorError:
            raise
        except Exception as exc:
            self._logger.error(
                "jdbc_discover_columns_failed",
                table_name=table_name,
                error=str(exc),
                error_type=type(exc).__name__,
            )
            raise DiscoveryError(
                message=f"Failed to discover JDBC columns for {table_name}: {exc}",
                erp_type=self._config.erp_type,
                original_error=exc,
            ) from exc

    def discover_relationships(
        self,
        schema_name: str | None = None,
    ) -> list[RelationshipMetadata]:
        """Discover FK relationships via JDBC ``DatabaseMetaData``.

        Uses both ``getImportedKeys()`` (outgoing foreign keys from each
        table) and ``getExportedKeys()`` (incoming foreign keys into each
        table) to build a comprehensive relationship map.

        **Privacy (C-001):** Only constraint metadata is accessed — no
        data rows are followed or inspected.

        For live JDBC connections the discovery call is wrapped with
        :meth:`~BaseConnector._retry_with_backoff` to tolerate transient
        JDBC errors.

        Args:
            schema_name: Optional schema filter.

        Returns:
            List of :class:`RelationshipMetadata`.

        Raises:
            DiscoveryError: If relationship discovery fails.
            ConnectionError: If not connected.
        """
        self._validate_connected()

        self._logger.debug(
            "jdbc_discover_relationships_start",
            connected=self.connected,
            schema_name=schema_name,
        )

        try:
            if self._use_live_jdbc and self._connection is not None:
                return self._retry_with_backoff(
                    self._discover_relationships_live,
                    schema_name,
                    operation_name="discover_relationships",
                )

            return self._discover_relationships_offline()

        except ConnectorError:
            raise
        except Exception as exc:
            self._logger.error(
                "jdbc_discover_relationships_failed",
                error=str(exc),
                error_type=type(exc).__name__,
            )
            raise DiscoveryError(
                message=f"Failed to discover JDBC relationships: {exc}",
                erp_type=self._config.erp_type,
                original_error=exc,
            ) from exc

    def close(self) -> None:
        """Close the JDBC connection and release resources.

        Safe to call multiple times (idempotent).
        """
        if self._connection is not None:
            try:
                self._connection.close()
            except Exception as exc:
                self._logger.warning(
                    "jdbc_close_warning",
                    error=str(exc),
                    error_type=type(exc).__name__,
                )
            finally:
                self._connection = None

        self._connected = False
        self._logger.info("jdbc_connector_closed")

    # -- Live JDBC Helpers --------------------------------------------------

    def _get_database_metadata(self) -> Any:
        """Obtain the ``java.sql.DatabaseMetaData`` object.

        Returns:
            JDBC ``DatabaseMetaData`` proxy via jaydebeapi.

        Raises:
            DiscoveryError: If the metadata handle cannot be retrieved.
        """
        try:
            return self._connection.jconn.getMetaData()
        except Exception as exc:
            raise DiscoveryError(
                message=f"Cannot retrieve JDBC DatabaseMetaData: {exc}",
                erp_type=self._config.erp_type,
                original_error=exc,
            ) from exc

    def _discover_tables_live(
        self,
        schema: str | None,
        module: ERPModule | None,
    ) -> list[TableMetadata]:
        """Extract table list from a live JDBC connection.

        Estimated row counts are obtained from database catalog statistics
        (e.g. ``INFORMATION_SCHEMA.TABLES``) when available.  This is a
        *metadata-safe* operation (C-001) — no ``SELECT COUNT(*)`` on
        business data tables is performed.

        Args:
            schema: Schema filter.
            module: ERP module filter.

        Returns:
            List of :class:`TableMetadata`.
        """
        metadata = self._get_database_metadata()
        rs = metadata.getTables(None, schema, "%", ["TABLE"])

        raw_tables: list[dict[str, Any]] = []
        try:
            while rs.next():
                tbl_catalog = str(rs.getString(1) or "")
                tbl_schema = str(rs.getString(2) or "")
                tbl_name = str(rs.getString(3) or "")
                tbl_type = str(rs.getString(4) or "TABLE")
                remarks = str(rs.getString(5) or "")

                if module is not None and not self._matches_module(tbl_name, module):
                    continue

                raw_tables.append({
                    "catalog": tbl_catalog,
                    "schema": tbl_schema,
                    "name": tbl_name,
                    "type": tbl_type,
                    "remarks": remarks,
                })
        finally:
            rs.close()

        # Attempt to obtain estimated row counts from INFORMATION_SCHEMA.
        # This is metadata-safe (C-001) — it queries catalog statistics,
        # never executes COUNT(*) on business data tables.
        row_count_map: dict[str, int | None] = {}
        row_count_map = self._estimate_row_counts_from_catalog(schema, raw_tables)

        result: list[TableMetadata] = []
        for entry in raw_tables:
            tbl_name = entry["name"]
            tbl_schema = entry["schema"]
            assigned_module = self._classify_table_by_name(tbl_name)
            estimated_count = row_count_map.get(tbl_name)

            result.append(
                TableMetadata(
                    table_name=tbl_name,
                    schema_name=tbl_schema or schema or "",
                    description=entry["remarks"],
                    estimated_row_count=estimated_count,
                    module=assigned_module,
                    table_type=entry["type"],
                )
            )

        self._logger.info(
            "jdbc_tables_discovered_live",
            table_count=len(result),
            schema=schema,
            module=module.value if module else "all",
        )
        return result

    def _discover_columns_live(
        self,
        table_name: str,
        schema: str | None,
    ) -> list[ColumnMetadata]:
        """Extract column metadata from a live JDBC connection.

        Args:
            table_name: Target table.
            schema: Optional schema qualifier.

        Returns:
            List of :class:`ColumnMetadata`.
        """
        metadata = self._get_database_metadata()

        # Determine primary-key columns first.
        pk_columns: set[str] = set()
        pk_rs = metadata.getPrimaryKeys(None, schema, table_name)
        try:
            while pk_rs.next():
                pk_columns.add(str(pk_rs.getString(4) or ""))
        finally:
            pk_rs.close()

        # Discover column metadata.
        col_rs = metadata.getColumns(None, schema, table_name, "%")
        result: list[ColumnMetadata] = []
        try:
            while col_rs.next():
                col_name = str(col_rs.getString(4) or "")
                type_code = int(col_rs.getInt(5))
                type_name = str(col_rs.getString(6) or "")
                col_size = col_rs.getInt(7)
                dec_digits = col_rs.getInt(9)
                nullable_code = int(col_rs.getInt(11))
                remarks = str(col_rs.getString(12) or "")
                ordinal = int(col_rs.getInt(17))
                auto_inc = str(col_rs.getString(23) or "").upper() == "YES"

                standard_type = self._map_jdbc_type(type_code, type_name, col_size, dec_digits)
                is_char_type = standard_type in {"VARCHAR", "TEXT"}

                result.append(
                    ColumnMetadata(
                        column_name=col_name,
                        native_type=type_name,
                        standard_type=standard_type,
                        max_length=col_size if is_char_type and col_size > 0 else None,
                        precision=col_size if not is_char_type and col_size > 0 else None,
                        scale=dec_digits if dec_digits and dec_digits > 0 else None,
                        is_nullable=(nullable_code != 0),
                        is_primary_key=(col_name in pk_columns),
                        is_auto_increment=auto_inc,
                        ordinal_position=ordinal,
                        description=remarks,
                    )
                )
        finally:
            col_rs.close()

        self._logger.info(
            "jdbc_columns_discovered_live",
            table_name=table_name,
            column_count=len(result),
        )
        return result

    def _discover_relationships_live(
        self,
        schema: str | None,
    ) -> list[RelationshipMetadata]:
        """Extract FK relationships from a live JDBC connection.

        Uses both ``getImportedKeys()`` (outgoing foreign keys from each
        table) and ``getExportedKeys()`` (incoming foreign keys referencing
        each table) to build a comprehensive relationship map.  Duplicate
        constraints are deduplicated by constraint name.

        **Privacy (C-001):** Only constraint catalogue metadata is read.

        Args:
            schema: Optional schema filter.

        Returns:
            List of :class:`RelationshipMetadata`.
        """
        metadata = self._get_database_metadata()

        # Discover all tables first.
        tables_rs = metadata.getTables(None, schema, "%", ["TABLE"])
        table_names: list[str] = []
        try:
            while tables_rs.next():
                table_names.append(str(tables_rs.getString(3) or ""))
        finally:
            tables_rs.close()

        seen_constraints: set[str] = set()
        result: list[RelationshipMetadata] = []

        for tbl in table_names:
            # --- Imported keys (outgoing FKs FROM this table) ---
            try:
                fk_rs = metadata.getImportedKeys(None, schema, tbl)
                try:
                    while fk_rs.next():
                        pk_table = str(fk_rs.getString(3) or "")
                        pk_col = str(fk_rs.getString(4) or "")
                        fk_table = str(fk_rs.getString(7) or "")
                        fk_col = str(fk_rs.getString(8) or "")
                        fk_name = str(fk_rs.getString(12) or f"{fk_table}_{pk_table}_FK")

                        if fk_name in seen_constraints:
                            continue
                        seen_constraints.add(fk_name)

                        # Determine referential actions when available.
                        on_update: str | None = self._map_referential_action(
                            fk_rs.getInt(10)  # UPDATE_RULE
                        )
                        on_delete: str | None = self._map_referential_action(
                            fk_rs.getInt(11)  # DELETE_RULE
                        )

                        result.append(
                            RelationshipMetadata(
                                constraint_name=fk_name,
                                source_table=fk_table,
                                source_column=fk_col,
                                target_table=pk_table,
                                target_column=pk_col,
                                relationship_type="MANY_TO_ONE",
                                on_update=on_update,
                                on_delete=on_delete,
                            )
                        )
                finally:
                    fk_rs.close()
            except Exception as inner_exc:
                self._logger.debug(
                    "jdbc_imported_keys_skip",
                    table=tbl,
                    error=str(inner_exc),
                )

            # --- Exported keys (incoming FKs INTO this table) ---
            try:
                ek_rs = metadata.getExportedKeys(None, schema, tbl)
                try:
                    while ek_rs.next():
                        pk_table = str(ek_rs.getString(3) or "")
                        pk_col = str(ek_rs.getString(4) or "")
                        fk_table = str(ek_rs.getString(7) or "")
                        fk_col = str(ek_rs.getString(8) or "")
                        fk_name = str(ek_rs.getString(12) or f"{fk_table}_{pk_table}_FK")

                        # Skip if already recorded from the imported-keys pass.
                        if fk_name in seen_constraints:
                            continue
                        seen_constraints.add(fk_name)

                        on_update_ek: str | None = self._map_referential_action(
                            ek_rs.getInt(10)
                        )
                        on_delete_ek: str | None = self._map_referential_action(
                            ek_rs.getInt(11)
                        )

                        result.append(
                            RelationshipMetadata(
                                constraint_name=fk_name,
                                source_table=fk_table,
                                source_column=fk_col,
                                target_table=pk_table,
                                target_column=pk_col,
                                relationship_type="MANY_TO_ONE",
                                on_update=on_update_ek,
                                on_delete=on_delete_ek,
                            )
                        )
                finally:
                    ek_rs.close()
            except Exception as inner_exc:
                self._logger.debug(
                    "jdbc_exported_keys_skip",
                    table=tbl,
                    error=str(inner_exc),
                )

        self._logger.info(
            "jdbc_relationships_discovered_live",
            relationship_count=len(result),
            tables_inspected=len(table_names),
        )
        return result

    # -- Offline Helpers ----------------------------------------------------

    def _discover_tables_offline(
        self,
        schema: str | None,
        module: ERPModule | None,
    ) -> list[TableMetadata]:
        """Return offline generic tables.

        Args:
            schema: Schema override.
            module: Module filter.

        Returns:
            List of :class:`TableMetadata`.
        """
        result: list[TableMetadata] = []
        for entry in _GENERIC_DD_TABLES:
            if module is not None:
                entry_module = entry.get("module")
                module_val = module.value if isinstance(module, ERPModule) else str(module)
                if entry_module is None or (
                    isinstance(entry_module, ERPModule) and entry_module.value != module_val
                ) or (
                    isinstance(entry_module, str) and entry_module != module_val
                ):
                    continue

            result.append(
                TableMetadata(
                    table_name=entry["name"],
                    schema_name=schema or entry.get("schema", "ERP"),
                    description=entry.get("desc", ""),
                    estimated_row_count=None,
                    module=entry.get("module"),
                    table_type="TABLE",
                )
            )

        self._logger.info(
            "jdbc_tables_discovered_offline",
            table_count=len(result),
            module=module.value if module else "all",
        )
        return result

    def _discover_columns_offline(self, table_name: str) -> list[ColumnMetadata]:
        """Return offline generic columns for a table.

        Args:
            table_name: Target table name.

        Returns:
            List of :class:`ColumnMetadata`.
        """
        columns_data = _GENERIC_DD_COLUMNS.get(table_name)
        if columns_data is None:
            self._logger.debug("jdbc_columns_not_cached_offline", table_name=table_name)
            return self._build_generic_columns(table_name)

        result: list[ColumnMetadata] = []
        for idx, col in enumerate(columns_data, start=1):
            standard_type = self._map_jdbc_type(
                col["type_code"],
                col["type_name"],
                col.get("size") or 0,
                col.get("digits") or 0,
            )
            is_char = standard_type in {"VARCHAR", "TEXT"}
            result.append(
                ColumnMetadata(
                    column_name=col["name"],
                    native_type=col["type_name"],
                    standard_type=standard_type,
                    max_length=col.get("size") if is_char and col.get("size") else None,
                    precision=col.get("size") if not is_char and col.get("size") else None,
                    scale=col.get("digits") if col.get("digits") and col["digits"] > 0 else None,
                    is_nullable=col["nullable"],
                    is_primary_key=col["key"],
                    is_auto_increment=False,
                    ordinal_position=idx,
                    description=col.get("desc"),
                )
            )

        self._logger.info(
            "jdbc_columns_discovered_offline",
            table_name=table_name,
            column_count=len(result),
        )
        return result

    def _discover_relationships_offline(self) -> list[RelationshipMetadata]:
        """Return offline generic relationships.

        Returns:
            List of :class:`RelationshipMetadata`.
        """
        result: list[RelationshipMetadata] = []
        for rel in _GENERIC_RELATIONSHIPS:
            result.append(
                RelationshipMetadata(
                    constraint_name=rel["constraint"],
                    source_table=rel["source_table"],
                    source_column=rel["source_column"],
                    target_table=rel["target_table"],
                    target_column=rel["target_column"],
                    relationship_type=rel["type"],
                )
            )

        self._logger.info(
            "jdbc_relationships_discovered_offline",
            relationship_count=len(result),
        )
        return result

    # -- Shared Private Helpers ---------------------------------------------

    def _estimate_row_counts_from_catalog(
        self,
        schema: str | None,
        raw_tables: list[dict[str, Any]],
    ) -> dict[str, int | None]:
        """Attempt to obtain estimated row counts from database catalog statistics.

        Tries the JDBC standard ``INFORMATION_SCHEMA.TABLES`` approach first.
        On failure (unsupported, permissions), returns empty estimates.

        **Privacy (C-001):** This method queries *catalog statistics only*
        — it never executes ``SELECT COUNT(*)`` or scans data tables.

        Args:
            schema: Optional schema filter.
            raw_tables: The list of table dicts already discovered.

        Returns:
            Mapping of table_name → estimated_row_count (``None`` when the
            estimate is unavailable).
        """
        counts: dict[str, int | None] = {}

        if self._connection is None:
            return counts

        for entry in raw_tables:
            counts[entry["name"]] = None

        try:
            # Many JDBC-compliant databases expose row estimates through
            # the INFORMATION_SCHEMA.TABLES view (TABLE_ROWS column).
            # This is a catalogue query and does NOT scan business data.
            cursor = self._connection.cursor()
            try:
                if schema:
                    cursor.execute(
                        "SELECT TABLE_NAME, TABLE_ROWS FROM INFORMATION_SCHEMA.TABLES "
                        "WHERE TABLE_SCHEMA = ? AND TABLE_TYPE = 'BASE TABLE'",
                        [schema],
                    )
                else:
                    cursor.execute(
                        "SELECT TABLE_NAME, TABLE_ROWS FROM INFORMATION_SCHEMA.TABLES "
                        "WHERE TABLE_TYPE = 'BASE TABLE'"
                    )

                for row in cursor.fetchall():
                    tbl_name = str(row[0])
                    try:
                        row_count = int(row[1]) if row[1] is not None else None
                    except (ValueError, TypeError):
                        row_count = None
                    if tbl_name in counts:
                        counts[tbl_name] = row_count
            finally:
                cursor.close()

            self._logger.debug(
                "jdbc_row_count_estimates_obtained",
                schema=schema,
                estimated_tables=sum(1 for v in counts.values() if v is not None),
            )
        except Exception as exc:
            # Graceful degradation: row count estimation is best-effort.
            # Not all JDBC drivers / databases support INFORMATION_SCHEMA.
            self._logger.debug(
                "jdbc_row_count_estimation_unavailable",
                error=str(exc),
                error_type=type(exc).__name__,
            )

        return counts

    @staticmethod
    def _map_referential_action(action_code: int) -> str | None:
        """Map a JDBC referential action code to a human-readable string.

        The codes are defined in ``java.sql.DatabaseMetaData`` for the
        ``UPDATE_RULE`` and ``DELETE_RULE`` columns of
        ``getImportedKeys()`` / ``getExportedKeys()``.

        Args:
            action_code: JDBC referential action integer:
                0 = CASCADE, 1 = RESTRICT, 2 = SET NULL,
                3 = NO ACTION, 4 = SET DEFAULT.

        Returns:
            Human-readable action string, or ``None`` when the code is
            unrecognised.
        """
        action_map: dict[int, str] = {
            0: "CASCADE",
            1: "RESTRICT",
            2: "SET NULL",
            3: "NO ACTION",
            4: "SET DEFAULT",
        }
        return action_map.get(action_code)

    def _map_jdbc_type(
        self,
        jdbc_type_code: int,
        type_name: str,
        column_size: int,
        decimal_digits: int,
    ) -> str:
        """Map a JDBC type code and name to a standard type string.

        Uses the numeric type code first; falls back to the type name
        string when the code is not recognised.

        Args:
            jdbc_type_code: ``java.sql.Types`` integer code.
            type_name: Human-readable type name from the driver.
            column_size: Column size / precision.
            decimal_digits: Decimal digits / scale.

        Returns:
            Normalised standard type string.
        """
        standard = JDBC_TYPE_MAPPING.get(jdbc_type_code)
        if standard is not None:
            # Refine DECIMAL → INTEGER when scale is zero and size is small.
            if standard == "DECIMAL" and decimal_digits == 0 and column_size <= 18:
                return "INTEGER" if column_size <= 10 else "BIGINT"
            return standard

        # Fallback: try the type name.
        upper_name = type_name.upper().split("(")[0].strip()
        return JDBC_TYPE_NAME_MAPPING.get(upper_name, "VARCHAR")

    def _matches_module(self, table_name: str, module: ERPModule) -> bool:
        """Check if a table name matches a module by prefix heuristic.

        Args:
            table_name: Physical table name.
            module: Target ERP module.

        Returns:
            ``True`` if the table name starts with any of the module's
            known prefixes.
        """
        module_key = module.value if isinstance(module, ERPModule) else str(module)
        prefixes = LEGACY_MODULE_PREFIXES.get(module_key, [])
        upper_name = table_name.upper()
        return any(upper_name.startswith(pfx) for pfx in prefixes)

    def _classify_table_by_name(self, table_name: str) -> ERPModule | None:
        """Classify a table into an ERP module by name prefix.

        Args:
            table_name: Physical table name.

        Returns:
            Matching :class:`ERPModule` or ``None``.
        """
        upper_name = table_name.upper()
        for module_key, prefixes in LEGACY_MODULE_PREFIXES.items():
            if any(upper_name.startswith(pfx) for pfx in prefixes):
                return ERPModule(module_key)
        return None

    def _build_generic_columns(self, _table_name: str) -> list[ColumnMetadata]:
        """Return a minimal generic column set for uncached tables.

        Args:
            table_name: Table name.

        Returns:
            A list containing only an ``ID`` key column.
        """
        return [
            ColumnMetadata(
                column_name="ID",
                native_type="INTEGER",
                standard_type="INTEGER",
                is_nullable=False,
                is_primary_key=True,
                ordinal_position=1,
                description="Primary Key",
            ),
        ]
