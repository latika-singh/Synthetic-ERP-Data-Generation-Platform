"""Flask Blueprint module for health and readiness probe REST endpoints.

Implements Kubernetes-compatible health probes for the API Gateway service:

- **GET /health** — Liveness probe. Returns a simple status response to confirm
  the process is running. Does NOT check downstream dependencies. Kubernetes uses
  this probe to determine whether to restart the pod. No JWT required.

- **GET /ready** — Readiness probe. Verifies connectivity to critical dependencies
  (MongoDB 7.0, Redis 7.x) by issuing lightweight ping commands and measuring
  response times. Kubernetes uses this probe to determine whether to route traffic
  to the pod. No JWT required.

- **GET /health/detailed** — Detailed health endpoint combining liveness,
  readiness, and runtime diagnostics (version, environment, uptime, memory usage,
  dependency statuses). Intended for debugging and monitoring dashboards.

All endpoints are exempt from JWT authentication middleware to ensure Kubernetes
probes succeed without bearer tokens. The ``/health`` endpoint deliberately avoids
structured logging to prevent log flooding from high-frequency liveness probes
(typically every 10-30 seconds per pod).

Design Decisions:
    - ``/health`` is a pure liveness check: if the process can respond, it is
      alive.  Checking dependencies here would couple liveness to external
      systems, causing unnecessary pod restarts during transient dependency
      outages.
    - ``/ready`` checks all critical dependencies: MongoDB and Redis.  If either
      is unreachable, the pod is marked as not-ready and Kubernetes stops routing
      traffic to it (without restarting the pod).
    - Response times are measured for each dependency check to provide
      observability into dependency latency via the probe response.

Example Kubernetes probe configuration::

    livenessProbe:
      httpGet:
        path: /health
        port: 8080
      initialDelaySeconds: 5
      periodSeconds: 10

    readinessProbe:
      httpGet:
        path: /ready
        port: 8080
      initialDelaySeconds: 10
      periodSeconds: 15
"""

from __future__ import annotations

import os
import resource
import time
from datetime import UTC, datetime
from typing import Any

from flask import Blueprint, Response, current_app, jsonify

from api_gateway.extensions import get_db, get_redis
from shared.logging.structured_logger import get_logger


# ---------------------------------------------------------------------------
# Module-level logger — pre-configured structlog BoundLogger for structured
# JSON logging.  Used ONLY for /ready failures and /health/detailed errors,
# NOT for the /health liveness probe (which is called too frequently).
# ---------------------------------------------------------------------------
logger = get_logger(__name__)

# ---------------------------------------------------------------------------
# Blueprint registration — all health probe routes are mounted under this
# Blueprint.  The application factory registers it without a URL prefix so
# that probes respond at /health, /ready, /health/detailed exactly.
# ---------------------------------------------------------------------------
health_bp: Blueprint = Blueprint("health", __name__)

# ---------------------------------------------------------------------------
# Module-level startup timestamp — recorded once when the module is first
# imported (which occurs during application factory initialisation).  Used
# to compute ``uptime_seconds`` in the /health and /health/detailed responses.
# ---------------------------------------------------------------------------
_start_time: float = time.time()

# ---------------------------------------------------------------------------
# Service constants — centralised to avoid magic strings throughout the
# module and to allow easy updates during version bumps.
# ---------------------------------------------------------------------------
_SERVICE_NAME: str = "api-gateway"
_SERVICE_VERSION: str = "1.0.0"


# ===================================================================
# Private helper functions
# ===================================================================


