"""Flask Blueprint module for statistical profile REST endpoints at /api/v1/profiles/.

Implements five REST endpoints for managing statistical profiles produced by the
Profiling Service microservice:

    - ``POST /``                         — Initiate profiling against an ERP source
    - ``GET /``                          — List profiles with tenant-scoped pagination
    - ``GET /<profile_id>``              — Retrieve a specific profile
    - ``GET /<profile_id>/statistics``   — Detailed statistical distributions per column
    - ``DELETE /<profile_id>``           — Delete a profile record

All endpoints require JWT authentication (``@jwt_required()``) and permission-based
RBAC enforcement (``@require_permissions()``) per security requirement R-006.
Multi-tenant isolation is enforced via ``g.tenant_id`` on every database query per
requirement R-007.

No raw production data flows through these endpoints — only schema metadata and
statistical distributions are captured and served, enforcing Constraint C-001.

The module delegates all business logic to :class:`ProfileService` (Service Layer
pattern), which communicates with the Profiling Service microservice via HTTP
with circuit-breaker resilience.

Typical usage::

    # In app.py (Application Factory):
    from api_gateway.routes.profiles import profiles_bp

    app.register_blueprint(profiles_bp, url_prefix="/api/v1/profiles")
"""

from __future__ import annotations

from typing import Any, Optional

from flask import Blueprint, g, jsonify, request
from flask_jwt_extended import jwt_required
from pydantic import ValidationError

from api_gateway.middleware.auth import require_permissions, require_roles
from api_gateway.schemas.profile import (
    ProfileRequest,
    ProfileResponse,
    StatisticalSummary,
)
from api_gateway.services.profile_service import ProfileService
from api_gateway.utils.pagination import PaginatedResponse, paginate_query
from shared.logging.structured_logger import get_logger


# ---------------------------------------------------------------------------
# Module-level configuration
# ---------------------------------------------------------------------------

logger = get_logger(__name__)
"""Structured JSON logger with correlation-ID propagation."""

profiles_bp = Blueprint("profiles", __name__)
"""Flask Blueprint grouping all statistical profile endpoints.

Exported as the primary module symbol.  Registered in the Application
Factory at the ``/api/v1/profiles`` URL prefix.
"""

_COLLECTION_NAME: str = "statistical_profiles"
"""MongoDB collection name — must stay in sync with ProfileService."""

_STATUS_MAP: dict[str, str] = {
    "pending": "pending",
    "profiling": "in_progress",
    "dispatch_failed": "failed",
    "completed": "completed",
    "in_progress": "in_progress",
    "failed": "failed",
}
"""Maps internal service statuses to :class:`ProfileResponse`-compatible values.

:class:`ProfileResponse` accepts only ``pending | in_progress | completed | failed``.
The Profiling Service may return additional transitional states like ``profiling``
or ``dispatch_failed`` that must be normalised before serialisation.
"""


# ---------------------------------------------------------------------------
# Helper functions
# ---------------------------------------------------------------------------


def _get_profile_service() -> ProfileService:
    """Lazily instantiate :class:`ProfileService` for the current request.

    :class:`ProfileService.__init__` reads ``PROFILING_SERVICE_URL`` from
    ``current_app.config``, which requires an active Flask application
    context.  Module-level instantiation is therefore not possible.  This
    helper caches the instance on Flask's ``g`` request-scoped proxy to
    avoid repeated object creation during a single request.

    Returns:
        A :class:`ProfileService` instance bound to the current Flask
        application context.
    """
    if not hasattr(g, "_profile_service"):
        g._profile_service = ProfileService()
    return g._profile_service


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


def _map_status(raw_status: str) -> str:
    """Normalise an internal service status to a ProfileResponse-compatible value.

    Args:
        raw_status: Raw status string from the service layer or MongoDB document.

    Returns:
        A status string accepted by :class:`ProfileResponse`'s field validator.
    """
    return _STATUS_MAP.get(raw_status, "pending")


# ---------------------------------------------------------------------------
# POST / — Create a new statistical profile
# ---------------------------------------------------------------------------


