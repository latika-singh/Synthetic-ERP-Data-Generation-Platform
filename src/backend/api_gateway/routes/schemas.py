"""Flask Blueprint for ERP schema discovery and browsing REST endpoints.

This module provides the ``/api/v1/schemas/`` route namespace with endpoints
for discovering, listing, browsing, and deleting ERP schema definitions.  It
serves as the HTTP interface between the Web Console (Screen S-005 — Schema
Browser) and the backend Profiling Service.

Endpoints:
    - ``POST   /discover``                          — Trigger schema discovery
    - ``GET    /``                                   — List discovered schemas
    - ``GET    /<schema_id>``                        — Retrieve schema definition
    - ``GET    /<schema_id>/tables``                 — List tables in schema
    - ``GET    /<schema_id>/tables/<table_name>``    — Table column details
    - ``GET    /<schema_id>/relationships``           — Foreign key relationships
    - ``DELETE /<schema_id>``                        — Delete a schema definition

Security:
    All endpoints require JWT authentication (``@jwt_required()``) and
    permission-based RBAC (``@require_permissions(...)``).  The two permission
    scopes used are:

    - ``schema:discover`` — Required for POST /discover and DELETE /<id>.
    - ``schema:read``     — Required for all GET endpoints.

Multi-Tenant Isolation (R-007):
    Every endpoint extracts ``g.tenant_id`` (populated by the JWT auth
    middleware) and passes it to the :class:`SchemaService`, which applies
    it as a mandatory filter on all MongoDB queries.  Cross-tenant data
    access is impossible by design.

Constraints:
    - **C-001**: Only schema metadata is extracted — no raw production data
      is accessed or stored by the Profiling Service.
    - **C-005**: Initial release limited to four ERP modules: Financial
      Accounting, Human Resources, Sales & Distribution, Material Management.

Typical usage (registered via Application Factory)::

    from api_gateway.routes.schemas import schemas_bp

    app.register_blueprint(schemas_bp, url_prefix="/api/v1/schemas")
"""

from __future__ import annotations

from flask import Blueprint, Response, g, jsonify, request
from flask_jwt_extended import jwt_required
from pydantic import ValidationError

from api_gateway.middleware.auth import require_permissions
from api_gateway.schemas.schema import SchemaDiscoveryRequest
from api_gateway.services.schema_service import SchemaService
from api_gateway.utils.pagination import validate_page_size
from shared.logging.structured_logger import get_logger


# ---------------------------------------------------------------------------
# Module-level logger
# ---------------------------------------------------------------------------

logger = get_logger(__name__)

# ---------------------------------------------------------------------------
# Blueprint registration
# ---------------------------------------------------------------------------

schemas_bp: Blueprint = Blueprint("schemas", __name__)
"""Flask Blueprint instance for all ``/api/v1/schemas/`` endpoints.

Registered on the Flask application by the Application Factory
(``create_app()``) with the URL prefix ``/api/v1/schemas``.
"""


# ---------------------------------------------------------------------------
# Helper — lazy service instantiation
# ---------------------------------------------------------------------------


def _get_schema_service() -> SchemaService:
    """Return a :class:`SchemaService` instance within the active app context.

    :class:`SchemaService.__init__` reads ``current_app.config``, which
    requires an active Flask application context.  Blueprint route handlers
    execute inside this context, so instantiation is safe here.

    Returns:
        A ready-to-use :class:`SchemaService` instance.
    """
    return SchemaService()


# ---------------------------------------------------------------------------
# POST /discover — Trigger ERP schema discovery
# ---------------------------------------------------------------------------


