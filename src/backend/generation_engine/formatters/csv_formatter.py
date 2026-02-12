"""CSV output formatter for the Synthetic ERP Data Generation Engine.

Converts generated synthetic ERP data (as :class:`pandas.DataFrame` instances)
into CSV files with configurable delimiters, quoting strategies, encoding
options, and header configuration.  Implements the :class:`BaseFormatter`
interface for consistent dispatch from the generation orchestrator and batch
processor.

Supported format variants:

- **Standard CSV** — RFC 4180-compliant comma-separated values.
- **TSV** — Tab-separated values for clipboard and spreadsheet use.
- **Pipe-delimited** — Common in legacy ERP data interchange.
- **Excel CSV** — UTF-8 with BOM for seamless Excel import.
- **Custom** — Arbitrary delimiter, quoting, encoding, and NULL handling.

Streaming output is supported via :meth:`CSVFormatter.format_to_stream` and
:meth:`CSVFormatter.format_batch` for large datasets (millions of rows)
without incurring excessive memory pressure.  File-splitting into multiple
output files is available via :meth:`CSVFormatter.format_to_files` when
``max_rows_per_file`` is configured.

This module fulfils multi-format export feature **F-009** for CSV output.

Typical usage::

    from generation_engine.formatters.csv_formatter import (
        CSVFormatter,
        CSVFormatterConfig,
        CSVQuoting,
    )

    # Standard CSV with defaults
    formatter = CSVFormatter()
    csv_string = formatter.format(dataframe, "gl_journal_entries")

    # Excel-compatible with BOM
    config = CSVFormatter.excel_csv()
    formatter = CSVFormatter(config)
    csv_string = formatter.format(dataframe, "hr_employees")

    # Stream to file
    with open("output.csv", "wb") as f:
        formatter.format_to_stream(dataframe, "hr_employees", col_defs, f)
"""

from __future__ import annotations

import csv
import io
import math
from collections.abc import Iterator
from datetime import date, datetime
from decimal import Decimal
from enum import Enum
from pathlib import Path
from typing import Any, Dict, List, Optional, Union

import pandas as pd
from pydantic import BaseModel, Field

from .base import BaseFormatter

# ---------------------------------------------------------------------------
# Module-level constants
# ---------------------------------------------------------------------------

_UTF8_BOM_BYTES: bytes = b"\xef\xbb\xbf"
"""UTF-8 Byte Order Mark as raw bytes for binary streams."""

_UTF8_BOM_CHAR: str = "\ufeff"
"""UTF-8 Byte Order Mark as a Unicode character for text streams."""

_DEFAULT_CHUNK_SIZE: int = 10_000
"""Default number of rows processed per write chunk to bound memory usage."""


# ---------------------------------------------------------------------------
# CSVQuoting enumeration
# ---------------------------------------------------------------------------


class CSVQuoting(Enum):
    """Maps logical quoting strategies to :mod:`csv` module constants.

    Used as the type for :attr:`CSVFormatterConfig.quoting` and passed
    directly to :func:`csv.writer` via ``quoting=<member>.value``.

    Members:
        MINIMAL: Quote fields only when they contain the delimiter,
            quotechar, or line terminator (default, RFC 4180 behaviour).
        ALL: Quote every field unconditionally.
        NONNUMERIC: Quote all non-numeric fields; useful when downstream
            consumers need to distinguish strings from numbers.
        NONE: Never quote; requires ``escapechar`` to be set on the
            configuration so the delimiter can be escaped within fields.
    """

    MINIMAL = csv.QUOTE_MINIMAL
    ALL = csv.QUOTE_ALL
    NONNUMERIC = csv.QUOTE_NONNUMERIC
    NONE = csv.QUOTE_NONE


# ---------------------------------------------------------------------------
# CSVFormatterConfig Pydantic model
# ---------------------------------------------------------------------------


