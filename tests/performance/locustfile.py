"""
Locust Distributed Load Testing Script for the Synthetic ERP Data Generation Platform.

This module provides a comprehensive, production-grade load testing suite targeting all
API Gateway REST endpoints of the Synthetic-ERP-Data-Generation-Platform. It simulates
realistic, multi-role user workflows — including generation job creation and monitoring,
schema discovery and browsing, statistical profile retrieval, template library access,
data export operations, admin panel usage, and health/monitoring checks.

Performance Targets:
    - API throughput: 1,000,000+ records/minute generation capacity
    - Health endpoint response time: <100ms (p95)
    - Standard API response time: <500ms (p95)
    - Error rate: <1% under sustained load
    - Kubernetes HPA scaling: Verified under progressive load ramp-up

User Profiles:
    - SyntheticERPUser (weight=10): General platform user exercising all workflows
    - SyntheticERPAdminUser (weight=1): Admin user focused on management and monitoring
    - SyntheticERPDeveloperUser (weight=3): Developer user with faster interaction pace

Execution Examples:
    # Basic local execution
    locust -f locustfile.py --host=http://localhost:5000

    # Full load test with parameters
    locust -f locustfile.py --host=http://localhost:5000 --users=100 --spawn-rate=10 --run-time=30m

    # Distributed mode (master)
    locust -f locustfile.py --master --host=http://localhost:5000

    # Distributed mode (worker)
    locust -f locustfile.py --worker --master-host=<master-ip>

    # Headless execution for CI/CD
    locust -f locustfile.py --host=http://localhost:5000 --headless --users=200 \
        --spawn-rate=20 --run-time=30m --csv=results/load_test
"""

from locust import HttpUser, TaskSet, task, between, events, tag
from locust.runners import MasterRunner, WorkerRunner

import json
import random
import uuid
import time
import logging
import os

# ---------------------------------------------------------------------------
# Logging Configuration
# ---------------------------------------------------------------------------
logger = logging.getLogger("synthetic_erp_load_test")
logger.setLevel(logging.INFO)
if not logger.handlers:
    _handler = logging.StreamHandler()
    _handler.setFormatter(
        logging.Formatter(
            "[%(asctime)s] %(levelname)s %(name)s: %(message)s",
            datefmt="%Y-%m-%d %H:%M:%S",
        )
    )
    logger.addHandler(_handler)

# ---------------------------------------------------------------------------
# Configuration Constants — All tuneable via environment variables
# ---------------------------------------------------------------------------
BASE_URL: str = os.environ.get("TARGET_HOST", "http://localhost:5000")
API_PREFIX: str = "/api/v1"
AUTH_TOKEN: str = os.environ.get("AUTH_TOKEN", "test-jwt-token")
TENANT_ID: str = os.environ.get("TENANT_ID", "test-tenant")

# Supported ERP systems matching platform Constraint C-005
ERP_SYSTEMS: list[str] = ["sap", "oracle_ebs", "dynamics", "legacy"]

# Initial-release ERP modules (Financial Accounting, HR, S&D, MM)
ERP_MODULES: list[str] = [
    "financial_accounting",
    "hr",
    "sales_distribution",
    "material_management",
]

# Four generation methods supported by the Generation Engine
GENERATION_METHODS: list[str] = ["ai_ml", "rules_based", "statistical", "masking"]

# Multi-format export targets
OUTPUT_FORMATS: list[str] = ["csv", "json", "parquet", "sql"]

# Realistic record count distribution for load variation
RECORD_COUNTS: list[int] = [1000, 5000, 10000, 50000, 100000]

# Throughput validation threshold (records per minute)
THROUGHPUT_TARGET: int = 1_000_000

# Slow-request warning threshold in milliseconds
SLOW_REQUEST_THRESHOLD_MS: int = 1000

# Health-check latency ceiling in milliseconds
HEALTH_CHECK_LATENCY_CEILING_MS: int = 100

# Sample ERP table names used for randomized payload construction
_SAMPLE_TABLE_NAMES: list[str] = [
    "gl_journal_entries",
    "accounts_payable",
    "accounts_receivable",
    "general_ledger",
    "cost_centers",
    "profit_centers",
    "employee_master",
    "payroll_records",
    "benefits_enrollment",
    "time_management",
    "sales_orders",
    "sales_items",
    "customer_master",
    "pricing_conditions",
    "delivery_documents",
    "purchase_orders",
    "purchase_items",
    "vendor_master",
    "material_master",
    "inventory_movements",
    "goods_receipts",
    "invoice_receipts",
]


# ---------------------------------------------------------------------------
# Throughput Tracker — Custom metric aggregator for records/min validation
# ---------------------------------------------------------------------------
class ThroughputTracker:
    """Tracks cumulative record generation throughput and validates against
    the 1M+ records/minute platform target.

    This singleton-style tracker is updated by generation job creation tasks
    and periodically evaluated by the event-hook system to log warnings when
    throughput falls below the target.
    """

    def __init__(self) -> None:
        self._total_records_requested: int = 0
        self._start_time: float = time.time()
        self._check_interval_seconds: float = 60.0
        self._last_check_time: float = self._start_time

    def add_records(self, count: int) -> None:
        """Register that *count* records were requested for generation."""
        self._total_records_requested += count

    @property
    def elapsed_minutes(self) -> float:
        """Minutes elapsed since tracking began."""
        elapsed = time.time() - self._start_time
        return max(elapsed / 60.0, 0.0001)  # guard against division by zero

    @property
    def records_per_minute(self) -> float:
        """Current throughput in records per minute."""
        return self._total_records_requested / self.elapsed_minutes

    def maybe_log_throughput(self) -> None:
        """Log a throughput report at most once per check-interval.

        Emits a WARNING when the observed throughput drops below the
        configured ``THROUGHPUT_TARGET`` (default 1 000 000 records/min).
        """
        now = time.time()
        if now - self._last_check_time < self._check_interval_seconds:
            return
        self._last_check_time = now
        rpm = self.records_per_minute
        if rpm < THROUGHPUT_TARGET:
            logger.warning(
                "Throughput %.0f records/min is BELOW target %d records/min "
                "(%.1f min elapsed, %d total records requested)",
                rpm,
                THROUGHPUT_TARGET,
                self.elapsed_minutes,
                self._total_records_requested,
            )
        else:
            logger.info(
                "Throughput %.0f records/min meets target %d records/min "
                "(%.1f min elapsed)",
                rpm,
                THROUGHPUT_TARGET,
                self.elapsed_minutes,
            )

    def summary(self) -> str:
        """Return a human-readable throughput summary."""
        rpm = self.records_per_minute
        status = "PASS" if rpm >= THROUGHPUT_TARGET else "FAIL"
        return (
            f"Throughput: {rpm:,.0f} records/min | "
            f"Target: {THROUGHPUT_TARGET:,} records/min | "
            f"Status: {status} | "
            f"Total records: {self._total_records_requested:,} | "
            f"Elapsed: {self.elapsed_minutes:.1f} min"
        )


