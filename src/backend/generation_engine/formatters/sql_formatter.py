"""SQL output formatter for the Synthetic ERP Data Generation Platform.

Generates SQL INSERT and COPY statements from synthetic data with
dialect-specific syntax support for PostgreSQL, Oracle, SQL Server,
and SAP HANA.  Implements the :class:`BaseFormatter` interface for
consistent dispatch from the generation orchestrator.

Handles batch INSERT generation with configurable batch sizes,
COPY / BULK INSERT syntax for high-throughput loading, proper SQL
escaping and quoting per dialect, data type casting for dialect-specific
types, transaction wrapping with optional ``CREATE TABLE`` DDL, and
``NULL`` value handling.

Supports multi-format export as required by feature F-009.

Typical usage::

    from generation_engine.formatters.sql_formatter import (
        SQLFormatter,
        SQLFormatterConfig,
        SQLDialect,
    )

    config = SQLFormatterConfig(
        dialect=SQLDialect.POSTGRESQL,
        batch_size=1000,
        include_transaction=True,
    )
    formatter = SQLFormatter(config=config)
    sql = formatter.format(dataframe, "gl_journal_entries", col_defs)
"""

from __future__ import annotations

import io
import math
from datetime import date, datetime
from decimal import Decimal
from enum import Enum
from typing import Any, Dict, List, Optional, Tuple, Union

import pandas as pd
from pydantic import BaseModel, Field

from .base import BaseFormatter


# ---------------------------------------------------------------------------
# SQLDialect Enum
# ---------------------------------------------------------------------------


class SQLDialect(Enum):
    """Supported SQL database dialects for output formatting.

    Each member controls dialect-specific syntax differences in INSERT
    statements, COPY / BULK INSERT commands, datetime formatting, type
    mappings, transaction wrappers, and DDL generation throughout the
    :class:`SQLFormatter`.

    Attributes:
        POSTGRESQL: PostgreSQL syntax — ``COPY FROM STDIN``,
            ``TO_TIMESTAMP``, ``SERIAL`` / ``BIGSERIAL``, ``BOOLEAN``.
        ORACLE: Oracle Database syntax — ``INSERT ALL … SELECT FROM
            DUAL``, ``TO_DATE``, ``NUMBER``, ``VARCHAR2``.
        SQLSERVER: Microsoft SQL Server syntax — ``BULK INSERT``,
            ``CONVERT``, ``NVARCHAR``, ``DATETIME2``.
        SAP_HANA: SAP HANA syntax — ``UPSERT``, ``TO_TIMESTAMP``,
            ``NVARCHAR``, ``DECIMAL``.
    """

    POSTGRESQL = "postgresql"
    ORACLE = "oracle"
    SQLSERVER = "sqlserver"
    SAP_HANA = "sap_hana"


# ---------------------------------------------------------------------------
# SQLFormatterConfig — Pydantic 2.x validated configuration
# ---------------------------------------------------------------------------


class SQLFormatterConfig(BaseModel):
    """Validated configuration for the SQL formatter.

    Uses Pydantic 2.x :class:`BaseModel` for type validation, default
    values, and serialisable configuration as required by project
    convention R-004.

    Attributes:
        dialect: Target SQL database dialect.  Controls syntax
            differences across all generated SQL output.
        batch_size: Maximum number of rows per ``INSERT`` statement.
            Larger batch sizes improve load performance but increase
            per-statement memory requirements.  Default: ``1000``.
        include_ddl: Whether to prepend ``CREATE TABLE`` DDL before
            the INSERT / COPY statements.  Default: ``False``.
        include_transaction: Whether to wrap output in ``BEGIN`` /
            ``COMMIT`` transaction statements.  Default: ``True``.
        use_copy: Whether to generate ``COPY`` (PostgreSQL) or
            ``BULK INSERT`` (SQL Server) statements instead of
            ``INSERT`` for higher throughput.  Default: ``False``.
        schema_name: Optional database schema prefix applied to the
            table name (e.g. ``"public"`` → ``public.table_name``).
        null_representation: String literal used for ``NULL`` values
            in the output.  Default: ``'NULL'``.
        string_escape_mode: Escaping strategy for string values.
            ``'standard'`` uses SQL-standard single-quote doubling;
            ``'backslash'`` also escapes backslashes.
            Default: ``'standard'``.
        encoding: Character encoding for the output.
            Default: ``'utf-8'``.
    """

    dialect: SQLDialect = Field(
        default=SQLDialect.POSTGRESQL,
        description="Target SQL database dialect",
    )
    batch_size: int = Field(
        default=1000,
        ge=1,
        le=100_000,
        description="Maximum number of rows per INSERT statement",
    )
    include_ddl: bool = Field(
        default=False,
        description="Whether to prepend CREATE TABLE DDL",
    )
    include_transaction: bool = Field(
        default=True,
        description="Whether to wrap output in BEGIN/COMMIT",
    )
    use_copy: bool = Field(
        default=False,
        description="Use COPY/BULK INSERT for high throughput",
    )
    schema_name: Optional[str] = Field(
        default=None,
        description="Database schema prefix for table names",
    )
    null_representation: str = Field(
        default="NULL",
        description="String literal for NULL values",
    )
    string_escape_mode: str = Field(
        default="standard",
        description="SQL escaping strategy for string values",
    )
    encoding: str = Field(
        default="utf-8",
        description="Character encoding for the output",
    )

    model_config = {"use_enum_values": False}


