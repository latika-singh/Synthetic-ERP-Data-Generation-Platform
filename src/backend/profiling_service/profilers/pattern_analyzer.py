"""Data pattern detection and analysis module for the Profiling Service.

This module implements the :class:`PatternAnalyzer` class, which detects format
patterns, regex patterns, value ranges, cardinality, null rates, character-class
distributions, string-length statistics, common prefix/suffix patterns, and
value frequency distributions for individual ERP data columns.

**Constraint C-001 Compliance:**

The PatternAnalyzer operates exclusively on metadata aggregates—it **never**
stores or returns raw production data values.  All format samples produced by
:meth:`PatternAnalyzer._generate_sample_formats` are anonymised exemplars
where digits are replaced by ``X``, letters by ``A``, and only structural
delimiters are preserved (e.g., ``'INV-2024-0001'`` → ``'AAA-XXXX-XXXX'``).

**ERP-Specific Format Patterns:**

The module ships with :data:`FORMAT_PATTERN_RULES`, a registry of predefined
regex-to-template mappings covering common ERP data formats across the four
initial modules (Financial Accounting, Human Resources, Sales & Distribution,
Material Management—per constraint C-005):

* SAP material numbers (18-digit)
* SAP document numbers (10-digit)
* ISO / US date formats
* Currency values, UUIDs, postal codes, phone numbers, e-mail addresses
* ERP account number patterns (``XX-XXXX-XXXX``)

**Integration with the Profiling Pipeline:**

The :class:`PatternAnalyzer` produces :class:`PatternMetadata` Pydantic 2.x
model instances that are stored in the ``statistical_profiles`` MongoDB
collection as part of :class:`ColumnProfile`.  The Generation Engine reads
these patterns to configure rules-based and intelligent masking generation
methods with accurate format templates.

Usage::

    from profiling_service.profilers.pattern_analyzer import PatternAnalyzer

    analyzer = PatternAnalyzer()
    metadata = analyzer.analyze_column_patterns(
        column_name="invoice_number",
        values=["INV-2024-0001", "INV-2024-0002", None],
        data_type="VARCHAR",
    )
"""

from __future__ import annotations

import re
import string
from collections import Counter, defaultdict
from typing import Any, Optional, Union

import numpy as np
import pandas as pd

from profiling_service.config import get_config
from profiling_service.models.statistical_profile import DataCategory, PatternMetadata
from shared.logging.structured_logger import get_logger


# ===================================================================
# Module-level constants
# ===================================================================

FORMAT_PATTERN_RULES: dict[str, dict[str, str]] = {
    "date_iso": {
        "regex": r"\d{4}-\d{2}-\d{2}",
        "template": "YYYY-MM-DD",
    },
    "date_us": {
        "regex": r"\d{2}/\d{2}/\d{4}",
        "template": "MM/DD/YYYY",
    },
    "phone_us": {
        "regex": r"\(\d{3}\)\s?\d{3}-\d{4}",
        "template": "(XXX) XXX-XXXX",
    },
    "email": {
        "regex": r"[a-zA-Z0-9._%+-]+@[a-zA-Z0-9.-]+\.[a-zA-Z]{2,}",
        "template": "xxx@xxx.xxx",
    },
    "account_number": {
        "regex": r"[A-Z]{2,4}-\d{4,}-\d{4,}",
        "template": "XX-XXXX-XXXX",
    },
    "sap_material": {
        "regex": r"\d{18}",
        "template": "XXXXXXXXXXXXXXXXXX",
    },
    "sap_document": {
        "regex": r"\d{10}",
        "template": "XXXXXXXXXX",
    },
    "currency": {
        "regex": r"\d+\.\d{2}",
        "template": "N.NN",
    },
    "uuid": {
        "regex": (
            r"[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}"
            r"-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}"
        ),
        "template": "XXXXXXXX-XXXX-XXXX-XXXX-XXXXXXXXXXXX",
    },
    "postal_code": {
        "regex": r"\d{5}(-\d{4})?",
        "template": "XXXXX or XXXXX-XXXX",
    },
}
"""Predefined format pattern templates for common ERP data formats.

Each entry maps a human-readable pattern name to a dictionary containing:

* ``regex`` — A regular expression string matching the pattern.
* ``template`` — An anonymised human-readable format string.
"""

