"""Flask Application Factory for the Profiling Service.

Implements the ``create_app()`` Application Factory pattern as the central
entry-point for the Profiling Service.  Gunicorn (production) and the Flask
development server both consume this factory to obtain a fully configured
Flask application instance.

Responsibilities:
    - Load environment-specific configuration via
      :func:`profiling_service.config.get_config`.
    - Initialise shared infrastructure (MongoDB, Redis, structured logging,
      OpenTelemetry tracing, Prometheus metrics).
    - Register health-check and domain Blueprints (schema discovery, profiling).
    - Wire request/response lifecycle hooks for correlation-ID propagation,
      request logging, and response-time measurement.
    - Register structured JSON error handlers for HTTP 400/404/405/422/429/500.

Usage::

    # Development server
    from profiling_service.app import create_app
    app = create_app("development")
    app.run(host="0.0.0.0", port=8002)

    # Gunicorn
    gunicorn --bind 0.0.0.0:8002 'profiling_service.app:create_app()'

Constraint C-001:
    No production data is accessed or stored. The Profiling Service handles
    only schema metadata and statistical aggregates.
"""

from __future__ import annotations

import time
import uuid

from flask import Blueprint, Flask, g, jsonify, request
from flask_cors import CORS

from profiling_service.config import ProfilingServiceConfig, get_config
from shared.database.mongodb import get_mongo_db, init_mongodb
from shared.database.redis_client import init_redis
from shared.logging.structured_logger import configure_logging, get_logger
from shared.middleware.circuit_breaker import CircuitBreakerRegistry
from shared.middleware.health_check import health_blueprint
from shared.observability.metrics import setup_metrics
from shared.observability.tracing import init_tracing


# ---------------------------------------------------------------------------
# Module-level logger — bound after ``configure_logging()`` is called in
# ``create_app()``.  Created eagerly so that import-time log calls
# still have a valid (unconfigured) logger rather than ``None``.
# ---------------------------------------------------------------------------
logger = get_logger(__name__)


# ===========================================================================
# Application Factory
# ===========================================================================


