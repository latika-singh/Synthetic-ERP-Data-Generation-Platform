"""Redis 7.x connection manager for the Synthetic ERP Data Generation Platform.

Provides a shared Redis connectivity layer used by all six backend microservices
(API Gateway, Generation Engine, Profiling Service, Quality Service, Compliance
Service, Provisioning Service) for:

- **Connection pooling**: Thread-safe singleton ``redis.Redis`` instance backed by
  ``redis.ConnectionPool`` with configurable pool size.
- **Cache operations**: Key-value caching with TTL management, batch operations
  via pipelines, and production-safe pattern-based invalidation using SCAN.
- **Session management**: JWT token session storage with configurable expiry for
  Auth0 integration across the API Gateway.
- **Pub/Sub**: Real-time job progress updates published by the Generation Engine
  and consumed by the API Gateway for WebSocket relay to the Web Console.
- **Distributed locking**: Redis-backed distributed locks for concurrent job
  coordination across horizontally-scaled service replicas.
- **Health checking**: PING-based health probes with latency measurement for
  Kubernetes readiness endpoints.

Configuration follows 12-factor app methodology via environment variables:

- ``REDIS_URL``: Redis connection URL (default: ``redis://localhost:6379/0``)
- ``REDIS_MAX_CONNECTIONS``: Maximum pool connections (default: ``50``)

Usage::

    from shared.database.redis_client import get_redis_client, cache_set, cache_get

    # Direct client access
    client = get_redis_client()
    client.set("key", "value")

    # Cache utilities
    cache_set("user:123", {"name": "Alice"}, ttl=3600)
    data = cache_get("user:123")

    # Pub/Sub for job progress
    from shared.database.redis_client import publish_progress, create_progress_channel
    channel = create_progress_channel("job-abc-123")
    publish_progress(channel, {"percent": 45, "records": 45000})
"""

from __future__ import annotations

import json
import logging
import os
import threading
import time
from typing import Any, Optional, Union

import redis
import redis.client
import redis.exceptions
import redis.lock
from flask import Flask

# ---------------------------------------------------------------------------
# Module-level logger
# ---------------------------------------------------------------------------
logger: logging.Logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Module-level singleton state
# ---------------------------------------------------------------------------
_redis_client: Optional[redis.Redis] = None  # type: ignore[type-arg]
_redis_pool: Optional[redis.ConnectionPool] = None
_lock: threading.Lock = threading.Lock()

# ---------------------------------------------------------------------------
# Key namespace prefixes (internal convention)
# ---------------------------------------------------------------------------
_SESSION_PREFIX: str = "session:"
_CACHE_PREFIX: str = "cache:"
_PROGRESS_CHANNEL_PREFIX: str = "job:progress:"


# ============================================================================
# Connection management
# ============================================================================


def get_redis_client() -> redis.Redis:  # type: ignore[type-arg]
    """Return a singleton ``redis.Redis`` instance backed by a connection pool.

    The client is lazily initialised on first call using a thread-safe
    double-checked locking pattern so that concurrent Gunicorn workers
    sharing the same process never create duplicate connection pools.

    Configuration is read from environment variables:

    - ``REDIS_URL`` — full Redis connection URL including database number.
      Default: ``redis://localhost:6379/0``
    - ``REDIS_MAX_CONNECTIONS`` — maximum number of pooled connections.
      Default: ``50``

    Returns:
        redis.Redis: A connected Redis client instance.

    Raises:
        redis.exceptions.ConnectionError: If the initial connection cannot
            be established after pool creation.

    Example::

        client = get_redis_client()
        client.set("hello", "world")
        assert client.get("hello") == "world"
    """
    global _redis_client, _redis_pool

    if _redis_client is not None:
        return _redis_client

    with _lock:
        # Double-checked locking — re-verify after acquiring the lock.
        if _redis_client is not None:
            return _redis_client

        redis_url: str = os.environ.get(
            "REDIS_URL", "redis://localhost:6379/0"
        )
        max_connections: int = int(
            os.environ.get("REDIS_MAX_CONNECTIONS", "50")
        )

        logger.info(
            "Initialising Redis connection pool",
            extra={
                "redis_url": _sanitise_url(redis_url),
                "max_connections": max_connections,
            },
        )

        _redis_pool = redis.ConnectionPool.from_url(
            url=redis_url,
            max_connections=max_connections,
            decode_responses=True,
            socket_timeout=5.0,
            socket_connect_timeout=5.0,
            retry_on_timeout=True,
            health_check_interval=30,
        )

        _redis_client = redis.Redis(connection_pool=_redis_pool)

        # Validate connectivity eagerly so callers get a clear error early.
        try:
            _redis_client.ping()
            logger.info("Redis connection established successfully")
        except redis.exceptions.ConnectionError as exc:
            logger.error(
                "Failed to connect to Redis on initialisation",
                extra={"error": str(exc)},
            )
            raise

        return _redis_client


