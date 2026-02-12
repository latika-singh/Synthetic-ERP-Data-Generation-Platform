"""Statistical distribution-based synthetic data generator.

This module implements the statistical synthesis strategy — one of four pluggable
generation methods available in the Generation Engine. It produces synthetic data
that faithfully preserves:

- **Marginal distributions** — Each column's value distribution is matched by
  fitting parametric distributions (normal, log-normal, Poisson, exponential,
  uniform) or using empirical / categorical distributions from the statistical
  profile captured by the Profiling Service.

- **Multivariate correlations** — Inter-column dependencies are preserved using
  a Gaussian copula approach: correlated uniform variates are generated via
  Cholesky decomposition of the correlation matrix and then transformed to target
  marginal distributions through inverse CDF (PPF) transforms.

The generator operates exclusively on *statistical profiles* — it never accesses
raw production data (Constraint C-001). This ensures privacy compliance while
enabling high-fidelity synthetic data generation suitable for testing, development,
and analytics workloads across Financial Accounting, HR, Sales & Distribution, and
Material Management ERP modules.

Key Algorithms:
    - Column-wise distribution fitting via method of moments and KS testing.
    - Gaussian copula for multivariate correlation preservation.
    - Cholesky decomposition for imposing correlation structure.
    - Laplace smoothing for categorical probability estimation.

Usage::

    from generation_engine.generators.statistical_generator import StatisticalGenerator

    generator = StatisticalGenerator(config={"seed": 42})
    result = generator.generate(
        schema={"columns": [{"name": "amount", "data_type": "float"}]},
        profile={"columns": {"amount": {"mean": 1000.0, "std": 250.0}}},
        num_records=10000,
    )
"""

from __future__ import annotations

from typing import Any, Literal

import numpy as np
import pandas as pd
from pydantic import BaseModel, Field
from scipy import stats as scipy_stats
from scipy.linalg import cholesky
from scipy.stats import (
    chi2,
    expon,
    kstest,
    lognorm,
    multinomial,
    norm,
    poisson,
    uniform,
)

from generation_engine.generators.base import (
    BaseGenerator,
    ColumnSpec,
    GenerationError,
    GenerationResult,
)
from shared.logging.structured_logger import get_logger


# ---------------------------------------------------------------------------
# Pydantic Configuration Models
# ---------------------------------------------------------------------------


class DistributionFit(BaseModel):
    """Result of fitting a parametric distribution to a column's statistical profile.

    Captures the fitted distribution type, its parameters, the goodness-of-fit
    metric (KS test p-value or chi-squared statistic), and the column it
    describes.

    Attributes:
        distribution_type: The identified distribution family.
        parameters: Distribution-specific parameters (e.g., loc, scale, shape).
        goodness_of_fit: KS test p-value (higher is a better fit) or
            chi-squared goodness-of-fit score for categorical columns.
        column_name: Name of the column this distribution describes.
    """

    distribution_type: Literal[
        "normal",
        "lognormal",
        "poisson",
        "exponential",
        "uniform",
        "categorical",
        "empirical",
    ]
    parameters: dict[str, float] = Field(
        ...,
        description="Distribution-specific parameters (loc, scale, shape, etc.).",
    )
    goodness_of_fit: float = Field(
        ...,
        ge=0.0,
        description="KS test p-value or chi-squared goodness-of-fit score.",
    )
    column_name: str = Field(
        ...,
        description="Name of the column this distribution describes.",
    )


class CorrelationConfig(BaseModel):
    """Configuration for inter-column correlation preservation.

    Controls how the generator maintains multivariate relationships between
    columns during synthesis.  The Gaussian copula method is the default and
    recommended approach for most scenarios.

    Attributes:
        method: Correlation preservation method.  ``'gaussian_copula'`` uses
            Cholesky decomposition of the correlation matrix; ``'empirical'``
            resamples from observed joint distributions; ``'none'`` generates
            columns independently.
        correlation_matrix: Optional pre-computed correlation matrix.  When
            ``None`` the generator computes it from the statistical profile.
        rank_correlation: If ``True`` (default), Spearman rank correlation is
            used which is more robust to non-linear relationships.
    """

    method: Literal["gaussian_copula", "empirical", "none"] = Field(
        default="gaussian_copula",
        description="Correlation preservation method.",
    )
    correlation_matrix: list[list[float]] | None = Field(
        default=None,
        description="Pre-computed correlation matrix (overrides profile-derived).",
    )
    rank_correlation: bool = Field(
        default=True,
        description="Use Spearman rank correlation (more robust).",
    )


class StatisticalConfig(BaseModel):
    """Master configuration for the statistical generator.

    Encapsulates all tuneable parameters controlling distribution fitting,
    correlation preservation, auto-fit behaviour, and post-processing options.

    Attributes:
        distributions: Optional per-column distribution overrides.
        correlation: Correlation preservation configuration.
        auto_fit: When ``True``, automatically fit distributions from profile.
        fit_candidates: Distribution families to try during auto-fit.
        significance_level: KS test significance level for distribution selection.
        seed: Optional random seed for reproducibility.
        categorical_smoothing: Laplace smoothing factor for categorical
            probabilities.
        outlier_handling: Strategy for generated values outside expected ranges.
    """

    distributions: dict[str, DistributionFit] | None = Field(
        default=None,
        description="Per-column distribution fits (overrides auto-fitting).",
    )
    correlation: CorrelationConfig = Field(
        default_factory=CorrelationConfig,
        description="Correlation preservation configuration.",
    )
    auto_fit: bool = Field(
        default=True,
        description="Automatically fit distributions from profile.",
    )
    fit_candidates: list[str] = Field(
        default=["normal", "lognormal", "poisson", "exponential", "uniform"],
        description="Distributions to try during auto-fit.",
    )
    significance_level: float = Field(
        default=0.05,
        gt=0.0,
        lt=1.0,
        description="KS test significance level for distribution selection.",
    )
    seed: int | None = Field(
        default=None,
        description="Random seed for reproducibility.",
    )
    categorical_smoothing: float = Field(
        default=0.01,
        ge=0.0,
        le=1.0,
        description="Laplace smoothing for categorical probabilities.",
    )
    outlier_handling: Literal["clip", "remove", "keep"] = Field(
        default="clip",
        description="How to handle outlier values in generated data.",
    )


# ---------------------------------------------------------------------------
# SciPy Distribution Mapping & Type Constants
# ---------------------------------------------------------------------------

# Maps distribution type identifiers to their corresponding SciPy
# distribution objects for consistent usage across fitting and sampling.
_SCIPY_DIST_MAP: dict[str, Any] = {
    "normal": scipy_stats.norm,
    "lognormal": scipy_stats.lognorm,
    "poisson": scipy_stats.poisson,
    "exponential": scipy_stats.expon,
    "uniform": scipy_stats.uniform,
}

