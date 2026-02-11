"""MongoDB document model for the ``statistical_profiles`` collection.

Defines Pydantic 2.x models representing statistical metadata captured from ERP
source systems.  These models store distribution parameters (normal, log-normal,
Poisson, categorical, uniform, and more), pattern metadata (format patterns, regex
patterns), cardinality, null rates, value ranges, percentiles per column, and
correlation matrices between columns.

Per-column statistical profiles are aggregated into table-level and schema-level
statistical profiles.  The :class:`StatisticalProfileRepository` provides a CRUD
helper with **multi-tenant isolation** — every database query is tenant-scoped.

**Consumers:**

* **Generation Engine** — reads distribution parameters for accurate statistical
  synthesis and copula-based multivariate correlation preservation.
* **Quality Service** — reads profiles for fidelity validation (40 % statistical
  weight in the weighted scoring model).

**Constraint C-001 Compliance:**

This model captures **metadata and statistical summaries only** — it never stores
raw production data values.  ``sample_formats`` entries are anonymised format
exemplars, not actual data from source systems.

Usage::

    from profiling_service.models.statistical_profile import (
        StatisticalProfile,
        StatisticalProfileRepository,
    )

    repo = StatisticalProfileRepository(tenant_id="tenant-abc")
    profile_id = repo.create(profile)
    profile = repo.get_by_id(profile_id)
"""

from __future__ import annotations

import logging
import uuid
from datetime import datetime, timezone
from enum import Enum
from typing import Any, Optional, Union

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from shared.database.mongodb import (
    COLLECTION_STATISTICAL_PROFILES,
    get_collection,
)

# ---------------------------------------------------------------------------
# Module-level logger — uses standard ``logging`` (not shared.logging) to
# avoid circular dependency at the model layer.
# ---------------------------------------------------------------------------
logger: logging.Logger = logging.getLogger(__name__)


# ===================================================================
# Enumerations
# ===================================================================


class DistributionType(str, Enum):
    """Statistical distribution types supported by the profiling engine.

    Each value corresponds to a SciPy 1.12+ continuous or discrete
    distribution family used for goodness-of-fit testing and
    parameterisation during profiling.
    """

    NORMAL = "normal"
    LOG_NORMAL = "log_normal"
    POISSON = "poisson"
    EXPONENTIAL = "exponential"
    UNIFORM = "uniform"
    CATEGORICAL = "categorical"
    BINOMIAL = "binomial"
    GAMMA = "gamma"
    BETA = "beta"
    WEIBULL = "weibull"
    CUSTOM = "custom"


class ProfileStatus(str, Enum):
    """Lifecycle status of a statistical profiling job."""

    PENDING = "pending"
    IN_PROGRESS = "in_progress"
    COMPLETED = "completed"
    FAILED = "failed"
    STALE = "stale"


class DataCategory(str, Enum):
    """High-level categorisation of a column's data type."""

    NUMERIC = "numeric"
    CATEGORICAL = "categorical"
    TEMPORAL = "temporal"
    TEXT = "text"
    BOOLEAN = "boolean"
    IDENTIFIER = "identifier"


# ===================================================================
# Supporting Pydantic Models
# ===================================================================


class DistributionParameters(BaseModel):
    """Best-fit distribution and its parameters for a single column.

    The ``parameters`` dictionary holds distribution-specific key-value
    pairs.  Examples:

    * Normal: ``{"mean": 50.0, "std": 10.5}``
    * Poisson: ``{"lambda": 3.2}``
    * Categorical: ``{"categories": {"A": 0.4, "B": 0.35, "C": 0.25}}``

    Goodness-of-fit is measured via the Kolmogorov–Smirnov test (for
    continuous distributions) or the chi-square test (for categorical).
    """

    distribution_type: DistributionType
    parameters: dict[str, Any] = Field(
        default_factory=dict,
        description="Distribution-specific parameters (e.g., mean/std for normal).",
    )
    goodness_of_fit: float = Field(
        default=0.0,
        ge=0.0,
        le=1.0,
        description="KS test or chi-square p-value; higher is better fit.",
    )
    ks_statistic: Optional[float] = Field(
        default=None,
        description="Kolmogorov–Smirnov test statistic.",
    )
    chi_square_statistic: Optional[float] = Field(
        default=None,
        description="Chi-square test statistic (categorical distributions).",
    )

    model_config = ConfigDict(use_enum_values=True)


