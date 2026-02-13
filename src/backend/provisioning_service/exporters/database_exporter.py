"""Direct database provisioning module for the Provisioning Service.

Manages the complete lifecycle of provisioning generated synthetic data directly
to target relational databases (PostgreSQL 12-16, Oracle 19c-23ai, SQL Server
2019-2022, SAP HANA 2.0 SPS 07+) via JDBC batch inserts.  This module is the
primary orchestrator that coordinates with the ``connectors`` package to execute
database-specific DDL and DML operations.

Key Capabilities:
    - **Batch provisioning** — Configurable batch sizes (default 10K records,
      tunable 1K-100K) with per-batch transaction commit/rollback semantics.
    - **Schema creation** — Automatic ``CREATE TABLE`` generation from schema
      definitions with topological ordering for foreign-key dependencies.
    - **Progress tracking** — Real-time provisioning progress reported via Redis
      ``HSET`` operations, enabling the Web Console to display live progress bars
      with records provisioned, percentage complete, and current table name.
    - **Multi-tenant isolation** — Database-schema-level namespace separation
      using ``tenant_{tenant_id}`` naming convention to guarantee cross-tenant
      data isolation at the storage layer.
    - **Resilience** — Circuit-breaker pattern for external database calls with
      configurable failure thresholds and recovery timeouts, combined with retry
      logic using exponential backoff for transient errors (deadlocks, timeouts).
    - **Verification** — Post-provisioning row-count verification to confirm
      data integrity between generated counts and actual database counts.
    - **Connection pooling** — Active connector reuse across provisioning jobs
      to avoid repeated JDBC handshake overhead.

Error Handling Strategy:
    Three configurable modes via ``options.on_error``:

    - ``"abort"`` (default) — Raise the error immediately, halting provisioning.
    - ``"skip"`` — Log a warning and skip the failed batch, continuing with
      subsequent batches.
    - ``"log"`` — Record the error in the error list, continue processing, and
      include all errors in the final result.

Usage::

    from provisioning_service.config import ProvisioningServiceConfig
    from provisioning_service.exporters.database_exporter import DatabaseExporter

    config = ProvisioningServiceConfig()

    with DatabaseExporter(config) as exporter:
        result = exporter.provision(
            data=data_generator,
            target_config={
                "db_type": "postgresql",
                "host": "localhost",
                "port": 5432,
                "database": "erp_target",
                "username": "admin",
                "password": "secret",
            },
            schema=schema_definition,
            job_id="job-abc-123",
            tenant_id="tenant-001",
        )

See Also:
    :mod:`provisioning_service.connectors` for the database connector registry
    and :class:`~provisioning_service.connectors.base.BaseConnector` for the
    abstract interface all connectors implement.
"""

from __future__ import annotations

import json
import time
from typing import Any, Dict, Generator, List, Optional, Tuple

from shared.database.redis_client import get_redis_client
from shared.logging.structured_logger import get_logger
from shared.middleware.circuit_breaker import CircuitBreakerError, create_circuit_breaker
from provisioning_service.config import ProvisioningServiceConfig
from provisioning_service.connectors import (
    BaseConnector,
    get_connector,
    get_supported_databases,
)


# ---------------------------------------------------------------------------
# Module-level constants
# ---------------------------------------------------------------------------

_PROGRESS_KEY_PREFIX: str = "provisioning:progress:"
"""Redis key prefix for provisioning progress hash maps."""

_PROGRESS_TTL_SECONDS: int = 3600
"""Time-to-live for progress keys in Redis (1 hour)."""

_MIN_BATCH_SIZE: int = 1000
"""Minimum allowed batch size (1K records)."""

_MAX_BATCH_SIZE: int = 100000
"""Maximum allowed batch size (100K records)."""

_DEFAULT_MAX_RETRIES: int = 3
"""Default number of retry attempts for transient database errors."""

_REQUIRED_TARGET_FIELDS: Tuple[str, ...] = (
    "db_type",
    "host",
    "port",
    "database",
    "username",
    "password",
)
"""Fields required in every target_config dictionary."""

_TENANT_SCHEMA_PREFIX: str = "tenant_"
"""Prefix for tenant-scoped database schema names."""


