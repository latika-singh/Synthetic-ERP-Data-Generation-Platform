"""Azure Blob Storage cloud provider for the Provisioning Service.

This module implements :class:`AzureBlobProvider`, a concrete cloud storage
provider that exports synthetic ERP data to Azure Blob Storage containers.
It extends :class:`BaseCloudProvider` with Azure-specific functionality
including managed identity authentication via :class:`DefaultAzureCredential`,
block blob uploads with automatic chunking, SAS URL generation for temporary
external access, and container lifecycle management.

Authentication Methods:
    * **Connection string** — For local development and non-Azure environments.
      Set ``connection_string`` in config or ``AZURE_STORAGE_CONNECTION_STRING``
      environment variable.
    * **Managed identity** — For production Azure deployments.  Enable via
      ``use_managed_identity=True`` in config or set the
      ``AZURE_USE_MANAGED_IDENTITY`` environment variable to ``"true"``.
      Uses :class:`DefaultAzureCredential` which automatically tries
      environment variables, managed identity, Azure CLI, Visual Studio Code,
      and interactive browser authentication in sequence.

Encryption:
    Azure Storage Service Encryption (SSE) provides AES-256 encryption at rest
    for all blob data by default.  The ``encryption_enabled`` config flag
    controls whether the provider reports encryption status in result
    dictionaries and metadata responses.

Usage::

    from provisioning_service.cloud.azure_blob_provider import AzureBlobProvider

    config = {
        "container_name": "synthetic-exports",
        "connection_string": "DefaultEndpointsProtocol=https;...",
        "prefix": "tenant-42/",
        "encryption_enabled": True,
    }

    with AzureBlobProvider(config) as provider:
        result = provider.upload("/tmp/output.parquet", "datasets/run-123.parquet")
        print(result)

Design Patterns:
    * **Strategy** — Encapsulates Azure Blob-specific upload / download
      algorithms behind the uniform :class:`BaseCloudProvider` interface.
    * **Template Method** — ``__init__`` orchestrates a fixed setup sequence
      then delegates to :meth:`_initialize_client` for Azure client creation.
    * **12-Factor App** — All configuration is read from a config dict with
      environment-variable fallbacks for secrets and connection strings.
"""

from __future__ import annotations

import io
import os
from collections.abc import Generator  # noqa: F401
from datetime import UTC, datetime, timedelta
from typing import Any, BinaryIO

from azure.core.exceptions import (
    AzureError,
    ResourceExistsError,
    ResourceNotFoundError,
)
from azure.identity import DefaultAzureCredential
from azure.storage.blob import (
    BlobClient,  # noqa: F401 — referenced in docstrings
    BlobSasPermissions,
    BlobServiceClient,
    BlobType,  # noqa: F401 — used for type reference in metadata
    ContainerClient,
    ContentSettings,
    StandardBlobTier,  # noqa: F401 — available for tier management
    generate_blob_sas,
)

from provisioning_service.cloud.base import BaseCloudProvider
from shared.logging.structured_logger import get_logger


# ---------------------------------------------------------------------------
# Module-level logger for non-instance contexts (e.g. module-load errors).
# ---------------------------------------------------------------------------
_logger = get_logger(__name__)


