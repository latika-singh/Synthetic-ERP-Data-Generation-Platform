"""JWT validation middleware for the Synthetic ERP Data Generation Platform API Gateway.

This module implements Auth0-based JWT authentication as a Flask ``before_request``
hook, ensuring that **every** API endpoint (except health probes and the OAuth
callback) requires a valid RS256-signed JSON Web Token.

Security Architecture:
    - Tokens are signed with RS256 (asymmetric) by Auth0
    - Public keys are fetched from Auth0's JWKS endpoint and cached for 1 hour
    - Token validation checks expiry, issuer, audience, and signature
    - Decoded claims (user_id, email, roles, permissions, tenant_id) are stored
      on Flask's ``g`` object for use by downstream route handlers and middleware
    - Structured JSON error responses are returned for all authentication failures

Role Model (five graduated roles per R-006):
    - Platform Admin
    - Data Engineer
    - Developer
    - QA Engineer
    - Data Analyst

Usage::

    # In the Flask Application Factory (app.py):
    from api_gateway.middleware.auth import register_auth_middleware

    def create_app():
        app = Flask(__name__)
        register_auth_middleware(app)
        return app

    # In route handlers — role-based protection:
    from api_gateway.middleware.auth import require_roles

    @app.route('/api/v1/admin/users')
    @require_roles('Platform Admin')
    def list_users():
        ...

    # Permission-based protection:
    from api_gateway.middleware.auth import require_permissions

    @app.route('/api/v1/generation/jobs', methods=['POST'])
    @require_permissions('create:generation_jobs')
    def create_job():
        ...
"""

from __future__ import annotations

import json
import time
import urllib.request
from functools import wraps
from typing import TYPE_CHECKING, Any

from flask import Flask, Response, current_app, g, jsonify, request
from jose import ExpiredSignatureError, JWTError, jwt as jose_jwt
from jose.jwt import JWTClaimsError

from shared.logging.structured_logger import get_logger


if TYPE_CHECKING:
    from collections.abc import Callable

    import structlog


# ---------------------------------------------------------------------------
# Module-level logger
# ---------------------------------------------------------------------------

logger: structlog.stdlib.BoundLogger = get_logger(__name__)

# ---------------------------------------------------------------------------
# Module-level constants (exported)
# ---------------------------------------------------------------------------

EXEMPT_PATHS: set[str] = {
    "/health",
    "/ready",
    "/api/v1/auth/login",
    "/api/v1/auth/callback",
}
"""Paths that bypass JWT validation entirely.

Health and readiness probes must respond without authentication for
Kubernetes liveness/readiness checks.  Auth endpoints handle their own
token lifecycle.
"""

JWKS_CACHE_TTL: int = 3600
"""Time-to-live in seconds for the cached Auth0 JWKS response (1 hour).

Caching the JSON Web Key Set avoids hitting Auth0's ``/.well-known/jwks.json``
endpoint on every request.  Keys rotate infrequently, so 1 hour is safe.
"""

# ---------------------------------------------------------------------------
# Private module state — JWKS cache
# ---------------------------------------------------------------------------

_jwks_cache: dict[str, Any] = {
    "keys": {},
    "fetched_at": 0.0,
}
"""Internal cache storing the last successful JWKS response and the timestamp
at which it was fetched.  Protected by the TTL check in :func:`_fetch_jwks`.
"""

# ---------------------------------------------------------------------------
# Auth0 custom claim namespace.  Auth0 requires custom claims to use a
# namespaced URI to avoid collisions with registered JWT claims.
# ---------------------------------------------------------------------------

_CUSTOM_CLAIM_PREFIX: str = "https://synthetic-erp/"
_ROLES_CLAIM: str = f"{_CUSTOM_CLAIM_PREFIX}roles"
_PERMISSIONS_CLAIM: str = f"{_CUSTOM_CLAIM_PREFIX}permissions"
_TENANT_ID_CLAIM: str = f"{_CUSTOM_CLAIM_PREFIX}tenant_id"

# ---------------------------------------------------------------------------
# Valid platform roles for documentation and optional strict validation.
# ---------------------------------------------------------------------------

