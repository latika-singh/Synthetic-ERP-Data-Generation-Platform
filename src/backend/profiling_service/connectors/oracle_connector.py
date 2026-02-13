"""Oracle E-Business Suite JDBC schema discovery connector for the Profiling Service.

Implements :class:`OracleConnector`, a concrete subclass of
:class:`~profiling_service.connectors.base.BaseConnector` that discovers
table, column, and foreign-key relationship metadata from Oracle EBS
databases via JDBC.

**Connection Strategy:**
    Uses ``jaydebeapi`` for JDBC connectivity to Oracle databases through
    the ``ojdbc11`` JDBC driver.  When ``jaydebeapi`` is unavailable
    (e.g. air-gapped environments without JVM — Constraint C-003), the
    connector falls back to an embedded data-dictionary that returns
    well-known Oracle EBS table structures for the four supported modules.

**Privacy Guarantee (Constraint C-001):**
    All discovery operations use Oracle data-dictionary views exclusively:

    - ``ALL_TABLES``          — table existence and row-count estimates
    - ``ALL_TAB_COMMENTS``    — table descriptions
    - ``ALL_TAB_COLUMNS``     — column metadata (data types, lengths)
    - ``ALL_COL_COMMENTS``    — column descriptions
    - ``ALL_CONSTRAINTS``     — constraint definitions (PK, FK)
    - ``ALL_CONS_COLUMNS``    — constraint-column mappings

    **No** ``SELECT`` on business-data tables is ever executed.

**Supported ERP Modules (Constraint C-005):**

- Financial Accounting — GL_JE_HEADERS, AP_INVOICES_ALL,
  AR_PAYMENT_SCHEDULES_ALL
- Human Resources — PER_ALL_PEOPLE_F, PER_ALL_ASSIGNMENTS_F
- Sales & Distribution — OE_ORDER_HEADERS_ALL, OE_ORDER_LINES_ALL
- Material Management — PO_HEADERS_ALL, MTL_SYSTEM_ITEMS_B

Example::

    from profiling_service.connectors.oracle_connector import OracleConnector
    from profiling_service.connectors.base import ConnectionConfig, ERPModule

    config = ConnectionConfig(
        erp_type="oracle_ebs",
        jdbc_url="jdbc:oracle:thin:@erp-host:1521/EBSDB",
        jdbc_driver_class="oracle.jdbc.OracleDriver",
        jdbc_driver_path="/opt/jdbc/ojdbc11.jar",
        username="discovery_user",
        password="********",
        database="APPS",
    )

    with OracleConnector(config) as connector:
        tables = connector.discover_tables(module=ERPModule.FINANCIAL_ACCOUNTING)
        for table in tables:
            columns = connector.discover_columns(table.table_name, table.schema_name)
"""

from __future__ import annotations

from typing import Any


# ---------------------------------------------------------------------------
# jaydebeapi — JDBC connectivity with graceful fallback.
# In air-gapped or development environments without a JVM, the connector
# degrades to offline mode using embedded Oracle EBS metadata knowledge.
# ---------------------------------------------------------------------------

try:
    import jaydebeapi  # type: ignore[import-untyped]

    _JAYDEBEAPI_AVAILABLE: bool = True
except ImportError:
    jaydebeapi = None  # type: ignore[assignment]
    _JAYDEBEAPI_AVAILABLE = False

from profiling_service.connectors.base import (
    BaseConnector,
    ColumnMetadata,
    ConnectionConfig,
    ConnectionError,  # noqa: A004 — intentionally shadows builtin for domain clarity
    ConnectorError,
    DiscoveryError,
    ERPModule,
    RelationshipMetadata,
    TableMetadata,
)
from shared.logging.structured_logger import get_logger
from shared.middleware.circuit_breaker import circuit_breaker_decorator


# ---------------------------------------------------------------------------
# Module-level structured logger
# ---------------------------------------------------------------------------

_logger = get_logger(__name__)


# ---------------------------------------------------------------------------
# Oracle EBS Module → Table Mappings  (Constraint C-005)
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

"""Maps each :class:`ERPModule` to its canonical Oracle EBS table names.

Only tables belonging to the four initially-supported modules (C-005) are
included.  The mapping is used for module-based filtering in
:meth:`OracleConnector.discover_tables` and for the offline fallback data
dictionary.
"""


# ---------------------------------------------------------------------------
# Oracle Data-Type → Platform Standard Type Mapping
# ---------------------------------------------------------------------------

ORACLE_TYPE_MAPPING: dict[str, str] = {
    # Character / string types
    "VARCHAR2": "VARCHAR",
    "NVARCHAR2": "VARCHAR",
    "CHAR": "VARCHAR",
    "NCHAR": "VARCHAR",
    "CLOB": "TEXT",
    "NCLOB": "TEXT",
    "LONG": "TEXT",
    # Numeric types
    "NUMBER": "DECIMAL",
    "FLOAT": "FLOAT",
    "BINARY_FLOAT": "FLOAT",
    "BINARY_DOUBLE": "FLOAT",
    # Date / timestamp types
    "DATE": "TIMESTAMP",
    "TIMESTAMP": "TIMESTAMP",
    "TIMESTAMP(6)": "TIMESTAMP",
    "TIMESTAMP WITH TIME ZONE": "TIMESTAMP",
    "TIMESTAMP WITH LOCAL TIME ZONE": "TIMESTAMP",
    # Binary / LOB types
    "RAW": "BINARY",
    "BLOB": "BINARY",
    "LONG RAW": "BINARY",
    # Pseudo / special types
    "ROWID": "VARCHAR",
    "UROWID": "VARCHAR",
    "XMLTYPE": "TEXT",
    "INTERVAL YEAR TO MONTH": "VARCHAR",
    "INTERVAL DAY TO SECOND": "VARCHAR",
}

"""Maps Oracle-native column data types (as reported by ``ALL_TAB_COLUMNS``)
to platform-standard type labels used throughout the Generation Engine and
Quality Service for cross-ERP normalisation.
"""


# ---------------------------------------------------------------------------
# SQL Query Constants — Oracle Data-Dictionary Views ONLY  (C-001)
#
# Every query targets ALL_* views which expose metadata visible to the
# authenticated schema.  No business-data table is referenced.
# ---------------------------------------------------------------------------

_SQL_DISCOVER_TABLES: str = """
SELECT t.TABLE_NAME,
       t.OWNER,
       t.NUM_ROWS,
       t.LAST_ANALYZED,
       tc.TABLE_TYPE,
       tc.COMMENTS
  FROM ALL_TABLES t
  LEFT JOIN ALL_TAB_COMMENTS tc
    ON t.TABLE_NAME = tc.TABLE_NAME
   AND t.OWNER      = tc.OWNER
 WHERE t.OWNER = ?
 ORDER BY t.TABLE_NAME
"""

_SQL_DISCOVER_TABLES_FILTERED: str = """
SELECT t.TABLE_NAME,
       t.OWNER,
       t.NUM_ROWS,
       t.LAST_ANALYZED,
       tc.TABLE_TYPE,
       tc.COMMENTS
  FROM ALL_TABLES t
  LEFT JOIN ALL_TAB_COMMENTS tc
    ON t.TABLE_NAME = tc.TABLE_NAME
   AND t.OWNER      = tc.OWNER
 WHERE t.OWNER = ?
   AND t.TABLE_NAME IN ({placeholders})
 ORDER BY t.TABLE_NAME
"""

