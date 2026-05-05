# ABOUTME: Code-browser helpers — resolve refs/paths, render blobs, find README.
# ABOUTME: Used by the web UI; pure read-only walks over the GitPython object graph.

from __future__ import annotations

import re
from dataclasses import dataclass

import markdown
from git import Blob, Commit, Tree
from git.exc import BadName
from pygments import highlight
from pygments.formatters import HtmlFormatter
from pygments.lexers import get_lexer_by_name, guess_lexer_for_filename
from pygments.util import ClassNotFound

from gitcabin.storage.repo import BareRepo

# Filenames a project might use for the repo's "front-page" doc. GitHub picks
# the first match in this order; we mirror that.
README_CANDIDATES: tuple[str, ...] = (
    "README.md",
    "README.markdown",
    "README.rst",
    "README.txt",
    "README",
    "readme.md",
    "readme",
)

# Cap on rendered file size so a 50MB log file doesn't swamp the browser.
# Above this, we fall back to a "too large to render inline" notice.
MAX_BLOB_RENDER_BYTES = 1_000_000


@dataclass(frozen=True, slots=True)
class TreeEntry:
    """One row in a file-listing view (a tree subdir, blob, or symlink)."""

    name: str
    type: str  # "tree" or "blob"
    sha: str
    size: int | None  # bytes for blobs, None for trees
    is_symlink: bool = False
    last_commit_message: str | None = None
    last_commit_sha: str | None = None
    last_commit_at: str | None = None


@dataclass(frozen=True, slots=True)
class RenderedBlob:
    """The blob viewer's payload — either highlighted HTML or a fallback."""

    name: str
    size: int
    is_binary: bool
    is_too_large: bool
    is_empty: bool
    highlighted_html: str | None
    raw_text: str | None


@dataclass(frozen=True, slots=True)
class CommitSummary:
    """One row in a commits-log view."""

    sha: str
    short_sha: str
    subject: str
    author_name: str
    author_email: str
    authored_at: str
    parents: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class DiffLine:
    """One line in a hunk. `kind` is "context" / "add" / "remove" / "noeol"."""

    kind: str
    old_no: int | None
    new_no: int | None
    text: str


@dataclass(frozen=True, slots=True)
class DiffHunk:
    """A `@@ -a,b +c,d @@` block — header line plus its body."""

    header: str
    lines: tuple[DiffLine, ...]


@dataclass(frozen=True, slots=True)
class DiffFile:
    """One file's worth of diff — old/new path, change type, hunks."""

    change_type: str
    old_path: str
    new_path: str
    hunks: tuple[DiffHunk, ...]
    is_binary: bool = False


@dataclass(frozen=True, slots=True)
class CommitDetail:
    """Single-commit view — metadata + the names of changed files."""

    sha: str
    short_sha: str
    subject: str
    body: str
    author_name: str
    author_email: str
    authored_at: str
    parents: tuple[str, ...]
    changed_paths: tuple[tuple[str, str], ...]  # (change_type, path) pairs
    diff_files: tuple[DiffFile, ...] = ()
    diff_truncated: bool = False


@dataclass(frozen=True, slots=True)
class RefSummary:
    """A branch or tag with the commit it points at."""

    name: str
    full_path: str
    target_sha: str
    target_short_sha: str
    target_subject: str
    target_authored_at: str


@dataclass(frozen=True, slots=True)
class BlameLine:
    """One line of blame output. The first line of any run-of-same-commit lines
    keeps the commit metadata; subsequent contiguous lines reuse it."""

    line_number: int
    text: str
    commit_sha: str
    short_sha: str
    author_name: str
    authored_at: str
    subject: str
    is_run_start: bool


def head_ref_name(bare: BareRepo) -> str | None:
    """Return the symbolic HEAD's ref name, or None on detached HEAD/empty repo."""
    try:
        return bare.repo.head.reference.name
    except TypeError, ValueError:
        return None


