"""VAE (Variational Autoencoder) architecture for synthetic tabular data generation.

This module implements a complete VAE pipeline for generating high-fidelity
synthetic tabular data that mirrors the statistical properties of real ERP
system records. The architecture supports mixed column types (numerical and
categorical) with appropriate reconstruction losses and output activations
for each type.

Architecture Overview::

    Encoder:  Input -> [Dense -> BatchNorm -> Activation -> Dropout] x N -> z_mean, z_log_var
    Sampling: z = z_mean + exp(0.5 * z_log_var) * epsilon  (reparameterization trick)
    Decoder:  z -> [Dense -> BatchNorm -> Activation -> Dropout] x N -> Reconstructed Output

The ELBO (Evidence Lower Bound) loss combines:

* **Reconstruction loss** — MSE for numerical columns + BCE for categorical columns
* **KL divergence** — Regularizes the latent space to approximate N(0, I)
* **Total loss** = reconstruction_loss + kl_weight x kl_loss

Key Features:

* Configurable hyperparameters via :class:`VAEConfig` dataclass
* KL weight annealing via :class:`KLAnnealingCallback` to prevent posterior collapse
* GPU memory management with dynamic growth (``set_memory_growth``)
* Mixed precision training support when GPU is available
* Model persistence (TensorFlow SavedModel + JSON sidecar)
* Structured JSON logging for training progress, generation events, and errors

Usage::

    from generation_engine.models.vae_model import TabularVAE, VAEConfig

    config = VAEConfig(
        latent_dim=64,
        numerical_columns=[0, 1, 2, 3],
        categorical_columns=[{"index": 4, "num_categories": 5}],
    )
    vae = TabularVAE(config)
    history = vae.train(real_data)
    synthetic_data = vae.generate(num_samples=10000)
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass, field
from typing import Any

import numpy as np
import tensorflow as tf  # type: ignore[import-untyped]

from shared.logging.structured_logger import get_logger


# ---------------------------------------------------------------------------
# Module-level logger
# ---------------------------------------------------------------------------
logger = get_logger(__name__)


# ---------------------------------------------------------------------------
# GPU / Device Configuration
# ---------------------------------------------------------------------------


def _configure_gpu_memory() -> None:
    """Configure GPU memory growth to prevent TensorFlow from pre-allocating all VRAM.

    When multiple TensorFlow processes share the same GPU (common in
    microservice deployments), eager pre-allocation causes OOM crashes.
    Enabling memory growth allows TensorFlow to allocate GPU memory
    incrementally as needed.

    Also enables mixed-precision compute (``float16``) when a GPU is
    detected, providing ~2x throughput improvement on Tensor-Core GPUs
    with minimal accuracy impact for the VAE training workload.
    """
    try:
        gpus = tf.config.list_physical_devices("GPU")
        if gpus:
            for gpu in gpus:
                tf.config.experimental.set_memory_growth(gpu, True)
            logger.info(
                "gpu_memory_configured",
                num_gpus=len(gpus),
                gpu_names=[gpu.name for gpu in gpus],
            )
            # Enable mixed precision for Tensor-Core GPUs
            try:
                tf.keras.mixed_precision.set_global_policy("mixed_float16")
                logger.info("mixed_precision_enabled", policy="mixed_float16")
            except Exception as mp_exc:
                logger.debug(
                    "mixed_precision_unavailable",
                    reason=str(mp_exc),
                )
        else:
            logger.info("no_gpu_detected", device="CPU")
    except RuntimeError as exc:
        # GPU config must happen before any TF operation
        logger.warning("gpu_memory_config_failed", error=str(exc))


_configure_gpu_memory()


# ---------------------------------------------------------------------------
# VAEConfig Dataclass
# ---------------------------------------------------------------------------


@dataclass
class VAEConfig:
    """Configuration dataclass for the Tabular VAE model.

    Encapsulates all hyper-parameters for the encoder, decoder, training
    loop, and inference pipeline.  Mutable collection defaults use
    ``field(default_factory=...)`` to avoid shared-state bugs.

    Attributes:
        latent_dim: Dimension of the latent space *z*.  Higher values
            capture more complex data distributions but increase model
            size and training time.
        encoder_hidden_dims: Sizes of hidden layers in the encoder,
            applied in order from input towards latent space.
        decoder_hidden_dims: Sizes of hidden layers in the decoder,
            applied in order from latent space towards output.
        learning_rate: Initial learning rate for the Adam optimiser.
        batch_size: Number of samples per training mini-batch.
        num_epochs: Maximum number of training epochs.
        kl_weight: KL divergence weight (β in β-VAE).  Controls the
            trade-off between reconstruction fidelity and latent-space
            regularisation.
        kl_annealing: Whether to linearly anneal the KL weight from 0
            to ``kl_weight`` over ``kl_annealing_epochs``.
        kl_annealing_epochs: Epoch count for the linear KL warmup ramp.
        dropout_rate: Dropout probability applied after every hidden
            layer in both encoder and decoder.
        input_dim: Total number of input features **after** preprocessing
            (numerical columns + one-hot-encoded categorical columns).
            Automatically set by :meth:`TabularVAE.train`.
        numerical_columns: Indices of numerical columns in the raw data.
        categorical_columns: Metadata for categorical columns — each
            dict must contain ``"index"`` (column position) and
            ``"num_categories"`` (number of unique categories).
        activation: Activation function for hidden layers
            (``"relu"`` or ``"leaky_relu"``).
        output_activation: Activation applied to numerical output
            neurons (``"sigmoid"`` for min-max-normalised data).
    """

    latent_dim: int = 64
    encoder_hidden_dims: list[int] = field(
        default_factory=lambda: [256, 128],
    )
    decoder_hidden_dims: list[int] = field(
        default_factory=lambda: [128, 256],
    )
    learning_rate: float = 0.001
    batch_size: int = 256
    num_epochs: int = 200
    kl_weight: float = 1.0
    kl_annealing: bool = True
    kl_annealing_epochs: int = 50
    dropout_rate: float = 0.2
    input_dim: int = 0
    numerical_columns: list[int] = field(default_factory=list)
    categorical_columns: list[dict] = field(default_factory=list)
    activation: str = "relu"
    output_activation: str = "sigmoid"


# ---------------------------------------------------------------------------
# Sampling Layer — Reparameterization Trick
# ---------------------------------------------------------------------------


class Sampling(tf.keras.layers.Layer):
    """Keras layer implementing the VAE reparameterization trick.

    Given the encoder outputs ``z_mean`` and ``z_log_var``, this layer
    produces a differentiable sample from
    *q(z | x) = N(z_mean, exp(z_log_var))* via::

        z = z_mean + exp(0.5 * z_log_var) * ε,   ε ~ N(0, I)

    Using the reparameterization trick allows gradients to flow through
    the stochastic sampling step, which is essential for end-to-end
    back-propagation during VAE training.
    """

    def __init__(self, **kwargs: Any) -> None:
        """Initialise the Sampling layer.

        Args:
            **kwargs: Additional keyword arguments forwarded to the
                parent ``keras.layers.Layer`` constructor.
        """
        super().__init__(**kwargs)

    def call(self, inputs: list) -> tf.Tensor:
        """Sample a latent vector using the reparameterization trick.

        Args:
            inputs: Two-element list ``[z_mean, z_log_var]`` where each
                tensor has shape ``(batch_size, latent_dim)``.

        Returns:
            Sampled latent vector *z* with shape ``(batch_size, latent_dim)``.
        """
        z_mean, z_log_var = inputs
        batch = tf.shape(z_mean)[0]
        dim = tf.shape(z_mean)[1]
        epsilon = tf.random.normal(shape=(batch, dim))
        return z_mean + tf.exp(0.5 * z_log_var) * epsilon

    def get_config(self) -> dict[str, Any]:
        """Return the layer configuration for serialisation."""
        config: dict[str, Any] = super().get_config()
        return config


# ---------------------------------------------------------------------------
# Encoder Builder
# ---------------------------------------------------------------------------


def build_encoder(input_dim: int, config: VAEConfig) -> tf.keras.Model:
    """Build the encoder sub-model mapping input data to latent parameters.

    Architecture::

        Input(input_dim)
          -> [Dense(h) -> BatchNorm -> Activation -> Dropout] x len(encoder_hidden_dims)
          -> Dense(latent_dim)  [z_mean]
          -> Dense(latent_dim)  [z_log_var]
          -> Sampling           [z]

    Args:
        input_dim: Number of input features after preprocessing.
        config: :class:`VAEConfig` containing encoder hyper-parameters.

    Returns:
        A ``keras.Model`` with input shape ``(batch, input_dim)`` and
        three outputs ``[z_mean, z_log_var, z]``, each of shape
        ``(batch, latent_dim)``.

    Raises:
        ValueError: If ``input_dim < 1``.
    """
    if input_dim < 1:
        raise ValueError(f"input_dim must be >= 1, got {input_dim}")

    inputs = tf.keras.Input(shape=(input_dim,), name="encoder_input")
    x = inputs

    for idx, hidden_dim in enumerate(config.encoder_hidden_dims):
        x = tf.keras.layers.Dense(hidden_dim, name=f"enc_dense_{idx}")(x)
        x = tf.keras.layers.BatchNormalization(name=f"enc_bn_{idx}")(x)
        if config.activation == "leaky_relu":
            x = tf.keras.layers.LeakyReLU(name=f"enc_act_{idx}")(x)
        else:
            x = tf.keras.layers.Activation("relu", name=f"enc_act_{idx}")(x)
        x = tf.keras.layers.Dropout(config.dropout_rate, name=f"enc_drop_{idx}")(x)

    z_mean = tf.keras.layers.Dense(config.latent_dim, name="z_mean")(x)
    z_log_var = tf.keras.layers.Dense(config.latent_dim, name="z_log_var")(x)
    z = Sampling(name="z")([z_mean, z_log_var])

    encoder = tf.keras.Model(inputs, [z_mean, z_log_var, z], name="encoder")
    logger.debug(
        "encoder_built",
        input_dim=input_dim,
        hidden_dims=config.encoder_hidden_dims,
        latent_dim=config.latent_dim,
    )
    return encoder


# ---------------------------------------------------------------------------
# Decoder activation helper
# ---------------------------------------------------------------------------


def _apply_decoder_activations(
    raw_output: tf.Tensor,
    output_dim: int,
    config: VAEConfig,
    numerical_output_indices: list[int],
    categorical_output_slices: list[tuple[int, int]],
) -> tf.Tensor:
    """Apply per-column-type activations (sigmoid / softmax) to decoder output.

    When column metadata is not available a single global activation
    (from ``config.output_activation``) is used instead.

    Args:
        raw_output: Raw logits tensor of shape ``(batch, output_dim)``.
        output_dim: Total number of output features.
        config: VAE configuration.
        numerical_output_indices: Indices for numerical columns.
        categorical_output_slices: ``(start, end)`` pairs for categoricals.

    Returns:
        Activated output tensor of shape ``(batch, output_dim)``.
    """
    has_column_info = bool(numerical_output_indices or categorical_output_slices)

    if not has_column_info:
        return tf.keras.layers.Activation(
            config.output_activation,
            name="dec_output_act",
        )(raw_output)

    # Build ordered segments of (start, end, type) across output_dim
    cat_starts: dict[int, tuple[int, int]] = {s: (s, e) for s, e in categorical_output_slices}
    segments: list[tuple[int, int, str]] = []
    pos = 0
    while pos < output_dim:
        if pos in cat_starts:
            seg_start, seg_end = cat_starts[pos]
            segments.append((seg_start, seg_end, "categorical"))
            pos = seg_end
        else:
            num_start = pos
            while pos < output_dim and pos not in cat_starts:
                pos += 1
            segments.append((num_start, pos, "numerical"))

    parts: list[tf.Tensor] = []
    for seg_start, seg_end, seg_type in segments:
        segment = raw_output[:, seg_start:seg_end]
        if seg_type == "numerical":
            activated = tf.keras.layers.Activation(
                "sigmoid",
                name=f"sig_{seg_start}_{seg_end}",
            )(segment)
        else:
            activated = tf.keras.layers.Activation(
                "softmax",
                name=f"sfx_{seg_start}_{seg_end}",
            )(segment)
        parts.append(activated)

    if len(parts) == 1:
        return parts[0]
    return tf.keras.layers.Concatenate(name="dec_output_concat")(parts)


# ---------------------------------------------------------------------------
# Decoder Builder
# ---------------------------------------------------------------------------


def build_decoder(
    latent_dim: int,
    output_dim: int,
    config: VAEConfig,
    numerical_output_indices: list[int],
    categorical_output_slices: list[tuple[int, int]],
) -> tf.keras.Model:
    """Build the decoder sub-model reconstructing data from latent vectors.

    Architecture::

        Input(latent_dim)
          -> [Dense(h) -> BatchNorm -> Activation -> Dropout] x len(decoder_hidden_dims)
          -> Dense(output_dim)  [raw logits]
          -> sigmoid on numerical indices, softmax on categorical groups

    Args:
        latent_dim: Dimension of the latent space.
        output_dim: Total number of output features after preprocessing.
        config: :class:`VAEConfig` with decoder hyper-parameters.
        numerical_output_indices: Preprocessed-output indices that
            correspond to numerical columns.
        categorical_output_slices: ``(start, end)`` index pairs
            delimiting each one-hot categorical group.

    Returns:
        A ``keras.Model`` mapping latent vectors to reconstructed output.

    Raises:
        ValueError: If ``latent_dim < 1`` or ``output_dim < 1``.
    """
    if latent_dim < 1:
        raise ValueError(f"latent_dim must be >= 1, got {latent_dim}")
    if output_dim < 1:
        raise ValueError(f"output_dim must be >= 1, got {output_dim}")

    inputs = tf.keras.Input(shape=(latent_dim,), name="decoder_input")
    x = inputs

    for idx, hidden_dim in enumerate(config.decoder_hidden_dims):
        x = tf.keras.layers.Dense(hidden_dim, name=f"dec_dense_{idx}")(x)
        x = tf.keras.layers.BatchNormalization(name=f"dec_bn_{idx}")(x)
        if config.activation == "leaky_relu":
            x = tf.keras.layers.LeakyReLU(name=f"dec_act_{idx}")(x)
        else:
            x = tf.keras.layers.Activation("relu", name=f"dec_act_{idx}")(x)
        x = tf.keras.layers.Dropout(config.dropout_rate, name=f"dec_drop_{idx}")(x)

    # Raw output logits (no activation)
    raw_output = tf.keras.layers.Dense(output_dim, name="dec_output_raw")(x)

    # ----- Apply column-type-specific activations -----
    activated_output = _apply_decoder_activations(
        raw_output,
        output_dim,
        config,
        numerical_output_indices,
        categorical_output_slices,
    )

    decoder = tf.keras.Model(inputs, activated_output, name="decoder")
    logger.debug(
        "decoder_built",
        latent_dim=latent_dim,
        output_dim=output_dim,
        hidden_dims=config.decoder_hidden_dims,
        num_numerical=len(numerical_output_indices),
        num_categorical_groups=len(categorical_output_slices),
    )
    return decoder


# ---------------------------------------------------------------------------
# KL Annealing Callback
# ---------------------------------------------------------------------------


class KLAnnealingCallback(tf.keras.callbacks.Callback):
    """Keras callback implementing linear KL-weight annealing.

    Linearly increases the KL divergence weight from **0** to
    ``max_kl_weight`` over ``total_annealing_epochs``.  This warmup
    strategy prevents *posterior collapse* (the KL term vanishing to
    zero) in the early stages of training by letting the reconstruction
    loss dominate initially.

    The callback modifies ``self.model._current_kl_weight`` which is
    read inside :meth:`TabularVAE.train_step` at every iteration.

    Attributes:
        total_annealing_epochs: Number of epochs for the linear ramp.
        max_kl_weight: Target KL weight reached after annealing.
    """

    def __init__(
        self,
        total_annealing_epochs: int,
        max_kl_weight: float = 1.0,
    ) -> None:
        """Initialise the KL annealing callback.

        Args:
            total_annealing_epochs: Epoch count for the linear warmup.
            max_kl_weight: Maximum KL weight after annealing completes.
        """
        super().__init__()
        self.total_annealing_epochs: int = max(total_annealing_epochs, 1)
        self.max_kl_weight: float = max_kl_weight

    def on_epoch_begin(
        self,
        epoch: int,
        _logs: dict[str, Any] | None = None,
    ) -> None:
        """Update the KL weight at the start of each epoch.

        The weight increases linearly from 0.0 to ``max_kl_weight`` over
        ``total_annealing_epochs``.  After the ramp completes the weight
        stays at ``max_kl_weight``.

        Args:
            epoch: Current epoch index (0-based).
            logs: Optional dict of metric values (unused).
        """
        if epoch < self.total_annealing_epochs:
            new_weight = self.max_kl_weight * (epoch / self.total_annealing_epochs)
        else:
            new_weight = self.max_kl_weight

        if hasattr(self.model, "_current_kl_weight"):
            self.model._current_kl_weight = new_weight

        # Periodic progress logging (every 10 epochs)
        if epoch % 10 == 0:
            logger.debug(
                "kl_weight_annealed",
                epoch=epoch,
                kl_weight=round(new_weight, 6),
                annealing_complete=epoch >= self.total_annealing_epochs,
            )


# ---------------------------------------------------------------------------
# TabularVAE — Main Public Interface
# ---------------------------------------------------------------------------


class TabularVAE(tf.keras.Model):
    """Variational Autoencoder for synthetic tabular data generation.

    Implements the complete VAE pipeline:

    1. **Preprocessing** — min-max normalisation (numerical) and one-hot
       encoding (categorical).
    2. **Encoding** — maps preprocessed data to a continuous latent space
       parameterised by *z_mean* and *z_log_var*.
    3. **Sampling** — reparameterization trick for differentiable sampling.
    4. **Decoding** — reconstructs data from latent representations with
       column-type-appropriate output activations.
    5. **Training** — custom ELBO loss (reconstruction + KL divergence)
       with KL annealing, early stopping, and LR scheduling.
    6. **Generation** — samples latent vectors from N(0, I), decodes, and
       post-processes back to the original data format.
    7. **Persistence** — save/load via TensorFlow SavedModel + JSON sidecar.

    Example::

        config = VAEConfig(
            latent_dim=64,
            numerical_columns=[0, 1, 2],
            categorical_columns=[{"index": 3, "num_categories": 5}],
        )
        vae = TabularVAE(config)
        history = vae.train(training_data, validation_split=0.1)
        synthetic = vae.generate(num_samples=10_000)
        vae.save_model("/models/vae_v1")
    """

    def __init__(self, config: VAEConfig) -> None:
        """Initialise the TabularVAE model.

        If ``config.input_dim > 0`` the encoder and decoder are built
        immediately; otherwise they are deferred until :meth:`train` is
        called and the input dimensionality can be inferred from the data.

        Args:
            config: :class:`VAEConfig` dataclass with all hyper-parameters.

        Raises:
            ValueError: If *config* is ``None``.
        """
        super().__init__(name="tabular_vae")
        if config is None:
            raise ValueError("config must not be None")

        self.config: VAEConfig = config
        self._logger = get_logger(__name__)

        # --- Normalisation / encoding state (populated by train) -----------
        self._num_mins: np.ndarray | None = None
        self._num_maxs: np.ndarray | None = None
        self._num_ranges: np.ndarray | None = None
        self._category_maps: dict[int, dict[int, Any]] = {}
        self._original_num_columns: int = 0

        # --- Output-index mappings (populated when submodels are built) ----
        self._numerical_output_indices: list[int] = []
        self._categorical_output_slices: list[tuple[int, int]] = []
        self._preprocessed_dim: int = 0

        # --- KL weight (mutated by KLAnnealingCallback) --------------------
        self._current_kl_weight: float = config.kl_weight

        # --- Training book-keeping -----------------------------------------
        self._training_history: dict[str, Any] | None = None
        self._is_trained: bool = False
        self._final_epoch: int = 0

        # --- Keras metrics trackers ----------------------------------------
        self.total_loss_tracker = tf.keras.metrics.Mean(name="total_loss")
        self.reconstruction_loss_tracker = tf.keras.metrics.Mean(
            name="reconstruction_loss",
        )
        self.kl_loss_tracker = tf.keras.metrics.Mean(name="kl_loss")

        # --- Sub-models (conditionally built) ------------------------------
        self.encoder: tf.keras.Model | None = None
        self.decoder: tf.keras.Model | None = None
        if config.input_dim > 0:
            self._build_submodels()

        self._logger.info(
            "tabular_vae_initialized",
            latent_dim=config.latent_dim,
            input_dim=config.input_dim,
            num_numerical=len(config.numerical_columns),
            num_categorical=len(config.categorical_columns),
        )

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _compute_output_mappings(self) -> None:
        """Derive preprocessed-output index mappings from column metadata.

        After preprocessing the output dimension is::

            len(numerical_columns) + Σ num_categories_i

        This helper populates ``_numerical_output_indices`` and
        ``_categorical_output_slices`` so that the decoder and loss
        computation can apply the correct activations per segment.
        """
        self._numerical_output_indices = []
        self._categorical_output_slices = []
        current_idx = 0

        # Numerical columns → one output index each
        for _ in self.config.numerical_columns:
            self._numerical_output_indices.append(current_idx)
            current_idx += 1

        # Categorical columns → contiguous one-hot slices
        for cat_meta in self.config.categorical_columns:
            num_cats = cat_meta.get("num_categories", 2)
            start = current_idx
            end = current_idx + num_cats
            self._categorical_output_slices.append((start, end))
            current_idx += num_cats

        self._preprocessed_dim = current_idx

    def _build_submodels(self) -> None:
        """(Re-)build encoder and decoder from current config & mappings."""
        self._compute_output_mappings()

        if self._preprocessed_dim == 0:
            # Fallback when no column-type metadata is provided
            self._preprocessed_dim = self.config.input_dim

        self.encoder = build_encoder(self._preprocessed_dim, self.config)
        self.decoder = build_decoder(
            self.config.latent_dim,
            self._preprocessed_dim,
            self.config,
            self._numerical_output_indices,
            self._categorical_output_slices,
        )

    # ------------------------------------------------------------------
    # Keras Model overrides
    # ------------------------------------------------------------------

    @property
    def metrics(self) -> list[tf.keras.metrics.Metric]:
        """Return tracked training metrics for Keras progress display."""
        return [
            self.total_loss_tracker,
            self.reconstruction_loss_tracker,
            self.kl_loss_tracker,
        ]

    def call(
        self,
        inputs: tf.Tensor,
        training: bool = False,
    ) -> tf.Tensor:
        """Forward pass: encode → sample → decode.

        Args:
            inputs: Tensor of shape ``(batch, preprocessed_dim)``.
            training: Whether the forward pass is for training
                (affects dropout and batch-norm behaviour).

        Returns:
            Reconstructed tensor of the same shape as *inputs*.
        """
        if self.encoder is None or self.decoder is None:
            raise RuntimeError("VAE encoder/decoder not built. Call build_model() first.")
        _z_mean, _z_log_var, z = self.encoder(inputs, training=training)
        reconstructed = self.decoder(z, training=training)
        return reconstructed

    def compute_loss(
        self,
        x: tf.Tensor,
        x_reconstructed: tf.Tensor,
        z_mean: tf.Tensor,
        z_log_var: tf.Tensor,
        kl_weight: float = 1.0,
    ) -> tuple[tf.Tensor, tf.Tensor, tf.Tensor]:
        """Compute the ELBO loss (reconstruction + KL divergence).

        .. math::

            \\mathcal{L} = \\mathcal{L}_{\\text{recon}}
                           + \\beta \\cdot \\mathcal{L}_{\\text{KL}}

        Where:

        * :math:`\\mathcal{L}_{\\text{recon}}` is MSE for numerical
          columns plus binary cross-entropy for categorical columns.
        * :math:`\\mathcal{L}_{\\text{KL}} = -0.5 \\cdot \\mathbb{E}
          [1 + \\log \\sigma^2 - \\mu^2 - \\sigma^2]`

        Args:
            x: Original input tensor ``(batch, dim)``.
            x_reconstructed: Reconstructed tensor ``(batch, dim)``.
            z_mean: Latent mean ``(batch, latent_dim)``.
            z_log_var: Latent log-variance ``(batch, latent_dim)``.
            kl_weight: Scalar weight for the KL term (β).

        Returns:
            Tuple of ``(total_loss, reconstruction_loss, kl_loss)`` as
            scalar tensors.
        """
        reconstruction_loss = tf.constant(0.0, dtype=tf.float32)

        # --- Numerical: MSE ------------------------------------------------
        if self._numerical_output_indices:
            num_idx = self._numerical_output_indices
            x_num = tf.gather(x, num_idx, axis=1)
            x_recon_num = tf.gather(x_reconstructed, num_idx, axis=1)
            numerical_mse = tf.reduce_mean(tf.square(x_num - x_recon_num))
            reconstruction_loss = reconstruction_loss + numerical_mse

        # --- Categorical: Binary Cross-Entropy per one-hot group -----------
        if self._categorical_output_slices:
            cat_bce_total = tf.constant(0.0, dtype=tf.float32)
            for start, end in self._categorical_output_slices:
                x_cat = x[:, start:end]
                x_recon_cat = tf.clip_by_value(
                    x_reconstructed[:, start:end],
                    1e-7,
                    1.0 - 1e-7,
                )
                bce = tf.reduce_mean(
                    tf.keras.losses.binary_crossentropy(x_cat, x_recon_cat),
                )
                cat_bce_total = cat_bce_total + bce
            num_groups = tf.cast(
                max(len(self._categorical_output_slices), 1),
                tf.float32,
            )
            reconstruction_loss = reconstruction_loss + cat_bce_total / num_groups

        # Fallback when column-type info is absent
        if not self._numerical_output_indices and not self._categorical_output_slices:
            reconstruction_loss = tf.reduce_mean(tf.square(x - x_reconstructed))

        # --- KL divergence -------------------------------------------------
        kl_loss = -0.5 * tf.reduce_mean(
            1.0 + z_log_var - tf.square(z_mean) - tf.exp(z_log_var),
        )

        # --- Total ELBO loss -----------------------------------------------
        total_loss = reconstruction_loss + kl_weight * kl_loss

        return total_loss, reconstruction_loss, kl_loss

    def train_step(self, data: tf.Tensor) -> dict[str, tf.Tensor]:
        """Custom training step implementing VAE-specific gradient update.

        Overrides ``keras.Model.train_step`` to:

        1. Forward-pass through encoder and decoder.
        2. Compute the ELBO loss with the current (possibly annealed) KL
           weight.
        3. Back-propagate and apply parameter gradients via the optimiser.
        4. Update running metric trackers.

        Args:
            data: Batch tensor of shape ``(batch, preprocessed_dim)``.

        Returns:
            Dictionary mapping metric names to their current batch values.
        """
        if self.encoder is None or self.decoder is None:
            raise RuntimeError("VAE encoder/decoder not built. Call build_model() first.")
        with tf.GradientTape() as tape:
            z_mean, z_log_var, z = self.encoder(data, training=True)
            reconstructed = self.decoder(z, training=True)
            total_loss, recon_loss, kl_loss = self.compute_loss(
                data,
                reconstructed,
                z_mean,
                z_log_var,
                kl_weight=self._current_kl_weight,
            )

        grads = tape.gradient(total_loss, self.trainable_weights)
        self.optimizer.apply_gradients(zip(grads, self.trainable_weights, strict=False))

        self.total_loss_tracker.update_state(total_loss)
        self.reconstruction_loss_tracker.update_state(recon_loss)
        self.kl_loss_tracker.update_state(kl_loss)

        return {
            "total_loss": self.total_loss_tracker.result(),
            "reconstruction_loss": self.reconstruction_loss_tracker.result(),
            "kl_loss": self.kl_loss_tracker.result(),
        }

    # ------------------------------------------------------------------
    # Data preprocessing / post-processing
    # ------------------------------------------------------------------

    def _preprocess_data(self, real_data: np.ndarray) -> np.ndarray:
        """Transform raw data for VAE consumption.

        * Numerical columns → min-max normalised to [0, 1].
        * Categorical columns → one-hot encoded.

        Normalisation parameters and category mappings are stored on the
        instance for use during :meth:`generate` post-processing.

        Args:
            real_data: Array ``(n_samples, n_raw_columns)``.

        Returns:
            Preprocessed array ``(n_samples, preprocessed_dim)``.
        """
        parts: list[np.ndarray] = []

        # --- Numerical columns: min-max scaling ----------------------------
        if self.config.numerical_columns:
            num_data = real_data[:, self.config.numerical_columns].astype(
                np.float32,
            )
            self._num_mins = np.min(num_data, axis=0)
            self._num_maxs = np.max(num_data, axis=0)
            self._num_ranges = self._num_maxs - self._num_mins
            # Guard against constant columns (range == 0)
            self._num_ranges[self._num_ranges == 0] = 1.0
            normalised = (num_data - self._num_mins) / self._num_ranges
            parts.append(normalised)

        # --- Categorical columns: one-hot encoding -------------------------
        for cat_meta in self.config.categorical_columns:
            col_idx: int = cat_meta["index"]
            num_cats: int = cat_meta.get("num_categories", 2)
            col_data = real_data[:, col_idx]

            unique_vals = sorted(set(col_data.tolist()))
            cat_to_idx: dict[Any, int] = {val: i for i, val in enumerate(unique_vals[:num_cats])}
            idx_to_cat: dict[int, Any] = {i: val for val, i in cat_to_idx.items()}
            self._category_maps[col_idx] = idx_to_cat

            one_hot = np.zeros((len(col_data), num_cats), dtype=np.float32)
            for row_i, val in enumerate(col_data):
                encoded_idx = cat_to_idx.get(val, 0)
                one_hot[row_i, encoded_idx] = 1.0
            parts.append(one_hot)

        # Fallback: treat every column as numerical if no metadata provided
        if not parts:
            all_data = real_data.astype(np.float32)
            self._num_mins = np.min(all_data, axis=0)
            self._num_maxs = np.max(all_data, axis=0)
            self._num_ranges = self._num_maxs - self._num_mins
            self._num_ranges[self._num_ranges == 0] = 1.0
            result: np.ndarray = (all_data - self._num_mins) / self._num_ranges
            return result

        return np.concatenate(parts, axis=1)

    def _postprocess_data(self, generated: np.ndarray) -> np.ndarray:
        """Reverse preprocessing to restore the original data format.

        * Numerical columns → de-normalised from [0, 1] to original range.
        * Categorical columns → argmax over one-hot probabilities, mapped
          back to original category values.

        Args:
            generated: Decoder output ``(n_samples, preprocessed_dim)``.

        Returns:
            Post-processed array ``(n_samples, n_raw_columns)``.
        """
        num_numerical = len(self.config.numerical_columns)
        num_categorical = len(self.config.categorical_columns)
        total_raw_cols = num_numerical + num_categorical
        n_samples = generated.shape[0]

        # No column metadata → simple de-normalisation
        if total_raw_cols == 0:
            if self._num_mins is not None and self._num_ranges is not None:
                denormed: np.ndarray = generated * self._num_ranges + self._num_mins
                return denormed
            return generated

        # Use original column count if known, else fall back to sum
        out_cols = max(self._original_num_columns, total_raw_cols)
        result = np.zeros((n_samples, out_cols), dtype=np.float64)

        # --- De-normalise numerical columns --------------------------------
        current_idx = 0
        if num_numerical > 0 and self._num_mins is not None:
            num_block = generated[:, :num_numerical]
            denorm = num_block * self._num_ranges + self._num_mins
            for i, raw_col_idx in enumerate(self.config.numerical_columns):
                if raw_col_idx < out_cols:
                    result[:, raw_col_idx] = denorm[:, i]
            current_idx = num_numerical

        # --- Decode categorical columns ------------------------------------
        for cat_meta in self.config.categorical_columns:
            col_idx: int = cat_meta["index"]
            num_cats: int = cat_meta.get("num_categories", 2)
            start = current_idx
            end = current_idx + num_cats

            if end <= generated.shape[1]:
                cat_probs = generated[:, start:end]
                cat_indices = np.argmax(cat_probs, axis=1)

                idx_to_cat = self._category_maps.get(col_idx, {})
                for row_i in range(n_samples):
                    cat_val = idx_to_cat.get(int(cat_indices[row_i]), 0)
                    if isinstance(cat_val, (int, float)):
                        result[row_i, col_idx] = cat_val
                    else:
                        # Non-numeric categories are hashed for array compat
                        result[row_i, col_idx] = float(
                            hash(str(cat_val)) % 10_000,
                        )

            current_idx = end

        return result

    # ------------------------------------------------------------------
    # Public API: train / generate / encode
    # ------------------------------------------------------------------

    def train(
        self,
        real_data: np.ndarray,
        validation_split: float = 0.1,
        callbacks_list: list[Any] | None = None,
    ) -> dict[str, Any]:
        """Train the VAE on real tabular data.

        End-to-end training pipeline:

        1. Preprocess ``real_data`` (normalise + one-hot encode).
        2. Build (or rebuild) encoder / decoder if dimensions changed.
        3. Compile the model with Adam optimiser.
        4. Fit with EarlyStopping, ReduceLROnPlateau, and optional
           KL-annealing callbacks.
        5. Store training history and normalization state.

        Args:
            real_data: Raw data ``(n_samples, n_columns)`` as a NumPy
                array.
            validation_split: Fraction of data reserved for validation
                (0.0-1.0).  Set to 0.0 to disable validation.
            callbacks_list: Optional extra Keras callbacks appended to
                the default set.

        Returns:
            Dictionary mapping metric names to lists of per-epoch values
            (mirrors the ``keras.History.history`` dict).

        Raises:
            ValueError: If ``real_data`` is empty or ``None``.
            RuntimeError: If TensorFlow encounters a fatal error (OOM,
                device failure, etc.).
        """
        if real_data is None or len(real_data) == 0:
            raise ValueError("real_data must be a non-empty numpy array")

        self._original_num_columns = real_data.shape[1] if real_data.ndim > 1 else 1

        self._logger.info(
            "vae_training_started",
            num_samples=real_data.shape[0],
            num_raw_columns=self._original_num_columns,
            latent_dim=self.config.latent_dim,
            num_epochs=self.config.num_epochs,
            batch_size=self.config.batch_size,
            kl_annealing=self.config.kl_annealing,
        )

        try:
            # ---- Preprocess -----------------------------------------------
            preprocessed = self._preprocess_data(real_data)
            self._preprocessed_dim = preprocessed.shape[1]
            self.config.input_dim = self._preprocessed_dim

            # ---- Build / rebuild submodels --------------------------------
            self._build_submodels()

            # ---- Compile with Adam ----------------------------------------
            self.compile(
                optimizer=tf.keras.optimizers.Adam(
                    learning_rate=self.config.learning_rate,
                ),
            )

            # ---- Assemble callbacks ---------------------------------------
            monitor = "val_total_loss" if validation_split > 0 else "total_loss"
            training_callbacks: list[Any] = [
                tf.keras.callbacks.EarlyStopping(
                    monitor=monitor,
                    patience=20,
                    restore_best_weights=True,
                    verbose=1,
                    mode="min",
                ),
                tf.keras.callbacks.ReduceLROnPlateau(
                    monitor=monitor,
                    factor=0.5,
                    patience=10,
                    min_lr=1e-6,
                    verbose=1,
                    mode="min",
                ),
            ]

            if self.config.kl_annealing:
                self._current_kl_weight = 0.0
                training_callbacks.append(
                    KLAnnealingCallback(
                        total_annealing_epochs=self.config.kl_annealing_epochs,
                        max_kl_weight=self.config.kl_weight,
                    ),
                )
            else:
                self._current_kl_weight = self.config.kl_weight

            if callbacks_list:
                training_callbacks.extend(callbacks_list)

            # ---- Fit ------------------------------------------------------
            history = self.fit(
                preprocessed,
                epochs=self.config.num_epochs,
                batch_size=self.config.batch_size,
                validation_split=validation_split,
                callbacks=training_callbacks,
                verbose=0,
            )

            # ---- Record results -------------------------------------------
            self._is_trained = True
            self._final_epoch = len(history.history.get("total_loss", []))
            self._training_history = {key: [float(v) for v in vals] for key, vals in history.history.items()}

            final_total = history.history.get("total_loss", [0.0])[-1]
            final_recon = history.history.get("reconstruction_loss", [0.0])[-1]
            final_kl = history.history.get("kl_loss", [0.0])[-1]

            self._logger.info(
                "vae_training_completed",
                epochs_trained=self._final_epoch,
                final_total_loss=round(float(final_total), 6),
                final_reconstruction_loss=round(float(final_recon), 6),
                final_kl_loss=round(float(final_kl), 6),
                kl_weight=round(float(self._current_kl_weight), 6),
            )

            return self._training_history

        except tf.errors.ResourceExhaustedError as exc:
            self._logger.error(
                "vae_training_oom",
                error=str(exc),
                batch_size=self.config.batch_size,
                latent_dim=self.config.latent_dim,
            )
            raise RuntimeError(
                f"Out of memory during VAE training.  Consider reducing "
                f"batch_size (current: {self.config.batch_size}) or "
                f"latent_dim (current: {self.config.latent_dim}).  "
                f"Original error: {exc}",
            ) from exc
        except Exception as exc:
            self._logger.error(
                "vae_training_failed",
                error=str(exc),
                error_type=type(exc).__name__,
            )
            raise

    def generate(self, num_samples: int) -> np.ndarray:
        """Generate synthetic tabular data from the learned distribution.

        Samples latent vectors from N(0, I), decodes them through the
        decoder, and post-processes the output to match the original data
        format (de-normalised numerical, decoded categorical).

        Args:
            num_samples: Number of synthetic records to produce.

        Returns:
            NumPy array ``(num_samples, n_raw_columns)`` of synthetic data
            in the original (non-preprocessed) column format.

        Raises:
            RuntimeError: If the model has not been trained yet.
            ValueError: If ``num_samples < 1``.
        """
        if self.decoder is None:
            raise RuntimeError(
                "Model must be trained or loaded before generating data",
            )
        if num_samples < 1:
            raise ValueError(f"num_samples must be >= 1, got {num_samples}")

        self._logger.info(
            "vae_generation_started",
            num_samples=num_samples,
            latent_dim=self.config.latent_dim,
        )

        try:
            # Sample latent vectors from standard normal
            z = np.random.normal(
                size=(num_samples, self.config.latent_dim),
            ).astype(np.float32)

            # Decode in manageable batches
            generated_preprocessed = self.decoder.predict(
                z,
                batch_size=self.config.batch_size,
            )

            # Post-process to original format
            synthetic_data = self._postprocess_data(generated_preprocessed)

            self._logger.info(
                "vae_generation_completed",
                num_samples=num_samples,
                output_shape=list(synthetic_data.shape),
            )
            return synthetic_data

        except tf.errors.ResourceExhaustedError as exc:
            self._logger.error(
                "vae_generation_oom",
                error=str(exc),
                num_samples=num_samples,
            )
            raise RuntimeError(
                f"Out of memory generating {num_samples} samples.  Try generating in smaller batches.  Original: {exc}",
            ) from exc
        except Exception as exc:
            self._logger.error(
                "vae_generation_failed",
                error=str(exc),
                error_type=type(exc).__name__,
            )
            raise

    def encode(self, data: np.ndarray) -> tuple[np.ndarray, np.ndarray]:
        """Encode data into the latent space.

        Args:
            data: Input array — can be raw (will **not** be preprocessed
                by this method) or already preprocessed.

        Returns:
            Tuple ``(z_mean, z_log_var)`` each of shape
            ``(n_samples, latent_dim)`` as NumPy arrays.

        Raises:
            RuntimeError: If the encoder has not been built.
        """
        if self.encoder is None:
            raise RuntimeError("Encoder not built — train the model first.")

        z_mean, z_log_var, _ = self.encoder.predict(
            data.astype(np.float32),
            batch_size=self.config.batch_size,
        )
        return z_mean, z_log_var

    # ------------------------------------------------------------------
    # Persistence
    # ------------------------------------------------------------------

    def save_model(self, path: str) -> None:
        """Persist the trained VAE to disk.

        Creates three artefacts inside *path*:

        * ``encoder/`` — TensorFlow SavedModel for the encoder.
        * ``decoder/`` — TensorFlow SavedModel for the decoder.
        * ``vae_config.json`` — JSON sidecar with :class:`VAEConfig`
          hyper-parameters, normalisation state, and category maps.

        Args:
            path: Directory into which model artefacts are written.
                Created automatically if it does not exist.

        Raises:
            RuntimeError: If the model has not been built.
            OSError: If the output directory cannot be created.
        """
        if self.encoder is None or self.decoder is None:
            raise RuntimeError("Model must be built before saving")

        os.makedirs(path, exist_ok=True)

        # Use .keras extension required by Keras 3.x native format
        encoder_path = os.path.join(path, "encoder")
        decoder_path = os.path.join(path, "decoder")
        encoder_save_path = os.path.join(path, "encoder.keras")
        decoder_save_path = os.path.join(path, "decoder.keras")
        config_path = os.path.join(path, "vae_config.json")

        try:
            self.encoder.save(encoder_save_path)
            self.decoder.save(decoder_save_path)

            sidecar: dict[str, Any] = {
                "config": {
                    "latent_dim": self.config.latent_dim,
                    "encoder_hidden_dims": self.config.encoder_hidden_dims,
                    "decoder_hidden_dims": self.config.decoder_hidden_dims,
                    "learning_rate": self.config.learning_rate,
                    "batch_size": self.config.batch_size,
                    "num_epochs": self.config.num_epochs,
                    "kl_weight": self.config.kl_weight,
                    "kl_annealing": self.config.kl_annealing,
                    "kl_annealing_epochs": self.config.kl_annealing_epochs,
                    "dropout_rate": self.config.dropout_rate,
                    "input_dim": self.config.input_dim,
                    "numerical_columns": self.config.numerical_columns,
                    "categorical_columns": self.config.categorical_columns,
                    "activation": self.config.activation,
                    "output_activation": self.config.output_activation,
                },
                "normalization": {
                    "num_mins": (self._num_mins.tolist() if self._num_mins is not None else None),
                    "num_maxs": (self._num_maxs.tolist() if self._num_maxs is not None else None),
                    "num_ranges": (self._num_ranges.tolist() if self._num_ranges is not None else None),
                },
                "category_maps": {
                    str(k): {str(ki): str(vi) for ki, vi in v.items()} for k, v in self._category_maps.items()
                },
                "output_mappings": {
                    "numerical_output_indices": self._numerical_output_indices,
                    "categorical_output_slices": [list(s) for s in self._categorical_output_slices],
                    "preprocessed_dim": self._preprocessed_dim,
                },
                "training_state": {
                    "is_trained": self._is_trained,
                    "final_epoch": self._final_epoch,
                    "original_num_columns": self._original_num_columns,
                },
            }

            with open(config_path, "w", encoding="utf-8") as fh:
                json.dump(sidecar, fh, indent=2)

            self._logger.info(
                "vae_model_saved",
                path=path,
                encoder_path=encoder_path,
                decoder_path=decoder_path,
                config_json=json.dumps(
                    {"latent_dim": self.config.latent_dim, "input_dim": self.config.input_dim},
                ),
            )

        except Exception as exc:
            self._logger.error(
                "vae_model_save_failed",
                path=path,
                error=str(exc),
                error_type=type(exc).__name__,
            )
            raise

    @staticmethod
    def _resolve_model_paths(
        path: str,
    ) -> tuple[str, str, str]:
        """Resolve encoder, decoder, and config paths for a saved model.

        Supports both ``.keras`` single-file format and legacy
        directory-based format.

        Args:
            path: Root directory containing saved artefacts.

        Returns:
            Tuple ``(encoder_path, decoder_path, config_path)``.

        Raises:
            FileNotFoundError: If any required artefact is missing.
        """
        if not os.path.exists(path):
            raise FileNotFoundError(f"Model directory not found: {path}")

        config_path = os.path.join(path, "vae_config.json")
        if not os.path.exists(config_path):
            raise FileNotFoundError(f"Config sidecar not found: {config_path}")

        encoder_keras = os.path.join(path, "encoder.keras")
        encoder_path = encoder_keras if os.path.exists(encoder_keras) else os.path.join(path, "encoder")
        if not os.path.exists(encoder_path):
            raise FileNotFoundError(f"Encoder artefact not found: {encoder_path}")

        decoder_keras = os.path.join(path, "decoder.keras")
        decoder_path = decoder_keras if os.path.exists(decoder_keras) else os.path.join(path, "decoder")
        if not os.path.exists(decoder_path):
            raise FileNotFoundError(f"Decoder artefact not found: {decoder_path}")

        return encoder_path, decoder_path, config_path

    def _restore_sidecar_state(self, sidecar: dict[str, Any]) -> None:
        """Restore normalisation, category maps, output mappings, and training state.

        Called from :meth:`load_model` after the config and Keras
        sub-models have been loaded.

        Args:
            sidecar: Parsed JSON sidecar dictionary.
        """
        # Normalisation parameters
        norm = sidecar.get("normalization", {})
        if norm.get("num_mins") is not None:
            self._num_mins = np.array(norm["num_mins"], dtype=np.float64)
        if norm.get("num_maxs") is not None:
            self._num_maxs = np.array(norm["num_maxs"], dtype=np.float64)
        if norm.get("num_ranges") is not None:
            self._num_ranges = np.array(norm["num_ranges"], dtype=np.float64)

        # Category maps
        raw_maps = sidecar.get("category_maps", {})
        self._category_maps = {int(k): {int(ki): vi for ki, vi in v.items()} for k, v in raw_maps.items()}

        # Output mappings
        mappings = sidecar.get("output_mappings", {})
        self._numerical_output_indices = mappings.get(
            "numerical_output_indices",
            [],
        )
        self._categorical_output_slices = [tuple(s) for s in mappings.get("categorical_output_slices", [])]
        self._preprocessed_dim = mappings.get(
            "preprocessed_dim",
            self.config.input_dim,
        )

        # Training state
        ts = sidecar.get("training_state", {})
        self._is_trained = ts.get("is_trained", True)
        self._final_epoch = ts.get("final_epoch", 0)
        self._original_num_columns = ts.get("original_num_columns", 0)

    @classmethod
    def load_model(cls, path: str) -> TabularVAE:
        """Load a previously saved VAE from disk.

        Reconstructs the full :class:`TabularVAE` instance -- including
        encoder, decoder, config, normalisation state, and category maps
        -- from the artefacts created by :meth:`save_model`.

        Args:
            path: Directory containing saved model artefacts.

        Returns:
            A fully initialised :class:`TabularVAE` ready for
            :meth:`generate` or further training.

        Raises:
            FileNotFoundError: If the directory or required files are
                missing.
            ValueError: If the JSON sidecar contains invalid data.
        """
        load_logger = get_logger(__name__)
        encoder_path, decoder_path, config_path = cls._resolve_model_paths(path)

        try:
            with open(config_path, encoding="utf-8") as fh:
                sidecar = json.load(fh)

            raw_cfg = sidecar["config"]
            config = VAEConfig(
                latent_dim=raw_cfg["latent_dim"],
                encoder_hidden_dims=raw_cfg["encoder_hidden_dims"],
                decoder_hidden_dims=raw_cfg["decoder_hidden_dims"],
                learning_rate=raw_cfg["learning_rate"],
                batch_size=raw_cfg["batch_size"],
                num_epochs=raw_cfg["num_epochs"],
                kl_weight=raw_cfg["kl_weight"],
                kl_annealing=raw_cfg["kl_annealing"],
                kl_annealing_epochs=raw_cfg["kl_annealing_epochs"],
                dropout_rate=raw_cfg["dropout_rate"],
                input_dim=raw_cfg["input_dim"],
                numerical_columns=raw_cfg["numerical_columns"],
                categorical_columns=raw_cfg["categorical_columns"],
                activation=raw_cfg["activation"],
                output_activation=raw_cfg["output_activation"],
            )

            instance = cls(config)

            custom_objects = {"Sampling": Sampling}
            instance.encoder = tf.keras.models.load_model(
                encoder_path,
                custom_objects=custom_objects,
            )
            instance.decoder = tf.keras.models.load_model(decoder_path)

            instance._restore_sidecar_state(sidecar)

            load_logger.info(
                "vae_model_loaded",
                path=path,
                latent_dim=config.latent_dim,
                is_trained=instance._is_trained,
            )
            return instance

        except json.JSONDecodeError as exc:
            load_logger.error(
                "vae_model_load_json_error",
                path=config_path,
                error=str(exc),
            )
            raise ValueError(
                f"Invalid JSON in config sidecar: {exc}",
            ) from exc
        except KeyError as exc:
            load_logger.error(
                "vae_model_load_missing_key",
                path=config_path,
                missing_key=str(exc),
            )
            raise ValueError(
                f"Missing required key in config sidecar: {exc}",
            ) from exc
        except Exception as exc:
            load_logger.error(
                "vae_model_load_failed",
                path=path,
                error=str(exc),
                error_type=type(exc).__name__,
            )
            raise

    # ------------------------------------------------------------------
    # Introspection
    # ------------------------------------------------------------------

    def get_training_summary(self) -> dict[str, Any]:
        """Return a concise summary of model architecture and training outcome.

        Returns:
            Dictionary with keys:

            * ``latent_dim`` — latent space dimensionality
            * ``input_dim`` — config input_dim value
            * ``preprocessed_dim`` — actual preprocessed feature count
            * ``final_epoch`` — number of epochs completed
            * ``is_trained`` — training completion flag
            * ``final_total_loss`` — last-epoch total ELBO loss
            * ``final_reconstruction_loss`` — last-epoch reconstruction loss
            * ``final_kl_loss`` — last-epoch KL divergence loss
            * ``kl_weight`` — current KL weight
            * ``config`` — full hyper-parameter snapshot
        """
        summary: dict[str, Any] = {
            "latent_dim": self.config.latent_dim,
            "input_dim": self.config.input_dim,
            "preprocessed_dim": self._preprocessed_dim,
            "final_epoch": self._final_epoch,
            "is_trained": self._is_trained,
            "final_total_loss": None,
            "final_reconstruction_loss": None,
            "final_kl_loss": None,
            "kl_weight": self._current_kl_weight,
            "config": {
                "latent_dim": self.config.latent_dim,
                "encoder_hidden_dims": self.config.encoder_hidden_dims,
                "decoder_hidden_dims": self.config.decoder_hidden_dims,
                "learning_rate": self.config.learning_rate,
                "batch_size": self.config.batch_size,
                "num_epochs": self.config.num_epochs,
                "kl_weight": self.config.kl_weight,
                "kl_annealing": self.config.kl_annealing,
                "kl_annealing_epochs": self.config.kl_annealing_epochs,
                "dropout_rate": self.config.dropout_rate,
                "activation": self.config.activation,
                "output_activation": self.config.output_activation,
            },
        }

        if self._training_history:
            total_vals = self._training_history.get("total_loss", [])
            recon_vals = self._training_history.get("reconstruction_loss", [])
            kl_vals = self._training_history.get("kl_loss", [])
            if total_vals:
                summary["final_total_loss"] = round(total_vals[-1], 6)
            if recon_vals:
                summary["final_reconstruction_loss"] = round(recon_vals[-1], 6)
            if kl_vals:
                summary["final_kl_loss"] = round(kl_vals[-1], 6)

        return summary
