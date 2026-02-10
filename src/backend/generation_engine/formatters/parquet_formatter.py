"""Apache Parquet columnar format output formatter for the Synthetic ERP Data Generation Platform.

This module provides the ParquetFormatter class that converts generated synthetic ERP data
(pandas DataFrames) into Apache Parquet columnar format files using PyArrow. Parquet offers
significantly better compression ratios than CSV or JSON for large datasets, making it the
preferred format for high-throughput data generation scenarios targeting data lake and
analytics platforms.

Features:
    - Configurable compression algorithms (Snappy, GZIP, ZSTD, LZ4, Brotli)
    - Row group sizing for optimized analytical read performance
    - Column-level encoding selection based on data characteristics
    - Schema-aware type mapping from ERP column definitions to Arrow types
    - File-level and column-level metadata embedding for provenance tracking
    - Partitioned output for splitting large datasets across multiple files
    - Streaming support for memory-efficient processing

Implements the BaseFormatter interface for consistent dispatch from the generation
orchestrator, supporting multi-format export as required by feature F-009.

Typical usage::

    config = ParquetFormatterConfig(compression=ParquetCompression.ZSTD)
    formatter = ParquetFormatter(config)
    parquet_bytes = formatter.format(dataframe, "gl_journal_entries", column_defs)
"""

import io
from typing import Optional, Dict, List, Union, Any
from pathlib import Path
from enum import Enum
from datetime import datetime, timezone

import pyarrow as pa
import pyarrow.parquet as pq
import pandas as pd
from pydantic import BaseModel, Field

# Import BaseFormatter from sibling base module to implement the formatter interface.
# All concrete formatters (SQL, CSV, JSON, Parquet) extend BaseFormatter for consistent
# orchestrator dispatch as required by the formatter registry pattern.
# Uses try/except to handle cases where base.py may not be available during parallel
# agent builds in the Blitzy platform.
try:
    from .base import BaseFormatter
except ImportError:
    from abc import ABC, abstractmethod

    class BaseFormatter(ABC):  # type: ignore[no-redef]
        """Fallback abstract base class providing the formatter interface contract.

        Defines the same abstract method signatures as the canonical BaseFormatter
        in formatters/base.py. Used only when the base module is not yet available
        during coordinated parallel file creation.
        """

        @abstractmethod
        def format(
            self,
            data: pd.DataFrame,
            table_name: str,
            column_definitions: dict = None,
        ) -> Union[str, bytes]:
            """Format data to output string or bytes."""
            ...

        @abstractmethod
        def format_to_stream(
            self,
            data: pd.DataFrame,
            table_name: str,
            column_definitions: dict,
            output: io.IOBase,
        ) -> None:
            """Stream formatted output for large datasets."""
            ...

        @abstractmethod
        def get_file_extension(self) -> str:
            """Return file extension (e.g., '.parquet')."""
            ...

        @abstractmethod
        def get_content_type(self) -> str:
            """Return MIME content type for HTTP responses."""
            ...

        def format_batch(
            self,
            data_batches: Any,
            table_name: str,
            column_definitions: dict,
            output: io.IOBase,
        ) -> int:
            """Default batch processing implementation."""
            total_rows = 0
            for batch in data_batches:
                self.format_to_stream(batch, table_name, column_definitions, output)
                total_rows += len(batch)
            return total_rows


# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

# Platform version identifier embedded in Parquet file metadata for provenance tracking
PLATFORM_VERSION: str = "1.0.0"

