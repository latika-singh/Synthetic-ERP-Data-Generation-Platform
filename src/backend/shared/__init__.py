"""Shared utilities library for the Synthetic ERP Data Generation Platform.

This package provides cross-cutting infrastructure concerns consumed by all
six backend microservices:

- **config** — Base configuration class implementing 12-factor app methodology
- **database** — MongoDB and Redis connection managers with pooling
- **auth** — JWT validation and role-based access control (RBAC)
- **logging** — Structured JSON logging with correlation ID propagation
- **observability** — Prometheus metrics and OpenTelemetry distributed tracing
- **middleware** — Circuit breaker, health checks, and shared Flask middleware

Convenience imports are provided so that common utilities can be accessed
directly from the ``shared`` namespace::

    from shared import get_logger, get_mongo_client, BaseConfig

Sub-packages that have not yet been installed or created will be silently
skipped — this allows incremental deployment and avoids circular import
errors during the build phase.
"""

from __future__ import annotations


__version__: str = "1.0.0"
"""Semantic version of the shared utilities library."""

# ---------------------------------------------------------------------------
# Public API — convenience re-exports
# ---------------------------------------------------------------------------
# The shared library is composed of several sub-packages that may be created
# independently by different agents.  We use conditional imports so that the
# package remains importable even when some sub-modules are not yet present
# on disk.  This follows the schema directive to "use lazy imports or
# conditional imports where appropriate to avoid circular dependencies."
# ---------------------------------------------------------------------------

__all__: list[str] = ["__version__"]

# --- config ---------------------------------------------------------------
try:
    from shared.config.base import BaseConfig

    __all__.append("BaseConfig")
except ImportError:
    pass

# --- database -------------------------------------------------------------
try:
    from shared.database.mongodb import get_mongo_client, get_mongo_db

    __all__.extend(["get_mongo_client", "get_mongo_db"])
except ImportError:
    pass

try:
    from shared.database.redis_client import get_redis_client

    __all__.append("get_redis_client")
except ImportError:
    pass

# --- auth -----------------------------------------------------------------
try:
    from shared.auth.jwt_handler import validate_token

    __all__.append("validate_token")
except ImportError:
    pass

try:
    from shared.auth.rbac import require_permission, require_role

    __all__.extend(["require_permission", "require_role"])
except ImportError:
    pass

# --- logging --------------------------------------------------------------
try:
    from shared.logging.structured_logger import get_logger

    __all__.append("get_logger")
except ImportError:
    pass

# --- middleware ------------------------------------------------------------
try:
    from shared.middleware.health_check import health_blueprint

    __all__.append("health_blueprint")
except ImportError:
    pass

try:
    from shared.middleware.circuit_breaker import (
        circuit_breaker_decorator,
        create_circuit_breaker,
    )

    __all__.extend(["circuit_breaker_decorator", "create_circuit_breaker"])
except ImportError:
    pass