# ---------------------------------------------------------------------------
# SQLFormatter — concrete BaseFormatter implementation
# ---------------------------------------------------------------------------


class SQLFormatter(BaseFormatter):
    """SQL output formatter producing dialect-specific INSERT / COPY statements.

    Extends :class:`BaseFormatter` to convert :class:`pandas.DataFrame`
    instances containing generated synthetic ERP data into SQL output
    compatible with PostgreSQL, Oracle, SQL Server, or SAP HANA.

    The formatter supports two output modes:

    * **INSERT mode** (default): Generates batched
      ``INSERT INTO … VALUES`` statements, splitting data into chunks of
      ``batch_size`` rows for memory-efficient loading.
    * **COPY mode**: Generates ``COPY FROM STDIN`` (PostgreSQL) or
      ``BULK INSERT`` (SQL Server) statements for maximum throughput.

    Optionally prepends ``CREATE TABLE`` DDL and wraps the output in
    transaction statements.

    Args:
        config: Configuration controlling dialect, batch size, DDL
            inclusion, transaction wrapping, and other formatting
            options.  Accepts a :class:`SQLFormatterConfig` instance
            or a plain ``dict`` that will be coerced.

    Example::

        formatter = SQLFormatter(config=SQLFormatterConfig(
            dialect=SQLDialect.ORACLE,
            batch_size=500,
            include_ddl=True,
        ))
        sql = formatter.format(df, "hr_employees", column_defs)
    """

    def __init__(
        self,
        config: Union[SQLFormatterConfig, Dict[str, Any], None] = None,
    ) -> None:
        """Initialise the SQL formatter with the given configuration.

        Args:
            config: Formatter configuration.  Can be:

                - A :class:`SQLFormatterConfig` instance.
                - A plain ``dict`` (coerced to ``SQLFormatterConfig``).
                - ``None`` (uses defaults: PostgreSQL, 1 000-row batches).
        """
        if config is None:
            self._config = SQLFormatterConfig()
        elif isinstance(config, dict):
            # Coerce bare dialect string to the SQLDialect enum when the
            # config arrives from the FormatterRegistry as a plain dict.
            raw = dict(config)
            if "dialect" in raw and isinstance(raw["dialect"], str):
                raw["dialect"] = SQLDialect(raw["dialect"])
            self._config = SQLFormatterConfig(**raw)
        elif isinstance(config, SQLFormatterConfig):
            self._config = config
        else:
            self._config = SQLFormatterConfig()

    # ------------------------------------------------------------------
    # BaseFormatter abstract method implementations
    # ------------------------------------------------------------------

    def format(
        self,
        data: pd.DataFrame,
        table_name: str,
        column_definitions: dict | None = None,
    ) -> str:
        """Format the entire *data* DataFrame as a SQL string.

        Materialises the complete SQL output in memory.  For datasets
        exceeding available RAM, prefer :meth:`format_to_stream`.

        Args:
            data: Pandas DataFrame of generated synthetic ERP data.
            table_name: Target database table name (e.g.
                ``"gl_journal_entries"``).
            column_definitions: Optional column-name → type metadata
                mapping used for DDL generation and type-aware value
                escaping.

        Returns:
            Complete SQL output as a string including optional DDL,
            transaction wrapper, and INSERT / COPY statements.

        Raises:
            ValueError: If *data* is empty.
        """
        if data.empty:
            raise ValueError(
                f"Cannot format empty DataFrame for table '{table_name}'"
            )

        buffer = io.StringIO()
        self.format_to_stream(
            data,
            table_name,
            column_definitions or {},
            buffer,
        )
        return buffer.getvalue()

    def format_to_stream(
        self,
        data: pd.DataFrame,
        table_name: str,
        column_definitions: dict,
        output: io.IOBase,
    ) -> None:
        """Stream SQL output directly to *output* for large datasets.

        Writes SQL statements incrementally to bound memory usage when
        handling multi-million-row synthetic datasets.

        Args:
            data: Pandas DataFrame of generated synthetic ERP data.
            table_name: Target database table name.
            column_definitions: Column-name → type metadata mapping.
            output: Writable stream (file handle, :class:`io.StringIO`,
                :class:`io.BytesIO`, or a network socket wrapper).

        Raises:
            IOError: If writing to *output* fails.
            ValueError: If *data* is empty.
        """
        if data.empty:
            raise ValueError(
                f"Cannot format empty DataFrame for table '{table_name}'"
            )

        write = self._get_write_fn(output)
        qualified_name = self._get_qualified_table_name(table_name)

        # -- Optional CREATE TABLE DDL ------------------------------------
        if self._config.include_ddl and column_definitions:
            ddl = self._generate_ddl(table_name, column_definitions)
            write(ddl)
            write("\n\n")

        # -- Transaction BEGIN --------------------------------------------
        if self._config.include_transaction:
            begin_stmt, _ = self._get_transaction_wrapper()
            write(begin_stmt)
            write("\n\n")

        # -- Data statements (INSERT or COPY) -----------------------------
        if self._config.use_copy:
            copy_output = self._generate_copy_statement(data, qualified_name)
            write(copy_output)
        else:
            insert_output = self._generate_insert_statements(
                data, qualified_name,
            )
            write(insert_output)

        # -- Transaction COMMIT -------------------------------------------
        if self._config.include_transaction:
            _, commit_stmt = self._get_transaction_wrapper()
            write("\n")
            write(commit_stmt)
            write("\n")

    def get_file_extension(self) -> str:
        """Return the canonical file extension for SQL output.

        Returns:
            ``".sql"``
        """
        return ".sql"

    def get_content_type(self) -> str:
        """Return the MIME content type for SQL output.

        Returns:
            ``"application/sql"``
        """
        return "application/sql"

    # ------------------------------------------------------------------
    # INSERT statement generation
    # ------------------------------------------------------------------

    def _generate_insert_statements(
        self,
        data: pd.DataFrame,
        table_name: str,
    ) -> str:
        """Generate batched ``INSERT INTO … VALUES`` statements.

        Splits the DataFrame into chunks of ``batch_size`` rows and
        generates one INSERT statement per batch.  For Oracle the
        ``INSERT ALL … SELECT 1 FROM DUAL`` multi-row syntax is used
        instead.

        Args:
            data: Source DataFrame.
            table_name: Qualified table name.

        Returns:
            Concatenated INSERT statements as a single string.
        """
        parts: List[str] = []
        columns = list(data.columns)
        col_list = ", ".join(self._quote_identifier(c) for c in columns)
        batches = self._batch_rows(data)

        for batch_df in batches:
            if self._config.dialect == SQLDialect.ORACLE:
                parts.append(
                    self._generate_oracle_insert_all(
                        batch_df, table_name, columns, col_list,
                    )
                )
            else:
                parts.append(
                    self._generate_standard_insert(
                        batch_df, table_name, columns, col_list,
                    )
                )

        return "\n\n".join(parts)

    def _generate_standard_insert(
        self,
        batch_df: pd.DataFrame,
        table_name: str,
        columns: List[str],
        col_list: str,
    ) -> str:
        """Generate a standard multi-row INSERT statement.

        Produces ``INSERT INTO table (cols) VALUES (…), (…), …;``
        syntax compatible with PostgreSQL, SQL Server, and SAP HANA.

        Args:
            batch_df: DataFrame batch to convert.
            table_name: Qualified table name.
            columns: Ordered column names.
            col_list: Pre-formatted quoted column list string.

        Returns:
            Single INSERT statement string.
        """
        value_rows: List[str] = []

        for row in batch_df.itertuples(index=False, name=None):
            values: List[str] = []
            for idx, val in enumerate(row):
                col_name = columns[idx]
                values.append(self._escape_value(val, col_name))
            value_rows.append(f"({', '.join(values)})")

        return (
            f"INSERT INTO {table_name} ({col_list})\nVALUES\n"
            + ",\n".join(f"    {vr}" for vr in value_rows)
            + ";"
        )

    def _generate_oracle_insert_all(
        self,
        batch_df: pd.DataFrame,
        table_name: str,
        columns: List[str],
        col_list: str,
    ) -> str:
        """Generate Oracle ``INSERT ALL … SELECT 1 FROM DUAL`` statement.

        Oracle does not support standard multi-row ``INSERT`` syntax.
        Instead uses::

            INSERT ALL
                INTO tbl (cols) VALUES (…)
                INTO tbl (cols) VALUES (…)
            SELECT 1 FROM DUAL;

        Args:
            batch_df: DataFrame batch to convert.
            table_name: Qualified table name.
            columns: Ordered column names.
            col_list: Pre-formatted quoted column list string.

        Returns:
            Oracle-compatible INSERT ALL statement string.
        """
        lines: List[str] = ["INSERT ALL"]

        for row in batch_df.itertuples(index=False, name=None):
            values: List[str] = []
            for idx, val in enumerate(row):
                col_name = columns[idx]
                values.append(self._escape_value(val, col_name))
            lines.append(
                f"    INTO {table_name} ({col_list}) "
                f"VALUES ({', '.join(values)})"
            )

        lines.append("SELECT 1 FROM DUAL;")
        return "\n".join(lines)

    # ------------------------------------------------------------------
    # COPY / BULK INSERT generation
    # ------------------------------------------------------------------

    def _generate_copy_statement(
        self,
        data: pd.DataFrame,
        table_name: str,
    ) -> str:
        """Generate COPY or BULK INSERT statements for high throughput.

        Produces dialect-specific bulk loading syntax:

        - **PostgreSQL**: ``COPY … FROM STDIN`` with tab-delimited data.
        - **SQL Server**: ``BULK INSERT`` preamble with INSERT fallback.
        - **Oracle / SAP HANA**: Falls back to INSERT statements with a
          comment explaining that COPY is not natively supported.

        Args:
            data: Source DataFrame.
            table_name: Qualified table name.

        Returns:
            COPY / BULK INSERT statement(s) as a string.
        """
        dialect = self._config.dialect
        columns = list(data.columns)
        col_list = ", ".join(self._quote_identifier(c) for c in columns)

        if dialect == SQLDialect.POSTGRESQL:
            return self._generate_postgresql_copy(
                data, table_name, columns, col_list,
            )

        if dialect == SQLDialect.SQLSERVER:
            return self._generate_sqlserver_bulk_insert(
                data, table_name, columns, col_list,
            )

        # Oracle and SAP HANA lack native COPY; fall back to INSERT
        comment = (
            f"-- COPY/BULK INSERT not natively supported for "
            f"{dialect.value}; using INSERT statements instead.\n\n"
        )
        return comment + self._generate_insert_statements(data, table_name)

    def _generate_postgresql_copy(
        self,
        data: pd.DataFrame,
        table_name: str,
        columns: List[str],
        col_list: str,
    ) -> str:
        """Generate PostgreSQL ``COPY … FROM STDIN`` with tab-delimited data.

        Args:
            data: Source DataFrame.
            table_name: Qualified table name.
            columns: Ordered column names.
            col_list: Quoted column list string.

        Returns:
            PostgreSQL COPY statement with inline data terminated by
            the ``\\.`` end-of-data marker.
        """
        lines: List[str] = [
            f"COPY {table_name} ({col_list}) FROM STDIN;",
        ]

        for row in data.itertuples(index=False, name=None):
            row_values: List[str] = []
            for val in row:
                if val is None or pd.isna(val):
                    row_values.append("\\N")
                elif isinstance(val, bool):
                    row_values.append("t" if val else "f")
                elif isinstance(val, str):
                    # Escape tabs, newlines, and backslashes in COPY
                    escaped = (
                        val.replace("\\", "\\\\")
                        .replace("\t", "\\t")
                        .replace("\n", "\\n")
                        .replace("\r", "\\r")
                    )
                    row_values.append(escaped)
                elif isinstance(val, (datetime, date)):
                    row_values.append(val.isoformat())
                elif isinstance(val, Decimal):
                    row_values.append(str(val))
                elif isinstance(val, float):
                    if math.isnan(val):
                        row_values.append("\\N")
                    else:
                        row_values.append(str(val))
                else:
                    row_values.append(str(val))
            lines.append("\t".join(row_values))

        lines.append("\\.")
        return "\n".join(lines)

    def _generate_sqlserver_bulk_insert(
        self,
        data: pd.DataFrame,
        table_name: str,
        columns: List[str],
        col_list: str,
    ) -> str:
        """Generate SQL Server BULK INSERT preamble with INSERT fallback.

        True ``BULK INSERT`` requires a server-accessible file path, so
        this method emits a ``SET IDENTITY_INSERT ON`` wrapper around
        standard INSERT statements together with a commented-out
        ``BULK INSERT`` template for file-based bulk loading.

        Args:
            data: Source DataFrame.
            table_name: Qualified table name.
            columns: Ordered column names.
            col_list: Quoted column list string.

        Returns:
            SQL Server-compatible INSERT statements with identity
            insert handling and BULK INSERT recommendation comment.
        """
        parts: List[str] = [
            f"-- For file-based bulk loading, use:",
            f"-- BULK INSERT {table_name}",
            f"-- FROM '<data_file_path>'",
            f"-- WITH (FIELDTERMINATOR = '\\t', ROWTERMINATOR = '\\n', "
            f"FIRSTROW = 2);",
            "",
            f"SET IDENTITY_INSERT {table_name} ON;",
            "",
        ]

        insert_stmts = self._generate_insert_statements(data, table_name)
        parts.append(insert_stmts)
        parts.append(f"\nSET IDENTITY_INSERT {table_name} OFF;")

        return "\n".join(parts)

    # ------------------------------------------------------------------
    # DDL generation
    # ------------------------------------------------------------------

    def _generate_ddl(
        self,
        table_name: str,
        column_definitions: Dict[str, Any],
    ) -> str:
        """Generate ``CREATE TABLE`` DDL with dialect-specific data types.

        Translates generic column type metadata into the appropriate
        dialect-specific SQL types using :meth:`_get_type_mapping`.

        Args:
            table_name: Unqualified or qualified table name.
            column_definitions: Column-name → type metadata mapping
                (e.g. ``{"amount": {"type": "DECIMAL", "precision": 15,
                "scale": 2}}``).

        Returns:
            Complete ``CREATE TABLE`` DDL statement as a string,
            preceded by a dialect-appropriate ``DROP TABLE`` guard.
        """
        qualified_name = self._get_qualified_table_name(table_name)
        type_mapping = self._get_type_mapping()

        col_defs: List[str] = []
        for col_name, meta in column_definitions.items():
            # Normalise metadata into a uniform shape
            if isinstance(meta, dict):
                col_type = meta.get("type", "VARCHAR").upper()
                nullable = meta.get("nullable", True)
                precision = meta.get("precision")
                scale = meta.get("scale")
                length = meta.get("length")
                primary_key = meta.get("primary_key", False)
            elif isinstance(meta, str):
                col_type = meta.upper()
                nullable = True
                precision = None
                scale = None
                length = None
                primary_key = False
            else:
                col_type = "VARCHAR"
                nullable = True
                precision = None
                scale = None
                length = None
                primary_key = False

            # Map generic type to dialect-specific SQL type
            sql_type = type_mapping.get(col_type, col_type)

            # Append precision / scale / length modifiers
            if precision is not None and scale is not None:
                sql_type = f"{sql_type}({precision}, {scale})"
            elif precision is not None:
                sql_type = f"{sql_type}({precision})"
            elif length is not None:
                sql_type = f"{sql_type}({length})"

            quoted_col = self._quote_identifier(col_name)
            tokens: List[str] = [quoted_col, sql_type]

            if primary_key:
                tokens.append("PRIMARY KEY")
            elif not nullable:
                tokens.append("NOT NULL")

            col_defs.append("    " + " ".join(tokens))

        # Dialect-specific DROP TABLE guard
        drop_prefix = self._generate_drop_table_guard(qualified_name)
        columns_sql = ",\n".join(col_defs)

        return (
            f"{drop_prefix}"
            f"CREATE TABLE {qualified_name} (\n"
            f"{columns_sql}\n"
            f");"
        )

    def _generate_drop_table_guard(self, qualified_name: str) -> str:
        """Generate a dialect-specific ``DROP TABLE IF EXISTS`` guard.

        Args:
            qualified_name: Fully qualified table name.

        Returns:
            SQL string to conditionally drop the table, followed by
            two newlines.  Returns an empty string if no guard is needed.
        """
        dialect = self._config.dialect

        if dialect == SQLDialect.POSTGRESQL:
            return f"DROP TABLE IF EXISTS {qualified_name};\n\n"

        if dialect == SQLDialect.ORACLE:
            return (
                f"BEGIN\n"
                f"    EXECUTE IMMEDIATE 'DROP TABLE {qualified_name}';\n"
                f"EXCEPTION\n"
                f"    WHEN OTHERS THEN\n"
                f"        IF SQLCODE != -942 THEN RAISE; END IF;\n"
                f"END;\n/\n\n"
            )

        if dialect == SQLDialect.SQLSERVER:
            return (
                f"IF OBJECT_ID('{qualified_name}', 'U') IS NOT NULL\n"
                f"    DROP TABLE {qualified_name};\nGO\n\n"
            )

        if dialect == SQLDialect.SAP_HANA:
            return (
                f"DO BEGIN\n"
                f"    DECLARE EXIT HANDLER FOR SQL_ERROR_CODE 259 BEGIN END;\n"
                f"    EXEC 'DROP TABLE {qualified_name}';\n"
                f"END;\n\n"
            )

        return ""

    # ------------------------------------------------------------------
    # Value escaping and formatting
    # ------------------------------------------------------------------

    def _escape_value(self, value: Any, column_type: str) -> str:
        """Escape and format a single cell value for SQL output.

        Dispatches to type-specific formatting methods based on the
        Python runtime type of *value*.  Handles ``None``, ``NaN``,
        ``NaT``, strings, datetimes, decimals, booleans, integers,
        floats, and bytes.

        Args:
            value: The cell value from the DataFrame row.
            column_type: Column name (used for logging context; type
                inference is based on the Python value type).

        Returns:
            SQL-safe string representation of the value.
        """
        # ---- None and missing-value detection ---------------------------
        if value is None:
            return self._format_null()

        # float NaN check — must come before pd.isna because bool(NaN) is
        # truthy in some edge cases with numpy scalars
        if isinstance(value, float) and math.isnan(value):
            return self._format_null()

        # pandas NaT and generic NA sentinel
        try:
            if pd.isna(value):
                return self._format_null()
        except (TypeError, ValueError):
            # pd.isna raises for some exotic types; safe to ignore
            pass

        # ---- Boolean (checked before int — bool is a subclass of int) ---
        if isinstance(value, bool):
            return self._format_boolean(value)

        # ---- Numeric types ----------------------------------------------
        if isinstance(value, Decimal):
            return self._format_decimal(value)

        if isinstance(value, int):
            return str(value)

        if isinstance(value, float):
            return str(value)

        # ---- Date/time (datetime before date since datetime ⊂ date) -----
        if isinstance(value, datetime):
            return self._format_datetime(value)

        if isinstance(value, date):
            return self._format_date(value)

        # ---- Binary data ------------------------------------------------
        if isinstance(value, bytes):
            return self._format_bytes(value)

        # ---- String and fallback ----------------------------------------
        if isinstance(value, str):
            return self._format_string(value)

        # Generic fallback — stringify and quote
        return self._format_string(str(value))

    def _format_string(self, value: str) -> str:
        """Escape and quote a string value for SQL.

        Uses SQL-standard single-quote doubling (``'`` → ``''``) by
        default.  When ``string_escape_mode`` is ``'backslash'``,
        backslashes are also escaped.

        Args:
            value: Raw string value.

        Returns:
            Quoted and escaped string literal (e.g. ``'O''Brien'``).
        """
        if self._config.string_escape_mode == "standard":
            escaped = value.replace("'", "''")
        else:
            escaped = value.replace("\\", "\\\\").replace("'", "''")

        return f"'{escaped}'"

    def _format_datetime(self, value: datetime) -> str:
        """Format a *datetime* value per the configured SQL dialect.

        Args:
            value: Python :class:`datetime.datetime` instance.

        Returns:
            Dialect-specific datetime literal:

            - **PostgreSQL**: ``TO_TIMESTAMP('…', 'YYYY-MM-DD HH24:MI:SS')``
            - **Oracle**: ``TO_DATE('…', 'YYYY-MM-DD HH24:MI:SS')``
            - **SQL Server**: ``CONVERT(DATETIME2, '…', 120)``
            - **SAP HANA**: ``TO_TIMESTAMP('…', 'YYYY-MM-DD HH24:MI:SS')``
        """
        iso_str = value.strftime("%Y-%m-%d %H:%M:%S")
        dialect = self._config.dialect

        if dialect == SQLDialect.POSTGRESQL:
            return f"TO_TIMESTAMP('{iso_str}', 'YYYY-MM-DD HH24:MI:SS')"

        if dialect == SQLDialect.ORACLE:
            return f"TO_DATE('{iso_str}', 'YYYY-MM-DD HH24:MI:SS')"

        if dialect == SQLDialect.SQLSERVER:
            return f"CONVERT(DATETIME2, '{iso_str}', 120)"

        if dialect == SQLDialect.SAP_HANA:
            return f"TO_TIMESTAMP('{iso_str}', 'YYYY-MM-DD HH24:MI:SS')"

        # Fallback: ISO-8601 string literal
        return f"'{value.isoformat()}'"

    def _format_date(self, value: date) -> str:
        """Format a *date* value per the configured SQL dialect.

        Args:
            value: Python :class:`datetime.date` instance.

        Returns:
            Dialect-specific date literal.
        """
        date_str = value.strftime("%Y-%m-%d")
        dialect = self._config.dialect

        if dialect == SQLDialect.POSTGRESQL:
            return f"DATE '{date_str}'"

        if dialect == SQLDialect.ORACLE:
            return f"TO_DATE('{date_str}', 'YYYY-MM-DD')"

        if dialect == SQLDialect.SQLSERVER:
            return f"CONVERT(DATE, '{date_str}', 120)"

        if dialect == SQLDialect.SAP_HANA:
            return f"DATE '{date_str}'"

        return f"'{date_str}'"

    def _format_decimal(self, value: Decimal) -> str:
        """Format a :class:`Decimal` value as a precise numeric literal.

        Preserves exact decimal representation without floating-point
        rounding errors — critical for financial precision in ERP data
        such as monetary amounts, tax rates, and exchange rates.

        Args:
            value: Python :class:`decimal.Decimal` instance.

        Returns:
            String representation of the decimal (e.g. ``"1234.56"``).
        """
        return str(value)

    def _format_null(self) -> str:
        """Return the configured NULL literal.

        Returns:
            The ``null_representation`` from the formatter
            configuration, defaulting to ``'NULL'``.
        """
        return self._config.null_representation

    def _format_boolean(self, value: bool) -> str:
        """Format a boolean value per the configured SQL dialect.

        Args:
            value: Python ``bool``.

        Returns:
            Dialect-specific boolean literal:

            - **PostgreSQL / SAP HANA**: ``TRUE`` / ``FALSE``
            - **Oracle**: ``1`` / ``0`` (no native ``BOOLEAN`` pre-23ai)
            - **SQL Server**: ``1`` / ``0`` (``BIT`` type)
        """
        dialect = self._config.dialect

        if dialect in (SQLDialect.ORACLE, SQLDialect.SQLSERVER):
            return "1" if value else "0"

        return "TRUE" if value else "FALSE"

    def _format_bytes(self, value: bytes) -> str:
        """Format a *bytes* value as a dialect-appropriate hex literal.

        Args:
            value: Raw bytes.

        Returns:
            Hex literal string.
        """
        hex_str = value.hex()
        dialect = self._config.dialect

        if dialect == SQLDialect.POSTGRESQL:
            return f"'\\x{hex_str}'"

        if dialect == SQLDialect.ORACLE:
            return f"HEXTORAW('{hex_str}')"

        if dialect == SQLDialect.SQLSERVER:
            return f"0x{hex_str}"

        # SAP HANA and fallback
        return f"X'{hex_str}'"

    # ------------------------------------------------------------------
    # Identifier quoting
    # ------------------------------------------------------------------

    def _quote_identifier(self, identifier: str) -> str:
        """Quote a SQL identifier (table or column name) per dialect.

        Args:
            identifier: Raw identifier string.

        Returns:
            Properly quoted identifier:

            - **SQL Server**: bracket-quoted (e.g. ``[column_name]``).
            - **Others**: double-quoted (e.g. ``"column_name"``).
        """
        if self._config.dialect == SQLDialect.SQLSERVER:
            escaped = identifier.replace("]", "]]")
            return f"[{escaped}]"

        # Standard SQL double-quoting for PostgreSQL, Oracle, SAP HANA
        escaped = identifier.replace('"', '""')
        return f'"{escaped}"'

    # ------------------------------------------------------------------
    # Table name qualification
    # ------------------------------------------------------------------

    def _get_qualified_table_name(self, table_name: str) -> str:
        """Apply the configured schema prefix to the table name.

        Args:
            table_name: Bare table name.

        Returns:
            Qualified table name with schema prefix if configured
            (e.g. ``"public"."gl_journal_entries"``), otherwise
            the quoted bare table name.
        """
        quoted_table = self._quote_identifier(table_name)

        if self._config.schema_name:
            quoted_schema = self._quote_identifier(self._config.schema_name)
            return f"{quoted_schema}.{quoted_table}"

        return quoted_table

    # ------------------------------------------------------------------
    # Type mapping
    # ------------------------------------------------------------------

    def _get_type_mapping(self) -> Dict[str, str]:
        """Return dialect-specific SQL type mappings.

        Maps generic column type names (upper-case) to their
        dialect-specific equivalents.  Used by :meth:`_generate_ddl`
        to produce correct ``CREATE TABLE`` statements.

        Returns:
            Dictionary mapping generic type names to dialect-specific
            SQL type strings.
        """
        dialect = self._config.dialect

        if dialect == SQLDialect.POSTGRESQL:
            return {
                "VARCHAR": "VARCHAR",
                "NVARCHAR": "VARCHAR",
                "INTEGER": "INTEGER",
                "INT": "INTEGER",
                "BIGINT": "BIGINT",
                "SMALLINT": "SMALLINT",
                "DECIMAL": "DECIMAL",
                "NUMERIC": "NUMERIC",
                "FLOAT": "DOUBLE PRECISION",
                "DOUBLE": "DOUBLE PRECISION",
                "BOOLEAN": "BOOLEAN",
                "BOOL": "BOOLEAN",
                "TIMESTAMP": "TIMESTAMP",
                "DATETIME": "TIMESTAMP",
                "DATETIME2": "TIMESTAMP",
                "DATE": "DATE",
                "TIME": "TIME",
                "TEXT": "TEXT",
                "CLOB": "TEXT",
                "NCLOB": "TEXT",
                "BLOB": "BYTEA",
                "BINARY": "BYTEA",
                "VARBINARY": "BYTEA",
                "BYTEA": "BYTEA",
                "UUID": "UUID",
                "SERIAL": "SERIAL",
                "BIGSERIAL": "BIGSERIAL",
                "JSON": "JSONB",
                "JSONB": "JSONB",
            }

        if dialect == SQLDialect.ORACLE:
            return {
                "VARCHAR": "VARCHAR2",
                "NVARCHAR": "NVARCHAR2",
                "INTEGER": "NUMBER(10)",
                "INT": "NUMBER(10)",
                "BIGINT": "NUMBER(19)",
                "SMALLINT": "NUMBER(5)",
                "DECIMAL": "NUMBER",
                "NUMERIC": "NUMBER",
                "FLOAT": "BINARY_DOUBLE",
                "DOUBLE": "BINARY_DOUBLE",
                "BOOLEAN": "NUMBER(1)",
                "BOOL": "NUMBER(1)",
                "TIMESTAMP": "TIMESTAMP",
                "DATETIME": "TIMESTAMP",
                "DATETIME2": "TIMESTAMP",
                "DATE": "DATE",
                "TIME": "TIMESTAMP",
                "TEXT": "CLOB",
                "CLOB": "CLOB",
                "NCLOB": "NCLOB",
                "BLOB": "BLOB",
                "BINARY": "RAW",
                "VARBINARY": "RAW",
                "BYTEA": "BLOB",
                "UUID": "RAW(16)",
                "SERIAL": "NUMBER(10)",
                "BIGSERIAL": "NUMBER(19)",
                "JSON": "CLOB",
                "JSONB": "CLOB",
            }

        if dialect == SQLDialect.SQLSERVER:
            return {
                "VARCHAR": "NVARCHAR",
                "NVARCHAR": "NVARCHAR",
                "INTEGER": "INT",
                "INT": "INT",
                "BIGINT": "BIGINT",
                "SMALLINT": "SMALLINT",
                "DECIMAL": "DECIMAL",
                "NUMERIC": "NUMERIC",
                "FLOAT": "FLOAT",
                "DOUBLE": "FLOAT",
                "BOOLEAN": "BIT",
                "BOOL": "BIT",
                "TIMESTAMP": "DATETIME2",
                "DATETIME": "DATETIME2",
                "DATETIME2": "DATETIME2",
                "DATE": "DATE",
                "TIME": "TIME",
                "TEXT": "NVARCHAR(MAX)",
                "CLOB": "NVARCHAR(MAX)",
                "NCLOB": "NVARCHAR(MAX)",
                "BLOB": "VARBINARY(MAX)",
                "BINARY": "VARBINARY",
                "VARBINARY": "VARBINARY",
                "BYTEA": "VARBINARY(MAX)",
                "UUID": "UNIQUEIDENTIFIER",
                "SERIAL": "INT IDENTITY(1,1)",
                "BIGSERIAL": "BIGINT IDENTITY(1,1)",
                "JSON": "NVARCHAR(MAX)",
                "JSONB": "NVARCHAR(MAX)",
            }

        if dialect == SQLDialect.SAP_HANA:
            return {
                "VARCHAR": "NVARCHAR",
                "NVARCHAR": "NVARCHAR",
                "INTEGER": "INTEGER",
                "INT": "INTEGER",
                "BIGINT": "BIGINT",
                "SMALLINT": "SMALLINT",
                "DECIMAL": "DECIMAL",
                "NUMERIC": "DECIMAL",
                "FLOAT": "DOUBLE",
                "DOUBLE": "DOUBLE",
                "BOOLEAN": "BOOLEAN",
                "BOOL": "BOOLEAN",
                "TIMESTAMP": "TIMESTAMP",
                "DATETIME": "TIMESTAMP",
                "DATETIME2": "TIMESTAMP",
                "DATE": "DATE",
                "TIME": "TIME",
                "TEXT": "NCLOB",
                "CLOB": "NCLOB",
                "NCLOB": "NCLOB",
                "BLOB": "BLOB",
                "BINARY": "VARBINARY",
                "VARBINARY": "VARBINARY",
                "BYTEA": "VARBINARY",
                "UUID": "NVARCHAR(36)",
                "SERIAL": "INTEGER",
                "BIGSERIAL": "BIGINT",
                "JSON": "NCLOB",
                "JSONB": "NCLOB",
            }

        # Fallback: identity mapping (pass-through)
        return {}

    # ------------------------------------------------------------------
    # Transaction wrappers
    # ------------------------------------------------------------------

    def _get_transaction_wrapper(self) -> Tuple[str, str]:
        """Return dialect-specific ``BEGIN`` / ``COMMIT`` statements.

        Returns:
            A ``(begin_statement, commit_statement)`` tuple.
        """
        dialect = self._config.dialect

        if dialect == SQLDialect.POSTGRESQL:
            return ("BEGIN;", "COMMIT;")

        if dialect == SQLDialect.ORACLE:
            # Oracle auto-begins transactions; just need COMMIT
            return ("-- Transaction start (implicit in Oracle)", "COMMIT;")

        if dialect == SQLDialect.SQLSERVER:
            return ("BEGIN TRANSACTION;", "COMMIT TRANSACTION;")

        if dialect == SQLDialect.SAP_HANA:
            # SAP HANA auto-begins transactions; just need COMMIT
            return ("-- Transaction start (implicit in SAP HANA)", "COMMIT;")

        return ("BEGIN;", "COMMIT;")

    # ------------------------------------------------------------------
    # Batch splitting
    # ------------------------------------------------------------------

    def _batch_rows(self, data: pd.DataFrame) -> List[pd.DataFrame]:
        """Split a DataFrame into chunks of ``batch_size`` rows.

        Args:
            data: Source DataFrame.

        Returns:
            List of DataFrame slices, each containing at most
            ``batch_size`` rows.
        """
        batch_size = self._config.batch_size
        num_rows = len(data)

        if num_rows <= batch_size:
            return [data]

        batches: List[pd.DataFrame] = []
        for start in range(0, num_rows, batch_size):
            end = min(start + batch_size, num_rows)
            batches.append(data.iloc[start:end])

        return batches

    # ------------------------------------------------------------------
    # Stream write helper
    # ------------------------------------------------------------------

    @staticmethod
    def _get_write_fn(output: io.IOBase):
        """Return a write callable appropriate for the stream type.

        Transparently encodes strings to bytes when the *output* stream
        is binary (e.g. :class:`io.BytesIO`).

        Args:
            output: The target output stream.

        Returns:
            A callable ``(str) → int`` that writes a string to the
            stream.
        """
        if isinstance(output, (io.StringIO, io.TextIOBase)):
            return output.write

        def _write_encoded(text: str) -> int:
            """Encode *text* to UTF-8 and write to the binary stream."""
            return output.write(text.encode("utf-8"))

        return _write_encoded
