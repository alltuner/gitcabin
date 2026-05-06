# ABOUTME: Per-repo sync config stored at refs/meta/sync as a single config.json blob.
# ABOUTME: Mirrors design in docs/github-sync.md — links a local repo to a GitHub repo.

from __future__ import annotations

from git import Commit
from git.exc import BadName
from pydantic import BaseModel, ConfigDict

from gitcabin.storage.repo import BareRepo

# A single ref carries the sync config; each write appends a commit so the
# history is auditable. Bare gitcabin repos that have never been linked to a
# GitHub repo simply don't have this ref.
SYNC_REF = "refs/meta/sync"


class SyncConfig(BaseModel):
    """The on-disk schema for the sync config blob.

    `gh_viewer_login` is the gh-side login the user expects to authenticate
    as for *this* sync target. It overrides Settings.viewer_login per repo so
    a user with multiple GitHub identities can sync different gitcabin repos
    against different upstream accounts.

    `last_synced_at` and `viewer_repo_role` are populated by the sync
    operations themselves, not by the user.
    """

    model_config = ConfigDict(extra="ignore")

    gh_owner: str
    gh_name: str
    gh_viewer_login: str
    last_synced_at: str | None = None
    viewer_repo_role: str | None = None


def read_config(repo: BareRepo) -> SyncConfig | None:
    """Return the linked GitHub repo's sync config, or None if not yet linked."""
    commit = _maybe_commit(repo, SYNC_REF)
    if commit is None:
        return None
    blob = commit.tree["config.json"]
    raw = blob.data_stream.read().decode()
    return SyncConfig.model_validate_json(raw)


def write_config(repo: BareRepo, config: SyncConfig) -> None:
    """Persist the sync config, advancing refs/meta/sync by one commit."""
    payload = config.model_dump_json(indent=2)
    blob_sha = repo.run_git("hash-object", "-w", "--stdin", input=payload + "\n").strip()
    tree_sha = repo.run_git("mktree", input=f"100644 blob {blob_sha}\tconfig.json\n").strip()

    args: list[str] = [
        "-c",
        "user.name=gitcabin-sync",
        "-c",
        "user.email=sync@gitcabin.local",
        "commit-tree",
        tree_sha,
        "-m",
        f"sync config: {config.gh_owner}/{config.gh_name}",
    ]
    parent = _maybe_commit(repo, SYNC_REF)
    if parent is not None:
        args += ["-p", parent.hexsha]

    commit_sha = repo.run_git(*args).strip()
    repo.run_git("update-ref", SYNC_REF, commit_sha)


def _maybe_commit(repo: BareRepo, ref: str) -> Commit | None:
    try:
        return repo.repo.commit(ref)
    except (BadName, ValueError):
        return None
