"""MongoDB connection manager for the Synthetic ERP Data Generation Platform.

Provides centralized MongoDB connectivity using PyMongo 4.x with connection pooling,
singleton pattern for MongoClient reuse across Flask request lifecycles, TLS/SSL support,
automatic retry for transient errors, and health checking for Kubernetes readiness probes.

This module is imported by all six backend microservices (API Gateway, Generation Engine,
Profiling Service, Quality Service, Compliance Service, Provisioning Service) to access the
five core MongoDB 7.0 collections:
    - generation_profiles
    - statistical_profiles
    - schema_definitions
    - audit_logs
    - tenant_configurations

Configuration follows 12-factor app methodology — all settings are read from environment
variables with sensible defaults for local development.

Environment Variables:
    MONGODB_URI: MongoDB connection URI (default: 'mongodb://localhost:27017/synthetic_erp')
    MONGODB_DATABASE: Default database name (default: 'synthetic_erp')
    MONGODB_MAX_POOL_SIZE: Maximum connection pool size (default: 100)
    MONGODB_TLS_ENABLED: Enable TLS/SSL connections ('true'/'false', default: 'false')
    MONGODB_TLS_CA_FILE: Path to TLS CA certificate file (when TLS enabled)
    SERVICE_NAME: Application name for MongoDB connection identification in logs

Usage:
    from shared.database.mongodb import get_mongo_client, get_mongo_db, get_collection

    # Get raw MongoClient for advanced operations
    client = get_mongo_client()

    # Get the default database
    db = get_mongo_db()

    # Get a specific collection
    profiles = get_collection(COLLECTION_GENERATION_PROFILES)
"""

from __future__ import annotations

import logging
import os
import threading
import time
from typing import TYPE_CHECKING, Any

from pymongo import ASCENDING, MongoClient
from pymongo.errors import (
    AutoReconnect,
    ConnectionFailure,
    OperationFailure,
    ServerSelectionTimeoutError,
)


if TYPE_CHECKING:
    from flask import Flask
    from pymongo.collection import Collection
    from pymongo.database import Database


# ---------------------------------------------------------------------------
# Module logger — uses standard logging to avoid circular dependency with
# shared.logging.structured_logger which wraps this at the application level.
# ---------------------------------------------------------------------------
logger: logging.Logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Collection name constants — canonical names for the five core MongoDB
# collections used across all microservices.  Import these constants instead
# of hard-coding collection name strings to ensure consistency.
# ---------------------------------------------------------------------------
COLLECTION_GENERATION_PROFILES: str = "generation_profiles"
COLLECTION_STATISTICAL_PROFILES: str = "statistical_profiles"
COLLECTION_SCHEMA_DEFINITIONS: str = "schema_definitions"
COLLECTION_AUDIT_LOGS: str = "audit_logs"
COLLECTION_TENANT_CONFIGURATIONS: str = "tenant_configurations"

# Complete set of known collection names for validation in get_collection()
_KNOWN_COLLECTIONS: frozenset[str] = frozenset(
    {
        COLLECTION_GENERATION_PROFILES,
        COLLECTION_STATISTICAL_PROFILES,
        COLLECTION_SCHEMA_DEFINITIONS,
        COLLECTION_AUDIT_LOGS,
        COLLECTION_TENANT_CONFIGURATIONS,
    }
)

# ---------------------------------------------------------------------------
# Module-level singleton state — thread-safe via _lock.
# ---------------------------------------------------------------------------
_mongo_client: MongoClient | None = None
_lock: threading.Lock = threading.Lock()


# ===================================================================
# Public API
# ===================================================================


