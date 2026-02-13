"""Flask Blueprint module for generation template CRUD REST endpoints.

Provides a complete set of RESTful API endpoints for managing reusable generation
templates in the Synthetic ERP Data Generation Platform.  Templates encapsulate
ERP module targeting, generation method selection, table configurations, and
parameter presets that power the Template Library (Screen S-003) and the
Generation Wizard (Screen S-002).

Endpoints:
    POST   /                       — Create a new generation template.
    GET    /                       — List/filter templates (paginated).
    GET    /<template_id>          — Retrieve a specific template by ID.
    PUT    /<template_id>          — Update an existing template.
    DELETE /<template_id>          — Delete a template.
    POST   /<template_id>/clone    — Clone a template into a new instance.

Security:
    - All endpoints require a valid JWT Bearer token via ``@jwt_required()``.
    - RBAC permission checks via ``@require_permissions()`` enforce:
        * ``template:create`` — POST create, PUT update, POST clone
        * ``template:read``   — GET list, GET by ID
        * ``template:delete`` — DELETE
    - Multi-tenant isolation (R-007): queries scope results to the
      authenticated tenant's namespace plus globally public templates.

Data Layer:
    Templates are persisted in the ``templates`` MongoDB collection as
    JSON-like documents with UUID-based ``template_id``, version tracking,
    usage counters, and UTC timestamps.

Usage::

    from api_gateway.routes.templates import templates_bp

    # In the application factory (app.py):
    app.register_blueprint(templates_bp, url_prefix="/api/v1/templates")
"""

from __future__ import annotations

import copy
import json as json_module
import uuid
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

from flask import Blueprint, Response, g, jsonify, request
from flask_jwt_extended import jwt_required
from pydantic import ValidationError

from api_gateway.extensions import get_db
from api_gateway.middleware.auth import require_permissions
from api_gateway.schemas.template import (
    TemplateListResponse,
    TemplateRequest,
    TemplateResponse,
)
from api_gateway.utils.pagination import (
    paginate_query,
    serialize_document,
    validate_page_size,
)
from shared.logging.structured_logger import get_logger

# ---------------------------------------------------------------------------
# Module-level logger — structured JSON output with correlation IDs.
# ---------------------------------------------------------------------------
logger = get_logger(__name__)

# ---------------------------------------------------------------------------
# Blueprint definition — registered by the application factory at
# ``/api/v1/templates``.
# ---------------------------------------------------------------------------
templates_bp: Blueprint = Blueprint("templates", __name__)

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------
TEMPLATES_COLLECTION: str = "templates"
"""Name of the MongoDB collection used for template document storage."""

PLATFORM_ADMIN_ROLE: str = "Platform Admin"
"""Role name that grants administrative override on ownership checks."""


# ---------------------------------------------------------------------------
# Private helpers
# ---------------------------------------------------------------------------


def _build_visibility_filter(tenant_id: str) -> Dict[str, Any]:
    """Construct a MongoDB ``$or`` filter enforcing multi-tenant visibility.

    Returns a filter that matches documents belonging to the authenticated
    tenant **or** marked as globally public.  This guarantees that users
    can never read templates owned by other tenants unless those templates
    have ``is_public=True`` (R-007).

    Args:
        tenant_id: The authenticated tenant's namespace identifier,
            extracted from ``g.tenant_id``.

    Returns:
        A MongoDB query fragment suitable for inclusion in an ``$and``
        condition list.
    """
    return {
        "$or": [
            {"tenant_id": tenant_id},
            {"is_public": True},
        ]
    }


def _is_owner_or_admin(
    template_doc: Dict[str, Any],
    user_id: str,
    user_roles: List[str],
) -> bool:
    """Check whether the current user is authorised to modify or delete a template.

    Modification rights are granted when the user is either the original
    creator of the template **or** holds the ``Platform Admin`` role.

    Args:
        template_doc: The MongoDB template document being checked.
        user_id: The ``sub`` claim from the JWT, identifying the current user.
        user_roles: The list of role names assigned to the current user.

    Returns:
        ``True`` if the user is authorised; ``False`` otherwise.
    """
    if PLATFORM_ADMIN_ROLE in user_roles:
        return True
    return template_doc.get("created_by") == user_id


