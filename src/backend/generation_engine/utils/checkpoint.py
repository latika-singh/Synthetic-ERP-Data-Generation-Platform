"""Checkpoint and resume support for long-running generation jobs.

This module implements Redis-backed state checkpoints that enable job recovery
after failures such as process crashes, OOM kills, and network partitions.
The :class:`CheckpointManager` class serializes batch processing state (current
batch index, records generated, intermediate data buffers, generator state,
random seeds) to Redis with TTL-based automatic cleanup.

Integration Points:
    - The orchestrator's ``batch_processor`` calls ``save_checkpoint()`` after
      each batch completion.
    - On job startup, ``batch_processor`` calls ``load_checkpoint()`` to resume
      from the last successful batch.
    - Without this module, long-running generation jobs (potentially hours for
      millions of records) would restart from scratch on any failure.

Configuration:
    - ``CHECKPOINT_TTL``: 48-hour retention in Redis (172 800 seconds).
    - ``DEFAULT_CHECKPOINT_INTERVAL``: 60 seconds between successive saves.
    - ``MAX_CHECKPOINT_SIZE``: 50 MB ceiling on serialized state.

Usage::

    from generation_engine.utils.checkpoint import CheckpointManager

    # Initialise for a specific job
    mgr = CheckpointManager(job_id="job-abc-123", checkpoint_interval=60)

    # During batch processing loop
    for batch_idx in range(total_batches):
        records = generate_batch(batch_idx)
        if mgr.should_checkpoint():
            mgr.save_checkpoint({
                "batch_index": batch_idx,
                "records_generated": len(records),
                "total_records": target_count,
                "generator_state": generator.get_state(),
                "column_states": column_tracker.state,
                "relationship_state": fk_tracker.state,
            })

    # On job completion
    mgr.delete_checkpoint()

    # On job restart (in batch_processor)
    if mgr.has_checkpoint():
        state = mgr.load_checkpoint()
        resume_from_batch = state["batch_index"] + 1
"""

from __future__ import annotations

import base64
import binascii
import contextlib
import datetime
import hashlib
import json
import pickle
import time
import zlib
from dataclasses import asdict, dataclass, field
from typing import Any

import redis.exceptions

from shared.database.redis_client import get_redis_client
from shared.logging.structured_logger import get_logger


# ---------------------------------------------------------------------------
# Module-level logger
# ---------------------------------------------------------------------------
logger = get_logger(__name__)

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

CHECKPOINT_KEY_PREFIX: str = "job:checkpoint:"
"""Redis key prefix for all checkpoint state entries."""

CHECKPOINT_TTL: int = 172_800
"""Checkpoint time-to-live in seconds (48 hours).  Redis automatically
evicts checkpoint keys older than this to prevent unbounded memory growth."""

DEFAULT_CHECKPOINT_INTERVAL: int = 60
"""Default minimum interval in seconds between successive checkpoint
saves.  Prevents excessive Redis writes during tight batch loops."""

MAX_CHECKPOINT_SIZE: int = 50 * 1024 * 1024
"""Maximum allowed size (50 MB) for serialized checkpoint state.
Oversized checkpoints trigger a warning and intermediate data truncation."""

# Internal serialization format markers used to disambiguate JSON vs pickle
# payloads stored in Redis.
_FORMAT_JSON: str = "json"
_FORMAT_PICKLE: str = "pickle"


# ===========================================================================
# CheckpointState Dataclass
# ===========================================================================


@dataclass
class CheckpointState:
    """Typed schema for checkpoint data persisted to Redis.

    Represents the complete snapshot of a generation job's progress at a
    specific point in time.  All mutable container fields use ``field()``
    with ``default_factory`` to prevent shared-state bugs across instances.

    Attributes:
        job_id: Unique identifier of the generation job.
        batch_index: Current batch number (0-indexed).
        records_generated: Total records produced so far.
        total_records: Target total records for the job.
        generator_state: Serialized state of the active generator (random
            seeds, model weights snapshot references).  ``None`` when no
            generator state is captured.
        intermediate_data: Compressed intermediate data buffer (zlib).
            ``None`` when no intermediate data is buffered.
        column_states: Per-column generation state for maintaining
            statistical distributions across batches.
        relationship_state: Referential integrity tracking state containing
            foreign-key mappings across generated tables.
        created_at: ISO 8601 timestamp of checkpoint creation (UTC).
        checksum: SHA-256 hex digest for data integrity verification.
    """

    job_id: str
    batch_index: int
    records_generated: int
    total_records: int
    generator_state: dict[str, Any] | None = None
    intermediate_data: bytes | None = None
    column_states: dict[str, Any] = field(default_factory=dict)
    relationship_state: dict[str, Any] = field(default_factory=dict)
    created_at: str = ""
    checksum: str = ""


