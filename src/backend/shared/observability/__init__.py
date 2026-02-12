"""Observability package for the Synthetic ERP Data Generation Platform.

This package provides Prometheus metrics collection and OpenTelemetry
distributed tracing capabilities shared across all six backend microservices:

- **API Gateway** — HTTP request metrics and API-level span tracing.
- **Generation Engine** — Generation job throughput/duration metrics and
  batch-processing trace spans.
- **Profiling Service** — Schema discovery and statistical profiling metrics
  with cross-service trace propagation.
- **Quality Service** — Quality score gauges and validation duration metrics.
- **Compliance Service** — Compliance check duration histograms and PII
  detection trace spans.
- **Provisioning Service** — Database provisioning and cloud export metrics
  with end-to-end trace correlation.

Sub-modules:

- :mod:`shared.observability.metrics` — Pre-registered Prometheus metrics
  (HTTP request duration/count, generation throughput, error counters,
  quality scores, compliance check durations), a Flask Blueprint exposing
  the ``/metrics`` scrape endpoint, automatic request instrumentation via
  ``setup_metrics()``, custom metric registration, and convenience helpers
  (``record_error``, ``record_generation_throughput``).

- :mod:`shared.observability.tracing` — OpenTelemetry distributed tracing
  with OTLP export and ``BatchSpanProcessor`` for production throughput,
  W3C TraceContext propagation (``inject_trace_context``), automatic Flask
  request instrumentation via ``FlaskInstrumentor``, tracer factory
  (``get_tracer``), convenience span context manager (``create_span``),
  and correlation ID helpers for unified log-trace correlation.

This ``__init__.py`` is a lightweight package marker — all substantial logic
resides in ``metrics.py`` and ``tracing.py``.  It simply re-exports the
public API so that consumers can write convenient short-form imports::

    # Short-form via package __init__
    from shared.observability import setup_metrics, init_tracing

    # Equivalent explicit imports (also valid)
    from shared.observability.metrics import setup_metrics
    from shared.observability.tracing import init_tracing

Sub-modules that have not yet been installed or whose transitive
dependencies (``prometheus_client``, ``opentelemetry-*``) are missing are
silently skipped to allow incremental deployment and avoid circular import
errors during early bootstrapping.

Note:
    This module does **not** import ``structlog`` or the platform's
    ``structured_logger`` to avoid circular dependencies, since the
    structured logger itself imports from the tracing sub-module for
    correlation ID injection.
"""

from __future__ import annotations


# ---------------------------------------------------------------------------
# Controlled public API — populated dynamically below so that missing
# optional dependencies (prometheus_client, opentelemetry) do not prevent
# the package from being imported.
# ---------------------------------------------------------------------------

__all__: list[str] = []

# ---------------------------------------------------------------------------
# Tracing sub-module re-exports
# ---------------------------------------------------------------------------
# Provides: init_tracing, get_tracer, inject_trace_context, create_span,
# extract_trace_context, get_current_trace_id, get_current_span_id,
# shutdown_tracing
# ---------------------------------------------------------------------------

try:
    from shared.observability.tracing import (  # noqa: F401
        create_span,
        extract_trace_context,
        get_current_span_id,
        get_current_trace_id,
        get_tracer,
        init_tracing,
        inject_trace_context,
        shutdown_tracing,
    )

    __all__.extend(
        [
            "init_tracing",
            "get_tracer",
            "inject_trace_context",
            "create_span",
            "extract_trace_context",
            "get_current_trace_id",
            "get_current_span_id",
            "shutdown_tracing",
        ]
    )
except ImportError:
    # opentelemetry SDK is not installed — tracing utilities unavailable.
    pass

# ---------------------------------------------------------------------------
# Metrics sub-module re-exports
# ---------------------------------------------------------------------------
# Provides: setup_metrics, metrics_blueprint, register_custom_metric,
# get_metrics_registry, track_request_metrics, record_error,
# record_generation_throughput, and all pre-registered standard metric
# constants (HTTP_REQUEST_DURATION, HTTP_REQUEST_TOTAL, etc.)
# ---------------------------------------------------------------------------

try:
    from shared.observability.metrics import (  # noqa: F401
        ACTIVE_CONNECTIONS,
        COMPLIANCE_CHECK_DURATION,
        ERROR_COUNTER,
        GENERATION_JOB_DURATION,
        GENERATION_JOB_THROUGHPUT,
        HTTP_REQUEST_DURATION,
        HTTP_REQUEST_IN_PROGRESS,
        HTTP_REQUEST_TOTAL,
        QUALITY_SCORE,
        get_metrics_registry,
        metrics_blueprint,
        record_error,
        record_generation_throughput,
        register_custom_metric,
        setup_metrics,
        track_request_metrics,
    )

    __all__.extend(
        [
            # Core initialisation and Blueprint
            "setup_metrics",
            "metrics_blueprint",
            "register_custom_metric",
            "get_metrics_registry",
            # Decorator and convenience helpers
            "track_request_metrics",
            "record_error",
            "record_generation_throughput",
            # Pre-registered standard metric constants
            "HTTP_REQUEST_DURATION",
            "HTTP_REQUEST_TOTAL",
            "HTTP_REQUEST_IN_PROGRESS",
            "ACTIVE_CONNECTIONS",
            "GENERATION_JOB_THROUGHPUT",
            "GENERATION_JOB_DURATION",
            "ERROR_COUNTER",
            "QUALITY_SCORE",
            "COMPLIANCE_CHECK_DURATION",
        ]
    )
except ImportError:
    # prometheus_client is not installed — metrics utilities unavailable.
    pass
