"""Quality Service configuration module for the Synthetic ERP Data Generation Platform.

This module extends the shared :class:`~shared.config.base.BaseConfig` with
quality-service-specific settings including scoring weights, validation thresholds,
batch processing parameters, and inter-service communication URLs.

Configuration hierarchy::

    shared.config.base.BaseConfig            (platform-wide defaults)
    └── quality_service.config.BaseConfig    (adds quality scoring settings)
        ├── quality_service.config.DevelopmentConfig
        ├── quality_service.config.TestingConfig
        └── quality_service.config.ProductionConfig

All quality-specific settings are loaded from environment variables following
the 12-factor app methodology.  No credentials, URLs, or connection strings are
hard-coded.

Environment Variables (quality-specific)::

    QUALITY_STATISTICAL_WEIGHT          Weight for statistical fidelity (default 0.4)
    QUALITY_BUSINESS_RULES_WEIGHT       Weight for business rules compliance (default 0.3)
    QUALITY_REFERENTIAL_INTEGRITY_WEIGHT Weight for referential integrity (default 0.3)
    QUALITY_MIN_THRESHOLD               Minimum acceptable quality score (default 0.95)
    QUALITY_BATCH_SIZE                  Records per validation batch (default 10000)
    GREAT_EXPECTATIONS_DATA_DIR         Great Expectations project directory
    VALIDATION_TIMEOUT                  Max seconds per validation run (default 300)
    GENERATION_ENGINE_URL               URL of the Generation Engine service
    COMPLIANCE_SERVICE_URL              URL of the Compliance Service

Usage::

    from quality_service.config import config_map

    config_class = config_map.get("development", DevelopmentConfig)
    config = config_class()
    config.validate()
    print(config.to_safe_dict())
"""

from __future__ import annotations

import os

from shared.config.base import BaseConfig as SharedBaseConfig, ConfigurationError


# ---------------------------------------------------------------------------
# Quality Service BaseConfig
# ---------------------------------------------------------------------------


