"""Prometheus metrics registration and export module for the Synthetic ERP Data
Generation Platform.

This module provides a centralised Prometheus metrics infrastructure that is shared
across all six backend microservices (API Gateway, Generation Engine, Profiling
Service, Quality Service, Compliance Service, Provisioning Service).  It delivers:

- **Pre-registered standard metrics** — HTTP request duration histograms, request
  counters by endpoint/method/status, active connection gauges, generation-job
  throughput counters, error-rate counters, quality-score gauges, and compliance-
  check duration histograms.  Every metric carries a ``service`` label so that a
  single Prometheus scrape target can distinguish data from different services.

- **Flask Blueprint for ``/metrics``** — A lightweight Blueprint
  (``metrics_blueprint``) that exposes all registered metrics in Prometheus text
  exposition format, ready for scraping by Prometheus or any compatible agent.

- **Automatic HTTP metrics collection** — The ``setup_metrics`` initialisation
  function attaches ``before_request`` and ``after_request`` hooks to the Flask
  application so that every inbound HTTP request is automatically measured
  (duration histogram + total counter + in-progress gauge).

- **Custom metric registration** — ``register_custom_metric`` allows each service
  to dynamically create business-specific metrics at startup.

- **Convenience helpers** — ``record_error`` and ``record_generation_throughput``
  provide high-level, intention-revealing wrappers around the raw metric objects.

The module intentionally uses the standard Python ``logging`` library (instead of
``structlog`` or the platform's ``structured_logger``) to avoid a circular
dependency, since ``structured_logger`` may import from this observability package.

All metric operations are thread-safe because ``prometheus_client`` instruments
are implemented with thread-safe internal state.

Usage::

    from shared.observability.metrics import setup_metrics

    def create_app() -> Flask:
        app = Flask(__name__)
        setup_metrics(app, service_name="api-gateway")
        return app

No credentials, connection strings, or secrets are referenced in this module.
Configuration is read exclusively from environment variables via
``shared.config.base.BaseConfig`` or passed as function parameters.
"""

from __future__ import annotations

import functools
import logging
import time
from typing import Any

from flask import Blueprint, Flask, Response, current_app, g, request

# ---------------------------------------------------------------------------
# Graceful import of prometheus_client — if the library is missing the module
# still loads but all metric operations become no-ops.
# ---------------------------------------------------------------------------

try:
    from prometheus_client import (
        CONTENT_TYPE_LATEST,
        REGISTRY,
        CollectorRegistry,
        Counter,
        Gauge,
        Histogram,
        Summary,
        generate_latest,
    )

    _PROMETHEUS_AVAILABLE: bool = True
except ImportError:  # pragma: no cover
    _PROMETHEUS_AVAILABLE = False

from shared.config.base import BaseConfig

# ---------------------------------------------------------------------------
# Module-level logger
# ---------------------------------------------------------------------------

logger: logging.Logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Excluded endpoints — these are not tracked in HTTP duration metrics to
# avoid polluting dashboards with high-frequency infrastructure traffic.
# ---------------------------------------------------------------------------

_EXCLUDED_ENDPOINTS: frozenset[str] = frozenset({"metrics_endpoint", "health", "ready"})
_EXCLUDED_PATHS: frozenset[str] = frozenset({"/metrics", "/health", "/ready"})

# ---------------------------------------------------------------------------
# Module-level custom-metrics registry (NOT the Prometheus registry — this is
# a plain Python dict mapping metric names to their instruments).
# ---------------------------------------------------------------------------

_custom_metrics: dict[str, Counter | Histogram | Gauge | Summary] = {}

# ===========================================================================
# Pre-registered Standard Metrics
# ===========================================================================
# All metrics include a 'service' label to distinguish between microservices.
# Histogram bucket boundaries are chosen to provide meaningful resolution for
# the expected latency/duration profiles of the platform's workloads.
# ===========================================================================

