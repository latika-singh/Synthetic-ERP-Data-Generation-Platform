"""Configuration module for the Generation Engine service.

This module extends the shared :class:`BaseConfig` with settings specific to
the Generation Engine microservice, following the 12-factor app methodology.
All configuration values are loaded from environment variables with sensible
defaults for local development.

Settings include:

- **Model paths** — Directory for persisted GAN/VAE model artifacts.
- **Batch processing** — Configurable batch size (default 10K records,
  valid range 1K–100K) for throughput tuning.
- **GPU acceleration** — CUDA device selection, memory fraction limits,
  and an enable/disable toggle.
- **LangChain integration** — Optional API key for LangChain-orchestrated
  generation workflows.
- **Timeouts & intervals** — Generation job timeout, checkpoint interval
  for long-running jobs, and progress-update frequency.
- **Worker concurrency** — Number of parallel generation workers.
- **Quality thresholds** — Minimum quality score (default ≥ 0.95) for the
  weighted fidelity model.
- **Resilience** — Retry counts, exponential back-off delay, circuit-
  breaker failure thresholds, and reset timeouts.

Environment-specific subclasses (``DevelopmentConfig``, ``TestingConfig``,
``ProductionConfig``) override defaults appropriate to each deployment
stage.  A ``config_map`` dictionary and ``get_config()`` factory function
simplify environment selection at service startup.

Usage::

    from generation_engine.config import get_config

    config = get_config()
    print(config.BATCH_SIZE)    # 10000 in production
    print(config.GPU_ENABLED)   # True / False based on env

No credentials, connection strings, or secrets are hard-coded anywhere in
this module.  Every sensitive value is read exclusively from the process
environment.
"""

from __future__ import annotations

import os
from typing import Any

from shared.config.base import BaseConfig, ConfigurationError


# ---------------------------------------------------------------------------
# Generation Engine base configuration
# ---------------------------------------------------------------------------


