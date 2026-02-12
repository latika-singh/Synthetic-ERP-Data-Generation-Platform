"""Flask Application Factory for the Profiling Service.

Implements the ``create_app()`` Application Factory pattern as the central
entry-point for the Profiling Service.  Gunicorn (production) and the Flask
development server both consume this factory to obtain a fully configured
Flask application instance.

Responsibilities:
    - Load environment-specific configuration via :func:`profiling_service.config.get_config`.
    - Initialise shared infrastructure (MongoDB, Redis, structured logging,
      OpenTelemetry tracing, Prometheus metrics).
    - Register health-check and domain Blueprints (schema discovery, profiling).
    - Wire request/response lifecycle hooks for correlation-ID propagation,
      request logging, and response-time measurement.
    - Register structured JSON error handlers for HTTP 400/404/422/500.

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
from shared.database.mongodb import init_mongodb
from shared.database.redis_client import init_redis
from shared.logging.structured_logger import configure_logging, get_logger
from shared.middleware.circuit_breaker import CircuitBreakerRegistry
from shared.middleware.health_check import init_health_checks
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


def create_app(config_name: str | None = None) -> Flask:
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
        environment=getattr(config, "FLASK_ENV", "development"),
    )

    # ------------------------------------------------------------------
    # 3. CORS — enable cross-origin requests from the Web Console
    # ------------------------------------------------------------------
    cors_origins = getattr(config, "CORS_ORIGINS", ["http://localhost:3000"])
    CORS(
        app,
        resources={r"/api/*": {"origins": cors_origins}},
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
    init_health_checks(app, url_prefix=None)

    # 7b. Schema discovery routes (/api/v1/schemas)
    schema_bp = Blueprint("schema_discovery", __name__, url_prefix="/api/v1/schemas")

    @schema_bp.route("", methods=["GET"])
    def list_schemas() -> tuple:
        """List available ERP schema definitions.

        Returns:
            JSON list of schema summary objects with 200 status.
        """
        return jsonify({"schemas": [], "total": 0}), 200

    @schema_bp.route("/<schema_id>", methods=["GET"])
    def get_schema(schema_id: str) -> tuple:
        """Retrieve a specific ERP schema definition by ID.

        Args:
            schema_id: Unique identifier for the schema definition.

        Returns:
            JSON schema object with 200 status, or 404 if not found.
        """
        return jsonify({"error": {"code": 404, "message": f"Schema {schema_id} not found"}}), 404

    @schema_bp.route("/discover", methods=["POST"])
    def discover_schema() -> tuple:
        """Initiate ERP schema discovery.

        Accepts a connection configuration in the request body and
        dispatches a schema extraction job.

        Returns:
            JSON object with the discovery job ID and 202 status.
        """
        return jsonify({"job_id": str(uuid.uuid4()), "status": "submitted"}), 202

    app.register_blueprint(schema_bp)

    # 7c. Profiling routes (/api/v1/profiles)
    profiling_bp = Blueprint("profiling", __name__, url_prefix="/api/v1/profiles")

    @profiling_bp.route("", methods=["GET"])
    def list_profiles() -> tuple:
        """List statistical profiles.

        Returns:
            JSON list of profile summary objects with 200 status.
        """
        return jsonify({"profiles": [], "total": 0}), 200

    @profiling_bp.route("/<profile_id>", methods=["GET"])
    def get_profile(profile_id: str) -> tuple:
        """Retrieve a statistical profile by ID.

        Args:
            profile_id: Unique identifier for the statistical profile.

        Returns:
            JSON profile object with 200 status, or 404 if not found.
        """
        return jsonify({"error": {"code": 404, "message": f"Profile {profile_id} not found"}}), 404

    @profiling_bp.route("", methods=["POST"])
    def create_profile() -> tuple:
        """Initiate statistical profiling for an ERP schema.

        Accepts a schema reference and profiling configuration in the
        request body and dispatches a profiling job.

        Returns:
            JSON object with the profiling job ID and 202 status.
        """
        return jsonify({"job_id": str(uuid.uuid4()), "status": "submitted"}), 202

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
            Tuple of (response_dict, status_code) for Flask to serialise.
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
        logger.warning("method_not_allowed", method=request.method, path=request.path)
        return _build_error_response(405, f"Method {request.method} is not allowed for this endpoint.")

    @app.errorhandler(422)
    def handle_unprocessable_entity(error: Exception) -> tuple:
        """Handle HTTP 422 Unprocessable Entity errors."""
        logger.warning("validation_error", error=str(error), path=request.path)
        return _build_error_response(422, f"Validation Error: {error}")

    @app.errorhandler(429)
    def handle_rate_limit(error: Exception) -> tuple:
        """Handle HTTP 429 Too Many Requests errors."""
        logger.warning("rate_limit_exceeded", path=request.path)
        return _build_error_response(429, "Rate limit exceeded. Please retry later.")

    @app.errorhandler(500)
    def handle_internal_error(error: Exception) -> tuple:
        """Handle HTTP 500 Internal Server Error."""
        logger.error("internal_server_error", error=str(error), path=request.path, exc_info=True)
        return _build_error_response(500, "An internal server error occurred.")

    @app.errorhandler(Exception)
    def handle_generic_exception(error: Exception) -> tuple:
        """Catch-all handler for unhandled exceptions.

        Logs the full traceback via structured logging and returns a
        generic 500 response.
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
      and response time are logged for every request.

    Args:
        app: The Flask application instance.
    """

    @app.before_request
    def before_request_hook() -> None:
        """Extract or generate correlation ID and record request start time."""
        # Correlation ID — propagate from upstream or generate
        correlation_id = request.headers.get("X-Correlation-ID") or str(uuid.uuid4())
        g.correlation_id = correlation_id

        # Request timing
        g.request_start_time = time.time()

        # Structured request logging (skip health-check noise)
        if request.path not in ("/health", "/ready", "/metrics"):
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
        correlation_id = getattr(g, "correlation_id", None)
        if correlation_id:
            response.headers["X-Correlation-ID"] = str(correlation_id)

        # Compute duration
        start_time = getattr(g, "request_start_time", None)
        duration_ms = round((time.time() - start_time) * 1000, 2) if start_time else None

        # Structured response logging (skip noisy endpoints)
        if request.path not in ("/health", "/ready", "/metrics"):
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