def get_mongo_client() -> MongoClient:
    """Return a singleton ``pymongo.MongoClient`` instance with connection pooling.

    The client is lazily initialised on first call and reused for all subsequent
    calls within the same process.  Initialisation is thread-safe via a
    double-checked locking pattern so that concurrent Gunicorn workers do not
    race to create multiple clients.

    Connection parameters are read exclusively from environment variables,
    following 12-factor app methodology.  Sensible defaults are provided for
    local development against a ``docker-compose`` MongoDB instance.

    Environment Variables:
        MONGODB_URI: Full connection URI including protocol, host(s), port,
            and optional authentication credentials.
            Default: ``mongodb://localhost:27017/synthetic_erp``
        MONGODB_MAX_POOL_SIZE: Maximum number of connections in the pool.
            Default: ``100``
        MONGODB_TLS_ENABLED: Set to ``'true'`` to enable TLS/SSL.
            Default: ``'false'``
        MONGODB_TLS_CA_FILE: Filesystem path to the TLS CA certificate
            bundle.  Only used when TLS is enabled.
        SERVICE_NAME: Name reported to MongoDB in connection metadata for
            easier debugging in server logs.
            Default: ``'synthetic-erp-service'``

    Returns:
        MongoClient: A connected ``pymongo.MongoClient`` instance backed by a
            connection pool of up to *MONGODB_MAX_POOL_SIZE* connections.

    Raises:
        ConnectionFailure: If the initial connection to MongoDB cannot be
            established within the configured timeouts.
        ServerSelectionTimeoutError: If no suitable server is found within
            the server-selection timeout window.

    Example::

        client = get_mongo_client()
        db = client["synthetic_erp"]
        collection = db["generation_profiles"]
    """
    global _mongo_client  # noqa: PLW0603

    # Fast path — already initialised (no lock acquisition required).
    if _mongo_client is not None:
        return _mongo_client

    with _lock:
        # Double-check inside the lock to prevent race conditions.
        if _mongo_client is not None:
            return _mongo_client

        uri: str = os.environ.get(
            "MONGODB_URI", "mongodb://localhost:27017/synthetic_erp"
        )
        max_pool_size: int = int(os.environ.get("MONGODB_MAX_POOL_SIZE", "100"))
        tls_enabled: bool = (
            os.environ.get("MONGODB_TLS_ENABLED", "false").lower() == "true"
        )
        tls_ca_file: str | None = os.environ.get("MONGODB_TLS_CA_FILE")
        service_name: str = os.environ.get("SERVICE_NAME", "synthetic-erp-service")

        # Build keyword arguments for MongoClient construction.
        client_kwargs: dict[str, Any] = {
            "host": uri,
            "maxPoolSize": max_pool_size,
            "minPoolSize": 10,
            "maxIdleTimeMS": 45_000,
            "serverSelectionTimeoutMS": 5_000,
            "connectTimeoutMS": 10_000,
            "socketTimeoutMS": 20_000,
            "retryWrites": True,
            "retryReads": True,
            "w": "majority",
            "readPreference": "primaryPreferred",
            "appname": service_name,
        }

        # Conditionally enable TLS/SSL when the environment flag is set.
        if tls_enabled:
            client_kwargs["tls"] = True
            if tls_ca_file:
                client_kwargs["tlsCAFile"] = tls_ca_file
            logger.info(
                "MongoDB TLS enabled",
                extra={
                    "tls_ca_file": tls_ca_file or "system-default",
                    "service": service_name,
                },
            )

        try:
            client: MongoClient = MongoClient(**client_kwargs)

            # Force a connection attempt so that configuration errors surface
            # immediately rather than on the first real operation.
            client.admin.command("ping")

            _mongo_client = client

            logger.info(
                "MongoDB connection established",
                extra={
                    "uri_host": _sanitise_uri(uri),
                    "max_pool_size": max_pool_size,
                    "tls_enabled": tls_enabled,
                    "service": service_name,
                },
            )
        except (ConnectionFailure, ServerSelectionTimeoutError) as exc:
            logger.error(
                "Failed to establish MongoDB connection",
                extra={"error": str(exc), "uri_host": _sanitise_uri(uri)},
            )
            raise

    return _mongo_client


