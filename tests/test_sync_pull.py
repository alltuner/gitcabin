# ABOUTME: Tests for gitcabin.sync.pull — inbound sync of issues from GitHub.
# ABOUTME: Real bare repos in tmp_path; gh runner faked so no network.

from __future__ import annotations

import json
from pathlib import Path

import pytest

from gitcabin.storage.issues import (
    ISSUE_REF_PREFIX,
    IssueDocument,
    IssueState,
    Provenance,
    get_synced_issue,
    import_comment,
    import_issue,
    list_synced_comments,
)
from gitcabin.storage.prs import (
    PrState,
    get_synced_pr,
    import_pr,
    list_synced_pr_comments,
    list_synced_prs,
)
from gitcabin.storage.repo import BareRepo
from gitcabin.sync.config import SyncConfig
from gitcabin.sync.gh import GhClient
from gitcabin.sync.pull import pull_comments, pull_issues, pull_prs


@pytest.fixture
def repo(tmp_path: Path) -> BareRepo:
    return BareRepo.open_or_init(tmp_path / "octo" / "hello.git")


@pytest.fixture
def config() -> SyncConfig:
    return SyncConfig(gh_owner="octo", gh_name="hello", gh_viewer_login="alice")


# ---- import_issue (storage primitive) ----------------------------------- #


def test_import_issue_writes_to_refs_issues_not_local(repo: BareRepo) -> None:
    import_issue(
        repo,
        number=42,
        title="t",
        body="b",
        author="alice",
        state=IssueState.OPEN,
        gh_issue_id=999000,
    )
    sha = repo.run_git("rev-parse", f"{ISSUE_REF_PREFIX}/42").strip()
    assert sha
    # Local namespace must be untouched.
    result = repo.run_git("for-each-ref", "refs/issues/local/")
    assert "42" not in result


def test_import_issue_persists_synced_provenance_and_gh_id(repo: BareRepo) -> None:
    import_issue(
        repo,
        number=42,
        title="t",
        body="b",
        author="alice",
        state=IssueState.OPEN,
        gh_issue_id=999000,
    )
    raw = repo.run_git("cat-file", "-p", f"{ISSUE_REF_PREFIX}/42:issue.json")
    payload = json.loads(raw)
    assert payload["provenance"] == "SYNCED_FROM_GITHUB"
    assert payload["gh_issue_id"] == 999000


def test_get_synced_issue_returns_none_when_absent(repo: BareRepo) -> None:
    assert get_synced_issue(repo, 1) is None


def test_get_synced_issue_returns_issue_after_import(repo: BareRepo) -> None:
    import_issue(
        repo,
        number=42,
        title="t",
        body="b",
        author="alice",
        state=IssueState.OPEN,
        gh_issue_id=999000,
    )
    issue = get_synced_issue(repo, 42)
    assert issue is not None
    assert issue.number == 42
    assert issue.provenance is Provenance.SYNCED_FROM_GITHUB
    assert issue.gh_issue_id == 999000


def test_re_import_replaces_issue_json_but_preserves_other_tree_entries(
    repo: BareRepo,
) -> None:
    # First import: just issue.json.
    import_issue(
        repo,
        number=42,
        title="t1",
        body="b1",
        author="alice",
        state=IssueState.OPEN,
        gh_issue_id=999000,
    )
    # Splice a comments/ subtree onto the ref to simulate a later commit
    # adding comments — what commit 5 (pull comments) will do.
    blob_sha = repo.run_git(
        "hash-object", "-w", "--stdin", input='{"body": "x", "author": "y"}\n'
    ).strip()
    sub_sha = repo.run_git(
        "mktree", input=f"100644 blob {blob_sha}\t0001.json\n"
    ).strip()
    issue_blob_sha = repo.run_git(
        "rev-parse", f"{ISSUE_REF_PREFIX}/42:issue.json"
    ).strip()
    new_top = repo.run_git(
        "mktree",
        input=(
            f"100644 blob {issue_blob_sha}\tissue.json\n"
            f"040000 tree {sub_sha}\tcomments\n"
        ),
    ).strip()
    parent = repo.run_git("rev-parse", f"{ISSUE_REF_PREFIX}/42").strip()
    new_tip = repo.run_git(
        "-c",
        "user.name=test",
        "-c",
        "user.email=test@example.com",
        "commit-tree",
        new_top,
        "-p",
        parent,
        "-m",
        "synthetic comments add",
    ).strip()
    repo.run_git("update-ref", f"{ISSUE_REF_PREFIX}/42", new_tip)

    # Re-import with a new title.
    import_issue(
        repo,
        number=42,
        title="t2",
        body="b2",
        author="alice",
        state=IssueState.CLOSED,
        gh_issue_id=999000,
    )

    # issue.json reflects the new content.
    raw = repo.run_git("cat-file", "-p", f"{ISSUE_REF_PREFIX}/42:issue.json")
    doc = IssueDocument.model_validate_json(raw)
    assert doc.title == "t2"
    assert doc.state is IssueState.CLOSED

    # comments/ subtree is preserved.
    listing = repo.run_git("ls-tree", "-r", f"{ISSUE_REF_PREFIX}/42")
    assert "comments/0001.json" in listing


