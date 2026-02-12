"""Provisioning Service Flask Application Factory.

This module implements the Flask Application Factory pattern for the
Provisioning Service — the final delivery stage of the Synthetic ERP
Data Generation Platform pipeline.

The Provisioning Service handles:

* **Database provisioning** — Batch INSERT operations to PostgreSQL,
  Oracle, SQL Server, and SAP HANA target databases via JDBC connectors.
* **Cloud storage export** — Multi-part upload with AES-256 encryption
  to AWS S3, Azure Blob Storage, and GCP Cloud Storage.
* **Multi-format file export** — SQL INSERT statements, CSV, JSON/JSONL,
  and Apache Parquet columnar output generation.

The :func:`create_app` factory function configures and returns a fully
initialised Flask application with:

- Cross-Origin Resource Sharing (CORS) support for the React Web Console
- MongoDB connection for metadata persistence and job tracking
- Redis connection for caching, session handling, and progress updates
- Structured JSON logging with correlation ID propagation
- OpenTelemetry distributed tracing for cross-service debugging
- Prometheus metrics collection and ``/metrics`` endpoint
- Health (``/health``) and readiness (``/ready``) endpoints for
  Kubernetes probes
- Provisioning route blueprints for file export, database provisioning,
  and cloud storage upload operations
- Comprehensive error handlers returning structured JSON responses
- ``before_request`` hook for multi-tenant context extraction from
  JWT claims and HTTP headers
- ``after_request`` hook for response logging with correlation IDs

Example::

    from provisioning_service.app import create_app

    app = create_app()
    app.run(host="0.0.0.0", port=5005)
"""

from __future__ import annotations

import base64
import json
import uuid

from flask import Blueprint, Flask, current_app, g, jsonify, request
from flask_cors import CORS

from provisioning_service.config import ProvisioningServiceConfig
from shared.config.base import get_config
from shared.database.mongodb import init_mongodb
from shared.database.redis_client import init_redis
from shared.logging.structured_logger import (
    bind_context,
    clear_context,
    configure_logging,
    get_logger,
)
from shared.middleware.health_check import init_health_checks
from shared.observability.metrics import setup_metrics
from shared.observability.tracing import init_tracing


# ---------------------------------------------------------------------------
# Optional exporter imports — resolved eagerly at module load so that
# ``PLC0415`` (import not at top level) is not triggered.  A ``None``
# sentinel signals that the dependency is not installed or not yet
# available.
# ---------------------------------------------------------------------------

try:
    from provisioning_service.exporters.file_exporter import FileExporter
except ImportError:  # pragma: no cover
    FileExporter = None

try:
    from provisioning_service.exporters.database_exporter import (
        DatabaseExporter,
    )
except ImportError:  # pragma: no cover
    DatabaseExporter = None


# ---------------------------------------------------------------------------
# Module-level constants
# ---------------------------------------------------------------------------

SERVICE_NAME: str = "provisioning-service"
"""Logical service identifier used for logging, tracing, and metrics."""

# ---------------------------------------------------------------------------
# Module-level logger — available after ``configure_logging`` is called, but
# safe to create eagerly because structlog defers configuration.
# ---------------------------------------------------------------------------

logger = get_logger(__name__)


# ============================================================================
# Provisioning API Blueprint
# ============================================================================

