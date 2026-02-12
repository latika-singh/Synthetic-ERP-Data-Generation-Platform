"""Statistical distribution analysis module for the Profiling Service.

Implements the :class:`StatisticalProfiler` class, which fits ERP data‑column
metadata to statistical distributions (normal, log‑normal, Poisson,
exponential, gamma, beta, Weibull, uniform, categorical) via **SciPy 1.12+**
distribution fitting and Kolmogorov–Smirnov / chi‑square goodness‑of‑fit
testing.

For every column the profiler computes:

* Descriptive statistics — mean, median, std‑dev, variance, skewness, kurtosis.
* Percentiles — p1 through p99, IQR.
* Distribution parameters — best‑fit distribution type, shape/loc/scale.
* Frequency histograms (numeric) and category counts (categorical).
* Outlier detection — IQR method with configurable fencing.
* Cardinality, null‑rate, uniqueness.
* Intra‑table correlations — Pearson, Spearman, Cramér's V, mutual info.

Results are assembled into :class:`ColumnProfile`, :class:`TableProfile`,
and :class:`StatisticalProfile` Pydantic models and persisted in the
``statistical_profiles`` MongoDB collection.

**Constraint C‑001 Compliance:**
    The profiler operates on **statistical metadata and sample data only** —
    it never accesses raw production data directly.  All inputs arrive from
    the discovery layer which itself only retrieves schema metadata.

**Consumers:**

* **Generation Engine** — reads distribution parameters for accurate
  statistical synthesis with copula‑based multivariate correlation.
* **Quality Service** — reads profiles for fidelity validation (40 %
  statistical weight in the quality scoring model).

Design Patterns:

* **Strategy** — each distribution candidate is tested via a uniform
  fit→test→score pipeline, and the best‑fit is selected.
* **Builder** — column → table → schema profiles are assembled bottom‑up.
"""

from __future__ import annotations

import math
import time
from datetime import datetime, timezone
from enum import Enum
from typing import Any, Optional, Union

import numpy as np
import pandas as pd
from pydantic import BaseModel, Field
from scipy import stats as scipy_stats

from profiling_service.config import get_config
from profiling_service.models.statistical_profile import (
    ColumnProfile,
    CorrelationEntry,
    DataCategory,
    DistributionParameters,
    DistributionType,
    FrequencyDistribution,
    PatternMetadata,
    Percentiles,
    ProfileStatus,
    StatisticalProfile,
    TableProfile,
    ValueRange,
)
from shared.logging.structured_logger import get_logger

# ---------------------------------------------------------------------------
# Module‑level logger
# ---------------------------------------------------------------------------

_logger = get_logger(__name__)

# ---------------------------------------------------------------------------
# Distribution candidate list — (DistributionType, scipy distribution object)
# ---------------------------------------------------------------------------

DISTRIBUTION_CANDIDATES: list[tuple[DistributionType, Any]] = [
    (DistributionType.NORMAL, scipy_stats.norm),
    (DistributionType.LOG_NORMAL, scipy_stats.lognorm),
    (DistributionType.EXPONENTIAL, scipy_stats.expon),
    (DistributionType.GAMMA, scipy_stats.gamma),
    (DistributionType.BETA, scipy_stats.beta),
    (DistributionType.WEIBULL, scipy_stats.weibull_min),
    (DistributionType.UNIFORM, scipy_stats.uniform),
]

# Minimum sample size thresholds for certain statistical tests.
_MIN_SAMPLE_KS = 5
_MIN_SAMPLE_CORRELATION = 10


# ===================================================================
# StatisticalProfiler
# ===================================================================


