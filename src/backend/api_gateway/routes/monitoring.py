"""Flask Blueprint for Prometheus metrics and monitoring REST endpoints.

Exposes the following endpoints under ``/api/v1/monitoring/``:

- **GET /metrics** — Prometheus scrape target that serialises all registered
  Prometheus metrics (request counts, latency histograms, error rates, active
  connections, service metadata) in OpenMetrics / text exposition format.
- **GET /status** — System-wide status dashboard data aggregating API Gateway
  health, MongoDB connection pool statistics, Redis client statistics, and
  downstream microservice availability.
- **GET /services** — Downstream service health matrix reporting the
  reachability, latency, and current status of every microservice in the
  platform (Generation Engine, Profiling, Quality, Compliance, Provisioning).

Security:
    All three endpoints require JWT authentication via the ``@jwt_required()``
    decorator (R-006).  The ``/status`` and ``/services`` endpoints additionally
    require the ``admin:system`` permission enforced by
    :func:`~api_gateway.middleware.auth.require_permissions`.

Observability:
    API Gateway-specific Prometheus metrics are defined at module level as
    ``Counter``, ``Histogram``, ``Gauge``, and ``Info`` instruments.  These
    are exported so that other parts of the API Gateway (middleware, route
    handlers, service layer) can record observations directly.

Resilience:
    Health checks to downstream microservices use the circuit breaker pattern
    via :func:`~shared.middleware.circuit_breaker.circuit_breaker_decorator`
    to prevent cascading failures when a downstream service is unresponsive.

Usage::

    # In the Flask Application Factory (app.py):
    from api_gateway.routes.monitoring import monitoring_bp

    app.register_blueprint(monitoring_bp, url_prefix="/api/v1/monitoring")
"""

from __future__ import annotations

import datetime
import time
from typing import Any, Dict, List, Optional

from flask import Blueprint, Response, current_app, jsonify, request
from flask_jwt_extended import jwt_required
from prometheus_client import (
    CONTENT_TYPE_LATEST,
    REGISTRY,
    Counter,
    Gauge,
    Histogram,
    Info,
    generate_latest,
)

from api_gateway.extensions import get_db, get_redis
from api_gateway.middleware.auth import require_permissions
from shared.logging.structured_logger import get_logger
from shared.middleware.circuit_breaker import (
    CircuitBreakerError,
    circuit_breaker_decorator,
)
from shared.observability.metrics import (
    ERROR_COUNTER,
    HTTP_REQUEST_DURATION,
    HTTP_REQUEST_TOTAL,
    record_error,
)

# Third-party HTTP client for downstream service health checks.
import requests as http_requests


# ---------------------------------------------------------------------------
# Module-level structured logger
# ---------------------------------------------------------------------------

logger = get_logger(__name__)

# ---------------------------------------------------------------------------
# Service start time — captured once at module load for uptime calculation.
# ---------------------------------------------------------------------------

_SERVICE_START_TIME: datetime.datetime = datetime.datetime.utcnow()

# ---------------------------------------------------------------------------
# Flask Blueprint
# ---------------------------------------------------------------------------

monitoring_bp: Blueprint = Blueprint("monitoring", __name__)
"""Monitoring Blueprint registered under ``/api/v1/monitoring`` by the
application factory.  Provides ``/metrics``, ``/status``, and ``/services``
endpoints."""

# ===========================================================================
# API Gateway-specific Prometheus Metrics
# ===========================================================================
# These metric instruments are exported so that middleware, route handlers,
# and service-layer code can record observations from anywhere in the API
# Gateway codebase.
# ===========================================================================

REQUEST_COUNT: Counter = Counter(
    "api_gateway_requests_total",
    "Total HTTP requests processed by the API Gateway",
    ["method", "endpoint", "status_code"],
)
"""Counter tracking every HTTP request handled by the API Gateway.

Labels:
    method: HTTP method (``GET``, ``POST``, ``PUT``, ``DELETE``, …).
    endpoint: Flask endpoint name or URL path.
    status_code: HTTP response status code as a string.
"""

