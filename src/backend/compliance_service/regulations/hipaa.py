"""HIPAA (Health Insurance Portability and Accountability Act) compliance rules
and verification module.

Implements a ``HIPAARegulationChecker`` class extending the
``BaseRegulationChecker`` interface to validate synthetic datasets against
HIPAA requirements.  The checker verifies compliance with the HIPAA Privacy
Rule, specifically:

* **45 CFR 164.514(b)(2)** — Safe Harbor de-identification method requiring
  removal or adequate transformation of all 18 PHI (Protected Health
  Information) identifier categories.
* **45 CFR 164.514(a)** — Expert Determination method validating the
  presence of statistical/scientific methodology documentation.
* **45 CFR 164.502(b)** — Minimum Necessary standard ensuring only the
  minimum amount of PHI needed for the stated purpose is generated.

The 18 PHI identifier categories covered per 45 CFR 164.514(b)(2):

 1. Names
 2. Geographic data smaller than state
 3. Dates (except year) directly related to an individual
 4. Phone numbers
 5. Fax numbers
 6. Email addresses
 7. Social Security numbers
 8. Medical record numbers
 9. Health plan beneficiary numbers
10. Account numbers
11. Certificate/license numbers
12. Vehicle identifiers and serial numbers
13. Device identifiers and serial numbers
14. Web URLs
15. IP addresses
16. Biometric identifiers
17. Full-face photographs and comparable images
18. Any other unique identifying number, characteristic, or code

Typical usage::

    from compliance_service.regulations.hipaa import HIPAARegulationChecker

    checker = HIPAARegulationChecker()
    result = checker.check_compliance(dataset_metadata, scan_results)
    if not result.is_compliant:
        for violation in result.violations:
            print(f"[{violation.severity.value}] {violation.description}")
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Any

from compliance_service.regulations import (
    BaseRegulationChecker,
    ComplianceResult,
    ComplianceViolation,
    RegulationType,
    Severity,
)
from shared.logging.structured_logger import get_logger


# ---------------------------------------------------------------------------
# HIPAA PHI Identifiers — 18 categories per 45 CFR 164.514(b)(2)
# ---------------------------------------------------------------------------

HIPAA_PHI_IDENTIFIERS: dict[str, list[str]] = {
    "names": [
        "first_name",
        "last_name",
        "full_name",
        "maiden_name",
        "alias",
    ],
    "geographic_data": [
        "street_address",
        "city",
        "county",
        "precinct",
        "zip_code",
    ],
    "dates": [
        "birth_date",
        "admission_date",
        "discharge_date",
        "death_date",
    ],
    "phone_numbers": [
        "phone",
        "telephone",
        "mobile",
        "cell_phone",
        "home_phone",
        "work_phone",
    ],
    "fax_numbers": [
        "fax",
        "fax_number",
    ],
    "email_addresses": [
        "email",
        "email_address",
        "e_mail",
    ],
    "social_security_numbers": [
        "ssn",
        "social_security",
        "social_security_number",
    ],
    "medical_record_numbers": [
        "mrn",
        "medical_record",
        "medical_record_number",
        "patient_id",
    ],
    "health_plan_beneficiary_numbers": [
        "health_plan_id",
        "beneficiary_number",
        "member_id",
        "subscriber_id",
        "insurance_id",
    ],
    "account_numbers": [
        "account_number",
        "bank_account",
        "financial_account",
    ],
    "certificate_license_numbers": [
        "license_number",
        "certificate_number",
        "dea_number",
        "npi",
    ],
    "vehicle_identifiers": [
        "vin",
        "vehicle_id",
        "license_plate",
    ],
    "device_identifiers": [
        "device_id",
        "serial_number",
        "udi",
        "imei",
    ],
    "web_urls": [
        "url",
        "web_address",
        "website",
    ],
    "ip_addresses": [
        "ip_address",
        "ipv4",
        "ipv6",
    ],
    "biometric_identifiers": [
        "fingerprint",
        "voiceprint",
        "retina_scan",
        "iris_scan",
    ],
    "full_face_photos": [
        "photo",
        "image",
        "photograph",
        "face_image",
    ],
    "unique_identifying_numbers": [
        "unique_id",
        "patient_identifier",
        "record_id",
    ],
}
"""All 18 HIPAA PHI identifier categories mapped to common column-name
patterns.  Each key is a category defined by the Privacy Rule and the
value list contains lower-case, underscore-separated field-name fragments
used for pattern matching during compliance checks."""


HIPAA_MINIMUM_NECESSARY_CATEGORIES: set[str] = {
    "names",
    "geographic_data",
    "dates",
    "phone_numbers",
    "fax_numbers",
    "email_addresses",
    "social_security_numbers",
    "medical_record_numbers",
    "health_plan_beneficiary_numbers",
    "account_numbers",
    "certificate_license_numbers",
    "vehicle_identifiers",
    "device_identifiers",
    "web_urls",
    "ip_addresses",
    "biometric_identifiers",
    "full_face_photos",
    "unique_identifying_numbers",
}
"""Set of PHI categories that require explicit justification for inclusion
in synthetic data generation under the HIPAA Minimum Necessary standard
(45 CFR 164.502(b)).  All 18 PHI categories are included because any PHI
present in generated datasets must be justified."""


# ---------------------------------------------------------------------------
# Internal constants
# ---------------------------------------------------------------------------

_PHI_CATEGORY_INDEX: dict[str, int] = {
    "names": 1,
    "geographic_data": 2,
    "dates": 3,
    "phone_numbers": 4,
    "fax_numbers": 5,
    "email_addresses": 6,
    "social_security_numbers": 7,
    "medical_record_numbers": 8,
    "health_plan_beneficiary_numbers": 9,
    "account_numbers": 10,
    "certificate_license_numbers": 11,
    "vehicle_identifiers": 12,
    "device_identifiers": 13,
    "web_urls": 14,
    "ip_addresses": 15,
    "biometric_identifiers": 16,
    "full_face_photos": 17,
    "unique_identifying_numbers": 18,
}
"""Mapping from PHI category name to its ordinal position (1-18) in the
Safe Harbor enumeration at 45 CFR 164.514(b)(2).  Used when constructing
``article_reference`` strings for violations."""

_DIRECT_IDENTIFIER_CATEGORIES: set[str] = {
    "social_security_numbers",
    "medical_record_numbers",
    "health_plan_beneficiary_numbers",
    "certificate_license_numbers",
    "device_identifiers",
    "vehicle_identifiers",
    "biometric_identifiers",
    "full_face_photos",
}
"""PHI categories classified as *direct identifiers* — capable of uniquely
identifying an individual without additional context.  These receive
``Severity.CRITICAL`` whereas quasi-identifiers receive ``Severity.HIGH``."""

_PII_TYPE_TO_PHI_MAP: dict[str, str] = {
    # Name-related PII types
    "PERSON": "names",
    "NAME": "names",
    "FIRST_NAME": "names",
    "LAST_NAME": "names",
    "FULL_NAME": "names",
    # Geographic PII types
    "GPE": "geographic_data",
    "LOC": "geographic_data",
    "ADDRESS": "geographic_data",
    "STREET_ADDRESS": "geographic_data",
    "CITY": "geographic_data",
    "ZIP_CODE": "geographic_data",
    "POSTAL_CODE": "geographic_data",
    # Date PII types
    "DATE": "dates",
    "DATE_TIME": "dates",
    "DATE_OF_BIRTH": "dates",
    "DOB": "dates",
    # Phone PII types
    "PHONE_NUMBER": "phone_numbers",
    "PHONE": "phone_numbers",
    "MOBILE": "phone_numbers",
    # Fax PII types
    "FAX_NUMBER": "fax_numbers",
    "FAX": "fax_numbers",
    # Email PII types
    "EMAIL": "email_addresses",
    "EMAIL_ADDRESS": "email_addresses",
    # SSN PII types
    "SSN": "social_security_numbers",
    "SOCIAL_SECURITY": "social_security_numbers",
    "SOCIAL_SECURITY_NUMBER": "social_security_numbers",
    # Medical record PII types
    "MEDICAL_RECORD": "medical_record_numbers",
    "MRN": "medical_record_numbers",
    "MEDICAL_RECORD_NUMBER": "medical_record_numbers",
    "PATIENT_ID": "medical_record_numbers",
    # Health plan PII types
    "HEALTH_PLAN_ID": "health_plan_beneficiary_numbers",
    "INSURANCE_ID": "health_plan_beneficiary_numbers",
    "BENEFICIARY_NUMBER": "health_plan_beneficiary_numbers",
    "MEMBER_ID": "health_plan_beneficiary_numbers",
    "SUBSCRIBER_ID": "health_plan_beneficiary_numbers",
    # Account PII types
    "ACCOUNT_NUMBER": "account_numbers",
    "FINANCIAL_ACCOUNT": "account_numbers",
    "BANK_ACCOUNT": "account_numbers",
    # Certificate/license PII types
    "LICENSE_NUMBER": "certificate_license_numbers",
    "CERTIFICATE": "certificate_license_numbers",
    "CERTIFICATE_NUMBER": "certificate_license_numbers",
    "NPI": "certificate_license_numbers",
    "DEA": "certificate_license_numbers",
    "DEA_NUMBER": "certificate_license_numbers",
    # Vehicle PII types
    "VIN": "vehicle_identifiers",
    "VEHICLE": "vehicle_identifiers",
    "VEHICLE_ID": "vehicle_identifiers",
    "LICENSE_PLATE": "vehicle_identifiers",
    # Device PII types
    "DEVICE_ID": "device_identifiers",
    "SERIAL_NUMBER": "device_identifiers",
    "IMEI": "device_identifiers",
    "UDI": "device_identifiers",
    # Web URL PII types
    "URL": "web_urls",
    "WEB_ADDRESS": "web_urls",
    "WEBSITE": "web_urls",
    # IP address PII types
    "IP_ADDRESS": "ip_addresses",
    "IPV4": "ip_addresses",
    "IPV6": "ip_addresses",
    # Biometric PII types
    "BIOMETRIC": "biometric_identifiers",
    "FINGERPRINT": "biometric_identifiers",
    "VOICEPRINT": "biometric_identifiers",
    "RETINA_SCAN": "biometric_identifiers",
    "IRIS_SCAN": "biometric_identifiers",
    # Photo PII types
    "PHOTO": "full_face_photos",
    "IMAGE": "full_face_photos",
    "PHOTOGRAPH": "full_face_photos",
    "FACE": "full_face_photos",
    "FACE_IMAGE": "full_face_photos",
    # Unique identifier PII types
    "UNIQUE_ID": "unique_identifying_numbers",
    "PATIENT_IDENTIFIER": "unique_identifying_numbers",
    "RECORD_ID": "unique_identifying_numbers",
}
"""Mapping of generic PII entity types (as produced by the PII detectors)
to the corresponding HIPAA PHI category name.  The keys are upper-case to
allow case-insensitive matching."""


# ---------------------------------------------------------------------------
# Internal data structures
# ---------------------------------------------------------------------------


@dataclass
class PHICheckResult:
    """Internal container for aggregating per-category PHI check outcomes.

    Used during Safe Harbor validation to track which of the 18 PHI categories
    have been detected and whether each has been adequately de-identified.

    Attributes:
        category: The PHI category name (e.g. ``'social_security_numbers'``).
        matched_fields: Column names that matched this category.
        is_deidentified: Whether de-identification has been confirmed.
        confidence: Average detection confidence across matched fields.
    """

    category: str
    matched_fields: list[str] = field(default_factory=list)
    is_deidentified: bool = False
    confidence: float = 0.0


class DeidentificationMethod(Enum):
    """HIPAA-recognized de-identification methods.

    The HIPAA Privacy Rule provides two methods for de-identification of
    protected health information:

    * **Safe Harbor** (45 CFR 164.514(b)) — Removal of 18 specified
      identifier types.
    * **Expert Determination** (45 CFR 164.514(a)) — A qualified
      statistical or scientific expert determines that the risk of
      identifying an individual is very small.

    Attributes:
        SAFE_HARBOR: Safe Harbor de-identification method.
        EXPERT_DETERMINATION: Expert Determination method.
    """

    SAFE_HARBOR = "safe_harbor"
    EXPERT_DETERMINATION = "expert_determination"


# ---------------------------------------------------------------------------
# Module-level helper functions
# ---------------------------------------------------------------------------


def _normalize_column_name(name: str) -> str:
    """Normalize a column name for case-insensitive PHI pattern matching.

    Converts to lower case, strips leading/trailing whitespace, and replaces
    hyphens with underscores so that column names such as ``'First-Name'``,
    ``'first_name'``, and ``'  FIRST NAME  '`` all reduce to the same form.

    Args:
        name: Raw column or field name.

    Returns:
        Normalized name suitable for substring comparison.
    """
    return name.strip().lower().replace("-", "_").replace(" ", "_")


def _is_direct_identifier(phi_category: str) -> bool:
    """Determine whether a PHI category is a direct identifier.

    Direct identifiers (e.g. SSN, medical record number) can uniquely
    identify an individual without requiring additional context and therefore
    receive ``Severity.CRITICAL`` in violation reports.  Quasi-identifiers
    (e.g. date of birth, ZIP code) receive ``Severity.HIGH``.

    Args:
        phi_category: One of the 18 PHI category key names.

    Returns:
        ``True`` if the category is a direct identifier, ``False`` otherwise.
    """
    return phi_category in _DIRECT_IDENTIFIER_CATEGORIES


def _map_pii_type_to_phi(pii_type: str) -> str | None:
    """Map a generic PII entity type to its HIPAA PHI category.

    The PII detectors (NLP-based and regex-based) produce entity labels
    such as ``'PERSON'``, ``'SSN'``, ``'EMAIL'``.  This function translates
    those labels into one of the 18 HIPAA PHI category names so that
    findings can be attributed to the correct Safe Harbor identifier.

    Args:
        pii_type: Upper- or mixed-case PII entity label produced by a
            detector.

    Returns:
        The PHI category name (e.g. ``'social_security_numbers'``), or
        ``None`` if the PII type does not map to a recognized PHI category.
    """
    return _PII_TYPE_TO_PHI_MAP.get(pii_type.upper().strip())


# ---------------------------------------------------------------------------
# HIPAARegulationChecker
# ---------------------------------------------------------------------------


class HIPAARegulationChecker(BaseRegulationChecker):
    """HIPAA regulatory compliance checker for synthetic dataset verification.

    Extends ``BaseRegulationChecker`` to implement the full suite of HIPAA
    Privacy Rule checks required before a synthetic dataset can be certified
    for release.  The checker enforces:

    * **PHI Identifier Scanning** — Detects presence of any of the 18 PHI
      identifier types defined by the Safe Harbor method.
    * **Minimum Necessary Standard** — Validates that each PHI-containing
      field has an explicit purpose justification and role-based access
      control definition.
    * **Safe Harbor Validation** — Confirms that all 18 identifier
      categories have been removed or adequately de-identified.
    * **Expert Determination Validation** — When the Expert Determination
      method is claimed, confirms that statistical/scientific methodology
      documentation is present and complete.

    The overall compliance result is ``is_compliant=True`` **only** when
    every individual check passes with zero violations.

    Attributes:
        regulation_type: Always ``RegulationType.HIPAA``.
        logger: Structured logger bound with ``regulation_type='HIPAA'``
            context.
        phi_identifiers: Flattened ``set`` of all PHI identifier field names
            across all 18 categories for O(1) lookups.
        phi_categories: Reference to ``HIPAA_PHI_IDENTIFIERS`` mapping from
            category name to identifier list.

    Example::

        checker = HIPAARegulationChecker()
        result = checker.check_compliance(
            dataset_metadata={
                "columns": [
                    {"name": "patient_ssn", "type": "VARCHAR(11)"},
                    {"name": "visit_date", "type": "DATE"},
                ],
                "generation_profile": {"purpose": "testing"},
            },
            scan_results={
                "detections": [
                    {"field_name": "patient_ssn", "pii_type": "SSN",
                     "confidence": 0.99},
                ],
            },
        )
        assert result.is_compliant is False
    """

    def __init__(self) -> None:
        """Initialize the HIPAA regulation checker.

        Pre-computes a flattened set of all PHI identifier field names from
        the 18 categories for efficient O(1) pattern matching during column
        scanning.
        """
        self.logger = get_logger(__name__)
        self.phi_identifiers: set[str] = set()
        for identifiers in HIPAA_PHI_IDENTIFIERS.values():
            self.phi_identifiers.update(identifiers)
        self.phi_categories: dict[str, list[str]] = HIPAA_PHI_IDENTIFIERS

    # ------------------------------------------------------------------
    # Abstract interface implementations
    # ------------------------------------------------------------------

    @property
    def regulation_type(self) -> RegulationType:
        """Return the HIPAA regulation type identifier.

        Returns:
            ``RegulationType.HIPAA``.
        """
        return RegulationType.HIPAA

    def check_compliance(
        self,
        dataset_metadata: dict[str, Any],
        scan_results: dict[str, Any],
    ) -> ComplianceResult:
        """Execute the full HIPAA compliance check suite.

        Orchestrates all four HIPAA sub-checks and aggregates their
        violations into a single ``ComplianceResult``.  The dataset is
        considered compliant **only** when every sub-check passes with
        zero violations.

        Sub-checks executed in order:

        1. **PHI Identifier Scan** — Column name + PII detection matching
           against the 18 PHI categories.
        2. **Minimum Necessary** — Justification and access-control
           validation for PHI-containing fields.
        3. **Safe Harbor** — Verification that all detected PHI categories
           have been adequately de-identified.
        4. **Expert Determination** — Documentation completeness check
           (only when Expert Determination method is claimed).

        Args:
            dataset_metadata: Schema information including ``columns`` (list
                of dicts with ``name`` and ``type`` keys), ``generation_profile``
                (dict with ``purpose`` key), ``phi_justifications`` (dict
                mapping field names to justification text),
                ``access_controls`` (dict mapping field names to role lists),
                ``deidentification_method`` (str), and
                ``expert_determination`` (dict with documentation fields).
            scan_results: PII detection output containing ``detections``
                (list of dicts with ``field_name``, ``pii_type``,
                ``confidence``), ``phi_categories_found`` (list of category
                names), and ``deidentification_status`` (dict mapping
                categories to status dicts with ``is_addressed`` and
                ``affected_fields``).

        Returns:
            A ``ComplianceResult`` with ``is_compliant=True`` only if all
            sub-checks produce zero violations.
        """
        self.logger.info(
            "hipaa_compliance_check_started",
            regulation_type="HIPAA",
        )

        columns: list[dict[str, Any]] = dataset_metadata.get("columns", [])
        all_violations: list[ComplianceViolation] = []

        # --- 1. PHI Identifier Check ---
        try:
            phi_violations = self.check_phi_identifiers(columns, scan_results)
            all_violations.extend(phi_violations)
            self.logger.info(
                "hipaa_phi_identifier_check_completed",
                regulation_type="HIPAA",
                violation_count=len(phi_violations),
            )
        except Exception as exc:
            self.logger.error(
                "hipaa_phi_identifier_check_failed",
                regulation_type="HIPAA",
                error=str(exc),
            )
            all_violations.append(
                ComplianceViolation(
                    regulation_type=RegulationType.HIPAA,
                    severity=Severity.CRITICAL,
                    article_reference="45 CFR 164.514(b)(2)",
                    description=(
                        f"PHI identifier check failed with error: {exc}"
                    ),
                    remediation="Investigate and resolve the error, then re-run the compliance check.",
                )
            )

        # --- 2. Minimum Necessary Check ---
        try:
            min_necessary_violations = self.check_minimum_necessary(
                dataset_metadata, columns,
            )
            all_violations.extend(min_necessary_violations)
            self.logger.info(
                "hipaa_minimum_necessary_check_completed",
                regulation_type="HIPAA",
                violation_count=len(min_necessary_violations),
            )
        except Exception as exc:
            self.logger.error(
                "hipaa_minimum_necessary_check_failed",
                regulation_type="HIPAA",
                error=str(exc),
            )
            all_violations.append(
                ComplianceViolation(
                    regulation_type=RegulationType.HIPAA,
                    severity=Severity.HIGH,
                    article_reference="45 CFR 164.502(b)",
                    description=(
                        f"Minimum Necessary check failed with error: {exc}"
                    ),
                    remediation="Investigate and resolve the error, then re-run the compliance check.",
                )
            )

        # --- 3. Safe Harbor Check ---
        try:
            safe_harbor_violations = self.check_safe_harbor_method(
                scan_results,
            )
            all_violations.extend(safe_harbor_violations)
            self.logger.info(
                "hipaa_safe_harbor_check_completed",
                regulation_type="HIPAA",
                violation_count=len(safe_harbor_violations),
            )
        except Exception as exc:
            self.logger.error(
                "hipaa_safe_harbor_check_failed",
                regulation_type="HIPAA",
                error=str(exc),
            )
            all_violations.append(
                ComplianceViolation(
                    regulation_type=RegulationType.HIPAA,
                    severity=Severity.CRITICAL,
                    article_reference="45 CFR 164.514(b)(2)",
                    description=(
                        f"Safe Harbor validation failed with error: {exc}"
                    ),
                    remediation="Investigate and resolve the error, then re-run the compliance check.",
                )
            )

        # --- 4. Expert Determination Check ---
        try:
            expert_violations = self.check_expert_determination(
                dataset_metadata,
            )
            all_violations.extend(expert_violations)
            self.logger.info(
                "hipaa_expert_determination_check_completed",
                regulation_type="HIPAA",
                violation_count=len(expert_violations),
            )
        except Exception as exc:
            self.logger.error(
                "hipaa_expert_determination_check_failed",
                regulation_type="HIPAA",
                error=str(exc),
            )
            all_violations.append(
                ComplianceViolation(
                    regulation_type=RegulationType.HIPAA,
                    severity=Severity.MEDIUM,
                    article_reference="45 CFR 164.514(a)",
                    description=(
                        f"Expert Determination check failed with error: {exc}"
                    ),
                    remediation="Investigate and resolve the error, then re-run the compliance check.",
                )
            )

        # --- Aggregate results ---
        has_critical = any(
            v.severity == Severity.CRITICAL for v in all_violations
        )
        is_compliant = len(all_violations) == 0
        total_fields = len(columns)

        result = self._create_result(
            is_compliant=is_compliant,
            violations=all_violations,
            total_fields=total_fields,
            metadata={
                "phi_violations": len(
                    [v for v in all_violations if "164.514(b)(2)" in v.article_reference
                     and v.metadata.get("detection_source") in ("column_name_match", "pii_scan")]
                ),
                "minimum_necessary_violations": len(
                    [v for v in all_violations if "164.502(b)" in v.article_reference]
                ),
                "safe_harbor_violations": len(
                    [v for v in all_violations if v.metadata.get("deidentification_method") == "safe_harbor"]
                ),
                "expert_determination_violations": len(
                    [v for v in all_violations if "164.514(a)" in v.article_reference]
                ),
                "has_critical_violations": has_critical,
                "deidentification_method": dataset_metadata.get(
                    "deidentification_method", "safe_harbor",
                ),
            },
        )

        self.logger.info(
            "hipaa_compliance_check_completed",
            regulation_type="HIPAA",
            is_compliant=is_compliant,
            compliance_score=result.compliance_score,
            total_violations=len(all_violations),
            total_fields_checked=total_fields,
        )

        return result

    def get_regulation_name(self) -> str:
        """Return the full human-readable name of the HIPAA regulation.

        Returns:
            ``'HIPAA - Health Insurance Portability and Accountability Act'``.
        """
        return "HIPAA - Health Insurance Portability and Accountability Act"

    def get_regulation_version(self) -> str:
        """Return the HIPAA legal citation.

        Returns:
            ``'Public Law 104-191, 45 CFR Parts 160-164'``.
        """
        return "Public Law 104-191, 45 CFR Parts 160-164"

    # ------------------------------------------------------------------
    # HIPAA-specific check methods
    # ------------------------------------------------------------------

    def check_phi_identifiers(
        self,
        columns: list[dict[str, Any]],
        scan_results: dict[str, Any],
    ) -> list[ComplianceViolation]:
        """Check dataset columns and PII scan results for PHI identifiers.

        Performs a two-pass detection:

        1. **Column-name matching** — Each column name is normalized and
           compared against the field-name patterns in all 18 PHI categories.
        2. **PII detection matching** — Each PII detection from the scan
           results is mapped to a PHI category via the PII-type-to-PHI
           mapping table.

        A ``ComplianceViolation`` is created for every match, with
        ``Severity.CRITICAL`` for direct identifiers (SSN, MRN, etc.) and
        ``Severity.HIGH`` for quasi-identifiers (dates, ZIP codes, etc.).

        Args:
            columns: List of column dicts, each with at least ``'name'``
                and ``'type'`` keys.
            scan_results: PII detection output containing a ``'detections'``
                list of dicts with ``'field_name'``, ``'pii_type'``, and
                ``'confidence'`` keys.

        Returns:
            List of ``ComplianceViolation`` instances for every PHI
            identifier found.
        """
        violations: list[ComplianceViolation] = []
        seen_column_categories: set[str] = set()

        # --- Pass 1: Column-name pattern matching ---
        for column in columns:
            col_name: str = column.get("name", "")
            col_type: str = column.get("type", "")
            if not col_name:
                continue

            phi_category = self.get_phi_category_for_column(col_name, col_type)
            if phi_category is not None:
                identifier_index = _PHI_CATEGORY_INDEX.get(phi_category, 0)
                severity = (
                    Severity.CRITICAL
                    if _is_direct_identifier(phi_category)
                    else Severity.HIGH
                )
                violations.append(
                    ComplianceViolation(
                        regulation_type=RegulationType.HIPAA,
                        severity=severity,
                        article_reference=(
                            f"45 CFR 164.514(b)(2) - Identifier #{identifier_index}"
                        ),
                        description=(
                            f"Column '{col_name}' matches HIPAA PHI identifier "
                            f"category '{phi_category}' (Safe Harbor identifier "
                            f"#{identifier_index})."
                        ),
                        affected_fields=[col_name],
                        remediation=(
                            f"Remove, mask, or generalize the '{phi_category}' "
                            f"data in column '{col_name}' to comply with "
                            f"HIPAA Safe Harbor de-identification requirements."
                        ),
                        metadata={
                            "phi_category": phi_category,
                            "identifier_index": identifier_index,
                            "detection_source": "column_name_match",
                        },
                    )
                )
                seen_column_categories.add(f"{col_name}:{phi_category}")

        # --- Pass 2: PII scan result matching ---
        detections: list[dict[str, Any]] = scan_results.get("detections", [])
        for detection in detections:
            field_name: str = detection.get("field_name", "")
            pii_type: str = detection.get("pii_type", "")
            confidence: float = float(detection.get("confidence", 0.0))
            if not pii_type:
                continue

            phi_category = _map_pii_type_to_phi(pii_type)
            if phi_category is not None:
                # Avoid complete duplicates when column-name match already
                # flagged the identical field+category pair; still create the
                # violation as a separate evidence record with PII-scan source.
                combo_key = f"{field_name}:{phi_category}"
                if combo_key in seen_column_categories:
                    # Supplement existing column-name-match violation with
                    # PII-scan confirmation, but still record it separately
                    # for audit completeness.
                    pass

                identifier_index = _PHI_CATEGORY_INDEX.get(phi_category, 0)
                severity = (
                    Severity.CRITICAL
                    if _is_direct_identifier(phi_category)
                    else Severity.HIGH
                )
                violations.append(
                    ComplianceViolation(
                        regulation_type=RegulationType.HIPAA,
                        severity=severity,
                        article_reference=(
                            f"45 CFR 164.514(b)(2) - Identifier #{identifier_index}"
                        ),
                        description=(
                            f"PII detection found '{pii_type}' in field "
                            f"'{field_name}' (confidence: {confidence:.2f}), "
                            f"mapping to PHI category '{phi_category}'."
                        ),
                        affected_fields=[field_name] if field_name else [],
                        remediation=(
                            f"Apply de-identification to field '{field_name}' "
                            f"to remove '{phi_category}' data per HIPAA Safe "
                            f"Harbor requirements."
                        ),
                        metadata={
                            "phi_category": phi_category,
                            "identifier_index": identifier_index,
                            "pii_type": pii_type,
                            "confidence": confidence,
                            "detection_source": "pii_scan",
                        },
                    )
                )

        self.logger.debug(
            "hipaa_phi_identifiers_scanned",
            columns_checked=len(columns),
            detections_checked=len(detections),
            violations_found=len(violations),
        )

        return violations

    def check_minimum_necessary(
        self,
        dataset_metadata: dict[str, Any],
        columns: list[dict[str, Any]],
    ) -> list[ComplianceViolation]:
        """Validate the HIPAA Minimum Necessary standard (45 CFR 164.502(b)).

        The Minimum Necessary standard requires that covered entities limit
        the use and disclosure of PHI to the minimum amount reasonably
        necessary for the intended purpose.  For synthetic data generation
        this translates to:

        1. The generation profile must declare a purpose.
        2. Every field containing PHI must have an explicit justification.
        3. Role-based access controls must be defined for PHI columns.

        Args:
            dataset_metadata: Full dataset metadata dict containing
                ``generation_profile``, ``phi_justifications``, and
                ``access_controls`` sub-dicts.
            columns: List of column dicts with ``'name'`` and ``'type'`` keys.

        Returns:
            List of ``ComplianceViolation`` instances for Minimum Necessary
            violations.
        """
        violations: list[ComplianceViolation] = []
        generation_profile: dict[str, Any] = dataset_metadata.get(
            "generation_profile", {},
        )
        phi_justifications: dict[str, str] = dataset_metadata.get(
            "phi_justifications", {},
        )
        access_controls: dict[str, Any] = dataset_metadata.get(
            "access_controls", {},
        )

        # Check: overall purpose declaration
        purpose: str = str(generation_profile.get("purpose", "")).strip()
        if not purpose:
            violations.append(
                ComplianceViolation(
                    regulation_type=RegulationType.HIPAA,
                    severity=Severity.HIGH,
                    article_reference="45 CFR 164.502(b)",
                    description=(
                        "Generation profile does not specify a purpose for "
                        "PHI inclusion, required by the HIPAA Minimum "
                        "Necessary standard."
                    ),
                    affected_fields=[],
                    remediation=(
                        "Specify the purpose for generating data containing "
                        "PHI in the generation profile configuration."
                    ),
                )
            )

        # Per-column checks for PHI fields
        phi_fields_found: int = 0
        for column in columns:
            col_name: str = column.get("name", "")
            col_type: str = column.get("type", "")
            if not col_name:
                continue

            phi_category = self.get_phi_category_for_column(col_name, col_type)
            if phi_category is None:
                continue
            if phi_category not in HIPAA_MINIMUM_NECESSARY_CATEGORIES:
                continue

            phi_fields_found += 1

            # Check: field-level justification
            if col_name not in phi_justifications:
                violations.append(
                    ComplianceViolation(
                        regulation_type=RegulationType.HIPAA,
                        severity=Severity.HIGH,
                        article_reference="45 CFR 164.502(b)",
                        description=(
                            f"PHI field '{col_name}' (category: "
                            f"'{phi_category}') lacks required justification "
                            f"under the Minimum Necessary standard."
                        ),
                        affected_fields=[col_name],
                        remediation=(
                            f"Provide explicit justification for including "
                            f"'{col_name}' in the generation profile, or "
                            f"remove the field if it is not necessary."
                        ),
                        metadata={"phi_category": phi_category},
                    )
                )

            # Check: role-based access controls
            if col_name not in access_controls:
                violations.append(
                    ComplianceViolation(
                        regulation_type=RegulationType.HIPAA,
                        severity=Severity.HIGH,
                        article_reference="45 CFR 164.502(b)",
                        description=(
                            f"PHI field '{col_name}' (category: "
                            f"'{phi_category}') lacks role-based access "
                            f"control definition as required by the Minimum "
                            f"Necessary standard."
                        ),
                        affected_fields=[col_name],
                        remediation=(
                            f"Define role-based access permissions for "
                            f"'{col_name}' to enforce the Minimum Necessary "
                            f"standard."
                        ),
                        metadata={"phi_category": phi_category},
                    )
                )

        self.logger.debug(
            "hipaa_minimum_necessary_checked",
            phi_fields_found=phi_fields_found,
            violations_found=len(violations),
            has_purpose=bool(purpose),
        )

        return violations

    def check_safe_harbor_method(
        self,
        scan_results: dict[str, Any],
    ) -> list[ComplianceViolation]:
        """Validate de-identification per Safe Harbor method (45 CFR 164.514(b)).

        The Safe Harbor method requires that all 18 PHI identifier categories
        be addressed — either removed entirely or transformed so that the
        remaining data cannot reasonably identify an individual.

        This check verifies that every PHI category detected in the dataset
        has a corresponding de-identification status confirming it has been
        addressed.  Any category found in the data without a confirmed
        de-identification record produces a ``Severity.CRITICAL`` violation.

        Args:
            scan_results: PII detection output containing
                ``phi_categories_found`` (list of detected category names)
                and ``deidentification_status`` (dict mapping category names
                to status dicts with ``is_addressed`` bool and
                ``affected_fields`` list).

        Returns:
            List of ``ComplianceViolation`` instances for unaddressed PHI
            categories.
        """
        violations: list[ComplianceViolation] = []
        deidentification_status: dict[str, Any] = scan_results.get(
            "deidentification_status", {},
        )
        phi_categories_found: set[str] = set(
            scan_results.get("phi_categories_found", []),
        )

        # Check each detected PHI category
        for category in HIPAA_PHI_IDENTIFIERS:
            if category not in phi_categories_found:
                continue

            identifier_index = _PHI_CATEGORY_INDEX.get(category, 0)
            status: dict[str, Any] = deidentification_status.get(category, {})
            is_addressed: bool = bool(status.get("is_addressed", False))
            affected_fields: list[str] = list(
                status.get("affected_fields", []),
            )

            if not is_addressed:
                violations.append(
                    ComplianceViolation(
                        regulation_type=RegulationType.HIPAA,
                        severity=Severity.CRITICAL,
                        article_reference=(
                            f"45 CFR 164.514(b)(2) - Identifier #{identifier_index}"
                        ),
                        description=(
                            f"PHI category '{category}' (Safe Harbor identifier "
                            f"#{identifier_index}) detected in the dataset but "
                            f"not adequately de-identified."
                        ),
                        affected_fields=affected_fields,
                        remediation=(
                            f"Apply de-identification to all '{category}' "
                            f"fields: remove the data, apply masking, "
                            f"generalize values, or apply an approved "
                            f"transformation method."
                        ),
                        metadata={
                            "phi_category": category,
                            "identifier_index": identifier_index,
                            "deidentification_method": "safe_harbor",
                        },
                    )
                )

        # Cross-check PII detections against de-identification status
        detections: list[dict[str, Any]] = scan_results.get("detections", [])
        uncovered_categories: set[str] = set()

        for detection in detections:
            pii_type: str = detection.get("pii_type", "")
            if not pii_type:
                continue
            phi_category = _map_pii_type_to_phi(pii_type)
            if phi_category is None:
                continue
            if phi_category in deidentification_status:
                continue
            if phi_category in uncovered_categories:
                # Already reported for this category in this pass
                continue

            uncovered_categories.add(phi_category)
            field_name: str = detection.get("field_name", "unknown")
            identifier_index = _PHI_CATEGORY_INDEX.get(phi_category, 0)

            violations.append(
                ComplianceViolation(
                    regulation_type=RegulationType.HIPAA,
                    severity=Severity.CRITICAL,
                    article_reference=(
                        f"45 CFR 164.514(b)(2) - Identifier #{identifier_index}"
                    ),
                    description=(
                        f"Detected PII type '{pii_type}' in field "
                        f"'{field_name}' has no de-identification record for "
                        f"PHI category '{phi_category}'."
                    ),
                    affected_fields=[field_name],
                    remediation=(
                        f"Ensure de-identification is applied and tracked for "
                        f"the '{phi_category}' category before dataset release."
                    ),
                    metadata={
                        "phi_category": phi_category,
                        "identifier_index": identifier_index,
                        "pii_type": pii_type,
                        "deidentification_method": "safe_harbor",
                    },
                )
            )

        self.logger.debug(
            "hipaa_safe_harbor_validated",
            categories_found=len(phi_categories_found),
            categories_addressed=len(
                [c for c in phi_categories_found
                 if deidentification_status.get(c, {}).get("is_addressed", False)]
            ),
            violations_found=len(violations),
        )

        return violations

    def check_expert_determination(
        self,
        dataset_metadata: dict[str, Any],
    ) -> list[ComplianceViolation]:
        """Validate Expert Determination documentation (45 CFR 164.514(a)).

        The Expert Determination method allows de-identification to be
        certified by a qualified statistical or scientific expert who
        determines that the risk of individual identification is very small.
        When this method is claimed in the dataset metadata, the following
        documentation elements are required:

        * ``expert_name`` — Name or identifier of the certifying expert.
        * ``methodology`` — Description of the statistical or scientific
          methodology applied.
        * ``statistical_basis`` — The statistical or scientific basis for
          the determination that re-identification risk is very small.
        * ``certification_date`` — Date the determination was made.

        If the Expert Determination method is not claimed (i.e. the dataset
        relies on Safe Harbor), this check is a no-op and returns an empty
        list.

        Args:
            dataset_metadata: Full dataset metadata dict.  Relevant keys are
                ``deidentification_method`` (str) and
                ``expert_determination`` (dict with documentation fields).

        Returns:
            List of ``ComplianceViolation`` instances for missing or
            incomplete Expert Determination documentation.
        """
        violations: list[ComplianceViolation] = []
        deidentification_method: str = str(
            dataset_metadata.get("deidentification_method", ""),
        ).strip().lower()

        # Only validate if Expert Determination method is claimed
        if deidentification_method != DeidentificationMethod.EXPERT_DETERMINATION.value:
            return violations

        expert_determination: dict[str, Any] = dataset_metadata.get(
            "expert_determination", {},
        )

        # Check: documentation exists at all
        if not expert_determination:
            violations.append(
                ComplianceViolation(
                    regulation_type=RegulationType.HIPAA,
                    severity=Severity.MEDIUM,
                    article_reference="45 CFR 164.514(a)",
                    description=(
                        "Expert Determination method claimed but no "
                        "documentation provided.  Per 45 CFR 164.514(a), "
                        "a qualified expert must apply statistical and "
                        "scientific principles and document their methodology."
                    ),
                    affected_fields=[],
                    remediation=(
                        "Provide complete documentation of the "
                        "statistical/scientific methodology used for "
                        "de-identification, including the expert's identity, "
                        "methodology description, statistical basis, and "
                        "certification date."
                    ),
                )
            )
            self.logger.debug(
                "hipaa_expert_determination_missing",
                deidentification_method=deidentification_method,
            )
            return violations

        # Check: required documentation elements
        required_elements: dict[str, str] = {
            "expert_name": (
                "the name or identifier of the qualified statistical or "
                "scientific expert"
            ),
            "methodology": (
                "a description of the statistical or scientific methodology "
                "applied for de-identification"
            ),
            "statistical_basis": (
                "the statistical or scientific basis supporting the "
                "determination that re-identification risk is very small"
            ),
            "certification_date": (
                "the date on which the expert determination was certified"
            ),
        }

        for element_key, element_description in required_elements.items():
            value = expert_determination.get(element_key)
            if not value or (isinstance(value, str) and not value.strip()):
                violations.append(
                    ComplianceViolation(
                        regulation_type=RegulationType.HIPAA,
                        severity=Severity.MEDIUM,
                        article_reference="45 CFR 164.514(a)",
                        description=(
                            f"Expert Determination documentation is missing "
                            f"required element '{element_key}': "
                            f"{element_description}."
                        ),
                        affected_fields=[],
                        remediation=(
                            f"Include '{element_key}' ({element_description}) "
                            f"in the Expert Determination documentation."
                        ),
                        metadata={"missing_element": element_key},
                    )
                )

        self.logger.debug(
            "hipaa_expert_determination_validated",
            elements_present=len(
                [k for k in required_elements if expert_determination.get(k)]
            ),
            elements_required=len(required_elements),
            violations_found=len(violations),
        )

        return violations

    # ------------------------------------------------------------------
    # Public utility methods
    # ------------------------------------------------------------------

    def get_phi_category_for_column(
        self,
        column_name: str,
        column_type: str,
    ) -> str | None:
        """Map a column name/type to one of the 18 PHI identifier categories.

        Uses case-insensitive matching with normalization (lowercasing,
        stripping, hyphen-to-underscore conversion).  Matching is performed
        in two passes:

        1. **Exact match** — The normalized column name equals a PHI
           identifier pattern.
        2. **Substring match** — The normalized column name contains a PHI
           identifier pattern, or vice-versa.

        If column-name matching yields no result, the same two-pass strategy
        is applied to ``column_type``.

        Args:
            column_name: Raw column or field name.
            column_type: Raw column data type string (e.g. ``'VARCHAR(11)'``).

        Returns:
            The PHI category name (e.g. ``'social_security_numbers'``), or
            ``None`` if the column does not match any PHI category.
        """
        if not column_name and not column_type:
            return None

        normalized_name = _normalize_column_name(column_name)

        # --- Pass 1: Match against column name ---
        for category, identifiers in self.phi_categories.items():
            for identifier in identifiers:
                normalized_id = _normalize_column_name(identifier)
                # Exact match
                if normalized_id == normalized_name:
                    return category
                # Substring match — identifier contained in column name
                if normalized_id in normalized_name:
                    return category
                # Substring match — column name contained in identifier
                if normalized_name and normalized_name in normalized_id:
                    return category

        # --- Pass 2: Match against column type ---
        if column_type:
            normalized_type = _normalize_column_name(column_type)
            for category, identifiers in self.phi_categories.items():
                for identifier in identifiers:
                    normalized_id = _normalize_column_name(identifier)
                    if normalized_id in normalized_type:
                        return category

        return None
