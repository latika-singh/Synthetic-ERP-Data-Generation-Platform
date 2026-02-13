"""Flask Blueprint module for export and provisioning trigger REST endpoints.

Provides the ``export_bp`` Blueprint registered at ``/api/v1/export/`` that
exposes endpoints for triggering, monitoring, downloading, and cancelling
synthetic data exports.  Supports multi-format output (SQL, CSV, JSON,
Parquet), database provisioning (PostgreSQL, Oracle, SQL Server, SAP HANA),
and cloud storage upload (AWS S3, Azure Blob, GCP Cloud Storage).

All endpoints require JWT authentication and RBAC permission enforcement:

- ``export:create`` — POST (trigger export), DELETE (cancel export).
- ``export:read``   — GET (list exports, get details, get download URL).

The Blueprint delegates all business logic to :class:`ExportService` and
validates request payloads with Pydantic v2 schemas.

Security Notes:
    - Connection strings and cloud credentials are **never** logged (R-005).
    - AES-256 encryption at rest is enforced via the ``encrypt`` field in
      :class:`ExportRequest` and the ``encrypt_at_rest`` flag in every
      Provisioning Service dispatch (R-006).
    - Multi-tenant isolation is enforced by extracting ``tenant_id`` from
      the Flask ``g`` context on every request (R-007).

Usage::

    from api_gateway.routes.export import export_bp

    app.register_blueprint(export_bp, url_prefix="/api/v1/export")
"""

from __future__ import annotations

from typing import Any, Optional

from flask import Blueprint, g, jsonify, request
from flask_jwt_extended import jwt_required
from pydantic import ValidationError

from api_gateway.middleware.auth import require_permissions
from api_gateway.schemas.export import (
    DestinationType,
    ExportFormat,
    ExportRequest,
    ExportResponse,
    ExportStatus,
    ProvisioningConfig,
)
from api_gateway.services.export_service import ExportService
from api_gateway.utils.pagination import validate_page_size
from shared.logging.structured_logger import get_logger

# ---------------------------------------------------------------------------
# Module-level setup
# ---------------------------------------------------------------------------

logger = get_logger(__name__)

export_bp = Blueprint("export", __name__)

# Mapping from API-facing DestinationType enum values to the internal
# export_type identifiers expected by ExportService.  The service uses
# "file_download" whereas the public API schema uses "local_file".
_DESTINATION_TO_EXPORT_TYPE: dict[str, str] = {
    DestinationType.DATABASE.value: "database",
    DestinationType.CLOUD_STORAGE.value: "cloud_storage",
    DestinationType.LOCAL_FILE.value: "file_download",
}

# Reverse mapping for converting ExportService results back to the
# API-facing DestinationType values used in ExportResponse.
_EXPORT_TYPE_TO_DESTINATION: dict[str, str] = {
    v: k for k, v in _DESTINATION_TO_EXPORT_TYPE.items()
}

# Mapping between ExportService internal statuses and API schema statuses.
# The service layer uses "exporting" while the public API schema uses
# "in_progress" for the active processing state.
_SERVICE_STATUS_TO_API: dict[str, str] = {
    "pending": ExportStatus.PENDING.value,
    "exporting": ExportStatus.IN_PROGRESS.value,
    "completed": ExportStatus.COMPLETED.value,
    "failed": ExportStatus.FAILED.value,
}

# Reverse mapping for converting API status filter values to service-layer
# values when querying the backend (e.g. list_exports with status filter).
_API_STATUS_TO_SERVICE: dict[str, str] = {
    v: k for k, v in _SERVICE_STATUS_TO_API.items()
}


# ---------------------------------------------------------------------------
# Private helpers
# ---------------------------------------------------------------------------


