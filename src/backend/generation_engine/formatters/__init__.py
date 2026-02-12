"""Output formatting package for the Synthetic ERP Data Generation Platform.

Converts generated synthetic data into multiple export formats — SQL, CSV,
JSON, JSONL, and Apache Parquet — as required by multi-format export feature
F-009.  Every formatter implements the :class:`BaseFormatter` abstract base
class so that the generation orchestrator can dispatch output to the
correct formatter without knowing the concrete type at compile time.

The package exposes a :class:`FormatterRegistry` that maps
:class:`OutputFormat` enum values to their concrete formatter classes, and a
convenience factory function :func:`get_formatter` that accepts a plain
format-name string and returns a fully-configured formatter instance.

Typical usage::

    from generation_engine.formatters import get_formatter, OutputFormat

    formatter = get_formatter("parquet", {"compression": "zstd"})
    result = formatter.format(dataframe, "gl_journal_entries", col_defs)
"""

from __future__ import annotations

import io
import logging
from collections.abc import Iterator
from enum import Enum
from typing import Any

import pandas as pd

from .base import BaseFormatter


__all__ = [
    "BaseFormatter",
    "FormatterRegistry",
    "OutputFormat",
    "__version__",
    "get_formatter",
]

__version__: str = "1.0.0"

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# OutputFormat Enum
# ---------------------------------------------------------------------------


class OutputFormat(Enum):
    """Supported output formats for synthetic data export.

    Each member corresponds to a concrete :class:`BaseFormatter` subclass
    registered in the :class:`FormatterRegistry`.

    Attributes:
        SQL: SQL INSERT / COPY statement output.
        CSV: Comma-separated (or configurable-delimiter) values.
        JSON: Standard JSON array of objects.
        JSONL: JSON Lines (NDJSON) — one JSON object per line.
        PARQUET: Apache Parquet columnar binary format.
    """

    SQL = "sql"
    CSV = "csv"
    JSON = "json"
    JSONL = "jsonl"
    PARQUET = "parquet"


# ---------------------------------------------------------------------------
# FormatterRegistry
# ---------------------------------------------------------------------------


class FormatterRegistry:
    """Central registry mapping :class:`OutputFormat` values to concrete formatter classes.

    The registry is populated at module-load time via :meth:`register` calls
    at the bottom of this file.  Consumers obtain formatter instances via
    :meth:`get_formatter` which instantiates the correct class with optional
    configuration.

    Each registration may include a *default_config* dictionary that is
    automatically applied when the caller does not supply explicit config.
    This allows the same formatter class to serve multiple output formats
    with different default behaviours — e.g. :class:`JSONFormatter` is
    registered for both ``JSON`` (default: ``output_mode='json'``) and
    ``JSONL`` (default: ``output_mode='jsonl'``).

    This class is not meant to be instantiated — all methods are class-level.
    """

    _registry: dict[
        OutputFormat,
        tuple[type[BaseFormatter], dict[str, Any] | None],
    ] = {}

    @classmethod
    def register(
        cls,
        format_type: OutputFormat,
        formatter_class: type[BaseFormatter],
        default_config: dict[str, Any] | None = None,
    ) -> None:
        """Register a concrete formatter class for a given output format.

        Args:
            format_type: The :class:`OutputFormat` member to associate.
            formatter_class: The class (not an instance) that implements
                :class:`BaseFormatter`.
            default_config: Optional dictionary of default configuration
                values that will be passed to the formatter constructor
                when no explicit *config* is supplied by the caller.
                Caller-supplied config keys take precedence over defaults.

        Raises:
            TypeError: If *formatter_class* is not a subclass of
                :class:`BaseFormatter`.
        """
        if not (isinstance(formatter_class, type) and issubclass(formatter_class, BaseFormatter)):
            msg = f"{formatter_class!r} is not a BaseFormatter subclass"
            raise TypeError(msg)
        cls._registry[format_type] = (formatter_class, default_config)
        logger.debug(
            "Registered formatter %s for %s (default_config=%r)",
            formatter_class.__name__,
            format_type.value,
            default_config,
        )

    @classmethod
    def get_formatter(
        cls,
        format_type: OutputFormat,
        config: dict[str, Any] | None = None,
    ) -> BaseFormatter:
        """Instantiate and return a configured formatter for *format_type*.

        Default configuration values registered with :meth:`register` are
        merged first, then any caller-supplied *config* values override them.
        This ensures that, for example, requesting ``JSONL`` without config
        still produces a formatter in JSON-Lines mode.

        Args:
            format_type: The desired :class:`OutputFormat`.
            config: Optional dictionary of configuration values forwarded
                to the formatter's constructor.  Overrides any defaults
                registered for *format_type*.

        Returns:
            A fully-configured :class:`BaseFormatter` instance.

        Raises:
            KeyError: If *format_type* has no registered formatter.
        """
        if format_type not in cls._registry:
            available = ", ".join(f.value for f in cls._registry)
            msg = f"No formatter registered for {format_type.value!r}. Available formats: {available}"
            raise KeyError(msg)

        formatter_cls, default_config = cls._registry[format_type]

        # Merge default config with caller-supplied config.
        # Caller values take precedence over defaults.
        effective_config: dict[str, Any] = {}
        if default_config:
            effective_config.update(default_config)
        if config:
            effective_config.update(config)

        if effective_config:
            return formatter_cls(config=effective_config)  # type: ignore[call-arg]
        return formatter_cls()

    @classmethod
    def list_formats(cls) -> list[OutputFormat]:
        """Return a list of all registered output formats.

        Returns:
            Sorted list of :class:`OutputFormat` members that have a
            registered formatter class.
        """
        return sorted(cls._registry.keys(), key=lambda f: f.value)

    @classmethod
    def is_supported(cls, format_type: str) -> bool:
        """Check whether a format name is supported.

        Args:
            format_type: A plain string format name (e.g. ``"parquet"``).

        Returns:
            ``True`` if a formatter is registered for the given name.
        """
        try:
            enum_val = OutputFormat(format_type.lower())
        except ValueError:
            return False
        return enum_val in cls._registry