def create_app(config_name: str | None = None) -> Flask:  # noqa: PLR0915
    """Create, configure, and return a Flask application instance.

    This is the canonical Application Factory for the Profiling Service.
    Every extension, Blueprint, middleware hook, and error handler is
    registered within this function so that multiple independent app
    instances can coexist (e.g. for testing).

    Args:
        config_name: Explicit environment name (``"development"``,
            ``"testing"``, ``"production"``).  When ``None``, the
            ``FLASK_ENV`` environment variable is inspected via
            :func:`profiling_service.config.get_config`.

    Returns:
        A fully initialised :class:`~flask.Flask` application ready to
        handle HTTP requests.

    Example::

        app = create_app("testing")
        with app.test_client() as client:
            resp = client.get("/health")
            assert resp.status_code == 200
    """

    # ------------------------------------------------------------------
    # 1. Flask application & configuration
    # ------------------------------------------------------------------
    app = Flask(__name__)
    config: ProfilingServiceConfig = get_config(config_name)
    app.config.from_object(config)

    # Ensure SERVICE_NAME and SERVICE_PORT are always accessible from
    # app.config for health-check, metrics, and logging consumers.
    app.config.setdefault("SERVICE_NAME", config.SERVICE_NAME)
    app.config.setdefault("SERVICE_PORT", config.SERVICE_PORT)

    # ------------------------------------------------------------------
    # 2. Structured logging (must be first so all subsequent init logs
    #    go through the structured pipeline)
    # ------------------------------------------------------------------
    configure_logging(
        service_name=config.SERVICE_NAME,
        log_level=getattr(config, "LOG_LEVEL", None),
        json_output=not config.DEBUG,
    )

    logger.info(
        "profiling_service_init_started",
        service_name=config.SERVICE_NAME,
        service_port=config.SERVICE_PORT,
        cors_origins=config.CORS_ORIGINS,
        environment=getattr(config, "FLASK_ENV", "development"),
    )

    # ------------------------------------------------------------------
    # 3. CORS — enable cross-origin requests from the Web Console
    # ------------------------------------------------------------------
    CORS(
        app,
        resources={r"/api/*": {"origins": config.CORS_ORIGINS}},
        supports_credentials=True,
    )

    # ------------------------------------------------------------------
    # 4. Database & caching infrastructure
    # ------------------------------------------------------------------
    if not config.TESTING:
        try:
            init_mongodb(app)
        except Exception:
            logger.warning(
                "mongodb_init_failed",
                exc_info=True,
                hint="Service will start but MongoDB-dependent routes may fail.",
            )

        try:
            init_redis(app)
        except Exception:
            logger.warning(
                "redis_init_failed",
                exc_info=True,
                hint="Service will start but Redis-dependent caching may fail.",
            )
    else:
        # In testing mode, let tests inject their own mocks/stubs.
        logger.info("skipping_db_init_in_testing_mode")

    # ------------------------------------------------------------------
    # 5. Observability — tracing & metrics
    # ------------------------------------------------------------------
    if not config.TESTING:
        try:
            init_tracing(
                app=app,
                service_name=config.SERVICE_NAME,
            )
        except Exception:
            logger.warning("tracing_init_failed", exc_info=True)

        try:
            setup_metrics(app, service_name=config.SERVICE_NAME)
        except Exception:
            logger.warning("metrics_init_failed", exc_info=True)

    # ------------------------------------------------------------------
    # 6. Circuit-breaker registry
    # ------------------------------------------------------------------
    _registry = CircuitBreakerRegistry.get_instance()
    app.extensions["circuit_breaker_registry"] = _registry

    # ------------------------------------------------------------------
    # 7. Blueprints
    # ------------------------------------------------------------------

    # 7a. Health / readiness probes (at application root)
    app.register_blueprint(health_blueprint)

    # 7b. Schema discovery routes (/api/v1/schemas)
    schema_bp = Blueprint("schema_discovery", __name__, url_prefix="/api/v1/schemas")

    @schema_bp.route("", methods=["GET"])
    def list_schemas() -> tuple:
        """List available ERP schema definitions with pagination.

        Supports optional query parameters:
            - ``page``: Page number (default ``1``).
            - ``per_page``: Results per page (default ``20``, max ``100``).
            - ``erp_type``: Filter by ERP system type.

        Returns:
            JSON list of schema summary objects with HTTP 200.
        """
        try:
            db = get_mongo_db()
            collection = db["schema_definitions"]

            page: int = request.args.get("page", 1, type=int)
            per_page: int = min(request.args.get("per_page", 20, type=int), 100)
            erp_type: str | None = request.args.get("erp_type")

            query_filter: dict = {}
            if erp_type:
                query_filter["erp_type"] = erp_type

            total: int = collection.count_documents(query_filter)
            skip: int = max((page - 1) * per_page, 0)

            cursor = (
                collection.find(query_filter, {"_id": 0})
                .skip(skip)
                .limit(per_page)
                .sort("created_at", -1)
            )
            schemas: list = list(cursor)

            return (
                jsonify(
                    {
                        "schemas": schemas,
                        "total": total,
                        "page": page,
                        "per_page": per_page,
                    }
                ),
                200,
            )
        except Exception as exc:
            logger.error("list_schemas_failed", error=str(exc), exc_info=True)
            correlation_id = getattr(g, "correlation_id", "unknown")
            return (
                jsonify(
                    {
                        "error": {
                            "code": 500,
                            "message": "Failed to retrieve schemas.",
                            "correlation_id": str(correlation_id),
                        }
                    }
                ),
                500,
            )

    @schema_bp.route("/<schema_id>", methods=["GET"])
    def get_schema(schema_id: str) -> tuple:
        """Retrieve a specific ERP schema definition by its identifier.

        Args:
            schema_id: Unique identifier for the schema definition.

        Returns:
            JSON schema object with HTTP 200, or HTTP 404 if not found.
        """
        try:
            db = get_mongo_db()
            collection = db["schema_definitions"]

            schema_doc = collection.find_one(
                {"schema_id": schema_id},
                {"_id": 0},
            )

            if schema_doc is None:
                return (
                    jsonify(
                        {
                            "error": {
                                "code": 404,
                                "message": f"Schema {schema_id} not found",
                                "correlation_id": getattr(g, "correlation_id", "unknown"),
                            }
                        }
                    ),
                    404,
                )

            return jsonify(schema_doc), 200
        except Exception as exc:
            logger.error(
                "get_schema_failed",
                error=str(exc),
                schema_id=schema_id,
                exc_info=True,
            )
            return (
                jsonify(
                    {
                        "error": {
                            "code": 500,
                            "message": "Failed to retrieve schema.",
                            "correlation_id": getattr(g, "correlation_id", "unknown"),
                        }
                    }
                ),
                500,
            )

    @schema_bp.route("/discover", methods=["POST"])
    def discover_schema() -> tuple:
        """Initiate ERP schema discovery.

        Accepts a JSON body containing ``erp_type`` and connection parameters.
        Creates a discovery job record in MongoDB and returns the job ID.

        Returns:
            JSON with ``job_id`` and ``status`` at HTTP 202, or an error
            response for validation / server failures.
        """
        payload = request.get_json(silent=True)
        if not payload:
            return (
                jsonify(
                    {
                        "error": {
                            "code": 400,
                            "message": "Request body is required.",
                            "correlation_id": getattr(g, "correlation_id", "unknown"),
                        }
                    }
                ),
                400,
            )

        erp_type: str | None = payload.get("erp_type")
        if not erp_type:
            return (
                jsonify(
                    {
                        "error": {
                            "code": 422,
                            "message": "Field 'erp_type' is required.",
                            "correlation_id": getattr(g, "correlation_id", "unknown"),
                        }
                    }
                ),
                422,
            )

        job_id: str = str(uuid.uuid4())

        try:
            db = get_mongo_db()
            collection = db["schema_definitions"]

            discovery_record: dict = {
                "job_id": job_id,
                "schema_id": str(uuid.uuid4()),
                "erp_type": erp_type,
                "status": "submitted",
                "connection_config": {
                    k: v
                    for k, v in payload.items()
                    if k not in ("password", "client_secret", "api_key")
                },
                "created_at": time.time(),
            }
            collection.insert_one(discovery_record)

            logger.info(
                "schema_discovery_submitted",
                job_id=job_id,
                erp_type=erp_type,
            )

            return (
                jsonify({"job_id": job_id, "status": "submitted"}),
                202,
            )
        except Exception as exc:
            logger.error(
                "schema_discovery_failed",
                error=str(exc),
                erp_type=erp_type,
                exc_info=True,
            )
            return (
                jsonify(
                    {
                        "error": {
                            "code": 500,
                            "message": "Failed to submit schema discovery.",
                            "correlation_id": getattr(g, "correlation_id", "unknown"),
                        }
                    }
                ),
                500,
            )

    app.register_blueprint(schema_bp)

    # 7c. Profiling routes (/api/v1/profiles)
    profiling_bp = Blueprint("profiling", __name__, url_prefix="/api/v1/profiles")

    @profiling_bp.route("", methods=["GET"])
    def list_profiles() -> tuple:
        """List statistical profiles with pagination.

        Supports optional query parameters:
            - ``page``: Page number (default ``1``).
            - ``per_page``: Results per page (default ``20``, max ``100``).
            - ``schema_id``: Filter profiles by schema reference.

        Returns:
            JSON list of profile summary objects with HTTP 200.
        """
        try:
            db = get_mongo_db()
            collection = db["statistical_profiles"]

            page: int = request.args.get("page", 1, type=int)
            per_page: int = min(request.args.get("per_page", 20, type=int), 100)
            schema_id: str | None = request.args.get("schema_id")

            query_filter: dict = {}
            if schema_id:
                query_filter["schema_id"] = schema_id

            total: int = collection.count_documents(query_filter)
            skip: int = max((page - 1) * per_page, 0)

            cursor = (
                collection.find(query_filter, {"_id": 0})
                .skip(skip)
                .limit(per_page)
                .sort("created_at", -1)
            )
            profiles: list = list(cursor)

            return (
                jsonify(
                    {
                        "profiles": profiles,
                        "total": total,
                        "page": page,
                        "per_page": per_page,
                    }
                ),
                200,
            )
        except Exception as exc:
            logger.error("list_profiles_failed", error=str(exc), exc_info=True)
            correlation_id = getattr(g, "correlation_id", "unknown")
            return (
                jsonify(
                    {
                        "error": {
                            "code": 500,
                            "message": "Failed to retrieve profiles.",
                            "correlation_id": str(correlation_id),
                        }
                    }
                ),
                500,
            )

    @profiling_bp.route("/<profile_id>", methods=["GET"])
    def get_profile(profile_id: str) -> tuple:
        """Retrieve a statistical profile by its identifier.

        Args:
            profile_id: Unique identifier for the statistical profile.

        Returns:
            JSON profile object with HTTP 200, or HTTP 404 if not found.
        """
        try:
            db = get_mongo_db()
            collection = db["statistical_profiles"]

            profile_doc = collection.find_one(
                {"profile_id": profile_id},
                {"_id": 0},
            )

            if profile_doc is None:
                return (
                    jsonify(
                        {
                            "error": {
                                "code": 404,
                                "message": f"Profile {profile_id} not found",
                                "correlation_id": getattr(g, "correlation_id", "unknown"),
                            }
                        }
                    ),
                    404,
                )

            return jsonify(profile_doc), 200
        except Exception as exc:
            logger.error(
                "get_profile_failed",
                error=str(exc),
                profile_id=profile_id,
                exc_info=True,
            )
            return (
                jsonify(
                    {
                        "error": {
                            "code": 500,
                            "message": "Failed to retrieve profile.",
                            "correlation_id": getattr(g, "correlation_id", "unknown"),
                        }
                    }
                ),
                500,
            )

    @profiling_bp.route("", methods=["POST"])
    def create_profile() -> tuple:
        """Initiate statistical profiling for an ERP schema.

        Accepts a JSON body containing ``schema_id`` and optional profiling
        configuration.  Creates a profiling job record and returns its ID.

        Returns:
            JSON with ``job_id`` and ``status`` at HTTP 202, or an error
            response for validation / server failures.
        """
        payload = request.get_json(silent=True)
        if not payload:
            return (
                jsonify(
                    {
                        "error": {
                            "code": 400,
                            "message": "Request body is required.",
                            "correlation_id": getattr(g, "correlation_id", "unknown"),
                        }
                    }
                ),
                400,
            )

        schema_id: str | None = payload.get("schema_id")
        if not schema_id:
            return (
                jsonify(
                    {
                        "error": {
                            "code": 422,
                            "message": "Field 'schema_id' is required.",
                            "correlation_id": getattr(g, "correlation_id", "unknown"),
                        }
                    }
                ),
                422,
            )

        job_id: str = str(uuid.uuid4())

        try:
            db = get_mongo_db()
            collection = db["statistical_profiles"]

            profiling_record: dict = {
                "job_id": job_id,
                "profile_id": str(uuid.uuid4()),
                "schema_id": schema_id,
                "status": "submitted",
                "config": payload.get("config", {}),
                "created_at": time.time(),
            }
            collection.insert_one(profiling_record)

            logger.info(
                "profiling_job_submitted",
                job_id=job_id,
                schema_id=schema_id,
            )

            return (
                jsonify({"job_id": job_id, "status": "submitted"}),
                202,
            )
        except Exception as exc:
            logger.error(
                "profiling_submission_failed",
                error=str(exc),
                schema_id=schema_id,
                exc_info=True,
            )
            return (
                jsonify(
                    {
                        "error": {
                            "code": 500,
                            "message": "Failed to submit profiling job.",
                            "correlation_id": getattr(g, "correlation_id", "unknown"),
                        }
                    }
                ),
                500,
            )

    app.register_blueprint(profiling_bp)

    # ------------------------------------------------------------------
    # 8. Error handlers
    # ------------------------------------------------------------------
    _register_error_handlers(app)

    # ------------------------------------------------------------------
    # 9. Request / response lifecycle hooks
    # ------------------------------------------------------------------
    _register_lifecycle_hooks(app)

    # ------------------------------------------------------------------
    # 10. Startup complete
    # ------------------------------------------------------------------
    logger.info(
        "profiling_service_init_complete",
        service_name=config.SERVICE_NAME,
        service_port=config.SERVICE_PORT,
        environment=getattr(config, "FLASK_ENV", "development"),
        debug=config.DEBUG,
    )

    return app


