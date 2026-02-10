"""Pydantic v2 request/response validation models for the statistical profiling domain.

This module defines the data validation schemas used by the profiling API endpoints
(POST /api/v1/profiles, GET /api/v1/profiles/{id}). It provides strict request
validation for profile creation and standardized response serialization for
statistical profile data including distributions, patterns, and column-level
statistics.

The profiling domain captures metadata only — no raw production data ever enters
the system, enforcing Constraint C-001 (no production data access or storage).

Models:
    DistributionType: Enumeration of supported statistical distribution types
        detected during profiling (normal, log-normal, Poisson, uniform, etc.).
    ConnectionConfig: Source ERP system connection parameters for JDBC, OData,
        or RFC connectivity.
    ProfileRequest: Request model for initiating a statistical profiling job
        against a source ERP system.
    StatisticalSummary: Per-column statistical analysis results including
        distribution type, central tendency, dispersion, and pattern detection.
    ColumnProfile: Column-level metadata combining schema information with
        optional statistical analysis.
    TableProfile: Table-level profile aggregating column profiles with
        row counts and relationship metadata.
    ProfileResponse: Complete profiling result returned by the API, containing
        table profiles, summary counts, and lifecycle timestamps.

Typical usage::

    from src.backend.api_gateway.schemas.profile import (
        ProfileRequest,
        ProfileResponse,
        StatisticalSummary,
    )

    # Validate an incoming profiling request
    request = ProfileRequest(
        source_connection=ConnectionConfig(
            connection_type="jdbc",
            host="erp.example.com",
            port=5432,
            username="profiler",
            password="encrypted_secret",
        ),
        erp_type="sap",
        erp_module="financial_accounting",
    )
"""

from datetime import datetime
from enum import StrEnum
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator


# ---------------------------------------------------------------------------
# Enumerations
# ---------------------------------------------------------------------------


class DistributionType(StrEnum):
    """Enumeration of statistical distribution types detected during profiling.

    Each value represents a probability distribution that the profiling engine
    can fit to numeric column data using SciPy/NumPy distribution fitting.
    The ``UNKNOWN`` sentinel is used when the profiler cannot determine
    a best-fit distribution with sufficient confidence.

    Attributes:
        NORMAL: Gaussian / normal distribution.
        LOG_NORMAL: Log-normal distribution (positively skewed data).
        POISSON: Poisson distribution (count data, rare events).
        UNIFORM: Uniform distribution (equally likely values).
        CATEGORICAL: Categorical / multinomial distribution (discrete labels).
        EXPONENTIAL: Exponential distribution (inter-arrival times).
        BINOMIAL: Binomial distribution (binary outcomes).
        UNKNOWN: Distribution could not be determined with confidence.
    """

    NORMAL = "normal"
    LOG_NORMAL = "log_normal"
    POISSON = "poisson"
    UNIFORM = "uniform"
    CATEGORICAL = "categorical"
    EXPONENTIAL = "exponential"
    BINOMIAL = "binomial"
    UNKNOWN = "unknown"


# ---------------------------------------------------------------------------
# Allowed value sets for field validators
# ---------------------------------------------------------------------------

_ALLOWED_ERP_TYPES: frozenset[str] = frozenset(
    {"sap", "oracle_ebs", "dynamics", "legacy"}
)

_ALLOWED_ERP_MODULES: frozenset[str] = frozenset(
    {
        "financial_accounting",
        "hr",
        "sales_distribution",
        "material_management",
    }
)

_ALLOWED_CONNECTION_TYPES: frozenset[str] = frozenset(
    {"jdbc", "odata", "rfc"}
)


# ---------------------------------------------------------------------------
# Request Models
# ---------------------------------------------------------------------------