def _get_export_service() -> ExportService:
    """Lazily instantiate the ExportService within the Flask app context.

    The :class:`ExportService` constructor reads
    ``current_app.config['PROVISIONING_SERVICE_URL']``, so it must be
    created inside an active Flask application context.  This function is
    called at the start of each request handler rather than at module
    import time.

    Returns:
        A new :class:`ExportService` instance bound to the current app.
    """
    return ExportService()


def _build_target_config(validated_request: ExportRequest) -> dict[str, Any]:
    """Transform a validated ExportRequest into the target_config dict.

    Maps the structured Pydantic configuration models into the flat
    dictionary format expected by :meth:`ExportService.create_export`.

    Args:
        validated_request: The validated :class:`ExportRequest` instance.

    Returns:
        A dictionary containing the export target configuration.  Secrets
        such as passwords and credential objects are included in the dict
        so that the service layer can forward them to the Provisioning
        Service, but they are **never** logged (R-005).
    """
    dest = validated_request.destination_type

    if dest == DestinationType.DATABASE and validated_request.provisioning_config:
        prov_cfg = ProvisioningConfig.model_validate(
            validated_request.provisioning_config.model_dump()
        )
        db_cfg = prov_cfg.database_config
        if db_cfg is None:
            return {}
        db_type_value = db_cfg.database_type.value
        # Construct a JDBC-style connection string from structured fields.
        connection_string = (
            f"jdbc:{db_type_value}://{db_cfg.host}:{db_cfg.port}/{db_cfg.database}"
        )
        return {
            "db_type": db_type_value,
            "connection_string": connection_string,
            "schema": db_cfg.schema_name or "public",
            "table_prefix": "",
            "host": db_cfg.host,
            "port": db_cfg.port,
            "database": db_cfg.database,
            "username": db_cfg.username,
            "password": db_cfg.password,
            "jdbc_driver": db_cfg.jdbc_driver,
            "connection_params": db_cfg.connection_params or {},
            "batch_size": db_cfg.batch_size,
        }

    if dest == DestinationType.CLOUD_STORAGE and validated_request.provisioning_config:
        prov_cfg = ProvisioningConfig.model_validate(
            validated_request.provisioning_config.model_dump()
        )
        cloud_cfg = prov_cfg.cloud_config
        if cloud_cfg is None:
            return {}
        return {
            "cloud_provider": cloud_cfg.provider.value,
            "bucket": cloud_cfg.bucket_name,
            "path_prefix": cloud_cfg.path_prefix or "",
            "credentials_ref": cloud_cfg.credentials or {},
            "region": cloud_cfg.region,
            "encryption_enabled": cloud_cfg.encryption_enabled,
        }

    # LOCAL_FILE / file_download — minimal configuration.
    return {
        "compress": validated_request.compress,
        "encrypt": validated_request.encrypt,
        "include_schema_ddl": validated_request.include_schema_ddl,
    }


def _build_export_response(export_doc: dict[str, Any]) -> dict[str, Any]:
    """Map an ExportService document into an ExportResponse-compatible dict.

    Translates field names and enum values between the internal service
    representation and the API-facing response schema.  Handles the
    status mapping (service ``"exporting"`` → API ``"in_progress"``) and
    destination type mapping (service ``"file_download"`` → API
    ``"local_file"``).

    Args:
        export_doc: Raw document from :class:`ExportService`.

    Returns:
        A JSON-serialisable dictionary matching the :class:`ExportResponse`
        schema.
    """
    # Map internal export_type back to API-facing destination_type.
    raw_export_type = export_doc.get("export_type", "")
    destination_type = _EXPORT_TYPE_TO_DESTINATION.get(
        raw_export_type, raw_export_type,
    )

    # Map internal status to API-facing status.
    raw_status = export_doc.get("status", "")
    api_status = _SERVICE_STATUS_TO_API.get(raw_status, raw_status)

    response = ExportResponse(
        export_id=export_doc.get("export_id", ""),
        job_id=export_doc.get("job_id", ""),
        status=api_status,
        format=export_doc.get("output_format", ExportFormat.CSV.value),
        destination_type=destination_type,
        download_url=export_doc.get("download_url"),
        file_size_bytes=export_doc.get("file_size_bytes"),
        record_count=export_doc.get("records_exported", 0),
        error_message=export_doc.get("error_message"),
        started_at=export_doc.get("created_at"),
        completed_at=export_doc.get("completed_at"),
        tenant_id=export_doc.get("tenant_id"),
    )
    return response.model_dump(mode="json")


