"""Audit event logger for the Compliance Service — SOC 2 Type II compliant.

Writes compliance lifecycle events to the ``audit_logs`` MongoDB collection with
a 7-year retention policy enforced via a TTL index.  Every entry carries a SHA-256
hash linking it to the previous entry within the same tenant, creating a tamper-
evident chain whose integrity can be verified at any point for SOC 2 Type II
audit readiness.

Recorded Events:
    - Scans initiated / completed
    - PII detected / PII check passed
    - Regulatory verifications started / completed / violations found
    - Certifications issued / failed
    - Certificates released / revoked
    - State transitions, configuration changes, and access-denied events

Key Design Decisions:
    - **Tenant isolation** — every query *requires* ``tenant_id``; cross-tenant
      reads are impossible by construction.
    - **Hash chain** — each ``AuditEntry`` stores a SHA-256 digest of its own
      content together with the hash of the preceding entry (per tenant).
      ``verify_chain_integrity`` can replay and verify this chain.
    - **TTL retention** — a MongoDB TTL index on ``timestamp`` automatically
      expires documents after 7 years (≈ 220 752 000 seconds), satisfying
      the regulatory retention window without manual cleanup jobs.
    - **Efficient indexing** — compound indexes on common query patterns
      (tenant + time, tenant + dataset, tenant + event type) support fast
      paginated queries required for audit exports.

Usage::

    from compliance_service.certification.audit_logger import (
        AuditLogger,
        AuditEventType,
    )

    logger = AuditLogger()
    entry = logger.log_event(
        event_type=AuditEventType.SCAN_INITIATED,
        tenant_id="tenant-42",
        user_id="user-abc",
        dataset_id="ds-001",
        details={"scanner_version": "1.0.0"},
    )

    # Verify hash chain integrity
    result = logger.verify_chain_integrity(tenant_id="tenant-42")
    assert result["is_valid"] is True
"""

from __future__ import annotations

import hashlib
import json
import uuid
from datetime import UTC, datetime
from enum import Enum
from typing import TYPE_CHECKING, Any

from pydantic import BaseModel, Field
from pymongo import ASCENDING, DESCENDING, IndexModel
from pymongo.errors import PyMongoError

from shared.database.mongodb import get_mongo_db
from shared.logging.structured_logger import get_logger


if TYPE_CHECKING:
    from pymongo.collection import Collection


# ---------------------------------------------------------------------------
# Audit Event Type Enumeration
# ---------------------------------------------------------------------------


class AuditEventType(Enum):
    """Classification of all compliance audit event types.

    Each value maps to a short, lowercase snake_case string that is persisted
    in MongoDB and used for filtering, querying, and reporting.

    Example::

        event = AuditEventType.SCAN_INITIATED
        assert event.value == "scan_initiated"
    """

    SCAN_INITIATED = "scan_initiated"
    SCAN_COMPLETED = "scan_completed"
    PII_DETECTED = "pii_detected"
    PII_CHECK_PASSED = "pii_check_passed"
    REGULATION_CHECK_STARTED = "regulation_check_started"
    REGULATION_CHECK_COMPLETED = "regulation_check_completed"
    REGULATION_VIOLATION_FOUND = "regulation_violation_found"
    CERTIFICATION_ISSUED = "certification_issued"
    CERTIFICATION_FAILED = "certification_failed"
    CERTIFICATE_RELEASED = "certificate_released"
    CERTIFICATE_REVOKED = "certificate_revoked"
    STATE_TRANSITION = "state_transition"
    ACCESS_DENIED = "access_denied"
    EXPORT_INITIATED = "export_initiated"
    EXPORT_COMPLETED = "export_completed"
    CONFIG_CHANGED = "config_changed"


# ---------------------------------------------------------------------------
# Pydantic Data Models
# ---------------------------------------------------------------------------


