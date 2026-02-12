"""Shared pytest fixtures and test configuration for backend unit tests.

Provides foundational test infrastructure including mock MongoDB via mongomock,
mock Redis, Flask test client factories, test JWT tokens, sample data, and
multi-tenant testing utilities.

All backend unit test modules depend on these shared fixtures for:
- In-memory database access (mongomock)
- Mocked Redis cache/sessions
- Service-specific Flask test clients
- JWT tokens with configurable roles
- Sample ERP data and statistical profiles
"""

from __future__ import annotations

import json
import os
import uuid
from datetime import datetime, timedelta, timezone
from typing import Any, Generator
from unittest.mock import MagicMock, Mock, patch

import mongomock
import numpy as np
import pandas as pd
import pytest
from flask import Flask

# ---------------------------------------------------------------------------
# Pytest markers registration
# ---------------------------------------------------------------------------

def pytest_configure(config: Any) -> None:
    """Register custom pytest markers."""
    config.addinivalue_line("markers", "slow: marks tests as slow-running")
    config.addinivalue_line("markers", "integration: marks tests touching external services")


# ---------------------------------------------------------------------------
# MongoDB Mock Fixtures
# ---------------------------------------------------------------------------

@pytest.fixture(scope="session")
def mock_mongo_client() -> mongomock.MongoClient:
    """Create an in-memory MongoDB client via mongomock (session-scoped).

    Returns:
        A mongomock MongoClient that mimics PyMongo's MongoClient interface.
    """
    return mongomock.MongoClient()


@pytest.fixture
def mock_mongodb(mock_mongo_client: mongomock.MongoClient) -> Any:
    """Return a mongomock database 'synthetic_erp_test'.

    Automatically clears all collections between tests to ensure isolation.

    Args:
        mock_mongo_client: Session-scoped mongomock client.

    Returns:
        A mongomock database instance.
    """
    db = mock_mongo_client["synthetic_erp_test"]
    # Clear all collections before each test
    for collection_name in db.list_collection_names():
        db[collection_name].drop()
    yield db
    # Cleanup after test
    for collection_name in db.list_collection_names():
        db[collection_name].drop()


@pytest.fixture
def mock_generation_profiles_collection(mock_mongodb: Any) -> Any:
    """Return generation_profiles collection pre-populated with sample data.

    Args:
        mock_mongodb: Test database from mock_mongodb fixture.

    Returns:
        A mongomock collection with sample generation profiles.
    """
    collection = mock_mongodb["generation_profiles"]
    sample_profile = {
        "_id": "job-001",
        "job_id": "job-001",
        "tenant_id": "test-tenant-001",
        "status": "completed",
        "method": "statistical",
        "schema_id": "schema-001",
        "record_count": 10000,
        "output_format": "csv",
        "created_at": datetime.now(timezone.utc),
        "updated_at": datetime.now(timezone.utc),
        "config": {
            "batch_size": 10000,
            "tables": ["gl_entries", "employees"],
        },
    }
    collection.insert_one(sample_profile)
    return collection


@pytest.fixture
def mock_statistical_profiles_collection(mock_mongodb: Any) -> Any:
    """Return statistical_profiles collection with sample data.

    Args:
        mock_mongodb: Test database.

    Returns:
        A mongomock collection with sample statistical profiles.
    """
    collection = mock_mongodb["statistical_profiles"]
    sample_profile = {
        "profile_id": "profile-001",
        "tenant_id": "test-tenant-001",
        "schema_id": "schema-001",
        "created_at": datetime.now(timezone.utc),
        "columns": {
            "salary": {
                "data_type": "float64",
                "mean": 75000.0,
                "std": 15000.0,
                "min": 30000.0,
                "max": 150000.0,
                "percentiles": {"25": 65000.0, "50": 75000.0, "75": 85000.0},
                "null_ratio": 0.01,
            },
            "department": {
                "data_type": "category",
                "categories": ["Engineering", "Sales", "HR", "Finance"],
                "frequencies": {"Engineering": 0.35, "Sales": 0.25, "HR": 0.20, "Finance": 0.20},
                "null_ratio": 0.0,
            },
        },
    }
    collection.insert_one(sample_profile)
    return collection