class StatisticalProfiler:
    """Fits ERP column metadata to statistical distributions and computes
    descriptive statistics, percentiles, correlations, and outlier boundaries.

    The profiler is the *statistical synthesis* backbone:
    the :class:`~generation_engine.generators.statistical_generator.StatisticalGenerator`
    reads the resulting :class:`StatisticalProfile` to reproduce source
    distributions with ≥ 95 % fidelity (weighted 40 % in the quality model).

    Args:
        config: Optional override dict.  When ``None``, configuration is
            loaded from :func:`~profiling_service.config.get_config`.
    """

    # ---------------------------------------------------------------
    # Initialisation
    # ---------------------------------------------------------------

    def __init__(self, config: Optional[dict] = None) -> None:
        """Initialise the statistical profiler.

        Args:
            config: Optional configuration override dict.  When ``None``
                the environment‑based config is loaded.
        """
        self._logger = get_logger(__name__)

        if config is not None:
            self._sample_size: int = int(config.get("sample_size", 10_000))
            self._max_categories: int = int(config.get("max_categories", 100))
            self._distribution_bins: int = int(config.get("distribution_bins", 50))
            self._timeout_seconds: int = int(config.get("timeout_seconds", 600))
        else:
            try:
                svc_config = get_config()
                self._sample_size = int(getattr(svc_config, "PROFILING_SAMPLE_SIZE", 10_000))
                self._max_categories = int(getattr(svc_config, "PROFILING_MAX_CATEGORIES", 100))
                self._distribution_bins = int(getattr(svc_config, "PROFILING_DISTRIBUTION_BINS", 50))
                self._timeout_seconds = int(getattr(svc_config, "PROFILING_TIMEOUT_SECONDS", 600))
            except Exception:
                self._sample_size = 10_000
                self._max_categories = 100
                self._distribution_bins = 50
                self._timeout_seconds = 600

        self._logger.info(
            "statistical_profiler_initialised",
            sample_size=self._sample_size,
            max_categories=self._max_categories,
            distribution_bins=self._distribution_bins,
            timeout_seconds=self._timeout_seconds,
        )

    # ---------------------------------------------------------------
    # Public API — profile_table
    # ---------------------------------------------------------------

    def profile_table(
        self,
        table_name: str,
        column_metadata: list[dict],
        sample_data: dict[str, list],
    ) -> TableProfile:
        """Profile all columns in a single ERP table.

        For each column listed in *column_metadata* the profiler:

        1. Extracts the sample values from *sample_data*.
        2. Classifies the column into a :class:`DataCategory`.
        3. Computes descriptive statistics, distribution parameters,
           frequency distributions, outlier boundaries, etc.
        4. Assembles the result into a :class:`ColumnProfile`.

        After all columns are profiled, inter‑column correlations are
        computed and the result is wrapped in a :class:`TableProfile`.

        Args:
            table_name: Physical table name.
            column_metadata: List of dicts, each containing at minimum
                ``column_name`` and ``data_type`` keys.
            sample_data: Mapping of column‑name → list of sample values.

        Returns:
            A fully populated :class:`TableProfile`.
        """
        start_ts = time.time()
        column_profiles: list[ColumnProfile] = []

        for col_meta in column_metadata:
            col_name: str = col_meta.get("column_name", "")
            data_type: str = col_meta.get("data_type", "VARCHAR")
            values = sample_data.get(col_name, [])

            try:
                cp = self._profile_column(col_name, table_name, values, data_type)
                column_profiles.append(cp)
            except Exception as exc:
                self._logger.warning(
                    "column_profiling_failed",
                    column_name=col_name,
                    table_name=table_name,
                    error=str(exc),
                    error_type=type(exc).__name__,
                )
                # Emit a minimal column profile on failure so that the
                # table profile is still usable.
                column_profiles.append(
                    ColumnProfile(
                        column_name=col_name,
                        table_name=table_name,
                        data_category=DataCategory.TEXT,
                        total_count=len(values),
                    )
                )

        # Compute correlations between numeric columns.
        correlations = self._compute_correlations(
            column_profiles, sample_data, table_name,
        )

        duration = time.time() - start_ts

        estimated_rows = max(len(v) for v in sample_data.values()) if sample_data else 0

        table_profile = TableProfile(
            table_name=table_name,
            columns=column_profiles,
            correlations=correlations,
            estimated_row_count=estimated_rows,
            profiling_duration_seconds=round(duration, 3),
            sample_size=min(estimated_rows, self._sample_size),
        )

        self._logger.info(
            "table_profiling_complete",
            table_name=table_name,
            column_count=len(column_profiles),
            correlation_count=len(correlations),
            duration_seconds=round(duration, 3),
        )
        return table_profile

    # ---------------------------------------------------------------
    # Public API — profile_schema
    # ---------------------------------------------------------------

    def profile_schema(
        self,
        schema_id: str,
        tenant_id: str,
        table_profiles: list[TableProfile],
        include_cross_table: bool = True,
    ) -> StatisticalProfile:
        """Assemble a schema‑wide :class:`StatisticalProfile` from tables.

        Optionally computes cross‑table correlations for copula‑based
        multivariate correlation preservation.

        The profile starts in :attr:`ProfileStatus.IN_PROGRESS` while
        cross‑table correlations are being computed and transitions to
        :attr:`ProfileStatus.COMPLETED` on success or
        :attr:`ProfileStatus.FAILED` on error.

        Args:
            schema_id: The schema definition ID.
            tenant_id: Tenant identifier for multi‑tenant isolation.
            table_profiles: List of :class:`TableProfile` instances.
            include_cross_table: Whether to compute cross‑table correlations.

        Returns:
            A fully assembled :class:`StatisticalProfile`.
        """
        start_ts = time.time()

        self._logger.info(
            "schema_profiling_started",
            schema_id=schema_id,
            tenant_id=tenant_id,
            table_count=len(table_profiles),
            status=ProfileStatus.IN_PROGRESS,
        )

        status = ProfileStatus.IN_PROGRESS
        cross_table_corrs: list[CorrelationEntry] = []

        try:
            if include_cross_table and len(table_profiles) > 1:
                cross_table_corrs = self._compute_cross_table_correlations(table_profiles)

            total_cols = sum(len(tp.columns) for tp in table_profiles)

            # Compute overall completeness — ratio of columns that have
            # a non‑None distribution or frequency (i.e. were successfully
            # profiled beyond basic counts).
            profiled_cols = sum(
                1
                for tp in table_profiles
                for cp in tp.columns
                if cp.distribution is not None or cp.frequency is not None
            )
            overall_completeness = profiled_cols / total_cols if total_cols > 0 else 0.0

            duration = round(time.time() - start_ts, 3)
            status = ProfileStatus.COMPLETED

            profile = StatisticalProfile(
                tenant_id=tenant_id,
                schema_id=schema_id,
                status=status,
                tables=table_profiles,
                cross_table_correlations=cross_table_corrs,
                total_tables_profiled=len(table_profiles),
                total_columns_profiled=total_cols,
                overall_completeness=round(overall_completeness, 6),
                profiling_config={
                    "sample_size": self._sample_size,
                    "max_categories": self._max_categories,
                    "distribution_bins": self._distribution_bins,
                    "timeout_seconds": self._timeout_seconds,
                },
                profiling_metadata={
                    "duration_seconds": duration,
                },
                created_at=datetime.now(timezone.utc),
                updated_at=datetime.now(timezone.utc),
            )

            self._logger.info(
                "schema_profiling_complete",
                schema_id=schema_id,
                tenant_id=tenant_id,
                table_count=len(table_profiles),
                column_count=total_cols,
                overall_completeness=round(overall_completeness, 6),
                duration_seconds=duration,
            )
            return profile

        except Exception as exc:
            duration = round(time.time() - start_ts, 3)
            self._logger.error(
                "schema_profiling_failed",
                schema_id=schema_id,
                tenant_id=tenant_id,
                error=str(exc),
                error_type=type(exc).__name__,
                duration_seconds=duration,
            )
            # Return a profile with FAILED status so callers can inspect
            # the error state rather than catching an unhandled exception.
            total_cols = sum(len(tp.columns) for tp in table_profiles)
            return StatisticalProfile(
                tenant_id=tenant_id,
                schema_id=schema_id,
                status=ProfileStatus.FAILED,
                tables=table_profiles,
                cross_table_correlations=cross_table_corrs,
                total_tables_profiled=len(table_profiles),
                total_columns_profiled=total_cols,
                overall_completeness=0.0,
                profiling_config={
                    "sample_size": self._sample_size,
                    "max_categories": self._max_categories,
                    "distribution_bins": self._distribution_bins,
                    "timeout_seconds": self._timeout_seconds,
                },
                profiling_metadata={
                    "duration_seconds": duration,
                    "error": str(exc),
                    "error_type": type(exc).__name__,
                },
                created_at=datetime.now(timezone.utc),
                updated_at=datetime.now(timezone.utc),
            )

    # ---------------------------------------------------------------
    # Core private — _profile_column
    # ---------------------------------------------------------------

    def _profile_column(
        self,
        column_name: str,
        table_name: str,
        values: list,
        data_type: str,
    ) -> ColumnProfile:
        """Profile a single column comprehensively.

        Args:
            column_name: Column name.
            table_name: Parent table.
            values: Raw sample values (may contain ``None``).
            data_type: Source data‑type string.

        Returns:
            A populated :class:`ColumnProfile`.
        """
        total_count = len(values)
        non_null = [v for v in values if v is not None and not _is_null(v)]
        null_count = total_count - len(non_null)
        null_rate = null_count / total_count if total_count > 0 else 0.0
        non_null_count = len(non_null)

        # Distinct / cardinality
        try:
            distinct_values = set(str(v) for v in non_null)
        except TypeError:
            distinct_values = set()
        distinct_count = len(distinct_values)
        cardinality = distinct_count / non_null_count if non_null_count > 0 else 0.0
        is_unique = distinct_count == non_null_count and non_null_count > 0

        data_category = self._classify_data_category(data_type, non_null)

        # Default profile fields
        distribution: DistributionParameters | None = None
        value_range: ValueRange | None = None
        percentiles: Percentiles | None = None
        frequency: FrequencyDistribution | None = None
        pattern: PatternMetadata | None = None
        outlier_count: int = 0
        outlier_boundaries: dict[str, float] | None = None

        if non_null_count > 0:
            if data_category == DataCategory.NUMERIC:
                arr = _to_numeric_array(non_null)
                if arr is not None and len(arr) > 0:
                    value_range = self._compute_value_range(arr)
                    percentiles = self._compute_percentiles(arr)
                    distribution = self._fit_distribution(arr)
                    frequency = self._compute_frequency_histogram(arr)
                    outlier_count, outlier_boundaries = self._detect_outliers(arr)

            elif data_category == DataCategory.CATEGORICAL:
                frequency = self._compute_categorical_frequency(non_null)
                cat_params: dict[str, Any] = {
                    "categories": {str(k): v for k, v in (frequency.categories or {}).items()},
                }
                # Chi‑square goodness‑of‑fit test against a uniform
                # distribution to measure how evenly distributed categories
                # are.  A high p‑value indicates near‑uniform spread.
                cat_gof = 1.0
                chi_sq_stat: float | None = None
                cat_counts = list((frequency.categories or {}).values())
                if len(cat_counts) >= 2:
                    try:
                        chi2_val, chi2_pval = scipy_stats.chisquare(cat_counts)
                        cat_gof = round(float(chi2_pval), 6)
                        chi_sq_stat = round(float(chi2_val), 6)
                    except Exception:
                        pass
                distribution = DistributionParameters(
                    distribution_type=DistributionType.CATEGORICAL,
                    parameters=cat_params,
                    goodness_of_fit=cat_gof,
                    chi_square_statistic=chi_sq_stat,
                )

            elif data_category == DataCategory.TEMPORAL:
                arr = _temporal_to_epoch(non_null)
                if arr is not None and len(arr) > 0:
                    value_range = self._compute_value_range(arr)
                    percentiles = self._compute_percentiles(arr)
                    distribution = self._fit_distribution(arr)
                    frequency = self._compute_frequency_histogram(arr)

            elif data_category == DataCategory.BOOLEAN:
                # Boolean columns are treated as categorical with two values.
                frequency = self._compute_categorical_frequency(non_null)
                distribution = DistributionParameters(
                    distribution_type=DistributionType.CATEGORICAL,
                    parameters={"categories": {str(k): v for k, v in (frequency.categories or {}).items()}},
                    goodness_of_fit=1.0,
                )

            elif data_category in (DataCategory.TEXT, DataCategory.IDENTIFIER):
                # For text columns generate frequency distributions and
                # compute pattern metadata (format patterns, character‑class
                # distributions, etc.) — anonymised per C‑001.
                if distinct_count <= self._max_categories:
                    frequency = self._compute_categorical_frequency(non_null)
                pattern = _compute_pattern_metadata(non_null)

        col_profile = ColumnProfile(
            column_name=column_name,
            table_name=table_name,
            data_category=data_category,
            total_count=total_count,
            null_count=null_count,
            null_rate=round(null_rate, 6),
            distinct_count=distinct_count,
            cardinality=round(cardinality, 6),
            is_unique=is_unique,
            distribution=distribution,
            pattern=pattern,
            value_range=value_range,
            percentiles=percentiles,
            frequency=frequency,
            outlier_count=outlier_count,
            outlier_boundaries=outlier_boundaries,
        )

        self._logger.debug(
            "column_profiled",
            column_name=column_name,
            table_name=table_name,
            data_category=data_category,
            total_count=total_count,
            null_count=null_count,
            distinct_count=distinct_count,
        )
        return col_profile

    # ---------------------------------------------------------------
    # Value range
    # ---------------------------------------------------------------

    def _compute_value_range(self, values: np.ndarray) -> ValueRange:
        """Compute descriptive statistics for a numeric array.

        Args:
            values: 1‑D numeric array (NaN‑free).

        Returns:
            :class:`ValueRange` with min/max/mean/median/mode/std/var/
            skewness/kurtosis.
        """
        min_val = float(np.min(values))
        max_val = float(np.max(values))
        mean_val = float(np.mean(values))
        median_val = float(np.median(values))
        std_val = float(np.std(values, ddof=1)) if len(values) > 1 else 0.0
        var_val = float(np.var(values, ddof=1)) if len(values) > 1 else 0.0

        # Mode — scipy_stats.mode returns ModeResult.
        try:
            mode_result = scipy_stats.mode(values, keepdims=False)
            mode_val: float | None = float(mode_result.mode)
        except Exception:
            mode_val = None

        skewness: float | None = None
        kurtosis: float | None = None
        if len(values) >= 3:
            try:
                skewness = float(scipy_stats.skew(values))
            except Exception:
                pass
            try:
                kurtosis = float(scipy_stats.kurtosis(values))
            except Exception:
                pass

        return ValueRange(
            min_value=min_val,
            max_value=max_val,
            mean=round(mean_val, 6),
            median=round(median_val, 6),
            mode=round(mode_val, 6) if mode_val is not None else None,
            standard_deviation=round(std_val, 6),
            variance=round(var_val, 6),
            skewness=round(skewness, 6) if skewness is not None else None,
            kurtosis=round(kurtosis, 6) if kurtosis is not None else None,
        )

    # ---------------------------------------------------------------
    # Percentiles
    # ---------------------------------------------------------------

    def _compute_percentiles(self, values: np.ndarray) -> Percentiles:
        """Compute p1 through p99 and IQR.

        Args:
            values: 1‑D numeric array.

        Returns:
            :class:`Percentiles`.
        """
        pcts = np.percentile(values, [1, 5, 10, 25, 50, 75, 90, 95, 99])
        p25 = float(pcts[3])
        p75 = float(pcts[5])
        return Percentiles(
            p1=round(float(pcts[0]), 6),
            p5=round(float(pcts[1]), 6),
            p10=round(float(pcts[2]), 6),
            p25=round(p25, 6),
            p50=round(float(pcts[4]), 6),
            p75=round(p75, 6),
            p90=round(float(pcts[5 + 1]), 6),
            p95=round(float(pcts[7]), 6),
            p99=round(float(pcts[8]), 6),
            iqr=round(p75 - p25, 6),
        )

    # ---------------------------------------------------------------
    # Normality pre‑tests
    # ---------------------------------------------------------------

    def _check_normality(self, values: np.ndarray) -> dict[str, Any]:
        """Run multiple normality tests on *values*.

        Applies the Shapiro‑Wilk, D'Agostino‑Pearson, and Anderson‑Darling
        tests and returns a summary dictionary.  These results can guide
        the distribution‑fitting pipeline — if all three agree that the
        data is normal, the fitter can short‑circuit to a normal fit.

        Args:
            values: 1‑D numeric array (NaN‑free).

        Returns:
            A dictionary with keys ``shapiro_stat``, ``shapiro_pvalue``,
            ``dagostino_stat``, ``dagostino_pvalue``, ``anderson_stat``,
            ``anderson_critical_values``, ``is_likely_normal``.
        """
        result: dict[str, Any] = {
            "shapiro_stat": None,
            "shapiro_pvalue": None,
            "dagostino_stat": None,
            "dagostino_pvalue": None,
            "anderson_stat": None,
            "anderson_critical_values": None,
            "is_likely_normal": False,
        }
        normal_votes = 0

        # Shapiro‑Wilk — works best with n ≤ 5000.
        try:
            sample = values[:5000] if len(values) > 5000 else values
            sw_stat, sw_pval = scipy_stats.shapiro(sample)
            result["shapiro_stat"] = round(float(sw_stat), 6)
            result["shapiro_pvalue"] = round(float(sw_pval), 6)
            if sw_pval > 0.05:
                normal_votes += 1
        except Exception:
            pass

        # D'Agostino‑Pearson (requires n ≥ 20).
        if len(values) >= 20:
            try:
                dp_stat, dp_pval = scipy_stats.normaltest(values)
                result["dagostino_stat"] = round(float(dp_stat), 6)
                result["dagostino_pvalue"] = round(float(dp_pval), 6)
                if dp_pval > 0.05:
                    normal_votes += 1
            except Exception:
                pass

        # Anderson‑Darling.
        try:
            ad_result = scipy_stats.anderson(
                values, dist="norm", method="interpolate",
            )
            result["anderson_stat"] = round(float(ad_result.statistic), 6)
            # SciPy ≥ 1.17 returns ``pvalue`` when ``method`` is given.
            if hasattr(ad_result, "pvalue") and ad_result.pvalue is not None:
                result["anderson_pvalue"] = round(float(ad_result.pvalue), 6)
                if ad_result.pvalue > 0.05:
                    normal_votes += 1
            elif hasattr(ad_result, "critical_values"):
                result["anderson_critical_values"] = [
                    round(float(cv), 6) for cv in ad_result.critical_values
                ]
                # Compare against 5% significance level (index 2).
                if len(ad_result.critical_values) > 2:
                    if ad_result.statistic < ad_result.critical_values[2]:
                        normal_votes += 1
        except Exception:
            pass

        result["is_likely_normal"] = normal_votes >= 2

        self._logger.debug(
            "normality_tests_complete",
            sample_size=len(values),
            normal_votes=normal_votes,
            is_likely_normal=result["is_likely_normal"],
        )
        return result

    # ---------------------------------------------------------------
    # Distribution fitting
    # ---------------------------------------------------------------

    def _fit_distribution(self, values: np.ndarray) -> DistributionParameters:
        """Fit candidate distributions and select the best via KS test.

        Iterates over :data:`DISTRIBUTION_CANDIDATES` (continuous families),
        fits each via ``distribution.fit()`` and evaluates with ``kstest``.
        Additionally tests the **Poisson** distribution for non‑negative
        integer‑valued data using a chi‑square goodness‑of‑fit test (since
        Poisson is discrete and the KS test is not directly applicable).

        Before fitting, a normality pre‑check is performed via
        :meth:`_check_normality` (Shapiro‑Wilk, D'Agostino‑Pearson,
        Anderson‑Darling).  If the data is strongly normal, the fitter
        can still confirm this through the standard KS pipeline, but the
        normality metadata is attached for downstream consumers.

        Args:
            values: 1‑D numeric array.

        Returns:
            :class:`DistributionParameters` for the best‑fitting distribution.
        """
        if len(values) < _MIN_SAMPLE_KS:
            return DistributionParameters(
                distribution_type=DistributionType.UNIFORM,
                parameters={"loc": float(np.min(values)), "scale": float(np.ptp(values))},
                goodness_of_fit=0.0,
                ks_statistic=1.0,
            )

        # Run normality pre‑tests for diagnostic metadata.
        normality_info = self._check_normality(values)

        best_type = DistributionType.NORMAL
        best_params: dict[str, Any] = {}
        best_pvalue = -1.0
        best_ks = 1.0

        # --- Continuous distribution candidates ---
        for dist_type, dist_obj in DISTRIBUTION_CANDIDATES:
            try:
                # Certain distributions require all‑positive data.
                if dist_type in (DistributionType.LOG_NORMAL, DistributionType.GAMMA,
                                 DistributionType.WEIBULL):
                    if np.any(values <= 0):
                        continue

                # Beta requires data in (0, 1).
                if dist_type == DistributionType.BETA:
                    if np.any(values <= 0) or np.any(values >= 1):
                        continue

                fitted_params = dist_obj.fit(values)
                ks_stat, p_value = scipy_stats.kstest(
                    values, dist_obj.cdf, args=fitted_params,
                )

                if p_value > best_pvalue:
                    best_type = dist_type
                    best_pvalue = p_value
                    best_ks = ks_stat
                    best_params = _params_to_dict(dist_type, dist_obj, fitted_params)

            except Exception as exc:
                self._logger.debug(
                    "distribution_fit_skipped",
                    distribution=str(dist_type),
                    error=str(exc),
                )

        # --- Poisson distribution (discrete — handled separately) ---
        poisson_pvalue = _fit_poisson(values)
        if poisson_pvalue is not None and poisson_pvalue > best_pvalue:
            lam = float(np.mean(values))
            best_type = DistributionType.POISSON
            best_pvalue = poisson_pvalue
            best_ks = 0.0  # KS not applicable; chi‑square used instead.
            best_params = {"lambda": round(lam, 6)}

        # Attach normality metadata to the parameters dict.
        best_params["_normality_info"] = normality_info

        return DistributionParameters(
            distribution_type=best_type,
            parameters=best_params,
            goodness_of_fit=round(max(best_pvalue, 0.0), 6),
            ks_statistic=round(best_ks, 6),
        )

    # ---------------------------------------------------------------
    # Frequency distributions
    # ---------------------------------------------------------------

    def _compute_frequency_histogram(
        self,
        values: np.ndarray,
        bins: Optional[int] = None,
    ) -> FrequencyDistribution:
        """Compute a histogram for a numeric column.

        Args:
            values: 1‑D numeric array.
            bins: Number of bins (defaults to ``distribution_bins`` config).

        Returns:
            :class:`FrequencyDistribution`.
        """
        n_bins = bins or self._distribution_bins
        counts_arr, bin_edges = np.histogram(values, bins=n_bins)

        # Top values
        top_values = _top_values_numeric(values, max_top=10)

        return FrequencyDistribution(
            bins=[round(float(b), 6) for b in bin_edges],
            counts=[int(c) for c in counts_arr],
            total_count=int(len(values)),
            top_values=top_values,
        )

    def _compute_categorical_frequency(
        self,
        values: list,
    ) -> FrequencyDistribution:
        """Compute category frequencies for a categorical column.

        Args:
            values: Non‑null sample values.

        Returns:
            :class:`FrequencyDistribution`.
        """
        series = pd.Series(values).astype(str)
        vc = series.value_counts()

        # Limit to max_categories.
        if len(vc) > self._max_categories:
            top_vc = vc.head(self._max_categories)
            other_count = int(vc.iloc[self._max_categories:].sum())
            categories = {str(k): int(v) for k, v in top_vc.items()}
            categories["__OTHER__"] = other_count
        else:
            categories = {str(k): int(v) for k, v in vc.items()}

        total = int(vc.sum())
        top_values: list[dict[str, Any]] = []
        for val, cnt in vc.head(10).items():
            top_values.append({
                "value": str(val),
                "count": int(cnt),
                "percentage": round(int(cnt) / total, 6) if total > 0 else 0.0,
            })

        return FrequencyDistribution(
            categories=categories,
            total_count=total,
            top_values=top_values,
        )

    # ---------------------------------------------------------------
    # Correlations
    # ---------------------------------------------------------------

    def _compute_correlations(
        self,
        column_profiles: list[ColumnProfile],
        sample_data: dict[str, list],
        table_name: str,
    ) -> list[CorrelationEntry]:
        """Compute pairwise correlations between numeric columns.

        For numeric–numeric pairs: Pearson and Spearman coefficients.
        For categorical–categorical pairs: Cramér's V and mutual info.

        Args:
            column_profiles: All column profiles for the table.
            sample_data: Column‑name → sample values mapping.
            table_name: Table name.

        Returns:
            List of :class:`CorrelationEntry`.
        """
        # Split columns into numeric vs. categorical buckets.
        numeric_cols: list[str] = [
            cp.column_name for cp in column_profiles
            if cp.data_category in (DataCategory.NUMERIC, DataCategory.TEMPORAL)
            and cp.column_name in sample_data
        ]
        categorical_cols: list[str] = [
            cp.column_name for cp in column_profiles
            if cp.data_category == DataCategory.CATEGORICAL
            and cp.column_name in sample_data
        ]

        entries: list[CorrelationEntry] = []

        # --- Numeric × numeric ---
        for i in range(len(numeric_cols)):
            for j in range(i + 1, len(numeric_cols)):
                col_a, col_b = numeric_cols[i], numeric_cols[j]
                arr_a = _to_numeric_array(sample_data[col_a])
                arr_b = _to_numeric_array(sample_data[col_b])
                if arr_a is None or arr_b is None:
                    continue
                min_len = min(len(arr_a), len(arr_b))
                if min_len < _MIN_SAMPLE_CORRELATION:
                    continue
                arr_a, arr_b = arr_a[:min_len], arr_b[:min_len]

                pearson: float | None = None
                spearman: float | None = None
                try:
                    # Primary: np.corrcoef for Pearson correlation matrix.
                    corr_matrix = np.corrcoef(arr_a, arr_b)
                    pearson = float(corr_matrix[0, 1])
                except Exception:
                    # Fallback: scipy_stats.pearsonr.
                    try:
                        pearson = float(scipy_stats.pearsonr(arr_a, arr_b).statistic)
                    except Exception:
                        pass
                try:
                    spearman = float(scipy_stats.spearmanr(arr_a, arr_b).statistic)
                except Exception:
                    pass

                if pearson is not None or spearman is not None:
                    entries.append(CorrelationEntry(
                        column_a=col_a,
                        column_b=col_b,
                        table_name=table_name,
                        pearson_coefficient=round(pearson, 6) if pearson is not None else None,
                        spearman_coefficient=round(spearman, 6) if spearman is not None else None,
                    ))

        # --- Categorical × categorical ---
        for i in range(len(categorical_cols)):
            for j in range(i + 1, len(categorical_cols)):
                col_a, col_b = categorical_cols[i], categorical_cols[j]
                vals_a = [str(v) for v in sample_data[col_a] if v is not None]
                vals_b = [str(v) for v in sample_data[col_b] if v is not None]
                min_len = min(len(vals_a), len(vals_b))
                if min_len < _MIN_SAMPLE_CORRELATION:
                    continue
                vals_a, vals_b = vals_a[:min_len], vals_b[:min_len]

                cramers = _cramers_v(vals_a, vals_b)
                mi = _mutual_information(vals_a, vals_b)

                if cramers is not None or mi is not None:
                    entries.append(CorrelationEntry(
                        column_a=col_a,
                        column_b=col_b,
                        table_name=table_name,
                        cramers_v=round(cramers, 6) if cramers is not None else None,
                        mutual_information=round(mi, 6) if mi is not None else None,
                    ))

        self._logger.debug(
            "correlations_computed",
            table_name=table_name,
            numeric_pairs=len(numeric_cols) * (len(numeric_cols) - 1) // 2,
            categorical_pairs=len(categorical_cols) * (len(categorical_cols) - 1) // 2,
            entries=len(entries),
        )
        return entries

    # ---------------------------------------------------------------
    # Cross‑table correlations
    # ---------------------------------------------------------------

    def _compute_cross_table_correlations(
        self,
        table_profiles: list[TableProfile],
    ) -> list[CorrelationEntry]:
        """Compute cross‑table correlation hints from shared column names.

        Full cross‑table correlations require joined data across tables,
        which is not available in a single profiling pass (C‑001: metadata
        only).  This method identifies columns that share identical names
        across different tables — a strong heuristic for foreign‑key
        relationships — and returns :class:`CorrelationEntry` records that
        downstream consumers can use for copula‑based correlation
        modelling.

        Args:
            table_profiles: All table profiles in the schema.

        Returns:
            List of :class:`CorrelationEntry` (may be empty).
        """
        entries: list[CorrelationEntry] = []

        # Identify columns with identical names across tables — a strong
        # hint of a foreign‑key relationship and potential correlation.
        col_table_map: dict[str, list[str]] = {}
        for tp in table_profiles:
            for cp in tp.columns:
                col_table_map.setdefault(cp.column_name, []).append(tp.table_name)

        for col_name, tables in col_table_map.items():
            if len(tables) < 2:
                continue
            for i in range(len(tables)):
                for j in range(i + 1, len(tables)):
                    entries.append(CorrelationEntry(
                        column_a=col_name,
                        column_b=col_name,
                        table_name=f"{tables[i]}↔{tables[j]}",
                        pearson_coefficient=None,
                        spearman_coefficient=None,
                    ))

        self._logger.debug(
            "cross_table_correlations_computed",
            entry_count=len(entries),
        )
        return entries

    # ---------------------------------------------------------------
    # Outlier detection
    # ---------------------------------------------------------------

    def _detect_outliers(
        self,
        values: np.ndarray,
    ) -> tuple[int, dict[str, float]]:
        """Detect outliers using the IQR method.

        Boundaries:

        * lower = Q1 − 1.5 × IQR
        * upper = Q3 + 1.5 × IQR

        Args:
            values: 1‑D numeric array.

        Returns:
            Tuple of (outlier_count, boundary_dict).
        """
        q1 = float(np.percentile(values, 25))
        q3 = float(np.percentile(values, 75))
        iqr = q3 - q1

        lower = q1 - 1.5 * iqr
        upper = q3 + 1.5 * iqr

        outlier_mask = (values < lower) | (values > upper)
        count = int(np.sum(outlier_mask))

        return count, {"lower": round(lower, 6), "upper": round(upper, 6)}

    # ---------------------------------------------------------------
    # Data category classification
    # ---------------------------------------------------------------

    def _classify_data_category(
        self,
        data_type: str,
        values: Optional[list] = None,
    ) -> DataCategory:
        """Map a data‑type string to a :class:`DataCategory`.

        Numeric types (INTEGER, FLOAT, DECIMAL, …) → ``NUMERIC``.
        Date / time types → ``TEMPORAL``.
        BOOLEAN / BIT → ``BOOLEAN``.
        Otherwise → ``TEXT`` or ``CATEGORICAL`` depending on cardinality.

        Args:
            data_type: Source SQL / ERP data‑type string.
            values: Optional non‑null sample for ambiguous types.

        Returns:
            A :class:`DataCategory` enum member.
        """
        upper = data_type.upper().strip()

        # Numeric
        numeric_types = {
            "INTEGER", "INT", "SMALLINT", "TINYINT", "BIGINT",
            "FLOAT", "DOUBLE", "REAL", "DECIMAL", "NUMERIC",
            "NUMBER", "MONEY", "INT4", "INT8", "SERIAL", "BIGSERIAL",
        }
        if upper in numeric_types:
            return DataCategory.NUMERIC

        # Temporal
        temporal_types = {"DATE", "TIME", "TIMESTAMP", "DATETIME", "DATETIME2"}
        if upper in temporal_types:
            return DataCategory.TEMPORAL

        # Boolean
        boolean_types = {"BOOLEAN", "BOOL", "BIT"}
        if upper in boolean_types:
            return DataCategory.BOOLEAN

        # Text / Categorical heuristic
        if values is not None and len(values) > 0:
            distinct_count = len(set(str(v) for v in values))
            total_count = len(values)
            if total_count > 0 and distinct_count / total_count < 0.05:
                return DataCategory.CATEGORICAL

        # Identifier heuristic — UUID or auto‑increment like columns
        identifier_keywords = {"UUID", "UNIQUEIDENTIFIER", "ROWID"}
        if upper in identifier_keywords:
            return DataCategory.IDENTIFIER

        return DataCategory.TEXT