REQUEST_LATENCY: Histogram = Histogram(
    "api_gateway_request_duration_seconds",
    "HTTP request duration in seconds for the API Gateway",
    ["method", "endpoint"],
    buckets=[0.01, 0.05, 0.1, 0.25, 0.5, 1.0, 2.5, 5.0, 10.0],
)
"""Histogram tracking the latency of API Gateway request processing.

Labels:
    method: HTTP method.
    endpoint: Flask endpoint name or URL path.
"""

ACTIVE_REQUESTS: Gauge = Gauge(
    "api_gateway_active_requests",
    "Number of HTTP requests currently being processed by the API Gateway",
)
"""Gauge reflecting the count of in-flight requests at any point in time."""

ERROR_COUNT: Counter = Counter(
    "api_gateway_errors_total",
    "Total error responses returned by the API Gateway",
    ["error_type", "status_code"],
)
"""Counter tracking error responses by error classification and HTTP status.

Labels:
    error_type: Classification string (e.g. ``validation``, ``timeout``,
        ``internal``, ``authentication``).
    status_code: HTTP response status code as a string.
"""

GENERATION_JOBS_CREATED: Counter = Counter(
    "generation_jobs_created_total",
    "Total generation jobs created through the API Gateway",
    ["method", "tenant_id"],
)
"""Counter for generation jobs submitted through the API Gateway.

Labels:
    method: Generation method selected (``ai_ml``, ``rules``, ``statistical``,
        ``masking``).
    tenant_id: The tenant namespace that created the job.
"""

SERVICE_INFO: Info = Info(
    "api_gateway",
    "API Gateway service information and build metadata",
)
"""Info metric exposing static service metadata (version, environment, etc.)
to Prometheus.  Set once at module load time and updated if the Flask app
configuration becomes available."""

# Set initial service info — updated with app context values when available.
SERVICE_INFO.info(
    {
        "version": "1.0.0",
        "service": "api-gateway",
        "framework": "flask",
        "python_version": "3.12",
    }
)

# ===========================================================================
# Downstream service definitions — used by /status and /services endpoints.
# ===========================================================================

_DOWNSTREAM_SERVICES: List[Dict[str, Any]] = [
    {
        "name": "generation-engine",
        "url": "http://generation-engine:5001/health",
        "port": 5001,
        "display_name": "Generation Engine",
    },
    {
        "name": "profiling-service",
        "url": "http://profiling-service:5002/health",
        "port": 5002,
        "display_name": "Profiling Service",
    },
    {
        "name": "quality-service",
        "url": "http://quality-service:5003/health",
        "port": 5003,
        "display_name": "Quality Service",
    },
    {
        "name": "compliance-service",
        "url": "http://compliance-service:5004/health",
        "port": 5004,
        "display_name": "Compliance Service",
    },
    {
        "name": "provisioning-service",
        "url": "http://provisioning-service:5005/health",
        "port": 5005,
        "display_name": "Provisioning Service",
    },
]

# Health check timeout in seconds for downstream service probes.
_HEALTH_CHECK_TIMEOUT: int = 5


# ===========================================================================
# Circuit-breaker-protected downstream health check helper
# ===========================================================================


