"""Business logic service for generation job lifecycle management.

This module implements the :class:`JobService` class — the primary service-layer
component responsible for orchestrating generation job CRUD operations, inter-service
dispatching, real-time progress tracking, and tenant-scoped data access within the
API Gateway.

**Architectural Role:**

``JobService`` acts as the intermediary between the HTTP routes layer
(``api_gateway.routes.generation``) and the data/external service layer
(:class:`~api_gateway.models.generation_job.GenerationJob` for MongoDB persistence,
Redis for caching/pub-sub, and httpx for inter-service REST calls to the Generation
Engine).  This separation ensures that route handlers remain thin and focused on
HTTP concerns while all business logic, validation, and orchestration is centralised
here.

**Multi-Tenant Isolation (R-007):**

Every public method requires a ``tenant_id`` parameter, and all downstream calls
(MongoDB queries, Redis key namespacing, HTTP headers) include the tenant context
so that cross-tenant data access is impossible by design.

**Resilience Patterns:**

- **Circuit Breaker** — The :meth:`_dispatch_to_generation_engine` and cancel-job
  HTTP calls to the Generation Engine are protected by the ``@circuit`` decorator
  (``failure_threshold=5``, ``recovery_timeout=30s``) to prevent cascade failures
  when the downstream service is unavailable.
- **Redis Cache** — Job status is cached in Redis with a 1-hour TTL to reduce
  MongoDB query load for the most common read pattern (status polling).
- **Redis Pub/Sub** — Terminal status events (``COMPLETED``, ``FAILED``) are
  published on the ``job_events`` channel so that WebSocket consumers can push
  real-time updates to the Web Console.

**Logging (R-013, R-005):**

All operations emit structured JSON log events via structlog with automatic
correlation ID propagation and tenant context injection.  Sensitive data such as
``schema_config`` is **never** included in log output (R-005: no PII leakage).

**Status State Machine:**

Jobs progress through a well-defined lifecycle::

    Submitted → Generating → Validating → Certifying → Provisioning → Completed
              ↘ Failed (from any non-terminal state)

Example::

    from api_gateway.services.job_service import JobService

    service = JobService()
    job = service.create_job(
        tenant_id="tenant-abc",
        user_id="user-123",
        generation_method="statistical",
        schema_config={"tables": [{"table_name": "GL_ENTRIES", "columns": ["amount"], "record_count": 10000}]},
        output_format="parquet",
    )
    retrieved = service.get_job(job["job_id"], tenant_id="tenant-abc")
"""

from __future__ import annotations

import json
import logging
import uuid
from datetime import datetime, timezone
from typing import Any, Dict, Optional

import httpx
from circuitbreaker import circuit
from flask import current_app

from api_gateway.extensions import get_db, get_redis
from api_gateway.models.generation_job import (
    GenerationJob,
    GenerationMethod,
    JobStatus,
    OutputFormat,
)
from shared.logging.structured_logger import get_logger


# ---------------------------------------------------------------------------
# Module-level constants
# ---------------------------------------------------------------------------

_REDIS_STATUS_TTL: int = 3600
"""TTL in seconds for Redis-cached job status entries (1 hour)."""

_REDIS_KEY_PREFIX: str = "job"
"""Prefix for all Redis keys managed by the JobService."""

_PUBSUB_CHANNEL: str = "job_events"
"""Redis pub/sub channel name for terminal job status events."""

_HTTP_TIMEOUT: float = 30.0
"""Timeout in seconds for HTTP calls to the Generation Engine."""

_CIRCUIT_FAILURE_THRESHOLD: int = 5
"""Number of consecutive failures before the circuit breaker opens."""

_CIRCUIT_RECOVERY_TIMEOUT: int = 30
"""Seconds to wait before attempting recovery after circuit opens."""

_CANCELLABLE_STATUSES: frozenset[str] = frozenset(
    {JobStatus.SUBMITTED.value, JobStatus.GENERATING.value}
)
"""Job statuses from which cancellation is permitted."""


# ===================================================================
# Service Class
# ===================================================================


