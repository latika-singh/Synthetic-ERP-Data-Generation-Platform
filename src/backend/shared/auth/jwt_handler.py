"""JWT RS256 token validation module for the Synthetic ERP Data Generation Platform.

This module provides shared JWT authentication infrastructure used by all six
backend microservices (API Gateway, Generation Engine, Profiling Service,
Quality Service, Compliance Service, Provisioning Service).  It implements:

- **Auth0 JWKS-based RS256 validation**: Fetches the JSON Web Key Set from
  Auth0's ``/.well-known/jwks.json`` endpoint, matches the ``kid`` (Key ID)
  in the token header to an RSA public key, and verifies the signature using
  ``python-jose`` 3.3.x.
- **Double-layer JWKS caching**: In-memory cache (thread-safe via
  ``threading.Lock``) backed by Redis (via ``shared.database.redis_client``)
  with a 1-hour TTL, minimising network round-trips to Auth0.
- **Claim extraction**: Extracts ``sub``, ``email``, ``name``, ``roles``,
  ``tenant_id``, ``permissions``, ``exp``, ``iat``, ``iss``, and ``aud``
  from validated JWT payloads.
- **Token refresh**: Exchanges refresh tokens for new access tokens via
  Auth0's ``/oauth/token`` endpoint with refresh-token rotation.
- **Flask middleware integration**: The :func:`jwt_required` decorator
  factory extracts and validates tokens from the ``Authorization`` header,
  stores user context on ``flask.g``, and short-circuits with ``401``
  responses for unauthenticated requests (health-check endpoints exempted).

Configuration follows 12-factor app methodology — all Auth0 settings are
read from environment variables (``AUTH0_DOMAIN``, ``AUTH0_AUDIENCE``,
``AUTH0_CLIENT_ID``, ``AUTH0_CLIENT_SECRET``) with centralised defaults
provided by :class:`shared.config.base.BaseConfig`.

Security notes:
    - Full JWT token values are **never** logged.  Only ``sub`` and ``exp``
      claims are included in log events for correlation purposes.
    - RS256 is the **only** permitted signing algorithm.
    - All HTTPS requests to Auth0 use a 10-second timeout.

Usage::

    # In any service's create_app():
    from shared.auth.jwt_handler import jwt_required, validate_token


    @app.before_request
    def before_request():
        return jwt_required()()


    # Validate a raw token:
    claims = validate_token(raw_jwt_string)

    # Get current user inside a request:
    from shared.auth.jwt_handler import get_current_user

    user = get_current_user()
"""

from __future__ import annotations

import json
import logging
import os
import threading
import time
from typing import TYPE_CHECKING, Any

import requests
import requests.exceptions
from flask import g, jsonify, request
from jose import JWTError, jwt
from jose.exceptions import ExpiredSignatureError, JWTClaimsError

from shared.config.base import BaseConfig
from shared.database.redis_client import cache_get, cache_set
from shared.logging.structured_logger import get_logger


if TYPE_CHECKING:
    from collections.abc import Callable


# ---------------------------------------------------------------------------
# Module-level logger
# ---------------------------------------------------------------------------
# Use the structlog-based logger as primary; keep a stdlib fallback for very
# early initialisation paths where structlog may not yet be configured.
logger = get_logger(__name__)
_fallback_logger: logging.Logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Module-level configuration constants (12-factor: read from environment)
# ---------------------------------------------------------------------------
# We load a BaseConfig instance once so that environment variables are
# resolved at import time with consistent defaults.  Individual functions
# also accept overrides via parameters where appropriate.
_config = BaseConfig()

AUTH0_DOMAIN: str = _config.AUTH0_DOMAIN or os.environ.get("AUTH0_DOMAIN", "")
AUTH0_AUDIENCE: str = _config.AUTH0_AUDIENCE or os.environ.get("AUTH0_AUDIENCE", "")
AUTH0_ALGORITHMS: list[str] = [_config.JWT_ALGORITHM or "RS256"]
JWKS_CACHE_TTL: int = 3600  # Cache JWKS for 1 hour in Redis
TOKEN_ISSUER_PREFIX: str = "https://"

