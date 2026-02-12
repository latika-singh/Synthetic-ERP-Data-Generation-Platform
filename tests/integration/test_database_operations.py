"""
Integration tests for MongoDB CRUD operations and Redis cache operations.

This module verifies the data layer against real MongoDB 7.0 and Redis 7.x
instances launched via Docker Compose test profile.  It covers:

- All five core MongoDB collections: generation_profiles, statistical_profiles,
  schema_definitions, audit_logs, tenant_configurations
- Document CRUD (insert, read, update, delete)
- Index verification (single-field, compound, unique, TTL)
- Cross-collection referential integrity
- Tenant-scoped query isolation
- Redis key/value operations, expiration, pipelines, and pub/sub
- BSON serialization round-trips (ObjectId, datetime, UUID, nested docs)

All fixtures are supplied by ``conftest.py`` in this package and are
auto-discovered by pytest.  The ``clean_collections`` and ``clean_redis``
autouse fixtures guarantee test isolation by clearing data before each test.
"""

import hashlib
import json
import time
import uuid
from datetime import UTC, datetime, timedelta

import pytest
import redis as redis_lib
from bson import ObjectId
from pymongo.errors import DuplicateKeyError


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _utcnow() -> datetime:
    """Return current UTC datetime with timezone info."""
    return datetime.now(tz=UTC)


def _make_generation_profile(tenant_id: str, **overrides) -> dict:
    """Build a minimal generation_profiles document.

    The document mirrors the structure stored by the API Gateway's
    ``generation_job`` model.  ``overrides`` can replace any field.
    """
    now = _utcnow()
    doc = {
        "job_id": str(uuid.uuid4()),
        "tenant_id": tenant_id,
        "name": "Test Generation Job",
        "schema_id": str(uuid.uuid4()),
        "status": "submitted",
        "generation_method": "statistical",
        "record_count": 10000,
        "output_format": "csv",
        "created_at": now,
        "updated_at": now,
        "started_at": None,
        "completed_at": None,
        "parameters": {
            "batch_size": 10000,
            "preserve_distributions": True,
            "maintain_referential_integrity": True,
            "seed": 42,
        },
        "quality_score": None,
        "quality_report": None,
        "compliance": {"status": "pending", "pii_detected": None},
        "output": None,
        "created_by": "test@integration.test",
    }
    doc.update(overrides)
    return doc


def _make_statistical_profile(tenant_id: str, schema_id: str, **overrides) -> dict:
    """Build a minimal statistical_profiles document."""
    now = _utcnow()
    doc = {
        "profile_id": str(uuid.uuid4()),
        "tenant_id": tenant_id,
        "schema_id": schema_id,
        "name": "Test Statistical Profile",
        "created_at": now,
        "updated_at": now,
        "status": "completed",
        "table_profiles": [
            {
                "table_name": "TEST_TABLE",
                "row_count": 1000,
                "column_profiles": [
                    {
                        "column_name": "id",
                        "data_type": "VARCHAR(36)",
                        "null_percentage": 0.0,
                        "unique_count": 1000,
                        "distribution_type": "uuid",
                    },
                ],
            },
        ],
    }
    doc.update(overrides)
    return doc


def _make_schema_definition(tenant_id: str, **overrides) -> dict:
    """Build a minimal schema_definitions document."""
    now = _utcnow()
    doc = {
        "schema_id": str(uuid.uuid4()),
        "tenant_id": tenant_id,
        "name": "Test ERP Schema",
        "erp_system": "SAP",
        "erp_module": "financial_accounting",
        "version": "1.0.0",
        "discovered_at": now,
        "created_at": now,
        "updated_at": now,
        "tables": [
            {
                "name": "GL_ACCOUNTS",
                "columns": [
                    {
                        "name": "account_id",
                        "data_type": "VARCHAR(20)",
                        "nullable": False,
                        "primary_key": True,
                    },
                    {
                        "name": "account_name",
                        "data_type": "VARCHAR(100)",
                        "nullable": False,
                        "primary_key": False,
                    },
                ],
            },
        ],
        "relationships": [],
        "metadata": {"total_tables": 1, "total_columns": 2, "total_relationships": 0},
    }
    doc.update(overrides)
    return doc


def _make_audit_log(tenant_id: str, *, previous_hash: str = "0" * 64, **overrides) -> dict:
    """Build a single audit_logs document with tamper-evident hash."""
    now = _utcnow()
    event_id = str(uuid.uuid4())
    action = overrides.pop("action", "generation.job.created")
    details = overrides.pop("details", {"job_id": str(uuid.uuid4())})

    hash_payload = json.dumps(
        {
            "event_id": event_id,
            "action": action,
            "details": details,
            "previous_hash": previous_hash,
        },
        sort_keys=True,
        default=str,
    )
    current_hash = hashlib.sha256(hash_payload.encode("utf-8")).hexdigest()

    doc = {
        "event_id": event_id,
        "tenant_id": tenant_id,
        "user_id": str(uuid.uuid4()),
        "action": action,
        "description": f"Test audit event: {action}",
        "details": details,
        "timestamp": now,
        "created_at": now,
        "correlation_id": str(uuid.uuid4()),
        "ip_address": "10.0.0.1",
        "user_agent": "integration-test-runner/1.0",
        "previous_hash": previous_hash,
        "hash": current_hash,
    }
    doc.update(overrides)
    return doc


def _make_tenant_configuration(tenant_id: str, **overrides) -> dict:
    """Build a minimal tenant_configurations document."""
    now = _utcnow()
    doc = {
        "tenant_id": tenant_id,
        "name": "Test Tenant",
        "created_at": now,
        "updated_at": now,
        "status": "active",
        "resource_quotas": {
            "max_concurrent_jobs": 5,
            "max_records_per_job": 1_000_000,
            "storage_limit_gb": 100,
            "api_rate_limit_per_minute": 300,
        },
        "allowed_erp_modules": [
            "financial_accounting",
            "human_resources",
            "sales_distribution",
            "material_management",
        ],
        "settings": {
            "default_output_format": "csv",
            "default_generation_method": "statistical",
            "encryption_enabled": True,
            "audit_logging_enabled": True,
            "notification_email": "admin@test.local",
            "webhook_url": None,
        },
        "subscription": {
            "plan": "enterprise",
            "expires_at": now + timedelta(days=365),
        },
    }
    doc.update(overrides)
    return doc


@pytest.fixture
def unique_job_id() -> str:
    """Generate a unique job_id for tests that need a guaranteed-unique identifier."""
    return f"job-{uuid.uuid4()}"


# ============================================================================
# 1. TestGenerationProfilesCollection  (12 tests)
# ============================================================================

