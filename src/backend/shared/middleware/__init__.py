"""Shared middleware package for the Synthetic ERP Data Generation Platform.

Provides cross-cutting Flask middleware components consumed by all six backend
microservices (API Gateway, Generation Engine, Profiling Service, Quality
Service, Compliance Service, Provisioning Service):

- **Circuit Breaker** — Resilience pattern implementation that prevents
  cascade failures when external dependencies (Auth0, ERP systems, target
  databases, cloud storage) are unavailable.  Provides configurable failure
  thresholds, recovery timeouts, and exponential backoff retry logic.

- **Health Check** — Standardised ``/health`` (liveness) and ``/ready``
  (readiness) Flask Blueprint endpoints for Kubernetes probe integration.
  Supports dynamic registration of service-specific dependency checks.

These middleware components are registered in every Flask Application Factory
to ensure consistent resilience patterns and operational health monitoring
across the entire platform.

Typical usage::

    from shared.middleware import (
        circuit_breaker_decorator,
        health_blueprint,
        init_health_checks,
    )
"""

from __future__ import annotations

# ---------------------------------------------------------------------------
# Circuit breaker re-exports
# ---------------------------------------------------------------------------
from shared.middleware.circuit_breaker import (
    CircuitBreakerError,
    CircuitBreakerRegistry,
    ServiceCircuitBreaker,
    circuit_breaker_decorator,
    create_circuit_breaker,
    handle_circuit_breaker_error,
    with_circuit_breaker,
)

# ---------------------------------------------------------------------------
# Health check re-exports
# ---------------------------------------------------------------------------
from shared.middleware.health_check import (
    clear_health_checks,
    health_blueprint,
    init_health_checks,
    register_health_check,
)

__all__: list[str] = [
    # Circuit breaker
    "ServiceCircuitBreaker",
    "create_circuit_breaker",
    "circuit_breaker_decorator",
    "CircuitBreakerRegistry",
    "CircuitBreakerError",
    "with_circuit_breaker",
    "handle_circuit_breaker_error",
    # Health check
    "health_blueprint",
    "register_health_check",
    "init_health_checks",
    "clear_health_checks",
]
