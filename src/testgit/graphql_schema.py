# ABOUTME: Strawberry GraphQL schema exposing the subset of GitHub's GraphQL API gh needs.
# ABOUTME: Executed inline from a FastAPI route in app.py — no ASGI mount, no /graphql redirect.

from __future__ import annotations

from enum import Enum
from pathlib import Path
from typing import Annotated

import strawberry

from testgit import ids
from testgit.config import Settings
from testgit.storage.issues import (
    Comment as StorageComment,
)
from testgit.storage.issues import (
    Issue as StorageIssue,
)
from testgit.storage.issues import (
    add_comment as storage_add_comment,
)
from testgit.storage.issues import (
    close_issue as storage_close_issue,
)
from testgit.storage.issues import (
    create_issue,
    get_issue,
    list_comments,
    list_issues,
)
from testgit.storage.repo import BareRepo

# ---- Enums --------------------------------------------------------------- #


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


@strawberry.enum
class IssueOrderField(Enum):
    CREATED_AT = "CREATED_AT"
    UPDATED_AT = "UPDATED_AT"
    COMMENTS = "COMMENTS"


@strawberry.enum
class OrderDirection(Enum):
    ASC = "ASC"
    DESC = "DESC"


# ---- Input types --------------------------------------------------------- #


@strawberry.input
class IssueOrder:
    """Sort spec for Repository.issues. Accepted-but-ignored for now."""

    field: IssueOrderField
    direction: OrderDirection


@strawberry.input
class IssueFilters:
    """Filter spec for Repository.issues. Accepted-but-ignored for now."""

    assignee: str | None = None
    created_by: str | None = None
    mentioned: str | None = None
    labels: list[str] | None = None
    states: list[IssueState] | None = None


# ---- Leaf types ---------------------------------------------------------- #


@strawberry.type
class User:
    """A GitHub user. gh's `...on User{...}` selects id, login, optionally name."""

    id: str
    login: str
    name: str | None = None
    database_id: int | None = None


@strawberry.type
class Label:
    """A GitHub label. gh selects id, name, description, color."""

    id: str
    name: str
    description: str | None
    color: str


@strawberry.type
class Milestone:
    """A milestone. We don't track milestones yet, so resolvers return None;
    the type is here so the schema validates `milestone{...}` selections."""

    number: int
    title: str
    description: str | None
    due_on: str | None


@strawberry.type
class IssueComment:
    """A comment on an issue.

    Field set is the union of what gh's `comments` and `lastComment` fragments
    request (api/query_builder.go::issueComments and issueCommentLast). Fields
    we don't track yet (edits, minimization, reactions) return defaulted values.
    """

    id: str
    author: User | None
    author_association: str
    body: str
    created_at: str
    includes_created_edit: bool
    is_minimized: bool
    minimized_reason: str | None
    url: str
    viewer_did_author: bool

    @strawberry.field
    def reaction_groups(self) -> list[ReactionGroup]:
        return []


@strawberry.type
class ReactionGroup:
    """A reaction bucket. gh selects content + users.totalCount."""

    content: str
    users: ReactionGroupUsers


@strawberry.type
class ReactionGroupUsers:
    total_count: int


# ---- Projects v2 stubs --------------------------------------------------- #
# gh's issue view expansion selects projectItems (GitHub Projects v2).
# We don't implement projects, but the schema needs the types so gh's
# query validates. All resolvers return empty/null.


@strawberry.type
class ProjectV2:
    id: str
    title: str
    number: int
    closed: bool
    url: str


@strawberry.type
class ProjectV2ItemFieldSingleSelectValue:
    option_id: str | None
    name: str | None


# Single-member union — Strawberry allows this and gh's `... on
# ProjectV2ItemFieldSingleSelectValue { ... }` selection still works.
ProjectV2ItemFieldValue = Annotated[
    ProjectV2ItemFieldSingleSelectValue,
    strawberry.union("ProjectV2ItemFieldValue"),
]


@strawberry.type
class ProjectV2Item:
    id: str
    project: ProjectV2

    @strawberry.field
    def field_value_by_name(self, name: str) -> ProjectV2ItemFieldValue | None:
        _ = name
        return None


@strawberry.type
class ProjectV2ItemConnection:
    nodes: list[ProjectV2Item]
    total_count: int
    page_info: PageInfo


@strawberry.type
class ProjectV2Connection:
    nodes: list[ProjectV2]
    total_count: int


# ---- Connection / page types -------------------------------------------- #


@strawberry.type
class PageInfo:
    has_next_page: bool
    end_cursor: str | None


@strawberry.type
class LabelConnection:
    nodes: list[Label]
    total_count: int


@strawberry.type
class UserConnection:
    nodes: list[User]
    total_count: int


