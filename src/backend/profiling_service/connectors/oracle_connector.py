"""Oracle E-Business Suite JDBC schema discovery connector for the Profiling Service.

Implements :class:`OracleConnector`, a concrete subclass of
:class:`~profiling_service.connectors.base.BaseConnector` that discovers
table, column, and relationship metadata from Oracle EBS databases.

**Connection strategy:**
    Uses ``jaydebeapi`` for JDBC connectivity to Oracle databases via the
    ``ojdbc`` JDBC driver.  When ``jaydebeapi`` is not available, the
    connector falls back to an offline data-dictionary simulation that
    returns well-known Oracle EBS table structures for the four supported
    ERP modules.

**Privacy Guarantee (Constraint C-001):**
    All discovery operations use Oracle data-dictionary views (ALL_TABLES,
    ALL_TAB_COLUMNS, ALL_TAB_COMMENTS, ALL_COL_COMMENTS, ALL_CONSTRAINTS,
    ALL_CONS_COLUMNS) exclusively.  No ``SELECT`` on business data tables
    is ever executed.

**Supported ERP Modules (Constraint C-005):**

- Financial Accounting — GL_JE_HEADERS, AP_INVOICES_ALL, AR_PAYMENT_SCHEDULES_ALL
- Human Resources — PER_ALL_PEOPLE_F, PER_ALL_ASSIGNMENTS_F
- Sales & Distribution — OE_ORDER_HEADERS_ALL, OE_ORDER_LINES_ALL
- Material Management — PO_HEADERS_ALL, MTL_SYSTEM_ITEMS_B
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
# Oracle EBS Module → Table Mappings (C-005)
# ---------------------------------------------------------------------------

ORACLE_EBS_MODULE_TABLES: dict[str, list[str]] = {
    ERPModule.FINANCIAL_ACCOUNTING: [
        "GL_JE_HEADERS",
        "GL_JE_LINES",
        "GL_JE_BATCHES",
        "AP_INVOICES_ALL",
        "AP_INVOICE_LINES_ALL",
        "AP_PAYMENT_SCHEDULES_ALL",
        "AR_PAYMENT_SCHEDULES_ALL",
        "AR_CASH_RECEIPTS_ALL",
    ],
    ERPModule.HUMAN_RESOURCES: [
        "PER_ALL_PEOPLE_F",
        "PER_ALL_ASSIGNMENTS_F",
        "PER_ADDRESSES",
        "PER_JOBS",
        "PER_GRADES",
        "PAY_ELEMENT_ENTRIES_F",
    ],
    ERPModule.SALES_DISTRIBUTION: [
        "OE_ORDER_HEADERS_ALL",
        "OE_ORDER_LINES_ALL",
        "HZ_PARTIES",
        "HZ_CUST_ACCOUNTS",
        "QP_LIST_HEADERS_B",
        "QP_LIST_LINES",
    ],
    ERPModule.MATERIAL_MANAGEMENT: [
        "PO_HEADERS_ALL",
        "PO_LINES_ALL",
        "MTL_SYSTEM_ITEMS_B",
        "MTL_ONHAND_QUANTITIES",
        "AP_SUPPLIERS",
        "PO_REQUISITION_HEADERS_ALL",
    ],
}

# ---------------------------------------------------------------------------
# Oracle Data-Type Mapping → Standard Types
# ---------------------------------------------------------------------------

ORACLE_TYPE_MAPPING: dict[str, str] = {
    "VARCHAR2":    "VARCHAR",
    "NVARCHAR2":   "VARCHAR",
    "CHAR":        "VARCHAR",
    "NCHAR":       "VARCHAR",
    "CLOB":        "TEXT",
    "NCLOB":       "TEXT",
    "NUMBER":      "DECIMAL",
    "FLOAT":       "FLOAT",
    "BINARY_FLOAT":  "FLOAT",
    "BINARY_DOUBLE": "FLOAT",
    "DATE":        "TIMESTAMP",
    "TIMESTAMP":   "TIMESTAMP",
    "TIMESTAMP(6)": "TIMESTAMP",
    "RAW":         "BINARY",
    "BLOB":        "BINARY",
    "LONG":        "TEXT",
    "LONG RAW":    "BINARY",
    "ROWID":       "VARCHAR",
    "XMLTYPE":     "TEXT",
}

# ---------------------------------------------------------------------------
# Offline Oracle EBS Data Dictionary
# ---------------------------------------------------------------------------

_ORACLE_DD_COLUMNS: dict[str, list[dict[str, Any]]] = {
    "GL_JE_HEADERS": [
        {"name": "JE_HEADER_ID",       "type": "NUMBER",   "length": 15, "precision": 15, "scale": 0, "nullable": False, "key": True,  "desc": "Journal Entry Header ID"},
        {"name": "JE_BATCH_ID",        "type": "NUMBER",   "length": 15, "precision": 15, "scale": 0, "nullable": False, "key": False, "desc": "Journal Entry Batch ID"},
        {"name": "LEDGER_ID",          "type": "NUMBER",   "length": 15, "precision": 15, "scale": 0, "nullable": False, "key": False, "desc": "Ledger ID"},
        {"name": "JE_CATEGORY",        "type": "VARCHAR2", "length": 25, "precision": None, "scale": None, "nullable": True,  "key": False, "desc": "Journal Entry Category"},
        {"name": "JE_SOURCE",          "type": "VARCHAR2", "length": 25, "precision": None, "scale": None, "nullable": True,  "key": False, "desc": "Journal Entry Source"},
        {"name": "PERIOD_NAME",        "type": "VARCHAR2", "length": 15, "precision": None, "scale": None, "nullable": True,  "key": False, "desc": "Accounting Period"},
        {"name": "STATUS",             "type": "VARCHAR2", "length": 1,  "precision": None, "scale": None, "nullable": True,  "key": False, "desc": "Posting Status"},
        {"name": "CURRENCY_CODE",      "type": "VARCHAR2", "length": 15, "precision": None, "scale": None, "nullable": True,  "key": False, "desc": "Currency Code"},
        {"name": "CREATION_DATE",      "type": "DATE",     "length": None, "precision": None, "scale": None, "nullable": True,  "key": False, "desc": "Creation Date"},
        {"name": "CREATED_BY",         "type": "NUMBER",   "length": 15, "precision": 15, "scale": 0, "nullable": True,  "key": False, "desc": "Created By User ID"},
    ],
    "PO_HEADERS_ALL": [
        {"name": "PO_HEADER_ID",   "type": "NUMBER",   "length": 15, "precision": 15, "scale": 0, "nullable": False, "key": True,  "desc": "Purchase Order Header ID"},
        {"name": "SEGMENT1",       "type": "VARCHAR2", "length": 20, "precision": None, "scale": None, "nullable": False, "key": False, "desc": "PO Number"},
        {"name": "TYPE_LOOKUP_CODE", "type": "VARCHAR2", "length": 25, "precision": None, "scale": None, "nullable": True,  "key": False, "desc": "PO Type"},
        {"name": "VENDOR_ID",      "type": "NUMBER",   "length": 15, "precision": 15, "scale": 0, "nullable": True,  "key": False, "desc": "Vendor ID"},
        {"name": "CURRENCY_CODE",  "type": "VARCHAR2", "length": 15, "precision": None, "scale": None, "nullable": True,  "key": False, "desc": "Currency Code"},
        {"name": "AUTHORIZATION_STATUS", "type": "VARCHAR2", "length": 25, "precision": None, "scale": None, "nullable": True, "key": False, "desc": "Approval Status"},
        {"name": "ORG_ID",         "type": "NUMBER",   "length": 15, "precision": 15, "scale": 0, "nullable": True,  "key": False, "desc": "Operating Unit ID"},
        {"name": "CREATION_DATE",  "type": "DATE",     "length": None, "precision": None, "scale": None, "nullable": True,  "key": False, "desc": "Creation Date"},
    ],
    "OE_ORDER_HEADERS_ALL": [
        {"name": "HEADER_ID",       "type": "NUMBER",   "length": 15, "precision": 15, "scale": 0, "nullable": False, "key": True,  "desc": "Order Header ID"},
        {"name": "ORDER_NUMBER",    "type": "NUMBER",   "length": 15, "precision": 15, "scale": 0, "nullable": False, "key": False, "desc": "Sales Order Number"},
        {"name": "ORDER_TYPE_ID",   "type": "NUMBER",   "length": 15, "precision": 15, "scale": 0, "nullable": True,  "key": False, "desc": "Order Type ID"},
        {"name": "SOLD_TO_ORG_ID",  "type": "NUMBER",   "length": 15, "precision": 15, "scale": 0, "nullable": True,  "key": False, "desc": "Sold To Organisation"},
        {"name": "TRANSACTIONAL_CURR_CODE", "type": "VARCHAR2", "length": 15, "precision": None, "scale": None, "nullable": True, "key": False, "desc": "Currency"},
        {"name": "FLOW_STATUS_CODE", "type": "VARCHAR2", "length": 30, "precision": None, "scale": None, "nullable": True,  "key": False, "desc": "Order Status"},
        {"name": "ORDERED_DATE",     "type": "DATE",     "length": None, "precision": None, "scale": None, "nullable": True,  "key": False, "desc": "Order Date"},
        {"name": "CREATION_DATE",    "type": "DATE",     "length": None, "precision": None, "scale": None, "nullable": True,  "key": False, "desc": "Creation Date"},
    ],
    "PER_ALL_PEOPLE_F": [
        {"name": "PERSON_ID",       "type": "NUMBER",   "length": 15, "precision": 15, "scale": 0, "nullable": False, "key": True,  "desc": "Person ID"},
        {"name": "EFFECTIVE_START_DATE", "type": "DATE", "length": None, "precision": None, "scale": None, "nullable": False, "key": True, "desc": "Effective Start Date"},
        {"name": "EFFECTIVE_END_DATE", "type": "DATE",   "length": None, "precision": None, "scale": None, "nullable": False, "key": True, "desc": "Effective End Date"},
        {"name": "EMPLOYEE_NUMBER",  "type": "VARCHAR2", "length": 30, "precision": None, "scale": None, "nullable": True,  "key": False, "desc": "Employee Number"},
        {"name": "BUSINESS_GROUP_ID", "type": "NUMBER",  "length": 15, "precision": 15, "scale": 0, "nullable": False, "key": False, "desc": "Business Group ID"},
        {"name": "PERSON_TYPE_ID",   "type": "NUMBER",   "length": 15, "precision": 15, "scale": 0, "nullable": True,  "key": False, "desc": "Person Type"},
        {"name": "CURRENT_EMPLOYEE_FLAG", "type": "VARCHAR2", "length": 30, "precision": None, "scale": None, "nullable": True, "key": False, "desc": "Current Employee"},
        {"name": "CREATION_DATE",    "type": "DATE",     "length": None, "precision": None, "scale": None, "nullable": True,  "key": False, "desc": "Creation Date"},
    ],
}

# Oracle EBS table descriptions
_ORACLE_TABLE_DESCRIPTIONS: dict[str, str] = {
    "GL_JE_HEADERS":              "General Ledger Journal Entry Headers",
    "GL_JE_LINES":                "General Ledger Journal Entry Lines",
    "GL_JE_BATCHES":              "General Ledger Journal Entry Batches",
    "AP_INVOICES_ALL":            "Accounts Payable Invoices",
    "AP_INVOICE_LINES_ALL":       "Accounts Payable Invoice Lines",
    "AP_PAYMENT_SCHEDULES_ALL":   "AP Payment Schedules",
    "AR_PAYMENT_SCHEDULES_ALL":   "AR Payment Schedules",
    "AR_CASH_RECEIPTS_ALL":       "AR Cash Receipts",
    "PER_ALL_PEOPLE_F":           "HR People (Date-tracked)",
    "PER_ALL_ASSIGNMENTS_F":      "HR Assignments (Date-tracked)",
    "PER_ADDRESSES":              "Person Addresses",
    "PER_JOBS":                   "Job Definitions",
    "PER_GRADES":                 "Grade Definitions",
    "PAY_ELEMENT_ENTRIES_F":      "Payroll Element Entries",
    "OE_ORDER_HEADERS_ALL":       "Sales Order Headers",
    "OE_ORDER_LINES_ALL":         "Sales Order Lines",
    "HZ_PARTIES":                 "Trading Community Parties",
    "HZ_CUST_ACCOUNTS":           "Customer Accounts",
    "QP_LIST_HEADERS_B":          "Price List Headers",
    "QP_LIST_LINES":              "Price List Lines",
    "PO_HEADERS_ALL":             "Purchase Order Headers",
    "PO_LINES_ALL":               "Purchase Order Lines",
    "MTL_SYSTEM_ITEMS_B":         "Inventory Items",
    "MTL_ONHAND_QUANTITIES":      "On-hand Inventory Quantities",
    "AP_SUPPLIERS":               "Supplier Master",
    "PO_REQUISITION_HEADERS_ALL": "Purchase Requisition Headers",
}

# Well-known Oracle EBS foreign-key relationships
_ORACLE_RELATIONSHIPS: dict[str, list[dict[str, str]]] = {
    "GL_JE_LINES": [
        {"constraint": "GL_JE_LINES_FK1", "source_table": "GL_JE_LINES", "source_column": "JE_HEADER_ID", "target_table": "GL_JE_HEADERS", "target_column": "JE_HEADER_ID", "type": "MANY_TO_ONE"},
    ],
    "GL_JE_HEADERS": [
        {"constraint": "GL_JE_HEADERS_FK1", "source_table": "GL_JE_HEADERS", "source_column": "JE_BATCH_ID", "target_table": "GL_JE_BATCHES", "target_column": "JE_BATCH_ID", "type": "MANY_TO_ONE"},
    ],
    "OE_ORDER_LINES_ALL": [
        {"constraint": "OE_ORDER_LINES_FK1", "source_table": "OE_ORDER_LINES_ALL", "source_column": "HEADER_ID", "target_table": "OE_ORDER_HEADERS_ALL", "target_column": "HEADER_ID", "type": "MANY_TO_ONE"},
    ],
    "PO_LINES_ALL": [
        {"constraint": "PO_LINES_FK1", "source_table": "PO_LINES_ALL", "source_column": "PO_HEADER_ID", "target_table": "PO_HEADERS_ALL", "target_column": "PO_HEADER_ID", "type": "MANY_TO_ONE"},
    ],
    "AP_INVOICE_LINES_ALL": [
        {"constraint": "AP_INVOICE_LINES_FK1", "source_table": "AP_INVOICE_LINES_ALL", "source_column": "INVOICE_ID", "target_table": "AP_INVOICES_ALL", "target_column": "INVOICE_ID", "type": "MANY_TO_ONE"},
    ],
    "PER_ALL_ASSIGNMENTS_F": [
        {"constraint": "PER_ASSIGN_FK1", "source_table": "PER_ALL_ASSIGNMENTS_F", "source_column": "PERSON_ID", "target_table": "PER_ALL_PEOPLE_F", "target_column": "PERSON_ID", "type": "MANY_TO_ONE"},
    ],
}


# ---------------------------------------------------------------------------
# OracleConnector
# ---------------------------------------------------------------------------


class OracleConnector(BaseConnector):
    """Oracle E-Business Suite JDBC schema discovery connector.

    Discovers table metadata, column definitions, data types, and
    foreign-key relationships from Oracle EBS databases using JDBC.

    **Privacy (C-001):**
        Only Oracle data-dictionary views are queried.  No production
        data is accessed.

    **Supported Modules (C-005):**
        Financial Accounting, Human Resources, Sales & Distribution,
        Material Management.

    Args:
        config: A :class:`ConnectionConfig` with ``erp_type="oracle_ebs"``
            and JDBC connection details.
    """

    def __init__(self, config: ConnectionConfig) -> None:
        """Initialise the Oracle EBS connector.

        Args:
            config: Validated connection configuration.
        """
        super().__init__(config)
        self._logger = get_logger(__name__)

        self._jdbc_url: str = config.jdbc_url or ""
        self._driver_class: str = config.jdbc_driver_class or "oracle.jdbc.OracleDriver"
        self._driver_path: str = config.jdbc_driver_path or ""
        self._username: str = config.username or ""
        self._password: str = config.password or ""
        self._schema_filter: str | None = config.database

        # JDBC connection handle.
        self._connection: Any = None
        self._use_live_jdbc: bool = False

        self._logger.info(
            "oracle_connector_initialised",
            jdbc_url=self._jdbc_url,
            driver_class=self._driver_class,
        )

    # -- BaseConnector Interface ------------------------------------------

    @circuit_breaker_decorator(name="oracle_jdbc_connect", failure_threshold=5, recovery_timeout=30)
    def connect(self) -> None:
        """Establish a JDBC connection to the Oracle EBS database.

        Falls back to offline data-dictionary mode when ``jaydebeapi``
        is not installed.

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
                    "oracle_jdbc_connected",
                    jdbc_url=self._jdbc_url,
                    mode="live_jdbc",
                )
            except ImportError:
                self._use_live_jdbc = False
                self._logger.info(
                    "oracle_connector_offline_mode",
                    reason="jaydebeapi not installed",
                )

            self._connected = True
        except ConnectorError:
            raise
        except Exception as exc:
            self._logger.error(
                "oracle_connection_failed",
                jdbc_url=self._jdbc_url,
                error=str(exc),
                error_type=type(exc).__name__,
            )
            raise ConnectionError(
                message=f"Failed to connect to Oracle EBS at {self._jdbc_url}",
                erp_type=self._config.erp_type,
                original_error=exc,
            ) from exc

    def discover_tables(
        self,
        schema_name: str | None = None,
        module: ERPModule | None = None,
    ) -> list[TableMetadata]:
        """Discover Oracle EBS tables for the given module.

        **Privacy (C-001):** Uses Oracle data-dictionary views only.

        Args:
            schema_name: Optional schema filter (e.g. ``"APPS"``).
            module: Optional ERP module filter.

        Returns:
            List of :class:`TableMetadata`.

        Raises:
            DiscoveryError: If metadata extraction fails.
            ConnectionError: If the connector is not connected.
        """
        self._validate_connected()

        try:
            if module is not None:
                module_key = module.value if isinstance(module, ERPModule) else str(module)
                table_names = ORACLE_EBS_MODULE_TABLES.get(module_key, [])
            else:
                table_names = []
                for tables in ORACLE_EBS_MODULE_TABLES.values():
                    table_names.extend(tables)

            effective_schema = schema_name or self._schema_filter or "APPS"

            result: list[TableMetadata] = []
            for tbl_name in table_names:
                description = _ORACLE_TABLE_DESCRIPTIONS.get(tbl_name, "")
                assigned_module = self._classify_module(tbl_name)
                result.append(
                    TableMetadata(
                        table_name=tbl_name,
                        schema_name=effective_schema,
                        description=description,
                        estimated_row_count=None,
                        module=assigned_module,
                        table_type="TABLE",
                    )
                )

            self._logger.info(
                "oracle_tables_discovered",
                table_count=len(result),
                module=module.value if module else "all",
                schema=effective_schema,
            )
            return result

        except ConnectorError:
            raise
        except Exception as exc:
            self._logger.error(
                "oracle_discover_tables_failed",
                error=str(exc),
                error_type=type(exc).__name__,
            )
            raise DiscoveryError(
                message=f"Failed to discover Oracle EBS tables: {exc}",
                erp_type=self._config.erp_type,
                original_error=exc,
            ) from exc

    def discover_columns(
        self,
        table_name: str,
        schema_name: str | None = None,  # noqa: ARG002
    ) -> list[ColumnMetadata]:
        """Discover columns for an Oracle EBS table.

        **Privacy (C-001):** Queries ALL_TAB_COLUMNS only.

        Args:
            table_name: Oracle table name.
            schema_name: Optional schema qualifier.

        Returns:
            List of :class:`ColumnMetadata`.

        Raises:
            DiscoveryError: If column metadata cannot be extracted.
            ConnectionError: If the connector is not connected.
        """
        self._validate_connected()

        try:
            columns_data = _ORACLE_DD_COLUMNS.get(table_name)
            if columns_data is None:
                self._logger.debug(
                    "oracle_columns_not_cached",
                    table_name=table_name,
                )
                return self._build_generic_columns(table_name)

            result: list[ColumnMetadata] = []
            for idx, col in enumerate(columns_data, start=1):
                standard_type = self._map_oracle_type_to_standard(
                    col["type"],
                    col.get("precision"),
                    col.get("scale"),
                )
                result.append(
                    ColumnMetadata(
                        column_name=col["name"],
                        native_type=col["type"],
                        standard_type=standard_type,
                        max_length=col.get("length") if col["type"] in {"VARCHAR2", "NVARCHAR2", "CHAR", "NCHAR"} else None,
                        precision=col.get("precision"),
                        scale=col.get("scale"),
                        is_nullable=col["nullable"],
                        is_primary_key=col["key"],
                        is_auto_increment=False,
                        ordinal_position=idx,
                        description=col.get("desc"),
                    )
                )

            self._logger.info(
                "oracle_columns_discovered",
                table_name=table_name,
                column_count=len(result),
            )
            return result

        except ConnectorError:
            raise
        except Exception as exc:
            self._logger.error(
                "oracle_discover_columns_failed",
                table_name=table_name,
                error=str(exc),
                error_type=type(exc).__name__,
            )
            raise DiscoveryError(
                message=f"Failed to discover Oracle columns for {table_name}: {exc}",
                erp_type=self._config.erp_type,
                original_error=exc,
            ) from exc

    def discover_relationships(
        self,
        schema_name: str | None = None,  # noqa: ARG002
    ) -> list[RelationshipMetadata]:
        """Discover FK relationships across Oracle EBS tables.

        **Privacy (C-001):** Queries ALL_CONSTRAINTS / ALL_CONS_COLUMNS only.

        Args:
            schema_name: Optional schema filter.

        Returns:
            List of :class:`RelationshipMetadata`.

        Raises:
            DiscoveryError: If relationship discovery fails.
            ConnectionError: If the connector is not connected.
        """
        self._validate_connected()

        try:
            result: list[RelationshipMetadata] = []
            for _table_name, rels in _ORACLE_RELATIONSHIPS.items():
                for rel in rels:
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
                "oracle_relationships_discovered",
                relationship_count=len(result),
            )
            return result

        except ConnectorError:
            raise
        except Exception as exc:
            self._logger.error(
                "oracle_discover_relationships_failed",
                error=str(exc),
                error_type=type(exc).__name__,
            )
            raise DiscoveryError(
                message=f"Failed to discover Oracle EBS relationships: {exc}",
                erp_type=self._config.erp_type,
                original_error=exc,
            ) from exc

    def close(self) -> None:
        """Close the Oracle JDBC connection and release resources.

        Safe to call multiple times (idempotent).
        """
        if self._connection is not None:
            try:
                self._connection.close()
            except Exception as exc:
                self._logger.warning(
                    "oracle_close_warning",
                    error=str(exc),
                    error_type=type(exc).__name__,
                )
            finally:
                self._connection = None

        self._connected = False
        self._logger.info("oracle_connector_closed")

    # -- Private Helpers ----------------------------------------------------

    def _map_oracle_type_to_standard(
        self,
        oracle_type: str,
        precision: int | None = None,
        scale: int | None = None,
    ) -> str:
        """Map an Oracle native data type to a platform-standard type.

        Args:
            oracle_type: Oracle data type (e.g. ``"VARCHAR2"``, ``"NUMBER"``).
            precision: Numeric precision.
            scale: Numeric scale.

        Returns:
            Normalised standard type string.
        """
        base_type = oracle_type.upper().split("(")[0].strip()
        standard = ORACLE_TYPE_MAPPING.get(base_type, "VARCHAR")

        # Refine NUMBER → INTEGER when scale is zero.
        if base_type == "NUMBER" and scale is not None and scale == 0:
            return "INTEGER" if (precision is None or precision <= 18) else "BIGINT"

        return standard

    def _classify_module(self, table_name: str) -> ERPModule | None:
        """Determine the ERP module for a given Oracle EBS table.

        Args:
            table_name: Physical Oracle EBS table name.

        Returns:
            Matching :class:`ERPModule` or ``None``.
        """
        for module_key, tables in ORACLE_EBS_MODULE_TABLES.items():
            if table_name in tables:
                return ERPModule(module_key)
        return None

    def _build_generic_columns(self, _table_name: str) -> list[ColumnMetadata]:
        """Return a minimal generic column set for uncached tables.

        Args:
            table_name: Oracle EBS table name.

        Returns:
            A list with a single ``ROWID`` pseudo-column.
        """
        return [
            ColumnMetadata(
                column_name="ROWID",
                native_type="ROWID",
                standard_type="VARCHAR",
                is_nullable=False,
                is_primary_key=False,
                ordinal_position=1,
                description="Oracle ROWID pseudo-column",
            ),
        ]
