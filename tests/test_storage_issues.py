# ABOUTME: Tests for the issue writer that persists each create as a real ref.
# ABOUTME: Real bare repos in tmp_path; commits are inspected via plain git plumbing.

from __future__ import annotations

import json
from pathlib import Path

import pytest

from testgit.storage.issues import Issue, IssueState, create_issue
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
    assert payload == {
        "number": 1,
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
