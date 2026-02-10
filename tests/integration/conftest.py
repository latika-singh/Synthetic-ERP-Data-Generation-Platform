"""
Shared pytest fixtures and test configuration for integration tests.

This module provides foundational test infrastructure for all integration test
modules in the tests/integration directory. Unlike the unit test conftest.py
which uses mocks (mongomock, MagicMock), this module connects to REAL service
instances running via Docker Compose test profile.

Fixtures include:
    - Real MongoDB 7.0 database connections with collection setup/teardown
    - Real Redis 7.x connections for cache/progress/session testing
    - HTTP client factories for each backend service endpoint
    - JWT token generation for all five RBAC roles
    - Multi-tenant test fixtures with isolated tenant IDs
    - Service health check waiters for Docker Compose readiness
    - Test data seeding and cleanup utilities
"""

import hashlib
import json
import os
import time
import uuid
from collections.abc import Generator
from datetime import UTC, datetime, timedelta

import httpx
import pymongo
import pytest
import redis as redis_lib
from jose import jwt


# ---------------------------------------------------------------------------
# 1. Environment Configuration
# ---------------------------------------------------------------------------
# Service URLs — resolved from environment variables with Docker Compose
# defaults for local development.  Each variable maps to the service name
# and port defined in the project's docker-compose.yml.

API_GATEWAY_URL: str = os.environ.get(
    "API_GATEWAY_URL", "http://localhost:5000"
)
GENERATION_ENGINE_URL: str = os.environ.get(
    "GENERATION_ENGINE_URL", "http://localhost:5001"
)
PROFILING_SERVICE_URL: str = os.environ.get(
    "PROFILING_SERVICE_URL", "http://localhost:5002"
)
QUALITY_SERVICE_URL: str = os.environ.get(
    "QUALITY_SERVICE_URL", "http://localhost:5003"
)
COMPLIANCE_SERVICE_URL: str = os.environ.get(
    "COMPLIANCE_SERVICE_URL", "http://localhost:5004"
)
PROVISIONING_SERVICE_URL: str = os.environ.get(
    "PROVISIONING_SERVICE_URL", "http://localhost:5005"
)

# Data store URIs
MONGODB_TEST_URI: str = os.environ.get(
    "MONGODB_TEST_URI", "mongodb://localhost:27017"
)
MONGODB_TEST_DB: str = os.environ.get(
    "MONGODB_TEST_DB", "synthetic_erp_test"
)
REDIS_TEST_URL: str = os.environ.get(
    "REDIS_TEST_URL", "redis://localhost:6379/1"
)

# JWT signing secret used exclusively for integration tests.  In production,
# tokens are issued by Auth0 and verified with RS256 public keys; here we
# use HS256 with a shared secret for deterministic token generation.
JWT_TEST_SECRET: str = os.environ.get(
    "JWT_TEST_SECRET", "integration-test-secret-key-do-not-use-in-prod"
)
JWT_TEST_ALGORITHM: str = "HS256"
JWT_TEST_ISSUER: str = os.environ.get(
    "JWT_TEST_ISSUER", "https://synthetic-erp-test.auth0.com/"
)
JWT_TEST_AUDIENCE: str = os.environ.get(
    "JWT_TEST_AUDIENCE", "https://api.synthetic-erp-test.local"
)

# Timeout and retry settings for service readiness checks
SERVICE_READINESS_TIMEOUT: int = int(
    os.environ.get("SERVICE_READINESS_TIMEOUT", "60")
)
SERVICE_READINESS_INTERVAL: int = int(
    os.environ.get("SERVICE_READINESS_INTERVAL", "2")
)

# Core MongoDB collection names aligned with the data-layer specification
COLLECTION_GENERATION_PROFILES = "generation_profiles"
COLLECTION_STATISTICAL_PROFILES = "statistical_profiles"
COLLECTION_SCHEMA_DEFINITIONS = "schema_definitions"
COLLECTION_AUDIT_LOGS = "audit_logs"
COLLECTION_TENANT_CONFIGURATIONS = "tenant_configurations"


# ---------------------------------------------------------------------------
# 2. Pytest Marker Registration
# ---------------------------------------------------------------------------

def pytest_configure(config: pytest.Config) -> None:
    """Register custom markers used across integration tests."""
    config.addinivalue_line(
        "markers",
        "integration: mark test as an integration test requiring live services",
    )
    config.addinivalue_line(
        "markers",
        "slow: mark test as slow-running (e.g. full pipeline end-to-end)",
    )


# ---------------------------------------------------------------------------
# 3. Service Readiness Fixture
# ---------------------------------------------------------------------------

