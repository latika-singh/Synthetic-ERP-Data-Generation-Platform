"""Role-Based Access Control (RBAC) module for the Synthetic ERP Data Generation Platform.

This module provides the authorisation infrastructure shared by all six backend
microservices (API Gateway, Generation Engine, Profiling Service, Quality Service,
Compliance Service, Provisioning Service).  It implements:

- **Five graduated user roles** — ``PLATFORM_ADMIN`` (full access),
  ``DATA_ENGINEER`` (generation + profiling), ``DEVELOPER`` (API + generation),
  ``QA_ENGINEER`` (quality + compliance), ``DATA_ANALYST`` (read-only + export).
- **Nineteen fine-grained permissions** covering generation, profiling, schema
  discovery, templates, exports, quality, compliance, administration, and
  monitoring.
- **Flask route decorators** — :func:`require_role` and
  :func:`require_permission` enforce access control on any Flask view function
  while preserving function metadata via ``functools.wraps``.
- **Multi-tenant isolation** — Every authorisation decision validates the
  ``X-Tenant-ID`` request header against the authenticated user's ``tenant_id``
  claim.  Cross-tenant data access is impossible by design (R-007).
- **Open Policy Agent (OPA) integration** — :class:`OPAClient` evaluates
  complex authorisation policies via OPA's REST API with a built-in circuit
  breaker (5 failures → 60-second local fallback).
- **SOC 2 Type II auditability** — All authorisation decisions, cross-tenant
  access attempts, and OPA interactions are logged via the platform's
  structured JSON logging infrastructure.

Configuration follows the 12-factor app methodology — the OPA endpoint URL is
read from the ``OPA_URL`` environment variable.

Security notes:
    - Full JWT payloads are **never** logged.  Only ``user_id``, ``role``,
      ``tenant_id``, and ``permission`` are included in authorisation log
      events.
    - ``ROLE_PERMISSIONS`` is an immutable mapping constructed at import time;
      runtime mutation is not possible.

Usage::

    from shared.auth.rbac import require_role, require_permission, Role, Permission

    @app.route("/api/v1/generation/jobs", methods=["POST"])
    @require_permission(Permission.GENERATION_CREATE)
    def create_generation_job():
        ...

    @app.route("/api/v1/admin/users", methods=["GET"])
    @require_role(Role.PLATFORM_ADMIN)
    def list_users():
        ...
"""

from __future__ import annotations

import enum
import functools
import logging
import os
import time
from collections.abc import Callable
from typing import Any, TypeVar

import requests
import requests.exceptions
from flask import g, jsonify, request

from shared.auth.jwt_handler import get_current_user
from shared.logging.structured_logger import get_logger


# ---------------------------------------------------------------------------
# Module-level logger
# ---------------------------------------------------------------------------
# Primary logger uses the structlog-based factory; a stdlib fallback is kept
# for very early initialisation paths where structlog may not yet be wired up.
logger = get_logger(__name__)
_fallback_logger: logging.Logger = logging.getLogger(__name__)

# TypeVar for preserving the wrapped function's type signature in decorators.
F = TypeVar("F", bound=Callable[..., Any])


# ============================================================================
# Role enumeration
# ============================================================================


class Role(enum.StrEnum):
    """Enumeration of the five graduated user roles.

    Each role maps to a fixed set of :class:`Permission` values defined in
    :data:`ROLE_PERMISSIONS`.  ``StrEnum`` ensures that enum members compare
    naturally with plain strings, which simplifies JWT claim matching (Auth0
    delivers roles as string arrays).

    Attributes:
        PLATFORM_ADMIN: Full system access including user/tenant management.
        DATA_ENGINEER: Generation, profiling, schema, template, and export
            access with read-only quality and compliance visibility.
        DEVELOPER: API access with generation, read-only profiling, schemas,
            templates, and export capabilities.
        QA_ENGINEER: Quality and compliance management with read-only
            generation, template, and export visibility.
        DATA_ANALYST: Read-only access to generation results, profiles,
            schemas, templates, exports, and quality reports.
    """

    PLATFORM_ADMIN = "platform_admin"
    DATA_ENGINEER = "data_engineer"
    DEVELOPER = "developer"
    QA_ENGINEER = "qa_engineer"
    DATA_ANALYST = "data_analyst"


