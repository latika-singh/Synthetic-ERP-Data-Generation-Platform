"""MongoDB document model for user profiles and role mapping.

Provides the :class:`User` class for managing Auth0 user identities within
the Synthetic ERP Data Generation Platform.  Each user document stores:

- **Auth0 identity mapping** — ``auth0_user_id`` (Auth0 ``sub`` claim)
  linked to a platform-generated ``user_id`` (UUID).
- **RBAC role assignment** — One of five graduated roles (Platform Admin,
  Data Engineer, Developer, QA Engineer, Data Analyst) with deterministic
  permission sets per Section 6.4 Security Architecture.
- **Multi-tenant isolation** — Every query and mutation includes a mandatory
  ``tenant_id`` filter, ensuring cross-tenant user access is impossible by
  design (R-007).
- **Login tracking** — ``last_login`` timestamp and ``login_count`` for
  audit and analytics.
- **Lifecycle management** — Active / Inactive / Suspended status with
  soft-delete semantics preserving audit trails (C-004 SOC 2 Type II).

All methods enforce:

- Full Python 3.12+ type hints (R-013).
- Google-style docstrings (R-013).
- Structured logging without PII (R-005).
- Password / credential data is **NEVER** stored — Auth0 handles
  authentication.

Collections:
    ``users`` — Indexed on ``auth0_user_id`` (unique),
    ``(tenant_id, role)`` compound, ``email``, and ``status``.

Example::

    from api_gateway.models.user import User, UserRole

    # Create a new user from Auth0 callback
    user = User.create(
        auth0_user_id="auth0|abc123",
        email="engineer@company.com",
        tenant_id="tenant_001",
        role=UserRole.DATA_ENGINEER.value,
        name="Jane Engineer",
    )

    # Find user by tenant (R-007 isolation)
    result = User.find_by_tenant("tenant_001", role=UserRole.DATA_ENGINEER.value)
"""

from __future__ import annotations

import enum
import logging
import uuid
from datetime import datetime, timezone
from typing import Any, Optional

from flask import current_app
from pymongo import ASCENDING, DESCENDING, IndexModel, ReturnDocument
from pymongo.collection import Collection
from pymongo.errors import DuplicateKeyError

from api_gateway.extensions import get_db

# ---------------------------------------------------------------------------
# Module-level logger — works with the structlog ProcessorFormatter
# configured at application level for consistent JSON output.  Logs
# user_id and tenant_id only; never PII (R-005).
# ---------------------------------------------------------------------------
logger: logging.Logger = logging.getLogger(__name__)


# ===================================================================
# Enumerations
# ===================================================================


class UserRole(str, enum.Enum):
    """RBAC roles for the Synthetic ERP Data Generation Platform.

    Five graduated roles with increasing privilege levels as defined in
    Section 6.4 Security Architecture.  Each role maps to a deterministic
    permission set via :data:`ROLE_PERMISSIONS`.

    Inherits from ``str`` so that enum values serialise cleanly to/from
    MongoDB string fields without additional conversion.

    Attributes:
        PLATFORM_ADMIN: Full system access including user/tenant management,
            monitoring, and all generation/export capabilities.
        DATA_ENGINEER: Generation lifecycle management, profiling, schema
            discovery, template management, and export.
        DEVELOPER: Generation creation and read, template read, and export.
        QA_ENGINEER: Quality review, compliance certification, generation
            read, and template read.
        DATA_ANALYST: Read-only access across generation, profiles, schemas,
            templates, exports, and quality reports.
    """

    PLATFORM_ADMIN = "platform_admin"
    DATA_ENGINEER = "data_engineer"
    DEVELOPER = "developer"
    QA_ENGINEER = "qa_engineer"
    DATA_ANALYST = "data_analyst"


class UserStatus(str, enum.Enum):
    """User account lifecycle states.

    Supports soft-delete semantics (``INACTIVE`` instead of hard deletion)
    to maintain audit trails per SOC 2 Type II compliance (C-004).

    Attributes:
        ACTIVE: User can authenticate and access resources normally.
        INACTIVE: Soft-deleted; cannot authenticate but audit trail is
            preserved for compliance.
        SUSPENDED: Temporarily restricted; may be reactivated by an
            administrator.
    """

    ACTIVE = "active"
    INACTIVE = "inactive"
    SUSPENDED = "suspended"


