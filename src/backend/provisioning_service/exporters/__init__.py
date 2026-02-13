"""Export handler package for the Provisioning Service.

Part of the Provisioning Service's delivery layer, this package
coordinates file export (CSV, JSON, Parquet, SQL) and direct database
provisioning (JDBC) of generated synthetic data.

Supported delivery targets:

* **File-based exports** -- CSV, JSON, JSONL, Parquet, and SQL formats
  with optional gzip compression, AES-256-GCM encryption, and cloud
  storage delivery (AWS S3, Azure Blob, GCP Cloud Storage) via
  :class:`FileExporter`.
* **Database provisioning** -- PostgreSQL, Oracle, SQL Server, and SAP
  HANA via JDBC batch inserts with tenant-scoped schema isolation,
  progress tracking, and circuit-breaker resilience via
  :class:`DatabaseExporter`.
"""

from __future__ import annotations

from typing import Any, Union

from provisioning_service.exporters.database_exporter import DatabaseExporter


# FileExporter may not be available yet if its module has not been
# processed.  Import conditionally so the package remains functional.
try:
    from provisioning_service.exporters.file_exporter import FileExporter

    _FILE_EXPORTER_AVAILABLE = True
except ImportError:
    FileExporter = None  # type: ignore[assignment,misc]
    _FILE_EXPORTER_AVAILABLE = False

# ---------------------------------------------------------------------------
# Exporter registry
# ---------------------------------------------------------------------------

_DB_TYPES: dict[str, type] = {
    "database": DatabaseExporter,
    "db": DatabaseExporter,
    "jdbc": DatabaseExporter,
    "postgresql": DatabaseExporter,
    "oracle": DatabaseExporter,
    "sqlserver": DatabaseExporter,
    "hana": DatabaseExporter,
}

_FILE_TYPES: dict[str, type] = {}
if _FILE_EXPORTER_AVAILABLE and FileExporter is not None:
    _FILE_TYPES = {
        "file": FileExporter,
        "csv": FileExporter,
        "json": FileExporter,
        "jsonl": FileExporter,
        "parquet": FileExporter,
        "sql": FileExporter,
    }

EXPORTER_REGISTRY: dict[str, type] = {**_FILE_TYPES, **_DB_TYPES}


# ---------------------------------------------------------------------------
# Public factory helpers
# ---------------------------------------------------------------------------


def get_exporter(
    export_type: str,
    config: Any,
) -> DatabaseExporter | Any:
    """Instantiate the appropriate exporter for *export_type*.

    Args:
        export_type: Target export type (e.g. ``'csv'``, ``'postgresql'``).
            Normalised to lowercase with surrounding whitespace stripped.
        config: A :class:`ProvisioningServiceConfig` (or compatible) object
            passed to the exporter's constructor.

    Returns:
        An instance of :class:`FileExporter` or :class:`DatabaseExporter`.

    Raises:
        ValueError: If *export_type* is not found in the registry.

    Examples:
        >>> db_exporter = get_exporter('postgresql', config)  # DatabaseExporter
        >>> file_exporter = get_exporter('csv', config)       # FileExporter
    """
    normalised: str = export_type.strip().lower()
    exporter_cls = EXPORTER_REGISTRY.get(normalised)
    if exporter_cls is None:
        supported = ", ".join(sorted(EXPORTER_REGISTRY))
        raise ValueError(
            f"Unsupported export type '{export_type}'. "
            f"Supported types: {supported}"
        )
    return exporter_cls(config)


def get_supported_export_types() -> list[str]:
    """Return a sorted list of all supported export type keys.

    Useful for API validation and documentation.

    Returns:
        Sorted list of unique export type identifiers accepted by
        :func:`get_exporter`.
    """
    return sorted(EXPORTER_REGISTRY)


def is_file_export(export_type: str) -> bool:
    """Check whether *export_type* maps to a file-based exporter.

    Args:
        export_type: The export type key to test.

    Returns:
        ``True`` if *export_type* maps to :class:`FileExporter`.

    Raises:
        ValueError: If *export_type* is not in the registry.
    """
    normalised: str = export_type.strip().lower()
    if normalised not in EXPORTER_REGISTRY:
        raise ValueError(
            f"Unknown export type '{export_type}'. "
            f"Supported: {', '.join(sorted(EXPORTER_REGISTRY))}"
        )
    return normalised in _FILE_TYPES


def is_database_export(export_type: str) -> bool:
    """Check whether *export_type* maps to a database exporter.

    Args:
        export_type: The export type key to test.

    Returns:
        ``True`` if *export_type* maps to :class:`DatabaseExporter`.

    Raises:
        ValueError: If *export_type* is not in the registry.
    """
    normalised: str = export_type.strip().lower()
    if normalised not in EXPORTER_REGISTRY:
        raise ValueError(
            f"Unknown export type '{export_type}'. "
            f"Supported: {', '.join(sorted(EXPORTER_REGISTRY))}"
        )
    return normalised in _DB_TYPES


__all__ = [
    "EXPORTER_REGISTRY",
    "DatabaseExporter",
    "FileExporter",
    "get_exporter",
    "get_supported_export_types",
    "is_database_export",
    "is_file_export",
]
