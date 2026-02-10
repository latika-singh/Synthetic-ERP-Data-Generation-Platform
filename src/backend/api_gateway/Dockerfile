# =============================================================================
# Dockerfile — API Gateway Service
# Synthetic ERP Data Generation Platform
# =============================================================================
#
# Multi-stage Docker build for the API Gateway service (Flask 3.1.x + Gunicorn).
# Produces a minimal, security-hardened production image using python:3.12-slim.
#
# Build context: src/backend/  (must include api_gateway/ and shared/ directories)
#
# Usage from project root:
#   docker build -f src/backend/api_gateway/Dockerfile -t synthetic-erp-api-gateway src/backend/
#
# Usage from src/backend/:
#   docker build -f api_gateway/Dockerfile -t synthetic-erp-api-gateway .
#
# Air-gapped deployment (C-003):
#   For private registry environments, replace the base image references:
#     FROM <private-registry>/python:3.12-slim AS builder
#     FROM <private-registry>/python:3.12-slim AS runtime
#   Ensure all pip packages are available from a local PyPI mirror:
#     pip install --no-cache-dir --index-url https://pypi.internal.corp/simple -r requirements.txt
#
# Recommended .dockerignore entries (place in build context root):
#   __pycache__/
#   *.pyc
#   *.pyo
#   .pytest_cache/
#   .mypy_cache/
#   .ruff_cache/
#   .venv/
#   venv/
#   *.egg-info/
#   .git/
#   .env
#   .env.*
#   tests/
#   docs/
#   *.md
#   Makefile
# =============================================================================

# ---------------------------------------------------------------------------
# Build arguments — configurable at build time for flexibility
# ---------------------------------------------------------------------------
ARG PYTHON_VERSION=3.12
ARG BUILD_DATE
ARG VCS_REF
ARG VERSION=1.0.0

# =============================================================================
# Stage 1: Builder — Install and compile Python dependencies
# =============================================================================
# Uses the full python:3.12-slim image with build tools to compile native
# extensions (cryptography, pydantic-core, etc.) into a virtual environment
# that can be copied to the lean runtime stage.
# =============================================================================
FROM python:${PYTHON_VERSION}-slim AS builder

# Set working directory for the build stage
WORKDIR /build

