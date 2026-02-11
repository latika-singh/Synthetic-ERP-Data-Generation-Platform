"""Observability package for the Synthetic ERP Data Generation Platform.

This package provides cross-cutting observability infrastructure shared by all
six backend microservices:

- **tracing** — OpenTelemetry distributed tracing with OTLP export, W3C
  TraceContext propagation, automatic Flask request instrumentation, and
  correlation ID helpers for unified log-trace correlation.
- **metrics** — Prometheus metrics registration and export for request
  latency, throughput, error rates, and business-level counters.

Usage::

    from shared.observability.tracing import init_tracing, get_tracer, create_span
    from shared.observability.metrics import setup_metrics, track_request_metrics

Sub-modules that have not yet been installed or created are silently skipped
to allow incremental deployment and avoid circular import errors.
"""

from __future__ import annotations


__all__: list[str] = []

# --- tracing --------------------------------------------------------------
try:
    from shared.observability.tracing import (
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
            "create_span",
            "extract_trace_context",
            "get_current_span_id",
            "get_current_trace_id",
            "get_tracer",
            "init_tracing",
            "inject_trace_context",
            "shutdown_tracing",
        ]
    )
except ImportError:
    pass

# --- metrics --------------------------------------------------------------
try:
    from shared.observability.metrics import (
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
            "ACTIVE_CONNECTIONS",
            "COMPLIANCE_CHECK_DURATION",
            "ERROR_COUNTER",
            "GENERATION_JOB_DURATION",
            "GENERATION_JOB_THROUGHPUT",
            "HTTP_REQUEST_DURATION",
            "HTTP_REQUEST_IN_PROGRESS",
            "HTTP_REQUEST_TOTAL",
            "QUALITY_SCORE",
            "get_metrics_registry",
            "metrics_blueprint",
            "record_error",
            "record_generation_throughput",
            "register_custom_metric",
            "setup_metrics",
            "track_request_metrics",
        ]
    )
except ImportError:
    pass
