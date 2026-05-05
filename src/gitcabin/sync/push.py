# ABOUTME: Outbound sync — publishes local-only issues + PRs to GitHub.
# ABOUTME: Issues move from refs/issues/local/<n> to refs/issues/<gh>; same for PRs.

from __future__ import annotations

import subprocess
from typing import Any, Protocol, cast

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


def _expect_dict(value: object, what: str) -> dict[str, Any]:
    """Narrow a JSON-decoded response to a dict, raising if it isn't one.

    GhClient returns `object` because json.loads is duck-typed at runtime,
    but every endpoint we hit responds with a JSON object — narrowing to
    `dict[str, Any]` lets call sites index into it without fighting the
    type checker over each value's shape. The cast is required because
    isinstance(x, dict) only narrows to `dict[Unknown, Unknown]`.
    """
    if not isinstance(value, dict):
        raise RuntimeError(f"unexpected {what}: {value!r}")
    return cast(dict[str, Any], value)


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
    created = _expect_dict(
        client.post_json(
            f"{base}/issues",
            {"title": issue.title, "body": issue.body},
        ),
        "POST /issues response",
    )
    gh_number = int(created["number"])
    gh_id = int(created["id"])
    issue_authored_at = str(created.get("created_at") or "") or None

    # 2. Push each local comment.
    pushed_comment_ids: list[tuple[int, str | None, str, str]] = []
    # Each tuple: (gh_comment_id, authored_at, original_body, original_author).
    for comment in local_comments:
        cresp = _expect_dict(
            client.post_json(
                f"{base}/issues/{gh_number}/comments",
                {"body": comment.body},
            ),
            "POST .../comments response",
        )
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


class BranchPusher(Protocol):
    """Callable that uploads a single local branch to a GitHub remote.

    Mirrors the GhClient.runner pattern — the default implementation shells
    out via `git push`; tests inject a recording fake so the suite never
    actually contacts the network.
    """

    def __call__(
        self,
        repo: BareRepo,
        /,
        *,
        gh_owner: str,
        gh_name: str,
        host: str,
        branch: str,
    ) -> None: ...


def _default_branch_pusher(
    repo: BareRepo,
    /,
    *,
    gh_owner: str,
    gh_name: str,
    host: str,
    branch: str,
) -> None:
    """Push refs/heads/<branch> from the bare repo to <host>:<owner>/<name>.

    Auth flows through `gh auth git-credential` instead of embedding the
    token in the URL — both have the same `ps`-listing exposure window, but
    the credential-helper form keeps the token out of the git argv string,
    which lands in shell history and any tracing tools watching subprocess
    invocations. The empty `helper=` first clears any system-level helpers
    (osxkeychain, manager-core) so we don't accidentally pick up a
    different account's token. Requires gh in PATH and authenticated for
    `host`, both of which are already prerequisites for the rest of sync.
    """
    url = f"https://{host}/{gh_owner}/{gh_name}.git"
    refspec = f"refs/heads/{branch}:refs/heads/{branch}"
    repo.run_git(
        "-c",
        f"credential.{url}.helper=",
        "-c",
        f"credential.{url}.helper=!gh auth git-credential",
        "push",
        url,
        refspec,
    )


def branch_for_push(head_ref: str, viewer_login: str) -> str | None:
    """Extract the bare branch name from a PR's `head_ref`, or None if we can't push it.

    `head_ref` follows GitHub's PR `head` convention:

      "branch"            same-repo PR — push refs/heads/branch.
      "<viewer>:branch"   viewer's own fork-style label — same as above.
      "<other>:branch"    cross-fork PR — branch lives on someone else's
                          fork; we have no remote for it, so skip and let
                          the user push manually (the legacy workflow).
    """
    if not head_ref:
        return None
    if ":" not in head_ref:
        return head_ref
    owner, _, branch = head_ref.partition(":")
    if owner == viewer_login:
        return branch
    return None


def _has_local_branch(repo: BareRepo, branch: str) -> bool:
    """True iff refs/heads/<branch> resolves in the bare repo.

    `git rev-parse --verify` exits 128 when the ref is missing; we treat
    that as "branch not local" rather than letting it propagate, so
    branch-on-fork-only cases skip the auto-push cleanly.
    """
    try:
        repo.run_git("rev-parse", "--verify", f"refs/heads/{branch}")
    except subprocess.CalledProcessError:
        return False
    return True


def push_local_prs(
    repo: BareRepo,
    client: GhClient,
    config: SyncConfig,
    *,
    push_branch: BranchPusher = _default_branch_pusher,
) -> list[Pr]:
    """Push every LOCAL_ONLY pull request to GitHub.

    For each PR at refs/prs/local/<n>:
      0. If the head branch lives in the local bare repo and isn't on a
         third-party fork, push it to the GitHub remote first. Skipping
         this step keeps the legacy manual `git push origin <branch>`
         workflow working unchanged for cross-fork PRs.
      1. POST it to /repos/<o>/<r>/pulls with title / body / head / base /
         draft. GitHub allocates the upstream number + id.
      2. Re-import as SYNCED_BIDIR at refs/prs/<gh_number>.
      3. Drop refs/prs/local/<n>.

    Same crash-window as push_local_issues: a failure after the upstream
    POST but before update-ref leaves the upstream PR with no local
    counterpart, and a retry would create a duplicate. Tracked at #12.
    """
    out: list[Pr] = []
    for pr in list_local_prs(repo):
        if pr.provenance is not Provenance.LOCAL_ONLY:
            continue
        out.append(_push_one_pr(repo, client, config, pr, push_branch=push_branch))
    return out


def _push_one_pr(
    repo: BareRepo,
    client: GhClient,
    config: SyncConfig,
    pr: Pr,
    *,
    push_branch: BranchPusher,
) -> Pr:
    """Execute the per-PR push protocol described in push_local_prs."""
    base = f"repos/{config.gh_owner}/{config.gh_name}"

    # 0. Push the head branch first if we own it locally. The POST in step 1
    # would otherwise 422 with "head ref does not exist" against a fresh
    # GitHub repo. Cross-fork PRs and branches that exist only on the
    # user's working tree (not the bare repo) are skipped — those are the
    # legacy manual-push cases and continue to work as before.
    branch = branch_for_push(pr.head_ref, config.gh_viewer_login)
    if branch is not None and _has_local_branch(repo, branch):
        push_branch(
            repo,
            gh_owner=config.gh_owner,
            gh_name=config.gh_name,
            host=client.host,
            branch=branch,
        )

    # 1. POST the PR upstream. GitHub's response carries the assigned number,
    # id, and timestamps we want on the SYNCED_BIDIR re-import.
    created = _expect_dict(
        client.post_json(
            f"{base}/pulls",
            {
                "title": pr.title,
                "body": pr.body,
                "head": pr.head_ref,
                "base": pr.base_ref,
                "draft": pr.is_draft,
            },
        ),
        "POST /pulls response",
    )
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
