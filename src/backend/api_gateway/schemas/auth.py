"""Pydantic v2 request/response validation models for Auth0 authentication
and user profile domain.

This module defines the complete set of authentication and authorization
schemas used by the API Gateway's auth endpoints. It provides:

- **UserRole** enum: Five RBAC roles with graduated permission sets
  (Platform Admin, Data Engineer, Developer, QA Engineer, Data Analyst).
- **Permission** enum: Eighteen fine-grained permissions covering all
  platform capabilities (generation, profiling, schema, template, export,
  admin, compliance, quality).
- **ROLE_PERMISSIONS** mapping: Authoritative role-to-permission matrix
  that defines the graduated access control model.
- **LoginRequest / LoginResponse**: Auth0 OAuth 2.0 login flow models.
- **UserProfile**: Full user context including role, permissions, tenant,
  and account metadata.
- **TokenRefreshRequest / TokenRefreshResponse**: JWT RS256 refresh token
  rotation models.

All models use Pydantic v2 ``ConfigDict``, ``Field``, and ``field_validator``
for strict runtime validation compatible with JSON serialization over REST.

Typical usage::

    from src.backend.api_gateway.schemas.auth import (
        LoginResponse,
        UserProfile,
        TokenRefreshRequest,
        TokenRefreshResponse,
        UserRole,
        Permission,
        ROLE_PERMISSIONS,
    )
"""

from __future__ import annotations

import re
from datetime import datetime  # noqa: TC003 — required at runtime by Pydantic field resolution
from enum import StrEnum

from pydantic import BaseModel, ConfigDict, Field, field_validator


# ---------------------------------------------------------------------------
# Email validation pattern
# ---------------------------------------------------------------------------
# RFC 5322 simplified pattern sufficient for Auth0 email validation without
# pulling in the optional ``email-validator`` package dependency.
_EMAIL_REGEX = re.compile(
    r"^[a-zA-Z0-9.!#$%&'*+/=?^_`{|}~-]+"
    r"@[a-zA-Z0-9](?:[a-zA-Z0-9-]{0,61}[a-zA-Z0-9])?"
    r"(?:\.[a-zA-Z0-9](?:[a-zA-Z0-9-]{0,61}[a-zA-Z0-9])?)*$"
)


# ===================================================================
# Enumerations
# ===================================================================


class UserRole(StrEnum):
    """Enumeration of the five RBAC user roles supported by the platform.

    Each role maps to a graduated set of :class:`Permission` values via the
    :data:`ROLE_PERMISSIONS` constant.  Roles are stored as lowercase strings
    to ensure JSON-serialization compatibility with Pydantic v2.

    Attributes:
        PLATFORM_ADMIN: Full administrative access across all platform
            capabilities including user/tenant management and system settings.
        DATA_ENGINEER: Broad data-oriented access covering generation,
            profiling, schema discovery, templates, exports, quality, and
            read-only compliance.
        DEVELOPER: Focused access for generation creation/reading, read-only
            profiling and schema browsing, template reading, and export
            creation.
        QA_ENGINEER: Read-only access to generation jobs, quality reports,
            compliance data, and templates for validation workflows.
        DATA_ANALYST: Read-only access to generation results, statistical
            profiles, schemas, and quality reports for analysis workflows.
    """

    PLATFORM_ADMIN = "platform_admin"
    DATA_ENGINEER = "data_engineer"
    DEVELOPER = "developer"
    QA_ENGINEER = "qa_engineer"
    DATA_ANALYST = "data_analyst"