def close_redis_connection(exception: Optional[BaseException] = None) -> None:
    """Safely close the Redis connection pool and reset singleton state.

    This function is designed to be registered as a Flask
    ``teardown_appcontext`` handler so the pool is cleaned up when the
    application shuts down.  It is also safe to call directly during tests
    or manual shutdown procedures.

    Args:
        exception: Optional exception passed by Flask's teardown hook.
            Logged at warning level when present but does not prevent
            cleanup.
    """
    global _redis_client, _redis_pool

    with _lock:
        if exception is not None:
            logger.warning(
                "Closing Redis connection due to exception",
                extra={"error": str(exception)},
            )

        if _redis_client is not None:
            try:
                _redis_client.close()
                logger.debug("Redis client closed")
            except redis.exceptions.RedisError as exc:
                logger.error(
                    "Error closing Redis client",
                    extra={"error": str(exc)},
                )
            _redis_client = None

        if _redis_pool is not None:
            try:
                _redis_pool.disconnect()
                logger.debug("Redis connection pool disconnected")
            except redis.exceptions.RedisError as exc:
                logger.error(
                    "Error disconnecting Redis pool",
                    extra={"error": str(exc)},
                )
            _redis_pool = None

        logger.info("Redis connection resources released")


def check_redis_health() -> dict[str, Any]:
    """Execute a Redis PING and return health status with latency.

    Used by the ``/health`` and ``/ready`` endpoints in every service to
    report Redis connectivity to Kubernetes probes.

    Returns:
        dict: A dictionary with the following keys:

        - ``status`` (str): ``"healthy"`` or ``"unhealthy"``.
        - ``latency_ms`` (float): Round-trip time of the PING in
          milliseconds (``-1.0`` when unhealthy).
        - ``connected_clients`` (int): Number of connected clients
          reported by Redis (``-1`` on error).
        - ``error`` (str | None): Error message when unhealthy.

    Example::

        health = check_redis_health()
        if health["status"] == "healthy":
            print(f"Redis OK — {health['latency_ms']:.1f} ms")
    """
    try:
        client = get_redis_client()

        start: float = time.time()
        client.ping()
        latency_ms: float = (time.time() - start) * 1000.0

        # Attempt to retrieve connected client count.
        connected_clients: int = -1
        try:
            info: dict[str, Any] = client.info(section="clients")
            connected_clients = int(info.get("connected_clients", -1))
        except (redis.exceptions.RedisError, ValueError):
            pass

        logger.debug(
            "Redis health check passed",
            extra={"latency_ms": round(latency_ms, 2)},
        )

        return {
            "status": "healthy",
            "latency_ms": round(latency_ms, 2),
            "connected_clients": connected_clients,
            "error": None,
        }

    except (
        redis.exceptions.ConnectionError,
        redis.exceptions.TimeoutError,
        redis.exceptions.RedisError,
    ) as exc:
        logger.error(
            "Redis health check failed", extra={"error": str(exc)}
        )
        return {
            "status": "unhealthy",
            "latency_ms": -1.0,
            "connected_clients": -1,
            "error": str(exc),
        }


# ============================================================================
# Cache utility functions
# ============================================================================


