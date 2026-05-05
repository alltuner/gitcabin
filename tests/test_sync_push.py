# ABOUTME: Tests for gitcabin.sync.push — outbound sync of local-only issues + comments.
# ABOUTME: Real bare repos in tmp_path; the gh runner is a stateful fake that records calls.

from __future__ import annotations

import json
from collections.abc import Callable
from pathlib import Path

import pytest

from gitcabin.storage.issues import (
    ISSUE_REF_PREFIX,
    LOCAL_ISSUE_REF_PREFIX,
    IssueState,
    Provenance,
    add_comment,
    close_issue,
    create_issue,
    get_synced_issue,
    list_synced_comments,
)
from gitcabin.storage.prs import (
    LOCAL_PR_REF_PREFIX,
    PR_REF_PREFIX,
    PrState,
    create_local_pr,
    get_synced_pr,
    list_local_prs,
)
from gitcabin.storage.repo import BareRepo
from gitcabin.sync.config import SyncConfig
from gitcabin.sync.gh import GhClient
from gitcabin.sync.push import branch_for_push, push_local_issues, push_local_prs


@pytest.fixture
def repo(tmp_path: Path) -> BareRepo:
    return BareRepo.open_or_init(tmp_path / "octo" / "hello.git")


@pytest.fixture
def config() -> SyncConfig:
    return SyncConfig(
        gh_owner="octo", gh_name="hello", gh_viewer_login="alice-on-github"
    )


# ---- fake gh runner ----------------------------------------------------- #


class FakeGh:
    """Stateful fake that mimics gh api against a single repo for push tests.

    Allocates GitHub numbers + ids on POST and stores them so subsequent
    PATCH / GET calls can find them. Records every call so tests can assert
    sequence + payload shape.
    """

    def __init__(
        self,
        *,
        gh_owner: str,
        gh_name: str,
        first_issue_number: int = 41,
        first_pr_number: int = 51,
    ) -> None:
        self.calls: list[tuple[list[str], str | None]] = []
        self._issues: dict[int, dict[str, object]] = {}
        self._next_number = first_issue_number
        self._next_pr_number = first_pr_number
        self._next_issue_id = 9_000_000
        self._next_pr_id = 7_000_000
        self._next_comment_id = 8_100_000_000
        self._comment_count_by_issue: dict[int, int] = {}
        self._gh_owner = gh_owner
        self._gh_name = gh_name

    def __call__(self, argv: list[str], *, stdin: str | None = None) -> str:
        self.calls.append((list(argv), stdin))
        method = self._method(argv)
        path = argv[-1]
        body = json.loads(stdin) if stdin else {}

        if method == "POST" and path == f"repos/{self._gh_owner}/{self._gh_name}/issues":
            return json.dumps(self._create_issue(body))
        if method == "POST" and path == f"repos/{self._gh_owner}/{self._gh_name}/pulls":
            return json.dumps(self._create_pr(body))
        if method == "POST" and "/issues/" in path and path.endswith("/comments"):
            issue_number = int(path.rsplit("/", 2)[-2])
            return json.dumps(self._create_comment(issue_number, body))
        if method == "PATCH" and "/issues/" in path:
            issue_number = int(path.rsplit("/", 1)[-1])
            return json.dumps(self._update_issue(issue_number, body))
        raise AssertionError(f"unhandled fake gh call: {argv} stdin={stdin!r}")

    @staticmethod
    def _method(argv: list[str]) -> str:
        if "-X" in argv:
            return argv[argv.index("-X") + 1]
        return "GET"

    def _create_issue(self, body: dict[str, object]) -> dict[str, object]:
        number = self._next_number
        self._next_number += 1
        gh_id = self._next_issue_id
        self._next_issue_id += 1
        record = {
            "number": number,
            "id": gh_id,
            "title": body.get("title", ""),
            "body": body.get("body", ""),
            "state": "open",
            "created_at": "2026-05-04T10:00:00Z",
            "user": {"login": "alice-on-github"},
        }
        self._issues[number] = record
        return record

    def _create_comment(self, issue_number: int, body: dict[str, object]) -> dict[str, object]:
        gh_id = self._next_comment_id
        self._next_comment_id += 1
        seq = self._comment_count_by_issue.get(issue_number, 0) + 1
        self._comment_count_by_issue[issue_number] = seq
        return {
            "id": gh_id,
            "body": body.get("body", ""),
            "issue_url": f"https://api.github.com/repos/{self._gh_owner}/{self._gh_name}/issues/{issue_number}",
            "created_at": f"2026-05-04T10:00:{seq:02d}Z",
            "user": {"login": "alice-on-github"},
        }

    def _update_issue(self, issue_number: int, body: dict[str, object]) -> dict[str, object]:
        record = self._issues[issue_number]
        record.update(body)
        return record

    def _create_pr(self, body: dict[str, object]) -> dict[str, object]:
        number = self._next_pr_number
        self._next_pr_number += 1
        gh_id = self._next_pr_id
        self._next_pr_id += 1
        return {
            "number": number,
            "id": gh_id,
            "title": body.get("title", ""),
            "body": body.get("body", ""),
            "state": "open",
            "draft": body.get("draft", False),
            "merged": False,
            "head": {"label": body.get("head", ""), "ref": body.get("head", "")},
            "base": {"ref": body.get("base", "")},
            "created_at": "2026-05-04T11:00:00Z",
            "user": {"login": "alice-on-github"},
            "pull_request": {"url": "x"},
        }


