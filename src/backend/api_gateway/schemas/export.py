"""Pydantic v2 request/response validation models for export and provisioning domain.

This module defines the data validation schemas for multi-format export operations
and database/cloud storage provisioning. It supports:

- **Multi-format export**: SQL, CSV, JSON, and Apache Parquet output formats.
- **Database provisioning via JDBC**: PostgreSQL, Oracle, SQL Server, SAP HANA.
- **Cloud storage upload**: AWS S3, Azure Blob Storage, GCP Cloud Storage.
- **AES-256 encryption**: Server-side encryption at rest during export.

These models are consumed by the API Gateway export and provisioning route
handlers to validate incoming requests and serialize export operation results.

Typical usage::

    from src.backend.api_gateway.schemas.export import (
        ExportRequest,
        ExportResponse,
        ProvisioningConfig,
        ExportFormat,
        DestinationType,
    )

    # Validate an incoming export request
    request = ExportRequest(
        job_id="job-abc-123",
        format=ExportFormat.CSV,
        destination_type=DestinationType.LOCAL_FILE,
    )

    # Serialize an export result
    response = ExportResponse(
        export_id="exp-xyz-789",
        job_id="job-abc-123",
        status=ExportStatus.COMPLETED,
        format=ExportFormat.CSV,
        destination_type=DestinationType.LOCAL_FILE,
        record_count=50000,
    )
"""

from enum import Enum
from datetime import datetime
from typing import Optional, Dict, Any

from pydantic import BaseModel, Field, field_validator, model_validator, ConfigDict


# ---------------------------------------------------------------------------
# Enumerations
# ---------------------------------------------------------------------------


class ExportFormat(str, Enum):
    """Supported output formats for synthetic data export.

    All members inherit from ``str`` for JSON serialization compatibility
    with Pydantic v2.

    Attributes:
        SQL: SQL INSERT / COPY statement format for direct DB ingestion.
        CSV: Comma-separated values with configurable delimiters.
        JSON: JSON or JSON Lines (JSONL) format.
        PARQUET: Apache Parquet columnar storage format for analytics.
    """

    SQL = "sql"
    CSV = "csv"
    JSON = "json"
    PARQUET = "parquet"


class DestinationType(str, Enum):
    """Export destination type classification.

    Determines the provisioning pathway and which sub-configuration
    (database or cloud) is required in :class:`ProvisioningConfig`.

    Attributes:
        DATABASE: Direct database provisioning via JDBC connectors.
        CLOUD_STORAGE: Cloud object storage (S3, Azure Blob, GCS).
        LOCAL_FILE: Local file system or signed-URL downloadable file.
    """

    DATABASE = "database"
    CLOUD_STORAGE = "cloud_storage"
    LOCAL_FILE = "local_file"


class DatabaseType(str, Enum):
    """Supported target database types for JDBC provisioning.

    Attributes:
        POSTGRESQL: PostgreSQL 12.x through 16.x.
        ORACLE: Oracle Database 19c through 23ai.
        SQLSERVER: Microsoft SQL Server 2019 through 2022.
        SAP_HANA: SAP HANA 2.0 SPS 07+.
    """

    POSTGRESQL = "postgresql"
    ORACLE = "oracle"
    SQLSERVER = "sqlserver"
    SAP_HANA = "sap_hana"


class CloudProvider(str, Enum):
    """Supported cloud storage providers for export upload.

    Attributes:
        AWS_S3: Amazon Web Services S3 Standard API.
        AZURE_BLOB: Microsoft Azure Blob Storage REST API.
        GCP_STORAGE: Google Cloud Platform Cloud Storage v1 API.
    """

    AWS_S3 = "aws_s3"
    AZURE_BLOB = "azure_blob"
    GCP_STORAGE = "gcp_storage"


class ExportStatus(str, Enum):
    """Export operation lifecycle status.

    Tracks the current state of an export job from creation to completion.

    Attributes:
        PENDING: Export job created, awaiting processing.
        IN_PROGRESS: Export operation currently executing.
        COMPLETED: Export finished successfully.
        FAILED: Export encountered an unrecoverable error.
    """

    PENDING = "pending"
    IN_PROGRESS = "in_progress"
    COMPLETED = "completed"
    FAILED = "failed"


