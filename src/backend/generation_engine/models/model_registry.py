"""Model versioning, loading, and caching system for the Generation Engine.

Provides a filesystem-based :class:`ModelRegistry` that manages the complete
lifecycle of trained GAN and VAE neural network models used for synthetic ERP
data generation.  Core capabilities include:

* **Registration** — Serialize PyTorch or TensorFlow model artifacts to disk
  with SHA-256 integrity checksums and semantic version tracking via a central
  ``manifest.json`` file.
* **Lazy loading** — Load model weights into memory only on first access,
  placing them on the correct device (GPU/CPU) automatically.
* **LRU caching** — Maintain frequently accessed model instances in an
  in-memory :class:`collections.OrderedDict`-backed cache with configurable
  maximum size, evicting least-recently-used entries when capacity is reached.
* **Thread safety** — All public operations are protected by a
  :class:`threading.RLock` for safe concurrent access in multi-worker
  Gunicorn deployments.
* **Distributed metadata** — Optionally synchronise model metadata to Redis
  for cross-worker lookups and publish cache invalidation events so that
  parallel Generation Engine replicas remain consistent.

Usage::

    from generation_engine.models.model_registry import (
        get_model_registry,
        ModelRegistry,
    )

    registry = get_model_registry()
    meta = registry.register_model(
        model_id="erp_gan_v1",
        model_type="gan",
        framework="pytorch",
        model_artifact=trained_gan_model,
    )
    model = registry.load_model("erp_gan_v1")
"""

from __future__ import annotations

import datetime
import hashlib
import json
import os
import shutil
import threading
import time
from collections import OrderedDict
from dataclasses import asdict, dataclass, field
from functools import lru_cache
from pathlib import Path
from typing import Any


# ---------------------------------------------------------------------------
# Third-party ML framework imports — graceful fallback when unavailable
# (e.g. lightweight test environments, CPU-only deployments, or containers
# that only install one framework).
# ---------------------------------------------------------------------------
try:
    import torch

    _TORCH_AVAILABLE: bool = True
except ImportError:  # pragma: no cover
    torch = None  # type: ignore[assignment]
    _TORCH_AVAILABLE = False

try:
    import tensorflow as tf  # type: ignore[import-untyped]

    _TF_AVAILABLE: bool = True
except ImportError:  # pragma: no cover
    tf = None
    _TF_AVAILABLE = False

# ---------------------------------------------------------------------------
# Internal imports from shared utilities
# ---------------------------------------------------------------------------
import contextlib

from shared.database.redis_client import get_redis_client
from shared.logging.structured_logger import get_logger


# ---------------------------------------------------------------------------
# Module-level logger
# ---------------------------------------------------------------------------
logger = get_logger(__name__)

# ---------------------------------------------------------------------------
# Redis key namespace constants
# ---------------------------------------------------------------------------
_REDIS_META_PREFIX: str = "model_registry:meta:"
_REDIS_INVALIDATE_CHANNEL: str = "model_registry:invalidate"

# ---------------------------------------------------------------------------
# Default configuration constants
# ---------------------------------------------------------------------------
_DEFAULT_MODEL_DIR: str = "/app/models"
_DEFAULT_CACHE_SIZE: int = 10
_CHECKSUM_BUFFER_SIZE: int = 65536  # 64 KiB read chunks for hashing


# ============================================================================
# Custom Exceptions
# ============================================================================


class ModelLoadError(Exception):
    """Raised when a model artifact cannot be loaded from disk.

    Attributes:
        model_id: Identifier of the model that failed to load.
        version: Version string that was requested.
        details: Human-readable error description.
    """

    def __init__(
        self,
        model_id: str,
        version: str | None = None,
        details: str = "",
    ) -> None:
        self.model_id = model_id
        self.version = version or "unknown"
        self.details = details
        message = (
            f"Failed to load model '{model_id}' version '{self.version}': "
            f"{details}"
        )
        super().__init__(message)


class ModelRegistrationError(Exception):
    """Raised when a model artifact cannot be registered or serialized.

    Attributes:
        model_id: Identifier of the model that failed registration.
        version: Version string that was attempted.
        details: Human-readable error description.
    """

    def __init__(
        self,
        model_id: str,
        version: str | None = None,
        details: str = "",
    ) -> None:
        self.model_id = model_id
        self.version = version or "unknown"
        self.details = details
        message = (
            f"Failed to register model '{model_id}' version "
            f"'{self.version}': {details}"
        )
        super().__init__(message)


# ============================================================================
# Model Metadata
# ============================================================================


