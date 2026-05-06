# ABOUTME: Per-repo pending-push state at refs/meta/sync-pending — survives crashes mid-push.
# ABOUTME: Mirrors the sync.config pattern; one ref carries one JSON blob, advanced per write.

from __future__ import annotations

from pydantic import BaseModel, ConfigDict

from gitcabin.storage.repo import BareRepo
from gitcabin.sync._meta_ref import read_meta_blob, write_meta_blob

# A single ref carries the pending-push state. Each write appends a commit so
# the history is auditable; the in-flight push owns this ref for the duration
# of its protocol. Repos that have never started a push simply don't have it.
PENDING_REF = "refs/meta/sync-pending"


class PushedComment(BaseModel):
    """One comment that has already been posted upstream during the current push.

    `local_index` is the position of the comment within the local issue's
    `list_comments(...)` output — the only stable handle we have, since
    local comments are numbered sequentially and reused across pushes.
    Persisting `gh_id` lets the resume path skip a re-POST that would
    otherwise create a duplicate upstream comment.
    """

    model_config = ConfigDict(extra="ignore")

    local_index: int
    gh_id: int
    authored_at: str | None = None


class PendingIssuePush(BaseModel):
    """The durable state of a partially completed `_push_one` invocation.

    Written immediately after the upstream issue POST succeeds; updated as
    each comment POSTs come back; removed once the local re-import + ref
    cleanup completes. Used by retry to figure out which steps are already
    done.
    """

    model_config = ConfigDict(extra="ignore")

    kind: str = "issue"
    gh_number: int
    gh_id: int
    authored_at: str | None = None
    comments: list[PushedComment] = []


class PendingPrPush(BaseModel):
    """The durable state of a partially completed `_push_one_pr` invocation.

    Written immediately after the upstream PR POST succeeds; removed once
    the local re-import + ref cleanup completes. PRs don't carry a comment
    sub-protocol the way issues do — the body is the only "comment-shaped"
    content and lands as part of the same POST.
    """

    model_config = ConfigDict(extra="ignore")

    kind: str = "pr"
    gh_number: int
    gh_id: int
    authored_at: str | None = None


class PendingState(BaseModel):
    """The whole `refs/meta/sync-pending` document.

    Keyed by local-ref path (e.g. `refs/issues/local/3`) so the same
    document can carry pending state for multiple in-flight items.
    """

    model_config = ConfigDict(extra="ignore")

    issues: dict[str, PendingIssuePush] = {}
    prs: dict[str, PendingPrPush] = {}


def read_pending(repo: BareRepo) -> PendingState:
    """Return the current pending document, or an empty one if the ref is absent."""
    raw = read_meta_blob(repo, PENDING_REF, "pending.json")
    if raw is None:
        return PendingState()
    return PendingState.model_validate_json(raw)


def write_pending(repo: BareRepo, state: PendingState) -> None:
    """Persist the pending document, advancing refs/meta/sync-pending by one commit."""
    write_meta_blob(
        repo,
        PENDING_REF,
        "pending.json",
        state.model_dump_json(indent=2),
        message="sync-pending: update",
    )


def clear_issue(repo: BareRepo, local_ref: str) -> None:
    """Remove the pending entry for `local_ref` after a successful push."""
    state = read_pending(repo)
    if local_ref not in state.issues:
        return
    new_issues = {k: v for k, v in state.issues.items() if k != local_ref}
    write_pending(repo, PendingState(issues=new_issues, prs=state.prs))


def clear_pr(repo: BareRepo, local_ref: str) -> None:
    """Remove the pending PR entry for `local_ref` after a successful push."""
    state = read_pending(repo)
    if local_ref not in state.prs:
        return
    new_prs = {k: v for k, v in state.prs.items() if k != local_ref}
    write_pending(repo, PendingState(issues=state.issues, prs=new_prs))


def record_issue_pushed(
    repo: BareRepo,
    local_ref: str,
    *,
    gh_number: int,
    gh_id: int,
    authored_at: str | None,
) -> None:
    """Stamp a freshly-POSTed issue into pending state — call this between
    the upstream POST and any further side effects."""
    state = read_pending(repo)
    state.issues[local_ref] = PendingIssuePush(
        gh_number=gh_number, gh_id=gh_id, authored_at=authored_at
    )
    write_pending(repo, state)


def record_pr_pushed(
    repo: BareRepo,
    local_ref: str,
    *,
    gh_number: int,
    gh_id: int,
    authored_at: str | None,
) -> None:
    """Stamp a freshly-POSTed PR into pending state — call this between
    the upstream POST and any further side effects."""
    state = read_pending(repo)
    state.prs[local_ref] = PendingPrPush(
        gh_number=gh_number, gh_id=gh_id, authored_at=authored_at
    )
    write_pending(repo, state)


def record_comment_pushed(
    repo: BareRepo,
    local_ref: str,
    *,
    local_index: int,
    gh_id: int,
    authored_at: str | None,
) -> None:
    """Append a posted comment to the pending entry for `local_ref`."""
    state = read_pending(repo)
    entry = state.issues[local_ref]
    entry.comments.append(
        PushedComment(local_index=local_index, gh_id=gh_id, authored_at=authored_at)
    )
    write_pending(repo, state)
