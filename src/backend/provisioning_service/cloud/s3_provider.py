"""AWS S3 cloud storage provider for the Provisioning Service.

This module implements :class:`S3Provider`, a concrete subclass of
:class:`~provisioning_service.cloud.base.BaseCloudProvider` that enables
the Synthetic ERP Data Generation Platform to export generated datasets
to Amazon S3 (and S3-compatible endpoints such as MinIO or LocalStack).

Key Capabilities:
    * **Multi-part upload** — Files exceeding the configurable threshold
      (default 8 MiB) are automatically split into parallel-uploadable
      parts.  Chunk size and concurrency are both tunable via config.
    * **AES-256 server-side encryption** — Supports both SSE-S3 (``AES256``)
      and SSE-KMS (``aws:kms``) encryption methods.  When KMS is selected,
      an optional ``kms_key_id`` can be specified; otherwise the default
      AWS-managed CMK is used.
    * **Streaming uploads** — Large datasets produced on-the-fly can be
      streamed directly to S3 via a manual multi-part upload, avoiding
      the need to buffer the entire payload on disk first.
    * **S3-compatible endpoint support** — An optional ``endpoint_url``
      configuration key allows the provider to target MinIO, LocalStack,
      or any other S3-compatible object store, enabling local development
      and air-gapped deployment scenarios.
    * **Tenant isolation** — An optional ``prefix`` key prepends a tenant
      namespace to every object key, ensuring data separation within a
      shared bucket.

Usage::

    from provisioning_service.cloud.s3_provider import S3Provider

    config = {
        "bucket_name": "erp-synthetic-exports",
        "aws_access_key_id": "AKIAIOSFODNN7EXAMPLE",
        "aws_secret_access_key": "wJalrXUtnFEMI/K7MDENG/bPxRfiCYEXAMPLEKEY",
        "aws_region": "us-east-1",
        "encryption_method": "AES256",
        "prefix": "tenant-42/",
    }

    with S3Provider(config) as provider:
        result = provider.upload("/tmp/output.parquet", "datasets/run-123.parquet")
        print(result)

Design Patterns:
    * **Strategy** — ``S3Provider`` encapsulates the AWS-specific upload,
      download, list, and delete algorithms behind the uniform
      ``BaseCloudProvider`` interface.
    * **Abstract Factory** — Selected dynamically by a cloud provider
      factory based on the target platform configuration.
    * **Circuit Breaker** — Inherits exponential-backoff retry logic from
      ``BaseCloudProvider._execute_with_retry``.
"""

from __future__ import annotations

import io
import os
from typing import Any, BinaryIO, Dict, Generator, List, Optional

import boto3
from boto3.s3.transfer import TransferConfig
from botocore.config import Config as BotoConfig
from botocore.exceptions import BotoCoreError, ClientError

from provisioning_service.cloud.base import BaseCloudProvider
from shared.logging.structured_logger import get_logger

# ---------------------------------------------------------------------------
# Module-level logger
# ---------------------------------------------------------------------------
logger = get_logger(__name__)