@schemas_bp.route("/discover", methods=["POST"])
@jwt_required()
@require_permissions("schema:discover")
def discover_schema() -> tuple[Response, int]:
    """Initiate an ERP schema discovery against a source system.

    Accepts a JSON body conforming to :class:`SchemaDiscoveryRequest`,
    validates the ERP type and modules against Constraint C-005, and
    dispatches the discovery request to the Profiling Service via
    :meth:`SchemaService.discover_schema`.

    **Important**: Only schema metadata (tables, columns, relationships,
    data types) is extracted by the Profiling Service.  No raw production
    data is accessed or stored (Constraint C-001).

    Request Body (JSON):
        See :class:`~api_gateway.schemas.schema.SchemaDiscoveryRequest` for
        the full schema.  Required fields:

        - ``erp_type`` — One of ``sap``, ``oracle_ebs``, ``dynamics``,
          ``legacy``.
        - ``connection_params`` — Host, port, credentials, connection type.
        - ``modules`` — List of ERP modules (C-005 scope enforced).

    Returns:
        A ``(response, status_code)`` tuple:

        - **202 Accepted** — Discovery initiated successfully.  Response
          body contains ``schema_id``, ``status``, ``erp_type``,
          ``modules``, and ``created_at``.
        - **400 Bad Request** — Validation error (malformed body, invalid
          ERP type, out-of-scope module).
        - **500 Internal Server Error** — Unexpected server-side failure.

    Raises:
        ValidationError: Caught and converted to a 400 JSON response with
            field-level error details.
        ValueError: Caught from :meth:`SchemaService.discover_schema` for
            invalid ERP type or modules and converted to a 400 response.
        Exception: Caught as a generic server error and returned as 500.
    """
    tenant_id: str = getattr(g, "tenant_id", "")
    user_id: str = getattr(g, "user_id", "")

    logger.info(
        "schema_discovery_request_received",
        tenant_id=tenant_id,
        user_id=user_id,
        method=request.method,
        path=request.path,
    )

    # --- Parse and validate request body ---
    body = request.get_json(silent=True)
    if body is None:
        logger.warning(
            "schema_discovery_missing_body",
            tenant_id=tenant_id,
            user_id=user_id,
        )
        return (
            jsonify({
                "error": "Request body is required",
                "code": "BAD_REQUEST",
            }),
            400,
        )

    try:
        validated: SchemaDiscoveryRequest = SchemaDiscoveryRequest(**body)
    except ValidationError as exc:
        logger.warning(
            "schema_discovery_validation_error",
            tenant_id=tenant_id,
            user_id=user_id,
            errors=exc.errors(),
        )
        return (
            jsonify({
                "error": "Validation failed",
                "code": "VALIDATION_ERROR",
                "details": exc.errors(),
            }),
            400,
        )

    # --- Delegate to SchemaService ---
    try:
        schema_service = _get_schema_service()

        # Extract validated fields for the service method signature.
        # SchemaService.discover_schema() accepts individual parameters
        # rather than the Pydantic model, keeping the service layer
        # decoupled from the API schema layer.
        connection_config: dict = validated.connection_params.model_dump()
        modules_list: list[str] = [m.value for m in validated.modules]

        result: dict = schema_service.discover_schema(
            tenant_id=tenant_id,
            user_id=user_id,
            erp_type=validated.erp_type.value,
            connection_config=connection_config,
            modules=modules_list,
        )

        logger.info(
            "schema_discovery_initiated",
            tenant_id=tenant_id,
            user_id=user_id,
            schema_id=result.get("schema_id"),
            erp_type=validated.erp_type.value,
            modules=modules_list,
        )

        return jsonify(result), 202

    except ValueError as exc:
        # Business-rule validation from SchemaService (e.g. unsupported
        # ERP type or module not in C-005 scope).
        logger.warning(
            "schema_discovery_value_error",
            tenant_id=tenant_id,
            user_id=user_id,
            error=str(exc),
        )
        return (
            jsonify({
                "error": str(exc),
                "code": "BAD_REQUEST",
            }),
            400,
        )

    except Exception as exc:
        logger.error(
            "schema_discovery_unexpected_error",
            tenant_id=tenant_id,
            user_id=user_id,
            error=str(exc),
            error_type=type(exc).__name__,
        )
        return (
            jsonify({
                "error": "An internal error occurred while initiating schema discovery",
                "code": "INTERNAL_ERROR",
            }),
            500,
        )


# ---------------------------------------------------------------------------
# GET / — List discovered schemas (paginated)
# ---------------------------------------------------------------------------