@profiles_bp.route("", methods=["POST"])
@jwt_required()
@require_permissions("profile:create")
def create_profile() -> tuple:
    """Create a new statistical profiling request against an ERP source.

    Validates the incoming JSON body with :class:`ProfileRequest`, extracts
    tenant and user identity from the JWT-populated ``flask.g`` object, and
    delegates the actual profiling job to :class:`ProfileService`.  The service
    dispatches an asynchronous task to the Profiling Service microservice
    (via HTTP with circuit-breaker resilience) and returns a ``profile_id``
    for future polling.

    **Constraint C-001** — Only schema *metadata* is extracted from the ERP
    source.  No raw production data is accessed or stored.

    Request Body (JSON):
        See :class:`ProfileRequest` for the full schema.  Key fields:

        - ``source_connection`` *(required)* — ERP connection parameters
        - ``erp_type`` *(required)* — ``sap``, ``oracle_ebs``, ``dynamics``,
          or ``legacy``
        - ``discovery_scope`` *(optional)* — table names to profile; empty
          triggers full schema discovery
        - ``erp_module`` *(optional)* — module-level scope filter
        - ``sample_size`` *(optional)* — 100 – 1,000,000

    Returns:
        - **201** — Profile created as :class:`ProfileResponse` JSON.
        - **400** — Missing / malformed request body.
        - **403** — Insufficient permissions.
        - **422** — Pydantic validation failure with field-level details.
        - **500** — Unexpected server error.
    """
    tenant_id: str = getattr(g, "tenant_id", "")
    user_id: str = getattr(g, "user_id", "")

    logger.info(
        "profile_create_request_received",
        tenant_id=tenant_id,
        user_id=user_id,
        method=request.method,
        path=request.path,
    )

    # ------------------------------------------------------------------
    # 1. Parse request body
    # ------------------------------------------------------------------
    request_json: dict[str, Any] | None = request.get_json(silent=True)
    if not request_json:
        logger.warning(
            "profile_create_missing_body",
            tenant_id=tenant_id,
            user_id=user_id,
        )
        return _build_error_response(
            "Request body is required and must be valid JSON",
            "INVALID_REQUEST",
            400,
        )

    # ------------------------------------------------------------------
    # 2. Validate against Pydantic ProfileRequest model
    # ------------------------------------------------------------------
    try:
        profile_request = ProfileRequest(**request_json)
    except ValidationError as exc:
        logger.warning(
            "profile_create_validation_error",
            tenant_id=tenant_id,
            user_id=user_id,
            error_count=len(exc.errors()),
        )
        # Sanitise Pydantic v2 error dicts so they are JSON-serialisable.
        # ``exc.errors()`` may contain non-serialisable objects such as
        # ``PydanticUndefined`` in the ``input`` or ``ctx`` fields.
        sanitised_errors: list[dict[str, Any]] = []
        for err in exc.errors():
            sanitised: dict[str, Any] = {
                "type": err.get("type", "unknown"),
                "loc": list(err.get("loc", ())),
                "msg": err.get("msg", ""),
            }
            # Only include ``input`` if it is a simple JSON-safe type
            raw_input = err.get("input")
            if isinstance(raw_input, (str, int, float, bool, list, dict, type(None))):
                sanitised["input"] = raw_input
            sanitised_errors.append(sanitised)

        return _build_error_response(
            "Request validation failed",
            "VALIDATION_ERROR",
            422,
            details=sanitised_errors,
        )

    # ------------------------------------------------------------------
    # 3. Build service-layer payload from validated data
    # ------------------------------------------------------------------
    validated_data: dict[str, Any] = profile_request.model_dump()

    source_connection: dict[str, Any] = {
        "erp_type": validated_data["erp_type"],
        "connection_params": {
            "connection_type": validated_data["source_connection"]["connection_type"],
            "host": validated_data["source_connection"]["host"],
            "port": validated_data["source_connection"]["port"],
            "database": validated_data["source_connection"].get("database"),
            "username": validated_data["source_connection"]["username"],
            "additional_params": validated_data["source_connection"].get(
                "additional_params"
            ),
        },
    }

    # Map discovery_scope → tables list; empty means full schema discovery
    tables: list[str] = validated_data.get("discovery_scope") or ["*"]

    # ------------------------------------------------------------------
    # 4. Delegate to ProfileService
    # ------------------------------------------------------------------
    try:
        profile_service = _get_profile_service()
        result: dict[str, Any] = profile_service.create_profile(
            tenant_id=tenant_id,
            user_id=user_id,
            source_connection=source_connection,
            tables=tables,
        )

        # Serialise through ProfileResponse for a consistent wire format
        response = ProfileResponse.model_validate(
            {
                "profile_id": result["profile_id"],
                "erp_type": result.get("erp_type", validated_data.get("erp_type", "")),
                "erp_module": validated_data.get("erp_module"),
                "tables": [],
                "total_tables": 0,
                "total_columns": 0,
                "created_at": result["created_at"],
                "updated_at": result["updated_at"],
                "status": _map_status(result.get("status", "pending")),
                "tenant_id": result.get("tenant_id", tenant_id),
            }
        )

        logger.info(
            "profile_created_successfully",
            tenant_id=tenant_id,
            user_id=user_id,
            profile_id=result.get("profile_id", ""),
            status=result.get("status", ""),
        )

        return jsonify(response.model_dump(mode="json")), 201

    except ValueError as exc:
        logger.warning(
            "profile_create_value_error",
            tenant_id=tenant_id,
            user_id=user_id,
            error=str(exc),
        )
        return _build_error_response(str(exc), "INVALID_REQUEST", 400)

    except Exception as exc:
        logger.error(
            "profile_create_internal_error",
            tenant_id=tenant_id,
            user_id=user_id,
            error=str(exc),
            error_type=type(exc).__name__,
        )
        return _build_error_response(
            "An internal error occurred while creating the profile",
            "INTERNAL_ERROR",
            500,
        )


