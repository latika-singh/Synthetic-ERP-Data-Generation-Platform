"""Tiered Redis-backed rate limiting middleware for the API Gateway.

Implements a sliding window rate limiting algorithm using Redis sorted sets,
enforcing role-based request quotas per user. Rate limit tiers are assigned
based on the highest-privilege role held by the authenticated user:

- **Platform Admin**: 1000 requests per minute
- **Data Engineer**: 300 requests per minute
- **Developer / QA Engineer / Data Analyst**: 60 requests per minute

The sliding window algorithm uses Redis sorted sets (ZSET) to accurately track
request timestamps within a rolling one-minute window. Each request is recorded
as a ZSET member with its Unix timestamp as the score.  Expired entries are
pruned automatically on every request via ``ZREMRANGEBYSCORE``, and the current
window count is obtained via ``ZCARD``.

**Fail-open strategy**: If Redis is unavailable, requests are allowed through
to prevent a caching infrastructure outage from cascading into a full API
outage.  Redis failures are logged at ERROR level for operational alerting.

**12-Factor compliance**: All rate limit thresholds can be overridden via
environment-backed Flask configuration keys (``RATE_LIMIT_DEFAULT``,
``RATE_LIMIT_ELEVATED``, ``RATE_LIMIT_ADMIN``).

Health check endpoints (``/health``, ``/ready``) and CORS preflight
(``OPTIONS``) requests are exempt from rate limiting.

Usage::

    from api_gateway.middleware.rate_limiter import register_rate_limiter

    def create_app():
        app = Flask(__name__)
        register_rate_limiter(app)
        return app

Exports:
    register_rate_limiter: Registers before_request and after_request hooks.
    ROLE_RATE_LIMITS: Mapping of role names to per-minute request limits.
    RATE_LIMIT_EXEMPT_PATHS: Set of URL paths exempt from rate limiting.
    DEFAULT_WINDOW_SIZE: Sliding window duration in seconds (60).
"""

from __future__ import annotations

import time
import uuid
from typing import Dict, Optional, Set, Tuple

import structlog
from flask import Flask, Response, current_app, g, jsonify, request
from redis import Redis
from redis.exceptions import ConnectionError as RedisConnectionError
from redis.exceptions import RedisError

from shared.logging.structured_logger import get_logger

# ---------------------------------------------------------------------------
# Module-level logger — initialised once via the shared factory function.
# Uses the structured JSON logger consistent with all other API Gateway
# middleware modules for unified log aggregation.
# ---------------------------------------------------------------------------

logger: structlog.stdlib.BoundLogger = get_logger(__name__)

# ---------------------------------------------------------------------------
# Module-level constants
# ---------------------------------------------------------------------------

RATE_LIMIT_EXEMPT_PATHS: Set[str] = {"/health", "/ready"}
"""URL paths that are exempt from rate limiting.

Health check and readiness probe endpoints must always remain accessible
regardless of rate limit status to satisfy Kubernetes liveness/readiness
probes and load-balancer health checks.
"""

DEFAULT_WINDOW_SIZE: int = 60
"""Sliding window duration in seconds.

Defines the rolling time window over which request counts are tracked.
Defaults to 60 seconds (1 minute).  All ``ROLE_RATE_LIMITS`` values
represent the maximum number of requests allowed within this window.
"""

REDIS_KEY_PREFIX: str = "rate_limit:"
"""Prefix applied to all Redis keys used by the rate limiter.

Keys follow the pattern ``rate_limit:{user_id}`` where *user_id* is either
the authenticated user's ``sub`` claim from the JWT or the client IP address
for unauthenticated requests.
"""

ROLE_RATE_LIMITS: Dict[str, int] = {
    "Platform Admin": 1000,
    "Data Engineer": 300,
    "Developer": 60,
    "QA Engineer": 60,
    "Data Analyst": 60,
    "default": 60,
}
"""Mapping of user roles to maximum requests permitted per sliding window.

When a user holds multiple roles, the **most permissive** (highest) limit
is applied.  The ``"default"`` key provides a fallback for unauthenticated
requests or users whose role is not explicitly listed.

Values can be overridden at runtime via Flask app configuration keys:

- ``RATE_LIMIT_DEFAULT`` → overrides ``Developer``, ``QA Engineer``,
  ``Data Analyst``, and ``default``
- ``RATE_LIMIT_ELEVATED`` → overrides ``Data Engineer``
- ``RATE_LIMIT_ADMIN`` → overrides ``Platform Admin``
"""


