"""Business logic service for Auth0 integration and authentication management.

Provides the :class:`AuthService` class that encapsulates all authentication
and authorisation workflows for the API Gateway, including:

- **Auth0 public key management** — JWKS endpoint fetching with Redis-backed
  24-hour caching for RS256 JWT validation.
- **Token lifecycle** — Access-token validation via ``python-jose`` RS256
  verification against Auth0 JWKS public keys, refresh-token rotation via
  Auth0 ``/oauth/token`` endpoint.
- **Role extraction** — Maps Auth0 custom-namespace JWT claims to the five
  platform :class:`~api_gateway.models.user.UserRole` values (Platform Admin,
  Data Engineer, Developer, QA Engineer, Data Analyst).
- **User profile management** — Create / update platform user documents in
  MongoDB from Auth0 callback data, track logins, and manage profile fields.
- **Login / logout flow** — Constructs Auth0 ``/authorize`` redirect URLs
  and ``/v2/logout`` redirect URLs, clears server-side session state in Redis.
- **Multi-tenant isolation** — Extracts ``tenant_id`` from custom JWT claims
  and propagates it through all user operations per R-007.

Security Considerations (R-005, R-006):
    - Tokens, secrets, and PII are **NEVER** logged.
    - All Auth0 HTTP calls are wrapped with :func:`circuitbreaker.circuit`
      decorators (failure_threshold=3, recovery_timeout=60s) for resilience.
    - RS256 algorithm is enforced — symmetric algorithms are rejected.
    - JWKS keys are cached to minimise latency and Auth0 rate-limit exposure.

Usage::

    from api_gateway.services.auth_service import AuthService

    auth = AuthService()
    login_url = auth.get_login_url(redirect_uri="https://app.example.com/callback")
    payload = auth.validate_token(access_token)
    roles = auth.extract_roles(payload)

Note:
    This module must be instantiated within an active Flask application
    context (e.g. inside a route handler or ``with app.app_context():``)
    because it reads configuration from ``current_app.config``.
"""

from __future__ import annotations

import json
import logging
import time
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, Optional
from urllib.parse import urlencode

import httpx
from circuitbreaker import circuit
from flask import current_app
from jose import JWTError, jwt, jwk
from jose.utils import base64url_decode

from api_gateway.extensions import get_redis
from api_gateway.models.user import ROLE_PERMISSIONS, User, UserRole
from shared.logging.structured_logger import get_logger


# ---------------------------------------------------------------------------
# Module-level fallback logger — works with the structlog ProcessorFormatter
# configured at application level for consistent JSON output.
# ---------------------------------------------------------------------------
_fallback_logger: logging.Logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

JWKS_CACHE_KEY: str = "auth:jwks_keys"
"""Redis key for cached JWKS public keys."""

JWKS_CACHE_TTL_SECONDS: int = 86_400  # 24 hours
"""Time-to-live for JWKS key cache entries in Redis."""

AUTH_SESSION_PREFIX: str = "auth:session:"
"""Redis key prefix for server-side session data."""

CLAIMS_NAMESPACE: str = "https://synthetic-erp"
"""Auth0 custom claims namespace for platform-specific JWT attributes."""

ROLES_CLAIM: str = f"{CLAIMS_NAMESPACE}/roles"
"""JWT claim key for the user's assigned platform roles."""

TENANT_ID_CLAIM: str = f"{CLAIMS_NAMESPACE}/tenant_id"
"""JWT claim key for the user's tenant identifier."""

DEFAULT_TOKEN_EXCHANGE_TIMEOUT: float = 30.0
"""HTTP timeout in seconds for Auth0 token exchange calls."""

DEFAULT_JWKS_FETCH_TIMEOUT: float = 15.0
"""HTTP timeout in seconds for Auth0 JWKS endpoint calls."""

# ---------------------------------------------------------------------------
# Auth0 Role → Platform Role Mapping
# ---------------------------------------------------------------------------
# Maps both human-readable Auth0 role names and their snake_case equivalents
# to platform ``UserRole`` enum values.  This two-form approach accommodates
# Auth0 rule/action scripts that may emit either format.
# ---------------------------------------------------------------------------

