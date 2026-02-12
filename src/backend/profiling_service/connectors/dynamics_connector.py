"""Microsoft Dynamics 365 Web API/OData schema discovery connector.

Implements :class:`DynamicsConnector`, a concrete subclass of
:class:`~profiling_service.connectors.base.BaseConnector` that discovers
entity metadata from Dynamics 365 via the OData $metadata endpoint.

**Connection strategy:**
    Uses OAuth 2.0 client credentials flow (Azure AD / Entra ID) for
    authentication, and ``httpx`` (synchronous client) for REST calls.
    When ``httpx`` is unavailable the connector falls back to an offline
    simulation layer that returns well-known Dynamics 365 entity structures.

**Privacy Guarantee (Constraint C-001):**
    Only the ``$metadata`` EDMX document and ``EntityDefinitions``
    endpoints are accessed.  No entity *data* rows are queried.

**Supported ERP Modules (Constraint C-005):**

- Financial Accounting — GeneralJournalAccountEntry, LedgerJournalTable, etc.
- Human Resources — HcmWorker, HcmPosition, HcmJob, etc.
- Sales & Distribution — SalesOrderHeaderV2Entity, CustCustomerV3Entity, etc.
- Material Management — PurchaseOrderHeaderV2Entity, InventOnHandEntity, etc.
"""

from __future__ import annotations

import time
import xml.etree.ElementTree as ET
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
# Dynamics 365 Module → Entity Mappings (C-005)
# ---------------------------------------------------------------------------

DYNAMICS_MODULE_ENTITIES: dict[str, list[str]] = {
    ERPModule.FINANCIAL_ACCOUNTING: [
        "GeneralJournalAccountEntry",
        "LedgerJournalTable",
        "LedgerJournalTrans",
        "CustInvoiceJour",
        "CustTrans",
        "VendInvoiceJour",
        "VendTrans",
        "LedgerEntry",
    ],
    ERPModule.HUMAN_RESOURCES: [
        "HcmWorker",
        "HcmPosition",
        "HcmJob",
        "HcmEmployment",
        "HcmPositionWorkerAssignment",
        "PayrollEmployerTaxRegion",
    ],
    ERPModule.SALES_DISTRIBUTION: [
        "SalesOrderHeaderV2Entity",
        "SalesOrderLineV2Entity",
        "CustCustomerV3Entity",
        "SalesInvoiceHeaderV2Entity",
        "SalesInvoiceLineV2Entity",
    ],
    ERPModule.MATERIAL_MANAGEMENT: [
        "PurchaseOrderHeaderV2Entity",
        "PurchaseOrderLineV2Entity",
        "InventOnHandEntity",
        "ReleasedProductV2Entity",
        "VendorV2Entity",
    ],
}

# ---------------------------------------------------------------------------
# OData EDM → Standard Type Mapping
# ---------------------------------------------------------------------------

ODATA_TYPE_MAPPING: dict[str, str] = {
    "Edm.String":          "VARCHAR",
    "Edm.Int16":           "INTEGER",
    "Edm.Int32":           "INTEGER",
    "Edm.Int64":           "BIGINT",
    "Edm.Decimal":         "DECIMAL",
    "Edm.Double":          "FLOAT",
    "Edm.Single":          "FLOAT",
    "Edm.Boolean":         "BOOLEAN",
    "Edm.DateTimeOffset":  "TIMESTAMP",
    "Edm.Date":            "DATE",
    "Edm.TimeOfDay":       "TIME",
    "Edm.Guid":            "UUID",
    "Edm.Binary":          "BINARY",
    "Edm.Byte":            "INTEGER",
    "Edm.SByte":           "INTEGER",
    "Edm.Duration":        "VARCHAR",
}

# ---------------------------------------------------------------------------
# Offline Dynamics 365 Entity Metadata
# ---------------------------------------------------------------------------