# ---------------------------------------------------------------------------
# Module-level convenience factory
# ---------------------------------------------------------------------------


def get_formatter(
    format_type: str,
    config: dict[str, Any] | None = None,
) -> BaseFormatter:
    """Create a configured formatter instance from a plain format name.

    This is the recommended entry point for the generation orchestrator.

    Args:
        format_type: Case-insensitive format name (e.g. ``"csv"``,
            ``"parquet"``).
        config: Optional configuration dictionary forwarded to the
            formatter's constructor.

    Returns:
        A :class:`BaseFormatter` instance ready for use.

    Raises:
        ValueError: If *format_type* does not correspond to a known
            :class:`OutputFormat` member.
        KeyError: If the format is valid but no class is registered.
    """
    try:
        enum_val = OutputFormat(format_type.lower())
    except ValueError as exc:
        supported = ", ".join(f.value for f in OutputFormat)
        msg = f"Unknown format {format_type!r}. Supported: {supported}"
        raise ValueError(msg) from exc
    return FormatterRegistry.get_formatter(enum_val, config)


# ---------------------------------------------------------------------------
# Lazy formatter registration
# ---------------------------------------------------------------------------
# Each concrete formatter is imported and registered below.  Imports are
# wrapped in try/except to tolerate partial builds where sibling modules
# may not yet exist during coordinated parallel agent generation on the
# Blitzy platform.

try:
    from .parquet_formatter import ParquetFormatter

    FormatterRegistry.register(OutputFormat.PARQUET, ParquetFormatter)
    __all__.append("ParquetFormatter")
except ImportError:  # pragma: no cover
    logger.debug("ParquetFormatter not available — skipping registration")

try:
    from .sql_formatter import SQLFormatter

    FormatterRegistry.register(OutputFormat.SQL, SQLFormatter)
    __all__.append("SQLFormatter")
except ImportError:  # pragma: no cover
    logger.debug("SQLFormatter not available — skipping registration")

try:
    from .csv_formatter import CSVFormatter

    FormatterRegistry.register(OutputFormat.CSV, CSVFormatter)
    __all__.append("CSVFormatter")
except ImportError:  # pragma: no cover
    logger.debug("CSVFormatter not available — skipping registration")

try:
    from .json_formatter import JSONFormatter

    FormatterRegistry.register(OutputFormat.JSON, JSONFormatter)
    FormatterRegistry.register(
        OutputFormat.JSONL,
        JSONFormatter,
        default_config={"output_mode": "jsonl"},
    )
    __all__.append("JSONFormatter")
except ImportError:  # pragma: no cover
    logger.debug("JSONFormatter not available — skipping registration")