def is_empty_repo(bare: BareRepo) -> bool:
    """True if the bare repo has no branches/tags — i.e. nothing to browse yet.

    A fresh `git init --bare --initial-branch=main` produces a repo where HEAD
    symref's to refs/heads/main but the ref itself doesn't exist; resolving
    "main" raises BadName. The branches collection is the load-bearing signal.
    """
    return not list(bare.repo.branches) and not list(bare.repo.tags)


def resolve_ref(bare: BareRepo, ref: str) -> Commit | None:
    """Resolve a ref string (branch / tag / sha) to a Commit, or None."""
    try:
        return bare.repo.commit(ref)
    except BadName, ValueError:
        return None


def walk_tree_at_path(commit: Commit, path: str) -> Tree | Blob | None:
    """Descend `commit.tree` along the slash-separated `path`. Empty path is the root."""
    if not path:
        return commit.tree
    node: Tree | Blob = commit.tree
    for segment in path.split("/"):
        if segment == "":
            continue
        try:
            node = node[segment]  # type: ignore[index]
        except KeyError, TypeError:
            return None
    return node


_GIT_SYMLINK_MODE = 0o120000


def list_tree_entries(tree: Tree) -> list[TreeEntry]:
    """List a tree's direct children. Trees come first (alphabetical), then blobs."""
    trees: list[TreeEntry] = []
    blobs: list[TreeEntry] = []
    for entry in tree:
        if entry.type == "tree":
            trees.append(TreeEntry(name=entry.name, type="tree", sha=entry.hexsha, size=None))
        else:
            is_symlink = entry.mode == _GIT_SYMLINK_MODE
            blobs.append(
                TreeEntry(
                    name=entry.name,
                    type="blob",
                    sha=entry.hexsha,
                    size=entry.size,
                    is_symlink=is_symlink,
                )
            )
    trees.sort(key=lambda e: e.name)
    blobs.sort(key=lambda e: e.name)
    return trees + blobs


def enrich_with_last_commits(
    commit: Commit,
    entries: list[TreeEntry],
    *,
    prefix: str = "",
    max_commits: int = 500,
) -> list[TreeEntry]:
    """Walk back from `commit` filling in last_commit_* per entry.

    Stops as soon as every entry has been resolved or after max_commits
    walked, whichever comes first. `prefix` is the path inside the tree
    where these entries live (`""` for repo root, `"src/web"` for
    nested). Each entry's name is joined with the prefix to form the
    full path that we match against the commit's diff.

    Algorithm: walk commits newest-first. For each commit `c`, take the
    diff against its first parent (or the root tree if it's the root
    commit). Any path under `prefix/<entry.name>` whose direct child
    `<entry.name>` matches an unresolved entry — that entry's last
    commit is `c`. Mark resolved, continue.

    Pure GitPython, no shell-out. For repos with many small files this
    is O(commits × files-touched-per-commit) which is acceptable for
    the typical interactive view.
    """
    pending: dict[str, TreeEntry] = {e.name: e for e in entries}
    resolved: dict[str, tuple[str, str, str]] = {}  # name -> (sha, subject, iso)
    walked = 0

    for c in commit.repo.iter_commits(commit.hexsha, max_count=max_commits):
        walked += 1
        if not pending:
            break
        # Diff against first parent — for the root commit, diff against an
        # empty tree (gitpython handles this when there's no parent).
        if c.parents:
            try:
                diff_paths = {item.b_path or item.a_path for item in c.parents[0].diff(c)}
            except Exception:
                continue
        else:
            diff_paths = {item.path for item in c.tree.traverse() if item.type == "blob"}

        # For each path in this commit, peel off `prefix/`, look at the
        # first segment, and if it matches a pending entry name, that
        # entry's last commit is this one.
        prefix_slash = f"{prefix}/" if prefix else ""
        msg = c.message if isinstance(c.message, str) else c.message.decode()
        subject = msg.split("\n", 1)[0].strip()
        ts = c.authored_datetime.isoformat()
        for p in diff_paths:
            if p is None:
                continue
            rel = p[len(prefix_slash):] if prefix_slash and p.startswith(prefix_slash) else p
            if not rel or "/" in rel and not (prefix_slash and p.startswith(prefix_slash)):
                # Path is outside this prefix's scope; skip.
                if prefix_slash and not p.startswith(prefix_slash):
                    continue
            head = rel.split("/", 1)[0]
            if head in pending and head not in resolved:
                resolved[head] = (c.hexsha, subject, ts)
        # Drop resolved from pending so we exit early.
        for name in resolved:
            pending.pop(name, None)

    out: list[TreeEntry] = []
    for entry in entries:
        info = resolved.get(entry.name)
        if info is None:
            out.append(entry)
        else:
            sha, subject, ts = info
            out.append(
                TreeEntry(
                    name=entry.name,
                    type=entry.type,
                    sha=entry.sha,
                    size=entry.size,
                    is_symlink=entry.is_symlink,
                    last_commit_message=subject,
                    last_commit_sha=sha,
                    last_commit_at=ts,
                )
            )
    return out


