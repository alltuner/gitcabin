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
from gitcabin.sync.pending import (
    clear_issue as clear_pending_issue,
)
from gitcabin.sync.pending import (
    clear_pr as clear_pending_pr,
)
from gitcabin.sync.pending import (
    read_pending,
    record_comment_pushed,
    record_issue_pushed,
    record_pr_pushed,
)


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

    Crash safety: each upstream side effect (issue POST, then each comment
    POST) is durably recorded into `refs/meta/sync-pending` before the next
    one runs. A retry consults that record first and skips re-POSTs for items
    GitHub already accepted, so a crash anywhere in the protocol can resume
    without double-publishing. See `gitcabin.sync.pending` for the storage
    format.

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
    """Execute the per-issue push protocol described in push_local_issues.

    Resumable: the upstream issue ID + each posted comment ID are persisted
    to refs/meta/sync-pending as they happen, so a crash mid-protocol can
    recover without re-POSTing items GitHub already accepted.
    """
    local_comments = list_comments(repo, issue.number)
    base = f"repos/{config.gh_owner}/{config.gh_name}"
    local_ref = f"{LOCAL_ISSUE_REF_PREFIX}/{issue.number}"
    pending = read_pending(repo).issues.get(local_ref)

    # 1. Create the issue upstream — unless we already did and the prior push
    # crashed before reaching the cleanup. The pending record is the durable
    # "we already paid for this slot" receipt that prevents a duplicate POST
    # on retry.
    if pending is None:
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
        record_issue_pushed(
            repo,
            local_ref,
            gh_number=gh_number,
            gh_id=gh_id,
            authored_at=issue_authored_at,
        )
    else:
        gh_number = pending.gh_number
        gh_id = pending.gh_id
        issue_authored_at = pending.authored_at

    # 2. Push each local comment, skipping those already POSTed in a prior
    # attempt. Persist each new gh_id immediately so a second crash
    # doesn't double-post the same comment.
    already_pushed = {c.local_index: c for c in (pending.comments if pending else [])}
    pushed_comment_ids: list[tuple[int, str | None, str, str]] = []
    for index, comment in enumerate(local_comments):
        prior = already_pushed.get(index)
        if prior is not None:
            pushed_comment_ids.append(
                (prior.gh_id, prior.authored_at, comment.body, config.gh_viewer_login)
            )
            continue
        cresp = _expect_dict(
            client.post_json(
                f"{base}/issues/{gh_number}/comments",
                {"body": comment.body},
            ),
            "POST .../comments response",
        )
        gh_comment_id = int(cresp["id"])
        comment_authored_at = str(cresp.get("created_at") or "") or None
        record_comment_pushed(
            repo,
            local_ref,
            local_index=index,
            gh_id=gh_comment_id,
            authored_at=comment_authored_at,
        )
        pushed_comment_ids.append(
            (gh_comment_id, comment_authored_at, comment.body, config.gh_viewer_login)
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
    repo.run_git("update-ref", "-d", local_ref)

    # 7. Clear the pending record — the protocol is complete for this issue.
    clear_pending_issue(repo, local_ref)

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
        "--",
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

    Crash safety: the upstream PR POST result (gh_number, gh_id, created_at)
    is durably recorded into `refs/meta/sync-pending` before any further
    work runs. A retry consults that record first and skips the re-POST for
    PRs GitHub already accepted, so a crash between the POST and the local
    re-import can resume without double-publishing. See
    `gitcabin.sync.pending` for the storage format.
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
    """Execute the per-PR push protocol described in push_local_prs.

    Resumable: the upstream PR id is persisted to refs/meta/sync-pending
    immediately after the POST returns, so a crash before the local
    re-import can recover without re-POSTing the PR upstream.
    """
    base = f"repos/{config.gh_owner}/{config.gh_name}"
    local_ref = f"{LOCAL_PR_REF_PREFIX}/{pr.number}"
    pending = read_pending(repo).prs.get(local_ref)

    # 0. Push the head branch first if we own it locally. The POST in step 1
    # would otherwise 422 with "head ref does not exist" against a fresh
    # GitHub repo. Cross-fork PRs and branches that exist only on the
    # user's working tree (not the bare repo) are skipped — those are the
    # legacy manual-push cases and continue to work as before.
    #
    # On retry (pending is set) we skip the branch push too: GitHub already
    # accepted the PR, which means it already saw the head branch — there's
    # nothing left to upload. Re-pushing would be a redundant network call.
    if pending is None:
        branch = branch_for_push(pr.head_ref, config.gh_viewer_login)
        if branch is not None and _has_local_branch(repo, branch):
            push_branch(
                repo,
                gh_owner=config.gh_owner,
                gh_name=config.gh_name,
                host=client.host,
                branch=branch,
            )

    # 1. POST the PR upstream — unless we already did and the prior push
    # crashed before reaching the cleanup. The pending record is the durable
    # "we already paid for this slot" receipt that prevents a duplicate POST
    # on retry.
    if pending is None:
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
        record_pr_pushed(
            repo,
            local_ref,
            gh_number=gh_number,
            gh_id=gh_id,
            authored_at=pr_authored_at,
        )
    else:
        gh_number = pending.gh_number
        gh_id = pending.gh_id
        pr_authored_at = pending.authored_at

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
    repo.run_git("update-ref", "-d", local_ref)

    # 4. Clear the pending record — the protocol is complete for this PR.
    clear_pending_pr(repo, local_ref)

    return pushed
