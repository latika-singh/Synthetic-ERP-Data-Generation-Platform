"""Standardized health and readiness check endpoints for the Synthetic ERP Platform.

Provides a Flask Blueprint with ``/health`` (liveness) and ``/ready`` (readiness)
endpoints used by Kubernetes probes across all six backend microservices:

- **API Gateway**
- **Generation Engine**
- **Profiling Service**
- **Quality Service**
- **Compliance Service**
- **Provisioning Service**

The ``/health`` endpoint is a lightweight liveness probe that confirms the Flask
process is running — no external dependency checks are performed.

The ``/ready`` endpoint is a deep readiness probe that verifies MongoDB
connectivity, Redis availability, and any service-specific custom checks
registered via :func:`register_health_check`.  Results include per-component
status, latency measurements, and an aggregate overall status.

Service-specific checks can be registered dynamically, allowing each
microservice to extend the readiness probe with its own dependency
verification (e.g., ERP connector health, ML model readiness, cloud storage
connectivity).

Overall status logic:
    - ``"healthy"``: All dependency checks (critical and custom) report healthy.
    - ``"degraded"``: At least one *non-critical* (custom) check is unhealthy,
      but both MongoDB and Redis are healthy.
    - ``"unhealthy"``: MongoDB **or** Redis (critical dependencies) is unhealthy.

HTTP response codes:
    - ``200 OK``: healthy or degraded.
    - ``503 Service Unavailable``: unhealthy.

Usage::

    # In a service's create_app():
    from shared.middleware.health_check import init_health_checks

    def create_app() -> Flask:
        app = Flask(__name__)
        init_health_checks(app)
        return app

    # Registering a custom readiness check:
    from shared.middleware.health_check import register_health_check

    def check_ml_model() -> dict:
        return {"status": "healthy", "latency_ms": 0.5, "model": "gan-v2"}

    register_health_check("ml_model", check_ml_model)
"""

from __future__ import annotations

import time
from typing import Any, Callable, Optional

from flask import Blueprint, Flask, current_app, jsonify

from shared.database.mongodb import check_mongo_health
from shared.database.redis_client import check_redis_health
from shared.logging.structured_logger import get_logger


# ---------------------------------------------------------------------------
# Module-level logger — uses the shared structured logger to emit JSON-
# formatted log events that include correlation IDs and service metadata.
# ---------------------------------------------------------------------------
logger = get_logger(__name__)

# ---------------------------------------------------------------------------
# Critical dependency names — used to differentiate between critical
# infrastructure components (whose failure triggers "unhealthy") and
# non-critical custom checks (whose failure triggers "degraded").
# ---------------------------------------------------------------------------
_CRITICAL_DEPENDENCIES: frozenset[str] = frozenset({"mongodb", "redis"})

# ---------------------------------------------------------------------------
# Custom health check registry — service-specific checks are appended here
# via register_health_check().  Each entry is a (name, check_function)
# tuple where check_function() returns a dict with at minimum:
#   {"status": "healthy" | "unhealthy", "latency_ms": float}
# ---------------------------------------------------------------------------
_custom_checks: list[tuple[str, Callable[[], dict[str, Any]]]] = []


# ============================================================================
# Registry management functions
# ============================================================================


def register_health_check(name: str, check_fn: Callable[[], dict[str, Any]]) -> None:
    """Register a custom dependency health check for the readiness probe.

    Individual microservices call this function during application
    initialisation to add service-specific readiness criteria.  Registered
    checks are executed sequentially by the ``/ready`` endpoint alongside
    the built-in MongoDB and Redis checks.

    Args:
        name: A short, descriptive identifier for the check (e.g.
            ``"erp_connector"``, ``"ml_model"``, ``"s3_storage"``).
            Must be unique; duplicate names will result in both checks
            running (the last registration wins for display purposes in
            the response, but all are executed).
        check_fn: A callable accepting no arguments and returning a
            ``dict`` with at minimum the keys:

            - ``status`` (str): ``"healthy"`` or ``"unhealthy"``.
            - ``latency_ms`` (float): Duration of the check in ms.

            Additional keys are passed through to the readiness response.

    Raises:
        TypeError: If *check_fn* is not callable.
        ValueError: If *name* is empty or not a string.

    Example::

        def check_sap_connector() -> dict:
            start = time.time()
            ok = sap_client.ping()
            return {
                "status": "healthy" if ok else "unhealthy",
                "latency_ms": (time.time() - start) * 1000,
            }

        register_health_check("sap_connector", check_sap_connector)
    """
    if not isinstance(name, str) or not name.strip():
        raise ValueError(
            f"Health check name must be a non-empty string, got: {name!r}"
        )
    if not callable(check_fn):
        raise TypeError(
            f"Health check function must be callable, got: {type(check_fn).__name__}"
        )

    _custom_checks.append((name.strip(), check_fn))
    logger.info(
        "custom_health_check_registered",
        check_name=name.strip(),
        total_custom_checks=len(_custom_checks),
    )