def _create_provisioning_blueprint() -> Blueprint:
    """Create the provisioning API blueprint with file export, database
    provisioning, and cloud storage export endpoints.

    The blueprint is defined in a factory function rather than at module
    level to allow for deferred import of exporter modules and to ensure
    clean testability.

    Returns:
        A Flask :class:`~flask.Blueprint` with all provisioning API
        routes registered under ``/api/v1/provision``.
    """
    bp = Blueprint("provisioning", __name__, url_prefix="/api/v1/provision")

    @bp.route("/export/file", methods=["POST"])
    def export_file() -> tuple:
        """Trigger a file export operation.

        Accepts a JSON payload specifying the generation job ID, output
        format (csv, json, parquet, sql), and optional formatting
        parameters.  Returns a provisioning job identifier for status
        tracking.

        Returns:
            Tuple of (JSON response, HTTP status code).
        """
        payload = request.get_json(silent=True)
        if not payload:
            return jsonify({
                "error": "Request body must be valid JSON",
                "code": 400,
                "request_id": getattr(g, "request_id", str(uuid.uuid4())),
            }), 400

        job_id = payload.get("job_id")
        output_format = payload.get("format", "csv")
        if not job_id:
            return jsonify({
                "error": "Missing required field: job_id",
                "code": 400,
                "request_id": getattr(g, "request_id", str(uuid.uuid4())),
            }), 400

        supported_formats = {"csv", "json", "jsonl", "parquet", "sql"}
        if output_format not in supported_formats:
            return jsonify({
                "error": f"Unsupported format '{output_format}'. "
                         f"Supported: {', '.join(sorted(supported_formats))}",
                "code": 400,
                "request_id": getattr(g, "request_id", str(uuid.uuid4())),
            }), 400

        provision_id = str(uuid.uuid4())
        logger.info(
            "file_export_requested",
            provision_id=provision_id,
            job_id=job_id,
            output_format=output_format,
            tenant_id=getattr(g, "tenant_id", None),
        )

        return _handle_file_export(provision_id, job_id, output_format, payload)

    @bp.route("/export/database", methods=["POST"])
    def provision_database() -> tuple:
        """Trigger a database provisioning operation.

        Accepts a JSON payload specifying the generation job ID, target
        database type (postgresql, oracle, sqlserver, hana), connection
        parameters, and optional batch configuration.

        Returns:
            Tuple of (JSON response, HTTP status code).
        """
        payload = request.get_json(silent=True)
        if not payload:
            return jsonify({
                "error": "Request body must be valid JSON",
                "code": 400,
                "request_id": getattr(g, "request_id", str(uuid.uuid4())),
            }), 400

        job_id = payload.get("job_id")
        db_type = payload.get("db_type")
        if not job_id or not db_type:
            return jsonify({
                "error": "Missing required fields: job_id, db_type",
                "code": 400,
                "request_id": getattr(g, "request_id", str(uuid.uuid4())),
            }), 400

        supported_dbs = {
            "postgresql", "postgres", "oracle", "oracle_ebs",
            "sqlserver", "mssql", "hana", "sap_hana",
        }
        if db_type not in supported_dbs:
            return jsonify({
                "error": f"Unsupported database type '{db_type}'. "
                         f"Supported: {', '.join(sorted(supported_dbs))}",
                "code": 400,
                "request_id": getattr(g, "request_id", str(uuid.uuid4())),
            }), 400

        provision_id = str(uuid.uuid4())
        logger.info(
            "database_provision_requested",
            provision_id=provision_id,
            job_id=job_id,
            db_type=db_type,
            tenant_id=getattr(g, "tenant_id", None),
        )

        return _handle_database_provision(provision_id, job_id, db_type, payload)

    @bp.route("/export/cloud", methods=["POST"])
    def export_cloud() -> tuple:
        """Trigger a cloud storage export operation.

        Accepts a JSON payload specifying the generation job ID, target
        cloud provider (s3, azure_blob, gcs), bucket/container name,
        and authentication parameters.

        Returns:
            Tuple of (JSON response, HTTP status code).
        """
        payload = request.get_json(silent=True)
        if not payload:
            return jsonify({
                "error": "Request body must be valid JSON",
                "code": 400,
                "request_id": getattr(g, "request_id", str(uuid.uuid4())),
            }), 400

        job_id = payload.get("job_id")
        provider = payload.get("provider")
        if not job_id or not provider:
            return jsonify({
                "error": "Missing required fields: job_id, provider",
                "code": 400,
                "request_id": getattr(g, "request_id", str(uuid.uuid4())),
            }), 400

        supported_providers = {"s3", "azure_blob", "gcs"}
        if provider not in supported_providers:
            return jsonify({
                "error": f"Unsupported cloud provider '{provider}'. "
                         f"Supported: {', '.join(sorted(supported_providers))}",
                "code": 400,
                "request_id": getattr(g, "request_id", str(uuid.uuid4())),
            }), 400

        provision_id = str(uuid.uuid4())
        logger.info(
            "cloud_export_requested",
            provision_id=provision_id,
            job_id=job_id,
            provider=provider,
            tenant_id=getattr(g, "tenant_id", None),
        )

        return _handle_cloud_export(provision_id, job_id, provider, payload)

    @bp.route("/status/<provision_id>", methods=["GET"])
    def get_provision_status(provision_id: str) -> tuple:
        """Retrieve the current status of a provisioning operation.

        Args:
            provision_id: UUID of the provisioning operation.

        Returns:
            Tuple of (JSON response, HTTP status code).
        """
        logger.info(
            "provision_status_requested",
            provision_id=provision_id,
            tenant_id=getattr(g, "tenant_id", None),
        )

        try:
            mongo = current_app.extensions.get("pymongo_client")
            if mongo is not None:
                db_name = current_app.config.get("MONGODB_DATABASE", "synthetic_erp")
                db = mongo[db_name]
                record = db["provisioning_jobs"].find_one(
                    {"provision_id": provision_id}
                )
                if record:
                    record.pop("_id", None)
                    return jsonify({
                        "provision_id": provision_id,
                        "status": record.get("status", "unknown"),
                        "details": record,
                        "request_id": getattr(g, "request_id", str(uuid.uuid4())),
                    }), 200
        except Exception as exc:
            logger.warning(
                "provision_status_lookup_error",
                provision_id=provision_id,
                error=str(exc),
            )

        return jsonify({
            "provision_id": provision_id,
            "status": "unknown",
            "message": "Provisioning job not found or status unavailable",
            "request_id": getattr(g, "request_id", str(uuid.uuid4())),
        }), 404

    return bp