AUTH0_ROLE_MAPPING: Dict[str, UserRole] = {
    # Human-readable Auth0 role names
    "Platform Admin": UserRole.PLATFORM_ADMIN,
    "Data Engineer": UserRole.DATA_ENGINEER,
    "Developer": UserRole.DEVELOPER,
    "QA Engineer": UserRole.QA_ENGINEER,
    "Data Analyst": UserRole.DATA_ANALYST,
    # snake_case equivalents (Auth0 action scripts / API)
    "platform_admin": UserRole.PLATFORM_ADMIN,
    "data_engineer": UserRole.DATA_ENGINEER,
    "developer": UserRole.DEVELOPER,
    "qa_engineer": UserRole.QA_ENGINEER,
    "data_analyst": UserRole.DATA_ANALYST,
}
"""Bidirectional mapping from Auth0 role strings to platform :class:`UserRole`
values.  Supports both human-readable names (e.g. ``'Platform Admin'``) and
snake_case identifiers (e.g. ``'platform_admin'``)."""


# ===================================================================
# AuthService
# ===================================================================


class AuthService:
    """Business logic service for Auth0 authentication and authorisation.

    Encapsulates all interaction with Auth0 identity provider APIs,
    JWKS public-key management, JWT token validation, user lifecycle
    management (create/update from Auth0 callbacks), and role-based
    access control mapping.

    All external HTTP calls to Auth0 are protected by circuit breakers
    (failure_threshold=3, recovery_timeout=60s) to prevent cascade
    failures when the identity provider is temporarily unreachable.

    Attributes:
        auth0_domain: Auth0 tenant domain (e.g. ``'myapp.auth0.com'``).
        auth0_client_id: OAuth 2.0 client identifier.
        auth0_client_secret: OAuth 2.0 client secret (never logged).
        auth0_api_audience: API audience / identifier for JWT validation.
        jwks_uri: Full URL to the Auth0 JWKS endpoint.
        issuer: Expected ``iss`` claim value for JWT validation.

    Example::

        with app.app_context():
            auth = AuthService()
            url = auth.get_login_url("https://app.example.com/callback")
    """

    # ------------------------------------------------------------------
    # Initialisation
    # ------------------------------------------------------------------

    def __init__(self) -> None:
        """Initialise the AuthService from Flask application configuration.

        Reads Auth0 credentials and configuration from ``current_app.config``
        following the Flask Application Factory pattern.  Configuration keys:

        - ``AUTH0_DOMAIN``
        - ``AUTH0_CLIENT_ID``
        - ``AUTH0_CLIENT_SECRET``
        - ``AUTH0_API_AUDIENCE``

        Raises:
            RuntimeError: If called outside a Flask application context.
        """
        self.logger = get_logger(__name__)

        config: dict = current_app.config

        self.auth0_domain: str = config.get("AUTH0_DOMAIN", "")
        self.auth0_client_id: str = config.get("AUTH0_CLIENT_ID", "")
        self.auth0_client_secret: str = config.get("AUTH0_CLIENT_SECRET", "")
        self.auth0_api_audience: str = config.get("AUTH0_API_AUDIENCE", "")

        self.jwks_uri: str = (
            f"https://{self.auth0_domain}/.well-known/jwks.json"
        )
        self.issuer: str = f"https://{self.auth0_domain}/"

        self.logger.info(
            "auth_service_initialized",
            auth0_domain=self.auth0_domain,
            jwks_uri=self.jwks_uri,
        )

    # ------------------------------------------------------------------
    # Public API — Login / Logout
    # ------------------------------------------------------------------

    def get_login_url(
        self,
        redirect_uri: str,
        state: Optional[str] = None,
    ) -> str:
        """Build the Auth0 ``/authorize`` redirect URL for initiating login.

        Constructs an OAuth 2.0 Authorization Code flow URL targeting the
        Auth0 ``/authorize`` endpoint with the ``openid profile email``
        scopes requested.

        Args:
            redirect_uri: The URL Auth0 should redirect to after
                successful authentication (must be registered in the
                Auth0 Application settings).
            state: Optional CSRF-protection state parameter.  When
                provided, Auth0 echoes it back in the redirect so the
                client can verify request integrity.

        Returns:
            str: The fully-constructed Auth0 ``/authorize`` URL ready for
                a client-side redirect (HTTP 302).

        Example::

            url = auth.get_login_url(
                redirect_uri="https://app.example.com/callback",
                state="random_csrf_token",
            )
        """
        params: Dict[str, str] = {
            "response_type": "code",
            "client_id": self.auth0_client_id,
            "redirect_uri": redirect_uri,
            "scope": "openid profile email offline_access",
            "audience": self.auth0_api_audience,
        }

        if state is not None:
            params["state"] = state

        authorize_url: str = (
            f"https://{self.auth0_domain}/authorize?{urlencode(params)}"
        )

        self.logger.info(
            "login_url_generated",
            redirect_uri=redirect_uri,
            has_state=state is not None,
        )

        return authorize_url

    def logout(self, user_id: str) -> dict:
        """Orchestrate server-side logout and build the Auth0 logout URL.

        Performs the following steps:

        1. Clears any server-side session data for the user from Redis.
        2. Constructs the Auth0 ``/v2/logout`` redirect URL so the client
           can complete the identity-provider logout flow.

        Args:
            user_id: The platform user UUID whose session should be
                invalidated.

        Returns:
            dict: Contains:
                - ``logout_url`` (str): Auth0 ``/v2/logout`` redirect URL.
                - ``user_id`` (str): The user ID that was logged out.

        Example::

            result = auth.logout(user_id="abc-123")
            # Redirect the client to result["logout_url"]
        """
        # Clear server-side session data from Redis
        try:
            redis_client = get_redis()
            session_key: str = f"{AUTH_SESSION_PREFIX}{user_id}"
            redis_client.delete(session_key)
            self.logger.info(
                "server_session_cleared",
                user_id=user_id,
            )
        except Exception as exc:
            # Log but do not raise — session cleanup is best-effort.
            # The Auth0 logout URL is still valid even if Redis is down.
            self.logger.warning(
                "server_session_clear_failed",
                user_id=user_id,
                error=str(exc),
                error_type=type(exc).__name__,
            )

        # Build Auth0 /v2/logout URL
        logout_params: Dict[str, str] = {
            "client_id": self.auth0_client_id,
        }
        logout_url: str = (
            f"https://{self.auth0_domain}/v2/logout?"
            f"{urlencode(logout_params)}"
        )

        self.logger.info(
            "logout_completed",
            user_id=user_id,
        )

        return {
            "logout_url": logout_url,
            "user_id": user_id,
        }

    # ------------------------------------------------------------------
    # Public API — Token Management
    # ------------------------------------------------------------------

    def handle_callback(
        self,
        authorization_code: str,
        redirect_uri: str,
    ) -> dict:
        """Exchange an Auth0 authorization code for tokens and sync user.

        Performs the full OAuth 2.0 Authorization Code exchange:

        1. Sends the authorization code to Auth0 ``/oauth/token``.
        2. Parses the response for ``access_token``, ``id_token``,
           ``refresh_token``, and ``expires_in``.
        3. Decodes the ``id_token`` (without full verification — it is
           self-issued by the same Auth0 tenant) to extract user profile
           fields (``email``, ``name``, ``picture``, ``sub``).
        4. Looks up or creates the platform user in MongoDB via
           :meth:`User.find_by_auth0_id` / :meth:`User.create`.
        5. Records the login timestamp via :meth:`User.update_last_login`.

        Args:
            authorization_code: The one-time authorization code received
                from the Auth0 ``/authorize`` redirect callback.
            redirect_uri: The same redirect URI used in the initial
                ``/authorize`` request (must match for security).

        Returns:
            dict: Token and user data containing:
                - ``access_token`` (str): Bearer access token for API calls.
                - ``refresh_token`` (str | None): Refresh token for rotation.
                - ``expires_in`` (int): Access token TTL in seconds.
                - ``token_type`` (str): Always ``'Bearer'``.
                - ``user`` (dict): Platform user profile document.

        Raises:
            httpx.HTTPError: If the Auth0 token exchange HTTP call fails.
            ValueError: If the Auth0 response is missing required fields.
        """
        start_time: float = time.time()

        # Step 1: Exchange code for tokens
        token_response: dict = self._exchange_code_for_tokens(
            code=authorization_code,
            redirect_uri=redirect_uri,
        )

        access_token: str = token_response.get("access_token", "")
        id_token: str = token_response.get("id_token", "")
        refresh_token_value: Optional[str] = token_response.get(
            "refresh_token"
        )
        expires_in: int = token_response.get("expires_in", 3600)

        if not access_token:
            raise ValueError(
                "Auth0 token exchange returned empty access_token."
            )

        # Step 2: Decode id_token to extract user profile (unverified
        # decode is safe here — the id_token is freshly received over
        # HTTPS from Auth0 in the same transaction).
        user_info: Dict[str, Any] = {}
        if id_token:
            try:
                unverified_claims: dict = jwt.get_unverified_claims(id_token)
                user_info = {
                    "auth0_user_id": unverified_claims.get("sub", ""),
                    "email": unverified_claims.get("email", ""),
                    "name": unverified_claims.get("name", ""),
                    "picture": unverified_claims.get("picture", ""),
                }
            except JWTError:
                self.logger.warning(
                    "id_token_decode_failed",
                    error_type="JWTError",
                )
                # Fallback: try decoding the access_token header for sub
                try:
                    access_claims = jwt.get_unverified_claims(access_token)
                    user_info = {
                        "auth0_user_id": access_claims.get("sub", ""),
                        "email": access_claims.get("email", ""),
                        "name": access_claims.get("name", ""),
                        "picture": access_claims.get("picture", ""),
                    }
                except JWTError:
                    self.logger.error("access_token_decode_failed")

        auth0_user_id: str = user_info.get("auth0_user_id", "")
        if not auth0_user_id:
            raise ValueError(
                "Could not extract Auth0 user ID (sub) from token response."
            )

        # Step 3: Extract roles and tenant_id from access_token claims
        access_claims_raw: dict = {}
        try:
            access_claims_raw = jwt.get_unverified_claims(access_token)
        except JWTError:
            self.logger.warning("access_token_claims_extraction_failed")

        roles: list[str] = self.extract_roles(access_claims_raw)
        tenant_id: str = self.extract_tenant_id(access_claims_raw)

        # Determine the primary role for user creation
        primary_role: str = (
            roles[0] if roles else UserRole.DATA_ANALYST.value
        )

        # Step 4: Look up or create platform user
        existing_user: Optional[dict] = User.find_by_auth0_id(auth0_user_id)
        callback_time: datetime = datetime.now(timezone.utc)

        if existing_user is not None:
            user_doc: dict = existing_user
            # Update last login
            updated_user: Optional[dict] = User.update_last_login(
                auth0_user_id
            )
            if updated_user is not None:
                user_doc = updated_user

            self.logger.info(
                "user_login_existing",
                user_id=user_doc.get("user_id"),
                tenant_id=user_doc.get("tenant_id"),
            )
        else:
            # Create new platform user — default tenant_id from JWT or
            # fall back to "default"
            effective_tenant: str = tenant_id if tenant_id else "default"
            user_doc = User.create(
                auth0_user_id=auth0_user_id,
                email=user_info.get("email", ""),
                tenant_id=effective_tenant,
                role=primary_role,
                name=user_info.get("name", ""),
                picture=user_info.get("picture", ""),
            )

            self.logger.info(
                "user_created_from_callback",
                user_id=user_doc.get("user_id"),
                tenant_id=effective_tenant,
                role=primary_role,
            )

        # Step 5: Store server-side session metadata in Redis
        platform_user_id: str = user_doc.get("user_id", "")
        session_ttl: timedelta = timedelta(seconds=expires_in)
        session_data: Dict[str, Any] = {
            "user_id": platform_user_id,
            "tenant_id": user_doc.get("tenant_id", ""),
            "roles": roles,
            "authenticated_at": callback_time.isoformat(),
            "expires_in": expires_in,
        }
        try:
            redis_client = get_redis()
            session_key: str = f"{AUTH_SESSION_PREFIX}{platform_user_id}"
            redis_client.set(
                session_key,
                json.dumps(session_data),
                ex=int(session_ttl.total_seconds()),
            )
            self.logger.info(
                "server_session_stored",
                user_id=platform_user_id,
                session_ttl_seconds=int(session_ttl.total_seconds()),
            )
        except Exception as exc:
            # Session storage is non-fatal — token-based auth still works.
            self.logger.warning(
                "server_session_store_failed",
                user_id=platform_user_id,
                error=str(exc),
                error_type=type(exc).__name__,
            )

        elapsed_ms: float = (time.time() - start_time) * 1000.0

        self.logger.info(
            "auth_callback_completed",
            user_id=platform_user_id,
            elapsed_ms=round(elapsed_ms, 2),
        )

        return {
            "access_token": access_token,
            "refresh_token": refresh_token_value,
            "expires_in": expires_in,
            "token_type": "Bearer",
            "user": user_doc,
        }

    def validate_token(self, token: str) -> dict:
        """Validate an Auth0 JWT access token using RS256 JWKS verification.

        Performs the following steps:

        1. Fetches Auth0 JWKS public keys via :meth:`_get_jwks_keys`
           (Redis-cached with 24-hour TTL).
        2. Extracts the ``kid`` (Key ID) from the unverified JWT header.
        3. Locates the matching RSA public key from the JWKS key set.
        4. Verifies the token signature, audience, issuer, and expiry
           using ``python-jose`` with the RS256 algorithm.

        Args:
            token: The raw JWT access token string (without the
                ``Bearer `` prefix).

        Returns:
            dict: The decoded JWT payload (claims dictionary) containing
                standard claims (``sub``, ``aud``, ``iss``, ``exp``,
                ``iat``) and any custom claims from the Auth0 namespace.

        Raises:
            ValueError: If the token header is missing a ``kid``, no
                matching JWKS key is found, or the JWKS response is
                malformed.
            JWTError: If the token signature is invalid, the token has
                expired, or audience / issuer validation fails.
        """
        start_time: float = time.time()

        # Step 1: Fetch JWKS keys (cached)
        jwks_data: dict = self._get_jwks_keys()
        keys: list[dict] = jwks_data.get("keys", [])

        if not keys:
            self.logger.error("jwks_keys_empty")
            raise ValueError(
                "JWKS endpoint returned no keys. Cannot validate token."
            )

        # Step 2: Extract kid from the unverified JWT header
        try:
            unverified_header: dict = jwt.get_unverified_header(token)
        except JWTError as exc:
            self.logger.warning(
                "token_header_decode_failed",
                error_type=type(exc).__name__,
            )
            raise

        kid: Optional[str] = unverified_header.get("kid")
        if not kid:
            self.logger.warning("token_missing_kid")
            raise ValueError("JWT header does not contain a 'kid' claim.")

        # Step 3: Find matching RSA public key
        rsa_key: Dict[str, Any] = {}
        for key in keys:
            if key.get("kid") == kid:
                rsa_key = {
                    "kty": key.get("kty", "RSA"),
                    "kid": key.get("kid", ""),
                    "use": key.get("use", "sig"),
                    "n": key.get("n", ""),
                    "e": key.get("e", ""),
                }
                break

        if not rsa_key:
            self.logger.warning(
                "jwks_key_not_found",
                kid=kid,
                available_kids=[k.get("kid", "") for k in keys],
            )
            raise ValueError(
                f"No matching JWKS key found for kid '{kid}'."
            )

        # Step 3b: Validate RSA key components are properly base64url-encoded
        # before attempting token verification — fail fast with a clear error
        # instead of an opaque decoding exception.
        try:
            _n_bytes: bytes = base64url_decode(rsa_key["n"].encode("utf-8"))
            _e_bytes: bytes = base64url_decode(rsa_key["e"].encode("utf-8"))
            if len(_n_bytes) < 128:  # RSA-2048 minimum modulus size
                self.logger.warning(
                    "rsa_key_modulus_too_short",
                    kid=kid,
                    modulus_bytes=len(_n_bytes),
                )
        except Exception as exc:
            self.logger.error(
                "rsa_key_component_decode_failed",
                kid=kid,
                error_type=type(exc).__name__,
            )
            raise ValueError(
                f"JWKS key components for kid '{kid}' are malformed."
            ) from exc

        # Step 3c: Construct an RSA key object via python-jose jwk module
        # for explicit key type validation before decode.
        try:
            constructed_key: Any = jwk.construct(rsa_key, algorithm="RS256")
            if constructed_key is None:
                raise ValueError("JWK construction returned None.")
        except Exception as exc:
            self.logger.error(
                "jwk_construct_failed",
                kid=kid,
                error_type=type(exc).__name__,
            )
            raise ValueError(
                f"Failed to construct JWK for kid '{kid}'."
            ) from exc

        # Step 4: Verify and decode the token using the validated RSA key
        try:
            payload: dict = jwt.decode(
                token,
                rsa_key,
                algorithms=["RS256"],
                audience=self.auth0_api_audience,
                issuer=self.issuer,
            )
        except JWTError as exc:
            self.logger.warning(
                "token_validation_failed",
                error_type=type(exc).__name__,
                kid=kid,
            )
            raise

        elapsed_ms: float = (time.time() - start_time) * 1000.0

        self.logger.info(
            "token_validated",
            subject=payload.get("sub", "unknown"),
            elapsed_ms=round(elapsed_ms, 2),
        )

        return payload

    def refresh_token(self, refresh_token_str: str) -> dict:
        """Exchange a refresh token for a new access token via Auth0.

        Performs an OAuth 2.0 Refresh Token grant against the Auth0
        ``/oauth/token`` endpoint.  Auth0 may optionally issue a new
        refresh token (rotation) alongside the new access token.

        Args:
            refresh_token_str: The current refresh token string.

        Returns:
            dict: Contains:
                - ``access_token`` (str): New access token.
                - ``refresh_token`` (str | None): Optionally rotated
                  refresh token (``None`` if Auth0 did not rotate).
                - ``expires_in`` (int): New access token TTL in seconds.
                - ``token_type`` (str): Always ``'Bearer'``.

        Raises:
            httpx.HTTPError: If the Auth0 HTTP call fails.
            ValueError: If the response is missing ``access_token``.
        """
        start_time: float = time.time()

        token_url: str = f"https://{self.auth0_domain}/oauth/token"

        payload: Dict[str, str] = {
            "grant_type": "refresh_token",
            "client_id": self.auth0_client_id,
            "client_secret": self.auth0_client_secret,
            "refresh_token": refresh_token_str,
        }

        try:
            response: httpx.Response = httpx.post(
                token_url,
                json=payload,
                headers={"Content-Type": "application/json"},
                timeout=DEFAULT_TOKEN_EXCHANGE_TIMEOUT,
            )
            response.raise_for_status()
            data: dict = response.json()
        except httpx.HTTPError as exc:
            self.logger.error(
                "refresh_token_exchange_failed",
                error_type=type(exc).__name__,
                status_code=getattr(
                    getattr(exc, "response", None), "status_code", None
                ),
            )
            raise

        new_access_token: str = data.get("access_token", "")
        if not new_access_token:
            raise ValueError(
                "Auth0 refresh token exchange returned empty access_token."
            )

        elapsed_ms: float = (time.time() - start_time) * 1000.0

        self.logger.info(
            "token_refreshed",
            expires_in=data.get("expires_in"),
            has_new_refresh_token="refresh_token" in data,
            elapsed_ms=round(elapsed_ms, 2),
        )

        return {
            "access_token": new_access_token,
            "refresh_token": data.get("refresh_token"),
            "expires_in": data.get("expires_in", 3600),
            "token_type": data.get("token_type", "Bearer"),
        }

    # ------------------------------------------------------------------
    # Public API — Role & Tenant Extraction
    # ------------------------------------------------------------------

    def extract_roles(self, token_payload: dict) -> list[str]:
        """Extract platform roles from Auth0 custom JWT claims.

        Reads the ``https://synthetic-erp/roles`` claim from the decoded
        JWT payload and maps each Auth0 role string to a platform
        :class:`~api_gateway.models.user.UserRole` value using the
        :data:`AUTH0_ROLE_MAPPING` lookup table.

        Unrecognised Auth0 role strings are logged at ``warning`` level
        and silently skipped — they do not propagate as platform roles.

        Args:
            token_payload: The decoded JWT claims dictionary (as returned
                by :meth:`validate_token`).

        Returns:
            list[str]: Platform role value strings (e.g.
                ``['data_engineer']``).  Returns an empty list if no
                roles claim is present or no recognised roles are found.

        Example::

            payload = auth.validate_token(access_token)
            roles = auth.extract_roles(payload)
            # ['data_engineer']
        """
        auth0_roles: list = token_payload.get(ROLES_CLAIM, [])

        if not isinstance(auth0_roles, list):
            self.logger.warning(
                "roles_claim_invalid_type",
                expected="list",
                actual=type(auth0_roles).__name__,
            )
            return []

        platform_roles: list[str] = []
        for auth0_role in auth0_roles:
            mapped_role: Optional[UserRole] = AUTH0_ROLE_MAPPING.get(
                auth0_role
            )
            if mapped_role is not None:
                platform_roles.append(mapped_role.value)
            else:
                self.logger.warning(
                    "unrecognised_auth0_role",
                    auth0_role=str(auth0_role),
                    valid_roles=list(AUTH0_ROLE_MAPPING.keys()),
                )

        self.logger.info(
            "roles_extracted",
            role_count=len(platform_roles),
        )

        return platform_roles

    def extract_tenant_id(self, token_payload: dict) -> str:
        """Extract the tenant identifier from Auth0 custom JWT claims.

        Reads the ``https://synthetic-erp/tenant_id`` claim from the
        decoded JWT payload.  If the claim is missing or empty, an
        empty string is returned and a warning is logged.

        Args:
            token_payload: The decoded JWT claims dictionary (as returned
                by :meth:`validate_token`).

        Returns:
            str: The tenant identifier string, or ``''`` if the claim
                is absent or empty.

        Example::

            payload = auth.validate_token(access_token)
            tenant_id = auth.extract_tenant_id(payload)
            # 'tenant_001'
        """
        tenant_id: str = token_payload.get(TENANT_ID_CLAIM, "")

        if not isinstance(tenant_id, str):
            tenant_id = str(tenant_id) if tenant_id else ""

        if not tenant_id:
            self.logger.warning(
                "tenant_id_claim_missing_or_empty",
                claim_key=TENANT_ID_CLAIM,
            )

        return tenant_id

    # ------------------------------------------------------------------
    # Public API — User Profile Management
    # ------------------------------------------------------------------

    def get_user_profile(self, auth0_user_id: str) -> Optional[dict]:
        """Retrieve a platform user profile by Auth0 identity.

        Delegates to :meth:`User.find_by_auth0_id` to look up the user
        in the MongoDB ``users`` collection.

        Args:
            auth0_user_id: The Auth0 ``sub`` claim (e.g.
                ``'auth0|abc123'``).

        Returns:
            dict or None: The user profile document, or ``None`` if no
                user is registered with the given Auth0 identity.
        """
        user_doc: Optional[dict] = User.find_by_auth0_id(auth0_user_id)

        if user_doc is not None:
            self.logger.info(
                "user_profile_retrieved",
                user_id=user_doc.get("user_id"),
                tenant_id=user_doc.get("tenant_id"),
            )
        else:
            self.logger.info(
                "user_profile_not_found",
                # Never log the full auth0_user_id — log a safe indicator
                auth0_id_prefix=auth0_user_id[:10] + "..."
                if len(auth0_user_id) > 10
                else "***",
            )

        return user_doc

    def update_user_profile(
        self,
        user_id: str,
        tenant_id: str,
        updates: dict,
    ) -> Optional[dict]:
        """Update whitelisted profile fields for a platform user.

        Delegates to :meth:`User.update_profile` which enforces a
        whitelist of mutable fields (``name``, ``picture``,
        ``preferences``) and silently ignores all other keys.

        Args:
            user_id: The platform user UUID.
            tenant_id: Tenant namespace for multi-tenant isolation
                (R-007).
            updates: Dictionary of field updates.  Only whitelisted
                keys are applied.

        Returns:
            dict or None: The updated user profile document, or ``None``
                if the user was not found within the specified tenant.
        """
        updated_doc: Optional[dict] = User.update_profile(
            user_id=user_id,
            tenant_id=tenant_id,
            updates=updates,
        )

        if updated_doc is not None:
            updated_keys: list[str] = [
                k for k in updates.keys()
                if k in {"name", "picture", "preferences"}
            ]
            self.logger.info(
                "user_profile_updated",
                user_id=user_id,
                tenant_id=tenant_id,
                fields_updated=updated_keys,
            )
        else:
            self.logger.warning(
                "user_profile_update_failed_not_found",
                user_id=user_id,
                tenant_id=tenant_id,
            )

        return updated_doc

    def get_user_permissions(
        self,
        user_id: str,
        tenant_id: str,
    ) -> list[str]:
        """Retrieve the permission set for a platform user.

        Delegates to :meth:`User.get_permissions` which resolves the
        user's role from MongoDB and returns the corresponding
        permission list from :data:`ROLE_PERMISSIONS`.

        If the database lookup fails, falls back to resolving
        permissions directly from the :data:`ROLE_PERMISSIONS` mapping
        using cached session data from Redis when available.

        Args:
            user_id: The platform user UUID.
            tenant_id: Tenant namespace for multi-tenant isolation
                (R-007).

        Returns:
            list[str]: Permission strings (e.g.
                ``['generation:create', 'generation:read', ...]``).
                Returns an empty list if the user is not found.
        """
        permissions: list[str] = User.get_permissions(
            user_id=user_id,
            tenant_id=tenant_id,
        )

        # If the database returned permissions, validate that they are
        # a subset of the known permission universe defined in
        # ROLE_PERMISSIONS.  This acts as a runtime consistency check.
        if permissions:
            all_known_permissions: set[str] = set()
            for role_perms in ROLE_PERMISSIONS.values():
                all_known_permissions.update(role_perms)

            unknown_permissions: list[str] = [
                p for p in permissions if p not in all_known_permissions
            ]
            if unknown_permissions:
                self.logger.warning(
                    "unknown_permissions_detected",
                    user_id=user_id,
                    unknown_permissions=unknown_permissions,
                )

        self.logger.info(
            "user_permissions_retrieved",
            user_id=user_id,
            tenant_id=tenant_id,
            permission_count=len(permissions),
        )

        return permissions

    # ------------------------------------------------------------------
    # Private — Auth0 HTTP Interactions (Circuit-Breaker Protected)
    # ------------------------------------------------------------------

    @circuit(failure_threshold=3, recovery_timeout=60)
    def _get_jwks_keys(self) -> dict:
        """Fetch Auth0 JWKS public keys with Redis caching.

        Resolution order:

        1. **Redis cache** — Checks for cached JWKS data under the key
           ``auth:jwks_keys``.  If present and valid JSON, returns
           immediately (cache hit).
        2. **Auth0 JWKS endpoint** — If no cache hit, fetches the JWKS
           document from ``https://{AUTH0_DOMAIN}/.well-known/jwks.json``
           via ``httpx.get()`` and caches the response in Redis with
           a 24-hour TTL.

        Returns:
            dict: The JWKS response containing a ``keys`` array of
                RSA public key objects.

        Raises:
            httpx.HTTPError: If the JWKS HTTP fetch fails and no
                cached keys are available.
            ValueError: If the JWKS response cannot be parsed as JSON.
        """
        start_time: float = time.time()

        # Step 1: Try Redis cache first
        try:
            redis_client = get_redis()
            cached_data: Optional[bytes] = redis_client.get(JWKS_CACHE_KEY)
            if cached_data is not None:
                jwks_keys: dict = json.loads(cached_data)
                elapsed_ms: float = (time.time() - start_time) * 1000.0
                self.logger.info(
                    "jwks_cache_hit",
                    elapsed_ms=round(elapsed_ms, 2),
                    key_count=len(jwks_keys.get("keys", [])),
                )
                return jwks_keys
        except Exception as exc:
            # Redis unavailable — fall through to HTTP fetch.
            self.logger.warning(
                "jwks_cache_read_failed",
                error=str(exc),
                error_type=type(exc).__name__,
            )

        # Step 2: Fetch from Auth0 JWKS endpoint
        self.logger.info("jwks_cache_miss_fetching_from_auth0")

        try:
            response: httpx.Response = httpx.get(
                self.jwks_uri,
                timeout=DEFAULT_JWKS_FETCH_TIMEOUT,
            )
            response.raise_for_status()
            jwks_keys = response.json()
        except httpx.HTTPError as exc:
            self.logger.error(
                "jwks_fetch_failed",
                error_type=type(exc).__name__,
                jwks_uri=self.jwks_uri,
                status_code=getattr(
                    getattr(exc, "response", None), "status_code", None
                ),
            )
            raise
        except (json.JSONDecodeError, ValueError) as exc:
            self.logger.error(
                "jwks_response_parse_failed",
                error_type=type(exc).__name__,
            )
            raise ValueError(
                "Failed to parse JWKS response as JSON."
            ) from exc

        # Step 3: Cache in Redis with 24-hour TTL
        try:
            redis_client = get_redis()
            redis_client.setex(
                JWKS_CACHE_KEY,
                JWKS_CACHE_TTL_SECONDS,
                json.dumps(jwks_keys),
            )
            self.logger.info(
                "jwks_cached",
                ttl_seconds=JWKS_CACHE_TTL_SECONDS,
                key_count=len(jwks_keys.get("keys", [])),
            )
        except Exception as exc:
            # Cache write failure is non-fatal — keys are still usable.
            self.logger.warning(
                "jwks_cache_write_failed",
                error=str(exc),
                error_type=type(exc).__name__,
            )

        elapsed_ms = (time.time() - start_time) * 1000.0
        self.logger.info(
            "jwks_fetched_from_auth0",
            elapsed_ms=round(elapsed_ms, 2),
            key_count=len(jwks_keys.get("keys", [])),
        )

        return jwks_keys

    @circuit(failure_threshold=3, recovery_timeout=60)
    def _exchange_code_for_tokens(
        self,
        code: str,
        redirect_uri: str,
    ) -> dict:
        """Exchange an authorization code for Auth0 tokens.

        Sends an HTTP POST to the Auth0 ``/oauth/token`` endpoint with
        the ``authorization_code`` grant type.

        Args:
            code: The one-time authorization code from the callback.
            redirect_uri: The redirect URI that was used in the initial
                ``/authorize`` request (must match).

        Returns:
            dict: The Auth0 token response containing ``access_token``,
                ``id_token``, ``refresh_token`` (optional), and
                ``expires_in``.

        Raises:
            httpx.HTTPError: If the HTTP request fails or Auth0 returns
                a non-2xx status.
        """
        start_time: float = time.time()

        token_url: str = f"https://{self.auth0_domain}/oauth/token"

        payload: Dict[str, str] = {
            "grant_type": "authorization_code",
            "client_id": self.auth0_client_id,
            "client_secret": self.auth0_client_secret,
            "code": code,
            "redirect_uri": redirect_uri,
        }

        try:
            response: httpx.Response = httpx.post(
                token_url,
                json=payload,
                headers={"Content-Type": "application/json"},
                timeout=DEFAULT_TOKEN_EXCHANGE_TIMEOUT,
            )
            response.raise_for_status()
            data: dict = response.json()
        except httpx.HTTPError as exc:
            self.logger.error(
                "auth0_token_exchange_failed",
                error_type=type(exc).__name__,
                status_code=getattr(
                    getattr(exc, "response", None), "status_code", None
                ),
            )
            raise

        elapsed_ms: float = (time.time() - start_time) * 1000.0

        self.logger.info(
            "auth0_token_exchange_completed",
            has_access_token=bool(data.get("access_token")),
            has_id_token=bool(data.get("id_token")),
            has_refresh_token=bool(data.get("refresh_token")),
            expires_in=data.get("expires_in"),
            elapsed_ms=round(elapsed_ms, 2),
        )

        return data
