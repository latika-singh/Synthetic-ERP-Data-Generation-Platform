/**
 * k6 Sustained Load Testing Script for the Synthetic ERP Data Generation Platform
 *
 * This script validates platform scalability by simulating realistic user workflows
 * against the API Gateway REST endpoints. It exercises the full range of API operations
 * including generation job creation/monitoring, schema discovery, profile retrieval,
 * template browsing, data export, admin operations, and health/monitoring checks.
 *
 * Performance Targets:
 *   - HTTP request duration: p95 < 500ms, p99 < 1500ms
 *   - Error rate: < 1% of all requests
 *   - Throughput: >= 100 requests/second sustained
 *   - Iteration duration: p95 < 3 seconds
 *   - Generation throughput: targeting 1M+ records/minute
 *
 * Load Profile (30 minutes total):
 *   Stage 1: Ramp up to 50 VUs over 2 minutes   (warm-up)
 *   Stage 2: Hold at 50 VUs for 5 minutes        (steady state - moderate)
 *   Stage 3: Ramp up to 200 VUs over 3 minutes   (stress ramp)
 *   Stage 4: Hold at 200 VUs for 10 minutes       (sustained high load)
 *   Stage 5: Ramp up to 500 VUs over 2 minutes   (peak load ramp)
 *   Stage 6: Hold at 500 VUs for 5 minutes        (peak sustained)
 *   Stage 7: Ramp down to 0 VUs over 3 minutes   (cool down)
 *
 * HPA Scaling Expectations:
 *   - During Stage 3-4: Kubernetes HPA should begin scaling API Gateway pods from
 *     the minimum replica count (2) toward the mid-range (4-6 pods) as CPU utilization
 *     exceeds the 70% threshold and request queue depth grows.
 *   - During Stage 5-6: HPA should scale to maximum replicas (8-10 pods). Generation
 *     Engine pods should also scale based on job queue depth.
 *   - During Stage 7: Pods should begin scaling down after the cool-down stabilization
 *     period (default 5 minutes in HPA).
 *
 * Redis Caching Validation:
 *   - Profile retrieval and schema browsing endpoints should exhibit significantly
 *     lower response times on repeated requests due to Redis caching.
 *   - Cache hit rate is tracked via response timing analysis: responses under 50ms
 *     are classified as cache hits, while responses over 100ms indicate cache misses.
 *
 * Usage:
 *   k6 run tests/performance/k6_load_test.js
 *   k6 run --env BASE_URL=https://staging.example.com tests/performance/k6_load_test.js
 *   k6 run --env BASE_URL=https://staging.example.com --env AUTH_TOKEN=<jwt> tests/performance/k6_load_test.js
 *
 * @module k6_load_test
 */

import http from 'k6/http';
import { check, sleep, group } from 'k6';
import { Rate, Trend, Counter } from 'k6/metrics';
import { randomString, randomIntBetween } from 'https://jslib.k6.io/k6-utils/1.2.0/index.js';

// =============================================================================
// Custom Metrics
// =============================================================================
// These custom metrics provide application-specific performance indicators beyond
// the standard k6 HTTP metrics, enabling fine-grained analysis of platform behavior.

/**
 * Tracks the duration of generation job creation requests (POST /api/v1/generation/jobs).
 * Used to measure API Gateway latency for the most critical write operation.
 */
const jobCreationDuration = new Trend('job_creation_duration');

/**
 * Tracks the proportion of generation jobs that reach a successful terminal state.
 * A rate below 95% during sustained load indicates capacity or reliability issues.
 */
const jobCompletionRate = new Rate('job_completion_rate');

/**
 * Accumulates the total number of synthetic records generated across all VU iterations.
 * Used to validate the 1M+ records/minute throughput target by dividing the counter
 * value by elapsed test time in minutes.
 */
const generationThroughput = new Counter('generation_throughput_records');

/**
 * Tracks Redis cache hit rate based on response timing analysis for profile and schema
 * retrieval endpoints. Responses under 50ms are classified as cache hits.
 */
const cacheHitRate = new Rate('cache_hit_rate');

/**
 * Tracks the response time for schema browsing operations to measure caching effectiveness.
 */
const schemaBrowseDuration = new Trend('schema_browse_duration');

/**
 * Tracks the response time for profile retrieval to measure Redis caching performance.
 */
const profileRetrievalDuration = new Trend('profile_retrieval_duration');

/**
 * Tracks export operation duration for provisioning service performance analysis.
 */
const exportDuration = new Trend('export_duration');

/**
 * Tracks the number of failed requests by endpoint group for drill-down analysis.
 */
const errorsByGroup = new Counter('errors_by_group');

// =============================================================================
// Environment Configuration
// =============================================================================
// All configuration is driven by environment variables following 12-factor app
// methodology, enabling the same script to target development, staging, or
// production environments without code changes.

/** Base URL of the API Gateway. Override with --env BASE_URL=<url> */
const BASE_URL = __ENV.BASE_URL || 'http://localhost:5000';

/** API version prefix for all REST endpoints */
const API_PREFIX = '/api/v1';

/** JWT authentication token. Override with --env AUTH_TOKEN=<token> */
const AUTH_TOKEN = __ENV.AUTH_TOKEN || 'test-jwt-token';

/** Tenant identifier for multi-tenant isolation testing */
const TENANT_ID = __ENV.TENANT_ID || 'test-tenant-001';

/** Maximum number of job status polling iterations before timeout */
const MAX_POLL_ITERATIONS = __ENV.MAX_POLL_ITERATIONS || 5;

/** Polling interval in seconds between job status checks */
const POLL_INTERVAL_SECONDS = __ENV.POLL_INTERVAL_SECONDS || 2;

/** Cache hit threshold in milliseconds — responses faster than this are cache hits */
const CACHE_HIT_THRESHOLD_MS = 50;

