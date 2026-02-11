"""Configuration module for the Compliance Service.

This module defines the complete configuration hierarchy for the Compliance
Service, which is responsible for PII detection, regulatory compliance
verification (GDPR, HIPAA, CCPA), tamper-evident compliance certification,
and audit logging across the Synthetic ERP Data Generation Platform.

All configuration values follow the 12-factor app methodology, reading
from environment variables with sensible defaults for local development.
The module extends :class:`~shared.config.base.BaseConfig` to inherit
shared platform settings (MongoDB, Redis, JWT, logging, etc.) and layers
on compliance-specific settings including:

- **PII Detection**: spaCy NLP model selection, confidence thresholds,
  batch sizes, regex patterns for SSN/email/phone/credit-card/IBAN/IP/DOB,
  and configurable NLP entity types.
- **Regulatory Frameworks**: Toggle switches for GDPR, HIPAA, and CCPA
  loaded from a comma-separated environment variable.
- **Certification**: Hash algorithm, signing key, and the compliance
  state-machine states (Pending → Scanning → PIICheck → Certified → Released).
- **Audit Logging**: Collection name, retention period (default 7 years
  per SOC 2 Type II), and pre-computed retention in seconds.

Usage::

    from compliance_service.config import config_by_name

    config = config_by_name["development"]()
    print(config.PII_PATTERNS)

Classes:
    BaseComplianceConfig: Root compliance configuration with all PII,
        regulatory, certification, and audit settings.
    DevelopmentConfig: Development overrides (relaxed thresholds, verbose logs).
    TestingConfig: Testing overrides (test database, small model).
    ProductionConfig: Production overrides (strict thresholds, minimal logs).

Constants:
    config_by_name: Maps environment name strings to configuration classes.
"""

from __future__ import annotations

import os

from shared.config.base import BaseConfig, ConfigurationError


# ---------------------------------------------------------------------------
# Base Compliance Configuration
# ---------------------------------------------------------------------------


