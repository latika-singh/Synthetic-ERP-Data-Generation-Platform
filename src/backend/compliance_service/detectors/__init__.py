"""PII detection engine package for the Compliance Service.

This package implements a **dual-layer PII detection pipeline** that combines
two complementary detection strategies to guarantee **zero PII leakage** in
all generated synthetic datasets:

1. **NLP-based entity recognition** — Uses spaCy 3.7.x trained NER models to
   identify unstructured PII such as person names, geographic locations,
   addresses, and organisation names.  Implemented in
   :class:`NLPDetector`.

2. **Regex-based pattern matching** — Deterministic regular-expression
   patterns for structured PII formats including SSN, email addresses,
   phone numbers, credit card numbers (with Luhn validation), and IBAN
   codes.  Implemented in :class:`PatternDetector`.

The two layers are orchestrated by the :class:`PIIDetector` class, which
runs both detectors against every text field, deduplicates overlapping
findings (keeping the highest-confidence match when both layers detect the
same PII span), and applies a configurable confidence threshold before
producing a comprehensive :class:`PIIDetectionResult`.

This dual-layer approach satisfies compliance requirements for:

* **GDPR** — General Data Protection Regulation (EU)
* **HIPAA** — Health Insurance Portability and Accountability Act (US)
* **CCPA** — California Consumer Privacy Act (US)
* **SOC 2 Type II** — Service Organization Control audit standard

Exported Public API:

* :class:`PIIDetector` — Orchestrator that combines both detection layers.
* :class:`PIIDetectionResult` — Aggregate detection result data model.
* :func:`detect_pii` — Module-level convenience function for one-shot scans.
* :class:`NLPDetector` — spaCy NLP entity recognition detector.
* :class:`PatternDetector` — Regex-based pattern matching detector.
* :func:`create_detector_pipeline` — Factory for initialising a configured
  detection pipeline in a single call.

Usage::

    # Import the orchestrator and scan a record
    from compliance_service.detectors import PIIDetector

    detector = PIIDetector()
    result = detector.detect_pii({"name": "John Smith", "ssn": "123-45-6789"})
    print(result.has_pii)          # True
    print(result.total_findings)   # >= 2
    print(result.pii_types_found)  # {'PERSON_NAME', 'SSN', ...}

    # Quick one-shot scan via convenience function
    from compliance_service.detectors import detect_pii

    result = detect_pii({"email": "user@example.com"})

    # Factory-based initialisation with custom config
    from compliance_service.detectors import create_detector_pipeline

    pipeline = create_detector_pipeline({
        "nlp_model": "en_core_web_lg",
        "confidence_threshold": 0.90,
    })
    result = pipeline.detect_pii({"note": "Jane Doe lives in Berlin"})

See Also:
    ``compliance_service.detectors.pii_detector`` — Dual-layer orchestrator.
    ``compliance_service.detectors.nlp_detector`` — NLP detection layer.
    ``compliance_service.detectors.pattern_detector`` — Regex detection layer.
    ``compliance_service.certification.certifier`` — Compliance state machine
    that uses this detection pipeline to gate dataset certification.
"""

from __future__ import annotations

from typing import Any

from compliance_service.detectors.nlp_detector import NLPDetector
from compliance_service.detectors.pattern_detector import PatternDetector
from compliance_service.detectors.pii_detector import (
    PIIDetectionResult,
    PIIDetector,
    detect_pii,
)


__version__: str = "1.0.0"
"""Semantic version of the ``compliance_service.detectors`` package."""

__all__: list[str] = [
    "NLPDetector",
    "PIIDetectionResult",
    "PIIDetector",
    "PatternDetector",
    "__version__",
    "create_detector_pipeline",
    "detect_pii",
]


# ---------------------------------------------------------------------------
# Convenience Factory
# ---------------------------------------------------------------------------


def create_detector_pipeline(
    config: dict[str, Any] | None = None,
) -> PIIDetector:
    """Create a fully initialised dual-layer PII detection pipeline.

    This is a convenience factory that instantiates :class:`NLPDetector`
    and :class:`PatternDetector` from a single configuration dictionary,
    wires them together into a :class:`PIIDetector` orchestrator, and
    returns the ready-to-use pipeline.  It is the recommended way to set
    up the detection pipeline for services that do not need fine-grained
    control over each detector instance.

    Supported configuration keys (all optional):

    * ``nlp_model`` *(str)* — spaCy model name for the NLP detector.
      Defaults to ``"en_core_web_sm"``.
    * ``pii_entity_types`` *(list[str])* — spaCy entity labels to treat
      as PII in the NLP detector (e.g. ``["PERSON", "GPE", "ORG"]``).
    * ``nlp_confidence_threshold`` *(float)* — Minimum confidence for the
      NLP detector to include an entity.  Defaults to ``0.85``.
    * ``custom_patterns`` *(list)* — Additional regex pattern definitions
      to register in the pattern detector.
    * ``confidence_threshold`` *(float)* — Orchestrator-level minimum
      confidence for findings to appear in the final result.  Defaults
      to ``0.85``.

    Args:
        config: Optional configuration dictionary.  When ``None`` or
            empty, all defaults are used.

    Returns:
        A fully configured :class:`PIIDetector` orchestrator with both
        the NLP and pattern detection layers initialised and ready for
        scanning.

    Example::

        from compliance_service.detectors import create_detector_pipeline

        # Use all defaults
        pipeline = create_detector_pipeline()
        result = pipeline.detect_pii({"ssn": "123-45-6789"})

        # Custom spaCy model and stricter threshold
        pipeline = create_detector_pipeline({
            "nlp_model": "en_core_web_lg",
            "confidence_threshold": 0.90,
        })

        # With additional regex patterns for tenant-specific requirements
        pipeline = create_detector_pipeline({
            "custom_patterns": [
                {
                    "name": "PASSPORT_US",
                    "regex": r"\\b[A-Z]\\d{8}\\b",
                    "pii_type": "PASSPORT",
                    "confidence": 0.85,
                    "description": "US passport number",
                }
            ],
        })
    """
    cfg: dict[str, Any] = config if config is not None else {}

    # --- Build the NLP detector with relevant config keys ---
    nlp_kwargs: dict[str, Any] = {}
    if "nlp_model" in cfg:
        nlp_kwargs["model_name"] = cfg["nlp_model"]
    if "pii_entity_types" in cfg:
        nlp_kwargs["pii_entity_types"] = cfg["pii_entity_types"]
    if "nlp_confidence_threshold" in cfg:
        nlp_kwargs["confidence_threshold"] = cfg["nlp_confidence_threshold"]

    nlp_detector = NLPDetector(**nlp_kwargs)

    # --- Build the pattern detector with relevant config keys ---
    pattern_kwargs: dict[str, Any] = {}
    if "custom_patterns" in cfg:
        pattern_kwargs["custom_patterns"] = cfg["custom_patterns"]

    pattern_detector = PatternDetector(**pattern_kwargs)

    # --- Build the orchestrator with both detectors ---
    orchestrator_threshold: float = cfg.get("confidence_threshold", 0.85)

    return PIIDetector(
        nlp_detector=nlp_detector,
        pattern_detector=pattern_detector,
        confidence_threshold=orchestrator_threshold,
    )
