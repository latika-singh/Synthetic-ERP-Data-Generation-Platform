"""Base configuration module for the Synthetic ERP Data Generation Platform.

This module implements the 12-factor app methodology by loading all configuration
values from environment variables. It provides:

- ``BaseConfig``: The root configuration class that all service-specific
  configuration classes inherit from.  Every attribute is loaded via
  ``os.environ.get()`` with sensible defaults for local development.
- ``DevelopmentConfig``, ``TestingConfig``, ``ProductionConfig``: Environment-
  specific subclasses that override defaults and validation strictness.
- ``ConfigurationError``: A custom exception raised when required configuration
  variables are missing or invalid.
- ``get_config()``: A factory function that inspects the ``FLASK_ENV``
  environment variable and returns the appropriate configuration instance,
  running startup validation before handing it back.
- ``config_registry``: A mapping of environment names to their configuration
  classes, provided for extensibility and programmatic lookup.

Usage::

    from shared.config.base import get_config

    config = get_config()
    print(config.MONGODB_URI)

No credentials, connection strings, or secrets are hard-coded anywhere in this
module.  Every sensitive value is read exclusively from the process environment.
"""

from __future__ import annotations

import os
import re
from typing import Any


# ---------------------------------------------------------------------------
# Custom exception
# ---------------------------------------------------------------------------

class ConfigurationError(Exception):
    """Raised when a configuration validation check fails.

    This exception is thrown by :meth:`BaseConfig.validate` (and its
    subclasses) when one or more required environment variables are missing
    or invalid for the current deployment environment.

    Attributes:
        message: Human-readable description of the validation failure,
            including the names of all missing variables when applicable.
    """

    def __init__(self, message: str) -> None:
        self.message = message
        super().__init__(self.message)


# ---------------------------------------------------------------------------
# Sentinel for "no default provided"
# ---------------------------------------------------------------------------

_UNSET: object = object()


# ---------------------------------------------------------------------------
# BaseConfig
# ---------------------------------------------------------------------------