@pytest.fixture(scope="session", autouse=True)
def wait_for_services() -> None:
    """Poll every backend service's ``/health`` endpoint until all respond.

    This session-scoped, autouse fixture runs once before any integration
    test executes.  It retries with a configurable interval (default 2 s)
    until a configurable timeout (default 60 s) is reached.  If any service
    does not become healthy in time the entire test session is skipped.

    The fixture checks all six backend micro-services to confirm that the
    Docker Compose test profile is fully operational.
    """
    service_urls = {
        "api_gateway": API_GATEWAY_URL,
        "generation_engine": GENERATION_ENGINE_URL,
        "profiling_service": PROFILING_SERVICE_URL,
        "quality_service": QUALITY_SERVICE_URL,
        "compliance_service": COMPLIANCE_SERVICE_URL,
        "provisioning_service": PROVISIONING_SERVICE_URL,
    }

    start = time.time()
    healthy_services: set[str] = set()

    while time.time() - start < SERVICE_READINESS_TIMEOUT:
        for name, base_url in service_urls.items():
            if name in healthy_services:
                continue
            try:
                with httpx.Client(timeout=5.0) as client:
                    resp = client.get(f"{base_url}/health")
                    if resp.status_code == 200:
                        # Parse JSON body to verify the service is truly
                        # healthy and not just returning a 200 with an
                        # error payload.
                        body = json.loads(resp.text) if resp.text else {}
                        status = body.get("status", "ok")
                        if status not in ("unhealthy", "error"):
                            healthy_services.add(name)
            except (httpx.ConnectError, httpx.ReadTimeout, httpx.ConnectTimeout):
                pass
            except (json.JSONDecodeError, ValueError):
                # Non-JSON 200 response is acceptable for simple
                # health endpoints that return plain-text "OK".
                healthy_services.add(name)

        if healthy_services == set(service_urls.keys()):
            return  # All services ready

        time.sleep(SERVICE_READINESS_INTERVAL)

    missing = set(service_urls.keys()) - healthy_services
    pytest.skip(
        f"Integration test services not ready after "
        f"{SERVICE_READINESS_TIMEOUT}s. Missing: {', '.join(sorted(missing))}"
    )


# ---------------------------------------------------------------------------
# 4. MongoDB Fixtures
# ---------------------------------------------------------------------------

@pytest.fixture(scope="session")
def mongo_client() -> Generator[pymongo.MongoClient, None, None]:
    """Session-scoped real MongoDB client connected to the test instance.

    Uses ``pymongo.MongoClient`` with ``maxPoolSize=10`` suitable for a
    test runner.  The client is closed at session teardown.
    """
    client: pymongo.MongoClient = pymongo.MongoClient(
        MONGODB_TEST_URI, maxPoolSize=10
    )
    # Verify connectivity immediately so failures surface early.
    client.admin.command("ping")
    yield client
    client.close()


@pytest.fixture(scope="session")
def mongo_db(mongo_client: pymongo.MongoClient) -> pymongo.database.Database:
    """Session-scoped handle to the integration-test MongoDB database."""
    return mongo_client[MONGODB_TEST_DB]


@pytest.fixture(scope="session")
def generation_profiles_collection(
    mongo_db: pymongo.database.Database,
) -> pymongo.collection.Collection:
    """Handle to the ``generation_profiles`` collection."""
    return mongo_db[COLLECTION_GENERATION_PROFILES]


@pytest.fixture(scope="session")
def statistical_profiles_collection(
    mongo_db: pymongo.database.Database,
) -> pymongo.collection.Collection:
    """Handle to the ``statistical_profiles`` collection."""
    return mongo_db[COLLECTION_STATISTICAL_PROFILES]


@pytest.fixture(scope="session")
def schema_definitions_collection(
    mongo_db: pymongo.database.Database,
) -> pymongo.collection.Collection:
    """Handle to the ``schema_definitions`` collection."""
    return mongo_db[COLLECTION_SCHEMA_DEFINITIONS]


@pytest.fixture(scope="session")
def audit_logs_collection(
    mongo_db: pymongo.database.Database,
) -> pymongo.collection.Collection:
    """Handle to the ``audit_logs`` collection."""
    return mongo_db[COLLECTION_AUDIT_LOGS]


@pytest.fixture(scope="session")
def tenant_configurations_collection(
    mongo_db: pymongo.database.Database,
) -> pymongo.collection.Collection:
    """Handle to the ``tenant_configurations`` collection."""
    return mongo_db[COLLECTION_TENANT_CONFIGURATIONS]


