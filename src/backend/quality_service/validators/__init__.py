"""Validator registry and package marker for the Quality Service validators.

Provides pluggable validation strategies implementing the **Strategy** pattern
for the Quality Service's weighted scoring model.  Three concrete validators
are registered, each responsible for a specific quality dimension:

- **Statistical fidelity** (40 % weight) — Distribution comparison, moment
  analysis, and value range validation via Great Expectations and SciPy.
  Registered as ``'statistical_fidelity'``.  See
  :class:`StatisticalValidator`.

- **Business rules** (30 % weight) — ERP-specific format, cross-field,
  domain, temporal, and conditional rule validation across the four initial
  ERP modules (Financial Accounting, HR, Sales & Distribution, Material
  Management).  Registered as ``'business_rules'``.  See
  :class:`BusinessRulesValidator`.

- **Referential integrity** (30 % weight) — Foreign key validity, orphan
  record detection, cascade chain verification, cardinality enforcement, and
  cross-module reference integrity.  Registered as
  ``'referential_integrity'``.  See :class:`ReferentialIntegrityValidator`.

Composite quality score formula::

    Q = 0.4 * S_statistical + 0.3 * S_business_rules + 0.3 * S_referential_integrity

All concrete validators extend :class:`BaseValidator` and are discoverable
through :data:`VALIDATOR_REGISTRY`.  The scoring package
(:class:`~quality_service.scoring.quality_scorer.QualityScorer`) uses the
:func:`get_all_validators` factory to obtain configured validator instances.

Usage::

    from quality_service.validators import get_all_validators, get_validator

    # Obtain all three validators ready for scoring
    validators = get_all_validators()
    for v in validators:
        result = v.validate(generated_data, profile)
        print(f"{v.get_name()}: {result.score:.4f} "
              f"(weighted: {result.weighted_score:.4f})")

    # Obtain a single validator by name
    stat = get_validator('statistical_fidelity')
    result = stat.validate_with_timing(generated_data, profile)
    print(f"Passed: {result.passed}, Errors: {result.errors}")

    # Register a custom validator at runtime
    from quality_service.validators import register_validator, BaseValidator

    class CustomValidator(BaseValidator):
        def validate(self, generated_data, profile):
            return self.create_result(
                score=0.99, details={"custom": True},
            )
        def get_weight(self):
            return 0.0  # supplementary, zero weight
        def get_name(self):
            return "custom_validator"

    register_validator('custom', CustomValidator)
"""

from __future__ import annotations

import logging
from typing import Any

# ---------------------------------------------------------------------------
# Core imports — base classes and result model
# ---------------------------------------------------------------------------
from quality_service.validators.base import BaseValidator, ValidationResult
from quality_service.validators.business_rules_validator import (
    BusinessRulesValidator,
)
from quality_service.validators.referential_integrity_validator import (
    ReferentialIntegrityValidator,
)

# ---------------------------------------------------------------------------
# Concrete validator imports
# ---------------------------------------------------------------------------
from quality_service.validators.statistical_validator import (
    StatisticalValidator,
)


# ---------------------------------------------------------------------------
# Module-level logger
# ---------------------------------------------------------------------------
_logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Validator registry — maps short names to concrete validator classes
# ---------------------------------------------------------------------------

VALIDATOR_REGISTRY: dict[str, type[BaseValidator]] = {
    "statistical_fidelity": StatisticalValidator,
    "business_rules": BusinessRulesValidator,
    "referential_integrity": ReferentialIntegrityValidator,
}
"""Maps short registry names to concrete validator classes.

Keys correspond to the quality dimensions evaluated by the scoring model.
Values are *uninstantiated* :class:`BaseValidator` subclasses that are
instantiated on demand by the factory functions.

Standard entries:

=============================  =============================  ======
Key                            Class                          Weight
=============================  =============================  ======
``statistical_fidelity``       :class:`StatisticalValidator`   0.40
``business_rules``             :class:`BusinessRulesValidator`  0.30
``referential_integrity``      :class:`ReferentialIntegrityValidator` 0.30
=============================  =============================  ======
"""

# ---------------------------------------------------------------------------
# Weight tolerance for floating-point comparison
# ---------------------------------------------------------------------------
_WEIGHT_SUM_TOLERANCE: float = 1e-6
_EXPECTED_WEIGHT_SUM: float = 1.0


# ---------------------------------------------------------------------------
# Factory functions
# ---------------------------------------------------------------------------


