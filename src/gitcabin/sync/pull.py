# ABOUTME: Inbound sync — pulls issues, PRs, and their comments from GitHub.
# ABOUTME: Issues at refs/issues/<n>, PRs at refs/prs/<n>; comments dispatch to the right ref.

from __future__ import annotations

from typing import Any

from gitcabin.storage._git_objects import load_commit
from gitcabin.storage.issues import (
    ISSUE_REF_PREFIX,
    Comment,
    Issue,
    IssueState,
    import_comment,
    import_issue,
)
from gitcabin.storage.prs import (
    PR_REF_PREFIX,
    Pr,
    PrState,
    import_pr,
    import_pr_comment,
)
from gitcabin.storage.repo import BareRepo
from gitcabin.sync.config import SyncConfig
from gitcabin.sync.gh import GhClient


def pull_issues(repo: BareRepo, client: GhClient, config: SyncConfig) -> list[Issue]:
    """Pull every issue from `config.gh_owner/config.gh_name` into the local repo.

    Each issue is written to refs/issues/<gh_number> with provenance
    SYNCED_FROM_GITHUB. Re-pulls replace the existing ref (GitHub wins —
    conflict surfacing is in a later commit). PRs are filtered out: GitHub's
    /issues endpoint returns them with a `pull_request` field, and PR sync
    is a separate code path.

    Returns the list of imported Issues, in upstream order.
    """
    path = f"repos/{config.gh_owner}/{config.gh_name}/issues?state=all&per_page=100"
    payload = client.get_json(path, paginate=True)
    if not isinstance(payload, list):
        raise RuntimeError(
            f"unexpected /issues response: {type(payload).__name__} (wanted list)"
        )

    out: list[Issue] = []
    for item in payload:
        if not isinstance(item, dict):
            continue
        if "pull_request" in item:
            continue
        out.append(_import_one(repo, item))
    return out


def _import_one(repo: BareRepo, gh_issue: dict[str, Any]) -> Issue:
    """Translate a GitHub issue payload into a stored Issue."""
    state = (
        IssueState.CLOSED
        if str(gh_issue.get("state", "open")).lower() == "closed"
        else IssueState.OPEN
    )
    author, gh_author_id = _extract_author(gh_issue.get("user"))
    return import_issue(
        repo,
        number=int(gh_issue["number"]),
        title=str(gh_issue.get("title") or ""),
        body=str(gh_issue.get("body") or ""),
        author=author,
        state=state,
        gh_issue_id=int(gh_issue["id"]),
        gh_author_id=gh_author_id,
        authored_at=gh_issue.get("created_at"),
    )


def _extract_author(user: Any) -> tuple[str, int | None]:
    """Pull `(login, id)` from a GitHub `user` payload.

    GitHub returns null user for items by deleted accounts ("ghost" UI).
    Mirror that behavior with a tombstone author and no stable id.
    """
    if isinstance(user, dict) and "login" in user:
        author = str(user["login"])
        raw_id = user.get("id")
        gh_author_id = int(raw_id) if isinstance(raw_id, int) else None
        return author, gh_author_id
    return "ghost", None


def pull_comments(repo: BareRepo, client: GhClient, config: SyncConfig) -> list[Comment]:
    """Pull every issue comment from the configured repo into refs/issues/<n>:comments/.

    Uses the bulk /issues/comments endpoint (one paginated call instead of one
    per issue). Each comment's `issue_url` field is parsed back to recover the
    issue number it belongs to. Comments on issues that haven't been pulled
    yet (a possible race if pull_issues hasn't run for this repo) are silently
    skipped — the caller is expected to invoke pull_issues first.

    Returns the list of imported Comments. Order matches the upstream listing.
    """
    path = f"repos/{config.gh_owner}/{config.gh_name}/issues/comments?per_page=100"
    payload = client.get_json(path, paginate=True)
    if not isinstance(payload, list):
        raise RuntimeError(
            f"unexpected /issues/comments response: {type(payload).__name__} (wanted list)"
        )

    out: list[Comment] = []
    for item in payload:
        if not isinstance(item, dict):
            continue
        number = _issue_number_from_url(str(item.get("issue_url", "")))
        if number is None:
            continue
        result = _import_one_comment(repo, number, item)
        if result is not None:
            out.append(result)
    return out


