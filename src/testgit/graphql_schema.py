# ABOUTME: Strawberry GraphQL schema exposing the subset of GitHub's GraphQL API gh needs.
# ABOUTME: Executed inline from a FastAPI route in app.py — no ASGI mount, no /graphql redirect.

from __future__ import annotations

import hashlib
from enum import Enum

import strawberry

from testgit.config import Settings


@strawberry.type
class User:
    """A GitHub user. Only the fields gh actually selects are modeled."""

    # gh's RepositoryGraphQL builder rewrites a bare `owner` selector into
    # `owner{id,login}`, so any User reachable as a repo owner needs an id.
    # The id is opaque to gh; we derive it from the login so it's stable.
    id: str
    login: str


@strawberry.enum
class RepositoryPermission(Enum):
    """Mirrors GitHub's RepositoryPermission enum.

    gh reads `viewerPermission` as a plain string and gates write commands on
    the value being one of ADMIN/MAINTAIN/WRITE. For a self-hosted clone the
    operator is always the owner, so we always answer ADMIN.
    """

    ADMIN = "ADMIN"
    MAINTAIN = "MAINTAIN"
    WRITE = "WRITE"
    TRIAGE = "TRIAGE"
    READ = "READ"


@strawberry.type
class Repository:
    """A repository identified by (owner, name).

    The `id` is opaque to gh but must be stable for the same coordinates so it
    can be used as a foreign key by mutations referencing this repo. We derive
    it deterministically from "<owner>/<name>" rather than allocating a number,
    because repos don't need a monotonic counter — issues and PRs do.
    """

    id: str
    name: str
    owner: User
    description: str | None
    has_issues_enabled: bool
    viewer_permission: RepositoryPermission


def _repo_id(owner: str, name: str) -> str:
    # Prefixed + hashed so the id is greppable in logs and stable across
    # requests. The "R_" prefix loosely mirrors GitHub's GlobalID convention
    # (it ships ids like "R_kgDOBAQqUw") without trying to match its scheme.
    digest = hashlib.sha1(f"{owner}/{name}".encode()).hexdigest()[:16]
    return f"R_{digest}"


def _user_id(login: str) -> str:
    # Same rationale as _repo_id: opaque, stable, greppable. "U_" because
    # GitHub uses that prefix for User node ids.
    digest = hashlib.sha1(f"user/{login}".encode()).hexdigest()[:16]
    return f"U_{digest}"


@strawberry.type
class Query:
    @strawberry.field
    def viewer(self, info: strawberry.Info) -> User:
        # gh auth status sends `query { viewer { login } }` to confirm the
        # token resolves to a user. The login is whatever the server says it is —
        # gh just writes it to its config and trusts the answer.
        settings: Settings = info.context["settings"]
        return User(id=_user_id(settings.viewer_login), login=settings.viewer_login)

    @strawberry.field
    def repository(self, owner: str, name: str) -> Repository | None:
        # Fixture phase: every (owner, name) resolves to an "exists, you can
        # write to it" repo. The git-backed implementation will replace this
        # with a real lookup against ./data/repos/<owner>/<name>.git, returning
        # None when the bare repo is absent.
        return Repository(
            id=_repo_id(owner, name),
            name=name,
            owner=User(id=_user_id(owner), login=owner),
            description=None,
            has_issues_enabled=True,
            viewer_permission=RepositoryPermission.ADMIN,
        )


schema = strawberry.Schema(query=Query)