_SQL_DISCOVER_COLUMNS: str = """
SELECT c.COLUMN_NAME,
       c.DATA_TYPE,
       c.DATA_LENGTH,
       c.DATA_PRECISION,
       c.DATA_SCALE,
       c.NULLABLE,
       c.COLUMN_ID,
       c.DATA_DEFAULT,
       cc.COMMENTS
  FROM ALL_TAB_COLUMNS c
  LEFT JOIN ALL_COL_COMMENTS cc
    ON c.TABLE_NAME  = cc.TABLE_NAME
   AND c.COLUMN_NAME = cc.COLUMN_NAME
   AND c.OWNER       = cc.OWNER
 WHERE c.OWNER      = ?
   AND c.TABLE_NAME = ?
 ORDER BY c.COLUMN_ID
"""

_SQL_DISCOVER_PRIMARY_KEY_COLUMNS: str = """
SELECT acc.COLUMN_NAME
  FROM ALL_CONSTRAINTS ac
  JOIN ALL_CONS_COLUMNS acc
    ON ac.CONSTRAINT_NAME = acc.CONSTRAINT_NAME
   AND ac.OWNER           = acc.OWNER
 WHERE ac.TABLE_NAME      = ?
   AND ac.OWNER           = ?
   AND ac.CONSTRAINT_TYPE = 'P'
"""

_SQL_DISCOVER_RELATIONSHIPS: str = """
SELECT a.CONSTRAINT_NAME,
       a.TABLE_NAME   AS SOURCE_TABLE,
       ac.COLUMN_NAME AS SOURCE_COLUMN,
       b.TABLE_NAME   AS TARGET_TABLE,
       bc.COLUMN_NAME AS TARGET_COLUMN,
       a.DELETE_RULE
  FROM ALL_CONSTRAINTS a
  JOIN ALL_CONS_COLUMNS ac
    ON a.CONSTRAINT_NAME = ac.CONSTRAINT_NAME
   AND a.OWNER           = ac.OWNER
  JOIN ALL_CONSTRAINTS b
    ON a.R_CONSTRAINT_NAME = b.CONSTRAINT_NAME
   AND a.R_OWNER           = b.OWNER
  JOIN ALL_CONS_COLUMNS bc
    ON b.CONSTRAINT_NAME = bc.CONSTRAINT_NAME
   AND b.OWNER           = bc.OWNER
   AND ac.POSITION       = bc.POSITION
 WHERE a.CONSTRAINT_TYPE = 'R'
   AND a.OWNER           = ?
 ORDER BY a.TABLE_NAME, a.CONSTRAINT_NAME
"""


# ---------------------------------------------------------------------------
# Offline Oracle EBS Data-Dictionary  (Fallback for air-gapped / dev envs)
# ---------------------------------------------------------------------------