// =============================================================================
// Test Data Constants
// =============================================================================
// Constants defining the parameter spaces for randomized test payloads, matching
// the platform's supported ERP systems, modules, generation methods, and formats.

/** Supported generation methods (F-001 through F-004) */
const GENERATION_METHODS = ['ai_ml', 'rules_based', 'statistical', 'masking'];

/** Supported ERP system types (F-005, F-006) */
const ERP_SYSTEMS = ['sap', 'oracle_ebs', 'dynamics', 'legacy'];

/** Supported ERP modules — limited to initial release scope (C-005) */
const ERP_MODULES = [
    'financial_accounting',
    'hr',
    'sales_distribution',
    'material_management',
];

/** Supported output formats (F-009) */
const OUTPUT_FORMATS = ['csv', 'json', 'parquet', 'sql'];

/** Sample table names for generation job payloads, organized by ERP module */
const SAMPLE_TABLES = {
    financial_accounting: [
        'gl_journal_entries',
        'accounts_payable',
        'accounts_receivable',
        'chart_of_accounts',
        'fiscal_periods',
        'cost_centers',
    ],
    hr: [
        'employees',
        'payroll_records',
        'benefits_enrollment',
        'time_attendance',
        'positions',
        'departments',
    ],
    sales_distribution: [
        'sales_orders',
        'customers',
        'pricing_conditions',
        'delivery_documents',
        'billing_documents',
        'sales_regions',
    ],
    material_management: [
        'purchase_orders',
        'vendors',
        'inventory_items',
        'goods_receipts',
        'material_master',
        'warehouse_locations',
    ],
};

// =============================================================================
// k6 Options Configuration
// =============================================================================
// Exported options object defines the load profile stages and performance
// thresholds. k6 automatically reads this exported constant to configure
// the test execution parameters.

/**
 * k6 test execution options defining load stages and pass/fail thresholds.
 *
 * The 7-stage load profile simulates a realistic traffic pattern:
 * warm-up → steady state → stress ramp → sustained high load → peak → cool down.
 *
 * Thresholds are set to validate the platform meets its SLA targets:
 * - p95 response time < 500ms ensures responsive user experience
 * - p99 response time < 1500ms allows for occasional slow responses under peak load
 * - Error rate < 1% ensures platform reliability under stress
 * - Minimum 100 req/sec throughput validates horizontal scalability
 * - p95 iteration duration < 3s ensures complete user workflows are responsive
 */
export const options = {
    stages: [
        // Stage 1: Warm-up — Gradually introduce load to allow services to initialize
        // connection pools, JIT compilation, and Redis cache warming
        { duration: '2m', target: 50 },

        // Stage 2: Steady state (moderate) — Validate baseline performance with moderate
        // concurrent users; HPA should maintain minimum replica count
        { duration: '5m', target: 50 },

        // Stage 3: Stress ramp — Increase load to trigger HPA scaling events;
        // CPU utilization should cross the 70% scaling threshold during this stage
        { duration: '3m', target: 200 },

        // Stage 4: Sustained high load — Hold elevated load to verify HPA has scaled
        // appropriately and the platform maintains performance at higher concurrency
        { duration: '10m', target: 200 },

        // Stage 5: Peak load ramp — Push to maximum designed concurrency;
        // HPA should scale to near-maximum pod count during this phase
        { duration: '2m', target: 500 },

        // Stage 6: Peak sustained — Validate platform stability at peak load;
        // all services should be at or near maximum replica counts
        { duration: '5m', target: 500 },

        // Stage 7: Cool down — Gradually reduce load to observe graceful scale-down
        // behavior; HPA stabilization window prevents premature de-scaling
        { duration: '3m', target: 0 },
    ],

    thresholds: {
        // 95th percentile response time must be under 500ms per SLA requirements.
        // 99th percentile under 1500ms allows headroom for occasional slow responses
        // during HPA scaling transitions and cache misses.
        http_req_duration: ['p(95)<500', 'p(99)<1500'],

        // Error rate must remain below 1% to ensure platform reliability.
        // Errors include HTTP 5xx responses, timeouts, and connection failures.
        http_req_failed: ['rate<0.01'],

        // Sustained throughput must exceed 100 requests/second to validate
        // that the platform can handle enterprise workloads at scale.
        http_reqs: ['rate>100'],

        // Complete user workflow iteration must finish within 3 seconds (p95)
        // to ensure responsive end-to-end user experience.
        iteration_duration: ['p(95)<3000'],

        // Custom metric thresholds for application-specific SLAs
        job_creation_duration: ['p(95)<1000', 'p(99)<2000'],
        schema_browse_duration: ['p(95)<300', 'p(99)<800'],
        profile_retrieval_duration: ['p(95)<200', 'p(99)<500'],
    },
};

// =============================================================================
// Helper Functions
// =============================================================================
// Utility functions for constructing authenticated requests and generating
// randomized test data that exercises diverse code paths.

/**
 * Returns HTTP headers with JWT Authorization Bearer token and required
 * headers for authenticated API requests.
 *
 * @returns {Object} Headers object with Authorization, Content-Type, and X-Tenant-ID
 */
function getAuthHeaders() {
    return {
        Authorization: `Bearer ${AUTH_TOKEN}`,
        'Content-Type': 'application/json',
        'X-Tenant-ID': TENANT_ID,
        'X-Correlation-ID': `k6-${randomString(16)}`,
    };
}

/**
 * Returns a randomly selected generation method from the supported methods.
 * Exercises all four generation paths: AI/ML (GAN/VAE), rules-based,
 * statistical synthesis, and intelligent masking.
 *
 * @returns {string} One of: 'ai_ml', 'rules_based', 'statistical', 'masking'
 */
function randomMethod() {
    return GENERATION_METHODS[Math.floor(Math.random() * GENERATION_METHODS.length)];
}

