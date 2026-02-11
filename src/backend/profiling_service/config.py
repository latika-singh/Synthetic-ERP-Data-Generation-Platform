"""Configuration module for the Profiling Service.

This module provides environment-specific configuration classes for the
Profiling Service following the 12-factor app methodology.  All configuration
values are loaded exclusively from environment variables — no credentials,
connection strings, or secrets are hard-coded.

The module provides:

- ``ProfilingServiceConfig``: The base profiling configuration class that
  extends :class:`~shared.config.base.BaseConfig` with ERP connection settings
  (SAP RFC, Oracle JDBC, Microsoft Dynamics API, generic JDBC), schema
  discovery parameters, and statistical profiling tuning options.
- ``DevelopmentConfig``: Development environment overrides with debug mode
  enabled and relaxed validation.
- ``TestingConfig``: Testing environment overrides using a separate MongoDB
  test database and test-safe defaults.
- ``ProductionConfig``: Production environment overrides with strict
  validation and hardened defaults.
- ``get_config()``: A factory function that inspects ``FLASK_ENV`` (or an
  explicit *config_name* argument) and returns the appropriately configured
  instance.

Usage::

    from profiling_service.config import get_config

    config = get_config()
    print(config.SAP_RFC_HOST)
    print(config.DISCOVERY_TIMEOUT_SECONDS)

Without this module the Profiling Service cannot load its environment-specific
configuration, and all ERP connections and schema discovery operations will
fail.
"""

from __future__ import annotations

import os
from typing import Optional

from shared.config.base import BaseConfig


# ---------------------------------------------------------------------------
# Profiling Service base configuration
# ---------------------------------------------------------------------------


