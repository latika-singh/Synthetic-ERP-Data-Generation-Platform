"""Abstract base class for all output formatters in the Generation Engine.

Defines the ``BaseFormatter`` ABC with abstract methods that every concrete
formatter (SQL, CSV, JSON, Parquet) must implement.  Extracted into a
separate module following the established project pattern
(``generators/base.py``, ``validators/base.py``, ``connectors/base.py``) to
prevent circular imports between ``__init__.py`` (which registers concrete
formatters) and the concrete formatter implementations (which inherit from
``BaseFormatter``).

All formatters accept pandas DataFrames containing generated synthetic ERP
data and produce output in their respective format.  The abstract interface
guarantees consistent dispatch from the generation orchestrator.

Example::

    class MyFormatter(BaseFormatter):
        def format(self, data, table_name, column_definitions=None):
            return data.to_csv()

        def format_to_stream(self, data, table_name, column_definitions, output):
            output.write(data.to_csv().encode())

        def get_file_extension(self):
            return ".csv"

        def get_content_type(self):
            return "text/csv"
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from typing import TYPE_CHECKING


if TYPE_CHECKING:
    import io
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
        formatter can implement a more efficient batch strategy.
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
        """Format the entire *data* DataFrame to output string or bytes.

        Args:
            data: Pandas DataFrame containing generated synthetic ERP data.
            table_name: Qualified name of the ERP table (e.g.
                ``"gl_journal_entries"``).
            column_definitions: Optional mapping of column names to their
                ERP type metadata.  When provided the formatter may use it
                to coerce types or select encoding strategies.

        Returns:
            The formatted output as a ``str`` (text formats such as SQL,
            CSV, JSON) or ``bytes`` (binary formats such as Parquet).
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

        Args:
            data: Pandas DataFrame containing generated synthetic ERP data.
            table_name: Qualified name of the ERP table.
            column_definitions: Column-name → ERP-type metadata mapping.
            output: Writable stream (file handle, ``BytesIO``,
                ``StringIO``, network socket wrapper, etc.).
        """
        ...

    @abstractmethod
    def get_file_extension(self) -> str:
        """Return the canonical file extension including the leading dot.

        Returns:
            A string such as ``".csv"``, ``".json"``, ``".sql"``, or
            ``".parquet"``.
        """
        ...

    @abstractmethod
    def get_content_type(self) -> str:
        """Return the MIME content type for HTTP responses.

        Returns:
            A MIME type string such as ``"text/csv"``,
            ``"application/json"``, or
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
        example a ``ParquetWriter`` that emits row-groups) should override
        this method.

        Args:
            data_batches: Iterator yielding ``pd.DataFrame`` instances,
                each representing one generation batch (typically 10 000
                rows).
            table_name: Qualified name of the ERP table.
            column_definitions: Column-name → ERP-type metadata mapping.
            output: Writable stream that receives the formatted output.

        Returns:
            The total number of rows written across all batches.
        """
        total_rows = 0
        for batch in data_batches:
            self.format_to_stream(batch, table_name, column_definitions, output)
            total_rows += len(batch)
        return total_rows
