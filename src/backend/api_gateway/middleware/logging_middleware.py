"""Structured request/response logging middleware for the API Gateway.

This module implements comprehensive HTTP request/response lifecycle logging
with OpenTelemetry distributed tracing correlation for the Synthetic ERP Data
Generation Platform. It captures method, path, status code, response duration
(ms), user_id, tenant_id, request_id, and content_length for every HTTP
request using structlog for structured JSON output.

Key Features:
    - Correlation ID generation/extraction from ``X-Request-ID`` header for
      distributed tracing continuity across all six backend microservices
    - OpenTelemetry ``trace_id`` and ``span_id`` injection into the structlog
      context for seamless integration with Jaeger/Zipkin trace backends
    - High-resolution request duration measurement via :func:`time.perf_counter`
    - Sensitive header redaction (Authorization, Cookie, X-API-Key) to prevent
      credential leakage in log output
    - Health probe endpoint exemption (``/health``, ``/ready``) from verbose
      logging to reduce noise in Kubernetes environments
    - Automatic response header injection (``X-Request-ID``, ``X-Response-Time``)
    - Log level escalation based on HTTP status code: INFO for success,
      WARNING for 4xx, ERROR for 5xx

Usage::

    from api_gateway.middleware.logging_middleware import register_logging_middleware

    def create_app():
        app = Flask(__name__)
        register_logging_middleware(app)
        return app

The middleware should be registered **first** in the middleware chain
(before auth, tenant, rate limiter, and error handlers) to ensure
correlation IDs are available for all subsequent middleware operations.
"""

from __future__ import annotations

import logging
import uuid
from time import perf_counter
from typing import Dict, Optional, Set

import structlog
from flask import Flask, Response, current_app, g, request

from shared.logging.structured_logger import bind_context, clear_context, get_logger

# ---------------------------------------------------------------------------
# Optional dependency imports with graceful fallbacks.
# OpenTelemetry may not be available in every deployment environment
# (e.g., local development without the observability stack, air-gapped
# environments where the package was not bundled, or unit test contexts).
# ---------------------------------------------------------------------------

try:
    from opentelemetry import trace
    from opentelemetry.trace import StatusCode

    _OTEL_AVAILABLE: bool = True
except ImportError:
    trace = None  # type: ignore[assignment]
    StatusCode = None  # type: ignore[assignment]
    _OTEL_AVAILABLE = False


# ---------------------------------------------------------------------------
# Module-level constants (exported)
# ---------------------------------------------------------------------------

REQUEST_ID_HEADER: str = "X-Request-ID"
"""HTTP header name for request correlation ID propagation across services.

All microservices in the platform read and forward this header to maintain
end-to-end distributed tracing continuity. When a request arrives without
this header, the middleware generates a fresh UUID4 value.
"""

SENSITIVE_HEADERS: Set[str] = {"authorization", "cookie", "x-api-key"}
"""Set of HTTP header names (lowercase) that must never appear in log output.

These headers may contain credentials (Bearer tokens, session cookies, API
keys) and are redacted to comply with SOC 2 Type II security requirements
and prevent credential leakage through log aggregation pipelines.
"""

LOG_EXEMPT_PATHS: Set[str] = {"/health", "/ready"}
"""Set of URL paths excluded from verbose request/response logging.

Kubernetes liveness and readiness probes generate high-frequency traffic
that would otherwise dominate log output without providing operational
value. These paths still receive ``X-Request-ID`` response headers.
"""

# ---------------------------------------------------------------------------
# Internal constants (not exported)
# ---------------------------------------------------------------------------

_MAX_USER_AGENT_LENGTH: int = 200
"""Maximum characters of User-Agent string included in log entries.

Truncating prevents oversized user-agent strings (common in automated
scanners and bots) from bloating structured log entries.
"""

_RESPONSE_TIME_HEADER: str = "X-Response-Time"
"""Response header containing the server-side processing duration."""

# ---------------------------------------------------------------------------
# Module-level logger instance
# ---------------------------------------------------------------------------

logger = get_logger(__name__)


# ---------------------------------------------------------------------------
# Internal Helper Functions
# ---------------------------------------------------------------------------


