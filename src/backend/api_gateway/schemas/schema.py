"""Pydantic v2 request/response validation models for the ERP schema discovery domain.

This module defines the data models used by the API Gateway to validate incoming
requests and serialize outgoing responses for ERP schema discovery and browsing
endpoints:

    - POST /api/v1/schemas/discover — Initiate schema discovery from an ERP system
    - GET  /api/v1/schemas/{id}     — Retrieve a discovered schema definition

The module enforces:
    - Constraint C-001: Only schema metadata flows through the system; no raw
      production data is accessed or stored.
    - Constraint C-005: Initial release limited to four ERP modules (Financial
      Accounting, Human Resources, Sales & Distribution, Material Management).

Supported ERP systems: SAP, Oracle E-Business Suite, Microsoft Dynamics 365,
and generic legacy systems via JDBC.

Typical usage::

    from src.backend.api_gateway.schemas.schema import (
        SchemaDiscoveryRequest,
        SchemaDefinition,
        ERPType,
        ERPModule,
    )

    # Validate an incoming discovery request
    request = SchemaDiscoveryRequest(
        erp_type=ERPType.SAP,
        connection_params=ConnectionParams(host="erp.example.com", port=3300, ...),
        modules=[ERPModule.FINANCIAL_ACCOUNTING],
    )
"""

from __future__ import annotations

from datetime import datetime  # noqa: TC003 — required at runtime by Pydantic field resolution
from enum import StrEnum

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator


# ---------------------------------------------------------------------------
# Enumeration Types
# ---------------------------------------------------------------------------


class ERPType(StrEnum):
    """Supported ERP system types for schema discovery.

    Each member maps to a specific ERP platform connector within the
    Profiling Service. The string value is used in JSON payloads.

    Attributes:
        SAP: SAP ERP (RFC/BAPI connectivity).
        ORACLE_EBS: Oracle E-Business Suite (OData/JDBC connectivity).
        DYNAMICS: Microsoft Dynamics 365 (Web API/OData connectivity).
        LEGACY: Generic legacy systems accessed via JDBC connectors.
    """

    SAP = "sap"
    ORACLE_EBS = "oracle_ebs"
    DYNAMICS = "dynamics"
    LEGACY = "legacy"


class ERPModule(StrEnum):
    """ERP functional modules available for schema discovery.

    Per Constraint C-005, the initial release is limited to exactly these
    four modules. Any attempt to request modules outside this set will be
    rejected by the ``SchemaDiscoveryRequest`` field validator.

    Attributes:
        FINANCIAL_ACCOUNTING: General Ledger, Accounts Payable/Receivable,
            invoices, and payment records.
        HUMAN_RESOURCES: Employee records, payroll, benefits, and
            organizational data.
        SALES_DISTRIBUTION: Sales orders, customer master data, pricing
            conditions, and billing documents.
        MATERIAL_MANAGEMENT: Inventory, purchase orders, vendor master
            data, and goods movements.
    """

    FINANCIAL_ACCOUNTING = "financial_accounting"
    HUMAN_RESOURCES = "hr"
    SALES_DISTRIBUTION = "sales_distribution"
    MATERIAL_MANAGEMENT = "material_management"


class ColumnDataType(StrEnum):
    """Data types for columns discovered in ERP database schemas.

    These values represent the canonical column data types that the Profiling
    Service maps from native RDBMS types. The Generation Engine uses this
    information to select the correct synthesis strategy per column.

    Attributes:
        VARCHAR: Variable-length character string.
        INTEGER: Whole number (32-bit or 64-bit depending on source).
        DECIMAL: Fixed-point decimal number with precision and scale.
        DATE: Calendar date without time component.
        DATETIME: Date and time combined (without timezone).
        BOOLEAN: True/false logical value.
        TEXT: Unbounded character large object.
        BLOB: Binary large object.
        CLOB: Character large object.
        NUMERIC: Exact numeric with configurable precision.
        FLOAT: IEEE 754 floating-point number.
        TIMESTAMP: Date, time, and optional timezone information.
    """

    VARCHAR = "varchar"
    INTEGER = "integer"
    DECIMAL = "decimal"
    DATE = "date"
    DATETIME = "datetime"
    BOOLEAN = "boolean"
    TEXT = "text"
    BLOB = "blob"
    CLOB = "clob"
    NUMERIC = "numeric"
    FLOAT = "float"
    TIMESTAMP = "timestamp"


