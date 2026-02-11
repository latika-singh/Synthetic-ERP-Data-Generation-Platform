"""Environment-based configuration classes for the API Gateway service.

This module defines the configuration hierarchy for the API Gateway,
following the 12-factor app methodology.  All configuration values are
loaded from environment variables with sensible defaults suitable for
local development.

The API Gateway's :class:`Config` extends
:class:`~shared.config.base.BaseConfig` to inherit foundational settings
(MongoDB, Redis, Auth0, JWT, CORS, logging, encryption) and adds
service-specific configuration including:

- **Tiered rate limiting** — Developer (60), Data Engineer (300),
  Platform Admin (1000) requests per minute.
- **Inter-service REST URLs** — Base addresses for the Generation Engine,
  Profiling Service, Quality Service, Compliance Service, and
  Provisioning Service.
- **JWT timedelta expiration** — Converts integer seconds into
  :class:`datetime.timedelta` objects expected by Flask-JWT-Extended.
- **Auth0 API audience** — Separate from the base Auth0 audience for
  API-specific token validation.
- **Structured JSON logging** — ``LOG_FORMAT`` defaults to ``'json'``
  for integration with the platform's observability stack.

Configuration Classes:
    Config: Base API Gateway configuration extending shared BaseConfig.
    DevelopmentConfig: Development environment overrides (debug enabled).
    TestingConfig: Test environment overrides (isolated database).
    ProductionConfig: Production environment with strict validation.

Usage::

    from api_gateway.config import config_map

    env = os.environ.get("FLASK_ENV", "development")
    config_class = config_map.get(env, DevelopmentConfig)
    app.config.from_object(config_class())

No credentials, connection strings, or secrets are hard-coded in this
module.  Every sensitive value is read exclusively from the process
environment.
"""

from __future__ import annotations

import os
from datetime import timedelta

from shared.config.base import BaseConfig, ConfigurationError


# ---------------------------------------------------------------------------
# API Gateway Base Configuration
# ---------------------------------------------------------------------------


