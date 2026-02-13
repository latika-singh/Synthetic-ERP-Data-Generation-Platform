"""Flask Application Factory for the API Gateway service.

Implements the Application Factory pattern recommended for Flask microservices,
providing a configurable ``create_app()`` function that produces a fully
wired Flask application with:

- Environment-specific configuration (development, testing, production)
- All Flask extensions (JWT, CORS, PyMongo, Redis)
- All middleware (JWT auth, rate limiting, error handling, logging, tenant)
- All route Blueprints (generation, profiles, schemas, templates, export,
  auth, admin, health, monitoring)
- Structured JSON logging via structlog
- OpenTelemetry distributed tracing

This is the central orchestration point for the entire API Gateway.  Without
it, no routes, middleware, or extensions would be registered and the service
cannot start.

Typical usage::

    from api_gateway.app import create_app

    # Development server
    app = create_app("development")
    app.run(debug=True)

    # Production (via Gunicorn)
    app = create_app("production")

    # Testing
    app = create_app("testing")
    client = app.test_client()
"""

from __future__ import annotations

import os

from flask import Flask, jsonify

from api_gateway.config import config_map
from api_gateway.extensions import init_extensions
from api_gateway.middleware import register_all_middleware
from api_gateway.routes import register_blueprints
from shared.logging.structured_logger import configure_logging, get_logger


# ---------------------------------------------------------------------------
# Module-level constants
# ---------------------------------------------------------------------------

_SERVICE_NAME: str = "api-gateway"
"""Service name used in logging and tracing configuration."""

_DEFAULT_ENV: str = "development"
"""Fallback environment when FLASK_ENV is not set."""


# ---------------------------------------------------------------------------
# Application Factory
# ---------------------------------------------------------------------------


def create_app(config_name: str | None = None) -> Flask:
    """Create and configure a Flask application instance.

    This is the Application Factory function (per Flask best practices)
    that produces a fully initialised Flask application.  It performs
    the following steps in order:

    1. Create the Flask instance.
    2. Load environment-specific configuration.
    3. Configure structured logging.
    4. Initialise Flask extensions (JWT, CORS, PyMongo, Redis).
    5. Register middleware in the correct execution order.
    6. Register all route Blueprints with URL prefixes.
    7. Register global error handlers.
    8. Attempt to initialise OpenTelemetry tracing (non-fatal on failure).
    9. Log startup information.

    Args:
        config_name: One of ``"development"``, ``"testing"``, or
            ``"production"``.  Defaults to the ``FLASK_ENV`` environment
            variable or ``"development"`` if not set.

    Returns:
        A fully configured Flask application instance ready to serve
        requests.

    Example::

        app = create_app("testing")
        with app.test_client() as client:
            response = client.get("/health")
            assert response.status_code == 200
    """
    # --- Resolve environment ---
    env = config_name or os.environ.get("FLASK_ENV", _DEFAULT_ENV)
    config_class = config_map.get(env)
    if config_class is None:
        config_class = config_map.get(_DEFAULT_ENV)

    # --- Create Flask instance ---
    app = Flask(__name__)
    app.config.from_object(config_class)

    # --- Structured logging ---
    log_level = "DEBUG" if env == "development" else "WARNING"
    try:
        configure_logging(
            service_name=_SERVICE_NAME,
            log_level=log_level,
            json_output=(env != "development"),
        )
    except Exception:
        # Logging configuration is non-fatal — fall back to defaults
        pass

    logger = get_logger(__name__)

    # --- Flask extensions (JWT, CORS, PyMongo, Redis) ---
    init_extensions(app)

    # --- Middleware pipeline (logging → auth → tenant → rate limit → errors) ---
    register_all_middleware(app)

    # --- Route Blueprints ---
    register_blueprints(app)

    # --- Global error handlers ---
    _register_error_handlers(app)

    # --- OpenTelemetry tracing (non-fatal) ---
    try:
        from shared.observability.tracing import init_tracing

        init_tracing(app, service_name=_SERVICE_NAME)
    except Exception as exc:
        logger.warning(
            "OpenTelemetry tracing initialisation skipped",
            error=str(exc),
        )

    # --- Startup log ---
    logger.info(
        "API Gateway application created",
        service=_SERVICE_NAME,
        environment=env,
        config_class=config_class.__name__ if config_class else "unknown",
    )

    return app


# ---------------------------------------------------------------------------
# Global error handlers
# ---------------------------------------------------------------------------


def _register_error_handlers(app: Flask) -> None:
    """Register global HTTP error handlers returning structured JSON.

    These handlers ensure that all error responses conform to a consistent
    JSON envelope structure regardless of where the error originates
    (middleware, routes, or Flask internals).

    Args:
        app: The Flask application instance.
    """

    @app.errorhandler(400)
    def handle_bad_request(error: Exception) -> tuple:
        """Handle 400 Bad Request errors."""
        return (
            jsonify(
                {
                    "error": "Bad Request",
                    "code": "BAD_REQUEST",
                    "message": str(error),
                }
            ),
            400,
        )

    @app.errorhandler(401)
    def handle_unauthorized(error: Exception) -> tuple:
        """Handle 401 Unauthorized errors."""
        return (
            jsonify(
                {
                    "error": "Unauthorized",
                    "code": "UNAUTHORIZED",
                    "message": "Authentication required.",
                }
            ),
            401,
        )

    @app.errorhandler(403)
    def handle_forbidden(error: Exception) -> tuple:
        """Handle 403 Forbidden errors."""
        return (
            jsonify(
                {
                    "error": "Forbidden",
                    "code": "FORBIDDEN",
                    "message": "Insufficient permissions.",
                }
            ),
            403,
        )

    @app.errorhandler(404)
    def handle_not_found(error: Exception) -> tuple:
        """Handle 404 Not Found errors."""
        return (
            jsonify(
                {
                    "error": "Not Found",
                    "code": "NOT_FOUND",
                    "message": "The requested resource was not found.",
                }
            ),
            404,
        )

    @app.errorhandler(429)
    def handle_rate_limited(error: Exception) -> tuple:
        """Handle 429 Too Many Requests errors."""
        return (
            jsonify(
                {
                    "error": "Too Many Requests",
                    "code": "RATE_LIMITED",
                    "message": "Rate limit exceeded. Please retry later.",
                }
            ),
            429,
        )

    @app.errorhandler(500)
    def handle_internal_error(error: Exception) -> tuple:
        """Handle 500 Internal Server Error.

        In production, stack traces are never exposed to the client
        per security best practices.
        """
        return (
            jsonify(
                {
                    "error": "Internal Server Error",
                    "code": "INTERNAL_ERROR",
                    "message": "An unexpected error occurred.",
                }
            ),
            500,
        )
