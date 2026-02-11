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
   * - :class:`ModelRegistry`
     - Versioning, caching, and lifecycle management
   * - :class:`ModelMetadata`
     - Immutable metadata record for a registered model
   * - :class:`ModelStatus`
     - Lifecycle status enumeration
   * - :class:`ModelType`
     - Supported architecture type enumeration
"""

from generation_engine.models.gan_model import (
    Discriminator,
    GANConfig,
    Generator,
    TabularGAN,
)
from generation_engine.models.model_registry import (
    ModelMetadata,
    ModelRegistry,
    ModelStatus,
    ModelType,
)
from generation_engine.models.vae_model import (
    TabularVAE,
    VAEConfig,
)

__all__: list[str] = [
    # GAN
    "GANConfig",
    "Generator",
    "Discriminator",
    "TabularGAN",
    # VAE
    "VAEConfig",
    "TabularVAE",
    # Registry
    "ModelRegistry",
    "ModelMetadata",
    "ModelStatus",
    "ModelType",
]
