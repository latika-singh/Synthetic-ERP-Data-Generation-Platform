"""Abstract base class for all output formatters in the Generation Engine.

Defines the ``BaseFormatter`` ABC with abstract methods that every concrete
formatter (SQL, CSV, JSON, Parquet) must implement.  Extracted into a
separate module following the established project pattern
(``generators/base.py``, ``validators/base.py``, ``connectors/base.py``) to
prevent circular imports between ``__init__.py`` (which registers concrete
formatters) and the concrete formatter implementations (which inherit from
``BaseFormatter``).

All formatters accept :class:`pandas.DataFrame` instances containing generated
synthetic ERP data and produce output in their respective format.  The
abstract interface guarantees consistent dispatch from the generation
orchestrator and batch processor.

Typical usage::

    from generation_engine.formatters.base import BaseFormatter


    class CSVFormatter(BaseFormatter):
        def format(self, data, table_name, column_definitions=None):
            return data.to_csv(index=False)

        def format_to_stream(self, data, table_name, column_definitions, output):
            output.write(data.to_csv(index=False).encode("utf-8"))

        def get_file_extension(self):
            return ".csv"

        def get_content_type(self):
            return "text/csv"
"""

import io
from abc import ABC, abstractmethod
from collections.abc import Iterator

import pandas as pd


