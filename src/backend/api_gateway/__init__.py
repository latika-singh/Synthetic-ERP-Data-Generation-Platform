"""API Gateway service package for the Synthetic ERP Data Generation Platform.

This package provides the single entry point for the Web Console and external
API consumers. It exposes a Flask 3.1.x REST API with Blueprint-based modular
routing, JWT authentication via Auth0, tiered rate limiting, multi-tenant
namespace isolation, and comprehensive observability instrumentation.

The API Gateway orchestrates requests across five downstream microservices:
    - Generation Engine: Synthetic data generation (AI/ML, rules, statistical, masking)
    - Profiling Service: ERP schema discovery and statistical profiling
    - Quality Service: Data quality validation and scoring
    - Compliance Service: PII detection and regulatory compliance
    - Provisioning Service: Database connectors and cloud storage export

Typical usage::

    from api_gateway import create_app

    app = create_app('development')
    app.run()

Attributes:
    __version__: Semantic version string for the API Gateway service.
    create_app: Flask Application Factory function for creating configured
        application instances.
"""

from __future__ import annotations


__version__: str = "1.0.0"

# Lazy import pattern to avoid circular dependencies and handle cases where
# dependent modules (app.py) may not yet be available during progressive
# project generation. The create_app function is the package's primary
# public API, used by wsgi.py, test fixtures, and Docker entrypoints.
try:
    from api_gateway.app import create_app  # type: ignore[import-not-found]
except ImportError:

    def create_app(config_name: str | None = None) -> object:
        """Placeholder for Flask Application Factory.

        This stub exists because api_gateway.app has not been created yet.
        Once app.py is available, this import will resolve to the real
        Application Factory function.

        Args:
            config_name: Optional environment name ('development', 'testing',
                'production'). Ignored by the placeholder.

        Returns:
            Never returns; always raises NotImplementedError.

        Raises:
            NotImplementedError: Always raised until app.py is available.
        """
        raise NotImplementedError(
            "api_gateway.app module is not yet available. "
            "Ensure app.py has been created in the api_gateway package."
        )


__all__: list[str] = ["__version__", "create_app"]