VALID_ROLES: set[str] = {
    "Platform Admin",
    "Data Engineer",
    "Developer",
    "QA Engineer",
    "Data Analyst",
}


# ---------------------------------------------------------------------------
# Private helper functions
# ---------------------------------------------------------------------------


def _fetch_jwks(auth0_domain: str) -> dict[str, Any]:
    """Fetch the JSON Web Key Set from Auth0's well-known endpoint.

    Results are cached in-memory for :data:`JWKS_CACHE_TTL` seconds.  If
    the network request fails and a previously cached response exists, the
    stale cache is returned as a graceful fallback.

    Args:
        auth0_domain: The Auth0 tenant domain (e.g. ``"myapp.us.auth0.com"``).

    Returns:
        A dictionary conforming to the JWKS specification, containing a
        ``"keys"`` list of JWK objects.

    Raises:
        RuntimeError: If the JWKS endpoint cannot be reached **and** no
            previously cached response is available.
    """
    now: float = time.time()

    # Return cached data if still within TTL.
    if _jwks_cache["keys"] and (now - _jwks_cache["fetched_at"]) < JWKS_CACHE_TTL:
        cached_keys: dict[str, Any] = _jwks_cache["keys"]
        return cached_keys

    jwks_url: str = f"https://{auth0_domain}/.well-known/jwks.json"

    try:
        req = urllib.request.Request(  # noqa: S310
            jwks_url,
            headers={"Accept": "application/json", "User-Agent": "SyntheticERP-APIGateway/1.0"},
        )
        with urllib.request.urlopen(req, timeout=10) as response:  # noqa: S310
            body: str = response.read().decode("utf-8")
            jwks_data: dict[str, Any] = json.loads(body)

        _jwks_cache["keys"] = jwks_data
        _jwks_cache["fetched_at"] = now

        logger.debug(
            "jwks_fetched",
            auth0_domain=auth0_domain,
            key_count=len(jwks_data.get("keys", [])),
        )
        return jwks_data

    except Exception as exc:
        logger.error(
            "jwks_fetch_failed",
            auth0_domain=auth0_domain,
            error=str(exc),
            has_cached_keys=bool(_jwks_cache["keys"]),
        )

        # Graceful fallback: return stale cache if available.
        if _jwks_cache["keys"]:
            logger.warning(
                "jwks_using_stale_cache",
                auth0_domain=auth0_domain,
                cached_age_seconds=int(now - _jwks_cache["fetched_at"]),
            )
            stale_keys: dict[str, Any] = _jwks_cache["keys"]
            return stale_keys

        raise RuntimeError(
            f"Unable to fetch JWKS from {jwks_url} and no cached keys available"
        ) from exc


def _get_rsa_key(token: str, auth0_domain: str) -> dict[str, str] | None:
    """Extract the RSA public key matching the token's ``kid`` header claim.

    Decodes the JWT header (without verifying the signature) to read the
    ``kid`` (Key ID) claim, then searches the JWKS for a matching key.

    Args:
        token: The raw JWT string from the ``Authorization: Bearer`` header.
        auth0_domain: The Auth0 tenant domain used to fetch the JWKS.

    Returns:
        A dictionary containing the RSA key components (``kty``, ``kid``,
        ``use``, ``n``, ``e``) suitable for passing to ``python-jose``'s
        ``jwt.decode()`` — or ``None`` if no matching key is found.
    """
    try:
        unverified_header: dict[str, Any] = jose_jwt.get_unverified_header(token)
    except JWTError as exc:
        logger.warning("jwt_header_decode_failed", error=str(exc))
        return None

    token_kid: str | None = unverified_header.get("kid")
    if not token_kid:
        logger.warning("jwt_missing_kid", header_keys=list(unverified_header.keys()))
        return None

    jwks: dict[str, Any] = _fetch_jwks(auth0_domain)
    keys: list[dict[str, Any]] = jwks.get("keys", [])

    for key in keys:
        if key.get("kid") == token_kid:
            rsa_key: dict[str, str] = {
                "kty": key["kty"],
                "kid": key["kid"],
                "use": key.get("use", "sig"),
                "n": key["n"],
                "e": key["e"],
            }
            logger.debug("rsa_key_matched", kid=token_kid)
            return rsa_key

    logger.warning(
        "rsa_key_not_found",
        token_kid=token_kid,
        available_kids=[k.get("kid") for k in keys],
    )
    return None


