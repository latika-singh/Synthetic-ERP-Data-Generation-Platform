"""Utility package for the Generation Engine service.

Provides cross-cutting operational support for the Generation Engine,
including real-time progress tracking and checkpoint/resume capabilities
for long-running synthetic data generation jobs.

This package exposes two primary classes and three convenience functions
as its public API:

Classes:
    ProgressTracker: Redis-backed real-time progress reporter that publishes
        completion percentages, throughput metrics, and estimated remaining
        time over Redis Pub/Sub channels.  Consumed by the orchestrator's
        ``batch_processor`` during generation loops and by the API Gateway
        for Job Monitoring screen (S-004) status polling.

    CheckpointManager: Redis-backed checkpoint/resume manager that persists
        batch processing state (batch index, records generated, generator
        state, relationship mappings) to Redis with SHA-256 integrity
        verification and zlib compression.  Enables recovery of long-running
        jobs after process crashes or OOM kills without restarting from
        scratch.

Functions:
    get_job_progress: Retrieve the current progress state for any generation
        job without instantiating a ``ProgressTracker``.  Designed for use
        by monitoring endpoints and admin APIs.

    get_checkpoint_info: Retrieve lightweight checkpoint metadata for any
        generation job without instantiating a ``CheckpointManager``.
        Suitable for progress-reporting APIs and monitoring dashboards.

    cancel_job_progress: Mark a running generation job as cancelled in Redis
        and publish a cancellation event to the progress Pub/Sub channel.

Typical usage from the orchestrator's batch processor::

    from generation_engine.utils import (
        CheckpointManager,
        ProgressTracker,
    )

    tracker = ProgressTracker(job_id="job-abc-123", total_records=100_000)
    tracker.start()

    mgr = CheckpointManager(job_id="job-abc-123", checkpoint_interval=60)
    if mgr.has_checkpoint():
        state = mgr.load_checkpoint()
        resume_batch = state["batch_index"] + 1

    for batch in batches:
        records = generate_batch(batch)
        tracker.update(records_in_batch=len(records))
        if mgr.should_checkpoint():
            mgr.save_checkpoint({"batch_index": batch.index, ...})

    tracker.complete(quality_score=0.97)
    mgr.delete_checkpoint()

Typical usage from the API Gateway for status queries::

    from generation_engine.utils import (
        cancel_job_progress,
        get_checkpoint_info,
        get_job_progress,
    )

    progress = get_job_progress("job-abc-123")
    checkpoint = get_checkpoint_info("job-abc-123")
    cancelled = cancel_job_progress("job-abc-123")
"""

from __future__ import annotations

from generation_engine.utils.checkpoint import (
    CheckpointManager,
    get_checkpoint_info,
)
from generation_engine.utils.progress_tracker import (
    ProgressTracker,
    cancel_job_progress,
    get_job_progress,
)


__all__: list[str] = [
    "CheckpointManager",
    "ProgressTracker",
    "cancel_job_progress",
    "get_checkpoint_info",
    "get_job_progress",
]