if _PROMETHEUS_AVAILABLE:
    HTTP_REQUEST_DURATION: Histogram = Histogram(
        "http_request_duration_seconds",
        "HTTP request duration in seconds",
        labelnames=["service", "method", "endpoint", "status_code"],
        buckets=(0.005, 0.01, 0.025, 0.05, 0.1, 0.25, 0.5, 1.0, 2.5, 5.0, 10.0),
    )
    """Histogram tracking the latency of every HTTP request handled by the service.

    Labels:
        service: Logical microservice name (e.g. ``api-gateway``).
        method: HTTP method (``GET``, ``POST``, …).
        endpoint: Flask endpoint name or URL path.
        status_code: HTTP response status code as a string.
    """

    HTTP_REQUEST_TOTAL: Counter = Counter(
        "http_requests_total",
        "Total HTTP requests",
        labelnames=["service", "method", "endpoint", "status_code"],
    )
    """Counter for the total number of HTTP requests processed.

    Labels: same as ``HTTP_REQUEST_DURATION``.
    """

    HTTP_REQUEST_IN_PROGRESS: Gauge = Gauge(
        "http_requests_in_progress",
        "Number of HTTP requests currently being processed",
        labelnames=["service", "method"],
    )
    """Gauge tracking the number of HTTP requests that are currently in-flight.

    Labels:
        service: Logical microservice name.
        method: HTTP method.
    """

    ACTIVE_CONNECTIONS: Gauge = Gauge(
        "active_connections",
        "Number of active connections",
        labelnames=["service", "connection_type"],
    )
    """Gauge for active connections to external resources (MongoDB, Redis, JDBC, …).

    Labels:
        service: Logical microservice name.
        connection_type: Resource identifier (e.g. ``mongodb``, ``redis``).
    """

    GENERATION_JOB_THROUGHPUT: Counter = Counter(
        "generation_job_records_total",
        "Total number of records generated",
        labelnames=["service", "generation_method", "erp_module"],
    )
    """Counter for the cumulative number of synthetic records produced.

    Labels:
        service: Logical microservice name.
        generation_method: Algorithm used (``ai_ml``, ``rules``, ``statistical``, ``masking``).
        erp_module: Target ERP module (``financial_accounting``, ``hr``, ``sales_distribution``, ``material_management``).
    """

    GENERATION_JOB_DURATION: Histogram = Histogram(
        "generation_job_duration_seconds",
        "Generation job duration in seconds",
        labelnames=["service", "generation_method"],
        buckets=(1.0, 5.0, 10.0, 30.0, 60.0, 120.0, 300.0, 600.0, 1800.0, 3600.0),
    )
    """Histogram tracking end-to-end generation job wall-clock duration.

    Labels:
        service: Logical microservice name.
        generation_method: Algorithm used.
    """

    ERROR_COUNTER: Counter = Counter(
        "errors_total",
        "Total number of errors",
        labelnames=["service", "error_type", "endpoint"],
    )
    """Counter for errors encountered across the platform.

    Labels:
        service: Logical microservice name.
        error_type: Categorisation string (e.g. ``validation``, ``timeout``, ``internal``).
        endpoint: The endpoint or operation where the error occurred.
    """

    QUALITY_SCORE: Gauge = Gauge(
        "quality_score",
        "Current quality score for generated data",
        labelnames=["service", "job_id", "score_type"],
    )
    """Gauge reflecting the most recent quality score for a generation job.

    Labels:
        service: Logical microservice name.
        job_id: Unique generation job identifier.
        score_type: Score dimension (``statistical``, ``business_rules``, ``referential_integrity``, ``composite``).
    """

    COMPLIANCE_CHECK_DURATION: Histogram = Histogram(
        "compliance_check_duration_seconds",
        "Compliance check duration in seconds",
        labelnames=["service", "check_type"],
    )
    """Histogram tracking the latency of compliance verification checks.

    Labels:
        service: Logical microservice name.
        check_type: Kind of compliance check (``pii_scan``, ``gdpr``, ``hipaa``, ``ccpa``).
    """
else:
    # ------------------------------------------------------------------
    # Fallback: When prometheus_client is not installed we expose module-
    # level sentinels so that importing code does not break.  These are
    # deliberately set to ``None`` — callers must guard metric operations
    # behind ``if _PROMETHEUS_AVAILABLE:`` or use the convenience helpers
    # provided later in this module (which include that guard internally).
    # ------------------------------------------------------------------
    HTTP_REQUEST_DURATION = None  # type: ignore[assignment]
    HTTP_REQUEST_TOTAL = None  # type: ignore[assignment]
    HTTP_REQUEST_IN_PROGRESS = None  # type: ignore[assignment]
    ACTIVE_CONNECTIONS = None  # type: ignore[assignment]
    GENERATION_JOB_THROUGHPUT = None  # type: ignore[assignment]
    GENERATION_JOB_DURATION = None  # type: ignore[assignment]
    ERROR_COUNTER = None  # type: ignore[assignment]
    QUALITY_SCORE = None  # type: ignore[assignment]
    COMPLIANCE_CHECK_DURATION = None  # type: ignore[assignment]

    logger.warning(
        "prometheus_client is not installed — all Prometheus metrics are disabled. "
        "Install prometheus-client>=0.20.0 to enable metrics collection."
    )


