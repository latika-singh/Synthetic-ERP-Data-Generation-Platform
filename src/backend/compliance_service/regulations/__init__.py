"""Regulatory framework package for GDPR, HIPAA, and CCPA compliance verification.

This package implements the Strategy pattern for pluggable regulation compliance
checkers. Each regulation (GDPR, HIPAA, CCPA) is implemented as a concrete
strategy extending the BaseRegulationChecker abstract interface, allowing the
Compliance Service to dynamically select and execute the appropriate checker
based on the regulatory framework requirements of a given dataset.

The compliance verification follows a state machine workflow:
    Pending → Scanning → PIICheck → Certified → Released

Each state transition is validated by the corresponding regulation checker,
which evaluates the dataset against regulation-specific rules and produces
a ComplianceResult containing pass/fail status, compliance score, and a
detailed list of ComplianceViolation records for audit trail purposes.

Typical usage::

    from compliance_service.regulations import (
        RegulationRegistry,
        RegulationType,
    )

    checker = RegulationRegistry.get_checker(RegulationType.GDPR)
    result = checker.check_compliance(dataset_metadata, scan_results)
    if not result.is_compliant:
        for violation in result.violations:
            print(f"{violation.severity}: {violation.description}")

Components:
    - RegulationType: Enum of supported regulatory frameworks (GDPR, HIPAA, CCPA).
    - Severity: Enum of violation severity levels (CRITICAL, HIGH, MEDIUM, LOW, INFO).
    - ComplianceViolation: Dataclass for individual violation records.
    - ComplianceResult: Dataclass for aggregated compliance check results.
    - BaseRegulationChecker: Abstract base class for all regulation checkers.
    - RegulationRegistry: Singleton registry for auto-discovered regulation checkers.
"""

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from datetime import UTC, datetime, timezone
from enum import Enum
from typing import Any


# ---------------------------------------------------------------------------
# Enumerations
# ---------------------------------------------------------------------------


class RegulationType(Enum):
    """Supported regulatory frameworks for compliance verification.

    Each value corresponds to a specific privacy regulation that the
    Compliance Service can verify datasets against. New regulations
    can be added by extending this enum and implementing a corresponding
    BaseRegulationChecker subclass.

    Attributes:
        GDPR: General Data Protection Regulation (EU 2016/679).
        HIPAA: Health Insurance Portability and Accountability Act (US).
        CCPA: California Consumer Privacy Act (Cal. Civ. Code §1798.100-199.100).
    """

    GDPR = "GDPR"
    HIPAA = "HIPAA"
    CCPA = "CCPA"


class Severity(Enum):
    """Violation severity levels for compliance check results.

    Severity levels are used both for human-readable reporting and for
    weighted compliance score calculation. Higher severity violations
    have a greater negative impact on the overall compliance score.

    Weight mapping used in score calculation:
        CRITICAL = 1.0, HIGH = 0.7, MEDIUM = 0.4, LOW = 0.2, INFO = 0.0

    Attributes:
        CRITICAL: Immediate compliance failure; must be resolved before release.
        HIGH: Significant risk; resolution strongly recommended.
        MEDIUM: Moderate risk; should be addressed.
        LOW: Minor concern; acceptable with documented justification.
        INFO: Informational only; no compliance impact.
    """

    CRITICAL = "CRITICAL"
    HIGH = "HIGH"
    MEDIUM = "MEDIUM"
    LOW = "LOW"
    INFO = "INFO"


# ---------------------------------------------------------------------------
# Severity weight mapping for compliance score calculation
# ---------------------------------------------------------------------------

_SEVERITY_WEIGHTS: dict[Severity, float] = {
    Severity.CRITICAL: 1.0,
    Severity.HIGH: 0.7,
    Severity.MEDIUM: 0.4,
    Severity.LOW: 0.2,
    Severity.INFO: 0.0,
}


# ---------------------------------------------------------------------------
# Data Classes
# ---------------------------------------------------------------------------


