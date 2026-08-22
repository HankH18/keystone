"""FastAPI application factory.

Only the `/health` stub lives here for now. Per-source adapter and database
reachability checks land in T-4 and populate the currently empty `checks` map.
"""

from __future__ import annotations

from typing import Any

from fastapi import FastAPI

from recon import __version__

SERVICE_NAME = "keystone"


def create_app() -> FastAPI:
    """Build and return the Keystone FastAPI application."""
    app = FastAPI(title="Keystone reconciliation service", version=__version__)

    @app.get("/health")
    def health() -> dict[str, Any]:
        """Liveness probe. `checks` stays empty until T-4 adds real probes."""
        return {
            "status": "ok",
            "service": SERVICE_NAME,
            "version": __version__,
            "checks": {},
        }

    return app