@pytest.mark.integration
class TestGenerationProfilesCollection:
    """CRUD and index tests for the ``generation_profiles`` collection.

    This collection stores generation job records including job lifecycle
    state, quality scores, compliance status, and output metadata.
    """

    # ---- CRUD ----

    def test_insert_generation_profile(
        self, generation_profiles_collection, sample_tenant_id
    ):
        """Inserting a valid document succeeds and assigns an ``_id``."""
        doc = _make_generation_profile(sample_tenant_id)
        result = generation_profiles_collection.insert_one(doc)

        assert result.inserted_id is not None
        assert result.acknowledged is True
        stored = generation_profiles_collection.find_one({"_id": result.inserted_id})
        assert stored is not None
        assert stored["job_id"] == doc["job_id"]
        assert stored["tenant_id"] == sample_tenant_id
        assert stored["status"] == "submitted"
        assert stored["generation_method"] == "statistical"
        assert stored["record_count"] == 10000
        assert stored["output_format"] == "csv"
        assert stored["parameters"]["batch_size"] == 10000

    def test_read_generation_profile_by_id(
        self, generation_profiles_collection, sample_tenant_id, unique_job_id
    ):
        """Reading a document by its ``job_id`` returns the correct record."""
        doc = _make_generation_profile(sample_tenant_id, job_id=unique_job_id)
        generation_profiles_collection.insert_one(doc)

        stored = generation_profiles_collection.find_one(
            {"job_id": unique_job_id, "tenant_id": sample_tenant_id}
        )
        assert stored is not None
        assert stored["job_id"] == unique_job_id
        assert stored["name"] == doc["name"]

    def test_update_generation_profile_status(
        self, generation_profiles_collection, sample_tenant_id
    ):
        """Updating ``status`` and ``updated_at`` persists correctly."""
        doc = _make_generation_profile(sample_tenant_id)
        generation_profiles_collection.insert_one(doc)

        new_time = _utcnow()
        result = generation_profiles_collection.update_one(
            {"job_id": doc["job_id"]},
            {"$set": {"status": "generating", "updated_at": new_time, "started_at": new_time}},
        )
        assert result.modified_count == 1

        updated = generation_profiles_collection.find_one({"job_id": doc["job_id"]})
        assert updated["status"] == "generating"
        assert updated["started_at"] is not None

    def test_delete_generation_profile(
        self, generation_profiles_collection, sample_tenant_id
    ):
        """Deleting a document by ``job_id`` removes it from the collection."""
        doc = _make_generation_profile(sample_tenant_id)
        generation_profiles_collection.insert_one(doc)

        result = generation_profiles_collection.delete_one({"job_id": doc["job_id"]})
        assert result.deleted_count == 1

        assert generation_profiles_collection.find_one({"job_id": doc["job_id"]}) is None

    def test_list_generation_profiles_by_tenant(
        self, generation_profiles_collection, sample_tenant_id, secondary_tenant_id
    ):
        """Querying by ``tenant_id`` returns only that tenant's documents."""
        docs_primary = [
            _make_generation_profile(sample_tenant_id) for _ in range(3)
        ]
        docs_secondary = [
            _make_generation_profile(secondary_tenant_id) for _ in range(2)
        ]
        generation_profiles_collection.insert_many(docs_primary + docs_secondary)

        primary_results = list(
            generation_profiles_collection.find({"tenant_id": sample_tenant_id})
        )
        secondary_results = list(
            generation_profiles_collection.find({"tenant_id": secondary_tenant_id})
        )
        assert len(primary_results) == 3
        assert len(secondary_results) == 2
        # Ensure no cross-contamination
        for r in primary_results:
            assert r["tenant_id"] == sample_tenant_id
        for r in secondary_results:
            assert r["tenant_id"] == secondary_tenant_id

    # ---- Index verification ----

    def test_generation_profiles_index_tenant_id(
        self, generation_profiles_collection
    ):
        """The ``idx_tenant_id`` index exists on ``tenant_id``."""
        indexes = generation_profiles_collection.index_information()
        assert "idx_tenant_id" in indexes
        keys = indexes["idx_tenant_id"]["key"]
        assert ("tenant_id", 1) in keys

    def test_generation_profiles_index_status(
        self, generation_profiles_collection
    ):
        """The ``idx_status`` index exists on ``status``."""
        indexes = generation_profiles_collection.index_information()
        assert "idx_status" in indexes
        keys = indexes["idx_status"]["key"]
        assert ("status", 1) in keys

    def test_generation_profiles_compound_index(
        self, generation_profiles_collection
    ):
        """The ``idx_tenant_status`` compound index exists on (tenant_id, status)."""
        indexes = generation_profiles_collection.index_information()
        assert "idx_tenant_status" in indexes
        keys = indexes["idx_tenant_status"]["key"]
        assert keys == [("tenant_id", 1), ("status", 1)]

    def test_generation_profiles_document_validation(
        self, generation_profiles_collection, sample_tenant_id
    ):
        """Documents with required fields are inserted; missing required
        fields still insert (schemaless) but tested for expected structure."""
        doc = _make_generation_profile(sample_tenant_id)
        generation_profiles_collection.insert_one(doc)
        stored = generation_profiles_collection.find_one({"job_id": doc["job_id"]})

        # Verify essential fields exist
        required_fields = [
            "job_id", "tenant_id", "name", "schema_id", "status",
            "generation_method", "record_count", "output_format",
            "created_at", "updated_at", "parameters",
        ]
        for field in required_fields:
            assert field in stored, f"Missing expected field: {field}"

    def test_generation_profiles_query_by_status(
        self, generation_profiles_collection, sample_tenant_id
    ):
        """Querying by ``status`` correctly filters documents."""
        statuses = ["submitted", "generating", "completed", "failed", "completed"]
        for s in statuses:
            generation_profiles_collection.insert_one(
                _make_generation_profile(sample_tenant_id, status=s)
            )

        completed = list(
            generation_profiles_collection.find(
                {"tenant_id": sample_tenant_id, "status": "completed"}
            )
        )
        assert len(completed) == 2

        failed = list(
            generation_profiles_collection.find(
                {"tenant_id": sample_tenant_id, "status": "failed"}
            )
        )
        assert len(failed) == 1

    def test_generation_profiles_sort_by_created_at(
        self, generation_profiles_collection, sample_tenant_id
    ):
        """Documents sort correctly by ``created_at`` descending."""
        now = _utcnow()
        for i in range(5):
            generation_profiles_collection.insert_one(
                _make_generation_profile(
                    sample_tenant_id,
                    created_at=now - timedelta(hours=i),
                    name=f"Job {i}",
                )
            )

        results = list(
            generation_profiles_collection.find(
                {"tenant_id": sample_tenant_id}
            ).sort("created_at", -1)
        )
        assert len(results) == 5
        # First result should be the most recent
        timestamps = [r["created_at"] for r in results]
        assert timestamps == sorted(timestamps, reverse=True)

    def test_generation_profiles_pagination(
        self, generation_profiles_collection, sample_tenant_id
    ):
        """Skip/limit cursor-based pagination returns correct page slices."""
        for i in range(20):
            generation_profiles_collection.insert_one(
                _make_generation_profile(sample_tenant_id, name=f"Page Job {i}")
            )

        page_size = 5
        page_1 = list(
            generation_profiles_collection.find({"tenant_id": sample_tenant_id})
            .sort("created_at", 1)
            .skip(0)
            .limit(page_size)
        )
        page_2 = list(
            generation_profiles_collection.find({"tenant_id": sample_tenant_id})
            .sort("created_at", 1)
            .skip(page_size)
            .limit(page_size)
        )
        assert len(page_1) == 5
        assert len(page_2) == 5
        # Pages must not overlap
        ids_1 = {str(d["_id"]) for d in page_1}
        ids_2 = {str(d["_id"]) for d in page_2}
        assert ids_1.isdisjoint(ids_2)


# ============================================================================
# 2. TestStatisticalProfilesCollection  (8 tests)
# ============================================================================

