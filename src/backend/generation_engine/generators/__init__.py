"""Generator strategies sub-package for the Synthetic ERP Data Generation Platform.

This package implements the **Strategy pattern** for synthetic data generation,
providing four concrete generation strategies that extend the
:class:`BaseGenerator` abstract base class:

1. **AI/ML Generator** — GAN/VAE-based synthetic data generation using
   PyTorch/TensorFlow for learning and reproducing complex data distributions.

2. **Rules Generator** — Business rules engine for constraint-based generation
   that enforces domain-specific validation rules and value constraints.

3. **Statistical Generator** — Statistical distribution-based synthesis using
   SciPy/NumPy for column-wise distribution fitting with copula-based
   multivariate correlation preservation.

4. **Masking Generator** — Intelligent data masking with privacy-preserving
   transformations including format-preserving encryption (FPE), value
   substitution, tokenization, generalization, perturbation, redaction,
   shuffling, and date shifting.

Each generator implements the common interface defined by :class:`BaseGenerator`:
    - ``generate(schema, profile, num_records, **kwargs) -> GenerationResult``
    - ``validate_config(config) -> bool``
    - ``get_capabilities() -> Dict[str, Any]``

Usage::

    from generation_engine.generators import get_generator

    gen = get_generator("rules", erp_module="financial_accounting")
    result = gen.generate(schema=schema, profile=profile, num_records=1000)
"""

from __future__ import annotations

from typing import Any


__version__: str = "1.0.0"

__all__: list[str] = [
    "GENERATOR_REGISTRY",
    "AIMLGenerator",
    "BaseGenerator",
    "ColumnSpec",
    "GenerationConfig",
    "GenerationError",
    "GenerationResult",
    "MaskingConfig",
    "MaskingGenerator",
    "MaskingStrategy",
    "RulesGenerator",
    "StatisticalGenerator",
    "get_available_methods",
    "get_generator",
]


# ---------------------------------------------------------------------------
# Lazy-import helpers
# ---------------------------------------------------------------------------

def _import_base(name: str) -> Any:
    """Resolve a symbol from :mod:`generation_engine.generators.base`."""
    from generation_engine.generators.base import (  # noqa: PLC0415
        BaseGenerator,
        ColumnSpec,
        GenerationConfig,
        GenerationError,
        GenerationResult,
    )
    _map: dict[str, Any] = {
        "BaseGenerator": BaseGenerator,
        "ColumnSpec": ColumnSpec,
        "GenerationConfig": GenerationConfig,
        "GenerationError": GenerationError,
        "GenerationResult": GenerationResult,
    }
    return _map.get(name)


def _import_masking(name: str) -> Any:
    """Resolve a symbol from :mod:`generation_engine.generators.masking_generator`."""
    from generation_engine.generators.masking_generator import (  # noqa: PLC0415
        MaskingConfig,
        MaskingGenerator,
        MaskingStrategy,
    )
    _map: dict[str, Any] = {
        "MaskingGenerator": MaskingGenerator,
        "MaskingStrategy": MaskingStrategy,
        "MaskingConfig": MaskingConfig,
    }
    return _map.get(name)


# ---------------------------------------------------------------------------
# Registry — maps method name strings to generator classes.
# Uses lazy loading so heavy deps (torch/tensorflow) aren't imported at
# package-init time.
# ---------------------------------------------------------------------------

def _build_registry() -> dict[str, type]:
    """Build the live GENERATOR_REGISTRY, skipping unavailable generators."""
    registry: dict[str, type] = {}
    # Rules generator
    try:
        from generation_engine.generators.rules_generator import (  # noqa: PLC0415
            RulesGenerator,
        )
        registry["rules"] = RulesGenerator
    except ImportError:
        pass
    # Statistical generator
    try:
        from generation_engine.generators.statistical_generator import (  # noqa: PLC0415
            StatisticalGenerator,
        )
        registry["statistical"] = StatisticalGenerator
    except ImportError:
        pass
    # Masking generator
    try:
        from generation_engine.generators.masking_generator import (  # noqa: PLC0415
            MaskingGenerator,
        )
        registry["masking"] = MaskingGenerator
    except ImportError:
        pass
    # AI/ML generator (may not be available if torch/tensorflow not installed)
    try:
        from generation_engine.generators.ai_ml_generator import (  # noqa: PLC0415
            AIMLGenerator,
        )
        registry["ai_ml"] = AIMLGenerator
    except ImportError:
        pass
    return registry


# Module-level singleton; built on first access via __getattr__.
_REGISTRY: dict[str, type] | None = None


def _get_registry() -> dict[str, type]:
    """Return (and lazily construct) the global GENERATOR_REGISTRY."""
    global _REGISTRY  # noqa: PLW0603
    if _REGISTRY is None:
        _REGISTRY = _build_registry()
    return _REGISTRY


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def get_generator(method_name: str, **kwargs: Any) -> Any:
    """Instantiate a generator by method name.

    Args:
        method_name: One of ``'ai_ml'``, ``'rules'``, ``'statistical'``,
            or ``'masking'``.
        **kwargs: Forwarded to the generator constructor.

    Returns:
        An instance of the requested :class:`BaseGenerator` subclass.

    Raises:
        ValueError: If *method_name* is not a registered generation method.
    """
    registry = _get_registry()
    cls = registry.get(method_name)
    if cls is None:
        available = sorted(registry.keys())
        msg = (
            f"Unknown generation method {method_name!r}. "
            f"Available methods: {available}"
        )
        raise ValueError(msg)
    return cls(**kwargs)


def get_available_methods() -> list[str]:
    """Return a sorted list of registered generation method names."""
    return sorted(_get_registry().keys())


# ---------------------------------------------------------------------------
# Lazy attribute resolution
# ---------------------------------------------------------------------------

_BASE_SYMBOLS = frozenset({
    "BaseGenerator", "ColumnSpec", "GenerationConfig",
    "GenerationError", "GenerationResult",
})
_MASKING_SYMBOLS = frozenset({
    "MaskingGenerator", "MaskingStrategy", "MaskingConfig",
})


def __getattr__(name: str) -> Any:
    """Lazy-import public symbols to avoid eagerly pulling in heavy deps."""
    if name in _BASE_SYMBOLS:
        result = _import_base(name)
        if result is not None:
            return result

    if name in _MASKING_SYMBOLS:
        result = _import_masking(name)
        if result is not None:
            return result

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

    if name == "AIMLGenerator":
        from generation_engine.generators.ai_ml_generator import (  # noqa: PLC0415
            AIMLGenerator,
        )
        return AIMLGenerator

    if name == "GENERATOR_REGISTRY":
        return _get_registry()

    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