def clear_health_checks() -> None:
    """Remove all registered custom health checks.

    Primarily intended for use in test suites to reset state between test
    cases.  Does **not** affect the built-in MongoDB and Redis checks
    which are always executed by the ``/ready`` endpoint.

    Example::

        # In a pytest fixture
        from shared.middleware.health_check import clear_health_checks

        @pytest.fixture(autouse=True)
        def reset_health_checks():
            clear_health_checks()
            yield
            clear_health_checks()
    """
    _custom_checks.clear()
    logger.debug("custom_health_checks_cleared")


# ============================================================================
# Flask Blueprint
# ============================================================================

health_blueprint: Blueprint = Blueprint("health", __name__)
"""Flask Blueprint exposing ``/health`` and ``/ready`` endpoints.

Register in any Flask Application Factory via::

    app.register_blueprint(health_blueprint)
"""


@health_blueprint.route("/health", methods=["GET"])
def liveness() -> tuple:
    """Kubernetes liveness probe endpoint.

    Returns a minimal JSON payload confirming the Flask process is alive
    and able to handle HTTP requests.  No external dependency checks are
    performed — this endpoint is intentionally lightweight to avoid false
    negatives caused by transient dependency outages.

    Returns:
        A ``(response, status_code)`` tuple with HTTP 200.

    Response JSON::

        {
            "status": "healthy",
            "service": "<SERVICE_NAME from app config>"
        }
    """
    service_name: str = current_app.config.get("SERVICE_NAME", "unknown")
    return jsonify({"status": "healthy", "service": service_name}), 200


@health_blueprint.route("/ready", methods=["GET"])
def readiness() -> tuple:
    """Kubernetes readiness probe endpoint with deep dependency checks.

    Sequentially verifies connectivity to all critical and custom
    dependencies:

    1. **MongoDB** — via :func:`~shared.database.mongodb.check_mongo_health`
    2. **Redis** — via :func:`~shared.database.redis_client.check_redis_health`
    3. **Custom checks** — all checks registered via
       :func:`register_health_check`

    Aggregate status logic:
        - ``"healthy"`` — every check reports ``"healthy"``.
        - ``"degraded"`` — at least one *non-critical* (custom) check is
          unhealthy while both MongoDB and Redis remain healthy.
        - ``"unhealthy"`` — MongoDB **or** Redis reports unhealthy.

    Returns:
        A ``(response, status_code)`` tuple:
        - HTTP 200 for ``"healthy"`` or ``"degraded"``
        - HTTP 503 for ``"unhealthy"``

    Response JSON::

        {
            "status": "healthy",
            "service": "api-gateway",
            "checks": {
                "mongodb": {"status": "healthy", "latency_ms": 2.5, ...},
                "redis": {"status": "healthy", "latency_ms": 1.2, ...},
                "<custom>": {"status": "healthy", "latency_ms": 0.8, ...}
            },
            "total_latency_ms": 4.5
        }
    """
    service_name: str = current_app.config.get("SERVICE_NAME", "unknown")
    checks: dict[str, dict[str, Any]] = {}
    overall_start: float = time.time()

    # ------------------------------------------------------------------
    # 1. MongoDB — critical dependency
    # ------------------------------------------------------------------
    checks["mongodb"] = _execute_check("mongodb", check_mongo_health)

    # ------------------------------------------------------------------
    # 2. Redis — critical dependency
    # ------------------------------------------------------------------
    checks["redis"] = _execute_check("redis", check_redis_health)

    # ------------------------------------------------------------------
    # 3. Custom (service-specific) checks — non-critical dependencies
    # ------------------------------------------------------------------
    for check_name, check_fn in _custom_checks:
        checks[check_name] = _execute_check(check_name, check_fn)

    # ------------------------------------------------------------------
    # Compute total latency and aggregate status
    # ------------------------------------------------------------------
    total_latency_ms: float = round((time.time() - overall_start) * 1000.0, 2)

    overall_status: str = _determine_overall_status(checks)

    response_body: dict[str, Any] = {
        "status": overall_status,
        "service": service_name,
        "checks": checks,
        "total_latency_ms": total_latency_ms,
    }

    # Choose HTTP status code based on aggregate health.
    http_status: int = 200 if overall_status in ("healthy", "degraded") else 503

    logger.info(
        "readiness_check_completed",
        status=overall_status,
        service=service_name,
        total_latency_ms=total_latency_ms,
        http_status=http_status,
        check_count=len(checks),
    )

    return jsonify(response_body), http_status