# ---------------------------------------------------------------------------
# Public API — token validation
# ---------------------------------------------------------------------------


def validate_jwt_token(token: str) -> dict[str, Any]:
    """Decode, verify, and validate a JWT issued by Auth0.

    Performs full RS256 signature verification using the public key fetched
    from Auth0's JWKS endpoint.  Additionally validates:

    - **Expiry** — the ``exp`` claim must be in the future.
    - **Issuer** — must match ``https://{AUTH0_DOMAIN}/``.
    - **Audience** — must include the configured ``AUTH0_API_AUDIENCE``.
    - **Required claims** — ``sub`` must be present.

    Args:
        token: The raw JWT string (without the ``Bearer `` prefix).

    Returns:
        The fully decoded JWT payload as a dictionary containing all
        registered and custom claims.

    Raises:
        ExpiredSignatureError: If the token's ``exp`` claim is in the past.
        JWTClaimsError: If the issuer or audience claims do not match the
            expected values, or if required claims are missing.
        JWTError: For any other JWT-related error (malformed token, invalid
            signature, missing key, etc.).
        RuntimeError: If the RSA public key cannot be resolved (JWKS
            unreachable and no cache).
    """
    auth0_domain: str = current_app.config.get("AUTH0_DOMAIN", "")
    api_audience: str = current_app.config.get("AUTH0_API_AUDIENCE", "")
    algorithm: str = current_app.config.get("JWT_ALGORITHM", "RS256")

    if not auth0_domain:
        logger.error("auth0_domain_not_configured")
        raise JWTError("AUTH0_DOMAIN is not configured")

    if not api_audience:
        logger.error("auth0_api_audience_not_configured")
        raise JWTError("AUTH0_API_AUDIENCE is not configured")

    # Resolve the RSA public key matching this token's kid.
    rsa_key: dict[str, str] | None = _get_rsa_key(token, auth0_domain)
    if rsa_key is None:
        raise JWTError(
            "Unable to find appropriate RSA key for token verification"
        )

    # Decode and verify the token.
    payload: dict[str, Any] = jose_jwt.decode(
        token,
        rsa_key,
        algorithms=[algorithm],
        audience=api_audience,
        issuer=f"https://{auth0_domain}/",
        options={
            "require_exp": True,
            "require_iat": True,
            "require_sub": True,
        },
    )

    # Ensure the ``sub`` claim (subject / user ID) is present and non-empty.
    if not payload.get("sub"):
        raise JWTClaimsError("Token is missing required 'sub' claim")

    return payload


# ---------------------------------------------------------------------------
# Public API — Flask before_request middleware
# ---------------------------------------------------------------------------


