"""Abstract base connector and shared data models for ERP schema discovery.

This module defines the foundational abstractions used by all ERP connector
implementations in the Profiling Service.  Every concrete connector — SAP,
Oracle E-Business Suite, Microsoft Dynamics 365, and generic JDBC — inherits
from :class:`BaseConnector` and overrides the five abstract methods that
constitute the schema-discovery interface.

**Privacy Guarantee (Constraint C-001):**
    All connectors are designed to extract *metadata only* — table names,
    column definitions, data types, relationship constraints, and aggregate
    statistics.  No raw production data is accessed, transferred, or stored
    at any point in the discovery pipeline.

**Supported ERP Modules (Constraint C-005):**
    The initial release supports four ERP functional modules:

    - Financial Accounting (GL entries, invoices, payments)
    - Human Resources (employee records, payroll, benefits)
    - Sales & Distribution (orders, customers, pricing)
    - Material Management (inventory, purchase orders, vendors)

Design Patterns:
    - **Abstract Factory** — :class:`BaseConnector` defines the interface;
      concrete connectors are instantiated via a registry/factory.
    - **Strategy** — Each connector encapsulates a different discovery
      strategy (RFC/BAPI, OData, JDBC, Web API).
    - **Context Manager** — Connectors support ``with`` statements for
      automatic resource cleanup.

Usage::

    from profiling_service.connectors.base import (
        BaseConnector,
        ConnectionConfig,
        ERPModule,
    )

    config = ConnectionConfig(
        erp_type="sap",
        host="erp.example.com",
        port=3300,
        username="discovery_user",
        password="********",
    )

    # Using context manager (recommended)
    with SAPConnector(config) as connector:
        tables = connector.discover_tables(module=ERPModule.FINANCIAL_ACCOUNTING)
        for table in tables:
            columns = connector.discover_columns(table.table_name, table.schema_name)
"""

from __future__ import annotations

import time
from abc import ABC, abstractmethod
from datetime import datetime  # noqa: TC003 — required at runtime by Pydantic field validators
from enum import StrEnum
from typing import TYPE_CHECKING, Any

from pydantic import BaseModel, ConfigDict, Field

from shared.logging.structured_logger import get_logger


if TYPE_CHECKING:
    from collections.abc import Callable


# Module-level logger for this base module.  Concrete connectors initialise
# their own logger via ``get_logger(__name__)`` in ``BaseConnector.__init__``.
_module_logger = get_logger(__name__)


# ---------------------------------------------------------------------------
# Enumerations
# ---------------------------------------------------------------------------


class ERPModule(StrEnum):
    """Enumeration of supported ERP functional modules.

    Constraint C-005 limits the initial release to these four modules.
    Each module maps to a well-defined set of ERP database tables and
    business objects across SAP, Oracle EBS, and Microsoft Dynamics.

    Attributes:
        FINANCIAL_ACCOUNTING: General Ledger, Accounts Payable/Receivable,
            invoices, payment transactions, and journal entries.
        HUMAN_RESOURCES: Employee master data, organisational structures,
            payroll records, benefits, and time management.
        SALES_DISTRIBUTION: Sales orders, customer master data, pricing
            conditions, deliveries, and billing documents.
        MATERIAL_MANAGEMENT: Material master, purchase orders, vendor
            master data, inventory movements, and goods receipts.
    """

    FINANCIAL_ACCOUNTING = "financial_accounting"
    HUMAN_RESOURCES = "human_resources"
    SALES_DISTRIBUTION = "sales_distribution"
    MATERIAL_MANAGEMENT = "material_management"


# ---------------------------------------------------------------------------
# Pydantic Data Models
# ---------------------------------------------------------------------------