# Install system-level build dependencies required for compiling native
# Python packages. These are NOT carried into the runtime image.
#   gcc / g++       — C/C++ compiler for native extensions
#   python3-dev     — Python header files for CPython API modules
#   libffi-dev      — Foreign Function Interface for cryptography / cffi
#   make            — Build tool occasionally required by pip packages
RUN apt-get update && \
    apt-get install -y --no-install-recommends \
        gcc \
        g++ \
        python3-dev \
        libffi-dev \
        make \
    && rm -rf /var/lib/apt/lists/*

# Create an isolated virtual environment so all installed packages
# live in a single, portable directory tree (/opt/venv).
RUN python -m venv /opt/venv

# Activate the virtual environment by prepending it to PATH.
# All subsequent pip installs and python invocations use this venv.
ENV PATH="/opt/venv/bin:$PATH"

# Upgrade pip and setuptools to the latest versions for faster, more
# reliable dependency resolution and wheel builds.
RUN pip install --no-cache-dir --upgrade pip setuptools wheel

# ---- Layer-caching optimisation ----
# Copy ONLY the requirements file first. Docker will cache this layer
# and only re-run pip install when requirements.txt actually changes,
# saving significant rebuild time during development.
COPY api_gateway/requirements.txt /build/requirements.txt

# Install all Python dependencies into the virtual environment.
# --no-cache-dir keeps the image layer small by not caching wheels.
# For air-gapped deployment, add: --index-url https://pypi.internal.corp/simple
RUN pip install --no-cache-dir -r requirements.txt

# Copy the shared utilities package so it is available for any build-time
# validation (e.g. import checks). It will also be copied at runtime.
COPY shared/ /build/shared/

# Verify that all installed packages are consistent and no dependency
# conflicts exist in the virtual environment.
RUN pip check

# =============================================================================
# Stage 2: Runtime — Minimal production image
# =============================================================================
# A clean python:3.12-slim image with no build tools. Only the pre-built
# virtual environment and application source code are copied in, yielding
# a significantly smaller attack surface and image size.
# =============================================================================
FROM python:${PYTHON_VERSION}-slim AS runtime

# ---------------------------------------------------------------------------
# OCI image metadata labels (opencontainers.org standard)
# ---------------------------------------------------------------------------
LABEL org.opencontainers.image.title="synthetic-erp-api-gateway" \
      org.opencontainers.image.description="Flask REST API Gateway for the Synthetic ERP Data Generation Platform — JWT auth, rate limiting, Blueprint routing" \
      org.opencontainers.image.version="${VERSION}" \
      org.opencontainers.image.created="${BUILD_DATE}" \
      org.opencontainers.image.revision="${VCS_REF}" \
      org.opencontainers.image.source="src/backend/api_gateway/Dockerfile" \
      org.opencontainers.image.vendor="Synthetic ERP Platform" \
      org.opencontainers.image.licenses="MIT" \
      maintainer="platform-engineering@synthetic-erp.io"

# ---------------------------------------------------------------------------
# Environment variables for Python runtime behaviour
# ---------------------------------------------------------------------------
# PYTHONDONTWRITEBYTECODE  — Prevent .pyc file generation (cleaner container FS)
# PYTHONUNBUFFERED         — Force stdout/stderr to be unbuffered for real-time logs
# FLASK_ENV                — Flask environment identifier for production config
# PYTHONPATH               — Ensure /app is on the module search path so both
#                            api_gateway and shared packages are importable
# SERVICE_NAME             — Identifies this service in logs and tracing spans
# OTEL_SERVICE_NAME        — OpenTelemetry service identification for distributed tracing
# OTEL_EXPORTER_OTLP_ENDPOINT — Default OTel collector endpoint (overrideable at runtime)
ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    FLASK_ENV=production \
    PYTHONPATH=/app \
    PATH="/opt/venv/bin:$PATH" \
    SERVICE_NAME=api-gateway \
    OTEL_SERVICE_NAME=api-gateway \
    OTEL_EXPORTER_OTLP_ENDPOINT=http://otel-collector:4317

# ---------------------------------------------------------------------------
# Install minimal runtime system packages
# ---------------------------------------------------------------------------
#   curl            — Required for the HEALTHCHECK probe
#   ca-certificates — Required for TLS connections to Auth0, inter-service HTTPS,
#                     MongoDB Atlas (if using cloud), and external OAuth providers
RUN apt-get update && \
    apt-get install -y --no-install-recommends \
        curl \
        ca-certificates \
    && rm -rf /var/lib/apt/lists/*

# ---------------------------------------------------------------------------
# Copy the pre-built virtual environment from the builder stage
# ---------------------------------------------------------------------------
# This single COPY pulls in every compiled package, avoiding the need
# for gcc / python3-dev in the runtime image.
COPY --from=builder /opt/venv /opt/venv

# ---------------------------------------------------------------------------
# Create a non-root system user for running the application (security best practice)
# ---------------------------------------------------------------------------
# Running as a non-root user limits the blast radius of any container escape
# or application-level vulnerability. The user has no login shell and no
# home directory beyond /app.
RUN addgroup --system app && \
    adduser --system --ingroup app --no-create-home app

# ---------------------------------------------------------------------------
# Set up the application directory
# ---------------------------------------------------------------------------
WORKDIR /app

# ---------------------------------------------------------------------------
# Copy application source code
# ---------------------------------------------------------------------------
# The api_gateway package contains all routes, middleware, schemas, services,
# models, and the Flask Application Factory (app.py) plus WSGI entry (wsgi.py).
COPY api_gateway/ /app/api_gateway/

# The shared package provides cross-cutting concerns used by all backend
# services: MongoDB/Redis clients, JWT handler, RBAC, structured logging,
# OpenTelemetry tracing, Prometheus metrics, circuit breaker, health checks.
COPY shared/ /app/shared/

# ---------------------------------------------------------------------------
# Set file ownership to the non-root user
# ---------------------------------------------------------------------------
RUN chown -R app:app /app

# ---------------------------------------------------------------------------
# Switch to the non-root user for all subsequent commands and at runtime
# ---------------------------------------------------------------------------
USER app

# ---------------------------------------------------------------------------
# Expose the API Gateway service port
# ---------------------------------------------------------------------------
# The API Gateway listens on port 5000 by default. This is configurable
# via the Gunicorn --bind flag or the GUNICORN_BIND environment variable.
EXPOSE 5000

# ---------------------------------------------------------------------------
# Container health check
# ---------------------------------------------------------------------------
# Kubernetes liveness/readiness probes may override this, but the built-in
# HEALTHCHECK provides a safety net for Docker Compose and standalone Docker.
#   --interval=30s      — Check every 30 seconds
#   --timeout=5s        — Fail if the check takes longer than 5 seconds
#   --start-period=10s  — Grace period for application startup before checks begin
#   --retries=3         — Mark unhealthy after 3 consecutive failures
HEALTHCHECK --interval=30s --timeout=5s --start-period=10s --retries=3 \
    CMD curl -f http://localhost:5000/health || exit 1

# ---------------------------------------------------------------------------
# Default command — launch via Gunicorn WSGI server
# ---------------------------------------------------------------------------
# Gunicorn configuration:
#   --bind 0.0.0.0:5000  — Listen on all interfaces, port 5000
#   --workers 4           — 4 worker processes (tune via GUNICORN_WORKERS env var
#                           or use the formula: 2 * CPU_CORES + 1)
#   --threads 2           — 2 threads per worker for concurrent I/O (8 total capacity)
#   --timeout 120         — Worker timeout for long-running requests (seconds)
#   --access-logfile -    — Write access logs to stdout for container log collection
#   --error-logfile -     — Write error logs to stderr for container log collection
#   --preload             — Pre-load the Flask app in the master process for faster
#                           worker fork and shared memory (reduces per-worker RAM)
#   api_gateway.wsgi:app  — Import the WSGI application object from wsgi.py
#
# For custom Gunicorn configuration, mount a gunicorn.conf.py file or override
# this CMD with environment-specific settings:
#   docker run -e GUNICORN_WORKERS=8 ... synthetic-erp-api-gateway \
#     gunicorn --bind 0.0.0.0:5000 --workers 8 api_gateway.wsgi:app
CMD ["gunicorn", \
     "--bind", "0.0.0.0:5000", \
     "--workers", "4", \
     "--threads", "2", \
     "--timeout", "120", \
     "--access-logfile", "-", \
     "--error-logfile", "-", \
     "--preload", \
     "api_gateway.wsgi:app"]
