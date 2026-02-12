"""MongoDB document model for the ``generation_profiles`` collection.

This module defines the :class:`GenerationJob` data-access class that provides
the complete CRUD lifecycle for synthetic data generation jobs within the
Synthetic ERP Data Generation Platform.

**Collection:** ``generation_profiles``

**Key Design Decisions:**

- **Multi-tenant isolation (R-007):** Every query and update method requires a
  ``tenant_id`` parameter, and all MongoDB queries include ``tenant_id`` in the
  filter to ensure cross-tenant data access is impossible by design.
- **Status state machine:** Jobs progress through a well-defined lifecycle::

      Submitted → Generating → Validating → Certifying → Provisioning → Completed
                ↘ Failed (from any state)

- **UUID-based identifiers:** Each job receives a UUID4 ``job_id`` to avoid
  exposing MongoDB ``ObjectId`` values in API responses.
- **Audit-friendly logging (C-004 / SOC 2 Type II):** All CRUD operations emit
  structured log events containing ``job_id`` and ``tenant_id`` — never sensitive
  ``schema_config`` data (per R-005).

**Indexes:**

1. Compound: ``(tenant_id ASC, status ASC, created_at DESC)`` — tenant-scoped
   filtered queries with chronological ordering.
2. Unique: ``(job_id ASC)`` — fast lookups and uniqueness enforcement.
3. Standard: ``(user_id ASC)`` — user-scoped queries.
4. Standard: ``(created_at DESC)`` — global chronological sorting.

Example::

    from api_gateway.models.generation_job import GenerationJob, JobStatus

    # Create a new generation job
    job = GenerationJob.create(
        tenant_id="tenant-abc",
        user_id="user-123",
        generation_method="ai_ml",
        schema_config={
            "tables": [
                {
                    "table_name": "GL_ENTRIES",
                    "columns": ["posting_date", "amount", "account_id"],
                    "record_count": 50000,
                }
            ]
        },
        output_format="parquet",
    )

    # Retrieve by ID (tenant-scoped)
    job = GenerationJob.find_by_id(job["job_id"], tenant_id="tenant-abc")

    # Transition to generating status
    GenerationJob.update_status(job["job_id"], "tenant-abc", "generating")
"""

from __future__ import annotations

import enum
import logging
import uuid
from datetime import UTC, datetime
from typing import Any

from pymongo import ASCENDING, DESCENDING, IndexModel
from pymongo.collection import Collection, ReturnDocument
from pymongo.errors import DuplicateKeyError, OperationFailure

from api_gateway.extensions import get_db


# ---------------------------------------------------------------------------
# Module-level logger — leverages structlog ProcessorFormatter when configured
# at the application level for structured JSON output with correlation IDs.
# ---------------------------------------------------------------------------
logger: logging.Logger = logging.getLogger(__name__)


# ===================================================================
# Enumerations
# ===================================================================


class JobStatus(enum.StrEnum):
    """Seven-state lifecycle for generation jobs.

    The generation job state machine follows this progression::

        Submitted → Generating → Validating → Certifying → Provisioning → Completed
                  ↘ Failed (reachable from any non-terminal state)

    Inheriting from ``enum.StrEnum`` enables clean MongoDB serialization —
    enum values are stored as plain strings in the document and can be
    compared directly against query filter strings.

    Attributes:
        SUBMITTED: Job has been created and is awaiting generation.
        GENERATING: Generation Engine is actively producing synthetic records.
        VALIDATING: Quality Service is running fidelity and rules checks.
        CERTIFYING: Compliance Service is performing PII and regulatory scans.
        PROVISIONING: Provisioning Service is exporting to the target destination.
        COMPLETED: All pipeline stages finished successfully.
        FAILED: An unrecoverable error halted the pipeline.
    """

    SUBMITTED = "submitted"
    GENERATING = "generating"
    VALIDATING = "validating"
    CERTIFYING = "certifying"
    PROVISIONING = "provisioning"
    COMPLETED = "completed"
    FAILED = "failed"


