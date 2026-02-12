"""PostgreSQL JDBC provisioning connector for the Provisioning Service.

This module implements :class:`PostgreSQLConnector`, a concrete subclass of
:class:`~provisioning_service.connectors.base.BaseConnector` that provisions
synthetic data to PostgreSQL databases (versions 12 through 16) via JDBC.

The connector provides:

* **JDBC connectivity** via ``jaydebeapi`` with the ``org.postgresql.Driver``
  JDBC driver (postgresql-42.7.x.jar), supporting SSL/TLS encryption for
  data in transit (AES-256 / TLS 1.3).
* **Optimized bulk loading** via the PostgreSQL ``COPY FROM STDIN`` command
  through the PostgreSQL JDBC CopyManager API, with automatic fallback to
  batched ``INSERT`` statements when COPY is unavailable.
* **Batch INSERT operations** with configurable batch sizes (default 10,000
  rows) and ``ON CONFLICT DO NOTHING`` upsert support.
* **Table creation** with full PostgreSQL type mapping and constraint support
  (PRIMARY KEY, NOT NULL, DEFAULT, UNIQUE, IF NOT EXISTS).
* **Multi-tenant namespace isolation** via PostgreSQL ``search_path`` schema
  scoping, ensuring tenant data is partitioned at the database schema level.
* **Health monitoring** via ``SELECT 1`` connectivity probes,
  ``pg_stat_activity`` connection pool status queries, and ``version()``
  server version introspection.

Design Patterns:
    * **Abstract Factory** — Implements the :class:`BaseConnector` interface,
      selected at runtime by the provisioning layer based on target database
      type.
    * **Strategy** — Swappable with Oracle, SQL Server, and SAP HANA connectors
      through the shared :class:`BaseConnector` contract.
    * **Context Manager** — Inherits ``__enter__``/``__exit__`` from
      :class:`BaseConnector` for deterministic JDBC resource cleanup.

Usage::

    from provisioning_service.connectors.postgresql_connector import (
        PostgreSQLConnector,
    )

    config = {
        "host": "pg.example.com",
        "port": 5432,
        "database": "erp_target",
        "username": "admin",
        "password": "secret",
        "schema": "tenant_001",
        "jar_path": "/opt/jdbc/postgresql-42.7.3.jar",
    }

    with PostgreSQLConnector(config) as conn:
        conn.create_table("gl_entries", columns=[...])
        inserted = conn.batch_insert("gl_entries", ["col1", "col2"], data=[...])
        # Or use optimized COPY for large datasets:
        copied = conn.copy_insert("gl_entries", ["col1", "col2"], data=[...])
"""

from __future__ import annotations

import io
import logging
import os
import time
from typing import Any, Dict, List, Optional, Tuple

import jaydebeapi

from provisioning_service.connectors.base import BaseConnector
from shared.logging.structured_logger import get_logger

# ---------------------------------------------------------------------------
# Module-level Constants
# ---------------------------------------------------------------------------

# Fallback standard logger for environments where structlog is not initialised
# during early bootstrap or in isolated testing contexts.
_fallback_logger: logging.Logger = logging.getLogger(__name__)

# PostgreSQL-native SQL type mapping from the generic column type vocabulary
# defined in :data:`~provisioning_service.connectors.base.GENERIC_COLUMN_TYPES`.
_PG_TYPE_MAP: Dict[str, str] = {
    "STRING": "VARCHAR",
    "INTEGER": "INTEGER",
    "BIGINT": "BIGINT",
    "FLOAT": "DOUBLE PRECISION",
    "DECIMAL": "NUMERIC",
    "BOOLEAN": "BOOLEAN",
    "DATE": "DATE",
    "TIMESTAMP": "TIMESTAMP WITH TIME ZONE",
    "TEXT": "TEXT",
    "JSON": "JSONB",
    "UUID": "UUID",
    "BINARY": "BYTEA",
}


# ---------------------------------------------------------------------------
# PostgreSQLConnector
# ---------------------------------------------------------------------------