_ORACLE_DD_COLUMNS: dict[str, list[dict[str, Any]]] = {
    # --- Financial Accounting -------------------------------------------------
    "GL_JE_HEADERS": [
        {"name": "JE_HEADER_ID", "type": "NUMBER", "length": 15, "precision": 15, "scale": 0, "nullable": False, "key": True, "desc": "Journal Entry Header ID"},
        {"name": "JE_BATCH_ID", "type": "NUMBER", "length": 15, "precision": 15, "scale": 0, "nullable": False, "key": False, "desc": "Journal Entry Batch ID"},
        {"name": "LEDGER_ID", "type": "NUMBER", "length": 15, "precision": 15, "scale": 0, "nullable": False, "key": False, "desc": "Ledger ID"},
        {"name": "JE_CATEGORY", "type": "VARCHAR2", "length": 25, "precision": None, "scale": None, "nullable": True, "key": False, "desc": "Journal Entry Category"},
        {"name": "JE_SOURCE", "type": "VARCHAR2", "length": 25, "precision": None, "scale": None, "nullable": True, "key": False, "desc": "Journal Entry Source"},
        {"name": "PERIOD_NAME", "type": "VARCHAR2", "length": 15, "precision": None, "scale": None, "nullable": True, "key": False, "desc": "Accounting Period"},
        {"name": "STATUS", "type": "VARCHAR2", "length": 1, "precision": None, "scale": None, "nullable": True, "key": False, "desc": "Posting Status"},
        {"name": "CURRENCY_CODE", "type": "VARCHAR2", "length": 15, "precision": None, "scale": None, "nullable": True, "key": False, "desc": "Currency Code"},
        {"name": "CREATION_DATE", "type": "DATE", "length": None, "precision": None, "scale": None, "nullable": True, "key": False, "desc": "Creation Date"},
        {"name": "CREATED_BY", "type": "NUMBER", "length": 15, "precision": 15, "scale": 0, "nullable": True, "key": False, "desc": "Created By User ID"},
    ],
    "AP_INVOICES_ALL": [
        {"name": "INVOICE_ID", "type": "NUMBER", "length": 15, "precision": 15, "scale": 0, "nullable": False, "key": True, "desc": "Invoice Identifier"},
        {"name": "INVOICE_NUM", "type": "VARCHAR2", "length": 50, "precision": None, "scale": None, "nullable": False, "key": False, "desc": "Invoice Number"},
        {"name": "INVOICE_TYPE_LOOKUP_CODE", "type": "VARCHAR2", "length": 25, "precision": None, "scale": None, "nullable": True, "key": False, "desc": "Invoice Type"},
        {"name": "INVOICE_DATE", "type": "DATE", "length": None, "precision": None, "scale": None, "nullable": True, "key": False, "desc": "Invoice Date"},
        {"name": "VENDOR_ID", "type": "NUMBER", "length": 15, "precision": 15, "scale": 0, "nullable": True, "key": False, "desc": "Vendor Identifier"},
        {"name": "INVOICE_AMOUNT", "type": "NUMBER", "length": None, "precision": 15, "scale": 2, "nullable": True, "key": False, "desc": "Invoice Amount"},
        {"name": "INVOICE_CURRENCY_CODE", "type": "VARCHAR2", "length": 15, "precision": None, "scale": None, "nullable": True, "key": False, "desc": "Invoice Currency"},
        {"name": "PAYMENT_STATUS_FLAG", "type": "VARCHAR2", "length": 1, "precision": None, "scale": None, "nullable": True, "key": False, "desc": "Payment Status"},
        {"name": "ORG_ID", "type": "NUMBER", "length": 15, "precision": 15, "scale": 0, "nullable": True, "key": False, "desc": "Operating Unit"},
        {"name": "CREATION_DATE", "type": "DATE", "length": None, "precision": None, "scale": None, "nullable": True, "key": False, "desc": "Creation Date"},
    ],
    "AR_PAYMENT_SCHEDULES_ALL": [
        {"name": "PAYMENT_SCHEDULE_ID", "type": "NUMBER", "length": 15, "precision": 15, "scale": 0, "nullable": False, "key": True, "desc": "Payment Schedule ID"},
        {"name": "CUSTOMER_ID", "type": "NUMBER", "length": 15, "precision": 15, "scale": 0, "nullable": True, "key": False, "desc": "Customer Identifier"},
        {"name": "CUSTOMER_SITE_USE_ID", "type": "NUMBER", "length": 15, "precision": 15, "scale": 0, "nullable": True, "key": False, "desc": "Customer Site Use ID"},
        {"name": "CLASS", "type": "VARCHAR2", "length": 20, "precision": None, "scale": None, "nullable": True, "key": False, "desc": "Transaction Class"},
        {"name": "DUE_DATE", "type": "DATE", "length": None, "precision": None, "scale": None, "nullable": True, "key": False, "desc": "Due Date"},
        {"name": "AMOUNT_DUE_ORIGINAL", "type": "NUMBER", "length": None, "precision": 15, "scale": 2, "nullable": True, "key": False, "desc": "Original Amount Due"},
        {"name": "AMOUNT_DUE_REMAINING", "type": "NUMBER", "length": None, "precision": 15, "scale": 2, "nullable": True, "key": False, "desc": "Remaining Amount Due"},
        {"name": "INVOICE_CURRENCY_CODE", "type": "VARCHAR2", "length": 15, "precision": None, "scale": None, "nullable": True, "key": False, "desc": "Currency Code"},
        {"name": "STATUS", "type": "VARCHAR2", "length": 30, "precision": None, "scale": None, "nullable": True, "key": False, "desc": "Schedule Status"},
        {"name": "ORG_ID", "type": "NUMBER", "length": 15, "precision": 15, "scale": 0, "nullable": True, "key": False, "desc": "Operating Unit"},
    ],
    # --- Human Resources ------------------------------------------------------
    "PER_ALL_PEOPLE_F": [
        {"name": "PERSON_ID", "type": "NUMBER", "length": 15, "precision": 15, "scale": 0, "nullable": False, "key": True, "desc": "Person ID"},
        {"name": "EFFECTIVE_START_DATE", "type": "DATE", "length": None, "precision": None, "scale": None, "nullable": False, "key": True, "desc": "Effective Start Date"},
        {"name": "EFFECTIVE_END_DATE", "type": "DATE", "length": None, "precision": None, "scale": None, "nullable": False, "key": True, "desc": "Effective End Date"},
        {"name": "EMPLOYEE_NUMBER", "type": "VARCHAR2", "length": 30, "precision": None, "scale": None, "nullable": True, "key": False, "desc": "Employee Number"},
        {"name": "BUSINESS_GROUP_ID", "type": "NUMBER", "length": 15, "precision": 15, "scale": 0, "nullable": False, "key": False, "desc": "Business Group ID"},
        {"name": "PERSON_TYPE_ID", "type": "NUMBER", "length": 15, "precision": 15, "scale": 0, "nullable": True, "key": False, "desc": "Person Type"},
        {"name": "CURRENT_EMPLOYEE_FLAG", "type": "VARCHAR2", "length": 30, "precision": None, "scale": None, "nullable": True, "key": False, "desc": "Current Employee Flag"},
        {"name": "CREATION_DATE", "type": "DATE", "length": None, "precision": None, "scale": None, "nullable": True, "key": False, "desc": "Creation Date"},
    ],
    "PER_ALL_ASSIGNMENTS_F": [
        {"name": "ASSIGNMENT_ID", "type": "NUMBER", "length": 15, "precision": 15, "scale": 0, "nullable": False, "key": True, "desc": "Assignment ID"},
        {"name": "PERSON_ID", "type": "NUMBER", "length": 15, "precision": 15, "scale": 0, "nullable": False, "key": False, "desc": "Person Identifier"},
        {"name": "EFFECTIVE_START_DATE", "type": "DATE", "length": None, "precision": None, "scale": None, "nullable": False, "key": True, "desc": "Effective Start Date"},
        {"name": "EFFECTIVE_END_DATE", "type": "DATE", "length": None, "precision": None, "scale": None, "nullable": False, "key": True, "desc": "Effective End Date"},
        {"name": "ORGANIZATION_ID", "type": "NUMBER", "length": 15, "precision": 15, "scale": 0, "nullable": True, "key": False, "desc": "Organisation ID"},
        {"name": "JOB_ID", "type": "NUMBER", "length": 15, "precision": 15, "scale": 0, "nullable": True, "key": False, "desc": "Job ID"},
        {"name": "GRADE_ID", "type": "NUMBER", "length": 15, "precision": 15, "scale": 0, "nullable": True, "key": False, "desc": "Grade ID"},
        {"name": "ASSIGNMENT_STATUS_TYPE_ID", "type": "NUMBER", "length": 15, "precision": 15, "scale": 0, "nullable": True, "key": False, "desc": "Assignment Status Type"},
        {"name": "PRIMARY_FLAG", "type": "VARCHAR2", "length": 1, "precision": None, "scale": None, "nullable": True, "key": False, "desc": "Primary Assignment Flag"},
        {"name": "BUSINESS_GROUP_ID", "type": "NUMBER", "length": 15, "precision": 15, "scale": 0, "nullable": False, "key": False, "desc": "Business Group ID"},
    ],
    # --- Sales & Distribution -------------------------------------------------
    "OE_ORDER_HEADERS_ALL": [
        {"name": "HEADER_ID", "type": "NUMBER", "length": 15, "precision": 15, "scale": 0, "nullable": False, "key": True, "desc": "Order Header ID"},
        {"name": "ORDER_NUMBER", "type": "NUMBER", "length": 15, "precision": 15, "scale": 0, "nullable": False, "key": False, "desc": "Sales Order Number"},
        {"name": "ORDER_TYPE_ID", "type": "NUMBER", "length": 15, "precision": 15, "scale": 0, "nullable": True, "key": False, "desc": "Order Type ID"},
        {"name": "SOLD_TO_ORG_ID", "type": "NUMBER", "length": 15, "precision": 15, "scale": 0, "nullable": True, "key": False, "desc": "Sold To Organisation"},
        {"name": "TRANSACTIONAL_CURR_CODE", "type": "VARCHAR2", "length": 15, "precision": None, "scale": None, "nullable": True, "key": False, "desc": "Transaction Currency"},
        {"name": "FLOW_STATUS_CODE", "type": "VARCHAR2", "length": 30, "precision": None, "scale": None, "nullable": True, "key": False, "desc": "Order Status"},
        {"name": "ORDERED_DATE", "type": "DATE", "length": None, "precision": None, "scale": None, "nullable": True, "key": False, "desc": "Order Date"},
        {"name": "CREATION_DATE", "type": "DATE", "length": None, "precision": None, "scale": None, "nullable": True, "key": False, "desc": "Creation Date"},
    ],
    "OE_ORDER_LINES_ALL": [
        {"name": "LINE_ID", "type": "NUMBER", "length": 15, "precision": 15, "scale": 0, "nullable": False, "key": True, "desc": "Order Line ID"},
        {"name": "HEADER_ID", "type": "NUMBER", "length": 15, "precision": 15, "scale": 0, "nullable": False, "key": False, "desc": "Order Header ID"},
        {"name": "LINE_NUMBER", "type": "NUMBER", "length": 15, "precision": 15, "scale": 0, "nullable": True, "key": False, "desc": "Line Number"},
        {"name": "INVENTORY_ITEM_ID", "type": "NUMBER", "length": 15, "precision": 15, "scale": 0, "nullable": True, "key": False, "desc": "Inventory Item ID"},
        {"name": "ORDERED_QUANTITY", "type": "NUMBER", "length": None, "precision": 15, "scale": 2, "nullable": True, "key": False, "desc": "Ordered Quantity"},
        {"name": "UNIT_SELLING_PRICE", "type": "NUMBER", "length": None, "precision": 15, "scale": 2, "nullable": True, "key": False, "desc": "Unit Selling Price"},
        {"name": "FLOW_STATUS_CODE", "type": "VARCHAR2", "length": 30, "precision": None, "scale": None, "nullable": True, "key": False, "desc": "Line Status"},
        {"name": "CREATION_DATE", "type": "DATE", "length": None, "precision": None, "scale": None, "nullable": True, "key": False, "desc": "Creation Date"},
    ],
    # --- Material Management --------------------------------------------------
    "PO_HEADERS_ALL": [
        {"name": "PO_HEADER_ID", "type": "NUMBER", "length": 15, "precision": 15, "scale": 0, "nullable": False, "key": True, "desc": "Purchase Order Header ID"},
        {"name": "SEGMENT1", "type": "VARCHAR2", "length": 20, "precision": None, "scale": None, "nullable": False, "key": False, "desc": "PO Number"},
        {"name": "TYPE_LOOKUP_CODE", "type": "VARCHAR2", "length": 25, "precision": None, "scale": None, "nullable": True, "key": False, "desc": "PO Type"},
        {"name": "VENDOR_ID", "type": "NUMBER", "length": 15, "precision": 15, "scale": 0, "nullable": True, "key": False, "desc": "Vendor ID"},
        {"name": "CURRENCY_CODE", "type": "VARCHAR2", "length": 15, "precision": None, "scale": None, "nullable": True, "key": False, "desc": "Currency Code"},
        {"name": "AUTHORIZATION_STATUS", "type": "VARCHAR2", "length": 25, "precision": None, "scale": None, "nullable": True, "key": False, "desc": "Approval Status"},
        {"name": "ORG_ID", "type": "NUMBER", "length": 15, "precision": 15, "scale": 0, "nullable": True, "key": False, "desc": "Operating Unit ID"},
        {"name": "CREATION_DATE", "type": "DATE", "length": None, "precision": None, "scale": None, "nullable": True, "key": False, "desc": "Creation Date"},
    ],
    "MTL_SYSTEM_ITEMS_B": [
        {"name": "INVENTORY_ITEM_ID", "type": "NUMBER", "length": 15, "precision": 15, "scale": 0, "nullable": False, "key": True, "desc": "Inventory Item ID"},
        {"name": "ORGANIZATION_ID", "type": "NUMBER", "length": 15, "precision": 15, "scale": 0, "nullable": False, "key": True, "desc": "Organisation ID"},
        {"name": "SEGMENT1", "type": "VARCHAR2", "length": 40, "precision": None, "scale": None, "nullable": True, "key": False, "desc": "Item Number"},
        {"name": "DESCRIPTION", "type": "VARCHAR2", "length": 240, "precision": None, "scale": None, "nullable": True, "key": False, "desc": "Item Description"},
        {"name": "PRIMARY_UOM_CODE", "type": "VARCHAR2", "length": 3, "precision": None, "scale": None, "nullable": True, "key": False, "desc": "Primary Unit of Measure"},
        {"name": "ITEM_TYPE", "type": "VARCHAR2", "length": 30, "precision": None, "scale": None, "nullable": True, "key": False, "desc": "Item Type"},
        {"name": "INVENTORY_ITEM_STATUS_CODE", "type": "VARCHAR2", "length": 10, "precision": None, "scale": None, "nullable": True, "key": False, "desc": "Item Status"},
        {"name": "CREATION_DATE", "type": "DATE", "length": None, "precision": None, "scale": None, "nullable": True, "key": False, "desc": "Creation Date"},
    ],
    "PO_LINES_ALL": [
        {"name": "PO_LINE_ID", "type": "NUMBER", "length": 15, "precision": 15, "scale": 0, "nullable": False, "key": True, "desc": "PO Line ID"},
        {"name": "PO_HEADER_ID", "type": "NUMBER", "length": 15, "precision": 15, "scale": 0, "nullable": False, "key": False, "desc": "PO Header ID"},
        {"name": "LINE_NUM", "type": "NUMBER", "length": 15, "precision": 15, "scale": 0, "nullable": True, "key": False, "desc": "Line Number"},
        {"name": "ITEM_ID", "type": "NUMBER", "length": 15, "precision": 15, "scale": 0, "nullable": True, "key": False, "desc": "Item Identifier"},
        {"name": "UNIT_PRICE", "type": "NUMBER", "length": None, "precision": 15, "scale": 5, "nullable": True, "key": False, "desc": "Unit Price"},
        {"name": "QUANTITY", "type": "NUMBER", "length": None, "precision": 15, "scale": 2, "nullable": True, "key": False, "desc": "Quantity Ordered"},
        {"name": "ITEM_DESCRIPTION", "type": "VARCHAR2", "length": 240, "precision": None, "scale": None, "nullable": True, "key": False, "desc": "Item Description"},
        {"name": "CREATION_DATE", "type": "DATE", "length": None, "precision": None, "scale": None, "nullable": True, "key": False, "desc": "Creation Date"},
    ],
}

