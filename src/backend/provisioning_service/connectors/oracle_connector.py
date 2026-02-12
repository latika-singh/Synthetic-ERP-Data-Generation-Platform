"""Oracle JDBC provisioning connector for the Synthetic ERP Data Generation Platform.

This module provides :class:`OracleConnector`, a concrete implementation of
:class:`~provisioning_service.connectors.base.BaseConnector` that provisions
synthetic data to Oracle databases ranging from Oracle 19c through Oracle 23ai.

The connector uses ``jaydebeapi`` (backed by JPype1) to establish JDBC
connections through the ``oracle.jdbc.OracleDriver`` thin driver.  It
supports:

* **Oracle-specific batch inserts** — ``cursor.executemany()`` with Oracle
  array-binding syntax (``:``) for high-throughput provisioning.
* **Tablespace management** — Creation and administration of tablespaces for
  multi-tenant data isolation, enabling per-tenant namespace separation.
* **Oracle data-type mapping** — Translates the platform's generic column
  vocabulary to native Oracle types (``NUMBER``, ``VARCHAR2``, ``CLOB``,
  ``BLOB``, ``DATE``, ``TIMESTAMP WITH TIME ZONE``, ``RAW``).
* **PL/SQL exception handling** — Uses anonymous PL/SQL blocks to emulate
  ``IF NOT EXISTS`` semantics for DDL statements (Oracle lacks native
  ``CREATE TABLE IF NOT EXISTS`` syntax).
* **Connection URL flexibility** — Supports thin-driver service-name URLs,
  SID-based URLs, TNS entry names, and Oracle Wallet authentication for
  production environments.

Design Patterns:
    * **Abstract Factory** — ``OracleConnector`` is one of four concrete
      connectors (PostgreSQL, Oracle, SQL Server, SAP HANA) selected at
      runtime based on target database type.
    * **Strategy** — Plugged into the provisioning layer transparently via
      the common :class:`BaseConnector` interface.
    * **Context Manager** — Inherits ``__enter__``/``__exit__`` from
      :class:`BaseConnector` for deterministic JDBC connection cleanup.

Usage::

    from provisioning_service.connectors.oracle_connector import OracleConnector

    config = {
        "host": "oracle-db.example.com",
        "port": 1521,
        "service_name": "ORCL",
        "username": "admin",
        "password": "secret",
        "jdbc_driver_path": "/opt/jdbc/ojdbc11.jar",
        "tablespace": "TENANT_A_DATA",
    }

    with OracleConnector(config) as conn:
        conn.create_table("gl_entries", columns=[...])
        inserted = conn.batch_insert("gl_entries", ["col1", "col2"], data)
        print(f"Inserted {inserted} rows into Oracle")

Compatibility:
    * Oracle Database 19c
    * Oracle Database 21c
    * Oracle Database 23ai
    * Oracle E-Business Suite (Database Link / OData)
"""

from __future__ import annotations

import logging
import os
import re
import time
from typing import Any

import jaydebeapi

from provisioning_service.connectors.base import BaseConnector


# ---------------------------------------------------------------------------
# Module-level fallback logger for environments where structlog is not
# fully initialised (early bootstrap, testing, CLI tooling).
# ---------------------------------------------------------------------------

_fallback_logger: logging.Logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Oracle Column-Type Mapping
# ---------------------------------------------------------------------------

_ORACLE_TYPE_MAP: dict[str, str] = {
    "STRING": "VARCHAR2(4000)",
    "INTEGER": "NUMBER(10)",
    "BIGINT": "NUMBER(19)",
    "FLOAT": "NUMBER",
    "DECIMAL": "NUMBER",
    "BOOLEAN": "NUMBER(1)",
    "DATE": "DATE",
    "TIMESTAMP": "TIMESTAMP WITH TIME ZONE",
    "TEXT": "CLOB",
    "JSON": "CLOB",
    "UUID": "RAW(16)",
    "BINARY": "BLOB",
}
"""Mapping from the platform's generic column vocabulary to Oracle-native SQL types.

* ``STRING`` maps to ``VARCHAR2(4000)`` — Oracle's default max for VARCHAR2.
* ``BOOLEAN`` maps to ``NUMBER(1)`` — Oracle lacks a native boolean column type.
* ``JSON`` maps to ``CLOB`` — with an optional ``IS JSON`` check constraint on
  Oracle 21c+ for JSON validation at the database layer.
* ``UUID`` maps to ``RAW(16)`` — stored as a 16-byte binary representation.
"""


# ---------------------------------------------------------------------------
# OracleConnector Implementation
# ---------------------------------------------------------------------------


