# ABOUTME: Strawberry GraphQL schema exposing the subset of GitHub's GraphQL API gh needs.
# ABOUTME: Executed inline from a FastAPI route in app.py — no ASGI mount, no /graphql redirect.

from __future__ import annotations

from enum import Enum
from pathlib import Path

import strawberry

from testgit import ids
from testgit.config import Settings
from testgit.storage.issues import IssueState as StorageIssueState
from testgit.storage.issues import create_issue
from testgit.storage.repo import BareRepo


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


@strawberry.enum
class IssueState(Enum):
    OPEN = "OPEN"
    CLOSED = "CLOSED"


@strawberry.type
class Repository:
    """A repository identified by (owner, name).

    The `id` round-trips back to (owner, name) via testgit.ids.decode_repo_id —
    that's how the createIssue mutation knows which bare repo to write to from
    only an opaque `repositoryId`.
    """

    id: str
    name: str
    owner: User
    description: str | None
    has_issues_enabled: bool
    viewer_permission: RepositoryPermission


@strawberry.type
class Issue:
    """An issue at <owner>/<name>#<number>."""

    id: str
    number: int
    title: str
    body: str
    state: IssueState
    url: str
    author: User


@strawberry.input
class CreateIssueInput:
    """Input for the createIssue mutation. Mirrors GitHub's CreateIssueInput.

    Only the fields gh actually sends are required; the rest gh ships
    (assigneeIds, labelIds, milestoneId, projectIds, etc.) are accepted as
    optional so the schema doesn't reject the mutation, but ignored for now.
    """

    repository_id: strawberry.ID
    title: str
    body: str | None = None
    assignee_ids: list[strawberry.ID] | None = None
    label_ids: list[strawberry.ID] | None = None
    milestone_id: strawberry.ID | None = None
    project_ids: list[strawberry.ID] | None = None
    issue_template: str | None = None


@strawberry.type
class CreateIssuePayload:
    """Return shape for createIssue. gh selects `issue { id, url }`."""

    issue: Issue


def _user(login: str) -> User:
    return User(id=ids.user_id(login), login=login)


def _repo_path(settings: Settings, owner: str, name: str) -> Path:
    """Path on disk for a repo's bare git directory."""
    return settings.data_dir / "repos" / owner / f"{name}.git"


def _issue_url(settings: Settings, owner: str, name: str, number: int) -> str:
    # gh prints this URL after `gh issue create` — for github.localhost the
    # natural URL is the parent host. We don't actually serve HTML there yet,
    # but the string is what the user copy-pastes into a browser later.
    _ = settings  # reserved for when we make the host configurable
    return f"http://github.localhost/{owner}/{name}/issues/{number}"


def _issue_state(state: StorageIssueState) -> IssueState:
    return IssueState[state.value]


@strawberry.type
class Query:
    @strawberry.field
    def viewer(self, info: strawberry.Info) -> User:
        # gh auth status sends `query { viewer { login } }` to confirm the
        # token resolves to a user. The login is whatever the server says it is —
        # gh just writes it to its config and trusts the answer.
        settings: Settings = info.context["settings"]
        return _user(settings.viewer_login)

    @strawberry.field
    def repository(self, owner: str, name: str) -> Repository | None:
        # Fixture phase: every (owner, name) resolves to an "exists, you can
        # write to it" repo. The git-backed implementation will replace this
        # with a real lookup against data_dir/repos/<owner>/<name>.git, returning
        # None when the bare repo is absent.
        return Repository(
            id=ids.repo_id(owner, name),
            name=name,
            owner=_user(owner),
            description=None,
            has_issues_enabled=True,
            viewer_permission=RepositoryPermission.ADMIN,
        )


@strawberry.type
class Mutation:
    @strawberry.mutation
    def create_issue(self, info: strawberry.Info, input: CreateIssueInput) -> CreateIssuePayload:
        settings: Settings = info.context["settings"]

        # The repositoryId is the opaque string we returned from Query.repository;
        # decode it back to (owner, name) so we know which bare repo to open.
        coords = ids.decode_repo_id(input.repository_id)
        if coords is None:
            raise ValueError(f"Unknown repositoryId: {input.repository_id!r}")

        repo = BareRepo.open_or_init(_repo_path(settings, coords.owner, coords.name))
        issue = create_issue(
            repo,
            title=input.title,
            body=input.body or "",
            author=settings.viewer_login,
        )

        return CreateIssuePayload(
            issue=Issue(
                id=ids.issue_id(coords.owner, coords.name, issue.number),
                number=issue.number,
                title=issue.title,
                body=issue.body,
                state=_issue_state(issue.state),
                url=_issue_url(settings, coords.owner, coords.name, issue.number),
                author=_user(issue.author),
            )
        )


schema = strawberry.Schema(query=Query, mutation=Mutation)