# ===========================================================================
# Flask Blueprint — /metrics endpoint
# ===========================================================================

metrics_blueprint: Blueprint = Blueprint("metrics", __name__)
"""Flask Blueprint that registers the ``/metrics`` Prometheus scrape endpoint."""


@metrics_blueprint.route("/metrics")
def metrics_endpoint() -> Response:
    """Prometheus scrape endpoint returning all registered metrics in text
    exposition format.

    Returns:
        Flask ``Response`` whose body is the serialised Prometheus metrics
        and whose ``Content-Type`` header is set to the standard Prometheus
        media type.
    """
    if not _PROMETHEUS_AVAILABLE:
        return Response(
            "# prometheus_client is not installed\n",
            status=503,
            mimetype="text/plain",
        )
    return Response(
        generate_latest(REGISTRY),
        mimetype=CONTENT_TYPE_LATEST,
    )


# ===========================================================================
# setup_metrics — main initialisation entry-point
# ===========================================================================


def setup_metrics(app: Flask, service_name: str) -> None:
    """Initialise Prometheus metrics collection for a Flask application.

    This function is designed to be called once inside each service's
    ``create_app()`` factory.  It performs the following steps:

    1. Registers the ``metrics_blueprint`` on the Flask application so that
       the ``/metrics`` endpoint becomes available.
    2. Stores *service_name* in ``app.config["SERVICE_NAME"]`` for use as a
       Prometheus label value.
    3. Attaches ``before_request`` / ``after_request`` hooks that automatically
       record HTTP request duration, total count, and in-progress gauge for
       every inbound request (excluding ``/metrics``, ``/health``, and
       ``/ready`` paths).

    Args:
        app: The Flask application instance.
        service_name: Logical name of the microservice (e.g. ``"api-gateway"``).
            Falls back to ``BaseConfig.SERVICE_NAME`` when an empty string is
            provided.

    Example::

        app = Flask(__name__)
        setup_metrics(app, service_name="generation-engine")
    """
    resolved_service_name: str = service_name or BaseConfig.SERVICE_NAME

    # Store the service name so hooks and helpers can retrieve it.
    app.config["SERVICE_NAME"] = resolved_service_name

    # Register the /metrics blueprint.
    app.register_blueprint(metrics_blueprint)

    if not _PROMETHEUS_AVAILABLE:
        logger.warning(
            "Metrics setup skipped — prometheus_client is not installed."
        )
        return

    logger.info(
        "Initialising Prometheus metrics for service '%s' "
        "(port hint: %d).",
        resolved_service_name,
        BaseConfig.PROMETHEUS_PORT,
    )

    # ------------------------------------------------------------------
    # before_request — record start time and increment in-progress gauge
    # ------------------------------------------------------------------

    @app.before_request
    def _before_request() -> None:
        """Capture request start time and bump the in-progress gauge."""
        g._metrics_start_time = time.perf_counter()  # noqa: SLF001
        request_path: str = request.path
        request_endpoint: str | None = request.endpoint

        if request_path in _EXCLUDED_PATHS or request_endpoint in _EXCLUDED_ENDPOINTS:
            g._metrics_excluded = True  # noqa: SLF001
            return

        g._metrics_excluded = False  # noqa: SLF001
        try:
            HTTP_REQUEST_IN_PROGRESS.labels(
                service=resolved_service_name,
                method=request.method,
            ).inc()
        except Exception:
            logger.debug("Failed to increment in-progress gauge.", exc_info=True)

    # ------------------------------------------------------------------
    # after_request — observe duration, bump counter, decrement in-prog.
    # ------------------------------------------------------------------

    @app.after_request
    def _after_request(response: Response) -> Response:
        """Record request duration, total count, and decrement in-progress."""
        if getattr(g, "_metrics_excluded", True):
            return response

        try:
            elapsed: float = time.perf_counter() - getattr(
                g, "_metrics_start_time", time.perf_counter()
            )
            endpoint_label: str = request.endpoint or request.path
            status_label: str = str(response.status_code)

            HTTP_REQUEST_DURATION.labels(
                service=resolved_service_name,
                method=request.method,
                endpoint=endpoint_label,
                status_code=status_label,
            ).observe(elapsed)

            HTTP_REQUEST_TOTAL.labels(
                service=resolved_service_name,
                method=request.method,
                endpoint=endpoint_label,
                status_code=status_label,
            ).inc()

            HTTP_REQUEST_IN_PROGRESS.labels(
                service=resolved_service_name,
                method=request.method,
            ).dec()
        except Exception:
            logger.debug("Failed to record after-request metrics.", exc_info=True)

        return response

    logger.info(
        "Prometheus metrics initialised for service '%s'.",
        resolved_service_name,
    )


