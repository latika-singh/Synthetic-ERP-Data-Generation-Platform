"""Multi-tenant context extraction and namespace isolation middleware.

This module enforces multi-tenant isolation (R-007) across the API Gateway by
extracting tenant identity from JWT claims or the ``X-Tenant-ID`` request
header, validating tenant existence against the ``tenant_configurations``
MongoDB collection, and storing the resolved :class:`TenantContext` on Flask's
``g`` object so that **all** downstream database queries and inter-service
REST calls are automatically scoped to the resolved tenant.

Security Guarantees:
    - Cross-tenant data access is impossible by design (R-007).
    - Only **Platform Admin** users may issue cross-tenant requests via the
      ``X-Tenant-ID`` header override.
    - Normal (non-admin) users are restricted to their JWT-embedded tenant.
    - Tenant existence and activation status are validated against MongoDB on
      every request, with a Redis / LRU cache to avoid redundant lookups.
    - All tenant context operations are audit-logged with structlog.

Integration Points:
    - **Auth middleware** (runs *before* this middleware) stores JWT claims on
      ``g.jwt_payload``, ``g.user_roles``, and ``g.tenant_id``.
    - **Rate limiter** (runs *after* this middleware) can use
      ``g.resource_quotas`` for tenant-aware throttling.
    - **Service layer / model classes** call :func:`get_tenant_filter` to
      obtain a MongoDB query filter that scopes every query to the current
      tenant.
    - The ``after_request`` hook :func:`_propagate_tenant_header` adds the
      ``X-Tenant-ID`` header to outgoing responses so that downstream
      inter-service calls can propagate tenant context.

Usage::

    # Registration in the application factory (app.py):
    from api_gateway.middleware.tenant import register_tenant_middleware

    def create_app() -> Flask:
        app = Flask(__name__)
        register_tenant_middleware(app)
        return app

    # Explicit route-level enforcement:
    from api_gateway.middleware.tenant import require_tenant

    @app.route('/api/v1/generation/jobs', methods=['POST'])
    @require_tenant
    def create_job():
        ...

    # MongoDB query scoping:
    from api_gateway.middleware.tenant import get_tenant_filter

    def list_jobs():
        return list(db['generation_profiles'].find(get_tenant_filter()))
"""

from __future__ import annotations

import json
import time
from dataclasses import dataclass, field
from functools import wraps
from typing import TYPE_CHECKING, Any

from flask import Flask, Response, g, jsonify, request


if TYPE_CHECKING:
    from collections.abc import Callable

    import structlog

from api_gateway.extensions import get_db, get_redis
from shared.database.mongodb import COLLECTION_TENANT_CONFIGURATIONS
from shared.logging.structured_logger import get_logger


# ---------------------------------------------------------------------------
# Module-level logger — pre-configured structlog BoundLogger for structured
# JSON output with correlation ID propagation and service context injection.
# ---------------------------------------------------------------------------
logger: structlog.stdlib.BoundLogger = get_logger(__name__)

# ---------------------------------------------------------------------------
# Module-level constants (exported)
# ---------------------------------------------------------------------------

TENANT_HEADER: str = "X-Tenant-ID"
"""HTTP header name used for explicit tenant identification in requests
and for propagating tenant context to downstream services."""

TENANT_JWT_CLAIM: str = "https://synthetic-erp/tenant_id"
"""Auth0 custom claim namespace key containing the tenant identifier.
Auth0 requires custom claims to use a namespaced URI to avoid collisions
with registered JWT claims (RFC 7519)."""

TENANT_EXEMPT_PATHS: set[str] = {
    "/health",
    "/ready",
    "/api/v1/auth/login",
    "/api/v1/auth/callback",
}
"""Request paths that bypass tenant context extraction entirely.

Health and readiness probes must respond without tenant context for
Kubernetes liveness/readiness checks.  Auth endpoints handle their own
lifecycle and do not operate within a tenant scope.
"""