class Permission(StrEnum):
    """Fine-grained permission tokens used for RBAC authorization checks.

    Permissions follow a ``<domain>:<action>`` naming convention and are
    evaluated at the API Gateway middleware layer as well as within
    individual service endpoints.  Each permission is assigned to one or
    more :class:`UserRole` values through the :data:`ROLE_PERMISSIONS`
    mapping.

    Attributes:
        GENERATION_CREATE: Create new generation jobs.
        GENERATION_READ: View generation jobs and their results.
        GENERATION_DELETE: Cancel or delete generation jobs.
        PROFILE_CREATE: Initiate statistical profiling of ERP schemas.
        PROFILE_READ: View statistical profiles.
        SCHEMA_DISCOVER: Trigger ERP schema discovery.
        SCHEMA_READ: View discovered schema definitions.
        TEMPLATE_CREATE: Create reusable generation templates.
        TEMPLATE_READ: View generation templates.
        TEMPLATE_DELETE: Remove generation templates.
        EXPORT_CREATE: Initiate data export/provisioning.
        EXPORT_READ: View export job statuses and results.
        ADMIN_USERS: Manage platform user accounts.
        ADMIN_TENANTS: Manage tenant configurations and quotas.
        ADMIN_SYSTEM: Manage system-level settings and maintenance.
        COMPLIANCE_READ: View compliance reports and certifications.
        COMPLIANCE_MANAGE: Manage compliance policies and rules.
        QUALITY_READ: View data quality reports and scores.
    """

    GENERATION_CREATE = "generation:create"
    GENERATION_READ = "generation:read"
    GENERATION_DELETE = "generation:delete"
    PROFILE_CREATE = "profile:create"
    PROFILE_READ = "profile:read"
    SCHEMA_DISCOVER = "schema:discover"
    SCHEMA_READ = "schema:read"
    TEMPLATE_CREATE = "template:create"
    TEMPLATE_READ = "template:read"
    TEMPLATE_DELETE = "template:delete"
    EXPORT_CREATE = "export:create"
    EXPORT_READ = "export:read"
    ADMIN_USERS = "admin:users"
    ADMIN_TENANTS = "admin:tenants"
    ADMIN_SYSTEM = "admin:system"
    COMPLIANCE_READ = "compliance:read"
    COMPLIANCE_MANAGE = "compliance:manage"
    QUALITY_READ = "quality:read"


# ===================================================================
# Role → Permission mapping
# ===================================================================

ROLE_PERMISSIONS: dict[UserRole, list[Permission]] = {
    UserRole.PLATFORM_ADMIN: [
        # Platform Admin has unrestricted access to every permission.
        Permission.GENERATION_CREATE,
        Permission.GENERATION_READ,
        Permission.GENERATION_DELETE,
        Permission.PROFILE_CREATE,
        Permission.PROFILE_READ,
        Permission.SCHEMA_DISCOVER,
        Permission.SCHEMA_READ,
        Permission.TEMPLATE_CREATE,
        Permission.TEMPLATE_READ,
        Permission.TEMPLATE_DELETE,
        Permission.EXPORT_CREATE,
        Permission.EXPORT_READ,
        Permission.ADMIN_USERS,
        Permission.ADMIN_TENANTS,
        Permission.ADMIN_SYSTEM,
        Permission.COMPLIANCE_READ,
        Permission.COMPLIANCE_MANAGE,
        Permission.QUALITY_READ,
    ],
    UserRole.DATA_ENGINEER: [
        # Data Engineer: broad data-oriented access, no admin.
        Permission.GENERATION_CREATE,
        Permission.GENERATION_READ,
        Permission.GENERATION_DELETE,
        Permission.PROFILE_CREATE,
        Permission.PROFILE_READ,
        Permission.SCHEMA_DISCOVER,
        Permission.SCHEMA_READ,
        Permission.TEMPLATE_CREATE,
        Permission.TEMPLATE_READ,
        Permission.TEMPLATE_DELETE,
        Permission.EXPORT_CREATE,
        Permission.EXPORT_READ,
        Permission.QUALITY_READ,
        Permission.COMPLIANCE_READ,
    ],
    UserRole.DEVELOPER: [
        # Developer: create/read generation, read-only profiling/schemas,
        # template reading, export create/read.
        Permission.GENERATION_CREATE,
        Permission.GENERATION_READ,
        Permission.PROFILE_READ,
        Permission.SCHEMA_READ,
        Permission.TEMPLATE_READ,
        Permission.EXPORT_CREATE,
        Permission.EXPORT_READ,
    ],
    UserRole.QA_ENGINEER: [
        # QA Engineer: read-only generation, quality, compliance, templates.
        Permission.GENERATION_READ,
        Permission.QUALITY_READ,
        Permission.COMPLIANCE_READ,
        Permission.TEMPLATE_READ,
    ],
    UserRole.DATA_ANALYST: [
        # Data Analyst: read-only generation, profiles, schemas, quality.
        Permission.GENERATION_READ,
        Permission.PROFILE_READ,
        Permission.SCHEMA_READ,
        Permission.QUALITY_READ,
    ],
}
"""Authoritative mapping from each :class:`UserRole` to its granted
:class:`Permission` list.

This constant is the single source of truth for the graduated RBAC
permission model.  Middleware and service layers reference this mapping
to determine whether an authenticated user is authorized for a given
action.  The mapping adheres to the principle of least privilege:

- **PLATFORM_ADMIN** — All 18 permissions.
- **DATA_ENGINEER** — 14 permissions (no admin:*).
- **DEVELOPER** — 7 permissions (create/read generation, read-only others).
- **QA_ENGINEER** — 4 permissions (read-only validation-related).
- **DATA_ANALYST** — 4 permissions (read-only analysis-related).
"""