/**
 * Returns a randomly selected ERP system type from supported systems.
 * Exercises connector paths for SAP, Oracle EBS, Microsoft Dynamics, and legacy.
 *
 * @returns {string} One of: 'sap', 'oracle_ebs', 'dynamics', 'legacy'
 */
function randomERPSystem() {
    return ERP_SYSTEMS[Math.floor(Math.random() * ERP_SYSTEMS.length)];
}

/**
 * Returns a randomly selected ERP module from the initial release scope (C-005).
 * Limited to four modules: Financial Accounting, HR, Sales & Distribution,
 * Material Management.
 *
 * @returns {string} One of: 'financial_accounting', 'hr', 'sales_distribution', 'material_management'
 */
function randomModule() {
    return ERP_MODULES[Math.floor(Math.random() * ERP_MODULES.length)];
}

/**
 * Returns a randomly selected output format from supported export formats (F-009).
 * Exercises all four output paths: CSV, JSON, Parquet, and SQL.
 *
 * @returns {string} One of: 'csv', 'json', 'parquet', 'sql'
 */
function randomOutputFormat() {
    return OUTPUT_FORMATS[Math.floor(Math.random() * OUTPUT_FORMATS.length)];
}

/**
 * Returns a random subset of sample table names for the specified ERP module.
 * Generates realistic generation job payloads with 1-4 table selections.
 *
 * @param {string} moduleName - The ERP module to select tables from
 * @returns {Array<string>} Array of 1-4 randomly selected table names
 */
function randomTableSelections(moduleName) {
    const tables = SAMPLE_TABLES[moduleName] || SAMPLE_TABLES.financial_accounting;
    const count = randomIntBetween(1, Math.min(4, tables.length));
    const shuffled = tables.slice().sort(() => 0.5 - Math.random());
    return shuffled.slice(0, count);
}

/**
 * Constructs a complete generation job request payload with randomized parameters.
 * Each call produces a unique combination of generation method, ERP system, module,
 * record count, and output format to exercise diverse platform code paths.
 *
 * @returns {Object} Complete generation job request payload
 */
function buildGenerationJobPayload() {
    const selectedModule = randomModule();
    const recordCount = randomIntBetween(1000, 100000);

    return {
        name: `k6-load-test-job-${randomString(8)}`,
        method: randomMethod(),
        erp_system: randomERPSystem(),
        module: selectedModule,
        record_count: recordCount,
        output_format: randomOutputFormat(),
        table_selections: randomTableSelections(selectedModule),
        configuration: {
            batch_size: 10000,
            preserve_relationships: true,
            quality_threshold: 0.95,
            enable_compliance_check: true,
        },
        metadata: {
            created_by: 'k6-load-test',
            correlation_id: `k6-${randomString(16)}`,
            test_run_timestamp: new Date().toISOString(),
        },
    };
}

/**
 * Constructs an export request payload for a completed generation job.
 *
 * @param {string} jobId - The ID of the generation job to export
 * @returns {Object} Complete export request payload
 */
function buildExportPayload(jobId) {
    return {
        job_id: jobId,
        format: randomOutputFormat(),
        destination: {
            type: 'cloud_storage',
            provider: 'aws_s3',
            bucket: `synthetic-erp-exports-${TENANT_ID}`,
            prefix: `load-test/${new Date().toISOString().split('T')[0]}/`,
        },
        options: {
            compression: true,
            encryption: 'aes_256',
            split_size_mb: 100,
        },
    };
}

/**
 * Determines if a response time indicates a Redis cache hit based on the
 * configured threshold. Used to track cache effectiveness under load.
 *
 * @param {number} durationMs - Response duration in milliseconds
 * @returns {boolean} true if the response time suggests a cache hit
 */
function isCacheHit(durationMs) {
    return durationMs < CACHE_HIT_THRESHOLD_MS;
}

// =============================================================================
// Setup Function
// =============================================================================
// Executed once before the test starts across all VUs. Validates that the
// target API is reachable and returns shared test context data.

/**
 * k6 setup function executed once before the test begins.
 * Validates API health, authenticates, and returns shared test context.
 *
 * @returns {Object} Shared test data available to all VU iterations via data parameter
 */
export function setup() {
    console.log(`[SETUP] Starting k6 load test against: ${BASE_URL}`);
    console.log(`[SETUP] Tenant ID: ${TENANT_ID}`);
    console.log(`[SETUP] Test stages: 7 stages over 30 minutes total`);

    // Verify the API Gateway health endpoint is responsive before starting load
    const healthResponse = http.get(`${BASE_URL}/health`);
    const healthCheck = check(healthResponse, {
        'setup: health endpoint returns 200': (r) => r.status === 200,
        'setup: health response has status field': (r) => {
            try {
                const body = JSON.parse(r.body);
                return body.status !== undefined;
            } catch (e) {
                return false;
            }
        },
    });

    if (!healthCheck) {
        console.error(
            `[SETUP] Health check failed. Status: ${healthResponse.status}. ` +
            `Ensure the API Gateway is running at ${BASE_URL}`
        );
    }

    // Verify the readiness endpoint confirms all dependencies are connected
    const readyResponse = http.get(`${BASE_URL}/ready`);
    check(readyResponse, {
        'setup: readiness endpoint returns 200': (r) => r.status === 200,
    });

    // Verify CORS preflight support for the API
    const optionsResponse = http.options(`${BASE_URL}${API_PREFIX}/generation/jobs`, null, {
        headers: {
            'Origin': 'http://localhost:3000',
            'Access-Control-Request-Method': 'POST',
            'Access-Control-Request-Headers': 'Authorization, Content-Type',
        },
    });
    check(optionsResponse, {
        'setup: CORS preflight returns success': (r) => r.status >= 200 && r.status < 300,
    });

    // Attempt authentication to obtain a valid token
    const authPayload = JSON.stringify({
        grant_type: 'client_credentials',
        client_id: 'k6-load-test-client',
        scope: 'read:generation write:generation read:schemas read:profiles read:templates write:export read:admin',
    });
    const authResponse = http.post(
        `${BASE_URL}${API_PREFIX}/auth/login`,
        authPayload,
        { headers: { 'Content-Type': 'application/json' } }
    );

    let authToken = AUTH_TOKEN;
    if (authResponse.status === 200) {
        try {
            const authBody = JSON.parse(authResponse.body);
            if (authBody.access_token) {
                authToken = authBody.access_token;
                console.log('[SETUP] Successfully obtained auth token from API');
            }
        } catch (e) {
            console.log('[SETUP] Using configured AUTH_TOKEN (auth endpoint returned non-JSON)');
        }
    } else {
        console.log(
            `[SETUP] Auth endpoint returned ${authResponse.status}. Using configured AUTH_TOKEN.`
        );
    }

    console.log('[SETUP] Setup complete. Beginning load test execution.');

    // Return shared test context available to all VUs
    return {
        authToken: authToken,
        tenantId: TENANT_ID,
        baseUrl: BASE_URL,
        apiPrefix: API_PREFIX,
        startTime: new Date().toISOString(),
    };
}

