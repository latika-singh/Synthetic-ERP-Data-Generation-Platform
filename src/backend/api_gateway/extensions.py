"""Flask extension initialization module for the API Gateway service.

Creates and configures all Flask extensions required by the API Gateway:

- **Flask-JWT-Extended** (4.6.x) — Auth0-based JWT authentication with RS256
  token verification, decorator-based route protection, and structured error
  responses for expired / invalid / missing / revoked tokens.
- **Flask-CORS** (4.0.x) — Cross-origin request handling so that the React 19.x
  Web Console frontend can communicate with the API Gateway from a different
  origin.
- **PyMongo** (4.x) — MongoDB 7.0 connectivity via the shared singleton
  ``MongoClient`` with connection pooling (``maxPoolSize=100``), TLS support,
  and automatic index creation on the five core collections.
- **Redis** (5.x) — Redis 7.x session/cache management via the shared singleton
  ``redis.Redis`` client with connection pooling.
- **Prometheus** (0.20.x) — Custom ``CollectorRegistry`` for API Gateway-specific
  metrics, separate from the default global registry.

Extensions are instantiated at **module level** (without app binding) and then
initialised inside :func:`init_extensions` following the standard Flask extension
pattern.  This allows the ``app.py`` application factory (``create_app()``) to
call ``init_extensions(app)`` exactly once to wire everything up.

The helper functions :func:`get_db` and :func:`get_redis` provide convenient
access to the MongoDB database and Redis client from within a Flask request
context or application context.

Environment Variables Read (via app.config or shared modules):
    CORS_ORIGINS: Allowed CORS origin(s) for the ``/api/*`` route namespace.
        Accepts a string (single origin / ``'*'``) or a list.
        Default: ``'*'``
    MONGODB_URI: MongoDB connection URI (delegated to ``shared.database.mongodb``).
    MONGODB_DATABASE: Default MongoDB database name (delegated to shared module).
    REDIS_URL: Redis connection URL (delegated to ``shared.database.redis_client``).

Example::

    from api_gateway.extensions import init_extensions, jwt, get_db, get_redis

    def create_app() -> Flask:
        app = Flask(__name__)
        app.config.from_object("api_gateway.config.ProductionConfig")
        init_extensions(app)
        return app
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from flask import Flask, current_app, jsonify
from flask_cors import CORS
from flask_jwt_extended import JWTManager
from prometheus_client import CollectorRegistry

from shared.database.mongodb import get_mongo_db, init_mongodb


if TYPE_CHECKING:
    import redis
    from pymongo import MongoClient
    from pymongo.database import Database
from shared.database.redis_client import get_redis_client, init_redis
from shared.logging.structured_logger import get_logger


# ---------------------------------------------------------------------------
# Module-level logger — pre-configured structlog BoundLogger for structured
# JSON output with correlation ID propagation and service context injection.
# ---------------------------------------------------------------------------
logger = get_logger(__name__)

# ---------------------------------------------------------------------------
# Module-level extension singletons — instantiated without app binding.
# These are imported by other modules in the api_gateway package
# (routes, middleware, services) and become functional after
# ``init_extensions(app)`` is called from the application factory.
# ---------------------------------------------------------------------------
jwt: JWTManager = JWTManager()
"""Flask-JWT-Extended manager.  Provides ``@jwt_required()`` decorator and
token lifecycle callbacks once initialised with a Flask app."""

cors: CORS = CORS()
"""Flask-CORS extension.  Handles ``Access-Control-*`` headers for the
``/api/*`` route namespace after initialisation."""

metrics_registry: CollectorRegistry = CollectorRegistry()
"""Custom Prometheus ``CollectorRegistry`` for API Gateway metrics, separate
from the default global registry to allow selective metric export on the
``/metrics`` monitoring endpoint."""

# ---------------------------------------------------------------------------
# Lazy-initialised connection references — populated during
# ``init_extensions()`` and available for module-level inspection.
# Actual request-time access should prefer ``get_db()`` / ``get_redis()``.
# ---------------------------------------------------------------------------
mongo_client: MongoClient | None = None
"""Singleton ``pymongo.MongoClient`` reference populated after MongoDB
initialisation completes.  ``None`` until ``init_extensions()`` is called
(or if the initial connection attempt failed)."""

redis_client: redis.Redis | None = None
"""Singleton ``redis.Redis`` client reference populated after Redis
initialisation completes.  ``None`` until ``init_extensions()`` is called
(or if the initial connection attempt failed)."""


# ===================================================================
# Public API
# ===================================================================


def init_extensions(app: Flask) -> None:
    """Initialise all Flask extensions for the API Gateway service.

    This function must be called **exactly once** from the application factory
    (``create_app()``) after the Flask app configuration has been loaded.  It
    performs the following steps in order:

    1. Bind the JWT manager to the app and register error callbacks.
    2. Configure CORS with allowed origins read from ``app.config``.
    3. Initialise MongoDB via the shared ``init_mongodb()`` utility, then
       store references in ``app.extensions`` for Flask-idiomatic access.
    4. Initialise Redis via the shared ``init_redis()`` utility, then store
       the client reference in ``app.extensions``.
    5. Register Prometheus metrics collectors.

    Connection errors for MongoDB and Redis are **logged but not raised** so
    that the service can start even when external dependencies are temporarily
    unavailable (the shared modules support lazy reconnection on the first
    real request).

    Args:
        app: The Flask application instance to bind extensions to.  Must have
            its configuration (``app.config``) fully loaded before this call.

    Example::

        app = Flask(__name__)
        app.config.from_object("api_gateway.config.DevelopmentConfig")
        init_extensions(app)
    """
    global mongo_client, redis_client  # noqa: PLW0603

    logger.info("extensions_init_started", app_name=app.name)

    # ------------------------------------------------------------------
    # 1. JWT — Flask-JWT-Extended with Auth0 RS256 token verification
    # ------------------------------------------------------------------
    jwt.init_app(app)
    _configure_jwt_error_handlers()
    logger.info("jwt_extension_initialized")

    # ------------------------------------------------------------------
    # 2. CORS — Cross-origin support for the React Web Console
    # ------------------------------------------------------------------
    cors_origins = app.config.get("CORS_ORIGINS", "*")
    cors.init_app(
        app,
        resources={r"/api/*": {"origins": cors_origins}},
        supports_credentials=True,
    )
    logger.info("cors_extension_initialized", origins=str(cors_origins))

    # ------------------------------------------------------------------
    # 3. MongoDB — Connection via shared singleton with pooling
    # ------------------------------------------------------------------
    try:
        init_mongodb(app)

        # Retrieve database handle and store references for Flask access.
        db = get_mongo_db()
        mongo_client = db.client
        app.extensions["mongodb"] = mongo_client
        app.extensions["mongodb_db"] = db

        logger.info(
            "mongodb_extension_initialized",
            database=db.name,
        )
    except Exception as exc:
        # Log but do not raise — the service may still start and the
        # shared MongoDB module will retry on the first actual request.
        logger.error(
            "mongodb_extension_initialization_failed",
            error=str(exc),
            error_type=type(exc).__name__,
        )

    # ------------------------------------------------------------------
    # 4. Redis — Connection via shared singleton with pooling
    # ------------------------------------------------------------------
    try:
        init_redis(app)

        redis_client = get_redis_client()
        app.extensions["redis"] = redis_client

        logger.info("redis_extension_initialized")
    except Exception as exc:
        # Log but do not raise — the service may still start and the
        # shared Redis module will retry on the first actual request.
        logger.error(
            "redis_extension_initialization_failed",
            error=str(exc),
            error_type=type(exc).__name__,
        )

    # ------------------------------------------------------------------
    # 5. Prometheus — Custom collector registry for API Gateway metrics
    # ------------------------------------------------------------------
    _setup_prometheus_metrics(app)

    logger.info("extensions_init_completed", app_name=app.name)


def get_db() -> Database:
    """Return the default MongoDB ``Database`` instance.

    When called inside a Flask request or application context the database
    handle is retrieved from ``current_app.extensions['mongodb_db']`` — this
    is the reference stored during :func:`init_extensions` and avoids an
    extra singleton lookup on every call.

    If no Flask application context is active (e.g. inside a CLI command,
    background worker, or test), the function falls back to the shared
    ``get_mongo_db()`` utility which returns the ``Database`` from the
    process-wide singleton ``MongoClient``.

    Returns:
        Database: A PyMongo ``Database`` object pointing to the default
            database (name read from ``MONGODB_DATABASE`` env var, defaulting
            to ``'synthetic_erp'``).

    Raises:
        pymongo.errors.ConnectionFailure: If the MongoDB connection cannot
            be established (the shared module retries internally but will
            eventually surface persistent failures).

    Example::

        from api_gateway.extensions import get_db

        @generation_bp.route("/api/v1/generation/jobs", methods=["GET"])
        def list_jobs():
            db = get_db()
            jobs = list(db["generation_profiles"].find({"status": "completed"}))
            return jsonify(jobs)
    """
    try:
        # Primary path: retrieve from Flask app context (fast, no singleton check).
        db: Database = current_app.extensions["mongodb_db"]
        return db
    except (RuntimeError, KeyError):
        # Fallback: no active Flask context or extension not registered yet.
        # This branch supports CLI utilities, background workers, and tests
        # that operate outside a Flask request lifecycle.
        return get_mongo_db()


def get_redis() -> redis.Redis:
    """Return the shared Redis client instance.

    When called inside a Flask request or application context the client is
    retrieved from ``current_app.extensions['redis']`` — this is the
    reference stored during :func:`init_extensions` and avoids an extra
    singleton lookup on every call.

    If no Flask application context is active (e.g. inside a CLI command,
    background worker, or test), the function falls back to the shared
    ``get_redis_client()`` utility which returns the process-wide singleton
    ``redis.Redis`` client.

    Returns:
        redis.Redis: A connected Redis client backed by a connection pool.

    Raises:
        redis.exceptions.ConnectionError: If the Redis connection cannot be
            established after pool creation.

    Example::

        from api_gateway.extensions import get_redis

        @generation_bp.route("/api/v1/generation/jobs/<job_id>/progress")
        def job_progress(job_id: str):
            client = get_redis()
            progress = client.get(f"job:progress:{job_id}")
            return jsonify({"progress": progress})
    """
    try:
        # Primary path: retrieve from Flask app context (fast, no singleton check).
        redis_client: redis.Redis = current_app.extensions["redis"]
        return redis_client
    except (RuntimeError, KeyError):
        # Fallback: no active Flask context or extension not registered yet.
        return get_redis_client()


# ===================================================================
# Private helpers
# ===================================================================


def _configure_jwt_error_handlers() -> None:
    """Register JWT token lifecycle error callbacks.

    Each callback returns a structured JSON error response with a consistent
    schema (``error``, ``message``, ``status_code``) and a ``401 Unauthorized``
    HTTP status code.  All callback invocations are logged at ``warning`` level
    for security audit trail purposes.

    Callbacks registered:
        - ``expired_token_loader`` — token TTL exceeded.
        - ``invalid_token_loader`` — malformed or tampered token.
        - ``unauthorized_loader`` — no token provided in request.
        - ``revoked_token_loader`` — token explicitly revoked.
    """

    @jwt.expired_token_loader
    def _handle_expired_token(
        jwt_header: dict,
        jwt_payload: dict,
    ) -> tuple:
        """Handle requests bearing an expired JWT.

        Args:
            jwt_header: Decoded JWT header containing algorithm and type.
            jwt_payload: Decoded JWT payload containing claims (sub, exp, etc.).

        Returns:
            Tuple of (JSON response body, 401 status code).
        """
        subject = jwt_payload.get("sub", "unknown")
        logger.warning(
            "jwt_token_expired",
            subject=subject,
            token_type=jwt_payload.get("type", "access"),
        )
        return (
            jsonify(
                {
                    "error": "token_expired",
                    "message": "The access token has expired. Please obtain a new token.",
                    "status_code": 401,
                }
            ),
            401,
        )

    @jwt.invalid_token_loader
    def _handle_invalid_token(error_string: str) -> tuple:
        """Handle requests bearing a malformed or tampered JWT.

        Args:
            error_string: Description of the validation failure from
                Flask-JWT-Extended.

        Returns:
            Tuple of (JSON response body, 401 status code).
        """
        logger.warning(
            "jwt_token_invalid",
            reason=error_string,
        )
        return (
            jsonify(
                {
                    "error": "invalid_token",
                    "message": "The provided token is invalid or malformed.",
                    "status_code": 401,
                }
            ),
            401,
        )

    @jwt.unauthorized_loader
    def _handle_missing_token(error_string: str) -> tuple:
        """Handle requests without an Authorization header.

        Args:
            error_string: Description of the missing-token condition from
                Flask-JWT-Extended.

        Returns:
            Tuple of (JSON response body, 401 status code).
        """
        logger.warning(
            "jwt_token_missing",
            reason=error_string,
        )
        return (
            jsonify(
                {
                    "error": "authorization_required",
                    "message": "An authorization token is required to access this resource.",
                    "status_code": 401,
                }
            ),
            401,
        )

    @jwt.revoked_token_loader
    def _handle_revoked_token(
        jwt_header: dict,
        jwt_payload: dict,
    ) -> tuple:
        """Handle requests bearing a revoked (blocklisted) JWT.

        Args:
            jwt_header: Decoded JWT header.
            jwt_payload: Decoded JWT payload containing claims.

        Returns:
            Tuple of (JSON response body, 401 status code).
        """
        subject = jwt_payload.get("sub", "unknown")
        logger.warning(
            "jwt_token_revoked",
            subject=subject,
            jti=jwt_payload.get("jti", "unknown"),
        )
        return (
            jsonify(
                {
                    "error": "token_revoked",
                    "message": "The token has been revoked and can no longer be used.",
                    "status_code": 401,
                }
            ),
            401,
        )


def _setup_prometheus_metrics(app: Flask) -> None:
    """Register the custom Prometheus ``CollectorRegistry`` in the Flask app.

    Stores the module-level ``metrics_registry`` in ``app.extensions`` so that
    the monitoring route (``/metrics``) can retrieve and expose only API
    Gateway-specific metrics rather than the full default global registry.

    Args:
        app: The Flask application instance.
    """
    app.extensions["metrics_registry"] = metrics_registry
    logger.info("prometheus_metrics_registry_initialized")