CHARACTER_CLASSES: dict[str, str] = {
    "alpha": string.ascii_letters,
    "digit": string.digits,
    "special": string.punctuation,
    "whitespace": string.whitespace,
}
"""Character class reference sets used by :meth:`PatternAnalyzer._analyze_character_classes`.

Maps class names to the full set of characters belonging to that class.
"""

# Pre-compute character lookup sets for O(1) membership tests.
_ALPHA_SET: frozenset[str] = frozenset(string.ascii_letters)
_DIGIT_SET: frozenset[str] = frozenset(string.digits)
_SPECIAL_SET: frozenset[str] = frozenset(string.punctuation)
_WHITESPACE_SET: frozenset[str] = frozenset(string.whitespace)

# Default prefix/suffix extraction length.
_DEFAULT_PREFIX_SUFFIX_LENGTH: int = 4

# Minimum match rate for a predefined pattern to be accepted.
_PATTERN_MATCH_THRESHOLD: float = 0.8

# Minimum dominance rate for an inferred pattern to be accepted.
_INFER_DOMINANCE_THRESHOLD: float = 0.6

# Maximum number of values to sample when testing predefined patterns.
_PATTERN_SAMPLE_LIMIT: int = 100

# Threshold fraction for prefix/suffix relevance.
_PREFIX_SUFFIX_RELEVANCE_THRESHOLD: float = 0.05


# ===================================================================
# PatternAnalyzer Class
# ===================================================================


