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

All service classes enforce **multi-tenant isolation** per requirement R-007:
every public method accepts a ``tenant_id`` parameter and all data-access
operations are tenant-scoped.
"""

from api_gateway.services.export_service import ExportService


__all__: list[str] = [
    "ExportService",
]
