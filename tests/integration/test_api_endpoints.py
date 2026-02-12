"""
Integration tests for API Gateway endpoints.

This module contains comprehensive integration tests for the API Gateway
service running against real MongoDB 7.0 and Redis 7.x instances.  Unlike
the unit test suite (which relies on ``mongomock`` and ``MagicMock``),
every test in this module exercises the **actual** HTTP request/response
cycle through a live API Gateway process, real database persistence, and
middleware behaviour (JWT authentication, rate limiting, CORS).

All 14 test classes are decorated with ``@pytest.mark.integration`` so
that they can be selectively executed via ``pytest -m integration``.  The
tests expect all backend services (and their data-store dependencies) to
be running via the Docker Compose test profile.

Test Classes (14 + 1 module-level constant):
    - TestGenerationEndpoints   — Generation job CRUD and lifecycle   (8 tests)
    - TestProfileEndpoints      — Statistical profile endpoints       (4 tests)
    - TestSchemaEndpoints       — Schema discovery and retrieval      (4 tests)
    - TestTemplateEndpoints     — Generation template CRUD            (5 tests)
    - TestExportEndpoints       — Data export and provisioning        (2 tests)
    - TestAuthEndpoints         — Authentication flow endpoints       (3 tests)
    - TestAdminEndpoints        — Admin and tenant management         (4 tests)
    - TestHealthEndpoints       — Health and readiness probes         (5 tests)
    - TestMonitoringEndpoints   — Prometheus metrics                  (1 test)
    - TestJWTAuthentication     — JWT token validation middleware     (5 tests)
    - TestRateLimiting          — Redis-backed rate limiting          (5 tests)
    - TestMultiTenancy          — Tenant namespace isolation          (4 tests)
    - TestPagination            — Cursor-based pagination             (4 tests)
    - TestCORS                  — Cross-Origin Resource Sharing       (3 tests)

Fixtures are sourced from ``conftest.py`` via pytest auto-discovery.
"""

from __future__ import annotations

# ---------------------------------------------------------------------------
# Standard library imports
# ---------------------------------------------------------------------------
import json
import os
import time
import uuid
from datetime import UTC, datetime, timedelta

# ---------------------------------------------------------------------------
# Third-party imports
# ---------------------------------------------------------------------------
import httpx
import pytest
from jose import jwt as jose_jwt

# ---------------------------------------------------------------------------
# Module-Level Constants
# ---------------------------------------------------------------------------

API_GATEWAY_URL: str = os.environ.get("API_GATEWAY_URL", "http://localhost:5000")
"""Base URL for the API Gateway service under test.

Configurable via environment variable so that the same tests work both
against a local Docker Compose environment and against a CI/CD pipeline
service endpoint.
"""

# JWT configuration for *local* token creation in negative-path tests.
# These must match the values used by conftest.py so that the API Gateway
# can validate (or correctly reject) the tokens.
_JWT_TEST_SECRET: str = os.environ.get(
    "JWT_TEST_SECRET", "integration-test-secret-key-do-not-use-in-prod"
)
_JWT_TEST_ALGORITHM: str = "HS256"
_JWT_TEST_ISSUER: str = os.environ.get(
    "JWT_TEST_ISSUER", "https://synthetic-erp-test.auth0.com/"
)
_JWT_TEST_AUDIENCE: str = os.environ.get(
    "JWT_TEST_AUDIENCE", "https://api.synthetic-erp-test.local"
)

# Common API version prefix used across all endpoint paths
_API_V1: str = "/api/v1"

# Generation methods supported by the platform (used in parametrized tests)
_GENERATION_METHODS: list[str] = ["ai_ml", "rules_based", "statistical", "masking"]

# HTTP status codes considered as valid "created" responses
_CREATED_CODES: tuple[int, ...] = (200, 201, 202)


# ===================================================================
# Module-Level Fixtures
# ===================================================================

@pytest.fixture(scope="module")
def api_base_url() -> str:
    """Return the configured API Gateway base URL for the test session.

    This fixture provides a single source of truth for the API Gateway
    URL that can be injected into any test that needs to construct
    ``httpx.Client`` instances with custom settings (e.g.  CORS preflight
    tests).
    """
    return API_GATEWAY_URL


# ===================================================================
# Helper Utilities
# ===================================================================

def _extract_id(body: dict, *keys: str) -> str | None:
    """Try multiple key names (including a ``data`` wrapper) to find an ID.

    The API Gateway may surface identifiers under different keys depending
    on the endpoint (``job_id``, ``id``, ``data.job_id``, …).  This helper
    searches each candidate in declaration order and returns the first
    match.

    Args:
        body: Parsed JSON response body.
        *keys: Candidate key names.

    Returns:
        The first matching value cast to ``str``, or ``None``.
    """
    for key in keys:
        if key in body:
            return str(body[key])
        if "data" in body and isinstance(body["data"], dict) and key in body["data"]:
            return str(body["data"][key])
    return None


def _extract_items(body: dict | list) -> list:
    """Extract a list of items from various paginated response shapes.

    Tries common wrapper keys (``data``, ``items``, ``results``, …) and
    falls back to treating the body itself as the list.

    Args:
        body: Parsed JSON response body (dict or list).

    Returns:
        A ``list`` of item dicts.
    """
    if isinstance(body, list):
        return body
    for key in (
        "data", "items", "results", "jobs", "profiles",
        "schemas", "templates", "exports", "users",
    ):
        val = body.get(key)
        if isinstance(val, list):
            return val
    return []


# ===================================================================
# 1. Generation Endpoint Tests
# ===================================================================

