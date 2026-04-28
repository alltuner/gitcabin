# ABOUTME: FastAPI factory for the HTML dashboard.
# ABOUTME: Runs as its own process — separate concern from gh's REST/GraphQL container.

from __future__ import annotations

from fastapi import FastAPI

from gitcabin.config import Settings
from gitcabin.web import routes as web_routes


def create_app(settings: Settings | None = None) -> FastAPI:
    """Build the dashboard FastAPI app.

    Reads the same data_dir as the API process; both processes mount the
    repos volume read-only / read-write per their role (the dashboard never
    writes today, but it loads the same Settings so the volume binding is
    identical and we don't have to track two configs).
    """
    settings = settings or Settings.from_env()

    app = FastAPI(title="gitcabin-web", version="0.1.0", redoc_url=None, docs_url=None)
    app.state.settings = settings

    web_routes.mount_static(app)
    app.include_router(web_routes.build_router(settings))

    return app