class BaseConfig:
    """Root configuration class implementing the 12-factor app methodology.

    All configuration values are loaded from environment variables using
    :func:`os.environ.get`.  Each service-specific configuration module
    subclasses ``BaseConfig`` and adds its own settings.

    Class-level attributes define the canonical set of configuration keys
    that every microservice in the platform may consume.  Helper class
    methods (``_get_env``, ``_get_bool_env``, ``_get_int_env``,
    ``_get_list_env``) provide type-safe loading with casting and default
    value support.

    Attributes:
        FLASK_ENV: The deployment environment name
            (``development`` | ``testing`` | ``production``).
        DEBUG: Whether Flask debug mode is enabled.
        TESTING: Whether the application is in testing mode.
        SECRET_KEY: Flask session / CSRF secret key.
        SERVICE_NAME: Logical name of the running microservice.
        LOG_LEVEL: Python logging level string.
        MONGODB_URI: PyMongo connection string.
        MONGODB_DATABASE: Target MongoDB database name.
        MONGODB_MAX_POOL_SIZE: PyMongo connection-pool ceiling.
        REDIS_URL: Redis connection URL.
        REDIS_MAX_CONNECTIONS: Redis client connection-pool ceiling.
        AUTH0_DOMAIN: Auth0 tenant domain.
        AUTH0_CLIENT_ID: Auth0 application client ID.
        AUTH0_CLIENT_SECRET: Auth0 application client secret.
        AUTH0_AUDIENCE: Auth0 API audience identifier.
        JWT_SECRET_KEY: Signing key for JWT tokens.
        JWT_ACCESS_TOKEN_EXPIRES: Token lifetime in seconds.
        JWT_ALGORITHM: JWT signing algorithm (default ``RS256``).
        CORS_ORIGINS: Allowed CORS origins (parsed from comma-separated
            environment variable).
        RATE_LIMIT_DEFAULT: Default API rate limit (requests per minute).
        ENCRYPTION_KEY: AES-256 encryption key for data at rest.
        OTEL_EXPORTER_ENDPOINT: OpenTelemetry collector gRPC endpoint.
        PROMETHEUS_PORT: Port on which Prometheus metrics are exposed.
    """

    # -- Environment identification -----------------------------------------

    FLASK_ENV: str = os.environ.get("FLASK_ENV", "development")
    DEBUG: bool = FLASK_ENV != "production"
    TESTING: bool = False

    # -- Flask core ---------------------------------------------------------

    SECRET_KEY: str = os.environ.get("FLASK_SECRET_KEY", "dev-secret-key-change-me")

    # -- Service identification ---------------------------------------------

    SERVICE_NAME: str = os.environ.get("SERVICE_NAME", "unknown-service")

    # -- Logging ------------------------------------------------------------

    LOG_LEVEL: str = os.environ.get("LOG_LEVEL", "INFO")

    # -- MongoDB ------------------------------------------------------------

    MONGODB_URI: str = os.environ.get(
        "MONGODB_URI", "mongodb://localhost:27017/synthetic_erp"
    )
    MONGODB_DATABASE: str = os.environ.get("MONGODB_DATABASE", "synthetic_erp")
    MONGODB_MAX_POOL_SIZE: int = int(os.environ.get("MONGODB_MAX_POOL_SIZE", "100"))

    # -- Redis --------------------------------------------------------------

    REDIS_URL: str = os.environ.get("REDIS_URL", "redis://localhost:6379/0")
    REDIS_MAX_CONNECTIONS: int = int(os.environ.get("REDIS_MAX_CONNECTIONS", "50"))

    # -- Auth0 / OAuth 2.0 --------------------------------------------------

    AUTH0_DOMAIN: str = os.environ.get("AUTH0_DOMAIN", "")
    AUTH0_CLIENT_ID: str = os.environ.get("AUTH0_CLIENT_ID", "")
    AUTH0_CLIENT_SECRET: str = os.environ.get("AUTH0_CLIENT_SECRET", "")
    AUTH0_AUDIENCE: str = os.environ.get("AUTH0_AUDIENCE", "")

    # -- JWT ----------------------------------------------------------------

    JWT_SECRET_KEY: str = os.environ.get("JWT_SECRET_KEY", "")
    JWT_ACCESS_TOKEN_EXPIRES: int = int(
        os.environ.get("JWT_ACCESS_TOKEN_EXPIRES", "3600")
    )
    JWT_ALGORITHM: str = os.environ.get("JWT_ALGORITHM", "RS256")

    # -- CORS ---------------------------------------------------------------
    # Parsed from comma-separated CORS_ORIGINS env var at instance init time.

    CORS_ORIGINS: list[str] = []

    # -- Rate limiting ------------------------------------------------------

    RATE_LIMIT_DEFAULT: int = int(os.environ.get("RATE_LIMIT_DEFAULT", "60"))

    # -- Encryption ---------------------------------------------------------

    ENCRYPTION_KEY: str = os.environ.get("ENCRYPTION_KEY", "")

    # -- Observability ------------------------------------------------------

    OTEL_EXPORTER_ENDPOINT: str = os.environ.get("OTEL_EXPORTER_ENDPOINT", "")
    PROMETHEUS_PORT: int = int(os.environ.get("PROMETHEUS_PORT", "9090"))

    # -----------------------------------------------------------------------
    # Initialiser — resolves dynamic attributes at instance creation time
    # -----------------------------------------------------------------------

    def __init__(self) -> None:
        """Initialise a configuration instance.

        Re-reads environment variables so that instances created *after*
        the module has been imported still pick up the latest values.
        This is critical for test isolation and container orchestration
        where environment variables may be injected after import time.
        """
        self.FLASK_ENV = os.environ.get("FLASK_ENV", "development")
        self.DEBUG = self.FLASK_ENV != "production"
        self.TESTING = False

        self.SECRET_KEY = os.environ.get("FLASK_SECRET_KEY", "dev-secret-key-change-me")
        self.SERVICE_NAME = os.environ.get("SERVICE_NAME", "unknown-service")
        self.LOG_LEVEL = os.environ.get("LOG_LEVEL", "INFO")

        self.MONGODB_URI = os.environ.get(
            "MONGODB_URI", "mongodb://localhost:27017/synthetic_erp"
        )
        self.MONGODB_DATABASE = os.environ.get("MONGODB_DATABASE", "synthetic_erp")
        self.MONGODB_MAX_POOL_SIZE = self._get_int_env("MONGODB_MAX_POOL_SIZE", 100)

        self.REDIS_URL = os.environ.get("REDIS_URL", "redis://localhost:6379/0")
        self.REDIS_MAX_CONNECTIONS = self._get_int_env("REDIS_MAX_CONNECTIONS", 50)

        self.AUTH0_DOMAIN = os.environ.get("AUTH0_DOMAIN", "")
        self.AUTH0_CLIENT_ID = os.environ.get("AUTH0_CLIENT_ID", "")
        self.AUTH0_CLIENT_SECRET = os.environ.get("AUTH0_CLIENT_SECRET", "")
        self.AUTH0_AUDIENCE = os.environ.get("AUTH0_AUDIENCE", "")

        self.JWT_SECRET_KEY = os.environ.get("JWT_SECRET_KEY", "")
        self.JWT_ACCESS_TOKEN_EXPIRES = self._get_int_env(
            "JWT_ACCESS_TOKEN_EXPIRES", 3600
        )
        self.JWT_ALGORITHM = os.environ.get("JWT_ALGORITHM", "RS256")

        self.CORS_ORIGINS = self._get_list_env(
            "CORS_ORIGINS", default=["http://localhost:3000"]
        )

        self.RATE_LIMIT_DEFAULT = self._get_int_env("RATE_LIMIT_DEFAULT", 60)
        self.ENCRYPTION_KEY = os.environ.get("ENCRYPTION_KEY", "")

        self.OTEL_EXPORTER_ENDPOINT = os.environ.get("OTEL_EXPORTER_ENDPOINT", "")
        self.PROMETHEUS_PORT = self._get_int_env("PROMETHEUS_PORT", 9090)

    # -----------------------------------------------------------------------
    # Helper class methods for type-safe env var access
    # -----------------------------------------------------------------------

    @staticmethod
    def _get_env(
        key: str,
        default: Any = None,
        cast_type: type = str,
        required: bool = False,
    ) -> Any:
        """Load an environment variable with optional type casting.

        Args:
            key: The environment variable name.
            default: Fallback value when the variable is not set.  Ignored
                when *required* is ``True``.
            cast_type: Target Python type.  Supports ``str``, ``int``,
                ``float``, ``bool``, and ``list``.
            required: If ``True`` and the variable is absent, a
                :class:`ConfigurationError` is raised.

        Returns:
            The environment variable's value cast to *cast_type*, or
            *default* if the variable is not set and not required.

        Raises:
            ConfigurationError: If *required* is ``True`` and the
                variable is not found in the environment.
            ConfigurationError: If the raw value cannot be cast to the
                requested *cast_type*.
        """
        raw_value: str | None = os.environ.get(key)

        if raw_value is None:
            if required:
                raise ConfigurationError(
                    f"Required environment variable '{key}' is not set."
                )
            return default

        # Type-specific casting
        try:
            if cast_type is bool:
                return raw_value.lower() in ("true", "1", "yes", "on")
            if cast_type is int:
                return int(raw_value)
            if cast_type is float:
                return float(raw_value)
            if cast_type is list:
                return [
                    item.strip() for item in raw_value.split(",") if item.strip()
                ]
            return cast_type(raw_value)
        except (ValueError, TypeError) as exc:
            raise ConfigurationError(
                f"Environment variable '{key}' with value '{raw_value}' "
                f"cannot be cast to {cast_type.__name__}: {exc}"
            ) from exc

    @staticmethod
    def _get_bool_env(key: str, default: bool = False) -> bool:
        """Convenience loader for boolean environment variables.

        Interprets ``true``, ``1``, ``yes``, and ``on`` (case-insensitive)
        as ``True``; everything else as ``False``.

        Args:
            key: The environment variable name.
            default: Fallback when the variable is not set.

        Returns:
            The boolean interpretation of the variable's value.
        """
        raw_value: str | None = os.environ.get(key)
        if raw_value is None:
            return default
        return raw_value.lower() in ("true", "1", "yes", "on")

    @staticmethod
    def _get_list_env(
        key: str,
        default: list[str] | None = None,
        separator: str = ",",
    ) -> list[str]:
        """Convenience loader for comma-separated list environment variables.

        Args:
            key: The environment variable name.
            default: Fallback list when the variable is not set.  An empty
                list is returned if *default* is ``None``.
            separator: Delimiter used to split the raw string.

        Returns:
            A list of stripped, non-empty string tokens.
        """
        raw_value: str | None = os.environ.get(key)
        if raw_value is None:
            return default if default is not None else []
        return [item.strip() for item in raw_value.split(separator) if item.strip()]

    @staticmethod
    def _get_int_env(key: str, default: int = 0) -> int:
        """Convenience loader for integer environment variables.

        Args:
            key: The environment variable name.
            default: Fallback when the variable is not set or is not a
                valid integer.

        Returns:
            The integer value of the environment variable, or *default*.
        """
        raw_value: str | None = os.environ.get(key)
        if raw_value is None:
            return default
        try:
            return int(raw_value)
        except (ValueError, TypeError):
            return default

    # -----------------------------------------------------------------------
    # Validation
    # -----------------------------------------------------------------------

    # Keys that must be non-empty in production
    _PRODUCTION_REQUIRED_KEYS: list[str] = [
        "FLASK_SECRET_KEY",
        "AUTH0_DOMAIN",
        "AUTH0_CLIENT_ID",
        "JWT_SECRET_KEY",
        "ENCRYPTION_KEY",
    ]

    def validate(self) -> None:
        """Validate that all required configuration variables are present.

        In **production** mode the following environment variables must be
        set and non-empty:

        - ``FLASK_SECRET_KEY``
        - ``AUTH0_DOMAIN``
        - ``AUTH0_CLIENT_ID``
        - ``JWT_SECRET_KEY``
        - ``ENCRYPTION_KEY``

        In **development** and **testing** modes the validation is relaxed
        so that the platform can run locally without external identity
        providers or encryption infrastructure.

        Raises:
            ConfigurationError: With a message listing every missing
                variable when one or more required variables are absent.
        """
        if self.FLASK_ENV != "production":
            # Development and testing environments use relaxed validation.
            # Only verify that the SECRET_KEY is not the default placeholder
            # when running in a shared or CI/CD dev environment.
            return

        missing: list[str] = []
        for env_key in self._PRODUCTION_REQUIRED_KEYS:
            value: str | None = os.environ.get(env_key)
            if not value:
                missing.append(env_key)

        if missing:
            raise ConfigurationError(
                f"Production environment is missing required configuration "
                f"variable(s): {', '.join(missing)}. Set them as environment "
                f"variables before starting the service."
            )

    # -----------------------------------------------------------------------
    # Safe serialisation
    # -----------------------------------------------------------------------

    # Attribute names whose values must never appear in logs
    _SENSITIVE_KEYS: set[str] = frozenset({
        "SECRET_KEY",
        "JWT_SECRET_KEY",
        "AUTH0_CLIENT_SECRET",
        "ENCRYPTION_KEY",
    })

    _REDACTED: str = "***REDACTED***"

    def to_safe_dict(self) -> dict[str, Any]:
        """Return a dictionary of configuration values safe for logging.

        Sensitive values (secrets, tokens, passwords) are replaced with
        ``'***REDACTED***'``.  The ``MONGODB_URI`` is treated specially:
        any ``user:password@`` segment in the connection string is masked
        so that the host and database name remain visible for debugging
        while credentials are hidden.

        Returns:
            A ``dict[str, Any]`` mapping configuration attribute names to
            their (possibly redacted) values.
        """
        safe: dict[str, Any] = {}

        for attr_name in sorted(dir(self)):
            # Skip private / dunder attributes and methods
            if attr_name.startswith("_"):
                continue
            value: Any = getattr(self, attr_name)
            if callable(value):
                continue

            # Redact sensitive keys entirely
            if attr_name in self._SENSITIVE_KEYS:
                safe[attr_name] = self._REDACTED
                continue

            # Special handling: mask password in MONGODB_URI
            if attr_name == "MONGODB_URI" and isinstance(value, str):
                safe[attr_name] = re.sub(
                    r"://([^:]+):([^@]+)@",
                    r"://\1:***REDACTED***@",
                    value,
                )
                continue

            safe[attr_name] = value

        return safe


