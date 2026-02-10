# =============================================================================
# Quality Service — Production-Optimized Multi-Stage Dockerfile
# =============================================================================
# Synthetic ERP Data Generation Platform
#
# This Dockerfile builds the Quality Service container responsible for data
# quality validation using Great Expectations with a weighted scoring model:
#   - 40% Statistical Fidelity
#   - 30% Business Rules Compliance
#   - 30% Referential Integrity
# Target quality threshold: ≥95%
#
# Multi-stage build:
#   Stage 1 (builder): Compiles native extensions (SciPy, NumPy, cryptography)
#   Stage 2 (runtime): Minimal production image with Gunicorn WSGI server
#
# Build:
#   docker build -f infrastructure/docker/quality-service.Dockerfile -t synthetic-erp-quality-service .
#
# Run:
#   docker run -p 5003:5003 --env-file .env synthetic-erp-quality-service
#
# Build context: Project root (.)
# =============================================================================

# ---------------------------------------------------------------------------
# Stage 1: Builder — Install dependencies and compile native extensions
# ---------------------------------------------------------------------------
# python:3.12-slim provides a minimal Debian-based Python 3.12 image
# reducing attack surface while including the full standard library
FROM python:3.12-slim AS builder

# Container metadata labels (OCI standard)
LABEL org.opencontainers.image.title="synthetic-erp-quality-service" \
      org.opencontainers.image.description="Quality validation service with Great Expectations weighted scoring" \
      org.opencontainers.image.source="infrastructure/docker/quality-service.Dockerfile"

# Set working directory for the build stage
WORKDIR /build