# ---------------------------------------------------------------------------
# Connection Configuration
# ---------------------------------------------------------------------------


class ConnectionParams(BaseModel):
    """Connection parameters for establishing a link to a source ERP system.

    These parameters are used exclusively by the Profiling Service to connect
    to ERP databases for schema metadata extraction. Per Constraint C-001, the
    connection is used *only* for reading catalog/dictionary metadata — no
    production data rows are accessed or stored.

    Attributes:
        host: Hostname or IP address of the ERP system or database server.
        port: Network port for the connection (1-65535).
        database: Optional database or schema name to scope discovery.
        username: Authentication username for the ERP/database connection.
        password: Authentication password (transmitted encrypted via TLS 1.3).
        connection_type: Protocol type (e.g., ``"jdbc"``, ``"odata"``,
            ``"rfc"``).
        driver: Optional JDBC driver class name or connector identifier.
        additional_params: Optional key-value pairs for driver-specific
            connection settings (e.g., ``{"ssl": "true", "timeout": "30"}``).
    """

    model_config = ConfigDict(strict=True)

    host: str = Field(
        ...,
        min_length=1,
        max_length=255,
        description="Hostname or IP address of the ERP system.",
    )
    port: int = Field(
        ...,
        gt=0,
        le=65535,
        description="Network port for the ERP connection.",
    )
    database: str | None = Field(
        default=None,
        max_length=255,
        description="Database or schema name to scope discovery.",
    )
    username: str = Field(
        ...,
        min_length=1,
        max_length=255,
        description="Authentication username for the connection.",
    )
    password: str = Field(
        ...,
        min_length=1,
        description="Authentication password (encrypted in transit via TLS 1.3).",
    )
    connection_type: str = Field(
        ...,
        min_length=1,
        max_length=50,
        description="Connection protocol: 'jdbc', 'odata', 'rfc', or 'web_api'.",
    )
    driver: str | None = Field(
        default=None,
        max_length=255,
        description="JDBC driver class name or connector identifier.",
    )
    additional_params: dict[str, str] | None = Field(
        default=None,
        description="Driver-specific key-value connection settings.",
    )

    @field_validator("connection_type")
    @classmethod
    def validate_connection_type(cls, value: str) -> str:
        """Validate that connection_type is one of the supported protocols.

        Args:
            value: The connection type string to validate.

        Returns:
            The validated connection type in lowercase.

        Raises:
            ValueError: If the connection type is not supported.
        """
        allowed_types = {"jdbc", "odata", "rfc", "web_api"}
        normalised = value.strip().lower()
        if normalised not in allowed_types:
            raise ValueError(
                f"Unsupported connection_type '{value}'. "
                f"Allowed values: {sorted(allowed_types)}"
            )
        return normalised


# ---------------------------------------------------------------------------
# Request Models
# ---------------------------------------------------------------------------