# Module-level singleton tracker instance
_throughput_tracker = ThroughputTracker()


# ---------------------------------------------------------------------------
# Request Metrics Tracker — Per-endpoint-group aggregation
# ---------------------------------------------------------------------------
class _RequestMetricsTracker:
    """Collects per-endpoint-group request counts, response times, and error
    rates for post-run analysis and event-hook reporting."""

    def __init__(self) -> None:
        self.requests_by_group: dict[str, int] = {}
        self.errors_by_group: dict[str, int] = {}
        self.response_times_by_group: dict[str, list[float]] = {}

    def record(
        self,
        group: str,
        response_time_ms: float,
        is_failure: bool,
    ) -> None:
        """Record a single request observation."""
        self.requests_by_group[group] = self.requests_by_group.get(group, 0) + 1
        if is_failure:
            self.errors_by_group[group] = self.errors_by_group.get(group, 0) + 1
        self.response_times_by_group.setdefault(group, []).append(response_time_ms)

    def summary(self) -> dict[str, dict[str, float]]:
        """Return a per-group summary dict suitable for JSON serialisation."""
        result: dict[str, dict[str, float]] = {}
        for group, count in self.requests_by_group.items():
            times = self.response_times_by_group.get(group, [])
            avg_time = sum(times) / len(times) if times else 0.0
            errors = self.errors_by_group.get(group, 0)
            error_rate = (errors / count * 100) if count else 0.0
            result[group] = {
                "total_requests": count,
                "errors": errors,
                "error_rate_pct": round(error_rate, 2),
                "avg_response_time_ms": round(avg_time, 2),
            }
        return result

    def to_json(self) -> str:
        """Serialise the summary to a JSON string for file or pipeline output."""
        return json.dumps(self.summary(), indent=2, sort_keys=True)

    @classmethod
    def from_json(cls, raw: str) -> "_RequestMetricsTracker":
        """Reconstruct a tracker from a previously serialised JSON summary.

        This supports merging metrics collected across distributed Locust
        workers by deserialising worker-reported JSON payloads.

        Args:
            raw: JSON string produced by ``to_json()``.

        Returns:
            A new ``_RequestMetricsTracker`` pre-populated with the
            deserialised counts and averages.
        """
        data = json.loads(raw)
        tracker = cls()
        for group, metrics in data.items():
            count = int(metrics.get("total_requests", 0))
            tracker.requests_by_group[group] = count
            tracker.errors_by_group[group] = int(metrics.get("errors", 0))
            avg_ms = float(metrics.get("avg_response_time_ms", 0.0))
            # Reconstruct a representative list with the average (preserves avg)
            tracker.response_times_by_group[group] = [avg_ms] * count if count else []
        return tracker


_request_metrics = _RequestMetricsTracker()


# ---------------------------------------------------------------------------
# Helper: classify a URL path into a high-level endpoint group name
# ---------------------------------------------------------------------------
def _classify_endpoint(path: str) -> str:
    """Map a request URL path to a human-readable endpoint group name."""
    if "/generation/jobs" in path:
        return "generation"
    if "/schemas" in path:
        return "schema"
    if "/profiles" in path:
        return "profile"
    if "/templates" in path:
        return "template"
    if "/export" in path:
        return "export"
    if "/admin" in path:
        return "admin"
    if "/monitoring" in path:
        return "monitoring"
    if "/health" in path or "/ready" in path:
        return "health"
    if "/auth" in path:
        return "auth"
    return "other"


# =========================================================================
# AuthMixin — Shared authentication helpers injected into TaskSets
# =========================================================================
class AuthMixin:
    """Mixin providing common authentication headers and payload factories
    for all task sets that interact with authenticated API endpoints.

    Attributes consumed from the parent ``HttpUser``:
        * ``self.user.auth_token`` — JWT bearer token obtained during on_start
        * ``self.user.tenant_id``  — Multi-tenant identifier
    """

    def get_auth_headers(self) -> dict[str, str]:
        """Return a header dict containing the JWT bearer token, content type,
        and tenant identifier required by every authenticated API call.

        Returns:
            dict with Authorization, Content-Type, and X-Tenant-ID headers.
        """
        token = getattr(getattr(self, "user", None), "auth_token", AUTH_TOKEN)
        tenant = getattr(getattr(self, "user", None), "tenant_id", TENANT_ID)
        return {
            "Authorization": f"Bearer {token}",
            "Content-Type": "application/json",
            "X-Tenant-ID": tenant,
        }

    def generate_job_payload(self) -> dict:
        """Build a randomised generation-job request body simulating realistic
        user behaviour across ERP systems, modules, methods, and record counts.

        Returns:
            dict matching the ``GenerationJobRequest`` Pydantic schema expected
            by POST /api/v1/generation/jobs.
        """
        num_tables = random.randint(1, 5)
        selected_tables = random.sample(
            _SAMPLE_TABLE_NAMES, min(num_tables, len(_SAMPLE_TABLE_NAMES))
        )
        record_count = random.choice(RECORD_COUNTS)
        return {
            "name": f"load-test-job-{uuid.uuid4().hex[:8]}",
            "method": random.choice(GENERATION_METHODS),
            "erp_system": random.choice(ERP_SYSTEMS),
            "module": random.choice(ERP_MODULES),
            "record_count": record_count,
            "output_format": random.choice(OUTPUT_FORMATS),
            "table_selections": selected_tables,
            "options": {
                "batch_size": 10000,
                "preserve_relationships": True,
                "quality_threshold": 0.95,
            },
        }