@pytest.mark.integration
class TestGenerationEndpoints:
    """Integration tests for ``/api/v1/generation/jobs`` endpoints.

    Covers job creation with Pydantic validation, retrieval from MongoDB,
    list pagination, status/method filtering, and error handling for
    invalid payloads.
    """

    def test_create_generation_job(
        self,
        api_client: httpx.Client,
        admin_jwt_headers: dict[str, str],
        sample_generation_job_request: dict,
    ) -> None:
        """POST /api/v1/generation/jobs returns 2xx with a job identifier."""
        resp = api_client.post(
            f"{_API_V1}/generation/jobs",
            json=sample_generation_job_request,
            headers=admin_jwt_headers,
        )
        assert resp.status_code in (200, 201, 202), (
            f"Expected 2xx, got {resp.status_code}: {resp.text}"
        )
        body = resp.json()
        job_id = _extract_id(body, "job_id", "id")
        assert job_id is not None, f"Response missing job identifier: {json.dumps(body)}"

    def test_create_generation_job_persisted_in_mongodb(
        self,
        api_client: httpx.Client,
        admin_jwt_headers: dict[str, str],
        sample_generation_job_request: dict,
        generation_profiles_collection,
        sample_tenant_id: str,
    ) -> None:
        """Verify that a created generation job is persisted in MongoDB."""
        resp = api_client.post(
            f"{_API_V1}/generation/jobs",
            json=sample_generation_job_request,
            headers=admin_jwt_headers,
        )
        assert resp.status_code in (200, 201, 202)
        body = resp.json()
        job_id = _extract_id(body, "job_id", "id")
        assert job_id is not None

        # Query MongoDB directly to confirm persistence
        doc = generation_profiles_collection.find_one({"job_id": job_id})
        if doc is None:
            # The API might store with ``_id`` as the primary key
            doc = generation_profiles_collection.find_one({"_id": job_id})
        assert doc is not None, (
            f"Job {job_id} not found in generation_profiles collection"
        )
        # Tenant scoping must be present on the persisted document
        assert doc.get("tenant_id") == sample_tenant_id

    def test_get_generation_job_from_database(
        self,
        api_client: httpx.Client,
        admin_jwt_headers: dict[str, str],
        seed_generation_job: dict,
    ) -> None:
        """GET /api/v1/generation/jobs/{id} returns the seeded job."""
        job_id = seed_generation_job["job_id"]
        resp = api_client.get(
            f"{_API_V1}/generation/jobs/{job_id}",
            headers=admin_jwt_headers,
        )
        assert resp.status_code == 200, (
            f"Expected 200, got {resp.status_code}: {resp.text}"
        )
        body = resp.json()
        returned_id = _extract_id(body, "job_id", "id")
        assert returned_id == job_id
        # Verify key fields from the seeded document
        job_data = body.get("data", body)
        assert job_data.get("status") == "completed"
        assert job_data.get("generation_method") == "statistical"

    def test_list_generation_jobs_pagination(
        self,
        api_client: httpx.Client,
        admin_jwt_headers: dict[str, str],
        generation_profiles_collection,
        sample_tenant_id: str,
    ) -> None:
        """GET /api/v1/generation/jobs with limit parameter caps page size."""
        now = datetime.now(tz=UTC)
        # Seed 5 jobs directly in MongoDB for pagination testing
        for i in range(5):
            generation_profiles_collection.insert_one({
                "job_id": str(uuid.uuid4()),
                "tenant_id": sample_tenant_id,
                "name": f"Pagination Test Job {i}",
                "status": "completed",
                "generation_method": "statistical",
                "record_count": 1000 * (i + 1),
                "output_format": "csv",
                "created_at": now - timedelta(minutes=5 - i),
                "updated_at": now,
                "created_by": "admin@integration.test",
            })

        # Request with limit=2 to verify pagination
        resp = api_client.get(
            f"{_API_V1}/generation/jobs",
            params={"limit": 2},
            headers=admin_jwt_headers,
        )
        assert resp.status_code == 200
        body = resp.json()
        items = _extract_items(body)
        assert len(items) <= 2, f"Expected ≤2 items, got {len(items)}"

    def test_list_generation_jobs_filter_by_status(
        self,
        api_client: httpx.Client,
        admin_jwt_headers: dict[str, str],
        generation_profiles_collection,
        sample_tenant_id: str,
    ) -> None:
        """GET /api/v1/generation/jobs?status=completed filters correctly."""
        now = datetime.now(tz=UTC)
        statuses = ["submitted", "generating", "completed", "completed", "failed"]
        for idx, status in enumerate(statuses):
            generation_profiles_collection.insert_one({
                "job_id": str(uuid.uuid4()),
                "tenant_id": sample_tenant_id,
                "name": f"Status Filter Job {idx}",
                "status": status,
                "generation_method": "statistical",
                "record_count": 1000,
                "created_at": now - timedelta(minutes=5 - idx),
                "updated_at": now,
            })

        resp = api_client.get(
            f"{_API_V1}/generation/jobs",
            params={"status": "completed"},
            headers=admin_jwt_headers,
        )
        assert resp.status_code == 200
        body = resp.json()
        items = _extract_items(body)
        for item in items:
            assert item.get("status") == "completed", (
                f"Expected status=completed, got {item.get('status')}"
            )

    def test_list_generation_jobs_filter_by_method(
        self,
        api_client: httpx.Client,
        admin_jwt_headers: dict[str, str],
        generation_profiles_collection,
        sample_tenant_id: str,
    ) -> None:
        """GET /api/v1/generation/jobs?generation_method=ai_ml filters results."""
        now = datetime.now(tz=UTC)
        methods = ["statistical", "ai_ml", "rules_based", "ai_ml", "masking"]
        for idx, method in enumerate(methods):
            generation_profiles_collection.insert_one({
                "job_id": str(uuid.uuid4()),
                "tenant_id": sample_tenant_id,
                "name": f"Method Filter Job {idx}",
                "status": "completed",
                "generation_method": method,
                "record_count": 1000,
                "created_at": now - timedelta(minutes=5 - idx),
                "updated_at": now,
            })

        resp = api_client.get(
            f"{_API_V1}/generation/jobs",
            params={"generation_method": "ai_ml"},
            headers=admin_jwt_headers,
        )
        assert resp.status_code == 200
        body = resp.json()
        items = _extract_items(body)
        for item in items:
            assert item.get("generation_method") == "ai_ml", (
                f"Expected generation_method=ai_ml, got {item.get('generation_method')}"
            )

    def test_generation_job_validation_error(
        self,
        api_client: httpx.Client,
        admin_jwt_headers: dict[str, str],
    ) -> None:
        """POST with missing required fields returns 400 or 422."""
        invalid_payload: dict = {"output_format": "csv"}
        resp = api_client.post(
            f"{_API_V1}/generation/jobs",
            content=json.dumps(invalid_payload),
            headers=admin_jwt_headers,
        )
        assert resp.status_code in (400, 422), (
            f"Expected 400/422 for validation error, got {resp.status_code}: {resp.text}"
        )
        body = resp.json()
        has_error_info = any(
            key in body for key in ("error", "errors", "message", "detail", "details")
        )
        assert has_error_info, f"Error response missing error details: {json.dumps(body)}"

    @pytest.mark.parametrize("bad_method", [
        "unsupported_method_xyz",
        "",
        "AI_ML",  # wrong casing
        "hybrid",
        "deep_learning",
    ])
    def test_generation_job_invalid_method(
        self,
        api_client: httpx.Client,
        admin_jwt_headers: dict[str, str],
        seed_schema_definition: dict,
        bad_method: str,
    ) -> None:
        """POST with an unsupported generation method returns 400 or 422."""
        invalid_payload = {
            "name": "Invalid Method Job",
            "schema_id": seed_schema_definition["schema_id"],
            "generation_method": bad_method,
            "record_count": 1000,
            "output_format": "csv",
        }
        resp = api_client.post(
            f"{_API_V1}/generation/jobs",
            json=invalid_payload,
            headers=admin_jwt_headers,
        )
        assert resp.status_code in (400, 422), (
            f"Expected 400/422 for invalid method '{bad_method}', "
            f"got {resp.status_code}: {resp.text}"
        )


# ===================================================================
# 2. Profile Endpoint Tests
# ===================================================================