# ===========================================================================
# Error handlers
# ===========================================================================


def _register_error_handlers(app: Flask) -> None:
    """Register structured JSON error handlers on the Flask application.

    All error responses follow a consistent envelope format::

        {
            "error": {
                "code": <http_status_code>,
                "message": "<human_readable_message>",
                "correlation_id": "<request_correlation_id>"
            }
        }

    Args:
        app: The Flask application instance to register handlers on.
    """

    def _build_error_response(status_code: int, message: str) -> tuple:
        """Build a standardised JSON error response.

        Args:
            status_code: HTTP status code.
            message: Human-readable error message.

        Returns:
            Tuple of ``(response, status_code)`` for Flask to serialise.
        """
        correlation_id = getattr(g, "correlation_id", "unknown")
        return (
            jsonify(
                {
                    "error": {
                        "code": status_code,
                        "message": message,
                        "correlation_id": str(correlation_id),
                    }
                }
            ),
            status_code,
        )

    @app.errorhandler(400)
    def handle_bad_request(error: Exception) -> tuple:
        """Handle HTTP 400 Bad Request errors."""
        logger.warning("bad_request", error=str(error), path=request.path)
        return _build_error_response(400, f"Bad Request: {error}")

    @app.errorhandler(404)
    def handle_not_found(error: Exception) -> tuple:
        """Handle HTTP 404 Not Found errors."""
        logger.info("not_found", path=request.path)
        return _build_error_response(404, "The requested resource was not found.")

    @app.errorhandler(405)
    def handle_method_not_allowed(error: Exception) -> tuple:
        """Handle HTTP 405 Method Not Allowed errors."""
        logger.warning(
            "method_not_allowed", method=request.method, path=request.path
        )
        return _build_error_response(
            405, f"Method {request.method} is not allowed for this endpoint."
        )

    @app.errorhandler(422)
    def handle_unprocessable_entity(error: Exception) -> tuple:
        """Handle HTTP 422 Unprocessable Entity errors."""
        logger.warning("validation_error", error=str(error), path=request.path)
        return _build_error_response(422, f"Validation Error: {error}")

    @app.errorhandler(429)
    def handle_rate_limit(error: Exception) -> tuple:
        """Handle HTTP 429 Too Many Requests errors."""
        logger.warning("rate_limit_exceeded", path=request.path)
        return _build_error_response(
            429, "Rate limit exceeded. Please retry later."
        )

    @app.errorhandler(500)
    def handle_internal_error(error: Exception) -> tuple:
        """Handle HTTP 500 Internal Server Error."""
        logger.error(
            "internal_server_error",
            error=str(error),
            path=request.path,
            exc_info=True,
        )
        return _build_error_response(500, "An internal server error occurred.")

    @app.errorhandler(Exception)
    def handle_generic_exception(error: Exception) -> tuple:
        """Catch-all handler for unhandled exceptions.

        Logs the full traceback via structured logging and returns a
        generic 500 response to prevent internal details from leaking
        to clients.
        """
        logger.error(
            "unhandled_exception",
            error_type=type(error).__name__,
            error=str(error),
            path=request.path,
            exc_info=True,
        )
        return _build_error_response(500, "An unexpected error occurred.")


