"""Compliance Service — PII detection and regulatory compliance verification.

This microservice implements the compliance layer of the Synthetic ERP Data
Generation Platform with the following capabilities:

* **Dual-layer PII detection** — spaCy NLP entity recognition combined with
  regex pattern matching for SSN, email, phone, financial accounts, and
  address patterns.
* **Regulatory framework verification** — pluggable checkers for GDPR, HIPAA,
  and CCPA that evaluate generated datasets against regulation-specific rules.
* **Dataset compliance certification** — tamper-evident SHA-256 signed
  certificates attesting that a generated dataset passes all applicable
  regulatory checks.
* **Audit event logging** — immutable, timestamped audit trail written to the
  ``audit_logs`` MongoDB collection with 7-year retention for SOC 2 Type II
  compliance.
* **Compliance state machine** — each dataset progresses through the stages
  ``Pending → Scanning → PIICheck → Certified → Released``.

Usage::

    from compliance_service import create_app

    app = create_app()
    app.run()
"""

from __future__ import annotations


__version__: str = "1.0.0"
"""Semantic version of the Compliance Service."""

# ---------------------------------------------------------------------------
# Convenience imports
# ---------------------------------------------------------------------------
# The ``create_app`` factory depends on Flask and several sub-modules that may
# not yet be present during incremental project generation.  A guarded import
# keeps the package importable in all scenarios.
# ---------------------------------------------------------------------------

__all__: list[str] = ["__version__"]

try:
    from compliance_service.app import create_app

    __all__.append("create_app")
except ImportError:
    pass