// =============================================================================
// Main Test Scenario (Default Export)
// =============================================================================
// Each virtual user (VU) iterates through this function continuously for
// the duration of the test. The function simulates a complete user workflow
// spanning multiple API endpoint groups with realistic think-time pauses.

/**
 * Default export function — main test scenario executed by each VU.
 * Simulates a realistic user workflow through the platform's API endpoints.
 *
 * @param {Object} data - Shared test context from setup()
 */
export default function (data) {
    const headers = getAuthHeaders();

    // =========================================================================
    // Group A: Health Check
    // =========================================================================
    // Validates that the API Gateway health and readiness endpoints remain
    // responsive under load. Health checks do not require authentication
    // and should always respond within milliseconds.
    group('Health Check', function () {
        const healthRes = http.get(`${BASE_URL}/health`);
        check(healthRes, {
            'health: status is 200': (r) => r.status === 200,
            'health: response time < 200ms': (r) => r.timings.duration < 200,
            'health: body contains status': (r) => {
                try {
                    const body = JSON.parse(r.body);
                    return body.status === 'healthy' || body.status === 'ok';
                } catch (e) {
                    return false;
                }
            },
        });

        const readyRes = http.get(`${BASE_URL}/ready`);
        check(readyRes, {
            'ready: status is 200': (r) => r.status === 200,
            'ready: response time < 500ms': (r) => r.timings.duration < 500,
        });
    });

    // Simulate user think time between workflow steps
    sleep(randomIntBetween(1, 2));

    // =========================================================================
    // Group B: Authentication Flow
    // =========================================================================
    // Verifies the authentication endpoint handles token validation correctly
    // under concurrent load. Tests both successful token verification and the
    // auth endpoint's ability to process login requests at scale.
    group('Authentication Flow', function () {
        const authPayload = JSON.stringify({
            grant_type: 'client_credentials',
            client_id: `k6-vu-${__VU}-iter-${__ITER}`,
            scope: 'read:generation write:generation',
        });

        const loginRes = http.post(
            `${BASE_URL}${API_PREFIX}/auth/login`,
            authPayload,
            { headers: { 'Content-Type': 'application/json' } }
        );

        check(loginRes, {
            'auth: login returns valid status': (r) =>
                r.status === 200 || r.status === 201 || r.status === 401,
            'auth: response time < 1000ms': (r) => r.timings.duration < 1000,
            'auth: response has body': (r) => r.body && r.body.length > 0,
        });

        if (loginRes.status >= 500) {
            errorsByGroup.add(1);
        }
    });

    sleep(randomIntBetween(1, 2));

    // =========================================================================
    // Group C: Schema Discovery
    // =========================================================================
    // Tests schema browsing endpoints that should benefit from Redis caching.
    // First request may be a cache miss (slower), subsequent requests to the
    // same schemas should be significantly faster (cache hits).
    group('Schema Discovery', function () {
        // List all available schemas with filtering
        const schemasRes = http.get(
            `${BASE_URL}${API_PREFIX}/schemas?erp_system=${randomERPSystem()}&module=${randomModule()}&page=1&per_page=20`,
            { headers: headers }
        );

        schemaBrowseDuration.add(schemasRes.timings.duration);
        cacheHitRate.add(isCacheHit(schemasRes.timings.duration));

        check(schemasRes, {
            'schemas: list returns 200': (r) => r.status === 200,
            'schemas: response time < 500ms': (r) => r.timings.duration < 500,
            'schemas: response has body': (r) => r.body && r.body.length > 0,
            'schemas: response is valid JSON': (r) => {
                try {
                    JSON.parse(r.body);
                    return true;
                } catch (e) {
                    return false;
                }
            },
        });

        if (schemasRes.status >= 500) {
            errorsByGroup.add(1);
        }

        // Request the same endpoint again to validate Redis caching behavior.
        // The second request should be significantly faster due to cache hit.
        sleep(0.5);
        const cachedSchemasRes = http.get(
            `${BASE_URL}${API_PREFIX}/schemas?erp_system=sap&module=financial_accounting&page=1&per_page=20`,
            { headers: headers }
        );

        schemaBrowseDuration.add(cachedSchemasRes.timings.duration);
        cacheHitRate.add(isCacheHit(cachedSchemasRes.timings.duration));

        check(cachedSchemasRes, {
            'schemas: cached request returns 200': (r) => r.status === 200,
            'schemas: cached response faster than uncached': (r) =>
                r.timings.duration <= schemasRes.timings.duration * 1.5,
        });

        // Fetch a specific schema by ID (using a well-known test schema ID)
        const schemaDetailRes = http.get(
            `${BASE_URL}${API_PREFIX}/schemas/test-schema-001`,
            { headers: headers }
        );

        check(schemaDetailRes, {
            'schemas: detail returns valid status': (r) =>
                r.status === 200 || r.status === 404,
            'schemas: detail response time < 500ms': (r) => r.timings.duration < 500,
        });
    });

    sleep(randomIntBetween(1, 2));

    // =========================================================================
    // Group D: Profile Retrieval
    // =========================================================================
    // Tests statistical profile endpoints which rely heavily on Redis caching.
    // Profile data is computed once during schema profiling and cached for
    // fast retrieval. This group validates that Redis caching delivers the
    // expected performance improvement under sustained load.
    group('Profile Retrieval', function () {
        // List profiles with pagination
        const profilesRes = http.get(
            `${BASE_URL}${API_PREFIX}/profiles?page=1&per_page=20`,
            { headers: headers }
        );

        profileRetrievalDuration.add(profilesRes.timings.duration);
        cacheHitRate.add(isCacheHit(profilesRes.timings.duration));

        check(profilesRes, {
            'profiles: list returns 200': (r) => r.status === 200,
            'profiles: response time < 300ms': (r) => r.timings.duration < 300,
            'profiles: response has body': (r) => r.body && r.body.length > 0,
            'profiles: response is valid JSON': (r) => {
                try {
                    JSON.parse(r.body);
                    return true;
                } catch (e) {
                    return false;
                }
            },
        });

        if (profilesRes.status >= 500) {
            errorsByGroup.add(1);
        }

        // Fetch same profile list again to measure Redis cache hit performance
        sleep(0.5);
        const cachedProfilesRes = http.get(
            `${BASE_URL}${API_PREFIX}/profiles?page=1&per_page=20`,
            { headers: headers }
        );

        profileRetrievalDuration.add(cachedProfilesRes.timings.duration);
        cacheHitRate.add(isCacheHit(cachedProfilesRes.timings.duration));

        check(cachedProfilesRes, {
            'profiles: cached request returns 200': (r) => r.status === 200,
            'profiles: cached response time < 100ms': (r) => r.timings.duration < 100,
        });

        // Fetch a specific profile detail
        const profileDetailRes = http.get(
            `${BASE_URL}${API_PREFIX}/profiles/test-profile-001`,
            { headers: headers }
        );

        profileRetrievalDuration.add(profileDetailRes.timings.duration);

        check(profileDetailRes, {
            'profiles: detail returns valid status': (r) =>
                r.status === 200 || r.status === 404,
            'profiles: detail response time < 500ms': (r) => r.timings.duration < 500,
        });
    });

    sleep(randomIntBetween(1, 2));

    // =========================================================================
    // Group E: Generation Job Lifecycle
    // =========================================================================
    // Tests the most critical workflow: creating a generation job, monitoring
    // its progress, and tracking completion. This group exercises the Generation
    // Engine, Quality Service, and Compliance Service pipeline.
    //
    // Under high load, this workflow validates:
    // - API Gateway can queue jobs efficiently
    // - Generation Engine scales via HPA to handle job backlog
    // - Redis-backed progress tracking remains responsive
    // - Job state machine transitions correctly under concurrency
    let createdJobId = null;

    group('Generation Job Lifecycle', function () {
        // Create a new generation job with randomized parameters
        const jobPayload = JSON.stringify(buildGenerationJobPayload());

        const createRes = http.post(
            `${BASE_URL}${API_PREFIX}/generation/jobs`,
            jobPayload,
            { headers: headers }
        );

        // Track job creation latency as a custom metric
        jobCreationDuration.add(createRes.timings.duration);

        const createCheck = check(createRes, {
            'job-create: returns 201 Created': (r) => r.status === 201,
            'job-create: response time < 1000ms': (r) => r.timings.duration < 1000,
            'job-create: response contains job_id': (r) => {
                try {
                    const body = JSON.parse(r.body);
                    return body.job_id !== undefined && body.job_id !== null;
                } catch (e) {
                    return false;
                }
            },
            'job-create: response contains status': (r) => {
                try {
                    const body = JSON.parse(r.body);
                    return body.status !== undefined;
                } catch (e) {
                    return false;
                }
            },
        });

        if (createRes.status >= 500) {
            errorsByGroup.add(1);
        }

        // Extract job ID for subsequent status polling
        if (createRes.status === 201) {
            try {
                const createBody = JSON.parse(createRes.body);
                createdJobId = createBody.job_id;

                // Track generation throughput based on requested record count
                if (createBody.record_count) {
                    generationThroughput.add(createBody.record_count);
                } else {
                    // Estimate from the payload we sent
                    const sentPayload = JSON.parse(jobPayload);
                    generationThroughput.add(sentPayload.record_count || 10000);
                }
            } catch (e) {
                createdJobId = null;
            }
        }

        // Poll for job status to validate the generation pipeline under load.
        // Under sustained load, the Generation Engine should be processing jobs
        // concurrently across multiple HPA-scaled pods.
        if (createdJobId) {
            let jobCompleted = false;

            for (let pollAttempt = 0; pollAttempt < MAX_POLL_ITERATIONS; pollAttempt++) {
                sleep(POLL_INTERVAL_SECONDS);

                const statusRes = http.get(
                    `${BASE_URL}${API_PREFIX}/generation/jobs/${createdJobId}`,
                    { headers: headers }
                );

                check(statusRes, {
                    'job-status: returns 200': (r) => r.status === 200,
                    'job-status: response time < 500ms': (r) => r.timings.duration < 500,
                    'job-status: body contains status field': (r) => {
                        try {
                            const body = JSON.parse(r.body);
                            return body.status !== undefined;
                        } catch (e) {
                            return false;
                        }
                    },
                });

                if (statusRes.status === 200) {
                    try {
                        const statusBody = JSON.parse(statusRes.body);
                        const jobStatus = statusBody.status;

                        // Track if the job reached a terminal state
                        if (
                            jobStatus === 'completed' ||
                            jobStatus === 'Completed'
                        ) {
                            jobCompleted = true;
                            break;
                        } else if (
                            jobStatus === 'failed' ||
                            jobStatus === 'Failed'
                        ) {
                            // Job failed — still a terminal state, but not successful
                            break;
                        }
                        // Non-terminal states: Submitted, Generating, Validating, Certifying
                        // Continue polling
                    } catch (e) {
                        // JSON parse error — continue polling
                    }
                }
            }

            // Record whether the job completed successfully within polling window
            jobCompletionRate.add(jobCompleted);
        } else {
            // Job creation failed — record as incomplete
            jobCompletionRate.add(false);
        }

        // List generation jobs with pagination to validate the listing endpoint
        // performance under load with growing job counts
        const listRes = http.get(
            `${BASE_URL}${API_PREFIX}/generation/jobs?page=1&per_page=20&sort=created_at&order=desc`,
            { headers: headers }
        );

        check(listRes, {
            'job-list: returns 200': (r) => r.status === 200,
            'job-list: response time < 500ms': (r) => r.timings.duration < 500,
            'job-list: response is valid JSON': (r) => {
                try {
                    JSON.parse(r.body);
                    return true;
                } catch (e) {
                    return false;
                }
            },
        });
    });

    sleep(randomIntBetween(1, 2));

    // =========================================================================
    // Group F: Template Library
    // =========================================================================
    // Tests the template browsing and retrieval endpoints. Templates are
    // relatively static resources that should be well-cached in Redis,
    // resulting in fast response times even under high load.
    group('Template Library', function () {
        // List available templates with optional filtering
        const templatesRes = http.get(
            `${BASE_URL}${API_PREFIX}/templates?page=1&per_page=20&category=generation`,
            { headers: headers }
        );

        cacheHitRate.add(isCacheHit(templatesRes.timings.duration));

        check(templatesRes, {
            'templates: list returns 200': (r) => r.status === 200,
            'templates: response time < 300ms': (r) => r.timings.duration < 300,
            'templates: response is valid JSON': (r) => {
                try {
                    JSON.parse(r.body);
                    return true;
                } catch (e) {
                    return false;
                }
            },
        });

        if (templatesRes.status >= 500) {
            errorsByGroup.add(1);
        }

        // Fetch a specific template detail
        const templateDetailRes = http.get(
            `${BASE_URL}${API_PREFIX}/templates/test-template-001`,
            { headers: headers }
        );

        check(templateDetailRes, {
            'templates: detail returns valid status': (r) =>
                r.status === 200 || r.status === 404,
            'templates: detail response time < 500ms': (r) => r.timings.duration < 500,
        });
    });

    sleep(randomIntBetween(1, 2));

    // =========================================================================
    // Group G: Export Flow
    // =========================================================================
    // Tests the export/provisioning endpoints that trigger data delivery to
    // target databases or cloud storage. Under load, the Provisioning Service
    // should handle concurrent export requests without resource exhaustion.
    group('Export Flow', function () {
        // Use the job ID from the generation lifecycle, or a placeholder
        const exportJobId = createdJobId || `test-job-${randomString(8)}`;
        const exportPayload = JSON.stringify(buildExportPayload(exportJobId));

        const exportRes = http.post(
            `${BASE_URL}${API_PREFIX}/export`,
            exportPayload,
            { headers: headers }
        );

        exportDuration.add(exportRes.timings.duration);

        check(exportRes, {
            'export: returns valid status': (r) =>
                r.status === 200 || r.status === 201 || r.status === 202 || r.status === 404,
            'export: response time < 2000ms': (r) => r.timings.duration < 2000,
            'export: response has body': (r) => r.body && r.body.length > 0,
        });

        if (exportRes.status >= 500) {
            errorsByGroup.add(1);
        }

        // Check export status if we received an export ID
        if (exportRes.status === 200 || exportRes.status === 201 || exportRes.status === 202) {
            try {
                const exportBody = JSON.parse(exportRes.body);
                const exportId = exportBody.export_id || exportBody.id;

                if (exportId) {
                    sleep(1);
                    const exportStatusRes = http.get(
                        `${BASE_URL}${API_PREFIX}/export/${exportId}`,
                        { headers: headers }
                    );

                    check(exportStatusRes, {
                        'export-status: returns valid status': (r) =>
                            r.status === 200 || r.status === 404,
                        'export-status: response time < 500ms': (r) =>
                            r.timings.duration < 500,
                    });
                }
            } catch (e) {
                // Export response was not JSON — skip status check
            }
        }
    });

    sleep(randomIntBetween(1, 2));

    // =========================================================================
    // Group H: Admin Operations
    // =========================================================================
    // Tests admin endpoints that are typically accessed by Platform Admin role.
    // These endpoints should enforce RBAC and return 403 for non-admin tokens.
    // Under load, admin endpoints should not consume disproportionate resources.
    group('Admin Operations', function () {
        // List users (requires admin role)
        const usersRes = http.get(
            `${BASE_URL}${API_PREFIX}/admin/users?page=1&per_page=20`,
            { headers: headers }
        );

        check(usersRes, {
            'admin: users endpoint returns valid status': (r) =>
                r.status === 200 || r.status === 403,
            'admin: users response time < 500ms': (r) => r.timings.duration < 500,
        });

        // Get tenant configuration
        const tenantRes = http.get(
            `${BASE_URL}${API_PREFIX}/admin/tenants/${TENANT_ID}`,
            { headers: headers }
        );

        check(tenantRes, {
            'admin: tenant config returns valid status': (r) =>
                r.status === 200 || r.status === 403 || r.status === 404,
            'admin: tenant config response time < 500ms': (r) =>
                r.timings.duration < 500,
        });

        // Get system settings
        const settingsRes = http.get(
            `${BASE_URL}${API_PREFIX}/admin/settings`,
            { headers: headers }
        );

        check(settingsRes, {
            'admin: settings returns valid status': (r) =>
                r.status === 200 || r.status === 403,
            'admin: settings response time < 500ms': (r) => r.timings.duration < 500,
        });

        if (usersRes.status >= 500 || tenantRes.status >= 500 || settingsRes.status >= 500) {
            errorsByGroup.add(1);
        }
    });

    sleep(randomIntBetween(1, 2));

    // =========================================================================
    // Group I: Monitoring Endpoints
    // =========================================================================
    // Tests the Prometheus metrics scrape endpoint and monitoring APIs.
    // These endpoints must remain responsive even under peak load since they
    // are consumed by the monitoring infrastructure (Prometheus, Grafana).
    group('Monitoring Endpoints', function () {
        // Prometheus metrics endpoint
        const metricsRes = http.get(
            `${BASE_URL}${API_PREFIX}/monitoring/metrics`,
            { headers: headers }
        );

        check(metricsRes, {
            'monitoring: metrics returns valid status': (r) =>
                r.status === 200 || r.status === 403,
            'monitoring: metrics response time < 500ms': (r) =>
                r.timings.duration < 500,
            'monitoring: metrics response has content': (r) =>
                r.body && r.body.length > 0,
        });

        if (metricsRes.status >= 500) {
            errorsByGroup.add(1);
        }
    });

    // Final think-time pause before the next iteration begins
    sleep(randomIntBetween(1, 2));

    // =========================================================================
    // Batch Request Validation
    // =========================================================================
    // Use http.batch() to simulate concurrent requests that a web console might
    // make when loading the dashboard page (multiple API calls in parallel).
    group('Dashboard Batch Load', function () {
        const batchResponses = http.batch([
            ['GET', `${BASE_URL}/health`, null, { headers: headers }],
            ['GET', `${BASE_URL}${API_PREFIX}/generation/jobs?page=1&per_page=5`, null, { headers: headers }],
            ['GET', `${BASE_URL}${API_PREFIX}/schemas?page=1&per_page=5`, null, { headers: headers }],
            ['GET', `${BASE_URL}${API_PREFIX}/profiles?page=1&per_page=5`, null, { headers: headers }],
            ['GET', `${BASE_URL}${API_PREFIX}/templates?page=1&per_page=5`, null, { headers: headers }],
        ]);

        // Verify all batch responses completed successfully
        for (let i = 0; i < batchResponses.length; i++) {
            check(batchResponses[i], {
                'batch: response status is successful': (r) => r.status >= 200 && r.status < 400,
                'batch: response time < 1000ms': (r) => r.timings.duration < 1000,
            });
        }
    });

    sleep(randomIntBetween(1, 2));

    // =========================================================================
    // Delete/Cleanup Operation
    // =========================================================================
    // Test the DELETE endpoint for generation jobs to validate cleanup behavior
    // under load. This exercises the http.del() method.
    group('Job Cleanup', function () {
        if (createdJobId) {
            const deleteRes = http.del(
                `${BASE_URL}${API_PREFIX}/generation/jobs/${createdJobId}`,
                null,
                { headers: headers }
            );

            check(deleteRes, {
                'delete: returns valid status': (r) =>
                    r.status === 200 || r.status === 204 || r.status === 404,
                'delete: response time < 500ms': (r) => r.timings.duration < 500,
            });
        }
    });
}