# Column data types treated as numeric for distribution fitting.
_NUMERIC_TYPES: frozenset[str] = frozenset(
    {"integer", "float", "decimal", "number", "int", "bigint"}
)

# Column data types treated as datetime — generated as epoch seconds then
# converted back.
_DATETIME_TYPES: frozenset[str] = frozenset({"date", "datetime", "timestamp"})

# Column data types treated as categorical.
_CATEGORICAL_TYPES: frozenset[str] = frozenset(
    {"string", "text", "boolean", "varchar", "char", "binary"}
)


# ---------------------------------------------------------------------------
# StatisticalGenerator — Strategy Pattern Implementation
# ---------------------------------------------------------------------------


class StatisticalGenerator(BaseGenerator):
    """Statistical distribution-based synthetic data generator.

    Implements the Strategy pattern contract defined by :class:`BaseGenerator`
    to produce synthetic data using column-wise distribution fitting and
    Gaussian copula-based multivariate correlation preservation.

    The generation pipeline follows five stages:

    1. **Fit marginal distributions** — For each column, identify the best-fit
       parametric distribution (or use categorical / empirical distributions)
       from the statistical profile.
    2. **Compute / extract correlation matrix** — Build or retrieve the
       inter-column correlation matrix.
    3. **Generate copula samples** — Use Cholesky decomposition and the
       Gaussian copula to produce correlated uniform variates.
    4. **Transform to marginals** — Apply inverse CDF (PPF) transforms to map
       uniform variates to each column's fitted distribution.
    5. **Post-process** — Round integers, clip ranges, map categoricals, inject
       nulls, and convert datetime epochs.

    Args:
        config: Optional configuration dictionary parsed into
            :class:`StatisticalConfig`.
    """

    def __init__(self, config: dict[str, Any] | None = None) -> None:
        """Initialise the statistical generator with optional configuration.

        Parses configuration into a validated :class:`StatisticalConfig`
        instance, sets the numpy random seed for reproducibility, and
        prepares internal state for distribution tracking.

        Args:
            config: Raw configuration dictionary.  Parsed into a
                :class:`StatisticalConfig` instance for validated access.
        """
        super().__init__(config)

        # Parse and validate configuration via Pydantic.
        stat_config_raw = config or {}
        try:
            self._stat_config = StatisticalConfig(**stat_config_raw)
        except Exception:
            # Graceful fallback to defaults if parsing fails at init time.
            self._stat_config = StatisticalConfig()
            self.logger.warning(
                "statistical_config_parse_fallback",
                reason="Failed to parse supplied config; using defaults.",
            )

        # Override the base logger with a module-specific instance.
        self.logger = get_logger(__name__)

        # Set numpy random seed for reproducibility when requested.
        if self._stat_config.seed is not None:
            np.random.seed(self._stat_config.seed)
            self.logger.info("random_seed_set", seed=self._stat_config.seed)

        # Populated during generate() — stores per-column distribution fits.
        self.fitted_distributions: dict[str, DistributionFit] = {}

        # Populated during generate() — the correlation matrix used for copula.
        self.correlation_matrix: np.ndarray | None = None

    # ------------------------------------------------------------------
    # Abstract Method Implementations  (Strategy Contract)
    # ------------------------------------------------------------------

    # -- generate() helper methods (extracted for PLR0912/PLR0915) -------

    @staticmethod
    def _classify_columns(
        column_specs: list[ColumnSpec],
    ) -> tuple[list[str], list[str], list[str], dict[str, str]]:
        """Classify columns into numeric, datetime, and categorical buckets.

        Returns:
            ``(numeric_cols, categorical_cols, datetime_cols, column_type_map)``
        """
        numeric_cols: list[str] = []
        categorical_cols: list[str] = []
        datetime_cols: list[str] = []
        column_type_map: dict[str, str] = {}

        for cs in column_specs:
            dtype_lower = cs.data_type.lower() if cs.data_type else "string"
            column_type_map[cs.name] = dtype_lower
            if dtype_lower in _NUMERIC_TYPES:
                numeric_cols.append(cs.name)
            elif dtype_lower in _DATETIME_TYPES:
                datetime_cols.append(cs.name)
            else:
                categorical_cols.append(cs.name)

        return numeric_cols, categorical_cols, datetime_cols, column_type_map

    def _generate_numeric_data(
        self,
        copula_numeric_cols: list[str],
        num_records: int,
    ) -> dict[str, np.ndarray]:
        """Generate numeric + datetime column data via copula or independently."""
        generated: dict[str, np.ndarray] = {}
        if not copula_numeric_cols:
            return generated

        if self.correlation_matrix is not None:
            copula_samples = self._generate_copula_samples(
                n_samples=num_records,
                n_columns=len(copula_numeric_cols),
                correlation_matrix=self.correlation_matrix,
            )
            for idx, col_name in enumerate(copula_numeric_cols):
                dist_fit = self.fitted_distributions.get(col_name)
                generated[col_name] = (
                    self._transform_to_marginal(copula_samples[:, idx], dist_fit)
                    if dist_fit is not None
                    else copula_samples[:, idx]
                )
            self._log_progress(
                records_generated=num_records // 2,
                total_records=num_records,
            )
        else:
            for col_name in copula_numeric_cols:
                dist_fit = self.fitted_distributions.get(col_name)
                if dist_fit is not None:
                    independent_u = np.array(
                        np.random.random(num_records), dtype=np.float64,
                    )
                    generated[col_name] = self._transform_to_marginal(
                        independent_u, dist_fit,
                    )
        return generated

    def _generate_categorical_data(
        self,
        categorical_cols: list[str],
        num_records: int,
    ) -> dict[str, np.ndarray]:
        """Generate categorical column data independently of the copula."""
        generated: dict[str, np.ndarray] = {}
        for col_name in categorical_cols:
            dist_fit = self.fitted_distributions.get(col_name)
            if dist_fit is not None:
                uniform_vals = np.array(
                    np.random.random(num_records), dtype=np.float64,
                )
                generated[col_name] = self._transform_to_marginal(
                    uniform_vals, dist_fit,
                )
        return generated

    def _build_gen_metadata(
        self,
        numeric_cols: list[str],
        categorical_cols: list[str],
        datetime_cols: list[str],
    ) -> dict[str, Any]:
        """Assemble the metadata dictionary for the generation result."""
        return {
            "method": "statistical",
            "correlation_method": self._stat_config.correlation.method,
            "num_numeric_columns": len(numeric_cols),
            "num_categorical_columns": len(categorical_cols),
            "num_datetime_columns": len(datetime_cols),
            "seed": self._stat_config.seed,
            "distribution_fits": {
                name: {
                    "type": fit.distribution_type,
                    "goodness_of_fit": fit.goodness_of_fit,
                }
                for name, fit in self.fitted_distributions.items()
            },
        }

    # -- Main generate entry point ----------------------------------------

    def generate(
        self,
        schema: dict[str, Any],
        profile: dict[str, Any],
        num_records: int,
        **kwargs: Any,  # noqa: ARG002
    ) -> GenerationResult:
        """Generate synthetic data matching the provided schema and profile.

        Orchestrates the five-stage statistical generation pipeline:
        fit marginals → compute correlation → generate copula → transform →
        post-process.

        Args:
            schema: Table schema dictionary containing a ``"columns"`` key
                with column definitions parseable by :meth:`_validate_schema`.
            profile: Statistical profile dictionary containing per-column
                statistics (mean, std, min, max, percentiles, frequencies,
                correlations).
            num_records: Number of synthetic records to generate.
            **kwargs: Additional parameters forwarded from the orchestrator.

        Returns:
            A :class:`GenerationResult` with the synthetic DataFrame, column
            metadata, and generation diagnostics.

        Raises:
            GenerationError: When a fatal error prevents generation.
        """
        self._start_timer()
        self.logger.info(
            "statistical_generation_started",
            num_records=num_records,
            method="statistical",
        )

        try:
            self.validate_config(self.config or {})
            column_specs = self._validate_schema(schema)
            profile_columns = profile.get("columns", {})

            numeric_cols, categorical_cols, datetime_cols, column_type_map = (
                self._classify_columns(column_specs)
            )
            copula_numeric_cols = numeric_cols + datetime_cols

            # Stage 1: Fit marginal distributions.
            self.fitted_distributions = self._fit_all_distributions(
                column_specs, numeric_cols, datetime_cols,
                categorical_cols, profile_columns,
            )

            # Stage 2: Resolve correlation matrix.
            self.correlation_matrix = (
                self._resolve_correlation_matrix(profile, copula_numeric_cols)
                if copula_numeric_cols
                and self._stat_config.correlation.method != "none"
                else None
            )

            # Stages 3-4: Generate via copula / independently.
            generated_data = self._generate_numeric_data(
                copula_numeric_cols, num_records,
            )
            generated_data.update(
                self._generate_categorical_data(categorical_cols, num_records),
            )
            self._log_progress(
                records_generated=int(num_records * 0.8),
                total_records=num_records,
            )

            # Stage 5: Post-processing + null injection.
            result_df = self._postprocess(
                generated_data, column_specs,
                column_type_map, profile_columns, num_records,
            )
            result_df = self._apply_nulls(result_df, schema)
            self._log_progress(
                records_generated=num_records, total_records=num_records,
            )

            self.logger.info(
                "statistical_generation_completed",
                num_records=len(result_df),
                num_columns=len(result_df.columns),
            )
            return self._build_result(
                data=result_df,
                metadata=self._build_gen_metadata(
                    numeric_cols, categorical_cols, datetime_cols,
                ),
            )

        except GenerationError:
            raise
        except Exception as exc:
            self.logger.error(
                "statistical_generation_failed",
                error=str(exc),
                error_type=type(exc).__name__,
            )
            raise GenerationError(
                message=f"Statistical generation failed: {exc}",
                method="statistical",
                details={"error": str(exc), "error_type": type(exc).__name__},
            ) from exc

    def validate_config(self, config: dict[str, Any]) -> bool:
        """Validate the statistical generator configuration.

        Parses the config through the :class:`StatisticalConfig` Pydantic model
        and performs additional semantic checks on significance level,
        correlation matrix shape, and fit candidate validity.

        Args:
            config: Configuration dictionary to validate.

        Returns:
            ``True`` if the configuration is valid.

        Raises:
            ValueError: With a descriptive message when validation fails.
        """
        try:
            parsed = StatisticalConfig(**(config or {}))
        except Exception as exc:
            raise ValueError(
                f"Invalid statistical generator configuration: {exc}"
            ) from exc

        # Check significance level bounds (already constrained by Pydantic,
        # but an explicit guard for programmatic callers).
        if not (0.0 < parsed.significance_level < 1.0):
            raise ValueError(
                f"significance_level must be in (0, 1), got {parsed.significance_level}"
            )

        # Validate correlation matrix if provided.
        if parsed.correlation.correlation_matrix is not None:
            matrix = np.array(parsed.correlation.correlation_matrix)
            if matrix.ndim != 2 or matrix.shape[0] != matrix.shape[1]:
                raise ValueError(
                    f"Correlation matrix must be square, got shape {matrix.shape}"
                )
            # Check positive semi-definiteness.
            eigenvalues = np.linalg.eigvalsh(matrix)
            if np.any(eigenvalues < -1e-8):
                self.logger.warning(
                    "correlation_matrix_not_psd",
                    min_eigenvalue=float(np.min(eigenvalues)),
                )

        # Validate fit_candidates against known scipy distributions.
        valid_dists = set(_SCIPY_DIST_MAP.keys())
        for candidate in parsed.fit_candidates:
            if candidate not in valid_dists:
                raise ValueError(
                    f"Unknown distribution '{candidate}' in fit_candidates. "
                    f"Valid options: {sorted(valid_dists)}"
                )

        self.logger.debug("config_validated", auto_fit=parsed.auto_fit)
        return True

    def get_capabilities(self) -> dict[str, Any]:
        """Return a machine-readable description of this generator's capabilities.

        Used by the method selector to determine whether this generator is
        appropriate for a given column or table.

        Returns:
            Dictionary describing supported distributions, correlation methods,
            and best-use scenarios.
        """
        return {
            "name": "statistical",
            "description": (
                "Statistical distribution-based synthesis with copula "
                "correlation preservation"
            ),
            "supported_distributions": [
                "normal",
                "lognormal",
                "poisson",
                "exponential",
                "uniform",
                "categorical",
                "empirical",
            ],
            "correlation_methods": ["gaussian_copula", "empirical", "none"],
            "supports_gpu": False,
            "supports_training": False,
            "supports_pretrained": False,
            "best_for": [
                "known_distributions",
                "correlation_preservation",
                "fast_generation",
                "large_volumes",
            ],
            "column_types": ["numeric", "categorical", "datetime"],
            "preserves_correlations": True,
        }

    # ------------------------------------------------------------------
    # Distribution Fitting
    # ------------------------------------------------------------------

    def _fit_all_distributions(
        self,
        column_specs: list[ColumnSpec],  # noqa: ARG002
        numeric_cols: list[str],
        datetime_cols: list[str],
        categorical_cols: list[str],
        profile_columns: dict[str, Any],
    ) -> dict[str, DistributionFit]:
        """Fit distributions for every column in the schema.

        Delegates to :meth:`_fit_distribution` for numeric / datetime columns
        and :meth:`_fit_categorical` for categorical columns, respecting
        user-supplied overrides in ``self._stat_config.distributions``.

        Returns:
            Mapping of column name → :class:`DistributionFit`.
        """
        fitted: dict[str, DistributionFit] = {}

        for col_name in numeric_cols + datetime_cols:
            col_stats = profile_columns.get(col_name, {})
            if (
                self._stat_config.distributions
                and col_name in self._stat_config.distributions
            ):
                fitted[col_name] = self._stat_config.distributions[col_name]
            elif self._stat_config.auto_fit:
                fitted[col_name] = self._fit_distribution(col_stats, col_name)
            else:
                mean = float(col_stats.get("mean", 0.0))
                std = max(float(col_stats.get("std", 1.0)), 1e-10)
                fitted[col_name] = DistributionFit(
                    distribution_type="normal",
                    parameters={"loc": mean, "scale": std},
                    goodness_of_fit=0.0,
                    column_name=col_name,
                )

        for col_name in categorical_cols:
            col_stats = profile_columns.get(col_name, {})
            if (
                self._stat_config.distributions
                and col_name in self._stat_config.distributions
            ):
                fitted[col_name] = self._stat_config.distributions[col_name]
            else:
                frequencies: dict[str, float] = col_stats.get(
                    "frequencies", col_stats.get("value_counts", {})
                )
                if frequencies:
                    fitted[col_name] = self._fit_categorical(
                        frequencies, col_name
                    )
                else:
                    fitted[col_name] = DistributionFit(
                        distribution_type="categorical",
                        parameters={"unknown": 1.0},
                        goodness_of_fit=1.0,
                        column_name=col_name,
                    )

        return fitted

    # -- _fit_distribution helpers (extracted for PLR0915) -----------------

    @staticmethod
    def _refine_via_mle(
        candidate_name: str,
        profile_sample: np.ndarray,
        params: dict[str, float],
    ) -> dict[str, float]:
        """Attempt MLE refinement of *params*; returns originals on failure."""
        if len(profile_sample) <= 10:
            return params
        try:
            if candidate_name == "lognormal":
                positive = profile_sample[profile_sample > 0]
                if len(positive) <= 10:
                    return params
                fit_result = lognorm.fit(positive, floc=0)
                return {
                    "s": float(fit_result[0]),
                    "loc": float(fit_result[1]),
                    "scale": float(fit_result[2]),
                }
            if candidate_name == "exponential":
                fit_result = expon.fit(profile_sample)
                return {"loc": float(fit_result[0]), "scale": float(fit_result[1])}
            if candidate_name == "uniform":
                fit_result = uniform.fit(profile_sample)
                return {"loc": float(fit_result[0]), "scale": float(fit_result[1])}
        except Exception:  # noqa: S110
            pass  # Keep method-of-moments params.
        return params

    def _empirical_fallback(
        self,
        column_name: str,
        percentile_values: list[tuple[float, float]],
        mean_val: float,
        std_val: float,
        best_pvalue: float,
    ) -> DistributionFit:
        """Build an empirical :class:`DistributionFit` when parametric fits fail."""
        self.logger.info(
            "falling_back_to_empirical",
            column=column_name,
            best_pvalue=round(best_pvalue, 6),
            significance_level=self._stat_config.significance_level,
        )
        pct_dict: dict[str, float] = {
            f"p{int(p[0] * 100)}": p[1] for p in percentile_values
        }
        pct_dict["mean"] = mean_val
        pct_dict["std"] = std_val
        return DistributionFit(
            distribution_type="empirical",
            parameters=pct_dict,
            goodness_of_fit=max(best_pvalue, 0.0),
            column_name=column_name,
        )

    # -- Main fit entry point ---------------------------------------------

    def _fit_distribution(
        self,
        column_stats: dict[str, Any],
        column_name: str,
    ) -> DistributionFit:
        """Fit the best parametric distribution to a column's statistical profile.

        Tries each candidate in :attr:`StatisticalConfig.fit_candidates`,
        estimates parameters via method of moments, then evaluates goodness
        of fit with the Kolmogorov-Smirnov test against a synthetic reference
        sample built from the profile's percentiles.

        Falls back to an empirical distribution when no parametric family
        achieves the configured significance level.

        Args:
            column_stats: Statistical summary for the column (mean, std, min,
                max, percentiles, count).
            column_name: Name of the column being fitted.

        Returns:
            A :class:`DistributionFit` describing the best-fit distribution.
        """
        mean_val = float(column_stats.get("mean", 0.0))
        std_val = float(column_stats.get("std", 1.0))
        min_val = float(column_stats.get("min", mean_val - 3.0 * std_val))
        max_val = float(column_stats.get("max", mean_val + 3.0 * std_val))
        count = int(column_stats.get("count", 1000))
        std_val = max(std_val, 1e-10)

        percentile_values = self._extract_percentiles(
            column_stats, mean_val, std_val, min_val, max_val,
        )

        best_fit: DistributionFit | None = None
        best_pvalue: float = -1.0

        for candidate_name in self._stat_config.fit_candidates:
            if _SCIPY_DIST_MAP.get(candidate_name) is None:
                continue
            try:
                sample_size = min(count, 5000)
                pct_q = [p[0] for p in percentile_values]
                pct_v = [p[1] for p in percentile_values]
                profile_sample = np.interp(
                    np.random.random(sample_size), pct_q, pct_v,
                )

                params = self._estimate_params(
                    candidate_name, mean_val, std_val, min_val, max_val,
                )
                params = self._refine_via_mle(
                    candidate_name, profile_sample, params,
                )

                ks_stat, p_value = self._ks_test_for_candidate(
                    profile_sample, candidate_name, params,
                )
                self.logger.debug(
                    "distribution_fit_candidate",
                    column=column_name,
                    candidate=candidate_name,
                    ks_statistic=round(ks_stat, 6),
                    p_value=round(p_value, 6),
                )

                if p_value > best_pvalue:
                    best_pvalue = p_value
                    best_fit = DistributionFit(
                        distribution_type=candidate_name,  # type: ignore[arg-type]
                        parameters=self._params_to_dict(candidate_name, params),
                        goodness_of_fit=round(p_value, 6),
                        column_name=column_name,
                    )
            except Exception as exc:
                self.logger.debug(
                    "distribution_fit_candidate_failed",
                    column=column_name,
                    candidate=candidate_name,
                    error=str(exc),
                )
                continue

        if best_fit is None or best_pvalue < self._stat_config.significance_level:
            best_fit = self._empirical_fallback(
                column_name, percentile_values, mean_val, std_val, best_pvalue,
            )

        self.logger.info(
            "distribution_fitted",
            column=column_name,
            distribution=best_fit.distribution_type,
            goodness_of_fit=best_fit.goodness_of_fit,
        )
        return best_fit

    def _fit_categorical(
        self,
        frequencies: dict[str, float],
        column_name: str,
    ) -> DistributionFit:
        """Fit a categorical distribution from observed value frequencies.

        Computes a probability vector from frequency counts with Laplace
        smoothing to handle unseen categories and zero-frequency values.

        Args:
            frequencies: Mapping of category values to their counts or
                proportions.
            column_name: Name of the column being fitted.

        Returns:
            A :class:`DistributionFit` with ``distribution_type='categorical'``
            and probability parameters for each category.
        """
        if not frequencies:
            return DistributionFit(
                distribution_type="categorical",
                parameters={"unknown": 1.0},
                goodness_of_fit=0.0,
                column_name=column_name,
            )

        total = sum(float(v) for v in frequencies.values())
        if total <= 0.0:
            total = 1.0

        num_categories = len(frequencies)
        smoothing = self._stat_config.categorical_smoothing

        # Laplace smoothing: P(x) = (count(x) + a) / (N + a*K)  [a = smoothing]
        smoothed_total = total + smoothing * num_categories
        prob_params: dict[str, float] = {}
        for category, cnt in frequencies.items():
            prob_params[str(category)] = (float(cnt) + smoothing) / smoothed_total

        # Use chi2 distribution quantile as a reference for goodness-of-fit:
        # compare observed proportions against a uniform null.
        expected_prob = 1.0 / max(num_categories, 1)
        chi2_stat = sum(
            ((p - expected_prob) ** 2) / expected_prob
            for p in prob_params.values()
        )
        # Reference critical value from the chi2 distribution.
        df_val = max(num_categories - 1, 1)
        chi2_critical = float(chi2.ppf(0.95, df_val))
        gof = 1.0 - min(chi2_stat / max(chi2_critical, 1e-10), 1.0)

        # Verify using multinomial PMF for the most likely category count
        # (informational, logged for diagnostics).
        try:
            n_check = 100
            most_common_prob = max(prob_params.values())
            expected_count = int(n_check * most_common_prob)
            counts_vector = np.zeros(num_categories, dtype=int)
            counts_vector[0] = expected_count
            counts_vector[-1] = n_check - expected_count
            probs = np.array(list(prob_params.values()), dtype=np.float64)
            probs = probs / probs.sum()
            _multinomial_pmf = float(multinomial.pmf(counts_vector, n=n_check, p=probs))
            self.logger.debug(
                "categorical_multinomial_check",
                column=column_name,
                multinomial_pmf=round(_multinomial_pmf, 8),
            )
        except Exception:  # noqa: S110
            pass  # Non-critical diagnostic — safe to skip.

        self.logger.info(
            "categorical_distribution_fitted",
            column=column_name,
            num_categories=num_categories,
            goodness_of_fit=round(gof, 6),
        )

        return DistributionFit(
            distribution_type="categorical",
            parameters=prob_params,
            goodness_of_fit=round(gof, 6),
            column_name=column_name,
        )

    # ------------------------------------------------------------------
    # Gaussian Copula Generation
    # ------------------------------------------------------------------

    def _generate_copula_samples(
        self,
        n_samples: int,
        n_columns: int,
        correlation_matrix: np.ndarray,
    ) -> np.ndarray:
        """Generate correlated uniform variates using a Gaussian copula.

        The Gaussian copula pipeline:

        1. Decompose the correlation matrix via Cholesky: ``L = chol(Σ)``
        2. Generate independent standard normals: ``Z ~ N(0, I)``
        3. Apply correlation: ``X = Z · Lᵀ``
        4. Transform to uniform: ``U = Φ(X)``

        Args:
            n_samples: Number of sample rows to generate.
            n_columns: Number of columns (dimensions).
            correlation_matrix: Positive semi-definite correlation matrix of
                shape ``(n_columns, n_columns)``.

        Returns:
            An ``(n_samples, n_columns)`` array of correlated uniform ``[0, 1]``
            variates.

        Raises:
            GenerationError: If Cholesky decomposition fails even after PSD
                correction.
        """
        # Ensure the matrix is positive semi-definite.
        psd_matrix = self._ensure_positive_semidefinite(correlation_matrix)

        try:
            # Lower-triangular Cholesky factor.
            lower: np.ndarray = cholesky(psd_matrix, lower=True)
        except Exception as exc:
            self.logger.error(
                "cholesky_decomposition_failed",
                error=str(exc),
                matrix_shape=list(psd_matrix.shape),
            )
            raise GenerationError(
                message=f"Cholesky decomposition failed: {exc}",
                method="statistical",
                details={
                    "error": str(exc),
                    "matrix_shape": list(psd_matrix.shape),
                },
            ) from exc

        # Generate independent standard normal variates.
        z: np.ndarray = np.random.standard_normal((n_samples, n_columns))

        # Apply correlation structure via Cholesky factor.
        x: np.ndarray = np.dot(z, lower.T)

        # Transform to uniform [0, 1] via the standard normal CDF Φ.
        u: np.ndarray = norm.cdf(x)

        # Clip to avoid exact 0.0 or 1.0 (which cause ±∞ from ppf).
        u = np.clip(u, 1e-10, 1.0 - 1e-10)

        self.logger.debug(
            "copula_samples_generated",
            n_samples=n_samples,
            n_columns=n_columns,
        )

        return u

    def _transform_to_marginal(
        self,
        uniform_values: np.ndarray,
        distribution: DistributionFit,
    ) -> np.ndarray:
        """Transform uniform ``[0,1]`` variates to a target marginal distribution.

        For parametric distributions the inverse CDF (percent-point function)
        is applied.  For categorical distributions, uniform ranges are mapped
        to categories via cumulative probability thresholds.  For empirical
        distributions an interpolated inverse CDF from stored percentiles is
        used.

        Args:
            uniform_values: Array of uniform variates in ``[0, 1]``.
            distribution: The fitted distribution to transform into.

        Returns:
            Array of values drawn from the target distribution.
        """
        dist_type = distribution.distribution_type
        params = distribution.parameters

        if dist_type == "categorical":
            return self._transform_categorical(uniform_values, params)

        if dist_type == "empirical":
            return self._transform_empirical(uniform_values, params)

        # Parametric distributions — apply inverse CDF (ppf).
        scipy_dist = _SCIPY_DIST_MAP.get(dist_type)
        if scipy_dist is None:
            self.logger.warning(
                "unknown_distribution_type",
                distribution=dist_type,
                column=distribution.column_name,
            )
            return uniform_values

        try:
            result: np.ndarray
            if dist_type == "normal":
                result = norm.ppf(
                    uniform_values,
                    loc=params.get("loc", 0.0),
                    scale=params.get("scale", 1.0),
                )
            elif dist_type == "lognormal":
                result = lognorm.ppf(
                    uniform_values,
                    s=params.get("s", params.get("shape", 1.0)),
                    loc=params.get("loc", 0.0),
                    scale=params.get("scale", 1.0),
                )
            elif dist_type == "poisson":
                result = poisson.ppf(
                    uniform_values,
                    mu=params.get("mu", params.get("loc", 1.0)),
                )
            elif dist_type == "exponential":
                result = expon.ppf(
                    uniform_values,
                    loc=params.get("loc", 0.0),
                    scale=params.get("scale", 1.0),
                )
            elif dist_type == "uniform":
                result = uniform.ppf(
                    uniform_values,
                    loc=params.get("loc", 0.0),
                    scale=params.get("scale", 1.0),
                )
            else:
                result = uniform_values

            return np.asarray(result, dtype=np.float64)

        except Exception as exc:
            self.logger.warning(
                "marginal_transform_failed",
                distribution=dist_type,
                column=distribution.column_name,
                error=str(exc),
            )
            return uniform_values

    # ------------------------------------------------------------------
    # Correlation Matrix Operations
    # ------------------------------------------------------------------

    def _resolve_correlation_matrix(
        self,
        profile: dict[str, Any],
        copula_numeric_cols: list[str],
    ) -> np.ndarray:
        """Resolve the correlation matrix to use for copula generation.

        Prefers the user-supplied matrix from :attr:`StatisticalConfig.correlation`,
        falling back to computing one from the profile.

        Returns:
            A positive semi-definite correlation matrix.
        """
        if self._stat_config.correlation.correlation_matrix is not None:
            raw_matrix = np.array(
                self._stat_config.correlation.correlation_matrix,
                dtype=np.float64,
            )
            n_expected = len(copula_numeric_cols)
            if raw_matrix.shape[0] == n_expected and raw_matrix.shape[1] == n_expected:
                return self._ensure_positive_semidefinite(raw_matrix)
            self.logger.warning(
                "correlation_matrix_dimension_mismatch",
                expected=n_expected,
                received=raw_matrix.shape[0],
            )

        return self._compute_correlation_matrix(profile, copula_numeric_cols)

    def _compute_correlation_matrix(
        self,
        profile: dict[str, Any],
        columns: list[str],
    ) -> np.ndarray:
        """Build the correlation matrix from the statistical profile.

        Extracts pairwise correlations from the profile.  When no correlation
        data is available, returns an identity matrix (independence assumption).

        Args:
            profile: Statistical profile dictionary, optionally containing a
                ``"correlation_matrix"`` or ``"correlations"`` key.
            columns: Ordered list of column names for matrix rows / columns.

        Returns:
            A symmetric, PSD correlation matrix of shape ``(n, n)``.
        """
        n = len(columns)
        if n == 0:
            return np.eye(0)

        matrix = np.eye(n, dtype=np.float64)

        # 1) Try a pre-computed full correlation matrix in the profile.
        profile_corr = profile.get("correlation_matrix")
        if profile_corr is not None:
            try:
                raw_matrix = np.array(profile_corr, dtype=np.float64)
                if raw_matrix.shape == (n, n):
                    matrix = raw_matrix.copy()
                    np.fill_diagonal(matrix, 1.0)
                    return self._ensure_positive_semidefinite(matrix)
            except Exception as exc:
                self.logger.warning(
                    "profile_correlation_matrix_invalid",
                    error=str(exc),
                )

        # 2) Fill from pairwise and per-column nested correlation data.
        column_index = {name: idx for idx, name in enumerate(columns)}
        self._fill_pairwise_correlations(
            profile.get("correlations", {}), column_index, matrix,
        )
        self._fill_nested_correlations(
            profile.get("columns", {}), column_index, matrix,
        )

        np.fill_diagonal(matrix, 1.0)
        matrix = self._ensure_positive_semidefinite(matrix)

        self.logger.debug(
            "correlation_matrix_built",
            num_columns=n,
            non_identity_pairs=int(
                np.sum(np.abs(matrix - np.eye(n)) > 0.01) // 2
            ),
        )

        return matrix

    @staticmethod
    def _fill_pairwise_correlations(
        correlations: dict[str, Any],
        column_index: dict[str, int],
        matrix: np.ndarray,
    ) -> None:
        """Populate *matrix* from ``"col_a,col_b"`` → value entries."""
        for pair_key, corr_val in correlations.items():
            parts = pair_key.split(",") if isinstance(pair_key, str) else []
            if len(parts) != 2:
                continue
            col_a, col_b = parts[0].strip(), parts[1].strip()
            if col_a in column_index and col_b in column_index:
                i, j = column_index[col_a], column_index[col_b]
                try:
                    corr = float(np.clip(float(corr_val), -1.0, 1.0))
                    matrix[i, j] = corr
                    matrix[j, i] = corr
                except (ValueError, TypeError):
                    continue

    @staticmethod
    def _fill_nested_correlations(
        profile_columns: dict[str, Any],
        column_index: dict[str, int],
        matrix: np.ndarray,
    ) -> None:
        """Populate *matrix* from per-column nested correlation dictionaries."""
        for col_name, col_stats in profile_columns.items():
            if col_name not in column_index:
                continue
            col_corrs: dict[str, Any] = col_stats.get("correlations", {})
            for other_col, corr_val in col_corrs.items():
                if other_col in column_index:
                    i, j = column_index[col_name], column_index[other_col]
                    try:
                        corr = float(np.clip(float(corr_val), -1.0, 1.0))
                        matrix[i, j] = corr
                        matrix[j, i] = corr
                    except (ValueError, TypeError):
                        continue

    def _ensure_positive_semidefinite(self, matrix: np.ndarray) -> np.ndarray:
        """Project a symmetric matrix to the nearest positive semi-definite matrix.

        Uses eigendecomposition to clip negative eigenvalues to a small
        positive epsilon, then reconstructs and renormalises the diagonal
        to 1.0 (correlation matrix convention).

        Args:
            matrix: A symmetric matrix (possibly not PSD due to rounding or
                incomplete pairwise correlations).

        Returns:
            The nearest PSD correlation matrix with unit diagonal.
        """
        # Force symmetry.
        sym_matrix = (matrix + matrix.T) / 2.0

        # Eigendecomposition (sorted eigenvalues for symmetric matrices).
        eigenvalues, eigenvectors = np.linalg.eigh(sym_matrix)

        # Clip negative eigenvalues to a small positive value.
        epsilon = 1e-10
        eigenvalues_clipped = np.clip(eigenvalues, epsilon, None)

        # Reconstruct from corrected eigenvalues.
        reconstructed: np.ndarray = (
            eigenvectors @ np.diag(eigenvalues_clipped) @ eigenvectors.T
        )

        # Renormalise diagonal to 1.0 (correlation matrix).
        diag_sqrt = np.sqrt(np.diag(reconstructed))
        diag_sqrt[diag_sqrt < epsilon] = 1.0
        normalised: np.ndarray = reconstructed / np.outer(diag_sqrt, diag_sqrt)

        # Ensure exact unit diagonal.
        np.fill_diagonal(normalised, 1.0)

        return normalised

    # ------------------------------------------------------------------
    # Private Helpers — Parameter Estimation & Testing
    # ------------------------------------------------------------------

    def _estimate_params(
        self,
        dist_name: str,
        mean: float,
        std: float,
        min_val: float,
        max_val: float,
    ) -> dict[str, float]:
        """Estimate distribution parameters via method of moments.

        Args:
            dist_name: Distribution family name.
            mean: Sample mean from the profile.
            std: Sample standard deviation from the profile.
            min_val: Minimum value from the profile.
            max_val: Maximum value from the profile.

        Returns:
            Dictionary of estimated distribution parameters.
        """
        std = max(std, 1e-10)

        if dist_name == "normal":
            return {"loc": mean, "scale": std}

        if dist_name == "lognormal":
            # Method of moments for log-normal: σ² = ln(1 + var / μ²)
            if mean > 0:
                variance = std ** 2
                sigma_sq = np.log(1.0 + variance / (mean ** 2))
                sigma = float(np.sqrt(max(sigma_sq, 1e-10)))
                mu = float(np.log(mean) - sigma_sq / 2.0)
                return {"s": sigma, "loc": 0.0, "scale": float(np.exp(mu))}
            return {"s": 1.0, "loc": 0.0, "scale": max(abs(mean), 1e-10)}

        if dist_name == "poisson":
            return {"mu": max(mean, 0.1)}

        if dist_name == "exponential":
            return {"loc": min_val, "scale": max(mean - min_val, 1e-10)}

        if dist_name == "uniform":
            return {"loc": min_val, "scale": max(max_val - min_val, 1e-10)}

        return {"loc": mean, "scale": std}

    @staticmethod
    def _params_to_dict(
        dist_name: str,  # noqa: ARG004
        params: dict[str, float],
    ) -> dict[str, float]:
        """Normalise a parameter dictionary for consistent storage.

        Args:
            dist_name: Distribution family name (unused, kept for symmetry).
            params: Raw parameter dictionary.

        Returns:
            Cleaned parameter dictionary with all values cast to ``float``.
        """
        return {k: float(v) for k, v in params.items()}

    def _ks_test_for_candidate(
        self,
        sample: np.ndarray,
        dist_name: str,
        params: dict[str, float],
    ) -> tuple[float, float]:
        """Run a one-sample Kolmogorov-Smirnov test for a candidate distribution.

        Uses :func:`scipy.stats.kstest` to compare the empirical CDF of
        *sample* against the theoretical CDF of the candidate.

        Args:
            sample: Empirical sample array constructed from profile percentiles.
            dist_name: Name of the candidate distribution.
            params: Estimated parameters for the candidate distribution.

        Returns:
            ``(ks_statistic, p_value)`` tuple.
        """
        try:
            if dist_name == "normal":
                stat, pval = kstest(
                    sample, "norm", args=(params["loc"], params["scale"])
                )
            elif dist_name == "lognormal":
                stat, pval = kstest(
                    sample,
                    "lognorm",
                    args=(params["s"], params.get("loc", 0.0), params["scale"]),
                )
            elif dist_name == "exponential":
                stat, pval = kstest(
                    sample,
                    "expon",
                    args=(params.get("loc", 0.0), params["scale"]),
                )
            elif dist_name == "uniform":
                stat, pval = kstest(
                    sample,
                    "uniform",
                    args=(params["loc"], params["scale"]),
                )
            elif dist_name == "poisson":
                # Poisson is discrete — use a two-sample test with a reference
                # sample generated from the fitted distribution.
                mu = params.get("mu", 1.0)
                ref_sample = scipy_stats.poisson.rvs(
                    mu=mu, size=len(sample)
                ).astype(np.float64)
                stat, pval = scipy_stats.ks_2samp(sample, ref_sample)
            else:
                stat, pval = 0.0, 0.0

            return float(stat), float(pval)
        except Exception:
            return 0.0, 0.0

    def _extract_percentiles(
        self,
        column_stats: dict[str, Any],
        mean: float,
        std: float,
        min_val: float,
        max_val: float,
    ) -> list[tuple[float, float]]:
        """Extract or synthesise (quantile, value) pairs from profile stats.

        Args:
            column_stats: Column statistical summary.
            mean: Column mean.
            std: Column standard deviation.
            min_val: Column minimum.
            max_val: Column maximum.

        Returns:
            Sorted list of ``(quantile_fraction, value)`` tuples.
        """
        percentiles_raw: dict[str, Any] = column_stats.get("percentiles", {})
        points: list[tuple[float, float]] = []

        for pct_key, pct_val in percentiles_raw.items():
            try:
                points.append((float(pct_key) / 100.0, float(pct_val)))
            except (ValueError, TypeError):
                continue

        if points:
            points.sort(key=lambda t: t[0])
            if points[0][0] > 0.01:
                points.insert(0, (0.0, min_val))
            if points[-1][0] < 0.99:
                points.append((1.0, max_val))
        else:
            # Synthesise from mean/std assuming approximate normality.
            points = [
                (0.0, min_val),
                (0.25, mean - 0.6745 * std),
                (0.50, mean),
                (0.75, mean + 0.6745 * std),
                (1.0, max_val),
            ]

        return points

    # ------------------------------------------------------------------
    # Private Helpers — Marginal Transforms
    # ------------------------------------------------------------------

    @staticmethod
    def _transform_categorical(
        uniform_values: np.ndarray,
        params: dict[str, float],
    ) -> np.ndarray:
        """Map uniform variates to categorical values via cumulative probabilities.

        Args:
            uniform_values: Array of uniform variates in ``[0, 1]``.
            params: Dictionary mapping category names to their probabilities.

        Returns:
            Object-dtype array of category string values.
        """
        categories: list[str] = list(params.keys())
        probabilities = np.array([params[c] for c in categories], dtype=np.float64)

        # Normalise probabilities.
        prob_sum = probabilities.sum()
        if prob_sum > 0:
            probabilities = probabilities / prob_sum

        # Build cumulative probability thresholds.
        cum_probs = np.cumsum(probabilities)
        cum_probs[-1] = 1.0  # avoid floating-point rounding issues

        # Map uniform values to category indices using searchsorted.
        indices = np.searchsorted(cum_probs, uniform_values, side="left")
        indices = np.clip(indices, 0, len(categories) - 1)

        result = np.array([categories[i] for i in indices], dtype=object)
        return result

    @staticmethod
    def _transform_empirical(
        uniform_values: np.ndarray,
        params: dict[str, float],
    ) -> np.ndarray:
        """Transform uniform variates via an interpolated empirical inverse CDF.

        Constructs a piecewise-linear inverse CDF from the stored percentiles
        in the parameter dictionary.

        Args:
            uniform_values: Array of uniform variates in ``[0, 1]``.
            params: Dictionary with percentile keys (e.g., ``p0``, ``p25``,
                ``p50``, ``p75``, ``p100``) and ``mean``/``std`` fallbacks.

        Returns:
            Array of values from the empirical distribution.
        """
        pct_points: list[tuple[float, float]] = []
        for key, val in params.items():
            if key.startswith("p") and key[1:].isdigit():
                pct = float(key[1:]) / 100.0
                pct_points.append((pct, float(val)))

        if len(pct_points) < 2:
            # Fallback to normal using mean / std from params.
            mean_val = params.get("mean", 0.0)
            std_val = params.get("std", 1.0)
            return np.asarray(
                norm.ppf(uniform_values, loc=mean_val, scale=max(std_val, 1e-10)),
                dtype=np.float64,
            )

        pct_points.sort(key=lambda t: t[0])
        quantiles = np.array([p[0] for p in pct_points])
        values = np.array([p[1] for p in pct_points])

        result = np.interp(uniform_values, quantiles, values)
        return result

    # ------------------------------------------------------------------
    # Private Helpers — Post-processing
    # ------------------------------------------------------------------

    def _postprocess(
        self,
        generated_data: dict[str, np.ndarray],
        column_specs: list[ColumnSpec],
        column_type_map: dict[str, str],
        profile_columns: dict[str, Any],
        num_records: int,
    ) -> pd.DataFrame:
        """Post-process generated arrays into a clean :class:`pandas.DataFrame`.

        Performs type-specific transformations:

        - Rounds integer columns to whole numbers.
        - Clips values to valid ranges from schema constraints.
        - Converts epoch timestamps back to datetime objects.
        - Ensures categorical columns contain valid string values.
        - Handles outliers according to the configured strategy.

        Args:
            generated_data: Column name → generated numpy array mapping.
            column_specs: Validated column specifications from the schema.
            column_type_map: Column name → data type string mapping.
            profile_columns: Per-column profile statistics for range info.
            num_records: Expected number of output records.

        Returns:
            A fully-processed :class:`pandas.DataFrame`.
        """
        processed: dict[str, pd.Series | np.ndarray] = {}

        for cs in column_specs:
            col_name = cs.name
            values = generated_data.get(col_name)
            if values is None:
                values = self._default_fill(
                    column_type_map.get(col_name, ""), num_records,
                )

            col_stats = profile_columns.get(col_name, {})
            data_type = column_type_map.get(col_name, cs.data_type.lower())
            processed[col_name] = self._postprocess_column(
                values, data_type, col_stats, cs,
            )

        df = pd.DataFrame(processed)
        expected_cols = [cs.name for cs in column_specs if cs.name in df.columns]
        return df[expected_cols].copy()

    @staticmethod
    def _default_fill(dtype_key: str, num_records: int) -> np.ndarray:
        """Generate default fill values for a missing generated column."""
        if dtype_key in _NUMERIC_TYPES:
            return np.zeros(num_records, dtype=np.float64)
        if dtype_key == "boolean":
            return np.array(np.random.choice([True, False], size=num_records))
        return np.full(num_records, "", dtype=object)

    def _postprocess_column(
        self,
        values: Any,
        data_type: str,
        col_stats: dict[str, Any],
        cs: ColumnSpec,
    ) -> pd.Series | np.ndarray:
        """Apply type-specific post-processing for a single generated column."""
        if data_type in ("integer", "int", "bigint"):
            series = pd.Series(values, dtype=np.float64).round(0)
            return self._apply_range_clip(series, cs, col_stats).astype(np.int64)

        if data_type in ("float", "decimal", "number"):
            series = self._apply_range_clip(
                pd.Series(values, dtype=np.float64), cs, col_stats,
            )
            precision = (cs.constraints or {}).get("precision")
            return series.round(int(precision)) if precision is not None else series

        if data_type in _DATETIME_TYPES:
            series = pd.Series(values, dtype=np.float64)
            min_epoch = float(col_stats.get("min_epoch", 0.0))
            max_epoch = float(col_stats.get("max_epoch", 4102444800.0))
            return pd.to_datetime(
                series.clip(lower=min_epoch, upper=max_epoch), unit="s", utc=True,
            )

        if data_type == "boolean":
            if hasattr(values, "dtype") and values.dtype == object:
                return pd.Series(values).map(
                    lambda x: str(x).lower() in ("true", "1", "yes"),
                )
            return pd.Series(values, dtype=bool)

        return pd.Series(values, dtype=object).apply(str)

    def _apply_range_clip(
        self,
        series: pd.Series,
        col_spec: ColumnSpec,
        col_stats: dict[str, Any],
    ) -> pd.Series:
        """Clip a numeric series to the range defined by constraints or profile.

        Priority:

        1. Explicit schema constraints (``min`` / ``max`` in ``col_spec.constraints``).
        2. Profile statistics (``min`` / ``max`` in ``col_stats``), only when
           ``outlier_handling == 'clip'``.
        3. No clipping when ``outlier_handling == 'keep'``.

        Args:
            series: Numeric pandas Series.
            col_spec: Column specification with optional constraints.
            col_stats: Profile statistics for the column.

        Returns:
            Clipped series.
        """
        constraints = col_spec.constraints or {}
        min_c = constraints.get("min")
        max_c = constraints.get("max")

        if min_c is not None or max_c is not None:
            return series.clip(
                lower=float(min_c) if min_c is not None else None,
                upper=float(max_c) if max_c is not None else None,
            )

        if self._stat_config.outlier_handling == "clip":
            p_min = col_stats.get("min")
            p_max = col_stats.get("max")
            if p_min is not None or p_max is not None:
                return series.clip(
                    lower=float(p_min) if p_min is not None else None,
                    upper=float(p_max) if p_max is not None else None,
                )

        return series