# ---------------------------------------------------------------------------
# Internal helper functions
# ---------------------------------------------------------------------------


def _get_rate_limit_for_user() -> int:
    """Determine the applicable rate limit for the current request user.

    Reads user roles from ``flask.g.user_roles`` (populated by the upstream
    JWT authentication middleware) and returns the **highest** (most
    permissive) rate limit among all of the user's assigned roles.

    If no roles are found on ``g`` or the roles list is empty, the
    ``"default"`` tier is used as a fallback.

    Returns:
        The maximum number of requests the user is allowed within the
        current sliding window (``DEFAULT_WINDOW_SIZE`` seconds).
    """
    user_roles = getattr(g, "user_roles", None)

    if not user_roles:
        return ROLE_RATE_LIMITS.get("default", 60)

    # Resolve the most permissive limit across all roles held by the user.
    max_limit: int = 0
    for role in user_roles:
        limit = ROLE_RATE_LIMITS.get(role, 0)
        if limit > max_limit:
            max_limit = limit

    # If none of the user's roles matched a known tier, fall back to default.
    if max_limit == 0:
        max_limit = ROLE_RATE_LIMITS.get("default", 60)

    return max_limit


def _get_redis_client() -> Optional[Redis]:
    """Retrieve the shared Redis client from the Flask application context.

    The Redis client instance is expected to be initialised by the
    ``extensions.py`` module during application startup and stored under
    ``current_app.extensions['redis']``.

    **Fail-open strategy**: If Redis is unavailable or not configured,
    ``None`` is returned and the caller must allow the request through.
    This prevents a Redis outage from blocking all API traffic.

    Returns:
        The :class:`redis.Redis` client instance, or ``None`` if the
        client is unavailable.
    """
    try:
        redis_client: Optional[Redis] = current_app.extensions.get("redis")
        if redis_client is None:
            logger.warning(
                "redis_client_unavailable",
                reason="Redis client not found in current_app.extensions",
                action="fail_open",
            )
        return redis_client
    except (RedisConnectionError, RedisError, RuntimeError, AttributeError) as exc:
        logger.error(
            "redis_client_retrieval_failed",
            error=str(exc),
            error_type=type(exc).__name__,
            action="fail_open",
        )
        return None


def _check_rate_limit(
    user_id: str,
    rate_limit: int,
) -> Tuple[bool, int, int]:
    """Check and record a request against the sliding window rate limiter.

    Implements the **sliding window counter** algorithm using a Redis sorted
    set (ZSET).  Each request is stored as a member with its Unix timestamp
    as the score.  The algorithm:

    1. Remove all members with scores older than ``now - DEFAULT_WINDOW_SIZE``
       (expired entries outside the current window).
    2. Add the current request as a new member with a unique identifier to
       prevent collisions when multiple requests arrive within the same
       millisecond.
    3. Count the remaining members (``ZCARD``) to determine how many
       requests have been made in the current window.
    4. Set an expiry on the key (``EXPIRE``) slightly beyond the window
       size to ensure automatic cleanup.

    Args:
        user_id: Unique identifier for the requester.  Typically the JWT
            ``sub`` claim or the client IP address.
        rate_limit: The maximum number of requests allowed in the current
            sliding window for this user.

    Returns:
        A 3-tuple of:

        - **is_allowed** (``bool``): ``True`` if the request is within the
          rate limit; ``False`` if the limit has been exceeded.
        - **remaining** (``int``): Number of requests remaining before the
          limit is reached.  Clamped to a minimum of ``0``.
        - **reset_time** (``int``): Unix timestamp (seconds) at which the
          current window expires and the counter resets.
    """
    redis_client: Optional[Redis] = _get_redis_client()

    # Fail-open: if Redis is unavailable, allow the request.
    if redis_client is None:
        now = time.time()
        return (True, rate_limit, int(now + DEFAULT_WINDOW_SIZE))

    key: str = f"{REDIS_KEY_PREFIX}{user_id}"
    now: float = time.time()
    window_start: float = now - DEFAULT_WINDOW_SIZE

    # Generate a unique member to prevent ZSET collisions when multiple
    # requests arrive at the same fractional-second timestamp.
    unique_member: str = f"{now}-{uuid.uuid4().hex[:8]}"

    try:
        pipe = redis_client.pipeline(transaction=True)

        # Step 1: Remove entries older than the current window.
        pipe.zremrangebyscore(key, 0, window_start)

        # Step 2: Add the current request with its timestamp as the score.
        pipe.zadd(key, {unique_member: now})

        # Step 3: Count members remaining in the current window.
        pipe.zcard(key)

        # Step 4: Ensure the key expires if no further requests arrive.
        pipe.expire(key, DEFAULT_WINDOW_SIZE + 1)

        results = pipe.execute()

        # results[0] = count of removed members (from ZREMRANGEBYSCORE)
        # results[1] = count of added members (from ZADD)
        # results[2] = total members in set (from ZCARD)
        # results[3] = boolean (from EXPIRE)
        current_count: int = int(results[2])

        is_allowed: bool = current_count <= rate_limit
        remaining: int = max(0, rate_limit - current_count)
        reset_time: int = int(now + DEFAULT_WINDOW_SIZE)

        return (is_allowed, remaining, reset_time)

    except (RedisConnectionError, RedisError) as exc:
        # Fail-open: Redis error should not block the request.
        logger.error(
            "rate_limit_check_failed",
            user_id=user_id,
            error=str(exc),
            error_type=type(exc).__name__,
            action="fail_open",
        )
        return (True, rate_limit, int(now + DEFAULT_WINDOW_SIZE))