def cache_get(key: str) -> Optional[Any]:
    """Retrieve a cached value, deserialising JSON for complex types.

    Args:
        key: The cache key to look up.

    Returns:
        The cached Python object (``dict``, ``list``, ``str``, etc.) or
        ``None`` if the key does not exist or has expired.
    """
    try:
        client = get_redis_client()
        raw: Optional[str] = client.get(key)
        if raw is None:
            logger.debug("Cache miss", extra={"key": key})
            return None

        logger.debug("Cache hit", extra={"key": key})
        try:
            return json.loads(raw)
        except (json.JSONDecodeError, TypeError):
            # Value is a plain string, not JSON-encoded.
            return raw

    except redis.exceptions.RedisError as exc:
        logger.error(
            "Cache get failed", extra={"key": key, "error": str(exc)}
        )
        return None


def cache_set(key: str, value: Any, ttl: int = 3600) -> bool:
    """Store a value in the cache with a TTL.

    Complex Python objects (``dict``, ``list``) are automatically
    serialised to JSON.  Scalar strings are stored as-is.

    Args:
        key: The cache key.
        value: The value to cache.  Must be JSON-serialisable for complex
            types.
        ttl: Time-to-live in seconds.  Defaults to ``3600`` (1 hour).

    Returns:
        ``True`` on success, ``False`` on failure.
    """
    try:
        client = get_redis_client()
        serialised: str = (
            json.dumps(value) if not isinstance(value, str) else value
        )
        result: Optional[bool] = client.set(key, serialised, ex=ttl)
        logger.debug(
            "Cache set", extra={"key": key, "ttl": ttl}
        )
        return bool(result)

    except (redis.exceptions.RedisError, TypeError, ValueError) as exc:
        logger.error(
            "Cache set failed", extra={"key": key, "error": str(exc)}
        )
        return False


def cache_delete(key: str) -> bool:
    """Delete a cached key.

    Args:
        key: The cache key to remove.

    Returns:
        ``True`` if the key existed and was deleted, ``False`` otherwise.
    """
    try:
        client = get_redis_client()
        deleted: int = client.delete(key)
        logger.debug("Cache delete", extra={"key": key, "deleted": deleted})
        return deleted > 0

    except redis.exceptions.RedisError as exc:
        logger.error(
            "Cache delete failed", extra={"key": key, "error": str(exc)}
        )
        return False


def cache_exists(key: str) -> bool:
    """Check whether a key exists in the cache.

    Args:
        key: The cache key to test.

    Returns:
        ``True`` if the key exists, ``False`` otherwise.
    """
    try:
        client = get_redis_client()
        return bool(client.exists(key))

    except redis.exceptions.RedisError as exc:
        logger.error(
            "Cache exists check failed",
            extra={"key": key, "error": str(exc)},
        )
        return False


def cache_set_many(mapping: dict[str, Any], ttl: int = 3600) -> bool:
    """Batch-set multiple key-value pairs using a Redis pipeline.

    Each value is JSON-serialised for complex types.  All keys share the
    same TTL.

    Args:
        mapping: Dictionary of ``{key: value}`` pairs to cache.
        ttl: Time-to-live in seconds applied to every key.  Defaults to
            ``3600`` (1 hour).

    Returns:
        ``True`` if the pipeline executed without error, ``False``
        otherwise.
    """
    try:
        client = get_redis_client()
        pipe = client.pipeline(transaction=False)

        for key, value in mapping.items():
            serialised: str = (
                json.dumps(value) if not isinstance(value, str) else value
            )
            pipe.set(key, serialised, ex=ttl)

        pipe.execute()
        logger.debug(
            "Cache set_many completed",
            extra={"count": len(mapping), "ttl": ttl},
        )
        return True

    except (redis.exceptions.RedisError, TypeError, ValueError) as exc:
        logger.error(
            "Cache set_many failed", extra={"error": str(exc)}
        )
        return False