class AzureBlobProvider(BaseCloudProvider):
    """Azure Blob Storage provider for exporting synthetic ERP data.

    Concrete implementation of :class:`BaseCloudProvider` that uses the
    Azure Blob Storage Python SDK (``azure-storage-blob >=12.0.0``) to
    upload, download, list, and delete blobs within an Azure Storage
    container.  Supports both connection-string and managed-identity
    authentication, automatic block staging for large files, streaming
    uploads for on-the-fly generated data, and SAS URL generation for
    temporary external access.

    All blob operations leverage the base class's
    :meth:`~BaseCloudProvider._execute_with_retry` for exponential-backoff
    retry on transient Azure API failures, and
    :meth:`~BaseCloudProvider._build_remote_key` for transparent tenant-prefix
    isolation.

    Attributes:
        PROVIDER_NAME: Identifier string for this cloud provider.
        DEFAULT_MAX_BLOCK_SIZE: Default maximum block size for staged
            uploads (4 MiB).
        DEFAULT_MAX_SINGLE_PUT_SIZE: Threshold below which a single PUT
            request is used instead of staged blocks (64 MiB).
        MAX_BLOCK_COUNT: Azure platform limit on blocks per blob (50 000).
        SUPPORTED_BLOB_TYPES: Blob types supported by this provider.

    Args:
        config: Provider configuration dictionary.  Recognised keys:

            * ``container_name`` (str) — Target Azure Storage container.
              Also accepted as ``bucket_name`` for base class compatibility.
            * ``connection_string`` (str, optional) — Azure Storage
              connection string.  Falls back to
              ``AZURE_STORAGE_CONNECTION_STRING`` env var.
            * ``account_name`` (str, optional) — Storage account name.
              Falls back to ``AZURE_STORAGE_ACCOUNT_NAME``.
            * ``account_key`` (str, optional) — Storage account key.
              Falls back to ``AZURE_STORAGE_ACCOUNT_KEY``.
            * ``use_managed_identity`` (bool, optional) — Use
              :class:`DefaultAzureCredential`.  Defaults to ``False``.
            * ``prefix`` (str, optional) — Key prefix for tenant isolation.
            * ``max_block_size`` (int, optional) — Per-block upload size.
            * ``max_single_put_size`` (int, optional) — Threshold for
              single-request uploads.
            * ``encryption_enabled`` (bool, optional) — AES-256 reporting
              flag (defaults to ``True``).
            * ``max_retries`` (int, optional) — Retry attempts (default 3).
            * ``retry_delay`` (float, optional) — Base retry delay in
              seconds (default 1.0).

    Raises:
        ValueError: If neither ``connection_string`` nor
            ``use_managed_identity`` is provided and no environment
            fallback is available.

    Example::

        provider = AzureBlobProvider({
            "container_name": "my-data",
            "use_managed_identity": True,
            "account_name": "mystorageaccount",
        })
        result = provider.upload("/tmp/data.csv", "exports/data.csv")
    """

    # ------------------------------------------------------------------
    # Class-level constants
    # ------------------------------------------------------------------

    PROVIDER_NAME: str = "azure_blob"
    """Identifier string used in result dictionaries and log events."""

    DEFAULT_MAX_BLOCK_SIZE: int = 4 * 1024 * 1024  # 4 MiB per block
    """Default maximum block size for staged (multi-part) uploads."""

    DEFAULT_MAX_SINGLE_PUT_SIZE: int = 64 * 1024 * 1024  # 64 MiB
    """Blobs smaller than this threshold are uploaded in a single PUT."""

    MAX_BLOCK_COUNT: int = 50_000
    """Azure platform limit: maximum number of blocks per block blob."""

    SUPPORTED_BLOB_TYPES: list[str] = ["BlockBlob", "AppendBlob"]
    """Blob types supported by this provider."""

    # ------------------------------------------------------------------
    # Initialisation
    # ------------------------------------------------------------------

    def __init__(self, config: dict[str, Any]) -> None:
        """Initialise the Azure Blob Storage provider.

        Extracts Azure-specific settings from *config* (with environment-
        variable fallbacks) **before** delegating to the base class
        ``__init__``, which in turn calls :meth:`_initialize_client`.

        Args:
            config: Provider configuration dictionary.  See class-level
                docstring for all recognised keys.

        Raises:
            ValueError: If required authentication parameters are missing
                or the configuration is invalid.
        """
        # ---- Validate config is a dict ------------------------------------
        if not isinstance(config, dict):
            raise ValueError(
                "AzureBlobProvider requires a non-null configuration "
                f"dictionary.  Received: {type(config).__name__!r}"
            )

        # ---- Azure-specific config extraction ----------------------------
        # These must be set BEFORE super().__init__() because the base
        # class calls _initialize_client() at the end of its __init__.
        self._connection_string: str | None = (
            config.get("connection_string")
            or os.environ.get("AZURE_STORAGE_CONNECTION_STRING")
        )
        self._account_name: str | None = (
            config.get("account_name")
            or os.environ.get("AZURE_STORAGE_ACCOUNT_NAME")
        )
        self._account_key: str | None = (
            config.get("account_key")
            or os.environ.get("AZURE_STORAGE_ACCOUNT_KEY")
        )
        self._use_managed_identity: bool = bool(
            config.get(
                "use_managed_identity",
                os.environ.get(
                    "AZURE_USE_MANAGED_IDENTITY", "false"
                ).lower() == "true",
            )
        )
        self._max_block_size_cfg: int = int(
            config.get("max_block_size", self.DEFAULT_MAX_BLOCK_SIZE)
        )
        self._max_single_put_size_cfg: int = int(
            config.get("max_single_put_size", self.DEFAULT_MAX_SINGLE_PUT_SIZE)
        )

        # Map Azure "container_name" → base class "bucket_name" if needed.
        if "container_name" in config and "bucket_name" not in config:
            config["bucket_name"] = config["container_name"]
        elif "bucket_name" not in config and "container_name" not in config:
            env_container = os.environ.get("AZURE_STORAGE_CONTAINER", "")
            config.setdefault("bucket_name", env_container)

        # Placeholders for Azure SDK clients — populated by
        # _initialize_client() which is called from super().__init__().
        self._blob_service_client: BlobServiceClient | None = None
        self._container_client: ContainerClient | None = None

        # ---- Delegate to BaseCloudProvider --------------------------------
        # Stores config, creates logger, initialises metrics, and calls
        # _initialize_client().
        super().__init__(config)

    # ------------------------------------------------------------------
    # Internal helpers for type-safe access to nullable SDK objects
    # ------------------------------------------------------------------

    @property
    def _active_container_client(self) -> ContainerClient:
        """Return the container client, raising if not initialised.

        This property narrows the ``ContainerClient | None`` type to a
        plain ``ContainerClient``, satisfying ``mypy``'s strict
        ``union-attr`` checks and providing a clear error message when
        the provider has not been connected.

        Returns:
            The initialised :class:`ContainerClient`.

        Raises:
            RuntimeError: If :meth:`_initialize_client` has not run yet.
        """
        if self._container_client is None:
            raise RuntimeError(
                "Azure container client has not been initialised — "
                "call connect() first."
            )
        return self._container_client

    @property
    def _active_service_client(self) -> BlobServiceClient:
        """Return the blob service client, raising if not initialised.

        Returns:
            The initialised :class:`BlobServiceClient`.

        Raises:
            RuntimeError: If :meth:`_initialize_client` has not run yet.
        """
        if self._blob_service_client is None:
            raise RuntimeError(
                "Azure blob service client has not been initialised — "
                "call connect() first."
            )
        return self._blob_service_client

    # ------------------------------------------------------------------
    # Abstract method implementations
    # ------------------------------------------------------------------

    def _initialize_client(self) -> None:
        """Create and configure the Azure Blob Storage SDK clients.

        Determines the authentication method (managed identity vs.
        connection string) and creates a :class:`BlobServiceClient`.
        Then obtains (or creates) the target container via
        :class:`ContainerClient`.

        Called automatically by :meth:`BaseCloudProvider.__init__`.

        Raises:
            ValueError: If no valid authentication method can be resolved.
            RuntimeError: If the SDK client cannot connect to the storage
                account or the container cannot be accessed.
        """
        # Validate that the container/bucket name is present in config.
        self._validate_config(["bucket_name"])

        # Build BlobServiceClient based on authentication method -----------
        if self._use_managed_identity:
            if not self._account_name:
                raise ValueError(
                    "AzureBlobProvider: 'account_name' (or "
                    "AZURE_STORAGE_ACCOUNT_NAME env var) is required when "
                    "use_managed_identity is True."
                )
            account_url = (
                f"https://{self._account_name}.blob.core.windows.net"
            )
            credential = DefaultAzureCredential()
            self._blob_service_client = BlobServiceClient(
                account_url=account_url,
                credential=credential,
                max_block_size=self._max_block_size_cfg,
                max_single_put_size=self._max_single_put_size_cfg,
            )
            self._logger.info(
                "azure_client_init_managed_identity",
                account_name=self._account_name,
                account_url=account_url,
            )
        elif self._connection_string:
            self._blob_service_client = (
                BlobServiceClient.from_connection_string(
                    conn_str=self._connection_string,
                    max_block_size=self._max_block_size_cfg,
                    max_single_put_size=self._max_single_put_size_cfg,
                )
            )
            # Extract account name from the service client when not
            # explicitly provided in config.
            if not self._account_name and self._blob_service_client:
                self._account_name = (
                    self._blob_service_client.account_name
                )
            self._logger.info(
                "azure_client_init_connection_string",
                account_name=self._account_name or "from_conn_str",
            )
        else:
            raise ValueError(
                "AzureBlobProvider requires either 'connection_string' "
                "(or AZURE_STORAGE_CONNECTION_STRING env var) or "
                "'use_managed_identity=True' with an 'account_name'. "
                "Neither was provided."
            )

        # Obtain / create the target container -----------------------------
        container_name = self._bucket_name
        self._container_client = (
            self._blob_service_client.get_container_client(container_name)
        )

        try:
            self._container_client.create_container()
            self._logger.info(
                "azure_container_created",
                container=container_name,
            )
        except ResourceExistsError:
            # Container already exists — expected in normal operation.
            self._logger.debug(
                "azure_container_exists",
                container=container_name,
            )
        except AzureError as exc:
            # Non-fatal: container may have been created externally or
            # the account may lack create permissions while still having
            # read/write access to an existing container.
            self._logger.warning(
                "azure_container_create_skipped",
                container=container_name,
                error=str(exc),
            )

        self._initialized = True
        self._logger.info(
            "azure_blob_provider_initialized",
            container=container_name,
            encryption_enabled=self._encryption_enabled,
            max_block_size=self._max_block_size_cfg,
            max_single_put_size=self._max_single_put_size_cfg,
        )

    # ------------------------------------------------------------------
    # Upload operations
    # ------------------------------------------------------------------

    def upload(
        self,
        local_path: str,
        remote_key: str,
        metadata: dict[str, str] | None = None,
        content_type: str | None = None,
    ) -> dict[str, Any]:
        """Upload a local file to Azure Blob Storage.

        Uses the Azure SDK's :meth:`BlobClient.upload_blob` which
        automatically handles block staging for files exceeding
        ``max_single_put_size``.  Large files are chunked into blocks of
        ``max_block_size`` and uploaded with up to 4 concurrent threads.

        Args:
            local_path: Absolute or relative path to the file on disk.
            remote_key: Blob name in the container.  The configured tenant
                prefix is automatically prepended via
                :meth:`~BaseCloudProvider._build_remote_key`.
            metadata: Optional user-defined key-value metadata to attach
                to the uploaded blob.
            content_type: MIME content type override.  Auto-detected from
                the file extension when ``None``.

        Returns:
            A dictionary describing the uploaded blob::

                {
                    "provider": "azure_blob",
                    "container": str,
                    "blob_name": str,
                    "size_bytes": int,
                    "etag": str,
                    "content_md5": str | None,
                    "url": str,
                    "last_modified": str,
                    "encryption": str,
                    "content_type": str,
                }

        Raises:
            FileNotFoundError: If *local_path* does not exist on disk.
            RuntimeError: If the upload fails after all retry attempts.
        """
        self.ensure_initialized()

        # Validate source file and determine size.
        file_size = self._get_file_size(local_path)

        # Resolve final blob name with tenant prefix.
        blob_name = self._build_remote_key(remote_key)

        # Resolve content type from extension or explicit override.
        resolved_content_type = content_type or self._detect_content_type(
            local_path
        )
        content_settings = ContentSettings(
            content_type=resolved_content_type,
        )

        blob_client = self._active_container_client.get_blob_client(blob_name)

        def _do_upload() -> None:
            """Inner upload callable for retry wrapper."""
            with open(local_path, "rb") as file_data:
                blob_client.upload_blob(
                    data=file_data,
                    overwrite=True,
                    content_settings=content_settings,
                    metadata=metadata,
                    max_concurrency=4,
                )

        self._execute_with_retry(_do_upload)

        # Retrieve blob properties for the result dictionary.
        props = blob_client.get_blob_properties()

        # Update operational metrics.
        self._total_bytes_uploaded += file_size
        self._total_operations += 1

        # Build the blob URL.
        blob_url = self._build_blob_url(blob_name)

        # Extract content MD5 hash if available.
        content_md5 = self._extract_content_md5(props)

        result: dict[str, Any] = {
            "provider": self.PROVIDER_NAME,
            "container": self._bucket_name,
            "blob_name": blob_name,
            "size_bytes": file_size,
            "etag": props.etag or "",
            "content_md5": content_md5,
            "url": blob_url,
            "last_modified": (
                props.last_modified.isoformat()
                if props.last_modified
                else ""
            ),
            "encryption": "AES-256" if self._encryption_enabled else "none",
            "content_type": resolved_content_type,
        }

        self._logger.info(
            "azure_blob_uploaded",
            container=self._bucket_name,
            blob_name=blob_name,
            size_bytes=file_size,
            content_type=resolved_content_type,
        )

        return result

    def upload_stream(
        self,
        stream: BinaryIO,
        remote_key: str,
        content_length: int | None = None,
        metadata: dict[str, str] | None = None,
        content_type: str | None = None,
    ) -> dict[str, Any]:
        """Upload data from a binary stream to Azure Blob Storage.

        Supports streaming uploads for large datasets generated on-the-fly
        without writing to a local file first.  The Azure SDK automatically
        stages blocks when the stream data exceeds ``max_single_put_size``.

        Args:
            stream: A readable binary file-like object (``BinaryIO``).
            remote_key: Blob name in the container (tenant prefix is
                automatically prepended).
            content_length: Optional total stream size in bytes.  Enables
                optimised single-request uploads for small payloads and
                progress tracking.
            metadata: Optional user-defined key-value metadata.
            content_type: MIME type override.  Defaults to
                ``"application/octet-stream"`` when ``None``.

        Returns:
            Same dictionary structure as :meth:`upload`.

        Raises:
            RuntimeError: If the upload fails after all retry attempts.
        """
        self.ensure_initialized()

        blob_name = self._build_remote_key(remote_key)

        resolved_content_type = content_type or "application/octet-stream"
        content_settings = ContentSettings(
            content_type=resolved_content_type,
        )

        blob_client = self._active_container_client.get_blob_client(blob_name)

        # Build keyword arguments for upload_blob.
        upload_kwargs: dict[str, Any] = {
            "data": stream,
            "overwrite": True,
            "content_settings": content_settings,
            "metadata": metadata,
            "max_concurrency": 4,
        }
        if content_length is not None:
            upload_kwargs["length"] = content_length

        def _do_stream_upload() -> None:
            """Inner streaming upload callable for retry wrapper."""
            blob_client.upload_blob(**upload_kwargs)

        self._execute_with_retry(_do_stream_upload)

        # Retrieve blob properties for verification and result.
        props = blob_client.get_blob_properties()
        blob_size = props.size or 0

        # Update operational metrics.
        self._total_bytes_uploaded += blob_size
        self._total_operations += 1

        blob_url = self._build_blob_url(blob_name)
        content_md5 = self._extract_content_md5(props)

        result: dict[str, Any] = {
            "provider": self.PROVIDER_NAME,
            "container": self._bucket_name,
            "blob_name": blob_name,
            "size_bytes": blob_size,
            "etag": props.etag or "",
            "content_md5": content_md5,
            "url": blob_url,
            "last_modified": (
                props.last_modified.isoformat()
                if props.last_modified
                else ""
            ),
            "encryption": "AES-256" if self._encryption_enabled else "none",
            "content_type": resolved_content_type,
        }

        self._logger.info(
            "azure_blob_stream_uploaded",
            container=self._bucket_name,
            blob_name=blob_name,
            size_bytes=blob_size,
            content_type=resolved_content_type,
        )

        return result

    # ------------------------------------------------------------------
    # Download operations
    # ------------------------------------------------------------------

    def download(self, remote_key: str, local_path: str) -> dict[str, Any]:
        """Download a blob to a local file.

        Uses Azure SDK's streaming download to write blob content directly
        to disk via :meth:`readinto`, minimising peak memory consumption
        for large synthetic data exports.

        Args:
            remote_key: Blob name in the container (tenant prefix is
                automatically prepended).
            local_path: Destination path on the local filesystem.  Parent
                directories must already exist.

        Returns:
            A dictionary describing the download::

                {
                    "provider": "azure_blob",
                    "container": str,
                    "blob_name": str,
                    "local_path": str,
                    "size_bytes": int,
                }

        Raises:
            FileNotFoundError: If the remote blob does not exist.
            RuntimeError: If the download fails after all retry attempts.
        """
        self.ensure_initialized()

        blob_name = self._build_remote_key(remote_key)
        blob_client = self._active_container_client.get_blob_client(blob_name)

        def _do_download() -> int:
            """Inner download callable for retry wrapper."""
            try:
                download_response = blob_client.download_blob()
            except ResourceNotFoundError as exc:
                raise FileNotFoundError(
                    f"Blob not found: {blob_name} in container "
                    f"{self._bucket_name}"
                ) from exc

            with open(local_path, "wb") as dest_file:
                download_response.readinto(dest_file)

            return download_response.properties.size or 0

        size_bytes: int = self._execute_with_retry(_do_download)

        # Update operational metrics.
        self._total_bytes_downloaded += size_bytes
        self._total_operations += 1

        result: dict[str, Any] = {
            "provider": self.PROVIDER_NAME,
            "container": self._bucket_name,
            "blob_name": blob_name,
            "local_path": local_path,
            "size_bytes": size_bytes,
        }

        self._logger.info(
            "azure_blob_downloaded",
            container=self._bucket_name,
            blob_name=blob_name,
            local_path=local_path,
            size_bytes=size_bytes,
        )

        return result

    def download_stream(self, remote_key: str) -> BinaryIO:
        """Download a blob as a seekable in-memory binary stream.

        Reads the entire blob into a :class:`io.BytesIO` buffer and
        returns it positioned at the start.  Useful for piping downloaded
        synthetic data between services without writing to disk.

        The **caller** is responsible for closing the returned stream when
        finished.

        Args:
            remote_key: Blob name in the container (tenant prefix is
                automatically prepended).

        Returns:
            A seekable ``BinaryIO`` stream containing the blob data.

        Raises:
            FileNotFoundError: If the remote blob does not exist.
            RuntimeError: If the download fails after all retry attempts.
        """
        self.ensure_initialized()

        blob_name = self._build_remote_key(remote_key)
        blob_client = self._active_container_client.get_blob_client(blob_name)

        def _do_download_stream() -> io.BytesIO:
            """Inner stream download callable for retry wrapper."""
            try:
                download_response = blob_client.download_blob()
            except ResourceNotFoundError as exc:
                raise FileNotFoundError(
                    f"Blob not found: {blob_name} in container "
                    f"{self._bucket_name}"
                ) from exc

            buffer = io.BytesIO(download_response.readall())
            buffer.seek(0)
            return buffer

        buffer: io.BytesIO = self._execute_with_retry(_do_download_stream)

        # Track metrics based on buffer size.
        size_bytes = buffer.getbuffer().nbytes
        self._total_bytes_downloaded += size_bytes
        self._total_operations += 1

        self._logger.info(
            "azure_blob_downloaded_stream",
            container=self._bucket_name,
            blob_name=blob_name,
            size_bytes=size_bytes,
        )

        return buffer

    # ------------------------------------------------------------------
    # Listing
    # ------------------------------------------------------------------

    def list_objects(
        self,
        prefix: str | None = None,
        max_results: int = 1000,
    ) -> list[dict[str, Any]]:
        """List blobs in the container with optional prefix filtering.

        Combines the provider's configured tenant prefix with the caller-
        supplied *prefix* to produce a fully-qualified name filter.
        Results are paginated internally and capped at *max_results*.

        Args:
            prefix: Additional key prefix filter (appended to the tenant
                prefix).  ``None`` returns all blobs under the tenant
                prefix.
            max_results: Maximum number of blob entries to return.
                Defaults to ``1000``.

        Returns:
            A list of dictionaries, each describing one blob::

                {
                    "key": str,
                    "size_bytes": int,
                    "last_modified": str,
                    "etag": str,
                    "content_type": str,
                    "blob_type": str,
                }

        Raises:
            RuntimeError: If the listing request fails after retries.
        """
        self.ensure_initialized()

        # Build the full prefix by combining tenant prefix and caller prefix.
        full_prefix: str | None = None
        if self._prefix and prefix:
            # Use _build_remote_key to combine tenant prefix + caller prefix.
            full_prefix = self._build_remote_key(prefix)
        elif self._prefix:
            # List everything under the tenant prefix.
            cleaned = self._prefix.strip("/")
            full_prefix = f"{cleaned}/" if cleaned else None
        elif prefix:
            full_prefix = prefix

        def _do_list() -> list[dict[str, Any]]:
            """Inner listing callable for retry wrapper."""
            results: list[dict[str, Any]] = []
            blob_iter = self._active_container_client.list_blobs(
                name_starts_with=full_prefix,
                results_per_page=max_results,
            )
            for count, blob in enumerate(blob_iter):
                if count >= max_results:
                    break
                blob_content_type = (
                    blob.content_settings.content_type
                    if blob.content_settings
                    and blob.content_settings.content_type
                    else "application/octet-stream"
                )
                results.append(
                    {
                        "key": blob.name,
                        "size_bytes": blob.size or 0,
                        "last_modified": (
                            blob.last_modified.isoformat()
                            if blob.last_modified
                            else ""
                        ),
                        "etag": blob.etag or "",
                        "content_type": blob_content_type,
                        "blob_type": (
                            str(blob.blob_type)
                            if blob.blob_type
                            else "BlockBlob"
                        ),
                    }
                )
            return results

        results: list[dict[str, Any]] = self._execute_with_retry(_do_list)

        self._total_operations += 1

        self._logger.info(
            "azure_blobs_listed",
            container=self._bucket_name,
            prefix=full_prefix,
            result_count=len(results),
        )

        return results

    # ------------------------------------------------------------------
    # Delete operations
    # ------------------------------------------------------------------

    def delete(self, remote_key: str) -> bool:
        """Delete a single blob from the container.

        Deletes the blob and all its snapshots.  Returns ``False`` if the
        blob does not exist (idempotent delete behaviour).

        Args:
            remote_key: Blob name (tenant prefix auto-prepended).

        Returns:
            ``True`` if the blob was successfully deleted; ``False`` if it
            was already absent.

        Raises:
            RuntimeError: If deletion fails for reasons other than
                "not found" (e.g. permission denied, network error).
        """
        self.ensure_initialized()

        blob_name = self._build_remote_key(remote_key)
        blob_client = self._active_container_client.get_blob_client(blob_name)

        def _do_delete() -> bool:
            """Inner delete callable for retry wrapper."""
            try:
                blob_client.delete_blob(delete_snapshots="include")
                return True
            except ResourceNotFoundError:
                return False

        deleted: bool = self._execute_with_retry(_do_delete)

        self._total_operations += 1

        self._logger.info(
            "azure_blob_deleted",
            container=self._bucket_name,
            blob_name=blob_name,
            deleted=deleted,
        )

        return deleted

    def delete_many(self, remote_keys: list[str]) -> dict[str, Any]:
        """Batch-delete multiple blobs from the container.

        Uses Azure's batch-delete API via
        :meth:`ContainerClient.delete_blobs` for efficient bulk removal.
        If the batch API fails (e.g. unsupported by the storage
        emulator), falls back to sequential individual deletes.

        Individual failures are captured in the ``errors`` list without
        aborting the remaining deletions.

        Args:
            remote_keys: List of blob names to delete (tenant prefix is
                auto-prepended to each key).

        Returns:
            A summary dictionary::

                {
                    "deleted": int,
                    "errors": [{"key": str, "error": str}, ...],
                }

        Raises:
            RuntimeError: If the batch request itself fails after retries.
        """
        self.ensure_initialized()

        if not remote_keys:
            return {"deleted": 0, "errors": []}

        blob_names = [self._build_remote_key(key) for key in remote_keys]
        deleted_count = 0
        errors: list[dict[str, str]] = []

        def _do_batch_delete() -> None:
            """Inner batch-delete callable for retry wrapper."""
            nonlocal deleted_count, errors
            # Reset counters in case of retry.
            deleted_count = 0
            errors = []

            try:
                responses = self._active_container_client.delete_blobs(
                    *blob_names,
                    delete_snapshots="include",
                )
                # Iterate responses to detect per-blob errors.
                for idx, response in enumerate(responses):
                    blob_key = (
                        blob_names[idx] if idx < len(blob_names) else "unknown"
                    )
                    if hasattr(response, "status_code"):
                        if 200 <= response.status_code < 300:
                            deleted_count += 1
                        elif response.status_code == 404:
                            # Already gone — counts as successfully deleted.
                            deleted_count += 1
                        else:
                            errors.append(
                                {
                                    "key": blob_key,
                                    "error": f"HTTP {response.status_code}",
                                }
                            )
                    else:
                        # No status_code attribute — assume success.
                        deleted_count += 1
            except AzureError as exc:
                # Batch API not supported or other failure — fall back to
                # sequential individual deletes.
                self._logger.warning(
                    "azure_batch_delete_fallback",
                    error=str(exc),
                    blob_count=len(blob_names),
                )
                deleted_count = 0
                errors = []
                for name in blob_names:
                    try:
                        client = self._active_container_client.get_blob_client(name)
                        client.delete_blob(delete_snapshots="include")
                        deleted_count += 1
                    except ResourceNotFoundError:
                        # Already gone — counts as deleted.
                        deleted_count += 1
                    except AzureError as inner_exc:
                        errors.append(
                            {"key": name, "error": str(inner_exc)}
                        )

        self._execute_with_retry(_do_batch_delete)

        self._total_operations += 1

        self._logger.info(
            "azure_blobs_batch_deleted",
            container=self._bucket_name,
            requested=len(blob_names),
            deleted=deleted_count,
            error_count=len(errors),
        )

        return {"deleted": deleted_count, "errors": errors}

    # ------------------------------------------------------------------
    # Existence & metadata
    # ------------------------------------------------------------------

    def exists(self, remote_key: str) -> bool:
        """Check whether a blob exists in the container.

        Performs a lightweight HEAD-equivalent call via
        :meth:`BlobClient.get_blob_properties` without downloading any
        blob data.

        Args:
            remote_key: Blob name (tenant prefix auto-prepended).

        Returns:
            ``True`` if the blob exists; ``False`` otherwise.

        Raises:
            RuntimeError: If the existence check fails due to a network
                or permission error (not "not found").
        """
        self.ensure_initialized()

        blob_name = self._build_remote_key(remote_key)
        blob_client = self._active_container_client.get_blob_client(blob_name)

        try:
            blob_client.get_blob_properties()
            return True
        except ResourceNotFoundError:
            return False
        except AzureError as exc:
            self._logger.error(
                "azure_blob_exists_error",
                container=self._bucket_name,
                blob_name=blob_name,
                error=str(exc),
            )
            raise RuntimeError(
                f"Failed to check existence of blob '{blob_name}' in "
                f"container '{self._bucket_name}': {exc}"
            ) from exc

    def get_metadata(self, remote_key: str) -> dict[str, Any]:
        """Retrieve blob metadata without downloading the blob body.

        Args:
            remote_key: Blob name (tenant prefix auto-prepended).

        Returns:
            A dictionary containing::

                {
                    "key": str,
                    "size_bytes": int,
                    "content_type": str,
                    "last_modified": str,
                    "etag": str,
                    "metadata": Dict[str, str],
                    "blob_type": str,
                    "encryption": str,
                }

        Raises:
            FileNotFoundError: If the blob does not exist.
            RuntimeError: If metadata retrieval fails after retries.
        """
        self.ensure_initialized()

        blob_name = self._build_remote_key(remote_key)
        blob_client = self._active_container_client.get_blob_client(blob_name)

        try:
            props = blob_client.get_blob_properties()
        except ResourceNotFoundError as exc:
            raise FileNotFoundError(
                f"Blob not found: {blob_name} in container "
                f"{self._bucket_name}"
            ) from exc

        self._total_operations += 1

        resolved_content_type = (
            props.content_settings.content_type
            if props.content_settings and props.content_settings.content_type
            else "application/octet-stream"
        )

        return {
            "key": blob_name,
            "size_bytes": props.size or 0,
            "content_type": resolved_content_type,
            "last_modified": (
                props.last_modified.isoformat()
                if props.last_modified
                else ""
            ),
            "etag": props.etag or "",
            "metadata": dict(props.metadata) if props.metadata else {},
            "blob_type": (
                str(props.blob_type) if props.blob_type else "BlockBlob"
            ),
            "encryption": "AES-256" if self._encryption_enabled else "none",
        }

    # ------------------------------------------------------------------
    # SAS URL generation
    # ------------------------------------------------------------------

    def generate_sas_url(
        self,
        remote_key: str,
        expiration_hours: int = 1,
        permissions: str = "r",
    ) -> str:
        """Generate a Shared Access Signature (SAS) URL for a blob.

        Creates a time-limited, permission-restricted URL that external
        consumers can use to access the blob without storage account
        credentials.  Supports both account-key signing and user-delegation-
        key signing (for managed identity scenarios).

        Args:
            remote_key: Blob name (tenant prefix auto-prepended).
            expiration_hours: Validity period in hours.  Defaults to ``1``.
            permissions: SAS permissions string.  Common values:

                * ``"r"`` — read
                * ``"w"`` — write
                * ``"d"`` — delete
                * ``"rw"`` — read + write

        Returns:
            The complete SAS URL as a string, including the
            ``?sv=...&se=...&sig=...`` query parameters.

        Raises:
            ValueError: If neither account key nor managed identity is
                available for signing the SAS token.
            AzureError: If user delegation key retrieval fails.
        """
        self.ensure_initialized()

        blob_name = self._build_remote_key(remote_key)

        # Resolve the account name — may come from config, env, or the
        # service client (when initialised from a connection string).
        account_name = self._account_name
        if not account_name and self._blob_service_client:
            account_name = self._blob_service_client.account_name

        if not account_name:
            raise ValueError(
                "Cannot generate SAS URL: account_name is required but "
                "could not be determined from config or service client."
            )

        start_time = datetime.now(tz=UTC)
        expiry_time = start_time + timedelta(hours=expiration_hours)

        # Build permissions from the provided string.
        sas_permissions = BlobSasPermissions(
            read="r" in permissions,
            write="w" in permissions,
            delete="d" in permissions,
            add="a" in permissions,
            create="c" in permissions,
        )

        if self._account_key:
            # Sign with the storage account key (simplest path).
            sas_token = generate_blob_sas(
                account_name=account_name,
                container_name=self._bucket_name,
                blob_name=blob_name,
                account_key=self._account_key,
                permission=sas_permissions,
                start=start_time,
                expiry=expiry_time,
            )
        elif self._use_managed_identity and self._blob_service_client:
            # Use a user delegation key for managed-identity scenarios.
            delegation_key = (
                self._blob_service_client.get_user_delegation_key(
                    key_start_time=start_time,
                    key_expiry_time=expiry_time,
                )
            )
            sas_token = generate_blob_sas(
                account_name=account_name,
                container_name=self._bucket_name,
                blob_name=blob_name,
                user_delegation_key=delegation_key,
                permission=sas_permissions,
                start=start_time,
                expiry=expiry_time,
            )
        else:
            raise ValueError(
                "Cannot generate SAS URL: either 'account_key' or "
                "'use_managed_identity=True' (with user delegation key "
                "support) is required for SAS token signing."
            )

        sas_url = (
            f"https://{account_name}.blob.core.windows.net/"
            f"{self._bucket_name}/{blob_name}?{sas_token}"
        )

        self._logger.info(
            "azure_sas_url_generated",
            container=self._bucket_name,
            blob_name=blob_name,
            expiration_hours=expiration_hours,
            permissions=permissions,
        )

        return sas_url

    # ------------------------------------------------------------------
    # Health check
    # ------------------------------------------------------------------

    def health_check(self) -> dict[str, Any]:
        """Verify Azure Blob Storage connectivity and container access.

        Performs a lightweight :meth:`ContainerClient.get_container_properties`
        call and measures the round-trip latency using the base class's
        :meth:`~BaseCloudProvider._measure_latency` helper.

        Returns:
            A dictionary with health status::

                {
                    "status": "healthy" | "unhealthy",
                    "provider": "azure_blob",
                    "container": str,
                    "account": str,
                    "latency_ms": float,
                    "encryption_enabled": bool,
                    "error": str | None,
                }
        """
        account = self._account_name or "unknown"

        if not self._initialized or not self._container_client:
            return {
                "status": "unhealthy",
                "provider": self.PROVIDER_NAME,
                "container": self._bucket_name,
                "account": account,
                "latency_ms": 0.0,
                "encryption_enabled": self._encryption_enabled,
                "error": "Provider not initialized",
            }

        try:
            _, latency_ms = self._measure_latency(
                self._active_container_client.get_container_properties,
            )

            # Include operational metrics in the health response.
            metrics = self.get_metrics()

            self._logger.debug(
                "azure_health_check_ok",
                container=self._bucket_name,
                account=account,
                latency_ms=round(latency_ms, 2),
            )

            return {
                "status": "healthy",
                "provider": self.PROVIDER_NAME,
                "container": self._bucket_name,
                "account": account,
                "latency_ms": round(latency_ms, 2),
                "encryption_enabled": self._encryption_enabled,
                "error": None,
                "metrics": metrics,
            }
        except AzureError as exc:
            self._logger.error(
                "azure_health_check_failed",
                container=self._bucket_name,
                account=account,
                error=str(exc),
            )
            return {
                "status": "unhealthy",
                "provider": self.PROVIDER_NAME,
                "container": self._bucket_name,
                "account": account,
                "latency_ms": 0.0,
                "encryption_enabled": self._encryption_enabled,
                "error": str(exc),
            }

    # ------------------------------------------------------------------
    # Private helpers
    # ------------------------------------------------------------------

    def _build_blob_url(self, blob_name: str) -> str:
        """Construct the public URL for a blob.

        Uses the account name and container to form the standard Azure
        Blob Storage URL.  If the account name is unavailable (e.g.
        local emulator), returns a relative path.

        Args:
            blob_name: The full blob name (including tenant prefix).

        Returns:
            The blob URL as a string.
        """
        if self._account_name:
            return (
                f"https://{self._account_name}.blob.core.windows.net/"
                f"{self._bucket_name}/{blob_name}"
            )
        return f"{self._bucket_name}/{blob_name}"

    @staticmethod
    def _extract_content_md5(props: Any) -> str | None:
        """Extract the Content-MD5 hash from blob properties.

        The Azure SDK stores the MD5 as raw bytes in
        ``props.content_settings.content_md5``.  This helper converts
        it to a hex string for JSON-serialisable result dictionaries.

        Args:
            props: Blob properties object returned by
                :meth:`BlobClient.get_blob_properties`.

        Returns:
            The MD5 hash as a hex string, or ``None`` if not available.
        """
        if (
            props.content_settings
            and props.content_settings.content_md5
        ):
            raw_md5 = props.content_settings.content_md5
            if isinstance(raw_md5, (bytes, bytearray)):
                return raw_md5.hex()
            return str(raw_md5)
        return None
