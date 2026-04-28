# ABOUTME: Runtime configuration for the testgit server.
# ABOUTME: Values are read once from the environment; tests construct Settings directly.

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path

# The OAuth scopes the server claims to grant. `gh auth status` reads these from the
# `X-OAuth-Scopes` response header on the API root and warns if the minimum set
# (repo, read:org, gist) is missing. We hand them all out unconditionally because
# this server has no real OAuth model — anyone who reaches it is the owner.
DEFAULT_OAUTH_SCOPES: tuple[str, ...] = ("repo", "read:org", "gist")

# The login name the GraphQL `viewer` query reports when no override is set.
# Defined as a module constant so `Settings.from_env` can reference it without
# touching `cls.viewer_login` — which would resolve to the slot descriptor on
# this slots=True dataclass, not the default value.
DEFAULT_VIEWER_LOGIN = "david"

# Directory containing all bare repos at runtime. Inside the container this
# is the bind-mount target /app/data; tests override with tmp_path.
DEFAULT_DATA_DIR = Path("/app/data")


@dataclass(frozen=True, slots=True)
class Settings:
    # The login name the GraphQL `viewer` query reports. gh writes this into
    # ~/.config/gh/hosts.yml on `gh auth login`, and uses it everywhere it needs
    # to know "who am I" (e.g. `gh issue create --assignee @me`).
    viewer_login: str = DEFAULT_VIEWER_LOGIN

    # OAuth scopes advertised on the REST root. Order is preserved when joined
    # for the response header so tests can assert exact values.
    oauth_scopes: tuple[str, ...] = DEFAULT_OAUTH_SCOPES

    # Where bare repos live on disk. Resolvers compute repos/<owner>/<name>.git
    # under this path. `field(default=...)` rather than a literal default
    # because Path is not a frozen-dataclass-friendly mutable, and we want a
    # single canonical value rather than copies sprinkled through callers.
    data_dir: Path = field(default=DEFAULT_DATA_DIR)

    @classmethod
    def from_env(cls) -> Settings:
        return cls(
            viewer_login=os.environ.get("TESTGIT_VIEWER_LOGIN", DEFAULT_VIEWER_LOGIN),
            data_dir=Path(os.environ.get("TESTGIT_DATA_DIR", str(DEFAULT_DATA_DIR))),
        )