@schemas_bp.route("", methods=["GET"])
@jwt_required()
@require_permissions("schema:read")
def list_schemas() -> tuple[Response, int]:
    """List discovered ERP schema definitions with pagination and filtering.

    Returns a paginated list of schema definitions belonging to the
    authenticated tenant (R-007).  Supports optional filtering by ERP
    type and module.

    Query Parameters:
        erp_type (str, optional):
            Filter by ERP system type (``sap``, ``oracle_ebs``,
            ``dynamics``, ``legacy``).
        module (str, optional):
            Filter by ERP module (``financial_accounting``, ``hr``,
            ``sales_distribution``, ``material_management``).
        page (int, optional):
            Page number (1-based).  Defaults to ``1``.
        page_size (int, optional):
            Items per page.  Defaults to ``20``, clamped to [1, 100].

    Returns:
        A ``(response, status_code)`` tuple:

        - **200 OK** — Paginated list with ``items``, ``total``, ``page``,
          ``page_size``, and ``has_next``.
        - **500 Internal Server Error** — Unexpected failure.
    """
    tenant_id: str = getattr(g, "tenant_id", "")

    # --- Parse query parameters ---
    erp_type: str | None = request.args.get("erp_type", default=None, type=str)
    module: str | None = request.args.get("module", default=None, type=str)

    # Page number: default 1, minimum 1
    try:
        page: int = max(1, int(request.args.get("page", 1)))
    except (TypeError, ValueError):
        page = 1

    # Page size: validated and clamped via utility function
    raw_page_size = request.args.get("page_size", default=None)
    try:
        page_size_param: int | None = int(raw_page_size) if raw_page_size is not None else None
    except (TypeError, ValueError):
        page_size_param = None
    page_size: int = validate_page_size(page_size_param)

    logger.info(
        "schema_list_request",
        tenant_id=tenant_id,
        erp_type=erp_type,
        module=module,
        page=page,
        page_size=page_size,
    )

    try:
        schema_service = _get_schema_service()
        result: dict = schema_service.list_schemas(
            tenant_id=tenant_id,
            erp_type=erp_type,
            module=module,
            page=page,
            page_size=page_size,
        )

        logger.debug(
            "schema_list_response",
            tenant_id=tenant_id,
            total=result.get("total", 0),
            page=page,
            page_size=page_size,
        )

        return jsonify(result), 200

    except Exception as exc:
        logger.error(
            "schema_list_unexpected_error",
            tenant_id=tenant_id,
            error=str(exc),
            error_type=type(exc).__name__,
        )
        return (
            jsonify({
                "error": "An internal error occurred while listing schemas",
                "code": "INTERNAL_ERROR",
            }),
            500,
        )


# ---------------------------------------------------------------------------
# GET /<schema_id> — Retrieve a specific schema definition
# ---------------------------------------------------------------------------


@schemas_bp.route("/<string:schema_id>", methods=["GET"])
@jwt_required()
@require_permissions("schema:read")
def get_schema(schema_id: str) -> tuple[Response, int]:
    """Retrieve a complete schema definition by its unique identifier.

    Returns the full schema definition including all discovered tables,
    columns, data types, and relationships.  The response is scoped to
    the authenticated tenant (R-007) — a schema belonging to a different
    tenant will appear as ``404 Not Found``.

    Args:
        schema_id: The unique identifier of the schema definition to
            retrieve (URL path parameter).

    Returns:
        A ``(response, status_code)`` tuple:

        - **200 OK** — Full schema definition with ``tables``,
          ``columns``, ``relationships``, ``erp_type``, ``erp_modules``,
          ``discovered_at``, and ``status``.
        - **404 Not Found** — No schema with the given ID exists for the
          current tenant.
        - **500 Internal Server Error** — Unexpected failure.
    """
    tenant_id: str = getattr(g, "tenant_id", "")

    logger.info(
        "schema_get_request",
        tenant_id=tenant_id,
        schema_id=schema_id,
    )

    try:
        schema_service = _get_schema_service()
        schema_data: dict | None = schema_service.get_schema(
            schema_id=schema_id,
            tenant_id=tenant_id,
        )

        if schema_data is None:
            logger.warning(
                "schema_get_not_found",
                tenant_id=tenant_id,
                schema_id=schema_id,
            )
            return (
                jsonify({
                    "error": f"Schema '{schema_id}' not found",
                    "code": "NOT_FOUND",
                }),
                404,
            )

        logger.debug(
            "schema_get_success",
            tenant_id=tenant_id,
            schema_id=schema_id,
        )

        return jsonify(schema_data), 200

    except Exception as exc:
        logger.error(
            "schema_get_unexpected_error",
            tenant_id=tenant_id,
            schema_id=schema_id,
            error=str(exc),
            error_type=type(exc).__name__,
        )
        return (
            jsonify({
                "error": "An internal error occurred while retrieving the schema",
                "code": "INTERNAL_ERROR",
            }),
            500,
        )


