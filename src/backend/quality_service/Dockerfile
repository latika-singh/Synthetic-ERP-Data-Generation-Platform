# =============================================================================
# Dockerfile — Quality Service
# Synthetic-ERP-Data-Generation-Platform
#
# Multi-stage Docker build for the Quality Service.
#
# Stage 1 (builder):
#   Compiles native extensions (scipy C/Fortran bindings, numpy, cryptography)
#   and installs all Python dependencies into an isolated virtual environment.
#   Build tools (gcc, g++, gfortran, python3-dev) are present ONLY in this
#   stage and are discarded before the production image, keeping it secure
#   and minimal.
#
# Stage 2 (production):
#   Minimal runtime image with Gunicorn 21.x WSGI server, non-root user for
#   security hardening (SOC 2 Type II — Constraint C-004), Docker HEALTHCHECK,
#   and tini for proper PID 1 signal handling in Kubernetes.
#
# Build context: src/backend/ (set via docker-compose.yml or CI/CD pipeline)
#
# Build examples:
#   docker build -f quality_service/Dockerfile -t quality-service:latest .
#   docker build -f quality_service/Dockerfile \
#       --build-arg BUILD_DATE=$(date -u +'%Y-%m-%dT%H:%M:%SZ') \
#       --build-arg VCS_REF=$(git rev-parse --short HEAD) \
#       -t quality-service:latest .
#
# Run:
#   docker run -p 5004:5004 --env-file .env quality-service:latest
#
# Air-gapped deployment (Constraint C-003):
#   For environments without internet access, use a private PyPI mirror:
#   docker build --build-arg PIP_INDEX_URL=https://private-pypi.internal/simple/ \
#       -f quality_service/Dockerfile -t quality-service:latest .
# =============================================================================

# ---------------------------------------------------------------------------
# Global build arguments
# ---------------------------------------------------------------------------
ARG PYTHON_VERSION=3.12
ARG BUILD_DATE
ARG VCS_REF


# =============================================================================
# Stage 1 — Builder
#
# Installs system-level build dependencies (gcc, g++, gfortran, libffi,
# libssl) for compiling native Python extensions required by scipy,
# numpy, great-expectations, and cryptography. Creates an isolated virtual
# environment with all pip packages from requirements.txt.
# =============================================================================
FROM python:${PYTHON_VERSION}-slim AS builder

# Prevent .pyc files and enable unbuffered stdout/stderr during build
ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

WORKDIR /build

