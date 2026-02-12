"""Business logic service for export orchestration in the API Gateway.

This module provides the :class:`ExportService` class that acts as the
intermediary between the API Gateway route handlers and the downstream
Provisioning Service.  It encapsulates all business logic for:

- **Database provisioning** — Dispatching export requests to the
  Provisioning Service for direct database insertion via JDBC into
  PostgreSQL, Oracle, SQL Server, and SAP HANA targets.
- **Cloud storage uploads** — Dispatching export requests for object
  storage upload to AWS S3, Azure Blob Storage, and GCP Cloud Storage.
- **File downloads** — Generating downloadable export files in SQL, CSV,
  JSON, and Parquet formats.
- **Export lifecycle management** — Creating, tracking, cancelling, and
  querying export jobs with full tenant isolation (R-007).
- **Progress tracking** — Real-time progress reporting backed by Redis
  cache with 3600 s TTL.
- **Resilience** — Circuit breaker protection on all inter-service HTTP
  calls to the Provisioning Service (5 failure threshold, 30 s recovery).

The module also exposes several constants and an enum that define the
valid export types, database targets, cloud targets, output formats, and
export lifecycle states.

Security Notes:
    - Connection strings and cloud credentials are **never** logged
      (R-005 / C-001).
    - AES-256 encryption at rest is enforced by setting the
      ``encrypt_at_rest`` flag in every dispatch payload; the actual
      encryption is handled by the Provisioning Service (R-006).

Usage::

    from api_gateway.services.export_service import ExportService

    svc = ExportService()
    export = svc.create_export(
        tenant_id="tenant-42",
        user_id="user-7",
        job_id="job-abc-123",
        export_type="cloud_storage",
        target_config={
            "cloud_provider": "aws_s3",
            "bucket": "my-bucket",
            "path_prefix": "exports/2024/",
            "credentials_ref": "vault:aws/s3-creds",
        },
        output_format="parquet",
    )
"""

from __future__ import annotations

import json
import re
import uuid
from datetime import UTC, datetime
from enum import Enum, StrEnum
from typing import Any

import httpx
from circuitbreaker import circuit
from flask import current_app

from api_gateway.extensions import get_db, get_redis
from shared.logging.structured_logger import get_logger


# ---------------------------------------------------------------------------
# Module-level constants
# ---------------------------------------------------------------------------

EXPORT_TYPES: list[str] = ["database", "cloud_storage", "file_download"]
"""Valid export type identifiers accepted by :meth:`ExportService.create_export`.

- ``"database"``      — Direct JDBC provisioning to a target database.
- ``"cloud_storage"`` — Upload to a cloud object-storage service.
- ``"file_download"`` — Generate a downloadable file on the platform.
"""

DATABASE_TARGETS: list[str] = ["postgresql", "oracle", "sqlserver", "hana"]
"""Supported database provisioning targets, corresponding to the JDBC
connectors implemented by the Provisioning Service."""

CLOUD_TARGETS: list[str] = ["aws_s3", "azure_blob", "gcp_storage"]
"""Supported cloud storage providers for export uploads."""

OUTPUT_FORMATS: list[str] = ["sql", "csv", "json", "parquet"]
"""Supported output file formats for generated data exports."""

# MongoDB collection name for export records.
_EXPORTS_COLLECTION: str = "exports"

# Redis key prefix for export status caching.
_REDIS_KEY_PREFIX: str = "export"

# Redis cache TTL for export status data (seconds).
_CACHE_TTL_SECONDS: int = 3600

# Redis key prefix for real-time progress tracking.
_PROGRESS_KEY_PREFIX: str = "export:progress"

# Default HTTP timeout for Provisioning Service calls (seconds).
_DISPATCH_TIMEOUT_SECONDS: int = 60

# Provisioning Service endpoint paths keyed by export_type.
_ENDPOINT_MAP: dict[str, str] = {
    "database": "/api/v1/provision/database",
    "cloud_storage": "/api/v1/provision/cloud",
    "file_download": "/api/v1/export/file",
}

# Regex for basic connection-string format validation — must contain a
# scheme separator (``://``) and at least one host character.
_CONNECTION_STRING_PATTERN: re.Pattern[str] = re.compile(
    r"^[a-zA-Z][a-zA-Z0-9+\-.]*://\S+",
)

