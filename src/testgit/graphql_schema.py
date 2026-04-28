# ABOUTME: Strawberry GraphQL schema exposing the subset of GitHub's GraphQL API gh needs.
# ABOUTME: Executed inline from a FastAPI route in app.py — no ASGI mount, no /graphql redirect.

from __future__ import annotations

import strawberry

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