class PatternMetadata(BaseModel):
    """String/format pattern metadata for a column.

    Captures format patterns, regex patterns, common prefixes/suffixes,
    and character-class distributions.  ``sample_formats`` contains
    **anonymised format exemplars** — never actual data values (C-001).
    """

    format_pattern: Optional[str] = Field(
        default=None,
        description="Detected format pattern, e.g. 'XXXX-XXXX-XXXX'.",
    )
    regex_pattern: Optional[str] = Field(
        default=None,
        description="Regex that matches the detected pattern.",
    )
    common_prefixes: list[str] = Field(
        default_factory=list,
        description="Most common string prefixes.",
    )
    common_suffixes: list[str] = Field(
        default_factory=list,
        description="Most common string suffixes.",
    )
    average_length: Optional[float] = Field(
        default=None,
        description="Average string/value length.",
    )
    min_length: Optional[int] = Field(
        default=None,
        ge=0,
        description="Minimum observed length.",
    )
    max_length: Optional[int] = Field(
        default=None,
        ge=0,
        description="Maximum observed length.",
    )
    sample_formats: list[str] = Field(
        default_factory=list,
        description="Anonymised format samples (not actual data — C-001).",
    )
    character_classes: dict[str, float] = Field(
        default_factory=dict,
        description="Character class distribution (alpha, digit, special).",
    )


class ValueRange(BaseModel):
    """Descriptive statistics for a column's value range.

    Applicable primarily to numeric and temporal columns.  ``min_value``
    and ``max_value`` accept both numeric (float) and ISO-format date
    strings to support temporal columns.
    """

    min_value: Optional[Union[float, str]] = Field(
        default=None,
        description="Minimum observed value (numeric or date string).",
    )
    max_value: Optional[Union[float, str]] = Field(
        default=None,
        description="Maximum observed value (numeric or date string).",
    )
    mean: Optional[float] = Field(default=None, description="Arithmetic mean.")
    median: Optional[float] = Field(default=None, description="Median value.")
    mode: Optional[Union[float, str]] = Field(
        default=None,
        description="Most frequent value.",
    )
    standard_deviation: Optional[float] = Field(
        default=None,
        ge=0.0,
        description="Standard deviation.",
    )
    variance: Optional[float] = Field(
        default=None,
        ge=0.0,
        description="Variance.",
    )
    skewness: Optional[float] = Field(default=None, description="Skewness measure.")
    kurtosis: Optional[float] = Field(default=None, description="Kurtosis measure.")


class Percentiles(BaseModel):
    """Percentile breakdown for a numeric column."""

    p1: Optional[float] = Field(default=None, description="1st percentile.")
    p5: Optional[float] = Field(default=None, description="5th percentile.")
    p10: Optional[float] = Field(default=None, description="10th percentile.")
    p25: Optional[float] = Field(default=None, description="25th percentile (Q1).")
    p50: Optional[float] = Field(default=None, description="50th percentile (median).")
    p75: Optional[float] = Field(default=None, description="75th percentile (Q3).")
    p90: Optional[float] = Field(default=None, description="90th percentile.")
    p95: Optional[float] = Field(default=None, description="95th percentile.")
    p99: Optional[float] = Field(default=None, description="99th percentile.")
    iqr: Optional[float] = Field(
        default=None,
        ge=0.0,
        description="Interquartile range (Q3 − Q1).",
    )