@pytest.fixture(scope="session", autouse=True)
def setup_indexes(
    generation_profiles_collection: pymongo.collection.Collection,
    statistical_profiles_collection: pymongo.collection.Collection,
    schema_definitions_collection: pymongo.collection.Collection,
    audit_logs_collection: pymongo.collection.Collection,
    tenant_configurations_collection: pymongo.collection.Collection,
) -> None:
    """Create required MongoDB indexes once per session.

    The indexes mirror the production index strategy defined in the data-
    layer specification (Section 6.2):
      - ``generation_profiles``: tenant_id, status, compound (tenant_id + status)
      - ``statistical_profiles``: tenant_id, schema_id
      - ``schema_definitions``: tenant_id, erp_system
      - ``audit_logs``: tenant_id, timestamp, user_id, action,
        plus a TTL index on ``created_at`` for 7-year retention
      - ``tenant_configurations``: unique on tenant_id
    """
    # generation_profiles indexes
    generation_profiles_collection.create_indexes([
        pymongo.IndexModel([("tenant_id", pymongo.ASCENDING)], name="idx_tenant_id"),
        pymongo.IndexModel([("status", pymongo.ASCENDING)], name="idx_status"),
        pymongo.IndexModel(
            [("tenant_id", pymongo.ASCENDING), ("status", pymongo.ASCENDING)],
            name="idx_tenant_status",
        ),
        pymongo.IndexModel(
            [("tenant_id", pymongo.ASCENDING), ("created_at", pymongo.ASCENDING)],
            name="idx_tenant_created",
        ),
    ])

    # statistical_profiles indexes
    statistical_profiles_collection.create_indexes([
        pymongo.IndexModel([("tenant_id", pymongo.ASCENDING)], name="idx_tenant_id"),
        pymongo.IndexModel([("schema_id", pymongo.ASCENDING)], name="idx_schema_id"),
    ])

    # schema_definitions indexes
    schema_definitions_collection.create_indexes([
        pymongo.IndexModel([("tenant_id", pymongo.ASCENDING)], name="idx_tenant_id"),
        pymongo.IndexModel([("erp_system", pymongo.ASCENDING)], name="idx_erp_system"),
    ])

    # audit_logs indexes
    # 7-year TTL = 7 * 365 * 24 * 3600 = 220_752_000 seconds
    audit_logs_collection.create_indexes([
        pymongo.IndexModel([("tenant_id", pymongo.ASCENDING)], name="idx_tenant_id"),
        pymongo.IndexModel([("timestamp", pymongo.ASCENDING)], name="idx_timestamp"),
        pymongo.IndexModel([("user_id", pymongo.ASCENDING)], name="idx_user_id"),
        pymongo.IndexModel([("action", pymongo.ASCENDING)], name="idx_action"),
        pymongo.IndexModel(
            [("created_at", pymongo.ASCENDING)],
            name="idx_ttl_7yr",
            expireAfterSeconds=220_752_000,
        ),
    ])

    # tenant_configurations indexes — unique on tenant_id
    tenant_configurations_collection.create_indexes([
        pymongo.IndexModel(
            [("tenant_id", pymongo.ASCENDING)],
            name="idx_unique_tenant_id",
            unique=True,
        ),
    ])


@pytest.fixture(autouse=True)
def clean_collections(mongo_db: pymongo.database.Database) -> None:
    """Drop all documents from every test collection before each test.

    This function-scoped, autouse fixture guarantees test isolation by
    clearing all five core collections.  Indexes are *not* dropped — they
    persist for the session via ``setup_indexes``.
    """
    for coll_name in (
        COLLECTION_GENERATION_PROFILES,
        COLLECTION_STATISTICAL_PROFILES,
        COLLECTION_SCHEMA_DEFINITIONS,
        COLLECTION_AUDIT_LOGS,
        COLLECTION_TENANT_CONFIGURATIONS,
    ):
        mongo_db[coll_name].delete_many({})


# ---------------------------------------------------------------------------
# 5. Redis Fixtures
# ---------------------------------------------------------------------------

@pytest.fixture(scope="session")
def redis_client() -> Generator[redis_lib.Redis, None, None]:
    """Session-scoped real Redis client connected to the test instance.

    Uses ``redis.Redis.from_url`` with ``decode_responses=True`` so that
    string values are returned as Python ``str`` instead of ``bytes``.
    Connectivity is verified with a ``PING`` command immediately.
    """
    client: redis_lib.Redis = redis_lib.Redis.from_url(
        REDIS_TEST_URL, decode_responses=True
    )
    client.ping()
    yield client
    client.close()


@pytest.fixture(autouse=True)
def clean_redis(redis_client: redis_lib.Redis) -> None:
    """Flush all keys in the test Redis database before each test.

    This function-scoped, autouse fixture ensures complete key-space
    isolation between integration tests.
    """
    redis_client.flushdb()


# ---------------------------------------------------------------------------
# 6. HTTP Client Fixtures (one per backend service)
# ---------------------------------------------------------------------------

@pytest.fixture(scope="session")
def api_client() -> Generator[httpx.Client, None, None]:
    """HTTP client targeting the **API Gateway** (port 5000, timeout 30 s).

    Exposes ``get()``, ``post()``, ``put()``, ``delete()``, ``patch()``.
    """
    client = httpx.Client(base_url=API_GATEWAY_URL, timeout=30.0)
    yield client
    client.close()