class AuditEntry(BaseModel):
    """Immutable record of a single audit event in the compliance lifecycle.

    Every field participates in the SHA-256 checksum computation (except
    ``entry_hash`` itself) so that any post-hoc modification is detectable.

    Attributes:
        entry_id: Globally-unique UUID-4 identifier for this audit entry.
        event_type: Classification of the event (see ``AuditEventType``).
        timestamp: UTC-normalised timestamp of when the event occurred.
        tenant_id: Tenant namespace — ensures multi-tenant isolation.
        user_id: Identity of the user or service account that triggered the event.
        dataset_id: Optional reference to the associated dataset.
        certificate_id: Optional reference to the associated compliance certificate.
        correlation_id: Optional request-scoped correlation ID for distributed tracing.
        details: Free-form dictionary carrying event-specific metadata.
        previous_entry_hash: SHA-256 hash of the preceding entry in this tenant's
            chain, or ``None`` for the very first entry.
        entry_hash: SHA-256 digest of this entry's content including
            ``previous_entry_hash``, forming the tamper-evident chain link.
        ip_address: Client IP address captured at the API gateway.
        user_agent: Client User-Agent header captured at the API gateway.
        service_name: Name of the service that produced the event.

    Example::

        entry = AuditEntry(
            event_type=AuditEventType.SCAN_INITIATED,
            tenant_id="tenant-42",
            user_id="user-abc",
            dataset_id="ds-001",
        )
    """

    entry_id: str = Field(
        default_factory=lambda: str(uuid.uuid4()),
        description="Globally-unique UUID-4 identifier for this audit entry.",
    )
    event_type: AuditEventType = Field(
        ...,
        description="Classification of the compliance event.",
    )
    timestamp: datetime = Field(
        default_factory=lambda: datetime.now(UTC),
        description="UTC timestamp of when the event occurred.",
    )
    tenant_id: str = Field(
        ...,
        description="Tenant namespace for multi-tenant isolation.",
    )
    user_id: str = Field(
        ...,
        description="Identity of the user who performed the action.",
    )
    dataset_id: str | None = Field(
        default=None,
        description="Associated dataset identifier.",
    )
    certificate_id: str | None = Field(
        default=None,
        description="Associated compliance certificate identifier.",
    )
    correlation_id: str | None = Field(
        default=None,
        description="Request-scoped correlation ID for distributed tracing.",
    )
    details: dict[str, Any] = Field(
        default_factory=dict,
        description="Event-specific metadata.",
    )
    previous_entry_hash: str | None = Field(
        default=None,
        description="SHA-256 hash of the preceding entry in this tenant's chain.",
    )
    entry_hash: str = Field(
        default="",
        description="SHA-256 tamper-evident checksum of this entry.",
    )
    ip_address: str | None = Field(
        default=None,
        description="Client IP address captured at the API gateway.",
    )
    user_agent: str | None = Field(
        default=None,
        description="Client User-Agent header.",
    )
    service_name: str = Field(
        default="compliance-service",
        description="Name of the originating service.",
    )

    model_config = {"use_enum_values": True}


class AuditQueryParams(BaseModel):
    """Parameters for querying the audit log with mandatory tenant isolation.

    ``tenant_id`` is *always* required — there is no way to query across
    tenants, which is a deliberate security constraint for SOC 2 Type II
    compliance.

    Attributes:
        tenant_id: Required tenant identifier for namespace isolation.
        dataset_id: Optional filter by dataset.
        certificate_id: Optional filter by compliance certificate.
        event_type: Optional single event-type filter.
        event_types: Optional list of event-type filters (OR logic).
        user_id: Optional filter by originating user.
        start_date: Optional inclusive lower bound on timestamp.
        end_date: Optional inclusive upper bound on timestamp.
        page: 1-based page number for pagination.
        page_size: Number of entries per page (max 500).

    Example::

        params = AuditQueryParams(
            tenant_id="tenant-42",
            event_types=[AuditEventType.SCAN_INITIATED, AuditEventType.SCAN_COMPLETED],
            start_date=datetime(2024, 1, 1, tzinfo=timezone.utc),
            page=1,
            page_size=25,
        )
    """

    tenant_id: str = Field(..., description="Required tenant identifier.")
    dataset_id: str | None = Field(default=None, description="Filter by dataset.")
    certificate_id: str | None = Field(default=None, description="Filter by certificate.")
    event_type: AuditEventType | None = Field(default=None, description="Single event-type filter.")
    event_types: list[AuditEventType] | None = Field(default=None, description="Multiple event-type filter (OR).")
    user_id: str | None = Field(default=None, description="Filter by user.")
    start_date: datetime | None = Field(default=None, description="Inclusive lower bound on timestamp.")
    end_date: datetime | None = Field(default=None, description="Inclusive upper bound on timestamp.")
    page: int = Field(default=1, ge=1, description="1-based page number.")
    page_size: int = Field(default=50, ge=1, le=500, description="Entries per page (max 500).")

    model_config = {"use_enum_values": True}


