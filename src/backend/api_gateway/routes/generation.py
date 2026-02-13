"""Flask Blueprint module for generation job REST endpoints at /api/v1/generation/.

Implements six REST endpoints for managing synthetic data generation jobs:

    - ``POST /jobs``                    — Create a new generation job
    - ``GET /jobs``                     — List jobs with tenant-scoped pagination
    - ``GET /jobs/statistics``          — Tenant-scoped aggregate statistics
    - ``GET /jobs/<job_id>``            — Retrieve a specific job by ID
    - ``GET /jobs/<job_id>/progress``   — Real-time progress for a running job
    - ``DELETE /jobs/<job_id>``         — Cancel an in-progress or queued job

All endpoints require JWT authentication (``@jwt_required()``) and
permission-based RBAC enforcement (``@require_permissions()``) per security
requirement R-006.  Multi-tenant isolation is enforced via ``g.tenant_id``
on every database query per requirement R-007.

The module delegates **all** business logic to :class:`JobService` (Service
Layer pattern), which communicates with the Generation Engine microservice
via HTTP with circuit-breaker resilience.

Route ordering note:
    ``/jobs/statistics`` is registered **before** ``/jobs/<job_id>`` so
    that Flask's routing engine matches the literal path first and does
    not treat ``"statistics"`` as a ``job_id`` path variable.

Typical usage::

    # In routes/__init__.py (Blueprint registration):
    from api_gateway.routes.generation import generation_bp

    app.register_blueprint(generation_bp, url_prefix="/api/v1/generation")
"""

from __future__ import annotations

from typing import Any, Optional

from flask import Blueprint, g, jsonify, request
from flask_jwt_extended import jwt_required
from pydantic import ValidationError

from api_gateway.middleware.auth import require_permissions
from api_gateway.schemas.generation import (
    GenerationJobListResponse,
    GenerationJobRequest,
    GenerationJobResponse,
)
from api_gateway.services.job_service import JobService
from api_gateway.utils.pagination import (
    PaginatedResponse,
    paginate_query,
    validate_page_size,
)
from shared.logging.structured_logger import get_logger


# ---------------------------------------------------------------------------
# Module-level configuration
# ---------------------------------------------------------------------------

logger = get_logger(__name__)
"""Structured JSON logger with correlation-ID propagation."""

generation_bp = Blueprint("generation", __name__)
"""Flask Blueprint grouping all generation job endpoints.

Exported as the primary module symbol.  Registered in the Application
Factory at the ``/api/v1/generation`` URL prefix so that all routes
defined here resolve under ``/api/v1/generation/jobs/*``.
"""


# ---------------------------------------------------------------------------
# Helper functions
# ---------------------------------------------------------------------------


def _get_job_service() -> JobService:
    """Lazily instantiate :class:`JobService` for the current request.

    :class:`JobService` reads configuration from ``current_app.config``,
    which requires an active Flask application context.  Module-level
    instantiation is therefore not safe.  This helper caches the instance
    on Flask's ``g`` request-scoped proxy to avoid repeated object creation
    during a single request while guaranteeing application context access.

    Returns:
        A :class:`JobService` instance bound to the current Flask
        application context.
    """
    if not hasattr(g, "_job_service"):
        g._job_service = JobService()
    return g._job_service


def _build_error_response(
    message: str,
    code: str,
    status: int,
    details: Any = None,
) -> tuple:
    """Build a standardised JSON error response envelope.

    Produces a uniform error structure so that API consumers can rely on
    consistent field names regardless of which endpoint generated the
    error.

    Args:
        message: Human-readable error description.
        code: Machine-readable error code (e.g. ``"VALIDATION_ERROR"``).
        status: HTTP status code to return (e.g. 400, 404, 422, 500).
        details: Optional extra context — Pydantic field errors, trace
            IDs, or any JSON-serialisable value.

    Returns:
        A ``(flask.Response, int)`` tuple suitable for direct Flask route
        return.
    """
    payload: dict[str, Any] = {
        "error": message,
        "code": code,
    }
    if details is not None:
        payload["details"] = details
    return jsonify(payload), status