class ConnectionConfig(BaseModel):
    """Configuration parameters for establishing a connection to an ERP system.

    This model supports multiple connection strategies:

    - **JDBC-based:** Uses ``host``/``port``/``database`` or a raw
      ``jdbc_url`` together with ``jdbc_driver_class`` and
      ``jdbc_driver_path``.
    - **API-based:** Uses ``api_url`` with optional OAuth credentials
      (``client_id``, ``client_secret``, ``tenant_id``).
    - **Hybrid:** Some connectors (e.g. SAP RFC) combine host-based
      connectivity with specialised authentication.

    The ``additional_params`` dictionary provides an escape hatch for
    connector-specific options not captured by the standard fields.

    Attributes:
        erp_type: Identifier of the ERP system type (e.g. ``"sap"``,
            ``"oracle_ebs"``, ``"dynamics365"``, ``"jdbc"``).
        host: Hostname or IP address of the ERP database / application
            server.
        port: Network port for the connection.
        username: Authentication username.
        password: Authentication password.  Sensitive — never logged.
        database: Database or schema name on the target system.
        jdbc_url: Full JDBC connection URL (overrides host/port/database
            when provided).
        jdbc_driver_class: Fully-qualified Java class name for the JDBC
            driver (e.g. ``"com.sap.db.jdbc.Driver"``).
        jdbc_driver_path: File-system path to the JDBC driver JAR file.
        api_url: Base URL for REST/OData API endpoints.
        client_id: OAuth 2.0 client identifier for API authentication.
        client_secret: OAuth 2.0 client secret.  Sensitive — never logged.
        tenant_id: Multi-tenant identifier used by some APIs (e.g.
            Microsoft Dynamics 365 Azure AD tenant).
        additional_params: Connector-specific key-value parameters not
            covered by the standard fields above.
        max_retries: Maximum number of retry attempts for transient
            failures.  Defaults to ``3``.
        retry_delay: Base delay in seconds between retries.  The actual
            delay is computed via exponential backoff:
            ``retry_delay * 2 ** attempt``.  Defaults to ``2.0``.
        timeout: Operation timeout in seconds.  Applied to connection
            establishment and individual metadata queries.  Defaults to
            ``300`` (5 minutes).
    """

    model_config = ConfigDict(frozen=False, str_strip_whitespace=True)

    erp_type: str
    host: str | None = None
    port: int | None = None
    username: str | None = None
    password: str | None = None
    database: str | None = None
    jdbc_url: str | None = None
    jdbc_driver_class: str | None = None
    jdbc_driver_path: str | None = None
    api_url: str | None = None
    client_id: str | None = None
    client_secret: str | None = None
    tenant_id: str | None = None
    additional_params: dict[str, Any] = Field(default_factory=dict)
    max_retries: int = Field(default=3, ge=0, le=10)
    retry_delay: float = Field(default=2.0, gt=0.0)
    timeout: int = Field(default=300, gt=0)


class TableMetadata(BaseModel):
    """Metadata describing a single database table or view in an ERP system.

    Captured during schema discovery, this model represents the structural
    metadata of a table without ever accessing its production data content
    (Constraint C-001).

    Attributes:
        table_name: Physical table or view name as defined in the ERP
            database catalog.
        schema_name: Database schema or namespace containing the table
            (e.g. ``"SAPSR3"``, ``"APPS"``).
        description: Human-readable description of the table's business
            purpose, typically sourced from ERP data dictionaries.
        estimated_row_count: Approximate number of rows, obtained from
            database statistics (``pg_stat_user_tables``,
            ``ALL_TAB_STATISTICS``, etc.) — *not* from ``SELECT COUNT(*)``.
        module: The ERP functional module to which this table belongs.
        table_type: Type indicator — ``"TABLE"``, ``"VIEW"``,
            ``"MATERIALIZED_VIEW"``, etc.
        last_analyzed: Timestamp of the most recent statistics refresh
            for this table, as reported by the database catalog.
    """

    model_config = ConfigDict(frozen=False, str_strip_whitespace=True)

    table_name: str
    schema_name: str | None = None
    description: str | None = None
    estimated_row_count: int | None = Field(default=None, ge=0)
    module: ERPModule | None = None
    table_type: str | None = Field(default="TABLE")
    last_analyzed: datetime | None = None