ADMIN_ROLE: str = "Platform Admin"
"""Role name that grants cross-tenant access privileges.

Only users with this role in ``g.user_roles`` may issue requests with an
``X-Tenant-ID`` header that differs from the tenant embedded in their JWT.
"""

TENANT_CACHE_TTL: int = 300
"""Time-to-live in seconds for cached tenant configuration entries.

Validated tenant configs are stored in Redis (primary) or an in-process
LRU cache (fallback) for this duration to avoid hitting MongoDB on every
single request.  Default is 5 minutes.
"""

# ---------------------------------------------------------------------------
# Redis cache key prefix for tenant configuration caching.
# ---------------------------------------------------------------------------
_REDIS_TENANT_PREFIX: str = "tenant_config:"

# ---------------------------------------------------------------------------
# Maximum entries in the fallback in-process LRU cache.
# ---------------------------------------------------------------------------
_LRU_CACHE_MAXSIZE: int = 256


# ===================================================================
# Data Classes
# ===================================================================


@dataclass
class TenantContext:
    """Encapsulates the resolved tenant context for the current request.

    An instance of this class is built by :func:`_validate_tenant` after
    querying the ``tenant_configurations`` MongoDB collection and is stored
    on ``flask.g.tenant_context`` for access by all downstream handlers.

    Attributes:
        tenant_id: Unique identifier for the tenant (UUID or slug string).
        tenant_name: Human-readable display name of the tenant.
        namespace: Kubernetes / logical namespace for resource isolation.
        is_active: Whether the tenant account is currently active.  Inactive
            tenants are rejected at the middleware layer.
        resource_quotas: Tenant-specific resource limits (e.g. max records
            per job, max concurrent jobs, storage quota in bytes).
        settings: Arbitrary tenant-specific configuration overrides that
            downstream services may consult.
    """

    tenant_id: str
    tenant_name: str
    namespace: str
    is_active: bool
    resource_quotas: dict[str, Any] = field(default_factory=dict)
    settings: dict[str, Any] = field(default_factory=dict)


# ===================================================================
# Private Helpers
# ===================================================================


def _extract_tenant_id() -> str | None:
    """Extract the tenant identifier from the current request context.

    Resolution priority:

    1. **JWT claim** — ``g.jwt_payload['https://synthetic-erp/tenant_id']``
       set by the auth middleware which runs before this middleware.
    2. **Request header** — ``X-Tenant-ID`` HTTP header.  For non-admin
       users the header value must match the JWT claim (enforced later in
       :func:`tenant_context_middleware`).
    3. **Platform Admin override** — When the authenticated user holds the
       ``Platform Admin`` role *and* an ``X-Tenant-ID`` header is present,
       the header value takes precedence, enabling cross-tenant access for
       administrative operations.

    Returns:
        The resolved tenant identifier string, or ``None`` if no tenant
        context can be determined from the request.
    """
    tenant_id: str | None = None
    source: str = "none"

    # --- Priority 1: JWT claim from the auth middleware ---
    jwt_payload: dict[str, Any] | None = getattr(g, "jwt_payload", None)
    if jwt_payload is not None:
        jwt_tenant: str | None = jwt_payload.get(TENANT_JWT_CLAIM)
        if jwt_tenant:
            tenant_id = str(jwt_tenant).strip()
            source = "jwt_claim"

    # --- Priority 2 / 3: X-Tenant-ID header ---
    header_tenant: str | None = request.headers.get(TENANT_HEADER)
    if header_tenant:
        header_tenant = header_tenant.strip()

        if _is_admin_cross_tenant_request():
            # Platform Admin may override the JWT tenant with the header.
            tenant_id = header_tenant
            source = "admin_header_override"
        elif tenant_id is None:
            # No JWT tenant available — use header as fallback.
            tenant_id = header_tenant
            source = "header_fallback"
        # else: JWT tenant takes priority for normal users; header is
        # validated for equality in tenant_context_middleware().

    logger.debug(
        "tenant_id_extracted",
        tenant_id=tenant_id,
        source=source,
        has_jwt_payload=jwt_payload is not None,
        has_header=header_tenant is not None,
    )

    return tenant_id if tenant_id else None