# ---------------------------------------------------------------------------
# GET / — List profiles with pagination
# ---------------------------------------------------------------------------


@profiles_bp.route("", methods=["GET"])
@jwt_required()
@require_permissions("profile:read")
def list_profiles() -> tuple:
    """List statistical profiles with tenant-scoped pagination.

    Supports two pagination modes selected automatically by query parameter
    presence:

    - **Offset-based** (default) — uses ``page`` and ``page_size`` query
      parameters, delegated to :meth:`ProfileService.list_profiles`.
    - **Cursor-based** — uses a ``cursor`` token with ``page_size``, routed
      through :func:`paginate_query` for efficient deep pagination.

    Results are always scoped to the authenticated user's tenant per R-007.

    Query Parameters:
        erp_type (str, optional): Filter by ERP type.
        erp_module (str, optional): Filter by ERP module (cursor mode).
        page (int, optional): Page number for offset mode (default ``1``).
        page_size (int, optional): Items per page (default ``20``, max ``100``).
        cursor (str, optional): Opaque cursor token for cursor-based mode.
        direction (str, optional): ``forward`` | ``backward`` (cursor mode).

    Returns:
        - **200** — Paginated list of profiles as :class:`PaginatedResponse`.
        - **400** — Invalid query parameters.
        - **403** — Insufficient permissions.
        - **500** — Internal server error.
    """
    tenant_id: str = getattr(g, "tenant_id", "")
    erp_type: Optional[str] = request.args.get("erp_type")
    erp_module: Optional[str] = request.args.get("erp_module")
    cursor: Optional[str] = request.args.get("cursor")
    direction: str = request.args.get("direction", "forward")
    page: int = request.args.get("page", 1, type=int)
    page_size: int = request.args.get("page_size", 20, type=int)

    # Clamp page_size to reasonable bounds
    page_size = max(1, min(page_size, 100))

    logger.info(
        "profile_list_request",
        tenant_id=tenant_id,
        erp_type=erp_type,
        erp_module=erp_module,
        cursor_mode=bool(cursor),
        page=page,
        page_size=page_size,
    )

    try:
        if cursor:
            # ----------------------------------------------------------
            # Cursor-based pagination via paginate_query
            # ----------------------------------------------------------
            from api_gateway.extensions import get_db  # noqa: WPS433

            db = get_db()
            collection = db[_COLLECTION_NAME]

            query_filter: dict[str, Any] = {"tenant_id": tenant_id}
            if erp_type:
                query_filter["erp_type"] = erp_type
            if erp_module:
                query_filter["erp_module"] = erp_module

            paginated_result: PaginatedResponse = paginate_query(
                collection=collection,
                query_filter=query_filter,
                page_size=page_size,
                cursor=cursor,
                direction=direction,
                sort_field="created_at",
                sort_order=-1,
            )

            logger.info(
                "profile_list_cursor_success",
                tenant_id=tenant_id,
                total_count=paginated_result.total_count,
                returned=len(paginated_result.items),
                has_more=paginated_result.has_more,
            )

            return jsonify(paginated_result.model_dump()), 200

        # ----------------------------------------------------------
        # Offset-based pagination via ProfileService
        # ----------------------------------------------------------
        profile_service = _get_profile_service()
        result: dict[str, Any] = profile_service.list_profiles(
            tenant_id=tenant_id,
            erp_type=erp_type,
            page=page,
            page_size=page_size,
        )

        # Wrap service result in PaginatedResponse for a unified
        # response envelope across both pagination modes.
        paginated = PaginatedResponse(
            items=result.get("items", []),
            total_count=result.get("total", 0),
            has_more=result.get("has_next", False),
            page_size=result.get("page_size", page_size),
        )

        logger.info(
            "profile_list_offset_success",
            tenant_id=tenant_id,
            total=result.get("total", 0),
            page=result.get("page", page),
            returned=len(result.get("items", [])),
        )

        return jsonify(paginated.model_dump()), 200

    except ValueError as exc:
        logger.warning(
            "profile_list_invalid_params",
            tenant_id=tenant_id,
            error=str(exc),
        )
        return _build_error_response(str(exc), "INVALID_REQUEST", 400)

    except Exception as exc:
        logger.error(
            "profile_list_internal_error",
            tenant_id=tenant_id,
            error=str(exc),
            error_type=type(exc).__name__,
        )
        return _build_error_response(
            "An internal error occurred while listing profiles",
            "INTERNAL_ERROR",
            500,
        )


