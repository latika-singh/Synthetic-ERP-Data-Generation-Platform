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

    from generation_engine.generators.base import BaseGenerator, GenerationResult
    from generation_engine.generators.masking_generator import MaskingGenerator

    generator = MaskingGenerator(config=masking_config)
    result = generator.generate(schema=schema, profile=profile, num_records=1000)
"""

from __future__ import annotations

__version__: str = "1.0.0"

__all__: list[str] = [
    "BaseGenerator",
    "ColumnSpec",
    "GenerationConfig",
    "GenerationError",
    "GenerationResult",
    "MaskingGenerator",
    "MaskingStrategy",
    "MaskingConfig",
]


def __getattr__(name: str):  # noqa: ANN001, ANN204
    """Lazy-import public symbols to avoid eagerly pulling in heavy deps."""
    _BASE_SYMBOLS = {
        "BaseGenerator",
        "ColumnSpec",
        "GenerationConfig",
        "GenerationError",
        "GenerationResult",
    }
    _MASKING_SYMBOLS = {
        "MaskingGenerator",
        "MaskingStrategy",
        "MaskingConfig",
    }

    if name in _BASE_SYMBOLS:
        from generation_engine.generators.base import (  # noqa: PLC0415
            BaseGenerator,
            ColumnSpec,
            GenerationConfig,
            GenerationError,
            GenerationResult,
        )
        _map = {
            "BaseGenerator": BaseGenerator,
            "ColumnSpec": ColumnSpec,
            "GenerationConfig": GenerationConfig,
            "GenerationError": GenerationError,
            "GenerationResult": GenerationResult,
        }
        return _map[name]

    if name in _MASKING_SYMBOLS:
        from generation_engine.generators.masking_generator import (  # noqa: PLC0415
            MaskingConfig,
            MaskingGenerator,
            MaskingStrategy,
        )
        _map = {
            "MaskingGenerator": MaskingGenerator,
            "MaskingStrategy": MaskingStrategy,
            "MaskingConfig": MaskingConfig,
        }
        return _map[name]

    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
