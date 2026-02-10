# ============================================================================
# Synthetic ERP Data Generation Platform — Compliance Service Dockerfile
# ============================================================================
# Production-optimized multi-stage build for the Compliance Service, which
# performs PII detection and regulatory compliance verification (GDPR, HIPAA,
# CCPA) on generated synthetic datasets. This service integrates spaCy NLP
# for entity recognition (names, addresses, organizations) and regex pattern
# matching for structured PII (SSN, email, financial accounts).
#
# CRITICAL: spaCy language models are pre-downloaded at build time and
# embedded in the virtual environment. This ensures the container is fully
# self-contained and can run in air-gapped environments without any runtime
# internet access — fulfilling Constraint C-003.
#
# Build context: Project root (.)
# Build command:
#   docker build -f infrastructure/docker/compliance-service.Dockerfile \
#     --build-arg BUILD_DATE=$(date -u +"%Y-%m-%dT%H:%M:%SZ") \
#     --build-arg VCS_REF=$(git rev-parse --short HEAD) \
#     -t synthetic-erp-compliance-service:latest .
#
# For larger spaCy model (better accuracy, larger image):
#   docker build -f infrastructure/docker/compliance-service.Dockerfile \
#     --build-arg SPACY_MODEL=en_core_web_lg \
#     -t synthetic-erp-compliance-service:latest .
#
# For air-gapped environments (C-003):
#   docker build -f infrastructure/docker/compliance-service.Dockerfile \
#     --build-arg PIP_INDEX_URL=https://private-pypi.internal/simple \
#     --build-arg PIP_TRUSTED_HOST=private-pypi.internal \
#     -t synthetic-erp-compliance-service:latest .
# ============================================================================

# ---------------------------------------------------------------------------
# Build arguments for image metadata, air-gapped registry support, and
# configurable spaCy model selection
# ---------------------------------------------------------------------------
ARG PYTHON_VERSION=3.12
ARG BUILD_DATE
ARG VCS_REF

# spaCy model selection: en_core_web_sm (~12MB, fast) is the default for
# speed-optimized PII detection. Override with en_core_web_lg (~560MB) for
# higher accuracy entity recognition at the cost of larger image size and
# longer model loading time.
ARG SPACY_MODEL=en_core_web_sm

# Air-gapped support: override PIP_INDEX_URL to point to a private PyPI mirror
# when building in environments without public internet access.
ARG PIP_INDEX_URL=https://pypi.org/simple
ARG PIP_TRUSTED_HOST=pypi.org

# ============================================================================
# Stage 1: Builder
# Purpose: Install system-level build dependencies, Python packages, and
#          pre-download the spaCy NLP language model into an isolated virtual
#          environment. This stage is discarded in the final image to minimize
#          attack surface and image size. The spaCy model is embedded within
#          the venv's site-packages directory, ensuring it carries over to the
#          runtime stage via a single COPY.
# ============================================================================
FROM python:${PYTHON_VERSION}-slim AS builder

# Metadata for the builder stage (informational)
LABEL stage=builder

# Set working directory for the build context
WORKDIR /build

# Re-declare ARGs after FROM to make them available in this build stage.
# Docker scoping rules require ARGs to be re-declared after each FROM.
ARG SPACY_MODEL
ARG PIP_INDEX_URL
ARG PIP_TRUSTED_HOST