@dataclass
class ComplianceViolation:
    """Individual compliance violation detected during a regulation check.

    Represents a single instance where a dataset field or configuration does
    not meet the requirements of a specific regulation. Violations are
    collected by regulation checkers and aggregated into a ComplianceResult.

    Attributes:
        regulation_type: Which regulatory framework was violated.
        severity: Severity level of the violation (CRITICAL to INFO).
        article_reference: Specific legal article or section reference
            (e.g., ``'Art. 9 GDPR'``, ``'45 CFR 164.514(b)(2)'``).
        description: Human-readable explanation of the violation.
        affected_fields: Column or field names involved in the violation.
        remediation: Recommended corrective action.
        detected_at: UTC timestamp when the violation was detected.
        metadata: Additional context key-value pairs for audit enrichment.
    """

    regulation_type: RegulationType
    severity: Severity
    article_reference: str
    description: str
    affected_fields: list[str] = field(default_factory=list)
    remediation: str = ""
    detected_at: datetime = field(default_factory=lambda: datetime.now(UTC))
    metadata: dict[str, Any] = field(default_factory=dict)


@dataclass
class ComplianceResult:
    """Aggregated result of a regulation compliance check.

    Contains the overall pass/fail status, a normalized compliance score,
    and the full list of violations discovered during the check. This is
    the primary return type of every ``BaseRegulationChecker.check_compliance``
    invocation.

    Attributes:
        regulation_type: Which regulatory framework was evaluated.
        is_compliant: Overall pass (``True``) or fail (``False``).
        compliance_score: Normalized score in the range ``[0.0, 1.0]``.
        violations: All violations detected during the check.
        checked_at: UTC timestamp when the check was performed.
        regulation_name: Full human-readable name of the regulation.
        regulation_version: Version or legal reference of the regulation.
        total_fields_checked: Number of dataset fields evaluated.
        fields_with_violations: Number of fields that had violations.
        metadata: Additional context key-value pairs for audit enrichment.
    """

    regulation_type: RegulationType
    is_compliant: bool
    compliance_score: float
    violations: list[ComplianceViolation] = field(default_factory=list)
    checked_at: datetime = field(default_factory=lambda: datetime.now(UTC))
    regulation_name: str = ""
    regulation_version: str = ""
    total_fields_checked: int = 0
    fields_with_violations: int = 0
    metadata: dict[str, Any] = field(default_factory=dict)


# ---------------------------------------------------------------------------
# Abstract Base Class
# ---------------------------------------------------------------------------


