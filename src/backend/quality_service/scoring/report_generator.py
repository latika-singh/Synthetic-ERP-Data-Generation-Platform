"""Quality report generation module for the Synthetic ERP Data Generation Platform.

Produces detailed, structured quality reports with per-metric drill-down from
validation results.  Accepts :class:`~quality_service.validators.base.ValidationResult`
objects from all three validators (statistical fidelity, business rules,
referential integrity) and the composite :class:`QualityScorer` output, then
generates comprehensive reports containing:

- Per-column statistical comparisons
- Per-rule compliance breakdowns
- Per-relationship integrity details
- Overall pass/fail determination
- Metadata about the generation job

Reports are persisted to MongoDB and retrievable via the API for
Screen S-007 (Quality Reports with drill-down).

Usage::

    from quality_service.scoring.report_generator import ReportGenerator

    rg = ReportGenerator()
    report = rg.generate_report(
        job_id="abc-123",
        tenant_id="tenant-1",
        validation_results=results,
        composite_score=0.96,
    )
    rg.save_report(report)
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any

from pydantic import BaseModel, Field

from shared.database.mongodb import get_mongo_db
from shared.logging.structured_logger import get_logger


if TYPE_CHECKING:
    from quality_service.validators.base import ValidationResult


# ---------------------------------------------------------------------------
# Module-level logger
# ---------------------------------------------------------------------------
logger = get_logger(__name__)


# ---------------------------------------------------------------------------
# Pydantic models — report data structures
# ---------------------------------------------------------------------------


class QualityReportSection(BaseModel):
    """Individual section of a quality report corresponding to a single validator.

    Each section maps 1:1 to a :class:`ValidationResult` but is enriched with
    report-specific formatting and the ``weighted_score`` pre-computation.

    Attributes:
        section_name: Name of the validation section (e.g.
            ``'statistical_fidelity'``, ``'business_rules'``,
            ``'referential_integrity'``).
        score: Section score normalised to [0.0, 1.0].
        weight: Section weight in the composite quality score.
        weighted_score: Pre-computed ``score * weight``.
        passed: Whether this section meets the minimum threshold.
        details: Per-column or per-rule detailed breakdown.
        errors: Validation errors found during this check.
        warnings: Non-critical warnings.
        records_validated: Number of records examined.
        records_passed: Number of records passing all checks.
        execution_time_ms: Time taken for this validation section (ms).
    """

    model_config = {"arbitrary_types_allowed": True}

    section_name: str = Field(
        ...,
        description="Name of the validation section.",
    )
    score: float = Field(
        ...,
        ge=0.0,
        le=1.0,
        description="Section score normalised to [0.0, 1.0].",
    )
    weight: float = Field(
        ...,
        ge=0.0,
        le=1.0,
        description="Section weight in composite score.",
    )
    weighted_score: float = Field(
        default=0.0,
        description="Computed as score * weight.",
    )
    passed: bool = Field(
        default=False,
        description="Whether this section meets the threshold.",
    )
    details: dict[str, Any] = Field(
        default_factory=dict,
        description="Per-column or per-rule metric breakdown.",
    )
    errors: list[str] = Field(
        default_factory=list,
        description="Validation errors found.",
    )
    warnings: list[str] = Field(
        default_factory=list,
        description="Non-critical warnings.",
    )
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
        description="Time taken for this section in milliseconds.",
    )


class QualityReport(BaseModel):
    """Complete quality report for a generation job.

    Encapsulates the full quality assessment across all validators with
    per-section breakdowns, overall scoring, actionable recommendations,
    and metadata linking the report back to its source job and profile.

    Attributes:
        report_id: Unique UUID4 identifier for this report.
        job_id: Associated generation job ID.
        tenant_id: Tenant namespace for multi-tenant isolation.
        created_at: Report creation timestamp (UTC, ISO 8601).
        status: ``'passed'`` or ``'failed'``.
        overall_score: Composite quality score in [0.0, 1.0].
        threshold: Minimum acceptable composite score (default 0.95).
        sections: Individual validator result sections.
        summary: High-level summary statistics (total records, pass rate,
            strongest / weakest section, etc.).
        metadata: Job configuration, schema info, generation method, and
            record counts.
        recommendations: Suggested improvements when score is below threshold.
        data_profile: Reference to the statistical profile used for validation.
        schema_info: Schema definition referenced during validation.
    """

    model_config = {"arbitrary_types_allowed": True}

    report_id: str = Field(
        ...,
        description="Unique report identifier (UUID4).",
    )
    job_id: str = Field(
        ...,
        description="Associated generation job ID.",
    )
    tenant_id: str = Field(
        ...,
        description="Tenant namespace for multi-tenant isolation.",
    )
    created_at: datetime = Field(
        default_factory=lambda: datetime.now(UTC),
        description="Report creation timestamp (UTC, ISO 8601).",
    )
    status: str = Field(
        default="failed",
        description="'passed' or 'failed'.",
    )
    overall_score: float = Field(
        ...,
        ge=0.0,
        le=1.0,
        description="Composite quality score in [0.0, 1.0].",
    )
    threshold: float = Field(
        default=0.95,
        ge=0.0,
        le=1.0,
        description="Minimum acceptable composite score.",
    )
    sections: list[QualityReportSection] = Field(
        default_factory=list,
        description="Individual validator result sections.",
    )
    summary: dict[str, Any] = Field(
        default_factory=dict,
        description="High-level summary statistics.",
    )
    metadata: dict[str, Any] = Field(
        default_factory=dict,
        description="Job config, schema info, generation method.",
    )
    recommendations: list[str] = Field(
        default_factory=list,
        description="Suggested improvements if score below threshold.",
    )
    data_profile: dict[str, Any] = Field(
        default_factory=dict,
        description="Reference to the statistical profile used.",
    )
    schema_info: dict[str, Any] = Field(
        default_factory=dict,
        description="Schema definition referenced during validation.",
    )


# ---------------------------------------------------------------------------
# ReportGenerator — quality report generation and persistence
# ---------------------------------------------------------------------------


class ReportGenerator:
    """Generates, stores, and retrieves quality reports for the Quality Service.

    Converts raw :class:`ValidationResult` objects into richly-structured
    :class:`QualityReport` instances with per-section drill-down, aggregated
    summary statistics, and actionable recommendations.  Reports are persisted
    to MongoDB for retrieval via the REST API (Screen S-007).

    Args:
        config: Optional configuration dictionary.  Recognised keys:

            - ``report_collection`` (str): Name of the MongoDB collection
              for storing reports.  Defaults to ``'quality_reports'``.

    Attributes:
        config: The (possibly empty) configuration dictionary.
        logger: Structured logger for audit-compliant event logging.
    """

    # Default MongoDB collection name
    _DEFAULT_COLLECTION = "quality_reports"

    def __init__(self, config: dict[str, Any] | None = None) -> None:
        self.config: dict[str, Any] = config if config is not None else {}
        self.logger = get_logger(self.__class__.__name__)
        self._collection_name: str = self.config.get(
            "report_collection", self._DEFAULT_COLLECTION
        )

        # Lazy-initialise MongoDB handle to avoid hard failures when the
        # database is unavailable (e.g. during unit-testing).
        self._db = None
        try:
            self._db = get_mongo_db()
            self.logger.info(
                "report_generator_initialized",
                collection=self._collection_name,
            )
        except Exception as exc:
            self.logger.warning(
                "report_generator_mongo_unavailable",
                error=str(exc),
            )

    # ------------------------------------------------------------------
    # Private helpers
    # ------------------------------------------------------------------

    def _get_collection(self) -> Any:
        """Return the MongoDB collection handle, raising if unavailable.

        Attempts lazy reconnection when the initial ``__init__`` connection
        failed (e.g. MongoDB was temporarily unreachable).

        Returns:
            A ``pymongo.collection.Collection`` for ``quality_reports``.

        Raises:
            RuntimeError: If MongoDB remains unavailable.
        """
        if self._db is None:
            try:
                self._db = get_mongo_db()
            except Exception as exc:
                raise RuntimeError(
                    "MongoDB is not available for report persistence."
                ) from exc
        return self._db[self._collection_name]

    # ------------------------------------------------------------------
    # Core methods
    # ------------------------------------------------------------------

    def generate_report(
        self,
        job_id: str,
        tenant_id: str,
        validation_results: list[ValidationResult],
        composite_score: float,
        threshold: float = 0.95,
        metadata: dict[str, Any] | None = None,
    ) -> QualityReport:
        """Create a complete quality report from validation results.

        Converts each :class:`ValidationResult` into a
        :class:`QualityReportSection`, computes summary statistics, generates
        recommendations when the composite score is below the threshold, and
        returns a fully populated :class:`QualityReport`.

        Args:
            job_id: The generation job identifier.
            tenant_id: Tenant namespace for multi-tenant isolation.
            validation_results: List of ``ValidationResult`` objects, one per
                validator executed.
            composite_score: Pre-computed weighted composite quality score.
            threshold: Minimum acceptable quality score (default 0.95).
            metadata: Optional dictionary of additional context (generation
                method, record counts, schema version, etc.).

        Returns:
            A fully populated :class:`QualityReport` instance.
        """
        report_id = str(uuid.uuid4())

        # Convert each ValidationResult to a report section.
        sections = [
            self._convert_validation_result_to_section(result)
            for result in validation_results
        ]

        # Determine overall status.
        status = "passed" if composite_score >= threshold else "failed"

        # Generate recommendations when quality is below target.
        recommendations = self._generate_recommendations(
            sections, composite_score, threshold
        )

        # Generate high-level summary statistics.
        summary = self._generate_summary(sections, composite_score)

        # Extract data_profile and schema_info from metadata when provided
        # so they populate the dedicated report fields for API drill-down.
        safe_metadata = metadata if metadata is not None else {}
        data_profile = safe_metadata.get("data_profile", {})
        schema_info = safe_metadata.get("schema_info", {})

        report = QualityReport(
            report_id=report_id,
            job_id=job_id,
            tenant_id=tenant_id,
            created_at=datetime.now(UTC),
            status=status,
            overall_score=composite_score,
            threshold=threshold,
            sections=sections,
            summary=summary,
            metadata=safe_metadata,
            recommendations=recommendations,
            data_profile=data_profile,
            schema_info=schema_info,
        )

        self.logger.info(
            "quality_report_generated",
            report_id=report_id,
            job_id=job_id,
            tenant_id=tenant_id,
            overall_score=round(composite_score, 4),
            status=status,
            sections_count=len(sections),
        )

        return report

    def save_report(self, report: QualityReport) -> str:
        """Persist a :class:`QualityReport` to MongoDB.

        Args:
            report: The quality report to persist.

        Returns:
            The ``report_id`` of the persisted report.
        """
        collection = self._get_collection()
        doc = report.model_dump()

        # Ensure datetime is serialisable in MongoDB.
        if isinstance(doc.get("created_at"), datetime):
            doc["created_at"] = doc["created_at"].isoformat()

        # Serialise nested Pydantic models (sections) to plain dicts.
        doc["sections"] = [
            s.model_dump() if hasattr(s, "model_dump") else s
            for s in report.sections
        ]

        collection.insert_one(doc)

        self.logger.info(
            "quality_report_saved",
            report_id=report.report_id,
            job_id=report.job_id,
            tenant_id=report.tenant_id,
        )
        return report.report_id

    def get_report(
        self, report_id: str, tenant_id: str
    ) -> QualityReport | None:
        """Retrieve a stored quality report by ID with tenant isolation.

        Args:
            report_id: The unique report identifier.
            tenant_id: Tenant namespace for access control.

        Returns:
            The :class:`QualityReport` if found, ``None`` otherwise.
        """
        collection = self._get_collection()
        doc = collection.find_one(
            {"report_id": report_id, "tenant_id": tenant_id}
        )
        if doc is None:
            return None

        doc.pop("_id", None)
        return self._reconstruct_report(doc)

    def get_reports_by_job(
        self, job_id: str, tenant_id: str
    ) -> list[QualityReport]:
        """Retrieve all quality reports for a specific generation job.

        Args:
            job_id: The generation job identifier.
            tenant_id: Tenant namespace for access control.

        Returns:
            List of :class:`QualityReport` instances sorted by ``created_at``
            descending (most recent first).
        """
        collection = self._get_collection()
        cursor = collection.find(
            {"job_id": job_id, "tenant_id": tenant_id}
        ).sort("created_at", -1)

        reports: list[QualityReport] = []
        for doc in cursor:
            doc.pop("_id", None)
            try:
                reports.append(self._reconstruct_report(doc))
            except Exception as exc:
                self.logger.warning(
                    "report_reconstruction_failed",
                    report_id=doc.get("report_id", "unknown"),
                    error=str(exc),
                )
        return reports

    def delete_report(self, report_id: str, tenant_id: str) -> bool:
        """Delete a quality report (admin operation) with tenant isolation.

        Args:
            report_id: The unique report identifier.
            tenant_id: Tenant namespace for access control.

        Returns:
            ``True`` if a report was deleted, ``False`` if not found.
        """
        collection = self._get_collection()
        result = collection.delete_one(
            {"report_id": report_id, "tenant_id": tenant_id}
        )
        deleted: bool = bool(result.deleted_count > 0)

        self.logger.info(
            "quality_report_deleted" if deleted else "quality_report_delete_not_found",
            report_id=report_id,
            tenant_id=tenant_id,
        )
        return deleted

    # ------------------------------------------------------------------
    # Private helpers — report section conversion
    # ------------------------------------------------------------------

    def _convert_validation_result_to_section(
        self, result: ValidationResult
    ) -> QualityReportSection:
        """Transform a :class:`ValidationResult` into a :class:`QualityReportSection`.

        Performs a direct field mapping so that report consumers receive a
        uniform structure regardless of which validator produced the result.
        The validator's ``threshold`` is included in the section ``details``
        under the ``validator_threshold`` key so that drill-down consumers can
        see the pass/fail criteria applied by each validator.

        Args:
            result: The validator output to convert.

        Returns:
            A :class:`QualityReportSection` containing the mapped data.
        """
        # Merge the validator's threshold into section details for drill-down
        # transparency — consumers viewing a section can see the pass/fail
        # criteria that was applied during validation.
        section_details = dict(result.details) if result.details else {}
        section_details["validator_threshold"] = result.threshold

        return QualityReportSection(
            section_name=result.validator_name,
            score=result.score,
            weight=result.weight,
            weighted_score=result.weighted_score,
            passed=result.passed,
            details=section_details,
            errors=list(result.errors) if result.errors else [],
            warnings=list(result.warnings) if result.warnings else [],
            records_validated=result.records_validated,
            records_passed=result.records_passed,
            execution_time_ms=result.execution_time_ms,
        )

    # ------------------------------------------------------------------
    # Private helpers — recommendation engine
    # ------------------------------------------------------------------

    def _generate_recommendations(
        self,
        sections: list[QualityReportSection],
        composite_score: float,
        threshold: float,
    ) -> list[str]:
        """Analyse failing sections and generate actionable recommendations.

        Recommendations are sorted by severity: the lowest-scoring section
        produces the first (most important) recommendation.

        Args:
            sections: List of report sections from all validators.
            composite_score: The composite quality score.
            threshold: The minimum acceptable quality score.

        Returns:
            List of human-readable recommendation strings.
        """
        if composite_score >= threshold:
            return ["Quality score meets the minimum threshold. No action required."]

        recommendations: list[str] = []

        # Sort sections by score ascending so the weakest section is first.
        sorted_sections = sorted(sections, key=lambda s: s.score)

        for section in sorted_sections:
            if section.passed:
                continue

            name = section.section_name.lower()
            score_pct = round(section.score * 100, 1)

            if "statistical" in name:
                recommendations.extend([
                    f"Statistical fidelity score is {score_pct}%. "
                    "Consider adjusting generation parameters or increasing "
                    "training epochs for AI/ML models.",
                    "Review distribution fitting for columns with low KS-test "
                    "p-values and increase sample diversity.",
                ])
            elif "business" in name:
                recommendations.extend([
                    f"Business rules compliance score is {score_pct}%. "
                    "Review rule configurations for the target ERP module.",
                    "Verify format patterns, cross-field dependencies, and "
                    "domain value sets match the source schema profile.",
                ])
            elif "referential" in name or "integrity" in name:
                recommendations.extend([
                    f"Referential integrity score is {score_pct}%. "
                    "Check foreign key relationship mappings and generation order.",
                    "Ensure parent records are generated before child records "
                    "and that FK value pools are correctly populated.",
                ])
            else:
                recommendations.append(
                    f"Section '{section.section_name}' scored {score_pct}%. "
                    "Review validation details for specific failures."
                )

        if not recommendations:
            recommendations.append(
                f"Composite score ({round(composite_score * 100, 1)}%) is below "
                f"the threshold ({round(threshold * 100, 1)}%). Review all "
                "section details for improvement opportunities."
            )

        return recommendations

    # ------------------------------------------------------------------
    # Private helpers — summary generation
    # ------------------------------------------------------------------

    def _generate_summary(
        self,
        sections: list[QualityReportSection],
        composite_score: float,
    ) -> dict[str, Any]:
        """Compute high-level summary statistics from report sections.

        Args:
            sections: List of all report sections.
            composite_score: The pre-computed composite quality score.

        Returns:
            Dictionary of summary metrics:

            - ``total_records_validated``: Sum of all section ``records_validated``.
            - ``total_records_passed``: Sum of all section ``records_passed``.
            - ``overall_pass_rate``: ``total_records_passed / total_records_validated``.
            - ``weakest_section``: Section name with the lowest score.
            - ``strongest_section``: Section name with the highest score.
            - ``total_execution_time_ms``: Sum of all section execution times.
            - ``sections_passed``: Count of sections that passed.
            - ``sections_total``: Total number of sections.
            - ``composite_score``: The overall composite quality score.
        """
        if not sections:
            return {
                "total_records_validated": 0,
                "total_records_passed": 0,
                "overall_pass_rate": 0.0,
                "weakest_section": None,
                "strongest_section": None,
                "total_execution_time_ms": 0.0,
                "sections_passed": 0,
                "sections_total": 0,
                "composite_score": composite_score,
            }

        total_validated = sum(s.records_validated for s in sections)
        total_passed = sum(s.records_passed for s in sections)
        overall_pass_rate = (
            total_passed / total_validated if total_validated > 0 else 0.0
        )

        sorted_by_score = sorted(sections, key=lambda s: s.score)
        weakest = sorted_by_score[0]
        strongest = sorted_by_score[-1]

        return {
            "total_records_validated": total_validated,
            "total_records_passed": total_passed,
            "overall_pass_rate": round(overall_pass_rate, 4),
            "weakest_section": weakest.section_name,
            "weakest_section_score": round(weakest.score, 4),
            "strongest_section": strongest.section_name,
            "strongest_section_score": round(strongest.score, 4),
            "total_execution_time_ms": round(
                sum(s.execution_time_ms for s in sections), 3
            ),
            "sections_passed": sum(1 for s in sections if s.passed),
            "sections_total": len(sections),
            "composite_score": round(composite_score, 4),
        }

    # ------------------------------------------------------------------
    # Private helpers — reconstruction from MongoDB documents
    # ------------------------------------------------------------------

    def _reconstruct_report(self, doc: dict[str, Any]) -> QualityReport:
        """Reconstruct a :class:`QualityReport` from a MongoDB document.

        Handles type coercions (e.g. ISO-string → datetime, nested dicts →
        ``QualityReportSection`` models) so that the caller receives a
        properly-typed Pydantic model.

        Args:
            doc: Raw MongoDB document (``_id`` should already be removed).

        Returns:
            A :class:`QualityReport` instance.
        """
        # Convert sections from raw dicts to Pydantic models.
        raw_sections = doc.get("sections", [])
        sections = []
        for raw in raw_sections:
            if isinstance(raw, dict):
                sections.append(QualityReportSection.model_validate(raw))
            elif isinstance(raw, QualityReportSection):
                sections.append(raw)
        doc["sections"] = sections

        # Convert ISO string back to datetime if needed.
        created_at = doc.get("created_at")
        if isinstance(created_at, str):
            try:
                doc["created_at"] = datetime.fromisoformat(created_at)
            except (ValueError, TypeError):
                doc["created_at"] = datetime.now(UTC)

        return QualityReport.model_validate(doc)


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

__all__: list[str] = [
    "QualityReport",
    "QualityReportSection",
    "ReportGenerator",
]
