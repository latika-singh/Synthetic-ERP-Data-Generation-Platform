"""Generation Engine utility modules.

Provides cross-cutting utility functionality shared across the Generation
Engine's orchestrator, generators, and formatters:

- **Checkpoint management** — Redis-backed checkpoint/resume support for
  long-running generation jobs via :class:`~.checkpoint.CheckpointManager`.
- **Progress tracking** — Real-time Redis pub/sub progress reporting via
  :class:`~.progress_tracker.ProgressTracker`.

Usage::

    from generation_engine.utils.checkpoint import CheckpointManager
    from generation_engine.utils.progress_tracker import ProgressTracker

    mgr = CheckpointManager("job-abc-123")
    if mgr.has_checkpoint():
        state = mgr.load_checkpoint()

    tracker = ProgressTracker(job_id="job-abc-123", total_records=100_000)
    tracker.start()
"""

from __future__ import annotations


__all__: list[str] = []

# Lazy imports to tolerate partial builds during parallel agent generation.
try:
    from .checkpoint import (
        CHECKPOINT_KEY_PREFIX,
        CHECKPOINT_TTL,
        DEFAULT_CHECKPOINT_INTERVAL,
        MAX_CHECKPOINT_SIZE,
        CheckpointManager,
        CheckpointState,
        cleanup_expired_checkpoints,
        get_checkpoint_info,
    )

    __all__.extend([
        "CHECKPOINT_KEY_PREFIX",
        "CHECKPOINT_TTL",
        "DEFAULT_CHECKPOINT_INTERVAL",
        "MAX_CHECKPOINT_SIZE",
        "CheckpointManager",
        "CheckpointState",
        "cleanup_expired_checkpoints",
        "get_checkpoint_info",
    ])
except ImportError:  # pragma: no cover
    pass

try:
    from .progress_tracker import (
        DEFAULT_UPDATE_INTERVAL,
        PROGRESS_CHANNEL_PREFIX,
        PROGRESS_KEY_PREFIX,
        PROGRESS_TTL,
        ProgressTracker,
        cancel_job_progress,
        get_job_progress,
    )

    __all__.extend([
        "DEFAULT_UPDATE_INTERVAL",
        "PROGRESS_CHANNEL_PREFIX",
        "PROGRESS_KEY_PREFIX",
        "PROGRESS_TTL",
        "ProgressTracker",
        "cancel_job_progress",
        "get_job_progress",
    ])
except ImportError:  # pragma: no cover
    pass