# Auth0 client credentials (used only for refresh-token exchange)
_AUTH0_CLIENT_ID: str = _config.AUTH0_CLIENT_ID or os.environ.get("AUTH0_CLIENT_ID", "")
_AUTH0_CLIENT_SECRET: str = _config.AUTH0_CLIENT_SECRET or os.environ.get("AUTH0_CLIENT_SECRET", "")
_JWT_ACCESS_TOKEN_EXPIRES: int = (
    _config.JWT_ACCESS_TOKEN_EXPIRES
    if _config.JWT_ACCESS_TOKEN_EXPIRES
    else int(os.environ.get("JWT_ACCESS_TOKEN_EXPIRES", "3600"))
)

# Health-check paths that bypass JWT validation.
_HEALTH_CHECK_PATHS: frozenset[str] = frozenset({"/health", "/ready"})

# HTTP timeout for Auth0 endpoint requests (seconds).
_HTTP_TIMEOUT: int = 10

# Maximum retry attempts for JWKS endpoint fetching.
_JWKS_FETCH_MAX_RETRIES: int = 2
_JWKS_FETCH_RETRY_BACKOFF: float = 1.0  # seconds


# ============================================================================
# Custom exception classes
# ============================================================================


class AuthenticationError(Exception):
    """Base authentication error raised when JWT validation fails.

    All other authentication-related exceptions in this module inherit from
    ``AuthenticationError`` so that callers can catch the entire family with
    a single ``except`` clause when fine-grained handling is not needed.

    Attributes:
        message: Human-readable description of the authentication failure.
    """

    def __init__(self, message: str = "Authentication failed") -> None:
        self.message = message
        super().__init__(self.message)


class TokenExpiredError(AuthenticationError):
    """Raised when a JWT has expired (the ``exp`` claim is in the past).

    Attributes:
        message: Human-readable description indicating token expiry.
    """

    def __init__(self, message: str = "Token has expired") -> None:
        super().__init__(message)


class InvalidTokenError(AuthenticationError):
    """Raised when a JWT is malformed, has an invalid signature, or fails
    claim validation (wrong issuer, wrong audience, etc.).

    Attributes:
        message: Human-readable description of the validation failure.
    """

    def __init__(self, message: str = "Token is invalid or malformed") -> None:
        super().__init__(message)


class InsufficientScopeError(AuthenticationError):
    """Raised when a JWT is valid but lacks a required scope or permission.

    Used by the RBAC layer (``shared.auth.rbac``) when the caller's token
    does not include the permission needed for the requested operation.

    Attributes:
        message: Human-readable description of the missing scope/permission.
    """

    def __init__(self, message: str = "Insufficient scope or permission") -> None:
        super().__init__(message)


# ============================================================================
# JWKS key cache management
# ============================================================================

# Thread-safe in-memory cache for JWKS keys.  The cache is a dict with the
# full JWKS payload from Auth0 and an expiry timestamp.  A ``threading.Lock``
# serialises access so that concurrent Gunicorn workers within the same
# process never create duplicate HTTP requests to the JWKS endpoint.
_jwks_cache: dict[str, Any] = {}
_jwks_cache_expiry: float = 0.0
_jwks_lock: threading.Lock = threading.Lock()


def _build_jwks_url() -> str:
    """Construct the full JWKS endpoint URL from the configured Auth0 domain.

    Returns:
        The HTTPS URL to Auth0's ``/.well-known/jwks.json`` endpoint.

    Raises:
        AuthenticationError: If ``AUTH0_DOMAIN`` is not configured.
    """
    domain = AUTH0_DOMAIN or os.environ.get("AUTH0_DOMAIN", "")
    if not domain:
        raise AuthenticationError(
            "AUTH0_DOMAIN environment variable is not configured. Cannot construct JWKS endpoint URL."
        )
    return f"{TOKEN_ISSUER_PREFIX}{domain}/.well-known/jwks.json"


def _build_issuer() -> str:
    """Construct the expected JWT issuer string from the configured Auth0 domain.

    Auth0 issues tokens with ``iss`` set to ``https://<domain>/`` (note the
    trailing slash).

    Returns:
        The issuer string, e.g. ``https://my-tenant.auth0.com/``.
    """
    domain = AUTH0_DOMAIN or os.environ.get("AUTH0_DOMAIN", "")
    return f"{TOKEN_ISSUER_PREFIX}{domain}/"