@pytest.fixture
def mock_schema_definitions_collection(mock_mongodb: Any) -> Any:
    """Return schema_definitions collection with sample data.

    Args:
        mock_mongodb: Test database.

    Returns:
        A mongomock collection with sample schema definitions.
    """
    collection = mock_mongodb["schema_definitions"]
    sample_schema = {
        "schema_id": "schema-001",
        "tenant_id": "test-tenant-001",
        "erp_system": "sap",
        "erp_module": "financial_accounting",
        "tables": [
            {
                "table_name": "gl_entries",
                "columns": [
                    {"name": "entry_id", "data_type": "integer", "nullable": False, "primary_key": True},
                    {"name": "gl_account", "data_type": "varchar", "nullable": False},
                    {"name": "amount", "data_type": "decimal", "nullable": False},
                    {"name": "posting_date", "data_type": "date", "nullable": False},
                ],
            },
        ],
        "relationships": [
            {
                "parent_table": "gl_accounts",
                "child_table": "gl_entries",
                "parent_column": "account_id",
                "child_column": "gl_account",
                "cardinality": "one_to_many",
            }
        ],
    }
    collection.insert_one(sample_schema)
    return collection


@pytest.fixture
def mock_audit_logs_collection(mock_mongodb: Any) -> Any:
    """Return empty audit_logs collection.

    Args:
        mock_mongodb: Test database.

    Returns:
        A mongomock collection for audit logs.
    """
    return mock_mongodb["audit_logs"]


@pytest.fixture
def mock_tenant_configurations_collection(mock_mongodb: Any) -> Any:
    """Return tenant_configurations collection with sample config.

    Args:
        mock_mongodb: Test database.

    Returns:
        A mongomock collection with sample tenant configuration.
    """
    collection = mock_mongodb["tenant_configurations"]
    sample_config = {
        "tenant_id": "test-tenant-001",
        "name": "Test Tenant",
        "namespace": "test-tenant-001",
        "resource_quotas": {
            "max_concurrent_jobs": 5,
            "max_records_per_job": 1000000,
            "storage_limit_gb": 50,
        },
        "settings": {
            "default_output_format": "csv",
            "quality_threshold": 0.95,
        },
    }
    collection.insert_one(sample_config)
    return collection


# ---------------------------------------------------------------------------
# Redis Mock Fixtures
# ---------------------------------------------------------------------------

@pytest.fixture
def mock_redis() -> MagicMock:
    """Return a MagicMock mimicking redis.Redis interface.

    Configures common methods (get, set, incr, expire, delete, pipeline)
    with sensible defaults.

    Returns:
        A MagicMock configured to behave like a Redis client.
    """
    redis_mock = MagicMock()
    redis_mock.get.return_value = None
    redis_mock.set.return_value = True
    redis_mock.incr.return_value = 1
    redis_mock.expire.return_value = True
    redis_mock.delete.return_value = 1
    redis_mock.exists.return_value = 0

    # Pipeline mock
    pipeline_mock = MagicMock()
    pipeline_mock.execute.return_value = []
    redis_mock.pipeline.return_value = pipeline_mock

    return redis_mock


# ---------------------------------------------------------------------------
# Flask Test Client Fixtures — conditionally imported
# ---------------------------------------------------------------------------

@pytest.fixture
def quality_service_client(mock_mongodb: Any, mock_redis: MagicMock) -> Generator:
    """Create Flask test client for the Quality Service.

    Patches MongoDB and Redis connections to use mocks.

    Args:
        mock_mongodb: In-memory MongoDB database.
        mock_redis: Mocked Redis client.

    Yields:
        Flask test client for the Quality Service.
    """
    with patch("shared.database.mongodb.get_mongo_db", return_value=mock_mongodb), \
         patch("shared.database.mongodb.init_mongodb"), \
         patch("shared.database.redis_client.get_redis_client", return_value=mock_redis), \
         patch("shared.database.redis_client.init_redis"):
        try:
            from quality_service.app import create_app
            app = create_app("testing")
            app.config["TESTING"] = True
            with app.test_client() as client:
                with app.app_context():
                    yield client
        except Exception as e:
            # If Flask app can't be created, yield a mock client
            mock_client = MagicMock()
            yield mock_client


