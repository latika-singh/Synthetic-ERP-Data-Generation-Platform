"""File export orchestration module for the Provisioning Service.

This module provides the :class:`FileExporter` class which manages the complete
lifecycle of exporting generated synthetic data to files in multiple output
formats — CSV, JSON, JSON Lines (JSONL), Apache Parquet, and SQL.  It is the
primary file-delivery component within the Synthetic ERP Data Generation
Platform's Provisioning Service.

Key Capabilities:
    * **Multi-format export** — CSV (RFC 4180), JSON arrays, JSON Lines,
      Apache Parquet (columnar), and SQL INSERT statements (F-009).
    * **Streaming writes** — Data is consumed from a :class:`Generator` in
      configurable batches (default 10 000 records) so that arbitrarily large
      datasets can be exported without full memory materialisation.
    * **Compression** — Gzip for text-based formats (CSV, JSON, JSONL, SQL)
      and Snappy (built-in) for Parquet.
    * **AES-256-GCM encryption** — Every exported file can be encrypted at
      rest with a 256-bit key and random 96-bit nonce, satisfying SOC 2
      Type II (C-004) and the platform's encryption requirements.
    * **SHA-256 integrity checksums** — Tamper-evident hash included in the
      export result for audit trail purposes.
    * **Cloud storage delivery** — Seamless upload to AWS S3, Azure Blob
      Storage, or GCP Cloud Storage via the cloud provider abstraction layer.
    * **Redis-backed progress tracking** — Real-time export progress
      published to Redis so the Web Console can display live progress bars.
    * **Multi-tenant isolation** — Tenant-scoped temporary directories
      prevent cross-tenant data leakage on shared worker nodes.
    * **Air-gapped readiness** (C-003) — All operations function with local
      file-system targets; cloud upload is optional.

Architecture:
    ``FileExporter`` delegates format-specific serialisation to private
    methods (``_export_csv``, ``_export_json``, etc.) while the public
    :meth:`export_to_file` and :meth:`export_to_cloud` methods orchestrate
    the full pipeline: serialise → compress → encrypt → checksum → upload.

Usage::

    from provisioning_service.config import ProvisioningServiceConfig
    from provisioning_service.exporters.file_exporter import FileExporter

    config = ProvisioningServiceConfig()
    exporter = FileExporter(config)

    result = exporter.export_to_file(
        data=record_generator(),
        format="parquet",
        output_path="/exports/output.parquet",
        schema=schema_def,
        job_id="job-abc-123",
        tenant_id="tenant-42",
    )

See Also:
    :mod:`provisioning_service.exporters.database_exporter` for direct
    database provisioning via JDBC connectors.
    :mod:`provisioning_service.cloud` for cloud storage provider
    implementations.
"""

from __future__ import annotations

import csv
import gzip
import hashlib
import io
import json
import os
import shutil
import tempfile
import time
from pathlib import Path
from typing import Any, BinaryIO, Dict, Generator, List, Optional, Tuple, Union

from cryptography.hazmat.primitives.ciphers.aead import AESGCM

import pyarrow as pa
import pyarrow.parquet as pq

from shared.database.redis_client import get_redis_client
from shared.logging.structured_logger import get_logger

from provisioning_service.cloud import BaseCloudProvider, get_cloud_provider
from provisioning_service.config import ProvisioningServiceConfig


# ---------------------------------------------------------------------------
# Module-level constants
# ---------------------------------------------------------------------------

SUPPORTED_FORMATS: set[str] = {"csv", "json", "jsonl", "parquet", "sql"}
"""Set of file formats supported by :class:`FileExporter`.

Accepted values (case-insensitive in :meth:`FileExporter.export_to_file`):

* ``csv``     — Comma-separated values (RFC 4180)
* ``json``    — JSON array (``[ {...}, {...} ]``)
* ``jsonl``   — JSON Lines (one JSON object per line)
* ``parquet`` — Apache Parquet columnar format
* ``sql``     — SQL INSERT / COPY statements
"""

# Chunk sizes used for I/O-bound operations.
_READ_CHUNK_SIZE: int = 65_536  # 64 KB for checksum / compression reads
_ENCRYPT_CHUNK_LIMIT: int = 256 * 1024 * 1024  # 256 MB max in-memory encrypt buffer

# Redis key namespace for export progress tracking.
_PROGRESS_KEY_PREFIX: str = "export:progress:"
_PROGRESS_TTL_SECONDS: int = 3600  # 1 hour

# SQL generation constants.
_SQL_BATCH_COMMIT_INTERVAL: int = 1000  # COMMIT every N records in SQL output


# ============================================================================
# FileExporter
# ============================================================================


