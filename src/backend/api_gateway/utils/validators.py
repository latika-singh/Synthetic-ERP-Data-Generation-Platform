"""Shared validation helpers for the API Gateway service.

Provides reusable validation functions used across all route handlers and
middleware in the API Gateway. Covers resource identifier validation (UUID v4,
MongoDB ObjectId), domain-specific value validation (ERP modules, generation
methods, output formats, ERP systems, database targets, cloud providers),
request parameter sanitization for XSS/injection prevention, pagination
parameter validation, email format checking, sort-field allow-listing, and
multi-tenant identifier validation.

All functions include comprehensive type hints and Google-style docstrings in
compliance with coding standard R-013.

Constraint References:
    C-005: Initial release limited to four ERP modules — Financial Accounting,
           Human Resources, Sales & Distribution, Material Management.
    R-007: Multi-tenant isolation — every request must be tenant-scoped.
    R-013: PEP 8 via Ruff, Google-style docstrings, type hints on all functions.
"""

from __future__ import annotations

import html
import re
import uuid
from typing import Any

from bson import ObjectId
from bson.errors import InvalidId


# ---------------------------------------------------------------------------
# Module-level constants — frozensets for O(1) membership checks
# ---------------------------------------------------------------------------

VALID_ERP_MODULES: frozenset[str] = frozenset(
    {
        "financial_accounting",
        "human_resources",
        "sales_distribution",
        "material_management",
    }
)
"""Valid ERP modules for the initial release per constraint C-005.

Members:
    financial_accounting: General Ledger entries, invoices, payments.
    human_resources: Employee records, payroll, benefits.
    sales_distribution: Orders, customers, pricing.
    material_management: Inventory, purchase orders, vendors.
"""

VALID_GENERATION_METHODS: frozenset[str] = frozenset(
    {
        "ai_ml",
        "rules_based",
        "statistical",
        "masking",
    }
)
"""Supported synthetic data generation methods.

Members:
    ai_ml: GAN / VAE-based generation via PyTorch / TensorFlow.
    rules_based: Business rules engine for constraint-based generation.
    statistical: Distribution-based synthesis via SciPy / NumPy.
    masking: Intelligent data masking with privacy preservation.
"""

VALID_OUTPUT_FORMATS: frozenset[str] = frozenset(
    {
        "sql",
        "csv",
        "json",
        "parquet",
    }
)
"""Supported multi-format export output types.

Members:
    sql: SQL INSERT / COPY statements.
    csv: Comma-separated values with configurable delimiters.
    json: JSON / JSONL documents.
    parquet: Apache Parquet columnar format.
"""

VALID_ERP_SYSTEMS: frozenset[str] = frozenset(
    {
        "sap",
        "oracle_ebs",
        "dynamics_365",
        "legacy",
    }
)
"""Supported source ERP system types for schema discovery.

Members:
    sap: SAP ERP via RFC / BAPI connectivity.
    oracle_ebs: Oracle E-Business Suite via OData / JDBC.
    dynamics_365: Microsoft Dynamics 365 via Web API / OData.
    legacy: Generic JDBC-accessible legacy systems.
"""

VALID_DATABASE_TARGETS: frozenset[str] = frozenset(
    {
        "postgresql",
        "oracle",
        "sqlserver",
        "sap_hana",
    }
)
"""Supported JDBC provisioning target databases.

Members:
    postgresql: PostgreSQL 12.x-16.x.
    oracle: Oracle Database 19c-23ai.
    sqlserver: SQL Server 2019-2022.
    sap_hana: SAP HANA 2.0 SPS 07+.
"""

VALID_CLOUD_PROVIDERS: frozenset[str] = frozenset(
    {
        "aws_s3",
        "azure_blob",
        "gcp_gcs",
    }
)
"""Supported cloud storage export providers.

Members:
    aws_s3: Amazon Web Services S3.
    azure_blob: Microsoft Azure Blob Storage.
    gcp_gcs: Google Cloud Platform Cloud Storage.
"""

# ---------------------------------------------------------------------------
# Pre-compiled regex patterns (compiled once at module load for performance)
# ---------------------------------------------------------------------------

_EMAIL_PATTERN: re.Pattern[str] = re.compile(
    r"^[a-zA-Z0-9._%+\-]+@[a-zA-Z0-9.\-]+\.[a-zA-Z]{2,}$"
)
"""RFC 5322 simplified email pattern — intentionally kept practical rather
than exhaustive to cover real-world enterprise email addresses."""

