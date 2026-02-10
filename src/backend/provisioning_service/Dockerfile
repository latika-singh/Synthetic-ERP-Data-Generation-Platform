# =============================================================================
# Dockerfile for Provisioning Service
# Synthetic ERP Data Generation Platform
# =============================================================================
#
# Multi-stage Docker build for the Provisioning Service that handles database
# provisioning (PostgreSQL 12-16, Oracle 19c-23ai, SQL Server 2019-2022,
# SAP HANA 2.0 SPS 07+) and cloud storage export (AWS S3, Azure Blob Storage,
# GCP Cloud Storage). Includes JDBC drivers for all supported target databases
# and cloud SDKs for multi-cloud export support.
#
# Build context: src/backend/provisioning_service/
# Usage:
#   docker build -t synthetic-erp-provisioning-service:latest \
#     -f src/backend/provisioning_service/Dockerfile .
#
# Air-gapped deployment (C-003): All dependencies are installed at build time.
#   For air-gapped environments, use a private PyPI mirror:
#   docker build --build-arg PIP_INDEX_URL=https://private-pypi.internal/simple/ ...
#
# =============================================================================

# ---------------------------------------------------------------------------
# Stage 1: Builder
# ---------------------------------------------------------------------------
# Installs system-level build dependencies, JDK for jaydebeapi JDBC
# compilation, downloads JDBC driver JARs, and installs all Python
# dependencies into an isolated virtual environment.
# ---------------------------------------------------------------------------
FROM python:3.12-slim AS builder

# Metadata labels for the builder stage (carried forward by convention)
LABEL stage="builder"

# Set build-time working directory
WORKDIR /build

