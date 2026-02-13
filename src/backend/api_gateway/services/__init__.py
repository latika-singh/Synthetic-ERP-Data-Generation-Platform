"""Service layer package for the API Gateway.

This package contains business logic services that orchestrate operations
between the API Gateway route handlers and downstream microservices
(Generation Engine, Profiling Service, Quality Service, Compliance Service,
Provisioning Service).

**Available services:**

- :class:`ExportService` — Export orchestration dispatching to the
  Provisioning Service for database provisioning (PostgreSQL, Oracle,
  SQL Server, SAP HANA), cloud storage uploads (AWS S3, Azure Blob,
  GCP Cloud Storage), and file downloads (SQL, CSV, JSON, Parquet).

- :class:`ProfileService` — Business logic for statistical profile
  management and inter-service communication with the Profiling Service.
  Provides create_profile, get_profile, list_profiles,
  get_profile_statistics, and delete_profile methods with multi-tenant
  isolation and circuit-breaker resilience.

- :class:`SchemaService` — Business logic for ERP schema discovery
  orchestration, schema retrieval with Redis caching, table/column
  detail lookups, and foreign-key relationship querying.  Dispatches
  discovery requests to the Profiling Service with circuit-breaker
  protection.

- :class:`JobService` — Business logic for generation job lifecycle
  management including create, get, list, update status/progress,
  cancel, and statistics methods with multi-tenant isolation.
  *(Available once job_service module is deployed.)*

- :class:`AuthService` — Auth0 integration and authentication management
  including login URL generation, callback handling, token validation,
  refresh, role/tenant extraction, and user profile management.
  *(Available once auth_service module is deployed.)*

All service classes enforce **multi-tenant isolation** per requirement R-007:
every public method accepts a ``tenant_id`` parameter and all data-access
operations are tenant-scoped.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

# ---------------------------------------------------------------------------
# Direct imports for services that are available on disk
# ---------------------------------------------------------------------------
from api_gateway.services.export_service import ExportService
from api_gateway.services.profile_service import ProfileService
from api_gateway.services.schema_service import SchemaService


# ---------------------------------------------------------------------------
# Lazy / conditional imports for services not yet deployed by other agents.
# Using try/except ensures the package remains importable even when
# job_service.py or auth_service.py have not been created yet.
# ---------------------------------------------------------------------------
try:
    from api_gateway.services.job_service import JobService
except ImportError:  # pragma: no cover
    JobService = None  # type: ignore[assignment,misc]

try:
    from api_gateway.services.auth_service import AuthService
except ImportError:  # pragma: no cover
    AuthService = None  # type: ignore[assignment,misc]


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------
__all__: list[str] = [
    "AuthService",
    "ExportService",
    "JobService",
    "ProfileService",
    "SchemaService",
    "get_auth_service",
    "get_export_service",
    "get_job_service",
    "get_profile_service",
    "get_schema_service",
]


# ---------------------------------------------------------------------------
# Factory helpers – simplify mocking in unit tests
# ---------------------------------------------------------------------------

def get_export_service() -> ExportService:
    """Return a new :class:`ExportService` instance.

    Factory function that enables easy mocking in tests via
    ``unittest.mock.patch('api_gateway.services.get_export_service')``.

    Returns:
        ExportService: A fresh service instance bound to the current
            Flask application context.
    """
    return ExportService()


def get_profile_service() -> ProfileService:
    """Return a new :class:`ProfileService` instance.

    Factory function that enables easy mocking in tests via
    ``unittest.mock.patch('api_gateway.services.get_profile_service')``.

    Returns:
        ProfileService: A fresh service instance bound to the current
            Flask application context.
    """
    return ProfileService()


def get_schema_service() -> SchemaService:
    """Return a new :class:`SchemaService` instance.

    Factory function that enables easy mocking in tests via
    ``unittest.mock.patch('api_gateway.services.get_schema_service')``.

    Returns:
        SchemaService: A fresh service instance bound to the current
            Flask application context.
    """
    return SchemaService()


def get_job_service():
    """Return a new :class:`JobService` instance if available.

    Factory function that enables easy mocking in tests via
    ``unittest.mock.patch('api_gateway.services.get_job_service')``.

    Returns:
        JobService | None: A fresh service instance bound to the current
            Flask application context, or ``None`` if the module is not
            yet deployed.

    Raises:
        RuntimeError: If ``JobService`` is not available.
    """
    if JobService is None:
        raise RuntimeError(
            "JobService is not available. Ensure job_service.py is deployed."
        )
    return JobService()


def get_auth_service():
    """Return a new :class:`AuthService` instance if available.

    Factory function that enables easy mocking in tests via
    ``unittest.mock.patch('api_gateway.services.get_auth_service')``.

    Returns:
        AuthService | None: A fresh service instance bound to the current
            Flask application context, or ``None`` if the module is not
            yet deployed.

    Raises:
        RuntimeError: If ``AuthService`` is not available.
    """
    if AuthService is None:
        raise RuntimeError(
            "AuthService is not available. Ensure auth_service.py is deployed."
        )
    return AuthService()