@pytest.fixture(scope="session")
def generation_client() -> Generator[httpx.Client, None, None]:
    """HTTP client targeting the **Generation Engine** (port 5001, timeout 60 s).

    Generation jobs may take longer; a 60 s default timeout accommodates
    batch processing.  Exposes ``get()``, ``post()``, ``put()``,
    ``delete()``, ``patch()``.
    """
    client = httpx.Client(base_url=GENERATION_ENGINE_URL, timeout=60.0)
    yield client
    client.close()


@pytest.fixture(scope="session")
def profiling_client() -> Generator[httpx.Client, None, None]:
    """HTTP client targeting the **Profiling Service** (port 5002, timeout 30 s).

    Exposes ``get()``, ``post()``, ``put()``, ``delete()``, ``patch()``.
    """
    client = httpx.Client(base_url=PROFILING_SERVICE_URL, timeout=30.0)
    yield client
    client.close()


@pytest.fixture(scope="session")
def quality_client() -> Generator[httpx.Client, None, None]:
    """HTTP client targeting the **Quality Service** (port 5003, timeout 30 s).

    Exposes ``get()``, ``post()``, ``put()``, ``delete()``, ``patch()``.
    """
    client = httpx.Client(base_url=QUALITY_SERVICE_URL, timeout=30.0)
    yield client
    client.close()


@pytest.fixture(scope="session")
def compliance_client() -> Generator[httpx.Client, None, None]:
    """HTTP client targeting the **Compliance Service** (port 5004, timeout 30 s).

    Exposes ``get()``, ``post()``, ``put()``, ``delete()``, ``patch()``.
    """
    client = httpx.Client(base_url=COMPLIANCE_SERVICE_URL, timeout=30.0)
    yield client
    client.close()


@pytest.fixture(scope="session")
def provisioning_client() -> Generator[httpx.Client, None, None]:
    """HTTP client targeting the **Provisioning Service** (port 5005, timeout 30 s).

    Exposes ``get()``, ``post()``, ``put()``, ``delete()``, ``patch()``.
    """
    client = httpx.Client(base_url=PROVISIONING_SERVICE_URL, timeout=30.0)
    yield client
    client.close()


# ---------------------------------------------------------------------------
# 7. Multi-Tenant Fixtures
# ---------------------------------------------------------------------------

@pytest.fixture(scope="session")
def sample_tenant_id() -> str:
    """Deterministic primary tenant identifier used across integration tests.

    Uses a fixed UUID so that seed data, JWT tokens, and query assertions
    are all tenant-consistent within a single session.
    """
    return "tenant-integration-primary-00000001"


@pytest.fixture(scope="session")
def secondary_tenant_id() -> str:
    """Deterministic secondary tenant identifier for cross-tenant isolation tests.

    This tenant must **never** see data belonging to ``sample_tenant_id``.
    """
    return "tenant-integration-secondary-00000002"


# ---------------------------------------------------------------------------
# 8. JWT Token Fixtures
# ---------------------------------------------------------------------------

def _create_test_jwt(
    roles: list[str],
    tenant_id: str,
    *,
    user_id: str | None = None,
    email: str | None = None,
    extra_claims: dict | None = None,
) -> str:
    """Create a signed HS256 JWT with the specified RBAC roles and tenant.

    Args:
        roles: List of role names (e.g. ``["platform_admin"]``).
        tenant_id: Tenant namespace the token is scoped to.
        user_id: Optional explicit ``sub`` claim.  Defaults to a random UUID.
        email: Optional email claim.
        extra_claims: Arbitrary additional claims merged into the payload.

    Returns:
        A compact JWS string suitable for use in an ``Authorization: Bearer``
        header.
    """
    now = datetime.now(tz=UTC)
    payload: dict = {
        "sub": user_id or str(uuid.uuid4()),
        "iss": JWT_TEST_ISSUER,
        "aud": JWT_TEST_AUDIENCE,
        "iat": now,
        "exp": now + timedelta(hours=1),
        "roles": roles,
        "tenant_id": tenant_id,
        "email": email or f"testuser-{uuid.uuid4().hex[:8]}@integration.test",
    }
    if extra_claims:
        payload.update(extra_claims)
    token: str = jwt.encode(payload, JWT_TEST_SECRET, algorithm=JWT_TEST_ALGORITHM)
    return token


@pytest.fixture(scope="session")
def test_jwt_headers(sample_tenant_id: str) -> dict[str, str]:
    """Default JWT ``Authorization`` header with **platform_admin** role.

    Provides the widest set of permissions for general-purpose tests that
    do not focus on RBAC restrictions.
    """
    token = _create_test_jwt(
        roles=["platform_admin"],
        tenant_id=sample_tenant_id,
        email="admin@integration.test",
    )
    return {
        "Authorization": f"Bearer {token}",
        "Content-Type": "application/json",
        "X-Tenant-ID": sample_tenant_id,
    }