class GenerationMethod(enum.StrEnum):
    """Supported synthetic data generation strategies.

    Each method corresponds to a distinct generator implementation in the
    Generation Engine service:

    Attributes:
        AI_ML: GAN/VAE-based neural generation via PyTorch/TensorFlow.
        RULES_BASED: Business-rules engine with constraint evaluation.
        STATISTICAL: Distribution-fitted synthesis via SciPy/NumPy.
        MASKING: Intelligent data masking with privacy preservation.
    """

    AI_ML = "ai_ml"
    RULES_BASED = "rules_based"
    STATISTICAL = "statistical"
    MASKING = "masking"


class OutputFormat(enum.StrEnum):
    """Supported output formats for generated synthetic data.

    Attributes:
        SQL: INSERT / COPY statements for direct database ingestion.
        CSV: Delimited text output (configurable delimiter).
        JSON: JSON / JSON Lines structured output.
        PARQUET: Apache Parquet columnar format optimised for analytics.
    """

    SQL = "sql"
    CSV = "csv"
    JSON = "json"
    PARQUET = "parquet"


# ===================================================================
# Constants
# ===================================================================

COLLECTION_NAME: str = "generation_profiles"
"""MongoDB collection name used to persist generation job documents."""


# ===================================================================
# Data-Access Class
# ===================================================================


