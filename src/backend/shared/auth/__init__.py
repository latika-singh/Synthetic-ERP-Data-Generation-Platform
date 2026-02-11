"""Authentication and authorisation utilities for the Synthetic ERP Data Generation Platform.

This sub-package provides JWT validation and role-based access control (RBAC)
infrastructure shared by all six backend microservices:

- **jwt_handler** — Auth0 JWKS-based RS256 JWT validation, token refresh,
  request context extraction, and Flask middleware integration.
- **rbac** — Role-based and permission-based access control decorators
  that gate Flask route handlers based on the validated JWT claims.

Convenience imports allow consumers to write::

    from shared.auth import validate_token, jwt_required
    from shared.auth import AuthenticationError, TokenExpiredError
"""

from __future__ import annotations


__all__: list[str] = []

# --- jwt_handler ----------------------------------------------------------
try:
    from shared.auth.jwt_handler import (
        AuthenticationError,
        InsufficientScopeError,
        InvalidTokenError,
        TokenExpiredError,
        decode_token_unverified,
        extract_token_from_request,
        fetch_jwks,
        get_current_user,
        get_signing_key,
        jwt_required,
        refresh_access_token,
        validate_token,
    )

    __all__.extend(
        [
            "AuthenticationError",
            "InsufficientScopeError",
            "InvalidTokenError",
            "TokenExpiredError",
            "decode_token_unverified",
            "extract_token_from_request",
            "fetch_jwks",
            "get_current_user",
            "get_signing_key",
            "jwt_required",
            "refresh_access_token",
            "validate_token",
        ]
    )
except ImportError:
    pass

# --- rbac -----------------------------------------------------------------
try:
    from shared.auth.rbac import require_permission, require_role

    __all__.extend(["require_permission", "require_role"])
except ImportError:
    pass