def _build_redis_cache_key() -> str:
    """Construct the Redis cache key for the JWKS payload.

    The key follows the pattern ``auth:jwks:{domain}`` so that multiple
    Auth0 domains (e.g. in multi-tenant setups) are cached independently.

    Returns:
        The Redis key string.
    """
    domain = AUTH0_DOMAIN or os.environ.get("AUTH0_DOMAIN", "")
    return f"auth:jwks:{domain}"


def _try_redis_cache(redis_key: str, now: float) -> dict[str, Any] | None:
    """Attempt to load JWKS from the Redis cache layer.

    Args:
        redis_key: The Redis key where JWKS data is stored.
        now: Current epoch timestamp for setting in-memory cache expiry.

    Returns:
        The JWKS data dict if found in Redis, or ``None`` on cache miss
        or Redis failure.
    """
    global _jwks_cache, _jwks_cache_expiry  # noqa: PLW0603

    try:
        cached_json = cache_get(redis_key)
        if cached_json is None:
            return None
        jwks_data: dict[str, Any] = json.loads(cached_json) if isinstance(cached_json, str) else cached_json
        _jwks_cache = jwks_data
        _jwks_cache_expiry = now + JWKS_CACHE_TTL
        logger.debug("jwks_cache_hit", cache_layer="redis")
        return jwks_data
    except Exception as exc:
        # Redis failure is non-fatal; proceed to Auth0 endpoint.
        logger.debug("jwks_redis_cache_error", error=str(exc))
        return None


def _fetch_jwks_from_auth0(jwks_url: str, redis_key: str, now: float) -> dict[str, Any]:
    """Fetch JWKS from Auth0's HTTPS endpoint with retry logic.

    Retries up to :data:`_JWKS_FETCH_MAX_RETRIES` times with exponential
    backoff on network errors.

    Args:
        jwks_url: Full URL to Auth0's ``/.well-known/jwks.json``.
        redis_key: Redis key for storing the fetched JWKS.
        now: Current epoch timestamp for setting in-memory cache expiry.

    Returns:
        The JWKS data dict.

    Raises:
        AuthenticationError: If all retry attempts are exhausted.
    """
    global _jwks_cache, _jwks_cache_expiry  # noqa: PLW0603

    last_error: Exception | None = None

    for attempt in range(_JWKS_FETCH_MAX_RETRIES):
        try:
            logger.debug("jwks_fetch_attempt", url=jwks_url, attempt=attempt + 1)
            response = requests.get(jwks_url, timeout=_HTTP_TIMEOUT)
            response.raise_for_status()
            jwks_data: dict[str, Any] = response.json()

            if "keys" not in jwks_data:
                raise AuthenticationError("JWKS response from Auth0 does not contain 'keys' array.")

            _jwks_cache = jwks_data
            _jwks_cache_expiry = now + JWKS_CACHE_TTL
            _store_jwks_in_redis(redis_key, jwks_data)

            logger.debug(
                "jwks_fetched_successfully",
                key_count=len(jwks_data.get("keys", [])),
            )
            return jwks_data

        except requests.exceptions.RequestException as exc:
            last_error = exc
            logger.debug(
                "jwks_fetch_error",
                attempt=attempt + 1,
                error_type=type(exc).__name__,
                error=str(exc),
            )

        # Backoff before retrying.
        if attempt < _JWKS_FETCH_MAX_RETRIES - 1:
            time.sleep(_JWKS_FETCH_RETRY_BACKOFF * (2**attempt))

    error_message = f"Failed to fetch JWKS from {jwks_url} after {_JWKS_FETCH_MAX_RETRIES} attempts: {last_error}"
    logger.warning("jwks_fetch_failed", error=error_message)
    raise AuthenticationError(error_message)


def _store_jwks_in_redis(redis_key: str, jwks_data: dict[str, Any]) -> None:
    """Best-effort storage of JWKS data in Redis.

    Args:
        redis_key: The Redis cache key.
        jwks_data: The JWKS payload to store.
    """
    try:
        cache_set(redis_key, jwks_data, ttl=JWKS_CACHE_TTL)
        logger.debug("jwks_stored_in_redis", redis_key=redis_key)
    except Exception as redis_err:
        logger.debug("jwks_redis_store_error", error=str(redis_err))


