"""Generators package — pluggable strategy pattern for synthetic data generation.

This package implements the **Strategy pattern** for the Synthetic ERP Data
Generation Platform, providing four concrete generation strategies that extend
the :class:`BaseGenerator` abstract base class:

1. **AI/ML Generator** (``'ai_ml'``) — GAN/VAE-based synthetic data generation
   using PyTorch and TensorFlow for learning and reproducing complex data
   distributions with high statistical fidelity.

2. **Rules Generator** (``'rules'``) — Business rules engine for constraint-based
   generation that enforces domain-specific validation rules, ERP document number
   formats, value ranges, cross-field dependencies, and lookup tables.

3. **Statistical Generator** (``'statistical'``) — Distribution-fitting synthesis
   using SciPy/NumPy for column-wise parametric distribution matching with
   Gaussian copula-based multivariate correlation preservation.

4. **Masking Generator** (``'masking'``) — Intelligent data masking with privacy-
   preserving transformations including format-preserving encryption (FPE), value
   substitution, tokenization, generalization, perturbation, redaction, shuffling,
   and date shifting for zero-PII guarantee.

Each generator implements the common interface defined by :class:`BaseGenerator`:

- ``generate(schema, profile, num_records, **kwargs) -> GenerationResult``
- ``validate_config(config) -> bool``
- ``get_capabilities() -> Dict[str, Any]``

The :data:`GENERATOR_REGISTRY` dictionary maps method name strings to their
corresponding generator classes, enabling dynamic strategy selection at runtime
by the orchestrator's method selector.

Usage::

    from generation_engine.generators import get_generator, get_available_methods

    # List all registered generation methods
    methods = get_available_methods()  # ['ai_ml', 'masking', 'rules', 'statistical']

    # Instantiate a generator by method name
    gen = get_generator("rules", config={"erp_module": "financial_accounting"})
    result = gen.generate(schema=schema, profile=profile, num_records=10000)

    # Access the registry directly
    from generation_engine.generators import GENERATOR_REGISTRY
    generator_cls = GENERATOR_REGISTRY["statistical"]
"""

from __future__ import annotations

from typing import Any

# ---------------------------------------------------------------------------
# Eager imports from base module — lightweight (no torch/tensorflow/scipy deps).
# BaseGenerator is needed as the return type annotation for get_generator().
# GenerationError is co-imported for exception handling and re-export.
# GeneratorType is the type alias (type[BaseGenerator]) used for registry typing.
# ---------------------------------------------------------------------------
from generation_engine.generators.base import (
    BaseGenerator,
    GenerationError,
    GeneratorType,
)

# ---------------------------------------------------------------------------
# Public API surface — all symbols importable via
#   ``from generation_engine.generators import <name>``
# ---------------------------------------------------------------------------
__all__: list[str] = [
    "BaseGenerator",
    "AIMLGenerator",
    "RulesGenerator",
    "StatisticalGenerator",
    "MaskingGenerator",
    "GENERATOR_REGISTRY",
    "get_generator",
    "get_available_methods",
]


# ---------------------------------------------------------------------------
# Lazy Registry Construction
# ---------------------------------------------------------------------------
# Concrete generator modules pull in heavy dependencies (PyTorch, TensorFlow,
# SciPy, cryptography).  The registry is built lazily on first access so that
# importing the generators package itself remains fast and doesn't force
# installation of every ML/scientific computing library.
# ---------------------------------------------------------------------------

_REGISTRY: dict[str, GeneratorType] | None = None


def _build_registry() -> dict[str, GeneratorType]:
    """Build the ``GENERATOR_REGISTRY`` by attempting to import each generator.

    Generators whose dependencies are unavailable (e.g. PyTorch not installed)
    are silently skipped, allowing the platform to run in environments where
    only a subset of generation methods is available.

    Returns:
        A dictionary mapping method name strings to their corresponding
        :class:`BaseGenerator` subclass types.
    """
    registry: dict[str, GeneratorType] = {}

    # AI/ML generator — requires torch and tensorflow
    try:
        from generation_engine.generators.ai_ml_generator import (  # noqa: PLC0415
            AIMLGenerator,
        )

        registry["ai_ml"] = AIMLGenerator
    except ImportError:
        pass

    # Rules generator — lightweight, should always be available
    try:
        from generation_engine.generators.rules_generator import (  # noqa: PLC0415
            RulesGenerator,
        )

        registry["rules"] = RulesGenerator
    except ImportError:
        pass

    # Statistical generator — requires scipy
    try:
        from generation_engine.generators.statistical_generator import (  # noqa: PLC0415
            StatisticalGenerator,
        )

        registry["statistical"] = StatisticalGenerator
    except ImportError:
        pass

    # Masking generator — requires cryptography
    try:
        from generation_engine.generators.masking_generator import (  # noqa: PLC0415
            MaskingGenerator,
        )

        registry["masking"] = MaskingGenerator
    except ImportError:
        pass

    return registry


