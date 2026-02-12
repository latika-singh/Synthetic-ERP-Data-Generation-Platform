"""SAP HANA JDBC provisioning connector for the Synthetic ERP Data Generation Platform.

This module implements :class:`HANAConnector`, a concrete database connector that
extends :class:`~provisioning_service.connectors.base.BaseConnector` to provision
synthetic data to SAP HANA databases (version 2.0 SPS 07+).

SAP HANA is an in-memory, column-oriented relational database.  This connector
leverages HANA-specific capabilities including:

* **Column-store table creation** — ``CREATE COLUMN TABLE`` for analytical
  workloads or ``CREATE ROW TABLE`` for OLTP workloads.
* **Batch INSERT optimised for in-memory** — larger default batch sizes (50 000
  rows) that take advantage of HANA's in-memory write throughput.
* **Native CSV import** — ``IMPORT FROM CSV FILE`` for high-speed bulk loading.
* **Schema-based multi-tenant isolation** — each tenant receives its own HANA
  schema with isolated table namespaces.
* **HANA data-type mapping** — ``NVARCHAR``, ``INTEGER``, ``BIGINT``,
  ``DECIMAL``, ``DOUBLE``, ``BOOLEAN``, ``DATE``, ``TIMESTAMP``, ``NCLOB``,
  ``VARBINARY``.

Usage::

    from provisioning_service.connectors.hana_connector import HANAConnector

    config = {
        "host": "hana.example.com",
        "instance_number": "00",
        "database": "HDB",
        "username": "SYSTEM",
        "password": "secret",
        "schema": "TENANT_A",
        "store_type": "COLUMN",
    }

    with HANAConnector(config) as conn:
        conn.create_table("gl_entries", columns=[...], store_type="COLUMN")
        inserted = conn.batch_insert("gl_entries", ["col1", "col2"], data)
        print(f"Inserted {inserted} rows")
"""

from __future__ import annotations

import contextlib
import csv as _csv_module
import logging
import os
import re
from typing import Any

import jaydebeapi

from provisioning_service.connectors.base import BaseConnector


# ---------------------------------------------------------------------------
# Fallback logger for environments where structlog is not configured.
# ---------------------------------------------------------------------------

_fallback_logger: logging.Logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# HANAConnector
# ---------------------------------------------------------------------------