class SchemaDiscoveryRequest(BaseModel):
    """Request model for initiating ERP schema discovery.

    Submitted via ``POST /api/v1/schemas/discover``. The request specifies the
    target ERP system, connection credentials, and the functional modules to
    include in the discovery scope.

    Validation rules:
        * ``modules`` must only contain values from :class:`ERPModule` (enforced
          by Constraint C-005).
        * At least one module must be specified.
        * ``table_filter`` items must be non-empty strings when provided.

    Attributes:
        erp_type: The type of ERP system to discover.
        connection_params: Connection credentials and settings.
        modules: ERP functional modules to include in discovery scope.
        include_relationships: Whether to discover foreign key relationships.
        include_indexes: Whether to discover index definitions.
        table_filter: Optional list of specific table names to discover.
            When ``None`` or empty, all tables in the specified modules are
            discovered.
        tenant_id: Optional tenant namespace for multi-tenant isolation.
    """

    model_config = ConfigDict(strict=True)

    erp_type: ERPType = Field(
        ...,
        description="ERP system type: sap, oracle_ebs, dynamics, or legacy.",
    )
    connection_params: ConnectionParams = Field(
        ...,
        description="Connection credentials and protocol settings.",
    )
    modules: list[ERPModule] = Field(
        ...,
        min_length=1,
        description=(
            "ERP modules to include in discovery. Must be a subset of the "
            "four initial modules per C-005."
        ),
    )
    include_relationships: bool = Field(
        default=True,
        description="Discover foreign key relationships between tables.",
    )
    include_indexes: bool = Field(
        default=False,
        description="Discover index definitions on tables.",
    )
    table_filter: list[str] | None = Field(
        default=None,
        description=(
            "Optional list of specific table names to discover. "
            "When omitted or empty, all tables in the specified modules "
            "are discovered."
        ),
    )
    tenant_id: str | None = Field(
        default=None,
        min_length=1,
        max_length=128,
        description="Tenant namespace identifier for multi-tenant isolation.",
    )

    @field_validator("modules")
    @classmethod
    def validate_modules_scope(cls, value: list[ERPModule]) -> list[ERPModule]:
        """Validate that requested modules conform to Constraint C-005.

        Constraint C-005 limits the initial release to exactly four ERP
        modules. This validator ensures that:
            1. No duplicate modules are present in the request.
            2. All requested modules are within the allowed C-005 scope.

        Args:
            value: The list of ERP modules from the request payload.

        Returns:
            The validated (deduplicated) list of ERP modules.

        Raises:
            ValueError: If duplicate modules are detected or a module is
                outside the C-005 scope.
        """
        allowed_modules = {
            ERPModule.FINANCIAL_ACCOUNTING,
            ERPModule.HUMAN_RESOURCES,
            ERPModule.SALES_DISTRIBUTION,
            ERPModule.MATERIAL_MANAGEMENT,
        }

        # Check for duplicates
        seen: set[ERPModule] = set()
        for module in value:
            if module in seen:
                raise ValueError(
                    f"Duplicate module detected: '{module.value}'. "
                    "Each module may only appear once."
                )
            seen.add(module)

        # Verify all modules are within C-005 scope
        invalid_modules = seen - allowed_modules
        if invalid_modules:
            invalid_names = [m.value for m in invalid_modules]
            allowed_names = sorted(m.value for m in allowed_modules)
            raise ValueError(
                f"Module(s) {invalid_names} are not available in the initial "
                f"release (C-005). Allowed modules: {allowed_names}"
            )

        return value

    @field_validator("table_filter")
    @classmethod
    def validate_table_filter(
        cls, value: list[str] | None
    ) -> list[str] | None:
        """Validate that table filter entries are non-empty and unique.

        Args:
            value: Optional list of table name strings.

        Returns:
            The validated table filter list, or ``None`` if not provided.

        Raises:
            ValueError: If any table name is empty or whitespace-only,
                or if duplicate table names are detected.
        """
        if value is None:
            return value

        cleaned: list[str] = []
        seen_names: set[str] = set()
        for table_name in value:
            stripped = table_name.strip()
            if not stripped:
                raise ValueError(
                    "Table filter entries must be non-empty strings."
                )
            lower_name = stripped.lower()
            if lower_name in seen_names:
                raise ValueError(
                    f"Duplicate table name in table_filter: '{stripped}'."
                )
            seen_names.add(lower_name)
            cleaned.append(stripped)

        return cleaned


# ---------------------------------------------------------------------------
# Response / Definition Models
# ---------------------------------------------------------------------------