class CSVFormatterConfig(BaseModel):
    """Validated configuration for the CSV formatter.

    All fields carry sensible defaults that produce RFC 4180-compliant
    output.  Create alternative configurations via the class-method
    presets on :class:`CSVFormatter` (e.g. :meth:`CSVFormatter.tsv`,
    :meth:`CSVFormatter.excel_csv`) or by constructing this model
    directly.

    Attributes:
        delimiter: Field separator character.  Defaults to ``','``.
        quotechar: Character used to quote fields that contain special
            characters.  Defaults to ``'"'``.
        escapechar: Escape character used when ``quoting`` is
            :attr:`CSVQuoting.NONE`.  ``None`` means the *quotechar* is
            doubled (standard CSV escaping).
        quoting: Quoting strategy drawn from :class:`CSVQuoting`.
        line_terminator: End-of-line sequence.  ``'\\r\\n'`` for RFC 4180.
        include_header: Whether to emit a header row with column names.
        encoding: Character encoding for file/stream output.
        include_bom: If ``True``, a UTF-8 BOM is prepended so that
            Microsoft Excel correctly detects the encoding.
        null_representation: String written for ``None`` / ``NaN`` / ``NaT``
            values.  Empty string by default.
        date_format: :func:`strftime` pattern for :class:`datetime.date`.
        datetime_format: :func:`strftime` pattern for
            :class:`datetime.datetime` and :class:`pandas.Timestamp`.
        decimal_separator: Decimal point character.  ``'.'`` by default;
            set to ``','`` for European number formatting.
        column_order: Explicit column ordering.  Columns present in the
            list are emitted first in the specified order; remaining
            DataFrame columns follow in their original order.
        max_rows_per_file: When set, :meth:`CSVFormatter.format_to_files`
            splits output into multiple files of at most this many data
            rows each.  ``None`` disables splitting.
    """

    delimiter: str = Field(
        default=",",
        min_length=1,
        max_length=1,
        description="Field separator character.",
    )
    quotechar: str = Field(
        default='"',
        min_length=1,
        max_length=1,
        description="Character used to quote fields containing special characters.",
    )
    escapechar: Optional[str] = Field(
        default=None,
        description=(
            "Escape character for QUOTE_NONE mode. "
            "None means the quotechar is doubled."
        ),
    )
    quoting: CSVQuoting = Field(
        default=CSVQuoting.MINIMAL,
        description="Quoting strategy for CSV output.",
    )
    line_terminator: str = Field(
        default="\r\n",
        description="End-of-line sequence (RFC 4180 default: CRLF).",
    )
    include_header: bool = Field(
        default=True,
        description="Whether to emit a header row with column names.",
    )
    encoding: str = Field(
        default="utf-8",
        description="Character encoding for file and stream output.",
    )
    include_bom: bool = Field(
        default=False,
        description="Prepend UTF-8 BOM for Excel compatibility.",
    )
    null_representation: str = Field(
        default="",
        description="String written for None / NaN / NaT values.",
    )
    date_format: str = Field(
        default="%Y-%m-%d",
        description="strftime pattern for date values.",
    )
    datetime_format: str = Field(
        default="%Y-%m-%dT%H:%M:%S",
        description="strftime pattern for datetime values.",
    )
    decimal_separator: str = Field(
        default=".",
        min_length=1,
        max_length=1,
        description="Decimal point character (e.g. '.' or ',').",
    )
    column_order: Optional[List[str]] = Field(
        default=None,
        description=(
            "Explicit column ordering. Columns not listed follow in "
            "their original DataFrame order."
        ),
    )
    max_rows_per_file: Optional[int] = Field(
        default=None,
        ge=1,
        description=(
            "Maximum data rows per output file. None disables file splitting."
        ),
    )


# ---------------------------------------------------------------------------
# Internal helper: binary stream text proxy
# ---------------------------------------------------------------------------


