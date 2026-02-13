"""Flask Blueprint module for generation job REST endpoints at /api/v1/generation/.

Implements six REST endpoints for managing synthetic data generation jobs:

    - ``POST /jobs``                    — Create a new generation job
    - ``GET /jobs``                     — List jobs with tenant-scoped pagination
    - ``GET /jobs/<job_id>``            — Retrieve a specific job
    - ``GET /jobs/<job_id>/progress``   — Real-time progress for a running job
    - ``DELETE /jobs/<job_id>``         — Cancel an in-progress job
    - ``GET /jobs/statistics``          — Tenant-scoped aggregate statistics

All endpoints require JWT authentication (``@jwt_required()``) and
permission-based RBAC enforcement (``@require_permissions()``) per security
requirement R-006.  Multi-tenant isolation is enforced via ``g.tenant_id``
on every database query per requirement R-007.

The module delegates all business logic to :class:`JobService` (Service Layer
pattern), which communicates with the Generation Engine microservice via HTTP
with circuit-breaker resilience.

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
from api_gateway.utils.pagination import PaginatedResponse, paginate_query
from shared.logging.structured_logger import get_logger


# ---------------------------------------------------------------------------
# Module-level configuration
# ---------------------------------------------------------------------------

logger = get_logger(__name__)
"""Structured JSON logger with correlation-ID propagation."""

generation_bp = Blueprint("generation", __name__)
"""Flask Blueprint grouping all generation job endpoints.

Exported as the primary module symbol.  Registered in the Application
Factory at the ``/api/v1/generation`` URL prefix.
"""


# ---------------------------------------------------------------------------
# Helper functions
# ---------------------------------------------------------------------------


def _get_job_service() -> JobService:
    """Lazily instantiate :class:`JobService` for the current request.

    :class:`JobService` reads configuration from ``current_app.config``,
    which requires an active Flask application context.  Module-level
    instantiation is therefore not possible.  This helper caches the
    instance on Flask's ``g`` request-scoped proxy to avoid repeated
    object creation during a single request.

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
    consistent field names regardless of which endpoint generated the error.

    Args:
        message: Human-readable error description.
        code: Machine-readable error code (e.g. ``"VALIDATION_ERROR"``).
        status: HTTP status code to return (e.g. 400, 404, 422, 500).
        details: Optional extra context — Pydantic field errors, trace IDs, etc.

    Returns:
        A ``(flask.Response, int)`` tuple suitable for direct Flask route return.
    """
    payload: dict[str, Any] = {
        "error": message,
        "code": code,
    }
    if details is not None:
        payload["details"] = details
    return jsonify(payload), status


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
        JSON conforming to :class:`GenerationJobRequest` schema.

    Returns:
        201 Created with :class:`GenerationJobResponse` body on success.
        400 Bad Request if the JSON body is missing.
        422 Unprocessable Entity if Pydantic validation fails.
        500 Internal Server Error for unexpected failures.

    Raises:
        No exceptions are raised directly; all errors are returned as
        structured JSON responses.
    """
    # --- Parse request body ---
    body = request.get_json(silent=True)
    if body is None:
        return _build_error_response(
            message="Request body must be valid JSON.",
            code="INVALID_JSON",
            status=400,
        )

    # --- Validate with Pydantic ---
    try:
        job_request = GenerationJobRequest.model_validate(body)
    except ValidationError as exc:
        logger.warning(
            "Generation job request validation failed",
            errors=exc.error_count(),
        )
        return _build_error_response(
            message="Request validation failed.",
            code="VALIDATION_ERROR",
            status=422,
            details=exc.errors(include_url=False),
        )

    # --- Extract context from middleware ---
    tenant_id: str = getattr(g, "tenant_id", job_request.tenant_id or "default")
    user_id: str = getattr(g, "user_id", "unknown")

    # --- Delegate to service layer ---
    try:
        service = _get_job_service()
        result = service.create_job(
            tenant_id=tenant_id,
            user_id=user_id,
            generation_method=job_request.method.value,
            schema_config={
                "schema_id": job_request.schema_id,
                "tables": [t.model_dump() for t in job_request.tables],
                "output_format": job_request.output_format.value,
                "batch_size": job_request.batch_size,
                "quality_threshold": job_request.quality_threshold,
                "template_id": job_request.template_id,
            },
            output_format=job_request.output_format.value,
            metadata=job_request.metadata,
        )

        logger.info(
            "Generation job created",
            job_id=result.get("job_id"),
            method=job_request.method.value,
            tenant_id=tenant_id,
        )

        return jsonify(result), 201

    except Exception as exc:
        logger.error(
            "Failed to create generation job",
            error=str(exc),
            tenant_id=tenant_id,
        )
        return _build_error_response(
            message="Failed to create generation job.",
            code="JOB_CREATION_FAILED",
            status=500,
        )


# ---------------------------------------------------------------------------
# GET /jobs — List generation jobs with pagination
# ---------------------------------------------------------------------------