class BaseRegulationChecker(ABC):
    """Abstract base class for regulation-specific compliance checkers.

    Defines the common interface that all regulation checkers (GDPR, HIPAA,
    CCPA) must implement.  Concrete subclasses provide regulation-specific
    logic in ``check_compliance``, ``get_regulation_name``, and
    ``get_regulation_version``.

    The class also provides shared helper methods for compliance score
    calculation and result construction that concrete implementations can
    leverage to ensure consistent output formatting.

    Example subclass::

        class GDPRRegulationChecker(BaseRegulationChecker):
            @property
            def regulation_type(self) -> RegulationType:
                return RegulationType.GDPR

            def check_compliance(self, dataset_metadata, scan_results):
                violations = self._run_gdpr_checks(...)
                return self._create_result(
                    is_compliant=len(violations) == 0,
                    violations=violations,
                    total_fields=len(dataset_metadata.get("columns", [])),
                )

            def get_regulation_name(self) -> str:
                return "GDPR - General Data Protection Regulation"

            def get_regulation_version(self) -> str:
                return "EU 2016/679"
    """

    # ------------------------------------------------------------------
    # Abstract interface
    # ------------------------------------------------------------------

    @property
    @abstractmethod
    def regulation_type(self) -> RegulationType:
        """Return the ``RegulationType`` enum value for this checker."""
        ...

    @abstractmethod
    def check_compliance(
        self,
        dataset_metadata: dict[str, Any],
        scan_results: dict[str, Any],
    ) -> ComplianceResult:
        """Execute the full compliance check for this regulation.

        Args:
            dataset_metadata: Schema information including columns, data types,
                generation profile configuration, and purpose declarations.
            scan_results: PII detection results from the PII detector,
                containing detected entity types, confidence scores, and
                affected field mappings.

        Returns:
            A ``ComplianceResult`` containing the overall pass/fail status,
            compliance score, and detailed violation list.
        """
        ...

    @abstractmethod
    def get_regulation_name(self) -> str:
        """Return the full human-readable name of the regulation.

        Returns:
            Regulation name, e.g. ``'GDPR - General Data Protection Regulation'``.
        """
        ...

    @abstractmethod
    def get_regulation_version(self) -> str:
        """Return the regulation version or legal citation.

        Returns:
            Version string, e.g. ``'EU 2016/679'``.
        """
        ...

    # ------------------------------------------------------------------
    # Concrete helper methods
    # ------------------------------------------------------------------

    def _calculate_compliance_score(
        self,
        violations: list[ComplianceViolation],
        total_fields: int,
    ) -> float:
        """Calculate a weighted compliance score from violations.

        The score is computed as::

            score = 1.0 - (weighted_violation_sum / max_possible_score)

        where each violation contributes its severity weight (CRITICAL=1.0,
        HIGH=0.7, MEDIUM=0.4, LOW=0.2, INFO=0.0) and the maximum possible
        score assumes every field could have a CRITICAL violation.

        The result is clamped to the ``[0.0, 1.0]`` range.

        Args:
            violations: List of violations to score.
            total_fields: Total number of fields that were checked.

        Returns:
            Normalized compliance score between ``0.0`` (completely
            non-compliant) and ``1.0`` (fully compliant).
        """
        if total_fields <= 0:
            # If no fields were checked, treat as fully compliant when
            # there are no violations, otherwise as non-compliant.
            return 0.0 if violations else 1.0

        # Sum weighted violation penalties
        weighted_violation_sum: float = 0.0
        for violation in violations:
            weight = _SEVERITY_WEIGHTS.get(violation.severity, 0.0)
            weighted_violation_sum += weight

        # Maximum possible penalty: every field has a CRITICAL violation
        max_possible_score = float(total_fields) * _SEVERITY_WEIGHTS[Severity.CRITICAL]

        if max_possible_score <= 0.0:
            return 1.0

        raw_score = 1.0 - (weighted_violation_sum / max_possible_score)

        # Clamp to [0.0, 1.0]
        return max(0.0, min(1.0, raw_score))

    def _create_result(
        self,
        is_compliant: bool,
        violations: list[ComplianceViolation],
        total_fields: int,
        fields_with_violations: int | None = None,
        metadata: dict[str, Any] | None = None,
    ) -> ComplianceResult:
        """Construct a fully-populated ``ComplianceResult``.

        Convenience factory that fills in the regulation name, version,
        and compliance score automatically from the checker instance.

        Args:
            is_compliant: Overall pass/fail determination.
            violations: All violations detected during the check.
            total_fields: Total number of dataset fields evaluated.
            fields_with_violations: Number of fields that had violations.
                If ``None``, this is derived from the unique set of
                affected fields across all violations.
            metadata: Optional additional audit context.

        Returns:
            A ``ComplianceResult`` ready for downstream consumption.
        """
        if fields_with_violations is None:
            # Derive from unique affected fields across violations
            affected_field_set: set[str] = set()
            for violation in violations:
                affected_field_set.update(violation.affected_fields)
            fields_with_violations = len(affected_field_set)

        compliance_score = self._calculate_compliance_score(violations, total_fields)

        return ComplianceResult(
            regulation_type=self.regulation_type,
            is_compliant=is_compliant,
            compliance_score=compliance_score,
            violations=violations,
            checked_at=datetime.now(UTC),
            regulation_name=self.get_regulation_name(),
            regulation_version=self.get_regulation_version(),
            total_fields_checked=total_fields,
            fields_with_violations=fields_with_violations,
            metadata=metadata if metadata is not None else {},
        )


# ---------------------------------------------------------------------------
# Regulation Registry (Singleton)
# ---------------------------------------------------------------------------