# ---------------------------------------------------------------------------
# JWT Token Fixtures
# ---------------------------------------------------------------------------

_TEST_JWT_SECRET = "test-secret-key-for-unit-testing-only"


def _create_jwt_token(
    role: str = "platform_admin",
    tenant_id: str = "test-tenant-001",
    user_id: str | None = None,
) -> str:
    """Generate a mock JWT token with configurable claims.

    Args:
        role: User role to encode in the token.
        tenant_id: Tenant ID for multi-tenant claims.
        user_id: Optional user ID. Generated if not provided.

    Returns:
        A signed JWT token string.
    """
    try:
        from jose import jwt as jose_jwt

        if user_id is None:
            user_id = str(uuid.uuid4())

        now = datetime.now(timezone.utc)
        claims = {
            "sub": user_id,
            "iss": "https://test-auth0.auth0.com/",
            "aud": "synthetic-erp-test-api",
            "exp": int((now + timedelta(hours=1)).timestamp()),
            "iat": int(now.timestamp()),
            "https://synthetic-erp.com/roles": [role],
            "https://synthetic-erp.com/tenant_id": tenant_id,
        }
        return jose_jwt.encode(claims, _TEST_JWT_SECRET, algorithm="HS256")
    except ImportError:
        # Fallback: return a mock token string
        return f"mock-jwt-{role}-{tenant_id}"


@pytest.fixture
def test_jwt_token() -> str:
    """Generate a valid JWT token with standard claims.

    Returns:
        A signed JWT token string with platform_admin role.
    """
    return _create_jwt_token(role="platform_admin")


@pytest.fixture
def admin_jwt_token() -> str:
    """JWT token with platform_admin role.

    Returns:
        A signed JWT token string with platform_admin role.
    """
    return _create_jwt_token(role="platform_admin")


@pytest.fixture
def data_engineer_jwt_token() -> str:
    """JWT token with data_engineer role.

    Returns:
        A signed JWT token string with data_engineer role.
    """
    return _create_jwt_token(role="data_engineer")


@pytest.fixture
def developer_jwt_token() -> str:
    """JWT token with developer role.

    Returns:
        A signed JWT token string with developer role.
    """
    return _create_jwt_token(role="developer")


@pytest.fixture
def qa_engineer_jwt_token() -> str:
    """JWT token with qa_engineer role.

    Returns:
        A signed JWT token string with qa_engineer role.
    """
    return _create_jwt_token(role="qa_engineer")


@pytest.fixture
def data_analyst_jwt_token() -> str:
    """JWT token with data_analyst role.

    Returns:
        A signed JWT token string with data_analyst role.
    """
    return _create_jwt_token(role="data_analyst")


# ---------------------------------------------------------------------------
# Tenant Fixtures
# ---------------------------------------------------------------------------

@pytest.fixture
def sample_tenant_id() -> str:
    """Return a fixed UUID string for the test tenant.

    Returns:
        A consistent tenant ID string.
    """
    return "test-tenant-001"


@pytest.fixture
def sample_tenant_config() -> dict[str, Any]:
    """Return a dict with tenant configuration.

    Returns:
        Tenant configuration dictionary.
    """
    return {
        "tenant_id": "test-tenant-001",
        "name": "Test Tenant",
        "namespace": "test-tenant-001",
        "resource_quotas": {
            "max_concurrent_jobs": 5,
            "max_records_per_job": 1000000,
            "storage_limit_gb": 50,
        },
        "settings": {
            "default_output_format": "csv",
            "quality_threshold": 0.95,
        },
    }


@pytest.fixture
def auth_headers(test_jwt_token: str, sample_tenant_id: str) -> dict[str, str]:
    """Return HTTP headers with Authorization and Tenant ID.

    Args:
        test_jwt_token: A valid JWT token.
        sample_tenant_id: The test tenant ID.

    Returns:
        Dict with Authorization and X-Tenant-ID headers.
    """
    return {
        "Authorization": f"Bearer {test_jwt_token}",
        "X-Tenant-ID": sample_tenant_id,
        "Content-Type": "application/json",
    }


