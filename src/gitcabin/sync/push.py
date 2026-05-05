# ABOUTME: Outbound sync — publishes local-only issues + PRs to GitHub.
# ABOUTME: Issues move from refs/issues/local/<n> to refs/issues/<gh>; same for PRs.

from __future__ import annotations

from gitcabin.storage.issues import (
    LOCAL_ISSUE_REF_PREFIX,
    Issue,
    IssueState,
    Provenance,
    import_comment,
    import_issue,
    list_comments,
    list_issues,
)
from gitcabin.storage.prs import (
    LOCAL_PR_REF_PREFIX,
    Pr,
    import_pr,
    list_local_prs,
)
from gitcabin.storage.repo import BareRepo
from gitcabin.sync.config import SyncConfig
from gitcabin.sync.gh import GhClient


def push_local_issues(repo: BareRepo, client: GhClient, config: SyncConfig) -> list[Issue]:
    """Push every LOCAL_ONLY issue (and its comments) to GitHub.

    For each issue at refs/issues/local/<n>:

      1. POST it to /repos/<o>/<r>/issues to claim a GitHub number + id.
      2. POST each of its comments to .../issues/<n>/comments.
      3. PATCH the issue closed upstream if state was CLOSED.
      4. Re-import as SYNCED_BIDIR at refs/issues/<gh_number>.
      5. Re-import each comment under the new ref with its gh_comment_id.
      6. Delete refs/issues/local/<n>.

    Author identity post-push is taken from `config.gh_viewer_login` —
    that's the GitHub-side login whose tokens gh actually used to create
    the upstream items, so the local `author` field is rewritten to match.

    A crash between steps 1 and 6 leaves a partial state where GitHub has
    the issue but the local ref hasn't been retired. A retry would create
    a second upstream issue (duplicate). Making this resumable is on the
    sync-state-tracking work in a later commit; for now it's a known
    limitation.

    Returns the list of pushed issues in their post-push (SYNCED_BIDIR) form.
    """
    out: list[Issue] = []
    for issue in list_issues(repo):
        if issue.provenance is not Provenance.LOCAL_ONLY:
            continue
        out.append(_push_one(repo, client, config, issue))
    return out


def _push_one(
    repo: BareRepo, client: GhClient, config: SyncConfig, issue: Issue
) -> Issue:
    """Execute the per-issue push protocol described in push_local_issues."""
    local_comments = list_comments(repo, issue.number)
    base = f"repos/{config.gh_owner}/{config.gh_name}"

    # 1. Create the issue upstream.
    created = client.post_json(
        f"{base}/issues",
        {"title": issue.title, "body": issue.body},
    )
    if not isinstance(created, dict):
        raise RuntimeError(f"unexpected POST /issues response: {created!r}")
    gh_number = int(created["number"])
    gh_id = int(created["id"])
    issue_authored_at = str(created.get("created_at") or "") or None

    # 2. Push each local comment.
    pushed_comment_ids: list[tuple[int, str | None, str, str]] = []
    # Each tuple: (gh_comment_id, authored_at, original_body, original_author).
    for comment in local_comments:
        cresp = client.post_json(
            f"{base}/issues/{gh_number}/comments",
            {"body": comment.body},
        )
        if not isinstance(cresp, dict):
            raise RuntimeError(f"unexpected POST .../comments response: {cresp!r}")
        pushed_comment_ids.append(
            (
                int(cresp["id"]),
                str(cresp.get("created_at") or "") or None,
                comment.body,
                config.gh_viewer_login,
            )
        )

    # 3. Close upstream if the local state was CLOSED — POST always creates open.
    if issue.state is IssueState.CLOSED:
        client.patch_json(f"{base}/issues/{gh_number}", {"state": "closed"})

    # 4. Re-import locally under the upstream number with SYNCED_BIDIR provenance.
    pushed_issue = import_issue(
        repo,
        number=gh_number,
        title=issue.title,
        body=issue.body,
        author=config.gh_viewer_login,
        state=issue.state,
        gh_issue_id=gh_id,
        provenance=Provenance.SYNCED_BIDIR,
        authored_at=issue_authored_at,
    )

    # 5. Re-import each comment under the new ref.
    for gh_comment_id, authored_at, body, author in pushed_comment_ids:
        import_comment(
            repo,
            issue_number=gh_number,
            body=body,
            author=author,
            gh_comment_id=gh_comment_id,
            provenance=Provenance.SYNCED_BIDIR,
            authored_at=authored_at,
        )

    # 6. Drop the old local ref now that it's mirrored upstream + locally.
    repo.run_git("update-ref", "-d", f"{LOCAL_ISSUE_REF_PREFIX}/{issue.number}")

    return pushed_issue


# ---- PR push ------------------------------------------------------------ //


def push_local_prs(repo: BareRepo, client: GhClient, config: SyncConfig) -> list[Pr]:
    """Push every LOCAL_ONLY pull request to GitHub.

    For each PR at refs/prs/local/<n>:
      1. POST it to /repos/<o>/<r>/pulls with title / body / head / base /
         draft. GitHub allocates the upstream number + id.
      2. Re-import as SYNCED_BIDIR at refs/prs/<gh_number>.
      3. Drop refs/prs/local/<n>.

    *Prerequisite:* the head branch must already exist on the GitHub repo.
    gitcabin doesn't push code, only metadata. POST /pulls returns 422 if
    GitHub can't resolve `head` to a real ref. The simplest workflow today
    is `git push origin <branch>` from the user's working tree before
    invoking this push — see docs/github-sync.md for the rationale.

    Same crash-window as push_local_issues: a failure after the upstream
    POST but before update-ref leaves the upstream PR with no local
    counterpart, and a retry would create a duplicate. Tracked at #12.
    """
    out: list[Pr] = []
    for pr in list_local_prs(repo):
        if pr.provenance is not Provenance.LOCAL_ONLY:
            continue
        out.append(_push_one_pr(repo, client, config, pr))
    return out


def _push_one_pr(
    repo: BareRepo, client: GhClient, config: SyncConfig, pr: Pr
) -> Pr:
    """Execute the per-PR push protocol described in push_local_prs."""
    base = f"repos/{config.gh_owner}/{config.gh_name}"

    # 1. POST the PR upstream. GitHub's response carries the assigned number,
    # id, and timestamps we want on the SYNCED_BIDIR re-import.
    created = client.post_json(
        f"{base}/pulls",
        {
            "title": pr.title,
            "body": pr.body,
            "head": pr.head_ref,
            "base": pr.base_ref,
            "draft": pr.is_draft,
        },
    )
    if not isinstance(created, dict):
        raise RuntimeError(f"unexpected POST /pulls response: {created!r}")
    gh_number = int(created["number"])
    gh_id = int(created["id"])
    pr_authored_at = str(created.get("created_at") or "") or None

    # 2. Re-import as SYNCED_BIDIR under the upstream number. PR comments
    # aren't part of this protocol — they live on the issue/comment side
    # and are handled by push_local_issues / pull_comments. A local PR's
    # body is the only "comment-shaped" content we own.
    pushed = import_pr(
        repo,
        number=gh_number,
        title=pr.title,
        body=pr.body,
        author=config.gh_viewer_login,
        state=pr.state,
        head_ref=pr.head_ref,
        base_ref=pr.base_ref,
        is_draft=pr.is_draft,
        gh_pr_id=gh_id,
        provenance=Provenance.SYNCED_BIDIR,
        authored_at=pr_authored_at,
    )

    # 3. Drop the local ref.
    repo.run_git("update-ref", "-d", f"{LOCAL_PR_REF_PREFIX}/{pr.number}")

    return pushed