class HANAConnector(BaseConnector):
    """SAP HANA JDBC provisioning connector supporting HANA 2.0 SPS 07+.

    Extends :class:`~provisioning_service.connectors.base.BaseConnector` to
    provide SAP HANA-specific provisioning capabilities including column-store
    table creation, in-memory-optimised batch inserts, native CSV import, and
    schema-based multi-tenant isolation.

    The connector communicates with SAP HANA via JDBC through the
    ``jaydebeapi`` library, using the ``com.sap.db.jdbc.Driver``
    (``ngdbc.jar``) driver.

    Args:
        config: Connection configuration dictionary.  Required keys:

            * ``host`` — HANA server hostname or IP address.
            * ``username`` — Database user name.
            * ``password`` — Database user password.

            Optional keys:

            * ``port`` — HANA SQL port (default derived from
              ``instance_number`` or 30015).
            * ``instance_number`` — HANA instance number used to compute the
              port as ``3<instance_number>15`` (e.g. ``"00"`` → 30015).
            * ``database`` — Database name / tenant database identifier.
            * ``schema`` — Target schema for multi-tenant isolation
              (default ``"SYSTEM"``).
            * ``store_type`` — Default store type for table creation:
              ``"COLUMN"`` (default) or ``"ROW"``.
            * ``jdbc_jar_path`` — Absolute path to ``ngdbc.jar``.  Falls back
              to ``HANA_JDBC_JAR_PATH`` env var, then to
              ``$JDBC_DRIVER_PATH/ngdbc.jar``.
            * ``encrypt`` — Enable TLS encryption (default ``False``).
            * ``validate_certificate`` — Validate server certificate when
              encryption is enabled (default ``True``).
            * ``reconnect`` — Enable automatic reconnection (default ``True``).
            * ``connection_timeout`` — Connection timeout in seconds
              (default 30).

    Raises:
        ValueError: If required configuration keys are missing.

    Example::

        config = {"host": "hana.local", "instance_number": "00",
                  "username": "ADMIN", "password": "pass",
                  "schema": "TENANT_X"}
        with HANAConnector(config) as conn:
            conn.create_table("invoices", columns=[...])
            conn.batch_insert("invoices", ["id", "amount"], rows)
    """

    # ------------------------------------------------------------------
    # Class-level constants
    # ------------------------------------------------------------------

    DRIVER_CLASS: str = "com.sap.db.jdbc.Driver"
    """JDBC driver class name for SAP HANA (ngdbc.jar)."""

    SUPPORTED_VERSIONS: list[str] = ["2.0 SPS 07", "2.0 SPS 08"]
    """SAP HANA versions validated for this connector."""

    DEFAULT_PORT: int = 30015
    """Default SQL port for HANA instance 00."""

    DEFAULT_SCHEMA: str = "SYSTEM"
    """Default schema when none is specified in the config."""

    STORE_TYPES: tuple[str, str] = ("COLUMN", "ROW")
    """Valid HANA store types for table creation."""

    # Mapping from generic column types to HANA-native SQL types.
    # STRING and BINARY map to base types without embedded sizes so that
    # ``_build_column_definition`` can apply ``max_length`` overrides
    # cleanly.  Default lengths (5000) are injected during ``create_table``.
    _COLUMN_TYPE_MAP: dict[str, str] = {
        "STRING": "NVARCHAR",
        "INTEGER": "INTEGER",
        "BIGINT": "BIGINT",
        "FLOAT": "DOUBLE",
        "DECIMAL": "DECIMAL",
        "BOOLEAN": "BOOLEAN",
        "DATE": "DATE",
        "TIMESTAMP": "TIMESTAMP",
        "TEXT": "NCLOB",
        "JSON": "NCLOB",
        "UUID": "NVARCHAR(36)",
        "BINARY": "VARBINARY",
    }

    # Default lengths injected into column specs when max_length is absent.
    _HANA_DEFAULT_LENGTHS: dict[str, int] = {
        "STRING": 5000,
        "BINARY": 5000,
    }

    # HANA-optimised default batch size — the in-memory engine handles
    # larger batches more efficiently than traditional disk-based RDBMSs.
    _HANA_DEFAULT_BATCH_SIZE: int = 50_000

    # ------------------------------------------------------------------
    # Initialisation
    # ------------------------------------------------------------------

    def __init__(self, config: dict[str, Any]) -> None:
        """Initialise the SAP HANA connector from *config*.

        Computes the JDBC port from ``instance_number`` when an explicit
        ``port`` is not provided, validates required configuration, and
        builds the JDBC connection URL.

        Args:
            config: Configuration dictionary (see class docstring).
        """
        # Derive the SQL port from instance number if port is not explicit.
        if "instance_number" in config and "port" not in config:
            instance_str = str(config["instance_number"]).zfill(2)
            config = {**config, "port": int(f"3{instance_str}15")}

        # Apply default port if still absent.
        if "port" not in config:
            config = {**config, "port": self.DEFAULT_PORT}

        # Initialise base class (sets _config, _host, _port, _database,
        # _username, _password, _logger, pool/retry settings, metrics).
        super().__init__(config)

        # Validate that the minimal required keys are present.
        self._validate_config(["host", "username", "password"])

        # --- HANA-specific attributes ---

        self._schema: str = str(config.get("schema", self.DEFAULT_SCHEMA))
        """Target HANA schema for multi-tenant namespace isolation."""

        self._store_type: str = str(
            config.get("store_type", "COLUMN")
        ).upper()
        if self._store_type not in self.STORE_TYPES:
            self._store_type = "COLUMN"

        # Resolve JDBC driver JAR path.
        self._jar_path: str = str(
            config.get(
                "jdbc_jar_path",
                os.environ.get(
                    "HANA_JDBC_JAR_PATH",
                    os.path.join(
                        os.environ.get("JDBC_DRIVER_PATH", "/opt/jdbc/drivers"),
                        "ngdbc.jar",
                    ),
                ),
            )
        )

        # TLS / encryption settings.
        self._encrypt: bool = bool(config.get("encrypt", False))
        self._validate_certificate: bool = bool(
            config.get("validate_certificate", True)
        )
        self._reconnect: bool = bool(config.get("reconnect", True))

        # Build the JDBC connection URL.
        self._jdbc_url: str = self._get_jdbc_url()

        self._logger.info(
            "hana_connector_initialised",
            host=self._host,
            port=self._port,
            schema=self._schema,
            store_type=self._store_type,
            encrypt=self._encrypt,
        )

    # ------------------------------------------------------------------
    # Abstract method implementations — Connection lifecycle
    # ------------------------------------------------------------------

    def connect(self) -> None:
        """Establish a JDBC connection to SAP HANA.

        Uses ``jaydebeapi.connect()`` with the SAP HANA JDBC driver class,
        URL, credentials, and driver JAR path.  After connection, sets the
        current schema and disables auto-commit for explicit transaction
        management.

        Raises:
            ConnectionError: If the JDBC JAR is missing or the connection
                cannot be established.
        """
        if self._connected:
            self._logger.debug("hana_already_connected", host=self._host)
            return

        # Pre-flight check: verify the JDBC driver JAR exists on disk.
        if not os.path.exists(self._jar_path):
            raise ConnectionError(
                f"SAP HANA JDBC driver JAR not found at: {self._jar_path}. "
                "Set the HANA_JDBC_JAR_PATH environment variable or provide "
                "'jdbc_jar_path' in the config dictionary."
            )

        try:
            def _establish() -> Any:
                return jaydebeapi.connect(
                    self.DRIVER_CLASS,
                    self._jdbc_url,
                    [self._username, self._password],
                    self._jar_path,
                )

            connection = self._execute_with_retry(_establish)
            self._connection = connection

            # Disable auto-commit for explicit transaction control.
            with contextlib.suppress(AttributeError):
                connection.jconn.setAutoCommit(False)

            # Set current schema for tenant isolation.
            cursor = connection.cursor()
            try:
                cursor.execute(f'SET SCHEMA "{self._schema}"')
            finally:
                cursor.close()

            self._connected = True

            self._logger.info(
                "hana_connected",
                host=self._host,
                port=self._port,
                schema=self._schema,
                jdbc_url=self._jdbc_url,
            )

        except ConnectionError:
            raise
        except Exception as exc:
            self._connected = False
            self._connection = None
            error_code = self._extract_hana_error_code(exc)
            self._logger.error(
                "hana_connection_failed",
                host=self._host,
                port=self._port,
                error=str(exc),
                hana_error_code=error_code,
            )
            raise ConnectionError(
                f"Failed to connect to SAP HANA at "
                f"{self._host}:{self._port}: [{error_code}] {exc}"
            ) from exc

    def disconnect(self) -> None:
        """Close the JDBC connection to SAP HANA and release resources.

        Sets ``_connected`` to ``False`` and ``_connection`` to ``None``
        regardless of whether the close operation succeeds.
        """
        if self._connection is not None:
            try:
                self._connection.close()
                self._logger.info(
                    "hana_disconnected",
                    host=self._host,
                    port=self._port,
                    schema=self._schema,
                )
            except Exception as exc:
                self._logger.warning(
                    "hana_disconnect_warning",
                    host=self._host,
                    port=self._port,
                    error=str(exc),
                )
            finally:
                self._connection = None
                self._connected = False

    # ------------------------------------------------------------------
    # Abstract method implementations — DDL operations
    # ------------------------------------------------------------------

    def create_table(
        self,
        table_name: str,
        columns: list[dict[str, Any]],
        if_not_exists: bool = True,
        store_type: str = "COLUMN",
    ) -> None:
        """Create a HANA column-store or row-store table.

        Builds a ``CREATE COLUMN TABLE`` (or ``CREATE ROW TABLE``) statement
        with HANA-specific double-quoted identifiers.  Column definitions are
        built via the inherited :meth:`_build_column_definition` helper with
        HANA-specific pre-processing (default ``NVARCHAR(5000)`` lengths,
        ``NCLOB`` without sizes, double-quoted names).

        When *if_not_exists* is ``True`` the connector catches HANA error
        code **288** (duplicate table name) and silently ignores it, since
        HANA does not support ``IF NOT EXISTS`` natively for all versions.

        Args:
            table_name: Unqualified table name (schema is prepended
                automatically).
            columns: Column specification dicts.  Keys: ``name``, ``type``
                (from :data:`GENERIC_COLUMN_TYPES`), ``nullable``,
                ``primary_key``, ``default``, ``unique``, ``max_length``,
                ``precision``, ``scale``.
            if_not_exists: Suppress errors when the table already exists.
            store_type: ``"COLUMN"`` (default, analytical) or ``"ROW"``
                (OLTP).

        Raises:
            ValueError: If *store_type* is invalid.
            RuntimeError: If the connector is not connected.
        """
        self.ensure_connected()

        effective_store = store_type.upper()
        if effective_store not in self.STORE_TYPES:
            raise ValueError(
                f"Invalid store_type '{store_type}'. "
                f"Must be one of: {self.STORE_TYPES}"
            )

        # Build column definition fragments with HANA adaptations.
        col_defs: list[str] = []
        for col in columns:
            # Create a shallow copy so we can inject HANA defaults without
            # mutating the caller's data.
            col_copy: dict[str, Any] = dict(col)
            col_type_upper: str = col_copy.get("type", "").upper()

            # Inject default max_length for variable-length types.
            if (
                col_type_upper in self._HANA_DEFAULT_LENGTHS
                and "max_length" not in col_copy
            ):
                col_copy["max_length"] = self._HANA_DEFAULT_LENGTHS[col_type_upper]

            # LOB types (NCLOB) do not accept a size parameter in HANA.
            if col_type_upper in ("TEXT", "JSON"):
                col_copy.pop("max_length", None)

            # Use the base class column-definition builder then replace
            # the unquoted column name with a double-quoted HANA identifier.
            base_def: str = self._build_column_definition(col_copy)
            col_name: str = col_copy.get("name", "")
            if base_def.startswith(col_name):
                base_def = f'"{col_name}"' + base_def[len(col_name):]
            col_defs.append(base_def)

        col_definitions_sql: str = ",\n    ".join(col_defs)
        qualified_name: str = f'"{self._schema}"."{table_name}"'

        create_sql: str = (
            f"CREATE {effective_store} TABLE {qualified_name} (\n"
            f"    {col_definitions_sql}\n"
            f")"
        )

        conn = self._require_connection()
        cursor = conn.cursor()
        try:
            cursor.execute(create_sql)
            conn.commit()

            self._logger.info(
                "hana_table_created",
                table_name=table_name,
                schema=self._schema,
                store_type=effective_store,
                column_count=len(columns),
                if_not_exists=if_not_exists,
            )
        except Exception as exc:
            error_code = self._extract_hana_error_code(exc)
            # HANA error 288 = "cannot use duplicate table name".
            if if_not_exists and error_code == "288":
                self._logger.debug(
                    "hana_table_already_exists",
                    table_name=table_name,
                    schema=self._schema,
                )
                with contextlib.suppress(Exception):
                    conn.rollback()
                return

            with contextlib.suppress(Exception):
                conn.rollback()

            self._logger.error(
                "hana_create_table_failed",
                table_name=table_name,
                schema=self._schema,
                error=str(exc),
                hana_error_code=error_code,
            )
            raise
        finally:
            cursor.close()

    # ------------------------------------------------------------------
    # Abstract method implementations — DML operations
    # ------------------------------------------------------------------

    def batch_insert(
        self,
        table_name: str,
        columns: list[str],
        data: list[tuple],
        batch_size: int = 10000,
    ) -> int:
        """Insert rows into a HANA table using batched ``executemany`` calls.

        Builds an ``INSERT INTO`` statement with HANA double-quoted
        identifiers and ``?`` bind-parameter placeholders.  Data is split
        into batches; each batch is committed independently so that
        previously-committed batches survive a mid-stream failure.

        The effective batch size is automatically raised to
        :attr:`_HANA_DEFAULT_BATCH_SIZE` (50 000) when the caller passes the
        generic default of 10 000, since HANA's in-memory architecture
        benefits from larger batches.

        Args:
            table_name: Target table name.
            columns: Ordered column names matching tuple positions in *data*.
            data: Row tuples to insert.
            batch_size: Rows per transactional batch (default 10 000; auto-
                promoted to 50 000 for HANA).

        Returns:
            Total number of rows successfully inserted.

        Raises:
            RuntimeError: If the connector is not connected.
        """
        self.ensure_connected()

        if not data:
            return 0

        # Promote generic default to HANA-optimised batch size.
        effective_batch: int = (
            self._HANA_DEFAULT_BATCH_SIZE
            if batch_size == 10_000
            else batch_size
        )

        # Build INSERT with HANA double-quoted identifiers.
        quoted_cols: str = ", ".join(f'"{c}"' for c in columns)
        placeholders: str = ", ".join("?" for _ in columns)
        qualified: str = f'"{self._schema}"."{table_name}"'
        insert_sql: str = (
            f"INSERT INTO {qualified} ({quoted_cols}) VALUES ({placeholders})"  # noqa: S608
        )

        total_inserted: int = 0
        total_rows: int = len(data)

        conn = self._require_connection()
        cursor = conn.cursor()
        try:
            for offset in range(0, total_rows, effective_batch):
                batch = data[offset : offset + effective_batch]
                batch_num: int = (offset // effective_batch) + 1

                try:
                    cursor.executemany(insert_sql, batch)
                    conn.commit()
                    total_inserted += len(batch)

                    self._logger.debug(
                        "hana_batch_inserted",
                        table_name=table_name,
                        batch_number=batch_num,
                        batch_rows=len(batch),
                        total_inserted=total_inserted,
                        total_rows=total_rows,
                        progress_pct=round(
                            (total_inserted / total_rows) * 100, 1
                        ),
                    )
                except Exception as exc:
                    with contextlib.suppress(Exception):
                        conn.rollback()
                    error_code = self._extract_hana_error_code(exc)
                    self._logger.error(
                        "hana_batch_insert_failed",
                        table_name=table_name,
                        batch_number=batch_num,
                        batch_size=len(batch),
                        total_inserted_so_far=total_inserted,
                        error=str(exc),
                        hana_error_code=error_code,
                    )
                    raise

            self._total_rows_inserted += total_inserted

            self._logger.info(
                "hana_batch_insert_complete",
                table_name=table_name,
                total_inserted=total_inserted,
                total_rows=total_rows,
                effective_batch_size=effective_batch,
                schema=self._schema,
            )

            return total_inserted
        finally:
            cursor.close()

    # ------------------------------------------------------------------
    # HANA-specific public methods
    # ------------------------------------------------------------------

    def import_from_csv(
        self,
        table_name: str,
        csv_path: str,
        columns: list[str] | None = None,
    ) -> int:
        """Bulk-load a CSV file into a HANA table via ``IMPORT FROM CSV FILE``.

        HANA's native CSV import is significantly faster than row-by-row
        inserts for large datasets.  If the native ``IMPORT`` statement fails
        (e.g. because the HANA server cannot access the file path), the
        method falls back to a Python-side CSV parse followed by
        :meth:`batch_insert`.

        Args:
            table_name: Target table name.
            csv_path: Absolute path to the CSV file **on the HANA server**
                for native import, or on the local filesystem for fallback
                mode.
            columns: Optional list of column names for column mapping.
                Defaults to all columns in table order.

        Returns:
            Total number of rows imported.

        Raises:
            FileNotFoundError: If *csv_path* does not exist on the local
                filesystem (fallback check only).
            RuntimeError: If the connector is not connected.
        """
        self.ensure_connected()

        qualified: str = f'"{self._schema}"."{table_name}"'

        conn = self._require_connection()
        cursor = conn.cursor()
        try:
            # Attempt HANA native CSV import.
            col_clause: str = ""
            if columns:
                col_clause = (
                    " (" + ", ".join(f'"{c}"' for c in columns) + ")"
                )

            import_sql: str = (
                f"IMPORT FROM CSV FILE '{csv_path}' "
                f"INTO {qualified}{col_clause} "
                f"WITH RECORD DELIMITED BY '\\n' "
                f"FIELD DELIMITED BY ',' "
                f"OPTIONALLY ENCLOSED BY '\"' "
                f"SKIP FIRST 1 ROW "
                f"FAIL ON INVALID DATA"
            )

            cursor.execute(import_sql)
            conn.commit()

            # Retrieve the count of imported rows.
            count_cursor = conn.cursor()
            try:
                count_cursor.execute(
                    f"SELECT COUNT(*) FROM {qualified}"  # noqa: S608
                )
                row = count_cursor.fetchone()
                row_count: int = int(row[0]) if row else 0
            finally:
                count_cursor.close()

            self._total_rows_inserted += row_count

            self._logger.info(
                "hana_csv_import_complete",
                table_name=table_name,
                csv_path=csv_path,
                rows_imported=row_count,
                schema=self._schema,
            )
            return row_count

        except Exception as exc:
            self._logger.warning(
                "hana_csv_import_failed_falling_back",
                table_name=table_name,
                csv_path=csv_path,
                error=str(exc),
            )
            with contextlib.suppress(Exception):
                conn.rollback()

            return self._csv_fallback_insert(table_name, csv_path, columns)
        finally:
            cursor.close()

    # ------------------------------------------------------------------
    # Abstract method implementations — Query execution
    # ------------------------------------------------------------------

    def execute_query(
        self,
        query: str,
        params: tuple | None = None,
    ) -> list[dict[str, Any]]:
        """Execute an arbitrary SQL query against SAP HANA.

        For ``SELECT`` statements the method returns a list of row
        dictionaries keyed by column name.  ``NCLOB`` column values are
        fully read into Python strings.  For DML/DDL statements an empty
        list is returned and the transaction is committed.

        Args:
            query: SQL query string with ``?`` placeholders for parameters.
            params: Optional bind-parameter tuple.

        Returns:
            List of row dicts for ``SELECT`` queries; empty list otherwise.

        Raises:
            RuntimeError: If the connector is not connected.
        """
        self.ensure_connected()

        conn = self._require_connection()
        cursor = conn.cursor()
        try:
            def _run() -> list[Any] | None:
                if params:
                    cursor.execute(query, params)
                else:
                    cursor.execute(query)
                if cursor.description is not None:
                    rows: list[Any] = cursor.fetchall()
                    return rows
                return None

            result, latency_ms = self._measure_latency(_run)
            self._total_queries_executed += 1

            if result is None:
                # Non-SELECT (DML / DDL) — commit and return empty.
                with contextlib.suppress(Exception):
                    conn.commit()
                self._logger.debug(
                    "hana_query_executed",
                    query_type="DML/DDL",
                    latency_ms=round(latency_ms, 2),
                )
                return []

            # Convert row tuples to dicts using column descriptions.
            col_names: list[str] = [desc[0] for desc in cursor.description]
            rows: list[dict[str, Any]] = []
            for row_tuple in result:
                row_dict: dict[str, Any] = {}
                for idx, cell in enumerate(row_tuple):
                    # NCLOB values may be returned as LOB handles —
                    # materialise the full text content.
                    resolved = cell.read() if hasattr(cell, "read") else cell
                    row_dict[col_names[idx]] = resolved
                rows.append(row_dict)

            self._logger.debug(
                "hana_query_executed",
                query_type="SELECT",
                row_count=len(rows),
                latency_ms=round(latency_ms, 2),
            )
            return rows

        except Exception as exc:
            error_code = self._extract_hana_error_code(exc)
            self._logger.error(
                "hana_query_failed",
                query_preview=query[:200],
                error=str(exc),
                hana_error_code=error_code,
            )
            raise
        finally:
            cursor.close()

    # ------------------------------------------------------------------
    # Abstract method implementations — Health monitoring
    # ------------------------------------------------------------------

    def health_check(self) -> dict[str, Any]:
        """Check SAP HANA connectivity and retrieve system diagnostics.

        Executes ``SELECT 1 FROM DUMMY`` (HANA's equivalent of
        ``SELECT 1 FROM DUAL``) to verify basic connectivity, then queries
        HANA monitoring views for version, SPS level, active connections, and
        memory utilisation.

        Returns:
            Dictionary with keys:

            * ``status`` — ``"healthy"`` or ``"unhealthy"``.
            * ``version`` — HANA database version string.
            * ``sps_level`` — Support Package Stack level.
            * ``active_connections`` — Current active connection count.
            * ``memory_usage_pct`` — Physical memory utilisation percentage.
            * ``host`` — Database host.
            * ``latency_ms`` — Round-trip probe latency in milliseconds.
        """
        health: dict[str, Any] = {
            "status": "unhealthy",
            "host": self._host,
            "port": self._port,
            "schema": self._schema,
            "version": "unknown",
            "sps_level": "unknown",
            "active_connections": 0,
            "memory_usage_pct": 0.0,
            "latency_ms": 0.0,
        }

        try:
            self.ensure_connected()

            # Connectivity probe with latency measurement.
            conn = self._require_connection()

            def _ping() -> None:
                cur = conn.cursor()
                try:
                    cur.execute("SELECT 1 FROM DUMMY")
                    cur.fetchone()
                finally:
                    cur.close()

            _, latency_ms = self._measure_latency(_ping)
            health["latency_ms"] = round(latency_ms, 2)

            # HANA version and SPS level from M_DATABASE.
            health.update(self._query_version_info())

            # Active connections from M_CONNECTIONS.
            health["active_connections"] = self._query_active_connections()

            # Memory utilisation from M_HOST_RESOURCE_UTILIZATION.
            health["memory_usage_pct"] = self._query_memory_usage()

            health["status"] = "healthy"

            self._logger.info(
                "hana_health_check",
                status="healthy",
                version=health["version"],
                sps_level=health["sps_level"],
                active_connections=health["active_connections"],
                memory_usage_pct=health["memory_usage_pct"],
                latency_ms=health["latency_ms"],
            )

        except Exception as exc:
            health["status"] = "unhealthy"
            health["error"] = str(exc)
            self._logger.error(
                "hana_health_check_failed",
                host=self._host,
                error=str(exc),
            )

        return health

    # ------------------------------------------------------------------
    # Abstract method implementations — Type mapping
    # ------------------------------------------------------------------

    def _map_column_type(self, generic_type: str) -> str:
        """Map a generic column type to its SAP HANA native SQL type.

        The full end-to-end mapping (including default sizes applied by
        :meth:`create_table`) is:

        +-----------+------------------+
        | Generic   | HANA Type        |
        +===========+==================+
        | STRING    | NVARCHAR(5000)   |
        | INTEGER   | INTEGER          |
        | BIGINT    | BIGINT           |
        | FLOAT     | DOUBLE           |
        | DECIMAL   | DECIMAL(p, s)    |
        | BOOLEAN   | BOOLEAN          |
        | DATE      | DATE             |
        | TIMESTAMP | TIMESTAMP        |
        | TEXT      | NCLOB            |
        | JSON      | NCLOB            |
        | UUID      | NVARCHAR(36)     |
        | BINARY    | VARBINARY(5000)  |
        +-----------+------------------+

        Args:
            generic_type: One of the keys in
                :data:`~provisioning_service.connectors.base.GENERIC_COLUMN_TYPES`.

        Returns:
            The HANA-native SQL type string.

        Raises:
            ValueError: If *generic_type* is not recognised.
        """
        normalised: str = generic_type.upper().strip()
        hana_type: str | None = self._COLUMN_TYPE_MAP.get(normalised)

        if hana_type is None:
            raise ValueError(
                f"Unsupported column type '{generic_type}' for SAP HANA. "
                f"Supported types: {sorted(self._COLUMN_TYPE_MAP.keys())}"
            )
        return hana_type

    # ------------------------------------------------------------------
    # HANA-specific protected helpers
    # ------------------------------------------------------------------

    def _create_schema_if_not_exists(self, schema_name: str) -> None:
        """Create a HANA schema if it does not already exist.

        Used for multi-tenant isolation — each tenant receives its own
        schema namespace.  If the schema already exists (HANA error **386**),
        the error is silently absorbed.

        Args:
            schema_name: Schema to create.

        Raises:
            RuntimeError: If the connector is not connected.
        """
        self.ensure_connected()

        conn = self._require_connection()
        cursor = conn.cursor()
        try:
            cursor.execute(f'CREATE SCHEMA "{schema_name}"')
            conn.commit()

            self._logger.info(
                "hana_schema_created",
                schema_name=schema_name,
            )
        except Exception as exc:
            error_code = self._extract_hana_error_code(exc)
            # Error 386: "cannot use duplicate schema name"
            if error_code == "386":
                self._logger.debug(
                    "hana_schema_already_exists",
                    schema_name=schema_name,
                )
                with contextlib.suppress(Exception):
                    conn.rollback()
            else:
                with contextlib.suppress(Exception):
                    conn.rollback()
                self._logger.error(
                    "hana_schema_creation_failed",
                    schema_name=schema_name,
                    error=str(exc),
                    hana_error_code=error_code,
                )
                raise
        finally:
            cursor.close()

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
                "HANAConnector is not connected. Call connect() first."
            )
        return conn

    def _get_jdbc_url(self) -> str:
        """Build the SAP HANA JDBC connection URL.

        Constructs a URL of the form::

            jdbc:sap://<host>:<port>/?currentSchema=<schema>&encrypt=true&...

        TLS encryption, certificate validation, automatic reconnection, and
        communication timeout are configured via optional URL parameters.

        Returns:
            A fully-formed HANA JDBC URL string.
        """
        base_url: str = (
            f"jdbc:sap://{self._host}:{self._port}"
            f"/?currentSchema={self._schema}"
        )

        params: list[str] = []

        if self._encrypt:
            params.append("encrypt=true")
            if self._validate_certificate:
                params.append("validateCertificate=true")
            else:
                params.append("validateCertificate=false")

        if self._reconnect:
            params.append("reconnect=true")

        # Communication timeout in milliseconds.
        timeout_s: int = self._config.get("connection_timeout", 30)
        params.append(f"communicationTimeout={timeout_s * 1000}")

        if params:
            base_url += "&" + "&".join(params)

        return base_url

    # ------------------------------------------------------------------
    # Private helper methods
    # ------------------------------------------------------------------

    def _query_version_info(self) -> dict[str, str]:
        """Query M_DATABASE for HANA version and SPS level.

        Returns:
            Dict with ``version`` and ``sps_level`` keys.
        """
        info: dict[str, str] = {"version": "unknown", "sps_level": "unknown"}
        try:
            conn = self._require_connection()
            cursor = conn.cursor()
            try:
                cursor.execute("SELECT VERSION FROM M_DATABASE")
                row = cursor.fetchone()
                if row and row[0]:
                    info["version"] = str(row[0])
                    # Parse SPS level from version string.
                    # Typical format: "2.00.070.00.xxxx" → SPS 07
                    parts = str(row[0]).split(".")
                    if len(parts) >= 3:
                        try:
                            sps_num = int(parts[2][:2])
                            info["sps_level"] = f"SPS {sps_num:02d}"
                        except (ValueError, IndexError):
                            pass
            finally:
                cursor.close()
        except Exception:
            self._logger.debug("Failed to query HANA version info")
        return info

    def _query_active_connections(self) -> int:
        """Query M_CONNECTIONS for the number of active connections.

        Returns:
            Active connection count, or ``0`` on error.
        """
        try:
            conn = self._require_connection()
            cursor = conn.cursor()
            try:
                cursor.execute(
                    "SELECT COUNT(*) FROM M_CONNECTIONS "
                    "WHERE CONNECTION_STATUS = 'RUNNING'"
                )
                row = cursor.fetchone()
                return int(row[0]) if row and row[0] is not None else 0
            finally:
                cursor.close()
        except Exception:
            return 0

    def _query_memory_usage(self) -> float:
        """Query M_HOST_RESOURCE_UTILIZATION for memory utilisation %.

        Returns:
            Memory usage percentage (0.0-100.0), or ``0.0`` on error.
        """
        try:
            conn = self._require_connection()
            cursor = conn.cursor()
            try:
                cursor.execute(
                    "SELECT USED_PHYSICAL_MEMORY, FREE_PHYSICAL_MEMORY "
                    "FROM M_HOST_RESOURCE_UTILIZATION"
                )
                row = cursor.fetchone()
                if row and row[0] is not None and row[1] is not None:
                    used: float = float(row[0])
                    free: float = float(row[1])
                    total: float = used + free
                    if total > 0:
                        return round((used / total) * 100.0, 2)
            finally:
                cursor.close()
        except Exception:
            self._logger.debug("Failed to query HANA memory usage")
        return 0.0

    def _csv_fallback_insert(
        self,
        table_name: str,
        csv_path: str,
        columns: list[str] | None = None,
    ) -> int:
        """Fall back to batch INSERT when native CSV import fails.

        Reads the CSV file locally, parses it with Python's ``csv`` module,
        and delegates to :meth:`batch_insert`.

        Args:
            table_name: Target table name.
            csv_path: Local filesystem path to the CSV file.
            columns: Optional column name list.  Defaults to the CSV header
                row.

        Returns:
            Total number of rows inserted.

        Raises:
            FileNotFoundError: If the CSV file does not exist locally.
        """
        if not os.path.exists(csv_path):
            raise FileNotFoundError(
                f"CSV file not found for fallback insert: {csv_path}"
            )

        self._logger.info(
            "hana_csv_fallback_start",
            table_name=table_name,
            csv_path=csv_path,
        )

        with open(csv_path, encoding="utf-8") as fh:
            reader = _csv_module.reader(fh)
            header: list[str] = next(reader, [])
            effective_columns: list[str] = columns if columns else header
            data: list[tuple] = [tuple(row) for row in reader]

        if not data:
            self._logger.info(
                "hana_csv_fallback_empty",
                table_name=table_name,
                csv_path=csv_path,
            )
            return 0

        return self.batch_insert(table_name, effective_columns, data)

    @staticmethod
    def _extract_hana_error_code(exc: Exception) -> str:
        """Extract a numeric HANA error code from an exception message.

        SAP HANA errors typically include patterns like
        ``[HANA-288]``, ``error code 288``, or ``[288]``.

        Args:
            exc: The exception whose message should be parsed.

        Returns:
            The numeric error code as a string, or ``"unknown"`` if no code
            can be extracted.
        """
        error_str: str = str(exc)

        # Pattern 1: "error code NNN" or "HANA-NNN"
        match = re.search(
            r"(?:error\s+code\s*[:\s]?\s*|HANA[- ])(\d+)",
            error_str,
            re.IGNORECASE,
        )
        if match:
            return match.group(1)

        # Pattern 2: "[NNN]" (bracketed numeric code)
        match = re.search(r"\[(\d+)\]", error_str)
        if match:
            return match.group(1)

        return "unknown"
