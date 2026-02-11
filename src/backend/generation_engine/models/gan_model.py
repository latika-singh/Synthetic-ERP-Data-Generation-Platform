"""GAN architecture for tabular synthetic data generation.

Implements a Wasserstein GAN with Gradient Penalty (WGAN-GP) using PyTorch
for generating high-fidelity synthetic records that match the statistical
distributions of source ERP data.  The architecture includes:

* **Generator** — maps random noise vectors to synthetic records with
  mixed numerical / categorical columns.
* **Discriminator** — a critic network that scores records as real or
  synthetic using spectral normalisation for training stability.
* **TabularGAN** — orchestrates adversarial training, generation, and
  model persistence.

Usage example::

    from generation_engine.models.gan_model import TabularGAN, GANConfig
    import numpy as np

    cfg = GANConfig(latent_dim=128, num_epochs=300)
    gan = TabularGAN(cfg)
    history = gan.train(real_data=np.random.randn(1000, 10))
    synthetic = gan.generate(num_samples=500)
"""

from __future__ import annotations

import json
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
# Module-level logger
# ---------------------------------------------------------------------------
logger = get_logger(__name__)


# ============================================================================
# Configuration
# ============================================================================


@dataclass
class GANConfig:
    """Hyperparameter configuration for the tabular GAN.

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
        output_dim: Number of output features (set automatically).
        numerical_columns: Indices of numerical columns in the data.
        categorical_columns: Metadata for categorical columns.
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
# Generator
# ============================================================================


class Generator(nn.Module):
    """Fully-connected generator mapping noise → synthetic record.

    The forward pass applies per-column activations: *sigmoid* for
    normalised numerical columns and *Gumbel-Softmax* for one-hot
    categorical columns.

    Args:
        config: A :class:`GANConfig` instance with architecture settings.
    """

    def __init__(self, config: GANConfig) -> None:
        super().__init__()
        self.config = config
        self._numerical_indices: list[int] = []
        self._categorical_slices: list[Tuple[int, int]] = []

        layers: list[nn.Module] = []
        in_dim = config.latent_dim
        for h_dim in config.generator_hidden_dims:
            layers.append(nn.Linear(in_dim, h_dim))
            layers.append(nn.BatchNorm1d(h_dim))
            layers.append(nn.LeakyReLU(0.2, inplace=True))
            in_dim = h_dim
        layers.append(nn.Linear(in_dim, config.output_dim))
        self.net = nn.Sequential(*layers)

        # He initialisation
        self._init_weights()

    # ---- weight init ---------------------------------------------------------

    def _init_weights(self) -> None:
        """Apply Kaiming He initialisation to Linear layers."""
        for module in self.modules():
            if isinstance(module, nn.Linear):
                nn.init.kaiming_normal_(module.weight, nonlinearity="leaky_relu")
                if module.bias is not None:
                    nn.init.zeros_(module.bias)

    # ---- forward -------------------------------------------------------------

    def forward(self, z: torch.Tensor) -> torch.Tensor:
        """Forward pass producing a batch of synthetic records.

        Args:
            z: Noise tensor of shape ``(batch, latent_dim)``.

        Returns:
            Generated record batch of shape ``(batch, output_dim)``.
        """
        raw = self.net(z)

        if not self._numerical_indices and not self._categorical_slices:
            return torch.sigmoid(raw)

        parts: list[torch.Tensor] = []
        last = 0
        for start, end in sorted(
            [(i, i + 1) for i in self._numerical_indices]
            + list(self._categorical_slices),
            key=lambda t: t[0],
        ):
            if start > last:
                parts.append(raw[:, last:start])
            if (start, end) in [(i, i + 1) for i in self._numerical_indices]:
                parts.append(torch.sigmoid(raw[:, start:end]))
            else:
                parts.append(
                    nn.functional.gumbel_softmax(raw[:, start:end], tau=0.5, hard=False),
                )
            last = end
        if last < raw.shape[1]:
            parts.append(torch.sigmoid(raw[:, last:]))
        return torch.cat(parts, dim=1)

    # ---- convenience ---------------------------------------------------------

    def generate(
        self,
        num_samples: int,
        device: str | None = None,
    ) -> torch.Tensor:
        """Generate samples in eval mode without gradient tracking.

        Args:
            num_samples: Number of records to produce.
            device: Override device for noise tensor.

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

    No sigmoid at output — raw Wasserstein scores.  Spectral normalisation
    is applied to all :class:`torch.nn.Linear` layers for stable training.

    Args:
        config: A :class:`GANConfig` instance with architecture settings.
    """

    def __init__(self, config: GANConfig) -> None:
        super().__init__()
        self.config = config

        layers: list[nn.Module] = []
        in_dim = config.output_dim
        for h_dim in config.discriminator_hidden_dims:
            layers.append(nn.utils.spectral_norm(nn.Linear(in_dim, h_dim)))
            layers.append(nn.LeakyReLU(0.2, inplace=True))
            layers.append(nn.Dropout(config.dropout_rate))
            in_dim = h_dim
        layers.append(nn.utils.spectral_norm(nn.Linear(in_dim, 1)))
        self.net = nn.Sequential(*layers)

    def forward(self, x: torch.Tensor) -> torch.Tensor:
        """Return critic score for each record.

        Args:
            x: Data tensor ``(batch, output_dim)``.

        Returns:
            Wasserstein score tensor ``(batch, 1)``.
        """
        return self.net(x)


# ============================================================================
# TabularGAN – main interface
# ============================================================================


class TabularGAN:
    """High-level interface for training and generating tabular data with a GAN.

    Orchestrates the adversarial training loop using WGAN-GP, manages
    data normalisation / encoding, and provides persistence methods.

    Args:
        config: Hyperparameter configuration.
    """

    def __init__(self, config: GANConfig) -> None:
        if config is None:
            raise ValueError("config must not be None")

        self.config = config
        self._device = torch.device(config.device)
        self.generator: Generator | None = None
        self.discriminator: Discriminator | None = None
        self.opt_g: optim.Adam | None = None
        self.opt_d: optim.Adam | None = None

        # Normalisation / encoding state
        self._num_mins: np.ndarray | None = None
        self._num_maxs: np.ndarray | None = None
        self._num_ranges: np.ndarray | None = None
        self._category_maps: Dict[int, Dict[int, Any]] = {}
        self._preprocessed_dim: int = 0
        self._original_num_columns: int = 0
        self._numerical_output_indices: list[int] = []
        self._categorical_output_slices: list[Tuple[int, int]] = []

        # Training state
        self._is_trained: bool = False
        self._final_epoch: int = 0
        self._g_losses: list[float] = []
        self._d_losses: list[float] = []

        self._logger = get_logger(__name__)
        self._logger.info(
            "tabular_gan_initialized",
            latent_dim=config.latent_dim,
            device=config.device,
            num_numerical=len(config.numerical_columns),
            num_categorical=len(config.categorical_columns),
        )

    # ------------------------------------------------------------------
    # Data preprocessing helpers
    # ------------------------------------------------------------------

    def _preprocess_data(self, data: np.ndarray) -> np.ndarray:
        """Normalise numerical cols and one-hot encode categoricals.

        Args:
            data: Raw data array of shape ``(N, num_columns)``.

        Returns:
            Preprocessed float32 array ready for training.
        """
        self._original_num_columns = data.shape[1]
        parts: list[np.ndarray] = []
        self._numerical_output_indices = []
        self._categorical_output_slices = []
        offset = 0

        num_cols = self.config.numerical_columns
        cat_cols = self.config.categorical_columns

        if not num_cols and not cat_cols:
            num_cols = list(range(data.shape[1]))

        # Numerical ----------------------------------------------------------
        if num_cols:
            num_data = data[:, num_cols].astype(np.float64)
            self._num_mins = num_data.min(axis=0)
            self._num_maxs = num_data.max(axis=0)
            self._num_ranges = self._num_maxs - self._num_mins
            self._num_ranges[self._num_ranges == 0] = 1.0
            normalised = (num_data - self._num_mins) / self._num_ranges
            parts.append(normalised.astype(np.float32))
            for i in range(len(num_cols)):
                self._numerical_output_indices.append(offset + i)
            offset += len(num_cols)

        # Categorical --------------------------------------------------------
        for cat_meta in cat_cols:
            col_idx = cat_meta["index"]
            n_cats = cat_meta["num_categories"]
            col_data = data[:, col_idx].astype(int)

            unique_vals = sorted(set(col_data.tolist()))
            cat_map: Dict[int, Any] = {v: idx for idx, v in enumerate(unique_vals)}
            self._category_maps[col_idx] = cat_map

            one_hot = np.zeros((data.shape[0], max(n_cats, len(unique_vals))), dtype=np.float32)
            for row_i, val in enumerate(col_data):
                mapped = cat_map.get(int(val), 0)
                one_hot[row_i, mapped] = 1.0
            actual_cats = one_hot.shape[1]
            self._categorical_output_slices.append((offset, offset + actual_cats))
            parts.append(one_hot)
            offset += actual_cats

        if not parts:
            return data.astype(np.float32)

        preprocessed = np.concatenate(parts, axis=1)
        self._preprocessed_dim = preprocessed.shape[1]
        return preprocessed

    def _postprocess_data(self, generated: np.ndarray) -> np.ndarray:
        """Denormalise numerical cols and decode categoricals.

        Args:
            generated: Generated float array from the decoder.

        Returns:
            Post-processed array matching original column layout.
        """
        num_cols = self.config.numerical_columns
        cat_cols = self.config.categorical_columns
        if not num_cols and not cat_cols:
            num_cols = list(range(self._original_num_columns))

        n_samples = generated.shape[0]
        result = np.zeros((n_samples, self._original_num_columns), dtype=np.float64)

        # Numerical ----------------------------------------------------------
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

        # Categorical --------------------------------------------------------
        for cat_meta, (start, end) in zip(cat_cols, self._categorical_output_slices):
            col_idx = cat_meta["index"]
            cat_probs = generated[:, start:end]
            cat_indices = np.argmax(cat_probs, axis=1)
            inv_map = {v: k for k, v in self._category_maps.get(col_idx, {}).items()}
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
        """Compute gradient penalty for WGAN-GP.

        Args:
            real: Real data batch.
            fake: Generated data batch.

        Returns:
            Scalar gradient penalty tensor.
        """
        alpha = torch.rand(real.size(0), 1, device=self._device)
        interpolated = (alpha * real + (1 - alpha) * fake).requires_grad_(True)
        d_interp = self.discriminator(interpolated)

        gradients = torch.autograd.grad(
            outputs=d_interp,
            inputs=interpolated,
            grad_outputs=torch.ones_like(d_interp),
            create_graph=True,
            retain_graph=True,
        )[0]
        gradients = gradients.view(gradients.size(0), -1)
        gp = ((gradients.norm(2, dim=1) - 1) ** 2).mean()
        return self.config.gradient_penalty_lambda * gp

    # ------------------------------------------------------------------
    # Training
    # ------------------------------------------------------------------

    def train(
        self,
        real_data: np.ndarray,
        callbacks: list[Any] | None = None,
    ) -> Dict[str, Any]:
        """Train the GAN on *real_data*.

        Args:
            real_data: Array of shape ``(N, features)``.
            callbacks: Optional list of callables invoked per epoch
                with ``(epoch, g_loss, d_loss)`` arguments.

        Returns:
            Training history dictionary with per-epoch losses.

        Raises:
            ValueError: If *real_data* is ``None`` or empty.
        """
        if real_data is None or (hasattr(real_data, "size") and real_data.size == 0):
            raise ValueError("real_data must be non-empty numpy array")

        self._logger.info(
            "gan_training_started",
            num_samples=real_data.shape[0],
            num_raw_columns=real_data.shape[1],
            latent_dim=self.config.latent_dim,
            num_epochs=self.config.num_epochs,
            batch_size=self.config.batch_size,
        )

        try:
            preprocessed = self._preprocess_data(real_data)
            self.config.output_dim = preprocessed.shape[1]

            # Build networks ---------------------------------------------------
            self.generator = Generator(self.config).to(self._device)
            self.discriminator = Discriminator(self.config).to(self._device)

            # Apply column-type info to generator
            self.generator._numerical_indices = self._numerical_output_indices
            self.generator._categorical_slices = self._categorical_output_slices

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

            dataset = TensorDataset(
                torch.tensor(preprocessed, dtype=torch.float32),
            )
            loader = DataLoader(
                dataset,
                batch_size=self.config.batch_size,
                shuffle=True,
                drop_last=True,
            )

            # Training loop ----------------------------------------------------
            self._g_losses = []
            self._d_losses = []

            for epoch in range(self.config.num_epochs):
                epoch_g_loss = 0.0
                epoch_d_loss = 0.0
                n_batches = 0

                for (real_batch,) in loader:
                    real_batch = real_batch.to(self._device)
                    bs = real_batch.size(0)

                    # ---- Train Discriminator ---------------------------------
                    for _ in range(self.config.n_critic):
                        z = torch.randn(bs, self.config.latent_dim, device=self._device)
                        fake = self.generator(z).detach()

                        d_real = self.discriminator(real_batch).mean()
                        d_fake = self.discriminator(fake).mean()
                        gp = self._compute_gradient_penalty(real_batch, fake)
                        d_loss = d_fake - d_real + gp

                        self.opt_d.zero_grad()
                        d_loss.backward()
                        self.opt_d.step()

                    # ---- Train Generator -------------------------------------
                    z = torch.randn(bs, self.config.latent_dim, device=self._device)
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

                if epoch % 10 == 0 or epoch == self.config.num_epochs - 1:
                    self._logger.debug(
                        "gan_epoch_completed",
                        epoch=epoch,
                        g_loss=round(avg_g, 6),
                        d_loss=round(avg_d, 6),
                    )

                if callbacks:
                    for cb in callbacks:
                        cb(epoch, avg_g, avg_d)

            self._is_trained = True
            self._final_epoch = self.config.num_epochs
            self._logger.info(
                "gan_training_completed",
                epochs_trained=self._final_epoch,
                final_g_loss=round(self._g_losses[-1], 6) if self._g_losses else None,
                final_d_loss=round(self._d_losses[-1], 6) if self._d_losses else None,
            )

            return {
                "g_losses": self._g_losses,
                "d_losses": self._d_losses,
                "final_epoch": self._final_epoch,
            }

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

        Args:
            num_samples: Number of records to produce.

        Returns:
            Numpy array of shape ``(num_samples, original_columns)``.

        Raises:
            RuntimeError: If the model has not been trained or loaded.
            ValueError: If *num_samples* < 1.
        """
        if not self._is_trained or self.generator is None:
            raise RuntimeError("Model must be trained or loaded before generation")
        if num_samples < 1:
            raise ValueError("num_samples must be >= 1")

        self._logger.info("gan_generation_started", num_samples=num_samples)

        try:
            self.generator.eval()
            all_samples: list[np.ndarray] = []
            remaining = num_samples
            batch_max = 10000

            while remaining > 0:
                bs = min(remaining, batch_max)
                with torch.no_grad():
                    z = torch.randn(bs, self.config.latent_dim, device=self._device)
                    raw = self.generator(z).cpu().numpy()
                all_samples.append(raw)
                remaining -= bs

            self.generator.train()
            generated = np.concatenate(all_samples, axis=0)
            result = self._postprocess_data(generated)

            self._logger.info(
                "gan_generation_completed",
                num_samples=result.shape[0],
                num_columns=result.shape[1],
            )
            return result

        except Exception as exc:
            self._logger.error(
                "gan_generation_failed",
                error=str(exc),
                error_type=type(exc).__name__,
            )
            raise

    # ------------------------------------------------------------------
    # Persistence
    # ------------------------------------------------------------------

    def save(self, path: str) -> None:
        """Save the trained GAN to *path*.

        Creates a single ``.pt`` checkpoint containing generator and
        discriminator state-dicts, config, and normalisation params.

        Args:
            path: File path for the checkpoint.

        Raises:
            RuntimeError: If the model has not been built.
        """
        if self.generator is None or self.discriminator is None:
            raise RuntimeError("Model must be built before saving")

        os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
        try:
            checkpoint: Dict[str, Any] = {
                "generator_state": self.generator.state_dict(),
                "discriminator_state": self.discriminator.state_dict(),
                "config": {
                    "latent_dim": self.config.latent_dim,
                    "generator_hidden_dims": self.config.generator_hidden_dims,
                    "discriminator_hidden_dims": self.config.discriminator_hidden_dims,
                    "learning_rate_g": self.config.learning_rate_g,
                    "learning_rate_d": self.config.learning_rate_d,
                    "beta1": self.config.beta1,
                    "beta2": self.config.beta2,
                    "batch_size": self.config.batch_size,
                    "num_epochs": self.config.num_epochs,
                    "n_critic": self.config.n_critic,
                    "gradient_penalty_lambda": self.config.gradient_penalty_lambda,
                    "dropout_rate": self.config.dropout_rate,
                    "output_dim": self.config.output_dim,
                    "numerical_columns": self.config.numerical_columns,
                    "categorical_columns": self.config.categorical_columns,
                    "device": self.config.device,
                },
                "normalization": {
                    "num_mins": self._num_mins.tolist() if self._num_mins is not None else None,
                    "num_maxs": self._num_maxs.tolist() if self._num_maxs is not None else None,
                    "num_ranges": self._num_ranges.tolist() if self._num_ranges is not None else None,
                },
                "category_maps": {
                    str(k): {str(ki): str(vi) for ki, vi in v.items()}
                    for k, v in self._category_maps.items()
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
            torch.save(checkpoint, path)
            self._logger.info("gan_model_saved", path=path)

        except Exception as exc:
            self._logger.error(
                "gan_model_save_failed",
                path=path,
                error=str(exc),
                error_type=type(exc).__name__,
            )
            raise

    @classmethod
    def load(cls, path: str, device: str = "cpu") -> "TabularGAN":
        """Load a trained GAN from a checkpoint file.

        Args:
            path: File path to the checkpoint.
            device: Target device for loaded tensors.

        Returns:
            Fully initialised :class:`TabularGAN` instance.

        Raises:
            FileNotFoundError: If the checkpoint file is missing.
        """
        load_logger = get_logger(__name__)
        if not os.path.exists(path):
            raise FileNotFoundError(f"Checkpoint not found: {path}")

        try:
            checkpoint = torch.load(path, map_location=device, weights_only=False)
            raw_cfg = checkpoint["config"]
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
            instance.generator = Generator(config).to(torch.device(device))
            instance.discriminator = Discriminator(config).to(torch.device(device))

            instance.generator.load_state_dict(checkpoint["generator_state"])
            instance.discriminator.load_state_dict(checkpoint["discriminator_state"])

            # Restore normalisation
            norm = checkpoint.get("normalization", {})
            if norm.get("num_mins") is not None:
                instance._num_mins = np.array(norm["num_mins"], dtype=np.float64)
            if norm.get("num_maxs") is not None:
                instance._num_maxs = np.array(norm["num_maxs"], dtype=np.float64)
            if norm.get("num_ranges") is not None:
                instance._num_ranges = np.array(norm["num_ranges"], dtype=np.float64)

            # Restore category maps
            raw_maps = checkpoint.get("category_maps", {})
            instance._category_maps = {
                int(k): {int(ki): vi for ki, vi in v.items()}
                for k, v in raw_maps.items()
            }

            # Restore output mappings
            mappings = checkpoint.get("output_mappings", {})
            instance._numerical_output_indices = mappings.get("numerical_output_indices", [])
            instance._categorical_output_slices = [
                tuple(s) for s in mappings.get("categorical_output_slices", [])
            ]
            instance._preprocessed_dim = mappings.get("preprocessed_dim", 0)

            # Restore generator column info
            instance.generator._numerical_indices = instance._numerical_output_indices
            instance.generator._categorical_slices = instance._categorical_output_slices

            # Restore training state
            ts = checkpoint.get("training_state", {})
            instance._is_trained = ts.get("is_trained", True)
            instance._final_epoch = ts.get("final_epoch", 0)
            instance._original_num_columns = ts.get("original_num_columns", 0)

            load_logger.info(
                "gan_model_loaded",
                path=path,
                latent_dim=config.latent_dim,
                is_trained=instance._is_trained,
            )
            return instance

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
        """Return a concise summary of training outcome.

        Returns:
            Dictionary with architecture, training, and loss information.
        """
        return {
            "latent_dim": self.config.latent_dim,
            "output_dim": self.config.output_dim,
            "preprocessed_dim": self._preprocessed_dim,
            "final_epoch": self._final_epoch,
            "is_trained": self._is_trained,
            "final_g_loss": self._g_losses[-1] if self._g_losses else None,
            "final_d_loss": self._d_losses[-1] if self._d_losses else None,
            "num_epochs_configured": self.config.num_epochs,
            "device": self.config.device,
        }
