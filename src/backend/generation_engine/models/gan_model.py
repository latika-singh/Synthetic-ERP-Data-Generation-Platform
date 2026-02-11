"""GAN architecture for tabular synthetic data generation.

Implements a Wasserstein GAN with Gradient Penalty (WGAN-GP) using PyTorch
for generating high-fidelity synthetic records that match the statistical
distributions of source ERP data.  The architecture includes:

* **Generator** — maps random noise vectors to synthetic records with
  mixed numerical / categorical columns, using residual connections
  between hidden layers and per-column activation functions (Tanh for
  numerical columns, Gumbel-Softmax for categorical columns).
* **Discriminator** — a critic network that scores records as real or
  synthetic using spectral normalisation for training stability.
* **TabularGAN** — orchestrates adversarial training with optional
  mixed-precision support (``torch.cuda.amp``), early stopping,
  batch generation, and model persistence.

Usage example::

    from generation_engine.models.gan_model import TabularGAN, GANConfig
    import numpy as np

    cfg = GANConfig(
        latent_dim=128,
        num_epochs=300,
        numerical_columns=[0, 1, 2],
        categorical_columns=[{"index": 3, "num_categories": 5}],
    )
    gan = TabularGAN(cfg)
    history = gan.train(real_data=np.random.randn(1000, 4))
    synthetic = gan.generate(num_samples=500)
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Tuple

import numpy as np
import torch
import torch.nn as nn
import torch.optim as optim
from torch.utils.data import DataLoader, TensorDataset

from shared.logging.structured_logger import get_logger

# ---------------------------------------------------------------------------
# Module-level logger — used for events that occur outside class instances
# (e.g. module import diagnostics).
# ---------------------------------------------------------------------------
logger = get_logger(__name__)


# ============================================================================
# Configuration
# ============================================================================


@dataclass
class GANConfig:
    """Hyperparameter configuration for the tabular GAN.

    All mutable collection defaults use ``field(default_factory=...)`` to
    avoid the shared-mutable-default pitfall.

    Attributes:
        latent_dim: Dimension of the noise vector *z*.
        generator_hidden_dims: Hidden-layer widths for the Generator.
        discriminator_hidden_dims: Hidden-layer widths for the Discriminator.
        learning_rate_g: Generator Adam learning rate.
        learning_rate_d: Discriminator Adam learning rate.
        beta1: Adam beta-1 parameter.
        beta2: Adam beta-2 parameter.
        batch_size: Training mini-batch size.
        num_epochs: Total training epochs.
        n_critic: Discriminator steps per generator step (WGAN-GP).
        gradient_penalty_lambda: Gradient penalty coefficient.
        dropout_rate: Dropout probability in the Discriminator.
        output_dim: Number of output features (computed from data schema).
        numerical_columns: Indices of numerical columns in the raw data.
        categorical_columns: Metadata for categorical columns — each dict
            must contain ``"index"`` (column index) and ``"num_categories"``
            (number of distinct categories).
        device: PyTorch device string (``'cpu'`` or ``'cuda:0'``).
    """

    latent_dim: int = 128
    generator_hidden_dims: list[int] = field(
        default_factory=lambda: [256, 512, 256],
    )
    discriminator_hidden_dims: list[int] = field(
        default_factory=lambda: [256, 512, 256],
    )
    learning_rate_g: float = 0.0002
    learning_rate_d: float = 0.0002
    beta1: float = 0.5
    beta2: float = 0.999
    batch_size: int = 256
    num_epochs: int = 300
    n_critic: int = 5
    gradient_penalty_lambda: float = 10.0
    dropout_rate: float = 0.3
    output_dim: int = 0
    numerical_columns: list[int] = field(default_factory=list)
    categorical_columns: list[dict] = field(default_factory=list)
    device: str = "cpu"


# ============================================================================
# Internal building block — Residual Block
# ============================================================================


class _ResidualBlock(nn.Module):
    """Fully-connected residual block with BatchNorm and LeakyReLU.

    When ``in_dim == out_dim`` a direct skip (identity) connection is used.
    When dimensions differ a learnable linear projection adapts the residual
    path so that the addition ``out + identity`` is dimensionally valid.

    Args:
        in_dim: Input feature dimension.
        out_dim: Output feature dimension.
    """

    def __init__(self, in_dim: int, out_dim: int) -> None:
        super().__init__()
        self.fc = nn.Linear(in_dim, out_dim)
        self.bn = nn.BatchNorm1d(out_dim)
        self.act = nn.LeakyReLU(0.2, inplace=True)
        # Learnable projection shortcut when dimensions differ
        self.shortcut: Optional[nn.Linear] = (
            nn.Linear(in_dim, out_dim) if in_dim != out_dim else None
        )

    def forward(self, x: torch.Tensor) -> torch.Tensor:
        """Apply linear → BN → LeakyReLU, then add the residual.

        Args:
            x: Input tensor of shape ``(batch, in_dim)``.

        Returns:
            Output tensor of shape ``(batch, out_dim)``.
        """
        identity = x if self.shortcut is None else self.shortcut(x)
        out = self.act(self.bn(self.fc(x)))
        return out + identity


# ============================================================================
# Generator
# ============================================================================


class Generator(nn.Module):
    """Fully-connected generator mapping noise → synthetic record.

    Architecture uses an input projection layer followed by residual
    hidden blocks for improved gradient flow.  The final output is split
    per column type and receives per-column activations:

    * **Tanh** for normalised numerical columns (rescaled from [-1, 1]
      to [0, 1] so the Generator output aligns with min-max normalised
      training data).
    * **Gumbel-Softmax** for one-hot categorical column slices, enabling
      differentiable categorical sampling during training.
    * **Sigmoid** as fallback for any unclassified output positions.

    Args:
        config: A :class:`GANConfig` instance with architecture settings.
    """

    def __init__(self, config: GANConfig) -> None:
        super().__init__()
        self.config = config
        self._numerical_indices: List[int] = []
        self._categorical_slices: List[Tuple[int, int]] = []

        hidden_dims: List[int] = config.generator_hidden_dims

        # Input projection: latent_dim → first hidden dim
        self.input_proj = nn.Sequential(
            nn.Linear(config.latent_dim, hidden_dims[0]),
            nn.BatchNorm1d(hidden_dims[0]),
            nn.LeakyReLU(0.2, inplace=True),
        )

        # Residual hidden blocks for improved gradient flow
        res_blocks: List[nn.Module] = []
        for i in range(len(hidden_dims) - 1):
            res_blocks.append(_ResidualBlock(hidden_dims[i], hidden_dims[i + 1]))
        self.res_layers: nn.Module = (
            nn.Sequential(*res_blocks) if res_blocks else nn.Identity()
        )

        # Output projection: last hidden dim → raw logits per column
        self.output_proj = nn.Linear(hidden_dims[-1], config.output_dim)

        # Activation modules (instantiated once, reused in forward)
        self._tanh = nn.Tanh()
        self._sigmoid = nn.Sigmoid()

        # He (Kaiming) initialisation for all Linear layers
        self._init_weights()

    # ---- weight initialisation -----------------------------------------------

    def _init_weights(self) -> None:
        """Apply Kaiming He initialisation to all :class:`nn.Linear` layers.

        Uses ``nonlinearity='leaky_relu'`` to account for the negative
        slope used throughout the Generator.
        """
        for module in self.modules():
            if isinstance(module, nn.Linear):
                nn.init.kaiming_normal_(module.weight, nonlinearity="leaky_relu")
                if module.bias is not None:
                    nn.init.zeros_(module.bias)

    # ---- forward pass --------------------------------------------------------

    def forward(self, z: torch.Tensor) -> torch.Tensor:
        """Forward pass producing a batch of synthetic records.

        Routes through the input projection, residual blocks, and output
        projection, then applies per-column activations:

        * **Numerical indices** → ``Tanh`` rescaled to [0, 1].
        * **Categorical slices** → ``Gumbel-Softmax`` (τ = 0.5).
        * **Other / gap positions** → ``Sigmoid``.

        Args:
            z: Noise tensor of shape ``(batch, latent_dim)``.

        Returns:
            Generated record batch of shape ``(batch, output_dim)``.
        """
        h = self.input_proj(z)
        h = self.res_layers(h)
        raw = self.output_proj(h)

        # Fast path when no column metadata is available
        if not self._numerical_indices and not self._categorical_slices:
            return self._sigmoid(raw)

        # Build a sorted list of (start, end, kind) column segments
        segments: List[Tuple[int, int, str]] = []
        for idx in self._numerical_indices:
            segments.append((idx, idx + 1, "numerical"))
        for start, end in self._categorical_slices:
            segments.append((start, end, "categorical"))
        segments.sort(key=lambda s: s[0])

        parts: List[torch.Tensor] = []
        last = 0
        for start, end, kind in segments:
            # Fill any gap between segments with sigmoid
            if start > last:
                parts.append(self._sigmoid(raw[:, last:start]))
            if kind == "numerical":
                # Tanh → rescale from [-1, 1] to [0, 1]
                parts.append((self._tanh(raw[:, start:end]) + 1.0) / 2.0)
            else:
                parts.append(
                    nn.functional.gumbel_softmax(
                        raw[:, start:end],
                        tau=0.5,
                        hard=False,
                    ),
                )
            last = end

        # Handle trailing columns beyond the last segment
        if last < raw.shape[1]:
            parts.append(self._sigmoid(raw[:, last:]))

        return torch.cat(parts, dim=1)

    # ---- convenience generation ----------------------------------------------

    def generate(
        self,
        num_samples: int,
        device: Optional[str] = None,
    ) -> torch.Tensor:
        """Generate samples in eval mode without gradient tracking.

        Creates a random noise vector z ∼ N(0, 1) of shape
        ``(num_samples, latent_dim)``, runs the forward pass in evaluation
        mode with :func:`torch.no_grad`, and returns results on CPU.

        Args:
            num_samples: Number of records to produce.
            device: Override device for the noise tensor.  When ``None``
                the device from ``self.config`` is used.

        Returns:
            Tensor of generated records on CPU.
        """
        target_device = device or self.config.device
        self.eval()
        with torch.no_grad():
            z = torch.randn(num_samples, self.config.latent_dim, device=target_device)
            samples = self.forward(z)
        self.train()
        return samples.cpu()


# ============================================================================
# Discriminator (Critic)
# ============================================================================


class Discriminator(nn.Module):
    """WGAN-GP critic network scoring records as real / synthetic.

    No sigmoid at output — raw Wasserstein scores are returned.  Spectral
    normalisation (:func:`nn.utils.spectral_norm`) is applied to every
    :class:`nn.Linear` layer for training stability.

    Args:
        config: A :class:`GANConfig` instance with architecture settings.
    """

    def __init__(self, config: GANConfig) -> None:
        super().__init__()
        self.config = config

        layers: List[nn.Module] = []
        in_dim = config.output_dim
        for h_dim in config.discriminator_hidden_dims:
            layers.append(nn.utils.spectral_norm(nn.Linear(in_dim, h_dim)))
            layers.append(nn.LeakyReLU(0.2, inplace=True))
            layers.append(nn.Dropout(config.dropout_rate))
            in_dim = h_dim
        # Final score layer — single scalar per sample, no activation
        layers.append(nn.utils.spectral_norm(nn.Linear(in_dim, 1)))
        self.net = nn.Sequential(*layers)

    def forward(self, x: torch.Tensor) -> torch.Tensor:
        """Return the Wasserstein critic score for each record.

        Args:
            x: Data tensor of shape ``(batch, output_dim)``.

        Returns:
            Score tensor of shape ``(batch, 1)``.
        """
        return self.net(x)


# ============================================================================
# TabularGAN — main public interface
# ============================================================================


class TabularGAN:
    """High-level interface for training and generating tabular data with a GAN.

    Orchestrates the adversarial training loop using WGAN-GP, manages data
    normalisation / one-hot encoding, and provides persistence methods.
    When running on a CUDA device, mixed-precision training via
    ``torch.cuda.amp`` is automatically enabled for faster throughput and
    reduced GPU memory usage.

    Args:
        config: Hyperparameter configuration.  If the requested device is
            ``"cuda"`` but no GPU is available, the device is silently
            downgraded to ``"cpu"``.
    """

    def __init__(self, config: GANConfig) -> None:
        if config is None:
            raise ValueError("config must not be None")

        self.config = config

        # Resolve device — validate CUDA availability
        if "cuda" in config.device and not torch.cuda.is_available():
            logger.warning(
                "cuda_not_available_fallback_cpu",
                requested_device=config.device,
                fallback="cpu",
            )
            config.device = "cpu"
        self._device: torch.device = torch.device(config.device)

        # Network placeholders (built during train() or load())
        self.generator: Optional[Generator] = None
        self.discriminator: Optional[Discriminator] = None
        self.opt_g: Optional[optim.Adam] = None
        self.opt_d: Optional[optim.Adam] = None

        # Mixed-precision training support — enabled on CUDA devices
        self._use_amp: bool = self._device.type == "cuda"
        self._scaler: Optional[torch.cuda.amp.GradScaler] = (
            torch.cuda.amp.GradScaler() if self._use_amp else None
        )

        # Normalisation / encoding state (populated in _preprocess_data)
        self._num_mins: Optional[np.ndarray] = None
        self._num_maxs: Optional[np.ndarray] = None
        self._num_ranges: Optional[np.ndarray] = None
        self._category_maps: Dict[int, Dict[int, Any]] = {}
        self._preprocessed_dim: int = 0
        self._original_num_columns: int = 0
        self._numerical_output_indices: List[int] = []
        self._categorical_output_slices: List[Tuple[int, int]] = []

        # Training state tracking
        self._is_trained: bool = False
        self._final_epoch: int = 0
        self._g_losses: List[float] = []
        self._d_losses: List[float] = []

        self._logger = get_logger(__name__)
        self._logger.info(
            "tabular_gan_initialized",
            latent_dim=config.latent_dim,
            device=config.device,
            mixed_precision=self._use_amp,
            num_numerical=len(config.numerical_columns),
            num_categorical=len(config.categorical_columns),
        )

    # ------------------------------------------------------------------
    # Data pre/post-processing
    # ------------------------------------------------------------------

    def _preprocess_data(self, data: np.ndarray) -> np.ndarray:
        """Normalise numerical columns and one-hot encode categoricals.

        Numerical columns are min-max scaled to [0, 1].  Categorical
        columns are converted to one-hot vectors.  The mapping state is
        stored on ``self`` for later reversal in :meth:`_postprocess_data`.

        Args:
            data: Raw data array of shape ``(N, num_columns)``.

        Returns:
            Preprocessed ``float32`` array ready for training.

        Raises:
            ValueError: If *data* has fewer than 1 row or column.
        """
        if data.ndim != 2 or data.shape[0] < 1 or data.shape[1] < 1:
            raise ValueError(
                f"data must be a 2-D array with ≥1 row and ≥1 column, "
                f"got shape {data.shape}"
            )

        self._original_num_columns = data.shape[1]
        parts: List[np.ndarray] = []
        self._numerical_output_indices = []
        self._categorical_output_slices = []
        offset = 0

        num_cols = self.config.numerical_columns
        cat_cols = self.config.categorical_columns

        # When no column metadata is provided, treat every column as numerical
        if not num_cols and not cat_cols:
            num_cols = list(range(data.shape[1]))

        # --- Numerical columns ------------------------------------------------
        if num_cols:
            num_data = data[:, num_cols].astype(np.float64)
            self._num_mins = np.min(num_data, axis=0)
            self._num_maxs = np.max(num_data, axis=0)
            self._num_ranges = self._num_maxs - self._num_mins
            # Prevent division by zero for constant columns
            self._num_ranges[self._num_ranges == 0] = 1.0
            normalised = ((num_data - self._num_mins) / self._num_ranges).astype(
                np.float32,
            )
            parts.append(normalised)
            for i in range(len(num_cols)):
                self._numerical_output_indices.append(offset + i)
            offset += len(num_cols)

        # --- Categorical columns ----------------------------------------------
        for cat_meta in cat_cols:
            col_idx: int = cat_meta["index"]
            n_cats: int = cat_meta["num_categories"]
            col_data = data[:, col_idx].astype(int)

            unique_vals = sorted(set(col_data.tolist()))
            cat_map: Dict[int, Any] = {
                v: idx for idx, v in enumerate(unique_vals)
            }
            self._category_maps[col_idx] = cat_map

            actual_cats = max(n_cats, len(unique_vals))
            one_hot = np.zeros(
                (data.shape[0], actual_cats), dtype=np.float32,
            )
            for row_i, val in enumerate(col_data):
                mapped = cat_map.get(int(val), 0)
                one_hot[row_i, mapped] = 1.0

            self._categorical_output_slices.append((offset, offset + actual_cats))
            parts.append(one_hot)
            offset += actual_cats

        if not parts:
            return data.astype(np.float32)

        preprocessed = np.concatenate(parts, axis=1)
        self._preprocessed_dim = preprocessed.shape[1]
        return preprocessed

    def _postprocess_data(self, generated: np.ndarray) -> np.ndarray:
        """Denormalise numerical columns and decode categoricals.

        Reverses the transformations applied by :meth:`_preprocess_data`
        so the output is in the original data space.

        Args:
            generated: Generated ``float`` array from the Generator.

        Returns:
            Post-processed array matching the original column layout.
        """
        num_cols = self.config.numerical_columns
        cat_cols = self.config.categorical_columns
        if not num_cols and not cat_cols:
            num_cols = list(range(self._original_num_columns))

        n_samples = generated.shape[0]
        result = np.zeros(
            (n_samples, self._original_num_columns), dtype=np.float64,
        )

        # --- Numerical columns ------------------------------------------------
        if num_cols and self._numerical_output_indices:
            num_raw = generated[:, self._numerical_output_indices]
            num_raw = np.clip(num_raw, 0.0, 1.0)
            if self._num_mins is not None and self._num_ranges is not None:
                denormed = num_raw * self._num_ranges + self._num_mins
            else:
                denormed = num_raw
            for out_i, orig_i in enumerate(num_cols):
                if orig_i < self._original_num_columns:
                    result[:, orig_i] = denormed[:, out_i]

        # --- Categorical columns ----------------------------------------------
        for cat_meta, (start, end) in zip(
            cat_cols, self._categorical_output_slices,
        ):
            col_idx: int = cat_meta["index"]
            cat_probs = generated[:, start:end]
            cat_indices = np.argmax(cat_probs, axis=1)
            inv_map = {
                v: k for k, v in self._category_maps.get(col_idx, {}).items()
            }
            for row_i, idx in enumerate(cat_indices):
                result[row_i, col_idx] = inv_map.get(int(idx), 0)

        return result

    # ------------------------------------------------------------------
    # Gradient penalty (WGAN-GP)
    # ------------------------------------------------------------------

    def _compute_gradient_penalty(
        self,
        real: torch.Tensor,
        fake: torch.Tensor,
    ) -> torch.Tensor:
        """Compute the gradient penalty term for WGAN-GP.

        Samples random interpolations between *real* and *fake*, computes
        the Discriminator output on the interpolations, and penalises the
        gradient norm when it deviates from 1.

        Gradient penalty is always computed in full precision (fp32) even
        when mixed-precision training is active, because
        ``torch.autograd.grad`` requires fp32 for numerical stability.

        Args:
            real: Real data batch of shape ``(B, D)``.
            fake: Generated data batch of shape ``(B, D)``.

        Returns:
            Scalar gradient penalty tensor:
            ``λ · E[(‖∇D(x̃)‖₂ − 1)²]``.
        """
        alpha = torch.rand(real.size(0), 1, device=self._device)
        interpolated = (alpha * real + (1 - alpha) * fake).requires_grad_(True)
        d_interp = self.discriminator(interpolated)  # type: ignore[misc]

        gradients = torch.autograd.grad(
            outputs=d_interp,
            inputs=interpolated,
            grad_outputs=torch.ones_like(d_interp),
            create_graph=True,
            retain_graph=True,
        )[0]
        gradients = gradients.view(gradients.size(0), -1)
        gradient_penalty = ((gradients.norm(2, dim=1) - 1) ** 2).mean()
        return self.config.gradient_penalty_lambda * gradient_penalty

    # ------------------------------------------------------------------
    # Training
    # ------------------------------------------------------------------

    def train(
        self,
        real_data: np.ndarray,
        callbacks: Optional[List[Any]] = None,
    ) -> Dict[str, Any]:
        """Train the GAN on *real_data* using WGAN-GP.

        Supports mixed-precision training via ``torch.cuda.amp`` when
        running on a CUDA device.  Implements early stopping when the
        generator loss converges (change < 1 × 10⁻⁵ over 20 consecutive
        epochs).

        Args:
            real_data: Array of shape ``(N, features)`` containing the
                real data to learn from.
            callbacks: Optional list of callables invoked after every
                epoch with signature ``(epoch: int, g_loss: float,
                d_loss: float)``.  Useful for progress tracking and
                checkpointing.

        Returns:
            Training history dictionary with keys ``"g_losses"``,
            ``"d_losses"``, and ``"final_epoch"``.

        Raises:
            ValueError: If *real_data* is ``None`` or empty.
            RuntimeError: On unrecoverable CUDA / OOM errors (after
                attempting to free GPU memory).
        """
        if real_data is None or (
            hasattr(real_data, "size") and real_data.size == 0
        ):
            raise ValueError("real_data must be a non-empty numpy array")

        self._logger.info(
            "gan_training_started",
            num_samples=real_data.shape[0],
            num_raw_columns=real_data.shape[1],
            latent_dim=self.config.latent_dim,
            num_epochs=self.config.num_epochs,
            batch_size=self.config.batch_size,
            device=self.config.device,
            mixed_precision=self._use_amp,
        )

        try:
            # --- Preprocessing ------------------------------------------------
            preprocessed = self._preprocess_data(real_data)
            self.config.output_dim = preprocessed.shape[1]

            self._logger.info(
                "gan_data_preprocessed",
                preprocessed_dim=self.config.output_dim,
                numerical_indices_count=len(self._numerical_output_indices),
                categorical_slices_count=len(self._categorical_output_slices),
            )

            # --- Build networks -----------------------------------------------
            self.generator = Generator(self.config).to(self._device)
            self.discriminator = Discriminator(self.config).to(self._device)

            # Wire column-type metadata into the Generator for per-column
            # activations in the forward pass
            self.generator._numerical_indices = self._numerical_output_indices
            self.generator._categorical_slices = self._categorical_output_slices

            # --- Optimisers ---------------------------------------------------
            self.opt_g = optim.Adam(
                self.generator.parameters(),
                lr=self.config.learning_rate_g,
                betas=(self.config.beta1, self.config.beta2),
            )
            self.opt_d = optim.Adam(
                self.discriminator.parameters(),
                lr=self.config.learning_rate_d,
                betas=(self.config.beta1, self.config.beta2),
            )

            # --- DataLoader ---------------------------------------------------
            dataset = TensorDataset(
                torch.tensor(preprocessed, dtype=torch.float32),
            )
            loader = DataLoader(
                dataset,
                batch_size=self.config.batch_size,
                shuffle=True,
                drop_last=True,
            )

            # --- Early-stopping state -----------------------------------------
            early_stop_patience: int = 20
            early_stop_min_delta: float = 1e-5
            patience_counter: int = 0
            best_g_loss: Optional[float] = None

            # --- Training loop ------------------------------------------------
            self._g_losses = []
            self._d_losses = []
            final_epoch: int = 0

            for epoch in range(self.config.num_epochs):
                epoch_g_loss = 0.0
                epoch_d_loss = 0.0
                n_batches = 0

                for (real_batch,) in loader:
                    real_batch = real_batch.to(self._device)
                    bs = real_batch.size(0)

                    # ---- Discriminator training (n_critic steps) -------------
                    for _ in range(self.config.n_critic):
                        z = torch.randn(
                            bs, self.config.latent_dim, device=self._device,
                        )

                        if self._use_amp and self._scaler is not None:
                            with torch.cuda.amp.autocast():
                                fake = self.generator(z).detach()
                                d_real = self.discriminator(real_batch).mean()
                                d_fake = self.discriminator(fake).mean()
                            # Gradient penalty must run in fp32
                            gp = self._compute_gradient_penalty(real_batch, fake)
                            d_loss = d_fake - d_real + gp

                            self.opt_d.zero_grad()
                            self._scaler.scale(d_loss).backward()
                            self._scaler.step(self.opt_d)
                            self._scaler.update()
                        else:
                            fake = self.generator(z).detach()
                            d_real = self.discriminator(real_batch).mean()
                            d_fake = self.discriminator(fake).mean()
                            gp = self._compute_gradient_penalty(real_batch, fake)
                            d_loss = d_fake - d_real + gp

                            self.opt_d.zero_grad()
                            d_loss.backward()
                            self.opt_d.step()

                    # ---- Generator training ----------------------------------
                    z = torch.randn(
                        bs, self.config.latent_dim, device=self._device,
                    )

                    if self._use_amp and self._scaler is not None:
                        with torch.cuda.amp.autocast():
                            fake = self.generator(z)
                            g_loss = -self.discriminator(fake).mean()

                        self.opt_g.zero_grad()
                        self._scaler.scale(g_loss).backward()
                        self._scaler.step(self.opt_g)
                        self._scaler.update()
                    else:
                        fake = self.generator(z)
                        g_loss = -self.discriminator(fake).mean()

                        self.opt_g.zero_grad()
                        g_loss.backward()
                        self.opt_g.step()

                    epoch_g_loss += g_loss.item()
                    epoch_d_loss += d_loss.item()
                    n_batches += 1

                avg_g = epoch_g_loss / max(n_batches, 1)
                avg_d = epoch_d_loss / max(n_batches, 1)
                self._g_losses.append(avg_g)
                self._d_losses.append(avg_d)
                final_epoch = epoch + 1

                # Periodic logging (every 10 epochs and final epoch)
                if epoch % 10 == 0 or epoch == self.config.num_epochs - 1:
                    wasserstein_est = abs(avg_d - gp.item()) if isinstance(
                        gp, torch.Tensor,
                    ) else abs(avg_d)
                    self._logger.debug(
                        "gan_epoch_completed",
                        epoch=epoch,
                        g_loss=round(avg_g, 6),
                        d_loss=round(avg_d, 6),
                        gradient_penalty=round(
                            gp.item() if isinstance(gp, torch.Tensor) else 0.0,
                            6,
                        ),
                        wasserstein_distance=round(wasserstein_est, 6),
                    )

                # Execute callbacks
                if callbacks:
                    for cb in callbacks:
                        cb(epoch, avg_g, avg_d)

                # Early-stopping check — convergence of generator loss
                if best_g_loss is None:
                    best_g_loss = avg_g
                elif abs(best_g_loss - avg_g) < early_stop_min_delta:
                    patience_counter += 1
                    if patience_counter >= early_stop_patience:
                        self._logger.info(
                            "gan_early_stopping_triggered",
                            epoch=epoch,
                            best_g_loss=round(best_g_loss, 6),
                            current_g_loss=round(avg_g, 6),
                            patience=early_stop_patience,
                        )
                        break
                else:
                    best_g_loss = avg_g
                    patience_counter = 0

            # --- Post-training bookkeeping ------------------------------------
            self._is_trained = True
            self._final_epoch = final_epoch
            self._logger.info(
                "gan_training_completed",
                epochs_trained=self._final_epoch,
                final_g_loss=(
                    round(self._g_losses[-1], 6) if self._g_losses else None
                ),
                final_d_loss=(
                    round(self._d_losses[-1], 6) if self._d_losses else None
                ),
            )

            # Free CUDA cache after training
            if self._device.type == "cuda":
                torch.cuda.empty_cache()
                self._logger.debug("cuda_cache_cleared_after_training")

            return {
                "g_losses": self._g_losses,
                "d_losses": self._d_losses,
                "final_epoch": self._final_epoch,
            }

        except RuntimeError as exc:
            error_msg = str(exc)
            if "out of memory" in error_msg.lower():
                self._logger.error(
                    "gan_cuda_oom_during_training",
                    error=error_msg,
                    batch_size=self.config.batch_size,
                    latent_dim=self.config.latent_dim,
                    output_dim=self.config.output_dim,
                    device=self.config.device,
                )
                # Attempt to free GPU memory before re-raising
                if self._device.type == "cuda":
                    torch.cuda.empty_cache()
            else:
                self._logger.error(
                    "gan_training_runtime_error",
                    error=error_msg,
                    error_type="RuntimeError",
                )
            raise
        except ValueError:
            # Re-raise validation errors without wrapping
            raise
        except Exception as exc:
            self._logger.error(
                "gan_training_failed",
                error=str(exc),
                error_type=type(exc).__name__,
            )
            raise

    # ------------------------------------------------------------------
    # Generation
    # ------------------------------------------------------------------

    def generate(self, num_samples: int) -> np.ndarray:
        """Generate *num_samples* synthetic records.

        Produces samples in configurable batches (max 10 000 per forward
        pass) to avoid GPU out-of-memory for large generation requests.
        Denormalises numerical columns and decodes categorical columns
        back to original label space.

        Args:
            num_samples: Number of records to produce.

        Returns:
            Numpy array of shape ``(num_samples, original_columns)``.

        Raises:
            RuntimeError: If the model has not been trained or loaded.
            ValueError: If *num_samples* < 1.
        """
        if not self._is_trained or self.generator is None:
            raise RuntimeError(
                "Model must be trained or loaded before generation"
            )
        if num_samples < 1:
            raise ValueError("num_samples must be >= 1")

        self._logger.info(
            "gan_generation_started",
            num_samples=num_samples,
            device=self.config.device,
        )

        try:
            self.generator.eval()
            all_samples: List[np.ndarray] = []
            remaining = num_samples
            batch_max = 10_000

            while remaining > 0:
                bs = min(remaining, batch_max)
                with torch.no_grad():
                    z = torch.randn(
                        bs, self.config.latent_dim, device=self._device,
                    )
                    if self._use_amp:
                        with torch.cuda.amp.autocast():
                            raw = self.generator(z).cpu().numpy()
                    else:
                        raw = self.generator(z).cpu().numpy()
                all_samples.append(raw)
                remaining -= bs

            self.generator.train()
            generated = np.concatenate(all_samples, axis=0)
            result = self._postprocess_data(generated)

            # Free CUDA cache after batch generation
            if self._device.type == "cuda":
                torch.cuda.empty_cache()

            self._logger.info(
                "gan_generation_completed",
                num_samples=result.shape[0],
                num_columns=result.shape[1],
            )
            return result

        except RuntimeError as exc:
            error_msg = str(exc)
            if "out of memory" in error_msg.lower():
                self._logger.error(
                    "gan_generation_oom",
                    error=error_msg,
                    num_samples=num_samples,
                    device=self.config.device,
                )
                if self._device.type == "cuda":
                    torch.cuda.empty_cache()
            else:
                self._logger.error(
                    "gan_generation_runtime_error",
                    error=error_msg,
                    error_type="RuntimeError",
                )
            raise
        except Exception as exc:
            self._logger.error(
                "gan_generation_failed",
                error=str(exc),
                error_type=type(exc).__name__,
            )
            raise

    # ------------------------------------------------------------------
    # Persistence — save
    # ------------------------------------------------------------------

    def save(self, path: str) -> None:
        """Save the trained GAN to *path*.

        Persists a single ``.pt`` checkpoint containing the Generator and
        Discriminator ``state_dict`` objects, the full :class:`GANConfig`,
        normalisation parameters (min / max / ranges), category maps, and
        output mapping metadata so the model can be fully reconstructed
        via :meth:`load`.

        Args:
            path: Filesystem path for the checkpoint file.

        Raises:
            RuntimeError: If the model has not been built yet.
            OSError: If the target directory cannot be created.
        """
        if self.generator is None or self.discriminator is None:
            raise RuntimeError(
                "Model must be built (via train() or load()) before saving"
            )

        # Ensure the parent directory exists
        parent_dir = os.path.dirname(path)
        if parent_dir:
            os.makedirs(parent_dir, exist_ok=True)

        try:
            checkpoint: Dict[str, Any] = {
                "generator_state": self.generator.state_dict(),
                "discriminator_state": self.discriminator.state_dict(),
                "config": {
                    "latent_dim": self.config.latent_dim,
                    "generator_hidden_dims": self.config.generator_hidden_dims,
                    "discriminator_hidden_dims": (
                        self.config.discriminator_hidden_dims
                    ),
                    "learning_rate_g": self.config.learning_rate_g,
                    "learning_rate_d": self.config.learning_rate_d,
                    "beta1": self.config.beta1,
                    "beta2": self.config.beta2,
                    "batch_size": self.config.batch_size,
                    "num_epochs": self.config.num_epochs,
                    "n_critic": self.config.n_critic,
                    "gradient_penalty_lambda": (
                        self.config.gradient_penalty_lambda
                    ),
                    "dropout_rate": self.config.dropout_rate,
                    "output_dim": self.config.output_dim,
                    "numerical_columns": self.config.numerical_columns,
                    "categorical_columns": self.config.categorical_columns,
                    "device": self.config.device,
                },
                "normalization": {
                    "num_mins": (
                        self._num_mins.tolist()
                        if self._num_mins is not None
                        else None
                    ),
                    "num_maxs": (
                        self._num_maxs.tolist()
                        if self._num_maxs is not None
                        else None
                    ),
                    "num_ranges": (
                        self._num_ranges.tolist()
                        if self._num_ranges is not None
                        else None
                    ),
                },
                "category_maps": {
                    str(k): {str(ki): str(vi) for ki, vi in v.items()}
                    for k, v in self._category_maps.items()
                },
                "output_mappings": {
                    "numerical_output_indices": self._numerical_output_indices,
                    "categorical_output_slices": [
                        list(s) for s in self._categorical_output_slices
                    ],
                    "preprocessed_dim": self._preprocessed_dim,
                },
                "training_state": {
                    "is_trained": self._is_trained,
                    "final_epoch": self._final_epoch,
                    "original_num_columns": self._original_num_columns,
                },
            }
            torch.save(checkpoint, path)
            self._logger.info(
                "gan_model_saved",
                path=path,
                output_dim=self.config.output_dim,
                is_trained=self._is_trained,
            )

        except Exception as exc:
            self._logger.error(
                "gan_model_save_failed",
                path=path,
                error=str(exc),
                error_type=type(exc).__name__,
            )
            raise

    # ------------------------------------------------------------------
    # Persistence — load (class method)
    # ------------------------------------------------------------------

    @classmethod
    def load(cls, path: str, device: str = "cpu") -> "TabularGAN":
        """Load a previously saved GAN from a checkpoint file.

        Reconstructs the :class:`Generator` and :class:`Discriminator`
        from the saved :class:`GANConfig`, loads their ``state_dict``
        weights, and restores all normalisation / category-mapping
        metadata so that :meth:`generate` works immediately.

        Args:
            path: Filesystem path to the ``.pt`` checkpoint.
            device: Target device for loaded tensors (e.g. ``"cpu"`` or
                ``"cuda:0"``).

        Returns:
            A fully initialised :class:`TabularGAN` instance.

        Raises:
            FileNotFoundError: If the checkpoint file does not exist.
            RuntimeError: If the checkpoint is corrupt or incompatible.
        """
        load_logger = get_logger(__name__)

        if not os.path.exists(path):
            load_logger.error("gan_checkpoint_not_found", path=path)
            raise FileNotFoundError(f"Checkpoint not found: {path}")

        # Validate CUDA availability for the requested device
        if "cuda" in device and not torch.cuda.is_available():
            load_logger.warning(
                "cuda_not_available_load_fallback",
                requested_device=device,
                fallback="cpu",
            )
            device = "cpu"

        try:
            checkpoint: Dict[str, Any] = torch.load(
                path, map_location=device, weights_only=False,
            )
            raw_cfg: Dict[str, Any] = checkpoint["config"]
            config = GANConfig(
                latent_dim=raw_cfg["latent_dim"],
                generator_hidden_dims=raw_cfg["generator_hidden_dims"],
                discriminator_hidden_dims=raw_cfg["discriminator_hidden_dims"],
                learning_rate_g=raw_cfg["learning_rate_g"],
                learning_rate_d=raw_cfg["learning_rate_d"],
                beta1=raw_cfg["beta1"],
                beta2=raw_cfg["beta2"],
                batch_size=raw_cfg["batch_size"],
                num_epochs=raw_cfg["num_epochs"],
                n_critic=raw_cfg["n_critic"],
                gradient_penalty_lambda=raw_cfg["gradient_penalty_lambda"],
                dropout_rate=raw_cfg["dropout_rate"],
                output_dim=raw_cfg["output_dim"],
                numerical_columns=raw_cfg["numerical_columns"],
                categorical_columns=raw_cfg["categorical_columns"],
                device=device,
            )

            instance = cls(config)

            # Build and load Generator
            instance.generator = Generator(config).to(torch.device(device))
            instance.generator.load_state_dict(checkpoint["generator_state"])

            # Build and load Discriminator
            instance.discriminator = Discriminator(config).to(
                torch.device(device),
            )
            instance.discriminator.load_state_dict(
                checkpoint["discriminator_state"],
            )

            # Restore normalisation parameters
            norm: Dict[str, Any] = checkpoint.get("normalization", {})
            if norm.get("num_mins") is not None:
                instance._num_mins = np.array(
                    norm["num_mins"], dtype=np.float64,
                )
            if norm.get("num_maxs") is not None:
                instance._num_maxs = np.array(
                    norm["num_maxs"], dtype=np.float64,
                )
            if norm.get("num_ranges") is not None:
                instance._num_ranges = np.array(
                    norm["num_ranges"], dtype=np.float64,
                )

            # Restore category maps
            raw_maps: Dict[str, Dict[str, str]] = checkpoint.get(
                "category_maps", {},
            )
            instance._category_maps = {
                int(k): {int(ki): vi for ki, vi in v.items()}
                for k, v in raw_maps.items()
            }

            # Restore output mappings
            mappings: Dict[str, Any] = checkpoint.get("output_mappings", {})
            instance._numerical_output_indices = mappings.get(
                "numerical_output_indices", [],
            )
            instance._categorical_output_slices = [
                tuple(s)
                for s in mappings.get("categorical_output_slices", [])
            ]
            instance._preprocessed_dim = mappings.get("preprocessed_dim", 0)

            # Wire column-type metadata into the loaded Generator
            instance.generator._numerical_indices = (
                instance._numerical_output_indices
            )
            instance.generator._categorical_slices = (
                instance._categorical_output_slices
            )

            # Restore training state
            ts: Dict[str, Any] = checkpoint.get("training_state", {})
            instance._is_trained = ts.get("is_trained", True)
            instance._final_epoch = ts.get("final_epoch", 0)
            instance._original_num_columns = ts.get(
                "original_num_columns", 0,
            )

            load_logger.info(
                "gan_model_loaded",
                path=path,
                device=device,
                latent_dim=config.latent_dim,
                output_dim=config.output_dim,
                is_trained=instance._is_trained,
            )
            return instance

        except FileNotFoundError:
            raise
        except Exception as exc:
            load_logger.error(
                "gan_model_load_failed",
                path=path,
                error=str(exc),
                error_type=type(exc).__name__,
            )
            raise

    # ------------------------------------------------------------------
    # Introspection
    # ------------------------------------------------------------------

    def get_training_summary(self) -> Dict[str, Any]:
        """Return a concise summary of the training outcome.

        Useful for dashboards, audit logging, and model registry metadata.

        Returns:
            Dictionary with architecture parameters, training progress,
            and final loss values.
        """
        return {
            "latent_dim": self.config.latent_dim,
            "output_dim": self.config.output_dim,
            "preprocessed_dim": self._preprocessed_dim,
            "generator_hidden_dims": self.config.generator_hidden_dims,
            "discriminator_hidden_dims": self.config.discriminator_hidden_dims,
            "final_epoch": self._final_epoch,
            "is_trained": self._is_trained,
            "final_g_loss": (
                self._g_losses[-1] if self._g_losses else None
            ),
            "final_d_loss": (
                self._d_losses[-1] if self._d_losses else None
            ),
            "num_epochs_configured": self.config.num_epochs,
            "batch_size": self.config.batch_size,
            "n_critic": self.config.n_critic,
            "gradient_penalty_lambda": self.config.gradient_penalty_lambda,
            "device": self.config.device,
            "mixed_precision": self._use_amp,
        }