def fetch_jwks() -> dict[str, Any]:
    """Fetch the JSON Web Key Set from Auth0's JWKS endpoint.

    Implements a **double-layer caching** strategy to minimise latency and
    network calls:

    1. **In-memory cache** - checked first (no I/O); refreshed when the
       cached expiry timestamp is exceeded.
    2. **Redis cache** - checked on in-memory miss; shared across all
       Gunicorn workers / service replicas.
    3. **Auth0 HTTPS fetch** - performed only when both caches miss.  The
       response is stored in both Redis (with :data:`JWKS_CACHE_TTL`) and
       in-memory for subsequent requests.

    Network errors during the HTTPS fetch are retried up to
    :data:`_JWKS_FETCH_MAX_RETRIES` times with a 1-second exponential
    backoff.

    Returns:
        A dictionary containing the JWKS payload (with a ``"keys"`` list).

    Raises:
        AuthenticationError: If the JWKS cannot be retrieved after retries.
    """
    now = time.time()

    # -- Layer 1: in-memory cache -----------------------------------------
    if _jwks_cache and now < _jwks_cache_expiry:
        logger.debug("jwks_cache_hit", cache_layer="memory")
        return _jwks_cache

    with _jwks_lock:
        # Re-check after acquiring the lock (double-checked locking).
        now = time.time()
        if _jwks_cache and now < _jwks_cache_expiry:
            logger.debug("jwks_cache_hit_after_lock", cache_layer="memory")
            return _jwks_cache

        # -- Layer 2: Redis cache -----------------------------------------
        redis_key = _build_redis_cache_key()
        redis_result = _try_redis_cache(redis_key, now)
        if redis_result is not None:
            return redis_result

        # -- Layer 3: Auth0 HTTPS fetch -----------------------------------
        return _fetch_jwks_from_auth0(_build_jwks_url(), redis_key, now)


def get_signing_key(token: str) -> dict[str, Any]:
    """Extract the RSA public signing key matching the token's ``kid``.

    Reads the unverified JWT header to obtain the ``kid`` (Key ID) claim,
    then searches the cached JWKS for the corresponding RSA public key.

    Args:
        token: The raw JWT string (not yet validated).

    Returns:
        A dictionary representing the matching JWK (JSON Web Key) that
        can be passed directly to ``jose.jwt.decode()`` as the *key*
        argument.

    Raises:
        InvalidTokenError: If the token header is malformed or missing
            the ``kid`` claim.
        AuthenticationError: If no matching key is found in the JWKS, or
            if the JWKS cannot be fetched.
    """
    try:
        unverified_header: dict[str, Any] = jwt.get_unverified_header(token)
    except JWTError as exc:
        raise InvalidTokenError(f"Unable to extract JWT header: {exc}") from exc

    kid: str | None = unverified_header.get("kid")
    if not kid:
        raise InvalidTokenError("JWT header does not contain a 'kid' (Key ID) claim.")

    jwks_data = fetch_jwks()
    keys: list[dict[str, Any]] = jwks_data.get("keys", [])

    for key in keys:
        if key.get("kid") == kid:
            logger.debug("signing_key_found", kid=kid)
            return key

    # No matching key — the JWKS may have been rotated.  Force a refresh
    # by invalidating the in-memory cache and retrying once.
    global _jwks_cache_expiry  # noqa: PLW0603
    _jwks_cache_expiry = 0.0
    jwks_data = fetch_jwks()
    keys = jwks_data.get("keys", [])

    for key in keys:
        if key.get("kid") == kid:
            logger.debug("signing_key_found_after_refresh", kid=kid)
            return key

    raise InvalidTokenError(f"No matching signing key found for kid '{kid}' in Auth0 JWKS.")


# ============================================================================
# Token validation and decoding
# ============================================================================