# Install system-level build dependencies required for:
#  - gcc, g++: Compiling native C/C++ extensions (pyarrow, cryptography, JPype1)
#  - libffi-dev: Foreign Function Interface library for cffi-based packages
#  - python3-dev: Python development headers for native extension compilation
#  - default-jdk-headless: Full JDK needed by JPype1/jaydebeapi for JDBC compile
#  - curl: Downloading JDBC driver JARs from Maven Central / vendor sites
#  - unzip: Extracting compressed driver archives
#  - libssl-dev: OpenSSL development headers for the cryptography package
#  - ca-certificates: TLS certificate bundle for secure downloads
RUN apt-get update && \
    apt-get install -y --no-install-recommends \
        gcc \
        g++ \
        libffi-dev \
        python3-dev \
        default-jdk-headless \
        curl \
        unzip \
        libssl-dev \
        ca-certificates \
    && rm -rf /var/lib/apt/lists/*

# Set JAVA_HOME for JDK used during JPype1/jaydebeapi compilation
ENV JAVA_HOME=/usr/lib/jvm/java-17-openjdk-amd64
ENV PATH="${JAVA_HOME}/bin:${PATH}"

# ---------------------------------------------------------------------------
# Download JDBC Driver JARs
# ---------------------------------------------------------------------------
# All JDBC drivers are placed in /opt/jdbc-drivers/ for use at runtime.
# For air-gapped deployments (C-003), pre-stage these JARs into the build
# context or a local artifact server and use COPY instead of curl.
# ---------------------------------------------------------------------------
RUN mkdir -p /opt/jdbc-drivers

# PostgreSQL JDBC Driver 42.7.3
# Supports PostgreSQL 12.x through 16.x
# Source: Maven Central Repository
RUN curl -fsSL -o /opt/jdbc-drivers/postgresql-42.7.3.jar \
    "https://jdbc.postgresql.org/download/postgresql-42.7.3.jar" && \
    echo "PostgreSQL JDBC driver downloaded successfully"

# Microsoft SQL Server JDBC Driver 12.4.2 (JRE 11 compatible)
# Supports SQL Server 2019 through 2022
# Source: Microsoft Maven Repository
RUN curl -fsSL -o /opt/jdbc-drivers/mssql-jdbc-12.4.2.jre11.jar \
    "https://repo1.maven.org/maven2/com/microsoft/sqlserver/mssql-jdbc/12.4.2.jre11/mssql-jdbc-12.4.2.jre11.jar" && \
    echo "SQL Server JDBC driver downloaded successfully"

# Oracle JDBC Thin Driver (ojdbc11.jar)
# Supports Oracle 19c through 23ai
# NOTE: Oracle JDBC drivers require acceptance of Oracle Technology Network
# License. For production, download from Oracle's Maven repository or supply
# via build context. The URL below uses Maven Central where ojdbc11 is
# published under the com.oracle.database.jdbc group.
RUN curl -fsSL -o /opt/jdbc-drivers/ojdbc11.jar \
    "https://repo1.maven.org/maven2/com/oracle/database/jdbc/ojdbc11/23.3.0.23.09/ojdbc11-23.3.0.23.09.jar" && \
    echo "Oracle JDBC driver downloaded successfully"

# SAP HANA JDBC Driver (ngdbc.jar)
# Supports SAP HANA 2.0 SPS 07+
# NOTE: SAP HANA JDBC driver (ngdbc.jar) requires a valid SAP license and
# is not available on public Maven repositories. For production deployments:
#   1. Download ngdbc.jar from SAP Development Tools or SAP ONE Support Launchpad
#   2. Place in build context and use: COPY ngdbc.jar /opt/jdbc-drivers/ngdbc.jar
#   3. Or host on a private artifact repository
# The following creates a placeholder to prevent runtime errors if the JAR
# is not provided. When the actual driver is available, replace this block.
RUN if [ ! -f /opt/jdbc-drivers/ngdbc.jar ]; then \
        echo "WARNING: SAP HANA JDBC driver (ngdbc.jar) not found." && \
        echo "SAP HANA provisioning will not be available." && \
        echo "To enable SAP HANA support, provide ngdbc.jar in the build context." && \
        touch /opt/jdbc-drivers/ngdbc.jar.placeholder; \
    fi

# Verify JDBC driver directory contents
RUN ls -la /opt/jdbc-drivers/

# ---------------------------------------------------------------------------
# Python Virtual Environment and Dependency Installation
# ---------------------------------------------------------------------------
# Create an isolated virtual environment for all Python packages. This venv
# is copied to the production stage to keep the final image lean.
# ---------------------------------------------------------------------------
RUN python -m venv /opt/venv
ENV PATH="/opt/venv/bin:${PATH}"

# Copy requirements.txt first for Docker layer caching optimization.
# Changes to application code will not invalidate the dependency layer.
COPY requirements.txt /build/requirements.txt

# Install all Python dependencies into the virtual environment.
# Cloud SDKs (boto3, azure-storage-blob, google-cloud-storage) are installed
# here along with jaydebeapi, pyarrow, and all other service dependencies.
# --no-cache-dir: Reduce image size by skipping pip's download cache
RUN pip install --no-cache-dir --upgrade pip setuptools wheel && \
    pip install --no-cache-dir -r requirements.txt

# Verify dependency consistency (no conflicts or missing packages)
RUN pip check

# Copy application source code into the builder stage
COPY . /build/app

# ---------------------------------------------------------------------------
# Stage 2: Production
# ---------------------------------------------------------------------------
# Minimal runtime image with only the dependencies needed to run the
# Provisioning Service. Build tools, compilers, and development headers
# are excluded to reduce attack surface and image size.
# ---------------------------------------------------------------------------
FROM python:3.12-slim AS production

# Container metadata labels following OCI Image Spec
LABEL maintainer="Synthetic ERP Platform Team" \
      service="provisioning-service" \
      version="1.0.0" \
      description="Database provisioning and cloud storage export service with JDBC and Cloud SDK support" \
      org.opencontainers.image.title="synthetic-erp-provisioning-service" \
      org.opencontainers.image.description="Provisioning Service for Synthetic ERP Data Generation Platform" \
      org.opencontainers.image.vendor="Synthetic ERP Platform" \
      org.opencontainers.image.version="1.0.0" \
      org.opencontainers.image.source="src/backend/provisioning_service/Dockerfile"

# Install minimal runtime dependencies:
#  - default-jre-headless: Java Runtime Environment for JDBC driver execution
#    via jaydebeapi/JPype1 (no full JDK needed at runtime)
#  - ca-certificates: TLS certificate bundle for secure connections to
#    cloud storage APIs (AWS S3, Azure Blob, GCP) and target databases
#  - curl: Required for Docker HEALTHCHECK probe and debugging
#  - tini: Lightweight init system for proper signal handling (PID 1)
RUN apt-get update && \
    apt-get install -y --no-install-recommends \
        default-jre-headless \
        ca-certificates \
        curl \
        tini \
    && rm -rf /var/lib/apt/lists/*

# Set JAVA_HOME for JRE runtime (used by jaydebeapi for JDBC operations)
ENV JAVA_HOME=/usr/lib/jvm/java-17-openjdk-amd64
ENV PATH="${JAVA_HOME}/bin:${PATH}"

# ---------------------------------------------------------------------------
# Copy artifacts from builder stage
# ---------------------------------------------------------------------------

# Copy the Python virtual environment with all installed packages
COPY --from=builder /opt/venv /opt/venv

# Copy JDBC driver JARs to the standard driver directory
COPY --from=builder /opt/jdbc-drivers /opt/jdbc-drivers

# Set virtual environment on PATH
ENV PATH="/opt/venv/bin:${PATH}"

# ---------------------------------------------------------------------------
# Application Setup
# ---------------------------------------------------------------------------

# Set working directory for the application
WORKDIR /app

# Copy application source code from builder stage
COPY --from=builder /build/app /app

# ---------------------------------------------------------------------------
# Security: Non-root user
# ---------------------------------------------------------------------------
# Create a dedicated non-root system user and group for running the service.
# This follows Docker and Kubernetes security best practices to minimize
# the blast radius of potential container breakouts.
# ---------------------------------------------------------------------------
RUN addgroup --system appuser && \
    adduser --system --ingroup appuser --no-create-home --disabled-password appuser

# Create required directories with appropriate ownership:
#  - /tmp/provisioning-exports: Temporary staging area for export files
#    before upload to cloud storage or delivery to target databases
RUN mkdir -p /tmp/provisioning-exports && \
    chown -R appuser:appuser /app /tmp/provisioning-exports /opt/jdbc-drivers

# ---------------------------------------------------------------------------
# Environment Variables
# ---------------------------------------------------------------------------
# Configure the Python and Flask runtime environment following 12-factor
# app methodology. All service-specific settings are provided via
# environment variables at container runtime (Docker Compose, Kubernetes).
# ---------------------------------------------------------------------------

# Python runtime settings
ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PYTHONPATH=/app

# Flask application settings
ENV FLASK_APP=app:create_app \
    FLASK_ENV=production

# Service identification
ENV SERVICE_NAME=provisioning-service

# JDBC classpath configuration: Makes all JAR files in /opt/jdbc-drivers/
# available to the JVM when jaydebeapi creates JDBC connections
ENV CLASSPATH="/opt/jdbc-drivers/*" \
    JDBC_DRIVER_PATH=/opt/jdbc-drivers

# Default AWS region (overrideable at runtime via environment variable)
ENV AWS_DEFAULT_REGION=us-east-1

# OpenTelemetry service identification for distributed tracing
ENV OTEL_SERVICE_NAME=provisioning-service \
    OTEL_EXPORTER_OTLP_ENDPOINT=http://otel-collector:4317

# ---------------------------------------------------------------------------
# Network Configuration
# ---------------------------------------------------------------------------
# Expose the Provisioning Service HTTP port. This port is used for:
#  - REST API endpoints for export and provisioning operations
#  - Health check and readiness probe endpoints
#  - Prometheus metrics scraping endpoint
# ---------------------------------------------------------------------------
EXPOSE 8005

# ---------------------------------------------------------------------------
# Health Check
# ---------------------------------------------------------------------------
# Docker native health check that queries the /health endpoint.
# Kubernetes liveness and readiness probes also target this endpoint.
#  - interval: Check every 30 seconds
#  - timeout: Fail if no response within 10 seconds
#  - start-period: Allow 15 seconds for JVM and JDBC driver initialization
#  - retries: Mark unhealthy after 3 consecutive failures
# ---------------------------------------------------------------------------
HEALTHCHECK --interval=30s --timeout=10s --start-period=15s --retries=3 \
    CMD curl --fail --silent http://localhost:8005/health || exit 1

# Switch to non-root user before running the application
USER appuser

# ---------------------------------------------------------------------------
# Container Entrypoint
# ---------------------------------------------------------------------------
# Use tini as PID 1 for proper signal forwarding to Gunicorn, ensuring
# graceful shutdown of JDBC connections and cloud SDK sessions.
# Gunicorn configuration:
#  - bind: Listen on all interfaces, port 8005
#  - workers: 4 worker processes for concurrent provisioning operations
#  - timeout: 120 seconds to accommodate large batch database inserts
#    and multi-part cloud storage uploads
#  - access-logfile: Log access requests to stdout for container log collection
#  - error-logfile: Log errors to stderr for container log collection
# ---------------------------------------------------------------------------
ENTRYPOINT ["tini", "--"]

CMD ["gunicorn", \
     "--bind", "0.0.0.0:8005", \
     "--workers", "4", \
     "--timeout", "120", \
     "--access-logfile", "-", \
     "--error-logfile", "-", \
     "--graceful-timeout", "30", \
     "--keep-alive", "5", \
     "wsgi:app"]