class GenerationEngineConfig(BaseConfig):
    """Base configuration for the Generation Engine service.

    Inherits common platform settings (MongoDB, Redis, Auth0, JWT,
    logging, observability) from :class:`BaseConfig` and adds generation-
    engine-specific attributes for model management, batch processing,
    GPU acceleration, quality scoring, and resilience.

    Class-level attributes document the canonical defaults.  The
    :meth:`__init__` method re-reads environment variables at instance
    creation time so that late-injected values (e.g. via Kubernetes
    ConfigMaps or Docker Compose ``environment:`` blocks) are honoured.

    Attributes:
        MODEL_PATH: Filesystem path where GAN/VAE model artifacts are
            stored and loaded from.
        BATCH_SIZE: Number of records generated per processing batch.
            Clamped to [``MIN_BATCH_SIZE``, ``MAX_BATCH_SIZE``].
        MAX_BATCH_SIZE: Upper bound for ``BATCH_SIZE`` (100 000).
        MIN_BATCH_SIZE: Lower bound for ``BATCH_SIZE`` (1 000).
        DEFAULT_BATCH_SIZE: Factory default when no environment variable
            is set (10 000).
        GPU_ENABLED: Whether GPU-accelerated generation is active.
        CUDA_DEVICE: CUDA device identifier (e.g. ``cuda:0``, ``cuda:1``).
        GPU_MEMORY_LIMIT: Maximum fraction of GPU memory the engine may
            consume (0.0–1.0).
        GENERATION_TIMEOUT: Maximum wall-clock seconds for a single
            generation job before it is forcibly terminated.
        CHECKPOINT_INTERVAL: Seconds between Redis-backed progress
            checkpoints for long-running jobs.
        WORKER_CONCURRENCY: Number of parallel generation worker threads
            or processes.
        QUALITY_THRESHOLD: Minimum acceptable weighted quality score
            (0.0–1.0).  The default of 0.95 corresponds to the ≥ 95%
            fidelity target.
        LANGCHAIN_API_KEY: Optional API key for LangChain cloud services.
        PROGRESS_UPDATE_INTERVAL: Seconds between Redis progress-
            percentage updates pushed to the Web Console.
        MAX_RETRIES: Maximum retry attempts for transient failures during
            generation.
        RETRY_DELAY: Base delay (seconds) for exponential back-off
            between retries (``delay = RETRY_DELAY * 2 ** attempt``).
        CIRCUIT_BREAKER_THRESHOLD: Number of consecutive failures before
            the circuit breaker opens.
        CIRCUIT_BREAKER_TIMEOUT: Seconds the circuit remains open before
            transitioning to half-open and re-testing the dependency.
    """

    # -- Batch processing constants -----------------------------------------
    # These are *not* configurable via environment variables; they define
    # the hard boundaries enforced by validate().

    MAX_BATCH_SIZE: int = 100000
    MIN_BATCH_SIZE: int = 1000
    DEFAULT_BATCH_SIZE: int = 10000

    # -- Class-level defaults (read at import time) -------------------------

    SERVICE_NAME: str = "generation-engine"

    MODEL_PATH: str = os.environ.get("MODEL_PATH", "/app/models")
    BATCH_SIZE: int = DEFAULT_BATCH_SIZE

    GPU_ENABLED: bool = False
    CUDA_DEVICE: str = "cuda:0"
    GPU_MEMORY_LIMIT: float = 0.8

    GENERATION_TIMEOUT: int = 3600
    CHECKPOINT_INTERVAL: int = 60
    WORKER_CONCURRENCY: int = 4

    QUALITY_THRESHOLD: float = 0.95

    LANGCHAIN_API_KEY: str = ""

    PROGRESS_UPDATE_INTERVAL: int = 5

    MAX_RETRIES: int = 3
    RETRY_DELAY: int = 2

    CIRCUIT_BREAKER_THRESHOLD: int = 5
    CIRCUIT_BREAKER_TIMEOUT: int = 60

    # -- Sensitive-key extension --------------------------------------------
    # LANGCHAIN_API_KEY must be redacted in to_safe_dict() output.

    _SENSITIVE_KEYS: frozenset[str] = BaseConfig._SENSITIVE_KEYS | frozenset(
        {"LANGCHAIN_API_KEY"}
    )

    # -- Production-required keys -------------------------------------------
    # Extend the base list with generation-engine-specific requirements.

    _PRODUCTION_REQUIRED_KEYS: list[str] = BaseConfig._PRODUCTION_REQUIRED_KEYS + [
        "MODEL_PATH",
    ]

    # -----------------------------------------------------------------------
    # Initialiser
    # -----------------------------------------------------------------------

    def __init__(self) -> None:
        """Initialise the Generation Engine configuration.

        Calls :meth:`BaseConfig.__init__` first to load common platform
        settings (``MONGODB_URI``, ``REDIS_URL``, ``SECRET_KEY``,
        ``SERVICE_NAME``, ``LOG_LEVEL``, etc.) and then reads all
        generation-engine-specific environment variables.

        ``BATCH_SIZE`` is clamped to [``MIN_BATCH_SIZE``,
        ``MAX_BATCH_SIZE``] after reading.  ``GPU_MEMORY_LIMIT`` is
        clamped to (0.0, 1.0].  ``QUALITY_THRESHOLD`` is clamped to
        [0.0, 1.0].
        """
        super().__init__()

        # Override service name for this microservice
        self.SERVICE_NAME = "generation-engine"

        # -- Model storage --------------------------------------------------
        self.MODEL_PATH: str = os.environ.get("MODEL_PATH", "/app/models")

        # -- Batch processing -----------------------------------------------
        raw_batch_size: int = self._get_int_env("BATCH_SIZE", self.DEFAULT_BATCH_SIZE)
        self.BATCH_SIZE: int = max(
            self.MIN_BATCH_SIZE, min(self.MAX_BATCH_SIZE, raw_batch_size)
        )

        # -- GPU acceleration -----------------------------------------------
        self.GPU_ENABLED: bool = self._get_bool_env("GPU_ENABLED", False)
        self.CUDA_DEVICE: str = os.environ.get("CUDA_DEVICE", "cuda:0")
        raw_gpu_memory: float = self._get_env(
            "GPU_MEMORY_LIMIT", 0.8, cast_type=float
        )
        self.GPU_MEMORY_LIMIT: float = max(0.1, min(1.0, raw_gpu_memory))

        # -- Timeouts and intervals -----------------------------------------
        self.GENERATION_TIMEOUT: int = self._get_int_env("GENERATION_TIMEOUT", 3600)
        self.CHECKPOINT_INTERVAL: int = self._get_int_env("CHECKPOINT_INTERVAL", 60)
        self.WORKER_CONCURRENCY: int = self._get_int_env("WORKER_CONCURRENCY", 4)

        # -- Quality scoring ------------------------------------------------
        raw_quality: float = self._get_env(
            "QUALITY_THRESHOLD", 0.95, cast_type=float
        )
        self.QUALITY_THRESHOLD: float = max(0.0, min(1.0, raw_quality))

        # -- LangChain integration ------------------------------------------
        self.LANGCHAIN_API_KEY: str = os.environ.get("LANGCHAIN_API_KEY", "")

        # -- Progress reporting ---------------------------------------------
        self.PROGRESS_UPDATE_INTERVAL: int = self._get_int_env(
            "PROGRESS_UPDATE_INTERVAL", 5
        )

        # -- Retry / resilience ---------------------------------------------
        self.MAX_RETRIES: int = self._get_int_env("MAX_RETRIES", 3)
        self.RETRY_DELAY: int = self._get_int_env("RETRY_DELAY", 2)
        self.CIRCUIT_BREAKER_THRESHOLD: int = self._get_int_env(
            "CIRCUIT_BREAKER_THRESHOLD", 5
        )
        self.CIRCUIT_BREAKER_TIMEOUT: int = self._get_int_env(
            "CIRCUIT_BREAKER_TIMEOUT", 60
        )

    # -----------------------------------------------------------------------
    # Validation
    # -----------------------------------------------------------------------

    def validate(self) -> None:
        """Validate all generation-engine-specific configuration values.

        Runs :meth:`BaseConfig.validate` first (which enforces production
        secrets), then applies domain-specific sanity checks:

        - ``BATCH_SIZE`` is re-clamped to the valid range.
        - ``GPU_MEMORY_LIMIT`` must be in (0.0, 1.0]; reset to 0.8 if
          out of range.
        - ``QUALITY_THRESHOLD`` must be in [0.0, 1.0]; reset to 0.95 if
          out of range.
        - ``WORKER_CONCURRENCY`` must be ≥ 1; reset to 4 if invalid.
        - All timeout and interval values must be ≥ 1; reset to their
          defaults if invalid.
        - Retry settings must be non-negative; reset to defaults if
          invalid.
        - Circuit-breaker settings must be ≥ 1; reset to defaults if
          invalid.

        In **production** mode, additionally validates that
        ``MODEL_PATH`` is a non-empty string.

        Raises:
            ConfigurationError: If production-required environment
                variables are missing (propagated from base validation),
                or if ``MODEL_PATH`` is empty in production.
        """
        # Delegate to BaseConfig for common production checks
        super().validate()

        # -- Batch size range enforcement -----------------------------------
        if self.BATCH_SIZE < self.MIN_BATCH_SIZE or self.BATCH_SIZE > self.MAX_BATCH_SIZE:
            self.BATCH_SIZE = max(
                self.MIN_BATCH_SIZE, min(self.MAX_BATCH_SIZE, self.BATCH_SIZE)
            )

        # -- GPU memory fraction sanity -------------------------------------
        if self.GPU_MEMORY_LIMIT <= 0.0 or self.GPU_MEMORY_LIMIT > 1.0:
            self.GPU_MEMORY_LIMIT = 0.8

        # -- Quality threshold sanity ---------------------------------------
        if self.QUALITY_THRESHOLD < 0.0 or self.QUALITY_THRESHOLD > 1.0:
            self.QUALITY_THRESHOLD = 0.95

        # -- Worker concurrency must be positive ----------------------------
        if self.WORKER_CONCURRENCY < 1:
            self.WORKER_CONCURRENCY = 4

        # -- Timeouts must be positive --------------------------------------
        if self.GENERATION_TIMEOUT < 1:
            self.GENERATION_TIMEOUT = 3600

        if self.CHECKPOINT_INTERVAL < 1:
            self.CHECKPOINT_INTERVAL = 60

        if self.PROGRESS_UPDATE_INTERVAL < 1:
            self.PROGRESS_UPDATE_INTERVAL = 5

        # -- Retry settings must be non-negative ----------------------------
        if self.MAX_RETRIES < 0:
            self.MAX_RETRIES = 3

        if self.RETRY_DELAY < 0:
            self.RETRY_DELAY = 2

        # -- Circuit breaker must be positive -------------------------------
        if self.CIRCUIT_BREAKER_THRESHOLD < 1:
            self.CIRCUIT_BREAKER_THRESHOLD = 5

        if self.CIRCUIT_BREAKER_TIMEOUT < 1:
            self.CIRCUIT_BREAKER_TIMEOUT = 60

        # -- Production-specific: MODEL_PATH must be set --------------------
        if self.FLASK_ENV == "production" and not self.MODEL_PATH:
            raise ConfigurationError(
                "Production environment requires MODEL_PATH to be set. "
                "Set the MODEL_PATH environment variable to the directory "
                "containing trained GAN/VAE model artifacts."
            )

    # -----------------------------------------------------------------------
    # Safe serialisation override
    # -----------------------------------------------------------------------

    def to_safe_dict(self) -> dict[str, Any]:
        """Return a dictionary of configuration values safe for logging.

        Extends :meth:`BaseConfig.to_safe_dict` to ensure the
        ``LANGCHAIN_API_KEY`` is redacted (handled via the extended
        ``_SENSITIVE_KEYS`` frozenset).

        Returns:
            A ``dict[str, Any]`` mapping configuration attribute names
            to their (possibly redacted) values.
        """
        return super().to_safe_dict()


