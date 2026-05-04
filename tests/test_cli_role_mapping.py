# ABOUTME: Tests for the REST permissions -> RepoRole mapping in gitcabin.cli.
# ABOUTME: Pure logic; no fixtures, no subprocess.

from __future__ import annotations

from gitcabin.cli import _role_from_repo_payload


def test_admin_flag_maps_to_admin() -> None:
    payload = {"permissions": {"admin": True, "maintain": True, "push": True, "pull": True}}
    assert _role_from_repo_payload(payload) == "ADMIN"


def test_maintain_without_admin_maps_to_maintain() -> None:
    payload = {"permissions": {"admin": False, "maintain": True, "push": True, "pull": True}}
    assert _role_from_repo_payload(payload) == "MAINTAIN"


def test_push_without_higher_maps_to_write() -> None:
    payload = {"permissions": {"admin": False, "maintain": False, "push": True, "pull": True}}
    assert _role_from_repo_payload(payload) == "WRITE"


def test_triage_without_push_maps_to_triage() -> None:
    payload = {
        "permissions": {
            "admin": False,
            "maintain": False,
            "push": False,
            "triage": True,
            "pull": True,
        }
    }
    assert _role_from_repo_payload(payload) == "TRIAGE"


def test_pull_only_maps_to_read() -> None:
    payload = {"permissions": {"admin": False, "maintain": False, "push": False, "pull": True}}
    assert _role_from_repo_payload(payload) == "READ"


def test_no_permissions_object_returns_none() -> None:
    assert _role_from_repo_payload({}) is None


def test_non_dict_payload_returns_none() -> None:
    assert _role_from_repo_payload("nope") is None
    assert _role_from_repo_payload(None) is None


def test_all_false_permissions_returns_none() -> None:
    payload = {"permissions": {"admin": False, "push": False, "pull": False}}
    assert _role_from_repo_payload(payload) is None