class BaseConfig(SharedBaseConfig):
    """Quality Service base configuration extending the platform-wide shared config.

    Inherits all shared configuration (MongoDB, Redis, Auth0, JWT, logging,
    observability) from :class:`~shared.config.base.BaseConfig` and adds
    quality-service-specific settings for the weighted scoring model, batch
    validation, Great Expectations integration, and inter-service communication.

    Quality Scoring Model::

        Q = W_stat * S_statistical + W_biz * S_business_rules + W_ri * S_referential_integrity

    Where the default weights are:

    - ``W_stat``  = 0.4  (40% statistical fidelity)
    - ``W_biz``   = 0.3  (30% business rules compliance)
    - ``W_ri``    = 0.3  (30% referential integrity)

    The composite score ``Q`` lies in the [0, 1] range.  The minimum acceptable
    threshold defaults to 0.95 (95%).

    Attributes:
        QUALITY_STATISTICAL_WEIGHT: Scoring weight for statistical fidelity (0.0-1.0).
        QUALITY_BUSINESS_RULES_WEIGHT: Scoring weight for business rules (0.0-1.0).
        QUALITY_REFERENTIAL_INTEGRITY_WEIGHT: Scoring weight for referential
            integrity (0.0-1.0).
        QUALITY_MIN_THRESHOLD: Minimum composite quality score to pass
            validation (0.0-1.0).
        QUALITY_BATCH_SIZE: Number of records processed per validation batch.
        GREAT_EXPECTATIONS_DATA_DIR: File-system path to the Great Expectations
            project directory.
        VALIDATION_TIMEOUT: Maximum wall-clock seconds for a single validation run.
        GENERATION_ENGINE_URL: HTTP base URL of the Generation Engine service.
        COMPLIANCE_SERVICE_URL: HTTP base URL of the Compliance Service.
    """

    # -- Quality scoring weights -----------------------------------------------
    # Class-level defaults; re-read in __init__ for late-binding support.

    QUALITY_STATISTICAL_WEIGHT: float = float(os.environ.get("QUALITY_STATISTICAL_WEIGHT", "0.4"))
    QUALITY_BUSINESS_RULES_WEIGHT: float = float(os.environ.get("QUALITY_BUSINESS_RULES_WEIGHT", "0.3"))
    QUALITY_REFERENTIAL_INTEGRITY_WEIGHT: float = float(os.environ.get("QUALITY_REFERENTIAL_INTEGRITY_WEIGHT", "0.3"))
    QUALITY_MIN_THRESHOLD: float = float(os.environ.get("QUALITY_MIN_THRESHOLD", "0.95"))

    # -- Batch processing & timeout --------------------------------------------

    QUALITY_BATCH_SIZE: int = int(os.environ.get("QUALITY_BATCH_SIZE", "10000"))
    VALIDATION_TIMEOUT: int = int(os.environ.get("VALIDATION_TIMEOUT", "300"))

    # -- Great Expectations integration ----------------------------------------

    GREAT_EXPECTATIONS_DATA_DIR: str = os.environ.get(
        "GREAT_EXPECTATIONS_DATA_DIR",
        os.path.join(os.path.expanduser("~"), ".great_expectations"),
    )

    # -- Inter-service communication -------------------------------------------

    GENERATION_ENGINE_URL: str = os.environ.get("GENERATION_ENGINE_URL", "http://localhost:5001")
    COMPLIANCE_SERVICE_URL: str = os.environ.get("COMPLIANCE_SERVICE_URL", "http://localhost:5004")

    # -- Internal constants ----------------------------------------------------

    _QUALITY_WEIGHT_TOLERANCE: float = 1e-6
    """Floating-point tolerance for weight-sum validation."""

    # -----------------------------------------------------------------------
    # Initialiser
    # -----------------------------------------------------------------------

    def __init__(self) -> None:
        """Initialise Quality Service base configuration.

        Calls the shared ``BaseConfig`` initialiser first to load platform-wide
        settings (MongoDB, Redis, Auth0, JWT, logging, observability), then
        re-reads all quality-specific environment variables so that values
        injected after module import time are captured correctly.
        """
        super().__init__()

        # Override service name default for the Quality Service
        self.SERVICE_NAME = os.environ.get("SERVICE_NAME", "quality-service")

        # -- Quality scoring weights (re-read for late-binding) ----------------
        self.QUALITY_STATISTICAL_WEIGHT = self._get_float_env("QUALITY_STATISTICAL_WEIGHT", 0.4)
        self.QUALITY_BUSINESS_RULES_WEIGHT = self._get_float_env("QUALITY_BUSINESS_RULES_WEIGHT", 0.3)
        self.QUALITY_REFERENTIAL_INTEGRITY_WEIGHT = self._get_float_env("QUALITY_REFERENTIAL_INTEGRITY_WEIGHT", 0.3)
        self.QUALITY_MIN_THRESHOLD = self._get_float_env("QUALITY_MIN_THRESHOLD", 0.95)

        # -- Batch processing & timeout ----------------------------------------
        self.QUALITY_BATCH_SIZE = self._get_int_env("QUALITY_BATCH_SIZE", 10000)
        self.VALIDATION_TIMEOUT = self._get_int_env("VALIDATION_TIMEOUT", 300)

        # -- Great Expectations integration ------------------------------------
        self.GREAT_EXPECTATIONS_DATA_DIR = os.environ.get(
            "GREAT_EXPECTATIONS_DATA_DIR",
            os.path.join(os.path.expanduser("~"), ".great_expectations"),
        )

        # -- Inter-service communication ---------------------------------------
        self.GENERATION_ENGINE_URL = os.environ.get("GENERATION_ENGINE_URL", "http://localhost:5001")
        self.COMPLIANCE_SERVICE_URL = os.environ.get("COMPLIANCE_SERVICE_URL", "http://localhost:5004")

    # -----------------------------------------------------------------------
    # Helper: float environment variable loader
    # -----------------------------------------------------------------------

    @staticmethod
    def _get_float_env(key: str, default: float = 0.0) -> float:
        """Load a float value from an environment variable.

        Mirrors :meth:`~shared.config.base.BaseConfig._get_int_env` for
        float values.  Returns *default* when the variable is absent or
        cannot be parsed.

        Args:
            key: The environment variable name.
            default: Fallback when the variable is not set or is invalid.

        Returns:
            The parsed float value, or *default*.
        """
        raw_value: str | None = os.environ.get(key)
        if raw_value is None:
            return default
        try:
            return float(raw_value)
        except (ValueError, TypeError):
            return default

    # -----------------------------------------------------------------------
    # Validation
    # -----------------------------------------------------------------------

    def validate(self) -> None:
        """Validate quality service configuration.

        Extends the shared :meth:`~shared.config.base.BaseConfig.validate`
        with quality-specific invariant checks that run in **all** environments
        (development, testing, and production):

        1. Each scoring weight must be in the ``[0.0, 1.0]`` range.
        2. The three scoring weights must sum to ``1.0`` (within
           floating-point tolerance).
        3. The minimum quality threshold must be in ``[0.0, 1.0]``.
        4. The batch size must be a positive integer.
        5. The validation timeout must be a positive integer.

        Raises:
            ConfigurationError: If any shared or quality-specific invariant
                is violated.
        """
        # Run shared platform-wide validation (production env var enforcement,
        # Auth0, JWT, encryption key checks when FLASK_ENV == "production").
        super().validate()

        # Quality-specific invariant checks (always executed).
        self._validate_quality_settings()

    def _validate_quality_settings(self) -> None:
        """Internal helper that validates all quality-specific configuration.

        Separated from :meth:`validate` so that subclasses can invoke
        quality checks independently of the shared platform validation
        when needed.

        Raises:
            ConfigurationError: If any quality-specific invariant is
                violated.
        """
        # -- Weight range checks -----------------------------------------------
        weights: dict[str, float] = {
            "QUALITY_STATISTICAL_WEIGHT": self.QUALITY_STATISTICAL_WEIGHT,
            "QUALITY_BUSINESS_RULES_WEIGHT": self.QUALITY_BUSINESS_RULES_WEIGHT,
            "QUALITY_REFERENTIAL_INTEGRITY_WEIGHT": self.QUALITY_REFERENTIAL_INTEGRITY_WEIGHT,
        }
        for name, value in weights.items():
            if not (0.0 <= value <= 1.0):
                raise ConfigurationError(f"{name} must be between 0.0 and 1.0, got {value}.")

        # -- Weight sum check --------------------------------------------------
        weight_sum: float = sum(weights.values())
        if abs(weight_sum - 1.0) > self._QUALITY_WEIGHT_TOLERANCE:
            raise ConfigurationError(
                f"Quality scoring weights must sum to 1.0, got {weight_sum:.6f} "
                f"(statistical={self.QUALITY_STATISTICAL_WEIGHT}, "
                f"business_rules={self.QUALITY_BUSINESS_RULES_WEIGHT}, "
                f"referential_integrity={self.QUALITY_REFERENTIAL_INTEGRITY_WEIGHT})."
            )

        # -- Threshold range check ---------------------------------------------
        if not (0.0 <= self.QUALITY_MIN_THRESHOLD <= 1.0):
            raise ConfigurationError(
                f"QUALITY_MIN_THRESHOLD must be between 0.0 and 1.0, got {self.QUALITY_MIN_THRESHOLD}."
            )

        # -- Batch size check --------------------------------------------------
        if self.QUALITY_BATCH_SIZE <= 0:
            raise ConfigurationError(f"QUALITY_BATCH_SIZE must be a positive integer, got {self.QUALITY_BATCH_SIZE}.")

        # -- Timeout check -----------------------------------------------------
        if self.VALIDATION_TIMEOUT <= 0:
            raise ConfigurationError(f"VALIDATION_TIMEOUT must be a positive integer, got {self.VALIDATION_TIMEOUT}.")


