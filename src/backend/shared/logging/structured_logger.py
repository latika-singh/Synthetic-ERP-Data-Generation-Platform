"""Structured JSON logging implementation for the Synthetic ERP Data Generation Platform.

This module provides the core structured logging infrastructure used by all six backend
microservices (API Gateway, Generation Engine, Profiling Service, Quality Service,
Compliance Service, and Provisioning Service). It integrates structlog 24.x with
Python's standard ``logging`` module to produce machine-parseable JSON log output
compatible with ELK/Fluentd log aggregation pipelines.

Key Features:
    - Structured JSON log output via structlog processor chains
    - Automatic correlation ID propagation from OpenTelemetry trace context
    - Fallback correlation ID extraction from ``X-Correlation-ID`` / ``X-Request-ID``
      HTTP headers
    - Tenant context injection for multi-tenant log isolation
    - Request context binding (HTTP method, path, user_id, tenant_id)
    - Configurable log levels per environment (DEBUG/INFO/WARNING)
    - Performance-optimized processor chains with cached hostname and PID

Usage::

    # At service startup (in create_app()):
    from shared.logging.structured_logger import configure_logging
    configure_logging(service_name="api-gateway")

    # In any module:
    from shared.logging.structured_logger import get_logger
    logger = get_logger(__name__)
    logger.info("processing_request", user_id="abc123")

    # In request middleware:
    from shared.logging.structured_logger import bind_context, clear_context
    bind_context(request_id=req_id, tenant_id=tenant)
    # ... process request ...
    clear_context()
"""

from __future__ import annotations

import datetime  # noqa: F401 — imported for JSON formatting support
import json  # noqa: F401 — imported for JSON serialization support
import logging
import os
import socket
import sys
import uuid
from typing import Any

import structlog
from structlog.stdlib import BoundLogger  # noqa: F401 — re-exported for type usage


# ---------------------------------------------------------------------------
# Optional dependency imports with graceful fallbacks.
# OpenTelemetry and Flask may not be available in every execution context
# (e.g., CLI scripts, background workers, or unit tests).
# ---------------------------------------------------------------------------

try:
    from opentelemetry import trace

    _OTEL_AVAILABLE: bool = True
except ImportError:  # pragma: no cover
    trace = None  # type: ignore[assignment]
    _OTEL_AVAILABLE = False

try:
    from flask import g, has_request_context, request

    _FLASK_AVAILABLE: bool = True
except ImportError:  # pragma: no cover
    g = None  # type: ignore[assignment]
    has_request_context = None  # type: ignore[assignment]
    request = None  # type: ignore[assignment]
    _FLASK_AVAILABLE = False


# ---------------------------------------------------------------------------
# Module-level cached values for performance optimization.
# Computed once at module load time to avoid repeated system calls on every
# log event — socket.gethostname() in particular is expensive to invoke per
# log entry in high-throughput services.
# ---------------------------------------------------------------------------

_CACHED_HOSTNAME: str = socket.gethostname()
_CACHED_PID: int = os.getpid()

# Default log level mapping per Flask environment.
_ENV_LOG_LEVEL_MAP: dict[str, str] = {
    "development": "DEBUG",
    "staging": "INFO",
    "production": "WARNING",
}

# Internal flag tracking whether configure_logging() has been called.
_logging_configured: bool = False


# ---------------------------------------------------------------------------
# Custom Structlog Processors
# ---------------------------------------------------------------------------


def add_service_info(
    logger: Any,
    method_name: str,
    event_dict: dict[str, Any],
) -> dict[str, Any]:
    """Inject service identification metadata into every log event.

    Adds the service name (from the ``SERVICE_NAME`` environment variable),
    the server hostname, and the current process ID.  Hostname and PID are
    cached at module level so the values are read once per process rather
    than on every log call.

    Args:
        logger: The wrapped logger object (required by structlog processor
            protocol but unused here).
        method_name: The name of the log method called (e.g. ``"info"``).
        event_dict: The mutable event dictionary being assembled by the
            processor chain.

    Returns:
        The *event_dict* with ``service``, ``hostname``, and ``pid`` keys
        added.
    """
    event_dict["service"] = os.environ.get("SERVICE_NAME", "unknown-service")
    event_dict["hostname"] = _CACHED_HOSTNAME
    event_dict["pid"] = _CACHED_PID
    return event_dict