# ---------------------------------------------------------------------------
# Route Handlers
# ---------------------------------------------------------------------------


@export_bp.route("", methods=["POST"])
@jwt_required()
@require_permissions("export:create")
def create_export():
    """Trigger a new export of generated synthetic data.

    Accepts a JSON body specifying the source generation job, output
    format, destination type, and optional provisioning configuration.
    The request is validated against :class:`ExportRequest`, transformed
    into the internal service format, and delegated to
    :meth:`ExportService.create_export`.

    Supported destinations:

    - **database** — Direct JDBC provisioning to PostgreSQL, Oracle,
      SQL Server, or SAP HANA.
    - **cloud_storage** — Upload to AWS S3, Azure Blob, or GCP Storage.
    - **local_file** — Generate a downloadable file on the platform.

    Supported formats: ``sql``, ``csv``, ``json``, ``parquet``.

    Request Body (JSON):
        See :class:`ExportRequest` for full schema.

    Returns:
        tuple: A JSON-serialised :class:`ExportResponse` with HTTP 202
        (Accepted), 400 if the body is missing, 422 (Unprocessable
        Entity) on validation failure, or 500 for unexpected exceptions.
    """
    tenant_id: str = g.tenant_id
    user_id: str = g.user_id

    # ------------------------------------------------------------------
    # Parse and validate the request body
    # ------------------------------------------------------------------
    body = request.get_json(silent=True)
    if body is None:
        logger.warning(
            "export_create_missing_body",
            tenant_id=tenant_id,
            user_id=user_id,
        )
        return jsonify({
            "error": "Request body is required",
            "code": "missing_body",
        }), 400

    try:
        validated: ExportRequest = ExportRequest.model_validate(body)
    except ValidationError as exc:
        logger.warning(
            "export_create_validation_error",
            tenant_id=tenant_id,
            user_id=user_id,
            errors=exc.errors(),
        )
        return jsonify({
            "error": "Validation failed",
            "code": "validation_error",
            "details": exc.errors(),
        }), 422

    # ------------------------------------------------------------------
    # Map validated schema to internal service parameters
    # ------------------------------------------------------------------
    export_type: str = _DESTINATION_TO_EXPORT_TYPE.get(
        validated.destination_type.value,
        validated.destination_type.value,
    )
    target_config: dict[str, Any] = _build_target_config(validated)
    output_format: str = validated.format.value

    # Log the request without any sensitive fields (R-005).
    logger.info(
        "export_create_request",
        tenant_id=tenant_id,
        user_id=user_id,
        job_id=validated.job_id,
        format=output_format,
        destination=export_type,
        encrypt=validated.encrypt,
        compress=validated.compress,
    )

    # ------------------------------------------------------------------
    # Delegate to service layer
    # ------------------------------------------------------------------
    try:
        export_service: ExportService = _get_export_service()
        export_doc: dict[str, Any] = export_service.create_export(
            tenant_id=tenant_id,
            user_id=user_id,
            job_id=validated.job_id,
            export_type=export_type,
            target_config=target_config,
            output_format=output_format,
        )
    except ValueError as exc:
        logger.warning(
            "export_create_value_error",
            tenant_id=tenant_id,
            user_id=user_id,
            error=str(exc),
        )
        return jsonify({
            "error": str(exc),
            "code": "invalid_export_config",
        }), 422
    except Exception as exc:
        logger.error(
            "export_create_failed",
            tenant_id=tenant_id,
            user_id=user_id,
            error=str(exc),
            error_type=type(exc).__name__,
        )
        return jsonify({
            "error": "Failed to create export",
            "code": "internal_error",
        }), 500

    response_data: dict[str, Any] = _build_export_response(export_doc)

    logger.info(
        "export_created",
        tenant_id=tenant_id,
        user_id=user_id,
        export_id=export_doc.get("export_id"),
        format=output_format,
        destination=export_type,
    )

    return jsonify(response_data), 202