class RegulationRegistry:
    """Singleton registry for regulation checker discovery and instantiation.

    Maintains a class-level mapping of ``RegulationType`` → checker class,
    and lazily instantiates singleton checker instances on first access.
    Regulation checker modules register themselves during package import
    via the auto-registration block at the bottom of this module.

    Example::

        # Registration (typically automatic during module import)
        RegulationRegistry.register(RegulationType.GDPR, GDPRRegulationChecker)

        # Retrieval
        checker = RegulationRegistry.get_checker(RegulationType.GDPR)
        result = checker.check_compliance(metadata, scan_results)

        # List available regulations
        available = RegulationRegistry.get_available_regulations()
    """

    _registry: dict[RegulationType, type[BaseRegulationChecker]] = {}
    _instances: dict[RegulationType, BaseRegulationChecker] = {}

    @classmethod
    def register(
        cls,
        regulation_type: RegulationType,
        checker_class: type[BaseRegulationChecker],
    ) -> None:
        """Register a regulation checker class for a given regulation type.

        If a checker is already registered for the same regulation type it
        will be replaced, and any cached singleton instance will be evicted
        so the next ``get_checker`` call creates a fresh instance.

        Args:
            regulation_type: The ``RegulationType`` this checker handles.
            checker_class: A concrete subclass of ``BaseRegulationChecker``.

        Raises:
            TypeError: If ``checker_class`` is not a subclass of
                ``BaseRegulationChecker``.
        """
        if not (isinstance(checker_class, type) and issubclass(checker_class, BaseRegulationChecker)):
            raise TypeError(f"checker_class must be a subclass of BaseRegulationChecker, got {checker_class!r}")
        cls._registry[regulation_type] = checker_class
        # Evict any cached instance so the new class is used on next access
        cls._instances.pop(regulation_type, None)

    @classmethod
    def get_checker(cls, regulation_type: RegulationType) -> BaseRegulationChecker:
        """Return the singleton checker instance for the given regulation type.

        The instance is lazily created on first access and cached for
        subsequent calls.

        Args:
            regulation_type: The regulation to retrieve a checker for.

        Returns:
            The ``BaseRegulationChecker`` instance for the regulation.

        Raises:
            ValueError: If no checker is registered for the given type.
        """
        if regulation_type not in cls._registry:
            registered = [rt.value for rt in cls._registry]
            raise ValueError(
                f"No checker registered for regulation type '{regulation_type.value}'. Registered types: {registered}"
            )

        if regulation_type not in cls._instances:
            checker_class = cls._registry[regulation_type]
            cls._instances[regulation_type] = checker_class()

        return cls._instances[regulation_type]

    @classmethod
    def get_all_checkers(cls) -> dict[RegulationType, BaseRegulationChecker]:
        """Return singleton instances for all registered regulation checkers.

        Lazily instantiates any checkers that have not yet been accessed.

        Returns:
            Dictionary mapping each registered ``RegulationType`` to its
            checker instance.
        """
        for regulation_type in cls._registry:
            if regulation_type not in cls._instances:
                cls._instances[regulation_type] = cls._registry[regulation_type]()
        return dict(cls._instances)

    @classmethod
    def get_available_regulations(cls) -> list[RegulationType]:
        """Return the list of regulation types that have registered checkers.

        Returns:
            List of ``RegulationType`` values with registered checkers.
        """
        return list(cls._registry.keys())


# ---------------------------------------------------------------------------
# Auto-registration of regulation checkers
# ---------------------------------------------------------------------------
# Each regulation module (gdpr, hipaa, ccpa) is imported and registered
# individually with try/except so that partial registration is allowed
# when a specific regulation module is unavailable (e.g., missing optional
# NLP dependency).  This ensures the package remains importable even in
# degraded environments.

_GDPRRegulationChecker: type[BaseRegulationChecker] | None = None
_HIPAARegulationChecker: type[BaseRegulationChecker] | None = None
_CCPARegulationChecker: type[BaseRegulationChecker] | None = None

try:
    from compliance_service.regulations.gdpr import (
        GDPRRegulationChecker as _GDPRCls,
    )

    _GDPRRegulationChecker = _GDPRCls
    RegulationRegistry.register(RegulationType.GDPR, _GDPRRegulationChecker)
except ImportError:
    pass

try:
    from compliance_service.regulations.hipaa import (
        HIPAARegulationChecker as _HIPAACls,
    )

    _HIPAARegulationChecker = _HIPAACls
    RegulationRegistry.register(RegulationType.HIPAA, _HIPAARegulationChecker)
except ImportError:
    pass

try:
    from compliance_service.regulations.ccpa import (
        CCPARegulationChecker as _CCPACls,
    )

    _CCPARegulationChecker = _CCPACls
    RegulationRegistry.register(RegulationType.CCPA, _CCPARegulationChecker)
except ImportError:
    pass

# Re-export the checker classes (or None if not available)
GDPRRegulationChecker = _GDPRRegulationChecker
HIPAARegulationChecker = _HIPAARegulationChecker
CCPARegulationChecker = _CCPARegulationChecker


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

__all__ = [
    "BaseRegulationChecker",
    "CCPARegulationChecker",
    "ComplianceResult",
    "ComplianceViolation",
    "GDPRRegulationChecker",
    "HIPAARegulationChecker",
    "RegulationRegistry",
    "RegulationType",
    "Severity",
]