# Install system-level build dependencies required for compiling native
# Python extensions. These are ONLY needed during the build stage:
#   - gcc, g++: C/C++ compilers for native extension compilation
#   - python3-dev: Python development headers for C API extensions
#   - libffi-dev: Foreign Function Interface library for cffi/cryptography
# These packages enable compilation of:
#   - scipy (Fortran/C numerical routines)
#   - numpy (C-based array operations)
#   - great-expectations (transitive native dependencies)
#   - cryptography (OpenSSL bindings for AES-256 encryption)
#   - pydantic-core (Rust-based validation compiled via maturin)
RUN apt-get update && \
    apt-get install -y --no-install-recommends \
        gcc \
        g++ \
        python3-dev \
        libffi-dev \
    && rm -rf /var/lib/apt/lists/*

# Create an isolated Python virtual environment for clean dependency
# management. Using a venv allows us to copy ONLY the installed packages
# to the runtime stage without any build-time artifacts.
RUN python -m venv /opt/venv

# Activate the virtual environment by prepending its bin to PATH.
# All subsequent pip installs and Python executions use this venv.
ENV PATH="/opt/venv/bin:$PATH"

# Upgrade pip, setuptools, and wheel to latest versions for reliable
# package installation and wheel-based builds
RUN pip install --no-cache-dir --upgrade pip setuptools wheel

# Copy only the requirements file first to leverage Docker layer caching.
# Dependencies are only re-installed when requirements.txt changes,
# not on every application code change.
COPY src/backend/quality_service/requirements.txt /build/requirements.txt

# Install all Python dependencies from the requirements manifest.
# --no-cache-dir: Prevents pip from caching downloaded packages, reducing
# image size since we discard the builder stage entirely.
# Key packages installed:
#   - flask (3.1.x): REST API framework with Application Factory pattern
#   - great-expectations (0.18.x): Data quality validation framework
#   - pydantic (2.x): Request/response schema validation
#   - scipy (1.12+): Statistical distribution fitting and hypothesis testing
#   - numpy (1.26+): Numerical computations for statistical validation
#   - pandas (2.x): DataFrame operations for batch quality analysis
#   - pymongo (4.x): MongoDB driver for quality reports persistence
#   - redis (5.x): Redis client for caching quality scores
#   - gunicorn (21.x): Production WSGI HTTP server
#   - structlog (24.x): Structured JSON logging
#   - opentelemetry-*: Distributed tracing instrumentation
#   - prometheus-client (0.20.x): Metrics exporter
#   - cryptography (42.x): AES-256 encryption support
RUN pip install --no-cache-dir -r requirements.txt

# Verify dependency consistency — ensures all installed packages have
# compatible versions and no broken transitive dependencies exist.
# This catches version conflicts early during the build process.
RUN pip check

# ---------------------------------------------------------------------------
# Stage 2: Runtime — Minimal production image
# ---------------------------------------------------------------------------
# Start from a fresh python:3.12-slim to ensure the runtime image
# contains NONE of the build toolchain (gcc, g++, dev headers).
# This dramatically reduces the final image size and attack surface.
FROM python:3.12-slim AS runtime

# ---- Environment Variables ------------------------------------------------
# PYTHONDONTWRITEBYTECODE=1: Prevents Python from writing .pyc bytecode
#   files to disk, reducing filesystem writes and image size in containers
# PYTHONUNBUFFERED=1: Forces stdout/stderr to be unbuffered so all log
#   output is immediately visible in Docker logs and Kubernetes pod logs
# FLASK_APP: Points Gunicorn to the Flask Application Factory entry point
# FLASK_ENV: Production mode — disables debug mode, reloader, and debugger
# PYTHONPATH: Ensures Python can resolve imports from the /app root,
#   enabling 'from quality_service.xxx' and 'from shared.xxx' imports
# SERVICE_NAME: Identifies this service in structured logs, distributed
#   traces, and Prometheus metrics for observability correlation
ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    FLASK_APP=quality_service.app:create_app() \
    FLASK_ENV=production \
    PYTHONPATH=/app \
    SERVICE_NAME=quality-service

# Install minimal runtime dependencies. Only curl is needed for
# health check probes (Kubernetes liveness/readiness and Docker HEALTHCHECK).
# No build tools, compilers, or dev headers are installed in the runtime.
RUN apt-get update && \
    apt-get install -y --no-install-recommends \
        curl \
        ca-certificates \
    && rm -rf /var/lib/apt/lists/*

# ---- Security: Non-Root User ---------------------------------------------
# Create a dedicated system user and group for running the application.
# Running as non-root is a container security best practice that:
#   - Limits the blast radius of potential container escapes
#   - Prevents write access to system directories
#   - Satisfies Kubernetes PodSecurityPolicies and OPA constraints
#   - Meets SOC 2 Type II security requirements
RUN addgroup --system appuser && \
    adduser --system --ingroup appuser --no-create-home appuser

# ---- Copy Virtual Environment from Builder --------------------------------
# Transfer only the compiled virtual environment from the builder stage.
# This contains all installed Python packages with their native extensions
# pre-compiled, without any of the build toolchain.
COPY --from=builder /opt/venv /opt/venv

# Activate the virtual environment in the runtime stage
ENV PATH="/opt/venv/bin:$PATH"

# Set the application working directory
WORKDIR /app

# ---- Copy Application Code -----------------------------------------------
# Copy the Quality Service application code
COPY src/backend/quality_service/ /app/quality_service/

# Copy the shared utilities library used by all backend services.
# Provides: MongoDB client, Redis client, JWT handler, RBAC, structured
# logging, OpenTelemetry tracing, Prometheus metrics, circuit breaker,
# health check endpoints, and base configuration.
COPY src/backend/shared/ /app/shared/

# ---- File Ownership -------------------------------------------------------
# Transfer ownership of all application files to the non-root user.
# This ensures the application process cannot modify system files
# and can only write to its own application directory.
RUN chown -R appuser:appuser /app

# Switch to non-root user for all subsequent commands and the
# container entrypoint. The application runs with minimal privileges.
USER appuser

# ---- Network Configuration -----------------------------------------------
# Expose the Quality Service port. This is the standard port for this
# service across Docker Compose, Kubernetes Services, and Ingress routing.
# Port mapping:
#   API Gateway:       5000
#   Generation Engine: 5001
#   Profiling Service: 5002
#   Quality Service:   5003  <-- this service
#   Compliance Service:5004
#   Provisioning Svc:  5005
EXPOSE 5003

# ---- Health Check ---------------------------------------------------------
# Docker-native health check that verifies the Quality Service is
# responding to HTTP requests. Also used by Docker Compose depends_on
# with condition: service_healthy.
#   --interval=30s:       Check every 30 seconds
#   --timeout=10s:        Allow 10 seconds for the health endpoint to respond
#   --start-period=15s:   Grace period during container startup for Flask
#                         initialization, MongoDB/Redis connection pooling
#   --retries=3:          Mark unhealthy after 3 consecutive failures
# The /health endpoint returns service status, database connectivity,
# and dependency health for comprehensive monitoring.
HEALTHCHECK --interval=30s --timeout=10s --start-period=15s --retries=3 \
    CMD curl -f http://localhost:5003/health || exit 1

# ---- Container Entrypoint ------------------------------------------------
# Run the Quality Service via Gunicorn WSGI server in production mode.
#   --bind 0.0.0.0:5003:     Listen on all interfaces at port 5003
#   --workers 4:             4 worker processes for concurrent request handling
#                            (recommended: 2 * CPU cores + 1 for I/O-bound services)
#   --timeout 120:           120-second worker timeout accommodating quality
#                            validation computations on large datasets
#   --access-logfile -:      Stream access logs to stdout for Docker log drivers
#   --error-logfile -:       Stream error logs to stderr for Docker log drivers
#   quality_service.app:create_app():
#                            Flask Application Factory entry point — Gunicorn
#                            calls create_app() to obtain the configured Flask
#                            application instance with all Blueprints registered
CMD ["gunicorn", \
     "--bind", "0.0.0.0:5003", \
     "--workers", "4", \
     "--timeout", "120", \
     "--access-logfile", "-", \
     "--error-logfile", "-", \
     "quality_service.app:create_app()"]
