"""Provisioning Service configuration module.

This module defines :class:`ProvisioningServiceConfig`, the service-specific
configuration class for the Provisioning Service.  It extends
:class:`~shared.config.base.BaseConfig` — inheriting the 12-factor app
foundation (MongoDB, Redis, Auth0, JWT, encryption, observability) — and
adds settings that are unique to data provisioning:

- **JDBC connection pool** — driver paths, pool sizing, and timeout
  controls for PostgreSQL 12-16, Oracle 19c-23ai, SQL Server 2019-2022,
  and SAP HANA 2.0 SPS 07+.
- **Cloud storage credentials** — AWS S3 (with multi-part upload tuning),
  Azure Blob Storage (with managed-identity support), and GCP Cloud
  Storage (with service-account auth).
- **Export behaviour** — batch sizes, file-size limits, temporary
  directory, compression toggle, and overall timeout.
- **Encryption** — AES-256-GCM at-rest encryption toggle and algorithm
  identifier for exported data.

Every value is loaded exclusively from environment variables so that no
credentials or connection strings are hard-coded.

Usage::

    from provisioning_service.config import ProvisioningServiceConfig

    config = ProvisioningServiceConfig()
    config.validate()
    print(config.JDBC_POOL_MAX_SIZE)

See Also:
    :mod:`shared.config.base` for the parent configuration class and the
    ``get_config()`` factory function.
"""

from __future__ import annotations

import logging
import os
from typing import Any

from shared.config.base import BaseConfig


# ---------------------------------------------------------------------------
# Module-level logger — uses the standard library ``logging`` module
# (not the shared structured_logger) to avoid import-time dependency
# ordering issues, since config modules are loaded before the structured
# logging infrastructure is initialised.
# ---------------------------------------------------------------------------

logger: logging.Logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# ProvisioningServiceConfig
# ---------------------------------------------------------------------------