def _serialize_job_to_response(job_data: dict[str, Any]) -> dict[str, Any]:
    """Map a raw MongoDB job document to the :class:`GenerationJobResponse` schema.

    The :class:`JobService` returns MongoDB document dicts whose field names
    may not exactly match the API response contract defined by
    :class:`GenerationJobResponse`.  This helper performs safe field
    extraction and renames so that the response always conforms to the
    published schema, even when the underlying document evolves.

    If Pydantic model validation succeeds the response dict is generated via
    ``model_dump(mode="json")``.  If validation fails — for example because
    the document structure has drifted — the function falls back to a
    manually constructed dict with safe defaults so that the API never
    returns an internal 500 due to serialisation issues.

    Args:
        job_data: A job document dict as returned by
            :meth:`JobService.get_job` or :meth:`JobService.create_job`.

    Returns:
        A JSON-serialisable dictionary conforming to the
        :class:`GenerationJobResponse` contract.
    """
    # Build a candidate dict with normalised field names.
    candidate: dict[str, Any] = {
        "job_id": job_data.get("job_id", ""),
        "status": job_data.get("status", "submitted"),
        "method": job_data.get("generation_method", job_data.get("method", "statistical")),
        "progress": float(job_data.get("progress_percentage", job_data.get("progress", 0.0))),
        "total_records": int(job_data.get("total_records", 0)),
        "generated_records": int(job_data.get("records_generated", job_data.get("generated_records", 0))),
        "quality_score": job_data.get("quality_score"),
        "output_location": job_data.get("output_location"),
        "error_message": job_data.get("error_message"),
        "created_at": job_data.get("created_at"),
        "updated_at": job_data.get("updated_at"),
        "completed_at": job_data.get("completed_at"),
        "tenant_id": job_data.get("tenant_id"),
    }

    try:
        response_model = GenerationJobResponse.model_validate(candidate)
        return response_model.model_dump(mode="json")
    except (ValidationError, Exception):
        # Fallback: return the candidate dict directly with ISO-formatted
        # datetimes so that jsonify can handle them.
        for key in ("created_at", "updated_at", "completed_at"):
            value = candidate.get(key)
            if value is not None and hasattr(value, "isoformat"):
                candidate[key] = value.isoformat()
        return candidate


def _serialize_job_list_response(
    items: list[dict[str, Any]],
    total: int,
    page: int,
    page_size: int,
) -> dict[str, Any]:
    """Map a paginated job result set to :class:`GenerationJobListResponse`.

    Serialises each job via :func:`_serialize_job_to_response` and wraps
    the result in the standard list envelope expected by the Web Console.

    Args:
        items: List of raw job document dicts from the service layer.
        total: Total count of matching jobs across all pages.
        page: The current page number (1-based).
        page_size: The number of items per page (after validation).

    Returns:
        A JSON-serialisable dictionary matching the
        :class:`GenerationJobListResponse` schema.
    """
    serialised_jobs: list[dict[str, Any]] = [
        _serialize_job_to_response(job) for job in items
    ]

    try:
        list_model = GenerationJobListResponse(
            jobs=serialised_jobs,  # type: ignore[arg-type]
            total=total,
            page=page,
            page_size=page_size,
        )
        return list_model.model_dump(mode="json")
    except (ValidationError, Exception):
        # Fallback: manual envelope construction.
        return {
            "jobs": serialised_jobs,
            "total": total,
            "page": page,
            "page_size": page_size,
        }


# ---------------------------------------------------------------------------
# POST /jobs — Create a new generation job
# ---------------------------------------------------------------------------