def validate_token(token: str) -> dict[str, Any]:
    """Validate and decode a JWT using Auth0 RS256 public key verification.

    This is the **core** validation function shared by all six backend
    microservices.  It performs the following steps:

    1. Resolves the RSA public key via :func:`get_signing_key`.
    2. Verifies the RS256 signature using ``jose.jwt.decode()``.
    3. Validates standard claims: ``exp`` (not expired), ``iss`` (correct
       Auth0 issuer), ``aud`` (correct API audience).
    4. Extracts custom claims: ``roles``, ``tenant_id``, ``permissions``.

    Args:
        token: The raw JWT string from the ``Authorization: Bearer …``
            header.

    Returns:
        The decoded JWT payload as a dictionary containing at minimum:
        ``sub``, ``email``, ``name``, ``roles``, ``tenant_id``,
        ``permissions``, ``exp``, ``iat``, ``iss``, ``aud``.

    Raises:
        TokenExpiredError: If the token's ``exp`` claim is in the past.
        InvalidTokenError: If the signature is invalid, the issuer or
            audience do not match, or the token is otherwise malformed.
        AuthenticationError: If the JWKS endpoint is unreachable.
    """
    # Resolve the audience — re-read from env in case it was set after import.
    audience = AUTH0_AUDIENCE or os.environ.get("AUTH0_AUDIENCE", "")
    issuer = _build_issuer()

    # Step 1: Resolve signing key.
    signing_key = get_signing_key(token)

    # Step 2-3: Decode and verify signature + standard claims.
    try:
        payload: dict[str, Any] = jwt.decode(
            token,
            signing_key,
            algorithms=AUTH0_ALGORITHMS,
            audience=audience,
            issuer=issuer,
            options={
                "verify_exp": True,
                "verify_iss": True,
                "verify_aud": True,
                "verify_sub": True,
            },
        )
    except ExpiredSignatureError as exc:
        # Extract sub for logging without exposing the full token.
        _sub = _safe_extract_sub(token)
        logger.warning(
            "token_expired",
            user_id=_sub,
        )
        raise TokenExpiredError("Token has expired") from exc
    except JWTClaimsError as exc:
        logger.warning(
            "token_claims_invalid",
            error=str(exc),
        )
        raise InvalidTokenError(f"Token claims validation failed: {exc}") from exc
    except JWTError as exc:
        logger.warning(
            "token_validation_failed",
            error=str(exc),
        )
        raise InvalidTokenError(f"Token validation failed: {exc}") from exc

    # Step 4: Extract and normalise custom claims.
    # Auth0 custom claims are typically namespaced (e.g. "https://myapp/roles").
    # We support both namespaced and flat claim names for flexibility.
    payload.setdefault("roles", payload.get("https://erp-platform/roles", []))
    payload.setdefault("tenant_id", payload.get("https://erp-platform/tenant_id", ""))
    payload.setdefault("permissions", payload.get("https://erp-platform/permissions", []))
    payload.setdefault("email", payload.get("https://erp-platform/email", ""))
    payload.setdefault("name", payload.get("https://erp-platform/name", ""))

    # Ensure list-type claims are actually lists.
    if not isinstance(payload.get("roles"), list):
        payload["roles"] = [payload["roles"]] if payload.get("roles") else []
    if not isinstance(payload.get("permissions"), list):
        payload["permissions"] = [payload["permissions"]] if payload.get("permissions") else []

    logger.debug(
        "token_validated",
        user_id=payload.get("sub"),
        exp=payload.get("exp"),
        tenant_id=payload.get("tenant_id"),
    )

    return payload


def decode_token_unverified(token: str) -> dict[str, Any]:
    """Decode a JWT payload **without** signature verification.

    .. warning::

        This function **must not** be used for authentication or
        authorisation decisions.  It is intended **only** for
        logging/debugging purposes — e.g. extracting ``sub`` and ``exp``
        to include in structured log events before the token is fully
        validated.

    Args:
        token: The raw JWT string.

    Returns:
        The decoded payload dictionary.  Contains whatever claims the
        token carries; no validation of ``exp``, ``iss``, or ``aud`` is
        performed.

    Raises:
        InvalidTokenError: If the token is so malformed that even
            unverified decoding fails.
    """
    try:
        claims: dict[str, Any] = jwt.get_unverified_claims(token)
        return claims
    except JWTError as exc:
        raise InvalidTokenError(f"Unable to decode token (unverified): {exc}") from exc


# ============================================================================
# Request context helpers
# ============================================================================


