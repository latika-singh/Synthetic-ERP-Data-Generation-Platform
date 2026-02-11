"""OpenTelemetry distributed tracing setup for the Synthetic ERP Data Generation Platform.

This module provides the complete distributed tracing infrastructure shared by all six
backend microservices (API Gateway, Generation Engine, Profiling Service, Quality
Service, Compliance Service, and Provisioning Service).  It configures:

- **OTLP trace exporters** for sending spans to tracing backends such as Jaeger,
  Zipkin, or any OTLP-compatible collector.
- **BatchSpanProcessor** for efficient span batching and asynchronous export with
  tuned queue sizes for production throughput.
- **W3C TraceContext propagation** for correlating traces across inter-service
  HTTP calls via the ``traceparent`` header.
- **Correlation ID helpers** (``get_current_trace_id``, ``get_current_span_id``)
  used by ``structured_logger.py`` for unified log-trace correlation.
- **Automatic Flask request tracing** via ``FlaskInstrumentor``, which instruments
  every incoming HTTP request as a span (excluding health/ready/metrics endpoints).

All configuration follows the 12-factor app methodology — values are read from
environment variables with sensible defaults.

Usage::

    # In a service's create_app():
    from shared.observability.tracing import init_tracing

    def create_app() -> Flask:
        app = Flask(__name__)
        provider = init_tracing(app, service_name="api-gateway")
        return app

    # In business logic:
    from shared.observability.tracing import get_tracer, create_span

    tracer = get_tracer(__name__)
    with tracer.start_as_current_span("process_batch") as span:
        span.set_attribute("batch_size", 10000)

    # Or use the convenience context manager:
    with create_span("process_batch", {"batch_size": 10000}) as span:
        ...

    # For inter-service HTTP calls:
    from shared.observability.tracing import inject_trace_context

    headers = inject_trace_context()
    response = requests.post(url, headers=headers, json=payload)

Note:
    This module intentionally uses the standard ``logging`` module rather than
    ``structlog`` or ``structured_logger`` to avoid a circular import dependency,
    since ``structured_logger`` imports ``get_current_trace_id`` from this module
    for correlation ID injection into structured log entries.
"""

from __future__ import annotations

import logging
import os
from contextlib import contextmanager
from typing import Generator

from flask import Flask

# ---------------------------------------------------------------------------
# Internal dependency — only from depends_on_files
# ---------------------------------------------------------------------------
from shared.config.base import BaseConfig

# ---------------------------------------------------------------------------
# OpenTelemetry API (opentelemetry-api >= 1.20.0)
# ---------------------------------------------------------------------------
from opentelemetry import context, propagate, trace
from opentelemetry.baggage.propagation import W3CBaggagePropagator
from opentelemetry.propagators.composite import CompositePropagator
from opentelemetry.trace.propagation.tracecontext import TraceContextTextMapPropagator

# ---------------------------------------------------------------------------
# OpenTelemetry SDK (opentelemetry-sdk >= 1.20.0)
# ---------------------------------------------------------------------------
from opentelemetry.sdk.resources import Resource
from opentelemetry.sdk.resources import SERVICE_NAME as RESOURCE_SERVICE_NAME
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import (
    BatchSpanProcessor,
    ConsoleSpanExporter,
    SimpleSpanProcessor,
)

# ---------------------------------------------------------------------------
# OTLP gRPC exporter (optional — graceful degradation if not installed)
# ---------------------------------------------------------------------------
try:
    from opentelemetry.exporter.otlp.proto.grpc.trace_exporter import (
        OTLPSpanExporter,
    )

    _OTLP_AVAILABLE: bool = True
except ImportError:
    _OTLP_AVAILABLE = False

# ---------------------------------------------------------------------------
# Flask instrumentation (optional — graceful degradation if not installed)
# ---------------------------------------------------------------------------
try:
    from opentelemetry.instrumentation.flask import FlaskInstrumentor

    _FLASK_INSTRUMENTOR_AVAILABLE: bool = True
except ImportError:
    _FLASK_INSTRUMENTOR_AVAILABLE = False

# ---------------------------------------------------------------------------
# Module-level logger — uses *standard* logging to avoid circular dependency
# with structured_logger.py which imports from this module.
# ---------------------------------------------------------------------------
logger: logging.Logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Module-level singleton state
# ---------------------------------------------------------------------------
_tracer_provider: TracerProvider | None = None
_initialized: bool = False

# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

__all__ = [
    "init_tracing",
    "get_tracer",
    "inject_trace_context",
    "extract_trace_context",
    "get_current_trace_id",
    "get_current_span_id",
    "create_span",
    "shutdown_tracing",
]