@generation_bp.route("/jobs", methods=["POST"])
@jwt_required()
@require_permissions("generation:create")
def create_generation_job() -> tuple:
    """Create a new synthetic data generation job.

    Validates the incoming JSON body with :class:`GenerationJobRequest`,
    extracts user and tenant context from the JWT-populated ``g`` object,
    and delegates job creation to :class:`JobService`.

    The job is persisted in MongoDB with status ``submitted``, dispatched
    asynchronously to the Generation Engine microservice, and its initial
    status is cached in Redis for fast polling.

    Request Body:
        JSON conforming to :class:`GenerationJobRequest` schema::

            {
                "method": "statistical",
                "schema_id": "sch_abc123",
                "tables": [
                    {"table_name": "GL_ENTRIES", "record_count": 50000}
                ],
                "output_format": "parquet",
                "batch_size": 10000,
                "quality_threshold": 0.95
            }

    Returns:
        **201 Created** — :class:`GenerationJobResponse` body on success.
        **400 Bad Request** — If the JSON body is missing or unparseable.
        **422 Unprocessable Entity** — If Pydantic validation fails with
        field-level error details.
        **500 Internal Server Error** — For unexpected backend failures.

    Raises:
        No exceptions are raised directly; all errors are returned as
        structured JSON responses.
    """
    tenant_id: str = getattr(g, "tenant_id", "default")
    user_id: str = getattr(g, "user_id", "unknown")

    logger.info(
        "generation_job_create_request_received",
        tenant_id=tenant_id,
        user_id=user_id,
        method=request.method,
        path=request.path,
    )

    # --- Parse request body ---------------------------------------------------
    body: dict[str, Any] | None = request.get_json(silent=True)
    if body is None:
        logger.warning(
            "generation_job_create_invalid_json",
            tenant_id=tenant_id,
            user_id=user_id,
        )
        return _build_error_response(
            message="Request body must be valid JSON.",
            code="INVALID_JSON",
            status=400,
        )

    # --- Validate with Pydantic -----------------------------------------------
    try:
        job_request: GenerationJobRequest = GenerationJobRequest.model_validate(body)
    except ValidationError as exc:
        logger.warning(
            "generation_job_create_validation_failed",
            tenant_id=tenant_id,
            user_id=user_id,
            error_count=exc.error_count(),
            errors=exc.errors(include_url=False),
        )
        return _build_error_response(
            message="Request validation failed.",
            code="VALIDATION_ERROR",
            status=422,
            details=exc.errors(include_url=False),
        )

    # --- Override tenant_id from middleware if present -------------------------
    tenant_id = getattr(g, "tenant_id", None) or job_request.tenant_id or "default"

    # --- Delegate to service layer --------------------------------------------
    try:
        service: JobService = _get_job_service()
        result: dict[str, Any] = service.create_job(
            tenant_id=tenant_id,
            user_id=user_id,
            generation_method=job_request.method.value,
            schema_config={
                "schema_id": job_request.schema_id,
                "tables": [table.model_dump() for table in job_request.tables],
                "output_format": job_request.output_format.value,
                "batch_size": job_request.batch_size,
                "quality_threshold": job_request.quality_threshold,
                "template_id": job_request.template_id,
            },
            output_format=job_request.output_format.value,
            metadata=job_request.metadata,
        )

        response_data: dict[str, Any] = _serialize_job_to_response(result)

        logger.info(
            "generation_job_created",
            job_id=result.get("job_id"),
            method=job_request.method.value,
            output_format=job_request.output_format.value,
            tenant_id=tenant_id,
            user_id=user_id,
        )

        return jsonify(response_data), 201

    except ValueError as exc:
        logger.warning(
            "generation_job_create_value_error",
            error=str(exc),
            tenant_id=tenant_id,
            user_id=user_id,
        )
        return _build_error_response(
            message=str(exc),
            code="INVALID_REQUEST",
            status=400,
        )

    except Exception as exc:
        logger.error(
            "generation_job_create_failed",
            error=str(exc),
            error_type=type(exc).__name__,
            tenant_id=tenant_id,
            user_id=user_id,
        )
        return _build_error_response(
            message="Failed to create generation job.",
            code="JOB_CREATION_FAILED",
            status=500,
        )


# ---------------------------------------------------------------------------
# GET /jobs — List generation jobs with pagination and filtering
# ---------------------------------------------------------------------------