@circuit_breaker_decorator(
    name="monitoring_health_check",
    failure_threshold=3,
    recovery_timeout=30,
    max_retries=0,
)
def _check_service_health(url: str, timeout: int = _HEALTH_CHECK_TIMEOUT) -> Dict[str, Any]:
    """Perform an HTTP GET health check against a downstream service.

    Protected by a circuit breaker to prevent blocking the monitoring
    endpoint when a downstream service is persistently unreachable.

    Args:
        url: The full URL of the downstream service's health endpoint
            (e.g. ``http://generation-engine:5001/health``).
        timeout: Maximum seconds to wait for a response.

    Returns:
        A dictionary containing:
            - ``status`` (str): ``"healthy"`` or ``"unhealthy"``.
            - ``latency_ms`` (float): Round-trip latency in milliseconds.
            - ``status_code`` (int | None): HTTP status code, or ``None``
              on connection failure.
            - ``detail`` (str | None): Optional detail from the health
              response body.

    Raises:
        requests.exceptions.RequestException: Propagated to the circuit
            breaker for failure tracking.
    """
    start: float = time.time()
    response = http_requests.get(url, timeout=timeout)
    elapsed_ms: float = round((time.time() - start) * 1000, 2)

    is_healthy: bool = response.status_code == 200

    # Attempt to extract detail from JSON body.
    detail: Optional[str] = None
    try:
        body: Dict[str, Any] = response.json()
        detail = body.get("status", body.get("message"))
    except (ValueError, AttributeError):
        pass

    return {
        "status": "healthy" if is_healthy else "unhealthy",
        "latency_ms": elapsed_ms,
        "status_code": response.status_code,
        "detail": detail,
    }


def _safe_check_service(service: Dict[str, Any]) -> Dict[str, Any]:
    """Execute a health check against a single downstream service with full
    error handling.

    Wraps :func:`_check_service_health` to catch circuit breaker errors,
    connection failures, and timeouts, returning a structured result in all
    cases.

    Args:
        service: A service descriptor dictionary from
            :data:`_DOWNSTREAM_SERVICES` containing ``name``, ``url``,
            ``port``, and ``display_name`` keys.

    Returns:
        A dictionary with keys ``name``, ``display_name``, ``url``,
        ``status``, ``latency_ms``, ``status_code``, and ``detail``
        suitable for inclusion in the ``/services`` JSON response.
    """
    url: str = current_app.config.get(
        f"{service['name'].upper().replace('-', '_')}_HEALTH_URL",
        service["url"],
    )

    result: Dict[str, Any] = {
        "name": service["name"],
        "display_name": service["display_name"],
        "url": url,
        "status": "unhealthy",
        "latency_ms": 0.0,
        "status_code": None,
        "detail": None,
    }

    try:
        check_result: Dict[str, Any] = _check_service_health(url)
        result.update(check_result)

    except CircuitBreakerError:
        result["status"] = "unhealthy"
        result["detail"] = "Circuit breaker open — service previously unresponsive"
        logger.warning(
            "service_health_check_circuit_open",
            service_name=service["name"],
            url=url,
        )

    except http_requests.exceptions.Timeout:
        result["status"] = "unhealthy"
        result["detail"] = f"Health check timed out after {_HEALTH_CHECK_TIMEOUT}s"
        logger.warning(
            "service_health_check_timeout",
            service_name=service["name"],
            url=url,
            timeout=_HEALTH_CHECK_TIMEOUT,
        )

    except http_requests.exceptions.ConnectionError as exc:
        result["status"] = "unhealthy"
        result["detail"] = f"Connection refused: {exc}"
        logger.warning(
            "service_health_check_connection_error",
            service_name=service["name"],
            url=url,
            error=str(exc),
        )

    except http_requests.exceptions.RequestException as exc:
        result["status"] = "unhealthy"
        result["detail"] = f"Request error: {exc}"
        logger.error(
            "service_health_check_request_error",
            service_name=service["name"],
            url=url,
            error=str(exc),
            error_type=type(exc).__name__,
        )

    except Exception as exc:
        result["status"] = "unhealthy"
        result["detail"] = f"Unexpected error: {type(exc).__name__}"
        logger.error(
            "service_health_check_unexpected_error",
            service_name=service["name"],
            url=url,
            error=str(exc),
            error_type=type(exc).__name__,
        )

    return result


# ===========================================================================
# Internal helpers for /status endpoint
# ===========================================================================