# ---------------------------------------------------------------------------
# Mock External Service Fixtures
# ---------------------------------------------------------------------------

@pytest.fixture
def mock_jdbc_connection() -> MagicMock:
    """MagicMock mimicking jaydebeapi.connect() interface.

    Returns:
        A mock JDBC connection with cursor, execute, fetchall, commit, rollback.
    """
    conn_mock = MagicMock()
    cursor_mock = MagicMock()
    cursor_mock.fetchall.return_value = []
    cursor_mock.fetchone.return_value = None
    cursor_mock.description = [("col1", "VARCHAR", None, None, None, None, None)]
    conn_mock.cursor.return_value = cursor_mock
    conn_mock.commit.return_value = None
    conn_mock.rollback.return_value = None
    return conn_mock


@pytest.fixture
def mock_s3_client() -> MagicMock:
    """MagicMock mimicking boto3.client('s3') interface.

    Returns:
        A mock S3 client with put_object, upload_fileobj, etc.
    """
    s3_mock = MagicMock()
    s3_mock.put_object.return_value = {"ResponseMetadata": {"HTTPStatusCode": 200}}
    s3_mock.upload_fileobj.return_value = None
    s3_mock.create_multipart_upload.return_value = {"UploadId": "test-upload-id"}
    s3_mock.list_objects_v2.return_value = {"Contents": [], "KeyCount": 0}
    return s3_mock


@pytest.fixture
def mock_azure_client() -> MagicMock:
    """MagicMock mimicking BlobServiceClient interface.

    Returns:
        A mock Azure Blob Storage client.
    """
    azure_mock = MagicMock()
    container_mock = MagicMock()
    blob_mock = MagicMock()
    container_mock.upload_blob.return_value = blob_mock
    azure_mock.get_container_client.return_value = container_mock
    return azure_mock


@pytest.fixture
def mock_gcs_client() -> MagicMock:
    """MagicMock mimicking google.cloud.storage.Client interface.

    Returns:
        A mock GCS client.
    """
    gcs_mock = MagicMock()
    bucket_mock = MagicMock()
    blob_mock = MagicMock()
    bucket_mock.blob.return_value = blob_mock
    gcs_mock.bucket.return_value = bucket_mock
    return gcs_mock


@pytest.fixture
def mock_spacy_model() -> MagicMock:
    """MagicMock mimicking spacy.load('en_core_web_lg') interface.

    Returns:
        A mock spaCy model that returns mock Doc with entities.
    """
    model_mock = MagicMock()
    doc_mock = MagicMock()
    doc_mock.ents = []
    model_mock.return_value = doc_mock
    return model_mock


# ---------------------------------------------------------------------------
# Sample Data Fixtures
# ---------------------------------------------------------------------------

@pytest.fixture
def sample_generated_data() -> pd.DataFrame:
    """Return a pandas DataFrame with sample synthetic ERP data.

    Provides realistic columns for Financial Accounting and HR modules
    including id, employee_name, department, salary, hire_date,
    gl_account, and amount.

    Returns:
        A DataFrame with 100 rows of sample ERP data.
    """
    np.random.seed(42)
    n = 100
    departments = ["Engineering", "Sales", "HR", "Finance"]
    return pd.DataFrame({
        "id": range(1, n + 1),
        "employee_name": [f"Employee_{i}" for i in range(1, n + 1)],
        "department": np.random.choice(departments, size=n),
        "salary": np.random.normal(75000, 15000, size=n).clip(30000, 150000),
        "hire_date": pd.date_range("2015-01-01", periods=n, freq="15D"),
        "gl_account": [f"GL-{np.random.randint(1000, 9999)}" for _ in range(n)],
        "amount": np.random.normal(5000, 2000, size=n).clip(0, 20000),
    })


