"""Redis-backed real-time progress reporting for synthetic data generation jobs.

This module provides the :class:`ProgressTracker` class and companion helper
functions that enable real-time progress visibility for long-running generation
jobs.  Progress state is persisted in Redis hash keys and change events are
published over Redis Pub/Sub channels so that the API Gateway can relay them to
the Web Console's Job Monitoring screen (S-004) via WebSocket connections.

Key capabilities:

- **Percentage completion & record counts** — stored in a Redis hash and
  published after every batch so the Job Monitoring screen can render live
  progress bars.
- **Throughput measurement** -- records/second computed per-batch with an
  Exponential Moving Average (EMA, alpha = 0.3) to smooth out burst noise.
- **Estimated time remaining** — derived from the smoothed throughput and
  remaining record count.
- **Update throttling** — Redis Pub/Sub publications are rate-limited to at
  most one event per ``update_interval`` seconds (default 5 s) to prevent
  channel flooding during high-throughput generation.
- **Automatic TTL-based cleanup** — completed or failed progress keys are
  automatically expired after :data:`PROGRESS_TTL` (24 h) so stale state
  does not accumulate indefinitely.
- **Graceful degradation** — all Redis operations are wrapped in exception
  handlers so that a transient Redis outage logs a warning but does *not*
  crash the generation job.

Channel naming convention::

    job:progress:<job_id>          — Pub/Sub channel for progress events
    job:progress:state:<job_id>    — Redis hash storing current progress state

Usage::

    from generation_engine.utils.progress_tracker import ProgressTracker

    tracker = ProgressTracker(job_id="job-abc-123", total_records=100_000)
    tracker.start()

    for batch in batches:
        records = generate_batch(batch)
        tracker.update(records_in_batch=len(records))

    tracker.complete(quality_score=0.97)

Consumed by:
    - ``generation_engine.orchestrator.batch_processor`` for real-time status
      visibility during batch generation loops.
    - Module-level helpers ``get_job_progress`` / ``cancel_job_progress`` are
      used by the API Gateway to query or cancel progress without
      instantiating a tracker.
"""

from __future__ import annotations

import datetime
import json
import math
import time
from typing import Any

import redis.exceptions

from shared.database.redis_client import (
    create_progress_channel,
    get_redis_client,
    publish_progress,
)
from shared.logging.structured_logger import get_logger


# ---------------------------------------------------------------------------
# Module-level constants
# ---------------------------------------------------------------------------

PROGRESS_KEY_PREFIX: str = "job:progress:state:"
"""Redis key prefix for progress state hashes.

Each generation job stores its current progress in a Redis hash at
``job:progress:state:<job_id>``.
"""

PROGRESS_CHANNEL_PREFIX: str = "job:progress:"
"""Redis Pub/Sub channel prefix for live progress events.

Events are published to ``job:progress:<job_id>`` and consumed by the API
Gateway's WebSocket relay.
"""

PROGRESS_TTL: int = 86_400
"""Time-to-live (in seconds) for completed/failed progress state.

Defaults to 24 hours (86 400 s).  After this period Redis automatically
removes the key so stale progress data does not accumulate.
"""

DEFAULT_UPDATE_INTERVAL: int = 5
"""Minimum interval (in seconds) between consecutive Redis Pub/Sub
publications.  Prevents flooding the channel during high-throughput
generation where ``update()`` may be called every few milliseconds.
"""

# ---------------------------------------------------------------------------
# Module-level logger (structlog BoundLogger)
# ---------------------------------------------------------------------------

logger = get_logger(__name__)

# ---------------------------------------------------------------------------
# Internal constants
# ---------------------------------------------------------------------------

_EMA_ALPHA: float = 0.3
"""Exponential moving average smoothing factor for throughput calculation.

A value of 0.3 gives ~70 % weight to the historical average and 30 % to
the most recent sample, providing responsive yet stable estimates.
"""

_MAX_THROUGHPUT_SAMPLES: int = 20
"""Maximum number of throughput samples retained for the EMA window."""


# ============================================================================
# ProgressTracker class
# ============================================================================