def _get_mongodb_status() -> Dict[str, Any]:
    """Retrieve MongoDB connection pool and health information.

    Queries the MongoDB client's ``server_info()`` and connection pool
    statistics to report database availability and resource utilisation.

    Returns:
        A dictionary with keys ``status``, ``server_version``,
        ``connection_pool_size``, ``active_connections``, and ``database``.
    """
    try:
        db = get_db()
        client = db.client
        server_info: Dict[str, Any] = client.server_info()

        # Extract pool stats from the MongoClient options.
        pool_options = client.options.pool_options
        max_pool_size: int = pool_options.max_pool_size if pool_options else 100

        return {
            "status": "connected",
            "server_version": server_info.get("version", "unknown"),
            "connection_pool_size": max_pool_size,
            "active_connections": max_pool_size,  # Approximate; exact count not always exposed
            "database": db.name,
        }
    except Exception as exc:
        logger.error(
            "mongodb_status_check_failed",
            error=str(exc),
            error_type=type(exc).__name__,
        )
        return {
            "status": "disconnected",
            "server_version": None,
            "connection_pool_size": 0,
            "active_connections": 0,
            "database": None,
            "error": str(exc),
        }


def _get_redis_status() -> Dict[str, Any]:
    """Retrieve Redis client health and memory utilisation statistics.

    Calls ``INFO`` on the Redis client to collect connected clients, used
    memory, and uptime statistics.

    Returns:
        A dictionary with keys ``status``, ``connected_clients``,
        ``used_memory_human``, ``uptime_seconds``, and ``version``.
    """
    try:
        redis_client = get_redis()
        info: Dict[str, Any] = redis_client.info()

        return {
            "status": "connected",
            "connected_clients": info.get("connected_clients", 0),
            "used_memory_human": info.get("used_memory_human", "0B"),
            "used_memory_bytes": info.get("used_memory", 0),
            "uptime_seconds": info.get("uptime_in_seconds", 0),
            "version": info.get("redis_version", "unknown"),
        }
    except Exception as exc:
        logger.error(
            "redis_status_check_failed",
            error=str(exc),
            error_type=type(exc).__name__,
        )
        return {
            "status": "disconnected",
            "connected_clients": 0,
            "used_memory_human": "0B",
            "used_memory_bytes": 0,
            "uptime_seconds": 0,
            "version": None,
            "error": str(exc),
        }


def _calculate_uptime_seconds() -> float:
    """Calculate the API Gateway uptime in seconds since module load.

    Returns:
        Elapsed time in seconds since :data:`_SERVICE_START_TIME`.
    """
    delta = datetime.datetime.utcnow() - _SERVICE_START_TIME
    return round(delta.total_seconds(), 2)


# ===========================================================================
# Route: GET /metrics
# ===========================================================================


@monitoring_bp.route("/metrics", methods=["GET"])
@jwt_required()
def prometheus_metrics() -> Response:
    """Prometheus scrape endpoint exposing all registered metrics.

    Serialises the global Prometheus registry into OpenMetrics / text
    exposition format.  The response includes both the API Gateway-specific
    metrics defined in this module (``REQUEST_COUNT``, ``REQUEST_LATENCY``,
    ``ACTIVE_REQUESTS``, ``ERROR_COUNT``, ``GENERATION_JOBS_CREATED``,
    ``SERVICE_INFO``) and any shared platform metrics registered by the
    observability layer.

    Returns:
        A :class:`flask.Response` with:
            - **Body**: Prometheus text exposition payload.
            - **Content-Type**: ``text/plain; version=0.0.4; charset=utf-8``
              (the Prometheus standard media type).
            - **Status**: 200 OK.

    Security:
        Requires a valid JWT Bearer token (``@jwt_required()``).

    Example::

        GET /api/v1/monitoring/metrics
        Authorization: Bearer <jwt>

        # HELP api_gateway_requests_total Total HTTP requests ...
        # TYPE api_gateway_requests_total counter
        api_gateway_requests_total{method="GET",endpoint="/api/v1/generation/jobs",status_code="200"} 42.0
    """
    logger.debug(
        "prometheus_metrics_scraped",
        remote_addr=request.remote_addr,
        user_agent=request.headers.get("User-Agent", ""),
    )

    # Update service info with current app configuration if available.
    try:
        SERVICE_INFO.info(
            {
                "version": current_app.config.get("APP_VERSION", "1.0.0"),
                "service": "api-gateway",
                "environment": current_app.config.get("FLASK_ENV", "production"),
                "framework": "flask",
                "python_version": "3.12",
            }
        )
    except Exception:
        pass  # Non-critical — INFO metric is best-effort.

    metrics_output: bytes = generate_latest(REGISTRY)
    return Response(
        metrics_output,
        status=200,
        content_type=CONTENT_TYPE_LATEST,
    )