def find_readme(tree: Tree) -> Blob | None:
    """Return the first README-shaped blob at the top level, or None."""
    by_name = {entry.name: entry for entry in tree if entry.type == "blob"}
    for candidate in README_CANDIDATES:
        if candidate in by_name:
            return by_name[candidate]
    return None


def render_markdown(text: str) -> str:
    """Render Markdown to HTML. Trusted source (repo author == operator)."""
    md = markdown.Markdown(extensions=["fenced_code", "tables", "toc"])
    return md.convert(text)


def render_blob(blob: Blob) -> RenderedBlob:
    """Decode and syntax-highlight a blob, with size, empty, and binary fallbacks."""
    raw = blob.data_stream.read()
    if len(raw) == 0:
        # Empty file — skip the pygments table entirely (it would emit a
        # phantom "1" line-number gutter for a single empty line).
        return RenderedBlob(
            name=blob.name,
            size=0,
            is_binary=False,
            is_too_large=False,
            is_empty=True,
            highlighted_html=None,
            raw_text="",
        )
    if len(raw) > MAX_BLOB_RENDER_BYTES:
        return RenderedBlob(
            name=blob.name,
            size=blob.size,
            is_binary=False,
            is_too_large=True,
            is_empty=False,
            highlighted_html=None,
            raw_text=None,
        )
    if b"\x00" in raw[:8192]:
        # Heuristic: a NUL byte in the first 8KB means binary. Same rule git
        # itself uses internally.
        return RenderedBlob(
            name=blob.name,
            size=blob.size,
            is_binary=True,
            is_too_large=False,
            is_empty=False,
            highlighted_html=None,
            raw_text=None,
        )

    try:
        text = raw.decode()
    except UnicodeDecodeError:
        return RenderedBlob(
            name=blob.name,
            size=blob.size,
            is_binary=True,
            is_too_large=False,
            is_empty=False,
            highlighted_html=None,
            raw_text=None,
        )

    # Pick a lexer by filename when we can; fall back to plain text.
    try:
        lexer = guess_lexer_for_filename(blob.name, text)
    except ClassNotFound:
        lexer = get_lexer_by_name("text")
    # `anchorlinenos` wraps each line number in `<a href="#L<n>">` and gives
    # the corresponding code line `id="L<n>"` so users can deep-link to a
    # specific line. Pygments hardcodes a hyphen in its anchor format
    # (`L-1`, `L-2`); rewriting to `L1`, `L2` matches GitHub's `#L42`
    # convention so links pasted between the two services Just Work.
    formatter = HtmlFormatter(
        linenos="table",
        cssclass="hl",
        wrapcode=True,
        anchorlinenos=True,
        lineanchors="L",
    )
    html = re.sub(
        r'(name|id|href)="(#?)L-(\d+)"',
        r'\1="\2L\3"',
        highlight(text, lexer, formatter),
    )
    return RenderedBlob(
        name=blob.name,
        size=blob.size,
        is_binary=False,
        is_too_large=False,
        is_empty=False,
        highlighted_html=html,
        raw_text=text,
    )