@pytest.mark.integration
class TestStatisticalProfilesCollection:
    """CRUD and index tests for the ``statistical_profiles`` collection.

    Statistical profiles store distribution metadata captured by the
    Profiling Service.  Each profile references a ``schema_definitions``
    document via ``schema_id``.
    """

    def test_insert_statistical_profile(
        self, statistical_profiles_collection, sample_tenant_id
    ):
        """Inserting a valid statistical profile document succeeds."""
        schema_id = str(uuid.uuid4())
        doc = _make_statistical_profile(sample_tenant_id, schema_id)
        result = statistical_profiles_collection.insert_one(doc)

        assert result.inserted_id is not None
        assert result.acknowledged is True
        stored = statistical_profiles_collection.find_one({"_id": result.inserted_id})
        assert stored is not None
        assert stored["profile_id"] == doc["profile_id"]
        assert stored["tenant_id"] == sample_tenant_id
        assert stored["schema_id"] == schema_id
        assert stored["status"] == "completed"

    def test_read_statistical_profile_by_id(
        self, statistical_profiles_collection, sample_tenant_id
    ):
        """Reading a profile by ``profile_id`` returns the correct record."""
        schema_id = str(uuid.uuid4())
        doc = _make_statistical_profile(sample_tenant_id, schema_id)
        statistical_profiles_collection.insert_one(doc)

        stored = statistical_profiles_collection.find_one(
            {"profile_id": doc["profile_id"]}
        )
        assert stored is not None
        assert stored["name"] == doc["name"]
        assert stored["schema_id"] == schema_id

    def test_update_statistical_profile(
        self, statistical_profiles_collection, sample_tenant_id
    ):
        """Updating a profile's ``status`` and ``updated_at`` persists."""
        schema_id = str(uuid.uuid4())
        doc = _make_statistical_profile(
            sample_tenant_id, schema_id, status="in_progress"
        )
        statistical_profiles_collection.insert_one(doc)

        new_time = _utcnow()
        result = statistical_profiles_collection.update_one(
            {"profile_id": doc["profile_id"]},
            {"$set": {"status": "completed", "updated_at": new_time}},
        )
        assert result.modified_count == 1
        updated = statistical_profiles_collection.find_one(
            {"profile_id": doc["profile_id"]}
        )
        assert updated["status"] == "completed"

    def test_statistical_profiles_index_schema_id(
        self, statistical_profiles_collection
    ):
        """The ``idx_schema_id`` index exists on ``schema_id``."""
        indexes = statistical_profiles_collection.index_information()
        assert "idx_schema_id" in indexes
        keys = indexes["idx_schema_id"]["key"]
        assert ("schema_id", 1) in keys

    def test_statistical_profiles_index_tenant_id(
        self, statistical_profiles_collection
    ):
        """The ``idx_tenant_id`` index exists on ``tenant_id``."""
        indexes = statistical_profiles_collection.index_information()
        assert "idx_tenant_id" in indexes
        keys = indexes["idx_tenant_id"]["key"]
        assert ("tenant_id", 1) in keys

    def test_statistical_profile_references_schema(
        self,
        statistical_profiles_collection,
        schema_definitions_collection,
        sample_tenant_id,
    ):
        """A statistical profile's ``schema_id`` matches an existing schema."""
        schema_doc = _make_schema_definition(sample_tenant_id)
        schema_definitions_collection.insert_one(schema_doc)

        profile_doc = _make_statistical_profile(
            sample_tenant_id, schema_doc["schema_id"]
        )
        statistical_profiles_collection.insert_one(profile_doc)

        # Verify the referenced schema exists
        ref_schema = schema_definitions_collection.find_one(
            {"schema_id": schema_doc["schema_id"]}
        )
        assert ref_schema is not None
        assert ref_schema["schema_id"] == profile_doc["schema_id"]

    def test_statistical_profiles_nested_document(
        self, statistical_profiles_collection, sample_tenant_id
    ):
        """Nested ``table_profiles`` and ``column_profiles`` store correctly."""
        schema_id = str(uuid.uuid4())
        doc = _make_statistical_profile(sample_tenant_id, schema_id)
        statistical_profiles_collection.insert_one(doc)

        stored = statistical_profiles_collection.find_one(
            {"profile_id": doc["profile_id"]}
        )
        assert isinstance(stored["table_profiles"], list)
        assert len(stored["table_profiles"]) >= 1
        table_profile = stored["table_profiles"][0]
        assert "table_name" in table_profile
        assert "row_count" in table_profile
        assert "column_profiles" in table_profile
        col = table_profile["column_profiles"][0]
        assert "column_name" in col
        assert "data_type" in col
        assert "distribution_type" in col

    def test_statistical_profiles_distribution_types(
        self, statistical_profiles_collection, sample_tenant_id
    ):
        """Various distribution types (uniform, categorical, log_normal, etc.)
        are stored and retrieved faithfully in column profiles."""
        schema_id = str(uuid.uuid4())
        distribution_columns = [
            {
                "column_name": "uniform_col",
                "data_type": "INTEGER",
                "null_percentage": 0.0,
                "unique_count": 100,
                "distribution_type": "uniform",
                "min_value": 1,
                "max_value": 100,
            },
            {
                "column_name": "categorical_col",
                "data_type": "VARCHAR(20)",
                "null_percentage": 0.0,
                "unique_count": 3,
                "distribution_type": "categorical",
                "top_values": [
                    {"value": "A", "frequency": 0.5},
                    {"value": "B", "frequency": 0.3},
                    {"value": "C", "frequency": 0.2},
                ],
            },
            {
                "column_name": "log_normal_col",
                "data_type": "DECIMAL(18,2)",
                "null_percentage": 0.0,
                "unique_count": 500,
                "distribution_type": "log_normal",
                "mean": 100.0,
                "std_dev": 50.0,
            },
            {
                "column_name": "bernoulli_col",
                "data_type": "BOOLEAN",
                "null_percentage": 0.0,
                "unique_count": 2,
                "distribution_type": "bernoulli",
                "true_percentage": 0.75,
            },
        ]
        doc = _make_statistical_profile(
            sample_tenant_id,
            schema_id,
            table_profiles=[
                {
                    "table_name": "DIST_TEST",
                    "row_count": 1000,
                    "column_profiles": distribution_columns,
                }
            ],
        )
        statistical_profiles_collection.insert_one(doc)

        stored = statistical_profiles_collection.find_one(
            {"profile_id": doc["profile_id"]}
        )
        cols = stored["table_profiles"][0]["column_profiles"]
        dist_types = {c["column_name"]: c["distribution_type"] for c in cols}
        assert dist_types["uniform_col"] == "uniform"
        assert dist_types["categorical_col"] == "categorical"
        assert dist_types["log_normal_col"] == "log_normal"
        assert dist_types["bernoulli_col"] == "bernoulli"


# ============================================================================
# 3. TestSchemaDefinitionsCollection  (9 tests)
# ============================================================================

@pytest.mark.integration
class TestSchemaDefinitionsCollection:
    """CRUD and index tests for the ``schema_definitions`` collection.

    Schema definitions store ERP table/column metadata discovered by the
    Profiling Service, including relationships and module classification.
    """

    def test_insert_schema_definition(
        self, schema_definitions_collection, sample_tenant_id
    ):
        """Inserting a valid schema definition document succeeds."""
        doc = _make_schema_definition(sample_tenant_id)
        result = schema_definitions_collection.insert_one(doc)

        assert result.inserted_id is not None
        assert result.acknowledged is True
        stored = schema_definitions_collection.find_one({"_id": result.inserted_id})
        assert stored is not None
        assert stored["schema_id"] == doc["schema_id"]
        assert stored["erp_system"] == "SAP"
        assert stored["erp_module"] == "financial_accounting"

    def test_read_schema_definition_by_id(
        self, schema_definitions_collection, sample_tenant_id
    ):
        """Reading a schema by ``schema_id`` returns the correct record."""
        doc = _make_schema_definition(sample_tenant_id)
        schema_definitions_collection.insert_one(doc)

        stored = schema_definitions_collection.find_one(
            {"schema_id": doc["schema_id"]}
        )
        assert stored is not None
        assert stored["name"] == doc["name"]
        assert stored["version"] == "1.0.0"

    def test_update_schema_definition(
        self, schema_definitions_collection, sample_tenant_id
    ):
        """Updating a schema definition's ``version`` and ``updated_at`` persists."""
        doc = _make_schema_definition(sample_tenant_id)
        schema_definitions_collection.insert_one(doc)

        new_time = _utcnow()
        result = schema_definitions_collection.update_one(
            {"schema_id": doc["schema_id"]},
            {"$set": {"version": "2.0.0", "updated_at": new_time}},
        )
        assert result.modified_count == 1
        updated = schema_definitions_collection.find_one(
            {"schema_id": doc["schema_id"]}
        )
        assert updated["version"] == "2.0.0"

    def test_delete_schema_definition(
        self, schema_definitions_collection, sample_tenant_id
    ):
        """Deleting a schema definition removes it from the collection."""
        doc = _make_schema_definition(sample_tenant_id)
        schema_definitions_collection.insert_one(doc)

        result = schema_definitions_collection.delete_one(
            {"schema_id": doc["schema_id"]}
        )
        assert result.deleted_count == 1
        assert (
            schema_definitions_collection.find_one(
                {"schema_id": doc["schema_id"]}
            )
            is None
        )

    def test_schema_definitions_index_erp_system(
        self, schema_definitions_collection
    ):
        """The ``idx_erp_system`` index exists on ``erp_system``."""
        indexes = schema_definitions_collection.index_information()
        assert "idx_erp_system" in indexes
        keys = indexes["idx_erp_system"]["key"]
        assert ("erp_system", 1) in keys

    def test_schema_definitions_index_tenant_id(
        self, schema_definitions_collection
    ):
        """The ``idx_tenant_id`` index exists on ``tenant_id``."""
        indexes = schema_definitions_collection.index_information()
        assert "idx_tenant_id" in indexes
        keys = indexes["idx_tenant_id"]["key"]
        assert ("tenant_id", 1) in keys

    def test_schema_definitions_nested_tables(
        self, schema_definitions_collection, sample_tenant_id
    ):
        """Nested ``tables`` with ``columns`` sub-documents are stored and
        queried correctly."""
        doc = _make_schema_definition(
            sample_tenant_id,
            tables=[
                {
                    "name": "CUSTOMERS",
                    "columns": [
                        {"name": "cust_id", "data_type": "VARCHAR(10)", "nullable": False, "primary_key": True},
                        {"name": "cust_name", "data_type": "VARCHAR(100)", "nullable": False, "primary_key": False},
                        {"name": "email", "data_type": "VARCHAR(255)", "nullable": True, "primary_key": False},
                    ],
                },
                {
                    "name": "ORDERS",
                    "columns": [
                        {"name": "order_id", "data_type": "VARCHAR(36)", "nullable": False, "primary_key": True},
                        {"name": "cust_id", "data_type": "VARCHAR(10)", "nullable": False, "primary_key": False},
                        {"name": "order_date", "data_type": "DATE", "nullable": False, "primary_key": False},
                        {"name": "total_amount", "data_type": "DECIMAL(18,2)", "nullable": False, "primary_key": False},
                    ],
                },
            ],
            metadata={"total_tables": 2, "total_columns": 7, "total_relationships": 0},
        )
        schema_definitions_collection.insert_one(doc)

        stored = schema_definitions_collection.find_one(
            {"schema_id": doc["schema_id"]}
        )
        assert len(stored["tables"]) == 2
        table_names = {t["name"] for t in stored["tables"]}
        assert table_names == {"CUSTOMERS", "ORDERS"}

        customers = next(t for t in stored["tables"] if t["name"] == "CUSTOMERS")
        assert len(customers["columns"]) == 3
        pk_cols = [c for c in customers["columns"] if c.get("primary_key")]
        assert len(pk_cols) == 1
        assert pk_cols[0]["name"] == "cust_id"

    def test_schema_definitions_nested_relationships(
        self, schema_definitions_collection, sample_tenant_id
    ):
        """Nested ``relationships`` array stores foreign key metadata correctly."""
        doc = _make_schema_definition(
            sample_tenant_id,
            relationships=[
                {
                    "name": "fk_order_customer",
                    "source_table": "ORDERS",
                    "source_column": "cust_id",
                    "target_table": "CUSTOMERS",
                    "target_column": "cust_id",
                    "relationship_type": "many_to_one",
                },
                {
                    "name": "fk_line_item_order",
                    "source_table": "LINE_ITEMS",
                    "source_column": "order_id",
                    "target_table": "ORDERS",
                    "target_column": "order_id",
                    "relationship_type": "many_to_one",
                },
            ],
        )
        schema_definitions_collection.insert_one(doc)

        stored = schema_definitions_collection.find_one(
            {"schema_id": doc["schema_id"]}
        )
        assert len(stored["relationships"]) == 2
        rel_names = {r["name"] for r in stored["relationships"]}
        assert "fk_order_customer" in rel_names
        assert "fk_line_item_order" in rel_names

        fk = next(
            r for r in stored["relationships"]
            if r["name"] == "fk_order_customer"
        )
        assert fk["source_table"] == "ORDERS"
        assert fk["target_table"] == "CUSTOMERS"
        assert fk["relationship_type"] == "many_to_one"

    @pytest.mark.parametrize(
        "erp_module",
        [
            "financial_accounting",
            "human_resources",
            "sales_distribution",
            "material_management",
        ],
    )
    def test_schema_definitions_erp_modules(
        self, schema_definitions_collection, sample_tenant_id, erp_module
    ):
        """Querying by ``erp_module`` correctly filters schemas for each of
        the four initial ERP modules (C-005)."""
        schema_definitions_collection.insert_one(
            _make_schema_definition(sample_tenant_id, erp_module=erp_module)
        )

        results = list(
            schema_definitions_collection.find(
                {"tenant_id": sample_tenant_id, "erp_module": erp_module}
            )
        )
        assert len(results) == 1, f"Expected 1 schema for module {erp_module}"
        assert results[0]["erp_module"] == erp_module