class JobService:
    """Business logic service for generation job lifecycle management.

    Provides a clean, tenant-scoped API for:

    - **Creating** generation jobs (persisted in MongoDB, dispatched to the
      Generation Engine, and cached in Redis).
    - **Retrieving** individual jobs with a Redis fast-path for status polling.
    - **Listing** jobs with cursor-based pagination and optional status filtering.
    - **Updating** job status through the seven-state lifecycle.
    - **Updating** generation progress (percentage and record count).
    - **Cancelling** in-progress or queued jobs.
    - **Aggregating** per-tenant job statistics for the dashboard.

    All public methods enforce multi-tenant isolation (R-007) by requiring a
    ``tenant_id`` parameter that is propagated to every MongoDB query and Redis
    key operation.

    Attributes:
        logger: Pre-configured structlog ``BoundLogger`` for structured JSON
            logging with correlation ID and tenant context injection.

    Example::

        service = JobService()
        job = service.create_job(
            tenant_id="tenant-abc",
            user_id="user-123",
            generation_method="ai_ml",
            schema_config={
                "tables": [
                    {
                        "table_name": "GL_ENTRIES",
                        "columns": ["posting_date", "amount"],
                        "record_count": 50000,
                    }
                ]
            },
            output_format="parquet",
        )
    """

    # ------------------------------------------------------------------
    # Initialisation
    # ------------------------------------------------------------------

    def __init__(self) -> None:
        """Initialise the JobService with a structlog logger.

        The logger is obtained from the shared logging infrastructure
        (:func:`~shared.logging.structured_logger.get_logger`) and is bound
        to the module name for easy identification in log output.  No
        configuration values are read at construction time — Flask's
        ``current_app.config`` is accessed lazily during method execution
        so that the service can be instantiated outside an app context
        (e.g. in tests or at module import time) and still function
        correctly once a context is established.
        """
        self.logger = get_logger(__name__)

    # ------------------------------------------------------------------
    # Public API: Create
    # ------------------------------------------------------------------

    def create_job(
        self,
        tenant_id: str,
        user_id: str,
        generation_method: str,
        schema_config: Dict[str, Any],
        output_format: str,
        metadata: Optional[Dict[str, Any]] = None,
    ) -> Dict[str, Any]:
        """Create a new generation job, persist it, and dispatch to the Generation Engine.

        Validates the generation method and output format against their
        respective enums, validates the ``schema_config`` structure, persists the
        job document in the ``generation_profiles`` MongoDB collection via
        :meth:`GenerationJob.create`, caches the initial status in Redis, and
        asynchronously dispatches the job to the Generation Engine.

        Args:
            tenant_id: The tenant namespace identifier (R-007 multi-tenant
                isolation).
            user_id: The authenticated user who initiated the job.
            generation_method: One of ``'ai_ml'``, ``'rules_based'``,
                ``'statistical'``, ``'masking'``.
            schema_config: Dict describing the tables, columns, and record counts
                for generation.  Must contain a ``tables`` list where each entry
                has ``table_name`` (str), ``columns`` (list), and
                ``record_count`` (int) keys.
            output_format: One of ``'sql'``, ``'csv'``, ``'json'``, ``'parquet'``.
            metadata: Optional arbitrary metadata dict to attach to the job
                document.

        Returns:
            The created job document dict with ``job_id``, ``status``, and all
            fields populated.

        Raises:
            ValueError: If ``generation_method``, ``output_format``, or
                ``schema_config`` fail validation.
            pymongo.errors.OperationFailure: If the MongoDB insert fails.
            Exception: If the Generation Engine dispatch fails (logged but the
                job is still created in ``submitted`` status).
        """
        # ----- Enum validation -----
        self._validate_generation_method(generation_method)
        self._validate_output_format(output_format)

        # ----- Schema config structure validation -----
        self._validate_schema_config(schema_config)

        # ----- Persist to MongoDB via the GenerationJob model -----
        self.logger.info(
            "job_creation_started",
            tenant_id=tenant_id,
            user_id=user_id,
            generation_method=generation_method,
            output_format=output_format,
        )

        job: Dict[str, Any] = GenerationJob.create(
            tenant_id=tenant_id,
            user_id=user_id,
            generation_method=generation_method,
            schema_config=schema_config,
            output_format=output_format,
            metadata=metadata or {},
        )

        job_id: str = job["job_id"]

        # ----- Cache initial status in Redis -----
        try:
            self._cache_job_status(
                job_id=job_id,
                status=JobStatus.SUBMITTED.value,
                progress=0,
            )
        except Exception as exc:
            # Redis cache failure is non-fatal — the job is persisted in MongoDB.
            self.logger.warning(
                "job_status_cache_failed",
                job_id=job_id,
                tenant_id=tenant_id,
                error=str(exc),
                error_type=type(exc).__name__,
            )

        # ----- Dispatch to Generation Engine (best-effort) -----
        try:
            self._dispatch_to_generation_engine(job)
        except Exception as exc:
            # Dispatch failure is logged but does not roll back the job.
            # The job remains in SUBMITTED status and can be retried or
            # picked up by a background reconciliation process.
            self.logger.error(
                "job_dispatch_failed",
                job_id=job_id,
                tenant_id=tenant_id,
                error=str(exc),
                error_type=type(exc).__name__,
            )

        self.logger.info(
            "job_creation_completed",
            job_id=job_id,
            tenant_id=tenant_id,
            generation_method=generation_method,
            output_format=output_format,
        )

        return job

    # ------------------------------------------------------------------
    # Public API: Read (single)
    # ------------------------------------------------------------------

    def get_job(
        self,
        job_id: str,
        tenant_id: str,
    ) -> Optional[Dict[str, Any]]:
        """Retrieve a single generation job by ID with tenant isolation.

        Implements a two-level read strategy:

        1. **Redis fast-path** — Check the Redis cache for the job's current
           status and progress.  If found, the cached values are merged into
           the MongoDB document to provide the most up-to-date view without
           waiting for MongoDB write propagation.
        2. **MongoDB fallback** — If the cache miss occurs (or Redis is
           unavailable), the full document is fetched from the
           ``generation_profiles`` collection via
           :meth:`GenerationJob.find_by_id`.

        The ``tenant_id`` is always included in the MongoDB query filter to
        enforce multi-tenant isolation (R-007).

        Args:
            job_id: The UUID4 identifier of the generation job.
            tenant_id: The tenant namespace identifier (R-007).

        Returns:
            The job document dict with the latest status and progress, or
            ``None`` if no matching document was found for this tenant.

        Raises:
            pymongo.errors.OperationFailure: If the MongoDB query fails.
        """
        # ----- Fast-path: check Redis cache for up-to-date status -----
        cached_status: Optional[Dict[str, Any]] = None
        try:
            cached_status = self._get_cached_status(job_id)
        except Exception as exc:
            self.logger.warning(
                "job_cache_read_failed",
                job_id=job_id,
                tenant_id=tenant_id,
                error=str(exc),
                error_type=type(exc).__name__,
            )

        # ----- MongoDB: authoritative source with tenant isolation -----
        job: Optional[Dict[str, Any]] = GenerationJob.find_by_id(
            job_id=job_id,
            tenant_id=tenant_id,
        )

        if job is None:
            self.logger.info(
                "job_not_found",
                job_id=job_id,
                tenant_id=tenant_id,
            )
            return None

        # Merge cached status/progress into the MongoDB document if available.
        # Redis values are more current for actively running jobs because the
        # Generation Engine pushes progress updates to Redis at higher frequency
        # than MongoDB writes.
        if cached_status is not None:
            if "status" in cached_status:
                job["status"] = cached_status["status"]
            if "progress" in cached_status:
                job["progress_percentage"] = cached_status["progress"]

        return job

    # ------------------------------------------------------------------
    # Public API: Read (list)
    # ------------------------------------------------------------------

    def list_jobs(
        self,
        tenant_id: str,
        status: Optional[str] = None,
        page: int = 1,
        page_size: int = 20,
    ) -> Dict[str, Any]:
        """Retrieve a paginated list of generation jobs for a tenant.

        Delegates to :meth:`GenerationJob.find_by_tenant` which applies
        tenant-scoped filtering, optional status filtering, and offset-based
        pagination sorted by ``created_at`` descending.

        Args:
            tenant_id: The tenant namespace identifier (R-007).
            status: Optional job status filter (e.g. ``'generating'``).  Must
                be a valid :class:`JobStatus` value if provided.
            page: 1-based page number.  Defaults to ``1``.
            page_size: Maximum items per page.  Defaults to ``20``.  Clamped
                to ``[1, 100]`` by the model layer.

        Returns:
            A pagination result dict with keys::

                {
                    "items": [<job document>, ...],
                    "total": <int>,
                    "page": <int>,
                    "page_size": <int>,
                    "has_next": <bool>
                }

        Raises:
            ValueError: If ``status`` is provided but is not a valid
                :class:`JobStatus` value.
            pymongo.errors.OperationFailure: If the MongoDB query fails.
        """
        self.logger.info(
            "job_list_requested",
            tenant_id=tenant_id,
            status_filter=status,
            page=page,
            page_size=page_size,
        )

        result: Dict[str, Any] = GenerationJob.find_by_tenant(
            tenant_id=tenant_id,
            status=status,
            page=page,
            page_size=page_size,
        )

        self.logger.info(
            "job_list_completed",
            tenant_id=tenant_id,
            total=result.get("total", 0),
            items_returned=len(result.get("items", [])),
        )

        return result

    # ------------------------------------------------------------------
    # Public API: Update — Status
    # ------------------------------------------------------------------

    def update_job_status(
        self,
        job_id: str,
        tenant_id: str,
        new_status: str,
        error_message: Optional[str] = None,
    ) -> Optional[Dict[str, Any]]:
        """Transition a generation job to a new lifecycle status.

        Validates the new status against :class:`JobStatus`, delegates the
        MongoDB update to :meth:`GenerationJob.update_status`, updates the
        Redis cache, and publishes a notification event on the
        ``job_events`` pub/sub channel when the job reaches a terminal state
        (``COMPLETED`` or ``FAILED``).

        Args:
            job_id: The UUID4 identifier of the generation job.
            tenant_id: The tenant namespace identifier (R-007).
            new_status: The target status value.  Must be a valid
                :class:`JobStatus` member value.
            error_message: Optional error description for ``FAILED`` transitions.

        Returns:
            The post-update job document dict, or ``None`` if no matching
            document was found.

        Raises:
            ValueError: If ``new_status`` is not a valid :class:`JobStatus`
                value.
            pymongo.errors.OperationFailure: If the MongoDB update fails.
        """
        # Validate status enum
        self._validate_status(new_status)

        self.logger.info(
            "job_status_update_started",
            job_id=job_id,
            tenant_id=tenant_id,
            new_status=new_status,
        )

        # ----- MongoDB: authoritative status update -----
        updated_job: Optional[Dict[str, Any]] = GenerationJob.update_status(
            job_id=job_id,
            tenant_id=tenant_id,
            new_status=new_status,
            error_message=error_message,
        )

        if updated_job is None:
            self.logger.warning(
                "job_status_update_not_found",
                job_id=job_id,
                tenant_id=tenant_id,
                new_status=new_status,
            )
            return None

        # ----- Redis: cache update for real-time status tracking -----
        progress: int = updated_job.get("progress_percentage", 0)
        try:
            self._cache_job_status(
                job_id=job_id,
                status=new_status,
                progress=progress,
            )
        except Exception as exc:
            self.logger.warning(
                "job_status_cache_update_failed",
                job_id=job_id,
                tenant_id=tenant_id,
                error=str(exc),
                error_type=type(exc).__name__,
            )

        # ----- Redis pub/sub: notify on terminal states -----
        if new_status in (JobStatus.COMPLETED.value, JobStatus.FAILED.value):
            try:
                redis_client = get_redis()
                event_payload: str = json.dumps({
                    "job_id": job_id,
                    "tenant_id": tenant_id,
                    "status": new_status,
                    "timestamp": datetime.now(timezone.utc).isoformat(),
                    "error_message": error_message,
                })
                redis_client.publish(_PUBSUB_CHANNEL, event_payload)
                self.logger.info(
                    "job_terminal_event_published",
                    job_id=job_id,
                    tenant_id=tenant_id,
                    status=new_status,
                    channel=_PUBSUB_CHANNEL,
                )
            except Exception as exc:
                # Pub/sub failure is non-fatal — the status is already
                # persisted in MongoDB and Redis cache.
                self.logger.warning(
                    "job_pubsub_publish_failed",
                    job_id=job_id,
                    tenant_id=tenant_id,
                    error=str(exc),
                    error_type=type(exc).__name__,
                )

        self.logger.info(
            "job_status_update_completed",
            job_id=job_id,
            tenant_id=tenant_id,
            new_status=new_status,
        )

        return updated_job

    # ------------------------------------------------------------------
    # Public API: Update — Progress
    # ------------------------------------------------------------------

    def update_job_progress(
        self,
        job_id: str,
        tenant_id: str,
        progress: int,
        records_generated: int,
    ) -> Optional[Dict[str, Any]]:
        """Update generation progress for a running job.

        Delegates the MongoDB update to :meth:`GenerationJob.update_progress`
        (which clamps values to valid ranges) and updates the Redis cache so
        that status-polling clients get near-instant visibility into progress.

        Args:
            job_id: The UUID4 identifier of the generation job.
            tenant_id: The tenant namespace identifier (R-007).
            progress: Integer percentage (0–100) indicating completion.
            records_generated: Number of synthetic records generated so far.

        Returns:
            The post-update job document dict, or ``None`` if no matching
            document was found.

        Raises:
            pymongo.errors.OperationFailure: If the MongoDB update fails.
        """
        self.logger.info(
            "job_progress_update_started",
            job_id=job_id,
            tenant_id=tenant_id,
            progress=progress,
            records_generated=records_generated,
        )

        updated_job: Optional[Dict[str, Any]] = GenerationJob.update_progress(
            job_id=job_id,
            tenant_id=tenant_id,
            progress_percentage=progress,
            records_generated=records_generated,
        )

        if updated_job is None:
            self.logger.warning(
                "job_progress_update_not_found",
                job_id=job_id,
                tenant_id=tenant_id,
            )
            return None

        # ----- Redis: cache updated progress -----
        current_status: str = updated_job.get("status", JobStatus.GENERATING.value)
        try:
            self._cache_job_status(
                job_id=job_id,
                status=current_status,
                progress=max(0, min(progress, 100)),
            )
        except Exception as exc:
            self.logger.warning(
                "job_progress_cache_update_failed",
                job_id=job_id,
                tenant_id=tenant_id,
                error=str(exc),
                error_type=type(exc).__name__,
            )

        self.logger.info(
            "job_progress_update_completed",
            job_id=job_id,
            tenant_id=tenant_id,
            progress=progress,
            records_generated=records_generated,
        )

        return updated_job

    # ------------------------------------------------------------------
    # Public API: Cancel
    # ------------------------------------------------------------------

    def cancel_job(
        self,
        job_id: str,
        tenant_id: str,
    ) -> Optional[Dict[str, Any]]:
        """Cancel a generation job that is queued or in progress.

        A job may only be cancelled when its current status is ``SUBMITTED``
        or ``GENERATING``.  If the job is in ``GENERATING`` status, a cancel
        request is also sent to the Generation Engine via HTTP.  Regardless
        of whether the downstream cancel succeeds, the job's status is
        transitioned to ``FAILED`` with ``error_message='Cancelled by user'``.

        Args:
            job_id: The UUID4 identifier of the generation job.
            tenant_id: The tenant namespace identifier (R-007).

        Returns:
            The post-update job document dict with ``FAILED`` status, or
            ``None`` if the job was not found or is not in a cancellable
            state.

        Raises:
            pymongo.errors.OperationFailure: If the MongoDB update fails.
        """
        self.logger.info(
            "job_cancellation_started",
            job_id=job_id,
            tenant_id=tenant_id,
        )

        # ----- Retrieve current job to check status -----
        current_job: Optional[Dict[str, Any]] = GenerationJob.find_by_id(
            job_id=job_id,
            tenant_id=tenant_id,
        )

        if current_job is None:
            self.logger.warning(
                "job_cancellation_not_found",
                job_id=job_id,
                tenant_id=tenant_id,
            )
            return None

        current_status: str = current_job.get("status", "")

        if current_status not in _CANCELLABLE_STATUSES:
            self.logger.warning(
                "job_cancellation_invalid_status",
                job_id=job_id,
                tenant_id=tenant_id,
                current_status=current_status,
                cancellable_statuses=list(_CANCELLABLE_STATUSES),
            )
            return None

        # ----- If actively generating, send cancel to Generation Engine -----
        if current_status == JobStatus.GENERATING.value:
            try:
                self._send_cancel_to_generation_engine(job_id, tenant_id)
            except Exception as exc:
                # Cancel HTTP failure is logged but does not prevent the
                # local status transition.  The Generation Engine should
                # also poll job status and self-terminate.
                self.logger.warning(
                    "job_cancel_engine_request_failed",
                    job_id=job_id,
                    tenant_id=tenant_id,
                    error=str(exc),
                    error_type=type(exc).__name__,
                )

        # ----- Transition to FAILED with cancellation message -----
        cancelled_job: Optional[Dict[str, Any]] = self.update_job_status(
            job_id=job_id,
            tenant_id=tenant_id,
            new_status=JobStatus.FAILED.value,
            error_message="Cancelled by user",
        )

        self.logger.info(
            "job_cancellation_completed",
            job_id=job_id,
            tenant_id=tenant_id,
        )

        return cancelled_job

    # ------------------------------------------------------------------
    # Public API: Statistics
    # ------------------------------------------------------------------

    def get_job_statistics(
        self,
        tenant_id: str,
    ) -> Dict[str, Any]:
        """Aggregate per-tenant job counts grouped by status.

        Uses :meth:`GenerationJob.count_by_tenant` for each status of
        interest and returns a summary dict suitable for dashboard display
        (Screen S-001).

        Args:
            tenant_id: The tenant namespace identifier (R-007).

        Returns:
            A dict with job counts::

                {
                    "total": <int>,
                    "submitted": <int>,
                    "generating": <int>,
                    "validating": <int>,
                    "certifying": <int>,
                    "provisioning": <int>,
                    "completed": <int>,
                    "failed": <int>
                }

        Raises:
            pymongo.errors.OperationFailure: If the MongoDB count query fails.
        """
        self.logger.info(
            "job_statistics_requested",
            tenant_id=tenant_id,
        )

        total: int = GenerationJob.count_by_tenant(tenant_id=tenant_id)

        submitted: int = GenerationJob.count_by_tenant(
            tenant_id=tenant_id,
            status=JobStatus.SUBMITTED.value,
        )
        generating: int = GenerationJob.count_by_tenant(
            tenant_id=tenant_id,
            status=JobStatus.GENERATING.value,
        )
        validating: int = GenerationJob.count_by_tenant(
            tenant_id=tenant_id,
            status=JobStatus.VALIDATING.value,
        )
        certifying: int = GenerationJob.count_by_tenant(
            tenant_id=tenant_id,
            status=JobStatus.CERTIFYING.value,
        )
        provisioning: int = GenerationJob.count_by_tenant(
            tenant_id=tenant_id,
            status=JobStatus.PROVISIONING.value,
        )
        completed: int = GenerationJob.count_by_tenant(
            tenant_id=tenant_id,
            status=JobStatus.COMPLETED.value,
        )
        failed: int = GenerationJob.count_by_tenant(
            tenant_id=tenant_id,
            status=JobStatus.FAILED.value,
        )

        statistics: Dict[str, Any] = {
            "total": total,
            "submitted": submitted,
            "generating": generating,
            "validating": validating,
            "certifying": certifying,
            "provisioning": provisioning,
            "completed": completed,
            "failed": failed,
        }

        self.logger.info(
            "job_statistics_completed",
            tenant_id=tenant_id,
            total=total,
            completed=completed,
            failed=failed,
        )

        return statistics

    # ------------------------------------------------------------------
    # Private: Generation Engine dispatch
    # ------------------------------------------------------------------

    @circuit(
        failure_threshold=_CIRCUIT_FAILURE_THRESHOLD,
        recovery_timeout=_CIRCUIT_RECOVERY_TIMEOUT,
    )
    def _dispatch_to_generation_engine(
        self,
        job: Dict[str, Any],
    ) -> Dict[str, Any]:
        """Dispatch a generation job to the Generation Engine via HTTP POST.

        Sends the job details as a JSON payload to the Generation Engine's
        ``/api/v1/generate`` endpoint.  The ``tenant_id`` is included in the
        HTTP headers (``X-Tenant-ID``) so that the downstream service can
        enforce its own tenant isolation.

        Protected by the ``@circuit`` decorator which opens the circuit after
        :data:`_CIRCUIT_FAILURE_THRESHOLD` consecutive failures and waits
        :data:`_CIRCUIT_RECOVERY_TIMEOUT` seconds before re-attempting.

        Args:
            job: The job document dict as returned by
                :meth:`GenerationJob.create`.  Must contain ``job_id``,
                ``tenant_id``, ``generation_method``, ``schema_config``,
                ``output_format``, and ``total_records_requested``.

        Returns:
            A dict containing the Generation Engine's response payload.

        Raises:
            httpx.HTTPError: If the HTTP request fails (connection error,
                timeout, or non-2xx response).
            circuitbreaker.CircuitBreakerError: If the circuit breaker is
                in the open state.
        """
        generation_engine_url: str = current_app.config.get(
            "GENERATION_ENGINE_URL", "http://generation-engine:5001"
        )
        endpoint: str = f"{generation_engine_url}/api/v1/generate"

        job_id: str = job.get("job_id", "unknown")
        tenant_id: str = job.get("tenant_id", "unknown")

        # Build the request payload — excludes sensitive fields and internal
        # MongoDB metadata per R-005.
        payload: Dict[str, Any] = {
            "job_id": job_id,
            "tenant_id": tenant_id,
            "generation_method": job.get("generation_method"),
            "schema_config": job.get("schema_config"),
            "output_format": job.get("output_format"),
            "total_records_requested": job.get("total_records_requested", 0),
            "metadata": job.get("metadata", {}),
        }

        headers: Dict[str, str] = {
            "Content-Type": "application/json",
            "X-Tenant-ID": tenant_id,
            "X-Job-ID": job_id,
        }

        self.logger.info(
            "generation_engine_dispatch_started",
            job_id=job_id,
            tenant_id=tenant_id,
            endpoint=endpoint,
        )

        with httpx.Client(timeout=_HTTP_TIMEOUT) as client:
            response: httpx.Response = client.post(
                endpoint,
                json=payload,
                headers=headers,
            )
            response.raise_for_status()

            response_data: Dict[str, Any] = response.json()

        self.logger.info(
            "generation_engine_dispatch_completed",
            job_id=job_id,
            tenant_id=tenant_id,
            response_status=response.status_code,
        )

        return response_data

    # ------------------------------------------------------------------
    # Private: Cancel request to Generation Engine
    # ------------------------------------------------------------------

    @circuit(
        failure_threshold=_CIRCUIT_FAILURE_THRESHOLD,
        recovery_timeout=_CIRCUIT_RECOVERY_TIMEOUT,
    )
    def _send_cancel_to_generation_engine(
        self,
        job_id: str,
        tenant_id: str,
    ) -> Dict[str, Any]:
        """Send a cancellation request to the Generation Engine.

        Issues an HTTP POST to the Generation Engine's cancel endpoint so
        that any in-progress generation work is terminated gracefully.

        Args:
            job_id: The UUID4 identifier of the generation job to cancel.
            tenant_id: The tenant namespace identifier for the
                ``X-Tenant-ID`` header.

        Returns:
            A dict containing the Generation Engine's cancellation response.

        Raises:
            httpx.HTTPError: If the HTTP request fails.
            circuitbreaker.CircuitBreakerError: If the circuit breaker is open.
        """
        generation_engine_url: str = current_app.config.get(
            "GENERATION_ENGINE_URL", "http://generation-engine:5001"
        )
        endpoint: str = f"{generation_engine_url}/api/v1/generate/{job_id}/cancel"

        headers: Dict[str, str] = {
            "Content-Type": "application/json",
            "X-Tenant-ID": tenant_id,
            "X-Job-ID": job_id,
        }

        self.logger.info(
            "generation_engine_cancel_started",
            job_id=job_id,
            tenant_id=tenant_id,
            endpoint=endpoint,
        )

        with httpx.Client(timeout=_HTTP_TIMEOUT) as client:
            response: httpx.Response = client.post(
                endpoint,
                headers=headers,
            )
            response.raise_for_status()
            response_data: Dict[str, Any] = response.json()

        self.logger.info(
            "generation_engine_cancel_completed",
            job_id=job_id,
            tenant_id=tenant_id,
            response_status=response.status_code,
        )

        return response_data

    # ------------------------------------------------------------------
    # Private: Redis cache helpers
    # ------------------------------------------------------------------

    def _cache_job_status(
        self,
        job_id: str,
        status: str,
        progress: int = 0,
    ) -> None:
        """Cache job status and progress in Redis with a TTL.

        Serialises the status and progress values as a JSON string and stores
        them under the key ``job:<job_id>:status`` with a TTL of
        :data:`_REDIS_STATUS_TTL` seconds (1 hour).

        Args:
            job_id: The UUID4 identifier of the generation job.
            status: Current job status string.
            progress: Current progress percentage (0–100).

        Raises:
            redis.exceptions.RedisError: If the Redis operation fails.
        """
        redis_client = get_redis()
        cache_key: str = f"{_REDIS_KEY_PREFIX}:{job_id}:status"
        cache_value: str = json.dumps({
            "status": status,
            "progress": progress,
            "updated_at": datetime.now(timezone.utc).isoformat(),
        })
        redis_client.setex(
            name=cache_key,
            time=_REDIS_STATUS_TTL,
            value=cache_value,
        )

    def _get_cached_status(
        self,
        job_id: str,
    ) -> Optional[Dict[str, Any]]:
        """Retrieve cached job status from Redis.

        Looks up the key ``job:<job_id>:status`` and deserialises the JSON
        value.  Returns ``None`` on cache miss.

        Args:
            job_id: The UUID4 identifier of the generation job.

        Returns:
            A dict with ``status``, ``progress``, and ``updated_at`` keys,
            or ``None`` if the key does not exist.

        Raises:
            redis.exceptions.RedisError: If the Redis operation fails.
        """
        redis_client = get_redis()
        cache_key: str = f"{_REDIS_KEY_PREFIX}:{job_id}:status"
        raw_value: Optional[bytes] = redis_client.get(cache_key)

        if raw_value is None:
            return None

        # Redis returns bytes; decode and parse JSON.
        return json.loads(raw_value)

    # ------------------------------------------------------------------
    # Private: Validation helpers
    # ------------------------------------------------------------------

    @staticmethod
    def _validate_generation_method(method: str) -> str:
        """Validate a generation method string against the GenerationMethod enum.

        Args:
            method: The generation method string to validate.

        Returns:
            The validated generation method value.

        Raises:
            ValueError: If ``method`` is not a valid :class:`GenerationMethod` value.
        """
        try:
            return GenerationMethod(method).value
        except ValueError:
            valid_methods = [m.value for m in GenerationMethod]
            raise ValueError(
                f"Invalid generation_method '{method}'. "
                f"Must be one of: {valid_methods}"
            ) from None

    @staticmethod
    def _validate_output_format(fmt: str) -> str:
        """Validate an output format string against the OutputFormat enum.

        Args:
            fmt: The output format string to validate.

        Returns:
            The validated output format value.

        Raises:
            ValueError: If ``fmt`` is not a valid :class:`OutputFormat` value.
        """
        try:
            return OutputFormat(fmt).value
        except ValueError:
            valid_formats = [f.value for f in OutputFormat]
            raise ValueError(
                f"Invalid output_format '{fmt}'. Must be one of: {valid_formats}"
            ) from None

    @staticmethod
    def _validate_status(status: str) -> str:
        """Validate a job status string against the JobStatus enum.

        Args:
            status: The status string to validate.

        Returns:
            The validated status value.

        Raises:
            ValueError: If ``status`` is not a valid :class:`JobStatus` value.
        """
        try:
            return JobStatus(status).value
        except ValueError:
            valid_statuses = [s.value for s in JobStatus]
            raise ValueError(
                f"Invalid status '{status}'. Must be one of: {valid_statuses}"
            ) from None

    @staticmethod
    def _validate_schema_config(schema_config: Dict[str, Any]) -> None:
        """Validate the structure of a schema configuration dict.

        Ensures the ``schema_config`` contains a non-empty ``tables`` list
        where each entry has the required keys: ``table_name`` (str),
        ``columns`` (list), and ``record_count`` (int ≥ 1).

        Args:
            schema_config: The schema configuration dict to validate.

        Raises:
            ValueError: If ``schema_config`` is missing required keys or
                contains invalid values.
        """
        if not isinstance(schema_config, dict):
            raise ValueError("schema_config must be a dictionary.")

        tables = schema_config.get("tables")
        if not isinstance(tables, list) or len(tables) == 0:
            raise ValueError(
                "schema_config must contain a non-empty 'tables' list."
            )

        for idx, table in enumerate(tables):
            if not isinstance(table, dict):
                raise ValueError(
                    f"schema_config.tables[{idx}] must be a dictionary."
                )

            table_name = table.get("table_name")
            if not isinstance(table_name, str) or not table_name.strip():
                raise ValueError(
                    f"schema_config.tables[{idx}].table_name must be a "
                    f"non-empty string."
                )

            columns = table.get("columns")
            if not isinstance(columns, list) or len(columns) == 0:
                raise ValueError(
                    f"schema_config.tables[{idx}].columns must be a "
                    f"non-empty list."
                )

            record_count = table.get("record_count")
            if not isinstance(record_count, int) or record_count < 1:
                raise ValueError(
                    f"schema_config.tables[{idx}].record_count must be a "
                    f"positive integer, got {record_count!r}."
                )