# ============================================================================
# Provisioning Operation Helpers
# ============================================================================

def _handle_file_export(
    provision_id: str,
    job_id: str,
    output_format: str,
    payload: dict,
) -> tuple:
    """Execute a file export operation via :class:`FileExporter`.

    The function delegates to the eagerly-imported ``FileExporter`` class.
    If the exporter module is unavailable (``FileExporter is None``), a
    *503 Service Unavailable* response is returned.

    Args:
        provision_id: Unique provisioning operation identifier.
        job_id: The generation job ID whose output is being exported.
        output_format: Target format (``csv``, ``json``, ``jsonl``,
            ``parquet``, ``sql``).
        payload: Full request JSON body with config and options.

    Returns:
        Tuple of (JSON response, HTTP status code).
    """
    if FileExporter is None:
        logger.warning(
            "file_exporter_unavailable",
            provision_id=provision_id,
            detail="FileExporter module not yet available",
        )
        return jsonify({
            "error": "File export module is not available",
            "code": 503,
            "request_id": getattr(g, "request_id", str(uuid.uuid4())),
        }), 503

    try:
        exporter = FileExporter(config=payload.get("config", {}))
        exporter.export(
            job_id=job_id,
            output_format=output_format,
            provision_id=provision_id,
            options=payload.get("options", {}),
        )
    except Exception as exc:
        logger.error(
            "file_export_failed",
            provision_id=provision_id,
            error=str(exc),
        )
        return jsonify({
            "error": f"File export failed: {exc}",
            "code": 500,
            "request_id": getattr(g, "request_id", str(uuid.uuid4())),
        }), 500

    return jsonify({
        "provision_id": provision_id,
        "job_id": job_id,
        "format": output_format,
        "status": "accepted",
        "request_id": getattr(g, "request_id", str(uuid.uuid4())),
    }), 202


