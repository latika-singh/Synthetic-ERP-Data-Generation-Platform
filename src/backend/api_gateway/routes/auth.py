"""Flask Blueprint module for Auth0 authentication REST endpoints.

This module implements authentication endpoints at /api/v1/auth/ providing
OAuth 2.0 / OpenID Connect integration with Auth0. The endpoints handle
user login, callback processing, logout, token refresh, and profile management.

Endpoints:
    GET  /login    - Redirect to Auth0 login page (public)
    GET  /callback - Handle Auth0 authorization code callback (public)
    POST /logout   - Logout and clear session (JWT required)
    POST /refresh  - Refresh JWT access token with rotation (public)
    GET  /profile  - Retrieve authenticated user profile (JWT required)
    PUT  /profile  - Update authenticated user profile (JWT required)

Security:
    - Login and callback endpoints are exempt from JWT validation
    - All other endpoints require valid JWT Bearer token
    - CSRF protection via state parameter on OAuth flow
    - Never logs tokens or credentials (R-005)
    - Auth0 RS256 JWT tokens with short expiry and refresh token rotation

References:
    - R-005: Privacy-first design — no credentials logged
    - R-006: Security by default — JWT on protected endpoints
    - R-013: Google-style docstrings and type hints
"""

import secrets
from typing import Optional

from flask import Blueprint, current_app, g, jsonify, redirect, request
from flask_jwt_extended import get_jwt_identity, jwt_required
from pydantic import ValidationError

from api_gateway.schemas.auth import (
    LoginResponse,
    TokenRefreshRequest,
    TokenRefreshResponse,
    UserProfile,
)
from api_gateway.services.auth_service import AuthService
from shared.logging.structured_logger import get_logger

# ---------------------------------------------------------------------------
# Module-level setup
# ---------------------------------------------------------------------------

# Structured JSON logger — never logs tokens or credentials (R-005)
logger = get_logger(__name__)

# Blueprint registered at /api/v1/auth by the routes package __init__.py
auth_bp = Blueprint("auth", __name__)

# Business-logic delegate — instantiated once at module load
auth_service = AuthService()

# CSRF state management constants
_CSRF_STATE_PREFIX: str = "auth:csrf_state:"
_CSRF_STATE_TTL_SECONDS: int = 600  # 10-minute expiry for CSRF tokens

# Whitelist of fields a user is allowed to update on their own profile.
# Role and permissions cannot be self-modified — admin endpoints only.
_PROFILE_UPDATABLE_FIELDS: set = {"name", "avatar_url", "preferences"}


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------


def _get_redis_client():
    """Retrieve the Redis client from Flask application extensions.

    The Redis client is initialised by the extensions module and attached to
    ``current_app.extensions['redis']`` during app startup.

    Returns:
        The Redis client instance, or ``None`` when Redis is unavailable.
    """
    try:
        return current_app.extensions.get("redis")
    except (RuntimeError, AttributeError):
        logger.warning(
            "redis_client_unavailable",
            reason="Flask app context or Redis extension not found",
        )
        return None


def _store_csrf_state(state: str, return_url: Optional[str] = None) -> None:
    """Store a CSRF state token in Redis with a TTL.

    The token is consumed (deleted) when validated during the callback to
    prevent replay attacks.

    Args:
        state: A cryptographically random state token.
        return_url: Optional URL to redirect the user to after login.
    """
    redis_client = _get_redis_client()
    if redis_client is not None:
        try:
            value = return_url or ""
            redis_client.setex(
                f"{_CSRF_STATE_PREFIX}{state}",
                _CSRF_STATE_TTL_SECONDS,
                value,
            )
        except Exception as exc:
            logger.error("csrf_state_store_failed", error=str(exc))


def _validate_and_consume_csrf_state(state: str) -> Optional[str]:
    """Validate and consume a CSRF state token stored in Redis.

    Tokens are single-use: once validated the key is deleted so it cannot be
    reused. Returns the optional ``return_url`` that was stored alongside the
    state during the login step.

    Args:
        state: The CSRF state token received from the Auth0 callback.

    Returns:
        The ``return_url`` stored with the state (may be an empty string),
        or ``None`` if the state is invalid, expired, or already consumed.
    """
    redis_client = _get_redis_client()
    if redis_client is not None:
        try:
            key = f"{_CSRF_STATE_PREFIX}{state}"
            value = redis_client.get(key)
            if value is not None:
                # Consume: delete the key so it cannot be replayed
                redis_client.delete(key)
                if isinstance(value, bytes):
                    return value.decode("utf-8")
                return str(value)
        except Exception as exc:
            logger.error("csrf_state_validate_failed", error=str(exc))
    return None