# ===================================================================
# Private helper functions
# ===================================================================


def _is_null(value: Any) -> bool:
    """Return ``True`` if *value* represents a missing/null sentinel.

    Checks for ``None``, ``NaN`` (via :func:`numpy.isnan`), pandas ``NA``,
    and empty/whitespace-only strings.

    Args:
        value: Any value.

    Returns:
        ``True`` for ``None``, ``NaN``, empty string, and pandas NA.
    """
    if value is None:
        return True
    if isinstance(value, (float, np.floating)):
        try:
            if np.isnan(value):
                return True
        except (TypeError, ValueError):
            pass
    try:
        if pd.isna(value):
            return True
    except (TypeError, ValueError):
        pass
    if isinstance(value, str) and value.strip() == "":
        return True
    return False


def _to_numeric_array(values: list) -> Optional[np.ndarray]:
    """Convert a list of values to a 1‑D float64 numpy array.

    Uses :func:`pandas.to_numeric` for robust type coercion with
    ``errors="coerce"`` semantics, then filters to finite values via
    :func:`numpy.isfinite`.  Non‑numeric and null values are silently
    dropped.

    Args:
        values: Raw sample values.

    Returns:
        A ``numpy.ndarray`` of finite floats, or ``None`` if conversion
        yields an empty array.
    """
    try:
        series = pd.to_numeric(pd.Series(values), errors="coerce")
        arr = series.dropna().values.astype(np.float64)
        # Keep only finite values (no inf / -inf).
        finite_mask = np.isfinite(arr)
        arr = arr[finite_mask]
        if len(arr) == 0:
            return None
        return arr
    except Exception:
        # Fallback: manual per-element conversion.
        nums: list[float] = []
        for v in values:
            if v is None:
                continue
            try:
                f = float(v)
                if np.isfinite(f):
                    nums.append(f)
            except (TypeError, ValueError):
                continue
        if not nums:
            return None
        return np.array(nums, dtype=np.float64)


