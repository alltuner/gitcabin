# ABOUTME: Tests for the BareRepo handle that wraps a bare git directory.
# ABOUTME: Real repos in tmp_path; never mocks of git plumbing.

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from testgit.storage.repo import BareRepo


def _is_bare(path: Path) -> bool:
    out = subprocess.check_output(["git", "rev-parse", "--is-bare-repository"], cwd=path, text=True)
    return out.strip() == "true"


def test_open_or_init_creates_bare_repo_when_absent(tmp_path: Path) -> None:
    repo_path = tmp_path / "octocat" / "hello.git"
    assert not repo_path.exists()

    repo = BareRepo.open_or_init(repo_path)

    assert repo.path == repo_path
    assert repo_path.is_dir()
    assert _is_bare(repo_path)


def test_open_or_init_is_idempotent(tmp_path: Path) -> None:
    repo_path = tmp_path / "octocat" / "hello.git"
    BareRepo.open_or_init(repo_path)
    # A second call must not reinitialize or wipe state — git init on an
    # existing repo is normally a no-op, but we want the contract to be
    # explicit so callers can use this without guarding against existence.
    repo = BareRepo.open_or_init(repo_path)
    assert _is_bare(repo.path)


def test_run_git_executes_inside_repo(tmp_path: Path) -> None:
    repo = BareRepo.open_or_init(tmp_path / "owner" / "name.git")
    # rev-parse --git-dir on a bare repo prints "." when run inside the
    # repo directory; this proves the cwd is set correctly.
    out = repo.run_git("rev-parse", "--git-dir")
    assert out.strip() == "."


def test_open_refuses_non_bare_repo(tmp_path: Path) -> None:
    non_bare = tmp_path / "regular"
    subprocess.check_call(["git", "init", "--quiet", str(non_bare)])
    with pytest.raises(ValueError, match="not a bare"):
        BareRepo.open_or_init(non_bare)