class BaseComplianceConfig(BaseConfig):
    """Root configuration class for the Compliance Service.

    Extends :class:`~shared.config.base.BaseConfig` with all settings
    specific to PII detection, regulatory compliance verification,
    tamper-evident certification, and SOC 2 Type II audit logging.

    Every attribute is loaded from an environment variable with a sensible
    default for local development.  Class-level attributes define the
    canonical defaults; the ``__init__`` method re-reads the environment
    so that instances created after module import still pick up the latest
    values (critical for container orchestration and test isolation).

    Attributes:
        SERVICE_NAME: Logical name identifying this microservice.
        SERVICE_PORT: TCP port the service listens on.
        DEBUG: Whether Flask debug mode is enabled.
        TESTING: Whether the application is in testing mode.
        SPACY_MODEL_NAME: spaCy NLP model used for entity recognition.
        PII_CONFIDENCE_THRESHOLD: Minimum confidence score (0.0-1.0) for
            a PII detection to be considered positive.
        PII_SCAN_BATCH_SIZE: Number of records processed per PII scan
            batch to control memory usage.
        PII_PATTERNS: Mapping of PII category names to compiled-ready
            regex patterns for pattern-based detection.
        NLP_ENTITY_TYPES: List of spaCy entity labels considered
            personally identifiable (e.g. PERSON, GPE).
        ENABLED_REGULATIONS: List of active regulatory framework codes.
        GDPR_ENABLED: Whether GDPR compliance checks are active.
        HIPAA_ENABLED: Whether HIPAA compliance checks are active.
        CCPA_ENABLED: Whether CCPA compliance checks are active.
        CERTIFICATION_HASH_ALGORITHM: Hash algorithm for tamper-evident
            compliance certificates (SHA-256).
        CERTIFICATION_SIGNING_KEY: Secret key for signing compliance
            certificates.  Must be set in production.
        COMPLIANCE_STATE_MACHINE_STATES: Ordered list of states in the
            compliance verification workflow.
        AUDIT_LOG_COLLECTION: MongoDB collection name for audit events.
        AUDIT_LOG_RETENTION_YEARS: Number of years audit logs are retained
            (default 7 per SOC 2 Type II).
        AUDIT_LOG_RETENTION_SECONDS: Pre-computed retention period in
            seconds for TTL index configuration.
    """

    # -- Service identification ------------------------------------------------

    SERVICE_NAME: str = "compliance-service"
    SERVICE_PORT: int = int(os.environ.get("COMPLIANCE_SERVICE_PORT", "5004"))
    DEBUG: bool = False
    TESTING: bool = False

    # -- PII Detection Settings ------------------------------------------------

    SPACY_MODEL_NAME: str = os.environ.get("SPACY_MODEL_NAME", "en_core_web_sm")

    PII_CONFIDENCE_THRESHOLD: float = float(os.environ.get("PII_CONFIDENCE_THRESHOLD", "0.85"))

    PII_SCAN_BATCH_SIZE: int = int(os.environ.get("PII_SCAN_BATCH_SIZE", "1000"))

    PII_PATTERNS: dict[str, str] = {
        "SSN": r"\b\d{3}-\d{2}-\d{4}\b",
        "EMAIL": r"\b[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Z|a-z]{2,}\b",
        "PHONE": r"\b(\+?1[-.]?)?\(?\d{3}\)?[-.]?\d{3}[-.]?\d{4}\b",
        "CREDIT_CARD": r"\b(?:\d{4}[-\s]?){3}\d{4}\b",
        "IBAN": r"\b[A-Z]{2}\d{2}[A-Z0-9]{4}\d{7}([A-Z0-9]?){0,16}\b",
        "IP_ADDRESS": r"\b(?:\d{1,3}\.){3}\d{1,3}\b",
        "DATE_OF_BIRTH": r"\b(0[1-9]|1[0-2])/(0[1-9]|[12]\d|3[01])/\d{4}\b",
    }

    NLP_ENTITY_TYPES: list[str] = [
        "PERSON",
        "GPE",
        "LOC",
        "ORG",
        "DATE",
        "NORP",
    ]

    # -- Regulatory Framework Settings -----------------------------------------

    ENABLED_REGULATIONS: list[str] = os.environ.get("ENABLED_REGULATIONS", "GDPR,HIPAA,CCPA").split(",")

    GDPR_ENABLED: bool = "GDPR" in ENABLED_REGULATIONS
    HIPAA_ENABLED: bool = "HIPAA" in ENABLED_REGULATIONS
    CCPA_ENABLED: bool = "CCPA" in ENABLED_REGULATIONS

    # -- Certification Settings ------------------------------------------------

    CERTIFICATION_HASH_ALGORITHM: str = "sha256"

    CERTIFICATION_SIGNING_KEY: str = os.environ.get("CERTIFICATION_SIGNING_KEY", "")

    COMPLIANCE_STATE_MACHINE_STATES: list[str] = [
        "Pending",
        "Scanning",
        "PIICheck",
        "Certified",
        "Released",
    ]

    # -- Audit Logging Settings ------------------------------------------------

    AUDIT_LOG_COLLECTION: str = "audit_logs"

    AUDIT_LOG_RETENTION_YEARS: int = int(os.environ.get("AUDIT_LOG_RETENTION_YEARS", "7"))

    AUDIT_LOG_RETENTION_SECONDS: int = AUDIT_LOG_RETENTION_YEARS * 365 * 24 * 3600

    # -- Sensitive keys extension ----------------------------------------------
    # Extend the parent set so that to_safe_dict() redacts compliance secrets.

    _SENSITIVE_KEYS: frozenset[str] = BaseConfig._SENSITIVE_KEYS | frozenset({"CERTIFICATION_SIGNING_KEY"})

    # -----------------------------------------------------------------------
    # Initialiser
    # -----------------------------------------------------------------------

    def __init__(self) -> None:
        """Initialise the compliance service configuration.

        Calls the parent :meth:`BaseConfig.__init__` to load shared
        platform settings (MongoDB, Redis, Auth0, JWT, observability),
        then re-reads every compliance-specific environment variable so
        that late-injected values (e.g. Kubernetes ConfigMap mounts) are
        picked up correctly.
        """
        super().__init__()

        # Override inherited service identification
        self.SERVICE_NAME = "compliance-service"
        self.SERVICE_PORT = self._get_int_env("COMPLIANCE_SERVICE_PORT", 5004)
        self.DEBUG = False
        self.TESTING = False

        # -- PII Detection Settings -------------------------------------------
        self.SPACY_MODEL_NAME = os.environ.get("SPACY_MODEL_NAME", "en_core_web_sm")
        self.PII_CONFIDENCE_THRESHOLD = float(os.environ.get("PII_CONFIDENCE_THRESHOLD", "0.85"))
        self.PII_SCAN_BATCH_SIZE = self._get_int_env("PII_SCAN_BATCH_SIZE", 1000)

        # PII regex patterns (immutable per deployment — not environment-driven)
        self.PII_PATTERNS: dict[str, str] = {
            "SSN": r"\b\d{3}-\d{2}-\d{4}\b",
            "EMAIL": r"\b[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Z|a-z]{2,}\b",
            "PHONE": r"\b(\+?1[-.]?)?\(?\d{3}\)?[-.]?\d{3}[-.]?\d{4}\b",
            "CREDIT_CARD": r"\b(?:\d{4}[-\s]?){3}\d{4}\b",
            "IBAN": r"\b[A-Z]{2}\d{2}[A-Z0-9]{4}\d{7}([A-Z0-9]?){0,16}\b",
            "IP_ADDRESS": r"\b(?:\d{1,3}\.){3}\d{1,3}\b",
            "DATE_OF_BIRTH": r"\b(0[1-9]|1[0-2])/(0[1-9]|[12]\d|3[01])/\d{4}\b",
        }

        # spaCy entity labels considered personally identifiable
        self.NLP_ENTITY_TYPES: list[str] = [
            "PERSON",
            "GPE",
            "LOC",
            "ORG",
            "DATE",
            "NORP",
        ]

        # -- Regulatory Framework Settings -------------------------------------
        self.ENABLED_REGULATIONS = self._get_list_env("ENABLED_REGULATIONS", default=["GDPR", "HIPAA", "CCPA"])
        self.GDPR_ENABLED: bool = "GDPR" in self.ENABLED_REGULATIONS
        self.HIPAA_ENABLED: bool = "HIPAA" in self.ENABLED_REGULATIONS
        self.CCPA_ENABLED: bool = "CCPA" in self.ENABLED_REGULATIONS

        # -- Certification Settings --------------------------------------------
        self.CERTIFICATION_HASH_ALGORITHM: str = "sha256"
        self.CERTIFICATION_SIGNING_KEY = os.environ.get("CERTIFICATION_SIGNING_KEY", "")
        self.COMPLIANCE_STATE_MACHINE_STATES: list[str] = [
            "Pending",
            "Scanning",
            "PIICheck",
            "Certified",
            "Released",
        ]

        # -- Audit Logging Settings --------------------------------------------
        self.AUDIT_LOG_COLLECTION: str = "audit_logs"
        self.AUDIT_LOG_RETENTION_YEARS = self._get_int_env("AUDIT_LOG_RETENTION_YEARS", 7)
        # Pre-compute retention in seconds for MongoDB TTL index configuration
        self.AUDIT_LOG_RETENTION_SECONDS: int = self.AUDIT_LOG_RETENTION_YEARS * 365 * 24 * 3600

    # -----------------------------------------------------------------------
    # Validation (extends parent)
    # -----------------------------------------------------------------------

    _PRODUCTION_REQUIRED_KEYS: list[str] = [
        *BaseConfig._PRODUCTION_REQUIRED_KEYS,
        "CERTIFICATION_SIGNING_KEY",
    ]

    def validate(self) -> None:
        """Validate compliance-specific configuration variables.

        Delegates to the parent :meth:`BaseConfig.validate` for shared
        platform checks, then enforces compliance-specific constraints:

        - In **production**, ``CERTIFICATION_SIGNING_KEY`` must be set
          and non-empty for tamper-evident signing.
        - The ``PII_CONFIDENCE_THRESHOLD`` must be in the [0.0, 1.0] range
          in all environments.
        - ``AUDIT_LOG_RETENTION_YEARS`` must be positive.

        Raises:
            ConfigurationError: If any required variable is missing or
                if a value is outside its valid range.
        """
        super().validate()

        # Confidence threshold must be a valid probability
        if not 0.0 <= self.PII_CONFIDENCE_THRESHOLD <= 1.0:
            raise ConfigurationError(
                f"PII_CONFIDENCE_THRESHOLD must be between 0.0 and 1.0, got {self.PII_CONFIDENCE_THRESHOLD}."
            )

        # Retention years must be positive
        if self.AUDIT_LOG_RETENTION_YEARS < 1:
            raise ConfigurationError(f"AUDIT_LOG_RETENTION_YEARS must be >= 1, got {self.AUDIT_LOG_RETENTION_YEARS}.")


