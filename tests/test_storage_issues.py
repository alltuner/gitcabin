# ABOUTME: Tests for the issue writer that persists each create as a real ref.
# ABOUTME: Real bare repos in tmp_path; commits are inspected via plain git plumbing.

from __future__ import annotations

import json
from pathlib import Path

import pytest

from testgit.storage.issues import Issue, IssueState, create_issue, get_issue, list_issues
from testgit.storage.repo import BareRepo


@pytest.fixture
def repo(tmp_path: Path) -> BareRepo:
    return BareRepo.open_or_init(tmp_path / "octocat" / "hello.git")


def test_create_issue_returns_issue_with_number_one(repo: BareRepo) -> None:
    issue = create_issue(repo, title="First", body="Hello", author="david")
    assert isinstance(issue, Issue)
    assert issue.number == 1
    assert issue.title == "First"
    assert issue.body == "Hello"
    assert issue.author == "david"
    assert issue.state is IssueState.OPEN


def test_create_issue_increments_number(repo: BareRepo) -> None:
    a = create_issue(repo, title="a", body="", author="david")
    b = create_issue(repo, title="b", body="", author="david")
    c = create_issue(repo, title="c", body="", author="david")
    assert [a.number, b.number, c.number] == [1, 2, 3]


def test_create_issue_writes_local_ref(repo: BareRepo) -> None:
    issue = create_issue(repo, title="ref check", body="", author="david")
    # Refs for not-yet-synced issues live under refs/issues/local/<n>; the
    # bare ref refs/issues/<n> is reserved for issues whose number is
    # authoritative (i.e. mirrored from upstream GitHub later on).
    sha = repo.run_git("rev-parse", f"refs/issues/local/{issue.number}").strip()
    assert sha, "refs/issues/local/<n> must point to a commit"


def test_create_issue_tree_contains_issue_json(repo: BareRepo) -> None:
    issue = create_issue(repo, title="Title", body="Body text", author="david")
    raw = repo.run_git("cat-file", "-p", f"refs/issues/local/{issue.number}:issue.json")
    payload = json.loads(raw)
    # Number is intentionally absent — it lives only in the ref name so a
    # future GitHub-authoritative renumbering on sync is a single ref move.
    assert payload == {
        "title": "Title",
        "body": "Body text",
        "author": "david",
        "state": "OPEN",
    }


def test_create_issue_commit_carries_author_metadata(repo: BareRepo) -> None:
    # The commit's author identity comes from the issue's author. This is
    # what makes `git log refs/issues/local/N` readable as an event log:
    # each entry shows who did what at what time.
    issue = create_issue(repo, title="t", body="b", author="alice")
    author_line = repo.run_git(
        "log", "-1", "--format=%an <%ae>", f"refs/issues/local/{issue.number}"
    ).strip()
    assert author_line.startswith("alice <")


def test_list_issues_returns_empty_for_fresh_repo(repo: BareRepo) -> None:
    assert list_issues(repo) == []


def test_list_issues_returns_all_creates_sorted_by_number(repo: BareRepo) -> None:
    create_issue(repo, title="first", body="", author="david")
    create_issue(repo, title="second", body="", author="david")
    create_issue(repo, title="third", body="", author="david")

    issues = list_issues(repo)
    assert [i.number for i in issues] == [1, 2, 3]
    assert [i.title for i in issues] == ["first", "second", "third"]


def test_list_issues_preserves_state_and_author(repo: BareRepo) -> None:
    create_issue(repo, title="t", body="b", author="alice")
    [issue] = list_issues(repo)
    assert isinstance(issue, Issue)
    assert issue.author == "alice"
    assert issue.state is IssueState.OPEN
    assert issue.body == "b"


def test_get_issue_returns_the_named_issue(repo: BareRepo) -> None:
    create_issue(repo, title="one", body="", author="david")
    create_issue(repo, title="two", body="", author="david")

    issue = get_issue(repo, 2)
    assert issue is not None
    assert issue.number == 2
    assert issue.title == "two"


def test_get_issue_returns_none_for_unknown_number(repo: BareRepo) -> None:
    assert get_issue(repo, 999) is None


def test_get_issue_returns_none_when_repo_has_no_issues(repo: BareRepo) -> None:
    assert get_issue(repo, 1) is None


def test_legacy_issue_json_with_extra_number_field_loads(repo: BareRepo) -> None:
    # Older writes embedded `number` inside issue.json. We've stopped writing
    # it, but existing data must keep loading — the IssueDocument model uses
    # extra='ignore' specifically to keep the on-disk format forward-compatible.
    legacy_payload = json.dumps(
        {
            "number": 7,
            "title": "Legacy",
            "body": "from older format",
            "author": "david",
            "state": "OPEN",
        },
        indent=2,
    )
    blob_sha = repo.run_git("hash-object", "-w", "--stdin", input=legacy_payload + "\n").strip()
    tree_sha = repo.run_git("mktree", input=f"100644 blob {blob_sha}\tissue.json\n").strip()
    commit_sha = repo.run_git(
        "-c",
        "user.name=test",
        "-c",
        "user.email=test@example.com",
        "commit-tree",
        tree_sha,
        "-m",
        "synthetic legacy",
    ).strip()
    repo.run_git("update-ref", "refs/issues/local/7", commit_sha)

    issue = get_issue(repo, 7)
    assert issue is not None
    assert issue.title == "Legacy"
    assert issue.body == "from older format"


def test_issue_carries_iso_timestamps(repo: BareRepo) -> None:
    # gh's IssueList query selects updatedAt; gh issue view also wants
    # createdAt. Both come from git commit metadata as ISO-8601 strings,
    # which need to round-trip through GraphQL untouched.
    issue = create_issue(repo, title="t", body="", author="david")
    assert issue.created_at
    assert issue.updated_at
    # ISO-8601 always starts with 4-digit year and a dash.
    assert issue.created_at[:5].endswith("-")
    # Today's create has only one event, so created_at == updated_at.
    assert issue.created_at == issue.updated_at
