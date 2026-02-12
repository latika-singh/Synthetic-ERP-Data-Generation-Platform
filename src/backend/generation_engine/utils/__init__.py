"""Generation Engine utility modules.

Provides cross-cutting utility functionality shared across the Generation
Engine's orchestrator, generators, and formatters:

- **Checkpoint management** — Redis-backed checkpoint/resume support for
  long-running generation jobs via :class:`~.checkpoint.CheckpointManager`.
- **Progress tracking** — Real-time Redis pub/sub progress reporting (see
  ``progress_tracker`` module when available).

Usage::

    from generation_engine.utils.checkpoint import CheckpointManager

    mgr = CheckpointManager("job-abc-123")
    if mgr.has_checkpoint():
        state = mgr.load_checkpoint()
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
        "CheckpointManager",
        "CheckpointState",
        "CHECKPOINT_KEY_PREFIX",
        "CHECKPOINT_TTL",
        "DEFAULT_CHECKPOINT_INTERVAL",
        "MAX_CHECKPOINT_SIZE",
        "get_checkpoint_info",
        "cleanup_expired_checkpoints",
    ])
except ImportError:  # pragma: no cover
    pass