# ============================================================================
# Convenience initialisation function
# ============================================================================


def init_health_checks(app: Flask, url_prefix: str | None = None) -> None:
    """Register the health blueprint and perform optional setup on a Flask app.

    This is the recommended single entry-point for health check
    initialisation inside each service's ``create_app()`` factory.

    Args:
        app: The Flask application instance to register the blueprint on.
        url_prefix: Optional URL prefix for the health endpoints.  When
            ``None``, endpoints are registered at ``/health`` and ``/ready``
            directly on the application root.  Passing ``"/ops"`` would
            result in ``/ops/health`` and ``/ops/ready``.

    Example::

        def create_app() -> Flask:
            app = Flask(__name__)
            app.config["SERVICE_NAME"] = "generation-engine"
            init_health_checks(app)
            return app
    """
    app.register_blueprint(health_blueprint, url_prefix=url_prefix)

    logger.info(
        "health_checks_initialised",
        service=app.config.get("SERVICE_NAME", "unknown"),
        url_prefix=url_prefix or "/",
        registered_custom_checks=len(_custom_checks),
    )


# ============================================================================
# Internal helper functions
# ============================================================================


def _execute_check(
    name: str,
    check_fn: Callable[[], dict[str, Any]],
) -> dict[str, Any]:
    """Safely execute a single health check and return its result.

    Wraps the execution in a try/except so that a failure in one check
    never prevents subsequent checks from running or crashes the readiness
    endpoint.

    If the *check_fn* raises an exception the result is marked as
    ``"unhealthy"`` with the error details captured in the response dict.

    If the *check_fn* returns a result that does not contain a
    ``latency_ms`` key, the elapsed wall-clock time is measured externally
    and injected.

    Args:
        name: Identifier for the check (used in logging).
        check_fn: Zero-argument callable returning a health-check result
            dict.

    Returns:
        A dict guaranteed to contain at least ``status`` and ``latency_ms``.
    """
    start: float = time.time()
    try:
        result: dict[str, Any] = check_fn()

        # Ensure the result always contains latency_ms.  Some check
        # implementations measure latency internally; if they don't, we
        # calculate it from the wall-clock time around the call.
        if "latency_ms" not in result:
            result["latency_ms"] = round((time.time() - start) * 1000.0, 2)

        # Normalise missing status key to "unhealthy" for safety.
        if "status" not in result:
            result["status"] = "unhealthy"

        return result

    except Exception as exc:
        elapsed_ms: float = round((time.time() - start) * 1000.0, 2)
        logger.error(
            "health_check_exception",
            check_name=name,
            error=str(exc),
            latency_ms=elapsed_ms,
        )
        return {
            "status": "unhealthy",
            "latency_ms": elapsed_ms,
            "error": str(exc),
        }


def _determine_overall_status(checks: dict[str, dict[str, Any]]) -> str:
    """Compute the aggregate health status from individual check results.

    Decision matrix:
        - ``"unhealthy"`` — any *critical* dependency (MongoDB or Redis) is
          unhealthy.
        - ``"degraded"`` — all critical dependencies are healthy but at
          least one non-critical (custom) check is unhealthy.
        - ``"healthy"`` — every check reports ``"healthy"``.

    Args:
        checks: Mapping of check name → result dict.  Each result dict
            must contain a ``"status"`` key.

    Returns:
        One of ``"healthy"``, ``"degraded"``, or ``"unhealthy"``.
    """
    has_critical_failure: bool = False
    has_non_critical_failure: bool = False

    for name, result in checks.items():
        status: str = result.get("status", "unhealthy")
        if status != "healthy":
            if name in _CRITICAL_DEPENDENCIES:
                has_critical_failure = True
            else:
                has_non_critical_failure = True

    if has_critical_failure:
        return "unhealthy"
    if has_non_critical_failure:
        return "degraded"
    return "healthy"