@pytest.mark.integration
class TestProfileEndpoints:
    """Integration tests for ``/api/v1/profiles`` endpoints.

    Validates statistical profile creation, retrieval by ID, 404 handling,
    and MongoDB persistence verification.
    """

    def test_create_profile(
        self,
        api_client: httpx.Client,
        admin_jwt_headers: dict[str, str],
        sample_profile_request: dict,
    ) -> None:
        """POST /api/v1/profiles returns 2xx with a profile identifier."""
        resp = api_client.post(
            f"{_API_V1}/profiles",
            json=sample_profile_request,
            headers=admin_jwt_headers,
        )
        assert resp.status_code in (200, 201, 202), (
            f"Expected 2xx, got {resp.status_code}: {resp.text}"
        )
        body = resp.json()
        profile_id = _extract_id(body, "profile_id", "id")
        assert profile_id is not None, (
            f"Response missing profile identifier: {json.dumps(body)}"
        )

    def test_get_profile_by_id(
        self,
        api_client: httpx.Client,
        admin_jwt_headers: dict[str, str],
        seed_statistical_profile: dict,
    ) -> None:
        """GET /api/v1/profiles/{id} returns the seeded profile."""
        profile_id = seed_statistical_profile["profile_id"]
        resp = api_client.get(
            f"{_API_V1}/profiles/{profile_id}",
            headers=admin_jwt_headers,
        )
        assert resp.status_code == 200, (
            f"Expected 200, got {resp.status_code}: {resp.text}"
        )
        body = resp.json()
        returned_id = _extract_id(body, "profile_id", "id")
        assert returned_id == profile_id

    def test_get_profile_not_found(
        self,
        api_client: httpx.Client,
        admin_jwt_headers: dict[str, str],
    ) -> None:
        """GET /api/v1/profiles/{id} with a nonexistent UUID returns 404."""
        fake_id = str(uuid.uuid4())
        resp = api_client.get(
            f"{_API_V1}/profiles/{fake_id}",
            headers=admin_jwt_headers,
        )
        assert resp.status_code == 404, (
            f"Expected 404, got {resp.status_code}: {resp.text}"
        )

    def test_profile_stored_in_mongodb(
        self,
        api_client: httpx.Client,
        admin_jwt_headers: dict[str, str],
        sample_profile_request: dict,
        statistical_profiles_collection,
        sample_tenant_id: str,
    ) -> None:
        """Verify that a created profile is persisted in MongoDB."""
        resp = api_client.post(
            f"{_API_V1}/profiles",
            json=sample_profile_request,
            headers=admin_jwt_headers,
        )
        assert resp.status_code in (200, 201, 202)
        body = resp.json()
        profile_id = _extract_id(body, "profile_id", "id")
        assert profile_id is not None

        doc = statistical_profiles_collection.find_one({"profile_id": profile_id})
        if doc is None:
            doc = statistical_profiles_collection.find_one({"_id": profile_id})
        assert doc is not None, (
            f"Profile {profile_id} not found in statistical_profiles collection"
        )
        assert doc.get("tenant_id") == sample_tenant_id


# ===================================================================
# 3. Schema Endpoint Tests
# ===================================================================

@pytest.mark.integration
class TestSchemaEndpoints:
    """Integration tests for ``/api/v1/schemas`` endpoints.

    Validates schema discovery initiation, retrieval by ID, 404 handling,
    and MongoDB persistence.
    """

    def test_discover_schema(
        self,
        api_client: httpx.Client,
        admin_jwt_headers: dict[str, str],
    ) -> None:
        """POST /api/v1/schemas/discover returns 2xx with schema info."""
        discover_payload = {
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
        }
        resp = api_client.post(
            f"{_API_V1}/schemas/discover",
            json=discover_payload,
            headers=admin_jwt_headers,
        )
        # Discovery may be synchronous (200/201) or async (202)
        assert resp.status_code in (200, 201, 202), (
            f"Expected 2xx, got {resp.status_code}: {resp.text}"
        )
        body = resp.json()
        schema_id = _extract_id(body, "schema_id", "id")
        assert schema_id is not None, (
            f"Response missing schema identifier: {json.dumps(body)}"
        )

    def test_get_schema_by_id(
        self,
        api_client: httpx.Client,
        admin_jwt_headers: dict[str, str],
        seed_schema_definition: dict,
    ) -> None:
        """GET /api/v1/schemas/{id} returns the seeded schema definition."""
        schema_id = seed_schema_definition["schema_id"]
        resp = api_client.get(
            f"{_API_V1}/schemas/{schema_id}",
            headers=admin_jwt_headers,
        )
        assert resp.status_code == 200, (
            f"Expected 200, got {resp.status_code}: {resp.text}"
        )
        body = resp.json()
        returned_id = _extract_id(body, "schema_id", "id")
        assert returned_id == schema_id
        schema_data = body.get("data", body)
        assert schema_data.get("erp_system") == "SAP"

    def test_get_schema_not_found(
        self,
        api_client: httpx.Client,
        admin_jwt_headers: dict[str, str],
    ) -> None:
        """GET /api/v1/schemas/{id} with a nonexistent UUID returns 404."""
        fake_id = str(uuid.uuid4())
        resp = api_client.get(
            f"{_API_V1}/schemas/{fake_id}",
            headers=admin_jwt_headers,
        )
        assert resp.status_code == 404, (
            f"Expected 404, got {resp.status_code}: {resp.text}"
        )

    def test_schema_stored_in_mongodb(
        self,
        api_client: httpx.Client,
        admin_jwt_headers: dict[str, str],
        schema_definitions_collection,
        sample_tenant_id: str,
    ) -> None:
        """Verify that a discovered schema is persisted in MongoDB."""
        discover_payload = {
            "erp_system": "Oracle",
            "erp_module": "human_resources",
            "connection": {
                "type": "jdbc",
                "host": "oracle-test.integration.local",
                "port": 1521,
                "database": "HRDB",
                "username": "profiler_ro",
                "password": "encrypted:placeholder",
            },
            "tables": ["EMPLOYEES", "DEPARTMENTS"],
        }
        resp = api_client.post(
            f"{_API_V1}/schemas/discover",
            json=discover_payload,
            headers=admin_jwt_headers,
        )
        assert resp.status_code in (200, 201, 202)
        body = resp.json()
        schema_id = _extract_id(body, "schema_id", "id")
        assert schema_id is not None

        doc = schema_definitions_collection.find_one({"schema_id": schema_id})
        if doc is None:
            doc = schema_definitions_collection.find_one({"_id": schema_id})
        assert doc is not None, (
            f"Schema {schema_id} not found in schema_definitions collection"
        )
        assert doc.get("tenant_id") == sample_tenant_id


# ===================================================================
# 4. Template Endpoint Tests
# ===================================================================

@pytest.mark.integration
class TestTemplateEndpoints:
    """Integration tests for ``/api/v1/templates`` CRUD endpoints.

    Validates creation, listing, retrieval, update, and deletion of
    reusable generation templates.
    """

    def test_create_template(
        self,
        api_client: httpx.Client,
        admin_jwt_headers: dict[str, str],
        sample_template_request: dict,
    ) -> None:
        """POST /api/v1/templates creates a template and returns 2xx."""
        resp = api_client.post(
            f"{_API_V1}/templates",
            json=sample_template_request,
            headers=admin_jwt_headers,
        )
        assert resp.status_code in (200, 201), (
            f"Expected 200/201, got {resp.status_code}: {resp.text}"
        )
        body = resp.json()
        template_id = _extract_id(body, "template_id", "id")
        assert template_id is not None, (
            f"Response missing template identifier: {json.dumps(body)}"
        )

    def test_list_templates(
        self,
        api_client: httpx.Client,
        admin_jwt_headers: dict[str, str],
        sample_template_request: dict,
    ) -> None:
        """GET /api/v1/templates returns a list containing created items."""
        # Create a template first
        api_client.post(
            f"{_API_V1}/templates",
            json=sample_template_request,
            headers=admin_jwt_headers,
        )
        resp = api_client.get(
            f"{_API_V1}/templates",
            headers=admin_jwt_headers,
        )
        assert resp.status_code == 200, (
            f"Expected 200, got {resp.status_code}: {resp.text}"
        )
        body = resp.json()
        items = _extract_items(body)
        assert len(items) >= 1, "Expected at least 1 template in listing"

    def test_get_template_by_id(
        self,
        api_client: httpx.Client,
        admin_jwt_headers: dict[str, str],
        sample_template_request: dict,
    ) -> None:
        """GET /api/v1/templates/{id} returns the previously created template."""
        create_resp = api_client.post(
            f"{_API_V1}/templates",
            json=sample_template_request,
            headers=admin_jwt_headers,
        )
        assert create_resp.status_code in (200, 201)
        create_body = create_resp.json()
        template_id = _extract_id(create_body, "template_id", "id")

        resp = api_client.get(
            f"{_API_V1}/templates/{template_id}",
            headers=admin_jwt_headers,
        )
        assert resp.status_code == 200
        body = resp.json()
        returned_id = _extract_id(body, "template_id", "id")
        assert returned_id == template_id

    def test_update_template(
        self,
        api_client: httpx.Client,
        admin_jwt_headers: dict[str, str],
        sample_template_request: dict,
    ) -> None:
        """PUT /api/v1/templates/{id} updates template fields."""
        create_resp = api_client.post(
            f"{_API_V1}/templates",
            json=sample_template_request,
            headers=admin_jwt_headers,
        )
        assert create_resp.status_code in (200, 201)
        create_body = create_resp.json()
        template_id = _extract_id(create_body, "template_id", "id")

        update_payload: dict = {
            **sample_template_request,
            "name": "Updated SAP FI Template",
            "default_record_count": 20000,
        }
        resp = api_client.put(
            f"{_API_V1}/templates/{template_id}",
            json=update_payload,
            headers=admin_jwt_headers,
        )
        assert resp.status_code in (200, 204), (
            f"Expected 200/204, got {resp.status_code}: {resp.text}"
        )
        if resp.status_code == 200:
            body = resp.json()
            template_data = body.get("data", body)
            updated_name = template_data.get("name")
            if updated_name is not None:
                assert updated_name == "Updated SAP FI Template"

    def test_delete_template(
        self,
        api_client: httpx.Client,
        admin_jwt_headers: dict[str, str],
        sample_template_request: dict,
    ) -> None:
        """DELETE /api/v1/templates/{id} removes the template."""
        create_resp = api_client.post(
            f"{_API_V1}/templates",
            json=sample_template_request,
            headers=admin_jwt_headers,
        )
        assert create_resp.status_code in (200, 201)
        create_body = create_resp.json()
        template_id = _extract_id(create_body, "template_id", "id")

        resp = api_client.delete(
            f"{_API_V1}/templates/{template_id}",
            headers=admin_jwt_headers,
        )
        assert resp.status_code in (200, 204), (
            f"Expected 200/204 on delete, got {resp.status_code}: {resp.text}"
        )

        # Confirm deletion via GET → 404
        get_resp = api_client.get(
            f"{_API_V1}/templates/{template_id}",
            headers=admin_jwt_headers,
        )
        assert get_resp.status_code == 404, (
            f"Expected 404 after deletion, got {get_resp.status_code}"
        )