# ---------------------------------------------------------------------------
# Environment-specific subclasses
# ---------------------------------------------------------------------------


class DevelopmentConfig(BaseConfig):
    """Development environment configuration for the Quality Service.

    Enables debug mode with verbose ``DEBUG``-level logging.  No external
    service dependencies (Auth0, encryption, compliance service) are required,
    allowing the quality service to run locally with minimal setup.

    Quality-specific invariants (weight sums, value ranges) are still
    validated to prevent silent configuration errors during development.
    """

    def __init__(self) -> None:
        """Initialise development-specific configuration."""
        super().__init__()
        self.FLASK_ENV = "development"
        self.DEBUG = True
        self.TESTING = False
        self.LOG_LEVEL = os.environ.get("LOG_LEVEL", "DEBUG")

    def validate(self) -> None:
        """Relaxed validation for the development environment.

        External service credentials (Auth0, encryption keys) are **not**
        required.  Quality-specific invariants (weight sums, value ranges)
        are still enforced so that misconfigured weights are caught early.
        """
        # The inherited validate() calls SharedBaseConfig.validate() which
        # skips strict production checks when FLASK_ENV != "production",
        # then runs _validate_quality_settings() unconditionally.
        super().validate()


class TestingConfig(BaseConfig):
    """Testing environment configuration for the Quality Service.

    Uses a **separate** MongoDB database (``synthetic_erp_test``) so that
    automated test runs never interfere with development data.  Logging is
    set to ``WARNING`` to reduce noise during test suites.

    Attributes:
        DEBUG: Disabled for testing to match production behaviour.
        TESTING: Enabled to signal Flask test mode.
        MONGODB_URI: Points to the ``synthetic_erp_test`` database.
        LOG_LEVEL: ``WARNING`` — only warnings and errors are logged.
    """

    def __init__(self) -> None:
        """Initialise testing-specific configuration."""
        super().__init__()
        self.FLASK_ENV = "testing"
        self.DEBUG = False
        self.TESTING = True
        self.LOG_LEVEL = os.environ.get("LOG_LEVEL", "WARNING")
        self.MONGODB_URI = os.environ.get("MONGODB_URI", "mongodb://localhost:27017/synthetic_erp_test")
        self.MONGODB_DATABASE = os.environ.get("MONGODB_DATABASE", "synthetic_erp_test")

    def validate(self) -> None:
        """Relaxed validation for the testing environment.

        External credentials are optional — tests use mocks or local stubs
        for Auth0, encryption, and cloud providers.  Quality-specific
        invariants are still validated.
        """
        super().validate()