# Install system-level build dependencies required for compiling native Python
# extensions:
#   - gcc, g++: C/C++ compilers for scipy, numpy, and cryptography extensions
#   - gfortran: Fortran compiler required by scipy BLAS/LAPACK routines
#   - python3-dev: Python development headers for C extension compilation
#   - libffi-dev: Foreign Function Interface library for cffi-based packages
#   - libssl-dev: OpenSSL development headers for the cryptography package
#   - build-essential: Meta-package ensuring complete build toolchain
#   - libopenblas-dev: Optimized BLAS implementation for numpy/scipy
# These are only needed at build time and will NOT be present in the
# production image, keeping it minimal and secure.
RUN apt-get update \
    && apt-get install -y --no-install-recommends \
        gcc \
        g++ \
        gfortran \
        python3-dev \
        libffi-dev \
        libssl-dev \
        build-essential \
        libopenblas-dev \
    && rm -rf /var/lib/apt/lists/*

# Create an isolated virtual environment for all Python dependencies.
# Using a venv allows clean COPY to the production stage without dragging
# in system-level build artifacts.
RUN python -m venv /opt/venv
ENV PATH="/opt/venv/bin:$PATH"

# Copy requirements.txt FIRST to maximize Docker layer caching.
# If dependencies have not changed, Docker will reuse the cached layer even
# when application source code changes.
COPY quality_service/requirements.txt ./requirements.txt

# Install all Python dependencies from requirements.txt into the virtual
# environment. The --no-cache-dir flag prevents pip from storing download
# caches, reducing the builder image size. Great Expectations, SciPy, NumPy,
# Pandas, and cryptography require native compilation handled by the build
# dependencies installed above.
RUN pip install --no-cache-dir --upgrade pip setuptools wheel \
    && pip install --no-cache-dir -r requirements.txt

# Verify dependency consistency — catch conflicts or missing packages early
# in the build pipeline rather than at container startup.
RUN pip check


# =============================================================================
# Stage 2 — Production
#
# Minimal runtime image without build tools, compilers, or package manager
# caches. Runs the Flask application via Gunicorn as a non-root user.
# =============================================================================
FROM python:${PYTHON_VERSION}-slim AS production

# Re-declare build args needed for labels
ARG BUILD_DATE
ARG VCS_REF

# ---------------------------------------------------------------------------
# OCI-compliant image labels for metadata and traceability
# ---------------------------------------------------------------------------
LABEL maintainer="Synthetic-ERP-Data-Generation-Platform Team" \
      description="Quality Service — Data quality validation with Great Expectations, weighted scoring model (40% statistical, 30% business rules, 30% referential integrity) targeting >=95% fidelity for the Synthetic ERP Data Generation Platform." \
      version="1.0.0" \
      org.opencontainers.image.title="quality-service" \
      org.opencontainers.image.description="Quality Service with weighted quality scoring and Great Expectations validation" \
      org.opencontainers.image.version="1.0.0" \
      org.opencontainers.image.created="${BUILD_DATE}" \
      org.opencontainers.image.revision="${VCS_REF}" \
      org.opencontainers.image.vendor="Synthetic-ERP-Data-Generation-Platform" \
      org.opencontainers.image.source="https://github.com/synthetic-erp/platform" \
      org.opencontainers.image.base.name="python:${PYTHON_VERSION}-slim"

# ---------------------------------------------------------------------------
# Install minimal runtime system dependencies.
# No compilers, dev headers, or build tools — only what the application
# needs at runtime.
#   - curl: Used by Docker HEALTHCHECK probe and debugging
#   - tini: Lightweight init system for proper signal handling (PID 1)
#   - libopenblas0: Runtime BLAS library required by numpy/scipy
#   - libgomp1: OpenMP runtime for parallel scipy/numpy operations
# ---------------------------------------------------------------------------
RUN apt-get update \
    && apt-get install -y --no-install-recommends \
        curl \
        tini \
        libopenblas0 \
        libgomp1 \
    && rm -rf /var/lib/apt/lists/*

# ---------------------------------------------------------------------------
# Create a non-root system user and group for running the application.
# Running as non-root is a security best practice and required for SOC 2
# Type II compliance (Constraint C-004). The user has no home directory and
# cannot log in interactively.
# ---------------------------------------------------------------------------
RUN addgroup --system appuser \
    && adduser --system --ingroup appuser --no-create-home --disabled-password appuser

# ---------------------------------------------------------------------------
# Copy the Python virtual environment from the builder stage.
# This includes all installed pip packages (Great Expectations, SciPy,
# NumPy, Pandas, Flask, Gunicorn, etc.), ensuring air-gapped operation
# (Constraint C-003) since all dependencies are baked into the image.
# ---------------------------------------------------------------------------
COPY --from=builder /opt/venv /opt/venv

# ---------------------------------------------------------------------------
# Application code
#
# Build context is expected to be src/backend/ so that both the shared
# utilities package and the quality_service package are accessible.
# The docker-compose.yml or CI/CD pipeline sets:
#   context: src/backend/
#   dockerfile: quality_service/Dockerfile
# ---------------------------------------------------------------------------
WORKDIR /app

# Copy the shared utilities package (database, auth, logging, observability,
# middleware, config) used by all backend microservices. The Quality Service
# imports from shared.database.mongodb, shared.database.redis_client,
# shared.auth.jwt_handler, shared.logging.structured_logger,
# shared.observability.tracing, shared.observability.metrics,
# shared.middleware.circuit_breaker, and shared.middleware.health_check.
COPY shared/ /app/shared/

# Copy the Quality Service application code.
COPY quality_service/ /app/quality_service/

# Ensure the non-root user owns all application files.
RUN chown -R appuser:appuser /app

# ---------------------------------------------------------------------------
# Environment Variables
# ---------------------------------------------------------------------------
# Configure the Python and Flask runtime environment following 12-factor
# app methodology. All service-specific settings (MongoDB URI, Redis URL,
# quality thresholds, Auth0 config) are provided via environment variables
# at container runtime (Docker Compose, Kubernetes ConfigMaps/Secrets).
# No credentials or secrets are baked into the image.
# ---------------------------------------------------------------------------

# Python runtime settings
ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PYTHONPATH=/app \
    PATH="/opt/venv/bin:$PATH"

# Flask application settings — the FLASK_APP value points to the Application
# Factory so that Gunicorn can call create_app() to initialize the service
# with all Blueprints, middleware, extensions, and configuration.
ENV FLASK_APP=quality_service.app:create_app \
    FLASK_ENV=production

# Service identification for logging and tracing
ENV SERVICE_NAME=quality-service

# OpenTelemetry service identification for distributed tracing across
# the microservice architecture (traces propagated via W3C TraceContext).
ENV OTEL_SERVICE_NAME=quality-service \
    OTEL_EXPORTER_OTLP_ENDPOINT=http://otel-collector:4317

# ---------------------------------------------------------------------------
# Network Configuration
# ---------------------------------------------------------------------------
# Expose the Quality Service HTTP port. This port is used for:
#   - REST API endpoints for quality validation and scoring
#   - Health check and readiness probe endpoints (/health, /ready)
#   - Prometheus metrics scraping endpoint (/metrics)
# The actual port binding is controlled by the Gunicorn --bind flag in CMD
# and the Kubernetes Service definition.
# ---------------------------------------------------------------------------
EXPOSE 5004

# ---------------------------------------------------------------------------
# Docker HEALTHCHECK
#
# Uses Python's built-in urllib (no additional dependencies required) to
# probe the /health liveness endpoint. Parameters:
#   --interval=30s    : Check every 30 seconds
#   --timeout=10s     : Fail if no response within 10 seconds
#   --start-period=30s: Grace period for Great Expectations initialization
#                       and MongoDB/Redis connection setup at startup
#   --retries=3       : Mark unhealthy after 3 consecutive failures
# ---------------------------------------------------------------------------
HEALTHCHECK --interval=30s --timeout=10s --start-period=30s --retries=3 \
    CMD python -c "import urllib.request; urllib.request.urlopen('http://localhost:5004/health')" || exit 1

# ---------------------------------------------------------------------------
# Switch to the non-root user for all subsequent commands and at runtime.
# ---------------------------------------------------------------------------
USER appuser

# ---------------------------------------------------------------------------
# Container entrypoint and command
#
# tini (https://github.com/krallin/tini) is used as PID 1 to properly handle
# SIGTERM signal forwarding to Gunicorn, zombie process reaping, and graceful
# shutdown — critical for Kubernetes pod lifecycle management.
#
# Gunicorn 21.x serves the Flask WSGI application with:
#   --bind 0.0.0.0:5004 : Listen on all interfaces, Quality Service port
#   --workers 4          : 4 worker processes for concurrent validation
#   --threads 2          : 2 threads per worker for I/O-bound quality checks
#   --timeout 120        : 120s worker timeout for large dataset validations
#   --graceful-timeout 30: 30s grace period for in-flight validation requests
#                          during Kubernetes rolling updates
#   --keep-alive 5       : TCP keep-alive for connection reuse
#   --worker-tmp-dir /dev/shm : Use tmpfs for heartbeat files (K8s best
#                                practice to avoid disk I/O)
#   --access-logfile -   : Stream access logs to stdout for container log
#                          collection (ELK/Fluentd/CloudWatch)
#   --error-logfile -    : Stream error logs to stderr for container log
#                          collection
#   --log-level info     : Gunicorn log level (overrideable at runtime)
# ---------------------------------------------------------------------------
ENTRYPOINT ["tini", "--"]

CMD ["gunicorn", \
     "--bind", "0.0.0.0:5004", \
     "--workers", "4", \
     "--threads", "2", \
     "--timeout", "120", \
     "--graceful-timeout", "30", \
     "--keep-alive", "5", \
     "--access-logfile", "-", \
     "--error-logfile", "-", \
     "--log-level", "info", \
     "--worker-tmp-dir", "/dev/shm", \
     "quality_service.wsgi:app"]