@export_bp.route("", methods=["GET"])
@jwt_required()
@require_permissions("export:read")
def list_exports():
    """List export records with tenant-scoped filtering and pagination.

    Supports filtering by ``job_id``, ``status``, and ``format`` via
    query parameters.  Pagination is controlled via ``page`` and
    ``page_size`` query parameters with a maximum page size of 100.

    Query Parameters:
        job_id (str, optional): Filter by source generation job ID.
        status (str, optional): Filter by export status
            (pending, in_progress, completed, failed).
        format (str, optional): Filter by output format
            (sql, csv, json, parquet).
        page (int, optional): 1-based page number (default: 1).
        page_size (int, optional): Records per page (default: 20,
            max: 100).

    Returns:
        tuple: A JSON object with ``items``, ``total``, ``page``,
        ``page_size``, and ``has_next`` fields with HTTP 200, or
        500 for unexpected exceptions.
    """
    tenant_id: str = g.tenant_id

    # ------------------------------------------------------------------
    # Parse optional filter query parameters
    # ------------------------------------------------------------------
    job_id: Optional[str] = request.args.get("job_id")
    status_filter: Optional[str] = request.args.get("status")
    format_filter: Optional[str] = request.args.get("format")

    # Convert API-facing status value to internal service status for the
    # query (e.g. "in_progress" → "exporting").
    service_status: Optional[str] = None
    if status_filter is not None:
        service_status = _API_STATUS_TO_SERVICE.get(
            status_filter, status_filter,
        )

    # ------------------------------------------------------------------
    # Parse and validate pagination parameters
    # ------------------------------------------------------------------
    page_str: Optional[str] = request.args.get("page", "1")
    try:
        page: int = max(1, int(page_str))  # type: ignore[arg-type]
    except (TypeError, ValueError):
        page = 1

    page_size: int = validate_page_size(request.args.get("page_size"))

    logger.info(
        "export_list_request",
        tenant_id=tenant_id,
        job_id=job_id,
        status=status_filter,
        format=format_filter,
        page=page,
        page_size=page_size,
    )

    # ------------------------------------------------------------------
    # Delegate to service layer
    # ------------------------------------------------------------------
    try:
        export_service: ExportService = _get_export_service()
        result: dict[str, Any] = export_service.list_exports(
            tenant_id=tenant_id,
            job_id=job_id,
            status=service_status,
            page=page,
            page_size=page_size,
        )
    except Exception as exc:
        logger.error(
            "export_list_failed",
            tenant_id=tenant_id,
            error=str(exc),
            error_type=type(exc).__name__,
        )
        return jsonify({
            "error": "Failed to list exports",
            "code": "internal_error",
        }), 500

    # ------------------------------------------------------------------
    # Transform items to API response format
    # ------------------------------------------------------------------
    items: list[dict[str, Any]] = [
        _build_export_response(item) for item in result.get("items", [])
    ]

    # Apply optional format filter client-side if the service layer does
    # not support it natively as a query parameter.
    if format_filter:
        items = [
            item for item in items
            if item.get("format") == format_filter
        ]

    response: dict[str, Any] = {
        "items": items,
        "total": result.get("total", 0),
        "page": result.get("page", page),
        "page_size": result.get("page_size", page_size),
        "has_next": result.get("has_next", False),
    }

    logger.debug(
        "export_list_response",
        tenant_id=tenant_id,
        total=response["total"],
        page=response["page"],
    )

    return jsonify(response), 200


