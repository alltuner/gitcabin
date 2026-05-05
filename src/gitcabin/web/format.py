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


_HEX = set("0123456789abcdef")

_MONTHS = (
    "January", "February", "March", "April", "May", "June",
    "July", "August", "September", "October", "November", "December",
)


def pretty_date(iso: str | None) -> str:
    """Format an ISO timestamp as 'Month D, YYYY' — used as a section
    header on the commits page where coarse relative phrasing isn't
    enough to anchor the day."""
    if not iso:
        return ""
    try:
        moment = datetime.fromisoformat(iso.replace("Z", "+00:00"))
    except ValueError:
        return iso
    return f"{_MONTHS[moment.month - 1]} {moment.day}, {moment.year}"


def ref_label(ref: str | None) -> str:
    """Render a ref name for display — branches and tags pass through;
    raw 40-char SHAs get shortened so the chrome doesn't blow up when a
    URL pins to a specific commit (`/blob/<sha>/...`)."""
    if not ref:
        return ""
    if len(ref) == 40 and all(c in _HEX for c in ref):
        return ref[:7]
    return ref


# Filename → icon-template mapping. Order matters: full-name matches win
# over extension matches, so e.g. "Dockerfile.dev" gets the Docker icon
# rather than falling back. New file types: add an entry here and a
# matching icons/file_<name>.html.
_FILE_ICON_BY_FULLNAME: dict[str, str] = {
    "dockerfile": "icons/file_dockerfile.html",
    "license": "icons/file_license.html",
    "license.md": "icons/file_license.html",
    "license.txt": "icons/file_license.html",
    "readme": "icons/file_md.html",
    "readme.md": "icons/file_md.html",
    "readme.markdown": "icons/file_md.html",
    "makefile": "icons/file_makefile.html",
    "justfile": "icons/file_makefile.html",
}

_FILE_ICON_BY_EXTENSION: dict[str, str] = {
    ".py": "icons/file_python.html",
    ".pyi": "icons/file_python.html",
    ".js": "icons/file_js.html",
    ".mjs": "icons/file_js.html",
    ".cjs": "icons/file_js.html",
    ".jsx": "icons/file_js.html",
    ".ts": "icons/file_ts.html",
    ".tsx": "icons/file_ts.html",
    ".json": "icons/file_json.html",
    ".md": "icons/file_md.html",
    ".markdown": "icons/file_md.html",
    ".yaml": "icons/file_yaml.html",
    ".yml": "icons/file_yaml.html",
    ".toml": "icons/file_yaml.html",
    ".css": "icons/file_css.html",
    ".html": "icons/file_html.html",
    ".htm": "icons/file_html.html",
    ".go": "icons/file_go.html",
    ".rs": "icons/file_rust.html",
    ".sh": "icons/file_shell.html",
    ".bash": "icons/file_shell.html",
    ".zsh": "icons/file_shell.html",
    ".lock": "icons/file_lock.html",
}


def file_icon(name: str) -> str:
    """Return the icon-template path for a filename. Falls back to icons/file.html."""
    if not name:
        return "icons/file.html"
    lower = name.lower()
    full_match = _FILE_ICON_BY_FULLNAME.get(lower)
    if full_match is not None:
        return full_match
    if "." in lower:
        ext = "." + lower.rsplit(".", 1)[1]
        return _FILE_ICON_BY_EXTENSION.get(ext, "icons/file.html")
    return "icons/file.html"