def get_mongo_db(database_name: str | None = None) -> Database:
    """Return a ``pymongo.database.Database`` handle for the specified database.

    If *database_name* is ``None`` the value of the ``MONGODB_DATABASE``
    environment variable is used (defaulting to ``'synthetic_erp'``).

    Args:
        database_name: Explicit database name override.  When ``None``, the
            default database configured via environment variable is used.

    Returns:
        Database: A PyMongo ``Database`` object connected to the requested
            database on the singleton ``MongoClient``.

    Example::

        db = get_mongo_db()
        docs = db["generation_profiles"].find({"status": "completed"})
    """
    if database_name is None:
        database_name = os.environ.get("MONGODB_DATABASE", "synthetic_erp")
    client = get_mongo_client()
    return client[database_name]


def get_collection(
    collection_name: str,
    database_name: str | None = None,
) -> Collection:
    """Return a ``pymongo.collection.Collection`` handle for direct collection access.

    This is a convenience wrapper combining :func:`get_mongo_db` and collection
    lookup.  If *collection_name* is not one of the five well-known core
    collections a warning is logged (but the operation still succeeds, since
    services may legitimately use ad-hoc collections).

    Args:
        collection_name: Name of the MongoDB collection.
        database_name: Optional database name override.

    Returns:
        Collection: A PyMongo ``Collection`` object ready for queries.

    Example::

        from shared.database.mongodb import get_collection, COLLECTION_AUDIT_LOGS
        audit = get_collection(COLLECTION_AUDIT_LOGS)
        audit.insert_one({"event": "login", "user_id": "u-123"})
    """
    if collection_name not in _KNOWN_COLLECTIONS:
        logger.warning(
            "Accessing unknown MongoDB collection",
            extra={"collection_name": collection_name},
        )
    db = get_mongo_db(database_name)
    return db[collection_name]


def close_mongo_connection(exception: BaseException | None = None) -> None:
    """Safely close the MongoDB connection and reset the singleton state.

    Designed to be registered as a Flask ``teardown_appcontext`` handler so
    that the connection pool is properly drained when the application shuts
    down.  Also suitable for explicit cleanup in tests or CLI utilities.

    The *exception* parameter is accepted (and ignored) to satisfy the
    signature expected by ``Flask.teardown_appcontext``.

    Args:
        exception: Optional exception passed by Flask on context teardown.
            This parameter is unused but required for the teardown callback
            signature.
    """
    global _mongo_client  # noqa: PLW0603

    with _lock:
        if _mongo_client is not None:
            try:
                _mongo_client.close()
                logger.info("MongoDB connection closed gracefully")
            except Exception as exc:
                logger.error(
                    "Error while closing MongoDB connection",
                    extra={"error": str(exc)},
                )
            finally:
                _mongo_client = None


def check_mongo_health() -> dict[str, Any]:
    """Execute a lightweight health check against the MongoDB deployment.

    Sends an ``admin.command('ping')`` and measures the round-trip latency.
    This function is used by the ``/health`` and ``/ready`` endpoints across
    all microservices for Kubernetes liveness and readiness probes.

    Returns:
        dict: A dictionary with the following keys:

            - ``status`` (str): ``'healthy'`` or ``'unhealthy'``.
            - ``latency_ms`` (float): Round-trip time of the ``ping``
              command in milliseconds.  ``-1.0`` when the check fails.
            - ``server_info`` (dict): Contains ``version`` and ``ok`` from
              the server, or ``error`` details on failure.

    Example::

        health = check_mongo_health()
        if health["status"] == "unhealthy":
            alert_ops_team(health)
    """
    try:
        client = get_mongo_client()
        start = time.time()
        result = client.admin.command("ping")
        latency_ms = (time.time() - start) * 1000.0

        # Retrieve server build info for version reporting.
        try:
            build_info = client.admin.command("buildInfo")
            server_version = build_info.get("version", "unknown")
        except (OperationFailure, ConnectionFailure):
            server_version = "unknown"

        health: dict[str, Any] = {
            "status": "healthy",
            "latency_ms": round(latency_ms, 2),
            "server_info": {
                "version": server_version,
                "ok": result.get("ok", 0),
            },
        }
        logger.debug(
            "MongoDB health check passed",
            extra={"latency_ms": health["latency_ms"]},
        )
        return health

    except AutoReconnect as exc:
        # AutoReconnect is a subclass of ConnectionFailure — it must be
        # caught *before* ConnectionFailure to distinguish transient
        # reconnection events from hard connection failures.
        logger.warning(
            "MongoDB health check encountered auto-reconnect",
            extra={"error": str(exc)},
        )
        return {
            "status": "unhealthy",
            "latency_ms": -1.0,
            "server_info": {
                "error": f"AutoReconnect: {exc}",
                "ok": 0,
            },
        }
    except (ConnectionFailure, ServerSelectionTimeoutError) as exc:
        logger.error(
            "MongoDB health check failed",
            extra={"error": str(exc)},
        )
        return {
            "status": "unhealthy",
            "latency_ms": -1.0,
            "server_info": {
                "error": str(exc),
                "ok": 0,
            },
        }


