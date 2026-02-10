"""Shared structured logging package for the Synthetic ERP Data Generation Platform.

This package provides structured JSON logging with automatic correlation ID
propagation, tenant context injection, and request context binding — shared
across all six backend microservices (API Gateway, Generation Engine, Profiling
Service, Quality Service, Compliance Service, and Provisioning Service).

Typical usage::

    from shared.logging import get_logger, configure_logging

    # At service startup (inside create_app()):
    configure_logging(service_name="api-gateway")

    # In any module:
    logger = get_logger(__name__)
    logger.info("processing_request", user_id="abc123")
"""

from shared.logging.structured_logger import (
    bind_context,
    clear_context,
    configure_logging,
    get_logger,
)


__all__: list[str] = [
    "get_logger",
    "configure_logging",
    "bind_context",
    "clear_context",
]
