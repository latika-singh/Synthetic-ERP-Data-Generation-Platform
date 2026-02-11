"""Regex-based pattern matching engine for structured PII detection.

This module implements the **regex layer** of the dual-layer PII detection
approach used by the Compliance Service.  It provides configurable regex
patterns for detecting structured PII formats that have deterministic
patterns — SSN, email, phone, credit card (Luhn-validated), IBAN, IP
address, date of birth, and financial account identifiers.

Each pattern carries a configurable confidence threshold so that
downstream consumers (e.g. the PII detector orchestrator) can weight
findings appropriately.  Secondary validation algorithms (Luhn for
credit cards, range rules for SSNs) reduce false-positive rates.

Design decisions:
    * **Pre-compiled regex** — All patterns are compiled once at
      ``PatternDetector.__init__()`` time and reused across calls to
      ``detect()`` / ``detect_batch()``.  This avoids repeated regex
      compilation overhead in high-throughput scanning scenarios.
    * **Redacted matched text** — ``PatternMatch.matched_text`` stores
      only a redacted representation (first 2 + ``***`` + last char)
      so that the match result itself does not leak PII into logs or
      audit records.
    * **Runtime extensibility** — ``add_pattern()`` / ``remove_pattern()``
      allow callers to register custom patterns without restarting the
      service, supporting tenant-specific compliance requirements.

Usage::

    from compliance_service.detectors.pattern_detector import PatternDetector

    detector = PatternDetector()
    result = detector.detect("Contact john@example.com or 123-45-6789")
    assert result.has_pii is True
    for match in result.matches:
        print(match.pattern_name, match.confidence)

See Also:
    ``compliance_service.detectors.nlp_detector`` — The spaCy NLP layer
    that complements this regex layer for detecting unstructured PII
    (names, addresses) via entity recognition.
"""

from __future__ import annotations

import re
import time

from pydantic import BaseModel, Field

from shared.logging.structured_logger import get_logger


# ---------------------------------------------------------------------------
# Pydantic Data Models
# ---------------------------------------------------------------------------


class PIIPattern(BaseModel):
    """Configuration model for a single PII detection regex pattern.

    Each ``PIIPattern`` encapsulates the regex string, its PII
    classification, a default confidence score, and an enabled flag
    that controls whether the pattern participates in detection scans.

    Attributes:
        name: Unique pattern identifier (e.g. ``'SSN'``, ``'EMAIL'``).
        regex: The regex pattern string.  Compiled at detector
            initialisation time via :func:`re.compile`.
        pii_type: PII classification category used for grouping related
            patterns (e.g. multiple SSN variants share ``pii_type='SSN'``).
        confidence: Default confidence score in the range ``[0.0, 1.0]``.
            Higher values indicate a stronger signal that a match is
            genuine PII rather than a coincidental pattern.
        description: Human-readable explanation of what the pattern
            detects, suitable for audit reports and UI display.
        enabled: Whether this pattern is active.  Disabled patterns are
            skipped during ``detect()`` and ``detect_batch()`` calls.

    Example::

        pattern = PIIPattern(
            name="SSN",
            regex=r"\\b\\d{3}-\\d{2}-\\d{4}\\b",
            pii_type="SSN",
            confidence=0.95,
            description="US Social Security Number (xxx-xx-xxxx)",
        )
    """

    name: str = Field(..., description="Unique pattern identifier")
    regex: str = Field(..., description="Regex pattern string")
    pii_type: str = Field(..., description="PII classification category")
    confidence: float = Field(
        ...,
        ge=0.0,
        le=1.0,
        description="Default confidence score [0.0, 1.0]",
    )
    description: str = Field(default="", description="Human-readable description")
    enabled: bool = Field(default=True, description="Whether this pattern is active")


