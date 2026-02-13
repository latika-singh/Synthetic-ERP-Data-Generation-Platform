"""Flask Application Factory for the Quality Service.

Implements the ``create_app()`` Application Factory pattern for the Quality
Service — the microservice responsible for data quality validation using the
weighted composite scoring model within the Synthetic ERP Data Generation
Platform.

The Quality Service validates generated synthetic data against three criteria:

- **Statistical fidelity** (40% weight) — distribution, moment, and range
  validation using SciPy and Great Expectations.
- **Business rules compliance** (30% weight) — ERP-specific format, cross-field,
  domain, and temporal rule enforcement.
- **Referential integrity** (30% weight) — foreign key validity, orphan
  detection, and cascade-chain verification.

The factory:

1. Resolves environment-specific configuration via :data:`config_map`.
2. Configures structured JSON logging (structlog) and OpenTelemetry distributed
   tracing **before** any other initialisation so that startup events are
   captured.
3. Initialises Flask extensions — CORS, PyMongo (MongoDB 7.0), Redis 7.x.
4. Registers shared middleware: health/readiness probes and Prometheus metrics.
5. Registers three quality-specific route blueprints:

   - ``/api/v1/quality/validate`` — Run quality validation on generated data.
   - ``/api/v1/quality/score``    — Retrieve quality scores.
   - ``/api/v1/quality/reports``  — Quality report management and retrieval.

6. Installs global error handlers producing structured JSON error responses.
7. Attaches ``before_request`` / ``after_request`` hooks that generate and
   propagate correlation IDs and log request lifecycle events with duration.

Usage::

    # Development — run directly
    from quality_service.app import create_app

    app = create_app("development")
    app.run(host="0.0.0.0", port=5003)

    # Production — via Gunicorn (wsgi.py)
    app = create_app()
"""

from __future__ import annotations

import os
import time
import uuid
from typing import Any

from flask import Blueprint, Flask, Response, g, jsonify, request
from flask_cors import CORS

from quality_service.config import config_map
from shared.database.mongodb import init_mongodb
from shared.database.redis_client import init_redis
from shared.logging.structured_logger import configure_logging, get_logger
from shared.middleware.health_check import health_blueprint
from shared.observability.metrics import setup_metrics
from shared.observability.tracing import init_tracing


# ---------------------------------------------------------------------------
# Quality Service route blueprints
# ---------------------------------------------------------------------------
# These blueprints define the three core REST endpoint groups exposed by the
# Quality Service.  They are registered by ``create_app()`` under the
# ``/api/v1/quality`` URL prefix.
# ---------------------------------------------------------------------------

validate_blueprint: Blueprint = Blueprint("validate", __name__)
"""Blueprint for quality validation endpoints."""

score_blueprint: Blueprint = Blueprint("score", __name__)
"""Blueprint for quality scoring endpoints."""

reports_blueprint: Blueprint = Blueprint("reports", __name__)
"""Blueprint for quality report endpoints."""


# ============================================================================
# Validate Blueprint — POST /api/v1/quality/validate
# ============================================================================


