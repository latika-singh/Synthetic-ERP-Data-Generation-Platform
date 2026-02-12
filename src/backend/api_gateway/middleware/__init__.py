"""API Gateway middleware package.

Provides cross-cutting request processing middleware for the API Gateway service:
- JWT authentication and authorization via Auth0
- Tiered Redis-backed rate limiting
- Global error handling with circuit breaker pattern
- Structured request/response logging with OpenTelemetry correlation
- Multi-tenant context extraction and namespace isolation

Usage in app.py::

    from api_gateway.middleware import register_all_middleware

    def create_app():
        app = Flask(__name__)
        register_all_middleware(app)
        return app
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING


if TYPE_CHECKING:
    from flask import Flask

# ---------------------------------------------------------------------------
# Re-exports from auth middleware
# ---------------------------------------------------------------------------
from api_gateway.middleware.auth import (
    register_auth_middleware,
    require_permissions,
    require_roles,
)

# ---------------------------------------------------------------------------
# Re-exports from error handler middleware
# ---------------------------------------------------------------------------
from api_gateway.middleware.error_handler import (
    ServiceCircuitBreaker,
    register_error_handlers,
    with_retry,
)

# ---------------------------------------------------------------------------
# Re-exports from logging middleware
# ---------------------------------------------------------------------------
from api_gateway.middleware.logging_middleware import (
    register_logging_middleware,
)

# ---------------------------------------------------------------------------
# Re-exports from rate limiter middleware
# ---------------------------------------------------------------------------
from api_gateway.middleware.rate_limiter import (
    register_rate_limiter,
)


# ---------------------------------------------------------------------------
# Re-exports from tenant middleware (may not yet be available)
# ---------------------------------------------------------------------------
_tenant_middleware_available: bool = False
try:
    from api_gateway.middleware.tenant import (
        get_tenant_filter,
        register_tenant_middleware,
        require_tenant,
    )

    _tenant_middleware_available = True
except ImportError:
    # tenant.py may not yet be deployed; provide graceful degradation
    _tenant_middleware_available = False

    def register_tenant_middleware(app: Flask) -> None:  # type: ignore[misc]
        """Stub — real implementation loads from ``tenant.py``.

        This placeholder is used only when the ``tenant`` middleware module has
        not yet been deployed.  It registers no hooks and logs a warning so that
        the rest of the middleware stack can still initialise.

        Args:
            app: The Flask application instance.
        """
        _logger = logging.getLogger(__name__)
        _logger.warning(
            "Tenant middleware module is not available — skipping registration. "
            "Multi-tenant isolation will NOT be enforced until tenant.py is deployed."
        )

    def require_tenant(fn):  # type: ignore[misc]
        """Pass-through decorator stub when tenant middleware is unavailable."""
        return fn

    def get_tenant_filter() -> dict[str, str]:  # type: ignore[misc]
        """Return an empty filter when tenant middleware is unavailable.

        Returns:
            An empty dict (no tenant scoping applied).
        """
        return {}


# ---------------------------------------------------------------------------
# Public API surface
# ---------------------------------------------------------------------------
__all__ = [
    "ServiceCircuitBreaker",
    "get_tenant_filter",
    "register_all_middleware",
    "register_auth_middleware",
    "register_error_handlers",
    "register_logging_middleware",
    "register_rate_limiter",
    "register_tenant_middleware",
    "require_permissions",
    "require_roles",
    "require_tenant",
    "with_retry",
]

# ---------------------------------------------------------------------------
# Module-level logger
# ---------------------------------------------------------------------------
logger = logging.getLogger(__name__)

# Try to use structlog if available, fall back to stdlib logging
try:
    import structlog

    _log = structlog.get_logger(__name__)
except ImportError:
    _log = logger  # type: ignore[assignment]


def register_all_middleware(app: Flask) -> None:
    """Register all middleware on the Flask application in the correct order.

    Middleware registration order is critical:

    1. **Logging middleware** (first) — establishes correlation IDs (``X-Request-ID``)
       and OpenTelemetry trace context for all subsequent middleware.
    2. **Auth middleware** (second) — JWT validation and user identity extraction.
    3. **Tenant middleware** (third) — requires auth context from step 2 to resolve
       the tenant identity from JWT claims.
    4. **Rate limiter** (fourth) — requires user roles from auth and tenant context
       for tiered throttling.
    5. **Error handlers** (last) — catch-all for errors from all upstream layers.

    Args:
        app: The Flask application instance to register middleware on.

    Example::

        from api_gateway.middleware import register_all_middleware

        app = Flask(__name__)
        register_all_middleware(app)
    """
    # 1. Logging — must be first for correlation ID propagation
    register_logging_middleware(app)
    _log.info("middleware.registered", middleware="logging")

    # 2. Authentication — JWT validation & user identity
    register_auth_middleware(app)
    _log.info("middleware.registered", middleware="auth")

    # 3. Tenant isolation — requires auth context
    register_tenant_middleware(app)
    _log.info(
        "middleware.registered",
        middleware="tenant",
        available=_tenant_middleware_available,
    )

    # 4. Rate limiting — requires user roles + tenant context
    register_rate_limiter(app)
    _log.info("middleware.registered", middleware="rate_limiter")

    # 5. Error handlers — catch-all (registered last)
    register_error_handlers(app)
    _log.info("middleware.registered", middleware="error_handler")

    _log.info(
        "middleware.all_registered",
        message="All middleware registered successfully",
        tenant_isolation=_tenant_middleware_available,
    )