def _temporal_to_epoch(values: list) -> Optional[np.ndarray]:
    """Convert temporal values to UNIX epoch seconds.

    Tries ``pd.to_datetime`` for robust parsing.

    Args:
        values: Non‑null temporal sample values.

    Returns:
        A ``numpy.ndarray`` of epoch floats, or ``None``.
    """
    try:
        dt_series = pd.to_datetime(pd.Series(values), errors="coerce")
        epoch = dt_series.dropna().astype("int64") / 1e9
        arr = epoch.values.astype(np.float64)
        if len(arr) == 0:
            return None
        return arr
    except Exception:
        return None


def _params_to_dict(
    dist_type: DistributionType,
    dist_obj: Any,
    fitted_params: tuple,
) -> dict[str, Any]:
    """Convert scipy ``fit()`` parameters into a readable dictionary.

    Args:
        dist_type: Distribution type enum.
        dist_obj: SciPy distribution object.
        fitted_params: Tuple returned by ``dist_obj.fit()``.

    Returns:
        Dict keyed by meaningful parameter names.
    """
    names = (dist_obj.shapes or "").split(", ") if dist_obj.shapes else []
    names = [n.strip() for n in names if n.strip()]

    result: dict[str, Any] = {}
    shape_count = len(fitted_params) - 2  # last two are loc, scale
    for i in range(shape_count):
        key = names[i] if i < len(names) else f"shape_{i}"
        result[key] = round(float(fitted_params[i]), 6)
    result["loc"] = round(float(fitted_params[-2]), 6)
    result["scale"] = round(float(fitted_params[-1]), 6)

    return result