# ERP-to-Arrow type mapping dictionary.
# Maps ERP system column type names (uppercase) to PyArrow type constructors.
# Covers common data types across SAP, Oracle EBS, Microsoft Dynamics, and
# legacy systems supported by the platform.
ERP_TYPE_MAP: Dict[str, pa.DataType] = {
    # String types
    "VARCHAR": pa.string(),
    "CHAR": pa.string(),
    "TEXT": pa.string(),
    "NVARCHAR": pa.string(),
    "NCHAR": pa.string(),
    "CLOB": pa.string(),
    "NCLOB": pa.string(),
    "STRING": pa.string(),
    # Integer types
    "INTEGER": pa.int64(),
    "INT": pa.int64(),
    "SMALLINT": pa.int16(),
    "TINYINT": pa.int16(),
    "BIGINT": pa.int64(),
    # Floating-point types
    "FLOAT": pa.float32(),
    "REAL": pa.float32(),
    "DOUBLE": pa.float64(),
    "DOUBLE PRECISION": pa.float64(),
    # Date/time types
    "DATE": pa.date32(),
    "TIMESTAMP": pa.timestamp("us"),
    "DATETIME": pa.timestamp("us"),
    "DATETIME2": pa.timestamp("us"),
    "TIME": pa.time64("us"),
    # Boolean types
    "BOOLEAN": pa.bool_(),
    "BOOL": pa.bool_(),
    # Binary types
    "BLOB": pa.binary(),
    "BINARY": pa.binary(),
    "VARBINARY": pa.binary(),
    "BYTEA": pa.binary(),
    "RAW": pa.binary(),
    # UUID types (stored as string in Parquet for portability)
    "UUID": pa.string(),
    "GUID": pa.string(),
    "UNIQUEIDENTIFIER": pa.string(),
}


# ---------------------------------------------------------------------------
# ParquetCompression Enum
# ---------------------------------------------------------------------------


class ParquetCompression(Enum):
    """Supported Parquet compression algorithms.

    Each algorithm offers different trade-offs between compression ratio,
    compression speed, and decompression speed. The choice depends on the
    downstream use case and infrastructure constraints.

    Attributes:
        SNAPPY: Default. Good balance of speed and compression ratio.
            Best for general-purpose use with fast decompression.
        GZIP: Best compression ratio. Slower but produces smaller files.
            Ideal for archival and storage-constrained scenarios.
        ZSTD: Fast compression with good ratio. Modern alternative to GZIP
            with better speed characteristics at comparable ratios.
        LZ4: Fastest compression and decompression. Lower compression ratio.
            Best for real-time or latency-sensitive pipelines.
        BROTLI: High compression ratio. Good for network transfer scenarios
            where bandwidth is constrained.
        NONE: No compression. Maximum write speed, largest file size.
            Use only when downstream systems require uncompressed data.
    """

    SNAPPY = "snappy"
    GZIP = "gzip"
    ZSTD = "zstd"
    LZ4 = "lz4"
    BROTLI = "brotli"
    NONE = "none"


# ---------------------------------------------------------------------------
# ParquetFormatterConfig Pydantic Model
# ---------------------------------------------------------------------------