# ============================================================================
# 4. TestAuditLogsCollection  (12 tests)
# ============================================================================

@pytest.mark.integration
class TestAuditLogsCollection:
    """Immutability, TTL, hash chain, and query tests for ``audit_logs``.

    Audit logs are tamper-evident (SHA-256 hash chain) and subject to a
    7-year TTL index for SOC 2 Type II compliance.
    """

    def test_insert_audit_log(
        self, audit_logs_collection, sample_tenant_id
    ):
        """Inserting a valid audit log entry succeeds."""
        doc = _make_audit_log(sample_tenant_id)
        result = audit_logs_collection.insert_one(doc)

        assert result.inserted_id is not None
        stored = audit_logs_collection.find_one({"_id": result.inserted_id})
        assert stored is not None
        assert stored["event_id"] == doc["event_id"]
        assert stored["tenant_id"] == sample_tenant_id
        assert stored["action"] == "generation.job.created"
        assert stored["hash"] is not None
        assert len(stored["hash"]) == 64  # SHA-256 hex digest length

    def test_read_audit_logs_by_tenant(
        self, audit_logs_collection, sample_tenant_id, secondary_tenant_id
    ):
        """Querying audit logs by ``tenant_id`` returns only that tenant's entries."""
        for _ in range(3):
            audit_logs_collection.insert_one(
                _make_audit_log(sample_tenant_id)
            )
        for _ in range(2):
            audit_logs_collection.insert_one(
                _make_audit_log(secondary_tenant_id)
            )

        primary_logs = list(
            audit_logs_collection.find({"tenant_id": sample_tenant_id})
        )
        secondary_logs = list(
            audit_logs_collection.find({"tenant_id": secondary_tenant_id})
        )
        assert len(primary_logs) == 3
        assert len(secondary_logs) == 2

    def test_audit_logs_immutable(
        self, audit_logs_collection, sample_tenant_id
    ):
        """Audit log entries should not be modified after insertion.

        While MongoDB does not natively enforce immutability, we verify that
        the application-level expectation is that updates to the ``action``,
        ``details``, or ``hash`` fields would break the hash chain.  This
        test demonstrates that modifying a hash-chained field produces an
        inconsistent hash, detectable at read time.
        """
        doc = _make_audit_log(sample_tenant_id)
        audit_logs_collection.insert_one(doc)

        # Simulate unauthorized modification
        audit_logs_collection.update_one(
            {"event_id": doc["event_id"]},
            {"$set": {"action": "TAMPERED_ACTION"}},
        )
        tampered = audit_logs_collection.find_one(
            {"event_id": doc["event_id"]}
        )

        # Recompute hash from stored payload — should not match
        recomputed_payload = json.dumps(
            {
                "event_id": tampered["event_id"],
                "action": tampered["action"],
                "details": tampered["details"],
                "previous_hash": tampered["previous_hash"],
            },
            sort_keys=True,
            default=str,
        )
        recomputed_hash = hashlib.sha256(
            recomputed_payload.encode("utf-8")
        ).hexdigest()
        assert tampered["hash"] != recomputed_hash, (
            "Hash should NOT match after tampering — immutability violated"
        )

    def test_audit_logs_ttl_index(self, audit_logs_collection):
        """The ``idx_ttl_7yr`` TTL index exists with correct expiration.

        7 years = 7 * 365 * 24 * 3600 = 220,752,000 seconds.
        """
        indexes = audit_logs_collection.index_information()
        assert "idx_ttl_7yr" in indexes
        ttl_index = indexes["idx_ttl_7yr"]
        assert ttl_index.get("expireAfterSeconds") == 220_752_000
        keys = ttl_index["key"]
        assert ("created_at", 1) in keys

    def test_audit_logs_index_timestamp(self, audit_logs_collection):
        """The ``idx_timestamp`` index exists on ``timestamp``."""
        indexes = audit_logs_collection.index_information()
        assert "idx_timestamp" in indexes
        keys = indexes["idx_timestamp"]["key"]
        assert ("timestamp", 1) in keys

    def test_audit_logs_index_user_id(self, audit_logs_collection):
        """The ``idx_user_id`` index exists on ``user_id``."""
        indexes = audit_logs_collection.index_information()
        assert "idx_user_id" in indexes
        keys = indexes["idx_user_id"]["key"]
        assert ("user_id", 1) in keys

    def test_audit_logs_index_action(self, audit_logs_collection):
        """The ``idx_action`` index exists on ``action``."""
        indexes = audit_logs_collection.index_information()
        assert "idx_action" in indexes
        keys = indexes["idx_action"]["key"]
        assert ("action", 1) in keys

    def test_audit_logs_tamper_evident_hash(
        self, audit_logs_collection, sample_tenant_id
    ):
        """Each audit entry's ``hash`` is a valid 64-char SHA-256 hex digest
        that matches a deterministic recomputation from event fields."""
        doc = _make_audit_log(sample_tenant_id)
        audit_logs_collection.insert_one(doc)

        stored = audit_logs_collection.find_one({"event_id": doc["event_id"]})
        assert len(stored["hash"]) == 64

        # Recompute and verify match
        recomputed_payload = json.dumps(
            {
                "event_id": stored["event_id"],
                "action": stored["action"],
                "details": stored["details"],
                "previous_hash": stored["previous_hash"],
            },
            sort_keys=True,
            default=str,
        )
        expected_hash = hashlib.sha256(
            recomputed_payload.encode("utf-8")
        ).hexdigest()
        assert stored["hash"] == expected_hash

    def test_audit_logs_hash_chain(
        self, audit_logs_collection, sample_tenant_id
    ):
        """A sequence of audit entries form a tamper-evident hash chain where
        each entry's ``previous_hash`` equals the preceding entry's ``hash``."""
        genesis_hash = "0" * 64
        chain: list[dict] = []

        prev_hash = genesis_hash
        for i in range(5):
            doc = _make_audit_log(
                sample_tenant_id,
                previous_hash=prev_hash,
                action=f"chain.event.{i}",
                details={"step": i},
            )
            audit_logs_collection.insert_one(doc)
            chain.append(doc)
            prev_hash = doc["hash"]

        # Retrieve and verify chain integrity
        stored_chain = list(
            audit_logs_collection.find({"tenant_id": sample_tenant_id}).sort(
                "timestamp", 1
            )
        )
        assert len(stored_chain) == 5

        expected_prev = genesis_hash
        for entry in stored_chain:
            assert entry["previous_hash"] == expected_prev
            # Recompute hash to verify integrity
            recomputed_payload = json.dumps(
                {
                    "event_id": entry["event_id"],
                    "action": entry["action"],
                    "details": entry["details"],
                    "previous_hash": entry["previous_hash"],
                },
                sort_keys=True,
                default=str,
            )
            expected_hash = hashlib.sha256(
                recomputed_payload.encode("utf-8")
            ).hexdigest()
            assert entry["hash"] == expected_hash
            expected_prev = entry["hash"]

    def test_audit_logs_query_by_date_range(
        self, audit_logs_collection, sample_tenant_id
    ):
        """Querying audit logs within a date range returns correct results."""
        now = _utcnow()
        # Insert logs across a 10-day span
        for day_offset in range(10):
            ts = now - timedelta(days=day_offset)
            doc = _make_audit_log(sample_tenant_id)
            doc["timestamp"] = ts
            doc["created_at"] = ts
            audit_logs_collection.insert_one(doc)

        # Query for last 3 days
        start = now - timedelta(days=3)
        results = list(
            audit_logs_collection.find(
                {
                    "tenant_id": sample_tenant_id,
                    "timestamp": {"$gte": start, "$lte": now},
                }
            )
        )
        # Days 0, 1, 2, 3 => 4 entries
        assert len(results) == 4

    def test_audit_logs_query_by_action_type(
        self, audit_logs_collection, sample_tenant_id
    ):
        """Querying audit logs by ``action`` filters correctly."""
        actions = [
            "generation.job.created",
            "generation.job.started",
            "generation.job.completed",
            "compliance.scan.completed",
            "generation.job.created",
        ]
        for action in actions:
            audit_logs_collection.insert_one(
                _make_audit_log(sample_tenant_id, action=action)
            )

        created_logs = list(
            audit_logs_collection.find(
                {"tenant_id": sample_tenant_id, "action": "generation.job.created"}
            )
        )
        assert len(created_logs) == 2

        compliance_logs = list(
            audit_logs_collection.find(
                {"tenant_id": sample_tenant_id, "action": "compliance.scan.completed"}
            )
        )
        assert len(compliance_logs) == 1

    def test_audit_logs_correlation_id(
        self, audit_logs_collection, sample_tenant_id
    ):
        """Each audit entry carries a ``correlation_id`` for distributed tracing."""
        correlation = str(uuid.uuid4())
        for i in range(3):
            doc = _make_audit_log(
                sample_tenant_id,
                action=f"correlated.event.{i}",
                details={"step": i},
            )
            doc["correlation_id"] = correlation
            audit_logs_collection.insert_one(doc)

        results = list(
            audit_logs_collection.find({"correlation_id": correlation})
        )
        assert len(results) == 3
        for entry in results:
            assert entry["correlation_id"] == correlation


