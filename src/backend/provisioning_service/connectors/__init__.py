"""Database connector package for the Provisioning Service.

This package provides abstract and concrete JDBC-based database
provisioning connectors for batch-inserting generated synthetic ERP
data into target database systems:

* :class:`BaseConnector` — Abstract base class defining the uniform
  interface for connection management, schema operations, batch data
  insertion, and health monitoring.

Concrete implementations (PostgreSQL, Oracle, SQL Server, SAP HANA)
extend ``BaseConnector`` and are registered here for convenient access.
"""

from __future__ import annotations

from provisioning_service.connectors.base import GENERIC_COLUMN_TYPES, BaseConnector


__all__: list[str] = ["GENERIC_COLUMN_TYPES", "BaseConnector"]
