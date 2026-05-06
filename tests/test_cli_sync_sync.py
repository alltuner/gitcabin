# ABOUTME: Tests for `gitcabin sync sync <local>` — orchestrated push-then-pull.
# ABOUTME: Fakes the gh runner so push and pull both execute against in-memory payloads.

from __future__ import annotations

import json
from pathlib import Path

import pytest

from gitcabin.cli import _cmd_sync
from gitcabin.config import Settings
from gitcabin.storage.issues import IssueState, create_issue, get_synced_issue
from gitcabin.storage.repo import BareRepo
from gitcabin.sync.config import SyncConfig, write_config
from gitcabin.sync.gh import GhClient


@pytest.fixture
def repo(tmp_path: Path) -> BareRepo:
    return BareRepo.open_or_init(
        (tmp_path / "data" / "projects" / "octo" / "hello").with_suffix(".git")
    )


@pytest.fixture
def settings(tmp_path: Path) -> Settings:
    return Settings(data_dir=tmp_path / "data")


@pytest.fixture
def linked_repo(repo: BareRepo) -> BareRepo:
    write_config(
        repo,
        SyncConfig(
            gh_owner="octo",
            gh_name="hello",
            gh_viewer_login="alice",
            viewer_repo_role="ADMIN",
        ),
    )
    return repo


def _runner_for(*, push_response: object, pull_responses: dict[str, object]):
    """Fake gh runner that dispatches by URL fragment.

    Push uses POST → the runner returns whatever GitHub would respond with for
    a created issue. Pull does paginated GETs against /issues, /pulls, and
    /issues/comments — the dict matches the path the GhClient asks for.
    """
    posts: list[list[str]] = []
    gets: list[list[str]] = []

    def runner(argv: list[str], **kwargs: object) -> str:
        _ = kwargs  # post_json passes stdin=...; we don't need it for these tests.
        if "POST" in argv:
            posts.append(argv)
            return json.dumps(push_response)
        gets.append(argv)
        url = argv[-1]
        for fragment, response in pull_responses.items():
            if fragment in url:
                return json.dumps(response)
        return "[]"

    return runner, posts, gets


def test_sync_runs_push_then_pull_by_default(
    linked_repo: BareRepo, settings: Settings, capsys: pytest.CaptureFixture[str]
) -> None:
    create_issue(linked_repo, title="local draft", body="", author="alice")
    runner, posts, gets = _runner_for(
        push_response={"number": 5, "id": 555},
        pull_responses={
            "/issues": [{"number": 5, "id": 555, "title": "round-tripped", "body": "",
                         "user": {"login": "alice"}, "state": "open",
                         "created_at": "2026-01-01T00:00:00Z"}],
            "/pulls": [],
            "/issues/comments": [],
        },
    )

    rc = _cmd_sync(
        settings, "octo/hello",
        push_only=False, pull_only=False, client=GhClient(runner=runner),
    )
    assert rc == 0

    # Push happened (one POST for the local draft).
    assert len(posts) == 1
    # Pull happened too (issues, pulls, comments — at least one GET per).
    assert any("/issues" in argv[-1] for argv in gets)
    assert any("/pulls" in argv[-1] for argv in gets)
    assert any("/issues/comments" in argv[-1] for argv in gets)

    out = capsys.readouterr().out
    assert "pushed 1 issues" in out
    assert "pulled 1 issues" in out


def test_sync_push_only_skips_pull(
    linked_repo: BareRepo, settings: Settings, capsys: pytest.CaptureFixture[str]
) -> None:
    create_issue(linked_repo, title="local draft", body="", author="alice")
    runner, posts, gets = _runner_for(
        push_response={"number": 5, "id": 555}, pull_responses={},
    )

    rc = _cmd_sync(
        settings, "octo/hello",
        push_only=True, pull_only=False, client=GhClient(runner=runner),
    )
    assert rc == 0
    assert len(posts) == 1
    assert gets == []

    out = capsys.readouterr().out
    assert "pushed" in out
    assert "pulled" not in out


def test_sync_pull_only_skips_push(
    linked_repo: BareRepo, settings: Settings, capsys: pytest.CaptureFixture[str]
) -> None:
    # Local-only draft that pull-only must NOT push.
    create_issue(linked_repo, title="should stay local", body="", author="alice")
    runner, posts, gets = _runner_for(
        push_response={},
        pull_responses={
            "/issues": [{"number": 9, "id": 99, "title": "from-gh", "body": "",
                         "user": {"login": "alice"}, "state": "open",
                         "created_at": "2026-01-01T00:00:00Z"}],
            "/pulls": [],
            "/issues/comments": [],
        },
    )

    rc = _cmd_sync(
        settings, "octo/hello",
        push_only=False, pull_only=True, client=GhClient(runner=runner),
    )
    assert rc == 0
    assert posts == []
    assert any("/issues" in argv[-1] for argv in gets)

    # The pulled issue landed at refs/issues/9.
    assert get_synced_issue(linked_repo, 9) is not None

    out = capsys.readouterr().out
    assert "pushed" not in out
    assert "pulled 1 issues" in out


def test_sync_returns_1_when_repo_unknown(
    settings: Settings, capsys: pytest.CaptureFixture[str]
) -> None:
    rc = _cmd_sync(
        settings, "no/such-repo",
        push_only=False, pull_only=False,
        client=GhClient(runner=lambda _argv: "[]"),
    )
    assert rc == 1
    assert "unknown local repo" in capsys.readouterr().err


def test_sync_returns_1_when_repo_unlinked(
    repo: BareRepo, settings: Settings, capsys: pytest.CaptureFixture[str]
) -> None:
    rc = _cmd_sync(
        settings, "octo/hello",
        push_only=False, pull_only=False,
        client=GhClient(runner=lambda _argv: "[]"),
    )
    assert rc == 1
    assert "is not linked" in capsys.readouterr().err
