"""Model versioning, loading, caching, and lifecycle management.

Provides a thread-safe :class:`ModelRegistry` that tracks trained ML models
(GAN, VAE, etc.), persists metadata in MongoDB, and manages a local disk
cache for rapid retrieval.  Key capabilities:

* Register a newly trained model (with artifact path)
* Look up a model by ID or by combination of schema + generation method
* Load a model into memory with LRU caching
* Promote, deprecate, and delete model versions
* List registered models with pagination / filtering

Usage example::

    from generation_engine.models.model_registry import ModelRegistry
    registry = ModelRegistry(base_path="/models")
    model_id = registry.register("my_gan_v1", "gan", "/models/my_gan_v1.pt", metadata={})
    model = registry.load(model_id)
"""

from __future__ import annotations

import json
import os
import shutil
import threading
import time
import uuid
from collections import OrderedDict
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from enum import Enum
from typing import Any, Dict, List, Optional, Tuple, Type, Union

import numpy as np

from shared.logging.structured_logger import get_logger

# ---------------------------------------------------------------------------
# Module-level logger
# ---------------------------------------------------------------------------
logger = get_logger(__name__)


# ============================================================================
# Enumerations
# ============================================================================


class ModelStatus(str, Enum):
    """Lifecycle status of a registered model."""

    REGISTERED = "registered"
    ACTIVE = "active"
    DEPRECATED = "deprecated"
    ARCHIVED = "archived"


class ModelType(str, Enum):
    """Supported model architecture types."""

    GAN = "gan"
    VAE = "vae"
    STATISTICAL = "statistical"
    RULES = "rules"


# ============================================================================
# Model Metadata
# ============================================================================


@dataclass
class ModelMetadata:
    """Immutable metadata record for a registered model artefact.

    Attributes:
        model_id: Unique identifier for this model version.
        name: Human-readable model name.
        model_type: Architecture type (GAN, VAE, etc.).
        artifact_path: Path to the persisted model artefact.
        status: Current lifecycle status.
        version: Semantic version string.
        schema_id: Optional ID of the ERP schema this model was trained on.
        generation_method: The generation method this model supports.
        metrics: Training / quality metrics dictionary.
        created_at: ISO 8601 creation timestamp.
        updated_at: ISO 8601 last-update timestamp.
        created_by: User or system identifier that registered the model.
        description: Free-form description of the model.
        tags: Searchable tags for classification.
        config: Hyperparameter snapshot used during training.
        training_data_summary: High-level stats about the training data.
    """

    model_id: str
    name: str
    model_type: str
    artifact_path: str
    status: str = ModelStatus.REGISTERED.value
    version: str = "1.0.0"
    schema_id: str | None = None
    generation_method: str | None = None
    metrics: Dict[str, Any] = field(default_factory=dict)
    created_at: str = field(default_factory=lambda: datetime.now(timezone.utc).isoformat())
    updated_at: str = field(default_factory=lambda: datetime.now(timezone.utc).isoformat())
    created_by: str = "system"
    description: str = ""
    tags: list[str] = field(default_factory=list)
    config: Dict[str, Any] = field(default_factory=dict)
    training_data_summary: Dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> Dict[str, Any]:
        """Serialise metadata to a plain dictionary.

        Returns:
            Metadata as a JSON-compatible dictionary.
        """
        return asdict(self)

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "ModelMetadata":
        """Deserialise metadata from a dictionary.

        Args:
            data: Dictionary produced by :meth:`to_dict`.

        Returns:
            Reconstructed :class:`ModelMetadata` instance.
        """
        known_fields = {f.name for f in cls.__dataclass_fields__.values()}
        filtered = {k: v for k, v in data.items() if k in known_fields}
        return cls(**filtered)


# ============================================================================
# LRU Model Cache
# ============================================================================