@generation_bp.route("/jobs", methods=["GET"])
@jwt_required()
@require_permissions("generation:read")
def list_generation_jobs() -> tuple:
    """List generation jobs with tenant-scoped pagination and optional filtering.

    Supports the following query parameters:

    - ``page`` (int, default 1): Page number (1-based).
    - ``page_size`` (int, default 20, max 100): Items per page.  Validated
      and clamped via :func:`validate_page_size`.
    - ``status`` (str, optional): Filter by job status (e.g.
      ``generating``, ``completed``).

    Pagination metadata is included in the response to support client-side
    navigation.  Results are sorted by ``created_at`` descending so that
    the most recent jobs appear first.

    Returns:
        **200 OK** — :class:`GenerationJobListResponse` body with paginated
        job list.
        **500 Internal Server Error** — For unexpected backend failures.
    """
    tenant_id: str = getattr(g, "tenant_id", "default")
    user_id: str = getattr(g, "user_id", "unknown")

    # --- Parse and validate query parameters ----------------------------------
    page: int = request.args.get("page", 1, type=int)
    raw_page_size: Optional[int] = request.args.get("page_size", None, type=int)
    status_filter: Optional[str] = request.args.get("status")

    # Use the pagination utility to validate and clamp page_size.
    page_size: int = validate_page_size(raw_page_size)

    # Ensure page is at least 1.
    page = max(1, page)

    logger.info(
        "generation_jobs_list_request",
        tenant_id=tenant_id,
        user_id=user_id,
        page=page,
        page_size=page_size,
        status_filter=status_filter,
    )

    try:
        service: JobService = _get_job_service()
        result: dict[str, Any] = service.list_jobs(
            tenant_id=tenant_id,
            status=status_filter,
            page=page,
            page_size=page_size,
        )

        # Extract raw items and total count from the service response.
        items: list[dict[str, Any]] = result.get("items", [])
        total: int = result.get("total", 0)

        # Wrap service results in PaginatedResponse for type-safe
        # cursor metadata.  paginate_query returns this model when
        # querying MongoDB directly; here we construct it from the
        # service-layer result to maintain the same pagination contract
        # (PaginatedResponse is the canonical pagination envelope used
        # by paginate_query and all list endpoints).
        paginated: PaginatedResponse = PaginatedResponse(
            items=items,
            total_count=total,
            has_more=(total > page * page_size),
            page_size=page_size,
            next_cursor=result.get("next_cursor"),
            prev_cursor=result.get("prev_cursor"),
        )

        # Serialise through GenerationJobListResponse for the final
        # API contract that the Web Console expects.
        response_data: dict[str, Any] = _serialize_job_list_response(
            items=paginated.items,
            total=paginated.total_count,
            page=page,
            page_size=paginated.page_size,
        )

        logger.info(
            "generation_jobs_listed",
            tenant_id=tenant_id,
            page=page,
            page_size=page_size,
            total=total,
            items_returned=len(items),
            has_more=paginated.has_more,
        )

        return jsonify(response_data), 200

    except ValueError as exc:
        logger.warning(
            "generation_jobs_list_value_error",
            error=str(exc),
            tenant_id=tenant_id,
        )
        return _build_error_response(
            message=str(exc),
            code="INVALID_FILTER",
            status=400,
        )

    except Exception as exc:
        logger.error(
            "generation_jobs_list_failed",
            error=str(exc),
            error_type=type(exc).__name__,
            tenant_id=tenant_id,
        )
        return _build_error_response(
            message="Failed to list generation jobs.",
            code="LIST_JOBS_FAILED",
            status=500,
        )


# ---------------------------------------------------------------------------
# GET /jobs/statistics — Tenant-scoped job aggregate statistics
#
# IMPORTANT: This route MUST be registered before /jobs/<job_id> to prevent
# Flask from matching "statistics" as a job_id path variable.
# ---------------------------------------------------------------------------


@generation_bp.route("/jobs/statistics", methods=["GET"])
@jwt_required()
@require_permissions("generation:read")
def get_job_statistics() -> tuple:
    """Retrieve aggregate job statistics for the current tenant.

    Returns counts grouped by status (submitted, generating, validating,
    certifying, provisioning, completed, failed) plus the total job count.
    This endpoint powers the Web Console Dashboard (Screen S-001)
    generation activity summary.

    Returns:
        **200 OK** — JSON body with per-status job counts::

            {
                "total": 142,
                "submitted": 3,
                "generating": 5,
                "validating": 2,
                "certifying": 1,
                "provisioning": 1,
                "completed": 120,
                "failed": 10
            }

        **500 Internal Server Error** — For unexpected backend failures.
    """
    tenant_id: str = getattr(g, "tenant_id", "default")

    logger.info(
        "job_statistics_request",
        tenant_id=tenant_id,
    )

    try:
        service: JobService = _get_job_service()
        stats: dict[str, Any] = service.get_job_statistics(tenant_id=tenant_id)

        logger.info(
            "job_statistics_retrieved",
            tenant_id=tenant_id,
            total=stats.get("total", 0),
        )

        return jsonify(stats), 200

    except Exception as exc:
        logger.error(
            "job_statistics_failed",
            error=str(exc),
            error_type=type(exc).__name__,
            tenant_id=tenant_id,
        )
        return _build_error_response(
            message="Failed to retrieve job statistics.",
            code="STATISTICS_FAILED",
            status=500,
        )


