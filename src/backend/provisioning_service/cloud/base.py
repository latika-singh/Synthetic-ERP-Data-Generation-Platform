"""Abstract base cloud storage provider for the Provisioning Service.

This module defines :class:`BaseCloudProvider`, the abstract interface that all
concrete cloud storage providers in the Synthetic ERP Data Generation Platform
must implement.  Three concrete subclasses are expected:

* :class:`S3Provider` — AWS S3 with multi-part upload and AES-256 SSE
* :class:`AzureBlobProvider` — Azure Blob Storage with managed identity
* :class:`GCSProvider` — GCP Cloud Storage with service account auth

The base class centralises functionality that is common across every provider:

* **Configuration validation** — ensures required keys are present before the
  provider attempts to connect.
* **Retry logic with exponential backoff** — transparently retries transient
  cloud API failures (HTTP 5xx, throttling, timeouts).
* **Content-type detection** — maps synthetic-data file extensions
  (``.csv``, ``.json``, ``.parquet``, ``.sql``) to their MIME types.
* **Latency measurement** — wraps any callable and returns the elapsed wall-
  clock time alongside the result (used for health checks and metrics).
* **Prefix-based tenant isolation** — automatically prepends a configurable
  key prefix to every remote object key so that tenants share a bucket/
  container without colliding.
* **Context-manager protocol** — ``with provider: ...`` guarantees that the
  client is initialised on entry and resources are released on exit.
* **Operational metrics** — tracks cumulative bytes uploaded/downloaded and
  total operation count for Prometheus export.

Usage::

    from provisioning_service.cloud.s3_provider import S3Provider

    config = {
        "bucket_name": "erp-exports",
        "prefix": "tenant-42/",
        "encryption_enabled": True,
        "aws_region": "us-east-1",
    }

    with S3Provider(config) as provider:
        result = provider.upload("/tmp/output.parquet", "datasets/run-123.parquet")
        print(result)

Design Patterns:
    * **Abstract Factory** — ``BaseCloudProvider`` acts as the product interface
      for an abstract factory; the concrete factory selects the appropriate
      subclass based on the target cloud provider configuration.
    * **Strategy** — each subclass encapsulates a cloud-specific upload /
      download algorithm behind a uniform interface.
    * **Template Method** — ``__init__`` orchestrates a fixed sequence of
      common setup steps then delegates to the abstract ``_initialize_client``
      hook for provider-specific client creation.
"""

from __future__ import annotations

import mimetypes
import os
import time
from abc import ABC, abstractmethod
from typing import Any, BinaryIO, Callable, Dict, List, Optional, Tuple

from shared.logging.structured_logger import get_logger

# ---------------------------------------------------------------------------
# Custom MIME type extensions for synthetic data formats that may not be
# registered in the system's default MIME database.
# ---------------------------------------------------------------------------
_SYNTHETIC_DATA_MIME_MAP: Dict[str, str] = {
    ".csv": "text/csv",
    ".json": "application/json",
    ".jsonl": "application/x-ndjson",
    ".parquet": "application/octet-stream",
    ".sql": "text/plain",
}