_PYGMENTS_LIGHT_STYLE = "default"
_PYGMENTS_DARK_STYLE = "github-dark"

# Three CSS selector branches that must all carry the dark token rules,
# mirroring the @custom-variant dark in styles.css. Keep these in sync.
_DARK_TRIGGER_PREFIXES = (
    "[data-theme=dark] .hl",
    ":root:has(input.theme-controller[value=dark]:checked) .hl",
)
_DARK_MEDIA_PREFIX = (
    ":root:not([data-theme=light])"
    ":not(:has(input.theme-controller[value=light]:checked)) .hl"
)


def _scoped_style_defs(formatter: HtmlFormatter, prefix: str) -> str:
    """Return pygments rules whose selectors actually start with `prefix`.

    `HtmlFormatter.get_style_defs(prefix)` prefixes the `.hl`-scoped rules
    but leaves a few helpers unprefixed (`pre {}`, `td.linenos .normal {}`,
    `span.linenos {}`). Those bare rules carry the chosen style's hard-
    coded colors and bleed across themes (the dark style's `td.linenos`
    background ends up applying in light mode too). We drop any rule whose
    selector doesn't start with our prefix.
    """
    out: list[str] = []
    for line in formatter.get_style_defs(prefix).splitlines():
        stripped = line.lstrip()
        if not stripped or stripped.startswith(prefix):
            out.append(line)
    return "\n".join(out)


def pygments_stylesheet() -> str:
    """The CSS Pygments needs for the `.hl` class our formatter emits.

    Bundles two themes — a light one for default surfaces and a dark one
    that activates whenever the page is in dark mode (explicit data-theme,
    DaisyUI's theme-controller :has() selector, or prefers-color-scheme
    fallback in `system` mode). The dark rules are emitted with three
    different prefix selectors so they fire under each of those triggers.
    """
    chunks: list[str] = []
    light = HtmlFormatter(style=_PYGMENTS_LIGHT_STYLE, cssclass="hl")
    chunks.append(_scoped_style_defs(light, ".hl"))

    dark = HtmlFormatter(style=_PYGMENTS_DARK_STYLE, cssclass="hl")
    for prefix in _DARK_TRIGGER_PREFIXES:
        chunks.append(_scoped_style_defs(dark, prefix))
    chunks.append(
        f"@media (prefers-color-scheme: dark) {{\n"
        f"{_scoped_style_defs(dark, _DARK_MEDIA_PREFIX)}\n"
        f"}}"
    )
    return "\n\n".join(chunks)


def list_commits(commit: Commit, *, max_count: int = 50) -> list[CommitSummary]:
    """Walk commits reachable from `commit`, newest first, capped at max_count."""
    out: list[CommitSummary] = []
    for c in commit.repo.iter_commits(commit.hexsha, max_count=max_count):
        out.append(_summarize(c))
    return out


MAX_DIFF_RENDER_BYTES = 1_000_000