class PatternMatch(BaseModel):
    """Result model for a single PII pattern match found in scanned text.

    Matched text is **always redacted** to prevent PII from leaking into
    logs, audit records, or downstream data stores.

    Attributes:
        pattern_name: Identifier of the pattern that produced this match.
        pii_type: PII classification category of the matched pattern.
        matched_text: Redacted representation of the matched text
            (e.g. ``'12***9'``).  Full text is never stored.
        start_pos: Zero-based start character index in the source text.
        end_pos: Zero-based end character index (exclusive) in the
            source text.
        confidence: Confidence score for this particular match.  May
            differ from the pattern's default if secondary validation
            adjusts it.
        validation_passed: ``True`` when secondary validation (Luhn
            algorithm, SSN range check) confirms the match; ``False``
            when validation fails but the regex still matched.

    Example::

        match = PatternMatch(
            pattern_name="CREDIT_CARD",
            pii_type="CREDIT_CARD",
            matched_text="41***3",
            start_pos=10,
            end_pos=29,
            confidence=0.90,
            validation_passed=True,
        )
    """

    pattern_name: str = Field(..., description="Pattern that produced this match")
    pii_type: str = Field(..., description="PII classification category")
    matched_text: str = Field(..., description="Redacted matched text")
    start_pos: int = Field(..., ge=0, description="Start character position")
    end_pos: int = Field(..., ge=0, description="End character position (exclusive)")
    confidence: float = Field(
        ...,
        ge=0.0,
        le=1.0,
        description="Confidence score for this match",
    )
    validation_passed: bool = Field(
        default=True,
        description="Whether secondary validation passed",
    )


class PatternDetectionResult(BaseModel):
    """Aggregate result of a PII pattern detection scan on a single text.

    Attributes:
        matches: Ordered list of all pattern matches found.
        has_pii: Convenience flag — ``True`` when ``len(matches) > 0``.
        match_count: Total number of matches (equal to ``len(matches)``).
        patterns_checked: Number of enabled patterns that were evaluated.
        processing_time_ms: Wall-clock scan duration in milliseconds,
            measured via :func:`time.perf_counter`.

    Example::

        result = PatternDetectionResult(
            matches=[...],
            has_pii=True,
            match_count=2,
            patterns_checked=8,
            processing_time_ms=1.23,
        )
    """

    matches: list[PatternMatch] = Field(
        default_factory=list,
        description="All pattern matches found",
    )
    has_pii: bool = Field(default=False, description="Whether any PII was detected")
    match_count: int = Field(default=0, ge=0, description="Total match count")
    patterns_checked: int = Field(
        default=0,
        ge=0,
        description="Number of patterns evaluated",
    )
    processing_time_ms: float = Field(
        default=0.0,
        ge=0.0,
        description="Scan duration in milliseconds",
    )


# ---------------------------------------------------------------------------
# Default PII Patterns
# ---------------------------------------------------------------------------

DEFAULT_PATTERNS: list[PIIPattern] = [
    PIIPattern(
        name="SSN",
        regex=r"\b\d{3}-\d{2}-\d{4}\b",
        pii_type="SSN",
        confidence=0.95,
        description="US Social Security Number (xxx-xx-xxxx)",
    ),
    PIIPattern(
        name="SSN_NO_DASH",
        regex=r"\b(?!000|666|9\d{2})\d{3}(?!00)\d{2}(?!0000)\d{4}\b",
        pii_type="SSN",
        confidence=0.70,
        description="US SSN without dashes (with basic validation)",
    ),
    PIIPattern(
        name="EMAIL",
        regex=r"\b[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Z|a-z]{2,}\b",
        pii_type="EMAIL",
        confidence=0.98,
        description="Email address",
    ),
    PIIPattern(
        name="PHONE_US",
        regex=r"\b(\+?1[-.\s]?)?(\(?\d{3}\)?[-.\s]?)\d{3}[-.\s]?\d{4}\b",
        pii_type="PHONE",
        confidence=0.85,
        description="US phone number with various separators",
    ),
    PIIPattern(
        name="CREDIT_CARD",
        regex=r"\b(?:\d{4}[-\s]?){3}\d{4}\b",
        pii_type="CREDIT_CARD",
        confidence=0.90,
        description="Credit card number (16 digits with optional separators)",
    ),
    PIIPattern(
        name="IBAN",
        regex=r"\b[A-Z]{2}\d{2}[A-Z0-9]{4}\d{7}([A-Z0-9]?){0,16}\b",
        pii_type="IBAN",
        confidence=0.92,
        description="International Bank Account Number",
    ),
    PIIPattern(
        name="IP_ADDRESS",
        regex=r"\b(?:(?:25[0-5]|2[0-4]\d|[01]?\d\d?)\.){3}(?:25[0-5]|2[0-4]\d|[01]?\d\d?)\b",
        pii_type="IP_ADDRESS",
        confidence=0.80,
        description="IPv4 address",
    ),
    PIIPattern(
        name="DATE_OF_BIRTH",
        regex=r"\b(0[1-9]|1[0-2])/(0[1-9]|[12]\d|3[01])/\d{4}\b",
        pii_type="DATE_OF_BIRTH",
        confidence=0.60,
        description="Date in MM/DD/YYYY format (lower confidence — many legitimate dates)",
    ),
]


