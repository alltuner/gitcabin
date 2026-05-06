# ABOUTME: Per-repo sync config stored at refs/meta/sync as a single config.json blob.
# ABOUTME: Mirrors design in docs/github-sync.md — links a local repo to a GitHub repo.

from __future__ import annotations

import re

from pydantic import BaseModel, ConfigDict, field_validator

from gitcabin.storage.repo import BareRepo
from gitcabin.sync._meta_ref import read_meta_blob, write_meta_blob

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

    @field_validator("gh_owner", "gh_name", mode="before")
    @classmethod
    def _validate_segment(cls, v: object) -> object:
        if (
            not isinstance(v, str)
            or not re.match(r"^[a-zA-Z0-9._-]+$", v)
            or v in (".", "..")
        ):
            raise ValueError("invalid repository segment")
        return v


def read_config(repo: BareRepo) -> SyncConfig | None:
    """Return the linked GitHub repo's sync config, or None if not yet linked."""
    raw = read_meta_blob(repo, SYNC_REF, "config.json")
    if raw is None:
        return None
    return SyncConfig.model_validate_json(raw)


def write_config(repo: BareRepo, config: SyncConfig) -> None:
    """Persist the sync config, advancing refs/meta/sync by one commit."""
    write_meta_blob(
        repo,
        SYNC_REF,
        "config.json",
        config.model_dump_json(indent=2),
        message=f"sync config: {config.gh_owner}/{config.gh_name}",
    )