# ---------------------------------------------------------------------------
# GET /<schema_id>/tables — List tables in a schema
# ---------------------------------------------------------------------------


@schemas_bp.route("/<string:schema_id>/tables", methods=["GET"])
@jwt_required()
@require_permissions("schema:read")
def get_schema_tables(schema_id: str) -> tuple[Response, int]:
    """List all tables within a discovered schema definition.

    Returns a summary of each table including its name, column count,
    estimated row count, and primary key columns.  This endpoint powers
    the Schema Browser (Screen S-005) table listing view.

    Each table in the response contains:
        - ``name`` — Table name as defined in the source ERP schema.
        - ``column_count`` — Number of columns discovered.
        - ``row_count_estimate`` — Approximate row count (may be ``null``
          if unavailable without data access per C-001).
        - ``primary_key`` — List of primary key column names.

    Args:
        schema_id: The unique identifier of the parent schema (URL path
            parameter).

    Returns:
        A ``(response, status_code)`` tuple:

        - **200 OK** — JSON object with ``schema_id`` and ``tables``
          array.
        - **404 Not Found** — No schema with the given ID exists for the
          current tenant.
        - **500 Internal Server Error** — Unexpected failure.
    """
    tenant_id: str = getattr(g, "tenant_id", "")

    logger.info(
        "schema_tables_request",
        tenant_id=tenant_id,
        schema_id=schema_id,
    )

    try:
        schema_service = _get_schema_service()
        tables: list[dict] | None = schema_service.get_schema_tables(
            schema_id=schema_id,
            tenant_id=tenant_id,
        )

        if tables is None:
            logger.warning(
                "schema_tables_not_found",
                tenant_id=tenant_id,
                schema_id=schema_id,
            )
            return (
                jsonify({
                    "error": f"Schema '{schema_id}' not found",
                    "code": "NOT_FOUND",
                }),
                404,
            )

        # Build summary entries for Schema Browser (S-005).
        # The raw table documents from SchemaService contain full column
        # arrays; here we produce a lightweight summary with counts
        # suitable for the table listing view.
        table_summaries: list[dict] = []
        for table in tables:
            columns = table.get("columns", [])
            primary_keys = table.get("primary_keys", table.get("primary_key", []))
            table_summaries.append({
                "name": table.get("name", table.get("table_name", "")),
                "schema_name": table.get("schema_name"),
                "column_count": len(columns),
                "row_count_estimate": table.get(
                    "row_count",
                    table.get("row_count_estimate", table.get("record_count_estimate")),
                ),
                "primary_key": primary_keys if isinstance(primary_keys, list) else [primary_keys],
                "erp_module": table.get("erp_module"),
                "description": table.get("description"),
            })

        logger.debug(
            "schema_tables_response",
            tenant_id=tenant_id,
            schema_id=schema_id,
            table_count=len(table_summaries),
        )

        return (
            jsonify({
                "schema_id": schema_id,
                "tables": table_summaries,
                "total": len(table_summaries),
            }),
            200,
        )

    except Exception as exc:
        logger.error(
            "schema_tables_unexpected_error",
            tenant_id=tenant_id,
            schema_id=schema_id,
            error=str(exc),
            error_type=type(exc).__name__,
        )
        return (
            jsonify({
                "error": "An internal error occurred while listing schema tables",
                "code": "INTERNAL_ERROR",
            }),
            500,
        )