# ---------------------------------------------------------------------------
# Flask request hooks
# ---------------------------------------------------------------------------


def rate_limit_middleware() -> Optional[Tuple]:
    """Flask ``before_request`` hook enforcing tiered rate limiting.

    Executed before every incoming request.  Determines the user's rate
    limit tier, checks the sliding window counter in Redis, and either
    allows the request to proceed or returns a ``429 Too Many Requests``
    response with a ``Retry-After`` header.

    **Exempt paths**: Requests to health check endpoints (``/health``,
    ``/ready``) bypass rate limiting entirely to ensure Kubernetes probes
    and load-balancer health checks are never throttled.

    **CORS preflight**: ``OPTIONS`` requests are also exempt so that
    browser-initiated preflight checks are not subject to rate limits.

    **Unauthenticated fallback**: If the upstream auth middleware has not
    set ``g.user_id`` (e.g. for exempt auth paths that are not in the rate
    limit exempt set), the client's IP address (``request.remote_addr``) is
    used as the rate-limit key.

    Returns:
        ``None`` if the request is allowed to proceed (Flask continues to
        the next handler).  Otherwise, returns a tuple of
        ``(response, status_code, headers)`` representing a 429 error.
    """
    # Skip rate limiting for health check endpoints.
    if request.path in RATE_LIMIT_EXEMPT_PATHS:
        return None

    # Skip rate limiting for CORS preflight requests.
    if request.method == "OPTIONS":
        return None

    # Identify the requester.  Prefer the authenticated user_id set by the
    # JWT auth middleware; fall back to the client IP for unauthenticated
    # requests (e.g. login endpoints that may not be auth-exempt but also
    # not rate-limit-exempt).
    user_id: str = getattr(g, "user_id", None) or request.remote_addr or "unknown"

    # Determine the user's rate limit tier.
    rate_limit: int = _get_rate_limit_for_user()

    # Check the sliding window counter.
    is_allowed, remaining, reset_time = _check_rate_limit(user_id, rate_limit)

    if is_allowed:
        # Store rate limit metadata on g for the after_request hook to
        # inject as response headers.
        g.rate_limit = rate_limit
        g.rate_limit_remaining = remaining
        g.rate_limit_reset = reset_time
        return None

    # Rate limit exceeded — compute the number of seconds until the
    # window resets for the Retry-After header.
    seconds_until_reset: int = max(1, reset_time - int(time.time()))

    logger.warning(
        "rate_limit_exceeded",
        user_id=user_id,
        path=request.path,
        method=request.method,
        rate_limit=rate_limit,
        remaining=0,
        reset_time=reset_time,
        retry_after=seconds_until_reset,
    )

    response_body = jsonify(
        {
            "error": "Rate limit exceeded",
            "code": "RATE_LIMIT_EXCEEDED",
            "retry_after": seconds_until_reset,
        }
    )

    return (
        response_body,
        429,
        {
            "Retry-After": str(seconds_until_reset),
            "X-RateLimit-Limit": str(rate_limit),
            "X-RateLimit-Remaining": "0",
            "X-RateLimit-Reset": str(reset_time),
        },
    )