def jwt_auth_middleware() -> tuple[Response, int] | None:
    """Flask ``before_request`` hook that enforces JWT authentication.

    Execution flow:

    1. Skip validation for exempt paths (:data:`EXEMPT_PATHS`) and CORS
       preflight ``OPTIONS`` requests.
    2. Extract the ``Authorization: Bearer <token>`` header.
    3. Validate the token via :func:`validate_jwt_token`.
    4. On success — store decoded claims on ``flask.g``:
       ``g.user_id``, ``g.user_email``, ``g.user_roles``,
       ``g.user_permissions``, ``g.tenant_id``, ``g.jwt_payload``.
    5. On failure — return a structured JSON 401 response.

    Returns:
        ``None`` when authentication succeeds (request processing
        continues).  A ``(Response, status_code)`` tuple when
        authentication fails, short-circuiting the request pipeline.
    """
    # ------------------------------------------------------------------
    # Exempt paths — health probes, auth callback, CORS preflight
    # ------------------------------------------------------------------
    request_path: str = request.path.rstrip("/") or "/"

    if request_path in EXEMPT_PATHS:
        return None

    if request.method == "OPTIONS":
        return None

    # Also handle path prefix matching for exempt paths that may have
    # trailing segments (e.g. /health/deep or /ready/checks).
    for exempt in EXEMPT_PATHS:
        if request_path.startswith(exempt):
            return None

    # ------------------------------------------------------------------
    # Extract Bearer token from Authorization header
    # ------------------------------------------------------------------
    auth_header: str | None = request.headers.get("Authorization")

    if not auth_header:
        logger.warning(
            "auth_missing_header",
            path=request.path,
            method=request.method,
            remote_addr=request.remote_addr,
        )
        return (
            jsonify({
                "error": "Missing authorization header",
                "code": "UNAUTHORIZED",
            }),
            401,
        )

    # Validate Bearer scheme.
    parts: list[str] = auth_header.split()

    if len(parts) != 2 or parts[0].lower() != "bearer":
        logger.warning(
            "auth_invalid_header_format",
            path=request.path,
            method=request.method,
        )
        return (
            jsonify({
                "error": "Authorization header must be 'Bearer <token>'",
                "code": "INVALID_HEADER",
            }),
            401,
        )

    token: str = parts[1]

    # ------------------------------------------------------------------
    # Validate the JWT
    # ------------------------------------------------------------------
    try:
        payload: dict[str, Any] = validate_jwt_token(token)

        # Store decoded claims on Flask's g object for downstream use.
        g.user_id = payload["sub"]
        g.user_email = payload.get("email", "")

        # Auth0 custom claims — try namespaced first, fall back to flat.
        g.user_roles = (
            payload.get(_ROLES_CLAIM, [])
            or payload.get("roles", [])
        )
        g.user_permissions = (
            payload.get(_PERMISSIONS_CLAIM, [])
            or payload.get("permissions", [])
        )
        g.tenant_id = (
            payload.get(_TENANT_ID_CLAIM, "")
            or payload.get("tenant_id", "")
        )
        g.jwt_payload = payload

        logger.info(
            "auth_success",
            user_id=g.user_id,
            email=g.user_email,
            roles=g.user_roles,
            tenant_id=g.tenant_id,
            path=request.path,
            method=request.method,
        )

        return None  # Authentication succeeded — continue request pipeline.

    except ExpiredSignatureError:
        logger.warning(
            "auth_token_expired",
            path=request.path,
            method=request.method,
            remote_addr=request.remote_addr,
        )
        return (
            jsonify({
                "error": "Token expired",
                "code": "TOKEN_EXPIRED",
            }),
            401,
        )

    except JWTClaimsError as exc:
        logger.warning(
            "auth_claims_error",
            error=str(exc),
            path=request.path,
            method=request.method,
            remote_addr=request.remote_addr,
        )
        return (
            jsonify({
                "error": "Invalid token claims",
                "code": "INVALID_CLAIMS",
                "detail": str(exc),
            }),
            401,
        )

    except JWTError as exc:
        logger.warning(
            "auth_invalid_token",
            error=str(exc),
            path=request.path,
            method=request.method,
            remote_addr=request.remote_addr,
        )
        return (
            jsonify({
                "error": "Invalid token",
                "code": "INVALID_TOKEN",
            }),
            401,
        )

    except RuntimeError as exc:
        # JWKS fetch failure with no cache — service-level issue.
        logger.error(
            "auth_jwks_unavailable",
            error=str(exc),
            path=request.path,
        )
        return (
            jsonify({
                "error": "Authentication service unavailable",
                "code": "AUTH_SERVICE_UNAVAILABLE",
            }),
            503,
        )

    except Exception as exc:
        # Catch-all for unexpected errors — must never leak stack traces.
        logger.error(
            "auth_unexpected_error",
            error=str(exc),
            error_type=type(exc).__name__,
            path=request.path,
        )
        return (
            jsonify({
                "error": "Authentication error",
                "code": "AUTH_ERROR",
            }),
            401,
        )


# ---------------------------------------------------------------------------
# Public API — role and permission decorators
# ---------------------------------------------------------------------------


