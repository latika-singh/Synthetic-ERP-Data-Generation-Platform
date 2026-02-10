"""Shared configuration package for the Synthetic ERP Data Generation Platform.

This package provides the base configuration class and environment variable
loading infrastructure shared across all six backend microservices:

- **API Gateway** — Flask REST API gateway
- **Generation Engine** — Multi-method synthetic data generation
- **Profiling Service** — ERP schema discovery and statistical profiling
- **Quality Service** — Data quality validation
- **Compliance Service** — PII detection and regulatory compliance
- **Provisioning Service** — Database connectors and cloud storage export

All services inherit from :class:`BaseConfig` and use :func:`get_config`
to obtain an environment-appropriate configuration instance at startup.

Usage::

    from shared.config import BaseConfig, get_config

    config = get_config()  # Automatically selects Dev/Test/Prod
    app.config.from_object(config)
"""

from __future__ import annotations

from .base import (
    BaseConfig,
    ConfigurationError,
    DevelopmentConfig,
    ProductionConfig,
    TestingConfig,
    config_registry,
    get_config,
)


__all__: list[str] = [
    "BaseConfig",
    "ConfigurationError",
    "DevelopmentConfig",
    "ProductionConfig",
    "TestingConfig",
    "config_registry",
    "get_config",
]
