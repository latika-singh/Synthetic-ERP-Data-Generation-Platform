"""AI/ML-based synthetic data generator using GANs (PyTorch) and VAEs (TensorFlow).

This module provides the highest-fidelity synthetic data generation method
available in the platform.  It implements two AI/ML approaches:

- **GAN (Generative Adversarial Network)** via PyTorch — A Generator network
  learns to produce realistic synthetic records by competing against a
  Discriminator network in an adversarial training loop.  Excels at capturing
  complex multivariate distributions and correlations.

- **VAE (Variational Autoencoder)** via TensorFlow — An Encoder-Decoder
  architecture learns a continuous latent representation of the input data
  space, enabling smooth interpolation and generation of novel records from
  the learned distribution.

Both methods support:

* Pre-trained model loading from the :mod:`model_registry` for instant
  generation without training.
* On-the-fly training from statistical profiles when no pre-trained model
  is available.
* GPU acceleration via CUDA when available and configured.

Architecture:
    ``AIMLGenerator`` extends :class:`~generators.base.BaseGenerator`
    implementing the Strategy pattern.  It delegates to either the GAN or
    VAE pathway based on the ``model_type`` field in :class:`AIMLConfig`.

Usage::

    from generation_engine.generators.ai_ml_generator import AIMLGenerator

    generator = AIMLGenerator(config={"model_type": "gan", "gpu_enabled": True})
    result = generator.generate(schema=schema, profile=profile, num_records=10000)

Exports:
    - :class:`AIMLConfig` — Pydantic configuration model for AI/ML hyperparameters.
    - :class:`AIMLGenerator` — Main generator orchestrating GAN/VAE synthesis.
    - :class:`GANGenerator` — PyTorch Generator network (nn.Module).
    - :class:`GANDiscriminator` — PyTorch Discriminator network (nn.Module).
    - :class:`VAEEncoder` — TensorFlow Encoder network (tf.keras.Model).
    - :class:`VAEDecoder` — TensorFlow Decoder network (tf.keras.Model).
"""

from __future__ import annotations

from typing import Any, Dict, List, Literal, Optional

import numpy as np
import pandas as pd
import torch
import torch.nn as nn
import tensorflow as tf
from pydantic import BaseModel, Field

from generation_engine.generators.base import (
    BaseGenerator,
    GenerationConfig,
    GenerationError,
    GenerationResult,
)
from generation_engine.models.gan_model import TabularGAN
from generation_engine.models.model_registry import ModelLoadError, get_model_registry
from generation_engine.models.vae_model import TabularVAE
from shared.logging.structured_logger import get_logger


# ---------------------------------------------------------------------------
# Pydantic Configuration Model
# ---------------------------------------------------------------------------


class AIMLConfig(BaseModel):
    """Configuration model for AI/ML generation hyperparameters.

    Validated via Pydantic v2 when a raw configuration dictionary is parsed
    in :meth:`AIMLGenerator.__init__`.  All fields have sensible defaults
    suitable for medium-sized tabular datasets.

    Attributes:
        model_type: Sub-method selection — ``'gan'`` for GAN-based
            generation or ``'vae'`` for VAE-based generation.
        latent_dim: Dimensionality of the latent noise vector fed to the
            Generator (GAN) or Decoder (VAE).
        epochs: Number of training epochs when training from scratch.
        batch_size: Mini-batch size for training iterations.
        learning_rate: Base learning rate for Adam optimiser.
        gpu_enabled: When ``True``, attempts to place models on CUDA.
        model_path: Optional filesystem path **or** model-registry ID for
            pre-trained model weights.  ``None`` triggers fresh training.
        discriminator_steps: Number of Discriminator updates per Generator
            update in GAN training (``1`` follows standard DCGAN practice).
        kl_weight: KL-divergence term weight (β) in the VAE ELBO loss.
        hidden_dims: Hidden-layer dimensions for Generator/Discriminator
            (GAN) or Encoder/Decoder (VAE).
    """

    model_type: Literal["gan", "vae"] = Field(
        default="gan",
        description="AI/ML sub-method: 'gan' or 'vae'.",
    )
    latent_dim: int = Field(
        default=128,
        gt=0,
        description="Latent-space dimensionality.",
    )
    epochs: int = Field(
        default=100,
        gt=0,
        description="Training epochs for from-scratch training.",
    )
    batch_size: int = Field(
        default=256,
        gt=0,
        description="Mini-batch size for training.",
    )
    learning_rate: float = Field(
        default=0.0002,
        gt=0.0,
        description="Adam optimiser learning rate.",
    )
    gpu_enabled: bool = Field(
        default=False,
        description="Enable CUDA GPU acceleration.",
    )
    model_path: Optional[str] = Field(
        default=None,
        description="Path or model-registry ID for pre-trained weights.",
    )
    discriminator_steps: int = Field(
        default=1,
        gt=0,
        description="Discriminator steps per Generator step (GAN only).",
    )
    kl_weight: float = Field(
        default=1.0,
        ge=0.0,
        description="KL-divergence weight in VAE loss (β).",
    )
    hidden_dims: List[int] = Field(
        default=[256, 512, 256],
        description="Hidden-layer dimensions for networks.",
    )


# ---------------------------------------------------------------------------
# PyTorch GAN Components
# ---------------------------------------------------------------------------


class GANGenerator(nn.Module):
    """PyTorch Generator network for tabular-data GAN.

    Transforms random noise vectors from the latent space into synthetic
    data records.  The architecture uses a sequence of fully-connected
    layers with Batch Normalisation and LeakyReLU activations, culminating
    in a Tanh output layer that maps to the normalised data range [-1, 1].

    Architecture::

        z (latent_dim) → hidden_dims[0] → hidden_dims[1] → … → input_dim
        Each hidden layer:  Linear → BatchNorm1d → LeakyReLU(0.2)
        Output layer:       Linear → Tanh

    Args:
        input_dim: Dimensionality of the output (preprocessed data width).
        latent_dim: Dimensionality of the input latent noise vector.
        hidden_dims: List of hidden-layer widths in forward order.
    """

    def __init__(
        self,
        input_dim: int,
        latent_dim: int,
        hidden_dims: List[int],
    ) -> None:
        super().__init__()
        self.input_dim = input_dim
        self.latent_dim = latent_dim

        layers: List[nn.Module] = []
        prev_dim = latent_dim

        for h_dim in hidden_dims:
            layers.extend(
                [
                    nn.Linear(prev_dim, h_dim),
                    nn.BatchNorm1d(h_dim),
                    nn.LeakyReLU(0.2, inplace=True),
                ]
            )
            prev_dim = h_dim

        # Final projection to data dimensionality with Tanh activation
        layers.append(nn.Linear(prev_dim, input_dim))
        layers.append(nn.Tanh())

        self.network = nn.Sequential(*layers)

    def forward(self, z: torch.Tensor) -> torch.Tensor:
        """Forward pass: transform latent vector to synthetic data.

        Args:
            z: Latent noise tensor of shape ``(batch, latent_dim)``.

        Returns:
            Generated data tensor of shape ``(batch, input_dim)``
            normalised to the range ``[-1, 1]``.
        """
        return self.network(z)