# ============================================================================
# 5. TestTenantConfigurationsCollection  (6 tests)
# ============================================================================

@pytest.mark.integration
class TestTenantConfigurationsCollection:
    """CRUD, unique index, and sub-document tests for ``tenant_configurations``.

    Each tenant has exactly one configuration document (enforced by a
    unique index on ``tenant_id``).
    """

    def test_insert_tenant_configuration(
        self, tenant_configurations_collection, sample_tenant_id
    ):
        """Inserting a valid tenant configuration succeeds."""
        doc = _make_tenant_configuration(sample_tenant_id)
        result = tenant_configurations_collection.insert_one(doc)

        assert result.inserted_id is not None
        stored = tenant_configurations_collection.find_one(
            {"_id": result.inserted_id}
        )
        assert stored is not None
        assert stored["tenant_id"] == sample_tenant_id
        assert stored["status"] == "active"
        assert stored["name"] == "Test Tenant"

    def test_read_tenant_configuration(
        self, tenant_configurations_collection, sample_tenant_id
    ):
        """Reading a tenant configuration by ``tenant_id`` returns the record."""
        doc = _make_tenant_configuration(sample_tenant_id)
        tenant_configurations_collection.insert_one(doc)

        stored = tenant_configurations_collection.find_one(
            {"tenant_id": sample_tenant_id}
        )
        assert stored is not None
        assert stored["tenant_id"] == sample_tenant_id
        assert "resource_quotas" in stored
        assert "settings" in stored
        assert "subscription" in stored

    def test_update_tenant_configuration(
        self, tenant_configurations_collection, sample_tenant_id
    ):
        """Updating tenant configuration sub-documents persists correctly."""
        doc = _make_tenant_configuration(sample_tenant_id)
        tenant_configurations_collection.insert_one(doc)

        new_time = _utcnow()
        result = tenant_configurations_collection.update_one(
            {"tenant_id": sample_tenant_id},
            {
                "$set": {
                    "resource_quotas.max_concurrent_jobs": 10,
                    "settings.default_output_format": "parquet",
                    "updated_at": new_time,
                }
            },
        )
        assert result.modified_count == 1

        updated = tenant_configurations_collection.find_one(
            {"tenant_id": sample_tenant_id}
        )
        assert updated["resource_quotas"]["max_concurrent_jobs"] == 10
        assert updated["settings"]["default_output_format"] == "parquet"

    def test_tenant_configurations_unique_tenant_id(
        self, tenant_configurations_collection, sample_tenant_id
    ):
        """The unique index on ``tenant_id`` prevents duplicate tenant configs."""
        doc1 = _make_tenant_configuration(sample_tenant_id)
        tenant_configurations_collection.insert_one(doc1)

        doc2 = _make_tenant_configuration(sample_tenant_id, name="Duplicate Tenant")
        with pytest.raises(DuplicateKeyError):
            tenant_configurations_collection.insert_one(doc2)

    def test_tenant_configurations_resource_quotas(
        self, tenant_configurations_collection, sample_tenant_id
    ):
        """The ``resource_quotas`` sub-document stores all quota fields."""
        doc = _make_tenant_configuration(sample_tenant_id)
        tenant_configurations_collection.insert_one(doc)

        stored = tenant_configurations_collection.find_one(
            {"tenant_id": sample_tenant_id}
        )
        quotas = stored["resource_quotas"]
        assert quotas["max_concurrent_jobs"] == 5
        assert quotas["max_records_per_job"] == 1_000_000
        assert quotas["storage_limit_gb"] == 100
        assert quotas["api_rate_limit_per_minute"] == 300

    def test_tenant_configurations_settings(
        self, tenant_configurations_collection, sample_tenant_id
    ):
        """The ``settings`` sub-document stores all preference fields."""
        doc = _make_tenant_configuration(sample_tenant_id)
        tenant_configurations_collection.insert_one(doc)

        stored = tenant_configurations_collection.find_one(
            {"tenant_id": sample_tenant_id}
        )
        settings = stored["settings"]
        assert settings["default_output_format"] == "csv"
        assert settings["default_generation_method"] == "statistical"
        assert settings["encryption_enabled"] is True
        assert settings["audit_logging_enabled"] is True
        assert settings["notification_email"] == "admin@test.local"
        assert settings["webhook_url"] is None


# ============================================================================
# 6. TestCrossCollectionIntegrity  (7 tests)
# ============================================================================

