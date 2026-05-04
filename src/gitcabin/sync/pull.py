# ABOUTME: Inbound sync — pulls issues from GitHub into refs/issues/<gh_number>.
# ABOUTME: Comments are pulled in a later step; this module handles issue-level data only.

from __future__ import annotations

from typing import Any

from gitcabin.storage.issues import Issue, IssueState, import_issue
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