class ProvisioningServiceConfig(BaseConfig):
    """Service-specific configuration for the Provisioning Service.

    Inherits all platform-wide settings from :class:`BaseConfig`
    (``MONGODB_URI``, ``REDIS_URL``, ``AUTH0_DOMAIN``, ``JWT_SECRET_KEY``,
    ``ENCRYPTION_KEY``, ``LOG_LEVEL``, etc.) and adds the provisioning-
    specific groups below.

    **JDBC Connection Pool:**
        Controls the JayDeBeAPI / JDBC driver lifecycle, including the
        filesystem path where JDBC JAR files are stored and connection-
        pool sizing.

    **Database Driver Identifiers:**
        Static class names and JAR filenames for PostgreSQL, Oracle,
        SQL Server, and SAP HANA JDBC drivers.

    **AWS S3:**
        Credentials, region, bucket name, optional S3-compatible endpoint,
        and multi-part upload tuning (threshold and chunk size).

    **Azure Blob Storage:**
        Connection string *or* managed-identity authentication, storage
        account, and container name.

    **GCP Cloud Storage:**
        Project ID, bucket name, and optional service-account key file
        path.

    **Export Behaviour:**
        Batch size (records per write), maximum output file size, temp
        directory, compression toggle, and overall export timeout.

    **Encryption:**
        AES-256-GCM at-rest encryption toggle and algorithm label used
        when writing exported files to cloud storage or local disk.

    Attributes:
        SERVICE_NAME: Logical service identifier (``provisioning-service``).
    """

    # -- Service identification ---------------------------------------------

    SERVICE_NAME: str = "provisioning-service"

    # -- JDBC Connection Pool settings --------------------------------------

    JDBC_DRIVER_PATH: str = os.environ.get("JDBC_DRIVER_PATH", "/opt/jdbc-drivers")
    JDBC_POOL_MIN_SIZE: int = int(os.environ.get("JDBC_POOL_MIN_SIZE", "5"))
    JDBC_POOL_MAX_SIZE: int = int(os.environ.get("JDBC_POOL_MAX_SIZE", "20"))
    JDBC_CONNECTION_TIMEOUT: int = int(os.environ.get("JDBC_CONNECTION_TIMEOUT", "30"))
    JDBC_QUERY_TIMEOUT: int = int(os.environ.get("JDBC_QUERY_TIMEOUT", "300"))

    # -- PostgreSQL JDBC driver ---------------------------------------------

    POSTGRES_JDBC_DRIVER: str = "org.postgresql.Driver"
    POSTGRES_JDBC_JAR: str = "postgresql-42.7.3.jar"

    # -- Oracle JDBC driver -------------------------------------------------

    ORACLE_JDBC_DRIVER: str = "oracle.jdbc.OracleDriver"
    ORACLE_JDBC_JAR: str = "ojdbc11.jar"

    # -- SQL Server JDBC driver ---------------------------------------------

    SQLSERVER_JDBC_DRIVER: str = "com.microsoft.sqlserver.jdbc.SQLServerDriver"
    SQLSERVER_JDBC_JAR: str = "mssql-jdbc-12.4.2.jre11.jar"

    # -- SAP HANA JDBC driver -----------------------------------------------

    HANA_JDBC_DRIVER: str = "com.sap.db.jdbc.Driver"
    HANA_JDBC_JAR: str = "ngdbc.jar"

    # -- AWS S3 settings ----------------------------------------------------

    AWS_ACCESS_KEY_ID: str | None = os.environ.get("AWS_ACCESS_KEY_ID")
    AWS_SECRET_ACCESS_KEY: str | None = os.environ.get("AWS_SECRET_ACCESS_KEY")
    AWS_REGION: str = os.environ.get("AWS_REGION", "us-east-1")
    AWS_S3_BUCKET: str | None = os.environ.get("AWS_S3_BUCKET")
    AWS_S3_ENDPOINT_URL: str | None = os.environ.get("AWS_S3_ENDPOINT_URL")
    AWS_S3_MULTIPART_THRESHOLD: int = int(os.environ.get("AWS_S3_MULTIPART_THRESHOLD", str(8 * 1024 * 1024)))
    AWS_S3_MULTIPART_CHUNKSIZE: int = int(os.environ.get("AWS_S3_MULTIPART_CHUNKSIZE", str(8 * 1024 * 1024)))

    # -- Azure Blob Storage settings ----------------------------------------

    AZURE_STORAGE_CONNECTION_STRING: str | None = os.environ.get("AZURE_STORAGE_CONNECTION_STRING")
    AZURE_STORAGE_ACCOUNT_NAME: str | None = os.environ.get("AZURE_STORAGE_ACCOUNT_NAME")
    AZURE_STORAGE_CONTAINER: str | None = os.environ.get("AZURE_STORAGE_CONTAINER")
    AZURE_USE_MANAGED_IDENTITY: bool = os.environ.get("AZURE_USE_MANAGED_IDENTITY", "false").lower() == "true"

    # -- GCP Cloud Storage settings -----------------------------------------

    GCP_PROJECT_ID: str | None = os.environ.get("GCP_PROJECT_ID")
    GCP_STORAGE_BUCKET: str | None = os.environ.get("GCP_STORAGE_BUCKET")
    GCP_SERVICE_ACCOUNT_KEY_PATH: str | None = os.environ.get("GCP_SERVICE_ACCOUNT_KEY_PATH")

    # -- Export settings ----------------------------------------------------

    EXPORT_BATCH_SIZE: int = int(os.environ.get("EXPORT_BATCH_SIZE", "10000"))
    EXPORT_MAX_FILE_SIZE_MB: int = int(os.environ.get("EXPORT_MAX_FILE_SIZE_MB", "500"))
    EXPORT_TEMP_DIR: str = os.environ.get(
        "EXPORT_TEMP_DIR",
        "/tmp/provisioning-exports",  # noqa: S108
    )
    EXPORT_COMPRESSION_ENABLED: bool = os.environ.get("EXPORT_COMPRESSION_ENABLED", "true").lower() == "true"
    EXPORT_TIMEOUT: int = int(os.environ.get("EXPORT_TIMEOUT", "3600"))

    # -- Encryption settings ------------------------------------------------

    EXPORT_ENCRYPTION_ENABLED: bool = os.environ.get("EXPORT_ENCRYPTION_ENABLED", "true").lower() == "true"
    EXPORT_ENCRYPTION_ALGORITHM: str = "AES-256-GCM"

    # -----------------------------------------------------------------------
    # Initialiser — re-reads environment at instance creation time
    # -----------------------------------------------------------------------

    def __init__(self) -> None:
        """Initialise the Provisioning Service configuration.

        Calls the parent :meth:`BaseConfig.__init__` to load all shared
        platform settings (MongoDB, Redis, Auth0, JWT, CORS, etc.) and
        then re-reads every provisioning-specific environment variable so
        that instances created *after* module import still pick up the
        latest values.  This is critical for test isolation and container
        orchestration where environment variables may be injected after
        import time.
        """
        super().__init__()

        # Override the service name to identify this microservice.
        self.SERVICE_NAME = "provisioning-service"

        # -- JDBC connection pool -------------------------------------------
        self.JDBC_DRIVER_PATH = os.environ.get("JDBC_DRIVER_PATH", "/opt/jdbc-drivers")
        self.JDBC_POOL_MIN_SIZE = self._get_int_env("JDBC_POOL_MIN_SIZE", 5)
        self.JDBC_POOL_MAX_SIZE = self._get_int_env("JDBC_POOL_MAX_SIZE", 20)
        self.JDBC_CONNECTION_TIMEOUT = self._get_int_env("JDBC_CONNECTION_TIMEOUT", 30)
        self.JDBC_QUERY_TIMEOUT = self._get_int_env("JDBC_QUERY_TIMEOUT", 300)

        # -- JDBC driver identifiers (static, not env-driven) ---------------
        self.POSTGRES_JDBC_DRIVER = "org.postgresql.Driver"
        self.POSTGRES_JDBC_JAR = "postgresql-42.7.3.jar"

        self.ORACLE_JDBC_DRIVER = "oracle.jdbc.OracleDriver"
        self.ORACLE_JDBC_JAR = "ojdbc11.jar"

        self.SQLSERVER_JDBC_DRIVER = "com.microsoft.sqlserver.jdbc.SQLServerDriver"
        self.SQLSERVER_JDBC_JAR = "mssql-jdbc-12.4.2.jre11.jar"

        self.HANA_JDBC_DRIVER = "com.sap.db.jdbc.Driver"
        self.HANA_JDBC_JAR = "ngdbc.jar"

        # -- AWS S3 ---------------------------------------------------------
        self.AWS_ACCESS_KEY_ID = os.environ.get("AWS_ACCESS_KEY_ID")
        self.AWS_SECRET_ACCESS_KEY = os.environ.get("AWS_SECRET_ACCESS_KEY")
        self.AWS_REGION = os.environ.get("AWS_REGION", "us-east-1")
        self.AWS_S3_BUCKET = os.environ.get("AWS_S3_BUCKET")
        self.AWS_S3_ENDPOINT_URL = os.environ.get("AWS_S3_ENDPOINT_URL")
        self.AWS_S3_MULTIPART_THRESHOLD = self._get_int_env("AWS_S3_MULTIPART_THRESHOLD", 8 * 1024 * 1024)
        self.AWS_S3_MULTIPART_CHUNKSIZE = self._get_int_env("AWS_S3_MULTIPART_CHUNKSIZE", 8 * 1024 * 1024)

        # -- Azure Blob Storage ---------------------------------------------
        self.AZURE_STORAGE_CONNECTION_STRING = os.environ.get("AZURE_STORAGE_CONNECTION_STRING")
        self.AZURE_STORAGE_ACCOUNT_NAME = os.environ.get("AZURE_STORAGE_ACCOUNT_NAME")
        self.AZURE_STORAGE_CONTAINER = os.environ.get("AZURE_STORAGE_CONTAINER")
        self.AZURE_USE_MANAGED_IDENTITY = self._get_bool_env("AZURE_USE_MANAGED_IDENTITY", False)

        # -- GCP Cloud Storage ----------------------------------------------
        self.GCP_PROJECT_ID = os.environ.get("GCP_PROJECT_ID")
        self.GCP_STORAGE_BUCKET = os.environ.get("GCP_STORAGE_BUCKET")
        self.GCP_SERVICE_ACCOUNT_KEY_PATH = os.environ.get("GCP_SERVICE_ACCOUNT_KEY_PATH")

        # -- Export settings ------------------------------------------------
        self.EXPORT_BATCH_SIZE = self._get_int_env("EXPORT_BATCH_SIZE", 10000)
        self.EXPORT_MAX_FILE_SIZE_MB = self._get_int_env("EXPORT_MAX_FILE_SIZE_MB", 500)
        self.EXPORT_TEMP_DIR = os.environ.get(
            "EXPORT_TEMP_DIR",
            "/tmp/provisioning-exports",  # noqa: S108
        )
        self.EXPORT_COMPRESSION_ENABLED = self._get_bool_env("EXPORT_COMPRESSION_ENABLED", True)
        self.EXPORT_TIMEOUT = self._get_int_env("EXPORT_TIMEOUT", 3600)

        # -- Encryption settings --------------------------------------------
        self.EXPORT_ENCRYPTION_ENABLED = self._get_bool_env("EXPORT_ENCRYPTION_ENABLED", True)
        self.EXPORT_ENCRYPTION_ALGORITHM = "AES-256-GCM"

    # -----------------------------------------------------------------------
    # Validation
    # -----------------------------------------------------------------------

    def validate(self) -> None:
        """Validate provisioning-specific configuration requirements.

        Performs the following checks on top of the parent
        :meth:`BaseConfig.validate` checks:

        1. **JDBC driver directory** — Verifies that
           :attr:`JDBC_DRIVER_PATH` points to an existing directory on
           the filesystem.  In non-production environments a warning is
           logged instead of raising an error, because developers may not
           have JDBC drivers installed locally.

        2. **Cloud credentials (production only)** — Warns if *none* of
           the three cloud providers (AWS, Azure, GCP) have credentials
           configured.  At least one provider should be available for
           export functionality to work in production.

        Raises:
            ConfigurationError: Propagated from the parent class when
                production-required base variables are missing.
        """
        # Run the shared platform validation first (checks AUTH0, JWT,
        # ENCRYPTION_KEY, etc. when FLASK_ENV == "production").
        super().validate()

        # -- JDBC driver path validation ------------------------------------
        if not os.path.isdir(self.JDBC_DRIVER_PATH):
            if self.FLASK_ENV == "production":
                logger.warning(
                    "JDBC driver directory '%s' does not exist. "
                    "Database provisioning connectors will not be "
                    "available until the directory is created and "
                    "populated with the required JAR files.",
                    self.JDBC_DRIVER_PATH,
                )
            else:
                logger.warning(
                    "JDBC driver directory '%s' does not exist. "
                    "This is acceptable for local development but "
                    "must be resolved before deploying to production.",
                    self.JDBC_DRIVER_PATH,
                )

        # -- Cloud credentials presence check (production only) -------------
        if self.FLASK_ENV == "production":
            aws_configured: bool = bool(self.AWS_ACCESS_KEY_ID and self.AWS_SECRET_ACCESS_KEY)
            azure_configured: bool = bool(self.AZURE_STORAGE_CONNECTION_STRING or self.AZURE_USE_MANAGED_IDENTITY)
            gcp_configured: bool = bool(
                self.GCP_PROJECT_ID
                and (self.GCP_SERVICE_ACCOUNT_KEY_PATH or os.environ.get("GOOGLE_APPLICATION_CREDENTIALS"))
            )

            if not any([aws_configured, azure_configured, gcp_configured]):
                logger.warning(
                    "No cloud storage credentials are configured for the "
                    "Provisioning Service. At least one cloud provider "
                    "(AWS S3, Azure Blob Storage, or GCP Cloud Storage) "
                    "should be configured for production export "
                    "functionality. Set the appropriate environment "
                    "variables: AWS_ACCESS_KEY_ID/AWS_SECRET_ACCESS_KEY, "
                    "AZURE_STORAGE_CONNECTION_STRING or "
                    "AZURE_USE_MANAGED_IDENTITY, or GCP_PROJECT_ID/"
                    "GCP_SERVICE_ACCOUNT_KEY_PATH.",
                )

            # Warn if encryption key is empty when export encryption is on
            if self.EXPORT_ENCRYPTION_ENABLED and not self.ENCRYPTION_KEY:
                logger.warning(
                    "Export encryption is enabled "
                    "(EXPORT_ENCRYPTION_ENABLED=true) but no "
                    "ENCRYPTION_KEY is set. Exported files will not "
                    "be encrypted at rest until a valid AES-256 key "
                    "is provided via the ENCRYPTION_KEY environment "
                    "variable.",
                )

    # -----------------------------------------------------------------------
    # Safe serialisation
    # -----------------------------------------------------------------------

    # Additional keys whose values must be masked in log-safe output,
    # extending the parent's _SENSITIVE_KEYS set.
    _PROVISIONING_SENSITIVE_KEYS: frozenset[str] = frozenset(
        {
            "AWS_SECRET_ACCESS_KEY",
            "AZURE_STORAGE_CONNECTION_STRING",
            "GCP_SERVICE_ACCOUNT_KEY_PATH",
        }
    )

    def to_safe_dict(self) -> dict[str, Any]:
        """Return a dictionary of configuration values safe for logging.

        Extends the parent :meth:`BaseConfig.to_safe_dict` by redacting
        additional provisioning-specific secrets:

        - ``AWS_SECRET_ACCESS_KEY``
        - ``AZURE_STORAGE_CONNECTION_STRING``
        - ``GCP_SERVICE_ACCOUNT_KEY_PATH``

        Returns:
            A ``dict[str, Any]`` mapping configuration attribute names
            to their (possibly redacted) values.  All sensitive values
            are replaced with ``'***REDACTED***'``.
        """
        safe: dict[str, Any] = super().to_safe_dict()

        # Mask provisioning-specific sensitive values that were not
        # already handled by the parent's _SENSITIVE_KEYS set.
        for key in self._PROVISIONING_SENSITIVE_KEYS:
            if safe.get(key):
                safe[key] = self._REDACTED

        return safe