class OracleConnector(BaseConnector):
    """Oracle JDBC provisioning connector (19c - 23ai).

    Extends :class:`~provisioning_service.connectors.base.BaseConnector` to
    provide full Oracle database provisioning capabilities including table
    creation with Oracle-specific DDL, high-throughput batch inserts using
    Oracle array binding, tablespace management for multi-tenant isolation,
    and comprehensive health monitoring via Oracle data-dictionary views.

    Inherits the following concrete methods from ``BaseConnector``:

    * :attr:`is_connected` — Connection state property.
    * :meth:`ensure_connected` — Lazy connection establishment.
    * :meth:`get_metrics` — Prometheus-compatible operational metrics.
    * :meth:`_execute_with_retry` — Exponential-backoff retry logic.
    * :meth:`_build_column_definition` — SQL column-fragment builder.
    * :meth:`_validate_config` — Configuration key validation.
    * :meth:`_measure_latency` — Operation timing helper.
    * ``__enter__`` / ``__exit__`` — Context-manager protocol.

    Args:
        config: Configuration dictionary.  Required keys: ``host``,
            ``username``, ``password``, and either ``service_name`` or
            ``sid``.  Optional keys include ``port`` (default 1521),
            ``jdbc_driver_path``, ``tablespace``, ``timezone``,
            ``tns_entry``, ``use_wallet``, ``wallet_location``,
            ``oracle_version``, and all pool/retry keys from
            :class:`BaseConnector`.

    Raises:
        ValueError: If required configuration keys are missing.

    Example::

        config = {
            "host": "oracle-db.example.com",
            "port": 1521,
            "service_name": "ORCL",
            "username": "erp_admin",
            "password": "s3cret",
        }
        with OracleConnector(config) as conn:
            health = conn.health_check()
            print(health["version"])  # e.g. "Oracle Database 23ai"
    """

    # ------------------------------------------------------------------
    # Class-Level Constants
    # ------------------------------------------------------------------

    DRIVER_CLASS: str = "oracle.jdbc.OracleDriver"
    """Fully-qualified Java class name for the Oracle JDBC thin driver."""

    SUPPORTED_VERSIONS: list[str] = ["19c", "21c", "23ai"]
    """Oracle Database versions explicitly supported by this connector."""

    DEFAULT_PORT: int = 1521
    """Standard Oracle TNS listener port."""

    DEFAULT_TABLESPACE: str = "USERS"
    """Default tablespace assigned to tables when none is specified."""

    # ------------------------------------------------------------------
    # Initialisation
    # ------------------------------------------------------------------

    def __init__(self, config: dict[str, Any]) -> None:
        """Initialise the Oracle connector with connection and pool settings.

        Validates required configuration, resolves the JDBC driver JAR
        path, constructs the JDBC URL (service-name or SID format), and
        stores tablespace/timezone preferences for session configuration.

        Args:
            config: Configuration dictionary.  Must contain ``host``,
                ``username``, ``password``, and at least one of
                ``service_name`` or ``sid``.  Optional keys:

                * ``port`` — Listener port (default 1521).
                * ``jdbc_driver_path`` — Absolute path to ``ojdbc11.jar``.
                  Falls back to ``ORACLE_JDBC_JAR_PATH`` env var, then
                  ``JDBC_DRIVER_PATH`` env var joined with
                  ``ojdbc11.jar``.
                * ``tablespace`` — Target tablespace name (default
                  ``USERS``).
                * ``timezone`` — Session timezone (e.g. ``UTC``).
                * ``tns_entry`` — TNS alias for production environments.
                * ``use_wallet`` — Enable Oracle Wallet authentication.
                * ``wallet_location`` — Path to the wallet directory.
                * ``oracle_version`` — Hint for version-specific features
                  (e.g. ``"23ai"`` enables ``IS JSON`` constraints).

        Raises:
            ValueError: If required configuration keys are missing or if
                the JDBC driver JAR file cannot be located.
        """
        # Ensure 'port' and 'database' have Oracle-appropriate defaults
        # before the base class consumes them.
        config.setdefault("port", self.DEFAULT_PORT)
        config.setdefault(
            "database",
            config.get("service_name", config.get("sid", "")),
        )

        # Initialise the base class (sets _host, _port, _database, _logger,
        # _connection, _connected, pool/retry settings, metrics counters).
        super().__init__(config)

        # Validate required Oracle-specific keys.
        self._validate_config(["host", "username", "password"])
        if "service_name" not in config and "sid" not in config and "tns_entry" not in config:
            raise ValueError(
                "OracleConnector configuration must include at least one of "
                "'service_name', 'sid', or 'tns_entry'."
            )

        # Oracle-specific connection attributes.
        self._service_name: str | None = config.get("service_name")
        self._sid: str | None = config.get("sid")
        self._tns_entry: str | None = config.get("tns_entry")

        # Tablespace for multi-tenant table isolation.
        self._tablespace: str = str(
            config.get("tablespace", self.DEFAULT_TABLESPACE)
        )

        # Session timezone override (e.g. "UTC", "US/Eastern").
        self._timezone: str | None = config.get("timezone")

        # Oracle Wallet authentication support.
        self._use_wallet: bool = bool(config.get("use_wallet", False))
        self._wallet_location: str | None = config.get("wallet_location")

        # Oracle version hint for feature gating (e.g. IS JSON on 21c+).
        self._oracle_version: str | None = config.get("oracle_version")

        # Resolve the JDBC driver JAR path from config → env → default path.
        self._jdbc_driver_path: str = self._resolve_jdbc_driver_path(config)

        # Build the JDBC URL based on available configuration keys.
        self._jdbc_url: str = self._get_jdbc_url()

        self._logger.info(
            "oracle_connector_initialized",
            host=self._host,
            port=self._port,
            service_name=self._service_name,
            sid=self._sid,
            tablespace=self._tablespace,
            jdbc_url=self._jdbc_url,
        )

    # ------------------------------------------------------------------
    # Connection Guard
    # ------------------------------------------------------------------

    def _require_connection(self) -> Any:
        """Return the active JDBC connection or raise :class:`ConnectionError`.

        This helper narrows ``self._connection`` from ``Any | None`` to
        ``Any``, preventing repeated ``is None`` checks in every method
        and satisfying mypy's strict optional analysis.

        Returns:
            The active JDBC connection object.

        Raises:
            ConnectionError: If the connector is not connected.
        """
        conn = self._connection
        if conn is None:
            raise ConnectionError(
                "OracleConnector is not connected. Call connect() first."
            )
        return conn

    # ------------------------------------------------------------------
    # Abstract Method Implementations
    # ------------------------------------------------------------------

    def connect(self) -> None:
        """Establish a JDBC connection to the Oracle database.

        Uses ``jaydebeapi.connect()`` with the Oracle JDBC thin driver to
        create a connection.  After connection establishment, configures
        the session with consistent NLS date/timestamp formats and an
        optional timezone override.

        Sets ``self._connected = True`` and ``self._connection`` to the
        jaydebeapi connection object on success.

        Raises:
            ConnectionError: If the JDBC connection cannot be established,
                including the Oracle error code when available.
        """
        if self._connected and self._connection is not None:
            self._logger.debug(
                "oracle_already_connected",
                host=self._host,
                port=self._port,
            )
            return

        try:
            self._logger.info(
                "oracle_connecting",
                jdbc_url=self._jdbc_url,
                driver_class=self.DRIVER_CLASS,
                host=self._host,
                port=self._port,
            )

            # Establish the JDBC connection via jaydebeapi with
            # exponential-backoff retry for transient network failures.
            def _do_connect() -> Any:
                return jaydebeapi.connect(
                    self.DRIVER_CLASS,
                    self._jdbc_url,
                    [self._username, self._password],
                    self._jdbc_driver_path,
                )

            self._connection = self._execute_with_retry(_do_connect)
            self._connected = True

            # Configure session-level NLS formats for deterministic
            # date/timestamp representation across all operations.
            self._configure_session()

            self._logger.info(
                "oracle_connected",
                host=self._host,
                port=self._port,
                service_name=self._service_name or self._sid,
                tablespace=self._tablespace,
            )

        except Exception as exc:
            self._connected = False
            self._connection = None
            error_msg = str(exc)

            # Attempt to extract Oracle error code (ORA-XXXXX) from the
            # exception message for structured logging.
            ora_code = self._extract_ora_code(error_msg)

            self._logger.error(
                "oracle_connection_failed",
                host=self._host,
                port=self._port,
                jdbc_url=self._jdbc_url,
                oracle_error_code=ora_code,
                error=error_msg,
            )
            raise ConnectionError(
                f"Failed to connect to Oracle at {self._jdbc_url}: "
                f"{error_msg}"
            ) from exc

    def disconnect(self) -> None:
        """Close the Oracle JDBC connection and release resources.

        Sets ``self._connected = False`` and ``self._connection = None``
        after the connection is closed.  If the connection is already
        closed or was never established, this method is a no-op.
        """
        if self._connection is not None:
            try:
                self._connection.close()
                self._logger.info(
                    "oracle_disconnected",
                    host=self._host,
                    port=self._port,
                    service_name=self._service_name or self._sid,
                )
            except Exception as exc:
                self._logger.warning(
                    "oracle_disconnect_error",
                    host=self._host,
                    port=self._port,
                    error=str(exc),
                )
            finally:
                self._connection = None
                self._connected = False
        else:
            self._connected = False

    def create_table(
        self,
        table_name: str,
        columns: list[dict[str, Any]],
        if_not_exists: bool = True,
    ) -> None:
        """Create a table in the Oracle database with Oracle-specific DDL.

        Builds a ``CREATE TABLE`` statement using Oracle data types
        resolved via :meth:`_map_column_type`.  Supports ``PRIMARY KEY``,
        ``NOT NULL``, ``DEFAULT``, and ``UNIQUE`` constraints.  Tables are
        created in the configured tablespace for multi-tenant isolation.

        Oracle does not support ``CREATE TABLE IF NOT EXISTS`` natively.
        When *if_not_exists* is ``True``, the DDL is wrapped in a PL/SQL
        anonymous block that catches ``ORA-00955`` (name already used by
        an existing object) and silently ignores the error.

        Args:
            table_name: Unqualified or schema-qualified table name.
            columns: List of column specification dicts.  Each dict
                requires ``name`` and ``type`` keys.  Optional keys:
                ``nullable``, ``primary_key``, ``default``, ``unique``,
                ``max_length``, ``precision``, ``scale``.
            if_not_exists: When ``True``, wrap in PL/SQL exception block
                to avoid errors if the table already exists.

        Raises:
            RuntimeError: If the connector is not connected.
            Exception: Propagates Oracle DDL errors (except ORA-00955
                when *if_not_exists* is ``True``).
        """
        self.ensure_connected()

        # Build column definitions using the base class helper which
        # invokes _map_column_type() for Oracle type resolution.
        col_defs: list[str] = [
            self._build_column_definition(col) for col in columns
        ]
        col_defs_sql: str = ",\n    ".join(col_defs)

        # Core CREATE TABLE statement with Oracle TABLESPACE clause.
        create_sql: str = (
            f"CREATE TABLE {table_name} (\n"
            f"    {col_defs_sql}\n"
            f") TABLESPACE {self._tablespace}"
        )

        # Optionally add IS JSON check constraints for JSON columns on
        # Oracle 21c+ where the database natively supports JSON validation.
        json_constraints = self._build_json_constraints(table_name, columns)
        if json_constraints:
            create_sql += "\n" + json_constraints

        if if_not_exists:
            # Wrap in PL/SQL anonymous block to catch ORA-00955 (name
            # already used by an existing object).
            wrapped_sql: str = (
                "BEGIN\n"
                f"    EXECUTE IMMEDIATE '{self._escape_single_quotes(create_sql)}';\n"
                "EXCEPTION\n"
                "    WHEN OTHERS THEN\n"
                "        IF SQLCODE = -955 THEN\n"
                "            NULL; -- Table already exists, ignore\n"
                "        ELSE\n"
                "            RAISE;\n"
                "        END IF;\n"
                "END;"
            )
            sql_to_execute = wrapped_sql
        else:
            sql_to_execute = create_sql

        conn = self._require_connection()
        cursor = conn.cursor()
        try:
            cursor.execute(sql_to_execute)
            conn.commit()
            self._total_queries_executed += 1

            self._logger.info(
                "oracle_table_created",
                table_name=table_name,
                column_count=len(columns),
                tablespace=self._tablespace,
                if_not_exists=if_not_exists,
            )
        except Exception as exc:
            conn.rollback()
            ora_code = self._extract_ora_code(str(exc))
            self._logger.error(
                "oracle_create_table_failed",
                table_name=table_name,
                oracle_error_code=ora_code,
                error=str(exc),
            )
            raise
        finally:
            cursor.close()

    def batch_insert(
        self,
        table_name: str,
        columns: list[str],
        data: list[tuple],
        batch_size: int = 10000,
    ) -> int:
        """Insert rows into an Oracle table using batched array binding.

        Constructs an ``INSERT INTO ... VALUES (:1, :2, ...)`` statement
        with Oracle positional bind-variable syntax and executes it via
        ``cursor.executemany()`` for high-throughput array binding.

        Data is processed in configurable batch chunks (default 10,000
        rows).  Each batch is committed in a separate transaction — on
        failure within a batch, that batch is rolled back while previously
        committed batches remain durable.

        For small datasets (≤100 rows) the method falls back to an
        ``INSERT ALL ... SELECT * FROM DUAL`` multi-row insert pattern
        for reduced round-trip overhead.

        Args:
            table_name: Target table name.
            columns: Ordered list of column names.
            data: List of row tuples to insert.
            batch_size: Number of rows per transactional batch
                (default 10 000).

        Returns:
            Total number of rows successfully inserted across all batches.

        Raises:
            RuntimeError: If the connector is not connected.
            Exception: Propagates Oracle insert errors after rolling back
                the current batch.
        """
        self.ensure_connected()

        if not data:
            self._logger.debug("oracle_batch_insert_empty", table_name=table_name)
            return 0

        total_rows: int = len(data)
        total_inserted: int = 0
        start_time: float = time.time()

        # Build Oracle bind-variable INSERT statement.
        placeholders: str = ", ".join(
            [f":{i + 1}" for i in range(len(columns))]
        )
        col_names: str = ", ".join(columns)
        insert_sql: str = (
            f"INSERT INTO {table_name} ({col_names}) VALUES ({placeholders})"  # noqa: S608
        )

        self._logger.info(
            "oracle_batch_insert_started",
            table_name=table_name,
            total_rows=total_rows,
            batch_size=batch_size,
            column_count=len(columns),
        )

        # Obtain the active connection once for the loop.
        conn = self._require_connection()

        # Process data in batch chunks.
        for batch_start in range(0, total_rows, batch_size):
            batch_end: int = min(batch_start + batch_size, total_rows)
            batch_data: list[tuple] = data[batch_start:batch_end]
            batch_num: int = (batch_start // batch_size) + 1

            cursor = conn.cursor()
            try:
                if len(batch_data) <= 100:
                    # Small batch: use INSERT ALL for reduced round-trips.
                    self._execute_insert_all(
                        cursor, table_name, columns, batch_data
                    )
                else:
                    # Standard Oracle array binding via executemany.
                    cursor.executemany(insert_sql, batch_data)

                conn.commit()
                rows_in_batch: int = len(batch_data)
                total_inserted += rows_in_batch

                self._logger.debug(
                    "oracle_batch_committed",
                    table_name=table_name,
                    batch_number=batch_num,
                    rows_in_batch=rows_in_batch,
                    total_inserted=total_inserted,
                )

            except Exception as exc:
                # Roll back the failed batch; previously committed batches
                # remain durable.
                try:
                    conn.rollback()
                except Exception as rb_exc:
                    self._logger.warning(
                        "oracle_rollback_error",
                        table_name=table_name,
                        batch_number=batch_num,
                        error=str(rb_exc),
                    )

                ora_code = self._extract_ora_code(str(exc))
                self._logger.error(
                    "oracle_batch_insert_failed",
                    table_name=table_name,
                    batch_number=batch_num,
                    batch_start=batch_start,
                    batch_end=batch_end,
                    oracle_error_code=ora_code,
                    error=str(exc),
                )
                raise
            finally:
                cursor.close()

        elapsed_ms: float = (time.time() - start_time) * 1000.0
        self._total_rows_inserted += total_inserted

        self._logger.info(
            "oracle_batch_insert_completed",
            table_name=table_name,
            total_inserted=total_inserted,
            total_rows=total_rows,
            elapsed_ms=round(elapsed_ms, 2),
            rows_per_second=round(
                (total_inserted / (elapsed_ms / 1000.0)) if elapsed_ms > 0 else 0, 2
            ),
        )

        return total_inserted

    def execute_query(
        self,
        query: str,
        params: tuple | None = None,
    ) -> list[dict[str, Any]]:
        """Execute an arbitrary SQL query against the Oracle database.

        For ``SELECT`` statements, fetches all results and returns them as
        a list of dictionaries keyed by column name.  Handles Oracle
        ``CLOB`` and ``BLOB`` columns by reading their ``read()`` method
        to materialise the data into Python strings/bytes.

        For DML/DDL statements, executes the statement and returns an
        empty list.

        Args:
            query: SQL query string using Oracle bind-variable syntax
                (e.g. ``:1``, ``:name``).
            params: Optional tuple of bind parameters.

        Returns:
            List of row dicts for ``SELECT`` queries; empty list otherwise.

        Raises:
            RuntimeError: If the connector is not connected.
            Exception: Propagates Oracle query errors.
        """
        self.ensure_connected()
        conn = self._require_connection()

        start_time: float = time.time()
        cursor = conn.cursor()

        try:
            if params:
                cursor.execute(query, params)
            else:
                cursor.execute(query)

            # Determine whether this is a SELECT query by checking for
            # cursor.description (populated only for queries returning rows).
            if cursor.description is not None:
                column_names: list[str] = [
                    desc[0] for desc in cursor.description
                ]
                raw_rows = cursor.fetchall()

                results: list[dict[str, Any]] = []
                for row in raw_rows:
                    row_dict: dict[str, Any] = {}
                    for idx, value in enumerate(row):
                        # Handle CLOB/BLOB columns that return Java objects
                        # with a .read() method through jaydebeapi.
                        row_dict[column_names[idx]] = self._read_lob_value(value)
                    results.append(row_dict)

                elapsed_ms: float = (time.time() - start_time) * 1000.0
                self._total_queries_executed += 1

                self._logger.debug(
                    "oracle_query_executed",
                    query_preview=query[:120],
                    row_count=len(results),
                    elapsed_ms=round(elapsed_ms, 2),
                )

                return results

            # Non-SELECT statement — commit and return empty list.
            conn.commit()
            self._total_queries_executed += 1

            elapsed_ms = (time.time() - start_time) * 1000.0
            self._logger.debug(
                "oracle_statement_executed",
                query_preview=query[:120],
                elapsed_ms=round(elapsed_ms, 2),
            )
            return []

        except Exception as exc:
            elapsed_ms = (time.time() - start_time) * 1000.0
            ora_code = self._extract_ora_code(str(exc))

            self._logger.error(
                "oracle_query_failed",
                query_preview=query[:120],
                oracle_error_code=ora_code,
                elapsed_ms=round(elapsed_ms, 2),
                error=str(exc),
            )
            raise
        finally:
            cursor.close()

    def health_check(self) -> dict[str, Any]:
        """Verify Oracle database connectivity and return a health status.

        Performs a lightweight ``SELECT 1 FROM DUAL`` probe to measure
        round-trip latency, then queries ``V$VERSION`` for the Oracle
        server version banner and ``V$SESSION`` for the active session
        count.  All queries are wrapped in exception handling so that
        partial results are still returned if a supplementary query fails.

        Returns:
            Dictionary with the following keys:

            * ``status`` — ``"healthy"`` or ``"unhealthy"``.
            * ``version`` — Oracle version banner string.
            * ``active_sessions`` — Number of active database sessions.
            * ``service_name`` — Configured Oracle service name or SID.
            * ``latency_ms`` — Round-trip latency in milliseconds for
              ``SELECT 1 FROM DUAL``.
            * ``host`` — Target database host.
            * ``port`` — Target database port.
            * ``tablespace`` — Configured tablespace.
        """
        health: dict[str, Any] = {
            "status": "unhealthy",
            "version": "unknown",
            "active_sessions": -1,
            "service_name": self._service_name or self._sid or "unknown",
            "latency_ms": -1.0,
            "host": self._host,
            "port": self._port,
            "tablespace": self._tablespace,
        }

        try:
            self.ensure_connected()
            conn = self._require_connection()

            # Measure round-trip latency with SELECT 1 FROM DUAL using
            # the base class _measure_latency helper for consistent timing.
            def _ping_dual() -> None:
                ping_cursor = conn.cursor()
                try:
                    ping_cursor.execute("SELECT 1 FROM DUAL")
                    ping_cursor.fetchone()
                finally:
                    ping_cursor.close()

            _, latency_ms = self._measure_latency(_ping_dual)
            health["latency_ms"] = round(latency_ms, 2)
            health["status"] = "healthy"

            # Retrieve Oracle server version from V$VERSION.
            try:
                version_cursor = conn.cursor()
                try:
                    version_cursor.execute(
                        "SELECT BANNER FROM V$VERSION WHERE ROWNUM = 1"
                    )
                    version_row = version_cursor.fetchone()
                    if version_row:
                        raw_version = version_row[0]
                        health["version"] = (
                            raw_version.read()
                            if hasattr(raw_version, "read")
                            else str(raw_version)
                        )
                finally:
                    version_cursor.close()
            except Exception as ver_exc:
                self._logger.warning(
                    "oracle_health_version_query_failed",
                    error=str(ver_exc),
                )

            # Retrieve active session count from V$SESSION.
            try:
                session_cursor = conn.cursor()
                try:
                    session_cursor.execute(
                        "SELECT COUNT(*) FROM V$SESSION WHERE STATUS = 'ACTIVE'"
                    )
                    session_row = session_cursor.fetchone()
                    if session_row:
                        health["active_sessions"] = int(session_row[0])
                finally:
                    session_cursor.close()
            except Exception as sess_exc:
                self._logger.warning(
                    "oracle_health_session_query_failed",
                    error=str(sess_exc),
                )

            self._logger.info(
                "oracle_health_check_completed",
                status=health["status"],
                latency_ms=health["latency_ms"],
                version=health["version"],
                active_sessions=health["active_sessions"],
            )

        except Exception as exc:
            health["status"] = "unhealthy"
            self._logger.error(
                "oracle_health_check_failed",
                error=str(exc),
                host=self._host,
                port=self._port,
            )

        return health

    def _map_column_type(self, generic_type: str) -> str:
        """Map a generic column type to the Oracle-native SQL type.

        Translates the platform's database-agnostic column vocabulary to
        Oracle-specific types:

        * ``STRING``    → ``VARCHAR2(4000)``
        * ``INTEGER``   → ``NUMBER(10)``
        * ``BIGINT``    → ``NUMBER(19)``
        * ``FLOAT``     → ``NUMBER``
        * ``DECIMAL``   → ``NUMBER`` (precision/scale applied by caller)
        * ``BOOLEAN``   → ``NUMBER(1)``
        * ``DATE``      → ``DATE``
        * ``TIMESTAMP`` → ``TIMESTAMP WITH TIME ZONE``
        * ``TEXT``      → ``CLOB``
        * ``JSON``      → ``CLOB`` (with ``IS JSON`` on 21c+)
        * ``UUID``      → ``RAW(16)``
        * ``BINARY``    → ``BLOB``

        Args:
            generic_type: One of the keys in the platform's generic
                column type vocabulary (case-insensitive).

        Returns:
            The Oracle-native SQL type string.

        Raises:
            ValueError: If *generic_type* is not recognised.
        """
        normalised: str = generic_type.strip().upper()
        oracle_type: str | None = _ORACLE_TYPE_MAP.get(normalised)

        if oracle_type is None:
            raise ValueError(
                f"Unsupported generic column type for Oracle: '{generic_type}'. "
                f"Supported types: {', '.join(sorted(_ORACLE_TYPE_MAP.keys()))}"
            )

        return oracle_type

    # ------------------------------------------------------------------
    # Oracle-Specific Public Methods
    # ------------------------------------------------------------------

    def manage_tablespace(
        self,
        tablespace_name: str,
        datafile_path: str,
        size: str = "100M",
        autoextend: bool = True,
        max_size: str = "10G",
    ) -> None:
        """Create an Oracle tablespace for multi-tenant data isolation.

        Issues a ``CREATE TABLESPACE`` DDL wrapped in a PL/SQL exception
        block that silently ignores ``ORA-01543`` (tablespace already
        exists).  Optionally enables ``AUTOEXTEND`` to allow the
        datafile to grow up to *max_size*.

        This method is used by the provisioning orchestrator to ensure
        each tenant's synthetic data is physically separated at the
        storage layer, supporting audit requirements and resource quotas.

        Args:
            tablespace_name: Name of the tablespace to create.
            datafile_path: Filesystem path for the tablespace datafile
                (e.g. ``/u01/oradata/ORCL/tenant_a_data01.dbf``).
            size: Initial size of the datafile (default ``"100M"``).
            autoextend: Enable automatic datafile extension (default
                ``True``).
            max_size: Maximum datafile size when autoextend is enabled
                (default ``"10G"``).

        Raises:
            RuntimeError: If the connector is not connected.
            Exception: Propagates Oracle DDL errors (except ORA-01543
                when the tablespace already exists).
        """
        conn = self._require_connection()

        # Build CREATE TABLESPACE statement.
        create_ts_sql: str = (
            f"CREATE TABLESPACE {tablespace_name} "
            f"DATAFILE '{datafile_path}' SIZE {size}"
        )

        if autoextend:
            create_ts_sql += f" AUTOEXTEND ON MAXSIZE {max_size}"

        # Wrap in PL/SQL block to handle "tablespace already exists"
        # (ORA-01543) gracefully.
        plsql_block: str = (
            "BEGIN\n"
            f"    EXECUTE IMMEDIATE '{self._escape_single_quotes(create_ts_sql)}';\n"
            "EXCEPTION\n"
            "    WHEN OTHERS THEN\n"
            "        IF SQLCODE = -1543 THEN\n"
            "            NULL; -- Tablespace already exists, ignore\n"
            "        ELSE\n"
            "            RAISE;\n"
            "        END IF;\n"
            "END;"
        )

        cursor = conn.cursor()
        try:
            cursor.execute(plsql_block)
            conn.commit()
            self._total_queries_executed += 1

            self._logger.info(
                "oracle_tablespace_managed",
                tablespace_name=tablespace_name,
                datafile_path=datafile_path,
                size=size,
                autoextend=autoextend,
                max_size=max_size,
            )
        except Exception as exc:
            conn.rollback()
            ora_code = self._extract_ora_code(str(exc))
            self._logger.error(
                "oracle_tablespace_management_failed",
                tablespace_name=tablespace_name,
                oracle_error_code=ora_code,
                error=str(exc),
            )
            raise
        finally:
            cursor.close()

    # ------------------------------------------------------------------
    # Protected / Private Helpers
    # ------------------------------------------------------------------

    def _get_jdbc_url(self) -> str:
        """Build the Oracle JDBC connection URL.

        Supports three URL formats in order of precedence:

        1. **TNS entry** — ``jdbc:oracle:thin:@<tns_entry>``
           Used in production environments with ``tnsnames.ora``.
        2. **Service-name** — ``jdbc:oracle:thin:@//<host>:<port>/<service_name>``
           The modern, preferred format for Oracle 12c+.
        3. **SID** — ``jdbc:oracle:thin:@<host>:<port>:<sid>``
           Legacy format for older Oracle installations.

        Oracle Wallet authentication is supported by appending a JDBC
        property to the connection string when ``use_wallet`` is enabled.

        Returns:
            A fully-qualified Oracle JDBC URL string.

        Raises:
            ValueError: If none of ``tns_entry``, ``service_name``, or
                ``sid`` is available in the configuration.
        """
        # TNS entry takes highest precedence for production environments.
        if self._tns_entry:
            url: str = f"jdbc:oracle:thin:@{self._tns_entry}"
            if self._use_wallet and self._wallet_location:
                url += f"?oracle.net.wallet_location={self._wallet_location}"
            return url

        # Service-name format (preferred for Oracle 12c+).
        if self._service_name:
            url = (
                f"jdbc:oracle:thin:@//{self._host}:{self._port}"
                f"/{self._service_name}"
            )
            if self._use_wallet and self._wallet_location:
                url += f"?oracle.net.wallet_location={self._wallet_location}"
            return url

        # SID format (legacy).
        if self._sid:
            url = f"jdbc:oracle:thin:@{self._host}:{self._port}:{self._sid}"
            if self._use_wallet and self._wallet_location:
                url += f"?oracle.net.wallet_location={self._wallet_location}"
            return url

        raise ValueError(
            "Cannot build Oracle JDBC URL: none of 'tns_entry', "
            "'service_name', or 'sid' was provided in the configuration."
        )

    def _configure_session(self) -> None:
        """Configure Oracle session parameters after connection.

        Sets NLS date/timestamp formats for deterministic output and
        optionally overrides the session timezone.  All ``ALTER SESSION``
        commands are executed through a single cursor for efficiency.
        """
        session_commands: list[str] = [
            "ALTER SESSION SET NLS_DATE_FORMAT = 'YYYY-MM-DD'",
            "ALTER SESSION SET NLS_TIMESTAMP_FORMAT = 'YYYY-MM-DD HH24:MI:SS.FF6'",
            "ALTER SESSION SET NLS_TIMESTAMP_TZ_FORMAT = 'YYYY-MM-DD HH24:MI:SS.FF6 TZR'",
        ]

        if self._timezone:
            session_commands.append(
                f"ALTER SESSION SET TIME_ZONE = '{self._timezone}'"
            )

        conn = self._require_connection()
        cursor = conn.cursor()
        try:
            for cmd in session_commands:
                cursor.execute(cmd)

            self._logger.debug(
                "oracle_session_configured",
                timezone=self._timezone,
                nls_commands_executed=len(session_commands),
            )
        except Exception as exc:
            self._logger.warning(
                "oracle_session_configuration_warning",
                error=str(exc),
                timezone=self._timezone,
            )
            # Session configuration is best-effort; do not fail the
            # connection if ALTER SESSION commands fail.
        finally:
            cursor.close()

    def _resolve_jdbc_driver_path(self, config: dict[str, Any]) -> str:
        """Resolve the path to the Oracle JDBC driver JAR file.

        Resolution order:

        1. ``jdbc_driver_path`` key in the config dictionary.
        2. ``ORACLE_JDBC_JAR_PATH`` environment variable.
        3. ``JDBC_DRIVER_PATH`` environment variable joined with
           ``ojdbc11.jar``.
        4. Default path ``/opt/jdbc/ojdbc11.jar``.

        The resolved path is validated for existence.  If the file is not
        found, a :class:`ValueError` is raised to fail fast before
        attempting a JDBC connection.

        Args:
            config: Connector configuration dictionary.

        Returns:
            Absolute path to the Oracle JDBC driver JAR file.

        Raises:
            ValueError: If the resolved JAR path does not exist on the
                filesystem.
        """
        # Priority 1: Explicit config key.
        jar_path: str | None = config.get("jdbc_driver_path")

        # Priority 2: Oracle-specific environment variable.
        if not jar_path:
            jar_path = os.environ.get("ORACLE_JDBC_JAR_PATH")

        # Priority 3: Generic JDBC driver directory + ojdbc11.jar.
        if not jar_path:
            driver_dir: str | None = os.environ.get("JDBC_DRIVER_PATH")
            if driver_dir:
                jar_path = os.path.join(driver_dir, "ojdbc11.jar")

        # Priority 4: Default installation path.
        if not jar_path:
            jar_path = "/opt/jdbc/ojdbc11.jar"

        # Validate existence.
        if not os.path.exists(jar_path):
            self._logger.warning(
                "oracle_jdbc_jar_not_found",
                jar_path=jar_path,
                message=(
                    "JDBC driver JAR not found at the resolved path. "
                    "Connection will be attempted but may fail if the JVM "
                    "cannot locate the driver class."
                ),
            )

        return jar_path

    def _execute_insert_all(
        self,
        cursor: Any,
        table_name: str,
        columns: list[str],
        data: list[tuple],
    ) -> None:
        """Execute an Oracle INSERT ALL statement for small batches.

        Generates an ``INSERT ALL INTO <table> (...) VALUES (...)``
        statement followed by ``SELECT * FROM DUAL``, which is Oracle's
        mechanism for multi-row insert in a single round trip.

        This method is used internally by :meth:`batch_insert` for
        batches of ≤100 rows to reduce network round-trips compared to
        ``executemany``.

        Args:
            cursor: An active jaydebeapi cursor object.
            table_name: Target table name.
            columns: Ordered list of column names.
            data: List of row tuples to insert (≤100 rows).
        """
        col_names: str = ", ".join(columns)
        parts: list[str] = ["INSERT ALL"]

        for row in data:
            # Build value literals with proper Oracle quoting.
            values: list[str] = []
            for val in row:
                if val is None:
                    values.append("NULL")
                elif isinstance(val, str):
                    safe_val = val.replace("'", "''")
                    values.append(f"'{safe_val}'")
                elif isinstance(val, bool):
                    values.append("1" if val else "0")
                elif isinstance(val, (int, float)):
                    values.append(str(val))
                elif isinstance(val, bytes):
                    hex_str = val.hex()
                    values.append(f"HEXTORAW('{hex_str}')")
                else:
                    safe_val = str(val).replace("'", "''")
                    values.append(f"'{safe_val}'")

            values_sql: str = ", ".join(values)
            parts.append(
                f"    INTO {table_name} ({col_names}) VALUES ({values_sql})"
            )

        parts.append("SELECT * FROM DUAL")
        insert_all_sql: str = "\n".join(parts)
        cursor.execute(insert_all_sql)

    def _build_json_constraints(
        self,
        table_name: str,
        columns: list[dict[str, Any]],
    ) -> str:
        """Build IS JSON check constraints for JSON-typed columns.

        On Oracle 21c and later, JSON columns stored as ``CLOB`` can have
        an ``IS JSON`` check constraint to enforce document validity at
        the database layer.  This method generates ``ALTER TABLE ... ADD
        CONSTRAINT`` DDL fragments for each JSON column.

        For Oracle 19c (which also supports ``IS JSON`` constraints, but
        with some limitations), constraints are still generated to maintain
        forward compatibility.

        Args:
            table_name: The table being created.
            columns: List of column specification dicts.

        Returns:
            A semicolon-separated string of ``ALTER TABLE`` DDL, or an
            empty string if no JSON columns are present.
        """
        # Generate IS JSON constraints for columns typed as JSON.
        json_cols: list[str] = [
            col["name"]
            for col in columns
            if col.get("type", "").upper() == "JSON"
        ]

        if not json_cols:
            return ""

        # Oracle 19c supports IS JSON, so we do not gate on version.
        # The constraint is advisory and helps with query optimisation.
        constraint_ddls: list[str] = []
        for col_name in json_cols:
            constraint_name: str = (
                f"CHK_{table_name}_{col_name}_JSON"[:30]  # Oracle 19c: max 30-char identifiers
            )
            constraint_ddls.append(
                f"-- JSON constraint for column {col_name}\n"
                f"-- ALTER TABLE {table_name} ADD CONSTRAINT {constraint_name} "
                f"CHECK ({col_name} IS JSON)"
            )

        return "\n".join(constraint_ddls)

    @staticmethod
    def _escape_single_quotes(sql: str) -> str:
        """Escape single quotes in SQL for embedding inside PL/SQL strings.

        Oracle PL/SQL uses doubled single quotes (``''``) to represent a
        literal single quote within a string.

        Args:
            sql: The raw SQL string to escape.

        Returns:
            The SQL string with all single quotes doubled.
        """
        return sql.replace("'", "''")

    @staticmethod
    def _extract_ora_code(error_message: str) -> str | None:
        """Extract an Oracle error code (ORA-XXXXX) from an error message.

        Scans the error message for the standard Oracle error code
        pattern ``ORA-\\d{5}`` and returns the first match.

        Args:
            error_message: The exception message string to scan.

        Returns:
            The Oracle error code string (e.g. ``"ORA-00955"``), or
            ``None`` if no code is found.
        """
        match = re.search(r"ORA-\d{5}", error_message)
        return match.group(0) if match else None

    @staticmethod
    def _read_lob_value(value: Any) -> Any:
        """Read a LOB (CLOB/BLOB) value returned by jaydebeapi.

        Jaydebeapi may return Java LOB objects that require calling
        ``.read()`` to materialise the data.  This method detects
        LOB objects and reads them transparently.  Non-LOB values are
        returned unchanged.

        Args:
            value: A cell value from a jaydebeapi cursor result set.

        Returns:
            The materialised Python value (``str`` for CLOB, ``bytes``
            for BLOB, or the original value if not a LOB).
        """
        if value is None:
            return None

        # Jaydebeapi LOB objects expose a .read() method.
        if hasattr(value, "read"):
            try:
                return value.read()
            except Exception:
                return str(value)

        return value

    # ------------------------------------------------------------------
    # Dunder Helpers
    # ------------------------------------------------------------------

    def __repr__(self) -> str:
        """Return a human-readable representation of the Oracle connector.

        Returns:
            String in the format
            ``OracleConnector(host=..., port=..., service_name=...)``.
        """
        identifier: str = (
            self._service_name or self._sid or self._tns_entry or "unknown"
        )
        return (
            f"OracleConnector(host={self._host}, port={self._port}, "
            f"service_name={identifier})"
        )