# ===========================================================================
# Private Helper Functions
# ===========================================================================


def _compute_checksum(data: str) -> str:
    """Compute a SHA-256 hex digest of the given string data.

    Used for tamper detection on checkpoint payloads.  The checksum is
    stored alongside the serialized state and re-verified on load.

    Args:
        data: The string data to hash (typically the serialized checkpoint
            payload *without* the checksum field populated).

    Returns:
        A 64-character lowercase hexadecimal digest string.
    """
    return hashlib.sha256(data.encode("utf-8")).hexdigest()


def _prepare_for_json(obj: Any) -> Any:
    """Recursively prepare a Python object for JSON serialization.

    Converts ``bytes`` values to base64-encoded strings wrapped in a type
    marker dictionary so they can be losslessly restored by
    :func:`_restore_from_json`.  Handles nested dicts and lists.

    Args:
        obj: Any Python object.  Dicts, lists, and bytes receive special
            handling; all other types pass through unchanged.

    Returns:
        A JSON-safe representation of *obj*.
    """
    if isinstance(obj, bytes):
        return {
            "__bytes__": True,
            "data": base64.b64encode(obj).decode("ascii"),
        }
    if isinstance(obj, dict):
        return {k: _prepare_for_json(v) for k, v in obj.items()}
    if isinstance(obj, (list, tuple)):
        return [_prepare_for_json(item) for item in obj]
    return obj


def _restore_from_json(obj: Any) -> Any:
    """Recursively restore Python objects from JSON-deserialized structures.

    Reverses the base64 encoding applied by :func:`_prepare_for_json` for
    ``bytes`` values.  Handles nested dicts and lists.

    Args:
        obj: A JSON-deserialized Python object potentially containing
            base64-encoded bytes markers.

    Returns:
        The object with bytes values restored.
    """
    if isinstance(obj, dict):
        if obj.get("__bytes__") is True:
            return base64.b64decode(obj["data"])
        return {k: _restore_from_json(v) for k, v in obj.items()}
    if isinstance(obj, list):
        return [_restore_from_json(item) for item in obj]
    return obj


def _serialize_state(state: dict[str, Any]) -> str:
    """Serialize a checkpoint state dictionary to a Redis-safe string.

    Attempts JSON serialization first for human-readability and
    cross-language compatibility.  Falls back to ``pickle`` + ``base64``
    encoding when the state contains non-JSON-serializable types such as
    NumPy arrays or PyTorch/TensorFlow model state dictionaries.

    The returned string is prefixed with a format marker (``json:`` or
    ``pickle:``) so that :func:`_deserialize_state` can select the
    correct decoder.

    Args:
        state: The checkpoint state dictionary.  May contain any picklable
            Python objects.

    Returns:
        A format-prefixed serialized string safe for Redis ``SET`` storage.

    Raises:
        ValueError: If serialization fails for both JSON and pickle.
    """
    # Prepare bytes/complex values for JSON safety.
    prepared = _prepare_for_json(state)

    try:
        json_str = json.dumps(prepared, sort_keys=True, default=str)
        return f"{_FORMAT_JSON}:{json_str}"
    except (TypeError, ValueError, OverflowError):
        # JSON serialization failed — fall back to pickle.
        logger.debug(
            "json_serialization_failed_using_pickle",
            reason="State contains non-JSON-serializable types",
        )

    # Pickle fallback for complex types (numpy arrays, model states).
    try:
        pickled_bytes = pickle.dumps(state, protocol=pickle.HIGHEST_PROTOCOL)
        encoded = base64.b64encode(pickled_bytes).decode("ascii")
        return f"{_FORMAT_PICKLE}:{encoded}"
    except (pickle.PicklingError, TypeError, OverflowError) as exc:
        raise ValueError(
            f"Failed to serialize checkpoint state: {exc}"
        ) from exc