def _add_rate_limit_headers(response: Response) -> Response:
    """Flask ``after_request`` hook that injects rate limit headers.

    Reads the rate limit metadata stored on ``flask.g`` by
    :func:`rate_limit_middleware` and adds the following standard headers
    to every successful response:

    - ``X-RateLimit-Limit``: The user's per-window request quota.
    - ``X-RateLimit-Remaining``: Requests remaining before hitting the limit.
    - ``X-RateLimit-Reset``: Unix timestamp when the current window expires.

    These headers allow API consumers to implement client-side back-off
    strategies and avoid unnecessary 429 errors.

    Args:
        response: The Flask :class:`~flask.Response` object being returned
            to the client.

    Returns:
        The *response* with rate limit headers injected (or unmodified if
        no rate limit metadata is available on ``g``).
    """
    rate_limit = getattr(g, "rate_limit", None)
    remaining = getattr(g, "rate_limit_remaining", None)
    reset_time = getattr(g, "rate_limit_reset", None)

    if rate_limit is not None:
        response.headers["X-RateLimit-Limit"] = str(rate_limit)
    if remaining is not None:
        response.headers["X-RateLimit-Remaining"] = str(remaining)
    if reset_time is not None:
        response.headers["X-RateLimit-Reset"] = str(reset_time)

    return response


# ---------------------------------------------------------------------------
# Public registration function
# ---------------------------------------------------------------------------


def register_rate_limiter(app: Flask) -> None:
    """Register the tiered rate limiting middleware on a Flask application.

    Attaches :func:`rate_limit_middleware` as a ``before_request`` hook and
    :func:`_add_rate_limit_headers` as an ``after_request`` hook.

    **Configuration overrides** (12-Factor methodology — all optional):

    - ``RATE_LIMIT_DEFAULT`` (``int``): Override for the default / Developer
      / QA Engineer / Data Analyst tier (default ``60``).
    - ``RATE_LIMIT_ELEVATED`` (``int``): Override for the Data Engineer tier
      (default ``300``).
    - ``RATE_LIMIT_ADMIN`` (``int``): Override for the Platform Admin tier
      (default ``1000``).
    - ``RATE_LIMIT_WINDOW_SIZE`` (``int``): Override for the sliding window
      duration in seconds (default ``60``).

    Args:
        app: The Flask application instance on which to register the
            middleware hooks.

    Example::

        app = Flask(__name__)
        app.config["RATE_LIMIT_ADMIN"] = 2000  # Double admin quota
        register_rate_limiter(app)
    """
    global DEFAULT_WINDOW_SIZE  # noqa: PLW0603

    # Apply configuration overrides from the Flask app config.  These
    # values may originate from environment variables loaded via the
    # service's config.py module, satisfying the 12-Factor methodology.
    config_default: Optional[int] = app.config.get("RATE_LIMIT_DEFAULT")
    config_elevated: Optional[int] = app.config.get("RATE_LIMIT_ELEVATED")
    config_admin: Optional[int] = app.config.get("RATE_LIMIT_ADMIN")
    config_window: Optional[int] = app.config.get("RATE_LIMIT_WINDOW_SIZE")

    if config_default is not None:
        override_val = int(config_default)
        ROLE_RATE_LIMITS["Developer"] = override_val
        ROLE_RATE_LIMITS["QA Engineer"] = override_val
        ROLE_RATE_LIMITS["Data Analyst"] = override_val
        ROLE_RATE_LIMITS["default"] = override_val

    if config_elevated is not None:
        ROLE_RATE_LIMITS["Data Engineer"] = int(config_elevated)

    if config_admin is not None:
        ROLE_RATE_LIMITS["Platform Admin"] = int(config_admin)

    if config_window is not None:
        DEFAULT_WINDOW_SIZE = int(config_window)

    # Register Flask hooks.
    app.before_request(rate_limit_middleware)
    app.after_request(_add_rate_limit_headers)

    logger.info(
        "rate_limiter_registered",
        role_limits=ROLE_RATE_LIMITS,
        window_size=DEFAULT_WINDOW_SIZE,
        exempt_paths=list(RATE_LIMIT_EXEMPT_PATHS),
    )