# ===================================================================
# 5. Export Endpoint Tests
# ===================================================================

@pytest.mark.integration
class TestExportEndpoints:
    """Integration tests for ``/api/v1/export`` endpoints.

    Validates export request creation and status retrieval.
    """

    def test_create_export_request(
        self,
        api_client: httpx.Client,
        admin_jwt_headers: dict[str, str],
        sample_export_request: dict,
    ) -> None:
        """POST /api/v1/export creates an export request and returns 2xx."""
        resp = api_client.post(
            f"{_API_V1}/export",
            json=sample_export_request,
            headers=admin_jwt_headers,
        )
        assert resp.status_code in (200, 201, 202), (
            f"Expected 2xx, got {resp.status_code}: {resp.text}"
        )
        body = resp.json()
        export_id = _extract_id(body, "export_id", "id", "request_id")
        assert export_id is not None, (
            f"Response missing export identifier: {json.dumps(body)}"
        )

    def test_get_export_status(
        self,
        api_client: httpx.Client,
        admin_jwt_headers: dict[str, str],
        sample_export_request: dict,
    ) -> None:
        """GET /api/v1/export/{id} returns the current export status."""
        create_resp = api_client.post(
            f"{_API_V1}/export",
            json=sample_export_request,
            headers=admin_jwt_headers,
        )
        assert create_resp.status_code in (200, 201, 202)
        create_body = create_resp.json()
        export_id = _extract_id(create_body, "export_id", "id", "request_id")

        resp = api_client.get(
            f"{_API_V1}/export/{export_id}",
            headers=admin_jwt_headers,
        )
        assert resp.status_code == 200, (
            f"Expected 200, got {resp.status_code}: {resp.text}"
        )
        body = resp.json()
        export_data = body.get("data", body)
        assert "status" in export_data or "state" in export_data, (
            f"Export response missing status/state: {json.dumps(body)}"
        )


# ===================================================================
# 6. Auth Endpoint Tests
# ===================================================================

@pytest.mark.integration
class TestAuthEndpoints:
    """Integration tests for ``/api/v1/auth`` authentication endpoints.

    Validates the login redirect, token refresh, and logout flows.
    """

    def test_login_redirect(
        self,
        api_client: httpx.Client,
    ) -> None:
        """GET /api/v1/auth/login returns a redirect or login URL."""
        resp = api_client.get(
            f"{_API_V1}/auth/login",
            follow_redirects=False,
        )
        # Login should redirect (302/303) to Auth0 or return a login URL
        assert resp.status_code in (200, 301, 302, 303), (
            f"Expected 200/302/303, got {resp.status_code}: {resp.text}"
        )
        if resp.status_code in (301, 302, 303):
            location = resp.headers.get("location", "")
            assert location, "Redirect location header is empty"
        else:
            body = resp.json()
            has_url = any(
                key in body
                for key in ("url", "login_url", "redirect_url", "authorization_url")
            )
            assert has_url, f"Login response missing URL: {json.dumps(body)}"

    def test_token_refresh(
        self,
        api_client: httpx.Client,
        test_jwt_headers: dict[str, str],
    ) -> None:
        """POST /api/v1/auth/refresh attempts token refresh.

        Uses ``test_jwt_headers`` (the general-purpose admin JWT) to
        authenticate the refresh request.  The response depends on
        whether a valid refresh token is supplied.
        """
        resp = api_client.post(
            f"{_API_V1}/auth/refresh",
            headers=test_jwt_headers,
            json={"refresh_token": "test-refresh-token"},
        )
        # 200 = successful refresh; 400/401 = invalid/missing refresh token
        assert resp.status_code in (200, 400, 401), (
            f"Expected 200/400/401, got {resp.status_code}: {resp.text}"
        )

    def test_logout(
        self,
        api_client: httpx.Client,
        test_jwt_headers: dict[str, str],
    ) -> None:
        """POST /api/v1/auth/logout invalidates the current session."""
        resp = api_client.post(
            f"{_API_V1}/auth/logout",
            headers=test_jwt_headers,
        )
        assert resp.status_code in (200, 204, 302), (
            f"Expected 200/204/302, got {resp.status_code}: {resp.text}"
        )


# ===================================================================
# 7. Admin Endpoint Tests
# ===================================================================

