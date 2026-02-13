"""Scoring package for the Quality Service weighted composite quality model.

Provides the orchestration layer for the Quality Service's weighted composite
quality scoring model:

    Q = 0.4 * S_statistical + 0.3 * S_business_rules + 0.3 * S_referential_integrity

The target threshold is ≥ 95 % quality fidelity.

Key classes:

- :class:`QualityScorer` — Central scorer that runs all validators and
  computes the composite quality score.
- :class:`QualityScoringResult` — Pydantic model containing the composite
  score, pass/fail status, and per-validator breakdowns.
- :class:`ReportGenerator` — Generates detailed quality reports with
  per-metric drill-down for Screen S-007.
- :class:`QualityReport` — Pydantic model for complete quality reports.
- :class:`QualityReportSection` — Pydantic model for individual report
  sections.

Usage::

    from quality_service.scoring import QualityScorer

    scorer = QualityScorer()
    result = scorer.score(job_id="abc", tenant_id="t1", generated_data=df, profile={})
"""

from __future__ import annotations

from quality_service.scoring.quality_scorer import (
    QualityScorer,
    QualityScoringResult,
)
from quality_service.scoring.report_generator import (
    QualityReport,
    QualityReportSection,
    ReportGenerator,
)


__all__: list[str] = [
    "QualityReport",
    "QualityReportSection",
    "QualityScorer",
    "QualityScoringResult",
    "ReportGenerator",
]