@strawberry.type
class IssueCommentConnection:
    nodes: list[IssueComment]
    total_count: int
    # gh issue view --comments selects pageInfo to know whether to paginate.
    # We always return everything in one page today, so hasNextPage is False.
    page_info: PageInfo


@strawberry.type
class IssueCommentEdge:
    """Edge wrapper for AddCommentPayload. gh selects `commentEdge { node { url } }`."""

    node: IssueComment


# ---- Issue & PullRequest ------------------------------------------------- #


def _issue_url(owner: str, name: str, number: int) -> str:
    # gh prints this URL after `gh issue create`. For github.localhost we
    # don't actually serve HTML at the parent host yet; the string is what
    # the user copy-pastes into a browser later.
    return f"http://github.localhost/{owner}/{name}/issues/{number}"


def _comment_url(owner: str, name: str, issue_number: int, comment_number: int) -> str:
    # Mirrors gh.com's anchor scheme so users see something familiar after
    # `gh issue comment` even though we don't host an HTML view yet.
    return f"{_issue_url(owner, name, issue_number)}#issuecomment-{comment_number}"


def _user_for(login: str) -> User:
    return User(id=ids.user_id(login), login=login, name=None, database_id=None)


def _to_gql_issue(stored: StorageIssue, owner: str, name: str) -> Issue:
    return Issue(
        id=ids.issue_id(owner, name, stored.number),
        number=stored.number,
        title=stored.title,
        body=stored.body,
        state=IssueState[stored.state.value],
        url=_issue_url(owner, name, stored.number),
        author=_user_for(stored.author),
        created_at=stored.created_at,
        updated_at=stored.updated_at,
        state_reason=None,
    )


def _to_gql_comment(
    stored: StorageComment, owner: str, name: str, issue_number: int
) -> IssueComment:
    return IssueComment(
        id=ids.comment_id(owner, name, issue_number, stored.number),
        author=_user_for(stored.author),
        # OWNER means the commenter has admin rights — true for any local
        # actor on a self-hosted clone, same logic as viewerPermission=ADMIN.
        author_association="OWNER",
        body=stored.body,
        created_at=stored.created_at,
        includes_created_edit=False,
        is_minimized=False,
        minimized_reason=None,
        url=_comment_url(owner, name, issue_number, stored.number),
        viewer_did_author=False,
    )


@strawberry.type
class Issue:
    """An issue at <owner>/<name>#<number>.

    Scalar fields are stored on the instance; relation fields (labels,
    assignees, comments, milestone, reactionGroups) are method resolvers
    because gh's queries pass pagination args like `labels(first: 100)`.
    For now those resolvers return empty/null because we don't track
    labels, assignees, comments, milestones, or reactions yet.
    """

    id: str
    number: int
    title: str
    body: str
    state: IssueState
    url: str
    author: User
    created_at: str
    updated_at: str
    state_reason: str | None = None

    @strawberry.field
    def labels(self, first: int | None = None, after: str | None = None) -> LabelConnection:
        _ = (first, after)
        return LabelConnection(nodes=[], total_count=0)

    @strawberry.field
    def assignees(self, first: int | None = None, after: str | None = None) -> UserConnection:
        _ = (first, after)
        return UserConnection(nodes=[], total_count=0)

    @strawberry.field
    def comments(
        self,
        info: strawberry.Info,
        first: int | None = None,
        last: int | None = None,
        after: str | None = None,
        before: str | None = None,
    ) -> IssueCommentConnection:
        # Single field for both `comments(first:100)` and `comments(last:1)`
        # selections. gh's `lastComment` pseudo-field is just `comments(last:1)`,
        # which lands here too.
        _ = (after, before)
        empty_page = PageInfo(has_next_page=False, end_cursor=None)
        coords = ids.decode_issue_id(self.id)
        if coords is None:
            return IssueCommentConnection(nodes=[], total_count=0, page_info=empty_page)
        settings: Settings = info.context["settings"]
        bare = _open_bare_or_none(settings, coords.owner, coords.name)
        if bare is None:
            return IssueCommentConnection(nodes=[], total_count=0, page_info=empty_page)
        stored = list_comments(bare, coords.number)
        nodes = [_to_gql_comment(c, coords.owner, coords.name, coords.number) for c in stored]
        if first is not None:
            nodes = nodes[:first]
        elif last is not None:
            nodes = nodes[-last:] if last > 0 else []
        return IssueCommentConnection(
            nodes=nodes, total_count=len(stored), page_info=empty_page
        )

    @strawberry.field
    def milestone(self) -> Milestone | None:
        return None

    @strawberry.field
    def reaction_groups(self) -> list[ReactionGroup]:
        return []

    @strawberry.field
    def project_items(
        self, first: int | None = None, after: str | None = None
    ) -> ProjectV2ItemConnection:
        _ = (first, after)
        return ProjectV2ItemConnection(
            nodes=[],
            total_count=0,
            page_info=PageInfo(has_next_page=False, end_cursor=None),
        )