# ---------------------------------------------------------------------------
# Pattern Detector
# ---------------------------------------------------------------------------


class PatternDetector:
    """Regex-based PII pattern matching engine.

    Scans text fields against a registry of compiled regex patterns to
    detect structured PII formats.  Each match is enriched with a
    confidence score and, where applicable, validated through secondary
    algorithms (Luhn for credit cards, range rules for SSNs).

    The detector ships with :data:`DEFAULT_PATTERNS` covering SSN, email,
    phone, credit card, IBAN, IPv4, and date-of-birth formats.  Custom
    patterns can be supplied at construction time or added at runtime via
    :meth:`add_pattern`.

    Args:
        patterns: Base pattern list.  Defaults to :data:`DEFAULT_PATTERNS`
            when ``None``.
        custom_patterns: Additional patterns appended to the base list.
            Useful for tenant-specific compliance requirements.

    Example::

        # Use defaults
        detector = PatternDetector()
        result = detector.detect("SSN: 123-45-6789")
        assert result.has_pii

        # Custom pattern
        from compliance_service.detectors.pattern_detector import PIIPattern

        custom = PIIPattern(
            name="PASSPORT_US",
            regex=r"\\b[A-Z]\\d{8}\\b",
            pii_type="PASSPORT",
            confidence=0.85,
            description="US passport number",
        )
        detector = PatternDetector(custom_patterns=[custom])
    """

    def __init__(
        self,
        patterns: list[PIIPattern] | None = None,
        custom_patterns: list[PIIPattern] | None = None,
    ) -> None:
        """Initialise the pattern detector with compiled regex patterns.

        Args:
            patterns: Base pattern list.  When ``None`` the module-level
                :data:`DEFAULT_PATTERNS` are used.
            custom_patterns: Optional additional patterns merged after
                the base list.
        """
        self._logger = get_logger(__name__)

        # Build the effective pattern registry.
        self._patterns: list[PIIPattern] = list(patterns if patterns is not None else DEFAULT_PATTERNS)
        if custom_patterns:
            self._patterns.extend(custom_patterns)

        # Pre-compile enabled patterns for scan-time performance.
        self._compiled: dict[str, re.Pattern[str]] = {p.name: re.compile(p.regex) for p in self._patterns if p.enabled}

        # Build a fast name → pattern lookup.
        self._pattern_map: dict[str, PIIPattern] = {p.name: p for p in self._patterns}

        self._logger.info(
            "pattern_detector_initialized",
            total_patterns=len(self._patterns),
            active_patterns=len(self._compiled),
            pattern_names=list(self._compiled.keys()),
        )

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def detect(self, text: str) -> PatternDetectionResult:
        """Scan a single text value for PII patterns.

        Iterates over all enabled (compiled) patterns, applies
        :func:`re.finditer` for each, and collects matches.  Credit card
        and SSN matches undergo secondary validation to reduce false
        positives.

        Args:
            text: The text string to scan.  May be empty, in which case
                an empty result is returned immediately.

        Returns:
            A :class:`PatternDetectionResult` containing all matches,
            aggregate counts, and timing information.

        Example::

            detector = PatternDetector()
            result = detector.detect("Email me at user@test.com")
            for m in result.matches:
                print(m.pattern_name, m.confidence)
        """
        start_time: float = time.perf_counter()
        matches: list[PatternMatch] = []
        patterns_checked: int = 0

        if not text:
            elapsed_ms = (time.perf_counter() - start_time) * 1000.0
            return PatternDetectionResult(
                matches=[],
                has_pii=False,
                match_count=0,
                patterns_checked=0,
                processing_time_ms=round(elapsed_ms, 3),
            )

        for pattern_name, compiled_re in self._compiled.items():
            patterns_checked += 1
            pattern_cfg: PIIPattern = self._pattern_map[pattern_name]

            for regex_match in compiled_re.finditer(text):
                matched_text_raw: str = regex_match.group()
                start_pos: int = regex_match.start()
                end_pos: int = regex_match.end()
                confidence: float = pattern_cfg.confidence
                validation_passed: bool = True

                # ---- Secondary validation for credit card numbers ----
                if pattern_cfg.pii_type == "CREDIT_CARD":
                    validation_passed = self._validate_credit_card(matched_text_raw)
                    if not validation_passed:
                        # Lower confidence when Luhn check fails — the
                        # regex matched a 16-digit group but it is not a
                        # valid card number.
                        confidence = max(confidence * 0.5, 0.0)

                # ---- Secondary validation for SSNs ----
                if pattern_cfg.pii_type == "SSN" and "-" in matched_text_raw:
                    validation_passed = self._validate_ssn(matched_text_raw)
                    if not validation_passed:
                        confidence = max(confidence * 0.6, 0.0)

                # Redact the matched text before storing.
                redacted: str = self._redact_match(matched_text_raw)

                matches.append(
                    PatternMatch(
                        pattern_name=pattern_name,
                        pii_type=pattern_cfg.pii_type,
                        matched_text=redacted,
                        start_pos=start_pos,
                        end_pos=end_pos,
                        confidence=round(confidence, 4),
                        validation_passed=validation_passed,
                    ),
                )

        elapsed_ms = (time.perf_counter() - start_time) * 1000.0
        has_pii: bool = len(matches) > 0

        if has_pii:
            self._logger.info(
                "pii_patterns_detected",
                match_count=len(matches),
                patterns_checked=patterns_checked,
                pii_types=list({m.pii_type for m in matches}),
                processing_time_ms=round(elapsed_ms, 3),
            )

        return PatternDetectionResult(
            matches=matches,
            has_pii=has_pii,
            match_count=len(matches),
            patterns_checked=patterns_checked,
            processing_time_ms=round(elapsed_ms, 3),
        )

    def detect_batch(self, texts: list[str]) -> list[PatternDetectionResult]:
        """Scan multiple text values for PII patterns.

        Processes each text individually via :meth:`detect` and returns
        the results in the same order.  Logs aggregate batch metrics.

        Args:
            texts: List of text strings to scan.

        Returns:
            A list of :class:`PatternDetectionResult`, one per input
            text, preserving input order.

        Example::

            detector = PatternDetector()
            results = detector.detect_batch(
                [
                    "SSN: 123-45-6789",
                    "No PII here",
                    "Card: 4111 1111 1111 1111",
                ]
            )
            for idx, r in enumerate(results):
                print(f"Text {idx}: {r.match_count} matches")
        """
        batch_start: float = time.perf_counter()
        results: list[PatternDetectionResult] = []
        total_matches: int = 0

        for text in texts:
            result = self.detect(text)
            results.append(result)
            total_matches += result.match_count

        batch_elapsed_ms = (time.perf_counter() - batch_start) * 1000.0

        self._logger.info(
            "batch_detection_complete",
            texts_scanned=len(texts),
            total_matches=total_matches,
            texts_with_pii=sum(1 for r in results if r.has_pii),
            batch_processing_time_ms=round(batch_elapsed_ms, 3),
        )

        return results

    def add_pattern(self, pattern: PIIPattern) -> None:
        """Register a custom PII pattern at runtime.

        The new pattern is appended to the internal registry.  If
        ``pattern.enabled`` is ``True``, its regex is compiled
        immediately and becomes available to subsequent ``detect()``
        calls.

        Args:
            pattern: The :class:`PIIPattern` to register.

        Raises:
            ValueError: If a pattern with the same ``name`` already
                exists in the registry.

        Example::

            detector = PatternDetector()
            detector.add_pattern(
                PIIPattern(
                    name="PASSPORT_US",
                    regex=r"\\b[A-Z]\\d{8}\\b",
                    pii_type="PASSPORT",
                    confidence=0.85,
                    description="US passport number",
                )
            )
        """
        if pattern.name in self._pattern_map:
            raise ValueError(
                f"Pattern '{pattern.name}' already exists. Remove it first or use a different name.",
            )

        self._patterns.append(pattern)
        self._pattern_map[pattern.name] = pattern

        if pattern.enabled:
            self._compiled[pattern.name] = re.compile(pattern.regex)

        self._logger.info(
            "pattern_added",
            pattern_name=pattern.name,
            pii_type=pattern.pii_type,
            confidence=pattern.confidence,
            enabled=pattern.enabled,
            active_patterns=len(self._compiled),
        )

    def remove_pattern(self, pattern_name: str) -> bool:
        """Remove a pattern from the detector by name.

        Both the pattern configuration and its compiled regex are
        purged.  Subsequent ``detect()`` calls will no longer match
        this pattern.

        Args:
            pattern_name: The ``name`` of the pattern to remove.

        Returns:
            ``True`` if the pattern was found and removed; ``False`` if
            no pattern with the given name existed.

        Example::

            detector = PatternDetector()
            removed = detector.remove_pattern("DATE_OF_BIRTH")
            assert removed is True
        """
        if pattern_name not in self._pattern_map:
            self._logger.warning(
                "pattern_remove_not_found",
                pattern_name=pattern_name,
            )
            return False

        # Remove from all internal data structures.
        del self._pattern_map[pattern_name]
        self._compiled.pop(pattern_name, None)
        self._patterns = [p for p in self._patterns if p.name != pattern_name]

        self._logger.info(
            "pattern_removed",
            pattern_name=pattern_name,
            remaining_patterns=len(self._compiled),
        )
        return True

    # ------------------------------------------------------------------
    # Secondary Validation Helpers
    # ------------------------------------------------------------------

    def _validate_credit_card(self, number: str) -> bool:
        """Validate a potential credit card number using the Luhn algorithm.

        The Luhn (mod-10) algorithm is an industry-standard checksum
        used to distinguish valid credit card numbers from arbitrary
        16-digit sequences, significantly reducing false positives.

        Algorithm steps:
            1. Strip non-digit characters (spaces, dashes).
            2. Starting from the rightmost digit, double every second
               digit.
            3. If doubling produces a value > 9, subtract 9.
            4. Sum all digits.
            5. The number is valid if the total modulo 10 equals zero.

        Args:
            number: The raw matched string (may contain spaces or
                dashes between digit groups).

        Returns:
            ``True`` if the Luhn checksum passes; ``False`` otherwise.

        Example::

            detector = PatternDetector()
            assert detector._validate_credit_card("4111-1111-1111-1111")
            assert not detector._validate_credit_card("1234-5678-9012-3456")
        """
        # Strip all non-digit characters.
        digits_only: str = re.sub(r"[^0-9]", "", number)

        # A valid card number must have between 13 and 19 digits.
        if len(digits_only) < 13 or len(digits_only) > 19:
            return False

        total: int = 0
        reverse_digits: str = digits_only[::-1]

        for idx, char in enumerate(reverse_digits):
            digit: int = int(char)
            if idx % 2 == 1:
                digit *= 2
                if digit > 9:
                    digit -= 9
            total += digit

        return total % 10 == 0

    def _validate_ssn(self, ssn: str) -> bool:
        """Validate a US Social Security Number against SSA rules.

        The Social Security Administration has published rules about
        invalid SSN ranges.  Applying these rules reduces false
        positives for 9-digit sequences that happen to match the
        ``xxx-xx-xxxx`` pattern but are not valid SSNs.

        Invalid conditions:
            * Area number (first 3 digits) is ``000``.
            * Area number is ``666``.
            * Area number is in the ``900-999`` range.
            * Group number (middle 2 digits) is ``00``.
            * Serial number (last 4 digits) is ``0000``.

        Args:
            ssn: The matched SSN string in ``xxx-xx-xxxx`` format.

        Returns:
            ``True`` if the SSN passes all range checks; ``False`` if
            any rule is violated.

        Example::

            detector = PatternDetector()
            assert detector._validate_ssn("123-45-6789")
            assert not detector._validate_ssn("000-45-6789")
            assert not detector._validate_ssn("666-45-6789")
            assert not detector._validate_ssn("900-45-6789")
        """
        # Strip dashes for numeric analysis.
        digits_only: str = ssn.replace("-", "")

        if len(digits_only) != 9:
            return False

        area: str = digits_only[:3]
        group: str = digits_only[3:5]
        serial: str = digits_only[5:]

        # Area number must not be 000, 666, or 900-999.
        if area == "000":
            return False
        if area == "666":
            return False
        if area.startswith("9"):
            return False

        # Group number must not be 00.
        if group == "00":
            return False

        # Serial number must not be 0000.
        return serial != "0000"

    def _redact_match(self, text: str) -> str:
        """Redact a matched PII string for safe storage in audit records.

        Preserves enough of the original text (first 2 characters and
        last character) for analysts to identify the data type while
        preventing the full PII value from leaking into logs or data
        stores.

        Args:
            text: The raw matched text to redact.

        Returns:
            A redacted string in the form ``"<first_2>***<last>"`` for
            strings with 4+ characters, ``"***"`` for shorter strings.

        Example::

            detector = PatternDetector()
            assert detector._redact_match("123-45-6789") == "12***9"
            assert detector._redact_match("AB") == "***"
        """
        if len(text) < 4:
            return "***"
        return text[:2] + "***" + text[-1]