_TENANT_ID_PATTERN: re.Pattern[str] = re.compile(
    r"^[a-zA-Z0-9][a-zA-Z0-9\-]{1,62}[a-zA-Z0-9]$"
)
"""Tenant ID format: 3-64 alphanumeric characters and hyphens, must start
and end with an alphanumeric character."""

_CONTROL_CHAR_PATTERN: re.Pattern[str] = re.compile(
    r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]"
)
"""Matches ASCII control characters (excluding ``\\t``, ``\\n``, ``\\r``)
that should be stripped from user input."""


# ---------------------------------------------------------------------------
# Identifier validators
# ---------------------------------------------------------------------------


def validate_uuid(value: str) -> bool:
    """Validate that *value* is a well-formed UUID version 4 string.

    Resource identifiers such as ``job_id``, ``template_id``, ``schema_id``,
    and ``profile_id`` are expected to be UUID v4.  This helper provides a
    single, canonical check used by every route handler that accepts a UUID
    path or query parameter.

    Args:
        value: The string to validate.

    Returns:
        ``True`` if *value* is a valid UUID v4, ``False`` otherwise.

    Example:
        >>> validate_uuid("550e8400-e29b-41d4-a716-446655440000")
        True
        >>> validate_uuid("not-a-uuid")
        False
    """
    if not value or not isinstance(value, str):
        return False
    try:
        parsed = uuid.UUID(value, version=4)
        # Ensure the string round-trips cleanly (guards against mixed-case
        # or otherwise syntactically-valid-but-non-canonical inputs).
        return str(parsed) == value.lower().strip()
    except (ValueError, AttributeError):
        return False


def validate_object_id(value: str) -> bool:
    """Validate that *value* is a well-formed 24-character hex MongoDB ObjectId.

    MongoDB document references (e.g. ``_id`` fields returned from PyMongo)
    must be valid BSON ObjectIds.  This centralises the check so route
    handlers and service layers do not duplicate parsing logic.

    Args:
        value: The string to validate.

    Returns:
        ``True`` if *value* is a valid MongoDB ObjectId, ``False`` otherwise.

    Example:
        >>> validate_object_id("507f1f77bcf86cd799439011")
        True
        >>> validate_object_id("invalid-id")
        False
    """
    if not value or not isinstance(value, str):
        return False
    try:
        if not ObjectId.is_valid(value):
            return False
        # Construct and verify round-trip to guard against edge cases.
        ObjectId(value)
        return True
    except (InvalidId, TypeError):
        return False


# ---------------------------------------------------------------------------
# Domain-value validators
# ---------------------------------------------------------------------------


def validate_erp_module(module: str) -> bool:
    """Validate that *module* identifies one of the four initial-release ERP modules.

    Per constraint **C-005**, the initial release is limited to:

    * ``financial_accounting`` — Financial Accounting (GL entries, invoices, payments)
    * ``human_resources`` — Human Resources (employee records, payroll, benefits)
    * ``sales_distribution`` — Sales & Distribution (orders, customers, pricing)
    * ``material_management`` — Material Management (inventory, POs, vendors)

    The input is normalised to lowercase with spaces replaced by underscores
    so that ``"Financial Accounting"`` and ``"financial_accounting"`` are both
    accepted.

    Args:
        module: The ERP module identifier to validate.

    Returns:
        ``True`` if the normalised value matches one of the four valid
        modules, ``False`` otherwise.

    Example:
        >>> validate_erp_module("financial_accounting")
        True
        >>> validate_erp_module("Financial Accounting")
        True
        >>> validate_erp_module("production_planning")
        False
    """
    if not module or not isinstance(module, str):
        return False
    normalised = module.strip().lower().replace(" ", "_").replace("-", "_")
    return normalised in VALID_ERP_MODULES


def validate_generation_method(method: str) -> bool:
    """Validate that *method* is a supported data generation strategy.

    The platform supports four generation methods:

    * ``ai_ml`` — GAN / VAE-based generation (PyTorch / TensorFlow)
    * ``rules_based`` — Business rules constraint engine
    * ``statistical`` — Statistical distribution synthesis (SciPy / NumPy)
    * ``masking`` — Intelligent data masking

    Args:
        method: The generation method identifier to validate.

    Returns:
        ``True`` if the normalised value is a supported method, ``False``
        otherwise.

    Example:
        >>> validate_generation_method("ai_ml")
        True
        >>> validate_generation_method("deep_learning")
        False
    """
    if not method or not isinstance(method, str):
        return False
    normalised = method.strip().lower().replace(" ", "_").replace("-", "_")
    return normalised in VALID_GENERATION_METHODS