def test_import_issue_with_authored_at_sets_commit_date(repo: BareRepo) -> None:
    import_issue(
        repo,
        number=42,
        title="t",
        body="b",
        author="alice",
        state=IssueState.OPEN,
        gh_issue_id=999,
        authored_at="2025-01-15T12:34:56Z",
    )
    iso = repo.run_git("log", "-1", "--format=%aI", f"{ISSUE_REF_PREFIX}/42").strip()
    # `git log` normalizes Z → +00:00 but keeps the same instant.
    assert iso.startswith("2025-01-15T12:34:56")


# ---- pull_issues (sync layer) ------------------------------------------- #


def _issue_payload(
    number: int,
    title: str,
    body: str,
    login: str,
    gh_id: int,
    state: str = "open",
    user_id: int | None = None,
) -> dict[str, object]:
    user: dict[str, object] = {"login": login}
    if user_id is not None:
        user["id"] = user_id
    return {
        "number": number,
        "id": gh_id,
        "title": title,
        "body": body,
        "user": user,
        "state": state,
        "created_at": "2025-01-01T00:00:00Z",
    }


def test_pull_issues_imports_each_issue_at_its_gh_number(
    repo: BareRepo, config: SyncConfig
) -> None:
    issues = [
        _issue_payload(1, "first", "body 1", "alice", gh_id=11),
        _issue_payload(7, "seventh", "body 7", "bob", gh_id=77, state="closed"),
    ]

    def runner(argv: list[str]) -> str:
        assert "repos/octo/hello/issues" in argv[-1]
        return json.dumps(issues)

    pulled = pull_issues(repo, GhClient(runner=runner), config)

    assert {i.number for i in pulled} == {1, 7}
    one = get_synced_issue(repo, 1)
    seven = get_synced_issue(repo, 7)
    assert one is not None and seven is not None
    assert one.title == "first"
    assert one.author == "alice"
    assert one.state is IssueState.OPEN
    assert seven.state is IssueState.CLOSED


def test_pull_issues_skips_pull_request_entries(
    repo: BareRepo, config: SyncConfig
) -> None:
    payload = [
        _issue_payload(1, "real issue", "", "alice", gh_id=11),
        # GitHub returns PRs from /issues with a pull_request field.
        {**_issue_payload(2, "a pr", "", "alice", gh_id=22), "pull_request": {"url": "x"}},
    ]

    def runner(argv: list[str]) -> str:
        return json.dumps(payload)

    pulled = pull_issues(repo, GhClient(runner=runner), config)

    assert [i.number for i in pulled] == [1]
    assert get_synced_issue(repo, 2) is None


def test_pull_issues_uses_paginate_and_state_all(
    repo: BareRepo, config: SyncConfig
) -> None:
    captured: list[list[str]] = []

    def runner(argv: list[str]) -> str:
        captured.append(argv)
        return "[]"

    pull_issues(repo, GhClient(runner=runner), config)

    assert "--paginate" in captured[0]
    # state=all because closed issues need to come down too.
    assert "state=all" in captured[0][-1]


def test_pull_issues_handles_null_user_as_ghost(
    repo: BareRepo, config: SyncConfig
) -> None:
    payload = [
        {
            "number": 5,
            "id": 55,
            "title": "ghost-authored",
            "body": "",
            "user": None,
            "state": "open",
            "created_at": "2025-01-01T00:00:00Z",
        }
    ]

    def runner(argv: list[str]) -> str:
        return json.dumps(payload)

    pull_issues(repo, GhClient(runner=runner), config)

    issue = get_synced_issue(repo, 5)
    assert issue is not None
    assert issue.author == "ghost"


