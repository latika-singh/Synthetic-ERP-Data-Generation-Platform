# ==============================================================================
# Dockerfile — Profiling Service
# Synthetic ERP Data Generation Platform
#
# Multi-stage Docker build for the Profiling Service that provides ERP schema
# discovery and statistical profiling. Includes JDBC driver support for
# connectivity to SAP, Oracle E-Business Suite, Microsoft Dynamics 365, and
# legacy ERP systems via JayDeBeAPI.
#
# Build context: Repository root (.)
# Usage:
#   docker build -f src/backend/profiling_service/Dockerfile -t profiling-service .
#
# Air-gapped builds:
#   All base images and packages can be sourced from private registries.
#   Override base image with --build-arg BASE_IMAGE=my-registry/python:3.12-slim
#   Place JDBC driver JARs in ./jdbc-drivers/ and uncomment the COPY line.
#
# .dockerignore recommendations (apply at build context root):
#   __pycache__/, *.pyc, *.pyo, .git/, .venv/, node_modules/,
#   .env, *.egg-info/, dist/, build/, tests/, docs/, infrastructure/,
#   .github/, .mypy_cache/, .pytest_cache/, .ruff_cache/
# ==============================================================================

# Build arguments for air-gapped and customizable builds
ARG BASE_IMAGE=python:3.12-slim
ARG JDBC_DRIVER_DIR=/opt/jdbc-drivers

# ==============================================================================
# Stage 1: Builder — Install system dependencies, Python packages, JDBC drivers
# ==============================================================================
FROM ${BASE_IMAGE} AS builder

# Metadata labels for the builder stage
LABEL stage="builder"
LABEL maintainer="Synthetic ERP Platform Team"

# Set working directory for the build stage
WORKDIR /build