@pytest.fixture(scope="session")
def admin_jwt_headers(sample_tenant_id: str) -> dict[str, str]:
    """JWT header for the **Platform Admin** role (full access)."""
    token = _create_test_jwt(
        roles=["platform_admin"],
        tenant_id=sample_tenant_id,
        email="platform-admin@integration.test",
    )
    return {
        "Authorization": f"Bearer {token}",
        "Content-Type": "application/json",
        "X-Tenant-ID": sample_tenant_id,
    }


@pytest.fixture(scope="session")
def data_engineer_jwt_headers(sample_tenant_id: str) -> dict[str, str]:
    """JWT header for the **Data Engineer** role."""
    token = _create_test_jwt(
        roles=["data_engineer"],
        tenant_id=sample_tenant_id,
        email="data-engineer@integration.test",
    )
    return {
        "Authorization": f"Bearer {token}",
        "Content-Type": "application/json",
        "X-Tenant-ID": sample_tenant_id,
    }


@pytest.fixture(scope="session")
def developer_jwt_headers(sample_tenant_id: str) -> dict[str, str]:
    """JWT header for the **Developer** role."""
    token = _create_test_jwt(
        roles=["developer"],
        tenant_id=sample_tenant_id,
        email="developer@integration.test",
    )
    return {
        "Authorization": f"Bearer {token}",
        "Content-Type": "application/json",
        "X-Tenant-ID": sample_tenant_id,
    }


@pytest.fixture(scope="session")
def qa_engineer_jwt_headers(sample_tenant_id: str) -> dict[str, str]:
    """JWT header for the **QA Engineer** role."""
    token = _create_test_jwt(
        roles=["qa_engineer"],
        tenant_id=sample_tenant_id,
        email="qa-engineer@integration.test",
    )
    return {
        "Authorization": f"Bearer {token}",
        "Content-Type": "application/json",
        "X-Tenant-ID": sample_tenant_id,
    }


@pytest.fixture(scope="session")
def data_analyst_jwt_headers(sample_tenant_id: str) -> dict[str, str]:
    """JWT header for the **Data Analyst** role."""
    token = _create_test_jwt(
        roles=["data_analyst"],
        tenant_id=sample_tenant_id,
        email="data-analyst@integration.test",
    )
    return {
        "Authorization": f"Bearer {token}",
        "Content-Type": "application/json",
        "X-Tenant-ID": sample_tenant_id,
    }


@pytest.fixture(scope="session")
def secondary_tenant_headers(secondary_tenant_id: str) -> dict[str, str]:
    """JWT header scoped to the **secondary tenant** for isolation tests.

    Carries the ``platform_admin`` role so that permission checks do not
    interfere with tenant-isolation assertions.
    """
    token = _create_test_jwt(
        roles=["platform_admin"],
        tenant_id=secondary_tenant_id,
        email="admin@secondary-tenant.test",
    )
    return {
        "Authorization": f"Bearer {token}",
        "Content-Type": "application/json",
        "X-Tenant-ID": secondary_tenant_id,
    }


# ---------------------------------------------------------------------------
# 9. Test Data Seeding Fixtures
# ---------------------------------------------------------------------------

@pytest.fixture
def seed_schema_definition(
    schema_definitions_collection: pymongo.collection.Collection,
    sample_tenant_id: str,
) -> dict:
    """Insert a representative ``schema_definitions`` document and return it.

    The document describes a minimal SAP Financial Accounting schema with
    two tables (``GL_ACCOUNTS`` and ``JOURNAL_ENTRIES``) and one
    foreign-key relationship.
    """
    now = datetime.now(tz=UTC)
    schema_id = str(uuid.uuid4())
    doc: dict = {
        "schema_id": schema_id,
        "tenant_id": sample_tenant_id,
        "name": "SAP FI Module Schema",
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
                    {"name": "account_id", "data_type": "VARCHAR(20)", "nullable": False, "primary_key": True},
                    {"name": "account_name", "data_type": "VARCHAR(100)", "nullable": False, "primary_key": False},
                    {"name": "account_type", "data_type": "VARCHAR(20)", "nullable": False, "primary_key": False},
                    {"name": "currency_code", "data_type": "VARCHAR(3)", "nullable": False, "primary_key": False},
                    {"name": "is_active", "data_type": "BOOLEAN", "nullable": False, "primary_key": False},
                ],
            },
            {
                "name": "JOURNAL_ENTRIES",
                "columns": [
                    {"name": "entry_id", "data_type": "VARCHAR(36)", "nullable": False, "primary_key": True},
                    {"name": "account_id", "data_type": "VARCHAR(20)", "nullable": False, "primary_key": False},
                    {"name": "posting_date", "data_type": "DATE", "nullable": False, "primary_key": False},
                    {"name": "amount", "data_type": "DECIMAL(18,2)", "nullable": False, "primary_key": False},
                    {"name": "currency_code", "data_type": "VARCHAR(3)", "nullable": False, "primary_key": False},
                    {"name": "description", "data_type": "VARCHAR(255)", "nullable": True, "primary_key": False},
                ],
            },
        ],
        "relationships": [
            {
                "name": "fk_je_account",
                "source_table": "JOURNAL_ENTRIES",
                "source_column": "account_id",
                "target_table": "GL_ACCOUNTS",
                "target_column": "account_id",
                "relationship_type": "many_to_one",
            },
        ],
        "metadata": {
            "total_tables": 2,
            "total_columns": 11,
            "total_relationships": 1,
        },
    }
    schema_definitions_collection.insert_one(doc)
    return doc