# ---------------------------------------------------------------------------
# GET /<profile_id> — Retrieve a specific profile
# ---------------------------------------------------------------------------


@profiles_bp.route("/<string:profile_id>", methods=["GET"])
@jwt_required()
@require_permissions("profile:read")
def get_profile(profile_id: str) -> tuple:
    """Retrieve a single statistical profile by its unique identifier.

    Returns the full profile document including table metadata, column
    profiles, and aggregate counts.  The response is automatically scoped
    to the authenticated user's tenant (R-007) — profiles belonging to
    other tenants are invisible and return 404.

    Path Parameters:
        profile_id: Unique identifier of the profile to retrieve.

    Returns:
        - **200** — Profile as :class:`ProfileResponse` JSON.
        - **403** — Insufficient permissions.
        - **404** — Profile not found for this tenant.
        - **500** — Internal server error.
    """
    tenant_id: str = getattr(g, "tenant_id", "")
    user_id: str = getattr(g, "user_id", "")

    logger.info(
        "profile_get_request",
        profile_id=profile_id,
        tenant_id=tenant_id,
        user_id=user_id,
    )

    try:
        profile_service = _get_profile_service()
        profile: dict[str, Any] | None = profile_service.get_profile(
            profile_id=profile_id,
            tenant_id=tenant_id,
        )

        if profile is None:
            logger.info(
                "profile_get_not_found",
                profile_id=profile_id,
                tenant_id=tenant_id,
            )
            return _build_error_response(
                f"Profile '{profile_id}' not found",
                "NOT_FOUND",
                404,
            )

        # ---------------------------------------------------------------
        # Serialise through ProfileResponse for wire-format consistency.
        # The raw service dict may carry tables as plain strings (pending
        # profiles) or nested dicts (completed profiles).  We normalise
        # the shape before handing it to Pydantic.
        # ---------------------------------------------------------------
        try:
            tables_data = profile.get("tables", [])
            if tables_data and isinstance(tables_data[0], str):
                # Profile is still pending — tables are plain name strings
                response_data: dict[str, Any] = {
                    **profile,
                    "tables": [],
                    "total_tables": len(tables_data),
                }
            else:
                response_data = dict(profile)

            # Normalise status for ProfileResponse's field validator
            if "status" in response_data:
                response_data["status"] = _map_status(
                    response_data.get("status", "pending")
                )

            response = ProfileResponse.model_validate(response_data)
            serialized: dict[str, Any] = response.model_dump(mode="json")
        except (ValidationError, Exception):
            # Graceful fallback — return the raw service dict so that the
            # client still receives useful data even if Pydantic validation
            # encounters an unexpected field shape.
            serialized = profile

        logger.info(
            "profile_get_success",
            profile_id=profile_id,
            tenant_id=tenant_id,
            status=profile.get("status", ""),
        )

        return jsonify(serialized), 200

    except Exception as exc:
        logger.error(
            "profile_get_internal_error",
            profile_id=profile_id,
            tenant_id=tenant_id,
            error=str(exc),
            error_type=type(exc).__name__,
        )
        return _build_error_response(
            "An internal error occurred while retrieving the profile",
            "INTERNAL_ERROR",
            500,
        )


