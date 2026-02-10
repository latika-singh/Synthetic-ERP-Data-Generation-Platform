# ============================================================================
# Synthetic ERP Data Generation Platform — Provisioning Service Dockerfile
# ============================================================================
# Production-optimized multi-stage build for the Provisioning Service.
# This service handles database provisioning (JDBC inserts to PostgreSQL,
# Oracle, SQL Server, SAP HANA) and cloud storage exports (AWS S3, Azure
# Blob Storage, GCP Cloud Storage) in multiple output formats (SQL, CSV,
# JSON, Parquet).
#
# The build bundles JDBC driver JARs for all four supported target
# databases, Cloud SDK Python packages (boto3, azure-storage-blob,
# google-cloud-storage), and the pyarrow library for Parquet output.
#
# Build context: Project root (.)
# Build command:
#   docker build -f infrastructure/docker/provisioning-service.Dockerfile \
#     --build-arg BUILD_DATE=$(date -u +"%Y-%m-%dT%H:%M:%SZ") \
#     --build-arg VCS_REF=$(git rev-parse --short HEAD) \
#     -t synthetic-erp-provisioning-service:latest .
#
# For air-gapped environments (C-003):
#   docker build -f infrastructure/docker/provisioning-service.Dockerfile \
#     --build-arg PIP_INDEX_URL=https://private-pypi.internal/simple \
#     --build-arg PIP_TRUSTED_HOST=private-pypi.internal \
#     --build-arg JDBC_MIRROR_URL=https://internal-artifacts.corp/jdbc \
#     -t synthetic-erp-provisioning-service:latest .
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
# Air-gapped support: override JDBC_MIRROR_URL to point to an internal artifact
# repository when building in environments without public internet access
ARG JDBC_MIRROR_URL=https://repo1.maven.org/maven2

# ============================================================================
# Stage 1: Builder
# Purpose: Install system-level build dependencies, Python packages, and
#          download JDBC driver JARs into isolated locations. This stage is
#          discarded in the final image to minimize attack surface and image
#          size. The builder produces two artifacts:
#            1. /opt/venv — Python virtual environment with all Cloud SDKs,
#               JDBC libraries (jaydebeapi, JPype1), and data format libs
#            2. /opt/jdbc-drivers — JDBC driver JARs for all target databases
# ============================================================================
FROM python:${PYTHON_VERSION}-slim AS builder

# Metadata for the builder stage (informational only)
LABEL stage=builder

# Set working directory for the build context
WORKDIR /build