@pytest.fixture
def seed_statistical_profile(
    statistical_profiles_collection: pymongo.collection.Collection,
    sample_tenant_id: str,
    seed_schema_definition: dict,
) -> dict:
    """Insert a representative ``statistical_profiles`` document.

    References the schema created by ``seed_schema_definition`` so that
    cross-collection integrity tests can verify the linkage.
    """
    now = datetime.now(tz=UTC)
    profile_id = str(uuid.uuid4())
    doc: dict = {
        "profile_id": profile_id,
        "tenant_id": sample_tenant_id,
        "schema_id": seed_schema_definition["schema_id"],
        "name": "SAP FI Profile",
        "created_at": now,
        "updated_at": now,
        "status": "completed",
        "table_profiles": [
            {
                "table_name": "GL_ACCOUNTS",
                "row_count": 1500,
                "column_profiles": [
                    {
                        "column_name": "account_id",
                        "data_type": "VARCHAR(20)",
                        "null_percentage": 0.0,
                        "unique_count": 1500,
                        "distribution_type": "uniform",
                        "min_value": "1000",
                        "max_value": "9999",
                    },
                    {
                        "column_name": "account_name",
                        "data_type": "VARCHAR(100)",
                        "null_percentage": 0.0,
                        "unique_count": 1500,
                        "distribution_type": "categorical",
                        "top_values": [
                            {"value": "Cash and Equivalents", "frequency": 0.05},
                            {"value": "Accounts Receivable", "frequency": 0.04},
                            {"value": "Inventory", "frequency": 0.03},
                        ],
                    },
                    {
                        "column_name": "account_type",
                        "data_type": "VARCHAR(20)",
                        "null_percentage": 0.0,
                        "unique_count": 5,
                        "distribution_type": "categorical",
                        "top_values": [
                            {"value": "asset", "frequency": 0.35},
                            {"value": "liability", "frequency": 0.25},
                            {"value": "equity", "frequency": 0.10},
                            {"value": "revenue", "frequency": 0.15},
                            {"value": "expense", "frequency": 0.15},
                        ],
                    },
                    {
                        "column_name": "currency_code",
                        "data_type": "VARCHAR(3)",
                        "null_percentage": 0.0,
                        "unique_count": 3,
                        "distribution_type": "categorical",
                        "top_values": [
                            {"value": "USD", "frequency": 0.70},
                            {"value": "EUR", "frequency": 0.20},
                            {"value": "GBP", "frequency": 0.10},
                        ],
                    },
                    {
                        "column_name": "is_active",
                        "data_type": "BOOLEAN",
                        "null_percentage": 0.0,
                        "unique_count": 2,
                        "distribution_type": "bernoulli",
                        "true_percentage": 0.92,
                    },
                ],
            },
            {
                "table_name": "JOURNAL_ENTRIES",
                "row_count": 500000,
                "column_profiles": [
                    {
                        "column_name": "entry_id",
                        "data_type": "VARCHAR(36)",
                        "null_percentage": 0.0,
                        "unique_count": 500000,
                        "distribution_type": "uuid",
                    },
                    {
                        "column_name": "account_id",
                        "data_type": "VARCHAR(20)",
                        "null_percentage": 0.0,
                        "unique_count": 1500,
                        "distribution_type": "foreign_key",
                        "references": "GL_ACCOUNTS.account_id",
                    },
                    {
                        "column_name": "posting_date",
                        "data_type": "DATE",
                        "null_percentage": 0.0,
                        "unique_count": 365,
                        "distribution_type": "uniform",
                        "min_value": "2024-01-01",
                        "max_value": "2024-12-31",
                    },
                    {
                        "column_name": "amount",
                        "data_type": "DECIMAL(18,2)",
                        "null_percentage": 0.0,
                        "unique_count": 45000,
                        "distribution_type": "log_normal",
                        "mean": 5200.50,
                        "std_dev": 12000.75,
                        "min_value": 0.01,
                        "max_value": 999999.99,
                    },
                    {
                        "column_name": "currency_code",
                        "data_type": "VARCHAR(3)",
                        "null_percentage": 0.0,
                        "unique_count": 3,
                        "distribution_type": "categorical",
                        "top_values": [
                            {"value": "USD", "frequency": 0.70},
                            {"value": "EUR", "frequency": 0.20},
                            {"value": "GBP", "frequency": 0.10},
                        ],
                    },
                    {
                        "column_name": "description",
                        "data_type": "VARCHAR(255)",
                        "null_percentage": 0.12,
                        "unique_count": 200000,
                        "distribution_type": "text",
                        "avg_length": 45,
                    },
                ],
            },
        ],
    }
    statistical_profiles_collection.insert_one(doc)
    return doc


