"""Pydantic v2 request/response validation models for the generation job domain.

This module defines the complete set of Pydantic v2 models used by the API Gateway
to validate incoming requests and serialize outgoing responses for generation job
endpoints:

    - POST /api/v1/generation/jobs       — Create a new generation job
    - GET  /api/v1/generation/jobs/{id}   — Retrieve a specific job's status/results
    - GET  /api/v1/generation/jobs        — List generation jobs with pagination

Models enforce strict type checking, field constraints, and cross-field validation
to ensure data integrity throughout the generation pipeline. All enums inherit from
``(str, Enum)`` for seamless JSON serialization with Pydantic v2.

Typical usage::

    from src.backend.api_gateway.schemas.generation import (
        GenerationJobRequest,
        GenerationJobResponse,
        GenerationJobListResponse,
        JobStatus,
        GenerationMethod,
        OutputFormat,
    )

    # Validate an incoming request payload
    request = GenerationJobRequest(**payload)

    # Build a response from a MongoDB document
    response = GenerationJobResponse.model_validate(document)
"""

from datetime import datetime
from enum import Enum
from typing import Any, Dict, List, Optional

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator


# ---------------------------------------------------------------------------
# Enumerations
# ---------------------------------------------------------------------------


class JobStatus(str, Enum):
    """Enumeration of generation job lifecycle states.

    A generation job progresses through the following state machine:

        SUBMITTED → GENERATING → VALIDATING → CERTIFYING → PROVISIONING → COMPLETED
                 ↘                                                       ↗
                   ──────────────── FAILED ────────────────────────────

    Each state transition is recorded with a timestamp in the job document
    stored in the ``generation_profiles`` MongoDB collection.

    Attributes:
        SUBMITTED: Job has been accepted and queued for processing.
        GENERATING: Batch data generation is actively in progress.
        VALIDATING: Post-generation quality validation is running
            (40% statistical + 30% business rules + 30% referential integrity).
        CERTIFYING: Compliance service is scanning for PII and verifying
            regulatory requirements (GDPR, HIPAA, CCPA).
        PROVISIONING: Output is being delivered to the target destination
            (file export or database provisioning).
        COMPLETED: Job finished successfully with all quality and compliance
            checks passed.
        FAILED: Job encountered an unrecoverable error at any stage.
    """

    SUBMITTED = "submitted"
    GENERATING = "generating"
    VALIDATING = "validating"
    CERTIFYING = "certifying"
    PROVISIONING = "provisioning"
    COMPLETED = "completed"
    FAILED = "failed"


class GenerationMethod(str, Enum):
    """Enumeration of supported synthetic data generation methods.

    The generation engine supports four distinct methods, each suited to
    different data types and fidelity requirements.  The method selector
    in the generation orchestrator automatically recommends the optimal
    approach per column based on the statistical profile.

    Attributes:
        AI_ML: GAN (Generative Adversarial Network) and VAE (Variational
            Autoencoder) based generation using PyTorch/TensorFlow.  Best
            for complex multivariate distributions and high-fidelity output.
        RULES_BASED: Constraint-driven generation using a configurable
            business rules engine.  Ideal for fields with strict format
            requirements (e.g., invoice numbers, cost centre codes).
        STATISTICAL: Distribution fitting and sampling using SciPy/NumPy.
            Supports normal, log-normal, Poisson, uniform, exponential,
            and categorical distributions with copula-based correlation
            preservation.
        MASKING: Intelligent data masking that transforms existing data
            patterns while preserving statistical properties.  Requires
            an existing statistical profile from the Profiling Service.
    """

    AI_ML = "ai_ml"
    RULES_BASED = "rules_based"
    STATISTICAL = "statistical"
    MASKING = "masking"


class OutputFormat(str, Enum):
    """Enumeration of supported output formats for generated data.

    The generation engine produces output in the requested format via
    dedicated formatter classes.  Format selection impacts file size,
    portability, and target system compatibility.

    Attributes:
        SQL: SQL INSERT/COPY statements compatible with the target database
            dialect (PostgreSQL, Oracle, SQL Server, SAP HANA).
        CSV: Comma-separated values with configurable delimiters.  Most
            portable format and widest tool compatibility.
        JSON: JSON or JSON Lines (JSONL) format for API consumption and
            document-oriented targets.
        PARQUET: Apache Parquet columnar format via PyArrow.  Provides
            the best compression ratio and is optimal for analytical
            workloads and cloud data lake ingestion.
    """

    SQL = "sql"
    CSV = "csv"
    JSON = "json"
    PARQUET = "parquet"


# ---------------------------------------------------------------------------
# Request Models
# ---------------------------------------------------------------------------