@strawberry.type
class PullRequest:
    """Stub mirroring Issue's shape so the IssueOrPullRequest union validates.

    We don't have PRs yet, so a PullRequest is never returned from any resolver.
    Defining the type with the same fields gh's PullRequestGraphQL fragment
    selects (a strict subset of IssueGraphQL) is enough for query validation.
    """

    id: str
    number: int
    title: str
    body: str
    state: IssueState
    url: str
    author: User
    created_at: str
    updated_at: str

    @strawberry.field
    def labels(self, first: int | None = None, after: str | None = None) -> LabelConnection:
        _ = (first, after)
        return LabelConnection(nodes=[], total_count=0)

    @strawberry.field
    def assignees(self, first: int | None = None, after: str | None = None) -> UserConnection:
        _ = (first, after)
        return UserConnection(nodes=[], total_count=0)

    @strawberry.field
    def comments(
        self,
        first: int | None = None,
        last: int | None = None,
        after: str | None = None,
        before: str | None = None,
    ) -> IssueCommentConnection:
        _ = (first, last, after, before)
        return IssueCommentConnection(
            nodes=[],
            total_count=0,
            page_info=PageInfo(has_next_page=False, end_cursor=None),
        )

    @strawberry.field
    def milestone(self) -> Milestone | None:
        return None

    @strawberry.field
    def reaction_groups(self) -> list[ReactionGroup]:
        return []

    @strawberry.field
    def project_items(
        self, first: int | None = None, after: str | None = None
    ) -> ProjectV2ItemConnection:
        _ = (first, after)
        return ProjectV2ItemConnection(
            nodes=[],
            total_count=0,
            page_info=PageInfo(has_next_page=False, end_cursor=None),
        )


IssueOrPullRequest = Annotated[
    Issue | PullRequest,
    strawberry.union("IssueOrPullRequest"),
]


@strawberry.type
class IssueConnection:
    nodes: list[Issue]
    page_info: PageInfo
    total_count: int


# ---- Repository --------------------------------------------------------- #


def _repo_path(settings: Settings, owner: str, name: str) -> Path:
    """Path on disk for a repo's bare git directory."""
    return settings.data_dir / "repos" / owner / f"{name}.git"


def _open_bare_or_none(settings: Settings, owner: str, name: str) -> BareRepo | None:
    path = _repo_path(settings, owner, name)
    if not path.is_dir():
        return None
    return BareRepo.open_or_init(path)


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

    @strawberry.field
    def issues(
        self,
        info: strawberry.Info,
        first: int | None = None,
        after: str | None = None,
        states: list[IssueState] | None = None,
        order_by: IssueOrder | None = None,
        filter_by: IssueFilters | None = None,
    ) -> IssueConnection:
        # gh's IssueList query passes states/orderBy/filterBy. We accept all
        # of them in the signature so the schema validates, then apply only
        # what's straightforward (state filter, simple limit). orderBy and
        # filterBy beyond states are accepted-and-ignored for now.
        _ = (after, order_by, filter_by)  # reserved
        settings: Settings = info.context["settings"]
        bare = _open_bare_or_none(settings, self.owner.login, self.name)
        all_stored = list_issues(bare) if bare is not None else []

        if states:
            wanted = {s.name for s in states}
            all_stored = [i for i in all_stored if i.state.value in wanted]

        total = len(all_stored)
        limit = first if first is not None else total
        page = all_stored[:limit]

        return IssueConnection(
            nodes=[_to_gql_issue(i, self.owner.login, self.name) for i in page],
            page_info=PageInfo(has_next_page=limit < total, end_cursor=None),
            total_count=total,
        )

    @strawberry.field
    def issue(self, info: strawberry.Info, number: int) -> Issue | None:
        """Single-issue lookup by number. Returns None if not found."""
        settings: Settings = info.context["settings"]
        bare = _open_bare_or_none(settings, self.owner.login, self.name)
        if bare is None:
            return None
        stored = get_issue(bare, number)
        if stored is None:
            return None
        return _to_gql_issue(stored, self.owner.login, self.name)

    @strawberry.field
    def issue_or_pull_request(
        self, info: strawberry.Info, number: int
    ) -> IssueOrPullRequest | None:
        """gh's IssueByNumber query uses this field. We don't have PRs yet, so
        it always resolves to an Issue if one exists at that number."""
        return self.issue(info, number)


# ---- Mutation input/payload --------------------------------------------- #


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