def add_correlation_id(
    logger: Any,
    method_name: str,
    event_dict: dict[str, Any],
) -> dict[str, Any]:
    """Extract or generate a correlation ID for distributed request tracing.

    Resolution order:

    1. **OpenTelemetry trace context** — ``trace_id`` and ``span_id`` from
       the currently active span.  If found, the hex-encoded ``trace_id``
       also serves as the ``correlation_id``.
    2. **HTTP header** — ``X-Correlation-ID`` or ``X-Request-ID`` extracted
       from the Flask request context.
    3. **Fallback** — A freshly generated UUID-4 string.

    Args:
        logger: The wrapped logger object.
        method_name: The name of the log method called.
        event_dict: The mutable event dictionary.

    Returns:
        The *event_dict* with ``correlation_id`` and, when available,
        ``trace_id`` and ``span_id`` keys.
    """
    correlation_id: str | None = None
    trace_id_hex: str | None = None
    span_id_hex: str | None = None

    # Strategy 1: OpenTelemetry trace context
    if _OTEL_AVAILABLE and trace is not None:
        try:
            span = trace.get_current_span()
            if span is not None:
                span_context = span.get_span_context()
                if span_context is not None and span_context.trace_id != 0:
                    trace_id_hex = format(span_context.trace_id, "032x")
                    span_id_hex = format(span_context.span_id, "016x")
                    correlation_id = trace_id_hex
        except Exception:  # noqa: S110
            # Graceful degradation — OTel context extraction is best-effort.
            # Logging the exception here would risk infinite recursion since
            # this code runs inside the logging processor chain itself.
            pass

    # Strategy 2: HTTP header via Flask request context
    if correlation_id is None and _FLASK_AVAILABLE:
        try:
            if has_request_context():
                correlation_id = (
                    request.headers.get("X-Correlation-ID")
                    or request.headers.get("X-Request-ID")
                )
        except Exception:  # noqa: S110
            # Graceful degradation when Flask context is unavailable.
            # Cannot log here — we are inside the log processor chain.
            pass

    # Strategy 3: UUID-4 fallback
    if correlation_id is None:
        correlation_id = str(uuid.uuid4())

    event_dict["correlation_id"] = correlation_id
    if trace_id_hex is not None:
        event_dict["trace_id"] = trace_id_hex
    if span_id_hex is not None:
        event_dict["span_id"] = span_id_hex

    return event_dict


def add_tenant_context(
    logger: Any,
    method_name: str,
    event_dict: dict[str, Any],
) -> dict[str, Any]:
    """Inject tenant context from Flask's ``g`` object for multi-tenant isolation.

    The tenant middleware (executed earlier in the request pipeline) is
    expected to set ``g.tenant_id``.  This processor copies that value into
    the log event so that every log entry produced during a request is
    automatically tagged with the tenant identifier.

    When no Flask request context is active (e.g. background tasks, CLI
    commands), the processor is a no-op to avoid raising errors.

    Args:
        logger: The wrapped logger object.
        method_name: The name of the log method called.
        event_dict: The mutable event dictionary.

    Returns:
        The *event_dict*, optionally enriched with a ``tenant_id`` key.
    """
    if _FLASK_AVAILABLE:
        try:
            if has_request_context():
                tenant_id = getattr(g, "tenant_id", None)
                if tenant_id is not None:
                    event_dict["tenant_id"] = tenant_id
        except Exception:  # noqa: S110
            # Graceful degradation outside Flask request context.
            # Cannot log here — we are inside the log processor chain.
            pass

    return event_dict


def add_request_context(
    logger: Any,
    method_name: str,
    event_dict: dict[str, Any],
) -> dict[str, Any]:
    """Inject HTTP request details into log events when a Flask request is active.

    Extracts the HTTP method, request path, remote address, and user-agent
    string from the current Flask request.  If the authentication middleware
    has set ``g.user_id``, that value is also included.

    When no Flask request context is active the processor is a no-op.

    Args:
        logger: The wrapped logger object.
        method_name: The name of the log method called.
        event_dict: The mutable event dictionary.

    Returns:
        The *event_dict*, optionally enriched with ``http_method``,
        ``path``, ``remote_addr``, ``user_agent``, and ``user_id`` keys.
    """
    if _FLASK_AVAILABLE:
        try:
            if has_request_context():
                event_dict["http_method"] = request.method
                event_dict["path"] = request.path
                event_dict["remote_addr"] = request.remote_addr
                event_dict["user_agent"] = str(
                    request.headers.get("User-Agent", "")
                )

                # user_id is set by the JWT / auth middleware on flask.g
                user_id = getattr(g, "user_id", None)
                if user_id is not None:
                    event_dict["user_id"] = user_id
        except Exception:  # noqa: S110
            # Graceful degradation outside Flask request context.
            # Cannot log here — we are inside the log processor chain.
            pass

    return event_dict