def init_tracing(
    app: Flask,
    service_name: str,
    exporter_endpoint: str | None = None,
    enable_console_export: bool = False,
) -> TracerProvider:
    """Initialise OpenTelemetry distributed tracing for a Flask microservice.

    This function must be called **once** during each service's ``create_app()``
    factory.  It performs the following steps:

    1. Guards against double initialisation — if already initialised, logs a
       warning and returns the existing ``TracerProvider``.
    2. Creates a ``Resource`` describing the service identity (name, namespace,
       version, deployment environment).
    3. Configures span processors:

       - **OTLP exporter** via ``BatchSpanProcessor`` when an endpoint is
         provided or set in ``OTEL_EXPORTER_ENDPOINT``.
       - **Console exporter** via ``SimpleSpanProcessor`` when
         ``enable_console_export`` is ``True`` or ``FLASK_ENV`` is
         ``development``.
       - Falls back to the console exporter if no OTLP endpoint is available.

    4. Sets the global ``TracerProvider`` and propagator
       (W3C TraceContext + Baggage).
    5. Instruments the Flask application with ``FlaskInstrumentor``, excluding
       health-check and metrics endpoints to reduce noise.

    Args:
        app: The Flask application instance to instrument.
        service_name: Logical name of the microservice (e.g.
            ``"api-gateway"``, ``"generation-engine"``).
        exporter_endpoint: Optional gRPC endpoint for the OTLP collector
            (e.g. ``"http://localhost:4317"``).  Falls back to the
            ``OTEL_EXPORTER_ENDPOINT`` environment variable and then to
            ``BaseConfig.OTEL_EXPORTER_ENDPOINT``.
        enable_console_export: When ``True``, spans are also printed to
            *stdout* via ``ConsoleSpanExporter``.  Automatically enabled
            when ``FLASK_ENV`` is ``development``.

    Returns:
        The configured ``TracerProvider`` instance, which can be used for
        advanced scenarios such as programmatically flushing spans.

    Raises:
        No exceptions are raised.  All errors during initialisation are
        logged and handled gracefully with fallback behaviour.

    Example::

        def create_app() -> Flask:
            app = Flask(__name__)
            init_tracing(app, service_name="api-gateway")
            return app
    """
    global _tracer_provider, _initialized  # noqa: PLW0603

    # ---- 1. Guard against double initialisation ----------------------------
    if _initialized and _tracer_provider is not None:
        logger.warning(
            "Tracing already initialised for this process. "
            "Returning existing TracerProvider."
        )
        return _tracer_provider

    # ---- 2. Resolve configuration (12-factor app) --------------------------
    resolved_endpoint: str | None = (
        exporter_endpoint
        or os.environ.get("OTEL_EXPORTER_ENDPOINT")
        or BaseConfig.OTEL_EXPORTER_ENDPOINT
        or None
    )
    # Treat empty string as None (BaseConfig defaults to "")
    if resolved_endpoint is not None and resolved_endpoint.strip() == "":
        resolved_endpoint = None

    resolved_service_name: str = (
        service_name
        or os.environ.get("SERVICE_NAME")
        or BaseConfig.SERVICE_NAME
        or "unknown-service"
    )

    flask_env: str = os.environ.get("FLASK_ENV", "development")

    # ---- 3. Create Resource with service identification --------------------
    resource: Resource = Resource.create(
        {
            RESOURCE_SERVICE_NAME: resolved_service_name,
            "service.namespace": "synthetic-erp-platform",
            "service.version": os.environ.get("SERVICE_VERSION", "1.0.0"),
            "deployment.environment": flask_env,
        }
    )

    # ---- 4. Create TracerProvider ------------------------------------------
    provider: TracerProvider = TracerProvider(resource=resource)

    # ---- 5. Configure span processors based on environment -----------------
    otlp_configured: bool = False

    if resolved_endpoint is not None:
        if not _OTLP_AVAILABLE:
            logger.warning(
                "opentelemetry-exporter-otlp-proto-grpc is not installed. "
                "OTLP span export is disabled. Install the package to enable it."
            )
        else:
            try:
                is_dev: bool = flask_env != "production"
                otlp_exporter: OTLPSpanExporter = OTLPSpanExporter(  # type: ignore[possibly-undefined]
                    endpoint=resolved_endpoint,
                    insecure=is_dev,
                )
                batch_processor: BatchSpanProcessor = BatchSpanProcessor(
                    otlp_exporter,
                    max_queue_size=2048,
                    schedule_delay_millis=5000,
                    max_export_batch_size=512,
                )
                provider.add_span_processor(batch_processor)
                otlp_configured = True
                logger.info(
                    "OTLP span exporter configured — endpoint=%s, insecure=%s",
                    resolved_endpoint,
                    is_dev,
                )
            except Exception as exc:
                logger.error(
                    "Failed to initialise OTLP span exporter at %s: %s. "
                    "Falling back to console exporter.",
                    resolved_endpoint,
                    exc,
                )

    # Console exporter for development / local debugging
    if enable_console_export or flask_env == "development":
        console_processor: SimpleSpanProcessor = SimpleSpanProcessor(
            ConsoleSpanExporter()
        )
        provider.add_span_processor(console_processor)
        logger.info("Console span exporter enabled for local debugging.")
    elif not otlp_configured:
        # No OTLP and not in development — add console as safety-net fallback
        console_processor = SimpleSpanProcessor(ConsoleSpanExporter())
        provider.add_span_processor(console_processor)
        logger.warning(
            "No OTLP exporter configured and not in development mode. "
            "Using console exporter as fallback."
        )

    # ---- 6. Set the global TracerProvider ----------------------------------
    trace.set_tracer_provider(provider)

    # ---- 7. Configure W3C TraceContext + Baggage propagation ---------------
    propagate.set_global_textmap(
        CompositePropagator(
            [
                TraceContextTextMapPropagator(),
                W3CBaggagePropagator(),
            ]
        )
    )

    # ---- 8. Instrument Flask application -----------------------------------
    if _FLASK_INSTRUMENTOR_AVAILABLE:
        try:
            FlaskInstrumentor().instrument_app(  # type: ignore[possibly-undefined]
                app,
                excluded_urls="health,ready,metrics",
                tracer_provider=provider,
            )
            logger.info(
                "Flask application instrumented for automatic request tracing."
            )
        except Exception as exc:
            logger.error("Failed to instrument Flask application: %s", exc)
    else:
        logger.warning(
            "opentelemetry-instrumentation-flask is not installed. "
            "Automatic Flask request tracing is disabled."
        )

    # ---- 9. Persist module-level state -------------------------------------
    _tracer_provider = provider
    _initialized = True

    logger.info(
        "OpenTelemetry tracing initialised for service '%s' "
        "(endpoint=%s, env=%s).",
        resolved_service_name,
        resolved_endpoint or "none",
        flask_env,
    )

    return provider