class FrequencyDistribution(BaseModel):
    """Histogram / frequency distribution data for a column.

    For numeric columns, ``bins`` and ``counts`` describe the histogram.
    For categorical columns, ``categories`` holds value→count mappings.
    ``top_values`` lists the *N* most frequent values with counts and
    percentages.
    """

    bins: list[float] = Field(
        default_factory=list,
        description="Histogram bin edges (numeric columns).",
    )
    counts: list[int] = Field(
        default_factory=list,
        description="Histogram bin counts.",
    )
    categories: dict[str, int] = Field(
        default_factory=dict,
        description="Category value → count mapping (categorical columns).",
    )
    top_values: list[dict[str, Any]] = Field(
        default_factory=list,
        description=(
            "Top N most frequent values with counts, e.g. "
            "[{'value': 'X', 'count': 100, 'percentage': 0.15}]."
        ),
    )
    total_count: int = Field(default=0, ge=0, description="Total observation count.")


# ===================================================================
# Column / Correlation / Table Profile Models
# ===================================================================


class ColumnProfile(BaseModel):
    """Statistical profile for a single database column.

    Aggregates all statistical measures, distribution parameters, pattern
    metadata, value range, percentiles, frequency distribution, and outlier
    information for one column within an ERP table.
    """

    column_name: str = Field(..., description="Column name matching schema definition.")
    table_name: str = Field(..., description="Parent table name.")
    data_category: DataCategory = Field(
        ..., description="Detected data category."
    )
    total_count: int = Field(default=0, ge=0, description="Total non-null values.")
    null_count: int = Field(default=0, ge=0, description="Count of NULL values.")
    null_rate: float = Field(
        default=0.0,
        ge=0.0,
        le=1.0,
        description="Fraction of NULL values (0.0–1.0).",
    )
    distinct_count: int = Field(
        default=0, ge=0, description="Number of distinct values."
    )
    cardinality: float = Field(
        default=0.0,
        ge=0.0,
        le=1.0,
        description="Cardinality ratio (distinct_count / total_count).",
    )
    is_unique: bool = Field(
        default=False,
        description="Whether all non-null values are unique.",
    )
    distribution: Optional[DistributionParameters] = Field(
        default=None, description="Best-fit distribution parameters."
    )
    pattern: Optional[PatternMetadata] = Field(
        default=None, description="String/format pattern metadata."
    )
    value_range: Optional[ValueRange] = Field(
        default=None, description="Min/max/mean/median statistics."
    )
    percentiles: Optional[Percentiles] = Field(
        default=None, description="Percentile breakdown."
    )
    frequency: Optional[FrequencyDistribution] = Field(
        default=None, description="Histogram / frequency data."
    )
    outlier_count: int = Field(
        default=0, ge=0, description="Number of statistical outliers detected."
    )
    outlier_boundaries: Optional[dict[str, float]] = Field(
        default=None,
        description="{'lower': x, 'upper': y} IQR-based outlier fences.",
    )

    model_config = ConfigDict(use_enum_values=True)

    # ------------------------------------------------------------------
    # Field-level validators
    # ------------------------------------------------------------------

    @field_validator("null_rate", mode="before")
    @classmethod
    def _clamp_null_rate(cls, value: Any) -> float:
        """Ensure null_rate is within [0.0, 1.0]."""
        if isinstance(value, (int, float)):
            return max(0.0, min(float(value), 1.0))
        return value

    @field_validator("cardinality", mode="before")
    @classmethod
    def _clamp_cardinality(cls, value: Any) -> float:
        """Ensure cardinality ratio is within [0.0, 1.0]."""
        if isinstance(value, (int, float)):
            return max(0.0, min(float(value), 1.0))
        return value