def _handle_database_provision(
    provision_id: str,
    job_id: str,
    db_type: str,
    payload: dict,
) -> tuple:
    """Execute a database provisioning operation via :class:`DatabaseExporter`.

    Args:
        provision_id: Unique provisioning operation identifier.
        job_id: The generation job ID whose output is being provisioned.
        db_type: Target database type (``postgresql``, ``oracle``,
            ``sqlserver``, ``hana``).
        payload: Full request JSON body with connection and options.

    Returns:
        Tuple of (JSON response, HTTP status code).
    """
    if DatabaseExporter is None:
        logger.warning(
            "database_exporter_unavailable",
            provision_id=provision_id,
            detail="DatabaseExporter module not yet available",
        )
        return jsonify({
            "error": "Database export module is not available",
            "code": 503,
            "request_id": getattr(g, "request_id", str(uuid.uuid4())),
        }), 503

    try:
        exporter = DatabaseExporter(config=payload.get("connection", {}))
        exporter.provision(
            job_id=job_id,
            db_type=db_type,
            provision_id=provision_id,
            options=payload.get("options", {}),
        )
    except ConnectionError as exc:
        logger.error(
            "database_connection_failed",
            provision_id=provision_id,
            db_type=db_type,
            error=str(exc),
        )
        return jsonify({
            "error": f"Target database unreachable: {exc}",
            "code": 502,
            "request_id": getattr(g, "request_id", str(uuid.uuid4())),
        }), 502
    except Exception as exc:
        logger.error(
            "database_provision_failed",
            provision_id=provision_id,
            error=str(exc),
        )
        return jsonify({
            "error": f"Database provisioning failed: {exc}",
            "code": 500,
            "request_id": getattr(g, "request_id", str(uuid.uuid4())),
        }), 500

    return jsonify({
        "provision_id": provision_id,
        "job_id": job_id,
        "db_type": db_type,
        "status": "accepted",
        "request_id": getattr(g, "request_id", str(uuid.uuid4())),
    }), 202


def _handle_cloud_export(
    provision_id: str,
    job_id: str,
    provider: str,
    payload: dict,
) -> tuple:
    """Execute a cloud storage export operation via :class:`FileExporter`.

    Args:
        provision_id: Unique provisioning operation identifier.
        job_id: The generation job ID whose output is being uploaded.
        provider: Cloud provider (``s3``, ``azure_blob``, ``gcs``).
        payload: Full request JSON body with cloud_config and options.

    Returns:
        Tuple of (JSON response, HTTP status code).
    """
    if FileExporter is None:
        logger.warning(
            "cloud_exporter_unavailable",
            provision_id=provision_id,
            detail="Cloud export module not yet available",
        )
        return jsonify({
            "error": "Cloud export module is not available",
            "code": 503,
            "request_id": getattr(g, "request_id", str(uuid.uuid4())),
        }), 503

    try:
        exporter = FileExporter(config=payload.get("config", {}))
        exporter.export_to_cloud(
            job_id=job_id,
            provider=provider,
            provision_id=provision_id,
            cloud_config=payload.get("cloud_config", {}),
            options=payload.get("options", {}),
        )
    except Exception as exc:
        logger.error(
            "cloud_export_failed",
            provision_id=provision_id,
            error=str(exc),
        )
        return jsonify({
            "error": f"Cloud export failed: {exc}",
            "code": 500,
            "request_id": getattr(g, "request_id", str(uuid.uuid4())),
        }), 500

    return jsonify({
        "provision_id": provision_id,
        "job_id": job_id,
        "provider": provider,
        "status": "accepted",
        "request_id": getattr(g, "request_id", str(uuid.uuid4())),
    }), 202


# ============================================================================
# Error Handler Registration
# ============================================================================