# ===================================================================
# Pydantic Models — Authentication
# ===================================================================


class LoginRequest(BaseModel):
    """Request model for initiating an Auth0 OAuth 2.0 / OpenID Connect
    login flow.

    This model captures the optional return URL that the frontend provides
    so the API Gateway can redirect the user back to the correct page after
    the Auth0 callback completes.

    Attributes:
        return_url: Optional URL to redirect the user to after successful
            Auth0 authentication.  When ``None``, the default dashboard
            URL is used.

    Example::

        request = LoginRequest(return_url="/generation/wizard")
    """

    return_url: str | None = Field(
        default=None,
        description="URL to redirect after login",
    )


class LoginResponse(BaseModel):
    """Response model returned after a successful Auth0 authentication
    callback.

    Contains the JWT access token, a refresh token for token rotation,
    the token type (always ``Bearer``), and the access token lifetime in
    seconds.

    Attributes:
        access_token: JWT RS256 access token issued by Auth0.  Used as a
            Bearer token in the ``Authorization`` header for all subsequent
            API calls.
        refresh_token: Opaque refresh token used for token rotation via the
            ``POST /api/v1/auth/token/refresh`` endpoint.
        token_type: OAuth 2.0 token type.  Always ``"Bearer"``.
        expires_in: Access token lifetime in seconds (e.g. ``3600`` for one
            hour).  The frontend should schedule a refresh before expiry.

    Example::

        response = LoginResponse(
            access_token="eyJhbGciOiJSUzI1NiIs...",
            refresh_token="v1.MjQ0YzJhNDAt...",
            token_type="Bearer",
            expires_in=3600,
        )
    """

    model_config = ConfigDict(from_attributes=True)

    access_token: str = Field(
        ...,
        min_length=1,
        description="JWT access token",
    )
    refresh_token: str = Field(
        ...,
        min_length=1,
        description="JWT refresh token for rotation",
    )
    token_type: str = Field(
        default="Bearer",
        description="Token type, always Bearer",
    )
    expires_in: int = Field(
        ...,
        gt=0,
        description="Access token expiry in seconds",
    )


# ===================================================================
# Pydantic Models — User Profile
# ===================================================================