@validate_blueprint.route("/validate", methods=["POST"])
def validate_data() -> tuple[Response, int]:
    """Run quality validation on generated synthetic data.

    Accepts a JSON payload describing the generated data and the source
    statistical profile, delegates to the :class:`QualityScorer` which
    executes all three validators (statistical, business rules, referential
    integrity), and returns the composite quality score with per-validator
    breakdowns.

    Request JSON:
        job_id (str): Unique identifier for the generation job.
        tenant_id (str): Tenant namespace for multi-tenant isolation.
        data (list[dict], optional): Sample records to validate.
        profile (dict, optional): Statistical profile metadata.
        metadata (dict, optional): Additional context (generation method,
            record counts, schema version).

    Returns:
        tuple: A ``(response, status_code)`` tuple.
            - 200: Validation completed successfully with results.
            - 400: Invalid request payload.
            - 500: Internal validation error.

    Response JSON::

        {
            "status": "completed",
            "job_id": "abc-123",
            "tenant_id": "tenant-1",
            "composite_score": 0.962,
            "passed": true,
            "threshold": 0.95,
            "validation_results": [...],
            "execution_time_ms": 1245.3
        }
    """
    logger = get_logger(__name__)
    start = time.time()

    body: dict[str, Any] | None = request.get_json(silent=True)
    if body is None:
        return jsonify({
            "error": "Bad Request",
            "message": "Request body must be valid JSON.",
        }), 400

    job_id: str = body.get("job_id", "")
    tenant_id: str = body.get("tenant_id", "")

    if not job_id or not tenant_id:
        return jsonify({
            "error": "Bad Request",
            "message": "Both 'job_id' and 'tenant_id' are required.",
        }), 400

    logger.info(
        "quality_validation_initiated",
        job_id=job_id,
        tenant_id=tenant_id,
    )

    try:
        import pandas as pd  # noqa: PLC0415

        from quality_service.scoring import QualityScorer  # noqa: PLC0415

        profile: dict[str, Any] = body.get("profile", {})
        metadata: dict[str, Any] = body.get("metadata", {})
        data_records: list[dict[str, Any]] = body.get("data", [])

        # Convert raw records to a DataFrame for the scoring pipeline.
        generated_data = pd.DataFrame(data_records) if data_records else pd.DataFrame()

        scorer = QualityScorer()
        scoring_result = scorer.score(
            job_id=job_id,
            tenant_id=tenant_id,
            generated_data=generated_data,
            profile=profile,
            metadata=metadata,
        )

        elapsed_ms = round((time.time() - start) * 1000, 2)

        logger.info(
            "quality_validation_completed",
            job_id=job_id,
            tenant_id=tenant_id,
            composite_score=scoring_result.composite_score,
            passed=scoring_result.passed,
            elapsed_ms=elapsed_ms,
        )

        return jsonify({
            "status": "completed",
            "job_id": job_id,
            "tenant_id": tenant_id,
            "composite_score": round(scoring_result.composite_score, 4),
            "passed": scoring_result.passed,
            "threshold": scoring_result.threshold,
            "validation_results": scoring_result.validation_results,
            "weights": scoring_result.weights,
            "execution_time_ms": elapsed_ms,
        }), 200

    except Exception:
        logger.error(
            "quality_validation_failed",
            job_id=job_id,
            tenant_id=tenant_id,
            exc_info=True,
        )
        return jsonify({
            "error": "Internal Server Error",
            "message": "Quality validation failed due to an internal error.",
            "job_id": job_id,
        }), 500


# ============================================================================
# Score Blueprint — GET /api/v1/quality/score
# ============================================================================


@score_blueprint.route("/score/<job_id>", methods=["GET"])
def get_score(job_id: str) -> tuple[Response, int]:
    """Retrieve the quality score for a specific generation job.

    Attempts to serve from Redis cache first, falling back to MongoDB
    persistence.

    Path Parameters:
        job_id (str): The generation job identifier.

    Query Parameters:
        tenant_id (str, required): Tenant namespace for access control.

    Returns:
        tuple: A ``(response, status_code)`` tuple.
            - 200: Score retrieved successfully.
            - 400: Missing required tenant_id parameter.
            - 404: Score not found for the given job_id/tenant_id.
            - 500: Internal retrieval error.
    """
    logger = get_logger(__name__)

    tenant_id: str = request.args.get("tenant_id", "")
    if not tenant_id:
        return jsonify({
            "error": "Bad Request",
            "message": "Query parameter 'tenant_id' is required.",
        }), 400

    logger.info(
        "quality_score_retrieval_initiated",
        job_id=job_id,
        tenant_id=tenant_id,
    )

    try:
        from quality_service.scoring import QualityScorer  # noqa: PLC0415

        scorer = QualityScorer()

        # Try cache first, then persistent store.
        result = scorer.get_cached_score(job_id=job_id, tenant_id=tenant_id)
        source = "cache"

        if result is None:
            result = scorer.get_stored_score(job_id=job_id, tenant_id=tenant_id)
            source = "database"

        if result is None:
            return jsonify({
                "error": "Not Found",
                "message": f"No quality score found for job '{job_id}'.",
            }), 404

        logger.info(
            "quality_score_retrieved",
            job_id=job_id,
            tenant_id=tenant_id,
            source=source,
            composite_score=result.composite_score,
        )

        return jsonify({
            "status": "success",
            "source": source,
            "job_id": result.job_id,
            "tenant_id": result.tenant_id,
            "composite_score": round(result.composite_score, 4),
            "passed": result.passed,
            "threshold": result.threshold,
            "validation_results": result.validation_results,
            "weights": result.weights,
            "scored_at": result.scored_at,
            "execution_time_ms": result.execution_time_ms,
        }), 200

    except Exception:
        logger.error(
            "quality_score_retrieval_failed",
            job_id=job_id,
            tenant_id=tenant_id,
            exc_info=True,
        )
        return jsonify({
            "error": "Internal Server Error",
            "message": "Quality score retrieval failed due to an internal error.",
        }), 500