def _validate_tenant(tenant_id: str) -> TenantContext | None:
    """Validate a tenant identifier against MongoDB and return its context.

    The function first checks the Redis cache (with key
    ``tenant_config:<tenant_id>``) for a previously serialised
    :class:`TenantContext`.  On a cache miss it queries the
    ``tenant_configurations`` MongoDB collection for a document whose
    ``tenant_id`` field matches.

    Inactive tenants (``is_active == False``) are treated as invalid and
    ``None`` is returned.

    Args:
        tenant_id: The tenant identifier to validate.

    Returns:
        A fully populated :class:`TenantContext` instance if the tenant
        exists and is active, or ``None`` otherwise.
    """
    # ------------------------------------------------------------------
    # 1. Try Redis cache first
    # ------------------------------------------------------------------
    cached_context: TenantContext | None = _get_cached_tenant(tenant_id)
    if cached_context is not None:
        logger.debug("tenant_cache_hit", tenant_id=tenant_id)
        if not cached_context.is_active:
            logger.warning(
                "tenant_inactive_cached",
                tenant_id=tenant_id,
            )
            return None
        return cached_context

    # ------------------------------------------------------------------
    # 2. Query MongoDB for the tenant configuration
    # ------------------------------------------------------------------
    try:
        db = get_db()
        collection = db[COLLECTION_TENANT_CONFIGURATIONS]

        doc: dict[str, Any] | None = collection.find_one(
            {"tenant_id": tenant_id}
        )

        if doc is None:
            logger.warning(
                "tenant_not_found",
                tenant_id=tenant_id,
            )
            return None

        # Build the TenantContext from the MongoDB document.
        context = TenantContext(
            tenant_id=str(doc.get("tenant_id", tenant_id)),
            tenant_name=str(doc.get("tenant_name", "")),
            namespace=str(doc.get("namespace", f"ns-{tenant_id}")),
            is_active=bool(doc.get("is_active", False)),
            resource_quotas=dict(doc.get("resource_quotas", {})),
            settings=dict(doc.get("settings", {})),
        )

        # ------------------------------------------------------------------
        # 3. Cache the validated context
        # ------------------------------------------------------------------
        _set_cached_tenant(tenant_id, context)

        if not context.is_active:
            logger.warning(
                "tenant_inactive",
                tenant_id=tenant_id,
                tenant_name=context.tenant_name,
            )
            return None

        logger.info(
            "tenant_validated",
            tenant_id=context.tenant_id,
            tenant_name=context.tenant_name,
            namespace=context.namespace,
        )
        return context

    except Exception as exc:
        logger.error(
            "tenant_validation_error",
            tenant_id=tenant_id,
            error=str(exc),
            error_type=type(exc).__name__,
        )
        # On database errors, try the in-process LRU cache as a last resort.
        fallback: TenantContext | None = _get_lru_cached_tenant(tenant_id)
        if fallback is not None and fallback.is_active:
            logger.warning(
                "tenant_validation_fallback_lru",
                tenant_id=tenant_id,
            )
            return fallback
        return None