class PostgreSQLConnector(BaseConnector):
    """PostgreSQL JDBC provisioning connector for synthetic data loading.

    Extends :class:`BaseConnector` to provide PostgreSQL-specific provisioning
    capabilities including COPY-based bulk loading, schema-scoped multi-tenant
    isolation, and PostgreSQL-native type mapping (e.g. ``JSONB``, ``UUID``,
    ``TIMESTAMP WITH TIME ZONE``).

    Supports PostgreSQL versions 12 through 16 via the ``org.postgresql.Driver``
    JDBC driver.

    Args:
        config: Configuration dictionary containing:

            **Required keys:**

            * ``host`` (str) — PostgreSQL server hostname or IP address.
            * ``database`` (str) — Target database name.
            * ``username`` (str) — Authentication username.
            * ``password`` (str) — Authentication password.

            **Optional keys:**

            * ``port`` (int) — Server port (default ``5432``).
            * ``schema`` (str) — Target schema for multi-tenant isolation
              (default ``"public"``).
            * ``jar_path`` (str) — Absolute path to the PostgreSQL JDBC JAR
              file.  Falls back to ``POSTGRES_JDBC_JAR_PATH`` env var, then
              to ``JDBC_DRIVER_PATH/postgresql-42.7.3.jar``.
            * ``sslmode`` (str) — SSL mode (``disable``, ``require``,
              ``verify-ca``, ``verify-full``).
            * ``sslrootcert`` (str) — Path to root CA certificate.
            * ``sslcert`` (str) — Path to client certificate.
            * ``sslkey`` (str) — Path to client private key.
            * ``pool_min_size`` (int) — Minimum pool size (default 1).
            * ``pool_max_size`` (int) — Maximum pool size (default 10).
            * ``connection_timeout`` (int) — Connect timeout in seconds
              (default 30).
            * ``query_timeout`` (int) — Query timeout in seconds (default 60).
            * ``max_retries`` (int) — Retry attempts (default 3).
            * ``retry_delay`` (float) — Base retry delay seconds (default 2.0).

    Raises:
        ValueError: If required configuration keys are missing.

    Example::

        config = {
            "host": "localhost",
            "database": "erp",
            "username": "u",
            "password": "p",
        }
        with PostgreSQLConnector(config) as conn:
            conn.create_table("orders", [
                {"name": "id", "type": "BIGINT", "primary_key": True},
                {"name": "total", "type": "DECIMAL", "precision": 15, "scale": 2},
            ])
            inserted = conn.batch_insert(
                "orders", ["id", "total"], [(1, 99.99), (2, 149.50)]
            )
    """

    # ------------------------------------------------------------------
    # Class-Level Attributes
    # ------------------------------------------------------------------

    DRIVER_CLASS: str = "org.postgresql.Driver"
    """Fully qualified Java class name for the PostgreSQL JDBC driver."""

    SUPPORTED_VERSIONS: list[str] = ["12", "13", "14", "15", "16"]
    """PostgreSQL server versions supported by this connector."""

    DEFAULT_PORT: int = 5432
    """Default PostgreSQL server port."""

    DEFAULT_SCHEMA: str = "public"
    """Default PostgreSQL schema when no tenant schema is specified."""

    # ------------------------------------------------------------------
    # Initialisation
    # ------------------------------------------------------------------

    def __init__(self, config: Dict[str, Any]) -> None:
        """Initialise the PostgreSQL connector with connection settings.

        Validates configuration, resolves the JDBC driver JAR path, builds
        the JDBC URL with optional SSL parameters, and initialises the
        structured logger scoped to this connector instance.

        Args:
            config: Configuration dictionary (see class docstring for the
                full list of supported keys).

        Raises:
            ValueError: If required configuration keys (``host``,
                ``database``, ``username``, ``password``) are missing, or
                if the JDBC JAR file cannot be located.
        """
        # Inject the default port before delegating to the base initialiser
        # so that BaseConnector.__init__ picks up the correct value.
        if "port" not in config:
            config = {**config, "port": self.DEFAULT_PORT}

        super().__init__(config)

        # Validate required configuration keys via the base-class helper.
        self._validate_config(["host", "database", "username", "password"])

        # PostgreSQL-specific schema for multi-tenant namespace isolation.
        self._schema: str = str(config.get("schema", self.DEFAULT_SCHEMA))

        # SSL connection parameters for AES-256 / TLS 1.3 data-in-transit
        # encryption.  These are appended as JDBC URL query parameters in
        # _get_jdbc_url().
        self._ssl_mode: Optional[str] = config.get("sslmode")
        self._ssl_root_cert: Optional[str] = config.get("sslrootcert")
        self._ssl_cert: Optional[str] = config.get("sslcert")
        self._ssl_key: Optional[str] = config.get("sslkey")

        # Resolve the JDBC driver JAR path from config, environment
        # variables, or the default filesystem location.
        self._jar_path: str = self._resolve_jar_path(config)

        # Build the complete JDBC URL (with SSL params when configured).
        self._jdbc_url: str = self._get_jdbc_url()

        self._logger.info(
            "postgresql_connector_initialized",
            host=self._host,
            port=self._port,
            database=self._database,
            schema=self._schema,
            jdbc_url=self._jdbc_url,
            supported_versions=self.SUPPORTED_VERSIONS,
        )

    # ------------------------------------------------------------------
    # Abstract Method Implementations
    # ------------------------------------------------------------------

    def connect(self) -> None:
        """Establish a JDBC connection to the PostgreSQL database.

        Creates a JDBC connection via ``jaydebeapi.connect()`` using the
        PostgreSQL driver class, the pre-built JDBC URL, and the configured
        credentials.  The connection attempt is wrapped in the base-class
        :meth:`~BaseConnector._execute_with_retry` helper for resilient
        exponential-backoff retry on transient network errors.

        After successful connection:

        1. Auto-commit is disabled for explicit transaction management.
        2. The ``search_path`` is set to the tenant schema for multi-tenant
           namespace isolation.

        Raises:
            ConnectionError: If the connection cannot be established after
                exhausting all retry attempts, or due to invalid credentials
                or driver errors.
        """
        if self._connected:
            self._logger.debug(
                "postgresql_already_connected",
                host=self._host,
                database=self._database,
            )
            return

        try:
            self._logger.info(
                "postgresql_connecting",
                host=self._host,
                port=self._port,
                database=self._database,
                schema=self._schema,
                driver=self.DRIVER_CLASS,
            )

            # Use _execute_with_retry from BaseConnector for resilient
            # connection establishment with exponential backoff.
            def _establish_connection() -> Any:
                """Create the underlying JDBC connection."""
                return jaydebeapi.connect(
                    self.DRIVER_CLASS,
                    self._jdbc_url,
                    [self._username, self._password],
                    self._jar_path,
                )

            connection = self._execute_with_retry(_establish_connection)

            # Disable auto-commit for explicit transaction control.
            connection.jconn.setAutoCommit(False)

            self._connection = connection
            self._connected = True

            # Set search_path to the tenant schema for multi-tenant isolation.
            # This ensures all unqualified table references resolve to the
            # correct tenant namespace.
            cursor = self._connection.cursor()
            try:
                cursor.execute(f"SET search_path TO {self._schema}")
                self._connection.commit()
            finally:
                cursor.close()

            self._logger.info(
                "postgresql_connected",
                host=self._host,
                port=self._port,
                database=self._database,
                schema=self._schema,
            )

        except Exception as exc:
            self._connected = False
            self._connection = None
            self._logger.error(
                "postgresql_connection_failed",
                host=self._host,
                port=self._port,
                database=self._database,
                error=str(exc),
                driver=self.DRIVER_CLASS,
                jdbc_url=self._jdbc_url,
            )
            raise ConnectionError(
                f"Failed to connect to PostgreSQL at "
                f"{self._host}:{self._port}/{self._database}: {exc}"
            ) from exc

    def disconnect(self) -> None:
        """Close the JDBC connection to PostgreSQL and release resources.

        Safely closes the underlying JDBC connection if one is open.  After
        disconnection the connector transitions to a disconnected state
        (``is_connected`` returns ``False``).  Any errors during close are
        logged but do not propagate, ensuring cleanup always completes.
        """
        if not self._connected or self._connection is None:
            self._logger.debug(
                "postgresql_already_disconnected",
                host=self._host,
                database=self._database,
            )
            return

        try:
            self._connection.close()
            self._logger.info(
                "postgresql_disconnected",
                host=self._host,
                port=self._port,
                database=self._database,
                schema=self._schema,
            )
        except Exception as exc:
            self._logger.error(
                "postgresql_disconnect_error",
                host=self._host,
                database=self._database,
                error=str(exc),
            )
        finally:
            self._connection = None
            self._connected = False

    def create_table(
        self,
        table_name: str,
        columns: List[Dict[str, Any]],
        if_not_exists: bool = True,
    ) -> None:
        """Create a table in the PostgreSQL database.

        Builds and executes a ``CREATE TABLE`` statement with column
        definitions mapped from generic types to PostgreSQL-native types.
        Uses the :meth:`~BaseConnector._build_column_definition` helper from
        the base class for column-level SQL fragment assembly (which in turn
        calls :meth:`_map_column_type` for native type resolution).

        The table is created in the current tenant schema
        (``self._schema``).

        Args:
            table_name: Table name (unqualified; the schema prefix is added
                automatically).
            columns: List of column specification dictionaries.  Each dict
                must include ``name`` and ``type`` keys; optional keys are
                ``nullable``, ``primary_key``, ``default``, ``unique``,
                ``max_length``, ``precision``, ``scale``.
            if_not_exists: When ``True`` (default), adds the
                ``IF NOT EXISTS`` clause to prevent errors on duplicate
                table creation.

        Raises:
            RuntimeError: If the connector is not connected.
            Exception: Propagates PostgreSQL DDL errors (after rollback).
        """
        self.ensure_connected()

        # Build column definitions using the base-class helper which
        # delegates to our _map_column_type() for PostgreSQL types.
        column_defs: list[str] = [
            self._build_column_definition(col) for col in columns
        ]

        # Assemble the CREATE TABLE statement.
        exists_clause = "IF NOT EXISTS " if if_not_exists else ""
        qualified_name = f"{self._schema}.{table_name}"
        sql = (
            f"CREATE TABLE {exists_clause}{qualified_name} "
            f"({', '.join(column_defs)})"
        )

        cursor = self._connection.cursor()
        try:
            self._logger.info(
                "postgresql_create_table",
                table_name=qualified_name,
                column_count=len(columns),
                if_not_exists=if_not_exists,
            )
            cursor.execute(sql)
            self._connection.commit()
            self._logger.info(
                "postgresql_table_created",
                table_name=qualified_name,
                column_count=len(columns),
            )
        except Exception as exc:
            try:
                self._connection.rollback()
            except Exception as rollback_exc:
                self._logger.error(
                    "postgresql_create_table_rollback_error",
                    table_name=qualified_name,
                    error=str(rollback_exc),
                )
            self._logger.error(
                "postgresql_create_table_failed",
                table_name=qualified_name,
                error=str(exc),
                sql=sql,
            )
            raise
        finally:
            cursor.close()

    def batch_insert(
        self,
        table_name: str,
        columns: List[str],
        data: List[Tuple],
        batch_size: int = 10000,
    ) -> int:
        """Insert rows into a PostgreSQL table using batched transactions.

        Processes the input data in configurable batch chunks (default 10,000
        rows per batch).  Each batch is committed in its own transaction; on
        failure within a batch, that batch is rolled back while previously
        committed batches remain durable.

        The generated INSERT statement uses parameterised placeholders
        (``?``) and leverages ``executemany()`` for batch execution.
        Supports ``ON CONFLICT DO NOTHING`` for upsert / duplicate-skip
        scenarios when the ``on_conflict_ignore`` flag is set via the config.

        Args:
            table_name: Target table name (unqualified; schema prefix is
                added automatically).
            columns: Ordered list of column names matching the tuple elements
                in *data*.
            data: List of row tuples to insert.
            batch_size: Number of rows per transactional batch (default
                10,000).

        Returns:
            Total number of rows successfully inserted across all batches.

        Raises:
            RuntimeError: If the connector is not connected.
            Exception: Propagates PostgreSQL insert errors (after rollback
                of the failing batch).
        """
        self.ensure_connected()

        if not data:
            return 0

        start_time = time.time()
        total_inserted: int = 0
        qualified_name = f"{self._schema}.{table_name}"
        col_list = ", ".join(columns)
        placeholders = ", ".join(["?" for _ in columns])

        # Build the INSERT statement, optionally with ON CONFLICT DO NOTHING
        # for upsert scenarios (configured via the connector config dict).
        conflict_clause = ""
        if self._config.get("on_conflict_ignore", False):
            conflict_clause = " ON CONFLICT DO NOTHING"

        insert_sql = (
            f"INSERT INTO {qualified_name} ({col_list}) "
            f"VALUES ({placeholders}){conflict_clause}"
        )

        self._logger.info(
            "postgresql_batch_insert_start",
            table_name=qualified_name,
            total_rows=len(data),
            batch_size=batch_size,
            column_count=len(columns),
        )

        # Process data in batch_size chunks for transactional batching.
        for batch_start in range(0, len(data), batch_size):
            batch_end = min(batch_start + batch_size, len(data))
            batch = data[batch_start:batch_end]

            cursor = self._connection.cursor()
            try:
                cursor.executemany(insert_sql, batch)
                self._connection.commit()
                batch_count = len(batch)
                total_inserted += batch_count

                self._logger.debug(
                    "postgresql_batch_committed",
                    table_name=qualified_name,
                    batch_start=batch_start,
                    batch_end=batch_end,
                    batch_rows=batch_count,
                    total_inserted=total_inserted,
                )
            except Exception as exc:
                # Rollback the failed batch; previously committed batches
                # remain durable in the database.
                try:
                    self._connection.rollback()
                except Exception as rollback_exc:
                    self._logger.error(
                        "postgresql_batch_rollback_error",
                        table_name=qualified_name,
                        error=str(rollback_exc),
                    )

                self._logger.error(
                    "postgresql_batch_insert_failed",
                    table_name=qualified_name,
                    batch_start=batch_start,
                    batch_end=batch_end,
                    total_inserted_before_failure=total_inserted,
                    error=str(exc),
                )
                raise
            finally:
                cursor.close()

        elapsed_ms = (time.time() - start_time) * 1000.0
        self._total_rows_inserted += total_inserted

        rows_per_second = (
            round(total_inserted / (elapsed_ms / 1000.0), 2)
            if elapsed_ms > 0
            else 0.0
        )

        self._logger.info(
            "postgresql_batch_insert_complete",
            table_name=qualified_name,
            total_inserted=total_inserted,
            elapsed_ms=round(elapsed_ms, 2),
            rows_per_second=rows_per_second,
        )

        return total_inserted

    def copy_insert(
        self,
        table_name: str,
        columns: List[str],
        data: List[Tuple],
    ) -> int:
        """Bulk-load rows into PostgreSQL using the optimised COPY command.

        This PostgreSQL-specific method leverages the ``COPY ... FROM STDIN``
        command through the PostgreSQL JDBC driver's ``CopyManager`` API for
        significantly faster data loading compared to batched ``INSERT``
        statements (typically 5-10x throughput improvement for large
        datasets).

        Data is serialised into the PostgreSQL text format (tab-separated
        values) in an in-memory :class:`io.StringIO` buffer and streamed
        to the server via the JDBC CopyManager interface.

        If the COPY approach fails for any reason (e.g. JDBC driver
        limitations, JPype unavailability, or CopyManager access errors),
        the method falls back to :meth:`batch_insert` transparently and
        logs a warning.

        Args:
            table_name: Target table name (unqualified; schema prefix is
                added automatically).
            columns: Ordered list of column names matching the tuple elements
                in *data*.
            data: List of row tuples to load.

        Returns:
            Total number of rows successfully loaded (via COPY or fallback
            batch INSERT).
        """
        self.ensure_connected()

        if not data:
            return 0

        start_time = time.time()
        qualified_name = f"{self._schema}.{table_name}"
        col_list = ", ".join(columns)
        copy_sql = (
            f"COPY {qualified_name} ({col_list}) FROM STDIN WITH (FORMAT text)"
        )

        self._logger.info(
            "postgresql_copy_insert_start",
            table_name=qualified_name,
            total_rows=len(data),
            column_count=len(columns),
        )

        try:
            # Build tab-delimited data buffer in PostgreSQL COPY text format.
            # Each row is a tab-separated line with backslash escaping for
            # special characters and \N for NULL representation.
            buffer = io.StringIO()
            for row in data:
                formatted_values: list[str] = [
                    self._escape_copy_value(val) for val in row
                ]
                buffer.write("\t".join(formatted_values) + "\n")

            # Access the PostgreSQL CopyManager through the underlying JDBC
            # Java connection object.  jaydebeapi stores the Java connection
            # in the ``jconn`` attribute (or ``_jconn`` in some versions).
            java_conn = getattr(
                self._connection,
                "jconn",
                getattr(self._connection, "_jconn", None),
            )
            if java_conn is None:
                raise AttributeError(
                    "Cannot access underlying Java JDBC connection object "
                    "for PostgreSQL COPY operation."
                )

            # Import JPype to access the PostgreSQL PGConnection interface
            # and its CopyManager API for streaming COPY FROM STDIN data.
            import jpype  # noqa: E402 — conditional import for COPY support

            PGConnection = jpype.JClass("org.postgresql.PGConnection")
            pg_conn = java_conn.unwrap(PGConnection)
            copy_manager = pg_conn.getCopyAPI()

            # Stream the tab-delimited data to the server via CopyManager.
            data_bytes = buffer.getvalue().encode("utf-8")
            copy_in = copy_manager.copyIn(copy_sql)
            copy_in.writeToCopy(data_bytes, 0, len(data_bytes))
            total_copied = int(copy_in.endCopy())

            # Commit the COPY transaction.
            self._connection.commit()

            elapsed_ms = (time.time() - start_time) * 1000.0
            self._total_rows_inserted += total_copied

            rows_per_second = (
                round(total_copied / (elapsed_ms / 1000.0), 2)
                if elapsed_ms > 0
                else 0.0
            )

            self._logger.info(
                "postgresql_copy_insert_complete",
                table_name=qualified_name,
                total_copied=total_copied,
                elapsed_ms=round(elapsed_ms, 2),
                rows_per_second=rows_per_second,
            )

            return total_copied

        except Exception as copy_exc:
            # Attempt to rollback any partial COPY state before falling back.
            try:
                self._connection.rollback()
            except Exception:
                # Connection may be in an error state after a COPY failure;
                # suppress the rollback error to allow fallback to proceed.
                pass

            self._logger.warning(
                "postgresql_copy_insert_fallback",
                table_name=qualified_name,
                error=str(copy_exc),
                fallback="batch_insert",
            )

            # Transparent fallback to standard batch INSERT.
            return self.batch_insert(table_name, columns, data)

    def execute_query(
        self,
        query: str,
        params: Optional[Tuple] = None,
    ) -> List[Dict[str, Any]]:
        """Execute an arbitrary SQL query against the PostgreSQL database.

        For ``SELECT`` statements the method fetches all rows and returns
        them as a list of dictionaries keyed by column name (derived from
        ``cursor.description``).  For DML/DDL statements the transaction is
        committed and an empty list is returned.

        Query execution time is measured and logged for performance
        monitoring.

        Args:
            query: SQL query string.  Use ``?`` placeholders for
                parameterised queries.
            params: Optional tuple of bind parameter values.

        Returns:
            List of row dictionaries for ``SELECT`` queries; empty list for
            DML/DDL statements.

        Raises:
            RuntimeError: If the connector is not connected.
            Exception: Propagates PostgreSQL query errors (after rollback).
        """
        self.ensure_connected()

        start_time = time.time()
        cursor = self._connection.cursor()

        try:
            if params:
                cursor.execute(query, params)
            else:
                cursor.execute(query)

            # Determine query type by inspecting cursor.description — it is
            # set to a non-None value only for SELECT (result-returning)
            # queries.
            is_select = cursor.description is not None

            if is_select:
                column_names = [desc[0] for desc in cursor.description]
                rows = cursor.fetchall()
                results: List[Dict[str, Any]] = [
                    dict(zip(column_names, row)) for row in rows
                ]
            else:
                # DML/DDL — commit the transaction and return empty.
                self._connection.commit()
                results = []

            elapsed_ms = (time.time() - start_time) * 1000.0
            self._total_queries_executed += 1

            self._logger.debug(
                "postgresql_query_executed",
                query_type="SELECT" if is_select else "DML/DDL",
                row_count=len(results),
                elapsed_ms=round(elapsed_ms, 2),
            )

            return results

        except Exception as exc:
            # Attempt rollback to clean up the failed transaction state.
            try:
                self._connection.rollback()
            except Exception as rollback_exc:
                self._logger.error(
                    "postgresql_query_rollback_error",
                    error=str(rollback_exc),
                )

            elapsed_ms = (time.time() - start_time) * 1000.0
            self._logger.error(
                "postgresql_query_failed",
                error=str(exc),
                elapsed_ms=round(elapsed_ms, 2),
                query_preview=query[:200],
            )
            raise
        finally:
            cursor.close()

    def health_check(self) -> Dict[str, Any]:
        """Verify PostgreSQL connectivity and return a health status dict.

        Performs the following diagnostic queries:

        1. ``SELECT 1`` — Basic connectivity probe with round-trip latency
           measurement via :meth:`~BaseConnector._measure_latency`.
        2. ``SELECT version()`` — PostgreSQL server version string.
        3. ``SELECT count(*) FROM pg_stat_activity WHERE datname = ?`` —
           Active connection count for the current database.

        Returns:
            Dictionary with keys:

            * ``status`` (str) — ``"healthy"`` or ``"unhealthy"``.
            * ``version`` (str) — PostgreSQL server version string, or
              ``"unknown"`` if retrieval fails.
            * ``active_connections`` (int) — Number of active connections
              to the target database.
            * ``database`` (str) — Target database name.
            * ``schema`` (str) — Active schema for tenant isolation.
            * ``latency_ms`` (float) — Round-trip latency in milliseconds
              for the ``SELECT 1`` probe.
        """
        health: Dict[str, Any] = {
            "status": "unhealthy",
            "version": "unknown",
            "active_connections": 0,
            "database": self._database,
            "schema": self._schema,
            "latency_ms": 0.0,
        }

        if not self._connected or self._connection is None:
            self._logger.warning(
                "postgresql_health_check_not_connected",
                host=self._host,
                database=self._database,
            )
            return health

        try:
            # Measure SELECT 1 round-trip latency using the base-class
            # _measure_latency helper for consistent timing.
            def _ping() -> bool:
                """Execute SELECT 1 as a connectivity probe."""
                cursor = self._connection.cursor()
                try:
                    cursor.execute("SELECT 1")
                    cursor.fetchone()
                    return True
                finally:
                    cursor.close()

            _, latency_ms = self._measure_latency(_ping)
            health["latency_ms"] = round(latency_ms, 2)

            # Retrieve the PostgreSQL server version string.
            cursor = self._connection.cursor()
            try:
                cursor.execute("SELECT version()")
                version_row = cursor.fetchone()
                if version_row:
                    health["version"] = str(version_row[0])
            finally:
                cursor.close()

            # Query active connection count from pg_stat_activity.
            cursor = self._connection.cursor()
            try:
                cursor.execute(
                    "SELECT count(*) FROM pg_stat_activity "
                    "WHERE datname = ?",
                    (self._database,),
                )
                count_row = cursor.fetchone()
                if count_row:
                    health["active_connections"] = int(count_row[0])
            finally:
                cursor.close()

            health["status"] = "healthy"

            self._logger.info(
                "postgresql_health_check_passed",
                status=health["status"],
                latency_ms=health["latency_ms"],
                active_connections=health["active_connections"],
                version=health["version"][:80],
            )

        except Exception as exc:
            health["status"] = "unhealthy"
            self._logger.error(
                "postgresql_health_check_failed",
                host=self._host,
                database=self._database,
                error=str(exc),
            )

        return health

    # ------------------------------------------------------------------
    # Private / Protected Helpers
    # ------------------------------------------------------------------

    def _get_jdbc_url(self) -> str:
        """Build the PostgreSQL JDBC connection URL.

        Constructs a URL in the format::

            jdbc:postgresql://<host>:<port>/<database>[?ssl=true&sslmode=...]

        SSL parameters (``sslmode``, ``sslrootcert``, ``sslcert``,
        ``sslkey``) are appended as query parameters when configured,
        enabling AES-256 / TLS 1.3 encrypted connections.

        Returns:
            Fully formatted JDBC URL string.
        """
        base_url = (
            f"jdbc:postgresql://{self._host}:{self._port}/{self._database}"
        )

        # Collect SSL / connection query parameters.
        params: list[str] = []

        if self._ssl_mode:
            params.append(f"sslmode={self._ssl_mode}")
            # Activate the ``ssl`` flag when any mode other than ``disable``
            # is configured.
            if self._ssl_mode != "disable":
                params.append("ssl=true")

        if self._ssl_root_cert:
            params.append(f"sslrootcert={self._ssl_root_cert}")

        if self._ssl_cert:
            params.append(f"sslcert={self._ssl_cert}")

        if self._ssl_key:
            params.append(f"sslkey={self._ssl_key}")

        # Append connection timeout from the base-class config.
        if self._connection_timeout:
            params.append(f"connectTimeout={self._connection_timeout}")

        if params:
            return f"{base_url}?{'&'.join(params)}"

        return base_url

    def _map_column_type(self, generic_type: str) -> str:
        """Map a generic column type to the PostgreSQL-native SQL type.

        Translation table:

        ============  ===========================
        Generic Type  PostgreSQL Type
        ============  ===========================
        STRING        VARCHAR
        INTEGER       INTEGER
        BIGINT        BIGINT
        FLOAT         DOUBLE PRECISION
        DECIMAL       NUMERIC
        BOOLEAN       BOOLEAN
        DATE          DATE
        TIMESTAMP     TIMESTAMP WITH TIME ZONE
        TEXT          TEXT
        JSON          JSONB
        UUID          UUID
        BINARY        BYTEA
        ============  ===========================

        Args:
            generic_type: One of the keys from
                :data:`~provisioning_service.connectors.base.GENERIC_COLUMN_TYPES`
                (case-insensitive matching is applied).

        Returns:
            The PostgreSQL-native SQL type string.

        Raises:
            ValueError: If *generic_type* is not recognised.
        """
        normalised = generic_type.strip().upper()
        pg_type = _PG_TYPE_MAP.get(normalised)

        if pg_type is None:
            raise ValueError(
                f"Unsupported generic column type '{generic_type}' for "
                f"PostgreSQL. Supported types: "
                f"{', '.join(sorted(_PG_TYPE_MAP.keys()))}"
            )

        return pg_type

    @staticmethod
    def _resolve_jar_path(config: Dict[str, Any]) -> str:
        """Resolve the absolute filesystem path to the PostgreSQL JDBC JAR.

        Resolution order:

        1. ``config["jar_path"]`` — Explicitly provided in the config dict.
        2. ``POSTGRES_JDBC_JAR_PATH`` — Environment variable.
        3. ``JDBC_DRIVER_PATH`` env var + ``postgresql-42.7.3.jar``.
        4. Default path ``/opt/jdbc/drivers/postgresql-42.7.3.jar``.

        Args:
            config: Connector configuration dictionary.

        Returns:
            Resolved path to the PostgreSQL JDBC JAR file.  The path is
            returned even if the file does not exist on the current
            filesystem (to support containerised environments where the
            JAR is mounted at runtime).
        """
        jar_path: Optional[str] = config.get("jar_path")

        if not jar_path:
            jar_path = os.environ.get("POSTGRES_JDBC_JAR_PATH")

        if not jar_path:
            driver_dir = os.environ.get(
                "JDBC_DRIVER_PATH", "/opt/jdbc/drivers"
            )
            jar_path = os.path.join(driver_dir, "postgresql-42.7.3.jar")

        # Validate that the JAR file exists on the filesystem.  In
        # containerised / CI environments the JAR may be mounted at
        # runtime, so a missing file is logged as a warning rather than
        # raising an error.
        if not os.path.exists(jar_path):
            _fallback_logger.warning(
                "PostgreSQL JDBC JAR not found at '%s'. Proceeding — "
                "ensure the JAR is available at container runtime.",
                jar_path,
            )

        return jar_path

    @staticmethod
    def _escape_copy_value(value: Any) -> str:
        """Escape a single value for PostgreSQL COPY text format.

        PostgreSQL COPY text format uses tab-separated values with
        backslash escaping for special characters and ``\\N`` for NULL
        representation.

        Args:
            value: The Python value to escape.  ``None`` is rendered as
                ``\\N`` (the PostgreSQL NULL literal for COPY format).

        Returns:
            Escaped string suitable for inclusion in a COPY data stream.
        """
        if value is None:
            return "\\N"

        str_val = str(value)
        # Escape backslashes first to prevent double-escaping.
        str_val = str_val.replace("\\", "\\\\")
        str_val = str_val.replace("\t", "\\t")
        str_val = str_val.replace("\n", "\\n")
        str_val = str_val.replace("\r", "\\r")
        return str_val