class TableConfig(BaseModel):
    """Per-table generation configuration.

    Specifies generation parameters for a single target table, including
    the number of records to produce, optional column-level overrides,
    and whether foreign key relationships should be maintained across
    dependent tables.

    Attributes:
        table_name: Fully qualified or short name of the target table.
            Must be non-empty and correspond to a table discovered via
            the Profiling Service.
        record_count: Number of synthetic records to generate for this
            table.  Must be between 1 and 10,000,000 (10 M).
        columns: Optional dictionary of column-specific generation
            overrides.  Keys are column names; values are configuration
            objects whose schema depends on the generation method (e.g.,
            distribution parameters for statistical, regex patterns for
            rules-based).
        preserve_relationships: When ``True`` (default), the generation
            engine enforces referential integrity by maintaining valid
            foreign key references to parent tables.
    """

    model_config = ConfigDict(strict=True)

    table_name: str = Field(
        ...,
        min_length=1,
        description="Target table name from the discovered ERP schema.",
    )
    record_count: int = Field(
        ...,
        gt=0,
        le=10_000_000,
        description="Number of synthetic records to generate (1–10M).",
    )
    columns: Optional[Dict[str, Any]] = Field(
        default=None,
        description="Column-specific generation overrides keyed by column name.",
    )
    preserve_relationships: bool = Field(
        default=True,
        description="Maintain foreign key relationships during generation.",
    )


class GenerationJobRequest(BaseModel):
    """Request model for creating a new generation job.

    Captures all parameters required to launch a synthetic data generation
    run, including the generation method, target schema, per-table
    configurations, output format, batch size, and quality threshold.

    The model enforces:
    * At least one table configuration is supplied.
    * No duplicate table names in the ``tables`` list.
    * Masking method requests include a valid ``schema_id`` referencing
      an existing statistical profile.
    * Batch size is within the supported range (1,000–100,000).
    * Quality threshold defaults to 0.95 (≥95% fidelity target per the
      weighted scoring formula).

    Attributes:
        method: The generation strategy to use (AI/ML, rules-based,
            statistical, or masking).
        schema_id: Reference identifier for the discovered ERP schema
            stored in the ``schema_definitions`` MongoDB collection.
        tables: One or more per-table generation configurations.
        output_format: Desired output format; defaults to CSV.
        batch_size: Number of records per processing batch; defaults to
            10,000 (tunable 1K–100K).
        quality_threshold: Minimum acceptable quality score on the 0–1
            scale; defaults to 0.95.
        template_id: Optional reference to a reusable generation template.
        tenant_id: Tenant namespace for multi-tenant isolation.  Populated
            automatically by the tenant middleware when omitted.
        metadata: Arbitrary key-value metadata attached to the job for
            auditing and filtering purposes.
    """

    model_config = ConfigDict(strict=True)

    method: GenerationMethod = Field(
        ...,
        description="Generation method to use (ai_ml, rules_based, statistical, masking).",
    )
    schema_id: str = Field(
        ...,
        min_length=1,
        description="Reference to a discovered ERP schema in schema_definitions.",
    )
    tables: List[TableConfig] = Field(
        ...,
        min_length=1,
        description="Per-table generation configurations; at least one required.",
    )
    output_format: OutputFormat = Field(
        default=OutputFormat.CSV,
        description="Output format for generated data (sql, csv, json, parquet).",
    )
    batch_size: int = Field(
        default=10_000,
        ge=1_000,
        le=100_000,
        description="Records per processing batch (1K–100K, default 10K).",
    )
    quality_threshold: float = Field(
        default=0.95,
        ge=0.0,
        le=1.0,
        description="Minimum quality score on a 0–1 scale (default 0.95 / 95%).",
    )
    template_id: Optional[str] = Field(
        default=None,
        description="Optional generation template identifier for reuse.",
    )
    tenant_id: Optional[str] = Field(
        default=None,
        description="Tenant namespace; auto-populated by tenant middleware if omitted.",
    )
    metadata: Optional[Dict[str, Any]] = Field(
        default=None,
        description="Arbitrary key-value metadata for auditing and filtering.",
    )

    # ------------------------------------------------------------------
    # Field-level validators
    # ------------------------------------------------------------------

    @field_validator("tables")
    @classmethod
    def validate_tables_no_duplicates(
        cls, tables: List[TableConfig]
    ) -> List[TableConfig]:
        """Ensure at least one table config exists and table names are unique.

        Args:
            tables: The list of ``TableConfig`` instances submitted in the
                request body.

        Returns:
            The validated list of table configurations.

        Raises:
            ValueError: If duplicate table names are detected.
        """
        seen_names: set[str] = set()
        duplicates: list[str] = []
        for table in tables:
            normalised = table.table_name.strip().lower()
            if normalised in seen_names:
                duplicates.append(table.table_name)
            seen_names.add(normalised)

        if duplicates:
            raise ValueError(
                f"Duplicate table names are not allowed: {', '.join(duplicates)}. "
                "Each table must appear at most once in the request."
            )
        return tables

    # ------------------------------------------------------------------
    # Cross-field (model-level) validators
    # ------------------------------------------------------------------

    @model_validator(mode="after")
    def validate_masking_requires_profile(self) -> "GenerationJobRequest":
        """Validate that the masking method is paired with a valid schema reference.

        The masking generation method transforms existing data patterns and
        therefore requires a statistical profile to already exist for the
        referenced schema.  This validator ensures that requests specifying
        ``GenerationMethod.MASKING`` provide a non-empty ``schema_id`` that
        can be resolved to a profile at execution time.

        Returns:
            The validated ``GenerationJobRequest`` instance.

        Raises:
            ValueError: If the masking method is selected without a usable
                ``schema_id``.
        """
        if self.method == GenerationMethod.MASKING:
            if not self.schema_id or not self.schema_id.strip():
                raise ValueError(
                    "The 'masking' generation method requires a valid 'schema_id' "
                    "referencing an existing statistical profile.  Please run the "
                    "Profiling Service on the target schema before using masking."
                )
        return self


