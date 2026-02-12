"""Validator registry and package marker for the Quality Service validators.

Provides pluggable validation strategies implementing the **Strategy** pattern
for the Quality Service's weighted scoring model:

- **Statistical fidelity** (40%) — Distribution comparison, moment analysis,
  value range validation via Great Expectations and SciPy.
- **Business rules** (30%) — ERP-specific format, cross-field, domain,
  temporal, and conditional rule validation across four ERP modules.
- **Referential integrity** (30%) — Foreign key validity, orphan detection,
  cascade chain verification, and cross-module reference integrity.

All concrete validators extend :class:`BaseValidator` and are discoverable
through :data:`VALIDATOR_REGISTRY`.

Usage::

    from quality_service.validators import get_all_validators

    validators = get_all_validators()
    for v in validators:
        result = v.validate(generated_data, profile)
        print(f"{v.get_name()}: {result.score:.4f}")
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING

# ---------------------------------------------------------------------------
# Core imports (always available)
# ---------------------------------------------------------------------------
from quality_service.validators.base import BaseValidator, ValidationResult


# ---------------------------------------------------------------------------
# Concrete validator imports (conditionally loaded)
# ---------------------------------------------------------------------------
# Some concrete validators may not yet be created during incremental project
# build.  We import them conditionally so that this package initialises
# correctly regardless of which files exist at import time.

_StatisticalValidator = None
_BusinessRulesValidator = None
_ReferentialIntegrityValidator = None

try:
    from quality_service.validators.statistical_validator import (
        StatisticalValidator,
    )

    _StatisticalValidator = StatisticalValidator
except ImportError:
    StatisticalValidator = None

try:
    from quality_service.validators.business_rules_validator import (
        BusinessRulesValidator,
    )

    _BusinessRulesValidator = BusinessRulesValidator
except ImportError:
    BusinessRulesValidator = None  # type: ignore[assignment,misc]

try:
    from quality_service.validators.referential_integrity_validator import (
        ReferentialIntegrityValidator,
    )

    _ReferentialIntegrityValidator = ReferentialIntegrityValidator
except ImportError:
    ReferentialIntegrityValidator = None  # type: ignore[assignment,misc]

# ---------------------------------------------------------------------------
# Validator registry
# ---------------------------------------------------------------------------

VALIDATOR_REGISTRY: dict[str, type[BaseValidator]] = {}
"""Maps short names to concrete validator classes.

Only validators whose modules were successfully imported are registered.
"""

if _StatisticalValidator is not None:
    VALIDATOR_REGISTRY["statistical_fidelity"] = _StatisticalValidator

if _BusinessRulesValidator is not None:
    VALIDATOR_REGISTRY["business_rules"] = _BusinessRulesValidator

if _ReferentialIntegrityValidator is not None:
    VALIDATOR_REGISTRY["referential_integrity"] = _ReferentialIntegrityValidator

_logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Factory functions
# ---------------------------------------------------------------------------


def get_all_validators(config: dict | None = None) -> list[BaseValidator]:
    """Instantiate and return all registered validators.

    Args:
        config: Optional configuration dict forwarded to each validator's
            ``__init__``.

    Returns:
        List of configured :class:`BaseValidator` instances ready for use by
        :class:`~quality_service.scoring.quality_scorer.QualityScorer`.

    Raises:
        RuntimeError: If no validators are registered (all imports failed).
        ValueError: If the sum of registered validator weights does not
            equal ``1.0`` (within floating-point tolerance).
    """
    if not VALIDATOR_REGISTRY:
        raise RuntimeError(
            "No validators are registered.  Ensure that at least one "
            "concrete validator module is available on the Python path."
        )

    instances: list[BaseValidator] = []
    for name, validator_cls in VALIDATOR_REGISTRY.items():
        try:
            instance = validator_cls(config=config) if config else validator_cls()
            instances.append(instance)
        except Exception:
            _logger.exception("Failed to instantiate validator '%s'", name)
            raise

    # Verify weight invariant when all three validators are present.
    weight_sum = sum(v.get_weight() for v in instances)
    if abs(weight_sum - 1.0) > 1e-6 and len(instances) == 3:
        raise ValueError(
            f"Validator weights must sum to 1.0, got {weight_sum:.6f}. "
            f"Registered validators: "
            f"{[(v.get_name(), v.get_weight()) for v in instances]}"
        )

    return instances


def get_validator(name: str, config: dict | None = None) -> BaseValidator:
    """Retrieve and instantiate a specific validator by registry name.

    Args:
        name: Registry key (e.g. ``'statistical_fidelity'``,
            ``'business_rules'``, ``'referential_integrity'``).
        config: Optional configuration dict forwarded to the validator's
            ``__init__``.

    Returns:
        Configured :class:`BaseValidator` instance.

    Raises:
        ValueError: If *name* is not found in :data:`VALIDATOR_REGISTRY`.
    """
    if name not in VALIDATOR_REGISTRY:
        available = ", ".join(sorted(VALIDATOR_REGISTRY)) or "(none)"
        raise ValueError(
            f"Unknown validator '{name}'.  Available validators: {available}"
        )

    validator_cls = VALIDATOR_REGISTRY[name]
    return validator_cls(config=config) if config else validator_cls()


def register_validator(
    name: str, validator_class: type[BaseValidator]
) -> None:
    """Register a custom validator class at runtime.

    Allows dynamic registration of user-defined validators that extend
    :class:`BaseValidator`.

    Args:
        name: Short registry key for the validator.
        validator_class: A class that extends :class:`BaseValidator`.

    Raises:
        TypeError: If *validator_class* does not extend :class:`BaseValidator`.
        ValueError: If *name* is already registered.
    """
    if not (isinstance(validator_class, type) and issubclass(validator_class, BaseValidator)):
        raise TypeError(
            f"validator_class must be a subclass of BaseValidator, "
            f"got {validator_class!r}"
        )
    if name in VALIDATOR_REGISTRY:
        raise ValueError(
            f"Validator '{name}' is already registered.  Use a different "
            f"name or remove the existing registration first."
        )

    VALIDATOR_REGISTRY[name] = validator_class
    _logger.info("Registered custom validator '%s': %s", name, validator_class.__name__)


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

__all__: list[str] = [
    "VALIDATOR_REGISTRY",
    "BaseValidator",
    "BusinessRulesValidator",
    "ReferentialIntegrityValidator",
    "StatisticalValidator",
    "ValidationResult",
    "get_all_validators",
    "get_validator",
    "register_validator",
]