class ColumnMetadata(BaseModel):
    """Metadata describing a single column within an ERP database table.

    Attributes:
        column_name: Physical column name as defined in the ERP database
            catalog.
        native_type: The ERP-native data type string exactly as reported
            by the source system (e.g. ``"NVARCHAR2(255)"``,
            ``"DECIMAL(15,2)"``, ``"DATS"`` for SAP date).
        standard_type: Normalised data type mapped to a platform-standard
            vocabulary (e.g. ``"string"``, ``"integer"``, ``"decimal"``,
            ``"date"``, ``"boolean"``).  ``None`` if mapping is unavailable.
        max_length: Maximum character or byte length for string/binary
            types.  ``None`` for non-length-constrained types.
        precision: Total number of significant digits for numeric types.
        scale: Number of decimal digits for numeric types.
        is_nullable: Whether the column permits ``NULL`` values.
        is_primary_key: Whether the column participates in the table's
            primary key.
        is_auto_increment: Whether the column value is auto-generated
            (identity, sequence, auto-increment).
        ordinal_position: 1-based position of the column within the
            table definition order.
        description: Human-readable description of the column's business
            purpose, sourced from ERP data dictionaries where available.
        default_value: Column default value expression as a string
            representation.  ``None`` if no default is defined.
    """

    model_config = ConfigDict(frozen=False, str_strip_whitespace=True)

    column_name: str
    native_type: str
    standard_type: str | None = None
    max_length: int | None = Field(default=None, ge=0)
    precision: int | None = Field(default=None, ge=0)
    scale: int | None = Field(default=None, ge=0)
    is_nullable: bool = True
    is_primary_key: bool = False
    is_auto_increment: bool = False
    ordinal_position: int | None = Field(default=None, ge=1)
    description: str | None = None
    default_value: str | None = None


class RelationshipMetadata(BaseModel):
    """Metadata describing a foreign-key relationship between two tables.

    Captures referential integrity constraints discovered in the ERP
    database catalog.  These relationships are critical for the Generation
    Engine's dependency graph construction and ordered data generation.

    Attributes:
        constraint_name: Database constraint name (e.g.
            ``"FK_ORDER_CUSTOMER"``).  May be ``None`` for inferred
            (non-declared) relationships.
        source_table: Fully-qualified name of the referencing (child)
            table.
        source_column: Column name in the source table that holds the
            foreign key value.
        target_table: Fully-qualified name of the referenced (parent)
            table.
        target_column: Column name in the target table that is
            referenced (typically a primary key).
        relationship_type: Cardinality indicator — ``"ONE_TO_MANY"``,
            ``"MANY_TO_ONE"``, ``"ONE_TO_ONE"``, or ``"MANY_TO_MANY"``.
        on_delete: Referential action on parent row deletion —
            ``"CASCADE"``, ``"SET NULL"``, ``"RESTRICT"``,
            ``"NO ACTION"``, or ``None`` if unspecified.
        on_update: Referential action on parent key update —
            same value domain as ``on_delete``.
    """

    model_config = ConfigDict(frozen=False, str_strip_whitespace=True)

    constraint_name: str | None = None
    source_table: str
    source_column: str
    target_table: str
    target_column: str
    relationship_type: str = Field(default="ONE_TO_MANY")
    on_delete: str | None = None
    on_update: str | None = None


# ---------------------------------------------------------------------------
# Custom Exception Hierarchy
# ---------------------------------------------------------------------------