class UserProfile(BaseModel):
    """Authenticated user profile model containing identity, role,
    permissions, and tenant context.

    Populated from Auth0 ``/userinfo`` and platform-specific claims after
    JWT validation.  The ``permissions`` field is typically derived from
    the user's ``role`` using :data:`ROLE_PERMISSIONS`, but can be
    overridden for fine-grained per-user permission grants.

    Attributes:
        user_id: Unique identifier for the user as provided by Auth0
            (e.g. ``"auth0|64f1e2..."``).
        email: Verified email address of the user.
        name: Human-readable display name.
        role: Assigned RBAC role from :class:`UserRole`.
        permissions: List of granted :class:`Permission` values, normally
            derived from the role but may include additional per-user grants.
        tenant_id: Namespace identifier for multi-tenant isolation.
        avatar_url: Optional URL to the user's profile picture.
        is_active: Whether the account is currently active.  Inactive
            accounts are denied API access.
        last_login: Timestamp of the user's most recent authentication, or
            ``None`` if they have never logged in.
        created_at: Account creation timestamp.

    Example::

        profile = UserProfile(
            user_id="auth0|64f1e2abc123",
            email="admin@acme.com",
            name="Jane Admin",
            role=UserRole.PLATFORM_ADMIN,
            permissions=ROLE_PERMISSIONS[UserRole.PLATFORM_ADMIN],
            tenant_id="tenant-acme-001",
            created_at=datetime.utcnow(),
        )
    """

    model_config = ConfigDict(from_attributes=True)

    user_id: str = Field(
        ...,
        min_length=1,
        description="Unique user identifier from Auth0",
    )
    email: str = Field(
        ...,
        description="User email address",
    )
    name: str = Field(
        ...,
        min_length=1,
        description="User display name",
    )
    role: UserRole = Field(
        ...,
        description="Assigned RBAC role",
    )
    permissions: list[Permission] = Field(
        default_factory=list,
        description="Derived permissions from role",
    )
    tenant_id: str = Field(
        ...,
        min_length=1,
        description="Assigned tenant namespace",
    )
    avatar_url: str | None = Field(
        default=None,
        description="Profile picture URL",
    )
    is_active: bool = Field(
        default=True,
        description="Account active status",
    )
    last_login: datetime | None = Field(
        default=None,
        description="Last login timestamp",
    )
    created_at: datetime = Field(
        ...,
        description="Account creation timestamp",
    )

    # ------------------------------------------------------------------
    # Validators
    # ------------------------------------------------------------------

    @field_validator("email")
    @classmethod
    def validate_email_format(cls, value: str) -> str:
        """Validate that the email address conforms to a standard format.

        Uses a simplified RFC 5322 regex pattern to verify the email
        without requiring the optional ``email-validator`` package.  This
        keeps the API Gateway dependency footprint minimal while still
        rejecting clearly malformed addresses.

        Args:
            value: Raw email string to validate.

        Returns:
            The validated email string (stripped of leading/trailing
            whitespace).

        Raises:
            ValueError: If the email does not match the expected format.
        """
        stripped = value.strip()
        if not stripped:
            raise ValueError("Email address must not be empty")
        if not _EMAIL_REGEX.match(stripped):
            raise ValueError(
                f"Invalid email format: '{stripped}' does not conform to "
                "the expected email address pattern"
            )
        return stripped


# ===================================================================
# Pydantic Models — Token Refresh
# ===================================================================


class TokenRefreshRequest(BaseModel):
    """Request model for refreshing an expired JWT access token.

    The client submits the previously issued refresh token.  Upon successful
    validation the server responds with a new access token and a rotated
    refresh token (one-time use) to prevent replay attacks.

    Attributes:
        refresh_token: The current (non-expired) refresh token that was
            issued alongside the previous access token.

    Example::

        request = TokenRefreshRequest(
            refresh_token="v1.MjQ0YzJhNDAt..."
        )
    """

    model_config = ConfigDict(strict=True)

    refresh_token: str = Field(
        ...,
        min_length=1,
        description="Current refresh token",
    )


class TokenRefreshResponse(BaseModel):
    """Response model returned after a successful token refresh operation.

    Contains the newly issued JWT access token, a rotated refresh token
    (the previous one is invalidated), the token type, and the new access
    token lifetime.

    Attributes:
        access_token: Newly issued JWT RS256 access token.
        refresh_token: Rotated refresh token.  The previous refresh token
            is immediately invalidated.
        token_type: OAuth 2.0 token type.  Always ``"Bearer"``.
        expires_in: New access token lifetime in seconds.

    Example::

        response = TokenRefreshResponse(
            access_token="eyJhbGciOiJSUzI1NiIs...",
            refresh_token="v1.ZmQ3NjRkMjAt...",
            token_type="Bearer",
            expires_in=3600,
        )
    """

    model_config = ConfigDict(from_attributes=True)

    access_token: str = Field(
        ...,
        min_length=1,
        description="New JWT access token",
    )
    refresh_token: str = Field(
        ...,
        min_length=1,
        description="Rotated refresh token",
    )
    token_type: str = Field(
        default="Bearer",
        description="Token type",
    )
    expires_in: int = Field(
        ...,
        gt=0,
        description="New expiry in seconds",
    )