def _get_callback_url() -> str:
    """Construct the Auth0 callback URL.

    Prefers the explicit ``AUTH0_CALLBACK_URL`` configuration value.  When
    that is not set, falls back to building the URL from the current request
    context (respecting ``X-Forwarded-*`` headers for reverse-proxy setups).

    Returns:
        The absolute URL to use as ``redirect_uri`` in the OAuth flow.
    """
    configured_url: Optional[str] = current_app.config.get("AUTH0_CALLBACK_URL")
    if configured_url:
        return configured_url
    # Fallback: derive from request headers (supports reverse proxies)
    scheme = request.headers.get("X-Forwarded-Proto", request.scheme)
    host = request.headers.get("X-Forwarded-Host", request.host)
    return f"{scheme}://{host}/api/v1/auth/callback"


# ---------------------------------------------------------------------------
# Route: GET /login
# ---------------------------------------------------------------------------


@auth_bp.route("/login", methods=["GET"])
def login():
    """Initiate the Auth0 login flow.

    Generates a cryptographic CSRF state token, stores it in Redis with a
    TTL, builds the Auth0 ``/authorize`` URL, and returns it for the client
    to redirect to.  Alternatively, if ``server_redirect=true`` is provided,
    performs a server-side 302 redirect directly.

    **This endpoint is public and exempt from JWT validation.**

    Query Parameters:
        return_url (str, optional): Where to redirect after successful login.
        server_redirect (str, optional): ``"true"`` for a 302 redirect.

    Returns:
        tuple: JSON body with ``auth_url`` and HTTP 200, **or** a 302
        redirect response when *server_redirect* is ``"true"``.

    Example Response::

        {
            "auth_url": "https://tenant.auth0.com/authorize?...",
            "state": "random-csrf-token",
            "message": "Redirect to auth_url to begin login"
        }
    """
    try:
        # Parse optional query parameters
        return_url: Optional[str] = request.args.get("return_url")
        server_redirect_flag: bool = (
            request.args.get("server_redirect", "false").lower() == "true"
        )

        # Generate a cryptographic CSRF state token and persist in Redis
        state: str = secrets.token_urlsafe(32)
        _store_csrf_state(state, return_url)

        # Build the Auth0 callback (redirect_uri) URL
        callback_url: str = _get_callback_url()

        # Delegate Auth0 authorize-URL construction to the service layer
        auth_url: str = auth_service.get_login_url(
            redirect_uri=callback_url,
            state=state,
        )

        logger.info(
            "login_initiated",
            has_return_url=return_url is not None,
            server_redirect=server_redirect_flag,
        )

        # Server-side redirect (traditional web-app flow)
        if server_redirect_flag:
            return redirect(auth_url)

        # Client-side redirect (SPA / React flow — default)
        return jsonify(
            {
                "auth_url": auth_url,
                "state": state,
                "message": "Redirect to auth_url to begin login",
            }
        ), 200

    except Exception as exc:
        logger.error("login_initiation_failed", error=str(exc))
        return jsonify(
            {
                "error": "login_failed",
                "message": "Failed to initiate login flow",
                "details": str(exc),
            }
        ), 500


# ---------------------------------------------------------------------------
# Route: GET /callback
# ---------------------------------------------------------------------------