def _get_registry() -> dict[str, GeneratorType]:
    """Return (and lazily construct) the global generator registry.

    The registry is a module-level singleton built on first access.  Subsequent
    calls return the same dictionary without rebuilding.

    Returns:
        The ``GENERATOR_REGISTRY`` dictionary mapping method name strings to
        generator class references typed as :data:`GeneratorType`.
    """
    global _REGISTRY  # noqa: PLW0603
    if _REGISTRY is None:
        _REGISTRY = _build_registry()
    return _REGISTRY


# ---------------------------------------------------------------------------
# Public Factory API
# ---------------------------------------------------------------------------


def get_generator(method_name: str, **kwargs: Any) -> BaseGenerator:
    """Instantiate a generator by its registered method name.

    Looks up *method_name* in the :data:`GENERATOR_REGISTRY` and returns a
    new instance of the corresponding :class:`BaseGenerator` subclass,
    forwarding any additional keyword arguments to the constructor.

    This factory function is the primary entry point used by the orchestrator's
    :class:`~generation_engine.orchestrator.method_selector.MethodSelector` to
    dynamically instantiate the appropriate generator at runtime.

    Args:
        method_name: One of ``'ai_ml'``, ``'rules'``, ``'statistical'``,
            or ``'masking'``.
        **kwargs: Keyword arguments forwarded to the generator constructor
            (e.g. ``config``, ``seed``, ``erp_module``).

    Returns:
        An instance of the requested :class:`BaseGenerator` subclass, fully
        initialised and ready for ``generate()`` calls.

    Raises:
        ValueError: If *method_name* is not a registered generation method.
            The error message lists all available methods for discoverability.

    Example::

        generator = get_generator("statistical", config={"seed": 42})
        result = generator.generate(schema=my_schema, profile=my_profile, num_records=5000)
    """
    registry: dict[str, GeneratorType] = _get_registry()
    generator_class: GeneratorType | None = registry.get(method_name)

    if generator_class is None:
        available: list[str] = sorted(registry.keys())
        raise ValueError(
            f"Unknown generation method {method_name!r}. "
            f"Available methods: {available}"
        )

    return generator_class(**kwargs)


def get_available_methods() -> list[str]:
    """Return a sorted list of all registered generation method names.

    Useful for populating UI dropdowns, validating API request payloads,
    and diagnostic logging.

    Returns:
        A sorted list of method name strings (e.g.
        ``['ai_ml', 'masking', 'rules', 'statistical']``).
    """
    return sorted(_get_registry().keys())


# ---------------------------------------------------------------------------
# Lazy Attribute Resolution
# ---------------------------------------------------------------------------
# Concrete generator class names (AIMLGenerator, RulesGenerator, etc.) are
# listed in __all__ for convenient star-imports and IDE auto-completion.
# However, they are resolved lazily via __getattr__ to avoid importing heavy
# dependencies until the symbol is actually accessed.
# ---------------------------------------------------------------------------


def __getattr__(name: str) -> Any:
    """Lazily import and return concrete generator classes and the registry.

    This module-level ``__getattr__`` (PEP 562) enables deferred loading of
    concrete generator classes.  When a consumer writes::

        from generation_engine.generators import AIMLGenerator

    Python calls ``__getattr__("AIMLGenerator")`` which triggers the import
    of ``ai_ml_generator`` only at that point, keeping the initial package
    import lightweight.

    Args:
        name: The attribute name being accessed on this module.

    Returns:
        The requested class, constant, or callable.

    Raises:
        AttributeError: If *name* is not a recognised public attribute of
            this package.
    """
    if name == "AIMLGenerator":
        from generation_engine.generators.ai_ml_generator import (  # noqa: PLC0415
            AIMLGenerator,
        )

        return AIMLGenerator

    if name == "RulesGenerator":
        from generation_engine.generators.rules_generator import (  # noqa: PLC0415
            RulesGenerator,
        )

        return RulesGenerator

    if name == "StatisticalGenerator":
        from generation_engine.generators.statistical_generator import (  # noqa: PLC0415
            StatisticalGenerator,
        )

        return StatisticalGenerator

    if name == "MaskingGenerator":
        from generation_engine.generators.masking_generator import (  # noqa: PLC0415
            MaskingGenerator,
        )

        return MaskingGenerator

    if name == "GENERATOR_REGISTRY":
        return _get_registry()

    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
