"""Certification package for the Compliance Service.

Provides dataset compliance certification with tamper-evident SHA-256 signing
and comprehensive audit event logging for SOC 2 Type II compliance.

Core capabilities:

* **Compliance state machine** — Each dataset progresses through
  ``Pending → Scanning → PIICheck → Certified → Released``, with ``Failed``
  and ``Revoked`` as terminal/recoverable states.
* **Tamper-evident certificates** — Immutable :class:`ComplianceCertificate`
  objects linked via a SHA-256 hash chain to prevent undetected modification.
* **Audit event logging** — Every state transition, PII scan, regulatory
  verification, and certificate lifecycle event is recorded in the MongoDB
  ``audit_logs`` collection with a 7-year TTL for SOC 2 Type II retention.
* **Hash chain integrity verification** — :meth:`AuditLogger.verify_chain_integrity`
  can validate that no audit entries have been tampered with.

Usage::

    from compliance_service.certification import AuditLogger, AuditEventType

    logger = AuditLogger()
    entry = logger.log_event(
        event_type=AuditEventType.SCAN_INITIATED,
        tenant_id="tenant-001",
        user_id="user-42",
        dataset_id="ds-abc",
    )
"""

from __future__ import annotations

from compliance_service.certification.audit_logger import (
    AuditEntry,
    AuditEventType,
    AuditLogger,
    AuditQueryParams,
    AuditQueryResult,
    log_compliance_event,
)


__version__: str = "1.0.0"
"""Semantic version of the certification package."""

__all__: list[str] = [
    "AuditEntry",
    "AuditEventType",
    "AuditLogger",
    "AuditQueryParams",
    "AuditQueryResult",
    "__version__",
    "log_compliance_event",
]

# ---------------------------------------------------------------------------
# Certifier module — guarded import (module may not be generated yet)
# ---------------------------------------------------------------------------

try:
    from compliance_service.certification.certifier import (
        ComplianceCertificate,
        ComplianceCertifier,
        ComplianceState,
        StateTransition,
    )

    __all__.extend(
        [
            "ComplianceCertificate",
            "ComplianceCertifier",
            "ComplianceState",
            "StateTransition",
        ]
    )
except ImportError:
    pass