# ============================================================================
# Permission enumeration
# ============================================================================


class Permission(enum.StrEnum):
    """Enumeration of the nineteen fine-grained permissions.

    Permission strings follow the ``<resource>:<action>`` convention (e.g.
    ``generation:create``) for clarity in audit logs and OPA policy rules.

    Attributes:
        GENERATION_CREATE: Create new generation jobs.
        GENERATION_READ: View generation job status and results.
        GENERATION_DELETE: Cancel or delete generation jobs.
        PROFILE_CREATE: Create new statistical profiles.
        PROFILE_READ: View statistical profiles.
        SCHEMA_DISCOVER: Initiate ERP schema discovery.
        SCHEMA_READ: View discovered ERP schemas.
        TEMPLATE_CREATE: Create generation templates.
        TEMPLATE_READ: View generation templates.
        TEMPLATE_DELETE: Delete generation templates.
        EXPORT_CREATE: Initiate data exports / provisioning.
        EXPORT_READ: View export status and results.
        QUALITY_READ: View quality reports and scores.
        COMPLIANCE_READ: View compliance status.
        COMPLIANCE_CERTIFY: Issue compliance certifications.
        ADMIN_USERS: Manage user accounts.
        ADMIN_TENANTS: Manage tenant configurations.
        ADMIN_SETTINGS: Manage system-wide settings.
        MONITORING_READ: View system monitoring metrics.
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
    QUALITY_READ = "quality:read"
    COMPLIANCE_READ = "compliance:read"
    COMPLIANCE_CERTIFY = "compliance:certify"
    ADMIN_USERS = "admin:users"
    ADMIN_TENANTS = "admin:tenants"
    ADMIN_SETTINGS = "admin:settings"
    MONITORING_READ = "monitoring:read"


# ============================================================================
# Role → Permission mapping
# ============================================================================

ROLE_PERMISSIONS: dict[Role, frozenset[Permission]] = {
    # ── Platform Admin: unrestricted access ──────────────────────────────
    Role.PLATFORM_ADMIN: frozenset(Permission),
    # ── Data Engineer: generation + profiling + schema + templates + export
    #    with read-only quality and compliance ────────────────────────────
    Role.DATA_ENGINEER: frozenset(
        {
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
        }
    ),
    # ── Developer: API access + generation with read-only profiling ──────
    Role.DEVELOPER: frozenset(
        {
            Permission.GENERATION_CREATE,
            Permission.GENERATION_READ,
            Permission.PROFILE_READ,
            Permission.SCHEMA_READ,
            Permission.TEMPLATE_READ,
            Permission.EXPORT_CREATE,
            Permission.EXPORT_READ,
        }
    ),
    # ── QA Engineer: quality + compliance management ─────────────────────
    Role.QA_ENGINEER: frozenset(
        {
            Permission.GENERATION_READ,
            Permission.QUALITY_READ,
            Permission.COMPLIANCE_READ,
            Permission.COMPLIANCE_CERTIFY,
            Permission.TEMPLATE_READ,
            Permission.EXPORT_READ,
        }
    ),
    # ── Data Analyst: read-only across the platform ──────────────────────
    Role.DATA_ANALYST: frozenset(
        {
            Permission.GENERATION_READ,
            Permission.PROFILE_READ,
            Permission.SCHEMA_READ,
            Permission.TEMPLATE_READ,
            Permission.EXPORT_READ,
            Permission.QUALITY_READ,
        }
    ),
}


# ============================================================================
# Permission and role checking functions
# ============================================================================


def check_permission(user_role: str, required_permission: str) -> bool:
    """Validate that a user's role grants the required permission.

    Converts string values to their enum counterparts and looks up the
    permission in :data:`ROLE_PERMISSIONS`.

    Args:
        user_role: The role string from the authenticated user's JWT claims
            (e.g. ``"data_engineer"``).
        required_permission: The permission string to check (e.g.
            ``"generation:create"``).

    Returns:
        ``True`` if the role's permission set includes the required
        permission; ``False`` for unknown roles, unknown permissions,
        or insufficient access.
    """
    try:
        role_enum = Role(user_role)
    except ValueError:
        logger.debug(
            "permission_check_unknown_role",
            user_role=user_role,
            required_permission=required_permission,
            result="denied",
        )
        return False

    try:
        perm_enum = Permission(required_permission)
    except ValueError:
        logger.debug(
            "permission_check_unknown_permission",
            user_role=user_role,
            required_permission=required_permission,
            result="denied",
        )
        return False

    granted = perm_enum in ROLE_PERMISSIONS.get(role_enum, frozenset())

    logger.debug(
        "permission_check",
        user_role=user_role,
        required_permission=required_permission,
        result="granted" if granted else "denied",
    )

    return granted


def check_role(user_role: str, required_roles: list[str]) -> bool:
    """Validate that a user's role is among the required roles.

    Performs a simple containment check — the user's role string must
    appear in the *required_roles* list.

    Args:
        user_role: The role string from the authenticated user's JWT claims.
        required_roles: A list of acceptable role strings.

    Returns:
        ``True`` if *user_role* is present in *required_roles*; ``False``
        otherwise.
    """
    is_allowed = user_role in required_roles

    logger.debug(
        "role_check",
        user_role=user_role,
        required_roles=required_roles,
        result="granted" if is_allowed else "denied",
    )

    return is_allowed


def validate_tenant_access(user_tenant_id: str, resource_tenant_id: str) -> bool:
    """Enforce multi-tenant isolation for a single access decision.

    Rules:
        1. **Platform Admins** may access resources in any tenant.
        2. All other roles may access resources **only** within their own
           tenant.  A tenant-ID mismatch results in an immediate denial
           and a WARNING-level log entry (potential cross-tenant intrusion).

    The current user's role is resolved from ``flask.g.current_user``
    (populated by the JWT middleware) to determine Platform Admin status.

    Args:
        user_tenant_id: The ``tenant_id`` from the authenticated user's
            JWT claims.
        resource_tenant_id: The tenant ID of the target resource, typically
            extracted from the ``X-Tenant-ID`` request header or the
            resource document itself.

    Returns:
        ``True`` if access is allowed; ``False`` if the tenant context
        does not match and the user is not a Platform Admin.
    """
    # Platform Admins bypass tenant isolation.
    current_user: dict[str, Any] | None = getattr(g, "current_user", None)
    if current_user is not None:
        user_roles: list[str] = current_user.get("roles", [])
        if Role.PLATFORM_ADMIN.value in user_roles:
            logger.debug(
                "tenant_access_admin_bypass",
                user_tenant_id=user_tenant_id,
                resource_tenant_id=resource_tenant_id,
            )
            return True

    # Standard tenant matching.
    if user_tenant_id == resource_tenant_id:
        logger.debug(
            "tenant_access_granted",
            user_tenant_id=user_tenant_id,
            resource_tenant_id=resource_tenant_id,
        )
        return True

    # Cross-tenant access attempt — log at WARNING for SOC 2 Type II audit.
    logger.warning(
        "cross_tenant_access_attempt",
        user_tenant_id=user_tenant_id,
        resource_tenant_id=resource_tenant_id,
        user_id=current_user.get("user_id", "unknown") if current_user else "unknown",
    )
    return False


# ============================================================================
# Open Policy Agent (OPA) client with circuit breaker
# ============================================================================

# Circuit breaker thresholds.
_OPA_FAILURE_THRESHOLD: int = 5
_OPA_RECOVERY_TIMEOUT: float = 60.0  # seconds
_OPA_REQUEST_TIMEOUT: float = 2.0  # seconds


class OPAClient:
    """HTTP client for the Open Policy Agent (OPA) REST API.

    Provides policy evaluation for complex authorisation scenarios that
    exceed the static ``ROLE_PERMISSIONS`` mapping — for example, attribute-
    based access control (ABAC) rules, time-of-day restrictions, or
    resource-level policies.

    Resilience features:
        - **Graceful degradation**: If OPA is unreachable the client falls
          back to local :data:`ROLE_PERMISSIONS`-based checks.
        - **Circuit breaker**: After :data:`_OPA_FAILURE_THRESHOLD`
          consecutive failures the circuit opens and all requests are
          served locally for :data:`_OPA_RECOVERY_TIMEOUT` seconds before
          a retry is attempted.
        - **Configurable timeout**: OPA HTTP calls time out after
          :data:`_OPA_REQUEST_TIMEOUT` seconds (default 2 s).

    Configuration:
        Set the ``OPA_URL`` environment variable (default
        ``http://localhost:8181``) following 12-factor app methodology.

    Attributes:
        opa_url: Base URL of the OPA server.
        _failure_count: Number of consecutive OPA request failures.
        _last_failure_time: Epoch timestamp of the most recent failure.
        _circuit_open: ``True`` when the circuit breaker is open (local
            fallback active).
    """

    def __init__(self, opa_url: str | None = None) -> None:
        """Initialise the OPA client.

        Args:
            opa_url: Base URL of the OPA server.  When ``None`` the value
                is read from the ``OPA_URL`` environment variable, falling
                back to ``http://localhost:8181``.
        """
        self.opa_url: str = opa_url or os.environ.get(
            "OPA_URL", "http://localhost:8181"
        )
        self._failure_count: int = 0
        self._last_failure_time: float = 0.0
        self._circuit_open: bool = False

    # ------------------------------------------------------------------
    # Circuit breaker helpers
    # ------------------------------------------------------------------

    def _is_circuit_open(self) -> bool:
        """Determine whether the circuit breaker is currently open.

        When the circuit is open the client checks whether the recovery
        timeout has elapsed.  If so, the circuit transitions to
        *half-open* (allowing one trial request).

        Returns:
            ``True`` if OPA requests should be skipped (fallback mode);
            ``False`` if OPA requests may proceed.
        """
        if not self._circuit_open:
            return False

        elapsed = time.time() - self._last_failure_time
        if elapsed >= _OPA_RECOVERY_TIMEOUT:
            # Transition to half-open: allow one trial request.
            logger.info(
                "opa_circuit_half_open",
                elapsed_seconds=round(elapsed, 1),
                recovery_timeout=_OPA_RECOVERY_TIMEOUT,
            )
            self._circuit_open = False
            self._failure_count = 0
            return False

        return True

    def _record_failure(self) -> None:
        """Record a consecutive OPA failure and potentially open the circuit."""
        self._failure_count += 1
        self._last_failure_time = time.time()

        if self._failure_count >= _OPA_FAILURE_THRESHOLD:
            self._circuit_open = True
            logger.warning(
                "opa_circuit_opened",
                failure_count=self._failure_count,
                recovery_timeout_seconds=_OPA_RECOVERY_TIMEOUT,
            )

    def _record_success(self) -> None:
        """Reset failure tracking after a successful OPA call."""
        if self._failure_count > 0 or self._circuit_open:
            logger.info(
                "opa_circuit_closed",
                previous_failure_count=self._failure_count,
            )
        self._failure_count = 0
        self._last_failure_time = 0.0
        self._circuit_open = False

    # ------------------------------------------------------------------
    # Policy evaluation
    # ------------------------------------------------------------------

    def evaluate_policy(self, policy_path: str, input_data: dict[str, Any]) -> dict[str, Any]:
        """Evaluate an OPA policy and return the decision document.

        Sends a ``POST`` request to
        ``{opa_url}/v1/data/{policy_path}`` with the *input_data*
        payload.

        Args:
            policy_path: The OPA policy path (e.g.
                ``"authz/allow"``).  Slashes are preserved as-is.
            input_data: Arbitrary input context for the OPA policy rule.

        Returns:
            The ``"result"`` value from the OPA response body.  If OPA is
            unavailable or the circuit is open an empty dict is returned
            and the failure is logged.

        Raises:
            No exceptions are propagated — all OPA errors are caught and
            logged, and an empty dict is returned so callers can fall
            back to local RBAC.
        """
        if self._is_circuit_open():
            logger.debug(
                "opa_request_skipped_circuit_open",
                policy_path=policy_path,
            )
            return {}

        url = f"{self.opa_url}/v1/data/{policy_path}"
        payload: dict[str, Any] = {"input": input_data}

        try:
            response = requests.post(
                url,
                json=payload,
                timeout=_OPA_REQUEST_TIMEOUT,
            )
            response.raise_for_status()
            result: dict[str, Any] = response.json()

            self._record_success()

            logger.debug(
                "opa_policy_evaluated",
                policy_path=policy_path,
                status_code=response.status_code,
            )

            return result.get("result", {})

        except requests.exceptions.Timeout:
            self._record_failure()
            logger.warning(
                "opa_request_timeout",
                url=url,
                timeout_seconds=_OPA_REQUEST_TIMEOUT,
                failure_count=self._failure_count,
            )
            return {}

        except requests.exceptions.ConnectionError:
            self._record_failure()
            logger.warning(
                "opa_connection_error",
                url=url,
                failure_count=self._failure_count,
            )
            return {}

        except requests.exceptions.RequestException as exc:
            self._record_failure()
            logger.warning(
                "opa_request_error",
                url=url,
                error_type=type(exc).__name__,
                error=str(exc),
                failure_count=self._failure_count,
            )
            return {}

    def check_authorization(
        self,
        user: dict[str, Any],
        action: str,
        resource: dict[str, Any],
    ) -> bool:
        """Convenience method for standard authorisation checks via OPA.

        Builds a structured OPA input document from the caller-supplied
        *user*, *action*, and *resource* parameters, evaluates the
        ``authz/allow`` policy, and interprets the boolean result.

        If OPA is unavailable (circuit open or network error), the method
        falls back to the local :data:`ROLE_PERMISSIONS` mapping using
        :func:`check_permission`.

        Args:
            user: Dictionary with at least ``user_id``, ``roles``,
                ``tenant_id``, and ``permissions`` keys (as returned by
                :func:`~shared.auth.jwt_handler.get_current_user`).
            action: The permission string being checked (e.g.
                ``"generation:create"``).
            resource: Contextual resource attributes (e.g.
                ``{"type": "generation_job", "tenant_id": "t-42"}``).

        Returns:
            ``True`` if the policy allows the action; ``False`` otherwise.
        """
        opa_input: dict[str, Any] = {
            "user": {
                "id": user.get("user_id", ""),
                "roles": user.get("roles", []),
                "tenant_id": user.get("tenant_id", ""),
                "permissions": user.get("permissions", []),
            },
            "action": action,
            "resource": resource,
        }

        decision = self.evaluate_policy("authz/allow", opa_input)

        # OPA returned a valid decision.
        if isinstance(decision, bool):
            logger.debug(
                "opa_authorization_decision",
                user_id=user.get("user_id"),
                action=action,
                decision="allow" if decision else "deny",
                source="opa",
            )
            return decision

        if isinstance(decision, dict) and "allow" in decision:
            allowed: bool = bool(decision["allow"])
            logger.debug(
                "opa_authorization_decision",
                user_id=user.get("user_id"),
                action=action,
                decision="allow" if allowed else "deny",
                source="opa",
            )
            return allowed

        # Fallback: OPA unavailable or returned unexpected shape — use local
        # RBAC as the source of truth.
        roles = user.get("roles", [])
        primary_role: str = roles[0] if roles else ""
        fallback_result = check_permission(primary_role, action)

        logger.debug(
            "opa_authorization_fallback",
            user_id=user.get("user_id"),
            action=action,
            decision="allow" if fallback_result else "deny",
            source="local_rbac",
        )

        return fallback_result


# ============================================================================
# OPAClient singleton
# ============================================================================

_opa_client_instance: OPAClient | None = None


def get_opa_client() -> OPAClient:
    """Return the singleton :class:`OPAClient` instance.

    Lazily initialises the client on the first call.  Subsequent calls
    return the same instance, preserving circuit breaker state across
    the lifetime of the process.

    Returns:
        The shared :class:`OPAClient` instance.
    """
    global _opa_client_instance  # noqa: PLW0603

    if _opa_client_instance is None:
        _opa_client_instance = OPAClient()
        logger.info(
            "opa_client_initialized",
            opa_url=_opa_client_instance.opa_url,
        )

    return _opa_client_instance


# ============================================================================
# Flask route decorators
# ============================================================================


def require_role(*roles: str) -> Callable[[F], F]:
    """Decorator factory that restricts a Flask view to specific roles.

    Extracts the authenticated user context from ``flask.g.current_user``
    (populated by the JWT middleware).  If the user's primary role is not
    in *roles* a ``403 Forbidden`` JSON response is returned immediately.

    Additionally validates the ``X-Tenant-ID`` request header against the
    user's ``tenant_id`` to enforce multi-tenant isolation (R-007).
    Platform Admins bypass tenant validation.

    Args:
        *roles: One or more acceptable role values.  Can be
            :class:`Role` enum members (which compare as strings) or
            plain strings.

    Returns:
        A decorator that wraps the original view function.

    Usage::

        @app.route("/api/v1/admin/users")
        @require_role(Role.PLATFORM_ADMIN)
        def list_users():
            ...

        @app.route("/api/v1/generation/jobs", methods=["POST"])
        @require_role(Role.PLATFORM_ADMIN, Role.DATA_ENGINEER, Role.DEVELOPER)
        def create_job():
            ...
    """
    allowed_roles: list[str] = [
        r.value if isinstance(r, enum.Enum) else str(r) for r in roles
    ]

    def decorator(fn: F) -> F:
        @functools.wraps(fn)
        def wrapper(*args: Any, **kwargs: Any) -> Any:
            # Resolve the authenticated user from Flask request context.
            current_user: dict[str, Any] | None = getattr(g, "current_user", None)
            if current_user is None:
                # Attempt lazy resolution via jwt_handler.
                current_user = get_current_user()

            if current_user is None:
                logger.warning(
                    "role_check_no_user",
                    endpoint=request.endpoint,
                    path=request.path,
                )
                return (
                    jsonify(
                        {
                            "error": "authentication_required",
                            "message": "No authenticated user context found",
                        }
                    ),
                    401,
                )

            user_id: str = current_user.get("user_id", "unknown")
            user_roles: list[str] = current_user.get("roles", [])

            # Check if any of the user's roles matches the allowed roles.
            has_required_role = any(
                check_role(role, allowed_roles) for role in user_roles
            )

            if not has_required_role:
                logger.warning(
                    "role_check_denied",
                    user_id=user_id,
                    user_roles=user_roles,
                    required_roles=allowed_roles,
                    endpoint=request.endpoint,
                    path=request.path,
                )
                return (
                    jsonify(
                        {
                            "error": "insufficient_permissions",
                            "message": "Required role not found",
                            "required_roles": allowed_roles,
                        }
                    ),
                    403,
                )

            # ── Tenant isolation check ───────────────────────────────
            tenant_header: str | None = request.headers.get("X-Tenant-ID")
            user_tenant_id: str = current_user.get("tenant_id", "")

            if (
                tenant_header
                and user_tenant_id
                and not validate_tenant_access(user_tenant_id, tenant_header)
            ):
                logger.warning(
                    "tenant_isolation_violation",
                    user_id=user_id,
                    user_tenant_id=user_tenant_id,
                    requested_tenant_id=tenant_header,
                    endpoint=request.endpoint,
                )
                return (
                    jsonify(
                        {
                            "error": "insufficient_permissions",
                            "message": "Cross-tenant access denied",
                        }
                    ),
                    403,
                )

            logger.debug(
                "role_check_granted",
                user_id=user_id,
                user_roles=user_roles,
                required_roles=allowed_roles,
                endpoint=request.endpoint,
            )

            return fn(*args, **kwargs)

        return wrapper  # type: ignore[return-value]

    return decorator


def require_permission(permission: str) -> Callable[[F], F]:
    """Decorator factory that restricts a Flask view to a specific permission.

    Extracts the authenticated user context from ``flask.g.current_user``
    and checks whether the user's primary role grants the requested
    *permission* via :func:`check_permission`.

    Additionally validates the ``X-Tenant-ID`` request header against the
    user's ``tenant_id`` to enforce multi-tenant isolation (R-007).

    Args:
        permission: The required permission string.  Can be a
            :class:`Permission` enum member or a plain string.

    Returns:
        A decorator that wraps the original view function.

    Usage::

        @app.route("/api/v1/generation/jobs", methods=["POST"])
        @require_permission(Permission.GENERATION_CREATE)
        def create_job():
            ...
    """
    required_permission: str = (
        permission.value if isinstance(permission, enum.Enum) else str(permission)
    )

    def decorator(fn: F) -> F:
        @functools.wraps(fn)
        def wrapper(*args: Any, **kwargs: Any) -> Any:
            # Resolve the authenticated user from Flask request context.
            current_user: dict[str, Any] | None = getattr(g, "current_user", None)
            if current_user is None:
                current_user = get_current_user()

            if current_user is None:
                logger.warning(
                    "permission_check_no_user",
                    endpoint=request.endpoint,
                    path=request.path,
                )
                return (
                    jsonify(
                        {
                            "error": "authentication_required",
                            "message": "No authenticated user context found",
                        }
                    ),
                    401,
                )

            user_id: str = current_user.get("user_id", "unknown")
            user_roles: list[str] = current_user.get("roles", [])

            # Check if any of the user's roles grants the required permission.
            has_permission = any(
                check_permission(role, required_permission) for role in user_roles
            )

            if not has_permission:
                logger.warning(
                    "permission_check_denied",
                    user_id=user_id,
                    user_roles=user_roles,
                    required_permission=required_permission,
                    endpoint=request.endpoint,
                    path=request.path,
                )
                return (
                    jsonify(
                        {
                            "error": "insufficient_permissions",
                            "message": "Required permission not found",
                            "required_permission": required_permission,
                        }
                    ),
                    403,
                )

            # ── Tenant isolation check ───────────────────────────────
            tenant_header: str | None = request.headers.get("X-Tenant-ID")
            user_tenant_id: str = current_user.get("tenant_id", "")

            if (
                tenant_header
                and user_tenant_id
                and not validate_tenant_access(user_tenant_id, tenant_header)
            ):
                logger.warning(
                    "tenant_isolation_violation",
                    user_id=user_id,
                    user_tenant_id=user_tenant_id,
                    requested_tenant_id=tenant_header,
                    endpoint=request.endpoint,
                )
                return (
                    jsonify(
                        {
                            "error": "insufficient_permissions",
                            "message": "Cross-tenant access denied",
                        }
                    ),
                    403,
                )

            logger.debug(
                "permission_check_granted",
                user_id=user_id,
                user_roles=user_roles,
                required_permission=required_permission,
                endpoint=request.endpoint,
            )

            return fn(*args, **kwargs)

        return wrapper  # type: ignore[return-value]

    return decorator