@pytest.mark.integration
class TestCrossCollectionIntegrity:
    """Referential integrity tests verifying cross-collection relationships.

    The data model links:
      - ``generation_profiles.schema_id`` → ``schema_definitions.schema_id``
      - ``generation_profiles.statistical_profile_id`` → ``statistical_profiles.profile_id``
      - ``statistical_profiles.schema_id`` → ``schema_definitions.schema_id``

    These references are enforced at the application level (not by MongoDB
    constraints), so these tests verify the patterns that the application
    services must maintain.
    """

    def test_generation_profile_references_schema(
        self,
        generation_profiles_collection,
        schema_definitions_collection,
        sample_tenant_id,
    ):
        """A generation profile's ``schema_id`` can be resolved to an existing
        schema definition document."""
        schema_doc = _make_schema_definition(sample_tenant_id)
        schema_definitions_collection.insert_one(schema_doc)

        gen_doc = _make_generation_profile(
            sample_tenant_id, schema_id=schema_doc["schema_id"]
        )
        generation_profiles_collection.insert_one(gen_doc)

        # Resolve the reference
        stored_gen = generation_profiles_collection.find_one(
            {"job_id": gen_doc["job_id"]}
        )
        referenced_schema = schema_definitions_collection.find_one(
            {"schema_id": stored_gen["schema_id"]}
        )
        assert referenced_schema is not None
        assert referenced_schema["schema_id"] == schema_doc["schema_id"]
        assert referenced_schema["tenant_id"] == sample_tenant_id

    def test_generation_profile_references_statistical_profile(
        self,
        generation_profiles_collection,
        statistical_profiles_collection,
        schema_definitions_collection,
        sample_tenant_id,
    ):
        """A generation profile can carry a ``statistical_profile_id`` that
        resolves to an existing statistical profile."""
        schema_doc = _make_schema_definition(sample_tenant_id)
        schema_definitions_collection.insert_one(schema_doc)

        profile_doc = _make_statistical_profile(
            sample_tenant_id, schema_doc["schema_id"]
        )
        statistical_profiles_collection.insert_one(profile_doc)

        gen_doc = _make_generation_profile(
            sample_tenant_id,
            schema_id=schema_doc["schema_id"],
            statistical_profile_id=profile_doc["profile_id"],
        )
        generation_profiles_collection.insert_one(gen_doc)

        # Resolve the reference
        stored_gen = generation_profiles_collection.find_one(
            {"job_id": gen_doc["job_id"]}
        )
        referenced_profile = statistical_profiles_collection.find_one(
            {"profile_id": stored_gen["statistical_profile_id"]}
        )
        assert referenced_profile is not None
        assert referenced_profile["profile_id"] == profile_doc["profile_id"]

    def test_statistical_profile_references_schema(
        self,
        statistical_profiles_collection,
        schema_definitions_collection,
        sample_tenant_id,
    ):
        """A statistical profile's ``schema_id`` resolves to an existing schema."""
        schema_doc = _make_schema_definition(sample_tenant_id)
        schema_definitions_collection.insert_one(schema_doc)

        profile_doc = _make_statistical_profile(
            sample_tenant_id, schema_doc["schema_id"]
        )
        statistical_profiles_collection.insert_one(profile_doc)

        stored_profile = statistical_profiles_collection.find_one(
            {"profile_id": profile_doc["profile_id"]}
        )
        referenced_schema = schema_definitions_collection.find_one(
            {"schema_id": stored_profile["schema_id"]}
        )
        assert referenced_schema is not None
        assert referenced_schema["schema_id"] == schema_doc["schema_id"]

    def test_orphan_detection_generation_profiles(
        self,
        generation_profiles_collection,
        schema_definitions_collection,
        sample_tenant_id,
    ):
        """Detect generation profiles whose ``schema_id`` references a
        non-existent schema definition (orphan detection)."""
        orphan_schema_id = str(uuid.uuid4())  # No matching schema
        gen_doc = _make_generation_profile(
            sample_tenant_id, schema_id=orphan_schema_id
        )
        generation_profiles_collection.insert_one(gen_doc)

        # Attempt to resolve — should not find a schema
        stored_gen = generation_profiles_collection.find_one(
            {"job_id": gen_doc["job_id"]}
        )
        referenced_schema = schema_definitions_collection.find_one(
            {"schema_id": stored_gen["schema_id"]}
        )
        assert referenced_schema is None, (
            "Orphan detected: generation profile references non-existent schema"
        )

    def test_orphan_detection_statistical_profiles(
        self,
        statistical_profiles_collection,
        schema_definitions_collection,
        sample_tenant_id,
    ):
        """Detect statistical profiles whose ``schema_id`` references a
        non-existent schema definition (orphan detection)."""
        orphan_schema_id = str(uuid.uuid4())  # No matching schema
        profile_doc = _make_statistical_profile(
            sample_tenant_id, orphan_schema_id
        )
        statistical_profiles_collection.insert_one(profile_doc)

        stored_profile = statistical_profiles_collection.find_one(
            {"profile_id": profile_doc["profile_id"]}
        )
        referenced_schema = schema_definitions_collection.find_one(
            {"schema_id": stored_profile["schema_id"]}
        )
        assert referenced_schema is None, (
            "Orphan detected: statistical profile references non-existent schema"
        )

    def test_tenant_consistency(
        self,
        generation_profiles_collection,
        schema_definitions_collection,
        statistical_profiles_collection,
        sample_tenant_id,
        secondary_tenant_id,
    ):
        """Cross-collection references must stay within the same tenant.

        A generation profile in tenant A should only reference schemas and
        profiles belonging to tenant A, not tenant B.
        """
        # Create schema in primary tenant
        schema_a = _make_schema_definition(sample_tenant_id)
        schema_definitions_collection.insert_one(schema_a)

        # Create schema in secondary tenant
        schema_b = _make_schema_definition(secondary_tenant_id)
        schema_definitions_collection.insert_one(schema_b)

        # Create profile in primary tenant referencing its own schema
        profile_a = _make_statistical_profile(
            sample_tenant_id, schema_a["schema_id"]
        )
        statistical_profiles_collection.insert_one(profile_a)

        # Create generation job in primary tenant
        gen_doc = _make_generation_profile(
            sample_tenant_id, schema_id=schema_a["schema_id"]
        )
        generation_profiles_collection.insert_one(gen_doc)

        # Verify tenant-consistent resolution
        stored_gen = generation_profiles_collection.find_one(
            {"job_id": gen_doc["job_id"]}
        )
        referenced_schema = schema_definitions_collection.find_one(
            {"schema_id": stored_gen["schema_id"], "tenant_id": sample_tenant_id}
        )
        assert referenced_schema is not None

        # Verify cross-tenant resolution fails
        cross_tenant_schema = schema_definitions_collection.find_one(
            {"schema_id": stored_gen["schema_id"], "tenant_id": secondary_tenant_id}
        )
        assert cross_tenant_schema is None, (
            "Cross-tenant reference should not resolve"
        )

    def test_cascading_reference_check(
        self,
        generation_profiles_collection,
        statistical_profiles_collection,
        schema_definitions_collection,
        sample_tenant_id,
    ):
        """Verify a full cascading reference chain:
        generation_profile → schema_definition ← statistical_profile.

        All three documents share the same ``schema_id`` and ``tenant_id``,
        forming a consistent reference graph.
        """
        schema_doc = _make_schema_definition(sample_tenant_id)
        schema_definitions_collection.insert_one(schema_doc)

        profile_doc = _make_statistical_profile(
            sample_tenant_id, schema_doc["schema_id"]
        )
        statistical_profiles_collection.insert_one(profile_doc)

        gen_doc = _make_generation_profile(
            sample_tenant_id,
            schema_id=schema_doc["schema_id"],
            statistical_profile_id=profile_doc["profile_id"],
        )
        generation_profiles_collection.insert_one(gen_doc)

        # Walk the chain: generation → schema
        stored_gen = generation_profiles_collection.find_one(
            {"job_id": gen_doc["job_id"]}
        )
        schema = schema_definitions_collection.find_one(
            {"schema_id": stored_gen["schema_id"]}
        )
        assert schema is not None

        # Walk the chain: generation → statistical_profile
        profile = statistical_profiles_collection.find_one(
            {"profile_id": stored_gen["statistical_profile_id"]}
        )
        assert profile is not None

        # Verify statistical_profile references the same schema
        assert profile["schema_id"] == schema["schema_id"]

        # Verify all belong to the same tenant
        assert stored_gen["tenant_id"] == schema["tenant_id"] == profile["tenant_id"]


