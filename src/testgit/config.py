# ABOUTME: Runtime configuration for the testgit server.
# ABOUTME: Values are read once from the environment; tests construct Settings directly.

from __future__ import annotations

import os
from dataclasses import dataclass

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


@dataclass(frozen=True, slots=True)
class Settings:
    # The login name the GraphQL `viewer` query reports. gh writes this into
    # ~/.config/gh/hosts.yml on `gh auth login`, and uses it everywhere it needs
    # to know "who am I" (e.g. `gh issue create --assignee @me`).
    viewer_login: str = DEFAULT_VIEWER_LOGIN

    # OAuth scopes advertised on the REST root. Order is preserved when joined
    # for the response header so tests can assert exact values.
    oauth_scopes: tuple[str, ...] = DEFAULT_OAUTH_SCOPES

    @classmethod
    def from_env(cls) -> Settings:
        return cls(
            viewer_login=os.environ.get("TESTGIT_VIEWER_LOGIN", DEFAULT_VIEWER_LOGIN),
        )