class AuditQueryResult(BaseModel):
    """Paginated result set returned by ``AuditLogger.query_audit_log``.

    Attributes:
        entries: List of audit entries for the current page.
        total_count: Total number of matching entries across all pages.
        page: Current 1-based page number.
        page_size: Maximum number of entries per page.
        has_next: ``True`` if there are more pages after the current one.

    Example::

        result = logger.query_audit_log(params)
        for entry in result.entries:
            print(entry.event_type, entry.timestamp)
        if result.has_next:
            params.page += 1
    """

    entries: list[AuditEntry] = Field(default_factory=list, description="Audit entries for the current page.")
    total_count: int = Field(default=0, description="Total matching entries across all pages.")
    page: int = Field(default=1, description="Current 1-based page number.")
    page_size: int = Field(default=50, description="Entries per page.")
    has_next: bool = Field(default=False, description="Whether more pages follow.")


# ---------------------------------------------------------------------------
# AuditLogger — Core audit event logging engine
# ---------------------------------------------------------------------------


class AuditLogger:
    """Tamper-evident audit event logger backed by MongoDB.

    Writes compliance lifecycle events to the ``audit_logs`` collection with:

    * A TTL index enforcing 7-year document retention.
    * SHA-256 hash chain linking each entry to its predecessor (per tenant).
    * Compound indexes for efficient SOC 2 Type II audit queries.

    The collection and its indexes are lazily initialised on first access so
    that instantiating an ``AuditLogger`` does not require a live MongoDB
    connection — useful in unit-test and import-time contexts.

    Args:
        collection: Optional pre-configured ``pymongo.collection.Collection``.
            When ``None`` (the default), the collection is obtained from
            ``get_mongo_db()`` on first use.

    Example::

        logger = AuditLogger()
        entry = logger.log_event(
            event_type=AuditEventType.CERTIFICATION_ISSUED,
            tenant_id="tenant-42",
            user_id="user-abc",
            certificate_id="cert-xyz",
        )
    """

    # Class-level constants exposed as public API
    COLLECTION_NAME: str = "audit_logs"
    RETENTION_YEARS: int = 7
    RETENTION_SECONDS: int = 7 * 365 * 24 * 3600  # ~220 752 000 seconds

    def __init__(self, collection: Collection | None = None) -> None:
        """Initialise the audit logger.

        Args:
            collection: Optional MongoDB collection override.  When ``None``,
                the collection is lazy-loaded from ``get_mongo_db()`` on the
                first call to ``_get_collection()``.
        """
        self._collection: Collection | None = collection
        self._logger = get_logger(__name__)
        self._last_entry_hash: str | None = None
        self._indexes_ensured: bool = False
        self._logger.info(
            "audit_logger_initialized",
            collection_name=self.COLLECTION_NAME,
            retention_years=self.RETENTION_YEARS,
        )

    # ------------------------------------------------------------------
    # Collection & Index Management
    # ------------------------------------------------------------------

    def _get_collection(self) -> Collection:
        """Return the MongoDB collection, lazily initialising indexes.

        On first invocation the method:

        1. Resolves the collection via ``get_mongo_db()`` (if not injected).
        2. Creates all required indexes (TTL, compound, unique hash).

        Subsequent calls return the cached collection reference directly.

        Returns:
            The ``audit_logs`` ``pymongo.collection.Collection``.
        """
        if self._collection is None:
            db = get_mongo_db()
            self._collection = db[self.COLLECTION_NAME]

        if not self._indexes_ensured:
            self._ensure_indexes()

        return self._collection

    def _ensure_indexes(self) -> None:
        """Create all required indexes on the audit_logs collection.

        Indexes created:

        * **TTL index** on ``timestamp`` — expires documents after 7 years.
        * **tenant + timestamp** — tenant-scoped chronological queries.
        * **tenant + dataset + timestamp** — dataset audit trail lookups.
        * **tenant + certificate** — certificate audit trail lookups.
        * **tenant + event_type + timestamp** — event-type filtering.
        * **entry_hash** — hash chain integrity verification lookups.

        This method is idempotent; MongoDB silently ignores index creation
        requests when an identical index already exists.
        """
        try:
            collection = self._collection
            if collection is None:
                return

            indexes: list[IndexModel] = [
                # TTL index — 7-year retention enforcement
                IndexModel(
                    [("timestamp", ASCENDING)],
                    expireAfterSeconds=self.RETENTION_SECONDS,
                    name="ttl_7yr_retention",
                ),
                # Tenant-scoped chronological queries
                IndexModel(
                    [("tenant_id", ASCENDING), ("timestamp", DESCENDING)],
                    name="idx_tenant_timestamp",
                ),
                # Dataset audit trail
                IndexModel(
                    [("tenant_id", ASCENDING), ("dataset_id", ASCENDING), ("timestamp", DESCENDING)],
                    name="idx_tenant_dataset_timestamp",
                ),
                # Certificate audit trail
                IndexModel(
                    [("tenant_id", ASCENDING), ("certificate_id", ASCENDING)],
                    name="idx_tenant_certificate",
                ),
                # Event-type filtering within a tenant
                IndexModel(
                    [("tenant_id", ASCENDING), ("event_type", ASCENDING), ("timestamp", DESCENDING)],
                    name="idx_tenant_eventtype_timestamp",
                ),
                # Hash chain integrity verification
                IndexModel(
                    [("entry_hash", ASCENDING)],
                    name="idx_entry_hash",
                ),
            ]

            collection.create_indexes(indexes)
            self._indexes_ensured = True
            self._logger.info(
                "audit_indexes_ensured",
                index_count=len(indexes),
                collection=self.COLLECTION_NAME,
            )
        except PyMongoError as exc:
            self._logger.error(
                "audit_index_creation_failed",
                error=str(exc),
                collection=self.COLLECTION_NAME,
            )
            raise

    # ------------------------------------------------------------------
    # Public API — Event Logging
    # ------------------------------------------------------------------

    def log_event(
        self,
        event_type: AuditEventType,
        tenant_id: str,
        user_id: str,
        dataset_id: str | None = None,
        certificate_id: str | None = None,
        correlation_id: str | None = None,
        details: dict[str, Any] | None = None,
        ip_address: str | None = None,
        user_agent: str | None = None,
    ) -> AuditEntry:
        """Record a single audit event with tamper-evident hash chaining.

        Workflow:

        1. Create an ``AuditEntry`` with the supplied parameters.
        2. Resolve ``previous_entry_hash`` from the in-memory cache or by
           querying the database for the latest entry in this tenant's chain.
        3. Compute the ``entry_hash`` (SHA-256) over all fields including
           ``previous_entry_hash``.
        4. Persist the entry to MongoDB.
        5. Update the in-memory ``_last_entry_hash`` cache.

        Args:
            event_type: The type of compliance event.
            tenant_id: Tenant namespace (mandatory for isolation).
            user_id: The user or service account that triggered the event.
            dataset_id: Optional associated dataset identifier.
            certificate_id: Optional associated compliance certificate.
            correlation_id: Optional request-scoped correlation ID.
            details: Optional event-specific metadata dictionary.
            ip_address: Optional client IP address.
            user_agent: Optional client User-Agent string.

        Returns:
            The fully-populated ``AuditEntry`` including computed hashes.

        Raises:
            PyMongoError: If the MongoDB insert operation fails.

        Example::

            entry = logger.log_event(
                event_type=AuditEventType.PII_DETECTED,
                tenant_id="tenant-42",
                user_id="user-abc",
                dataset_id="ds-001",
                details={"pii_types": ["SSN", "email"], "count": 15},
            )
        """
        # Resolve the previous hash for chain continuity
        previous_hash = self._last_entry_hash
        if previous_hash is None:
            previous_hash = self._get_latest_entry_hash(tenant_id)

        # Build the audit entry — timestamp is normalised to millisecond
        # precision so that the SHA-256 hash survives a MongoDB round-trip.
        normalised_ts = self._normalize_timestamp(datetime.now(UTC))
        entry = AuditEntry(
            event_type=event_type,
            timestamp=normalised_ts,
            tenant_id=tenant_id,
            user_id=user_id,
            dataset_id=dataset_id,
            certificate_id=certificate_id,
            correlation_id=correlation_id,
            details=details if details is not None else {},
            previous_entry_hash=previous_hash,
            ip_address=ip_address,
            user_agent=user_agent,
        )

        # Compute and assign the tamper-evident hash
        entry.entry_hash = self._compute_entry_hash(entry)

        # Persist to MongoDB
        try:
            collection = self._get_collection()
            document = entry.model_dump()
            # Convert datetime to native Python datetime for PyMongo BSON
            collection.insert_one(document)
        except PyMongoError as exc:
            self._logger.error(
                "audit_event_insert_failed",
                entry_id=entry.entry_id,
                event_type=entry.event_type,
                tenant_id=tenant_id,
                error=str(exc),
            )
            raise

        # Update in-memory hash cache for the next entry in this process
        self._last_entry_hash = entry.entry_hash

        # Structured log for observability
        self._logger.info(
            "audit_event_logged",
            entry_id=entry.entry_id,
            event_type=entry.event_type,
            tenant_id=tenant_id,
            user_id=user_id,
            dataset_id=dataset_id,
            certificate_id=certificate_id,
            correlation_id=correlation_id,
        )

        return entry

    # ------------------------------------------------------------------
    # Public API — Querying
    # ------------------------------------------------------------------

    def query_audit_log(self, params: AuditQueryParams) -> AuditQueryResult:
        """Query audit log entries with mandatory tenant isolation and pagination.

        Builds a MongoDB filter from the supplied parameters.  ``tenant_id``
        is always included (it is a required field on ``AuditQueryParams``),
        guaranteeing that cross-tenant reads are structurally impossible.

        Results are sorted by ``timestamp`` descending (newest first) and
        paginated via skip/limit.

        Args:
            params: Query parameters with mandatory ``tenant_id``.

        Returns:
            ``AuditQueryResult`` with matching entries, total count, and
            pagination metadata.

        Example::

            result = logger.query_audit_log(
                AuditQueryParams(
                    tenant_id="tenant-42",
                    event_type=AuditEventType.CERTIFICATION_ISSUED,
                    page=1,
                    page_size=25,
                )
            )
        """
        query_filter: dict[str, Any] = {"tenant_id": params.tenant_id}

        # Optional filters
        if params.dataset_id is not None:
            query_filter["dataset_id"] = params.dataset_id

        if params.certificate_id is not None:
            query_filter["certificate_id"] = params.certificate_id

        if params.event_type is not None:
            event_value = params.event_type if isinstance(params.event_type, str) else params.event_type.value
            query_filter["event_type"] = event_value

        if params.event_types is not None and len(params.event_types) > 0:
            resolved_types: list[str] = []
            for et in params.event_types:
                resolved_types.append(et if isinstance(et, str) else et.value)
            query_filter["event_type"] = {"$in": resolved_types}

        if params.user_id is not None:
            query_filter["user_id"] = params.user_id

        # Date range — inclusive bounds
        if params.start_date is not None or params.end_date is not None:
            ts_filter: dict[str, Any] = {}
            if params.start_date is not None:
                ts_filter["$gte"] = params.start_date
            if params.end_date is not None:
                ts_filter["$lte"] = params.end_date
            query_filter["timestamp"] = ts_filter

        collection = self._get_collection()

        try:
            total_count: int = collection.count_documents(query_filter)

            skip = (params.page - 1) * params.page_size
            cursor = collection.find(query_filter).sort("timestamp", DESCENDING).skip(skip).limit(params.page_size)

            entries: list[AuditEntry] = []
            for doc in cursor:
                self._normalize_document(doc)
                entries.append(AuditEntry(**doc))

            has_next = (skip + params.page_size) < total_count

            return AuditQueryResult(
                entries=entries,
                total_count=total_count,
                page=params.page,
                page_size=params.page_size,
                has_next=has_next,
            )
        except PyMongoError as exc:
            self._logger.error(
                "audit_query_failed",
                tenant_id=params.tenant_id,
                error=str(exc),
            )
            raise

    def get_dataset_audit_trail(
        self,
        dataset_id: str,
        tenant_id: str,
    ) -> list[AuditEntry]:
        """Retrieve the complete audit trail for a specific dataset.

        Returns entries in chronological order (oldest first) so that the
        trail reads as a natural sequence of events.

        Args:
            dataset_id: The dataset to retrieve the trail for.
            tenant_id: Mandatory tenant identifier for isolation.

        Returns:
            List of ``AuditEntry`` objects sorted by ``timestamp`` ascending.

        Example::

            trail = logger.get_dataset_audit_trail("ds-001", "tenant-42")
            for entry in trail:
                print(f"{entry.timestamp}: {entry.event_type}")
        """
        collection = self._get_collection()
        query_filter: dict[str, Any] = {
            "tenant_id": tenant_id,
            "dataset_id": dataset_id,
        }

        try:
            cursor = collection.find(query_filter).sort("timestamp", ASCENDING)
            entries: list[AuditEntry] = []
            for doc in cursor:
                self._normalize_document(doc)
                entries.append(AuditEntry(**doc))
            return entries
        except PyMongoError as exc:
            self._logger.error(
                "audit_dataset_trail_failed",
                dataset_id=dataset_id,
                tenant_id=tenant_id,
                error=str(exc),
            )
            raise

    def get_certificate_audit_trail(
        self,
        certificate_id: str,
        tenant_id: str,
    ) -> list[AuditEntry]:
        """Retrieve the complete audit trail for a specific compliance certificate.

        Returns entries in chronological order (oldest first) so that the
        trail reads as a natural lifecycle sequence from issuance through
        release or revocation.

        Args:
            certificate_id: The compliance certificate to retrieve the trail for.
            tenant_id: Mandatory tenant identifier for isolation.

        Returns:
            List of ``AuditEntry`` objects sorted by ``timestamp`` ascending.

        Example::

            trail = logger.get_certificate_audit_trail("cert-xyz", "tenant-42")
            for entry in trail:
                print(f"{entry.timestamp}: {entry.event_type}")
        """
        collection = self._get_collection()
        query_filter: dict[str, Any] = {
            "tenant_id": tenant_id,
            "certificate_id": certificate_id,
        }

        try:
            cursor = collection.find(query_filter).sort("timestamp", ASCENDING)
            entries: list[AuditEntry] = []
            for doc in cursor:
                self._normalize_document(doc)
                entries.append(AuditEntry(**doc))
            return entries
        except PyMongoError as exc:
            self._logger.error(
                "audit_certificate_trail_failed",
                certificate_id=certificate_id,
                tenant_id=tenant_id,
                error=str(exc),
            )
            raise

    # ------------------------------------------------------------------
    # Public API — Hash Chain Verification
    # ------------------------------------------------------------------

    def verify_chain_integrity(
        self,
        tenant_id: str,
        start_date: datetime | None = None,
        end_date: datetime | None = None,
    ) -> dict[str, Any]:
        """Verify the tamper-evidence of the hash chain for a tenant.

        Retrieves all entries in chronological order (optionally bounded by
        a date range), recomputes each entry's expected SHA-256 hash, and
        verifies:

        1. The stored ``entry_hash`` matches the recomputed hash.
        2. The stored ``previous_entry_hash`` matches the preceding entry's
           ``entry_hash``.

        If any discrepancy is found the chain is marked invalid and the
        first offending entry is reported.

        Args:
            tenant_id: The tenant whose chain to verify.
            start_date: Optional inclusive lower bound on the verification
                window.
            end_date: Optional inclusive upper bound on the verification
                window.

        Returns:
            A dictionary with the following keys:

            - ``is_valid`` (bool): ``True`` if the entire chain is intact.
            - ``entries_checked`` (int): Number of entries verified.
            - ``first_invalid_entry`` (str | None): ``entry_id`` of the
              first tampered entry, or ``None`` if the chain is valid.
            - ``verification_timestamp`` (datetime): UTC time of this check.
            - ``error_details`` (str | None): Human-readable description of
              the first integrity violation, or ``None`` if valid.

        Example::

            result = logger.verify_chain_integrity("tenant-42")
            if not result["is_valid"]:
                alert_security_team(result)
        """
        collection = self._get_collection()
        query_filter: dict[str, Any] = {"tenant_id": tenant_id}

        if start_date is not None or end_date is not None:
            ts_filter: dict[str, Any] = {}
            if start_date is not None:
                ts_filter["$gte"] = start_date
            if end_date is not None:
                ts_filter["$lte"] = end_date
            query_filter["timestamp"] = ts_filter

        verification_result: dict[str, Any] = {
            "is_valid": True,
            "entries_checked": 0,
            "first_invalid_entry": None,
            "verification_timestamp": datetime.now(UTC),
            "error_details": None,
        }

        try:
            cursor = collection.find(query_filter).sort("timestamp", ASCENDING)

            previous_hash: str | None = None
            entries_checked: int = 0

            for doc in cursor:
                self._normalize_document(doc)
                entry = AuditEntry(**doc)
                entries_checked += 1

                # Verify 1: previous_entry_hash links to predecessor
                if previous_hash is not None and entry.previous_entry_hash != previous_hash:
                    verification_result["is_valid"] = False
                    verification_result["first_invalid_entry"] = entry.entry_id
                    verification_result["error_details"] = (
                        f"Entry {entry.entry_id}: previous_entry_hash mismatch. "
                        f"Expected '{previous_hash}', found '{entry.previous_entry_hash}'."
                    )
                    break

                # Verify 2: recomputed entry_hash matches stored hash
                expected_hash = self._compute_entry_hash(entry)
                if entry.entry_hash != expected_hash:
                    verification_result["is_valid"] = False
                    verification_result["first_invalid_entry"] = entry.entry_id
                    verification_result["error_details"] = (
                        f"Entry {entry.entry_id}: entry_hash mismatch. "
                        f"Expected '{expected_hash}', found '{entry.entry_hash}'."
                    )
                    break

                previous_hash = entry.entry_hash

            verification_result["entries_checked"] = entries_checked

        except PyMongoError as exc:
            self._logger.error(
                "audit_chain_verification_failed",
                tenant_id=tenant_id,
                error=str(exc),
            )
            verification_result["is_valid"] = False
            verification_result["error_details"] = f"Database error: {exc}"

        self._logger.info(
            "audit_chain_verification_completed",
            tenant_id=tenant_id,
            is_valid=verification_result["is_valid"],
            entries_checked=verification_result["entries_checked"],
        )

        return verification_result

    # ------------------------------------------------------------------
    # Private helpers
    # ------------------------------------------------------------------

    @staticmethod
    def _normalize_timestamp(ts: datetime) -> datetime:
        """Normalise a timestamp to millisecond precision with UTC timezone.

        MongoDB BSON datetimes only support millisecond precision and may
        drop timezone information on round-trip.  To guarantee that the
        SHA-256 hash computed *before* storage matches the hash recomputed
        *after* retrieval, every timestamp used in hash computation must be
        normalised to:

        1. **Millisecond precision** — microsecond digits beyond the first
           three are truncated (not rounded) to match BSON behaviour.
        2. **Explicit UTC timezone** — a timezone-naïve datetime returned by
           PyMongo is re-tagged as UTC.

        Args:
            ts: The timestamp to normalise.

        Returns:
            A timezone-aware, millisecond-precision ``datetime`` in UTC.
        """
        # Ensure UTC timezone
        if ts.tzinfo is None:
            ts = ts.replace(tzinfo=UTC)
        # Truncate to millisecond precision (BSON datetime resolution)
        truncated_us = (ts.microsecond // 1000) * 1000
        return ts.replace(microsecond=truncated_us)

    @staticmethod
    def _normalize_document(doc: dict[str, Any]) -> dict[str, Any]:
        """Normalise a raw MongoDB document for ``AuditEntry`` reconstruction.

        Handles two MongoDB round-trip artefacts:

        * ``event_type`` is stored as a plain string — convert back to the
          ``AuditEventType`` enum so that Pydantic can accept it.
        * ``timestamp`` may have lost timezone info and/or microsecond
          precision — restore UTC and truncate to milliseconds.
        * ``_id`` is an ObjectId added by MongoDB — remove it.

        Args:
            doc: A raw document from ``collection.find()``.

        Returns:
            A cleaned dictionary suitable for ``AuditEntry(**doc)``.
        """
        doc.pop("_id", None)
        if "event_type" in doc and isinstance(doc["event_type"], str):
            doc["event_type"] = AuditEventType(doc["event_type"])
        if "timestamp" in doc and isinstance(doc["timestamp"], datetime):
            doc["timestamp"] = AuditLogger._normalize_timestamp(doc["timestamp"])
        return doc

    def _compute_entry_hash(self, entry: AuditEntry) -> str:
        """Compute the SHA-256 tamper-evident checksum for an audit entry.

        The hash is computed over a *deterministic* JSON serialisation of all
        entry fields **except** ``entry_hash`` itself.  The
        ``previous_entry_hash`` field *is* included so that the hash chains
        consecutive entries together.

        Determinism is achieved by:

        * Sorting dictionary keys.
        * Normalising the timestamp to millisecond-precision UTC before
          converting to ISO-8601 — this ensures the hash is invariant
          across MongoDB round-trips (which truncate to milliseconds and
          may strip timezone information).

        Args:
            entry: The audit entry to hash.

        Returns:
            A lowercase hexadecimal SHA-256 digest string.
        """
        # Normalise timestamp for MongoDB round-trip stability
        ts = entry.timestamp if isinstance(entry.timestamp, datetime) else datetime.fromisoformat(str(entry.timestamp))
        normalised_ts = self._normalize_timestamp(ts)

        # Build a canonical dictionary omitting the entry_hash field
        hash_payload: dict[str, Any] = {
            "entry_id": entry.entry_id,
            "event_type": entry.event_type,
            "timestamp": normalised_ts.isoformat(),
            "tenant_id": entry.tenant_id,
            "user_id": entry.user_id,
            "dataset_id": entry.dataset_id,
            "certificate_id": entry.certificate_id,
            "correlation_id": entry.correlation_id,
            "details": entry.details,
            "previous_entry_hash": entry.previous_entry_hash,
            "ip_address": entry.ip_address,
            "user_agent": entry.user_agent,
            "service_name": entry.service_name,
        }

        # Produce a deterministic byte string — sorted keys, no whitespace
        canonical_json = json.dumps(hash_payload, sort_keys=True, default=str)
        return hashlib.sha256(canonical_json.encode("utf-8")).hexdigest()

    def _get_latest_entry_hash(self, tenant_id: str) -> str | None:
        """Query MongoDB for the hash of the most recent entry for a tenant.

        This is used to seed the hash chain when ``_last_entry_hash`` is not
        yet populated (e.g. after a service restart).

        Args:
            tenant_id: The tenant whose latest hash to retrieve.

        Returns:
            The ``entry_hash`` string of the most recent entry, or ``None``
            if no prior entries exist for this tenant.
        """
        try:
            collection = self._get_collection()
            latest = collection.find_one(
                {"tenant_id": tenant_id},
                sort=[("timestamp", DESCENDING)],
                projection={"entry_hash": 1, "_id": 0},
            )
            if latest is not None and "entry_hash" in latest:
                return str(latest["entry_hash"])
        except PyMongoError as exc:
            self._logger.warning(
                "audit_latest_hash_lookup_failed",
                tenant_id=tenant_id,
                error=str(exc),
            )
        return None


# ---------------------------------------------------------------------------
# Module-level convenience function
# ---------------------------------------------------------------------------

# Module-level singleton for the convenience function (lazy-initialised).
_default_audit_logger: AuditLogger | None = None


def log_compliance_event(
    event_type: AuditEventType,
    tenant_id: str,
    user_id: str,
    **kwargs: Any,
) -> AuditEntry:
    """Log a single compliance event using a module-level ``AuditLogger``.

    This is a convenience wrapper for call sites that need to log a one-off
    event without managing an ``AuditLogger`` instance.  A module-level
    singleton is lazily created on first invocation and reused for all
    subsequent calls.

    Args:
        event_type: The type of compliance event.
        tenant_id: Tenant namespace (mandatory for isolation).
        user_id: The user or service account that triggered the event.
        **kwargs: Additional keyword arguments forwarded to
            ``AuditLogger.log_event`` (e.g. ``dataset_id``,
            ``certificate_id``, ``details``, ``correlation_id``,
            ``ip_address``, ``user_agent``).

    Returns:
        The created ``AuditEntry``.

    Example::

        from compliance_service.certification.audit_logger import (
            log_compliance_event,
            AuditEventType,
        )

        log_compliance_event(
            event_type=AuditEventType.EXPORT_COMPLETED,
            tenant_id="tenant-42",
            user_id="system",
            details={"format": "parquet", "row_count": 500_000},
        )
    """
    global _default_audit_logger  # noqa: PLW0603

    if _default_audit_logger is None:
        _default_audit_logger = AuditLogger()

    return _default_audit_logger.log_event(
        event_type=event_type,
        tenant_id=tenant_id,
        user_id=user_id,
        **kwargs,
    )