def extract_token_from_request() -> str | None:
    """Extract the JWT from the current Flask request.

    Extraction order:

    1. ``Authorization`` header — expects the format ``Bearer <token>``.
    2. ``access_token`` query parameter — for WebSocket upgrade requests
       where headers cannot always be set by the client.

    Returns:
        The raw JWT string, or ``None`` if no token is present or the
        header format is malformed.
    """
    # Strategy 1: Authorization header
    auth_header: str | None = request.headers.get("Authorization")
    if auth_header:
        parts = auth_header.split()
        if len(parts) == 2 and parts[0].lower() == "bearer":
            return parts[1]

    # Strategy 2: Query parameter fallback (WebSocket upgrade)
    token_param: str | None = request.args.get("access_token")
    if token_param:
        return token_param

    return None


def get_current_user() -> dict[str, Any] | None:
    """Retrieve the authenticated user context for the current request.

    Resolution order:

    1. ``flask.g.current_user`` — if the JWT middleware has already
       validated the token and stored the user dict on ``g``.
    2. **Lazy validation** — extracts and validates the token from the
       ``Authorization`` header, caches the result on ``g.current_user``,
       and returns it.

    Returns:
        A dictionary with keys ``user_id``, ``email``, ``name``,
        ``roles``, ``tenant_id``, and ``permissions``; or ``None`` if
        no valid token is present.
    """
    # Check for already-cached user context.
    existing_user = getattr(g, "current_user", None)
    if existing_user is not None:
        return existing_user

    # Attempt to extract and validate the token.
    token = extract_token_from_request()
    if token is None:
        return None

    try:
        payload = validate_token(token)
    except AuthenticationError:
        return None

    user_context: dict[str, Any] = {
        "user_id": payload.get("sub", ""),
        "email": payload.get("email", ""),
        "name": payload.get("name", ""),
        "roles": payload.get("roles", []),
        "tenant_id": payload.get("tenant_id", ""),
        "permissions": payload.get("permissions", []),
    }

    # Cache on Flask g for the remainder of the request lifecycle.
    g.current_user = user_context
    return user_context


# ============================================================================
# Token refresh
# ============================================================================


def refresh_access_token(refresh_token: str) -> dict[str, Any]:
    """Exchange a refresh token for a new access token via Auth0.

    Sends a ``POST`` request to Auth0's ``/oauth/token`` endpoint with
    ``grant_type=refresh_token``.  Auth0's refresh-token rotation policy
    issues a new refresh token alongside the access token; the caller is
    responsible for persisting the new refresh token.

    Args:
        refresh_token: The OAuth 2.0 refresh token to exchange.

    Returns:
        A dictionary containing at minimum:

        - ``access_token`` (str): The new JWT access token.
        - ``expires_in`` (int): Token lifetime in seconds.
        - ``token_type`` (str): Typically ``"Bearer"``.

        Additional fields returned by Auth0 (e.g. ``refresh_token``,
        ``scope``) are passed through as-is.

    Raises:
        AuthenticationError: If the refresh-token exchange fails for
            any reason (invalid token, Auth0 downtime, network error).
    """
    domain = AUTH0_DOMAIN or os.environ.get("AUTH0_DOMAIN", "")
    client_id = _AUTH0_CLIENT_ID or os.environ.get("AUTH0_CLIENT_ID", "")
    client_secret = _AUTH0_CLIENT_SECRET or os.environ.get("AUTH0_CLIENT_SECRET", "")

    if not domain:
        raise AuthenticationError("AUTH0_DOMAIN is not configured. Cannot refresh token.")
    if not client_id:
        raise AuthenticationError("AUTH0_CLIENT_ID is not configured. Cannot refresh token.")

    token_url = f"{TOKEN_ISSUER_PREFIX}{domain}/oauth/token"

    payload: dict[str, str] = {
        "grant_type": "refresh_token",
        "client_id": client_id,
        "client_secret": client_secret,
        "refresh_token": refresh_token,
    }

    logger.info(
        "token_refresh_initiated",
        auth0_domain=domain,
    )

    try:
        response = requests.post(
            token_url,
            json=payload,
            headers={"Content-Type": "application/json"},
            timeout=_HTTP_TIMEOUT,
        )
    except requests.exceptions.Timeout as exc:
        logger.warning("token_refresh_timeout", error=str(exc))
        raise AuthenticationError(f"Timeout while refreshing access token: {exc}") from exc
    except requests.exceptions.ConnectionError as exc:
        logger.warning("token_refresh_connection_error", error=str(exc))
        raise AuthenticationError(f"Connection error during token refresh: {exc}") from exc
    except requests.exceptions.RequestException as exc:
        logger.warning("token_refresh_request_error", error=str(exc))
        raise AuthenticationError(f"Request error during token refresh: {exc}") from exc

    if response.status_code != 200:
        error_body = response.text
        logger.warning(
            "token_refresh_failed",
            status_code=response.status_code,
            error=error_body[:200],  # Truncate to avoid logging huge bodies.
        )
        raise AuthenticationError(f"Token refresh failed with status {response.status_code}: {error_body[:200]}")

    result: dict[str, Any] = response.json()

    logger.info(
        "token_refresh_successful",
        expires_in=result.get("expires_in"),
        token_type=result.get("token_type"),
    )

    return result