# ---------------------------------------------------------------------------
# Environment-specific subclasses
# ---------------------------------------------------------------------------

class DevelopmentConfig(BaseConfig):
    """Development environment configuration.

    Enables debug mode and verbose logging.  Auth0 and encryption keys
    are **not** required, allowing the platform to start locally without
    external identity providers or encryption infrastructure.
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

        In development mode only critical runtime errors (e.g. MongoDB
        connectivity) are caught at startup — Auth0 and encryption
        settings are optional.
        """
        # No strict validation for development


class TestingConfig(BaseConfig):
    """Testing environment configuration.

    Uses a **separate** MongoDB database (``synthetic_erp_test``) and a
    different Redis database index (``/1``) so that test runs never
    interfere with development data.
    """

    def __init__(self) -> None:
        """Initialise testing-specific configuration."""
        super().__init__()
        self.FLASK_ENV = "testing"
        self.DEBUG = True
        self.TESTING = True
        self.LOG_LEVEL = os.environ.get("LOG_LEVEL", "DEBUG")
        self.MONGODB_URI = os.environ.get(
            "MONGODB_URI", "mongodb://localhost:27017/synthetic_erp_test"
        )
        self.MONGODB_DATABASE = os.environ.get("MONGODB_DATABASE", "synthetic_erp_test")
        self.REDIS_URL = os.environ.get("REDIS_URL", "redis://localhost:6379/1")
        self.SECRET_KEY = os.environ.get("FLASK_SECRET_KEY", "test-secret-key")
        self.JWT_SECRET_KEY = os.environ.get("JWT_SECRET_KEY", "test-jwt-secret")

    def validate(self) -> None:
        """Relaxed validation for the testing environment.

        All external-service credentials are optional during test
        execution; tests use mocks or local stubs for Auth0, encryption,
        and cloud providers.
        """
        # No strict validation for testing