# ---------------------------------------------------------------------------
# Internal Helpers
# ---------------------------------------------------------------------------


def _resolve_log_level(log_level: str | None) -> int:
    """Determine the effective numeric log level.

    Resolution order:

    1. Explicit *log_level* parameter (if provided).
    2. ``LOG_LEVEL`` environment variable.
    3. ``FLASK_ENV``-based mapping: ``development`` → DEBUG,
       ``staging`` → INFO, ``production`` → WARNING.
    4. Final fallback: ``INFO``.

    Args:
        log_level: Optional explicit log level string (e.g. ``"DEBUG"``).

    Returns:
        The resolved Python :mod:`logging` level as an integer.
    """
    level_str: str | None = log_level

    if level_str is None:
        level_str = os.environ.get("LOG_LEVEL")

    if level_str is None:
        flask_env = os.environ.get("FLASK_ENV", "production")
        level_str = _ENV_LOG_LEVEL_MAP.get(flask_env, "INFO")

    level_str = level_str.upper()
    numeric_level = getattr(logging, level_str, None)
    if not isinstance(numeric_level, int):
        # Invalid level string — default to INFO.
        numeric_level = logging.INFO

    return numeric_level


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def configure_logging(
    service_name: str,
    log_level: str | None = None,
    json_output: bool = True,
) -> None:
    """Configure structured logging for a backend microservice.

    This function **must be called once** at service startup — typically
    inside the Flask Application Factory's ``create_app()`` function.  It
    wires together Python's standard :mod:`logging` module and *structlog*
    so that **all** log output (from both structlog loggers and standard-
    library loggers) is emitted in a consistent, structured format.

    The processor chain is ordered for performance — cheap context-variable
    merging first, expensive exception rendering last:

    1. Context variable merging
    2. Log level and logger name injection
    3. ISO 8601 timestamp
    4. Service info injection (cached values)
    5. Correlation ID extraction
    6. Tenant context injection
    7. Request context injection
    8. Stack info rendering
    9. Exception formatting
    10. Unicode decoding
    11. ``ProcessorFormatter.wrap_for_formatter`` (terminal processor)

    Args:
        service_name: Identifier for the microservice (e.g.
            ``"api-gateway"``, ``"generation-engine"``).  Also written to the
            ``SERVICE_NAME`` environment variable if not already set.
        log_level: Optional explicit log level string.  Falls back to
            ``LOG_LEVEL`` env var, then ``FLASK_ENV``-based defaults:
            development → DEBUG, staging → INFO, production → WARNING.
        json_output: When ``True`` (default) the final output is JSON —
            suitable for ELK / Fluentd ingestion.  Set to ``False`` for
            human-readable console output during local development.

    Raises:
        No exceptions are raised; the function configures logging in-place.

    Example::

        configure_logging(service_name="api-gateway", log_level="DEBUG")
        logger = get_logger(__name__)
        logger.info("service_started", port=8080)
    """
    global _logging_configured  # noqa: PLW0603

    # Ensure SERVICE_NAME is visible to the add_service_info processor.
    if "SERVICE_NAME" not in os.environ:
        os.environ["SERVICE_NAME"] = service_name

    effective_level: int = _resolve_log_level(log_level)

    # ------------------------------------------------------------------
    # Shared processor chain — applied to both structlog-originated and
    # standard-library-originated log entries.
    # ------------------------------------------------------------------
    shared_processors: list[Any] = [
        structlog.contextvars.merge_contextvars,
        structlog.stdlib.add_log_level,
        structlog.stdlib.add_logger_name,
        structlog.processors.TimeStamper(fmt="iso"),
        add_service_info,
        add_correlation_id,
        add_tenant_context,
        add_request_context,
        structlog.processors.StackInfoRenderer(),
        structlog.processors.format_exc_info,
        structlog.processors.UnicodeDecoder(),
    ]

    # ------------------------------------------------------------------
    # Formatter processor chain — responsible for final rendering inside
    # the ProcessorFormatter (after wrap_for_formatter unpacks the dict).
    # ------------------------------------------------------------------
    if json_output:
        formatter_processors: list[Any] = [
            structlog.stdlib.ProcessorFormatter.remove_processors_meta,
            structlog.processors.JSONRenderer(),
        ]
    else:
        formatter_processors = [
            structlog.stdlib.ProcessorFormatter.remove_processors_meta,
            structlog.dev.ConsoleRenderer(),
        ]

    # ------------------------------------------------------------------
    # Wire up Python's standard logging with the structlog formatter.
    # ------------------------------------------------------------------
    formatter = structlog.stdlib.ProcessorFormatter(
        processors=formatter_processors,
        foreign_pre_chain=shared_processors,
    )

    root_logger = logging.getLogger()
    # Remove pre-existing handlers to prevent duplicate log lines when
    # configure_logging() is called more than once (e.g. in tests).
    root_logger.handlers.clear()

    handler = logging.StreamHandler(sys.stdout)
    handler.setFormatter(formatter)
    root_logger.addHandler(handler)
    root_logger.setLevel(effective_level)

    # ------------------------------------------------------------------
    # Configure structlog itself.  The shared processor chain terminates
    # with wrap_for_formatter so that the ProcessorFormatter can unpack
    # the event dict on the logging side.
    # ------------------------------------------------------------------
    structlog.configure(
        processors=[
            *shared_processors,
            structlog.stdlib.ProcessorFormatter.wrap_for_formatter,
        ],
        context_class=dict,
        logger_factory=structlog.stdlib.LoggerFactory(),
        wrapper_class=structlog.stdlib.BoundLogger,
        cache_logger_on_first_use=True,
    )

    _logging_configured = True


