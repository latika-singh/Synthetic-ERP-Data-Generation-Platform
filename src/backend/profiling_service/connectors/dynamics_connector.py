"""Microsoft Dynamics 365 Web API/OData schema discovery connector.

Implements :class:`DynamicsConnector`, a concrete subclass of
:class:`~profiling_service.connectors.base.BaseConnector` that discovers
entity metadata from Microsoft Dynamics 365 Finance & Operations via the
OData ``$metadata`` endpoint and the ``EntityDefinitions`` Web API.

**Connection Strategy:**
    Uses OAuth 2.0 client credentials flow (Azure AD / Entra ID) for
    authentication.  The connector obtains a bearer token by posting to
    ``https://login.microsoftonline.com/{tenant_id}/oauth2/v2.0/token``
    with ``grant_type=client_credentials``, then uses ``httpx.Client``
    for synchronous REST calls with connection pooling and configurable
    timeouts.

    When ``httpx`` is unavailable (e.g. air-gapped CI environments
    without full dependency installation), the connector falls back to
    a comprehensive offline metadata layer that returns well-known
    Dynamics 365 entity structures for the four supported ERP modules.

**Privacy Guarantee (Constraint C-001):**
    Only the ``$metadata`` EDMX document and ``EntityDefinitions``
    endpoints are accessed.  No entity *data* rows are queried, read,
    or streamed at any point.

**Supported ERP Modules (Constraint C-005):**

- Financial Accounting — GeneralJournalAccountEntry, LedgerJournalTable, etc.
- Human Resources — HcmWorker, HcmPosition, HcmJob, etc.
- Sales & Distribution — SalesOrderHeaderV2Entity, CustCustomerV3Entity, etc.
- Material Management — PurchaseOrderHeaderV2Entity, InventOnHandEntity, etc.

Usage::

    from profiling_service.connectors.base import ConnectionConfig
    from profiling_service.connectors.dynamics_connector import DynamicsConnector

    config = ConnectionConfig(
        erp_type="dynamics365",
        api_url="https://myorg.operations.dynamics.com",
        client_id="<azure-ad-client-id>",
        client_secret="<azure-ad-client-secret>",
        tenant_id="<azure-ad-tenant-id>",
    )

    with DynamicsConnector(config) as connector:
        tables = connector.discover_tables(module=ERPModule.FINANCIAL_ACCOUNTING)
        for table in tables:
            columns = connector.discover_columns(table.table_name)
"""

from __future__ import annotations

import time
import xml.etree.ElementTree as ET
from typing import Any

from profiling_service.connectors.base import (
    BaseConnector,
    ColumnMetadata,
    ConnectionConfig,
    ConnectionError as ConnConnectionError,
    ConnectorError,
    DiscoveryError,
    ERPModule,
    RelationshipMetadata,
    TableMetadata,
)
from shared.logging.structured_logger import get_logger
from shared.middleware.circuit_breaker import circuit_breaker_decorator


# ---------------------------------------------------------------------------
# Optional third-party dependency — httpx.
# When unavailable the connector operates in offline/simulation mode,
# returning well-known Dynamics 365 entity structures from the built-in
# metadata dictionaries.  This supports air-gapped environments (C-003).
# ---------------------------------------------------------------------------

try:
    import httpx

    _HTTPX_AVAILABLE: bool = True
except ImportError:  # pragma: no cover
    httpx = None  # type: ignore[assignment]
    _HTTPX_AVAILABLE = False


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
    "Edm.String": "VARCHAR",
    "Edm.Int16": "INTEGER",
    "Edm.Int32": "INTEGER",
    "Edm.Int64": "BIGINT",
    "Edm.Decimal": "DECIMAL",
    "Edm.Double": "FLOAT",
    "Edm.Single": "FLOAT",
    "Edm.Boolean": "BOOLEAN",
    "Edm.DateTimeOffset": "TIMESTAMP",
    "Edm.Date": "DATE",
    "Edm.TimeOfDay": "TIME",
    "Edm.Guid": "UUID",
    "Edm.Binary": "BINARY",
    "Edm.Byte": "INTEGER",
    "Edm.SByte": "INTEGER",
    "Edm.Duration": "VARCHAR",
}


# ---------------------------------------------------------------------------
# OData EDMX XML namespaces used for parsing $metadata documents.
# ---------------------------------------------------------------------------

_EDMX_NAMESPACES: dict[str, str] = {
    "edmx": "http://docs.oasis-open.org/odata/ns/edmx",
    "edm": "http://docs.oasis-open.org/odata/ns/edm",
}

# Dynamics 365 Web API base path for data entities.
_D365_DATA_API_PATH: str = "/api/data/v9.2"


# ---------------------------------------------------------------------------
# Offline Dynamics 365 Entity Column Metadata
# Provides realistic column definitions for all supported entities so
# that schema discovery works without a live Dynamics 365 connection.
# ---------------------------------------------------------------------------

