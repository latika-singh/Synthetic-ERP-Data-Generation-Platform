"""Cloud storage provider package for the Provisioning Service.

This package provides abstract and concrete cloud storage provider
implementations for exporting generated synthetic ERP data to cloud
object stores:

* :class:`BaseCloudProvider` — Abstract base class defining the uniform
  interface for all cloud storage operations (upload, download, list,
  delete) with built-in retry logic, tenant prefix isolation, and
  operational metrics.

Concrete implementations (S3, Azure Blob, GCS) extend
``BaseCloudProvider`` and are registered here for convenient access.
"""

from __future__ import annotations

from provisioning_service.cloud.base import BaseCloudProvider


__all__: list[str] = ["BaseCloudProvider"]