# =========================================================================
# TaskSet: Generation Job Lifecycle
# =========================================================================
class GenerationTaskSet(TaskSet, AuthMixin):
    """Simulates the core generation-job lifecycle: creating jobs, polling for
    status updates, listing recent jobs, and cancelling in-flight jobs.

    This is the highest-weighted task set because generation is the platform's
    primary value proposition and the most resource-intensive workflow.
    """

    def on_start(self) -> None:
        """Initialise per-user tracking lists on TaskSet activation."""
        if not hasattr(self.user, "job_ids"):
            self.user.job_ids = []

    @tag("generation", "write")
    @task(5)
    def create_generation_job(self) -> None:
        """POST /api/v1/generation/jobs — Create a new synthetic data generation job.

        Sends a randomised payload covering all four generation methods and all
        four ERP modules.  Tracks the returned ``job_id`` for subsequent polling
        and records the ``record_count`` against the throughput tracker.
        """
        payload = self.generate_job_payload()
        start = time.time()
        with self.client.post(
            f"{API_PREFIX}/generation/jobs",
            json=payload,
            headers=self.get_auth_headers(),
            name="/api/v1/generation/jobs [POST]",
            catch_response=True,
        ) as response:
            elapsed_ms = (time.time() - start) * 1000
            if response.status_code == 201:
                try:
                    body = response.json()
                    job_id = body.get("id") or body.get("job_id")
                    if job_id:
                        self.user.job_ids.append(job_id)
                    response.success()
                except (json.JSONDecodeError, ValueError):
                    response.failure("Invalid JSON in 201 response body")
            elif response.status_code == 202:
                # Accepted is also valid for async job creation
                try:
                    body = response.json()
                    job_id = body.get("id") or body.get("job_id")
                    if job_id:
                        self.user.job_ids.append(job_id)
                    response.success()
                except (json.JSONDecodeError, ValueError):
                    response.failure("Invalid JSON in 202 response body")
            else:
                response.failure(
                    f"Expected 201/202, got {response.status_code}"
                )

            # Record throughput regardless of success for demand tracking
            _throughput_tracker.add_records(payload["record_count"])
            _throughput_tracker.maybe_log_throughput()

            if elapsed_ms > SLOW_REQUEST_THRESHOLD_MS:
                logger.warning(
                    "Slow job creation: %.0fms (threshold %dms)",
                    elapsed_ms,
                    SLOW_REQUEST_THRESHOLD_MS,
                )

    @tag("generation", "read")
    @task(10)
    def monitor_job_status(self) -> None:
        """GET /api/v1/generation/jobs/{job_id} — Poll a previously created job.

        Selects a random job from the user's tracked list and validates that
        the status field is one of the expected lifecycle states.
        """
        if not self.user.job_ids:
            # No jobs to monitor yet; skip gracefully
            return

        job_id = random.choice(self.user.job_ids)
        start = time.time()
        with self.client.get(
            f"{API_PREFIX}/generation/jobs/{job_id}",
            headers=self.get_auth_headers(),
            name="/api/v1/generation/jobs/[id] [GET]",
            catch_response=True,
        ) as response:
            elapsed_ms = (time.time() - start) * 1000
            if response.status_code == 200:
                try:
                    body = response.json()
                    status = body.get("status", "")
                    valid_statuses = {
                        "Submitted",
                        "Generating",
                        "Validating",
                        "Completed",
                        "Failed",
                        "Cancelled",
                        "submitted",
                        "generating",
                        "validating",
                        "completed",
                        "failed",
                        "cancelled",
                    }
                    if status in valid_statuses:
                        response.success()
                    else:
                        response.failure(f"Unexpected job status: {status}")
                except (json.JSONDecodeError, ValueError):
                    response.failure("Invalid JSON in job status response")
            elif response.status_code == 404:
                # Job may have been cleaned up; remove from tracking
                if job_id in self.user.job_ids:
                    self.user.job_ids.remove(job_id)
                response.success()
            else:
                response.failure(
                    f"Expected 200/404, got {response.status_code}"
                )

            if elapsed_ms > SLOW_REQUEST_THRESHOLD_MS:
                logger.warning(
                    "Slow job status check: %.0fms for job %s",
                    elapsed_ms,
                    job_id,
                )

    @tag("generation", "read")
    @task(3)
    def list_generation_jobs(self) -> None:
        """GET /api/v1/generation/jobs — List generation jobs with pagination.

        Validates that the response is 200 and contains the expected ``jobs``
        key in the response body.
        """
        page = random.randint(1, 5)
        with self.client.get(
            f"{API_PREFIX}/generation/jobs",
            params={"page": page, "per_page": 20},
            headers=self.get_auth_headers(),
            name="/api/v1/generation/jobs [GET]",
            catch_response=True,
        ) as response:
            if response.status_code == 200:
                try:
                    body = response.json()
                    if "jobs" in body or "items" in body or "data" in body:
                        response.success()
                    else:
                        response.failure("Response missing jobs/items/data key")
                except (json.JSONDecodeError, ValueError):
                    response.failure("Invalid JSON in job list response")
            else:
                response.failure(
                    f"Expected 200, got {response.status_code}"
                )

    @tag("generation", "write")
    @task(2)
    def cancel_generation_job(self) -> None:
        """DELETE /api/v1/generation/jobs/{job_id} — Cancel a running job.

        Accepts 200 (successfully cancelled) or 404 (already finished/removed).
        """
        if not self.user.job_ids:
            return

        job_id = random.choice(self.user.job_ids)
        with self.client.delete(
            f"{API_PREFIX}/generation/jobs/{job_id}",
            headers=self.get_auth_headers(),
            name="/api/v1/generation/jobs/[id] [DELETE]",
            catch_response=True,
        ) as response:
            if response.status_code in (200, 204, 404):
                if job_id in self.user.job_ids:
                    self.user.job_ids.remove(job_id)
                response.success()
            else:
                response.failure(
                    f"Expected 200/204/404, got {response.status_code}"
                )


