"""Database connector registry package for the Provisioning Service.

This package implements the **Abstract Factory** pattern for JDBC-based
database provisioning connectors.  It provides a unified interface for
provisioning synthetic ERP data to four enterprise database platforms:

* **PostgreSQL** (12 – 16) — via :class:`PostgreSQLConnector`
* **Oracle** (19c – 23ai) — via :class:`OracleConnector`
* **SQL Server** (2019 – 2022) — via :class:`SQLServerConnector`
* **SAP HANA** (2.0 SPS 07+) — via :class:`HANAConnector`

All concrete connectors extend :class:`BaseConnector`, which defines the
uniform interface for connection management, schema operations (DDL),
batch data insertion (DML), query execution, and health monitoring.

The :data:`CONNECTOR_REGISTRY` dictionary maps canonical database type
strings (and common aliases) to their corresponding connector classes,
enabling runtime selection via the :func:`get_connector` factory function.

Usage::

    from provisioning_service.connectors import get_connector

    config = {
        "host": "db.example.com",
        "port": 5432,
        "database": "erp_target",
        "username": "admin",
        "password": "secret",
    }

    with get_connector("postgresql", config) as conn:
        conn.create_table("gl_entries", columns=[...])
        inserted = conn.batch_insert("gl_entries", columns=[...], data=[...])

Alternatively, import individual connector classes directly::

    from provisioning_service.connectors import (
        BaseConnector,
        PostgreSQLConnector,
        OracleConnector,
        SQLServerConnector,
        HANAConnector,
    )
"""

from __future__ import annotations

import logging
from typing import Any, Dict, List, Type

from provisioning_service.connectors.base import BaseConnector
from provisioning_service.connectors.hana_connector import HANAConnector
from provisioning_service.connectors.oracle_connector import OracleConnector
from provisioning_service.connectors.postgresql_connector import PostgreSQLConnector
from provisioning_service.connectors.sqlserver_connector import SQLServerConnector

# ---------------------------------------------------------------------------
# Module logger
# ---------------------------------------------------------------------------

_logger: logging.Logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Connector Registry — Abstract Factory mapping
# ---------------------------------------------------------------------------

CONNECTOR_REGISTRY: Dict[str, Type[BaseConnector]] = {
    # PostgreSQL 12 – 16: COPY-optimized bulk loading, schema-based tenancy
    "postgresql": PostgreSQLConnector,
    "postgres": PostgreSQLConnector,
    # Oracle 19c – 23ai: Array-bind batch inserts, tablespace tenancy
    "oracle": OracleConnector,
    "oracle_ebs": OracleConnector,
    # SQL Server 2019 – 2022: BCP bulk insert, schema-qualified tenancy
    "sqlserver": SQLServerConnector,
    "mssql": SQLServerConnector,
    # SAP HANA 2.0 SPS 07+: Column-store tables, in-memory batch inserts
    "hana": HANAConnector,
    "sap_hana": HANAConnector,
}
"""Registry mapping database type strings (and aliases) to connector classes.

Each key is a case-insensitive canonical name or common alias for a
supported target database platform.  Values are **class objects** (not
instances) that can be instantiated with a configuration dictionary.

Keys:
    postgresql: PostgreSQL connector (canonical).
    postgres: PostgreSQL connector (alias).
    oracle: Oracle connector (canonical).
    oracle_ebs: Oracle E-Business Suite connector (alias).
    sqlserver: SQL Server connector (canonical).
    mssql: Microsoft SQL Server connector (alias).
    hana: SAP HANA connector (canonical).
    sap_hana: SAP HANA connector (alias).
"""


# ---------------------------------------------------------------------------
# Factory Function
# ---------------------------------------------------------------------------


def get_connector(db_type: str, config: Dict[str, Any]) -> BaseConnector:
    """Instantiate and return a database connector for the given type.

    This is the primary entry point for the Abstract Factory pattern.  It
    looks up *db_type* (case-insensitive) in :data:`CONNECTOR_REGISTRY`
    and returns a freshly constructed connector instance configured with
    the provided *config* dictionary.

    Args:
        db_type: Target database type string.  Must match one of the keys
            in :data:`CONNECTOR_REGISTRY` (case-insensitive).  Accepted
            values include ``"postgresql"``, ``"postgres"``, ``"oracle"``,
            ``"oracle_ebs"``, ``"sqlserver"``, ``"mssql"``, ``"hana"``,
            and ``"sap_hana"``.
        config: Database connection configuration dictionary.  At minimum
            must contain ``host``, ``port``, ``database``, ``username``,
            and ``password`` keys.  Additional connector-specific keys
            (e.g., ``schema``, ``jar_path``, ``ssl``) are forwarded to
            the concrete connector.

    Returns:
        An instance of the appropriate :class:`BaseConnector` subclass,
        ready for :meth:`~BaseConnector.connect` to be called (or used
        as a context manager).

    Raises:
        ValueError: If *db_type* is ``None``, empty, or not found in the
            :data:`CONNECTOR_REGISTRY`.
        TypeError: If *config* is not a dictionary.

    Example::

        connector = get_connector("postgresql", {
            "host": "localhost",
            "port": 5432,
            "database": "erp_dev",
            "username": "admin",
            "password": "secret",
        })
        connector.connect()
    """
    # --- Input validation ------------------------------------------------
    if not db_type or not isinstance(db_type, str):
        supported = get_supported_databases()
        raise ValueError(
            f"db_type must be a non-empty string. "
            f"Supported database types: {supported}"
        )

    if not isinstance(config, dict):
        raise TypeError(
            f"config must be a dictionary, got {type(config).__name__}"
        )

    # Normalise to lower-case for case-insensitive lookup.
    normalised_type: str = db_type.strip().lower()

    connector_class: Type[BaseConnector] | None = CONNECTOR_REGISTRY.get(
        normalised_type
    )

    if connector_class is None:
        supported = get_supported_databases()
        raise ValueError(
            f"Unsupported database type: '{db_type}'. "
            f"Supported database types: {supported}"
        )

    _logger.info(
        "Creating %s connector for database type '%s'",
        connector_class.__name__,
        normalised_type,
    )

    return connector_class(config)


# ---------------------------------------------------------------------------
# Utility Function
# ---------------------------------------------------------------------------


def get_supported_databases() -> List[str]:
    """Return a sorted list of unique supported database type identifiers.

    Aliases (e.g., ``"postgres"`` and ``"postgresql"``) are **both**
    included in the returned list so that consumers can present all
    accepted identifiers to end-users or in documentation.

    Returns:
        Sorted list of all registered database type strings from
        :data:`CONNECTOR_REGISTRY`.

    Example::

        >>> get_supported_databases()
        ['hana', 'mssql', 'oracle', 'oracle_ebs', 'postgres',
         'postgresql', 'sap_hana', 'sqlserver']
    """
    return sorted(CONNECTOR_REGISTRY.keys())


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

__all__: List[str] = [
    # Base interface
    "BaseConnector",
    # Concrete connector classes
    "PostgreSQLConnector",
    "OracleConnector",
    "SQLServerConnector",
    "HANAConnector",
    # Registry and factory
    "CONNECTOR_REGISTRY",
    "get_connector",
    "get_supported_databases",
]