def _is_admin_cross_tenant_request() -> bool:
    """Determine whether the current request is an admin cross-tenant override.

    A cross-tenant request is defined as one where:

    1. The authenticated user possesses the **Platform Admin** role
       (checked via ``g.user_roles``), AND
    2. The ``X-Tenant-ID`` header is present and non-empty (the admin
       explicitly specified a target tenant).

    Normal users **cannot** use the ``X-Tenant-ID`` header to access
    another tenant's data — the middleware will reject such attempts with
    a 403 response.

    Returns:
        ``True`` if the current user is a Platform Admin performing an
        explicit cross-tenant operation; ``False`` otherwise.
    """
    user_roles: list[str] = getattr(g, "user_roles", [])
    if not user_roles:
        return False

    is_admin: bool = ADMIN_ROLE in user_roles
    has_header: bool = bool(request.headers.get(TENANT_HEADER, "").strip())

    if is_admin and has_header:
        logger.info(
            "admin_cross_tenant_request_detected",
            user_id=getattr(g, "user_id", "unknown"),
            target_tenant=request.headers.get(TENANT_HEADER),
        )

    return is_admin and has_header


# ===================================================================
# Caching Layer
# ===================================================================


def _tenant_context_to_dict(ctx: TenantContext) -> dict[str, Any]:
    """Serialise a :class:`TenantContext` instance to a JSON-safe dict.

    Args:
        ctx: The tenant context to serialise.

    Returns:
        A dictionary suitable for :func:`json.dumps`.
    """
    return {
        "tenant_id": ctx.tenant_id,
        "tenant_name": ctx.tenant_name,
        "namespace": ctx.namespace,
        "is_active": ctx.is_active,
        "resource_quotas": ctx.resource_quotas,
        "settings": ctx.settings,
    }


def _dict_to_tenant_context(data: dict[str, Any]) -> TenantContext:
    """Deserialise a dictionary back into a :class:`TenantContext`.

    Args:
        data: A dictionary produced by :func:`_tenant_context_to_dict`.

    Returns:
        A :class:`TenantContext` instance populated from the dictionary.
    """
    return TenantContext(
        tenant_id=str(data.get("tenant_id", "")),
        tenant_name=str(data.get("tenant_name", "")),
        namespace=str(data.get("namespace", "")),
        is_active=bool(data.get("is_active", False)),
        resource_quotas=dict(data.get("resource_quotas", {})),
        settings=dict(data.get("settings", {})),
    )


def _get_cached_tenant(tenant_id: str) -> TenantContext | None:
    """Attempt to retrieve a cached tenant config from Redis.

    Falls back to the in-process LRU cache if Redis is unavailable.

    Args:
        tenant_id: The tenant identifier to look up.

    Returns:
        A :class:`TenantContext` if found in cache, else ``None``.
    """
    # --- Try Redis first ---
    try:
        redis_client = get_redis()
        cache_key: str = f"{_REDIS_TENANT_PREFIX}{tenant_id}"
        raw: bytes | None = redis_client.get(cache_key)  # type: ignore[assignment]
        if raw is not None:
            data: dict[str, Any] = json.loads(raw.decode("utf-8"))
            return _dict_to_tenant_context(data)
    except Exception:
        # Redis unavailable — fall through to LRU cache.
        logger.debug("redis_tenant_cache_miss", tenant_id=tenant_id)

    # --- Fallback: in-process LRU cache ---
    return _get_lru_cached_tenant(tenant_id)


def _set_cached_tenant(tenant_id: str, ctx: TenantContext) -> None:
    """Store a validated tenant config in both Redis and the LRU cache.

    The Redis entry is stored with a TTL of :data:`TENANT_CACHE_TTL`
    seconds.  The LRU cache is updated unconditionally as a local
    hot-path optimization.

    Args:
        tenant_id: The tenant identifier (used as cache key).
        ctx: The :class:`TenantContext` to cache.
    """
    serialised: str = json.dumps(_tenant_context_to_dict(ctx))

    # --- Redis cache ---
    try:
        redis_client = get_redis()
        cache_key: str = f"{_REDIS_TENANT_PREFIX}{tenant_id}"
        redis_client.setex(cache_key, TENANT_CACHE_TTL, serialised)
    except Exception:
        # Redis unavailable — LRU cache still works as fallback.
        logger.debug(
            "tenant_redis_cache_write_failed",
            tenant_id=tenant_id,
        )

    # --- In-process LRU cache ---
    _set_lru_cached_tenant(tenant_id, serialised)