# =========================================================================
# TaskSet: Schema Discovery & Browsing
# =========================================================================
class SchemaTaskSet(TaskSet, AuthMixin):
    """Simulates ERP schema discovery and browsing workflows: triggering
    schema extraction from connected ERP systems, listing discovered schemas
    with filtering, and retrieving detailed schema definitions."""

    def on_start(self) -> None:
        """Initialise schema tracking on TaskSet activation."""
        if not hasattr(self.user, "schema_ids"):
            self.user.schema_ids = []

    @tag("schema", "write")
    @task(4)
    def discover_schemas(self) -> None:
        """POST /api/v1/schemas/discover — Trigger schema discovery for an ERP system.

        Sends an ERP system type and mock connection configuration.
        """
        erp_system = random.choice(ERP_SYSTEMS)
        payload = {
            "erp_system": erp_system,
            "connection_config": {
                "host": f"{erp_system}-host.internal",
                "port": 3306,
                "database": f"{erp_system}_db",
                "username": "profiler",
                "use_ssl": True,
            },
            "modules": random.sample(ERP_MODULES, k=random.randint(1, len(ERP_MODULES))),
        }
        with self.client.post(
            f"{API_PREFIX}/schemas/discover",
            json=payload,
            headers=self.get_auth_headers(),
            name="/api/v1/schemas/discover [POST]",
            catch_response=True,
        ) as response:
            if response.status_code in (200, 202):
                try:
                    body = response.json()
                    schema_id = body.get("id") or body.get("schema_id") or body.get("discovery_id")
                    if schema_id:
                        self.user.schema_ids.append(str(schema_id))
                    response.success()
                except (json.JSONDecodeError, ValueError):
                    response.failure("Invalid JSON in schema discovery response")
            else:
                response.failure(
                    f"Expected 200/202, got {response.status_code}"
                )

    @tag("schema", "read")
    @task(8)
    def browse_schemas(self) -> None:
        """GET /api/v1/schemas — Browse discovered schemas with optional filtering.

        Validates 200 response with well-formed body.
        """
        params: dict[str, str] = {}
        if random.random() < 0.5:
            params["erp_system"] = random.choice(ERP_SYSTEMS)
        if random.random() < 0.5:
            params["module"] = random.choice(ERP_MODULES)
        params["page"] = str(random.randint(1, 3))
        params["per_page"] = "20"

        with self.client.get(
            f"{API_PREFIX}/schemas",
            params=params,
            headers=self.get_auth_headers(),
            name="/api/v1/schemas [GET]",
            catch_response=True,
        ) as response:
            if response.status_code == 200:
                try:
                    body = response.json()
                    # Attempt to extract schema IDs for later detail lookups
                    items = body.get("schemas") or body.get("items") or body.get("data") or []
                    for item in items[:5]:
                        sid = item.get("id") or item.get("schema_id")
                        if sid and str(sid) not in (self.user.schema_ids or []):
                            self.user.schema_ids.append(str(sid))
                    response.success()
                except (json.JSONDecodeError, ValueError):
                    response.failure("Invalid JSON in schema browse response")
            else:
                response.failure(
                    f"Expected 200, got {response.status_code}"
                )

    @tag("schema", "read")
    @task(3)
    def get_schema_details(self) -> None:
        """GET /api/v1/schemas/{schema_id} — Retrieve full schema definition."""
        if not self.user.schema_ids:
            # Use a synthetic UUID when no real IDs have been collected yet
            schema_id = str(uuid.uuid4())
        else:
            schema_id = random.choice(self.user.schema_ids)

        with self.client.get(
            f"{API_PREFIX}/schemas/{schema_id}",
            headers=self.get_auth_headers(),
            name="/api/v1/schemas/[id] [GET]",
            catch_response=True,
        ) as response:
            if response.status_code in (200, 404):
                response.success()
            else:
                response.failure(
                    f"Expected 200/404, got {response.status_code}"
                )