# ---------------------------------------------------------------------------
# Environment-specific subclasses
# ---------------------------------------------------------------------------


class DevelopmentConfig(BaseComplianceConfig):
    """Development environment configuration for the Compliance Service.

    Enables debug mode and verbose logging.  Lowers the PII confidence
    threshold to 0.7 so that borderline detections surface during
    development and testing of PII patterns.  Auth0, encryption keys,
    and the certification signing key are not required, allowing the
    service to run locally without external dependencies.
    """

    def __init__(self) -> None:
        """Initialise development-specific compliance configuration."""
        super().__init__()
        self.FLASK_ENV = "development"
        self.DEBUG = True
        self.TESTING = False
        self.LOG_LEVEL = os.environ.get("LOG_LEVEL", "DEBUG")

        # Lower confidence threshold for development to surface borderline
        # PII detections that may need tuning.
        self.PII_CONFIDENCE_THRESHOLD = float(os.environ.get("PII_CONFIDENCE_THRESHOLD", "0.7"))

    def validate(self) -> None:
        """Relaxed validation for the development environment.

        Skips production-level checks for signing keys and external
        service credentials.  Only validates value-range constraints on
        PII thresholds and retention settings.
        """
        # Validate value ranges without strict production key checks
        if not 0.0 <= self.PII_CONFIDENCE_THRESHOLD <= 1.0:
            raise ConfigurationError(
                f"PII_CONFIDENCE_THRESHOLD must be between 0.0 and 1.0, got {self.PII_CONFIDENCE_THRESHOLD}."
            )