_DYNAMICS_DD_COLUMNS: dict[str, list[dict[str, Any]]] = {
    # ---- Financial Accounting ----
    "GeneralJournalAccountEntry": [
        {"name": "RecId", "type": "Edm.Int64", "nullable": False, "key": True, "desc": "Record ID"},
        {"name": "GeneralJournalEntry", "type": "Edm.Int64", "nullable": False, "key": False, "desc": "Journal Entry RecId"},
        {"name": "MainAccount", "type": "Edm.Int64", "nullable": True, "key": False, "desc": "Main Account ID"},
        {"name": "TransactionCurrencyAmount", "type": "Edm.Decimal", "nullable": True, "key": False, "desc": "Transaction Amount"},
        {"name": "AccountingCurrencyAmount", "type": "Edm.Decimal", "nullable": True, "key": False, "desc": "Accounting Amount"},
        {"name": "PostingType", "type": "Edm.Int32", "nullable": True, "key": False, "desc": "Posting Type"},
        {"name": "IsCredit", "type": "Edm.Boolean", "nullable": True, "key": False, "desc": "Is Credit"},
        {"name": "AccountingDate", "type": "Edm.DateTimeOffset", "nullable": True, "key": False, "desc": "Accounting Date"},
    ],
    "LedgerJournalTable": [
        {"name": "RecId", "type": "Edm.Int64", "nullable": False, "key": True, "desc": "Record ID"},
        {"name": "JournalNum", "type": "Edm.String", "nullable": False, "key": False, "desc": "Journal Number"},
        {"name": "JournalName", "type": "Edm.String", "nullable": True, "key": False, "desc": "Journal Name"},
        {"name": "JournalType", "type": "Edm.Int32", "nullable": True, "key": False, "desc": "Journal Type"},
        {"name": "Posted", "type": "Edm.Int32", "nullable": True, "key": False, "desc": "Posted Status"},
        {"name": "CreatedDateTime", "type": "Edm.DateTimeOffset", "nullable": True, "key": False, "desc": "Created Date"},
        {"name": "CurrentOperationsTax", "type": "Edm.Int32", "nullable": True, "key": False, "desc": "Operations Tax"},
    ],
    "LedgerJournalTrans": [
        {"name": "RecId", "type": "Edm.Int64", "nullable": False, "key": True, "desc": "Record ID"},
        {"name": "JournalNum", "type": "Edm.String", "nullable": False, "key": False, "desc": "Journal Number"},
        {"name": "Voucher", "type": "Edm.String", "nullable": True, "key": False, "desc": "Voucher Number"},
        {"name": "AccountNum", "type": "Edm.String", "nullable": True, "key": False, "desc": "Account Number"},
        {"name": "AmountCurDebit", "type": "Edm.Decimal", "nullable": True, "key": False, "desc": "Debit Amount"},
        {"name": "AmountCurCredit", "type": "Edm.Decimal", "nullable": True, "key": False, "desc": "Credit Amount"},
        {"name": "CurrencyCode", "type": "Edm.String", "nullable": True, "key": False, "desc": "Currency Code"},
        {"name": "TransDate", "type": "Edm.Date", "nullable": True, "key": False, "desc": "Transaction Date"},
    ],
    "CustInvoiceJour": [
        {"name": "RecId", "type": "Edm.Int64", "nullable": False, "key": True, "desc": "Record ID"},
        {"name": "InvoiceId", "type": "Edm.String", "nullable": False, "key": False, "desc": "Invoice ID"},
        {"name": "InvoiceAccount", "type": "Edm.String", "nullable": True, "key": False, "desc": "Invoice Account"},
        {"name": "SalesId", "type": "Edm.String", "nullable": True, "key": False, "desc": "Sales Order ID"},
        {"name": "InvoiceAmount", "type": "Edm.Decimal", "nullable": True, "key": False, "desc": "Invoice Amount"},
        {"name": "InvoiceDate", "type": "Edm.Date", "nullable": True, "key": False, "desc": "Invoice Date"},
        {"name": "CurrencyCode", "type": "Edm.String", "nullable": True, "key": False, "desc": "Currency Code"},
    ],
    "CustTrans": [
        {"name": "RecId", "type": "Edm.Int64", "nullable": False, "key": True, "desc": "Record ID"},
        {"name": "AccountNum", "type": "Edm.String", "nullable": False, "key": False, "desc": "Customer Account"},
        {"name": "Invoice", "type": "Edm.String", "nullable": True, "key": False, "desc": "Invoice Number"},
        {"name": "AmountCur", "type": "Edm.Decimal", "nullable": True, "key": False, "desc": "Amount in Currency"},
        {"name": "AmountMST", "type": "Edm.Decimal", "nullable": True, "key": False, "desc": "Amount in MST"},
        {"name": "TransDate", "type": "Edm.Date", "nullable": True, "key": False, "desc": "Transaction Date"},
        {"name": "TransType", "type": "Edm.Int32", "nullable": True, "key": False, "desc": "Transaction Type"},
    ],
    "VendInvoiceJour": [
        {"name": "RecId", "type": "Edm.Int64", "nullable": False, "key": True, "desc": "Record ID"},
        {"name": "InvoiceId", "type": "Edm.String", "nullable": False, "key": False, "desc": "Invoice ID"},
        {"name": "InvoiceAccount", "type": "Edm.String", "nullable": True, "key": False, "desc": "Invoice Account"},
        {"name": "PurchId", "type": "Edm.String", "nullable": True, "key": False, "desc": "Purchase Order ID"},
        {"name": "InvoiceAmount", "type": "Edm.Decimal", "nullable": True, "key": False, "desc": "Invoice Amount"},
        {"name": "InvoiceDate", "type": "Edm.Date", "nullable": True, "key": False, "desc": "Invoice Date"},
        {"name": "CurrencyCode", "type": "Edm.String", "nullable": True, "key": False, "desc": "Currency Code"},
    ],
    "VendTrans": [
        {"name": "RecId", "type": "Edm.Int64", "nullable": False, "key": True, "desc": "Record ID"},
        {"name": "AccountNum", "type": "Edm.String", "nullable": False, "key": False, "desc": "Vendor Account"},
        {"name": "Invoice", "type": "Edm.String", "nullable": True, "key": False, "desc": "Invoice Number"},
        {"name": "AmountCur", "type": "Edm.Decimal", "nullable": True, "key": False, "desc": "Amount in Currency"},
        {"name": "AmountMST", "type": "Edm.Decimal", "nullable": True, "key": False, "desc": "Amount in MST"},
        {"name": "TransDate", "type": "Edm.Date", "nullable": True, "key": False, "desc": "Transaction Date"},
        {"name": "TransType", "type": "Edm.Int32", "nullable": True, "key": False, "desc": "Transaction Type"},
    ],
    "LedgerEntry": [
        {"name": "RecId", "type": "Edm.Int64", "nullable": False, "key": True, "desc": "Record ID"},
        {"name": "GeneralJournalEntry", "type": "Edm.Int64", "nullable": False, "key": False, "desc": "Journal Entry RecId"},
        {"name": "PostingLayer", "type": "Edm.Int32", "nullable": True, "key": False, "desc": "Posting Layer"},
        {"name": "AccountingDate", "type": "Edm.DateTimeOffset", "nullable": True, "key": False, "desc": "Accounting Date"},
        {"name": "JournalNumber", "type": "Edm.String", "nullable": True, "key": False, "desc": "Journal Number"},
        {"name": "IsCorrection", "type": "Edm.Boolean", "nullable": True, "key": False, "desc": "Is Correction"},
    ],
    # ---- Human Resources ----
    "HcmWorker": [
        {"name": "RecId", "type": "Edm.Int64", "nullable": False, "key": True, "desc": "Record ID"},
        {"name": "PersonnelNumber", "type": "Edm.String", "nullable": False, "key": False, "desc": "Personnel Number"},
        {"name": "DirPerson", "type": "Edm.Int64", "nullable": True, "key": False, "desc": "Person RecId"},
        {"name": "Type", "type": "Edm.Int32", "nullable": True, "key": False, "desc": "Worker Type"},
        {"name": "WorkerStatus", "type": "Edm.Int32", "nullable": True, "key": False, "desc": "Worker Status"},
        {"name": "CreatedDateTime", "type": "Edm.DateTimeOffset", "nullable": True, "key": False, "desc": "Created Date"},
    ],
    "HcmPosition": [
        {"name": "RecId", "type": "Edm.Int64", "nullable": False, "key": True, "desc": "Record ID"},
        {"name": "PositionId", "type": "Edm.String", "nullable": False, "key": False, "desc": "Position ID"},
        {"name": "Department", "type": "Edm.Int64", "nullable": True, "key": False, "desc": "Department RecId"},
        {"name": "Job", "type": "Edm.Int64", "nullable": True, "key": False, "desc": "Job RecId"},
        {"name": "PositionType", "type": "Edm.Int32", "nullable": True, "key": False, "desc": "Position Type"},
        {"name": "AvailableForAssignment", "type": "Edm.DateTimeOffset", "nullable": True, "key": False, "desc": "Available for Assignment"},
    ],
    "HcmJob": [
        {"name": "RecId", "type": "Edm.Int64", "nullable": False, "key": True, "desc": "Record ID"},
        {"name": "JobId", "type": "Edm.String", "nullable": False, "key": False, "desc": "Job ID"},
        {"name": "MaximumPositions", "type": "Edm.Int32", "nullable": True, "key": False, "desc": "Max Positions"},
        {"name": "JobDescription", "type": "Edm.String", "nullable": True, "key": False, "desc": "Job Description"},
        {"name": "JobType", "type": "Edm.Int64", "nullable": True, "key": False, "desc": "Job Type RecId"},
        {"name": "AllowUnlimitedPositions", "type": "Edm.Boolean", "nullable": True, "key": False, "desc": "Allow Unlimited"},
    ],
    "HcmEmployment": [
        {"name": "RecId", "type": "Edm.Int64", "nullable": False, "key": True, "desc": "Record ID"},
        {"name": "Worker", "type": "Edm.Int64", "nullable": False, "key": False, "desc": "Worker RecId"},
        {"name": "LegalEntity", "type": "Edm.Int64", "nullable": True, "key": False, "desc": "Legal Entity RecId"},
        {"name": "EmploymentStartDate", "type": "Edm.DateTimeOffset", "nullable": True, "key": False, "desc": "Start Date"},
        {"name": "EmploymentEndDate", "type": "Edm.DateTimeOffset", "nullable": True, "key": False, "desc": "End Date"},
        {"name": "EmploymentType", "type": "Edm.Int32", "nullable": True, "key": False, "desc": "Employment Type"},
    ],
    "HcmPositionWorkerAssignment": [
        {"name": "RecId", "type": "Edm.Int64", "nullable": False, "key": True, "desc": "Record ID"},
        {"name": "Worker", "type": "Edm.Int64", "nullable": False, "key": False, "desc": "Worker RecId"},
        {"name": "Position", "type": "Edm.Int64", "nullable": False, "key": False, "desc": "Position RecId"},
        {"name": "ValidFrom", "type": "Edm.DateTimeOffset", "nullable": True, "key": False, "desc": "Valid From"},
        {"name": "ValidTo", "type": "Edm.DateTimeOffset", "nullable": True, "key": False, "desc": "Valid To"},
        {"name": "AssignmentReasonCode", "type": "Edm.String", "nullable": True, "key": False, "desc": "Reason Code"},
    ],
    "PayrollEmployerTaxRegion": [
        {"name": "RecId", "type": "Edm.Int64", "nullable": False, "key": True, "desc": "Record ID"},
        {"name": "LegalEntity", "type": "Edm.Int64", "nullable": False, "key": False, "desc": "Legal Entity RecId"},
        {"name": "TaxRegionId", "type": "Edm.String", "nullable": True, "key": False, "desc": "Tax Region ID"},
        {"name": "TaxRegionName", "type": "Edm.String", "nullable": True, "key": False, "desc": "Tax Region Name"},
        {"name": "CountryRegion", "type": "Edm.String", "nullable": True, "key": False, "desc": "Country/Region"},
        {"name": "State", "type": "Edm.String", "nullable": True, "key": False, "desc": "State"},
    ],
    # ---- Sales & Distribution ----
    "SalesOrderHeaderV2Entity": [
        {"name": "SalesOrderNumber", "type": "Edm.String", "nullable": False, "key": True, "desc": "Sales Order Number"},
        {"name": "OrderingCustomerAccountNumber", "type": "Edm.String", "nullable": True, "key": False, "desc": "Customer Account"},
        {"name": "CurrencyCode", "type": "Edm.String", "nullable": True, "key": False, "desc": "Currency Code"},
        {"name": "SalesOrderStatus", "type": "Edm.Int32", "nullable": True, "key": False, "desc": "Order Status"},
        {"name": "RequestedShippingDate", "type": "Edm.Date", "nullable": True, "key": False, "desc": "Requested Shipping Date"},
        {"name": "OrderCreationDateTime", "type": "Edm.DateTimeOffset", "nullable": True, "key": False, "desc": "Order Creation Date"},
        {"name": "TotalChargeAmount", "type": "Edm.Decimal", "nullable": True, "key": False, "desc": "Total Charge Amount"},
    ],
    "SalesOrderLineV2Entity": [
        {"name": "SalesOrderNumber", "type": "Edm.String", "nullable": False, "key": True, "desc": "Sales Order Number"},
        {"name": "SalesOrderLineNumber", "type": "Edm.Decimal", "nullable": False, "key": True, "desc": "Line Number"},
        {"name": "ItemNumber", "type": "Edm.String", "nullable": True, "key": False, "desc": "Item Number"},
        {"name": "OrderedSalesQuantity", "type": "Edm.Decimal", "nullable": True, "key": False, "desc": "Ordered Quantity"},
        {"name": "SalesPrice", "type": "Edm.Decimal", "nullable": True, "key": False, "desc": "Sales Price"},
        {"name": "LineAmount", "type": "Edm.Decimal", "nullable": True, "key": False, "desc": "Line Amount"},
    ],
    "CustCustomerV3Entity": [
        {"name": "CustomerAccount", "type": "Edm.String", "nullable": False, "key": True, "desc": "Customer Account Number"},
        {"name": "CustomerGroupId", "type": "Edm.String", "nullable": True, "key": False, "desc": "Customer Group"},
        {"name": "OrganizationName", "type": "Edm.String", "nullable": True, "key": False, "desc": "Organization Name"},
        {"name": "SalesCurrencyCode", "type": "Edm.String", "nullable": True, "key": False, "desc": "Sales Currency"},
        {"name": "CreditLimit", "type": "Edm.Decimal", "nullable": True, "key": False, "desc": "Credit Limit"},
        {"name": "IsSalesTaxIncludedInPrices", "type": "Edm.Boolean", "nullable": True, "key": False, "desc": "Tax Included"},
    ],
    "SalesInvoiceHeaderV2Entity": [
        {"name": "InvoiceNumber", "type": "Edm.String", "nullable": False, "key": True, "desc": "Invoice Number"},
        {"name": "InvoiceCustomerAccountNumber", "type": "Edm.String", "nullable": True, "key": False, "desc": "Customer Account"},
        {"name": "SalesOrderNumber", "type": "Edm.String", "nullable": True, "key": False, "desc": "Sales Order Number"},
        {"name": "InvoiceDate", "type": "Edm.Date", "nullable": True, "key": False, "desc": "Invoice Date"},
        {"name": "TotalInvoiceAmount", "type": "Edm.Decimal", "nullable": True, "key": False, "desc": "Total Amount"},
        {"name": "CurrencyCode", "type": "Edm.String", "nullable": True, "key": False, "desc": "Currency Code"},
    ],
    "SalesInvoiceLineV2Entity": [
        {"name": "InvoiceNumber", "type": "Edm.String", "nullable": False, "key": True, "desc": "Invoice Number"},
        {"name": "InvoiceLineNumber", "type": "Edm.Decimal", "nullable": False, "key": True, "desc": "Line Number"},
        {"name": "ItemNumber", "type": "Edm.String", "nullable": True, "key": False, "desc": "Item Number"},
        {"name": "InvoicedQuantity", "type": "Edm.Decimal", "nullable": True, "key": False, "desc": "Invoiced Quantity"},
        {"name": "SalesPrice", "type": "Edm.Decimal", "nullable": True, "key": False, "desc": "Sales Price"},
        {"name": "LineAmount", "type": "Edm.Decimal", "nullable": True, "key": False, "desc": "Line Amount"},
    ],
    # ---- Material Management ----
    "PurchaseOrderHeaderV2Entity": [
        {"name": "PurchaseOrderNumber", "type": "Edm.String", "nullable": False, "key": True, "desc": "PO Number"},
        {"name": "OrderVendorAccountNumber", "type": "Edm.String", "nullable": True, "key": False, "desc": "Vendor Account"},
        {"name": "CurrencyCode", "type": "Edm.String", "nullable": True, "key": False, "desc": "Currency Code"},
        {"name": "PurchaseOrderStatus", "type": "Edm.Int32", "nullable": True, "key": False, "desc": "PO Status"},
        {"name": "AccountingDate", "type": "Edm.Date", "nullable": True, "key": False, "desc": "Accounting Date"},
        {"name": "VendorInvoiceDeclarationDate", "type": "Edm.DateTimeOffset", "nullable": True, "key": False, "desc": "Vendor Invoice Date"},
    ],
    "PurchaseOrderLineV2Entity": [
        {"name": "PurchaseOrderNumber", "type": "Edm.String", "nullable": False, "key": True, "desc": "PO Number"},
        {"name": "PurchaseOrderLineNumber", "type": "Edm.Decimal", "nullable": False, "key": True, "desc": "Line Number"},
        {"name": "ItemNumber", "type": "Edm.String", "nullable": True, "key": False, "desc": "Item Number"},
        {"name": "OrderedPurchaseQuantity", "type": "Edm.Decimal", "nullable": True, "key": False, "desc": "Ordered Quantity"},
        {"name": "PurchasePrice", "type": "Edm.Decimal", "nullable": True, "key": False, "desc": "Purchase Price"},
        {"name": "LineAmount", "type": "Edm.Decimal", "nullable": True, "key": False, "desc": "Line Amount"},
    ],
    "InventOnHandEntity": [
        {"name": "ItemId", "type": "Edm.String", "nullable": False, "key": True, "desc": "Item ID"},
        {"name": "InventSiteId", "type": "Edm.String", "nullable": True, "key": False, "desc": "Site ID"},
        {"name": "InventLocationId", "type": "Edm.String", "nullable": True, "key": False, "desc": "Warehouse ID"},
        {"name": "PhysicalInventory", "type": "Edm.Decimal", "nullable": True, "key": False, "desc": "Physical Inventory"},
        {"name": "AvailPhysical", "type": "Edm.Decimal", "nullable": True, "key": False, "desc": "Available Physical"},
        {"name": "PostedQuantity", "type": "Edm.Decimal", "nullable": True, "key": False, "desc": "Posted Quantity"},
    ],
    "ReleasedProductV2Entity": [
        {"name": "ProductNumber", "type": "Edm.String", "nullable": False, "key": True, "desc": "Product Number"},
        {"name": "ItemModelGroupId", "type": "Edm.String", "nullable": True, "key": False, "desc": "Item Model Group"},
        {"name": "ProductType", "type": "Edm.Int32", "nullable": True, "key": False, "desc": "Product Type"},
        {"name": "SearchName", "type": "Edm.String", "nullable": True, "key": False, "desc": "Search Name"},
        {"name": "ProductSubType", "type": "Edm.Int32", "nullable": True, "key": False, "desc": "Product Sub Type"},
        {"name": "IsStockedProduct", "type": "Edm.Boolean", "nullable": True, "key": False, "desc": "Is Stocked"},
    ],
    "VendorV2Entity": [
        {"name": "VendorAccountNumber", "type": "Edm.String", "nullable": False, "key": True, "desc": "Vendor Account"},
        {"name": "VendorGroupId", "type": "Edm.String", "nullable": True, "key": False, "desc": "Vendor Group"},
        {"name": "VendorOrganizationName", "type": "Edm.String", "nullable": True, "key": False, "desc": "Organization Name"},
        {"name": "DefaultPaymentDayName", "type": "Edm.String", "nullable": True, "key": False, "desc": "Payment Day"},
        {"name": "PurchaseCurrencyCode", "type": "Edm.String", "nullable": True, "key": False, "desc": "Purchase Currency"},
        {"name": "IsOnHold", "type": "Edm.Boolean", "nullable": True, "key": False, "desc": "Is On Hold"},
    ],
}