class S3Provider(BaseCloudProvider):
    """AWS S3 cloud storage provider for exporting synthetic ERP data.

    Implements every abstract method defined by :class:`BaseCloudProvider` using
    the ``boto3`` AWS SDK.  Multi-part uploads, server-side encryption, and
    S3-compatible endpoint routing are supported out-of-the-box.

    Configuration Keys:
        Required:
            * ``bucket_name`` (str) — Target S3 bucket name.

        Optional:
            * ``aws_access_key_id`` (str) — AWS access key ID.  Falls back
              to ``AWS_ACCESS_KEY_ID`` environment variable.
            * ``aws_secret_access_key`` (str) — AWS secret access key.  Falls
              back to ``AWS_SECRET_ACCESS_KEY`` environment variable.
            * ``aws_session_token`` (str) — Temporary session token for STS.
              Falls back to ``AWS_SESSION_TOKEN`` environment variable.
            * ``aws_region`` (str) — AWS region.  Falls back to
              ``AWS_DEFAULT_REGION`` environment variable, then ``us-east-1``.
            * ``endpoint_url`` (str) — Custom S3 endpoint for MinIO /
              LocalStack.  Falls back to ``AWS_S3_ENDPOINT_URL`` env var.
            * ``encryption_method`` (str) — ``"AES256"`` (default) or
              ``"aws:kms"``.
            * ``kms_key_id`` (str) — KMS key ARN when using ``aws:kms``.
            * ``prefix`` (str) — Key prefix for tenant isolation.
            * ``multipart_threshold`` (int) — Byte threshold for multi-part
              uploads.  Default ``8 MiB``.
            * ``multipart_chunksize`` (int) — Byte size per upload part.
              Default ``8 MiB``.
            * ``max_retries`` (int) — Maximum retry attempts.  Default ``3``.
            * ``retry_delay`` (float) — Base delay between retries.
              Default ``1.0`` seconds.

    Args:
        config: Provider configuration dictionary.

    Raises:
        ValueError: If required keys are missing or the encryption method
            is not supported.
    """

    # ------------------------------------------------------------------
    # Class-level constants
    # ------------------------------------------------------------------

    PROVIDER_NAME: str = "aws_s3"
    """Human-readable provider identifier used in result dictionaries."""

    DEFAULT_REGION: str = "us-east-1"
    """Fallback AWS region when none is supplied via config or environment."""

    DEFAULT_MULTIPART_THRESHOLD: int = 8 * 1024 * 1024  # 8 MiB
    """Byte threshold above which uploads switch to multi-part mode."""

    DEFAULT_MULTIPART_CHUNKSIZE: int = 8 * 1024 * 1024  # 8 MiB
    """Byte size of each part in a multi-part upload."""

    MAX_UPLOAD_CONCURRENCY: int = 10
    """Maximum number of parallel upload threads for multi-part transfers."""

    SUPPORTED_ENCRYPTION_METHODS: List[str] = ["AES256", "aws:kms"]
    """Supported S3 server-side encryption algorithms."""

    # ------------------------------------------------------------------
    # Initialisation
    # ------------------------------------------------------------------

    def __init__(self, config: Dict[str, Any]) -> None:
        """Initialise the S3 provider with the given configuration.

        Extracts AWS credentials (from *config* or environment variables),
        region, encryption settings, multi-part tuning parameters, and an
        optional S3-compatible endpoint URL.  Then delegates to
        :meth:`_initialize_client` via the base-class constructor to
        create the ``boto3`` S3 client and resource.

        Args:
            config: Provider configuration dictionary.  See the class
                docstring for supported keys.

        Raises:
            ValueError: If ``bucket_name`` is missing, or if an
                unsupported ``encryption_method`` is supplied.
        """
        # Extract AWS credentials — config takes precedence over env vars.
        self._aws_access_key_id: Optional[str] = config.get(
            "aws_access_key_id",
            os.environ.get("AWS_ACCESS_KEY_ID"),
        )
        self._aws_secret_access_key: Optional[str] = config.get(
            "aws_secret_access_key",
            os.environ.get("AWS_SECRET_ACCESS_KEY"),
        )
        self._aws_session_token: Optional[str] = config.get(
            "aws_session_token",
            os.environ.get("AWS_SESSION_TOKEN"),
        )

        # Region — config → env var → class default.
        self._region: str = config.get(
            "aws_region",
            os.environ.get("AWS_DEFAULT_REGION", self.DEFAULT_REGION),
        )

        # S3-compatible endpoint URL (MinIO, LocalStack) — optional.
        self._endpoint_url: Optional[str] = config.get(
            "endpoint_url",
            os.environ.get("AWS_S3_ENDPOINT_URL"),
        )

        # Multi-part upload tuning parameters.
        self._multipart_threshold: int = int(
            config.get("multipart_threshold", self.DEFAULT_MULTIPART_THRESHOLD)
        )
        self._multipart_chunksize: int = int(
            config.get("multipart_chunksize", self.DEFAULT_MULTIPART_CHUNKSIZE)
        )

        # Encryption configuration.
        self._encryption_method: str = config.get(
            "encryption_method", "AES256"
        )
        if self._encryption_method not in self.SUPPORTED_ENCRYPTION_METHODS:
            raise ValueError(
                f"Unsupported encryption_method '{self._encryption_method}'. "
                f"Supported: {self.SUPPORTED_ENCRYPTION_METHODS}"
            )
        self._kms_key_id: Optional[str] = config.get("kms_key_id")

        # boto3 objects — initialised by _initialize_client() via super().__init__.
        self._client: Any = None
        self._resource: Any = None
        self._transfer_config: Optional[TransferConfig] = None

        # Delegate to BaseCloudProvider.__init__ → _initialize_client().
        super().__init__(config)

    # ------------------------------------------------------------------
    # Client initialisation (abstract method implementation)
    # ------------------------------------------------------------------

    def _initialize_client(self) -> None:
        """Create the ``boto3`` S3 client, resource, and transfer config.

        Called automatically from :meth:`BaseCloudProvider.__init__`.
        Configures:

        * **Retry strategy** — adaptive mode with 3 maximum attempts.
        * **Signature version** — SigV4 (``s3v4``).
        * **Endpoint URL** — custom endpoint when targeting S3-compatible
          services (MinIO, LocalStack, etc.).
        * **Transfer config** — multi-part threshold, chunk size, and
          concurrency.

        Raises:
            RuntimeError: If the boto3 session or client cannot be created.
        """
        try:
            # Validate that bucket_name is present.
            self._validate_config(["bucket_name"])

            # Boto retry and signature configuration.
            boto_config = BotoConfig(
                retries={"max_attempts": 3, "mode": "adaptive"},
                signature_version="s3v4",
                region_name=self._region,
            )

            # Build a session with explicit credentials when available,
            # otherwise rely on the default credential chain (IAM role,
            # env vars, ~/.aws/credentials).
            session_kwargs: Dict[str, Any] = {
                "region_name": self._region,
            }
            if self._aws_access_key_id and self._aws_secret_access_key:
                session_kwargs["aws_access_key_id"] = self._aws_access_key_id
                session_kwargs["aws_secret_access_key"] = (
                    self._aws_secret_access_key
                )
            if self._aws_session_token:
                session_kwargs["aws_session_token"] = self._aws_session_token

            session = boto3.Session(**session_kwargs)

            # Low-level S3 client for all API calls.
            client_kwargs: Dict[str, Any] = {"config": boto_config}
            if self._endpoint_url:
                client_kwargs["endpoint_url"] = self._endpoint_url

            self._client = session.client("s3", **client_kwargs)

            # High-level S3 resource for managed file transfers.
            resource_kwargs: Dict[str, Any] = {}
            if self._endpoint_url:
                resource_kwargs["endpoint_url"] = self._endpoint_url
            self._resource = session.resource("s3", **resource_kwargs)

            # Transfer configuration for multi-part uploads / downloads.
            self._transfer_config = TransferConfig(
                multipart_threshold=self._multipart_threshold,
                multipart_chunksize=self._multipart_chunksize,
                max_concurrency=self.MAX_UPLOAD_CONCURRENCY,
                use_threads=True,
            )

            self._initialized = True

            self._logger.info(
                "s3_client_initialized",
                provider=self.PROVIDER_NAME,
                bucket=self._bucket_name,
                region=self._region,
                endpoint_url=self._endpoint_url or "default",
                encryption_method=self._encryption_method,
                multipart_threshold=self._multipart_threshold,
                multipart_chunksize=self._multipart_chunksize,
            )

        except (ClientError, BotoCoreError, ValueError) as exc:
            self._logger.error(
                "s3_client_initialization_failed",
                provider=self.PROVIDER_NAME,
                error=str(exc),
                region=self._region,
            )
            raise RuntimeError(
                f"Failed to initialise S3 client: {exc}"
            ) from exc

    # ------------------------------------------------------------------
    # Encryption helpers
    # ------------------------------------------------------------------

    def _build_extra_args(
        self,
        metadata: Optional[Dict[str, str]] = None,
        content_type: Optional[str] = None,
    ) -> Dict[str, Any]:
        """Build the ``ExtraArgs`` dictionary for S3 upload operations.

        Merges server-side encryption settings, user metadata, and
        content-type into a single dictionary suitable for passing to
        ``upload_file``, ``put_object``, and ``create_multipart_upload``.

        Args:
            metadata: Optional user-defined metadata key-value pairs.
            content_type: Optional MIME type override.

        Returns:
            Dictionary of S3 extra arguments with encryption, metadata,
            and content type fields set as appropriate.
        """
        extra_args: Dict[str, Any] = {}

        # Server-side encryption.
        if self._encryption_enabled:
            extra_args["ServerSideEncryption"] = self._encryption_method
            if (
                self._encryption_method == "aws:kms"
                and self._kms_key_id
            ):
                extra_args["SSEKMSKeyId"] = self._kms_key_id

        # User-defined metadata.
        if metadata:
            extra_args["Metadata"] = metadata

        # Content type.
        if content_type:
            extra_args["ContentType"] = content_type

        return extra_args

    def _build_object_url(self, key: str) -> str:
        """Construct the canonical HTTPS URL for an S3 object.

        For custom endpoints the URL uses the endpoint hostname; otherwise
        the standard virtual-hosted-style S3 URL is returned.

        Args:
            key: The S3 object key.

        Returns:
            The HTTPS URL of the object.
        """
        if self._endpoint_url:
            return f"{self._endpoint_url.rstrip('/')}/{self._bucket_name}/{key}"
        return (
            f"https://{self._bucket_name}.s3.{self._region}"
            f".amazonaws.com/{key}"
        )

    # ------------------------------------------------------------------
    # Upload operations
    # ------------------------------------------------------------------

    def upload(
        self,
        local_path: str,
        remote_key: str,
        metadata: Optional[Dict[str, str]] = None,
        content_type: Optional[str] = None,
    ) -> Dict[str, Any]:
        """Upload a local file to Amazon S3.

        Automatically uses multi-part upload when the file size exceeds
        :attr:`DEFAULT_MULTIPART_THRESHOLD`.  Server-side encryption is
        applied according to the configured encryption method.

        Args:
            local_path: Absolute or relative path to the local file.
            remote_key: Target S3 object key.  The configured prefix is
                prepended via :meth:`_build_remote_key`.
            metadata: Optional user-defined metadata to attach to the
                object.
            content_type: MIME type override.  When ``None``, the type is
                inferred from the file extension via
                :meth:`_detect_content_type`.

        Returns:
            Result dictionary with provider, bucket, key, size_bytes, url,
            encryption, and content_type fields.

        Raises:
            FileNotFoundError: If *local_path* does not exist on disk.
            RuntimeError: If the upload fails after all retry attempts.
        """
        self.ensure_initialized()

        # Resolve full remote key with tenant prefix.
        full_key = self._build_remote_key(remote_key)

        # Determine file size and content type.
        file_size = self._get_file_size(local_path)
        resolved_content_type = content_type or self._detect_content_type(
            local_path
        )

        extra_args = self._build_extra_args(
            metadata=metadata,
            content_type=resolved_content_type,
        )

        self._logger.info(
            "s3_upload_start",
            provider=self.PROVIDER_NAME,
            bucket=self._bucket_name,
            key=full_key,
            local_path=local_path,
            size_bytes=file_size,
            content_type=resolved_content_type,
            encryption=self._encryption_method,
        )

        def _do_upload() -> None:
            self._client.upload_file(
                Filename=local_path,
                Bucket=self._bucket_name,
                Key=full_key,
                ExtraArgs=extra_args,
                Config=self._transfer_config,
            )

        self._execute_with_retry(_do_upload)

        # Update operational metrics.
        self._total_bytes_uploaded += file_size
        self._total_operations += 1

        result: Dict[str, Any] = {
            "provider": self.PROVIDER_NAME,
            "bucket": self._bucket_name,
            "key": full_key,
            "size_bytes": file_size,
            "url": self._build_object_url(full_key),
            "encryption": self._encryption_method
            if self._encryption_enabled
            else "none",
            "content_type": resolved_content_type,
        }

        self._logger.info(
            "s3_upload_complete",
            provider=self.PROVIDER_NAME,
            bucket=self._bucket_name,
            key=full_key,
            size_bytes=file_size,
        )

        return result

    def upload_stream(
        self,
        stream: BinaryIO,
        remote_key: str,
        content_length: Optional[int] = None,
        metadata: Optional[Dict[str, str]] = None,
        content_type: Optional[str] = None,
    ) -> Dict[str, Any]:
        """Upload data from a binary stream to S3 using multi-part upload.

        When *content_length* is known and fits within the multi-part
        threshold, a single ``put_object`` call is used.  Otherwise a
        manual multi-part upload is initiated to stream data in configurable
        chunks.

        Args:
            stream: A readable binary file-like object.
            remote_key: Target S3 object key.  Prefix is prepended.
            content_length: Optional total content length in bytes.
            metadata: Optional user-defined metadata.
            content_type: MIME type override.  Defaults to
                ``"application/octet-stream"`` when ``None``.

        Returns:
            Result dictionary matching the :meth:`upload` return schema.

        Raises:
            RuntimeError: If the upload fails after all retry attempts.
        """
        self.ensure_initialized()

        full_key = self._build_remote_key(remote_key)
        resolved_content_type = content_type or "application/octet-stream"
        extra_args = self._build_extra_args(
            metadata=metadata,
            content_type=resolved_content_type,
        )

        self._logger.info(
            "s3_stream_upload_start",
            provider=self.PROVIDER_NAME,
            bucket=self._bucket_name,
            key=full_key,
            content_length=content_length,
            content_type=resolved_content_type,
        )

        # For small known payloads — single put_object call.
        if (
            content_length is not None
            and content_length <= self._multipart_threshold
        ):
            total_uploaded = self._upload_stream_single(
                stream, full_key, content_length, extra_args
            )
        else:
            # Multi-part upload for large or unknown-length streams.
            total_uploaded = self._upload_stream_multipart(
                stream, full_key, extra_args
            )

        self._total_bytes_uploaded += total_uploaded
        self._total_operations += 1

        result: Dict[str, Any] = {
            "provider": self.PROVIDER_NAME,
            "bucket": self._bucket_name,
            "key": full_key,
            "size_bytes": total_uploaded,
            "url": self._build_object_url(full_key),
            "encryption": self._encryption_method
            if self._encryption_enabled
            else "none",
            "content_type": resolved_content_type,
        }

        self._logger.info(
            "s3_stream_upload_complete",
            provider=self.PROVIDER_NAME,
            bucket=self._bucket_name,
            key=full_key,
            size_bytes=total_uploaded,
        )

        return result

    # ------------------------------------------------------------------
    # Internal streaming helpers
    # ------------------------------------------------------------------

    def _upload_stream_single(
        self,
        stream: BinaryIO,
        full_key: str,
        content_length: int,
        extra_args: Dict[str, Any],
    ) -> int:
        """Upload a stream in a single ``put_object`` call.

        Args:
            stream: Readable binary stream.
            full_key: Fully-qualified S3 object key.
            content_length: Total stream length in bytes.
            extra_args: S3 extra arguments (encryption, metadata, etc.).

        Returns:
            Number of bytes uploaded.
        """
        put_kwargs: Dict[str, Any] = {
            "Bucket": self._bucket_name,
            "Key": full_key,
            "Body": stream,
            "ContentLength": content_length,
        }
        put_kwargs.update(extra_args)

        def _do_put() -> None:
            self._client.put_object(**put_kwargs)

        self._execute_with_retry(_do_put)
        return content_length

    def _upload_stream_multipart(
        self,
        stream: BinaryIO,
        full_key: str,
        extra_args: Dict[str, Any],
    ) -> int:
        """Upload a stream via manual multi-part upload.

        Reads the stream in :attr:`_multipart_chunksize` blocks, uploading
        each as a separate S3 part.  If an error occurs mid-upload the
        multi-part upload is aborted to avoid leaving orphaned parts.

        Args:
            stream: Readable binary stream.
            full_key: Fully-qualified S3 object key.
            extra_args: S3 extra arguments (encryption, metadata, etc.).

        Returns:
            Total number of bytes uploaded.

        Raises:
            RuntimeError: If the multi-part upload fails.
        """
        # Initiate multi-part upload.
        mpu_kwargs: Dict[str, Any] = {
            "Bucket": self._bucket_name,
            "Key": full_key,
        }
        # Apply encryption and content type from extra_args.
        for key in (
            "ServerSideEncryption",
            "SSEKMSKeyId",
            "ContentType",
            "Metadata",
        ):
            if key in extra_args:
                mpu_kwargs[key] = extra_args[key]

        mpu_response = self._client.create_multipart_upload(**mpu_kwargs)
        upload_id: str = mpu_response["UploadId"]

        parts: List[Dict[str, Any]] = []
        part_number = 1
        total_bytes = 0

        try:
            while True:
                chunk = stream.read(self._multipart_chunksize)
                if not chunk:
                    break

                chunk_buffer = io.BytesIO(chunk)
                part_response = self._client.upload_part(
                    Bucket=self._bucket_name,
                    Key=full_key,
                    PartNumber=part_number,
                    UploadId=upload_id,
                    Body=chunk_buffer,
                    ContentLength=len(chunk),
                )

                parts.append(
                    {
                        "PartNumber": part_number,
                        "ETag": part_response["ETag"],
                    }
                )

                total_bytes += len(chunk)
                part_number += 1

                self._logger.debug(
                    "s3_multipart_part_uploaded",
                    key=full_key,
                    part_number=part_number - 1,
                    chunk_bytes=len(chunk),
                    total_bytes=total_bytes,
                )

            # Complete the multi-part upload.
            if parts:
                self._client.complete_multipart_upload(
                    Bucket=self._bucket_name,
                    Key=full_key,
                    UploadId=upload_id,
                    MultipartUpload={"Parts": parts},
                )
            else:
                # Edge case: empty stream — abort and do an empty put.
                self._client.abort_multipart_upload(
                    Bucket=self._bucket_name,
                    Key=full_key,
                    UploadId=upload_id,
                )
                put_args: Dict[str, Any] = {
                    "Bucket": self._bucket_name,
                    "Key": full_key,
                    "Body": b"",
                }
                put_args.update(extra_args)
                self._client.put_object(**put_args)

        except (ClientError, BotoCoreError, Exception) as exc:
            # Abort multi-part upload to avoid orphaned parts.
            self._logger.error(
                "s3_multipart_upload_abort",
                key=full_key,
                upload_id=upload_id,
                error=str(exc),
            )
            try:
                self._client.abort_multipart_upload(
                    Bucket=self._bucket_name,
                    Key=full_key,
                    UploadId=upload_id,
                )
            except (ClientError, BotoCoreError) as abort_exc:
                self._logger.warning(
                    "s3_multipart_abort_failed",
                    key=full_key,
                    upload_id=upload_id,
                    error=str(abort_exc),
                )
            raise RuntimeError(
                f"Multi-part upload failed for key '{full_key}': {exc}"
            ) from exc

        return total_bytes

    # ------------------------------------------------------------------
    # Download operations
    # ------------------------------------------------------------------

    def download(self, remote_key: str, local_path: str) -> Dict[str, Any]:
        """Download an S3 object to a local file.

        Uses the ``boto3`` managed transfer which automatically selects
        multi-part or single-part download based on object size.

        Args:
            remote_key: S3 object key to download.  Prefix is prepended.
            local_path: Destination file path on the local filesystem.
                The parent directory must exist.

        Returns:
            Result dictionary with provider, bucket, key, local_path,
            and size_bytes fields.

        Raises:
            FileNotFoundError: If the remote object does not exist.
            RuntimeError: If the download fails after retry attempts.
        """
        self.ensure_initialized()

        full_key = self._build_remote_key(remote_key)

        self._logger.info(
            "s3_download_start",
            provider=self.PROVIDER_NAME,
            bucket=self._bucket_name,
            key=full_key,
            local_path=local_path,
        )

        def _do_download() -> None:
            self._client.download_file(
                Bucket=self._bucket_name,
                Key=full_key,
                Filename=local_path,
                Config=self._transfer_config,
            )

        try:
            self._execute_with_retry(_do_download)
        except ClientError as exc:
            error_code = exc.response.get("Error", {}).get("Code", "")
            if error_code in ("404", "NoSuchKey"):
                raise FileNotFoundError(
                    f"S3 object not found: s3://{self._bucket_name}/{full_key}"
                ) from exc
            raise RuntimeError(
                f"S3 download failed for key '{full_key}': {exc}"
            ) from exc

        # Determine downloaded file size.
        downloaded_size = os.path.getsize(local_path)

        self._total_bytes_downloaded += downloaded_size
        self._total_operations += 1

        result: Dict[str, Any] = {
            "provider": self.PROVIDER_NAME,
            "bucket": self._bucket_name,
            "key": full_key,
            "local_path": local_path,
            "size_bytes": downloaded_size,
        }

        self._logger.info(
            "s3_download_complete",
            provider=self.PROVIDER_NAME,
            bucket=self._bucket_name,
            key=full_key,
            local_path=local_path,
            size_bytes=downloaded_size,
        )

        return result

    def download_stream(self, remote_key: str) -> BinaryIO:
        """Download an S3 object as a seekable in-memory binary stream.

        The S3 ``StreamingBody`` is read fully into a :class:`io.BytesIO`
        buffer so that the caller receives a seekable stream suitable for
        piping between services without writing to disk.

        Args:
            remote_key: S3 object key to download.  Prefix is prepended.

        Returns:
            A seekable :class:`io.BytesIO` containing the object data.

        Raises:
            FileNotFoundError: If the remote object does not exist.
            RuntimeError: If the download fails after retry attempts.
        """
        self.ensure_initialized()

        full_key = self._build_remote_key(remote_key)

        self._logger.info(
            "s3_stream_download_start",
            provider=self.PROVIDER_NAME,
            bucket=self._bucket_name,
            key=full_key,
        )

        def _do_get() -> Dict[str, Any]:
            return self._client.get_object(
                Bucket=self._bucket_name,
                Key=full_key,
            )

        try:
            response = self._execute_with_retry(_do_get)
        except ClientError as exc:
            error_code = exc.response.get("Error", {}).get("Code", "")
            if error_code in ("404", "NoSuchKey"):
                raise FileNotFoundError(
                    f"S3 object not found: s3://{self._bucket_name}/{full_key}"
                ) from exc
            raise RuntimeError(
                f"S3 download_stream failed for key '{full_key}': {exc}"
            ) from exc

        # Read the StreamingBody into a seekable BytesIO buffer.
        body_bytes: bytes = response["Body"].read()
        buffer = io.BytesIO(body_bytes)
        buffer.seek(0)

        downloaded_size = len(body_bytes)
        self._total_bytes_downloaded += downloaded_size
        self._total_operations += 1

        self._logger.info(
            "s3_stream_download_complete",
            provider=self.PROVIDER_NAME,
            bucket=self._bucket_name,
            key=full_key,
            size_bytes=downloaded_size,
        )

        return buffer

    # ------------------------------------------------------------------
    # List operations
    # ------------------------------------------------------------------

    def list_objects(
        self,
        prefix: Optional[str] = None,
        max_results: int = 1000,
    ) -> List[Dict[str, Any]]:
        """List objects in the S3 bucket, optionally filtered by prefix.

        Uses the S3 ``list_objects_v2`` paginator for efficient retrieval
        of large object listings.  Results are capped at *max_results*.

        Args:
            prefix: Optional key prefix filter.  Combined with the
                configured tenant prefix.  When ``None``, the tenant
                prefix alone is used (if configured).
            max_results: Maximum number of objects to return.  Defaults
                to ``1000``.

        Returns:
            A list of dictionaries, each containing ``key``, ``size_bytes``,
            ``last_modified`` (ISO 8601), and ``content_type`` fields.

        Raises:
            RuntimeError: If the listing fails after retries.
        """
        self.ensure_initialized()

        # Build the effective prefix, combining tenant prefix and caller
        # prefix.
        effective_prefix = self._build_remote_key(prefix or "")

        self._logger.info(
            "s3_list_objects_start",
            provider=self.PROVIDER_NAME,
            bucket=self._bucket_name,
            prefix=effective_prefix,
            max_results=max_results,
        )

        objects: List[Dict[str, Any]] = []

        def _do_list() -> List[Dict[str, Any]]:
            collected: List[Dict[str, Any]] = []
            paginator = self._client.get_paginator("list_objects_v2")

            page_config: Dict[str, Any] = {
                "Bucket": self._bucket_name,
                "MaxKeys": min(max_results, 1000),
            }
            if effective_prefix:
                page_config["Prefix"] = effective_prefix

            for page in paginator.paginate(**page_config):
                for obj in page.get("Contents", []):
                    collected.append(
                        {
                            "key": obj["Key"],
                            "size_bytes": obj.get("Size", 0),
                            "last_modified": obj["LastModified"].isoformat()
                            if hasattr(obj.get("LastModified"), "isoformat")
                            else str(obj.get("LastModified", "")),
                            "content_type": "application/octet-stream",
                        }
                    )
                    if len(collected) >= max_results:
                        return collected

            return collected

        try:
            objects = self._execute_with_retry(_do_list)
        except (ClientError, BotoCoreError) as exc:
            self._logger.error(
                "s3_list_objects_failed",
                provider=self.PROVIDER_NAME,
                bucket=self._bucket_name,
                prefix=effective_prefix,
                error=str(exc),
            )
            raise RuntimeError(
                f"S3 list_objects failed: {exc}"
            ) from exc

        self._total_operations += 1

        self._logger.info(
            "s3_list_objects_complete",
            provider=self.PROVIDER_NAME,
            bucket=self._bucket_name,
            prefix=effective_prefix,
            object_count=len(objects),
        )

        return objects

    # ------------------------------------------------------------------
    # Delete operations
    # ------------------------------------------------------------------

    def delete(self, remote_key: str) -> bool:
        """Delete a single object from S3.

        S3 delete operations are idempotent — deleting a non-existent key
        does **not** raise an error.  This method returns ``False`` only
        when the object was confirmed absent *before* the delete attempt.

        Args:
            remote_key: S3 object key to delete.  Prefix is prepended.

        Returns:
            ``True`` if the delete operation succeeded; ``False`` if the
            object was not found.

        Raises:
            RuntimeError: If the deletion fails for reasons other than
                "not found".
        """
        self.ensure_initialized()

        full_key = self._build_remote_key(remote_key)

        self._logger.info(
            "s3_delete_start",
            provider=self.PROVIDER_NAME,
            bucket=self._bucket_name,
            key=full_key,
        )

        # Check existence first for accurate return value.
        if not self.exists(remote_key):
            self._logger.warning(
                "s3_delete_not_found",
                provider=self.PROVIDER_NAME,
                bucket=self._bucket_name,
                key=full_key,
            )
            return False

        def _do_delete() -> None:
            self._client.delete_object(
                Bucket=self._bucket_name,
                Key=full_key,
            )

        try:
            self._execute_with_retry(_do_delete)
        except (ClientError, BotoCoreError) as exc:
            self._logger.error(
                "s3_delete_failed",
                provider=self.PROVIDER_NAME,
                bucket=self._bucket_name,
                key=full_key,
                error=str(exc),
            )
            raise RuntimeError(
                f"S3 delete failed for key '{full_key}': {exc}"
            ) from exc

        self._total_operations += 1

        self._logger.info(
            "s3_delete_complete",
            provider=self.PROVIDER_NAME,
            bucket=self._bucket_name,
            key=full_key,
        )

        return True

    def delete_many(self, remote_keys: List[str]) -> Dict[str, Any]:
        """Delete multiple S3 objects in a single batch operation.

        Uses S3's ``delete_objects`` API which supports up to 1000 keys
        per request.  For lists exceeding 1000 keys, the batch is
        automatically chunked.

        Args:
            remote_keys: List of S3 object keys to delete.  Prefix is
                prepended to each key.

        Returns:
            Dictionary with ``deleted`` (int) and ``errors`` (list of
            ``{"key": str, "error": str}`` dicts) summarising the result.

        Raises:
            RuntimeError: If the batch delete request fails entirely.
        """
        self.ensure_initialized()

        if not remote_keys:
            return {"deleted": 0, "errors": []}

        full_keys = [self._build_remote_key(k) for k in remote_keys]

        self._logger.info(
            "s3_delete_many_start",
            provider=self.PROVIDER_NAME,
            bucket=self._bucket_name,
            key_count=len(full_keys),
        )

        total_deleted = 0
        all_errors: List[Dict[str, str]] = []

        # S3 delete_objects accepts a max of 1000 keys per request.
        batch_size = 1000
        for i in range(0, len(full_keys), batch_size):
            batch = full_keys[i : i + batch_size]
            delete_payload: Dict[str, Any] = {
                "Objects": [{"Key": k} for k in batch],
                "Quiet": False,
            }

            def _do_batch_delete(
                payload: Dict[str, Any] = delete_payload,
            ) -> Dict[str, Any]:
                return self._client.delete_objects(
                    Bucket=self._bucket_name,
                    Delete=payload,
                )

            try:
                response = self._execute_with_retry(_do_batch_delete)
                total_deleted += len(response.get("Deleted", []))
                for err in response.get("Errors", []):
                    all_errors.append(
                        {
                            "key": err.get("Key", ""),
                            "error": err.get("Message", "Unknown error"),
                        }
                    )
            except (ClientError, BotoCoreError) as exc:
                self._logger.error(
                    "s3_delete_many_batch_failed",
                    provider=self.PROVIDER_NAME,
                    bucket=self._bucket_name,
                    batch_start=i,
                    batch_size=len(batch),
                    error=str(exc),
                )
                for k in batch:
                    all_errors.append({"key": k, "error": str(exc)})

        self._total_operations += 1

        result: Dict[str, Any] = {
            "deleted": total_deleted,
            "errors": all_errors,
        }

        self._logger.info(
            "s3_delete_many_complete",
            provider=self.PROVIDER_NAME,
            bucket=self._bucket_name,
            deleted=total_deleted,
            error_count=len(all_errors),
        )

        return result

    # ------------------------------------------------------------------
    # Metadata & existence checks
    # ------------------------------------------------------------------

    def exists(self, remote_key: str) -> bool:
        """Check whether an S3 object exists.

        Performs a lightweight ``head_object`` call that retrieves only
        metadata (no data transfer).

        Args:
            remote_key: S3 object key to check.  Prefix is prepended.

        Returns:
            ``True`` if the object exists; ``False`` otherwise.

        Raises:
            RuntimeError: If the check fails due to a non-404 error.
        """
        self.ensure_initialized()

        full_key = self._build_remote_key(remote_key)

        try:
            self._client.head_object(
                Bucket=self._bucket_name,
                Key=full_key,
            )
            return True
        except ClientError as exc:
            error_code = exc.response.get("Error", {}).get("Code", "")
            if error_code in ("404", "NoSuchKey"):
                return False
            self._logger.error(
                "s3_exists_check_failed",
                provider=self.PROVIDER_NAME,
                bucket=self._bucket_name,
                key=full_key,
                error=str(exc),
            )
            raise RuntimeError(
                f"S3 existence check failed for key '{full_key}': {exc}"
            ) from exc
        except BotoCoreError as exc:
            self._logger.error(
                "s3_exists_check_failed",
                provider=self.PROVIDER_NAME,
                bucket=self._bucket_name,
                key=full_key,
                error=str(exc),
            )
            raise RuntimeError(
                f"S3 existence check failed for key '{full_key}': {exc}"
            ) from exc

    def get_metadata(self, remote_key: str) -> Dict[str, Any]:
        """Retrieve metadata for an S3 object without downloading its body.

        Uses ``head_object`` to fetch content type, size, last-modified
        timestamp, ETag, user metadata, and encryption details.

        Args:
            remote_key: S3 object key.  Prefix is prepended.

        Returns:
            Metadata dictionary with ``key``, ``size_bytes``,
            ``content_type``, ``last_modified``, ``etag``, ``metadata``,
            and ``encryption`` fields.

        Raises:
            FileNotFoundError: If the object does not exist.
            RuntimeError: If the metadata request fails.
        """
        self.ensure_initialized()

        full_key = self._build_remote_key(remote_key)

        self._logger.info(
            "s3_get_metadata",
            provider=self.PROVIDER_NAME,
            bucket=self._bucket_name,
            key=full_key,
        )

        def _do_head() -> Dict[str, Any]:
            return self._client.head_object(
                Bucket=self._bucket_name,
                Key=full_key,
            )

        try:
            response = self._execute_with_retry(_do_head)
        except ClientError as exc:
            error_code = exc.response.get("Error", {}).get("Code", "")
            if error_code in ("404", "NoSuchKey"):
                raise FileNotFoundError(
                    f"S3 object not found: s3://{self._bucket_name}/{full_key}"
                ) from exc
            raise RuntimeError(
                f"S3 get_metadata failed for key '{full_key}': {exc}"
            ) from exc

        last_modified = response.get("LastModified")
        last_modified_str = (
            last_modified.isoformat()
            if hasattr(last_modified, "isoformat")
            else str(last_modified or "")
        )

        self._total_operations += 1

        return {
            "key": full_key,
            "size_bytes": response.get("ContentLength", 0),
            "content_type": response.get(
                "ContentType", "application/octet-stream"
            ),
            "last_modified": last_modified_str,
            "etag": response.get("ETag", "").strip('"'),
            "metadata": response.get("Metadata", {}),
            "encryption": response.get(
                "ServerSideEncryption", "none"
            ),
        }

    # ------------------------------------------------------------------
    # Presigned URLs
    # ------------------------------------------------------------------

    def generate_presigned_url(
        self,
        remote_key: str,
        expiration: int = 3600,
        http_method: str = "GET",
    ) -> str:
        """Generate a pre-signed URL for temporary access to an S3 object.

        Pre-signed URLs are useful for granting time-limited access to
        exported synthetic datasets without exposing AWS credentials.

        Args:
            remote_key: S3 object key.  Prefix is prepended.
            expiration: URL validity period in seconds.  Default ``3600``
                (1 hour).
            http_method: HTTP method the URL should permit.  ``"GET"``
                (default) for downloads, ``"PUT"`` for uploads.

        Returns:
            The pre-signed HTTPS URL string.

        Raises:
            RuntimeError: If URL generation fails.
        """
        self.ensure_initialized()

        full_key = self._build_remote_key(remote_key)

        # Map human-friendly HTTP method to S3 client method name.
        client_method_map: Dict[str, str] = {
            "GET": "get_object",
            "PUT": "put_object",
        }
        client_method = client_method_map.get(
            http_method.upper(), "get_object"
        )

        self._logger.info(
            "s3_presigned_url_generate",
            provider=self.PROVIDER_NAME,
            bucket=self._bucket_name,
            key=full_key,
            expiration=expiration,
            http_method=http_method,
        )

        try:
            url: str = self._client.generate_presigned_url(
                ClientMethod=client_method,
                Params={
                    "Bucket": self._bucket_name,
                    "Key": full_key,
                },
                ExpiresIn=expiration,
            )
        except (ClientError, BotoCoreError) as exc:
            self._logger.error(
                "s3_presigned_url_failed",
                provider=self.PROVIDER_NAME,
                bucket=self._bucket_name,
                key=full_key,
                error=str(exc),
            )
            raise RuntimeError(
                f"Failed to generate pre-signed URL for '{full_key}': {exc}"
            ) from exc

        self._total_operations += 1

        self._logger.info(
            "s3_presigned_url_generated",
            provider=self.PROVIDER_NAME,
            bucket=self._bucket_name,
            key=full_key,
            expiration=expiration,
        )

        return url

    # ------------------------------------------------------------------
    # Health check
    # ------------------------------------------------------------------

    def health_check(self) -> Dict[str, Any]:
        """Verify S3 connectivity by performing a ``head_bucket`` call.

        Measures the round-trip latency and returns a structured health
        report suitable for Kubernetes readiness probes and monitoring
        dashboards.

        Returns:
            Dictionary with ``status`` (``"healthy"`` or ``"unhealthy"``),
            ``provider``, ``bucket``, ``latency_ms``,
            ``encryption_enabled``, and optionally ``error`` fields.
        """
        self._logger.info(
            "s3_health_check_start",
            provider=self.PROVIDER_NAME,
            bucket=self._bucket_name,
        )

        try:
            def _do_head_bucket() -> None:
                self._client.head_bucket(Bucket=self._bucket_name)

            _, latency_ms = self._measure_latency(_do_head_bucket)

            result: Dict[str, Any] = {
                "status": "healthy",
                "provider": self.PROVIDER_NAME,
                "bucket": self._bucket_name,
                "region": self._region,
                "latency_ms": round(latency_ms, 2),
                "encryption_enabled": self._encryption_enabled,
                "error": None,
            }

            self._logger.info(
                "s3_health_check_healthy",
                provider=self.PROVIDER_NAME,
                bucket=self._bucket_name,
                latency_ms=round(latency_ms, 2),
            )

            return result

        except (ClientError, BotoCoreError, Exception) as exc:
            self._logger.error(
                "s3_health_check_unhealthy",
                provider=self.PROVIDER_NAME,
                bucket=self._bucket_name,
                error=str(exc),
            )

            return {
                "status": "unhealthy",
                "provider": self.PROVIDER_NAME,
                "bucket": self._bucket_name,
                "region": self._region,
                "latency_ms": -1.0,
                "encryption_enabled": self._encryption_enabled,
                "error": str(exc),
            }