def test_pull_issues_captures_gh_author_id_from_user_payload(
    repo: BareRepo, config: SyncConfig
) -> None:
    # GitHub's stable numeric `user.id` lets us match a user across login
    # renames. Sync persists it on the issue alongside the login string.
    payload = [_issue_payload(5, "t", "", "alice", gh_id=55, user_id=42)]

    pull_issues(repo, GhClient(runner=lambda _argv: json.dumps(payload)), config)

    issue = get_synced_issue(repo, 5)
    assert issue is not None
    assert issue.author == "alice"
    assert issue.gh_author_id == 42


def test_pull_issues_leaves_gh_author_id_none_when_user_id_missing(
    repo: BareRepo, config: SyncConfig
) -> None:
    # Older GitHub payloads (or our test fixtures) may omit `user.id`. The
    # author login still imports; the numeric id is just None.
    payload = [_issue_payload(5, "t", "", "alice", gh_id=55)]

    pull_issues(repo, GhClient(runner=lambda _argv: json.dumps(payload)), config)

    issue = get_synced_issue(repo, 5)
    assert issue is not None
    assert issue.gh_author_id is None


def test_pull_issues_re_pull_replaces_existing_data(
    repo: BareRepo, config: SyncConfig
) -> None:
    first = [_issue_payload(1, "old title", "old body", "alice", gh_id=11)]
    second = [_issue_payload(1, "new title", "new body", "alice", gh_id=11, state="closed")]

    pull_issues(repo, GhClient(runner=lambda _argv: json.dumps(first)), config)
    pull_issues(repo, GhClient(runner=lambda _argv: json.dumps(second)), config)

    issue = get_synced_issue(repo, 1)
    assert issue is not None
    assert issue.title == "new title"
    assert issue.state is IssueState.CLOSED


# ---- import_comment / list_synced_comments ----------------------------- #


def test_import_comment_returns_none_when_issue_ref_missing(repo: BareRepo) -> None:
    assert (
        import_comment(
            repo,
            issue_number=1,
            body="hi",
            author="alice",
            gh_comment_id=12345,
        )
        is None
    )


def test_import_comment_writes_blob_at_gh_id_filename(repo: BareRepo) -> None:
    import_issue(
        repo,
        number=1,
        title="t",
        body="",
        author="alice",
        state=IssueState.OPEN,
        gh_issue_id=11,
    )
    import_comment(
        repo,
        issue_number=1,
        body="from-gh",
        author="bob",
        gh_comment_id=999000111,
    )
    raw = repo.run_git(
        "cat-file", "-p", f"{ISSUE_REF_PREFIX}/1:comments/999000111.json"
    )
    payload = json.loads(raw)
    assert payload == {
        "body": "from-gh",
        "author": "bob",
        "provenance": "SYNCED_FROM_GITHUB",
        "gh_comment_id": 999000111,
        "gh_author_id": None,
    }


def test_re_import_replaces_blob_in_place(repo: BareRepo) -> None:
    import_issue(
        repo,
        number=1,
        title="t",
        body="",
        author="alice",
        state=IssueState.OPEN,
        gh_issue_id=11,
    )
    import_comment(repo, issue_number=1, body="v1", author="bob", gh_comment_id=555)
    import_comment(repo, issue_number=1, body="v2 (edited)", author="bob", gh_comment_id=555)

    comments = list_synced_comments(repo, 1)
    assert len(comments) == 1
    assert comments[0].body == "v2 (edited)"


def test_list_synced_comments_returns_each_comment_in_id_order(repo: BareRepo) -> None:
    import_issue(
        repo,
        number=1,
        title="t",
        body="",
        author="alice",
        state=IssueState.OPEN,
        gh_issue_id=11,
    )
    import_comment(repo, issue_number=1, body="second", author="x", gh_comment_id=200)
    import_comment(repo, issue_number=1, body="first", author="y", gh_comment_id=100)

    comments = list_synced_comments(repo, 1)
    assert [c.gh_comment_id for c in comments] == [100, 200]
    assert [c.body for c in comments] == ["first", "second"]
    assert all(c.provenance is Provenance.SYNCED_FROM_GITHUB for c in comments)