# Oracle EBS table descriptions — used in offline mode and for enrichment.
_ORACLE_TABLE_DESCRIPTIONS: dict[str, str] = {
    "GL_JE_HEADERS": "General Ledger Journal Entry Headers",
    "GL_JE_LINES": "General Ledger Journal Entry Lines",
    "GL_JE_BATCHES": "General Ledger Journal Entry Batches",
    "AP_INVOICES_ALL": "Accounts Payable Invoices",
    "AP_INVOICE_LINES_ALL": "Accounts Payable Invoice Lines",
    "AP_PAYMENT_SCHEDULES_ALL": "AP Payment Schedules",
    "AR_PAYMENT_SCHEDULES_ALL": "AR Payment Schedules",
    "AR_CASH_RECEIPTS_ALL": "AR Cash Receipts",
    "PER_ALL_PEOPLE_F": "HR People (Date-tracked)",
    "PER_ALL_ASSIGNMENTS_F": "HR Assignments (Date-tracked)",
    "PER_ADDRESSES": "Person Addresses",
    "PER_JOBS": "Job Definitions",
    "PER_GRADES": "Grade Definitions",
    "PAY_ELEMENT_ENTRIES_F": "Payroll Element Entries",
    "OE_ORDER_HEADERS_ALL": "Sales Order Headers",
    "OE_ORDER_LINES_ALL": "Sales Order Lines",
    "HZ_PARTIES": "Trading Community Parties",
    "HZ_CUST_ACCOUNTS": "Customer Accounts",
    "QP_LIST_HEADERS_B": "Price List Headers",
    "QP_LIST_LINES": "Price List Lines",
    "PO_HEADERS_ALL": "Purchase Order Headers",
    "PO_LINES_ALL": "Purchase Order Lines",
    "MTL_SYSTEM_ITEMS_B": "Inventory Items",
    "MTL_ONHAND_QUANTITIES": "On-hand Inventory Quantities",
    "AP_SUPPLIERS": "Supplier Master",
    "PO_REQUISITION_HEADERS_ALL": "Purchase Requisition Headers",
}