# Install system-level build dependencies required for compiling native
# Python extensions:
#   - gcc, g++: C/C++ compilers for native extensions (cryptography, spaCy
#     Cython components, pydantic-core compiled validators)
#   - python3-dev: Python development headers for C extension compilation
#   - libffi-dev: Foreign function interface library for cffi/cryptography
#     package which provides AES-256 encryption primitives
# These are only needed at build time and are NOT included in the runtime image.
RUN apt-get update && \
    apt-get install -y --no-install-recommends \
        gcc \
        g++ \
        python3-dev \
        libffi-dev \
    && rm -rf /var/lib/apt/lists/*

# Create an isolated Python virtual environment at /opt/venv.
# Using a venv ensures clean separation of build artifacts from the system
# Python, enabling efficient multi-stage COPY of only the venv to runtime.
# The spaCy model data files are installed into the venv's site-packages,
# so they will be included in the COPY --from=builder step.
RUN python -m venv /opt/venv

# Prepend the virtual environment bin directory to PATH so all subsequent
# pip and python commands use the venv rather than the system Python.
ENV PATH="/opt/venv/bin:$PATH"

# Copy only the requirements file first to leverage Docker layer caching.
# If requirements.txt hasn't changed, Docker reuses the cached pip install
# layer, significantly speeding up rebuilds during development.
COPY src/backend/compliance_service/requirements.txt /build/requirements.txt

# Install all Python dependencies into the virtual environment.
# --no-cache-dir: Prevents pip from caching downloaded packages, reducing
#   the builder layer size.
# Key packages installed:
#   - flask 3.1.x: Lightweight REST API framework
#   - gunicorn 21.x: Production WSGI HTTP server
#   - spacy 3.7.x: NLP library for entity recognition (PII detection)
#   - pydantic 2.x: Data validation and schema models
#   - pymongo 4.x: MongoDB 7.0 driver for audit log persistence
#   - redis 5.x: Redis 7.x client for caching
#   - python-jose 3.3.x: JWT signing and verification for compliance certs
#   - cryptography 42.x: AES-256 encryption and tamper-evident signing
#   - structlog 24.x: Structured JSON logging
#   - prometheus-client: Prometheus metrics exporter
#   - opentelemetry-api, opentelemetry-sdk: Distributed tracing
RUN pip install --no-cache-dir --upgrade pip setuptools wheel && \
    pip install --no-cache-dir \
        --index-url "${PIP_INDEX_URL}" \
        --trusted-host "${PIP_TRUSTED_HOST}" \
        -r /build/requirements.txt

# ---------------------------------------------------------------------------
# Pre-download spaCy NLP Language Model (CRITICAL for Air-Gapped Deployment)
# ---------------------------------------------------------------------------
# The spaCy model is downloaded at build time and stored within the virtual
# environment's site-packages directory (e.g., /opt/venv/lib/python3.12/
# site-packages/en_core_web_sm/). This ensures:
#   1. Air-gapped compliance (C-003): No internet access required at runtime
#   2. Deterministic builds: Model version is pinned at build time
#   3. Fast container startup: No model download delay on first request
#
# Model options:
#   - en_core_web_sm (~12MB): Fast, suitable for entity recognition of
#     PERSON, ORG, GPE, LOC entities. Default for speed-optimized PII scan.
#   - en_core_web_lg (~560MB): Higher accuracy with word vectors. Use when
#     PII detection precision is critical (e.g., distinguishing real names
#     from product names in financial contexts).
#
# Override at build time:
#   docker build --build-arg SPACY_MODEL=en_core_web_lg ...
RUN python -m spacy download ${SPACY_MODEL}

# Verify that the spaCy model was correctly installed and can be loaded.
# This catch-early validation prevents deploying a container that would fail
# at runtime when the Compliance Service attempts to initialize the NLP
# pipeline for PII entity recognition.
RUN python -c "\
import spacy; \
nlp = spacy.load('${SPACY_MODEL}'); \
print(f'spaCy model [{\"${SPACY_MODEL}\"}] loaded successfully'); \
print(f'Pipeline components: {nlp.pipe_names}'); \
print(f'Vocabulary size: {len(nlp.vocab)}')"

# Verify that all installed packages have consistent dependency versions.
# This catches silent version conflicts that could cause runtime failures,
# especially important given the complex dependency tree of spaCy + Flask +
# cryptography packages.
RUN pip check

# ============================================================================
# Stage 2: Runtime
# Purpose: Minimal production image containing only the Python virtual
#          environment (with all dependencies AND the pre-downloaded spaCy
#          model) and the application source code. No build tools, compilers,
#          or development headers are included, minimizing the attack surface
#          per security requirements and SOC 2 Type II compliance (C-004).
# ============================================================================
FROM python:${PYTHON_VERSION}-slim AS runtime

# Re-declare build arguments needed for labels in this stage
ARG BUILD_DATE
ARG VCS_REF
ARG SPACY_MODEL=en_core_web_sm

# ---------------------------------------------------------------------------
# OCI Image Specification Labels
# Provides standardized container metadata for registry discovery,
# vulnerability scanning tools, and deployment automation.
# ---------------------------------------------------------------------------
LABEL org.opencontainers.image.title="synthetic-erp-compliance-service" \
      org.opencontainers.image.description="PII detection and regulatory compliance service with spaCy NLP" \
      org.opencontainers.image.source="infrastructure/docker/compliance-service.Dockerfile" \
      org.opencontainers.image.vendor="Synthetic ERP Platform" \
      org.opencontainers.image.created="${BUILD_DATE}" \
      org.opencontainers.image.revision="${VCS_REF}" \
      org.opencontainers.image.licenses="Proprietary"

# Install minimal runtime dependencies only — no compilers or dev headers.
#   - curl: Required for Docker/Kubernetes health check probes (HEALTHCHECK CMD)
#   - ca-certificates: Root CA bundle for HTTPS connections to inter-service
#     communication, MongoDB TLS, and Auth0 identity provider calls
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
#   PYTHONPATH: Ensures Python can locate the compliance_service and shared packages
ENV FLASK_APP="compliance_service.app:create_app()" \
    FLASK_ENV=production \
    PYTHONPATH=/app

# Service identification for logging, tracing, and monitoring:
#   SERVICE_NAME: Used by structured logger for service identification in logs
#   SPACY_MODEL_NAME: Identifies which spaCy model is loaded at runtime, used
#     by the NLP detector to call spacy.load() with the correct model name.
#     Can be overridden at runtime via Kubernetes ConfigMap or Docker env vars
#     (provided the model is pre-installed in the venv).
ENV SERVICE_NAME="compliance-service" \
    SPACY_MODEL_NAME="${SPACY_MODEL}"

# ---------------------------------------------------------------------------
# Non-Root User Configuration (Security Requirement)
# ---------------------------------------------------------------------------
# Create a dedicated system group and user with no login shell, no home
# directory, and no password. Running as non-root is a container security
# best practice and is required for SOC 2 Type II compliance (C-004).
# The appuser has no elevated privileges, limiting the blast radius of
# any container compromise. This is especially important for the Compliance
# Service which handles sensitive PII detection logic and audit operations.
RUN addgroup --system appuser && \
    adduser --system --ingroup appuser --no-create-home appuser

# ---------------------------------------------------------------------------
# Copy Virtual Environment from Builder Stage
# ---------------------------------------------------------------------------
# Copy the complete virtual environment containing all installed Python
# packages AND the pre-downloaded spaCy language model from the builder
# stage. The spaCy model is stored within the venv's site-packages (e.g.,
# /opt/venv/lib/python3.12/site-packages/en_core_web_sm/), so this single
# COPY transfers both the Python dependencies and the NLP model data.
# This is the key multi-stage optimization: the runtime image gets only
# the compiled packages without any build tools (gcc, g++, python3-dev).
COPY --from=builder /opt/venv /opt/venv

# Ensure the virtual environment's bin directory is first in PATH so
# python, gunicorn, and all entry-point scripts resolve to the venv.
ENV PATH="/opt/venv/bin:$PATH"

# ---------------------------------------------------------------------------
# Application Code
# ---------------------------------------------------------------------------
# Set the working directory where the application code will reside.
WORKDIR /app

# Copy the Compliance Service code. The COPY context is the project root,
# so paths are relative to the repository root directory.
# The compliance_service package contains:
#   - app.py (Flask Application Factory)
#   - config.py (compliance rules, PII patterns, regulatory frameworks)
#   - detectors/ (PII detection: pii_detector, nlp_detector, pattern_detector)
#   - regulations/ (GDPR, HIPAA, CCPA compliance verification)
#   - certification/ (compliance certifier, audit logger)
COPY src/backend/compliance_service/ /app/compliance_service/

# Copy the shared utilities library used by all backend services.
# Contains: database/ (MongoDB, Redis clients), auth/ (JWT, RBAC),
# logging/ (structured logger), observability/ (metrics, tracing),
# config/ (base config), middleware/ (circuit breaker, health check).
COPY src/backend/shared/ /app/shared/

# Set recursive ownership of all application files to the non-root user.
# This ensures appuser can read all source files and write to any
# application-managed directories if needed (e.g., temporary scan results).
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
# Expose port 5004 for the Flask/Gunicorn HTTP server.
# This is the port the Compliance Service listens on for incoming REST API
# requests from the Generation Engine (POST /verify for compliance checks)
# and the API Gateway (compliance status queries and certification history).
# Kubernetes Service and Docker Compose port mappings reference this port.
EXPOSE 5004

# ---------------------------------------------------------------------------
# Health Check Configuration
# ---------------------------------------------------------------------------
# Docker HEALTHCHECK uses curl to probe the /health endpoint served by the
# Flask health check Blueprint. This endpoint returns HTTP 200 when the
# service is operational and all critical dependencies (MongoDB, Redis,
# spaCy model) are reachable/loaded.
#
# Parameters:
#   --interval=30s: Check every 30 seconds
#   --timeout=10s: Fail if /health doesn't respond within 10 seconds
#                  (slightly higher than API Gateway due to NLP model
#                  involvement in health verification)
#   --start-period=20s: Grace period after container start before first check
#                       (allows Gunicorn worker initialization, Flask app setup,
#                       and spaCy model loading into memory — the NLP pipeline
#                       initialization takes longer than typical Flask apps)
#   --retries=3: Mark unhealthy after 3 consecutive failures
#
# In Kubernetes, the liveness/readiness probes in the Deployment manifest
# typically override this HEALTHCHECK, but it serves as a fallback for
# Docker Compose and standalone Docker deployments.
HEALTHCHECK --interval=30s --timeout=10s --start-period=20s --retries=3 \
    CMD curl -f http://localhost:5004/health || exit 1

# ---------------------------------------------------------------------------
# Container Entrypoint — Gunicorn WSGI Server
# ---------------------------------------------------------------------------
# Run the Flask application via Gunicorn, the production WSGI server.
#
# Gunicorn configuration:
#   --bind 0.0.0.0:5004   : Listen on all interfaces at port 5004
#   --workers 4            : 4 worker processes for parallel request handling.
#                            Each worker loads its own spaCy NLP pipeline
#                            instance for thread-safe PII detection.
#   --timeout 120          : Worker timeout in seconds; allows sufficient time
#                            for large dataset compliance scans that involve
#                            iterating through many records with NLP analysis.
#                            Prevents hung requests from consuming worker slots.
#   --access-logfile -     : Stream access logs to stdout for Docker log driver
#                            and Kubernetes log collectors (Fluentd/Filebeat)
#   --error-logfile -      : Stream error logs to stderr for Docker log driver
#   compliance_service.app:create_app()
#                          : Application factory entry point. Gunicorn calls
#                            create_app() to obtain the Flask WSGI application
#                            instance with all Blueprints, middleware, and
#                            extensions registered. Each worker initializes
#                            independently, loading the spaCy model from the
#                            venv's site-packages.
#
# Workers sizing rationale:
#   - 4 workers: Appropriate for 2-4 CPU core pods in Kubernetes
#   - No --threads: spaCy NLP processing is CPU-bound; additional threads
#     would contend for the GIL during entity recognition. Use multiple
#     processes instead for true parallelism.
#   - Scale horizontally via Kubernetes HPA rather than increasing per-pod workers
CMD ["gunicorn", \
     "--bind", "0.0.0.0:5004", \
     "--workers", "4", \
     "--timeout", "120", \
     "--access-logfile", "-", \
     "--error-logfile", "-", \
     "compliance_service.app:create_app()"]