# ===================================================================
# Permission Mapping — Section 6.4 Security Architecture
# ===================================================================


ROLE_PERMISSIONS: dict[UserRole, list[str]] = {
    UserRole.PLATFORM_ADMIN: [
        "generation:*",
        "profile:*",
        "schema:*",
        "template:*",
        "export:*",
        "quality:*",
        "compliance:*",
        "admin:*",
        "monitoring:*",
    ],
    UserRole.DATA_ENGINEER: [
        "generation:create",
        "generation:read",
        "generation:delete",
        "profile:create",
        "profile:read",
        "schema:discover",
        "schema:read",
        "template:create",
        "template:read",
        "template:delete",
        "export:create",
        "export:read",
        "quality:read",
        "compliance:read",
    ],
    UserRole.DEVELOPER: [
        "generation:create",
        "generation:read",
        "profile:read",
        "schema:read",
        "template:read",
        "export:create",
        "export:read",
    ],
    UserRole.QA_ENGINEER: [
        "generation:read",
        "quality:read",
        "compliance:read",
        "compliance:certify",
        "template:read",
        "export:read",
    ],
    UserRole.DATA_ANALYST: [
        "generation:read",
        "profile:read",
        "schema:read",
        "template:read",
        "export:read",
        "quality:read",
    ],
}
"""Mapping from :class:`UserRole` to granted permission strings.

Each permission follows the ``<domain>:<action>`` convention.  Wildcard
permissions (``<domain>:*``) grant all actions within that domain.  The
RBAC middleware resolves these at request time to determine access.
"""


# ===================================================================
# Constants
# ===================================================================

COLLECTION_NAME: str = "users"
"""MongoDB collection name for user documents."""


# ===================================================================
# User Document Model
# ===================================================================