def _register_error_handlers(app: Flask) -> None:
    """Register structured JSON error handlers for common HTTP error codes.

    Each handler returns a JSON body with the keys ``error`` (human-readable
    message), ``code`` (HTTP status code), and ``request_id`` (correlation
    identifier for log tracing).

    Args:
        app: The Flask application instance to register handlers on.
    """

    @app.errorhandler(400)
    def bad_request(error: Exception) -> tuple:
        """Handle 400 Bad Request — invalid export configuration."""
        return jsonify({
            "error": str(error.description)
            if hasattr(error, "description")
            else "Invalid export configuration",
            "code": 400,
            "request_id": getattr(g, "request_id", str(uuid.uuid4())),
        }), 400

    @app.errorhandler(401)
    def unauthorized(error: Exception) -> tuple:
        """Handle 401 Unauthorized — missing or invalid JWT."""
        return jsonify({
            "error": "Authentication required. Provide a valid JWT token.",
            "code": 401,
            "request_id": getattr(g, "request_id", str(uuid.uuid4())),
        }), 401

    @app.errorhandler(403)
    def forbidden(error: Exception) -> tuple:
        """Handle 403 Forbidden — insufficient permissions."""
        return jsonify({
            "error": "Insufficient permissions for this operation.",
            "code": 403,
            "request_id": getattr(g, "request_id", str(uuid.uuid4())),
        }), 403

    @app.errorhandler(404)
    def not_found(error: Exception) -> tuple:
        """Handle 404 Not Found — resource not found."""
        return jsonify({
            "error": "The requested resource was not found.",
            "code": 404,
            "request_id": getattr(g, "request_id", str(uuid.uuid4())),
        }), 404

    @app.errorhandler(413)
    def payload_too_large(error: Exception) -> tuple:
        """Handle 413 Payload Too Large — export exceeds configured limits."""
        return jsonify({
            "error": "Export payload exceeds the maximum allowed size.",
            "code": 413,
            "request_id": getattr(g, "request_id", str(uuid.uuid4())),
        }), 413

    @app.errorhandler(500)
    def internal_server_error(error: Exception) -> tuple:
        """Handle 500 Internal Server Error — unexpected failures."""
        logger.error(
            "internal_server_error",
            error=str(error),
            request_id=getattr(g, "request_id", None),
        )
        return jsonify({
            "error": "An unexpected internal error occurred.",
            "code": 500,
            "request_id": getattr(g, "request_id", str(uuid.uuid4())),
        }), 500

    @app.errorhandler(502)
    def bad_gateway(error: Exception) -> tuple:
        """Handle 502 Bad Gateway — target database unreachable."""
        logger.error(
            "bad_gateway",
            error=str(error),
            request_id=getattr(g, "request_id", None),
        )
        return jsonify({
            "error": "Target database is unreachable. Check connection parameters.",
            "code": 502,
            "request_id": getattr(g, "request_id", str(uuid.uuid4())),
        }), 502

    @app.errorhandler(503)
    def service_unavailable(error: Exception) -> tuple:
        """Handle 503 Service Unavailable — cloud provider errors."""
        logger.error(
            "service_unavailable",
            error=str(error),
            request_id=getattr(g, "request_id", None),
        )
        return jsonify({
            "error": "Service temporarily unavailable. Cloud provider may be "
                     "experiencing issues.",
            "code": 503,
            "request_id": getattr(g, "request_id", str(uuid.uuid4())),
        }), 503


# ============================================================================
# Request Lifecycle Hooks
# ============================================================================

