"""Statistical profiling package for the Profiling Service.

Contains :class:`StatisticalProfiler` for distribution analysis using
SciPy 1.12+ and NumPy 1.26+, and :class:`PatternAnalyzer` for data
format and pattern detection.

These profilers capture statistical metadata about ERP data columns to
enable high-fidelity synthetic data generation.  Output is stored in the
``statistical_profiles`` MongoDB collection.

**Constraint C-001:** No production data access — profilers operate on
metadata and statistical samples only.

**Constraint C-005:** Supports four ERP modules — Financial Accounting,
Human Resources, Sales & Distribution, Material Management.

Usage::

    from profiling_service.profilers import get_profiler

    profiler = get_profiler("statistical")
    table_profile = profiler.profile_table(table_name, columns, sample)

    analyzer = get_profiler("pattern")
    pattern = analyzer.analyze_column_patterns("col", values, "VARCHAR")
"""

from __future__ import annotations

from typing import Optional, Union

from profiling_service.profilers.pattern_analyzer import PatternAnalyzer
from profiling_service.profilers.statistical_profiler import StatisticalProfiler

# ---------------------------------------------------------------------------
# Profiler Registry (factory pattern)
# ---------------------------------------------------------------------------

PROFILER_REGISTRY: dict[str, type] = {
    "statistical": StatisticalProfiler,
    "pattern": PatternAnalyzer,
}


# ---------------------------------------------------------------------------
# Factory functions
# ---------------------------------------------------------------------------


def get_profiler(
    profiler_type: str,
    config: Optional[dict] = None,
) -> Union[StatisticalProfiler, PatternAnalyzer]:
    """Create and return a profiler instance for the given type.

    Looks up *profiler_type* in :data:`PROFILER_REGISTRY` and instantiates
    the corresponding class with *config*.

    Args:
        profiler_type: Profiler identifier — ``"statistical"`` or
            ``"pattern"``.
        config: Optional configuration override dict.

    Returns:
        An instance of :class:`StatisticalProfiler` or
        :class:`PatternAnalyzer`.

    Raises:
        ValueError: If *profiler_type* is not recognised.

    Example::

        profiler = get_profiler("statistical", {"sample_size": 5000})
    """
    profiler_cls = PROFILER_REGISTRY.get(profiler_type)
    if profiler_cls is None:
        supported = ", ".join(sorted(PROFILER_REGISTRY.keys()))
        raise ValueError(
            f"Unsupported profiler type '{profiler_type}'. "
            f"Supported types: {supported}"
        )
    return profiler_cls(config=config)


def get_available_profilers() -> list[str]:
    """Return a sorted list of available profiler type identifiers.

    Returns:
        Sorted list of strings, each a valid ``profiler_type`` value
        for :func:`get_profiler`.
    """
    return sorted(PROFILER_REGISTRY.keys())


# ---------------------------------------------------------------------------
# Package exports
# ---------------------------------------------------------------------------

__all__ = [
    "StatisticalProfiler",
    "PatternAnalyzer",
    "PROFILER_REGISTRY",
    "get_profiler",
    "get_available_profilers",
]