# ============================================================================
# Flask middleware integration
# ============================================================================


def jwt_required() -> Callable[..., Any]:
    """Create a Flask ``before_request`` handler that enforces JWT auth.

    The returned callable:

    1. Skips validation for health-check paths (``/health``, ``/ready``).
    2. Extracts the JWT from the ``Authorization: Bearer …`` header.
    3. Validates the token via :func:`validate_token`.
    4. Stores the decoded user context on ``flask.g.current_user`` and
       the tenant identifier on ``flask.g.tenant_id``.
    5. Returns a ``401 Unauthorized`` JSON response if any step fails.

    Returns:
        A callable suitable for use with ``@app.before_request`` or
        direct invocation inside a before-request hook.

    Example::

        app = Flask(__name__)


        @app.before_request
        def enforce_auth():
            handler = jwt_required()
            response = handler()
            if response is not None:
                return response
    """

    def _before_request() -> Any:
        """Validate JWT on every incoming request (except health checks).

        Returns:
            ``None`` if the request is authenticated (Flask continues to
            the route handler).  A ``(response, 401)`` tuple if the
            request is unauthenticated.
        """
        # Skip health-check endpoints (R-006).
        current_path: str = request.path
        if current_path in _HEALTH_CHECK_PATHS:
            return None

        token = extract_token_from_request()
        if token is None:
            logger.warning(
                "missing_authorization_header",
                path=current_path,
            )
            return (
                jsonify(
                    {
                        "error": "unauthorized",
                        "message": "Missing or malformed Authorization header.",
                    }
                ),
                401,
            )

        try:
            payload = validate_token(token)
        except TokenExpiredError:
            return (
                jsonify(
                    {
                        "error": "unauthorized",
                        "message": "Token has expired. Please re-authenticate.",
                    }
                ),
                401,
            )
        except InvalidTokenError as exc:
            logger.warning(
                "invalid_token",
                path=current_path,
                error=str(exc),
            )
            return (
                jsonify(
                    {
                        "error": "unauthorized",
                        "message": f"Invalid token: {exc.message}",
                    }
                ),
                401,
            )
        except AuthenticationError as exc:
            logger.warning(
                "authentication_error",
                path=current_path,
                error=str(exc),
            )
            return (
                jsonify(
                    {
                        "error": "unauthorized",
                        "message": str(exc),
                    }
                ),
                401,
            )

        # Store validated context on Flask g for downstream access.
        g.current_user = {
            "user_id": payload.get("sub", ""),
            "email": payload.get("email", ""),
            "name": payload.get("name", ""),
            "roles": payload.get("roles", []),
            "tenant_id": payload.get("tenant_id", ""),
            "permissions": payload.get("permissions", []),
        }
        g.tenant_id = payload.get("tenant_id", "")

        return None

    return _before_request


# ============================================================================
# Internal helpers
# ============================================================================


def _safe_extract_sub(token: str) -> str:
    """Best-effort extraction of the ``sub`` claim for logging.

    Used when we need to log which user's token failed validation but
    cannot decode the token via the normal validated path.  Falls back
    to ``"unknown"`` on any error.

    Args:
        token: The raw JWT string.

    Returns:
        The ``sub`` claim value, or ``"unknown"`` if extraction fails.
    """
    try:
        claims = jwt.get_unverified_claims(token)
        return str(claims.get("sub", "unknown"))
    except Exception:
        return "unknown"