class CorrelationEntry(BaseModel):
    """Pairwise correlation between two columns.

    Supports Pearson (linear), Spearman (rank), Cramér's V (categorical),
    and mutual information (general dependence) coefficients.  Used both
    for intra-table and cross-table correlation tracking to enable
    copula-based multivariate correlation preservation during generation.
    """

    column_a: str = Field(..., description="First column name.")
    column_b: str = Field(..., description="Second column name.")
    table_name: str = Field(
        ..., description="Table containing both columns."
    )
    pearson_coefficient: Optional[float] = Field(
        default=None,
        ge=-1.0,
        le=1.0,
        description="Pearson correlation coefficient (−1 to 1).",
    )
    spearman_coefficient: Optional[float] = Field(
        default=None,
        ge=-1.0,
        le=1.0,
        description="Spearman rank correlation coefficient.",
    )
    cramers_v: Optional[float] = Field(
        default=None,
        ge=0.0,
        le=1.0,
        description="Cramér's V for categorical columns.",
    )
    mutual_information: Optional[float] = Field(
        default=None,
        ge=0.0,
        description="Mutual information score (≥ 0).",
    )


class TableProfile(BaseModel):
    """Aggregated statistical profile for one ERP table.

    Collects per-column profiles, intra-table correlations, and table-level
    metadata (estimated row count, sample size, profiling duration).
    """

    table_name: str = Field(..., description="Table name.")
    columns: list[ColumnProfile] = Field(
        default_factory=list,
        description="Per-column statistical profiles.",
    )
    correlations: list[CorrelationEntry] = Field(
        default_factory=list,
        description="Inter-column correlations within the table.",
    )
    estimated_row_count: int = Field(
        default=0, ge=0, description="Estimated total rows in the table."
    )
    profiling_duration_seconds: Optional[float] = Field(
        default=None,
        ge=0.0,
        description="Wall-clock time taken to profile this table (seconds).",
    )
    sample_size: int = Field(
        default=0, ge=0, description="Number of rows sampled for profiling."
    )


# ===================================================================
# Top-Level Statistical Profile Model
# ===================================================================


class StatisticalProfile(BaseModel):
    """Schema-level statistical profile persisted in MongoDB.

    Represents the complete profiling result for one ERP schema, composed
    of per-table profiles, cross-table correlations, and operational
    metadata.  The ``profile_id`` serves as the MongoDB document key and
    is referenced by the Generation Engine and Quality Service.

    **Multi-tenant isolation:** The ``tenant_id`` field is mandatory and
    every query in :class:`StatisticalProfileRepository` is filtered by
    ``tenant_id``.
    """

    profile_id: str = Field(
        default_factory=lambda: str(uuid.uuid4()),
        description="Globally unique profile identifier (UUID4).",
    )
    tenant_id: str = Field(
        ..., description="Tenant identifier for multi-tenant isolation."
    )
    schema_id: str = Field(
        ...,
        description="Reference to the SchemaDefinition this profile is based on.",
    )
    status: ProfileStatus = Field(
        default=ProfileStatus.PENDING,
        description="Current profiling lifecycle status.",
    )
    tables: list[TableProfile] = Field(
        default_factory=list,
        description="Per-table statistical profiles.",
    )
    cross_table_correlations: list[CorrelationEntry] = Field(
        default_factory=list,
        description="Cross-table column correlations for copula modelling.",
    )
    total_tables_profiled: int = Field(
        default=0,
        ge=0,
        description="Count of tables profiled.",
    )
    total_columns_profiled: int = Field(
        default=0,
        ge=0,
        description="Count of columns profiled.",
    )
    overall_completeness: float = Field(
        default=0.0,
        ge=0.0,
        le=1.0,
        description="Fraction of columns successfully profiled.",
    )
    profiling_config: dict[str, Any] = Field(
        default_factory=dict,
        description="Configuration used for profiling (sample_size, timeout, …).",
    )
    profiling_metadata: dict[str, Any] = Field(
        default_factory=dict,
        description="Additional metadata (duration, errors, warnings).",
    )
    created_at: datetime = Field(
        default_factory=lambda: datetime.now(timezone.utc),
        description="UTC timestamp when the profile was created.",
    )
    updated_at: datetime = Field(
        default_factory=lambda: datetime.now(timezone.utc),
        description="UTC timestamp of the most recent update.",
    )
    created_by: Optional[str] = Field(
        default=None,
        description="User who initiated profiling.",
    )
    tags: list[str] = Field(
        default_factory=list,
        description="User-defined tags for organisation and filtering.",
    )

    model_config = ConfigDict(use_enum_values=True, populate_by_name=True)

    # ------------------------------------------------------------------
    # Auto-compute aggregate counters from table data
    # ------------------------------------------------------------------

    @model_validator(mode="before")
    @classmethod
    def _auto_compute_totals(cls, data: Any) -> Any:
        """Auto-compute ``total_tables_profiled`` and ``total_columns_profiled``.

        When the caller does not supply explicit totals, this validator
        derives them from the ``tables`` list.  Explicit values are
        preserved when provided so that partial-update scenarios are
        not overwritten.
        """
        if isinstance(data, dict):
            tables = data.get("tables", [])
            if tables and not data.get("total_tables_profiled"):
                data["total_tables_profiled"] = len(tables)
            if tables and not data.get("total_columns_profiled"):
                total_cols = 0
                for table in tables:
                    if isinstance(table, dict):
                        total_cols += len(table.get("columns", []))
                    elif isinstance(table, TableProfile):
                        total_cols += len(table.columns)
                data["total_columns_profiled"] = total_cols
        return data