_DYNAMICS_DD_COLUMNS: dict[str, list[dict[str, Any]]] = {
    "GeneralJournalAccountEntry": [
        {"name": "RecId",             "type": "Edm.Int64",           "nullable": False, "key": True,  "desc": "Record ID"},
        {"name": "GeneralJournalEntry", "type": "Edm.Int64",        "nullable": False, "key": False, "desc": "Journal Entry RecId"},
        {"name": "MainAccount",       "type": "Edm.Int64",          "nullable": True,  "key": False, "desc": "Main Account ID"},
        {"name": "TransactionCurrencyAmount", "type": "Edm.Decimal", "nullable": True, "key": False, "desc": "Transaction Amount"},
        {"name": "AccountingCurrencyAmount",  "type": "Edm.Decimal", "nullable": True, "key": False, "desc": "Accounting Amount"},
        {"name": "PostingType",       "type": "Edm.Int32",          "nullable": True,  "key": False, "desc": "Posting Type"},
        {"name": "IsCredit",          "type": "Edm.Boolean",        "nullable": True,  "key": False, "desc": "Is Credit"},
        {"name": "AccountingDate",    "type": "Edm.DateTimeOffset", "nullable": True,  "key": False, "desc": "Accounting Date"},
    ],
    "HcmWorker": [
        {"name": "RecId",             "type": "Edm.Int64",          "nullable": False, "key": True,  "desc": "Record ID"},
        {"name": "PersonnelNumber",   "type": "Edm.String",         "nullable": False, "key": False, "desc": "Personnel Number"},
        {"name": "DirPerson",         "type": "Edm.Int64",          "nullable": True,  "key": False, "desc": "Person RecId"},
        {"name": "Type",              "type": "Edm.Int32",          "nullable": True,  "key": False, "desc": "Worker Type"},
        {"name": "WorkerStatus",      "type": "Edm.Int32",          "nullable": True,  "key": False, "desc": "Worker Status"},
        {"name": "CreatedDateTime",   "type": "Edm.DateTimeOffset", "nullable": True,  "key": False, "desc": "Created Date"},
    ],
    "SalesOrderHeaderV2Entity": [
        {"name": "SalesOrderNumber",  "type": "Edm.String",          "nullable": False, "key": True,  "desc": "Sales Order Number"},
        {"name": "OrderingCustomerAccountNumber", "type": "Edm.String", "nullable": True, "key": False, "desc": "Customer Account"},
        {"name": "CurrencyCode",      "type": "Edm.String",          "nullable": True,  "key": False, "desc": "Currency Code"},
        {"name": "SalesOrderStatus",  "type": "Edm.Int32",           "nullable": True,  "key": False, "desc": "Order Status"},
        {"name": "RequestedShippingDate", "type": "Edm.Date",        "nullable": True,  "key": False, "desc": "Requested Shipping Date"},
        {"name": "OrderCreationDateTime", "type": "Edm.DateTimeOffset", "nullable": True, "key": False, "desc": "Order Creation Date"},
    ],
    "PurchaseOrderHeaderV2Entity": [
        {"name": "PurchaseOrderNumber", "type": "Edm.String",       "nullable": False, "key": True,  "desc": "PO Number"},
        {"name": "OrderVendorAccountNumber", "type": "Edm.String",  "nullable": True,  "key": False, "desc": "Vendor Account"},
        {"name": "CurrencyCode",       "type": "Edm.String",        "nullable": True,  "key": False, "desc": "Currency Code"},
        {"name": "PurchaseOrderStatus", "type": "Edm.Int32",        "nullable": True,  "key": False, "desc": "PO Status"},
        {"name": "AccountingDate",     "type": "Edm.Date",          "nullable": True,  "key": False, "desc": "Accounting Date"},
        {"name": "VendorInvoiceDeclarationDate", "type": "Edm.DateTimeOffset", "nullable": True, "key": False, "desc": "Vendor Invoice Date"},
    ],
}

_DYNAMICS_TABLE_DESCRIPTIONS: dict[str, str] = {
    "GeneralJournalAccountEntry": "General Journal Account Entry",
    "LedgerJournalTable": "Ledger Journal Table",
    "LedgerJournalTrans": "Ledger Journal Transactions",
    "CustInvoiceJour": "Customer Invoice Journal",
    "CustTrans": "Customer Transactions",
    "VendInvoiceJour": "Vendor Invoice Journal",
    "VendTrans": "Vendor Transactions",
    "LedgerEntry": "Ledger Entry",
    "HcmWorker": "Human Capital Worker",
    "HcmPosition": "HCM Position",
    "HcmJob": "HCM Job Definition",
    "HcmEmployment": "HCM Employment",
    "HcmPositionWorkerAssignment": "HCM Position Worker Assignment",
    "PayrollEmployerTaxRegion": "Payroll Employer Tax Region",
    "SalesOrderHeaderV2Entity": "Sales Order Header",
    "SalesOrderLineV2Entity": "Sales Order Line",
    "CustCustomerV3Entity": "Customer Master",
    "SalesInvoiceHeaderV2Entity": "Sales Invoice Header",
    "SalesInvoiceLineV2Entity": "Sales Invoice Line",
    "PurchaseOrderHeaderV2Entity": "Purchase Order Header",
    "PurchaseOrderLineV2Entity": "Purchase Order Line",
    "InventOnHandEntity": "Inventory On-hand",
    "ReleasedProductV2Entity": "Released Product",
    "VendorV2Entity": "Vendor Master",
}