class ConnectorError(Exception):
    """Base exception for all ERP connector errors.

    Provides structured error context that downstream consumers (API
    Gateway, monitoring, audit logging) can use for diagnostics.

    Attributes:
        message: Human-readable error description.
        erp_type: The ERP system type that produced the error (e.g.
            ``"sap"``, ``"oracle_ebs"``).  ``None`` when the error is
            not specific to an ERP type.
        original_error: The underlying exception that triggered this
            error, preserved for root-cause analysis.  ``None`` when the
            error originated within the connector itself.
    """

    def __init__(
        self,
        message: str,
        erp_type: str | None = None,
        original_error: Exception | None = None,
    ) -> None:
        """Initialise a ConnectorError.

        Args:
            message: Human-readable error description.
            erp_type: The ERP system type identifier.
            original_error: The underlying exception, if any.
        """
        self.message = message
        self.erp_type = erp_type
        self.original_error = original_error
        super().__init__(message)

    def __str__(self) -> str:
        """Return a formatted string representation of the error."""
        parts: list[str] = []
        if self.erp_type:
            parts.append(f"[{self.erp_type}]")
        parts.append(self.message)
        if self.original_error:
            parts.append(f"(caused by {type(self.original_error).__name__}: {self.original_error})")
        return " ".join(parts)

    def __repr__(self) -> str:
        """Return a detailed repr for debugging."""
        return (
            f"{type(self).__name__}("
            f"message={self.message!r}, "
            f"erp_type={self.erp_type!r}, "
            f"original_error={self.original_error!r})"
        )


class ConnectionError(ConnectorError):  # noqa: A001 — intentionally shadows builtin
    """Raised when a connection to an ERP system cannot be established.

    Common causes include:
        - Network unreachability (firewall, DNS resolution failure)
        - Authentication failure (invalid credentials, expired tokens)
        - JDBC driver not found or incompatible
        - Connection timeout exceeded
    """


class DiscoveryError(ConnectorError):
    """Raised when schema metadata discovery fails after a connection is established.

    Common causes include:
        - Insufficient database privileges for catalog queries
        - Unsupported schema object types
        - Malformed metadata returned by the ERP system
        - Query timeout during large schema enumeration
    """


# ---------------------------------------------------------------------------
# Abstract Base Connector
# ---------------------------------------------------------------------------