def _top_values_numeric(
    values: np.ndarray,
    max_top: int = 10,
) -> list[dict[str, Any]]:
    """Return top‑N most frequent numeric values.

    Args:
        values: 1‑D numeric array.
        max_top: Maximum number of entries.

    Returns:
        List of ``{"value": ..., "count": ..., "percentage": ...}`` dicts.
    """
    total = len(values)
    if total == 0:
        return []

    unique, counts = np.unique(values, return_counts=True)
    order = np.argsort(-counts)[:max_top]

    result: list[dict[str, Any]] = []
    for idx in order:
        result.append({
            "value": round(float(unique[idx]), 6),
            "count": int(counts[idx]),
            "percentage": round(int(counts[idx]) / total, 6),
        })
    return result


def _fit_poisson(values: np.ndarray) -> Optional[float]:
    """Attempt to fit a Poisson distribution to *values*.

    Poisson is appropriate when values are non‑negative integers.  The
    method estimates λ = mean(values) and performs a chi‑square
    goodness‑of‑fit test via :func:`scipy.stats.chisquare` against
    expected Poisson frequencies.

    Args:
        values: 1‑D numeric array.

    Returns:
        The chi‑square test *p*‑value if the Poisson fit is feasible,
        or ``None`` if the data is unsuitable for Poisson fitting.
    """
    # Poisson requires non‑negative integer data.
    if np.any(values < 0):
        return None
    rounded = np.round(values)
    if not np.allclose(values, rounded, atol=0.01):
        return None

    int_vals = rounded.astype(int)
    lam = float(np.mean(int_vals))
    if lam <= 0:
        return None

    try:
        max_val = int(np.max(int_vals))
        # Limit bins to a reasonable range.
        max_bin = min(max_val + 1, 50)
        observed = np.bincount(int_vals, minlength=max_bin)[:max_bin]

        from scipy.stats import poisson as _poisson_dist

        expected_raw = np.array([
            _poisson_dist.pmf(k, lam) * len(int_vals)
            for k in range(max_bin)
        ])

        # Normalize expected frequencies so their sum matches the observed
        # total exactly.  SciPy ≥ 1.17 enforces strict agreement between
        # observed and expected sums in ``chisquare``.
        obs_total = float(np.sum(observed))
        exp_total = float(np.sum(expected_raw))
        if exp_total <= 0:
            return None
        expected = expected_raw * (obs_total / exp_total)

        # Merge bins with expected counts < 5 (chi‑square requirement).
        obs_merged: list[float] = []
        exp_merged: list[float] = []
        obs_acc = 0.0
        exp_acc = 0.0
        for o, e in zip(observed, expected):
            obs_acc += o
            exp_acc += e
            if exp_acc >= 5:
                obs_merged.append(obs_acc)
                exp_merged.append(exp_acc)
                obs_acc = 0.0
                exp_acc = 0.0
        if exp_acc > 0:
            if exp_merged:
                obs_merged[-1] += obs_acc
                exp_merged[-1] += exp_acc
            else:
                obs_merged.append(obs_acc)
                exp_merged.append(exp_acc)

        if len(obs_merged) < 2:
            return None

        chi2_stat, p_value = scipy_stats.chisquare(obs_merged, f_exp=exp_merged)
        return float(p_value)
    except Exception:
        return None