def cache_get_many(keys: list[str]) -> dict[str, Any]:
    """Batch-get multiple keys using a Redis pipeline.

    Missing keys are omitted from the returned dictionary.

    Args:
        keys: List of cache keys to retrieve.

    Returns:
        Dictionary mapping each found key to its deserialised value.
    """
    result: dict[str, Any] = {}
    if not keys:
        return result

    try:
        client = get_redis_client()
        pipe = client.pipeline(transaction=False)

        for key in keys:
            pipe.get(key)

        raw_values = pipe.execute()

        for key, raw in zip(keys, raw_values, strict=False):
            if raw is None:
                continue
            try:
                result[key] = json.loads(raw)
            except (json.JSONDecodeError, TypeError):
                result[key] = raw

        logger.debug(
            "Cache get_many completed",
            extra={"requested": len(keys), "found": len(result)},
        )
        return result

    except redis.exceptions.RedisError as exc:
        logger.error(
            "Cache get_many failed", extra={"error": str(exc)}
        )
        return result


def cache_invalidate_pattern(pattern: str) -> int:
    """Delete all keys matching a glob *pattern* using SCAN.

    Unlike ``KEYS``, ``SCAN`` is cursor-based and safe for production use
    because it does not block the Redis event loop on large key-spaces.

    Args:
        pattern: Glob-style pattern (e.g. ``"user:*"``, ``"cache:job:*"``).

    Returns:
        The number of keys deleted.
    """
    deleted_count: int = 0

    try:
        client = get_redis_client()
        cursor_keys: list[str] = []

        for key in client.scan_iter(match=pattern, count=500):
            cursor_keys.append(key)
            # Flush deletes in batches of 500 to limit memory pressure.
            if len(cursor_keys) >= 500:
                deleted_count += client.delete(*cursor_keys)
                cursor_keys = []

        if cursor_keys:
            deleted_count += client.delete(*cursor_keys)

        logger.info(
            "Cache pattern invalidation completed",
            extra={"pattern": pattern, "deleted": deleted_count},
        )
        return deleted_count

    except redis.exceptions.RedisError as exc:
        logger.error(
            "Cache invalidate_pattern failed",
            extra={"pattern": pattern, "error": str(exc)},
        )
        return deleted_count


# ============================================================================
# Session management
# ============================================================================


def session_store(
    session_id: str, data: dict[str, Any], ttl: int = 86400
) -> bool:
    """Store session data (e.g. cached JWT claims) with a TTL.

    Session keys are namespaced under ``session:<session_id>`` so they
    can be invalidated independently of general cache keys.

    Args:
        session_id: Unique identifier for the session (e.g. a JTI or
            opaque token).
        data: Arbitrary JSON-serialisable session payload.
        ttl: Time-to-live in seconds.  Defaults to ``86400`` (24 hours).

    Returns:
        ``True`` on success, ``False`` on failure.
    """
    try:
        client = get_redis_client()
        full_key: str = f"{_SESSION_PREFIX}{session_id}"
        serialised: str = json.dumps(data)
        result: Optional[bool] = client.set(full_key, serialised, ex=ttl)
        logger.debug(
            "Session stored",
            extra={"session_id": session_id, "ttl": ttl},
        )
        return bool(result)

    except (redis.exceptions.RedisError, TypeError, ValueError) as exc:
        logger.error(
            "Session store failed",
            extra={"session_id": session_id, "error": str(exc)},
        )
        return False


def session_get(session_id: str) -> Optional[dict[str, Any]]:
    """Retrieve session data.

    Args:
        session_id: The session identifier used during ``session_store``.

    Returns:
        The session payload as a dictionary, or ``None`` if the session
        does not exist or has expired.
    """
    try:
        client = get_redis_client()
        full_key: str = f"{_SESSION_PREFIX}{session_id}"
        raw: Optional[str] = client.get(full_key)
        if raw is None:
            logger.debug(
                "Session not found",
                extra={"session_id": session_id},
            )
            return None

        logger.debug(
            "Session retrieved", extra={"session_id": session_id}
        )
        return json.loads(raw)

    except (
        redis.exceptions.RedisError,
        json.JSONDecodeError,
    ) as exc:
        logger.error(
            "Session get failed",
            extra={"session_id": session_id, "error": str(exc)},
        )
        return None


