# ABOUTME: FastAPI application factory wiring REST + GraphQL routes for gh.
# ABOUTME: Serves bare paths (github.localhost) and /api/v3 + /api/graphql (GHES shape).

from __future__ import annotations

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse

from gitcabin import graphql_schema, rest
from gitcabin.config import Settings


def create_app(settings: Settings | None = None) -> FastAPI:
    """Build the API FastAPI app — REST + GraphQL for gh.

    The HTML dashboard lives in a separate process (gitcabin.web.app:create_app)
    so each app has only one routing concern. Both processes read the same
    bare repos through the storage layer.
    """
    settings = settings or Settings.from_env()

    app = FastAPI(title="gitcabin", version="0.1.0", redoc_url=None, docs_url=None)
    app.state.settings = settings

    # gh dials a different URL shape depending on the hostname: bare `/` and
    # `/graphql` for github.localhost (the special HTTP path baked into gh),
    # `/api/v3/...` and `/api/graphql` for every other host (the GHES shape).
    # We expose both so a sidecar TLS deploy under a real hostname works the
    # same as the local-only github.localhost path.
    rest_router = rest.build_router(settings)
    app.include_router(rest_router)
    app.include_router(rest_router, prefix="/api/v3")

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
            # graphql-core returns SourceLocation namedtuples for `locations`,
            # which serialize as JSON arrays — but gh's Go decoder expects each
            # location to be {"line": int, "column": int}. Without this mapping
            # any error masks the real message with a Go unmarshalling failure.
            payload["errors"] = [
                {
                    "message": err.message,
                    "locations": [
                        {"line": loc.line, "column": loc.column} for loc in (err.locations or [])
                    ],
                    "path": err.path,
                }
                for err in result.errors
            ]
        return JSONResponse(payload)

    app.post("/graphql")(graphql)
    app.post("/api/graphql")(graphql)

    return app
