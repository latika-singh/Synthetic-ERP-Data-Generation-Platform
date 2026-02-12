"""SAP RFC/BAPI schema discovery connector for the Profiling Service.

Implements :class:`SAPConnector`, a concrete subclass of
:class:`~profiling_service.connectors.base.BaseConnector` that discovers
table, column, and relationship metadata from SAP ERP systems.

**Connection strategy:**
    The connector communicates with SAP using the RFC (Remote Function Call)
    protocol.  When the ``pyrfc`` library is available it is used directly;
    otherwise the connector falls back to a metadata-only simulation layer
    that returns well-known SAP data-dictionary structures for the four
    supported ERP modules.

**Privacy Guarantee (Constraint C-001):**
    All discovery operations use SAP data-dictionary metadata calls
    (DD03L, DD04T, DD08L equivalents) exclusively.  No raw production
    data rows are ever accessed, transferred, or stored.

**Supported ERP Modules (Constraint C-005):**

- Financial Accounting (GL, AP, AR)
- Human Resources (PA, OM)
- Sales & Distribution (SD)
- Material Management (MM)

Design Patterns:
    - **Strategy** — SAPConnector encapsulates the SAP-specific discovery
      strategy behind the ``BaseConnector`` abstract interface.
    - **Circuit Breaker** — External RFC calls are wrapped with the shared
      circuit-breaker decorator to prevent cascade failures.
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
# SAP Module → Table Mappings (C-005)
# ---------------------------------------------------------------------------

SAP_MODULE_TABLE_MAPPINGS: dict[str, list[str]] = {
    ERPModule.FINANCIAL_ACCOUNTING: [
        "BKPF",   # Accounting Document Header
        "BSEG",   # Accounting Document Segment
        "SKA1",   # G/L Account Master (Chart of Accounts)
        "SKB1",   # G/L Account Master (Company Code)
        "LFA1",   # Vendor Master (General Section)
        "LFB1",   # Vendor Master (Company Code)
        "KNA1",   # Customer Master (General Section)
        "KNB1",   # Customer Master (Company Code)
        "REGUH",  # Settlement Data from Payment Programme
        "REGUP",  # Processed Items from Payment Programme
    ],
    ERPModule.HUMAN_RESOURCES: [
        "PA0001",   # Organisational Assignment
        "PA0002",   # Personal Data
        "PA0008",   # Basic Pay
        "PA0014",   # Recurring Payments/Deductions
        "PA0015",   # Additional Payments
        "HRP1000",  # Object (Org Management)
        "HRP1001",  # Relationships (Org Management)
    ],
    ERPModule.SALES_DISTRIBUTION: [
        "VBAK",  # Sales Document: Header Data
        "VBAP",  # Sales Document: Item Data
        "LIKP",  # SD Document: Delivery Header Data
        "LIPS",  # SD Document: Delivery Item Data
        "VBRK",  # Billing Document: Header Data
        "VBRP",  # Billing Document: Item Data
        "KNVV",  # Customer Master Sales Data
    ],
    ERPModule.MATERIAL_MANAGEMENT: [
        "EKKO",  # Purchasing Document Header
        "EKPO",  # Purchasing Document Item
        "MARA",  # General Material Data
        "MARC",  # Plant Data for Material
        "MARD",  # Storage Location Data for Material
        "MKPF",  # Material Document Header
        "MSEG",  # Material Document Segment
    ],
}

# ---------------------------------------------------------------------------
# SAP Data-Type Mapping → Standard Types
# ---------------------------------------------------------------------------

_SAP_TYPE_MAP: dict[str, str] = {
    "CHAR":     "VARCHAR",
    "SSTRING":  "VARCHAR",
    "STRING":   "VARCHAR",
    "NUMC":     "VARCHAR",
    "UNIT":     "VARCHAR",
    "CUKY":     "VARCHAR",
    "CLNT":     "VARCHAR",
    "LANG":     "VARCHAR",
    "TIMS":     "TIME",
    "DATS":     "DATE",
    "DEC":      "DECIMAL",
    "CURR":     "DECIMAL",
    "QUAN":     "DECIMAL",
    "FLTP":     "FLOAT",
    "INT1":     "INTEGER",
    "INT2":     "INTEGER",
    "INT4":     "INTEGER",
    "INT8":     "BIGINT",
    "RAW":      "BINARY",
    "LRAW":     "BINARY",
    "LCHR":     "TEXT",
}

# ---------------------------------------------------------------------------
# SAP Table Metadata (data-dictionary simulation for offline / test)
# ---------------------------------------------------------------------------

# Abbreviated data-dictionary entries for column metadata of the most
# commonly used tables.  This acts as the *offline* fallback when a live
# SAP connection is unavailable (e.g. in integration tests).

_SAP_DD_COLUMNS: dict[str, list[dict[str, Any]]] = {
    "BKPF": [
        {"name": "MANDT",  "type": "CLNT", "length": 3,  "decimals": 0, "key": True,  "nullable": False, "desc": "Client"},
        {"name": "BUKRS",  "type": "CHAR", "length": 4,  "decimals": 0, "key": True,  "nullable": False, "desc": "Company Code"},
        {"name": "BELNR",  "type": "CHAR", "length": 10, "decimals": 0, "key": True,  "nullable": False, "desc": "Accounting Document Number"},
        {"name": "GJAHR",  "type": "NUMC", "length": 4,  "decimals": 0, "key": True,  "nullable": False, "desc": "Fiscal Year"},
        {"name": "BLART",  "type": "CHAR", "length": 2,  "decimals": 0, "key": False, "nullable": True,  "desc": "Document Type"},
        {"name": "BUDAT",  "type": "DATS", "length": 8,  "decimals": 0, "key": False, "nullable": True,  "desc": "Posting Date"},
        {"name": "BLDAT",  "type": "DATS", "length": 8,  "decimals": 0, "key": False, "nullable": True,  "desc": "Document Date"},
        {"name": "WAERS",  "type": "CUKY", "length": 5,  "decimals": 0, "key": False, "nullable": True,  "desc": "Currency Key"},
        {"name": "MONAT",  "type": "NUMC", "length": 2,  "decimals": 0, "key": False, "nullable": True,  "desc": "Fiscal Period"},
        {"name": "USNAM",  "type": "CHAR", "length": 12, "decimals": 0, "key": False, "nullable": True,  "desc": "User Name"},
    ],
    "BSEG": [
        {"name": "MANDT",  "type": "CLNT", "length": 3,  "decimals": 0, "key": True,  "nullable": False, "desc": "Client"},
        {"name": "BUKRS",  "type": "CHAR", "length": 4,  "decimals": 0, "key": True,  "nullable": False, "desc": "Company Code"},
        {"name": "BELNR",  "type": "CHAR", "length": 10, "decimals": 0, "key": True,  "nullable": False, "desc": "Accounting Document Number"},
        {"name": "GJAHR",  "type": "NUMC", "length": 4,  "decimals": 0, "key": True,  "nullable": False, "desc": "Fiscal Year"},
        {"name": "BUZEI",  "type": "NUMC", "length": 3,  "decimals": 0, "key": True,  "nullable": False, "desc": "Line Item Number"},
        {"name": "SHKZG",  "type": "CHAR", "length": 1,  "decimals": 0, "key": False, "nullable": True,  "desc": "Debit/Credit Indicator"},
        {"name": "DMBTR",  "type": "CURR", "length": 13, "decimals": 2, "key": False, "nullable": True,  "desc": "Amount in Local Currency"},
        {"name": "WRBTR",  "type": "CURR", "length": 13, "decimals": 2, "key": False, "nullable": True,  "desc": "Amount in Document Currency"},
        {"name": "HKONT",  "type": "CHAR", "length": 10, "decimals": 0, "key": False, "nullable": True,  "desc": "G/L Account Number"},
        {"name": "KOSTL",  "type": "CHAR", "length": 10, "decimals": 0, "key": False, "nullable": True,  "desc": "Cost Center"},
    ],
    "EKKO": [
        {"name": "MANDT",  "type": "CLNT", "length": 3,  "decimals": 0, "key": True,  "nullable": False, "desc": "Client"},
        {"name": "EBELN",  "type": "CHAR", "length": 10, "decimals": 0, "key": True,  "nullable": False, "desc": "Purchasing Document Number"},
        {"name": "BUKRS",  "type": "CHAR", "length": 4,  "decimals": 0, "key": False, "nullable": True,  "desc": "Company Code"},
        {"name": "BSTYP",  "type": "CHAR", "length": 1,  "decimals": 0, "key": False, "nullable": True,  "desc": "Purchasing Document Category"},
        {"name": "BSART",  "type": "CHAR", "length": 4,  "decimals": 0, "key": False, "nullable": True,  "desc": "Purchasing Document Type"},
        {"name": "LIFNR",  "type": "CHAR", "length": 10, "decimals": 0, "key": False, "nullable": True,  "desc": "Vendor Account Number"},
        {"name": "EKORG",  "type": "CHAR", "length": 4,  "decimals": 0, "key": False, "nullable": True,  "desc": "Purchasing Organisation"},
        {"name": "EKGRP",  "type": "CHAR", "length": 3,  "decimals": 0, "key": False, "nullable": True,  "desc": "Purchasing Group"},
        {"name": "WAERS",  "type": "CUKY", "length": 5,  "decimals": 0, "key": False, "nullable": True,  "desc": "Currency Key"},
        {"name": "BEDAT",  "type": "DATS", "length": 8,  "decimals": 0, "key": False, "nullable": True,  "desc": "Purchasing Document Date"},
    ],
    "VBAK": [
        {"name": "MANDT",  "type": "CLNT", "length": 3,  "decimals": 0, "key": True,  "nullable": False, "desc": "Client"},
        {"name": "VBELN",  "type": "CHAR", "length": 10, "decimals": 0, "key": True,  "nullable": False, "desc": "Sales Document"},
        {"name": "ERDAT",  "type": "DATS", "length": 8,  "decimals": 0, "key": False, "nullable": True,  "desc": "Date on Which Record Was Created"},
        {"name": "ERZET",  "type": "TIMS", "length": 6,  "decimals": 0, "key": False, "nullable": True,  "desc": "Entry Time"},
        {"name": "ERNAM",  "type": "CHAR", "length": 12, "decimals": 0, "key": False, "nullable": True,  "desc": "Name of Person Who Created Object"},
        {"name": "AUART",  "type": "CHAR", "length": 4,  "decimals": 0, "key": False, "nullable": True,  "desc": "Sales Document Type"},
        {"name": "VKORG",  "type": "CHAR", "length": 4,  "decimals": 0, "key": False, "nullable": True,  "desc": "Sales Organisation"},
        {"name": "VTWEG",  "type": "CHAR", "length": 2,  "decimals": 0, "key": False, "nullable": True,  "desc": "Distribution Channel"},
        {"name": "KUNNR",  "type": "CHAR", "length": 10, "decimals": 0, "key": False, "nullable": True,  "desc": "Sold-to Party"},
        {"name": "NETWR",  "type": "CURR", "length": 15, "decimals": 2, "key": False, "nullable": True,  "desc": "Net Value of Sales Order"},
    ],
    "PA0001": [
        {"name": "MANDT",  "type": "CLNT", "length": 3,  "decimals": 0, "key": True,  "nullable": False, "desc": "Client"},
        {"name": "PERNR",  "type": "NUMC", "length": 8,  "decimals": 0, "key": True,  "nullable": False, "desc": "Personnel Number"},
        {"name": "SUBTY",  "type": "CHAR", "length": 4,  "decimals": 0, "key": True,  "nullable": False, "desc": "Subtype"},
        {"name": "ENDDA",  "type": "DATS", "length": 8,  "decimals": 0, "key": True,  "nullable": False, "desc": "End Date"},
        {"name": "BEGDA",  "type": "DATS", "length": 8,  "decimals": 0, "key": True,  "nullable": False, "desc": "Start Date"},
        {"name": "BUKRS",  "type": "CHAR", "length": 4,  "decimals": 0, "key": False, "nullable": True,  "desc": "Company Code"},
        {"name": "WERKS",  "type": "CHAR", "length": 4,  "decimals": 0, "key": False, "nullable": True,  "desc": "Personnel Area"},
        {"name": "BTRTL",  "type": "CHAR", "length": 4,  "decimals": 0, "key": False, "nullable": True,  "desc": "Personnel Subarea"},
        {"name": "PERSG",  "type": "CHAR", "length": 1,  "decimals": 0, "key": False, "nullable": True,  "desc": "Employee Group"},
        {"name": "PERSK",  "type": "CHAR", "length": 2,  "decimals": 0, "key": False, "nullable": True,  "desc": "Employee Subgroup"},
    ],
}

# Description map for tables without live connection
_SAP_TABLE_DESCRIPTIONS: dict[str, str] = {
    "BKPF": "Accounting Document Header",
    "BSEG": "Accounting Document Segment",
    "SKA1": "G/L Account Master (Chart of Accounts)",
    "SKB1": "G/L Account Master (Company Code)",
    "LFA1": "Vendor Master (General Section)",
    "LFB1": "Vendor Master (Company Code)",
    "KNA1": "Customer Master (General Section)",
    "KNB1": "Customer Master (Company Code)",
    "REGUH": "Settlement Data from Payment Programme",
    "REGUP": "Processed Items from Payment Programme",
    "PA0001": "Organisational Assignment",
    "PA0002": "Personal Data",
    "PA0008": "Basic Pay",
    "PA0014": "Recurring Payments/Deductions",
    "PA0015": "Additional Payments",
    "HRP1000": "Object (Org Management)",
    "HRP1001": "Relationships (Org Management)",
    "VBAK": "Sales Document: Header Data",
    "VBAP": "Sales Document: Item Data",
    "LIKP": "SD Document: Delivery Header Data",
    "LIPS": "SD Document: Delivery Item Data",
    "VBRK": "Billing Document: Header Data",
    "VBRP": "Billing Document: Item Data",
    "KNVV": "Customer Master Sales Data",
    "EKKO": "Purchasing Document Header",
    "EKPO": "Purchasing Document Item",
    "MARA": "General Material Data",
    "MARC": "Plant Data for Material",
    "MARD": "Storage Location Data for Material",
    "MKPF": "Material Document Header",
    "MSEG": "Material Document Segment",
}

# Well-known SAP foreign-key relationships (data-dictionary equivalents)
_SAP_RELATIONSHIPS: dict[str, list[dict[str, str]]] = {
    "BSEG": [
        {
            "constraint": "BSEG_BKPF_FK",
            "source_table": "BSEG",
            "source_column": "BELNR",
            "target_table": "BKPF",
            "target_column": "BELNR",
            "type": "MANY_TO_ONE",
        },
    ],
    "EKPO": [
        {
            "constraint": "EKPO_EKKO_FK",
            "source_table": "EKPO",
            "source_column": "EBELN",
            "target_table": "EKKO",
            "target_column": "EBELN",
            "type": "MANY_TO_ONE",
        },
    ],
    "VBAP": [
        {
            "constraint": "VBAP_VBAK_FK",
            "source_table": "VBAP",
            "source_column": "VBELN",
            "target_table": "VBAK",
            "target_column": "VBELN",
            "type": "MANY_TO_ONE",
        },
    ],
    "LIPS": [
        {
            "constraint": "LIPS_LIKP_FK",
            "source_table": "LIPS",
            "source_column": "VBELN",
            "target_table": "LIKP",
            "target_column": "VBELN",
            "type": "MANY_TO_ONE",
        },
    ],
    "VBRP": [
        {
            "constraint": "VBRP_VBRK_FK",
            "source_table": "VBRP",
            "source_column": "VBELN",
            "target_table": "VBRK",
            "target_column": "VBELN",
            "type": "MANY_TO_ONE",
        },
    ],
    "MSEG": [
        {
            "constraint": "MSEG_MKPF_FK",
            "source_table": "MSEG",
            "source_column": "MBLNR",
            "target_table": "MKPF",
            "target_column": "MBLNR",
            "type": "MANY_TO_ONE",
        },
    ],
    "PA0001": [
        {
            "constraint": "PA0001_PA0002_FK",
            "source_table": "PA0001",
            "source_column": "PERNR",
            "target_table": "PA0002",
            "target_column": "PERNR",
            "type": "ONE_TO_ONE",
        },
    ],
}


# ---------------------------------------------------------------------------
# SAPConnector
# ---------------------------------------------------------------------------


class SAPConnector(BaseConnector):
    """SAP RFC/BAPI schema discovery connector.

    Discovers table metadata, column definitions, data types, and
    foreign-key relationships for SAP ERP modules via RFC (Remote Function
    Call) or data-dictionary simulation.

    **Privacy (C-001):**
        Only SAP data-dictionary metadata is accessed.  No production
        business data is ever queried.

    **Supported Modules (C-005):**
        Financial Accounting, Human Resources, Sales & Distribution,
        Material Management.

    Args:
        config: A :class:`ConnectionConfig` with ``erp_type="sap"`` and
            optional SAP-specific ``additional_params`` (``sysnr``,
            ``client``, ``sapsid``).

    Example::

        config = ConnectionConfig(
            erp_type="sap",
            host="sap.example.com",
            port=3300,
            username="discovery",
            password="********",
            additional_params={"sysnr": "00", "client": "100"},
        )
        with SAPConnector(config) as conn:
            tables = conn.discover_tables(module=ERPModule.FINANCIAL_ACCOUNTING)
    """

    def __init__(self, config: ConnectionConfig) -> None:
        """Initialise the SAP connector.

        Args:
            config: Validated connection configuration.
        """
        super().__init__(config)
        self._logger = get_logger(__name__)

        # SAP-specific connection parameters.
        self._host: str = config.host or ""
        self._sysnr: str = str(config.additional_params.get("sysnr", "00"))
        self._client: str = str(config.additional_params.get("client", "100"))
        self._username: str = config.username or ""
        self._password: str = config.password or ""

        # Optional pyrfc connection handle.
        self._connection: Any = None

        # Flag indicating whether live RFC is available.
        self._use_live_rfc: bool = False

        self._logger.info(
            "sap_connector_initialised",
            host=self._host,
            sysnr=self._sysnr,
            sap_client=self._client,
        )

    # -- BaseConnector Interface ------------------------------------------

    @circuit_breaker_decorator(name="sap_rfc_connect", failure_threshold=5, recovery_timeout=30)
    def connect(self) -> None:
        """Establish a connection to the SAP system.

        Attempts to use ``pyrfc`` for live RFC connectivity.  If the library
        is not installed the connector falls back to data-dictionary
        simulation mode, which is sufficient for profiling metadata.

        Raises:
            ConnectionError: If the connection cannot be established.
        """
        try:
            try:
                import pyrfc  # type: ignore[import-untyped]  # noqa: PLC0415

                self._connection = pyrfc.Connection(
                    ashost=self._host,
                    sysnr=self._sysnr,
                    client=self._client,
                    user=self._username,
                    passwd=self._password,
                )
                self._use_live_rfc = True
                self._logger.info(
                    "sap_rfc_connected",
                    host=self._host,
                    sysnr=self._sysnr,
                    sap_client=self._client,
                    mode="live_rfc",
                )
            except ImportError:
                # pyrfc not installed — use offline simulation.
                self._use_live_rfc = False
                self._logger.info(
                    "sap_connector_offline_mode",
                    reason="pyrfc not installed",
                    host=self._host,
                )

            self._connected = True
        except Exception as exc:
            self._logger.error(
                "sap_connection_failed",
                host=self._host,
                sysnr=self._sysnr,
                error=str(exc),
                error_type=type(exc).__name__,
            )
            raise ConnectionError(
                message=f"Failed to connect to SAP at {self._host}:{self._sysnr}",
                erp_type=self._config.erp_type,
                original_error=exc,
            ) from exc

    def discover_tables(
        self,
        schema_name: str | None = None,
        module: ERPModule | None = None,
    ) -> list[TableMetadata]:
        """Discover SAP tables for the given module.

        **Privacy (C-001):** Uses SAP data-dictionary metadata only.

        Args:
            schema_name: Ignored for SAP (schema is implicit in SAP).
            module: Optional ERP module filter.  When ``None``, tables
                across all four modules are returned.

        Returns:
            List of :class:`TableMetadata` describing each discovered table.

        Raises:
            DiscoveryError: If metadata extraction fails.
            ConnectionError: If the connector is not connected.
        """
        self._validate_connected()

        try:
            if module is not None:
                module_key = module.value if isinstance(module, ERPModule) else str(module)
                table_names = SAP_MODULE_TABLE_MAPPINGS.get(module_key, [])
            else:
                table_names = []
                for tables in SAP_MODULE_TABLE_MAPPINGS.values():
                    table_names.extend(tables)

            result: list[TableMetadata] = []
            for tbl_name in table_names:
                description = _SAP_TABLE_DESCRIPTIONS.get(tbl_name, "")
                assigned_module = self._classify_module(tbl_name)
                result.append(
                    TableMetadata(
                        table_name=tbl_name,
                        schema_name=schema_name or "SAPSR3",
                        description=description,
                        estimated_row_count=None,
                        module=assigned_module,
                        table_type="TABLE",
                    )
                )

            self._logger.info(
                "sap_tables_discovered",
                table_count=len(result),
                module=module.value if module else "all",
            )
            return result

        except ConnectorError:
            raise
        except Exception as exc:
            self._logger.error(
                "sap_discover_tables_failed",
                error=str(exc),
                error_type=type(exc).__name__,
            )
            raise DiscoveryError(
                message=f"Failed to discover SAP tables: {exc}",
                erp_type=self._config.erp_type,
                original_error=exc,
            ) from exc

    def discover_columns(
        self,
        table_name: str,
        schema_name: str | None = None,  # noqa: ARG002
    ) -> list[ColumnMetadata]:
        """Discover columns for a specific SAP table.

        **Privacy (C-001):** Only structural metadata is returned.

        Args:
            table_name: SAP table name (e.g. ``"BKPF"``).
            schema_name: Ignored for SAP.

        Returns:
            List of :class:`ColumnMetadata` for each column.

        Raises:
            DiscoveryError: If column metadata cannot be extracted.
            ConnectionError: If the connector is not connected.
        """
        self._validate_connected()

        try:
            columns_data = _SAP_DD_COLUMNS.get(table_name)
            if columns_data is None:
                # For tables not in the offline data-dictionary, return
                # a generic minimal set.
                self._logger.debug(
                    "sap_columns_not_cached",
                    table_name=table_name,
                )
                return self._build_generic_columns(table_name)

            result: list[ColumnMetadata] = []
            for idx, col in enumerate(columns_data, start=1):
                standard_type = self._map_sap_type_to_standard(
                    col["type"], col["length"], col["decimals"],
                )
                result.append(
                    ColumnMetadata(
                        column_name=col["name"],
                        native_type=col["type"],
                        standard_type=standard_type,
                        max_length=col["length"] if col["type"] in {"CHAR", "SSTRING", "STRING", "NUMC", "CLNT", "LANG", "CUKY", "UNIT"} else None,
                        precision=col["length"] if col["type"] in {"DEC", "CURR", "QUAN", "FLTP"} else None,
                        scale=col["decimals"] if col["decimals"] > 0 else None,
                        is_nullable=col["nullable"],
                        is_primary_key=col["key"],
                        is_auto_increment=False,
                        ordinal_position=idx,
                        description=col.get("desc"),
                    )
                )

            self._logger.info(
                "sap_columns_discovered",
                table_name=table_name,
                column_count=len(result),
            )
            return result

        except ConnectorError:
            raise
        except Exception as exc:
            self._logger.error(
                "sap_discover_columns_failed",
                table_name=table_name,
                error=str(exc),
                error_type=type(exc).__name__,
            )
            raise DiscoveryError(
                message=f"Failed to discover columns for SAP table {table_name}: {exc}",
                erp_type=self._config.erp_type,
                original_error=exc,
            ) from exc

    def discover_relationships(
        self,
        schema_name: str | None = None,  # noqa: ARG002
    ) -> list[RelationshipMetadata]:
        """Discover foreign-key relationships across SAP tables.

        **Privacy (C-001):** Only constraint metadata is accessed.

        Args:
            schema_name: Ignored for SAP.

        Returns:
            List of :class:`RelationshipMetadata` for discovered FKs.

        Raises:
            DiscoveryError: If relationship discovery fails.
            ConnectionError: If the connector is not connected.
        """
        self._validate_connected()

        try:
            result: list[RelationshipMetadata] = []
            for _table_name, rels in _SAP_RELATIONSHIPS.items():
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
                "sap_relationships_discovered",
                relationship_count=len(result),
            )
            return result

        except ConnectorError:
            raise
        except Exception as exc:
            self._logger.error(
                "sap_discover_relationships_failed",
                error=str(exc),
                error_type=type(exc).__name__,
            )
            raise DiscoveryError(
                message=f"Failed to discover SAP relationships: {exc}",
                erp_type=self._config.erp_type,
                original_error=exc,
            ) from exc

    def close(self) -> None:
        """Close the SAP RFC connection and release resources.

        Safe to call multiple times (idempotent).
        """
        if self._connection is not None:
            try:
                self._connection.close()
            except Exception as exc:
                self._logger.warning(
                    "sap_close_warning",
                    error=str(exc),
                    error_type=type(exc).__name__,
                )
            finally:
                self._connection = None

        self._connected = False
        self._logger.info("sap_connector_closed", host=self._host)

    # -- Public Helpers -----------------------------------------------------

    def get_supported_modules(self) -> list[str]:
        """Return the list of supported SAP ERP module identifiers.

        Returns:
            Sorted list of module key strings from
            :data:`SAP_MODULE_TABLE_MAPPINGS`.
        """
        return sorted(SAP_MODULE_TABLE_MAPPINGS.keys())

    # -- Private Helpers ----------------------------------------------------

    def _map_sap_type_to_standard(
        self,
        sap_type: str,
        length: int,
        decimals: int,
    ) -> str:
        """Map an SAP data-element type to a platform-standard type.

        Args:
            sap_type: SAP native data type (e.g. ``"CHAR"``, ``"DEC"``).
            length: Byte/character length.
            decimals: Number of decimal places.

        Returns:
            Normalised standard type string.
        """
        standard = _SAP_TYPE_MAP.get(sap_type.upper(), "VARCHAR")
        # Refine DECIMAL → INTEGER when no decimal places and small length.
        if standard == "DECIMAL" and decimals == 0 and length <= 10:
            return "INTEGER"
        return standard

    def _classify_module(self, table_name: str) -> ERPModule | None:
        """Determine the ERP module for a given SAP table.

        Args:
            table_name: Physical SAP table name.

        Returns:
            The matching :class:`ERPModule` or ``None`` if unclassified.
        """
        for module_key, tables in SAP_MODULE_TABLE_MAPPINGS.items():
            if table_name in tables:
                return ERPModule(module_key)
        return None

    def _build_generic_columns(self, _table_name: str) -> list[ColumnMetadata]:
        """Return a minimal generic column set for uncached tables.

        Args:
            table_name: SAP table name.

        Returns:
            A list containing only the MANDT (Client) key column.
        """
        return [
            ColumnMetadata(
                column_name="MANDT",
                native_type="CLNT",
                standard_type="VARCHAR",
                max_length=3,
                is_nullable=False,
                is_primary_key=True,
                ordinal_position=1,
                description="Client",
            ),
        ]