# ---------------------------------------------------------------------------
# Offline Entity Descriptions
# ---------------------------------------------------------------------------

_DYNAMICS_TABLE_DESCRIPTIONS: dict[str, str] = {
    "GeneralJournalAccountEntry": "General Journal Account Entry — subledger transactions",
    "LedgerJournalTable": "Ledger Journal Table — journal header records",
    "LedgerJournalTrans": "Ledger Journal Transactions — journal line items",
    "CustInvoiceJour": "Customer Invoice Journal — posted customer invoices",
    "CustTrans": "Customer Transactions — accounts receivable ledger",
    "VendInvoiceJour": "Vendor Invoice Journal — posted vendor invoices",
    "VendTrans": "Vendor Transactions — accounts payable ledger",
    "LedgerEntry": "Ledger Entry — general ledger postings",
    "HcmWorker": "Human Capital Worker — employee master data",
    "HcmPosition": "HCM Position — organisational position definitions",
    "HcmJob": "HCM Job Definition — job template definitions",
    "HcmEmployment": "HCM Employment — worker employment records",
    "HcmPositionWorkerAssignment": "HCM Position Worker Assignment — position allocations",
    "PayrollEmployerTaxRegion": "Payroll Employer Tax Region — tax jurisdiction definitions",
    "SalesOrderHeaderV2Entity": "Sales Order Header — sales order master records",
    "SalesOrderLineV2Entity": "Sales Order Line — sales order line items",
    "CustCustomerV3Entity": "Customer Master — customer account records",
    "SalesInvoiceHeaderV2Entity": "Sales Invoice Header — posted sales invoice headers",
    "SalesInvoiceLineV2Entity": "Sales Invoice Line — posted sales invoice line items",
    "PurchaseOrderHeaderV2Entity": "Purchase Order Header — purchase order master records",
    "PurchaseOrderLineV2Entity": "Purchase Order Line — purchase order line items",
    "InventOnHandEntity": "Inventory On-hand — current stock positions",
    "ReleasedProductV2Entity": "Released Product — product definitions released to legal entities",
    "VendorV2Entity": "Vendor Master — vendor account records",
}