class ParquetFormatterConfig(BaseModel):
    """Configuration for the Parquet output formatter.

    Controls all aspects of Parquet file generation including compression,
    row group sizing, encoding options, timestamp handling, and file-level
    metadata. Uses Pydantic 2.x for validated configuration with sensible
    defaults optimized for ERP synthetic data workloads.

    Attributes:
        compression: Compression algorithm for Parquet columns. Defaults to
            SNAPPY for balanced speed and compression.
        compression_level: Codec-specific compression level. None uses the
            codec's default. Higher values yield better compression at the
            cost of slower write speed.
        row_group_size: Number of rows per row group. Controls the granularity
            of predicate pushdown and min/max statistics. Default 100,000
            balances analytical read performance with memory usage.
        use_dictionary: Enable dictionary encoding for low-cardinality columns.
            Significantly improves compression for columns with few distinct
            values (e.g., status codes, country codes, currency codes).
        write_statistics: Write min/max/null_count statistics per column per
            row group. Enables predicate pushdown in Spark, Presto, and DuckDB.
        coerce_timestamps: Resolution for timestamp coercion ('ms', 'us', 'ns').
            Default 'us' (microseconds) provides sufficient precision for
            most ERP timestamp fields.
        allow_truncated_timestamps: Allow loss of precision when coercing
            timestamps. Set to True if source timestamps have higher precision.
        use_deprecated_int96_timestamps: Use legacy INT96 timestamp encoding
            for Hive and Impala compatibility. Modern systems should use False.
        version: Parquet format version. '2.6' supports all modern features
            including nanosecond timestamps and improved encoding.
        data_page_size: Target data page size in bytes. None uses PyArrow
            default (~1MB). Smaller pages enable finer-grained column pruning.
        flavor: Compatibility mode. Set to 'spark' for Spark-specific
            optimizations and timestamp handling.
        file_metadata: Custom key-value metadata pairs embedded in the Parquet
            file footer for provenance tracking and documentation.
        max_rows_per_file: Maximum rows per output file. When set, large
            datasets are automatically partitioned across multiple files for
            parallel downstream processing.
    """

    compression: ParquetCompression = Field(
        default=ParquetCompression.SNAPPY,
        description="Compression algorithm for Parquet columns",
    )
    compression_level: Optional[int] = Field(
        default=None,
        description="Codec-specific compression level (None = use codec default)",
    )
    row_group_size: int = Field(
        default=100000,
        gt=0,
        description="Number of rows per row group for read optimization",
    )
    use_dictionary: bool = Field(
        default=True,
        description="Enable dictionary encoding for low-cardinality columns",
    )
    write_statistics: bool = Field(
        default=True,
        description="Write min/max/null_count column statistics per row group",
    )
    coerce_timestamps: Optional[str] = Field(
        default="us",
        description="Timestamp resolution: 'ms', 'us', or 'ns'",
    )
    allow_truncated_timestamps: bool = Field(
        default=False,
        description="Allow precision loss when coercing timestamps",
    )
    use_deprecated_int96_timestamps: bool = Field(
        default=False,
        description="Use legacy INT96 timestamp encoding for Hive/Impala compatibility",
    )
    version: str = Field(
        default="2.6",
        description="Parquet format version",
    )
    data_page_size: Optional[int] = Field(
        default=None,
        gt=0,
        description="Target data page size in bytes",
    )
    flavor: Optional[str] = Field(
        default=None,
        description="Compatibility mode: 'spark' for Spark optimizations",
    )
    file_metadata: Optional[Dict[str, str]] = Field(
        default=None,
        description="Custom key-value metadata pairs for Parquet file footer",
    )
    max_rows_per_file: Optional[int] = Field(
        default=None,
        gt=0,
        description="Maximum rows per file; enables partitioned output",
    )

    model_config = {
        "use_enum_values": False,
        "json_schema_extra": {
            "examples": [
                {
                    "compression": "snappy",
                    "row_group_size": 100000,
                    "use_dictionary": True,
                    "write_statistics": True,
                }
            ]
        },
    }


# ---------------------------------------------------------------------------
# ParquetFormatter Class
# ---------------------------------------------------------------------------