class ProductionConfig(BaseConfig):
    """Production environment configuration for the Quality Service.

    Disables debug mode, sets the log level to ``INFO``, and enforces
    **strict** validation: the service will refuse to start if any required
    environment variable is missing.

    In addition to the shared production requirements (``FLASK_SECRET_KEY``,
    ``AUTH0_DOMAIN``, ``AUTH0_CLIENT_ID``, ``JWT_SECRET_KEY``,
    ``ENCRYPTION_KEY``), the Quality Service also requires explicit values
    for ``MONGODB_URI`` and ``REDIS_URL`` — development defaults are not
    acceptable in production.

    Attributes:
        DEBUG: Always ``False`` in production.
        TESTING: Always ``False`` in production.
        LOG_LEVEL: ``INFO`` — balanced verbosity for operational monitoring.
    """

    _QUALITY_PRODUCTION_REQUIRED_ENV_VARS: list[str] = [
        "FLASK_SECRET_KEY",
        "MONGODB_URI",
        "REDIS_URL",
    ]
    """Environment variables that must be explicitly set in production.

    ``FLASK_SECRET_KEY`` is also checked by the shared base validation, but
    is included here for completeness and clear error messaging specific to
    the Quality Service.
    """

    def __init__(self) -> None:
        """Initialise production-specific configuration."""
        super().__init__()
        self.FLASK_ENV = "production"
        self.DEBUG = False
        self.TESTING = False
        self.LOG_LEVEL = os.environ.get("LOG_LEVEL", "INFO")

    def validate(self) -> None:
        """Strict validation for the production environment.

        Runs the full validation chain:

        1. Shared platform-wide production checks (Auth0, JWT, encryption).
        2. Quality-specific invariants (weight sums, value ranges).
        3. Quality Service production requirements (``MONGODB_URI``,
           ``REDIS_URL``, ``FLASK_SECRET_KEY`` must be explicitly set).

        Raises:
            ConfigurationError: If any required variable is missing or
                any quality invariant is violated.
        """
        # Run shared production validation + quality weight checks
        super().validate()

        # Enforce quality-service-specific required environment variables
        missing: list[str] = []
        for env_key in self._QUALITY_PRODUCTION_REQUIRED_ENV_VARS:
            if not os.environ.get(env_key):
                missing.append(env_key)

        if missing:
            raise ConfigurationError(
                f"Quality Service production environment is missing required "
                f"configuration variable(s): {', '.join(missing)}. "
                f"Set them as environment variables before starting the service."
            )


# ---------------------------------------------------------------------------
# Configuration registry
# ---------------------------------------------------------------------------

config_map: dict[str, type[BaseConfig]] = {
    "development": DevelopmentConfig,
    "testing": TestingConfig,
    "production": ProductionConfig,
}
"""Maps environment name strings to Quality Service configuration classes.

Provides a lookup table for selecting the appropriate configuration class
based on the ``FLASK_ENV`` environment variable.  Falls back to
:class:`DevelopmentConfig` when the environment name is unrecognised.

Usage::

    import os
    from quality_service.config import config_map, DevelopmentConfig

    env = os.environ.get("FLASK_ENV", "development")
    config_class = config_map.get(env, DevelopmentConfig)
    config = config_class()
    config.validate()
    # config.to_safe_dict() returns a log-safe representation
"""