# ---------------------------------------------------------------------------
# In-process LRU cache — used when Redis is unavailable.  The cache is keyed
# by (tenant_id, timestamp_bucket) to approximate TTL behaviour.
# ---------------------------------------------------------------------------

# We store serialised JSON strings in a module-level dict with timestamps so
# we can honour the TTL without relying on functools.lru_cache expiry (which
# does not support TTL).  The dict is bounded to _LRU_CACHE_MAXSIZE entries.

_lru_store: dict[str, tuple[str, float]] = {}
"""In-process LRU-style cache: ``{tenant_id: (serialised_json, timestamp)}``."""


def _get_lru_cached_tenant(tenant_id: str) -> TenantContext | None:
    """Retrieve a tenant config from the in-process LRU cache.

    Entries older than :data:`TENANT_CACHE_TTL` seconds are considered stale
    and are evicted on access.

    Args:
        tenant_id: The tenant identifier to look up.

    Returns:
        A :class:`TenantContext` if found and not expired, else ``None``.
    """
    entry: tuple[str, float] | None = _lru_store.get(tenant_id)
    if entry is None:
        return None

    serialised, stored_at = entry
    if (time.time() - stored_at) > TENANT_CACHE_TTL:
        # Expired — evict and return cache miss.
        _lru_store.pop(tenant_id, None)
        return None

    try:
        data: dict[str, Any] = json.loads(serialised)
        return _dict_to_tenant_context(data)
    except (json.JSONDecodeError, KeyError):
        _lru_store.pop(tenant_id, None)
        return None


def _set_lru_cached_tenant(tenant_id: str, serialised: str) -> None:
    """Store a serialised tenant config in the in-process LRU cache.

    If the cache has reached :data:`_LRU_CACHE_MAXSIZE`, the oldest entry
    (by insertion time) is evicted before the new one is added.

    Args:
        tenant_id: The tenant identifier (cache key).
        serialised: JSON-serialised :class:`TenantContext` string.
    """
    # Simple size-bounded eviction — remove the oldest entry.
    if len(_lru_store) >= _LRU_CACHE_MAXSIZE and tenant_id not in _lru_store:
        oldest_key: str | None = None
        oldest_ts: float = float("inf")
        for key, (_, ts) in _lru_store.items():
            if ts < oldest_ts:
                oldest_ts = ts
                oldest_key = key
        if oldest_key is not None:
            _lru_store.pop(oldest_key, None)

    _lru_store[tenant_id] = (serialised, time.time())


# ===================================================================
# Public API — Middleware Hooks
# ===================================================================


