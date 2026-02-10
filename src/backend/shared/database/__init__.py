"""Database utilities package for the Synthetic ERP Data Generation Platform.

Provides MongoDB and Redis connection management utilities shared across all
six backend microservices (API Gateway, Generation Engine, Profiling Service,
Quality Service, Compliance Service, Provisioning Service).

This package re-exports the primary public API from :pymod:`mongodb` and
:pymod:`redis_client` so that consumers can use the convenience path::

    from shared.database import get_mongo_client, get_redis_client

All substantial logic resides in the respective sub-modules.
"""

from __future__ import annotations

# ---------------------------------------------------------------------------
# MongoDB re-exports
# ---------------------------------------------------------------------------
from shared.database.mongodb import (
    check_mongo_health,
    close_mongo_connection,
    get_mongo_client,
    get_mongo_db,
)

# ---------------------------------------------------------------------------
# Redis re-exports
# ---------------------------------------------------------------------------
from shared.database.redis_client import (
    check_redis_health,
    close_redis_connection,
    get_redis_client,
)


__all__: list[str] = [
    "check_mongo_health",
    "check_redis_health",
    "close_mongo_connection",
    "close_redis_connection",
    "get_mongo_client",
    "get_mongo_db",
    "get_redis_client",
]