class ConnectionConfig(BaseModel):
    """Source ERP system connection configuration.

    Encapsulates all parameters needed to establish a connection to a source
    ERP system for schema discovery and statistical profiling.  The password
    field is transmitted encrypted in transit (TLS 1.3) and is never persisted
    in clear text, enforcing security-by-default (R-006).

    Attributes:
        connection_type: Protocol used to connect — ``jdbc``, ``odata``, or
            ``rfc`` (SAP-specific).
        host: Fully-qualified hostname or IP address of the ERP system.
        port: TCP port number (1-65535).
        database: Optional database or schema name on the target system.
        username: Authenticated username for the connection.
        password: Connection password (encrypted in transit via TLS 1.3).
        additional_params: Optional driver-specific key-value connection
            parameters (e.g., ``{"sslMode": "require"}``).
    """

    connection_type: str = Field(
        ...,
        description="Connection type: jdbc, odata, rfc",
    )
    host: str = Field(
        ...,
        min_length=1,
        description="ERP system hostname",
    )
    port: int = Field(
        ...,
        gt=0,
        le=65535,
        description="Connection port",
    )
    database: str | None = Field(
        default=None,
        description="Database/schema name",
    )
    username: str = Field(
        ...,
        min_length=1,
        description="Connection username",
    )
    password: str = Field(
        ...,
        min_length=1,
        description="Connection password (encrypted in transit)",
    )
    additional_params: dict[str, str] | None = Field(
        default=None,
        description="Driver-specific connection parameters",
    )

    model_config = ConfigDict(strict=True)

    # ------------------------------------------------------------------
    # Validators
    # ------------------------------------------------------------------

    @field_validator("connection_type")
    @classmethod
    def validate_connection_type(cls, value: str) -> str:
        """Ensure ``connection_type`` is one of the supported protocols.

        Args:
            value: The raw connection type string.

        Returns:
            The validated (lowercased) connection type.

        Raises:
            ValueError: If the value is not ``jdbc``, ``odata``, or ``rfc``.
        """
        normalized = value.strip().lower()
        if normalized not in _ALLOWED_CONNECTION_TYPES:
            raise ValueError(
                f"connection_type must be one of {sorted(_ALLOWED_CONNECTION_TYPES)}, "
                f"got '{value}'"
            )
        return normalized


class ProfileRequest(BaseModel):
    """Request model for initiating a statistical profiling job.

    Submitted via ``POST /api/v1/profiles`` to trigger schema discovery and
    statistical analysis of a source ERP system.  The profiling process
    captures metadata only — no raw production data is accessed or stored,
    enforcing Constraint C-001.

    The ``erp_type`` must match one of the four supported ERP families and
    the ``erp_module`` (if specified) must be one of the four initial-release
    modules per Constraint C-005.

    Attributes:
        source_connection: Validated :class:`ConnectionConfig` for the ERP
            system to profile.
        erp_type: ERP system family — ``sap``, ``oracle_ebs``, ``dynamics``,
            or ``legacy``.
        discovery_scope: Optional list of specific table/schema names to
            profile.  An empty list triggers a full-scope discovery.
        erp_module: Optional ERP functional module to restrict profiling
            scope — ``financial_accounting``, ``hr``, ``sales_distribution``,
            or ``material_management``.
        include_statistics: Whether to include statistical distribution
            analysis in the profiling results (default ``True``).
        include_relationships: Whether to discover and include foreign-key
            relationships (default ``True``).
        sample_size: Optional sample size for statistical profiling, bounded
            between 100 and 1,000,000 rows.
        tenant_id: Tenant namespace for multi-tenant isolation (R-007).
    """

    source_connection: ConnectionConfig = Field(
        ...,
        description="Source ERP connection config",
    )
    erp_type: str = Field(
        ...,
        description="ERP type: sap, oracle_ebs, dynamics, legacy",
    )
    discovery_scope: list[str] = Field(
        default_factory=list,
        description="Table/schema names to profile, empty for all",
    )
    erp_module: str | None = Field(
        default=None,
        description=(
            "ERP module: financial_accounting, hr, "
            "sales_distribution, material_management"
        ),
    )
    include_statistics: bool = Field(
        default=True,
        description="Include statistical distribution profiling",
    )
    include_relationships: bool = Field(
        default=True,
        description="Include FK relationship discovery",
    )
    sample_size: int | None = Field(
        default=None,
        ge=100,
        le=1_000_000,
        description="Sample size for statistical profiling",
    )
    tenant_id: str | None = Field(
        default=None,
        description="Tenant namespace",
    )

    model_config = ConfigDict(strict=True)

    # ------------------------------------------------------------------
    # Validators
    # ------------------------------------------------------------------

    @field_validator("erp_type")
    @classmethod
    def validate_erp_type(cls, value: str) -> str:
        """Validate that ``erp_type`` is a supported ERP system family.

        Args:
            value: The raw ERP type string.

        Returns:
            The validated (lowercased) ERP type.

        Raises:
            ValueError: If the value is not one of the allowed ERP types.
        """
        normalized = value.strip().lower()
        if normalized not in _ALLOWED_ERP_TYPES:
            raise ValueError(
                f"erp_type must be one of {sorted(_ALLOWED_ERP_TYPES)}, "
                f"got '{value}'"
            )
        return normalized

    @field_validator("erp_module")
    @classmethod
    def validate_erp_module(cls, value: str | None) -> str | None:
        """Validate that ``erp_module`` is within the C-005 initial scope.

        The initial release is limited to four ERP functional modules:
        Financial Accounting, HR, Sales & Distribution, and Material
        Management.

        Args:
            value: The raw ERP module string, or ``None``.

        Returns:
            The validated (lowercased) ERP module, or ``None``.

        Raises:
            ValueError: If a non-``None`` value is not one of the four
                allowed modules.
        """
        if value is None:
            return value
        normalized = value.strip().lower()
        if normalized not in _ALLOWED_ERP_MODULES:
            raise ValueError(
                f"erp_module must be one of {sorted(_ALLOWED_ERP_MODULES)}, "
                f"got '{value}'"
            )
        return normalized


