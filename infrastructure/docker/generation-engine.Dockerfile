# ============================================================================
# Synthetic ERP Data Generation Platform — Generation Engine Dockerfile
# ============================================================================
# Production-optimized multi-stage build for the Generation Engine service.
# This is the core synthetic data generation component, orchestrating four
# generation methods: AI/ML (GAN via PyTorch, VAE via TensorFlow),
# rules-based, statistical synthesis (SciPy/NumPy), and intelligent masking.
# LangChain coordinates method selection and job orchestration.
#
# This is the LARGEST container image in the platform due to ML library sizes
# (PyTorch ~2GB, TensorFlow ~1.5GB). An optional GPU/CUDA build mode is
# provided for production environments with NVIDIA GPUs.
#
# Build context: Project root (.)
# Build command (CPU-only, default):
#   docker build -f infrastructure/docker/generation-engine.Dockerfile \
#     --build-arg BUILD_DATE=$(date -u +"%Y-%m-%dT%H:%M:%SZ") \
#     --build-arg VCS_REF=$(git rev-parse --short HEAD) \
#     -t synthetic-erp-generation-engine:latest .
#
# Build command (GPU/CUDA enabled):
#   docker build -f infrastructure/docker/generation-engine.Dockerfile \
#     --build-arg ENABLE_GPU=true \
#     --build-arg CUDA_VERSION=12.1 \
#     --build-arg BUILD_DATE=$(date -u +"%Y-%m-%dT%H:%M:%SZ") \
#     --build-arg VCS_REF=$(git rev-parse --short HEAD) \
#     -t synthetic-erp-generation-engine:gpu-latest .
#
# Run command (GPU mode requires NVIDIA Container Toolkit):
#   docker run --gpus all -p 5001:5001 synthetic-erp-generation-engine:gpu-latest
#
# For air-gapped environments (C-003):
#   docker build -f infrastructure/docker/generation-engine.Dockerfile \
#     --build-arg PIP_INDEX_URL=https://private-pypi.internal/simple \
#     --build-arg PIP_TRUSTED_HOST=private-pypi.internal \
#     --build-arg PYTORCH_INDEX_URL=https://private-pypi.internal/whl/cpu \
#     -t synthetic-erp-generation-engine:latest .
# ============================================================================

# ---------------------------------------------------------------------------
# Build arguments for image configuration, GPU support, and air-gapped builds
# ---------------------------------------------------------------------------

# Python base image version — pinned to 3.12 per platform requirements
ARG PYTHON_VERSION=3.12

# GPU/CUDA toggle: set to "true" to install CUDA-enabled PyTorch and full
# TensorFlow with GPU support. Default "false" installs lightweight CPU-only
# variants, significantly reducing image size and build time.
ARG ENABLE_GPU=false

# CUDA toolkit version used when ENABLE_GPU=true. Must match the CUDA version
# installed on the host GPU driver. PyTorch and TensorFlow pip wheels are
# built against specific CUDA versions.
ARG CUDA_VERSION=12.1

# Image metadata arguments for OCI labels
ARG BUILD_DATE
ARG VCS_REF

# Air-gapped support: override these to point to private PyPI and PyTorch
# wheel mirrors for environments without internet access (Constraint C-003).
ARG PIP_INDEX_URL=https://pypi.org/simple
ARG PIP_TRUSTED_HOST=pypi.org
ARG PYTORCH_INDEX_URL=""

# ============================================================================
# Stage 1: Builder
# Purpose: Install system-level build dependencies and heavy ML Python
#          packages (PyTorch, TensorFlow, LangChain, SciPy, NumPy, Pandas,
#          pyarrow) into an isolated virtual environment. This stage is
#          discarded in the final image to minimize attack surface and size.
# ============================================================================
FROM python:${PYTHON_VERSION}-slim AS builder

# Metadata for the builder stage (informational only)
LABEL stage=builder

# Re-declare ARGs after FROM so they are available in this stage.
# Docker resets ARGs across FROM boundaries.
ARG ENABLE_GPU
ARG CUDA_VERSION
ARG PIP_INDEX_URL
ARG PIP_TRUSTED_HOST
ARG PYTORCH_INDEX_URL

# Set working directory for the build context
WORKDIR /build