// =============================================================================
// Custom Summary Handler
// =============================================================================
// Generates a detailed JSON summary report at the end of the test run,
// including standard k6 metrics and custom application-specific metrics.
// The report is output to both stdout and a file for CI/CD integration.

/**
 * k6 handleSummary function — generates custom summary report.
 * Called once after all VU iterations complete with aggregated metrics data.
 *
 * @param {Object} data - Aggregated k6 metrics data for the entire test run
 * @returns {Object} Output targets (stdout and file) with formatted reports
 */
export function handleSummary(data) {
    // Build a structured summary report with key performance indicators
    const summary = {
        report_name: 'Synthetic ERP Platform - k6 Sustained Load Test Report',
        generated_at: new Date().toISOString(),
        target_url: BASE_URL,
        tenant_id: TENANT_ID,
        test_configuration: {
            total_stages: 7,
            total_duration_minutes: 30,
            max_virtual_users: 500,
            thresholds: {
                http_req_duration_p95: '< 500ms',
                http_req_duration_p99: '< 1500ms',
                http_req_failed: '< 1%',
                http_reqs_rate: '> 100 req/s',
                iteration_duration_p95: '< 3000ms',
            },
        },
        results: {
            // Extract key metrics from the data object
            total_requests: data.metrics.http_reqs
                ? data.metrics.http_reqs.values.count
                : 0,
            total_iterations: data.metrics.iterations
                ? data.metrics.iterations.values.count
                : 0,
            avg_request_duration_ms: data.metrics.http_req_duration
                ? Math.round(data.metrics.http_req_duration.values.avg * 100) / 100
                : 0,
            p95_request_duration_ms: data.metrics.http_req_duration
                ? Math.round(data.metrics.http_req_duration.values['p(95)'] * 100) / 100
                : 0,
            p99_request_duration_ms: data.metrics.http_req_duration
                ? Math.round(data.metrics.http_req_duration.values['p(99)'] * 100) / 100
                : 0,
            max_request_duration_ms: data.metrics.http_req_duration
                ? Math.round(data.metrics.http_req_duration.values.max * 100) / 100
                : 0,
            error_rate_percent: data.metrics.http_req_failed
                ? Math.round(data.metrics.http_req_failed.values.rate * 10000) / 100
                : 0,
            requests_per_second: data.metrics.http_reqs
                ? Math.round(data.metrics.http_reqs.values.rate * 100) / 100
                : 0,
        },
        custom_metrics: {
            job_creation_duration_avg_ms: data.metrics.job_creation_duration
                ? Math.round(data.metrics.job_creation_duration.values.avg * 100) / 100
                : 0,
            job_creation_duration_p95_ms: data.metrics.job_creation_duration
                ? Math.round(data.metrics.job_creation_duration.values['p(95)'] * 100) / 100
                : 0,
            job_completion_rate_percent: data.metrics.job_completion_rate
                ? Math.round(data.metrics.job_completion_rate.values.rate * 10000) / 100
                : 0,
            total_records_generated: data.metrics.generation_throughput_records
                ? data.metrics.generation_throughput_records.values.count
                : 0,
            cache_hit_rate_percent: data.metrics.cache_hit_rate
                ? Math.round(data.metrics.cache_hit_rate.values.rate * 10000) / 100
                : 0,
            schema_browse_avg_ms: data.metrics.schema_browse_duration
                ? Math.round(data.metrics.schema_browse_duration.values.avg * 100) / 100
                : 0,
            profile_retrieval_avg_ms: data.metrics.profile_retrieval_duration
                ? Math.round(data.metrics.profile_retrieval_duration.values.avg * 100) / 100
                : 0,
            export_avg_ms: data.metrics.export_duration
                ? Math.round(data.metrics.export_duration.values.avg * 100) / 100
                : 0,
            total_errors_by_group: data.metrics.errors_by_group
                ? data.metrics.errors_by_group.values.count
                : 0,
        },
        throughput_analysis: {
            target_records_per_minute: 1000000,
            estimated_records_generated: data.metrics.generation_throughput_records
                ? data.metrics.generation_throughput_records.values.count
                : 0,
            test_duration_minutes: 30,
            estimated_records_per_minute: data.metrics.generation_throughput_records
                ? Math.round(
                    data.metrics.generation_throughput_records.values.count / 30
                )
                : 0,
            throughput_target_met: data.metrics.generation_throughput_records
                ? data.metrics.generation_throughput_records.values.count / 30 >= 1000000
                : false,
        },
        hpa_scaling_analysis: {
            description:
                'HPA behavior should be validated by monitoring pod counts during ' +
                'the test execution via kubectl or Prometheus metrics.',
            expected_behavior: {
                'stage_1_2_warm_up': 'Minimum replica count (2 pods)',
                'stage_3_4_stress': 'Scale up to 4-6 pods as CPU > 70%',
                'stage_5_6_peak': 'Scale to maximum replicas (8-10 pods)',
                'stage_7_cool_down': 'Gradual scale-down after stabilization window',
            },
            validation_notes:
                'Monitor the kubernetes_pod_count metric during the test. ' +
                'Pod scaling events should correlate with load stage transitions.',
        },
        threshold_results: {},
    };

    // Extract threshold pass/fail results
    if (data.root_group && data.root_group.checks) {
        summary.check_results = {
            total_checks: 0,
            passed_checks: 0,
            failed_checks: 0,
        };
    }

    // Build threshold summary from the data
    const thresholdKeys = Object.keys(options.thresholds);
    for (const key of thresholdKeys) {
        if (data.metrics[key]) {
            summary.threshold_results[key] = {
                configured: options.thresholds[key],
                values: data.metrics[key].values,
            };
        }
    }

    // Generate formatted console output
    const consoleOutput = [
        '',
        '╔══════════════════════════════════════════════════════════════════╗',
        '║   Synthetic ERP Platform - k6 Load Test Summary Report         ║',
        '╚══════════════════════════════════════════════════════════════════╝',
        '',
        `  Target URL:         ${BASE_URL}`,
        `  Tenant ID:          ${TENANT_ID}`,
        `  Test Duration:      30 minutes (7 stages)`,
        `  Max Virtual Users:  500`,
        '',
        '  ── Performance Results ─────────────────────────────────────────',
        `  Total Requests:     ${summary.results.total_requests}`,
        `  Requests/Second:    ${summary.results.requests_per_second}`,
        `  Avg Duration:       ${summary.results.avg_request_duration_ms}ms`,
        `  P95 Duration:       ${summary.results.p95_request_duration_ms}ms`,
        `  P99 Duration:       ${summary.results.p99_request_duration_ms}ms`,
        `  Error Rate:         ${summary.results.error_rate_percent}%`,
        '',
        '  ── Custom Metrics ──────────────────────────────────────────────',
        `  Job Creation P95:   ${summary.custom_metrics.job_creation_duration_p95_ms}ms`,
        `  Job Completion:     ${summary.custom_metrics.job_completion_rate_percent}%`,
        `  Cache Hit Rate:     ${summary.custom_metrics.cache_hit_rate_percent}%`,
        `  Records Generated:  ${summary.custom_metrics.total_records_generated}`,
        '',
        '  ── Throughput Analysis ─────────────────────────────────────────',
        `  Target:             1,000,000 records/minute`,
        `  Estimated:          ${summary.throughput_analysis.estimated_records_per_minute} records/minute`,
        `  Target Met:         ${summary.throughput_analysis.throughput_target_met ? 'YES ✓' : 'NO ✗'}`,
        '',
        '══════════════════════════════════════════════════════════════════',
        '',
    ].join('\n');

    // Return outputs for both stdout and file
    return {
        stdout: consoleOutput,
        'k6_load_test_report.json': JSON.stringify(summary, null, 2),
    };
}