# ===========================================================================
# Lifecycle hooks
# ===========================================================================


def _register_lifecycle_hooks(app: Flask) -> None:
    """Register before-request, after-request, and teardown hooks.

    These hooks provide:

    - **Correlation ID propagation**: Every request is tagged with a unique
      ID (from the ``X-Correlation-ID`` header or auto-generated UUID4).
    - **Request/response logging**: Method, path, query params, status code,
      and response time are logged for every non-infrastructure request.

    Args:
        app: The Flask application instance.
    """

    # Paths excluded from verbose logging to avoid noise from Kubernetes
    # probes and Prometheus scraping.
    _silent_paths: frozenset = frozenset(("/health", "/ready", "/metrics"))

    @app.before_request
    def before_request_hook() -> None:
        """Extract or generate correlation ID and record request start time."""
        # Correlation ID — propagate from upstream or generate
        correlation_id: str = request.headers.get(
            "X-Correlation-ID"
        ) or str(uuid.uuid4())
        g.correlation_id = correlation_id

        # Request timing
        g.request_start_time = time.time()

        # Structured request logging (skip health-check noise)
        if request.path not in _silent_paths:
            logger.info(
                "http_request_started",
                method=request.method,
                path=request.path,
                query=dict(request.args) if request.args else None,
                correlation_id=correlation_id,
            )

    @app.after_request
    def after_request_hook(response):
        """Add correlation ID to response headers and log response details."""
        # Propagate correlation ID downstream
        correlation_id: str | None = getattr(g, "correlation_id", None)
        if correlation_id:
            response.headers["X-Correlation-ID"] = str(correlation_id)

        # Compute duration
        start_time: float | None = getattr(g, "request_start_time", None)
        duration_ms: float | None = (
            round((time.time() - start_time) * 1000, 2)
            if start_time is not None
            else None
        )

        # Structured response logging (skip noisy infrastructure endpoints)
        if request.path not in _silent_paths:
            logger.info(
                "http_request_completed",
                method=request.method,
                path=request.path,
                status_code=response.status_code,
                duration_ms=duration_ms,
                correlation_id=correlation_id,
            )

        return response

    @app.teardown_appcontext
    def teardown_hook(exception: BaseException | None = None) -> None:
        """Clean up per-request resources on application context teardown.

        Args:
            exception: The exception that caused the teardown, if any.
        """
        if exception is not None:
            logger.debug(
                "appcontext_teardown_with_exception",
                error_type=type(exception).__name__,
                error=str(exception),
            )