@score_blueprint.route("/score/history", methods=["GET"])
def get_score_history() -> tuple[Response, int]:
    """Retrieve quality score history for a tenant.

    Query Parameters:
        tenant_id (str, required): Tenant namespace.
        limit (int, optional): Max results (default 50, max 200).

    Returns:
        tuple: A ``(response, status_code)`` tuple.
            - 200: History retrieved successfully.
            - 400: Missing required tenant_id parameter.
            - 500: Internal retrieval error.
    """
    logger = get_logger(__name__)

    tenant_id: str = request.args.get("tenant_id", "")
    if not tenant_id:
        return jsonify({
            "error": "Bad Request",
            "message": "Query parameter 'tenant_id' is required.",
        }), 400

    limit: int = min(200, max(1, int(request.args.get("limit", "50"))))

    try:
        from quality_service.scoring import QualityScorer  # noqa: PLC0415

        scorer = QualityScorer()
        results = scorer.get_score_history(tenant_id=tenant_id, limit=limit)

        history = []
        for r in results:
            try:
                history.append({
                    "job_id": r.job_id,
                    "composite_score": round(r.composite_score, 4),
                    "passed": r.passed,
                    "scored_at": r.scored_at,
                })
            except Exception:
                logger.debug(
                    "score_history_item_serialization_skipped",
                    error_type="serialization",
                )

        return jsonify({
            "status": "success",
            "tenant_id": tenant_id,
            "history": history,
            "count": len(history),
        }), 200

    except Exception:
        logger.error(
            "quality_score_history_failed",
            tenant_id=tenant_id,
            exc_info=True,
        )
        return jsonify({
            "error": "Internal Server Error",
            "message": "Quality score history retrieval failed.",
        }), 500


# ============================================================================
# Reports Blueprint — /api/v1/quality/reports
# ============================================================================


@reports_blueprint.route("/reports/<report_id>", methods=["GET"])
def get_report(report_id: str) -> tuple[Response, int]:
    """Retrieve a specific quality report by ID.

    Path Parameters:
        report_id (str): The unique report identifier.

    Query Parameters:
        tenant_id (str, required): Tenant namespace for access control.

    Returns:
        tuple: A ``(response, status_code)`` tuple.
            - 200: Report retrieved successfully.
            - 400: Missing required tenant_id parameter.
            - 404: Report not found.
            - 500: Internal retrieval error.
    """
    logger = get_logger(__name__)

    tenant_id: str = request.args.get("tenant_id", "")
    if not tenant_id:
        return jsonify({
            "error": "Bad Request",
            "message": "Query parameter 'tenant_id' is required.",
        }), 400

    try:
        from quality_service.scoring import ReportGenerator  # noqa: PLC0415

        generator = ReportGenerator()
        report = generator.get_report(report_id=report_id, tenant_id=tenant_id)

        if report is None:
            return jsonify({
                "error": "Not Found",
                "message": f"No quality report found with ID '{report_id}'.",
            }), 404

        return jsonify({
            "status": "success",
            "report": report.model_dump(),
        }), 200

    except Exception:
        logger.error(
            "quality_report_retrieval_failed",
            report_id=report_id,
            tenant_id=tenant_id,
            exc_info=True,
        )
        return jsonify({
            "error": "Internal Server Error",
            "message": "Quality report retrieval failed.",
        }), 500


@reports_blueprint.route("/reports/job/<job_id>", methods=["GET"])
def get_reports_by_job(job_id: str) -> tuple[Response, int]:
    """Retrieve all quality reports for a specific generation job.

    Path Parameters:
        job_id (str): The generation job identifier.

    Query Parameters:
        tenant_id (str, required): Tenant namespace for access control.

    Returns:
        tuple: A ``(response, status_code)`` tuple.
            - 200: Reports retrieved successfully.
            - 400: Missing required tenant_id parameter.
            - 500: Internal retrieval error.
    """
    logger = get_logger(__name__)

    tenant_id: str = request.args.get("tenant_id", "")
    if not tenant_id:
        return jsonify({
            "error": "Bad Request",
            "message": "Query parameter 'tenant_id' is required.",
        }), 400

    try:
        from quality_service.scoring import ReportGenerator  # noqa: PLC0415

        generator = ReportGenerator()
        reports = generator.get_reports_by_job(job_id=job_id, tenant_id=tenant_id)

        reports_data = []
        for report in reports:
            try:
                reports_data.append(report.model_dump())
            except Exception:
                logger.debug(
                    "report_serialization_skipped",
                    error_type="serialization",
                )

        return jsonify({
            "status": "success",
            "job_id": job_id,
            "tenant_id": tenant_id,
            "reports": reports_data,
            "count": len(reports_data),
        }), 200

    except Exception:
        logger.error(
            "quality_reports_by_job_failed",
            job_id=job_id,
            tenant_id=tenant_id,
            exc_info=True,
        )
        return jsonify({
            "error": "Internal Server Error",
            "message": "Quality reports retrieval failed.",
        }), 500