def session_delete(session_id: str) -> bool:
    """Delete a session (e.g. on user logout).

    Args:
        session_id: The session identifier to remove.

    Returns:
        ``True`` if the session existed and was deleted, ``False``
        otherwise.
    """
    try:
        client = get_redis_client()
        full_key: str = f"{_SESSION_PREFIX}{session_id}"
        deleted: int = client.delete(full_key)
        logger.debug(
            "Session deleted",
            extra={"session_id": session_id, "deleted": deleted},
        )
        return deleted > 0

    except redis.exceptions.RedisError as exc:
        logger.error(
            "Session delete failed",
            extra={"session_id": session_id, "error": str(exc)},
        )
        return False


def session_extend(session_id: str, ttl: int = 86400) -> bool:
    """Extend the expiry of an existing session.

    Useful for sliding-window session expiry where activity resets the
    countdown.

    Args:
        session_id: The session identifier whose TTL should be extended.
        ttl: New time-to-live in seconds.  Defaults to ``86400``
            (24 hours).

    Returns:
        ``True`` if the session exists and its TTL was extended,
        ``False`` if the session does not exist or on error.
    """
    try:
        client = get_redis_client()
        full_key: str = f"{_SESSION_PREFIX}{session_id}"
        result: bool = client.expire(full_key, ttl)
        if result:
            logger.debug(
                "Session TTL extended",
                extra={"session_id": session_id, "ttl": ttl},
            )
        else:
            logger.debug(
                "Session not found for TTL extension",
                extra={"session_id": session_id},
            )
        return result

    except redis.exceptions.RedisError as exc:
        logger.error(
            "Session extend failed",
            extra={"session_id": session_id, "error": str(exc)},
        )
        return False


# ============================================================================
# Pub/Sub — real-time job progress
# ============================================================================


def create_progress_channel(job_id: str) -> str:
    """Generate a standardised Redis Pub/Sub channel name for a job.

    The naming convention ``job:progress:<job_id>`` is shared between the
    Generation Engine (publisher) and the API Gateway (subscriber /
    WebSocket relay).

    Args:
        job_id: Unique generation job identifier.

    Returns:
        The channel name string, e.g. ``"job:progress:abc-123"``.
    """
    return f"{_PROGRESS_CHANNEL_PREFIX}{job_id}"


def publish_progress(channel: str, message: dict[str, Any]) -> int:
    """Publish a job progress update to a Redis Pub/Sub channel.

    The Generation Engine calls this function during batch processing to
    notify subscribers (API Gateway → Web Console) of progress changes.

    The *message* dictionary is JSON-serialised before publishing.

    Args:
        channel: Target channel name (use ``create_progress_channel``).
        message: Progress payload, e.g.::

            {"percent": 45, "records_generated": 45000, "status": "generating"}

    Returns:
        The number of subscribers that received the message.

    Raises:
        redis.exceptions.RedisError: On critical publish failure (logged
            but propagated so callers can decide on retry).
    """
    try:
        client = get_redis_client()
        serialised: str = json.dumps(message)
        receivers: int = client.publish(channel, serialised)
        logger.debug(
            "Progress published",
            extra={
                "channel": channel,
                "receivers": receivers,
            },
        )
        return receivers

    except redis.exceptions.RedisError as exc:
        logger.error(
            "Progress publish failed",
            extra={"channel": channel, "error": str(exc)},
        )
        raise


def subscribe_progress(channel: str) -> redis.client.PubSub:
    """Create a Pub/Sub subscription for real-time job progress.

    The API Gateway uses this to listen for progress events and relay
    them to the Web Console over WebSockets.

    .. note::
        The returned ``PubSub`` object should be iterated in a dedicated
        thread or async task.  Remember to call ``pubsub.close()`` when
        done to release the underlying connection.

    Args:
        channel: Channel name to subscribe to (use
            ``create_progress_channel``).

    Returns:
        A ``redis.client.PubSub`` instance already subscribed to the
        given channel.

    Example::

        pubsub = subscribe_progress(create_progress_channel("job-123"))
        for msg in pubsub.listen():
            if msg["type"] == "message":
                data = json.loads(msg["data"])
                print(data)
        pubsub.close()
    """
    client = get_redis_client()
    pubsub: redis.client.PubSub = client.pubsub()
    pubsub.subscribe(channel)
    logger.info(
        "Subscribed to progress channel", extra={"channel": channel}
    )
    return pubsub