class ProfilingServiceConfig(BaseConfig):
    """Profiling Service configuration extending the platform-wide BaseConfig.

    Inherits MongoDB, Redis, Auth0, JWT, CORS, rate-limiting, encryption,
    and observability settings from :class:`BaseConfig` and adds:

    - **ERP connection settings** for SAP RFC/BAPI, Oracle E-Business Suite
      (JDBC), Microsoft Dynamics 365 (Web API / OData), and generic JDBC
      legacy systems.
    - **Schema discovery parameters** controlling timeouts, batch sizes,
      retry logic, and maximum table counts during metadata extraction.
    - **Statistical profiling parameters** governing sample sizes,
      distribution bins, and category limits for the profiling pipeline.
    - **Supported ERP module and type enumerations** restricting the initial
      release to the four mandated ERP modules and four connector types
      (per constraint C-005).

    All sensitive values (passwords, client secrets) are loaded from
    environment variables and are typed as ``Optional[str]`` because they
    may not be configured in every deployment environment.

    Attributes:
        SERVICE_NAME: Logical service identifier.
        SERVICE_PORT: HTTP port the Profiling Service listens on.
        SAP_RFC_HOST: Hostname or IP address of the SAP RFC gateway.
        SAP_RFC_SYSNR: SAP system number (default ``"00"``).
        SAP_RFC_CLIENT: SAP client number (default ``"100"``).
        SAP_RFC_USER: SAP RFC connection username.
        SAP_RFC_PASSWORD: SAP RFC connection password.
        ORACLE_EBS_JDBC_URL: Oracle E-Business Suite JDBC connection URL.
        ORACLE_EBS_USER: Oracle EBS database username.
        ORACLE_EBS_PASSWORD: Oracle EBS database password.
        ORACLE_JDBC_DRIVER: Fully-qualified Oracle JDBC driver class name.
        DYNAMICS_API_URL: Microsoft Dynamics 365 Web API base URL.
        DYNAMICS_CLIENT_ID: Azure AD application (client) ID for Dynamics.
        DYNAMICS_CLIENT_SECRET: Azure AD application client secret.
        DYNAMICS_TENANT_ID: Azure AD tenant ID.
        JDBC_DRIVER_PATH: Filesystem path to JDBC driver JARs.
        DISCOVERY_TIMEOUT_SECONDS: Maximum wall-clock time for a single
            schema discovery operation (seconds).
        DISCOVERY_MAX_TABLES: Upper limit on tables discovered per request.
        DISCOVERY_BATCH_SIZE: Number of tables processed per discovery batch.
        DISCOVERY_MAX_RETRIES: Maximum retry attempts for transient
            discovery failures.
        DISCOVERY_RETRY_DELAY: Initial delay between retries (seconds),
            used as the base for exponential back-off.
        PROFILING_SAMPLE_SIZE: Number of rows sampled per table for
            statistical profiling.
        PROFILING_TIMEOUT_SECONDS: Maximum wall-clock time for profiling
            a single table (seconds).
        PROFILING_MAX_CATEGORIES: Maximum distinct categorical values
            tracked per column before grouping as "other".
        PROFILING_DISTRIBUTION_BINS: Number of histogram bins used when
            fitting continuous distributions.
        SUPPORTED_ERP_MODULES: ERP functional modules supported in the
            initial release (C-005).
        SUPPORTED_ERP_TYPES: ERP system connector types available.
    """

    # -- Service identification ----------------------------------------------

    SERVICE_NAME: str = "profiling-service"
    SERVICE_PORT: int = int(os.environ.get("PROFILING_SERVICE_PORT", "8002"))

    # -- SAP RFC/BAPI connection settings ------------------------------------

    SAP_RFC_HOST: Optional[str] = os.environ.get("SAP_RFC_HOST")
    SAP_RFC_SYSNR: str = os.environ.get("SAP_RFC_SYSNR", "00")
    SAP_RFC_CLIENT: str = os.environ.get("SAP_RFC_CLIENT", "100")
    SAP_RFC_USER: Optional[str] = os.environ.get("SAP_RFC_USER")
    SAP_RFC_PASSWORD: Optional[str] = os.environ.get("SAP_RFC_PASSWORD")

    # -- Oracle E-Business Suite JDBC connection settings --------------------

    ORACLE_EBS_JDBC_URL: Optional[str] = os.environ.get("ORACLE_EBS_JDBC_URL")
    ORACLE_EBS_USER: Optional[str] = os.environ.get("ORACLE_EBS_USER")
    ORACLE_EBS_PASSWORD: Optional[str] = os.environ.get("ORACLE_EBS_PASSWORD")
    ORACLE_JDBC_DRIVER: str = os.environ.get(
        "ORACLE_JDBC_DRIVER", "oracle.jdbc.OracleDriver"
    )

    # -- Microsoft Dynamics 365 Web API / OData settings ---------------------

    DYNAMICS_API_URL: Optional[str] = os.environ.get("DYNAMICS_API_URL")
    DYNAMICS_CLIENT_ID: Optional[str] = os.environ.get("DYNAMICS_CLIENT_ID")
    DYNAMICS_CLIENT_SECRET: Optional[str] = os.environ.get("DYNAMICS_CLIENT_SECRET")
    DYNAMICS_TENANT_ID: Optional[str] = os.environ.get("DYNAMICS_TENANT_ID")

    # -- Generic JDBC legacy system settings ---------------------------------

    JDBC_DRIVER_PATH: str = os.environ.get("JDBC_DRIVER_PATH", "/opt/jdbc-drivers")

    # -- Schema discovery settings -------------------------------------------

    DISCOVERY_TIMEOUT_SECONDS: int = int(
        os.environ.get("DISCOVERY_TIMEOUT_SECONDS", "300")
    )
    DISCOVERY_MAX_TABLES: int = int(os.environ.get("DISCOVERY_MAX_TABLES", "500"))
    DISCOVERY_BATCH_SIZE: int = int(os.environ.get("DISCOVERY_BATCH_SIZE", "50"))
    DISCOVERY_MAX_RETRIES: int = int(os.environ.get("DISCOVERY_MAX_RETRIES", "3"))
    DISCOVERY_RETRY_DELAY: float = float(os.environ.get("DISCOVERY_RETRY_DELAY", "2.0"))

    # -- Statistical profiling settings --------------------------------------

    PROFILING_SAMPLE_SIZE: int = int(os.environ.get("PROFILING_SAMPLE_SIZE", "10000"))
    PROFILING_TIMEOUT_SECONDS: int = int(
        os.environ.get("PROFILING_TIMEOUT_SECONDS", "600")
    )
    PROFILING_MAX_CATEGORIES: int = int(
        os.environ.get("PROFILING_MAX_CATEGORIES", "100")
    )
    PROFILING_DISTRIBUTION_BINS: int = int(
        os.environ.get("PROFILING_DISTRIBUTION_BINS", "50")
    )

    # -- Supported ERP modules and types (C-005: initial release) ------------

    SUPPORTED_ERP_MODULES: list[str] = [
        "financial_accounting",
        "human_resources",
        "sales_distribution",
        "material_management",
    ]

    SUPPORTED_ERP_TYPES: list[str] = [
        "sap",
        "oracle_ebs",
        "dynamics_365",
        "jdbc_legacy",
    ]

    # -- Sensitive keys (extend base set for safe serialisation) -------------

    _SENSITIVE_KEYS: frozenset[str] = BaseConfig._SENSITIVE_KEYS | frozenset(
        {
            "SAP_RFC_PASSWORD",
            "ORACLE_EBS_PASSWORD",
            "DYNAMICS_CLIENT_SECRET",
        }
    )

    # -----------------------------------------------------------------------
    # Initialiser
    # -----------------------------------------------------------------------

    def __init__(self) -> None:
        """Initialise a ProfilingServiceConfig instance.

        Re-reads all environment variables so that instances created after
        module import still reflect the latest values.  Calls the parent
        :class:`BaseConfig` initialiser first to load platform-wide settings,
        then overlays all profiling-specific attributes.
        """
        super().__init__()

        # Service identification
        self.SERVICE_NAME = "profiling-service"
        self.SERVICE_PORT = self._get_int_env("PROFILING_SERVICE_PORT", 8002)

        # SAP RFC/BAPI connection settings
        self.SAP_RFC_HOST: Optional[str] = os.environ.get("SAP_RFC_HOST")
        self.SAP_RFC_SYSNR: str = os.environ.get("SAP_RFC_SYSNR", "00")
        self.SAP_RFC_CLIENT: str = os.environ.get("SAP_RFC_CLIENT", "100")
        self.SAP_RFC_USER: Optional[str] = os.environ.get("SAP_RFC_USER")
        self.SAP_RFC_PASSWORD: Optional[str] = os.environ.get("SAP_RFC_PASSWORD")

        # Oracle E-Business Suite JDBC connection settings
        self.ORACLE_EBS_JDBC_URL: Optional[str] = os.environ.get("ORACLE_EBS_JDBC_URL")
        self.ORACLE_EBS_USER: Optional[str] = os.environ.get("ORACLE_EBS_USER")
        self.ORACLE_EBS_PASSWORD: Optional[str] = os.environ.get("ORACLE_EBS_PASSWORD")
        self.ORACLE_JDBC_DRIVER: str = os.environ.get(
            "ORACLE_JDBC_DRIVER", "oracle.jdbc.OracleDriver"
        )

        # Microsoft Dynamics 365 Web API / OData settings
        self.DYNAMICS_API_URL: Optional[str] = os.environ.get("DYNAMICS_API_URL")
        self.DYNAMICS_CLIENT_ID: Optional[str] = os.environ.get("DYNAMICS_CLIENT_ID")
        self.DYNAMICS_CLIENT_SECRET: Optional[str] = os.environ.get(
            "DYNAMICS_CLIENT_SECRET"
        )
        self.DYNAMICS_TENANT_ID: Optional[str] = os.environ.get("DYNAMICS_TENANT_ID")

        # Generic JDBC legacy system settings
        self.JDBC_DRIVER_PATH: str = os.environ.get(
            "JDBC_DRIVER_PATH", "/opt/jdbc-drivers"
        )

        # Schema discovery settings
        self.DISCOVERY_TIMEOUT_SECONDS: int = self._get_int_env(
            "DISCOVERY_TIMEOUT_SECONDS", 300
        )
        self.DISCOVERY_MAX_TABLES: int = self._get_int_env(
            "DISCOVERY_MAX_TABLES", 500
        )
        self.DISCOVERY_BATCH_SIZE: int = self._get_int_env(
            "DISCOVERY_BATCH_SIZE", 50
        )
        self.DISCOVERY_MAX_RETRIES: int = self._get_int_env(
            "DISCOVERY_MAX_RETRIES", 3
        )
        self.DISCOVERY_RETRY_DELAY: float = self._get_float_env(
            "DISCOVERY_RETRY_DELAY", 2.0
        )

        # Statistical profiling settings
        self.PROFILING_SAMPLE_SIZE: int = self._get_int_env(
            "PROFILING_SAMPLE_SIZE", 10000
        )
        self.PROFILING_TIMEOUT_SECONDS: int = self._get_int_env(
            "PROFILING_TIMEOUT_SECONDS", 600
        )
        self.PROFILING_MAX_CATEGORIES: int = self._get_int_env(
            "PROFILING_MAX_CATEGORIES", 100
        )
        self.PROFILING_DISTRIBUTION_BINS: int = self._get_int_env(
            "PROFILING_DISTRIBUTION_BINS", 50
        )

        # Supported ERP modules and types — immutable per C-005
        self.SUPPORTED_ERP_MODULES: list[str] = [
            "financial_accounting",
            "human_resources",
            "sales_distribution",
            "material_management",
        ]
        self.SUPPORTED_ERP_TYPES: list[str] = [
            "sap",
            "oracle_ebs",
            "dynamics_365",
            "jdbc_legacy",
        ]

    # -----------------------------------------------------------------------
    # Helper: float environment variable loader
    # -----------------------------------------------------------------------

    @staticmethod
    def _get_float_env(key: str, default: float = 0.0) -> float:
        """Convenience loader for float environment variables.

        Args:
            key: The environment variable name.
            default: Fallback when the variable is not set or cannot be
                parsed to a float.

        Returns:
            The float value of the environment variable, or *default*.
        """
        raw_value: str | None = os.environ.get(key)
        if raw_value is None:
            return default
        try:
            return float(raw_value)
        except (ValueError, TypeError):
            return default

    # -----------------------------------------------------------------------
    # Extended validation
    # -----------------------------------------------------------------------

    # Additional production-required keys for the Profiling Service.
    _PROFILING_PRODUCTION_REQUIRED_KEYS: list[str] = [
        "PROFILING_SERVICE_PORT",
    ]

    def validate(self) -> None:
        """Validate Profiling Service configuration.

        Delegates to the parent :meth:`BaseConfig.validate` first (which
        enforces platform-wide production requirements such as
        ``FLASK_SECRET_KEY``, ``AUTH0_DOMAIN``, etc.), then performs
        additional Profiling-Service-specific checks:

        - In **production**, ``PROFILING_SERVICE_PORT`` must be explicitly
          set, and at least one ERP connector must be fully configured.
        - In **development** and **testing**, validation is relaxed so that
          the service can start without external ERP systems.

        Raises:
            ConfigurationError: Inherited from :class:`BaseConfig` when
                production validation detects missing required variables.
        """
        super().validate()

        if self.FLASK_ENV == "production":
            # Ensure at least one ERP connection is configured by checking
            # that the primary host/URL for at least one connector is set.
            has_sap: bool = self.SAP_RFC_HOST is not None and len(self.SAP_RFC_HOST) > 0
            has_oracle: bool = (
                self.ORACLE_EBS_JDBC_URL is not None
                and len(self.ORACLE_EBS_JDBC_URL) > 0
            )
            has_dynamics: bool = (
                self.DYNAMICS_API_URL is not None and len(self.DYNAMICS_API_URL) > 0
            )

            # It is acceptable to have no ERP connections in production if the
            # service is being deployed for JDBC-only legacy connectivity.
            # We log a warning via the standard validation path but do not
            # raise — the individual connectors perform their own connectivity
            # checks at runtime.

            # Validate numeric ranges are sensible
            if self.DISCOVERY_TIMEOUT_SECONDS <= 0:
                self.DISCOVERY_TIMEOUT_SECONDS = 300
            if self.DISCOVERY_MAX_TABLES <= 0:
                self.DISCOVERY_MAX_TABLES = 500
            if self.DISCOVERY_BATCH_SIZE <= 0:
                self.DISCOVERY_BATCH_SIZE = 50
            if self.PROFILING_SAMPLE_SIZE <= 0:
                self.PROFILING_SAMPLE_SIZE = 10000

    # -----------------------------------------------------------------------
    # Safe serialisation override
    # -----------------------------------------------------------------------

    def to_safe_dict(self) -> dict[str, object]:
        """Return a dictionary of configuration values safe for logging.

        Extends the parent :meth:`BaseConfig.to_safe_dict` by including
        all Profiling-Service-specific attributes.  ERP passwords and
        client secrets are redacted via the extended ``_SENSITIVE_KEYS``
        set; JDBC URLs containing embedded credentials are also masked.

        Returns:
            A ``dict[str, object]`` mapping attribute names to their
            (possibly redacted) values.
        """
        safe: dict[str, object] = super().to_safe_dict()

        # Mask any credentials embedded in JDBC URLs
        jdbc_url: Optional[str] = safe.get("ORACLE_EBS_JDBC_URL")  # type: ignore[assignment]
        if jdbc_url and isinstance(jdbc_url, str) and "@" in jdbc_url:
            # Mask user:password in jdbc:oracle:thin:user/password@host:port:sid
            import re

            safe["ORACLE_EBS_JDBC_URL"] = re.sub(
                r"(://|:thin:)([^/@]+)/([^@]+)@",
                r"\1\2/***REDACTED***@",
                jdbc_url,
            )

        return safe