def ensure_indexes() -> None:
    """Create or verify indexes on the five core MongoDB collections.

    Indexes are created with ``background=True`` so that the operation does not
    block ongoing read/write operations in production.  This function is
    idempotent — calling it multiple times is safe because MongoDB's
    ``create_index`` is a no-op when the index already exists with the same
    specification.

    Index Specifications:
        **generation_profiles**
            - Compound index on ``(tenant_id, status, created_at)`` for
              filtered listing queries.
            - Unique index on ``job_id`` for direct lookup.

        **statistical_profiles**
            - Compound index on ``(tenant_id, schema_id)`` for scoped queries.
            - Index on ``created_at`` for chronological ordering.

        **schema_definitions**
            - Compound index on ``(tenant_id, erp_type)`` for ERP-scoped
              schema browsing.
            - Unique index on ``schema_id`` for direct lookup.

        **audit_logs**
            - Compound index on ``(tenant_id, timestamp)`` for tenant-scoped
              audit queries.
            - Index on ``event_type`` for event filtering.
            - TTL index on ``timestamp`` with 7-year (2556-day) expiry for
              automatic document expiration.

        **tenant_configurations**
            - Unique index on ``tenant_id`` for direct lookup.
    """
    db = get_mongo_db()

    # ---- generation_profiles ----
    gen_profiles: Collection = db[COLLECTION_GENERATION_PROFILES]
    try:
        gen_profiles.create_index(
            [
                ("tenant_id", ASCENDING),
                ("status", ASCENDING),
                ("created_at", ASCENDING),
            ],
            name="idx_tenant_status_created",
            background=True,
        )
        gen_profiles.create_index(
            "job_id",
            name="idx_job_id_unique",
            unique=True,
            background=True,
        )
        logger.info(
            "Indexes ensured for collection",
            extra={"collection": COLLECTION_GENERATION_PROFILES},
        )
    except OperationFailure as exc:
        logger.error(
            "Failed to create indexes on generation_profiles",
            extra={"error": str(exc)},
        )

    # ---- statistical_profiles ----
    stat_profiles: Collection = db[COLLECTION_STATISTICAL_PROFILES]
    try:
        stat_profiles.create_index(
            [
                ("tenant_id", ASCENDING),
                ("schema_id", ASCENDING),
            ],
            name="idx_tenant_schema",
            background=True,
        )
        stat_profiles.create_index(
            "created_at",
            name="idx_created_at",
            background=True,
        )
        logger.info(
            "Indexes ensured for collection",
            extra={"collection": COLLECTION_STATISTICAL_PROFILES},
        )
    except OperationFailure as exc:
        logger.error(
            "Failed to create indexes on statistical_profiles",
            extra={"error": str(exc)},
        )

    # ---- schema_definitions ----
    schema_defs: Collection = db[COLLECTION_SCHEMA_DEFINITIONS]
    try:
        schema_defs.create_index(
            [
                ("tenant_id", ASCENDING),
                ("erp_type", ASCENDING),
            ],
            name="idx_tenant_erp_type",
            background=True,
        )
        schema_defs.create_index(
            "schema_id",
            name="idx_schema_id_unique",
            unique=True,
            background=True,
        )
        logger.info(
            "Indexes ensured for collection",
            extra={"collection": COLLECTION_SCHEMA_DEFINITIONS},
        )
    except OperationFailure as exc:
        logger.error(
            "Failed to create indexes on schema_definitions",
            extra={"error": str(exc)},
        )

    # ---- audit_logs ----
    audit_logs: Collection = db[COLLECTION_AUDIT_LOGS]
    try:
        audit_logs.create_index(
            [
                ("tenant_id", ASCENDING),
                ("timestamp", ASCENDING),
            ],
            name="idx_tenant_timestamp",
            background=True,
        )
        audit_logs.create_index(
            "event_type",
            name="idx_event_type",
            background=True,
        )
        # TTL index for automatic 7-year document expiration.
        # 7 years ≈ 2556 days = 220_838_400 seconds.
        audit_logs.create_index(
            "timestamp",
            name="idx_timestamp_ttl",
            expireAfterSeconds=220_838_400,
            background=True,
        )
        logger.info(
            "Indexes ensured for collection",
            extra={"collection": COLLECTION_AUDIT_LOGS},
        )
    except OperationFailure as exc:
        logger.error(
            "Failed to create indexes on audit_logs",
            extra={"error": str(exc)},
        )

    # ---- tenant_configurations ----
    tenant_cfg: Collection = db[COLLECTION_TENANT_CONFIGURATIONS]
    try:
        tenant_cfg.create_index(
            "tenant_id",
            name="idx_tenant_id_unique",
            unique=True,
            background=True,
        )
        logger.info(
            "Indexes ensured for collection",
            extra={"collection": COLLECTION_TENANT_CONFIGURATIONS},
        )
    except OperationFailure as exc:
        logger.error(
            "Failed to create indexes on tenant_configurations",
            extra={"error": str(exc)},
        )

    logger.info("MongoDB index creation/verification complete for all core collections")