class BaseCloudProvider(ABC):
    """Abstract base class for cloud storage providers.

    Every concrete provider (S3, Azure Blob, GCS) **must** subclass
    ``BaseCloudProvider`` and implement all ``@abstractmethod``-decorated
    methods.  The base class provides shared infrastructure — retry logic,
    content-type detection, tenant-prefix management, latency measurement,
    and a context-manager lifecycle — so that subclasses only need to
    implement the cloud-specific API calls.

    Args:
        config: Provider configuration dictionary.  Required keys depend on
            the concrete provider, but the following keys are universally
            recognised by the base class:

            * ``bucket_name`` (str) — Target bucket or container name.
            * ``prefix`` (str, optional) — Key prefix for tenant isolation.
            * ``encryption_enabled`` (bool, optional) — Whether to enable
              AES-256 server-side encryption.  Defaults to ``True``.
            * ``max_retries`` (int, optional) — Maximum retry attempts for
              transient failures.  Defaults to ``3``.
            * ``retry_delay`` (float, optional) — Base delay in seconds
              between retry attempts (doubled each attempt).  Defaults to
              ``1.0``.

    Raises:
        ValueError: If *config* is ``None`` or not a dictionary.

    Example::

        class MyProvider(BaseCloudProvider):
            def _initialize_client(self) -> None:
                self._client = SomeSDK(...)
                self._initialized = True

            # ... implement remaining abstract methods ...
    """

    # ------------------------------------------------------------------
    # Initialisation
    # ------------------------------------------------------------------

    def __init__(self, config: Dict[str, Any]) -> None:
        """Initialise the cloud storage provider with the given configuration.

        Performs the following steps in order:

        1. Validate that *config* is a non-``None`` dictionary.
        2. Store the full configuration and extract common settings.
        3. Create a structured logger bound to the concrete class name.
        4. Initialise internal state trackers (metrics, retry settings).
        5. Delegate to :meth:`_initialize_client` for provider-specific
           client creation.

        Args:
            config: Provider configuration dictionary.  See the class-level
                docstring for universally recognised keys.

        Raises:
            ValueError: If *config* is ``None`` or not a ``dict``.
        """
        if config is None or not isinstance(config, dict):
            raise ValueError(
                "BaseCloudProvider requires a non-None dict for 'config'. "
                f"Received: {type(config).__name__}"
            )

        # Full configuration reference -----------------------------------
        self._config: Dict[str, Any] = config

        # Common settings extracted from config --------------------------
        self._bucket_name: str = str(config.get("bucket_name", ""))
        self._prefix: Optional[str] = config.get("prefix")

        # Structured logger bound to the concrete subclass name ----------
        self._logger = get_logger(self.__class__.__name__)

        # Client / connection state --------------------------------------
        self._initialized: bool = False

        # Retry settings -------------------------------------------------
        self._max_retries: int = int(config.get("max_retries", 3))
        self._retry_delay: float = float(config.get("retry_delay", 1.0))

        # AES-256 encryption flag (enabled by default) -------------------
        self._encryption_enabled: bool = bool(
            config.get("encryption_enabled", True)
        )

        # Cumulative operational metrics ---------------------------------
        self._total_bytes_uploaded: int = 0
        self._total_bytes_downloaded: int = 0
        self._total_operations: int = 0

        # Delegate to provider-specific client setup ---------------------
        self._initialize_client()

    # ------------------------------------------------------------------
    # Abstract methods — must be implemented by every concrete provider
    # ------------------------------------------------------------------

    @abstractmethod
    def _initialize_client(self) -> None:
        """Create and configure the provider-specific SDK client.

        Called automatically at the end of :meth:`__init__`.  Implementations
        **must** set ``self._initialized = True`` upon successful client
        creation so that :meth:`ensure_initialized` and the context-manager
        protocol can verify readiness.

        Raises:
            RuntimeError: If the underlying SDK client cannot be created
                (e.g. invalid credentials, unreachable endpoint).
        """

    @abstractmethod
    def upload(
        self,
        local_path: str,
        remote_key: str,
        metadata: Optional[Dict[str, str]] = None,
        content_type: Optional[str] = None,
    ) -> Dict[str, Any]:
        """Upload a local file to cloud storage.

        Implementations must:

        * Automatically use multi-part upload for files exceeding the
          provider's single-part threshold (typically 5 GiB for S3,
          256 MiB for Azure, 5 MiB for GCS resumable).
        * Apply AES-256 server-side encryption when
          ``self._encryption_enabled`` is ``True``.
        * Track bytes uploaded in ``self._total_bytes_uploaded`` and
          increment ``self._total_operations``.

        Args:
            local_path: Absolute or relative path to the file on disk.
            remote_key: Object key / blob name in the bucket or container.
                The configured prefix is **not** automatically prepended;
                callers should use :meth:`_build_remote_key` if needed.
            metadata: Optional user-defined key-value metadata to attach
                to the uploaded object.
            content_type: MIME content type override.  When ``None``, the
                provider should call :meth:`_detect_content_type` to infer
                the type from the file extension.

        Returns:
            A dictionary containing at minimum::

                {
                    "provider": str,            # e.g. "s3", "azure_blob", "gcs"
                    "bucket": str,              # bucket or container name
                    "key": str,                 # final object key
                    "size_bytes": int,          # uploaded file size
                    "url": str,                 # provider-specific object URL
                    "encryption": str,          # encryption algorithm used
                    "content_type": str,        # resolved MIME type
                }

        Raises:
            FileNotFoundError: If *local_path* does not exist.
            RuntimeError: If the upload fails after retries.
        """

    @abstractmethod
    def upload_stream(
        self,
        stream: BinaryIO,
        remote_key: str,
        content_length: Optional[int] = None,
        metadata: Optional[Dict[str, str]] = None,
        content_type: Optional[str] = None,
    ) -> Dict[str, Any]:
        """Upload data from a binary stream to cloud storage.

        Supports streaming uploads for large datasets that are generated
        on-the-fly without first writing to a local file.  When
        *content_length* is unknown, implementations must fall back to
        chunked / resumable upload protocols.

        Args:
            stream: A readable binary file-like object (``BinaryIO``).
            remote_key: Object key / blob name in the bucket or container.
            content_length: Optional total content length in bytes.  When
                provided, enables single-request uploads and progress
                tracking.  When ``None``, the provider must use a chunked
                upload strategy.
            metadata: Optional user-defined metadata for the object.
            content_type: MIME type override.  Defaults to
                ``application/octet-stream`` when ``None``.

        Returns:
            Same result dictionary structure as :meth:`upload`.

        Raises:
            RuntimeError: If the upload fails after retries.
        """

    @abstractmethod
    def download(self, remote_key: str, local_path: str) -> Dict[str, Any]:
        """Download a cloud object to a local file.

        Implementations must track bytes downloaded in
        ``self._total_bytes_downloaded`` and increment
        ``self._total_operations``.

        Args:
            remote_key: Object key / blob name to download.
            local_path: Destination path on the local filesystem.  Parent
                directories are **not** created automatically; callers must
                ensure the target directory exists.

        Returns:
            A dictionary containing::

                {
                    "provider": str,
                    "bucket": str,
                    "key": str,
                    "local_path": str,
                    "size_bytes": int,
                }

        Raises:
            FileNotFoundError: If the remote object does not exist.
            RuntimeError: If the download fails after retries.
        """

    @abstractmethod
    def download_stream(self, remote_key: str) -> BinaryIO:
        """Download a cloud object as a readable binary stream.

        Returns a file-like object that the caller can read incrementally.
        The **caller** is responsible for closing the stream when finished.

        Args:
            remote_key: Object key / blob name to download.

        Returns:
            A readable ``BinaryIO`` stream containing the object data.

        Raises:
            FileNotFoundError: If the remote object does not exist.
            RuntimeError: If the download fails after retries.
        """

    @abstractmethod
    def list_objects(
        self,
        prefix: Optional[str] = None,
        max_results: int = 1000,
    ) -> List[Dict[str, Any]]:
        """List objects in the bucket or container.

        Supports prefix-based filtering, which is the primary mechanism for
        tenant isolation — each tenant's data lives under a distinct key
        prefix.

        Args:
            prefix: Optional key prefix filter.  When ``None``, all objects
                in the bucket are returned (subject to *max_results*).
            max_results: Maximum number of object entries to return.
                Defaults to ``1000``.

        Returns:
            A list of dictionaries, each containing at minimum::

                {
                    "key": str,
                    "size_bytes": int,
                    "last_modified": str,       # ISO 8601
                    "content_type": str,
                }

        Raises:
            RuntimeError: If the listing fails after retries.
        """

    @abstractmethod
    def delete(self, remote_key: str) -> bool:
        """Delete a single object from cloud storage.

        Args:
            remote_key: Object key / blob name to delete.

        Returns:
            ``True`` if the object was successfully deleted;
            ``False`` if the object was not found (idempotent).

        Raises:
            RuntimeError: If the delete fails for reasons other than
                "not found" (e.g. permission denied, network error).
        """

    @abstractmethod
    def delete_many(self, remote_keys: List[str]) -> Dict[str, Any]:
        """Delete multiple objects in a single batch operation.

        Implementations should use the provider's native batch-delete API
        where available (e.g. S3 ``delete_objects``, Azure batch delete).

        Args:
            remote_keys: List of object keys to delete.

        Returns:
            A dictionary summarising the operation::

                {
                    "deleted": int,             # number of successfully deleted objects
                    "errors": [                  # list of per-key error details
                        {"key": str, "error": str},
                        ...
                    ],
                }

        Raises:
            RuntimeError: If the batch delete request itself fails.
        """

    @abstractmethod
    def exists(self, remote_key: str) -> bool:
        """Check whether an object exists in cloud storage.

        This is a lightweight metadata-only check (HEAD request equivalent)
        and does **not** download any object data.

        Args:
            remote_key: Object key / blob name to check.

        Returns:
            ``True`` if the object exists; ``False`` otherwise.

        Raises:
            RuntimeError: If the existence check fails due to a network
                or permission error.
        """

    @abstractmethod
    def get_metadata(self, remote_key: str) -> Dict[str, Any]:
        """Retrieve object metadata without downloading the object body.

        Args:
            remote_key: Object key / blob name.

        Returns:
            A dictionary containing metadata fields such as::

                {
                    "key": str,
                    "size_bytes": int,
                    "content_type": str,
                    "last_modified": str,       # ISO 8601
                    "etag": str,
                    "metadata": Dict[str, str], # user-defined metadata
                    "encryption": str,          # encryption algorithm
                }

        Raises:
            FileNotFoundError: If the remote object does not exist.
            RuntimeError: If the metadata retrieval fails after retries.
        """

    @abstractmethod
    def health_check(self) -> Dict[str, Any]:
        """Verify cloud storage connectivity and access permissions.

        Implementations should perform a lightweight operation (e.g. list
        zero objects, head-bucket) and measure the round-trip latency.

        Returns:
            A dictionary with at minimum::

                {
                    "status": str,              # "healthy" or "unhealthy"
                    "provider": str,            # e.g. "s3", "azure_blob", "gcs"
                    "bucket": str,
                    "latency_ms": float,
                    "encryption_enabled": bool,
                    "error": Optional[str],     # error message if unhealthy
                }
        """

    # ------------------------------------------------------------------
    # Properties
    # ------------------------------------------------------------------

    @property
    def is_initialized(self) -> bool:
        """Return ``True`` if the provider client has been successfully initialised.

        Returns:
            Current initialisation state.
        """
        return self._initialized

    # ------------------------------------------------------------------
    # Concrete helper methods
    # ------------------------------------------------------------------

    def ensure_initialized(self) -> None:
        """Ensure the provider client is initialised, attempting init if needed.

        If the provider is not yet initialised, this method calls
        :meth:`_initialize_client` once.  If initialisation still fails, a
        ``RuntimeError`` is raised so that callers receive a clear signal
        rather than silent degradation.

        Raises:
            RuntimeError: If initialisation fails or the client remains
                uninitialised after the attempt.
        """
        if self._initialized:
            return

        self._logger.warning(
            "provider_not_initialized",
            provider=self.__class__.__name__,
            bucket=self._bucket_name,
        )

        try:
            self._initialize_client()
        except Exception as exc:
            self._logger.error(
                "provider_initialization_failed",
                provider=self.__class__.__name__,
                error=str(exc),
            )
            raise RuntimeError(
                f"{self.__class__.__name__} failed to initialise: {exc}"
            ) from exc

        if not self._initialized:
            raise RuntimeError(
                f"{self.__class__.__name__} is still not initialised after "
                "_initialize_client() completed without error.  Ensure the "
                "concrete implementation sets self._initialized = True."
            )

    def _build_remote_key(self, key: str) -> str:
        """Prepend the configured tenant prefix to an object key.

        Handles leading and trailing slashes so that the resulting key never
        contains doubled slashes or a leading slash (cloud storage keys are
        typically relative, not absolute paths).

        Args:
            key: The raw object key (e.g. ``"datasets/run-123.parquet"``).

        Returns:
            The fully-qualified remote key incorporating the tenant prefix.
            If no prefix is configured, the *key* is returned unchanged
            (with leading slashes stripped).

        Examples::

            # prefix = "tenant-42/"
            _build_remote_key("data/file.csv")
            # → "tenant-42/data/file.csv"

            # prefix = None
            _build_remote_key("data/file.csv")
            # → "data/file.csv"
        """
        # Strip leading slashes from the key — cloud object keys are relative.
        cleaned_key = key.lstrip("/")

        if not self._prefix:
            return cleaned_key

        # Normalise the prefix: strip leading slashes, ensure trailing slash.
        cleaned_prefix = self._prefix.strip("/")
        if cleaned_prefix:
            return f"{cleaned_prefix}/{cleaned_key}"

        return cleaned_key

    def _detect_content_type(self, file_path: str) -> str:
        """Detect the MIME content type of a file from its extension.

        Uses Python's :mod:`mimetypes` module for standard extensions and
        falls back to a built-in mapping for synthetic-data formats that
        may not be registered in the system MIME database.

        Args:
            file_path: Path to the file (only the extension is inspected).

        Returns:
            The resolved MIME type string (e.g. ``"text/csv"``).  Defaults
            to ``"application/octet-stream"`` when the type cannot be
            determined.

        Examples::

            _detect_content_type("export.csv")    # → "text/csv"
            _detect_content_type("data.parquet")  # → "application/octet-stream"
            _detect_content_type("dump.sql")      # → "text/plain"
            _detect_content_type("unknown.xyz")   # → "application/octet-stream"
        """
        # Extract the file extension in lower-case for consistent matching.
        _, ext = os.path.splitext(file_path)
        ext_lower = ext.lower()

        # Priority 1: explicit synthetic-data format map.
        if ext_lower in _SYNTHETIC_DATA_MIME_MAP:
            return _SYNTHETIC_DATA_MIME_MAP[ext_lower]

        # Priority 2: system MIME database via the mimetypes module.
        guessed_type, _ = mimetypes.guess_type(file_path)
        if guessed_type:
            return guessed_type

        # Fallback: generic binary stream.
        return "application/octet-stream"

    def _execute_with_retry(
        self,
        operation: Callable[..., Any],
        *args: Any,
        **kwargs: Any,
    ) -> Any:
        """Execute *operation* with exponential-backoff retry on failure.

        The delay between attempts doubles after each failure::

            attempt 0 → immediate
            attempt 1 → retry_delay * 2^0  seconds
            attempt 2 → retry_delay * 2^1  seconds
            ...
            attempt N → retry_delay * 2^(N-1) seconds

        Every retry is logged via the structured logger with the operation
        name, attempt number, and error details.

        Args:
            operation: A callable to execute.
            *args: Positional arguments forwarded to *operation*.
            **kwargs: Keyword arguments forwarded to *operation*.

        Returns:
            The return value of *operation* on the first successful call.

        Raises:
            Exception: Re-raises the last exception after all retry attempts
                have been exhausted.
        """
        operation_name = getattr(operation, "__name__", str(operation))
        last_exception: Optional[Exception] = None

        for attempt in range(self._max_retries + 1):
            try:
                return operation(*args, **kwargs)
            except Exception as exc:
                last_exception = exc

                if attempt < self._max_retries:
                    delay = self._retry_delay * (2 ** attempt)
                    self._logger.warning(
                        "operation_retry",
                        operation=operation_name,
                        attempt=attempt + 1,
                        max_retries=self._max_retries,
                        delay_seconds=delay,
                        error=str(exc),
                        provider=self.__class__.__name__,
                        bucket=self._bucket_name,
                    )
                    time.sleep(delay)
                else:
                    self._logger.error(
                        "operation_retries_exhausted",
                        operation=operation_name,
                        total_attempts=self._max_retries + 1,
                        error=str(exc),
                        provider=self.__class__.__name__,
                        bucket=self._bucket_name,
                    )

        # Should not be reachable unless max_retries < 0, but guard anyway.
        raise last_exception  # type: ignore[misc]

    def _measure_latency(
        self,
        operation: Callable[..., Any],
        *args: Any,
        **kwargs: Any,
    ) -> Tuple[Any, float]:
        """Execute *operation* and measure its wall-clock latency.

        Args:
            operation: A callable to execute and time.
            *args: Positional arguments forwarded to *operation*.
            **kwargs: Keyword arguments forwarded to *operation*.

        Returns:
            A ``(result, latency_ms)`` tuple where *result* is the return
            value of *operation* and *latency_ms* is the elapsed time in
            milliseconds.

        Raises:
            Exception: Propagates any exception raised by *operation*.
        """
        start = time.time()
        result = operation(*args, **kwargs)
        elapsed_ms = (time.time() - start) * 1000.0
        return result, elapsed_ms

    def _validate_config(self, required_keys: List[str]) -> None:
        """Validate that all *required_keys* are present and non-empty.

        Called by concrete providers at the start of
        :meth:`_initialize_client` to fail fast with a clear error message
        when mandatory configuration is missing.

        Args:
            required_keys: A list of configuration key names that must
                exist in ``self._config`` with truthy values.

        Raises:
            ValueError: If one or more required keys are missing or empty.

        Example::

            # In a concrete provider:
            def _initialize_client(self) -> None:
                self._validate_config(["bucket_name", "aws_region", "aws_access_key_id"])
                # ... proceed with client creation ...
        """
        missing: List[str] = [
            key
            for key in required_keys
            if not self._config.get(key)
        ]

        if missing:
            raise ValueError(
                f"{self.__class__.__name__} configuration is missing required "
                f"keys: {', '.join(missing)}"
            )

    def _get_file_size(self, file_path: str) -> int:
        """Return the size of a local file in bytes.

        Args:
            file_path: Path to the file on disk.

        Returns:
            File size in bytes.

        Raises:
            FileNotFoundError: If *file_path* does not exist on disk.
        """
        if not os.path.exists(file_path):
            raise FileNotFoundError(
                f"File not found: {file_path}"
            )
        return os.path.getsize(file_path)

    def get_metrics(self) -> Dict[str, Any]:
        """Return cumulative operational metrics for this provider instance.

        Useful for Prometheus metrics collection and operational dashboards.

        Returns:
            A dictionary containing::

                {
                    "provider": str,
                    "bucket": str,
                    "total_bytes_uploaded": int,
                    "total_bytes_downloaded": int,
                    "total_operations": int,
                    "is_initialized": bool,
                    "encryption_enabled": bool,
                }
        """
        return {
            "provider": self.__class__.__name__,
            "bucket": self._bucket_name,
            "total_bytes_uploaded": self._total_bytes_uploaded,
            "total_bytes_downloaded": self._total_bytes_downloaded,
            "total_operations": self._total_operations,
            "is_initialized": self._initialized,
            "encryption_enabled": self._encryption_enabled,
        }

    # ------------------------------------------------------------------
    # Context-manager protocol
    # ------------------------------------------------------------------

    def __enter__(self) -> "BaseCloudProvider":
        """Enter the runtime context: ensure the client is initialised.

        Returns:
            The provider instance (``self``).

        Raises:
            RuntimeError: If initialisation fails.
        """
        self.ensure_initialized()
        return self

    def __exit__(
        self,
        exc_type: Optional[type],
        exc_val: Optional[BaseException],
        exc_tb: Optional[Any],
    ) -> None:
        """Exit the runtime context: log exceptions and release resources.

        Args:
            exc_type: Exception class if an exception occurred, else ``None``.
            exc_val: Exception instance if an exception occurred, else ``None``.
            exc_tb: Traceback object if an exception occurred, else ``None``.
        """
        if exc_val is not None:
            self._logger.error(
                "provider_context_exception",
                provider=self.__class__.__name__,
                bucket=self._bucket_name,
                error_type=exc_type.__name__ if exc_type else "Unknown",
                error=str(exc_val),
            )

        self._logger.debug(
            "provider_context_exit",
            provider=self.__class__.__name__,
            bucket=self._bucket_name,
            total_operations=self._total_operations,
        )

    # ------------------------------------------------------------------
    # Dunder helpers
    # ------------------------------------------------------------------

    def __repr__(self) -> str:
        """Return a developer-friendly string representation.

        Returns:
            A string in the format
            ``ClassName(bucket=<bucket_name>, prefix=<prefix>)``.
        """
        return (
            f"{self.__class__.__name__}"
            f"(bucket={self._bucket_name!r}, prefix={self._prefix!r})"
        )