# ---------------------------------------------------------------------------
# GET /<schema_id>/tables/<table_name> — Table column details
# ---------------------------------------------------------------------------


@schemas_bp.route(
    "/<string:schema_id>/tables/<string:table_name>",
    methods=["GET"],
)
@jwt_required()
@require_permissions("schema:read")
def get_table_details(schema_id: str, table_name: str) -> tuple[Response, int]:
    """Retrieve detailed column definitions for a specific table.

    Returns the full metadata for every column in the specified table,
    including data type, nullability, max length, precision, scale, and
    any foreign key references.  This data drives the Generation Engine's
    column-level synthesis strategy selection.

    Column detail fields:
        - ``column_name`` — Column name in the source schema.
        - ``data_type`` — Canonical data type (varchar, integer, etc.).
        - ``nullable`` — Whether the column accepts NULL values.
        - ``max_length`` — Maximum character length (string columns).
        - ``precision`` — Total digits (numeric columns).
        - ``scale`` — Decimal digits (numeric columns).
        - ``primary_key`` — Whether the column is part of the primary key.
        - ``default_value`` — Default value expression, if any.
        - ``description`` — Column description or catalog comment.

    Args:
        schema_id: The unique identifier of the parent schema (URL path
            parameter).
        table_name: The name of the table whose columns are requested
            (URL path parameter).

    Returns:
        A ``(response, status_code)`` tuple:

        - **200 OK** — Full table definition with column details.
        - **404 Not Found** — Schema or table not found for the current
          tenant.
        - **500 Internal Server Error** — Unexpected failure.
    """
    tenant_id: str = getattr(g, "tenant_id", "")

    logger.info(
        "schema_table_details_request",
        tenant_id=tenant_id,
        schema_id=schema_id,
        table_name=table_name,
    )

    try:
        schema_service = _get_schema_service()
        table_data: dict | None = schema_service.get_table_details(
            schema_id=schema_id,
            tenant_id=tenant_id,
            table_name=table_name,
        )

        if table_data is None:
            logger.warning(
                "schema_table_details_not_found",
                tenant_id=tenant_id,
                schema_id=schema_id,
                table_name=table_name,
            )
            return (
                jsonify({
                    "error": (
                        f"Table '{table_name}' not found in schema "
                        f"'{schema_id}'"
                    ),
                    "code": "NOT_FOUND",
                }),
                404,
            )

        logger.debug(
            "schema_table_details_success",
            tenant_id=tenant_id,
            schema_id=schema_id,
            table_name=table_name,
            column_count=len(table_data.get("columns", [])),
        )

        return (
            jsonify({
                "schema_id": schema_id,
                "table": table_data,
            }),
            200,
        )

    except Exception as exc:
        logger.error(
            "schema_table_details_unexpected_error",
            tenant_id=tenant_id,
            schema_id=schema_id,
            table_name=table_name,
            error=str(exc),
            error_type=type(exc).__name__,
        )
        return (
            jsonify({
                "error": "An internal error occurred while retrieving table details",
                "code": "INTERNAL_ERROR",
            }),
            500,
        )


# ---------------------------------------------------------------------------
# GET /<schema_id>/relationships — Foreign key relationships
# ---------------------------------------------------------------------------