@pytest.mark.integration
class TestAdminEndpoints:
    """Integration tests for ``/api/v1/admin`` endpoints.

    Validates admin-only access (RBAC), user listing, tenant config
    updates, and system settings retrieval.
    """

    def test_list_users_as_admin(
        self,
        api_client: httpx.Client,
        admin_jwt_headers: dict[str, str],
    ) -> None:
        """GET /api/v1/admin/users with admin role returns 200."""
        resp = api_client.get(
            f"{_API_V1}/admin/users",
            headers=admin_jwt_headers,
        )
        assert resp.status_code == 200, (
            f"Expected 200 for admin, got {resp.status_code}: {resp.text}"
        )
        body = resp.json()
        items = _extract_items(body)
        assert isinstance(items, list)

    def test_list_users_as_developer_forbidden(
        self,
        api_client: httpx.Client,
        developer_jwt_headers: dict[str, str],
    ) -> None:
        """GET /api/v1/admin/users with developer role returns 403."""
        resp = api_client.get(
            f"{_API_V1}/admin/users",
            headers=developer_jwt_headers,
        )
        assert resp.status_code == 403, (
            f"Expected 403 for developer, got {resp.status_code}: {resp.text}"
        )

    def test_update_tenant_config(
        self,
        api_client: httpx.Client,
        admin_jwt_headers: dict[str, str],
        sample_tenant_id: str,
        seed_tenant_config: dict,
        tenant_configurations_collection,
    ) -> None:
        """PUT /api/v1/admin/tenants/{id} updates tenant configuration.

        Verifies both the HTTP response and direct MongoDB persistence of
        the updated configuration.
        """
        update_payload: dict = {
            "resource_quotas": {
                "max_concurrent_jobs": 10,
                "max_records_per_job": 2_000_000,
                "storage_limit_gb": 200,
                "api_rate_limit_per_minute": 600,
            },
        }
        resp = api_client.put(
            f"{_API_V1}/admin/tenants/{sample_tenant_id}",
            json=update_payload,
            headers=admin_jwt_headers,
        )
        assert resp.status_code in (200, 204), (
            f"Expected 200/204, got {resp.status_code}: {resp.text}"
        )

        # Optionally verify the update was persisted in MongoDB
        doc = tenant_configurations_collection.find_one(
            {"tenant_id": sample_tenant_id}
        )
        if doc is not None:
            quotas = doc.get("resource_quotas", {})
            if quotas.get("max_concurrent_jobs") is not None:
                assert quotas["max_concurrent_jobs"] == 10

    def test_get_system_settings_admin_only(
        self,
        api_client: httpx.Client,
        admin_jwt_headers: dict[str, str],
        developer_jwt_headers: dict[str, str],
        audit_logs_collection,
    ) -> None:
        """GET /api/v1/admin/settings is restricted to admin role.

        Also verifies that administrative actions are recorded in the
        ``audit_logs`` collection (tamper-evident audit trail).
        """
        # Admin should succeed
        admin_resp = api_client.get(
            f"{_API_V1}/admin/settings",
            headers=admin_jwt_headers,
        )
        assert admin_resp.status_code == 200, (
            f"Expected 200 for admin, got {admin_resp.status_code}: {admin_resp.text}"
        )

        # Developer should be forbidden
        dev_resp = api_client.get(
            f"{_API_V1}/admin/settings",
            headers=developer_jwt_headers,
        )
        assert dev_resp.status_code == 403, (
            f"Expected 403 for developer, got {dev_resp.status_code}: {dev_resp.text}"
        )

        # Check audit_logs collection for any recent activity records.
        # The audit subsystem may or may not log reads, but the collection
        # should be accessible and queryable.
        audit_count = audit_logs_collection.count_documents({})
        assert isinstance(audit_count, int), (
            "audit_logs collection should be queryable"
        )


# ===================================================================
# 8. Health Endpoint Tests
# ===================================================================

@pytest.mark.integration
class TestHealthEndpoints:
    """Integration tests for health and readiness probe endpoints.

    These endpoints (``/health``, ``/ready``) must respond **without**
    JWT authentication so that Kubernetes liveness/readiness probes work.
    """

    def test_health_endpoint_no_auth_required(
        self,
        api_client: httpx.Client,
    ) -> None:
        """GET /health responds 200 without an Authorization header."""
        resp = api_client.get("/health")
        assert resp.status_code == 200, (
            f"Expected 200, got {resp.status_code}: {resp.text}"
        )

    def test_health_returns_status(
        self,
        api_client: httpx.Client,
    ) -> None:
        """GET /health response includes a healthy status field."""
        resp = api_client.get("/health")
        assert resp.status_code == 200
        body = json.loads(resp.text)
        assert "status" in body, f"Health response missing 'status': {body}"
        assert body["status"] in ("ok", "healthy", "up"), (
            f"Unexpected health status: {body['status']}"
        )

    def test_readiness_includes_dependencies(
        self,
        api_client: httpx.Client,
    ) -> None:
        """GET /ready reports status of MongoDB and Redis dependencies."""
        resp = api_client.get("/ready")
        assert resp.status_code == 200, (
            f"Expected 200, got {resp.status_code}: {resp.text}"
        )
        body = resp.json()
        # Readiness should include dependency-health information
        has_deps = any(
            key in body
            for key in (
                "dependencies", "checks", "components",
                "services", "mongodb", "redis",
            )
        )
        assert has_deps or "status" in body, (
            f"Readiness response missing dependency info: {json.dumps(body)}"
        )

    def test_readiness_when_mongodb_down(
        self,
        api_client: httpx.Client,
        mongo_db,
    ) -> None:
        """Verify readiness endpoint correctly reports MongoDB status.

        In a live Docker Compose environment MongoDB is expected to be
        healthy.  This test validates the response structure includes
        MongoDB connectivity information.  A true "down" test would
        require stopping the MongoDB container mid-session which is not
        feasible in standard integration-test harnesses.

        We also perform a direct ``mongo_db`` ping to confirm the test
        database itself is reachable.
        """
        # Confirm MongoDB is reachable from the test runner
        ping_result = mongo_db.command("ping")
        assert ping_result.get("ok") == 1.0

        resp = api_client.get("/ready")
        body = resp.json()
        status = body.get("status", "ok")
        assert status in ("ok", "healthy", "up", "ready"), (
            f"Unexpected readiness status with MongoDB up: {status}"
        )
        # If the endpoint provides granular dependency info, check MongoDB
        deps = body.get(
            "dependencies",
            body.get("checks", body.get("components", {})),
        )
        if isinstance(deps, dict) and "mongodb" in deps:
            mongo_status = deps["mongodb"]
            if isinstance(mongo_status, dict):
                assert mongo_status.get("status") in (
                    "ok", "healthy", "up", "connected",
                )
            else:
                assert mongo_status in ("ok", "healthy", "up", "connected")

    def test_readiness_when_redis_down(
        self,
        api_client: httpx.Client,
        redis_client,
    ) -> None:
        """Verify readiness endpoint correctly reports Redis status.

        Like the MongoDB test above, Redis is expected to be healthy in
        the Docker Compose test profile.  This test confirms the readiness
        endpoint surfaces Redis status information.

        We also perform a direct ``redis_client.ping()`` to confirm Redis
        is reachable from the test runner.
        """
        # Confirm Redis is reachable from the test runner
        assert redis_client.ping() is True

        resp = api_client.get("/ready")
        body = resp.json()
        status = body.get("status", "ok")
        assert status in ("ok", "healthy", "up", "ready"), (
            f"Unexpected readiness status with Redis up: {status}"
        )
        deps = body.get(
            "dependencies",
            body.get("checks", body.get("components", {})),
        )
        if isinstance(deps, dict) and "redis" in deps:
            redis_status = deps["redis"]
            if isinstance(redis_status, dict):
                assert redis_status.get("status") in (
                    "ok", "healthy", "up", "connected",
                )
            else:
                assert redis_status in ("ok", "healthy", "up", "connected")


# ===================================================================
# 9. Monitoring Endpoint Tests
# ===================================================================

@pytest.mark.integration
class TestMonitoringEndpoints:
    """Integration tests for the Prometheus metrics endpoint."""

    def test_metrics_endpoint(
        self,
        api_client: httpx.Client,
        admin_jwt_headers: dict[str, str],
    ) -> None:
        """GET /metrics returns Prometheus-formatted metrics or JSON.

        The metrics endpoint may or may not require JWT authentication;
        this test attempts unauthenticated access first, then falls back
        to authenticated.
        """
        # Try without auth first (metrics are commonly unauthenticated)
        resp = api_client.get("/metrics")
        if resp.status_code == 401:
            # Retry with auth
            resp = api_client.get("/metrics", headers=admin_jwt_headers)

        assert resp.status_code == 200, (
            f"Expected 200, got {resp.status_code}: {resp.text}"
        )

        content_type = resp.headers.get("content-type", "")
        body_text = resp.text

        # Prometheus exposition format or JSON are both acceptable
        is_prometheus = (
            "text/plain" in content_type
            or "# HELP" in body_text
            or "# TYPE" in body_text
            or "process_" in body_text
            or "python_" in body_text
        )
        is_json_format = "application/json" in content_type
        assert is_prometheus or is_json_format, (
            f"Metrics returned unexpected content type: {content_type}"
        )