# ---------------------------------------------------------------------------
# Configuration Models
# ---------------------------------------------------------------------------


class DatabaseConnectionConfig(BaseModel):
    """Configuration for JDBC database provisioning connections.

    Specifies the connection parameters required to provision generated
    synthetic data directly into a target database via JDBC.  Supports
    batch inserts with configurable batch sizes for optimal throughput.

    Attributes:
        database_type: Target database engine
            (PostgreSQL, Oracle, SQL Server, SAP HANA).
        host: Database server hostname or IP address.
        port: Database server port number (1–65 535).
        database: Target database / catalog name.
        schema_name: Optional target schema within the database.
        username: Database authentication username.
        password: Database authentication password (encrypted in transit).
        jdbc_driver: Optional JDBC driver class name override.
        connection_params: Optional additional JDBC connection parameters.
        batch_size: Number of records per batch insert (100–50 000, default 5 000).
    """

    database_type: DatabaseType = Field(
        ...,
        description="Target database type",
    )
    host: str = Field(
        ...,
        min_length=1,
        description="Database hostname",
    )
    port: int = Field(
        ...,
        gt=0,
        le=65535,
        description="Database port",
    )
    database: str = Field(
        ...,
        min_length=1,
        description="Database name",
    )
    schema_name: Optional[str] = Field(
        default=None,
        description="Target schema",
    )
    username: str = Field(
        ...,
        min_length=1,
        description="Database username",
    )
    password: str = Field(
        ...,
        min_length=1,
        description="Database password",
    )
    jdbc_driver: Optional[str] = Field(
        default=None,
        description="JDBC driver class",
    )
    connection_params: Optional[Dict[str, str]] = Field(
        default=None,
        description="Additional JDBC params",
    )
    batch_size: int = Field(
        default=5000,
        ge=100,
        le=50000,
        description="Batch insert size",
    )

    model_config = ConfigDict(strict=True)


class CloudStorageConfig(BaseModel):
    """Configuration for cloud object storage export destinations.

    Specifies the cloud provider, bucket / container, and authentication
    details needed to upload generated synthetic data.  AES-256 server-side
    encryption is enabled by default in compliance with security requirements.

    Attributes:
        provider: Cloud storage provider (AWS S3, Azure Blob, GCP Storage).
        bucket_name: Target bucket or container name.
        path_prefix: Optional object key prefix / directory path.
        region: Optional cloud provider region identifier.
        credentials: Optional provider-specific authentication credentials.
            For AWS: ``{"access_key": "...", "secret_key": "..."}``.
            For Azure: ``{"connection_string": "..."}``.
            For GCP: ``{"service_account": "..."}``.
        encryption_enabled: Whether to enable AES-256 server-side encryption.
    """

    provider: CloudProvider = Field(
        ...,
        description="Cloud storage provider",
    )
    bucket_name: str = Field(
        ...,
        min_length=1,
        description="Bucket/container name",
    )
    path_prefix: Optional[str] = Field(
        default=None,
        description="Object key prefix/path",
    )
    region: Optional[str] = Field(
        default=None,
        description="Cloud region",
    )
    credentials: Optional[Dict[str, str]] = Field(
        default=None,
        description=(
            "Provider-specific credentials "
            "(access_key, secret_key for AWS; "
            "connection_string for Azure; "
            "service_account for GCP)"
        ),
    )
    encryption_enabled: bool = Field(
        default=True,
        description="Enable AES-256 server-side encryption",
    )

    model_config = ConfigDict(strict=True)


