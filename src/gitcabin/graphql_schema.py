# ABOUTME: Strawberry GraphQL schema exposing the subset of GitHub's GraphQL API gh needs.
# ABOUTME: Executed inline from a FastAPI route in app.py — no ASGI mount, no /graphql redirect.

from __future__ import annotations

import subprocess
from datetime import UTC, datetime
from enum import Enum
from pathlib import Path
from typing import Annotated

import strawberry

from gitcabin import ids
from gitcabin.config import Settings
from gitcabin.permissions import (
    RepoRole,
    can_change_issue_state,
    can_delete_comment,
    can_edit_comment,
    can_edit_issue,
    viewer_role,
)
from gitcabin.storage.issues import (
    Comment as StorageComment,
)
from gitcabin.storage.issues import (
    Issue as StorageIssue,
)
from gitcabin.storage.issues import (
    add_any_comment as storage_add_comment,
)
from gitcabin.storage.issues import (
    close_any_issue as storage_close_issue,
)
from gitcabin.storage.issues import (
    create_issue,
    delete_any_comment,
    get_any_issue,
    list_all_issues,
    list_any_comments,
    update_any_comment,
    update_any_issue,
)
from gitcabin.storage.prs import (
    Pr as StoragePr,
)
from gitcabin.storage.prs import (
    PrState,
    get_any_pr,
    list_all_prs,
    list_synced_pr_comments,
)
from gitcabin.storage.repo import BareRepo

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


@strawberry.enum
class RepositoryPrivacy(Enum):
    PUBLIC = "PUBLIC"
    PRIVATE = "PRIVATE"
    INTERNAL = "INTERNAL"


@strawberry.enum
class RepositoryAffiliation(Enum):
    OWNER = "OWNER"
    COLLABORATOR = "COLLABORATOR"
    ORGANIZATION_MEMBER = "ORGANIZATION_MEMBER"


@strawberry.enum
class RepositoryOrderField(Enum):
    CREATED_AT = "CREATED_AT"
    UPDATED_AT = "UPDATED_AT"
    PUSHED_AT = "PUSHED_AT"
    NAME = "NAME"
    STARGAZERS = "STARGAZERS"


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


@strawberry.input
class RepositoryOrder:
    """Sort spec for repositoryOwner.repositories. Accepted-but-ignored for now."""

    field: RepositoryOrderField
    direction: OrderDirection


# ---- Leaf types ---------------------------------------------------------- #


@strawberry.type
class User:
    """A GitHub user. gh's `...on User{...}` selects id, login, optionally name."""

    id: str
    login: str
    name: str | None = None
    database_id: int | None = None

    @strawberry.field
    def repositories(
        self,
        info: strawberry.Info,
        first: int | None = None,
        after: str | None = None,
        privacy: RepositoryPrivacy | None = None,
        is_fork: bool | None = None,
        owner_affiliations: list[RepositoryAffiliation] | None = None,
        order_by: RepositoryOrder | None = None,
    ) -> RepositoryConnection:
        # gh repo list with no owner arg rewrites to `repositoryOwner: viewer`,
        # i.e. it queries User.repositories instead of RepositoryOwner.repositories.
        # Same shape, same arg names — both lookups walk the user's owner dir.
        _ = (after, owner_affiliations, order_by)
        return _build_repo_connection(
            info.context["settings"],
            self.login,
            first=first,
            privacy=privacy,
            is_fork=is_fork,
        )


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
    viewer_can_update: bool
    viewer_can_delete: bool

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


def _to_gql_issue(
    stored: StorageIssue, owner: str, name: str, viewer: str, role: RepoRole
) -> Issue:
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
        viewer_did_author=stored.author == viewer,
        viewer_can_update=can_edit_issue(stored, viewer),
        viewer_can_close_or_reopen=can_change_issue_state(stored, viewer, role),
    )


