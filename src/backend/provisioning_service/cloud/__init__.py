"""Cloud storage providers package for the Provisioning Service.

This package implements the Abstract Factory pattern for cloud storage
providers, enabling the Synthetic ERP Data Generation Platform to export
generated datasets to multiple cloud object stores through a uniform
interface.

Supported Cloud Storage Providers:
    * **AWS S3** (:class:`S3Provider`) — Amazon S3 with multi-part upload,
      AES-256 server-side encryption (SSE-S3 / SSE-KMS), and S3-compatible
      endpoint support (MinIO, LocalStack) via ``boto3``.
    * **Azure Blob Storage** (:class:`AzureBlobProvider`) — Azure Blob
      Storage with managed identity (``DefaultAzureCredential``) and
      connection-string authentication via ``azure-storage-blob`` and
      ``azure-identity``.
    * **GCP Cloud Storage** (:class:`GCSProvider`) — Google Cloud Storage
      with service-account and Application Default Credentials (ADC) via
      ``google-cloud-storage``.

All providers extend :class:`BaseCloudProvider`, which defines a consistent
interface for upload, upload_stream, download, download_stream, list_objects,
delete, delete_many, exists, get_metadata, and health_check operations.
Providers also support streaming uploads for large dataset export and
integrate with the :mod:`provisioning_service.exporters.file_exporter`
module for cloud-based delivery of synthetic ERP data.

Architecture:
    The :data:`PROVIDER_REGISTRY` dictionary maps human-friendly provider
    type strings (e.g. ``"s3"``, ``"azure"``, ``"gcs"``) to their
    corresponding provider classes.  The :func:`get_cloud_provider` factory
    function instantiates the appropriate provider based on the target cloud
    platform, enabling runtime provider selection without hard-coding
    cloud-specific imports at the call site.

Usage::

    from provisioning_service.cloud import get_cloud_provider

    # Instantiate an AWS S3 provider
    provider = get_cloud_provider('s3', {
        'bucket_name': 'my-bucket',
        'aws_region': 'us-east-1',
        'aws_access_key_id': 'AKID...',
        'aws_secret_access_key': 'SECRET...',
    })
    result = provider.upload('/tmp/data.csv', 'exports/data.csv')

    # Instantiate an Azure Blob provider
    provider = get_cloud_provider('azure', {
        'container_name': 'synthetic-exports',
        'connection_string': 'DefaultEndpointsProtocol=https;...',
    })

    # Instantiate a GCP Cloud Storage provider
    provider = get_cloud_provider('gcs', {
        'bucket_name': 'erp-exports',
        'project_id': 'my-project',
    })

    # List all supported provider type strings
    from provisioning_service.cloud import get_supported_providers
    print(get_supported_providers())
    # ['aws', 'aws_s3', 'azure', 'azure_blob', 'azure_storage', 'gcp', 'gcp_gcs', 'gcs', 's3']
"""

from __future__ import annotations

from typing import Any, Dict, List, Type

from provisioning_service.cloud.azure_blob_provider import AzureBlobProvider
from provisioning_service.cloud.base import BaseCloudProvider
from provisioning_service.cloud.gcs_provider import GCSProvider
from provisioning_service.cloud.s3_provider import S3Provider

# ---------------------------------------------------------------------------
# Provider Registry
# ---------------------------------------------------------------------------
# Maps human-friendly provider type strings to their corresponding concrete
# provider classes.  Multiple aliases are supported for each provider so that
# callers can use short names ("s3"), cloud-prefixed names ("aws_s3"), or
# vendor-level names ("aws") interchangeably.
# ---------------------------------------------------------------------------

PROVIDER_REGISTRY: Dict[str, Type[BaseCloudProvider]] = {
    # AWS S3 -------------------------------------------------------------------
    "aws_s3": S3Provider,
    "s3": S3Provider,
    "aws": S3Provider,
    # Azure Blob Storage -------------------------------------------------------
    "azure_blob": AzureBlobProvider,
    "azure": AzureBlobProvider,
    "azure_storage": AzureBlobProvider,
    # GCP Cloud Storage --------------------------------------------------------
    "gcp_gcs": GCSProvider,
    "gcs": GCSProvider,
    "gcp": GCSProvider,
}