class _BinaryStreamTextProxy:
    """Lightweight proxy that encodes text writes for a binary stream.

    The :mod:`csv` module's :func:`csv.writer` requires a file-like
    object whose ``write`` method accepts :class:`str`.  When the
    caller-provided output stream is binary (e.g. an open file in
    ``'wb'`` mode, :class:`io.BytesIO`, or a network socket wrapper),
    this proxy transparently encodes each text write to the configured
    character encoding before forwarding to the underlying stream.

    Args:
        binary_stream: The underlying binary-mode stream.
        encoding: Character encoding to apply (e.g. ``'utf-8'``).
    """

    __slots__ = ("_stream", "_encoding")

    def __init__(self, binary_stream: io.IOBase, encoding: str) -> None:
        self._stream = binary_stream
        self._encoding = encoding

    def write(self, text: str) -> int:
        """Encode *text* and write to the underlying binary stream.

        Args:
            text: String data to encode and write.

        Returns:
            Number of characters in the original *text* (not encoded bytes).
        """
        encoded: bytes = text.encode(self._encoding)
        self._stream.write(encoded)
        return len(text)


# ---------------------------------------------------------------------------
# CSVFormatter — concrete BaseFormatter implementation
# ---------------------------------------------------------------------------


class CSVFormatter(BaseFormatter):
    """CSV output formatter implementing the :class:`BaseFormatter` interface.

    Converts :class:`pandas.DataFrame` instances containing generated
    synthetic ERP data into CSV output with comprehensive formatting
    control.  Supports in-memory formatting, streaming to arbitrary
    :class:`io.IOBase` targets, batch processing for multi-million-row
    datasets, and file-splitting for export workflows.

    The formatter handles:

    * Configurable delimiters (comma, tab, pipe, custom)
    * RFC 4180-compliant quoting and escaping
    * UTF-8 BOM injection for Excel compatibility
    * NULL/NaN/NaT representation with configurable placeholder strings
    * Date, datetime, and decimal formatting with locale-aware separators
    * Column reordering for downstream schema alignment
    * Chunked row writing to bound memory during large exports
    * Multi-file splitting when ``max_rows_per_file`` is configured

    Args:
        config: Formatter configuration.  If ``None``, standard CSV
            defaults (RFC 4180) are used.

    Example:
        Basic usage::

            formatter = CSVFormatter()
            output = formatter.format(df, "gl_journal_entries")

        With custom configuration::

            config = CSVFormatterConfig(
                delimiter="|",
                include_bom=True,
                null_representation="NULL",
            )
            formatter = CSVFormatter(config)
            output = formatter.format(df, "hr_employees")
    """

    def __init__(self, config: Optional[CSVFormatterConfig] = None) -> None:
        """Initialise the CSV formatter with the given configuration.

        Args:
            config: A :class:`CSVFormatterConfig` instance.  When
                ``None``, a default configuration producing standard
                RFC 4180 CSV output is used.
        """
        self.config: CSVFormatterConfig = (
            config if config is not None else CSVFormatterConfig()
        )

    # ------------------------------------------------------------------
    # BaseFormatter abstract method implementations
    # ------------------------------------------------------------------

    def format(
        self,
        data: pd.DataFrame,
        table_name: str,
        column_definitions: Optional[Dict[str, Any]] = None,
    ) -> str:
        """Format the entire DataFrame to a CSV string in memory.

        Materialises the complete CSV content in a :class:`io.StringIO`
        buffer and returns it as a string.  Suitable for datasets that
        comfortably fit in memory.  For larger datasets, prefer
        :meth:`format_to_stream` or :meth:`format_batch`.

        Args:
            data: DataFrame containing generated synthetic ERP records.
            table_name: Qualified ERP table name (e.g.
                ``"gl_journal_entries"``).  Not used directly in CSV
                output but available for logging and diagnostics.
            column_definitions: Optional column-name → type-metadata
                mapping.  Currently reserved for future type-aware
                formatting enhancements.

        Returns:
            The complete CSV content as a string, including BOM, header,
            and all data rows according to the current configuration.

        Raises:
            ValueError: If *data* is not a :class:`pandas.DataFrame`.
        """
        if not isinstance(data, pd.DataFrame):
            raise ValueError(
                f"Expected pandas DataFrame, got {type(data).__name__}"
            )

        data = self._apply_column_order(data)
        buffer = io.StringIO()

        # Prepend UTF-8 BOM as Unicode character if configured
        if self.config.include_bom:
            buffer.write(_UTF8_BOM_CHAR)

        writer = self._get_csv_writer(buffer)

        if self.config.include_header:
            self._write_header(writer, list(data.columns))

        if not data.empty:
            self._write_rows(writer, data)

        return buffer.getvalue()

    def format_to_stream(
        self,
        data: pd.DataFrame,
        table_name: str,
        column_definitions: dict,
        output: io.IOBase,
    ) -> None:
        """Stream CSV output to *output* for large datasets.

        Writes CSV data directly to the provided stream, processing rows
        in chunks of :data:`_DEFAULT_CHUNK_SIZE` (10,000) to bound
        memory usage.  Automatically detects whether *output* is a text
        or binary stream and adapts accordingly.

        Args:
            data: DataFrame containing generated synthetic ERP records.
            table_name: Qualified ERP table name.
            column_definitions: Column-name → type-metadata mapping.
            output: Writable stream (:class:`io.IOBase`).  May be text
                mode (e.g. :class:`io.StringIO`) or binary mode (e.g.
                :class:`io.BytesIO`, file opened with ``'wb'``).

        Raises:
            ValueError: If *data* is not a :class:`pandas.DataFrame`.
            IOError: If writing to *output* fails.
        """
        if not isinstance(data, pd.DataFrame):
            raise ValueError(
                f"Expected pandas DataFrame, got {type(data).__name__}"
            )

        data = self._apply_column_order(data)
        is_binary = self._is_binary_stream(output)

        # Prepend BOM before any CSV content
        if self.config.include_bom:
            self._add_bom(output)

        # Create a text-compatible write target for csv.writer
        text_target: Any = (
            _BinaryStreamTextProxy(output, self.config.encoding)
            if is_binary
            else output
        )
        writer = self._get_csv_writer(text_target)

        if self.config.include_header:
            self._write_header(writer, list(data.columns))

        if not data.empty:
            self._write_rows(writer, data)

        # Flush to ensure all buffered data reaches the stream
        if hasattr(output, "flush"):
            output.flush()

    def get_file_extension(self) -> str:
        """Return the canonical file extension based on the delimiter.

        Returns:
            ``'.tsv'`` when the delimiter is a tab character, otherwise
            ``'.csv'``.
        """
        if self.config.delimiter == "\t":
            return ".tsv"
        return ".csv"

    def get_content_type(self) -> str:
        """Return the MIME content type for HTTP responses.

        Returns:
            ``'text/csv'`` — the standard MIME type for CSV data
            regardless of the specific delimiter in use.
        """
        return "text/csv"

    # ------------------------------------------------------------------
    # Override: format_batch with single-header semantics
    # ------------------------------------------------------------------

    def format_batch(
        self,
        data_batches: Iterator[pd.DataFrame],
        table_name: str,
        column_definitions: dict,
        output: io.IOBase,
    ) -> int:
        """Write multiple batches to *output* with a single header.

        Overrides the base-class default to ensure the CSV header row is
        written only once (before the first batch), rather than being
        repeated for each batch.  This is critical for producing valid
        multi-batch CSV output.

        Args:
            data_batches: Iterator yielding DataFrames, each typically
                containing 10,000 rows from the generation engine's
                batch processor.
            table_name: Qualified ERP table name.
            column_definitions: Column-name → type-metadata mapping
                (constant across all batches).
            output: Writable stream receiving the formatted CSV data.

        Returns:
            Total number of data rows written across all batches.
        """
        is_binary = self._is_binary_stream(output)

        # BOM once at the start of the stream
        if self.config.include_bom:
            self._add_bom(output)

        text_target: Any = (
            _BinaryStreamTextProxy(output, self.config.encoding)
            if is_binary
            else output
        )
        writer = self._get_csv_writer(text_target)

        total_rows: int = 0
        header_written: bool = False

        for batch in data_batches:
            if not isinstance(batch, pd.DataFrame):
                continue  # Defensive: skip non-DataFrame batches
            batch = self._apply_column_order(batch)

            # Write header only before the first non-empty batch
            if not header_written and self.config.include_header:
                self._write_header(writer, list(batch.columns))
                header_written = True

            if not batch.empty:
                total_rows += self._write_rows(writer, batch)

        # Final flush
        if hasattr(output, "flush"):
            output.flush()

        return total_rows

    # ------------------------------------------------------------------
    # Additional public methods
    # ------------------------------------------------------------------

    def format_to_files(
        self,
        data: pd.DataFrame,
        table_name: str,
        output_dir: str,
    ) -> List[str]:
        """Write CSV output to one or more files on disk.

        When :attr:`CSVFormatterConfig.max_rows_per_file` is set, the
        output is split across multiple files with sequential suffixes
        (e.g. ``gl_journal_entries_0000.csv``,
        ``gl_journal_entries_0001.csv``).  Each file includes its own
        header row when ``include_header`` is enabled.

        Args:
            data: DataFrame containing generated synthetic ERP records.
            table_name: Used as the base filename (sanitised for the
                filesystem).
            output_dir: Directory path for output files.  Created
                (including parents) if it does not exist.

        Returns:
            List of absolute file paths that were written.

        Raises:
            ValueError: If *data* is not a :class:`pandas.DataFrame`.
            OSError: If directory creation or file writing fails.
        """
        if not isinstance(data, pd.DataFrame):
            raise ValueError(
                f"Expected pandas DataFrame, got {type(data).__name__}"
            )

        data = self._apply_column_order(data)
        output_path = Path(output_dir)
        output_path.mkdir(parents=True, exist_ok=True)

        extension = self.get_file_extension()
        generated_files: List[str] = []

        # Sanitise table_name for use as a filename component
        safe_name = table_name.replace("/", "_").replace("\\", "_")

        if (
            self.config.max_rows_per_file is None
            or len(data) <= self.config.max_rows_per_file
        ):
            # Single output file
            file_path = output_path / f"{safe_name}{extension}"
            self._write_single_file(data, str(file_path))
            generated_files.append(str(file_path))
        else:
            # Split into multiple files
            total_rows = len(data)
            file_index: int = 0
            for start in range(0, total_rows, self.config.max_rows_per_file):
                end = min(start + self.config.max_rows_per_file, total_rows)
                chunk = data.iloc[start:end]
                file_path = (
                    output_path / f"{safe_name}_{file_index:04d}{extension}"
                )
                self._write_single_file(chunk, str(file_path))
                generated_files.append(str(file_path))
                file_index += 1

        return generated_files

    # ------------------------------------------------------------------
    # Predefined format presets (class methods)
    # ------------------------------------------------------------------

    @classmethod
    def standard_csv(cls) -> CSVFormatterConfig:
        """Return a standard comma-delimited CSV configuration.

        Produces RFC 4180-compliant output with comma delimiter,
        double-quote quoting, CRLF line terminators, and UTF-8 encoding.

        Returns:
            A :class:`CSVFormatterConfig` with all default values.
        """
        return CSVFormatterConfig()

    @classmethod
    def tsv(cls) -> CSVFormatterConfig:
        """Return a tab-separated values (TSV) configuration.

        Uses a tab character as the delimiter.  All other settings
        remain at their defaults.

        Returns:
            A :class:`CSVFormatterConfig` with ``delimiter='\\t'``.
        """
        return CSVFormatterConfig(delimiter="\t")

    @classmethod
    def excel_csv(cls) -> CSVFormatterConfig:
        """Return an Excel-compatible CSV configuration.

        Includes a UTF-8 BOM so Microsoft Excel auto-detects the
        encoding.  All fields are quoted for maximum compatibility.

        Returns:
            A :class:`CSVFormatterConfig` with ``include_bom=True``
            and ``quoting=CSVQuoting.ALL``.
        """
        return CSVFormatterConfig(
            include_bom=True,
            quoting=CSVQuoting.ALL,
        )

    @classmethod
    def pipe_delimited(cls) -> CSVFormatterConfig:
        """Return a pipe-delimited configuration.

        Common in legacy ERP data interchange formats and mainframe
        flat-file exports.

        Returns:
            A :class:`CSVFormatterConfig` with ``delimiter='|'``.
        """
        return CSVFormatterConfig(delimiter="|")

    # ------------------------------------------------------------------
    # Private helper methods
    # ------------------------------------------------------------------

    def _write_header(
        self, writer: csv.writer, columns: List[str]
    ) -> None:
        """Write the column header row to the CSV writer.

        Args:
            writer: Configured :class:`csv.writer` instance.
            columns: Ordered list of column names to emit.
        """
        writer.writerow(columns)

    def _write_rows(
        self,
        writer: csv.writer,
        data: pd.DataFrame,
        chunk_size: int = _DEFAULT_CHUNK_SIZE,
    ) -> int:
        """Write data rows in chunks, returning the total row count.

        Iterates through the DataFrame in slices of *chunk_size* rows
        to avoid building a massive intermediate list.  Each cell value
        is passed through :meth:`_format_value` for type-aware
        serialisation (dates, decimals, NaN, etc.).

        Args:
            writer: Configured :class:`csv.writer` instance.
            data: DataFrame to write.
            chunk_size: Number of rows per processing chunk.

        Returns:
            Total number of rows written.
        """
        columns: List[str] = list(data.columns)
        row_count: int = 0
        total_rows: int = len(data)

        for start in range(0, total_rows, chunk_size):
            end = min(start + chunk_size, total_rows)
            chunk = data.iloc[start:end]
            for row in chunk.itertuples(index=False, name=None):
                formatted_row: List[str] = [
                    self._format_value(val, col)
                    for val, col in zip(row, columns)
                ]
                writer.writerow(formatted_row)
                row_count += 1

        return row_count

    def _format_value(
        self, value: Any, column_name: Optional[str] = None
    ) -> str:
        """Format an individual cell value for CSV output.

        Handles ``None``, ``NaN``, ``NaT``, dates, datetimes, decimals,
        floats, and generic values.  The *column_name* parameter is
        accepted for future column-specific formatting extensions but
        is not currently used for dispatch.

        Args:
            value: The raw cell value from the DataFrame.
            column_name: Name of the column (reserved for future use).

        Returns:
            String representation suitable for CSV output.
        """
        # --- Null / missing value handling ---

        if value is None:
            return self.config.null_representation

        # Explicit pandas NaT check (Not a Time — common in ERP date cols)
        if value is pd.NaT:
            return self.config.null_representation

        # pandas-level NA check (handles NaT, numpy.nan, pd.NA)
        try:
            if pd.isna(value):
                return self.config.null_representation
        except (TypeError, ValueError):
            # pd.isna can raise for non-scalar or ambiguous types;
            # fall through to type-specific handling below.
            pass

        # Explicit float NaN guard (redundant with pd.isna but defensive)
        if isinstance(value, float) and math.isnan(value):
            return self.config.null_representation

        # --- Temporal types (datetime before date — datetime ⊂ date) ---

        if isinstance(value, datetime):
            return value.strftime(self.config.datetime_format)

        if isinstance(value, date):
            return value.strftime(self.config.date_format)

        # --- Numeric types with configurable decimal separator ---

        if isinstance(value, Decimal):
            str_val: str = str(value)
            if self.config.decimal_separator != ".":
                str_val = str_val.replace(".", self.config.decimal_separator)
            return str_val

        if isinstance(value, float):
            str_val = str(value)
            if self.config.decimal_separator != ".":
                str_val = str_val.replace(".", self.config.decimal_separator)
            return str_val

        # --- Default: stringify ---

        return str(value)

    def _apply_column_order(self, data: pd.DataFrame) -> pd.DataFrame:
        """Reorder DataFrame columns according to configuration.

        Columns listed in :attr:`CSVFormatterConfig.column_order` that
        exist in *data* are placed first in the specified order.
        Remaining columns follow in their original DataFrame order.
        If ``column_order`` is ``None``, the DataFrame is returned
        unchanged.

        Args:
            data: Input DataFrame.

        Returns:
            DataFrame with columns in the desired order.  A view is
            returned when possible to avoid unnecessary copies.
        """
        if self.config.column_order is None:
            return data

        # Columns from the ordering that actually exist in the DataFrame
        ordered: List[str] = [
            col for col in self.config.column_order if col in data.columns
        ]
        # Remaining columns not mentioned in column_order
        remaining: List[str] = [
            col for col in data.columns if col not in self.config.column_order
        ]
        final_order: List[str] = ordered + remaining

        if final_order == list(data.columns):
            return data  # Already in the desired order — avoid copy
        return data[final_order]

    def _get_csv_writer(self, output: Any) -> csv.writer:
        """Create a :class:`csv.writer` configured from current settings.

        Args:
            output: Text-mode file-like object (or
                :class:`_BinaryStreamTextProxy`) whose ``write`` method
                accepts :class:`str`.

        Returns:
            A configured :class:`csv.writer` instance.
        """
        kwargs: Dict[str, Any] = {
            "delimiter": self.config.delimiter,
            "quotechar": self.config.quotechar,
            "quoting": self.config.quoting.value,
            "lineterminator": self.config.line_terminator,
        }
        if self.config.escapechar is not None:
            kwargs["escapechar"] = self.config.escapechar
        return csv.writer(output, **kwargs)

    def _add_bom(self, output: io.IOBase) -> None:
        """Write a UTF-8 BOM to the output stream if configured.

        Detects whether the stream is binary or text and writes the
        appropriate representation.

        Args:
            output: Target stream (text or binary mode).
        """
        if self._is_binary_stream(output):
            output.write(_UTF8_BOM_BYTES)
        else:
            output.write(_UTF8_BOM_CHAR)

    def _write_single_file(
        self, data: pd.DataFrame, file_path: str
    ) -> None:
        """Write a single CSV file to disk.

        Opens the file in text mode with the configured encoding,
        writes the BOM (if enabled), header, and all data rows.

        Args:
            data: DataFrame slice to write.
            file_path: Absolute or relative path for the output file.
        """
        with open(
            file_path,
            mode="w",
            newline="",
            encoding=self.config.encoding,
        ) as file_handle:
            if self.config.include_bom:
                file_handle.write(_UTF8_BOM_CHAR)

            writer = self._get_csv_writer(file_handle)

            if self.config.include_header:
                self._write_header(writer, list(data.columns))

            if not data.empty:
                self._write_rows(writer, data)

    @staticmethod
    def _is_binary_stream(stream: io.IOBase) -> bool:
        """Determine whether *stream* is a binary-mode stream.

        Uses :mod:`io` abstract base classes for reliable detection,
        with a fallback to the ``mode`` attribute for file objects.

        Args:
            stream: Stream to inspect.

        Returns:
            ``True`` if the stream expects :class:`bytes`, ``False``
            if it expects :class:`str`.
        """
        if isinstance(stream, (io.RawIOBase, io.BufferedIOBase, io.BytesIO)):
            return True
        if isinstance(stream, (io.TextIOBase, io.StringIO)):
            return False
        # Fallback: check for a ``mode`` attribute (standard file objects)
        if hasattr(stream, "mode"):
            return "b" in getattr(stream, "mode", "")
        # Conservative default: assume binary to avoid encoding errors
        return True