# ---------------------------------------------------------------------------
# Environment-specific subclasses
# ---------------------------------------------------------------------------


class DevelopmentConfig(GenerationEngineConfig):
    """Development environment configuration for the Generation Engine.

    Enables debug mode, verbose ``DEBUG``-level logging, and uses a
    reduced batch size (1 000 records) for faster iteration during local
    development.  Auth0, encryption, and LangChain credentials are
    **not** required.
    """

    def __init__(self) -> None:
        """Initialise development-specific Generation Engine configuration."""
        super().__init__()
        self.FLASK_ENV = "development"
        self.DEBUG = True
        self.TESTING = False
        self.LOG_LEVEL = os.environ.get("LOG_LEVEL", "DEBUG")

        # Smaller batch size for faster local iteration
        raw_batch: int = self._get_int_env("BATCH_SIZE", 1000)
        self.BATCH_SIZE = max(self.MIN_BATCH_SIZE, min(self.MAX_BATCH_SIZE, raw_batch))

    def validate(self) -> None:
        """Relaxed validation for the development environment.

        In development mode, strict production checks (Auth0 credentials,
        encryption keys, model path) are skipped so that the engine can
        start locally without external infrastructure.
        """
        # No strict validation in development — allow quick startup


class TestingConfig(GenerationEngineConfig):
    """Testing environment configuration for the Generation Engine.

    Uses a **separate** MongoDB database (``test_synthetic_erp``) and a
    different Redis database index (``/1``) so that test runs never
    interfere with development data.

    The batch size is set to a tiny value (100 records) for fast test
    execution, and GPU acceleration is forcibly disabled to ensure
    tests run reliably on any machine (including CI runners without
    GPUs).
    """

    def __init__(self) -> None:
        """Initialise testing-specific Generation Engine configuration."""
        super().__init__()
        self.FLASK_ENV = "testing"
        self.DEBUG = True
        self.TESTING = True
        self.LOG_LEVEL = os.environ.get("LOG_LEVEL", "DEBUG")

        # Separate test databases to avoid polluting development data
        self.MONGODB_URI = os.environ.get(
            "MONGODB_URI", "mongodb://localhost:27017/test_synthetic_erp"
        )
        self.MONGODB_DATABASE = os.environ.get(
            "MONGODB_DATABASE", "test_synthetic_erp"
        )
        self.REDIS_URL = os.environ.get("REDIS_URL", "redis://localhost:6379/1")

        # Tiny batch size for fast test execution (below MIN_BATCH_SIZE on
        # purpose — tests need speed, not throughput)
        self.BATCH_SIZE = self._get_int_env("BATCH_SIZE", 100)

        # Forcibly disable GPU in tests for deterministic, portable runs
        self.GPU_ENABLED = False

        # Test-safe secrets
        self.SECRET_KEY = os.environ.get("FLASK_SECRET_KEY", "test-secret-key")
        self.JWT_SECRET_KEY = os.environ.get("JWT_SECRET_KEY", "test-jwt-secret")

    def validate(self) -> None:
        """Relaxed validation for the testing environment.

        All external-service credentials are optional during test
        execution; tests use mocks or local stubs for Auth0, encryption,
        cloud providers, and LangChain.
        """
        # No strict validation in testing — rely on test fixtures


