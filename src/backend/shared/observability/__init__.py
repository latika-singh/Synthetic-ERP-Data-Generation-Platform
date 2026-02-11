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
    from shared.observability.metrics import init_metrics, track_request

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
    from shared.observability.metrics import init_metrics  # type: ignore[import-untyped]

    __all__.append("init_metrics")
except ImportError:
    pass
