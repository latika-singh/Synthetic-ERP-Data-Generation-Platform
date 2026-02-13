"""Gunicorn WSGI entry point for the API Gateway service.

Creates the Flask application instance via the Application Factory pattern
(``create_app()``) and exposes it as the ``app`` object for Gunicorn to
serve.  This module is referenced in the Dockerfile CMD and Kubernetes
Deployment spec as::

    gunicorn wsgi:app --bind 0.0.0.0:5000 --workers 4

Gunicorn configuration is sourced from environment variables following the
12-factor app methodology.

Environment Variables:
    GUNICORN_WORKERS: Number of worker processes.  Defaults to
        ``multiprocessing.cpu_count() * 2 + 1`` per Gunicorn guidelines.
    GUNICORN_BIND: Bind address and port (default: ``0.0.0.0:5000``).
    GUNICORN_TIMEOUT: Worker timeout in seconds (default: ``120``).
    GUNICORN_WORKER_CLASS: Worker type (default: ``sync``, options
        include ``gevent`` for async support).
    GUNICORN_LOG_LEVEL: Log level for Gunicorn output (default: ``info``).

Typical usage::

    # Production (Gunicorn)
    gunicorn wsgi:app --bind 0.0.0.0:5000

    # Development (direct execution)
    python wsgi.py
"""

from __future__ import annotations

import multiprocessing
import os

from api_gateway.app import create_app


# ---------------------------------------------------------------------------
# Gunicorn configuration from environment variables
# ---------------------------------------------------------------------------

def _default_workers() -> int:
    """Calculate the default number of Gunicorn workers.

    Uses the Gunicorn-recommended formula of ``(2 * CPU_COUNT) + 1``.
    Falls back to 4 workers if CPU count cannot be determined.

    Returns:
        Recommended number of Gunicorn worker processes.
    """
    try:
        return multiprocessing.cpu_count() * 2 + 1
    except NotImplementedError:
        return 4


# Read Gunicorn settings from environment variables for 12-factor compliance
workers: int = int(os.environ.get("GUNICORN_WORKERS", _default_workers()))
bind: str = os.environ.get("GUNICORN_BIND", "0.0.0.0:5000")
timeout: int = int(os.environ.get("GUNICORN_TIMEOUT", "120"))
worker_class: str = os.environ.get("GUNICORN_WORKER_CLASS", "sync")
loglevel: str = os.environ.get("GUNICORN_LOG_LEVEL", "info")

# ---------------------------------------------------------------------------
# WSGI application object
# ---------------------------------------------------------------------------

app = create_app()
"""Flask WSGI application instance.

This is the object Gunicorn references when started with
``gunicorn wsgi:app``.  It is created via the Application Factory
at import time so that Gunicorn's forked worker processes all share
the same configuration.
"""


if __name__ == "__main__":
    # Direct execution for local development convenience.
    # In production, always use Gunicorn: ``gunicorn wsgi:app``
    app.run(
        host="0.0.0.0",
        port=5000,
        debug=True,
    )
