"""MongoDB document models package for the API Gateway service.

This package provides data-access objects (DAO) for the API Gateway's
MongoDB persistence layer.  Each model class wraps a single MongoDB
collection and exposes a class-method API for CRUD operations, indexing,
and tenant-scoped queries.

**Available models:**

- :class:`GenerationJob` — ``generation_profiles`` collection.  Manages the
  full lifecycle of synthetic-data generation jobs (creation, status
  transitions, progress tracking, quality scoring, compliance certification).
- :class:`User` — ``users`` collection.  Manages Auth0 user profiles,
  RBAC role assignments for the five platform roles (Platform Admin,
  Data Engineer, Developer, QA Engineer, Data Analyst), graduated
  permissions, tenant associations, and login tracking.

All models enforce **multi-tenant isolation** per requirement R-007: every
query and mutation includes a ``tenant_id`` filter so cross-tenant data
access is impossible by design.

Typical usage from API Gateway services or routes::

    from api_gateway.models import GenerationJob, User

    job = GenerationJob.find_by_id(job_id="...", tenant_id="...")
    user = User.find_by_email(email="...", tenant_id="...")
"""

from __future__ import annotations

from api_gateway.models.generation_job import (
    COLLECTION_NAME,
    GenerationJob,
    GenerationMethod,
    JobStatus,
    OutputFormat,
)
from api_gateway.models.user import User, UserRole, UserStatus


__all__: list[str] = [
    "COLLECTION_NAME",
    "GenerationJob",
    "GenerationMethod",
    "JobStatus",
    "OutputFormat",
    "User",
    "UserRole",
    "UserStatus",
]