# ---- pull_comments ------------------------------------------------------ #


def _comment_payload(
    comment_id: int,
    issue_number: int,
    body: str,
    login: str,
    user_id: int | None = None,
) -> dict[str, object]:
    user: dict[str, object] = {"login": login}
    if user_id is not None:
        user["id"] = user_id
    return {
        "id": comment_id,
        "issue_url": f"https://api.github.com/repos/octo/hello/issues/{issue_number}",
        "body": body,
        "user": user,
        "created_at": "2025-01-02T00:00:00Z",
    }


def test_pull_comments_imports_each_comment_under_its_issue(
    repo: BareRepo, config: SyncConfig
) -> None:
    import_issue(
        repo,
        number=1,
        title="t1",
        body="",
        author="alice",
        state=IssueState.OPEN,
        gh_issue_id=11,
    )
    import_issue(
        repo,
        number=2,
        title="t2",
        body="",
        author="alice",
        state=IssueState.OPEN,
        gh_issue_id=22,
    )

    comments = [
        _comment_payload(101, 1, "first on issue 1", "bob"),
        _comment_payload(202, 2, "first on issue 2", "alice"),
        _comment_payload(102, 1, "second on issue 1", "carol"),
    ]

    def runner(argv: list[str]) -> str:
        assert "issues/comments" in argv[-1]
        return json.dumps(comments)

    pulled = pull_comments(repo, GhClient(runner=runner), config)

    assert len(pulled) == 3
    on_one = list_synced_comments(repo, 1)
    assert [c.body for c in on_one] == ["first on issue 1", "second on issue 1"]
    on_two = list_synced_comments(repo, 2)
    assert [c.body for c in on_two] == ["first on issue 2"]


def test_pull_comments_captures_gh_author_id_from_user_payload(
    repo: BareRepo, config: SyncConfig
) -> None:
    import_issue(
        repo,
        number=1,
        title="t",
        body="",
        author="alice",
        state=IssueState.OPEN,
        gh_issue_id=11,
    )
    payload = [_comment_payload(101, 1, "hi", "bob", user_id=42)]

    pull_comments(repo, GhClient(runner=lambda _argv: json.dumps(payload)), config)

    on_one = list_synced_comments(repo, 1)
    assert len(on_one) == 1
    assert on_one[0].author == "bob"
    assert on_one[0].gh_author_id == 42


def test_pull_comments_skips_comments_for_unknown_issues(
    repo: BareRepo, config: SyncConfig
) -> None:
    # Issue 99 was never imported (pull_issues ran on a different scope).
    payload = [_comment_payload(50, 99, "orphaned", "alice")]

    def runner(argv: list[str]) -> str:
        return json.dumps(payload)

    pulled = pull_comments(repo, GhClient(runner=runner), config)
    assert pulled == []


def test_pull_comments_handles_null_user_as_ghost(
    repo: BareRepo, config: SyncConfig
) -> None:
    import_issue(
        repo,
        number=1,
        title="t",
        body="",
        author="alice",
        state=IssueState.OPEN,
        gh_issue_id=11,
    )
    payload = [
        {
            "id": 999,
            "issue_url": "https://api.github.com/repos/octo/hello/issues/1",
            "body": "ghost comment",
            "user": None,
            "created_at": "2025-01-02T00:00:00Z",
        }
    ]

    def runner(argv: list[str]) -> str:
        return json.dumps(payload)

    pull_comments(repo, GhClient(runner=runner), config)

    [comment] = list_synced_comments(repo, 1)
    assert comment.author == "ghost"


def test_pull_comments_uses_paginate_flag(repo: BareRepo, config: SyncConfig) -> None:
    captured: list[list[str]] = []

    def runner(argv: list[str]) -> str:
        captured.append(argv)
        return "[]"

    pull_comments(repo, GhClient(runner=runner), config)

    assert "--paginate" in captured[0]


# ---- pull_prs ----------------------------------------------------------- #


def _pr_payload(
    number: int,
    title: str,
    login: str,
    gh_id: int,
    *,
    state: str = "open",
    merged: bool = False,
    draft: bool = False,
    head_ref: str = "feature",
    base_ref: str = "main",
) -> dict[str, object]:
    return {
        "number": number,
        "id": gh_id,
        "title": title,
        "body": "",
        "user": {"login": login},
        "state": state,
        "merged": merged,
        "merged_at": "2025-02-01T00:00:00Z" if merged else None,
        "draft": draft,
        "head": {"label": f"{login}:{head_ref}", "ref": head_ref},
        "base": {"ref": base_ref},
        "created_at": "2025-01-01T00:00:00Z",
        "pull_request": {"url": "x"},
    }