@auth_bp.route("/callback", methods=["GET"])
def callback():
    """Handle the Auth0 authorization-code callback.

    Auth0 redirects the user here after authentication.  The handler:

    1. Checks for Auth0-returned errors.
    2. Validates the CSRF state token against Redis (single-use).
    3. Exchanges the authorization code for JWT tokens via
       :meth:`AuthService.handle_callback`.
    4. Returns a :class:`LoginResponse` containing the access token,
       refresh token, expiry, and the user profile.

    **This endpoint is public and exempt from JWT validation.**

    Query Parameters:
        code (str): The authorization code issued by Auth0.
        state (str): The CSRF state token generated during ``/login``.
        error (str, optional): Auth0 error code on failure.
        error_description (str, optional): Human-readable error message.

    Returns:
        tuple: :class:`LoginResponse` JSON with HTTP 200 on success.

    Error Responses:
        400: Missing code/state or Auth0-returned error.
        401: Invalid/expired CSRF state token.
        500: Unexpected processing failure.
    """
    try:
        # ----- Auth0-returned errors -----
        auth_error: Optional[str] = request.args.get("error")
        if auth_error:
            error_description = request.args.get(
                "error_description", "Unknown Auth0 error"
            )
            logger.warning(
                "auth0_callback_error",
                auth_error=auth_error,
                error_description=error_description,
            )
            return jsonify(
                {
                    "error": "auth0_error",
                    "message": error_description,
                    "auth0_error_code": auth_error,
                }
            ), 400

        # ----- Extract required query parameters -----
        code: Optional[str] = request.args.get("code")
        state: Optional[str] = request.args.get("state")

        if not code:
            logger.warning("callback_missing_authorization_code")
            return jsonify(
                {
                    "error": "invalid_request",
                    "message": "Authorization code is required",
                }
            ), 400

        if not state:
            logger.warning("callback_missing_state_parameter")
            return jsonify(
                {
                    "error": "invalid_request",
                    "message": "State parameter is required for CSRF protection",
                }
            ), 400

        # ----- CSRF state validation (single-use) -----
        stored_return_url = _validate_and_consume_csrf_state(state)
        if stored_return_url is None:
            logger.warning("callback_invalid_or_expired_state")
            return jsonify(
                {
                    "error": "invalid_state",
                    "message": (
                        "Invalid or expired state parameter. "
                        "Please restart the login flow."
                    ),
                }
            ), 401

        # ----- Exchange code for tokens -----
        callback_url: str = _get_callback_url()
        token_data: dict = auth_service.handle_callback(
            authorization_code=code,
            redirect_uri=callback_url,
        )

        # Build a validated Pydantic response model
        login_response = LoginResponse.model_validate(
            {
                "access_token": token_data.get("access_token", ""),
                "refresh_token": token_data.get("refresh_token", ""),
                "token_type": token_data.get("token_type", "Bearer"),
                "expires_in": token_data.get("expires_in", 3600),
                "user": token_data.get("user"),
            }
        )

        # Log success without exposing tokens (R-005)
        user_info: dict = token_data.get("user") or {}
        logger.info(
            "login_callback_successful",
            user_id=user_info.get("user_id"),
            has_return_url=bool(stored_return_url),
        )

        response_body: dict = login_response.model_dump(mode="json")

        # Append the optional return_url so the client can redirect
        if stored_return_url:
            response_body["return_url"] = stored_return_url

        return jsonify(response_body), 200

    except ValidationError as exc:
        logger.error(
            "callback_response_validation_error",
            errors=exc.errors(),
        )
        return jsonify(
            {
                "error": "validation_error",
                "message": "Failed to validate authentication response",
                "details": exc.errors(),
            }
        ), 500

    except Exception as exc:
        logger.error("callback_processing_failed", error=str(exc))
        return jsonify(
            {
                "error": "callback_failed",
                "message": "Failed to process authentication callback",
                "details": str(exc),
            }
        ), 500


# ---------------------------------------------------------------------------
# Route: POST /logout
# ---------------------------------------------------------------------------


@auth_bp.route("/logout", methods=["POST"])
@jwt_required()
def logout():
    """Logout the authenticated user and clear session data.

    Clears server-side session data from Redis, constructs the Auth0
    ``/v2/logout`` URL for client-side redirect, and returns a confirmation
    message.

    **Requires a valid JWT Bearer token.**

    Returns:
        tuple: JSON with ``logout_url`` and ``message``, HTTP 200.

    Example Response::

        {
            "logout_url": "https://tenant.auth0.com/v2/logout?...",
            "message": "Logout successful"
        }
    """
    try:
        # Extract user identity — prefer g.user_id set by auth middleware,
        # fall back to get_jwt_identity() for flexibility.
        user_id: str = getattr(g, "user_id", None) or get_jwt_identity()

        # Delegate session cleanup and Auth0 logout-URL generation
        result: dict = auth_service.logout(user_id)

        logger.info("user_logged_out", user_id=user_id)

        return jsonify(
            {
                "logout_url": result.get("logout_url", ""),
                "message": result.get("message", "Logout successful"),
            }
        ), 200

    except Exception as exc:
        logger.error("logout_processing_failed", error=str(exc))
        return jsonify(
            {
                "error": "logout_failed",
                "message": "Failed to process logout request",
                "details": str(exc),
            }
        ), 500


