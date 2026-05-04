# ABOUTME: Tests for gitcabin.sync.gh — the gh-CLI wrapper sync uses to talk to GitHub.
# ABOUTME: Real subprocess never runs here; runner is injected so tests stay deterministic.

from __future__ import annotations

import pytest

from gitcabin.sync.gh import GhClient, gh_login


def test_get_json_invokes_gh_api_with_hostname() -> None:
    calls: list[list[str]] = []

    def runner(argv: list[str]) -> str:
        calls.append(argv)
        return '{"login": "alice"}'

    client = GhClient(host="example.com", runner=runner)
    payload = client.get_json("user")

    assert calls == [["api", "--hostname", "example.com", "user"]]
    assert payload == {"login": "alice"}


def test_get_json_appends_paginate_flag_when_requested() -> None:
    calls: list[list[str]] = []

    def runner(argv: list[str]) -> str:
        calls.append(argv)
        return "[]"

    GhClient(runner=runner).get_json("repos/octo/hello/issues", paginate=True)

    assert "--paginate" in calls[0]
    # Path comes after the --paginate flag, host comes before; gh accepts both
    # orders so this assertion just sanity-checks we didn't drop the path.
    assert calls[0][-1] == "repos/octo/hello/issues"


def test_gh_login_returns_login_field() -> None:
    def runner(argv: list[str]) -> str:
        assert argv == ["api", "--hostname", "github.com", "user"]
        return '{"login": "octocat", "id": 1}'

    assert gh_login(GhClient(runner=runner)) == "octocat"


def test_gh_login_raises_on_unexpected_response_shape() -> None:
    def runner(argv: list[str]) -> str:
        return "{}"

    with pytest.raises(RuntimeError, match="unexpected"):
        gh_login(GhClient(runner=runner))


def test_default_host_is_github_com() -> None:
    captured: list[str] = []

    def runner(argv: list[str]) -> str:
        captured.extend(argv)
        return '{"login": "x"}'

    GhClient(runner=runner).get_json("user")

    # --hostname should be present and set to github.com unless overridden.
    idx = captured.index("--hostname")
    assert captured[idx + 1] == "github.com"