class DatabaseExporter:
    """Direct database provisioning exporter for synthetic ERP data.

    Orchestrates the complete lifecycle of provisioning generated synthetic
    data to target relational databases.  Coordinates with database-specific
    connectors from the ``provisioning_service.connectors`` package for DDL
    (schema/table creation) and DML (batch insert) operations.

    The exporter supports four target database platforms:

    * **PostgreSQL** (12 – 16)
    * **Oracle** (19c – 23ai)
    * **SQL Server** (2019 – 2022)
    * **SAP HANA** (2.0 SPS 07+)

    All operations are instrumented with structured JSON logging, circuit
    breaker protection, and real-time progress tracking via Redis.

    Args:
        config: :class:`ProvisioningServiceConfig` instance providing batch
            size, timeout, and connection pool settings loaded from
            environment variables.

    Example::

        config = ProvisioningServiceConfig()
        with DatabaseExporter(config) as exporter:
            result = exporter.provision(
                data=record_generator,
                target_config=target_cfg,
                schema=schema_def,
                job_id="job-123",
                tenant_id="t-001",
            )
    """

    # ------------------------------------------------------------------
    # Initialisation
    # ------------------------------------------------------------------

    def __init__(self, config: ProvisioningServiceConfig) -> None:
        """Initialise the DatabaseExporter with service configuration.

        Reads batch sizes, timeouts, and retry parameters from the provided
        configuration.  Initialises the structured logger, Redis client for
        progress tracking, an empty connector cache, and a circuit breaker
        protecting external database calls.

        Args:
            config: Service-level configuration providing ``EXPORT_BATCH_SIZE``,
                ``JDBC_CONNECTION_TIMEOUT``, ``JDBC_QUERY_TIMEOUT``, and other
                environment-driven settings.
        """
        self._config: ProvisioningServiceConfig = config
        self._logger = get_logger("database_exporter")

        # Redis client for real-time progress tracking.
        self._redis_client = get_redis_client()

        # Batch and timeout configuration from environment variables.
        raw_batch_size: int = getattr(config, "EXPORT_BATCH_SIZE", 10000)
        self._batch_size: int = max(
            _MIN_BATCH_SIZE, min(raw_batch_size, _MAX_BATCH_SIZE)
        )
        self._connection_timeout: int = getattr(
            config, "JDBC_CONNECTION_TIMEOUT", 30
        )
        self._query_timeout: int = getattr(config, "JDBC_QUERY_TIMEOUT", 300)

        # Retry configuration for transient database errors.
        self._max_retries: int = _DEFAULT_MAX_RETRIES

        # Active connector cache for connection reuse across operations.
        self._active_connectors: Dict[str, BaseConnector] = {}

        # Circuit breaker protecting external database calls.
        self._circuit_breaker = create_circuit_breaker(
            name="database_exporter",
            failure_threshold=3,
            recovery_timeout=30,
        )

        # Internal state for tracking provisioned row counts per table.
        self._table_row_counts: Dict[str, int] = {}

        self._logger.info(
            "database_exporter_initialised",
            batch_size=self._batch_size,
            connection_timeout=self._connection_timeout,
            query_timeout=self._query_timeout,
            max_retries=self._max_retries,
        )

    # ------------------------------------------------------------------
    # Public Methods
    # ------------------------------------------------------------------

    def provision(
        self,
        data: Generator,
        target_config: Dict[str, Any],
        schema: Dict[str, Any],
        job_id: str,
        tenant_id: str,
        options: Optional[Dict[str, Any]] = None,
    ) -> Dict[str, Any]:
        """Provision synthetic data to a target relational database.

        This is the primary entry point for direct database provisioning.
        It validates the target configuration, establishes a connection via
        the appropriate JDBC connector, creates the necessary database
        schema and tables, inserts data in configurable batches, verifies
        row counts, and returns a comprehensive result summary.

        Args:
            data: A generator yielding data records.  Each yielded item
                should be a dictionary mapping table names to lists of row
                tuples, or a flat list of row tuples when provisioning a
                single table.
            target_config: Connection configuration dictionary containing
                at minimum: ``db_type``, ``host``, ``port``, ``database``,
                ``username``, ``password``.  Additional keys (e.g.,
                ``schema``, ``ssl``) are forwarded to the connector.
            schema: Schema definition dictionary describing tables, columns,
                data types, constraints, and foreign-key relationships.
                Expected structure::

                    {
                        "tables": [
                            {
                                "name": "gl_entries",
                                "columns": [
                                    {"name": "id", "type": "BIGINT", ...},
                                    ...
                                ],
                                "foreign_keys": [...]
                            },
                            ...
                        ]
                    }

            job_id: Unique generation job identifier for progress tracking.
            tenant_id: Tenant identifier for multi-tenant schema isolation.
            options: Optional provisioning options dictionary.  Supported
                keys:

                * ``drop_existing`` (bool) — Drop tables before creation.
                * ``truncate_existing`` (bool) — Truncate tables before insert.
                * ``on_error`` (str) — Error strategy: ``"abort"`` (default),
                  ``"skip"``, or ``"log"``.
                * ``batch_size`` (int) — Override the default batch size.

        Returns:
            A result summary dictionary::

                {
                    "db_type": "postgresql",
                    "host": "db.example.com",
                    "database": "erp_target",
                    "tables_created": ["gl_entries", "invoices"],
                    "records_provisioned": 50000,
                    "duration_seconds": 12.34,
                    "batches_processed": 5,
                    "errors": [],
                    "verification": {...},
                }

        Raises:
            ValueError: If ``target_config`` is missing required fields or
                contains an unsupported ``db_type``.
            ConnectionError: If the target database is unreachable.
            CircuitBreakerError: If the circuit breaker is in the OPEN state.
            RuntimeError: If provisioning fails fatally and ``on_error``
                is ``"abort"``.
        """
        options = options or {}
        start_time: float = time.time()
        errors: List[Dict[str, Any]] = []

        self._logger.info(
            "provisioning_started",
            job_id=job_id,
            tenant_id=tenant_id,
            db_type=target_config.get("db_type"),
            host=target_config.get("host"),
            database=target_config.get("database"),
        )

        # Step 1: Validate target configuration.
        self._validate_target_config(target_config)

        db_type: str = target_config["db_type"]
        host: str = target_config["host"]
        database: str = target_config["database"]

        # Step 2: Get or create the database connector.
        connector: BaseConnector = self._get_connector(db_type, target_config)

        try:
            # Step 3: Ensure the connector is connected.
            self._circuit_breaker.call(connector.ensure_connected)

            # Step 4: Set up tenant-scoped schema for namespace isolation.
            self._setup_tenant_schema(connector, tenant_id)

            # Update progress: initialising.
            self._update_progress(
                job_id=job_id,
                records_provisioned=0,
                status="initialising",
            )

            # Step 5: Create tables from schema definition.
            tables_created: List[str] = self._create_tables(
                connector, schema, options
            )

            # Step 6: Insert data in batches.
            batch_result: Dict[str, Any] = self._batch_provision(
                connector, data, schema, job_id, options
            )
            errors.extend(batch_result.get("errors", []))

            # Step 7: Verify provisioned data counts.
            verification: Dict[str, Any] = self._verify_provisioning(
                connector, schema
            )

            duration_seconds: float = round(time.time() - start_time, 3)

            # Step 8: Update final progress in Redis.
            total_records: int = batch_result.get("total_records", 0)
            self._update_progress(
                job_id=job_id,
                records_provisioned=total_records,
                total_records=total_records,
                status="completed",
            )

            # Store verification results and error summary as JSON in Redis
            # for downstream consumers to retrieve.
            try:
                final_key: str = f"{_PROGRESS_KEY_PREFIX}{job_id}"
                self._redis_client.hset(
                    final_key,
                    "verification",
                    json.dumps(verification),
                )
                if errors:
                    self._redis_client.hset(
                        final_key,
                        "errors",
                        json.dumps(errors),
                    )
            except Exception as redis_exc:
                self._logger.warning(
                    "final_progress_update_failed",
                    job_id=job_id,
                    error=str(redis_exc),
                )

            result: Dict[str, Any] = {
                "db_type": db_type,
                "host": host,
                "database": database,
                "tables_created": tables_created,
                "records_provisioned": total_records,
                "duration_seconds": duration_seconds,
                "batches_processed": batch_result.get("batches_processed", 0),
                "errors": errors,
                "verification": verification,
                "skipped_records": batch_result.get("skipped_records", 0),
                "tables_provisioned": batch_result.get(
                    "tables_provisioned", []
                ),
            }

            self._logger.info(
                "provisioning_completed",
                job_id=job_id,
                tenant_id=tenant_id,
                records_provisioned=total_records,
                duration_seconds=duration_seconds,
                tables_created=len(tables_created),
                batches_processed=result["batches_processed"],
                error_count=len(errors),
            )

            return result

        except CircuitBreakerError:
            duration_seconds = round(time.time() - start_time, 3)
            self._update_progress(
                job_id=job_id,
                records_provisioned=0,
                status="failed",
            )
            self._logger.error(
                "provisioning_circuit_breaker_open",
                job_id=job_id,
                tenant_id=tenant_id,
                db_type=db_type,
            )
            raise

        except Exception as exc:
            duration_seconds = round(time.time() - start_time, 3)
            self._update_progress(
                job_id=job_id,
                records_provisioned=0,
                status="failed",
            )
            self._logger.error(
                "provisioning_failed",
                job_id=job_id,
                tenant_id=tenant_id,
                db_type=db_type,
                error=str(exc),
                error_type=type(exc).__name__,
                duration_seconds=duration_seconds,
            )
            raise

    def disconnect_all(self) -> None:
        """Disconnect all active database connectors and release resources.

        Iterates through the active connector cache, calling ``disconnect()``
        on each connector.  Errors during individual disconnections are logged
        but do not prevent other connectors from being closed.

        After this method completes the active connector cache is empty.
        """
        connector_count: int = len(self._active_connectors)
        if connector_count == 0:
            self._logger.debug("disconnect_all_no_active_connectors")
            return

        self._logger.info(
            "disconnecting_all_connectors",
            connector_count=connector_count,
        )

        for key, connector in list(self._active_connectors.items()):
            try:
                connector.disconnect()
                self._logger.debug(
                    "connector_disconnected",
                    connector_key=key,
                )
            except Exception as exc:
                self._logger.warning(
                    "connector_disconnect_error",
                    connector_key=key,
                    error=str(exc),
                    error_type=type(exc).__name__,
                )

        self._active_connectors.clear()
        self._table_row_counts.clear()

        self._logger.info(
            "all_connectors_disconnected",
            connectors_closed=connector_count,
        )

    def health_check(self, target_config: Dict[str, Any]) -> Dict[str, Any]:
        """Check the health of a target database.

        Creates a temporary connector for the specified database, executes
        the connector's health check probe, and returns the result.  The
        temporary connector is disconnected after the check regardless of
        the outcome.

        Args:
            target_config: Connection configuration dictionary containing
                ``db_type``, ``host``, ``port``, ``database``, ``username``,
                and ``password``.

        Returns:
            Health status dictionary from the connector's
            :meth:`~BaseConnector.health_check` method, enriched with
            ``db_type`` and error information if the check failed.
        """
        db_type: str = target_config.get("db_type", "unknown")
        connector: Optional[BaseConnector] = None

        try:
            connector = get_connector(db_type, target_config)
            connector.ensure_connected()
            health: Dict[str, Any] = connector.health_check()
            health["db_type"] = db_type
            return health

        except Exception as exc:
            self._logger.warning(
                "health_check_failed",
                db_type=db_type,
                host=target_config.get("host"),
                error=str(exc),
            )
            return {
                "status": "unhealthy",
                "db_type": db_type,
                "error": str(exc),
                "error_type": type(exc).__name__,
            }

        finally:
            if connector is not None:
                try:
                    connector.disconnect()
                except Exception as disc_exc:
                    self._logger.debug(
                        "health_check_disconnect_error",
                        error=str(disc_exc),
                    )

    def get_supported_databases(self) -> List[str]:
        """Return the list of supported target database types.

        Delegates to :func:`provisioning_service.connectors.get_supported_databases`
        to retrieve all registered database type identifiers including aliases.

        Returns:
            Sorted list of supported database type strings (e.g.,
            ``["hana", "mssql", "oracle", "postgresql", ...]``).
        """
        return get_supported_databases()

    def get_provisioning_progress(self, job_id: str) -> Dict[str, Any]:
        """Retrieve the current provisioning progress for a job from Redis.

        Reads the progress hash stored in Redis by :meth:`_update_progress`
        and deserialises the structured ``details`` field via
        :func:`json.loads`.

        Args:
            job_id: Unique generation job identifier.

        Returns:
            Progress dictionary with ``records_provisioned``, ``percentage``,
            ``status``, ``current_table``, and other tracking fields.
            Returns an empty dictionary if no progress record exists.
        """
        progress_key: str = f"{_PROGRESS_KEY_PREFIX}{job_id}"

        try:
            raw_data: Dict[str, str] = self._redis_client.hgetall(
                progress_key
            )
            if not raw_data:
                return {}

            # Deserialise string values back to native types.
            progress: Dict[str, Any] = {
                "job_id": raw_data.get("job_id", job_id),
                "records_provisioned": int(
                    raw_data.get("records_provisioned", "0")
                ),
                "total_records": int(raw_data.get("total_records", "0")),
                "percentage": float(raw_data.get("percentage", "0.0")),
                "status": raw_data.get("status", "unknown"),
                "current_table": raw_data.get("current_table", ""),
                "updated_at": float(raw_data.get("updated_at", "0")),
            }

            # Deserialise the structured details field using json.loads.
            details_raw: str = raw_data.get("details", "{}")
            progress["details"] = json.loads(details_raw)

            return progress

        except Exception as exc:
            self._logger.warning(
                "progress_retrieval_failed",
                job_id=job_id,
                error=str(exc),
            )
            return {}

    # ------------------------------------------------------------------
    # Context Manager Protocol
    # ------------------------------------------------------------------

    def __enter__(self) -> "DatabaseExporter":
        """Enter the runtime context.

        Returns:
            The ``DatabaseExporter`` instance for use in a ``with`` block.
        """
        return self

    def __exit__(
        self,
        exc_type: Optional[type],
        exc_val: Optional[BaseException],
        exc_tb: Any,
    ) -> None:
        """Exit the runtime context — disconnect all active connectors.

        If an exception occurred inside the ``with`` block it is logged at
        the error level before cleanup.  The exception is **not** suppressed
        and will continue to propagate after all connectors are closed.

        Args:
            exc_type: Exception class if an exception was raised, else None.
            exc_val: Exception instance if raised, else None.
            exc_tb: Traceback object if an exception was raised, else None.
        """
        if exc_val is not None:
            self._logger.error(
                "database_exporter_context_error",
                error=str(exc_val),
                error_type=type(exc_val).__name__ if exc_type else "Unknown",
            )

        self.disconnect_all()

    # ------------------------------------------------------------------
    # Private Methods
    # ------------------------------------------------------------------

    def _validate_target_config(self, target_config: Dict[str, Any]) -> None:
        """Validate that the target configuration contains all required fields.

        Args:
            target_config: Connection configuration dictionary to validate.

        Raises:
            ValueError: If any required field is missing from the config.
        """
        missing: List[str] = [
            field
            for field in _REQUIRED_TARGET_FIELDS
            if field not in target_config
            or target_config[field] is None
            or (isinstance(target_config[field], str) and not target_config[field].strip())
        ]

        if missing:
            raise ValueError(
                f"target_config is missing required fields: {', '.join(missing)}. "
                f"Required fields: {', '.join(_REQUIRED_TARGET_FIELDS)}"
            )

    def _get_connector(
        self, db_type: str, config: Dict[str, Any]
    ) -> BaseConnector:
        """Get or create a database connector, with connection reuse.

        Checks the active connector cache first.  If no cached connector
        exists for the given database type and host/database combination,
        a new one is created via the connector factory and stored in the
        cache.

        Args:
            db_type: Target database type string (e.g., ``"postgresql"``).
            config: Connection configuration dictionary.

        Returns:
            A :class:`~provisioning_service.connectors.base.BaseConnector`
            instance ready for connection.

        Raises:
            ValueError: If *db_type* is not supported by the connector
                registry.
        """
        cache_key: str = (
            f"{db_type}:{config.get('host', '')}:"
            f"{config.get('port', '')}:{config.get('database', '')}"
        )

        if cache_key in self._active_connectors:
            self._logger.debug(
                "reusing_cached_connector",
                connector_key=cache_key,
                db_type=db_type,
            )
            return self._active_connectors[cache_key]

        try:
            connector: BaseConnector = get_connector(db_type, config)
            self._active_connectors[cache_key] = connector
            self._logger.info(
                "connector_created",
                connector_key=cache_key,
                db_type=db_type,
                host=config.get("host"),
                database=config.get("database"),
            )
            return connector

        except ValueError as exc:
            self._logger.error(
                "unsupported_database_type",
                db_type=db_type,
                error=str(exc),
                supported=get_supported_databases(),
            )
            raise

    def _setup_tenant_schema(
        self, connector: BaseConnector, tenant_id: str
    ) -> None:
        """Create and activate a tenant-scoped database schema.

        Establishes namespace isolation by creating a database schema named
        ``tenant_{tenant_id}`` (sanitised) and setting it as the active
        search path / current schema for all subsequent operations on the
        connector.

        Args:
            connector: An active database connector.
            tenant_id: Tenant identifier used to derive the schema name.
        """
        if not tenant_id:
            self._logger.debug("skip_tenant_schema_no_tenant_id")
            return

        # Sanitise tenant_id to prevent SQL injection in schema names.
        sanitised_tenant_id: str = "".join(
            c if c.isalnum() or c == "_" else "_" for c in tenant_id
        )
        schema_name: str = f"{_TENANT_SCHEMA_PREFIX}{sanitised_tenant_id}"

        self._logger.info(
            "setting_up_tenant_schema",
            tenant_id=tenant_id,
            schema_name=schema_name,
        )

        try:
            # Create the tenant schema if it does not exist.
            create_schema_sql: str = (
                f"CREATE SCHEMA IF NOT EXISTS {schema_name}"
            )
            self._circuit_breaker.call(
                connector.execute_query, create_schema_sql
            )

            # Set the search path / current schema.
            set_schema_sql: str = f"SET search_path TO {schema_name}, public"
            self._circuit_breaker.call(
                connector.execute_query, set_schema_sql
            )

            self._logger.info(
                "tenant_schema_ready",
                tenant_id=tenant_id,
                schema_name=schema_name,
            )

        except CircuitBreakerError:
            self._logger.error(
                "tenant_schema_circuit_breaker_open",
                tenant_id=tenant_id,
                schema_name=schema_name,
            )
            raise

        except Exception as exc:
            self._logger.warning(
                "tenant_schema_setup_error",
                tenant_id=tenant_id,
                schema_name=schema_name,
                error=str(exc),
                error_type=type(exc).__name__,
            )
            # Non-fatal: some databases may not support schema creation via
            # simple SQL or may require different syntax.  Log and continue.

    def _create_tables(
        self,
        connector: BaseConnector,
        schema: Dict[str, Any],
        options: Optional[Dict[str, Any]] = None,
    ) -> List[str]:
        """Create tables in the target database from the schema definition.

        Tables are created in topological (dependency) order so that tables
        referenced by foreign keys are created before the tables that
        reference them.

        Args:
            connector: An active database connector.
            schema: Schema definition dictionary with a ``tables`` key
                containing a list of table definitions.
            options: Optional dictionary with provisioning options:

                * ``drop_existing`` (bool) — Drop existing tables before
                  creation (default ``False``).
                * ``truncate_existing`` (bool) — Truncate existing tables
                  before inserting data (default ``False``).

        Returns:
            Ordered list of table names that were created or prepared.
        """
        options = options or {}
        tables: List[Dict[str, Any]] = schema.get("tables", [])
        if not tables:
            self._logger.warning("no_tables_in_schema")
            return []

        # Resolve creation order based on foreign-key dependencies.
        ordered_table_names: List[str] = self._resolve_table_order(schema)
        created_tables: List[str] = []
        drop_existing: bool = options.get("drop_existing", False)
        truncate_existing: bool = options.get("truncate_existing", False)

        # Build a lookup from table name to table definition.
        table_map: Dict[str, Dict[str, Any]] = {
            t["name"]: t for t in tables if "name" in t
        }

        for table_name in ordered_table_names:
            table_def: Optional[Dict[str, Any]] = table_map.get(table_name)
            if table_def is None:
                self._logger.warning(
                    "table_definition_not_found",
                    table_name=table_name,
                )
                continue

            columns: List[Dict[str, Any]] = table_def.get("columns", [])
            if not columns:
                self._logger.warning(
                    "table_has_no_columns",
                    table_name=table_name,
                )
                continue

            try:
                # Optionally drop existing table.
                if drop_existing:
                    drop_sql: str = f"DROP TABLE IF EXISTS {table_name} CASCADE"
                    self._circuit_breaker.call(
                        connector.execute_query, drop_sql
                    )
                    self._logger.info(
                        "table_dropped",
                        table_name=table_name,
                    )

                # Create the table.
                self._circuit_breaker.call(
                    connector.create_table,
                    table_name,
                    columns,
                    True,  # if_not_exists
                )

                # Optionally truncate existing data.
                if truncate_existing and not drop_existing:
                    truncate_sql: str = f"TRUNCATE TABLE {table_name}"
                    self._circuit_breaker.call(
                        connector.execute_query, truncate_sql
                    )
                    self._logger.info(
                        "table_truncated",
                        table_name=table_name,
                    )

                created_tables.append(table_name)
                self._logger.info(
                    "table_created",
                    table_name=table_name,
                    column_count=len(columns),
                )

            except CircuitBreakerError:
                self._logger.error(
                    "table_creation_circuit_breaker_open",
                    table_name=table_name,
                )
                raise

            except Exception as exc:
                self._logger.error(
                    "table_creation_failed",
                    table_name=table_name,
                    error=str(exc),
                    error_type=type(exc).__name__,
                )
                raise RuntimeError(
                    f"Failed to create table '{table_name}': {exc}"
                ) from exc

        self._logger.info(
            "all_tables_created",
            table_count=len(created_tables),
            tables=created_tables,
        )
        return created_tables

    def _batch_provision(
        self,
        connector: BaseConnector,
        data: Generator,
        schema: Dict[str, Any],
        job_id: str,
        options: Optional[Dict[str, Any]] = None,
    ) -> Dict[str, Any]:
        """Insert data into target tables using batched transactions.

        Consumes records from the data generator, organises them by table,
        buffers them into batches of ``batch_size`` rows, and inserts each
        batch via the connector's ``batch_insert`` method.  Progress is
        reported to Redis after every batch.

        Args:
            connector: An active database connector.
            data: Generator yielding data records.  Each yielded item may
                be:

                * A ``dict`` mapping table names to lists of row tuples.
                * A ``tuple`` representing a single row (when provisioning
                  a single table only — the first table in schema).

            schema: Schema definition dictionary with table and column info.
            job_id: Job identifier for Redis progress tracking.
            options: Optional dictionary with:

                * ``on_error`` (str) — ``"abort"`` | ``"skip"`` | ``"log"``.
                * ``batch_size`` (int) — Override default batch size.

        Returns:
            Summary dictionary::

                {
                    "total_records": 50000,
                    "batches_processed": 5,
                    "tables_provisioned": ["gl_entries", "invoices"],
                    "errors": [...],
                    "skipped_records": 0,
                }
        """
        options = options or {}
        on_error: str = options.get("on_error", "abort")
        effective_batch_size: int = options.get("batch_size", self._batch_size)
        effective_batch_size = max(
            _MIN_BATCH_SIZE, min(effective_batch_size, _MAX_BATCH_SIZE)
        )

        ordered_table_names: List[str] = self._resolve_table_order(schema)
        tables: List[Dict[str, Any]] = schema.get("tables", [])
        table_map: Dict[str, Dict[str, Any]] = {
            t["name"]: t for t in tables if "name" in t
        }

        total_records: int = 0
        batches_processed: int = 0
        tables_provisioned: List[str] = []
        errors: List[Dict[str, Any]] = []
        skipped_records: int = 0

        # Consume the generator and organise data by table.
        table_data: Dict[str, List[tuple]] = self._organise_data_by_table(
            data, ordered_table_names
        )

        for table_name in ordered_table_names:
            table_def: Optional[Dict[str, Any]] = table_map.get(table_name)
            if table_def is None:
                continue

            column_defs: List[Dict[str, Any]] = table_def.get("columns", [])
            column_names: List[str] = [
                col["name"] for col in column_defs if "name" in col
            ]

            rows: List[tuple] = table_data.get(table_name, [])
            if not rows:
                self._logger.debug(
                    "no_data_for_table",
                    table_name=table_name,
                )
                continue

            self._logger.info(
                "batch_provision_table_start",
                table_name=table_name,
                total_rows=len(rows),
                batch_size=effective_batch_size,
            )

            # Update progress with current table context.
            self._update_progress(
                job_id=job_id,
                records_provisioned=total_records,
                status="provisioning",
                current_table=table_name,
            )

            # Process rows in batches.
            table_rows_inserted: int = 0
            batch_number: int = 0

            for batch_start in range(0, len(rows), effective_batch_size):
                batch_end: int = min(
                    batch_start + effective_batch_size, len(rows)
                )
                batch_data: List[tuple] = rows[batch_start:batch_end]
                batch_number += 1

                # Retry loop for transient errors (deadlocks, timeouts).
                batch_succeeded: bool = False
                last_error: Optional[Exception] = None

                for attempt in range(1, self._max_retries + 1):
                    try:
                        inserted: int = self._circuit_breaker.call(
                            connector.batch_insert,
                            table_name,
                            column_names,
                            batch_data,
                            effective_batch_size,
                        )
                        table_rows_inserted += inserted
                        total_records += inserted
                        batches_processed += 1
                        batch_succeeded = True

                        self._logger.debug(
                            "batch_inserted",
                            table_name=table_name,
                            batch_number=batch_number,
                            attempt=attempt,
                            rows_in_batch=len(batch_data),
                            rows_inserted=inserted,
                            total_records=total_records,
                        )

                        # Report progress after each batch.
                        self._update_progress(
                            job_id=job_id,
                            records_provisioned=total_records,
                            status="provisioning",
                            current_table=table_name,
                        )
                        break  # Success — exit retry loop.

                    except CircuitBreakerError:
                        self._logger.error(
                            "batch_insert_circuit_breaker_open",
                            table_name=table_name,
                            batch_number=batch_number,
                            attempt=attempt,
                        )
                        raise

                    except Exception as exc:
                        last_error = exc
                        is_transient: bool = self._is_transient_error(exc)

                        if is_transient and attempt < self._max_retries:
                            # Exponential backoff: 2^(attempt-1) seconds.
                            backoff_seconds: float = float(
                                2 ** (attempt - 1)
                            )
                            self._logger.warning(
                                "batch_insert_transient_error_retry",
                                table_name=table_name,
                                batch_number=batch_number,
                                attempt=attempt,
                                max_retries=self._max_retries,
                                backoff_seconds=backoff_seconds,
                                error_type=type(exc).__name__,
                                error=str(exc),
                            )
                            time.sleep(backoff_seconds)
                            continue  # Retry the batch.

                        # Non-transient error, or final retry attempt.
                        break  # Exit retry loop — handle below.

                # If all retries exhausted or non-transient error, handle it.
                if not batch_succeeded and last_error is not None:
                    error_result: Optional[bool] = self._handle_batch_error(
                        error=last_error,
                        table_name=table_name,
                        batch_number=batch_number,
                        on_error=on_error,
                    )

                    error_entry: Dict[str, Any] = {
                        "table": table_name,
                        "batch_number": batch_number,
                        "error": str(last_error),
                        "error_type": type(last_error).__name__,
                        "rows_in_batch": len(batch_data),
                        "retries_attempted": self._max_retries,
                    }
                    errors.append(error_entry)

                    if error_result is True:
                        # Skip or log mode — continue processing.
                        skipped_records += len(batch_data)
                        continue
                    # abort mode — error was re-raised by _handle_batch_error.

            # Track per-table counts for verification.
            self._table_row_counts[table_name] = table_rows_inserted
            tables_provisioned.append(table_name)

            self._logger.info(
                "batch_provision_table_complete",
                table_name=table_name,
                rows_inserted=table_rows_inserted,
                batches=batch_number,
            )

        return {
            "total_records": total_records,
            "batches_processed": batches_processed,
            "tables_provisioned": tables_provisioned,
            "errors": errors,
            "skipped_records": skipped_records,
        }

    def _verify_provisioning(
        self, connector: BaseConnector, schema: Dict[str, Any]
    ) -> Dict[str, Any]:
        """Verify that provisioned row counts match expected counts.

        Executes ``SELECT COUNT(*) FROM <table>`` for each table in the
        schema and compares the actual count against the tracked expected
        count from the batch provisioning phase.

        Args:
            connector: An active database connector.
            schema: Schema definition dictionary with table info.

        Returns:
            Verification dictionary mapping table names to status dicts::

                {
                    "gl_entries": {
                        "expected": 10000,
                        "actual": 10000,
                        "match": True,
                    },
                    ...
                }
        """
        tables: List[Dict[str, Any]] = schema.get("tables", [])
        verification: Dict[str, Any] = {}

        for table_def in tables:
            table_name: str = table_def.get("name", "")
            if not table_name:
                continue

            expected: int = self._table_row_counts.get(table_name, 0)

            try:
                count_sql: str = f"SELECT COUNT(*) AS row_count FROM {table_name}"
                result: List[Dict[str, Any]] = self._circuit_breaker.call(
                    connector.execute_query, count_sql
                )

                actual: int = 0
                if result and len(result) > 0:
                    # Handle different possible result formats.
                    row: Dict[str, Any] = result[0]
                    actual = int(
                        row.get("row_count", row.get("count", row.get("COUNT(*)", 0)))
                    )

                match: bool = actual >= expected
                verification[table_name] = {
                    "expected": expected,
                    "actual": actual,
                    "match": match,
                }

                if not match:
                    self._logger.warning(
                        "verification_count_mismatch",
                        table_name=table_name,
                        expected=expected,
                        actual=actual,
                    )
                else:
                    self._logger.debug(
                        "verification_count_ok",
                        table_name=table_name,
                        expected=expected,
                        actual=actual,
                    )

            except CircuitBreakerError:
                self._logger.error(
                    "verification_circuit_breaker_open",
                    table_name=table_name,
                )
                verification[table_name] = {
                    "expected": expected,
                    "actual": -1,
                    "match": False,
                    "error": "circuit_breaker_open",
                }

            except Exception as exc:
                self._logger.warning(
                    "verification_query_failed",
                    table_name=table_name,
                    error=str(exc),
                )
                verification[table_name] = {
                    "expected": expected,
                    "actual": -1,
                    "match": False,
                    "error": str(exc),
                }

        return verification

    def _resolve_table_order(self, schema: Dict[str, Any]) -> List[str]:
        """Determine the correct table creation and insertion order.

        Performs a topological sort on the table dependency graph defined
        by foreign-key relationships.  Tables without foreign-key
        dependencies are placed first, followed by tables that depend on
        them, recursively.

        Args:
            schema: Schema definition dictionary with a ``tables`` key.

        Returns:
            Ordered list of table names suitable for sequential creation
            and data insertion.

        Raises:
            ValueError: If the dependency graph contains circular
                dependencies that cannot be resolved.
        """
        tables: List[Dict[str, Any]] = schema.get("tables", [])
        if not tables:
            return []

        # Build adjacency list: table_name -> set of tables it depends on.
        all_table_names: List[str] = [
            t["name"] for t in tables if "name" in t
        ]
        dependencies: Dict[str, set] = {name: set() for name in all_table_names}

        for table_def in tables:
            table_name: str = table_def.get("name", "")
            if not table_name:
                continue

            foreign_keys: List[Dict[str, Any]] = table_def.get(
                "foreign_keys", []
            )
            for fk in foreign_keys:
                ref_table: str = fk.get(
                    "references_table",
                    fk.get("reference_table", fk.get("ref_table", "")),
                )
                if ref_table and ref_table in dependencies:
                    dependencies[table_name].add(ref_table)

        # Kahn's algorithm for topological sort.
        in_degree: Dict[str, int] = {
            name: len(deps) for name, deps in dependencies.items()
        }
        queue: List[str] = [
            name for name, degree in in_degree.items() if degree == 0
        ]
        ordered: List[str] = []

        while queue:
            # Sort for deterministic ordering among tables with same in-degree.
            queue.sort()
            current: str = queue.pop(0)
            ordered.append(current)

            for name, deps in dependencies.items():
                if current in deps:
                    deps.discard(current)
                    in_degree[name] -= 1
                    if in_degree[name] == 0:
                        queue.append(name)

        if len(ordered) != len(all_table_names):
            unresolved: List[str] = [
                name for name in all_table_names if name not in ordered
            ]
            self._logger.error(
                "circular_dependency_detected",
                unresolved_tables=unresolved,
            )
            raise ValueError(
                f"Circular foreign-key dependency detected among tables: "
                f"{', '.join(unresolved)}. Cannot determine safe insertion "
                f"order."
            )

        self._logger.debug(
            "table_order_resolved",
            order=ordered,
        )
        return ordered

    def _organise_data_by_table(
        self,
        data: Generator,
        table_names: List[str],
    ) -> Dict[str, List[tuple]]:
        """Consume a data generator and organise records by table name.

        Supports two generator output formats:

        1. **Multi-table** — Each yielded item is a ``dict`` mapping table
           names to lists of row tuples.
        2. **Single-table** — Each yielded item is a raw ``tuple``
           representing one row, assigned to the first table in the schema.

        Args:
            data: Generator yielding data records.
            table_names: Ordered list of table names from the schema.

        Returns:
            Dictionary mapping table names to lists of row tuples.
        """
        result: Dict[str, List[tuple]] = {name: [] for name in table_names}
        default_table: str = table_names[0] if table_names else ""

        for item in data:
            if isinstance(item, dict):
                # Multi-table format: {table_name: [rows]}.
                for tbl_name, rows in item.items():
                    if tbl_name in result:
                        if isinstance(rows, list):
                            result[tbl_name].extend(
                                row if isinstance(row, tuple) else tuple(row)
                                for row in rows
                            )
                        elif isinstance(rows, tuple):
                            result[tbl_name].append(rows)
            elif isinstance(item, (tuple, list)):
                # Single-table format: one row tuple.
                if default_table:
                    result[default_table].append(
                        item if isinstance(item, tuple) else tuple(item)
                    )

        total_rows: int = sum(len(rows) for rows in result.values())
        self._logger.info(
            "data_organised_by_table",
            total_rows=total_rows,
            tables_with_data=[
                name for name, rows in result.items() if rows
            ],
        )
        return result

    def _update_progress(
        self,
        job_id: str,
        records_provisioned: int,
        total_records: Optional[int] = None,
        status: str = "provisioning",
        current_table: Optional[str] = None,
    ) -> None:
        """Update provisioning progress in Redis for real-time monitoring.

        Stores a structured hash in Redis keyed by
        ``provisioning:progress:{job_id}`` with fields for records
        provisioned, percentage, current table, and status.  The key
        is set to expire after 1 hour.

        Args:
            job_id: Unique generation job identifier.
            records_provisioned: Number of records provisioned so far.
            total_records: Total expected records (if known).
            status: Current provisioning status string.
            current_table: Name of the table currently being populated.
        """
        percentage: float = 0.0
        if total_records and total_records > 0:
            percentage = round(
                (records_provisioned / total_records) * 100.0, 2
            )

        progress_key: str = f"{_PROGRESS_KEY_PREFIX}{job_id}"

        # Use json.dumps for structured serialisation of complex fields
        # and consistent typing in Redis hash values.
        progress_data: Dict[str, str] = {
            "job_id": job_id,
            "records_provisioned": str(records_provisioned),
            "total_records": str(total_records) if total_records else "0",
            "percentage": str(percentage),
            "status": status,
            "current_table": current_table or "",
            "updated_at": str(time.time()),
            "details": json.dumps({
                "records_provisioned": records_provisioned,
                "total_records": total_records or 0,
                "percentage": percentage,
                "status": status,
                "current_table": current_table or "",
            }),
        }

        try:
            self._redis_client.hset(progress_key, mapping=progress_data)
            self._redis_client.expire(progress_key, _PROGRESS_TTL_SECONDS)
        except Exception as exc:
            # Progress tracking failure is non-fatal; log and continue.
            self._logger.warning(
                "progress_update_failed",
                job_id=job_id,
                error=str(exc),
            )

    def _handle_batch_error(
        self,
        error: Exception,
        table_name: str,
        batch_number: int,
        on_error: str = "abort",
    ) -> Optional[bool]:
        """Handle errors that occur during batch insert processing.

        Implements the configurable error strategy:

        * ``"abort"`` — Re-raises the error immediately.
        * ``"skip"`` — Logs a warning and returns ``True`` to signal the
          caller to skip the failed batch and continue.
        * ``"log"`` — Logs the error at error level and returns ``True``
          to continue processing.

        For transient errors (deadlocks, connection timeouts) the method
        attempts up to ``max_retries`` retries with exponential backoff
        before applying the configured error strategy.

        Args:
            error: The exception that occurred.
            table_name: Name of the table being provisioned.
            batch_number: Sequential batch number that failed.
            on_error: Error handling strategy (``"abort"``, ``"skip"``,
                or ``"log"``).

        Returns:
            ``True`` if the caller should continue processing (skip/log
            mode), ``None`` if the error is fatal.  In ``"abort"`` mode
            the error is re-raised and this method does not return.

        Raises:
            Exception: Re-raises the original error when ``on_error``
                is ``"abort"``.
        """
        error_type: str = type(error).__name__
        error_msg: str = str(error)

        # Detect transient errors that may benefit from retry.
        is_transient: bool = self._is_transient_error(error)

        if is_transient:
            self._logger.warning(
                "transient_batch_error_detected",
                table_name=table_name,
                batch_number=batch_number,
                error_type=error_type,
                error=error_msg,
                will_retry=True,
            )

        if on_error == "abort":
            self._logger.error(
                "batch_error_abort",
                table_name=table_name,
                batch_number=batch_number,
                error_type=error_type,
                error=error_msg,
            )
            raise error

        elif on_error == "skip":
            self._logger.warning(
                "batch_error_skip",
                table_name=table_name,
                batch_number=batch_number,
                error_type=error_type,
                error=error_msg,
            )
            return True

        elif on_error == "log":
            self._logger.error(
                "batch_error_logged",
                table_name=table_name,
                batch_number=batch_number,
                error_type=error_type,
                error=error_msg,
            )
            return True

        # Unknown strategy — treat as abort.
        self._logger.error(
            "batch_error_unknown_strategy",
            table_name=table_name,
            batch_number=batch_number,
            on_error=on_error,
            error_type=error_type,
            error=error_msg,
        )
        raise error

    @staticmethod
    def _is_transient_error(error: Exception) -> bool:
        """Determine whether an exception represents a transient error.

        Transient errors include deadlocks, lock timeouts, connection
        resets, and temporary unavailability that may resolve on retry.

        Args:
            error: The exception to classify.

        Returns:
            ``True`` if the error is likely transient and retryable.
        """
        transient_indicators: Tuple[str, ...] = (
            "deadlock",
            "lock timeout",
            "connection reset",
            "connection refused",
            "broken pipe",
            "timed out",
            "timeout",
            "temporary",
            "too many connections",
            "server closed",
            "communication link failure",
        )

        error_str: str = str(error).lower()
        return any(indicator in error_str for indicator in transient_indicators)
