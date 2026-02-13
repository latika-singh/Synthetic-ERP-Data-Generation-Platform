"""Provisioning Service — database provisioning and cloud storage export.

This microservice handles the final delivery stage of the Synthetic ERP Data
Generation Platform pipeline.  It is responsible for taking generated synthetic
datasets and delivering them to their target destinations, which may be
relational databases or cloud object stores.

Supported capabilities:

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
    app.run(host="0.0.0.0", port=5005)
"""

from __future__ import annotations

from provisioning_service.app import create_app


__version__: str = "1.0.0"
"""Semantic version of the Provisioning Service package."""

__all__: list[str] = ["__version__", "create_app"]
