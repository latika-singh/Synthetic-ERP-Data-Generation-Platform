"""Dual-layer PII detection orchestrator for the Compliance Service.

This module is the **primary entry point** for PII scanning in the
compliance verification workflow.  It combines two complementary
detection strategies into a unified pipeline:

1.  **NLP-based entity recognition** — via :class:`NLPDetector` (spaCy)
    for unstructured PII such as person names, addresses, and
    organisation names.
2.  **Regex-based pattern matching** — via :class:`PatternDetector` for
    structured PII formats including SSN, email, phone, credit card,
    and IBAN numbers.

The orchestrator runs both detection layers against every text field in
the input data, aggregates findings with confidence scores, deduplicates
overlapping detections (keeping the highest-confidence match), and
produces a comprehensive :class:`PIIDetectionResult` with per-field PII
findings.  Batch processing support enables efficient scanning of large
datasets while reporting progress.

Without this module the Compliance Service cannot perform PII detection
and datasets cannot be certified as PII-free — a prerequisite for the
``Pending → Scanning → PIICheck → Certified → Released`` compliance
state machine.

Design decisions:
    * **No hardcoded PII patterns** — All detection logic is delegated to
      the specialised ``NLPDetector`` and ``PatternDetector`` classes.  This
      module solely orchestrates, merges, and filters their outputs.
    * **Confidence-gated findings** — A configurable threshold (default
      ``0.85``) filters low-confidence matches to minimise false positives
      while maintaining zero-PII-leakage guarantees.
    * **Redacted matched values** — Matched text is masked (first 2
      characters + ``***``) before inclusion in findings so that audit
      records never contain raw PII.
    * **Overlap deduplication** — When both layers detect the same PII
      span, findings are merged into a single entry with
      ``detection_method='both'`` and the maximum confidence score.

Usage::

    # Single-record scan
    from compliance_service.detectors.pii_detector import PIIDetector

    detector = PIIDetector()
    result = detector.detect_pii({"name": "John Smith", "ssn": "123-45-6789"})
    print(result.has_pii)          # True
    print(result.total_findings)   # ≥ 2
    print(result.pii_types_found)  # {'PERSON_NAME', 'SSN', ...}

    # Batch scan
    records = [{"email": "user@example.com"}, {"note": "No PII here"}]
    result = detector.detect_batch(records, batch_size=500)

    # Module-level convenience function
    from compliance_service.detectors.pii_detector import detect_pii

    result = detect_pii({"email": "test@example.com"})

See Also:
    ``compliance_service.detectors.nlp_detector`` — NLP detection layer.
    ``compliance_service.detectors.pattern_detector`` — Regex detection layer.
    ``compliance_service.certification.certifier`` — Compliance state machine.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import Any, Optional

from pydantic import BaseModel, Field

from compliance_service.detectors.nlp_detector import NLPDetector, NLPDetectionResult
from compliance_service.detectors.pattern_detector import (
    PatternDetector,
    PatternDetectionResult,
)
from shared.logging.structured_logger import get_logger


# ---------------------------------------------------------------------------
# Pydantic Data Models
# ---------------------------------------------------------------------------


class PIIFinding(BaseModel):
    """A single PII detection finding from the dual-layer detection pipeline.

    Each finding represents one occurrence of PII detected in a specific
    data field, including which detection method(s) identified it, the
    confidence score, and a redacted representation of the matched value
    for audit purposes.

    Attributes:
        field_name: The column or field name where PII was found.
        pii_type: Type of PII detected (e.g. ``'SSN'``, ``'EMAIL'``,
            ``'PERSON_NAME'``, ``'PHONE'``, ``'CREDIT_CARD'``,
            ``'IBAN'``, ``'ADDRESS'``, ``'ORGANIZATION'``).
        detection_method: Which layer(s) detected the PII.  One of
            ``'nlp'``, ``'pattern'``, or ``'both'`` when both layers
            independently identified the same PII span.
        confidence: Confidence score in the range ``[0.0, 1.0]``.  When
            ``detection_method='both'``, this is the maximum of the
            individual layer confidences.
        matched_value: Redacted representation of the detected text
            (e.g. ``'Jo***h'``).  Raw PII is **never** stored.
        start_pos: Zero-based character start position in the source
            text.  ``None`` when position information is unavailable.
        end_pos: Zero-based character end position (exclusive) in the
            source text.  ``None`` when position information is
            unavailable.
        metadata: Additional context about the detection, such as the
            regex pattern name or spaCy entity label.  Defaults to an
            empty dict.

    Example::

        finding = PIIFinding(
            field_name="employee_name",
            pii_type="PERSON_NAME",
            detection_method="nlp",
            confidence=0.95,
            matched_value="Jo***h",
            start_pos=0,
            end_pos=10,
            metadata={"spacy_label": "PERSON"},
        )
    """

    field_name: str = Field(
        ...,
        description="The column/field where PII was found",
    )
    pii_type: str = Field(
        ...,
        description="Type of PII detected (SSN, EMAIL, PERSON_NAME, PHONE, etc.)",
    )
    detection_method: str = Field(
        ...,
        description="Detection layer: 'nlp', 'pattern', or 'both'",
    )
    confidence: float = Field(
        ...,
        ge=0.0,
        le=1.0,
        description="Confidence score [0.0, 1.0]",
    )
    matched_value: str = Field(
        ...,
        description="Redacted/masked detected value (first 2 chars + '***')",
    )
    start_pos: int | None = Field(
        default=None,
        description="Character start position in source text",
    )
    end_pos: int | None = Field(
        default=None,
        description="Character end position in source text",
    )
    metadata: dict[str, Any] = Field(
        default_factory=dict,
        description="Additional context (regex pattern name, spaCy entity label)",
    )


class PIIDetectionResult(BaseModel):
    """Aggregate result of a dual-layer PII detection scan.

    Contains the complete set of PII findings, summary statistics, and
    performance metrics produced by scanning one or more data records
    through the NLP and pattern detection pipeline.

    Attributes:
        has_pii: ``True`` if at least one PII finding was detected
            above the confidence threshold.
        total_findings: Total number of PII findings after confidence
            filtering and deduplication.
        findings: Ordered list of all :class:`PIIFinding` objects.
        pii_types_found: Set of distinct PII type strings found across
            all findings (e.g. ``{'SSN', 'PERSON_NAME'}``).
        scan_duration_ms: Total wall-clock scan duration in
            milliseconds, measured via :func:`time.perf_counter`.
        records_scanned: Number of data records (rows) scanned.
        confidence_threshold: The threshold that was applied to filter
            low-confidence findings.
        nlp_findings_count: Count of findings originating from (or
            including) the NLP detection layer.
        pattern_findings_count: Count of findings originating from (or
            including) the pattern detection layer.

    Example::

        result = PIIDetectionResult(
            has_pii=True,
            total_findings=3,
            findings=[...],
            pii_types_found={"SSN", "EMAIL", "PERSON_NAME"},
            scan_duration_ms=45.6,
            records_scanned=100,
            confidence_threshold=0.85,
            nlp_findings_count=1,
            pattern_findings_count=2,
        )
    """

    has_pii: bool = Field(
        default=False,
        description="Whether any PII was detected",
    )
    total_findings: int = Field(
        default=0,
        ge=0,
        description="Total number of PII findings",
    )
    findings: list[PIIFinding] = Field(
        default_factory=list,
        description="List of all PII findings",
    )
    pii_types_found: set[str] = Field(
        default_factory=set,
        description="Set of distinct PII types found",
    )
    scan_duration_ms: float = Field(
        default=0.0,
        ge=0.0,
        description="Duration of the scan in milliseconds",
    )
    records_scanned: int = Field(
        default=0,
        ge=0,
        description="Number of records/fields scanned",
    )
    confidence_threshold: float = Field(
        default=0.85,
        ge=0.0,
        le=1.0,
        description="The threshold used for filtering",
    )
    nlp_findings_count: int = Field(
        default=0,
        ge=0,
        description="Findings from NLP layer",
    )
    pattern_findings_count: int = Field(
        default=0,
        ge=0,
        description="Findings from pattern layer",
    )


# ---------------------------------------------------------------------------
# Internal lightweight data structures (no Pydantic validation overhead)
# ---------------------------------------------------------------------------


@dataclass
class _MergeCandidate:
    """Internal state tracker for overlap deduplication during merge.

    Used inside :meth:`PIIDetector._merge_findings` to track individual
    detection results before deduplication.  Using a plain dataclass
    avoids the Pydantic validation overhead that would be incurred on
    every intermediate merge step.

    Attributes:
        pii_type: Detected PII type category.
        detection_method: Source layer (``'nlp'`` or ``'pattern'``).
        confidence: Detection confidence score.
        matched_value: Redacted matched text.
        start_pos: Character start offset (may be ``None``).
        end_pos: Character end offset (may be ``None``).
        metadata: Additional detection context.
    """

    pii_type: str = ""
    detection_method: str = ""
    confidence: float = 0.0
    matched_value: str = ""
    start_pos: int | None = None
    end_pos: int | None = None
    metadata: dict[str, Any] = field(default_factory=dict)


# ---------------------------------------------------------------------------
# PII Detector Orchestrator
# ---------------------------------------------------------------------------


class PIIDetector:
    """Dual-layer PII detection orchestrator.

    Combines NLP-based entity recognition (:class:`NLPDetector`) and
    regex-based pattern matching (:class:`PatternDetector`) into a
    unified detection pipeline.  This is the primary entry point for PII
    scanning throughout the Compliance Service verification workflow.

    The detection process for each text field:

    1. Run the NLP detector to identify unstructured PII (names,
       addresses, organisations).
    2. Run the pattern detector to identify structured PII (SSN, email,
       phone, credit card, IBAN).
    3. Merge and deduplicate overlapping findings — when both layers
       detect the same text span, findings are consolidated with
       ``detection_method='both'`` and the maximum confidence score.
    4. Apply the confidence threshold to filter low-confidence findings.
    5. Aggregate results into a :class:`PIIDetectionResult`.

    Args:
        nlp_detector: Pre-configured NLP detector instance.  When
            ``None``, a default :class:`NLPDetector` is lazily
            initialised with the ``en_core_web_sm`` spaCy model.
        pattern_detector: Pre-configured pattern detector instance.
            When ``None``, a default :class:`PatternDetector` is lazily
            initialised with the built-in pattern registry.
        confidence_threshold: Minimum confidence score for findings to
            be included in results.  Defaults to ``0.85``.

    Example::

        # Default configuration
        detector = PIIDetector()
        result = detector.detect_pii({"name": "John Smith"})

        # Custom detectors and threshold
        detector = PIIDetector(
            nlp_detector=NLPDetector(model_name="en_core_web_lg"),
            pattern_detector=PatternDetector(),
            confidence_threshold=0.90,
        )
    """

    def __init__(
        self,
        nlp_detector: NLPDetector | None = None,
        pattern_detector: PatternDetector | None = None,
        confidence_threshold: float = 0.85,
    ) -> None:
        """Initialize the PII detector orchestrator.

        Stores or lazily initialises the NLP and pattern detectors,
        configures the confidence threshold, and sets up structured
        logging.

        Args:
            nlp_detector: Optional pre-configured NLP detector.  When
                ``None``, a default instance is created on first use.
            pattern_detector: Optional pre-configured pattern detector.
                When ``None``, a default instance is created on first
                use.
            confidence_threshold: Minimum confidence for findings to be
                included in results.  Must be in ``[0.0, 1.0]``.

        Raises:
            ValueError: If *confidence_threshold* is outside ``[0.0, 1.0]``.
        """
        if not 0.0 <= confidence_threshold <= 1.0:
            raise ValueError(
                f"confidence_threshold must be in [0.0, 1.0], got {confidence_threshold}"
            )

        self._logger = get_logger(__name__)
        self._confidence_threshold: float = confidence_threshold

        # Lazy-initialise detectors: store provided instances or defer
        # creation until first detection call to avoid loading the spaCy
        # model at import time (expensive in cold-start scenarios).
        self._nlp_detector: NLPDetector | None = nlp_detector
        self._pattern_detector: PatternDetector | None = pattern_detector
        self._nlp_initialized: bool = nlp_detector is not None
        self._pattern_initialized: bool = pattern_detector is not None

        self._logger.info(
            "pii_detector_initialized",
            confidence_threshold=confidence_threshold,
            nlp_detector_provided=nlp_detector is not None,
            pattern_detector_provided=pattern_detector is not None,
        )

    # ------------------------------------------------------------------
    # Lazy initialisation helpers
    # ------------------------------------------------------------------

    def _get_nlp_detector(self) -> NLPDetector:
        """Return the NLP detector, lazily creating it if needed.

        Returns:
            A ready-to-use :class:`NLPDetector` instance.
        """
        if self._nlp_detector is None:
            self._logger.info("lazy_init_nlp_detector", model="en_core_web_sm")
            self._nlp_detector = NLPDetector()
            self._nlp_initialized = True
        return self._nlp_detector

    def _get_pattern_detector(self) -> PatternDetector:
        """Return the pattern detector, lazily creating it if needed.

        Returns:
            A ready-to-use :class:`PatternDetector` instance.
        """
        if self._pattern_detector is None:
            self._logger.info("lazy_init_pattern_detector")
            self._pattern_detector = PatternDetector()
            self._pattern_initialized = True
        return self._pattern_detector

    # ------------------------------------------------------------------
    # Public Detection API
    # ------------------------------------------------------------------

    def detect_pii(
        self,
        data: dict[str, Any] | list[dict[str, Any]],
        field_names: list[str] | None = None,
    ) -> PIIDetectionResult:
        """Run the dual-layer PII detection pipeline against input data.

        This is the main entry point for PII detection.  It iterates over
        every record and every field (or a caller-specified subset),
        runs both NLP and pattern detection on each text value, merges
        and deduplicates the findings, applies the confidence threshold,
        and aggregates results into a single :class:`PIIDetectionResult`.

        Args:
            data: Input data to scan.  May be a single record
                (``dict[str, Any]``) or a list of records
                (``list[dict[str, Any]]``).  Non-string field values are
                converted to strings before scanning; ``None`` values are
                skipped.
            field_names: Optional list of field names to scan.  When
                ``None``, all fields in each record are scanned.

        Returns:
            A :class:`PIIDetectionResult` containing all findings,
            aggregate statistics, and performance metrics.

        Example::

            detector = PIIDetector()

            # Single record
            result = detector.detect_pii({"ssn": "123-45-6789", "note": "safe text"})
            assert result.has_pii is True

            # Multiple records
            result = detector.detect_pii([
                {"email": "user@example.com"},
                {"comment": "No PII here"},
            ])

            # Selective field scanning
            result = detector.detect_pii(
                {"name": "John Smith", "id": "12345"},
                field_names=["name"],
            )
        """
        start_time: float = time.perf_counter()

        # Normalise input: wrap single dict in a list for uniform processing.
        records: list[dict[str, Any]]
        if isinstance(data, dict):
            records = [data]
        else:
            records = list(data)

        # Obtain detector instances (lazy-init if needed).
        nlp: NLPDetector = self._get_nlp_detector()
        pattern: PatternDetector = self._get_pattern_detector()

        all_findings: list[PIIFinding] = []
        nlp_count: int = 0
        pattern_count: int = 0
        fields_scanned: int = 0

        for record in records:
            # Determine which fields to scan for this record.
            target_fields: list[str]
            if field_names is not None:
                target_fields = [f for f in field_names if f in record]
            else:
                target_fields = list(record.keys())

            for field_name in target_fields:
                value = record.get(field_name)
                if value is None:
                    continue

                # Convert non-string values for text-based scanning.
                text: str = str(value) if not isinstance(value, str) else value
                if not text.strip():
                    continue

                fields_scanned += 1

                # --- Layer 1: NLP detection ---
                nlp_result: NLPDetectionResult = nlp.detect(text)

                # --- Layer 2: Pattern detection ---
                pattern_result: PatternDetectionResult = pattern.detect(text)

                # --- Merge and deduplicate ---
                merged: list[PIIFinding] = self._merge_findings(
                    nlp_result,
                    pattern_result,
                    field_name,
                )

                # --- Confidence filtering ---
                filtered: list[PIIFinding] = self._filter_by_confidence(merged)

                # Tally layer-specific counts before adding to global list.
                for finding in filtered:
                    if finding.detection_method in ("nlp", "both"):
                        nlp_count += 1
                    if finding.detection_method in ("pattern", "both"):
                        pattern_count += 1

                all_findings.extend(filtered)

        elapsed_ms: float = (time.perf_counter() - start_time) * 1000.0

        pii_types: set[str] = {f.pii_type for f in all_findings}

        result = PIIDetectionResult(
            has_pii=len(all_findings) > 0,
            total_findings=len(all_findings),
            findings=all_findings,
            pii_types_found=pii_types,
            scan_duration_ms=round(elapsed_ms, 2),
            records_scanned=len(records),
            confidence_threshold=self._confidence_threshold,
            nlp_findings_count=nlp_count,
            pattern_findings_count=pattern_count,
        )

        self._logger.info(
            "pii_scan_complete",
            has_pii=result.has_pii,
            total_findings=result.total_findings,
            pii_types_found=sorted(pii_types),
            records_scanned=result.records_scanned,
            fields_scanned=fields_scanned,
            scan_duration_ms=result.scan_duration_ms,
            nlp_findings_count=nlp_count,
            pattern_findings_count=pattern_count,
            confidence_threshold=self._confidence_threshold,
        )

        return result

    def detect_batch(
        self,
        records: list[dict[str, Any]],
        batch_size: int = 1000,
        field_names: list[str] | None = None,
    ) -> PIIDetectionResult:
        """Process records in batches for efficient large-dataset scanning.

        Splits the input records into batches of *batch_size*, runs
        :meth:`detect_pii` on each batch, aggregates findings across
        all batches, and returns a combined :class:`PIIDetectionResult`.
        Progress is reported via structured logging after each batch.

        Args:
            records: List of data records (dicts) to scan.
            batch_size: Maximum number of records per batch.  Defaults
                to ``1000``.  Must be at least ``1``.
            field_names: Optional list of field names to scan.  Passed
                through to :meth:`detect_pii`.

        Returns:
            A combined :class:`PIIDetectionResult` aggregating findings
            from all batches.

        Example::

            detector = PIIDetector()
            large_dataset = [{"email": f"user{i}@test.com"} for i in range(5000)]
            result = detector.detect_batch(large_dataset, batch_size=500)
            print(f"Scanned {result.records_scanned} records, "
                  f"found {result.total_findings} PII findings")
        """
        start_time: float = time.perf_counter()

        if batch_size < 1:
            batch_size = 1

        total_records: int = len(records)
        total_batches: int = (total_records + batch_size - 1) // batch_size if total_records > 0 else 0

        all_findings: list[PIIFinding] = []
        total_nlp_count: int = 0
        total_pattern_count: int = 0

        self._logger.info(
            "batch_scan_started",
            total_records=total_records,
            batch_size=batch_size,
            total_batches=total_batches,
        )

        for batch_idx in range(total_batches):
            batch_start: int = batch_idx * batch_size
            batch_end: int = min(batch_start + batch_size, total_records)
            batch_records: list[dict[str, Any]] = records[batch_start:batch_end]

            batch_result: PIIDetectionResult = self.detect_pii(
                batch_records,
                field_names=field_names,
            )

            all_findings.extend(batch_result.findings)
            total_nlp_count += batch_result.nlp_findings_count
            total_pattern_count += batch_result.pattern_findings_count

            self._logger.info(
                "batch_progress",
                batch_index=batch_idx + 1,
                total_batches=total_batches,
                batch_records=len(batch_records),
                batch_findings=batch_result.total_findings,
                cumulative_findings=len(all_findings),
                progress_pct=round(((batch_idx + 1) / total_batches) * 100.0, 1)
                if total_batches > 0
                else 100.0,
            )

        elapsed_ms: float = (time.perf_counter() - start_time) * 1000.0
        pii_types: set[str] = {f.pii_type for f in all_findings}

        result = PIIDetectionResult(
            has_pii=len(all_findings) > 0,
            total_findings=len(all_findings),
            findings=all_findings,
            pii_types_found=pii_types,
            scan_duration_ms=round(elapsed_ms, 2),
            records_scanned=total_records,
            confidence_threshold=self._confidence_threshold,
            nlp_findings_count=total_nlp_count,
            pattern_findings_count=total_pattern_count,
        )

        self._logger.info(
            "batch_scan_complete",
            has_pii=result.has_pii,
            total_findings=result.total_findings,
            total_records=total_records,
            total_batches=total_batches,
            pii_types_found=sorted(pii_types),
            scan_duration_ms=result.scan_duration_ms,
            nlp_findings_count=total_nlp_count,
            pattern_findings_count=total_pattern_count,
        )

        return result

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _merge_findings(
        self,
        nlp_result: NLPDetectionResult,
        pattern_result: PatternDetectionResult,
        field_name: str,
    ) -> list[PIIFinding]:
        """Merge and deduplicate findings from both detection layers.

        Combines NLP entities and pattern matches for a single field.
        When both layers detect PII at the same (overlapping) text span,
        the findings are consolidated into a single :class:`PIIFinding`
        with ``detection_method='both'`` and the maximum confidence of
        the two layers.

        Two findings are considered overlapping when their character
        ranges intersect — specifically, when ``start_a < end_b`` and
        ``start_b < end_a``.

        Args:
            nlp_result: Detection result from the NLP layer for this
                field.
            pattern_result: Detection result from the pattern layer for
                this field.
            field_name: The name of the data field being scanned.

        Returns:
            A list of merged :class:`PIIFinding` objects for this field.
        """
        # Step 1: Convert NLP entities to merge candidates.
        nlp_candidates: list[_MergeCandidate] = []
        for entity in nlp_result.entities:
            nlp_candidates.append(
                _MergeCandidate(
                    pii_type=entity.pii_type,
                    detection_method="nlp",
                    confidence=entity.confidence,
                    matched_value=self._mask_value(entity.text),
                    start_pos=entity.start_char,
                    end_pos=entity.end_char,
                    metadata={
                        "spacy_label": entity.label,
                        "detection_layer": "nlp",
                    },
                )
            )

        # Step 2: Convert pattern matches to merge candidates.
        pattern_candidates: list[_MergeCandidate] = []
        for match in pattern_result.matches:
            pattern_candidates.append(
                _MergeCandidate(
                    pii_type=match.pii_type,
                    detection_method="pattern",
                    confidence=match.confidence,
                    # Pattern detector already redacts matched_text.
                    matched_value=match.matched_text,
                    start_pos=match.start_pos,
                    end_pos=match.end_pos,
                    metadata={
                        "pattern_name": match.pattern_name,
                        "validation_passed": match.validation_passed,
                        "detection_layer": "pattern",
                    },
                )
            )

        # Step 3: Deduplicate overlapping detections.
        # Track which pattern candidates were merged into an NLP candidate.
        merged_pattern_indices: set[int] = set()

        for nlp_cand in nlp_candidates:
            for p_idx, pat_cand in enumerate(pattern_candidates):
                if p_idx in merged_pattern_indices:
                    continue

                if self._spans_overlap(
                    nlp_cand.start_pos,
                    nlp_cand.end_pos,
                    pat_cand.start_pos,
                    pat_cand.end_pos,
                ):
                    # Merge: promote to 'both', keep max confidence.
                    nlp_cand.detection_method = "both"
                    nlp_cand.confidence = max(nlp_cand.confidence, pat_cand.confidence)
                    # Combine metadata from both layers.
                    nlp_cand.metadata.update(pat_cand.metadata)
                    nlp_cand.metadata["detection_layer"] = "both"
                    # Prefer the more specific PII type (pattern types
                    # like 'SSN' are more specific than NLP types like
                    # 'PERSON_NAME').
                    if pat_cand.pii_type not in ("", "UNKNOWN"):
                        nlp_cand.pii_type = pat_cand.pii_type
                    merged_pattern_indices.add(p_idx)

                    self._logger.debug(
                        "merge_duplicate_finding",
                        field_name=field_name,
                        pii_type=nlp_cand.pii_type,
                        nlp_confidence=nlp_cand.confidence,
                        pattern_confidence=pat_cand.confidence,
                        merged_confidence=nlp_cand.confidence,
                    )

        # Step 4: Collect remaining unmerged pattern candidates.
        unmerged_patterns: list[_MergeCandidate] = [
            c for idx, c in enumerate(pattern_candidates) if idx not in merged_pattern_indices
        ]

        # Step 5: Build final PIIFinding list from all candidates.
        all_candidates: list[_MergeCandidate] = nlp_candidates + unmerged_patterns
        findings: list[PIIFinding] = []

        for candidate in all_candidates:
            findings.append(
                PIIFinding(
                    field_name=field_name,
                    pii_type=candidate.pii_type,
                    detection_method=candidate.detection_method,
                    confidence=round(candidate.confidence, 4),
                    matched_value=candidate.matched_value,
                    start_pos=candidate.start_pos,
                    end_pos=candidate.end_pos,
                    metadata=candidate.metadata,
                )
            )

        return findings

    @staticmethod
    def _spans_overlap(
        start_a: int | None,
        end_a: int | None,
        start_b: int | None,
        end_b: int | None,
    ) -> bool:
        """Determine whether two character spans overlap.

        Two spans ``[start_a, end_a)`` and ``[start_b, end_b)`` overlap
        when ``start_a < end_b`` and ``start_b < end_a``.  Returns
        ``False`` when either span has ``None`` boundaries (position
        information unavailable).

        Args:
            start_a: Start of the first span (inclusive).
            end_a: End of the first span (exclusive).
            start_b: Start of the second span (inclusive).
            end_b: End of the second span (exclusive).

        Returns:
            ``True`` if the spans overlap; ``False`` otherwise.
        """
        if start_a is None or end_a is None or start_b is None or end_b is None:
            return False
        return start_a < end_b and start_b < end_a

    def _mask_value(self, value: str) -> str:
        """Redact a detected PII value for safe inclusion in audit records.

        Preserves enough of the original text (first 2 characters plus
        ``***``) for analysts to identify the data type without exposing
        the full PII value.

        Args:
            value: The raw detected text to redact.

        Returns:
            A redacted string in the form ``"<first_2>***"`` for strings
            of 2+ characters, or ``"***"`` for shorter strings.

        Example::

            detector = PIIDetector()
            assert detector._mask_value("John Smith") == "Jo***"
            assert detector._mask_value("A") == "***"
            assert detector._mask_value("AB") == "AB***"
            assert detector._mask_value("") == "***"
        """
        if len(value) < 2:
            return "***"
        return value[:2] + "***"

    def _filter_by_confidence(
        self,
        findings: list[PIIFinding],
    ) -> list[PIIFinding]:
        """Filter findings below the configured confidence threshold.

        Findings with a confidence score strictly below
        ``self._confidence_threshold`` are excluded from the result.
        Filtered-out findings are logged at ``DEBUG`` level for
        diagnostic traceability.

        Args:
            findings: The list of findings to filter.

        Returns:
            A new list containing only findings at or above the
            confidence threshold.
        """
        accepted: list[PIIFinding] = []

        for finding in findings:
            if finding.confidence >= self._confidence_threshold:
                accepted.append(finding)
            else:
                self._logger.debug(
                    "finding_filtered_by_confidence",
                    field_name=finding.field_name,
                    pii_type=finding.pii_type,
                    confidence=finding.confidence,
                    threshold=self._confidence_threshold,
                    detection_method=finding.detection_method,
                )

        return accepted


# ---------------------------------------------------------------------------
# Module-level convenience function
# ---------------------------------------------------------------------------


def detect_pii(
    data: dict[str, Any] | list[dict[str, Any]],
    confidence_threshold: float = 0.85,
) -> PIIDetectionResult:
    """Convenience function for one-shot PII detection.

    Creates a default :class:`PIIDetector` instance with the specified
    confidence threshold, runs the dual-layer detection pipeline against
    the provided data, and returns the result.

    This function is suitable for simple use cases and scripts.  For
    repeated scanning (e.g. batch pipelines), prefer creating a
    :class:`PIIDetector` instance once and calling its methods to avoid
    re-loading the spaCy model on every call.

    Args:
        data: Input data to scan.  May be a single record
            (``dict[str, Any]``) or a list of records
            (``list[dict[str, Any]]``).
        confidence_threshold: Minimum confidence for findings to be
            included.  Defaults to ``0.85``.

    Returns:
        A :class:`PIIDetectionResult` with all findings above the
        threshold.

    Example::

        from compliance_service.detectors.pii_detector import detect_pii

        # Scan a single record
        result = detect_pii({"email": "user@example.com", "name": "John Smith"})
        if result.has_pii:
            print(f"Found {result.total_findings} PII items: "
                  f"{result.pii_types_found}")

        # Scan multiple records with custom threshold
        result = detect_pii(
            [
                {"ssn": "123-45-6789"},
                {"note": "No PII here"},
            ],
            confidence_threshold=0.90,
        )
        for finding in result.findings:
            print(f"{finding.field_name}: {finding.pii_type} "
                  f"({finding.confidence:.2f})")
    """
    detector = PIIDetector(confidence_threshold=confidence_threshold)
    return detector.detect_pii(data)