def commit_detail(commit: Commit) -> CommitDetail:
    """Metadata + changed-files + rendered unified diff for a single commit."""
    msg = commit.message if isinstance(commit.message, str) else commit.message.decode()
    subject, _, body = msg.partition("\n")

    changed: list[tuple[str, str]] = []
    diff_files: tuple[DiffFile, ...] = ()
    diff_truncated = False

    if commit.parents:
        diffs = commit.parents[0].diff(commit, create_patch=True)
        files: list[DiffFile] = []
        running = 0
        for d in diffs:
            change_type = (
                "added"
                if d.new_file
                else "deleted"
                if d.deleted_file
                else "renamed"
                if d.renamed_file
                else "modified"
            )
            path = d.b_path or d.a_path or ""
            changed.append((change_type, path))

            patch = d.diff if isinstance(d.diff, bytes) else (d.diff or b"")
            running += len(patch)
            # Cap total patch size; oversized diffs (e.g. lockfile bumps) skip
            # rendering and surface a "Diff too large" notice.
            if running > MAX_DIFF_RENDER_BYTES:
                diff_truncated = True
                break

            patch_text = patch.decode(errors="replace") if patch else ""
            is_binary = "Binary files" in patch_text
            files.append(
                DiffFile(
                    change_type=change_type,
                    old_path=d.a_path or "",
                    new_path=d.b_path or "",
                    hunks=_parse_hunks(patch_text) if not is_binary else (),
                    is_binary=is_binary,
                )
            )
        if not diff_truncated:
            diff_files = tuple(files)
    else:
        # Initial commit: every file in the tree was "added". No parent to
        # diff against, so we don't render a unified diff body.
        for blob in commit.tree.traverse():
            if blob.type == "blob":
                changed.append(("added", blob.path))

    return CommitDetail(
        sha=commit.hexsha,
        short_sha=commit.hexsha[:7],
        subject=subject.strip(),
        body=body.strip("\n"),
        author_name=commit.author.name or "",
        author_email=commit.author.email or "",
        authored_at=commit.authored_datetime.isoformat(),
        parents=tuple(p.hexsha for p in commit.parents),
        changed_paths=tuple(changed),
        diff_files=diff_files,
        diff_truncated=diff_truncated,
    )


_HUNK_HEADER_RE = re.compile(r"^@@ -(\d+)(?:,(\d+))? \+(\d+)(?:,(\d+))? @@")


def paired_lines(
    lines: tuple[DiffLine, ...],
) -> tuple[tuple[DiffLine | None, DiffLine | None], ...]:
    """Reshape unified-diff lines into (left, right) pairs for side-by-side
    rendering.

    Walks the hunk preserving order, accumulating runs of remove / add lines
    and flushing them as pairs when context arrives. Within a flush, removes
    and adds zip 1-to-1 up to the shorter run; trailing extras land in their
    own row with the other side empty. Mirrors the heuristic GitHub uses to
    align edits.
    """
    rows: list[tuple[DiffLine | None, DiffLine | None]] = []
    pending_removes: list[DiffLine] = []
    pending_adds: list[DiffLine] = []

    def flush() -> None:
        n = max(len(pending_removes), len(pending_adds))
        for i in range(n):
            left = pending_removes[i] if i < len(pending_removes) else None
            right = pending_adds[i] if i < len(pending_adds) else None
            rows.append((left, right))
        pending_removes.clear()
        pending_adds.clear()

    for line in lines:
        if line.kind == "remove":
            pending_removes.append(line)
        elif line.kind == "add":
            pending_adds.append(line)
        else:
            flush()
            rows.append((line, line))
    flush()
    return tuple(rows)