def validate_output_format(format_type: str) -> bool:
    """Validate that *format_type* is a supported export output format.

    Supported formats:

    * ``sql`` — SQL INSERT / COPY statements
    * ``csv`` — Comma-separated values
    * ``json`` — JSON / JSONL documents
    * ``parquet`` — Apache Parquet columnar format

    Args:
        format_type: The output format identifier to validate.

    Returns:
        ``True`` if the normalised value is a supported format, ``False``
        otherwise.

    Example:
        >>> validate_output_format("parquet")
        True
        >>> validate_output_format("xml")
        False
    """
    if not format_type or not isinstance(format_type, str):
        return False
    normalised = format_type.strip().lower()
    return normalised in VALID_OUTPUT_FORMATS


def validate_erp_system(system: str) -> bool:
    """Validate that *system* is a supported source ERP system type.

    Supported ERP systems:

    * ``sap`` — SAP ERP (RFC / BAPI)
    * ``oracle_ebs`` — Oracle E-Business Suite (OData / JDBC)
    * ``dynamics_365`` — Microsoft Dynamics 365 (Web API / OData)
    * ``legacy`` — Generic JDBC-accessible legacy systems

    Args:
        system: The ERP system identifier to validate.

    Returns:
        ``True`` if the normalised value is a supported ERP system, ``False``
        otherwise.

    Example:
        >>> validate_erp_system("sap")
        True
        >>> validate_erp_system("salesforce")
        False
    """
    if not system or not isinstance(system, str):
        return False
    normalised = system.strip().lower().replace(" ", "_").replace("-", "_")
    return normalised in VALID_ERP_SYSTEMS


def validate_database_target(target: str) -> bool:
    """Validate that *target* is a supported JDBC provisioning database.

    Supported targets:

    * ``postgresql`` — PostgreSQL 12.x-16.x
    * ``oracle`` — Oracle Database 19c-23ai
    * ``sqlserver`` — SQL Server 2019-2022
    * ``sap_hana`` — SAP HANA 2.0 SPS 07+

    Args:
        target: The database target identifier to validate.

    Returns:
        ``True`` if the normalised value is a supported database target,
        ``False`` otherwise.

    Example:
        >>> validate_database_target("postgresql")
        True
        >>> validate_database_target("mysql")
        False
    """
    if not target or not isinstance(target, str):
        return False
    normalised = target.strip().lower().replace(" ", "_").replace("-", "_")
    return normalised in VALID_DATABASE_TARGETS


def validate_cloud_provider(provider: str) -> bool:
    """Validate that *provider* is a supported cloud storage export provider.

    Supported providers:

    * ``aws_s3`` — Amazon Web Services S3
    * ``azure_blob`` — Microsoft Azure Blob Storage
    * ``gcp_gcs`` — Google Cloud Platform Cloud Storage

    Args:
        provider: The cloud provider identifier to validate.

    Returns:
        ``True`` if the normalised value is a supported cloud provider,
        ``False`` otherwise.

    Example:
        >>> validate_cloud_provider("aws_s3")
        True
        >>> validate_cloud_provider("digitalocean_spaces")
        False
    """
    if not provider or not isinstance(provider, str):
        return False
    normalised = provider.strip().lower().replace(" ", "_").replace("-", "_")
    return normalised in VALID_CLOUD_PROVIDERS


# ---------------------------------------------------------------------------
# Input sanitisation
# ---------------------------------------------------------------------------


