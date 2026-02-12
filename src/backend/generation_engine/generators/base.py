"""Abstract base generator module for the Synthetic ERP Data Generation Platform.

This module establishes the **Strategy pattern** contract that all four concrete
generation strategies must implement:

- **AI/ML Generator** (GAN/VAE-based synthesis)
- **Rules Generator** (Business-rules-based constraint generation)
- **Statistical Generator** (Distribution-fitting synthesis via SciPy/NumPy)
- **Masking Generator** (Intelligent data masking with privacy preservation)

Core Components:
    - :class:`ColumnSpec` — Pydantic model describing a single column's schema
      (data type, nullability, constraints, FK relationships).
    - :class:`GenerationConfig` — Pydantic model encapsulating all generation
      parameters (method, record count, batch size, quality threshold, etc.).
    - :class:`GenerationResult` — Pydantic model wrapping the generated data
      along with metadata, timing, quality scores, and diagnostics.
    - :class:`GenerationError` — Custom exception carrying the failing method
      name and structured error details for upstream error handling.
    - :class:`BaseGenerator` — Abstract base class (ABC) defining three abstract
      methods (``generate``, ``validate_config``, ``get_capabilities``) and
      providing concrete helpers for batching, timing, null injection, schema
      validation, and progress reporting.
    - :data:`GeneratorType` — Type alias for generator class references used by
      the generator registry and method selector.

The orchestrator (:mod:`generation_engine.orchestrator.method_selector`) relies
on this module to polymorphically invoke any generation method without coupling
to concrete implementations.

Usage::

    from generation_engine.generators.base import BaseGenerator, GenerationResult


    class MyGenerator(BaseGenerator):
        def generate(self, schema, profile, num_records, **kwargs): ...
        def validate_config(self, config): ...
        def get_capabilities(self): ...
"""

from __future__ import annotations

import math
import time
from abc import ABC, abstractmethod
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING, Any, Literal


if TYPE_CHECKING:
    from collections.abc import Iterator

import numpy as np
import pandas as pd
from pydantic import BaseModel, ConfigDict, Field, field_validator

from shared.logging.structured_logger import get_logger


# ---------------------------------------------------------------------------
# Pydantic Data Models
# ---------------------------------------------------------------------------


class ColumnSpec(BaseModel):
    """Schema specification for a single table column.

    Captures everything a concrete generator needs to know about a column
    in order to produce synthetic values: the data type, nullability
    settings, value constraints, primary/foreign key status, and an
    optional human-readable description.

    Attributes:
        name: Column name as it appears in the target schema.
        data_type: The logical data type the generator must produce values
            for. One of a fixed set of recognised types covering all common
            ERP column categories.
        nullable: Whether the column accepts ``NULL`` values.
        null_probability: Probability (0.0-1.0) that any given row will
            have a ``NULL`` in this column. Only meaningful when
            *nullable* is ``True``.
        constraints: Free-form dictionary of column-specific constraints.
            Common keys include ``min``, ``max``, ``pattern``,
            ``enum_values``, ``unique``, ``regex``, ``precision``, and
            ``scale``.
        primary_key: ``True`` if this column is (part of) the table's
            primary key.
        foreign_key: If the column is a foreign key, a mapping of
            ``{"table": "<referenced_table>", "column": "<referenced_col>"}``.
        description: Optional human-readable description of the column's
            business meaning.
    """

    name: str
    data_type: Literal[
        "integer",
        "float",
        "string",
        "boolean",
        "date",
        "datetime",
        "timestamp",
        "decimal",
        "text",
        "binary",
    ]
    nullable: bool = False
    null_probability: float = Field(
        default=0.0,
        ge=0.0,
        le=1.0,
        description="Probability of generating a NULL value (0.0-1.0).",
    )
    constraints: dict[str, Any] | None = Field(
        default=None,
        description=(
            "Column-specific constraints such as min, max, pattern, enum_values, unique, regex, precision, scale."
        ),
    )
    primary_key: bool = False
    foreign_key: dict[str, str] | None = Field(
        default=None,
        description=('Foreign key reference: {"table": "<table>", "column": "<column>"}.'),
    )
    description: str | None = None