@pytest.fixture
def seed_generation_job(
    generation_profiles_collection: pymongo.collection.Collection,
    sample_tenant_id: str,
    seed_schema_definition: dict,
) -> dict:
    """Insert a representative ``generation_profiles`` document (generation job).

    The job references the schema from ``seed_schema_definition`` and is
    seeded in ``completed`` status to enable downstream tests that read
    finished jobs.
    """
    now = datetime.now(tz=UTC)
    job_id = str(uuid.uuid4())
    doc: dict = {
        "job_id": job_id,
        "tenant_id": sample_tenant_id,
        "name": "SAP FI Integration Test Job",
        "schema_id": seed_schema_definition["schema_id"],
        "status": "completed",
        "generation_method": "statistical",
        "record_count": 10000,
        "output_format": "csv",
        "created_at": now - timedelta(hours=2),
        "updated_at": now,
        "started_at": now - timedelta(hours=2),
        "completed_at": now - timedelta(minutes=5),
        "parameters": {
            "batch_size": 10000,
            "preserve_distributions": True,
            "maintain_referential_integrity": True,
            "seed": 42,
        },
        "quality_score": 0.967,
        "quality_report": {
            "statistical_fidelity": 0.975,
            "business_rules_compliance": 0.960,
            "referential_integrity": 0.965,
            "weighted_score": 0.967,
        },
        "compliance": {
            "status": "certified",
            "pii_detected": False,
            "certificate_id": str(uuid.uuid4()),
            "certified_at": now - timedelta(minutes=10),
        },
        "output": {
            "format": "csv",
            "total_records": 10000,
            "total_tables": 2,
            "file_size_bytes": 2_450_000,
        },
        "created_by": "admin@integration.test",
    }
    generation_profiles_collection.insert_one(doc)
    return doc


@pytest.fixture
def seed_tenant_config(
    tenant_configurations_collection: pymongo.collection.Collection,
    sample_tenant_id: str,
) -> dict:
    """Insert a representative ``tenant_configurations`` document.

    Defines resource quotas, allowed ERP modules, and notification
    preferences for the primary integration-test tenant.
    """
    now = datetime.now(tz=UTC)
    doc: dict = {
        "tenant_id": sample_tenant_id,
        "name": "Integration Test Tenant",
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
            "notification_email": "admin@integration.test",
            "webhook_url": None,
        },
        "subscription": {
            "plan": "enterprise",
            "expires_at": now + timedelta(days=365),
        },
    }
    tenant_configurations_collection.insert_one(doc)
    return doc


@pytest.fixture
def seed_audit_logs(
    audit_logs_collection: pymongo.collection.Collection,
    sample_tenant_id: str,
) -> list[dict]:
    """Insert a set of tamper-evident ``audit_logs`` documents.

    Each log entry contains a SHA-256 hash computed over its serialized
    event data.  Subsequent entries incorporate the previous entry's hash
    to form a tamper-evident chain (SOC 2 Type II).  Returns the list of
    inserted documents.
    """
    now = datetime.now(tz=UTC)
    user_id = str(uuid.uuid4())
    actions = [
        ("generation.job.created", "Generation job created", {"job_id": str(uuid.uuid4()), "method": "statistical"}),
        ("generation.job.started", "Generation job started", {"job_id": str(uuid.uuid4()), "batch_size": 10000}),
        ("compliance.scan.completed", "PII scan completed", {"pii_detected": False, "scan_duration_ms": 1250}),
        ("generation.job.completed", "Generation job completed", {"job_id": str(uuid.uuid4()), "records": 10000}),
        ("export.file.created", "Export file created", {"format": "csv", "size_bytes": 2_450_000}),
    ]

    docs: list[dict] = []
    previous_hash: str = "0" * 64  # Genesis hash

    for idx, (action, description, details) in enumerate(actions):
        event_id = str(uuid.uuid4())
        timestamp = now - timedelta(minutes=len(actions) - idx)

        # Build the payload whose hash will be stored
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

        doc: dict = {
            "event_id": event_id,
            "tenant_id": sample_tenant_id,
            "user_id": user_id,
            "action": action,
            "description": description,
            "details": details,
            "timestamp": timestamp,
            "created_at": timestamp,
            "correlation_id": str(uuid.uuid4()),
            "ip_address": "10.0.0.1",
            "user_agent": "integration-test-runner/1.0",
            "previous_hash": previous_hash,
            "hash": current_hash,
        }
        docs.append(doc)
        previous_hash = current_hash

    audit_logs_collection.insert_many(docs)
    return docs