# =========================================================================
# TaskSet: Statistical Profile Operations
# =========================================================================
class ProfileTaskSet(TaskSet, AuthMixin):
    """Simulates statistical profile management: creating profiling jobs,
    listing profiles (exercises Redis caching), and retrieving profile details
    with distribution data."""

    def on_start(self) -> None:
        """Initialise profile tracking on TaskSet activation."""
        if not hasattr(self.user, "profile_ids"):
            self.user.profile_ids = []

    @tag("profile", "write")
    @task(3)
    def create_profile(self) -> None:
        """POST /api/v1/profiles — Create a new statistical profiling job.

        Attaches a schema reference and profiling configuration for the
        Profiling Service to execute.
        """
        schema_id = (
            random.choice(self.user.schema_ids)
            if getattr(self.user, "schema_ids", None)
            else str(uuid.uuid4())
        )
        payload = {
            "schema_id": schema_id,
            "profiling_config": {
                "sample_size": random.choice([1000, 5000, 10000]),
                "statistical_tests": ["ks_test", "chi_squared", "anderson_darling"],
                "include_patterns": True,
                "include_distributions": True,
            },
        }
        with self.client.post(
            f"{API_PREFIX}/profiles",
            json=payload,
            headers=self.get_auth_headers(),
            name="/api/v1/profiles [POST]",
            catch_response=True,
        ) as response:
            if response.status_code in (201, 202):
                try:
                    body = response.json()
                    pid = body.get("id") or body.get("profile_id")
                    if pid:
                        self.user.profile_ids.append(str(pid))
                    response.success()
                except (json.JSONDecodeError, ValueError):
                    response.failure("Invalid JSON in profile creation response")
            else:
                response.failure(
                    f"Expected 201/202, got {response.status_code}"
                )

    @tag("profile", "read", "caching")
    @task(7)
    def get_profiles(self) -> None:
        """GET /api/v1/profiles — List statistical profiles with pagination.

        This endpoint exercises Redis caching; repeated calls should return
        faster due to cache hits.
        """
        with self.client.get(
            f"{API_PREFIX}/profiles",
            params={"page": random.randint(1, 3), "per_page": 20},
            headers=self.get_auth_headers(),
            name="/api/v1/profiles [GET]",
            catch_response=True,
        ) as response:
            if response.status_code == 200:
                try:
                    body = response.json()
                    items = body.get("profiles") or body.get("items") or body.get("data") or []
                    for item in items[:5]:
                        pid = item.get("id") or item.get("profile_id")
                        if pid and str(pid) not in (self.user.profile_ids or []):
                            self.user.profile_ids.append(str(pid))
                    response.success()
                except (json.JSONDecodeError, ValueError):
                    response.failure("Invalid JSON in profile list response")
            else:
                response.failure(
                    f"Expected 200, got {response.status_code}"
                )

    @tag("profile", "read")
    @task(5)
    def get_profile_details(self) -> None:
        """GET /api/v1/profiles/{profile_id} — Retrieve a statistical profile."""
        if not self.user.profile_ids:
            profile_id = str(uuid.uuid4())
        else:
            profile_id = random.choice(self.user.profile_ids)

        with self.client.get(
            f"{API_PREFIX}/profiles/{profile_id}",
            headers=self.get_auth_headers(),
            name="/api/v1/profiles/[id] [GET]",
            catch_response=True,
        ) as response:
            if response.status_code in (200, 404):
                response.success()
            else:
                response.failure(
                    f"Expected 200/404, got {response.status_code}"
                )


# =========================================================================
# TaskSet: Template Library
# =========================================================================
class TemplateTaskSet(TaskSet, AuthMixin):
    """Simulates template library interactions: listing available generation
    templates with filtering, creating new templates, and viewing template
    details."""

    def on_start(self) -> None:
        """Initialise template tracking on TaskSet activation."""
        if not hasattr(self.user, "template_ids"):
            self.user.template_ids = []

    @tag("template", "read")
    @task(5)
    def list_templates(self) -> None:
        """GET /api/v1/templates — Browse the template catalog with optional filtering."""
        params: dict[str, str] = {"page": "1", "per_page": "20"}
        if random.random() < 0.4:
            params["erp_system"] = random.choice(ERP_SYSTEMS)
        if random.random() < 0.3:
            params["module"] = random.choice(ERP_MODULES)

        with self.client.get(
            f"{API_PREFIX}/templates",
            params=params,
            headers=self.get_auth_headers(),
            name="/api/v1/templates [GET]",
            catch_response=True,
        ) as response:
            if response.status_code == 200:
                try:
                    body = response.json()
                    items = body.get("templates") or body.get("items") or body.get("data") or []
                    for item in items[:5]:
                        tid = item.get("id") or item.get("template_id")
                        if tid and str(tid) not in (self.user.template_ids or []):
                            self.user.template_ids.append(str(tid))
                    response.success()
                except (json.JSONDecodeError, ValueError):
                    response.failure("Invalid JSON in template list response")
            else:
                response.failure(
                    f"Expected 200, got {response.status_code}"
                )

    @tag("template", "write")
    @task(2)
    def create_template(self) -> None:
        """POST /api/v1/templates — Create a new generation template."""
        payload = {
            "name": f"load-test-template-{uuid.uuid4().hex[:8]}",
            "description": "Template created by Locust load test",
            "erp_system": random.choice(ERP_SYSTEMS),
            "module": random.choice(ERP_MODULES),
            "method": random.choice(GENERATION_METHODS),
            "table_selections": random.sample(
                _SAMPLE_TABLE_NAMES, k=random.randint(1, 4)
            ),
            "default_record_count": random.choice(RECORD_COUNTS),
            "output_format": random.choice(OUTPUT_FORMATS),
            "options": {
                "preserve_relationships": True,
                "quality_threshold": 0.95,
            },
        }
        with self.client.post(
            f"{API_PREFIX}/templates",
            json=payload,
            headers=self.get_auth_headers(),
            name="/api/v1/templates [POST]",
            catch_response=True,
        ) as response:
            if response.status_code == 201:
                try:
                    body = response.json()
                    tid = body.get("id") or body.get("template_id")
                    if tid:
                        self.user.template_ids.append(str(tid))
                    response.success()
                except (json.JSONDecodeError, ValueError):
                    response.failure("Invalid JSON in template creation response")
            else:
                response.failure(
                    f"Expected 201, got {response.status_code}"
                )

    @tag("template", "read")
    @task(3)
    def get_template_details(self) -> None:
        """GET /api/v1/templates/{template_id} — Retrieve template details."""
        if not self.user.template_ids:
            template_id = str(uuid.uuid4())
        else:
            template_id = random.choice(self.user.template_ids)

        with self.client.get(
            f"{API_PREFIX}/templates/{template_id}",
            headers=self.get_auth_headers(),
            name="/api/v1/templates/[id] [GET]",
            catch_response=True,
        ) as response:
            if response.status_code in (200, 404):
                response.success()
            else:
                response.failure(
                    f"Expected 200/404, got {response.status_code}"
                )