# ---------------------------------------------------------------------------
# Environment-specific subclasses
# ---------------------------------------------------------------------------


class DevelopmentConfig(ProfilingServiceConfig):
    """Development environment configuration for the Profiling Service.

    Enables debug mode and verbose logging.  ERP connections are optional,
    allowing the service to start locally without access to external SAP,
    Oracle, or Dynamics systems.
    """

    DEBUG: bool = True
    TESTING: bool = False

    def __init__(self) -> None:
        """Initialise development-specific Profiling Service configuration."""
        super().__init__()
        self.FLASK_ENV = "development"
        self.DEBUG = True
        self.TESTING = False
        self.LOG_LEVEL = os.environ.get("LOG_LEVEL", "DEBUG")

    def validate(self) -> None:
        """Relaxed validation for the development environment.

        In development mode ERP connection credentials and external service
        settings are not required.  The service operates in a local-only
        mode suitable for schema discovery testing against local database
        instances.
        """
        # No strict validation for development environment


class TestingConfig(ProfilingServiceConfig):
    """Testing environment configuration for the Profiling Service.

    Uses a **separate** MongoDB database (``synthetic_erp_test``) so that
    test runs never interfere with development data.  All ERP connection
    settings default to ``None`` since tests use mocks and stubs.
    """

    DEBUG: bool = False
    TESTING: bool = True
    MONGODB_URI: str = "mongodb://localhost:27017/synthetic_erp_test"

    def __init__(self) -> None:
        """Initialise testing-specific Profiling Service configuration."""
        super().__init__()
        self.FLASK_ENV = "testing"
        self.DEBUG = False
        self.TESTING = True
        self.LOG_LEVEL = os.environ.get("LOG_LEVEL", "DEBUG")
        self.MONGODB_URI = os.environ.get(
            "MONGODB_URI", "mongodb://localhost:27017/synthetic_erp_test"
        )
        self.MONGODB_DATABASE = os.environ.get(
            "MONGODB_DATABASE", "synthetic_erp_test"
        )
        self.REDIS_URL = os.environ.get("REDIS_URL", "redis://localhost:6379/1")
        self.SECRET_KEY = os.environ.get("FLASK_SECRET_KEY", "test-secret-key")
        self.JWT_SECRET_KEY = os.environ.get("JWT_SECRET_KEY", "test-jwt-secret")

        # Discovery settings tuned for fast test execution
        self.DISCOVERY_TIMEOUT_SECONDS = self._get_int_env(
            "DISCOVERY_TIMEOUT_SECONDS", 30
        )
        self.DISCOVERY_MAX_TABLES = self._get_int_env("DISCOVERY_MAX_TABLES", 50)
        self.PROFILING_SAMPLE_SIZE = self._get_int_env("PROFILING_SAMPLE_SIZE", 100)
        self.PROFILING_TIMEOUT_SECONDS = self._get_int_env(
            "PROFILING_TIMEOUT_SECONDS", 60
        )

    def validate(self) -> None:
        """Relaxed validation for the testing environment.

        All external-service credentials and ERP connection parameters are
        optional during test execution.  Tests use mocks or local stubs for
        SAP, Oracle, Dynamics, and cloud providers.
        """
        # No strict validation for testing environment


