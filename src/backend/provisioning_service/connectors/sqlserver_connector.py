"""SQL Server JDBC provisioning connector for the Synthetic ERP Data Generation Platform.

This module provides :class:`SQLServerConnector`, a concrete implementation of
:class:`~provisioning_service.connectors.base.BaseConnector` that enables
provisioning synthetic data to Microsoft SQL Server databases (versions 2019
through 2022).

Key Capabilities:
    * **JDBC connectivity** via ``jaydebeapi`` using the
      ``com.microsoft.sqlserver.jdbc.SQLServerDriver`` driver class.
    * **BCP-style BULK INSERT** for high-throughput data loading of large
      datasets into SQL Server tables via T-SQL ``BULK INSERT`` with
      configurable field/row terminators and temporary BCP-format files.
    * **Batch INSERT** operations with bracket-quoted identifiers, T-SQL
      optimizations, configurable batch sizes, and ``SET IDENTITY_INSERT``
      support for identity columns.
    * **Schema-qualified table creation** using ``[{schema}].[{table}]``
      naming for multi-tenant namespace isolation.
    * **SQL Server data type mapping** — ``STRING`` → ``NVARCHAR(4000)``,
      ``INTEGER`` → ``INT``, ``BIGINT`` → ``BIGINT``, ``FLOAT`` → ``FLOAT``,
      ``DECIMAL`` → ``DECIMAL(p,s)``, ``BOOLEAN`` → ``BIT``,
      ``DATE`` → ``DATE``, ``TIMESTAMP`` → ``DATETIME2(7)``,
      ``TEXT`` → ``NVARCHAR(MAX)``, ``JSON`` → ``NVARCHAR(MAX)``,
      ``UUID`` → ``UNIQUEIDENTIFIER``, ``BINARY`` → ``VARBINARY(MAX)``.
    * **Health monitoring** via ``@@VERSION``, ``sys.dm_exec_sessions``, and
      ``SELECT 1`` latency probes.
    * **Context manager** protocol inherited from ``BaseConnector``.

Design Patterns:
    * **Strategy** — ``SQLServerConnector`` is a concrete strategy selected at
      runtime by the provisioning orchestrator when the target database type is
      ``"sqlserver"`` or ``"mssql"``.
    * **Abstract Factory** — Extends ``BaseConnector`` to guarantee a uniform
      connector interface across PostgreSQL, Oracle, SQL Server, and SAP HANA.

Usage::

    from provisioning_service.connectors.sqlserver_connector import SQLServerConnector

    config = {
        "host": "sqlserver.example.com",
        "port": 1433,
        "database": "erp_synthetic",
        "username": "sa_user",
        "password": "s3cur3p@ss",
        "schema": "tenant_001",
    }

    with SQLServerConnector(config) as conn:
        conn.create_table("gl_entries", columns=[...])
        inserted = conn.batch_insert("gl_entries", ["col1", "col2"], data=[...])
        print(f"Inserted {inserted} rows")
"""

from __future__ import annotations

import contextlib
import logging
import os
import tempfile
import time
from typing import Any

import jaydebeapi

from provisioning_service.connectors.base import BaseConnector
from shared.logging.structured_logger import get_logger


# ---------------------------------------------------------------------------
# Module-level fallback logger for early-bootstrap or test environments
# where structlog may not yet be configured.
# ---------------------------------------------------------------------------
_fallback_logger: logging.Logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# SQL Server Column Type Mapping
# ---------------------------------------------------------------------------

_SQLSERVER_TYPE_MAP: dict[str, str] = {
    "STRING": "NVARCHAR(4000)",
    "INTEGER": "INT",
    "BIGINT": "BIGINT",
    "FLOAT": "FLOAT",
    "DECIMAL": "DECIMAL",
    "BOOLEAN": "BIT",
    "DATE": "DATE",
    "TIMESTAMP": "DATETIME2(7)",
    "TEXT": "NVARCHAR(MAX)",
    "JSON": "NVARCHAR(MAX)",
    "UUID": "UNIQUEIDENTIFIER",
    "BINARY": "VARBINARY(MAX)",
}
"""Mapping of generic column type tokens to their SQL Server-native equivalents.

``DECIMAL`` is listed without precision/scale here; callers should supply
``precision`` and ``scale`` column-spec keys, which
:meth:`SQLServerConnector._map_column_type` merges at runtime.
"""


