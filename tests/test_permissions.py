# ABOUTME: Tests for gitcabin.permissions — the can_edit / can_delete edit-affordance helpers.
# ABOUTME: Pure logic on Issue / Comment dataclasses; no fixtures or storage involvement.

from __future__ import annotations

from gitcabin.permissions import (
    RepoRole,
    can_change_issue_state,
    can_delete_comment,
    can_edit_comment,
    can_edit_issue,
)
from gitcabin.storage.issues import Comment, Issue, IssueState, Provenance


def _issue(author: str, provenance: Provenance = Provenance.LOCAL_ONLY) -> Issue:
    return Issue(
        number=1,
        title="t",
        body="b",
        author=author,
        state=IssueState.OPEN,
        created_at="2026-01-01T00:00:00Z",
        updated_at="2026-01-01T00:00:00Z",
        provenance=provenance,
        gh_issue_id=None,
        gh_author_id=None,
    )


def _comment(author: str, provenance: Provenance = Provenance.LOCAL_ONLY) -> Comment:
    return Comment(
        number=1,
        body="b",
        author=author,
        created_at="2026-01-01T00:00:00Z",
        provenance=provenance,
        gh_comment_id=None,
        gh_author_id=None,
    )


# ---- can_edit_issue ----------------------------------------------------- #


def test_author_can_edit_their_own_issue() -> None:
    assert can_edit_issue(_issue("alice"), viewer="alice") is True


def test_non_author_cannot_edit_someone_elses_issue() -> None:
    assert can_edit_issue(_issue("alice"), viewer="bob") is False


def test_admin_role_does_not_grant_edit_permission() -> None:
    # Admins can DELETE comments and CHANGE issue state, but they cannot
    # edit content — that would impersonate the original author.
    issue = _issue("alice", Provenance.SYNCED_FROM_GITHUB)
    assert can_edit_issue(issue, viewer="bob") is False


def test_provenance_does_not_change_edit_rule_for_author() -> None:
    for prov in Provenance:
        assert can_edit_issue(_issue("alice", prov), viewer="alice") is True


# ---- can_change_issue_state -------------------------------------------- #


def test_author_can_close_their_own_issue_regardless_of_role() -> None:
    issue = _issue("alice")
    assert can_change_issue_state(issue, viewer="alice", role=RepoRole.READ) is True


def test_triage_role_can_close_other_authors_issue() -> None:
    issue = _issue("alice", Provenance.SYNCED_FROM_GITHUB)
    assert can_change_issue_state(issue, viewer="bob", role=RepoRole.TRIAGE) is True
    assert can_change_issue_state(issue, viewer="bob", role=RepoRole.WRITE) is True
    assert can_change_issue_state(issue, viewer="bob", role=RepoRole.MAINTAIN) is True
    assert can_change_issue_state(issue, viewer="bob", role=RepoRole.ADMIN) is True


def test_read_role_cannot_close_other_authors_issue() -> None:
    issue = _issue("alice", Provenance.SYNCED_FROM_GITHUB)
    assert can_change_issue_state(issue, viewer="bob", role=RepoRole.READ) is False


# ---- can_edit_comment -------------------------------------------------- #


def test_author_can_edit_their_own_comment() -> None:
    assert can_edit_comment(_comment("alice"), viewer="alice") is True


def test_non_author_cannot_edit_someone_elses_comment() -> None:
    assert can_edit_comment(_comment("alice"), viewer="bob") is False


def test_admin_does_not_grant_comment_edit_permission() -> None:
    # Same impersonation guard as issue editing — admin can DELETE but not edit.
    assert can_edit_comment(_comment("alice"), viewer="bob") is False


# ---- can_delete_comment ----------------------------------------------- #


def test_author_can_delete_their_own_comment_regardless_of_role() -> None:
    comment = _comment("alice")
    assert can_delete_comment(comment, viewer="alice", role=RepoRole.READ) is True


def test_admin_can_delete_other_authors_comment() -> None:
    comment = _comment("alice", Provenance.SYNCED_FROM_GITHUB)
    assert can_delete_comment(comment, viewer="bob", role=RepoRole.ADMIN) is True


def test_non_admin_roles_cannot_delete_other_authors_comment() -> None:
    comment = _comment("alice", Provenance.SYNCED_FROM_GITHUB)
    for role in (RepoRole.READ, RepoRole.TRIAGE, RepoRole.WRITE, RepoRole.MAINTAIN):
        assert can_delete_comment(comment, viewer="bob", role=role) is False
