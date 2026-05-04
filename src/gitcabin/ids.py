# ABOUTME: Opaque-but-reversible GraphQL node IDs for repos, users, and issues.
# ABOUTME: Encoding is base64-urlsafe so callers can decode an ID back to its coords.

from __future__ import annotations

import base64
import hashlib
from dataclasses import dataclass


def _encode(prefix: str, payload: str) -> str:
    """base64-urlsafe encode `payload` and prefix it.

    URL-safe alphabet so IDs survive being passed in URLs unmodified, and
    the trailing "=" padding is stripped to keep IDs compact (it's added
    back deterministically on decode).
    """
    raw = base64.urlsafe_b64encode(payload.encode()).rstrip(b"=").decode()
    return f"{prefix}_{raw}"


def _decode(prefix: str, node_id: str) -> str | None:
    """Reverse of `_encode`. Returns None if the ID isn't ours or doesn't decode."""
    expected = f"{prefix}_"
    if not node_id.startswith(expected):
        return None
    raw = node_id[len(expected) :]
    # Re-pad to a multiple of 4 so urlsafe_b64decode accepts it.
    padding = "=" * (-len(raw) % 4)
    try:
        return base64.urlsafe_b64decode(raw + padding).decode()
    except ValueError:
        # UnicodeDecodeError is a subclass of ValueError, so this covers both
        # base64-decode errors (binascii.Error subclasses ValueError too) and
        # UTF-8 decode errors that come from the .decode() call.
        return None


def repo_id(owner: str, name: str) -> str:
    """ID for a repository. Round-trips via `decode_repo_id`."""
    return _encode("R", f"{owner}/{name}")


@dataclass(frozen=True, slots=True)
class RepoCoords:
    owner: str
    name: str


def decode_repo_id(node_id: str) -> RepoCoords | None:
    """(owner, name) for a repo node id, or None if the id is malformed."""
    payload = _decode("R", node_id)
    if payload is None or "/" not in payload:
        return None
    owner, name = payload.split("/", 1)
    if not owner or not name:
        return None
    return RepoCoords(owner=owner, name=name)


def user_id(login: str) -> str:
    """ID for a user. Hashed (not reversible) — we never need to map back."""
    digest = hashlib.sha1(f"user/{login}".encode()).hexdigest()[:16]
    return f"U_{digest}"


def issue_id(owner: str, name: str, number: int) -> str:
    """ID for an issue. Reversible via `decode_issue_id`."""
    return _encode("I", f"{owner}/{name}#{number}")


@dataclass(frozen=True, slots=True)
class IssueCoords:
    owner: str
    name: str
    number: int


def decode_issue_id(node_id: str) -> IssueCoords | None:
    payload = _decode("I", node_id)
    if payload is None or "#" not in payload:
        return None
    repo_part, _, number_str = payload.rpartition("#")
    if "/" not in repo_part:
        return None
    owner, name = repo_part.split("/", 1)
    if not owner or not name or not number_str.isdigit():
        return None
    return IssueCoords(owner=owner, name=name, number=int(number_str))


def comment_id(owner: str, name: str, issue_number: int, comment_number: int) -> str:
    """ID for an issue comment. Reversible via `decode_comment_id`."""
    return _encode("IC", f"{owner}/{name}#{issue_number}.{comment_number}")


@dataclass(frozen=True, slots=True)
class CommentCoords:
    owner: str
    name: str
    issue_number: int
    number: int


def decode_comment_id(node_id: str) -> CommentCoords | None:
    """(owner, name, issue_number, comment_number) for a comment id."""
    payload = _decode("IC", node_id)
    if payload is None or "#" not in payload:
        return None
    repo_part, _, rest = payload.rpartition("#")
    if "/" not in repo_part or "." not in rest:
        return None
    issue_str, _, number_str = rest.rpartition(".")
    if not issue_str.isdigit() or not number_str.isdigit():
        return None
    owner, name = repo_part.split("/", 1)
    if not owner or not name:
        return None
    return CommentCoords(
        owner=owner,
        name=name,
        issue_number=int(issue_str),
        number=int(number_str),
    )