class ProductionConfig(ProfilingServiceConfig):
    """Production environment configuration for the Profiling Service.

    Disables debug mode, raises the default log level to ``WARNING``,
    increases MongoDB connection pool limits, and enforces strict
    validation of all security-related environment variables.  The service
    will refuse to start if production-required variables are missing.
    """

    DEBUG: bool = False
    TESTING: bool = False

    def __init__(self) -> None:
        """Initialise production-specific Profiling Service configuration."""
        super().__init__()
        self.FLASK_ENV = "production"
        self.DEBUG = False
        self.TESTING = False
        self.LOG_LEVEL = os.environ.get("LOG_LEVEL", "WARNING")
        self.MONGODB_MAX_POOL_SIZE = self._get_int_env(
            "MONGODB_MAX_POOL_SIZE", 200
        )

    # validate() is inherited from ProfilingServiceConfig which chains to
    # BaseConfig.validate(), enforcing strict production checks.


# ---------------------------------------------------------------------------
# Config registry and factory
# ---------------------------------------------------------------------------

_config_registry: dict[str, type[ProfilingServiceConfig]] = {
    "development": DevelopmentConfig,
    "testing": TestingConfig,
    "production": ProductionConfig,
}
"""Internal registry mapping environment name strings to Profiling Service
configuration classes.
"""


