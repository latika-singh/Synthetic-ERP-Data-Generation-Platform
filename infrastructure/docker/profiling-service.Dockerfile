# =============================================================================
# Profiling Service Dockerfile
# =============================================================================
# Production-optimized multi-stage Dockerfile for the Profiling Service.
# This service performs ERP schema discovery and statistical profiling via JDBC
# connections to SAP, Oracle E-Business Suite, Microsoft Dynamics 365, and
# legacy JDBC-accessible systems — extracting metadata only, never raw
# production data (Constraint C-001).
#
# Features:
#   - Multi-stage build for minimal final image size
#   - JDBC driver support for ERP schema discovery
#   - JRE headless runtime (no full JDK in production)
#   - Non-root user execution for security hardening
#   - Health check endpoint for Kubernetes liveness/readiness probes
#   - Gunicorn WSGI server for production concurrency
#   - Air-gapped deployment readiness (C-003): all dependencies bundled
#
# Build context: Project root (.)
# Build command:
#   docker build -f infrastructure/docker/profiling-service.Dockerfile -t synthetic-erp-profiling-service .
#
# Run command:
#   docker run -p 5002:5002 --env-file .env synthetic-erp-profiling-service
# =============================================================================

# ---------------------------------------------------------------------------
# Stage 1: Builder
# ---------------------------------------------------------------------------
# Installs all build-time dependencies including JDK (required for
# jaydebeapi/JPype1 JDBC compilation), C compilers (required for scipy,
# numpy native extensions), and downloads JDBC driver JARs for ERP
# connectivity.
# ---------------------------------------------------------------------------
FROM python:3.12-slim AS builder

# Container metadata labels (OCI standard)
LABEL org.opencontainers.image.title="synthetic-erp-profiling-service" \
      org.opencontainers.image.description="ERP schema discovery and statistical profiling service with JDBC connectivity" \
      org.opencontainers.image.source="infrastructure/docker/profiling-service.Dockerfile"

WORKDIR /build