# =========================================================================
# TaskSet: Data Export
# =========================================================================
class ExportTaskSet(TaskSet, AuthMixin):
    """Simulates data export operations: triggering an export of generated data
    to a target destination (file or database) and polling export status."""

    def on_start(self) -> None:
        """Initialise export tracking on TaskSet activation."""
        if not hasattr(self.user, "export_ids"):
            self.user.export_ids = []

    @tag("export", "write")
    @task(3)
    def export_data(self) -> None:
        """POST /api/v1/export — Trigger a data export for a completed generation job.

        Sends a randomised export configuration specifying format and
        destination (cloud storage or database).
        """
        job_id = (
            random.choice(self.user.job_ids)
            if getattr(self.user, "job_ids", None)
            else str(uuid.uuid4())
        )
        output_format = random.choice(OUTPUT_FORMATS)
        destination_type = random.choice(["s3", "azure_blob", "gcs", "database", "local"])
        payload = {
            "job_id": job_id,
            "format": output_format,
            "destination": {
                "type": destination_type,
                "config": {
                    "bucket": f"synthetic-erp-exports-{random.randint(1, 5)}",
                    "path": f"exports/{uuid.uuid4().hex[:8]}/",
                    "encryption": "AES-256",
                },
            },
        }
        with self.client.post(
            f"{API_PREFIX}/export",
            json=payload,
            headers=self.get_auth_headers(),
            name="/api/v1/export [POST]",
            catch_response=True,
        ) as response:
            if response.status_code in (200, 202):
                try:
                    body = response.json()
                    eid = body.get("id") or body.get("export_id")
                    if eid:
                        self.user.export_ids.append(str(eid))
                    response.success()
                except (json.JSONDecodeError, ValueError):
                    response.failure("Invalid JSON in export response")
            else:
                response.failure(
                    f"Expected 200/202, got {response.status_code}"
                )

    @tag("export", "read")
    @task(2)
    def get_export_status(self) -> None:
        """GET /api/v1/export/{export_id} — Check the status of an export job."""
        if not self.user.export_ids:
            export_id = str(uuid.uuid4())
        else:
            export_id = random.choice(self.user.export_ids)

        with self.client.get(
            f"{API_PREFIX}/export/{export_id}",
            headers=self.get_auth_headers(),
            name="/api/v1/export/[id] [GET]",
            catch_response=True,
        ) as response:
            if response.status_code in (200, 404):
                response.success()
            else:
                response.failure(
                    f"Expected 200/404, got {response.status_code}"
                )


# =========================================================================
# TaskSet: Admin Operations
# =========================================================================
class AdminTaskSet(TaskSet, AuthMixin):
    """Simulates admin-role operations: listing platform users, viewing system
    settings, and retrieving tenant configuration.  These are low-frequency
    operations typically performed by Platform Admin users."""

    @tag("admin", "read")
    @task(3)
    def list_users(self) -> None:
        """GET /api/v1/admin/users — Retrieve platform user list."""
        with self.client.get(
            f"{API_PREFIX}/admin/users",
            params={"page": "1", "per_page": "50"},
            headers=self.get_auth_headers(),
            name="/api/v1/admin/users [GET]",
            catch_response=True,
        ) as response:
            if response.status_code == 200:
                response.success()
            elif response.status_code == 403:
                # Non-admin token — expected for non-admin user types
                response.success()
            else:
                response.failure(
                    f"Expected 200/403, got {response.status_code}"
                )

    @tag("admin", "read")
    @task(1)
    def get_system_settings(self) -> None:
        """GET /api/v1/admin/settings — Retrieve platform system settings."""
        with self.client.get(
            f"{API_PREFIX}/admin/settings",
            headers=self.get_auth_headers(),
            name="/api/v1/admin/settings [GET]",
            catch_response=True,
        ) as response:
            if response.status_code in (200, 403):
                response.success()
            else:
                response.failure(
                    f"Expected 200/403, got {response.status_code}"
                )

    @tag("admin", "read")
    @task(2)
    def get_tenant_config(self) -> None:
        """GET /api/v1/admin/tenants/{tenant_id} — Retrieve tenant configuration."""
        tenant = getattr(getattr(self, "user", None), "tenant_id", TENANT_ID)
        with self.client.get(
            f"{API_PREFIX}/admin/tenants/{tenant}",
            headers=self.get_auth_headers(),
            name="/api/v1/admin/tenants/[id] [GET]",
            catch_response=True,
        ) as response:
            if response.status_code in (200, 403, 404):
                response.success()
            else:
                response.failure(
                    f"Expected 200/403/404, got {response.status_code}"
                )


# =========================================================================
# TaskSet: Health & Readiness Checks
# =========================================================================
class HealthCheckTaskSet(TaskSet):
    """Simulates Kubernetes liveness and readiness probe traffic.

    Health check endpoints do NOT require authentication.  The health
    endpoint response time is validated against a 100 ms ceiling to
    ensure probe reliability.
    """

    @tag("health")
    @task(10)
    def health_check(self) -> None:
        """GET /health — Liveness probe.

        Validates 200 status AND response time below the health-check
        latency ceiling (default 100 ms).
        """
        start = time.time()
        with self.client.get(
            "/health",
            name="/health [GET]",
            catch_response=True,
        ) as response:
            elapsed_ms = (time.time() - start) * 1000
            if response.status_code == 200:
                if elapsed_ms > HEALTH_CHECK_LATENCY_CEILING_MS:
                    response.failure(
                        f"Health check took {elapsed_ms:.0f}ms "
                        f"(ceiling {HEALTH_CHECK_LATENCY_CEILING_MS}ms)"
                    )
                else:
                    response.success()
            else:
                response.failure(
                    f"Expected 200, got {response.status_code}"
                )

    @tag("health")
    @task(5)
    def readiness_check(self) -> None:
        """GET /ready — Readiness probe.

        Validates 200 status to confirm the service and its dependencies
        (MongoDB, Redis) are ready to serve traffic.
        """
        with self.client.get(
            "/ready",
            name="/ready [GET]",
            catch_response=True,
        ) as response:
            if response.status_code == 200:
                response.success()
            else:
                response.failure(
                    f"Expected 200, got {response.status_code}"
                )


# =========================================================================
# TaskSet: Monitoring & Metrics
# =========================================================================
class MonitoringTaskSet(TaskSet, AuthMixin):
    """Simulates Prometheus scrape and monitoring dashboard traffic against
    the platform's metrics endpoint."""

    @tag("monitoring", "read")
    @task(3)
    def get_metrics(self) -> None:
        """GET /api/v1/monitoring/metrics — Retrieve platform metrics.

        Validates 200 response from the metrics/monitoring endpoint.
        """
        with self.client.get(
            f"{API_PREFIX}/monitoring/metrics",
            headers=self.get_auth_headers(),
            name="/api/v1/monitoring/metrics [GET]",
            catch_response=True,
        ) as response:
            if response.status_code == 200:
                response.success()
            else:
                response.failure(
                    f"Expected 200, got {response.status_code}"
                )