def _sanitize_validation_errors(errors: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """Convert Pydantic v2 validation error dicts to a JSON-serializable form.

    Pydantic v2's ``ValidationError.errors()`` may include a ``ctx`` entry
    whose values are raw Python exception objects (e.g. ``ValueError``).
    Flask's ``jsonify`` cannot serialise those objects, so this helper
    stringifies any non-primitive values in the ``ctx`` dict and ensures
    the entire list is safe for JSON encoding.

    Args:
        errors: The list returned by ``ValidationError.errors()``.

    Returns:
        A new list of error dicts that is fully JSON-serializable.
    """
    sanitized: List[Dict[str, Any]] = []
    for err in errors:
        clean: Dict[str, Any] = {}
        for key, value in err.items():
            if key == "ctx" and isinstance(value, dict):
                # Stringify non-primitive context values (e.g. ValueError).
                clean[key] = {
                    k: str(v) if not isinstance(v, (str, int, float, bool, type(None))) else v
                    for k, v in value.items()
                }
            elif key == "url":
                # Pydantic v2 includes documentation URLs — keep as-is.
                clean[key] = str(value)
            else:
                clean[key] = value
        sanitized.append(clean)
    return sanitized


def _error_response(
    message: str,
    code: str,
    status: int,
    details: Any = None,
) -> tuple[Response, int]:
    """Build a consistent JSON error response.

    Args:
        message: Human-readable error description.
        code: Machine-readable error code (e.g. ``"NOT_FOUND"``).
        status: HTTP status code to return.
        details: Optional additional error context (validation errors, etc.).

    Returns:
        A ``(Response, status_code)`` tuple suitable for returning from a
        Flask route handler.
    """
    body: Dict[str, Any] = {"error": message, "code": code}
    if details is not None:
        body["details"] = details
    return jsonify(body), status


# ===================================================================
# Route handlers
# ===================================================================


@templates_bp.route("", methods=["POST"])
@jwt_required()
@require_permissions("template:create")
def create_template() -> tuple[Response, int]:
    """Create a new generation template.

    Accepts a JSON request body conforming to :class:`TemplateRequest`,
    validates all fields (name, category, ERP type, generation method,
    table configurations, and parameters), then persists the template
    document in MongoDB with a new UUID ``template_id``, version ``1``,
    and usage count ``0``.

    The ``tenant_id`` is always overridden from the authenticated JWT
    context to enforce namespace isolation (R-007).

    Returns:
        A JSON-serialised :class:`TemplateResponse` with HTTP 201 Created
        on success.

    Error Responses:
        400: Request body is missing or not valid JSON.
        422: Request body fails Pydantic schema validation.
        500: Unexpected server error during MongoDB insertion.
    """
    # ------------------------------------------------------------------
    # Parse request body
    # ------------------------------------------------------------------
    body: Optional[Dict[str, Any]] = request.get_json(silent=True)
    if not body:
        logger.warning(
            "template_create_missing_body",
            user_id=getattr(g, "user_id", "unknown"),
            tenant_id=getattr(g, "tenant_id", "unknown"),
        )
        return _error_response(
            "Request body is required and must be valid JSON",
            "BAD_REQUEST",
            400,
        )

    # ------------------------------------------------------------------
    # Validate with Pydantic schema
    # ------------------------------------------------------------------
    try:
        # Use model_validate_json for proper JSON-mode deserialization which
        # handles string-to-enum coercion even with strict=True model config.
        template_req: TemplateRequest = TemplateRequest.model_validate_json(
            json_module.dumps(body)
        )
    except ValidationError as exc:
        logger.warning(
            "template_create_validation_failed",
            user_id=getattr(g, "user_id", "unknown"),
            tenant_id=getattr(g, "tenant_id", "unknown"),
            error_count=len(exc.errors()),
        )
        return _error_response(
            "Template validation failed",
            "VALIDATION_ERROR",
            422,
            details=_sanitize_validation_errors(exc.errors()),
        )

    # ------------------------------------------------------------------
    # Extract authenticated context
    # ------------------------------------------------------------------
    tenant_id: str = getattr(g, "tenant_id", "")
    user_id: str = getattr(g, "user_id", "")

    # ------------------------------------------------------------------
    # Build MongoDB document
    # ------------------------------------------------------------------
    now: datetime = datetime.now(timezone.utc)
    template_id: str = str(uuid.uuid4())

    doc: Dict[str, Any] = template_req.model_dump()
    doc.update(
        {
            "template_id": template_id,
            "tenant_id": tenant_id,
            "created_by": user_id,
            "version": 1,
            "usage_count": 0,
            "created_at": now,
            "updated_at": now,
        }
    )

    # ------------------------------------------------------------------
    # Persist to MongoDB
    # ------------------------------------------------------------------
    try:
        db = get_db()
        collection = db[TEMPLATES_COLLECTION]
        collection.insert_one(doc)
    except Exception as exc:
        logger.error(
            "template_create_db_error",
            template_id=template_id,
            user_id=user_id,
            tenant_id=tenant_id,
            error=str(exc),
            error_type=type(exc).__name__,
        )
        return _error_response(
            "Failed to create template due to a server error",
            "INTERNAL_ERROR",
            500,
        )

    # ------------------------------------------------------------------
    # Build and return response
    # ------------------------------------------------------------------
    serialized: Dict[str, Any] = serialize_document(doc)
    response: TemplateResponse = TemplateResponse.model_validate(serialized)

    logger.info(
        "template_created",
        template_id=template_id,
        name=template_req.name,
        category=str(template_req.category),
        erp_type=template_req.erp_type,
        generation_method=template_req.generation_method,
        user_id=user_id,
        tenant_id=tenant_id,
    )

    return jsonify(response.model_dump(mode="json")), 201


@templates_bp.route("", methods=["GET"])
@jwt_required()
@require_permissions("template:read")
def list_templates() -> tuple[Response, int]:
    """List and filter generation templates with pagination.

    Returns a paginated list of templates visible to the authenticated
    user, supporting optional filtering by category, ERP type, generation
    method, tags, public visibility, and free-text search across the
    ``name`` and ``description`` fields.

    Results are sorted by ``updated_at`` in descending order (most
    recently modified first) and scoped to the tenant's own templates
    plus any globally public templates (R-007).

    Query Parameters:
        category (str): Filter by template category.
        erp_type (str): Filter by target ERP system type.
        generation_method (str): Filter by generation strategy.
        tags (str): Comma-separated tag list; matches templates containing
            **any** of the specified tags.
        is_public (str): ``"true"`` or ``"false"`` to filter by visibility.
        search (str): Free-text search on ``name`` and ``description``.
        page (int): Page number (1-indexed, default 1).
        page_size (int): Items per page (1–100, default 20).
        cursor (str): Optional cursor token for cursor-based pagination.

    Returns:
        A JSON-serialised :class:`TemplateListResponse` with HTTP 200.

    Error Responses:
        500: Unexpected server error during query execution.
    """
    # ------------------------------------------------------------------
    # Parse query parameters
    # ------------------------------------------------------------------
    category: Optional[str] = request.args.get("category")
    erp_type: Optional[str] = request.args.get("erp_type")
    generation_method: Optional[str] = request.args.get("generation_method")
    tags_raw: Optional[str] = request.args.get("tags")
    is_public_str: Optional[str] = request.args.get("is_public")
    search: Optional[str] = request.args.get("search")
    page: int = request.args.get("page", 1, type=int)
    page_size_raw: Optional[int] = request.args.get("page_size", type=int)
    cursor: Optional[str] = request.args.get("cursor")

    tenant_id: str = getattr(g, "tenant_id", "")
    validated_page_size: int = validate_page_size(page_size_raw)

    # Ensure page is at least 1
    if page < 1:
        page = 1

    # ------------------------------------------------------------------
    # Build MongoDB query filter with tenant isolation
    # ------------------------------------------------------------------
    conditions: List[Dict[str, Any]] = [_build_visibility_filter(tenant_id)]

    if category:
        conditions.append({"category": category})
    if erp_type:
        conditions.append({"erp_type": erp_type})
    if generation_method:
        conditions.append({"generation_method": generation_method})
    if tags_raw:
        tag_list: List[str] = [
            tag.strip() for tag in tags_raw.split(",") if tag.strip()
        ]
        if tag_list:
            conditions.append({"tags": {"$in": tag_list}})
    if is_public_str is not None:
        is_public: bool = is_public_str.lower() in ("true", "1", "yes")
        conditions.append({"is_public": is_public})
    if search:
        conditions.append(
            {
                "$or": [
                    {"name": {"$regex": search, "$options": "i"}},
                    {"description": {"$regex": search, "$options": "i"}},
                ]
            }
        )

    query_filter: Dict[str, Any] = (
        {"$and": conditions} if len(conditions) > 1 else conditions[0]
    )

    # ------------------------------------------------------------------
    # Execute query
    # ------------------------------------------------------------------
    try:
        db = get_db()
        collection = db[TEMPLATES_COLLECTION]

        if cursor:
            # Cursor-based pagination via shared pagination utility.
            paginated = paginate_query(
                collection=collection,
                query_filter=query_filter,
                page_size=validated_page_size,
                cursor=cursor,
                sort_field="updated_at",
                sort_order=-1,
            )
            template_models: List[TemplateResponse] = [
                TemplateResponse.model_validate(item) for item in paginated.items
            ]
            list_response: TemplateListResponse = TemplateListResponse.model_validate(
                {
                    "templates": [t.model_dump(mode="json") for t in template_models],
                    "total": paginated.total_count,
                    "page": page,
                    "page_size": paginated.page_size,
                }
            )
        else:
            # Page-based (offset / limit) pagination.
            skip: int = (page - 1) * validated_page_size
            total: int = collection.count_documents(query_filter)

            docs: List[Dict[str, Any]] = list(
                collection.find(query_filter)
                .sort("updated_at", -1)
                .skip(skip)
                .limit(validated_page_size)
            )

            serialized_docs: List[Dict[str, Any]] = [
                serialize_document(doc) for doc in docs
            ]
            template_models = [
                TemplateResponse.model_validate(sdoc) for sdoc in serialized_docs
            ]
            list_response = TemplateListResponse.model_validate(
                {
                    "templates": [t.model_dump(mode="json") for t in template_models],
                    "total": total,
                    "page": page,
                    "page_size": validated_page_size,
                }
            )

    except ValueError as exc:
        # Invalid cursor token
        logger.warning(
            "template_list_invalid_cursor",
            cursor=cursor,
            tenant_id=tenant_id,
            error=str(exc),
        )
        return _error_response(
            f"Invalid pagination cursor: {exc}",
            "BAD_REQUEST",
            400,
        )
    except Exception as exc:
        logger.error(
            "template_list_db_error",
            tenant_id=tenant_id,
            error=str(exc),
            error_type=type(exc).__name__,
        )
        return _error_response(
            "Failed to retrieve templates due to a server error",
            "INTERNAL_ERROR",
            500,
        )

    logger.info(
        "templates_listed",
        tenant_id=tenant_id,
        user_id=getattr(g, "user_id", "unknown"),
        total=list_response.total,
        page=list_response.page,
        page_size=list_response.page_size,
        filters={
            "category": category,
            "erp_type": erp_type,
            "generation_method": generation_method,
            "search": search,
        },
    )

    return jsonify(list_response.model_dump(mode="json")), 200


@templates_bp.route("/<string:template_id>", methods=["GET"])
@jwt_required()
@require_permissions("template:read")
def get_template(template_id: str) -> tuple[Response, int]:
    """Retrieve a specific generation template by its unique ID.

    Returns the full template document including configuration, version
    history, and usage statistics.  The template must belong to the
    authenticated tenant or be marked as globally public (R-007).

    Args:
        template_id: The UUID of the template to retrieve.

    Returns:
        A JSON-serialised :class:`TemplateResponse` with HTTP 200.

    Error Responses:
        404: No template found with the given ID in the user's visibility scope.
        500: Unexpected server error during query execution.
    """
    tenant_id: str = getattr(g, "tenant_id", "")
    user_id: str = getattr(g, "user_id", "unknown")

    try:
        db = get_db()
        collection = db[TEMPLATES_COLLECTION]

        # Query with tenant isolation: own templates + public templates.
        query_filter: Dict[str, Any] = {
            "$and": [
                {"template_id": template_id},
                _build_visibility_filter(tenant_id),
            ]
        }

        doc: Optional[Dict[str, Any]] = collection.find_one(query_filter)

    except Exception as exc:
        logger.error(
            "template_get_db_error",
            template_id=template_id,
            tenant_id=tenant_id,
            error=str(exc),
            error_type=type(exc).__name__,
        )
        return _error_response(
            "Failed to retrieve template due to a server error",
            "INTERNAL_ERROR",
            500,
        )

    if doc is None:
        logger.info(
            "template_not_found",
            template_id=template_id,
            tenant_id=tenant_id,
            user_id=user_id,
        )
        return _error_response(
            f"Template '{template_id}' not found",
            "NOT_FOUND",
            404,
        )

    serialized: Dict[str, Any] = serialize_document(doc)
    response: TemplateResponse = TemplateResponse.model_validate(serialized)

    logger.info(
        "template_retrieved",
        template_id=template_id,
        tenant_id=tenant_id,
        user_id=user_id,
    )

    return jsonify(response.model_dump(mode="json")), 200


@templates_bp.route("/<string:template_id>", methods=["PUT"])
@jwt_required()
@require_permissions("template:create")
def update_template(template_id: str) -> tuple[Response, int]:
    """Update an existing generation template.

    Accepts a JSON request body conforming to :class:`TemplateRequest`,
    validates all fields, then updates the mutable fields of the existing
    template document.  The ``version`` number is incremented and
    ``updated_at`` is set to the current UTC timestamp.

    Only the template owner or a Platform Admin may update a template.
    The template must belong to the authenticated tenant (public templates
    from other tenants cannot be modified in-place).

    Args:
        template_id: The UUID of the template to update.

    Returns:
        A JSON-serialised :class:`TemplateResponse` with HTTP 200 on
        success.

    Error Responses:
        400: Request body is missing or not valid JSON.
        403: User is not the template owner and not a Platform Admin.
        404: No matching template found for the authenticated tenant.
        422: Request body fails Pydantic schema validation.
        500: Unexpected server error during MongoDB update.
    """
    # ------------------------------------------------------------------
    # Parse request body
    # ------------------------------------------------------------------
    body: Optional[Dict[str, Any]] = request.get_json(silent=True)
    if not body:
        logger.warning(
            "template_update_missing_body",
            template_id=template_id,
            user_id=getattr(g, "user_id", "unknown"),
            tenant_id=getattr(g, "tenant_id", "unknown"),
        )
        return _error_response(
            "Request body is required and must be valid JSON",
            "BAD_REQUEST",
            400,
        )

    # ------------------------------------------------------------------
    # Validate with Pydantic schema
    # ------------------------------------------------------------------
    try:
        # Use model_validate_json for proper JSON-mode deserialization which
        # handles string-to-enum coercion even with strict=True model config.
        template_req: TemplateRequest = TemplateRequest.model_validate_json(
            json_module.dumps(body)
        )
    except ValidationError as exc:
        logger.warning(
            "template_update_validation_failed",
            template_id=template_id,
            user_id=getattr(g, "user_id", "unknown"),
            tenant_id=getattr(g, "tenant_id", "unknown"),
            error_count=len(exc.errors()),
        )
        return _error_response(
            "Template validation failed",
            "VALIDATION_ERROR",
            422,
            details=_sanitize_validation_errors(exc.errors()),
        )

    # ------------------------------------------------------------------
    # Extract authenticated context
    # ------------------------------------------------------------------
    tenant_id: str = getattr(g, "tenant_id", "")
    user_id: str = getattr(g, "user_id", "")
    user_roles: List[str] = getattr(g, "user_roles", [])

    # ------------------------------------------------------------------
    # Retrieve existing template (must belong to same tenant)
    # ------------------------------------------------------------------
    try:
        db = get_db()
        collection = db[TEMPLATES_COLLECTION]

        existing: Optional[Dict[str, Any]] = collection.find_one(
            {"template_id": template_id, "tenant_id": tenant_id}
        )
    except Exception as exc:
        logger.error(
            "template_update_db_error",
            template_id=template_id,
            tenant_id=tenant_id,
            error=str(exc),
            error_type=type(exc).__name__,
        )
        return _error_response(
            "Failed to update template due to a server error",
            "INTERNAL_ERROR",
            500,
        )

    if existing is None:
        logger.info(
            "template_update_not_found",
            template_id=template_id,
            tenant_id=tenant_id,
            user_id=user_id,
        )
        return _error_response(
            f"Template '{template_id}' not found",
            "NOT_FOUND",
            404,
        )

    # ------------------------------------------------------------------
    # Authorisation check — owner or Platform Admin
    # ------------------------------------------------------------------
    if not _is_owner_or_admin(existing, user_id, user_roles):
        logger.warning(
            "template_update_forbidden",
            template_id=template_id,
            user_id=user_id,
            owner_id=existing.get("created_by"),
            tenant_id=tenant_id,
        )
        return _error_response(
            "You are not authorised to update this template",
            "FORBIDDEN",
            403,
        )

    # ------------------------------------------------------------------
    # Apply mutable field updates
    # ------------------------------------------------------------------
    now: datetime = datetime.now(timezone.utc)
    update_data: Dict[str, Any] = template_req.model_dump()
    current_version: int = existing.get("version", 1)

    update_fields: Dict[str, Any] = {
        "name": update_data["name"],
        "description": update_data.get("description"),
        "category": update_data["category"],
        "erp_type": update_data["erp_type"],
        "generation_method": update_data["generation_method"],
        "schema_id": update_data.get("schema_id"),
        "tables": update_data["tables"],
        "parameters": update_data["parameters"],
        "tags": update_data.get("tags"),
        "is_public": update_data.get("is_public", False),
        "version": current_version + 1,
        "updated_at": now,
    }

    try:
        collection.update_one(
            {"template_id": template_id, "tenant_id": tenant_id},
            {"$set": update_fields},
        )
    except Exception as exc:
        logger.error(
            "template_update_write_error",
            template_id=template_id,
            tenant_id=tenant_id,
            error=str(exc),
            error_type=type(exc).__name__,
        )
        return _error_response(
            "Failed to update template due to a server error",
            "INTERNAL_ERROR",
            500,
        )

    # ------------------------------------------------------------------
    # Retrieve and return the updated document
    # ------------------------------------------------------------------
    updated_doc: Optional[Dict[str, Any]] = collection.find_one(
        {"template_id": template_id, "tenant_id": tenant_id}
    )

    if updated_doc is None:
        # Should not happen, but guard defensively.
        return _error_response(
            "Template was updated but could not be retrieved",
            "INTERNAL_ERROR",
            500,
        )

    serialized: Dict[str, Any] = serialize_document(updated_doc)
    response: TemplateResponse = TemplateResponse.model_validate(serialized)

    logger.info(
        "template_updated",
        template_id=template_id,
        name=template_req.name,
        version=current_version + 1,
        user_id=user_id,
        tenant_id=tenant_id,
    )

    return jsonify(response.model_dump(mode="json")), 200


@templates_bp.route("/<string:template_id>", methods=["DELETE"])
@jwt_required()
@require_permissions("template:delete")
def delete_template(template_id: str) -> tuple[Response, int]:
    """Delete a generation template.

    Permanently removes the template from the MongoDB collection.  Only
    the template owner or a Platform Admin may delete a template.  The
    template must belong to the authenticated tenant — public templates
    owned by other tenants cannot be deleted.

    Args:
        template_id: The UUID of the template to delete.

    Returns:
        HTTP 204 No Content on successful deletion.

    Error Responses:
        403: User is not the template owner and not a Platform Admin.
        404: No matching template found for the authenticated tenant.
        500: Unexpected server error during deletion.
    """
    tenant_id: str = getattr(g, "tenant_id", "")
    user_id: str = getattr(g, "user_id", "")
    user_roles: List[str] = getattr(g, "user_roles", [])

    # ------------------------------------------------------------------
    # Retrieve existing template (must belong to same tenant)
    # ------------------------------------------------------------------
    try:
        db = get_db()
        collection = db[TEMPLATES_COLLECTION]

        existing: Optional[Dict[str, Any]] = collection.find_one(
            {"template_id": template_id, "tenant_id": tenant_id}
        )
    except Exception as exc:
        logger.error(
            "template_delete_db_error",
            template_id=template_id,
            tenant_id=tenant_id,
            error=str(exc),
            error_type=type(exc).__name__,
        )
        return _error_response(
            "Failed to delete template due to a server error",
            "INTERNAL_ERROR",
            500,
        )

    if existing is None:
        logger.info(
            "template_delete_not_found",
            template_id=template_id,
            tenant_id=tenant_id,
            user_id=user_id,
        )
        return _error_response(
            f"Template '{template_id}' not found",
            "NOT_FOUND",
            404,
        )

    # ------------------------------------------------------------------
    # Authorisation check — owner or Platform Admin
    # ------------------------------------------------------------------
    if not _is_owner_or_admin(existing, user_id, user_roles):
        logger.warning(
            "template_delete_forbidden",
            template_id=template_id,
            user_id=user_id,
            owner_id=existing.get("created_by"),
            tenant_id=tenant_id,
        )
        return _error_response(
            "You are not authorised to delete this template",
            "FORBIDDEN",
            403,
        )

    # ------------------------------------------------------------------
    # Delete from MongoDB
    # ------------------------------------------------------------------
    try:
        result = collection.delete_one(
            {"template_id": template_id, "tenant_id": tenant_id}
        )
    except Exception as exc:
        logger.error(
            "template_delete_write_error",
            template_id=template_id,
            tenant_id=tenant_id,
            error=str(exc),
            error_type=type(exc).__name__,
        )
        return _error_response(
            "Failed to delete template due to a server error",
            "INTERNAL_ERROR",
            500,
        )

    if result.deleted_count == 0:
        # Rare race condition — document removed between find and delete.
        return _error_response(
            f"Template '{template_id}' not found",
            "NOT_FOUND",
            404,
        )

    logger.info(
        "template_deleted",
        template_id=template_id,
        user_id=user_id,
        tenant_id=tenant_id,
    )

    return "", 204  # type: ignore[return-value]


@templates_bp.route("/<string:template_id>/clone", methods=["POST"])
@jwt_required()
@require_permissions("template:create")
def clone_template(template_id: str) -> tuple[Response, int]:
    """Clone an existing template into a new independent copy.

    Creates a deep copy of the source template with a new UUID
    ``template_id``, resets the ``version`` to 1, sets ``usage_count``
    to 0, assigns ``created_by`` to the current user, and scopes
    ``tenant_id`` to the authenticated tenant.

    An optional ``name`` field may be provided in the request body to
    override the cloned template's name; otherwise the original name is
    suffixed with ``" (Copy)"``.

    The source template must be visible to the authenticated user (own
    tenant or public).

    Args:
        template_id: The UUID of the source template to clone.

    Returns:
        A JSON-serialised :class:`TemplateResponse` with HTTP 201 Created.

    Error Responses:
        404: Source template not found or not visible to the user.
        500: Unexpected server error during cloning.
    """
    tenant_id: str = getattr(g, "tenant_id", "")
    user_id: str = getattr(g, "user_id", "")

    # ------------------------------------------------------------------
    # Retrieve source template (own + public visibility)
    # ------------------------------------------------------------------
    try:
        db = get_db()
        collection = db[TEMPLATES_COLLECTION]

        query_filter: Dict[str, Any] = {
            "$and": [
                {"template_id": template_id},
                _build_visibility_filter(tenant_id),
            ]
        }

        source: Optional[Dict[str, Any]] = collection.find_one(query_filter)

    except Exception as exc:
        logger.error(
            "template_clone_db_error",
            template_id=template_id,
            tenant_id=tenant_id,
            error=str(exc),
            error_type=type(exc).__name__,
        )
        return _error_response(
            "Failed to clone template due to a server error",
            "INTERNAL_ERROR",
            500,
        )

    if source is None:
        logger.info(
            "template_clone_source_not_found",
            template_id=template_id,
            tenant_id=tenant_id,
            user_id=user_id,
        )
        return _error_response(
            f"Source template '{template_id}' not found",
            "NOT_FOUND",
            404,
        )

    # ------------------------------------------------------------------
    # Deep-copy the source document and reset metadata
    # ------------------------------------------------------------------
    now: datetime = datetime.now(timezone.utc)
    new_template_id: str = str(uuid.uuid4())

    cloned: Dict[str, Any] = copy.deepcopy(source)

    # Remove MongoDB internal _id to allow a fresh auto-generated one.
    cloned.pop("_id", None)

    # Parse optional overrides from request body (e.g. custom name).
    clone_body: Optional[Dict[str, Any]] = request.get_json(silent=True)
    clone_name: str = (
        clone_body.get("name")
        if clone_body and clone_body.get("name")
        else f"{source.get('name', 'Template')} (Copy)"
    )

    cloned.update(
        {
            "template_id": new_template_id,
            "name": clone_name,
            "tenant_id": tenant_id,
            "created_by": user_id,
            "version": 1,
            "usage_count": 0,
            "is_public": False,
            "created_at": now,
            "updated_at": now,
        }
    )

    # ------------------------------------------------------------------
    # Persist cloned template
    # ------------------------------------------------------------------
    try:
        collection.insert_one(cloned)
    except Exception as exc:
        logger.error(
            "template_clone_write_error",
            source_template_id=template_id,
            new_template_id=new_template_id,
            tenant_id=tenant_id,
            error=str(exc),
            error_type=type(exc).__name__,
        )
        return _error_response(
            "Failed to clone template due to a server error",
            "INTERNAL_ERROR",
            500,
        )

    # ------------------------------------------------------------------
    # Build and return response
    # ------------------------------------------------------------------
    serialized: Dict[str, Any] = serialize_document(cloned)
    response: TemplateResponse = TemplateResponse.model_validate(serialized)

    logger.info(
        "template_cloned",
        source_template_id=template_id,
        new_template_id=new_template_id,
        name=clone_name,
        user_id=user_id,
        tenant_id=tenant_id,
    )

    return jsonify(response.model_dump(mode="json")), 201