@schemas_bp.route("/<string:schema_id>/relationships", methods=["GET"])
@jwt_required()
@require_permissions("schema:read")
def get_schema_relationships(schema_id: str) -> tuple[Response, int]:
    """Retrieve foreign key relationships for a schema definition.

    Returns all discovered foreign key constraints between tables in the
    schema.  This data is used by:

    - The **Generation Engine** to enforce referential integrity during
      synthetic data generation (determining table generation order and
      ensuring FK values reference valid PK records).
    - The **Schema Browser** (Screen S-005) to visualize table
      relationships and dependency graphs.

    Each relationship entry contains:
        - ``source_table`` — The referencing (child) table name.
        - ``source_column`` — The foreign key column.
        - ``target_table`` — The referenced (parent) table name.
        - ``target_column`` — The primary key column being referenced.
        - ``relationship_type`` — Cardinality (``one_to_many``,
          ``many_to_one``, ``one_to_one``, ``many_to_many``).

    Args:
        schema_id: The unique identifier of the parent schema (URL path
            parameter).

    Returns:
        A ``(response, status_code)`` tuple:

        - **200 OK** — JSON object with ``schema_id``, ``relationships``
          array, and ``total`` count.
        - **404 Not Found** — No schema with the given ID exists for the
          current tenant.
        - **500 Internal Server Error** — Unexpected failure.
    """
    tenant_id: str = getattr(g, "tenant_id", "")

    logger.info(
        "schema_relationships_request",
        tenant_id=tenant_id,
        schema_id=schema_id,
    )

    try:
        schema_service = _get_schema_service()
        relationships: list[dict] | None = schema_service.get_relationships(
            schema_id=schema_id,
            tenant_id=tenant_id,
        )

        if relationships is None:
            logger.warning(
                "schema_relationships_not_found",
                tenant_id=tenant_id,
                schema_id=schema_id,
            )
            return (
                jsonify({
                    "error": f"Schema '{schema_id}' not found",
                    "code": "NOT_FOUND",
                }),
                404,
            )

        logger.debug(
            "schema_relationships_response",
            tenant_id=tenant_id,
            schema_id=schema_id,
            relationship_count=len(relationships),
        )

        return (
            jsonify({
                "schema_id": schema_id,
                "relationships": relationships,
                "total": len(relationships),
            }),
            200,
        )

    except Exception as exc:
        logger.error(
            "schema_relationships_unexpected_error",
            tenant_id=tenant_id,
            schema_id=schema_id,
            error=str(exc),
            error_type=type(exc).__name__,
        )
        return (
            jsonify({
                "error": "An internal error occurred while retrieving relationships",
                "code": "INTERNAL_ERROR",
            }),
            500,
        )


# ---------------------------------------------------------------------------
# DELETE /<schema_id> — Delete a schema definition
# ---------------------------------------------------------------------------


@schemas_bp.route("/<string:schema_id>", methods=["DELETE"])
@jwt_required()
@require_permissions("schema:discover")
def delete_schema(schema_id: str) -> tuple[Response, int]:
    """Delete a discovered schema definition.

    Removes the schema definition from MongoDB and invalidates any cached
    copies in Redis.  The operation is scoped to the authenticated tenant
    (R-007) — a schema belonging to a different tenant cannot be deleted.

    The ``schema:discover`` permission is required (same as the POST
    /discover endpoint) because schema deletion is a privileged operation
    that should be restricted to users who are also allowed to initiate
    discovery.

    Args:
        schema_id: The unique identifier of the schema definition to
            delete (URL path parameter).

    Returns:
        A ``(response, status_code)`` tuple:

        - **204 No Content** — Schema successfully deleted.
        - **404 Not Found** — No schema with the given ID exists for the
          current tenant.
        - **500 Internal Server Error** — Unexpected failure.
    """
    tenant_id: str = getattr(g, "tenant_id", "")

    logger.info(
        "schema_delete_request",
        tenant_id=tenant_id,
        schema_id=schema_id,
    )

    try:
        schema_service = _get_schema_service()
        deleted: bool = schema_service.delete_schema(
            schema_id=schema_id,
            tenant_id=tenant_id,
        )

        if not deleted:
            logger.warning(
                "schema_delete_not_found",
                tenant_id=tenant_id,
                schema_id=schema_id,
            )
            return (
                jsonify({
                    "error": f"Schema '{schema_id}' not found",
                    "code": "NOT_FOUND",
                }),
                404,
            )

        logger.info(
            "schema_delete_success",
            tenant_id=tenant_id,
            schema_id=schema_id,
        )

        return Response(status=204, mimetype="application/json")

    except Exception as exc:
        logger.error(
            "schema_delete_unexpected_error",
            tenant_id=tenant_id,
            schema_id=schema_id,
            error=str(exc),
            error_type=type(exc).__name__,
        )
        return (
            jsonify({
                "error": "An internal error occurred while deleting the schema",
                "code": "INTERNAL_ERROR",
            }),
            500,
        )