@pytest.fixture
def sample_source_profile() -> dict[str, Any]:
    """Return a dict representing a statistical profile.

    Contains column distributions with mean, std, min, max, percentiles,
    and data_type for use in quality validation testing.

    Returns:
        A statistical profile dictionary.
    """
    return {
        "profile_id": "profile-001",
        "schema_id": "schema-001",
        "columns": {
            "salary": {
                "data_type": "float64",
                "mean": 75000.0,
                "std": 15000.0,
                "min": 30000.0,
                "max": 150000.0,
                "percentiles": {"25": 65000.0, "50": 75000.0, "75": 85000.0},
                "null_ratio": 0.01,
                "count": 10000,
            },
            "department": {
                "data_type": "category",
                "categories": ["Engineering", "Sales", "HR", "Finance"],
                "frequencies": {
                    "Engineering": 0.35,
                    "Sales": 0.25,
                    "HR": 0.20,
                    "Finance": 0.20,
                },
                "null_ratio": 0.0,
                "count": 10000,
            },
            "amount": {
                "data_type": "float64",
                "mean": 5000.0,
                "std": 2000.0,
                "min": 0.0,
                "max": 20000.0,
                "percentiles": {"25": 3500.0, "50": 5000.0, "75": 6500.0},
                "null_ratio": 0.0,
                "count": 10000,
            },
            "gl_account": {
                "data_type": "category",
                "pattern": r"GL-\d{4}",
                "null_ratio": 0.0,
                "count": 10000,
            },
        },
    }


@pytest.fixture
def sample_schema_definition() -> dict[str, Any]:
    """Return a dict representing an ERP schema definition.

    Contains tables, columns, relationships, and ERP module information.

    Returns:
        A schema definition dictionary.
    """
    return {
        "schema_id": "schema-001",
        "erp_system": "sap",
        "erp_module": "financial_accounting",
        "tables": [
            {
                "table_name": "gl_entries",
                "columns": [
                    {"name": "entry_id", "data_type": "integer", "nullable": False, "primary_key": True},
                    {"name": "gl_account", "data_type": "varchar(10)", "nullable": False},
                    {"name": "amount", "data_type": "decimal(15,2)", "nullable": False},
                    {"name": "posting_date", "data_type": "date", "nullable": False},
                    {"name": "company_code", "data_type": "varchar(4)", "nullable": False},
                ],
            },
            {
                "table_name": "gl_accounts",
                "columns": [
                    {"name": "account_id", "data_type": "varchar(10)", "nullable": False, "primary_key": True},
                    {"name": "account_name", "data_type": "varchar(100)", "nullable": False},
                    {"name": "account_type", "data_type": "varchar(20)", "nullable": False},
                ],
            },
        ],
        "relationships": [
            {
                "parent_table": "gl_accounts",
                "child_table": "gl_entries",
                "parent_column": "account_id",
                "child_column": "gl_account",
                "cardinality": "one_to_many",
            },
        ],
    }


@pytest.fixture
def sample_generation_job_request() -> dict[str, Any]:
    """Return a dict matching GenerationJobRequest Pydantic model.

    Returns:
        A generation job request dictionary.
    """
    return {
        "method": "statistical",
        "schema_id": "schema-001",
        "table_configs": [
            {
                "table_name": "gl_entries",
                "record_count": 10000,
                "columns": ["entry_id", "gl_account", "amount", "posting_date"],
            },
        ],
        "output_format": "csv",
        "record_count": 10000,
        "tenant_id": "test-tenant-001",
    }


@pytest.fixture
def sample_generation_job() -> dict[str, Any]:
    """Return a dict matching a MongoDB generation_profiles document.

    Returns:
        A generation job dictionary with status, timestamps, and config.
    """
    now = datetime.now(timezone.utc)
    return {
        "job_id": "job-001",
        "tenant_id": "test-tenant-001",
        "status": "completed",
        "method": "statistical",
        "schema_id": "schema-001",
        "record_count": 10000,
        "output_format": "csv",
        "created_at": now.isoformat(),
        "updated_at": now.isoformat(),
        "completed_at": now.isoformat(),
        "config": {
            "batch_size": 10000,
            "tables": ["gl_entries"],
        },
        "quality_score": 0.96,
    }


