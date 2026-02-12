"""Quality Service package for the Synthetic ERP Data Generation Platform.

Provides data quality validation integrating Great Expectations with a
weighted scoring model:

- **40%** Statistical fidelity  (distribution, moments, ranges)
- **30%** Business rules compliance  (format, cross-field, domain, temporal)
- **30%** Referential integrity  (FK validity, orphan detection, cascade chains)

The composite quality score targets ≥ 95% fidelity for production-grade
synthetic data.

Usage::

    from quality_service import create_app

    app = create_app("development")
    app.run()

Subpackages:

- ``quality_service.validators`` — Pluggable validation strategies
- ``quality_service.scoring`` — Composite scoring and report generation
"""

from __future__ import annotations

__version__: str = "1.0.0"
"""Semantic version of the Quality Service package."""

# ---------------------------------------------------------------------------
# Convenience imports
# ---------------------------------------------------------------------------
# Keep imports minimal to avoid circular dependencies.  Detailed validator
# and scorer imports should happen at point of use, not here.

try:
    from quality_service.app import create_app  # noqa: F401
except ImportError:
    # app.py may not yet exist during incremental build or testing.
    create_app = None  # type: ignore[assignment,misc]

__all__: list[str] = [
    "create_app",
    "__version__",
]
