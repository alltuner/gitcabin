# ABOUTME: Edit-affordance rules — who can edit / close / delete which item.
# ABOUTME: Single source of truth used by both the API mutations and the UI.

from __future__ import annotations

from enum import StrEnum

from gitcabin.storage.issues import Comment, Issue


class RepoRole(StrEnum):
    """Mirrors GitHub's RepositoryPermission values for the viewer.

    Local-only repos (never linked to GitHub) are implicitly ADMIN — the user
    owns the bare repo on their disk and can do anything they want.
    """

    READ = "READ"
    TRIAGE = "TRIAGE"
    WRITE = "WRITE"
    MAINTAIN = "MAINTAIN"
    ADMIN = "ADMIN"


# Roles that grant moderation actions (close / reopen / lock / hide). Author
# always has edit rights regardless of role; this set is the *non-author*
# escalation path for actions that are administratively allowed but where the
# viewer didn't author the content.
_TRIAGE_ROLES: frozenset[RepoRole] = frozenset(
    {RepoRole.TRIAGE, RepoRole.WRITE, RepoRole.MAINTAIN, RepoRole.ADMIN}
)


def can_edit_issue(issue: Issue, viewer: str) -> bool:
    """True iff `viewer` may edit the issue's body or title.

    Content edits require authorship — closing or hiding someone else's
    issue is a moderation action (see can_change_issue_state); editing
    their words is impersonation, regardless of role.
    """
    return issue.author == viewer


def can_change_issue_state(issue: Issue, viewer: str, role: RepoRole) -> bool:
    """True iff `viewer` may close / reopen the issue.

    Author can always change state on their own issues. Other viewers need a
    privileged repo role — TRIAGE or above lets you close or reopen issues
    you didn't author, mirroring GitHub's own rules.
    """
    if issue.author == viewer:
        return True
    return role in _TRIAGE_ROLES


def can_edit_comment(comment: Comment, viewer: str) -> bool:
    """True iff `viewer` may edit the comment's body.

    Content edits require authorship — exactly the same constraint as
    can_edit_issue. Repo admins cannot edit other people's comments;
    deletion (see can_delete_comment) is the moderation lever instead.
    """
    return comment.author == viewer


def can_delete_comment(comment: Comment, viewer: str, role: RepoRole) -> bool:
    """True iff `viewer` may delete the comment.

    Author can always delete their own comments. Repo ADMIN can delete anyone's
    comment as a moderation action (the "remove off-topic comment" affordance
    GitHub exposes). TRIAGE/WRITE/MAINTAIN cannot — deletion is destructive,
    so we draw the moderation line at full admin only.
    """
    if comment.author == viewer:
        return True
    return role is RepoRole.ADMIN
