# ============================================================================
# Synthetic ERP Data Generation Platform — API Gateway Dockerfile
# ============================================================================
# Production-optimized multi-stage build for the Flask REST API Gateway.
# This is the primary entry point for all client requests, handling JWT
# authentication via Auth0, tiered rate limiting backed by Redis,
# Blueprint-based modular routing, and request forwarding to all
# downstream microservices (Generation Engine, Profiling, Quality,
# Compliance, Provisioning).
#
# Build context: Project root (.)
# Build command:
#   docker build -f infrastructure/docker/api-gateway.Dockerfile \
#     --build-arg BUILD_DATE=$(date -u +"%Y-%m-%dT%H:%M:%SZ") \
#     --build-arg VCS_REF=$(git rev-parse --short HEAD) \
#     -t synthetic-erp-api-gateway:latest .
#
# For air-gapped environments (C-003):
#   docker build -f infrastructure/docker/api-gateway.Dockerfile \
#     --build-arg PIP_INDEX_URL=https://private-pypi.internal/simple \
#     --build-arg PIP_TRUSTED_HOST=private-pypi.internal \
#     -t synthetic-erp-api-gateway:latest .
# ============================================================================

# ---------------------------------------------------------------------------
# Build arguments for image metadata and air-gapped registry support
# ---------------------------------------------------------------------------
ARG PYTHON_VERSION=3.12
ARG BUILD_DATE
ARG VCS_REF
# Air-gapped support: override PIP_INDEX_URL to point to a private PyPI mirror
ARG PIP_INDEX_URL=https://pypi.org/simple
ARG PIP_TRUSTED_HOST=pypi.org

# ============================================================================
# Stage 1: Builder
# Purpose: Install system-level build dependencies and Python packages into
#          an isolated virtual environment. This stage is discarded in the
#          final image to minimize attack surface and image size.
# ============================================================================
FROM python:${PYTHON_VERSION}-slim AS builder

# Metadata for the builder stage (informational)
LABEL stage=builder

# Set working directory for the build context
WORKDIR /build

