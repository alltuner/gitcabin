# ABOUTME: Outbound sync — publishes local-only issues + their comments to GitHub.
# ABOUTME: After a successful push, the issue moves from refs/issues/local/<n> to refs/issues/<gh>.

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
