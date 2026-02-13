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
"""Semantic version of the Compliance Service package."""

__all__: list[str] = ["__version__", "create_app"]
"""Public API surface exported by the ``compliance_service`` package."""


def __getattr__(name: str) -> object:
    """Lazily import heavy sub-modules to avoid circular dependencies.

    Uses PEP 562 module-level ``__getattr__`` so that the ``create_app``
    factory — which depends on Flask, MongoDB, Redis, and many internal
    sub-modules — is only imported when first accessed rather than at
    package import time.  This prevents circular import chains when
    sibling modules reference the package before it is fully initialised.

    Args:
        name: The attribute name being requested.

    Returns:
        The requested module-level attribute.

    Raises:
        AttributeError: If *name* is not a recognised public attribute
            of this package.
    """
    if name == "create_app":
        from compliance_service.app import create_app  # noqa: PLC0415

        # Cache in the module globals so subsequent accesses bypass
        # __getattr__ entirely, matching normal attribute lookup speed.
        globals()["create_app"] = create_app
        return create_app

    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