# ===========================================================================
# track_request_metrics — decorator for individual route handlers
# ===========================================================================


def track_request_metrics(service_name: str) -> Any:
    """Decorator that automatically tracks HTTP request duration and count for
    a wrapped Flask route handler.

    This is useful when a service needs per-route tracking *in addition to* the
    automatic ``setup_metrics`` hooks, or when the automatic hooks have not been
    enabled.

    The decorator uses ``time.perf_counter()`` for high-resolution monotonic
    timing and records labels ``service``, ``method``, ``endpoint``, and
    ``status_code``.

    Args:
        service_name: Logical microservice name for the ``service`` label.

    Returns:
        A decorator that wraps the target function with metrics collection.

    Example::

        @app.route("/api/v1/data")
        @track_request_metrics("api-gateway")
        def get_data():
            ...
    """

    def decorator(fn: Any) -> Any:
        @functools.wraps(fn)
        def wrapper(*args: Any, **kwargs: Any) -> Any:
            if not _PROMETHEUS_AVAILABLE:
                return fn(*args, **kwargs)

            start: float = time.perf_counter()
            response: Any = None
            try:
                response = fn(*args, **kwargs)
                return response
            finally:
                elapsed = time.perf_counter() - start
                # Determine the status code from a Flask Response or default
                # to '200' for plain string/tuple returns.
                status_code: str = "200"
                if hasattr(response, "status_code"):
                    status_code = str(response.status_code)
                elif isinstance(response, tuple) and len(response) >= 2:
                    status_code = str(response[1])

                endpoint_label: str = request.endpoint or request.path

                try:
                    HTTP_REQUEST_DURATION.labels(
                        service=service_name,
                        method=request.method,
                        endpoint=endpoint_label,
                        status_code=status_code,
                    ).observe(elapsed)

                    HTTP_REQUEST_TOTAL.labels(
                        service=service_name,
                        method=request.method,
                        endpoint=endpoint_label,
                        status_code=status_code,
                    ).inc()
                except Exception:
                    logger.debug(
                        "track_request_metrics: failed to record metrics.",
                        exc_info=True,
                    )

        return wrapper

    return decorator


# ===========================================================================
# register_custom_metric — dynamic metric factory
# ===========================================================================


def register_custom_metric(
    name: str,
    description: str,
    metric_type: str,
    labelnames: list[str] | None = None,
    **kwargs: Any,
) -> Counter | Histogram | Gauge | Summary:
    """Create and register a custom Prometheus metric instrument.

    Services call this function at startup to define business-specific metrics
    that are not covered by the pre-registered standard set.  The created
    instrument is stored in a module-level dictionary so it can be retrieved
    later via ``get_metrics_registry``.

    Args:
        name: Metric name following Prometheus naming conventions
            (e.g. ``"batch_processing_duration_seconds"``).
        description: Human-readable help text.
        metric_type: One of ``"counter"``, ``"histogram"``, ``"gauge"``,
            or ``"summary"``.
        labelnames: Optional list of label names for the metric.
        **kwargs: Additional keyword arguments forwarded to the
            ``prometheus_client`` constructor (e.g. ``buckets`` for
            histograms).

    Returns:
        The newly created metric instrument.

    Raises:
        ValueError: If *metric_type* is not one of the four supported types.
        RuntimeError: If ``prometheus_client`` is not installed.

    Example::

        batch_size_hist = register_custom_metric(
            name="batch_size_records",
            description="Number of records in each generation batch",
            metric_type="histogram",
            labelnames=["service", "generation_method"],
            buckets=[100, 500, 1000, 5000, 10000, 50000, 100000],
        )
    """
    if not _PROMETHEUS_AVAILABLE:
        raise RuntimeError(
            "Cannot register custom metric — prometheus_client is not installed."
        )

    resolved_labels: list[str] = labelnames if labelnames is not None else []

    _METRIC_CONSTRUCTORS: dict[str, type] = {
        "counter": Counter,
        "histogram": Histogram,
        "gauge": Gauge,
        "summary": Summary,
    }

    normalised_type: str = metric_type.strip().lower()

    constructor = _METRIC_CONSTRUCTORS.get(normalised_type)
    if constructor is None:
        supported = ", ".join(sorted(_METRIC_CONSTRUCTORS.keys()))
        raise ValueError(
            f"Unknown metric_type '{metric_type}'. "
            f"Supported types: {supported}."
        )

    try:
        metric: Counter | Histogram | Gauge | Summary = constructor(
            name,
            description,
            labelnames=resolved_labels,
            **kwargs,
        )
    except Exception as exc:
        logger.error(
            "Failed to register custom %s metric '%s': %s",
            normalised_type,
            name,
            exc,
        )
        raise

    _custom_metrics[name] = metric

    logger.info(
        "Registered custom %s metric '%s' with labels %s.",
        normalised_type,
        name,
        resolved_labels,
    )

    return metric