def _client_for(fake: FakeGh) -> GhClient:
    runner: Callable[..., str] = fake
    return GhClient(runner=runner)


# ---- happy paths -------------------------------------------------------- #


def test_push_creates_upstream_issue_and_renumbers_locally(
    repo: BareRepo, config: SyncConfig
) -> None:
    create_issue(repo, title="hello", body="world", author="david")

    fake = FakeGh(gh_owner="octo", gh_name="hello", first_issue_number=41)
    pushed = push_local_issues(repo, _client_for(fake), config)

    assert len(pushed) == 1
    assert pushed[0].number == 41
    assert pushed[0].provenance is Provenance.SYNCED_BIDIR
    assert pushed[0].author == "alice-on-github"

    # New synced ref exists at the upstream number.
    issue = get_synced_issue(repo, 41)
    assert issue is not None
    assert issue.title == "hello"
    assert issue.gh_issue_id == 9_000_000

    # Old local ref is gone.
    refs = repo.run_git("for-each-ref", LOCAL_ISSUE_REF_PREFIX)
    assert refs == ""


def test_push_includes_comments(repo: BareRepo, config: SyncConfig) -> None:
    create_issue(repo, title="t", body="b", author="david")
    add_comment(repo, number=1, body="first reply", author="david")
    add_comment(repo, number=1, body="second reply", author="david")

    fake = FakeGh(gh_owner="octo", gh_name="hello", first_issue_number=10)
    push_local_issues(repo, _client_for(fake), config)

    comments = list_synced_comments(repo, 10)
    assert [c.body for c in comments] == ["first reply", "second reply"]
    assert all(c.provenance is Provenance.SYNCED_BIDIR for c in comments)
    assert all(c.author == "alice-on-github" for c in comments)


def test_push_patches_closed_state_upstream(
    repo: BareRepo, config: SyncConfig
) -> None:
    create_issue(repo, title="t", body="", author="david")
    close_issue(repo, number=1, actor="david")

    fake = FakeGh(gh_owner="octo", gh_name="hello", first_issue_number=10)
    push_local_issues(repo, _client_for(fake), config)

    # Verify the PATCH call actually fired with state=closed.
    methods = [a[0][a[0].index("-X") + 1] for a in fake.calls if "-X" in a[0]]
    assert "PATCH" in methods
    patch_call = next(
        c
        for c in fake.calls
        if "-X" in c[0] and c[0][c[0].index("-X") + 1] == "PATCH"
    )
    assert json.loads(patch_call[1] or "{}") == {"state": "closed"}

    # Locally the issue should also be CLOSED.
    issue = get_synced_issue(repo, 10)
    assert issue is not None
    assert issue.state is IssueState.CLOSED


def test_push_skips_already_synced_issues(
    repo: BareRepo, config: SyncConfig
) -> None:
    # Create one local issue (will be pushed) and a fake synced issue
    # (already SYNCED_BIDIR; should not be re-pushed).
    create_issue(repo, title="local one", body="", author="david")

    from gitcabin.storage.issues import import_issue as imp

    imp(
        repo,
        number=99,
        title="already synced",
        body="",
        author="alice-on-github",
        state=IssueState.OPEN,
        gh_issue_id=12345,
        provenance=Provenance.SYNCED_BIDIR,
    )

    fake = FakeGh(gh_owner="octo", gh_name="hello", first_issue_number=10)
    pushed = push_local_issues(repo, _client_for(fake), config)

    # Only the local one was pushed.
    assert len(pushed) == 1
    assert pushed[0].number == 10
    # The SYNCED_BIDIR one is still there at 99, untouched.
    assert get_synced_issue(repo, 99) is not None


def test_push_returns_empty_when_no_local_issues(
    repo: BareRepo, config: SyncConfig
) -> None:
    fake = FakeGh(gh_owner="octo", gh_name="hello")
    assert push_local_issues(repo, _client_for(fake), config) == []
    assert fake.calls == []