@strawberry.input
class CloseIssueInput:
    """Input for the closeIssue mutation. Mirrors GitHub's CloseIssueInput.

    `stateReason` is "COMPLETED" / "NOT_PLANNED" / "DUPLICATE"; we don't
    persist it yet (the IssueDocument only has OPEN/CLOSED), but we accept it
    so gh's mutation validates. `duplicateIssueId` is only sent with
    stateReason=DUPLICATE; same accept-and-ignore rule applies.
    """

    issue_id: strawberry.ID
    state_reason: str | None = None
    duplicate_issue_id: strawberry.ID | None = None
    client_mutation_id: str | None = None


@strawberry.type
class CloseIssuePayload:
    issue: Issue


@strawberry.input
class AddCommentInput:
    """Input for the addComment mutation. `subjectId` is the issue's GraphQL ID."""

    subject_id: strawberry.ID
    body: str
    client_mutation_id: str | None = None


@strawberry.type
class AddCommentPayload:
    """Return shape for addComment. gh selects `commentEdge { node { url } }`."""

    comment_edge: IssueCommentEdge


# ---- Query / Mutation roots --------------------------------------------- #


@strawberry.type
class Query:
    @strawberry.field
    def viewer(self, info: strawberry.Info) -> User:
        # gh auth status sends `query { viewer { login } }` to confirm the
        # token resolves to a user. The login is whatever the server says it is —
        # gh just writes it to its config and trusts the answer.
        settings: Settings = info.context["settings"]
        return _user_for(settings.viewer_login)

    @strawberry.field
    def repository(self, info: strawberry.Info, owner: str, name: str) -> Repository | None:
        # Strict lookup: returns None unless data_dir/repos/<owner>/<name>.git
        # is an actual bare repo. gh repo view of a nonexistent repo correctly
        # surfaces NOT_FOUND instead of silently inventing a fixture.
        #
        # Note: this means `gh issue create -R new/repo` against a fresh repo
        # name now fails — gh's IssueRepoInfo lookup returns null, so gh aborts
        # before sending the createIssue mutation. To bring a new repo into
        # existence today, mkdir the bare dir on the host (or wait for a
        # createRepository mutation in a follow-up).
        settings: Settings = info.context["settings"]
        if BareRepo.open(_repo_path(settings, owner, name)) is None:
            return None
        return Repository(
            id=ids.repo_id(owner, name),
            name=name,
            owner=_user_for(owner),
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
        stored = create_issue(
            repo,
            title=input.title,
            body=input.body or "",
            author=settings.viewer_login,
        )

        return CreateIssuePayload(issue=_to_gql_issue(stored, coords.owner, coords.name))

    @strawberry.mutation
    def close_issue(self, info: strawberry.Info, input: CloseIssueInput) -> CloseIssuePayload:
        settings: Settings = info.context["settings"]

        coords = ids.decode_issue_id(input.issue_id)
        if coords is None:
            raise ValueError(f"Unknown issueId: {input.issue_id!r}")

        bare = _open_bare_or_none(settings, coords.owner, coords.name)
        # stateReason / duplicateIssueId are accepted-and-ignored for now —
        # the storage doc only models OPEN/CLOSED, and gh treats the close as
        # successful as long as the mutation returns the issue. A follow-up
        # can extend IssueDocument to persist the reason.
        stored = (
            storage_close_issue(bare, number=coords.number, actor=settings.viewer_login)
            if bare is not None
            else None
        )
        if stored is None:
            raise ValueError(f"Unknown issueId: {input.issue_id!r}")

        return CloseIssuePayload(issue=_to_gql_issue(stored, coords.owner, coords.name))

    @strawberry.mutation
    def add_comment(self, info: strawberry.Info, input: AddCommentInput) -> AddCommentPayload:
        settings: Settings = info.context["settings"]

        # subjectId is an issue ID — gh sends the issue's GraphQL id as the
        # comment's subject. We don't accept PR or discussion subjects yet.
        coords = ids.decode_issue_id(input.subject_id)
        if coords is None:
            raise ValueError(f"Unknown subjectId: {input.subject_id!r}")

        bare = _open_bare_or_none(settings, coords.owner, coords.name)
        stored = (
            storage_add_comment(
                bare,
                number=coords.number,
                body=input.body,
                author=settings.viewer_login,
            )
            if bare is not None
            else None
        )
        if stored is None:
            raise ValueError(f"Unknown subjectId: {input.subject_id!r}")

        return AddCommentPayload(
            comment_edge=IssueCommentEdge(
                node=_to_gql_comment(stored, coords.owner, coords.name, coords.number),
            ),
        )


# Strawberry doesn't auto-discover unused types reachable only via unions, so
# we have to register PullRequest explicitly via `types=[...]` for the union
# to validate. Otherwise gh's `... on PullRequest { ... }` selection would fail
# query validation with "Unknown type 'PullRequest'".
schema = strawberry.Schema(
    query=Query,
    mutation=Mutation,
    types=[Issue, PullRequest],
)
