# =============================================================================
# Dockerfile — Compliance Service
# Synthetic-ERP-Data-Generation-Platform
#
# Multi-stage Docker build for the Compliance Service.
#
# Stage 1 (builder):
#   Compiles native extensions (cryptography, spaCy C bindings) and installs
#   all Python dependencies into an isolated virtual environment. Pre-downloads
#   the spaCy NLP model (en_core_web_sm by default) for PII entity recognition
#   so that the production image can operate in air-gapped environments without
#   internet access (Constraint C-003).
#
# Stage 2 (production):
#   Minimal runtime image with Gunicorn 21.x WSGI server, non-root user for
#   security hardening (SOC 2 Type II — Constraint C-004), Docker HEALTHCHECK,
#   and tini for proper PID 1 signal handling in Kubernetes.
#
# Build context: src/backend/ (set via docker-compose.yml or CI/CD pipeline)
#
# Build examples:
#   docker build -f compliance_service/Dockerfile -t compliance-service:latest .
#   docker build -f compliance_service/Dockerfile \
#       --build-arg SPACY_MODEL=en_core_web_lg \
#       --build-arg BUILD_DATE=$(date -u +'%Y-%m-%dT%H:%M:%SZ') \
#       --build-arg VCS_REF=$(git rev-parse --short HEAD) \
#       -t compliance-service:latest .
#
# Run:
#   docker run -p 5004:5004 --env-file .env compliance-service:latest
# =============================================================================

# ---------------------------------------------------------------------------
# Global build arguments
# ---------------------------------------------------------------------------
ARG PYTHON_VERSION=3.12
ARG BUILD_DATE
ARG VCS_REF
ARG SPACY_MODEL=en_core_web_sm


# =============================================================================
# Stage 1 — Builder
#
# Installs system-level build dependencies (gcc, libffi, libssl) for compiling
# native Python extensions, creates an isolated virtual environment, installs
# all pip packages from requirements.txt, and pre-downloads the spaCy NLP model
# for air-gapped PII entity recognition.
# =============================================================================
FROM python:${PYTHON_VERSION}-slim AS builder

# Re-declare ARGs after FROM to make them available in this stage
ARG SPACY_MODEL

# Prevent .pyc files and enable unbuffered stdout/stderr during build
ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

WORKDIR /build