def _deserialize_state(data: str) -> dict[str, Any]:
    """Deserialize a checkpoint state string back to a Python dictionary.

    Handles both JSON-prefixed and pickle-prefixed formats produced by
    :func:`_serialize_state`.

    Args:
        data: The format-prefixed serialized string retrieved from Redis.

    Returns:
        The deserialized checkpoint state dictionary.

    Raises:
        ValueError: If the data format is unrecognised or deserialization
            fails.
    """
    if data.startswith(f"{_FORMAT_JSON}:"):
        json_str = data[len(_FORMAT_JSON) + 1:]
        try:
            raw = json.loads(json_str)
            if not isinstance(raw, dict):
                raise ValueError(
                    f"JSON checkpoint root is not a dict: {type(raw)}"
                )
            restored: dict[str, Any] = _restore_from_json(raw)
            return restored
        except json.JSONDecodeError as exc:
            raise ValueError(
                f"Failed to deserialize JSON checkpoint: {exc}"
            ) from exc

    if data.startswith(f"{_FORMAT_PICKLE}:"):
        encoded = data[len(_FORMAT_PICKLE) + 1:]
        try:
            pickled_bytes = base64.b64decode(encoded)
            result = pickle.loads(pickled_bytes)  # noqa: S301 — trusted internal data
            if not isinstance(result, dict):
                raise ValueError(
                    f"Pickled checkpoint is not a dict: {type(result)}"
                )
            return result
        except (pickle.UnpicklingError, binascii.Error, TypeError) as exc:
            raise ValueError(
                f"Failed to deserialize pickled checkpoint: {exc}"
            ) from exc

    raise ValueError(
        f"Unknown checkpoint serialization format: {data[:30]!r}..."
    )


# ===========================================================================
# CheckpointManager Class
# ===========================================================================