# Install system-level build dependencies:
#   - gcc, g++: C/C++ compilers for scipy, numpy native extensions
#   - python3-dev: Python header files for native extension compilation
#   - libffi-dev: Foreign Function Interface library for cffi/cryptography
#   - default-jdk-headless: Full JDK required for jaydebeapi/JPype1 compilation
#   - curl: For downloading JDBC driver JARs from Maven Central or local mirror
#   - unzip: For extracting any archived JDBC driver packages
RUN apt-get update && \
    apt-get install -y --no-install-recommends \
        gcc \
        g++ \
        python3-dev \
        libffi-dev \
        default-jdk-headless \
        curl \
        unzip \
    && rm -rf /var/lib/apt/lists/*

# Set JAVA_HOME for jaydebeapi/JPype1 compilation against JDK 17
ENV JAVA_HOME=/usr/lib/jvm/java-17-openjdk-amd64

# Create isolated Python virtual environment for clean dependency management
RUN python -m venv /opt/venv
ENV PATH="/opt/venv/bin:$PATH"

# Copy and install Python dependencies first for Docker layer caching.
# Subsequent code changes will not invalidate the dependency layer.
COPY src/backend/profiling_service/requirements.txt /build/requirements.txt
RUN pip install --no-cache-dir --upgrade pip setuptools wheel && \
    pip install --no-cache-dir -r requirements.txt && \
    pip check

# ---------------------------------------------------------------------------
# Download JDBC Driver JARs
# ---------------------------------------------------------------------------
# These drivers enable the Profiling Service to connect to ERP database
# systems for schema metadata extraction. For air-gapped deployments (C-003),
# replace the Maven Central URLs below with your private artifact mirror.
# ---------------------------------------------------------------------------
RUN mkdir -p /opt/jdbc-drivers

# PostgreSQL JDBC Driver 42.7.3
# Source: Maven Central (or private mirror for air-gapped environments)
RUN curl -fSL -o /opt/jdbc-drivers/postgresql-42.7.3.jar \
    "https://repo1.maven.org/maven2/org/postgresql/postgresql/42.7.3/postgresql-42.7.3.jar" \
    || echo "WARN: PostgreSQL JDBC driver download failed — provide manually for air-gapped builds"

# Microsoft SQL Server JDBC Driver 12.4.2 (JRE 11 compatible)
# Source: Maven Central (or private mirror for air-gapped environments)
RUN curl -fSL -o /opt/jdbc-drivers/mssql-jdbc-12.4.2.jre11.jar \
    "https://repo1.maven.org/maven2/com/microsoft/sqlserver/mssql-jdbc/12.4.2.jre11/mssql-jdbc-12.4.2.jre11.jar" \
    || echo "WARN: SQL Server JDBC driver download failed — provide manually for air-gapped builds"

# Oracle JDBC Thin Driver (ojdbc11)
# NOTE: Oracle JDBC drivers are subject to Oracle Technology Network License.
# For production use, download ojdbc11.jar from Oracle's website or your
# organization's licensed artifact repository and place it at:
#   /opt/jdbc-drivers/ojdbc11.jar
# The following attempts to fetch from Maven Central where Oracle has published
# a redistributable version:
RUN curl -fSL -o /opt/jdbc-drivers/ojdbc11.jar \
    "https://repo1.maven.org/maven2/com/oracle/database/jdbc/ojdbc11/23.3.0.23.09/ojdbc11-23.3.0.23.09.jar" \
    || echo "WARN: Oracle JDBC driver download failed — provide ojdbc11.jar manually"

# SAP HANA JDBC Driver (ngdbc.jar)
# NOTE: The SAP HANA JDBC driver (ngdbc.jar) requires an SAP license and is
# NOT available on public Maven repositories. For deployments requiring SAP
# HANA connectivity:
#   1. Obtain ngdbc.jar from SAP Support Portal (SAP Software Download Center)
#   2. Place it in the build context or a private artifact repository
#   3. Uncomment and adjust the COPY line below:
# COPY drivers/ngdbc.jar /opt/jdbc-drivers/ngdbc.jar
#
# Alternatively, for air-gapped builds, mount a volume with the driver:
#   docker build --build-context drivers=/path/to/sap-drivers ...
RUN echo "INFO: SAP HANA JDBC driver (ngdbc.jar) requires SAP license — add manually if needed"

# Verify JDBC driver directory contents
RUN ls -la /opt/jdbc-drivers/ && \
    echo "JDBC drivers staged successfully"


# ---------------------------------------------------------------------------
# Stage 2: Runtime
# ---------------------------------------------------------------------------
# Minimal production image with only the JRE (not JDK), Python virtual
# environment with all dependencies, JDBC drivers, and application code.
# Runs as non-root user for security hardening.
# ---------------------------------------------------------------------------
FROM python:3.12-slim AS runtime

# Container metadata labels (OCI standard) — repeated for final stage
LABEL org.opencontainers.image.title="synthetic-erp-profiling-service" \
      org.opencontainers.image.description="ERP schema discovery and statistical profiling service with JDBC connectivity" \
      org.opencontainers.image.source="infrastructure/docker/profiling-service.Dockerfile"

# Install minimal runtime dependencies:
#   - default-jre-headless: JRE for jaydebeapi JDBC runtime (NO full JDK)
#   - ca-certificates: TLS certificate bundle for secure ERP connections
#   - curl: Required for container health check probes
RUN apt-get update && \
    apt-get install -y --no-install-recommends \
        default-jre-headless \
        ca-certificates \
        curl \
    && rm -rf /var/lib/apt/lists/*

# ---------------------------------------------------------------------------
# Environment Configuration
# ---------------------------------------------------------------------------
# Python runtime settings
ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1

# Flask application configuration
ENV FLASK_APP=profiling_service.app:create_app() \
    FLASK_ENV=production

# Python module resolution — allows importing profiling_service and shared
ENV PYTHONPATH=/app

# Service identification for logging and observability
ENV SERVICE_NAME=profiling-service

# Java runtime configuration for jaydebeapi JDBC connectivity
ENV JAVA_HOME=/usr/lib/jvm/java-17-openjdk-amd64

# JDBC driver classpath — jaydebeapi uses this to locate driver JARs
ENV CLASSPATH=/opt/jdbc-drivers/*

# Explicit JDBC driver path for application-level driver loading
ENV JDBC_DRIVER_PATH=/opt/jdbc-drivers

# ---------------------------------------------------------------------------
# Security: Non-root user
# ---------------------------------------------------------------------------
# Create a dedicated system user and group for running the application.
# This follows the principle of least privilege (Docker security best practice).
RUN addgroup --system appuser && \
    adduser --system --ingroup appuser appuser

# ---------------------------------------------------------------------------
# Copy artifacts from builder stage
# ---------------------------------------------------------------------------
# Copy the Python virtual environment with all installed dependencies
COPY --from=builder /opt/venv /opt/venv

# Copy JDBC driver JARs for ERP database connectivity
COPY --from=builder /opt/jdbc-drivers /opt/jdbc-drivers

# Activate virtual environment via PATH
ENV PATH="/opt/venv/bin:$PATH"

# ---------------------------------------------------------------------------
# Application code
# ---------------------------------------------------------------------------
WORKDIR /app

# Copy the Profiling Service application code
COPY src/backend/profiling_service/ /app/profiling_service/

# Copy shared utilities (database, auth, logging, observability, config)
COPY src/backend/shared/ /app/shared/

# Set ownership of application directory to non-root user
RUN chown -R appuser:appuser /app

# Switch to non-root user for all subsequent operations
USER appuser

# ---------------------------------------------------------------------------
# Network and health configuration
# ---------------------------------------------------------------------------
# Expose the Profiling Service HTTP port
EXPOSE 5002

# Docker health check — Kubernetes also uses liveness/readiness probes,
# but this provides a baseline for Docker Compose and standalone runs.
# Parameters:
#   --interval=30s:      Check every 30 seconds
#   --timeout=10s:       Fail if check takes longer than 10 seconds
#   --start-period=15s:  Grace period for service startup before checks begin
#   --retries=3:         Mark unhealthy after 3 consecutive failures
HEALTHCHECK --interval=30s --timeout=10s --start-period=15s --retries=3 \
    CMD curl -f http://localhost:5002/health || exit 1

# ---------------------------------------------------------------------------
# Entrypoint: Gunicorn WSGI Server
# ---------------------------------------------------------------------------
# Gunicorn configuration:
#   --bind 0.0.0.0:5002:    Listen on all interfaces, port 5002
#   --workers 4:             4 worker processes for concurrent request handling
#   --timeout 120:           120s worker timeout for long-running schema discovery
#   --access-logfile -:      Stream access logs to stdout (for container log aggregation)
#   --error-logfile -:       Stream error logs to stderr (for container log aggregation)
CMD ["gunicorn", \
     "--bind", "0.0.0.0:5002", \
     "--workers", "4", \
     "--timeout", "120", \
     "--access-logfile", "-", \
     "--error-logfile", "-", \
     "profiling_service.app:create_app()"]