class ProductionConfig(GenerationEngineConfig):
    """Production environment configuration for the Generation Engine.

    Disables debug mode, uses ``INFO``-level logging, and enforces the
    full default batch size (10 000 records).  The MongoDB connection
    pool is enlarged for higher concurrency.

    All security-related environment variables are **required** via the
    inherited :meth:`~GenerationEngineConfig.validate` method — the
    service will refuse to start if any are missing.
    """

    def __init__(self) -> None:
        """Initialise production-specific Generation Engine configuration."""
        super().__init__()
        self.FLASK_ENV = "production"
        self.DEBUG = False
        self.TESTING = False
        self.LOG_LEVEL = os.environ.get("LOG_LEVEL", "INFO")

        # Production batch size (overridable via env, clamped to valid range)
        raw_batch: int = self._get_int_env("BATCH_SIZE", 10000)
        self.BATCH_SIZE = max(self.MIN_BATCH_SIZE, min(self.MAX_BATCH_SIZE, raw_batch))

        # Larger connection pool for production traffic
        self.MONGODB_MAX_POOL_SIZE = self._get_int_env("MONGODB_MAX_POOL_SIZE", 200)

    # validate() is inherited from GenerationEngineConfig → BaseConfig.
    # It enforces:
    #   1. BaseConfig production required keys (secrets, Auth0, JWT)
    #   2. GenerationEngineConfig domain checks (batch range, GPU mem,
    #      quality threshold, MODEL_PATH)


