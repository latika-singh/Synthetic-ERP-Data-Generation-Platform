"""JSON and JSONL output formatter for the Generation Engine.

Converts generated synthetic ERP data from pandas DataFrames into JSON
(standard array) or JSON Lines (NDJSON, one record per line) format with
configurable pretty-printing, streaming support, and custom type
serialization for ERP-specific data types.

Implements the :class:`BaseFormatter` interface from
``generation_engine.formatters.base`` for consistent dispatch from the
generation orchestrator and batch processor.

Supports:
    - Standard JSON array format (``[{...}, {...}, ...]``)
    - JSON Lines / NDJSON streaming format (one JSON object per line)
    - Configurable indentation and pretty-printing
    - Custom datetime/date/Decimal/numpy/UUID/bytes serialization
    - Metadata wrapper with table name, record count, and timestamp
    - Chunked streaming output for memory-efficient large dataset processing
    - Multi-format export as required by feature F-009

Typical usage::

    from generation_engine.formatters.json_formatter import (
        JSONFormatter,
        JSONFormatterConfig,
        JSONOutputMode,
    )

    # Standard JSON array output
    config = JSONFormatterConfig(output_mode=JSONOutputMode.JSON_ARRAY, pretty_print=True)
    formatter = JSONFormatter(config=config)
    json_output = formatter.format(dataframe, "gl_journal_entries")

    # JSONL streaming output
    config = JSONFormatterConfig(output_mode=JSONOutputMode.JSON_LINES)
    formatter = JSONFormatter(config=config)
    with open("output.jsonl", "w") as f:
        formatter.format_to_stream(dataframe, "gl_journal_entries", {}, f)
"""

from __future__ import annotations

import base64
import io
import json
import uuid
from datetime import UTC, date, datetime
from decimal import Decimal
from enum import Enum
from typing import TYPE_CHECKING, Any

import numpy as np
import pandas as pd
from pydantic import BaseModel, Field

from generation_engine.formatters.base import BaseFormatter


if TYPE_CHECKING:
    from collections.abc import Callable, Iterator


# ======================================================================
# Enumerations
# ======================================================================


class JSONOutputMode(Enum):
    """Output format mode for JSON serialization.

    Controls whether the formatter produces a standard JSON array of
    objects or JSON Lines (NDJSON) output with one object per line.

    Attributes:
        JSON_ARRAY: Standard JSON array format ``[{...}, {...}]``.
            Produces a single valid JSON document containing all records.
        JSON_LINES: JSON Lines (NDJSON) format with one JSON object per
            line.  Each line is an independently valid JSON document.
            Preferred for streaming and large-dataset processing.
    """

    JSON_ARRAY = "json"
    JSON_LINES = "jsonl"


# ======================================================================
# Configuration
# ======================================================================