class ColumnDefinition(BaseModel):
    """Definition of a single column within a discovered ERP table.

    Captures the column metadata extracted by the Profiling Service without
    accessing actual row data (Constraint C-001). This metadata drives the
    Generation Engine's column-level synthesis strategy selection.

    Attributes:
        name: Column name as defined in the source schema.
        data_type: Canonical data type mapped from the source RDBMS type.
        max_length: Maximum character length for string columns, or ``None``
            for non-string types.
        precision: Total number of digits for numeric columns.
        scale: Number of digits after the decimal point for numeric columns.
        nullable: Whether the column accepts NULL values.
        primary_key: Whether the column is part of the table's primary key.
        default_value: Default value expression if defined, or ``None``.
        description: Optional column description or comment from the catalog.
    """

    model_config = ConfigDict(from_attributes=True)

    name: str = Field(
        ...,
        min_length=1,
        max_length=255,
        description="Column name as defined in the source schema.",
    )
    data_type: ColumnDataType = Field(
        ...,
        description="Canonical data type mapped from the source RDBMS type.",
    )
    max_length: int | None = Field(
        default=None,
        ge=1,
        description="Maximum character length for string-type columns.",
    )
    precision: int | None = Field(
        default=None,
        ge=1,
        le=38,
        description="Total digit count for numeric columns.",
    )
    scale: int | None = Field(
        default=None,
        ge=0,
        le=38,
        description="Digits after the decimal point for numeric columns.",
    )
    nullable: bool = Field(
        default=True,
        description="Whether the column accepts NULL values.",
    )
    primary_key: bool = Field(
        default=False,
        description="Whether this column is part of the primary key.",
    )
    default_value: str | None = Field(
        default=None,
        max_length=1024,
        description="Default value expression from the catalog.",
    )
    description: str | None = Field(
        default=None,
        max_length=2000,
        description="Column description or catalog comment.",
    )

    @model_validator(mode="after")
    def validate_precision_scale(self) -> ColumnDefinition:
        """Ensure scale does not exceed precision for numeric columns.

        Returns:
            The validated ``ColumnDefinition`` instance.

        Raises:
            ValueError: If ``scale`` is greater than ``precision`` when both
                are provided.
        """
        if (
            self.precision is not None
            and self.scale is not None
            and self.scale > self.precision
        ):
            raise ValueError(
                f"Column '{self.name}': scale ({self.scale}) cannot exceed "
                f"precision ({self.precision})."
            )
        return self


