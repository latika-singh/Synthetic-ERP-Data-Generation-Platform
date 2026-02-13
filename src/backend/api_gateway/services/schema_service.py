"""Business logic service for ERP schema discovery orchestration.

This module provides the :class:`SchemaService` class, which serves as the business
logic layer between API Gateway route handlers and the downstream Profiling Service
for all schema-related operations.  It orchestrates ERP schema discovery, stores
results in the MongoDB ``schema_definitions`` collection, and provides methods to
browse schemas, tables, columns, and relationships.

Key Responsibilities:
    - Validate and dispatch schema discovery requests to the Profiling Service
    - Store and retrieve schema definitions from MongoDB with Redis caching
    - Enforce multi-tenant isolation on all queries (R-007)
    - Enforce ERP module scope (C-005): only four modules in initial release
    - Ensure no raw production data access — metadata only (C-001)
    - Apply circuit breaker pattern on inter-service HTTP calls

Architecture:
    Route handlers (``api_gateway.routes.schemas``) call ``SchemaService`` methods,
    which in turn communicate with:
        - **MongoDB** (via ``get_db()``) for persistent schema definitions
        - **Redis** (via ``get_redis()``) for caching frequently accessed schemas
        - **Profiling Service** (via ``httpx``) for ERP connector invocations

Usage::

    from api_gateway.services.schema_service import SchemaService

    service = SchemaService()
    result = service.discover_schema(
        tenant_id="tenant_abc",
        user_id="user_123",
        erp_type="sap",
        connection_config={"host": "erp.example.com", "port": 3300},
        modules=["financial_accounting", "human_resources"],
    )
"""

from __future__ import annotations

import json
import uuid
from datetime import UTC, datetime
from typing import Any

import httpx
from circuitbreaker import circuit
from flask import current_app

from api_gateway.extensions import get_db, get_redis
from shared.logging.structured_logger import get_logger


# ---------------------------------------------------------------------------
# Module-level constants
# ---------------------------------------------------------------------------

COLLECTION_NAME: str = "schema_definitions"
"""MongoDB collection name for persisting discovered ERP schema definitions."""

SUPPORTED_ERP_TYPES: list[str] = [
    "sap",
    "oracle_ebs",
    "dynamics",
    "legacy",
]
"""ERP system types supported by the platform's Profiling Service connectors.

Each value maps to a dedicated connector in the Profiling Service and must
align with the ``ERPType`` string enum defined in
``api_gateway.schemas.schema``:
    - ``sap`` → SAP RFC/BAPI schema discovery connector
    - ``oracle_ebs`` → Oracle E-Business Suite OData/JDBC connector
    - ``dynamics`` → Microsoft Dynamics 365 Web API/OData connector
    - ``legacy`` → Generic JDBC connector for legacy systems
"""

SUPPORTED_ERP_MODULES: list[str] = [
    "financial_accounting",
    "hr",
    "sales_distribution",
    "material_management",
]
"""ERP modules available in the initial release per Constraint C-005.

Values must align with the ``ERPModule`` string enum defined in
``api_gateway.schemas.schema``.  Only these four modules are permitted for
schema discovery.  Additional modules (Production Planning, Plant Maintenance,
Quality Management) are deferred to future releases.
"""

# ---------------------------------------------------------------------------
# Internal constants for Redis caching configuration
# ---------------------------------------------------------------------------

_CACHE_KEY_PREFIX: str = "schema"
_CACHE_TTL_SECONDS: int = 7200  # 2 hours — schemas change infrequently