class ProgressTracker:
    """Redis-backed real-time progress tracker for a single generation job.

    Manages the full lifecycle of progress reporting — start, update,
    complete, and fail — by persisting state in a Redis hash and publishing
    JSON events to a Redis Pub/Sub channel.

    Thread safety is achieved via Redis atomic commands (``HSET``,
    ``EXPIRE``, ``PUBLISH``).  Multiple processes may safely read progress
    via ``get_progress()`` without contention.

    Args:
        job_id: Unique identifier for the generation job.
        total_records: Target number of records to generate.  Must be ≥ 1.
        update_interval: Minimum seconds between Redis Pub/Sub publications.
            Defaults to :data:`DEFAULT_UPDATE_INTERVAL` (5 s).

    Raises:
        ValueError: If *total_records* is less than 1.

    Example::

        tracker = ProgressTracker("job-xyz", total_records=50_000)
        tracker.start()
        tracker.update(records_in_batch=10_000)
        tracker.update(records_in_batch=10_000)
        result = tracker.complete(quality_score=0.96)
    """

    # ------------------------------------------------------------------ #
    # Construction
    # ------------------------------------------------------------------ #

    def __init__(
        self,
        job_id: str,
        total_records: int,
        update_interval: int = DEFAULT_UPDATE_INTERVAL,
    ) -> None:
        if total_records < 1:
            raise ValueError(
                f"total_records must be >= 1, got {total_records}"
            )

        self._job_id: str = job_id
        self._total_records: int = total_records
        self._update_interval: int = max(update_interval, 1)

        # Mutable runtime state
        self._records_generated: int = 0
        self._start_time: float | None = None
        self._last_update_time: float = 0.0
        self._last_batch_time: float = 0.0
        self._throughput_samples: list[float] = []

        # Redis connection & channel
        self._redis = get_redis_client()
        self._logger = get_logger(__name__)
        self._progress_key: str = f"{PROGRESS_KEY_PREFIX}{job_id}"
        self._channel: str = (
            create_progress_channel(job_id)
            or f"{PROGRESS_CHANNEL_PREFIX}{job_id}"
        )

    # ------------------------------------------------------------------ #
    # Public lifecycle methods
    # ------------------------------------------------------------------ #

    def start(self) -> None:
        """Record the job start and publish an initial progress event.

        Stores the baseline progress state (0 records, 0 % complete) in a
        Redis hash and publishes a ``started`` event to the progress channel.
        Must be called exactly once before any calls to :meth:`update`.
        """
        self._start_time = time.time()
        self._last_batch_time = self._start_time
        self._last_update_time = self._start_time
        timestamp: str = datetime.datetime.now(tz=datetime.UTC).isoformat().replace("+00:00", "Z")

        # Baseline progress state stored as a Redis hash.  All values are
        # JSON-encoded strings so that retrieval via HGETALL + json.loads()
        # restores the original Python types losslessly.
        initial_state: dict[str, str] = {
            "job_id": json.dumps(self._job_id),
            "status": json.dumps("generating"),
            "total_records": json.dumps(self._total_records),
            "records_generated": json.dumps(0),
            "percentage": json.dumps(0.0),
            "start_time": json.dumps(timestamp),
            "estimated_remaining_seconds": json.dumps(None),
            "throughput_records_per_second": json.dumps(0.0),
        }

        try:
            self._redis.hset(self._progress_key, mapping=initial_state)
            publish_progress(
                self._channel,
                {
                    "event": "started",
                    "job_id": self._job_id,
                    "total_records": self._total_records,
                    "timestamp": timestamp,
                },
            )
        except (
            redis.exceptions.ConnectionError,
            redis.exceptions.TimeoutError,
            redis.exceptions.RedisError,
        ) as exc:
            self._logger.warning(
                "redis_start_event_failed",
                job_id=self._job_id,
                error=str(exc),
            )

        self._logger.info(
            "Generation job started",
            job_id=self._job_id,
            total_records=self._total_records,
        )

    def update(
        self,
        records_in_batch: int,
        batch_metadata: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        """Record a completed batch and optionally publish a progress event.

        Local state (record count, throughput samples) is **always** updated.
        A Redis hash update and Pub/Sub publication occur only when at least
        ``update_interval`` seconds have elapsed since the last publication,
        implementing the throttling contract.

        Args:
            records_in_batch: Number of records successfully generated in the
                most recent batch.
            batch_metadata: Optional free-form dictionary carrying
                batch-specific context (e.g. table name, batch index) that is
                forwarded verbatim in the published event.

        Returns:
            A dictionary containing the latest computed progress metrics::

                {
                    "job_id": "...",
                    "records_generated": 30000,
                    "total_records": 100000,
                    "percentage": 30.0,
                    "throughput_records_per_second": 5200.3,
                    "estimated_remaining_seconds": 13,
                    "elapsed_seconds": 5.78,
                    "batch_metadata": {...} | None,
                }
        """
        current_time: float = time.time()

        # ----- Accumulate record count -------------------------------- #
        self._records_generated += records_in_batch

        # ----- Percentage --------------------------------------------- #
        percentage: float = min(
            100.0,
            (self._records_generated / self._total_records) * 100.0,
        )

        # ----- Per-batch throughput ----------------------------------- #
        batch_duration: float = current_time - self._last_batch_time
        if batch_duration > 0:
            current_throughput: float = records_in_batch / batch_duration
            if math.isfinite(current_throughput) and current_throughput >= 0:
                self._throughput_samples.append(current_throughput)
                # Trim to sliding window size
                if len(self._throughput_samples) > _MAX_THROUGHPUT_SAMPLES:
                    self._throughput_samples = self._throughput_samples[
                        -_MAX_THROUGHPUT_SAMPLES:
                    ]
        self._last_batch_time = current_time

        # ----- Smoothed (EMA) throughput ------------------------------ #
        smoothed_throughput: float = self._compute_ema_throughput()

        # ----- Estimated remaining time ------------------------------- #
        remaining_records: int = self._total_records - self._records_generated
        estimated_remaining: float | None = None
        if smoothed_throughput > 0 and remaining_records > 0:
            raw_estimate: float = remaining_records / smoothed_throughput
            if math.isfinite(raw_estimate):
                estimated_remaining = round(raw_estimate, 0)

        # ----- Elapsed time ------------------------------------------- #
        elapsed_time: float = current_time - (self._start_time or current_time)

        # ----- Build return dict -------------------------------------- #
        progress: dict[str, Any] = {
            "job_id": self._job_id,
            "records_generated": self._records_generated,
            "total_records": self._total_records,
            "percentage": round(percentage, 2),
            "throughput_records_per_second": round(smoothed_throughput, 1),
            "estimated_remaining_seconds": estimated_remaining,
            "elapsed_seconds": round(elapsed_time, 2),
            "batch_metadata": batch_metadata,
        }

        # ----- Throttled Redis update --------------------------------- #
        if (current_time - self._last_update_time) >= self._update_interval:
            self._publish_progress_update(
                percentage=percentage,
                smoothed_throughput=smoothed_throughput,
                estimated_remaining=estimated_remaining,
                batch_metadata=batch_metadata,
            )
            self._last_update_time = current_time

        return progress

    def complete(
        self,
        quality_score: float | None = None,
        output_metadata: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        """Mark the job as completed and publish a completion event.

        Updates the Redis hash with final metrics, sets a TTL for automatic
        cleanup, and publishes a ``completed`` event.

        Args:
            quality_score: Optional composite quality score (0.0 - 1.0)
                computed by the Quality Service.
            output_metadata: Optional dictionary describing generated output
                (file paths, row counts per table, etc.).

        Returns:
            A summary dictionary with final timing and throughput metrics.
        """
        end_time: float = time.time()
        total_duration: float = end_time - (self._start_time or end_time)
        average_throughput: float = (
            self._records_generated / total_duration
            if total_duration > 0
            else 0.0
        )
        timestamp: str = datetime.datetime.now(tz=datetime.UTC).isoformat().replace("+00:00", "Z")

        summary: dict[str, Any] = {
            "job_id": self._job_id,
            "status": "completed",
            "records_generated": self._records_generated,
            "total_records": self._total_records,
            "percentage": 100.0,
            "total_duration_seconds": round(total_duration, 2),
            "average_throughput": round(average_throughput, 1),
            "quality_score": quality_score,
            "output_metadata": output_metadata,
            "completed_at": timestamp,
        }

        try:
            # Persist final state in the Redis hash.
            self._redis.hset(
                self._progress_key,
                mapping={
                    "status": json.dumps("completed"),
                    "percentage": json.dumps(100.0),
                    "records_generated": json.dumps(self._records_generated),
                    "total_duration_seconds": json.dumps(
                        round(total_duration, 2)
                    ),
                    "average_throughput": json.dumps(
                        round(average_throughput, 1)
                    ),
                    "quality_score": json.dumps(quality_score),
                    "completed_at": json.dumps(timestamp),
                },
            )
            # Set TTL so the key is automatically evicted after 24 h.
            self._redis.expire(self._progress_key, PROGRESS_TTL)

            publish_progress(
                self._channel,
                {
                    "event": "completed",
                    "job_id": self._job_id,
                    "records_generated": self._records_generated,
                    "total_duration_seconds": round(total_duration, 2),
                    "average_throughput": round(average_throughput, 1),
                    "quality_score": quality_score,
                    "output_metadata": output_metadata,
                    "timestamp": timestamp,
                },
            )
        except (
            redis.exceptions.ConnectionError,
            redis.exceptions.TimeoutError,
            redis.exceptions.RedisError,
        ) as exc:
            self._logger.warning(
                "redis_completion_event_failed",
                job_id=self._job_id,
                error=str(exc),
            )

        self._logger.info(
            "Generation job completed",
            job_id=self._job_id,
            records_generated=self._records_generated,
            total_duration_seconds=round(total_duration, 2),
            average_throughput=round(average_throughput, 1),
            quality_score=quality_score,
        )

        return summary

    def fail(
        self,
        error: str,
        error_code: str | None = None,
    ) -> dict[str, Any]:
        """Mark the job as failed and publish a failure event.

        Records the error details in the Redis hash, sets a TTL for cleanup,
        and publishes a ``failed`` event.

        Args:
            error: Human-readable error description.
            error_code: Optional machine-readable error code for programmatic
                error handling (e.g. ``"GEN_TIMEOUT"``, ``"INTEGRITY_ERR"``).

        Returns:
            A failure summary dictionary including elapsed time and the
            number of records generated before the failure.
        """
        failure_time: float = time.time()
        elapsed: float = (
            failure_time - self._start_time if self._start_time else 0.0
        )
        timestamp: str = datetime.datetime.now(tz=datetime.UTC).isoformat().replace("+00:00", "Z")

        failure_summary: dict[str, Any] = {
            "job_id": self._job_id,
            "status": "failed",
            "error": error,
            "error_code": error_code,
            "records_generated": self._records_generated,
            "elapsed_seconds": round(elapsed, 2),
            "failed_at": timestamp,
        }

        try:
            self._redis.hset(
                self._progress_key,
                mapping={
                    "status": json.dumps("failed"),
                    "error": json.dumps(error),
                    "error_code": json.dumps(error_code),
                    "failed_at": json.dumps(timestamp),
                    "records_generated_at_failure": json.dumps(
                        self._records_generated
                    ),
                },
            )
            self._redis.expire(self._progress_key, PROGRESS_TTL)

            publish_progress(
                self._channel,
                {
                    "event": "failed",
                    "job_id": self._job_id,
                    "error": error,
                    "error_code": error_code,
                    "records_generated": self._records_generated,
                    "elapsed_seconds": round(elapsed, 2),
                    "timestamp": timestamp,
                },
            )
        except (
            redis.exceptions.ConnectionError,
            redis.exceptions.TimeoutError,
            redis.exceptions.RedisError,
        ) as exc:
            self._logger.warning(
                "redis_failure_event_failed",
                job_id=self._job_id,
                error=str(exc),
            )

        self._logger.error(
            "Generation job failed",
            job_id=self._job_id,
            error=error,
            error_code=error_code,
            records_generated=self._records_generated,
            elapsed_seconds=round(elapsed, 2),
        )

        return failure_summary

    def get_progress(self) -> dict[str, Any] | None:
        """Read the current progress state from the Redis hash.

        Returns:
            A dictionary with the latest progress fields, or ``None`` if the
            key does not exist (e.g. the job has not started or the TTL has
            expired).
        """
        try:
            raw: dict[str, str] = self._redis.hgetall(self._progress_key)  # type: ignore[assignment]
            if not raw:
                return None
            return _deserialize_progress_hash(raw)
        except (
            redis.exceptions.ConnectionError,
            redis.exceptions.TimeoutError,
            redis.exceptions.RedisError,
        ) as exc:
            self._logger.warning(
                "redis_get_progress_failed",
                job_id=self._job_id,
                error=str(exc),
            )
            return None

    # ------------------------------------------------------------------ #
    # Private helpers
    # ------------------------------------------------------------------ #

    def _compute_ema_throughput(self) -> float:
        """Compute the Exponential Moving Average of throughput samples.

        Uses a smoothing factor of :data:`_EMA_ALPHA` (0.3) over the most
        recent :data:`_MAX_THROUGHPUT_SAMPLES` (20) samples.

        Returns:
            The smoothed throughput in records/second, or ``0.0`` when no
            samples are available.
        """
        if not self._throughput_samples:
            return 0.0

        ema: float = self._throughput_samples[0]
        for sample in self._throughput_samples[1:]:
            ema = _EMA_ALPHA * sample + (1.0 - _EMA_ALPHA) * ema

        return ema if math.isfinite(ema) else 0.0

    def _publish_progress_update(
        self,
        percentage: float,
        smoothed_throughput: float,
        estimated_remaining: float | None,
        batch_metadata: dict[str, Any] | None,
    ) -> None:
        """Persist current progress in Redis and publish a ``progress`` event.

        Called by :meth:`update` only when the throttle interval has elapsed.
        Separated into its own method for readability and to isolate error
        handling from the main progress accumulation logic.

        Args:
            percentage: Current completion percentage (0.0 - 100.0).
            smoothed_throughput: EMA-smoothed throughput (records/second).
            estimated_remaining: Estimated seconds to completion, or ``None``.
            batch_metadata: Optional caller-supplied batch context.
        """
        timestamp: str = datetime.datetime.now(tz=datetime.UTC).isoformat().replace("+00:00", "Z")

        try:
            # Atomic hash update for the mutable progress fields.
            self._redis.hset(
                self._progress_key,
                mapping={
                    "records_generated": json.dumps(self._records_generated),
                    "percentage": json.dumps(round(percentage, 2)),
                    "throughput_records_per_second": json.dumps(
                        round(smoothed_throughput, 1)
                    ),
                    "estimated_remaining_seconds": json.dumps(
                        estimated_remaining
                    ),
                    "last_updated": json.dumps(timestamp),
                },
            )

            publish_progress(
                self._channel,
                {
                    "event": "progress",
                    "job_id": self._job_id,
                    "records_generated": self._records_generated,
                    "total_records": self._total_records,
                    "percentage": round(percentage, 2),
                    "throughput_records_per_second": round(
                        smoothed_throughput, 1
                    ),
                    "estimated_remaining_seconds": estimated_remaining,
                    "batch_metadata": batch_metadata,
                    "timestamp": timestamp,
                },
            )
        except (
            redis.exceptions.ConnectionError,
            redis.exceptions.TimeoutError,
            redis.exceptions.RedisError,
        ) as exc:
            self._logger.warning(
                "redis_progress_update_failed",
                job_id=self._job_id,
                error=str(exc),
            )


# ============================================================================
# Module-level helper functions
# ============================================================================


def get_job_progress(job_id: str) -> dict[str, Any] | None:
    """Retrieve the current progress state for a generation job.

    This is a convenience function that reads the Redis hash directly
    without requiring a :class:`ProgressTracker` instance.  It is designed
    for use by the API Gateway when the Job Monitoring screen polls for
    the latest status of a running or recently completed job.

    Args:
        job_id: The unique generation job identifier.

    Returns:
        A dictionary with the latest progress fields, or ``None`` if no
        progress state exists for the given *job_id*.

    Example::

        progress = get_job_progress("job-abc-123")
        if progress and progress.get("status") == "generating":
            print(f"{progress['percentage']}% complete")
    """
    try:
        client = get_redis_client()
        progress_key: str = f"{PROGRESS_KEY_PREFIX}{job_id}"
        raw: dict[str, str] = client.hgetall(progress_key)  # type: ignore[assignment]
        if not raw:
            return None
        return _deserialize_progress_hash(raw)
    except (
        redis.exceptions.ConnectionError,
        redis.exceptions.TimeoutError,
        redis.exceptions.RedisError,
    ) as exc:
        logger.warning(
            "redis_get_job_progress_failed",
            job_id=job_id,
            error=str(exc),
        )
        return None


def cancel_job_progress(job_id: str) -> bool:
    """Mark a generation job as cancelled in Redis and publish a cancel event.

    Updates the progress hash with a ``cancelled`` status, sets a TTL for
    automatic cleanup, and publishes a ``cancelled`` event to the progress
    channel so that listening WebSocket consumers are notified.

    Args:
        job_id: The unique generation job identifier to cancel.

    Returns:
        ``True`` if the job progress was found and successfully marked as
        cancelled, ``False`` if the progress key does not exist or a Redis
        error occurred.

    Example::

        if cancel_job_progress("job-abc-123"):
            print("Job cancelled successfully")
    """
    try:
        client = get_redis_client()
        progress_key: str = f"{PROGRESS_KEY_PREFIX}{job_id}"

        # Verify the progress key exists before attempting to update.
        if not client.exists(progress_key):
            logger.info(
                "cancel_job_progress_key_not_found",
                job_id=job_id,
            )
            return False

        timestamp: str = datetime.datetime.now(tz=datetime.UTC).isoformat().replace("+00:00", "Z")

        # Atomically update the status and cancellation timestamp.
        client.hset(
            progress_key,
            mapping={
                "status": json.dumps("cancelled"),
                "cancelled_at": json.dumps(timestamp),
            },
        )

        # Set TTL for eventual cleanup.
        client.expire(progress_key, PROGRESS_TTL)

        # Publish a cancellation event to the Pub/Sub channel.
        channel: str = (
            create_progress_channel(job_id)
            or f"{PROGRESS_CHANNEL_PREFIX}{job_id}"
        )
        publish_progress(
            channel,
            {
                "event": "cancelled",
                "job_id": job_id,
                "timestamp": timestamp,
            },
        )

        logger.info("Generation job cancelled", job_id=job_id)
        return True

    except (
        redis.exceptions.ConnectionError,
        redis.exceptions.TimeoutError,
        redis.exceptions.RedisError,
    ) as exc:
        logger.error(
            "redis_cancel_job_progress_failed",
            job_id=job_id,
            error=str(exc),
        )
        return False


# ============================================================================
# Internal deserialization helper
# ============================================================================


def _deserialize_progress_hash(
    raw: dict[str, str],
) -> dict[str, Any]:
    """Deserialize a Redis hash of JSON-encoded values into native Python types.

    Each value stored via ``json.dumps()`` in :meth:`ProgressTracker.start`,
    :meth:`ProgressTracker.update`, :meth:`ProgressTracker.complete`, and
    :meth:`ProgressTracker.fail` is decoded back to its original Python type.
    Values that cannot be JSON-decoded (e.g. plain strings stored by older
    code paths) are returned as-is.

    Args:
        raw: Mapping of ``{field: json_encoded_value}`` as returned by
            ``redis.hgetall()``.

    Returns:
        A dictionary with each value decoded from JSON.
    """
    result: dict[str, Any] = {}
    for key, value in raw.items():
        try:
            result[key] = json.loads(value)
        except (json.JSONDecodeError, TypeError):
            # Fallback: return the raw string if it isn't valid JSON.
            result[key] = value
    return result