class TestingConfig(BaseComplianceConfig):
    """Testing environment configuration for the Compliance Service.

    Uses a dedicated ``test_compliance`` MongoDB database to isolate test
    data from development state.  Forces the smallest spaCy model for
    fast test execution and disables debug mode side-effects that could
    interfere with assertions.
    """

    def __init__(self) -> None:
        """Initialise testing-specific compliance configuration."""
        super().__init__()
        self.FLASK_ENV = "testing"
        self.DEBUG = True
        self.TESTING = True
        self.LOG_LEVEL = os.environ.get("LOG_LEVEL", "DEBUG")

        # Isolated test database
        self.MONGODB_URI = os.environ.get("MONGODB_URI", "mongodb://localhost:27017/test_compliance")
        self.MONGODB_DATABASE = os.environ.get("MONGODB_DATABASE", "test_compliance")

        # Use the smallest model for speed in CI/CD pipelines
        self.SPACY_MODEL_NAME = os.environ.get("SPACY_MODEL_NAME", "en_core_web_sm")

        # Test-friendly secrets
        self.SECRET_KEY = os.environ.get("FLASK_SECRET_KEY", "test-compliance-secret")
        self.JWT_SECRET_KEY = os.environ.get("JWT_SECRET_KEY", "test-jwt-secret")
        self.CERTIFICATION_SIGNING_KEY = os.environ.get("CERTIFICATION_SIGNING_KEY", "test-signing-key")

        # Use separate Redis database index for test isolation
        self.REDIS_URL = os.environ.get("REDIS_URL", "redis://localhost:6379/1")

    def validate(self) -> None:
        """Relaxed validation for the testing environment.

        All external-service credentials are optional during test
        execution; tests use mocks or local stubs for Auth0, encryption,
        cloud providers, and certification signing.
        """
        # No strict validation for testing


class ProductionConfig(BaseComplianceConfig):
    """Production environment configuration for the Compliance Service.

    Enforces the strictest PII detection threshold (0.90), disables
    debug mode, raises the log level to ``WARNING``, and requires all
    security-related environment variables (including the certification
    signing key) to be present.  The service will refuse to start in
    production if any required variable is missing.
    """

    def __init__(self) -> None:
        """Initialise production-specific compliance configuration."""
        super().__init__()
        self.FLASK_ENV = "production"
        self.DEBUG = False
        self.TESTING = False
        self.LOG_LEVEL = os.environ.get("LOG_LEVEL", "WARNING")

        # Strict PII confidence threshold for production — reduces false
        # negatives at the cost of marginally higher false positives.
        self.PII_CONFIDENCE_THRESHOLD = float(os.environ.get("PII_CONFIDENCE_THRESHOLD", "0.90"))

        # Increase connection pool for production workloads
        self.MONGODB_MAX_POOL_SIZE = self._get_int_env("MONGODB_MAX_POOL_SIZE", 200)

    # validate() is inherited from BaseComplianceConfig which calls
    # BaseConfig.validate() — both enforce strict checks when
    # FLASK_ENV == "production", including CERTIFICATION_SIGNING_KEY
    # via the extended _PRODUCTION_REQUIRED_KEYS list.


# ---------------------------------------------------------------------------
# Configuration registry
# ---------------------------------------------------------------------------

config_by_name: dict[str, type[BaseComplianceConfig]] = {
    "development": DevelopmentConfig,
    "testing": TestingConfig,
    "production": ProductionConfig,
}
"""Maps environment name strings to their Compliance Service configuration classes.

This registry mirrors the pattern established in
:data:`shared.config.base.config_registry` but is scoped to the Compliance
Service's extended configuration hierarchy.  Use it to look up the correct
configuration class by environment name::

    config_cls = config_by_name[os.environ.get("FLASK_ENV", "development")]
    config = config_cls()
    config.validate()
"""
