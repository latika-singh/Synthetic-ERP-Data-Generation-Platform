"""Pydantic v2 request/response validation models for all API Gateway domains.

This package centralises Pydantic v2 schema definitions used by the API Gateway
route handlers and service layer to validate incoming HTTP requests and serialise
outgoing responses.  Each domain module covers a specific functional area:

Modules:
    generation: Generation job request/response models and the ``JobStatus`` enum
        for the ``POST /api/v1/generation/jobs`` and ``GET /api/v1/generation/jobs``
        endpoints.
    profile: Statistical profiling request/response models and the
        ``StatisticalSummary`` model for the ``POST /api/v1/profiles`` and
        ``GET /api/v1/profiles/{id}`` endpoints.
    schema: ERP schema discovery request/response models for the
        ``POST /api/v1/schemas/discover`` and ``GET /api/v1/schemas/{id}``
        endpoints.
    template: Generation template CRUD request/response models for the
        template library endpoints.
    auth: Auth0-based authentication models — login response, user profile,
        and token refresh request/response pairs.
    export: Export and provisioning request/response models including database
        and cloud storage target configurations.

All key models are re-exported from this ``__init__`` module so that consumers
can import directly from the package root::

    from src.backend.api_gateway.schemas import (
        GenerationJobRequest,
        GenerationJobResponse,
        JobStatus,
        ProfileRequest,
        ProfileResponse,
        StatisticalSummary,
        SchemaDiscoveryRequest,
        SchemaDefinition,
        TemplateRequest,
        TemplateResponse,
        LoginResponse,
        UserProfile,
        TokenRefreshRequest,
        TokenRefreshResponse,
        ExportRequest,
        ExportResponse,
        ProvisioningConfig,
    )
"""

# ---------------------------------------------------------------------------
# Generation domain models
# ---------------------------------------------------------------------------
from .generation import (
    GenerationJobRequest,
    GenerationJobResponse,
    JobStatus,
)

# ---------------------------------------------------------------------------
# Profiling domain models
# ---------------------------------------------------------------------------
from .profile import (
    ProfileRequest,
    ProfileResponse,
    StatisticalSummary,
)

# ---------------------------------------------------------------------------
# ERP schema discovery domain models
# ---------------------------------------------------------------------------
from .schema import (
    SchemaDefinition,
    SchemaDiscoveryRequest,
)

# ---------------------------------------------------------------------------
# Template domain models
# ---------------------------------------------------------------------------
from .template import (
    TemplateRequest,
    TemplateResponse,
)

# ---------------------------------------------------------------------------
# Authentication domain models
# ---------------------------------------------------------------------------
from .auth import (
    LoginResponse,
    TokenRefreshRequest,
    TokenRefreshResponse,
    UserProfile,
)

# ---------------------------------------------------------------------------
# Export / provisioning domain models
# ---------------------------------------------------------------------------
from .export import (
    ExportRequest,
    ExportResponse,
    ProvisioningConfig,
)

# ---------------------------------------------------------------------------
# Public API surface
# ---------------------------------------------------------------------------
__all__: list[str] = [
    # Generation
    "GenerationJobRequest",
    "GenerationJobResponse",
    "JobStatus",
    # Profiling
    "ProfileRequest",
    "ProfileResponse",
    "StatisticalSummary",
    # Schema discovery
    "SchemaDiscoveryRequest",
    "SchemaDefinition",
    # Templates
    "TemplateRequest",
    "TemplateResponse",
    # Authentication
    "LoginResponse",
    "UserProfile",
    "TokenRefreshRequest",
    "TokenRefreshResponse",
    # Export / provisioning
    "ExportRequest",
    "ExportResponse",
    "ProvisioningConfig",
]
