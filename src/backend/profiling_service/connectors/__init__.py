"""ERP connector registry for the Profiling Service.

Implements a **factory pattern** for dynamically selecting and instantiating
the correct ERP schema discovery connector based on the ``erp_type`` string
provided in the :class:`ConnectionConfig`.

Supported ERP Connector Types:

+----------------+------------------------------------+
| ``erp_type``   | Connector Class                    |
+================+====================================+
| ``sap``        | :class:`SAPConnector`              |
+----------------+------------------------------------+
| ``oracle_ebs`` | :class:`OracleConnector`           |
+----------------+------------------------------------+
| ``dynamics_365``| :class:`DynamicsConnector`        |
+----------------+------------------------------------+
| ``jdbc_legacy``| :class:`JDBCConnector`             |
+----------------+------------------------------------+

**Privacy Guarantee (Constraint C-001):**
    All connectors extract **schema metadata only** — no raw production
    data is accessed or stored.

**Supported ERP Modules (Constraint C-005):**
    Financial Accounting, Human Resources, Sales & Distribution,
    Material Management.

Usage::

    from profiling_service.connectors import get_connector, ConnectionConfig

    config = ConnectionConfig(erp_type="sap", host="sap.example.com", ...)
    connector = get_connector(config.erp_type, config)
    connector.connect()
    tables = connector.discover_tables()
    connector.close()
"""

from __future__ import annotations

from typing import Type

from profiling_service.connectors.base import (
    BaseConnector,
    ColumnMetadata,
    ConnectionConfig,
    ConnectionError,
    ConnectorError,
    DiscoveryError,
    ERPModule,
    RelationshipMetadata,
    TableMetadata,
)
from profiling_service.connectors.dynamics_connector import DynamicsConnector
from profiling_service.connectors.jdbc_connector import JDBCConnector
from profiling_service.connectors.oracle_connector import OracleConnector
from profiling_service.connectors.sap_connector import SAPConnector
from shared.logging.structured_logger import get_logger

# ---------------------------------------------------------------------------
# Module-level logger
# ---------------------------------------------------------------------------

_logger = get_logger(__name__)

# ---------------------------------------------------------------------------
# Connector Registry  (factory pattern)
# ---------------------------------------------------------------------------

CONNECTOR_REGISTRY: dict[str, Type[BaseConnector]] = {
    "sap": SAPConnector,
    "oracle_ebs": OracleConnector,
    "dynamics_365": DynamicsConnector,
    "jdbc_legacy": JDBCConnector,
}

# ---------------------------------------------------------------------------
# Factory Function
# ---------------------------------------------------------------------------


def get_connector(erp_type: str, config: ConnectionConfig) -> BaseConnector:
    """Create and return the appropriate ERP connector for the given type.

    Looks up *erp_type* in :data:`CONNECTOR_REGISTRY` and instantiates the
    corresponding :class:`BaseConnector` subclass with *config*.

    Args:
        erp_type: Identifier for the target ERP system.  Must be one of the
            keys in :data:`CONNECTOR_REGISTRY` (``"sap"``, ``"oracle_ebs"``,
            ``"dynamics_365"``, ``"jdbc_legacy"``).
        config: A :class:`ConnectionConfig` carrying the connection details
            for the target ERP system.

    Returns:
        An instance of the matching :class:`BaseConnector` subclass,
        **not yet connected** — callers must invoke :meth:`connect`.

    Raises:
        ValueError: If *erp_type* is not recognised.

    Example::

        connector = get_connector("sap", config)
        connector.connect()
        try:
            tables = connector.discover_tables(module=ERPModule.FINANCIAL_ACCOUNTING)
        finally:
            connector.close()
    """
    connector_cls = CONNECTOR_REGISTRY.get(erp_type)
    if connector_cls is None:
        supported = ", ".join(sorted(CONNECTOR_REGISTRY.keys()))
        msg = (
            f"Unsupported ERP type '{erp_type}'. "
            f"Supported types: {supported}"
        )
        _logger.error(
            "connector_factory_unsupported_erp_type",
            erp_type=erp_type,
            supported_types=supported,
        )
        raise ValueError(msg)

    _logger.info(
        "connector_factory_creating",
        erp_type=erp_type,
        connector_class=connector_cls.__name__,
    )
    return connector_cls(config)


def get_supported_erp_types() -> list[str]:
    """Return a sorted list of supported ERP type identifiers.

    Returns:
        Sorted list of strings, each a valid ``erp_type`` value for
        :func:`get_connector`.
    """
    return sorted(CONNECTOR_REGISTRY.keys())


# ---------------------------------------------------------------------------
# Package Exports
# ---------------------------------------------------------------------------

__all__ = [
    # Base abstractions
    "BaseConnector",
    "ConnectionConfig",
    "TableMetadata",
    "ColumnMetadata",
    "RelationshipMetadata",
    "ERPModule",
    # Exceptions
    "ConnectorError",
    "ConnectionError",
    "DiscoveryError",
    # Concrete connectors
    "SAPConnector",
    "OracleConnector",
    "DynamicsConnector",
    "JDBCConnector",
    # Factory utilities
    "get_connector",
    "get_supported_erp_types",
    "CONNECTOR_REGISTRY",
]