@reports_blueprint.route("/reports/<report_id>", methods=["DELETE"])
def delete_report(report_id: str) -> tuple[Response, int]:
    """Delete a quality report (admin operation).

    Path Parameters:
        report_id (str): The unique report identifier.

    Query Parameters:
        tenant_id (str, required): Tenant namespace for access control.

    Returns:
        tuple: A ``(response, status_code)`` tuple.
            - 200: Report deleted successfully.
            - 400: Missing required tenant_id parameter.
            - 404: Report not found.
            - 500: Internal deletion error.
    """
    logger = get_logger(__name__)

    tenant_id: str = request.args.get("tenant_id", "")
    if not tenant_id:
        return jsonify({
            "error": "Bad Request",
            "message": "Query parameter 'tenant_id' is required.",
        }), 400

    try:
        from quality_service.scoring import ReportGenerator  # noqa: PLC0415

        generator = ReportGenerator()
        deleted = generator.delete_report(report_id=report_id, tenant_id=tenant_id)

        if not deleted:
            return jsonify({
                "error": "Not Found",
                "message": f"No quality report found with ID '{report_id}'.",
            }), 404

        logger.info(
            "quality_report_deleted_via_api",
            report_id=report_id,
            tenant_id=tenant_id,
        )

        return jsonify({
            "status": "success",
            "message": f"Report '{report_id}' deleted successfully.",
        }), 200

    except Exception:
        logger.error(
            "quality_report_deletion_failed",
            report_id=report_id,
            tenant_id=tenant_id,
            exc_info=True,
        )
        return jsonify({
            "error": "Internal Server Error",
            "message": "Quality report deletion failed.",
        }), 500


# ============================================================================
# Application Factory
# ============================================================================


def create_app(config_name: str | None = None) -> Flask:
    """Create and configure the Quality Service Flask application.

    Implements the Flask Application Factory pattern, producing a fully
    configured application instance suitable for both development (direct
    ``app.run()``) and production (Gunicorn WSGI) deployments.

    The factory performs the following steps in order:

    1. **Resolve configuration** — reads ``FLASK_ENV`` environment variable
       when *config_name* is not provided (12-factor app compliance).
    2. **Configure structured logging** — initialises structlog with JSON
       output, correlation ID propagation, and tenant context injection.
    3. **Create Flask instance** — instantiates the Flask application and
       loads the resolved configuration via ``app.config.from_object()``.
    4. **Initialise extensions** — CORS, MongoDB, Redis, OpenTelemetry
       tracing, and Prometheus metrics.
    5. **Register blueprints** — shared health/readiness probes and three
       quality-specific route blueprints (validate, score, reports).
    6. **Register error handlers** — global JSON error responses for HTTP
       400, 404, 422, 500, and unhandled exceptions.
    7. **Attach request hooks** — ``before_request`` (correlation ID,
       start time) and ``after_request`` (duration logging).

    Args:
        config_name: Name of the configuration environment to load.
            Must be one of ``'development'``, ``'testing'``, or
            ``'production'``.  When ``None``, the ``FLASK_ENV``
            environment variable is used with a fallback to
            ``'development'``.

    Returns:
        Flask: A fully configured Flask application instance.

    Example::

        # Explicit config
        app = create_app("testing")

        # Environment-driven config (reads FLASK_ENV)
        app = create_app()
    """
    # ------------------------------------------------------------------
    # 1. Resolve configuration name from environment if not provided
    # ------------------------------------------------------------------
    if config_name is None:
        config_name = os.environ.get("FLASK_ENV", "development")

    # ------------------------------------------------------------------
    # 2. Configure structured logging (must happen before any logging)
    # ------------------------------------------------------------------
    configure_logging(service_name="quality-service")
    logger = get_logger(__name__)
    logger.info("Creating Quality Service application", config=config_name)

    # ------------------------------------------------------------------
    # 3. Create Flask application and load configuration
    # ------------------------------------------------------------------
    app = Flask(__name__)

    config_cls = config_map.get(config_name)
    if config_cls is None:
        logger.warning(
            "unknown_config_name_fallback",
            requested=config_name,
            fallback="development",
        )
        config_cls = config_map["development"]

    app.config.from_object(config_cls())

    # ------------------------------------------------------------------
    # 4. Initialise Flask extensions
    # ------------------------------------------------------------------
    _init_extensions(app)

    # ------------------------------------------------------------------
    # 5. Register blueprints
    # ------------------------------------------------------------------
    _register_blueprints(app)

    # ------------------------------------------------------------------
    # 6. Register error handlers
    # ------------------------------------------------------------------
    _register_error_handlers(app)

    # ------------------------------------------------------------------
    # 7. Attach request lifecycle hooks
    # ------------------------------------------------------------------
    _register_request_hooks(app)

    logger.info(
        "Quality Service application created successfully",
        config=config_name,
        service_name=app.config.get("SERVICE_NAME", "quality-service"),
    )
    return app