def _to_gql_comment(
    stored: StorageComment, owner: str, name: str, issue_number: int,
    viewer: str, role: RepoRole,
) -> IssueComment:
    is_author = stored.author == viewer
    return IssueComment(
        id=ids.comment_id(owner, name, issue_number, stored.number),
        author=_user_for(stored.author),
        # OWNER == "viewer authored this comment", per GitHub's enum semantics.
        # Map to OWNER for the viewer's own comments and NONE otherwise so gh
        # renders a sensible label.
        author_association="OWNER" if is_author else "NONE",
        body=stored.body,
        created_at=stored.created_at,
        includes_created_edit=False,
        is_minimized=False,
        minimized_reason=None,
        url=_comment_url(owner, name, issue_number, stored.number),
        viewer_did_author=is_author,
        viewer_can_update=can_edit_comment(stored, viewer),
        viewer_can_delete=can_delete_comment(stored, viewer, role),
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
    viewer_did_author: bool = False
    viewer_can_update: bool = False
    viewer_can_close_or_reopen: bool = False

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
        stored = list_any_comments(bare, coords.number)
        viewer = settings.viewer_login
        role = viewer_role(bare)
        nodes = [
            _to_gql_comment(c, coords.owner, coords.name, coords.number, viewer, role)
            for c in stored
        ]
        if first is not None:
            nodes = nodes[:first]
        elif last is not None:
            nodes = nodes[-last:] if last > 0 else []
        return IssueCommentConnection(nodes=nodes, total_count=len(stored), page_info=empty_page)

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
    """A pull request mirrored from GitHub.

    State is exposed as IssueState (OPEN/CLOSED) for gh wire compatibility —
    `merged` is a separate boolean flag. `head_ref_name` / `base_ref_name`
    are the source/target branch labels, mostly for display.
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
    head_ref_name: str = ""
    base_ref_name: str = ""
    is_draft: bool = False
    merged: bool = False
    viewer_did_author: bool = False
    viewer_can_update: bool = False
    viewer_can_close_or_reopen: bool = False

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
        _ = (after, before)
        empty_page = PageInfo(has_next_page=False, end_cursor=None)
        coords = ids.decode_issue_id(self.id)
        if coords is None:
            return IssueCommentConnection(nodes=[], total_count=0, page_info=empty_page)
        settings: Settings = info.context["settings"]
        bare = _open_bare_or_none(settings, coords.owner, coords.name)
        if bare is None:
            return IssueCommentConnection(nodes=[], total_count=0, page_info=empty_page)
        stored = list_synced_pr_comments(bare, coords.number)
        viewer = settings.viewer_login
        role = viewer_role(bare)
        nodes = [
            _to_gql_comment(c, coords.owner, coords.name, coords.number, viewer, role)
            for c in stored
        ]
        if first is not None:
            nodes = nodes[:first]
        elif last is not None:
            nodes = nodes[-last:] if last > 0 else []
        return IssueCommentConnection(nodes=nodes, total_count=len(stored), page_info=empty_page)

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


def _to_gql_pr(
    stored: StoragePr, owner: str, name: str, viewer: str, role: RepoRole
) -> PullRequest:
    """Translate a storage Pr into the GraphQL PullRequest. State maps OPEN
    or CLOSED for gh wire compatibility — merged is exposed via the boolean."""
    state = IssueState.OPEN if stored.state is PrState.OPEN else IssueState.CLOSED
    is_author = stored.author == viewer
    return PullRequest(
        id=ids.issue_id(owner, name, stored.number),
        number=stored.number,
        title=stored.title,
        body=stored.body,
        state=state,
        url=_issue_url(owner, name, stored.number),
        author=_user_for(stored.author),
        created_at=stored.created_at,
        updated_at=stored.updated_at,
        head_ref_name=stored.head_ref,
        base_ref_name=stored.base_ref,
        is_draft=stored.is_draft,
        merged=stored.state is PrState.MERGED,
        viewer_did_author=is_author,
        # PRs are content-edit-only-by-author, mirroring issues. Merge / close
        # are admin actions but we surface them under the same close gate for now.
        viewer_can_update=is_author,
        viewer_can_close_or_reopen=is_author or role in _PRIVILEGED_ROLES_FOR_PR,
    )


# Same triage-or-above ladder issues use; PRs share the rule because closing
# someone else's PR is the same kind of moderation action as closing an issue.
_PRIVILEGED_ROLES_FOR_PR: frozenset[RepoRole] = frozenset(
    {RepoRole.TRIAGE, RepoRole.WRITE, RepoRole.MAINTAIN, RepoRole.ADMIN}
)


@strawberry.type
class PullRequestConnection:
    nodes: list[PullRequest]
    page_info: PageInfo
    total_count: int


@strawberry.type
class IssueConnection:
    nodes: list[Issue]
    page_info: PageInfo
    total_count: int


# ---- Repository --------------------------------------------------------- #


def _repo_path(settings: Settings, owner: str, name: str) -> Path:
    """Path on disk for a repo's bare git directory."""
    return (settings.data_dir / "projects" / owner / name).with_suffix(".git")


def _open_bare_or_none(settings: Settings, owner: str, name: str) -> BareRepo | None:
    path = _repo_path(settings, owner, name)
    if not path.is_dir():
        return None
    return BareRepo.open_or_init(path)


def _repo_url(owner: str, name: str) -> str:
    return f"http://github.localhost/{owner}/{name}"


@strawberry.type
class Ref:
    """A git ref (branch/tag). gh's defaultBranchRef selects just `name`."""

    name: str


@strawberry.type
class Repository:
    """A repository identified by (owner, name).

    The `id` round-trips back to (owner, name) via gitcabin.ids.decode_repo_id —
    that's how the createIssue mutation knows which bare repo to write to from
    only an opaque `repositoryId`.
    """

    id: str
    name: str
    owner: User
    description: str | None
    has_issues_enabled: bool
    viewer_permission: RepositoryPermission
    is_private: bool = False
    is_fork: bool = False
    is_archived: bool = False
    visibility: RepositoryPrivacy = RepositoryPrivacy.PUBLIC
    # ISO-8601 strings — populated by _to_gql_repository from git/filesystem
    # state. Defaulted so other Repository constructions (Query.repository)
    # don't have to plumb them through.
    created_at: str = ""
    pushed_at: str = ""

    @strawberry.field
    def url(self) -> str:
        # Web URL on github.localhost. We don't host an HTML repo view yet,
        # but `gh repo view --json url` and any caller that pastes the URL
        # into a browser need a real-shaped string.
        return _repo_url(self.owner.login, self.name)

    @strawberry.field
    def name_with_owner(self) -> str:
        return f"{self.owner.login}/{self.name}"

    @strawberry.field
    def default_branch_ref(self, info: strawberry.Info) -> Ref | None:
        # Resolves from the bare repo's symbolic HEAD. Fresh `git init --bare`
        # sets HEAD to refs/heads/main (initial-branch=main), but a sync from
        # an upstream would make HEAD whatever the upstream had — read it
        # from git rather than hard-coding "main".
        settings: Settings = info.context["settings"]
        bare = _open_bare_or_none(settings, self.owner.login, self.name)
        if bare is None:
            return None
        try:
            head_ref = bare.repo.head.reference.name
        except (TypeError, ValueError):
            # Detached HEAD or no commits yet — gh tolerates a null here.
            return None
        return Ref(name=head_ref)

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
        all_stored = list_all_issues(bare) if bare is not None else []

        if states:
            wanted = {s.name for s in states}
            all_stored = [i for i in all_stored if i.state.value in wanted]

        total = len(all_stored)
        limit = first if first is not None else total
        page = all_stored[:limit]

        viewer = settings.viewer_login
        role = viewer_role(bare)
        return IssueConnection(
            nodes=[_to_gql_issue(i, self.owner.login, self.name, viewer, role) for i in page],
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
        stored = get_any_issue(bare, number)
        if stored is None:
            return None
        return _to_gql_issue(
            stored, self.owner.login, self.name, settings.viewer_login, viewer_role(bare)
        )

    @strawberry.field
    def issue_or_pull_request(
        self, info: strawberry.Info, number: int
    ) -> IssueOrPullRequest | None:
        """gh's IssueByNumber query uses this field — number-based lookup that
        resolves to whichever item type matches. PRs win over issues when both
        exist (which can't actually happen in real GitHub repos because the
        numbering is shared, but we check both namespaces deterministically)."""
        settings: Settings = info.context["settings"]
        bare = _open_bare_or_none(settings, self.owner.login, self.name)
        if bare is None:
            return None
        viewer = settings.viewer_login
        role = viewer_role(bare)
        pr = get_any_pr(bare, number)
        if pr is not None:
            return _to_gql_pr(pr, self.owner.login, self.name, viewer, role)
        return self.issue(info, number)

    @strawberry.field
    def pull_request(self, info: strawberry.Info, number: int) -> PullRequest | None:
        """Single-PR lookup by number. Checks both synced and local namespaces;
        synced wins on collision (rare; only happens before push)."""
        settings: Settings = info.context["settings"]
        bare = _open_bare_or_none(settings, self.owner.login, self.name)
        if bare is None:
            return None
        stored = get_any_pr(bare, number)
        if stored is None:
            return None
        return _to_gql_pr(
            stored, self.owner.login, self.name, settings.viewer_login, viewer_role(bare)
        )

    @strawberry.field
    def pull_requests(
        self,
        info: strawberry.Info,
        first: int | None = None,
        after: str | None = None,
        states: list[IssueState] | None = None,
    ) -> PullRequestConnection:
        """List of synced + local PRs, synced first then local (matches the
        list_all_issues ordering). State filter accepts gh's IssueState
        (OPEN/CLOSED); merged PRs are filtered as CLOSED for wire compatibility."""
        _ = after
        settings: Settings = info.context["settings"]
        bare = _open_bare_or_none(settings, self.owner.login, self.name)
        all_stored = list_all_prs(bare) if bare is not None else []

        if states:
            wanted = {s.name for s in states}

            def _match(pr: StoragePr) -> bool:
                # GraphQL wire IssueState only has OPEN/CLOSED, so a "CLOSED"
                # filter must include MERGED PRs (they're closed-and-merged).
                if pr.state is PrState.OPEN:
                    return "OPEN" in wanted
                return "CLOSED" in wanted

            all_stored = [p for p in all_stored if _match(p)]

        total = len(all_stored)
        limit = first if first is not None else total
        page = all_stored[:limit]
        viewer = settings.viewer_login
        role = viewer_role(bare)
        return PullRequestConnection(
            nodes=[_to_gql_pr(p, self.owner.login, self.name, viewer, role) for p in page],
            page_info=PageInfo(has_next_page=limit < total, end_cursor=None),
            total_count=total,
        )


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


@strawberry.input
class UpdateIssueInput:
    """Input for the updateIssue mutation. Mirrors gh's UpdateIssue selection."""

    id: strawberry.ID
    title: str | None = None
    body: str | None = None
    client_mutation_id: str | None = None


@strawberry.type
class UpdateIssuePayload:
    issue: Issue


@strawberry.input
class UpdateIssueCommentInput:
    """Input for the updateIssueComment mutation."""

    id: strawberry.ID
    body: str
    client_mutation_id: str | None = None


@strawberry.type
class UpdateIssueCommentPayload:
    issue_comment: IssueComment


@strawberry.input
class DeleteIssueCommentInput:
    """Input for the deleteIssueComment mutation."""

    id: strawberry.ID
    client_mutation_id: str | None = None


@strawberry.type
class DeleteIssueCommentPayload:
    """Return shape for deleteIssueComment. gh selects clientMutationId."""

    client_mutation_id: str | None = None


# ---- RepositoryConnection / RepositoryOwner ----------------------------- #


@strawberry.type
class RepositoryConnection:
    """The connection type behind RepositoryOwner.repositories (gh repo list)."""

    nodes: list[Repository]
    total_count: int
    page_info: PageInfo


def _to_gql_repository(bare: BareRepo, owner: str, name: str) -> Repository:
    """Build a Repository from on-disk state. Used by the repo list resolver."""
    created_at, pushed_at = _repo_timestamps(bare)
    return Repository(
        id=ids.repo_id(owner, name),
        name=name,
        owner=_user_for(owner),
        description=None,
        has_issues_enabled=True,
        viewer_permission=RepositoryPermission.ADMIN,
        is_private=False,
        is_fork=False,
        is_archived=False,
        visibility=RepositoryPrivacy.PUBLIC,
        created_at=created_at,
        pushed_at=pushed_at,
    )


def _build_repo_connection(
    settings: Settings,
    login: str,
    *,
    first: int | None,
    privacy: RepositoryPrivacy | None,
    is_fork: bool | None,
) -> RepositoryConnection:
    """Walk data_dir/projects/<login>/*.git and build a RepositoryConnection.

    Shared between Query.repositoryOwner.repositories and Viewer.repositories
    (gh repo list with and without an owner arg). We're always PUBLIC and
    never a fork, so privacy=PRIVATE / isFork=true legitimately yields empty.
    """
    empty = RepositoryConnection(
        nodes=[], total_count=0, page_info=PageInfo(has_next_page=False, end_cursor=None)
    )
    if privacy is not None and privacy is not RepositoryPrivacy.PUBLIC:
        return empty
    if is_fork is True:
        return empty

    owner_dir = settings.data_dir / "projects" / login
    repos: list[Repository] = []
    if owner_dir.is_dir():
        for entry in sorted(owner_dir.iterdir()):
            if not entry.name.endswith(".git"):
                continue
            bare = BareRepo.open(entry)
            if bare is None:
                continue
            repos.append(_to_gql_repository(bare, login, entry.name[:-4]))

    # Sort newest-pushed-first to match gh's default `orderBy: PUSHED_AT DESC`.
    repos.sort(key=lambda r: r.pushed_at, reverse=True)

    total = len(repos)
    limit = first if first is not None else total
    page = repos[:limit]
    return RepositoryConnection(
        nodes=page,
        total_count=total,
        page_info=PageInfo(has_next_page=limit < total, end_cursor=None),
    )


def _repo_timestamps(bare: BareRepo) -> tuple[str, str]:
    """Return (created_at, pushed_at) as ISO-8601 strings.

    pushed_at is the latest commit author date across all branches: a
    one-shot `git log --max-count=1` returns it in O(1). created_at is the
    oldest reachable commit's author date: `--max-parents=0` filters to
    root commits (typically one per repo) and we take the oldest among
    them. This stays O(roots) which is effectively O(1) for real repos.

    `--reverse --max-count=1` does NOT work for finding the oldest commit:
    git applies --max-count during traversal (newest-first) and only then
    reverses, so the result is still the newest commit.

    With no commits at all (a fresh `git init`), both timestamps fall back
    to the bare directory's mtime so the field still has a real value gh
    can render.
    """
    try:
        newest = bare.run_git(
            "log", "--all", "--max-count=1", "--format=%aI"
        ).strip()
        # Root-commit dates, one per line; pick the smallest. There's
        # almost always exactly one (the initial commit) but a repo could
        # have multiple roots from grafted branches.
        roots = bare.run_git(
            "log", "--all", "--max-parents=0", "--format=%aI"
        ).strip()
    except subprocess.CalledProcessError:
        newest = roots = ""
    if not newest:
        mtime = datetime.fromtimestamp(bare.path.stat().st_mtime, tz=UTC).isoformat()
        return (mtime, mtime)
    oldest = min(roots.splitlines()) if roots else newest
    return (oldest, newest)


@strawberry.type
class RepositoryOwner:
    """A user or organization that owns repositories.

    GitHub models RepositoryOwner as an interface implemented by User and
    Organization; gh's repo list query selects only the shared fields
    (`login` and `repositories`), so a single concrete type is enough.
    """

    login: str

    @strawberry.field
    def repositories(
        self,
        info: strawberry.Info,
        first: int | None = None,
        after: str | None = None,
        privacy: RepositoryPrivacy | None = None,
        is_fork: bool | None = None,
        owner_affiliations: list[RepositoryAffiliation] | None = None,
        order_by: RepositoryOrder | None = None,
    ) -> RepositoryConnection:
        # ownerAffiliations/orderBy are part of gh's query template; we accept
        # them for validation but local repos have no notion of affiliation
        # and the connection is always pre-sorted by pushed_at desc.
        _ = (after, owner_affiliations, order_by)
        return _build_repo_connection(
            info.context["settings"],
            self.login,
            first=first,
            privacy=privacy,
            is_fork=is_fork,
        )


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
        # Strict lookup: returns None unless data_dir/projects/<owner>/<name>.git
        # is an actual bare repo. gh repo view of a nonexistent repo correctly
        # surfaces NOT_FOUND instead of silently inventing a fixture.
        #
        # Note: this means `gh issue create -R new/repo` against a fresh repo
        # name now fails — gh's IssueRepoInfo lookup returns null, so gh aborts
        # before sending the createIssue mutation. To bring a new repo into
        # existence today, mkdir the bare dir on the host (or wait for a
        # createRepository mutation in a follow-up).
        settings: Settings = info.context["settings"]
        bare = BareRepo.open(_repo_path(settings, owner, name))
        if bare is None:
            return None
        return _to_gql_repository(bare, owner, name)

    @strawberry.field
    def repository_owner(self, info: strawberry.Info, login: str) -> RepositoryOwner | None:
        """Return the owner namespace at data_dir/projects/<login>/, or None.

        gh repo list sends `repositoryOwner(login: $owner)`; null means
        NOT_FOUND. We say None when no directory at all exists for that login
        (so `gh repo list ghost-org` shows nothing-found cleanly), but if the
        directory exists with zero repos inside, we still resolve — the owner
        is real, just empty.
        """
        settings: Settings = info.context["settings"]
        owner_dir = settings.data_dir / "projects" / login
        if not owner_dir.is_dir():
            return None
        return RepositoryOwner(login=login)


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

        return CreateIssuePayload(
            issue=_to_gql_issue(
                stored, coords.owner, coords.name, settings.viewer_login, viewer_role(repo)
            )
        )

    @strawberry.mutation
    def close_issue(self, info: strawberry.Info, input: CloseIssueInput) -> CloseIssuePayload:
        settings: Settings = info.context["settings"]

        coords = ids.decode_issue_id(input.issue_id)
        if coords is None:
            raise ValueError(f"Unknown issueId: {input.issue_id!r}")

        bare = _open_bare_or_none(settings, coords.owner, coords.name)
        if bare is None:
            raise ValueError(f"Unknown issueId: {input.issue_id!r}")

        # Permission check before mutating: a viewer who isn't the author and
        # doesn't have a privileged role on the repo cannot close someone
        # else's issue. For local-only repos the role defaults to ADMIN, so
        # this is effectively a no-op there. For synced repos it's the
        # security boundary protecting upstream-authored issues.
        existing = get_any_issue(bare, coords.number)
        if existing is None:
            raise ValueError(f"Unknown issueId: {input.issue_id!r}")
        viewer = settings.viewer_login
        role = viewer_role(bare)
        if not can_change_issue_state(existing, viewer, role):
            raise PermissionError(
                f"viewer {viewer!r} cannot change state of issue authored by "
                f"{existing.author!r}"
            )

        # stateReason / duplicateIssueId are accepted-and-ignored for now —
        # the storage doc only models OPEN/CLOSED, and gh treats the close as
        # successful as long as the mutation returns the issue. A follow-up
        # can extend IssueDocument to persist the reason.
        stored = storage_close_issue(bare, number=coords.number, actor=viewer)
        if stored is None:
            raise ValueError(f"Unknown issueId: {input.issue_id!r}")

        return CloseIssuePayload(
            issue=_to_gql_issue(stored, coords.owner, coords.name, viewer, role)
        )

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
                node=_to_gql_comment(
                    stored,
                    coords.owner,
                    coords.name,
                    coords.number,
                    settings.viewer_login,
                    viewer_role(bare),
                ),
            ),
        )

    @strawberry.mutation
    def update_issue(
        self, info: strawberry.Info, input: UpdateIssueInput
    ) -> UpdateIssuePayload:
        """Edit an issue's title and/or body. Author-only — non-authors get
        a permission error regardless of repo role, because content edits
        attribute words to whoever wrote them upstream."""
        settings: Settings = info.context["settings"]
        coords = ids.decode_issue_id(input.id)
        if coords is None:
            raise ValueError(f"Unknown issue id: {input.id!r}")

        bare = _open_bare_or_none(settings, coords.owner, coords.name)
        if bare is None:
            raise ValueError(f"Unknown issue id: {input.id!r}")

        existing = get_any_issue(bare, coords.number)
        if existing is None:
            raise ValueError(f"Unknown issue id: {input.id!r}")
        viewer = settings.viewer_login
        if not can_edit_issue(existing, viewer):
            raise PermissionError(
                f"viewer {viewer!r} cannot edit issue authored by {existing.author!r}"
            )

        stored = update_any_issue(
            bare,
            number=coords.number,
            title=input.title if input.title is not None else existing.title,
            body=input.body if input.body is not None else existing.body,
            actor=viewer,
        )
        if stored is None:
            raise ValueError(f"Unknown issue id: {input.id!r}")
        return UpdateIssuePayload(
            issue=_to_gql_issue(
                stored, coords.owner, coords.name, viewer, viewer_role(bare)
            )
        )

    @strawberry.mutation
    def update_issue_comment(
        self, info: strawberry.Info, input: UpdateIssueCommentInput
    ) -> UpdateIssueCommentPayload:
        """Edit a comment's body. Author-only — same impersonation guard as
        update_issue. Repo admins can DELETE other people's comments
        (deleteIssueComment) but cannot edit them."""
        settings: Settings = info.context["settings"]
        coords = ids.decode_comment_id(input.id)
        if coords is None:
            raise ValueError(f"Unknown comment id: {input.id!r}")

        bare = _open_bare_or_none(settings, coords.owner, coords.name)
        if bare is None:
            raise ValueError(f"Unknown comment id: {input.id!r}")

        existing = next(
            (c for c in list_any_comments(bare, coords.issue_number)
             if c.number == coords.number),
            None,
        )
        if existing is None:
            raise ValueError(f"Unknown comment id: {input.id!r}")
        viewer = settings.viewer_login
        if not can_edit_comment(existing, viewer):
            raise PermissionError(
                f"viewer {viewer!r} cannot edit comment authored by {existing.author!r}"
            )

        stored = update_any_comment(
            bare,
            issue_number=coords.issue_number,
            comment_number=coords.number,
            body=input.body,
            actor=viewer,
        )
        if stored is None:
            raise ValueError(f"Unknown comment id: {input.id!r}")
        return UpdateIssueCommentPayload(
            issue_comment=_to_gql_comment(
                stored,
                coords.owner,
                coords.name,
                coords.issue_number,
                viewer,
                viewer_role(bare),
            ),
        )

    @strawberry.mutation
    def delete_issue_comment(
        self, info: strawberry.Info, input: DeleteIssueCommentInput
    ) -> DeleteIssueCommentPayload:
        """Delete a comment. Author can always delete their own; non-authors
        need ADMIN role on the repo (per the moderation rule in
        docs/github-sync.md). TRIAGE/WRITE/MAINTAIN can't delete others'."""
        settings: Settings = info.context["settings"]
        coords = ids.decode_comment_id(input.id)
        if coords is None:
            raise ValueError(f"Unknown comment id: {input.id!r}")

        bare = _open_bare_or_none(settings, coords.owner, coords.name)
        if bare is None:
            raise ValueError(f"Unknown comment id: {input.id!r}")

        existing = next(
            (c for c in list_any_comments(bare, coords.issue_number)
             if c.number == coords.number),
            None,
        )
        if existing is None:
            raise ValueError(f"Unknown comment id: {input.id!r}")
        viewer = settings.viewer_login
        role = viewer_role(bare)
        if not can_delete_comment(existing, viewer, role):
            raise PermissionError(
                f"viewer {viewer!r} (role {role.value}) cannot delete comment "
                f"authored by {existing.author!r}"
            )

        ok = delete_any_comment(
            bare,
            issue_number=coords.issue_number,
            comment_number=coords.number,
            actor=viewer,
        )
        if not ok:
            raise ValueError(f"Unknown comment id: {input.id!r}")
        return DeleteIssueCommentPayload(client_mutation_id=input.client_mutation_id)


# Strawberry doesn't auto-discover unused types reachable only via unions, so
# we have to register PullRequest explicitly via `types=[...]` for the union
# to validate. Otherwise gh's `... on PullRequest { ... }` selection would fail
# query validation with "Unknown type 'PullRequest'".
schema = strawberry.Schema(
    query=Query,
    mutation=Mutation,
    types=[Issue, PullRequest],
)
