"""Abstract base validator class for the Quality Service weighted scoring model.

This module defines the foundational components of the Strategy pattern used by
the Quality Service to validate generated synthetic data against multiple
quality criteria.  It provides:

- :class:`ValidationResult` — A Pydantic 2.x model standardising validator
  output with normalised scores, pass/fail status, detailed metrics, and
  per-field breakdowns.
- :class:`BaseValidator` — An abstract base class defining the contract that
  all concrete validators must implement.

The weighted scoring model aggregates results from three validators::

    Q = 0.4 * S_statistical + 0.3 * S_business_rules + 0.3 * S_referential_integrity

Each validator produces a :class:`ValidationResult` with a score normalised to
[0.0, 1.0].  The :class:`QualityScorer` (in the scoring package) computes the
composite quality score from these results, targeting ≥ 95 % fidelity.

Concrete implementations:
    - ``StatisticalValidator``           — weight 0.4 (distribution / moment fidelity)
    - ``BusinessRulesValidator``         — weight 0.3 (ERP business-rule compliance)
    - ``ReferentialIntegrityValidator``  — weight 0.3 (FK / cross-table integrity)

Usage::

    from quality_service.validators.base import BaseValidator, ValidationResult

    class MyValidator(BaseValidator):
        def validate(self, generated_data, profile):
            # ... validation logic ...
            return self.create_result(score=0.97, details={...})

        def get_weight(self) -> float:
            return 0.4

        def get_name(self) -> str:
            return "my_validator"
"""

from __future__ import annotations

import time
from abc import ABC, abstractmethod
from typing import TYPE_CHECKING, Any

from pydantic import BaseModel, Field, field_validator

from shared.logging.structured_logger import get_logger


if TYPE_CHECKING:
    import pandas as pd


# ---------------------------------------------------------------------------
# ValidationResult — standardised output model for every quality validator
# ---------------------------------------------------------------------------


class ValidationResult(BaseModel):
    """Standardised output produced by every quality validator.

    Encapsulates the complete result of a single validation pass, including
    the normalised score (0.0-1.0), pass/fail determination against a
    configurable threshold, detailed per-column or per-rule metric
    breakdowns, error / warning lists, and execution metadata.

    The :class:`QualityScorer` aggregates multiple ``ValidationResult``
    instances to compute the composite quality score via the weighted
    formula.

    Attributes:
        validator_name: Human-readable name of the validator that produced
            this result (e.g. ``"statistical_fidelity"``).
        score: Normalised quality score in [0.0, 1.0] where 1.0 is perfect.
        weight: This validator's weight in the composite quality score
            (e.g. 0.4 for statistical, 0.3 for business rules).
        weighted_score: Pre-computed ``score * weight`` for aggregation.
        passed: ``True`` when ``score >= threshold``.
        threshold: Minimum acceptable score (default 0.95 per the platform
            quality requirement).
        details: Detailed per-column or per-rule metric breakdown dictionary.
        errors: Specific validation error descriptions.
        warnings: Non-critical warnings that do not affect the score.
        metadata: Additional context (timestamps, data shape, profile
            version, etc.).
        records_validated: Total number of records examined.
        records_passed: Number of records that passed all checks.
        execution_time_ms: Wall-clock time for the validation run (ms).

    Example::

        result = ValidationResult(
            validator_name="statistical_fidelity",
            score=0.97,
            weight=0.4,
            weighted_score=0.388,
            passed=True,
            threshold=0.95,
            details={"col_a": {"ks_pvalue": 0.82}},
            records_validated=10000,
            records_passed=9700,
        )
        assert result.pass_rate == 0.97
        assert result.model_dump()["score"] == 0.97
    """

    # Allow dict[str, Any] values and other non-strict types to flow
    # through without Pydantic raising for arbitrary nested structures.
    model_config = {"arbitrary_types_allowed": True}

    # ---- Core identification ----
    validator_name: str = Field(
        ...,
        description="Name of the validator that produced this result.",
    )

    # ---- Scoring ----
    score: float = Field(
        ...,
        ge=0.0,
        le=1.0,
        description=(
            "Normalised quality score in [0.0, 1.0] where 1.0 is perfect."
        ),
    )
    weight: float = Field(
        ...,
        ge=0.0,
        le=1.0,
        description="Validator weight in the composite quality score.",
    )
    weighted_score: float = Field(
        default=0.0,
        description="Computed as score * weight.",
    )
    passed: bool = Field(
        default=False,
        description="Whether the score meets the minimum threshold.",
    )
    threshold: float = Field(
        default=0.95,
        ge=0.0,
        le=1.0,
        description="Minimum acceptable score (default 0.95).",
    )

    # ---- Detail payloads ----
    details: dict[str, Any] = Field(
        default_factory=dict,
        description="Detailed per-column or per-rule metric breakdown.",
    )
    errors: list[str] = Field(
        default_factory=list,
        description="List of specific validation errors found.",
    )
    warnings: list[str] = Field(
        default_factory=list,
        description="List of non-critical validation warnings.",
    )
    metadata: dict[str, Any] = Field(
        default_factory=dict,
        description=(
            "Additional context (timestamp, data shape, profile version)."
        ),
    )

    # ---- Record-level metrics ----
    records_validated: int = Field(
        default=0,
        ge=0,
        description="Number of records processed.",
    )
    records_passed: int = Field(
        default=0,
        ge=0,
        description="Number of records passing all checks.",
    )
    execution_time_ms: float = Field(
        default=0.0,
        ge=0.0,
        description="Time taken for validation in milliseconds.",
    )

    # ------------------------------------------------------------------
    # Pydantic 2.x field validators
    # ------------------------------------------------------------------

    @field_validator("score")
    @classmethod
    def _score_in_range(cls, value: float) -> float:
        """Enforce that score is strictly within [0.0, 1.0]."""
        if not 0.0 <= value <= 1.0:
            raise ValueError(
                f"score must be in [0.0, 1.0], got {value}"
            )
        return value

    @field_validator("weight")
    @classmethod
    def _weight_in_range(cls, value: float) -> float:
        """Enforce that weight is strictly within [0.0, 1.0]."""
        if not 0.0 <= value <= 1.0:
            raise ValueError(
                f"weight must be in [0.0, 1.0], got {value}"
            )
        return value

    @field_validator("records_validated")
    @classmethod
    def _records_validated_non_negative(cls, value: int) -> int:
        """Enforce that records_validated is non-negative."""
        if value < 0:
            raise ValueError(
                f"records_validated must be >= 0, got {value}"
            )
        return value

    @field_validator("records_passed")
    @classmethod
    def _records_passed_le_validated(
        cls,
        value: int,
        info: Any,
    ) -> int:
        """Ensure records_passed does not exceed records_validated.

        Because Pydantic 2.x validates fields in declaration order,
        ``records_validated`` is guaranteed to be present in
        ``info.data`` by the time this validator runs.
        """
        records_validated: int = info.data.get("records_validated", 0)
        if value > records_validated:
            raise ValueError(
                f"records_passed ({value}) cannot exceed "
                f"records_validated ({records_validated})"
            )
        return value

    # ------------------------------------------------------------------
    # Computed properties
    # ------------------------------------------------------------------

    @property
    def pass_rate(self) -> float:
        """Fraction of records that passed validation.

        Returns:
            ``records_passed / records_validated`` when
            ``records_validated > 0``, otherwise ``0.0`` to avoid
            division-by-zero.
        """
        if self.records_validated > 0:
            return self.records_passed / self.records_validated
        return 0.0