def _check_mongodb() -> dict[str, Any]:
    """Check MongoDB connectivity by issuing a lightweight ``ping`` command.

    Uses the :func:`~api_gateway.extensions.get_db` helper to obtain the
    PyMongo ``Database`` instance, then calls ``db.command('ping')`` which
    is the standard MongoDB healthcheck command (returns ``{'ok': 1.0}``
    on success).

    The round-trip time is measured using :func:`time.time` and reported in
    milliseconds for observability.

    Returns:
        Dict containing:
            - ``status`` (str): ``"healthy"`` or ``"unhealthy"``.
            - ``response_time_ms`` (float): Round-trip time in milliseconds.
            - ``details`` (str | None): Error message if unhealthy,
              ``None`` if healthy.
    """
    start: float = time.time()
    try:
        db = get_db()
        result = db.command("ping")
        elapsed_ms: float = round((time.time() - start) * 1000, 2)

        if result.get("ok") == 1.0:
            return {
                "status": "healthy",
                "response_time_ms": elapsed_ms,
                "details": None,
            }
        return {
            "status": "unhealthy",
            "response_time_ms": elapsed_ms,
            "details": f"Unexpected ping response: {result}",
        }
    except Exception as exc:
        elapsed_ms = round((time.time() - start) * 1000, 2)
        return {
            "status": "unhealthy",
            "response_time_ms": elapsed_ms,
            "details": f"{type(exc).__name__}: {exc}",
        }


def _check_redis() -> dict[str, Any]:
    """Check Redis connectivity by issuing a lightweight ``PING`` command.

    Uses the :func:`~api_gateway.extensions.get_redis` helper to obtain the
    ``redis.Redis`` client instance, then calls ``redis.ping()`` which
    returns ``True`` on success.

    The round-trip time is measured using :func:`time.time` and reported in
    milliseconds for observability.

    Returns:
        Dict containing:
            - ``status`` (str): ``"healthy"`` or ``"unhealthy"``.
            - ``response_time_ms`` (float): Round-trip time in milliseconds.
            - ``details`` (str | None): Error message if unhealthy,
              ``None`` if healthy.
    """
    start: float = time.time()
    try:
        redis_client = get_redis()
        pong = bool(redis_client.ping())
        elapsed_ms: float = round((time.time() - start) * 1000, 2)

        if pong:
            return {
                "status": "healthy",
                "response_time_ms": elapsed_ms,
                "details": None,
            }
        return {
            "status": "unhealthy",
            "response_time_ms": elapsed_ms,
            "details": "Redis PING returned False",
        }
    except Exception as exc:
        elapsed_ms = round((time.time() - start) * 1000, 2)
        return {
            "status": "unhealthy",
            "response_time_ms": elapsed_ms,
            "details": f"{type(exc).__name__}: {exc}",
        }


def _get_memory_usage_mb() -> float:
    """Return the current process memory usage in megabytes.

    Uses ``resource.getrusage(resource.RUSAGE_SELF).ru_maxrss`` which
    returns kilobytes on Linux (the primary deployment target).  The value
    is converted to megabytes and rounded to two decimal places.

    Returns:
        Current resident set size (RSS) in megabytes.
    """
    usage_kb: int = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
    # On Linux, ru_maxrss is in kilobytes; on macOS it is in bytes.
    # Normalise to megabytes assuming Linux (Kubernetes deployment target).
    return round(usage_kb / 1024, 2)


# ===================================================================
# Route handlers
# ===================================================================


@health_bp.route("/health", methods=["GET"])
def health_check() -> tuple[Response, int]:
    """Kubernetes liveness probe endpoint.

    Returns a lightweight JSON response confirming that the API Gateway
    process is alive and responsive.  This endpoint performs **no
    dependency checks** — it only verifies that the Python/Flask process
    can accept and respond to HTTP requests.

    Kubernetes uses this probe to decide whether to restart the pod.  By
    keeping this endpoint free of external dependency checks, transient
    MongoDB or Redis outages will not trigger unnecessary pod restarts.

    **Authentication:** None — this endpoint is exempt from JWT
    middleware.  Kubernetes probes do not carry authorisation tokens.

    **Logging:** Deliberately suppressed to avoid flooding structured
    logs from high-frequency probe calls (typically every 10-30 s).

    Returns:
        Tuple of (JSON response, HTTP 200):
            - ``status`` (str): Always ``"healthy"`` if the process responds.
            - ``service`` (str): Service identifier (``"api-gateway"``).
            - ``version`` (str): Service version (``"1.0.0"``).
            - ``timestamp`` (str): Current UTC time in ISO 8601 format.
            - ``uptime_seconds`` (float): Seconds since service startup.

    Example Response::

        HTTP/1.1 200 OK
        Content-Type: application/json

        {
            "status": "healthy",
            "service": "api-gateway",
            "version": "1.0.0",
            "timestamp": "2025-01-15T10:30:00.000000+00:00",
            "uptime_seconds": 3600.5
        }
    """
    now: datetime = datetime.now(UTC)
    uptime: float = round(time.time() - _start_time, 2)

    return jsonify({
        "status": "healthy",
        "service": _SERVICE_NAME,
        "version": _SERVICE_VERSION,
        "timestamp": now.isoformat(),
        "uptime_seconds": uptime,
    }), 200