def _parse_hunks(patch: str) -> tuple[DiffHunk, ...]:
    """Parse a single file's patch body (no `diff --git`/`---`/`+++` headers).

    GitPython's `Diff.diff` returns just the hunk content for one file —
    walk it line-by-line, classifying each by its leading char and tracking
    old/new line numbers from the most recent `@@ -a,b +c,d @@` header.
    """
    hunks: list[DiffHunk] = []
    in_hunk = False
    hunk_header = ""
    hunk_lines: list[DiffLine] = []
    old_no = 0
    new_no = 0

    def flush() -> None:
        nonlocal hunk_lines, hunk_header, in_hunk
        if in_hunk:
            hunks.append(DiffHunk(header=hunk_header, lines=tuple(hunk_lines)))
        hunk_lines = []
        hunk_header = ""
        in_hunk = False

    for line in patch.splitlines():
        match = _HUNK_HEADER_RE.match(line)
        if match:
            flush()
            in_hunk = True
            hunk_header = line
            old_no = int(match.group(1))
            new_no = int(match.group(3))
            continue
        if not in_hunk:
            continue
        if line.startswith("\\"):
            # "\ No newline at end of file"
            hunk_lines.append(
                DiffLine(kind="noeol", old_no=None, new_no=None, text=line)
            )
            continue
        if line.startswith("+"):
            hunk_lines.append(
                DiffLine(kind="add", old_no=None, new_no=new_no, text=line[1:])
            )
            new_no += 1
        elif line.startswith("-"):
            hunk_lines.append(
                DiffLine(kind="remove", old_no=old_no, new_no=None, text=line[1:])
            )
            old_no += 1
        else:
            text = line[1:] if line.startswith(" ") else line
            hunk_lines.append(
                DiffLine(kind="context", old_no=old_no, new_no=new_no, text=text)
            )
            old_no += 1
            new_no += 1

    flush()
    return tuple(hunks)


def list_branches(bare: BareRepo) -> list[RefSummary]:
    """Every refs/heads/* with the commit it points at."""
    return _summarize_refs(bare, bare.repo.branches)


def list_tags(bare: BareRepo) -> list[RefSummary]:
    """Every refs/tags/* with the (resolved-through-tag-object) commit."""
    return _summarize_refs(bare, bare.repo.tags)


def _summarize_refs(bare: BareRepo, refs) -> list[RefSummary]:  # type: ignore[no-untyped-def]
    out: list[RefSummary] = []
    for ref in refs:
        target = ref.commit
        msg = target.message if isinstance(target.message, str) else target.message.decode()
        subject = msg.split("\n", 1)[0].strip()
        out.append(
            RefSummary(
                name=ref.name,
                full_path=ref.path,
                target_sha=target.hexsha,
                target_short_sha=target.hexsha[:7],
                target_subject=subject,
                target_authored_at=target.authored_datetime.isoformat(),
            )
        )
    out.sort(key=lambda r: r.target_authored_at, reverse=True)
    return out


def blame_blob(bare: BareRepo, ref: str, path: str) -> list[BlameLine] | None:
    """Run `git blame` and return per-line attribution. None if path absent."""
    commit = resolve_ref(bare, ref)
    if commit is None:
        return None
    node = walk_tree_at_path(commit, path)
    if node is None or node.type != "blob":
        return None
    blame = bare.repo.blame(ref, path)
    if blame is None:
        return []
    out: list[BlameLine] = []
    last_sha: str | None = None
    line_no = 0
    for blame_commit, lines in blame:
        msg = (
            blame_commit.message
            if isinstance(blame_commit.message, str)
            else blame_commit.message.decode()
        )
        subject = msg.split("\n", 1)[0].strip()
        for content in lines:
            line_no += 1
            text = content if isinstance(content, str) else content.decode(errors="replace")
            is_run_start = blame_commit.hexsha != last_sha
            out.append(
                BlameLine(
                    line_number=line_no,
                    text=text.rstrip("\n"),
                    commit_sha=blame_commit.hexsha,
                    short_sha=blame_commit.hexsha[:7],
                    author_name=blame_commit.author.name or "",
                    authored_at=blame_commit.authored_datetime.isoformat(),
                    subject=subject,
                    is_run_start=is_run_start,
                )
            )
            last_sha = blame_commit.hexsha
    return out


def _summarize(c: Commit) -> CommitSummary:
    msg = c.message if isinstance(c.message, str) else c.message.decode()
    subject = msg.split("\n", 1)[0].strip()
    return CommitSummary(
        sha=c.hexsha,
        short_sha=c.hexsha[:7],
        subject=subject,
        author_name=c.author.name or "",
        author_email=c.author.email or "",
        authored_at=c.authored_datetime.isoformat(),
        parents=tuple(p.hexsha for p in c.parents),
    )
