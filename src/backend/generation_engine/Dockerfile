# =============================================================================
# Dockerfile — Generation Engine Service
# =============================================================================
# Synthetic-ERP-Data-Generation-Platform
# Service: Generation Engine (Multi-method synthetic data generation)
#
# Multi-stage Docker build optimized for production deployment.
# Supports AI/ML generation (GAN/VAE via PyTorch/TensorFlow), rules-based
# generation, statistical synthesis (SciPy/NumPy), and intelligent masking.
#
# Build:
#   docker build -t generation-engine:latest -f src/backend/generation_engine/Dockerfile .
#
# Run:
#   docker run -p 8001:8001 --env-file .env generation-engine:latest
#
# GPU Build (uncomment GPU base image lines and comment CPU lines):
#   docker build -t generation-engine:gpu --build-arg GPU_ENABLED=true \
#     -f src/backend/generation_engine/Dockerfile .
# =============================================================================

# -----------------------------------------------------------------------------
# GPU Variant (uncomment the following block for GPU-accelerated generation)
# Provides CUDA 12.2 runtime for PyTorch/TensorFlow GPU support.
# Requires NVIDIA Container Toolkit on the host.
# -----------------------------------------------------------------------------
# FROM nvidia/cuda:12.2.0-runtime-ubuntu22.04 AS builder-gpu
# RUN apt-get update && apt-get install -y --no-install-recommends \
#     python3.12 \
#     python3.12-venv \
#     python3.12-dev \
#     python3-pip \
#     gcc \
#     g++ \
#     libffi-dev \
#     curl \
#     && rm -rf /var/lib/apt/lists/*
# RUN ln -sf /usr/bin/python3.12 /usr/bin/python3 && \
#     ln -sf /usr/bin/python3.12 /usr/bin/python
# -----------------------------------------------------------------------------

# =============================================================================
# Stage 1: Builder — Install Python dependencies into a virtual environment
# =============================================================================
FROM python:3.12-slim AS builder