# Install system-level build dependencies required for compiling native Python
# extensions: cryptography (libffi, libssl), spaCy (gcc, g++), and other C
# extensions. These are only needed at build time and will NOT be present in
# the production image, keeping it minimal and secure.
RUN apt-get update \
    && apt-get install -y --no-install-recommends \
        gcc \
        g++ \
        python3-dev \
        libffi-dev \
        libssl-dev \
        build-essential \
    && rm -rf /var/lib/apt/lists/*

# Create an isolated virtual environment for all Python dependencies.
# Using a venv allows clean COPY to the production stage without dragging
# in system-level build artifacts, and properly contains spaCy models.
RUN python -m venv /opt/venv
ENV PATH="/opt/venv/bin:$PATH"

# Copy requirements.txt FIRST to maximize Docker layer caching.
# If dependencies have not changed, Docker will reuse the cached layer even
# when application source code changes.
COPY compliance_service/requirements.txt ./requirements.txt

# Install all Python dependencies from requirements.txt into the virtual
# environment. The --no-cache-dir flag prevents pip from storing download
# caches, reducing the builder image size.
RUN pip install --no-cache-dir --upgrade pip setuptools wheel \
    && pip install --no-cache-dir -r requirements.txt

# Pre-download the spaCy NLP model for PII entity recognition.
# This bakes the model into the virtual environment so it is available at
# runtime without internet access, satisfying the air-gapped deployment
# requirement (Constraint C-003). The default model is en_core_web_sm;
# pass --build-arg SPACY_MODEL=en_core_web_lg for higher accuracy.
RUN python -m spacy download ${SPACY_MODEL}

# Verify the spaCy model was downloaded correctly and is loadable.
# This catches model download failures early in the build process rather
# than at container startup time.
RUN python -c "\
import spacy; \
nlp = spacy.load('${SPACY_MODEL}'); \
print(f'spaCy model ${SPACY_MODEL} loaded — pipeline: {nlp.pipe_names}')"


# =============================================================================
# Stage 2 — Production
#
# Minimal runtime image without build tools, compilers, or package manager
# caches. Runs the Flask application via Gunicorn as a non-root user.
# =============================================================================
FROM python:${PYTHON_VERSION}-slim AS production

# Re-declare build args needed for labels and environment
ARG BUILD_DATE
ARG VCS_REF
ARG SPACY_MODEL=en_core_web_sm

# ---------------------------------------------------------------------------
# OCI-compliant image labels for metadata and traceability
# ---------------------------------------------------------------------------
LABEL maintainer="Synthetic-ERP-Data-Generation-Platform Team" \
      description="Compliance Service — Dual-layer PII detection (spaCy NLP + regex), regulatory compliance verification (GDPR, HIPAA, CCPA), tamper-evident dataset certification, and audit logging for the Synthetic ERP Data Generation Platform." \
      version="1.0.0" \
      org.opencontainers.image.title="compliance-service" \
      org.opencontainers.image.description="Compliance Service with PII detection and regulatory verification" \
      org.opencontainers.image.version="1.0.0" \
      org.opencontainers.image.created="${BUILD_DATE}" \
      org.opencontainers.image.revision="${VCS_REF}" \
      org.opencontainers.image.vendor="Synthetic-ERP-Data-Generation-Platform" \
      org.opencontainers.image.source="https://github.com/synthetic-erp/platform" \
      org.opencontainers.image.base.name="python:${PYTHON_VERSION}-slim"

# ---------------------------------------------------------------------------
# Environment variables for Python runtime and application configuration.
# All application-specific settings (MongoDB URI, Redis URL, Auth0, etc.) are
# injected at runtime via environment variables per 12-factor app methodology —
# no credentials or secrets are baked into the image.
# ---------------------------------------------------------------------------
ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    FLASK_ENV=production \
    SPACY_MODEL=${SPACY_MODEL} \
    PORT=5004 \
    PATH="/opt/venv/bin:$PATH"

# ---------------------------------------------------------------------------
# Install minimal runtime system dependencies.
# No compilers, dev headers, or build tools — only what the application
# needs at runtime.
# ---------------------------------------------------------------------------
RUN apt-get update \
    && apt-get install -y --no-install-recommends \
        curl \
        tini \
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
# This includes all installed pip packages AND the pre-downloaded spaCy NLP
# model, ensuring air-gapped operation (Constraint C-003).
# ---------------------------------------------------------------------------
COPY --from=builder /opt/venv /opt/venv

# ---------------------------------------------------------------------------
# Application code
#
# Build context is expected to be src/backend/ so that both the shared
# utilities package and the compliance_service package are accessible.
# The docker-compose.yml or CI/CD pipeline sets:
#   context: src/backend/
#   dockerfile: compliance_service/Dockerfile
# ---------------------------------------------------------------------------
WORKDIR /app

# Copy the shared utilities package (database, auth, logging, observability,
# middleware, config) used by all backend microservices.
COPY shared/ /app/shared/

# Copy the Compliance Service application code.
COPY compliance_service/ /app/

# Ensure the non-root user owns all application files.
RUN chown -R appuser:appuser /app

# ---------------------------------------------------------------------------
# Switch to the non-root user for all subsequent commands and at runtime.
# ---------------------------------------------------------------------------
USER appuser

# ---------------------------------------------------------------------------
# Expose the Compliance Service port.
# This is informational metadata — actual port binding is controlled by the
# Gunicorn --bind flag in CMD and the Kubernetes Service definition.
# ---------------------------------------------------------------------------
EXPOSE 5004

# ---------------------------------------------------------------------------
# Docker HEALTHCHECK
#
# Uses Python's built-in urllib (no curl dependency required) to probe the
# /health liveness endpoint. Parameters:
#   --interval=30s   : Check every 30 seconds
#   --timeout=10s    : Fail if no response within 10 seconds
#   --start-period=40s : Grace period for spaCy model loading at startup
#   --retries=3      : Mark unhealthy after 3 consecutive failures
# ---------------------------------------------------------------------------
HEALTHCHECK --interval=30s --timeout=10s --start-period=40s --retries=3 \
    CMD python -c "import urllib.request; urllib.request.urlopen('http://localhost:5004/health')" || exit 1

# ---------------------------------------------------------------------------
# Container entrypoint and command
#
# tini (https://github.com/krallin/tini) is used as PID 1 to properly handle
# SIGTERM signal forwarding, zombie process reaping, and graceful shutdown —
# critical for Kubernetes pod lifecycle management.
#
# Gunicorn 21.x serves the Flask WSGI application with:
#   --workers 4        : 4 worker processes for concurrent request handling
#   --threads 2        : 2 threads per worker for I/O-bound compliance scans
#   --timeout 120      : 120s worker timeout for long-running PII scans
#   --graceful-timeout 30 : 30s grace for in-flight requests during shutdown
#   --keep-alive 5     : TCP keep-alive for connection reuse
#   --worker-tmp-dir /dev/shm : Use tmpfs for heartbeat files (K8s best practice)
#   --access-logfile - : Stream access logs to stdout for container log collection
#   --error-logfile -  : Stream error logs to stderr for container log collection
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
     "wsgi:app"]