def _issue_number_from_url(url: str) -> int | None:
    """Extract the issue number from a GitHub issue URL.

    GitHub's `issue_url` looks like
    https://api.github.com/repos/<owner>/<repo>/issues/<n>. PRs would surface
    as `pulls/<n>` instead, so requiring the parent segment to be `issues`
    filters out the PR-comment case (PR comments have separate sync work).
    """
    parts = url.rstrip("/").split("/")
    if len(parts) < 2 or parts[-2] != "issues":
        return None
    try:
        return int(parts[-1])
    except ValueError:
        return None


def _import_one_comment(
    repo: BareRepo, number: int, gh_comment: dict[str, Any]
) -> Comment | None:
    """Translate a GitHub comment payload and dispatch it to issue or PR storage.

    GitHub's /issues/comments endpoint returns comments for both issues and
    PRs (PRs are issues with extra fields, in GitHub's data model). We
    decide where to store the comment by checking which ref already exists:
    refs/issues/<n> means it's an issue comment, refs/prs/<n> means PR.
    Numbers are unique across both within a repo, so there's never both.
    """
    author, gh_author_id = _extract_author(gh_comment.get("user"))
    body = str(gh_comment.get("body") or "")
    gh_comment_id = int(gh_comment["id"])
    authored_at = gh_comment.get("created_at")

    if load_commit(repo, f"{PR_REF_PREFIX}/{number}") is not None:
        return import_pr_comment(
            repo,
            pr_number=number,
            body=body,
            author=author,
            gh_comment_id=gh_comment_id,
            gh_author_id=gh_author_id,
            authored_at=authored_at,
        )
    if load_commit(repo, f"{ISSUE_REF_PREFIX}/{number}") is not None:
        return import_comment(
            repo,
            issue_number=number,
            body=body,
            author=author,
            gh_comment_id=gh_comment_id,
            gh_author_id=gh_author_id,
            authored_at=authored_at,
        )
    return None


def pull_prs(repo: BareRepo, client: GhClient, config: SyncConfig) -> list[Pr]:
    """Pull every pull request from `config.gh_owner/config.gh_name` into refs/prs/<n>.

    Each PR is written with provenance SYNCED_FROM_GITHUB. Re-pulls replace
    pr.json but preserve the comments/ subtree (same shape as pull_issues).
    State mapping: GitHub's `merged` boolean takes precedence over `state` —
    a closed-and-merged PR surfaces as MERGED, distinct from CLOSED.

    Returns the list of imported Prs in upstream order.
    """
    path = f"repos/{config.gh_owner}/{config.gh_name}/pulls?state=all&per_page=100"
    payload = client.get_json(path, paginate=True)
    if not isinstance(payload, list):
        raise RuntimeError(
            f"unexpected /pulls response: {type(payload).__name__} (wanted list)"
        )

    out: list[Pr] = []
    for item in payload:
        if not isinstance(item, dict):
            continue
        out.append(_import_one_pr(repo, item))
    return out


def _import_one_pr(repo: BareRepo, gh_pr: dict[str, Any]) -> Pr:
    """Translate a GitHub PR payload into a stored Pr."""
    state_str = str(gh_pr.get("state", "open")).lower()
    merged = bool(gh_pr.get("merged") or gh_pr.get("merged_at"))
    if merged:
        state = PrState.MERGED
    elif state_str == "closed":
        state = PrState.CLOSED
    else:
        state = PrState.OPEN

    user = gh_pr.get("user")
    if isinstance(user, dict) and "login" in user:
        author = str(user["login"])
    else:
        author = "ghost"

    head = gh_pr.get("head") or {}
    base = gh_pr.get("base") or {}
    head_ref = str(head.get("label") or head.get("ref") or "")
    base_ref = str(base.get("ref") or "")

    return import_pr(
        repo,
        number=int(gh_pr["number"]),
        title=str(gh_pr.get("title") or ""),
        body=str(gh_pr.get("body") or ""),
        author=author,
        state=state,
        head_ref=head_ref,
        base_ref=base_ref,
        is_draft=bool(gh_pr.get("draft", False)),
        gh_pr_id=int(gh_pr["id"]),
        authored_at=gh_pr.get("created_at"),
    )