class FileExporter:
    """Orchestrates file-based export of generated synthetic data.

    The exporter consumes records from a :class:`~typing.Generator`, writes
    them to a local temporary file in the requested format, optionally
    compresses and encrypts the output, calculates a SHA-256 checksum, and
    (when requested) uploads the result to cloud object storage.

    All temporary files are written to a tenant-scoped subdirectory beneath
    :attr:`ProvisioningServiceConfig.EXPORT_TEMP_DIR` so that concurrent
    exports for different tenants are isolated on the filesystem.

    Args:
        config: A fully-initialised :class:`ProvisioningServiceConfig`
            instance providing export settings (batch size, temp directory,
            compression/encryption toggles, encryption key).

    Attributes:
        config: Reference to the service configuration.
        export_temp_dir: Base temporary directory for export artefacts.
        max_file_size_mb: Maximum allowed output file size in megabytes.
        compression_enabled: Whether gzip compression is applied.
        encryption_enabled: Whether AES-256-GCM encryption is applied.
        batch_size: Number of records buffered per streaming write batch.

    Example::

        config = ProvisioningServiceConfig()
        exporter = FileExporter(config)

        result = exporter.export_to_file(
            data=my_generator(),
            format="csv",
            output_path="/data/export.csv",
            schema={"table_name": "gl_entries", "columns": [...]},
            job_id="job-001",
            tenant_id="tenant-42",
        )
        print(result["checksum_sha256"])
    """

    # ------------------------------------------------------------------
    # Initialisation
    # ------------------------------------------------------------------

    def __init__(self, config: ProvisioningServiceConfig) -> None:
        """Initialise the FileExporter with service configuration.

        Creates the base temporary export directory if it does not exist
        and prepares internal state for streaming export operations.

        Args:
            config: Service configuration providing export-related settings
                including ``EXPORT_TEMP_DIR``, ``EXPORT_BATCH_SIZE``,
                ``EXPORT_MAX_FILE_SIZE_MB``, ``EXPORT_COMPRESSION_ENABLED``,
                ``EXPORT_ENCRYPTION_ENABLED``, and ``ENCRYPTION_KEY``.
        """
        self.config: ProvisioningServiceConfig = config
        self._logger = get_logger("file_exporter")
        self._redis = get_redis_client()

        # Export behaviour settings from config.
        self.export_temp_dir: str = config.EXPORT_TEMP_DIR
        self.max_file_size_mb: int = config.EXPORT_MAX_FILE_SIZE_MB
        self.compression_enabled: bool = config.EXPORT_COMPRESSION_ENABLED
        self.encryption_enabled: bool = config.EXPORT_ENCRYPTION_ENABLED
        self.batch_size: int = config.EXPORT_BATCH_SIZE

        # Ensure the base temp directory exists.
        os.makedirs(self.export_temp_dir, exist_ok=True)

        # Cloud provider — lazily initialised on first cloud export.
        self._cloud_provider: Optional[BaseCloudProvider] = None

        self._logger.info(
            "file_exporter_initialised",
            export_temp_dir=self.export_temp_dir,
            batch_size=self.batch_size,
            compression_enabled=self.compression_enabled,
            encryption_enabled=self.encryption_enabled,
            max_file_size_mb=self.max_file_size_mb,
        )

    # ------------------------------------------------------------------
    # Public API — export_to_file
    # ------------------------------------------------------------------

    def export_to_file(
        self,
        data: Generator,
        format: str,
        output_path: str,
        schema: Dict[str, Any],
        job_id: str,
        tenant_id: str,
        options: Optional[Dict[str, Any]] = None,
    ) -> Dict[str, Any]:
        """Export synthetic data to a local file in the specified format.

        This is the primary entry point for file-based export.  The method:

        1. Validates the requested *format*.
        2. Creates a tenant-scoped temporary directory.
        3. Dispatches to the appropriate format-specific handler.
        4. Optionally compresses the output (gzip for text formats).
        5. Optionally encrypts the output (AES-256-GCM).
        6. Calculates a SHA-256 checksum for integrity verification.
        7. Moves/copies the final artefact to *output_path*.
        8. Reports progress to Redis throughout.

        Args:
            data: A generator yielding ``dict`` records to export.
            format: Output format — one of ``'csv'``, ``'json'``,
                ``'jsonl'``, ``'parquet'``, ``'sql'``.
            output_path: Destination path for the final exported file.
            schema: Schema definition dictionary containing at minimum
                ``table_name`` (str) and ``columns`` (list of column defs).
            job_id: Unique identifier of the generation job.
            tenant_id: Identifier of the owning tenant for path isolation.
            options: Optional configuration overrides.  Recognised keys:

                * ``delimiter`` (str) — CSV delimiter (default ``','``).
                * ``indent`` (int | None) — JSON indentation level.
                * ``include_ddl`` (bool) — Include CREATE TABLE in SQL.
                * ``compression`` (str) — Compression algorithm override.
                * ``parquet_compression`` (str) — Parquet-specific codec.

        Returns:
            A dictionary describing the export result::

                {
                    "format": str,
                    "output_path": str,
                    "size_bytes": int,
                    "checksum_sha256": str,
                    "compressed": bool,
                    "encrypted": bool,
                    "records_exported": int,
                    "duration_seconds": float,
                }

        Raises:
            ValueError: If *format* is not in :data:`SUPPORTED_FORMATS`.
            OSError: If the temporary or output directory cannot be created.
            RuntimeError: If the export operation fails unexpectedly.
        """
        start_time: float = time.time()
        fmt: str = format.strip().lower()

        # Validate format.
        if fmt not in SUPPORTED_FORMATS:
            raise ValueError(
                f"Unsupported export format: '{format}'. "
                f"Supported formats: {sorted(SUPPORTED_FORMATS)}"
            )

        self._logger.info(
            "export_to_file_started",
            format=fmt,
            output_path=output_path,
            job_id=job_id,
            tenant_id=tenant_id,
        )

        # Create tenant-scoped temporary directory (resolve to absolute path).
        tenant_temp_dir: Path = (
            Path(self.export_temp_dir) / tenant_id / job_id
        ).resolve()
        tenant_temp_dir.mkdir(parents=True, exist_ok=True)

        # Determine the temporary file name based on format.
        temp_filename: str = f"export_{job_id}.{fmt}"
        temp_path: str = str(tenant_temp_dir / temp_filename)

        options = options or {}

        # Update initial progress.
        self._update_progress(job_id, records_exported=0, status="exporting")

        try:
            # Dispatch to format-specific handler.
            format_handlers: Dict[str, Any] = {
                "csv": self._export_csv,
                "json": self._export_json,
                "jsonl": self._export_jsonl,
                "parquet": self._export_parquet,
                "sql": self._export_sql,
            }
            handler = format_handlers[fmt]
            temp_path, records_written = handler(
                data, temp_path, schema, job_id, options
            )

            compressed: bool = False
            encrypted: bool = False

            # Apply compression if enabled (skip Parquet — uses internal compression).
            if self.compression_enabled and fmt != "parquet":
                algorithm: str = options.get("compression", "gzip")
                temp_path = self._compress_file(temp_path, algorithm=algorithm)
                compressed = True

            # Apply AES-256-GCM encryption if enabled.
            if self.encryption_enabled:
                encryption_key_str: str = self.config.ENCRYPTION_KEY
                encryption_key: Optional[bytes] = None
                if encryption_key_str:
                    # Derive a 32-byte key from the configured key string.
                    encryption_key = hashlib.sha256(
                        encryption_key_str.encode("utf-8")
                    ).digest()
                temp_path = self._encrypt_file(temp_path, encryption_key)
                encrypted = True

            # Calculate SHA-256 checksum for integrity verification.
            checksum: str = self._calculate_checksum(temp_path)

            # Move the final file to the requested output path.
            output_dir: str = os.path.dirname(output_path)
            if output_dir:
                os.makedirs(output_dir, exist_ok=True)
            shutil.move(temp_path, output_path)

            final_size: int = os.path.getsize(output_path)
            duration: float = time.time() - start_time

            # Final progress update.
            self._update_progress(
                job_id,
                records_exported=records_written,
                status="completed",
            )

            result: Dict[str, Any] = {
                "format": fmt,
                "output_path": output_path,
                "size_bytes": final_size,
                "checksum_sha256": checksum,
                "compressed": compressed,
                "encrypted": encrypted,
                "records_exported": records_written,
                "duration_seconds": round(duration, 3),
            }

            self._logger.info(
                "export_to_file_completed",
                format=fmt,
                records_exported=records_written,
                size_bytes=final_size,
                duration_seconds=round(duration, 3),
                compressed=compressed,
                encrypted=encrypted,
                job_id=job_id,
                tenant_id=tenant_id,
            )

            return result

        except Exception as exc:
            duration = time.time() - start_time
            self._update_progress(
                job_id, records_exported=0, status="failed"
            )
            self._logger.error(
                "export_to_file_failed",
                format=fmt,
                job_id=job_id,
                tenant_id=tenant_id,
                error=str(exc),
                duration_seconds=round(duration, 3),
            )
            raise RuntimeError(
                f"File export failed for job '{job_id}' in format '{fmt}': {exc}"
            ) from exc

    # ------------------------------------------------------------------
    # Public API — export_to_cloud
    # ------------------------------------------------------------------

    def export_to_cloud(
        self,
        data: Generator,
        format: str,
        remote_key: str,
        schema: Dict[str, Any],
        job_id: str,
        tenant_id: str,
        cloud_config: Dict[str, Any],
        options: Optional[Dict[str, Any]] = None,
    ) -> Dict[str, Any]:
        """Export synthetic data and upload to cloud object storage.

        Combines :meth:`export_to_file` with a cloud upload step.  The data
        is first written to a local temporary file (compressed, encrypted,
        and checksummed), then uploaded to the specified cloud provider, and
        the local temporary file is cleaned up.

        Args:
            data: A generator yielding ``dict`` records.
            format: Output format string (see :meth:`export_to_file`).
            remote_key: Object key / blob name in the cloud bucket.
            schema: Schema definition dictionary.
            job_id: Unique job identifier.
            tenant_id: Owning tenant identifier.
            cloud_config: Cloud provider configuration dictionary.  Must
                contain at least a ``provider`` key (``'s3'``, ``'azure'``,
                ``'gcs'``) plus provider-specific credentials.
            options: Optional export configuration overrides.

        Returns:
            A dictionary merging the file export result with cloud upload
            metadata::

                {
                    "format": str,
                    "output_path": str,           # local temp path
                    "size_bytes": int,
                    "checksum_sha256": str,
                    "compressed": bool,
                    "encrypted": bool,
                    "records_exported": int,
                    "duration_seconds": float,
                    "cloud_upload": {
                        "provider": str,
                        "bucket": str,
                        "key": str,
                        "url": str,
                        ...
                    },
                }

        Raises:
            ValueError: If *cloud_config* is missing the ``provider`` key
                or specifies an unsupported provider type.
            RuntimeError: If the export or upload fails.
        """
        start_time: float = time.time()

        self._logger.info(
            "export_to_cloud_started",
            format=format,
            remote_key=remote_key,
            job_id=job_id,
            tenant_id=tenant_id,
            provider=cloud_config.get("provider", "unknown"),
        )

        # Validate cloud configuration.
        provider_type: str = cloud_config.get("provider", "")
        if not provider_type:
            raise ValueError(
                "cloud_config must include a 'provider' key specifying the "
                "target cloud platform (e.g. 's3', 'azure', 'gcs')."
            )

        # Create a unique local temp path for the intermediate file.
        temp_dir: str = tempfile.mkdtemp(
            prefix=f"cloud_export_{job_id}_",
            dir=self.export_temp_dir,
        )
        # Use NamedTemporaryFile for secure intermediate file creation,
        # ensuring OS-level atomicity and proper permission handling.
        with tempfile.NamedTemporaryFile(
            suffix=f".{format.strip().lower()}",
            prefix=f"export_{job_id}_",
            dir=temp_dir,
            delete=False,
        ) as ntf:
            local_path: str = ntf.name

        try:
            # Step 1: Export to local temp file.
            file_result: Dict[str, Any] = self.export_to_file(
                data=data,
                format=format,
                output_path=local_path,
                schema=schema,
                job_id=job_id,
                tenant_id=tenant_id,
                options=options,
            )

            # Step 2: Initialise the cloud provider and upload.
            self._update_progress(
                job_id,
                records_exported=file_result["records_exported"],
                status="uploading",
            )

            cloud_provider: BaseCloudProvider = get_cloud_provider(
                provider_type, cloud_config
            )

            upload_metadata: Dict[str, str] = {
                "tenant_id": tenant_id,
                "job_id": job_id,
                "format": format,
                "checksum_sha256": file_result["checksum_sha256"],
            }

            upload_result: Dict[str, Any] = cloud_provider.upload(
                local_path=local_path,
                remote_key=remote_key,
                metadata=upload_metadata,
            )

            duration: float = time.time() - start_time

            # Merge file export result with cloud upload details.
            result: Dict[str, Any] = {
                **file_result,
                "cloud_upload": upload_result,
                "duration_seconds": round(duration, 3),
            }

            self._update_progress(
                job_id,
                records_exported=file_result["records_exported"],
                status="completed",
            )

            self._logger.info(
                "export_to_cloud_completed",
                format=format,
                remote_key=remote_key,
                records_exported=file_result["records_exported"],
                size_bytes=file_result["size_bytes"],
                duration_seconds=round(duration, 3),
                provider=provider_type,
                job_id=job_id,
                tenant_id=tenant_id,
            )

            return result

        except Exception as exc:
            duration = time.time() - start_time
            self._update_progress(
                job_id, records_exported=0, status="failed"
            )
            self._logger.error(
                "export_to_cloud_failed",
                format=format,
                remote_key=remote_key,
                job_id=job_id,
                tenant_id=tenant_id,
                error=str(exc),
                duration_seconds=round(duration, 3),
            )
            raise RuntimeError(
                f"Cloud export failed for job '{job_id}': {exc}"
            ) from exc

        finally:
            # Clean up the local temporary directory regardless of outcome.
            if os.path.exists(temp_dir):
                try:
                    shutil.rmtree(temp_dir)
                except OSError as cleanup_err:
                    self._logger.warning(
                        "cloud_export_temp_cleanup_failed",
                        temp_dir=temp_dir,
                        error=str(cleanup_err),
                    )

    # ------------------------------------------------------------------
    # Format-specific export handlers (private)
    # ------------------------------------------------------------------

    def _export_csv(
        self,
        data: Generator,
        output_path: str,
        schema: Dict[str, Any],
        job_id: str,
        options: Optional[Dict[str, Any]] = None,
    ) -> Tuple[str, int]:
        """Export data to CSV format with streaming writes.

        Writes a header row followed by data rows using the standard
        library :mod:`csv` module.  The delimiter, quoting mode, and
        encoding are configurable via *options*.

        Args:
            data: Generator yielding ``dict`` records.
            output_path: Destination file path.
            schema: Schema definition with ``columns`` list.
            job_id: Job identifier for progress tracking.
            options: Optional overrides (``delimiter``, ``quoting``).

        Returns:
            A tuple of ``(output_path, records_written)``.
        """
        options = options or {}
        delimiter: str = options.get("delimiter", ",")
        quoting: int = options.get("quoting", csv.QUOTE_MINIMAL)
        columns: List[str] = self._extract_column_names(schema)

        records_written: int = 0

        self._logger.info(
            "export_csv_started",
            output_path=output_path,
            job_id=job_id,
            column_count=len(columns),
        )

        with open(output_path, "w", newline="", encoding="utf-8") as f:
            writer = csv.writer(f, delimiter=delimiter, quoting=quoting)

            # Write header row.
            writer.writerow(columns)

            for record in data:
                row: List[Any] = [record.get(col) for col in columns]
                writer.writerow(row)
                records_written += 1

                if records_written % self.batch_size == 0:
                    self._update_progress(job_id, records_written)

        self._logger.info(
            "export_csv_completed",
            output_path=output_path,
            job_id=job_id,
            records_written=records_written,
        )

        return output_path, records_written

    def _export_json(
        self,
        data: Generator,
        output_path: str,
        schema: Dict[str, Any],
        job_id: str,
        options: Optional[Dict[str, Any]] = None,
    ) -> Tuple[str, int]:
        """Export data as a JSON array.

        Produces a JSON document structured as ``[ {...}, {...}, ... ]``
        with configurable indentation.  Records are streamed to avoid
        holding the full array in memory.

        Args:
            data: Generator yielding ``dict`` records.
            output_path: Destination file path.
            schema: Schema definition (used for logging context).
            job_id: Job identifier for progress tracking.
            options: Optional overrides (``indent``).

        Returns:
            A tuple of ``(output_path, records_written)``.
        """
        options = options or {}
        indent: Optional[int] = options.get("indent")
        records_written: int = 0

        self._logger.info(
            "export_json_started",
            output_path=output_path,
            job_id=job_id,
        )

        with open(output_path, "w", encoding="utf-8") as f:
            f.write("[\n")

            for record in data:
                if records_written > 0:
                    f.write(",\n")

                serialised: str = json.dumps(
                    record,
                    ensure_ascii=False,
                    indent=indent,
                    default=str,
                )
                f.write(serialised)
                records_written += 1

                if records_written % self.batch_size == 0:
                    self._update_progress(job_id, records_written)

            f.write("\n]")

        self._logger.info(
            "export_json_completed",
            output_path=output_path,
            job_id=job_id,
            records_written=records_written,
        )

        return output_path, records_written

    def _export_jsonl(
        self,
        data: Generator,
        output_path: str,
        schema: Dict[str, Any],
        job_id: str,
        options: Optional[Dict[str, Any]] = None,
    ) -> Tuple[str, int]:
        """Export data in JSON Lines format (one JSON object per line).

        Each record is serialised as a single-line JSON string terminated
        by a newline character, enabling line-by-line streaming reads by
        downstream consumers.

        Args:
            data: Generator yielding ``dict`` records.
            output_path: Destination file path.
            schema: Schema definition (used for logging context).
            job_id: Job identifier for progress tracking.
            options: Optional overrides (currently unused).

        Returns:
            A tuple of ``(output_path, records_written)``.
        """
        records_written: int = 0

        self._logger.info(
            "export_jsonl_started",
            output_path=output_path,
            job_id=job_id,
        )

        with open(output_path, "w", encoding="utf-8") as f:
            for record in data:
                line: str = json.dumps(
                    record,
                    ensure_ascii=False,
                    default=str,
                )
                f.write(line + "\n")
                records_written += 1

                if records_written % self.batch_size == 0:
                    self._update_progress(job_id, records_written)

        self._logger.info(
            "export_jsonl_completed",
            output_path=output_path,
            job_id=job_id,
            records_written=records_written,
        )

        return output_path, records_written

    def _export_parquet(
        self,
        data: Generator,
        output_path: str,
        schema: Dict[str, Any],
        job_id: str,
        options: Optional[Dict[str, Any]] = None,
    ) -> Tuple[str, int]:
        """Export data to Apache Parquet columnar format.

        Uses PyArrow's :class:`~pyarrow.parquet.ParquetWriter` for
        streaming batch writes with configurable compression (Snappy
        default).  Records are buffered into :class:`~pyarrow.RecordBatch`
        objects of ``batch_size`` records before being flushed to disk.

        Args:
            data: Generator yielding ``dict`` records.
            output_path: Destination file path.
            schema: Schema definition with ``columns`` providing type
                information for PyArrow schema construction.
            job_id: Job identifier for progress tracking.
            options: Optional overrides.  Recognised keys:

                * ``parquet_compression`` (str) — Compression codec.
                  Accepted values: ``'snappy'``, ``'gzip'``, ``'zstd'``,
                  ``'lz4'``, ``'none'``.  Default: ``'snappy'``.

        Returns:
            A tuple of ``(output_path, records_written)``.
        """
        options = options or {}
        compression: str = options.get("parquet_compression", "snappy")
        arrow_schema: pa.Schema = self._build_parquet_schema(schema)
        column_names: List[str] = self._extract_column_names(schema)

        records_written: int = 0
        batch_buffer: List[Dict[str, Any]] = []

        self._logger.info(
            "export_parquet_started",
            output_path=output_path,
            job_id=job_id,
            compression=compression,
            column_count=len(column_names),
        )

        writer: Optional[pq.ParquetWriter] = None
        try:
            writer = pq.ParquetWriter(
                output_path,
                schema=arrow_schema,
                compression=compression,
            )

            for record in data:
                batch_buffer.append(record)
                records_written += 1

                if len(batch_buffer) >= self.batch_size:
                    self._write_parquet_batch(
                        writer, batch_buffer, column_names, arrow_schema
                    )
                    batch_buffer = []
                    self._update_progress(job_id, records_written)

            # Flush remaining records.
            if batch_buffer:
                self._write_parquet_batch(
                    writer, batch_buffer, column_names, arrow_schema
                )

        finally:
            if writer is not None:
                writer.close()

        self._logger.info(
            "export_parquet_completed",
            output_path=output_path,
            job_id=job_id,
            records_written=records_written,
            compression=compression,
        )

        return output_path, records_written

    def _export_sql(
        self,
        data: Generator,
        output_path: str,
        schema: Dict[str, Any],
        job_id: str,
        options: Optional[Dict[str, Any]] = None,
    ) -> Tuple[str, int]:
        """Export data as SQL INSERT statements.

        Generates standard SQL ``INSERT INTO ... VALUES (...)`` statements
        with proper value escaping.  Optionally includes a ``CREATE TABLE``
        DDL preamble and wraps batches in explicit transactions.

        Args:
            data: Generator yielding ``dict`` records.
            output_path: Destination file path.
            schema: Schema definition with ``table_name`` and ``columns``.
            job_id: Job identifier for progress tracking.
            options: Optional overrides.  Recognised keys:

                * ``include_ddl`` (bool) — Prepend CREATE TABLE statement.
                * ``dialect`` (str) — SQL dialect hint (``'postgresql'``,
                  ``'mysql'``, ``'generic'``).  Default: ``'generic'``.
                * ``batch_commit_interval`` (int) — COMMIT frequency.

        Returns:
            A tuple of ``(output_path, records_written)``.
        """
        options = options or {}
        include_ddl: bool = options.get("include_ddl", False)
        dialect: str = options.get("dialect", "generic")
        commit_interval: int = options.get(
            "batch_commit_interval", _SQL_BATCH_COMMIT_INTERVAL
        )

        table_name: str = schema.get("table_name", "exported_data")
        columns: List[Dict[str, Any]] = schema.get("columns", [])
        column_names: List[str] = self._extract_column_names(schema)

        records_written: int = 0

        self._logger.info(
            "export_sql_started",
            output_path=output_path,
            job_id=job_id,
            table_name=table_name,
            include_ddl=include_ddl,
            dialect=dialect,
        )

        with open(output_path, "w", encoding="utf-8") as f:
            # Optional DDL preamble.
            if include_ddl:
                ddl: str = self._generate_create_table(
                    table_name, columns, dialect
                )
                f.write(ddl)
                f.write("\n\n")

            # Open transaction.
            f.write("BEGIN;\n\n")

            for record in data:
                values: List[str] = [
                    self._escape_sql_value(record.get(col))
                    for col in column_names
                ]
                cols_joined: str = ", ".join(column_names)
                vals_joined: str = ", ".join(values)
                f.write(
                    f"INSERT INTO {table_name} ({cols_joined}) "
                    f"VALUES ({vals_joined});\n"
                )
                records_written += 1

                # Periodic COMMIT for large exports.
                if records_written % commit_interval == 0:
                    f.write("COMMIT;\n")
                    f.write("BEGIN;\n")
                    self._update_progress(job_id, records_written)

            # Final COMMIT.
            f.write("\nCOMMIT;\n")

        self._logger.info(
            "export_sql_completed",
            output_path=output_path,
            job_id=job_id,
            records_written=records_written,
            table_name=table_name,
        )

        return output_path, records_written

    # ------------------------------------------------------------------
    # Compression
    # ------------------------------------------------------------------

    def _compress_file(self, file_path: str, algorithm: str = "gzip") -> str:
        """Compress an exported file using the specified algorithm.

        For gzip compression, reads the source file in 64 KB chunks and
        writes a ``.gz`` output.  Parquet files should **not** be passed
        to this method as they use internal Snappy compression.

        Args:
            file_path: Path to the uncompressed file.
            algorithm: Compression algorithm.  Currently only ``'gzip'``
                is supported for text-based formats.

        Returns:
            Path to the compressed file (``file_path + '.gz'``).

        Raises:
            ValueError: If *algorithm* is unsupported.
        """
        if algorithm != "gzip":
            raise ValueError(
                f"Unsupported compression algorithm: '{algorithm}'. "
                "Supported: 'gzip'."
            )

        original_size: int = os.path.getsize(file_path)
        compressed_path: str = file_path + ".gz"

        with open(file_path, "rb") as f_in, gzip.open(
            compressed_path, "wb", compresslevel=6
        ) as f_out:
            while True:
                chunk: bytes = f_in.read(_READ_CHUNK_SIZE)
                if not chunk:
                    break
                f_out.write(chunk)

        compressed_size: int = os.path.getsize(compressed_path)

        # Remove uncompressed original.
        os.remove(file_path)

        ratio: float = (
            (1 - compressed_size / original_size) * 100
            if original_size > 0
            else 0.0
        )
        self._logger.info(
            "file_compressed",
            algorithm=algorithm,
            original_bytes=original_size,
            compressed_bytes=compressed_size,
            compression_ratio_pct=round(ratio, 1),
        )

        return compressed_path

    # ------------------------------------------------------------------
    # Encryption
    # ------------------------------------------------------------------

    def _encrypt_file(
        self,
        file_path: str,
        encryption_key: Optional[bytes] = None,
    ) -> str:
        """Encrypt a file using AES-256-GCM authenticated encryption.

        Reads the plaintext file, encrypts the contents with a 256-bit
        key and a randomly generated 96-bit nonce, and writes the output
        as ``nonce (12 bytes) || ciphertext+tag`` to a ``.enc`` file.
        The original unencrypted file is deleted after encryption.

        For files larger than :data:`_ENCRYPT_CHUNK_LIMIT` (256 MB), the
        file is read in a single pass to satisfy AESGCM's requirement for
        a contiguous plaintext buffer, but memory-constrained deployments
        should tune the export batch size to produce smaller intermediate
        files.

        Args:
            encryption_key: A 32-byte (256-bit) AES key.  When ``None``,
                a new random key is generated via
                :meth:`AESGCM.generate_key`.  **Note:** a generated key
                is non-recoverable; callers should supply a key derived
                from :attr:`ProvisioningServiceConfig.ENCRYPTION_KEY`
                for production use.

        Returns:
            Path to the encrypted file (``file_path + '.enc'``).
        """
        if encryption_key is None:
            encryption_key = AESGCM.generate_key(bit_length=256)
            self._logger.warning(
                "encryption_key_generated",
                msg="No encryption key provided; generated a random key. "
                "The key is not persisted — decrypt will not be possible.",
            )

        aesgcm = AESGCM(encryption_key)

        # Generate a random 96-bit (12-byte) nonce.
        nonce: bytes = os.urandom(12)

        # Read the entire plaintext file.
        with open(file_path, "rb") as f_in:
            plaintext: bytes = f_in.read()

        ciphertext: bytes = aesgcm.encrypt(nonce, plaintext, None)

        # Assemble the encrypted payload (nonce || ciphertext+tag) in memory
        # before writing to disk in a single atomic operation.
        encrypted_buffer: io.BytesIO = io.BytesIO()
        encrypted_buffer.write(nonce)
        encrypted_buffer.write(ciphertext)

        encrypted_path: str = file_path + ".enc"
        with open(encrypted_path, "wb") as f_out:
            f_out.write(encrypted_buffer.getvalue())

        # Remove unencrypted original.
        os.remove(file_path)

        self._logger.info(
            "file_encrypted",
            algorithm="AES-256-GCM",
            original_bytes=len(plaintext),
            encrypted_bytes=len(nonce) + len(ciphertext),
        )

        return encrypted_path

    # ------------------------------------------------------------------
    # Checksum
    # ------------------------------------------------------------------

    def _calculate_checksum(self, file_path: str) -> str:
        """Calculate the SHA-256 checksum of a file.

        Reads the file in 64 KB chunks for memory efficiency and returns
        the hexadecimal digest string.  The checksum supports tamper-evident
        audit trails required for SOC 2 Type II compliance (C-004).

        Args:
            file_path: Path to the file to hash.

        Returns:
            The lowercase hexadecimal SHA-256 digest string.
        """
        sha256 = hashlib.sha256()

        with open(file_path, "rb") as f:
            while True:
                chunk: bytes = f.read(_READ_CHUNK_SIZE)
                if not chunk:
                    break
                sha256.update(chunk)

        digest: str = sha256.hexdigest()

        self._logger.info(
            "checksum_calculated",
            algorithm="SHA-256",
            file_path=file_path,
            checksum=digest,
        )

        return digest

    # ------------------------------------------------------------------
    # Progress tracking
    # ------------------------------------------------------------------

    def _update_progress(
        self,
        job_id: str,
        records_exported: int,
        total_records: Optional[int] = None,
        status: str = "exporting",
    ) -> None:
        """Update export progress in Redis for real-time monitoring.

        Stores structured progress data as a Redis hash set with a
        one-hour TTL.  The Web Console polls these keys via the API
        Gateway to render live progress bars.

        Args:
            job_id: The generation job identifier.
            records_exported: Count of records written so far.
            total_records: Optional total expected record count.  When
                provided, a percentage is calculated.
            status: Current status string — ``'exporting'``,
                ``'uploading'``, ``'completed'``, or ``'failed'``.
        """
        progress_key: str = f"{_PROGRESS_KEY_PREFIX}{job_id}"

        percentage: float = 0.0
        if total_records and total_records > 0:
            percentage = round(
                (records_exported / total_records) * 100, 2
            )

        progress_data: Dict[str, str] = {
            "job_id": job_id,
            "records_exported": str(records_exported),
            "total_records": str(total_records) if total_records else "",
            "percentage": str(percentage),
            "status": status,
            "updated_at": str(time.time()),
        }

        try:
            self._redis.hset(progress_key, mapping=progress_data)
            self._redis.expire(progress_key, _PROGRESS_TTL_SECONDS)
        except Exception as exc:
            # Progress tracking is best-effort; do not abort the export.
            self._logger.warning(
                "progress_update_failed",
                job_id=job_id,
                error=str(exc),
            )

    # ------------------------------------------------------------------
    # Parquet schema helpers
    # ------------------------------------------------------------------

    def _build_parquet_schema(self, schema: Dict[str, Any]) -> pa.Schema:
        """Convert a generic schema definition to a PyArrow schema.

        Maps platform-standard type names to their PyArrow equivalents:

        +-------------+-------------------------------+
        | Type Name   | PyArrow Type                  |
        +=============+===============================+
        | STRING      | ``pa.string()``               |
        | INTEGER     | ``pa.int64()``                |
        | FLOAT       | ``pa.float64()``              |
        | DECIMAL     | ``pa.decimal128(38, 10)``     |
        | BOOLEAN     | ``pa.bool_()``                |
        | DATE        | ``pa.date32()``               |
        | TIMESTAMP   | ``pa.timestamp('us')``        |
        | TEXT        | ``pa.large_string()``         |
        | JSON        | ``pa.large_string()``         |
        | UUID        | ``pa.string()``               |
        | BINARY      | ``pa.binary()``               |
        +-------------+-------------------------------+

        Unrecognised types default to ``pa.string()``.

        Args:
            schema: Schema definition dictionary with a ``columns`` key
                containing a list of column definitions.  Each column dict
                must have ``name`` (str) and ``type`` (str) keys.

        Returns:
            A :class:`pyarrow.Schema` with one field per column.
        """
        type_map: Dict[str, pa.DataType] = {
            "STRING": pa.string(),
            "VARCHAR": pa.string(),
            "CHAR": pa.string(),
            "INTEGER": pa.int64(),
            "INT": pa.int64(),
            "BIGINT": pa.int64(),
            "SMALLINT": pa.int32(),
            "FLOAT": pa.float64(),
            "DOUBLE": pa.float64(),
            "DECIMAL": pa.decimal128(38, 10),
            "NUMERIC": pa.decimal128(38, 10),
            "BOOLEAN": pa.bool_(),
            "BOOL": pa.bool_(),
            "DATE": pa.date32(),
            "TIMESTAMP": pa.timestamp("us"),
            "DATETIME": pa.timestamp("us"),
            "TEXT": pa.large_string(),
            "CLOB": pa.large_string(),
            "JSON": pa.large_string(),
            "JSONB": pa.large_string(),
            "UUID": pa.string(),
            "BINARY": pa.binary(),
            "BLOB": pa.binary(),
            "BYTEA": pa.binary(),
        }

        columns: List[Dict[str, Any]] = schema.get("columns", [])
        fields: List[pa.Field] = []

        for col in columns:
            col_name: str = col.get("name", "unknown")
            col_type: str = col.get("type", "STRING").upper()
            nullable: bool = col.get("nullable", True)

            arrow_type: pa.DataType = type_map.get(col_type, pa.string())
            fields.append(pa.field(col_name, arrow_type, nullable=nullable))

        return pa.schema(fields)

    def _write_parquet_batch(
        self,
        writer: pq.ParquetWriter,
        batch_data: List[Dict[str, Any]],
        column_names: List[str],
        arrow_schema: pa.Schema,
    ) -> None:
        """Write a batch of records to the Parquet writer.

        Converts a list of dict records into a :class:`pyarrow.RecordBatch`
        and writes it using the provided :class:`~pyarrow.parquet.ParquetWriter`.

        Args:
            writer: An open ParquetWriter instance.
            batch_data: List of dict records to write.
            column_names: Ordered list of column names.
            arrow_schema: PyArrow schema matching the column definitions.
        """
        # Transpose row-oriented dicts into column-oriented dict-of-lists.
        column_dict: Dict[str, List[Any]] = {
            col: [row.get(col) for row in batch_data]
            for col in column_names
        }

        batch: pa.RecordBatch = pa.RecordBatch.from_pydict(
            column_dict, schema=arrow_schema
        )
        writer.write_batch(batch)

    # ------------------------------------------------------------------
    # SQL helpers
    # ------------------------------------------------------------------

    @staticmethod
    def _escape_sql_value(value: Any) -> str:
        """Escape a Python value for safe inclusion in a SQL statement.

        Handles ``None`` (→ ``NULL``), strings (single-quote escaped),
        booleans, integers, floats, and falls back to stringifying
        unknown types.

        Args:
            value: The Python value to escape.

        Returns:
            A SQL-safe string representation of the value.
        """
        if value is None:
            return "NULL"

        if isinstance(value, bool):
            return "TRUE" if value else "FALSE"

        if isinstance(value, (int, float)):
            return str(value)

        # String values — escape single quotes by doubling them.
        str_val: str = str(value).replace("'", "''")
        return f"'{str_val}'"

    @staticmethod
    def _generate_create_table(
        table_name: str,
        columns: List[Dict[str, Any]],
        dialect: str = "generic",
    ) -> str:
        """Generate a CREATE TABLE DDL statement.

        Produces a basic ``CREATE TABLE IF NOT EXISTS`` statement suitable
        for the specified SQL dialect.

        Args:
            table_name: Target table name.
            columns: List of column definition dicts with ``name`` and
                ``type`` keys, and optional ``nullable`` and ``primary_key``.
            dialect: SQL dialect hint (currently only affects minor syntax
                differences).

        Returns:
            A complete CREATE TABLE SQL string.
        """
        sql_type_map: Dict[str, str] = {
            "STRING": "VARCHAR(255)",
            "VARCHAR": "VARCHAR(255)",
            "CHAR": "CHAR(1)",
            "INTEGER": "INTEGER",
            "INT": "INTEGER",
            "BIGINT": "BIGINT",
            "SMALLINT": "SMALLINT",
            "FLOAT": "DOUBLE PRECISION",
            "DOUBLE": "DOUBLE PRECISION",
            "DECIMAL": "DECIMAL(38, 10)",
            "NUMERIC": "NUMERIC(38, 10)",
            "BOOLEAN": "BOOLEAN",
            "BOOL": "BOOLEAN",
            "DATE": "DATE",
            "TIMESTAMP": "TIMESTAMP",
            "DATETIME": "TIMESTAMP",
            "TEXT": "TEXT",
            "CLOB": "TEXT",
            "JSON": "TEXT",
            "JSONB": "JSONB" if dialect == "postgresql" else "TEXT",
            "UUID": "UUID" if dialect == "postgresql" else "VARCHAR(36)",
            "BINARY": "BYTEA" if dialect == "postgresql" else "BLOB",
            "BLOB": "BLOB",
            "BYTEA": "BYTEA",
        }

        col_defs: List[str] = []
        for col in columns:
            col_name: str = col.get("name", "unknown")
            col_type: str = col.get("type", "STRING").upper()
            nullable: bool = col.get("nullable", True)
            primary_key: bool = col.get("primary_key", False)

            sql_type: str = sql_type_map.get(col_type, "VARCHAR(255)")
            constraint_parts: List[str] = [col_name, sql_type]

            if primary_key:
                constraint_parts.append("PRIMARY KEY")
            elif not nullable:
                constraint_parts.append("NOT NULL")

            col_defs.append("    " + " ".join(constraint_parts))

        columns_sql: str = ",\n".join(col_defs)
        return (
            f"CREATE TABLE IF NOT EXISTS {table_name} (\n"
            f"{columns_sql}\n"
            f");"
        )

    # ------------------------------------------------------------------
    # Schema helpers
    # ------------------------------------------------------------------

    @staticmethod
    def _extract_column_names(schema: Dict[str, Any]) -> List[str]:
        """Extract ordered column names from a schema definition.

        Args:
            schema: Schema definition with a ``columns`` list where each
                entry has a ``name`` key.

        Returns:
            A list of column name strings.
        """
        columns: List[Dict[str, Any]] = schema.get("columns", [])
        return [col.get("name", f"column_{i}") for i, col in enumerate(columns)]

    # ------------------------------------------------------------------
    # Cleanup
    # ------------------------------------------------------------------

    def cleanup_temp_files(self, job_id: str, tenant_id: str) -> int:
        """Remove temporary export files for a completed job.

        Recursively deletes the tenant-scoped temporary directory
        associated with the given *job_id*.  This should be called after
        a successful export and cloud upload to free disk space on the
        export worker node.

        Args:
            job_id: The generation job identifier.
            tenant_id: The owning tenant identifier.

        Returns:
            The number of files that were removed.
        """
        job_temp_dir: Path = Path(self.export_temp_dir) / tenant_id / job_id
        files_removed: int = 0

        if not job_temp_dir.exists():
            self._logger.info(
                "cleanup_no_files_found",
                job_id=job_id,
                tenant_id=tenant_id,
                path=str(job_temp_dir),
            )
            return 0

        # Count files before removal.
        for item in job_temp_dir.rglob("*"):
            if item.is_file():
                files_removed += 1

        try:
            shutil.rmtree(str(job_temp_dir))
            self._logger.info(
                "temp_files_cleaned_up",
                job_id=job_id,
                tenant_id=tenant_id,
                files_removed=files_removed,
                path=str(job_temp_dir),
            )
        except OSError as exc:
            self._logger.error(
                "temp_file_cleanup_failed",
                job_id=job_id,
                tenant_id=tenant_id,
                error=str(exc),
                path=str(job_temp_dir),
            )

        # Also attempt to clean up an empty tenant directory.
        tenant_dir: Path = Path(self.export_temp_dir) / tenant_id
        try:
            if tenant_dir.exists() and not any(tenant_dir.iterdir()):
                tenant_dir.rmdir()
        except OSError:
            pass  # Non-critical; another job may be using the tenant dir.

        return files_removed

    # ------------------------------------------------------------------
    # Public helpers
    # ------------------------------------------------------------------

    def get_supported_formats(self) -> List[str]:
        """Return a sorted list of supported export format names.

        Useful for API input validation and populating UI dropdowns in
        the Web Console generation wizard.

        Returns:
            A sorted list of format strings (e.g.
            ``['csv', 'json', 'jsonl', 'parquet', 'sql']``).
        """
        return sorted(SUPPORTED_FORMATS)