@export_bp.route("/<string:export_id>", methods=["GET"])
@jwt_required()
@require_permissions("export:read")
def get_export(export_id: str):
    """Retrieve a single export record by ID.

    Returns the full export details including status, progress,
    file size, record count, and temporal metadata.  Enforces
    multi-tenant isolation per R-007.

    Args:
        export_id: Unique identifier of the export to retrieve.

    Returns:
        tuple: A JSON-serialised :class:`ExportResponse` with HTTP 200,
        404 if not found, or 500 for unexpected exceptions.
    """
    tenant_id: str = g.tenant_id

    logger.info(
        "export_get_request",
        tenant_id=tenant_id,
        export_id=export_id,
    )

    try:
        export_service: ExportService = _get_export_service()
        export_doc: Optional[dict[str, Any]] = export_service.get_export(
            export_id, tenant_id,
        )
    except Exception as exc:
        logger.error(
            "export_get_failed",
            tenant_id=tenant_id,
            export_id=export_id,
            error=str(exc),
            error_type=type(exc).__name__,
        )
        return jsonify({
            "error": "Failed to retrieve export",
            "code": "internal_error",
        }), 500

    if export_doc is None:
        logger.warning(
            "export_not_found",
            tenant_id=tenant_id,
            export_id=export_id,
        )
        return jsonify({
            "error": f"Export '{export_id}' not found",
            "code": "not_found",
        }), 404

    response_data: dict[str, Any] = _build_export_response(export_doc)
    return jsonify(response_data), 200


@export_bp.route("/<string:export_id>/download", methods=["GET"])
@jwt_required()
@require_permissions("export:read")
def get_download_url(export_id: str):
    """Get a time-limited signed download URL for a completed export.

    For ``local_file`` exports the URL includes a temporary access
    token.  For ``cloud_storage`` exports the URL is the cloud-native
    object URI.  Database exports do not produce a download URL.

    Args:
        export_id: Unique identifier of the export.

    Returns:
        tuple: A JSON object with ``export_id`` and ``download_url``
        and HTTP 200, 404 if the export is not found, 409 if the
        export is not yet completed or has no downloadable artefact,
        or 500 for unexpected exceptions.
    """
    tenant_id: str = g.tenant_id

    logger.info(
        "export_download_url_request",
        tenant_id=tenant_id,
        export_id=export_id,
    )

    try:
        export_service: ExportService = _get_export_service()

        # Retrieve the export record to differentiate 404 from 409.
        export_doc: Optional[dict[str, Any]] = export_service.get_export(
            export_id, tenant_id,
        )
        if export_doc is None:
            logger.warning(
                "export_download_not_found",
                tenant_id=tenant_id,
                export_id=export_id,
            )
            return jsonify({
                "error": f"Export '{export_id}' not found",
                "code": "not_found",
            }), 404

        # Verify that the export has reached a completed state.
        current_status: str = export_doc.get("status", "")
        if current_status != "completed":
            logger.warning(
                "export_download_not_completed",
                tenant_id=tenant_id,
                export_id=export_id,
                status=current_status,
            )
            api_status = _SERVICE_STATUS_TO_API.get(
                current_status, current_status,
            )
            return jsonify({
                "error": (
                    f"Export '{export_id}' is not yet completed "
                    f"(status: {api_status})"
                ),
                "code": "export_not_completed",
            }), 409

        # Verify the export type supports downloads.
        export_type: str = export_doc.get("export_type", "")
        if export_type == "database":
            logger.info(
                "export_download_not_applicable",
                tenant_id=tenant_id,
                export_id=export_id,
                export_type=export_type,
            )
            return jsonify({
                "error": "Database exports do not produce downloadable files",
                "code": "no_download_available",
            }), 409

        # Request the download URL from the service layer.
        download_url: Optional[str] = export_service.get_download_url(
            export_id, tenant_id,
        )
        if download_url is None:
            logger.warning(
                "export_download_url_unavailable",
                tenant_id=tenant_id,
                export_id=export_id,
            )
            return jsonify({
                "error": "Download URL is not available",
                "code": "download_unavailable",
            }), 409

        logger.info(
            "export_download_url_generated",
            tenant_id=tenant_id,
            export_id=export_id,
        )

        return jsonify({
            "export_id": export_id,
            "download_url": download_url,
        }), 200

    except Exception as exc:
        logger.error(
            "export_download_url_failed",
            tenant_id=tenant_id,
            export_id=export_id,
            error=str(exc),
            error_type=type(exc).__name__,
        )
        return jsonify({
            "error": "Failed to generate download URL",
            "code": "internal_error",
        }), 500