class GenerationJob:
    """MongoDB data-access object for the ``generation_profiles`` collection.

    All public methods are ``@classmethod`` instances that obtain the PyMongo
    :class:`~pymongo.collection.Collection` internally via :meth:`get_collection`,
    so callers never need to manage collection references directly.

    **Multi-tenant isolation (R-007):** Every query and mutation method requires
    a ``tenant_id`` parameter.  All MongoDB filters include ``tenant_id`` to
    guarantee that one tenant cannot read, modify, or delete another tenant's
    generation jobs.

    **Audit logging (C-004):** All state-changing operations (create, update,
    delete) emit structured log events with ``job_id`` and ``tenant_id``.
    Sensitive fields such as ``schema_config`` are never logged.
    """

    # ------------------------------------------------------------------
    # Instance initialisation
    # ------------------------------------------------------------------

    def __init__(self, collection: Collection) -> None:
        """Initialise the GenerationJob instance with a PyMongo Collection.

        Args:
            collection: A PyMongo ``Collection`` instance pointing to the
                ``generation_profiles`` collection.
        """
        self._collection: Collection = collection
        logger.info(
            "generation_job_model_initialized",
            extra={"collection": collection.name},
        )

    # ------------------------------------------------------------------
    # Collection access
    # ------------------------------------------------------------------

    @classmethod
    def get_collection(cls) -> Collection:
        """Return the ``generation_profiles`` PyMongo Collection.

        Retrieves the MongoDB ``Database`` via the shared :func:`get_db`
        helper (which reads from the Flask application context or falls back
        to the process-wide singleton) and indexes into it by
        :data:`COLLECTION_NAME`.

        Returns:
            Collection: The PyMongo ``Collection`` object for
                ``generation_profiles``.

        Raises:
            RuntimeError: If no MongoDB connection is available (e.g. no Flask
                application context and no process-wide singleton).
        """
        return get_db()[COLLECTION_NAME]

    # ------------------------------------------------------------------
    # Index management
    # ------------------------------------------------------------------

    @classmethod
    def ensure_indexes(cls, collection: Collection) -> None:
        """Create required indexes on the ``generation_profiles`` collection.

        Indexes created:

        1. **Compound** ``(tenant_id ASC, status ASC, created_at DESC)`` —
           Optimises the most common query pattern: listing jobs for a specific
           tenant filtered by status and sorted by recency.
        2. **Unique** ``(job_id ASC)`` — Guarantees UUID uniqueness across the
           entire collection and enables fast point lookups.
        3. **Standard** ``(user_id ASC)`` — Supports user-scoped queries
           (e.g. "show my jobs").
        4. **Standard** ``(created_at DESC)`` — Supports global chronological
           sorting without a full collection scan.

        This method is **idempotent** — calling it multiple times is safe because
        MongoDB skips index creation when a matching index already exists.

        Args:
            collection: The PyMongo ``Collection`` to create indexes on.
                Typically obtained via :meth:`get_collection`.

        Raises:
            pymongo.errors.OperationFailure: If MongoDB rejects the index
                definitions (e.g. conflicting options on an existing index
                with the same key pattern).
        """
        indexes = [
            IndexModel(
                [
                    ("tenant_id", ASCENDING),
                    ("status", ASCENDING),
                    ("created_at", DESCENDING),
                ],
                name="idx_tenant_status_created",
                background=True,
            ),
            IndexModel(
                [("job_id", ASCENDING)],
                name="idx_job_id_unique",
                unique=True,
                background=True,
            ),
            IndexModel(
                [("user_id", ASCENDING)],
                name="idx_user_id",
                background=True,
            ),
            IndexModel(
                [("created_at", DESCENDING)],
                name="idx_created_at_desc",
                background=True,
            ),
        ]

        try:
            created_names = collection.create_indexes(indexes)
            logger.info(
                "generation_job_indexes_ensured",
                extra={"indexes_created": created_names},
            )
        except OperationFailure as exc:
            logger.error(
                "generation_job_index_creation_failed",
                extra={
                    "error": str(exc),
                    "error_code": getattr(exc, "code", None),
                },
            )
            raise

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    @staticmethod
    def _serialize_document(doc: dict[str, Any] | None) -> dict[str, Any] | None:
        """Convert a raw MongoDB document for JSON-safe serialisation.

        Transforms the MongoDB-internal ``_id`` (``bson.ObjectId``) field to a
        plain string so that the document can be returned directly in API
        responses without additional post-processing.

        Args:
            doc: A raw MongoDB document dict, or ``None``.

        Returns:
            The document with ``_id`` converted to a string, or ``None``
            if the input was ``None``.
        """
        if doc is None:
            return None
        if "_id" in doc:
            doc["_id"] = str(doc["_id"])
        return doc

    @staticmethod
    def _validate_generation_method(method: str) -> str:
        """Validate and normalise a generation method string.

        Args:
            method: The generation method string to validate.

        Returns:
            The validated generation method value (one of the
            :class:`GenerationMethod` enum values).

        Raises:
            ValueError: If ``method`` is not one of the supported
                :class:`GenerationMethod` values.
        """
        try:
            return GenerationMethod(method).value
        except ValueError:
            valid = [m.value for m in GenerationMethod]
            raise ValueError(
                f"Invalid generation_method '{method}'. "
                f"Must be one of: {valid}"
            ) from None

    @staticmethod
    def _validate_output_format(fmt: str) -> str:
        """Validate and normalise an output format string.

        Args:
            fmt: The output format string to validate.

        Returns:
            The validated output format value (one of the
            :class:`OutputFormat` enum values).

        Raises:
            ValueError: If ``fmt`` is not one of the supported
                :class:`OutputFormat` values.
        """
        try:
            return OutputFormat(fmt).value
        except ValueError:
            valid = [f.value for f in OutputFormat]
            raise ValueError(
                f"Invalid output_format '{fmt}'. Must be one of: {valid}"
            ) from None

    @staticmethod
    def _validate_status(status: str) -> str:
        """Validate and normalise a job status string.

        Args:
            status: The status string to validate.

        Returns:
            The validated status value (one of the :class:`JobStatus`
            enum values).

        Raises:
            ValueError: If ``status`` is not one of the supported
                :class:`JobStatus` values.
        """
        try:
            return JobStatus(status).value
        except ValueError:
            valid = [s.value for s in JobStatus]
            raise ValueError(
                f"Invalid status '{status}'. Must be one of: {valid}"
            ) from None

    @staticmethod
    def _calculate_total_records(schema_config: dict[str, Any]) -> int:
        """Sum ``record_count`` across all tables in the schema configuration.

        Args:
            schema_config: Schema configuration dict.  Expected to contain a
                ``tables`` list where each entry has a ``record_count`` field.

        Returns:
            Total number of requested records across all tables.  Returns ``0``
            if no tables are found or none have ``record_count``.
        """
        tables = schema_config.get("tables", [])
        return sum(
            table.get("record_count", 0)
            for table in tables
            if isinstance(table, dict)
        )

    # ------------------------------------------------------------------
    # CRUD: Create
    # ------------------------------------------------------------------

    @classmethod
    def create(
        cls,
        tenant_id: str,
        user_id: str,
        generation_method: str,
        schema_config: dict[str, Any],
        output_format: str,
        **kwargs: Any,
    ) -> dict[str, Any]:
        """Create a new generation job document in the collection.

        Generates a UUID4 ``job_id``, validates the ``generation_method`` and
        ``output_format`` against their respective enums, computes the
        ``total_records_requested`` from the ``schema_config``, sets the initial
        status to ``submitted``, and inserts the document into MongoDB.

        Args:
            tenant_id: The tenant namespace identifier (R-007 multi-tenant
                isolation).
            user_id: The authenticated user who initiated the job.
            generation_method: One of ``'ai_ml'``, ``'rules_based'``,
                ``'statistical'``, ``'masking'``.
            schema_config: Dict describing the tables, columns, and record
                counts for generation.  Expected structure::

                    {
                        "tables": [
                            {
                                "table_name": "GL_ENTRIES",
                                "columns": ["col1", "col2"],
                                "record_count": 50000
                            }
                        ]
                    }

            output_format: One of ``'sql'``, ``'csv'``, ``'json'``,
                ``'parquet'``.
            **kwargs: Optional additional fields.  Recognised keys:

                - ``metadata`` (dict): Arbitrary user-supplied metadata
                  attached to the job document.

        Returns:
            The inserted document dict with ``_id`` serialised to string and
            all fields populated (including the generated ``job_id``).

        Raises:
            ValueError: If ``generation_method`` or ``output_format`` are
                not valid enum values.
            pymongo.errors.DuplicateKeyError: If a document with the same
                ``job_id`` already exists (extremely unlikely with UUID4 but
                handled for correctness).
            pymongo.errors.OperationFailure: If the MongoDB insert operation
                fails.
        """
        # Validate enum fields before building the document.
        validated_method: str = cls._validate_generation_method(generation_method)
        validated_format: str = cls._validate_output_format(output_format)

        job_id: str = str(uuid.uuid4())
        now: datetime = datetime.now(UTC)

        document: dict[str, Any] = {
            "job_id": job_id,
            "tenant_id": tenant_id,
            "user_id": user_id,
            "status": JobStatus.SUBMITTED.value,
            "generation_method": validated_method,
            "schema_config": schema_config,
            "output_format": validated_format,
            "quality_score": None,
            "quality_report": None,
            "compliance_certificate": None,
            "error_message": None,
            "progress_percentage": 0,
            "total_records_requested": cls._calculate_total_records(schema_config),
            "total_records_generated": 0,
            "created_at": now,
            "updated_at": now,
            "completed_at": None,
            "metadata": kwargs.get("metadata", {}),
        }

        collection: Collection = cls.get_collection()

        try:
            result = collection.insert_one(document)
            document["_id"] = str(result.inserted_id)

            # Audit log: creation event with non-sensitive identifiers only.
            logger.info(
                "generation_job_created",
                extra={
                    "job_id": job_id,
                    "tenant_id": tenant_id,
                    "user_id": user_id,
                    "generation_method": validated_method,
                    "output_format": validated_format,
                    "status": JobStatus.SUBMITTED.value,
                    "total_records_requested": document["total_records_requested"],
                },
            )

            return document

        except DuplicateKeyError:
            logger.error(
                "generation_job_duplicate_key",
                extra={"job_id": job_id, "tenant_id": tenant_id},
            )
            raise
        except OperationFailure as exc:
            logger.error(
                "generation_job_create_failed",
                extra={
                    "job_id": job_id,
                    "tenant_id": tenant_id,
                    "error": str(exc),
                    "error_code": getattr(exc, "code", None),
                },
            )
            raise

    # ------------------------------------------------------------------
    # CRUD: Read
    # ------------------------------------------------------------------

    @classmethod
    def find_by_id(
        cls,
        job_id: str,
        tenant_id: str,
    ) -> dict[str, Any] | None:
        """Retrieve a single generation job by ``job_id``.

        The query always includes ``tenant_id`` in the filter to enforce
        multi-tenant isolation (R-007).  A tenant cannot retrieve another
        tenant's job even if they possess the ``job_id``.

        Args:
            job_id: The UUID4 identifier of the generation job.
            tenant_id: The tenant namespace identifier (R-007).

        Returns:
            The job document dict with ``_id`` serialised to string, or
            ``None`` if no matching document was found.

        Raises:
            pymongo.errors.OperationFailure: If the MongoDB query fails.
        """
        collection: Collection = cls.get_collection()

        try:
            doc = collection.find_one(
                {"job_id": job_id, "tenant_id": tenant_id}
            )
            return cls._serialize_document(doc)
        except OperationFailure as exc:
            logger.error(
                "generation_job_find_by_id_failed",
                extra={
                    "job_id": job_id,
                    "tenant_id": tenant_id,
                    "error": str(exc),
                    "error_code": getattr(exc, "code", None),
                },
            )
            raise

    @classmethod
    def find_by_tenant(
        cls,
        tenant_id: str,
        status: str | None = None,
        page: int = 1,
        page_size: int = 20,
        sort_by: str = "created_at",
        sort_order: int = DESCENDING,
    ) -> dict[str, Any]:
        """Retrieve paginated generation jobs for a tenant.

        Builds a query filter scoped to the given ``tenant_id`` (R-007),
        optionally filtered by ``status``, and returns a paginated result set
        with metadata.

        Args:
            tenant_id: The tenant namespace identifier (R-007).
            status: Optional job status filter (e.g. ``'generating'``).  If
                provided, must be a valid :class:`JobStatus` value.
            page: 1-based page number.  Defaults to ``1``.  Values below 1 are
                clamped to 1.
            page_size: Maximum number of documents per page.  Defaults to ``20``.
                Clamped to the range ``[1, 100]``.
            sort_by: Document field to sort by.  Defaults to ``'created_at'``.
            sort_order: Sort direction — :data:`~pymongo.ASCENDING` (``1``) or
                :data:`~pymongo.DESCENDING` (``-1``).  Defaults to
                :data:`~pymongo.DESCENDING`.

        Returns:
            A dict with pagination metadata::

                {
                    "items": [<job document>, ...],
                    "total": <int>,
                    "page": <int>,
                    "page_size": <int>,
                    "has_next": <bool>
                }

        Raises:
            ValueError: If ``status`` is provided but is not a valid
                :class:`JobStatus` value.
            pymongo.errors.OperationFailure: If the MongoDB query fails.
        """
        query_filter: dict[str, Any] = {"tenant_id": tenant_id}

        if status is not None:
            validated_status: str = cls._validate_status(status)
            query_filter["status"] = validated_status

        # Clamp pagination parameters to sensible bounds.
        page = max(1, page)
        page_size = max(1, min(page_size, 100))
        skip: int = (page - 1) * page_size

        collection: Collection = cls.get_collection()

        try:
            total: int = collection.count_documents(query_filter)

            cursor = (
                collection.find(query_filter)
                .sort(sort_by, sort_order)
                .skip(skip)
                .limit(page_size)
            )

            items: list[dict[str, Any]] = [
                serialized
                for doc in cursor
                if (serialized := cls._serialize_document(doc)) is not None
            ]

            has_next: bool = (skip + page_size) < total

            return {
                "items": items,
                "total": total,
                "page": page,
                "page_size": page_size,
                "has_next": has_next,
            }

        except OperationFailure as exc:
            logger.error(
                "generation_job_find_by_tenant_failed",
                extra={
                    "tenant_id": tenant_id,
                    "error": str(exc),
                    "error_code": getattr(exc, "code", None),
                },
            )
            raise

    # ------------------------------------------------------------------
    # CRUD: Update — Status
    # ------------------------------------------------------------------

    @classmethod
    def update_status(
        cls,
        job_id: str,
        tenant_id: str,
        new_status: str,
        error_message: str | None = None,
    ) -> dict[str, Any] | None:
        """Transition a generation job to a new lifecycle status.

        Validates ``new_status`` against the :class:`JobStatus` enum, updates
        the ``status`` and ``updated_at`` fields, and conditionally sets
        terminal-state fields:

        - **COMPLETED / FAILED:** ``completed_at`` is set to the current UTC
          timestamp.
        - **FAILED:** ``error_message`` is populated when provided.

        Args:
            job_id: The UUID4 identifier of the generation job.
            tenant_id: The tenant namespace identifier (R-007).
            new_status: The target status value.  Must be a valid
                :class:`JobStatus` member value.
            error_message: Optional error description.  Used when
                transitioning to ``FAILED``.

        Returns:
            The **post-update** document dict with ``_id`` serialised to
            string, or ``None`` if no matching document was found.

        Raises:
            ValueError: If ``new_status`` is not a valid :class:`JobStatus`
                value.
            pymongo.errors.OperationFailure: If the MongoDB update fails.
        """
        validated_status: str = cls._validate_status(new_status)
        now: datetime = datetime.now(UTC)

        update_fields: dict[str, Any] = {
            "status": validated_status,
            "updated_at": now,
        }

        # Set completed_at for terminal states.
        if validated_status in (JobStatus.COMPLETED.value, JobStatus.FAILED.value):
            update_fields["completed_at"] = now

        # Attach error message for failed jobs.
        if validated_status == JobStatus.FAILED.value and error_message is not None:
            update_fields["error_message"] = error_message

        collection: Collection = cls.get_collection()

        try:
            doc = collection.find_one_and_update(
                {"job_id": job_id, "tenant_id": tenant_id},
                {"$set": update_fields},
                return_document=ReturnDocument.AFTER,
            )

            result = cls._serialize_document(doc)

            if result is not None:
                # Audit log: status transition event.
                logger.info(
                    "generation_job_status_updated",
                    extra={
                        "job_id": job_id,
                        "tenant_id": tenant_id,
                        "new_status": validated_status,
                    },
                )
            else:
                logger.warning(
                    "generation_job_status_update_not_found",
                    extra={
                        "job_id": job_id,
                        "tenant_id": tenant_id,
                        "requested_status": validated_status,
                    },
                )

            return result

        except OperationFailure as exc:
            logger.error(
                "generation_job_status_update_failed",
                extra={
                    "job_id": job_id,
                    "tenant_id": tenant_id,
                    "new_status": validated_status,
                    "error": str(exc),
                    "error_code": getattr(exc, "code", None),
                },
            )
            raise

    # ------------------------------------------------------------------
    # CRUD: Update — Progress
    # ------------------------------------------------------------------

    @classmethod
    def update_progress(
        cls,
        job_id: str,
        tenant_id: str,
        progress_percentage: int,
        records_generated: int,
    ) -> dict[str, Any] | None:
        """Update generation progress for a running job.

        Progress values are clamped to valid ranges to prevent erroneous
        upstream callers from persisting impossible percentages or negative
        record counts.

        Args:
            job_id: The UUID4 identifier of the generation job.
            tenant_id: The tenant namespace identifier (R-007).
            progress_percentage: Integer percentage (0-100) indicating how
                much of the generation is complete.  Values outside the range
                are clamped.
            records_generated: Number of records generated so far.  Negative
                values are clamped to 0.

        Returns:
            The **post-update** document dict, or ``None`` if no matching
            document was found.

        Raises:
            pymongo.errors.OperationFailure: If the MongoDB update fails.
        """
        clamped_progress: int = max(0, min(progress_percentage, 100))
        clamped_records: int = max(0, records_generated)

        collection: Collection = cls.get_collection()

        try:
            doc = collection.find_one_and_update(
                {"job_id": job_id, "tenant_id": tenant_id},
                {
                    "$set": {
                        "progress_percentage": clamped_progress,
                        "total_records_generated": clamped_records,
                        "updated_at": datetime.now(UTC),
                    }
                },
                return_document=ReturnDocument.AFTER,
            )

            result = cls._serialize_document(doc)

            if result is not None:
                logger.info(
                    "generation_job_progress_updated",
                    extra={
                        "job_id": job_id,
                        "tenant_id": tenant_id,
                        "progress_percentage": clamped_progress,
                        "records_generated": clamped_records,
                    },
                )

            return result

        except OperationFailure as exc:
            logger.error(
                "generation_job_progress_update_failed",
                extra={
                    "job_id": job_id,
                    "tenant_id": tenant_id,
                    "error": str(exc),
                    "error_code": getattr(exc, "code", None),
                },
            )
            raise

    # ------------------------------------------------------------------
    # CRUD: Update — Quality Score
    # ------------------------------------------------------------------

    @classmethod
    def update_quality_score(
        cls,
        job_id: str,
        tenant_id: str,
        quality_score: float,
        quality_report: dict[str, Any] | None = None,
    ) -> dict[str, Any] | None:
        """Update the quality score and optionally the full quality report.

        The weighted quality scoring model is:
        ``Q = 0.4 x S_statistical + 0.3 x S_business_rules + 0.3 x S_referential_integrity``
        where each component score is normalised to ``[0.0, 1.0]``.

        Args:
            job_id: The UUID4 identifier of the generation job.
            tenant_id: The tenant namespace identifier (R-007).
            quality_score: Composite quality score in ``[0.0, 1.0]``.
            quality_report: Optional detailed quality report dict containing
                per-dimension scores, metric breakdowns, and validation
                results.

        Returns:
            The **post-update** document dict, or ``None`` if no matching
            document was found.

        Raises:
            ValueError: If ``quality_score`` is outside ``[0.0, 1.0]``.
            pymongo.errors.OperationFailure: If the MongoDB update fails.
        """
        if not 0.0 <= quality_score <= 1.0:
            raise ValueError(
                f"quality_score must be between 0.0 and 1.0, got {quality_score}"
            )

        update_fields: dict[str, Any] = {
            "quality_score": quality_score,
            "updated_at": datetime.now(UTC),
        }

        if quality_report is not None:
            update_fields["quality_report"] = quality_report

        collection: Collection = cls.get_collection()

        try:
            doc = collection.find_one_and_update(
                {"job_id": job_id, "tenant_id": tenant_id},
                {"$set": update_fields},
                return_document=ReturnDocument.AFTER,
            )

            result = cls._serialize_document(doc)

            if result is not None:
                logger.info(
                    "generation_job_quality_score_updated",
                    extra={
                        "job_id": job_id,
                        "tenant_id": tenant_id,
                        "quality_score": quality_score,
                    },
                )

            return result

        except OperationFailure as exc:
            logger.error(
                "generation_job_quality_score_update_failed",
                extra={
                    "job_id": job_id,
                    "tenant_id": tenant_id,
                    "error": str(exc),
                    "error_code": getattr(exc, "code", None),
                },
            )
            raise

    # ------------------------------------------------------------------
    # CRUD: Update — Compliance Certificate
    # ------------------------------------------------------------------

    @classmethod
    def update_compliance_certificate(
        cls,
        job_id: str,
        tenant_id: str,
        certificate: dict[str, Any],
    ) -> dict[str, Any] | None:
        """Attach a compliance certificate to a generation job.

        The Compliance Service issues certificates after a dataset passes PII
        detection and regulatory verification (GDPR, HIPAA, CCPA).  The
        certificate is stored as a sub-document within the generation job
        for audit trail purposes (C-004).

        Args:
            job_id: The UUID4 identifier of the generation job.
            tenant_id: The tenant namespace identifier (R-007).
            certificate: Compliance certificate dict.  Expected structure::

                    {
                        "certificate_id": "<uuid>",
                        "issued_at": "<ISO 8601 datetime>",
                        "regulations_checked": ["GDPR", "HIPAA", "CCPA"],
                        "pii_detected": false,
                        "status": "certified"
                    }

        Returns:
            The **post-update** document dict, or ``None`` if no matching
            document was found.

        Raises:
            pymongo.errors.OperationFailure: If the MongoDB update fails.
        """
        collection: Collection = cls.get_collection()

        try:
            doc = collection.find_one_and_update(
                {"job_id": job_id, "tenant_id": tenant_id},
                {
                    "$set": {
                        "compliance_certificate": certificate,
                        "updated_at": datetime.now(UTC),
                    }
                },
                return_document=ReturnDocument.AFTER,
            )

            result = cls._serialize_document(doc)

            if result is not None:
                # Audit log: certificate attachment event.
                logger.info(
                    "generation_job_compliance_certificate_updated",
                    extra={
                        "job_id": job_id,
                        "tenant_id": tenant_id,
                        "certificate_id": certificate.get("certificate_id"),
                        "pii_detected": certificate.get("pii_detected"),
                        "regulations_checked": certificate.get(
                            "regulations_checked"
                        ),
                    },
                )

            return result

        except OperationFailure as exc:
            logger.error(
                "generation_job_compliance_certificate_update_failed",
                extra={
                    "job_id": job_id,
                    "tenant_id": tenant_id,
                    "error": str(exc),
                    "error_code": getattr(exc, "code", None),
                },
            )
            raise

    # ------------------------------------------------------------------
    # CRUD: Delete
    # ------------------------------------------------------------------

    @classmethod
    def delete(cls, job_id: str, tenant_id: str) -> bool:
        """Delete a generation job document.

        The query always includes ``tenant_id`` to enforce multi-tenant
        isolation (R-007) — a tenant cannot delete another tenant's job even
        if they possess the ``job_id``.

        Args:
            job_id: The UUID4 identifier of the generation job.
            tenant_id: The tenant namespace identifier (R-007).

        Returns:
            ``True`` if a document was deleted, ``False`` if no matching
            document was found.

        Raises:
            pymongo.errors.OperationFailure: If the MongoDB delete fails.
        """
        collection: Collection = cls.get_collection()

        try:
            result = collection.delete_one(
                {"job_id": job_id, "tenant_id": tenant_id}
            )
            deleted: bool = result.deleted_count > 0

            if deleted:
                # Audit log: deletion event.
                logger.info(
                    "generation_job_deleted",
                    extra={"job_id": job_id, "tenant_id": tenant_id},
                )
            else:
                logger.warning(
                    "generation_job_delete_not_found",
                    extra={"job_id": job_id, "tenant_id": tenant_id},
                )

            return deleted

        except OperationFailure as exc:
            logger.error(
                "generation_job_delete_failed",
                extra={
                    "job_id": job_id,
                    "tenant_id": tenant_id,
                    "error": str(exc),
                    "error_code": getattr(exc, "code", None),
                },
            )
            raise

    # ------------------------------------------------------------------
    # Aggregation
    # ------------------------------------------------------------------

    @classmethod
    def count_by_tenant(
        cls,
        tenant_id: str,
        status: str | None = None,
    ) -> int:
        """Count generation jobs for a tenant.

        Useful for dashboard summaries (e.g. "12 jobs completed, 3 in
        progress") and pagination metadata without fetching full documents.

        Args:
            tenant_id: The tenant namespace identifier (R-007).
            status: Optional status filter.  If provided, must be a valid
                :class:`JobStatus` value.

        Returns:
            The number of matching documents (``0`` if none found).

        Raises:
            ValueError: If ``status`` is provided but is not a valid
                :class:`JobStatus` value.
            pymongo.errors.OperationFailure: If the MongoDB count fails.
        """
        query_filter: dict[str, Any] = {"tenant_id": tenant_id}

        if status is not None:
            validated_status: str = cls._validate_status(status)
            query_filter["status"] = validated_status

        collection: Collection = cls.get_collection()

        try:
            count: int = collection.count_documents(query_filter)
            return count
        except OperationFailure as exc:
            logger.error(
                "generation_job_count_failed",
                extra={
                    "tenant_id": tenant_id,
                    "error": str(exc),
                    "error_code": getattr(exc, "code", None),
                },
            )
            raise