# Install system-level build dependencies required for compiling native
# Python extensions used by ML and scientific computing libraries:
#   - gcc, g++: C/C++ compilers for native extensions (torch, tensorflow,
#     scipy, numpy C extensions, pydantic-core Rust bridge)
#   - python3-dev: Python development headers for C extension compilation
#   - libffi-dev: Foreign function interface library for cffi/cryptography
#   - cmake: Occasionally required by PyTorch for compilation of custom ops
#     and by some scientific libraries for their build systems
#   - libgomp1: OpenMP runtime library providing parallel execution support
#     for PyTorch, NumPy, and SciPy multi-threaded operations
# These are only needed at build time and are NOT included in the runtime image.
RUN apt-get update && \
    apt-get install -y --no-install-recommends \
        gcc \
        g++ \
        python3-dev \
        libffi-dev \
        cmake \
        libgomp1 \
    && rm -rf /var/lib/apt/lists/*

# Create an isolated Python virtual environment at /opt/venv.
# Using a venv ensures clean separation of build artifacts from the system
# Python, enabling efficient multi-stage COPY of only the venv to runtime.
RUN python -m venv /opt/venv

# Prepend the virtual environment bin directory to PATH so all subsequent
# pip and python commands use the venv rather than the system Python.
ENV PATH="/opt/venv/bin:$PATH"

# Upgrade pip, setuptools, and wheel to latest versions to ensure
# compatibility with modern package metadata and wheel formats used by
# PyTorch, TensorFlow, and other ML libraries.
RUN pip install --no-cache-dir --upgrade pip setuptools wheel

# Copy only the requirements file first to leverage Docker layer caching.
# If requirements.txt hasn't changed, Docker reuses the cached pip install
# layer, significantly speeding up rebuilds during development.
# NOTE: requirements.txt should NOT include torch or tensorflow directly;
# those are installed conditionally based on ENABLE_GPU below.
COPY src/backend/generation_engine/requirements.txt /build/requirements.txt

# ---------------------------------------------------------------------------
# Conditional PyTorch Installation (CPU vs CUDA)
# ---------------------------------------------------------------------------
# PyTorch is installed separately from requirements.txt to allow switching
# between CPU-only (~200MB) and CUDA-enabled (~2GB) variants via the
# ENABLE_GPU build argument. This approach:
#   - Keeps requirements.txt portable and environment-agnostic
#   - Allows air-gapped builds with a private PyTorch wheel mirror
#   - Reduces default image size by ~1.8GB when GPU is not needed
#
# CPU mode (default): Uses the PyTorch CPU-only wheel index
# GPU mode: Uses CUDA 12.1 wheels (or the version specified by CUDA_VERSION)
RUN if [ "${ENABLE_GPU}" = "true" ]; then \
        echo "Installing PyTorch with CUDA ${CUDA_VERSION} support..."; \
        if [ -n "${PYTORCH_INDEX_URL}" ]; then \
            pip install --no-cache-dir torch \
                --index-url "${PYTORCH_INDEX_URL}"; \
        else \
            pip install --no-cache-dir torch \
                --index-url "https://download.pytorch.org/whl/cu${CUDA_VERSION//./}"; \
        fi; \
    else \
        echo "Installing PyTorch (CPU-only)..."; \
        if [ -n "${PYTORCH_INDEX_URL}" ]; then \
            pip install --no-cache-dir torch \
                --index-url "${PYTORCH_INDEX_URL}"; \
        else \
            pip install --no-cache-dir torch \
                --index-url https://download.pytorch.org/whl/cpu; \
        fi; \
    fi

# ---------------------------------------------------------------------------
# Conditional TensorFlow Installation (CPU vs GPU)
# ---------------------------------------------------------------------------
# TensorFlow installation follows the same conditional pattern as PyTorch:
#   - CPU mode: Install tensorflow-cpu (smaller, no CUDA dependency)
#   - GPU mode: Install full tensorflow (includes CUDA/cuDNN support)
#
# TensorFlow is used for VAE-based (Variational Autoencoder) synthetic data
# generation as an alternative to PyTorch GAN models.
RUN if [ "${ENABLE_GPU}" = "true" ]; then \
        echo "Installing TensorFlow with GPU support..."; \
        pip install --no-cache-dir \
            --index-url "${PIP_INDEX_URL}" \
            --trusted-host "${PIP_TRUSTED_HOST}" \
            tensorflow; \
    else \
        echo "Installing TensorFlow (CPU-only)..."; \
        pip install --no-cache-dir \
            --index-url "${PIP_INDEX_URL}" \
            --trusted-host "${PIP_TRUSTED_HOST}" \
            tensorflow-cpu; \
    fi

# ---------------------------------------------------------------------------
# Install remaining Python dependencies from requirements.txt
# ---------------------------------------------------------------------------
# With PyTorch and TensorFlow already installed, install the remaining
# dependencies. Key packages include:
#   - langchain 0.3.x, langchain-community 0.3.x: AI/ML orchestration for
#     method selection and job coordination across generation strategies
#   - scipy 1.12+: Statistical distribution modeling for synthesis
#   - numpy 1.26+: Numerical computing for statistical generation
#   - pandas 2.x: Data manipulation and transformation pipelines
#   - pyarrow 15.x: Apache Parquet columnar format output support
#   - flask 3.1.x: Lightweight REST API for job management endpoints
#   - gunicorn 21.x: Production WSGI HTTP server
#   - pymongo 4.x: MongoDB 7.0 driver for metadata persistence
#   - redis 5.x: Redis 7.x client for progress tracking and caching
#   - pydantic 2.x: Data validation and schema models
#   - structlog 24.x: Structured JSON logging
#   - opentelemetry-api, opentelemetry-sdk: Distributed tracing
#   - prometheus-client: Prometheus metrics exporter
#   - circuitbreaker: Circuit breaker pattern for resilience
RUN pip install --no-cache-dir \
        --index-url "${PIP_INDEX_URL}" \
        --trusted-host "${PIP_TRUSTED_HOST}" \
        -r /build/requirements.txt

# Verify that all installed packages have consistent dependency versions.
# This catches silent version conflicts between PyTorch, TensorFlow, and
# the scientific computing stack that could cause runtime failures.
# NOTE: Some ML packages may emit warnings about optional dependencies;
# the || true ensures the build continues while logging any issues.
RUN pip check || echo "WARNING: pip check reported dependency issues (may be optional ML deps)"

# ============================================================================
# Stage 2: Runtime
# Purpose: Minimal production image containing only the Python virtual
#          environment (with all ML and service dependencies) and the
#          application source code. No compilers, build tools, or
#          development headers are included, minimizing the attack surface
#          per security requirements (SOC 2 Type II, C-004).
# ============================================================================
FROM python:${PYTHON_VERSION}-slim AS runtime

# Re-declare metadata ARGs for labels in the runtime stage
ARG BUILD_DATE
ARG VCS_REF
ARG ENABLE_GPU

# ---------------------------------------------------------------------------
# OCI Image Specification Labels
# Provides standardized container metadata for registry discovery,
# vulnerability scanning tools, and deployment automation.
# ---------------------------------------------------------------------------
LABEL org.opencontainers.image.title="synthetic-erp-generation-engine" \
      org.opencontainers.image.description="Multi-method synthetic data generation engine with AI/ML, rules-based, and statistical generators" \
      org.opencontainers.image.source="infrastructure/docker/generation-engine.Dockerfile" \
      org.opencontainers.image.vendor="Synthetic ERP Platform" \
      org.opencontainers.image.created="${BUILD_DATE}" \
      org.opencontainers.image.revision="${VCS_REF}" \
      org.opencontainers.image.licenses="Proprietary" \
      org.opencontainers.image.gpu-enabled="${ENABLE_GPU}"

# Install minimal runtime dependencies only — no compilers or dev headers.
#   - libgomp1: OpenMP runtime library required at runtime by PyTorch and
#     NumPy for parallel multi-threaded operations during data generation.
#     Without libgomp1, torch.set_num_threads() and NumPy parallel ops fail.
#   - ca-certificates: Root CA bundle for HTTPS connections to inter-service
#     communication, MongoDB TLS, Redis TLS, and external API calls
#   - curl: Required for Docker/Kubernetes health check probes (HEALTHCHECK CMD)
# The apt cache is cleaned immediately to minimize the image layer size.
RUN apt-get update && \
    apt-get install -y --no-install-recommends \
        libgomp1 \
        ca-certificates \
        curl \
    && rm -rf /var/lib/apt/lists/*

# ---------------------------------------------------------------------------
# Environment Variables — Python Runtime Behavior
# ---------------------------------------------------------------------------
#   PYTHONDONTWRITEBYTECODE=1: Prevent .pyc file generation (cleaner container,
#     avoids permission issues with read-only filesystems)
#   PYTHONUNBUFFERED=1: Force stdout/stderr streams to be unbuffered for
#     real-time log output in Docker/Kubernetes log collectors
ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1

# ---------------------------------------------------------------------------
# Environment Variables — Flask Application Configuration
# ---------------------------------------------------------------------------
#   FLASK_APP: Application factory entry point for Flask CLI and Gunicorn
#   FLASK_ENV: Production mode (disables debug, reloader, interactive debugger)
#   PYTHONPATH: Ensures Python can locate generation_engine and shared packages
ENV FLASK_APP="generation_engine.app:create_app()" \
    FLASK_ENV=production \
    PYTHONPATH=/app

# ---------------------------------------------------------------------------
# Environment Variables — Service Identification and Monitoring
# ---------------------------------------------------------------------------
#   SERVICE_NAME: Used by structured logger for service identification in logs
#     and by OpenTelemetry for distributed trace span attribution
ENV SERVICE_NAME="generation-engine"

# ---------------------------------------------------------------------------
# Environment Variables — Generation Engine Configuration
# ---------------------------------------------------------------------------
#   GENERATION_BATCH_SIZE: Number of records per generation batch. Default
#     10,000 records per batch as specified in the tech spec. Configurable
#     at runtime via environment variable (valid range: 1,000 - 100,000).
#     Higher values improve throughput but increase memory usage per worker.
#   OMP_NUM_THREADS: OpenMP thread count for parallel NumPy and SciPy
#     operations during statistical synthesis. Adjust based on pod CPU limits.
#   TORCH_NUM_THREADS: PyTorch inter-op thread count for parallel tensor
#     operations during GAN/VAE model inference. Keep in sync with OMP threads.
ENV GENERATION_BATCH_SIZE=10000 \
    OMP_NUM_THREADS=4 \
    TORCH_NUM_THREADS=4

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
# Model Cache and Checkpoint Directories
# ---------------------------------------------------------------------------
# Create directories for ML model management and job resilience:
#   /app/model_cache: Pre-trained GAN (Generator/Discriminator) and VAE
#     (Encoder/Decoder) model weights. Models are loaded into memory at
#     startup and cached here for fast access across generation jobs.
#   /tmp/checkpoints: Redis-backed checkpoint files for long-running
#     generation jobs. Enables job resume after container restart or
#     failure, preventing loss of partially generated datasets.
# Both directories are created before chown to ensure correct ownership.
RUN mkdir -p /app/model_cache /tmp/checkpoints

# ---------------------------------------------------------------------------
# Copy Virtual Environment from Builder Stage
# ---------------------------------------------------------------------------
# Copy the complete virtual environment containing all installed Python
# packages (PyTorch, TensorFlow, LangChain, SciPy, NumPy, Pandas, etc.)
# from the builder stage. This is the key multi-stage optimization:
# the runtime image gets only the compiled packages without any build tools,
# compilers, or development headers.
COPY --from=builder /opt/venv /opt/venv

# Ensure the virtual environment's bin directory is first in PATH so
# python, gunicorn, and all entry-point scripts resolve to the venv.
ENV PATH="/opt/venv/bin:$PATH"

# ---------------------------------------------------------------------------
# Application Code
# ---------------------------------------------------------------------------
# Set the working directory where the application code will reside.
WORKDIR /app

# Copy the Generation Engine service code. The COPY context is the project
# root, so paths are relative to the repository root directory.
# The generation_engine package contains:
#   - app.py: Flask Application Factory for generation worker
#   - config.py: Model paths, batch sizes, GPU settings configuration
#   - generators/: Strategy pattern implementations
#       - base.py: Abstract base generator class
#       - ai_ml_generator.py: GAN/VAE-based synthetic data generation
#       - rules_generator.py: Business rules engine for constraint-based generation
#       - statistical_generator.py: Distribution-based synthesis (SciPy/NumPy)
#       - masking_generator.py: Intelligent data masking with privacy preservation
#   - orchestrator/: Job coordination
#       - job_orchestrator.py: LangChain-powered job orchestration
#       - batch_processor.py: Batch generation loop (10K records/batch)
#       - method_selector.py: Intelligent method selection per data column
#   - models/: ML model architectures
#       - gan_model.py: GAN architecture (Generator, Discriminator)
#       - vae_model.py: VAE architecture (Encoder, Decoder)
#       - model_registry.py: Model versioning, loading, and caching
#   - integrity/: Referential integrity enforcement
#       - relationship_manager.py: Foreign key tracking and enforcement
#       - dependency_graph.py: Table dependency ordering for generation
#   - formatters/: Output format handlers
#       - sql_formatter.py: SQL INSERT/COPY statement generation
#       - csv_formatter.py: CSV output with configurable delimiters
#       - json_formatter.py: JSON/JSONL output formatting
#       - parquet_formatter.py: Apache Parquet columnar format output
#   - utils/: Utility modules
#       - progress_tracker.py: Redis-backed progress reporting
#       - checkpoint.py: Checkpoint/resume support for long-running jobs
COPY src/backend/generation_engine/ /app/generation_engine/

# Copy the shared utilities library used by all backend services.
# Contains: database/ (MongoDB, Redis clients), auth/ (JWT, RBAC),
# logging/ (structured logger), observability/ (metrics, tracing),
# config/ (base config), middleware/ (circuit breaker, health check).
COPY src/backend/shared/ /app/shared/

# Set recursive ownership of all application files and working directories
# to the non-root user. This ensures appuser can:
#   - Read all source files for execution
#   - Write to /app/model_cache for model weight storage
#   - Write to /tmp/checkpoints for job checkpoint persistence
RUN chown -R appuser:appuser /app /tmp/checkpoints

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
# Expose port 5001 for the Flask/Gunicorn HTTP server.
# The Generation Engine listens on port 5001 (distinct from API Gateway's
# 5000) for internal service-to-service REST calls from the API Gateway.
# Kubernetes ClusterIP Service and Docker Compose port mappings reference
# this port. External traffic should NOT reach this port directly; all
# requests flow through the API Gateway.
EXPOSE 5001

# ---------------------------------------------------------------------------
# Health Check Configuration
# ---------------------------------------------------------------------------
# Docker HEALTHCHECK uses curl to probe the /health endpoint served by the
# Flask health check Blueprint. This endpoint returns HTTP 200 when the
# service is operational and all critical dependencies (MongoDB, Redis,
# ML models loaded) are reachable.
#
# Parameters:
#   --interval=30s: Check every 30 seconds
#   --timeout=10s: Fail if /health doesn't respond within 10 seconds
#     (slightly higher than API Gateway's 5s due to potential ML computation
#     during health checks that verify model availability)
#   --start-period=30s: Extended grace period after container start before
#     the first health check. ML libraries (PyTorch, TensorFlow) require
#     significant initialization time to:
#       - Load shared libraries (libcudnn, libtorch)
#       - Initialize CUDA context (GPU mode)
#       - Load pre-trained GAN/VAE model weights from /app/model_cache
#     30s accommodates cold start with large models.
#   --retries=3: Mark unhealthy after 3 consecutive failures
#
# In Kubernetes, the liveness/readiness probes in the Deployment manifest
# typically override this HEALTHCHECK, but it serves as a fallback for
# Docker Compose and standalone Docker deployments.
HEALTHCHECK --interval=30s --timeout=10s --start-period=30s --retries=3 \
    CMD curl -f http://localhost:5001/health || exit 1

# ---------------------------------------------------------------------------
# Container Entrypoint — Gunicorn WSGI Server
# ---------------------------------------------------------------------------
# Run the Flask application via Gunicorn, the production WSGI server.
#
# Gunicorn configuration:
#   --bind 0.0.0.0:5001   : Listen on all interfaces at port 5001
#   --workers 2            : Only 2 worker processes (NOT 4 like API Gateway)
#                            Each worker loads PyTorch/TensorFlow models into
#                            memory (~2-4GB per worker). With 2 workers, the
#                            container requires ~4-8GB RAM for ML models alone.
#                            Scale horizontally via Kubernetes HPA rather than
#                            increasing per-pod workers.
#   --threads 4            : 4 threads per worker (gthread worker class)
#                            Total concurrency: 2 workers × 4 threads = 8
#                            Threads handle I/O-bound operations (MongoDB
#                            writes, Redis progress updates, inter-service
#                            calls) while GIL-free NumPy/PyTorch C extensions
#                            handle CPU-bound generation in parallel.
#   --timeout 600          : 10-minute worker timeout (NOT 120s like API GW)
#                            Generation jobs process batches of 10,000 records
#                            and may involve multiple passes through GAN/VAE
#                            models. A single batch with complex referential
#                            integrity constraints can take several minutes.
#                            600s prevents premature worker kills during
#                            legitimate long-running generation operations.
#   --access-logfile -     : Stream access logs to stdout for Docker log driver
#   --error-logfile -      : Stream error logs to stderr for Docker log driver
#   generation_engine.app:create_app()
#                          : Application factory entry point. Gunicorn calls
#                            create_app() to obtain the Flask WSGI application
#                            instance with all route Blueprints, middleware,
#                            and ML model initialization registered.
#
# Note: --preload is intentionally NOT used (unlike API Gateway) because:
#   - ML model loading is memory-intensive and benefits from per-worker
#     isolated memory rather than shared memory via fork()
#   - TensorFlow and PyTorch may have issues with fork() after CUDA
#     context initialization in the master process
CMD ["gunicorn", \
     "--bind", "0.0.0.0:5001", \
     "--workers", "2", \
     "--threads", "4", \
     "--timeout", "600", \
     "--access-logfile", "-", \
     "--error-logfile", "-", \
     "generation_engine.app:create_app()"]