# ===========================================================================
# get_metrics_registry — introspection of custom metrics
# ===========================================================================


def get_metrics_registry() -> dict[str, Counter | Histogram | Gauge | Summary]:
    """Return a shallow copy of the custom-metrics dictionary.

    This mapping contains **only** the metrics created via
    ``register_custom_metric``; the pre-registered standard metrics
    (``HTTP_REQUEST_DURATION``, ``ERROR_COUNTER``, etc.) are not included
    because they are already accessible as module-level constants.

    Returns:
        A dictionary mapping metric names to their ``prometheus_client``
        instrument objects.
    """
    return dict(_custom_metrics)


# ===========================================================================
# Convenience helpers
# ===========================================================================


def _get_service_name_from_app(fallback: str = "unknown-service") -> str:
    """Resolve the current service name from the Flask application config.

    This helper attempts to read ``SERVICE_NAME`` from
    ``current_app.config`` (set by ``setup_metrics``).  If the application
    context is not active, the *fallback* value is returned.

    Args:
        fallback: Default service name when no app context is available.

    Returns:
        The resolved service name string.
    """
    try:
        return str(current_app.config.get("SERVICE_NAME", fallback))
    except RuntimeError:
        # Outside of a Flask application context.
        return fallback


def record_error(
    service_name: str,
    error_type: str,
    endpoint: str = "unknown",
) -> None:
    """Increment the ``errors_total`` counter.

    This is a convenience wrapper intended to be called from error handlers,
    exception hooks, and middleware layers.

    Args:
        service_name: Logical microservice name.
        error_type: Classification of the error (e.g. ``"validation"``,
            ``"timeout"``, ``"internal"``).
        endpoint: The Flask endpoint or operation name where the error
            occurred.  Defaults to ``"unknown"`` when not supplied.
    """
    if not _PROMETHEUS_AVAILABLE or ERROR_COUNTER is None:
        return

    try:
        ERROR_COUNTER.labels(
            service=service_name,
            error_type=error_type,
            endpoint=endpoint,
        ).inc()
    except Exception:
        logger.debug("record_error: failed to increment error counter.", exc_info=True)


def record_generation_throughput(
    service_name: str,
    method: str,
    erp_module: str,
    record_count: int,
) -> None:
    """Increment the ``generation_job_records_total`` counter by *record_count*.

    This is called by the Generation Engine after each batch of synthetic
    records has been produced.

    Args:
        service_name: Logical microservice name.
        method: Generation algorithm (e.g. ``"ai_ml"``, ``"rules"``,
            ``"statistical"``, ``"masking"``).
        erp_module: Target ERP module (e.g. ``"financial_accounting"``).
        record_count: Number of records generated in this batch.
    """
    if not _PROMETHEUS_AVAILABLE or GENERATION_JOB_THROUGHPUT is None:
        return

    if record_count < 0:
        logger.warning(
            "record_generation_throughput called with negative record_count=%d; "
            "clamping to 0.",
            record_count,
        )
        record_count = 0

    try:
        GENERATION_JOB_THROUGHPUT.labels(
            service=service_name,
            generation_method=method,
            erp_module=erp_module,
        ).inc(record_count)
    except Exception:
        logger.debug(
            "record_generation_throughput: failed to increment throughput counter.",
            exc_info=True,
        )