class GANDiscriminator(nn.Module):
    """PyTorch Discriminator network for tabular-data GAN.

    Classifies input data records as real or generated.  Uses a mirrored
    architecture relative to the Generator (reversed hidden dims) with
    LeakyReLU activations and Dropout for regularisation.

    Architecture::

        x (input_dim) → hidden_dims[-1] → … → hidden_dims[0] → 1
        Each hidden layer:  Linear → LeakyReLU(0.2) → Dropout(0.3)
        Output layer:       Linear → Sigmoid

    Args:
        input_dim: Dimensionality of the input data.
        hidden_dims: List of hidden-layer widths (applied in reverse).
    """

    def __init__(
        self,
        input_dim: int,
        hidden_dims: List[int],
    ) -> None:
        super().__init__()
        self.input_dim = input_dim

        layers: List[nn.Module] = []
        prev_dim = input_dim

        # Mirror the Generator by reversing hidden dimensions
        reversed_dims = list(reversed(hidden_dims))
        for h_dim in reversed_dims:
            layers.extend(
                [
                    nn.Linear(prev_dim, h_dim),
                    nn.LeakyReLU(0.2, inplace=True),
                    nn.Dropout(0.3),
                ]
            )
            prev_dim = h_dim

        # Single output probability: real (≈1) vs fake (≈0)
        layers.append(nn.Linear(prev_dim, 1))
        layers.append(nn.Sigmoid())

        self.network = nn.Sequential(*layers)

    def forward(self, x: torch.Tensor) -> torch.Tensor:
        """Forward pass: classify data as real or fake.

        Args:
            x: Data tensor of shape ``(batch, input_dim)``.

        Returns:
            Probability tensor of shape ``(batch, 1)`` where values
            near ``1.0`` indicate "real" and near ``0.0`` indicate
            "fake".
        """
        return self.network(x)


# ---------------------------------------------------------------------------
# TensorFlow VAE Components
# ---------------------------------------------------------------------------


class VAEEncoder(tf.keras.Model):
    """TensorFlow VAE Encoder network.

    Maps input data to the parameters of a Gaussian latent distribution
    (mean μ and log-variance log σ²) and samples from it using the
    reparameterisation trick:  ``z = μ + σ · ε``  where ``ε ~ N(0, I)``.

    Architecture::

        x (input_dim) → hidden_dims[0] → hidden_dims[1] → …
        Each hidden layer:  Dense → BatchNormalization → LeakyReLU → Dropout(0.2)
        Output:  Two parallel Dense layers → z_mean, z_log_var  (size latent_dim)

    Args:
        input_dim: Dimensionality of input data.
        latent_dim: Dimensionality of the latent space.
        hidden_dims: List of hidden-layer widths.
    """

    def __init__(
        self,
        input_dim: int,
        latent_dim: int,
        hidden_dims: List[int],
        **kwargs: Any,
    ) -> None:
        super().__init__(**kwargs)
        self._input_dim = input_dim
        self._latent_dim = latent_dim

        # Build hidden layers
        self.hidden_layers: List[tf.keras.layers.Layer] = []
        for h_dim in hidden_dims:
            self.hidden_layers.append(tf.keras.layers.Dense(h_dim))
            self.hidden_layers.append(tf.keras.layers.BatchNormalization())
            self.hidden_layers.append(tf.keras.layers.LeakyReLU(0.2))
            self.hidden_layers.append(tf.keras.layers.Dropout(0.2))

        # Latent distribution parameter heads
        self.z_mean_layer = tf.keras.layers.Dense(latent_dim, name="z_mean")
        self.z_log_var_layer = tf.keras.layers.Dense(latent_dim, name="z_log_var")

    def call(
        self,
        x: tf.Tensor,
        training: bool = False,
    ) -> tuple:
        """Encode input data to latent distribution parameters and sample.

        Implements the reparameterisation trick for differentiable sampling:
        ``z = z_mean + exp(0.5 * z_log_var) * epsilon``.

        Args:
            x: Input tensor of shape ``(batch, input_dim)``.
            training: Whether in training mode (affects BatchNorm/Dropout).

        Returns:
            Tuple of ``(z_mean, z_log_var, z)`` each of shape
            ``(batch, latent_dim)``.
        """
        h = x
        for layer in self.hidden_layers:
            if isinstance(
                layer,
                (tf.keras.layers.BatchNormalization, tf.keras.layers.Dropout),
            ):
                h = layer(h, training=training)
            else:
                h = layer(h)

        z_mean = self.z_mean_layer(h)
        z_log_var = self.z_log_var_layer(h)

        # Reparameterisation trick
        epsilon = tf.random.normal(shape=tf.shape(z_mean))
        z = z_mean + tf.exp(0.5 * z_log_var) * epsilon

        return z_mean, z_log_var, z


class VAEDecoder(tf.keras.Model):
    """TensorFlow VAE Decoder network.

    Reconstructs data from sampled latent vectors by reversing the
    encoder's hidden-layer structure.

    Architecture::

        z (latent_dim) → hidden_dims[-1] → … → hidden_dims[0] → output_dim
        Each hidden layer:  Dense → BatchNormalization → LeakyReLU → Dropout(0.2)
        Output layer:       Dense → Sigmoid

    Args:
        latent_dim: Dimensionality of the latent space.
        output_dim: Dimensionality of reconstructed output.
        hidden_dims: List of hidden-layer widths (applied in reverse).
    """

    def __init__(
        self,
        latent_dim: int,
        output_dim: int,
        hidden_dims: List[int],
        **kwargs: Any,
    ) -> None:
        super().__init__(**kwargs)
        self._latent_dim = latent_dim
        self._output_dim = output_dim

        # Build hidden layers in reverse order
        self.hidden_layers: List[tf.keras.layers.Layer] = []
        reversed_dims = list(reversed(hidden_dims))
        for h_dim in reversed_dims:
            self.hidden_layers.append(tf.keras.layers.Dense(h_dim))
            self.hidden_layers.append(tf.keras.layers.BatchNormalization())
            self.hidden_layers.append(tf.keras.layers.LeakyReLU(0.2))
            self.hidden_layers.append(tf.keras.layers.Dropout(0.2))

        # Output reconstruction layer
        self.output_layer = tf.keras.layers.Dense(
            output_dim, activation="sigmoid"
        )

    def call(
        self,
        z: tf.Tensor,
        training: bool = False,
    ) -> tf.Tensor:
        """Decode latent vector to reconstructed data.

        Args:
            z: Latent vector tensor of shape ``(batch, latent_dim)``.
            training: Whether in training mode (affects BatchNorm/Dropout).

        Returns:
            Reconstructed data tensor of shape ``(batch, output_dim)``.
        """
        h = z
        for layer in self.hidden_layers:
            if isinstance(
                layer,
                (tf.keras.layers.BatchNormalization, tf.keras.layers.Dropout),
            ):
                h = layer(h, training=training)
            else:
                h = layer(h)

        return self.output_layer(h)