class ProvisioningConfig(BaseModel):
    """Wrapper for mutually-exclusive export provisioning targets.

    Holds either a database connection configuration **or** a cloud storage
    configuration.  Exactly one must be provided — not both, not neither.
    This invariant is enforced by a model-level validator.

    Attributes:
        database_config: Database provisioning configuration for JDBC targets.
        cloud_config: Cloud storage configuration for object storage targets.

    Raises:
        ValueError: If both configurations are provided or if neither is
            provided.
    """

    database_config: Optional[DatabaseConnectionConfig] = Field(
        default=None,
        description="Database provisioning config",
    )
    cloud_config: Optional[CloudStorageConfig] = Field(
        default=None,
        description="Cloud storage config",
    )

    model_config = ConfigDict(strict=True)

    @model_validator(mode="after")
    def validate_exactly_one_config(self) -> "ProvisioningConfig":
        """Ensure exactly one of ``database_config`` or ``cloud_config`` is set.

        Mutual exclusivity guarantees an unambiguous provisioning target,
        preventing conflicting configurations from reaching the Provisioning
        Service.

        Returns:
            The validated :class:`ProvisioningConfig` instance.

        Raises:
            ValueError: If both configurations are provided simultaneously
                or if neither is provided.
        """
        has_database = self.database_config is not None
        has_cloud = self.cloud_config is not None

        if has_database and has_cloud:
            raise ValueError(
                "Only one of 'database_config' or 'cloud_config' may be "
                "provided, not both. Choose either database provisioning "
                "or cloud storage export."
            )
        if not has_database and not has_cloud:
            raise ValueError(
                "Exactly one of 'database_config' or 'cloud_config' must "
                "be provided. A provisioning target is required."
            )
        return self


# ---------------------------------------------------------------------------
# Request / Response Models
# ---------------------------------------------------------------------------


class ExportRequest(BaseModel):
    """Request model for initiating a synthetic data export operation.

    Specifies the source generation job, desired output format, destination
    type, and optional provisioning configuration.  Cross-field validators
    ensure consistency between the destination type, the provisioning
    configuration, and the chosen output format.

    Attributes:
        job_id: Identifier of the source generation job to export.
        format: Output file format (SQL, CSV, JSON, Parquet).
        destination_type: Export target category
            (database, cloud_storage, local_file).
        provisioning_config: Database or cloud storage provisioning details.
        compress: Whether to apply gzip compression to file output.
        encrypt: Whether to apply AES-256 encryption at rest.
        include_schema_ddl: Whether to prepend CREATE TABLE DDL to SQL exports.
        tenant_id: Multi-tenant namespace identifier.

    Raises:
        ValueError: If the destination/config pairing is invalid or the
            format is incompatible with the destination type.
    """

    job_id: str = Field(
        ...,
        min_length=1,
        description="Source generation job ID",
    )
    format: ExportFormat = Field(
        ...,
        description="Output format",
    )
    destination_type: DestinationType = Field(
        ...,
        description="Export destination type",
    )
    provisioning_config: Optional[ProvisioningConfig] = Field(
        default=None,
        description="Database or cloud config",
    )
    compress: bool = Field(
        default=False,
        description="Apply gzip compression to file output",
    )
    encrypt: bool = Field(
        default=True,
        description="Apply AES-256 encryption at rest",
    )
    include_schema_ddl: bool = Field(
        default=False,
        description="Include CREATE TABLE DDL with SQL exports",
    )
    tenant_id: Optional[str] = Field(
        default=None,
        description="Tenant namespace",
    )

    model_config = ConfigDict(strict=True)

    # ------------------------------------------------------------------
    # Field-level validation
    # ------------------------------------------------------------------

    @field_validator("format")
    @classmethod
    def validate_export_format(cls, value: ExportFormat) -> ExportFormat:
        """Validate the export format is a recognised output type.

        Provides an explicit validation gate with a human-readable error
        message.  Format-to-destination compatibility (e.g. PARQUET is not
        valid for DATABASE destinations) is enforced in the model-level
        validator :meth:`validate_destination_config_consistency` because
        it requires access to the ``destination_type`` field.

        Args:
            value: The parsed ``ExportFormat`` enum member.

        Returns:
            The validated ``ExportFormat`` value.

        Raises:
            ValueError: If the value is not a supported export format.
        """
        supported_formats = {member for member in ExportFormat}
        if value not in supported_formats:
            raise ValueError(
                f"Unsupported export format '{value}'. "
                f"Supported formats: {[f.value for f in ExportFormat]}"
            )
        return value

    # ------------------------------------------------------------------
    # Model-level (cross-field) validation
    # ------------------------------------------------------------------

    @model_validator(mode="after")
    def validate_destination_config_consistency(self) -> "ExportRequest":
        """Validate destination type, provisioning config, and format consistency.

        Enforces the following business rules:

        * **DATABASE** destination requires ``provisioning_config`` with a
          non-``None`` ``database_config``.
        * **CLOUD_STORAGE** destination requires ``provisioning_config``
          with a non-``None`` ``cloud_config``.
        * **LOCAL_FILE** destination does **not** require a
          ``provisioning_config`` (it is optional).
        * **PARQUET** format is incompatible with **DATABASE** destinations
          because Parquet is a columnar file format that cannot be directly
          inserted via JDBC.

        Returns:
            The validated :class:`ExportRequest` instance.

        Raises:
            ValueError: If the destination type and provisioning configuration
                are inconsistent, or if the output format is incompatible
                with the chosen destination type.
        """
        dest = self.destination_type
        config = self.provisioning_config
        fmt = self.format

        # ----- Destination ↔ provisioning config consistency -----
        if dest == DestinationType.DATABASE:
            if config is None or config.database_config is None:
                raise ValueError(
                    "Database destination requires 'provisioning_config' with "
                    "'database_config' specified. Provide JDBC connection "
                    "details for the target database."
                )
        elif dest == DestinationType.CLOUD_STORAGE:
            if config is None or config.cloud_config is None:
                raise ValueError(
                    "Cloud storage destination requires 'provisioning_config' "
                    "with 'cloud_config' specified. Provide cloud "
                    "bucket/container details for the target storage."
                )
        # LOCAL_FILE — provisioning_config is optional; no constraint

        # ----- Format ↔ destination compatibility -----
        if dest == DestinationType.DATABASE and fmt == ExportFormat.PARQUET:
            raise ValueError(
                "PARQUET format is not compatible with DATABASE destination. "
                "Use SQL, CSV, or JSON format for database provisioning, or "
                "choose a CLOUD_STORAGE or LOCAL_FILE destination for PARQUET."
            )

        return self