# ---------------------------------------------------------------------------
# GET /<profile_id>/statistics — Detailed statistical distributions
# ---------------------------------------------------------------------------


@profiles_bp.route("/<string:profile_id>/statistics", methods=["GET"])
@jwt_required()
@require_permissions("profile:read")
def get_profile_statistics(profile_id: str) -> tuple:
    """Retrieve per-column statistical distributions for a profile.

    Returns detailed statistics for every profiled column, including mean,
    median, standard deviation, min/max values, distribution type, and null
    percentage.  This data powers the **Profile Viewer** screen (S-006) in
    the Web Console.

    Only statistical **metadata** is returned — no raw production data
    leaves the Profiling Service (Constraint C-001).

    Path Parameters:
        profile_id: Unique profile identifier.

    Returns:
        - **200** — Column-level statistics list (per
          :class:`StatisticalSummary`).
        - **403** — Insufficient permissions.
        - **404** — Profile or statistics not available.
        - **500** — Internal server error.
    """
    tenant_id: str = getattr(g, "tenant_id", "")
    user_id: str = getattr(g, "user_id", "")

    logger.info(
        "profile_statistics_request",
        profile_id=profile_id,
        tenant_id=tenant_id,
        user_id=user_id,
    )

    try:
        profile_service = _get_profile_service()
        stats_result: dict[str, Any] | None = (
            profile_service.get_profile_statistics(
                profile_id=profile_id,
                tenant_id=tenant_id,
            )
        )

        if stats_result is None:
            logger.info(
                "profile_statistics_not_found",
                profile_id=profile_id,
                tenant_id=tenant_id,
            )
            return _build_error_response(
                f"Statistics for profile '{profile_id}' not found",
                "NOT_FOUND",
                404,
            )

        # ---------------------------------------------------------------
        # Validate each column's statistics through StatisticalSummary
        # so the output conforms to the documented schema.
        # ---------------------------------------------------------------
        column_stats_raw: list[dict[str, Any]] = stats_result.get(
            "column_statistics", []
        )
        validated_column_stats: list[dict[str, Any]] = []

        for col_stat in column_stats_raw:
            try:
                summary = StatisticalSummary.model_validate(
                    {
                        "column_name": col_stat.get("column_name", ""),
                        "data_type": col_stat.get("data_type", "unknown"),
                        "distribution_type": col_stat.get(
                            "distribution_type", "unknown"
                        ),
                        "mean": col_stat.get("mean"),
                        "median": col_stat.get("median"),
                        "std_dev": col_stat.get("std_dev"),
                        "min_value": col_stat.get(
                            "min_value", col_stat.get("min")
                        ),
                        "max_value": col_stat.get(
                            "max_value", col_stat.get("max")
                        ),
                        "null_percentage": float(
                            col_stat.get("null_percentage", 0.0)
                        ),
                        "unique_count": col_stat.get(
                            "unique_count", col_stat.get("distinct_count")
                        ),
                        "sample_values": col_stat.get("sample_values"),
                        "pattern": col_stat.get("pattern"),
                    }
                )
                validated_column_stats.append(summary.model_dump())
            except (ValidationError, Exception) as stat_err:
                # Include the raw stat dict when Pydantic rejects the shape
                # so the caller still gets maximum available data.
                logger.warning(
                    "profile_statistics_column_validation_warning",
                    profile_id=profile_id,
                    column_name=col_stat.get("column_name", "unknown"),
                    error=str(stat_err),
                )
                validated_column_stats.append(col_stat)

        response: dict[str, Any] = {
            "profile_id": stats_result.get("profile_id", profile_id),
            "tenant_id": stats_result.get("tenant_id", tenant_id),
            "erp_type": stats_result.get("erp_type", ""),
            "status": stats_result.get("status", ""),
            "table_count": stats_result.get("table_count", 0),
            "tables": stats_result.get("tables", []),
            "column_statistics": validated_column_stats,
            "created_at": stats_result.get("created_at"),
            "updated_at": stats_result.get("updated_at"),
        }

        logger.info(
            "profile_statistics_success",
            profile_id=profile_id,
            tenant_id=tenant_id,
            column_count=len(validated_column_stats),
            table_count=stats_result.get("table_count", 0),
        )

        return jsonify(response), 200

    except Exception as exc:
        logger.error(
            "profile_statistics_internal_error",
            profile_id=profile_id,
            tenant_id=tenant_id,
            error=str(exc),
            error_type=type(exc).__name__,
        )
        return _build_error_response(
            "An internal error occurred while retrieving profile statistics",
            "INTERNAL_ERROR",
            500,
        )