class Config(BaseConfig):
    """API Gateway base configuration extending the shared BaseConfig.

    Inherits foundational 12-factor app settings from
    :class:`~shared.config.base.BaseConfig` (``SECRET_KEY``, ``DEBUG``,
    ``TESTING``, ``MONGODB_URI``, ``MONGODB_DATABASE``,
    ``MONGODB_MAX_POOL_SIZE``, ``REDIS_URL``, ``AUTH0_DOMAIN``,
    ``AUTH0_CLIENT_ID``, ``AUTH0_CLIENT_SECRET``, ``AUTH0_AUDIENCE``,
    ``JWT_SECRET_KEY``, ``JWT_ALGORITHM``, ``CORS_ORIGINS``,
    ``RATE_LIMIT_DEFAULT``, ``LOG_LEVEL``, ``ENCRYPTION_KEY``,
    ``SERVICE_NAME``) and adds API Gateway-specific configuration.

    The :meth:`validate` and :meth:`to_safe_dict` methods are also
    inherited, providing production readiness checks and safe logging
    of configuration values.

    Attributes:
        JWT_ACCESS_TOKEN_EXPIRES: Access token lifetime as
            :class:`datetime.timedelta`.  Defaults to 1 hour.
            Flask-JWT-Extended expects a ``timedelta`` value.
        JWT_REFRESH_TOKEN_EXPIRES: Refresh token lifetime as
            :class:`datetime.timedelta`.  Defaults to 30 days.
        AUTH0_API_AUDIENCE: Auth0 API audience identifier for token
            validation scoping.  Falls back to ``AUTH0_AUDIENCE`` from
            the base class when unset.
        RATE_LIMIT_ELEVATED: Rate limit for the Data Engineer role
            (requests per minute).  Defaults to 300.
        RATE_LIMIT_ADMIN: Rate limit for the Platform Admin role
            (requests per minute).  Defaults to 1000.
        GENERATION_ENGINE_URL: Base URL for the Generation Engine
            service.
        PROFILING_SERVICE_URL: Base URL for the Profiling Service.
        QUALITY_SERVICE_URL: Base URL for the Quality Service.
        COMPLIANCE_SERVICE_URL: Base URL for the Compliance Service.
        PROVISIONING_SERVICE_URL: Base URL for the Provisioning Service.
        LOG_FORMAT: Structured log output format.  Defaults to
            ``'json'`` for integration with the observability stack.
    """

    # -- JWT / Auth0 (extends base with timedelta and API audience) ----------
    # NOTE: JWT_ACCESS_TOKEN_EXPIRES is intentionally NOT declared at class
    # level here because BaseConfig defines it as ``int`` (seconds).  The
    # API Gateway's ``__init__`` converts the integer value into a
    # ``datetime.timedelta`` as Flask-JWT-Extended expects.  Declaring it
    # here with ``timedelta`` would create an incompatible override.

    JWT_REFRESH_TOKEN_EXPIRES: timedelta = timedelta(days=30)
    AUTH0_API_AUDIENCE: str = os.environ.get("AUTH0_API_AUDIENCE", "")

    # -- Tiered rate limiting ------------------------------------------------
    # Developer = RATE_LIMIT_DEFAULT (60, inherited from BaseConfig)
    # Data Engineer = RATE_LIMIT_ELEVATED (300)
    # Platform Admin = RATE_LIMIT_ADMIN (1000)

    RATE_LIMIT_ELEVATED: int = int(os.environ.get("RATE_LIMIT_ELEVATED", "300"))
    RATE_LIMIT_ADMIN: int = int(os.environ.get("RATE_LIMIT_ADMIN", "1000"))

    # -- Inter-service communication URLs ------------------------------------

    GENERATION_ENGINE_URL: str = os.environ.get(
        "GENERATION_ENGINE_URL", "http://generation-engine:5001"
    )
    PROFILING_SERVICE_URL: str = os.environ.get(
        "PROFILING_SERVICE_URL", "http://profiling-service:5002"
    )
    QUALITY_SERVICE_URL: str = os.environ.get(
        "QUALITY_SERVICE_URL", "http://quality-service:5003"
    )
    COMPLIANCE_SERVICE_URL: str = os.environ.get(
        "COMPLIANCE_SERVICE_URL", "http://compliance-service:5004"
    )
    PROVISIONING_SERVICE_URL: str = os.environ.get(
        "PROVISIONING_SERVICE_URL", "http://provisioning-service:5005"
    )

    # -- Logging -------------------------------------------------------------

    LOG_FORMAT: str = "json"

    # -----------------------------------------------------------------------
    # Initialiser
    # -----------------------------------------------------------------------

    def __init__(self) -> None:
        """Initialise API Gateway configuration.

        Calls the parent :class:`~shared.config.base.BaseConfig`
        initialiser to load shared settings from the environment, then
        re-reads API Gateway-specific environment variables and converts
        JWT expiration values to :class:`datetime.timedelta` objects as
        required by Flask-JWT-Extended.
        """
        super().__init__()

        # -- Service identity ------------------------------------------------
        self.SERVICE_NAME: str = os.environ.get("SERVICE_NAME", "api-gateway")

        # -- Flask core override ---------------------------------------------
        # API Gateway uses a distinct default sentinel so that
        # ProductionConfig can detect an unchanged secret.
        self.SECRET_KEY: str = os.environ.get(
            "FLASK_SECRET_KEY", "dev-secret-key-change-in-production"
        )

        # -- JWT expiration as timedelta ------------------------------------
        # BaseConfig stores JWT_ACCESS_TOKEN_EXPIRES as int (seconds).
        # Flask-JWT-Extended expects timedelta objects, so we convert.
        access_token_seconds: int = self._get_int_env(
            "JWT_ACCESS_TOKEN_EXPIRES", 3600
        )
        # Intentional type override: BaseConfig stores seconds as int,
        # but Flask-JWT-Extended requires a timedelta object.
        self.JWT_ACCESS_TOKEN_EXPIRES: timedelta = timedelta(  # type: ignore[assignment]
            seconds=access_token_seconds
        )

        refresh_token_days: int = self._get_int_env(
            "JWT_REFRESH_TOKEN_EXPIRES", 30
        )
        self.JWT_REFRESH_TOKEN_EXPIRES: timedelta = timedelta(
            days=refresh_token_days
        )

        # -- Auth0 API audience ----------------------------------------------
        # Falls back to the base AUTH0_AUDIENCE when the API-specific
        # audience variable is not set, ensuring backward compatibility.
        self.AUTH0_API_AUDIENCE: str = os.environ.get(
            "AUTH0_API_AUDIENCE", self.AUTH0_AUDIENCE
        )

        # -- Tiered rate limiting --------------------------------------------
        self.RATE_LIMIT_ELEVATED: int = self._get_int_env(
            "RATE_LIMIT_ELEVATED", 300
        )
        self.RATE_LIMIT_ADMIN: int = self._get_int_env(
            "RATE_LIMIT_ADMIN", 1000
        )

        # -- Inter-service communication URLs --------------------------------
        self.GENERATION_ENGINE_URL: str = os.environ.get(
            "GENERATION_ENGINE_URL", "http://generation-engine:5001"
        )
        self.PROFILING_SERVICE_URL: str = os.environ.get(
            "PROFILING_SERVICE_URL", "http://profiling-service:5002"
        )
        self.QUALITY_SERVICE_URL: str = os.environ.get(
            "QUALITY_SERVICE_URL", "http://quality-service:5003"
        )
        self.COMPLIANCE_SERVICE_URL: str = os.environ.get(
            "COMPLIANCE_SERVICE_URL", "http://compliance-service:5004"
        )
        self.PROVISIONING_SERVICE_URL: str = os.environ.get(
            "PROVISIONING_SERVICE_URL", "http://provisioning-service:5005"
        )

        # -- Logging format --------------------------------------------------
        self.LOG_FORMAT: str = os.environ.get("LOG_FORMAT", "json")


