"""Abstract base database connector for the Provisioning Service.

This module defines :class:`BaseConnector`, the abstract base class that
all JDBC-based database provisioning connectors must extend.  Four concrete
implementations exist — PostgreSQL, Oracle, SQL Server, and SAP HANA — and
each one inherits from ``BaseConnector`` to guarantee a uniform interface for
connection management, schema operations, batch data insertion, and health
monitoring.

The module also exports :data:`GENERIC_COLUMN_TYPES`, a vocabulary of
database-agnostic column type names that concrete connectors map to their
native SQL dialects via the :meth:`BaseConnector._map_column_type` abstract
method.

Design Patterns:
    * **Abstract Factory** — ``BaseConnector`` cannot be instantiated directly;
      concrete connectors implement every abstract method.
    * **Strategy** — The provisioning layer selects the appropriate connector
      at runtime based on target database type, swapping implementations
      transparently.
    * **Context Manager** — ``BaseConnector`` implements ``__enter__`` /
      ``__exit__`` for deterministic resource cleanup.

Usage::

    from provisioning_service.connectors.postgresql_connector import PostgreSQLConnector

    config = {
        "host": "db.example.com",
        "port": 5432,
        "database": "erp_target",
        "username": "admin",
        "password": "secret",
    }

    with PostgreSQLConnector(config) as conn:
        conn.create_table("gl_entries", columns=[...])
        inserted = conn.batch_insert("gl_entries", columns=[...], data=[...])
        print(f"Inserted {inserted} rows")
"""

from __future__ import annotations

import logging
import time
from abc import ABC, abstractmethod
from typing import Any, Callable, Dict, List, Optional, Tuple

from shared.logging.structured_logger import get_logger

# ---------------------------------------------------------------------------
# Generic Column Type Vocabulary
# ---------------------------------------------------------------------------

GENERIC_COLUMN_TYPES: Dict[str, str] = {
    "STRING": "STRING",
    "INTEGER": "INTEGER",
    "BIGINT": "BIGINT",
    "FLOAT": "FLOAT",
    "DECIMAL": "DECIMAL",
    "BOOLEAN": "BOOLEAN",
    "DATE": "DATE",
    "TIMESTAMP": "TIMESTAMP",
    "TEXT": "TEXT",
    "JSON": "JSON",
    "UUID": "UUID",
    "BINARY": "BINARY",
}
"""Database-agnostic column type names.

Each concrete connector translates these tokens to native SQL types via its
:meth:`_map_column_type` implementation.  For example, a PostgreSQL connector
maps ``"STRING"`` to ``VARCHAR`` while an Oracle connector maps it to
``VARCHAR2``.

Supported Types:
    STRING: Variable-length character data (default up to 255 characters).
    INTEGER: 32-bit signed integer.
    BIGINT: 64-bit signed integer.
    FLOAT: Double-precision floating-point number.
    DECIMAL: Exact numeric with configurable precision and scale.
    BOOLEAN: Logical true/false value.
    DATE: Calendar date without time component.
    TIMESTAMP: Date and time with timezone awareness.
    TEXT: Unbounded character large object.
    JSON: JSON document storage.
    UUID: Universally unique identifier (128-bit).
    BINARY: Variable-length binary data.
"""

# Fallback standard logger for environments where structlog is not configured.
_fallback_logger: logging.Logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# BaseConnector Abstract Class
# ---------------------------------------------------------------------------