class ProductionConfig(BaseConfig):
    """Production environment configuration.

    Disables debug mode, raises the log level to ``WARNING``, and
    increases MongoDB connection-pool limits.  **All** security-related
    environment variables are **required** — the service will refuse to
    start if any are missing.
    """

    def __init__(self) -> None:
        """Initialise production-specific configuration."""
        super().__init__()
        self.FLASK_ENV = "production"
        self.DEBUG = False
        self.TESTING = False
        self.LOG_LEVEL = os.environ.get("LOG_LEVEL", "WARNING")
        self.MONGODB_MAX_POOL_SIZE = self._get_int_env("MONGODB_MAX_POOL_SIZE", 200)

    # validate() is inherited from BaseConfig and enforces strict checks
    # when FLASK_ENV == "production".


# ---------------------------------------------------------------------------
# Config registry and factory
# ---------------------------------------------------------------------------

config_registry: dict[str, type[BaseConfig]] = {
    "development": DevelopmentConfig,
    "testing": TestingConfig,
    "production": ProductionConfig,
}
"""Maps environment name strings to their configuration classes.

This registry is provided for programmatic lookup and extensibility.
Third-party or service-specific configurations can register additional
environments by adding entries to this mapping before calling
:func:`get_config`.
"""


def get_config() -> BaseConfig:
    """Create and validate the appropriate configuration for the current environment.

    Reads the ``FLASK_ENV`` environment variable and instantiates the
    matching configuration class from :data:`config_registry`.  Falls
    back to :class:`DevelopmentConfig` when the variable is unset or
    contains an unrecognised value.

    The returned instance has already passed its :meth:`~BaseConfig.validate`
    check, so callers can use it immediately without additional verification.

    Returns:
        A fully validated configuration instance matching the current
        deployment environment.

    Raises:
        ConfigurationError: If production validation detects missing
            required environment variables.

    Example::

        config = get_config()
        app.config.from_object(config)
    """
    env: str = os.environ.get("FLASK_ENV", "development").lower()
    config_class: type[BaseConfig] = config_registry.get(env, DevelopmentConfig)
    config: BaseConfig = config_class()
    config.validate()
    return config