# Install system-level build dependencies required for compiled Python packages
# gcc/g++: C/C++ compiler for native extensions (PyTorch, TensorFlow, SciPy)
# libffi-dev: Foreign Function Interface for cryptography and other packages
# libpq-dev: PostgreSQL client library headers for psycopg2 compatibility
# libhdf5-dev: HDF5 library for TensorFlow model serialization
# curl: HTTP client for health checks and artifact downloads
# default-jre-headless: Java Runtime for JDBC database connectivity
RUN apt-get update && apt-get install -y --no-install-recommends \
    gcc \
    g++ \
    libffi-dev \
    libpq-dev \
    libhdf5-dev \
    curl \
    default-jre-headless \
    && rm -rf /var/lib/apt/lists/*

# Set working directory for the build stage
WORKDIR /build

# Create a virtual environment for clean dependency isolation
RUN python -m venv /opt/venv

# Ensure the virtual environment's bin directory is on PATH so pip and
# all installed console scripts are found first
ENV PATH="/opt/venv/bin:$PATH"

# Upgrade pip, setuptools, and wheel to latest versions for reliable installs
RUN pip install --no-cache-dir --upgrade pip setuptools wheel

# Copy only the requirements file first to leverage Docker layer caching.
# Dependencies change less frequently than application code, so this layer
# is cached across most builds.
COPY src/backend/generation_engine/requirements.txt /build/requirements.txt

# Install all Python dependencies into the virtual environment.
# --no-cache-dir: Prevents pip from caching downloaded packages (smaller image)
# --default-timeout=300: Extended timeout for large packages (PyTorch, TF)
RUN pip install --no-cache-dir --default-timeout=300 -r /build/requirements.txt

# =============================================================================
# Stage 2: Production — Minimal runtime image with application code
# =============================================================================
FROM python:3.12-slim AS production

# -------------------------------------------------------------------------
# OCI Image Labels (https://github.com/opencontainers/image-spec/blob/main/annotations.md)
# -------------------------------------------------------------------------
LABEL org.opencontainers.image.title="Generation Engine Service" \
      org.opencontainers.image.description="Multi-method synthetic ERP data generation engine supporting AI/ML (GAN/VAE), rules-based, statistical, and masking generation strategies." \
      org.opencontainers.image.version="1.0.0" \
      org.opencontainers.image.vendor="Synthetic-ERP-Data-Generation-Platform" \
      org.opencontainers.image.authors="platform-engineering@synthetic-erp.io" \
      org.opencontainers.image.url="https://github.com/synthetic-erp/generation-engine" \
      org.opencontainers.image.source="https://github.com/synthetic-erp/generation-engine" \
      org.opencontainers.image.licenses="MIT"

# -------------------------------------------------------------------------
# Install minimal runtime dependencies (no compilers)
# -------------------------------------------------------------------------
# curl: Required by HEALTHCHECK to probe /health endpoint
# default-jre-headless: Java Runtime for JDBC database connectivity
#   (jaydebeapi requires JVM for connecting to Oracle, SQL Server, SAP HANA)
# libpq5: PostgreSQL client library for runtime psycopg2 usage
# libhdf5-103: HDF5 runtime library for TensorFlow model I/O
# libgomp1: OpenMP runtime for multi-threaded numerical operations (NumPy, SciPy)
RUN apt-get update && apt-get install -y --no-install-recommends \
    curl \
    default-jre-headless \
    libpq5 \
    libhdf5-103-1 \
    libgomp1 \
    && rm -rf /var/lib/apt/lists/* \
    && apt-get purge -y --auto-remove

# -------------------------------------------------------------------------
# Python runtime environment variables
# -------------------------------------------------------------------------
# Prevent Python from writing .pyc bytecode files to disk (reduces image churn)
ENV PYTHONDONTWRITEBYTECODE=1
# Force unbuffered stdout/stderr so logs appear immediately in container output
ENV PYTHONUNBUFFERED=1
# Disable pip version check warnings in production
ENV PIP_DISABLE_PIP_VERSION_CHECK=1

# -------------------------------------------------------------------------
# Application environment variables
# -------------------------------------------------------------------------
# Flask application entry point module
ENV FLASK_APP=app.py
# Flask environment (overridable at runtime)
ENV FLASK_ENV=production
# Python module search path — allows imports from src/backend
ENV PYTHONPATH=/app/src/backend
# Directory for pre-trained GAN/VAE model artifacts
ENV MODEL_PATH=/app/models
# Default batch size for synthetic data generation (10K records per batch)
ENV BATCH_SIZE=10000
# Service port for the Generation Engine
ENV SERVICE_PORT=8001
# Gunicorn worker configuration
ENV GUNICORN_WORKERS=4
ENV GUNICORN_THREADS=2
ENV GUNICORN_TIMEOUT=300
# GPU configuration (disabled by default; enable via runtime env override)
ENV GPU_ENABLED=false
ENV CUDA_VISIBLE_DEVICES=""
# Generation engine specific settings
ENV QUALITY_THRESHOLD=0.95
ENV CHECKPOINT_INTERVAL=60
ENV PROGRESS_UPDATE_INTERVAL=5
ENV MAX_RETRIES=3

# -------------------------------------------------------------------------
# Copy virtual environment from builder stage
# -------------------------------------------------------------------------
COPY --from=builder /opt/venv /opt/venv
ENV PATH="/opt/venv/bin:$PATH"

# -------------------------------------------------------------------------
# Set working directory
# -------------------------------------------------------------------------
WORKDIR /app

# -------------------------------------------------------------------------
# Create non-root user for security compliance (SOC 2 Type II / CIS benchmark)
# Running as root inside containers is a security anti-pattern.
# -------------------------------------------------------------------------
RUN groupadd --system appgroup && \
    useradd --system --gid appgroup --shell /usr/sbin/nologin --no-create-home appuser

# -------------------------------------------------------------------------
# Create required directories with correct ownership
# -------------------------------------------------------------------------
# /app/models: Pre-trained GAN/VAE model storage (may be volume-mounted)
# /app/data: Temporary workspace for generation output buffering
# /app/checkpoints: Job checkpoint files for resume capability
# /app/logs: Application log files (structured JSON)
RUN mkdir -p /app/models /app/data /app/checkpoints /app/logs && \
    chown -R appuser:appgroup /app/models /app/data /app/checkpoints /app/logs

# -------------------------------------------------------------------------
# Copy application source code
# -------------------------------------------------------------------------
# Copy the shared utilities library (database, auth, logging, observability)
COPY src/backend/shared/ /app/src/backend/shared/

# Copy the Generation Engine service code
COPY src/backend/generation_engine/ /app/src/backend/generation_engine/

# -------------------------------------------------------------------------
# Set file ownership and permissions
# -------------------------------------------------------------------------
RUN chown -R appuser:appgroup /app && \
    chmod -R 755 /app

# -------------------------------------------------------------------------
# Switch to non-root user
# -------------------------------------------------------------------------
USER appuser

# -------------------------------------------------------------------------
# Expose the Generation Engine service port
# -------------------------------------------------------------------------
EXPOSE 8001

# -------------------------------------------------------------------------
# Health check — Kubernetes liveness probe fallback
# Checks the /health endpoint every 30 seconds with a 10-second timeout.
# Allows 3 retries before marking the container as unhealthy.
# Start period gives the service 40 seconds for model loading on cold start.
# -------------------------------------------------------------------------
HEALTHCHECK --interval=30s --timeout=10s --retries=3 --start-period=40s \
    CMD curl -f http://localhost:8001/health || exit 1

# -------------------------------------------------------------------------
# Gunicorn WSGI server entry point
# -------------------------------------------------------------------------
# --bind 0.0.0.0:8001         : Listen on all interfaces, port 8001
# --workers 4                  : 4 worker processes (configurable via env)
# --worker-class gthread       : Threaded workers for mixed I/O and CPU workloads
# --threads 2                  : 2 threads per worker (8 total concurrent requests)
# --timeout 300                : 5-minute timeout for long-running generation jobs
# --graceful-timeout 30        : 30s grace period for in-flight requests on shutdown
# --keep-alive 5               : Keep-alive timeout for persistent connections
# --max-requests 1000          : Recycle workers after 1000 requests (memory leak guard)
# --max-requests-jitter 50     : Random jitter to avoid thundering herd on recycle
# --access-logfile -           : Stream access logs to stdout for container log capture
# --error-logfile -            : Stream error logs to stderr for container log capture
# --log-level info             : Default log verbosity
# generation_engine.app:create_app() : Flask Application Factory entry point
# -------------------------------------------------------------------------
ENTRYPOINT ["gunicorn", \
    "--bind", "0.0.0.0:8001", \
    "--workers", "4", \
    "--worker-class", "gthread", \
    "--threads", "2", \
    "--timeout", "300", \
    "--graceful-timeout", "30", \
    "--keep-alive", "5", \
    "--max-requests", "1000", \
    "--max-requests-jitter", "50", \
    "--access-logfile", "-", \
    "--error-logfile", "-", \
    "--log-level", "info", \
    "generation_engine.app:create_app()"]
