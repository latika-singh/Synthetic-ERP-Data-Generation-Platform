"""Authentication and authorisation utilities for the Synthetic ERP Data Generation Platform.

This sub-package provides the **shared authentication and authorisation
infrastructure** consumed by all six backend microservices:

*   **API Gateway** — request-level JWT validation and route protection.
*   **Generation Engine** — service-to-service token verification.
*   **Profiling Service** — user-context extraction for audit trails.
*   **Quality Service** — permission gating on report access.
*   **Compliance Service** — role checks for certification workflows.
*   **Provisioning Service** — tenant-scoped export authorisation.

The package surfaces two complementary capabilities:

1. **JWT RS256 token validation** (``jwt_handler`` module)
   Fetches Auth0's JSON Web Key Set (JWKS), validates RS256 signatures,
   extracts claims (``sub``, ``roles``, ``tenant_id``, ``permissions``),
   and provides Flask middleware integration via the :func:`jwt_required`
   decorator.

2. **Role-Based Access Control / RBAC** (``rbac`` module)
   Defines five graduated user roles (:class:`Role`) and nineteen
   fine-grained permissions (:class:`Permission`), with Flask route
   decorators (:func:`require_role`, :func:`require_permission`) and
   Open Policy Agent (OPA) integration for complex policy evaluation.

Convenience re-exports let consumers write concise import statements::

    # JWT validation
    from shared.auth import validate_token, jwt_required
    from shared.auth import AuthenticationError, TokenExpiredError

    # RBAC decorators and enums
    from shared.auth import require_role, require_permission
    from shared.auth import Role, Permission

    # Permission checking utilities
    from shared.auth import check_permission, check_role
    from shared.auth import validate_tenant_access

    # OPA client
    from shared.auth import get_opa_client

All substantial logic resides in :mod:`shared.auth.jwt_handler` and
:mod:`shared.auth.rbac`.  This ``__init__.py`` simply aggregates the
public API surface for ergonomic consumption.

Note:
    Imports are wrapped in ``try … except ImportError`` blocks to handle
    potential circular-dependency or missing-module scenarios gracefully
    during early application bootstrap.  If either sub-module fails to
    import (e.g. because a transitive dependency like ``python-jose`` or
    ``requests`` is not yet installed), the corresponding symbols will
    not be available in the package namespace — but the package itself
    will still load without raising, allowing other parts of the
    application to proceed.
"""

from __future__ import annotations


# ---------------------------------------------------------------------------
# Public API surface — populated dynamically by the conditional imports below.
# ---------------------------------------------------------------------------
__all__: list[str] = [
    # jwt_handler exports
    "validate_token",
    "get_current_user",
    "extract_token_from_request",
    "refresh_access_token",
    "jwt_required",
    "AuthenticationError",
    "TokenExpiredError",
    "InvalidTokenError",
    # rbac exports
    "require_role",
    "require_permission",
    "check_permission",
    "check_role",
    "validate_tenant_access",
    "Role",
    "Permission",
    "get_opa_client",
]

# ---------------------------------------------------------------------------
# Re-exports from shared.auth.jwt_handler
# ---------------------------------------------------------------------------
# Provides JWT RS256 validation against Auth0 JWKS, token extraction from
# Flask requests, user-context helpers, token refresh, and the ``jwt_required``
# Flask middleware decorator.  Exception classes allow callers to handle
# authentication failures at the appropriate granularity.
#
# Wrapped in try/except to avoid hard failures if transitive dependencies
# (python-jose, requests, Flask) are not yet installed or if there is a
# circular-import edge case during early application bootstrapping.
# ---------------------------------------------------------------------------
try:
    from shared.auth.jwt_handler import (
        AuthenticationError,
        InvalidTokenError,
        TokenExpiredError,
        extract_token_from_request,
        get_current_user,
        jwt_required,
        refresh_access_token,
        validate_token,
    )
except ImportError:
    # Degrade gracefully — the symbols simply won't be available in the
    # package namespace.  Callers that import them directly from
    # ``shared.auth.jwt_handler`` will still get the correct ImportError
    # pointing at the real missing dependency.
    pass

# ---------------------------------------------------------------------------
# Re-exports from shared.auth.rbac
# ---------------------------------------------------------------------------
# Provides role and permission enumerations, Flask route-protection
# decorators, standalone permission/role checking helpers, multi-tenant
# access validation, and the OPA client factory.
#
# ``rbac`` imports ``get_current_user`` from ``jwt_handler``, so it must
# be loaded after the jwt_handler block above.  The try/except pattern
# ensures that a failure in ``rbac`` does not prevent ``jwt_handler``
# symbols from being available.
# ---------------------------------------------------------------------------
try:
    from shared.auth.rbac import (
        Permission,
        Role,
        check_permission,
        check_role,
        get_opa_client,
        require_permission,
        require_role,
        validate_tenant_access,
    )
except ImportError:
    # Same graceful-degradation strategy as jwt_handler above.
    pass