# Well-known Oracle EBS foreign-key relationships for offline mode.
_ORACLE_RELATIONSHIPS: dict[str, list[dict[str, str]]] = {
    "GL_JE_LINES": [
        {"constraint": "GL_JE_LINES_FK1", "source_table": "GL_JE_LINES", "source_column": "JE_HEADER_ID", "target_table": "GL_JE_HEADERS", "target_column": "JE_HEADER_ID", "type": "MANY_TO_ONE"},
    ],
    "GL_JE_HEADERS": [
        {"constraint": "GL_JE_HEADERS_FK1", "source_table": "GL_JE_HEADERS", "source_column": "JE_BATCH_ID", "target_table": "GL_JE_BATCHES", "target_column": "JE_BATCH_ID", "type": "MANY_TO_ONE"},
    ],
    "AP_INVOICE_LINES_ALL": [
        {"constraint": "AP_INVOICE_LINES_FK1", "source_table": "AP_INVOICE_LINES_ALL", "source_column": "INVOICE_ID", "target_table": "AP_INVOICES_ALL", "target_column": "INVOICE_ID", "type": "MANY_TO_ONE"},
    ],
    "AP_INVOICES_ALL": [
        {"constraint": "AP_INVOICES_FK1", "source_table": "AP_INVOICES_ALL", "source_column": "VENDOR_ID", "target_table": "AP_SUPPLIERS", "target_column": "VENDOR_ID", "type": "MANY_TO_ONE"},
    ],
    "OE_ORDER_LINES_ALL": [
        {"constraint": "OE_ORDER_LINES_FK1", "source_table": "OE_ORDER_LINES_ALL", "source_column": "HEADER_ID", "target_table": "OE_ORDER_HEADERS_ALL", "target_column": "HEADER_ID", "type": "MANY_TO_ONE"},
    ],
    "OE_ORDER_HEADERS_ALL": [
        {"constraint": "OE_ORDER_HEADERS_FK1", "source_table": "OE_ORDER_HEADERS_ALL", "source_column": "SOLD_TO_ORG_ID", "target_table": "HZ_CUST_ACCOUNTS", "target_column": "CUST_ACCOUNT_ID", "type": "MANY_TO_ONE"},
    ],
    "HZ_CUST_ACCOUNTS": [
        {"constraint": "HZ_CUST_ACCOUNTS_FK1", "source_table": "HZ_CUST_ACCOUNTS", "source_column": "PARTY_ID", "target_table": "HZ_PARTIES", "target_column": "PARTY_ID", "type": "MANY_TO_ONE"},
    ],
    "PO_LINES_ALL": [
        {"constraint": "PO_LINES_FK1", "source_table": "PO_LINES_ALL", "source_column": "PO_HEADER_ID", "target_table": "PO_HEADERS_ALL", "target_column": "PO_HEADER_ID", "type": "MANY_TO_ONE"},
    ],
    "PER_ALL_ASSIGNMENTS_F": [
        {"constraint": "PER_ASSIGN_FK1", "source_table": "PER_ALL_ASSIGNMENTS_F", "source_column": "PERSON_ID", "target_table": "PER_ALL_PEOPLE_F", "target_column": "PERSON_ID", "type": "MANY_TO_ONE"},
    ],
    "MTL_ONHAND_QUANTITIES": [
        {"constraint": "MTL_ONHAND_FK1", "source_table": "MTL_ONHAND_QUANTITIES", "source_column": "INVENTORY_ITEM_ID", "target_table": "MTL_SYSTEM_ITEMS_B", "target_column": "INVENTORY_ITEM_ID", "type": "MANY_TO_ONE"},
    ],
}


# ---------------------------------------------------------------------------
# OracleConnector
# ---------------------------------------------------------------------------