def get_tracer(name: str | None = None) -> trace.Tracer:
    """Return an OpenTelemetry ``Tracer`` for creating custom spans.

    This is the primary entry-point for services that need to add manual
    instrumentation beyond the automatic Flask request spans.

    If tracing has not been initialised (e.g. during unit tests or import-time
    side-effects), a **no-op tracer** is returned so that calling code never
    needs to check for ``None``.

    Args:
        name: Logical name for the tracer, typically ``__name__`` of the
            calling module.  When omitted the current module's ``__name__``
            is used.

    Returns:
        An ``opentelemetry.trace.Tracer`` that can be used to create spans::

            tracer = get_tracer(__name__)
            with tracer.start_as_current_span("process_batch") as span:
                span.set_attribute("batch_size", 10000)

    Example::

        from shared.observability.tracing import get_tracer

        tracer = get_tracer(__name__)
        with tracer.start_as_current_span("validate_schema") as span:
            span.set_attribute("table_count", 42)
    """
    tracer_name: str = name if name is not None else __name__

    if not _initialized or _tracer_provider is None:
        logger.warning(
            "Tracing not initialised. Returning no-op tracer for '%s'.",
            tracer_name,
        )
        # trace.get_tracer without a configured provider returns a no-op tracer
        return trace.get_tracer(tracer_name)

    return trace.get_tracer(tracer_name, tracer_provider=_tracer_provider)


def inject_trace_context(
    headers: dict[str, str] | None = None,
) -> dict[str, str]:
    """Inject the current trace context into HTTP headers for propagation.

    When making outbound HTTP calls to other microservices, call this function
    to add the W3C ``traceparent`` (and optionally ``tracestate`` / ``baggage``)
    headers so that the downstream service can continue the same trace.

    Args:
        headers: An existing header dictionary to inject into.  When ``None``
            a new dictionary is created.

    Returns:
        The headers dictionary with trace-context headers injected.  This is
        the **same** object passed in (or the newly created one) — no copy
        is made.

    Example::

        from shared.observability.tracing import inject_trace_context

        headers = inject_trace_context()
        response = requests.post(
            "http://generation-engine:5001/generate",
            headers=headers,
            json=payload,
        )
    """
    if headers is None:
        headers = {}

    propagate.inject(headers)
    return headers