# Regex for valid bucket / container names (3-63 characters, lowercase
# alphanumeric and hyphens, no leading/trailing hyphen).
_BUCKET_NAME_PATTERN: re.Pattern[str] = re.compile(
    r"^[a-z0-9][a-z0-9\-]{1,61}[a-z0-9]$",
)


# ---------------------------------------------------------------------------
# ExportStatus Enum
# ---------------------------------------------------------------------------


class ExportStatus(StrEnum):
    """Lifecycle states for an export job.

    The state machine follows a linear progression::

        PENDING ──► EXPORTING ──► COMPLETED
                        │
                        └──► FAILED

    ``PENDING`` is the initial state when the export record is created
    in MongoDB.  The Provisioning Service transitions the export to
    ``EXPORTING`` when processing begins.  Upon successful completion the
    state moves to ``COMPLETED``; any error causes a transition to
    ``FAILED``.  Manual cancellation also results in ``FAILED`` with an
    ``error_message`` of ``'Cancelled by user'``.
    """

    PENDING = "pending"
    EXPORTING = "exporting"
    COMPLETED = "completed"
    FAILED = "failed"


# ---------------------------------------------------------------------------
# ExportService Class
# ---------------------------------------------------------------------------


class ExportService:
    """Orchestrates export operations between the API Gateway and the
    Provisioning Service.

    Responsibilities:

    * Validate incoming export requests (type, target configuration,
      output format).
    * Persist export records in the ``exports`` MongoDB collection.
    * Dispatch export work to the Provisioning Service via HTTP REST
      calls protected by a circuit breaker.
    * Cache and retrieve export status / progress in Redis for low-latency
      reads.
    * Enforce multi-tenant isolation on every data-access operation
      (R-007).

    Attributes:
        logger: Pre-configured structlog ``BoundLogger`` instance for
            structured JSON logging with correlation-ID propagation.
        provisioning_service_url: Base URL of the Provisioning Service
            read from ``current_app.config['PROVISIONING_SERVICE_URL']``.
    """

    # ------------------------------------------------------------------ #
    # Construction
    # ------------------------------------------------------------------ #

    def __init__(self) -> None:
        """Initialise the ExportService.

        Reads the ``PROVISIONING_SERVICE_URL`` from the active Flask
        application configuration.  Must be called within an active Flask
        application context (i.e. during a request or inside
        ``app.app_context()``).

        Raises:
            RuntimeError: If called outside a Flask application context.
        """
        self.logger = get_logger(__name__)
        self.provisioning_service_url: str = current_app.config.get(
            "PROVISIONING_SERVICE_URL",
            "http://provisioning-service:8005",
        )

    # ================================================================== #
    # Public API
    # ================================================================== #

    def create_export(
        self,
        tenant_id: str,
        user_id: str,
        job_id: str,
        export_type: str,
        target_config: dict[str, Any],
        output_format: str,
    ) -> dict[str, Any]:
        """Create a new export job and dispatch it to the Provisioning Service.

        Validates all inputs, persists an export record with status
        ``PENDING`` in MongoDB, dispatches the work to the Provisioning
        Service, and caches the initial status in Redis.

        Args:
            tenant_id: Tenant identifier for multi-tenant isolation (R-007).
            user_id: Identifier of the user initiating the export.
            job_id: Identifier of the parent generation job whose output
                is being exported.
            export_type: One of ``EXPORT_TYPES`` (``'database'``,
                ``'cloud_storage'``, or ``'file_download'``).
            target_config: Export-type-specific configuration dictionary:

                - ``database``:  ``db_type``, ``connection_string``,
                  ``schema``, ``table_prefix``.
                - ``cloud_storage``:  ``cloud_provider``, ``bucket`` (or
                  ``container``), ``path_prefix``, ``credentials_ref``.
                - ``file_download``:  No additional fields required beyond
                  ``output_format``.
            output_format: One of ``OUTPUT_FORMATS`` (``'sql'``, ``'csv'``,
                ``'json'``, or ``'parquet'``).

        Returns:
            The created export document as a dictionary containing at least
            ``export_id``, ``tenant_id``, ``job_id``, ``status``,
            ``export_type``, ``output_format``, and ``created_at``.

        Raises:
            ValueError: If any input parameter fails validation.
            httpx.HTTPStatusError: If the Provisioning Service returns
                a non-2xx response.
            Exception: For MongoDB or Redis connectivity issues (logged
                and re-raised).
        """
        # -- Input validation -----------------------------------------------
        if export_type not in EXPORT_TYPES:
            raise ValueError(
                f"Invalid export_type '{export_type}'. "
                f"Must be one of {EXPORT_TYPES}.",
            )

        if output_format not in OUTPUT_FORMATS:
            raise ValueError(
                f"Invalid output_format '{output_format}'. "
                f"Must be one of {OUTPUT_FORMATS}.",
            )

        # Type-specific configuration validation.
        if export_type == "database":
            if not self._validate_database_config(target_config):
                raise ValueError(
                    "Invalid database target_config. Required fields: "
                    "db_type (one of postgresql/oracle/sqlserver/hana), "
                    "connection_string, schema, table_prefix.",
                )
        elif export_type == "cloud_storage" and not self._validate_cloud_config(target_config):
            raise ValueError(
                "Invalid cloud_storage target_config. Required fields: "
                "cloud_provider (one of aws_s3/azure_blob/gcp_storage), "
                "bucket or container, path_prefix, credentials_ref.",
            )
        # 'file_download' requires no extra target_config fields.

        # -- Build export document ------------------------------------------
        export_id: str = str(uuid.uuid4())
        now: datetime = datetime.now(UTC)

        export_doc: dict[str, Any] = {
            "export_id": export_id,
            "tenant_id": tenant_id,
            "user_id": user_id,
            "job_id": job_id,
            "export_type": export_type,
            "target_config": self._sanitize_config_for_storage(target_config),
            "output_format": output_format,
            "status": ExportStatus.PENDING.value,
            "encrypt_at_rest": True,
            "progress_percentage": 0,
            "records_exported": 0,
            "error_message": None,
            "download_url": None,
            "created_at": now,
            "updated_at": now,
            "completed_at": None,
        }

        # -- Persist to MongoDB ---------------------------------------------
        try:
            db = get_db()
            db[_EXPORTS_COLLECTION].insert_one(export_doc)
            self.logger.info(
                "export_created",
                export_id=export_id,
                tenant_id=tenant_id,
                job_id=job_id,
                export_type=export_type,
                output_format=output_format,
            )
        except Exception as exc:
            self.logger.error(
                "export_creation_failed",
                export_id=export_id,
                tenant_id=tenant_id,
                error=str(exc),
                error_type=type(exc).__name__,
            )
            raise

        # -- Dispatch to Provisioning Service --------------------------------
        try:
            dispatch_payload: dict[str, Any] = {
                "export_id": export_id,
                "tenant_id": tenant_id,
                "job_id": job_id,
                "export_type": export_type,
                "target_config": target_config,
                "output_format": output_format,
                "encrypt_at_rest": True,
            }
            self._dispatch_export(dispatch_payload)
        except Exception as exc:
            # Update MongoDB status to FAILED if dispatch fails.
            self.logger.error(
                "export_dispatch_failed",
                export_id=export_id,
                tenant_id=tenant_id,
                error=str(exc),
                error_type=type(exc).__name__,
            )
            self._update_export_status_in_db(
                export_id=export_id,
                tenant_id=tenant_id,
                new_status=ExportStatus.FAILED.value,
                error_message=f"Dispatch failed: {type(exc).__name__}",
            )
            export_doc["status"] = ExportStatus.FAILED.value
            export_doc["error_message"] = f"Dispatch failed: {type(exc).__name__}"

        # -- Cache initial status in Redis -----------------------------------
        self._cache_export_status(export_id, {
            "export_id": export_id,
            "status": export_doc["status"],
            "progress_percentage": 0,
            "records_exported": 0,
            "export_type": export_type,
            "output_format": output_format,
        })

        # Remove MongoDB's internal _id before returning.
        export_doc.pop("_id", None)
        return export_doc

    def get_export(
        self,
        export_id: str,
        tenant_id: str,
    ) -> dict[str, Any] | None:
        """Retrieve a single export record by ID with tenant isolation.

        Uses a cache-first strategy: checks Redis before falling back to
        MongoDB.  The MongoDB query always includes the ``tenant_id``
        filter to enforce multi-tenant isolation per R-007.

        Args:
            export_id: Unique identifier of the export.
            tenant_id: Tenant identifier for isolation (R-007).

        Returns:
            The export document as a dictionary, or ``None`` if not found
            or the export belongs to a different tenant.
        """
        # Fast path: Redis cache lookup.
        cached: dict[str, Any] | None = self._get_cached_status(export_id)
        if cached is not None:
            self.logger.debug(
                "export_cache_hit",
                export_id=export_id,
                tenant_id=tenant_id,
            )
            return cached

        # Slow path: MongoDB query with tenant filter.
        try:
            db = get_db()
            doc: dict[str, Any] | None = db[_EXPORTS_COLLECTION].find_one(
                {"export_id": export_id, "tenant_id": tenant_id},
            )
            if doc is not None:
                doc.pop("_id", None)
                # Warm the cache for subsequent reads.
                self._cache_export_status(export_id, doc)
                self.logger.debug(
                    "export_cache_miss_db_hit",
                    export_id=export_id,
                    tenant_id=tenant_id,
                )
            return doc
        except Exception as exc:
            self.logger.error(
                "export_get_failed",
                export_id=export_id,
                tenant_id=tenant_id,
                error=str(exc),
                error_type=type(exc).__name__,
            )
            raise

    def list_exports(
        self,
        tenant_id: str,
        job_id: str | None = None,
        status: str | None = None,
        page: int = 1,
        page_size: int = 20,
    ) -> dict[str, Any]:
        """List export records with tenant-scoped filtering and pagination.

        Args:
            tenant_id: Tenant identifier — always applied as a filter
                (R-007).
            job_id: Optional generation job ID to filter by.
            status: Optional export status string to filter by.
            page: 1-based page number (defaults to 1).
            page_size: Number of records per page (defaults to 20,
                clamped to 1-100).

        Returns:
            A dictionary with pagination metadata::

                {
                    "items": [...],
                    "total": <int>,
                    "page": <int>,
                    "page_size": <int>,
                    "has_next": <bool>,
                }
        """
        # Clamp page_size to a safe range.
        page_size = max(1, min(page_size, 100))
        page = max(1, page)
        skip: int = (page - 1) * page_size

        query: dict[str, Any] = {"tenant_id": tenant_id}
        if job_id is not None:
            query["job_id"] = job_id
        if status is not None:
            query["status"] = status

        try:
            db = get_db()
            collection = db[_EXPORTS_COLLECTION]

            total: int = collection.count_documents(query)
            cursor = (
                collection.find(query)
                .sort("created_at", -1)
                .skip(skip)
                .limit(page_size)
            )
            items: list[dict[str, Any]] = []
            for doc in cursor:
                doc.pop("_id", None)
                items.append(doc)

            has_next: bool = (skip + page_size) < total

            self.logger.debug(
                "exports_listed",
                tenant_id=tenant_id,
                total=total,
                page=page,
                page_size=page_size,
            )
            return {
                "items": items,
                "total": total,
                "page": page,
                "page_size": page_size,
                "has_next": has_next,
            }
        except Exception as exc:
            self.logger.error(
                "export_list_failed",
                tenant_id=tenant_id,
                error=str(exc),
                error_type=type(exc).__name__,
            )
            raise

    def get_export_progress(
        self,
        export_id: str,
        tenant_id: str,
    ) -> dict[str, Any] | None:
        """Retrieve real-time export progress from Redis.

        Progress data is written by the Provisioning Service and cached
        in Redis under a dedicated progress key.

        Args:
            export_id: Unique identifier of the export.
            tenant_id: Tenant identifier for isolation (R-007).

        Returns:
            A dictionary with progress details::

                {
                    "export_id": "...",
                    "status": "exporting",
                    "progress_percentage": 45,
                    "records_exported": 45000,
                    "estimated_remaining_seconds": 120,
                }

            Returns ``None`` if no progress data is available.
        """
        try:
            redis_client = get_redis()
            key: str = f"{_PROGRESS_KEY_PREFIX}:{export_id}"
            raw: Any = redis_client.get(key)

            if raw is not None:
                progress_data: dict[str, Any] = json.loads(raw)
                self.logger.debug(
                    "export_progress_retrieved",
                    export_id=export_id,
                    tenant_id=tenant_id,
                    progress=progress_data.get("progress_percentage"),
                )
                return progress_data

            # Fallback: return basic status from the export record.
            export_doc = self.get_export(export_id, tenant_id)
            if export_doc is not None:
                return {
                    "export_id": export_id,
                    "status": export_doc.get("status", ExportStatus.PENDING.value),
                    "progress_percentage": export_doc.get("progress_percentage", 0),
                    "records_exported": export_doc.get("records_exported", 0),
                    "estimated_remaining_seconds": None,
                }
            return None

        except Exception as exc:
            self.logger.error(
                "export_progress_retrieval_failed",
                export_id=export_id,
                tenant_id=tenant_id,
                error=str(exc),
                error_type=type(exc).__name__,
            )
            return None

    def cancel_export(
        self,
        export_id: str,
        tenant_id: str,
    ) -> dict[str, Any] | None:
        """Cancel an in-progress or pending export.

        Only exports in ``PENDING`` or ``EXPORTING`` state may be
        cancelled.  A cancellation request is forwarded to the
        Provisioning Service, and the export record is marked as
        ``FAILED`` with ``error_message='Cancelled by user'``.

        Args:
            export_id: Unique identifier of the export to cancel.
            tenant_id: Tenant identifier for isolation (R-007).

        Returns:
            The updated export document, or ``None`` if the export was
            not found or is not in a cancellable state.
        """
        try:
            db = get_db()
            doc: dict[str, Any] | None = db[_EXPORTS_COLLECTION].find_one(
                {"export_id": export_id, "tenant_id": tenant_id},
            )

            if doc is None:
                self.logger.warning(
                    "export_cancel_not_found",
                    export_id=export_id,
                    tenant_id=tenant_id,
                )
                return None

            current_status: str = doc.get("status", "")
            cancellable_states = {ExportStatus.PENDING.value, ExportStatus.EXPORTING.value}
            if current_status not in cancellable_states:
                self.logger.warning(
                    "export_cancel_not_cancellable",
                    export_id=export_id,
                    tenant_id=tenant_id,
                    current_status=current_status,
                )
                return None

            # Notify the Provisioning Service about the cancellation.
            if current_status == ExportStatus.EXPORTING.value:
                try:
                    cancel_url = (
                        f"{self.provisioning_service_url}"
                        f"/api/v1/export/{export_id}/cancel"
                    )
                    httpx.post(
                        cancel_url,
                        json={"export_id": export_id, "tenant_id": tenant_id},
                        headers={"X-Tenant-ID": tenant_id},
                        timeout=_DISPATCH_TIMEOUT_SECONDS,
                    )
                except Exception as cancel_exc:
                    # Log but proceed with local status update regardless.
                    self.logger.warning(
                        "export_cancel_provisioning_notify_failed",
                        export_id=export_id,
                        tenant_id=tenant_id,
                        error=str(cancel_exc),
                        error_type=type(cancel_exc).__name__,
                    )

            # Update MongoDB status to FAILED.
            updated_doc = self._update_export_status_in_db(
                export_id=export_id,
                tenant_id=tenant_id,
                new_status=ExportStatus.FAILED.value,
                error_message="Cancelled by user",
            )

            # Update Redis cache.
            if updated_doc:
                self._cache_export_status(export_id, {
                    "export_id": export_id,
                    "status": ExportStatus.FAILED.value,
                    "progress_percentage": doc.get("progress_percentage", 0),
                    "records_exported": doc.get("records_exported", 0),
                    "error_message": "Cancelled by user",
                })

            self.logger.info(
                "export_cancelled",
                export_id=export_id,
                tenant_id=tenant_id,
                previous_status=current_status,
            )
            return updated_doc

        except Exception as exc:
            self.logger.error(
                "export_cancel_failed",
                export_id=export_id,
                tenant_id=tenant_id,
                error=str(exc),
                error_type=type(exc).__name__,
            )
            raise

    def get_download_url(
        self,
        export_id: str,
        tenant_id: str,
    ) -> str | None:
        """Retrieve the download URL for a completed export.

        For ``file_download`` exports the URL points to a platform-hosted
        file with a time-limited access token.  For ``cloud_storage``
        exports the URL is the object's cloud-native URI.  Database
        exports do not produce a download URL and will return ``None``.

        Args:
            export_id: Unique identifier of the export.
            tenant_id: Tenant identifier for isolation (R-007).

        Returns:
            A download URL string, or ``None`` if the export is not found,
            belongs to a different tenant, is not yet completed, or does
            not produce a downloadable artefact.
        """
        try:
            db = get_db()
            doc: dict[str, Any] | None = db[_EXPORTS_COLLECTION].find_one(
                {"export_id": export_id, "tenant_id": tenant_id},
            )

            if doc is None:
                self.logger.debug(
                    "export_download_url_not_found",
                    export_id=export_id,
                    tenant_id=tenant_id,
                )
                return None

            if doc.get("status") != ExportStatus.COMPLETED.value:
                self.logger.debug(
                    "export_download_url_not_ready",
                    export_id=export_id,
                    tenant_id=tenant_id,
                    status=doc.get("status"),
                )
                return None

            export_type: str = doc.get("export_type", "")

            if export_type == "file_download":
                # Generate a time-limited signed download URL.  The
                # download token is a UUID that the file-serving endpoint
                # validates against Redis.
                download_token: str = str(uuid.uuid4())
                token_key: str = f"export:download_token:{download_token}"
                try:
                    redis_client = get_redis()
                    token_data: dict[str, str] = {
                        "export_id": export_id,
                        "tenant_id": tenant_id,
                    }
                    redis_client.setex(
                        token_key,
                        _CACHE_TTL_SECONDS,
                        json.dumps(token_data),
                    )
                except Exception as redis_exc:
                    self.logger.error(
                        "export_download_token_cache_failed",
                        export_id=export_id,
                        tenant_id=tenant_id,
                        error=str(redis_exc),
                        error_type=type(redis_exc).__name__,
                    )
                    return None

                base_url: str = current_app.config.get(
                    "EXPORT_DOWNLOAD_BASE_URL",
                    "/api/v1/exports/download",
                )
                download_url: str = (
                    f"{base_url}?token={download_token}"
                    f"&export_id={export_id}"
                )
                self.logger.info(
                    "export_download_url_generated",
                    export_id=export_id,
                    tenant_id=tenant_id,
                )
                return download_url

            if export_type == "cloud_storage":
                # Return the cloud storage URL stored by the Provisioning
                # Service upon export completion.
                cloud_url: str | None = doc.get("download_url")
                if cloud_url:
                    self.logger.info(
                        "export_cloud_url_retrieved",
                        export_id=export_id,
                        tenant_id=tenant_id,
                    )
                return cloud_url

            # Database exports do not produce a download URL.
            return None

        except Exception as exc:
            self.logger.error(
                "export_download_url_failed",
                export_id=export_id,
                tenant_id=tenant_id,
                error=str(exc),
                error_type=type(exc).__name__,
            )
            raise

    # ================================================================== #
    # Private Helpers — Inter-Service Dispatch
    # ================================================================== #

    @circuit(failure_threshold=5, recovery_timeout=30)
    def _dispatch_export(self, export_request: dict[str, Any]) -> dict[str, Any]:
        """Dispatch an export request to the Provisioning Service.

        The target endpoint is determined by the ``export_type`` field in
        the request payload:

        - ``'database'``      → ``POST /api/v1/provision/database``
        - ``'cloud_storage'`` → ``POST /api/v1/provision/cloud``
        - ``'file_download'`` → ``POST /api/v1/export/file``

        The call is protected by a circuit breaker that opens after **5**
        consecutive failures and recovers after **30 seconds**.

        Args:
            export_request: Complete export configuration dictionary
                including ``export_id``, ``tenant_id``, ``job_id``,
                ``export_type``, ``target_config``, ``output_format``,
                and ``encrypt_at_rest``.

        Returns:
            The JSON response body from the Provisioning Service as a
            dictionary.

        Raises:
            httpx.HTTPStatusError: If the Provisioning Service returns a
                non-2xx status code.
            httpx.ConnectError: If the Provisioning Service is
                unreachable.
            circuitbreaker.CircuitBreakerError: If the circuit breaker is
                currently open.
        """
        export_type: str = export_request.get("export_type", "")
        endpoint_path: str = _ENDPOINT_MAP.get(export_type, "/api/v1/export/file")
        url: str = f"{self.provisioning_service_url}{endpoint_path}"
        tenant_id: str = export_request.get("tenant_id", "")

        self.logger.info(
            "export_dispatch_started",
            export_id=export_request.get("export_id"),
            tenant_id=tenant_id,
            export_type=export_type,
            endpoint=endpoint_path,
        )

        response = httpx.post(
            url,
            json=export_request,
            headers={
                "X-Tenant-ID": tenant_id,
                "Content-Type": "application/json",
            },
            timeout=_DISPATCH_TIMEOUT_SECONDS,
        )
        response.raise_for_status()

        response_data: dict[str, Any] = response.json()
        self.logger.info(
            "export_dispatch_completed",
            export_id=export_request.get("export_id"),
            tenant_id=tenant_id,
            provisioning_status=response.status_code,
        )
        return response_data

    # ================================================================== #
    # Private Helpers — Validation
    # ================================================================== #

    def _validate_database_config(self, config: dict[str, Any]) -> bool:
        """Validate a database export target configuration.

        Required fields:

        - ``db_type``           — Must be one of :data:`DATABASE_TARGETS`.
        - ``connection_string`` — Must contain a scheme (``://``) prefix.
        - ``schema``            — Non-empty string.
        - ``table_prefix``      — Non-empty string.

        Args:
            config: The ``target_config`` dictionary for a ``database``
                export.

        Returns:
            ``True`` if the configuration is valid, ``False`` otherwise.
        """
        if not isinstance(config, dict):
            return False

        db_type: Any = config.get("db_type")
        if db_type not in DATABASE_TARGETS:
            self.logger.warning(
                "export_validation_invalid_db_type",
                db_type=str(db_type),
            )
            return False

        connection_string: Any = config.get("connection_string")
        if not isinstance(connection_string, str) or not _CONNECTION_STRING_PATTERN.match(
            connection_string,
        ):
            self.logger.warning(
                "export_validation_invalid_connection_string",
            )
            return False

        schema: Any = config.get("schema")
        if not isinstance(schema, str) or not schema.strip():
            self.logger.warning("export_validation_missing_schema")
            return False

        table_prefix: Any = config.get("table_prefix")
        if not isinstance(table_prefix, str) or not table_prefix.strip():
            self.logger.warning("export_validation_missing_table_prefix")
            return False

        return True

    def _validate_cloud_config(self, config: dict[str, Any]) -> bool:
        """Validate a cloud storage export target configuration.

        Required fields:

        - ``cloud_provider``  — Must be one of :data:`CLOUD_TARGETS`.
        - ``bucket`` (or ``container``) — Valid bucket/container name.
        - ``path_prefix``     — Non-empty string.
        - ``credentials_ref`` — Non-empty string pointing to a vault
          or credentials store reference.

        Args:
            config: The ``target_config`` dictionary for a
                ``cloud_storage`` export.

        Returns:
            ``True`` if the configuration is valid, ``False`` otherwise.
        """
        if not isinstance(config, dict):
            return False

        cloud_provider: Any = config.get("cloud_provider")
        if cloud_provider not in CLOUD_TARGETS:
            self.logger.warning(
                "export_validation_invalid_cloud_provider",
                cloud_provider=str(cloud_provider),
            )
            return False

        # Accept either 'bucket' or 'container' key.
        bucket_name: Any = config.get("bucket") or config.get("container")
        if not isinstance(bucket_name, str) or not bucket_name.strip():
            self.logger.warning("export_validation_missing_bucket")
            return False

        # Validate bucket name format (relaxed — allow uppercase for Azure).
        bucket_lower: str = bucket_name.lower()
        if not _BUCKET_NAME_PATTERN.match(bucket_lower) and len(bucket_name) > 63:
            self.logger.warning(
                "export_validation_invalid_bucket_name",
            )
            return False

        path_prefix: Any = config.get("path_prefix")
        if not isinstance(path_prefix, str) or not path_prefix.strip():
            self.logger.warning("export_validation_missing_path_prefix")
            return False

        credentials_ref: Any = config.get("credentials_ref")
        if not isinstance(credentials_ref, str) or not credentials_ref.strip():
            self.logger.warning("export_validation_missing_credentials_ref")
            return False

        return True

    # ================================================================== #
    # Private Helpers — Redis Caching
    # ================================================================== #

    def _cache_export_status(
        self,
        export_id: str,
        status_data: dict[str, Any],
    ) -> None:
        """Cache export status data in Redis.

        Serialises *status_data* to JSON and stores it under the key
        ``export:<export_id>`` with a TTL of :data:`_CACHE_TTL_SECONDS`
        (3600 s).

        Args:
            export_id: Unique identifier of the export.
            status_data: Dictionary of status fields to cache.
        """
        try:
            redis_client = get_redis()
            key: str = f"{_REDIS_KEY_PREFIX}:{export_id}"
            # Ensure datetime objects are serialised as ISO strings.
            serialisable: dict[str, Any] = self._make_json_serialisable(status_data)
            redis_client.setex(key, _CACHE_TTL_SECONDS, json.dumps(serialisable))
        except Exception as exc:
            # Cache failures are non-fatal — log and continue.
            self.logger.warning(
                "export_cache_write_failed",
                export_id=export_id,
                error=str(exc),
                error_type=type(exc).__name__,
            )

    def _get_cached_status(
        self,
        export_id: str,
    ) -> dict[str, Any] | None:
        """Retrieve cached export status from Redis.

        Args:
            export_id: Unique identifier of the export.

        Returns:
            The cached status dictionary, or ``None`` if the key does not
            exist or has expired.
        """
        try:
            redis_client = get_redis()
            key: str = f"{_REDIS_KEY_PREFIX}:{export_id}"
            raw: Any = redis_client.get(key)
            if raw is not None:
                result: dict[str, Any] = json.loads(raw)
                return result
            return None
        except Exception as exc:
            # Cache read failures are non-fatal — fall back to MongoDB.
            self.logger.warning(
                "export_cache_read_failed",
                export_id=export_id,
                error=str(exc),
                error_type=type(exc).__name__,
            )
            return None

    # ================================================================== #
    # Private Helpers — MongoDB Updates
    # ================================================================== #

    def _update_export_status_in_db(
        self,
        export_id: str,
        tenant_id: str,
        new_status: str,
        error_message: str | None = None,
    ) -> dict[str, Any] | None:
        """Update the status of an export record in MongoDB.

        Args:
            export_id: Unique identifier of the export.
            tenant_id: Tenant identifier for isolation (R-007).
            new_status: The new status value.
            error_message: Optional error description (set on failures).

        Returns:
            The updated export document, or ``None`` if no matching
            document was found.
        """
        now: datetime = datetime.now(UTC)
        update_fields: dict[str, Any] = {
            "status": new_status,
            "updated_at": now,
        }
        if error_message is not None:
            update_fields["error_message"] = error_message

        if new_status in (ExportStatus.COMPLETED.value, ExportStatus.FAILED.value):
            update_fields["completed_at"] = now

        try:
            db = get_db()
            result = db[_EXPORTS_COLLECTION].find_one_and_update(
                {"export_id": export_id, "tenant_id": tenant_id},
                {"$set": update_fields},
                return_document=True,
            )
            if result is not None:
                result.pop("_id", None)
                self.logger.info(
                    "export_status_updated",
                    export_id=export_id,
                    tenant_id=tenant_id,
                    new_status=new_status,
                )
            return result
        except Exception as exc:
            self.logger.error(
                "export_status_update_failed",
                export_id=export_id,
                tenant_id=tenant_id,
                error=str(exc),
                error_type=type(exc).__name__,
            )
            raise

    # ================================================================== #
    # Private Helpers — Utilities
    # ================================================================== #

    @staticmethod
    def _sanitize_config_for_storage(config: dict[str, Any]) -> dict[str, Any]:
        """Remove sensitive fields from target config before MongoDB storage.

        Connection strings and credential references are replaced with
        a redacted placeholder to ensure that secrets are never
        persisted in the metadata repository (R-005).

        Args:
            config: The original target configuration dictionary.

        Returns:
            A copy of *config* with sensitive values replaced by
            ``'***REDACTED***'``.
        """
        sensitive_keys = {"connection_string", "credentials_ref", "password", "secret"}
        sanitized: dict[str, Any] = {}
        for key, value in config.items():
            if key.lower() in sensitive_keys:
                sanitized[key] = "***REDACTED***"
            else:
                sanitized[key] = value
        return sanitized

    @staticmethod
    def _make_json_serialisable(data: dict[str, Any]) -> dict[str, Any]:
        """Convert non-JSON-serialisable values to strings.

        Handles ``datetime`` instances by converting to ISO 8601 format.

        Args:
            data: Dictionary potentially containing ``datetime`` values.

        Returns:
            A copy of *data* with all values JSON-serialisable.
        """
        result: dict[str, Any] = {}
        for key, value in data.items():
            if isinstance(value, datetime):
                result[key] = value.isoformat()
            elif isinstance(value, Enum):
                result[key] = value.value
            elif isinstance(value, dict):
                result[key] = ExportService._make_json_serialisable(value)
            else:
                result[key] = value
        return result