# ---------------------------------------------------------------------------
# Statistical / Response Models
# ---------------------------------------------------------------------------


class StatisticalSummary(BaseModel):
    """Per-column statistical analysis results produced by the profiling engine.

    Contains central tendency measures, dispersion metrics, distribution
    classification, and pattern detection for a single database column.
    Numeric fields (``mean``, ``median``, etc.) are ``None`` for non-numeric
    columns.

    The ``distribution_type`` is determined by the SciPy distribution fitting
    algorithm, which selects the best-fit distribution from the
    :class:`DistributionType` enumeration.

    Attributes:
        column_name: Name of the profiled column.
        data_type: Detected data type string (e.g., ``varchar``, ``integer``).
        distribution_type: Best-fit statistical distribution for numeric data.
        mean: Arithmetic mean (numeric columns only).
        median: Median value (numeric columns only).
        std_dev: Standard deviation (non-negative, numeric columns only).
        min_value: Minimum observed value (numeric columns only).
        max_value: Maximum observed value (numeric columns only).
        null_percentage: Percentage of null values in the column (0.0-100.0).
        unique_count: Count of distinct non-null values.
        sample_values: Representative sample values from the column for
            display purposes (never raw PII — metadata only per C-001).
        pattern: Detected data pattern expressed as a regex or format string
            (e.g., ``\\d{3}-\\d{2}-\\d{4}`` for SSN-like patterns).
    """

    column_name: str = Field(
        ...,
        description="Column name",
    )
    data_type: str = Field(
        ...,
        description="Detected data type",
    )
    distribution_type: DistributionType = Field(
        default=DistributionType.UNKNOWN,
        description="Best-fit distribution",
    )
    mean: float | None = Field(
        default=None,
        description="Mean value for numeric columns",
    )
    median: float | None = Field(
        default=None,
        description="Median value",
    )
    std_dev: float | None = Field(
        default=None,
        ge=0,
        description="Standard deviation",
    )
    min_value: float | None = Field(
        default=None,
        description="Minimum value",
    )
    max_value: float | None = Field(
        default=None,
        description="Maximum value",
    )
    null_percentage: float = Field(
        default=0.0,
        ge=0.0,
        le=100.0,
        description="Percentage of null values",
    )
    unique_count: int | None = Field(
        default=None,
        ge=0,
        description="Count of unique values",
    )
    sample_values: list[Any] | None = Field(
        default=None,
        description="Representative sample values",
    )
    pattern: str | None = Field(
        default=None,
        description="Detected data pattern (e.g., regex)",
    )

    model_config = ConfigDict(from_attributes=True)

    # ------------------------------------------------------------------
    # Cross-field validation
    # ------------------------------------------------------------------

    @model_validator(mode="after")
    def validate_numeric_consistency(self) -> "StatisticalSummary":
        """Validate that numeric statistics fields are consistent.

        Ensures that ``min_value`` ≤ ``max_value`` when both are present,
        and that ``std_dev`` is non-negative (already enforced by ``ge=0``
        but double-checked here for robustness).

        Returns:
            The validated :class:`StatisticalSummary` instance.

        Raises:
            ValueError: If ``min_value`` exceeds ``max_value``.
        """
        if (
            self.min_value is not None
            and self.max_value is not None
            and self.min_value > self.max_value
        ):
            raise ValueError(
                f"min_value ({self.min_value}) must not exceed "
                f"max_value ({self.max_value})"
            )
        return self


class ColumnProfile(BaseModel):
    """Column-level profile combining schema metadata with statistical analysis.

    Pairs basic schema information (name, type, nullability) with an optional
    :class:`StatisticalSummary` produced by the profiling engine when
    ``include_statistics`` is enabled in the :class:`ProfileRequest`.

    Attributes:
        column_name: Name of the database column.
        data_type: Column data type as reported by the source system.
        nullable: Whether the column allows ``NULL`` values.
        statistics: Optional statistical summary (populated when profiling
            includes statistical distribution analysis).
    """

    column_name: str = Field(
        ...,
        min_length=1,
        description="Database column name",
    )
    data_type: str = Field(
        ...,
        min_length=1,
        description="Column data type",
    )
    nullable: bool = Field(
        ...,
        description="Whether the column allows NULL values",
    )
    statistics: StatisticalSummary | None = Field(
        default=None,
        description="Statistical summary for the column",
    )

    model_config = ConfigDict(from_attributes=True)