def _get_or_create_request_id() -> str:
    """Extract or generate a unique request correlation ID.

    Checks the incoming HTTP request for an existing ``X-Request-ID``
    header. If present and non-empty, the value is sanitized (trimmed
    and length-limited to 128 characters) and reused to maintain
    distributed tracing continuity across microservices. If absent, a
    new UUID4 is generated to ensure every request has a unique
    identifier.

    Returns:
        A string containing the request correlation ID — either
        extracted from the incoming header or a freshly generated UUID4.
    """
    existing_id: Optional[str] = request.headers.get(REQUEST_ID_HEADER)

    if existing_id is not None and existing_id.strip():
        # Sanitize: strip whitespace and limit length to prevent header
        # injection or excessively long values in log output.
        sanitized_id: str = existing_id.strip()[:128]
        return sanitized_id

    # No existing correlation ID — generate a new UUID4.
    return str(uuid.uuid4())


def _get_otel_context() -> Dict[str, str]:
    """Extract OpenTelemetry trace context from the current active span.

    Retrieves the currently active OpenTelemetry span and extracts the
    ``trace_id`` (formatted as a 32-character lowercase hex string) and
    ``span_id`` (formatted as a 16-character lowercase hex string) for
    injection into the structlog logging context. This enables log
    entries to be correlated with distributed traces in Jaeger, Zipkin,
    or other OpenTelemetry-compatible trace backends.

    Returns:
        A dictionary with ``trace_id`` and ``span_id`` keys. Values are
        empty strings if OpenTelemetry is not installed, not initialized,
        or no active span exists in the current execution context.
    """
    result: Dict[str, str] = {"trace_id": "", "span_id": ""}

    if not _OTEL_AVAILABLE or trace is None:
        return result

    try:
        span = trace.get_current_span()
        if span is None:
            return result

        span_context = span.get_span_context()
        if span_context is None or span_context.trace_id == 0:
            return result

        result["trace_id"] = format(span_context.trace_id, "032x")
        result["span_id"] = format(span_context.span_id, "016x")

    except Exception:  # noqa: S110, BLE001
        # Graceful degradation — OTel context extraction is best-effort.
        # We intentionally do not log here to avoid potential infinite
        # recursion if the logging system itself triggers this code path
        # during processor chain execution.
        pass

    return result


# ---------------------------------------------------------------------------
# Flask Request Lifecycle Hooks
# ---------------------------------------------------------------------------


def _before_request_logging() -> None:
    """Flask ``before_request`` hook for request lifecycle logging.

    Executes at the beginning of every HTTP request to:

    1. Record a high-resolution start timestamp for duration calculation.
    2. Generate or extract the ``X-Request-ID`` correlation identifier.
    3. Retrieve OpenTelemetry trace context (``trace_id``, ``span_id``).
    4. Bind all request-scoped metadata to the structlog context for
       automatic inclusion in every subsequent log event within this
       request lifecycle.
    5. Log the incoming request at ``INFO`` level (unless the path is
       in :data:`LOG_EXEMPT_PATHS`).

    Sensitive headers listed in :data:`SENSITIVE_HEADERS` are never
    included in log output. The ``User-Agent`` string is truncated to
    :data:`_MAX_USER_AGENT_LENGTH` characters to prevent log bloat.
    """
    # Step 1: Record high-resolution start time for duration measurement.
    g.request_start_time = perf_counter()

    # Step 2: Generate or extract the request correlation ID.
    g.request_id = _get_or_create_request_id()

    # Step 3: Retrieve OpenTelemetry trace context (trace_id, span_id).
    g.otel_context = _get_otel_context()

    # Step 4: Compute truncated user-agent string.
    user_agent_raw: str = ""
    try:
        user_agent_raw = request.user_agent.string or ""
    except (AttributeError, RuntimeError):
        # Graceful fallback for edge cases where user_agent is unavailable.
        user_agent_raw = request.headers.get("User-Agent", "")
    user_agent: str = user_agent_raw[:_MAX_USER_AGENT_LENGTH]

    # Step 5: Bind request-scoped context variables to structlog.
    # All subsequent log events within this request will automatically
    # include these fields without explicit passing.
    bind_context(
        request_id=g.request_id,
        trace_id=g.otel_context.get("trace_id", ""),
        span_id=g.otel_context.get("span_id", ""),
        method=request.method,
        path=request.path,
        remote_addr=request.remote_addr,
        user_agent=user_agent,
    )

    # Step 6: Log incoming request (skip for health probe endpoints).
    if request.path not in LOG_EXEMPT_PATHS:
        log_data: Dict[str, object] = {
            "method": request.method,
            "path": request.path,
            "query_string": request.query_string.decode("utf-8", errors="replace"),
        }

        # Include content_length when present (e.g., POST/PUT bodies).
        content_length: Optional[int] = request.content_length
        if content_length is not None:
            log_data["content_length"] = content_length

        logger.info("request_started", **log_data)