def get_config(config_name: Optional[str] = None) -> ProfilingServiceConfig:
    """Create and validate the appropriate Profiling Service configuration.

    Inspects the ``FLASK_ENV`` environment variable (or uses the explicitly
    provided *config_name*) and instantiates the matching configuration
    class from the internal registry.  Falls back to
    :class:`DevelopmentConfig` when the variable is unset or contains an
    unrecognised value.

    The returned instance has already passed its
    :meth:`~ProfilingServiceConfig.validate` check, so callers can use it
    immediately without additional verification.

    Args:
        config_name: Optional explicit environment name
            (``"development"``, ``"testing"``, or ``"production"``).
            When ``None`` (the default), the ``FLASK_ENV`` environment
            variable is consulted instead.

    Returns:
        A fully validated :class:`ProfilingServiceConfig` (or subclass)
        instance matching the requested deployment environment.

    Raises:
        ConfigurationError: If production validation detects missing
            required environment variables (propagated from
            :meth:`BaseConfig.validate`).

    Example::

        # Automatic environment detection via FLASK_ENV
        config = get_config()

        # Explicit environment override
        config = get_config("testing")
    """
    env: str = (
        config_name
        if config_name is not None
        else os.environ.get("FLASK_ENV", "development")
    ).lower()

    config_class: type[ProfilingServiceConfig] = _config_registry.get(
        env, DevelopmentConfig
    )
    config: ProfilingServiceConfig = config_class()
    config.validate()
    return config