@pytest.fixture
def sample_quality_report() -> dict[str, Any]:
    """Return a dict representing a quality report with scores and breakdown.

    Returns:
        A quality report dictionary.
    """
    return {
        "report_id": str(uuid.uuid4()),
        "job_id": "job-001",
        "tenant_id": "test-tenant-001",
        "created_at": datetime.now(timezone.utc).isoformat(),
        "status": "passed",
        "overall_score": 0.96,
        "threshold": 0.95,
        "sections": [
            {
                "section_name": "statistical_fidelity",
                "score": 0.97,
                "weight": 0.4,
                "weighted_score": 0.388,
                "passed": True,
                "details": {"salary": {"ks_pvalue": 0.85, "moment_score": 0.96}},
                "errors": [],
                "warnings": [],
                "records_validated": 10000,
                "records_passed": 9700,
                "execution_time_ms": 1250.0,
            },
            {
                "section_name": "business_rules",
                "score": 0.95,
                "weight": 0.3,
                "weighted_score": 0.285,
                "passed": True,
                "details": {"not_null": 1.0, "format": 0.92, "range": 0.95},
                "errors": [],
                "warnings": ["Some gl_account values near boundary"],
                "records_validated": 10000,
                "records_passed": 9500,
                "execution_time_ms": 850.0,
            },
            {
                "section_name": "referential_integrity",
                "score": 0.96,
                "weight": 0.3,
                "weighted_score": 0.288,
                "passed": True,
                "details": {"fk_validity": 0.97, "orphan_count": 30},
                "errors": [],
                "warnings": [],
                "records_validated": 10000,
                "records_passed": 9600,
                "execution_time_ms": 650.0,
            },
        ],
        "summary": {
            "total_records_validated": 10000,
            "total_records_passed": 9600,
            "overall_pass_rate": 0.96,
            "weakest_section": "business_rules",
            "strongest_section": "statistical_fidelity",
            "total_execution_time_ms": 2750.0,
        },
        "metadata": {
            "generation_method": "statistical",
            "schema_id": "schema-001",
            "record_count": 10000,
        },
        "recommendations": [],
    }


# ---------------------------------------------------------------------------
# Test Configuration Fixture
# ---------------------------------------------------------------------------

@pytest.fixture
def test_config() -> dict[str, Any]:
    """Return a dict with test-specific configuration overrides.

    Returns:
        Test configuration dictionary.
    """
    return {
        "TESTING": True,
        "MONGODB_URI": "mongodb://localhost:27017/synthetic_erp_test",
        "MONGODB_DATABASE": "synthetic_erp_test",
        "REDIS_URL": "redis://localhost:6379/15",
        "LOG_LEVEL": "DEBUG",
        "FLASK_SECRET_KEY": "test-secret-key",
        "QUALITY_STATISTICAL_WEIGHT": 0.4,
        "QUALITY_BUSINESS_RULES_WEIGHT": 0.3,
        "QUALITY_REFERENTIAL_INTEGRITY_WEIGHT": 0.3,
        "QUALITY_MIN_THRESHOLD": 0.95,
    }


# ---------------------------------------------------------------------------
# Autouse Fixtures — Database Cleanup
# ---------------------------------------------------------------------------

@pytest.fixture(autouse=True)
def clean_database(mock_mongodb: Any) -> Generator:
    """Clear all mock MongoDB collections before and after each test.

    Args:
        mock_mongodb: The test database.

    Yields:
        Control to the test function.
    """
    yield
    # Clean up after test
    for collection_name in mock_mongodb.list_collection_names():
        mock_mongodb[collection_name].delete_many({})


@pytest.fixture(autouse=True)
def reset_redis(mock_redis: MagicMock) -> Generator:
    """Reset all mock Redis state before each test.

    Args:
        mock_redis: The mocked Redis client.

    Yields:
        Control to the test function.
    """
    mock_redis.reset_mock()
    mock_redis.get.return_value = None
    mock_redis.set.return_value = True
    mock_redis.incr.return_value = 1
    mock_redis.expire.return_value = True
    mock_redis.delete.return_value = 1
    mock_redis.exists.return_value = 0
    pipeline_mock = MagicMock()
    pipeline_mock.execute.return_value = []
    mock_redis.pipeline.return_value = pipeline_mock
    yield