# ---------------------------------------------------------------------------
# Offline Relationship Definitions
# ---------------------------------------------------------------------------

_DYNAMICS_RELATIONSHIPS: dict[str, list[dict[str, str]]] = {
    "SalesOrderLineV2Entity": [
        {
            "constraint": "FK_SOLine_SOHeader",
            "source_table": "SalesOrderLineV2Entity",
            "source_column": "SalesOrderNumber",
            "target_table": "SalesOrderHeaderV2Entity",
            "target_column": "SalesOrderNumber",
            "type": "MANY_TO_ONE",
        },
    ],
    "SalesOrderHeaderV2Entity": [
        {
            "constraint": "FK_SOHeader_Customer",
            "source_table": "SalesOrderHeaderV2Entity",
            "source_column": "OrderingCustomerAccountNumber",
            "target_table": "CustCustomerV3Entity",
            "target_column": "CustomerAccount",
            "type": "MANY_TO_ONE",
        },
    ],
    "PurchaseOrderLineV2Entity": [
        {
            "constraint": "FK_POLine_POHeader",
            "source_table": "PurchaseOrderLineV2Entity",
            "source_column": "PurchaseOrderNumber",
            "target_table": "PurchaseOrderHeaderV2Entity",
            "target_column": "PurchaseOrderNumber",
            "type": "MANY_TO_ONE",
        },
    ],
    "PurchaseOrderHeaderV2Entity": [
        {
            "constraint": "FK_POHeader_Vendor",
            "source_table": "PurchaseOrderHeaderV2Entity",
            "source_column": "OrderVendorAccountNumber",
            "target_table": "VendorV2Entity",
            "target_column": "VendorAccountNumber",
            "type": "MANY_TO_ONE",
        },
    ],
    "SalesInvoiceLineV2Entity": [
        {
            "constraint": "FK_SInvLine_SInvHeader",
            "source_table": "SalesInvoiceLineV2Entity",
            "source_column": "InvoiceNumber",
            "target_table": "SalesInvoiceHeaderV2Entity",
            "target_column": "InvoiceNumber",
            "type": "MANY_TO_ONE",
        },
    ],
    "HcmEmployment": [
        {
            "constraint": "FK_Employment_Worker",
            "source_table": "HcmEmployment",
            "source_column": "Worker",
            "target_table": "HcmWorker",
            "target_column": "RecId",
            "type": "MANY_TO_ONE",
        },
    ],
    "HcmPositionWorkerAssignment": [
        {
            "constraint": "FK_PosAssign_Worker",
            "source_table": "HcmPositionWorkerAssignment",
            "source_column": "Worker",
            "target_table": "HcmWorker",
            "target_column": "RecId",
            "type": "MANY_TO_ONE",
        },
        {
            "constraint": "FK_PosAssign_Position",
            "source_table": "HcmPositionWorkerAssignment",
            "source_column": "Position",
            "target_table": "HcmPosition",
            "target_column": "RecId",
            "type": "MANY_TO_ONE",
        },
    ],
    "HcmPosition": [
        {
            "constraint": "FK_Position_Job",
            "source_table": "HcmPosition",
            "source_column": "Job",
            "target_table": "HcmJob",
            "target_column": "RecId",
            "type": "MANY_TO_ONE",
        },
    ],
    "LedgerJournalTrans": [
        {
            "constraint": "FK_JournalTrans_JournalTable",
            "source_table": "LedgerJournalTrans",
            "source_column": "JournalNum",
            "target_table": "LedgerJournalTable",
            "target_column": "JournalNum",
            "type": "MANY_TO_ONE",
        },
    ],
    "CustTrans": [
        {
            "constraint": "FK_CustTrans_CustInvoiceJour",
            "source_table": "CustTrans",
            "source_column": "Invoice",
            "target_table": "CustInvoiceJour",
            "target_column": "InvoiceId",
            "type": "MANY_TO_ONE",
        },
    ],
    "VendTrans": [
        {
            "constraint": "FK_VendTrans_VendInvoiceJour",
            "source_table": "VendTrans",
            "source_column": "Invoice",
            "target_table": "VendInvoiceJour",
            "target_column": "InvoiceId",
            "type": "MANY_TO_ONE",
        },
    ],
    "GeneralJournalAccountEntry": [
        {
            "constraint": "FK_GJAccEntry_LedgerEntry",
            "source_table": "GeneralJournalAccountEntry",
            "source_column": "GeneralJournalEntry",
            "target_table": "LedgerEntry",
            "target_column": "GeneralJournalEntry",
            "type": "MANY_TO_ONE",
        },
    ],
}


