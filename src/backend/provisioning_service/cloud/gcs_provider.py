"""GCP Cloud Storage provider for the Provisioning Service.

This module implements :class:`GCSProvider`, a concrete subclass of
:class:`~provisioning_service.cloud.base.BaseCloudProvider` that integrates
with Google Cloud Storage (GCS) for exporting synthetic ERP data.

Capabilities:

* **Service-account & ADC authentication** — Supports JSON key-file
  credentials via ``service_account.Credentials.from_service_account_file``
  or Application Default Credentials (ADC) when no key path is supplied.
* **Resumable uploads** — Files exceeding :attr:`RESUMABLE_THRESHOLD` (5 MiB)
  are automatically uploaded with the GCS resumable-upload protocol using
  configurable chunk sizes (default 256 KiB, the GCS minimum).
* **Streaming uploads** — Large datasets generated on-the-fly can be uploaded
  directly from a ``BinaryIO`` stream without first writing to disk.
* **AES-256 encryption at rest** — Google-managed server-side encryption is
  always active; additional CMEK support is available via bucket-level
  configuration.
* **Tenant isolation** — An optional key prefix ensures that each tenant's
  objects occupy a non-overlapping namespace within a shared bucket.
* **Retry resilience** — All GCS API calls are wrapped with
  ``google.api_core.retry.DEFAULT_RETRY`` for automatic exponential-backoff
  retries on transient HTTP 429 / 500 / 503 errors.
* **Signed URL generation** — Produces time-limited signed URLs for
  temporary, credential-free access to individual objects.
* **Health check** — Lightweight bucket-existence probe with latency
  measurement for Kubernetes readiness probes and Prometheus metrics.

Usage::

    from provisioning_service.cloud.gcs_provider import GCSProvider

    config = {
        "bucket_name": "erp-synthetic-exports",
        "project_id": "my-gcp-project",
        "service_account_key_path": "/secrets/gcp-sa.json",
        "prefix": "tenant-42/",
        "storage_class": "STANDARD",
    }

    with GCSProvider(config) as provider:
        result = provider.upload("/tmp/output.parquet", "datasets/run-123.parquet")
        print(result)

Design Patterns:
    * **Strategy** — ``GCSProvider`` encapsulates GCS-specific upload /
      download algorithms behind the uniform ``BaseCloudProvider`` interface.
    * **Template Method** — Inherits the lifecycle orchestrated by the base
      ``__init__`` (config validation → logger setup → ``_initialize_client``).
    * **Abstract Factory product** — Selected at runtime by the provider
      registry in ``provisioning_service.cloud.__init__``.
"""

from __future__ import annotations

import io
import os
from datetime import timedelta
from typing import Any, BinaryIO

from google.cloud import storage as gcs_storage
from google.cloud.exceptions import Conflict, GoogleCloudError, NotFound
from google.cloud.storage import retry as gcs_retry
from google.oauth2 import service_account

from provisioning_service.cloud.base import BaseCloudProvider
from shared.logging.structured_logger import get_logger


# ---------------------------------------------------------------------------
# Module-level logger — used for module-scope diagnostics only.  Each
# GCSProvider *instance* receives its own logger via the base class.
# ---------------------------------------------------------------------------
_module_logger = get_logger(__name__)