class JSONFormatterConfig(BaseModel):
    """Configuration for JSON/JSONL output formatting.

    Pydantic-validated configuration model controlling all aspects of
    JSON serialization including output mode, pretty-printing, type
    serialization options, metadata inclusion, and streaming chunk size.

    Attributes:
        output_mode: Format mode — JSON array or JSON Lines.
        pretty_print: Enable indented, human-readable output.
        indent: Number of spaces for indentation when pretty-printing.
        sort_keys: Sort JSON object keys alphabetically.
        ensure_ascii: Escape non-ASCII characters to ``\\uXXXX``.
        encoding: Output character encoding.
        date_format: Date serialization (``'iso'`` for ISO 8601,
            ``'epoch'`` for Unix timestamp as integer).
        datetime_format: Datetime serialization (``'iso'`` or ``'epoch'``).
        decimal_as_string: Serialize ``Decimal`` as string to preserve
            precision for financial ERP data (SAP amounts, Oracle EBS
            monetary values).
        null_value: Representation for null/missing values.  ``None``
            produces JSON ``null``.
        include_metadata: Wrap output with metadata envelope containing
            table name, record count, and generation timestamp.
        root_key: Wrap the records array under a named key.  Applied
            only when ``include_metadata`` is ``False``.
        chunk_size: Number of rows per chunk in streaming mode.

    Example::

        config = JSONFormatterConfig(
            output_mode=JSONOutputMode.JSON_LINES,
            pretty_print=False,
            decimal_as_string=True,
            include_metadata=True,
        )
    """

    output_mode: JSONOutputMode = Field(
        default=JSONOutputMode.JSON_ARRAY,
        description="JSON output format mode: json (array) or jsonl (lines).",
    )
    pretty_print: bool = Field(
        default=False,
        description="Enable indented, human-readable JSON output.",
    )
    indent: int = Field(
        default=2,
        ge=0,
        le=16,
        description="Number of spaces for indentation when pretty-printing.",
    )
    sort_keys: bool = Field(
        default=False,
        description="Sort JSON object keys alphabetically.",
    )
    ensure_ascii: bool = Field(
        default=False,
        description="Escape non-ASCII characters to Unicode escape sequences.",
    )
    encoding: str = Field(
        default="utf-8",
        description="Character encoding for the output stream.",
    )
    date_format: str = Field(
        default="iso",
        pattern=r"^(iso|epoch)$",
        description="Date serialization: 'iso' for ISO 8601, 'epoch' for Unix timestamp.",
    )
    datetime_format: str = Field(
        default="iso",
        pattern=r"^(iso|epoch)$",
        description="Datetime serialization: 'iso' for ISO 8601, 'epoch' for Unix timestamp.",
    )
    decimal_as_string: bool = Field(
        default=False,
        description="Serialize Decimal as string to preserve financial precision.",
    )
    null_value: str | None = Field(
        default=None,
        description="Null representation.  None produces JSON null.",
    )
    include_metadata: bool = Field(
        default=False,
        description=("Wrap output with metadata (table_name, record_count, generated_at, format_version)."),
    )
    root_key: str | None = Field(
        default=None,
        description="Wrap records array under a named key (e.g. 'records').",
    )
    chunk_size: int = Field(
        default=10000,
        ge=1,
        le=1_000_000,
        description="Rows per chunk for streaming output.",
    )


# ======================================================================
# Custom JSON Encoder
# ======================================================================