# ---------------------------------------------------------------------------
# GET /jobs/<job_id> — Retrieve a specific generation job
# ---------------------------------------------------------------------------


@generation_bp.route("/jobs/<job_id>", methods=["GET"])
@jwt_required()
@require_permissions("generation:read")
def get_generation_job(job_id: str) -> tuple:
    """Retrieve a specific generation job by ID.

    Looks up the job first in the Redis status cache (fast path) and
    falls back to MongoDB if not cached.  Only returns jobs belonging
    to the current tenant per multi-tenant isolation requirement R-007.

    Args:
        job_id: The unique job identifier (UUID string from the URL path).

    Returns:
        **200 OK** — :class:`GenerationJobResponse` body on success.
        **404 Not Found** — If the job does not exist or belongs to
        another tenant.
        **500 Internal Server Error** — For unexpected backend failures.
    """
    tenant_id: str = getattr(g, "tenant_id", "default")

    logger.info(
        "generation_job_get_request",
        job_id=job_id,
        tenant_id=tenant_id,
    )

    try:
        service: JobService = _get_job_service()
        job: dict[str, Any] | None = service.get_job(
            job_id=job_id,
            tenant_id=tenant_id,
        )

        if job is None:
            logger.warning(
                "generation_job_not_found",
                job_id=job_id,
                tenant_id=tenant_id,
            )
            return _build_error_response(
                message=f"Generation job '{job_id}' not found.",
                code="JOB_NOT_FOUND",
                status=404,
            )

        response_data: dict[str, Any] = _serialize_job_to_response(job)

        logger.info(
            "generation_job_retrieved",
            job_id=job_id,
            tenant_id=tenant_id,
            status=job.get("status"),
        )

        return jsonify(response_data), 200

    except Exception as exc:
        logger.error(
            "generation_job_get_failed",
            job_id=job_id,
            error=str(exc),
            error_type=type(exc).__name__,
            tenant_id=tenant_id,
        )
        return _build_error_response(
            message="Failed to retrieve generation job.",
            code="GET_JOB_FAILED",
            status=500,
        )


# ---------------------------------------------------------------------------
# GET /jobs/<job_id>/progress — Real-time generation progress
# ---------------------------------------------------------------------------


@generation_bp.route("/jobs/<job_id>/progress", methods=["GET"])
@jwt_required()
@require_permissions("generation:read")
def get_job_progress(job_id: str) -> tuple:
    """Retrieve real-time progress for a running generation job.

    Progress data is served primarily from the Redis cache where the
    Generation Engine publishes updates during batch processing.  Falls
    back to MongoDB for terminal states (completed / failed).

    The response is intentionally lightweight — a subset of the full
    job document — optimised for high-frequency polling by the Web
    Console's Job Monitoring screen (Screen S-004).

    Args:
        job_id: The unique job identifier (UUID string from the URL path).

    Returns:
        **200 OK** — JSON with progress fields::

            {
                "job_id": "abc-123",
                "status": "generating",
                "progress": 42.5,
                "total_records": 100000,
                "generated_records": 42500
            }

        **404 Not Found** — If the job does not exist or belongs to
        another tenant.
        **500 Internal Server Error** — For unexpected backend failures.
    """
    tenant_id: str = getattr(g, "tenant_id", "default")

    logger.info(
        "job_progress_request",
        job_id=job_id,
        tenant_id=tenant_id,
    )

    try:
        service: JobService = _get_job_service()
        job: dict[str, Any] | None = service.get_job(
            job_id=job_id,
            tenant_id=tenant_id,
        )

        if job is None:
            logger.warning(
                "job_progress_not_found",
                job_id=job_id,
                tenant_id=tenant_id,
            )
            return _build_error_response(
                message=f"Generation job '{job_id}' not found.",
                code="JOB_NOT_FOUND",
                status=404,
            )

        # Extract progress fields with safe fallbacks.  The underlying
        # MongoDB document may use 'progress_percentage' or 'progress'
        # depending on write path (service vs direct engine update).
        progress_data: dict[str, Any] = {
            "job_id": job_id,
            "status": job.get("status", "unknown"),
            "progress": float(
                job.get("progress_percentage", job.get("progress", 0.0))
            ),
            "total_records": int(job.get("total_records", 0)),
            "generated_records": int(
                job.get("records_generated", job.get("generated_records", 0))
            ),
        }

        logger.info(
            "job_progress_retrieved",
            job_id=job_id,
            tenant_id=tenant_id,
            status=progress_data["status"],
            progress=progress_data["progress"],
        )

        return jsonify(progress_data), 200

    except Exception as exc:
        logger.error(
            "job_progress_failed",
            job_id=job_id,
            error=str(exc),
            error_type=type(exc).__name__,
            tenant_id=tenant_id,
        )
        return _build_error_response(
            message="Failed to retrieve job progress.",
            code="PROGRESS_FAILED",
            status=500,
        )