class CheckpointManager:
    """Manages Redis-backed checkpoints for long-running generation jobs.

    Provides save/load/delete semantics with time-based throttling,
    SHA-256 integrity verification, and zlib compression of intermediate
    data buffers.  Designed for integration with the orchestrator's batch
    processor:

    1. On job startup, call :meth:`has_checkpoint` / :meth:`load_checkpoint`
       to detect and resume from a prior checkpoint.
    2. During the batch loop, call :meth:`should_checkpoint` before each
       potential save to respect the configured interval, then
       :meth:`save_checkpoint` to persist progress.
    3. On successful completion, call :meth:`delete_checkpoint` to free
       Redis memory.

    Thread Safety:
        All public methods are safe for concurrent use from multiple
        threads within the same process.  Redis operations are atomic or
        pipeline-batched.

    Args:
        job_id: Unique identifier of the generation job.
        checkpoint_interval: Minimum seconds between successive checkpoint
            saves.  Defaults to :data:`DEFAULT_CHECKPOINT_INTERVAL` (60 s).

    Example::

        mgr = CheckpointManager("job-abc-123", checkpoint_interval=30)
        if mgr.has_checkpoint():
            state = mgr.load_checkpoint()
            start_batch = state["batch_index"] + 1
        else:
            start_batch = 0
    """

    def __init__(
        self,
        job_id: str,
        checkpoint_interval: int = DEFAULT_CHECKPOINT_INTERVAL,
    ) -> None:
        self._job_id: str = job_id
        self._checkpoint_interval: int = checkpoint_interval
        self._redis = get_redis_client()
        self._logger = get_logger(__name__)
        self._checkpoint_key: str = f"{CHECKPOINT_KEY_PREFIX}{job_id}"
        self._last_checkpoint_time: float = 0.0
        self._checkpoint_count: int = 0

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def save_checkpoint(
        self,
        state: dict[str, Any],
        force: bool = False,
    ) -> bool:
        """Persist the current generation state to Redis.

        Applies time-based throttling to avoid excessive Redis writes.
        When *force* is ``False`` (default), the checkpoint is only saved
        if at least ``checkpoint_interval`` seconds have elapsed since the
        last save.

        The state dictionary **must** include the following keys:
        ``batch_index``, ``records_generated``, ``total_records``.

        Intermediate data (if provided under key ``intermediate_data``)
        is compressed with zlib before storage.  Complex types such as
        NumPy arrays are handled via a pickle fallback path.

        Args:
            state: A dictionary containing the batch processing state.
                Required keys: ``batch_index``, ``records_generated``,
                ``total_records``.  Optional keys: ``generator_state``,
                ``intermediate_data``, ``column_states``,
                ``relationship_state``.
            force: When ``True``, bypass the time-based throttle and save
                immediately.  Useful for forced checkpoints before risky
                operations.

        Returns:
            ``True`` if the checkpoint was saved, ``False`` if throttled
            or if a Redis error occurred.

        Raises:
            ValueError: If required keys are missing from *state*.
        """
        # Throttle check — skip if interval not elapsed and not forced.
        if not force and not self.should_checkpoint():
            return False

        # Validate required keys.
        required_keys = {"batch_index", "records_generated", "total_records"}
        missing = required_keys - set(state.keys())
        if missing:
            raise ValueError(
                f"Checkpoint state missing required keys: {missing}"
            )

        # Compress intermediate data if provided.
        compressed_intermediate: bytes | None = None
        intermediate_raw = state.get("intermediate_data")
        if intermediate_raw is not None:
            if isinstance(intermediate_raw, bytes):
                compressed_intermediate = zlib.compress(intermediate_raw)
            else:
                self._logger.warning(
                    "intermediate_data_not_bytes",
                    job_id=self._job_id,
                    actual_type=type(intermediate_raw).__name__,
                )

        # Build the CheckpointState instance.
        checkpoint = CheckpointState(
            job_id=self._job_id,
            batch_index=int(state["batch_index"]),
            records_generated=int(state["records_generated"]),
            total_records=int(state["total_records"]),
            generator_state=state.get("generator_state"),
            intermediate_data=compressed_intermediate,
            column_states=state.get("column_states", {}),
            relationship_state=state.get("relationship_state", {}),
            created_at=datetime.datetime.now(tz=datetime.UTC).isoformat(),
            checksum="",  # Populated after payload serialization.
        )

        # Convert to a plain dictionary for serialization.
        state_dict: dict[str, Any] = asdict(checkpoint)

        # Compute checksum over the payload with checksum field empty.
        state_dict["checksum"] = ""
        payload_for_checksum = _serialize_state(state_dict)
        state_dict["checksum"] = _compute_checksum(payload_for_checksum)

        # Final serialization with the checksum included.
        serialized = _serialize_state(state_dict)
        serialized_size = len(serialized.encode("utf-8"))

        # Size guard — truncate intermediate data if the checkpoint exceeds
        # the configured maximum to protect Redis memory.
        if serialized_size > MAX_CHECKPOINT_SIZE:
            self._logger.warning(
                "checkpoint_oversized_truncating_intermediate",
                job_id=self._job_id,
                size_bytes=serialized_size,
                max_bytes=MAX_CHECKPOINT_SIZE,
            )
            state_dict["intermediate_data"] = None
            # Recompute checksum after truncation.
            state_dict["checksum"] = ""
            payload_for_checksum = _serialize_state(state_dict)
            state_dict["checksum"] = _compute_checksum(payload_for_checksum)
            serialized = _serialize_state(state_dict)
            serialized_size = len(serialized.encode("utf-8"))

        # Atomic Redis write with TTL via pipeline.
        try:
            pipe = self._redis.pipeline(transaction=True)
            pipe.set(self._checkpoint_key, serialized)
            pipe.expire(self._checkpoint_key, CHECKPOINT_TTL)
            pipe.execute()
        except (
            redis.exceptions.RedisError,
            redis.exceptions.ConnectionError,
            redis.exceptions.TimeoutError,
        ) as exc:
            self._logger.error(
                "checkpoint_save_failed",
                job_id=self._job_id,
                batch_index=state["batch_index"],
                error=str(exc),
            )
            return False

        # Update internal counters on success.
        self._checkpoint_count += 1
        self._last_checkpoint_time = time.time()

        self._logger.debug(
            "checkpoint_saved",
            job_id=self._job_id,
            batch_index=state["batch_index"],
            records_generated=state["records_generated"],
            size_bytes=serialized_size,
            checkpoint_count=self._checkpoint_count,
        )
        return True

    def load_checkpoint(self) -> dict[str, Any] | None:
        """Load the most recent checkpoint from Redis and verify integrity.

        Returns ``None`` when no checkpoint exists (fresh start) or when
        the stored checkpoint fails integrity verification (the corrupt
        checkpoint is automatically deleted).

        Intermediate data is decompressed (zlib) before inclusion in the
        returned dictionary.

        Returns:
            The deserialized checkpoint state dictionary, or ``None`` if
            no valid checkpoint is available.
        """
        try:
            serialized: str | None = self._redis.get(self._checkpoint_key)  # type: ignore[assignment]
        except (
            redis.exceptions.RedisError,
            redis.exceptions.ConnectionError,
            redis.exceptions.TimeoutError,
        ) as exc:
            self._logger.error(
                "checkpoint_load_redis_error",
                job_id=self._job_id,
                error=str(exc),
            )
            return None

        if serialized is None:
            self._logger.debug(
                "no_checkpoint_found",
                job_id=self._job_id,
            )
            return None

        # Deserialize the stored payload.
        try:
            state_dict = _deserialize_state(serialized)
        except ValueError as exc:
            self._logger.error(
                "checkpoint_deserialization_failed",
                job_id=self._job_id,
                error=str(exc),
            )
            self._safe_delete()
            return None

        # Verify checksum integrity by recomputing the SHA-256 digest.
        stored_checksum: str = state_dict.get("checksum", "")
        state_dict["checksum"] = ""
        payload_for_checksum = _serialize_state(state_dict)
        computed_checksum = _compute_checksum(payload_for_checksum)

        if stored_checksum != computed_checksum:
            self._logger.error(
                "checkpoint_integrity_violation",
                job_id=self._job_id,
                stored_checksum=stored_checksum[:16] + "...",
                computed_checksum=computed_checksum[:16] + "...",
            )
            self._safe_delete()
            return None

        # Restore the verified checksum in the returned dictionary.
        state_dict["checksum"] = stored_checksum

        # Decompress intermediate data if present.
        intermediate = state_dict.get("intermediate_data")
        if intermediate is not None:
            try:
                if isinstance(intermediate, bytes):
                    state_dict["intermediate_data"] = zlib.decompress(
                        intermediate
                    )
                elif isinstance(intermediate, str):
                    # May arrive as base64 string via legacy storage path.
                    raw_bytes = base64.b64decode(intermediate)
                    state_dict["intermediate_data"] = zlib.decompress(raw_bytes)
            except (zlib.error, binascii.Error) as exc:
                self._logger.warning(
                    "intermediate_data_decompression_failed",
                    job_id=self._job_id,
                    error=str(exc),
                )
                state_dict["intermediate_data"] = None

        self._logger.info(
            "checkpoint_loaded",
            job_id=self._job_id,
            batch_index=state_dict.get("batch_index"),
            records_generated=state_dict.get("records_generated"),
            total_records=state_dict.get("total_records"),
        )
        return state_dict

    def has_checkpoint(self) -> bool:
        """Check whether a checkpoint exists in Redis for this job.

        Used by the batch processor on startup to decide between a fresh
        start and resuming from a prior checkpoint.

        Returns:
            ``True`` if a checkpoint key exists, ``False`` otherwise
            (including Redis communication failures, for safe default
            behaviour).
        """
        try:
            return bool(self._redis.exists(self._checkpoint_key))
        except (
            redis.exceptions.RedisError,
            redis.exceptions.ConnectionError,
            redis.exceptions.TimeoutError,
        ) as exc:
            self._logger.error(
                "checkpoint_exists_check_failed",
                job_id=self._job_id,
                error=str(exc),
            )
            return False

    def delete_checkpoint(self) -> bool:
        """Delete the checkpoint from Redis after successful job completion.

        Should be called when the generation job finishes successfully to
        free Redis memory.  Failed deletions are logged but do **not**
        raise exceptions — the TTL will eventually clean up the key.

        Returns:
            ``True`` if the checkpoint was deleted, ``False`` if not found
            or if deletion failed.
        """
        try:
            deleted_count = self._redis.delete(self._checkpoint_key)
            if deleted_count:
                self._logger.info(
                    "checkpoint_deleted",
                    job_id=self._job_id,
                    checkpoint_count=self._checkpoint_count,
                )
                return True
            self._logger.debug(
                "checkpoint_not_found_for_deletion",
                job_id=self._job_id,
            )
            return False
        except (
            redis.exceptions.RedisError,
            redis.exceptions.ConnectionError,
            redis.exceptions.TimeoutError,
        ) as exc:
            self._logger.error(
                "checkpoint_delete_failed",
                job_id=self._job_id,
                error=str(exc),
            )
            return False

    def get_checkpoint_metadata(self) -> dict[str, Any] | None:
        """Retrieve lightweight checkpoint metadata for status reporting.

        Returns only the essential metadata fields (no ``intermediate_data``
        or ``generator_state``) to minimise Redis transfer and
        deserialization overhead.  Suitable for progress-reporting APIs and
        monitoring dashboards.

        Returns:
            A dictionary with ``job_id``, ``batch_index``,
            ``records_generated``, ``total_records``, ``created_at``,
            and ``checkpoint_count`` keys, or ``None`` if no checkpoint
            exists.
        """
        try:
            serialized: str | None = self._redis.get(self._checkpoint_key)  # type: ignore[assignment]
        except (
            redis.exceptions.RedisError,
            redis.exceptions.ConnectionError,
            redis.exceptions.TimeoutError,
        ) as exc:
            self._logger.error(
                "checkpoint_metadata_redis_error",
                job_id=self._job_id,
                error=str(exc),
            )
            return None

        if serialized is None:
            return None

        try:
            state_dict = _deserialize_state(serialized)
        except ValueError as exc:
            self._logger.error(
                "checkpoint_metadata_deserialization_failed",
                job_id=self._job_id,
                error=str(exc),
            )
            return None

        return {
            "job_id": state_dict.get("job_id", self._job_id),
            "batch_index": state_dict.get("batch_index", 0),
            "records_generated": state_dict.get("records_generated", 0),
            "total_records": state_dict.get("total_records", 0),
            "created_at": state_dict.get("created_at", ""),
            "checkpoint_count": self._checkpoint_count,
        }

    def should_checkpoint(self) -> bool:
        """Determine whether enough time has elapsed for a new checkpoint.

        Compares the elapsed time since the last successful save against
        ``checkpoint_interval``.

        Returns:
            ``True`` if a checkpoint should be saved, ``False`` otherwise.
        """
        return (
            time.time() - self._last_checkpoint_time
        ) >= self._checkpoint_interval

    # ------------------------------------------------------------------
    # Private helpers
    # ------------------------------------------------------------------

    def _safe_delete(self) -> None:
        """Attempt to delete the checkpoint key, suppressing Redis errors.

        Used during error-recovery paths where a corrupt checkpoint must
        be removed but a secondary Redis failure should not mask the
        original error.
        """
        # Best-effort cleanup; the TTL will eventually expire the key.
        with contextlib.suppress(redis.exceptions.RedisError):
            self._redis.delete(self._checkpoint_key)


