"""Profiling Service — ERP schema discovery and statistical profiling.

This service is responsible for extracting schema metadata (tables, columns,
relationships, data types) from source ERP systems and generating statistical
profiles that capture distribution patterns without accessing raw production data.

Supported ERP Systems:
    - SAP ERP: Schema discovery via RFC/BAPI interfaces.
    - Oracle E-Business Suite: Schema discovery via OData/JDBC connectors.
    - Microsoft Dynamics 365: Schema discovery via Web API/OData endpoints.
    - Legacy Systems: Generic JDBC connector for custom ERP databases.

Statistical Profiling Stack:
    - SciPy 1.12+: Distribution fitting and statistical analysis.
    - NumPy 1.26+: Numerical computing for aggregation and sampling.
    - Pandas 2.x: Data manipulation and metadata transformation.

Constraint C-001 Enforcement:
    This service strictly enforces Constraint C-001 — no production data is
    accessed or stored. Only schema metadata (table names, column definitions,
    foreign key relationships, data type specifications) and aggregate
    statistical profiles (distributions, value ranges, cardinality counts)
    flow through the service. Raw production records are never read, cached,
    or persisted.

Architecture:
    The Profiling Service follows the Flask Application Factory pattern.
    Heavy initialization (Flask app, database connections, ERP connectors,
    Blueprint registration) is deferred to ``create_app()`` in ``app.py``.
    This ``__init__.py`` is intentionally kept minimal to avoid circular
    dependency issues across submodules.

Submodules:
    connectors: ERP system connector implementations (SAP, Oracle, Dynamics, JDBC).
    discovery: Schema extraction, relationship mapping, and dependency analysis.
    profilers: Statistical profiling and pattern analysis engines.
    models: MongoDB document models for schema definitions and statistical profiles.
    config: Environment-specific configuration classes.
    app: Flask Application Factory (``create_app``).

Example:
    Creating and running the Profiling Service application::

        from profiling_service.app import create_app

        app = create_app("development")
        app.run(host="0.0.0.0", port=8002)
"""

__version__: str = "1.0.0"
"""Current version of the Profiling Service package.

Follows `Semantic Versioning <https://semver.org/>`_:
    - MAJOR: Incompatible API changes.
    - MINOR: Backward-compatible feature additions.
    - PATCH: Backward-compatible bug fixes.
"""

__all__: list[str] = [
    "DevelopmentConfig",
    "ProductionConfig",
    "ProfilingServiceConfig",
    "TestingConfig",
    "create_app",
    "get_config",
]
"""Public API surface of the Profiling Service package.

Components are listed as string references only to prevent eager imports
and avoid circular dependency issues. Use explicit imports from the
relevant submodules:

    - ``from profiling_service.app import create_app``
    - ``from profiling_service.config import ProfilingServiceConfig``
    - ``from profiling_service.config import get_config``
"""