# ---------------------------------------------------------------------------
# Response Models
# ---------------------------------------------------------------------------


class GenerationJobResponse(BaseModel):
    """Response model for a single generation job.

    Serialises the current state of a generation job, including its progress,
    quality score, output location, and temporal lifecycle timestamps.  Used
    by both the ``GET /api/v1/generation/jobs/{id}`` detail endpoint and as
    an element within ``GenerationJobListResponse``.

    The model uses ``from_attributes=True`` (Pydantic v2 ORM mode) to support
    direct construction from MongoDB document dictionaries or ORM objects.

    Attributes:
        job_id: Unique identifier for this generation job (UUID string).
        status: Current lifecycle state of the job.
        method: The generation method used for this job.
        progress: Completion percentage (0.0–100.0) updated in real time
            via Redis pub/sub.
        total_records: Aggregate record count requested across all tables.
        generated_records: Number of records produced so far.
        quality_score: Weighted quality score (0.0–1.0) computed after
            validation.  ``None`` until validation completes.
        output_location: URI or path to the generated output (file path,
            S3 URI, database identifier).  ``None`` until provisioning.
        error_message: Human-readable error description populated when
            the job status is ``FAILED``.
        created_at: Timestamp when the job was first submitted.
        updated_at: Timestamp of the most recent status change.
        completed_at: Timestamp when the job reached ``COMPLETED`` or
            ``FAILED``.  ``None`` while in progress.
        tenant_id: Tenant namespace for multi-tenant isolation.
    """

    model_config = ConfigDict(from_attributes=True)

    job_id: str = Field(
        ...,
        description="Unique job identifier (UUID).",
    )
    status: JobStatus = Field(
        ...,
        description="Current job lifecycle status.",
    )
    method: GenerationMethod = Field(
        ...,
        description="Generation method used for this job.",
    )
    progress: float = Field(
        default=0.0,
        ge=0.0,
        le=100.0,
        description="Job completion percentage (0–100).",
    )
    total_records: int = Field(
        default=0,
        ge=0,
        description="Total records requested across all tables.",
    )
    generated_records: int = Field(
        default=0,
        ge=0,
        description="Records generated so far.",
    )
    quality_score: Optional[float] = Field(
        default=None,
        ge=0.0,
        le=1.0,
        description="Weighted quality score (0.0–1.0) after validation.",
    )
    output_location: Optional[str] = Field(
        default=None,
        description="URI/path of the generated output.",
    )
    error_message: Optional[str] = Field(
        default=None,
        description="Error details when status is FAILED.",
    )
    created_at: datetime = Field(
        ...,
        description="Job creation timestamp (ISO 8601).",
    )
    updated_at: datetime = Field(
        ...,
        description="Last status update timestamp (ISO 8601).",
    )
    completed_at: Optional[datetime] = Field(
        default=None,
        description="Job completion timestamp (ISO 8601); None while in progress.",
    )
    tenant_id: Optional[str] = Field(
        default=None,
        description="Tenant namespace for multi-tenant isolation.",
    )


# ---------------------------------------------------------------------------
# List / Pagination Response
# ---------------------------------------------------------------------------


class GenerationJobListResponse(BaseModel):
    """Paginated response for the generation jobs list endpoint.

    Wraps a page of ``GenerationJobResponse`` items together with
    pagination metadata, used by ``GET /api/v1/generation/jobs``.

    Attributes:
        jobs: List of generation job response objects for the current page.
        total: Total number of jobs matching the query filters.
        page: Current page number (1-based).
        page_size: Maximum number of jobs per page (1–100, default 20).
    """

    jobs: List[GenerationJobResponse] = Field(
        default_factory=list,
        description="Generation job items for the current page.",
    )
    total: int = Field(
        default=0,
        ge=0,
        description="Total number of matching generation jobs.",
    )
    page: int = Field(
        default=1,
        ge=1,
        description="Current page number (1-based).",
    )
    page_size: int = Field(
        default=20,
        ge=1,
        le=100,
        description="Number of items per page (1–100).",
    )
