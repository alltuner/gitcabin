# ABOUTME: FastAPI application factory wiring REST routes and the GraphQL endpoint together.
# ABOUTME: For github.localhost gh expects REST at / and GraphQL at /graphql with no /api prefix.

from __future__ import annotations

from fastapi import FastAPI

from testgit import graphql_schema, rest
from testgit.config import Settings


def create_app(settings: Settings | None = None) -> FastAPI:
    """Build a FastAPI app instance.

    Tests call this with a fresh Settings to keep state fully isolated. Production
    entry points read Settings from the environment via Settings.from_env().
    """
    settings = settings or Settings.from_env()

    app = FastAPI(title="testgit", version="0.1.0", redoc_url=None, docs_url=None)
    # Stash settings on app.state so the GraphQL context_getter can reach them
    # without us having to thread Settings through every resolver signature.
    app.state.settings = settings

    app.include_router(rest.build_router(settings))
    # Mount the GraphQL ASGI app at /graphql to match what gh sends for github.localhost.
    app.mount("/graphql", graphql_schema.build_app(settings))

    return app