@dataclass
class ModelMetadata:
    """Immutable metadata record for a registered model artifact.

    Each registered model version produces exactly one ``ModelMetadata``
    record which is persisted in the manifest JSON file and optionally
    replicated to Redis for distributed access.

    Attributes:
        model_id: Unique identifier for the model (e.g. ``"erp_gan_v1"``).
        model_type: Architecture type — ``'gan'`` or ``'vae'``.
        version: Semantic version string (e.g. ``'1.0.0'``).
        framework: Serialization framework — ``'pytorch'`` or ``'tensorflow'``.
        file_path: Absolute path to the saved model artifact on disk.
        file_size_bytes: Size of the serialized artifact in bytes.
        checksum: SHA-256 hex digest of the artifact file for integrity
            verification before loading.
        created_at: ISO 8601 UTC timestamp of registration.
        metadata: Arbitrary model-specific metadata such as architecture
            hyperparameters, training configuration, or performance metrics.
        device: Target compute device (e.g. ``'cpu'``, ``'cuda:0'``).
    """

    model_id: str
    model_type: str
    version: str
    framework: str
    file_path: str
    file_size_bytes: int = 0
    checksum: str = ""
    created_at: str = field(
        default_factory=lambda: datetime.datetime.now(
            datetime.UTC
        ).isoformat()
    )
    metadata: dict[str, Any] = field(default_factory=dict)
    device: str = "cpu"

    def to_dict(self) -> dict[str, Any]:
        """Serialize metadata to a JSON-compatible dictionary.

        Returns:
            Plain dictionary representation of all fields.
        """
        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> ModelMetadata:
        """Reconstruct a ``ModelMetadata`` instance from a dictionary.

        Unknown keys are silently ignored so that forward-compatible
        manifest files do not cause deserialization failures.

        Args:
            data: Dictionary previously produced by :meth:`to_dict`.

        Returns:
            Reconstructed :class:`ModelMetadata` instance.
        """
        known_fields = {f.name for f in cls.__dataclass_fields__.values()}
        filtered = {k: v for k, v in data.items() if k in known_fields}
        return cls(**filtered)


# ============================================================================
# Model Registry
# ============================================================================