def get_logger(name: str | None = None) -> structlog.stdlib.BoundLogger:
    """Return a pre-configured structlog ``BoundLogger`` instance.

    This is the **primary entry point** for obtaining a logger across all
    six backend microservices.  The returned logger produces structured
    output (JSON or console, depending on how :func:`configure_logging` was
    called) with automatic injection of correlation IDs, tenant context,
    and HTTP request details.

    Thread-safe and suitable for module-level initialisation::

        logger = get_logger(__name__)

    Args:
        name: Optional logger name.  When ``None``, structlog will
            determine the name automatically from the calling module.

    Returns:
        A :class:`structlog.stdlib.BoundLogger` instance ready for use.

    Example::

        logger = get_logger("api_gateway.routes.generation")
        logger.info("job_created", job_id="abc-123", records=10000)
        logger.error("job_failed", job_id="abc-123", exc_info=True)
    """
    if name is not None:
        return structlog.get_logger(name)  # type: ignore[no-any-return]
    return structlog.get_logger()  # type: ignore[no-any-return]


def bind_context(**kwargs: Any) -> None:
    """Bind key-value pairs to the structlog context variables.

    Values bound here are automatically merged into **every** subsequent
    log event within the same execution context (backed by
    :mod:`contextvars`).  Typically called at the beginning of request
    processing — for example in a ``before_request`` Flask hook — to
    inject request-scoped identifiers.

    Args:
        **kwargs: Arbitrary key-value pairs to bind.

    Example::

        bind_context(
            request_id="req-abc-123",
            tenant_id="tenant-42",
            user_id="user-7",
        )
        logger.info("processing_started")
        # Log output automatically includes request_id, tenant_id, user_id
    """
    structlog.contextvars.bind_contextvars(**kwargs)


def clear_context() -> None:
    """Clear all bound context variables from the structlog context.

    Resets the logging context to a clean state.  **Must** be called at
    the end of every request lifecycle (e.g. in a ``teardown_request``
    Flask hook or a ``finally`` block) to prevent context values from
    leaking into subsequent requests served by the same thread / async
    task.

    Example::

        bind_context(request_id="req-abc-123")
        try:
            # ... process request ...
            pass
        finally:
            clear_context()
    """
    structlog.contextvars.clear_contextvars()
