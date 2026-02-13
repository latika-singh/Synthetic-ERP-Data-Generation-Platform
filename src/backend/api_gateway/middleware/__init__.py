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

from typing import TYPE_CHECKING

import structlog


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
# Re-exports from tenant middleware
# ---------------------------------------------------------------------------
from api_gateway.middleware.tenant import (
    get_tenant_filter,
    register_tenant_middleware,
    require_tenant,
)


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
logger: structlog.stdlib.BoundLogger = structlog.get_logger(__name__)


def register_all_middleware(app: Flask) -> None:
    """Register all middleware on the Flask application in the correct order.

    Middleware registration order is critical:

    1. **Logging middleware** (first) — establishes correlation IDs
       (``X-Request-ID``) and OpenTelemetry trace context so that all
       subsequent middleware and route handlers include consistent
       request tracing information.
    2. **Auth middleware** (second) — JWT validation and user identity
       extraction.  Decoded claims (user_id, email, roles, permissions,
       tenant_id) are stored on Flask's ``g`` object.
    3. **Tenant middleware** (third) — requires the auth context from
       step 2 to resolve the tenant identity from JWT custom claims
       and enforce namespace isolation.
    4. **Rate limiter** (fourth) — requires user roles from auth and
       tenant context for tiered throttling (60/300/1000 req/min).
    5. **Error handlers** (last) — catch-all for errors raised by any
       upstream middleware or route handler, returning structured JSON
       error responses.

    Args:
        app: The Flask application instance to register middleware on.

    Example::

        from api_gateway.middleware import register_all_middleware

        app = Flask(__name__)
        register_all_middleware(app)
    """
    # 1. Logging — must be first for correlation ID propagation
    register_logging_middleware(app)

    # 2. Authentication — JWT validation & user identity
    register_auth_middleware(app)

    # 3. Tenant isolation — requires auth context
    register_tenant_middleware(app)

    # 4. Rate limiting — requires user roles + tenant context
    register_rate_limiter(app)

    # 5. Error handlers — catch-all (registered last)
    register_error_handlers(app)

    logger.info(
        "All middleware registered successfully",
        middleware_order=[
            "logging",
            "auth",
            "tenant",
            "rate_limiter",
            "error_handlers",
        ],
    )
