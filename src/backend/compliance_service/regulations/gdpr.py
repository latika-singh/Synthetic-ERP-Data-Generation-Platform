"""GDPR (General Data Protection Regulation) compliance rules and verification.

This module implements the GDPR regulatory compliance checker for the Synthetic
ERP Data Generation Platform.  It validates synthetic datasets against key GDPR
requirements to guarantee zero PII leakage and ensure regulatory compliance for
EU data privacy.

GDPR Articles Covered:
    - **Article 4** — Personal data categories: name, identification number,
      location data, online identifier, genetic/biometric data, health data,
      economic data, and social identity.
    - **Article 9** — Special categories of data requiring explicit consent:
      racial/ethnic origin, political opinions, religious/philosophical beliefs,
      trade union membership, genetic data, biometric data, health data, and
      sex life/sexual orientation.
    - **Article 5(1)(c)** — Data minimization principle: only data necessary
      for the stated purpose should be generated.
    - **Article 17** — Right to erasure: generated data must support deletion
      capabilities with defined retention periods.
    - **Article 20** — Data portability: generated data must be available in
      machine-readable, portable formats (CSV, JSON, Parquet).

Compliance State Machine Integration:
    This checker is invoked during the ``PIICheck`` state transition in the
    compliance verification workflow:
    Pending → Scanning → **PIICheck** → Certified → Released.

Usage::

    from compliance_service.regulations.gdpr import GDPRRegulationChecker

    checker = GDPRRegulationChecker()
    result = checker.check_compliance(dataset_metadata, scan_results)
    if not result.is_compliant:
        for violation in result.violations:
            print(f"{violation.severity.value}: {violation.description}")
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Dict, List, Optional

from shared.logging.structured_logger import get_logger

# The __init__.py imports this module for auto-registration *after* it has
# defined the base symbols.  Python's partial-module caching guarantees that
# BaseRegulationChecker et al. are already available when this import runs.
from compliance_service.regulations import (
    BaseRegulationChecker,
    ComplianceResult,
    ComplianceViolation,
    RegulationType,
    Severity,
)

# ---------------------------------------------------------------------------
# Module-level logger
# ---------------------------------------------------------------------------

logger = get_logger(__name__)

# ---------------------------------------------------------------------------
# GDPR Article Reference Enumeration
# ---------------------------------------------------------------------------


class GDPRArticle(Enum):
    """GDPR article references for type-safe compliance check classification.

    Each value corresponds to a specific GDPR article that the compliance
    checker verifies against.  Used in violation records for clear
    regulatory traceability.

    Attributes:
        ART_4: Definition of personal data (Art. 4 GDPR).
        ART_5_1_C: Data minimization principle (Art. 5(1)(c) GDPR).
        ART_9: Special categories of personal data (Art. 9 GDPR).
        ART_17: Right to erasure / right to be forgotten (Art. 17 GDPR).
        ART_20: Right to data portability (Art. 20 GDPR).
    """

    ART_4 = "Art. 4 GDPR"
    ART_5_1_C = "Art. 5(1)(c) GDPR"
    ART_9 = "Art. 9 GDPR"
    ART_17 = "Art. 17 GDPR"
    ART_20 = "Art. 20 GDPR"


# ---------------------------------------------------------------------------
# GDPR Personal Data Categories (Art. 4)
# ---------------------------------------------------------------------------

GDPR_PERSONAL_DATA_CATEGORIES: Dict[str, List[str]] = {
    "name": [
        "first_name",
        "last_name",
        "full_name",
        "given_name",
        "surname",
        "maiden_name",
        "middle_name",
        "display_name",
        "person_name",
        "employee_name",
        "customer_name",
        "contact_name",
    ],
    "identification_number": [
        "national_id",
        "passport_number",
        "ssn",
        "social_security",
        "tax_id",
        "tax_number",
        "driver_license",
        "drivers_licence",
        "identity_card",
        "personal_id",
        "id_number",
        "citizen_id",
        "residence_permit",
        "visa_number",
    ],
    "location_data": [
        "address",
        "street_address",
        "city",
        "state",
        "zip_code",
        "postal_code",
        "country",
        "latitude",
        "longitude",
        "gps",
        "geo_location",
        "geolocation",
        "coordinates",
        "region",
        "province",
        "district",
        "home_address",
        "work_address",
        "mailing_address",
        "billing_address",
        "shipping_address",
    ],
    "online_identifier": [
        "ip_address",
        "ip_addr",
        "cookie_id",
        "device_fingerprint",
        "imei",
        "mac_address",
        "device_id",
        "browser_fingerprint",
        "advertising_id",
        "session_id",
        "tracking_id",
        "user_agent",
        "login_id",
        "username",
        "email",
        "email_address",
    ],
    "genetic_data": [
        "dna",
        "genome",
        "genetic_marker",
        "genetic_profile",
        "dna_sequence",
        "genotype",
        "allele",
        "chromosome",
        "genetic_test",
        "genetic_result",
    ],
    "biometric_data": [
        "fingerprint",
        "facial_recognition",
        "retina_scan",
        "voice_print",
        "iris_scan",
        "palm_print",
        "gait_pattern",
        "biometric_template",
        "biometric_id",
        "face_encoding",
        "voiceprint",
        "keystroke_dynamics",
    ],
    "health_data": [
        "medical_record",
        "diagnosis",
        "prescription",
        "blood_type",
        "health_condition",
        "medical_history",
        "treatment",
        "medication",
        "allergy",
        "disability",
        "mental_health",
        "vaccination",
        "lab_result",
        "vital_sign",
        "bmi",
        "health_insurance",
        "patient_id",
        "medical_id",
    ],
    "economic_data": [
        "salary",
        "bank_account",
        "credit_score",
        "income",
        "revenue",
        "compensation",
        "bonus",
        "wage",
        "account_number",
        "iban",
        "swift_code",
        "routing_number",
        "credit_card",
        "debit_card",
        "financial_account",
        "investment",
        "pension",
        "net_worth",
    ],
    "social_identity": [
        "ethnic_origin",
        "ethnicity",
        "race",
        "religion",
        "religious_belief",
        "political_opinion",
        "political_party",
        "political_affiliation",
        "sexual_orientation",
        "gender_identity",
        "marital_status",
        "nationality",
        "citizenship",
    ],
}


# ---------------------------------------------------------------------------
# GDPR Special Categories of Data (Art. 9)
# ---------------------------------------------------------------------------

GDPR_SPECIAL_CATEGORIES: List[str] = [
    "racial_ethnic_origin",
    "political_opinions",
    "religious_philosophical_beliefs",
    "trade_union_membership",
    "genetic_data",
    "biometric_data",
    "health_data",
    "sex_life_sexual_orientation",
]

# Internal mapping from special category identifiers to detection keywords
# used for cross-referencing PII scan results with Art. 9 categories.
_SPECIAL_CATEGORY_KEYWORDS: Dict[str, List[str]] = {
    "racial_ethnic_origin": [
        "race",
        "ethnicity",
        "ethnic_origin",
        "racial_origin",
        "ethnic_group",
        "national_origin",
    ],
    "political_opinions": [
        "political_opinion",
        "political_party",
        "political_affiliation",
        "political_belief",
        "political_view",
        "party_membership",
    ],
    "religious_philosophical_beliefs": [
        "religion",
        "religious_belief",
        "faith",
        "denomination",
        "philosophical_belief",
        "spiritual",
    ],
    "trade_union_membership": [
        "trade_union",
        "union_membership",
        "labor_union",
        "union_id",
        "union_member",
        "collective_bargaining",
    ],
    "genetic_data": [
        "dna",
        "genome",
        "genetic_marker",
        "genetic_profile",
        "dna_sequence",
        "genotype",
        "allele",
        "genetic_test",
    ],
    "biometric_data": [
        "fingerprint",
        "facial_recognition",
        "retina_scan",
        "voice_print",
        "iris_scan",
        "biometric_template",
        "biometric_id",
        "face_encoding",
        "palm_print",
    ],
    "health_data": [
        "medical_record",
        "diagnosis",
        "prescription",
        "blood_type",
        "health_condition",
        "treatment",
        "medication",
        "allergy",
        "disability",
        "vaccination",
        "patient_id",
        "health_insurance",
    ],
    "sex_life_sexual_orientation": [
        "sexual_orientation",
        "sex_life",
        "gender_identity",
        "sexual_preference",
    ],
}


# ---------------------------------------------------------------------------
# GDPR Data Minimization Rules (Art. 5(1)(c))
# ---------------------------------------------------------------------------

GDPR_DATA_MINIMIZATION_RULES: Dict[str, Dict[str, Any]] = {
    "personal_identifiers": {
        "max_retention_days": 90,
        "requires_purpose": True,
        "description": "Direct personal identifiers (names, IDs) in synthetic data",
    },
    "location_data": {
        "max_retention_days": 180,
        "requires_purpose": True,
        "description": "Geographic and location-based data fields",
    },
    "financial_data": {
        "max_retention_days": 365,
        "requires_purpose": True,
        "description": "Economic and financial data fields",
    },
    "health_data": {
        "max_retention_days": 30,
        "requires_purpose": True,
        "description": "Health and medical data fields (Art. 9 special category)",
    },
    "online_identifiers": {
        "max_retention_days": 30,
        "requires_purpose": True,
        "description": "IP addresses, device IDs, cookies, and tracking identifiers",
    },
    "biometric_data": {
        "max_retention_days": 30,
        "requires_purpose": True,
        "description": "Biometric templates and biometric identifiers",
    },
    "genetic_data": {
        "max_retention_days": 30,
        "requires_purpose": True,
        "description": "Genetic markers, DNA sequences, and genotype data",
    },
    "social_identity": {
        "max_retention_days": 90,
        "requires_purpose": True,
        "description": "Ethnic origin, religion, political opinion, and similar",
    },
    "general_data": {
        "max_retention_days": 365,
        "requires_purpose": False,
        "description": "Non-personal general business data fields",
    },
}

# Portable formats accepted under Art. 20 data portability.
_PORTABLE_FORMATS: frozenset[str] = frozenset(
    {"csv", "json", "jsonl", "parquet", "xml", "tsv"}
)

# Maximum reasonable retention period in days (7 years per SOC 2 alignment).
_MAX_RETENTION_DAYS: int = 2555


# ---------------------------------------------------------------------------
# Internal Data Structure
# ---------------------------------------------------------------------------


@dataclass
class _PersonalDataMatch:
    """Internal container for a matched personal data category.

    Attributes:
        category: GDPR personal data category (e.g. ``'name'``).
        matched_pattern: The specific keyword that triggered the match.
        column_name: Dataset column that matched.
    """

    category: str
    matched_pattern: str
    column_name: str = field(default="")


# ---------------------------------------------------------------------------
# Pre-compiled Regex Pattern Builders
# ---------------------------------------------------------------------------


def _build_category_patterns(
    categories: Dict[str, List[str]],
) -> Dict[str, re.Pattern[str]]:
    """Compile regex patterns from personal data category keyword lists.

    For each category, builds a single compiled regex that matches any of
    the keywords.  Patterns are case-insensitive to handle varied column
    naming conventions across ERP systems.

    Args:
        categories: Mapping of category name to keyword lists.

    Returns:
        Mapping of category name to compiled ``re.Pattern``.
    """
    compiled: Dict[str, re.Pattern[str]] = {}
    for category, keywords in categories.items():
        escaped = [re.escape(kw) for kw in keywords]
        pattern_str = "|".join(escaped)
        compiled[category] = re.compile(pattern_str, re.IGNORECASE)
    return compiled


def _build_special_category_patterns(
    keywords: Dict[str, List[str]],
) -> Dict[str, re.Pattern[str]]:
    """Compile regex patterns for Art. 9 special category detection.

    Args:
        keywords: Mapping of special category to keyword lists.

    Returns:
        Mapping of special category to compiled ``re.Pattern``.
    """
    compiled: Dict[str, re.Pattern[str]] = {}
    for category, kw_list in keywords.items():
        escaped = [re.escape(kw) for kw in kw_list]
        pattern_str = "|".join(escaped)
        compiled[category] = re.compile(pattern_str, re.IGNORECASE)
    return compiled


# Module-level pre-compiled patterns (computed once at import time).
_PERSONAL_DATA_PATTERNS: Dict[str, re.Pattern[str]] = _build_category_patterns(
    GDPR_PERSONAL_DATA_CATEGORIES
)
_SPECIAL_CATEGORY_PATTERNS: Dict[str, re.Pattern[str]] = (
    _build_special_category_patterns(_SPECIAL_CATEGORY_KEYWORDS)
)


# ---------------------------------------------------------------------------
# Module-Level Helper Functions
# ---------------------------------------------------------------------------


def _match_column_to_personal_data(
    column_name: str,
    column_type: str,
) -> Optional[str]:
    """Map a dataset column to a GDPR personal data category.

    Checks the column name (and optionally its data type) against the
    pre-compiled personal data category patterns derived from Art. 4.

    Args:
        column_name: The name of the dataset column to evaluate.
        column_type: The data type of the column (e.g. ``'VARCHAR'``,
            ``'INTEGER'``).  Used as a secondary matching signal.

    Returns:
        The GDPR personal data category name (e.g. ``'name'``,
        ``'health_data'``) if a match is found, or ``None`` if the
        column does not match any personal data category.
    """
    normalised_name = column_name.lower().strip()
    normalised_type = column_type.lower().strip() if column_type else ""

    for category, pattern in _PERSONAL_DATA_PATTERNS.items():
        # Primary: attempt exact match on the full column name first
        if pattern.match(normalised_name):
            return category
        # Secondary: search for keyword within a longer column name
        if pattern.search(normalised_name):
            return category
        # Tertiary: column type may hint at personal data in some ERP schemas
        if normalised_type and pattern.search(normalised_type):
            return category

    return None


def _is_special_category(pii_type: str) -> bool:
    """Determine if a PII type falls under Art. 9 special categories.

    Art. 9 of the GDPR defines special categories of personal data that
    are subject to stricter processing conditions.  This function checks
    whether a detected PII type corresponds to one of these categories.

    Args:
        pii_type: The PII type string to check (e.g. ``'health_data'``,
            ``'political_opinions'``, ``'email'``).

    Returns:
        ``True`` if the PII type is a GDPR Art. 9 special category,
        ``False`` otherwise.
    """
    normalised = pii_type.lower().strip()

    # Direct match against special category identifiers.
    if normalised in GDPR_SPECIAL_CATEGORIES:
        return True

    # Check against special category keyword patterns.
    for _category, pattern in _SPECIAL_CATEGORY_PATTERNS.items():
        if pattern.search(normalised):
            return True

    return False


# ---------------------------------------------------------------------------
# GDPRRegulationChecker
# ---------------------------------------------------------------------------


class GDPRRegulationChecker(BaseRegulationChecker):
    """GDPR compliance verification checker for synthetic datasets.

    Validates generated datasets against key GDPR requirements to ensure
    zero PII leakage and regulatory compliance for EU data privacy.  This
    checker is registered with the ``RegulationRegistry`` and invoked
    during the compliance verification workflow.

    The checker performs five categories of compliance verification:

    1. **Art. 4 — Personal Data Categories**: Detects personal data fields
       (names, IDs, locations, online identifiers, genetic/biometric/health
       data, economic data, social identity) and verifies they are properly
       anonymised in synthetic output.

    2. **Art. 9 — Special Categories**: Identifies special-category data
       (racial origin, political opinions, religious beliefs, health data,
       genetic/biometric data, sexual orientation, trade union membership)
       and requires explicit purpose justification.

    3. **Art. 5(1)(c) — Data Minimization**: Ensures only necessary fields
       are included in the generated dataset and each field has a declared
       purpose.

    4. **Art. 17 — Right to Erasure**: Verifies that generated data
       supports deletion capabilities with defined retention periods.

    5. **Art. 20 — Data Portability**: Confirms generated data is available
       in machine-readable, portable formats (CSV, JSON, Parquet).

    Attributes:
        regulation_type: Always ``RegulationType.GDPR``.
        personal_data_patterns: Pre-compiled regex patterns for Art. 4.
        special_category_patterns: Pre-compiled regex patterns for Art. 9.

    Example::

        checker = GDPRRegulationChecker()
        result = checker.check_compliance(
            dataset_metadata={
                "columns": [...],
                "generation_profile": {...},
            },
            scan_results={
                "detected_pii": [...],
                "field_pii_mapping": {...},
            },
        )
        assert result.is_compliant
    """

    def __init__(self) -> None:
        """Initialise the GDPR regulation checker.

        Sets up the structured logger and references the module-level
        pre-compiled regex patterns for Art. 4 personal data categories
        and Art. 9 special categories.
        """
        self._logger = get_logger(__name__)
        self.personal_data_patterns: Dict[str, re.Pattern[str]] = (
            _PERSONAL_DATA_PATTERNS
        )
        self.special_category_patterns: Dict[str, re.Pattern[str]] = (
            _SPECIAL_CATEGORY_PATTERNS
        )

    # ------------------------------------------------------------------
    # Abstract property implementation
    # ------------------------------------------------------------------

    @property
    def regulation_type(self) -> RegulationType:
        """Return the regulation type identifier for GDPR.

        Returns:
            ``RegulationType.GDPR``.
        """
        return RegulationType.GDPR

    # ------------------------------------------------------------------
    # Core compliance check (abstract method implementation)
    # ------------------------------------------------------------------

    def check_compliance(
        self,
        dataset_metadata: Dict[str, Any],
        scan_results: Dict[str, Any],
    ) -> ComplianceResult:
        """Execute the full GDPR compliance check for a synthetic dataset.

        Runs all five GDPR verification checks and aggregates the results
        into a single ``ComplianceResult``.  The dataset is considered
        compliant only if **all** checks pass (zero violations).

        Args:
            dataset_metadata: Schema information including:
                - ``columns`` (list[dict]): Column definitions with ``name``
                  and ``type`` keys.
                - ``generation_profile`` (dict): Configuration including
                  ``purpose``, ``field_purposes``, ``retention_period_days``,
                  ``export_formats``, ``erasure_supported``, and
                  ``special_category_justifications``.
                - ``record_count`` (int): Number of records generated.
            scan_results: PII detection results from the PII detector:
                - ``detected_pii`` (list[dict]): Detected PII entities with
                  ``type``, ``field``, ``confidence`` keys.
                - ``field_pii_mapping`` (dict[str, list[str]]): Mapping of
                  field names to detected PII types.
                - ``pii_fields_count`` (int): Total fields with PII detected.

        Returns:
            A ``ComplianceResult`` containing:
                - ``is_compliant``: ``True`` only if all checks pass.
                - ``compliance_score``: Weighted score in ``[0.0, 1.0]``.
                - ``violations``: Detailed list of all violations found.
        """
        self._logger.info(
            "gdpr_compliance_check_started",
            regulation_type="GDPR",
            dataset_columns=len(dataset_metadata.get("columns", [])),
            pii_fields_detected=scan_results.get("pii_fields_count", 0),
        )

        columns: List[Dict[str, Any]] = dataset_metadata.get("columns", [])
        generation_profile: Dict[str, Any] = dataset_metadata.get(
            "generation_profile", {}
        )

        # Enrich scan_results with generation_profile so that
        # check_special_categories can access justification data.
        enriched_scan: Dict[str, Any] = {**scan_results}
        if "generation_profile" not in enriched_scan:
            enriched_scan["generation_profile"] = generation_profile

        all_violations: List[ComplianceViolation] = []

        # 1. Art. 4 — Personal data categories
        art4_violations = self.check_personal_data_categories(
            columns, scan_results
        )
        all_violations.extend(art4_violations)
        self._logger.info(
            "gdpr_art4_check_complete",
            regulation_type="GDPR",
            article_reference=GDPRArticle.ART_4.value,
            violation_count=len(art4_violations),
        )

        # 2. Art. 9 — Special categories of data
        art9_violations = self.check_special_categories(
            columns, enriched_scan
        )
        all_violations.extend(art9_violations)
        self._logger.info(
            "gdpr_art9_check_complete",
            regulation_type="GDPR",
            article_reference=GDPRArticle.ART_9.value,
            violation_count=len(art9_violations),
        )

        # 3. Art. 5(1)(c) — Data minimization
        art5_violations = self.check_data_minimization(dataset_metadata)
        all_violations.extend(art5_violations)
        self._logger.info(
            "gdpr_art5_check_complete",
            regulation_type="GDPR",
            article_reference=GDPRArticle.ART_5_1_C.value,
            violation_count=len(art5_violations),
        )

        # 4. Art. 17 — Right to erasure
        art17_violations = self.check_right_to_erasure(dataset_metadata)
        all_violations.extend(art17_violations)
        self._logger.info(
            "gdpr_art17_check_complete",
            regulation_type="GDPR",
            article_reference=GDPRArticle.ART_17.value,
            violation_count=len(art17_violations),
        )

        # 5. Art. 20 — Data portability
        art20_violations = self.check_data_portability(dataset_metadata)
        all_violations.extend(art20_violations)
        self._logger.info(
            "gdpr_art20_check_complete",
            regulation_type="GDPR",
            article_reference=GDPRArticle.ART_20.value,
            violation_count=len(art20_violations),
        )

        # Aggregate final result using base class helpers
        is_compliant = len(all_violations) == 0
        total_fields = len(columns)

        # Use inherited _calculate_compliance_score for explicit scoring
        compliance_score = self._calculate_compliance_score(
            all_violations, total_fields
        )

        result = self._create_result(
            is_compliant=is_compliant,
            violations=all_violations,
            total_fields=total_fields,
            metadata={
                "regulation": "GDPR",
                "articles_checked": [
                    GDPRArticle.ART_4.value,
                    GDPRArticle.ART_9.value,
                    GDPRArticle.ART_5_1_C.value,
                    GDPRArticle.ART_17.value,
                    GDPRArticle.ART_20.value,
                ],
                "total_violations": len(all_violations),
                "critical_violations": sum(
                    1
                    for v in all_violations
                    if v.severity == Severity.CRITICAL
                ),
                "high_violations": sum(
                    1
                    for v in all_violations
                    if v.severity == Severity.HIGH
                ),
                "medium_violations": sum(
                    1
                    for v in all_violations
                    if v.severity == Severity.MEDIUM
                ),
            },
        )

        self._logger.info(
            "gdpr_compliance_check_complete",
            regulation_type="GDPR",
            is_compliant=is_compliant,
            compliance_score=compliance_score,
            violation_count=len(all_violations),
            total_fields_checked=total_fields,
        )

        return result

    # ------------------------------------------------------------------
    # Art. 4 — Personal data categories
    # ------------------------------------------------------------------

    def check_personal_data_categories(
        self,
        columns: List[Dict[str, Any]],
        scan_results: Dict[str, Any],
    ) -> List[ComplianceViolation]:
        """Check for personal data categories per GDPR Art. 4.

        Iterates through all dataset columns and cross-references them
        with PII scan results.  If a column matches an Art. 4 personal
        data category **and** PII-like patterns were detected in the
        generated synthetic data for that column, a compliance violation
        is raised.

        For synthetic data to be compliant, personal data columns must
        contain only anonymised values that cannot be linked to real
        individuals.

        Args:
            columns: List of column definitions, each containing at
                minimum ``name`` (str) and ``type`` (str) keys.
            scan_results: PII detection results containing:
                - ``field_pii_mapping`` (dict[str, list[str]]): Fields
                  to PII types mapping.
                - ``detected_pii`` (list[dict]): Individual detections.

        Returns:
            List of ``ComplianceViolation`` instances for each column
            where PII was detected in a personal data category field.
            Severity is ``Severity.HIGH``, article ``'Art. 4 GDPR'``.
        """
        violations: List[ComplianceViolation] = []
        field_pii_mapping: Dict[str, List[str]] = scan_results.get(
            "field_pii_mapping", {}
        )

        for column in columns:
            column_name: str = column.get("name", "")
            column_type: str = column.get("type", "")

            if not column_name:
                continue

            # Check if this column matches a GDPR personal data category
            matched_category = _match_column_to_personal_data(
                column_name, column_type
            )

            if matched_category is None:
                continue

            # Check if PII was actually detected in this field
            detected_pii_types: List[str] = field_pii_mapping.get(
                column_name, []
            )

            if detected_pii_types:
                violations.append(
                    ComplianceViolation(
                        regulation_type=RegulationType.GDPR,
                        severity=Severity.HIGH,
                        article_reference=GDPRArticle.ART_4.value,
                        description=(
                            f"Personal data detected in column '{column_name}' "
                            f"(category: {matched_category}). Detected PII "
                            f"types: {', '.join(detected_pii_types)}. Synthetic "
                            f"data in personal data fields must be fully "
                            f"anonymised to prevent re-identification."
                        ),
                        affected_fields=[column_name],
                        remediation=(
                            f"Apply stronger anonymisation or masking to "
                            f"column '{column_name}'. Consider using "
                            f"differential privacy techniques or complete "
                            f"synthetic replacement for '{matched_category}' "
                            f"category data. Ensure generated values cannot "
                            f"be linked back to real individuals."
                        ),
                        metadata={
                            "gdpr_category": matched_category,
                            "detected_pii_types": detected_pii_types,
                            "column_type": column_type,
                        },
                    )
                )

                self._logger.warning(
                    "gdpr_personal_data_violation",
                    regulation_type="GDPR",
                    article_reference=GDPRArticle.ART_4.value,
                    column_name=column_name,
                    category=matched_category,
                    pii_types=detected_pii_types,
                )

        return violations

    # ------------------------------------------------------------------
    # Art. 9 — Special categories of data
    # ------------------------------------------------------------------

    def check_special_categories(
        self,
        columns: List[Dict[str, Any]],
        scan_results: Dict[str, Any],
    ) -> List[ComplianceViolation]:
        """Check for special category data per GDPR Art. 9.

        Art. 9 special categories (racial/ethnic origin, political opinions,
        religious beliefs, trade union membership, genetic data, biometric
        data, health data, sexual orientation) require **explicit** purpose
        justification in the generation profile before synthetic data
        containing these categories can be generated.

        A CRITICAL-severity violation is raised if:
          - A special-category column is present in the dataset, AND
          - No explicit justification is provided in the generation profile.

        Args:
            columns: List of column definitions with ``name`` and ``type``.
            scan_results: PII detection results with:
                - ``field_pii_mapping`` (dict[str, list[str]]): Field to
                  PII types mapping.
                - ``detected_pii`` (list[dict]): Individual detections.
                - ``generation_profile`` (dict, optional): May contain
                  ``special_category_justifications``.

        Returns:
            List of ``ComplianceViolation`` instances with
            ``severity=Severity.CRITICAL`` for unjustified special
            categories.
        """
        violations: List[ComplianceViolation] = []
        field_pii_mapping: Dict[str, List[str]] = scan_results.get(
            "field_pii_mapping", {}
        )

        # Retrieve generation profile from scan_results (injected by
        # check_compliance) or fall back to empty dict.
        generation_profile: Dict[str, Any] = scan_results.get(
            "generation_profile", {}
        )
        justifications: Dict[str, str] = generation_profile.get(
            "special_category_justifications", {}
        )

        for column in columns:
            column_name: str = column.get("name", "")
            column_type: str = column.get("type", "")

            if not column_name:
                continue

            # Determine which special category (if any) this column maps to
            matched_special_category: Optional[str] = None

            for category, pattern in self.special_category_patterns.items():
                if pattern.search(column_name.lower()):
                    matched_special_category = category
                    break

            # Also check if any detected PII for this column is special
            if matched_special_category is None:
                column_pii_types = field_pii_mapping.get(column_name, [])
                for pii_type in column_pii_types:
                    if _is_special_category(pii_type):
                        matched_special_category = pii_type
                        break

            if matched_special_category is None:
                continue

            # Special category column found — verify justification exists
            has_justification = (
                matched_special_category in justifications
                and bool(justifications[matched_special_category])
            )

            if not has_justification:
                violations.append(
                    ComplianceViolation(
                        regulation_type=RegulationType.GDPR,
                        severity=Severity.CRITICAL,
                        article_reference=GDPRArticle.ART_9.value,
                        description=(
                            f"Special category data "
                            f"'{matched_special_category}' detected in "
                            f"column '{column_name}' without explicit "
                            f"purpose justification. Art. 9 GDPR prohibits "
                            f"processing of special category data without "
                            f"a lawful basis and explicit consent."
                        ),
                        affected_fields=[column_name],
                        remediation=(
                            f"Provide explicit purpose justification for "
                            f"generating '{matched_special_category}' data "
                            f"in the generation profile's "
                            f"'special_category_justifications' config. "
                            f"Alternatively, exclude this column from the "
                            f"synthetic dataset if the data is not essential "
                            f"for the stated purpose."
                        ),
                        metadata={
                            "special_category": matched_special_category,
                            "column_type": column_type,
                            "has_justification": False,
                        },
                    )
                )

                self._logger.warning(
                    "gdpr_special_category_violation",
                    regulation_type="GDPR",
                    article_reference=GDPRArticle.ART_9.value,
                    column_name=column_name,
                    special_category=matched_special_category,
                    severity="CRITICAL",
                )

        return violations

    # ------------------------------------------------------------------
    # Art. 5(1)(c) — Data minimization
    # ------------------------------------------------------------------

    def check_data_minimization(
        self,
        dataset_metadata: Dict[str, Any],
    ) -> List[ComplianceViolation]:
        """Verify data minimization principles per GDPR Art. 5(1)(c).

        Ensures that only necessary fields are included in the generated
        dataset and that each personal-data field has a declared purpose.
        Also validates that synthetic data volume does not exceed the
        stated purpose.

        Checks performed:
        1. Overall purpose must be declared for the generation.
        2. Each personal data column must have a field-level purpose.
        3. Record count must not exceed stated maximum.

        Args:
            dataset_metadata: Dataset information containing ``columns``,
                ``generation_profile`` (with ``field_purposes``, ``purpose``,
                ``max_records``), and ``record_count``.

        Returns:
            List of ``ComplianceViolation`` instances with
            ``severity=Severity.MEDIUM`` for minimization violations.
        """
        violations: List[ComplianceViolation] = []
        columns: List[Dict[str, Any]] = dataset_metadata.get("columns", [])
        generation_profile: Dict[str, Any] = dataset_metadata.get(
            "generation_profile", {}
        )
        field_purposes: Dict[str, str] = generation_profile.get(
            "field_purposes", {}
        )
        overall_purpose: str = generation_profile.get("purpose", "")
        record_count: int = dataset_metadata.get("record_count", 0)
        max_records: int = generation_profile.get("max_records", 0)

        # Check 1: Overall purpose must be declared
        if not overall_purpose:
            violations.append(
                ComplianceViolation(
                    regulation_type=RegulationType.GDPR,
                    severity=Severity.MEDIUM,
                    article_reference=GDPRArticle.ART_5_1_C.value,
                    description=(
                        "No overall purpose declared for the synthetic "
                        "dataset generation. GDPR Art. 5(1)(c) requires "
                        "data to be adequate, relevant, and limited to "
                        "what is necessary for the stated purpose."
                    ),
                    affected_fields=[],
                    remediation=(
                        "Define a clear purpose statement in the generation "
                        "profile's 'purpose' field describing why this "
                        "synthetic dataset is being generated (e.g., 'QA "
                        "testing for invoice processing module')."
                    ),
                    metadata={"check_type": "overall_purpose"},
                )
            )

        # Check 2: Each personal data column needs a field-level purpose
        undeclared_fields: List[str] = []
        for column in columns:
            col_name: str = column.get("name", "")
            col_type: str = column.get("type", "")
            if not col_name:
                continue
            matched = _match_column_to_personal_data(col_name, col_type)
            if matched is not None and col_name not in field_purposes:
                undeclared_fields.append(col_name)

        if undeclared_fields:
            display_fields = undeclared_fields[:10]
            suffix = "..." if len(undeclared_fields) > 10 else ""
            violations.append(
                ComplianceViolation(
                    regulation_type=RegulationType.GDPR,
                    severity=Severity.MEDIUM,
                    article_reference=GDPRArticle.ART_5_1_C.value,
                    description=(
                        f"{len(undeclared_fields)} personal data field(s) "
                        f"lack declared purpose justification: "
                        f"{', '.join(display_fields)}{suffix}. Each "
                        f"personal data field must specify why it is "
                        f"included in the synthetic dataset."
                    ),
                    affected_fields=undeclared_fields,
                    remediation=(
                        "Add purpose declarations for each personal data "
                        "field in the generation profile's 'field_purposes' "
                        "mapping. Alternatively, remove unnecessary personal "
                        "data columns from the generation configuration."
                    ),
                    metadata={
                        "check_type": "field_purpose",
                        "undeclared_count": len(undeclared_fields),
                    },
                )
            )

        # Check 3: Record count must not exceed stated maximum
        if max_records > 0 and record_count > max_records:
            violations.append(
                ComplianceViolation(
                    regulation_type=RegulationType.GDPR,
                    severity=Severity.MEDIUM,
                    article_reference=GDPRArticle.ART_5_1_C.value,
                    description=(
                        f"Generated record count ({record_count:,}) exceeds "
                        f"the stated maximum ({max_records:,}) for the "
                        f"declared purpose. Data volume must be limited to "
                        f"what is necessary."
                    ),
                    affected_fields=[],
                    remediation=(
                        f"Reduce the generation volume to at most "
                        f"{max_records:,} records, or update the generation "
                        f"profile to justify the higher record count for "
                        f"the stated purpose."
                    ),
                    metadata={
                        "check_type": "volume_excess",
                        "record_count": record_count,
                        "max_records": max_records,
                    },
                )
            )

        return violations

    # ------------------------------------------------------------------
    # Art. 17 — Right to erasure
    # ------------------------------------------------------------------

    def check_right_to_erasure(
        self,
        dataset_metadata: Dict[str, Any],
    ) -> List[ComplianceViolation]:
        """Verify right to erasure compliance per GDPR Art. 17.

        Ensures that the generated synthetic dataset supports deletion
        capabilities and has a defined, reasonable retention period.

        Checks performed:
        1. Erasure capability must be enabled in the generation profile.
        2. A retention period must be defined.
        3. Retention period must not exceed 7 years (2555 days).
        4. Retention period must be a positive integer.

        Args:
            dataset_metadata: Dataset information containing
                ``generation_profile`` with ``erasure_supported`` (bool),
                ``retention_period_days`` (int), and optionally
                ``dataset_id`` (str).

        Returns:
            List of ``ComplianceViolation`` instances with
            ``severity=Severity.HIGH`` for erasure compliance violations.
        """
        violations: List[ComplianceViolation] = []
        generation_profile: Dict[str, Any] = dataset_metadata.get(
            "generation_profile", {}
        )
        erasure_supported: bool = generation_profile.get(
            "erasure_supported", False
        )
        retention_period_days: Optional[int] = generation_profile.get(
            "retention_period_days"
        )
        dataset_id: str = dataset_metadata.get("dataset_id", "unknown")

        # Check 1: Erasure capability must be enabled
        if not erasure_supported:
            violations.append(
                ComplianceViolation(
                    regulation_type=RegulationType.GDPR,
                    severity=Severity.HIGH,
                    article_reference=GDPRArticle.ART_17.value,
                    description=(
                        f"Dataset '{dataset_id}' does not support data "
                        f"erasure. GDPR Art. 17 requires the ability to "
                        f"erase personal data upon request ('right to be "
                        f"forgotten'). The generation profile must enable "
                        f"erasure support."
                    ),
                    affected_fields=[],
                    remediation=(
                        "Enable erasure support in the generation profile "
                        "by setting 'erasure_supported' to True. Implement "
                        "data lifecycle management that allows complete "
                        "deletion of generated datasets upon request."
                    ),
                    metadata={
                        "check_type": "erasure_capability",
                        "dataset_id": dataset_id,
                    },
                )
            )

        # Check 2: Retention period must be defined
        if retention_period_days is None:
            violations.append(
                ComplianceViolation(
                    regulation_type=RegulationType.GDPR,
                    severity=Severity.HIGH,
                    article_reference=GDPRArticle.ART_17.value,
                    description=(
                        f"No retention period defined for dataset "
                        f"'{dataset_id}'. GDPR Art. 17 requires clear "
                        f"data retention policies. Synthetic datasets "
                        f"containing personal data categories must specify "
                        f"how long data will be retained."
                    ),
                    affected_fields=[],
                    remediation=(
                        "Set 'retention_period_days' in the generation "
                        "profile to define how long the generated dataset "
                        "should be retained. Guidance: personal identifiers "
                        "≤90 days, health/biometric data ≤30 days, general "
                        "data ≤365 days."
                    ),
                    metadata={
                        "check_type": "retention_undefined",
                        "dataset_id": dataset_id,
                    },
                )
            )
        elif retention_period_days <= 0:
            violations.append(
                ComplianceViolation(
                    regulation_type=RegulationType.GDPR,
                    severity=Severity.HIGH,
                    article_reference=GDPRArticle.ART_17.value,
                    description=(
                        f"Invalid retention period "
                        f"({retention_period_days} days) for dataset "
                        f"'{dataset_id}'. Retention period must be a "
                        f"positive integer."
                    ),
                    affected_fields=[],
                    remediation=(
                        "Set a positive integer value for "
                        "'retention_period_days' in the generation profile."
                    ),
                    metadata={
                        "check_type": "retention_invalid",
                        "dataset_id": dataset_id,
                        "retention_period_days": retention_period_days,
                    },
                )
            )
        elif retention_period_days > _MAX_RETENTION_DAYS:
            violations.append(
                ComplianceViolation(
                    regulation_type=RegulationType.GDPR,
                    severity=Severity.HIGH,
                    article_reference=GDPRArticle.ART_17.value,
                    description=(
                        f"Retention period ({retention_period_days} days) "
                        f"for dataset '{dataset_id}' exceeds the maximum "
                        f"allowable period of {_MAX_RETENTION_DAYS} days "
                        f"(7 years). Excessive retention contradicts the "
                        f"right to erasure."
                    ),
                    affected_fields=[],
                    remediation=(
                        f"Reduce the retention period to at most "
                        f"{_MAX_RETENTION_DAYS} days. Consider whether "
                        f"the full retention duration is justified for "
                        f"the declared purpose."
                    ),
                    metadata={
                        "check_type": "retention_excessive",
                        "dataset_id": dataset_id,
                        "retention_period_days": retention_period_days,
                        "max_retention_days": _MAX_RETENTION_DAYS,
                    },
                )
            )

        return violations

    # ------------------------------------------------------------------
    # Art. 20 — Data portability
    # ------------------------------------------------------------------

    def check_data_portability(
        self,
        dataset_metadata: Dict[str, Any],
    ) -> List[ComplianceViolation]:
        """Verify data portability requirements per GDPR Art. 20.

        Ensures that the generated synthetic data supports machine-readable,
        portable export formats (CSV, JSON, Parquet) and that export
        capabilities are available.

        Checks performed:
        1. Export capability must be enabled.
        2. At least one portable export format must be configured.
        3. Configured formats must include a machine-readable option.

        Args:
            dataset_metadata: Dataset information containing
                ``generation_profile`` with ``export_formats`` (list[str])
                and ``export_enabled`` (bool).

        Returns:
            List of ``ComplianceViolation`` instances with
            ``severity=Severity.MEDIUM`` for portability violations.
        """
        violations: List[ComplianceViolation] = []
        generation_profile: Dict[str, Any] = dataset_metadata.get(
            "generation_profile", {}
        )
        export_formats: List[str] = generation_profile.get(
            "export_formats", []
        )
        export_enabled: bool = generation_profile.get("export_enabled", True)

        # Check 1: Export capability must be available
        if not export_enabled:
            violations.append(
                ComplianceViolation(
                    regulation_type=RegulationType.GDPR,
                    severity=Severity.MEDIUM,
                    article_reference=GDPRArticle.ART_20.value,
                    description=(
                        "Data export is disabled for this dataset. GDPR "
                        "Art. 20 requires that personal data be available "
                        "in a structured, commonly used, and "
                        "machine-readable format."
                    ),
                    affected_fields=[],
                    remediation=(
                        "Enable data export in the generation profile by "
                        "setting 'export_enabled' to True. Ensure at "
                        "least one portable format (CSV, JSON, or Parquet) "
                        "is configured."
                    ),
                    metadata={"check_type": "export_disabled"},
                )
            )

        # Check 2: At least one portable format must be configured
        if not export_formats:
            violations.append(
                ComplianceViolation(
                    regulation_type=RegulationType.GDPR,
                    severity=Severity.MEDIUM,
                    article_reference=GDPRArticle.ART_20.value,
                    description=(
                        "No export formats configured for this dataset. "
                        "At least one machine-readable, portable format "
                        "(CSV, JSON, or Parquet) must be available per "
                        "Art. 20 GDPR."
                    ),
                    affected_fields=[],
                    remediation=(
                        "Configure at least one portable export format "
                        "in the generation profile's 'export_formats' "
                        "list. Recommended: 'csv', 'json', 'parquet'."
                    ),
                    metadata={"check_type": "no_export_formats"},
                )
            )
        else:
            # Check 3: At least one format must be a portable format
            normalised_formats = {
                fmt.lower().strip() for fmt in export_formats
            }
            portable_available = normalised_formats & _PORTABLE_FORMATS

            if not portable_available:
                violations.append(
                    ComplianceViolation(
                        regulation_type=RegulationType.GDPR,
                        severity=Severity.MEDIUM,
                        article_reference=GDPRArticle.ART_20.value,
                        description=(
                            f"Configured export formats "
                            f"({', '.join(sorted(normalised_formats))}) "
                            f"do not include any machine-readable portable "
                            f"format. Art. 20 GDPR requires data in "
                            f"structured, commonly used formats."
                        ),
                        affected_fields=[],
                        remediation=(
                            "Add at least one of the following portable "
                            "formats to 'export_formats': "
                            f"{', '.join(sorted(_PORTABLE_FORMATS))}."
                        ),
                        metadata={
                            "check_type": "non_portable_formats",
                            "configured_formats": sorted(normalised_formats),
                            "accepted_portable_formats": sorted(
                                _PORTABLE_FORMATS
                            ),
                        },
                    )
                )

        return violations

    # ------------------------------------------------------------------
    # Regulation identity methods
    # ------------------------------------------------------------------

    def get_regulation_name(self) -> str:
        """Return the full human-readable name of the GDPR regulation.

        Returns:
            ``'GDPR - General Data Protection Regulation'``.
        """
        return "GDPR - General Data Protection Regulation"

    def get_regulation_version(self) -> str:
        """Return the official GDPR legal reference.

        Returns:
            ``'EU 2016/679'``.
        """
        return "EU 2016/679"


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

__all__ = [
    "GDPRRegulationChecker",
    "GDPR_PERSONAL_DATA_CATEGORIES",
    "GDPR_SPECIAL_CATEGORIES",
    "GDPR_DATA_MINIMIZATION_RULES",
]
