"""Business logic service layer for the API Gateway.

This package provides the service layer that acts as the intermediary between
the HTTP route handlers (``api_gateway.routes``) and the data / external
service layer.  All business logic, inter-service communication, data access
patterns, and orchestration are encapsulated in the service classes below,
keeping route handlers thin and focused on request/response translation.

**Available Service Classes:**

- :class:`JobService` — Generation job lifecycle management including create,
  get, list, update status/progress, cancel, and per-tenant statistics.
  Dispatches jobs to the Generation Engine with circuit-breaker resilience
  and tracks progress via Redis pub/sub.

- :class:`ProfileService` — Statistical profile management and inter-service
  communication with the Profiling Service.  Provides create, get, list,
  statistics access, and delete operations with Redis cache-aside and
  circuit-breaker protection on outbound HTTP calls.

- :class:`SchemaService` — ERP schema discovery orchestration.  Validates
  and dispatches discovery requests to the Profiling Service, stores results
  in MongoDB, and supports browsing schemas, tables, columns, and foreign-key
  relationships with Redis caching.

- :class:`AuthService` — Auth0 integration and authentication management
  including login URL generation, OAuth callback handling, RS256 JWT
  validation, refresh-token rotation, role/tenant extraction, user profile
  management, and logout flow.

- :class:`ExportService` — Export orchestration to the Provisioning Service
  for database provisioning (PostgreSQL, Oracle, SQL Server, SAP HANA),
  cloud storage uploads (AWS S3, Azure Blob, GCP Cloud Storage), and file
  downloads (SQL, CSV, JSON, Parquet).  Provides full export lifecycle
  management with progress tracking.

**Multi-Tenant Isolation (R-007):**

Every service class enforces multi-tenant isolation by requiring a
``tenant_id`` parameter on all public methods.  All downstream operations
(MongoDB queries, Redis key namespacing, HTTP headers) are tenant-scoped
so that cross-tenant data access is impossible by design.

**Factory Functions:**

Convenience factory functions (``get_job_service``, ``get_profile_service``,
``get_schema_service``, ``get_auth_service``, ``get_export_service``) are
provided for easy dependency injection and test mocking via
``unittest.mock.patch``.

Example::

    # Direct class import
    from api_gateway.services import JobService
    service = JobService()

    # Factory function import (preferred for test mocking)
    from api_gateway.services import get_job_service
    service = get_job_service()
"""

from __future__ import annotations

from api_gateway.services.auth_service import AuthService
from api_gateway.services.export_service import ExportService
from api_gateway.services.job_service import JobService
from api_gateway.services.profile_service import ProfileService
from api_gateway.services.schema_service import SchemaService

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
# Factory functions — enable easy mocking in unit tests via
# unittest.mock.patch('api_gateway.services.get_<name>_service')
# ---------------------------------------------------------------------------


def get_job_service() -> JobService:
    """Return a new :class:`JobService` instance.

    Factory function that creates a fresh ``JobService`` bound to the
    current Flask application context.  Designed for dependency injection
    in route handlers and easy mocking in unit tests.

    Returns:
        JobService: A newly constructed service instance ready for use
            within an active Flask application context.

    Example::

        from api_gateway.services import get_job_service

        service = get_job_service()
        job = service.create_job(
            tenant_id="tenant-abc",
            user_id="user-123",
            generation_method="statistical",
            schema_config={"tables": [...]},
            output_format="parquet",
        )
    """
    return JobService()


def get_profile_service() -> ProfileService:
    """Return a new :class:`ProfileService` instance.

    Factory function that creates a fresh ``ProfileService`` bound to the
    current Flask application context.  Designed for dependency injection
    in route handlers and easy mocking in unit tests.

    Returns:
        ProfileService: A newly constructed service instance ready for use
            within an active Flask application context.

    Example::

        from api_gateway.services import get_profile_service

        service = get_profile_service()
        profile = service.create_profile(
            tenant_id="tenant-abc",
            user_id="user-123",
            source_connection={"erp_type": "sap", ...},
            tables=["GL_ACCOUNTS"],
        )
    """
    return ProfileService()


def get_schema_service() -> SchemaService:
    """Return a new :class:`SchemaService` instance.

    Factory function that creates a fresh ``SchemaService`` bound to the
    current Flask application context.  Designed for dependency injection
    in route handlers and easy mocking in unit tests.

    Returns:
        SchemaService: A newly constructed service instance ready for use
            within an active Flask application context.

    Example::

        from api_gateway.services import get_schema_service

        service = get_schema_service()
        schema = service.discover_schema(
            tenant_id="tenant-abc",
            user_id="user-123",
            erp_type="sap",
            connection_config={"host": "erp.example.com"},
            modules=["financial_accounting"],
        )
    """
    return SchemaService()


def get_auth_service() -> AuthService:
    """Return a new :class:`AuthService` instance.

    Factory function that creates a fresh ``AuthService`` bound to the
    current Flask application context.  Designed for dependency injection
    in route handlers and easy mocking in unit tests.

    Returns:
        AuthService: A newly constructed service instance ready for use
            within an active Flask application context.

    Example::

        from api_gateway.services import get_auth_service

        auth = get_auth_service()
        login_url = auth.get_login_url(
            redirect_uri="https://app.example.com/callback",
        )
    """
    return AuthService()


def get_export_service() -> ExportService:
    """Return a new :class:`ExportService` instance.

    Factory function that creates a fresh ``ExportService`` bound to the
    current Flask application context.  Designed for dependency injection
    in route handlers and easy mocking in unit tests.

    Returns:
        ExportService: A newly constructed service instance ready for use
            within an active Flask application context.

    Example::

        from api_gateway.services import get_export_service

        service = get_export_service()
        export = service.create_export(
            tenant_id="tenant-abc",
            user_id="user-123",
            job_id="job-xyz-789",
            export_type="cloud_storage",
            target_config={"cloud_provider": "aws_s3", "bucket": "my-bucket"},
            output_format="parquet",
        )
    """
    return ExportService()