@pytest.fixture
def seed_test_data(
    seed_schema_definition: dict,
    seed_statistical_profile: dict,
    seed_generation_job: dict,
    seed_tenant_config: dict,
    seed_audit_logs: list[dict],
) -> dict:
    """Convenience fixture that seeds **all five** collections at once.

    Returns a dictionary keyed by collection/entity name pointing to the
    inserted documents.  Useful for tests that need a fully-populated
    data layer without specifying each seed fixture individually.
    """
    return {
        "schema_definition": seed_schema_definition,
        "statistical_profile": seed_statistical_profile,
        "generation_job": seed_generation_job,
        "tenant_config": seed_tenant_config,
        "audit_logs": seed_audit_logs,
    }


# ---------------------------------------------------------------------------
# 10. Sample Request Payload Fixtures
# ---------------------------------------------------------------------------

@pytest.fixture
def sample_generation_job_request(
    sample_tenant_id: str,
    seed_schema_definition: dict,
) -> dict:
    """Sample JSON payload for ``POST /api/v1/generation/jobs``.

    References the seeded schema to satisfy referential integrity at the
    API level.
    """
    return {
        "name": "Integration Test Generation Job",
        "schema_id": seed_schema_definition["schema_id"],
        "generation_method": "statistical",
        "record_count": 5000,
        "output_format": "csv",
        "parameters": {
            "batch_size": 5000,
            "preserve_distributions": True,
            "maintain_referential_integrity": True,
            "seed": 12345,
        },
        "tables": [
            {"name": "GL_ACCOUNTS", "record_count": 500},
            {"name": "JOURNAL_ENTRIES", "record_count": 4500},
        ],
    }


@pytest.fixture
def sample_profile_request(sample_tenant_id: str) -> dict:
    """Sample JSON payload for ``POST /api/v1/profiles``.

    Describes a profiling request targeting a SAP Financial Accounting
    source system.
    """
    return {
        "name": "SAP FI Profiling Request",
        "erp_system": "SAP",
        "erp_module": "financial_accounting",
        "connection": {
            "type": "jdbc",
            "host": "sap-test.integration.local",
            "port": 3306,
            "database": "SAPFI",
            "username": "profiler_ro",
            "password": "encrypted:placeholder",
        },
        "tables": ["GL_ACCOUNTS", "JOURNAL_ENTRIES"],
        "options": {
            "sample_size": 10000,
            "include_distributions": True,
            "include_patterns": True,
            "timeout_seconds": 300,
        },
    }


@pytest.fixture
def sample_template_request(
    sample_tenant_id: str,
    seed_schema_definition: dict,
) -> dict:
    """Sample JSON payload for ``POST /api/v1/templates``.

    Creates a reusable generation template linked to the seeded schema.
    """
    return {
        "name": "SAP FI Standard Template",
        "description": "Standard template for SAP Financial Accounting data generation",
        "schema_id": seed_schema_definition["schema_id"],
        "generation_method": "statistical",
        "default_record_count": 10000,
        "default_output_format": "csv",
        "parameters": {
            "batch_size": 10000,
            "preserve_distributions": True,
            "maintain_referential_integrity": True,
        },
        "table_configs": [
            {
                "name": "GL_ACCOUNTS",
                "default_record_count": 1500,
                "column_overrides": {},
            },
            {
                "name": "JOURNAL_ENTRIES",
                "default_record_count": 8500,
                "column_overrides": {
                    "amount": {"min_value": 0.01, "max_value": 500000.00},
                },
            },
        ],
        "tags": ["sap", "financial_accounting", "standard"],
        "is_public": False,
    }


@pytest.fixture
def sample_export_request(
    sample_tenant_id: str,
    seed_generation_job: dict,
) -> dict:
    """Sample JSON payload for ``POST /api/v1/export``.

    Requests CSV export of the seeded generation job's output to cloud
    storage.
    """
    return {
        "job_id": seed_generation_job["job_id"],
        "export_format": "csv",
        "destination": {
            "type": "cloud_storage",
            "provider": "aws_s3",
            "bucket": "synthetic-erp-exports-test",
            "prefix": f"integration-test/{sample_tenant_id}/",
            "region": "us-east-1",
        },
        "options": {
            "compression": "gzip",
            "include_headers": True,
            "delimiter": ",",
            "encoding": "utf-8",
            "encryption": {
                "enabled": True,
                "algorithm": "AES-256",
            },
        },
    }
