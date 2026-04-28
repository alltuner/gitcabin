# ABOUTME: FastAPI application factory wiring REST routes and the GraphQL endpoint together.
# ABOUTME: For github.localhost gh expects REST at / and GraphQL at /graphql with no /api prefix.

from __future__ import annotations

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse

from testgit import graphql_schema, rest
from testgit.config import Settings


def create_app(settings: Settings | None = None) -> FastAPI:
    """Build a FastAPI app instance.

    Tests call this with a fresh Settings to keep state fully isolated. Production
    entry points read Settings from the environment via Settings.from_env().
    """
    settings = settings or Settings.from_env()

    app = FastAPI(title="testgit", version="0.1.0", redoc_url=None, docs_url=None)
    app.state.settings = settings

    app.include_router(rest.build_router(settings))

    @app.post("/graphql")
    async def graphql(request: Request) -> JSONResponse:
        # We execute the schema directly rather than mounting Strawberry's ASGI
        # app: mounted apps trigger a 307 redirect from /graphql to /graphql/,
        # and gh sends to /graphql exactly. Doing it inline also keeps the
        # request fully inside FastAPI's routing layer, avoiding the FastAPI
        # 0.136 + Starlette 1.0 + Strawberry 0.315 introspection bug.
        body = await request.json()
        result = await graphql_schema.schema.execute(
            body["query"],
            variable_values=body.get("variables"),
            operation_name=body.get("operationName"),
            context_value={"settings": settings},
        )
        payload: dict[str, object] = {"data": result.data}
        if result.errors:
            payload["errors"] = [
                {"message": err.message, "locations": err.locations, "path": err.path}
                for err in result.errors
            ]
        return JSONResponse(payload)

    return app
