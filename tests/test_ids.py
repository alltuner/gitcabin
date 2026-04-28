# ABOUTME: Tests for opaque-but-reversible GraphQL node IDs.
# ABOUTME: Round-trip is the contract: every encoded ID must decode back to the same coords.

from __future__ import annotations

from gitcabin.ids import (
    decode_issue_id,
    decode_repo_id,
    issue_id,
    repo_id,
)


def test_repo_id_round_trips() -> None:
    coords = decode_repo_id(repo_id("octocat", "hello-world"))
    assert coords is not None
    assert coords.owner == "octocat"
    assert coords.name == "hello-world"


def test_repo_id_handles_special_characters_in_name() -> None:
    # Names with dots, dashes, underscores are common; URL-safe base64
    # must keep them clean.
    coords = decode_repo_id(repo_id("foo-bar", "my.repo_name"))
    assert coords is not None
    assert coords.owner == "foo-bar"
    assert coords.name == "my.repo_name"


def test_decode_repo_id_returns_none_for_garbage() -> None:
    assert decode_repo_id("not-an-id") is None
    assert decode_repo_id("R_!!!") is None
    assert decode_repo_id("U_abc") is None  # wrong prefix


def test_issue_id_round_trips() -> None:
    coords = decode_issue_id(issue_id("octocat", "hello", 42))
    assert coords is not None
    assert coords.owner == "octocat"
    assert coords.name == "hello"
    assert coords.number == 42
