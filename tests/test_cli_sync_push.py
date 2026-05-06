# ABOUTME: Tests for `gitcabin sync push <local>` — CLI wrapper around push_local_issues.
# ABOUTME: Verifies last_synced_at is bumped so push-only flows don't leave the timestamp stale.

from __future__ import annotations

import json
from pathlib import Path

import pytest

from gitcabin.cli import _cmd_push
from gitcabin.config import Settings
from gitcabin.storage.issues import create_issue
from gitcabin.storage.repo import BareRepo
from gitcabin.sync.config import SyncConfig, read_config, write_config
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


def _runner_for(*, push_response: object):
    """Fake gh runner that returns push_response for any POST and [] otherwise."""

    def runner(argv: list[str], **kwargs: object) -> str:
        _ = kwargs
        if "POST" in argv:
            return json.dumps(push_response)
        return "[]"

    return runner


def test_push_updates_last_synced_at(
    linked_repo: BareRepo, settings: Settings
) -> None:
    create_issue(linked_repo, title="local draft", body="", author="alice")
    runner = _runner_for(push_response={"number": 5, "id": 555})

    before = read_config(linked_repo)
    assert before is not None

    rc = _cmd_push(settings, "octo/hello", client=GhClient(runner=runner))
    assert rc == 0

    after = read_config(linked_repo)
    assert after is not None
    assert after.last_synced_at is not None
    if before.last_synced_at is not None:
        assert after.last_synced_at > before.last_synced_at


def test_push_returns_1_when_repo_unknown(
    settings: Settings, capsys: pytest.CaptureFixture[str]
) -> None:
    rc = _cmd_push(
        settings, "no/such-repo", client=GhClient(runner=lambda _argv: "[]")
    )
    assert rc == 1
    assert "unknown local repo" in capsys.readouterr().err


def test_push_returns_1_when_repo_unlinked(
    repo: BareRepo, settings: Settings, capsys: pytest.CaptureFixture[str]
) -> None:
    rc = _cmd_push(
        settings, "octo/hello", client=GhClient(runner=lambda _argv: "[]")
    )
    assert rc == 1
    assert "is not linked" in capsys.readouterr().err