def _register_request_hooks(app: Flask) -> None:
    """Register ``before_request`` and ``after_request`` hooks.

    The *before_request* hook extracts multi-tenant context from JWT
    claims and HTTP headers, generating a unique request ID for
    distributed trace correlation.

    The *after_request* hook logs the completed response with status
    code, duration, and correlation IDs, then clears the request-scoped
    logging context to prevent cross-request leakage.

    Args:
        app: The Flask application instance to register hooks on.
    """

    @app.before_request
    def before_request_hook() -> None:
        """Extract tenant context and bind logging identifiers."""
        # Generate or propagate a unique request identifier.  Prefer the
        # X-Request-ID header (set by the API Gateway or Ingress) over a
        # locally-generated UUID for end-to-end trace correlation.
        g.request_id = (
            request.headers.get("X-Request-ID")
            or request.headers.get("X-Correlation-ID")
            or str(uuid.uuid4())
        )

        # Extract tenant identifier from JWT claims or headers.
        # In production, this is typically injected by the API Gateway
        # after JWT verification.
        g.tenant_id = (
            request.headers.get("X-Tenant-ID")
            or _extract_tenant_from_token()
        )

        # Extract user identifier from headers (injected by API Gateway).
        g.user_id = request.headers.get("X-User-ID")

        # Bind context variables for structured logging so that every
        # log event emitted during this request includes these fields.
        bind_context(
            request_id=g.request_id,
            tenant_id=g.tenant_id,
            user_id=g.user_id,
            method=request.method,
            path=request.path,
            service=SERVICE_NAME,
        )

        logger.debug(
            "request_started",
            remote_addr=request.remote_addr,
            content_length=request.content_length,
        )

    @app.after_request
    def after_request_hook(response):  # type: ignore[no-untyped-def]
        """Log response details and clear logging context."""
        logger.info(
            "request_completed",
            status_code=response.status_code,
            content_length=response.content_length,
            request_id=getattr(g, "request_id", None),
        )

        # Inject the request ID into the response headers so that
        # callers can correlate their client-side logs.
        response.headers["X-Request-ID"] = getattr(
            g, "request_id", str(uuid.uuid4())
        )

        # Clear the structlog context variables to prevent them from
        # leaking into subsequent requests handled by the same worker.
        clear_context()

        return response

    @app.teardown_request
    def teardown_request_hook(exception: BaseException | None = None) -> None:
        """Ensure logging context is cleared even on unhandled errors."""
        if exception is not None:
            logger.error(
                "request_teardown_exception",
                error=str(exception),
                request_id=getattr(g, "request_id", None),
            )
        # Belt-and-suspenders context cleanup.
        clear_context()


# ============================================================================
# Helpers
# ============================================================================

def _extract_tenant_from_token() -> str | None:
    """Attempt to extract the tenant identifier from the JWT ``Authorization``
    header without performing full token verification.

    The Provisioning Service is an internal service behind the API Gateway,
    so full JWT verification is handled upstream.  This function performs
    a lightweight decode of the payload section to extract ``tenant_id``
    for logging and namespace isolation.

    Returns:
        The tenant identifier string if present, otherwise ``None``.
    """
    auth_header = request.headers.get("Authorization", "")
    if not auth_header.startswith("Bearer "):
        return None

    token = auth_header[len("Bearer "):]
    parts = token.split(".")
    if len(parts) != 3:
        return None

    try:
        # Decode the payload section (index 1) with padding fix.
        payload_b64 = parts[1]
        # Add padding if necessary (JWT tokens strip trailing '=').
        padding = 4 - len(payload_b64) % 4
        if padding != 4:
            payload_b64 += "=" * padding

        payload_bytes = base64.urlsafe_b64decode(payload_b64)
        payload = json.loads(payload_bytes)
        tenant: str | None = payload.get("tenant_id") or payload.get("https://synth-erp/tenant_id")
        return tenant
    except (ValueError, KeyError, json.JSONDecodeError):
        return None


# ============================================================================
# Application Factory
# ============================================================================