# ============================================================================
# 7. TestMongoDBConfiguration  (6 tests)
# ============================================================================

@pytest.mark.integration
class TestMongoDBConfiguration:
    """Connection, pool, write concern, and read preference verification.

    These tests verify the MongoDB client's operational configuration
    against the real test instance launched by Docker Compose.
    """

    def test_mongodb_connection(self, mongo_client):
        """The MongoClient can successfully ping the server."""
        result = mongo_client.admin.command("ping")
        assert result.get("ok") == 1.0

    def test_mongodb_database_exists(self, mongo_client, mongo_db):
        """The test database is accessible and can be listed by the client."""
        # Insert a temporary document so the DB is materialised
        mongo_db["_connection_test"].insert_one({"_probe": True})
        db_names = mongo_client.list_database_names()
        assert mongo_db.name in db_names
        # Cleanup probe collection
        mongo_db.drop_collection("_connection_test")

    def test_mongodb_collection_list(self, mongo_db, setup_indexes):
        """All five core collections are creatable / listable after index setup.

        Because MongoDB lazily creates collections on first document insert,
        we insert a probe document into each collection to materialise them,
        then verify they appear in ``list_collection_names()``.
        """
        expected_collections = {
            "generation_profiles",
            "statistical_profiles",
            "schema_definitions",
            "audit_logs",
            "tenant_configurations",
        }
        # Insert probe docs to materialise collections
        for coll_name in expected_collections:
            mongo_db[coll_name].insert_one({"_probe": True})

        actual_names = set(mongo_db.list_collection_names())
        for coll in expected_collections:
            assert coll in actual_names, f"Collection '{coll}' not found"

        # Cleanup probe docs
        for coll_name in expected_collections:
            mongo_db[coll_name].delete_many({"_probe": True})

    def test_mongodb_connection_pooling(self, mongo_client):
        """The client is configured with ``maxPoolSize`` for connection pooling.

        The test conftest.py creates the client with ``maxPoolSize=10``.
        We verify the pool configuration is set.
        """
        pool_options = mongo_client.options.pool_options
        assert pool_options.max_pool_size == 10

    def test_mongodb_write_concern(self, mongo_client):
        """The client's write concern defaults to ``w=1`` (acknowledged writes)."""
        wc = mongo_client.write_concern
        # Default MongoClient write concern is w=1
        assert wc.acknowledged is True

    def test_mongodb_read_preference(self, mongo_client):
        """The client's read preference defaults to ``PRIMARY``."""
        rp = mongo_client.read_preference
        # pymongo.read_preferences.Primary mode value is 0
        assert rp.mode == 0  # PRIMARY


# ============================================================================
# 8. TestRedisCacheOperations  (10 tests)
# ============================================================================

@pytest.mark.integration
class TestRedisCacheOperations:
    """SET/GET, expiration, pipeline, pub/sub, and key pattern tests for Redis.

    All tests run against a real Redis 7.x instance.  The ``clean_redis``
    autouse fixture (``FLUSHDB``) guarantees key-space isolation.
    """

    def test_redis_connection(self, redis_client):
        """The Redis client can successfully PING the server and is the
        correct client type."""
        assert isinstance(redis_client, redis_lib.Redis)
        assert redis_client.ping() is True
        # Verify server info is accessible
        info = redis_client.info()
        assert "redis_version" in info

    def test_redis_set_get(self, redis_client):
        """Basic SET and GET operations round-trip a string value."""
        key = f"test:set_get:{uuid.uuid4().hex[:8]}"
        redis_client.set(key, "hello_redis")

        value = redis_client.get(key)
        assert value == "hello_redis"

    def test_redis_expiration(self, redis_client):
        """A key with a TTL expires and becomes inaccessible after the TTL."""
        key = f"test:expiration:{uuid.uuid4().hex[:8]}"
        redis_client.set(key, "ephemeral", ex=2)  # 2 second TTL

        # Key should exist immediately
        assert redis_client.exists(key) == 1
        ttl = redis_client.ttl(key)
        assert 0 < ttl <= 2

        # Wait for expiration
        time.sleep(3)
        assert redis_client.exists(key) == 0
        assert redis_client.get(key) is None

    def test_redis_job_progress_key(self, redis_client):
        """Job progress keys follow the ``progress:{job_id}`` pattern and
        store JSON-serialized progress data."""
        job_id = str(uuid.uuid4())
        key = f"progress:{job_id}"
        progress_data = {
            "job_id": job_id,
            "status": "generating",
            "progress_pct": 45.5,
            "records_generated": 4550,
            "total_records": 10000,
            "current_table": "JOURNAL_ENTRIES",
        }
        redis_client.set(key, json.dumps(progress_data))

        raw = redis_client.get(key)
        assert raw is not None
        parsed = json.loads(raw)
        assert parsed["job_id"] == job_id
        assert parsed["progress_pct"] == 45.5
        assert parsed["status"] == "generating"
        assert parsed["records_generated"] == 4550

    def test_redis_session_storage(self, redis_client):
        """Session data stored under ``session:{session_id}`` keys can be
        set, read, and carry a TTL for session expiry."""
        session_id = str(uuid.uuid4())
        key = f"session:{session_id}"
        session_data = {
            "user_id": str(uuid.uuid4()),
            "tenant_id": "tenant-integration-primary-00000001",
            "roles": ["platform_admin"],
            "email": "admin@integration.test",
        }
        redis_client.set(key, json.dumps(session_data), ex=3600)  # 1hr TTL

        raw = redis_client.get(key)
        parsed = json.loads(raw)
        assert parsed["roles"] == ["platform_admin"]
        assert parsed["email"] == "admin@integration.test"

        ttl = redis_client.ttl(key)
        assert 3500 < ttl <= 3600

    def test_redis_rate_limit_counter(self, redis_client):
        """Rate limit counters under ``rate_limit:{endpoint}:{tenant_id}``
        use INCR for atomic increment and carry a TTL window."""
        tenant_id = "tenant-integration-primary-00000001"
        key = f"rate_limit:/api/v1/generation/jobs:{tenant_id}"

        # Simulate 5 requests
        for _ in range(5):
            redis_client.incr(key)

        count = int(redis_client.get(key))
        assert count == 5

        # Set expiration window (60 seconds for rate limit window)
        redis_client.set(key, count, ex=60)
        ttl = redis_client.ttl(key)
        assert 0 < ttl <= 60

    def test_redis_pipeline_transaction(self, redis_client):
        """Pipeline transactions execute multiple commands atomically."""
        keys = [f"pipe:{i}:{uuid.uuid4().hex[:6]}" for i in range(5)]

        pipe = redis_client.pipeline(transaction=True)
        for i, key in enumerate(keys):
            pipe.set(key, f"value_{i}")
        results = pipe.execute()

        # All SET commands should return True
        assert all(r is True for r in results)

        # Verify all keys were set
        for i, key in enumerate(keys):
            assert redis_client.get(key) == f"value_{i}"

    def test_redis_cache_invalidation(self, redis_client):
        """Cache keys can be invalidated (deleted) individually and in bulk."""
        # Set up cache keys
        cache_keys = [
            f"cache:schema:{uuid.uuid4().hex[:8]}",
            f"cache:profile:{uuid.uuid4().hex[:8]}",
            f"cache:template:{uuid.uuid4().hex[:8]}",
        ]
        for key in cache_keys:
            redis_client.set(key, "cached_data")

        # Verify all exist
        for key in cache_keys:
            assert redis_client.exists(key) == 1

        # Invalidate one key
        redis_client.delete(cache_keys[0])
        assert redis_client.exists(cache_keys[0]) == 0
        assert redis_client.exists(cache_keys[1]) == 1

        # Invalidate remaining keys
        redis_client.delete(*cache_keys[1:])
        for key in cache_keys:
            assert redis_client.exists(key) == 0

    def test_redis_pub_sub(self, redis_client):
        """Publish/subscribe messaging works for job progress notifications.

        Uses a non-blocking subscription pattern with ``get_message()``
        to avoid blocking the test runner.
        """
        channel = f"job_progress:{uuid.uuid4().hex[:8]}"
        pubsub = redis_client.pubsub()
        pubsub.subscribe(channel)

        # Consume the subscribe confirmation message
        # (type='subscribe', channel=..., data=1)
        confirmation = pubsub.get_message(timeout=5)
        assert confirmation is not None
        assert confirmation["type"] == "subscribe"

        # Publish a progress update
        message_data = json.dumps({"progress_pct": 75.0, "status": "generating"})
        receivers = redis_client.publish(channel, message_data)
        assert receivers >= 1  # At least our subscriber

        # Allow brief propagation delay
        time.sleep(0.5)

        # Receive the message
        msg = pubsub.get_message(timeout=5)
        assert msg is not None
        assert msg["type"] == "message"
        assert msg["channel"] == channel
        parsed = json.loads(msg["data"])
        assert parsed["progress_pct"] == 75.0

        pubsub.unsubscribe(channel)
        pubsub.close()

    def test_redis_key_patterns(self, redis_client):
        """Key naming conventions follow the defined patterns:
        progress:*, session:*, rate_limit:*, cache:*."""
        # Insert keys following each pattern
        pattern_keys = {
            "progress:job-abc": "progress_data",
            "session:sess-123": "session_data",
            "rate_limit:endpoint-x": "5",
            "cache:schema-xyz": "schema_cache",
        }
        for key, value in pattern_keys.items():
            redis_client.set(key, value)

        # Verify pattern-based key discovery
        progress_keys = redis_client.keys("progress:*")
        assert len(progress_keys) >= 1
        assert "progress:job-abc" in progress_keys

        session_keys = redis_client.keys("session:*")
        assert len(session_keys) >= 1
        assert "session:sess-123" in session_keys

        rate_limit_keys = redis_client.keys("rate_limit:*")
        assert len(rate_limit_keys) >= 1
        assert "rate_limit:endpoint-x" in rate_limit_keys

        cache_keys = redis_client.keys("cache:*")
        assert len(cache_keys) >= 1
        assert "cache:schema-xyz" in cache_keys