def get_cloud_provider(
    provider_type: str,
    config: Dict[str, Any],
) -> BaseCloudProvider:
    """Instantiate the appropriate cloud storage provider for a given type.

    This is the primary entry point for obtaining a configured cloud storage
    provider instance.  It normalises the *provider_type* string, looks it up
    in :data:`PROVIDER_REGISTRY`, and returns a fully-initialised provider
    ready for upload / download operations.

    Args:
        provider_type: A string identifying the target cloud platform.
            Accepted values (case-insensitive, whitespace-trimmed):

            * ``"aws_s3"`` / ``"s3"`` / ``"aws"`` — AWS S3
            * ``"azure_blob"`` / ``"azure"`` / ``"azure_storage"`` — Azure
              Blob Storage
            * ``"gcp_gcs"`` / ``"gcs"`` / ``"gcp"`` — GCP Cloud Storage

        config: Provider-specific configuration dictionary.  Each concrete
            provider documents its own required and optional keys.  Common
            keys recognised by :class:`BaseCloudProvider` include
            ``bucket_name``, ``prefix``, ``encryption_enabled``,
            ``max_retries``, and ``retry_delay``.

    Returns:
        A fully-initialised :class:`BaseCloudProvider` subclass instance
        corresponding to the requested *provider_type*.

    Raises:
        ValueError: If *provider_type* is empty, ``None``, or does not
            match any entry in :data:`PROVIDER_REGISTRY`.

    Examples::

        # AWS S3
        provider = get_cloud_provider('s3', {
            'bucket_name': 'my-bucket',
            'aws_region': 'us-east-1',
            'aws_access_key_id': 'AKID...',
            'aws_secret_access_key': 'SECRET...',
        })
        provider.upload('/tmp/data.csv', 'exports/data.csv')

        # Azure Blob Storage
        provider = get_cloud_provider('azure', {
            'container_name': 'synthetic-exports',
            'connection_string': 'DefaultEndpointsProtocol=https;...',
        })

        # GCP Cloud Storage
        provider = get_cloud_provider('gcs', {
            'bucket_name': 'erp-exports',
            'project_id': 'my-project',
        })
    """
    if not provider_type:
        supported = sorted(PROVIDER_REGISTRY.keys())
        raise ValueError(
            "provider_type must be a non-empty string. "
            f"Supported provider types: {supported}"
        )

    normalised = provider_type.strip().lower()

    provider_class = PROVIDER_REGISTRY.get(normalised)
    if provider_class is None:
        supported = sorted(PROVIDER_REGISTRY.keys())
        raise ValueError(
            f"Unsupported cloud provider type: '{provider_type}' "
            f"(normalised: '{normalised}'). "
            f"Supported provider types: {supported}"
        )

    return provider_class(config)


def get_supported_providers() -> List[str]:
    """Return a sorted list of unique supported cloud provider type names.

    The returned list includes all registered aliases (e.g. ``"s3"``,
    ``"aws_s3"``, ``"aws"``).  Useful for populating UI dropdowns,
    validating API request payloads, and generating documentation.

    Returns:
        A sorted list of all accepted *provider_type* strings.

    Example::

        >>> from provisioning_service.cloud import get_supported_providers
        >>> get_supported_providers()
        ['aws', 'aws_s3', 'azure', 'azure_blob', 'azure_storage',
         'gcp', 'gcp_gcs', 'gcs', 's3']
    """
    return sorted(PROVIDER_REGISTRY.keys())


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

__all__: List[str] = [
    "BaseCloudProvider",
    "S3Provider",
    "AzureBlobProvider",
    "GCSProvider",
    "PROVIDER_REGISTRY",
    "get_cloud_provider",
    "get_supported_providers",
]