class GenerationConfig(BaseModel):
    """Configuration parameters for a single data-generation run.

    Encapsulates every knob that the orchestrator and concrete generators
    consult when producing synthetic data — from high-level method
    selection to fine-grained batch sizing, quality thresholds, and
    multi-tenant isolation.

    Attributes:
        method: Generation method identifier. One of ``'ai_ml'``,
            ``'rules'``, ``'statistical'``, ``'masking'``.
        num_records: Total number of synthetic records to produce. Must be
            strictly positive.
        batch_size: Records produced per batch iteration (1-100 000).
            Defaults to 10 000, the platform's recommended batch size.
        seed: Optional random seed for reproducible generation runs.
        output_format: Desired in-memory representation of the result:
            ``'dataframe'`` (default), ``'dict'``, or ``'records'``.
        quality_threshold: Minimum acceptable quality score (0.0-1.0).
            Defaults to 0.95 per the platform's ≥95 % fidelity target.
        timeout_seconds: Maximum wall-clock seconds allowed for generation
            before the engine aborts. Defaults to 3 600 (one hour).
        tenant_id: Optional tenant namespace for multi-tenant isolation.
        job_id: Optional identifier linking this run to a tracked
            generation job in MongoDB.
        method_config: Arbitrary dictionary of parameters forwarded
            verbatim to the selected concrete generator.
    """

    method: str = Field(
        ...,
        description="Generation method name: 'ai_ml', 'rules', 'statistical', 'masking'.",
    )
    num_records: int = Field(
        ...,
        gt=0,
        description="Number of records to generate (must be > 0).",
    )
    batch_size: int = Field(
        default=10000,
        ge=1,
        le=100000,
        description="Records per batch (1-100 000). Default 10 000.",
    )
    seed: int | None = Field(
        default=None,
        description="Random seed for reproducibility.",
    )
    output_format: Literal["dataframe", "dict", "records"] = Field(
        default="dataframe",
        description="In-memory output representation.",
    )
    quality_threshold: float = Field(
        default=0.95,
        ge=0.0,
        le=1.0,
        description="Minimum acceptable quality score (0.0-1.0).",
    )
    timeout_seconds: int = Field(
        default=3600,
        ge=1,
        description="Maximum wall-clock seconds for generation.",
    )
    tenant_id: str | None = Field(
        default=None,
        description="Tenant namespace for multi-tenant isolation.",
    )
    job_id: str | None = Field(
        default=None,
        description="Associated generation job ID in MongoDB.",
    )
    method_config: dict[str, Any] | None = Field(
        default=None,
        description="Method-specific configuration forwarded to the concrete generator.",
    )

    @field_validator("num_records")
    @classmethod
    def _validate_num_records_positive(cls, value: int) -> int:
        """Ensure *num_records* is strictly positive.

        While ``Field(gt=0)`` already enforces this at the schema level,
        this explicit validator provides a clearer error message when the
        value originates from loosely-typed sources (e.g. JSON API
        payloads decoded as ``int``).
        """
        if value <= 0:
            raise ValueError(f"num_records must be a positive integer, got {value}.")
        return value