def extract_trace_context(headers: dict[str, str]) -> context.Context:
    """Extract trace context from incoming HTTP request headers.

    Reads the W3C ``traceparent`` header (and optional ``tracestate`` /
    ``baggage``) from *headers* and returns an OpenTelemetry ``Context``
    that can be set as the current context.

    Typically this is handled **automatically** by ``FlaskInstrumentor``,
    but this function is available for manual use in non-Flask entry-points
    such as background workers or message consumers.

    Args:
        headers: A dictionary of HTTP headers from the incoming request.

    Returns:
        An ``opentelemetry.context.Context`` containing the extracted span
        context, or a root context if no valid trace headers are found.

    Example::

        ctx = extract_trace_context(request.headers)
        with tracer.start_as_current_span("process", context=ctx):
            ...
    """
    return propagate.extract(headers)


def get_current_trace_id() -> str | None:
    """Return the current trace ID as a 32-character lowercase hex string.

    This is used by ``structured_logger.py`` to inject correlation IDs into
    every structured log entry, enabling unified log-trace correlation across
    all six backend microservices.

    Returns:
        A 32-character hex string (e.g.
        ``"0af7651916cd43dd8448eb211c80319c"``) when a valid span is active,
        or ``None`` when no span is in progress.

    Example::

        trace_id = get_current_trace_id()
        if trace_id:
            log_extra["trace_id"] = trace_id
    """
    span: trace.Span = trace.get_current_span()

    if span is None or span is trace.INVALID_SPAN:
        return None

    span_context = span.get_span_context()
    if span_context is None or not span_context.is_valid:
        return None

    return format(span_context.trace_id, "032x")


def get_current_span_id() -> str | None:
    """Return the current span ID as a 16-character lowercase hex string.

    Returns:
        A 16-character hex string (e.g. ``"00f067aa0ba902b7"``) when a
        valid span is active, or ``None`` when no span is in progress.

    Example::

        span_id = get_current_span_id()
        if span_id:
            log_extra["span_id"] = span_id
    """
    span: trace.Span = trace.get_current_span()

    if span is None or span is trace.INVALID_SPAN:
        return None

    span_context = span.get_span_context()
    if span_context is None or not span_context.is_valid:
        return None

    return format(span_context.span_id, "016x")


@contextmanager
def create_span(
    name: str,
    attributes: dict[str, str | int | float | bool] | None = None,
) -> Generator[trace.Span, None, None]:
    """Create an OpenTelemetry span as a convenience context manager.

    Wraps ``get_tracer().start_as_current_span()`` with automatic attribute
    setting and exception recording.  If an exception occurs inside the
    ``with`` block it is recorded on the span, the span status is set to
    ``ERROR``, and the exception is re-raised.

    Args:
        name: Human-readable name for the span (e.g. ``"process_batch"``,
            ``"validate_schema"``).
        attributes: Optional mapping of span attributes to set immediately
            after creation.  Keys must be strings; values can be ``str``,
            ``int``, ``float``, or ``bool``.

    Yields:
        The active ``trace.Span`` instance, which can be used to add
        additional attributes, events, or links.

    Raises:
        Any exception raised inside the ``with`` block is recorded on the
        span and re-raised to the caller.

    Example::

        from shared.observability.tracing import create_span

        with create_span("generate_batch", {"batch_size": 10000}) as span:
            records = generate(batch_size=10000)
            span.set_attribute("records_generated", len(records))
    """
    tracer: trace.Tracer = get_tracer()
    with tracer.start_as_current_span(name) as span:
        if attributes is not None:
            for key, value in attributes.items():
                span.set_attribute(key, value)
        try:
            yield span
        except Exception as exc:
            span.record_exception(exc)
            span.set_status(trace.StatusCode.ERROR, str(exc))
            raise


def shutdown_tracing() -> None:
    """Gracefully shut down the tracing infrastructure and flush pending spans.

    Calls ``TracerProvider.shutdown()`` to ensure all buffered spans are
    exported before the process exits.  After shutdown the module reverts to
    its uninitialised state so that ``init_tracing`` can be called again
    (useful in test fixtures).

    This function should be registered via ``atexit`` or called in the Flask
    application teardown::

        import atexit
        from shared.observability.tracing import shutdown_tracing

        atexit.register(shutdown_tracing)
    """
    global _tracer_provider, _initialized  # noqa: PLW0603

    if not _initialized or _tracer_provider is None:
        logger.warning(
            "Tracing shutdown called but tracing was not initialised. "
            "No action taken."
        )
        return

    try:
        _tracer_provider.shutdown()
        logger.info("OpenTelemetry tracing shut down successfully.")
    except Exception as exc:
        logger.error(
            "Error during OpenTelemetry tracing shutdown: %s", exc
        )
    finally:
        _tracer_provider = None
        _initialized = False