# Install system-level build dependencies required for compiling native
# Python extensions and JDBC driver tooling:
#   - gcc, g++: C/C++ compilers for native extensions (cryptography,
#     pydantic-core, pyarrow, JPype1, grpcio for cloud SDKs)
#   - python3-dev: Python development headers for C extension compilation
#   - libffi-dev: Foreign function interface library for cffi/cryptography
#   - libssl-dev: OpenSSL development headers for the cryptography package
#     which provides AES-256 encryption primitives for data-at-rest security
#   - default-jdk-headless: Full JDK required at build time for JPype1
#     (jaydebeapi dependency) to compile its JNI bindings against the JVM
#   - curl: Used to download JDBC driver JARs from Maven Central or mirrors
#   - unzip: Used to extract JDBC driver archives if packaged as ZIP
# These are only needed at build time and are NOT included in runtime.
RUN apt-get update && \
    apt-get install -y --no-install-recommends \
        gcc \
        g++ \
        python3-dev \
        libffi-dev \
        libssl-dev \
        default-jdk-headless \
        curl \
        unzip \
    && rm -rf /var/lib/apt/lists/*

# Set JAVA_HOME for the JDK so that JPype1 can locate the JVM headers and
# shared libraries during its native extension compilation step.
ENV JAVA_HOME=/usr/lib/jvm/java-17-openjdk-amd64

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
COPY src/backend/provisioning_service/requirements.txt /build/requirements.txt

# Install all Python dependencies into the virtual environment.
# --no-cache-dir: Prevents pip from caching downloaded packages, reducing
#   the builder layer size.
# Key packages installed:
#   - flask 3.1.x: Lightweight REST API framework
#   - gunicorn 21.x: Production WSGI HTTP server
#   - pymongo 4.x: MongoDB 7.0 driver for job metadata persistence
#   - redis 5.x: Redis 7.x client for caching and progress tracking
#   - pydantic 2.x: Data validation and schema models
#   - boto3 1.34.x: AWS S3 SDK for multi-part upload with AES-256 encryption
#   - azure-storage-blob 12.x: Azure Blob Storage SDK with managed identity
#   - google-cloud-storage 2.x: GCP Cloud Storage SDK with service account auth
#   - jaydebeapi 1.2.x: JDBC database connectivity via JPype1 JNI bridge
#   - pyarrow 15.x: Apache Parquet columnar format read/write support
#   - structlog 24.x: Structured JSON logging
#   - opentelemetry-api, opentelemetry-sdk: Distributed tracing
#   - prometheus-client: Prometheus metrics exporter
#   - cryptography 42.x: AES-256 encryption primitives for data at rest
#   - circuitbreaker 2.0.x: Circuit breaker pattern for external service calls
ARG PIP_INDEX_URL
ARG PIP_TRUSTED_HOST
RUN pip install --no-cache-dir --upgrade pip setuptools wheel && \
    pip install --no-cache-dir \
        --index-url "${PIP_INDEX_URL}" \
        --trusted-host "${PIP_TRUSTED_HOST}" \
        -r /build/requirements.txt

# ---------------------------------------------------------------------------
# JDBC Driver Downloads
# ---------------------------------------------------------------------------
# Download JDBC driver JARs for all four supported target databases.
# These are placed into /opt/jdbc-drivers/ and copied to the runtime stage.
#
# For air-gapped deployments (C-003): override JDBC_MIRROR_URL build arg
# to point to an internal artifact repository (e.g., Nexus, Artifactory)
# that mirrors Maven Central. Example:
#   --build-arg JDBC_MIRROR_URL=https://nexus.corp.internal/repository/maven-central
# ---------------------------------------------------------------------------
ARG JDBC_MIRROR_URL
RUN mkdir -p /opt/jdbc-drivers

# PostgreSQL JDBC Driver 42.7.3
# Official open-source driver (BSD-2 license), freely redistributable.
# Supports PostgreSQL 12.x through 16.x as required by compatibility matrix.
RUN curl -fSL -o /opt/jdbc-drivers/postgresql-42.7.3.jar \
    "${JDBC_MIRROR_URL}/org/postgresql/postgresql/42.7.3/postgresql-42.7.3.jar"

# Microsoft SQL Server JDBC Driver 12.4.2 (JRE 11 compatible)
# Open-source driver (MIT license), freely redistributable.
# Supports SQL Server 2019 through 2022 as required by compatibility matrix.
RUN curl -fSL -o /opt/jdbc-drivers/mssql-jdbc-12.4.2.jre11.jar \
    "${JDBC_MIRROR_URL}/com/microsoft/sqlserver/mssql-jdbc/12.4.2.jre11/mssql-jdbc-12.4.2.jre11.jar"

# Oracle JDBC Thin Driver (ojdbc11.jar)
# The Oracle JDBC driver is available on Maven Central under the Oracle
# Free Use Terms and Conditions license. The ojdbc11 variant supports
# JDK 11+ and Oracle Database 19c through 23ai.
# NOTE: For enterprise deployments requiring Oracle JDBC Thin driver,
# ensure compliance with Oracle's license terms. In air-gapped environments,
# place the driver in your internal artifact mirror.
RUN curl -fSL -o /opt/jdbc-drivers/ojdbc11.jar \
    "${JDBC_MIRROR_URL}/com/oracle/database/jdbc/ojdbc11/23.4.0.24.05/ojdbc11-23.4.0.24.05.jar" \
    || echo "NOTICE: Oracle JDBC driver download skipped. Place ojdbc11.jar in /opt/jdbc-drivers/ manually if Oracle provisioning is required."

# SAP HANA JDBC Driver (ngdbc.jar)
# The SAP HANA JDBC driver requires acceptance of the SAP Developer License
# Agreement and is NOT available on public Maven Central. For production
# deployments:
#   1. Download ngdbc.jar from SAP Development Tools or SAP Support Portal
#   2. Place it in your internal artifact repository
#   3. Override JDBC_MIRROR_URL to point to your internal mirror
#   4. Or mount the driver as a volume at /opt/jdbc-drivers/ngdbc.jar
#
# The following attempts download from the configured mirror; if unavailable,
# it creates a placeholder README explaining how to obtain the driver.
RUN curl -fSL -o /opt/jdbc-drivers/ngdbc.jar \
    "${JDBC_MIRROR_URL}/com/sap/cloud/db/jdbc/ngdbc/2.19.16/ngdbc-2.19.16.jar" \
    || echo "SAP HANA JDBC driver (ngdbc.jar) not found in mirror. See /opt/jdbc-drivers/HANA_DRIVER_README.txt" \
    && echo "SAP HANA JDBC Driver Setup Instructions\n\
========================================\n\
The SAP HANA JDBC driver (ngdbc.jar) requires a SAP Developer License.\n\
\n\
To enable SAP HANA provisioning:\n\
  1. Download ngdbc.jar from https://tools.hana.ondemand.com/#hanatools\n\
  2. Place it in /opt/jdbc-drivers/ via volume mount or custom image layer:\n\
     docker run -v /path/to/ngdbc.jar:/opt/jdbc-drivers/ngdbc.jar ...\n\
  3. Or add to your internal artifact mirror and rebuild with:\n\
     --build-arg JDBC_MIRROR_URL=https://your-mirror.corp/maven\n\
\n\
SAP HANA 2.0 SPS 07+ is supported.\n" > /opt/jdbc-drivers/HANA_DRIVER_README.txt

# List all downloaded JDBC drivers for build verification and audit trail
RUN echo "=== JDBC Drivers in /opt/jdbc-drivers/ ===" && \
    ls -la /opt/jdbc-drivers/ && \
    echo "=== End JDBC Driver Listing ==="

# Verify that all installed Python packages have consistent dependency
# versions. This catches silent version conflicts that could cause runtime
# failures in cloud SDK or JDBC interactions.
RUN pip check

# ============================================================================
# Stage 2: Runtime
# Purpose: Minimal production image containing only the Python virtual
#          environment (with all Cloud SDKs and JDBC libraries), JDBC
#          driver JARs, JRE runtime, and the application source code.
#          No build tools, compilers, JDK, or development headers are
#          included, minimizing the attack surface per security requirements.
# ============================================================================
FROM python:${PYTHON_VERSION}-slim AS runtime

# ---------------------------------------------------------------------------
# OCI Image Specification Labels
# Provides standardized container metadata for registry discovery,
# vulnerability scanning tools, and deployment automation.
# ---------------------------------------------------------------------------
LABEL org.opencontainers.image.title="synthetic-erp-provisioning-service" \
      org.opencontainers.image.description="Database provisioning and cloud storage export service with JDBC and Cloud SDK support" \
      org.opencontainers.image.source="infrastructure/docker/provisioning-service.Dockerfile" \
      org.opencontainers.image.vendor="Synthetic ERP Platform" \
      org.opencontainers.image.created="${BUILD_DATE}" \
      org.opencontainers.image.revision="${VCS_REF}" \
      org.opencontainers.image.licenses="Proprietary"

# Install minimal runtime dependencies only — no compilers, JDK, or dev headers.
#   - default-jre-headless: Java Runtime Environment required by jaydebeapi/JPype1
#     at runtime to establish JDBC connections to target databases (PostgreSQL,
#     Oracle, SQL Server, SAP HANA). Only the JRE is needed — the full JDK is
#     not required at runtime.
#   - ca-certificates: Root CA bundle for TLS/HTTPS connections to:
#     * AWS S3 API endpoints (boto3)
#     * Azure Blob Storage API endpoints (azure-storage-blob)
#     * GCP Cloud Storage API endpoints (google-cloud-storage)
#     * Target databases with TLS-encrypted connections
#   - curl: Required for Docker/Kubernetes health check probes (HEALTHCHECK CMD)
# The apt cache is cleaned immediately to minimize the image layer size.
RUN apt-get update && \
    apt-get install -y --no-install-recommends \
        default-jre-headless \
        ca-certificates \
        curl \
    && rm -rf /var/lib/apt/lists/*

# ---------------------------------------------------------------------------
# Environment Variables — Python Runtime Behavior
# ---------------------------------------------------------------------------
#   PYTHONDONTWRITEBYTECODE=1: Prevent .pyc file generation (cleaner container)
#   PYTHONUNBUFFERED=1: Force stdout/stderr streams to be unbuffered for
#     real-time log output in Docker/Kubernetes log collectors
ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1

# ---------------------------------------------------------------------------
# Environment Variables — Flask Application Configuration
# ---------------------------------------------------------------------------
#   FLASK_APP: Application factory entry point for Flask CLI and Gunicorn
#   FLASK_ENV: Production mode (disables debug, reloader, and debugger)
#   PYTHONPATH: Ensures Python can locate provisioning_service and shared packages
ENV FLASK_APP="provisioning_service.app:create_app()" \
    FLASK_ENV=production \
    PYTHONPATH=/app

# ---------------------------------------------------------------------------
# Environment Variables — Service Identity and Observability
# ---------------------------------------------------------------------------
#   SERVICE_NAME: Used by structured logger for service identification in logs
#     and by OpenTelemetry for distributed trace span naming
ENV SERVICE_NAME="provisioning-service"

# ---------------------------------------------------------------------------
# Environment Variables — Java/JDBC Configuration
# ---------------------------------------------------------------------------
#   JAVA_HOME: Points to the JRE installation so JPype1 can locate the JVM
#     shared library (libjvm.so) at runtime for JDBC bridge operations
#   CLASSPATH: Includes all JDBC driver JARs so jaydebeapi can locate the
#     appropriate driver class when establishing database connections:
#     - org.postgresql.Driver (PostgreSQL)
#     - oracle.jdbc.OracleDriver (Oracle)
#     - com.microsoft.sqlserver.jdbc.SQLServerDriver (SQL Server)
#     - com.sap.db.jdbc.Driver (SAP HANA)
#   JDBC_DRIVER_PATH: Application-level configuration allowing the
#     provisioning service to programmatically locate JDBC driver JARs
#     when building jaydebeapi connection strings
ENV JAVA_HOME=/usr/lib/jvm/java-17-openjdk-amd64 \
    CLASSPATH="/opt/jdbc-drivers/*" \
    JDBC_DRIVER_PATH=/opt/jdbc-drivers

# ---------------------------------------------------------------------------
# Environment Variables — Cloud Provider Defaults
# ---------------------------------------------------------------------------
#   AWS_DEFAULT_REGION: Default AWS region for S3 operations; override at
#     runtime via environment variable or IAM instance metadata. This
#     provides a sensible default for development and testing.
#   Note: Azure and GCP credentials/configuration are injected entirely
#     via runtime environment variables or mounted service account keys.
ENV AWS_DEFAULT_REGION=us-east-1

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
# Temporary Export Directory
# ---------------------------------------------------------------------------
# Create a temporary directory for staging export files (CSV, JSON, Parquet,
# SQL) before they are uploaded to cloud storage or transferred via JDBC.
# This directory is owned by appuser and cleaned after each export operation.
# Using /tmp/exports ensures the directory is writable and on a writable
# filesystem even in read-only root filesystem Kubernetes configurations
# (via emptyDir volume mount).
RUN mkdir -p /tmp/exports

# ---------------------------------------------------------------------------
# Copy Virtual Environment from Builder Stage
# ---------------------------------------------------------------------------
# Copy the complete virtual environment containing all installed Python
# packages from the builder stage. This includes:
#   - Cloud SDKs: boto3 (AWS S3), azure-storage-blob, google-cloud-storage
#   - JDBC bridge: jaydebeapi, JPype1
#   - Data formats: pyarrow (Parquet), pandas
#   - Framework: Flask, Gunicorn, Pydantic
#   - Observability: structlog, opentelemetry, prometheus-client
#   - Security: cryptography (AES-256)
COPY --from=builder /opt/venv /opt/venv

# ---------------------------------------------------------------------------
# Copy JDBC Driver JARs from Builder Stage
# ---------------------------------------------------------------------------
# Copy all downloaded JDBC driver JARs:
#   - postgresql-42.7.3.jar: PostgreSQL 12.x-16.x
#   - mssql-jdbc-12.4.2.jre11.jar: SQL Server 2019-2022
#   - ojdbc11.jar: Oracle 19c-23ai (if download succeeded)
#   - ngdbc.jar: SAP HANA 2.0 SPS 07+ (if available in mirror)
COPY --from=builder /opt/jdbc-drivers /opt/jdbc-drivers

# Ensure the virtual environment's bin directory is first in PATH so
# python, gunicorn, and all entry-point scripts resolve to the venv.
ENV PATH="/opt/venv/bin:$PATH"

# ---------------------------------------------------------------------------
# Application Code
# ---------------------------------------------------------------------------
# Set the working directory where the application code will reside.
WORKDIR /app

# Copy the Provisioning Service code. The COPY context is the project root,
# so paths are relative to the repository root directory.
# The provisioning_service package contains: app.py (factory), config.py,
# connectors/ (PostgreSQL, Oracle, SQL Server, HANA), cloud/ (S3, Azure,
# GCS), and exporters/ (file_exporter, database_exporter).
COPY src/backend/provisioning_service/ /app/provisioning_service/

# Copy the shared utilities library used by all backend services.
# Contains: database/ (MongoDB, Redis clients), auth/ (JWT, RBAC),
# logging/ (structured logger), observability/ (metrics, tracing),
# config/ (base config), middleware/ (circuit breaker, health check).
COPY src/backend/shared/ /app/shared/

# Set recursive ownership of all application files and the export staging
# directory to the non-root user. This ensures appuser can:
#   - Read all source files
#   - Write temporary export files to /tmp/exports
#   - Create checkpoint files if needed
RUN chown -R appuser:appuser /app /tmp/exports

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
# Expose port 5005 for the Flask/Gunicorn HTTP server.
# This is the port the Provisioning Service listens on for incoming REST
# requests from the API Gateway and Generation Engine for:
#   - Database provisioning (JDBC batch inserts)
#   - Cloud storage uploads (S3, Azure Blob, GCS)
#   - File exports (CSV, JSON, Parquet, SQL)
# Kubernetes Service and Docker Compose port mappings reference this port.
EXPOSE 5005

# ---------------------------------------------------------------------------
# Health Check Configuration
# ---------------------------------------------------------------------------
# Docker HEALTHCHECK uses curl to probe the /health endpoint served by the
# Flask health check Blueprint. This endpoint returns HTTP 200 when the
# service is operational and critical dependencies (MongoDB, Redis) are
# reachable.
#
# Parameters:
#   --interval=30s: Check every 30 seconds
#   --timeout=10s: Fail if /health doesn't respond within 10 seconds
#     (slightly higher than API Gateway's 5s due to JDBC/cloud SDK
#     dependency checks in health endpoint)
#   --start-period=15s: Grace period after container start before first check
#     (allows JVM initialization, Gunicorn worker startup, and Flask app setup)
#   --retries=3: Mark unhealthy after 3 consecutive failures
#
# In Kubernetes, the liveness/readiness probes in the Deployment manifest
# typically override this HEALTHCHECK, but it serves as a fallback for
# Docker Compose and standalone Docker deployments.
HEALTHCHECK --interval=30s --timeout=10s --start-period=15s --retries=3 \
    CMD curl -f http://localhost:5005/health || exit 1

# ---------------------------------------------------------------------------
# Container Entrypoint — Gunicorn WSGI Server
# ---------------------------------------------------------------------------
# Run the Flask application via Gunicorn, the production WSGI server.
#
# Gunicorn configuration:
#   --bind 0.0.0.0:5005   : Listen on all interfaces at port 5005
#   --workers 4            : 4 worker processes for parallel request handling.
#                            Handles concurrent provisioning jobs and export
#                            requests from multiple tenants simultaneously.
#   --timeout 300          : Worker timeout set to 300 seconds (5 minutes).
#                            This is significantly higher than the API Gateway
#                            (120s) because provisioning operations involve:
#                            - Large dataset JDBC batch inserts (potentially
#                              millions of rows to PostgreSQL/Oracle/etc.)
#                            - Multi-part cloud storage uploads (large Parquet
#                              or CSV files to S3/Azure Blob/GCS)
#                            - Cross-region network transfers with variable
#                              latency
#                            Workers processing long-running provisioning
#                            requests must not be killed prematurely.
#   --access-logfile -     : Stream access logs to stdout for Docker log driver
#   --error-logfile -      : Stream error logs to stderr for Docker log driver
#   provisioning_service.app:create_app()
#                          : Application factory entry point. Gunicorn calls
#                            create_app() to obtain the Flask WSGI application
#                            instance with all routes, middleware, and
#                            extensions registered.
#
# Workers sizing rationale:
#   - 4 workers: Appropriate for 2-4 CPU core pods in Kubernetes
#   - No threads: Provisioning operations are primarily I/O-bound (JDBC,
#     cloud SDK) and benefit from process-level isolation for JDBC
#     connection stability (JPype1/JVM is not thread-safe across workers)
#   - Scale horizontally via Kubernetes HPA rather than per-pod workers
CMD ["gunicorn", \
     "--bind", "0.0.0.0:5005", \
     "--workers", "4", \
     "--timeout", "300", \
     "--access-logfile", "-", \
     "--error-logfile", "-", \
     "provisioning_service.app:create_app()"]
