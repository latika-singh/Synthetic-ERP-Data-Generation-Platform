"""Dataset compliance certification engine for the Compliance Service.

Implements the compliance state machine that gates every dataset through
the ``Pending → Scanning → PIICheck → Certified → Released`` workflow with
``Failed`` and ``Revoked`` as terminal/recoverable states.

The engine orchestrates:

* **PII detection** — Dual-layer scanning via :class:`PIIDetector` (spaCy NLP
  + regex pattern matching) to ensure zero PII leakage.
* **Regulatory verification** — GDPR, HIPAA, and CCPA compliance checks via
  :class:`RegulationRegistry` and its registered
  :class:`BaseRegulationChecker` implementations.
* **Tamper-evident certification** — Each :class:`ComplianceCertificate` is
  sealed with a SHA-256 hash (optionally HMAC-SHA256 when a signing key is
  configured) and linked to the previous certificate via a hash chain for
  SOC 2 Type II tamper evidence.
* **Audit logging** — Every state transition, scan result, and lifecycle
  event is recorded in the MongoDB ``audit_logs`` collection via
  :class:`AuditLogger` with 7-year retention.

State Machine Transitions::

    PENDING   → [SCANNING]
    SCANNING  → [PII_CHECK, FAILED]
    PII_CHECK → [CERTIFIED, FAILED]
    CERTIFIED → [RELEASED, REVOKED]
    RELEASED  → [REVOKED]
    FAILED    → [PENDING]   (retry)
    REVOKED   → [PENDING]   (re-certify)

Usage::

    from compliance_service.certification.certifier import ComplianceCertifier

    certifier = ComplianceCertifier()
    certificate = certifier.certify_dataset(
        dataset_id="ds-001",
        dataset_metadata={"tables": [...], "columns": [...]},
        data_sample=[{"name": "Jane Doe", "email": "jane@example.com"}],
        tenant_id="tenant-42",
        user_id="user-abc",
        regulations=["GDPR", "HIPAA"],
    )
    print(certificate.state)           # ComplianceState.CERTIFIED or FAILED
    print(certificate.overall_compliant)

See Also:
    ``compliance_service.detectors.pii_detector`` — Dual-layer PII detection.
    ``compliance_service.regulations`` — Regulatory framework strategy pattern.
    ``compliance_service.certification.audit_logger`` — Audit event logging.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import time
import uuid
from datetime import UTC, datetime, timezone
from enum import Enum
from typing import Any, Dict, List, Optional

from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import padding
from cryptography.hazmat.primitives.asymmetric.rsa import RSAPrivateKey
from pydantic import BaseModel, Field

from compliance_service.certification.audit_logger import AuditEventType, AuditLogger
from compliance_service.detectors import PIIDetectionResult, PIIDetector
from compliance_service.regulations import (
    BaseRegulationChecker,
    ComplianceResult,
    RegulationRegistry,
    RegulationType,
)
from shared.database.mongodb import get_mongo_db
from shared.logging.structured_logger import get_logger


# ---------------------------------------------------------------------------
# Compliance State Machine Enumeration
# ---------------------------------------------------------------------------


class ComplianceState(Enum):
    """States for the compliance certification state machine.

    Each dataset progresses through a well-defined lifecycle::

        PENDING → SCANNING → PII_CHECK → CERTIFIED → RELEASED

    With ``FAILED`` and ``REVOKED`` as terminal (but recoverable) states.

    Attributes:
        PENDING: Initial state; dataset awaits certification.
        SCANNING: PII detection scan is in progress.
        PII_CHECK: PII scan results are being evaluated.
        CERTIFIED: Dataset passed all compliance checks.
        RELEASED: Certified dataset has been approved for distribution.
        FAILED: Dataset failed one or more compliance checks.
        REVOKED: A previously certified/released certificate was revoked.
    """

    PENDING = "Pending"
    SCANNING = "Scanning"
    PII_CHECK = "PIICheck"
    CERTIFIED = "Certified"
    RELEASED = "Released"
    FAILED = "Failed"
    REVOKED = "Revoked"


# ---------------------------------------------------------------------------
# Pydantic Data Models
# ---------------------------------------------------------------------------


class StateTransition(BaseModel):
    """Record of a single state machine transition.

    Captures the from/to states, timestamp, reason, and the identity of the
    actor who triggered the transition for audit trail purposes.

    Attributes:
        from_state: The state before the transition.
        to_state: The state after the transition.
        timestamp: UTC timestamp when the transition occurred.
        reason: Human-readable explanation for the transition.
        performed_by: User or service that triggered the transition.

    Example::

        transition = StateTransition(
            from_state=ComplianceState.PENDING,
            to_state=ComplianceState.SCANNING,
            timestamp=datetime.now(UTC),
            reason="Initiating PII scan",
            performed_by="user-abc",
        )
    """

    from_state: ComplianceState = Field(
        ...,
        description="State before the transition.",
    )
    to_state: ComplianceState = Field(
        ...,
        description="State after the transition.",
    )
    timestamp: datetime = Field(
        default_factory=lambda: datetime.now(UTC),
        description="UTC timestamp of the transition.",
    )
    reason: str = Field(
        default="",
        description="Explanation for the transition.",
    )
    performed_by: str = Field(
        default="",
        description="User or service that triggered the transition.",
    )

    model_config = {"use_enum_values": True}


class ComplianceCertificate(BaseModel):
    """Immutable compliance certificate for a synthetic dataset.

    Each certificate represents the outcome of the full compliance
    verification pipeline for a single dataset.  Certificates form a hash
    chain via ``previous_certificate_hash`` to provide tamper evidence that
    satisfies SOC 2 Type II requirements.

    Attributes:
        certificate_id: Globally-unique UUID-4 identifier.
        dataset_id: Identifier of the certified dataset.
        tenant_id: Tenant namespace for multi-tenant isolation.
        state: Current state in the compliance state machine.
        dataset_fingerprint: SHA-256 fingerprint of the dataset content.
        pii_scan_results: Summary of the PII detection scan.
        regulatory_results: Per-regulation compliance check results.
        overall_compliant: Final compliance determination.
        compliance_score: Weighted compliance score in ``[0.0, 1.0]``.
        regulations_checked: List of regulation types that were verified.
        certified_at: UTC timestamp of certification (``None`` until certified).
        certified_by: User or service that performed the certification.
        certificate_hash: Tamper-evident SHA-256 hash of this certificate.
        previous_certificate_hash: Hash chain link to the prior certificate.
        metadata: Additional context key-value pairs.
        transitions: Ordered list of state transitions for audit trail.

    Example::

        cert = ComplianceCertificate(
            dataset_id="ds-001",
            tenant_id="tenant-42",
            state=ComplianceState.PENDING,
            dataset_fingerprint="abc123...",
            pii_scan_results={},
            regulatory_results={},
            overall_compliant=False,
            compliance_score=0.0,
            regulations_checked=[],
            certified_by="system",
            certificate_hash="",
        )
    """

    certificate_id: str = Field(
        default_factory=lambda: str(uuid.uuid4()),
        description="Globally-unique UUID-4 identifier.",
    )
    dataset_id: str = Field(
        ...,
        description="Identifier of the certified dataset.",
    )
    tenant_id: str = Field(
        ...,
        description="Tenant namespace for multi-tenant isolation.",
    )
    state: ComplianceState = Field(
        default=ComplianceState.PENDING,
        description="Current state in the compliance state machine.",
    )
    dataset_fingerprint: str = Field(
        default="",
        description="SHA-256 fingerprint of the dataset content/metadata.",
    )
    pii_scan_results: dict[str, Any] = Field(
        default_factory=dict,
        description="Summary of the PII detection scan.",
    )
    regulatory_results: dict[str, Any] = Field(
        default_factory=dict,
        description="Per-regulation compliance check results.",
    )
    overall_compliant: bool = Field(
        default=False,
        description="Final compliance determination.",
    )
    compliance_score: float = Field(
        default=0.0,
        ge=0.0,
        le=1.0,
        description="Weighted compliance score [0.0, 1.0].",
    )
    regulations_checked: list[str] = Field(
        default_factory=list,
        description="List of regulation types that were verified.",
    )
    certified_at: datetime | None = Field(
        default=None,
        description="UTC timestamp of certification.",
    )
    certified_by: str = Field(
        default="",
        description="User or service that performed the certification.",
    )
    certificate_hash: str = Field(
        default="",
        description="Tamper-evident SHA-256 hash of this certificate.",
    )
    previous_certificate_hash: str | None = Field(
        default=None,
        description="Hash chain link to the prior certificate.",
    )
    metadata: dict[str, Any] = Field(
        default_factory=dict,
        description="Additional context key-value pairs.",
    )
    transitions: list[StateTransition] = Field(
        default_factory=list,
        description="Ordered list of state transitions.",
    )

    model_config = {"use_enum_values": True}


# ---------------------------------------------------------------------------
# Valid state transitions lookup
# ---------------------------------------------------------------------------

_VALID_TRANSITIONS: dict[ComplianceState, list[ComplianceState]] = {
    ComplianceState.PENDING: [ComplianceState.SCANNING],
    ComplianceState.SCANNING: [ComplianceState.PII_CHECK, ComplianceState.FAILED],
    ComplianceState.PII_CHECK: [ComplianceState.CERTIFIED, ComplianceState.FAILED],
    ComplianceState.CERTIFIED: [ComplianceState.RELEASED, ComplianceState.REVOKED],
    ComplianceState.RELEASED: [ComplianceState.REVOKED],
    ComplianceState.FAILED: [ComplianceState.PENDING],
    ComplianceState.REVOKED: [ComplianceState.PENDING],
}


# ---------------------------------------------------------------------------
# Compliance Certifier
# ---------------------------------------------------------------------------


class ComplianceCertifier:
    """Compliance certification engine implementing the state machine workflow.

    Orchestrates the full ``Pending → Scanning → PIICheck → Certified →
    Released`` pipeline by combining PII detection, regulatory verification,
    tamper-evident certificate generation, and audit event logging.

    Args:
        pii_detector: Pre-configured PII detector instance.  When ``None``,
            a default :class:`PIIDetector` is lazily initialised.
        audit_logger: Pre-configured audit logger instance.  When ``None``,
            a default :class:`AuditLogger` is lazily initialised.
        signing_key: Optional HMAC signing key for certificate hashes.
            When provided, certificates are signed with HMAC-SHA256 instead
            of plain SHA-256.
        rsa_private_key_pem: Optional PEM-encoded RSA private key bytes for
            asymmetric certificate signing.  When provided, an RSA-PKCS1v15
            signature is appended to the certificate metadata alongside the
            standard SHA-256/HMAC hash for enhanced tamper evidence.

    Example::

        certifier = ComplianceCertifier()
        certificate = certifier.certify_dataset(
            dataset_id="ds-001",
            dataset_metadata={"tables": ["employees"]},
            data_sample=[{"name": "John", "ssn": "123-45-6789"}],
            tenant_id="tenant-42",
            user_id="user-abc",
        )
    """

    # Collection name in MongoDB for certificate persistence
    COLLECTION_NAME: str = "compliance_certificates"

    # Default set of supported regulation types for quick reference
    DEFAULT_REGULATION_TYPES: list[RegulationType] = [
        RegulationType.GDPR,
        RegulationType.HIPAA,
        RegulationType.CCPA,
    ]

    def __init__(
        self,
        pii_detector: PIIDetector | None = None,
        audit_logger: AuditLogger | None = None,
        signing_key: str | None = None,
        rsa_private_key_pem: bytes | None = None,
    ) -> None:
        """Initialise the compliance certifier.

        Args:
            pii_detector: Optional PII detector.  Defaults are created lazily.
            audit_logger: Optional audit logger.  Defaults are created lazily.
            signing_key: Optional HMAC signing key for tamper evidence.
            rsa_private_key_pem: Optional PEM-encoded RSA private key for
                asymmetric certificate signing via PKCS1v15 + SHA-256.
        """
        self._pii_detector: PIIDetector = pii_detector or PIIDetector()
        self._audit_logger: AuditLogger = audit_logger or AuditLogger()
        self._signing_key: str | None = signing_key
        self._rsa_private_key: RSAPrivateKey | None = None
        if rsa_private_key_pem:
            _loaded_key = serialization.load_pem_private_key(
                rsa_private_key_pem, password=None,
            )
            if not isinstance(_loaded_key, RSAPrivateKey):
                raise TypeError(
                    "Provided PEM key is not an RSA private key. "
                    f"Got {type(_loaded_key).__name__}.",
                )
            self._rsa_private_key = _loaded_key
        self._logger = get_logger(__name__)
        self._logger.info("compliance_certifier_initialised")

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def certify_dataset(
        self,
        dataset_id: str,
        dataset_metadata: dict[str, Any],
        data_sample: list[dict[str, Any]],
        tenant_id: str,
        user_id: str,
        regulations: list[str] | None = None,
    ) -> ComplianceCertificate:
        """Execute the full compliance certification pipeline.

        Drives the state machine from ``PENDING`` through ``SCANNING``,
        ``PII_CHECK``, and finally ``CERTIFIED`` (or ``FAILED`` on any
        compliance violation).  Every state transition is audit-logged.

        Args:
            dataset_id: Unique identifier of the dataset being certified.
            dataset_metadata: Schema information including tables, columns,
                data types, and generation profile configuration.
            data_sample: Representative sample records from the dataset
                used for PII detection scanning.
            tenant_id: Tenant namespace for multi-tenant isolation.
            user_id: Identity of the user initiating certification.
            regulations: List of regulation names to verify (e.g.
                ``["GDPR", "HIPAA"]``).  Defaults to all registered
                regulations when ``None``.

        Returns:
            A :class:`ComplianceCertificate` in either ``CERTIFIED`` or
            ``FAILED`` state depending on the outcome.

        Example::

            cert = certifier.certify_dataset(
                dataset_id="ds-001",
                dataset_metadata={"tables": ["employees"]},
                data_sample=[{"name": "Jane", "email": "jane@test.com"}],
                tenant_id="tenant-42",
                user_id="user-abc",
                regulations=["GDPR"],
            )
        """
        start_time = time.monotonic()
        regulation_names = self._resolve_regulation_names(regulations)

        certificate = self._build_pending_certificate(
            dataset_id, dataset_metadata, data_sample,
            tenant_id, user_id, regulation_names,
        )

        self._logger.info(
            "certification_started",
            dataset_id=dataset_id,
            tenant_id=tenant_id,
            regulations=regulation_names,
        )

        try:
            certificate = self._run_pipeline(
                certificate, dataset_metadata, data_sample,
                tenant_id, user_id, regulation_names,
            )
        except Exception as exc:
            certificate = self._handle_pipeline_error(
                certificate, exc, dataset_id, user_id,
            )

        return self._seal_and_persist(certificate, start_time, dataset_id)

    def release_certificate(
        self,
        certificate_id: str,
        user_id: str,
        tenant_id: str,
    ) -> ComplianceCertificate:
        """Transition a certified dataset to RELEASED state.

        Validates that the certificate exists and is in ``CERTIFIED`` state,
        then transitions to ``RELEASED`` and logs the release event.

        Args:
            certificate_id: The certificate to release.
            user_id: Identity of the user approving the release.
            tenant_id: Tenant namespace for isolation.

        Returns:
            The updated :class:`ComplianceCertificate` in ``RELEASED`` state.

        Raises:
            ValueError: If the certificate is not found or not in
                ``CERTIFIED`` state.
        """
        certificate = self._get_certificate(certificate_id, tenant_id)

        certificate = self._transition_state(
            certificate, ComplianceState.RELEASED,
            "Certificate released for distribution", user_id,
        )

        # Re-seal after state change
        certificate.certificate_hash = self._generate_certificate_hash(certificate)
        self._store_certificate(certificate)

        self._audit_logger.log_event(
            event_type=AuditEventType.CERTIFICATE_RELEASED,
            tenant_id=tenant_id,
            user_id=user_id,
            dataset_id=certificate.dataset_id,
            certificate_id=certificate_id,
            details={"released_by": user_id},
        )

        self._logger.info(
            "certificate_released",
            certificate_id=certificate_id,
            tenant_id=tenant_id,
        )

        return certificate

    def revoke_certificate(
        self,
        certificate_id: str,
        reason: str,
        user_id: str,
        tenant_id: str,
    ) -> ComplianceCertificate:
        """Revoke a certified or released certificate.

        Transitions from ``CERTIFIED`` or ``RELEASED`` to ``REVOKED`` and
        records the revocation reason in the audit log.

        Args:
            certificate_id: The certificate to revoke.
            reason: Explanation for why the certificate is being revoked.
            user_id: Identity of the user performing the revocation.
            tenant_id: Tenant namespace for isolation.

        Returns:
            The updated :class:`ComplianceCertificate` in ``REVOKED`` state.

        Raises:
            ValueError: If the certificate is not found or not in a
                revocable state (``CERTIFIED`` or ``RELEASED``).
        """
        certificate = self._get_certificate(certificate_id, tenant_id)

        certificate = self._transition_state(
            certificate, ComplianceState.REVOKED, reason, user_id,
        )

        # Re-seal after state change
        certificate.certificate_hash = self._generate_certificate_hash(certificate)
        self._store_certificate(certificate)

        self._audit_logger.log_event(
            event_type=AuditEventType.CERTIFICATE_REVOKED,
            tenant_id=tenant_id,
            user_id=user_id,
            dataset_id=certificate.dataset_id,
            certificate_id=certificate_id,
            details={"reason": reason, "revoked_by": user_id},
        )

        self._logger.info(
            "certificate_revoked",
            certificate_id=certificate_id,
            tenant_id=tenant_id,
            reason=reason,
        )

        return certificate

    # ------------------------------------------------------------------
    # Pipeline Orchestration Helpers
    # ------------------------------------------------------------------

    def _resolve_regulation_names(
        self,
        regulations: list[str] | None,
    ) -> list[str]:
        """Determine which regulation names to check.

        When no explicit list is provided, defaults to the three core
        regulatory frameworks: :attr:`RegulationType.GDPR`,
        :attr:`RegulationType.HIPAA`, and :attr:`RegulationType.CCPA`.
        If the :class:`RegulationRegistry` has additional registered
        checkers, those are included as well.

        Args:
            regulations: Explicit list or ``None`` for all registered.

        Returns:
            List of regulation name strings.
        """
        if regulations is not None:
            return list(regulations)

        # Retrieve all regulations registered in the registry
        available = RegulationRegistry.get_available_regulations()
        if available:
            return [r.value for r in available]

        # Fallback to the three core regulation types when registry is empty
        return [
            RegulationType.GDPR.value,
            RegulationType.HIPAA.value,
            RegulationType.CCPA.value,
        ]

    def _build_pending_certificate(
        self,
        dataset_id: str,
        dataset_metadata: dict[str, Any],
        data_sample: list[dict[str, Any]],
        tenant_id: str,
        user_id: str,
        regulation_names: list[str],
    ) -> ComplianceCertificate:
        """Create a new certificate in PENDING state.

        Computes the dataset fingerprint and resolves the previous
        certificate hash for hash-chain linking.

        Args:
            dataset_id: Unique dataset identifier.
            dataset_metadata: Schema information.
            data_sample: Representative sample records.
            tenant_id: Tenant namespace.
            user_id: Certifying user.
            regulation_names: Regulations to verify.

        Returns:
            A fresh :class:`ComplianceCertificate` in ``PENDING`` state.
        """
        fingerprint = self._compute_dataset_fingerprint(
            dataset_metadata, data_sample,
        )
        previous_hash = self._get_previous_certificate_hash(
            dataset_id, tenant_id,
        )

        return ComplianceCertificate(
            dataset_id=dataset_id,
            tenant_id=tenant_id,
            state=ComplianceState.PENDING,
            dataset_fingerprint=fingerprint,
            certified_by=user_id,
            previous_certificate_hash=previous_hash,
            regulations_checked=regulation_names,
        )

    def _run_pipeline(
        self,
        certificate: ComplianceCertificate,
        dataset_metadata: dict[str, Any],
        data_sample: list[dict[str, Any]],
        tenant_id: str,
        user_id: str,
        regulation_names: list[str],
    ) -> ComplianceCertificate:
        """Drive PENDING → SCANNING → PII_CHECK → CERTIFIED / FAILED.

        Args:
            certificate: Certificate in ``PENDING`` state.
            dataset_metadata: Schema information.
            data_sample: Records to scan for PII.
            tenant_id: Tenant namespace.
            user_id: Certifying user.
            regulation_names: Regulations to verify.

        Returns:
            Certificate in ``CERTIFIED`` or ``FAILED`` state.
        """
        # PENDING → SCANNING
        certificate = self._transition_state(
            certificate, ComplianceState.SCANNING,
            "Initiating PII scan", user_id,
        )

        # Phase 1: PII scanning
        pii_result = self._execute_pii_scan(
            certificate, data_sample, tenant_id, user_id,
        )
        certificate.pii_scan_results = _summarise_pii_results(pii_result)

        # SCANNING → PII_CHECK
        certificate = self._transition_state(
            certificate, ComplianceState.PII_CHECK,
            "Evaluating PII scan results", user_id,
        )

        # Evaluate and log PII outcome
        pii_passed = self._check_pii(pii_result)
        self._log_pii_outcome(certificate, pii_result, tenant_id, user_id)

        if not pii_passed:
            return self._fail_on_pii(
                certificate, pii_result, tenant_id, user_id,
            )

        # Phase 2: Regulatory compliance
        return self._run_regulatory_phase(
            certificate, dataset_metadata,
            regulation_names, tenant_id, user_id, pii_passed,
        )

    def _execute_pii_scan(
        self,
        certificate: ComplianceCertificate,
        data_sample: list[dict[str, Any]],
        tenant_id: str,
        user_id: str,
    ) -> PIIDetectionResult:
        """Run PII detection with audit logging bookends.

        Args:
            certificate: Current certificate for identifiers.
            data_sample: Records to scan.
            tenant_id: Tenant namespace.
            user_id: Certifying user.

        Returns:
            PII detection result from the dual-layer detector.
        """
        self._audit_logger.log_event(
            event_type=AuditEventType.SCAN_INITIATED,
            tenant_id=tenant_id,
            user_id=user_id,
            dataset_id=certificate.dataset_id,
            certificate_id=certificate.certificate_id,
            details={
                "regulations": certificate.regulations_checked,
                "sample_size": len(data_sample),
            },
        )

        pii_result = self._scan_dataset(data_sample)

        self._audit_logger.log_event(
            event_type=AuditEventType.SCAN_COMPLETED,
            tenant_id=tenant_id,
            user_id=user_id,
            dataset_id=certificate.dataset_id,
            certificate_id=certificate.certificate_id,
            details={
                "has_pii": pii_result.has_pii,
                "total_findings": pii_result.total_findings,
                "scan_duration_ms": pii_result.scan_duration_ms,
            },
        )
        return pii_result

    def _log_pii_outcome(
        self,
        certificate: ComplianceCertificate,
        pii_result: PIIDetectionResult,
        tenant_id: str,
        user_id: str,
    ) -> None:
        """Emit ``PII_DETECTED`` or ``PII_CHECK_PASSED`` audit event.

        Args:
            certificate: Current certificate for identifiers.
            pii_result: PII detection result.
            tenant_id: Tenant namespace.
            user_id: Certifying user.
        """
        if pii_result.has_pii:
            self._audit_logger.log_event(
                event_type=AuditEventType.PII_DETECTED,
                tenant_id=tenant_id,
                user_id=user_id,
                dataset_id=certificate.dataset_id,
                certificate_id=certificate.certificate_id,
                details={
                    "pii_types": list(pii_result.pii_types_found),
                    "findings_count": pii_result.total_findings,
                },
            )
        else:
            self._audit_logger.log_event(
                event_type=AuditEventType.PII_CHECK_PASSED,
                tenant_id=tenant_id,
                user_id=user_id,
                dataset_id=certificate.dataset_id,
                certificate_id=certificate.certificate_id,
            )

    def _fail_on_pii(
        self,
        certificate: ComplianceCertificate,
        pii_result: PIIDetectionResult,
        tenant_id: str,
        user_id: str,
    ) -> ComplianceCertificate:
        """Transition to ``FAILED`` when PII is detected.

        Args:
            certificate: Certificate in ``PII_CHECK`` state.
            pii_result: PII findings that caused failure.
            tenant_id: Tenant namespace.
            user_id: Certifying user.

        Returns:
            Certificate in ``FAILED`` state with early-exit hash sealed.
        """
        certificate = self._transition_state(
            certificate, ComplianceState.FAILED,
            f"PII detected: {list(pii_result.pii_types_found)}",
            user_id,
        )
        certificate.overall_compliant = False
        certificate.compliance_score = 0.0

        self._audit_logger.log_event(
            event_type=AuditEventType.CERTIFICATION_FAILED,
            tenant_id=tenant_id,
            user_id=user_id,
            dataset_id=certificate.dataset_id,
            certificate_id=certificate.certificate_id,
            details={"reason": "PII detected in dataset"},
        )
        return certificate

    def _run_regulatory_phase(
        self,
        certificate: ComplianceCertificate,
        dataset_metadata: dict[str, Any],
        regulation_names: list[str],
        tenant_id: str,
        user_id: str,
        pii_passed: bool,
    ) -> ComplianceCertificate:
        """Run regulatory checks and decide CERTIFIED or FAILED.

        Args:
            certificate: Certificate in ``PII_CHECK`` state.
            dataset_metadata: Schema information.
            regulation_names: Regulations to verify.
            tenant_id: Tenant namespace.
            user_id: Certifying user.
            pii_passed: Whether the PII check passed.

        Returns:
            Certificate in ``CERTIFIED`` or ``FAILED`` state.
        """
        regulatory_results = self._verify_regulations(
            dataset_metadata, certificate.pii_scan_results, regulation_names,
        )

        reg_summary, all_compliant, avg_score = _aggregate_regulatory_results(
            regulatory_results,
        )
        certificate.regulatory_results = reg_summary
        certificate.compliance_score = avg_score

        if all_compliant and pii_passed:
            return self._certify_success(
                certificate, regulation_names, tenant_id, user_id,
            )
        return self._certify_failure(
            certificate, regulatory_results, tenant_id, user_id,
        )

    def _certify_success(
        self,
        certificate: ComplianceCertificate,
        regulation_names: list[str],
        tenant_id: str,
        user_id: str,
    ) -> ComplianceCertificate:
        """Transition to ``CERTIFIED`` on full compliance.

        Args:
            certificate: Certificate in ``PII_CHECK`` state.
            regulation_names: Regulations that were checked.
            tenant_id: Tenant namespace.
            user_id: Certifying user.

        Returns:
            Certificate in ``CERTIFIED`` state.
        """
        certificate = self._transition_state(
            certificate, ComplianceState.CERTIFIED,
            "All compliance checks passed", user_id,
        )
        certificate.overall_compliant = True
        certificate.certified_at = datetime.now(UTC)

        self._audit_logger.log_event(
            event_type=AuditEventType.CERTIFICATION_ISSUED,
            tenant_id=tenant_id,
            user_id=user_id,
            dataset_id=certificate.dataset_id,
            certificate_id=certificate.certificate_id,
            details={
                "compliance_score": certificate.compliance_score,
                "regulations_checked": regulation_names,
            },
        )
        return certificate

    def _certify_failure(
        self,
        certificate: ComplianceCertificate,
        regulatory_results: dict[str, ComplianceResult],
        tenant_id: str,
        user_id: str,
    ) -> ComplianceCertificate:
        """Transition to ``FAILED`` on regulatory non-compliance.

        Args:
            certificate: Certificate in ``PII_CHECK`` state.
            regulatory_results: Per-regulation results containing failures.
            tenant_id: Tenant namespace.
            user_id: Certifying user.

        Returns:
            Certificate in ``FAILED`` state.
        """
        failed_regs = [
            name
            for name, res in regulatory_results.items()
            if not res.is_compliant
        ]
        certificate = self._transition_state(
            certificate, ComplianceState.FAILED,
            f"Regulatory compliance failed: {failed_regs}",
            user_id,
        )
        certificate.overall_compliant = False

        self._audit_logger.log_event(
            event_type=AuditEventType.CERTIFICATION_FAILED,
            tenant_id=tenant_id,
            user_id=user_id,
            dataset_id=certificate.dataset_id,
            certificate_id=certificate.certificate_id,
            details={
                "failed_regulations": failed_regs,
                "compliance_score": certificate.compliance_score,
            },
        )
        return certificate

    def _handle_pipeline_error(
        self,
        certificate: ComplianceCertificate,
        exc: Exception,
        dataset_id: str,
        user_id: str,
    ) -> ComplianceCertificate:
        """Handle unexpected errors by transitioning to ``FAILED``.

        Args:
            certificate: Certificate at point of failure.
            exc: The exception that was raised.
            dataset_id: Dataset identifier for logging.
            user_id: User for transition attribution.

        Returns:
            Certificate in ``FAILED`` state.
        """
        self._logger.error(
            "certification_error",
            dataset_id=dataset_id,
            error=str(exc),
        )
        try:
            certificate = self._transition_state(
                certificate, ComplianceState.FAILED,
                f"Unexpected error: {exc!s}", user_id,
            )
        except ValueError:
            # Already in FAILED or cannot transition — set directly
            certificate.state = ComplianceState.FAILED
        certificate.overall_compliant = False
        certificate.compliance_score = 0.0
        return certificate

    def _seal_and_persist(
        self,
        certificate: ComplianceCertificate,
        start_time: float,
        dataset_id: str,
    ) -> ComplianceCertificate:
        """Compute certificate hash, record timing, persist, and log.

        Args:
            certificate: The certificate to finalise.
            start_time: Monotonic clock start for duration measurement.
            dataset_id: Dataset identifier for logging.

        Returns:
            The sealed and persisted certificate.
        """
        certificate.certificate_hash = self._generate_certificate_hash(
            certificate,
        )

        # Attach optional RSA digital signature for enhanced tamper evidence
        rsa_sig = self._sign_certificate_rsa(
            certificate.certificate_hash.encode("utf-8"),
        )
        if rsa_sig is not None:
            certificate.metadata["rsa_signature"] = rsa_sig.hex()

        certificate.metadata["certification_duration_ms"] = round(
            (time.monotonic() - start_time) * 1000, 2,
        )
        # Record wall-clock completion timestamp for observability
        certificate.metadata["completed_at_epoch"] = time.time()

        self._store_certificate(certificate)

        state_val = (
            certificate.state.value
            if isinstance(certificate.state, ComplianceState)
            else certificate.state
        )
        self._logger.info(
            "certification_completed",
            dataset_id=dataset_id,
            certificate_id=certificate.certificate_id,
            state=state_val,
            overall_compliant=certificate.overall_compliant,
            compliance_score=certificate.compliance_score,
        )
        return certificate

    # ------------------------------------------------------------------
    # State Machine
    # ------------------------------------------------------------------

    def _transition_state(
        self,
        certificate: ComplianceCertificate,
        new_state: ComplianceState,
        reason: str,
        user_id: str,
    ) -> ComplianceCertificate:
        """Validate and execute a state machine transition.

        Checks the transition against the valid transition map, creates a
        :class:`StateTransition` record, and logs the event via the audit
        logger.

        Args:
            certificate: The certificate being transitioned.
            new_state: The desired target state.
            reason: Human-readable explanation for the transition.
            user_id: User or service triggering the transition.

        Returns:
            The certificate with updated state and transition record.

        Raises:
            ValueError: If the transition from current state to ``new_state``
                is not permitted by the state machine.
        """
        current_state = certificate.state
        if isinstance(current_state, str):
            current_state = ComplianceState(current_state)

        allowed = _VALID_TRANSITIONS.get(current_state, [])
        if new_state not in allowed:
            msg = (
                f"Invalid state transition: {current_state.value} → "
                f"{new_state.value}. "
                f"Allowed transitions from {current_state.value}: "
                f"{[s.value for s in allowed]}"
            )
            raise ValueError(msg)

        transition = StateTransition(
            from_state=current_state,
            to_state=new_state,
            timestamp=datetime.now(UTC),
            reason=reason,
            performed_by=user_id,
        )

        certificate.state = new_state
        certificate.transitions.append(transition)

        # Log transition to audit
        self._audit_logger.log_event(
            event_type=AuditEventType.STATE_TRANSITION,
            tenant_id=certificate.tenant_id,
            user_id=user_id,
            dataset_id=certificate.dataset_id,
            certificate_id=certificate.certificate_id,
            details={
                "from_state": current_state.value,
                "to_state": new_state.value,
                "reason": reason,
            },
        )

        self._logger.debug(
            "state_transition",
            certificate_id=certificate.certificate_id,
            from_state=current_state.value,
            to_state=new_state.value,
            reason=reason,
        )

        return certificate

    # ------------------------------------------------------------------
    # PII Detection
    # ------------------------------------------------------------------

    def _scan_dataset(
        self,
        data_sample: list[dict[str, Any]],
        field_names: list[str] | None = None,
    ) -> PIIDetectionResult:
        """Run the dual-layer PII detector on the data sample.

        Delegates to :class:`PIIDetector` which combines NLP-based entity
        recognition and regex-based pattern matching.

        Args:
            data_sample: List of record dictionaries to scan.
            field_names: Optional subset of field names to scan.
                When ``None``, all fields in each record are scanned.

        Returns:
            A :class:`PIIDetectionResult` with findings, scores, and metrics.
        """
        if not data_sample:
            return PIIDetectionResult(
                has_pii=False,
                total_findings=0,
                records_scanned=0,
            )

        # If field_names specified, filter data
        if field_names:
            filtered_sample = [
                {k: v for k, v in record.items() if k in field_names}
                for record in data_sample
            ]
        else:
            filtered_sample = data_sample

        # Use the dual-layer PII detection pipeline (NLP + regex)
        return self._pii_detector.detect_pii(filtered_sample)

    def _check_pii(self, scan_results: PIIDetectionResult) -> bool:
        """Evaluate PII scan results for compliance.

        Returns ``True`` (pass) when no PII is detected, ``False`` (fail)
        when PII is found in the dataset.

        Args:
            scan_results: Result from :meth:`_scan_dataset`.

        Returns:
            ``True`` if dataset is PII-free, ``False`` otherwise.
        """
        return not scan_results.has_pii

    # ------------------------------------------------------------------
    # Regulatory Verification
    # ------------------------------------------------------------------

    def _verify_regulations(
        self,
        dataset_metadata: dict[str, Any],
        scan_results: dict[str, Any],
        regulations: list[str],
    ) -> dict[str, ComplianceResult]:
        """Run regulatory compliance checks for specified regulations.

        Iterates over the requested regulation names, resolves each to
        a :class:`BaseRegulationChecker` via :class:`RegulationRegistry`,
        and executes ``check_compliance`` against the dataset metadata
        and PII scan results.

        Args:
            dataset_metadata: Schema and configuration information.
            scan_results: PII detection results dictionary.
            regulations: List of regulation type names (e.g. ``["GDPR"]``).

        Returns:
            Dictionary mapping regulation name to :class:`ComplianceResult`.
        """
        results: dict[str, ComplianceResult] = {}

        for reg_name in regulations:
            result = self._check_single_regulation(
                reg_name, dataset_metadata, scan_results,
            )
            if result is not None:
                results[reg_name] = result

        return results

    def _check_single_regulation(
        self,
        reg_name: str,
        dataset_metadata: dict[str, Any],
        scan_results: dict[str, Any],
    ) -> ComplianceResult | None:
        """Check compliance for a single regulation type.

        Resolves the regulation name to a checker, runs the compliance
        check, and logs the start/completion/violation audit events.

        Args:
            reg_name: Regulation type name (e.g. ``"GDPR"``).
            dataset_metadata: Schema and configuration information.
            scan_results: PII detection results dictionary.

        Returns:
            :class:`ComplianceResult` or ``None`` if the regulation
            type is unknown or has no registered checker.
        """
        try:
            reg_type = RegulationType(reg_name)
        except ValueError:
            self._logger.warning("unknown_regulation_type", regulation=reg_name)
            return None

        try:
            checker: BaseRegulationChecker = RegulationRegistry.get_checker(
                reg_type,
            )
        except ValueError:
            self._logger.warning("no_checker_registered", regulation=reg_name)
            return None

        tid = dataset_metadata.get("tenant_id", "unknown")
        uid = dataset_metadata.get("user_id", "system")
        did = dataset_metadata.get("dataset_id", "unknown")

        self._audit_logger.log_event(
            event_type=AuditEventType.REGULATION_CHECK_STARTED,
            tenant_id=tid, user_id=uid, dataset_id=did,
            details={"regulation": reg_name},
        )

        result = checker.check_compliance(dataset_metadata, scan_results)

        self._audit_logger.log_event(
            event_type=AuditEventType.REGULATION_CHECK_COMPLETED,
            tenant_id=tid, user_id=uid, dataset_id=did,
            details={
                "regulation": reg_name,
                "is_compliant": result.is_compliant,
                "compliance_score": result.compliance_score,
                "violations_count": len(result.violations),
            },
        )

        if not result.is_compliant:
            self._audit_logger.log_event(
                event_type=AuditEventType.REGULATION_VIOLATION_FOUND,
                tenant_id=tid, user_id=uid, dataset_id=did,
                details={
                    "regulation": reg_name,
                    "violations": [
                        {
                            "severity": (
                                v.severity.value
                                if hasattr(v.severity, "value")
                                else str(v.severity)
                            ),
                            "description": v.description,
                            "article_reference": v.article_reference,
                        }
                        for v in result.violations
                    ],
                },
            )

        return result

    # ------------------------------------------------------------------
    # Certificate Hashing and Fingerprinting
    # ------------------------------------------------------------------

    def _generate_certificate_hash(
        self,
        certificate: ComplianceCertificate,
    ) -> str:
        """Compute the tamper-evident hash for a compliance certificate.

        Serializes all certificate fields except ``certificate_hash`` to
        deterministic JSON (sorted keys) and computes a SHA-256 digest.
        When a signing key is configured, HMAC-SHA256 is used instead of
        plain SHA-256 for enhanced tamper evidence.

        Args:
            certificate: The certificate to hash.

        Returns:
            Hex-encoded SHA-256 or HMAC-SHA256 digest string.
        """
        state_val = (
            certificate.state.value
            if isinstance(certificate.state, ComplianceState)
            else certificate.state
        )
        hash_fields = {
            "certificate_id": certificate.certificate_id,
            "dataset_id": certificate.dataset_id,
            "tenant_id": certificate.tenant_id,
            "state": state_val,
            "dataset_fingerprint": certificate.dataset_fingerprint,
            "pii_scan_results": certificate.pii_scan_results,
            "regulatory_results": certificate.regulatory_results,
            "overall_compliant": certificate.overall_compliant,
            "compliance_score": certificate.compliance_score,
            "regulations_checked": certificate.regulations_checked,
            "certified_at": (
                certificate.certified_at.isoformat()
                if certificate.certified_at
                else None
            ),
            "certified_by": certificate.certified_by,
            "previous_certificate_hash": certificate.previous_certificate_hash,
            "metadata": certificate.metadata,
        }

        canonical = json.dumps(
            hash_fields, sort_keys=True, default=str,
        ).encode("utf-8")

        if self._signing_key:
            return hmac.new(
                self._signing_key.encode("utf-8"),
                canonical,
                hashlib.sha256,
            ).hexdigest()

        return hashlib.sha256(canonical).hexdigest()

    def verify_certificate_hash(
        self,
        certificate: ComplianceCertificate,
    ) -> bool:
        """Verify the tamper-evident hash of a compliance certificate.

        Recomputes the certificate hash using the same deterministic
        serialisation and compares it to the stored hash using
        :func:`hmac.compare_digest` for constant-time comparison that
        prevents timing side-channel attacks.

        Args:
            certificate: The certificate whose hash integrity to verify.

        Returns:
            ``True`` if the stored ``certificate_hash`` matches the
            recomputed hash, ``False`` if the certificate has been
            tampered with.

        Example::

            is_valid = certifier.verify_certificate_hash(certificate)
            if not is_valid:
                raise SecurityError("Certificate tampered with!")
        """
        expected_hash = self._generate_certificate_hash(certificate)
        return hmac.compare_digest(
            certificate.certificate_hash, expected_hash,
        )

    def _sign_certificate_rsa(
        self,
        data: bytes,
    ) -> bytes | None:
        """Sign certificate data using RSA PKCS#1 v1.5 with SHA-256.

        Uses the ``cryptography`` library's :func:`padding.PKCS1v15`
        and :class:`hashes.SHA256` to produce a digital signature when
        an RSA private key was provided during initialisation.

        Args:
            data: The byte string to sign (typically the certificate
                hash digest).

        Returns:
            The RSA signature bytes, or ``None`` when no RSA private
            key is configured.

        Example::

            sig = certifier._sign_certificate_rsa(b"certificate-hash")
            if sig:
                certificate.metadata["rsa_signature"] = sig.hex()
        """
        if self._rsa_private_key is None:
            return None

        try:
            signature: bytes = self._rsa_private_key.sign(
                data,
                padding.PKCS1v15(),
                hashes.SHA256(),
            )
            return signature
        except Exception as exc:
            self._logger.warning(
                "rsa_signing_failed",
                error=str(exc),
            )
            return None

    def _compute_dataset_fingerprint(
        self,
        dataset_metadata: dict[str, Any],
        data_sample: list[dict[str, Any]],
    ) -> str:
        """Compute a SHA-256 fingerprint of the dataset content.

        Combines the dataset schema metadata with a representative sample
        of the data to produce a unique identifier for the dataset version
        being certified.

        Args:
            dataset_metadata: Schema information (tables, columns, types).
            data_sample: Representative sample records.

        Returns:
            Hex-encoded SHA-256 digest string.
        """
        fingerprint_data = {
            "metadata": dataset_metadata,
            "sample_hash": hashlib.sha256(
                json.dumps(
                    data_sample, sort_keys=True, default=str,
                ).encode("utf-8"),
            ).hexdigest(),
        }
        canonical = json.dumps(
            fingerprint_data, sort_keys=True, default=str,
        ).encode("utf-8")
        return hashlib.sha256(canonical).hexdigest()

    # ------------------------------------------------------------------
    # MongoDB Persistence
    # ------------------------------------------------------------------

    def _store_certificate(
        self,
        certificate: ComplianceCertificate,
    ) -> None:
        """Persist a compliance certificate to MongoDB.

        Uses ``upsert`` semantics so that the same certificate can be
        stored multiple times (e.g. after state transitions) without
        creating duplicates.

        Args:
            certificate: The certificate to persist.
        """
        try:
            db = get_mongo_db()
            collection = db[self.COLLECTION_NAME]

            doc = certificate.model_dump(mode="json")

            collection.update_one(
                {
                    "certificate_id": certificate.certificate_id,
                    "tenant_id": certificate.tenant_id,
                },
                {"$set": doc},
                upsert=True,
            )

            self._logger.debug(
                "certificate_stored",
                certificate_id=certificate.certificate_id,
                tenant_id=certificate.tenant_id,
            )
        except Exception as exc:
            self._logger.error(
                "certificate_store_error",
                certificate_id=certificate.certificate_id,
                error=str(exc),
            )

    def _get_certificate(
        self,
        certificate_id: str,
        tenant_id: str,
    ) -> ComplianceCertificate:
        """Retrieve a certificate from MongoDB by ID and tenant.

        Args:
            certificate_id: The certificate identifier.
            tenant_id: Tenant namespace for isolation.

        Returns:
            The :class:`ComplianceCertificate` instance.

        Raises:
            ValueError: If the certificate is not found or belongs to a
                different tenant.
        """
        db = get_mongo_db()
        collection = db[self.COLLECTION_NAME]

        doc = collection.find_one(
            {"certificate_id": certificate_id, "tenant_id": tenant_id},
        )

        if doc is None:
            raise ValueError(
                f"Certificate '{certificate_id}' not found "
                f"for tenant '{tenant_id}'",
            )

        # Remove MongoDB _id field
        doc.pop("_id", None)
        return ComplianceCertificate(**doc)

    def _get_previous_certificate_hash(
        self,
        dataset_id: str,
        tenant_id: str,
    ) -> str | None:
        """Retrieve the most recent certificate hash for hash chain linking.

        Args:
            dataset_id: The dataset to look up.
            tenant_id: Tenant namespace for isolation.

        Returns:
            The ``certificate_hash`` of the most recent certificate for this
            dataset, or ``None`` if no prior certificates exist.
        """
        try:
            db = get_mongo_db()
            collection = db[self.COLLECTION_NAME]

            doc = collection.find_one(
                {"dataset_id": dataset_id, "tenant_id": tenant_id},
                sort=[("certified_at", -1)],
                projection={"certificate_hash": 1},
            )

            if doc and doc.get("certificate_hash"):
                return str(doc["certificate_hash"])
        except Exception as exc:
            self._logger.warning(
                "previous_hash_lookup_error",
                dataset_id=dataset_id,
                error=str(exc),
            )

        return None


# ---------------------------------------------------------------------------
# Module-level helpers (stateless)
# ---------------------------------------------------------------------------


def _summarise_pii_results(pii_result: PIIDetectionResult) -> dict[str, Any]:
    """Build a serialisable summary dictionary from PII detection results.

    Args:
        pii_result: The detection result to summarise.

    Returns:
        Dictionary with PII scan metrics.
    """
    return {
        "has_pii": pii_result.has_pii,
        "total_findings": pii_result.total_findings,
        "pii_types_found": list(pii_result.pii_types_found),
        "scan_duration_ms": pii_result.scan_duration_ms,
        "records_scanned": pii_result.records_scanned,
        "nlp_findings_count": pii_result.nlp_findings_count,
        "pattern_findings_count": pii_result.pattern_findings_count,
    }


def _aggregate_regulatory_results(
    regulatory_results: dict[str, ComplianceResult],
) -> tuple[dict[str, Any], bool, float]:
    """Aggregate individual regulation results into a summary.

    Args:
        regulatory_results: Per-regulation compliance results.

    Returns:
        Tuple of ``(summary_dict, all_compliant, average_score)``.
    """
    reg_summary: dict[str, Any] = {}
    all_compliant = True
    total_score = 0.0
    score_count = 0

    for reg_name, result in regulatory_results.items():
        reg_summary[reg_name] = {
            "is_compliant": result.is_compliant,
            "compliance_score": result.compliance_score,
            "violations_count": len(result.violations),
            "regulation_name": result.regulation_name,
            "regulation_version": result.regulation_version,
            "total_fields_checked": result.total_fields_checked,
            "fields_with_violations": result.fields_with_violations,
        }
        if not result.is_compliant:
            all_compliant = False
        total_score += result.compliance_score
        score_count += 1

    avg_score = total_score / max(score_count, 1)
    return reg_summary, all_compliant, avg_score
