# ABOUTME: Tests for gitcabin.sync.config — the per-repo SyncConfig stored at refs/meta/sync.
# ABOUTME: Roundtrip via real bare repos in tmp_path; no fakes.

from __future__ import annotations

from pathlib import Path

import pytest
from pydantic import ValidationError

from gitcabin.storage.repo import BareRepo
from gitcabin.sync.config import SYNC_REF, SyncConfig, read_config, write_config


@pytest.fixture
def repo(tmp_path: Path) -> BareRepo:
    return BareRepo.open_or_init(tmp_path / "octo" / "hello.git")


def test_read_config_returns_none_for_unconfigured_repo(repo: BareRepo) -> None:
    assert read_config(repo) is None


def test_write_then_read_roundtrip(repo: BareRepo) -> None:
    config = SyncConfig(gh_owner="octo", gh_name="hello", gh_viewer_login="alice")
    write_config(repo, config)

    reloaded = read_config(repo)
    assert reloaded == config


def test_write_creates_meta_sync_ref(repo: BareRepo) -> None:
    write_config(
        repo, SyncConfig(gh_owner="octo", gh_name="hello", gh_viewer_login="alice")
    )

    sha = repo.run_git("rev-parse", SYNC_REF).strip()
    assert sha


def test_write_appends_commit_to_meta_sync_history(repo: BareRepo) -> None:
    write_config(repo, SyncConfig(gh_owner="a", gh_name="x", gh_viewer_login="alice"))
    write_config(repo, SyncConfig(gh_owner="b", gh_name="y", gh_viewer_login="alice"))

    count = int(repo.run_git("rev-list", "--count", SYNC_REF).strip())
    assert count == 2


def test_write_replaces_existing_value(repo: BareRepo) -> None:
    write_config(repo, SyncConfig(gh_owner="old", gh_name="x", gh_viewer_login="alice"))
    write_config(repo, SyncConfig(gh_owner="new", gh_name="x", gh_viewer_login="alice"))

    config = read_config(repo)
    assert config is not None
    assert config.gh_owner == "new"


def test_optional_fields_default_to_none() -> None:
    minimal = SyncConfig(gh_owner="x", gh_name="y", gh_viewer_login="z")
    assert minimal.last_synced_at is None
    assert minimal.viewer_repo_role is None


def test_config_loads_legacy_payload_with_extra_fields_ignored() -> None:
    legacy = (
        '{"gh_owner": "x", "gh_name": "y", "gh_viewer_login": "z", '
        '"future_field": "ignored"}'
    )
    config = SyncConfig.model_validate_json(legacy)
    assert config.gh_owner == "x"


def test_round_trip_preserves_optional_fields(repo: BareRepo) -> None:
    config = SyncConfig(
        gh_owner="octo",
        gh_name="hello",
        gh_viewer_login="alice",
        last_synced_at="2026-05-04T12:00:00Z",
        viewer_repo_role="ADMIN",
    )
    write_config(repo, config)

    reloaded = read_config(repo)
    assert reloaded is not None
    assert reloaded.last_synced_at == "2026-05-04T12:00:00Z"
    assert reloaded.viewer_repo_role == "ADMIN"


def test_sync_config_rejects_dotdot_owner() -> None:
    with pytest.raises(ValidationError):
        SyncConfig(gh_owner="..", gh_name="hello", gh_viewer_login="alice")


def test_sync_config_rejects_dotdot_name() -> None:
    with pytest.raises(ValidationError):
        SyncConfig(gh_owner="octocat", gh_name="..", gh_viewer_login="alice")


def test_sync_config_rejects_slash_in_owner() -> None:
    with pytest.raises(ValidationError):
        SyncConfig(gh_owner="../etc/passwd", gh_name="hello", gh_viewer_login="alice")


def test_sync_config_accepts_valid_segments() -> None:
    config = SyncConfig(gh_owner="octo-cat.1", gh_name="my_repo", gh_viewer_login="alice")
    assert config.gh_owner == "octo-cat.1"