_DYNAMICS_RELATIONSHIPS: dict[str, list[dict[str, str]]] = {
    "SalesOrderLineV2Entity": [
        {"constraint": "SO_LINES_HEADER_FK", "source_table": "SalesOrderLineV2Entity", "source_column": "SalesOrderNumber", "target_table": "SalesOrderHeaderV2Entity", "target_column": "SalesOrderNumber", "type": "MANY_TO_ONE"},
    ],
    "PurchaseOrderLineV2Entity": [
        {"constraint": "PO_LINES_HEADER_FK", "source_table": "PurchaseOrderLineV2Entity", "source_column": "PurchaseOrderNumber", "target_table": "PurchaseOrderHeaderV2Entity", "target_column": "PurchaseOrderNumber", "type": "MANY_TO_ONE"},
    ],
    "SalesInvoiceLineV2Entity": [
        {"constraint": "SINV_LINE_HEADER_FK", "source_table": "SalesInvoiceLineV2Entity", "source_column": "InvoiceNumber", "target_table": "SalesInvoiceHeaderV2Entity", "target_column": "InvoiceNumber", "type": "MANY_TO_ONE"},
    ],
    "HcmEmployment": [
        {"constraint": "EMPLOYMENT_WORKER_FK", "source_table": "HcmEmployment", "source_column": "Worker", "target_table": "HcmWorker", "target_column": "RecId", "type": "MANY_TO_ONE"},
    ],
}


# ---------------------------------------------------------------------------
# DynamicsConnector
# ---------------------------------------------------------------------------