# ---------------------------------------------------------------------------
# BaseValidator — abstract Strategy interface for the quality scoring system
# ---------------------------------------------------------------------------


class BaseValidator(ABC):
    """Abstract base class defining the contract for quality validators.

    Implements the **Strategy** pattern so that the :class:`QualityScorer`
    can invoke any concrete validator polymorphically.  All concrete
    validators **must** implement:

    - :meth:`validate` — core validation logic
    - :meth:`get_weight` — weight in the composite score
    - :meth:`get_name` — human-readable identifier

    In addition, two concrete helper methods are provided:

    - :meth:`create_result` — factory for building a ``ValidationResult``
      with common fields pre-populated.
    - :meth:`validate_with_timing` — wrapper that automatically measures
      execution time and handles exceptions gracefully.

    Args:
        config: Optional dictionary of validator-specific settings.  When
            ``None``, sensible defaults are applied.  Recognised keys:

            - ``minimum_threshold`` (float): Minimum acceptable quality
              score.  Defaults to ``0.95``.

    Attributes:
        config: The (possibly empty) configuration dictionary.
        logger: Structured logger instance bound to this validator's class
            name, provided by
            :func:`shared.logging.structured_logger.get_logger`.
        minimum_threshold: The score floor below which validation is
            considered failed.  Defaults to ``0.95``.
    """

    def __init__(self, config: dict[str, Any] | None = None) -> None:
        self.config: dict[str, Any] = config if config is not None else {}
        self.logger = get_logger(self.__class__.__name__)
        self.minimum_threshold: float = float(
            self.config.get("minimum_threshold", 0.95)
        )

    # ------------------------------------------------------------------
    # Abstract methods — subclasses MUST implement these
    # ------------------------------------------------------------------

    @abstractmethod
    def validate(
        self,
        generated_data: pd.DataFrame | dict[str, pd.DataFrame],
        profile: dict[str, Any],
    ) -> ValidationResult:
        """Run all validation checks against generated data.

        This is the core method that every concrete validator **must**
        implement.  It should execute the full battery of checks and
        return a :class:`ValidationResult` summarising the outcome.

        Args:
            generated_data: Either a single :class:`pd.DataFrame`
                (single-table validation) or a ``dict[str, pd.DataFrame]``
                mapping table names to DataFrames (multi-table validation).
            profile: Statistical or schema profile metadata from the
                Profiling Service.  The exact structure varies per
                validator.

        Returns:
            A fully populated :class:`ValidationResult`.
        """
        ...  # pragma: no cover

    @abstractmethod
    def get_weight(self) -> float:
        """Return this validator's weight in the composite quality score.

        Standard weights defined by the scoring model:

        ============================================  ======
        Validator                                     Weight
        ============================================  ======
        ``StatisticalValidator``                      0.4
        ``BusinessRulesValidator``                    0.3
        ``ReferentialIntegrityValidator``             0.3
        ============================================  ======

        Returns:
            A ``float`` in [0.0, 1.0].
        """
        ...  # pragma: no cover

    @abstractmethod
    def get_name(self) -> str:
        """Return a human-readable validator name for logging and reporting.

        Examples: ``"statistical_fidelity"``, ``"business_rules"``,
        ``"referential_integrity"``.

        Returns:
            A descriptive, snake_case string identifier.
        """
        ...  # pragma: no cover

    # ------------------------------------------------------------------
    # Concrete methods — shared implementation for all subclasses
    # ------------------------------------------------------------------

    def create_result(
        self,
        score: float,
        details: dict[str, Any],
        errors: list[str] | None = None,
        warnings: list[str] | None = None,
        records_validated: int = 0,
        records_passed: int = 0,
        execution_time_ms: float = 0.0,
    ) -> ValidationResult:
        """Factory method to construct a :class:`ValidationResult`.

        Automatically populates ``validator_name``, ``weight``,
        ``weighted_score``, ``passed``, and ``threshold`` from the
        current validator's state so that subclasses only need to provide
        the variable scoring data.

        The *score* is clamped to [0.0, 1.0] before assignment to guard
        against floating-point drift in aggregation routines.

        Args:
            score: Normalised quality score (will be clamped to
                [0.0, 1.0]).
            details: Per-column or per-rule metric breakdown dictionary.
            errors: Optional list of validation error descriptions.
            warnings: Optional list of non-critical warnings.
            records_validated: Total records examined.
            records_passed: Records passing all checks.
            execution_time_ms: Elapsed validation time in milliseconds.

        Returns:
            A fully populated :class:`ValidationResult` instance.
        """
        weight = self.get_weight()
        # Clamp to valid range to protect against FP accumulation errors.
        clamped_score = max(0.0, min(1.0, score))

        return ValidationResult(
            validator_name=self.get_name(),
            score=clamped_score,
            weight=weight,
            weighted_score=clamped_score * weight,
            passed=clamped_score >= self.minimum_threshold,
            threshold=self.minimum_threshold,
            details=details if details is not None else {},
            errors=errors if errors is not None else [],
            warnings=warnings if warnings is not None else [],
            metadata={},
            records_validated=records_validated,
            records_passed=records_passed,
            execution_time_ms=execution_time_ms,
        )

    def validate_with_timing(
        self,
        generated_data: pd.DataFrame | dict[str, pd.DataFrame],
        profile: dict[str, Any],
    ) -> ValidationResult:
        """Execute :meth:`validate` and automatically measure execution time.

        Wraps the abstract ``validate()`` with high-resolution timing via
        :func:`time.perf_counter` and structured logging of start,
        completion, and any exceptions.

        On failure the method returns a **zero-score**
        :class:`ValidationResult` so that the composite scorer degrades
        gracefully rather than propagating an unhandled exception.

        Args:
            generated_data: Single-table DataFrame or multi-table mapping.
            profile: Statistical / schema profile metadata.

        Returns:
            A :class:`ValidationResult` with ``execution_time_ms``
            populated.  On error, the result carries ``score=0.0`` and
            the exception details in ``errors``.
        """
        validator_name = self.get_name()

        self.logger.info(
            "validation_started",
            validator=validator_name,
            weight=self.get_weight(),
            threshold=self.minimum_threshold,
        )

        start = time.perf_counter()
        try:
            result = self.validate(generated_data, profile)

            elapsed_ms = (time.perf_counter() - start) * 1000.0

            # Guarantee that timing is recorded even when the subclass
            # built the result without passing execution_time_ms.
            if result.execution_time_ms == 0.0:
                result = result.model_copy(
                    update={"execution_time_ms": elapsed_ms},
                )

            self.logger.info(
                "validation_completed",
                validator=validator_name,
                score=result.score,
                weighted_score=result.weighted_score,
                passed=result.passed,
                execution_time_ms=round(elapsed_ms, 3),
                records_validated=result.records_validated,
                records_passed=result.records_passed,
            )

            return result

        except Exception as exc:
            elapsed_ms = (time.perf_counter() - start) * 1000.0

            self.logger.error(
                "validation_failed",
                validator=validator_name,
                error=str(exc),
                error_type=type(exc).__name__,
                execution_time_ms=round(elapsed_ms, 3),
                exc_info=True,
            )

            # Return a zero-score result so that the composite scorer can
            # still compute an aggregate without raising.
            return self.create_result(
                score=0.0,
                details={
                    "error": str(exc),
                    "error_type": type(exc).__name__,
                },
                errors=[f"Validation failed: {exc}"],
                warnings=[],
                records_validated=0,
                records_passed=0,
                execution_time_ms=elapsed_ms,
            )