class _LRUModelCache:
    """Thread-safe least-recently-used cache for loaded models.

    Args:
        max_size: Maximum number of models held in memory.
    """

    def __init__(self, max_size: int = 10) -> None:
        self._max_size = max(max_size, 1)
        self._cache: OrderedDict[str, Any] = OrderedDict()
        self._lock = threading.Lock()

    def get(self, model_id: str) -> Any | None:
        """Fetch a model from cache, promoting it to most-recently-used.

        Args:
            model_id: Unique model identifier.

        Returns:
            Cached model object or ``None``.
        """
        with self._lock:
            if model_id in self._cache:
                self._cache.move_to_end(model_id)
                return self._cache[model_id]
        return None

    def put(self, model_id: str, model: Any) -> None:
        """Insert or update a model in the cache, evicting LRU if full.

        Args:
            model_id: Unique model identifier.
            model: The loaded model object.
        """
        with self._lock:
            if model_id in self._cache:
                self._cache.move_to_end(model_id)
                self._cache[model_id] = model
            else:
                if len(self._cache) >= self._max_size:
                    evicted_key, _ = self._cache.popitem(last=False)
                    logger.debug("model_cache_evicted", evicted_model_id=evicted_key)
                self._cache[model_id] = model

    def remove(self, model_id: str) -> None:
        """Remove a specific model from the cache.

        Args:
            model_id: Unique model identifier.
        """
        with self._lock:
            self._cache.pop(model_id, None)

    def clear(self) -> None:
        """Evict all entries from the cache."""
        with self._lock:
            self._cache.clear()

    @property
    def size(self) -> int:
        """Return the current number of cached models."""
        with self._lock:
            return len(self._cache)


# ============================================================================
# Model Registry
# ============================================================================


