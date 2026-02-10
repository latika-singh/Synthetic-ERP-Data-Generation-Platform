"""Provisioning Service — database provisioning and cloud storage export.

This microservice handles the final delivery stage of the Synthetic ERP Data
Generation Platform pipeline:

* **JDBC database connectors** — batch-insert provisioning for PostgreSQL,
  Oracle, SQL Server, and SAP HANA target databases.
* **Cloud storage export** — multi-part upload with AES-256 encryption to
  AWS S3, Azure Blob Storage, and GCP Cloud Storage.
* **Multi-format export** — generated datasets can be exported as SQL INSERT
  statements, CSV files, JSON/JSONL documents, or Apache Parquet columnar
  files.

Usage::

    from provisioning_service import create_app

    app = create_app()
    app.run()
"""

from __future__ import annotations


__version__: str = "1.0.0"
"""Semantic version of the Provisioning Service."""

# ---------------------------------------------------------------------------
# Convenience imports
# ---------------------------------------------------------------------------
# The ``create_app`` factory depends on Flask and several sub-modules that may
# not yet be present during incremental project generation.  A guarded import
# keeps the package importable in all scenarios.
# ---------------------------------------------------------------------------

__all__: list[str] = ["__version__"]

try:
    from provisioning_service.app import create_app

    __all__.append("create_app")
except ImportError:
    pass