@export_bp.route("/<string:export_id>", methods=["DELETE"])
@jwt_required()
@require_permissions("export:create")
def cancel_export(export_id: str):
    """Cancel a pending or in-progress export.

    Only exports in ``pending`` or ``exporting`` (displayed as
    ``in_progress``) state can be cancelled.  The cancellation is
    forwarded to the Provisioning Service for exports that are
    already in progress.

    Args:
        export_id: Unique identifier of the export to cancel.

    Returns:
        tuple: A JSON object with the updated export status and
        HTTP 200, 404 if the export is not found, 409 if the export
        is in a non-cancellable state, or 500 for unexpected
        exceptions.
    """
    tenant_id: str = g.tenant_id
    user_id: str = g.user_id

    logger.info(
        "export_cancel_request",
        tenant_id=tenant_id,
        user_id=user_id,
        export_id=export_id,
    )

    try:
        export_service: ExportService = _get_export_service()

        # Pre-check existence so we can distinguish 404 from 409.
        export_doc: Optional[dict[str, Any]] = export_service.get_export(
            export_id, tenant_id,
        )
        if export_doc is None:
            logger.warning(
                "export_cancel_not_found",
                tenant_id=tenant_id,
                export_id=export_id,
            )
            return jsonify({
                "error": f"Export '{export_id}' not found",
                "code": "not_found",
            }), 404

        # The service recognises "pending" and "exporting" as cancellable.
        current_status: str = export_doc.get("status", "")
        cancellable_states = {"pending", "exporting"}
        if current_status not in cancellable_states:
            api_status = _SERVICE_STATUS_TO_API.get(
                current_status, current_status,
            )
            logger.warning(
                "export_cancel_not_cancellable",
                tenant_id=tenant_id,
                export_id=export_id,
                status=api_status,
            )
            return jsonify({
                "error": (
                    f"Export '{export_id}' cannot be cancelled "
                    f"(status: {api_status})"
                ),
                "code": "not_cancellable",
            }), 409

        # Perform the cancellation.
        updated_doc: Optional[dict[str, Any]] = export_service.cancel_export(
            export_id, tenant_id,
        )
        if updated_doc is None:
            # Defensive: should not happen after pre-checks, but handle
            # race conditions gracefully.
            logger.warning(
                "export_cancel_race_condition",
                tenant_id=tenant_id,
                export_id=export_id,
            )
            return jsonify({
                "error": f"Export '{export_id}' could not be cancelled",
                "code": "cancel_failed",
            }), 409

        logger.info(
            "export_cancelled",
            tenant_id=tenant_id,
            user_id=user_id,
            export_id=export_id,
        )

        response_data: dict[str, Any] = _build_export_response(updated_doc)
        return jsonify(response_data), 200

    except Exception as exc:
        logger.error(
            "export_cancel_failed",
            tenant_id=tenant_id,
            export_id=export_id,
            error=str(exc),
            error_type=type(exc).__name__,
        )
        return jsonify({
            "error": "Failed to cancel export",
            "code": "internal_error",
        }), 500