def _after_request_logging(response: Response) -> Response:
    """Flask ``after_request`` hook for response lifecycle logging.

    Executes after every HTTP request has been processed to:

    1. Calculate the request duration in milliseconds using the
       high-resolution timer recorded in ``_before_request_logging``.
    2. Inject ``X-Request-ID`` and ``X-Response-Time`` response headers
       for downstream consumers and monitoring tools.
    3. Log the completed request with full context including status code,
       duration, user/tenant identity, and content length.
    4. Set OpenTelemetry span status to ``ERROR`` for 5xx responses.

    Log level is escalated based on HTTP status code:

    - ``INFO`` for 1xx–3xx successful and redirect responses
    - ``WARNING`` for 4xx client error responses
    - ``ERROR`` for 5xx server error responses

    Args:
        response: The Flask :class:`~flask.Response` object to augment
            with tracing headers and log.

    Returns:
        The augmented :class:`~flask.Response` with ``X-Request-ID`` and
        ``X-Response-Time`` headers added.
    """
    # Calculate request duration in milliseconds with 2 decimal precision.
    start_time: float = getattr(g, "request_start_time", perf_counter())
    duration_ms: float = round((perf_counter() - start_time) * 1000, 2)

    # Retrieve request_id (may be absent if before_request was not executed,
    # e.g., when Flask internally handles a redirect before hooks run).
    request_id: str = getattr(g, "request_id", str(uuid.uuid4()))

    # Inject tracing and timing headers into the response.
    response.headers[REQUEST_ID_HEADER] = request_id
    response.headers[_RESPONSE_TIME_HEADER] = f"{duration_ms:.2f}ms"

    # Log completed request (skip for health probe endpoints).
    if request.path not in LOG_EXEMPT_PATHS:
        otel_ctx: Dict[str, str] = getattr(
            g, "otel_context", {"trace_id": "", "span_id": ""}
        )

        # Resolve user and tenant identity from Flask g (set by auth and
        # tenant middleware which execute after this middleware's before_request).
        user_id: str = getattr(g, "user_id", "anonymous")
        tenant_id: str = getattr(g, "tenant_id", "unknown")

        log_data: Dict[str, object] = {
            "method": request.method,
            "path": request.path,
            "status_code": response.status_code,
            "duration_ms": duration_ms,
            "user_id": user_id,
            "tenant_id": tenant_id,
            "content_length": response.content_length,
            "request_id": request_id,
            "trace_id": otel_ctx.get("trace_id", ""),
            "span_id": otel_ctx.get("span_id", ""),
        }

        status_code: int = response.status_code

        # Set OpenTelemetry span status to ERROR for server error responses
        # to enable automatic error detection in trace visualization tools.
        if _OTEL_AVAILABLE and trace is not None and StatusCode is not None:
            try:
                current_span = trace.get_current_span()
                if current_span is not None and status_code >= 500:
                    current_span.set_status(
                        StatusCode.ERROR,
                        f"HTTP {status_code}",
                    )
            except Exception:  # noqa: S110, BLE001
                # Best-effort — do not let OTel failures affect response flow.
                pass

        # Determine whether to include debug-level details based on the
        # application's debug configuration.
        is_debug: bool = current_app.config.get("DEBUG", False)
        if is_debug and status_code >= 400:
            log_data["response_headers"] = {
                k: v
                for k, v in response.headers
                if k.lower() not in SENSITIVE_HEADERS
            }

        # Escalate log level based on HTTP status code.
        if status_code >= 500:
            logger.error("request_completed", **log_data)
        elif status_code >= 400:
            logger.warning("request_completed", **log_data)
        else:
            logger.info("request_completed", **log_data)

    return response