# ---------------------------------------------------------------------------
# Route: POST /refresh
# ---------------------------------------------------------------------------


@auth_bp.route("/refresh", methods=["POST"])
def refresh_token():
    """Refresh a JWT access token using a refresh token.

    Accepts a refresh token in the request body, exchanges it for a new
    access token and a new refresh token (rotation) via Auth0.  This
    endpoint is **public** because the caller's access token may already
    be expired.

    Request Body (JSON):
        refresh_token (str): A valid Auth0 refresh token.

    Returns:
        tuple: :class:`TokenRefreshResponse` JSON with HTTP 200.

    Error Responses:
        400: Missing or empty request body.
        401: Expired or invalid refresh token.
        422: Pydantic validation failure on request body.
        500: Unexpected processing failure.

    Example Request::

        {"refresh_token": "v1.MjE…"}

    Example Response::

        {
            "access_token": "eyJhbGci…",
            "refresh_token": "v1.NWY…",
            "token_type": "Bearer",
            "expires_in": 3600
        }
    """
    try:
        # Parse JSON body
        body: Optional[dict] = request.get_json(silent=True)
        if not body:
            return jsonify(
                {
                    "error": "invalid_request",
                    "message": "Request body is required with a refresh_token field",
                }
            ), 400

        # Validate against Pydantic schema
        refresh_request = TokenRefreshRequest.model_validate(body)

        # Exchange refresh token for new tokens via Auth0
        token_data: dict = auth_service.refresh_token(
            refresh_token=refresh_request.refresh_token,
        )

        # Build validated response model
        refresh_response = TokenRefreshResponse.model_validate(
            {
                "access_token": token_data.get("access_token", ""),
                "refresh_token": token_data.get("refresh_token", ""),
                "token_type": token_data.get("token_type", "Bearer"),
                "expires_in": token_data.get("expires_in", 3600),
            }
        )

        # Log without exposing token values (R-005)
        logger.info("access_token_refreshed")

        return jsonify(refresh_response.model_dump(mode="json")), 200

    except ValidationError as exc:
        logger.warning("refresh_request_validation_error", errors=exc.errors())
        return jsonify(
            {
                "error": "validation_error",
                "message": "Invalid request body",
                "details": exc.errors(),
            }
        ), 422

    except Exception as exc:
        # Distinguish expired/invalid refresh tokens from unexpected errors
        error_lower = str(exc).lower()
        if any(
            keyword in error_lower
            for keyword in ("expired", "invalid", "unauthorized", "revoked")
        ):
            logger.warning("refresh_token_expired_or_invalid")
            return jsonify(
                {
                    "error": "invalid_refresh_token",
                    "message": (
                        "Refresh token is expired or invalid. "
                        "Please log in again."
                    ),
                }
            ), 401

        logger.error("token_refresh_failed", error=str(exc))
        return jsonify(
            {
                "error": "refresh_failed",
                "message": "Failed to refresh access token",
                "details": str(exc),
            }
        ), 500


# ---------------------------------------------------------------------------
# Route: GET /profile
# ---------------------------------------------------------------------------