class ExportResponse(BaseModel):
    """Response model representing the state and outcome of an export operation.

    Serialises the current or final state of an export job, including
    status, progress metrics, download location, file size, and temporal
    information.  Used by the API Gateway to return export results to
    callers.

    Attributes:
        export_id: Unique identifier for the export operation.
        job_id: Identifier of the source generation job.
        status: Current export operation lifecycle status.
        format: Output format that was used for the export.
        destination_type: Export destination category that was used.
        download_url: Signed download URL for file-based exports.
        file_size_bytes: Total exported file size in bytes.
        record_count: Total number of records exported.
        error_message: Detailed error information if the export failed.
        started_at: Timestamp when the export operation began.
        completed_at: Timestamp when the export operation finished.
        tenant_id: Multi-tenant namespace identifier.
    """

    export_id: str = Field(
        ...,
        description="Unique export ID",
    )
    job_id: str = Field(
        ...,
        description="Source generation job ID",
    )
    status: ExportStatus = Field(
        ...,
        description="Export status",
    )
    format: ExportFormat = Field(
        ...,
        description="Export format used",
    )
    destination_type: DestinationType = Field(
        ...,
        description="Export destination",
    )
    download_url: Optional[str] = Field(
        default=None,
        description="Signed download URL for file exports",
    )
    file_size_bytes: Optional[int] = Field(
        default=None,
        ge=0,
        description="File size in bytes",
    )
    record_count: int = Field(
        default=0,
        ge=0,
        description="Total records exported",
    )
    error_message: Optional[str] = Field(
        default=None,
        description="Error details if failed",
    )
    started_at: Optional[datetime] = Field(
        default=None,
        description="Export start time",
    )
    completed_at: Optional[datetime] = Field(
        default=None,
        description="Export completion time",
    )
    tenant_id: Optional[str] = Field(
        default=None,
        description="Tenant namespace",
    )

    model_config = ConfigDict(from_attributes=True)