def _teardown_request_logging(exception: Optional[BaseException]) -> None:
    """Flask ``teardown_request`` hook for cleanup after request completion.

    Executes at the very end of the request lifecycle (after the response
    has been sent to the client) to:

    1. Log any unhandled exception that occurred during request processing
       at ``ERROR`` level with the exception type, message, and available
       request context (request_id, method, path, duration_ms).
    2. Clear all structlog bound context variables to prevent context
       leakage between requests served by the same thread or async worker.

    This hook **always** clears context, even when no exception occurred,
    ensuring clean state for the next request.

    Args:
        exception: The unhandled exception that occurred during request
            processing, or ``None`` if the request completed normally.
    """
    if exception is not None:
        # Calculate duration if start time was recorded.
        duration_ms: Optional[float] = None
        start_time: Optional[float] = getattr(g, "request_start_time", None)
        if start_time is not None:
            duration_ms = round((perf_counter() - start_time) * 1000, 2)

        error_data: Dict[str, object] = {
            "exception_type": type(exception).__name__,
            "exception_message": str(exception),
            "request_id": getattr(g, "request_id", "unknown"),
            "method": getattr(request, "method", "unknown"),
            "path": getattr(request, "path", "unknown"),
        }
        if duration_ms is not None:
            error_data["duration_ms"] = duration_ms

        logger.error("request_exception", **error_data)

    # Always clear structlog context variables to prevent leakage between
    # requests. This is critical in threaded WSGI servers (Gunicorn with
    # sync workers) where the same thread serves multiple sequential requests.
    clear_context()


# ---------------------------------------------------------------------------
# Public Registration Function
# ---------------------------------------------------------------------------


def register_logging_middleware(app: Flask) -> None:
    """Register structured logging middleware on a Flask application.

    Configures structlog for structured JSON output (as a baseline
    configuration that may be enhanced or overridden by the shared
    :func:`~shared.logging.structured_logger.configure_logging` function),
    then registers three Flask hooks to capture the complete HTTP request
    lifecycle:

    1. ``before_request`` — Records start time, generates/extracts
       correlation ID, binds request context to structlog.
    2. ``after_request`` — Calculates duration, injects response headers,
       logs completed request with status-appropriate log level.
    3. ``teardown_request`` — Logs unhandled exceptions, clears structlog
       context to prevent cross-request leakage.

    This middleware should be registered **first** in the middleware chain
    (before auth, tenant, rate limiter, and error handlers) to ensure
    correlation IDs are established before any other middleware executes.

    The structlog configuration uses the ``LOG_LEVEL`` from the Flask
    application config, falling back to ``INFO`` if not specified. The
    configuration includes:

    - Context variable merging for request-scoped context propagation
    - Automatic log level injection
    - ISO 8601 timestamps
    - JSON rendering for structured log output

    Args:
        app: The Flask application instance to register the logging
            middleware hooks on.

    Example::

        from flask import Flask
        from api_gateway.middleware.logging_middleware import (
            register_logging_middleware,
        )

        app = Flask(__name__)
        app.config["LOG_LEVEL"] = "DEBUG"
        register_logging_middleware(app)
    """
    # Determine the effective log level from app configuration.
    log_level_str: str = app.config.get("LOG_LEVEL", "INFO")
    if isinstance(log_level_str, str):
        log_level_str = log_level_str.upper()
    else:
        log_level_str = "INFO"

    # Convert string level to logging module integer.
    log_level: int = getattr(logging, log_level_str, logging.INFO)
    if not isinstance(log_level, int):
        log_level = logging.INFO

    # Configure structlog with JSON renderer as a baseline configuration.
    # The shared configure_logging() function provides a more comprehensive
    # setup (with service info, correlation IDs, and stdlib integration) and
    # will override this if called after middleware registration. This
    # baseline ensures the middleware operates correctly even in isolation
    # (e.g., during unit tests or when the shared module is not initialized).
    try:
        structlog.configure(
            processors=[
                structlog.contextvars.merge_contextvars,
                structlog.processors.add_log_level,
                structlog.processors.TimeStamper(fmt="iso"),
                structlog.processors.JSONRenderer(),
            ],
            wrapper_class=structlog.make_filtering_bound_logger(log_level),
            context_class=dict,
            logger_factory=structlog.PrintLoggerFactory(),
            cache_logger_on_first_use=True,
        )
    except Exception:  # noqa: S110, BLE001
        # structlog configuration may fail if already fully configured with
        # incompatible settings. This is a non-critical initialization step;
        # the middleware hooks function correctly with any structlog config.
        pass

    # Register Flask lifecycle hooks in execution order.
    app.before_request(_before_request_logging)
    app.after_request(_after_request_logging)
    app.teardown_request(_teardown_request_logging)

    # Log successful middleware registration.
    logger.info(
        "logging_middleware_registered",
        log_level=log_level_str,
        exempt_paths=sorted(LOG_EXEMPT_PATHS),
        request_id_header=REQUEST_ID_HEADER,
        otel_available=_OTEL_AVAILABLE,
    )