class RelationshipDefinition(BaseModel):
    """Definition of a foreign key relationship between two ERP tables.

    Relationships are critical for the Generation Engine's referential
    integrity enforcement — they determine the order in which tables are
    generated and ensure that foreign key values always reference valid
    primary key records.

    Attributes:
        name: Constraint name as defined in the source schema.
        source_table: Name of the referencing (child) table.
        source_column: Column in the source table holding the foreign key.
        target_table: Name of the referenced (parent) table.
        target_column: Column in the target table being referenced (typically
            its primary key).
        relationship_type: Cardinality type (e.g., ``"one_to_many"``,
            ``"many_to_one"``, ``"one_to_one"``, ``"many_to_many"``).
        on_delete: Optional referential action on delete (e.g., ``"CASCADE"``,
            ``"SET NULL"``, ``"RESTRICT"``).
    """

    model_config = ConfigDict(from_attributes=True)

    name: str = Field(
        ...,
        min_length=1,
        max_length=255,
        description="Constraint name from the source schema.",
    )
    source_table: str = Field(
        ...,
        min_length=1,
        max_length=255,
        description="Referencing (child) table name.",
    )
    source_column: str = Field(
        ...,
        min_length=1,
        max_length=255,
        description="Foreign key column in the source table.",
    )
    target_table: str = Field(
        ...,
        min_length=1,
        max_length=255,
        description="Referenced (parent) table name.",
    )
    target_column: str = Field(
        ...,
        min_length=1,
        max_length=255,
        description="Primary key column in the target table.",
    )
    relationship_type: str = Field(
        ...,
        min_length=1,
        max_length=50,
        description=(
            "Cardinality type: 'one_to_many', 'many_to_one', "
            "'one_to_one', or 'many_to_many'."
        ),
    )
    on_delete: str | None = Field(
        default=None,
        max_length=50,
        description=(
            "Referential action on delete: 'CASCADE', 'SET NULL', "
            "'RESTRICT', or 'NO ACTION'."
        ),
    )

    @field_validator("relationship_type")
    @classmethod
    def validate_relationship_type(cls, value: str) -> str:
        """Validate the relationship cardinality type.

        Args:
            value: The relationship type string.

        Returns:
            The validated relationship type in lowercase.

        Raises:
            ValueError: If the relationship type is not recognised.
        """
        allowed_types = {
            "one_to_many",
            "many_to_one",
            "one_to_one",
            "many_to_many",
        }
        normalised = value.strip().lower()
        if normalised not in allowed_types:
            raise ValueError(
                f"Unsupported relationship_type '{value}'. "
                f"Allowed values: {sorted(allowed_types)}"
            )
        return normalised

    @field_validator("on_delete")
    @classmethod
    def validate_on_delete(cls, value: str | None) -> str | None:
        """Validate the referential delete action.

        Args:
            value: The on_delete action string, or ``None``.

        Returns:
            The validated on_delete action in uppercase, or ``None``.

        Raises:
            ValueError: If the on_delete action is not recognised.
        """
        if value is None:
            return value
        allowed_actions = {"CASCADE", "SET NULL", "RESTRICT", "NO ACTION"}
        normalised = value.strip().upper()
        if normalised not in allowed_actions:
            raise ValueError(
                f"Unsupported on_delete action '{value}'. "
                f"Allowed values: {sorted(allowed_actions)}"
            )
        return normalised


class TableDefinition(BaseModel):
    """Definition of a single table within a discovered ERP schema.

    Aggregates column definitions and metadata for one table, including
    its assignment to an ERP functional module. The Generation Engine
    uses this information to determine generation order (via primary keys
    and relationships) and to configure per-column synthesis strategies.

    Attributes:
        name: Table name as defined in the source schema.
        schema_name: Optional database schema or namespace containing the
            table (e.g., ``"dbo"``, ``"public"``).
        columns: Ordered list of column definitions for this table.
        primary_keys: List of column names forming the table's primary key.
        row_count: Approximate row count in the source table, used for
            calibrating generation volume. ``None`` if the count could not
            be determined without accessing data.
        erp_module: The ERP functional module this table belongs to.
        description: Optional table description or comment from the catalog.
    """

    model_config = ConfigDict(from_attributes=True)

    name: str = Field(
        ...,
        min_length=1,
        max_length=255,
        description="Table name as defined in the source schema.",
    )
    schema_name: str | None = Field(
        default=None,
        max_length=255,
        description="Database schema or namespace containing the table.",
    )
    columns: list[ColumnDefinition] = Field(
        default_factory=list,
        description="Ordered list of column definitions for this table.",
    )
    primary_keys: list[str] = Field(
        default_factory=list,
        description="Column names forming the table's primary key.",
    )
    row_count: int | None = Field(
        default=None,
        ge=0,
        description=(
            "Approximate row count from the source table. "
            "None if unavailable without accessing data."
        ),
    )
    erp_module: ERPModule | None = Field(
        default=None,
        description="ERP functional module this table belongs to.",
    )
    description: str | None = Field(
        default=None,
        max_length=2000,
        description="Table description or catalog comment.",
    )

    @model_validator(mode="after")
    def validate_primary_keys_in_columns(self) -> TableDefinition:
        """Verify that all primary key column names exist in the column list.

        Returns:
            The validated ``TableDefinition`` instance.

        Raises:
            ValueError: If a primary key references a column not present
                in the ``columns`` list.
        """
        if self.columns and self.primary_keys:
            column_names = {col.name for col in self.columns}
            for pk_col in self.primary_keys:
                if pk_col not in column_names:
                    raise ValueError(
                        f"Primary key column '{pk_col}' not found in the "
                        f"columns list for table '{self.name}'. "
                        f"Available columns: {sorted(column_names)}"
                    )
        return self