class ModelRegistry:
    """Central registry for model versioning, caching, and lifecycle management.

    The registry uses a single ``manifest.json`` file inside *model_dir* to
    track all registered model versions.  Model artifacts are stored in a
    directory hierarchy of ``<model_dir>/<model_id>/<version>/``.

    An in-memory LRU cache backed by :class:`collections.OrderedDict` keeps
    the most frequently loaded model objects ready for immediate use without
    repeated disk I/O and deserialization overhead.

    When Redis is reachable the registry will:

    * Store serialized :class:`ModelMetadata` for cross-worker lookups.
    * Publish invalidation events on the
      ``model_registry:invalidate`` channel so that sibling workers can
      evict stale cache entries.

    Args:
        model_dir: Root directory for model artifact storage.  Falls back
            to the ``MODEL_PATH`` environment variable, then
            ``/app/models``.
        cache_size: Maximum number of loaded model objects to keep in the
            LRU cache.
        device: Target compute device.  When ``None`` the registry
            auto-detects: ``'cuda:0'`` if ``GPU_ENABLED=true`` **and**
            CUDA is available, otherwise ``'cpu'``.
    """

    # ------------------------------------------------------------------
    # Initialization
    # ------------------------------------------------------------------

    def __init__(
        self,
        model_dir: str | None = None,
        cache_size: int = _DEFAULT_CACHE_SIZE,
        device: str | None = None,
    ) -> None:
        # Resolve model directory from parameter → env var → default
        self._model_dir: str = (
            model_dir
            or os.environ.get("MODEL_PATH", _DEFAULT_MODEL_DIR)
        )
        self._cache_size: int = max(cache_size, 1)

        # Auto-detect device
        if device is not None:
            self._device: str = device
        else:
            gpu_enabled = os.environ.get("GPU_ENABLED", "false").lower() in (
                "true",
                "1",
                "yes",
            )
            if (
                gpu_enabled
                and _TORCH_AVAILABLE
                and torch.cuda.is_available()
            ):
                self._device = "cuda:0"
            else:
                self._device = "cpu"

        # Manifest file path
        self._manifest_path: str = os.path.join(
            self._model_dir, "manifest.json"
        )

        # Thread-safe reentrant lock for all mutable state
        self._lock: threading.RLock = threading.RLock()

        # LRU model cache:  key = "<model_id>:<version>", value = loaded model
        self._model_cache: OrderedDict[str, Any] = OrderedDict()

        # In-memory manifest:  { model_id: { version: {metadata_dict} } }
        self._manifest: dict[str, dict[str, dict[str, Any]]] = {}

        # Logger
        self._logger = get_logger(__name__)

        # Ensure model directory exists
        model_path = Path(self._model_dir)
        model_path.mkdir(parents=True, exist_ok=True)

        # Load existing manifest from disk
        self._manifest = self._load_manifest()

        self._logger.info(
            "model_registry_initialized",
            model_dir=self._model_dir,
            cache_size=self._cache_size,
            device=self._device,
            registered_models=sum(
                len(versions) for versions in self._manifest.values()
            ),
        )

    # ------------------------------------------------------------------
    # Public API — Registration
    # ------------------------------------------------------------------

    def register_model(
        self,
        model_id: str,
        model_type: str,
        framework: str,
        model_artifact: Any,
        version: str | None = None,
        metadata: dict[str, Any] | None = None,
    ) -> ModelMetadata:
        """Register and persist a trained model artifact.

        Serializes the in-memory model to disk using the appropriate
        framework method, computes a SHA-256 checksum, creates a
        :class:`ModelMetadata` record, and updates the manifest.

        If *version* is ``None`` the registry auto-increments the patch
        component of the latest existing version for *model_id* (or starts
        at ``'1.0.0'`` if no prior version exists).

        Args:
            model_id: Unique model identifier (e.g. ``"erp_gan_v1"``).
            model_type: Architecture type — ``'gan'`` or ``'vae'``.
            framework: Serialization framework — ``'pytorch'`` or
                ``'tensorflow'``.
            model_artifact: The in-memory model object to serialize.
            version: Semantic version string.  Auto-incremented when
                ``None``.
            metadata: Additional model-specific metadata dictionary.

        Returns:
            The :class:`ModelMetadata` record for the newly registered model.

        Raises:
            ModelRegistrationError: If serialization or persistence fails.
            ValueError: If *framework* is unsupported.
        """
        with self._lock:
            try:
                # Auto-increment version when not provided
                if version is None:
                    version = self._next_version(model_id)

                # Validate framework
                framework_lower = framework.lower()
                if framework_lower == "pytorch" and not _TORCH_AVAILABLE:
                    raise ModelRegistrationError(
                        model_id,
                        version,
                        "PyTorch is not installed in this environment.",
                    )
                if framework_lower == "tensorflow" and not _TF_AVAILABLE:
                    raise ModelRegistrationError(
                        model_id,
                        version,
                        "TensorFlow is not installed in this environment.",
                    )
                if framework_lower not in ("pytorch", "tensorflow"):
                    raise ValueError(
                        f"Unsupported framework '{framework}'. "
                        "Use 'pytorch' or 'tensorflow'."
                    )

                # Build artifact directory using Path.joinpath for
                # object-oriented path construction
                artifact_path = Path(self._model_dir).joinpath(
                    model_id, version
                )
                artifact_path.mkdir(parents=True, exist_ok=True)
                artifact_dir = str(artifact_path)

                # Determine file path based on framework
                if framework_lower == "pytorch":
                    file_path = os.path.join(artifact_dir, "model.pt")
                    self._save_pytorch_model(model_artifact, file_path)
                else:
                    file_path = os.path.join(artifact_dir, "model")
                    self._save_tensorflow_model(model_artifact, file_path)

                # Compute checksum and file size
                checksum = self._compute_checksum(file_path)
                file_size_bytes = os.path.getsize(file_path) if os.path.isfile(
                    file_path
                ) else self._compute_dir_size(file_path)

                # Build metadata record
                now = datetime.datetime.now(
                    datetime.UTC
                ).isoformat()
                meta = ModelMetadata(
                    model_id=model_id,
                    model_type=model_type.lower(),
                    version=version,
                    framework=framework_lower,
                    file_path=file_path,
                    file_size_bytes=file_size_bytes,
                    checksum=checksum,
                    created_at=now,
                    metadata=metadata or {},
                    device=self._device,
                )

                # Update in-memory manifest
                if model_id not in self._manifest:
                    self._manifest[model_id] = {}
                self._manifest[model_id][version] = meta.to_dict()

                # Persist manifest to disk (atomic write)
                self._save_manifest()

                # Invalidate local cache entry if it existed
                cache_key = self._cache_key(model_id, version)
                if cache_key in self._model_cache:
                    del self._model_cache[cache_key]

                # Sync metadata to Redis for cross-worker visibility
                self._sync_metadata_to_redis(meta)
                self._publish_invalidation(model_id, version, action="register")

                self._logger.info(
                    "model_registered",
                    model_id=model_id,
                    model_type=model_type,
                    version=version,
                    framework=framework_lower,
                    file_path=file_path,
                    file_size_bytes=file_size_bytes,
                    checksum=checksum,
                )

                return meta

            except (ModelRegistrationError, ValueError):
                raise
            except Exception as exc:
                self._logger.error(
                    "model_registration_failed",
                    model_id=model_id,
                    version=version or "auto",
                    error=str(exc),
                )
                raise ModelRegistrationError(
                    model_id,
                    version,
                    f"Serialization failed: {exc}",
                ) from exc

    # ------------------------------------------------------------------
    # Public API — Loading
    # ------------------------------------------------------------------

    def load_model(
        self,
        model_id: str,
        version: str | None = None,
        device: str | None = None,
    ) -> Any:
        """Load a registered model into memory with LRU caching.

        On cache hit the model is returned immediately.  On miss the
        artifact is loaded from disk, the stored SHA-256 checksum is
        verified, device placement is applied, and the model is inserted
        into the LRU cache (evicting the oldest entry if at capacity).

        Args:
            model_id: Unique model identifier.
            version: Specific version to load.  When ``None`` the latest
                registered version is used.
            device: Override the registry-level device for this load.

        Returns:
            The deserialized model object placed on the target device.

        Raises:
            ModelLoadError: If the model is not registered, the artifact
                file is missing, the checksum does not match, or
                deserialization fails.
        """
        target_device = device or self._device
        start_time = time.time()

        with self._lock:
            # Resolve version
            if version is None:
                version = self.get_latest_version(model_id)
                if version is None:
                    raise ModelLoadError(
                        model_id,
                        version,
                        "No versions registered for this model.",
                    )

            cache_key = self._cache_key(model_id, version)

            # LRU cache hit
            if cache_key in self._model_cache:
                self._model_cache.move_to_end(cache_key)
                elapsed = time.time() - start_time
                self._logger.debug(
                    "model_cache_hit",
                    model_id=model_id,
                    version=version,
                    load_time_ms=round(elapsed * 1000, 2),
                )
                return self._model_cache[cache_key]

        # Cache miss — load from disk outside the lock to avoid blocking
        # other threads during potentially slow I/O.
        meta = self.get_model(model_id, version)
        if meta is None:
            raise ModelLoadError(
                model_id,
                version,
                "Model version not found in manifest.",
            )

        file_path = meta.file_path
        if not os.path.exists(file_path):
            raise ModelLoadError(
                model_id,
                version,
                f"Artifact file missing: {file_path}",
            )

        # Verify checksum integrity before loading
        current_checksum = self._compute_checksum(file_path)
        if current_checksum != meta.checksum:
            raise ModelLoadError(
                model_id,
                version,
                (
                    f"Checksum mismatch — expected {meta.checksum}, "
                    f"got {current_checksum}. Artifact may be corrupted."
                ),
            )

        self._logger.info(
            "model_loading_from_disk",
            model_id=model_id,
            version=version,
            framework=meta.framework,
            file_path=file_path,
            device=target_device,
        )

        try:
            if meta.framework == "pytorch":
                model = self._load_pytorch_model(file_path, target_device)
            elif meta.framework == "tensorflow":
                model = self._load_tensorflow_model(file_path, target_device)
            else:
                raise ModelLoadError(
                    model_id,
                    version,
                    f"Unsupported framework: {meta.framework}",
                )
        except ModelLoadError:
            raise
        except Exception as exc:
            raise ModelLoadError(
                model_id,
                version,
                f"Deserialization failed: {exc}",
            ) from exc

        # Insert into LRU cache under the lock
        with self._lock:
            # Evict LRU if at capacity
            while len(self._model_cache) >= self._cache_size:
                self._evict_lru()
            self._model_cache[cache_key] = model

        elapsed = time.time() - start_time
        self._logger.info(
            "model_loaded",
            model_id=model_id,
            version=version,
            device=target_device,
            load_time_ms=round(elapsed * 1000, 2),
            cache_size=len(self._model_cache),
        )

        return model

    # ------------------------------------------------------------------
    # Public API — Metadata Lookup
    # ------------------------------------------------------------------

    def get_model(
        self,
        model_id: str,
        version: str | None = None,
    ) -> ModelMetadata | None:
        """Retrieve metadata for a model without loading the artifact.

        Args:
            model_id: Unique model identifier.
            version: Specific version.  When ``None`` the latest version
                is returned.

        Returns:
            :class:`ModelMetadata` instance, or ``None`` if not found.
        """
        with self._lock:
            model_versions = self._manifest.get(model_id)
            if model_versions is None:
                return None

            if version is None:
                version = self.get_latest_version(model_id)
                if version is None:
                    return None

            version_data = model_versions.get(version)
            if version_data is None:
                return None

            return ModelMetadata.from_dict(version_data)

    # ------------------------------------------------------------------
    # Public API — Listing
    # ------------------------------------------------------------------

    def list_models(
        self,
        model_type: str | None = None,
    ) -> list[ModelMetadata]:
        """List all registered model versions with optional filtering.

        Args:
            model_type: When provided, only models matching this type
                (e.g. ``'gan'``, ``'vae'``) are returned.

        Returns:
            List of :class:`ModelMetadata` instances sorted by
            ``created_at`` descending (newest first).
        """
        results: list[ModelMetadata] = []
        with self._lock:
            for _model_id, versions in self._manifest.items():
                for _ver, meta_dict in versions.items():
                    meta = ModelMetadata.from_dict(meta_dict)
                    if model_type is not None and meta.model_type != model_type.lower():
                        continue
                    results.append(meta)

        results.sort(key=lambda m: m.created_at, reverse=True)
        return results

    # ------------------------------------------------------------------
    # Public API — Version Management
    # ------------------------------------------------------------------

    def get_latest_version(self, model_id: str) -> str | None:
        """Return the highest semantic version registered for *model_id*.

        Parses version strings as ``(major, minor, patch)`` tuples and
        returns the lexicographically largest.

        Args:
            model_id: Unique model identifier.

        Returns:
            Version string (e.g. ``'1.2.3'``), or ``None`` if no
            versions are registered.
        """
        with self._lock:
            model_versions = self._manifest.get(model_id)
            if not model_versions:
                return None

            parsed: list[tuple[tuple[int, ...], str]] = []
            for ver_str in model_versions:
                try:
                    parts = tuple(int(p) for p in ver_str.split("."))
                    parsed.append((parts, ver_str))
                except (ValueError, AttributeError):
                    # Non-numeric version — treat as (0,) for sorting
                    parsed.append(((0,), ver_str))

            if not parsed:
                return None

            parsed.sort(key=lambda item: item[0], reverse=True)
            return parsed[0][1]

    # ------------------------------------------------------------------
    # Deletion helpers
    # ------------------------------------------------------------------

    def _remove_version_artifacts(
        self,
        model_id: str,
        version: str,
        meta_dict: dict[str, Any],
    ) -> None:
        """Remove filesystem artifacts for a single model version.

        Deletes the artifact file or directory referenced in *meta_dict*
        and cleans up the version directory underneath
        ``self._model_dir``.

        Args:
            model_id: Model identifier.
            version: Version string.
            meta_dict: Metadata dict for this version.
        """
        file_path = meta_dict.get("file_path", "")
        if file_path and os.path.exists(file_path):
            try:
                parent = Path(file_path).parent
                if os.path.isdir(file_path):
                    shutil.rmtree(file_path)
                else:
                    os.remove(file_path)
                    # Also remove the version directory if empty
                    if parent.exists() and not any(parent.iterdir()):
                        shutil.rmtree(str(parent))
            except OSError as exc:
                self._logger.warning(
                    "artifact_removal_failed",
                    model_id=model_id,
                    version=version,
                    file_path=file_path,
                    error=str(exc),
                )

        # Remove version directory
        version_dir = os.path.join(self._model_dir, model_id, version)
        if os.path.isdir(version_dir):
            with contextlib.suppress(OSError):
                shutil.rmtree(version_dir)

    # ------------------------------------------------------------------
    # Public API — Deletion
    # ------------------------------------------------------------------

    def delete_model(
        self,
        model_id: str,
        version: str | None = None,
    ) -> bool:
        """Remove a model version (or all versions) from the registry.

        Deletes the artifact files from the filesystem, removes the entry
        from the manifest, evicts the model from the in-memory cache, and
        publishes an invalidation event to Redis.

        Args:
            model_id: Unique model identifier.
            version: Specific version to delete.  When ``None`` **all**
                versions of the model are removed.

        Returns:
            ``True`` if at least one version was deleted, ``False`` if the
            model was not found.
        """
        with self._lock:
            model_versions = self._manifest.get(model_id)
            if model_versions is None:
                self._logger.warning(
                    "model_delete_not_found",
                    model_id=model_id,
                    version=version,
                )
                return False

            versions_to_delete: list[str] = (
                [version] if version is not None else list(model_versions.keys())
            )

            deleted_any = False
            for ver in versions_to_delete:
                meta_dict = model_versions.get(ver)
                if meta_dict is None:
                    continue

                self._remove_version_artifacts(model_id, ver, meta_dict)

                # Remove from manifest
                del model_versions[ver]

                # Evict from cache
                cache_key = self._cache_key(model_id, ver)
                if cache_key in self._model_cache:
                    del self._model_cache[cache_key]

                deleted_any = True

                self._logger.info(
                    "model_version_deleted",
                    model_id=model_id,
                    version=ver,
                )

            # Clean up model_id entry if no versions remain
            if not model_versions:
                self._manifest.pop(model_id, None)
                # Remove model_id directory
                model_dir_path = os.path.join(self._model_dir, model_id)
                if os.path.isdir(model_dir_path):
                    with contextlib.suppress(OSError):
                        shutil.rmtree(model_dir_path)

            # Persist manifest and notify
            if deleted_any:
                self._save_manifest()
                self._publish_invalidation(
                    model_id, version or "all", action="delete"
                )

            return deleted_any

    # ------------------------------------------------------------------
    # Public API — Cache Management
    # ------------------------------------------------------------------

    def clear_cache(self) -> None:
        """Evict all loaded model instances from the in-memory cache.

        For PyTorch models this also calls ``torch.cuda.empty_cache()``
        to release GPU memory held by evicted tensors.
        """
        with self._lock:
            evicted_count = len(self._model_cache)
            self._model_cache.clear()

        # Free GPU memory if PyTorch is available
        if _TORCH_AVAILABLE and torch.cuda.is_available():
            try:
                torch.cuda.empty_cache()
            except Exception as exc:
                self._logger.warning(
                    "cuda_cache_clear_failed",
                    error=str(exc),
                )

        self._logger.info(
            "model_cache_cleared",
            evicted_count=evicted_count,
        )

    # ------------------------------------------------------------------
    # Internal — Manifest I/O
    # ------------------------------------------------------------------

    def _load_manifest(self) -> dict[str, dict[str, dict[str, Any]]]:
        """Read and parse the manifest JSON file from disk.

        Returns:
            Parsed manifest dictionary, or an empty dict if the file does
            not exist or is unparseable.
        """
        if not os.path.exists(self._manifest_path):
            return {}
        try:
            with open(self._manifest_path, encoding="utf-8") as fh:
                data = json.load(fh)
            if not isinstance(data, dict):
                self._logger.warning(
                    "manifest_invalid_format",
                    path=self._manifest_path,
                )
                return {}
            return data
        except (json.JSONDecodeError, OSError) as exc:
            self._logger.warning(
                "manifest_load_failed",
                path=self._manifest_path,
                error=str(exc),
            )
            return {}

    def _save_manifest(self) -> None:
        """Persist the in-memory manifest to disk as JSON.

        Uses an atomic write strategy — writes to a temporary file first,
        then renames to the target path — to prevent corruption if the
        process is interrupted mid-write.
        """
        tmp_path = self._manifest_path + ".tmp"
        try:
            manifest_dir = os.path.dirname(self._manifest_path)
            os.makedirs(manifest_dir, exist_ok=True)

            with open(tmp_path, "w", encoding="utf-8") as fh:
                json.dump(self._manifest, fh, indent=2, default=str)

            os.rename(tmp_path, self._manifest_path)
        except OSError as exc:
            self._logger.error(
                "manifest_save_failed",
                path=self._manifest_path,
                error=str(exc),
            )
            # Clean up temp file on failure
            if os.path.exists(tmp_path):
                with contextlib.suppress(OSError):
                    os.remove(tmp_path)

    # ------------------------------------------------------------------
    # Internal — Checksum
    # ------------------------------------------------------------------

    def _compute_checksum(self, file_path: str) -> str:
        """Compute the SHA-256 hex digest of a file or directory.

        For directories (e.g. TensorFlow SavedModel), the checksum is
        computed over a sorted concatenation of all file contents within
        the directory tree.

        Args:
            file_path: Path to the file or directory.

        Returns:
            Lowercase hex-encoded SHA-256 digest string.
        """
        sha = hashlib.sha256()

        if os.path.isdir(file_path):
            # Hash all files in the directory tree in sorted order
            all_files: list[str] = []
            for root, _dirs, files in os.walk(file_path):
                for fname in files:
                    all_files.append(os.path.join(root, fname))
            all_files.sort()
            for fpath in all_files:
                try:
                    with open(fpath, "rb") as fh:
                        while True:
                            chunk = fh.read(_CHECKSUM_BUFFER_SIZE)
                            if not chunk:
                                break
                            sha.update(chunk)
                except OSError:
                    pass
        else:
            try:
                with open(file_path, "rb") as fh:
                    while True:
                        chunk = fh.read(_CHECKSUM_BUFFER_SIZE)
                        if not chunk:
                            break
                        sha.update(chunk)
            except OSError as exc:
                self._logger.warning(
                    "checksum_compute_failed",
                    file_path=file_path,
                    error=str(exc),
                )

        return sha.hexdigest()

    # ------------------------------------------------------------------
    # Internal — LRU Eviction
    # ------------------------------------------------------------------

    def _evict_lru(self) -> None:
        """Remove the least-recently-used entry from the model cache.

        For PyTorch models, attempts to free associated GPU memory via
        ``torch.cuda.empty_cache()`` after eviction.
        """
        if not self._model_cache:
            return

        evicted_key, evicted_model = self._model_cache.popitem(last=False)

        self._logger.debug(
            "model_cache_evicted",
            evicted_key=evicted_key,
        )

        # Attempt to free GPU memory for PyTorch models
        if (
            _TORCH_AVAILABLE
            and torch.cuda.is_available()
            and hasattr(evicted_model, "parameters")
        ):
            try:
                # Move model to CPU before deletion to free GPU mem
                evicted_model.cpu()
                del evicted_model
                torch.cuda.empty_cache()
            except Exception:
                # Best-effort GPU cleanup; non-critical if it fails.
                self._logger.debug("gpu_cache_cleanup_failed_on_eviction")

    # ------------------------------------------------------------------
    # Internal — PyTorch Serialization
    # ------------------------------------------------------------------

    def _save_pytorch_model(
        self, model_artifact: Any, file_path: str
    ) -> None:
        """Serialize a PyTorch model to disk.

        Attempts to save the ``state_dict()`` first (preferred for
        portability).  Falls back to full-model ``torch.save()`` if the
        artifact does not expose ``state_dict``.

        Args:
            model_artifact: The in-memory PyTorch model or state dict.
            file_path: Destination path for the ``.pt`` file.

        Raises:
            ModelRegistrationError: If serialization fails.
        """
        if not _TORCH_AVAILABLE:
            raise ModelRegistrationError(
                "unknown", None, "PyTorch is not installed."
            )

        try:
            if hasattr(model_artifact, "state_dict"):
                torch.save(model_artifact.state_dict(), file_path)
            else:
                # Assume it is already a state dict or plain tensor
                torch.save(model_artifact, file_path)
        except Exception as exc:
            raise ModelRegistrationError(
                "unknown",
                None,
                f"PyTorch serialization failed: {exc}",
            ) from exc

    def _load_pytorch_model(
        self, file_path: str, device: str
    ) -> Any:
        """Deserialize a PyTorch model from disk.

        Loads the saved artifact with ``map_location`` set to the target
        device, moves the model to that device, and sets it to evaluation
        mode.

        Args:
            file_path: Path to the ``.pt`` file.
            device: Target device string (e.g. ``'cpu'``, ``'cuda:0'``).

        Returns:
            The loaded PyTorch model in evaluation mode.
        """
        if not _TORCH_AVAILABLE:
            raise ModelLoadError(
                "unknown", None, "PyTorch is not installed."
            )

        target = torch.device(device)
        model = torch.load(
            file_path,
            map_location=target,
            weights_only=False,
        )

        # If the loaded object is a full module, move and set eval
        if hasattr(model, "to"):
            model = model.to(target)
        if hasattr(model, "eval"):
            model.eval()

        return model

    # ------------------------------------------------------------------
    # Internal — TensorFlow Serialization
    # ------------------------------------------------------------------

    def _save_tensorflow_model(
        self, model_artifact: Any, file_path: str
    ) -> None:
        """Serialize a TensorFlow/Keras model to disk.

        Uses ``tf.keras.models.save_model()`` to persist in the
        SavedModel format by default.  If the artifact has a native
        ``.save()`` method it is preferred.

        Args:
            model_artifact: The in-memory TensorFlow/Keras model.
            file_path: Destination path for the SavedModel directory.

        Raises:
            ModelRegistrationError: If serialization fails.
        """
        if not _TF_AVAILABLE:
            raise ModelRegistrationError(
                "unknown", None, "TensorFlow is not installed."
            )

        try:
            if hasattr(model_artifact, "save"):
                model_artifact.save(file_path)
            else:
                tf.keras.models.save_model(model_artifact, file_path)
        except Exception as exc:
            raise ModelRegistrationError(
                "unknown",
                None,
                f"TensorFlow serialization failed: {exc}",
            ) from exc

    def _load_tensorflow_model(
        self, file_path: str, device: str
    ) -> Any:
        """Deserialize a TensorFlow/Keras model from disk.

        Loads the model within a ``tf.device()`` context to place
        variables on the requested device.

        Args:
            file_path: Path to the SavedModel directory or HDF5 file.
            device: Target device string (e.g. ``'cpu'``, ``'cuda:0'``).

        Returns:
            The loaded TensorFlow/Keras model.
        """
        if not _TF_AVAILABLE:
            raise ModelLoadError(
                "unknown", None, "TensorFlow is not installed."
            )

        # Map common device strings to TensorFlow device names
        tf_device = self._map_tf_device(device)

        with tf.device(tf_device):
            model = tf.keras.models.load_model(file_path)

        return model

    # ------------------------------------------------------------------
    # Internal — Helpers
    # ------------------------------------------------------------------

    @staticmethod
    def _cache_key(model_id: str, version: str) -> str:
        """Build a composite cache key from model ID and version.

        Args:
            model_id: Unique model identifier.
            version: Version string.

        Returns:
            Cache key in the format ``"<model_id>:<version>"``.
        """
        return f"{model_id}:{version}"

    def _next_version(self, model_id: str) -> str:
        """Compute the next semantic version for *model_id*.

        Increments the patch component of the highest existing version.
        Returns ``'1.0.0'`` when no prior version exists.

        Args:
            model_id: Unique model identifier.

        Returns:
            Auto-incremented version string.
        """
        latest = self.get_latest_version(model_id)
        if latest is None:
            return "1.0.0"

        try:
            parts = [int(p) for p in latest.split(".")]
            while len(parts) < 3:
                parts.append(0)
            parts[2] += 1
            return ".".join(str(p) for p in parts)
        except (ValueError, AttributeError):
            return "1.0.0"

    @staticmethod
    def _map_tf_device(device: str) -> str:
        """Map a generic device string to a TensorFlow device path.

        Args:
            device: Generic device string (e.g. ``'cpu'``, ``'cuda:0'``).

        Returns:
            TensorFlow device path (e.g. ``'/CPU:0'``, ``'/GPU:0'``).
        """
        device_lower = device.lower()
        if device_lower == "cpu":
            return "/CPU:0"
        if device_lower.startswith("cuda"):
            # e.g. "cuda:0" → "/GPU:0"
            idx = device_lower.split(":")[-1] if ":" in device_lower else "0"
            return f"/GPU:{idx}"
        return "/CPU:0"

    @staticmethod
    def _compute_dir_size(dir_path: str) -> int:
        """Recursively compute the total size of all files in a directory.

        Args:
            dir_path: Path to the directory.

        Returns:
            Total size in bytes.
        """
        total: int = 0
        for root, _dirs, files in os.walk(dir_path):
            for fname in files:
                fpath = os.path.join(root, fname)
                with contextlib.suppress(OSError):
                    total += os.path.getsize(fpath)
        return total

    # ------------------------------------------------------------------
    # Internal — Redis Integration
    # ------------------------------------------------------------------

    def _sync_metadata_to_redis(self, meta: ModelMetadata) -> None:
        """Store model metadata in Redis for cross-worker visibility.

        Serializes the :class:`ModelMetadata` as JSON and stores it under
        the key ``model_registry:meta:<model_id>:<version>`` with a
        24-hour TTL.

        Failures are logged but do not interrupt the registration flow —
        Redis is an optional optimization layer.

        Args:
            meta: The metadata record to synchronize.
        """
        try:
            client = get_redis_client()
            key = f"{_REDIS_META_PREFIX}{meta.model_id}:{meta.version}"
            payload = json.dumps(meta.to_dict(), default=str)
            client.set(key, payload, ex=86400)  # 24-hour TTL
        except Exception as exc:
            self._logger.debug(
                "redis_metadata_sync_failed",
                model_id=meta.model_id,
                version=meta.version,
                error=str(exc),
            )

    def _publish_invalidation(
        self,
        model_id: str,
        version: str,
        action: str = "invalidate",
    ) -> None:
        """Publish a cache invalidation event to the Redis channel.

        Sibling Generation Engine workers subscribing to the
        ``model_registry:invalidate`` channel can evict their local
        cache entries upon receiving these messages.

        Args:
            model_id: Identifier of the affected model.
            version: Version string (or ``"all"`` for bulk operations).
            action: Event type — ``"register"``, ``"delete"``, or
                ``"invalidate"``.
        """
        try:
            client = get_redis_client()
            message = json.dumps(
                {
                    "model_id": model_id,
                    "version": version,
                    "action": action,
                    "timestamp": datetime.datetime.now(
                        datetime.UTC
                    ).isoformat(),
                }
            )
            client.publish(_REDIS_INVALIDATE_CHANNEL, message)
        except Exception as exc:
            self._logger.debug(
                "redis_invalidation_publish_failed",
                model_id=model_id,
                version=version,
                error=str(exc),
            )


# ============================================================================
# Module-level Singleton Factory
# ============================================================================

# Lock for thread-safe singleton initialization (complements @lru_cache)
_singleton_lock: threading.Lock = threading.Lock()


@lru_cache(maxsize=4)
def get_model_registry(model_dir: str | None = None) -> ModelRegistry:
    """Return a shared :class:`ModelRegistry` singleton instance.

    On the first call a new ``ModelRegistry`` is created with the
    specified (or default) *model_dir*.  Subsequent calls with the same
    argument return the cached instance.

    Thread-safe via :func:`functools.lru_cache` internal locking and an
    additional module-level :class:`threading.Lock` guard.

    Args:
        model_dir: Root directory for model storage.  Falls back to the
            ``MODEL_PATH`` environment variable, then ``/app/models``.

    Returns:
        Shared :class:`ModelRegistry` instance.

    Example::

        registry = get_model_registry()
        model = registry.load_model("erp_gan_v1")
    """
    with _singleton_lock:
        logger.info(
            "creating_model_registry_singleton",
            model_dir=model_dir or os.environ.get("MODEL_PATH", _DEFAULT_MODEL_DIR),
        )
        return ModelRegistry(model_dir=model_dir)