# ---------------------------------------------------------------------------
# DynamicsConnector
# ---------------------------------------------------------------------------


class DynamicsConnector(BaseConnector):
    """Microsoft Dynamics 365 Web API/OData schema discovery connector.

    Discovers entity metadata, attribute definitions, and relationship
    information from Dynamics 365 Finance & Operations using the OData
    ``$metadata`` endpoint and ``EntityDefinitions`` API.

    **Privacy (C-001):**
        Only metadata endpoints are called.  No entity data is retrieved,
        queried, or streamed.

    **Supported Modules (C-005):**
        Financial Accounting, Human Resources, Sales & Distribution,
        Material Management.

    **Resilience:**
        External API calls are protected by the circuit breaker pattern
        (``circuit_breaker_decorator``) and exponential-backoff retry
        (``_retry_with_backoff``) to prevent cascade failures from
        unavailable Azure AD token endpoints or Dynamics 365 Web API.

    Args:
        config: A :class:`ConnectionConfig` with ``erp_type="dynamics365"``
            containing ``api_url``, ``client_id``, ``client_secret``, and
            ``tenant_id``.

    Example::

        config = ConnectionConfig(
            erp_type="dynamics365",
            api_url="https://myorg.operations.dynamics.com",
            client_id="<client-id>",
            client_secret="<secret>",
            tenant_id="<tenant-id>",
        )

        with DynamicsConnector(config) as conn:
            tables = conn.discover_tables(
                module=ERPModule.FINANCIAL_ACCOUNTING,
            )
    """

    def __init__(self, config: ConnectionConfig) -> None:
        """Initialise the Dynamics 365 connector.

        Extracts Azure AD / Entra ID credentials and the Dynamics 365
        API base URL from *config*.  Builds the OAuth 2.0 token endpoint
        URL for the configured tenant.

        Args:
            config: Validated connection configuration.  Required fields:
                ``api_url``, ``client_id``, ``client_secret``, ``tenant_id``.
        """
        super().__init__(config)
        self._logger = get_logger(__name__)

        # Connection parameters extracted from config.
        self._api_url: str = (config.api_url or "").rstrip("/")
        self._client_id: str = config.client_id or ""
        self._client_secret: str = config.client_secret or ""
        self._tenant_id: str = config.tenant_id or ""

        # OAuth2 token state.
        self._access_token: str | None = None
        self._token_expires_at: float = 0.0

        # HTTP client handle — typed as Optional[httpx.Client] when httpx
        # is available, otherwise kept as None throughout the lifecycle.
        self._http_client: Any | None = None
        self._use_live_api: bool = False

        # Azure AD v2.0 token endpoint for client credentials flow.
        self._token_endpoint: str = (
            f"https://login.microsoftonline.com/{self._tenant_id}/oauth2/v2.0/token"
        )

        # Cached EDMX metadata from live API (populated on first $metadata call).
        self._cached_edmx: dict[str, Any] | None = None

        self._logger.info(
            "dynamics_connector_initialised",
            api_url=self._api_url,
            tenant_id=self._tenant_id,
            httpx_available=_HTTPX_AVAILABLE,
        )

    # -- BaseConnector Interface -------------------------------------------

    @circuit_breaker_decorator(
        name="dynamics365_oauth_connect",
        failure_threshold=5,
        recovery_timeout=30,
    )
    def connect(self) -> None:
        """Authenticate via OAuth2 client credentials and prepare the HTTP client.

        Performs the following steps:

        1. POST to the Azure AD v2.0 token endpoint with
           ``grant_type=client_credentials`` to obtain a bearer token.
        2. Create an ``httpx.Client`` with the bearer token in the
           ``Authorization`` header.
        3. Validate the connection by issuing a HEAD/GET to the
           ``$metadata`` endpoint (metadata only — C-001).

        Falls back to offline mode when ``httpx`` is not installed,
        allowing the connector to return built-in metadata dictionaries
        without network access.

        Raises:
            ConnectionError: If OAuth2 authentication fails or the
                ``$metadata`` endpoint is unreachable.
        """
        try:
            if _HTTPX_AVAILABLE and httpx is not None:
                self._authenticate_oauth2()
                self._use_live_api = True
                self._logger.info(
                    "dynamics_oauth_authenticated",
                    api_url=self._api_url,
                    mode="live_api",
                )
            else:
                # Offline/simulation mode — no network access required.
                self._use_live_api = False
                self._logger.info(
                    "dynamics_connector_offline_mode",
                    reason="httpx_not_installed",
                )

            self._connected = True

        except ConnectorError:
            # Already a connector-level exception — propagate as-is.
            raise
        except Exception as exc:
            self._logger.error(
                "dynamics_connection_failed",
                api_url=self._api_url,
                error=str(exc),
                error_type=type(exc).__name__,
            )
            raise ConnConnectionError(
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

        When connected to a live Dynamics 365 instance, fetches the OData
        ``$metadata`` EDMX document and parses EntityType definitions.
        Otherwise, returns entities from the built-in metadata dictionaries.

        **Privacy (C-001):** Only the ``$metadata`` endpoint is accessed.
        No entity data is retrieved.

        Args:
            schema_name: Ignored for Dynamics 365 (OData has no schema
                concept); passed through to :class:`TableMetadata` if
                provided, defaults to ``"dbo"``.
            module: Optional ERP module filter.  When provided, only
                entities belonging to the specified functional module are
                returned.

        Returns:
            List of :class:`TableMetadata` instances.

        Raises:
            DiscoveryError: If metadata extraction fails.
            ConnectionError: If the connector is not connected.
        """
        self._validate_connected()

        try:
            self._refresh_token_if_needed()

            # Determine entity names based on module filter.
            if module is not None:
                module_key = module.value if isinstance(module, ERPModule) else str(module)
                entity_names = DYNAMICS_MODULE_ENTITIES.get(module_key, [])
            else:
                entity_names = [
                    name
                    for entities in DYNAMICS_MODULE_ENTITIES.values()
                    for name in entities
                ]

            # Attempt live API metadata retrieval first.
            live_entities: dict[str, Any] | None = None
            if self._use_live_api and self.connected:
                live_entities = self._fetch_live_metadata()

            result: list[TableMetadata] = []
            effective_schema = schema_name or "dbo"

            for entity_name in entity_names:
                # Determine description from live metadata or offline dict.
                description = _DYNAMICS_TABLE_DESCRIPTIONS.get(entity_name, "")
                if live_entities and entity_name in live_entities:
                    live_props = live_entities[entity_name]
                    if isinstance(live_props, dict) and live_props.get("description"):
                        description = live_props["description"]

                assigned_module = self._classify_module(entity_name)
                result.append(
                    TableMetadata(
                        table_name=entity_name,
                        schema_name=effective_schema,
                        description=description,
                        estimated_row_count=None,
                        module=assigned_module,
                        table_type="TABLE",
                    ),
                )

            self._logger.info(
                "dynamics_entities_discovered",
                entity_count=len(result),
                module=module.value if module else "all",
                mode="live" if live_entities else "offline",
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
        schema_name: str | None = None,
    ) -> list[ColumnMetadata]:
        """Discover attributes for a Dynamics 365 entity.

        When connected to a live Dynamics 365 instance, queries the
        ``EntityDefinitions(LogicalName='{entity}')/Attributes`` endpoint
        for rich attribute metadata including display names, option sets,
        and validation constraints.  Falls back to the built-in column
        dictionaries when the live API is unavailable.

        **Privacy (C-001):** Only ``EntityDefinitions`` metadata is used.
        No entity data rows are accessed.

        Args:
            table_name: Dynamics 365 entity logical name (e.g.
                ``"HcmWorker"``, ``"SalesOrderHeaderV2Entity"``).
            schema_name: Ignored for Dynamics 365.

        Returns:
            List of :class:`ColumnMetadata` instances ordered by
            ``ordinal_position``.

        Raises:
            DiscoveryError: If attribute metadata cannot be extracted.
            ConnectionError: If the connector is not connected.
        """
        self._validate_connected()
        _ = schema_name  # Unused for Dynamics 365 (no schema namespaces).

        try:
            self._refresh_token_if_needed()

            # Attempt live API attribute discovery first.
            if self._use_live_api and self.connected:
                live_columns = self._fetch_live_entity_attributes(table_name)
                if live_columns:
                    self._logger.info(
                        "dynamics_columns_discovered",
                        entity_name=table_name,
                        column_count=len(live_columns),
                        mode="live",
                    )
                    return live_columns

            # Fallback to offline column metadata.
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
                    ),
                )

            self._logger.info(
                "dynamics_columns_discovered",
                entity_name=table_name,
                column_count=len(result),
                mode="offline",
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
        schema_name: str | None = None,
    ) -> list[RelationshipMetadata]:
        """Discover foreign-key relationships across Dynamics 365 entities.

        When connected to a live Dynamics 365 instance, queries the
        ``EntityDefinitions/OneToManyRelationships`` and
        ``ManyToOneRelationships`` endpoints for each known entity.
        Falls back to the built-in relationship definitions when the
        live API is unavailable.

        **Privacy (C-001):** Only NavigationProperty and relationship
        metadata is used.  No entity data is accessed.

        Args:
            schema_name: Optional entity logical name to filter
                relationships for a specific entity.  When ``None``,
                all known relationships are returned.

        Returns:
            List of :class:`RelationshipMetadata` instances.

        Raises:
            DiscoveryError: If relationship discovery fails.
            ConnectionError: If the connector is not connected.
        """
        self._validate_connected()

        try:
            self._refresh_token_if_needed()

            # Attempt live API relationship discovery first.
            if self._use_live_api and self.connected:
                live_rels = self._fetch_live_relationships(schema_name)
                if live_rels:
                    self._logger.info(
                        "dynamics_relationships_discovered",
                        relationship_count=len(live_rels),
                        filter_entity=schema_name or "all",
                        mode="live",
                    )
                    return live_rels

            # Fallback to offline relationship metadata.
            result: list[RelationshipMetadata] = []
            for entity_name, rels in _DYNAMICS_RELATIONSHIPS.items():
                # If schema_name is specified, treat it as an entity filter.
                if schema_name is not None and entity_name != schema_name:
                    continue

                for rel in rels:
                    result.append(
                        RelationshipMetadata(
                            constraint_name=rel["constraint"],
                            source_table=rel["source_table"],
                            source_column=rel["source_column"],
                            target_table=rel["target_table"],
                            target_column=rel["target_column"],
                            relationship_type=rel["type"],
                        ),
                    )

            self._logger.info(
                "dynamics_relationships_discovered",
                relationship_count=len(result),
                filter_entity=schema_name or "all",
                mode="offline",
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

        Releases all held resources (HTTP connection pool, cached
        tokens).  Safe to call multiple times (idempotent).
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
        self._cached_edmx = None
        self._connected = False
        self._logger.info("dynamics_connector_closed", api_url=self._api_url)

    # -- Private Helpers: Authentication -----------------------------------

    def _authenticate_oauth2(self) -> None:
        """Perform OAuth2 client credentials authentication with Azure AD.

        Creates a temporary ``httpx.Client`` for the token request, then
        initialises the main ``self._http_client`` with the obtained
        bearer token.  Uses ``_retry_with_backoff`` for resilience against
        transient Azure AD failures.

        Raises:
            ConnectionError: If the token endpoint returns an error or
                is unreachable after retries.
        """

        def _do_authenticate() -> None:
            auth_client: httpx.Client = httpx.Client(timeout=self._config.timeout)
            try:
                token_response: httpx.Response = auth_client.post(
                    self._token_endpoint,
                    data={
                        "grant_type": "client_credentials",
                        "client_id": self._client_id,
                        "client_secret": self._client_secret,
                        "scope": f"{self._api_url}/.default",
                    },
                )

                # Log status for diagnostics but never log credentials.
                self._logger.debug(
                    "dynamics_token_response",
                    status_code=token_response.status_code,
                    api_url=self._api_url,
                )

                token_response.raise_for_status()
                token_data: dict[str, Any] = token_response.json()

                self._access_token = token_data["access_token"]
                expires_in = int(token_data.get("expires_in", 3600))
                # Refresh 60 s before actual expiry to avoid edge-case failures.
                self._token_expires_at = time.time() + expires_in - 60

            except httpx.HTTPError as http_exc:
                raise ConnConnectionError(
                    message=(
                        f"Dynamics 365 OAuth2 token request failed "
                        f"(endpoint={self._token_endpoint})"
                    ),
                    erp_type=self._config.erp_type,
                    original_error=http_exc,
                ) from http_exc
            finally:
                auth_client.close()

            # Create the persistent HTTP client with the bearer token.
            self._http_client = httpx.Client(
                headers={
                    "Authorization": f"Bearer {self._access_token}",
                    "Accept": "application/json",
                    "OData-MaxVersion": "4.0",
                    "OData-Version": "4.0",
                },
                timeout=self._config.timeout,
            )

            # Validate connectivity by fetching $metadata (METADATA ONLY — C-001).
            metadata_url = f"{self._api_url}{_D365_DATA_API_PATH}/$metadata"
            validation_response: httpx.Response = self._http_client.get(metadata_url)
            self._logger.debug(
                "dynamics_metadata_validation",
                status_code=validation_response.status_code,
                url=metadata_url,
            )
            validation_response.raise_for_status()

        # Wrap the auth flow in retry logic from the base connector.
        self._retry_with_backoff(
            _do_authenticate,
            operation_name="dynamics_oauth2_authenticate",
        )

    def _refresh_token_if_needed(self) -> None:
        """Check token expiry and refresh via Azure AD if necessary.

        Called before every API request to ensure the bearer token is
        still valid.  If the token has expired (or is within 60 seconds
        of expiry), a new token is obtained from the Azure AD v2.0
        endpoint.

        Raises:
            ConnectionError: If token refresh fails after retries.
        """
        if not self._use_live_api:
            return

        if time.time() < self._token_expires_at:
            return  # Token is still valid.

        self._logger.info("dynamics_token_refresh_started")

        def _do_refresh() -> None:
            if not _HTTPX_AVAILABLE or httpx is None:
                return

            refresh_client: httpx.Client = httpx.Client(timeout=self._config.timeout)
            try:
                response: httpx.Response = refresh_client.post(
                    self._token_endpoint,
                    data={
                        "grant_type": "client_credentials",
                        "client_id": self._client_id,
                        "client_secret": self._client_secret,
                        "scope": f"{self._api_url}/.default",
                    },
                )
                response.raise_for_status()
                token_data: dict[str, Any] = response.json()
                self._access_token = token_data["access_token"]
                expires_in = int(token_data.get("expires_in", 3600))
                self._token_expires_at = time.time() + expires_in - 60

                # Update the main client's Authorization header.
                if self._http_client is not None:
                    self._http_client.headers["Authorization"] = (
                        f"Bearer {self._access_token}"
                    )

                self._logger.info("dynamics_token_refreshed")
            except httpx.HTTPError as http_exc:
                raise ConnConnectionError(
                    message="Failed to refresh Dynamics 365 OAuth token",
                    erp_type=self._config.erp_type,
                    original_error=http_exc,
                ) from http_exc
            finally:
                refresh_client.close()

        self._retry_with_backoff(
            _do_refresh,
            operation_name="dynamics_token_refresh",
        )

    # -- Private Helpers: Live API Calls -----------------------------------

    def _fetch_live_metadata(self) -> dict[str, Any] | None:
        """Fetch and parse the OData $metadata EDMX document from live API.

        Caches the parsed result in ``self._cached_edmx`` to avoid
        repeated $metadata requests within the same session.

        Returns:
            Dictionary keyed by EntityType name → property list, or
            ``None`` if the request fails.
        """
        if self._cached_edmx is not None:
            return self._cached_edmx

        if self._http_client is None:
            return None

        try:
            metadata_url = f"{self._api_url}{_D365_DATA_API_PATH}/$metadata"

            def _do_fetch() -> str:
                response: httpx.Response = self._http_client.get(metadata_url)
                response.raise_for_status()
                return response.text

            raw_xml: str = self._retry_with_backoff(
                _do_fetch,
                operation_name="dynamics_fetch_metadata",
            )
            self._cached_edmx = self._parse_odata_metadata(raw_xml)
            return self._cached_edmx

        except Exception as exc:
            self._logger.warning(
                "dynamics_live_metadata_failed",
                error=str(exc),
                error_type=type(exc).__name__,
            )
            return None

    def _fetch_live_entity_attributes(
        self,
        entity_name: str,
    ) -> list[ColumnMetadata] | None:
        """Fetch attribute metadata for a specific entity from the live API.

        Calls ``EntityDefinitions(LogicalName='{entity}')/Attributes`` to
        obtain rich attribute definitions including data types, max lengths,
        precision, and display names.

        **Privacy (C-001):** Only the ``EntityDefinitions/Attributes``
        metadata endpoint is queried — no entity data is accessed.

        Args:
            entity_name: Dynamics 365 entity logical name.

        Returns:
            List of :class:`ColumnMetadata` instances, or ``None`` if the
            live API call fails (triggering fallback to offline metadata).
        """
        if self._http_client is None:
            return None

        try:
            attributes_url = (
                f"{self._api_url}{_D365_DATA_API_PATH}/"
                f"EntityDefinitions(LogicalName='{entity_name}')/Attributes"
            )

            def _do_fetch_attrs() -> dict[str, Any]:
                response: httpx.Response = self._http_client.get(attributes_url)
                response.raise_for_status()
                return response.json()

            data: dict[str, Any] = self._retry_with_backoff(
                _do_fetch_attrs,
                operation_name=f"dynamics_fetch_attributes_{entity_name}",
            )

            attributes: list[dict[str, Any]] = data.get("value", [])
            result: list[ColumnMetadata] = []

            for idx, attr in enumerate(attributes, start=1):
                logical_name: str = attr.get("LogicalName", "")
                attr_type: str = attr.get("AttributeType", "String")

                # Map Dynamics AttributeType to OData EDM type string.
                odata_type = self._dynamics_attr_type_to_edm(attr_type)
                standard_type = ODATA_TYPE_MAPPING.get(odata_type, "VARCHAR")

                result.append(
                    ColumnMetadata(
                        column_name=logical_name,
                        native_type=odata_type,
                        standard_type=standard_type,
                        max_length=attr.get("MaxLength"),
                        precision=attr.get("Precision"),
                        scale=attr.get("Scale"),
                        is_nullable=not attr.get("RequiredLevel", {}).get(
                            "Value", "None",
                        ) == "ApplicationRequired",
                        is_primary_key=attr.get("IsPrimaryId", False),
                        is_auto_increment=attr.get("IsAutoNumberAttribute", False),
                        ordinal_position=idx,
                        description=attr.get("Description", {}).get(
                            "UserLocalizedLabel", {},
                        ).get("Label", ""),
                    ),
                )

            return result if result else None

        except Exception as exc:
            self._logger.warning(
                "dynamics_live_attributes_failed",
                entity_name=entity_name,
                error=str(exc),
                error_type=type(exc).__name__,
            )
            return None

    def _fetch_live_relationships(
        self,
        filter_entity: str | None = None,
    ) -> list[RelationshipMetadata] | None:
        """Fetch relationship metadata from the live Dynamics 365 API.

        For each known entity (or just *filter_entity* if specified),
        queries ``OneToManyRelationships`` and ``ManyToOneRelationships``
        endpoints.

        **Privacy (C-001):** Only relationship metadata endpoints are
        queried — no entity data is accessed.

        Args:
            filter_entity: If provided, only relationships involving this
                entity are fetched.

        Returns:
            List of :class:`RelationshipMetadata`, or ``None`` if the
            live API is unavailable.
        """
        if self._http_client is None:
            return None

        target_entities: list[str]
        if filter_entity:
            target_entities = [filter_entity]
        else:
            target_entities = [
                name
                for entities in DYNAMICS_MODULE_ENTITIES.values()
                for name in entities
            ]

        result: list[RelationshipMetadata] = []

        for entity_name in target_entities:
            # Fetch ManyToOne relationships (child → parent).
            m2o_rels = self._fetch_entity_relationship_type(
                entity_name,
                "ManyToOneRelationships",
            )
            for rel in m2o_rels:
                result.append(
                    RelationshipMetadata(
                        constraint_name=rel.get("SchemaName", ""),
                        source_table=rel.get("ReferencingEntity", entity_name),
                        source_column=rel.get("ReferencingAttribute", ""),
                        target_table=rel.get("ReferencedEntity", ""),
                        target_column=rel.get("ReferencedAttribute", ""),
                        relationship_type="MANY_TO_ONE",
                    ),
                )

            # Fetch OneToMany relationships (parent → children).
            o2m_rels = self._fetch_entity_relationship_type(
                entity_name,
                "OneToManyRelationships",
            )
            for rel in o2m_rels:
                result.append(
                    RelationshipMetadata(
                        constraint_name=rel.get("SchemaName", ""),
                        source_table=rel.get("ReferencingEntity", ""),
                        source_column=rel.get("ReferencingAttribute", ""),
                        target_table=rel.get("ReferencedEntity", entity_name),
                        target_column=rel.get("ReferencedAttribute", ""),
                        relationship_type="ONE_TO_MANY",
                    ),
                )

        if not result:
            return None

        return result

    def _fetch_entity_relationship_type(
        self,
        entity_name: str,
        relationship_type: str,
    ) -> list[dict[str, Any]]:
        """Fetch a specific relationship type for an entity from the live API.

        Args:
            entity_name: Entity logical name.
            relationship_type: ``"OneToManyRelationships"`` or
                ``"ManyToOneRelationships"``.

        Returns:
            List of relationship definition dictionaries from the Web API.
        """
        if self._http_client is None:
            return []

        try:
            url = (
                f"{self._api_url}{_D365_DATA_API_PATH}/"
                f"EntityDefinitions(LogicalName='{entity_name}')/"
                f"{relationship_type}"
            )

            def _do_fetch_rels() -> dict[str, Any]:
                response: httpx.Response = self._http_client.get(url)
                response.raise_for_status()
                return response.json()

            data: dict[str, Any] = self._retry_with_backoff(
                _do_fetch_rels,
                operation_name=f"dynamics_fetch_{relationship_type}_{entity_name}",
            )
            return data.get("value", [])

        except Exception as exc:
            self._logger.debug(
                "dynamics_fetch_relationship_type_failed",
                entity_name=entity_name,
                relationship_type=relationship_type,
                error=str(exc),
            )
            return []

    # -- Private Helpers: XML Parsing --------------------------------------

    def _parse_odata_metadata(self, metadata_xml: str) -> dict[str, Any]:
        """Parse an OData EDMX ``$metadata`` XML document.

        Extracts EntityType definitions with their Property elements,
        Key elements, and NavigationProperty elements from the EDMX
        XML structure.

        Args:
            metadata_xml: Raw XML string from the ``$metadata`` endpoint.

        Returns:
            Dictionary keyed by EntityType name → dict with keys:
            ``"properties"`` (list of property dicts), ``"keys"``
            (list of key property names), ``"description"`` (str),
            ``"navigation_properties"`` (list of nav property dicts).
        """
        entities: dict[str, Any] = {}

        try:
            root: ET.Element = ET.fromstring(metadata_xml)  # noqa: S314

            # Find all EntityType elements within the EDMX Schema.
            for entity_type in root.findall(".//edm:EntityType", _EDMX_NAMESPACES):
                name: str = entity_type.get("Name", "")
                if not name:
                    continue

                # Extract Key element to determine primary key properties.
                keys: list[str] = []
                key_element: ET.Element | None = entity_type.find(
                    "edm:Key",
                    _EDMX_NAMESPACES,
                )
                if key_element is not None:
                    for prop_ref in key_element.findall(
                        "edm:PropertyRef",
                        _EDMX_NAMESPACES,
                    ):
                        key_name = prop_ref.get("Name", "")
                        if key_name:
                            keys.append(key_name)

                # Extract Property elements for column metadata.
                properties: list[dict[str, Any]] = []
                for prop in entity_type.findall("edm:Property", _EDMX_NAMESPACES):
                    prop_name: str = prop.get("Name", "")
                    prop_type: str = prop.get("Type", "Edm.String")
                    nullable_str: str = prop.get("Nullable", "true")

                    # Access additional attributes via attrib dict for
                    # MaxLength, Precision, Scale when present.
                    prop_attribs: dict[str, str] = prop.attrib
                    max_length_str: str | None = prop_attribs.get("MaxLength")
                    precision_str: str | None = prop_attribs.get("Precision")
                    scale_str: str | None = prop_attribs.get("Scale")

                    properties.append({
                        "name": prop_name,
                        "type": prop_type,
                        "nullable": nullable_str.lower() != "false",
                        "is_key": prop_name in keys,
                        "max_length": (
                            int(max_length_str)
                            if max_length_str and max_length_str.isdigit()
                            else None
                        ),
                        "precision": (
                            int(precision_str)
                            if precision_str and precision_str.isdigit()
                            else None
                        ),
                        "scale": (
                            int(scale_str)
                            if scale_str and scale_str.isdigit()
                            else None
                        ),
                    })

                # Extract NavigationProperty elements for relationships.
                nav_properties: list[dict[str, str]] = []
                for nav_prop in entity_type.findall(
                    "edm:NavigationProperty",
                    _EDMX_NAMESPACES,
                ):
                    nav_name: str = nav_prop.get("Name", "")
                    nav_type: str = nav_prop.get("Type", "")

                    # Check for Annotation elements that provide descriptions.
                    annotation_text: str = ""
                    annotation: ET.Element | None = nav_prop.find(
                        "edm:Annotation",
                        _EDMX_NAMESPACES,
                    )
                    if annotation is not None:
                        # Use the element's tag for type checking and text
                        # for extracting annotation string values.
                        _ = annotation.tag  # Verify correct element type.
                        if annotation.text:
                            annotation_text = annotation.text

                    nav_properties.append({
                        "name": nav_name,
                        "type": nav_type,
                        "annotation": annotation_text,
                    })

                # Determine entity description from Annotation on EntityType.
                entity_desc: str = ""
                entity_annotation: ET.Element | None = entity_type.find(
                    "edm:Annotation",
                    _EDMX_NAMESPACES,
                )
                if entity_annotation is not None and entity_annotation.text:
                    entity_desc = entity_annotation.text

                entities[name] = {
                    "properties": properties,
                    "keys": keys,
                    "description": entity_desc,
                    "navigation_properties": nav_properties,
                }

        except ET.ParseError as exc:
            self._logger.error(
                "odata_metadata_parse_error",
                error=str(exc),
            )

        return entities

    # -- Private Helpers: Utility ------------------------------------------

    def _classify_module(self, entity_name: str) -> ERPModule | None:
        """Determine the ERP module for a Dynamics 365 entity.

        Looks up the entity name in :data:`DYNAMICS_MODULE_ENTITIES` to
        find which module it belongs to.

        Args:
            entity_name: Dynamics 365 entity logical name.

        Returns:
            Matching :class:`ERPModule` or ``None`` if the entity is not
            mapped to any supported module.
        """
        for module_key, entities in DYNAMICS_MODULE_ENTITIES.items():
            if entity_name in entities:
                return ERPModule(module_key)
        return None

    def _build_generic_columns(self, entity_name: str) -> list[ColumnMetadata]:
        """Return a minimal generic attribute set for entities without cached metadata.

        Provides a baseline column set (RecId primary key) so that schema
        discovery always returns at least one column per entity.

        Args:
            entity_name: Dynamics 365 entity logical name (used for logging).

        Returns:
            A list containing the ``RecId`` key column.
        """
        self._logger.debug(
            "dynamics_generic_columns_generated",
            entity_name=entity_name,
        )
        return [
            ColumnMetadata(
                column_name="RecId",
                native_type="Edm.Int64",
                standard_type="BIGINT",
                is_nullable=False,
                is_primary_key=True,
                ordinal_position=1,
                description="Record ID (auto-generated)",
            ),
        ]

    @staticmethod
    def _dynamics_attr_type_to_edm(attr_type: str) -> str:
        """Map Dynamics 365 AttributeType values to OData EDM type strings.

        The ``EntityDefinitions/Attributes`` endpoint returns a
        ``AttributeType`` string (e.g. ``"String"``, ``"Integer"``,
        ``"Money"``) that needs to be mapped to the corresponding
        ``Edm.*`` type used in the ``$metadata`` document.

        Args:
            attr_type: Dynamics 365 ``AttributeType`` value.

        Returns:
            Corresponding ``Edm.*`` type string.
        """
        mapping: dict[str, str] = {
            "String": "Edm.String",
            "Memo": "Edm.String",
            "Integer": "Edm.Int32",
            "BigInt": "Edm.Int64",
            "Decimal": "Edm.Decimal",
            "Double": "Edm.Double",
            "Money": "Edm.Decimal",
            "Boolean": "Edm.Boolean",
            "DateTime": "Edm.DateTimeOffset",
            "Uniqueidentifier": "Edm.Guid",
            "Lookup": "Edm.Guid",
            "Picklist": "Edm.Int32",
            "State": "Edm.Int32",
            "Status": "Edm.Int32",
            "EntityName": "Edm.String",
            "Virtual": "Edm.String",
            "ManagedProperty": "Edm.Boolean",
            "Owner": "Edm.Guid",
            "Customer": "Edm.Guid",
        }
        return mapping.get(attr_type, "Edm.String")