def require_roles(*roles: str) -> Callable:
    """Decorator that enforces role-based access control on route handlers.

    Checks the authenticated user's roles (stored in ``g.user_roles`` by
    :func:`jwt_auth_middleware`) against the required roles.  Access is
    granted if the user possesses **any** of the specified roles (logical
    OR).

    Args:
        *roles: One or more role names that grant access to the decorated
            endpoint.  Valid roles are: ``Platform Admin``,
            ``Data Engineer``, ``Developer``, ``QA Engineer``,
            ``Data Analyst``.

    Returns:
        A decorator function that wraps the route handler with role
        checking logic.

    Example::

        @app.route('/api/v1/admin/users')
        @require_roles('Platform Admin')
        def list_users():
            return jsonify(users=[...])

        @app.route('/api/v1/generation/jobs', methods=['POST'])
        @require_roles('Platform Admin', 'Data Engineer')
        def create_job():
            return jsonify(job_id='...')
    """
    def decorator(fn: Callable) -> Callable:
        @wraps(fn)
        def wrapper(*args: Any, **kwargs: Any) -> Any:
            user_roles: list[str] = getattr(g, "user_roles", [])

            # Check if user has at least one of the required roles.
            if not any(role in user_roles for role in roles):
                logger.warning(
                    "auth_role_denied",
                    user_id=getattr(g, "user_id", "unknown"),
                    user_roles=user_roles,
                    required_roles=list(roles),
                    path=request.path,
                    method=request.method,
                )
                return (
                    jsonify({
                        "error": "Insufficient permissions",
                        "code": "FORBIDDEN",
                        "required_roles": list(roles),
                    }),
                    403,
                )

            return fn(*args, **kwargs)

        return wrapper

    return decorator


def require_permissions(*permissions: str) -> Callable:
    """Decorator that enforces permission-based access control on route handlers.

    Checks the authenticated user's permissions (stored in
    ``g.user_permissions`` by :func:`jwt_auth_middleware`) against the
    required permissions.  Access is granted if the user possesses **all**
    of the specified permissions (logical AND).

    Args:
        *permissions: One or more permission strings that must all be
            present for the user to access the endpoint (e.g.
            ``"create:generation_jobs"``, ``"read:schemas"``).

    Returns:
        A decorator function that wraps the route handler with permission
        checking logic.

    Example::

        @app.route('/api/v1/schemas', methods=['DELETE'])
        @require_permissions('delete:schemas')
        def delete_schema():
            return jsonify(deleted=True)

        @app.route('/api/v1/admin/tenants', methods=['POST'])
        @require_permissions('create:tenants', 'admin:tenants')
        def create_tenant():
            return jsonify(tenant_id='...')
    """
    def decorator(fn: Callable) -> Callable:
        @wraps(fn)
        def wrapper(*args: Any, **kwargs: Any) -> Any:
            user_permissions: list[str] = getattr(g, "user_permissions", [])

            # Check that user has ALL required permissions (AND logic).
            missing: list[str] = [
                perm for perm in permissions if perm not in user_permissions
            ]

            if missing:
                logger.warning(
                    "auth_permission_denied",
                    user_id=getattr(g, "user_id", "unknown"),
                    user_permissions=user_permissions,
                    required_permissions=list(permissions),
                    missing_permissions=missing,
                    path=request.path,
                    method=request.method,
                )
                return (
                    jsonify({
                        "error": "Insufficient permissions",
                        "code": "FORBIDDEN",
                        "required_permissions": list(permissions),
                        "missing_permissions": missing,
                    }),
                    403,
                )

            return fn(*args, **kwargs)

        return wrapper

    return decorator


# ---------------------------------------------------------------------------
# Public API — middleware registration
# ---------------------------------------------------------------------------


def register_auth_middleware(app: Flask) -> None:
    """Register the JWT authentication middleware on a Flask application.

    Attaches :func:`jwt_auth_middleware` as a ``before_request`` hook so
    that every incoming request is authenticated before reaching any route
    handler.  This function is intended to be called once during the
    Application Factory's ``create_app()`` setup.

    Args:
        app: The Flask application instance to register the middleware on.

    Example::

        def create_app():
            app = Flask(__name__)
            app.config.from_object(config)
            register_auth_middleware(app)
            return app
    """
    app.before_request(jwt_auth_middleware)

    logger.info(
        "auth_middleware_registered",
        exempt_paths=sorted(EXEMPT_PATHS),
        jwks_cache_ttl=JWKS_CACHE_TTL,
    )