# ============================================================================
# Distributed locking
# ============================================================================


def acquire_lock(
    lock_name: str,
    timeout: int = 30,
    blocking_timeout: int = 10,
) -> Optional[redis.lock.Lock]:
    """Acquire a Redis distributed lock for concurrent job coordination.

    Uses the Redis ``SET NX`` based locking mechanism provided by
    ``redis-py`` which is safe across multiple service replicas.

    Args:
        lock_name: Logical name identifying the resource to lock (e.g.
            ``"job:generation:abc-123"``).
        timeout: Maximum number of seconds the lock is held before
            automatic release.  Defaults to ``30``.
        blocking_timeout: Maximum number of seconds to wait while
            attempting to acquire the lock.  Defaults to ``10``.

    Returns:
        A ``redis.lock.Lock`` instance if acquired, or ``None`` if the
        lock could not be obtained within *blocking_timeout*.
    """
    try:
        client = get_redis_client()
        lock: redis.lock.Lock = client.lock(
            name=lock_name,
            timeout=timeout,
            blocking_timeout=blocking_timeout,
        )
        acquired: bool = lock.acquire()
        if acquired:
            logger.debug(
                "Distributed lock acquired",
                extra={"lock_name": lock_name, "timeout": timeout},
            )
            return lock

        logger.warning(
            "Failed to acquire distributed lock within blocking timeout",
            extra={
                "lock_name": lock_name,
                "blocking_timeout": blocking_timeout,
            },
        )
        return None

    except redis.exceptions.RedisError as exc:
        logger.error(
            "Distributed lock acquisition error",
            extra={"lock_name": lock_name, "error": str(exc)},
        )
        return None


def release_lock(lock: redis.lock.Lock) -> bool:
    """Release a previously acquired distributed lock.

    Safe to call even if the lock has already expired or been released;
    errors are caught and logged.

    Args:
        lock: The ``redis.lock.Lock`` instance returned by
            ``acquire_lock``.

    Returns:
        ``True`` if the lock was successfully released, ``False``
        otherwise (e.g. already expired).
    """
    try:
        lock.release()
        logger.debug("Distributed lock released")
        return True

    except redis.exceptions.LockNotOwnedError:
        logger.warning(
            "Attempted to release a lock no longer owned (expired or "
            "released by another process)"
        )
        return False

    except redis.exceptions.RedisError as exc:
        logger.error(
            "Distributed lock release error",
            extra={"error": str(exc)},
        )
        return False


# ============================================================================
# Flask integration
# ============================================================================


def init_redis(app: Flask) -> None:
    """Initialise Redis within a Flask application context.

    Registers ``close_redis_connection`` as a ``teardown_appcontext``
    handler so the connection pool is automatically cleaned up when the
    Flask application shuts down.

    Should be called once during application factory setup::

        def create_app():
            app = Flask(__name__)
            init_redis(app)
            return app

    Args:
        app: The Flask application instance.
    """
    # Eagerly create the connection so misconfigurations surface at
    # startup rather than on the first request.
    with app.app_context():
        get_redis_client()

    app.teardown_appcontext(close_redis_connection)

    logger.info(
        "Redis initialised for Flask application",
        extra={"app_name": app.name},
    )


# ============================================================================
# Internal helpers
# ============================================================================


def _sanitise_url(url: str) -> str:
    """Mask credentials in a Redis URL for safe logging.

    Replaces the ``password`` component (if present) with ``***`` so
    that connection URLs can appear in log output without leaking
    secrets.

    Args:
        url: The raw Redis connection URL.

    Returns:
        A sanitised copy of the URL safe for logging.
    """
    # redis://user:password@host:port/db → redis://user:***@host:port/db
    if "@" in url:
        scheme_and_creds, host_part = url.rsplit("@", 1)
        if ":" in scheme_and_creds.split("//", 1)[-1]:
            scheme = scheme_and_creds.split("//", 1)[0] + "//"
            creds = scheme_and_creds.split("//", 1)[-1]
            user = creds.rsplit(":", 1)[0]
            return f"{scheme}{user}:***@{host_part}"
    return url