class BaseFormatter(ABC):
    """Abstract base class providing the formatter interface contract.

    Every output formatter in the Generation Engine extends this class to
    implement a consistent interface for the orchestrator's dispatch
    mechanism.  Concrete implementations exist for SQL, CSV, JSON/JSONL,
    and Parquet formats as required by multi-format export feature F-009.

    Subclass Contract:
        Concrete formatters **must** implement the four abstract methods:

        - :meth:`format` — Convert a DataFrame to an in-memory string/bytes.
        - :meth:`format_to_stream` — Write formatted output to a stream.
        - :meth:`get_file_extension` — Return the canonical file extension.
        - :meth:`get_content_type` — Return the MIME content type.

        The :meth:`format_batch` method provides a default batch-processing
        implementation that iterates over batches and delegates to
        :meth:`format_to_stream`.  Override it only if the concrete
        formatter can implement a more efficient batch strategy (e.g. a
        ``ParquetWriter`` that emits row-groups without closing the file
        between batches).

    Example:
        Minimal concrete formatter::

            class MyFormatter(BaseFormatter):
                def format(self, data, table_name, column_definitions=None):
                    return data.to_string()

                def format_to_stream(self, data, table_name, column_definitions, output):
                    output.write(data.to_string().encode("utf-8"))

                def get_file_extension(self):
                    return ".txt"

                def get_content_type(self):
                    return "text/plain"
    """

    # ------------------------------------------------------------------
    # Abstract methods — must be implemented by every concrete formatter
    # ------------------------------------------------------------------

    @abstractmethod
    def format(
        self,
        data: pd.DataFrame,
        table_name: str,
        column_definitions: dict | None = None,
    ) -> str | bytes:
        """Format the entire *data* DataFrame to an output string or bytes.

        This method materialises the complete formatted output in memory and
        is appropriate for datasets that comfortably fit in the available
        RAM budget.  For larger datasets prefer :meth:`format_to_stream` or
        :meth:`format_batch`.

        Args:
            data: Pandas DataFrame containing generated synthetic ERP data.
                Each row represents a single record and columns correspond
                to ERP table fields.
            table_name: Qualified name of the ERP table (e.g.
                ``"gl_journal_entries"``, ``"hr_employees"``).  Formatters
                may use this to generate DDL statements, JSON root keys, or
                file names.
            column_definitions: Optional mapping of column names to their
                ERP type metadata (e.g.
                ``{"amount": {"type": "DECIMAL", "precision": 15, "scale": 2}}``).
                When provided the formatter may use it to coerce types,
                select encoding strategies, or generate schema-aware output.
                Defaults to ``None``, in which case the formatter infers
                types from the DataFrame dtypes.

        Returns:
            The formatted output as a ``str`` (text formats such as SQL,
            CSV, JSON) or ``bytes`` (binary formats such as Parquet).

        Raises:
            ValueError: If *data* is empty and the formatter does not
                support empty output.
            TypeError: If *column_definitions* contains unsupported type
                metadata.
        """
        ...

    @abstractmethod
    def format_to_stream(
        self,
        data: pd.DataFrame,
        table_name: str,
        column_definitions: dict,
        output: io.IOBase,
    ) -> None:
        """Stream formatted output directly to *output* for large datasets.

        This method avoids materialising the entire output in memory and is
        the preferred path for datasets exceeding the available RAM budget.
        The orchestrator calls this method when writing to files, network
        sockets, or cloud storage upload streams.

        Args:
            data: Pandas DataFrame containing generated synthetic ERP data.
            table_name: Qualified name of the ERP table.
            column_definitions: Column-name → ERP-type metadata mapping.
                Unlike :meth:`format`, this parameter is required because
                streaming formatters typically need schema information to
                write headers or determine encoding up-front.
            output: Writable stream accepting the formatted data.  Must be
                compatible with :class:`io.IOBase` (e.g. a file handle
                opened in the correct mode, :class:`io.BytesIO`,
                :class:`io.StringIO`, or a network socket wrapper).

        Raises:
            IOError: If writing to *output* fails.
            ValueError: If *data* is empty and the formatter does not
                support empty output.
        """
        ...

    @abstractmethod
    def get_file_extension(self) -> str:
        """Return the canonical file extension including the leading dot.

        The orchestrator uses this value when constructing output file paths
        for export operations.

        Returns:
            A string such as ``".csv"``, ``".json"``, ``".jsonl"``,
            ``".sql"``, or ``".parquet"``.
        """
        ...

    @abstractmethod
    def get_content_type(self) -> str:
        """Return the MIME content type for HTTP responses.

        The API Gateway uses this value to set the ``Content-Type`` header
        when serving generated data via download endpoints.

        Returns:
            A MIME type string such as ``"text/csv"``,
            ``"application/json"``, ``"application/x-ndjson"``,
            ``"application/sql"``, or
            ``"application/vnd.apache.parquet"``.
        """
        ...

    # ------------------------------------------------------------------
    # Default implementation — may be overridden for better performance
    # ------------------------------------------------------------------

    def format_batch(
        self,
        data_batches: Iterator[pd.DataFrame],
        table_name: str,
        column_definitions: dict,
        output: io.IOBase,
    ) -> int:
        """Write multiple batches to *output* sequentially.

        The default implementation simply iterates over *data_batches* and
        delegates each batch to :meth:`format_to_stream`.  Subclasses that
        can achieve higher throughput by keeping state between batches (for
        example a ``ParquetWriter`` that emits row-groups without closing
        the file, or a SQL formatter that keeps a running transaction) are
        encouraged to override this method.

        The generation engine's batch processor produces batches of 10,000
        rows by default.  This method processes them one at a time, flushing
        output after each batch to bound memory usage.

        Args:
            data_batches: Iterator yielding :class:`pd.DataFrame` instances,
                each representing one generation batch (typically 10,000
                rows per the ``BATCH_SIZE`` configuration).
            table_name: Qualified name of the ERP table.
            column_definitions: Column-name → ERP-type metadata mapping
                that remains constant across all batches.
            output: Writable stream that receives the formatted output.
                The stream is **not** closed by this method — the caller
                is responsible for closing it after all batches have been
                written.

        Returns:
            The total number of rows written across all batches.  This
            value can be used for progress reporting and audit logging.

        Raises:
            IOError: If writing to *output* fails for any batch.
            StopIteration: Implicitly when *data_batches* is exhausted
                (handled by the ``for`` loop).
        """
        total_rows: int = 0
        for batch in data_batches:
            self.format_to_stream(batch, table_name, column_definitions, output)
            total_rows += len(batch)
        return total_rows