class SchemaService:
    """Business logic service for ERP schema discovery orchestration.

    Acts as the intermediary between API Gateway route handlers and the Profiling
    Service, encapsulating all schema discovery, retrieval, browsing, and deletion
    logic.  Every public method enforces multi-tenant isolation by requiring a
    ``tenant_id`` parameter that is applied as a filter on all MongoDB queries
    (R-007).

    The service communicates with the Profiling Service via synchronous HTTP
    (``httpx``) with circuit breaker protection to prevent cascade failures.
    Discovered schemas are persisted in the MongoDB ``schema_definitions``
    collection and cached in Redis for fast retrieval.

    Attributes:
        logger: Pre-configured structlog ``BoundLogger`` for structured JSON
            logging with correlation ID propagation.
        profiling_service_url: Base URL of the Profiling Service read from
            Flask application configuration.

    Example::

        service = SchemaService()
        schemas = service.list_schemas(tenant_id="tenant_abc", erp_type="sap")
    """

    def __init__(self) -> None:
        """Initialise the SchemaService.

        Reads the ``PROFILING_SERVICE_URL`` from the Flask application
        configuration (``current_app.config``).  Must be called within an
        active Flask application context.

        Raises:
            KeyError: If ``PROFILING_SERVICE_URL`` is not set in the Flask
                application configuration.
        """
        self.logger = get_logger(__name__)
        self.profiling_service_url: str = current_app.config["PROFILING_SERVICE_URL"]

    # ------------------------------------------------------------------
    # Public API — Schema Discovery
    # ------------------------------------------------------------------

    def discover_schema(
        self,
        tenant_id: str,
        user_id: str,
        erp_type: str,
        connection_config: dict[str, Any],
        modules: list[str],
    ) -> dict:
        """Initiate an ERP schema discovery request.

        Validates the ERP type and requested modules, creates a discovery
        record in MongoDB, and dispatches the actual discovery to the Profiling
        Service.  The Profiling Service connects to the source ERP system and
        extracts table/column metadata, relationships, and data types **without
        accessing raw production data** (Constraint C-001).

        Args:
            tenant_id: Identifier of the requesting tenant (R-007 isolation).
            user_id: Identifier of the user who initiated the discovery.
            erp_type: Type of ERP system to discover.  Must be one of
                :data:`SUPPORTED_ERP_TYPES`.
            connection_config: ERP connection parameters (host, port, credentials
                reference, etc.).  Passed to the Profiling Service as-is.
                No raw production data is included — only connection metadata.
            modules: List of ERP modules to discover.  Each must be in
                :data:`SUPPORTED_ERP_MODULES` (Constraint C-005).

        Returns:
            A dict representing the newly created discovery request, including:
            ``schema_id``, ``status`` (``'discovering'``), ``erp_type``,
            ``modules``, ``created_at``, and ``tenant_id``.

        Raises:
            ValueError: If ``erp_type`` is not in :data:`SUPPORTED_ERP_TYPES`,
                any module in ``modules`` is not in :data:`SUPPORTED_ERP_MODULES`,
                or ``modules`` is empty.
            httpx.HTTPError: If the Profiling Service is unreachable (after
                circuit breaker evaluation).
            Exception: For unexpected MongoDB write failures.
        """
        self.logger.info(
            "schema_discovery_initiated",
            tenant_id=tenant_id,
            user_id=user_id,
            erp_type=erp_type,
            modules=modules,
        )

        # --- Validate ERP type ---
        if erp_type not in SUPPORTED_ERP_TYPES:
            self.logger.warning(
                "schema_discovery_invalid_erp_type",
                tenant_id=tenant_id,
                erp_type=erp_type,
                supported_types=SUPPORTED_ERP_TYPES,
            )
            raise ValueError(
                f"Unsupported ERP type '{erp_type}'. "
                f"Supported types: {SUPPORTED_ERP_TYPES}"
            )

        # --- Validate modules (C-005 enforcement) ---
        invalid_modules: list[str] = [
            m for m in modules if m not in SUPPORTED_ERP_MODULES
        ]
        if invalid_modules:
            self.logger.warning(
                "schema_discovery_invalid_modules",
                tenant_id=tenant_id,
                invalid_modules=invalid_modules,
                supported_modules=SUPPORTED_ERP_MODULES,
            )
            raise ValueError(
                f"Unsupported ERP modules: {invalid_modules}. "
                f"Supported modules: {SUPPORTED_ERP_MODULES}"
            )

        if not modules:
            self.logger.warning(
                "schema_discovery_empty_modules",
                tenant_id=tenant_id,
            )
            raise ValueError("At least one ERP module must be specified.")

        # --- Create discovery request record in MongoDB ---
        schema_id: str = str(uuid.uuid4())
        now: datetime = datetime.now(UTC)

        discovery_record: dict[str, Any] = {
            "schema_id": schema_id,
            "tenant_id": tenant_id,
            "user_id": user_id,
            "erp_type": erp_type,
            "modules": modules,
            "connection_config": connection_config,
            "status": "discovering",
            "tables": [],
            "relationships": [],
            "metadata": {},
            "created_at": now,
            "updated_at": now,
        }

        try:
            db = get_db()
            db[COLLECTION_NAME].insert_one(discovery_record)
            self.logger.info(
                "schema_discovery_record_created",
                schema_id=schema_id,
                tenant_id=tenant_id,
            )
        except Exception as exc:
            self.logger.error(
                "schema_discovery_record_creation_failed",
                schema_id=schema_id,
                tenant_id=tenant_id,
                error=str(exc),
                error_type=type(exc).__name__,
            )
            raise

        # --- Dispatch to Profiling Service (metadata only — C-001) ---
        try:
            profiling_response: dict = self._dispatch_discovery(discovery_record)

            # Update the record with any immediate response data from the
            # Profiling Service (e.g. an upstream job_id or updated status).
            update_fields: dict[str, Any] = {
                "updated_at": datetime.now(UTC),
            }
            if isinstance(profiling_response, dict):
                profiling_job_id = profiling_response.get("job_id")
                if profiling_job_id is not None:
                    update_fields["profiling_job_id"] = profiling_job_id
                profiling_status = profiling_response.get("status")
                if profiling_status:
                    update_fields["status"] = profiling_status

            db[COLLECTION_NAME].update_one(
                {"schema_id": schema_id, "tenant_id": tenant_id},
                {"$set": update_fields},
            )
            self.logger.info(
                "schema_discovery_dispatched",
                schema_id=schema_id,
                tenant_id=tenant_id,
            )
        except Exception as exc:
            # Mark discovery as failed but preserve the record for auditability
            db[COLLECTION_NAME].update_one(
                {"schema_id": schema_id, "tenant_id": tenant_id},
                {
                    "$set": {
                        "status": "failed",
                        "error_message": str(exc),
                        "updated_at": datetime.now(UTC),
                    }
                },
            )
            self.logger.error(
                "schema_discovery_dispatch_failed",
                schema_id=schema_id,
                tenant_id=tenant_id,
                error=str(exc),
                error_type=type(exc).__name__,
            )
            raise

        # Return a sanitised response (exclude MongoDB _id and raw
        # connection_config to avoid leaking credentials).
        result: dict[str, Any] = {
            "schema_id": schema_id,
            "tenant_id": tenant_id,
            "erp_type": erp_type,
            "modules": modules,
            "status": update_fields.get("status", "discovering"),
            "created_at": now.isoformat(),
        }
        return result

    # ------------------------------------------------------------------
    # Public API — Schema Retrieval
    # ------------------------------------------------------------------

    def get_schema(self, schema_id: str, tenant_id: str) -> dict | None:
        """Retrieve a schema definition by ID with tenant isolation.

        Checks Redis cache first for fast retrieval.  On cache miss, queries
        MongoDB and populates the cache for subsequent requests.

        Args:
            schema_id: Unique identifier of the schema definition.
            tenant_id: Identifier of the requesting tenant (R-007 isolation).

        Returns:
            The schema definition document (including ``tables``, ``columns``,
            ``relationships``, and ``data_types``) or ``None`` if the schema
            is not found or does not belong to the given tenant.
        """
        # --- Check Redis cache first ---
        cache_key: str = f"{_CACHE_KEY_PREFIX}:{schema_id}"
        try:
            redis_client = get_redis()
            cached: bytes | None = redis_client.get(cache_key)
            if cached is not None:
                schema_data: dict = json.loads(cached)
                # Verify tenant ownership even on cached data
                if schema_data.get("tenant_id") == tenant_id:
                    self.logger.debug(
                        "schema_cache_hit",
                        schema_id=schema_id,
                        tenant_id=tenant_id,
                    )
                    return schema_data
                # Tenant mismatch in cache — fall through to database query
                self.logger.warning(
                    "schema_cache_tenant_mismatch",
                    schema_id=schema_id,
                    tenant_id=tenant_id,
                )
        except Exception as exc:
            # Redis unavailable — log and fall through to MongoDB
            self.logger.warning(
                "schema_cache_read_error",
                schema_id=schema_id,
                error=str(exc),
                error_type=type(exc).__name__,
            )

        # --- Fall back to MongoDB ---
        try:
            db = get_db()
            schema: dict | None = db[COLLECTION_NAME].find_one(
                {"schema_id": schema_id, "tenant_id": tenant_id},
                {"_id": 0},  # Exclude MongoDB internal _id
            )
            if schema is None:
                self.logger.debug(
                    "schema_not_found",
                    schema_id=schema_id,
                    tenant_id=tenant_id,
                )
                return None

            # Convert datetime objects for JSON serialisability
            schema_serializable: dict = self._serialize_document(schema)

            # Populate cache for future requests
            self._cache_schema(schema_id, schema_serializable)

            self.logger.debug(
                "schema_cache_miss_db_hit",
                schema_id=schema_id,
                tenant_id=tenant_id,
            )
            return schema_serializable
        except Exception as exc:
            self.logger.error(
                "schema_retrieval_failed",
                schema_id=schema_id,
                tenant_id=tenant_id,
                error=str(exc),
                error_type=type(exc).__name__,
            )
            raise

    def list_schemas(
        self,
        tenant_id: str,
        erp_type: str | None = None,
        module: str | None = None,
        page: int = 1,
        page_size: int = 20,
    ) -> dict:
        """List schema definitions with tenant-scoped pagination and optional filters.

        Always filters by ``tenant_id`` (R-007).  Supports optional filtering by
        ERP type and module, and returns results sorted by ``created_at``
        descending with skip/limit pagination.

        Args:
            tenant_id: Identifier of the requesting tenant (R-007 isolation).
            erp_type: Optional ERP type filter (e.g. ``'sap'``).
            module: Optional module filter (e.g. ``'financial_accounting'``).
                Matches schemas whose ``modules`` array contains this value.
            page: Page number (1-based).  Defaults to ``1``.
            page_size: Number of items per page.  Defaults to ``20``.
                Clamped to the range [1, 100].

        Returns:
            A dict with keys:
                - ``items``: List of schema definition documents.
                - ``total``: Total number of matching schemas.
                - ``page``: Current page number.
                - ``page_size``: Items per page.
                - ``has_next``: Boolean indicating whether more pages exist.
        """
        # Sanitise pagination parameters
        page = max(1, page)
        page_size = max(1, min(page_size, 100))

        # Build query filter — tenant_id is always mandatory (R-007)
        query_filter: dict[str, Any] = {"tenant_id": tenant_id}
        if erp_type is not None:
            query_filter["erp_type"] = erp_type
        if module is not None:
            # Match schemas whose modules array contains the requested module
            query_filter["modules"] = module

        try:
            db = get_db()
            collection = db[COLLECTION_NAME]

            total: int = collection.count_documents(query_filter)
            skip: int = (page - 1) * page_size

            cursor = (
                collection.find(query_filter, {"_id": 0})
                .sort("created_at", -1)
                .skip(skip)
                .limit(page_size)
            )
            items: list[dict] = [self._serialize_document(doc) for doc in cursor]

            has_next: bool = (skip + page_size) < total

            self.logger.debug(
                "schema_list_retrieved",
                tenant_id=tenant_id,
                erp_type=erp_type,
                module=module,
                page=page,
                page_size=page_size,
                total=total,
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
                "schema_list_failed",
                tenant_id=tenant_id,
                error=str(exc),
                error_type=type(exc).__name__,
            )
            raise

    # ------------------------------------------------------------------
    # Public API — Table & Column Browsing
    # ------------------------------------------------------------------

    def get_schema_tables(
        self,
        schema_id: str,
        tenant_id: str,
    ) -> list[dict] | None:
        """Retrieve the list of tables from a schema definition.

        Each table entry includes ``table_name``, ``columns`` (list of column
        dicts), ``primary_key``, and ``record_count_estimate``.

        Args:
            schema_id: Unique identifier of the schema definition.
            tenant_id: Identifier of the requesting tenant (R-007 isolation).

        Returns:
            A list of table dicts, or ``None`` if the schema is not found or
            does not belong to the given tenant.
        """
        try:
            db = get_db()
            schema: dict | None = db[COLLECTION_NAME].find_one(
                {"schema_id": schema_id, "tenant_id": tenant_id},
                {"_id": 0, "tables": 1},
            )
            if schema is None:
                self.logger.debug(
                    "schema_tables_not_found",
                    schema_id=schema_id,
                    tenant_id=tenant_id,
                )
                return None

            tables: list[dict] = schema.get("tables", [])
            self.logger.debug(
                "schema_tables_retrieved",
                schema_id=schema_id,
                tenant_id=tenant_id,
                table_count=len(tables),
            )
            return tables
        except Exception as exc:
            self.logger.error(
                "schema_tables_retrieval_failed",
                schema_id=schema_id,
                tenant_id=tenant_id,
                error=str(exc),
                error_type=type(exc).__name__,
            )
            raise

    def get_table_details(
        self,
        schema_id: str,
        tenant_id: str,
        table_name: str,
    ) -> dict | None:
        """Retrieve detailed column information for a specific table.

        Returns full column-level metadata including ``column_name``,
        ``data_type``, ``nullable``, ``max_length``, ``precision``, and
        ``foreign_keys`` for the requested table within the schema.

        Args:
            schema_id: Unique identifier of the schema definition.
            tenant_id: Identifier of the requesting tenant (R-007 isolation).
            table_name: Name of the table to retrieve details for.

        Returns:
            A dict with the table's full metadata (including its columns list),
            or ``None`` if the schema or table is not found.
        """
        try:
            db = get_db()
            schema: dict | None = db[COLLECTION_NAME].find_one(
                {"schema_id": schema_id, "tenant_id": tenant_id},
                {"_id": 0, "tables": 1},
            )
            if schema is None:
                self.logger.debug(
                    "schema_table_details_schema_not_found",
                    schema_id=schema_id,
                    tenant_id=tenant_id,
                    table_name=table_name,
                )
                return None

            # Search for the specific table in the tables array
            tables: list[dict] = schema.get("tables", [])
            for table in tables:
                if table.get("table_name") == table_name:
                    self.logger.debug(
                        "schema_table_details_retrieved",
                        schema_id=schema_id,
                        tenant_id=tenant_id,
                        table_name=table_name,
                        column_count=len(table.get("columns", [])),
                    )
                    return table

            self.logger.debug(
                "schema_table_details_table_not_found",
                schema_id=schema_id,
                tenant_id=tenant_id,
                table_name=table_name,
            )
            return None
        except Exception as exc:
            self.logger.error(
                "schema_table_details_retrieval_failed",
                schema_id=schema_id,
                tenant_id=tenant_id,
                table_name=table_name,
                error=str(exc),
                error_type=type(exc).__name__,
            )
            raise

    # ------------------------------------------------------------------
    # Public API — Relationships
    # ------------------------------------------------------------------

    def get_relationships(
        self,
        schema_id: str,
        tenant_id: str,
    ) -> list[dict] | None:
        """Retrieve foreign key relationships from a schema definition.

        Each relationship dict includes ``source_table``, ``source_column``,
        ``target_table``, ``target_column``, and ``relationship_type``.
        This data is consumed by the Generation Engine to enforce referential
        integrity during synthetic data generation.

        Args:
            schema_id: Unique identifier of the schema definition.
            tenant_id: Identifier of the requesting tenant (R-007 isolation).

        Returns:
            A list of relationship dicts, or ``None`` if the schema is not
            found or does not belong to the given tenant.
        """
        try:
            db = get_db()
            schema: dict | None = db[COLLECTION_NAME].find_one(
                {"schema_id": schema_id, "tenant_id": tenant_id},
                {"_id": 0, "relationships": 1},
            )
            if schema is None:
                self.logger.debug(
                    "schema_relationships_not_found",
                    schema_id=schema_id,
                    tenant_id=tenant_id,
                )
                return None

            relationships: list[dict] = schema.get("relationships", [])
            self.logger.debug(
                "schema_relationships_retrieved",
                schema_id=schema_id,
                tenant_id=tenant_id,
                relationship_count=len(relationships),
            )
            return relationships
        except Exception as exc:
            self.logger.error(
                "schema_relationships_retrieval_failed",
                schema_id=schema_id,
                tenant_id=tenant_id,
                error=str(exc),
                error_type=type(exc).__name__,
            )
            raise

    # ------------------------------------------------------------------
    # Public API — Schema Deletion
    # ------------------------------------------------------------------

    def delete_schema(self, schema_id: str, tenant_id: str) -> bool:
        """Delete a schema definition and invalidate its cache.

        Removes the schema document from MongoDB (enforcing tenant isolation)
        and clears any cached version from Redis.

        Args:
            schema_id: Unique identifier of the schema definition to delete.
            tenant_id: Identifier of the requesting tenant (R-007 isolation).

        Returns:
            ``True`` if the schema was successfully deleted, ``False`` if no
            matching schema was found for the given tenant.
        """
        try:
            db = get_db()
            result = db[COLLECTION_NAME].delete_one(
                {"schema_id": schema_id, "tenant_id": tenant_id},
            )

            if result.deleted_count == 0:
                self.logger.warning(
                    "schema_delete_not_found",
                    schema_id=schema_id,
                    tenant_id=tenant_id,
                )
                return False

            # Invalidate the Redis cache entry for this schema
            self._invalidate_cache(schema_id)

            self.logger.info(
                "schema_deleted",
                schema_id=schema_id,
                tenant_id=tenant_id,
                deleted_at=datetime.now(UTC).isoformat(),
            )
            return True
        except Exception as exc:
            self.logger.error(
                "schema_deletion_failed",
                schema_id=schema_id,
                tenant_id=tenant_id,
                error=str(exc),
                error_type=type(exc).__name__,
            )
            raise

    # ------------------------------------------------------------------
    # Private — Profiling Service Communication
    # ------------------------------------------------------------------

    @circuit(failure_threshold=5, recovery_timeout=30)
    def _dispatch_discovery(self, discovery_request: dict[str, Any]) -> dict:
        """Dispatch a schema discovery request to the Profiling Service.

        Sends a POST request to the Profiling Service's ``/api/v1/discover``
        endpoint with the ERP connection configuration, type, and requested
        modules.  Protected by a circuit breaker (5 failures open the circuit,
        30-second recovery timeout) to prevent cascade failures when the
        Profiling Service is unavailable.

        Schema discovery against ERP systems can be slow (database introspection
        over JDBC/OData), hence the extended timeout of 120 seconds.

        Note:
            Only metadata flows through this call — no raw production data
            is accessed or transferred (Constraint C-001).

        Args:
            discovery_request: The full discovery record dict containing at
                minimum ``schema_id``, ``tenant_id``, ``erp_type``,
                ``connection_config``, and ``modules``.

        Returns:
            The JSON response body from the Profiling Service as a dict.

        Raises:
            httpx.HTTPStatusError: If the Profiling Service returns a non-2xx
                HTTP status code.
            httpx.HTTPError: If a connection-level error occurs (timeout,
                network unreachable, DNS failure, etc.).
            circuitbreaker.CircuitBreakerError: If the circuit is open due
                to repeated Profiling Service failures.
        """
        url: str = f"{self.profiling_service_url}/api/v1/discover"

        payload: dict[str, Any] = {
            "schema_id": discovery_request["schema_id"],
            "erp_type": discovery_request["erp_type"],
            "connection_config": discovery_request["connection_config"],
            "modules": discovery_request["modules"],
        }

        headers: dict[str, str] = {
            "Content-Type": "application/json",
            "X-Tenant-ID": discovery_request["tenant_id"],
            "X-Correlation-ID": str(uuid.uuid4()),
        }

        self.logger.info(
            "profiling_service_dispatch_started",
            schema_id=discovery_request["schema_id"],
            tenant_id=discovery_request["tenant_id"],
            erp_type=discovery_request["erp_type"],
            url=url,
        )

        try:
            response = httpx.post(
                url,
                json=payload,
                headers=headers,
                timeout=120.0,
            )
            response.raise_for_status()

            response_data: dict = response.json()
            self.logger.info(
                "profiling_service_dispatch_completed",
                schema_id=discovery_request["schema_id"],
                tenant_id=discovery_request["tenant_id"],
                status_code=response.status_code,
            )
            return response_data
        except httpx.HTTPStatusError as exc:
            self.logger.error(
                "profiling_service_http_error",
                schema_id=discovery_request["schema_id"],
                tenant_id=discovery_request["tenant_id"],
                status_code=exc.response.status_code,
                error=str(exc),
            )
            raise
        except httpx.HTTPError as exc:
            self.logger.error(
                "profiling_service_connection_error",
                schema_id=discovery_request["schema_id"],
                tenant_id=discovery_request["tenant_id"],
                error=str(exc),
                error_type=type(exc).__name__,
            )
            raise

    # ------------------------------------------------------------------
    # Private — Redis Caching
    # ------------------------------------------------------------------

    def _cache_schema(self, schema_id: str, schema_data: dict) -> None:
        """Cache a schema definition in Redis.

        Stores the schema as a JSON string with a TTL of 7200 seconds
        (2 hours).  Schemas change infrequently after initial discovery,
        making them excellent candidates for caching.

        Args:
            schema_id: Unique identifier used to build the cache key
                (``schema:<schema_id>``).
            schema_data: The schema definition dict to cache.  Must be
                JSON-serialisable (datetime objects are converted via the
                ``default=str`` fallback).
        """
        cache_key: str = f"{_CACHE_KEY_PREFIX}:{schema_id}"
        try:
            redis_client = get_redis()
            redis_client.setex(
                cache_key,
                _CACHE_TTL_SECONDS,
                json.dumps(schema_data, default=str),
            )
            self.logger.debug(
                "schema_cached",
                schema_id=schema_id,
                ttl_seconds=_CACHE_TTL_SECONDS,
            )
        except Exception as exc:
            # Cache write failure is non-critical — log and continue.
            # The service remains functional via direct MongoDB queries.
            self.logger.warning(
                "schema_cache_write_error",
                schema_id=schema_id,
                error=str(exc),
                error_type=type(exc).__name__,
            )

    def _invalidate_cache(self, schema_id: str) -> None:
        """Remove a schema definition from the Redis cache.

        Called after schema deletion to ensure stale data is not served
        from cache.

        Args:
            schema_id: Unique identifier used to build the cache key to delete
                (``schema:<schema_id>``).
        """
        cache_key: str = f"{_CACHE_KEY_PREFIX}:{schema_id}"
        try:
            redis_client = get_redis()
            redis_client.delete(cache_key)
            self.logger.debug(
                "schema_cache_invalidated",
                schema_id=schema_id,
            )
        except Exception as exc:
            # Cache invalidation failure is non-critical — the entry will
            # expire naturally after the TTL period.
            self.logger.warning(
                "schema_cache_invalidation_error",
                schema_id=schema_id,
                error=str(exc),
                error_type=type(exc).__name__,
            )

    # ------------------------------------------------------------------
    # Private — Utilities
    # ------------------------------------------------------------------

    @staticmethod
    def _serialize_document(doc: dict[str, Any]) -> dict[str, Any]:
        """Convert a MongoDB document to a JSON-serialisable dict.

        Handles ``datetime`` objects by converting them to ISO 8601 strings
        and strips the MongoDB internal ``_id`` field if present.  Recursively
        processes nested dicts and lists.

        Args:
            doc: The raw MongoDB document dict.

        Returns:
            A new dict with all values converted to JSON-serialisable types.
        """
        result: dict[str, Any] = {}
        for key, value in doc.items():
            if key == "_id":
                # Strip MongoDB internal identifier
                continue
            if isinstance(value, datetime):
                result[key] = value.isoformat()
            elif isinstance(value, list):
                result[key] = [
                    SchemaService._serialize_document(item)
                    if isinstance(item, dict)
                    else (
                        item.isoformat() if isinstance(item, datetime) else item
                    )
                    for item in value
                ]
            elif isinstance(value, dict):
                result[key] = SchemaService._serialize_document(value)
            else:
                result[key] = value
        return result