def test_push_post_payload_contains_title_and_body(
    repo: BareRepo, config: SyncConfig
) -> None:
    create_issue(repo, title="hello", body="multiline\nbody\nwith special chars!", author="david")

    fake = FakeGh(gh_owner="octo", gh_name="hello")
    push_local_issues(repo, _client_for(fake), config)

    post = next(
        c for c in fake.calls
        if "-X" in c[0] and c[0][c[0].index("-X") + 1] == "POST"
        and c[0][-1].endswith("/issues")
    )
    payload = json.loads(post[1] or "{}")
    assert payload == {"title": "hello", "body": "multiline\nbody\nwith special chars!"}


def test_pushed_synced_ref_is_under_refs_issues_not_local(
    repo: BareRepo, config: SyncConfig
) -> None:
    create_issue(repo, title="t", body="", author="david")

    fake = FakeGh(gh_owner="octo", gh_name="hello", first_issue_number=42)
    push_local_issues(repo, _client_for(fake), config)

    # New upstream-numbered ref exists.
    sha = repo.run_git("rev-parse", f"{ISSUE_REF_PREFIX}/42").strip()
    assert sha
    # Local ref is gone.
    listing = repo.run_git("for-each-ref", LOCAL_ISSUE_REF_PREFIX).strip()
    assert listing == ""


# ---- PR push ------------------------------------------------------------ #


def test_push_local_pr_creates_upstream_and_renumbers(
    repo: BareRepo, config: SyncConfig
) -> None:
    pr = create_local_pr(
        repo,
        title="add tests",
        body="describe what was added",
        author="david",
        head_ref="david:tests",
        base_ref="main",
    )
    assert pr.provenance is Provenance.LOCAL_ONLY
    assert pr.number == 1  # first allocation from the prs counter

    fake = FakeGh(gh_owner="octo", gh_name="hello", first_pr_number=51)
    pushed = push_local_prs(repo, _client_for(fake), config)

    assert len(pushed) == 1
    assert pushed[0].number == 51
    assert pushed[0].provenance is Provenance.SYNCED_BIDIR
    assert pushed[0].author == "alice-on-github"
    assert pushed[0].head_ref == "david:tests"
    assert pushed[0].base_ref == "main"

    # Synced ref written.
    synced = get_synced_pr(repo, 51)
    assert synced is not None
    assert synced.gh_pr_id == 7_000_000

    # Local ref deleted.
    assert list_local_prs(repo) == []
    listing = repo.run_git("for-each-ref", LOCAL_PR_REF_PREFIX).strip()
    assert listing == ""


def test_push_local_pr_post_payload_contains_head_base_draft(
    repo: BareRepo, config: SyncConfig
) -> None:
    create_local_pr(
        repo,
        title="draft PR",
        body="WIP",
        author="david",
        head_ref="david:wip",
        base_ref="main",
        is_draft=True,
    )

    fake = FakeGh(gh_owner="octo", gh_name="hello", first_pr_number=10)
    push_local_prs(repo, _client_for(fake), config)

    post = next(
        c
        for c in fake.calls
        if "-X" in c[0]
        and c[0][c[0].index("-X") + 1] == "POST"
        and c[0][-1].endswith("/pulls")
    )
    payload = json.loads(post[1] or "{}")
    assert payload == {
        "title": "draft PR",
        "body": "WIP",
        "head": "david:wip",
        "base": "main",
        "draft": True,
    }


def test_push_local_pr_returns_empty_when_no_local_prs(
    repo: BareRepo, config: SyncConfig
) -> None:
    fake = FakeGh(gh_owner="octo", gh_name="hello")
    assert push_local_prs(repo, _client_for(fake), config) == []
    assert fake.calls == []


def test_push_local_pr_skips_already_synced_prs(
    repo: BareRepo, config: SyncConfig
) -> None:
    # Create one local PR and one already-synced PR (using import_pr directly).
    create_local_pr(
        repo,
        title="local one",
        body="",
        author="david",
        head_ref="david:f1",
        base_ref="main",
    )

    from gitcabin.storage.prs import import_pr as imp_pr

    imp_pr(
        repo,
        number=99,
        title="already synced",
        body="",
        author="alice-on-github",
        state=PrState.OPEN,
        head_ref="alice:f2",
        base_ref="main",
        is_draft=False,
        gh_pr_id=12345,
        provenance=Provenance.SYNCED_BIDIR,
    )

    fake = FakeGh(gh_owner="octo", gh_name="hello", first_pr_number=10)
    pushed = push_local_prs(repo, _client_for(fake), config)

    assert len(pushed) == 1
    assert pushed[0].number == 10
    assert get_synced_pr(repo, 99) is not None  # untouched