# ---------------------------------------------------------------------------
# Environment-specific subclasses
# ---------------------------------------------------------------------------


class DevelopmentConfig(Config):
    """Development environment configuration for the API Gateway.

    Enables Flask debug mode and sets verbose ``DEBUG``-level logging to
    aid local development and troubleshooting.  Auth0 credentials and
    encryption keys are **not** required, allowing the gateway to start
    locally without external identity providers.

    Attributes:
        DEBUG: Enabled (``True``) for hot-reload and detailed error pages.
        LOG_LEVEL: Defaults to ``'DEBUG'`` for maximum verbosity.
    """

    def __init__(self) -> None:
        """Initialise development-specific API Gateway configuration."""
        super().__init__()
        self.DEBUG: bool = True
        self.LOG_LEVEL: str = os.environ.get("LOG_LEVEL", "DEBUG")

    def validate(self) -> None:
        """Relaxed validation for the development environment.

        Skips strict production checks so that the API Gateway can run
        locally without Auth0 credentials, encryption keys, or TLS
        certificates.  Only critical runtime errors (e.g. MongoDB
        connectivity) are caught at startup.
        """
        # No strict validation required for development


class TestingConfig(Config):
    """Testing environment configuration for the API Gateway.

    Uses a **separate** MongoDB database (``synthetic_erp_test``) and a
    different Redis database index (``/1``) so that automated test runs
    never interfere with development data.

    Attributes:
        TESTING: Enabled (``True``) to activate Flask test mode.
        MONGODB_DATABASE: Isolated test database name.
        REDIS_URL: Isolated Redis database index ``/1``.
    """

    def __init__(self) -> None:
        """Initialise testing-specific API Gateway configuration."""
        super().__init__()
        self.TESTING: bool = True
        self.DEBUG: bool = True
        self.MONGODB_DATABASE: str = os.environ.get(
            "MONGODB_DATABASE", "synthetic_erp_test"
        )
        self.REDIS_URL: str = os.environ.get(
            "REDIS_URL", "redis://localhost:6379/1"
        )
        # Deterministic secrets for reproducible test execution
        self.SECRET_KEY: str = os.environ.get(
            "FLASK_SECRET_KEY", "test-secret-key"
        )
        self.JWT_SECRET_KEY: str = os.environ.get(
            "JWT_SECRET_KEY", "test-jwt-secret"
        )

    def validate(self) -> None:
        """Relaxed validation for the testing environment.

        All external-service credentials are optional during test
        execution — tests use mocks or local stubs for Auth0,
        encryption, and cloud providers.
        """
        # No strict validation required for testing