# ===========================================================================
# Route: GET /status
# ===========================================================================


@monitoring_bp.route("/status", methods=["GET"])
@jwt_required()
@require_permissions("admin:system")
def system_status() -> tuple[Response, int]:
    """System-wide status dashboard data.

    Aggregates health, resource utilisation, and availability information
    from every layer of the platform:

    - **API Gateway** — version, uptime, approximate request count and
      error rate derived from Prometheus counters.
    - **MongoDB** — connection pool statistics, server version, database
      name, and reachability.
    - **Redis** — connected clients, memory utilisation, uptime, and
      version.
    - **Downstream services** — health status of all five microservices
      (Generation Engine, Profiling, Quality, Compliance, Provisioning).

    Returns:
        A JSON response with a top-level ``status`` key (``"healthy"`` if
        all components are reachable, ``"degraded"`` otherwise) and nested
        objects for each subsystem.  HTTP 200 is always returned — the
        ``status`` field within the body conveys the overall health.

    Security:
        Requires a valid JWT and the ``admin:system`` permission.

    Example::

        GET /api/v1/monitoring/status
        Authorization: Bearer <jwt>

        {
            "status": "healthy",
            "timestamp": "2025-01-15T10:30:00Z",
            "api_gateway": { ... },
            "mongodb": { ... },
            "redis": { ... },
            "downstream_services": { ... }
        }
    """
    logger.info(
        "system_status_requested",
        remote_addr=request.remote_addr,
    )

    # --- API Gateway status ---
    uptime_seconds: float = _calculate_uptime_seconds()
    app_version: str = current_app.config.get("APP_VERSION", "1.0.0")
    environment: str = current_app.config.get("FLASK_ENV", "production")

    # Attempt to read approximate request/error counts from shared metrics.
    total_requests: float = 0.0
    total_errors: float = 0.0
    try:
        if HTTP_REQUEST_TOTAL is not None:
            # Sum across all label combinations for the api-gateway service.
            total_requests = sum(
                sample.value
                for metric in HTTP_REQUEST_TOTAL.collect()
                for sample in metric.samples
                if sample.labels.get("service") == "api-gateway"
            )
    except Exception:
        pass

    try:
        if ERROR_COUNTER is not None:
            total_errors = sum(
                sample.value
                for metric in ERROR_COUNTER.collect()
                for sample in metric.samples
                if sample.labels.get("service") == "api-gateway"
            )
    except Exception:
        pass

    error_rate: float = round(
        (total_errors / total_requests * 100) if total_requests > 0 else 0.0,
        2,
    )

    api_gateway_status: Dict[str, Any] = {
        "version": app_version,
        "environment": environment,
        "uptime_seconds": uptime_seconds,
        "start_time": _SERVICE_START_TIME.isoformat() + "Z",
        "request_count": total_requests,
        "error_count": total_errors,
        "error_rate_percent": error_rate,
    }

    # --- MongoDB status ---
    mongodb_status: Dict[str, Any] = _get_mongodb_status()

    # --- Redis status ---
    redis_status: Dict[str, Any] = _get_redis_status()

    # --- Downstream services ---
    downstream_results: Dict[str, Dict[str, Any]] = {}
    for svc in _DOWNSTREAM_SERVICES:
        result: Dict[str, Any] = _safe_check_service(svc)
        downstream_results[svc["name"]] = {
            "status": result["status"],
            "latency_ms": result["latency_ms"],
            "detail": result.get("detail"),
        }

    # --- Determine overall platform health ---
    all_healthy: bool = (
        mongodb_status.get("status") == "connected"
        and redis_status.get("status") == "connected"
        and all(
            v.get("status") == "healthy"
            for v in downstream_results.values()
        )
    )

    overall_status: str = "healthy" if all_healthy else "degraded"

    response_body: Dict[str, Any] = {
        "status": overall_status,
        "timestamp": datetime.datetime.utcnow().isoformat() + "Z",
        "api_gateway": api_gateway_status,
        "mongodb": mongodb_status,
        "redis": redis_status,
        "downstream_services": downstream_results,
    }

    logger.info(
        "system_status_collected",
        overall_status=overall_status,
        mongodb_status=mongodb_status.get("status"),
        redis_status=redis_status.get("status"),
    )

    return jsonify(response_body), 200


