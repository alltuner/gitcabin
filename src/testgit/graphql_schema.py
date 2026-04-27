# ABOUTME: Strawberry GraphQL schema exposing the subset of GitHub's GraphQL API gh requires.
# ABOUTME: Mounted as a plain ASGI app so FastAPI's signature introspection doesn't touch resolvers.

from __future__ import annotations

import strawberry
from starlette.requests import Request
from strawberry.asgi import GraphQL

from testgit.config import Settings


@strawberry.type
class User:
    """A GitHub user. Only the fields gh actually selects are modeled."""

    login: str


@strawberry.type
class Query:
    @strawberry.field
    def viewer(self, info: strawberry.Info) -> User:
        # gh auth status sends `query { viewer { login } }` to confirm the
        # token resolves to a user. The login is whatever the server says it is —
        # gh just writes it to its config and trusts the answer.
        settings: Settings = info.context["settings"]
        return User(login=settings.viewer_login)


schema = strawberry.Schema(query=Query)


class TestgitGraphQL(GraphQL):
    """ASGI GraphQL app bound to a Settings instance.

    Mounting an ASGI app (instead of using strawberry.fastapi.GraphQLRouter)
    sidesteps a FastAPI 0.136 + Starlette 1.0 + Strawberry 0.315 incompatibility
    where FastAPI's signature introspection misreads Strawberry's request
    handlers and treats `request: Request` as a query-string field.

    Settings are bound at construction so resolvers don't have to dig through
    request.scope. (Mounted apps don't share state with the parent app: the
    ASGI scope rebinds `scope["app"]` to the child for requests under the mount.)
    """

    def __init__(self, schema: strawberry.Schema, settings: Settings) -> None:
        super().__init__(schema)
        self._settings = settings

    async def get_context(self, request: Request, response: object = None) -> dict[str, object]:
        return {"settings": self._settings}


def build_app(settings: Settings) -> TestgitGraphQL:
    return TestgitGraphQL(schema, settings)