class GenerationResult(BaseModel):
    """Container for the output of a generation run.

    Wraps the synthetic data alongside rich metadata — timing, quality
    scores, non-fatal errors, warnings, and start/end timestamps — so
    that downstream services (Quality Service, Compliance Service) can
    process and audit the result without additional lookups.

    ``model_config`` enables ``arbitrary_types_allowed`` so that the
    ``data`` field can hold a :class:`pandas.DataFrame` in addition to
    plain Python containers.

    Attributes:
        data: The generated synthetic data.  Typically a
            :class:`pandas.DataFrame`, but may also be ``List[Dict]`` or
            another serialisable container depending on
            ``GenerationConfig.output_format``.
        num_records: Actual number of records produced (may differ from the
            requested count when errors or timeouts occur).
        columns: Ordered list of column names present in the generated
            data.
        metadata: Free-form dictionary capturing generation metadata such
            as the method used, parameter snapshot, and timing breakdown.
        generation_time_seconds: Wall-clock duration of the generation
            phase in seconds.
        quality_score: Optional quality score in [0.0, 1.0] if the Quality
            Service has already validated the result.
        errors: Non-fatal error messages encountered during generation.
            Fatal errors raise :class:`GenerationError` instead.
        warnings: Advisory messages (e.g. partial generation, fallback
            methods applied).
        started_at: UTC timestamp when generation began.
        completed_at: UTC timestamp when generation finished.
    """

    model_config = ConfigDict(arbitrary_types_allowed=True)

    data: Any = Field(
        ...,
        description="Generated data (pd.DataFrame, List[Dict], etc.).",
    )
    num_records: int = Field(
        ...,
        ge=0,
        description="Actual number of records generated.",
    )
    columns: list[str] = Field(
        ...,
        description="Column names in generated data.",
    )
    metadata: dict[str, Any] = Field(
        default_factory=dict,
        description="Generation metadata (method, parameters, timing).",
    )
    generation_time_seconds: float = Field(
        default=0.0,
        ge=0.0,
        description="Wall-clock generation duration in seconds.",
    )
    quality_score: float | None = Field(
        default=None,
        ge=0.0,
        le=1.0,
        description="Quality score (0.0-1.0) if validated.",
    )
    errors: list[str] = Field(
        default_factory=list,
        description="Non-fatal errors encountered during generation.",
    )
    warnings: list[str] = Field(
        default_factory=list,
        description="Warnings (e.g. partial generation, fallbacks).",
    )
    started_at: datetime | None = Field(
        default=None,
        description="UTC timestamp when generation started.",
    )
    completed_at: datetime | None = Field(
        default=None,
        description="UTC timestamp when generation completed.",
    )


# ---------------------------------------------------------------------------
# Custom Exception
# ---------------------------------------------------------------------------


class GenerationError(Exception):
    """Raised when a generation strategy encounters a fatal error.

    Carries structured information (the failing method name and an
    optional detail dictionary) so that the orchestrator and API layer
    can produce actionable error responses without losing context.

    Attributes:
        message: Human-readable error description.
        method: Name of the generation method that failed (e.g.
            ``'ai_ml'``, ``'statistical'``).
        details: Optional dictionary of contextual information such as
            the failing column, constraint violation, or traceback
            snippet.
    """

    def __init__(
        self,
        message: str,
        method: str,
        details: dict[str, Any] | None = None,
    ) -> None:
        self.message: str = message
        self.method: str = method
        self.details: dict[str, Any] = details if details is not None else {}
        super().__init__(self.message)

    def __repr__(self) -> str:  # pragma: no cover — convenience repr
        return f"GenerationError(message={self.message!r}, method={self.method!r}, details={self.details!r})"


# ---------------------------------------------------------------------------
# Abstract Base Generator
# ---------------------------------------------------------------------------