class PatternAnalyzer:
    """Comprehensive data pattern detection and analysis for ERP columns.

    Detects format patterns, regex patterns, string length statistics,
    common prefixes/suffixes, character-class distributions, cardinality,
    null rates, and value frequencies.  Produces :class:`PatternMetadata`
    instances stored in the ``statistical_profiles`` MongoDB collection as
    part of :class:`ColumnProfile`.

    All analysis enforces **Constraint C-001**: only metadata and
    statistical summaries are produced—raw production data is never stored
    or returned.

    Args:
        config: Optional configuration dictionary.  When ``None``, the
            environment-specific configuration is loaded automatically via
            :func:`profiling_service.config.get_config`.

    Attributes:
        _logger: Structured JSON logger bound to this module.
        _max_categories: Upper limit on distinct categorical values tracked.
        _sample_size: Number of values sampled for pattern detection.
        _max_prefix_suffix_length: Maximum character length for
            prefix/suffix extraction.
        _compiled_patterns: Pre-compiled regex objects for each entry in
            :data:`FORMAT_PATTERN_RULES`.
    """

    # ------------------------------------------------------------------
    # Initialiser
    # ------------------------------------------------------------------

    def __init__(self, config: Optional[dict[str, Any]] = None) -> None:
        """Initialise the PatternAnalyzer.

        Args:
            config: Optional dictionary overriding default configuration
                values.  Recognised keys:

                * ``max_categories`` — Maximum distinct categorical values
                  tracked per column.
                * ``sample_size`` — Rows sampled for pattern analysis.
                * ``max_prefix_suffix_length`` — Character length for
                  prefix/suffix extraction.
        """
        self._logger = get_logger(__name__)

        # Load platform configuration or apply overrides.
        if config is not None:
            self._max_categories: int = int(
                config.get("max_categories", 100)
            )
            self._sample_size: int = int(
                config.get("sample_size", 10000)
            )
            self._max_prefix_suffix_length: int = int(
                config.get("max_prefix_suffix_length", _DEFAULT_PREFIX_SUFFIX_LENGTH)
            )
        else:
            try:
                svc_config = get_config()
                self._max_categories = svc_config.PROFILING_MAX_CATEGORIES
                self._sample_size = svc_config.PROFILING_SAMPLE_SIZE
            except Exception:
                # Graceful fallback when config infrastructure is unavailable
                # (e.g., during isolated unit tests).
                self._logger.warning(
                    "config_load_fallback",
                    message="Failed to load service config; using defaults.",
                )
                self._max_categories = 100
                self._sample_size = 10000
            self._max_prefix_suffix_length = _DEFAULT_PREFIX_SUFFIX_LENGTH

        # Pre-compile FORMAT_PATTERN_RULES regex patterns for performance.
        self._compiled_patterns: dict[str, tuple[re.Pattern[str], str]] = {}
        for name, rule in FORMAT_PATTERN_RULES.items():
            try:
                compiled = re.compile(rule["regex"])
                self._compiled_patterns[name] = (compiled, rule["template"])
            except re.error as exc:
                self._logger.error(
                    "regex_compile_error",
                    pattern_name=name,
                    regex=rule["regex"],
                    error=str(exc),
                )

        self._logger.debug(
            "pattern_analyzer_initialized",
            max_categories=self._max_categories,
            sample_size=self._sample_size,
            compiled_patterns=len(self._compiled_patterns),
        )

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def analyze_column_patterns(
        self,
        column_name: str,
        values: list[Any],
        data_type: str,
    ) -> PatternMetadata:
        """Analyse patterns for a single ERP data column.

        This is the main entry point for pattern analysis.  The method
        filters null values, converts remaining values to strings, detects
        format patterns, computes string length statistics, discovers
        common prefixes/suffixes, generates anonymised sample formats
        (C-001 compliant), and computes character-class distributions.

        Args:
            column_name: Name of the column being analysed.
            values: Raw column values (may contain ``None`` / ``NaN``).
            data_type: Database-level data type string (e.g., ``VARCHAR``,
                ``INTEGER``, ``DATE``).

        Returns:
            A fully populated :class:`PatternMetadata` instance.
        """
        self._logger.info(
            "column_pattern_analysis_started",
            column_name=column_name,
            data_type=data_type,
            total_values=len(values),
        )

        try:
            # Classify the column's data category for branching logic.
            category = self._classify_data_category(data_type)

            # Filter out None / NaN values using robust pandas detection.
            series = pd.Series(values)
            non_null_mask = ~pd.isna(series)
            non_null_values: list[Any] = series[non_null_mask].tolist()

            if not non_null_values:
                self._logger.warning(
                    "column_all_null",
                    column_name=column_name,
                )
                return PatternMetadata()

            # Convert to string representations for pattern analysis.
            str_values: list[str] = [str(v) for v in non_null_values]

            # --- Data-category-aware pattern analysis ---

            # Format / regex pattern detection.
            # Temporal columns benefit most from date-pattern matching;
            # Numeric columns rarely have meaningful string patterns.
            format_pattern: Optional[str] = None
            regex_pattern: Optional[str] = None
            if category == DataCategory.TEMPORAL or category == DataCategory.TEXT:
                format_pattern, regex_pattern = self._detect_format_pattern(
                    str_values
                )
            elif category == DataCategory.NUMERIC:
                # Numeric values have a simpler structure; still check
                # for currency/accounting format patterns.
                format_pattern, regex_pattern = self._detect_format_pattern(
                    str_values
                )
            elif category == DataCategory.CATEGORICAL:
                # Categorical codes may follow fixed formats.
                format_pattern, regex_pattern = self._detect_format_pattern(
                    str_values
                )

            # Compute string length statistics (always useful).
            length_stats = self._analyze_string_lengths(str_values)

            # Prefix / suffix detection — most useful for TEXT and
            # CATEGORICAL columns (e.g. "INV-" prefix, ".00" suffix).
            # Numeric columns skip this to avoid noise.
            common_prefixes: list[str] = []
            common_suffixes: list[str] = []
            if category in (DataCategory.TEXT, DataCategory.CATEGORICAL):
                common_prefixes = self._detect_common_prefixes(str_values)
                common_suffixes = self._detect_common_suffixes(str_values)
            elif category == DataCategory.TEMPORAL:
                # Date strings may share year-based prefixes.
                common_prefixes = self._detect_common_prefixes(str_values)
                common_suffixes = []

            # Generate anonymised sample format exemplars (C-001).
            sample_formats = self._generate_sample_formats(str_values)

            # Compute character-class distribution (always informative).
            character_classes = self._analyze_character_classes(str_values)

            pattern_metadata = PatternMetadata(
                format_pattern=format_pattern,
                regex_pattern=regex_pattern,
                common_prefixes=common_prefixes,
                common_suffixes=common_suffixes,
                average_length=float(length_stats["average_length"]),
                min_length=int(length_stats["min_length"]),
                max_length=int(length_stats["max_length"]),
                sample_formats=sample_formats,
                character_classes=character_classes,
            )

            self._logger.info(
                "column_pattern_analysis_completed",
                column_name=column_name,
                data_category=category.value,
                format_pattern=format_pattern,
                regex_pattern=regex_pattern,
                avg_length=length_stats["average_length"],
                prefix_count=len(common_prefixes),
                suffix_count=len(common_suffixes),
                sample_format_count=len(sample_formats),
            )

            return pattern_metadata

        except Exception as exc:
            self._logger.error(
                "column_pattern_analysis_error",
                column_name=column_name,
                data_type=data_type,
                error=str(exc),
                exc_info=True,
            )
            # Return a safe empty PatternMetadata rather than propagating
            # an exception, so that upstream profiling can continue for
            # remaining columns.
            return PatternMetadata()

    def analyze_cardinality(self, values: list[Any]) -> dict[str, Any]:
        """Compute cardinality metrics for a column's values.

        Cardinality measures how many distinct values exist relative to
        the total non-null count, and classifies the result into one of
        six levels: ``constant``, ``low``, ``medium``, ``high``,
        ``very_high``, or ``unique``.

        Args:
            values: Raw column values (may contain ``None`` / ``NaN``).

        Returns:
            A dictionary containing:

            * ``total_count`` — Number of non-null values.
            * ``distinct_count`` — Number of unique values.
            * ``cardinality_ratio`` — ``distinct_count / total_count``.
            * ``classification`` — Human-readable cardinality level.
        """
        try:
            series = pd.Series(values)
            non_null = series[~pd.isna(series)]
            total_count: int = len(non_null)

            if total_count == 0:
                return {
                    "total_count": 0,
                    "distinct_count": 0,
                    "cardinality_ratio": 0.0,
                    "classification": "empty",
                }

            distinct_count: int = int(non_null.nunique())
            cardinality_ratio: float = distinct_count / total_count

            # Classify cardinality.
            if distinct_count == total_count:
                classification = "unique"
            elif distinct_count == 1:
                classification = "constant"
            elif distinct_count < 10:
                classification = "low"
            elif distinct_count < 100:
                classification = "medium"
            elif distinct_count < 1000:
                classification = "high"
            else:
                classification = "very_high"

            self._logger.debug(
                "cardinality_analysis",
                total_count=total_count,
                distinct_count=distinct_count,
                cardinality_ratio=round(cardinality_ratio, 4),
                classification=classification,
            )

            return {
                "total_count": total_count,
                "distinct_count": distinct_count,
                "cardinality_ratio": round(cardinality_ratio, 6),
                "classification": classification,
            }

        except Exception as exc:
            self._logger.error(
                "cardinality_analysis_error",
                error=str(exc),
                exc_info=True,
            )
            return {
                "total_count": 0,
                "distinct_count": 0,
                "cardinality_ratio": 0.0,
                "classification": "error",
            }

    def analyze_null_rate(self, values: list[Any]) -> dict[str, Any]:
        """Compute null-rate metrics for a column's values.

        Null rate is the fraction of ``None`` / ``NaN`` / missing values
        relative to the total count.  The result is classified into one
        of five levels: ``complete``, ``sparse``, ``moderate``, ``heavy``,
        or ``mostly_null``.

        Args:
            values: Raw column values (may contain ``None`` / ``NaN``).

        Returns:
            A dictionary containing:

            * ``total_count`` — Total number of values (including nulls).
            * ``null_count`` — Number of null / missing values.
            * ``non_null_count`` — Number of non-null values.
            * ``null_rate`` — ``null_count / total_count``.
            * ``null_classification`` — Human-readable null-rate level.
        """
        try:
            series = pd.Series(values)
            total_count: int = len(series)

            if total_count == 0:
                return {
                    "total_count": 0,
                    "null_count": 0,
                    "non_null_count": 0,
                    "null_rate": 0.0,
                    "null_classification": "empty",
                }

            null_count: int = int(pd.isna(series).sum())
            non_null_count: int = total_count - null_count
            null_rate: float = null_count / total_count

            # Classify null pattern.
            if null_rate == 0.0:
                null_classification = "complete"
            elif null_rate < 0.05:
                null_classification = "sparse"
            elif null_rate < 0.25:
                null_classification = "moderate"
            elif null_rate < 0.50:
                null_classification = "heavy"
            else:
                null_classification = "mostly_null"

            self._logger.debug(
                "null_rate_analysis",
                total_count=total_count,
                null_count=null_count,
                null_rate=round(null_rate, 4),
                null_classification=null_classification,
            )

            return {
                "total_count": total_count,
                "null_count": null_count,
                "non_null_count": non_null_count,
                "null_rate": round(null_rate, 6),
                "null_classification": null_classification,
            }

        except Exception as exc:
            self._logger.error(
                "null_rate_analysis_error",
                error=str(exc),
                exc_info=True,
            )
            return {
                "total_count": len(values) if values else 0,
                "null_count": 0,
                "non_null_count": 0,
                "null_rate": 0.0,
                "null_classification": "error",
            }

    # ------------------------------------------------------------------
    # Private helpers — data category classification
    # ------------------------------------------------------------------

    @staticmethod
    def _classify_data_category(data_type: str) -> DataCategory:
        """Map a database-level data type string to a :class:`DataCategory`.

        This classification drives data-type-specific branching in
        :meth:`analyze_column_patterns`, e.g. skipping prefix/suffix
        detection for numeric columns or prioritising date-format matching
        for temporal columns.

        Args:
            data_type: The database data type string (e.g., ``VARCHAR``,
                ``INTEGER``, ``DATE``, ``TIMESTAMP``).

        Returns:
            The corresponding :class:`DataCategory` enum member.
        """
        dt_upper = data_type.upper().strip()

        # Temporal types.
        if any(
            keyword in dt_upper
            for keyword in ("DATE", "TIME", "TIMESTAMP", "INTERVAL")
        ):
            return DataCategory.TEMPORAL

        # Numeric types.
        if any(
            keyword in dt_upper
            for keyword in (
                "INT", "INTEGER", "BIGINT", "SMALLINT", "TINYINT",
                "FLOAT", "DOUBLE", "DECIMAL", "NUMERIC", "REAL",
                "NUMBER", "MONEY", "CURRENCY",
            )
        ):
            return DataCategory.NUMERIC

        # Boolean types.
        if any(keyword in dt_upper for keyword in ("BOOL", "BOOLEAN", "BIT")):
            return DataCategory.CATEGORICAL

        # Text / string types — further refined below.
        if any(
            keyword in dt_upper
            for keyword in (
                "CHAR", "VARCHAR", "NCHAR", "NVARCHAR", "TEXT",
                "CLOB", "STRING", "NTEXT",
            )
        ):
            return DataCategory.TEXT

        # Fallback: treat unknown types as text for broadest analysis.
        return DataCategory.TEXT

    # ------------------------------------------------------------------
    # Private helpers — pattern detection
    # ------------------------------------------------------------------

    def _detect_format_pattern(
        self,
        str_values: list[str],
    ) -> tuple[Optional[str], Optional[str]]:
        """Detect a matching format pattern from predefined or inferred rules.

        Iterates through :data:`FORMAT_PATTERN_RULES` and tests each
        compiled regex against a sample of *str_values*.  If a predefined
        pattern matches ≥80 % of the sample, it is accepted.  Otherwise
        :meth:`_infer_pattern_from_values` attempts automatic detection.

        Args:
            str_values: Non-null string representations of column values.

        Returns:
            A ``(format_pattern, regex_pattern)`` tuple.  Both elements
            are ``None`` when no pattern could be determined.
        """
        if not str_values:
            return None, None

        # Sample values for efficient matching.
        sample = str_values[:_PATTERN_SAMPLE_LIMIT]
        sample_size = len(sample)

        # Test each predefined pattern.
        best_match_name: Optional[str] = None
        best_match_rate: float = 0.0

        for name, (compiled_re, template) in self._compiled_patterns.items():
            matches = sum(
                1 for val in sample if compiled_re.fullmatch(val)
            )
            match_rate = matches / sample_size
            if match_rate > _PATTERN_MATCH_THRESHOLD and match_rate > best_match_rate:
                best_match_rate = match_rate
                best_match_name = name

        if best_match_name is not None:
            _, template = self._compiled_patterns[best_match_name]
            regex_str = FORMAT_PATTERN_RULES[best_match_name]["regex"]
            self._logger.debug(
                "predefined_pattern_matched",
                pattern_name=best_match_name,
                match_rate=round(best_match_rate, 4),
                template=template,
            )
            return template, regex_str

        # Fall back to automatic inference.
        return self._infer_pattern_from_values(str_values)

    def _infer_pattern_from_values(
        self,
        str_values: list[str],
    ) -> tuple[Optional[str], Optional[str]]:
        """Infer an abstract format pattern from the values themselves.

        Groups values by string length, selects the dominant length group
        (>60 % of values), and analyses character positions to build an
        abstract template where digits become ``X``, letters become ``A``,
        and all other characters are kept as literal delimiters.

        Args:
            str_values: Non-null string column values.

        Returns:
            A ``(format_pattern, regex_pattern)`` tuple, or ``(None, None)``
            if no dominant pattern is found.
        """
        if not str_values:
            return None, None

        # Group values by string length.
        length_groups: dict[int, list[str]] = defaultdict(list)
        for val in str_values:
            length_groups[len(val)].append(val)

        # Find the dominant length group.
        total = len(str_values)
        dominant_length: Optional[int] = None
        dominant_group: list[str] = []

        for length, group in sorted(
            length_groups.items(), key=lambda kv: len(kv[1]), reverse=True
        ):
            if len(group) / total >= _INFER_DOMINANCE_THRESHOLD:
                dominant_length = length
                dominant_group = group
                break

        if dominant_length is None or dominant_length == 0:
            return None, None

        # Analyse character positions across the dominant group.
        sample = dominant_group[:_PATTERN_SAMPLE_LIMIT]
        format_chars: list[str] = []
        regex_chars: list[str] = []

        for pos in range(dominant_length):
            chars_at_pos = [v[pos] for v in sample if pos < len(v)]
            if not chars_at_pos:
                break

            alpha_count = sum(1 for c in chars_at_pos if c in _ALPHA_SET)
            digit_count = sum(1 for c in chars_at_pos if c in _DIGIT_SET)
            pos_total = len(chars_at_pos)

            # If ≥80 % of characters at this position are digits → X / \d
            if digit_count / pos_total >= 0.8:
                format_chars.append("X")
                regex_chars.append(r"\d")
            # If ≥80 % are alpha → A / [a-zA-Z]
            elif alpha_count / pos_total >= 0.8:
                format_chars.append("A")
                regex_chars.append("[a-zA-Z]")
            else:
                # Use the most common literal character at this position.
                most_common_char = Counter(chars_at_pos).most_common(1)[0][0]
                format_chars.append(most_common_char)
                regex_chars.append(re.escape(most_common_char))

        format_pattern = "".join(format_chars)
        regex_pattern = "".join(regex_chars)

        # Collapse consecutive identical placeholders for readability.
        # e.g., "XXXX-XXXX" stays, but "XXXXXXXXXX" is already fine.
        if not format_pattern:
            return None, None

        self._logger.debug(
            "inferred_pattern",
            dominant_length=dominant_length,
            group_fraction=round(len(dominant_group) / total, 4),
            format_pattern=format_pattern,
        )
        return format_pattern, regex_pattern

    # ------------------------------------------------------------------
    # Private helpers — string length analysis
    # ------------------------------------------------------------------

    def _analyze_string_lengths(
        self,
        str_values: list[str],
    ) -> dict[str, Union[float, int]]:
        """Compute string-length statistics for column values.

        Args:
            str_values: Non-null string column values.

        Returns:
            A dictionary with ``average_length``, ``min_length``, and
            ``max_length`` keys.
        """
        if not str_values:
            return {"average_length": 0.0, "min_length": 0, "max_length": 0}

        lengths = [len(v) for v in str_values]
        return {
            "average_length": float(np.mean(lengths)),
            "min_length": min(lengths),
            "max_length": max(lengths),
        }

    # ------------------------------------------------------------------
    # Private helpers — prefix / suffix detection
    # ------------------------------------------------------------------

    def _detect_common_prefixes(
        self,
        str_values: list[str],
        max_count: int = 5,
    ) -> list[str]:
        """Detect the most common string prefixes in column values.

        Extracts the first *N* characters (controlled by
        ``_max_prefix_suffix_length``) of each value, counts occurrences,
        and returns those exceeding a 5 % relevance threshold.

        Args:
            str_values: Non-null string column values.
            max_count: Maximum number of prefixes to return.

        Returns:
            A list of the most common prefix strings (up to *max_count*).
        """
        if not str_values:
            return []

        prefix_len = self._max_prefix_suffix_length
        threshold = max(1, int(len(str_values) * _PREFIX_SUFFIX_RELEVANCE_THRESHOLD))

        prefix_counter: Counter[str] = Counter()
        for val in str_values:
            if len(val) >= prefix_len:
                prefix_counter[val[:prefix_len]] += 1

        # Also try shorter prefixes for additional coverage.
        for shorter in range(max(1, prefix_len - 2), prefix_len):
            for val in str_values:
                if len(val) >= shorter:
                    prefix_counter[val[:shorter]] += 1

        return [
            prefix
            for prefix, count in prefix_counter.most_common(max_count * 3)
            if count >= threshold and len(prefix) == prefix_len
        ][:max_count]

    def _detect_common_suffixes(
        self,
        str_values: list[str],
        max_count: int = 5,
    ) -> list[str]:
        """Detect the most common string suffixes in column values.

        Extracts the last *N* characters of each value, counts
        occurrences, and returns those exceeding the relevance threshold.

        Args:
            str_values: Non-null string column values.
            max_count: Maximum number of suffixes to return.

        Returns:
            A list of the most common suffix strings (up to *max_count*).
        """
        if not str_values:
            return []

        suffix_len = self._max_prefix_suffix_length
        threshold = max(1, int(len(str_values) * _PREFIX_SUFFIX_RELEVANCE_THRESHOLD))

        suffix_counter: Counter[str] = Counter()
        for val in str_values:
            if len(val) >= suffix_len:
                suffix_counter[val[-suffix_len:]] += 1

        # Also try shorter suffixes for additional coverage.
        for shorter in range(max(1, suffix_len - 2), suffix_len):
            for val in str_values:
                if len(val) >= shorter:
                    suffix_counter[val[-shorter:]] += 1

        return [
            suffix
            for suffix, count in suffix_counter.most_common(max_count * 3)
            if count >= threshold and len(suffix) == suffix_len
        ][:max_count]

    # ------------------------------------------------------------------
    # Private helpers — anonymised sample format generation
    # ------------------------------------------------------------------

    def _generate_sample_formats(
        self,
        str_values: list[str],
        count: int = 5,
    ) -> list[str]:
        """Generate anonymised format exemplars from column values.

        **CRITICAL for Constraint C-001:** This method NEVER returns
        actual production data values.  Instead, it replaces every digit
        with ``X``, every letter with ``A``, and preserves only
        structural delimiter characters (hyphens, dots, slashes, etc.).

        Example::

            'INV-2024-0001'  →  'AAA-XXXX-XXXX'
            'john.doe@acme.com'  →  'AAAA.AAA@AAAA.AAA'

        Args:
            str_values: Non-null string column values.
            count: Maximum number of unique anonymised samples to return.

        Returns:
            A deduplicated list of anonymised format exemplar strings.
        """
        if not str_values:
            return []

        seen: set[str] = set()
        samples: list[str] = []

        for val in str_values:
            anonymised = self._anonymise_value(val)
            if anonymised not in seen:
                seen.add(anonymised)
                samples.append(anonymised)
                if len(samples) >= count:
                    break

        return samples

    @staticmethod
    def _anonymise_value(value: str) -> str:
        """Replace letters with ``A`` and digits with ``X`` in *value*.

        Preserves structural delimiter characters (hyphens, dots, slashes,
        underscores, spaces, at-signs, parentheses, etc.) so that the
        output conveys the format without revealing actual data.

        Args:
            value: A single string value.

        Returns:
            An anonymised format string.
        """
        result: list[str] = []
        for ch in value:
            if ch in _ALPHA_SET:
                result.append("A")
            elif ch in _DIGIT_SET:
                result.append("X")
            else:
                # Keep structural delimiter characters as-is.
                result.append(ch)
        return "".join(result)

    # ------------------------------------------------------------------
    # Private helpers — character class analysis
    # ------------------------------------------------------------------

    def _analyze_character_classes(
        self,
        str_values: list[str],
    ) -> dict[str, float]:
        """Compute character-class distribution across column values.

        Counts how many characters fall into each of four classes—alpha,
        digit, special, whitespace—and returns percentage distributions.

        Args:
            str_values: Non-null string column values.

        Returns:
            A dictionary mapping class names to their fraction of total
            characters, e.g. ``{'alpha': 0.45, 'digit': 0.35, ...}``.
        """
        if not str_values:
            return {
                "alpha": 0.0,
                "digit": 0.0,
                "special": 0.0,
                "whitespace": 0.0,
            }

        alpha_count = 0
        digit_count = 0
        special_count = 0
        whitespace_count = 0
        total_chars = 0

        for val in str_values:
            for ch in val:
                total_chars += 1
                if ch in _ALPHA_SET:
                    alpha_count += 1
                elif ch in _DIGIT_SET:
                    digit_count += 1
                elif ch in _WHITESPACE_SET:
                    whitespace_count += 1
                elif ch in _SPECIAL_SET:
                    special_count += 1
                else:
                    # Characters not in any defined class (e.g. Unicode)
                    # are counted toward the total but not classified.
                    pass

        if total_chars == 0:
            return {
                "alpha": 0.0,
                "digit": 0.0,
                "special": 0.0,
                "whitespace": 0.0,
            }

        return {
            "alpha": round(alpha_count / total_chars, 4),
            "digit": round(digit_count / total_chars, 4),
            "special": round(special_count / total_chars, 4),
            "whitespace": round(whitespace_count / total_chars, 4),
        }

    # ------------------------------------------------------------------
    # Private helpers — value frequency analysis
    # ------------------------------------------------------------------

    def _compute_value_frequency(
        self,
        values: list[Any],
        max_top: int = 10,
    ) -> list[dict[str, Any]]:
        """Compute value frequency distribution for a column.

        Returns the *max_top* most frequent values with counts and
        percentages.  For **Constraint C-001** compliance, this method
        only returns frequency metadata—callers must decide whether the
        values themselves are safe to expose (e.g., categorical codes are
        acceptable, but free-text fields may contain PII).

        Args:
            values: Raw column values (nulls are excluded before counting).
            max_top: Maximum number of top values to return.

        Returns:
            A list of dictionaries, each containing ``value``, ``count``,
            and ``percentage`` keys, sorted by descending count.
        """
        if not values:
            return []

        # Filter nulls.
        series = pd.Series(values)
        non_null = series[~pd.isna(series)]
        total: int = len(non_null)

        if total == 0:
            return []

        counter: Counter[Any] = Counter(non_null.tolist())
        result: list[dict[str, Any]] = []

        for val, cnt in counter.most_common(max_top):
            result.append(
                {
                    "value": val,
                    "count": cnt,
                    "percentage": round(cnt / total, 6),
                }
            )

        return result