# ===========================================================================
# Route: GET /services
# ===========================================================================


@monitoring_bp.route("/services", methods=["GET"])
@jwt_required()
@require_permissions("admin:system")
def service_health() -> tuple[Response, int]:
    """Downstream microservice health status matrix.

    Probes the ``/health`` endpoint of every downstream microservice and
    returns an array of health check results.  Each entry includes the
    service name, URL, reachability status, round-trip latency, and any
    additional detail from the health response body.

    Resilience:
        Each health check call is protected by a circuit breaker
        (:func:`~shared.middleware.circuit_breaker.circuit_breaker_decorator`)
        so that a persistently unresponsive service does not block the
        monitoring endpoint.

    Returns:
        A JSON response containing:
            - ``services``: A list of service health result objects.
            - ``checked_at``: ISO 8601 timestamp of the check.
            - ``healthy_count`` / ``unhealthy_count``: Summary counts.
        HTTP 200 is always returned; individual service failures are
        reflected in each service's ``status`` field.

    Security:
        Requires a valid JWT and the ``admin:system`` permission.

    Example::

        GET /api/v1/monitoring/services
        Authorization: Bearer <jwt>

        {
            "services": [
                {
                    "name": "generation-engine",
                    "display_name": "Generation Engine",
                    "url": "http://generation-engine:5001/health",
                    "status": "healthy",
                    "latency_ms": 12.34,
                    "status_code": 200,
                    "detail": "ok"
                },
                ...
            ],
            "checked_at": "2025-01-15T10:30:00Z",
            "healthy_count": 4,
            "unhealthy_count": 1
        }
    """
    logger.info(
        "service_health_check_requested",
        remote_addr=request.remote_addr,
    )

    services_results: List[Dict[str, Any]] = []

    for svc in _DOWNSTREAM_SERVICES:
        result: Dict[str, Any] = _safe_check_service(svc)
        services_results.append(result)

    healthy_count: int = sum(
        1 for s in services_results if s["status"] == "healthy"
    )
    unhealthy_count: int = len(services_results) - healthy_count

    response_body: Dict[str, Any] = {
        "services": services_results,
        "checked_at": datetime.datetime.utcnow().isoformat() + "Z",
        "healthy_count": healthy_count,
        "unhealthy_count": unhealthy_count,
    }

    logger.info(
        "service_health_check_completed",
        healthy_count=healthy_count,
        unhealthy_count=unhealthy_count,
        total_services=len(services_results),
    )

    return jsonify(response_body), 200
