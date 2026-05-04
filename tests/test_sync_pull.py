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
    import_issue,
)
from gitcabin.storage.repo import BareRepo
from gitcabin.sync.config import SyncConfig
from gitcabin.sync.gh import GhClient
from gitcabin.sync.pull import pull_issues


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
    number: int, title: str, body: str, login: str, gh_id: int, state: str = "open"
) -> dict[str, object]:
    return {
        "number": number,
        "id": gh_id,
        "title": title,
        "body": body,
        "user": {"login": login},
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