class SyntheticDataEncoder(json.JSONEncoder):
    """Custom JSON encoder for synthetic ERP data types.

    Extends :class:`json.JSONEncoder` to handle non-standard Python
    types commonly found in generated synthetic ERP data including
    ``datetime``, ``date``, ``Decimal``, numpy scalar/array types,
    ``bytes``, ``UUID``, and pandas ``Timestamp`` / ``NaT``.

    Acts as a safety-net encoder invoked by ``json.dumps`` for objects
    not pre-processed by :meth:`JSONFormatter._serialize_value`.

    Args:
        config: Formatter configuration controlling serialization
            behaviour (e.g. ``decimal_as_string``, ``datetime_format``).
        **kwargs: Additional keyword arguments forwarded to the base
            :class:`json.JSONEncoder` (e.g. ``indent``, ``sort_keys``).

    Example::

        encoder = SyntheticDataEncoder(config=JSONFormatterConfig(decimal_as_string=True))
        json.dumps({"amount": Decimal("19.99")}, default=encoder.default)
    """

    def __init__(self, config: JSONFormatterConfig | None = None, **kwargs: Any) -> None:
        """Initialize the encoder with formatter configuration.

        Args:
            config: Optional formatter configuration.  Defaults to a
                fresh :class:`JSONFormatterConfig` with default values.
            **kwargs: Keyword arguments forwarded to the base encoder.
        """
        super().__init__(**kwargs)
        self._config: JSONFormatterConfig = config if config is not None else JSONFormatterConfig()

    def default(self, obj: Any) -> Any:
        """Serialize non-standard types to JSON-compatible values.

        Called by the JSON encoder for objects that are not natively
        serializable (``str``, ``int``, ``float``, ``list``, ``dict``,
        ``bool``, ``None``).

        Type checking order matters — pandas NaT must be checked before
        ``datetime`` since ``isinstance(pd.NaT, datetime)`` is ``True``.

        Args:
            obj: The Python object to serialize.

        Returns:
            A JSON-serializable representation of *obj*.

        Raises:
            TypeError: If *obj* is not a recognized type — delegates to
                the parent encoder which raises ``TypeError``.
        """
        # -- pandas NaT (singleton, subclass of datetime.datetime) ---------
        if obj is pd.NaT:
            return self._config.null_value

        # -- Temporal types (Timestamp → datetime → date) ------------------
        if isinstance(obj, pd.Timestamp):
            return self._encode_timestamp(obj)
        if isinstance(obj, datetime):
            return self._encode_datetime(obj)
        if isinstance(obj, date):
            return self._encode_date(obj)

        # -- Decimal — preserve ERP financial precision -------------------
        if isinstance(obj, Decimal):
            return str(obj) if self._config.decimal_as_string else float(obj)

        # -- numpy types ---------------------------------------------------
        if isinstance(obj, (np.integer, np.floating, np.bool_, np.ndarray)):
            return self._encode_numpy(obj)

        # -- bytes / bytearray → base64 ----------------------------------
        if isinstance(obj, (bytes, bytearray)):
            return base64.b64encode(obj).decode("ascii")

        # -- UUID → string ------------------------------------------------
        if isinstance(obj, uuid.UUID):
            return str(obj)

        # Delegate to parent (raises TypeError for truly unsupported types)
        return super().default(obj)

    # -- Encoder sub-helpers (extracted to reduce branch count) -----------

    def _encode_timestamp(self, obj: pd.Timestamp) -> Any:
        """Encode a pandas Timestamp value."""
        if pd.isna(obj):
            return self._config.null_value
        if self._config.datetime_format == "epoch":
            return obj.timestamp()
        return obj.isoformat()

    def _encode_datetime(self, obj: datetime) -> str | float:
        """Encode a standard datetime value."""
        if self._config.datetime_format == "epoch":
            return obj.timestamp()
        return obj.isoformat()

    def _encode_date(self, obj: date) -> str | int:
        """Encode a standard date value."""
        if self._config.date_format == "epoch":
            epoch_dt = datetime.combine(obj, datetime.min.time())
            return int(epoch_dt.timestamp())
        return obj.isoformat()

    def _encode_numpy(self, obj: Any) -> Any:
        """Encode numpy scalar and array types."""
        if isinstance(obj, np.integer):
            return int(obj)
        if isinstance(obj, np.floating):
            return self._config.null_value if np.isnan(obj) else float(obj)
        if isinstance(obj, np.bool_):
            return bool(obj)
        # np.ndarray
        return obj.tolist()


# ======================================================================
# JSON / JSONL Formatter
# ======================================================================