@health_bp.route("/ready", methods=["GET"])
def readiness_check() -> tuple[Response, int]:
    """Kubernetes readiness probe endpoint.

    Verifies that the API Gateway can successfully communicate with its
    critical runtime dependencies:

    - **MongoDB 7.0** — via ``db.command('ping')``
    - **Redis 7.x** — via ``redis.ping()``

    If **all** dependencies respond successfully, the pod is marked as
    ready and Kubernetes routes traffic to it.  If **any** dependency is
    unreachable, the pod is marked as not-ready and Kubernetes stops
    sending traffic (without restarting the pod).

    **Authentication:** None — this endpoint is exempt from JWT
    middleware.  Kubernetes probes do not carry authorisation tokens.

    **Logging:** Failures are logged at ``warning`` level for alerting.
    Successful checks are NOT logged to avoid noise from frequent probes.

    Returns:
        Tuple of (JSON response, HTTP status code):
            HTTP 200 — All dependencies healthy.
            HTTP 503 — One or more dependencies unhealthy.

    Response Body:
        - ``status`` (str): ``"ready"`` or ``"not_ready"``.
        - ``service`` (str): Service identifier (``"api-gateway"``).
        - ``timestamp`` (str): Current UTC time in ISO 8601 format.
        - ``checks`` (dict): Per-dependency health check results with
          ``status``, ``response_time_ms``, and ``details`` fields.

    Example Response (healthy)::

        HTTP/1.1 200 OK
        Content-Type: application/json

        {
            "status": "ready",
            "service": "api-gateway",
            "timestamp": "2025-01-15T10:30:00.000000+00:00",
            "checks": {
                "mongodb": {
                    "status": "healthy",
                    "response_time_ms": 2.45,
                    "details": null
                },
                "redis": {
                    "status": "healthy",
                    "response_time_ms": 0.87,
                    "details": null
                }
            }
        }

    Example Response (unhealthy)::

        HTTP/1.1 503 Service Unavailable
        Content-Type: application/json

        {
            "status": "not_ready",
            "service": "api-gateway",
            "timestamp": "2025-01-15T10:30:00.000000+00:00",
            "checks": {
                "mongodb": {
                    "status": "unhealthy",
                    "response_time_ms": 5001.2,
                    "details": "ConnectionFailure: ..."
                },
                "redis": {
                    "status": "healthy",
                    "response_time_ms": 0.91,
                    "details": null
                }
            }
        }
    """
    now: datetime = datetime.now(UTC)

    # Run dependency health checks in sequence (not parallel) to keep the
    # implementation simple and avoid thread-pool overhead for two lightweight
    # ping operations that should each complete in < 50 ms.
    mongodb_check: dict[str, Any] = _check_mongodb()
    redis_check: dict[str, Any] = _check_redis()

    # Determine overall readiness — all dependencies must be healthy.
    all_healthy: bool = (
        mongodb_check["status"] == "healthy"
        and redis_check["status"] == "healthy"
    )

    status: str = "ready" if all_healthy else "not_ready"
    http_status: int = 200 if all_healthy else 503

    # Log only failures for alerting — do NOT log successes (too frequent).
    if not all_healthy:
        logger.warning(
            "readiness_check_failed",
            mongodb_status=mongodb_check["status"],
            mongodb_details=mongodb_check.get("details"),
            mongodb_response_time_ms=mongodb_check.get("response_time_ms"),
            redis_status=redis_check["status"],
            redis_details=redis_check.get("details"),
            redis_response_time_ms=redis_check.get("response_time_ms"),
        )

    return jsonify({
        "status": status,
        "service": _SERVICE_NAME,
        "timestamp": now.isoformat(),
        "checks": {
            "mongodb": mongodb_check,
            "redis": redis_check,
        },
    }), http_status