# ===================================================================
# Request / Response DTOs
# ===================================================================


class ProfileRequest(BaseModel):
    """Request payload for initiating a statistical profiling job."""

    schema_id: str = Field(
        ..., description="Reference to the schema to profile."
    )
    sample_size: int = Field(
        default=10_000,
        ge=100,
        le=1_000_000,
        description="Number of rows to sample from each table.",
    )
    include_correlations: bool = Field(
        default=True,
        description="Whether to compute inter-column correlations.",
    )
    timeout_seconds: int = Field(
        default=600,
        ge=60,
        le=7200,
        description="Maximum profiling timeout in seconds.",
    )
    tags: list[str] = Field(
        default_factory=list,
        description="User-defined tags.",
    )


class ProfileResponse(BaseModel):
    """Response payload returned after creating a profiling job."""

    profile_id: str = Field(..., description="Unique profile identifier.")
    schema_id: str = Field(..., description="Schema being profiled.")
    status: ProfileStatus = Field(
        ..., description="Current profiling status."
    )
    total_tables_profiled: int = Field(
        default=0, description="Tables profiled so far."
    )
    total_columns_profiled: int = Field(
        default=0, description="Columns profiled so far."
    )
    created_at: datetime = Field(..., description="Creation timestamp (UTC).")
    message: str = Field(
        default="Statistical profiling initiated",
        description="Human-readable status message.",
    )

    model_config = ConfigDict(use_enum_values=True)


# ===================================================================
# Repository — MongoDB CRUD with Multi-Tenant Isolation
# ===================================================================