# ============================================================================
# Private helpers — extension, blueprint, error handler, and hook setup
# ============================================================================


def _init_extensions(app: Flask) -> None:
    """Initialise all Flask extensions required by the Quality Service.

    Extensions are initialised in dependency order: CORS first (stateless),
    then database clients (MongoDB, Redis), then observability (tracing,
    metrics).

    Args:
        app: The Flask application instance.
    """
    # CORS — allow cross-origin requests from the React 19.x Web Console
    CORS(app, resources={r"/api/*": {"origins": "*"}})

    # MongoDB 7.0 — quality reports, scoring results persistence
    init_mongodb(app)

    # Redis 7.x — quality score caching, session handling
    init_redis(app)

    # OpenTelemetry distributed tracing
    init_tracing(app, service_name="quality-service")

    # Prometheus metrics (registers /metrics blueprint + request hooks)
    setup_metrics(app, service_name="quality-service")


def _register_blueprints(app: Flask) -> None:
    """Register all route blueprints on the Flask application.

    Registers the shared health check blueprint at the application root
    and the three quality-specific blueprints under the
    ``/api/v1/quality`` URL prefix.

    Args:
        app: The Flask application instance.
    """
    # Shared health/readiness probes (/health, /ready)
    app.register_blueprint(health_blueprint)

    # Quality-specific routes under /api/v1/quality
    quality_prefix = "/api/v1/quality"
    app.register_blueprint(validate_blueprint, url_prefix=quality_prefix)
    app.register_blueprint(score_blueprint, url_prefix=quality_prefix)
    app.register_blueprint(reports_blueprint, url_prefix=quality_prefix)


