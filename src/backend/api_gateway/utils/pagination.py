"""Cursor-based pagination utility for MongoDB queries.

This module provides cursor-based pagination for MongoDB collections in the
API Gateway service. It implements forward and backward navigation using
base64-encoded MongoDB ObjectId tokens, configurable page sizes (default 20,
max 100), and a standardized PaginatedResponse Pydantic model.

All paginated queries should include tenant_id in the query filter to enforce
multi-tenant isolation per requirement R-007. Callers are responsible for
injecting tenant scope before invoking paginate_query().

Used by all API Gateway route handlers that return paginated list responses
(generation jobs, profiles, schemas, templates) to prevent unbounded result
sets that could overwhelm clients and the database.

Typical usage::

    from api_gateway.utils.pagination import paginate_query, PaginatedResponse

    response = paginate_query(
        collection=db.generation_profiles,
        query_filter={"tenant_id": current_tenant_id},
        page_size=20,
        cursor=request.args.get("cursor"),
    )
    return response.model_dump()
"""

import base64
import copy
import datetime
from typing import Any, Generic, TypeVar

from bson import ObjectId
from bson.errors import InvalidId
from pydantic import BaseModel, Field, field_validator
from pymongo.collection import Collection


# ---------------------------------------------------------------------------
# Type variable for generic PaginatedResponse
# ---------------------------------------------------------------------------
T = TypeVar("T")

# ---------------------------------------------------------------------------
# Pagination constants
# ---------------------------------------------------------------------------
DEFAULT_PAGE_SIZE: int = 20
"""Default number of items returned per page when no page_size is specified."""

MAX_PAGE_SIZE: int = 100
"""Maximum allowed items per page. Requests exceeding this are clamped."""

MIN_PAGE_SIZE: int = 1
"""Minimum allowed items per page. Requests below this are clamped."""


# ---------------------------------------------------------------------------
# PaginatedResponse model
# ---------------------------------------------------------------------------
class PaginatedResponse(BaseModel, Generic[T]):  # noqa: UP046
    """Standardized paginated response model for MongoDB query results.

    Generic Pydantic v2 model that wraps a page of results with cursor tokens
    for navigating forward and backward through a MongoDB collection.  Every
    paginated API endpoint returns this structure so that clients can rely on
    a consistent pagination contract.

    Type Parameters:
        T: The type of individual items in the ``items`` list.  Typically
            ``dict[str, Any]`` for serialized MongoDB documents.

    Attributes:
        items: The page of result documents.
        next_cursor: Base64-encoded cursor token for fetching the next page,
            or ``None`` when the current page is the last page.
        prev_cursor: Base64-encoded cursor token for fetching the previous
            page, or ``None`` when the current page is the first page.
        total_count: Total number of matching documents across all pages,
            computed independently of cursor position.
        has_more: ``True`` when additional pages exist beyond the current
            page; ``False`` otherwise.
        page_size: Number of items per page that was used for this request,
            after validation and clamping.

    Example::

        >>> response = PaginatedResponse(
        ...     items=[{"_id": "abc123", "name": "Job-1"}],
        ...     next_cursor="NjRhYjJjM2Q1ZTZmN2E4YjljMGQxZTJm",
        ...     prev_cursor=None,
        ...     total_count=42,
        ...     has_more=True,
        ...     page_size=20,
        ... )
        >>> data = response.model_dump()
        >>> data["has_more"]
        True
    """

    items: list[T] = Field(
        default_factory=list,
        description="The page of result documents.",
    )
    next_cursor: str | None = Field(
        default=None,
        description=(
            "Base64-encoded cursor token for the next page. "
            "None when this is the last page."
        ),
    )
    prev_cursor: str | None = Field(
        default=None,
        description=(
            "Base64-encoded cursor token for the previous page. "
            "None when this is the first page."
        ),
    )
    total_count: int = Field(
        default=0,
        ge=0,
        description="Total number of matching documents across all pages.",
    )
    has_more: bool = Field(
        default=False,
        description="Whether more pages exist after the current page.",
    )
    page_size: int = Field(
        default=DEFAULT_PAGE_SIZE,
        ge=MIN_PAGE_SIZE,
        le=MAX_PAGE_SIZE,
        description="Number of items per page used for this request.",
    )

    model_config = {"arbitrary_types_allowed": True}

    @field_validator("page_size")
    @classmethod
    def clamp_page_size(cls, value: int) -> int:
        """Ensure *page_size* stays within the allowed bounds.

        Args:
            value: The raw ``page_size`` value supplied to the model.

        Returns:
            The clamped page size, guaranteed to fall within
            [``MIN_PAGE_SIZE``, ``MAX_PAGE_SIZE``].
        """
        return max(MIN_PAGE_SIZE, min(value, MAX_PAGE_SIZE))