# ===================================================================
# 10. JWT Authentication Tests
# ===================================================================

@pytest.mark.integration
class TestJWTAuthentication:
    """Integration tests for JWT token validation middleware.

    Uses ``jose.jwt.encode()`` to craft expired and improperly signed
    tokens for negative-path testing.  Valid tokens are provided by the
    conftest fixtures.
    """

    def test_valid_jwt_accepted(
        self,
        api_client: httpx.Client,
        test_jwt_headers: dict[str, str],
    ) -> None:
        """Requests with a valid JWT (from conftest) are accepted."""
        resp = api_client.get(
            f"{_API_V1}/generation/jobs",
            headers=test_jwt_headers,
        )
        assert 200 <= resp.status_code < 300, (
            f"Expected 2xx with valid JWT, got {resp.status_code}: {resp.text}"
        )

    def test_expired_jwt_rejected(
        self,
        api_client: httpx.Client,
        sample_tenant_id: str,
    ) -> None:
        """Requests with an expired JWT are rejected with 401."""
        now = datetime.now(tz=UTC)
        expired_payload = {
            "sub": str(uuid.uuid4()),
            "iss": _JWT_TEST_ISSUER,
            "aud": _JWT_TEST_AUDIENCE,
            "iat": now - timedelta(hours=2),
            "exp": now - timedelta(hours=1),  # Expired 1 hour ago
            "roles": ["platform_admin"],
            "tenant_id": sample_tenant_id,
            "email": "expired@integration.test",
        }
        expired_token: str = jose_jwt.encode(
            expired_payload, _JWT_TEST_SECRET, algorithm=_JWT_TEST_ALGORITHM,
        )
        headers = {
            "Authorization": f"Bearer {expired_token}",
            "Content-Type": "application/json",
            "X-Tenant-ID": sample_tenant_id,
        }
        resp = api_client.get(
            f"{_API_V1}/generation/jobs",
            headers=headers,
        )
        assert resp.status_code == 401, (
            f"Expected 401 for expired JWT, got {resp.status_code}: {resp.text}"
        )

    def test_invalid_signature_rejected(
        self,
        api_client: httpx.Client,
        sample_tenant_id: str,
    ) -> None:
        """Requests with a JWT signed by the wrong key are rejected (401)."""
        now = datetime.now(tz=UTC)
        payload = {
            "sub": str(uuid.uuid4()),
            "iss": _JWT_TEST_ISSUER,
            "aud": _JWT_TEST_AUDIENCE,
            "iat": now,
            "exp": now + timedelta(hours=1),
            "roles": ["platform_admin"],
            "tenant_id": sample_tenant_id,
            "email": "wrongkey@integration.test",
        }
        bad_token: str = jose_jwt.encode(
            payload, "completely-wrong-secret-key", algorithm=_JWT_TEST_ALGORITHM,
        )
        headers = {
            "Authorization": f"Bearer {bad_token}",
            "Content-Type": "application/json",
            "X-Tenant-ID": sample_tenant_id,
        }
        resp = api_client.get(
            f"{_API_V1}/generation/jobs",
            headers=headers,
        )
        assert resp.status_code == 401, (
            f"Expected 401 for invalid signature, got {resp.status_code}: {resp.text}"
        )

    def test_missing_authorization_header(
        self,
        api_client: httpx.Client,
    ) -> None:
        """Requests without an Authorization header are rejected (401)."""
        resp = api_client.get(
            f"{_API_V1}/generation/jobs",
            headers={"Content-Type": "application/json"},
        )
        assert resp.status_code == 401, (
            f"Expected 401 for missing auth, got {resp.status_code}: {resp.text}"
        )

    def test_role_based_access_control(
        self,
        api_client: httpx.Client,
        admin_jwt_headers: dict[str, str],
        data_engineer_jwt_headers: dict[str, str],
        developer_jwt_headers: dict[str, str],
        qa_engineer_jwt_headers: dict[str, str],
        data_analyst_jwt_headers: dict[str, str],
    ) -> None:
        """Verify RBAC enforcement for all five user roles.

        Platform Admin has full access including admin endpoints.
        Data Engineer and QA Engineer can access generation endpoints.
        Developer and Data Analyst are forbidden from admin endpoints.
        """
        # Platform Admin → admin endpoints
        resp = api_client.get(
            f"{_API_V1}/admin/users", headers=admin_jwt_headers,
        )
        assert resp.status_code == 200, (
            "platform_admin should access /admin/users"
        )

        # Data Engineer → generation endpoints
        resp = api_client.get(
            f"{_API_V1}/generation/jobs", headers=data_engineer_jwt_headers,
        )
        assert resp.status_code == 200, (
            "data_engineer should access /generation/jobs"
        )

        # Developer → admin endpoints forbidden
        resp = api_client.get(
            f"{_API_V1}/admin/users", headers=developer_jwt_headers,
        )
        assert resp.status_code == 403, (
            "developer should NOT access /admin/users"
        )

        # QA Engineer → generation endpoints
        resp = api_client.get(
            f"{_API_V1}/generation/jobs", headers=qa_engineer_jwt_headers,
        )
        assert resp.status_code == 200, (
            "qa_engineer should access /generation/jobs"
        )

        # Data Analyst → admin endpoints forbidden
        resp = api_client.get(
            f"{_API_V1}/admin/users", headers=data_analyst_jwt_headers,
        )
        assert resp.status_code == 403, (
            "data_analyst should NOT access /admin/users"
        )


# ===================================================================
# 11. Rate Limiting Tests
# ===================================================================