class OracleConnector(BaseConnector):
    """Oracle E-Business Suite JDBC schema discovery connector.

    Discovers table metadata, column definitions, data types, and
    foreign-key relationships from Oracle EBS databases using JDBC
    (``jaydebeapi``).  When JDBC is unavailable the connector falls
    back to an embedded data-dictionary for the four supported ERP
    modules.

    **Dual-Mode Operation:**

    - **Live JDBC mode** — When ``jaydebeapi`` is installed and the
      Oracle JDBC driver is accessible, the connector executes real-time
      metadata queries against Oracle data-dictionary views.
    - **Offline / fallback mode** — When ``jaydebeapi`` is not installed
      (e.g. development or air-gapped environments), the connector uses
      an embedded catalogue of well-known Oracle EBS table structures.

    **Privacy (C-001):**
        Only Oracle data-dictionary views are queried.  No production
        data is accessed at any point.

    **Supported Modules (C-005):**
        Financial Accounting, Human Resources, Sales & Distribution,
        Material Management.

    Args:
        config: A :class:`ConnectionConfig` with ``erp_type="oracle_ebs"``
            and JDBC connection details (``jdbc_url``, ``jdbc_driver_class``,
            ``jdbc_driver_path``, ``username``, ``password``).

    Example::

        config = ConnectionConfig(
            erp_type="oracle_ebs",
            jdbc_url="jdbc:oracle:thin:@erp-host:1521/EBSDB",
            jdbc_driver_class="oracle.jdbc.OracleDriver",
            jdbc_driver_path="/opt/jdbc/ojdbc11.jar",
            username="discovery_user",
            password="********",
            database="APPS",
        )

        with OracleConnector(config) as connector:
            tables = connector.discover_tables(
                module=ERPModule.FINANCIAL_ACCOUNTING,
            )
    """

    # Default Oracle EBS application schema.
    _DEFAULT_SCHEMA: str = "APPS"

    def __init__(self, config: ConnectionConfig) -> None:
        """Initialise the Oracle EBS connector.

        Parses connection configuration, resolves the JDBC URL (building
        it from ``host``/``port``/``database`` when ``jdbc_url`` is not
        provided), and prepares internal state.

        Args:
            config: Validated connection configuration.  Credentials are
                stored internally but never written to log output.
        """
        super().__init__(config)
        self._logger = get_logger(__name__)

        # Resolve JDBC connection parameters.
        self._jdbc_url: str = config.jdbc_url or self._build_jdbc_url(config)
        self._driver_class: str = config.jdbc_driver_class or "oracle.jdbc.OracleDriver"
        self._driver_path: str = config.jdbc_driver_path or ""
        self._username: str = config.username or ""
        self._password: str = config.password or ""
        self._schema_filter: str | None = config.database

        # JDBC connection handle — typed as Optional[Any] because
        # jaydebeapi.Connection is only available when the library is present.
        self._connection: Any | None = None

        # Tracks whether the connector operates in live JDBC or offline mode.
        self._use_live_jdbc: bool = False

        self._logger.info(
            "oracle_connector_initialised",
            jdbc_url=self._jdbc_url,
            driver_class=self._driver_class,
            has_jaydebeapi=_JAYDEBEAPI_AVAILABLE,
        )

    # ------------------------------------------------------------------
    # BaseConnector Interface — Public Methods
    # ------------------------------------------------------------------

    @circuit_breaker_decorator(
        name="oracle_jdbc_connect",
        failure_threshold=5,
        recovery_timeout=30,
    )
    def connect(self) -> None:
        """Establish a JDBC connection to the Oracle EBS database.

        Uses :meth:`BaseConnector._retry_with_backoff` for automatic
        retry with exponential backoff on transient failures.  Falls
        back to offline data-dictionary mode when ``jaydebeapi`` is not
        installed.

        Raises:
            ConnectionError: If the connection cannot be established
                after exhausting all retry attempts.
        """
        if self.connected:
            self._logger.debug("oracle_already_connected")
            return

        if not _JAYDEBEAPI_AVAILABLE or jaydebeapi is None:
            # Graceful degradation to offline mode.
            self._use_live_jdbc = False
            self._connected = True
            self._logger.info(
                "oracle_connector_offline_mode",
                reason="jaydebeapi not installed — using embedded data dictionary",
            )
            return

        try:
            def _establish_jdbc_connection() -> None:
                """Inner callable executed within the retry loop."""
                driver_args: list[str] = [self._username, self._password]
                jar_path: str | None = self._driver_path if self._driver_path else None

                self._connection = jaydebeapi.connect(
                    self._driver_class,
                    self._jdbc_url,
                    driver_args,
                    jar_path,
                )

            self._retry_with_backoff(
                _establish_jdbc_connection,
                operation_name="oracle_jdbc_connect",
            )

            self._use_live_jdbc = True
            self._connected = True

            self._logger.info(
                "oracle_jdbc_connected",
                jdbc_url=self._jdbc_url,
                mode="live_jdbc",
            )
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
        """Discover Oracle EBS tables for the given schema/module.

        **Privacy (C-001):** Uses ``ALL_TABLES`` and
        ``ALL_TAB_COMMENTS`` data-dictionary views exclusively.

        When connected via live JDBC the method queries the Oracle
        data dictionary in real-time.  In offline mode it returns
        metadata from the embedded EBS catalogue.

        Args:
            schema_name: Oracle schema/owner filter (e.g. ``"APPS"``).
                Defaults to the ``database`` field from
                :class:`ConnectionConfig` or ``"APPS"``.
            module: Optional ERP module filter.  When provided, only
                tables belonging to the specified functional module are
                returned.

        Returns:
            A list of :class:`TableMetadata` instances for each
            discovered table.

        Raises:
            DiscoveryError: If metadata extraction fails.
            ConnectionError: If the connector is not connected.
        """
        self._validate_connected()

        effective_schema: str = schema_name or self._schema_filter or self._DEFAULT_SCHEMA

        # Resolve target table names from module mapping.
        if module is not None:
            module_key = module.value if isinstance(module, ERPModule) else str(module)
            target_tables: list[str] = ORACLE_EBS_MODULE_TABLES.get(module_key, [])
        else:
            target_tables = []
            for tables in ORACLE_EBS_MODULE_TABLES.values():
                target_tables.extend(tables)

        try:
            if self._use_live_jdbc and self._connection is not None:
                result = self._discover_tables_live(effective_schema, target_tables)
            else:
                result = self._discover_tables_offline(effective_schema, target_tables)

            self._logger.info(
                "oracle_tables_discovered",
                table_count=len(result),
                module=module.value if module else "all",
                schema=effective_schema,
                mode="live_jdbc" if self._use_live_jdbc else "offline",
            )
            return result

        except ConnectorError:
            raise
        except Exception as exc:
            self._logger.error(
                "oracle_discover_tables_failed",
                error=str(exc),
                error_type=type(exc).__name__,
                schema=effective_schema,
                module=module.value if module else "all",
            )
            raise DiscoveryError(
                message=f"Failed to discover Oracle EBS tables: {exc}",
                erp_type=self._config.erp_type,
                original_error=exc,
            ) from exc

    def discover_columns(
        self,
        table_name: str,
        schema_name: str | None = None,
    ) -> list[ColumnMetadata]:
        """Discover columns for an Oracle EBS table.

        **Privacy (C-001):** Uses ``ALL_TAB_COLUMNS``,
        ``ALL_COL_COMMENTS``, ``ALL_CONSTRAINTS``, and
        ``ALL_CONS_COLUMNS`` data-dictionary views exclusively.

        When connected via live JDBC the method queries the Oracle
        data dictionary in real-time to extract column metadata and
        primary-key participation.  In offline mode it returns
        metadata from the embedded EBS catalogue.

        Args:
            table_name: Physical Oracle table name (e.g.
                ``"GL_JE_HEADERS"``).
            schema_name: Optional schema/owner qualifier.  Defaults to
                the ``database`` field from :class:`ConnectionConfig`
                or ``"APPS"``.

        Returns:
            A list of :class:`ColumnMetadata` instances ordered by
            ``ordinal_position``.

        Raises:
            DiscoveryError: If column metadata extraction fails.
            ConnectionError: If the connector is not connected.
        """
        self._validate_connected()

        effective_schema: str = schema_name or self._schema_filter or self._DEFAULT_SCHEMA

        try:
            if self._use_live_jdbc and self._connection is not None:
                result = self._discover_columns_live(table_name, effective_schema)
            else:
                result = self._discover_columns_offline(table_name)

            self._logger.info(
                "oracle_columns_discovered",
                table_name=table_name,
                column_count=len(result),
                schema=effective_schema,
                mode="live_jdbc" if self._use_live_jdbc else "offline",
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
        schema_name: str | None = None,
    ) -> list[RelationshipMetadata]:
        """Discover FK relationships across Oracle EBS tables.

        **Privacy (C-001):** Uses ``ALL_CONSTRAINTS`` and
        ``ALL_CONS_COLUMNS`` data-dictionary views exclusively.

        When connected via live JDBC the method queries the Oracle
        data dictionary for all foreign-key constraints in the target
        schema.  In offline mode it returns well-known EBS relationships
        from the embedded catalogue.

        Args:
            schema_name: Oracle schema/owner filter.  Defaults to the
                ``database`` field from :class:`ConnectionConfig` or
                ``"APPS"``.

        Returns:
            A list of :class:`RelationshipMetadata` instances.

        Raises:
            DiscoveryError: If relationship discovery fails.
            ConnectionError: If the connector is not connected.
        """
        self._validate_connected()

        effective_schema: str = schema_name or self._schema_filter or self._DEFAULT_SCHEMA

        try:
            if self._use_live_jdbc and self._connection is not None:
                result = self._discover_relationships_live(effective_schema)
            else:
                result = self._discover_relationships_offline()

            self._logger.info(
                "oracle_relationships_discovered",
                relationship_count=len(result),
                schema=effective_schema,
                mode="live_jdbc" if self._use_live_jdbc else "offline",
            )
            return result

        except ConnectorError:
            raise
        except Exception as exc:
            self._logger.error(
                "oracle_discover_relationships_failed",
                error=str(exc),
                error_type=type(exc).__name__,
                schema=effective_schema,
            )
            raise DiscoveryError(
                message=f"Failed to discover Oracle EBS relationships: {exc}",
                erp_type=self._config.erp_type,
                original_error=exc,
            ) from exc

    def close(self) -> None:
        """Close the Oracle JDBC connection and release resources.

        Idempotent — safe to call multiple times.  After ``close()`` is
        called, the :attr:`connected` property returns ``False`` and
        subsequent discovery calls raise :class:`ConnectionError`.
        """
        if self._connection is not None:
            try:
                self._connection.close()
                self._logger.info(
                    "oracle_jdbc_connection_closed",
                    was_connected=self.connected,
                )
            except Exception as exc:
                self._logger.warning(
                    "oracle_close_warning",
                    error=str(exc),
                    error_type=type(exc).__name__,
                )
            finally:
                self._connection = None

        self._use_live_jdbc = False
        self._connected = False
        self._logger.info("oracle_connector_closed")

    # ------------------------------------------------------------------
    # Type Mapping Helper — Exposed in exports schema
    # ------------------------------------------------------------------

    def _map_oracle_type_to_standard(
        self,
        oracle_type: str,
        precision: int | None = None,
        scale: int | None = None,
    ) -> str:
        """Map an Oracle-native data type to a platform-standard type label.

        Performs intelligent refinement for ``NUMBER`` columns:
        - ``NUMBER(p, 0)`` with ``p <= 18`` → ``"INTEGER"``
        - ``NUMBER(p, 0)`` with ``p > 18`` → ``"BIGINT"``
        - ``NUMBER(p, s)`` with ``s > 0`` → ``"DECIMAL"``

        Args:
            oracle_type: Oracle data type string as reported by
                ``ALL_TAB_COLUMNS.DATA_TYPE`` (e.g. ``"VARCHAR2"``,
                ``"NUMBER"``, ``"DATE"``).
            precision: Numeric precision (``DATA_PRECISION``).
            scale: Numeric scale (``DATA_SCALE``).

        Returns:
            A normalised platform-standard type string (e.g.
            ``"VARCHAR"``, ``"INTEGER"``, ``"DECIMAL"``,
            ``"TIMESTAMP"``).
        """
        # Strip any length/precision suffix — e.g. "TIMESTAMP(6)" → "TIMESTAMP".
        base_type: str = oracle_type.upper().split("(")[0].strip()

        standard: str = ORACLE_TYPE_MAPPING.get(base_type, "VARCHAR")

        # Refine NUMBER to INTEGER/BIGINT when scale is zero (integer semantics).
        if base_type == "NUMBER" and scale is not None and scale == 0:
            if precision is None or precision <= 18:
                return "INTEGER"
            return "BIGINT"

        return standard

    # ------------------------------------------------------------------
    # Live JDBC Discovery — Private Implementation Methods
    # ------------------------------------------------------------------

    def _discover_tables_live(
        self,
        schema: str,
        target_tables: list[str],
    ) -> list[TableMetadata]:
        """Query ALL_TABLES and ALL_TAB_COMMENTS via JDBC for table metadata.

        Uses :meth:`_retry_with_backoff` for resilience against transient
        JDBC query failures.

        Args:
            schema: Oracle schema/owner name.
            target_tables: List of table names to filter on.  If empty,
                all tables in the schema are returned.

        Returns:
            A list of :class:`TableMetadata` from the live Oracle catalog.
        """
        def _run_query() -> list[tuple[Any, ...]]:
            if target_tables:
                placeholders = ", ".join("?" for _ in target_tables)
                sql = _SQL_DISCOVER_TABLES_FILTERED.format(placeholders=placeholders)
                params: list[Any] = [schema, *target_tables]
            else:
                sql = _SQL_DISCOVER_TABLES
                params = [schema]
            return self._execute_query(sql, params)

        rows: list[tuple[Any, ...]] = self._retry_with_backoff(
            _run_query,
            operation_name="oracle_discover_tables_query",
        )

        result: list[TableMetadata] = []
        for row in rows:
            table_name_val: str = str(row[0])
            owner_val: str = str(row[1]) if row[1] else schema
            num_rows_val: int | None = int(row[2]) if row[2] is not None else None
            last_analyzed_val = row[3]  # datetime or None
            table_type_val: str = str(row[4]) if row[4] else "TABLE"
            comments_val: str | None = str(row[5]) if row[5] else _ORACLE_TABLE_DESCRIPTIONS.get(table_name_val)

            assigned_module: ERPModule | None = self._classify_module(table_name_val)

            result.append(
                TableMetadata(
                    table_name=table_name_val,
                    schema_name=owner_val,
                    description=comments_val,
                    estimated_row_count=num_rows_val,
                    module=assigned_module,
                    table_type=table_type_val,
                    last_analyzed=last_analyzed_val,
                ),
            )

        return result

    def _discover_columns_live(
        self,
        table_name: str,
        schema: str,
    ) -> list[ColumnMetadata]:
        """Query ALL_TAB_COLUMNS, ALL_COL_COMMENTS, and PK constraints via JDBC.

        Executes two queries:
        1. Column metadata from ``ALL_TAB_COLUMNS`` + ``ALL_COL_COMMENTS``.
        2. Primary-key columns from ``ALL_CONSTRAINTS`` +
           ``ALL_CONS_COLUMNS``.

        Both queries are wrapped with :meth:`_retry_with_backoff`.

        Args:
            table_name: Physical Oracle table name.
            schema: Oracle schema/owner name.

        Returns:
            A list of :class:`ColumnMetadata` ordered by ordinal position.
        """
        # Step 1: Fetch primary-key column names for this table.
        def _fetch_pk_columns() -> set[str]:
            rows = self._execute_query(
                _SQL_DISCOVER_PRIMARY_KEY_COLUMNS,
                [table_name, schema],
            )
            return {str(r[0]).upper() for r in rows}

        pk_columns: set[str] = self._retry_with_backoff(
            _fetch_pk_columns,
            operation_name="oracle_discover_pk_columns",
        )

        # Step 2: Fetch column definitions.
        def _fetch_columns() -> list[tuple[Any, ...]]:
            return self._execute_query(
                _SQL_DISCOVER_COLUMNS,
                [schema, table_name],
            )

        rows: list[tuple[Any, ...]] = self._retry_with_backoff(
            _fetch_columns,
            operation_name="oracle_discover_columns_query",
        )

        result: list[ColumnMetadata] = []
        for row in rows:
            col_name: str = str(row[0])
            data_type: str = str(row[1]) if row[1] else "VARCHAR2"
            data_length: int | None = int(row[2]) if row[2] is not None else None
            data_precision: int | None = int(row[3]) if row[3] is not None else None
            data_scale: int | None = int(row[4]) if row[4] is not None else None
            nullable_flag: str = str(row[5]) if row[5] else "Y"
            column_id: int | None = int(row[6]) if row[6] is not None else None
            data_default: str | None = str(row[7]).strip() if row[7] is not None else None
            comments: str | None = str(row[8]) if row[8] else None

            standard_type = self._map_oracle_type_to_standard(
                data_type,
                data_precision,
                data_scale,
            )

            # Determine max_length for character types only.
            is_char_type: bool = data_type.upper().split("(")[0].strip() in {
                "VARCHAR2", "NVARCHAR2", "CHAR", "NCHAR",
            }

            result.append(
                ColumnMetadata(
                    column_name=col_name,
                    native_type=data_type,
                    standard_type=standard_type,
                    max_length=data_length if is_char_type else None,
                    precision=data_precision,
                    scale=data_scale,
                    is_nullable=(nullable_flag == "Y"),
                    is_primary_key=(col_name.upper() in pk_columns),
                    is_auto_increment=False,
                    ordinal_position=column_id,
                    description=comments,
                    default_value=data_default,
                ),
            )

        return result

    def _discover_relationships_live(
        self,
        schema: str,
    ) -> list[RelationshipMetadata]:
        """Query ALL_CONSTRAINTS and ALL_CONS_COLUMNS via JDBC for FK metadata.

        Args:
            schema: Oracle schema/owner name.

        Returns:
            A list of :class:`RelationshipMetadata` from the live catalog.
        """
        def _fetch_relationships() -> list[tuple[Any, ...]]:
            return self._execute_query(
                _SQL_DISCOVER_RELATIONSHIPS,
                [schema],
            )

        rows: list[tuple[Any, ...]] = self._retry_with_backoff(
            _fetch_relationships,
            operation_name="oracle_discover_relationships_query",
        )

        result: list[RelationshipMetadata] = []
        for row in rows:
            constraint_name: str | None = str(row[0]) if row[0] else None
            source_table: str = str(row[1])
            source_column: str = str(row[2])
            target_table: str = str(row[3])
            target_column: str = str(row[4])
            delete_rule: str | None = str(row[5]) if row[5] else None

            # Map Oracle delete-rule values to standard notation.
            on_delete: str | None = None
            if delete_rule:
                delete_rule_upper = delete_rule.upper().strip()
                if delete_rule_upper == "CASCADE":
                    on_delete = "CASCADE"
                elif delete_rule_upper in {"SET NULL", "SET_NULL"}:
                    on_delete = "SET NULL"
                elif delete_rule_upper in {"NO ACTION", "NO_ACTION", "RESTRICT"}:
                    on_delete = "NO ACTION"

            result.append(
                RelationshipMetadata(
                    constraint_name=constraint_name,
                    source_table=source_table,
                    source_column=source_column,
                    target_table=target_table,
                    target_column=target_column,
                    relationship_type="MANY_TO_ONE",
                    on_delete=on_delete,
                ),
            )

        return result

    # ------------------------------------------------------------------
    # Offline Discovery — Private Fallback Methods
    # ------------------------------------------------------------------

    def _discover_tables_offline(
        self,
        schema: str,
        target_tables: list[str],
    ) -> list[TableMetadata]:
        """Return table metadata from the embedded EBS data dictionary.

        Args:
            schema: Oracle schema name to assign (used for
                ``schema_name`` in :class:`TableMetadata`).
            target_tables: Table names to include.

        Returns:
            A list of :class:`TableMetadata` built from static knowledge.
        """
        result: list[TableMetadata] = []
        for tbl_name in target_tables:
            description = _ORACLE_TABLE_DESCRIPTIONS.get(tbl_name, "")
            assigned_module: ERPModule | None = self._classify_module(tbl_name)

            result.append(
                TableMetadata(
                    table_name=tbl_name,
                    schema_name=schema,
                    description=description,
                    estimated_row_count=None,
                    module=assigned_module,
                    table_type="TABLE",
                ),
            )

        return result

    def _discover_columns_offline(
        self,
        table_name: str,
    ) -> list[ColumnMetadata]:
        """Return column metadata from the embedded EBS data dictionary.

        Falls back to :meth:`_build_generic_columns` for tables not
        present in the offline catalogue.

        Args:
            table_name: Physical Oracle EBS table name.

        Returns:
            A list of :class:`ColumnMetadata`.
        """
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

            is_char_type: bool = col["type"] in {"VARCHAR2", "NVARCHAR2", "CHAR", "NCHAR"}

            result.append(
                ColumnMetadata(
                    column_name=col["name"],
                    native_type=col["type"],
                    standard_type=standard_type,
                    max_length=col.get("length") if is_char_type else None,
                    precision=col.get("precision"),
                    scale=col.get("scale"),
                    is_nullable=col["nullable"],
                    is_primary_key=col["key"],
                    is_auto_increment=False,
                    ordinal_position=idx,
                    description=col.get("desc"),
                ),
            )

        return result

    def _discover_relationships_offline(self) -> list[RelationshipMetadata]:
        """Return FK relationships from the embedded EBS data dictionary.

        Returns:
            A list of :class:`RelationshipMetadata` built from known
            Oracle EBS FK constraints.
        """
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
                    ),
                )

        return result

    # ------------------------------------------------------------------
    # Internal Utility Methods
    # ------------------------------------------------------------------

    def _execute_query(
        self,
        sql: str,
        params: list[Any] | None = None,
    ) -> list[tuple[Any, ...]]:
        """Execute a SQL query against the JDBC connection and return all rows.

        Manages the cursor lifecycle (create → execute → fetch → close)
        with robust error handling.  Only metadata-querying SQL against
        Oracle data-dictionary views should be passed to this method
        (Constraint C-001).

        Args:
            sql: The SQL query string using ``?`` parameter placeholders.
            params: Positional parameters for the prepared statement.

        Returns:
            A list of tuples, one per result row.

        Raises:
            DiscoveryError: If the query fails or the connection is
                unexpectedly absent.
        """
        if self._connection is None:
            raise DiscoveryError(
                message="JDBC connection is None — cannot execute query",
                erp_type=self._config.erp_type,
            )

        cursor: Any = None
        try:
            cursor = self._connection.cursor()
            if params:
                cursor.execute(sql, params)
            else:
                cursor.execute(sql)

            rows: list[tuple[Any, ...]] = cursor.fetchall()
            return rows

        except Exception as exc:
            self._logger.error(
                "oracle_query_execution_failed",
                error=str(exc),
                error_type=type(exc).__name__,
                sql_preview=sql[:120].strip(),
            )
            raise DiscoveryError(
                message=f"Oracle data-dictionary query failed: {exc}",
                erp_type=self._config.erp_type,
                original_error=exc,
            ) from exc
        finally:
            if cursor is not None:
                try:
                    cursor.close()
                except Exception as close_exc:
                    self._logger.debug(
                        "oracle_cursor_close_warning",
                        error=str(close_exc),
                    )

    def _classify_module(self, table_name: str) -> ERPModule | None:
        """Determine the ERP module for a given Oracle EBS table name.

        Args:
            table_name: Physical Oracle EBS table name.

        Returns:
            The matching :class:`ERPModule` or ``None`` if the table
            does not belong to any recognised module.
        """
        for module_key, tables in ORACLE_EBS_MODULE_TABLES.items():
            if table_name in tables:
                return ERPModule(module_key)
        return None

    def _build_generic_columns(self, table_name: str) -> list[ColumnMetadata]:
        """Return a minimal generic column set for tables absent from the offline catalogue.

        Provides a ROWID pseudo-column as a placeholder so that callers
        always receive at least one column.  When operating in live-JDBC
        mode this method is not invoked because the real catalog is
        queried instead.

        Args:
            table_name: Oracle EBS table name (logged for diagnostics).

        Returns:
            A list containing a single ``ROWID`` pseudo-column.
        """
        self._logger.debug(
            "oracle_building_generic_columns",
            table_name=table_name,
        )
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

    @staticmethod
    def _build_jdbc_url(config: ConnectionConfig) -> str:
        """Construct an Oracle JDBC thin-driver URL from host/port/database.

        When ``jdbc_url`` is explicitly provided in the config it takes
        precedence — this method is only called when ``jdbc_url`` is
        ``None`` or empty.

        Args:
            config: Connection configuration with ``host``, ``port``,
                and ``database`` fields.

        Returns:
            A JDBC URL of the form
            ``jdbc:oracle:thin:@<host>:<port>/<database>``.  Falls
            back to an empty string if host information is missing.
        """
        if not config.host:
            return ""

        port: int = config.port or 1521
        service_name: str = config.database or "ORCL"

        return f"jdbc:oracle:thin:@{config.host}:{port}/{service_name}"
