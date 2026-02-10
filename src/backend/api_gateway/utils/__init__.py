"""Shared utility modules for the API Gateway service.

This package provides reusable validation helpers for request parameter
validation and cursor-based pagination utilities for MongoDB queries. All
API Gateway route handlers, services, and middleware import from this
package for standardized input validation and paginated list responses.

Submodules:
    validators: UUID, ObjectId, ERP module, generation method, output format,
        email, tenant ID validation, and XSS-safe string sanitization.
    pagination: Cursor-based MongoDB pagination with base64-encoded cursor
        tokens, configurable page sizes, and PaginatedResponse model.

Typical usage::

    from api_gateway.utils import (
        validate_uuid,
        validate_object_id,
        validate_erp_module,
        sanitize_string,
    )

    if validate_uuid(job_id):
        clean_name = sanitize_string(user_input)
"""

from __future__ import annotations

# ---------------------------------------------------------------------------
# Re-export core validation functions from validators.py
# ---------------------------------------------------------------------------
from api_gateway.utils.validators import (
    VALID_CLOUD_PROVIDERS,
    VALID_DATABASE_TARGETS,
    VALID_ERP_MODULES,
    VALID_ERP_SYSTEMS,
    VALID_GENERATION_METHODS,
    VALID_OUTPUT_FORMATS,
    sanitize_string,
    validate_cloud_provider,
    validate_database_target,
    validate_email,
    validate_erp_module,
    validate_erp_system,
    validate_generation_method,
    validate_object_id,
    validate_output_format,
    validate_page_size,
    validate_sort_field,
    validate_tenant_id,
    validate_uuid,
)


# ---------------------------------------------------------------------------
# Re-export pagination utilities from pagination.py (lazy import)
# pagination.py may not exist yet during incremental project generation.
# ---------------------------------------------------------------------------
try:
    from api_gateway.utils.pagination import (  # type: ignore[import-not-found]
        PaginatedResponse,
        decode_cursor,
        encode_cursor,
        paginate_query,
    )

    _PAGINATION_AVAILABLE: bool = True
except ImportError:
    _PAGINATION_AVAILABLE = False

# ---------------------------------------------------------------------------
# Public API definition
# ---------------------------------------------------------------------------
__all__: list[str] = [
    # Constants (sorted alphabetically)
    "VALID_CLOUD_PROVIDERS",
    "VALID_DATABASE_TARGETS",
    "VALID_ERP_MODULES",
    "VALID_ERP_SYSTEMS",
    "VALID_GENERATION_METHODS",
    "VALID_OUTPUT_FORMATS",
    # Validation and utility functions (sorted alphabetically)
    "sanitize_string",
    "validate_cloud_provider",
    "validate_database_target",
    "validate_email",
    "validate_erp_module",
    "validate_erp_system",
    "validate_generation_method",
    "validate_object_id",
    "validate_output_format",
    "validate_page_size",
    "validate_sort_field",
    "validate_tenant_id",
    "validate_uuid",
]

# Conditionally add pagination exports if pagination module is available
if _PAGINATION_AVAILABLE:
    __all__.extend([
        "PaginatedResponse",
        "decode_cursor",
        "encode_cursor",
        "paginate_query",
    ])
