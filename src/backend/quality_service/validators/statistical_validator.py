"""Statistical fidelity validator for the quality scoring model.

This module implements the :class:`StatisticalValidator`, responsible for the
**40 % weighted** statistical fidelity component of the composite quality
score.  It validates that generated synthetic data matches the statistical
distributions, value ranges, means, standard deviations, and cardinality
of source statistical profiles captured by the Profiling Service.

Scoring model::

    Q = 0.4 * S_statistical + 0.3 * S_business_rules + 0.3 * S_referential_integrity

The validator employs three complementary comparison strategies:

1. **Hypothesis testing** — Two-sample Kolmogorov-Smirnov test (numerical)
   and chi-squared test (categorical) via SciPy.
2. **Moment comparison** — Weighted relative-error comparison of mean (40 %),
   standard deviation (30 %), skewness (15 %), and kurtosis (15 %).
3. **Declarative expectations** — Great Expectations 0.18.x in-memory
   validation for range, mean, stdev, and distinct-value checks.

Per-column sub-scores are aggregated using configurable weights (defaults:
distribution 30 %, moments 25 %, ranges 20 %, cardinality 15 %, null
ratio 10 %) and then blended with the Great Expectations pass rate.

Usage::

    from quality_service.validators.statistical_validator import StatisticalValidator

    validator = StatisticalValidator(config={"minimum_threshold": 0.95})
    result = validator.validate(generated_df, profile_dict)
    assert result.score >= 0.95
"""

from __future__ import annotations

import math
from typing import TYPE_CHECKING, Any

import numpy as np
from great_expectations.core import ExpectationSuite
from great_expectations.data_context import EphemeralDataContext  # type: ignore[attr-defined]
from great_expectations.data_context.types.base import (
    DataContextConfig,
    InMemoryStoreBackendDefaults,
)
from scipy import stats

from quality_service.validators.base import BaseValidator, ValidationResult
from shared.logging.structured_logger import get_logger


if TYPE_CHECKING:
    import pandas as pd


# ---------------------------------------------------------------------------
# Module-level logger (structured JSON, correlation-ID-aware)
# ---------------------------------------------------------------------------
logger = get_logger(__name__)

# ---------------------------------------------------------------------------
# Default configuration constants
# ---------------------------------------------------------------------------

# Hypothesis-test thresholds
_DEFAULT_KS_PVALUE_THRESHOLD: float = 0.05
_DEFAULT_MOMENT_TOLERANCE: float = 0.10
_DEFAULT_RANGE_TOLERANCE: float = 0.05
_DEFAULT_CARDINALITY_TOLERANCE: float = 0.15
_DEFAULT_NULL_RATIO_TOLERANCE: float = 0.05

# Moment comparison weights (must sum to 1.0)
_MOMENT_WEIGHT_MEAN: float = 0.40
_MOMENT_WEIGHT_STD: float = 0.30
_MOMENT_WEIGHT_SKEWNESS: float = 0.15
_MOMENT_WEIGHT_KURTOSIS: float = 0.15

# Per-column sub-validation weights (must sum to 1.0)
_SUBVALIDATION_WEIGHT_DISTRIBUTION: float = 0.30
_SUBVALIDATION_WEIGHT_MOMENTS: float = 0.25
_SUBVALIDATION_WEIGHT_RANGES: float = 0.20
_SUBVALIDATION_WEIGHT_CARDINALITY: float = 0.15
_SUBVALIDATION_WEIGHT_NULL_RATIO: float = 0.10

# Blending weight for Great Expectations result in the final score
_DEFAULT_GE_BLEND_WEIGHT: float = 0.10

# Minimum number of data points required for meaningful statistical tests
_MIN_SAMPLE_SIZE: int = 2

# Categorical data type identifiers
_CATEGORICAL_TYPES: frozenset[str] = frozenset(
    {"categorical", "string", "boolean", "object", "category"}
)


# ---------------------------------------------------------------------------
# StatisticalValidator — 40 % of composite quality score
# ---------------------------------------------------------------------------