# ---------------------------------------------------------------------------
# DELETE /<profile_id> — Delete a profile
# ---------------------------------------------------------------------------


@profiles_bp.route("/<string:profile_id>", methods=["DELETE"])
@jwt_required()
@require_permissions("profile:create")
def delete_profile(profile_id: str) -> tuple:
    """Delete a statistical profile by its unique identifier.

    Removes the profile document from MongoDB and invalidates the
    corresponding Redis cache entry via :class:`ProfileService`.  The
    operation is scoped to the authenticated user's tenant (R-007) —
    profiles belonging to other tenants cannot be deleted and return 404.

    The ``profile:create`` permission is required (matching the principle
    that creators are allowed to destroy their own resources).

    Path Parameters:
        profile_id: Unique identifier of the profile to delete.

    Returns:
        - **204** — Profile deleted successfully (no response body).
        - **403** — Insufficient permissions.
        - **404** — Profile not found for this tenant.
        - **500** — Internal server error.
    """
    tenant_id: str = getattr(g, "tenant_id", "")
    user_id: str = getattr(g, "user_id", "")

    logger.info(
        "profile_delete_request",
        profile_id=profile_id,
        tenant_id=tenant_id,
        user_id=user_id,
    )

    try:
        profile_service = _get_profile_service()
        deleted: bool = profile_service.delete_profile(
            profile_id=profile_id,
            tenant_id=tenant_id,
        )

        if not deleted:
            logger.info(
                "profile_delete_not_found",
                profile_id=profile_id,
                tenant_id=tenant_id,
            )
            return _build_error_response(
                f"Profile '{profile_id}' not found",
                "NOT_FOUND",
                404,
            )

        logger.info(
            "profile_deleted_successfully",
            profile_id=profile_id,
            tenant_id=tenant_id,
            user_id=user_id,
        )

        return "", 204

    except Exception as exc:
        logger.error(
            "profile_delete_internal_error",
            profile_id=profile_id,
            tenant_id=tenant_id,
            error=str(exc),
            error_type=type(exc).__name__,
        )
        return _build_error_response(
            "An internal error occurred while deleting the profile",
            "INTERNAL_ERROR",
            500,
        )