# ---------------------------------------------------------------------------
# Config registry and factory
# ---------------------------------------------------------------------------

config_map: dict[str, type[GenerationEngineConfig]] = {
    "development": DevelopmentConfig,
    "testing": TestingConfig,
    "production": ProductionConfig,
}
"""Maps environment name strings to their configuration classes.

This dictionary is used by :func:`get_config` to resolve the
``FLASK_ENV`` environment variable into the appropriate configuration
class.  Third-party or test code may register additional environments
by inserting new entries before calling :func:`get_config`.
"""


def get_config() -> GenerationEngineConfig:
    """Create and validate the appropriate configuration for the current environment.

    Reads the ``FLASK_ENV`` environment variable and instantiates the
    matching configuration class from :data:`config_map`.  Falls back
    to :class:`DevelopmentConfig` when the variable is unset or contains
    an unrecognised value.

    The returned instance has already passed its
    :meth:`~GenerationEngineConfig.validate` check, so callers can use
    it immediately without additional verification.

    Returns:
        A fully validated :class:`GenerationEngineConfig` (or subclass)
        instance matching the current deployment environment.

    Raises:
        ConfigurationError: If production validation detects missing
            required environment variables or an empty ``MODEL_PATH``.

    Example::

        from generation_engine.config import get_config

        config = get_config()
        app.config.from_object(config)
        print(config.BATCH_SIZE)
    """
    env: str = os.environ.get("FLASK_ENV", "development").lower()
    config_class: type[GenerationEngineConfig] = config_map.get(env, DevelopmentConfig)
    config: GenerationEngineConfig = config_class()
    config.validate()
    return config
