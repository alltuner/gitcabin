# ABOUTME: Inbound sync — pulls issues and their comments from GitHub.
# ABOUTME: Issues land at refs/issues/<gh_number>; comments at the same ref's comments/ subtree.

from __future__ import annotations

from typing import Any

from gitcabin.storage.issues import (
    Comment,
    Issue,
    IssueState,
    import_comment,
    import_issue,
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
    user = gh_issue.get("user")
    if isinstance(user, dict) and "login" in user:
        author = str(user["login"])
    else:
        # GitHub returns null user for items by deleted accounts ("ghost" UI).
        # Mirror that behavior with a tombstone author.
        author = "ghost"
    return import_issue(
        repo,
        number=int(gh_issue["number"]),
        title=str(gh_issue.get("title") or ""),
        body=str(gh_issue.get("body") or ""),
        author=author,
        state=state,
        gh_issue_id=int(gh_issue["id"]),
        authored_at=gh_issue.get("created_at"),
    )


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
        issue_number = _issue_number_from_url(str(item.get("issue_url", "")))
        if issue_number is None:
            continue
        result = _import_one_comment(repo, issue_number, item)
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
    repo: BareRepo, issue_number: int, gh_comment: dict[str, Any]
) -> Comment | None:
    """Translate a GitHub comment payload into a stored Comment."""
    user = gh_comment.get("user")
    if isinstance(user, dict) and "login" in user:
        author = str(user["login"])
    else:
        author = "ghost"
    return import_comment(
        repo,
        issue_number=issue_number,
        body=str(gh_comment.get("body") or ""),
        author=author,
        gh_comment_id=int(gh_comment["id"]),
        authored_at=gh_comment.get("created_at"),
    )