@generation_bp.route("/jobs", methods=["GET"])
@jwt_required()
@require_permissions("generation:read")
def list_generation_jobs() -> tuple:
    """List generation jobs with tenant-scoped pagination and optional filtering.

    Supports the following query parameters:

    - ``page`` (int, default 1): Page number (1-based).
    - ``page_size`` (int, default 20, max 100): Items per page.
    - ``status`` (str, optional): Filter by job status (e.g. ``generating``).
    - ``method`` (str, optional): Filter by generation method.

    Returns:
        200 OK with :class:`GenerationJobListResponse` body.
        500 Internal Server Error for unexpected failures.
    """
    tenant_id: str = getattr(g, "tenant_id", "default")

    # --- Parse query parameters ---
    page = request.args.get("page", 1, type=int)
    page_size = request.args.get("page_size", 20, type=int)
    status_filter: Optional[str] = request.args.get("status")
    method_filter: Optional[str] = request.args.get("method")

    # Clamp page_size
    page_size = max(1, min(page_size, 100))

    try:
        service = _get_job_service()
        result = service.list_jobs(
            tenant_id=tenant_id,
            page=page,
            page_size=page_size,
            status_filter=status_filter,
        )

        logger.info(
            "Generation jobs listed",
            tenant_id=tenant_id,
            page=page,
            page_size=page_size,
            total=result.get("total", 0),
        )

        return jsonify(result), 200

    except Exception as exc:
        logger.error(
            "Failed to list generation jobs",
            error=str(exc),
            tenant_id=tenant_id,
        )
        return _build_error_response(
            message="Failed to list generation jobs.",
            code="LIST_JOBS_FAILED",
            status=500,
        )


# ---------------------------------------------------------------------------
# GET /jobs/statistics — Tenant-scoped job aggregate statistics
# ---------------------------------------------------------------------------


@generation_bp.route("/jobs/statistics", methods=["GET"])
@jwt_required()
@require_permissions("generation:read")
def get_job_statistics() -> tuple:
    """Retrieve aggregate job statistics for the current tenant.

    Returns counts grouped by status (submitted, generating, completed,
    failed, etc.) plus total job count and recent activity summary.

    This endpoint is used by the Web Console Dashboard (Screen S-001)
    to render generation activity charts.

    Returns:
        200 OK with statistics JSON body.
        500 Internal Server Error for unexpected failures.
    """
    tenant_id: str = getattr(g, "tenant_id", "default")

    try:
        service = _get_job_service()
        stats = service.get_job_statistics(tenant_id=tenant_id)

        logger.info(
            "Job statistics retrieved",
            tenant_id=tenant_id,
        )

        return jsonify(stats), 200

    except Exception as exc:
        logger.error(
            "Failed to retrieve job statistics",
            error=str(exc),
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
    to the current tenant (R-007).

    Args:
        job_id: The unique job identifier (UUID string from the URL path).

    Returns:
        200 OK with :class:`GenerationJobResponse` body on success.
        404 Not Found if the job does not exist or belongs to another tenant.
        500 Internal Server Error for unexpected failures.
    """
    tenant_id: str = getattr(g, "tenant_id", "default")

    try:
        service = _get_job_service()
        job = service.get_job(job_id=job_id, tenant_id=tenant_id)

        if job is None:
            logger.warning(
                "Generation job not found",
                job_id=job_id,
                tenant_id=tenant_id,
            )
            return _build_error_response(
                message=f"Generation job '{job_id}' not found.",
                code="JOB_NOT_FOUND",
                status=404,
            )

        logger.info(
            "Generation job retrieved",
            job_id=job_id,
            tenant_id=tenant_id,
        )

        return jsonify(job), 200

    except Exception as exc:
        logger.error(
            "Failed to retrieve generation job",
            job_id=job_id,
            error=str(exc),
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

    Progress data is served primarily from the Redis cache where
    the Generation Engine publishes updates during batch processing.
    Falls back to MongoDB for terminal states (completed/failed).

    Args:
        job_id: The unique job identifier (UUID string from the URL path).

    Returns:
        200 OK with progress JSON (percentage, records generated, status).
        404 Not Found if the job does not exist or belongs to another tenant.
        500 Internal Server Error for unexpected failures.
    """
    tenant_id: str = getattr(g, "tenant_id", "default")

    try:
        service = _get_job_service()
        job = service.get_job(job_id=job_id, tenant_id=tenant_id)

        if job is None:
            return _build_error_response(
                message=f"Generation job '{job_id}' not found.",
                code="JOB_NOT_FOUND",
                status=404,
            )

        progress_data = {
            "job_id": job_id,
            "status": job.get("status", "unknown"),
            "progress": job.get("progress", 0.0),
            "total_records": job.get("total_records", 0),
            "generated_records": job.get("generated_records", 0),
        }

        return jsonify(progress_data), 200

    except Exception as exc:
        logger.error(
            "Failed to retrieve job progress",
            job_id=job_id,
            error=str(exc),
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
    halts batch processing and cleans up partial outputs.

    Args:
        job_id: The unique job identifier (UUID string from the URL path).

    Returns:
        200 OK with cancellation confirmation and updated job status.
        404 Not Found if the job does not exist or belongs to another tenant.
        409 Conflict if the job is not in a cancellable state.
        500 Internal Server Error for unexpected failures.
    """
    tenant_id: str = getattr(g, "tenant_id", "default")

    try:
        service = _get_job_service()
        result = service.cancel_job(job_id=job_id, tenant_id=tenant_id)

        if result is None:
            return _build_error_response(
                message=f"Generation job '{job_id}' not found.",
                code="JOB_NOT_FOUND",
                status=404,
            )

        if result.get("error") == "not_cancellable":
            return _build_error_response(
                message=f"Job '{job_id}' cannot be cancelled in its current state.",
                code="JOB_NOT_CANCELLABLE",
                status=409,
            )

        logger.info(
            "Generation job cancelled",
            job_id=job_id,
            tenant_id=tenant_id,
        )

        return jsonify(result), 200

    except Exception as exc:
        logger.error(
            "Failed to cancel generation job",
            job_id=job_id,
            error=str(exc),
            tenant_id=tenant_id,
        )
        return _build_error_response(
            message="Failed to cancel generation job.",
            code="CANCEL_JOB_FAILED",
            status=500,
        )