# Install system-level build dependencies required for compiling native
# Python extensions:
#   - gcc: C compiler for native extensions (cryptography, pydantic-core)
#   - python3-dev: Python development headers for C extension compilation
#   - libffi-dev: Foreign function interface library for cffi/cryptography
# These are only needed at build time and are NOT included in the runtime image.
RUN apt-get update && \
    apt-get install -y --no-install-recommends \
        gcc \
        python3-dev \
        libffi-dev \
    && rm -rf /var/lib/apt/lists/*

# Create an isolated Python virtual environment at /opt/venv.
# Using a venv ensures clean separation of build artifacts from the system
# Python, enabling efficient multi-stage COPY of only the venv to runtime.
RUN python -m venv /opt/venv

# Prepend the virtual environment bin directory to PATH so all subsequent
# pip and python commands use the venv rather than the system Python.
ENV PATH="/opt/venv/bin:$PATH"

# Copy only the requirements file first to leverage Docker layer caching.
# If requirements.txt hasn't changed, Docker reuses the cached pip install
# layer, significantly speeding up rebuilds during development.
COPY src/backend/api_gateway/requirements.txt /build/requirements.txt

# Install all Python dependencies into the virtual environment.
# --no-cache-dir: Prevents pip from caching downloaded packages, reducing
#   the builder layer size.
# Key packages installed:
#   - flask 3.1.x: Lightweight REST API framework
#   - flask-restful: Resource-based REST API structure
#   - flask-jwt-extended: JWT token authentication with Auth0 integration
#   - flask-cors: Cross-origin request handling for Web Console
#   - gunicorn 21.x: Production WSGI HTTP server
#   - pymongo 4.x: MongoDB 7.0 driver for metadata persistence
#   - redis 5.x: Redis 7.x client for caching and rate limiting
#   - pydantic 2.x: Data validation and schema models
#   - structlog 24.x: Structured JSON logging
#   - opentelemetry-api, opentelemetry-sdk: Distributed tracing
#   - opentelemetry-instrumentation-flask: Flask auto-instrumentation
#   - prometheus-client: Prometheus metrics exporter
#   - python-jose: JWT signing and verification (RS256)
#   - cryptography: AES-256 encryption primitives
#   - circuitbreaker: Circuit breaker pattern for resilience
ARG PIP_INDEX_URL
ARG PIP_TRUSTED_HOST
RUN pip install --no-cache-dir --upgrade pip setuptools wheel && \
    pip install --no-cache-dir \
        --index-url "${PIP_INDEX_URL}" \
        --trusted-host "${PIP_TRUSTED_HOST}" \
        -r /build/requirements.txt

# Verify that all installed packages have consistent dependency versions.
# This catches silent version conflicts that could cause runtime failures.
RUN pip check

# ============================================================================
# Stage 2: Runtime
# Purpose: Minimal production image containing only the Python virtual
#          environment (with all dependencies) and the application source
#          code. No build tools, compilers, or development headers are
#          included, minimizing the attack surface per security requirements.
# ============================================================================
FROM python:${PYTHON_VERSION}-slim AS runtime

# ---------------------------------------------------------------------------
# OCI Image Specification Labels
# Provides standardized container metadata for registry discovery,
# vulnerability scanning tools, and deployment automation.
# ---------------------------------------------------------------------------
LABEL org.opencontainers.image.title="synthetic-erp-api-gateway" \
      org.opencontainers.image.description="Flask REST API Gateway with JWT auth, rate limiting, and Blueprint routing" \
      org.opencontainers.image.source="infrastructure/docker/api-gateway.Dockerfile" \
      org.opencontainers.image.vendor="Synthetic ERP Platform" \
      org.opencontainers.image.created="${BUILD_DATE}" \
      org.opencontainers.image.revision="${VCS_REF}" \
      org.opencontainers.image.licenses="Proprietary"

# Install minimal runtime dependencies only — no compilers or dev headers.
#   - curl: Required for Docker/Kubernetes health check probes (HEALTHCHECK CMD)
#   - ca-certificates: Root CA bundle for HTTPS connections to Auth0 identity
#     provider, inter-service TLS communication, and external API calls
# The apt cache is cleaned immediately to minimize the image layer size.
RUN apt-get update && \
    apt-get install -y --no-install-recommends \
        curl \
        ca-certificates \
    && rm -rf /var/lib/apt/lists/*

# ---------------------------------------------------------------------------
# Environment Variables
# ---------------------------------------------------------------------------
# Python runtime behavior:
#   PYTHONDONTWRITEBYTECODE=1: Prevent .pyc file generation (cleaner container)
#   PYTHONUNBUFFERED=1: Force stdout/stderr streams to be unbuffered for
#     real-time log output in Docker/Kubernetes log collectors
ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1

# Flask application configuration:
#   FLASK_APP: Application factory entry point for Flask CLI and Gunicorn
#   FLASK_ENV: Production mode (disables debug, reloader, and interactive debugger)
#   PYTHONPATH: Ensures Python can locate the api_gateway and shared packages
ENV FLASK_APP="api_gateway.app:create_app()" \
    FLASK_ENV=production \
    PYTHONPATH=/app

# Service identification for logging, tracing, and monitoring:
#   SERVICE_NAME: Used by structured logger for service identification in logs
#   OTEL_SERVICE_NAME: OpenTelemetry service name for distributed trace spans
#   OTEL_EXPORTER_OTLP_ENDPOINT: Default OTLP exporter endpoint; overrideable
#     at container runtime via docker-compose or Kubernetes ConfigMap/env vars
ENV SERVICE_NAME="api-gateway" \
    OTEL_SERVICE_NAME="api-gateway" \
    OTEL_EXPORTER_OTLP_ENDPOINT="http://otel-collector:4317"

# ---------------------------------------------------------------------------
# Non-Root User Configuration (Security Requirement)
# ---------------------------------------------------------------------------
# Create a dedicated system group and user with no login shell, no home
# directory, and no password. Running as non-root is a container security
# best practice and is required for SOC 2 Type II compliance (C-004).
# The appuser has no elevated privileges, limiting the blast radius of
# any container compromise.
RUN addgroup --system appuser && \
    adduser --system --ingroup appuser --no-create-home appuser

# ---------------------------------------------------------------------------
# Copy Virtual Environment from Builder Stage
# ---------------------------------------------------------------------------
# Copy the complete virtual environment containing all installed Python
# packages from the builder stage. This is the key multi-stage optimization:
# the runtime image gets only the compiled packages without any build tools.
COPY --from=builder /opt/venv /opt/venv

# Ensure the virtual environment's bin directory is first in PATH so
# python, gunicorn, and all entry-point scripts resolve to the venv.
ENV PATH="/opt/venv/bin:$PATH"

# ---------------------------------------------------------------------------
# Application Code
# ---------------------------------------------------------------------------
# Set the working directory where the application code will reside.
WORKDIR /app

# Copy the API Gateway service code. The COPY context is the project root,
# so paths are relative to the repository root directory.
# The api_gateway package contains: app.py (factory), config.py, routes/,
# middleware/, schemas/, services/, models/, utils/, and extensions.py.
COPY src/backend/api_gateway/ /app/api_gateway/

# Copy the shared utilities library used by all backend services.
# Contains: database/ (MongoDB, Redis clients), auth/ (JWT, RBAC),
# logging/ (structured logger), observability/ (metrics, tracing),
# config/ (base config), middleware/ (circuit breaker, health check).
COPY src/backend/shared/ /app/shared/

# Set recursive ownership of all application files to the non-root user.
# This ensures appuser can read all source files and write to any
# application-managed directories if needed.
RUN chown -R appuser:appuser /app

# ---------------------------------------------------------------------------
# Switch to Non-Root User
# ---------------------------------------------------------------------------
# All subsequent commands and the container entrypoint run as appuser.
# This is enforced at image level — even if the container orchestrator
# doesn't set a security context, the process runs unprivileged.
USER appuser

# ---------------------------------------------------------------------------
# Network Configuration
# ---------------------------------------------------------------------------
# Expose port 5000 for the Flask/Gunicorn HTTP server.
# This is the port the API Gateway listens on for incoming REST API requests
# from the Web Console, CI/CD integrations, and external API consumers.
# Kubernetes Service and Docker Compose port mappings reference this port.
EXPOSE 5000

# ---------------------------------------------------------------------------
# Health Check Configuration
# ---------------------------------------------------------------------------
# Docker HEALTHCHECK uses curl to probe the /health endpoint served by the
# Flask health check Blueprint. This endpoint returns HTTP 200 when the
# service is operational and all critical dependencies (MongoDB, Redis) are
# reachable.
#
# Parameters:
#   --interval=30s: Check every 30 seconds
#   --timeout=5s: Fail if /health doesn't respond within 5 seconds
#   --start-period=10s: Grace period after container start before first check
#                       (allows Gunicorn worker initialization and Flask app setup)
#   --retries=3: Mark unhealthy after 3 consecutive failures
#
# In Kubernetes, the liveness/readiness probes in the Deployment manifest
# typically override this HEALTHCHECK, but it serves as a fallback for
# Docker Compose and standalone Docker deployments.
HEALTHCHECK --interval=30s --timeout=5s --start-period=10s --retries=3 \
    CMD curl -f http://localhost:5000/health || exit 1

# ---------------------------------------------------------------------------
# Container Entrypoint — Gunicorn WSGI Server
# ---------------------------------------------------------------------------
# Run the Flask application via Gunicorn, the production WSGI server.
#
# Gunicorn configuration:
#   --bind 0.0.0.0:5000   : Listen on all interfaces at port 5000
#   --workers 4            : 4 worker processes for parallel request handling
#   --threads 2            : 2 threads per worker (gthread worker class)
#                            Total concurrency: 4 workers × 2 threads = 8
#   --timeout 120          : Worker timeout in seconds; prevents hung requests
#                            from consuming worker slots indefinitely
#   --access-logfile -     : Stream access logs to stdout for Docker log driver
#   --error-logfile -      : Stream error logs to stderr for Docker log driver
#   --preload              : Preload the Flask application in the master process
#                            before forking workers. Benefits:
#                            - Faster worker startup (shared memory for app code)
#                            - Memory savings via copy-on-write fork semantics
#                            - Earlier detection of import/startup errors
#   api_gateway.app:create_app()
#                          : Application factory entry point. Gunicorn calls
#                            create_app() to obtain the Flask WSGI application
#                            instance with all Blueprints, middleware, and
#                            extensions registered.
#
# Workers/threads sizing rationale:
#   - 4 workers: Appropriate for 2-4 CPU core pods in Kubernetes
#   - 2 threads: Handles I/O-bound operations (MongoDB, Redis, Auth0 calls)
#     without the overhead of full async workers
#   - Scale horizontally via Kubernetes HPA rather than increasing per-pod workers
CMD ["gunicorn", \
     "--bind", "0.0.0.0:5000", \
     "--workers", "4", \
     "--threads", "2", \
     "--timeout", "120", \
     "--access-logfile", "-", \
     "--error-logfile", "-", \
     "--preload", \
     "api_gateway.app:create_app()"]