def test_pushed_pr_synced_ref_is_under_refs_prs_not_local(
    repo: BareRepo, config: SyncConfig
) -> None:
    create_local_pr(
        repo,
        title="t",
        body="",
        author="david",
        head_ref="david:f",
        base_ref="main",
    )

    fake = FakeGh(gh_owner="octo", gh_name="hello", first_pr_number=42)
    push_local_prs(repo, _client_for(fake), config)

    sha = repo.run_git("rev-parse", f"{PR_REF_PREFIX}/42").strip()
    assert sha
    listing = repo.run_git("for-each-ref", LOCAL_PR_REF_PREFIX).strip()
    assert listing == ""


# ---- branch auto-push --------------------------------------------------- #


def test_branch_for_push_strips_viewer_prefix() -> None:
    # Plain branch name passes through.
    assert branch_for_push("feature", "alice-on-github") == "feature"
    # viewer-prefixed forks resolve to the bare branch.
    assert branch_for_push("alice-on-github:feature", "alice-on-github") == "feature"


def test_branch_for_push_returns_none_for_cross_fork_or_empty() -> None:
    # Different-account prefix means the branch lives on someone else's fork —
    # gitcabin can't push there, so signal "skip" via None.
    assert branch_for_push("someone-else:feature", "alice-on-github") is None
    # Empty string defends against degenerate stored data.
    assert branch_for_push("", "alice-on-github") is None


def _seed_head(repo: BareRepo, branch: str) -> str:
    """Create refs/heads/<branch> in the bare repo pointing at an empty tree commit.

    Local-PR tests don't normally create branch refs, so the auto-push step
    is naturally skipped. This helper opts a single test into the auto-push
    path so we can assert the pusher gets called with the right args.
    """
    tree = repo.run_git("mktree", input="").strip()
    commit = repo.run_git(
        "commit-tree", tree, "-m", "seed", input=""
    ).strip()
    repo.run_git("update-ref", f"refs/heads/{branch}", commit)
    return commit


def test_push_local_pr_auto_pushes_head_branch_when_local(
    repo: BareRepo, config: SyncConfig
) -> None:
    # Set up a local PR whose head branch actually exists in the bare repo.
    _seed_head(repo, "wip")
    create_local_pr(
        repo,
        title="t",
        body="",
        author="david",
        head_ref="alice-on-github:wip",
        base_ref="main",
    )

    pushed_branches: list[dict[str, str]] = []

    def fake_push(
        _repo: BareRepo,
        /,
        *,
        gh_owner: str,
        gh_name: str,
        host: str,
        branch: str,
    ) -> None:
        pushed_branches.append(
            {"owner": gh_owner, "name": gh_name, "host": host, "branch": branch}
        )

    fake = FakeGh(gh_owner="octo", gh_name="hello")
    push_local_prs(
        repo, _client_for(fake), config, push_branch=fake_push
    )

    assert pushed_branches == [
        {"owner": "octo", "name": "hello", "host": "github.com", "branch": "wip"}
    ]


def test_push_local_pr_skips_auto_push_for_cross_fork_head(
    repo: BareRepo, config: SyncConfig
) -> None:
    # Even with a local heads/wip, a cross-fork head ref ("other:wip")
    # belongs on someone else's remote — gitcabin should not try to push it.
    _seed_head(repo, "wip")
    create_local_pr(
        repo,
        title="t",
        body="",
        author="david",
        head_ref="other:wip",
        base_ref="main",
    )

    calls: list[str] = []

    def fake_push(
        _repo: BareRepo,
        /,
        *,
        gh_owner: str,
        gh_name: str,
        host: str,
        branch: str,
    ) -> None:
        del gh_owner, gh_name, host  # not asserted in this test
        calls.append(branch)

    fake = FakeGh(gh_owner="octo", gh_name="hello")
    push_local_prs(repo, _client_for(fake), config, push_branch=fake_push)

    assert calls == []


def test_push_local_pr_skips_auto_push_when_branch_not_in_bare_repo(
    repo: BareRepo, config: SyncConfig
) -> None:
    # head_ref names a viewer-owned branch, but it doesn't exist in the bare
    # repo — the user is on the legacy "git push origin <b>" path. Skip
    # auto-push silently and let the user-managed remote branch suffice.
    create_local_pr(
        repo,
        title="t",
        body="",
        author="david",
        head_ref="alice-on-github:wip",
        base_ref="main",
    )

    calls: list[str] = []

    def fake_push(
        _repo: BareRepo,
        /,
        *,
        gh_owner: str,
        gh_name: str,
        host: str,
        branch: str,
    ) -> None:
        del gh_owner, gh_name, host  # not asserted in this test
        calls.append(branch)

    fake = FakeGh(gh_owner="octo", gh_name="hello")
    push_local_prs(repo, _client_for(fake), config, push_branch=fake_push)

    assert calls == []