class BaseConnector(ABC):
    """Abstract base class for all ERP schema discovery connectors.

    Provides the common interface, retry logic, connection state tracking,
    and context manager support that concrete connectors (SAP, Oracle EBS,
    Dynamics 365, JDBC) inherit and extend.

    **Privacy by Design (C-001):**
        The abstract interface is intentionally limited to *metadata
        discovery* operations.  There are no methods for reading, writing,
        or streaming production data.  Concrete implementations must honour
        this contract — only catalog/dictionary queries are permitted.

    **Lifecycle:**

    1. Instantiate with a :class:`ConnectionConfig`.
    2. Call :meth:`connect` (or use as a context manager).
    3. Call discovery methods: :meth:`discover_tables`,
       :meth:`discover_columns`, :meth:`discover_relationships`.
    4. Call :meth:`close` (automatic when exiting context manager).

    Example::

        connector = SAPConnector(config)
        try:
            connector.connect()
            tables = connector.discover_tables(module=ERPModule.HUMAN_RESOURCES)
        finally:
            connector.close()

        # Or, using the context manager:
        with SAPConnector(config) as conn:
            tables = conn.discover_tables(module=ERPModule.HUMAN_RESOURCES)

    Attributes:
        config: The connection configuration for this connector instance.
    """

    def __init__(self, config: ConnectionConfig) -> None:
        """Initialise the base connector with the given configuration.

        Args:
            config: Validated connection parameters for the target ERP
                system.  The ``erp_type`` field is used for log messages
                and error context.
        """
        self._config: ConnectionConfig = config
        self._connected: bool = False
        self._logger = get_logger(f"{self.__class__.__module__}.{self.__class__.__name__}")

        self._logger.info(
            "connector_initialised",
            erp_type=config.erp_type,
            host=config.host,
            port=config.port,
            max_retries=config.max_retries,
            timeout=config.timeout,
        )

    # -- Properties ---------------------------------------------------------

    @property
    def config(self) -> ConnectionConfig:
        """Return the connection configuration (read-only access)."""
        return self._config

    @property
    def connected(self) -> bool:
        """Indicate whether the connector currently holds an active connection.

        Returns:
            ``True`` if :meth:`connect` has been called successfully and
            :meth:`close` has not yet been called; ``False`` otherwise.
        """
        return self._connected

    # -- Abstract Interface -------------------------------------------------

    @abstractmethod
    def connect(self) -> None:
        """Establish a connection to the target ERP system.

        Implementations must set ``self._connected = True`` on success and
        raise :class:`ConnectionError` on failure.

        Raises:
            ConnectionError: If the connection cannot be established after
                exhausting retry attempts.
        """

    @abstractmethod
    def discover_tables(
        self,
        schema_name: str | None = None,
        module: ERPModule | None = None,
    ) -> list[TableMetadata]:
        """Discover tables and views in the target ERP schema.

        **Privacy (C-001):** Implementations must query the database
        catalog or data dictionary *only* — never ``SELECT`` from
        business data tables.

        Args:
            schema_name: Optional schema/namespace filter.  When ``None``,
                the connector's default schema is used.
            module: Optional ERP module filter.  When provided, only
                tables belonging to the specified functional module are
                returned.

        Returns:
            A list of :class:`TableMetadata` instances describing each
            discovered table or view.

        Raises:
            DiscoveryError: If metadata extraction fails.
            ConnectionError: If the connector is not connected.
        """

    @abstractmethod
    def discover_columns(
        self,
        table_name: str,
        schema_name: str | None = None,
    ) -> list[ColumnMetadata]:
        """Discover columns for a specific table.

        **Privacy (C-001):** Implementations must query the database
        catalog or data dictionary *only* — column metadata such as
        data type, length, and constraints are extracted without
        accessing actual data values.

        Args:
            table_name: The physical name of the table to introspect.
            schema_name: Optional schema/namespace qualifier.  When
                ``None``, the connector's default schema is used.

        Returns:
            A list of :class:`ColumnMetadata` instances describing each
            column in the specified table, ordered by
            ``ordinal_position``.

        Raises:
            DiscoveryError: If metadata extraction fails or the table
                does not exist.
            ConnectionError: If the connector is not connected.
        """

    @abstractmethod
    def discover_relationships(
        self,
        schema_name: str | None = None,
    ) -> list[RelationshipMetadata]:
        """Discover foreign-key relationships across the target schema.

        **Privacy (C-001):** Implementations must query constraint metadata
        from the database catalog — never follow foreign keys into
        production data.

        Args:
            schema_name: Optional schema/namespace filter.  When ``None``,
                the connector's default schema is used.

        Returns:
            A list of :class:`RelationshipMetadata` instances describing
            each discovered foreign-key constraint.

        Raises:
            DiscoveryError: If relationship discovery fails.
            ConnectionError: If the connector is not connected.
        """

    @abstractmethod
    def close(self) -> None:
        """Release all resources and close the connection to the ERP system.

        Implementations must set ``self._connected = False`` after cleanup
        and must be safe to call multiple times (idempotent).

        After ``close()`` is called, subsequent discovery method calls must
        raise :class:`ConnectionError`.
        """

    # -- Concrete Helpers ---------------------------------------------------

    def _retry_with_backoff(
        self,
        operation: Callable[..., Any],
        *args: Any,
        operation_name: str = "operation",
        max_retries: int | None = None,
        base_delay: float | None = None,
        **kwargs: Any,
    ) -> Any:
        """Execute an operation with automatic retry and exponential backoff.

        Designed for wrapping transient-failure-prone operations such as
        network connections, JDBC queries, and API calls.  On each failed
        attempt the delay doubles (``base_delay * 2 ** attempt``), giving
        the target system time to recover.

        Args:
            operation: The callable to execute.
            *args: Positional arguments forwarded to *operation*.
            operation_name: Human-readable label for log messages
                (e.g. ``"connect"``, ``"discover_tables"``).
            max_retries: Override for ``config.max_retries``.  When
                ``None``, the connector's configured value is used.
            base_delay: Override for ``config.retry_delay``.  When
                ``None``, the connector's configured value is used.
            **kwargs: Keyword arguments forwarded to *operation*.

        Returns:
            The return value of *operation* on the first successful
            invocation.

        Raises:
            ConnectorError: Re-raises the last caught exception (wrapped
                in a :class:`ConnectorError` if necessary) after all retry
                attempts are exhausted.
        """
        effective_retries: int = max_retries if max_retries is not None else self._config.max_retries
        effective_delay: float = base_delay if base_delay is not None else self._config.retry_delay

        last_exception: Exception | None = None

        for attempt in range(effective_retries + 1):
            try:
                result = operation(*args, **kwargs)
                if attempt > 0:
                    self._logger.info(
                        "retry_succeeded",
                        operation=operation_name,
                        attempt=attempt + 1,
                        erp_type=self._config.erp_type,
                    )
                return result
            except ConnectorError:
                # Already a connector-level exception — propagate as-is.
                raise
            except Exception as exc:
                last_exception = exc
                if attempt < effective_retries:
                    delay = effective_delay * (2 ** attempt)
                    self._logger.warning(
                        "operation_retry",
                        operation=operation_name,
                        attempt=attempt + 1,
                        max_retries=effective_retries,
                        delay_seconds=delay,
                        error=str(exc),
                        error_type=type(exc).__name__,
                        erp_type=self._config.erp_type,
                    )
                    time.sleep(delay)
                else:
                    self._logger.error(
                        "operation_failed_after_retries",
                        operation=operation_name,
                        total_attempts=effective_retries + 1,
                        error=str(exc),
                        error_type=type(exc).__name__,
                        erp_type=self._config.erp_type,
                    )

        # All retries exhausted — wrap the last exception and raise.
        raise ConnectorError(
            message=f"{operation_name} failed after {effective_retries + 1} attempt(s)",
            erp_type=self._config.erp_type,
            original_error=last_exception,
        )

    def _validate_connected(self) -> None:
        """Assert that the connector holds an active connection.

        Convenience guard method intended to be called at the beginning of
        every discovery method implementation.

        Raises:
            ConnectionError: If the connector is not currently connected.
        """
        if not self._connected:
            raise ConnectionError(
                message="Connector is not connected. Call connect() before performing discovery operations.",
                erp_type=self._config.erp_type,
            )

    # -- Context Manager Protocol -------------------------------------------

    def __enter__(self) -> BaseConnector:
        """Enter the context manager, establishing the ERP connection.

        Calls :meth:`connect` and returns ``self`` so that the connector
        can be used in a ``with`` statement::

            with SAPConnector(config) as conn:
                tables = conn.discover_tables()

        Returns:
            The connector instance with an active connection.

        Raises:
            ConnectionError: If the connection cannot be established.
        """
        self._logger.debug(
            "context_manager_enter",
            erp_type=self._config.erp_type,
        )
        self.connect()
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc_val: BaseException | None,
        exc_tb: Any,
    ) -> None:
        """Exit the context manager, closing the ERP connection.

        Always calls :meth:`close` to release resources, even when an
        exception occurred inside the ``with`` block.  Exceptions are
        **not** suppressed — they propagate to the caller after cleanup.

        Args:
            exc_type: The exception type, if an exception was raised
                inside the context block.  ``None`` otherwise.
            exc_val: The exception instance, if any.
            exc_tb: The traceback object, if any.

        Returns:
            ``None`` — exceptions are never suppressed by this context
            manager.
        """
        try:
            self.close()
        except Exception as close_exc:
            # Log but do not mask the original exception (if any).
            self._logger.warning(
                "context_manager_close_error",
                error=str(close_exc),
                error_type=type(close_exc).__name__,
                erp_type=self._config.erp_type,
            )
        finally:
            self._logger.debug(
                "context_manager_exit",
                erp_type=self._config.erp_type,
                had_exception=exc_type is not None,
            )