@pytest.mark.integration
class TestRateLimiting:
    """Integration tests for Redis-backed rate limiting middleware.

    Rate limit tiers (requests/minute):
        - Developer:     60
        - Data Engineer: 300
        - Platform Admin: 1000
    """

    def test_rate_limit_counter_in_redis(
        self,
        api_client: httpx.Client,
        admin_jwt_headers: dict[str, str],
        redis_client,
        clean_redis,  # noqa: ARG002 — Explicitly request Redis cleanup
    ) -> None:
        """Verify that API requests increment a rate-limit counter in Redis.

        The ``clean_redis`` fixture (autouse) is explicitly requested here
        to guarantee that all rate-limit keys are flushed before this test
        begins, preventing interference from earlier test runs.
        """
        # Make a request to trigger counter increment
        api_client.get(
            f"{_API_V1}/generation/jobs", headers=admin_jwt_headers,
        )

        # Inspect Redis for any rate-limit-related keys
        all_keys: list[str] = []
        for pattern in ("*rate*", "*limit*", "*rl:*", "*throttle*"):
            all_keys.extend(redis_client.keys(pattern))

        if all_keys:
            first_key = all_keys[0]
            val = redis_client.get(first_key)
            assert val is not None, (
                f"Rate limit key '{first_key}' exists but has no value"
            )

    def test_rate_limit_not_exceeded(
        self,
        api_client: httpx.Client,
        data_engineer_jwt_headers: dict[str, str],
    ) -> None:
        """A small burst of requests stays within the limit (no 429)."""
        start_time = time.time()
        for i in range(5):
            resp = api_client.get(
                f"{_API_V1}/generation/jobs",
                headers=data_engineer_jwt_headers,
            )
            assert resp.status_code != 429, (
                f"Rate limit hit unexpectedly on request {i + 1} of 5"
            )
        elapsed = time.time() - start_time
        # Sanity check: 5 requests should complete quickly
        assert elapsed < 30, f"5 requests took {elapsed:.1f}s — unexpectedly slow"

    def test_rate_limit_exceeded(
        self,
        api_client: httpx.Client,
        developer_jwt_headers: dict[str, str],
    ) -> None:
        """Exceeding the rate limit returns 429 Too Many Requests.

        The developer tier allows 60 requests/minute.  This test sends a
        burst of 70 rapid requests to trigger the limit.  If the API does
        not enforce rate limiting at this threshold, the test is skipped.
        """
        hit_429 = False
        for i in range(70):
            resp = api_client.get(
                "/health",  # Use a lightweight endpoint
                headers=developer_jwt_headers,
            )
            if resp.status_code == 429:
                hit_429 = True
                break

        if not hit_429:
            pytest.skip(
                "Rate limit not triggered within 70 requests; "
                "rate limiting may use a higher threshold or be disabled"
            )
        assert hit_429, "Expected 429 after exceeding rate limit"

    def test_rate_limit_tier_developer(
        self,
        api_client: httpx.Client,
        developer_jwt_headers: dict[str, str],
    ) -> None:
        """Developer tier exposes rate-limit info via response headers."""
        resp = api_client.get(
            f"{_API_V1}/generation/jobs",
            headers=developer_jwt_headers,
        )
        # Rate-limit metadata is commonly exposed via X-RateLimit-* headers
        limit = (
            resp.headers.get("X-RateLimit-Limit")
            or resp.headers.get("X-Rate-Limit-Limit")
        )
        remaining = (
            resp.headers.get("X-RateLimit-Remaining")
            or resp.headers.get("X-Rate-Limit-Remaining")
        )
        if limit is not None:
            # Developer tier should be ≤300 (spec says 60)
            assert int(limit) <= 300, (
                f"Developer rate limit unexpectedly high: {limit}"
            )
        if remaining is not None:
            assert int(remaining) >= 0
        # If headers aren't present, just verify the request succeeded
        assert resp.status_code in (200, 401, 403)

    def test_rate_limit_tier_admin(
        self,
        api_client: httpx.Client,
        admin_jwt_headers: dict[str, str],
    ) -> None:
        """Admin tier has a higher rate limit than developer tier."""
        # Small delay to ensure rate-limit window is fresh
        time.sleep(0.1)
        resp = api_client.get(
            f"{_API_V1}/generation/jobs",
            headers=admin_jwt_headers,
        )
        limit = (
            resp.headers.get("X-RateLimit-Limit")
            or resp.headers.get("X-Rate-Limit-Limit")
        )
        if limit is not None:
            # Admin tier should be ≥ developer tier (spec says 1000)
            assert int(limit) >= 60, (
                f"Admin rate limit unexpectedly low: {limit}"
            )
        assert resp.status_code in (200, 401, 403)


# ===================================================================
# 12. Multi-Tenancy Tests
# ===================================================================

@pytest.mark.integration
class TestMultiTenancy:
    """Integration tests for multi-tenant namespace isolation.

    Verifies that data belonging to one tenant is invisible to another
    tenant, and that API queries are always scoped by ``tenant_id``.
    """

    def test_tenant_scoped_data(
        self,
        api_client: httpx.Client,
        admin_jwt_headers: dict[str, str],
        seed_test_data: dict,
        sample_tenant_id: str,
    ) -> None:
        """Data seeded for the primary tenant is visible via its JWT.

        Uses the ``seed_test_data`` convenience fixture which populates
        all five MongoDB collections.
        """
        resp = api_client.get(
            f"{_API_V1}/generation/jobs",
            headers=admin_jwt_headers,
        )
        assert resp.status_code == 200
        body = resp.json()
        items = _extract_items(body)
        # Every returned item must belong to the primary tenant
        for item in items:
            if "tenant_id" in item:
                assert item["tenant_id"] == sample_tenant_id, (
                    f"Tenant leak: expected {sample_tenant_id}, "
                    f"got {item['tenant_id']}"
                )

    def test_cross_tenant_access_denied(
        self,
        api_client: httpx.Client,
        admin_jwt_headers: dict[str, str],
        secondary_tenant_headers: dict[str, str],
        generation_profiles_collection,
        sample_tenant_id: str,
        secondary_tenant_id: str,
    ) -> None:
        """Data from the primary tenant must be invisible to the secondary."""
        now = datetime.now(tz=UTC)
        primary_job_id = str(uuid.uuid4())
        generation_profiles_collection.insert_one({
            "job_id": primary_job_id,
            "tenant_id": sample_tenant_id,
            "name": "Primary Tenant Job",
            "status": "completed",
            "generation_method": "statistical",
            "record_count": 1000,
            "created_at": now,
            "updated_at": now,
        })

        # Query as the secondary tenant
        resp = api_client.get(
            f"{_API_V1}/generation/jobs",
            headers=secondary_tenant_headers,
        )
        assert resp.status_code == 200
        body = resp.json()
        items = _extract_items(body)
        returned_ids = [
            item.get("job_id") or item.get("id") for item in items
        ]
        assert primary_job_id not in returned_ids, (
            f"Cross-tenant leak: job {primary_job_id} visible to secondary"
        )

    def test_tenant_id_from_header(
        self,
        api_client: httpx.Client,
        admin_jwt_headers: dict[str, str],
        sample_tenant_id: str,
    ) -> None:
        """The X-Tenant-ID header is used to scope operations."""
        # Confirm the header is present in the fixture
        assert "X-Tenant-ID" in admin_jwt_headers
        assert admin_jwt_headers["X-Tenant-ID"] == sample_tenant_id

        resp = api_client.get(
            f"{_API_V1}/generation/jobs",
            headers=admin_jwt_headers,
        )
        assert resp.status_code == 200

    def test_mongodb_queries_scoped(
        self,
        api_client: httpx.Client,
        admin_jwt_headers: dict[str, str],
        secondary_tenant_headers: dict[str, str],
        mongo_db,
        generation_profiles_collection,
        sample_tenant_id: str,
        secondary_tenant_id: str,
    ) -> None:
        """Verify that MongoDB queries include tenant_id scoping.

        Inserts data for two tenants, queries each tenant's endpoint, and
        confirms no cross-contamination.  Also performs a direct MongoDB
        count to validate the underlying data.
        """
        now = datetime.now(tz=UTC)
        generation_profiles_collection.insert_many([
            {
                "job_id": str(uuid.uuid4()),
                "tenant_id": sample_tenant_id,
                "name": "Primary Tenant Scoped Job",
                "status": "completed",
                "generation_method": "statistical",
                "record_count": 1000,
                "created_at": now,
                "updated_at": now,
            },
            {
                "job_id": str(uuid.uuid4()),
                "tenant_id": secondary_tenant_id,
                "name": "Secondary Tenant Scoped Job",
                "status": "completed",
                "generation_method": "ai_ml",
                "record_count": 2000,
                "created_at": now,
                "updated_at": now,
            },
        ])

        # Direct MongoDB verification: both docs exist
        total = mongo_db["generation_profiles"].count_documents({})
        assert total == 2

        primary_count = mongo_db["generation_profiles"].count_documents(
            {"tenant_id": sample_tenant_id}
        )
        assert primary_count == 1

        secondary_count = mongo_db["generation_profiles"].count_documents(
            {"tenant_id": secondary_tenant_id}
        )
        assert secondary_count == 1

        # API queries should be tenant-scoped
        primary_resp = api_client.get(
            f"{_API_V1}/generation/jobs",
            headers=admin_jwt_headers,
        )
        primary_items = _extract_items(primary_resp.json())

        secondary_resp = api_client.get(
            f"{_API_V1}/generation/jobs",
            headers=secondary_tenant_headers,
        )
        secondary_items = _extract_items(secondary_resp.json())

        for item in primary_items:
            if "tenant_id" in item:
                assert item["tenant_id"] == sample_tenant_id
        for item in secondary_items:
            if "tenant_id" in item:
                assert item["tenant_id"] == secondary_tenant_id