class ParquetFormatter(BaseFormatter):
    """Apache Parquet columnar format output formatter.

    Converts generated synthetic ERP data (pandas DataFrames) into Apache
    Parquet files using PyArrow. Implements the BaseFormatter interface for
    consistent dispatch from the generation orchestrator's formatter registry.

    Parquet offers significantly better compression ratios than CSV or JSON for
    large datasets with columnar access patterns, making it the preferred format
    for high-throughput data generation scenarios targeting data lake and
    analytics platforms (Spark, Presto, DuckDB, BigQuery).

    Key capabilities:
        - Schema-aware type mapping from ERP column definitions to Arrow types
        - Configurable compression (Snappy, GZIP, ZSTD, LZ4, Brotli)
        - Row group sizing for optimized analytical read performance
        - Column-level encoding selection based on data characteristics
        - File-level metadata embedding for provenance and audit tracking
        - Partitioned output for splitting large datasets across files
        - Streaming support via ParquetWriter for memory-efficient processing

    Example::

        config = ParquetFormatterConfig(
            compression=ParquetCompression.ZSTD,
            row_group_size=50000,
        )
        formatter = ParquetFormatter(config)
        parquet_bytes = formatter.format(df, "gl_journal_entries", column_defs)

    Args:
        config: Optional ParquetFormatterConfig instance. If None, uses default
            configuration with Snappy compression and 100K row groups.
    """

    def __init__(self, config: Optional[ParquetFormatterConfig] = None) -> None:
        """Initialize the Parquet formatter with configuration.

        Args:
            config: Parquet formatting configuration. If None, creates a default
                configuration with Snappy compression and 100,000-row groups.
        """
        self._config: ParquetFormatterConfig = (
            config if config is not None else ParquetFormatterConfig()
        )

    # ------------------------------------------------------------------
    # Public Interface — BaseFormatter Implementation
    # ------------------------------------------------------------------

    def format(
        self,
        data: pd.DataFrame,
        table_name: str,
        column_definitions: Optional[Dict[str, Any]] = None,
    ) -> bytes:
        """Format a complete DataFrame to Parquet bytes.

        Converts the entire DataFrame to an in-memory Parquet representation
        and returns the raw bytes. Suitable for small to medium datasets that
        fit comfortably in memory. For larger datasets, prefer format_to_file()
        or format_to_files() to avoid doubling memory usage.

        Args:
            data: DataFrame containing generated synthetic ERP data.
            table_name: Name of the ERP table (e.g., 'gl_journal_entries').
                Embedded in file metadata for provenance tracking.
            column_definitions: Optional dict mapping column names to their ERP
                type definitions. Used for schema-aware type mapping. Format::

                    {
                        "amount": {"type": "DECIMAL", "precision": 18, "scale": 2},
                        "posting_date": {"type": "DATE"},
                        "description": {"type": "VARCHAR", "length": 255},
                    }

                If None, types are inferred from the DataFrame dtypes.

        Returns:
            Raw Parquet file bytes ready for storage or transmission.

        Raises:
            pyarrow.ArrowInvalid: If data types cannot be converted to Arrow types.
        """
        arrow_schema = (
            self._build_arrow_schema(column_definitions)
            if column_definitions
            else None
        )
        table = self._dataframe_to_table(data, arrow_schema)
        enriched_schema = self._add_file_metadata(
            table.schema, table_name, record_count=len(data)
        )
        table = table.replace_schema_metadata(enriched_schema.metadata)

        buffer = io.BytesIO()
        write_options = self._get_write_options()
        pq.write_table(table, buffer, **write_options)
        return buffer.getvalue()

    def format_to_stream(
        self,
        data: pd.DataFrame,
        table_name: str,
        column_definitions: Optional[Dict[str, Any]] = None,
        output: Optional[io.IOBase] = None,
    ) -> None:
        """Write Parquet data to an output stream.

        Serializes the DataFrame to Parquet format and writes directly to the
        provided stream using PyArrow's ParquetWriter for streaming row group
        output. This avoids holding the complete Parquet bytes in memory.

        Args:
            data: DataFrame containing generated synthetic ERP data.
            table_name: Name of the ERP table for metadata embedding.
            column_definitions: Optional ERP column type definitions for
                schema-aware type mapping.
            output: Writable binary stream (e.g., file handle, BytesIO).
                Must support write() operations.

        Raises:
            ValueError: If output stream is None or not writable.
            pyarrow.ArrowInvalid: If data types cannot be converted.
        """
        if output is None:
            raise ValueError(
                "Output stream must be provided for format_to_stream(). "
                "Pass a writable binary stream (e.g., open file handle or BytesIO)."
            )

        arrow_schema = (
            self._build_arrow_schema(column_definitions)
            if column_definitions
            else None
        )
        table = self._dataframe_to_table(data, arrow_schema)
        enriched_schema = self._add_file_metadata(
            table.schema, table_name, record_count=len(data)
        )
        table = table.replace_schema_metadata(enriched_schema.metadata)

        write_options = self._get_write_options()

        # Use ParquetWriter for streaming output with row group control
        writer = pq.ParquetWriter(
            output,
            table.schema,
            compression=write_options.get("compression", "snappy"),
            version=write_options.get("version", "2.6"),
            use_dictionary=write_options.get("use_dictionary", True),
            write_statistics=write_options.get("write_statistics", True),
            flavor=self._config.flavor,
            coerce_timestamps=write_options.get("coerce_timestamps"),
            allow_truncated_timestamps=write_options.get(
                "allow_truncated_timestamps", False
            ),
            data_page_size=write_options.get("data_page_size"),
            use_deprecated_int96_timestamps=self._config.use_deprecated_int96_timestamps,
        )

        try:
            row_group_size = self._config.row_group_size
            total_rows = len(table)

            if total_rows == 0:
                # Write an empty row group to produce a valid Parquet file
                writer.write_table(table)
            else:
                # Write data in sized row groups for optimal read performance
                for start_idx in range(0, total_rows, row_group_size):
                    end_idx = min(start_idx + row_group_size, total_rows)
                    row_group = table.slice(start_idx, end_idx - start_idx)
                    writer.write_table(row_group)
        finally:
            writer.close()

    def format_to_file(
        self,
        data: pd.DataFrame,
        table_name: str,
        column_definitions: Optional[Dict[str, Any]] = None,
        file_path: Optional[str] = None,
    ) -> str:
        """Write Parquet data directly to a file path.

        Serializes the DataFrame to Parquet format and writes to the specified
        file path. Creates parent directories if they don't exist. More efficient
        than format() + manual file write because PyArrow can write directly to
        disk without buffering the entire file in memory.

        Args:
            data: DataFrame containing generated synthetic ERP data.
            table_name: Name of the ERP table for metadata embedding.
            column_definitions: Optional ERP column type definitions.
            file_path: Output file path. Parent directories are created
                automatically if they do not exist.

        Returns:
            Absolute path string to the written Parquet file.

        Raises:
            ValueError: If file_path is None.
            OSError: If the file cannot be written to the specified path.
        """
        if file_path is None:
            raise ValueError(
                "file_path must be provided for format_to_file(). "
                "Supply the destination path for the Parquet output."
            )

        output_path = Path(file_path)
        output_path.parent.mkdir(parents=True, exist_ok=True)

        arrow_schema = (
            self._build_arrow_schema(column_definitions)
            if column_definitions
            else None
        )
        table = self._dataframe_to_table(data, arrow_schema)
        enriched_schema = self._add_file_metadata(
            table.schema, table_name, record_count=len(data)
        )
        table = table.replace_schema_metadata(enriched_schema.metadata)

        write_options = self._get_write_options()
        pq.write_table(table, str(output_path), **write_options)

        return str(output_path.resolve())

    def format_to_files(
        self,
        data: pd.DataFrame,
        table_name: str,
        column_definitions: Optional[Dict[str, Any]] = None,
        output_dir: Optional[str] = None,
    ) -> List[str]:
        """Split data into multiple Parquet files based on max_rows_per_file.

        Partitions a large DataFrame into multiple Parquet files, each containing
        at most ``max_rows_per_file`` rows. File naming follows the pattern::

            {table_name}_part_{index:05d}.parquet

        This is essential for large-scale ERP data generation where output datasets
        may contain millions of rows. Partitioned output enables parallel downstream
        processing and avoids memory pressure on consumer systems.

        Args:
            data: DataFrame containing generated synthetic ERP data.
            table_name: Name of the ERP table, used in file naming and metadata.
            column_definitions: Optional ERP column type definitions.
            output_dir: Directory path for output files. Created if it doesn't exist.

        Returns:
            List of absolute file path strings for all written Parquet files,
            ordered by partition index.

        Raises:
            ValueError: If output_dir is None.
            OSError: If the output directory cannot be created or written to.
        """
        if output_dir is None:
            raise ValueError(
                "output_dir must be provided for format_to_files(). "
                "Supply the destination directory for partitioned output."
            )

        output_path = Path(output_dir)
        output_path.mkdir(parents=True, exist_ok=True)

        max_rows = self._config.max_rows_per_file
        total_rows = len(data)

        # If no max_rows_per_file is set, or data fits in a single file,
        # write everything to one file
        if max_rows is None or total_rows <= max_rows:
            file_name = f"{table_name}_part_00000{self.get_file_extension()}"
            single_path = str(output_path / file_name)
            written_path = self.format_to_file(
                data, table_name, column_definitions, single_path
            )
            return [written_path]

        written_files: List[str] = []
        part_index = 0

        for start_idx in range(0, total_rows, max_rows):
            end_idx = min(start_idx + max_rows, total_rows)
            chunk = data.iloc[start_idx:end_idx].reset_index(drop=True)

            file_name = (
                f"{table_name}_part_{part_index:05d}{self.get_file_extension()}"
            )
            chunk_path = str(output_path / file_name)

            written_path = self.format_to_file(
                chunk, table_name, column_definitions, chunk_path
            )
            written_files.append(written_path)
            part_index += 1

        return written_files

    def get_file_extension(self) -> str:
        """Return the file extension for Parquet output files.

        Returns:
            The string ``'.parquet'``.
        """
        return ".parquet"

    def get_content_type(self) -> str:
        """Return the MIME content type for Parquet data.

        Used for HTTP Content-Type headers when serving Parquet data
        through the API Gateway or Provisioning Service.

        Returns:
            The MIME type ``'application/vnd.apache.parquet'``.
        """
        return "application/vnd.apache.parquet"

    # ------------------------------------------------------------------
    # Private Helpers
    # ------------------------------------------------------------------

    def _build_arrow_schema(
        self, column_definitions: Optional[Dict[str, Any]]
    ) -> Optional[pa.Schema]:
        """Build a PyArrow Schema from ERP column type definitions.

        Maps ERP system column type names to PyArrow data types using the
        module-level ``ERP_TYPE_MAP`` dictionary. Handles DECIMAL/NUMERIC types
        with precision and scale, and applies nullable flags from column
        definitions.

        Args:
            column_definitions: Dict mapping column names to type definitions.
                Supported formats::

                    # Dict-based definition (full control)
                    {
                        "amount": {
                            "type": "DECIMAL",
                            "precision": 18,
                            "scale": 2,
                            "nullable": True,
                        },
                        "status": {"type": "VARCHAR", "length": 20},
                    }

                    # String-based definition (simple type name)
                    {
                        "amount": "DECIMAL",
                        "status": "VARCHAR",
                    }

        Returns:
            PyArrow Schema with mapped types, or None if column_definitions
            is None or empty.
        """
        if not column_definitions:
            return None

        fields: List[pa.Field] = []

        for col_name, col_def in column_definitions.items():
            # Extract type information based on definition format
            if isinstance(col_def, dict):
                col_type = col_def.get("type", "VARCHAR").upper().strip()
                precision = col_def.get("precision", 18)
                scale = col_def.get("scale", 2)
                nullable = col_def.get("nullable", True)
            elif isinstance(col_def, str):
                col_type = col_def.upper().strip()
                precision = 18
                scale = 2
                nullable = True
            else:
                # Fallback for unexpected definition types
                col_type = "VARCHAR"
                precision = 18
                scale = 2
                nullable = True

            # Resolve Arrow type from the ERP type mapping
            if col_type in ("DECIMAL", "NUMERIC", "NUMBER"):
                # DECIMAL/NUMERIC types require explicit precision and scale
                arrow_type = pa.decimal128(precision, scale)
            elif col_type in ERP_TYPE_MAP:
                arrow_type = ERP_TYPE_MAP[col_type]
            else:
                # Default to string for unknown types to prevent data loss
                arrow_type = pa.string()

            field = pa.field(col_name, arrow_type, nullable=nullable)
            fields.append(field)

        return pa.schema(fields)

    def _dataframe_to_table(
        self,
        data: pd.DataFrame,
        schema: Optional[pa.Schema] = None,
    ) -> pa.Table:
        """Convert a pandas DataFrame to a PyArrow Table.

        Performs type-safe conversion from pandas DataFrame to Arrow columnar
        format. When a schema is provided, enforces strict type mapping;
        otherwise infers types from the DataFrame's dtypes.

        If strict conversion with the provided schema fails (e.g., due to
        incompatible types), falls back to inferring types and then attempts
        per-column casting for maximum data preservation.

        Args:
            data: DataFrame to convert. May be empty.
            schema: Optional Arrow schema for type enforcement. If None,
                types are inferred from the DataFrame dtypes.

        Returns:
            PyArrow Table with the converted data.

        Raises:
            pyarrow.ArrowInvalid: If data cannot be converted even with
                fallback casting logic.
        """
        if schema is not None:
            try:
                table = pa.Table.from_pandas(
                    data,
                    schema=schema,
                    preserve_index=False,
                    safe=True,
                )
            except (
                pa.ArrowInvalid,
                pa.ArrowTypeError,
                pa.ArrowNotImplementedError,
                KeyError,
            ):
                # Fallback: convert without schema, then cast columns individually
                table = pa.Table.from_pandas(data, preserve_index=False)
                cast_columns: Dict[str, pa.Array] = {}

                for field in schema:
                    if field.name in data.columns:
                        source_col = table.column(field.name)
                        try:
                            cast_columns[field.name] = source_col.cast(
                                field.type, safe=False
                            )
                        except (
                            pa.ArrowInvalid,
                            pa.ArrowNotImplementedError,
                            pa.ArrowTypeError,
                        ):
                            # Keep original column type if cast is impossible
                            cast_columns[field.name] = source_col
                    else:
                        # Column defined in schema but absent in data: fill nulls
                        cast_columns[field.name] = pa.nulls(
                            len(data), type=field.type
                        )

                # Rebuild table with cast columns in schema field order
                table = pa.table(cast_columns)
        else:
            table = pa.Table.from_pandas(data, preserve_index=False)

        return table

    def _get_write_options(self) -> Dict[str, Any]:
        """Build pq.write_table() keyword arguments from configuration.

        Translates the ParquetFormatterConfig fields into the keyword arguments
        accepted by PyArrow's ``pq.write_table()`` function. Only includes
        optional parameters when they are explicitly set to avoid overriding
        PyArrow's own defaults.

        Returns:
            Dict of keyword arguments for ``pq.write_table()``.
        """
        # Determine compression value: None means no compression in PyArrow
        compression_value: Optional[str] = None
        if self._config.compression != ParquetCompression.NONE:
            compression_value = self._config.compression.value

        options: Dict[str, Any] = {
            "compression": compression_value,
            "row_group_size": self._config.row_group_size,
            "use_dictionary": self._config.use_dictionary,
            "write_statistics": self._config.write_statistics,
            "version": self._config.version,
            "use_deprecated_int96_timestamps": (
                self._config.use_deprecated_int96_timestamps
            ),
            "allow_truncated_timestamps": self._config.allow_truncated_timestamps,
        }

        # Add optional compression level when specified
        if (
            self._config.compression_level is not None
            and compression_value is not None
        ):
            options["compression_level"] = self._config.compression_level

        # Add timestamp coercion setting
        if self._config.coerce_timestamps is not None:
            options["coerce_timestamps"] = self._config.coerce_timestamps

        # Add data page size when explicitly configured
        if self._config.data_page_size is not None:
            options["data_page_size"] = self._config.data_page_size

        # Add Spark compatibility flavor when specified
        if self._config.flavor is not None:
            options["flavor"] = self._config.flavor

        return options

    def _add_file_metadata(
        self,
        schema: pa.Schema,
        table_name: str,
        record_count: int = 0,
    ) -> pa.Schema:
        """Add custom metadata to the Parquet schema.

        Embeds provenance metadata in the Parquet file's schema metadata section.
        This metadata is preserved in the file footer and can be read by any
        Parquet reader without scanning the data. Essential for audit trails and
        data lineage tracking in enterprise ERP environments, supporting SOC 2
        Type II compliance requirements.

        Metadata keys embedded:
            - ``table_name``: Source ERP table name
            - ``generated_by``: Platform identifier
            - ``generated_at``: ISO 8601 UTC generation timestamp
            - ``record_count``: Number of data records in the file
            - ``platform_version``: Platform version string

        Args:
            schema: Arrow schema to annotate with metadata.
            table_name: Name of the ERP table.
            record_count: Number of records in the output file.

        Returns:
            New Arrow schema with embedded metadata. Arrow schemas are
            immutable, so a new instance is returned.
        """
        # Build platform metadata using bytes keys/values as required by Arrow
        metadata: Dict[bytes, bytes] = {
            b"table_name": table_name.encode("utf-8"),
            b"generated_by": b"Synthetic-ERP-Data-Generation-Platform",
            b"generated_at": (
                datetime.now(timezone.utc).isoformat().encode("utf-8")
            ),
            b"record_count": str(record_count).encode("utf-8"),
            b"platform_version": PLATFORM_VERSION.encode("utf-8"),
        }

        # Merge with user-provided file metadata from config
        if self._config.file_metadata:
            for key, value in self._config.file_metadata.items():
                metadata[key.encode("utf-8")] = value.encode("utf-8")

        # Preserve any existing schema metadata (e.g., from pandas conversion)
        if schema.metadata:
            existing: Dict[bytes, bytes] = dict(schema.metadata)
            existing.update(metadata)
            metadata = existing

        return schema.with_metadata(metadata)

    def _get_column_encoding(
        self, column_name: str, column_type: str
    ) -> Optional[str]:
        """Select optimal column encoding based on data type.

        Determines the best Parquet encoding for a column based on its ERP
        data type. Proper encoding selection can significantly improve both
        compression ratio and read performance for analytical workloads.

        Encoding recommendations by data type:
            - **PLAIN**: Default encoding, suitable for large text/CLOB data
              where dictionary encoding is ineffective.
            - **DELTA_BINARY_PACKED**: Efficient for sorted or nearly-sorted
              integers and timestamps. Stores deltas between consecutive values.
            - **BYTE_STREAM_SPLIT**: Optimal for floating-point and decimal data.
              Splits IEEE 754 bytes into separate streams for better compression.
            - **RLE_DICTIONARY**: Best for low-cardinality columns. Applied
              automatically via the ``use_dictionary`` config flag.

        Args:
            column_name: Name of the column (for logging/context).
            column_type: Uppercase ERP type name (e.g., 'VARCHAR', 'INTEGER').

        Returns:
            Recommended encoding name string, or None to use the PyArrow
            default encoding for the column's data type.
        """
        col_type_upper = column_type.upper().strip()

        # Large string/CLOB columns: PLAIN encoding
        # Dictionary encoding is ineffective for high-cardinality text data
        if col_type_upper in ("TEXT", "CLOB", "NCLOB", "BLOB"):
            return "PLAIN"

        # Integer columns: delta binary packing
        # Excellent for sequential IDs and sorted integer columns common in
        # ERP primary keys and transaction identifiers
        if col_type_upper in ("INTEGER", "INT", "BIGINT", "SMALLINT", "TINYINT"):
            return "DELTA_BINARY_PACKED"

        # Floating-point columns: byte stream split
        # Splits IEEE 754 float bytes into separate streams, exploiting the
        # fact that exponent bytes often compress better than mantissa bytes
        if col_type_upper in ("FLOAT", "REAL", "DOUBLE", "DOUBLE PRECISION"):
            return "BYTE_STREAM_SPLIT"

        # DECIMAL/NUMERIC columns: byte stream split
        # Financial amounts in ERP systems benefit from this encoding
        if col_type_upper in ("DECIMAL", "NUMERIC", "NUMBER"):
            return "BYTE_STREAM_SPLIT"

        # Boolean columns: PyArrow uses RLE by default, which is optimal
        if col_type_upper in ("BOOLEAN", "BOOL"):
            return None

        # Date/timestamp columns: delta binary packing
        # Temporal sequences in ERP data (posting dates, transaction timestamps)
        # compress well with delta encoding
        if col_type_upper in ("DATE", "TIMESTAMP", "DATETIME", "DATETIME2", "TIME"):
            return "DELTA_BINARY_PACKED"

        # Default: let PyArrow choose the optimal encoding
        return None