class ModelRegistry:
    """Central registry for model versioning, caching, and lifecycle management.

    By default the registry stores metadata as JSON sidecar files alongside
    model artefacts.  When a ``mongo_collection`` is provided, metadata is
    persisted to MongoDB instead.

    Args:
        base_path: Root directory for model artefact storage.
        cache_size: Maximum number of models held in the in-memory LRU cache.
        mongo_collection: Optional PyMongo collection for metadata persistence.
    """

    def __init__(
        self,
        base_path: str = "/tmp/model_registry",
        cache_size: int = 10,
        mongo_collection: Any | None = None,
    ) -> None:
        self._base_path = base_path
        self._cache = _LRUModelCache(max_size=cache_size)
        self._mongo: Any | None = mongo_collection
        self._lock = threading.Lock()
        self._metadata_store: Dict[str, ModelMetadata] = {}
        self._logger = get_logger(__name__)

        os.makedirs(base_path, exist_ok=True)

        # Attempt to rebuild index from disk
        self._rebuild_index()

        self._logger.info(
            "model_registry_initialized",
            base_path=base_path,
            cache_size=cache_size,
            use_mongo=self._mongo is not None,
            known_models=len(self._metadata_store),
        )

    # ------------------------------------------------------------------
    # Index management
    # ------------------------------------------------------------------

    def _rebuild_index(self) -> None:
        """Scan *base_path* for sidecar metadata files and re-index."""
        if self._mongo is not None:
            try:
                cursor = self._mongo.find({})
                for doc in cursor:
                    doc.pop("_id", None)
                    meta = ModelMetadata.from_dict(doc)
                    self._metadata_store[meta.model_id] = meta
            except Exception as exc:
                self._logger.warning(
                    "mongo_index_rebuild_failed",
                    error=str(exc),
                )
            return

        for entry in os.listdir(self._base_path):
            meta_path = os.path.join(self._base_path, entry, "metadata.json")
            if os.path.isfile(meta_path):
                try:
                    with open(meta_path, "r") as fh:
                        raw = json.load(fh)
                    meta = ModelMetadata.from_dict(raw)
                    self._metadata_store[meta.model_id] = meta
                except Exception as exc:
                    self._logger.warning(
                        "metadata_load_failed",
                        path=meta_path,
                        error=str(exc),
                    )

    # ------------------------------------------------------------------
    # CRUD operations
    # ------------------------------------------------------------------

    def register(
        self,
        name: str,
        model_type: str,
        artifact_path: str,
        *,
        version: str = "1.0.0",
        schema_id: str | None = None,
        generation_method: str | None = None,
        metrics: Dict[str, Any] | None = None,
        description: str = "",
        tags: list[str] | None = None,
        config: Dict[str, Any] | None = None,
        training_data_summary: Dict[str, Any] | None = None,
        created_by: str = "system",
    ) -> str:
        """Register a new model artefact.

        Args:
            name: Human-readable name.
            model_type: One of ``'gan'``, ``'vae'``, ``'statistical'``, ``'rules'``.
            artifact_path: Path where the artefact is persisted.
            version: Semantic version string.
            schema_id: ERP schema this model targets.
            generation_method: Generation method string.
            metrics: Training / quality metrics.
            description: Free-form description.
            tags: Searchable tags.
            config: Hyperparameter configuration snapshot.
            training_data_summary: High-level stats about the training data.
            created_by: Identifier of the user or system registering the model.

        Returns:
            Unique ``model_id`` string.

        Raises:
            ValueError: If required fields are missing or empty.
            FileNotFoundError: If *artifact_path* does not exist.
        """
        if not name or not name.strip():
            raise ValueError("name must not be empty")
        if not model_type or not model_type.strip():
            raise ValueError("model_type must not be empty")
        if not artifact_path or not artifact_path.strip():
            raise ValueError("artifact_path must not be empty")
        if not os.path.exists(artifact_path):
            raise FileNotFoundError(f"Artifact not found: {artifact_path}")

        model_id = str(uuid.uuid4())
        now = datetime.now(timezone.utc).isoformat()

        meta = ModelMetadata(
            model_id=model_id,
            name=name,
            model_type=model_type,
            artifact_path=artifact_path,
            status=ModelStatus.REGISTERED.value,
            version=version,
            schema_id=schema_id,
            generation_method=generation_method,
            metrics=metrics or {},
            created_at=now,
            updated_at=now,
            created_by=created_by,
            description=description,
            tags=tags or [],
            config=config or {},
            training_data_summary=training_data_summary or {},
        )

        with self._lock:
            self._metadata_store[model_id] = meta
            self._persist_metadata(meta)

        self._logger.info(
            "model_registered",
            model_id=model_id,
            name=name,
            model_type=model_type,
            version=version,
        )
        return model_id

    def get_metadata(self, model_id: str) -> ModelMetadata | None:
        """Retrieve metadata for a model by ID.

        Args:
            model_id: Unique model identifier.

        Returns:
            :class:`ModelMetadata` instance or ``None`` if not found.
        """
        return self._metadata_store.get(model_id)

    def load(self, model_id: str) -> Any:
        """Load a trained model into memory (with cache).

        Attempts the LRU cache first; on miss, loads from the artefact
        file and caches the result.

        Args:
            model_id: Unique model identifier.

        Returns:
            Loaded model object (type depends on *model_type*).

        Raises:
            KeyError: If *model_id* is not registered.
            FileNotFoundError: If the artefact file is missing.
            RuntimeError: If the model type is unsupported.
        """
        cached = self._cache.get(model_id)
        if cached is not None:
            self._logger.debug("model_cache_hit", model_id=model_id)
            return cached

        meta = self._metadata_store.get(model_id)
        if meta is None:
            raise KeyError(f"No model registered with ID: {model_id}")
        if not os.path.exists(meta.artifact_path):
            raise FileNotFoundError(f"Artifact missing: {meta.artifact_path}")

        self._logger.info("model_loading_from_disk", model_id=model_id, path=meta.artifact_path)

        model = self._load_artifact(meta)
        self._cache.put(model_id, model)
        return model

    def _load_artifact(self, meta: ModelMetadata) -> Any:
        """Dispatch to the appropriate loader based on model type.

        Args:
            meta: Model metadata record.

        Returns:
            Loaded model object.

        Raises:
            RuntimeError: If the model type is unsupported.
        """
        mtype = meta.model_type.lower()
        if mtype == ModelType.GAN.value:
            from generation_engine.models.gan_model import TabularGAN

            return TabularGAN.load(meta.artifact_path)
        elif mtype == ModelType.VAE.value:
            from generation_engine.models.vae_model import TabularVAE

            return TabularVAE.load_model(meta.artifact_path)
        elif mtype in (ModelType.STATISTICAL.value, ModelType.RULES.value):
            # Statistical and rules-based models are lightweight dicts
            with open(meta.artifact_path, "r") as fh:
                return json.load(fh)
        else:
            raise RuntimeError(f"Unsupported model type: {meta.model_type}")

    def update_status(
        self,
        model_id: str,
        new_status: str | ModelStatus,
    ) -> ModelMetadata:
        """Transition a model to a new lifecycle status.

        Args:
            model_id: Unique model identifier.
            new_status: Target status string or enum value.

        Returns:
            Updated :class:`ModelMetadata`.

        Raises:
            KeyError: If the model is not registered.
            ValueError: If the status transition is invalid.
        """
        meta = self._metadata_store.get(model_id)
        if meta is None:
            raise KeyError(f"No model registered with ID: {model_id}")

        if isinstance(new_status, ModelStatus):
            target = new_status.value
        else:
            target = new_status

        valid_transitions: Dict[str, list[str]] = {
            ModelStatus.REGISTERED.value: [ModelStatus.ACTIVE.value, ModelStatus.ARCHIVED.value],
            ModelStatus.ACTIVE.value: [ModelStatus.DEPRECATED.value, ModelStatus.ARCHIVED.value],
            ModelStatus.DEPRECATED.value: [ModelStatus.ARCHIVED.value, ModelStatus.ACTIVE.value],
            ModelStatus.ARCHIVED.value: [],
        }

        allowed = valid_transitions.get(meta.status, [])
        if target not in allowed:
            raise ValueError(
                f"Cannot transition from '{meta.status}' to '{target}'. "
                f"Allowed: {allowed}"
            )

        with self._lock:
            meta.status = target
            meta.updated_at = datetime.now(timezone.utc).isoformat()
            self._persist_metadata(meta)

        self._logger.info(
            "model_status_updated",
            model_id=model_id,
            new_status=target,
        )
        return meta

    def delete(self, model_id: str, remove_artifact: bool = False) -> None:
        """Remove a model from the registry.

        Args:
            model_id: Unique model identifier.
            remove_artifact: If ``True``, delete the artefact file as well.

        Raises:
            KeyError: If the model is not registered.
        """
        meta = self._metadata_store.get(model_id)
        if meta is None:
            raise KeyError(f"No model registered with ID: {model_id}")

        with self._lock:
            self._metadata_store.pop(model_id, None)
            self._cache.remove(model_id)

        if remove_artifact and os.path.exists(meta.artifact_path):
            try:
                if os.path.isdir(meta.artifact_path):
                    shutil.rmtree(meta.artifact_path)
                else:
                    os.remove(meta.artifact_path)
            except OSError as exc:
                self._logger.warning(
                    "artifact_removal_failed",
                    path=meta.artifact_path,
                    error=str(exc),
                )

        # Remove sidecar metadata
        sidecar_dir = os.path.join(self._base_path, model_id)
        if os.path.isdir(sidecar_dir):
            try:
                shutil.rmtree(sidecar_dir)
            except OSError:
                pass

        if self._mongo is not None:
            try:
                self._mongo.delete_one({"model_id": model_id})
            except Exception:
                pass

        self._logger.info(
            "model_deleted",
            model_id=model_id,
            artifact_removed=remove_artifact,
        )

    # ------------------------------------------------------------------
    # Query / listing
    # ------------------------------------------------------------------

    def list_models(
        self,
        *,
        model_type: str | None = None,
        status: str | None = None,
        schema_id: str | None = None,
        tags: list[str] | None = None,
        limit: int = 50,
        offset: int = 0,
    ) -> List[ModelMetadata]:
        """List registered models with optional filtering and pagination.

        Args:
            model_type: Filter by architecture type.
            status: Filter by lifecycle status.
            schema_id: Filter by target schema.
            tags: Filter by tags (any match).
            limit: Maximum results to return.
            offset: Number of results to skip.

        Returns:
            List of :class:`ModelMetadata` instances.
        """
        candidates = list(self._metadata_store.values())

        if model_type is not None:
            candidates = [m for m in candidates if m.model_type == model_type]
        if status is not None:
            candidates = [m for m in candidates if m.status == status]
        if schema_id is not None:
            candidates = [m for m in candidates if m.schema_id == schema_id]
        if tags:
            tag_set = set(tags)
            candidates = [m for m in candidates if tag_set.intersection(m.tags)]

        # Sort newest first
        candidates.sort(key=lambda m: m.created_at, reverse=True)
        return candidates[offset : offset + limit]

    def find_by_schema_and_method(
        self,
        schema_id: str,
        generation_method: str,
        *,
        status: str | None = ModelStatus.ACTIVE.value,
    ) -> ModelMetadata | None:
        """Find the best model for a given schema and method.

        Args:
            schema_id: Target ERP schema identifier.
            generation_method: Required generation method (``'gan'``, ``'vae'``, etc.).
            status: Filter by status; defaults to ``active``.

        Returns:
            Most recent matching :class:`ModelMetadata` or ``None``.
        """
        candidates = [
            m
            for m in self._metadata_store.values()
            if m.schema_id == schema_id
            and m.generation_method == generation_method
            and (status is None or m.status == status)
        ]
        if not candidates:
            return None
        candidates.sort(key=lambda m: m.created_at, reverse=True)
        return candidates[0]

    def update_metrics(
        self,
        model_id: str,
        metrics: Dict[str, Any],
    ) -> ModelMetadata:
        """Merge additional metrics into a model's metadata.

        Args:
            model_id: Unique model identifier.
            metrics: Metrics dictionary to merge.

        Returns:
            Updated :class:`ModelMetadata`.

        Raises:
            KeyError: If the model is not registered.
        """
        meta = self._metadata_store.get(model_id)
        if meta is None:
            raise KeyError(f"No model registered with ID: {model_id}")

        with self._lock:
            meta.metrics.update(metrics)
            meta.updated_at = datetime.now(timezone.utc).isoformat()
            self._persist_metadata(meta)

        self._logger.debug(
            "model_metrics_updated",
            model_id=model_id,
            new_metrics=list(metrics.keys()),
        )
        return meta

    # ------------------------------------------------------------------
    # Persistence helpers
    # ------------------------------------------------------------------

    def _persist_metadata(self, meta: ModelMetadata) -> None:
        """Persist metadata to MongoDB or sidecar JSON file.

        Args:
            meta: Model metadata to persist.
        """
        if self._mongo is not None:
            try:
                self._mongo.replace_one(
                    {"model_id": meta.model_id},
                    meta.to_dict(),
                    upsert=True,
                )
                return
            except Exception as exc:
                self._logger.warning(
                    "mongo_persist_failed",
                    model_id=meta.model_id,
                    error=str(exc),
                )

        # Fallback: sidecar JSON
        model_dir = os.path.join(self._base_path, meta.model_id)
        os.makedirs(model_dir, exist_ok=True)
        meta_path = os.path.join(model_dir, "metadata.json")
        try:
            with open(meta_path, "w") as fh:
                json.dump(meta.to_dict(), fh, indent=2, default=str)
        except Exception as exc:
            self._logger.error(
                "metadata_persist_failed",
                model_id=meta.model_id,
                error=str(exc),
            )

    # ------------------------------------------------------------------
    # Cache management
    # ------------------------------------------------------------------

    def clear_cache(self) -> None:
        """Evict all models from the in-memory cache."""
        self._cache.clear()
        self._logger.info("model_cache_cleared")

    @property
    def cache_size(self) -> int:
        """Return the current number of cached models."""
        return self._cache.size

    @property
    def registered_count(self) -> int:
        """Return the total number of registered models."""
        return len(self._metadata_store)
