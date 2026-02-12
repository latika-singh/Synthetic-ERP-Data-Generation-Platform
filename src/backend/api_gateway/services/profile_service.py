"""Business logic service for statistical profile management.

This module provides the :class:`ProfileService` class, which acts as the
API Gateway's intermediary to the Profiling Service microservice.  It
encapsulates all business logic for statistical profile lifecycle
management — creation, retrieval, listing, statistics access, and deletion
— with built-in resilience patterns (circuit breaker), caching (Redis),
multi-tenant isolation (R-007), and structured JSON logging (R-013).

Design Patterns:
    - **Service Layer** — Separates HTTP route handling from domain logic
      so that route handlers remain thin request/response translators.
    - **Circuit Breaker** — Applied to the outbound HTTP call to the
      Profiling Service via the ``circuitbreaker`` library (5 failures →
      open circuit, 30 s recovery timeout) to prevent cascade failures
      when the downstream service is unavailable.
    - **Cache-Aside** — Redis is used as a read-through cache with a
      3 600 s TTL for individual profile documents, reducing MongoDB
      read load for frequently accessed profiles.
    - **Repository-Lite** — Direct PyMongo access to the
      ``statistical_profiles`` collection with tenant-scoped queries.

Inter-Service Communication:
    Profile creation dispatches an HTTP POST to the Profiling Service
    (``PROFILING_SERVICE_URL``) which performs the actual ERP schema
    extraction and statistical profiling.  Only metadata flows through
    the system — no raw production data is accessed (Constraint C-001).

Usage::

    from api_gateway.services.profile_service import ProfileService

    service = ProfileService()
    result = service.create_profile(
        tenant_id="tenant-42",
        user_id="user-7",
        source_connection={"erp_type": "sap", "connection_params": {...}},
        tables=["GL_ACCOUNTS", "GL_JOURNAL_ENTRIES"],
    )

Environment Variables (via ``current_app.config``):
    PROFILING_SERVICE_URL: Base URL of the Profiling Service
        (e.g. ``http://profiling-service:5001``).
"""

from __future__ import annotations

import json
import logging
import uuid
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

import httpx
from circuitbreaker import circuit
from flask import current_app

from api_gateway.extensions import get_db, get_redis
from shared.logging.structured_logger import get_logger

# ---------------------------------------------------------------------------
# Module-level constants
# ---------------------------------------------------------------------------

COLLECTION_NAME: str = "statistical_profiles"
"""Name of the MongoDB collection used for storing statistical profile
documents.  Matches the five core collections defined in the data layer
specification."""

# Supported ERP types for input validation.
_SUPPORTED_ERP_TYPES: List[str] = ["sap", "oracle", "dynamics", "legacy"]

# Redis cache key prefix and TTL.
_CACHE_KEY_PREFIX: str = "profile"
_CACHE_TTL_SECONDS: int = 3600  # 1 hour

# HTTP dispatch timeout (profiling operations can be slow).
_DISPATCH_TIMEOUT_SECONDS: int = 60