def init_mongodb(app: Flask) -> None:
    """Initialise MongoDB connectivity within a Flask application context.

    Registers :func:`close_mongo_connection` as a ``teardown_appcontext``
    handler so that the connection pool is automatically drained when the Flask
    application shuts down or the application context is torn down.

    Optionally calls :func:`ensure_indexes` to guarantee that required indexes
    exist on first startup.

    This function should be called once during application factory
    initialisation (inside ``create_app()``).

    Args:
        app: The Flask application instance to bind MongoDB lifecycle to.

    Example::

        from flask import Flask
        from shared.database.mongodb import init_mongodb

        def create_app() -> Flask:
            app = Flask(__name__)
            init_mongodb(app)
            return app
    """
    # Register the teardown handler for automatic connection cleanup.
    app.teardown_appcontext(close_mongo_connection)

    # Eagerly verify connectivity and create indexes during startup so that
    # configuration errors are surfaced immediately.
    with app.app_context():
        try:
            get_mongo_client()
            ensure_indexes()
            logger.info(
                "MongoDB initialised for Flask application",
                extra={"app_name": app.name},
            )
        except (ConnectionFailure, ServerSelectionTimeoutError) as exc:
            # Log but do not raise — the service may still start and recover
            # once MongoDB becomes available (e.g. in Kubernetes rolling
            # deployments where the database pod starts after the app pod).
            logger.error(
                "MongoDB unavailable during Flask initialisation; "
                "service will retry on first request",
                extra={"error": str(exc), "app_name": app.name},
            )


# ===================================================================
# Private helpers
# ===================================================================


def _sanitise_uri(uri: str) -> str:
    """Remove credentials from a MongoDB URI for safe logging.

    Strips the ``userinfo`` section (``user:password@``) from the URI so
    that connection strings can be logged without leaking secrets.

    Args:
        uri: Full MongoDB connection URI.

    Returns:
        str: A redacted URI suitable for inclusion in log messages.
    """
    # Handle standard mongodb:// and mongodb+srv:// schemes.
    if "@" in uri:
        scheme_end = uri.find("://")
        if scheme_end != -1:
            after_scheme = uri[scheme_end + 3 :]
            host_start = after_scheme.find("@")
            if host_start != -1:
                return uri[: scheme_end + 3] + "***:***@" + after_scheme[host_start + 1 :]
    return uri