@auth_bp.route("/profile", methods=["GET"])
@jwt_required()
def get_profile():
    """Retrieve the authenticated user's profile.

    Returns the full user profile, including identity fields, assigned role,
    granted permissions, tenant association, and timestamps.

    **Requires a valid JWT Bearer token.**

    Returns:
        tuple: :class:`UserProfile` JSON with HTTP 200.

    Error Responses:
        404: User profile not found in MongoDB.
        500: Unexpected processing failure.

    Example Response::

        {
            "user_id": "auth0|abc123",
            "email": "user@example.com",
            "name": "John Doe",
            "role": "data_engineer",
            "permissions": ["generation:create", "generation:read"],
            "tenant_id": "tenant-abc",
            "avatar_url": "https://…",
            "is_active": true,
            "created_at": "2025-01-01T00:00:00Z",
            "updated_at": "2025-01-15T12:00:00Z",
            "last_login_at": "2025-02-13T10:30:00Z"
        }
    """
    try:
        # Extract user identity from JWT claims
        user_id: str = get_jwt_identity()

        # Delegate profile retrieval to the service layer
        user_data: Optional[dict] = auth_service.get_user_profile(user_id)

        if user_data is None:
            logger.warning("user_profile_not_found", user_id=user_id)
            return jsonify(
                {
                    "error": "not_found",
                    "message": "User profile not found",
                }
            ), 404

        # Validate and serialise using Pydantic
        user_profile = UserProfile.model_validate(user_data)

        logger.info("user_profile_retrieved", user_id=user_id)

        return jsonify(user_profile.model_dump(mode="json")), 200

    except ValidationError as exc:
        logger.error("profile_data_validation_error", errors=exc.errors())
        return jsonify(
            {
                "error": "validation_error",
                "message": "Failed to validate user profile data",
                "details": exc.errors(),
            }
        ), 500

    except Exception as exc:
        logger.error("profile_retrieval_failed", error=str(exc))
        return jsonify(
            {
                "error": "profile_retrieval_failed",
                "message": "Failed to retrieve user profile",
                "details": str(exc),
            }
        ), 500


# ---------------------------------------------------------------------------
# Route: PUT /profile
# ---------------------------------------------------------------------------


@auth_bp.route("/profile", methods=["PUT"])
@jwt_required()
def update_profile():
    """Update the authenticated user's profile.

    Allows self-service updates to a restricted set of fields:
    ``name``, ``avatar_url``, and ``preferences``.  Sensitive fields such
    as ``role``, ``permissions``, and ``tenant_id`` are **not** modifiable
    through this endpoint — they require admin-level operations.

    **Requires a valid JWT Bearer token.**

    Request Body (JSON):
        name (str, optional): Updated display name.
        avatar_url (str, optional): Updated avatar image URL.
        preferences (dict, optional): Updated user preferences.

    Returns:
        tuple: Updated :class:`UserProfile` JSON with HTTP 200.

    Error Responses:
        400: Empty body or no valid updatable fields.
        404: User profile not found.
        422: Pydantic validation failure.
        500: Unexpected processing failure.

    Example Request::

        {
            "name": "Jane Doe",
            "avatar_url": "https://example.com/avatar.png",
            "preferences": {"theme": "dark", "notifications_enabled": true}
        }
    """
    try:
        # Extract identity and tenant context
        user_id: str = get_jwt_identity()
        tenant_id: str = getattr(g, "tenant_id", "") or ""

        # Parse JSON body
        body: Optional[dict] = request.get_json(silent=True)
        if not body:
            return jsonify(
                {
                    "error": "invalid_request",
                    "message": "Request body is required",
                }
            ), 400

        # Filter to only the whitelisted updatable fields
        updates: dict = {
            key: value
            for key, value in body.items()
            if key in _PROFILE_UPDATABLE_FIELDS
        }

        if not updates:
            return jsonify(
                {
                    "error": "invalid_request",
                    "message": (
                        "No valid updatable fields provided. "
                        "Allowed fields: name, avatar_url, preferences"
                    ),
                }
            ), 400

        # Delegate update to service layer
        updated_data: Optional[dict] = auth_service.update_user_profile(
            user_id=user_id,
            tenant_id=tenant_id,
            updates=updates,
        )

        if updated_data is None:
            logger.warning("profile_update_target_not_found", user_id=user_id)
            return jsonify(
                {
                    "error": "not_found",
                    "message": "User profile not found",
                }
            ), 404

        # Validate and serialise the updated profile
        updated_profile = UserProfile.model_validate(updated_data)

        logger.info(
            "user_profile_updated",
            user_id=user_id,
            updated_fields=list(updates.keys()),
        )

        return jsonify(updated_profile.model_dump(mode="json")), 200

    except ValidationError as exc:
        logger.warning("profile_update_validation_error", errors=exc.errors())
        return jsonify(
            {
                "error": "validation_error",
                "message": "Invalid profile update data",
                "details": exc.errors(),
            }
        ), 422

    except Exception as exc:
        logger.error("profile_update_failed", error=str(exc))
        return jsonify(
            {
                "error": "profile_update_failed",
                "message": "Failed to update user profile",
                "details": str(exc),
            }
        ), 500