class ProfileService:
    """Business logic service for statistical profile CRUD and inter-service calls.

    All public methods enforce multi-tenant isolation by requiring a
    ``tenant_id`` parameter and including it in every MongoDB query filter
    (R-007).  External HTTP calls to the Profiling Service are protected
    by a circuit breaker to prevent cascade failures.

    Attributes:
        logger: A structlog ``BoundLogger`` instance for structured JSON
            logging with automatic correlation-ID propagation.
        profiling_service_url: Base URL of the downstream Profiling
            Service, read from ``current_app.config["PROFILING_SERVICE_URL"]``.
    """

    def __init__(self) -> None:
        """Initialise the ProfileService.

        Reads ``PROFILING_SERVICE_URL`` from the active Flask application
        configuration.  Must be called within a Flask application context
        (i.e. during a request or inside ``with app.app_context():``).

        Raises:
            KeyError: If ``PROFILING_SERVICE_URL`` is not set in the
                Flask configuration.
        """
        self.logger = get_logger(__name__)
        self.profiling_service_url: str = current_app.config.get(
            "PROFILING_SERVICE_URL",
            "http://profiling-service:5001",
        )
        self.logger.debug(
            "profile_service_initialized",
            profiling_service_url=self.profiling_service_url,
        )

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def create_profile(
        self,
        tenant_id: str,
        user_id: str,
        source_connection: Dict[str, Any],
        tables: List[str],
    ) -> Dict[str, Any]:
        """Create a new statistical profiling request.

        Validates the incoming ``source_connection`` payload, persists a
        profile request document in MongoDB with status ``'pending'``, and
        asynchronously dispatches the profiling job to the Profiling
        Service via HTTP.

        Only schema **metadata** is extracted — no raw production data is
        accessed (Constraint C-001).

        Args:
            tenant_id: Unique identifier of the requesting tenant (R-007).
            user_id: Identifier of the user initiating the request.
            source_connection: Dictionary containing ERP connection
                details.  Must include:
                - ``erp_type`` (str): One of ``sap``, ``oracle``,
                  ``dynamics``, or ``legacy``.
                - ``connection_params`` (dict): ERP-specific connection
                  parameters (host, port, credentials reference, etc.).
            tables: List of table names to profile in the ERP source
                system.

        Returns:
            The newly created profile request document as a dictionary,
            including the generated ``profile_id`` and initial status.

        Raises:
            ValueError: If ``source_connection`` is missing required
                fields or ``erp_type`` is not supported.
            pymongo.errors.PyMongoError: On MongoDB write failures.
            httpx.HTTPError: On Profiling Service communication failures
                (handled internally with structured logging and circuit
                breaker — the profile record is still created).
        """
        # ----- Input validation -----
        if not source_connection or not isinstance(source_connection, dict):
            self.logger.warning(
                "profile_creation_validation_failed",
                tenant_id=tenant_id,
                reason="source_connection must be a non-empty dictionary",
            )
            raise ValueError("source_connection must be a non-empty dictionary")

        erp_type: str = source_connection.get("erp_type", "")
        if erp_type not in _SUPPORTED_ERP_TYPES:
            self.logger.warning(
                "profile_creation_validation_failed",
                tenant_id=tenant_id,
                erp_type=erp_type,
                reason=f"Unsupported erp_type. Must be one of {_SUPPORTED_ERP_TYPES}",
            )
            raise ValueError(
                f"Unsupported erp_type '{erp_type}'. "
                f"Must be one of {_SUPPORTED_ERP_TYPES}"
            )

        if "connection_params" not in source_connection:
            self.logger.warning(
                "profile_creation_validation_failed",
                tenant_id=tenant_id,
                reason="source_connection must include 'connection_params'",
            )
            raise ValueError("source_connection must include 'connection_params'")

        if not tables or not isinstance(tables, list):
            self.logger.warning(
                "profile_creation_validation_failed",
                tenant_id=tenant_id,
                reason="tables must be a non-empty list",
            )
            raise ValueError("tables must be a non-empty list of table names")

        # ----- Build profile document -----
        profile_id: str = str(uuid.uuid4())
        now: datetime = datetime.now(timezone.utc)

        profile_doc: Dict[str, Any] = {
            "profile_id": profile_id,
            "tenant_id": tenant_id,
            "user_id": user_id,
            "erp_type": erp_type,
            "source_connection": {
                "erp_type": erp_type,
                "connection_params": source_connection["connection_params"],
            },
            "tables": tables,
            "status": "pending",
            "statistics": {},
            "created_at": now,
            "updated_at": now,
        }

        # ----- Persist in MongoDB -----
        try:
            db = get_db()
            db[COLLECTION_NAME].insert_one(profile_doc)
            self.logger.info(
                "profile_created",
                profile_id=profile_id,
                tenant_id=tenant_id,
                erp_type=erp_type,
                table_count=len(tables),
            )
        except Exception as exc:
            self.logger.error(
                "profile_creation_db_error",
                profile_id=profile_id,
                tenant_id=tenant_id,
                error=str(exc),
                error_type=type(exc).__name__,
            )
            raise

        # ----- Dispatch to Profiling Service -----
        try:
            dispatch_response = self._dispatch_to_profiling_service(profile_doc)
            # Update the profile document with the dispatch acknowledgement.
            db[COLLECTION_NAME].update_one(
                {"profile_id": profile_id, "tenant_id": tenant_id},
                {
                    "$set": {
                        "status": "profiling",
                        "dispatch_response": dispatch_response,
                        "updated_at": datetime.now(timezone.utc),
                    }
                },
            )
            profile_doc["status"] = "profiling"
            profile_doc["dispatch_response"] = dispatch_response
            self.logger.info(
                "profile_dispatch_success",
                profile_id=profile_id,
                tenant_id=tenant_id,
            )
        except Exception as exc:
            # Log but do not raise — the profile record exists and can be
            # retried or manually re-dispatched.  The circuit breaker will
            # track the failure for upstream resilience.
            self.logger.error(
                "profile_dispatch_failed",
                profile_id=profile_id,
                tenant_id=tenant_id,
                error=str(exc),
                error_type=type(exc).__name__,
            )
            db[COLLECTION_NAME].update_one(
                {"profile_id": profile_id, "tenant_id": tenant_id},
                {
                    "$set": {
                        "status": "dispatch_failed",
                        "error_message": str(exc),
                        "updated_at": datetime.now(timezone.utc),
                    }
                },
            )
            profile_doc["status"] = "dispatch_failed"
            profile_doc["error_message"] = str(exc)

        # Remove the internal MongoDB ``_id`` field before returning.
        profile_doc.pop("_id", None)
        return profile_doc

    def get_profile(
        self,
        profile_id: str,
        tenant_id: str,
    ) -> Optional[Dict[str, Any]]:
        """Retrieve a statistical profile by ID with cache-aside strategy.

        Checks Redis first (key ``profile:<profile_id>``) for a cached
        copy.  On cache miss, falls back to the ``statistical_profiles``
        MongoDB collection and populates the cache on success.

        Args:
            profile_id: Unique identifier of the profile to retrieve.
            tenant_id: Tenant scope for multi-tenant isolation (R-007).

        Returns:
            The profile document as a dictionary, or ``None`` if no
            matching document is found for the given tenant.

        Raises:
            pymongo.errors.PyMongoError: On MongoDB read failures.
        """
        # ----- Redis cache lookup -----
        try:
            redis_client = get_redis()
            cache_key = f"{_CACHE_KEY_PREFIX}:{profile_id}"
            cached: Optional[bytes] = redis_client.get(cache_key)
            if cached is not None:
                profile_data: Dict[str, Any] = json.loads(cached)
                # Enforce tenant isolation even on cached data.
                if profile_data.get("tenant_id") == tenant_id:
                    self.logger.debug(
                        "profile_cache_hit",
                        profile_id=profile_id,
                        tenant_id=tenant_id,
                    )
                    return profile_data
                # Cache entry belongs to a different tenant — treat as miss.
                self.logger.warning(
                    "profile_cache_tenant_mismatch",
                    profile_id=profile_id,
                    tenant_id=tenant_id,
                )
        except Exception as exc:
            # Redis is non-critical — degrade gracefully to MongoDB.
            self.logger.warning(
                "profile_cache_read_error",
                profile_id=profile_id,
                tenant_id=tenant_id,
                error=str(exc),
                error_type=type(exc).__name__,
            )

        # ----- MongoDB fallback -----
        try:
            db = get_db()
            profile: Optional[Dict[str, Any]] = db[COLLECTION_NAME].find_one(
                {"profile_id": profile_id, "tenant_id": tenant_id},
                {"_id": 0},
            )
            if profile is not None:
                self.logger.debug(
                    "profile_cache_miss_db_hit",
                    profile_id=profile_id,
                    tenant_id=tenant_id,
                )
                self._cache_profile(profile_id, profile)
                return profile

            self.logger.debug(
                "profile_not_found",
                profile_id=profile_id,
                tenant_id=tenant_id,
            )
            return None
        except Exception as exc:
            self.logger.error(
                "profile_get_db_error",
                profile_id=profile_id,
                tenant_id=tenant_id,
                error=str(exc),
                error_type=type(exc).__name__,
            )
            raise

    def list_profiles(
        self,
        tenant_id: str,
        erp_type: Optional[str] = None,
        page: int = 1,
        page_size: int = 20,
    ) -> Dict[str, Any]:
        """List statistical profiles with pagination and optional filtering.

        Queries the ``statistical_profiles`` MongoDB collection with an
        **always-present** ``tenant_id`` filter (R-007) and an optional
        ``erp_type`` filter.  Results are sorted by ``created_at``
        descending and paginated using skip/limit.

        Args:
            tenant_id: Tenant scope for multi-tenant isolation (R-007).
            erp_type: Optional ERP type filter.  One of ``sap``,
                ``oracle``, ``dynamics``, or ``legacy``.
            page: Page number (1-indexed, defaults to 1).
            page_size: Number of items per page (defaults to 20,
                clamped to the range [1, 100]).

        Returns:
            A pagination envelope dictionary with keys:
            - ``items`` (list[dict]): Profile documents for the page.
            - ``total`` (int): Total matching documents.
            - ``page`` (int): Current page number.
            - ``page_size`` (int): Items per page.
            - ``has_next`` (bool): Whether a subsequent page exists.

        Raises:
            pymongo.errors.PyMongoError: On MongoDB read failures.
        """
        # Sanitise pagination params.
        page = max(1, page)
        page_size = max(1, min(page_size, 100))
        skip: int = (page - 1) * page_size

        # Build query filter — tenant_id is mandatory.
        query_filter: Dict[str, Any] = {"tenant_id": tenant_id}
        if erp_type is not None:
            if erp_type in _SUPPORTED_ERP_TYPES:
                query_filter["erp_type"] = erp_type
            else:
                self.logger.warning(
                    "profile_list_unsupported_erp_type",
                    tenant_id=tenant_id,
                    erp_type=erp_type,
                    supported=_SUPPORTED_ERP_TYPES,
                )

        try:
            db = get_db()
            collection = db[COLLECTION_NAME]

            total: int = collection.count_documents(query_filter)
            cursor = (
                collection.find(query_filter, {"_id": 0})
                .sort("created_at", -1)
                .skip(skip)
                .limit(page_size)
            )
            items: List[Dict[str, Any]] = list(cursor)

            has_next: bool = (skip + page_size) < total

            self.logger.debug(
                "profile_list_success",
                tenant_id=tenant_id,
                erp_type=erp_type,
                total=total,
                page=page,
                page_size=page_size,
                returned=len(items),
            )

            return {
                "items": items,
                "total": total,
                "page": page,
                "page_size": page_size,
                "has_next": has_next,
            }
        except Exception as exc:
            self.logger.error(
                "profile_list_db_error",
                tenant_id=tenant_id,
                error=str(exc),
                error_type=type(exc).__name__,
            )
            raise

    def get_profile_statistics(
        self,
        profile_id: str,
        tenant_id: str,
    ) -> Optional[Dict[str, Any]]:
        """Retrieve the full statistical profile including distribution data.

        Returns column-level statistics such as min, max, mean, standard
        deviation, null count, distinct count, and distribution type
        (normal, log-normal, Poisson, categorical) for every profiled
        column.

        Args:
            profile_id: Unique identifier of the profile.
            tenant_id: Tenant scope for multi-tenant isolation (R-007).

        Returns:
            A dictionary containing the statistical summary keyed by
            table and column, or ``None`` if the profile does not exist
            or has not yet completed profiling.

        Raises:
            pymongo.errors.PyMongoError: On MongoDB read failures.
        """
        try:
            db = get_db()
            profile: Optional[Dict[str, Any]] = db[COLLECTION_NAME].find_one(
                {"profile_id": profile_id, "tenant_id": tenant_id},
                {"_id": 0},
            )
            if profile is None:
                self.logger.debug(
                    "profile_statistics_not_found",
                    profile_id=profile_id,
                    tenant_id=tenant_id,
                )
                return None

            # Build the statistics response envelope.
            statistics: Dict[str, Any] = profile.get("statistics", {})
            tables_profiled: List[str] = profile.get("tables", [])

            result: Dict[str, Any] = {
                "profile_id": profile_id,
                "tenant_id": tenant_id,
                "erp_type": profile.get("erp_type", ""),
                "status": profile.get("status", ""),
                "tables": tables_profiled,
                "table_count": len(tables_profiled),
                "statistics": statistics,
                "column_statistics": self._extract_column_statistics(statistics),
                "created_at": profile.get("created_at"),
                "updated_at": profile.get("updated_at"),
            }

            self.logger.debug(
                "profile_statistics_retrieved",
                profile_id=profile_id,
                tenant_id=tenant_id,
                table_count=len(tables_profiled),
            )
            return result
        except Exception as exc:
            self.logger.error(
                "profile_statistics_db_error",
                profile_id=profile_id,
                tenant_id=tenant_id,
                error=str(exc),
                error_type=type(exc).__name__,
            )
            raise

    def delete_profile(
        self,
        profile_id: str,
        tenant_id: str,
    ) -> bool:
        """Delete a statistical profile document.

        Removes the profile from MongoDB and invalidates the
        corresponding Redis cache entry.  The ``tenant_id`` filter
        ensures cross-tenant deletion is impossible (R-007).

        Args:
            profile_id: Unique identifier of the profile to delete.
            tenant_id: Tenant scope for multi-tenant isolation (R-007).

        Returns:
            ``True`` if a document was deleted, ``False`` if no matching
            document was found.

        Raises:
            pymongo.errors.PyMongoError: On MongoDB delete failures.
        """
        try:
            db = get_db()
            result = db[COLLECTION_NAME].delete_one(
                {"profile_id": profile_id, "tenant_id": tenant_id},
            )
            deleted: bool = result.deleted_count > 0

            if deleted:
                self._invalidate_cache(profile_id)
                self.logger.info(
                    "profile_deleted",
                    profile_id=profile_id,
                    tenant_id=tenant_id,
                )
            else:
                self.logger.warning(
                    "profile_delete_not_found",
                    profile_id=profile_id,
                    tenant_id=tenant_id,
                )

            return deleted
        except Exception as exc:
            self.logger.error(
                "profile_delete_db_error",
                profile_id=profile_id,
                tenant_id=tenant_id,
                error=str(exc),
                error_type=type(exc).__name__,
            )
            raise

    # ------------------------------------------------------------------
    # Inter-Service Communication (Private)
    # ------------------------------------------------------------------

    @circuit(failure_threshold=5, recovery_timeout=30)
    def _dispatch_to_profiling_service(
        self,
        profile_request: Dict[str, Any],
    ) -> Dict[str, Any]:
        """Dispatch a profiling request to the Profiling Service.

        Sends an HTTP POST to ``<PROFILING_SERVICE_URL>/api/v1/profile``
        containing the source connection details and table list.  The
        ``tenant_id`` is included in the request headers so that the
        downstream service can enforce its own tenant isolation.

        The ``@circuit`` decorator (from ``circuitbreaker``) opens the
        circuit after **5** consecutive failures and automatically
        retries after **30** seconds, preventing cascade failures.

        Args:
            profile_request: The profile request document containing at
                minimum ``tenant_id``, ``profile_id``,
                ``source_connection``, and ``tables``.

        Returns:
            The JSON response body from the Profiling Service as a
            dictionary.

        Raises:
            httpx.HTTPError: On transport-level or HTTP-status errors.
            circuitbreaker.CircuitBreakerError: When the circuit is open
                (too many recent failures).
        """
        url: str = f"{self.profiling_service_url}/api/v1/profile"
        tenant_id: str = profile_request.get("tenant_id", "")
        profile_id: str = profile_request.get("profile_id", "")

        payload: Dict[str, Any] = {
            "profile_id": profile_id,
            "tenant_id": tenant_id,
            "source_connection": profile_request.get("source_connection", {}),
            "tables": profile_request.get("tables", []),
        }

        headers: Dict[str, str] = {
            "Content-Type": "application/json",
            "X-Tenant-ID": tenant_id,
            "X-Correlation-ID": profile_id,
        }

        self.logger.info(
            "profiling_service_dispatch_started",
            profile_id=profile_id,
            tenant_id=tenant_id,
            url=url,
            table_count=len(payload.get("tables", [])),
        )

        try:
            response = httpx.post(
                url,
                json=payload,
                headers=headers,
                timeout=_DISPATCH_TIMEOUT_SECONDS,
            )
            response.raise_for_status()
            response_data: Dict[str, Any] = response.json()

            self.logger.info(
                "profiling_service_dispatch_completed",
                profile_id=profile_id,
                tenant_id=tenant_id,
                status_code=response.status_code,
            )
            return response_data

        except httpx.TimeoutException as exc:
            self.logger.error(
                "profiling_service_dispatch_timeout",
                profile_id=profile_id,
                tenant_id=tenant_id,
                url=url,
                timeout=_DISPATCH_TIMEOUT_SECONDS,
                error=str(exc),
            )
            raise httpx.HTTPStatusError(
                message=f"Profiling Service request timed out after {_DISPATCH_TIMEOUT_SECONDS}s",
                request=exc.request,
                response=None,  # type: ignore[arg-type]
            ) from exc

        except httpx.HTTPStatusError as exc:
            self.logger.error(
                "profiling_service_dispatch_http_error",
                profile_id=profile_id,
                tenant_id=tenant_id,
                url=url,
                status_code=exc.response.status_code if exc.response else None,
                error=str(exc),
            )
            raise

        except httpx.HTTPError as exc:
            self.logger.error(
                "profiling_service_dispatch_connection_error",
                profile_id=profile_id,
                tenant_id=tenant_id,
                url=url,
                error=str(exc),
                error_type=type(exc).__name__,
            )
            raise

    # ------------------------------------------------------------------
    # Redis Caching (Private)
    # ------------------------------------------------------------------

    def _cache_profile(
        self,
        profile_id: str,
        profile_data: Dict[str, Any],
    ) -> None:
        """Cache a profile document in Redis.

        Serialises the profile dictionary to a JSON string and stores it
        under the key ``profile:<profile_id>`` with a TTL of 3 600
        seconds (1 hour).

        Args:
            profile_id: Unique identifier used as the cache key suffix.
            profile_data: The profile document to cache.
        """
        try:
            redis_client = get_redis()
            cache_key: str = f"{_CACHE_KEY_PREFIX}:{profile_id}"
            serialized: str = json.dumps(
                profile_data,
                default=str,  # Handle datetime serialisation gracefully.
            )
            redis_client.setex(cache_key, _CACHE_TTL_SECONDS, serialized)
            self.logger.debug(
                "profile_cached",
                profile_id=profile_id,
                ttl=_CACHE_TTL_SECONDS,
            )
        except Exception as exc:
            # Cache write failures are non-critical — log and continue.
            self.logger.warning(
                "profile_cache_write_error",
                profile_id=profile_id,
                error=str(exc),
                error_type=type(exc).__name__,
            )

    def _invalidate_cache(self, profile_id: str) -> None:
        """Remove a profile document from the Redis cache.

        Deletes the key ``profile:<profile_id>`` if it exists.  Failures
        are logged but not raised since cache invalidation is best-effort.

        Args:
            profile_id: Unique identifier of the profile to evict.
        """
        try:
            redis_client = get_redis()
            cache_key: str = f"{_CACHE_KEY_PREFIX}:{profile_id}"
            redis_client.delete(cache_key)
            self.logger.debug(
                "profile_cache_invalidated",
                profile_id=profile_id,
            )
        except Exception as exc:
            self.logger.warning(
                "profile_cache_invalidate_error",
                profile_id=profile_id,
                error=str(exc),
                error_type=type(exc).__name__,
            )

    # ------------------------------------------------------------------
    # Internal Helpers (Private)
    # ------------------------------------------------------------------

    @staticmethod
    def _extract_column_statistics(
        statistics: Dict[str, Any],
    ) -> List[Dict[str, Any]]:
        """Flatten nested statistics into a column-level summary list.

        Iterates over per-table statistics and extracts column-level
        metrics (min, max, mean, std_dev, null_count, distinct_count,
        distribution_type) into a flat list suitable for API responses.

        Args:
            statistics: The raw ``statistics`` subdocument from a
                profile record, expected to be keyed by table name with
                each value containing a ``columns`` mapping.

        Returns:
            A list of dictionaries, each representing one column's
            statistical summary.
        """
        column_stats: List[Dict[str, Any]] = []

        if not statistics or not isinstance(statistics, dict):
            return column_stats

        for table_name, table_stats in statistics.items():
            if not isinstance(table_stats, dict):
                continue
            columns: Dict[str, Any] = table_stats.get("columns", {})
            if not isinstance(columns, dict):
                continue

            for column_name, col_data in columns.items():
                if not isinstance(col_data, dict):
                    continue
                column_stats.append(
                    {
                        "table_name": table_name,
                        "column_name": column_name,
                        "min": col_data.get("min"),
                        "max": col_data.get("max"),
                        "mean": col_data.get("mean"),
                        "std_dev": col_data.get("std_dev"),
                        "null_count": col_data.get("null_count", 0),
                        "distinct_count": col_data.get("distinct_count", 0),
                        "distribution_type": col_data.get(
                            "distribution_type", "unknown"
                        ),
                    }
                )

        return column_stats