# ---------------------------------------------------------------------------
# Main AIMLGenerator
# ---------------------------------------------------------------------------


class AIMLGenerator(BaseGenerator):
    """AI/ML-based synthetic data generator using GANs and VAEs.

    Extends :class:`~generators.base.BaseGenerator` with the highest-fidelity
    generation method available in the platform.  Supports:

    * **GAN pathway** — Adversarial training via PyTorch with Generator and
      Discriminator networks.
    * **VAE pathway** — Variational inference via TensorFlow with Encoder
      and Decoder networks.
    * **Pre-trained model loading** — Instant generation from pre-existing
      model weights stored in the :mod:`model_registry`.
    * **GPU acceleration** — Automatic CUDA device placement when available.

    Args:
        config: Optional configuration dictionary parsed into an
            :class:`AIMLConfig` instance.  If ``None``, defaults are used.
    """

    def __init__(self, config: Optional[Dict[str, Any]] = None) -> None:
        super().__init__(config)
        self.logger = get_logger(__name__)

        # Parse configuration with Pydantic validation
        try:
            self._aiml_config = AIMLConfig(**(config or {}))
        except Exception as exc:
            self.logger.error(
                "aiml_config_parse_failed",
                error=str(exc),
                config=config,
            )
            raise ValueError(f"Invalid AI/ML configuration: {exc}") from exc

        # Resolve compute device
        self._device: torch.device = self._resolve_device()
        self.logger.info(
            "aiml_generator_initialized",
            model_type=self._aiml_config.model_type,
            latent_dim=self._aiml_config.latent_dim,
            device=str(self._device),
            gpu_enabled=self._aiml_config.gpu_enabled,
        )

        # Model components — lazy initialised during training or loading
        self._gan_generator: Optional[GANGenerator] = None
        self._gan_discriminator: Optional[GANDiscriminator] = None
        self._vae_encoder: Optional[VAEEncoder] = None
        self._vae_decoder: Optional[VAEDecoder] = None
        self._pretrained_model: Any = None

        # Preprocessing ↔ postprocessing state
        self._normalization_params: Dict[str, Dict[str, Any]] = {}
        self._categorical_mappings: Dict[str, Dict[str, Any]] = {}
        self._column_order: List[str] = []
        self._numerical_columns: List[str] = []
        self._categorical_columns: List[str] = []
        self._datetime_columns: List[str] = []
        self._preprocessed_dim: int = 0

    # ------------------------------------------------------------------
    # Device Resolution
    # ------------------------------------------------------------------

    def _resolve_device(self) -> torch.device:
        """Resolve the compute device based on configuration and availability.

        Returns:
            ``torch.device('cuda')`` if GPU is enabled and CUDA is
            available, ``torch.device('cpu')`` otherwise.
        """
        if self._aiml_config.gpu_enabled and torch.cuda.is_available():
            device = torch.device("cuda")
            self.logger.info(
                "gpu_device_selected",
                cuda_device=torch.cuda.get_device_name(0),
                cuda_memory_gb=round(
                    torch.cuda.get_device_properties(0).total_mem / (1024**3),
                    2,
                ),
            )
            return device

        if self._aiml_config.gpu_enabled and not torch.cuda.is_available():
            self.logger.warning(
                "gpu_requested_but_unavailable",
                falling_back_to="cpu",
            )

        return torch.device("cpu")

    # ------------------------------------------------------------------
    # Abstract Method Implementations  (Strategy Pattern)
    # ------------------------------------------------------------------

    def generate(
        self,
        schema: dict[str, Any],
        profile: dict[str, Any],
        num_records: int,
        **kwargs: Any,
    ) -> GenerationResult:
        """Generate synthetic records using GAN or VAE.

        Full pipeline:

        1. Validate inputs and parse the schema.
        2. Attempt to load a pre-trained model if ``model_path`` is set.
        3. If no pre-trained model, synthesise training data from the
           statistical profile and train a fresh model.
        4. Generate the requested number of records (batched internally).
        5. Post-process outputs to match original column types and ranges.
        6. Inject NULL values per schema null probabilities.
        7. Return a :class:`GenerationResult` with metadata.

        Args:
            schema: Table schema with a ``"columns"`` key listing column
                definitions conforming to :class:`ColumnSpec`.
            profile: Statistical profile from the Profiling Service
                containing per-column statistics.
            num_records: Number of synthetic records to generate.
            **kwargs: Extra parameters (e.g. ``model_id``).

        Returns:
            :class:`GenerationResult` encapsulating the generated
            :class:`~pandas.DataFrame` and metadata.

        Raises:
            GenerationError: On any fatal generation failure.
        """
        self._start_timer()
        method = self._aiml_config.model_type

        self.logger.info(
            "aiml_generation_started",
            method=method,
            num_records=num_records,
            latent_dim=self._aiml_config.latent_dim,
            gpu_enabled=self._aiml_config.gpu_enabled,
        )

        try:
            # 1. Validate and parse schema into ColumnSpec objects
            column_specs = self._validate_schema(schema)

            # 2. Categorise columns for pre/post-processing
            self._column_order = [col.name for col in column_specs]
            self._numerical_columns = [
                col.name
                for col in column_specs
                if col.data_type in ("integer", "float", "decimal")
            ]
            self._categorical_columns = [
                col.name
                for col in column_specs
                if col.data_type in ("string", "boolean", "text")
            ]
            self._datetime_columns = [
                col.name
                for col in column_specs
                if col.data_type in ("date", "datetime", "timestamp")
            ]

            # 3. Attempt to load a pre-trained model
            model_loaded = False
            if self._aiml_config.model_path:
                try:
                    self._load_model(self._aiml_config.model_path)
                    model_loaded = True
                    self.logger.info(
                        "pretrained_model_loaded",
                        model_path=self._aiml_config.model_path,
                    )
                except (
                    ModelLoadError,
                    FileNotFoundError,
                    RuntimeError,
                ) as load_err:
                    self.logger.warning(
                        "pretrained_model_load_failed",
                        model_path=self._aiml_config.model_path,
                        error=str(load_err),
                        fallback="training_from_profile",
                    )

            # 4. Train from profile if no pre-trained model is available
            if not model_loaded:
                profile_data = self._preprocess_profile(schema, profile)
                if method == "gan":
                    self._train_gan(profile_data, self._aiml_config.epochs)
                else:
                    self._train_vae(profile_data, self._aiml_config.epochs)

            # 5. Generate synthetic records
            self.logger.info(
                "aiml_batch_generation_started",
                method=method,
                num_records=num_records,
            )

            if model_loaded and self._pretrained_model is not None:
                raw_output = self._generate_with_pretrained(num_records)
            elif method == "gan":
                raw_output = self._generate_gan(num_records)
            else:
                raw_output = self._generate_vae(num_records)

            self._log_progress(num_records, num_records)

            # 6. Post-process to a properly typed DataFrame
            if model_loaded and self._pretrained_model is not None:
                # Pre-trained models handle their own postprocessing
                df = self._coerce_pretrained_output(raw_output, schema)
            else:
                df = self._postprocess_output(raw_output, schema)

            # 7. Inject NULLs per schema null probabilities
            df = self._apply_nulls(df, schema)

            # 8. Build result with metadata
            metadata: Dict[str, Any] = {
                "method": "ai_ml",
                "sub_method": method,
                "latent_dim": self._aiml_config.latent_dim,
                "epochs": self._aiml_config.epochs,
                "model_loaded_from": (
                    self._aiml_config.model_path if model_loaded else None
                ),
                "gpu_used": str(self._device) != "cpu",
                "preprocessed_dim": self._preprocessed_dim,
                "num_numerical_columns": len(self._numerical_columns),
                "num_categorical_columns": len(self._categorical_columns),
                "num_datetime_columns": len(self._datetime_columns),
            }

            result = self._build_result(data=df, metadata=metadata)

            self.logger.info(
                "aiml_generation_completed",
                method=method,
                num_records=result.num_records,
                generation_time_seconds=result.generation_time_seconds,
            )
            return result

        except GenerationError:
            raise
        except torch.cuda.OutOfMemoryError as oom_err:
            self.logger.error(
                "aiml_cuda_oom",
                error=str(oom_err),
                num_records=num_records,
                latent_dim=self._aiml_config.latent_dim,
            )
            raise GenerationError(
                message=(
                    f"CUDA out of memory during {method} generation. "
                    f"Reduce batch_size or num_records. Error: {oom_err}"
                ),
                method="ai_ml",
                details={
                    "sub_method": method,
                    "num_records": num_records,
                    "error": str(oom_err),
                },
            ) from oom_err
        except Exception as exc:
            self.logger.error(
                "aiml_generation_failed",
                error=str(exc),
                error_type=type(exc).__name__,
                method=method,
                num_records=num_records,
            )
            raise GenerationError(
                message=f"AI/ML generation failed: {exc}",
                method="ai_ml",
                details={
                    "sub_method": method,
                    "num_records": num_records,
                    "error": str(exc),
                    "error_type": type(exc).__name__,
                },
            ) from exc

    def validate_config(self, config: dict[str, Any]) -> bool:
        """Validate AI/ML generator configuration.

        Parses the configuration through :class:`AIMLConfig` Pydantic
        validation and performs additional runtime checks such as CUDA
        availability.

        Args:
            config: Configuration dictionary to validate.

        Returns:
            ``True`` when configuration is valid.

        Raises:
            ValueError: With details when validation fails.
        """
        try:
            parsed = AIMLConfig(**config)
        except Exception as exc:
            raise ValueError(
                f"Invalid AI/ML configuration: {exc}"
            ) from exc

        if parsed.model_type not in ("gan", "vae"):
            raise ValueError(
                f"model_type must be 'gan' or 'vae', got '{parsed.model_type}'"
            )
        if parsed.latent_dim <= 0:
            raise ValueError(
                f"latent_dim must be > 0, got {parsed.latent_dim}"
            )
        if parsed.epochs <= 0:
            raise ValueError(f"epochs must be > 0, got {parsed.epochs}")
        if parsed.learning_rate <= 0:
            raise ValueError(
                f"learning_rate must be > 0, got {parsed.learning_rate}"
            )
        if parsed.gpu_enabled and not torch.cuda.is_available():
            self.logger.warning(
                "gpu_validation_warning",
                message="GPU enabled in config but CUDA is not available",
            )
        if not parsed.hidden_dims or any(d <= 0 for d in parsed.hidden_dims):
            raise ValueError(
                f"hidden_dims must be non-empty with all positive values: "
                f"{parsed.hidden_dims}"
            )

        self.logger.debug(
            "config_validated",
            model_type=parsed.model_type,
            latent_dim=parsed.latent_dim,
            epochs=parsed.epochs,
        )
        return True

    def get_capabilities(self) -> dict[str, Any]:
        """Return capabilities of the AI/ML generator.

        Provides machine-readable metadata about what this generator can
        do, used by the method selector for optimal generator routing.

        Returns:
            Dictionary describing generator capabilities.
        """
        return {
            "name": "ai_ml",
            "description": "AI/ML-based generation using GANs and VAEs",
            "methods": ["gan", "vae"],
            "supports_gpu": True,
            "supports_training": True,
            "supports_pretrained": True,
            "best_for": [
                "complex_distributions",
                "multivariate_correlations",
                "high_fidelity",
            ],
            "column_types": ["numeric", "categorical", "datetime", "text"],
            "min_profile_records": 1000,
            "supported_column_types": [
                "integer",
                "float",
                "decimal",
                "string",
                "boolean",
                "date",
                "datetime",
                "timestamp",
                "text",
            ],
        }

    # ------------------------------------------------------------------
    # GAN Training and Generation
    # ------------------------------------------------------------------

    def _train_gan(self, profile_data: np.ndarray, epochs: int) -> None:
        """Train a GAN from scratch on synthetic profile data.

        Implements the standard GAN adversarial training loop:

        1. Train Discriminator on real and fake mini-batches (BCE loss).
        2. Train Generator to fool the Discriminator.
        3. Repeat for *epochs* iterations.

        Args:
            profile_data: Preprocessed training data of shape
                ``(n_samples, preprocessed_dim)``.
            epochs: Number of training epochs.
        """
        input_dim = profile_data.shape[1]
        self._preprocessed_dim = input_dim
        latent_dim = self._aiml_config.latent_dim
        hidden_dims = self._aiml_config.hidden_dims
        batch_size = self._aiml_config.batch_size
        lr = self._aiml_config.learning_rate
        d_steps = self._aiml_config.discriminator_steps

        self.logger.info(
            "gan_training_started",
            input_dim=input_dim,
            latent_dim=latent_dim,
            hidden_dims=hidden_dims,
            epochs=epochs,
            batch_size=batch_size,
            learning_rate=lr,
            discriminator_steps=d_steps,
        )

        # Initialise networks on the resolved device
        self._gan_generator = GANGenerator(
            input_dim, latent_dim, hidden_dims
        ).to(self._device)
        self._gan_discriminator = GANDiscriminator(
            input_dim, hidden_dims
        ).to(self._device)

        # Adam optimisers with DCGAN-recommended betas
        optimizer_g = torch.optim.Adam(
            self._gan_generator.parameters(),
            lr=lr,
            betas=(0.5, 0.999),
        )
        optimizer_d = torch.optim.Adam(
            self._gan_discriminator.parameters(),
            lr=lr,
            betas=(0.5, 0.999),
        )

        criterion = nn.BCELoss()

        # Prepare training tensor
        real_data_tensor = torch.Tensor(profile_data).to(self._device)
        num_samples = real_data_tensor.shape[0]
        num_batches = max(1, num_samples // batch_size)

        last_d_loss = 0.0
        last_g_loss = 0.0

        for epoch in range(epochs):
            epoch_d_loss = 0.0
            epoch_g_loss = 0.0

            # Shuffle every epoch
            indices = torch.randperm(num_samples, device=self._device)
            real_data_tensor = real_data_tensor[indices]

            for batch_idx in range(num_batches):
                start = batch_idx * batch_size
                end = min(start + batch_size, num_samples)
                real_batch = real_data_tensor[start:end]
                current_bs = real_batch.shape[0]

                real_labels = torch.ones(current_bs, 1, device=self._device)
                fake_labels = torch.zeros(current_bs, 1, device=self._device)

                # ---------- Train Discriminator ----------
                for _ in range(d_steps):
                    optimizer_d.zero_grad()

                    # Real data loss
                    real_pred = self._gan_discriminator(real_batch)
                    d_real_loss = criterion(real_pred, real_labels)

                    # Fake data loss
                    z = torch.randn(
                        current_bs, latent_dim, device=self._device
                    )
                    fake_data = self._gan_generator(z).detach()
                    fake_pred = self._gan_discriminator(fake_data)
                    d_fake_loss = criterion(fake_pred, fake_labels)

                    d_loss = d_real_loss + d_fake_loss
                    d_loss.backward()
                    optimizer_d.step()
                    epoch_d_loss += d_loss.item()

                # ---------- Train Generator ----------
                optimizer_g.zero_grad()
                z = torch.randn(
                    current_bs, latent_dim, device=self._device
                )
                fake_data = self._gan_generator(z)
                fake_pred = self._gan_discriminator(fake_data)
                g_loss = criterion(fake_pred, real_labels)
                g_loss.backward()
                optimizer_g.step()
                epoch_g_loss += g_loss.item()

            # Normalise epoch losses
            last_d_loss = epoch_d_loss / max(num_batches * d_steps, 1)
            last_g_loss = epoch_g_loss / max(num_batches, 1)

            if (epoch + 1) % 10 == 0 or epoch == 0:
                self.logger.info(
                    "gan_training_progress",
                    epoch=epoch + 1,
                    total_epochs=epochs,
                    d_loss=round(last_d_loss, 6),
                    g_loss=round(last_g_loss, 6),
                )

        self.logger.info(
            "gan_training_completed",
            epochs=epochs,
            final_d_loss=round(last_d_loss, 6),
            final_g_loss=round(last_g_loss, 6),
        )

    def _train_vae(self, profile_data: np.ndarray, epochs: int) -> None:
        """Train a VAE from scratch on synthetic profile data.

        Implements custom VAE training with:

        * Reconstruction loss (mean squared error).
        * KL-divergence regularisation weighted by ``kl_weight``.
        * ``tf.GradientTape`` for fine-grained gradient control.

        Args:
            profile_data: Preprocessed training data of shape
                ``(n_samples, preprocessed_dim)``.
            epochs: Number of training epochs.
        """
        input_dim = profile_data.shape[1]
        self._preprocessed_dim = input_dim
        latent_dim = self._aiml_config.latent_dim
        hidden_dims = self._aiml_config.hidden_dims
        batch_size = self._aiml_config.batch_size
        lr = self._aiml_config.learning_rate
        kl_weight = self._aiml_config.kl_weight

        self.logger.info(
            "vae_training_started",
            input_dim=input_dim,
            latent_dim=latent_dim,
            hidden_dims=hidden_dims,
            epochs=epochs,
            batch_size=batch_size,
            kl_weight=kl_weight,
        )

        # Initialise encoder and decoder
        self._vae_encoder = VAEEncoder(input_dim, latent_dim, hidden_dims)
        self._vae_decoder = VAEDecoder(latent_dim, input_dim, hidden_dims)

        optimizer = tf.keras.optimizers.Adam(learning_rate=lr)

        # Build a TF dataset from the preprocessed profile data
        dataset = (
            tf.data.Dataset.from_tensor_slices(
                profile_data.astype(np.float32)
            )
            .shuffle(buffer_size=min(10_000, profile_data.shape[0]))
            .batch(batch_size)
        )

        last_total = 0.0
        last_recon = 0.0
        last_kl = 0.0

        for epoch in range(epochs):
            epoch_total_loss = 0.0
            epoch_recon_loss = 0.0
            epoch_kl_loss = 0.0
            num_batches = 0

            for batch_data in dataset:
                with tf.GradientTape() as tape:
                    # Encode
                    z_mean, z_log_var, z = self._vae_encoder(
                        batch_data, training=True
                    )
                    # Decode
                    reconstructed = self._vae_decoder(z, training=True)

                    # Reconstruction loss (MSE)
                    reconstruction_loss = tf.reduce_mean(
                        tf.square(batch_data - reconstructed)
                    )

                    # KL divergence: -0.5 * Σ(1 + log(σ²) - μ² - σ²)
                    kl_loss = -0.5 * tf.reduce_mean(
                        1.0
                        + z_log_var
                        - tf.square(z_mean)
                        - tf.exp(z_log_var)
                    )

                    total_loss = reconstruction_loss + kl_weight * kl_loss

                # Apply gradients
                trainable_vars = (
                    self._vae_encoder.trainable_variables
                    + self._vae_decoder.trainable_variables
                )
                gradients = tape.gradient(total_loss, trainable_vars)
                optimizer.apply_gradients(zip(gradients, trainable_vars))

                epoch_total_loss += float(total_loss)
                epoch_recon_loss += float(reconstruction_loss)
                epoch_kl_loss += float(kl_loss)
                num_batches += 1

            last_total = epoch_total_loss / max(num_batches, 1)
            last_recon = epoch_recon_loss / max(num_batches, 1)
            last_kl = epoch_kl_loss / max(num_batches, 1)

            if (epoch + 1) % 10 == 0 or epoch == 0:
                self.logger.info(
                    "vae_training_progress",
                    epoch=epoch + 1,
                    total_epochs=epochs,
                    total_loss=round(last_total, 6),
                    reconstruction_loss=round(last_recon, 6),
                    kl_loss=round(last_kl, 6),
                )

        self.logger.info(
            "vae_training_completed",
            epochs=epochs,
            final_total_loss=round(last_total, 6),
            final_recon_loss=round(last_recon, 6),
            final_kl_loss=round(last_kl, 6),
        )

    # ------------------------------------------------------------------
    # Generation (post-training inference)
    # ------------------------------------------------------------------

    def _generate_gan(self, num_records: int) -> np.ndarray:
        """Generate synthetic data using the trained GAN Generator.

        Samples latent vectors from ``N(0, 1)`` and passes them through the
        Generator network in evaluation mode (no gradient tracking).

        Args:
            num_records: Number of records to generate.

        Returns:
            Generated data array of shape ``(num_records, preprocessed_dim)``.

        Raises:
            GenerationError: If the Generator has not been initialised.
        """
        if self._gan_generator is None:
            raise GenerationError(
                message="GAN Generator not trained or loaded",
                method="ai_ml",
                details={"sub_method": "gan"},
            )

        self._gan_generator.eval()
        latent_dim = self._aiml_config.latent_dim
        batch_size = self._aiml_config.batch_size
        all_generated: List[np.ndarray] = []
        remaining = num_records

        with torch.no_grad():
            while remaining > 0:
                current_batch = min(batch_size, remaining)
                z = torch.randn(
                    current_batch, latent_dim, device=self._device
                )
                generated = self._gan_generator(z)
                all_generated.append(generated.cpu().numpy())
                remaining -= current_batch

        return np.concatenate(all_generated, axis=0)[:num_records]

    def _generate_vae(self, num_records: int) -> np.ndarray:
        """Generate synthetic data using the trained VAE Decoder.

        Samples latent vectors from ``N(0, 1)`` and decodes them through
        the Decoder network.

        Args:
            num_records: Number of records to generate.

        Returns:
            Generated data array of shape ``(num_records, preprocessed_dim)``.

        Raises:
            GenerationError: If the Decoder has not been initialised.
        """
        if self._vae_decoder is None:
            raise GenerationError(
                message="VAE Decoder not trained or loaded",
                method="ai_ml",
                details={"sub_method": "vae"},
            )

        latent_dim = self._aiml_config.latent_dim
        batch_size = self._aiml_config.batch_size
        all_generated: List[np.ndarray] = []
        remaining = num_records

        while remaining > 0:
            current_batch = min(batch_size, remaining)
            z = np.random.normal(
                size=(current_batch, latent_dim)
            ).astype(np.float32)
            z_tensor = tf.constant(z)
            decoded = self._vae_decoder(z_tensor, training=False)
            all_generated.append(decoded.numpy())
            remaining -= current_batch

        return np.concatenate(all_generated, axis=0)[:num_records]

    def _generate_with_pretrained(self, num_records: int) -> np.ndarray:
        """Generate data using a loaded :class:`TabularGAN` or :class:`TabularVAE`.

        Delegates to the pre-trained model's own ``generate()`` method
        which handles internal batching and postprocessing.

        Args:
            num_records: Number of records to generate.

        Returns:
            Generated numpy array.

        Raises:
            GenerationError: If no pre-trained model is loaded or the
                model type is unrecognised.
        """
        if self._pretrained_model is None:
            raise GenerationError(
                message="No pre-trained model loaded",
                method="ai_ml",
            )

        if isinstance(self._pretrained_model, TabularGAN):
            return self._pretrained_model.generate(num_records)
        elif isinstance(self._pretrained_model, TabularVAE):
            return self._pretrained_model.generate(num_records)
        else:
            raise GenerationError(
                message=(
                    f"Unsupported pre-trained model type: "
                    f"{type(self._pretrained_model).__name__}"
                ),
                method="ai_ml",
            )

    # ------------------------------------------------------------------
    # Model Loading
    # ------------------------------------------------------------------

    def _load_model(self, model_path: str) -> None:
        """Load a pre-trained GAN or VAE model.

        Attempts loading in the following order:

        1. **Model Registry** — Interpret *model_path* as a model ID and
           load via :func:`get_model_registry`.
        2. **Direct file loading** — Fall back to filesystem loading using
           ``TabularGAN.load()`` or ``TabularVAE.load_model()``.

        Args:
            model_path: Model-registry ID or filesystem path.

        Raises:
            ModelLoadError: If registry loading fails and filesystem
                loading is not attempted.
            FileNotFoundError: If the filesystem path does not exist.
            RuntimeError: If deserialization fails.
        """
        method = self._aiml_config.model_type
        device_str = "cuda" if self._device.type == "cuda" else "cpu"

        # Attempt 1: Model registry
        try:
            registry = get_model_registry()
            model = registry.load_model(
                model_id=model_path,
                device=device_str,
            )
            self._pretrained_model = model
            self.logger.info(
                "model_loaded_from_registry",
                model_id=model_path,
                model_type=type(model).__name__,
            )
            return
        except ModelLoadError:
            self.logger.debug(
                "registry_load_failed_trying_filesystem",
                model_path=model_path,
            )

        # Attempt 2: Direct filesystem loading
        if method == "gan":
            model = TabularGAN.load(model_path, device=device_str)
            self._pretrained_model = model
            self.logger.info(
                "gan_model_loaded_from_file",
                path=model_path,
            )
        else:
            model = TabularVAE.load_model(model_path)
            self._pretrained_model = model
            self.logger.info(
                "vae_model_loaded_from_file",
                path=model_path,
            )

    # ------------------------------------------------------------------
    # Preprocessing / Postprocessing
    # ------------------------------------------------------------------

    def _preprocess_profile(
        self,
        schema: dict[str, Any],
        profile: dict[str, Any],
    ) -> np.ndarray:
        """Synthesise training data from statistical-profile metadata.

        Since the platform never accesses raw production data (Constraint
        C-001), the AI/ML generator creates synthetic "seed" data that
        reproduces the statistical distributions described in the profile.
        This seed data is used to train GAN/VAE models from scratch.

        Processing steps:

        1. Extract per-column statistics (mean, std, min, max, categories).
        2. Generate synthetic samples matching each column's distribution.
        3. Normalise numerical columns to ``[-1, 1]`` (GAN) or ``[0, 1]``
           (VAE).
        4. One-hot encode categorical columns.
        5. Store normalisation parameters for :meth:`_postprocess_output`.

        Args:
            schema: Table schema with ``"columns"`` list.
            profile: Statistical profile with per-column statistics.

        Returns:
            Preprocessed training array ``(n_samples, preprocessed_dim)``.
        """
        columns_raw = schema.get("columns", [])
        column_stats = profile.get(
            "column_stats", profile.get("columns", {})
        )
        num_samples = max(
            profile.get("num_samples", 5000),
            profile.get("sample_size", 5000),
            1000,
        )

        parts: List[np.ndarray] = []
        self._normalization_params = {}
        self._categorical_mappings = {}
        is_gan = self._aiml_config.model_type == "gan"

        for col_def in columns_raw:
            col_name, col_type = self._extract_col_info(col_def)
            col_profile = (
                column_stats.get(col_name, {})
                if isinstance(column_stats, dict)
                else {}
            )

            if col_type in ("integer", "float", "decimal"):
                part = self._preprocess_numeric(
                    col_name, col_type, col_profile, num_samples, is_gan
                )
                parts.append(part)

            elif col_type in ("string", "boolean", "text"):
                part, num_cats = self._preprocess_categorical(
                    col_name, col_type, col_profile, num_samples
                )
                parts.append(part)

            elif col_type in ("date", "datetime", "timestamp"):
                part = self._preprocess_temporal(
                    col_name, col_type, col_profile, num_samples, is_gan
                )
                parts.append(part)

            else:
                # Fallback: uniform noise
                samples = np.random.uniform(-1.0, 1.0, size=num_samples)
                parts.append(samples.reshape(-1, 1))
                self._normalization_params[col_name] = {
                    "min": -1.0,
                    "max": 1.0,
                    "range": 2.0,
                    "type": col_type,
                }

        if not parts:
            self.logger.warning(
                "no_columns_for_preprocessing",
                message="No processable columns found; generating random data",
            )
            self._preprocessed_dim = 10
            return np.random.normal(size=(num_samples, 10)).astype(np.float32)

        result = np.concatenate(parts, axis=1).astype(np.float32)
        self._preprocessed_dim = result.shape[1]

        self.logger.info(
            "profile_preprocessed",
            num_samples=num_samples,
            preprocessed_dim=self._preprocessed_dim,
            numerical_columns=len(self._numerical_columns),
            categorical_columns=len(self._categorical_columns),
            datetime_columns=len(self._datetime_columns),
        )
        return result

    # -- Preprocessing helpers ------------------------------------------

    @staticmethod
    def _extract_col_info(col_def: Any) -> tuple[str, str]:
        """Extract column name and data type from a schema column definition."""
        if isinstance(col_def, dict):
            return col_def.get("name", ""), col_def.get("data_type", "string")
        return getattr(col_def, "name", ""), getattr(col_def, "data_type", "string")

    def _preprocess_numeric(
        self,
        col_name: str,
        col_type: str,
        col_profile: Dict[str, Any],
        num_samples: int,
        is_gan: bool,
    ) -> np.ndarray:
        """Generate and normalise synthetic samples for a numeric column."""
        mean_val = float(col_profile.get("mean", 0.0))
        std_val = max(float(col_profile.get("std", 1.0)), 0.001)
        min_val = float(
            col_profile.get("min", mean_val - 3 * std_val)
        )
        max_val = float(
            col_profile.get("max", mean_val + 3 * std_val)
        )

        samples = np.random.normal(loc=mean_val, scale=std_val, size=num_samples)
        samples = np.clip(samples, min_val, max_val)

        if col_type == "integer":
            samples = np.round(samples)

        data_range = max(max_val - min_val, 1e-9)

        if is_gan:
            normalized = 2.0 * (samples - min_val) / data_range - 1.0
        else:
            normalized = (samples - min_val) / data_range

        self._normalization_params[col_name] = {
            "min": min_val,
            "max": max_val,
            "range": data_range,
            "mean": mean_val,
            "std": std_val,
            "type": col_type,
        }
        return normalized.reshape(-1, 1)

    def _preprocess_categorical(
        self,
        col_name: str,
        col_type: str,
        col_profile: Dict[str, Any],
        num_samples: int,
    ) -> tuple[np.ndarray, int]:
        """Generate one-hot encoded samples for a categorical column."""
        categories = col_profile.get(
            "categories", col_profile.get("unique_values", [])
        )
        frequencies = col_profile.get(
            "frequencies", col_profile.get("value_counts", {})
        )

        if not categories:
            if col_type == "boolean":
                categories = ["True", "False"]
            else:
                num_default = min(10, max(2, len(frequencies) if frequencies else 2))
                categories = [f"{col_name}_cat_{i}" for i in range(num_default)]

        num_cats = len(categories)
        cat_to_idx = {str(cat): idx for idx, cat in enumerate(categories)}
        idx_to_cat = {idx: str(cat) for idx, cat in enumerate(categories)}

        # Build probability vector from frequencies
        if frequencies:
            probs = [
                float(
                    frequencies.get(str(cat), frequencies.get(cat, 1.0))
                )
                for cat in categories
            ]
            total = sum(probs)
            probs = [p / total for p in probs] if total > 0 else [1.0 / num_cats] * num_cats
        else:
            probs = [1.0 / num_cats] * num_cats

        cat_indices = np.random.choice(num_cats, size=num_samples, p=probs)
        one_hot = np.eye(num_cats, dtype=np.float32)[cat_indices]

        self._categorical_mappings[col_name] = {
            "cat_to_idx": cat_to_idx,
            "idx_to_cat": idx_to_cat,
            "num_categories": num_cats,
        }
        return one_hot, num_cats

    def _preprocess_temporal(
        self,
        col_name: str,
        col_type: str,
        col_profile: Dict[str, Any],
        num_samples: int,
        is_gan: bool,
    ) -> np.ndarray:
        """Generate and normalise synthetic samples for a temporal column."""
        min_ts = float(col_profile.get("min_timestamp", 0.0))
        max_ts = float(col_profile.get("max_timestamp", 1.0))

        if min_ts >= max_ts:
            max_ts = min_ts + 86400.0  # one day

        ts_range = max_ts - min_ts
        samples = np.random.uniform(min_ts, max_ts, size=num_samples)

        if is_gan:
            normalized = 2.0 * (samples - min_ts) / ts_range - 1.0
        else:
            normalized = (samples - min_ts) / ts_range

        self._normalization_params[col_name] = {
            "min": min_ts,
            "max": max_ts,
            "range": ts_range,
            "type": col_type,
        }
        return normalized.reshape(-1, 1)

    # ------------------------------------------------------------------
    # Postprocessing
    # ------------------------------------------------------------------

    def _postprocess_output(
        self,
        raw_output: np.ndarray,
        schema: dict[str, Any],
    ) -> pd.DataFrame:
        """Convert raw model output to a properly typed DataFrame.

        Reverses the preprocessing applied in :meth:`_preprocess_profile`:

        1. Denormalise numerical columns to original value ranges.
        2. Decode one-hot vectors back to categorical values.
        3. Round integer columns and clip to valid ranges.
        4. Cast each column to the appropriate dtype.
        5. Convert temporal columns to ``datetime64``.

        Args:
            raw_output: Raw model output ``(num_records, preprocessed_dim)``.
            schema: Table schema defining target column types.

        Returns:
            A :class:`~pandas.DataFrame` with columns matching the schema
            and proper dtypes.
        """
        columns_raw = schema.get("columns", [])
        num_records = raw_output.shape[0]
        result_dict: Dict[str, Any] = {}
        col_idx = 0  # Current position in the raw_output feature vector
        is_gan = self._aiml_config.model_type == "gan"

        for col_def in columns_raw:
            col_name, col_type = self._extract_col_info(col_def)
            constraints = (
                col_def.get("constraints", {})
                if isinstance(col_def, dict)
                else getattr(col_def, "constraints", {})
            ) or {}

            norm = self._normalization_params.get(col_name)
            cat_map = self._categorical_mappings.get(col_name)

            # --- Numerical / temporal columns ---
            if norm and col_type in (
                "integer",
                "float",
                "decimal",
                "date",
                "datetime",
                "timestamp",
            ):
                if col_idx < raw_output.shape[1]:
                    raw_col = raw_output[:, col_idx]
                    col_idx += 1
                else:
                    raw_col = np.zeros(num_records, dtype=np.float32)

                d_min = norm["min"]
                d_max = norm["max"]
                d_range = norm["range"]

                # Denormalise
                if is_gan:
                    denorm = (raw_col + 1.0) / 2.0 * d_range + d_min
                else:
                    denorm = raw_col * d_range + d_min

                denorm = np.clip(denorm, d_min, d_max)

                if col_type == "integer":
                    denorm = np.round(denorm).astype(np.int64)
                    result_dict[col_name] = denorm
                elif col_type in ("float", "decimal"):
                    result_dict[col_name] = denorm.astype(np.float64)
                elif col_type in ("date", "datetime", "timestamp"):
                    result_dict[col_name] = pd.to_datetime(
                        denorm, unit="s", errors="coerce"
                    )

            # --- Categorical columns ---
            elif cat_map:
                num_cats = cat_map["num_categories"]
                idx_to_cat = cat_map["idx_to_cat"]

                if col_idx + num_cats <= raw_output.shape[1]:
                    cat_probs = raw_output[:, col_idx : col_idx + num_cats]
                    col_idx += num_cats
                else:
                    remaining_cols = raw_output.shape[1] - col_idx
                    if remaining_cols > 0:
                        partial = raw_output[:, col_idx : col_idx + remaining_cols]
                        padding = np.zeros(
                            (num_records, num_cats - remaining_cols),
                            dtype=np.float32,
                        )
                        cat_probs = np.concatenate([partial, padding], axis=1)
                        col_idx += remaining_cols
                    else:
                        cat_probs = (
                            np.ones((num_records, num_cats), dtype=np.float32)
                            / num_cats
                        )

                cat_indices = np.argmax(cat_probs, axis=1)
                categories = [
                    idx_to_cat.get(int(idx), idx_to_cat.get(0, "unknown"))
                    for idx in cat_indices
                ]
                result_dict[col_name] = pd.Categorical(categories)

            # --- Fallback for unrecognised columns ---
            else:
                result_dict[col_name] = self._default_column(
                    col_type, num_records
                )

            # Apply explicit min/max constraints if present
            if col_name in result_dict and constraints:
                self._apply_constraints(
                    result_dict, col_name, col_type, constraints
                )

        df = pd.DataFrame(result_dict)

        # Ensure column order matches schema
        expected_cols = [
            self._extract_col_info(cd)[0] for cd in columns_raw
        ]
        ordered_cols = [c for c in expected_cols if c in df.columns]
        if ordered_cols:
            df = df[ordered_cols]

        self.logger.debug(
            "postprocessing_completed",
            num_records=len(df),
            num_columns=len(df.columns),
            columns=list(df.columns),
        )
        return df

    def _coerce_pretrained_output(
        self,
        raw_output: np.ndarray,
        schema: dict[str, Any],
    ) -> pd.DataFrame:
        """Wrap pre-trained model output into a schema-conforming DataFrame.

        Pre-trained :class:`TabularGAN` and :class:`TabularVAE` models
        handle their own internal postprocessing, so we only need to
        map the resulting numpy array to named columns and enforce
        dtype casting.

        Args:
            raw_output: Numpy array from the pre-trained model's
                ``generate()`` method.
            schema: Target schema.

        Returns:
            :class:`~pandas.DataFrame` matching the schema column order.
        """
        columns_raw = schema.get("columns", [])
        num_records = raw_output.shape[0]
        num_output_cols = raw_output.shape[1] if raw_output.ndim > 1 else 1

        col_names = [self._extract_col_info(cd)[0] for cd in columns_raw]
        col_types = [self._extract_col_info(cd)[1] for cd in columns_raw]

        result_dict: Dict[str, Any] = {}
        for idx, (name, dtype) in enumerate(zip(col_names, col_types)):
            if idx < num_output_cols:
                col_data = (
                    raw_output[:, idx] if raw_output.ndim > 1 else raw_output
                )
            else:
                col_data = self._default_column(dtype, num_records)
                result_dict[name] = col_data
                continue

            if dtype == "integer":
                result_dict[name] = np.round(col_data).astype(np.int64)
            elif dtype in ("float", "decimal"):
                result_dict[name] = col_data.astype(np.float64)
            elif dtype in ("date", "datetime", "timestamp"):
                result_dict[name] = pd.to_datetime(
                    col_data, unit="s", errors="coerce"
                )
            elif dtype == "boolean":
                result_dict[name] = col_data > 0.5
            else:
                result_dict[name] = col_data

        df = pd.DataFrame(result_dict)
        ordered = [c for c in col_names if c in df.columns]
        return df[ordered] if ordered else df

    # -- Postprocessing helpers -----------------------------------------

    @staticmethod
    def _default_column(col_type: str, num_records: int) -> Any:
        """Produce a default filler array for an unmapped column."""
        if col_type == "integer":
            return np.zeros(num_records, dtype=np.int64)
        if col_type in ("float", "decimal"):
            return np.zeros(num_records, dtype=np.float64)
        if col_type == "boolean":
            return np.random.choice([True, False], size=num_records)
        return np.zeros(num_records, dtype=np.float64)

    @staticmethod
    def _apply_constraints(
        result_dict: Dict[str, Any],
        col_name: str,
        col_type: str,
        constraints: Dict[str, Any],
    ) -> None:
        """Apply explicit min/max constraints to a result column in-place."""
        col_data = result_dict[col_name]
        if not isinstance(col_data, np.ndarray):
            return
        if col_type not in ("integer", "float", "decimal"):
            return
        if "min" in constraints:
            col_data = np.maximum(col_data, float(constraints["min"]))
        if "max" in constraints:
            col_data = np.minimum(col_data, float(constraints["max"]))
        result_dict[col_name] = col_data