def get_all_validators(config: dict[str, Any] | None = None) -> list[BaseValidator]:
    """Instantiate and return all registered validators.

    Creates an instance of every validator class in :data:`VALIDATOR_REGISTRY`,
    forwarding the optional *config* dictionary to each constructor.  Before
    returning, verifies the **weight invariant**: the sum of all validator
    weights must equal ``1.0`` (within floating-point tolerance) to guarantee
    correct composite scoring.

    This is the primary entry point used by
    :class:`~quality_service.scoring.quality_scorer.QualityScorer` to obtain
    the full set of validation strategies.

    Args:
        config: Optional configuration dictionary forwarded to each
            validator's ``__init__``.  Recognised keys depend on the
            concrete validator; common keys include
            ``minimum_threshold`` (float).

    Returns:
        List of configured :class:`BaseValidator` instances, one per
        registered validator, ready for invocation via
        :meth:`~BaseValidator.validate` or
        :meth:`~BaseValidator.validate_with_timing`.

    Raises:
        RuntimeError: If :data:`VALIDATOR_REGISTRY` is empty (no validators
            are registered).
        ValueError: If the sum of validator weights does not equal ``1.0``
            (within tolerance of ±1e-6).

    Example::

        validators = get_all_validators(config={"minimum_threshold": 0.90})
        for v in validators:
            result = v.validate(generated_data, profile)
            print(v.get_name(), result.score, result.weighted_score)
    """
    if not VALIDATOR_REGISTRY:
        raise RuntimeError(
            "No validators are registered. Ensure that at least one "
            "concrete validator module is available on the Python path."
        )

    instances: list[BaseValidator] = []
    for name, validator_cls in VALIDATOR_REGISTRY.items():
        try:
            instance = validator_cls(config=config) if config else validator_cls()
            instances.append(instance)
            _logger.debug(
                "Instantiated validator '%s' (weight=%.2f, threshold=%.2f)",
                instance.get_name(),
                instance.get_weight(),
                instance.minimum_threshold,
            )
        except Exception:
            _logger.exception("Failed to instantiate validator '%s'", name)
            raise

    # Verify weight invariant: all weights must sum to exactly 1.0
    weight_sum = sum(v.get_weight() for v in instances)
    if abs(weight_sum - _EXPECTED_WEIGHT_SUM) > _WEIGHT_SUM_TOLERANCE:
        validator_details = [
            (v.get_name(), v.get_weight()) for v in instances
        ]
        raise ValueError(
            f"Validator weights must sum to {_EXPECTED_WEIGHT_SUM}, "
            f"got {weight_sum:.6f}. "
            f"Registered validators: {validator_details}"
        )

    _logger.info(
        "Loaded %d validators with total weight %.2f",
        len(instances),
        weight_sum,
    )

    return instances


def get_validator(
    name: str,
    config: dict[str, Any] | None = None,
) -> BaseValidator:
    """Retrieve and instantiate a specific validator by registry name.

    Looks up *name* in :data:`VALIDATOR_REGISTRY`, instantiates the
    corresponding validator class with the optional *config*, and returns
    the configured instance.

    Args:
        name: Registry key identifying the validator.  Must be one of the
            keys in :data:`VALIDATOR_REGISTRY` (e.g.
            ``'statistical_fidelity'``, ``'business_rules'``,
            ``'referential_integrity'``).
        config: Optional configuration dictionary forwarded to the
            validator's ``__init__``.

    Returns:
        A configured :class:`BaseValidator` instance.

    Raises:
        ValueError: If *name* is not found in :data:`VALIDATOR_REGISTRY`.

    Example::

        stat_validator = get_validator('statistical_fidelity')
        result = stat_validator.validate_with_timing(data, profile)
        print(result.score, result.passed, result.errors, result.warnings)
    """
    if name not in VALIDATOR_REGISTRY:
        available = ", ".join(sorted(VALIDATOR_REGISTRY)) or "(none)"
        raise ValueError(
            f"Unknown validator '{name}'. "
            f"Available validators: {available}"
        )

    validator_cls = VALIDATOR_REGISTRY[name]
    instance = validator_cls(config=config) if config else validator_cls()

    _logger.debug(
        "Created validator '%s' (class=%s, weight=%.2f)",
        name,
        validator_cls.__name__,
        instance.get_weight(),
    )

    return instance


def register_validator(
    name: str,
    validator_class: type[BaseValidator],
) -> None:
    """Register a custom validator class at runtime.

    Enables dynamic extension of the validation pipeline by adding
    user-defined validators that conform to the :class:`BaseValidator`
    interface.  The new validator becomes immediately available through
    :func:`get_all_validators` and :func:`get_validator`.

    .. note::

        Dynamically registered validators will affect the weight-sum
        invariant check in :func:`get_all_validators`.  If the new
        validator carries a non-zero weight, ensure the total weight
        across all registered validators still sums to ``1.0``.

    Args:
        name: Short registry key for the validator.  Must not conflict
            with an existing key in :data:`VALIDATOR_REGISTRY`.
        validator_class: A class that is a subclass of
            :class:`BaseValidator`.  Must implement ``validate()``,
            ``get_weight()``, and ``get_name()``.

    Raises:
        TypeError: If *validator_class* is not a class or does not extend
            :class:`BaseValidator`.
        ValueError: If *name* is already registered in
            :data:`VALIDATOR_REGISTRY`.

    Example::

        from quality_service.validators import register_validator, BaseValidator

        class MyValidator(BaseValidator):
            def validate(self, generated_data, profile):
                return self.create_result(score=1.0, details={})
            def get_weight(self):
                return 0.0
            def get_name(self):
                return "my_validator"

        register_validator('my_custom', MyValidator)
    """
    if not isinstance(validator_class, type) or not issubclass(
        validator_class, BaseValidator
    ):
        raise TypeError(
            f"validator_class must be a subclass of BaseValidator, "
            f"got {validator_class!r}"
        )

    if name in VALIDATOR_REGISTRY:
        raise ValueError(
            f"Validator '{name}' is already registered. Use a different "
            f"name or remove the existing registration first."
        )

    VALIDATOR_REGISTRY[name] = validator_class
    _logger.info(
        "Registered custom validator '%s': %s",
        name,
        validator_class.__name__,
    )


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