# ============================================================================
# 9. TestDataSerialization  (6 tests)
# ============================================================================

@pytest.mark.integration
class TestDataSerialization:
    """BSON ObjectId, datetime, UUID, nested doc, and large document handling.

    These tests verify that MongoDB correctly round-trips various data
    types through BSON serialization/deserialization.
    """

    def test_bson_objectid_handling(self, mongo_db):
        """BSON ``ObjectId`` is automatically assigned as ``_id`` when not
        provided, and can be used for document retrieval."""
        coll = mongo_db["_serialization_test"]
        try:
            doc = {"data": "objectid_test", "value": 42}
            result = coll.insert_one(doc)

            # Verify _id is an ObjectId
            assert isinstance(result.inserted_id, ObjectId)

            # Retrieve by ObjectId
            stored = coll.find_one({"_id": result.inserted_id})
            assert stored is not None
            assert stored["data"] == "objectid_test"
            assert isinstance(stored["_id"], ObjectId)

            # Verify ObjectId string representation round-trip
            oid_str = str(result.inserted_id)
            assert len(oid_str) == 24  # ObjectId hex string is 24 chars
            reconstructed = ObjectId(oid_str)
            assert reconstructed == result.inserted_id

            # Query with constructed ObjectId
            found = coll.find_one({"_id": ObjectId(oid_str)})
            assert found is not None
            assert found["value"] == 42
        finally:
            coll.drop()

    def test_datetime_storage(self, mongo_db):
        """Python ``datetime`` objects are stored as BSON Date and retrieved
        with timezone-aware UTC timestamps."""
        coll = mongo_db["_serialization_test"]
        try:
            now = _utcnow()
            past = now - timedelta(days=30)
            future = now + timedelta(days=365)

            doc = {
                "created_at": now,
                "past_date": past,
                "future_date": future,
            }
            result = coll.insert_one(doc)

            stored = coll.find_one({"_id": result.inserted_id})
            assert stored is not None

            # Verify datetime round-trip (MongoDB stores with millisecond precision)
            assert isinstance(stored["created_at"], datetime)
            assert isinstance(stored["past_date"], datetime)
            assert isinstance(stored["future_date"], datetime)

            # Verify date ordering is preserved
            assert stored["past_date"] < stored["created_at"] < stored["future_date"]

            # Verify range queries work with datetimes
            range_result = coll.find_one(
                {"created_at": {"$gte": past, "$lte": future}}
            )
            assert range_result is not None
        finally:
            coll.drop()

    def test_uuid_storage(self, mongo_db):
        """UUID values stored as strings round-trip faithfully and support
        exact-match queries."""
        coll = mongo_db["_serialization_test"]
        try:
            test_uuid = str(uuid.uuid4())
            doc = {"entity_id": test_uuid, "name": "UUID Test Entity"}
            _result = coll.insert_one(doc)

            # Retrieve by UUID string
            stored = coll.find_one({"entity_id": test_uuid})
            assert stored is not None
            assert stored["entity_id"] == test_uuid
            assert isinstance(stored["entity_id"], str)

            # Verify UUID format (8-4-4-4-12 hex pattern)
            parts = stored["entity_id"].split("-")
            assert len(parts) == 5
            assert [len(p) for p in parts] == [8, 4, 4, 4, 12]
        finally:
            coll.drop()

    def test_nested_document_storage(self, mongo_db):
        """Deeply nested documents (3+ levels) are stored and retrieved
        with full structural fidelity."""
        coll = mongo_db["_serialization_test"]
        try:
            doc = {
                "level_1": {
                    "name": "outer",
                    "level_2": {
                        "name": "middle",
                        "level_3": {
                            "name": "inner",
                            "value": 999,
                            "tags": ["deep", "nested", "test"],
                        },
                    },
                },
            }
            result = coll.insert_one(doc)

            stored = coll.find_one({"_id": result.inserted_id})
            assert stored["level_1"]["name"] == "outer"
            assert stored["level_1"]["level_2"]["name"] == "middle"
            assert stored["level_1"]["level_2"]["level_3"]["name"] == "inner"
            assert stored["level_1"]["level_2"]["level_3"]["value"] == 999
            assert stored["level_1"]["level_2"]["level_3"]["tags"] == [
                "deep", "nested", "test"
            ]

            # Verify dot-notation query on nested field
            found = coll.find_one(
                {"level_1.level_2.level_3.value": 999}
            )
            assert found is not None
        finally:
            coll.drop()

    def test_array_field_storage(self, mongo_db):
        """Array fields with mixed types and nested objects are stored
        and queryable via MongoDB array operators."""
        coll = mongo_db["_serialization_test"]
        try:
            doc = {
                "tags": ["alpha", "beta", "gamma"],
                "scores": [95.5, 87.3, 91.0, 78.2],
                "items": [
                    {"name": "item_a", "quantity": 10},
                    {"name": "item_b", "quantity": 25},
                    {"name": "item_c", "quantity": 5},
                ],
            }
            result = coll.insert_one(doc)

            stored = coll.find_one({"_id": result.inserted_id})
            assert len(stored["tags"]) == 3
            assert "beta" in stored["tags"]
            assert len(stored["scores"]) == 4
            assert len(stored["items"]) == 3

            # Array element query with $in
            found_by_tag = coll.find_one({"tags": {"$in": ["beta"]}})
            assert found_by_tag is not None

            # Array element query with $elemMatch on nested array
            found_by_item = coll.find_one(
                {"items": {"$elemMatch": {"name": "item_b", "quantity": 25}}}
            )
            assert found_by_item is not None

            # Array size query
            found_by_size = coll.find_one({"tags": {"$size": 3}})
            assert found_by_size is not None
        finally:
            coll.drop()

    def test_large_document_storage(self, mongo_db):
        """A large document (approaching but under 16 MB BSON limit) stores
        and retrieves correctly.  We test with a ~1 MB document containing
        a large array of records."""
        coll = mongo_db["_serialization_test"]
        try:
            # Generate a ~1 MB document with 10,000 embedded records
            large_array = [
                {
                    "record_id": str(uuid.uuid4()),
                    "account_id": f"ACCT-{i:06d}",
                    "amount": round(i * 1.23, 2),
                    "currency": "USD",
                    "description": f"Transaction record number {i} for large document test",
                }
                for i in range(10000)
            ]
            doc = {
                "batch_id": str(uuid.uuid4()),
                "record_count": len(large_array),
                "records": large_array,
            }
            result = coll.insert_one(doc)

            stored = coll.find_one({"_id": result.inserted_id})
            assert stored is not None
            assert stored["record_count"] == 10000
            assert len(stored["records"]) == 10000
            assert stored["records"][0]["account_id"] == "ACCT-000000"
            assert stored["records"][9999]["account_id"] == "ACCT-009999"
        finally:
            coll.drop()
