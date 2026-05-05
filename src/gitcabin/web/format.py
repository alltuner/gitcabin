# ABOUTME: Small formatting helpers exposed as Jinja filters by routes.py.
# ABOUTME: relative_time turns an ISO timestamp into "X days ago"; short_sha trims 40-char SHAs.

from __future__ import annotations

from datetime import UTC, datetime


def relative_time(iso: str | None) -> str:
    """Render an ISO 8601 timestamp as a coarse relative phrase ("5 days ago").

    Designed for git metadata where second-level precision isn't useful at a
    glance. Pair with `<time title="{{ iso }}">{{ iso | relative_time }}</time>`
    so the full timestamp stays one hover away. Returns empty string for
    None / unparseable input — callers can guard if they need a stronger
    fallback.
    """
    if not iso:
        return ""
    try:
        # Accept "Z"-suffixed ISO strings as well as offset-included ones.
        moment = datetime.fromisoformat(iso.replace("Z", "+00:00"))
    except ValueError:
        return iso
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=UTC)
    delta = datetime.now(UTC) - moment
    seconds = int(delta.total_seconds())
    if seconds < 0:
        # Future timestamp — clock skew or a deliberate backdate. Fall back to
        # the literal text rather than printing "in 3 days" misleadingly.
        return iso[:10]
    if seconds < 60:
        return "just now"
    minutes = seconds // 60
    if minutes < 60:
        return f"{minutes} minute{'s' if minutes != 1 else ''} ago"
    hours = minutes // 60
    if hours < 24:
        return f"{hours} hour{'s' if hours != 1 else ''} ago"
    days = hours // 24
    if days < 30:
        return f"{days} day{'s' if days != 1 else ''} ago"
    months = days // 30
    if months < 12:
        return f"{months} month{'s' if months != 1 else ''} ago"
    years = days // 365
    return f"{years} year{'s' if years != 1 else ''} ago"


def short_sha(sha: str | None, length: int = 7) -> str:
    """Trim a git object SHA to its short form. None passes through as ''."""
    if not sha:
        return ""
    return sha[:length]