# ===========================================================================
# Module-Level Functions
# ===========================================================================


def get_checkpoint_info(job_id: str) -> dict[str, Any] | None:
    """Retrieve checkpoint metadata for any job without instantiating a manager.

    A lightweight helper intended for monitoring endpoints and admin APIs
    that need to inspect checkpoint state without creating a full
    :class:`CheckpointManager` instance.

    Args:
        job_id: The unique identifier of the generation job.

    Returns:
        A metadata dictionary with ``job_id``, ``batch_index``,
        ``records_generated``, ``total_records``, and ``created_at``
        keys, or ``None`` if no checkpoint exists.
    """
    checkpoint_key = f"{CHECKPOINT_KEY_PREFIX}{job_id}"

    try:
        client = get_redis_client()
        serialized: str | None = client.get(checkpoint_key)  # type: ignore[assignment]
    except (
        redis.exceptions.RedisError,
        redis.exceptions.ConnectionError,
        redis.exceptions.TimeoutError,
    ) as exc:
        logger.error(
            "get_checkpoint_info_redis_error",
            job_id=job_id,
            error=str(exc),
        )
        return None

    if serialized is None:
        return None

    try:
        state_dict = _deserialize_state(serialized)
    except ValueError as exc:
        logger.error(
            "get_checkpoint_info_deserialization_failed",
            job_id=job_id,
            error=str(exc),
        )
        return None

    return {
        "job_id": state_dict.get("job_id", job_id),
        "batch_index": state_dict.get("batch_index", 0),
        "records_generated": state_dict.get("records_generated", 0),
        "total_records": state_dict.get("total_records", 0),
        "created_at": state_dict.get("created_at", ""),
    }


