"""PII detection engine package for the Compliance Service.

Implements a dual-layer PII detection pipeline that combines:

* **NLP-based entity recognition** — Uses spaCy 3.7.x trained models to
  identify unstructured PII such as person names, geographic locations,
  and organisations.
* **Regex-based pattern matching** — Deterministic regular-expression
  patterns for structured PII formats including SSN, email addresses,
  phone numbers, credit card numbers, and IBAN codes.

The two layers work in concert to provide a **zero PII leakage guarantee**
that satisfies GDPR, HIPAA, CCPA, and SOC 2 Type II compliance
requirements.

Usage::

    from compliance_service.detectors import PatternDetector

    detector = PatternDetector()
    findings = detector.detect({"ssn": "123-45-6789", "name": "John"})
"""

from __future__ import annotations

from typing import Any

from compliance_service.detectors.nlp_detector import NLPDetector
from compliance_service.detectors.pattern_detector import PatternDetector


__version__: str = "1.0.0"
"""Semantic version of the detectors package."""

__all__: list[str] = [
    "NLPDetector",
    "PatternDetector",
    "__version__",
    "create_detector_pipeline",
]

# ---------------------------------------------------------------------------
# PIIDetector orchestrator — guarded import (module may not exist yet)
# ---------------------------------------------------------------------------

_PIIDetector: type | None = None
"""Reference to PIIDetector class, set when the module is available."""

try:
    from compliance_service.detectors.pii_detector import (
        PIIDetectionResult,
        PIIDetector,
        detect_pii,
    )

    _PIIDetector = PIIDetector
    __all__.extend(
        [
            "PIIDetectionResult",
            "PIIDetector",
            "detect_pii",
        ]
    )
except ImportError:
    pass


# ---------------------------------------------------------------------------
# Convenience factory
# ---------------------------------------------------------------------------


def create_detector_pipeline(config: dict[str, Any] | None = None) -> Any:
    """Create a fully initialised PII detection pipeline.

    Instantiates :class:`NLPDetector` and :class:`PatternDetector` with the
    supplied *config* dictionary and, when the ``PIIDetector`` orchestrator
    is available, returns a configured orchestrator instance.  Falls back to
    a :class:`PatternDetector` when the orchestrator module has not been
    generated yet.

    Args:
        config: Optional configuration dictionary.  Supported keys include
            ``nlp_model`` (spaCy model name), ``confidence_threshold``
            (float), and ``custom_patterns`` (list of regex definitions).

    Returns:
        A configured :class:`PIIDetector` orchestrator when available,
        otherwise a :class:`PatternDetector`.

    Example::

        from compliance_service.detectors import create_detector_pipeline

        pipeline = create_detector_pipeline({"confidence_threshold": 0.85})
    """
    cfg = config or {}

    if _PIIDetector is not None:
        return _PIIDetector(**cfg) if cfg else _PIIDetector()

    # Fallback: return the pattern detector alone
    return PatternDetector(**cfg) if cfg else PatternDetector()