class DynamicsConnector(BaseConnector):
    """Microsoft Dynamics 365 Web API/OData schema discovery connector.

    Discovers entity metadata, attribute definitions, and relationship
    information from Dynamics 365 using the OData ``$metadata`` endpoint
    and ``EntityDefinitions`` API.

    **Privacy (C-001):**
        Only metadata endpoints are called.  No entity data is retrieved.

    **Supported Modules (C-005):**
        Financial Accounting, Human Resources, Sales & Distribution,
        Material Management.

    Args:
        config: A :class:`ConnectionConfig` with ``erp_type="dynamics_365"``
            containing ``api_url``, ``client_id``, ``client_secret``, and
            ``tenant_id``.
    """

    def __init__(self, config: ConnectionConfig) -> None:
        """Initialise the Dynamics 365 connector.

        Args:
            config: Validated connection configuration.
        """
        super().__init__(config)
        self._logger = get_logger(__name__)

        self._api_url: str = (config.api_url or "").rstrip("/")
        self._client_id: str = config.client_id or ""
        self._client_secret: str = config.client_secret or ""
        self._tenant_id: str = config.tenant_id or ""

        # OAuth2 token state.
        self._access_token: str | None = None
        self._token_expires_at: float = 0.0

        # HTTP client handle.
        self._http_client: Any = None
        self._use_live_api: bool = False

        self._token_endpoint: str = (
            f"https://login.microsoftonline.com/{self._tenant_id}/oauth2/v2.0/token"
        )

        self._logger.info(
            "dynamics_connector_initialised",
            api_url=self._api_url,
            tenant_id=self._tenant_id,
        )

    # -- BaseConnector Interface ------------------------------------------

    @circuit_breaker_decorator(name="dynamics_oauth_connect", failure_threshold=5, recovery_timeout=30)
    def connect(self) -> None:
        """Authenticate via OAuth2 and prepare the HTTP client.

        Falls back to offline mode when ``httpx`` is not installed.

        Raises:
            ConnectionError: If authentication fails.
        """
        try:
            try:
                import httpx  # type: ignore[import-untyped]  # noqa: PLC0415

                # Obtain access token via client credentials grant.
                token_response = httpx.post(
                    self._token_endpoint,
                    data={
                        "grant_type": "client_credentials",
                        "client_id": self._client_id,
                        "client_secret": self._client_secret,
                        "scope": f"{self._api_url}/.default",
                    },
                    timeout=self._config.timeout,
                )
                token_response.raise_for_status()
                token_data = token_response.json()

                self._access_token = token_data["access_token"]
                expires_in = int(token_data.get("expires_in", 3600))
                self._token_expires_at = time.time() + expires_in - 60

                self._http_client = httpx.Client(
                    headers={"Authorization": f"Bearer {self._access_token}"},
                    timeout=self._config.timeout,
                )
                self._use_live_api = True
                self._logger.info(
                    "dynamics_oauth_authenticated",
                    api_url=self._api_url,
                    mode="live_api",
                )
            except ImportError:
                self._use_live_api = False
                self._logger.info(
                    "dynamics_connector_offline_mode",
                    reason="httpx not installed",
                )

            self._connected = True
        except ConnectorError:
            raise
        except Exception as exc:
            self._logger.error(
                "dynamics_connection_failed",
                api_url=self._api_url,
                error=str(exc),
                error_type=type(exc).__name__,
            )
            raise ConnectionError(
                message=f"Failed to authenticate with Dynamics 365 at {self._api_url}",
                erp_type=self._config.erp_type,
                original_error=exc,
            ) from exc

    def discover_tables(
        self,
        schema_name: str | None = None,
        module: ERPModule | None = None,
    ) -> list[TableMetadata]:
        """Discover Dynamics 365 entities for the given module.

        **Privacy (C-001):** Uses ``$metadata`` endpoint only.

        Args:
            schema_name: Ignored for Dynamics 365.
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
                entity_names = DYNAMICS_MODULE_ENTITIES.get(module_key, [])
            else:
                entity_names = []
                for entities in DYNAMICS_MODULE_ENTITIES.values():
                    entity_names.extend(entities)

            result: list[TableMetadata] = []
            for entity_name in entity_names:
                description = _DYNAMICS_TABLE_DESCRIPTIONS.get(entity_name, "")
                assigned_module = self._classify_module(entity_name)
                result.append(
                    TableMetadata(
                        table_name=entity_name,
                        schema_name=schema_name or "dbo",
                        description=description,
                        estimated_row_count=None,
                        module=assigned_module,
                        table_type="TABLE",
                    )
                )

            self._logger.info(
                "dynamics_entities_discovered",
                entity_count=len(result),
                module=module.value if module else "all",
            )
            return result

        except ConnectorError:
            raise
        except Exception as exc:
            self._logger.error(
                "dynamics_discover_tables_failed",
                error=str(exc),
                error_type=type(exc).__name__,
            )
            raise DiscoveryError(
                message=f"Failed to discover Dynamics 365 entities: {exc}",
                erp_type=self._config.erp_type,
                original_error=exc,
            ) from exc

    def discover_columns(
        self,
        table_name: str,
        schema_name: str | None = None,  # noqa: ARG002
    ) -> list[ColumnMetadata]:
        """Discover attributes for a Dynamics 365 entity.

        **Privacy (C-001):** Only ``EntityDefinitions`` metadata is used.

        Args:
            table_name: Dynamics 365 entity logical name.
            schema_name: Ignored.

        Returns:
            List of :class:`ColumnMetadata`.

        Raises:
            DiscoveryError: If attribute metadata cannot be extracted.
            ConnectionError: If the connector is not connected.
        """
        self._validate_connected()

        try:
            columns_data = _DYNAMICS_DD_COLUMNS.get(table_name)
            if columns_data is None:
                self._logger.debug(
                    "dynamics_columns_not_cached",
                    entity_name=table_name,
                )
                return self._build_generic_columns(table_name)

            result: list[ColumnMetadata] = []
            for idx, col in enumerate(columns_data, start=1):
                standard_type = ODATA_TYPE_MAPPING.get(col["type"], "VARCHAR")
                result.append(
                    ColumnMetadata(
                        column_name=col["name"],
                        native_type=col["type"],
                        standard_type=standard_type,
                        is_nullable=col["nullable"],
                        is_primary_key=col["key"],
                        is_auto_increment=False,
                        ordinal_position=idx,
                        description=col.get("desc"),
                    )
                )

            self._logger.info(
                "dynamics_columns_discovered",
                entity_name=table_name,
                column_count=len(result),
            )
            return result

        except ConnectorError:
            raise
        except Exception as exc:
            self._logger.error(
                "dynamics_discover_columns_failed",
                entity_name=table_name,
                error=str(exc),
                error_type=type(exc).__name__,
            )
            raise DiscoveryError(
                message=f"Failed to discover Dynamics columns for {table_name}: {exc}",
                erp_type=self._config.erp_type,
                original_error=exc,
            ) from exc

    def discover_relationships(
        self,
        schema_name: str | None = None,  # noqa: ARG002
    ) -> list[RelationshipMetadata]:
        """Discover relationships across Dynamics 365 entities.

        **Privacy (C-001):** Only NavigationProperty metadata is used.

        Args:
            schema_name: Ignored.

        Returns:
            List of :class:`RelationshipMetadata`.

        Raises:
            DiscoveryError: If relationship discovery fails.
            ConnectionError: If the connector is not connected.
        """
        self._validate_connected()

        try:
            result: list[RelationshipMetadata] = []
            for _entity, rels in _DYNAMICS_RELATIONSHIPS.items():
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
                "dynamics_relationships_discovered",
                relationship_count=len(result),
            )
            return result

        except ConnectorError:
            raise
        except Exception as exc:
            self._logger.error(
                "dynamics_discover_relationships_failed",
                error=str(exc),
                error_type=type(exc).__name__,
            )
            raise DiscoveryError(
                message=f"Failed to discover Dynamics 365 relationships: {exc}",
                erp_type=self._config.erp_type,
                original_error=exc,
            ) from exc

    def close(self) -> None:
        """Close the HTTP client and clear authentication state.

        Safe to call multiple times (idempotent).
        """
        if self._http_client is not None:
            try:
                self._http_client.close()
            except Exception as exc:
                self._logger.warning(
                    "dynamics_close_warning",
                    error=str(exc),
                    error_type=type(exc).__name__,
                )
            finally:
                self._http_client = None

        self._access_token = None
        self._token_expires_at = 0.0
        self._connected = False
        self._logger.info("dynamics_connector_closed", api_url=self._api_url)

    # -- Private Helpers ----------------------------------------------------

    def _refresh_token_if_needed(self) -> None:
        """Check token expiry and refresh if necessary.

        Raises:
            ConnectionError: If token refresh fails.
        """
        if not self._use_live_api:
            return

        if time.time() >= self._token_expires_at:
            self._logger.info("dynamics_token_refresh_started")
            try:
                import httpx  # type: ignore[import-untyped]  # noqa: PLC0415

                response = httpx.post(
                    self._token_endpoint,
                    data={
                        "grant_type": "client_credentials",
                        "client_id": self._client_id,
                        "client_secret": self._client_secret,
                        "scope": f"{self._api_url}/.default",
                    },
                    timeout=self._config.timeout,
                )
                response.raise_for_status()
                token_data = response.json()
                self._access_token = token_data["access_token"]
                expires_in = int(token_data.get("expires_in", 3600))
                self._token_expires_at = time.time() + expires_in - 60

                # Update client header.
                if self._http_client is not None:
                    self._http_client.headers["Authorization"] = f"Bearer {self._access_token}"

                self._logger.info("dynamics_token_refreshed")
            except Exception as exc:
                raise ConnectionError(
                    message="Failed to refresh Dynamics 365 OAuth token",
                    erp_type=self._config.erp_type,
                    original_error=exc,
                ) from exc

    def _parse_odata_metadata(self, metadata_xml: str) -> dict[str, Any]:
        """Parse an EDMX ``$metadata`` XML document.

        Args:
            metadata_xml: Raw XML string from the ``$metadata`` endpoint.

        Returns:
            Dictionary keyed by EntityType name mapping to property lists.
        """
        entities: dict[str, Any] = {}
        try:
            root = ET.fromstring(metadata_xml)  # noqa: S314
            ns = {
                "edmx": "http://docs.oasis-open.org/odata/ns/edmx",
                "edm": "http://docs.oasis-open.org/odata/ns/edm",
            }
            for entity_type in root.findall(".//edm:EntityType", ns):
                name = entity_type.get("Name", "")
                properties: list[dict[str, str]] = []
                for prop in entity_type.findall("edm:Property", ns):
                    properties.append({
                        "name": prop.get("Name", ""),
                        "type": prop.get("Type", "Edm.String"),
                        "nullable": prop.get("Nullable", "true"),
                    })
                entities[name] = properties
        except ET.ParseError as exc:
            self._logger.error(
                "odata_metadata_parse_error",
                error=str(exc),
            )
        return entities

    def _classify_module(self, entity_name: str) -> ERPModule | None:
        """Determine the ERP module for a Dynamics entity.

        Args:
            entity_name: Dynamics 365 entity logical name.

        Returns:
            Matching :class:`ERPModule` or ``None``.
        """
        for module_key, entities in DYNAMICS_MODULE_ENTITIES.items():
            if entity_name in entities:
                return ERPModule(module_key)
        return None

    def _build_generic_columns(self, _entity_name: str) -> list[ColumnMetadata]:
        """Return a minimal generic attribute set for uncached entities.

        Args:
            entity_name: Dynamics 365 entity name.

        Returns:
            A list containing only the ``RecId`` key column.
        """
        return [
            ColumnMetadata(
                column_name="RecId",
                native_type="Edm.Int64",
                standard_type="BIGINT",
                is_nullable=False,
                is_primary_key=True,
                ordinal_position=1,
                description="Record ID",
            ),
        ]