# ---------------------------------------------------------------------------
# SQLServerConnector
# ---------------------------------------------------------------------------


class SQLServerConnector(BaseConnector):
    """JDBC provisioning connector for Microsoft SQL Server 2019-2022.

    ``SQLServerConnector`` extends :class:`BaseConnector` and implements every
    abstract method defined in the base class.  It adds SQL Server-specific
    features such as BCP-style ``BULK INSERT``, schema-qualified DDL with
    bracket-quoted identifiers, and ``SET IDENTITY_INSERT`` for identity
    columns.

    Args:
        config: Connection configuration dictionary.  Required keys:

            * ``host`` (str) — SQL Server hostname or IP address.
            * ``port`` (int) — TCP port (default 1433).
            * ``database`` (str) — Target database name.
            * ``username`` (str) — SQL Server login username.
            * ``password`` (str) — Login password.

            Optional keys:

            * ``schema`` (str) — Default schema for multi-tenant isolation
              (default ``"dbo"``).
            * ``jdbc_jar_path`` (str) — Path to the ``mssql-jdbc-*.jar``
              driver file.  Falls back to ``SQLSERVER_JDBC_JAR_PATH`` and
              ``JDBC_DRIVER_PATH`` environment variables.
            * ``integrated_security`` (bool) — Use Windows Integrated
              Authentication (default ``False``).
            * ``encrypt`` (bool) — Require TLS encryption (default ``True``).
            * ``trust_server_certificate`` (bool) — Trust the server
              certificate without CA validation (default ``False``).
            * ``pool_min_size`` (int) — Min connections in pool (default 1).
            * ``pool_max_size`` (int) — Max connections in pool (default 10).
            * ``connection_timeout`` (int) — Connection timeout in seconds
              (default 30).
            * ``query_timeout`` (int) — Query timeout in seconds
              (default 60).
            * ``max_retries`` (int) — Retry attempts for transient errors
              (default 3).
            * ``retry_delay`` (float) — Base retry delay in seconds
              (default 2.0).

    Raises:
        ValueError: If required configuration keys are missing.

    Example::

        config = {"host": "localhost", "database": "erp", "username": "sa",
                  "password": "Pa$$w0rd!", "schema": "tenant_42"}
        with SQLServerConnector(config) as conn:
            conn.create_table("invoices", columns=[
                {"name": "id", "type": "INTEGER", "primary_key": True, "nullable": False},
                {"name": "amount", "type": "DECIMAL", "precision": 18, "scale": 2},
            ])
            conn.batch_insert("invoices", ["id", "amount"], [(1, 99.99), (2, 250.00)])
    """

    # ------------------------------------------------------------------
    # Class-level constants
    # ------------------------------------------------------------------

    DRIVER_CLASS: str = "com.microsoft.sqlserver.jdbc.SQLServerDriver"
    """Fully-qualified JDBC driver class name for SQL Server."""

    SUPPORTED_VERSIONS: list[str] = ["2019", "2022"]
    """SQL Server versions validated for compatibility with this connector."""

    DEFAULT_PORT: int = 1433
    """Default TCP port for Microsoft SQL Server."""

    DEFAULT_SCHEMA: str = "dbo"
    """Default schema used when ``schema`` is not specified in config."""

    # BCP / BULK INSERT delimiters
    _FIELD_TERMINATOR: str = "\t"
    _ROW_TERMINATOR: str = "\n"

    # ------------------------------------------------------------------
    # Initialisation
    # ------------------------------------------------------------------

    def __init__(self, config: dict[str, Any]) -> None:
        """Initialise the SQL Server connector with connection parameters.

        Validates that required configuration keys (``host``, ``database``,
        ``username``, ``password``) are present and resolves the JDBC driver
        JAR path from the config or environment variables.

        Args:
            config: Connection configuration dictionary (see class docstring
                for full key reference).
        """
        # Invoke BaseConnector.__init__ first — sets _host, _port, _database,
        # _username, _password, pool/retry settings, and metrics counters.
        super().__init__(config)

        # Validate mandatory keys early.
        self._validate_config(["host", "database", "username", "password"])

        # Override port with SQL Server default when not explicitly provided.
        if not self._port:
            self._port = self.DEFAULT_PORT

        # Multi-tenant schema name (bracket-quoted in SQL).
        self._schema: str = str(config.get("schema", self.DEFAULT_SCHEMA))

        # TLS / authentication options.
        self._encrypt: bool = bool(config.get("encrypt", True))
        self._trust_server_certificate: bool = bool(
            config.get("trust_server_certificate", False)
        )
        self._integrated_security: bool = bool(
            config.get("integrated_security", False)
        )

        # Resolve JDBC driver JAR path.
        self._jdbc_jar_path: str = self._resolve_jdbc_jar_path(config)

        # Build the JDBC URL once during initialisation.
        self._jdbc_url: str = self._get_jdbc_url()

        # Structured logger scoped to this connector.
        try:
            self._logger = get_logger(__name__)
        except Exception:
            self._logger = _fallback_logger  # type: ignore[assignment]

        self._logger.info(
            "sqlserver_connector_initialised",
            host=self._host,
            port=self._port,
            database=self._database,
            schema=self._schema,
            encrypt=self._encrypt,
        )

    # ------------------------------------------------------------------
    # Abstract method implementations
    # ------------------------------------------------------------------

    def connect(self) -> None:
        """Establish a JDBC connection to the SQL Server instance.

        Uses ``jaydebeapi.connect()`` with the Microsoft SQL Server JDBC
        driver.  After connection, session-level settings are applied:

        * ``SET ANSI_NULLS ON`` — Standard NULL comparison semantics.
        * ``SET QUOTED_IDENTIFIER ON`` — Allow bracket/double-quote
          delimited identifiers.

        If the configured schema does not exist it is created automatically
        via :meth:`_create_schema_if_not_exists`.

        Raises:
            ConnectionError: If the connection cannot be established.
        """
        if self._connected and self._connection is not None:
            self._logger.debug(
                "sqlserver_already_connected",
                host=self._host,
                database=self._database,
            )
            return

        try:
            # Use the inherited _execute_with_retry helper for resilient
            # connection establishment (exponential backoff on transient
            # network/server errors).
            connection = self._execute_with_retry(
                self._establish_jdbc_connection,
            )
            self._connection = connection

            # Apply session-level settings for deterministic T-SQL behaviour.
            cursor = connection.cursor()
            try:
                cursor.execute("SET ANSI_NULLS ON")
                cursor.execute("SET QUOTED_IDENTIFIER ON")
            finally:
                cursor.close()

            # Ensure the target schema exists for multi-tenant isolation.
            if self._schema.lower() != self.DEFAULT_SCHEMA.lower():
                self._create_schema_if_not_exists(self._schema)

            self._connected = True
            self._logger.info(
                "sqlserver_connected",
                host=self._host,
                port=self._port,
                database=self._database,
                schema=self._schema,
            )

        except Exception as exc:
            self._connected = False
            self._connection = None
            self._logger.error(
                "sqlserver_connection_failed",
                host=self._host,
                port=self._port,
                database=self._database,
                error=str(exc),
            )
            raise ConnectionError(
                f"Failed to connect to SQL Server at "
                f"{self._host}:{self._port}/{self._database}: {exc}"
            ) from exc

    def disconnect(self) -> None:
        """Close the JDBC connection to SQL Server and release resources.

        Sets ``is_connected`` to ``False`` and the internal connection
        reference to ``None`` regardless of whether the close succeeds.
        """
        if self._connection is not None:
            try:
                self._connection.close()
                self._logger.info(
                    "sqlserver_disconnected",
                    host=self._host,
                    port=self._port,
                    database=self._database,
                )
            except Exception as exc:
                self._logger.warning(
                    "sqlserver_disconnect_warning",
                    host=self._host,
                    database=self._database,
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
        """Create a table in SQL Server with schema-qualified naming.

        Generates a ``CREATE TABLE [{schema}].[{table}]`` DDL statement
        with SQL Server-native column types resolved via
        :meth:`_map_column_type`.  Primary keys use ``CLUSTERED`` indexing.

        Args:
            table_name: Simple (unqualified) table name.
            columns: List of column specification dictionaries.  Each dict
                supports keys: ``name``, ``type``, ``nullable``,
                ``primary_key``, ``default``, ``unique``, ``max_length``,
                ``precision``, ``scale``.
            if_not_exists: When ``True``, wraps the DDL in an
                ``IF NOT EXISTS (SELECT * FROM sys.tables …)`` guard
                to avoid errors on duplicate creation.

        Raises:
            RuntimeError: If the connector is not connected.
            Exception: Propagates SQL Server DDL errors.
        """
        self.ensure_connected()

        # Build column definitions using the base-class helper which
        # delegates type resolution to our _map_column_type override.
        col_defs: list[str] = []
        pk_columns: list[str] = []

        for col_spec in columns:
            col_name: str = col_spec.get("name", "")
            # Build the SQL fragment; _build_column_definition calls
            # _map_column_type internally.
            col_def: str = self._build_column_definition(col_spec)

            # Bracket-quote the column name in the definition fragment.
            # _build_column_definition returns "name TYPE …"; we need
            # "[name] TYPE …" for SQL Server.
            col_def = f"[{col_name}] " + col_def.split(" ", 1)[1]
            col_defs.append(col_def)

            if col_spec.get("primary_key", False):
                pk_columns.append(col_name)

        column_sql: str = ",\n    ".join(col_defs)

        # Append a composite CLUSTERED PRIMARY KEY if any PK columns.
        pk_clause: str = ""
        if pk_columns:
            # Remove inline PRIMARY KEY from individual column defs to avoid
            # duplicate constraint errors when using a table-level PK.
            cleaned_defs: list[str] = []
            for cd in col_defs:
                cleaned_defs.append(cd.replace(" PRIMARY KEY", ""))
            column_sql = ",\n    ".join(cleaned_defs)

            pk_cols_quoted: str = ", ".join(f"[{c}]" for c in pk_columns)
            pk_clause = (
                f",\n    CONSTRAINT [PK_{table_name}] "
                f"PRIMARY KEY CLUSTERED ({pk_cols_quoted})"
            )

        full_table: str = f"[{self._schema}].[{table_name}]"
        create_sql: str = (
            f"CREATE TABLE {full_table} (\n    {column_sql}{pk_clause}\n)"
        )

        if if_not_exists:
            safe_table: str = table_name.replace("'", "''")
            safe_schema: str = self._schema.replace("'", "''")
            create_sql = (
                f"IF NOT EXISTS (\n"  # noqa: S608
                f"    SELECT * FROM sys.tables t\n"
                f"    JOIN sys.schemas s ON t.schema_id = s.schema_id\n"
                f"    WHERE t.name = N'{safe_table}'\n"
                f"    AND s.name = N'{safe_schema}'\n"
                f")\n"
                f"BEGIN\n"
                f"    {create_sql}\n"
                f"END"
            )

        conn = self._require_connection()
        cursor = conn.cursor()
        try:
            cursor.execute(create_sql)
            conn.commit()
            self._logger.info(
                "sqlserver_table_created",
                table=full_table,
                columns_count=len(columns),
                if_not_exists=if_not_exists,
            )
        except Exception as exc:
            with contextlib.suppress(Exception):
                conn.rollback()
            self._logger.error(
                "sqlserver_create_table_failed",
                table=full_table,
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
        """Insert rows into a SQL Server table in transactional batches.

        Generates bracket-quoted ``INSERT INTO [{schema}].[{table}]``
        statements and uses ``cursor.executemany()`` for each batch of
        *batch_size* rows.  Each batch is committed independently; a
        failure within a batch triggers a rollback of that batch only.

        Args:
            table_name: Target table name (unqualified).
            columns: Ordered list of column names.
            data: List of row tuples to insert.
            batch_size: Rows per transactional batch (default 10 000).

        Returns:
            Total number of rows successfully inserted.

        Raises:
            RuntimeError: If the connector is not connected.
            Exception: Propagates SQL Server insert errors after rollback.
        """
        self.ensure_connected()

        if not data:
            return 0

        full_table: str = f"[{self._schema}].[{table_name}]"
        cols_quoted: str = ", ".join(f"[{c}]" for c in columns)
        placeholders: str = ", ".join("?" for _ in columns)
        insert_sql: str = (
            f"INSERT INTO {full_table} ({cols_quoted}) VALUES ({placeholders})"  # noqa: S608
        )

        total_inserted: int = 0
        total_batches: int = (len(data) + batch_size - 1) // batch_size
        start_time: float = time.time()

        self._logger.info(
            "sqlserver_batch_insert_start",
            table=full_table,
            total_rows=len(data),
            batch_size=batch_size,
            total_batches=total_batches,
        )

        conn = self._require_connection()

        for batch_idx in range(total_batches):
            batch_start: int = batch_idx * batch_size
            batch_end: int = min(batch_start + batch_size, len(data))
            batch_data: list[tuple] = data[batch_start:batch_end]

            cursor = conn.cursor()
            try:
                cursor.executemany(insert_sql, batch_data)
                conn.commit()
                rows_in_batch: int = len(batch_data)
                total_inserted += rows_in_batch

                self._logger.debug(
                    "sqlserver_batch_committed",
                    table=full_table,
                    batch=batch_idx + 1,
                    total_batches=total_batches,
                    rows_in_batch=rows_in_batch,
                    total_inserted=total_inserted,
                )
            except Exception as exc:
                with contextlib.suppress(Exception):
                    conn.rollback()
                self._logger.error(
                    "sqlserver_batch_insert_failed",
                    table=full_table,
                    batch=batch_idx + 1,
                    total_batches=total_batches,
                    rows_committed_so_far=total_inserted,
                    error=str(exc),
                )
                raise
            finally:
                cursor.close()

        elapsed_ms: float = (time.time() - start_time) * 1000.0
        self._total_rows_inserted += total_inserted
        self._logger.info(
            "sqlserver_batch_insert_complete",
            table=full_table,
            total_inserted=total_inserted,
            elapsed_ms=round(elapsed_ms, 2),
        )
        return total_inserted

    def bulk_insert(
        self,
        table_name: str,
        columns: list[str],
        data: list[tuple],
    ) -> int:
        """High-throughput data loading via T-SQL ``BULK INSERT``.

        Writes the supplied *data* to a temporary BCP-format file and
        executes a ``BULK INSERT`` statement against the SQL Server table.
        This path is significantly faster than :meth:`batch_insert` for
        large datasets (100 K+ rows).

        If the ``BULK INSERT`` fails (e.g. due to file-access permissions
        or BCP format issues), the method falls back to :meth:`batch_insert`
        automatically.

        Args:
            table_name: Target table name (unqualified).
            columns: Ordered list of column names matching the data tuples.
            data: List of row tuples to insert.

        Returns:
            Total number of rows successfully inserted.

        Raises:
            RuntimeError: If the connector is not connected.
            Exception: Propagates errors from both BULK INSERT and the
                fallback :meth:`batch_insert` if both fail.
        """
        self.ensure_connected()

        if not data:
            return 0

        full_table: str = f"[{self._schema}].[{table_name}]"
        tmp_path: str | None = None
        start_time: float = time.time()

        self._logger.info(
            "sqlserver_bulk_insert_start",
            table=full_table,
            total_rows=len(data),
        )

        try:
            # Write data to a temporary BCP-format file using a context manager.
            with tempfile.NamedTemporaryFile(
                mode="w",
                suffix=".bcp",
                delete=False,
                encoding="utf-8",
            ) as tmp_file:
                tmp_path = tmp_file.name

                for row in data:
                    # Convert each value to its string representation.
                    # NULL is represented as an empty string in BCP format.
                    str_vals: list[str] = [
                        "" if v is None else str(v) for v in row
                    ]
                    tmp_file.write(
                        self._FIELD_TERMINATOR.join(str_vals) + self._ROW_TERMINATOR
                    )

            # Build the BULK INSERT T-SQL statement.
            safe_path: str = tmp_path.replace("'", "''")
            bulk_sql: str = (
                f"BULK INSERT {full_table}\n"
                f"FROM '{safe_path}'\n"
                f"WITH (\n"
                f"    FIELDTERMINATOR = '{self._FIELD_TERMINATOR}',\n"
                f"    ROWTERMINATOR = '{self._ROW_TERMINATOR}',\n"
                f"    TABLOCK,\n"
                f"    MAXERRORS = 0\n"
                f")"
            )

            conn = self._require_connection()
            cursor = conn.cursor()
            try:
                cursor.execute(bulk_sql)
                conn.commit()
            finally:
                cursor.close()

            total_inserted: int = len(data)
            elapsed_ms: float = (time.time() - start_time) * 1000.0
            self._total_rows_inserted += total_inserted
            self._logger.info(
                "sqlserver_bulk_insert_complete",
                table=full_table,
                total_inserted=total_inserted,
                elapsed_ms=round(elapsed_ms, 2),
            )
            return total_inserted

        except Exception as exc:
            self._logger.warning(
                "sqlserver_bulk_insert_fallback",
                table=full_table,
                error=str(exc),
                fallback="batch_insert",
            )
            # Fallback to standard batch_insert on BULK INSERT failure.
            return self.batch_insert(table_name, columns, data)

        finally:
            # Clean up the temporary BCP file.
            if tmp_path is not None and os.path.exists(tmp_path):
                try:
                    os.remove(tmp_path)
                except OSError as rm_exc:
                    self._logger.warning(
                        "sqlserver_tmp_cleanup_failed",
                        path=tmp_path,
                        error=str(rm_exc),
                    )

    def execute_query(
        self,
        query: str,
        params: tuple | None = None,
    ) -> list[dict[str, Any]]:
        """Execute an arbitrary T-SQL query and return results as row dicts.

        For ``SELECT`` statements the method fetches all rows and returns
        them as a list of dictionaries keyed by column name.  For DML/DDL
        statements (``INSERT``, ``UPDATE``, ``DELETE``, ``CREATE``, etc.)
        an empty list is returned.

        ``NTEXT`` and ``NVARCHAR(MAX)`` columns are handled transparently;
        values are returned as Python strings.

        Args:
            query: T-SQL query string.  Use ``?`` placeholders for
                parameterised queries.
            params: Optional tuple of bind parameter values.

        Returns:
            List of row dictionaries for ``SELECT`` queries; otherwise an
            empty list.

        Raises:
            RuntimeError: If the connector is not connected.
            Exception: Propagates SQL Server query errors.
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

            # Determine if this is a result-returning statement.
            if cursor.description is None:
                # DML / DDL - no result set.
                with contextlib.suppress(Exception):
                    conn.commit()
                return []

            col_names: list[str] = [
                desc[0] for desc in cursor.description
            ]
            rows = cursor.fetchall()

            results: list[dict[str, Any]] = []
            for row in rows:
                row_dict: dict[str, Any] = {}
                for idx, col_name in enumerate(col_names):
                    value = row[idx]
                    # Handle JDBC Clob / NClob objects returned for
                    # NTEXT / NVARCHAR(MAX) columns.
                    if hasattr(value, "getSubString"):
                        try:
                            value = value.getSubString(1, int(value.length()))
                        except Exception:
                            value = str(value)
                    row_dict[col_name] = value
                results.append(row_dict)

            elapsed_ms: float = (time.time() - start_time) * 1000.0
            self._total_queries_executed += 1
            self._logger.debug(
                "sqlserver_query_executed",
                query_preview=query[:120],
                rows_returned=len(results),
                elapsed_ms=round(elapsed_ms, 2),
            )
            return results

        except Exception as exc:
            elapsed_ms = (time.time() - start_time) * 1000.0
            self._logger.error(
                "sqlserver_query_failed",
                query_preview=query[:120],
                elapsed_ms=round(elapsed_ms, 2),
                error=str(exc),
            )
            raise
        finally:
            cursor.close()

    def health_check(self) -> dict[str, Any]:
        """Verify SQL Server connectivity and return a health status dict.

        Executes ``SELECT 1`` to confirm round-trip latency, then queries
        ``@@VERSION`` for the server version string and
        ``sys.dm_exec_sessions`` for the active session count.

        Returns:
            Dictionary with keys:

            * ``status`` (str) — ``"healthy"`` or ``"unhealthy"``.
            * ``version`` (str) — SQL Server version string.
            * ``active_sessions`` (int) — Number of active sessions.
            * ``database`` (str) — Target database name.
            * ``host`` (str) — Target host.
            * ``latency_ms`` (float) — Round-trip latency in milliseconds.
        """
        result: dict[str, Any] = {
            "status": "unhealthy",
            "version": "unknown",
            "active_sessions": 0,
            "database": self._database,
            "host": self._host,
            "latency_ms": -1.0,
        }

        try:
            self.ensure_connected()

            # Measure SELECT 1 latency using the inherited helper.
            _, latency_ms = self._measure_latency(self._ping)
            result["latency_ms"] = round(latency_ms, 2)

            # Retrieve server version.
            version_rows: list[dict[str, Any]] = self.execute_query(
                "SELECT @@VERSION AS version_info"
            )
            if version_rows:
                result["version"] = str(
                    version_rows[0].get("version_info", "unknown")
                )

            # Retrieve active session count.
            session_rows: list[dict[str, Any]] = self.execute_query(
                "SELECT COUNT(*) AS session_count "
                "FROM sys.dm_exec_sessions WHERE is_user_process = 1"
            )
            if session_rows:
                result["active_sessions"] = int(
                    session_rows[0].get("session_count", 0)
                )

            result["status"] = "healthy"
            self._logger.info(
                "sqlserver_health_check",
                status="healthy",
                latency_ms=result["latency_ms"],
                active_sessions=result["active_sessions"],
            )

        except Exception as exc:
            result["status"] = "unhealthy"
            self._logger.error(
                "sqlserver_health_check_failed",
                error=str(exc),
                host=self._host,
                database=self._database,
            )

        return result

    def _map_column_type(self, generic_type: str) -> str:
        """Map a generic column type to its SQL Server-native equivalent.

        Args:
            generic_type: One of the keys in ``GENERIC_COLUMN_TYPES``
                (e.g. ``"STRING"``, ``"INTEGER"``, ``"UUID"``).

        Returns:
            The SQL Server type string (e.g. ``"NVARCHAR(4000)"``,
            ``"INT"``, ``"UNIQUEIDENTIFIER"``).

        Raises:
            ValueError: If *generic_type* is not recognised by the SQL
                Server type map.
        """
        normalized: str = generic_type.strip().upper()
        native_type: str | None = _SQLSERVER_TYPE_MAP.get(normalized)
        if native_type is None:
            raise ValueError(
                f"Unsupported generic column type for SQL Server: "
                f"'{generic_type}'. Supported types: "
                f"{', '.join(sorted(_SQLSERVER_TYPE_MAP.keys()))}"
            )
        return native_type

    # ------------------------------------------------------------------
    # SQL Server-specific public methods
    # ------------------------------------------------------------------

    def _create_schema_if_not_exists(self, schema_name: str) -> None:
        """Create a SQL Server schema if it does not already exist.

        Uses dynamic SQL with ``EXEC()`` because ``CREATE SCHEMA`` cannot
        be wrapped in an ``IF`` block directly.

        Args:
            schema_name: Schema name to create.

        Raises:
            Exception: Propagates DDL errors from SQL Server.
        """
        self.ensure_connected()

        safe_schema: str = schema_name.replace("'", "''")
        safe_schema_bracket: str = schema_name.replace("]", "]]")
        ddl_sql: str = (
            f"IF NOT EXISTS (\n"  # noqa: S608
            f"    SELECT * FROM sys.schemas WHERE name = N'{safe_schema}'\n"
            f")\n"
            f"EXEC('CREATE SCHEMA [{safe_schema_bracket}]')"
        )

        conn = self._require_connection()
        cursor = conn.cursor()
        try:
            cursor.execute(ddl_sql)
            conn.commit()
            self._logger.info(
                "sqlserver_schema_ensured",
                schema=schema_name,
            )
        except Exception as exc:
            with contextlib.suppress(Exception):
                conn.rollback()
            self._logger.error(
                "sqlserver_schema_creation_failed",
                schema=schema_name,
                error=str(exc),
            )
            raise
        finally:
            cursor.close()

    def _get_jdbc_url(self) -> str:
        """Build the SQL Server JDBC connection URL.

        Constructs a URL of the form::

            jdbc:sqlserver://{host}:{port};databaseName={database};
            encrypt=true;trustServerCertificate=false

        Additional properties such as ``integratedSecurity``,
        ``columnEncryptionSetting`` (Always Encrypted), and
        ``authentication`` (Azure AD) are appended when configured.

        Returns:
            A fully-formed JDBC URL string.
        """
        url: str = (
            f"jdbc:sqlserver://{self._host}:{self._port}"
            f";databaseName={self._database}"
            f";encrypt={'true' if self._encrypt else 'false'}"
            f";trustServerCertificate="
            f"{'true' if self._trust_server_certificate else 'false'}"
        )

        if self._integrated_security:
            url += ";integratedSecurity=true"

        # Support Always Encrypted when requested.
        always_encrypted: bool = bool(
            self._config.get("always_encrypted", False)
        )
        if always_encrypted:
            url += ";columnEncryptionSetting=Enabled"

        # Support Azure AD authentication mode.
        azure_auth: str | None = self._config.get("azure_ad_authentication")
        if azure_auth:
            url += f";authentication={azure_auth}"

        # Connection / login timeout in seconds.
        url += f";loginTimeout={self._connection_timeout}"

        return url

    # ------------------------------------------------------------------
    # Private helpers
    # ------------------------------------------------------------------

    def _require_connection(self) -> Any:
        """Return the active JDBC connection or raise :class:`ConnectionError`.

        This helper narrows ``self._connection`` from ``Any | None`` to
        ``Any``, preventing repeated ``is None`` checks in every method.

        Returns:
            The active JDBC connection object.

        Raises:
            ConnectionError: If the connector is not connected.
        """
        conn = self._connection
        if conn is None:
            raise ConnectionError(
                "SQLServerConnector is not connected. Call connect() first."
            )
        return conn

    def _establish_jdbc_connection(self) -> Any:
        """Create a raw JDBC connection via ``jaydebeapi.connect()``.

        This helper is designed to be wrapped by
        :meth:`BaseConnector._execute_with_retry` so that transient
        connection failures (e.g. network timeouts, temporary server
        unavailability) are retried with exponential backoff.

        Returns:
            A ``jaydebeapi`` connection object.

        Raises:
            Exception: Propagates any JDBC connection error.
        """
        credentials: list[str] = [self._username, self._password]
        return jaydebeapi.connect(
            self.DRIVER_CLASS,
            self._jdbc_url,
            credentials,
            self._jdbc_jar_path,
        )

    def _ping(self) -> None:
        """Execute ``SELECT 1`` to verify connectivity (used by health_check)."""
        conn = self._require_connection()
        cursor = conn.cursor()
        try:
            cursor.execute("SELECT 1")
            cursor.fetchall()
        finally:
            cursor.close()

    @staticmethod
    def _resolve_jdbc_jar_path(config: dict[str, Any]) -> str:
        """Resolve the JDBC driver JAR file path.

        Resolution order:

        1. ``jdbc_jar_path`` key in *config*.
        2. ``SQLSERVER_JDBC_JAR_PATH`` environment variable.
        3. ``JDBC_DRIVER_PATH`` environment variable joined with a
           default JAR file name (``mssql-jdbc-12.4.2.jre11.jar``).

        Args:
            config: Connector configuration dictionary.

        Returns:
            Resolved filesystem path to the ``mssql-jdbc-*.jar`` file.

        Raises:
            FileNotFoundError: If the resolved path does not exist on disk.
        """
        jar_path: str | None = config.get("jdbc_jar_path")

        if not jar_path:
            jar_path = os.environ.get("SQLSERVER_JDBC_JAR_PATH")

        if not jar_path:
            driver_dir: str = os.environ.get("JDBC_DRIVER_PATH", "/opt/jdbc/drivers")
            jar_path = os.path.join(driver_dir, "mssql-jdbc-12.4.2.jre11.jar")

        if not os.path.exists(jar_path):
            raise FileNotFoundError(
                f"SQL Server JDBC driver JAR not found at '{jar_path}'. "
                f"Set 'jdbc_jar_path' in config, or the "
                f"SQLSERVER_JDBC_JAR_PATH / JDBC_DRIVER_PATH environment "
                f"variable."
            )

        return jar_path