# ---------------------------------------------------------------------------
# Cursor encoding / decoding
# ---------------------------------------------------------------------------
def encode_cursor(object_id: ObjectId) -> str:
    """Encode a MongoDB ObjectId into a URL-safe base64 cursor token.

    Converts a BSON ``ObjectId`` to its 24-character hexadecimal string
    representation, then base64 URL-safe encodes it to produce an opaque
    cursor token suitable for use in query parameters and HTTP headers.

    Args:
        object_id: The MongoDB ``ObjectId`` to encode as a pagination
            cursor.

    Returns:
        A URL-safe base64-encoded string representing the cursor position.
        The token is deterministic: the same ``ObjectId`` always yields the
        same cursor string.

    Example::

        >>> from bson import ObjectId
        >>> oid = ObjectId("64ab2c3d5e6f7a8b9c0d1e2f")
        >>> token = encode_cursor(oid)
        >>> isinstance(token, str)
        True
    """
    object_id_str: str = str(object_id)
    encoded_bytes: bytes = base64.urlsafe_b64encode(
        object_id_str.encode("utf-8")
    )
    return encoded_bytes.decode("utf-8")


def decode_cursor(cursor: str) -> ObjectId:
    """Decode a base64 cursor token back into a MongoDB ObjectId.

    Performs the inverse of :func:`encode_cursor`: base64 URL-safe decodes
    the token, then converts the resulting string to a BSON ``ObjectId``
    for use in MongoDB query filters.

    Args:
        cursor: A URL-safe base64-encoded cursor token string, as returned
            by :func:`encode_cursor` or a previous ``PaginatedResponse``.

    Returns:
        The decoded MongoDB ``ObjectId``.

    Raises:
        ValueError: If *cursor* is ``None``, empty, not a string, contains
            invalid base64 characters, or does not decode to a valid 24-char
            hexadecimal MongoDB ObjectId.

    Example::

        >>> from bson import ObjectId
        >>> original = ObjectId("64ab2c3d5e6f7a8b9c0d1e2f")
        >>> token = encode_cursor(original)
        >>> decoded = decode_cursor(token)
        >>> decoded == original
        True
    """
    if not cursor or not isinstance(cursor, str):
        raise ValueError(
            "Invalid cursor: expected a non-empty string, "
            f"got {type(cursor).__name__}."
        )

    try:
        decoded_bytes: bytes = base64.urlsafe_b64decode(
            cursor.encode("utf-8")
        )
        object_id_str: str = decoded_bytes.decode("utf-8")
        return ObjectId(object_id_str)
    except (InvalidId, ValueError, UnicodeDecodeError) as exc:
        raise ValueError(
            f"Invalid pagination cursor '{cursor}': unable to decode to a "
            f"valid MongoDB ObjectId. Ensure the cursor was obtained from a "
            f"previous paginated response. Original error: {exc}"
        ) from exc


# ---------------------------------------------------------------------------
# Page-size validation
# ---------------------------------------------------------------------------
def validate_page_size(page_size: int | None = None) -> int:
    """Validate and normalize the requested page size.

    If *page_size* is ``None``, returns :data:`DEFAULT_PAGE_SIZE`.
    Otherwise the value is coerced to ``int`` and clamped to the range
    [``MIN_PAGE_SIZE``, ``MAX_PAGE_SIZE``].

    Args:
        page_size: The requested number of items per page.  Accepts
            ``None`` (uses default), ``int``, or any value castable to
            ``int``.

    Returns:
        The validated page size, guaranteed to satisfy
        ``MIN_PAGE_SIZE <= result <= MAX_PAGE_SIZE``.

    Example::

        >>> validate_page_size(None)
        20
        >>> validate_page_size(50)
        50
        >>> validate_page_size(200)
        100
        >>> validate_page_size(-5)
        1
        >>> validate_page_size(0)
        1
    """
    if page_size is None:
        return DEFAULT_PAGE_SIZE

    try:
        page_size_int: int = int(page_size)
    except (TypeError, ValueError):
        return DEFAULT_PAGE_SIZE

    return max(MIN_PAGE_SIZE, min(page_size_int, MAX_PAGE_SIZE))