def create_app(config_override: object | None = None) -> Flask:
    """Create and configure the Provisioning Service Flask application.

    This is the central Flask Application Factory implementing the standard
    ``create_app()`` pattern.  It initialises all extensions, registers
    blueprints, wires up middleware hooks, and returns a fully configured
    :class:`~flask.Flask` instance ready to serve requests.

    Args:
        config_override: An optional configuration object whose attributes
            will be loaded into ``app.config``.  When ``None``, the factory
            instantiates :class:`ProvisioningServiceConfig` (which reads
            environment variables) as the default configuration source.

    Returns:
        A fully initialised Flask application for the Provisioning Service.

    Raises:
        RuntimeError: If a critical extension (MongoDB, Redis) fails to
            initialise during startup.

    Example::

        # Default configuration from environment variables
        app = create_app()

        # Override with custom config for testing
        from provisioning_service.config import ProvisioningServiceConfig
        test_config = ProvisioningServiceConfig()
        test_config.TESTING = True
        app = create_app(config_override=test_config)
    """
    # ------------------------------------------------------------------
    # 1. Create the Flask application instance
    # ------------------------------------------------------------------
    app = Flask(__name__)

    # ------------------------------------------------------------------
    # 2. Load configuration
    # ------------------------------------------------------------------
    if config_override is not None:
        app.config.from_object(config_override)
    else:
        # Use ProvisioningServiceConfig as primary; fall back to the
        # environment-aware base config factory if the service-specific
        # config is unavailable (e.g. during minimal bootstrap).
        try:
            config = ProvisioningServiceConfig()
        except Exception:
            config = get_config()  # type: ignore[assignment]
        app.config.from_object(config)

    # Ensure SERVICE_NAME is available in config for health checks and
    # observability utilities that read it from app.config.
    app.config.setdefault("SERVICE_NAME", SERVICE_NAME)

    # ------------------------------------------------------------------
    # 3. Configure structured logging (before other initialisations so
    #    that all subsequent log events are captured in JSON format)
    # ------------------------------------------------------------------
    configure_logging(
        service_name=SERVICE_NAME,
        log_level=app.config.get("LOG_LEVEL", "INFO"),
        json_output=app.config.get("LOG_JSON_OUTPUT", True),
    )

    logger.info("provisioning_service_starting", config_class=type(
        config_override if config_override is not None
        else ProvisioningServiceConfig
    ).__name__)

    # ------------------------------------------------------------------
    # 4. Initialise Flask-CORS
    # ------------------------------------------------------------------
    cors_origins = app.config.get("CORS_ORIGINS", ["*"])
    CORS(
        app,
        origins=cors_origins,
        supports_credentials=True,
        allow_headers=["Content-Type", "Authorization", "X-Request-ID",
                       "X-Tenant-ID", "X-Correlation-ID"],
        expose_headers=["X-Request-ID"],
    )
    logger.info("cors_initialised", origins=cors_origins)

    # ------------------------------------------------------------------
    # 5. Initialise database connections
    # ------------------------------------------------------------------
    try:
        init_mongodb(app)
        logger.info("mongodb_initialised")
    except Exception as exc:
        logger.error("mongodb_init_failed", error=str(exc))
        raise RuntimeError(
            f"Failed to initialise MongoDB connection: {exc}"
        ) from exc

    try:
        init_redis(app)
        logger.info("redis_initialised")
    except Exception as exc:
        logger.error("redis_init_failed", error=str(exc))
        raise RuntimeError(
            f"Failed to initialise Redis connection: {exc}"
        ) from exc

    # ------------------------------------------------------------------
    # 6. Initialise OpenTelemetry distributed tracing
    # ------------------------------------------------------------------
    otel_endpoint = app.config.get("OTEL_EXPORTER_ENDPOINT") or None
    init_tracing(
        app,
        service_name=SERVICE_NAME,
        exporter_endpoint=otel_endpoint,
    )
    logger.info("tracing_initialised", otel_endpoint=otel_endpoint)

    # ------------------------------------------------------------------
    # 7. Initialise Prometheus metrics
    # ------------------------------------------------------------------
    setup_metrics(app, service_name=SERVICE_NAME)
    logger.info("metrics_initialised")

    # ------------------------------------------------------------------
    # 8. Register blueprints
    # ------------------------------------------------------------------
    # Health / readiness probes for Kubernetes liveness and readiness.
    init_health_checks(app)
    logger.info("health_blueprint_registered")

    # Provisioning API routes (file export, database provisioning, cloud
    # storage upload, and status endpoints).
    provisioning_bp = _create_provisioning_blueprint()
    app.register_blueprint(provisioning_bp)
    logger.info(
        "provisioning_blueprint_registered",
        url_prefix=provisioning_bp.url_prefix,
    )

    # ------------------------------------------------------------------
    # 9. Register error handlers
    # ------------------------------------------------------------------
    _register_error_handlers(app)
    logger.info("error_handlers_registered")

    # ------------------------------------------------------------------
    # 10. Register request lifecycle hooks
    # ------------------------------------------------------------------
    _register_request_hooks(app)
    logger.info("request_hooks_registered")

    # ------------------------------------------------------------------
    # Startup complete
    # ------------------------------------------------------------------
    logger.info(
        "provisioning_service_ready",
        service=SERVICE_NAME,
        debug=app.debug,
        testing=app.testing,
    )

    return app
