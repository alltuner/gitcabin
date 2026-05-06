# ABOUTME: Perf-shape tests for storage helpers — assert structural call counts.
# ABOUTME: Guards against regressions where a list-walk reverts to per-item subprocesses.

from __future__ import annotations

from pathlib import Path

import pytest

from gitcabin.storage.issues import (
    IssueState,
    add_comment,
    close_issue,
    create_issue,
    import_issue,
    issue_counts,
    list_comments,
)
from gitcabin.storage.repo import BareRepo


@pytest.fixture
def repo(tmp_path: Path) -> BareRepo:
    return BareRepo.open_or_init(tmp_path / "octocat" / "hello.git")


def _wrap_run_git(
    monkeypatch: pytest.MonkeyPatch, repo: BareRepo
) -> list[tuple[str, ...]]:
    """Patch `BareRepo.run_git` to record positional-arg tuples and delegate.

    BareRepo is a frozen+slotted dataclass, so we can't rebind on the
    instance — we patch at the class level via monkeypatch instead, which
    pytest restores at teardown.
    """
    calls: list[tuple[str, ...]] = []
    original = BareRepo.run_git

    def wrapper(self: BareRepo, *args: str, input: str | None = None) -> str:
        calls.append(args)
        return original(self, *args, input=input)

    monkeypatch.setattr(BareRepo, "run_git", wrapper)
    return calls


def test_issue_counts_returns_total_and_open(repo: BareRepo) -> None:
    create_issue(repo, title="open-1", body="", author="alice")
    create_issue(repo, title="open-2", body="", author="alice")
    third = create_issue(repo, title="will-close", body="", author="alice")
    close_issue(repo, number=third.number, actor="alice")
    import_issue(
        repo,
        number=42,
        title="synced",
        body="",
        author="alice",
        state=IssueState.CLOSED,
        gh_issue_id=900,
    )

    total, open_count = issue_counts(repo)
    assert total == 4
    assert open_count == 2


def test_issue_counts_does_not_shell_out(
    repo: BareRepo, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The total comes from ref names alone; open-state from GitPython object loads.

    Either way, no `git` subprocess should be spawned — every shell-out is a
    fork+exec that we paid for on every chrome page render before this change.
    """
    for _ in range(5):
        create_issue(repo, title="t", body="", author="alice")

    calls = _wrap_run_git(monkeypatch, repo)
    total, open_count = issue_counts(repo)
    assert total == 5
    assert open_count == 5
    assert calls == []


def test_list_comments_uses_one_git_log_call_for_all_comments(
    repo: BareRepo, monkeypatch: pytest.MonkeyPatch
) -> None:
    issue = create_issue(repo, title="t", body="", author="alice")
    for i in range(5):
        add_comment(repo, number=issue.number, body=f"c{i}", author="alice")

    calls = _wrap_run_git(monkeypatch, repo)
    comments = list_comments(repo, issue.number)
    assert len(comments) == 5

    # Exactly one `git log` invocation regardless of comment count — was N
    # before (one `comment_created_at` per comment).
    log_calls = [c for c in calls if c and c[0] == "log"]
    assert len(log_calls) == 1, f"expected 1 git-log call, got {log_calls}"


def test_list_comments_preserves_authored_at_timestamps(repo: BareRepo) -> None:
    """The bulk walk must produce the same ISO-8601 dates the per-file lookup did."""
    issue = create_issue(repo, title="t", body="", author="alice")
    a = add_comment(repo, number=issue.number, body="first", author="alice")
    b = add_comment(repo, number=issue.number, body="second", author="alice")
    c = add_comment(repo, number=issue.number, body="third", author="alice")

    listed = list_comments(repo, issue.number)
    assert {c.number: c.created_at for c in listed} == {
        1: a.created_at,
        2: b.created_at,
        3: c.created_at,
    }
    # Sanity: every timestamp is a non-empty ISO string.
    for comment in listed:
        assert comment.created_at
        assert "T" in comment.created_at