def tenant_context_middleware() -> tuple[Response, int] | None:
    """Flask ``before_request`` hook enforcing multi-tenant context resolution.

    Execution flow:

    1. Skip for exempt paths (:data:`TENANT_EXEMPT_PATHS`) and CORS
       preflight ``OPTIONS`` requests.
    2. Extract the tenant identifier via :func:`_extract_tenant_id`.
    3. Validate the tenant against MongoDB via :func:`_validate_tenant`.
    4. Enforce cross-tenant authorization — non-admin users whose header
       tenant differs from their JWT tenant are rejected with 403.
    5. Store the resolved :class:`TenantContext` on ``flask.g`` for
       downstream consumption.

    Returns:
        ``None`` when tenant context is successfully resolved (request
        processing continues).  A ``(Response, status_code)`` tuple when
        tenant resolution fails, short-circuiting the request pipeline.
    """
    # ------------------------------------------------------------------
    # 1. Exempt paths — health probes, auth endpoints, CORS preflight
    # ------------------------------------------------------------------
    request_path: str = request.path.rstrip("/") or "/"

    if request_path in TENANT_EXEMPT_PATHS:
        return None

    if request.method == "OPTIONS":
        return None

    # Also handle prefix matching for exempt paths that may have trailing
    # sub-paths (e.g. /health/deep).
    for exempt in TENANT_EXEMPT_PATHS:
        if request_path.startswith(exempt + "/"):
            return None

    # ------------------------------------------------------------------
    # 2. Extract tenant identifier
    # ------------------------------------------------------------------
    tenant_id: str | None = _extract_tenant_id()

    if tenant_id is None:
        logger.warning(
            "tenant_context_missing",
            path=request.path,
            method=request.method,
            remote_addr=request.remote_addr,
            user_id=getattr(g, "user_id", "unknown"),
        )
        return (
            jsonify({
                "error": "Tenant context required",
                "code": "TENANT_REQUIRED",
            }),
            400,
        )

    # ------------------------------------------------------------------
    # 3. Validate tenant existence and activation status
    # ------------------------------------------------------------------
    tenant_context: TenantContext | None = _validate_tenant(tenant_id)

    if tenant_context is None:
        logger.warning(
            "tenant_invalid_or_inactive",
            tenant_id=tenant_id,
            path=request.path,
            method=request.method,
            user_id=getattr(g, "user_id", "unknown"),
        )
        return (
            jsonify({
                "error": "Invalid or inactive tenant",
                "code": "INVALID_TENANT",
            }),
            403,
        )

    # ------------------------------------------------------------------
    # 4. Cross-tenant authorization check
    # ------------------------------------------------------------------
    jwt_tenant: str | None = None
    jwt_payload: dict[str, Any] | None = getattr(g, "jwt_payload", None)
    if jwt_payload is not None:
        jwt_tenant = jwt_payload.get(TENANT_JWT_CLAIM)
        if jwt_tenant:
            jwt_tenant = str(jwt_tenant).strip()

    header_tenant: str | None = request.headers.get(TENANT_HEADER)
    if header_tenant:
        header_tenant = header_tenant.strip()

    # If the resolved tenant differs from the JWT tenant, only Platform
    # Admin is allowed to proceed.
    if (
        jwt_tenant
        and header_tenant
        and header_tenant != jwt_tenant
        and not _is_admin_cross_tenant_request()
    ):
        logger.warning(
            "cross_tenant_access_denied",
            user_id=getattr(g, "user_id", "unknown"),
            jwt_tenant=jwt_tenant,
            header_tenant=header_tenant,
            path=request.path,
        )
        return (
            jsonify({
                "error": "Cross-tenant access denied",
                "code": "CROSS_TENANT_DENIED",
            }),
            403,
        )

    # ------------------------------------------------------------------
    # 5. Store tenant context on Flask g for downstream consumption
    # ------------------------------------------------------------------
    g.tenant_id = tenant_context.tenant_id
    g.tenant_name = tenant_context.tenant_name
    g.tenant_namespace = tenant_context.namespace
    g.tenant_context = tenant_context
    g.resource_quotas = tenant_context.resource_quotas

    logger.info(
        "tenant_context_bound",
        tenant_id=tenant_context.tenant_id,
        tenant_name=tenant_context.tenant_name,
        namespace=tenant_context.namespace,
        path=request.path,
        method=request.method,
        user_id=getattr(g, "user_id", "unknown"),
    )

    return None


def _propagate_tenant_header(response: Response) -> Response:
    """Flask ``after_request`` hook adding the tenant ID to the response.

    Ensures that the ``X-Tenant-ID`` header is present on every outgoing
    response.  Downstream services and API clients can read this header
    for tenant-aware correlation and logging.

    Args:
        response: The Flask :class:`Response` object being returned to the
            client.

    Returns:
        The same :class:`Response` with the ``X-Tenant-ID`` header set
        (if a tenant context was resolved for the request).
    """
    tenant_id: str | None = getattr(g, "tenant_id", None)
    if tenant_id:
        response.headers[TENANT_HEADER] = tenant_id
    return response


