"""API Gateway routes package — Blueprint registration aggregator.

This module serves as the central entry point for all route Blueprints in the
API Gateway service.  It imports the nine domain-specific Blueprints and exposes
a :func:`register_blueprints` helper that the Application Factory (``app.py``)
calls during startup to wire all REST API endpoints into the Flask application.

Blueprint URL Prefix Strategy:

    - Health endpoints (``/health``, ``/ready``) are registered at the root
      URL prefix (``""``).  This ensures Kubernetes liveness and readiness
      probes work without the ``/api/v1/`` prefix.
    - All other domain endpoints are registered under ``/api/v1/`` to follow
      URL-path API versioning per requirement R-012.

Typical usage::

    from api_gateway.routes import register_blueprints

    app = Flask(__name__)
    register_blueprints(app)
"""

from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from flask import Flask

from api_gateway.routes.admin import admin_bp
from api_gateway.routes.auth import auth_bp
from api_gateway.routes.export import export_bp
from api_gateway.routes.generation import generation_bp
from api_gateway.routes.health import health_bp
from api_gateway.routes.monitoring import monitoring_bp
from api_gateway.routes.profiles import profiles_bp
from api_gateway.routes.schemas import schemas_bp
from api_gateway.routes.templates import templates_bp


__all__ = [
    "register_blueprints",
    "generation_bp",
    "profiles_bp",
    "schemas_bp",
    "templates_bp",
    "export_bp",
    "auth_bp",
    "admin_bp",
    "health_bp",
    "monitoring_bp",
]


def register_blueprints(app: Flask) -> None:
    """Register all route Blueprints with the Flask application.

    This function is called once by the Application Factory (``create_app()``)
    during service startup.  It registers each Blueprint with the appropriate
    URL prefix, enforcing URL-path API versioning per R-012.

    Health check endpoints are registered at the root to ensure Kubernetes
    probes can access ``/health`` and ``/ready`` without the ``/api/v1/``
    prefix.

    Args:
        app: The Flask application instance to register Blueprints on.

    Example::

        from api_gateway.routes import register_blueprints

        def create_app():
            app = Flask(__name__)
            register_blueprints(app)
            return app
    """
    # Health and readiness probes at root level for Kubernetes compatibility
    app.register_blueprint(health_bp, url_prefix="")

    # Domain endpoints under /api/v1/ prefix for API versioning (R-012)
    app.register_blueprint(generation_bp, url_prefix="/api/v1/generation")
    app.register_blueprint(profiles_bp, url_prefix="/api/v1/profiles")
    app.register_blueprint(schemas_bp, url_prefix="/api/v1/schemas")
    app.register_blueprint(templates_bp, url_prefix="/api/v1/templates")
    app.register_blueprint(export_bp, url_prefix="/api/v1/export")
    app.register_blueprint(auth_bp, url_prefix="/api/v1/auth")
    app.register_blueprint(admin_bp, url_prefix="/api/v1/admin")
    app.register_blueprint(monitoring_bp, url_prefix="/api/v1/monitoring")