class JSONFormatter(BaseFormatter):
    """JSON and JSONL output formatter for synthetic ERP data.

    Converts pandas DataFrames containing generated synthetic data into
    JSON array or JSON Lines (NDJSON) format.  Supports configurable
    pretty-printing, custom type serialization, metadata envelopes, and
    chunked streaming for memory-efficient processing of large datasets.

    Implements the :class:`BaseFormatter` abstract interface for
    consistent dispatch from the generation orchestrator.

    Attributes:
        _config: Formatter configuration instance.
        _encoder: Custom JSON encoder for ERP data types.

    Example::

        formatter = JSONFormatter(
            JSONFormatterConfig(
                output_mode=JSONOutputMode.JSON_ARRAY,
                pretty_print=True,
                include_metadata=True,
            )
        )
        result = formatter.format(df, "hr_employees")
    """

    def __init__(
        self,
        config: JSONFormatterConfig | dict | None = None,
    ) -> None:
        """Initialize the JSON formatter with configuration.

        Accepts a :class:`JSONFormatterConfig` instance, a plain ``dict``
        of configuration values (automatically converted to
        :class:`JSONFormatterConfig`), or ``None`` for defaults.

        Args:
            config: Optional formatter configuration.  May be a
                :class:`JSONFormatterConfig` instance, a ``dict`` whose
                keys match :class:`JSONFormatterConfig` field names, or
                ``None`` for a fresh default configuration.

        Raises:
            TypeError: If *config* is not ``None``, a ``dict``, or a
                :class:`JSONFormatterConfig` instance.
            ValidationError: If dict values fail Pydantic validation.
        """
        if config is None:
            self._config: JSONFormatterConfig = JSONFormatterConfig()
        elif isinstance(config, dict):
            self._config = JSONFormatterConfig(**config)
        elif isinstance(config, JSONFormatterConfig):
            self._config = config
        else:
            raise TypeError(f"config must be JSONFormatterConfig, dict, or None; got {type(config).__name__}")
        self._encoder: SyntheticDataEncoder = SyntheticDataEncoder(config=self._config)

    # ------------------------------------------------------------------
    # BaseFormatter abstract method implementations
    # ------------------------------------------------------------------

    def format(
        self,
        data: pd.DataFrame,
        table_name: str,
        column_definitions: dict | None = None,
    ) -> str:
        """Format an entire DataFrame to a JSON or JSONL string.

        Materialises the complete formatted output in memory.  For
        large datasets that may exceed RAM, prefer
        :meth:`format_to_stream`.

        Args:
            data: Pandas DataFrame containing generated synthetic ERP
                data.  Each row represents a single record.
            table_name: Qualified name of the ERP table (e.g.
                ``"gl_journal_entries"``).
            column_definitions: Optional column-name → ERP-type metadata
                mapping for type-aware serialization.

        Returns:
            A JSON string (JSON_ARRAY mode) or JSONL string (JSON_LINES
            mode).

        Raises:
            ValueError: If *data* is ``None``.
            TypeError: If *data* is not a pandas DataFrame.
        """
        if data is None:
            raise ValueError("Input data must not be None.")
        if not isinstance(data, pd.DataFrame):
            raise TypeError(f"Expected pandas DataFrame, got {type(data).__name__}.")

        if self._config.output_mode == JSONOutputMode.JSON_LINES:
            return self._format_json_lines(data, column_definitions)
        return self._format_json_array(data, table_name, column_definitions)

    def format_to_stream(
        self,
        data: pd.DataFrame,
        table_name: str,
        column_definitions: dict,
        output: io.IOBase,
    ) -> None:
        """Stream JSON/JSONL output directly to a writable stream.

        Processes data in chunks of ``config.chunk_size`` rows to bound
        memory usage.  Preferred for datasets exceeding available RAM.

        Args:
            data: Pandas DataFrame containing generated synthetic data.
            table_name: Qualified name of the ERP table.
            column_definitions: Column-name → ERP-type metadata mapping.
            output: Writable stream (file handle, ``StringIO``,
                ``BytesIO``, or network socket wrapper).

        Raises:
            ValueError: If *data* is ``None``.
            TypeError: If *data* is not a pandas DataFrame.
            IOError: If writing to *output* fails.
        """
        if data is None:
            raise ValueError("Input data must not be None.")
        if not isinstance(data, pd.DataFrame):
            raise TypeError(f"Expected pandas DataFrame, got {type(data).__name__}.")

        if self._config.output_mode == JSONOutputMode.JSON_LINES:
            self._stream_json_lines(data, column_definitions, output)
        else:
            self._stream_json_array(data, table_name, column_definitions, output)

    def get_file_extension(self) -> str:
        """Return the canonical file extension for the current output mode.

        Returns:
            ``".json"`` for JSON_ARRAY mode, ``".jsonl"`` for
            JSON_LINES mode.
        """
        if self._config.output_mode == JSONOutputMode.JSON_LINES:
            return ".jsonl"
        return ".json"

    def get_content_type(self) -> str:
        """Return the MIME content type for HTTP responses.

        Returns:
            ``"application/json"`` for JSON_ARRAY mode,
            ``"application/x-ndjson"`` for JSON_LINES mode.
        """
        if self._config.output_mode == JSONOutputMode.JSON_LINES:
            return "application/x-ndjson"
        return "application/json"

    # ------------------------------------------------------------------
    # Override format_batch for correct JSON array batching
    # ------------------------------------------------------------------

    def format_batch(
        self,
        data_batches: Iterator[pd.DataFrame],
        table_name: str,
        column_definitions: dict,
        output: io.IOBase,
    ) -> int:
        """Write multiple batches to *output* with proper JSON structure.

        Overrides the base implementation to handle JSON array mode
        correctly — a single opening bracket, comma-separated records
        across all batches, and a single closing bracket.

        For JSON Lines mode, delegates to the base implementation since
        each batch writes independent lines that concatenate validly.

        Args:
            data_batches: Iterator yielding DataFrames (typically 10 000
                rows per batch from the generation engine).
            table_name: Qualified name of the ERP table.
            column_definitions: Column-name → ERP-type metadata mapping.
            output: Writable stream that receives formatted output.

        Returns:
            Total number of rows written across all batches.
        """
        if self._config.output_mode == JSONOutputMode.JSON_LINES:
            # JSONL batches concatenate safely — use base implementation
            return super().format_batch(data_batches, table_name, column_definitions, output)

        # JSON array mode: produce a single coherent JSON document
        _write = self._get_stream_writer(output)
        indent = self._config.indent if self._config.pretty_print else None
        total_rows: int = 0

        # Collect all records across batches for metadata wrapper
        # (metadata needs total record_count which is only known after
        # processing all batches).  For very large datasets the caller
        # should prefer format_to_stream with a single concatenated DF.
        all_records: list[dict[str, Any]] = []

        for batch in data_batches:
            for _, row in batch.iterrows():
                all_records.append(self._row_to_dict(row, column_definitions))
            total_rows += len(batch)

        # Build output structure
        if self._config.include_metadata:
            output_data: Any = self._add_metadata_wrapper(all_records, table_name)
        elif self._config.root_key:
            output_data = {self._config.root_key: all_records}
        else:
            output_data = all_records

        serialized = json.dumps(
            output_data,
            default=self._encoder.default,
            ensure_ascii=self._config.ensure_ascii,
            sort_keys=self._config.sort_keys,
            indent=indent,
        )
        _write(serialized)
        return total_rows

    # ------------------------------------------------------------------
    # Private formatting methods — JSON Array
    # ------------------------------------------------------------------

    def _format_json_array(
        self,
        data: pd.DataFrame,
        table_name: str,
        column_definitions: dict | None = None,
    ) -> str:
        """Generate standard JSON array output.

        Converts all rows to a list of dictionaries and serializes to a
        single JSON document.  Optionally wraps the array with a
        metadata envelope or a named root key.

        Args:
            data: Source DataFrame.
            table_name: ERP table name (used for metadata envelope).
            column_definitions: Optional type metadata for columns.

        Returns:
            Complete JSON string.
        """
        records: list[dict[str, Any]] = [self._row_to_dict(row, column_definitions) for _, row in data.iterrows()]

        # Determine the output structure
        output_data: Any
        if self._config.include_metadata:
            output_data = self._add_metadata_wrapper(records, table_name)
        elif self._config.root_key:
            output_data = {self._config.root_key: records}
        else:
            output_data = records

        return json.dumps(
            output_data,
            default=self._encoder.default,
            ensure_ascii=self._config.ensure_ascii,
            sort_keys=self._config.sort_keys,
            indent=self._config.indent if self._config.pretty_print else None,
        )

    def _format_json_lines(
        self,
        data: pd.DataFrame,
        column_definitions: dict | None = None,
    ) -> str:
        """Generate JSON Lines (NDJSON) output.

        Each row is serialized as a standalone JSON object on its own
        line.  The output contains no enclosing array brackets.  JSONL
        lines are never individually pretty-printed.

        Args:
            data: Source DataFrame.
            column_definitions: Optional type metadata for columns.

        Returns:
            JSONL string with one JSON object per line, terminated by
            a trailing newline.
        """
        lines: list[str] = []
        for _, row in data.iterrows():
            record = self._row_to_dict(row, column_definitions)
            line = json.dumps(
                record,
                default=self._encoder.default,
                ensure_ascii=self._config.ensure_ascii,
                sort_keys=self._config.sort_keys,
            )
            lines.append(line)
        # JSONL convention: trailing newline after the last record
        return "\n".join(lines) + ("\n" if lines else "")

    # ------------------------------------------------------------------
    # Private streaming methods
    # ------------------------------------------------------------------

    def _stream_json_array(
        self,
        data: pd.DataFrame,
        table_name: str,
        column_definitions: dict | None,
        output: io.IOBase,
    ) -> None:
        """Stream JSON array output to a writable stream.

        Writes the opening bracket, processes data in chunks writing
        comma-separated JSON objects, and finishes with the closing
        bracket.  Handles metadata and root-key envelopes.

        Args:
            data: Source DataFrame.
            table_name: ERP table name for metadata envelope.
            column_definitions: Column type metadata.
            output: Writable stream.
        """
        _write = self._get_stream_writer(output)
        indent = self._config.indent if self._config.pretty_print else None
        pretty = self._config.pretty_print
        chunk_size = self._config.chunk_size
        total_rows = len(data)
        has_envelope = self._config.include_metadata or self._config.root_key is not None

        # ---- Opening structure (delegated to helper) ----
        self._write_array_opening(_write, table_name, total_rows, pretty)

        # ---- Stream records in chunks ----
        first_record = True
        record_indent = self._config.indent * (2 if has_envelope else 1)

        for start_idx in range(0, total_rows, chunk_size):
            end_idx = min(start_idx + chunk_size, total_rows)
            chunk = data.iloc[start_idx:end_idx]

            for _, row in chunk.iterrows():
                record = self._row_to_dict(row, column_definitions)
                record_json = json.dumps(
                    record,
                    default=self._encoder.default,
                    ensure_ascii=self._config.ensure_ascii,
                    sort_keys=self._config.sort_keys,
                    indent=indent,
                )

                if not first_record:
                    _write(",\n" if pretty else ",")
                first_record = False

                if pretty:
                    _write(self._indent_json_block(record_json, record_indent))
                else:
                    _write(record_json)

        # ---- Closing structure (delegated to helper) ----
        self._write_array_closing(_write, pretty)

    # -- Streaming structure helpers (reduce branch count) ---------------

    def _write_array_opening(
        self,
        _write: Any,
        table_name: str,
        total_rows: int,
        pretty: bool,
    ) -> None:
        """Write the opening structure for a streamed JSON array.

        Handles three cases: metadata envelope, root-key envelope, or
        plain array.

        Args:
            _write: Callable that writes a string to the output stream.
            table_name: ERP table name for the metadata envelope.
            total_rows: Total number of records (for metadata).
            pretty: Whether to emit pretty-printed output.
        """
        records_key = self._config.root_key or "records"

        if self._config.include_metadata:
            meta: dict[str, Any] = {
                "table_name": table_name,
                "record_count": total_rows,
                "generated_at": datetime.now(tz=UTC).isoformat(),
                "format_version": "1.0",
            }
            if pretty:
                _write("{\n")
                pad = " " * self._config.indent
                for key, val in meta.items():
                    _write(f'{pad}"{key}": {json.dumps(val)},\n')
                _write(f'{pad}"{records_key}": [\n')
            else:
                prefix_parts = ", ".join(f'"{k}": {json.dumps(v)}' for k, v in meta.items())
                _write(f'{{{prefix_parts}, "{records_key}": [')
        elif self._config.root_key:
            if pretty:
                _write("{\n")
                pad = " " * self._config.indent
                _write(f'{pad}"{self._config.root_key}": [\n')
            else:
                _write(f'{{"{self._config.root_key}": [')
        else:
            _write("[\n" if pretty else "[")

    def _write_array_closing(self, _write: Any, pretty: bool) -> None:
        """Write the closing structure for a streamed JSON array.

        Matches the opening written by :meth:`_write_array_opening`.

        Args:
            _write: Callable that writes a string to the output stream.
            pretty: Whether to emit pretty-printed output.
        """
        if self._config.include_metadata or self._config.root_key:
            if pretty:
                pad = " " * self._config.indent
                _write(f"\n{pad}]\n}}\n")
            else:
                _write("]}")
        else:
            _write("\n]\n" if pretty else "]")

    def _stream_json_lines(
        self,
        data: pd.DataFrame,
        column_definitions: dict | None,
        output: io.IOBase,
    ) -> None:
        """Stream JSON Lines output to a writable stream.

        Processes data in chunks, writing one JSON object per line
        without pretty-printing.  Each line is an independently valid
        JSON document followed by a newline character.

        Args:
            data: Source DataFrame.
            column_definitions: Column type metadata.
            output: Writable stream.
        """
        _write = self._get_stream_writer(output)
        chunk_size = self._config.chunk_size
        total_rows = len(data)

        for start_idx in range(0, total_rows, chunk_size):
            end_idx = min(start_idx + chunk_size, total_rows)
            chunk = data.iloc[start_idx:end_idx]

            for _, row in chunk.iterrows():
                record = self._row_to_dict(row, column_definitions)
                line = json.dumps(
                    record,
                    default=self._encoder.default,
                    ensure_ascii=self._config.ensure_ascii,
                    sort_keys=self._config.sort_keys,
                )
                _write(line + "\n")

    # ------------------------------------------------------------------
    # Row and value processing helpers
    # ------------------------------------------------------------------

    def _row_to_dict(
        self,
        row: pd.Series,
        column_definitions: dict | None = None,
    ) -> dict[str, Any]:
        """Convert a DataFrame row to a dictionary with type handling.

        Iterates over each column value, resolves the column's ERP type
        from *column_definitions* (when available), and delegates to
        :meth:`_serialize_value` for type-aware conversion.

        Args:
            row: A single row from a pandas DataFrame.
            column_definitions: Optional mapping of column names to ERP
                type metadata.  Supports both string values
                (``"DECIMAL"``) and dict values
                (``{"type": "DECIMAL", "precision": 15}``).

        Returns:
            A dictionary with column names as keys and serialized values.
        """
        result: dict[str, Any] = {}
        for col_name, value in row.items():
            col_type: str | None = None
            if column_definitions and col_name in column_definitions:
                col_meta = column_definitions[col_name]
                if isinstance(col_meta, dict):
                    col_type = col_meta.get("type")
                elif isinstance(col_meta, str):
                    col_type = col_meta
            result[str(col_name)] = self._serialize_value(value, col_type)
        return result

    def _add_metadata_wrapper(
        self,
        records: list[dict[str, Any]],
        table_name: str,
    ) -> dict[str, Any]:
        """Wrap records with a metadata envelope.

        Creates a JSON object containing metadata fields (table name,
        record count, generation timestamp, format version) alongside
        the records array.  The records key defaults to ``"records"``
        but can be overridden via ``config.root_key``.

        Args:
            records: List of record dictionaries.
            table_name: Qualified ERP table name.

        Returns:
            A dictionary with metadata fields and the records array.
        """
        records_key = self._config.root_key or "records"
        return {
            "table_name": table_name,
            "record_count": len(records),
            "generated_at": datetime.now(tz=UTC).isoformat(),
            "format_version": "1.0",
            records_key: records,
        }

    def _serialize_value(
        self,
        value: Any,
        column_type: str | None = None,
    ) -> Any:
        """Serialize an individual value with type-aware conversion.

        Converts Python, numpy, and pandas types to JSON-compatible
        values.  Handles null / NaN / NaT detection, datetime
        formatting, Decimal precision, numpy type coercion, bytes
        encoding, and UUID stringification.

        When *column_type* is provided (from the ERP schema metadata),
        it is used to resolve ambiguous cases — e.g. a Python ``float``
        that the schema says is ``DECIMAL`` can be forced through
        :pyattr:`decimal_as_string` serialization.

        Type checking order is significant:
            1. ``None`` and ``pd.NaT`` first (fast singleton checks).
            2. ``pd.Timestamp`` before ``datetime`` (subclass).
            3. ``datetime`` before ``date`` (subclass).
            4. ``np.floating`` with NaN guard before plain ``float``.

        Args:
            value: The raw value from the DataFrame cell.
            column_type: Optional ERP column type hint (e.g.
                ``"DECIMAL"``, ``"TIMESTAMP"``, ``"VARCHAR"``).
                Used to influence serialization when the Python type
                alone is ambiguous.

        Returns:
            A JSON-serializable value (``str``, ``int``, ``float``,
            ``bool``, ``None``, ``list``, or ``dict``).
        """
        # -- Null-like values --------------------------------------------
        if value is None:
            return self._config.null_value

        if value is pd.NaT:
            return self._config.null_value

        # Normalise column_type for case-insensitive comparison.
        _col_type_upper = column_type.upper() if column_type else None

        # -- pandas Timestamp (extends datetime) -------------------------
        if isinstance(value, pd.Timestamp):
            return self._serialize_temporal(value)

        # -- datetime (extends date) -------------------------------------
        if isinstance(value, datetime):
            return self._serialize_temporal(value)

        # -- date --------------------------------------------------------
        if isinstance(value, date):
            return self._serialize_date(value)

        # -- Decimal — financial precision for ERP systems ---------------
        if isinstance(value, Decimal):
            return self._serialize_decimal(value)

        # -- ERP schema hints: coerce float → Decimal serialization ------
        if (
            _col_type_upper in {"DECIMAL", "NUMERIC", "NUMBER", "CURRENCY"}
            and isinstance(value, (float, int))
            and not isinstance(value, bool)
        ):
            return self._serialize_decimal(Decimal(str(value)))

        # -- numpy types (int, float, bool, ndarray) ----------------------
        if isinstance(value, (np.integer, np.floating, np.bool_, np.ndarray)):
            return self._encode_numpy(value)

        # -- Python float NaN → null (after numpy checks) ----------------
        if isinstance(value, float) and np.isnan(value):
            return self._config.null_value

        # -- bytes / UUID / containers -----------------------------------
        if isinstance(value, (bytes, bytearray)):
            return base64.b64encode(value).decode("ascii")
        if isinstance(value, uuid.UUID):
            return str(value)
        if isinstance(value, (dict, list)):
            return value

        # -- Basic JSON-native types (str, int, float, bool) -------------
        return value

    # -- Serialization sub-helpers (reduce branch count in main method) --

    def _serialize_temporal(self, value: pd.Timestamp | datetime) -> Any:
        """Serialize a datetime or Timestamp value."""
        if isinstance(value, pd.Timestamp) and pd.isna(value):
            return self._config.null_value
        if self._config.datetime_format == "epoch":
            return value.timestamp()
        return value.isoformat()

    def _serialize_date(self, value: date) -> Any:
        """Serialize a date value."""
        if self._config.date_format == "epoch":
            epoch_dt = datetime.combine(value, datetime.min.time())
            return int(epoch_dt.timestamp())
        return value.isoformat()

    def _serialize_decimal(self, value: Decimal) -> str | float:
        """Serialize a Decimal value respecting config."""
        if self._config.decimal_as_string:
            return str(value)
        return float(value)

    def _encode_numpy(self, obj: Any) -> Any:
        """Encode numpy scalar and array types to JSON-native values.

        Converts numpy-backed dtypes (common in pandas DataFrames) into
        plain Python types that are JSON-serializable.

        Args:
            obj: A numpy scalar (``np.integer``, ``np.floating``,
                ``np.bool_``) or ``np.ndarray``.

        Returns:
            The equivalent Python native value (``int``, ``float``,
            ``bool``, ``list``, or ``None`` for NaN floats).
        """
        if isinstance(obj, np.integer):
            return int(obj)
        if isinstance(obj, np.floating):
            return self._config.null_value if np.isnan(obj) else float(obj)
        if isinstance(obj, np.bool_):
            return bool(obj)
        # np.ndarray → Python list
        if isinstance(obj, np.ndarray):
            return obj.tolist()
        return obj

    # ------------------------------------------------------------------
    # Utility helpers
    # ------------------------------------------------------------------

    @staticmethod
    def _get_stream_writer(output: io.IOBase) -> Callable[[str], Any]:
        """Create a write callable compatible with text and binary streams.

        Detects whether *output* is binary and returns a function that
        encodes strings to UTF-8 bytes when necessary.

        Args:
            output: The target writable stream.

        Returns:
            A callable ``(str) -> None`` that writes text to the stream.
        """
        # Binary stream detection (BytesIO, BufferedWriter, RawIOBase)
        if isinstance(output, (io.RawIOBase, io.BufferedIOBase)):

            def _write_binary(text: str) -> None:
                output.write(text.encode("utf-8"))

            return _write_binary

        # Text mode streams (StringIO, TextIOWrapper) accept str directly
        return output.write

    @staticmethod
    def _indent_json_block(json_str: str, spaces: int) -> str:
        """Indent every line of a JSON string by the given number of spaces.

        Used to visually nest record objects within an envelope when
        pretty-printing streamed JSON array output.

        Args:
            json_str: A JSON string (potentially multi-line from
                ``json.dumps`` with ``indent``).
            spaces: Number of leading spaces to prepend to each line.

        Returns:
            The indented JSON string.
        """
        prefix = " " * spaces
        lines = json_str.split("\n")
        return "\n".join((prefix + line) if line.strip() else line for line in lines)