# ---------------------------------------------------------------------------
# Document serialization
# ---------------------------------------------------------------------------
def serialize_document(doc: dict) -> dict:
    """Serialize a MongoDB document for JSON-safe API responses.

    Recursively walks the document and converts BSON-specific types to
    their JSON-serializable Python equivalents:

    * ``ObjectId`` → ``str`` (24-character hex string)
    * ``datetime.datetime`` → ``str`` (ISO 8601 format via ``isoformat()``)
    * Nested ``dict`` values are recursively serialized.
    * ``list`` values containing ``dict``, ``ObjectId``, or ``datetime``
      elements are serialized element-wise.

    Args:
        doc: A MongoDB document dictionary, typically obtained from a
            ``find()`` query result.

    Returns:
        A **new** dictionary with all BSON types converted to
        JSON-serializable Python primitives.  The original *doc* is not
        mutated.

    Example::

        >>> from bson import ObjectId
        >>> import datetime
        >>> doc = {
        ...     "_id": ObjectId("64ab2c3d5e6f7a8b9c0d1e2f"),
        ...     "name": "test",
        ...     "created_at": datetime.datetime(2024, 1, 15, 12, 0, 0),
        ... }
        >>> result = serialize_document(doc)
        >>> result["_id"]
        '64ab2c3d5e6f7a8b9c0d1e2f'
        >>> isinstance(result["created_at"], str)
        True
    """
    if not isinstance(doc, dict):
        return doc

    serialized: dict = {}
    for key, value in doc.items():
        if isinstance(value, ObjectId):
            serialized[key] = str(value)
        elif isinstance(value, datetime.datetime):
            serialized[key] = value.isoformat()
        elif isinstance(value, dict):
            serialized[key] = serialize_document(value)
        elif isinstance(value, list):
            serialized[key] = _serialize_list(value)
        else:
            serialized[key] = value
    return serialized


def _serialize_list(items: list) -> list:
    """Serialize a list of values, converting BSON types element-wise.

    Internal helper used by :func:`serialize_document` to handle list
    fields in MongoDB documents.

    Args:
        items: A list of values from a MongoDB document field.

    Returns:
        A new list with BSON types converted to JSON-serializable primitives.
    """
    serialized: list = []
    for item in items:
        if isinstance(item, dict):
            serialized.append(serialize_document(item))
        elif isinstance(item, ObjectId):
            serialized.append(str(item))
        elif isinstance(item, datetime.datetime):
            serialized.append(item.isoformat())
        elif isinstance(item, list):
            serialized.append(_serialize_list(item))
        else:
            serialized.append(item)
    return serialized