class StatisticalValidator(BaseValidator):
    """Statistical fidelity validator (weight = 0.4).

    Validates that the generated synthetic data reproduces the statistical
    properties of the source ERP data as captured in the statistical profile.

    Five per-column checks are performed:

    * **Distribution** — KS test (numerical) / χ² test (categorical).
    * **Moments** — Relative-error comparison of mean, std, skewness, kurtosis.
    * **Value ranges** — Min/max bounds and percentile-boundary compliance.
    * **Cardinality** — Unique-value count and distinct-value-set coverage.
    * **Null ratio** — Null / missing-value percentage similarity.

    An additional Great Expectations validation suite is dynamically built
    from the profile and executed against the generated data to supplement
    the per-column scores.

    Args:
        config: Optional configuration dictionary.  Recognised keys:

            - ``ks_pvalue_threshold`` (float, default 0.05)
            - ``moment_tolerance`` (float, default 0.10)
            - ``range_tolerance`` (float, default 0.05)
            - ``cardinality_tolerance`` (float, default 0.15)
            - ``null_ratio_tolerance`` (float, default 0.05)
            - ``minimum_threshold`` (float, default 0.95)
            - ``ge_blend_weight`` (float, default 0.10)
            - ``distribution_weight`` / ``moments_weight`` /
              ``ranges_weight`` / ``cardinality_weight`` /
              ``null_ratio_weight`` — per-column sub-validation weights.
            - ``column_weights`` (dict[str, float]) — per-column
              importance weights for the final aggregation.

    Attributes:
        weight: The validator's weight in the composite quality score (0.4).
    """

    # Class-level weight constant exposed for external access
    weight: float = 0.4

    # ------------------------------------------------------------------
    # Construction
    # ------------------------------------------------------------------

    def __init__(self, config: dict[str, Any] | None = None) -> None:
        """Initialise the validator with thresholds and GE context.

        Args:
            config: Optional settings dictionary (see class docstring).
        """
        super().__init__(config=config)

        # --- statistical comparison tolerances ---
        self._ks_pvalue_threshold: float = float(
            self.config.get("ks_pvalue_threshold", _DEFAULT_KS_PVALUE_THRESHOLD)
        )
        self._moment_tolerance: float = float(
            self.config.get("moment_tolerance", _DEFAULT_MOMENT_TOLERANCE)
        )
        self._range_tolerance: float = float(
            self.config.get("range_tolerance", _DEFAULT_RANGE_TOLERANCE)
        )
        self._cardinality_tolerance: float = float(
            self.config.get("cardinality_tolerance", _DEFAULT_CARDINALITY_TOLERANCE)
        )
        self._null_ratio_tolerance: float = float(
            self.config.get("null_ratio_tolerance", _DEFAULT_NULL_RATIO_TOLERANCE)
        )

        # --- sub-validation weights ---
        self._dist_weight: float = float(
            self.config.get("distribution_weight", _SUBVALIDATION_WEIGHT_DISTRIBUTION)
        )
        self._moment_sub_weight: float = float(
            self.config.get("moments_weight", _SUBVALIDATION_WEIGHT_MOMENTS)
        )
        self._range_weight: float = float(
            self.config.get("ranges_weight", _SUBVALIDATION_WEIGHT_RANGES)
        )
        self._cardinality_weight: float = float(
            self.config.get("cardinality_weight", _SUBVALIDATION_WEIGHT_CARDINALITY)
        )
        self._null_weight: float = float(
            self.config.get("null_ratio_weight", _SUBVALIDATION_WEIGHT_NULL_RATIO)
        )

        # --- Great Expectations context (in-memory) ---
        self._ge_context: EphemeralDataContext | None = None
        self._initialize_ge_context()

        self.logger.info(
            "statistical_validator_initialized",
            ks_pvalue_threshold=self._ks_pvalue_threshold,
            moment_tolerance=self._moment_tolerance,
            minimum_threshold=self.minimum_threshold,
            weight=self.weight,
        )

    def _initialize_ge_context(self) -> None:
        """Create an ephemeral (in-memory) Great Expectations DataContext.

        Falls back gracefully if the GE library is unavailable or
        context construction fails, logging a warning rather than
        raising so that statistical validation can proceed without GE.
        """
        try:
            ge_config = DataContextConfig(
                store_backend_defaults=InMemoryStoreBackendDefaults(),
            )
            self._ge_context = EphemeralDataContext(project_config=ge_config)
            self.logger.info("great_expectations_context_initialized")
        except Exception as exc:
            self.logger.warning(
                "great_expectations_context_init_failed",
                error=str(exc),
                error_type=type(exc).__name__,
            )
            self._ge_context = None

    # ------------------------------------------------------------------
    # Abstract-method implementations (Strategy interface)
    # ------------------------------------------------------------------

    def get_weight(self) -> float:
        """Return ``0.4`` — 40 % of the composite quality score."""
        return 0.4

    def get_name(self) -> str:
        """Return the validator's human-readable identifier."""
        return "statistical_fidelity"

    def validate(
        self,
        generated_data: pd.DataFrame | dict[str, pd.DataFrame],
        profile: dict[str, Any],
    ) -> ValidationResult:
        """Run full statistical fidelity validation.

        Iterates through every column present in both the generated
        ``DataFrame`` and the source statistical profile, running five
        sub-validations per column.  An optional Great Expectations suite
        is executed to supplement the scores.

        Args:
            generated_data: Synthetic data to validate.  Accepts a single
                DataFrame or a dict of table-name to DataFrame.  When a dict
                is provided, the first DataFrame is used for statistical
                profiling (single-table mode).
            profile: Statistical profile from the Profiling Service.
                Expected to contain a ``"columns"`` key mapping column
                names to per-column statistical metadata dictionaries.

        Returns:
            A :class:`ValidationResult` with an aggregate fidelity score,
            per-column breakdowns, errors, and warnings.
        """
        # When a dict of tables is provided, extract the first DataFrame
        # so column-level statistical validation can proceed in single-table
        # mode.
        if isinstance(generated_data, dict):
            if not generated_data:
                return self.create_result(
                    score=0.0,
                    details={"error": "Empty dict of DataFrames provided"},
                    errors=["No tables provided for statistical validation"],
                    records_validated=0,
                    records_passed=0,
                )
            generated_data = next(iter(generated_data.values()))

        profile_columns: dict[str, Any] = profile.get("columns", {})

        # Guard: empty profile
        if not profile_columns:
            self.logger.warning(
                "empty_profile_columns",
                profile_keys=list(profile.keys()),
            )
            return self.create_result(
                score=0.0,
                details={"error": "No column profiles available"},
                errors=["Statistical profile contains no column metadata"],
                records_validated=len(generated_data),
                records_passed=0,
            )

        total_records: int = len(generated_data)
        generated_columns: set[str] = set(generated_data.columns)
        profile_column_names: set[str] = set(profile_columns.keys())
        columns_to_validate: set[str] = generated_columns & profile_column_names

        warnings: list[str] = []
        if not columns_to_validate:
            warnings.append(
                f"No overlapping columns between generated data "
                f"({len(generated_columns)} cols) and profile "
                f"({len(profile_column_names)} cols)"
            )
            return self.create_result(
                score=0.0,
                details={"warning": "No overlapping columns for validation"},
                warnings=warnings,
                records_validated=total_records,
                records_passed=0,
            )

        # Report column coverage gaps and log start
        self._report_coverage_gaps(
            warnings, generated_columns, profile_column_names
        )
        missing_count = len(profile_column_names - generated_columns)
        extra_count = len(generated_columns - profile_column_names)
        self.logger.info(
            "statistical_validation_started",
            total_columns=len(columns_to_validate),
            total_records=total_records,
            missing_columns=missing_count,
            extra_columns=extra_count,
        )

        # ---- Per-column validation ----
        errors: list[str] = []
        column_details: dict[str, Any] = {}
        column_scores: dict[str, float] = {}
        self._run_column_validations(
            generated_data, profile_columns, columns_to_validate,
            errors, column_details, column_scores,
        )

        # ---- Great Expectations supplementary validation ----
        ge_score = self._run_ge_validation(
            generated_data, profile, column_details, warnings,
        )

        # ---- Final score aggregation ----
        return self._build_final_result(
            column_scores, column_details, ge_score,
            total_records, columns_to_validate,
            missing_count, extra_count, errors, warnings,
        )

    # ------------------------------------------------------------------
    # validate() helper: column coverage gap reporting
    # ------------------------------------------------------------------

    @staticmethod
    def _report_coverage_gaps(
        warnings: list[str],
        generated_columns: set[str],
        profile_column_names: set[str],
    ) -> None:
        """Append coverage-gap warnings for missing or extra columns."""
        missing_from_generated = profile_column_names - generated_columns
        if missing_from_generated:
            warnings.append(
                f"Columns in profile but absent from generated data: "
                f"{sorted(missing_from_generated)}"
            )
        extra_in_generated = generated_columns - profile_column_names
        if extra_in_generated:
            warnings.append(
                f"Columns in generated data but absent from profile: "
                f"{sorted(extra_in_generated)}"
            )

    # ------------------------------------------------------------------
    # validate() helper: per-column validation loop
    # ------------------------------------------------------------------

    def _run_column_validations(
        self,
        generated_data: pd.DataFrame,
        profile_columns: dict[str, Any],
        columns_to_validate: set[str],
        errors: list[str],
        column_details: dict[str, Any],
        column_scores: dict[str, float],
    ) -> None:
        """Run per-column five-check validation, populating results in-place."""
        for col_name in sorted(columns_to_validate):
            try:
                col_profile = profile_columns[col_name]
                generated_series: pd.Series = generated_data[col_name]

                col_detail = self._validate_column(
                    col_name, generated_series, col_profile
                )
                column_details[col_name] = col_detail
                column_scores[col_name] = col_detail.get("composite_score", 0.0)

                self.logger.debug(
                    "column_validation_complete",
                    column=col_name,
                    score=col_detail.get("composite_score", 0.0),
                )
            except Exception as exc:
                self.logger.error(
                    "column_validation_error",
                    column=col_name,
                    error=str(exc),
                    error_type=type(exc).__name__,
                )
                errors.append(f"Column '{col_name}': {exc}")
                column_details[col_name] = {
                    "error": str(exc),
                    "composite_score": 0.0,
                }
                column_scores[col_name] = 0.0

    # ------------------------------------------------------------------
    # validate() helper: Great Expectations supplementary validation
    # ------------------------------------------------------------------

    def _run_ge_validation(
        self,
        generated_data: pd.DataFrame,
        profile: dict[str, Any],
        column_details: dict[str, Any],
        warnings: list[str],
    ) -> float:
        """Execute GE supplementary validation and return the GE score."""
        if self._ge_context is None:
            return 1.0

        try:
            ge_suite = self._build_great_expectations_suite(profile)
            ge_results = self._run_great_expectations_validation(
                data=generated_data, suite=ge_suite
            )
            ge_score = float(ge_results.get("success_ratio", 1.0))
            column_details["great_expectations"] = ge_results
        except Exception as exc:
            self.logger.warning(
                "great_expectations_validation_failed",
                error=str(exc),
                error_type=type(exc).__name__,
            )
            warnings.append(
                f"Great Expectations validation skipped: {exc}"
            )
            ge_score = 1.0  # do not penalise when GE is unavailable
        return ge_score

    # ------------------------------------------------------------------
    # validate() helper: final result construction
    # ------------------------------------------------------------------

    def _build_final_result(
        self,
        column_scores: dict[str, float],
        column_details: dict[str, Any],
        ge_score: float,
        total_records: int,
        columns_to_validate: set[str],
        missing_count: int,
        extra_count: int,
        errors: list[str],
        warnings: list[str],
    ) -> ValidationResult:
        """Aggregate column scores, blend with GE, and build the result."""
        statistical_score = self._aggregate_column_scores(column_scores)
        ge_blend_weight = float(
            self.config.get("ge_blend_weight", _DEFAULT_GE_BLEND_WEIGHT)
        )
        final_score = float(
            np.clip(
                (1.0 - ge_blend_weight) * statistical_score
                + ge_blend_weight * ge_score,
                0.0,
                1.0,
            )
        )

        records_passed = int(total_records * final_score)

        details: dict[str, Any] = {
            "column_scores": column_scores,
            "column_details": column_details,
            "aggregate_statistical_score": float(statistical_score),
            "great_expectations_score": float(ge_score),
            "final_blended_score": float(final_score),
            "columns_validated": len(columns_to_validate),
            "columns_missing": missing_count,
            "columns_extra": extra_count,
        }

        self.logger.info(
            "statistical_validation_completed",
            final_score=final_score,
            statistical_score=statistical_score,
            ge_score=ge_score,
            columns_validated=len(columns_to_validate),
            records_validated=total_records,
            passed=final_score >= self.minimum_threshold,
        )

        return self.create_result(
            score=final_score,
            details=details,
            errors=errors,
            warnings=warnings,
            records_validated=total_records,
            records_passed=records_passed,
        )

    # ------------------------------------------------------------------
    # Per-column composite validation
    # ------------------------------------------------------------------

    def _validate_column(
        self,
        _col_name: str,
        generated_series: pd.Series,
        profile_column: dict[str, Any],
    ) -> dict[str, Any]:
        """Run all five sub-validations on a single column.

        Returns a detail dictionary including sub-scores and a weighted
        composite.
        """
        data_type: str = profile_column.get("data_type", "numerical")
        is_categorical: bool = data_type in _CATEGORICAL_TYPES

        dist_score = self._validate_distributions(
            generated_series, profile_column
        )

        # Moments are only meaningful for numerical data
        moments_score = (
            1.0
            if is_categorical
            else self._validate_moments(generated_series, profile_column)
        )

        range_score = self._validate_value_ranges(
            generated_series, profile_column
        )
        cardinality_score = self._validate_cardinality(
            generated_series, profile_column
        )
        null_score = self._validate_null_ratios(
            generated_series, profile_column
        )

        composite = float(
            np.clip(
                self._dist_weight * dist_score
                + self._moment_sub_weight * moments_score
                + self._range_weight * range_score
                + self._cardinality_weight * cardinality_score
                + self._null_weight * null_score,
                0.0,
                1.0,
            )
        )

        return {
            "data_type": data_type,
            "distribution_score": float(dist_score),
            "moments_score": float(moments_score),
            "range_score": float(range_score),
            "cardinality_score": float(cardinality_score),
            "null_ratio_score": float(null_score),
            "composite_score": composite,
        }

    # ------------------------------------------------------------------
    # 1. Distribution validation
    # ------------------------------------------------------------------

    def _validate_distributions(
        self,
        generated_series: pd.Series,
        profile_column: dict[str, Any],
    ) -> float:
        """Compare generated vs profiled data distribution.

        * **Numerical** → two-sample Kolmogorov-Smirnov test
          (:func:`scipy.stats.ks_2samp`).
        * **Categorical** → chi-squared goodness-of-fit test
          (:func:`scipy.stats.chisquare`).

        Args:
            generated_series: Column from the generated DataFrame.
            profile_column: Per-column profile metadata.

        Returns:
            Similarity score normalised to [0.0, 1.0].
        """
        data_type: str = profile_column.get("data_type", "numerical")
        is_categorical: bool = data_type in _CATEGORICAL_TYPES

        try:
            if is_categorical:
                return self._compare_categorical_distribution(
                    generated_series, profile_column
                )
            return self._compare_numerical_distribution(
                generated_series, profile_column
            )
        except Exception as exc:
            self.logger.warning(
                "distribution_validation_error",
                column=str(generated_series.name),
                error=str(exc),
            )
            return 0.0

    def _compare_numerical_distribution(
        self,
        generated_series: pd.Series,
        profile_column: dict[str, Any],
    ) -> float:
        """Two-sample KS test for numerical distributions.

        When the profile contains ``sample_values`` from the source, a
        direct two-sample KS test is performed.  Otherwise, a synthetic
        reference sample is drawn from a normal distribution fitted to
        the profiled mean and standard deviation.
        """
        clean_generated = generated_series.dropna()
        if len(clean_generated) < _MIN_SAMPLE_SIZE:
            return 0.0

        generated_values = clean_generated.values.astype(np.float64)

        sample_values = profile_column.get("sample_values")
        if sample_values is not None and len(sample_values) >= _MIN_SAMPLE_SIZE:
            reference_array = np.array(sample_values, dtype=np.float64)
            ks_statistic, p_value = stats.ks_2samp(
                generated_values, reference_array
            )
        else:
            profiled_mean = profile_column.get("mean")
            profiled_std = profile_column.get("std")

            if (
                profiled_mean is not None
                and profiled_std is not None
                and float(profiled_std) > 0.0
            ):
                rng = np.random.default_rng(42)
                reference_data = rng.normal(
                    loc=float(profiled_mean),
                    scale=float(profiled_std),
                    size=len(clean_generated),
                )
                ks_statistic, p_value = stats.ks_2samp(
                    generated_values, reference_data
                )
            else:
                # Insufficient data for comparison — return neutral score
                return 0.5

        score = self._pvalue_to_score(p_value)

        self.logger.debug(
            "ks_test_result",
            column=str(generated_series.name),
            ks_statistic=float(ks_statistic),
            p_value=float(p_value),
            score=score,
        )
        return score

    def _compare_categorical_distribution(
        self,
        generated_series: pd.Series,
        profile_column: dict[str, Any],
    ) -> float:
        """Chi-squared goodness-of-fit test for categorical distributions.

        Aligns categories across generated and profiled data, constructs
        observed / expected frequency vectors, and computes the χ² test.
        """
        profiled_distribution: dict[str, Any] = profile_column.get(
            "frequency_distribution", {}
        )

        if not profiled_distribution:
            distinct_values = profile_column.get("distinct_values", [])
            if distinct_values:
                uniform_freq = 1.0 / max(len(distinct_values), 1)
                profiled_distribution = {
                    str(v): uniform_freq for v in distinct_values
                }
            else:
                return 0.5

        clean_generated = generated_series.dropna()
        if len(clean_generated) == 0:
            return 0.0

        generated_counts = clean_generated.value_counts()

        # Build unified category set
        all_categories = sorted(
            {str(k) for k in profiled_distribution}
            | {str(v) for v in generated_counts.index}
        )

        if len(all_categories) < _MIN_SAMPLE_SIZE:
            return 1.0  # single category - trivially matching

        total_generated = float(np.sum(np.asarray(generated_counts.values)))
        total_profiled = float(np.sum(np.asarray(list(profiled_distribution.values()))))

        if total_profiled == 0.0 or total_generated == 0.0:
            return 0.0

        observed = np.array(
            [float(generated_counts.get(cat, 0)) for cat in all_categories],
            dtype=np.float64,
        )
        expected_raw = np.array(
            [float(profiled_distribution.get(cat, 0)) for cat in all_categories],
            dtype=np.float64,
        )

        # Normalise expected frequencies to match generated sample size
        expected = (expected_raw / total_profiled) * total_generated

        # Prevent division-by-zero in chi-squared by clamping minimum
        min_expected = 0.001 * total_generated / max(len(all_categories), 1)
        expected = np.clip(expected, min_expected, None)

        try:
            chi2_stat, p_value = stats.chisquare(f_obs=observed, f_exp=expected)
        except Exception:
            return 0.5

        score = self._pvalue_to_score(p_value)

        self.logger.debug(
            "chi_squared_test_result",
            column=str(generated_series.name),
            chi2_statistic=float(chi2_stat),
            p_value=float(p_value),
            score=score,
            n_categories=len(all_categories),
        )
        return score

    # ------------------------------------------------------------------
    # 2. Moment comparison
    # ------------------------------------------------------------------

    def _validate_moments(
        self,
        generated_series: pd.Series,
        profile_column: dict[str, Any],
    ) -> float:
        """Compare statistical moments of generated vs profiled data.

        Moments compared and their weights:

        - **Mean** — 40 %
        - **Standard deviation** — 30 %
        - **Skewness** — 15 %
        - **Kurtosis** — 15 %

        Uses relative-error comparison with configurable tolerance.

        Args:
            generated_series: Numerical column from generated data.
            profile_column: Per-column profile metadata.

        Returns:
            Moments similarity score in [0.0, 1.0].
        """
        clean_generated = generated_series.dropna()
        if len(clean_generated) < _MIN_SAMPLE_SIZE:
            return 0.0

        gen_mean = float(clean_generated.mean())
        gen_std = float(clean_generated.std())
        gen_skew = float(clean_generated.skew())  # type: ignore[arg-type]
        gen_kurtosis = float(clean_generated.kurtosis())  # type: ignore[arg-type]

        prof_mean = profile_column.get("mean")
        prof_std = profile_column.get("std")
        prof_skew = profile_column.get("skewness")
        prof_kurtosis = profile_column.get("kurtosis")

        moment_scores: list[tuple[float, float]] = []

        if prof_mean is not None:
            score = self._compute_relative_similarity(
                gen_mean, float(prof_mean), self._moment_tolerance
            )
            moment_scores.append((_MOMENT_WEIGHT_MEAN, score))

        if prof_std is not None:
            score = self._compute_relative_similarity(
                gen_std, float(prof_std), self._moment_tolerance
            )
            moment_scores.append((_MOMENT_WEIGHT_STD, score))

        if prof_skew is not None:
            score = self._compute_relative_similarity(
                gen_skew,
                float(prof_skew),
                self._moment_tolerance * 2.0,
            )
            moment_scores.append((_MOMENT_WEIGHT_SKEWNESS, score))

        if prof_kurtosis is not None:
            score = self._compute_relative_similarity(
                gen_kurtosis,
                float(prof_kurtosis),
                self._moment_tolerance * 2.0,
            )
            moment_scores.append((_MOMENT_WEIGHT_KURTOSIS, score))

        if not moment_scores:
            return 0.5  # neutral when no profiled moments available

        weights = np.array([w for w, _ in moment_scores], dtype=np.float64)
        scores = np.array([s for _, s in moment_scores], dtype=np.float64)

        weight_sum = float(np.sum(weights))
        result = (
            float(np.average(scores, weights=weights))
            if weight_sum > 0.0
            else float(np.mean(scores))
        )

        self.logger.debug(
            "moments_validation_result",
            column=str(generated_series.name),
            generated_mean=gen_mean,
            generated_std=gen_std,
            generated_skew=gen_skew,
            generated_kurtosis=gen_kurtosis,
            score=result,
        )

        return float(np.clip(result, 0.0, 1.0))

    @staticmethod
    def _compute_relative_similarity(
        generated_value: float,
        profiled_value: float,
        tolerance: float,
    ) -> float:
        """Score similarity using relative error with smooth decay.

        Returns ``1.0`` when values match exactly, degrading smoothly
        toward ``0.0`` as relative difference exceeds *tolerance*.

        Uses absolute comparison when *profiled_value* is near zero.
        """
        if np.isclose(profiled_value, 0.0, atol=1e-10):
            if np.isclose(generated_value, 0.0, atol=max(tolerance, 1e-10)):
                return 1.0
            abs_diff = float(np.abs(generated_value - profiled_value))
            return float(
                np.clip(1.0 - abs_diff / max(tolerance, 1e-10), 0.0, 1.0)
            )

        relative_error = float(
            np.abs(generated_value - profiled_value) / np.abs(profiled_value)
        )

        if relative_error <= tolerance:
            return 1.0

        excess = relative_error - tolerance
        return float(np.clip(math.exp(-excess / max(tolerance, 1e-10)), 0.0, 1.0))

    # ------------------------------------------------------------------
    # 3. Value-range validation
    # ------------------------------------------------------------------

    def _validate_value_ranges(
        self,
        generated_series: pd.Series,
        profile_column: dict[str, Any],
    ) -> float:
        """Verify generated values fall within profiled ranges.

        Checks:
        1. Percentage of values within ``[min, max]`` (with tolerance).
        2. Percentile boundary compliance (5th, 25th, 50th, 75th, 95th).

        Args:
            generated_series: Column from generated data.
            profile_column: Per-column profile metadata.

        Returns:
            Range compliance score in [0.0, 1.0].
        """
        data_type: str = profile_column.get("data_type", "numerical")
        if data_type in _CATEGORICAL_TYPES:
            return 1.0  # ranges not applicable to categorical data

        clean_generated = generated_series.dropna()
        if len(clean_generated) == 0:
            return 0.0

        scores: list[float] = []

        # Min / max bounds
        profiled_min = profile_column.get("min")
        profiled_max = profile_column.get("max")

        if profiled_min is not None and profiled_max is not None:
            value_range = float(profiled_max) - float(profiled_min)
            tolerance_abs = max(value_range * self._range_tolerance, 1e-10)

            extended_min = float(profiled_min) - tolerance_abs
            extended_max = float(profiled_max) + tolerance_abs

            in_range = clean_generated.between(extended_min, extended_max)
            range_ratio = float(in_range.sum()) / len(clean_generated)
            scores.append(range_ratio)

        # Percentile boundaries
        percentiles: dict[str, Any] = profile_column.get("percentiles", {})
        if percentiles:
            pct_scores = self._check_percentile_boundaries(
                clean_generated, percentiles
            )
            scores.extend(pct_scores)

        if not scores:
            return 1.0  # no range data available — benefit of the doubt

        result = float(np.mean(scores))

        self.logger.debug(
            "range_validation_result",
            column=str(generated_series.name),
            score=result,
            n_checks=len(scores),
        )
        return float(np.clip(result, 0.0, 1.0))

    @staticmethod
    def _check_percentile_boundaries(
        series: pd.Series,
        percentiles: dict[str, Any],
    ) -> list[float]:
        """Score percentile-boundary compliance.

        For each profiled percentile value, performs two complementary
        checks and blends them:

        1. **Fraction check** — what fraction of generated values lies
           at or below the profiled boundary (CDF match).
        2. **Quantile check** — the actual quantile value in the
           generated data via :meth:`pd.Series.quantile` compared
           against the profiled value (value-space match).
        """
        pct_map: dict[str, float] = {
            "5": 0.05,
            "25": 0.25,
            "50": 0.50,
            "75": 0.75,
            "95": 0.95,
        }

        scores: list[float] = []
        n = len(series)
        if n == 0:
            return scores

        series_min = float(series.min())
        series_max = float(series.max())
        value_range = series_max - series_min

        for pct_key, expected_fraction in pct_map.items():
            pct_value = percentiles.get(pct_key)
            if pct_value is None:
                continue

            pct_value_f = float(pct_value)

            # -- Fraction-based check (CDF match) --
            actual_fraction = float((series <= pct_value_f).sum()) / n
            deviation = float(np.abs(actual_fraction - expected_fraction))

            max_allowed_deviation = 0.10
            if deviation <= max_allowed_deviation:
                fraction_score = 1.0
            else:
                fraction_score = float(
                    np.clip(
                        1.0 - (deviation - max_allowed_deviation) / 0.5,
                        0.0,
                        1.0,
                    )
                )

            # -- Quantile value-space check --
            generated_quantile = float(series.quantile(expected_fraction))

            if value_range > 0.0:
                quantile_deviation = (
                    float(np.abs(generated_quantile - pct_value_f)) / value_range
                )
                quantile_score = float(
                    np.clip(1.0 - quantile_deviation * 2.0, 0.0, 1.0)
                )
            else:
                quantile_score = (
                    1.0
                    if np.isclose(generated_quantile, pct_value_f, atol=1e-10)
                    else 0.5
                )

            # Blend both checks (equal weight)
            score = 0.5 * fraction_score + 0.5 * quantile_score
            scores.append(score)

        return scores

    # ------------------------------------------------------------------
    # 4. Cardinality validation
    # ------------------------------------------------------------------

    def _validate_cardinality(
        self,
        generated_series: pd.Series,
        profile_column: dict[str, Any],
    ) -> float:
        """Compare unique value counts between generated and profiled data.

        * **Categorical**: checks distinct-value-set coverage.
        * **Numerical**: compares cardinality ratios.

        Args:
            generated_series: Column from generated data.
            profile_column: Per-column profile metadata.

        Returns:
            Cardinality similarity score in [0.0, 1.0].
        """
        profiled_cardinality = profile_column.get("cardinality")

        if profiled_cardinality is None or int(profiled_cardinality) == 0:
            return 1.0

        generated_cardinality: int = int(generated_series.nunique())
        profiled_cardinality_int: int = int(profiled_cardinality)

        data_type: str = profile_column.get("data_type", "numerical")
        is_categorical: bool = data_type in _CATEGORICAL_TYPES

        if is_categorical:
            distinct_values = profile_column.get("distinct_values", [])

            if distinct_values:
                generated_values: set[str] = set(
                    generated_series.dropna().astype(str).unique()
                )
                profiled_values: set[str] = {str(v) for v in distinct_values}

                # Coverage: fraction of expected values present
                coverage = (
                    len(generated_values & profiled_values)
                    / max(len(profiled_values), 1)
                )

                # Penalty for unexpected values
                unexpected = generated_values - profiled_values
                unexpected_penalty = min(
                    len(unexpected) / max(len(profiled_values), 1), 0.5
                )

                score = float(
                    np.clip(coverage - unexpected_penalty * 0.5, 0.0, 1.0)
                )
            else:
                score = self._cardinality_ratio_score(
                    generated_cardinality, profiled_cardinality_int
                )
        else:
            score = self._cardinality_ratio_score(
                generated_cardinality, profiled_cardinality_int
            )

        self.logger.debug(
            "cardinality_validation_result",
            column=str(generated_series.name),
            generated_cardinality=generated_cardinality,
            profiled_cardinality=profiled_cardinality_int,
            score=score,
        )
        return float(np.clip(score, 0.0, 1.0))

    def _cardinality_ratio_score(
        self,
        generated: int,
        profiled: int,
    ) -> float:
        """Score cardinality similarity based on ratio proximity to 1.0."""
        if profiled == 0:
            return 1.0 if generated == 0 else 0.0

        ratio = generated / profiled
        deviation = float(np.abs(ratio - 1.0))

        if deviation <= self._cardinality_tolerance:
            return 1.0

        excess = deviation - self._cardinality_tolerance
        return float(
            np.clip(
                1.0 - excess / (1.0 + self._cardinality_tolerance), 0.0, 1.0
            )
        )

    # ------------------------------------------------------------------
    # 5. Null-ratio validation
    # ------------------------------------------------------------------

    def _validate_null_ratios(
        self,
        generated_series: pd.Series,
        profile_column: dict[str, Any],
    ) -> float:
        """Compare null / missing-value ratios.

        Scores based on the absolute difference between the generated
        and profiled null percentages, with a configurable tolerance
        band inside which the score is 1.0.

        Args:
            generated_series: Column from generated data.
            profile_column: Per-column profile metadata.

        Returns:
            Null-ratio compliance score in [0.0, 1.0].
        """
        profiled_null_ratio = profile_column.get("null_ratio")

        if profiled_null_ratio is None:
            return 1.0

        profiled_null_ratio_f = float(profiled_null_ratio)

        total_count = len(generated_series)
        if total_count == 0:
            return 0.0

        null_count: int = int(generated_series.isna().sum())
        generated_null_ratio = null_count / total_count

        diff = float(np.abs(generated_null_ratio - profiled_null_ratio_f))

        if diff <= self._null_ratio_tolerance:
            score = 1.0
        else:
            excess = diff - self._null_ratio_tolerance
            score = float(np.clip(1.0 - excess / 0.5, 0.0, 1.0))

        self.logger.debug(
            "null_ratio_validation_result",
            column=str(generated_series.name),
            generated_null_ratio=generated_null_ratio,
            profiled_null_ratio=profiled_null_ratio_f,
            score=score,
        )
        return score

    # ------------------------------------------------------------------
    # Great Expectations integration
    # ------------------------------------------------------------------

    def _build_great_expectations_suite(
        self,
        profile: dict[str, Any],
    ) -> ExpectationSuite:
        """Dynamically construct a GE ExpectationSuite from a profile.

        Adds declarative expectations for value ranges, column means,
        column standard deviations, and distinct-value-set membership
        derived from the source statistical profile.

        Args:
            profile: Statistical profile containing ``"columns"`` metadata.

        Returns:
            An :class:`ExpectationSuite` ready for validation.
        """
        suite = ExpectationSuite(
            expectation_suite_name="statistical_validation_suite"
        )
        profile_columns: dict[str, Any] = profile.get("columns", {})

        for col_name, col_profile in profile_columns.items():
            data_type: str = col_profile.get("data_type", "numerical")
            is_categorical: bool = data_type in _CATEGORICAL_TYPES

            if not is_categorical:
                self._add_numerical_expectations(suite, col_name, col_profile)
            else:
                self._add_categorical_expectations(suite, col_name, col_profile)

        expectation_count = (
            len(suite.expectations) if hasattr(suite, "expectations") else 0
        )
        self.logger.info(
            "great_expectations_suite_built",
            suite_name="statistical_validation_suite",
            n_expectations=expectation_count,
        )
        return suite

    def _add_numerical_expectations(
        self,
        suite: ExpectationSuite,
        col_name: str,
        col_profile: dict[str, Any],
    ) -> None:
        """Add value-range, mean, and stdev expectations for a numerical column."""
        col_min = col_profile.get("min")
        col_max = col_profile.get("max")

        if col_min is not None and col_max is not None:
            value_range = float(col_max) - float(col_min)
            tol = max(value_range * self._range_tolerance, 1e-6)
            self._safe_add_expectation(
                suite,
                "expect_column_values_to_be_between",
                {
                    "column": col_name,
                    "min_value": float(col_min) - tol,
                    "max_value": float(col_max) + tol,
                    "mostly": 0.95,
                },
            )

        col_mean = col_profile.get("mean")
        if col_mean is not None:
            mean_tol = max(abs(float(col_mean)) * self._moment_tolerance, 1e-6)
            self._safe_add_expectation(
                suite,
                "expect_column_mean_to_be_between",
                {
                    "column": col_name,
                    "min_value": float(col_mean) - mean_tol,
                    "max_value": float(col_mean) + mean_tol,
                },
            )

        col_std = col_profile.get("std")
        if col_std is not None and float(col_std) > 0.0:
            std_tol = max(float(col_std) * self._moment_tolerance, 1e-6)
            self._safe_add_expectation(
                suite,
                "expect_column_stdev_to_be_between",
                {
                    "column": col_name,
                    "min_value": max(0.0, float(col_std) - std_tol),
                    "max_value": float(col_std) + std_tol,
                },
            )

    def _add_categorical_expectations(
        self,
        suite: ExpectationSuite,
        col_name: str,
        col_profile: dict[str, Any],
    ) -> None:
        """Add distinct-value-set expectation for a categorical column."""
        distinct_values = col_profile.get("distinct_values", [])
        if distinct_values:
            self._safe_add_expectation(
                suite,
                "expect_column_distinct_values_to_be_in_set",
                {"column": col_name, "value_set": list(distinct_values)},
            )

    def _safe_add_expectation(
        self,
        suite: ExpectationSuite,
        expectation_type: str,
        kwargs: dict[str, Any],
    ) -> None:
        """Add a single expectation with graceful error handling.

        Attempts to import and use ``ExpectationConfiguration``.  If
        unavailable, falls back to the dict-based approach supported
        in some GE versions.
        """
        try:
            from great_expectations.core import ExpectationConfiguration  # noqa: PLC0415

            config = ExpectationConfiguration(
                expectation_type=expectation_type, kwargs=kwargs
            )
            suite.add_expectation(config)
        except ImportError:
            # Fallback: dict-based expectation (supported in some GE versions)
            try:
                suite.add_expectation(
                    expectation_configuration={
                        "expectation_type": expectation_type,
                        "kwargs": kwargs,
                    }
                )
            except Exception as exc:
                self.logger.debug(
                    "ge_expectation_add_failed_dict_fallback",
                    expectation_type=expectation_type,
                    column=kwargs.get("column", "unknown"),
                    error=str(exc),
                )
        except Exception as exc:
            self.logger.debug(
                "ge_expectation_add_failed",
                expectation_type=expectation_type,
                column=kwargs.get("column", "unknown"),
                error=str(exc),
            )

    def _run_great_expectations_validation(
        self,
        data: pd.DataFrame,
        suite: ExpectationSuite,
    ) -> dict[str, Any]:
        """Execute GE validation against the generated DataFrame.

        Attempts the full GE DataContext workflow; on failure, falls back
        to a manual evaluation of each expectation.

        Args:
            data: Generated DataFrame.
            suite: Configured ExpectationSuite.

        Returns:
            Structured dict with ``success_ratio``, counts, and failure
            details.
        """
        if self._ge_context is None:
            return self._evaluate_expectations_manually(data, suite)

        try:
            # Persist suite in the ephemeral context store
            self._ge_context.add_expectation_suite(expectation_suite=suite)

            # Attempt to obtain a GE Validator with the DataFrame batch
            validator = self._ge_context.get_validator(
                batch_request=None,
                expectation_suite_name=suite.expectation_suite_name,
            )

            # If we reach here, run validation through GE
            validation_result = validator.validate()

            total = validation_result.statistics.get("evaluated_expectations", 0)
            successful = validation_result.statistics.get(
                "successful_expectations", 0
            )
            success_ratio = successful / max(total, 1)

            failed_details: list[dict[str, Any]] = []
            for result in validation_result.results:
                if not result.success:
                    failed_details.append(
                        {
                            "expectation_type": result.expectation_config.expectation_type,
                            "kwargs": dict(result.expectation_config.kwargs),
                        }
                    )

            return {
                "success_ratio": float(success_ratio),
                "total_expectations": total,
                "successful_expectations": successful,
                "failed_expectations": failed_details,
                "status": "ge_native",
            }

        except Exception as exc:
            self.logger.debug(
                "ge_native_validation_unavailable_falling_back",
                error=str(exc),
                error_type=type(exc).__name__,
            )
            return self._evaluate_expectations_manually(data, suite)

    def _evaluate_expectations_manually(
        self,
        data: pd.DataFrame,
        suite: ExpectationSuite,
    ) -> dict[str, Any]:
        """Manually evaluate GE-style expectations against a DataFrame.

        Provides a reliable fallback when the full GE runtime cannot
        produce a validator for in-memory DataFrames.

        Each supported expectation type is evaluated programmatically
        using standard pandas / numpy operations.
        """
        expectations = getattr(suite, "expectations", [])
        if not expectations:
            return {
                "success_ratio": 1.0,
                "total_expectations": 0,
                "successful_expectations": 0,
                "failed_expectations": [],
                "status": "manual_no_expectations",
            }

        total = 0
        successful = 0
        failed: list[dict[str, Any]] = []

        for exp in expectations:
            # Normalise access - ExpectationConfiguration or dict
            try:
                exp_type = getattr(exp, "expectation_type", None)
                exp_kwargs = getattr(exp, "kwargs", None)
                if exp_type is None and isinstance(exp, dict):
                    exp_type = exp.get("expectation_type")
                    exp_kwargs = exp.get("kwargs", {})
                if exp_kwargs is None:
                    exp_kwargs = {}
            except Exception:
                logger.debug("skipping_malformed_expectation", exp=str(exp))
                continue

            if exp_type is None:
                continue

            total += 1
            passed = self._evaluate_single_expectation(
                data, exp_type, dict(exp_kwargs)
            )

            if passed:
                successful += 1
            else:
                failed.append({"expectation_type": exp_type, "kwargs": dict(exp_kwargs)})

        success_ratio = successful / max(total, 1)

        return {
            "success_ratio": float(success_ratio),
            "total_expectations": total,
            "successful_expectations": successful,
            "failed_expectations": failed,
            "status": "manual_evaluation",
        }

    def _evaluate_single_expectation(
        self,
        data: pd.DataFrame,
        expectation_type: str,
        kwargs: dict[str, Any],
    ) -> bool:
        """Evaluate one expectation against *data*, returning True if passed."""
        column: str | None = kwargs.get("column")

        if column is not None and column not in data.columns:
            return False

        try:
            if expectation_type == "expect_column_values_to_be_between":
                return self._eval_values_between(data, kwargs)
            if expectation_type == "expect_column_mean_to_be_between":
                return self._eval_mean_between(data, kwargs)
            if expectation_type == "expect_column_stdev_to_be_between":
                return self._eval_stdev_between(data, kwargs)
            if expectation_type == "expect_column_distinct_values_to_be_in_set":
                return self._eval_distinct_in_set(data, kwargs)
        except Exception:
            return False

        # Unknown expectation type — assume pass
        return True

    # --- Manual expectation evaluators ---

    @staticmethod
    def _eval_values_between(
        data: pd.DataFrame, kwargs: dict[str, Any]
    ) -> bool:
        col = kwargs["column"]
        series = data[col].dropna()
        if len(series) == 0:
            return True
        min_val = kwargs.get("min_value")
        max_val = kwargs.get("max_value")
        mostly = kwargs.get("mostly", 1.0)

        in_range = series.between(
            float(min_val) if min_val is not None else series.min(),
            float(max_val) if max_val is not None else series.max(),
        )
        ratio = float(in_range.sum()) / len(series)
        return bool(ratio >= mostly)

    @staticmethod
    def _eval_mean_between(
        data: pd.DataFrame, kwargs: dict[str, Any]
    ) -> bool:
        col = kwargs["column"]
        series = data[col].dropna()
        if len(series) == 0:
            return True
        actual_mean = float(series.mean())
        min_val = kwargs.get("min_value")
        max_val = kwargs.get("max_value")

        lower_ok = min_val is None or actual_mean >= float(min_val)
        upper_ok = max_val is None or actual_mean <= float(max_val)
        return lower_ok and upper_ok

    @staticmethod
    def _eval_stdev_between(
        data: pd.DataFrame, kwargs: dict[str, Any]
    ) -> bool:
        col = kwargs["column"]
        series = data[col].dropna()
        if len(series) < 2:
            return True
        actual_std = float(series.std())
        min_val = kwargs.get("min_value")
        max_val = kwargs.get("max_value")

        lower_ok = min_val is None or actual_std >= float(min_val)
        upper_ok = max_val is None or actual_std <= float(max_val)
        return lower_ok and upper_ok

    @staticmethod
    def _eval_distinct_in_set(
        data: pd.DataFrame, kwargs: dict[str, Any]
    ) -> bool:
        col = kwargs["column"]
        value_set = {str(v) for v in kwargs.get("value_set", [])}
        if not value_set:
            return True
        actual_values = set(data[col].dropna().astype(str).unique())
        return actual_values.issubset(value_set)

    # ------------------------------------------------------------------
    # Score helpers
    # ------------------------------------------------------------------

    def _pvalue_to_score(self, p_value: float) -> float:
        """Map a hypothesis-test p-value to a [0, 1] similarity score.

        When ``p_value >= threshold`` the distributions are statistically
        indistinguishable, yielding ``1.0``.  Below the threshold the
        score degrades linearly.
        """
        if p_value >= self._ks_pvalue_threshold:
            return 1.0
        return float(
            np.clip(p_value / max(self._ks_pvalue_threshold, 1e-15), 0.0, 1.0)
        )

    def _aggregate_column_scores(
        self,
        column_scores: dict[str, float],
    ) -> float:
        """Weighted average across all validated columns.

        Optionally applies per-column importance weights from
        ``config["column_weights"]``.

        Args:
            column_scores: Map of column names to composite scores.

        Returns:
            Aggregate statistical fidelity score in [0.0, 1.0].
        """
        if not column_scores:
            return 0.0

        custom_weights: dict[str, float] = self.config.get("column_weights", {})

        scores_list: list[float] = []
        weights_list: list[float] = []

        for col_name, score in column_scores.items():
            scores_list.append(float(score))
            weights_list.append(float(custom_weights.get(col_name, 1.0)))

        scores_array = np.array(scores_list, dtype=np.float64)
        weights_array = np.array(weights_list, dtype=np.float64)

        weight_sum = float(np.sum(weights_array))
        result = (
            float(np.average(scores_array, weights=weights_array))
            if weight_sum > 0.0
            else float(np.mean(scores_array))
        )

        return float(np.clip(result, 0.0, 1.0))