@health_bp.route("/health/detailed", methods=["GET"])
def detailed_health() -> tuple[Response, int]:
    """Detailed health endpoint combining liveness, readiness, and diagnostics.

    Provides a comprehensive view of the API Gateway's operational state,
    including runtime information (version, environment, uptime), dependency
    health statuses (MongoDB, Redis), and process diagnostics (memory usage).

    This endpoint is intended for debugging and monitoring dashboards — it
    is **not** used by Kubernetes probes.  Response times may be slightly
    longer than ``/health`` or ``/ready`` due to additional information
    gathering.

    **Authentication:** None — kept unauthenticated for operational tooling
    access.  Sensitive configuration details are excluded from the response.

    Returns:
        Tuple of (JSON response, HTTP status code):
            HTTP 200 — Service operational (dependencies healthy).
            HTTP 503 — One or more critical dependencies unhealthy.

    Response Body:
        - ``status`` (str): ``"healthy"`` or ``"degraded"``.
        - ``service`` (str): Service identifier (``"api-gateway"``).
        - ``version`` (str): Service version (``"1.0.0"``).
        - ``environment`` (str): Current Flask environment name.
        - ``timestamp`` (str): Current UTC time in ISO 8601 format.
        - ``uptime_seconds`` (float): Seconds since service startup.
        - ``memory_usage_mb`` (float): Current RSS memory in megabytes.
        - ``checks`` (dict): Per-dependency health check results.

    Example Response::

        HTTP/1.1 200 OK
        Content-Type: application/json

        {
            "status": "healthy",
            "service": "api-gateway",
            "version": "1.0.0",
            "environment": "production",
            "timestamp": "2025-01-15T10:30:00.000000+00:00",
            "uptime_seconds": 86400.5,
            "memory_usage_mb": 128.45,
            "checks": {
                "mongodb": {
                    "status": "healthy",
                    "response_time_ms": 1.23,
                    "details": null
                },
                "redis": {
                    "status": "healthy",
                    "response_time_ms": 0.56,
                    "details": null
                }
            }
        }
    """
    now: datetime = datetime.now(UTC)
    uptime: float = round(time.time() - _start_time, 2)

    # Gather dependency health — reuse the same check functions as /ready.
    mongodb_check: dict[str, Any] = _check_mongodb()
    redis_check: dict[str, Any] = _check_redis()

    # Determine overall status: "healthy" if all deps OK, "degraded" otherwise.
    all_healthy: bool = (
        mongodb_check["status"] == "healthy"
        and redis_check["status"] == "healthy"
    )
    status: str = "healthy" if all_healthy else "degraded"
    http_status: int = 200 if all_healthy else 503

    # Read the Flask environment from app config — gracefully default when
    # accessed outside a request context (should not happen for this route,
    # but defensively coded).
    try:
        environment: str = current_app.config.get(
            "ENV",
            os.environ.get("FLASK_ENV", "production"),
        )
    except RuntimeError:
        environment = os.environ.get("FLASK_ENV", "production")

    # Gather process-level diagnostics.
    memory_mb: float = _get_memory_usage_mb()

    # Log degraded state for alerting.
    if not all_healthy:
        logger.warning(
            "detailed_health_check_degraded",
            mongodb_status=mongodb_check["status"],
            mongodb_details=mongodb_check.get("details"),
            redis_status=redis_check["status"],
            redis_details=redis_check.get("details"),
            memory_usage_mb=memory_mb,
            uptime_seconds=uptime,
        )

    return jsonify({
        "status": status,
        "service": _SERVICE_NAME,
        "version": _SERVICE_VERSION,
        "environment": environment,
        "timestamp": now.isoformat(),
        "uptime_seconds": uptime,
        "memory_usage_mb": memory_mb,
        "checks": {
            "mongodb": mongodb_check,
            "redis": redis_check,
        },
    }), http_status