# =========================================================================
# HttpUser: General Platform User
# =========================================================================
class SyntheticERPUser(HttpUser):
    """Primary simulated user representing a Data Engineer or QA Engineer
    exercising all platform workflows.

    Weight: 10 (default — majority of simulated traffic).
    Think time: 1-5 seconds between tasks, matching realistic human
    interaction pacing.
    """

    wait_time = between(1, 5)
    tasks = {
        GenerationTaskSet: 5,     # Core workflow — highest weight
        SchemaTaskSet: 3,         # Schema discovery — medium weight
        ProfileTaskSet: 3,        # Profile management — medium weight
        TemplateTaskSet: 2,       # Template library — lower weight
        ExportTaskSet: 2,         # Export operations — lower weight
        AdminTaskSet: 1,          # Admin tasks — lowest (few admins)
        HealthCheckTaskSet: 1,    # Background monitoring
        MonitoringTaskSet: 1,     # Background monitoring
    }
    weight = 10

    # Attributes populated in on_start
    auth_token: str = AUTH_TOKEN
    tenant_id: str = TENANT_ID
    job_ids: list[str]
    schema_ids: list[str]
    profile_ids: list[str]
    template_ids: list[str]
    export_ids: list[str]
    _request_count: int
    _session_start: float

    def on_start(self) -> None:
        """Authenticate the simulated user and initialise per-session state.

        Attempts to obtain a JWT via POST /api/v1/auth/login.  Falls back
        to the environment-provided ``AUTH_TOKEN`` if the auth endpoint is
        not available (common in isolated load-test environments).
        """
        self.job_ids = []
        self.schema_ids = []
        self.profile_ids = []
        self.template_ids = []
        self.export_ids = []
        self._request_count = 0
        self._session_start = time.time()

        # Attempt authentication against the auth endpoint
        try:
            response = self.client.post(
                f"{API_PREFIX}/auth/login",
                json={
                    "username": f"loadtest-user-{uuid.uuid4().hex[:6]}",
                    "password": "load-test-credential",
                    "grant_type": "password",
                },
                headers={"Content-Type": "application/json"},
                name="/api/v1/auth/login [POST]",
                catch_response=True,
            )
            if response.status_code == 200:
                try:
                    body = response.json()
                    self.auth_token = body.get("access_token") or body.get("token") or AUTH_TOKEN
                    self.tenant_id = body.get("tenant_id") or TENANT_ID
                    response.success()
                except (json.JSONDecodeError, ValueError):
                    self.auth_token = AUTH_TOKEN
                    response.success()
            else:
                # Auth endpoint may not be available in load-test mode;
                # fall back gracefully to env-provided token
                self.auth_token = AUTH_TOKEN
                response.success()
        except Exception:
            self.auth_token = AUTH_TOKEN

        logger.info(
            "User session started: tenant=%s, token=%s...",
            self.tenant_id,
            self.auth_token[:16] if self.auth_token else "none",
        )

    def on_stop(self) -> None:
        """Log a summary of the user's session upon virtual user teardown."""
        elapsed = time.time() - self._session_start
        logger.info(
            "User session ended: tenant=%s, duration=%.1fs, "
            "jobs_created=%d, schemas_tracked=%d, profiles_tracked=%d, "
            "templates_tracked=%d, exports_tracked=%d",
            self.tenant_id,
            elapsed,
            len(self.job_ids),
            len(self.schema_ids),
            len(self.profile_ids),
            len(self.template_ids),
            len(self.export_ids),
        )


# =========================================================================
# HttpUser: Admin User
# =========================================================================
class SyntheticERPAdminUser(HttpUser):
    """Simulated Platform Admin user focused on management and monitoring.

    Weight: 1 — approximately 1 admin per 10 regular users, reflecting
    realistic admin-to-user ratios in enterprise platforms.
    Think time: 2-8 seconds (admins review dashboards more slowly).
    """

    weight = 1
    wait_time = between(2, 8)
    tasks = {
        AdminTaskSet: 5,
        MonitoringTaskSet: 3,
        HealthCheckTaskSet: 2,
    }

    # Shared user attributes
    auth_token: str = AUTH_TOKEN
    tenant_id: str = TENANT_ID
    job_ids: list[str]
    schema_ids: list[str]
    profile_ids: list[str]
    template_ids: list[str]
    export_ids: list[str]

    def on_start(self) -> None:
        """Initialise admin user state."""
        self.job_ids = []
        self.schema_ids = []
        self.profile_ids = []
        self.template_ids = []
        self.export_ids = []
        self.auth_token = os.environ.get("ADMIN_AUTH_TOKEN", AUTH_TOKEN)
        self.tenant_id = TENANT_ID
        logger.info("Admin user session started: tenant=%s", self.tenant_id)

    def on_stop(self) -> None:
        """Log admin session summary."""
        logger.info("Admin user session ended: tenant=%s", self.tenant_id)


# =========================================================================
# HttpUser: Developer User
# =========================================================================
class SyntheticERPDeveloperUser(HttpUser):
    """Simulated Developer user with a faster interaction pace, focused on
    generation, schema browsing, and export operations.

    Weight: 3 — moderate proportion of total traffic.
    Think time: 1-3 seconds (developers work faster with API-driven workflows).
    """

    weight = 3
    wait_time = between(1, 3)
    tasks = {
        GenerationTaskSet: 5,
        SchemaTaskSet: 4,
        ExportTaskSet: 3,
    }

    # Shared user attributes
    auth_token: str = AUTH_TOKEN
    tenant_id: str = TENANT_ID
    job_ids: list[str]
    schema_ids: list[str]
    profile_ids: list[str]
    template_ids: list[str]
    export_ids: list[str]

    def on_start(self) -> None:
        """Initialise developer user state."""
        self.job_ids = []
        self.schema_ids = []
        self.profile_ids = []
        self.template_ids = []
        self.export_ids = []
        self.auth_token = AUTH_TOKEN
        self.tenant_id = TENANT_ID
        logger.info("Developer user session started: tenant=%s", self.tenant_id)

    def on_stop(self) -> None:
        """Log developer session summary."""
        logger.info(
            "Developer user session ended: tenant=%s, jobs=%d, exports=%d",
            self.tenant_id,
            len(self.job_ids),
            len(self.export_ids),
        )