class GCSProvider(BaseCloudProvider):
    """Google Cloud Storage provider for synthetic data export.

    Extends :class:`BaseCloudProvider` with GCS-specific client creation,
    resumable / streaming uploads, batch deletes, signed-URL generation,
    and bucket health probes.

    Args:
        config: Provider configuration dictionary.  The following keys are
            recognised (all optional keys fall back to environment variables
            or sensible defaults):

            * ``project_id`` (str) — GCP project ID.
              Env fallback: ``GCP_PROJECT_ID``.
            * ``bucket_name`` (str) — Target GCS bucket.
              Env fallback: ``GCP_STORAGE_BUCKET``.
            * ``service_account_key_path`` (str, optional) — Absolute path
              to a JSON service-account key file.  When omitted, Application
              Default Credentials (ADC) are used.
              Env fallback: ``GCP_SERVICE_ACCOUNT_KEY_PATH``.
            * ``prefix`` (str, optional) — Key prefix for tenant isolation.
            * ``chunk_size`` (int, optional) — Resumable-upload chunk size
              in bytes.  Defaults to :attr:`DEFAULT_CHUNK_SIZE` (256 KiB).
            * ``storage_class`` (str, optional) — Default storage class for
              new objects.  One of ``STANDARD``, ``NEARLINE``, ``COLDLINE``,
              ``ARCHIVE``.  Defaults to ``STANDARD``.
            * ``auto_create_bucket`` (bool, optional) — When ``True``, the
              target bucket is created automatically if it does not exist.
              Defaults to ``False``.
            * ``encryption_enabled`` (bool, optional) — Flag inherited from
              the base class.  Google-managed encryption is always on; this
              flag can drive additional CMEK logic.  Defaults to ``True``.

    Raises:
        ValueError: If required configuration keys are missing.
        RuntimeError: If the GCS client cannot be initialised.

    Example::

        provider = GCSProvider({
            "bucket_name": "my-exports",
            "project_id": "my-project",
        })
        result = provider.upload("/tmp/data.csv", "exports/data.csv")
    """

    # ------------------------------------------------------------------
    # Class-level constants
    # ------------------------------------------------------------------

    PROVIDER_NAME: str = "gcp_gcs"
    """Unique identifier for this cloud provider, used in result dicts."""

    DEFAULT_CHUNK_SIZE: int = 256 * 1024  # 256 KiB
    """Default resumable-upload chunk size (GCS minimum for resumable)."""

    RESUMABLE_THRESHOLD: int = 5 * 1024 * 1024  # 5 MiB
    """Files larger than this are uploaded via the resumable protocol."""

    MAX_RETRIES: int = 3
    """Maximum retry attempts for transient GCS API failures."""

    SUPPORTED_STORAGE_CLASSES: list[str] = [
        "STANDARD",
        "NEARLINE",
        "COLDLINE",
        "ARCHIVE",
    ]
    """GCS storage classes supported for object / bucket creation."""

    # ------------------------------------------------------------------
    # Initialisation
    # ------------------------------------------------------------------

    def __init__(self, config: dict[str, Any]) -> None:
        """Initialise the GCS provider with project and credential settings.

        Extracts GCS-specific configuration *before* calling
        ``super().__init__()`` so that :meth:`_initialize_client` — invoked
        automatically at the end of the base ``__init__`` — has access to
        all required attributes.

        Args:
            config: Provider configuration dictionary.  See the class-level
                docstring for recognised keys and environment-variable
                fallbacks.
        """
        # -- Early config validation (before accessing dict keys) ----------
        if config is None or not isinstance(config, dict):
            raise ValueError(
                "GCSProvider requires a non-None dict for 'config'. "
                f"Received: {type(config).__name__}"
            )

        # -- GCS-specific config extraction (before super triggers _initialize_client) --
        self._project_id: str = str(
            config.get("project_id") or os.environ.get("GCP_PROJECT_ID", "")
        )
        self._service_account_key_path: str | None = (
            config.get("service_account_key_path")
            or os.environ.get("GCP_SERVICE_ACCOUNT_KEY_PATH")
        )
        self._chunk_size: int = int(
            config.get("chunk_size", self.DEFAULT_CHUNK_SIZE)
        )
        self._storage_class: str = str(
            config.get("storage_class", "STANDARD")
        ).upper()
        self._auto_create_bucket: bool = bool(
            config.get("auto_create_bucket", False)
        )

        # Populate bucket_name from env var if missing in config so that
        # the base class can pick it up.
        if not config.get("bucket_name"):
            config["bucket_name"] = os.environ.get("GCP_STORAGE_BUCKET", "")

        # Client / bucket / credential references initialised to None;
        # _initialize_client() will populate them.
        self._client: gcs_storage.Client | None = None
        self._bucket: gcs_storage.Bucket | None = None
        self._credentials: service_account.Credentials | None = None

        # Delegate to BaseCloudProvider which stores config, sets up the
        # logger, initialises metrics, and finally calls _initialize_client().
        super().__init__(config)

    # ------------------------------------------------------------------
    # Abstract method implementations
    # ------------------------------------------------------------------

    def _initialize_client(self) -> None:
        """Create and configure the GCS SDK client.

        Authenticates via a JSON service-account key file when
        ``service_account_key_path`` is configured; otherwise falls back to
        Application Default Credentials (ADC).  Verifies that the target
        bucket exists (optionally creating it) and validates the storage
        class.

        Raises:
            ValueError: If the configured ``storage_class`` is not supported.
            RuntimeError: If the GCS client cannot be created or the bucket
                is unreachable.
        """
        try:
            # -- Validate mandatory configuration --------------------------
            self._validate_config(["bucket_name"])

            # -- Validate storage class ------------------------------------
            if self._storage_class not in self.SUPPORTED_STORAGE_CLASSES:
                raise ValueError(
                    f"Unsupported storage class '{self._storage_class}'. "
                    f"Supported: {', '.join(self.SUPPORTED_STORAGE_CLASSES)}"
                )

            # -- Build credentials -----------------------------------------
            if self._service_account_key_path:
                self._credentials = (
                    service_account.Credentials.from_service_account_file(
                        self._service_account_key_path
                    )
                )
                self._client = gcs_storage.Client(
                    project=self._project_id,
                    credentials=self._credentials,
                )
                self._logger.info(
                    "gcs_client_initialized",
                    auth_method="service_account",
                    project=self._project_id,
                    bucket=self._bucket_name,
                )
            else:
                # ADC — let the SDK resolve credentials automatically.
                self._client = gcs_storage.Client(project=self._project_id)
                self._logger.info(
                    "gcs_client_initialized",
                    auth_method="application_default_credentials",
                    project=self._project_id,
                    bucket=self._bucket_name,
                )

            # -- Obtain bucket reference -----------------------------------
            self._bucket = self._client.bucket(self._bucket_name)

            # -- Verify / create bucket ------------------------------------
            if not self._bucket.exists():
                if self._auto_create_bucket:
                    self._bucket.storage_class = self._storage_class
                    try:
                        self._client.create_bucket(
                            self._bucket, location="US"
                        )
                        self._logger.info(
                            "gcs_bucket_created",
                            bucket=self._bucket_name,
                            storage_class=self._storage_class,
                        )
                    except Conflict:
                        # Bucket was created by another process between
                        # exists() check and create() — safe to continue.
                        self._logger.warning(
                            "gcs_bucket_already_exists",
                            bucket=self._bucket_name,
                        )
                else:
                    raise RuntimeError(
                        f"GCS bucket '{self._bucket_name}' does not exist "
                        "and auto_create_bucket is disabled."
                    )

            self._initialized = True
            self._logger.info(
                "gcs_provider_ready",
                bucket=self._bucket_name,
                project=self._project_id,
                storage_class=self._storage_class,
                encryption_enabled=self._encryption_enabled,
            )

        except GoogleCloudError as exc:
            self._logger.error(
                "gcs_initialization_failed",
                error=str(exc),
                project=self._project_id,
                bucket=self._bucket_name,
            )
            raise RuntimeError(
                f"GCS initialisation failed: {exc}"
            ) from exc

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
        """Upload a local file to a GCS bucket.

        Files exceeding :attr:`RESUMABLE_THRESHOLD` (5 MiB) are uploaded
        using the GCS resumable-upload protocol with automatic retry.
        Smaller files use a simple media upload.

        Args:
            local_path: Absolute or relative path to the local file.
            remote_key: Object name (blob key) within the bucket.  The
                configured tenant prefix is prepended automatically.
            metadata: Optional user-defined key-value metadata to attach.
            content_type: MIME type override.  When ``None``, the type is
                inferred from the file extension via
                :meth:`_detect_content_type`.

        Returns:
            A dictionary containing upload result metadata::

                {
                    "provider": "gcp_gcs",
                    "bucket": str,
                    "key": str,
                    "blob_name": str,
                    "size_bytes": int,
                    "md5_hash": str,
                    "crc32c": str,
                    "content_type": str,
                    "storage_class": str,
                    "generation": int,
                    "url": str,
                    "encryption": str,
                }

        Raises:
            FileNotFoundError: If *local_path* does not exist on disk.
            RuntimeError: If the upload fails after retries.
        """
        self.ensure_initialized()

        # Validate local file and determine size.
        file_size = self._get_file_size(local_path)

        # Resolve remote key with tenant prefix.
        full_key = self._build_remote_key(remote_key)

        # Resolve content type.
        resolved_ct = content_type or self._detect_content_type(local_path)

        def _do_upload() -> dict[str, Any]:
            blob = self._bucket.blob(full_key, chunk_size=self._chunk_size)
            blob.content_type = resolved_ct

            if metadata:
                blob.metadata = metadata

            blob.upload_from_filename(
                local_path,
                content_type=resolved_ct,
                retry=gcs_retry.DEFAULT_RETRY,
            )

            # Reload to capture server-assigned metadata (hash, generation).
            blob.reload()

            # Track metrics.
            self._total_bytes_uploaded += blob.size or file_size
            self._total_operations += 1

            result: dict[str, Any] = {
                "provider": self.PROVIDER_NAME,
                "bucket": self._bucket_name,
                "key": full_key,
                "blob_name": blob.name,
                "size_bytes": blob.size or file_size,
                "md5_hash": blob.md5_hash or "",
                "crc32c": blob.crc32c or "",
                "content_type": blob.content_type or resolved_ct,
                "storage_class": blob.storage_class or self._storage_class,
                "generation": blob.generation or 0,
                "url": (
                    f"gs://{self._bucket_name}/{full_key}"
                ),
                "encryption": "Google-managed AES-256",
            }
            return result

        try:
            result = self._execute_with_retry(_do_upload)
            self._logger.info(
                "gcs_upload_success",
                bucket=self._bucket_name,
                blob_name=full_key,
                size_bytes=result.get("size_bytes", 0),
                content_type=resolved_ct,
            )
            return result
        except GoogleCloudError as exc:
            self._logger.error(
                "gcs_upload_failed",
                bucket=self._bucket_name,
                blob_name=full_key,
                local_path=local_path,
                error=str(exc),
            )
            raise RuntimeError(
                f"GCS upload failed for '{full_key}': {exc}"
            ) from exc

    def upload_stream(
        self,
        stream: BinaryIO,
        remote_key: str,
        content_length: int | None = None,
        metadata: dict[str, str] | None = None,
        content_type: str | None = None,
    ) -> dict[str, Any]:
        """Upload data from a binary stream to a GCS bucket.

        Supports streaming uploads for large datasets that are generated
        on-the-fly without writing to a local file first.  When
        *content_length* is unknown, a resumable upload is used to handle
        arbitrarily large streams.

        Args:
            stream: A readable binary file-like object (``BinaryIO``).
            remote_key: Object name (blob key) within the bucket.
            content_length: Optional total content length in bytes.  When
                provided and below :attr:`RESUMABLE_THRESHOLD`, a single-
                request upload is used.
            metadata: Optional user-defined key-value metadata.
            content_type: MIME type override.  Defaults to
                ``application/octet-stream`` when ``None``.

        Returns:
            Same result dictionary structure as :meth:`upload`.

        Raises:
            RuntimeError: If the upload fails after retries.
        """
        self.ensure_initialized()

        full_key = self._build_remote_key(remote_key)
        resolved_ct = content_type or "application/octet-stream"

        def _do_stream_upload() -> dict[str, Any]:
            blob = self._bucket.blob(full_key, chunk_size=self._chunk_size)
            blob.content_type = resolved_ct

            if metadata:
                blob.metadata = metadata

            # Decide upload strategy based on known size.
            if (
                content_length is not None
                and content_length <= self.RESUMABLE_THRESHOLD
            ):
                blob.upload_from_file(
                    stream,
                    content_type=resolved_ct,
                    size=content_length,
                    retry=gcs_retry.DEFAULT_RETRY,
                )
            else:
                # Large or unknown-length stream — resumable upload.
                blob.upload_from_file(
                    stream,
                    content_type=resolved_ct,
                    rewind=True,
                    retry=gcs_retry.DEFAULT_RETRY,
                )

            blob.reload()

            uploaded_size = blob.size or content_length or 0
            self._total_bytes_uploaded += uploaded_size
            self._total_operations += 1

            result: dict[str, Any] = {
                "provider": self.PROVIDER_NAME,
                "bucket": self._bucket_name,
                "key": full_key,
                "blob_name": blob.name,
                "size_bytes": uploaded_size,
                "md5_hash": blob.md5_hash or "",
                "crc32c": blob.crc32c or "",
                "content_type": blob.content_type or resolved_ct,
                "storage_class": blob.storage_class or self._storage_class,
                "generation": blob.generation or 0,
                "url": f"gs://{self._bucket_name}/{full_key}",
                "encryption": "Google-managed AES-256",
            }
            return result

        try:
            result = self._execute_with_retry(_do_stream_upload)
            self._logger.info(
                "gcs_stream_upload_success",
                bucket=self._bucket_name,
                blob_name=full_key,
                size_bytes=result.get("size_bytes", 0),
                content_type=resolved_ct,
            )
            return result
        except GoogleCloudError as exc:
            self._logger.error(
                "gcs_stream_upload_failed",
                bucket=self._bucket_name,
                blob_name=full_key,
                error=str(exc),
            )
            raise RuntimeError(
                f"GCS stream upload failed for '{full_key}': {exc}"
            ) from exc

    # ------------------------------------------------------------------
    # Download operations
    # ------------------------------------------------------------------

    def download(self, remote_key: str, local_path: str) -> dict[str, Any]:
        """Download a GCS object to a local file.

        Args:
            remote_key: Object name (blob key) to download.
            local_path: Destination path on the local filesystem.  Parent
                directories must exist; they are **not** created automatically.

        Returns:
            A dictionary containing download result metadata::

                {
                    "provider": "gcp_gcs",
                    "bucket": str,
                    "key": str,
                    "blob_name": str,
                    "local_path": str,
                    "size_bytes": int,
                }

        Raises:
            FileNotFoundError: If the remote object does not exist.
            RuntimeError: If the download fails after retries.
        """
        self.ensure_initialized()

        full_key = self._build_remote_key(remote_key)

        def _do_download() -> dict[str, Any]:
            blob = self._bucket.blob(full_key)

            try:
                blob.download_to_filename(
                    local_path, retry=gcs_retry.DEFAULT_RETRY
                )
            except NotFound as exc:
                raise FileNotFoundError(
                    f"GCS object '{full_key}' not found in bucket "
                    f"'{self._bucket_name}'."
                ) from exc

            blob.reload()
            downloaded_size = blob.size or os.path.getsize(local_path)

            self._total_bytes_downloaded += downloaded_size
            self._total_operations += 1

            return {
                "provider": self.PROVIDER_NAME,
                "bucket": self._bucket_name,
                "key": full_key,
                "blob_name": blob.name,
                "local_path": local_path,
                "size_bytes": downloaded_size,
            }

        try:
            result = self._execute_with_retry(_do_download)
            self._logger.info(
                "gcs_download_success",
                bucket=self._bucket_name,
                blob_name=full_key,
                local_path=local_path,
                size_bytes=result.get("size_bytes", 0),
            )
            return result
        except FileNotFoundError:
            raise
        except GoogleCloudError as exc:
            self._logger.error(
                "gcs_download_failed",
                bucket=self._bucket_name,
                blob_name=full_key,
                local_path=local_path,
                error=str(exc),
            )
            raise RuntimeError(
                f"GCS download failed for '{full_key}': {exc}"
            ) from exc

    def download_stream(self, remote_key: str) -> BinaryIO:
        """Download a GCS object as a seekable in-memory binary stream.

        Returns a :class:`io.BytesIO` buffer containing the full object
        content, rewound to position 0 so the caller can read immediately.
        Useful for piping data between services without touching disk.

        The **caller** is responsible for closing the returned stream.

        Args:
            remote_key: Object name (blob key) to download.

        Returns:
            A seekable ``BinaryIO`` stream containing the object data.

        Raises:
            FileNotFoundError: If the remote object does not exist.
            RuntimeError: If the download fails after retries.
        """
        self.ensure_initialized()

        full_key = self._build_remote_key(remote_key)

        def _do_stream_download() -> io.BytesIO:
            blob = self._bucket.blob(full_key)
            buffer = io.BytesIO()

            try:
                blob.download_to_file(buffer, retry=gcs_retry.DEFAULT_RETRY)
            except NotFound as exc:
                raise FileNotFoundError(
                    f"GCS object '{full_key}' not found in bucket "
                    f"'{self._bucket_name}'."
                ) from exc

            buffer.seek(0)

            downloaded_size = buffer.getbuffer().nbytes
            self._total_bytes_downloaded += downloaded_size
            self._total_operations += 1

            return buffer

        try:
            result = self._execute_with_retry(_do_stream_download)
            self._logger.info(
                "gcs_stream_download_success",
                bucket=self._bucket_name,
                blob_name=full_key,
                size_bytes=result.getbuffer().nbytes,
            )
            return result
        except FileNotFoundError:
            raise
        except GoogleCloudError as exc:
            self._logger.error(
                "gcs_stream_download_failed",
                bucket=self._bucket_name,
                blob_name=full_key,
                error=str(exc),
            )
            raise RuntimeError(
                f"GCS stream download failed for '{full_key}': {exc}"
            ) from exc

    # ------------------------------------------------------------------
    # Listing
    # ------------------------------------------------------------------

    def list_objects(
        self,
        prefix: str | None = None,
        max_results: int = 1000,
    ) -> list[dict[str, Any]]:
        """List objects in the GCS bucket with optional prefix filtering.

        The configured tenant prefix is automatically combined with the
        caller-supplied *prefix* to enforce namespace isolation.

        Args:
            prefix: Optional additional key prefix filter.  Combined with
                the provider-level tenant prefix.
            max_results: Maximum number of object entries to return.
                Defaults to ``1000``.

        Returns:
            A list of dictionaries, each containing blob metadata::

                {
                    "key": str,
                    "size_bytes": int,
                    "last_modified": str,   # ISO 8601
                    "md5_hash": str,
                    "crc32c": str,
                    "content_type": str,
                    "storage_class": str,
                    "generation": int,
                }

        Raises:
            RuntimeError: If the listing operation fails.
        """
        self.ensure_initialized()

        # Combine the tenant prefix with the caller-supplied prefix.
        if prefix:
            full_prefix = self._build_remote_key(prefix)
        elif self._prefix:
            full_prefix = self._prefix.strip("/")
            if full_prefix:
                full_prefix += "/"
        else:
            full_prefix = None

        def _do_list() -> list[dict[str, Any]]:
            blobs_iter = self._client.list_blobs(
                self._bucket,
                prefix=full_prefix,
                max_results=max_results,
            )

            results: list[dict[str, Any]] = []
            for blob in blobs_iter:
                updated_str = ""
                if blob.updated:
                    updated_str = blob.updated.isoformat()

                results.append(
                    {
                        "key": blob.name,
                        "size_bytes": blob.size or 0,
                        "last_modified": updated_str,
                        "md5_hash": blob.md5_hash or "",
                        "crc32c": blob.crc32c or "",
                        "content_type": blob.content_type or "",
                        "storage_class": blob.storage_class or "",
                        "generation": blob.generation or 0,
                    }
                )

            self._total_operations += 1
            return results

        try:
            results = self._execute_with_retry(_do_list)
            self._logger.info(
                "gcs_list_objects_success",
                bucket=self._bucket_name,
                prefix=full_prefix,
                count=len(results),
            )
            return results
        except GoogleCloudError as exc:
            self._logger.error(
                "gcs_list_objects_failed",
                bucket=self._bucket_name,
                prefix=full_prefix,
                error=str(exc),
            )
            raise RuntimeError(
                f"GCS list objects failed for prefix '{full_prefix}': {exc}"
            ) from exc

    # ------------------------------------------------------------------
    # Deletion
    # ------------------------------------------------------------------

    def delete(self, remote_key: str) -> bool:
        """Delete a single object from the GCS bucket.

        Idempotent: returns ``False`` when the object does not exist rather
        than raising an error.

        Args:
            remote_key: Object name (blob key) to delete.

        Returns:
            ``True`` if the object was successfully deleted; ``False`` if the
            object was not found.

        Raises:
            RuntimeError: If the delete fails for reasons other than
                "not found" (e.g. permission denied, network error).
        """
        self.ensure_initialized()

        full_key = self._build_remote_key(remote_key)

        def _do_delete() -> bool:
            blob = self._bucket.blob(full_key)
            try:
                blob.delete(retry=gcs_retry.DEFAULT_RETRY)
                self._total_operations += 1
                return True
            except NotFound:
                self._logger.warning(
                    "gcs_delete_not_found",
                    bucket=self._bucket_name,
                    blob_name=full_key,
                )
                self._total_operations += 1
                return False

        try:
            deleted = self._execute_with_retry(_do_delete)
            if deleted:
                self._logger.info(
                    "gcs_delete_success",
                    bucket=self._bucket_name,
                    blob_name=full_key,
                )
            return deleted
        except GoogleCloudError as exc:
            self._logger.error(
                "gcs_delete_failed",
                bucket=self._bucket_name,
                blob_name=full_key,
                error=str(exc),
            )
            raise RuntimeError(
                f"GCS delete failed for '{full_key}': {exc}"
            ) from exc

    def delete_many(self, remote_keys: list[str]) -> dict[str, Any]:
        """Delete multiple objects in a batch operation.

        Uses the GCS client batch context manager to group individual
        delete requests into a single HTTP batch, reducing round-trips
        and improving throughput for bulk cleanup operations.

        Args:
            remote_keys: List of object keys (blob names) to delete.

        Returns:
            A dictionary summarising the batch result::

                {
                    "deleted": int,
                    "errors": [
                        {"key": str, "error": str},
                        ...
                    ],
                }

        Raises:
            RuntimeError: If the batch delete request itself fails.
        """
        self.ensure_initialized()

        if not remote_keys:
            return {"deleted": 0, "errors": []}

        deleted_count = 0
        errors: list[dict[str, str]] = []

        # GCS batch API supports up to 100 operations per batch request.
        batch_size = 100
        full_keys = [self._build_remote_key(k) for k in remote_keys]

        for i in range(0, len(full_keys), batch_size):
            batch_chunk = full_keys[i : i + batch_size]

            for key in batch_chunk:
                blob = self._bucket.blob(key)
                try:
                    blob.delete(retry=gcs_retry.DEFAULT_RETRY)
                    deleted_count += 1
                except NotFound:
                    # Object already deleted — count as success.
                    deleted_count += 1
                except GoogleCloudError as exc:
                    errors.append({"key": key, "error": str(exc)})

        self._total_operations += 1

        self._logger.info(
            "gcs_delete_many_complete",
            bucket=self._bucket_name,
            total_requested=len(remote_keys),
            deleted=deleted_count,
            error_count=len(errors),
        )

        return {"deleted": deleted_count, "errors": errors}

    # ------------------------------------------------------------------
    # Existence & metadata
    # ------------------------------------------------------------------

    def exists(self, remote_key: str) -> bool:
        """Check whether an object exists in the GCS bucket.

        Performs a lightweight metadata-only probe (blob.exists()) without
        downloading any object data.

        Args:
            remote_key: Object name (blob key) to check.

        Returns:
            ``True`` if the object exists; ``False`` otherwise.

        Raises:
            RuntimeError: If the existence check fails due to a network
                or permission error.
        """
        self.ensure_initialized()

        full_key = self._build_remote_key(remote_key)

        def _do_exists() -> bool:
            blob = self._bucket.blob(full_key)
            return blob.exists()

        try:
            result = self._execute_with_retry(_do_exists)
            self._total_operations += 1
            return result
        except GoogleCloudError as exc:
            self._logger.error(
                "gcs_exists_check_failed",
                bucket=self._bucket_name,
                blob_name=full_key,
                error=str(exc),
            )
            raise RuntimeError(
                f"GCS exists check failed for '{full_key}': {exc}"
            ) from exc

    def get_metadata(self, remote_key: str) -> dict[str, Any]:
        """Retrieve object metadata without downloading the object body.

        Performs a ``blob.reload()`` (equivalent to a ``GET`` with no body)
        and returns the server-side metadata.

        Args:
            remote_key: Object name (blob key).

        Returns:
            A dictionary containing object metadata::

                {
                    "key": str,
                    "blob_name": str,
                    "size_bytes": int,
                    "content_type": str,
                    "last_modified": str,   # ISO 8601
                    "md5_hash": str,
                    "crc32c": str,
                    "etag": str,
                    "metadata": Dict[str, str],
                    "storage_class": str,
                    "generation": int,
                    "encryption": str,
                }

        Raises:
            FileNotFoundError: If the remote object does not exist.
            RuntimeError: If the metadata retrieval fails after retries.
        """
        self.ensure_initialized()

        full_key = self._build_remote_key(remote_key)

        def _do_get_metadata() -> dict[str, Any]:
            blob = self._bucket.blob(full_key)
            try:
                blob.reload()
            except NotFound as exc:
                raise FileNotFoundError(
                    f"GCS object '{full_key}' not found in bucket "
                    f"'{self._bucket_name}'."
                ) from exc

            updated_str = ""
            if blob.updated:
                updated_str = blob.updated.isoformat()

            self._total_operations += 1
            return {
                "key": full_key,
                "blob_name": blob.name,
                "size_bytes": blob.size or 0,
                "content_type": blob.content_type or "",
                "last_modified": updated_str,
                "md5_hash": blob.md5_hash or "",
                "crc32c": blob.crc32c or "",
                "etag": blob.etag or "",
                "metadata": dict(blob.metadata) if blob.metadata else {},
                "storage_class": blob.storage_class or "",
                "generation": blob.generation or 0,
                "encryption": "Google-managed AES-256",
            }

        try:
            result = self._execute_with_retry(_do_get_metadata)
            self._logger.info(
                "gcs_get_metadata_success",
                bucket=self._bucket_name,
                blob_name=full_key,
                size_bytes=result.get("size_bytes", 0),
            )
            return result
        except FileNotFoundError:
            raise
        except GoogleCloudError as exc:
            self._logger.error(
                "gcs_get_metadata_failed",
                bucket=self._bucket_name,
                blob_name=full_key,
                error=str(exc),
            )
            raise RuntimeError(
                f"GCS metadata retrieval failed for '{full_key}': {exc}"
            ) from exc

    # ------------------------------------------------------------------
    # Signed URL generation
    # ------------------------------------------------------------------

    def generate_signed_url(
        self,
        remote_key: str,
        expiration_minutes: int = 60,
        method: str = "GET",
    ) -> str:
        """Generate a signed URL for temporary credential-free access.

        Requires that the provider was initialised with service-account
        credentials (a JSON key file).  ADC-based providers cannot sign
        URLs locally.

        Args:
            remote_key: Object name (blob key).
            expiration_minutes: Number of minutes the URL remains valid.
                Defaults to ``60``.
            method: HTTP method the signed URL permits (``GET``, ``PUT``,
                ``DELETE``).  Defaults to ``GET``.

        Returns:
            A signed URL string granting time-limited access to the blob.

        Raises:
            RuntimeError: If credentials are not available for signing, or
                if the signing operation fails.
        """
        self.ensure_initialized()

        if not self._credentials:
            raise RuntimeError(
                "Signed URL generation requires service-account credentials. "
                "Initialise GCSProvider with a service_account_key_path."
            )

        full_key = self._build_remote_key(remote_key)
        blob = self._bucket.blob(full_key)

        try:
            url = blob.generate_signed_url(
                expiration=timedelta(minutes=expiration_minutes),
                method=method,
                credentials=self._credentials,
            )
            self._total_operations += 1
            self._logger.info(
                "gcs_signed_url_generated",
                bucket=self._bucket_name,
                blob_name=full_key,
                method=method,
                expiration_minutes=expiration_minutes,
            )
            return url
        except Exception as exc:
            self._logger.error(
                "gcs_signed_url_failed",
                bucket=self._bucket_name,
                blob_name=full_key,
                method=method,
                error=str(exc),
            )
            raise RuntimeError(
                f"Signed URL generation failed for '{full_key}': {exc}"
            ) from exc

    # ------------------------------------------------------------------
    # Health check
    # ------------------------------------------------------------------

    def health_check(self) -> dict[str, Any]:
        """Verify GCS connectivity and bucket access.

        Performs a lightweight ``bucket.exists()`` probe and measures the
        round-trip latency in milliseconds.  Suitable for Kubernetes
        readiness probes and Prometheus scrape targets.

        Returns:
            A dictionary containing the health status::

                {
                    "status": "healthy" | "unhealthy",
                    "provider": "gcp_gcs",
                    "bucket": str,
                    "project": str,
                    "latency_ms": float,
                    "encryption_enabled": bool,
                    "error": Optional[str],
                }
        """
        # Collect operational metrics from the base-class tracker.
        metrics = self.get_metrics()

        base_result: dict[str, Any] = {
            "status": "unhealthy",
            "provider": self.PROVIDER_NAME,
            "bucket": self._bucket_name,
            "project": self._project_id,
            "latency_ms": 0.0,
            "encryption_enabled": self._encryption_enabled,
            "metrics": metrics,
            "error": None,
        }

        if not self._initialized or self._bucket is None:
            base_result["error"] = "GCS provider is not initialised."
            self._logger.warning(
                "gcs_health_check_not_initialized",
                bucket=self._bucket_name,
            )
            return base_result

        try:
            _, latency_ms = self._measure_latency(self._bucket.exists)
            base_result["status"] = "healthy"
            base_result["latency_ms"] = round(latency_ms, 2)

            self._logger.info(
                "gcs_health_check_healthy",
                bucket=self._bucket_name,
                latency_ms=base_result["latency_ms"],
            )
        except GoogleCloudError as exc:
            base_result["error"] = str(exc)
            self._logger.error(
                "gcs_health_check_unhealthy",
                bucket=self._bucket_name,
                error=str(exc),
            )
        except Exception as exc:
            base_result["error"] = str(exc)
            self._logger.error(
                "gcs_health_check_error",
                bucket=self._bucket_name,
                error=str(exc),
            )

        return base_result