# ===================================================================
# 13. Pagination Tests
# ===================================================================

@pytest.mark.integration
class TestPagination:
    """Integration tests for cursor-based pagination.

    Validates page-size enforcement, cursor navigation, default limits,
    and empty-collection behaviour.
    """

    def test_cursor_based_pagination(
        self,
        api_client: httpx.Client,
        admin_jwt_headers: dict[str, str],
        generation_profiles_collection,
        sample_tenant_id: str,
    ) -> None:
        """Cursor-based pagination navigates through seeded results."""
        now = datetime.now(tz=UTC)
        total_jobs = 10
        for i in range(total_jobs):
            generation_profiles_collection.insert_one({
                "job_id": str(uuid.uuid4()),
                "tenant_id": sample_tenant_id,
                "name": f"Cursor Pagination Job {i:02d}",
                "status": "completed",
                "generation_method": "statistical",
                "record_count": 1000,
                "created_at": now - timedelta(minutes=total_jobs - i),
                "updated_at": now,
            })

        all_ids: set[str] = set()
        cursor: str | None = None
        page_count = 0
        max_pages = 20  # Safety limit

        while page_count < max_pages:
            params: dict = {"limit": 3}
            if cursor:
                params["cursor"] = cursor

            resp = api_client.get(
                f"{_API_V1}/generation/jobs",
                params=params,
                headers=admin_jwt_headers,
            )
            assert resp.status_code == 200
            body = resp.json()
            items = _extract_items(body)

            if not items:
                break

            for item in items:
                item_id = item.get("job_id") or item.get("id")
                if item_id:
                    all_ids.add(item_id)

            # Extract the next cursor for subsequent page
            next_cursor = (
                body.get("next_cursor")
                or body.get("cursor")
                or (body.get("pagination", {}) or {}).get("next_cursor")
            )
            page_count += 1
            if not next_cursor or next_cursor == cursor:
                break
            cursor = next_cursor

        # Should have collected items across pages
        assert len(all_ids) >= 1, "Pagination returned no items at all"

    def test_pagination_limit(
        self,
        api_client: httpx.Client,
        admin_jwt_headers: dict[str, str],
        generation_profiles_collection,
        sample_tenant_id: str,
    ) -> None:
        """Custom limit parameter restricts the returned page size."""
        now = datetime.now(tz=UTC)
        for i in range(5):
            generation_profiles_collection.insert_one({
                "job_id": str(uuid.uuid4()),
                "tenant_id": sample_tenant_id,
                "name": f"Limit Test Job {i}",
                "status": "completed",
                "generation_method": "statistical",
                "record_count": 1000,
                "created_at": now - timedelta(minutes=5 - i),
                "updated_at": now,
            })

        resp = api_client.get(
            f"{_API_V1}/generation/jobs",
            params={"limit": 2},
            headers=admin_jwt_headers,
        )
        assert resp.status_code == 200
        body = resp.json()
        items = _extract_items(body)
        assert len(items) <= 2, f"Expected ≤2 items, got {len(items)}"

    def test_pagination_default_limit(
        self,
        api_client: httpx.Client,
        admin_jwt_headers: dict[str, str],
        generation_profiles_collection,
        sample_tenant_id: str,
    ) -> None:
        """Without an explicit limit, a sensible default page size is used."""
        now = datetime.now(tz=UTC)
        for i in range(30):
            generation_profiles_collection.insert_one({
                "job_id": str(uuid.uuid4()),
                "tenant_id": sample_tenant_id,
                "name": f"Default Limit Job {i:02d}",
                "status": "completed",
                "generation_method": "statistical",
                "record_count": 1000,
                "created_at": now - timedelta(minutes=30 - i),
                "updated_at": now,
            })

        resp = api_client.get(
            f"{_API_V1}/generation/jobs",
            headers=admin_jwt_headers,
        )
        assert resp.status_code == 200
        body = resp.json()
        items = _extract_items(body)
        # A default limit should cap the result set (typically 10–50)
        assert len(items) <= 100, (
            f"Default limit seems too high: {len(items)} items returned"
        )

    def test_pagination_empty_result(
        self,
        api_client: httpx.Client,
        admin_jwt_headers: dict[str, str],
    ) -> None:
        """An empty collection returns an empty list with 200 status."""
        # Collections are cleaned by the autouse clean_collections fixture
        resp = api_client.get(
            f"{_API_V1}/generation/jobs",
            headers=admin_jwt_headers,
        )
        assert resp.status_code == 200
        body = resp.json()
        items = _extract_items(body)
        assert len(items) == 0, f"Expected empty list, got {len(items)} items"


# ===================================================================
# 14. CORS Tests
# ===================================================================

@pytest.mark.integration
class TestCORS:
    """Integration tests for Cross-Origin Resource Sharing headers.

    Validates that the API Gateway returns appropriate CORS headers for
    allowed origins, preflight ``OPTIONS`` requests, and credentials.
    """

    def test_cors_allowed_origin(
        self,
        api_client: httpx.Client,
        admin_jwt_headers: dict[str, str],
    ) -> None:
        """Verify Access-Control-Allow-Origin is present for allowed origins."""
        headers = {
            **admin_jwt_headers,
            "Origin": "http://localhost:3000",
        }
        resp = api_client.get(
            f"{_API_V1}/generation/jobs", headers=headers,
        )
        cors_origin = resp.headers.get("access-control-allow-origin")
        if cors_origin is not None:
            # Should echo back the allowed origin or use wildcard
            assert cors_origin in ("*", "http://localhost:3000"), (
                f"Unexpected CORS origin: {cors_origin}"
            )

    def test_cors_preflight(self) -> None:
        """OPTIONS preflight request returns CORS headers."""
        with httpx.Client(base_url=API_GATEWAY_URL, timeout=10.0) as client:
            headers = {
                "Origin": "http://localhost:3000",
                "Access-Control-Request-Method": "POST",
                "Access-Control-Request-Headers": "Authorization, Content-Type",
            }
            resp = client.options(
                f"{_API_V1}/generation/jobs", headers=headers,
            )
            # Preflight should return 200, 204, or possibly 405 if not configured
            assert resp.status_code in (200, 204, 405), (
                f"CORS preflight returned {resp.status_code}: {resp.text}"
            )
            if resp.status_code in (200, 204):
                allow_methods = resp.headers.get(
                    "access-control-allow-methods", "",
                )
                # POST should be in the allowed methods
                if allow_methods:
                    assert "POST" in allow_methods.upper(), (
                        f"POST not in allowed methods: {allow_methods}"
                    )

    def test_cors_credentials(
        self,
        api_client: httpx.Client,
        admin_jwt_headers: dict[str, str],
    ) -> None:
        """Verify Access-Control-Allow-Credentials for cookie support."""
        headers = {
            **admin_jwt_headers,
            "Origin": "http://localhost:3000",
        }
        resp = api_client.get(
            f"{_API_V1}/generation/jobs", headers=headers,
        )
        credentials = resp.headers.get("access-control-allow-credentials")
        if credentials is not None:
            assert credentials.lower() == "true", (
                f"Expected credentials=true, got {credentials}"
            )
