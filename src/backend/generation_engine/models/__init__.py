"""ML model management package for the Generation Engine.

Provides GAN and VAE architectures for tabular synthetic data generation
as well as a :class:`ModelRegistry` for versioning, caching, and lifecycle
management of trained model artefacts.

Exports
-------
.. list-table::
   :widths: 30 70

   * - :class:`GANConfig`
     - Hyperparameter configuration for the Tabular GAN
   * - :class:`Generator`
     - PyTorch generator network
   * - :class:`Discriminator`
     - PyTorch discriminator / critic network
   * - :class:`TabularGAN`
     - High-level GAN training and generation interface
   * - :class:`VAEConfig`
     - Hyperparameter configuration for the Tabular VAE
   * - :class:`TabularVAE`
     - High-level VAE training and generation interface
   * - :class:`Sampling`
     - Custom Keras layer implementing the reparameterization trick
   * - :class:`ModelRegistry`
     - Versioning, caching, and lifecycle management
   * - :func:`get_model_registry`
     - Singleton factory for the global ModelRegistry instance
   * - :class:`ModelMetadata`
     - Immutable metadata record for a registered model
   * - :class:`ModelLoadError`
     - Custom exception for model loading failures
   * - :class:`ModelRegistrationError`
     - Custom exception for model registration failures

Uses a lazy import pattern via ``__getattr__`` to avoid loading PyTorch
and TensorFlow eagerly at package import time, which would be expensive.
"""

from __future__ import annotations

from typing import Any


__version__: str = "1.0.0"

__all__: list[str] = [
    # GAN components (from gan_model)
    "TabularGAN",
    "GANConfig",
    "Generator",
    "Discriminator",
    # VAE components (from vae_model)
    "TabularVAE",
    "VAEConfig",
    "Sampling",
    # Registry components (from model_registry)
    "ModelRegistry",
    "get_model_registry",
    "ModelMetadata",
    "ModelLoadError",
    "ModelRegistrationError",
]

# ---------------------------------------------------------------------------
# Lazy import mapping: attribute name → (module_path, name)
# ---------------------------------------------------------------------------
_LAZY_IMPORTS: dict[str, tuple[str, str]] = {
    # GAN components
    "GANConfig": ("generation_engine.models.gan_model", "GANConfig"),
    "Generator": ("generation_engine.models.gan_model", "Generator"),
    "Discriminator": ("generation_engine.models.gan_model", "Discriminator"),
    "TabularGAN": ("generation_engine.models.gan_model", "TabularGAN"),
    # VAE components
    "VAEConfig": ("generation_engine.models.vae_model", "VAEConfig"),
    "TabularVAE": ("generation_engine.models.vae_model", "TabularVAE"),
    "Sampling": ("generation_engine.models.vae_model", "Sampling"),
    # Registry components
    "ModelRegistry": (
        "generation_engine.models.model_registry",
        "ModelRegistry",
    ),
    "get_model_registry": (
        "generation_engine.models.model_registry",
        "get_model_registry",
    ),
    "ModelMetadata": (
        "generation_engine.models.model_registry",
        "ModelMetadata",
    ),
    "ModelLoadError": (
        "generation_engine.models.model_registry",
        "ModelLoadError",
    ),
    "ModelRegistrationError": (
        "generation_engine.models.model_registry",
        "ModelRegistrationError",
    ),
}


def __getattr__(name: str) -> Any:
    """Lazily import package members on first access.

    This avoids loading heavy ML framework dependencies (PyTorch,
    TensorFlow) at package import time, deferring them until the
    specific class or function is actually needed.

    Args:
        name: The attribute name being accessed.

    Returns:
        The lazily-imported object.

    Raises:
        AttributeError: If the name is not a known export.
    """
    if name in _LAZY_IMPORTS:
        module_path, attr_name = _LAZY_IMPORTS[name]
        import importlib  # noqa: PLC0415

        module = importlib.import_module(module_path)
        value = getattr(module, attr_name)
        # Cache on the module for subsequent fast access
        globals()[name] = value
        return value
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