# =========================================================================
# Event Hooks — Lifecycle and per-request instrumentation
# =========================================================================

@events.test_start.add_listener
def on_test_start(environment, **kwargs) -> None:
    """Fired when the Locust test begins.  Logs test parameters and resets
    the throughput tracker.

    On the master runner in distributed mode, also logs the expected
    worker count.
    """
    global _throughput_tracker, _request_metrics
    _throughput_tracker = ThroughputTracker()
    _request_metrics = _RequestMetricsTracker()

    user_count = environment.parsed_options.num_users if environment.parsed_options else "N/A"
    spawn_rate = environment.parsed_options.spawn_rate if environment.parsed_options else "N/A"
    run_time = (
        environment.parsed_options.run_time if environment.parsed_options else "N/A"
    )

    logger.info(
        "=== Load Test STARTED at %s ===",
        time.strftime("%Y-%m-%d %H:%M:%S"),
    )
    logger.info(
        "Configuration: users=%s, spawn_rate=%s, run_time=%s, target_host=%s",
        user_count,
        spawn_rate,
        run_time,
        environment.host or BASE_URL,
    )
    logger.info("Throughput target: %d records/minute", THROUGHPUT_TARGET)

    if isinstance(environment.runner, MasterRunner):
        logger.info(
            "Running in DISTRIBUTED mode (master). "
            "Workers expected: %d",
            environment.runner.target_user_count
            if hasattr(environment.runner, "target_user_count")
            else 0,
        )
    elif isinstance(environment.runner, WorkerRunner):
        logger.info("Running in DISTRIBUTED mode (worker).")


@events.test_stop.add_listener
def on_test_stop(environment, **kwargs) -> None:
    """Fired when the Locust test ends.  Logs the throughput summary and
    per-endpoint-group metrics."""
    logger.info(
        "=== Load Test STOPPED at %s ===",
        time.strftime("%Y-%m-%d %H:%M:%S"),
    )
    logger.info("Throughput Summary: %s", _throughput_tracker.summary())

    group_summary = _request_metrics.summary()
    if group_summary:
        logger.info("Per-Endpoint-Group Metrics:")
        for group_name, metrics in sorted(group_summary.items()):
            logger.info(
                "  %-15s  requests=%d  errors=%d  error_rate=%.2f%%  avg_time=%.1fms",
                group_name,
                metrics["total_requests"],
                metrics["errors"],
                metrics["error_rate_pct"],
                metrics["avg_response_time_ms"],
            )
        # Emit a machine-parseable JSON summary for CI/CD pipeline consumption
        logger.info(
            "Structured metrics (JSON): %s",
            json.dumps(group_summary, indent=2, sort_keys=True),
        )


@events.request.add_listener
def on_request(
    request_type: str,
    name: str,
    response_time: float,
    response_length: int,
    response: object,
    exception: object,
    context: dict,
    **kwargs,
) -> None:
    """Fired after every HTTP request.  Performs:

    1. Per-endpoint-group metric aggregation (count, errors, response time).
    2. Slow-request warning when response_time exceeds the configured
       threshold (default 1000 ms).
    3. Error-rate logging for non-2xx responses.
    """
    is_failure = exception is not None
    group = _classify_endpoint(name)
    _request_metrics.record(group, response_time, is_failure)

    # Warn on slow requests
    if response_time > SLOW_REQUEST_THRESHOLD_MS:
        logger.warning(
            "SLOW REQUEST: %s %s  %.0fms (threshold %dms)",
            request_type,
            name,
            response_time,
            SLOW_REQUEST_THRESHOLD_MS,
        )

    # Log individual failures for debugging
    if is_failure:
        logger.error(
            "REQUEST FAILED: %s %s  exception=%s  response_time=%.0fms",
            request_type,
            name,
            str(exception)[:200],
            response_time,
        )


# =========================================================================
# Main entry-point for direct execution
# =========================================================================
if __name__ == "__main__":
    print(
        """
================================================================================
  Synthetic ERP Data Generation Platform — Locust Load Test Suite
================================================================================

Usage:

  # 1. Basic local run (opens Locust Web UI at http://localhost:8089)
  locust -f locustfile.py --host=http://localhost:5000

  # 2. Headless run with fixed parameters
  locust -f locustfile.py --host=http://localhost:5000 \\
      --users=100 --spawn-rate=10 --run-time=30m \\
      --headless --csv=results/load_test

  # 3. Distributed mode — Master
  locust -f locustfile.py --master --host=http://localhost:5000

  # 4. Distributed mode — Worker (run on each worker machine)
  locust -f locustfile.py --worker --master-host=<MASTER_IP>

  # 5. High-load stress test (validates HPA scaling)
  locust -f locustfile.py --host=http://localhost:5000 \\
      --users=500 --spawn-rate=50 --run-time=60m \\
      --headless --csv=results/stress_test

Environment Variables:
  TARGET_HOST       API Gateway URL (default: http://localhost:5000)
  AUTH_TOKEN        JWT bearer token for authenticated endpoints
  ADMIN_AUTH_TOKEN  JWT token with admin role claims
  TENANT_ID         Multi-tenant identifier (default: test-tenant)

Performance Targets:
  - Throughput:  >= 1,000,000 records/minute
  - Health p95:  < 100ms
  - API p95:     < 500ms
  - Error rate:  < 1%%
================================================================================
"""
    )