class StatisticalProfileRepository:
    """CRUD repository for ``statistical_profiles`` MongoDB collection.

    Every query is scoped to the ``tenant_id`` supplied at construction
    time, guaranteeing multi-tenant isolation.  The repository converts
    between :class:`StatisticalProfile` Pydantic models and MongoDB BSON
    documents.

    Args:
        tenant_id: The tenant namespace that scopes all operations.

    Example::

        repo = StatisticalProfileRepository(tenant_id="acme-corp")
        pid = repo.create(profile)
        found = repo.get_by_id(pid)
    """

    def __init__(self, tenant_id: str) -> None:
        """Initialise repository with tenant scope and collection handle.

        Args:
            tenant_id: Tenant identifier used to scope every query.
        """
        if not tenant_id or not tenant_id.strip():
            raise ValueError("tenant_id must be a non-empty string.")
        self._tenant_id: str = tenant_id.strip()
        self._collection = get_collection(COLLECTION_STATISTICAL_PROFILES)
        logger.debug(
            "StatisticalProfileRepository initialised",
            extra={"tenant_id": self._tenant_id},
        )

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _tenant_filter(self, extra: dict[str, Any] | None = None) -> dict[str, Any]:
        """Return a base MongoDB filter dict that includes tenant_id.

        Args:
            extra: Additional filter criteria to merge.

        Returns:
            MongoDB query filter with ``tenant_id`` always present.
        """
        query: dict[str, Any] = {"tenant_id": self._tenant_id}
        if extra:
            query.update(extra)
        return query

    @staticmethod
    def _profile_to_doc(profile: StatisticalProfile) -> dict[str, Any]:
        """Serialise a :class:`StatisticalProfile` to a MongoDB document.

        Args:
            profile: Pydantic model instance.

        Returns:
            Dictionary suitable for ``insert_one`` / ``replace_one``.
        """
        doc = profile.model_dump(mode="python")
        doc["_id"] = profile.profile_id
        return doc

    @staticmethod
    def _doc_to_profile(doc: dict[str, Any]) -> StatisticalProfile:
        """Deserialise a MongoDB document into a :class:`StatisticalProfile`.

        Args:
            doc: Raw BSON document from MongoDB.

        Returns:
            Validated Pydantic model instance.
        """
        doc.pop("_id", None)
        return StatisticalProfile.model_validate(doc)

    # ------------------------------------------------------------------
    # CRUD operations
    # ------------------------------------------------------------------

    def create(self, profile: StatisticalProfile) -> str:
        """Insert a new statistical profile document.

        The ``tenant_id`` on the profile is overwritten to match the
        repository's tenant scope, ensuring that documents cannot be
        inserted into the wrong namespace.

        Args:
            profile: The statistical profile to persist.

        Returns:
            The ``profile_id`` of the newly created document.

        Raises:
            Exception: If the MongoDB insert operation fails.
        """
        profile.tenant_id = self._tenant_id
        profile.updated_at = datetime.now(timezone.utc)
        doc = self._profile_to_doc(profile)
        try:
            self._collection.insert_one(doc)
            logger.info(
                "Statistical profile created",
                extra={
                    "profile_id": profile.profile_id,
                    "tenant_id": self._tenant_id,
                    "schema_id": profile.schema_id,
                },
            )
            return profile.profile_id
        except Exception:
            logger.error(
                "Failed to create statistical profile",
                extra={
                    "profile_id": profile.profile_id,
                    "tenant_id": self._tenant_id,
                },
                exc_info=True,
            )
            raise

    def get_by_id(self, profile_id: str) -> Optional[StatisticalProfile]:
        """Retrieve a statistical profile by its unique identifier.

        Args:
            profile_id: The profile UUID to look up.

        Returns:
            The matching :class:`StatisticalProfile` or ``None`` if not
            found within the tenant scope.
        """
        query = self._tenant_filter({"profile_id": profile_id})
        try:
            doc = self._collection.find_one(query)
            if doc is None:
                logger.debug(
                    "Statistical profile not found",
                    extra={
                        "profile_id": profile_id,
                        "tenant_id": self._tenant_id,
                    },
                )
                return None
            return self._doc_to_profile(doc)
        except Exception:
            logger.error(
                "Error retrieving statistical profile by id",
                extra={
                    "profile_id": profile_id,
                    "tenant_id": self._tenant_id,
                },
                exc_info=True,
            )
            raise

    def get_by_schema_id(self, schema_id: str) -> Optional[StatisticalProfile]:
        """Retrieve the most recent profile for a given schema.

        Returns the latest profile (by ``created_at`` descending) that
        matches the schema and tenant scope.

        Args:
            schema_id: The schema definition identifier.

        Returns:
            The most recent :class:`StatisticalProfile` or ``None``.
        """
        query = self._tenant_filter({"schema_id": schema_id})
        try:
            doc = self._collection.find_one(
                query,
                sort=[("created_at", -1)],
            )
            if doc is None:
                logger.debug(
                    "No statistical profile found for schema",
                    extra={
                        "schema_id": schema_id,
                        "tenant_id": self._tenant_id,
                    },
                )
                return None
            return self._doc_to_profile(doc)
        except Exception:
            logger.error(
                "Error retrieving statistical profile by schema_id",
                extra={
                    "schema_id": schema_id,
                    "tenant_id": self._tenant_id,
                },
                exc_info=True,
            )
            raise

    def list_profiles(
        self,
        schema_id: Optional[str] = None,
        status: Optional[ProfileStatus] = None,
        skip: int = 0,
        limit: int = 20,
    ) -> list[StatisticalProfile]:
        """List statistical profiles with optional filters and pagination.

        Results are sorted by ``created_at`` descending (most recent first).

        Args:
            schema_id: Optional filter by schema identifier.
            status: Optional filter by profiling status.
            skip: Number of documents to skip (offset-based pagination).
            limit: Maximum number of documents to return.

        Returns:
            List of matching :class:`StatisticalProfile` instances.
        """
        extra: dict[str, Any] = {}
        if schema_id is not None:
            extra["schema_id"] = schema_id
        if status is not None:
            extra["status"] = status.value if isinstance(status, ProfileStatus) else status
        query = self._tenant_filter(extra)
        try:
            cursor = (
                self._collection.find(query)
                .sort("created_at", -1)
                .skip(skip)
                .limit(limit)
            )
            profiles: list[StatisticalProfile] = []
            for doc in cursor:
                try:
                    profiles.append(self._doc_to_profile(doc))
                except Exception:
                    logger.warning(
                        "Skipping malformed statistical profile document",
                        extra={
                            "doc_id": str(doc.get("_id", "unknown")),
                            "tenant_id": self._tenant_id,
                        },
                        exc_info=True,
                    )
            logger.debug(
                "Listed statistical profiles",
                extra={
                    "tenant_id": self._tenant_id,
                    "count": len(profiles),
                    "skip": skip,
                    "limit": limit,
                },
            )
            return profiles
        except Exception:
            logger.error(
                "Error listing statistical profiles",
                extra={"tenant_id": self._tenant_id},
                exc_info=True,
            )
            raise

    def update(self, profile_id: str, updates: dict[str, Any]) -> bool:
        """Partially update a statistical profile document.

        ``updated_at`` is automatically set to the current UTC time.  The
        ``_id``, ``profile_id``, and ``tenant_id`` fields cannot be
        changed through this method.

        Args:
            profile_id: The profile UUID to update.
            updates: Dictionary of field-level updates.

        Returns:
            ``True`` if a document was modified, ``False`` otherwise.
        """
        # Guard against overwriting immutable fields.
        for protected in ("_id", "profile_id", "tenant_id"):
            updates.pop(protected, None)

        updates["updated_at"] = datetime.now(timezone.utc)

        query = self._tenant_filter({"profile_id": profile_id})
        try:
            result = self._collection.update_one(query, {"$set": updates})
            modified = result.modified_count > 0
            if modified:
                logger.info(
                    "Statistical profile updated",
                    extra={
                        "profile_id": profile_id,
                        "tenant_id": self._tenant_id,
                        "updated_fields": list(updates.keys()),
                    },
                )
            else:
                logger.warning(
                    "Statistical profile update matched no documents",
                    extra={
                        "profile_id": profile_id,
                        "tenant_id": self._tenant_id,
                    },
                )
            return modified
        except Exception:
            logger.error(
                "Error updating statistical profile",
                extra={
                    "profile_id": profile_id,
                    "tenant_id": self._tenant_id,
                },
                exc_info=True,
            )
            raise

    def update_status(self, profile_id: str, status: ProfileStatus) -> bool:
        """Convenience method to update only the profiling status.

        Args:
            profile_id: The profile UUID to update.
            status: The new :class:`ProfileStatus` value.

        Returns:
            ``True`` if a document was modified, ``False`` otherwise.
        """
        status_value = status.value if isinstance(status, ProfileStatus) else status
        return self.update(profile_id, {"status": status_value})

    def add_table_profile(
        self,
        profile_id: str,
        table_profile: TableProfile,
    ) -> bool:
        """Append a table profile to an existing statistical profile.

        Supports incremental profiling where tables are profiled one at a
        time and their results are pushed into the parent profile document.

        After appending, ``total_tables_profiled`` and
        ``total_columns_profiled`` are also incremented to keep counters
        consistent.

        Args:
            profile_id: The parent profile UUID.
            table_profile: The :class:`TableProfile` to append.

        Returns:
            ``True`` if the document was modified, ``False`` otherwise.
        """
        table_doc = table_profile.model_dump(mode="python")
        query = self._tenant_filter({"profile_id": profile_id})
        num_new_columns = len(table_profile.columns)
        try:
            result = self._collection.update_one(
                query,
                {
                    "$push": {"tables": table_doc},
                    "$inc": {
                        "total_tables_profiled": 1,
                        "total_columns_profiled": num_new_columns,
                    },
                    "$set": {"updated_at": datetime.now(timezone.utc)},
                },
            )
            modified = result.modified_count > 0
            if modified:
                logger.info(
                    "Table profile appended",
                    extra={
                        "profile_id": profile_id,
                        "tenant_id": self._tenant_id,
                        "table_name": table_profile.table_name,
                        "columns_added": num_new_columns,
                    },
                )
            else:
                logger.warning(
                    "add_table_profile matched no documents",
                    extra={
                        "profile_id": profile_id,
                        "tenant_id": self._tenant_id,
                    },
                )
            return modified
        except Exception:
            logger.error(
                "Error appending table profile",
                extra={
                    "profile_id": profile_id,
                    "tenant_id": self._tenant_id,
                    "table_name": table_profile.table_name,
                },
                exc_info=True,
            )
            raise

    def delete(self, profile_id: str) -> bool:
        """Delete a statistical profile by its identifier.

        Only deletes within the repository's tenant scope.

        Args:
            profile_id: The profile UUID to delete.

        Returns:
            ``True`` if a document was deleted, ``False`` otherwise.
        """
        query = self._tenant_filter({"profile_id": profile_id})
        try:
            result = self._collection.delete_one(query)
            deleted = result.deleted_count > 0
            if deleted:
                logger.info(
                    "Statistical profile deleted",
                    extra={
                        "profile_id": profile_id,
                        "tenant_id": self._tenant_id,
                    },
                )
            else:
                logger.warning(
                    "Statistical profile delete matched no documents",
                    extra={
                        "profile_id": profile_id,
                        "tenant_id": self._tenant_id,
                    },
                )
            return deleted
        except Exception:
            logger.error(
                "Error deleting statistical profile",
                extra={
                    "profile_id": profile_id,
                    "tenant_id": self._tenant_id,
                },
                exc_info=True,
            )
            raise

    def count(self, schema_id: Optional[str] = None) -> int:
        """Count statistical profiles for the current tenant.

        Args:
            schema_id: Optional filter to count profiles for a specific
                schema only.

        Returns:
            Number of matching documents.
        """
        extra: dict[str, Any] = {}
        if schema_id is not None:
            extra["schema_id"] = schema_id
        query = self._tenant_filter(extra)
        try:
            total = self._collection.count_documents(query)
            logger.debug(
                "Counted statistical profiles",
                extra={
                    "tenant_id": self._tenant_id,
                    "count": total,
                    "schema_id": schema_id,
                },
            )
            return total
        except Exception:
            logger.error(
                "Error counting statistical profiles",
                extra={"tenant_id": self._tenant_id},
                exc_info=True,
            )
            raise