def _compute_pattern_metadata(values: list, max_samples: int = 5) -> PatternMetadata:
    """Derive string pattern metadata from a list of text values.

    Computes average/min/max string lengths, common prefixes and suffixes,
    character‑class distributions (alphabetic, digit, special), and a
    simplified format pattern.  ``sample_formats`` contains anonymised
    format exemplars (e.g. ``"AAAA-9999"``), never raw data (C‑001).

    Args:
        values: Non‑null string values.
        max_samples: Maximum number of format samples to include.

    Returns:
        A populated :class:`PatternMetadata`.
    """
    str_values = [str(v) for v in values if v is not None]
    if not str_values:
        return PatternMetadata()

    lengths = [len(s) for s in str_values]
    avg_len = sum(lengths) / len(lengths) if lengths else 0.0
    min_len = min(lengths) if lengths else 0
    max_len = max(lengths) if lengths else 0

    # Character‑class distribution across all characters.
    total_chars = sum(lengths)
    alpha_count = sum(c.isalpha() for s in str_values for c in s)
    digit_count = sum(c.isdigit() for s in str_values for c in s)
    special_count = total_chars - alpha_count - digit_count
    char_classes: dict[str, float] = {}
    if total_chars > 0:
        char_classes = {
            "alpha": round(alpha_count / total_chars, 6),
            "digit": round(digit_count / total_chars, 6),
            "special": round(special_count / total_chars, 6),
        }

    # Common prefixes (first 3 characters).
    prefix_counter: dict[str, int] = {}
    suffix_counter: dict[str, int] = {}
    for s in str_values:
        if len(s) >= 3:
            prefix_counter[s[:3]] = prefix_counter.get(s[:3], 0) + 1
            suffix_counter[s[-3:]] = suffix_counter.get(s[-3:], 0) + 1
    threshold = max(1, len(str_values) // 10)
    common_prefixes = sorted(
        [p for p, c in prefix_counter.items() if c >= threshold],
        key=lambda p: prefix_counter[p],
        reverse=True,
    )[:5]
    common_suffixes = sorted(
        [s for s, c in suffix_counter.items() if c >= threshold],
        key=lambda s: suffix_counter[s],
        reverse=True,
    )[:5]

    # Build anonymised format pattern from a representative sample.
    sample_formats: list[str] = []
    seen_patterns: set[str] = set()
    for s in str_values[:200]:
        pattern = ""
        for ch in s:
            if ch.isalpha():
                pattern += "A"
            elif ch.isdigit():
                pattern += "9"
            else:
                pattern += ch
        if pattern not in seen_patterns:
            seen_patterns.add(pattern)
            sample_formats.append(pattern)
            if len(sample_formats) >= max_samples:
                break

    # Attempt a simple regex pattern from the most common format.
    regex_pattern: str | None = None
    format_pattern: str | None = None
    if sample_formats:
        fmt = sample_formats[0]
        format_pattern = fmt
        regex_chars: list[str] = []
        i = 0
        while i < len(fmt):
            if fmt[i] == "A":
                run = 0
                while i < len(fmt) and fmt[i] == "A":
                    run += 1
                    i += 1
                regex_chars.append(f"[A-Za-z]{{{run}}}")
            elif fmt[i] == "9":
                run = 0
                while i < len(fmt) and fmt[i] == "9":
                    run += 1
                    i += 1
                regex_chars.append(f"\\d{{{run}}}")
            else:
                import re as _re_mod

                regex_chars.append(_re_mod.escape(fmt[i]))
                i += 1
        regex_pattern = "^" + "".join(regex_chars) + "$"

    return PatternMetadata(
        format_pattern=format_pattern,
        regex_pattern=regex_pattern,
        common_prefixes=common_prefixes,
        common_suffixes=common_suffixes,
        average_length=round(avg_len, 2),
        min_length=min_len,
        max_length=max_len,
        sample_formats=sample_formats,
        character_classes=char_classes,
    )


def _cramers_v(col_a: list[str], col_b: list[str]) -> Optional[float]:
    """Compute Cramér's V for two categorical columns.

    Args:
        col_a: First column values (stringified).
        col_b: Second column values (stringified).

    Returns:
        Cramér's V ∈ [0, 1], or ``None`` on error.
    """
    try:
        ct = pd.crosstab(pd.Series(col_a), pd.Series(col_b))
        chi2 = scipy_stats.chi2_contingency(ct)[0]
        n = ct.sum().sum()
        r, c = ct.shape
        denom = n * (min(r, c) - 1)
        if denom <= 0:
            return None
        return float(math.sqrt(chi2 / denom))
    except Exception:
        return None


def _mutual_information(col_a: list[str], col_b: list[str]) -> Optional[float]:
    """Compute mutual information between two categorical columns.

    Uses the empirical probability tables derived from the crosstab.

    Args:
        col_a: First column values.
        col_b: Second column values.

    Returns:
        Mutual information in nats (≥ 0), or ``None`` on error.
    """
    try:
        ct = pd.crosstab(pd.Series(col_a), pd.Series(col_b))
        n = ct.sum().sum()
        if n == 0:
            return None

        mi = 0.0
        row_sums = ct.sum(axis=1)
        col_sums = ct.sum(axis=0)

        for r_idx in ct.index:
            for c_idx in ct.columns:
                p_xy = ct.loc[r_idx, c_idx] / n
                p_x = row_sums[r_idx] / n
                p_y = col_sums[c_idx] / n
                if p_xy > 0 and p_x > 0 and p_y > 0:
                    mi += p_xy * math.log(p_xy / (p_x * p_y))

        return max(mi, 0.0)
    except Exception:
        return None