class SchemaDefinition(BaseModel):
    """Complete schema definition produced by ERP schema discovery.

    Returned by ``GET /api/v1/schemas/{id}`` after a successful discovery
    operation. Contains all tables, columns, and relationships extracted
    from the source ERP system's metadata catalog.

    This model uses ``from_attributes=True`` to support direct serialization
    from MongoDB document objects via PyMongo.

    Attributes:
        schema_id: Unique identifier for this schema definition.
        erp_type: The ERP system type that was discovered.
        erp_modules: ERP functional modules included in the discovery.
        tables: List of table definitions with their columns.
        relationships: List of foreign key relationships between tables.
        total_tables: Total number of tables discovered.
        total_relationships: Total number of relationships discovered.
        discovered_at: Timestamp when the schema discovery was performed.
        status: Discovery status (e.g., ``"completed"``, ``"in_progress"``,
            ``"failed"``).
        tenant_id: Optional tenant namespace for multi-tenant isolation.
    """

    model_config = ConfigDict(from_attributes=True)

    schema_id: str = Field(
        ...,
        min_length=1,
        description="Unique identifier for this schema definition.",
    )
    erp_type: ERPType = Field(
        ...,
        description="ERP system type that was discovered.",
    )
    erp_modules: list[ERPModule] = Field(
        default_factory=list,
        description="ERP modules included in this discovery.",
    )
    tables: list[TableDefinition] = Field(
        default_factory=list,
        description="Discovered table definitions with column metadata.",
    )
    relationships: list[RelationshipDefinition] = Field(
        default_factory=list,
        description="Foreign key relationships between discovered tables.",
    )
    total_tables: int = Field(
        default=0,
        ge=0,
        description="Total number of tables discovered.",
    )
    total_relationships: int = Field(
        default=0,
        ge=0,
        description="Total number of relationships discovered.",
    )
    discovered_at: datetime = Field(
        ...,
        description="ISO 8601 timestamp of when discovery was performed.",
    )
    status: str = Field(
        default="completed",
        min_length=1,
        max_length=50,
        description=(
            "Discovery status: 'pending', 'in_progress', 'completed', "
            "or 'failed'."
        ),
    )
    tenant_id: str | None = Field(
        default=None,
        min_length=1,
        max_length=128,
        description="Tenant namespace for multi-tenant isolation.",
    )

    @field_validator("status")
    @classmethod
    def validate_status(cls, value: str) -> str:
        """Validate the discovery status value.

        Args:
            value: The status string to validate.

        Returns:
            The validated status in lowercase.

        Raises:
            ValueError: If the status is not one of the allowed values.
        """
        allowed_statuses = {"pending", "in_progress", "completed", "failed"}
        normalised = value.strip().lower()
        if normalised not in allowed_statuses:
            raise ValueError(
                f"Invalid status '{value}'. "
                f"Allowed values: {sorted(allowed_statuses)}"
            )
        return normalised

    @model_validator(mode="after")
    def validate_totals_consistency(self) -> SchemaDefinition:
        """Ensure total counts are consistent with the actual list lengths.

        If ``total_tables`` or ``total_relationships`` are explicitly set to
        non-zero values but do not match the list lengths, the totals are
        automatically corrected to reflect the actual data. This provides a
        self-healing mechanism for responses constructed from MongoDB
        documents that may have stale counts.

        Returns:
            The validated ``SchemaDefinition`` instance with corrected totals.
        """
        actual_tables = len(self.tables)
        actual_relationships = len(self.relationships)

        # Auto-correct totals to match actual list lengths when lists are
        # populated. When lists are empty but totals are non-zero, it may
        # indicate a summary-only response, so we leave them as-is.
        if actual_tables > 0:
            self.total_tables = actual_tables
        if actual_relationships > 0:
            self.total_relationships = actual_relationships

        return self