class User:
    """MongoDB document model for user profiles and RBAC role mapping.

    Provides class-level CRUD operations against the ``users`` MongoDB
    collection via PyMongo 4.x.  All query and mutation methods enforce
    multi-tenant isolation (R-007) by requiring and filtering on
    ``tenant_id``, with the sole exception of :meth:`find_by_auth0_id`
    which uses the globally unique Auth0 ``sub`` claim.

    This class **never** stores passwords or credentials — Auth0 handles
    all authentication concerns.  Only metadata, role assignments, and
    platform-specific preferences are persisted.

    The class accesses MongoDB via :func:`~api_gateway.extensions.get_db`,
    which resolves the database from ``current_app.extensions['mongodb_db']``
    when inside a Flask request context, or falls back to the shared
    singleton outside of one.

    Attributes:
        COLLECTION_NAME: The MongoDB collection name (``'users'``).
    """

    COLLECTION_NAME: str = COLLECTION_NAME

    # ------------------------------------------------------------------
    # Collection access
    # ------------------------------------------------------------------

    @classmethod
    def get_collection(cls) -> Collection:
        """Return the PyMongo :class:`~pymongo.collection.Collection` for
        user documents.

        Resolves the MongoDB database via :func:`get_db` (which in turn
        reads ``current_app.extensions['mongodb_db']``) and returns the
        ``users`` collection handle.

        Returns:
            Collection: The ``users`` PyMongo Collection object, backed
                by the application's connection pool.

        Raises:
            RuntimeError: If no Flask application context is active and
                the shared MongoDB singleton is not initialised.

        Example::

            collection = User.get_collection()
            doc = collection.find_one({"user_id": "..."})
        """
        return get_db()[COLLECTION_NAME]

    # ------------------------------------------------------------------
    # Index management
    # ------------------------------------------------------------------

    @classmethod
    def ensure_indexes(cls, collection: Collection) -> None:
        """Create required indexes on the ``users`` collection.

        Indexes created:

        - **auth0_user_id** (unique, ascending) — Fast lookup during
          Auth0 login callback and prevents duplicate Auth0 identity
          registration.
        - **(tenant_id, role)** compound ascending — Efficient tenant-
          scoped role queries for admin user management screens.
        - **email** ascending — Email-based user search within a tenant.
        - **status** ascending — Filtering active / inactive users.

        All indexes are created with ``background=True`` to avoid blocking
        the MongoDB server during creation on a live collection.

        Args:
            collection: The PyMongo Collection to create indexes on.
                Typically obtained via :meth:`get_collection`.

        Example::

            collection = User.get_collection()
            User.ensure_indexes(collection)
        """
        indexes: list[IndexModel] = [
            IndexModel(
                [("auth0_user_id", ASCENDING)],
                unique=True,
                name="idx_auth0_user_id_unique",
                background=True,
            ),
            IndexModel(
                [("tenant_id", ASCENDING), ("role", ASCENDING)],
                name="idx_tenant_role",
                background=True,
            ),
            IndexModel(
                [("email", ASCENDING)],
                name="idx_email",
                background=True,
            ),
            IndexModel(
                [("status", ASCENDING)],
                name="idx_status",
                background=True,
            ),
        ]
        collection.create_indexes(indexes)
        logger.info(
            "user_indexes_ensured",
            extra={
                "collection": COLLECTION_NAME,
                "index_count": len(indexes),
            },
        )

    # ------------------------------------------------------------------
    # Create
    # ------------------------------------------------------------------

    @classmethod
    def create(
        cls,
        auth0_user_id: str,
        email: str,
        tenant_id: str,
        role: str,
        **kwargs: Any,
    ) -> dict:
        """Create a new user document in the ``users`` collection.

        Generates a UUID-based ``user_id``, validates the role against
        :class:`UserRole`, computes permissions from
        :data:`ROLE_PERMISSIONS`, and inserts the document.  If a
        document with the same ``auth0_user_id`` already exists (unique
        index violation), the existing document is returned instead of
        raising an error — this supports idempotent Auth0 callback
        handling.

        Args:
            auth0_user_id: The Auth0 ``sub`` claim — globally unique
                identifier from the identity provider.
            email: User email address from the Auth0 profile.
            tenant_id: Tenant namespace for multi-tenant isolation
                (R-007).
            role: One of the :class:`UserRole` string values
                (e.g. ``'data_engineer'``).
            **kwargs: Optional fields:

                - ``name`` (str): Full name from Auth0 profile.
                  Defaults to ``''``.
                - ``picture`` (str): Avatar URL from Auth0 profile.
                  Defaults to ``''``.
                - ``preferences`` (dict): User UI / notification
                  preferences.  Defaults to ``{}``.
                - ``metadata`` (dict): Additional Auth0
                  ``app_metadata``.  Defaults to ``{}``.

        Returns:
            dict: The created (or existing) user document with all
                fields populated.  The MongoDB ``_id`` is converted
                to a string.

        Raises:
            ValueError: If ``role`` is not a valid :class:`UserRole`
                string value.
        """
        # Validate role against the UserRole enumeration
        try:
            validated_role = UserRole(role)
        except ValueError:
            valid_roles = [r.value for r in UserRole]
            raise ValueError(
                f"Invalid role '{role}'. Must be one of: {valid_roles}"
            )

        # Compute permissions for the validated role
        permissions: list[str] = list(ROLE_PERMISSIONS.get(validated_role, []))

        now: datetime = datetime.now(timezone.utc)
        user_id: str = str(uuid.uuid4())

        document: dict[str, Any] = {
            "user_id": user_id,
            "auth0_user_id": auth0_user_id,
            "email": email,
            "name": kwargs.get("name", ""),
            "picture": kwargs.get("picture", ""),
            "tenant_id": tenant_id,
            "role": validated_role.value,
            "permissions": permissions,
            "status": UserStatus.ACTIVE.value,
            "last_login": now,
            "login_count": 1,
            "preferences": kwargs.get("preferences", {}),
            "created_at": now,
            "updated_at": now,
            "metadata": kwargs.get("metadata", {}),
        }

        collection: Collection = cls.get_collection()

        try:
            collection.insert_one(document)
            # Remove MongoDB internal ObjectId for clean serialisation
            document.pop("_id", None)
            logger.info(
                "user_created",
                extra={
                    "user_id": user_id,
                    "tenant_id": tenant_id,
                    "role": validated_role.value,
                },
            )
            return document

        except DuplicateKeyError:
            # Auth0 user already registered — return the existing document
            # rather than surfacing an error (idempotent callback support).
            logger.warning(
                "user_creation_duplicate_auth0_id",
                extra={
                    "tenant_id": tenant_id,
                    "role": validated_role.value,
                },
            )
            existing: Optional[dict] = collection.find_one(
                {"auth0_user_id": auth0_user_id}
            )
            if existing is not None:
                existing["_id"] = str(existing["_id"])
                return existing
            # Fallback: return the document we attempted to insert
            # (should not happen in practice).
            return document

    # ------------------------------------------------------------------
    # Read operations
    # ------------------------------------------------------------------

    @classmethod
    def find_by_id(cls, user_id: str, tenant_id: str) -> Optional[dict]:
        """Find a user by platform ``user_id`` within a tenant.

        Enforces multi-tenant isolation (R-007) by requiring both
        ``user_id`` and ``tenant_id`` in the query filter.  A user
        belonging to tenant A cannot be retrieved by tenant B.

        Args:
            user_id: The platform-generated UUID for the user.
            tenant_id: The tenant namespace — cross-tenant lookups are
                impossible by design.

        Returns:
            dict or None: The user document with ``_id`` converted to
                a string, or ``None`` if no matching document exists.
        """
        collection: Collection = cls.get_collection()
        doc: Optional[dict] = collection.find_one(
            {"user_id": user_id, "tenant_id": tenant_id}
        )
        if doc is not None:
            doc["_id"] = str(doc["_id"])
        return doc

    @classmethod
    def find_by_auth0_id(cls, auth0_user_id: str) -> Optional[dict]:
        """Find a user by their Auth0 ``sub`` claim.

        Used during the Auth0 login callback to map an Auth0 identity to
        a platform user.  No ``tenant_id`` filter is applied because
        Auth0 identifiers are globally unique across all tenants.

        Args:
            auth0_user_id: The Auth0 ``sub`` claim
                (e.g. ``'auth0|abc123'``).

        Returns:
            dict or None: The user document with ``_id`` converted to
                a string, or ``None`` if no user is registered with
                the given Auth0 identity.
        """
        collection: Collection = cls.get_collection()
        doc: Optional[dict] = collection.find_one(
            {"auth0_user_id": auth0_user_id}
        )
        if doc is not None:
            doc["_id"] = str(doc["_id"])
        return doc

    @classmethod
    def find_by_email(cls, email: str, tenant_id: str) -> Optional[dict]:
        """Find a user by email address within a tenant.

        Enforces multi-tenant isolation (R-007) by filtering on both
        ``email`` and ``tenant_id``.

        Args:
            email: The user's email address.
            tenant_id: The tenant namespace for isolation.

        Returns:
            dict or None: The user document with ``_id`` converted to
                a string, or ``None`` if not found.
        """
        collection: Collection = cls.get_collection()
        doc: Optional[dict] = collection.find_one(
            {"email": email, "tenant_id": tenant_id}
        )
        if doc is not None:
            doc["_id"] = str(doc["_id"])
        return doc

    @classmethod
    def find_by_tenant(
        cls,
        tenant_id: str,
        role: Optional[str] = None,
        status: Optional[str] = None,
        page: int = 1,
        page_size: int = 20,
    ) -> dict:
        """List users belonging to a tenant with optional filtering.

        **Always** scopes the query to the given ``tenant_id`` (R-007).
        Supports optional filtering by role and / or status, plus
        cursor-style pagination via skip / limit.  Results are sorted
        by ``created_at`` in descending order (newest first).

        Args:
            tenant_id: The tenant namespace — only users in this tenant
                are returned.
            role: Optional role filter (e.g. ``'data_engineer'``).
                When ``None``, all roles are included.
            status: Optional status filter (e.g. ``'active'``).
                When ``None``, all statuses are included.
            page: Page number, 1-indexed.  Defaults to ``1``.
            page_size: Maximum items per page.  Defaults to ``20``.

        Returns:
            dict: Paginated result containing:

                - ``items`` (list[dict]): User documents for the
                  current page.
                - ``total`` (int): Total matching documents across
                  all pages.
                - ``page`` (int): Current page number.
                - ``page_size`` (int): Requested page size.
                - ``has_next`` (bool): ``True`` if more pages exist.
        """
        collection: Collection = cls.get_collection()

        # Build query filter — always tenant-scoped (R-007)
        query: dict[str, Any] = {"tenant_id": tenant_id}
        if role is not None:
            query["role"] = role
        if status is not None:
            query["status"] = status

        # Total count for pagination metadata
        total: int = collection.count_documents(query)

        # Calculate skip offset (clamp page to >= 1)
        effective_page: int = max(page, 1)
        skip: int = (effective_page - 1) * page_size

        # Execute paginated query sorted by created_at descending
        cursor = (
            collection.find(query)
            .sort("created_at", DESCENDING)
            .skip(skip)
            .limit(page_size)
        )

        items: list[dict] = []
        for doc in cursor:
            doc["_id"] = str(doc["_id"])
            items.append(doc)

        has_next: bool = (skip + page_size) < total

        return {
            "items": items,
            "total": total,
            "page": effective_page,
            "page_size": page_size,
            "has_next": has_next,
        }

    # ------------------------------------------------------------------
    # Update operations
    # ------------------------------------------------------------------

    @classmethod
    def update_role(
        cls,
        user_id: str,
        tenant_id: str,
        new_role: str,
    ) -> Optional[dict]:
        """Update a user's RBAC role and recompute permissions.

        Validates the new role against :class:`UserRole`, recalculates
        the permission set from :data:`ROLE_PERMISSIONS`, and persists
        the change atomically.  Logs the role change as an auditable
        event per SOC 2 Type II (C-004).

        Args:
            user_id: The platform user UUID.
            tenant_id: Tenant namespace for isolation (R-007).
            new_role: The new role value (e.g. ``'developer'``).

        Returns:
            dict or None: The updated document with ``_id`` as string,
                or ``None`` if the user was not found within the
                specified tenant.

        Raises:
            ValueError: If ``new_role`` is not a valid
                :class:`UserRole` value.
        """
        # Validate the new role
        try:
            validated_role = UserRole(new_role)
        except ValueError:
            valid_roles = [r.value for r in UserRole]
            raise ValueError(
                f"Invalid role '{new_role}'. Must be one of: {valid_roles}"
            )

        permissions: list[str] = list(ROLE_PERMISSIONS.get(validated_role, []))
        now: datetime = datetime.now(timezone.utc)

        collection: Collection = cls.get_collection()
        doc: Optional[dict] = collection.find_one_and_update(
            {"user_id": user_id, "tenant_id": tenant_id},
            {
                "$set": {
                    "role": validated_role.value,
                    "permissions": permissions,
                    "updated_at": now,
                }
            },
            return_document=ReturnDocument.AFTER,
        )

        if doc is not None:
            doc["_id"] = str(doc["_id"])
            logger.info(
                "user_role_updated",
                extra={
                    "user_id": user_id,
                    "tenant_id": tenant_id,
                    "new_role": validated_role.value,
                },
            )

        return doc

    @classmethod
    def update_last_login(cls, auth0_user_id: str) -> Optional[dict]:
        """Record a successful login for the given Auth0 identity.

        Updates ``last_login`` to the current UTC timestamp, increments
        ``login_count`` by 1 using the MongoDB ``$inc`` operator, and
        sets ``updated_at``.  Called from the Auth0 login callback on
        each successful authentication.

        Args:
            auth0_user_id: The Auth0 ``sub`` claim identifying the user.

        Returns:
            dict or None: The updated document with ``_id`` as string,
                or ``None`` if no user is registered with the given
                Auth0 identity.
        """
        now: datetime = datetime.now(timezone.utc)
        collection: Collection = cls.get_collection()

        doc: Optional[dict] = collection.find_one_and_update(
            {"auth0_user_id": auth0_user_id},
            {
                "$set": {
                    "last_login": now,
                    "updated_at": now,
                },
                "$inc": {
                    "login_count": 1,
                },
            },
            return_document=ReturnDocument.AFTER,
        )

        if doc is not None:
            doc["_id"] = str(doc["_id"])
            logger.info(
                "user_login_recorded",
                extra={
                    "user_id": doc.get("user_id"),
                    "tenant_id": doc.get("tenant_id"),
                },
            )

        return doc

    @classmethod
    def update_profile(
        cls,
        user_id: str,
        tenant_id: str,
        updates: dict[str, Any],
    ) -> Optional[dict]:
        """Update whitelisted profile fields for a user.

        Only the following fields may be updated via this method:

        - ``name`` (str): Display name.
        - ``picture`` (str): Avatar URL.
        - ``preferences`` (dict): UI / notification settings.

        All other keys in ``updates`` are **silently ignored** to prevent
        privilege escalation or accidental mutation of system fields
        (e.g. ``role``, ``permissions``, ``status``).

        Args:
            user_id: The platform user UUID.
            tenant_id: Tenant namespace for isolation (R-007).
            updates: Dictionary of field updates.  Only whitelisted
                keys are applied; all others are filtered out.

        Returns:
            dict or None: The updated document with ``_id`` as string,
                or ``None`` if the user was not found within the
                specified tenant.
        """
        allowed_fields: set[str] = {"name", "picture", "preferences"}
        filtered_updates: dict[str, Any] = {
            key: value
            for key, value in updates.items()
            if key in allowed_fields
        }

        if not filtered_updates:
            # Nothing to update — return current document unchanged
            return cls.find_by_id(user_id, tenant_id)

        filtered_updates["updated_at"] = datetime.now(timezone.utc)

        collection: Collection = cls.get_collection()
        doc: Optional[dict] = collection.find_one_and_update(
            {"user_id": user_id, "tenant_id": tenant_id},
            {"$set": filtered_updates},
            return_document=ReturnDocument.AFTER,
        )

        if doc is not None:
            doc["_id"] = str(doc["_id"])
            # Log updated field names only — never log field values (R-005)
            updated_keys: list[str] = [
                k for k in filtered_updates if k != "updated_at"
            ]
            logger.info(
                "user_profile_updated",
                extra={
                    "user_id": user_id,
                    "tenant_id": tenant_id,
                    "fields_updated": updated_keys,
                },
            )

        return doc

    @classmethod
    def deactivate(cls, user_id: str, tenant_id: str) -> Optional[dict]:
        """Soft-delete a user by setting status to ``INACTIVE``.

        Does **not** remove the document from MongoDB — preserves the
        complete audit trail per SOC 2 Type II compliance (C-004).  The
        user will no longer be able to authenticate, but their historical
        data remains intact for compliance reporting.

        Args:
            user_id: The platform user UUID.
            tenant_id: Tenant namespace for isolation (R-007).

        Returns:
            dict or None: The updated document with ``_id`` as string,
                or ``None`` if the user was not found within the
                specified tenant.
        """
        now: datetime = datetime.now(timezone.utc)
        collection: Collection = cls.get_collection()

        doc: Optional[dict] = collection.find_one_and_update(
            {"user_id": user_id, "tenant_id": tenant_id},
            {
                "$set": {
                    "status": UserStatus.INACTIVE.value,
                    "updated_at": now,
                }
            },
            return_document=ReturnDocument.AFTER,
        )

        if doc is not None:
            doc["_id"] = str(doc["_id"])
            logger.info(
                "user_deactivated",
                extra={
                    "user_id": user_id,
                    "tenant_id": tenant_id,
                },
            )

        return doc

    # ------------------------------------------------------------------
    # Counting / Statistics
    # ------------------------------------------------------------------

    @classmethod
    def count_by_tenant(
        cls,
        tenant_id: str,
        role: Optional[str] = None,
    ) -> int:
        """Count users belonging to a tenant, optionally filtered by role.

        Always scoped to ``tenant_id`` for multi-tenant isolation (R-007).

        Args:
            tenant_id: Tenant namespace.
            role: Optional role filter (e.g. ``'data_engineer'``).
                When ``None``, counts all roles.

        Returns:
            int: Number of matching user documents.
        """
        collection: Collection = cls.get_collection()
        query: dict[str, Any] = {"tenant_id": tenant_id}
        if role is not None:
            query["role"] = role
        return collection.count_documents(query)

    # ------------------------------------------------------------------
    # Permission helpers
    # ------------------------------------------------------------------

    @classmethod
    def get_permissions(cls, user_id: str, tenant_id: str) -> list[str]:
        """Retrieve the permission set for a user based on their role.

        Looks up the user document, extracts the ``role`` field, and
        returns the corresponding permission list from
        :data:`ROLE_PERMISSIONS`.  Used by the RBAC middleware for
        authorisation decisions at the API Gateway layer.

        Args:
            user_id: The platform user UUID.
            tenant_id: Tenant namespace for isolation (R-007).

        Returns:
            list[str]: Permission strings for the user's current role.
                Returns an empty list if the user is not found or the
                stored role value is unrecognised.
        """
        doc: Optional[dict] = cls.find_by_id(user_id, tenant_id)
        if doc is None:
            return []

        role_value: str = doc.get("role", "")
        try:
            user_role = UserRole(role_value)
        except ValueError:
            logger.warning(
                "user_unknown_role_in_permissions_lookup",
                extra={
                    "user_id": user_id,
                    "tenant_id": tenant_id,
                    "role": role_value,
                },
            )
            return []

        return list(ROLE_PERMISSIONS.get(user_role, []))