def cleanup_expired_checkpoints(max_age_hours: int = 48) -> int:
    """Scan Redis for expired checkpoint keys and delete them.

    Uses ``SCAN`` (not ``KEYS``) to iterate through matching keys without
    blocking the Redis server on large key spaces.  Keys with no TTL set
    (orphans) or whose stored timestamps exceed *max_age_hours* are
    deleted.

    Intended for periodic maintenance tasks (e.g., a scheduled Kubernetes
    CronJob).

    Args:
        max_age_hours: Maximum age in hours before a checkpoint is
            considered expired.  Defaults to 48 hours (matching
            :data:`CHECKPOINT_TTL`).

    Returns:
        The number of checkpoint keys deleted.
    """
    deleted_count: int = 0
    max_age_seconds = max_age_hours * 3600
    cutoff_time = datetime.datetime.now(tz=datetime.UTC) - datetime.timedelta(
        seconds=max_age_seconds
    )
    scan_pattern = f"{CHECKPOINT_KEY_PREFIX}*"

    try:
        client = get_redis_client()
        cursor: int = 0

        while True:
            cursor, keys = client.scan(  # type: ignore[misc]
                cursor=cursor,
                match=scan_pattern,
                count=100,
            )

            for key in keys:
                should_delete = False

                # Check whether the key has a TTL.
                ttl_value = client.ttl(key)
                if ttl_value == -1:
                    # No TTL set — likely an orphaned checkpoint; delete.
                    should_delete = True
                elif ttl_value == -2:
                    # Key expired between SCAN and TTL check; skip.
                    continue

                # If the key has a TTL but may still be stale, inspect the
                # embedded timestamp for a secondary age check.
                if not should_delete:
                    try:
                        raw = client.get(key)
                        if raw is not None:
                            state = _deserialize_state(raw)  # type: ignore[arg-type]
                            created_at_str = state.get("created_at", "")
                            if created_at_str:
                                created_at = datetime.datetime.fromisoformat(
                                    created_at_str
                                )
                                if created_at < cutoff_time:
                                    should_delete = True
                    except (ValueError, KeyError, TypeError):
                        # Unparseable checkpoint — treat as expired.
                        should_delete = True

                if should_delete:
                    client.delete(key)
                    deleted_count += 1
                    logger.debug(
                        "expired_checkpoint_cleaned",
                        key=str(key),
                    )

            # A cursor value of 0 signals that the full key space has been
            # traversed.
            if cursor == 0:
                break

    except (
        redis.exceptions.RedisError,
        redis.exceptions.ConnectionError,
        redis.exceptions.TimeoutError,
    ) as exc:
        logger.error(
            "cleanup_expired_checkpoints_failed",
            error=str(exc),
            deleted_so_far=deleted_count,
        )

    logger.info(
        "checkpoint_cleanup_completed",
        deleted_count=deleted_count,
        max_age_hours=max_age_hours,
    )
    return deleted_count