class BaseGenerator(ABC):
    """Abstract base class for all synthetic data generators.

    Establishes the **Strategy pattern** contract consumed by the
    :class:`~generation_engine.orchestrator.method_selector.MethodSelector`
    and :class:`~generation_engine.orchestrator.job_orchestrator.JobOrchestrator`.

    Subclasses **must** implement:

    - :meth:`generate` — core generation logic.
    - :meth:`validate_config` — configuration validation.
    - :meth:`get_capabilities` — capability introspection.

    Concrete helper methods provided:

    - :meth:`generate_batch` / :meth:`iterate_batches` — batch-oriented
      generation wrappers.
    - :meth:`_start_timer` / :meth:`_stop_timer` — high-resolution
      performance timing.
    - :meth:`_build_result` — ``GenerationResult`` factory.
    - :meth:`_validate_schema` — schema-to-``ColumnSpec`` parser.
    - :meth:`_apply_nulls` — probabilistic ``NULL`` injection.
    - :meth:`_log_progress` — milestone-based progress logging.

    Args:
        config: Optional raw configuration dictionary.  Stored as
            :attr:`config` for subclass access.
    """

    def __init__(self, config: dict[str, Any] | None = None) -> None:
        self.config: dict[str, Any] = config if config is not None else {}
        self.logger = get_logger(__name__)
        self._start_time: float | None = None
        self._records_generated: int = 0

    # ------------------------------------------------------------------
    # Abstract Methods (Strategy Pattern Contract)
    # ------------------------------------------------------------------

    @abstractmethod
    def generate(
        self,
        schema: dict[str, Any],
        profile: dict[str, Any],
        num_records: int,
        **kwargs: Any,
    ) -> GenerationResult:
        """Generate synthetic data matching the provided schema and profile.

        Concrete implementations must produce *num_records* rows of
        synthetic data whose statistical properties approximate the
        supplied *profile* and whose structure conforms to *schema*.

        Args:
            schema: Dictionary containing the table schema definition.
                Expected to include a ``"columns"`` key whose value is a
                list of column descriptors parseable by
                :meth:`_validate_schema`.
            profile: Dictionary containing the statistical profile
                captured by the Profiling Service (distributions, value
                ranges, pattern frequencies, etc.).
            num_records: Number of synthetic records to generate.
            **kwargs: Additional method-specific parameters (forwarded
                from ``GenerationConfig.method_config``).

        Returns:
            A :class:`GenerationResult` encapsulating the generated data,
            metadata, and diagnostics.

        Raises:
            GenerationError: When a fatal, non-recoverable error prevents
                generation from completing.
        """

    @abstractmethod
    def validate_config(self, config: dict[str, Any]) -> bool:
        """Validate generator-specific configuration before a run starts.

        Each concrete generator defines its own required and optional
        configuration keys.  This method is invoked by the orchestrator
        before dispatching a generation job.

        Args:
            config: Configuration dictionary to validate.

        Returns:
            ``True`` when the configuration is valid.

        Raises:
            ValueError: With a descriptive message when any required key
                is missing or any value is outside its acceptable range.
        """

    @abstractmethod
    def get_capabilities(self) -> dict[str, Any]:
        """Return a machine-readable description of this generator's capabilities.

        The orchestrator's method selector uses this information to choose
        the most appropriate generator for each column or table.

        Returns:
            A dictionary containing at minimum:

            - ``name`` (:class:`str`): Short identifier (e.g.
              ``"ai_ml"``).
            - ``description`` (:class:`str`): Human-readable summary.
            - ``supported_column_types`` (:class:`list[str]`): Data types
              this generator can handle.
            - ``best_for`` (:class:`list[str]`): Use-case tags indicating
              where this generator excels (e.g. ``"high_cardinality"``,
              ``"correlated_columns"``).
            - ``supports_gpu`` (:class:`bool`): Whether the generator can
              leverage GPU acceleration.
        """

    # ------------------------------------------------------------------
    # Concrete Batch Generation Helpers
    # ------------------------------------------------------------------

    def generate_batch(
        self,
        schema: dict[str, Any],
        profile: dict[str, Any],
        batch_size: int,
        **kwargs: Any,
    ) -> pd.DataFrame:
        """Generate a single batch of synthetic records.

        Delegates to :meth:`generate` with *batch_size* as the record
        count and extracts the ``data`` field from the resulting
        :class:`GenerationResult`.

        Args:
            schema: Table schema dictionary.
            profile: Statistical profile dictionary.
            batch_size: Number of records to produce in this batch.
            **kwargs: Forwarded to :meth:`generate`.

        Returns:
            A :class:`pandas.DataFrame` containing *batch_size* rows of
            synthetic data.

        Raises:
            GenerationError: Propagated from :meth:`generate`.
        """
        result: GenerationResult = self.generate(
            schema=schema,
            profile=profile,
            num_records=batch_size,
            **kwargs,
        )
        data = result.data
        if isinstance(data, pd.DataFrame):
            return data
        # Fallback: convert list-of-dicts or similar to DataFrame.
        return pd.DataFrame(data)

    def iterate_batches(
        self,
        schema: dict[str, Any],
        profile: dict[str, Any],
        total_records: int,
        batch_size: int = 10000,
        **kwargs: Any,
    ) -> Iterator[pd.DataFrame]:
        """Yield successive batches of generated data until *total_records* are produced.

        Calculates the number of full batches and an optional final
        partial batch, delegates each to :meth:`generate_batch`, and
        logs progress at each batch boundary.

        Args:
            schema: Table schema dictionary.
            profile: Statistical profile dictionary.
            total_records: Total number of records to generate across all
                batches.
            batch_size: Records per batch (default 10 000).
            **kwargs: Forwarded to :meth:`generate_batch`.

        Yields:
            :class:`pandas.DataFrame` instances, one per batch.

        Raises:
            GenerationError: If any individual batch fails.
        """
        num_batches: int = math.ceil(total_records / batch_size)
        records_remaining: int = total_records

        self.logger.info(
            "batch_generation_started",
            total_records=total_records,
            batch_size=batch_size,
            num_batches=num_batches,
        )

        for batch_index in range(num_batches):
            current_batch_size: int = min(batch_size, records_remaining)
            self.logger.debug(
                "generating_batch",
                batch_index=batch_index + 1,
                batch_size=current_batch_size,
            )

            batch_df: pd.DataFrame = self.generate_batch(
                schema=schema,
                profile=profile,
                batch_size=current_batch_size,
                **kwargs,
            )

            records_remaining -= len(batch_df)
            self._log_progress(
                records_generated=total_records - records_remaining,
                total_records=total_records,
            )

            yield batch_df

        self.logger.info(
            "batch_generation_completed",
            total_records=total_records,
            batches_produced=num_batches,
        )

    # ------------------------------------------------------------------
    # Timer Helpers
    # ------------------------------------------------------------------

    def _start_timer(self) -> None:
        """Record a high-resolution start timestamp.

        Uses :func:`time.monotonic` for a clock that is immune to
        system-clock adjustments.
        """
        self._start_time = time.monotonic()

    def _stop_timer(self) -> float:
        """Return the elapsed seconds since :meth:`_start_timer`.

        Returns:
            Elapsed wall-clock seconds as a ``float``.  Returns ``0.0``
            if :meth:`_start_timer` was never called.
        """
        if self._start_time is None:
            self.logger.warning("stop_timer_called_without_start")
            return 0.0
        elapsed: float = time.monotonic() - self._start_time
        return elapsed

    # ------------------------------------------------------------------
    # Result Builder
    # ------------------------------------------------------------------

    def _build_result(
        self,
        data: pd.DataFrame,
        metadata: dict[str, Any] | None = None,
    ) -> GenerationResult:
        """Construct a :class:`GenerationResult` from a generated DataFrame.

        Auto-populates ``num_records``, ``columns``,
        ``generation_time_seconds``, ``started_at``, and
        ``completed_at``.  Callers can supply additional *metadata* which
        is merged into the result.

        Args:
            data: The generated :class:`pandas.DataFrame`.
            metadata: Optional dictionary of additional metadata.

        Returns:
            A fully-populated :class:`GenerationResult`.
        """
        elapsed: float = self._stop_timer()
        now_utc: datetime = datetime.now(UTC)

        # Compute a rough started_at from elapsed, falling back to now.
        started_at_utc: datetime = now_utc
        if elapsed > 0.0:
            started_at_utc = now_utc - timedelta(seconds=elapsed)

        result_metadata: dict[str, Any] = metadata if metadata is not None else {}
        result_metadata.setdefault(
            "memory_usage_bytes",
            int(data.memory_usage(deep=True).sum()),
        )

        return GenerationResult(
            data=data,
            num_records=len(data),
            columns=list(data.columns),
            metadata=result_metadata,
            generation_time_seconds=round(elapsed, 6),
            started_at=started_at_utc,
            completed_at=now_utc,
        )

    # ------------------------------------------------------------------
    # Schema Validation
    # ------------------------------------------------------------------

    def _validate_schema(self, schema: dict[str, Any]) -> list[ColumnSpec]:
        """Parse and validate a raw schema dictionary into :class:`ColumnSpec` instances.

        The incoming *schema* is expected to contain a ``"columns"`` key
        holding a list of column descriptor dictionaries.  Each descriptor
        is validated against the :class:`ColumnSpec` Pydantic model.

        Args:
            schema: Raw schema dictionary.  Must include a ``"columns"``
                key with a list of column definitions.

        Returns:
            A list of validated :class:`ColumnSpec` objects.

        Raises:
            GenerationError: If the schema is missing the ``"columns"``
                key, the key is empty, or any column definition fails
                validation.
        """
        columns_raw = schema.get("columns")
        if columns_raw is None:
            raise GenerationError(
                message="Schema must contain a 'columns' key with column definitions.",
                method=self.__class__.__name__,
                details={"schema_keys": list(schema.keys())},
            )

        if not isinstance(columns_raw, list) or len(columns_raw) == 0:
            raise GenerationError(
                message="Schema 'columns' must be a non-empty list.",
                method=self.__class__.__name__,
                details={"columns_type": type(columns_raw).__name__},
            )

        column_specs: list[ColumnSpec] = []
        for idx, col_def in enumerate(columns_raw):
            try:
                if isinstance(col_def, ColumnSpec):
                    column_specs.append(col_def)
                elif isinstance(col_def, dict):
                    column_specs.append(ColumnSpec(**col_def))
                else:
                    raise GenerationError(
                        message=(
                            f"Column definition at index {idx} must be a dict "
                            f"or ColumnSpec, got {type(col_def).__name__}."
                        ),
                        method=self.__class__.__name__,
                        details={"index": idx},
                    )
            except GenerationError:
                raise
            except Exception as exc:
                raise GenerationError(
                    message=f"Invalid column definition at index {idx}: {exc}",
                    method=self.__class__.__name__,
                    details={"index": idx, "error": str(exc)},
                ) from exc

        self.logger.debug(
            "schema_validated",
            num_columns=len(column_specs),
            column_names=[cs.name for cs in column_specs],
        )
        return column_specs

    # ------------------------------------------------------------------
    # Null Injection
    # ------------------------------------------------------------------

    def _apply_nulls(
        self,
        df: pd.DataFrame,
        schema: dict[str, Any],
    ) -> pd.DataFrame:
        """Inject ``NULL`` values into *df* according to column-level null probabilities.

        For each column in the schema that is marked ``nullable=True``
        with a ``null_probability > 0.0``, a random mask is generated
        using NumPy and the column values at those positions are replaced
        with ``None``.

        The operation is performed **in-place** on a copy to preserve the
        caller's original DataFrame.

        Args:
            df: The :class:`pandas.DataFrame` to process.
            schema: Raw schema dictionary with a ``"columns"`` key.

        Returns:
            A new DataFrame with ``NULL`` values injected where appropriate.
        """
        result_df: pd.DataFrame = df.copy()
        columns_raw = schema.get("columns", [])

        for col_def in columns_raw:
            # Accept both ColumnSpec instances and raw dicts.
            if isinstance(col_def, ColumnSpec):
                col_name = col_def.name
                nullable = col_def.nullable
                null_prob = col_def.null_probability
            elif isinstance(col_def, dict):
                col_name = col_def.get("name", "")
                nullable = col_def.get("nullable", False)
                null_prob = col_def.get("null_probability", 0.0)
            else:
                continue

            if not nullable or null_prob <= 0.0:
                continue

            if col_name not in result_df.columns:
                continue

            num_rows: int = len(result_df)
            random_mask: np.ndarray = np.random.random(num_rows)
            null_positions = np.where(random_mask < null_prob)[0]

            if len(null_positions) > 0:
                result_df.loc[null_positions, col_name] = None
                self.logger.debug(
                    "nulls_applied",
                    column=col_name,
                    null_count=len(null_positions),
                    total_rows=num_rows,
                )

        return result_df

    # ------------------------------------------------------------------
    # Progress Logging
    # ------------------------------------------------------------------

    def _log_progress(
        self,
        records_generated: int,
        total_records: int,
    ) -> None:
        """Log generation progress at 10 % milestones.

        Updates the internal ``_records_generated`` counter and emits an
        ``info``-level log entry whenever the completion percentage
        crosses a 10 % boundary (10 %, 20 %, …, 100 %).

        Args:
            records_generated: Cumulative count of records produced so far.
            total_records: Target total record count.
        """
        previous_pct: int = int((self._records_generated / total_records) * 100) if total_records > 0 else 0
        self._records_generated = records_generated

        current_pct: int = int((records_generated / total_records) * 100) if total_records > 0 else 0

        # Emit a log at every 10 % boundary crossed since the last call.
        previous_milestone: int = (previous_pct // 10) * 10
        current_milestone: int = (current_pct // 10) * 10

        if current_milestone > previous_milestone:
            self.logger.info(
                "generation_progress",
                records_generated=records_generated,
                total_records=total_records,
                percent_complete=current_pct,
            )


# ---------------------------------------------------------------------------
# Type Alias for Generator Registry
# ---------------------------------------------------------------------------

GeneratorType = type[BaseGenerator]
"""Type alias for a :class:`BaseGenerator` subclass reference.

Used by the generator registry and method selector to hold references to
concrete generator *classes* (not instances) that can be instantiated
on demand::

    registry: Dict[str, GeneratorType] = {
        "ai_ml": AIMLGenerator,
        "statistical": StatisticalGenerator,
    }
"""