def test_pull_prs_imports_each_pr(repo: BareRepo, config: SyncConfig) -> None:
    payload = [
        _pr_payload(1, "first", "alice", 11),
        _pr_payload(7, "merged one", "bob", 77, state="closed", merged=True),
        _pr_payload(8, "draft", "alice", 88, draft=True),
    ]

    def runner(argv: list[str]) -> str:
        assert "repos/octo/hello/pulls" in argv[-1]
        return json.dumps(payload)

    pulled = pull_prs(repo, GhClient(runner=runner), config)

    assert {p.number for p in pulled} == {1, 7, 8}
    one = get_synced_pr(repo, 1)
    seven = get_synced_pr(repo, 7)
    eight = get_synced_pr(repo, 8)
    assert one is not None and seven is not None and eight is not None
    assert one.state is PrState.OPEN
    assert seven.state is PrState.MERGED
    assert eight.is_draft is True
    assert one.head_ref == "alice:feature"
    assert one.base_ref == "main"


def test_pull_prs_uses_paginate_and_state_all(
    repo: BareRepo, config: SyncConfig
) -> None:
    captured: list[list[str]] = []

    def runner(argv: list[str]) -> str:
        captured.append(argv)
        return "[]"

    pull_prs(repo, GhClient(runner=runner), config)

    assert "--paginate" in captured[0]
    assert "state=all" in captured[0][-1]


def test_pull_prs_handles_null_user_as_ghost(
    repo: BareRepo, config: SyncConfig
) -> None:
    payload = [
        {
            "number": 5,
            "id": 55,
            "title": "ghost pr",
            "body": "",
            "user": None,
            "state": "closed",
            "merged": False,
            "draft": False,
            "head": {"ref": "x"},
            "base": {"ref": "main"},
            "created_at": "2025-01-01T00:00:00Z",
        }
    ]

    def runner(argv: list[str]) -> str:
        return json.dumps(payload)

    pull_prs(repo, GhClient(runner=runner), config)

    pr = get_synced_pr(repo, 5)
    assert pr is not None
    assert pr.author == "ghost"
    assert pr.state is PrState.CLOSED


# ---- pull_comments dispatches to PR or issue --------------------------- #


def test_pull_comments_dispatches_to_pr_when_pr_ref_exists(
    repo: BareRepo, config: SyncConfig
) -> None:
    # Pre-import a PR at number 5 — comment with issue_url ending in /issues/5
    # should land in the PR namespace, not the issue namespace.
    import_pr(
        repo,
        number=5,
        title="t",
        body="",
        author="alice",
        state=PrState.OPEN,
        head_ref="x",
        base_ref="main",
        is_draft=False,
        gh_pr_id=55,
    )

    payload = [
        {
            "id": 100,
            "issue_url": "https://api.github.com/repos/octo/hello/issues/5",
            "body": "comment on a pr",
            "user": {"login": "bob"},
            "created_at": "2025-01-02T00:00:00Z",
        }
    ]

    def runner(argv: list[str]) -> str:
        return json.dumps(payload)

    pull_comments(repo, GhClient(runner=runner), config)

    pr_comments = list_synced_pr_comments(repo, 5)
    assert [c.body for c in pr_comments] == ["comment on a pr"]
    # Issue side should be empty.
    assert list_synced_comments(repo, 5) == []


def test_pull_comments_skips_when_neither_issue_nor_pr_ref_exists(
    repo: BareRepo, config: SyncConfig
) -> None:
    # No issue 7, no PR 7 — comment should be silently dropped.
    payload = [
        {
            "id": 100,
            "issue_url": "https://api.github.com/repos/octo/hello/issues/7",
            "body": "orphaned",
            "user": {"login": "x"},
            "created_at": "2025-01-02T00:00:00Z",
        }
    ]

    def runner(argv: list[str]) -> str:
        return json.dumps(payload)

    pulled = pull_comments(repo, GhClient(runner=runner), config)
    assert pulled == []
    assert list_synced_prs(repo) == []