# ---------------------------------------------------------------------------
# DELETE /jobs/<job_id> — Cancel a generation job
# ---------------------------------------------------------------------------


@generation_bp.route("/jobs/<job_id>", methods=["DELETE"])
@jwt_required()
@require_permissions("generation:delete")
def cancel_generation_job(job_id: str) -> tuple:
    """Cancel an in-progress or queued generation job.

    Only jobs in ``submitted`` or ``generating`` status can be cancelled.
    The cancellation request is forwarded to the Generation Engine, which
    halts batch processing and cleans up partial outputs.  The job
    transitions to ``failed`` status with ``error_message='Cancelled by
    user'``.

    Args:
        job_id: The unique job identifier (UUID string from the URL path).

    Returns:
        **200 OK** — Cancellation confirmation with updated job document.
        **404 Not Found** — If the job does not exist or belongs to
        another tenant.
        **409 Conflict** — If the job is not in a cancellable state
        (already completed, failed, or past the generating stage).
        **500 Internal Server Error** — For unexpected backend failures.
    """
    tenant_id: str = getattr(g, "tenant_id", "default")
    user_id: str = getattr(g, "user_id", "unknown")

    logger.info(
        "generation_job_cancel_request",
        job_id=job_id,
        tenant_id=tenant_id,
        user_id=user_id,
    )

    try:
        service: JobService = _get_job_service()
        result: dict[str, Any] | None = service.cancel_job(
            job_id=job_id,
            tenant_id=tenant_id,
        )

        if result is None:
            # cancel_job returns None when the job is not found OR when
            # the job is not in a cancellable state.  To distinguish the
            # two cases we perform a separate lookup.
            existing_job: dict[str, Any] | None = service.get_job(
                job_id=job_id,
                tenant_id=tenant_id,
            )

            if existing_job is None:
                logger.warning(
                    "generation_job_cancel_not_found",
                    job_id=job_id,
                    tenant_id=tenant_id,
                )
                return _build_error_response(
                    message=f"Generation job '{job_id}' not found.",
                    code="JOB_NOT_FOUND",
                    status=404,
                )

            # Job exists but is not in a cancellable state.
            current_status: str = existing_job.get("status", "unknown")
            logger.warning(
                "generation_job_cancel_not_cancellable",
                job_id=job_id,
                tenant_id=tenant_id,
                current_status=current_status,
            )
            return _build_error_response(
                message=(
                    f"Job '{job_id}' cannot be cancelled in its current "
                    f"state '{current_status}'.  Only jobs in 'submitted' "
                    f"or 'generating' state may be cancelled."
                ),
                code="JOB_NOT_CANCELLABLE",
                status=409,
            )

        response_data: dict[str, Any] = _serialize_job_to_response(result)

        logger.info(
            "generation_job_cancelled",
            job_id=job_id,
            tenant_id=tenant_id,
            user_id=user_id,
        )

        return jsonify(response_data), 200

    except Exception as exc:
        logger.error(
            "generation_job_cancel_failed",
            job_id=job_id,
            error=str(exc),
            error_type=type(exc).__name__,
            tenant_id=tenant_id,
        )
        return _build_error_response(
            message="Failed to cancel generation job.",
            code="CANCEL_JOB_FAILED",
            status=500,
        )