# ===================================================================
# Public API — Decorator
# ===================================================================


def require_tenant(f: Callable) -> Callable:
    """Decorator that explicitly requires tenant context on a route handler.

    While the ``before_request`` middleware already enforces tenant context
    for most paths, this decorator provides an additional safety net for
    routes that might be registered under exempt path prefixes or for
    explicit documentation of the tenant requirement.

    If ``g.tenant_id`` is not set when the decorated function is called,
    a ``400 Bad Request`` response is returned immediately.

    Args:
        f: The route handler function to wrap.

    Returns:
        A decorated function that validates tenant context before
        delegating to the original handler.

    Example::

        @app.route('/api/v1/generation/jobs', methods=['POST'])
        @require_tenant
        def create_job():
            # g.tenant_id is guaranteed to be set here
            ...
    """

    @wraps(f)
    def decorated(*args: Any, **kwargs: Any) -> Any:
        """Inner wrapper validating tenant context presence.

        Args:
            *args: Positional arguments forwarded to the wrapped handler.
            **kwargs: Keyword arguments forwarded to the wrapped handler.

        Returns:
            The return value of the wrapped handler, or a 400 JSON error
            response if no tenant context is available.
        """
        tenant_id: str | None = getattr(g, "tenant_id", None)
        if not tenant_id:
            logger.warning(
                "require_tenant_failed",
                path=request.path,
                method=request.method,
                user_id=getattr(g, "user_id", "unknown"),
            )
            return (
                jsonify({
                    "error": "Tenant context required",
                    "code": "TENANT_REQUIRED",
                }),
                400,
            )
        return f(*args, **kwargs)

    return decorated


# ===================================================================
# Public API — Query Filter Utility
# ===================================================================


def get_tenant_filter() -> dict[str, str]:
    """Return a MongoDB query filter scoped to the current tenant.

    This utility is intended to be called from service-layer methods and
    model classes to ensure that **every** database query is automatically
    scoped to the authenticated tenant (R-007 namespace isolation).

    Returns:
        A dictionary ``{'tenant_id': '<current_tenant_id>'}`` suitable for
        passing as a filter (or merging into an existing filter) in PyMongo
        ``find()``, ``find_one()``, ``update_one()``, ``delete_one()``,
        etc.  Returns an empty dict if no tenant context is available
        (e.g. during health checks or background tasks).

    Example::

        from api_gateway.middleware.tenant import get_tenant_filter

        def list_jobs(db):
            base_filter = get_tenant_filter()
            base_filter['status'] = 'completed'
            return list(db['generation_profiles'].find(base_filter))
    """
    tenant_id: str | None = getattr(g, "tenant_id", None)
    if tenant_id:
        return {"tenant_id": tenant_id}
    return {}


# ===================================================================
# Public API — Middleware Registration
# ===================================================================


def register_tenant_middleware(app: Flask) -> None:
    """Register the tenant middleware hooks on the Flask application.

    This function must be called **after** the auth middleware has been
    registered (so that ``g.jwt_payload`` and ``g.user_roles`` are
    available) and **before** the rate limiter (which may consult
    ``g.resource_quotas``).

    Registers:
        - :func:`tenant_context_middleware` as a ``before_request`` hook.
        - :func:`_propagate_tenant_header` as an ``after_request`` hook.

    Args:
        app: The Flask application instance to bind tenant middleware to.

    Example::

        from api_gateway.middleware.tenant import register_tenant_middleware

        app = Flask(__name__)
        register_tenant_middleware(app)
    """
    app.before_request(tenant_context_middleware)
    app.after_request(_propagate_tenant_header)

    logger.info(
        "tenant_middleware_registered",
        exempt_paths=sorted(TENANT_EXEMPT_PATHS),
        cache_ttl_seconds=TENANT_CACHE_TTL,
        admin_role=ADMIN_ROLE,
    )