def _register_error_handlers(app: Flask) -> None:
    """Register global JSON error handlers on the Flask application.

    Each handler returns a structured JSON response with ``error`` (short
    label), ``message`` (human-readable description), and ``status_code``
    fields.  The 500 and unhandled-exception handlers also emit structured
    log events for alerting and debugging.

    Args:
        app: The Flask application instance.
    """
    logger = get_logger(__name__)

    @app.errorhandler(400)
    def handle_bad_request(error: Exception) -> tuple[Response, int]:
        """Handle HTTP 400 Bad Request errors."""
        return jsonify({
            "error": "Bad Request",
            "message": str(error.description) if hasattr(error, "description") else str(error),
            "status_code": 400,
        }), 400

    @app.errorhandler(401)
    def handle_unauthorized(error: Exception) -> tuple[Response, int]:
        """Handle HTTP 401 Unauthorized errors."""
        return jsonify({
            "error": "Unauthorized",
            "message": "Authentication is required to access this resource.",
            "status_code": 401,
        }), 401

    @app.errorhandler(403)
    def handle_forbidden(error: Exception) -> tuple[Response, int]:
        """Handle HTTP 403 Forbidden errors."""
        return jsonify({
            "error": "Forbidden",
            "message": "You do not have permission to access this resource.",
            "status_code": 403,
        }), 403

    @app.errorhandler(404)
    def handle_not_found(error: Exception) -> tuple[Response, int]:
        """Handle HTTP 404 Not Found errors."""
        return jsonify({
            "error": "Not Found",
            "message": "The requested resource was not found.",
            "status_code": 404,
        }), 404

    @app.errorhandler(422)
    def handle_validation_error(error: Exception) -> tuple[Response, int]:
        """Handle HTTP 422 Unprocessable Entity (validation) errors."""
        return jsonify({
            "error": "Validation Error",
            "message": str(error.description) if hasattr(error, "description") else str(error),
            "status_code": 422,
        }), 422

    @app.errorhandler(500)
    def handle_internal_error(error: Exception) -> tuple[Response, int]:
        """Handle HTTP 500 Internal Server Error."""
        logger.error(
            "internal_server_error",
            error=str(error),
            path=request.path if request else "unknown",
            exc_info=True,
        )
        return jsonify({
            "error": "Internal Server Error",
            "message": "An unexpected error occurred. Please try again later.",
            "status_code": 500,
        }), 500

    @app.errorhandler(Exception)
    def handle_unhandled_exception(error: Exception) -> tuple[Response, int]:
        """Catch-all handler for unhandled exceptions.

        Logs the full exception traceback via structlog and returns a
        generic 500 JSON response to prevent leaking internal details
        to the client.

        If the exception is an :class:`~werkzeug.exceptions.HTTPException`
        (e.g. 405 Method Not Allowed), the original HTTP status code is
        preserved so that Flask/Werkzeug semantics are respected.
        """
        from werkzeug.exceptions import HTTPException  # noqa: PLC0415

        if isinstance(error, HTTPException):
            return jsonify({
                "error": error.name,
                "message": error.description,
                "status_code": error.code,
            }), error.code  # type: ignore[return-value]

        logger.error(
            "unhandled_exception",
            error_type=type(error).__name__,
            error_message=str(error),
            path=request.path if request else "unknown",
            method=request.method if request else "unknown",
            exc_info=True,
        )
        return jsonify({
            "error": "Internal Server Error",
            "message": "An unexpected error occurred. Please try again later.",
            "status_code": 500,
        }), 500


def _register_request_hooks(app: Flask) -> None:
    """Attach ``before_request`` and ``after_request`` lifecycle hooks.

    The ``before_request`` hook:
        - Generates or extracts a correlation ID (from ``X-Correlation-ID``
          or ``X-Request-ID`` headers, falling back to a UUID4).
        - Records the request start time in ``flask.g``.
        - Binds the correlation ID to the structlog context for automatic
          propagation into all subsequent log events within this request.

    The ``after_request`` hook:
        - Computes request duration in milliseconds.
        - Logs the completed request with method, path, status code,
          duration, and client IP.
        - Injects the ``X-Correlation-ID`` header into the response.

    Args:
        app: The Flask application instance.
    """
    logger = get_logger(__name__)

    @app.before_request
    def before_request_handler() -> None:
        """Capture request start time and bind correlation ID context."""
        # Record start time for duration calculation in after_request
        g.start_time = time.time()

        # Extract or generate correlation ID for distributed tracing
        correlation_id: str = (
            request.headers.get("X-Correlation-ID")
            or request.headers.get("X-Request-ID")
            or str(uuid.uuid4())
        )
        g.correlation_id = correlation_id

        # Bind correlation ID to structlog context for this request scope
        try:
            from shared.logging.structured_logger import bind_context  # noqa: PLC0415

            bind_context(
                correlation_id=correlation_id,
                http_method=request.method,
                http_path=request.path,
            )
        except ImportError:
            pass

        logger.info(
            "request_started",
            method=request.method,
            path=request.path,
            remote_addr=request.remote_addr,
            correlation_id=correlation_id,
        )

    @app.after_request
    def after_request_handler(response: Response) -> Response:
        """Log request completion and inject correlation ID header."""
        # Calculate request duration
        start_time: float = getattr(g, "start_time", time.time())
        duration_ms: float = round((time.time() - start_time) * 1000, 2)

        # Retrieve correlation ID set in before_request
        correlation_id: str = getattr(g, "correlation_id", "unknown")

        # Inject correlation ID into response headers for client tracing
        response.headers["X-Correlation-ID"] = correlation_id

        logger.info(
            "request_completed",
            method=request.method,
            path=request.path,
            status_code=response.status_code,
            duration_ms=duration_ms,
            correlation_id=correlation_id,
            remote_addr=request.remote_addr,
        )

        # Clear structlog context to prevent cross-request contamination
        try:
            from shared.logging.structured_logger import clear_context  # noqa: PLC0415

            clear_context()
        except ImportError:
            pass

        return response