# ---------------------------------------------------------------------------
# Core pagination query
# ---------------------------------------------------------------------------
def paginate_query(
    collection: Collection,
    query_filter: dict[str, Any],
    page_size: int | None = None,
    cursor: str | None = None,
    direction: str = "forward",
    sort_field: str = "_id",
    sort_order: int = 1,
    projection: dict[str, Any] | None = None,
) -> PaginatedResponse:
    """Execute a cursor-based paginated query on a MongoDB collection.

    Performs a MongoDB ``find()`` with cursor-based pagination, supporting
    both **forward** and **backward** navigation.  The implementation
    fetches ``page_size + 1`` documents so that it can determine whether
    additional pages exist without an extra count query for ``has_more``.

    **Multi-tenant isolation (R-007):**  Callers *must* include a
    ``tenant_id`` key in *query_filter* to enforce namespace isolation.
    This function does **not** inject tenant scope automatically — it is the
    responsibility of the calling route handler or middleware.

    Algorithm:
        1. Validate and clamp *page_size*.
        2. Deep-copy *query_filter* as *base_filter* (used later for
           ``count_documents``).
        3. If a *cursor* is provided, decode it and augment the filter with
           an ``_id`` comparison operator that depends on *direction* and
           *sort_order*.
        4. Execute ``find().sort().limit(page_size + 1)`` to fetch one extra
           document for ``has_more`` detection.
        5. Trim results to *page_size* and, for backward navigation, reverse
           the list to restore the expected ordering.
        6. Build ``next_cursor`` and ``prev_cursor`` tokens from the first /
           last document ``_id`` values.
        7. Obtain ``total_count`` via ``count_documents(base_filter)``.
        8. Serialize every document for JSON-safe output.
        9. Return a :class:`PaginatedResponse`.

    Args:
        collection: The PyMongo ``Collection`` instance to query.
        query_filter: MongoDB query filter dictionary.  **Must** include
            ``tenant_id`` for multi-tenant isolation per R-007.
        page_size: Number of items per page.  Defaults to
            :data:`DEFAULT_PAGE_SIZE` (20).  Clamped to
            [:data:`MIN_PAGE_SIZE`, :data:`MAX_PAGE_SIZE`].
        cursor: Base64-encoded cursor token from a previous
            :class:`PaginatedResponse`.  ``None`` for the first page.
        direction: Pagination direction — ``'forward'`` (default) to
            navigate toward newer / later items, or ``'backward'`` to
            navigate toward older / earlier items.
        sort_field: MongoDB field name to sort by.  Defaults to ``'_id'``.
        sort_order: Sort direction: ``1`` for ascending, ``-1`` for
            descending.  Defaults to ``1``.
        projection: Optional MongoDB projection dictionary to limit the
            fields returned in each document.

    Returns:
        A :class:`PaginatedResponse` containing the page of serialized
        documents, forward / backward cursor tokens, total document count,
        and pagination metadata.

    Raises:
        ValueError: If *cursor* is provided but malformed or invalid.
        ValueError: If *direction* is not ``'forward'`` or ``'backward'``.

    Example::

        >>> from pymongo import MongoClient
        >>> db = MongoClient("mongodb://localhost:27017").synth_erp
        >>> # First page
        >>> page1 = paginate_query(
        ...     collection=db.generation_profiles,
        ...     query_filter={"tenant_id": "tenant-001"},
        ...     page_size=20,
        ... )
        >>> page1.has_more
        True
        >>> # Second page
        >>> page2 = paginate_query(
        ...     collection=db.generation_profiles,
        ...     query_filter={"tenant_id": "tenant-001"},
        ...     page_size=20,
        ...     cursor=page1.next_cursor,
        ... )
    """
    # ------------------------------------------------------------------
    # 1. Validate inputs
    # ------------------------------------------------------------------
    validated_page_size: int = validate_page_size(page_size)

    if direction not in ("forward", "backward"):
        raise ValueError(
            f"Invalid pagination direction '{direction}'. "
            "Must be 'forward' or 'backward'."
        )

    # ------------------------------------------------------------------
    # 2. Preserve original filter for total_count
    # ------------------------------------------------------------------
    base_filter: dict[str, Any] = copy.deepcopy(query_filter)

    # ------------------------------------------------------------------
    # 3. Augment filter with cursor condition
    # ------------------------------------------------------------------
    paginated_filter: dict[str, Any] = copy.deepcopy(query_filter)
    effective_sort_order: int = sort_order

    if cursor is not None:
        decoded_id: ObjectId = decode_cursor(cursor)

        if direction == "forward":
            if sort_order == 1:
                # Ascending: fetch documents with _id greater than cursor
                paginated_filter["_id"] = {"$gt": decoded_id}
            else:
                # Descending: fetch documents with _id less than cursor
                paginated_filter["_id"] = {"$lt": decoded_id}
        # Backward navigation — temporarily reverse sort so we can
        # grab the preceding slice, then reverse results later.
        elif sort_order == 1:
            paginated_filter["_id"] = {"$lt": decoded_id}
            effective_sort_order = -1
        else:
            paginated_filter["_id"] = {"$gt": decoded_id}
            effective_sort_order = 1

    # ------------------------------------------------------------------
    # 4. Execute query — fetch page_size + 1 for has_more detection
    # ------------------------------------------------------------------
    mongo_cursor = (
        collection.find(filter=paginated_filter, projection=projection)
        .sort(sort_field, effective_sort_order)
        .limit(validated_page_size + 1)
    )

    results: list[dict] = list(mongo_cursor)

    # ------------------------------------------------------------------
    # 5. Determine has_more and trim
    # ------------------------------------------------------------------
    has_more: bool = len(results) > validated_page_size
    if has_more:
        results = results[:validated_page_size]

    # Restore natural ordering after a backward fetch
    if direction == "backward" and results:
        results.reverse()

    # ------------------------------------------------------------------
    # 6. Build cursor tokens
    # ------------------------------------------------------------------
    next_cursor: str | None = None
    prev_cursor: str | None = None

    if results:
        if has_more:
            next_cursor = encode_cursor(results[-1]["_id"])
        if cursor is not None:
            prev_cursor = encode_cursor(results[0]["_id"])

    # ------------------------------------------------------------------
    # 7. Total count (uses base filter without cursor conditions)
    # ------------------------------------------------------------------
    total_count: int = collection.count_documents(base_filter)

    # ------------------------------------------------------------------
    # 8. Serialize documents for JSON-safe output
    # ------------------------------------------------------------------
    serialized_items: list[dict] = [
        serialize_document(doc) for doc in results
    ]

    # ------------------------------------------------------------------
    # 9. Construct and return paginated response
    # ------------------------------------------------------------------
    return PaginatedResponse(
        items=serialized_items,
        next_cursor=next_cursor,
        prev_cursor=prev_cursor,
        total_count=total_count,
        has_more=has_more,
        page_size=validated_page_size,
    )