class BaseConnector(ABC):
    """Abstract base class for all JDBC database provisioning connectors.

    ``BaseConnector`` defines the contract that every concrete database
    connector must fulfil.  It provides:

    * **Abstract methods** — ``connect``, ``disconnect``, ``create_table``,
      ``batch_insert``, ``execute_query``, ``health_check``, and
      ``_map_column_type`` that subclasses *must* implement.
    * **Shared utilities** — retry logic with exponential backoff
      (``_execute_with_retry``), latency measurement (``_measure_latency``),
      SQL column-definition building (``_build_column_definition``),
      configuration validation (``_validate_config``), and metrics
      collection (``get_metrics``).
    * **Lifecycle management** — context-manager protocol for deterministic
      connection open/close, plus ``ensure_connected`` for lazy connections.

    Args:
        config: Dictionary containing at minimum ``host``, ``port``,
            ``database``, ``username``, and ``password`` keys.  Optional
            keys include ``pool_min_size``, ``pool_max_size``,
            ``connection_timeout``, ``query_timeout``, ``max_retries``,
            and ``retry_delay``.

    Raises:
        TypeError: If instantiated directly (abstract class).

    Example::

        class PostgreSQLConnector(BaseConnector):
            def connect(self) -> None: ...
            # ... implement remaining abstract methods ...

        with PostgreSQLConnector(config) as conn:
            conn.create_table("invoices", columns)
            conn.batch_insert("invoices", column_names, rows)
    """

    # ------------------------------------------------------------------
    # Initialisation
    # ------------------------------------------------------------------

    def __init__(self, config: Dict[str, Any]) -> None:
        """Initialise the base connector with connection and pool settings.

        Args:
            config: Configuration dictionary.  Required keys are ``host``,
                ``port``, ``database``, ``username``, ``password``.
                Optional pool/retry keys with their defaults:

                * ``pool_min_size`` — Minimum connections in pool (default 1).
                * ``pool_max_size`` — Maximum connections in pool (default 10).
                * ``connection_timeout`` — Timeout in seconds for establishing
                  a new connection (default 30).
                * ``query_timeout`` — Timeout in seconds for query execution
                  (default 60).
                * ``max_retries`` — Number of retry attempts for transient
                  failures (default 3).
                * ``retry_delay`` — Base delay in seconds between retries;
                  increases exponentially (default 2.0).
        """
        # Store the full configuration for subclass access.
        self._config: Dict[str, Any] = config

        # Core connection parameters.
        self._host: str = str(config.get("host", "localhost"))
        self._port: int = int(config.get("port", 0))
        self._database: str = str(config.get("database", ""))
        self._username: str = str(config.get("username", ""))
        self._password: str = str(config.get("password", ""))

        # Structured logger scoped to the concrete connector class name.
        try:
            self._logger = get_logger(self.__class__.__name__)
        except Exception:
            # Fallback to standard logging if structlog is not initialised.
            self._logger = _fallback_logger  # type: ignore[assignment]

        # Connection state tracking.
        self._connected: bool = False
        self._connection: Optional[Any] = None

        # Connection pool settings.
        self._pool_min_size: int = int(config.get("pool_min_size", 1))
        self._pool_max_size: int = int(config.get("pool_max_size", 10))
        self._connection_timeout: int = int(config.get("connection_timeout", 30))
        self._query_timeout: int = int(config.get("query_timeout", 60))

        # Retry / resilience settings.
        self._max_retries: int = int(config.get("max_retries", 3))
        self._retry_delay: float = float(config.get("retry_delay", 2.0))

        # Operational metrics.
        self._total_rows_inserted: int = 0
        self._total_queries_executed: int = 0

    # ------------------------------------------------------------------
    # Abstract Methods — must be implemented by concrete connectors
    # ------------------------------------------------------------------

    @abstractmethod
    def connect(self) -> None:
        """Establish a connection (or connection pool) to the target database.

        Implementations **must** set ``self._connected = True`` upon
        successful connection and ``self._connection`` to the underlying
        driver-level connection object.

        Raises:
            ConnectionError: If the connection cannot be established after
                exhausting retry attempts or due to invalid credentials.
        """

    @abstractmethod
    def disconnect(self) -> None:
        """Close the database connection and release pooled resources.

        Implementations **must** set ``self._connected = False`` and
        ``self._connection = None`` after teardown completes.
        """

    @abstractmethod
    def create_table(
        self,
        table_name: str,
        columns: List[Dict[str, Any]],
        if_not_exists: bool = True,
    ) -> None:
        """Create a table in the target database.

        Args:
            table_name: Fully qualified or simple table name.
            columns: List of column specification dicts.  Each dict may
                contain the following keys:

                * ``name`` (str, required) — Column name.
                * ``type`` (str, required) — One of :data:`GENERIC_COLUMN_TYPES`.
                * ``nullable`` (bool) — ``True`` if the column allows NULLs
                  (default ``True``).
                * ``primary_key`` (bool) — ``True`` to mark as primary key
                  (default ``False``).
                * ``default`` (Any) — Default value expression.
                * ``unique`` (bool) — ``True`` to add a UNIQUE constraint
                  (default ``False``).
                * ``max_length`` (int) — Maximum length for STRING/TEXT types.
                * ``precision`` (int) — Numeric precision for DECIMAL.
                * ``scale`` (int) — Numeric scale for DECIMAL.

            if_not_exists: When ``True``, emit ``IF NOT EXISTS`` to avoid
                errors when the table already exists.

        Raises:
            RuntimeError: If the connector is not connected.
            Exception: Propagates database-specific DDL errors.
        """

    @abstractmethod
    def batch_insert(
        self,
        table_name: str,
        columns: List[str],
        data: List[Tuple],
        batch_size: int = 10000,
    ) -> int:
        """Insert rows into a table using batched transactions.

        Each batch of *batch_size* rows is committed in a separate
        transaction.  On failure within a batch, that batch is rolled back
        while previously committed batches remain durable.

        Args:
            table_name: Target table name.
            columns: Ordered list of column names corresponding to the
                tuple elements in *data*.
            data: List of row tuples to insert.
            batch_size: Number of rows per transactional batch
                (default 10 000).

        Returns:
            Total number of rows successfully inserted across all batches.

        Raises:
            RuntimeError: If the connector is not connected.
            Exception: Propagates database-specific insert errors.
        """

    @abstractmethod
    def execute_query(
        self,
        query: str,
        params: Optional[Tuple] = None,
    ) -> List[Dict[str, Any]]:
        """Execute an arbitrary SQL query.

        For ``SELECT`` statements the method returns a list of row
        dictionaries keyed by column name.  For DML/DDL statements an
        empty list is returned.

        Args:
            query: SQL query string.  Use ``%s`` or ``?`` placeholders
                (driver-dependent) for parameterised queries.
            params: Optional tuple of bind parameters.

        Returns:
            List of row dicts for ``SELECT`` queries; empty list otherwise.

        Raises:
            RuntimeError: If the connector is not connected.
            Exception: Propagates database-specific query errors.
        """

    @abstractmethod
    def health_check(self) -> Dict[str, Any]:
        """Verify database connectivity and return a health status dict.

        Returns:
            A dictionary containing **at minimum**:

            * ``status`` (str) — ``"healthy"`` or ``"unhealthy"``.
            * ``latency_ms`` (float) — Round-trip latency in milliseconds.

            Implementations may add extra keys such as ``database``,
            ``version``, or ``server_info``.
        """

    @abstractmethod
    def _map_column_type(self, generic_type: str) -> str:
        """Map a generic column type to the database-specific SQL type.

        Concrete connectors translate each value from
        :data:`GENERIC_COLUMN_TYPES` into the native dialect.  For example,
        PostgreSQL maps ``"UUID"`` to ``UUID`` while SQL Server maps it to
        ``UNIQUEIDENTIFIER``.

        Args:
            generic_type: One of the keys in :data:`GENERIC_COLUMN_TYPES`.

        Returns:
            The native SQL type string.

        Raises:
            ValueError: If *generic_type* is not recognised or not supported
                by the target database.
        """

    # ------------------------------------------------------------------
    # Concrete Properties
    # ------------------------------------------------------------------

    @property
    def is_connected(self) -> bool:
        """Return the current connection state.

        Returns:
            ``True`` if the connector has an active connection.
        """
        return self._connected

    # ------------------------------------------------------------------
    # Concrete Public Methods
    # ------------------------------------------------------------------

    def ensure_connected(self) -> None:
        """Lazily establish a connection if one does not already exist.

        Calls :meth:`connect` when ``is_connected`` is ``False``.  After
        the call, verifies that the connection was successfully established.

        Raises:
            RuntimeError: If the connection could not be established after
                calling :meth:`connect`.
        """
        if self._connected:
            return

        self._logger.info(
            "establishing_connection",
            host=self._host,
            port=self._port,
            database=self._database,
        )

        try:
            self.connect()
        except Exception as exc:
            self._logger.error(
                "connection_failed",
                host=self._host,
                port=self._port,
                database=self._database,
                error=str(exc),
            )
            raise RuntimeError(
                f"Failed to establish connection to {self._host}:{self._port}"
                f"/{self._database}: {exc}"
            ) from exc

        if not self._connected:
            raise RuntimeError(
                f"Connection to {self._host}:{self._port}/{self._database} "
                "was attempted but the connector did not transition to "
                "a connected state."
            )

        self._logger.info(
            "connection_established",
            host=self._host,
            port=self._port,
            database=self._database,
        )

    def get_metrics(self) -> Dict[str, Any]:
        """Return operational metrics for Prometheus / monitoring collection.

        Returns:
            Dictionary with the following keys:

            * ``total_rows_inserted`` (int) — Cumulative rows inserted since
              the connector was instantiated.
            * ``total_queries_executed`` (int) — Cumulative queries executed.
            * ``is_connected`` (bool) — Current connection state.
            * ``connector_type`` (str) — Name of the concrete connector class.
            * ``host`` (str) — Target database host.
            * ``port`` (int) — Target database port.
            * ``database`` (str) — Target database name.
        """
        return {
            "total_rows_inserted": self._total_rows_inserted,
            "total_queries_executed": self._total_queries_executed,
            "is_connected": self._connected,
            "connector_type": self.__class__.__name__,
            "host": self._host,
            "port": self._port,
            "database": self._database,
        }

    # ------------------------------------------------------------------
    # Concrete Protected Helpers
    # ------------------------------------------------------------------

    def _execute_with_retry(
        self,
        operation: Callable[..., Any],
        *args: Any,
        **kwargs: Any,
    ) -> Any:
        """Execute *operation* with exponential-backoff retry logic.

        The method retries up to ``self._max_retries`` times.  On each
        failure the delay doubles: ``retry_delay * 2 ** attempt``.  After
        all retries are exhausted the last exception is re-raised.

        Args:
            operation: A callable to execute.
            *args: Positional arguments forwarded to *operation*.
            **kwargs: Keyword arguments forwarded to *operation*.

        Returns:
            The return value of *operation* on success.

        Raises:
            Exception: The final exception raised by *operation* after all
                retries are exhausted.
        """
        last_exception: Optional[Exception] = None

        for attempt in range(self._max_retries + 1):
            try:
                return operation(*args, **kwargs)
            except Exception as exc:
                last_exception = exc
                if attempt < self._max_retries:
                    delay: float = self._retry_delay * (2 ** attempt)
                    self._logger.warning(
                        "operation_retry",
                        attempt=attempt + 1,
                        max_retries=self._max_retries,
                        delay_seconds=delay,
                        error=str(exc),
                        operation=getattr(operation, "__name__", str(operation)),
                    )
                    time.sleep(delay)
                else:
                    self._logger.error(
                        "operation_retries_exhausted",
                        total_attempts=self._max_retries + 1,
                        error=str(exc),
                        operation=getattr(operation, "__name__", str(operation)),
                    )

        # This line is reached only when all retries are exhausted.
        raise last_exception  # type: ignore[misc]

    def _build_column_definition(self, column: Dict[str, Any]) -> str:
        """Build a SQL column-definition fragment from a column spec dict.

        Translates a generic column specification into a SQL fragment
        suitable for inclusion in a ``CREATE TABLE`` statement.  The
        concrete connector's :meth:`_map_column_type` is called to
        resolve the native type.

        Args:
            column: Dictionary with at least ``name`` and ``type`` keys.
                Optional keys: ``nullable``, ``primary_key``, ``default``,
                ``unique``, ``max_length``, ``precision``, ``scale``.

        Returns:
            A formatted SQL column definition string, e.g.
            ``"order_id BIGINT NOT NULL PRIMARY KEY"``.

        Raises:
            ValueError: If ``name`` or ``type`` is missing, or if the
                generic type is unsupported by the connector.
        """
        col_name: str = column.get("name", "")
        col_type: str = column.get("type", "")

        if not col_name:
            raise ValueError("Column specification must include a 'name' key.")
        if not col_type:
            raise ValueError(
                f"Column '{col_name}' specification must include a 'type' key."
            )

        # Resolve the native SQL type via the concrete connector.
        native_type: str = self._map_column_type(col_type)

        # Apply precision modifiers for types that support them.
        max_length: Optional[int] = column.get("max_length")
        precision: Optional[int] = column.get("precision")
        scale: Optional[int] = column.get("scale")

        if max_length is not None and col_type in ("STRING", "TEXT", "BINARY"):
            native_type = f"{native_type}({max_length})"
        elif precision is not None and col_type == "DECIMAL":
            if scale is not None:
                native_type = f"{native_type}({precision}, {scale})"
            else:
                native_type = f"{native_type}({precision})"

        # Begin assembling the fragment: <name> <type>
        parts: List[str] = [col_name, native_type]

        # Nullability (default is nullable).
        nullable: bool = column.get("nullable", True)
        if not nullable:
            parts.append("NOT NULL")

        # Default value.
        default_value: Any = column.get("default")
        if default_value is not None:
            # Strings are quoted; everything else is rendered as-is.
            if isinstance(default_value, str):
                # Escape single quotes in default value to prevent SQL injection.
                safe_default = default_value.replace("'", "''")
                parts.append(f"DEFAULT '{safe_default}'")
            elif isinstance(default_value, bool):
                parts.append(f"DEFAULT {'TRUE' if default_value else 'FALSE'}")
            else:
                parts.append(f"DEFAULT {default_value}")

        # Primary key constraint.
        if column.get("primary_key", False):
            parts.append("PRIMARY KEY")

        # Unique constraint (only if not already a primary key).
        if column.get("unique", False) and not column.get("primary_key", False):
            parts.append("UNIQUE")

        return " ".join(parts)

    def _validate_config(self, required_keys: List[str]) -> None:
        """Validate that all required keys are present in the config dict.

        Called by concrete connector ``__init__`` methods to fail fast when
        mandatory configuration is absent.

        Args:
            required_keys: List of key names that must exist in
                ``self._config``.

        Raises:
            ValueError: With a message listing all missing keys.
        """
        missing: List[str] = [
            key for key in required_keys if key not in self._config
        ]
        if missing:
            raise ValueError(
                f"{self.__class__.__name__} configuration is missing "
                f"required keys: {', '.join(missing)}"
            )

    def _measure_latency(
        self,
        operation: Callable[..., Any],
        *args: Any,
        **kwargs: Any,
    ) -> Tuple[Any, float]:
        """Execute *operation* and measure wall-clock latency in milliseconds.

        Useful for health-check probes and performance monitoring.

        Args:
            operation: A callable to execute and time.
            *args: Positional arguments forwarded to *operation*.
            **kwargs: Keyword arguments forwarded to *operation*.

        Returns:
            A two-element tuple ``(result, latency_ms)`` where *result* is
            the return value of *operation* and *latency_ms* is the elapsed
            time in milliseconds.
        """
        start: float = time.time()
        result: Any = operation(*args, **kwargs)
        elapsed_ms: float = (time.time() - start) * 1000.0
        return result, elapsed_ms

    # ------------------------------------------------------------------
    # Context Manager Protocol
    # ------------------------------------------------------------------

    def __enter__(self) -> "BaseConnector":
        """Enter the runtime context — establish a database connection.

        Returns:
            The connector instance (``self``) for use in a ``with`` block.
        """
        self.connect()
        return self

    def __exit__(
        self,
        exc_type: Optional[type],
        exc_val: Optional[BaseException],
        exc_tb: Optional[Any],
    ) -> None:
        """Exit the runtime context — close the database connection.

        If an exception occurred inside the ``with`` block it is logged
        at the ``error`` level before disconnection.  The exception is
        **not** suppressed — it continues to propagate after cleanup.

        Args:
            exc_type: Exception class, or ``None`` if no exception.
            exc_val: Exception instance, or ``None``.
            exc_tb: Traceback object, or ``None``.
        """
        if exc_type is not None:
            self._logger.error(
                "context_exit_with_exception",
                exception_type=exc_type.__name__,
                exception_message=str(exc_val),
                host=self._host,
                database=self._database,
            )

        try:
            self.disconnect()
        except Exception as disconnect_exc:
            self._logger.error(
                "disconnect_error_during_exit",
                error=str(disconnect_exc),
                host=self._host,
                database=self._database,
            )

    # ------------------------------------------------------------------
    # Dunder Helpers
    # ------------------------------------------------------------------

    def __repr__(self) -> str:
        """Return a human-readable string representation of the connector.

        Returns:
            String in the format
            ``ClassName(host=..., port=..., database=...)``.
        """
        return (
            f"{self.__class__.__name__}("
            f"host={self._host}, "
            f"port={self._port}, "
            f"database={self._database})"
        )