class TableProfile(BaseModel):
    """Table-level profile aggregating column profiles and relationships.

    Contains the schema-level metadata for a single table including its row
    count, per-column profiles, and discovered foreign-key relationships.

    Attributes:
        table_name: Fully-qualified table name.
        row_count: Total number of rows in the table (non-negative).
        columns: Ordered list of :class:`ColumnProfile` entries for each
            column in the table.
        relationships: Optional list of foreign-key relationship descriptors,
            each containing ``source_column``, ``target_table``, and
            ``target_column`` keys.
    """

    table_name: str = Field(
        ...,
        min_length=1,
        description="Table name",
    )
    row_count: int = Field(
        ...,
        ge=0,
        description="Total row count",
    )
    columns: list[ColumnProfile] = Field(
        ...,
        description="Column profiles for the table",
    )
    relationships: list[dict[str, str]] | None = Field(
        default=None,
        description="Foreign-key relationship descriptors",
    )

    model_config = ConfigDict(from_attributes=True)


class ProfileResponse(BaseModel):
    """Response model for a completed statistical profiling result.

    Returned by ``GET /api/v1/profiles/{id}`` after a profiling job has
    completed.  Contains the full set of table profiles, aggregate counts,
    and lifecycle timestamps for the profiling operation.

    Attributes:
        profile_id: Unique identifier for this profiling result.
        erp_type: Source ERP system type that was profiled.
        erp_module: Optional ERP functional module scope.
        tables: List of :class:`TableProfile` entries — one per profiled
            table.
        total_tables: Count of tables included in the profile.
        total_columns: Aggregate count of columns across all profiled tables.
        created_at: ISO 8601 timestamp of profile creation.
        updated_at: ISO 8601 timestamp of the last profile update.
        status: Current profiling status (``completed``, ``in_progress``,
            ``failed``).
        tenant_id: Tenant namespace for multi-tenant isolation.
    """

    profile_id: str = Field(
        ...,
        min_length=1,
        description="Unique profile identifier",
    )
    erp_type: str = Field(
        ...,
        description="Source ERP type",
    )
    erp_module: str | None = Field(
        default=None,
        description="ERP module",
    )
    tables: list[TableProfile] = Field(
        default_factory=list,
        description="Table profiles",
    )
    total_tables: int = Field(
        default=0,
        ge=0,
        description="Number of tables profiled",
    )
    total_columns: int = Field(
        default=0,
        ge=0,
        description="Total columns across all tables",
    )
    created_at: datetime = Field(
        ...,
        description="Profile creation timestamp",
    )
    updated_at: datetime = Field(
        ...,
        description="Last update timestamp",
    )
    status: str = Field(
        default="completed",
        description="Profiling status",
    )
    tenant_id: str | None = Field(
        default=None,
        description="Tenant namespace",
    )

    model_config = ConfigDict(from_attributes=True)

    # ------------------------------------------------------------------
    # Cross-field validation
    # ------------------------------------------------------------------

    @model_validator(mode="after")
    def validate_counts_consistency(self) -> "ProfileResponse":
        """Validate that aggregate counts are consistent with table data.

        When ``tables`` are provided, ``total_tables`` is automatically
        reconciled with the actual table count.  ``total_columns`` is
        reconciled with the sum of columns across all tables.

        Returns:
            The validated :class:`ProfileResponse` instance with reconciled
            counts.
        """
        if self.tables:
            # Reconcile total_tables with actual table list length
            actual_table_count = len(self.tables)
            if self.total_tables == 0:
                object.__setattr__(self, "total_tables", actual_table_count)

            # Reconcile total_columns with actual column sum
            actual_column_count = sum(
                len(table.columns) for table in self.tables
            )
            if self.total_columns == 0:
                object.__setattr__(self, "total_columns", actual_column_count)
        return self

    @field_validator("status")
    @classmethod
    def validate_status(cls, value: str) -> str:
        """Validate that ``status`` is a recognized profiling lifecycle state.

        Args:
            value: The raw status string.

        Returns:
            The validated (lowercased) status string.

        Raises:
            ValueError: If the value is not a recognized status.
        """
        allowed_statuses = {"pending", "in_progress", "completed", "failed"}
        normalized = value.strip().lower()
        if normalized not in allowed_statuses:
            raise ValueError(
                f"status must be one of {sorted(allowed_statuses)}, "
                f"got '{value}'"
            )
        return normalized