# Install system-level build dependencies required for compiling native
# Python extensions (scipy, numpy, pandas C extensions) and JDBC support.
# - gcc, g++, python3-dev: C/C++ compiler toolchain for native extension builds
# - libffi-dev: Foreign function interface library for cffi/cryptography
# - default-jdk-headless: Java Development Kit for JayDeBeAPI/JPype1 compilation
# - curl, unzip: For downloading JDBC driver JAR files
# - libgfortran5: Fortran runtime for SciPy LAPACK/BLAS routines
RUN apt-get update && \
    apt-get install -y --no-install-recommends \
        gcc \
        g++ \
        python3-dev \
        libffi-dev \
        default-jdk-headless \
        curl \
        unzip \
        libgfortran5 && \
    apt-get clean && \
    rm -rf /var/lib/apt/lists/*

# Set JAVA_HOME for JayDeBeAPI/JPype1 compilation against JDK headers
ENV JAVA_HOME=/usr/lib/jvm/java-17-openjdk-amd64
ENV PATH="${JAVA_HOME}/bin:${PATH}"

# Create and activate a Python virtual environment for clean dependency isolation.
# This allows us to copy only the venv to the runtime stage without system packages.
RUN python -m venv /opt/venv
ENV PATH="/opt/venv/bin:${PATH}"

# Upgrade pip and install build tools to ensure latest wheel/setuptools support
RUN pip install --no-cache-dir --upgrade pip setuptools wheel

# Copy requirements.txt first for Docker layer caching — dependencies change
# less frequently than source code, so this layer is cached across builds.
COPY src/backend/profiling_service/requirements.txt /build/requirements.txt

# Install all Python dependencies into the virtual environment.
# --no-cache-dir minimizes image size by not storing pip download cache.
RUN pip install --no-cache-dir -r /build/requirements.txt

# Verify dependency consistency — catches broken dependency chains early
RUN pip check || echo "Warning: pip check reported issues (non-fatal for build)"

# ==============================================================================
# JDBC Driver Installation
#
# Download JDBC driver JARs for ERP system connectivity. These drivers enable
# JayDeBeAPI to establish connections to target ERP databases for schema
# discovery without accessing raw production data (Constraint C-001).
#
# For air-gapped deployments (C-003): Pre-download JARs and place them in a
# local directory, then use COPY instead of curl. Uncomment the COPY line below
# and comment out the curl download commands.
# ==============================================================================
ARG JDBC_DRIVER_DIR
RUN mkdir -p ${JDBC_DRIVER_DIR}

# PostgreSQL JDBC Driver (42.7.3) — for PostgreSQL-backed ERP systems
RUN curl -fsSL -o ${JDBC_DRIVER_DIR}/postgresql-42.7.3.jar \
    "https://jdbc.postgresql.org/download/postgresql-42.7.3.jar" || \
    echo "Warning: PostgreSQL JDBC driver download failed — provide manually for air-gapped builds"

# Microsoft SQL Server JDBC Driver (12.4.2) — for SQL Server-based ERP systems
RUN curl -fsSL -o /tmp/mssql-jdbc.tar.gz \
    "https://github.com/microsoft/mssql-jdbc/releases/download/v12.4.2/mssql-jdbc-12.4.2.jre11.jar" && \
    mv /tmp/mssql-jdbc.tar.gz ${JDBC_DRIVER_DIR}/mssql-jdbc-12.4.2.jre11.jar || \
    echo "Warning: SQL Server JDBC driver download failed — provide manually for air-gapped builds"

# Oracle JDBC Driver — License-restricted; must be provided by the operator.
# For Oracle E-Business Suite connectivity, place ojdbc11.jar in the
# jdbc-drivers/ directory and uncomment the COPY instruction below.
# COPY jdbc-drivers/ojdbc11.jar ${JDBC_DRIVER_DIR}/ojdbc11.jar
RUN echo "Oracle JDBC driver (ojdbc11.jar) must be provided separately due to Oracle licensing" \
    > ${JDBC_DRIVER_DIR}/ORACLE_DRIVER_README.txt

# SAP HANA JDBC Driver — License-restricted; must be provided by the operator.
# For SAP ERP connectivity, place ngdbc.jar in the jdbc-drivers/ directory
# and uncomment the COPY instruction below.
# COPY jdbc-drivers/ngdbc.jar ${JDBC_DRIVER_DIR}/ngdbc.jar
RUN echo "SAP HANA JDBC driver (ngdbc.jar) must be provided separately due to SAP licensing" \
    > ${JDBC_DRIVER_DIR}/SAP_DRIVER_README.txt

# For air-gapped deployments, uncomment the following line to copy pre-staged
# JDBC drivers from the build context instead of downloading them:
# COPY jdbc-drivers/*.jar ${JDBC_DRIVER_DIR}/

# Verify JDBC driver directory contents
RUN ls -la ${JDBC_DRIVER_DIR}/ && \
    echo "JDBC driver directory prepared at ${JDBC_DRIVER_DIR}"


# ==============================================================================
# Stage 2: Runtime — Minimal production image with only necessary components
# ==============================================================================
FROM ${BASE_IMAGE} AS runtime

# Container metadata labels following OCI image specification
LABEL org.opencontainers.image.title="synthetic-erp-profiling-service"
LABEL org.opencontainers.image.description="ERP schema discovery and statistical profiling service with JDBC connectivity"
LABEL org.opencontainers.image.source="src/backend/profiling_service/Dockerfile"
LABEL org.opencontainers.image.vendor="Synthetic ERP Platform Team"
LABEL org.opencontainers.image.version="1.0.0"

# Install minimal runtime dependencies only — no compilers or dev headers.
# - default-jre-headless: Java Runtime Environment for JayDeBeAPI JDBC execution
# - ca-certificates: Root certificates for TLS connections to ERP systems
# - curl: Required for Docker HEALTHCHECK probe
# - libgfortran5: Fortran runtime for SciPy LAPACK/BLAS at runtime
RUN apt-get update && \
    apt-get install -y --no-install-recommends \
        default-jre-headless \
        ca-certificates \
        curl \
        libgfortran5 && \
    apt-get clean && \
    rm -rf /var/lib/apt/lists/*

# Set JAVA_HOME for JayDeBeAPI runtime — must match the JRE installation path
ENV JAVA_HOME=/usr/lib/jvm/java-17-openjdk-amd64
ENV PATH="${JAVA_HOME}/bin:${PATH}"

# Create a dedicated non-root user for running the application.
# Running as non-root follows Docker security best practices and satisfies
# SOC 2 Type II compliance requirements (C-004).
RUN addgroup --system appuser && \
    adduser --system --ingroup appuser --no-create-home --disabled-password appuser

# Copy the Python virtual environment from the builder stage.
# This includes all installed packages (Flask, SciPy, NumPy, Pandas,
# JayDeBeAPI, PyMongo, structlog, etc.) without build-time compiler deps.
COPY --from=builder /opt/venv /opt/venv
ENV PATH="/opt/venv/bin:${PATH}"

# Copy JDBC driver JARs from the builder stage
ARG JDBC_DRIVER_DIR=/opt/jdbc-drivers
COPY --from=builder ${JDBC_DRIVER_DIR} ${JDBC_DRIVER_DIR}

# Set CLASSPATH for JayDeBeAPI to locate JDBC driver JARs at runtime
ENV CLASSPATH="${JDBC_DRIVER_DIR}/*"
ENV JDBC_DRIVER_PATH="${JDBC_DRIVER_DIR}"

# Set application working directory
WORKDIR /app

# Copy the Profiling Service source code into the container
COPY src/backend/profiling_service/ /app/profiling_service/

# Copy shared utilities library required by all backend services for
# MongoDB connectivity, Redis caching, JWT auth, structured logging,
# OpenTelemetry tracing, Prometheus metrics, and circuit breaker patterns
COPY src/backend/shared/ /app/shared/

# Set PYTHONPATH so Python can resolve both 'profiling_service' and 'shared'
# package imports from the /app directory
ENV PYTHONPATH=/app

# Configure Python and Flask environment variables
# - PYTHONDONTWRITEBYTECODE: Prevent .pyc file generation (reduces image noise)
# - PYTHONUNBUFFERED: Ensure real-time stdout/stderr for container log drivers
# - FLASK_APP: Point Gunicorn/Flask to the Application Factory
# - FLASK_ENV: Default to production (override via docker-compose or K8s env)
# - SERVICE_NAME: Identifies this service in logs and traces
ENV PYTHONDONTWRITEBYTECODE=1
ENV PYTHONUNBUFFERED=1
ENV FLASK_APP=profiling_service.app:create_app()
ENV FLASK_ENV=production
ENV SERVICE_NAME=profiling-service

# OpenTelemetry configuration defaults — overrideable at runtime via K8s env
ENV OTEL_SERVICE_NAME=profiling-service
ENV OTEL_EXPORTER_OTLP_ENDPOINT=http://otel-collector:4317

# Set file ownership to non-root user before switching
RUN chown -R appuser:appuser /app

# Switch to non-root user for all subsequent operations
USER appuser

# Expose the Profiling Service port for inter-service communication
# This port is configurable via PROFILING_SERVICE_PORT env var at runtime
EXPOSE 8002

# Docker health check to verify the service is responsive.
# Kubernetes liveness/readiness probes will also use /health endpoint.
# - interval: Check every 30 seconds
# - timeout: Fail if no response within 5 seconds
# - start-period: Allow 15 seconds for application startup (JDBC init)
# - retries: Mark unhealthy after 3 consecutive failures
HEALTHCHECK --interval=30s --timeout=5s --start-period=15s --retries=3 \
    CMD curl -f http://localhost:8002/health || exit 1

# Start Gunicorn WSGI server with the Flask Application Factory.
# - --bind 0.0.0.0:8002: Listen on all interfaces at service port
# - --workers 4: 4 worker processes for concurrent request handling
# - --timeout 120: Extended timeout for long-running schema discovery operations
# - --access-logfile -: Stream access logs to stdout for container log collection
# - --error-logfile -: Stream error logs to stderr for container log collection
# - --graceful-timeout 30: Allow 30s for workers to finish during shutdown
# - --max-requests 1000: Recycle workers after 1000 requests to prevent memory leaks
# - --max-requests-jitter 50: Add jitter to prevent all workers recycling simultaneously
CMD ["gunicorn", \
     "--bind", "0.0.0.0:8002", \
     "--workers", "4", \
     "--timeout", "120", \
     "--access-logfile", "-", \
     "--error-logfile", "-", \
     "--graceful-timeout", "30", \
     "--max-requests", "1000", \
     "--max-requests-jitter", "50", \
     "profiling_service.app:create_app()"]