def sanitize_string(value: str, max_length: int = 1000) -> str:
    """Sanitise a user-supplied string for safe downstream processing.

    The function applies the following transformations **in order**:

    1. Strip leading / trailing whitespace.
    2. Remove ASCII control characters (null bytes, BEL, etc.) while
       preserving tabs, newlines, and carriage returns.
    3. HTML-escape dangerous characters (``<``, ``>``, ``&``, ``"``, ``'``)
       to prevent cross-site scripting (XSS) attacks.
    4. Truncate to *max_length* characters to prevent payload inflation.

    Args:
        value: The raw input string to sanitise.
        max_length: Maximum allowed length of the output string.  Defaults
            to ``1000``.

    Returns:
        The sanitised string, safe for rendering and storage.

    Example:
        >>> sanitize_string("  <script>alert('xss')</script>  ")
        "&lt;script&gt;alert(&#x27;xss&#x27;)&lt;/script&gt;"
        >>> sanitize_string("a" * 2000, max_length=10)
        'aaaaaaaaaa'
    """
    if not isinstance(value, str):
        return ""
    # 1. Strip whitespace
    cleaned = value.strip()
    # 2. Remove control characters (keep \t \n \r)
    cleaned = re.sub(_CONTROL_CHAR_PATTERN, "", cleaned)
    # 3. HTML-escape to neutralise XSS payloads
    cleaned = html.escape(cleaned, quote=True)
    # 4. Truncate to max_length
    if max_length > 0:
        cleaned = cleaned[:max_length]
    return cleaned


# ---------------------------------------------------------------------------
# Pagination & query helpers
# ---------------------------------------------------------------------------


def validate_page_size(
    value: Any,
    default: int = 20,
    max_size: int = 100,
) -> int:
    """Validate and normalise a page-size query parameter.

    Handles the diverse types that query-string values may arrive as (``str``,
    ``int``, ``float``, ``None``, etc.) and clamps the result to
    ``[1, max_size]``.

    Args:
        value: The raw page-size value from the request query string.
        default: Value to return when *value* is ``None`` or cannot be
            converted to an integer.  Defaults to ``20``.
        max_size: Upper bound for the returned page size.  Defaults to
            ``100``.

    Returns:
        An integer in the range ``[1, max_size]``.

    Example:
        >>> validate_page_size("50")
        50
        >>> validate_page_size(None)
        20
        >>> validate_page_size(999, max_size=100)
        100
        >>> validate_page_size(-5)
        1
    """
    if value is None:
        return default
    try:
        size = int(value)
    except (ValueError, TypeError):
        return default
    # Clamp to [1, max_size]
    return max(1, min(size, max_size))


def validate_email(email: str) -> bool:
    """Validate that *email* is a syntactically plausible email address.

    Uses a practical regex pattern that covers real-world enterprise email
    addresses.  This is **not** a full RFC 5322 parser; it is intended as a
    quick format gate for API request validation.

    Args:
        email: The email address string to validate.

    Returns:
        ``True`` if *email* matches the simplified email pattern, ``False``
        otherwise.

    Example:
        >>> validate_email("user@example.com")
        True
        >>> validate_email("not-an-email")
        False
        >>> validate_email("")
        False
    """
    if not email or not isinstance(email, str):
        return False
    cleaned = email.strip()
    if not cleaned:
        return False
    return re.match(_EMAIL_PATTERN, cleaned) is not None


def validate_sort_field(field: str, allowed_fields: frozenset[str]) -> bool:
    """Validate that *field* is in the set of allowed sort fields.

    Prevents arbitrary field injection in MongoDB ``sort()`` queries by
    restricting the sort field to a caller-supplied allow-list.

    Args:
        field: The sort-field name supplied by the client.
        allowed_fields: The set of field names that are valid sort targets
            for the calling endpoint.

    Returns:
        ``True`` if *field* is present in *allowed_fields*, ``False``
        otherwise.

    Example:
        >>> allowed = frozenset({"created_at", "name", "status"})
        >>> validate_sort_field("created_at", allowed)
        True
        >>> validate_sort_field("$where", allowed)
        False
    """
    if not field or not isinstance(field, str):
        return False
    if not allowed_fields:
        return False
    return field.strip() in allowed_fields


def validate_tenant_id(tenant_id: str) -> bool:
    """Validate that *tenant_id* conforms to the required format.

    Per rule **R-007** (multi-tenant isolation), every API request must carry
    a valid tenant identifier.  Accepted format:

    * 3-64 characters long.
    * Alphanumeric characters and hyphens only.
    * Must start and end with an alphanumeric character.

    Args:
        tenant_id: The tenant identifier to validate.

    Returns:
        ``True`` if *tenant_id* matches the required format, ``False``
        otherwise.

    Example:
        >>> validate_tenant_id("acme-corp-123")
        True
        >>> validate_tenant_id("-invalid")
        False
        >>> validate_tenant_id("ab")
        False
    """
    if not tenant_id or not isinstance(tenant_id, str):
        return False
    cleaned = tenant_id.strip()
    if not cleaned:
        return False
    return re.match(_TENANT_ID_PATTERN, cleaned) is not None