class ProductionConfig(Config):
    """Production environment configuration for the API Gateway.

    Disables debug mode, raises the log level to ``WARNING``, and
    enables secure session cookies (``Secure`` and ``HttpOnly`` flags).
    The :meth:`validate` method enforces strict checks to ensure that
    all required credentials, encryption keys, and secrets are present
    before the service starts.

    Attributes:
        LOG_LEVEL: Defaults to ``'WARNING'`` to reduce log volume.
        SESSION_COOKIE_SECURE: Enabled — cookies sent only over HTTPS.
        SESSION_COOKIE_HTTPONLY: Enabled — cookies inaccessible to JS.
    """

    def __init__(self) -> None:
        """Initialise production-specific API Gateway configuration."""
        super().__init__()
        self.DEBUG: bool = False
        self.TESTING: bool = False
        self.LOG_LEVEL: str = os.environ.get("LOG_LEVEL", "WARNING")
        self.SESSION_COOKIE_SECURE: bool = True
        self.SESSION_COOKIE_HTTPONLY: bool = True

    def validate(self) -> None:
        """Strict validation for the production environment.

        Verifies that the ``SECRET_KEY`` has been changed from its
        default development placeholder and then delegates further
        checks to the parent :meth:`~shared.config.base.BaseConfig.validate`
        method, which enforces all production-required environment
        variables (``AUTH0_DOMAIN``, ``AUTH0_CLIENT_ID``,
        ``JWT_SECRET_KEY``, ``ENCRYPTION_KEY``).

        Raises:
            ConfigurationError: If ``SECRET_KEY`` still holds the
                default value ``'dev-secret-key-change-in-production'``.
            ConfigurationError: If any required production environment
                variable (enforced by the parent) is missing.
        """
        if self.SECRET_KEY == "dev-secret-key-change-in-production":
            raise ConfigurationError(
                "Production SECRET_KEY must be changed from its default "
                "value. Set the FLASK_SECRET_KEY environment variable to "
                "a cryptographically secure random string."
            )
        # Delegate to BaseConfig's production validation which checks
        # AUTH0_DOMAIN, AUTH0_CLIENT_ID, JWT_SECRET_KEY, ENCRYPTION_KEY.
        super().validate()


# ---------------------------------------------------------------------------
# Configuration registry
# ---------------------------------------------------------------------------

config_map: dict[str, type[Config]] = {
    "development": DevelopmentConfig,
    "testing": TestingConfig,
    "production": ProductionConfig,
}
"""Maps environment name strings to API Gateway configuration classes.

Used by the API Gateway application factory to select the appropriate
configuration based on the ``FLASK_ENV`` environment variable.

Example::

    from api_gateway.config import config_map

    env = os.environ.get("FLASK_ENV", "development")
    config_class = config_map.get(env, DevelopmentConfig)
    config_instance = config_class()
    config_instance.validate()
    app.config.from_object(config_instance)
"""
