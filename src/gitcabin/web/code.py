# ABOUTME: Code-browser helpers — resolve refs/paths, render blobs, find README.
# ABOUTME: Used by the web UI; pure read-only walks over the GitPython object graph.

from __future__ import annotations

import re
from dataclasses import dataclass

import markdown
import nh3
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
    is_image: bool
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
    keeps the commit metadata; subsequent contiguous lines reuse it.

    `run_index` increments at every commit boundary so the template can
    alternate row backgrounds and make runs visually distinct.
    `highlighted_html` is the pygments-rendered HTML for this line's
    code (or None when the file's lexer wasn't found, in which case the
    template falls back to `text`).
    """

    line_number: int
    text: str
    commit_sha: str
    short_sha: str
    author_name: str
    authored_at: str
    subject: str
    is_run_start: bool
    run_index: int
    highlighted_html: str | None


def head_ref_name(bare: BareRepo) -> str | None:
    """Return the symbolic HEAD's ref name, or None on detached HEAD/empty repo."""
    try:
        return bare.repo.head.reference.name
    except (TypeError, ValueError):
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
    except (BadName, ValueError):
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
        except (KeyError, TypeError):
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


def _walk_to_prefix(tree: Tree, prefix: str) -> Tree | None:
    """Navigate from `tree` down through `prefix` (slash-separated).

    Returns the `Tree` at that subpath, or None if any segment is
    missing / not a tree. GitPython does this in-process via gitdb —
    no `git` subprocess.
    """
    if not prefix:
        return tree
    node: Tree | Blob = tree
    for segment in prefix.split("/"):
        if not segment:
            continue
        try:
            node = node[segment]  # type: ignore[index]
        except KeyError:
            return None
        if node.type != "tree":
            return None
    return node  # type: ignore[return-value]


def _changed_top_level(
    parent_tree: Tree | None, commit_tree: Tree | None
) -> set[str]:
    """Names whose contents differ between `parent_tree` and `commit_tree`.

    Compares one level deep using each entry's `binsha` — git trees are
    content-addressed, so equal binshas guarantee identical subtrees
    without needing to recurse. A name on only one side counts as
    changed. Either argument can be None (the path didn't exist on
    that side).
    """
    if (
        parent_tree is not None
        and commit_tree is not None
        and parent_tree.binsha == commit_tree.binsha
    ):
        return set()
    parent_entries = {e.name: e for e in (parent_tree or ())}
    commit_entries = {e.name: e for e in (commit_tree or ())}
    out: set[str] = set()
    for name in parent_entries.keys() | commit_entries.keys():
        pe = parent_entries.get(name)
        ce = commit_entries.get(name)
        if pe is None or ce is None or pe.binsha != ce.binsha:
            out.add(name)
    return out


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
    nested).

    Algorithm: walk commits newest-first. For each commit, navigate
    both its own tree and its first parent's tree down to `prefix`,
    then compare the resulting subtrees one level deep by entry binsha
    (git's content-addressing makes "did this commit change anything
    under prefix" an O(1) `binsha` check; only diverging subtrees pay
    the per-entry cost). Any name in our pending set that shows a
    binsha mismatch against the parent — or that exists on only one
    side — is resolved to this commit's metadata.

    Pure GitPython object-graph access via gitdb — no subprocess.
    """
    pending: dict[str, TreeEntry] = {e.name: e for e in entries}
    resolved: dict[str, tuple[str, str, str]] = {}  # name -> (sha, subject, iso)

    for c in commit.repo.iter_commits(commit.hexsha, max_count=max_commits):
        if not pending:
            break

        # Cheap commit-level skip: if the whole tree is unchanged from
        # parent, nothing under `prefix` could have changed either.
        if c.parents and c.tree.binsha == c.parents[0].tree.binsha:
            continue

        commit_subtree = _walk_to_prefix(c.tree, prefix)
        if commit_subtree is None:
            # Prefix doesn't exist in this commit; can't attribute.
            continue
        parent_subtree = (
            _walk_to_prefix(c.parents[0].tree, prefix) if c.parents else None
        )
        changed = _changed_top_level(parent_subtree, commit_subtree)
        if not changed:
            continue

        msg = c.message if isinstance(c.message, str) else c.message.decode()
        subject = msg.split("\n", 1)[0].strip()
        ts = c.authored_datetime.isoformat()
        for name in changed:
            if name in pending:
                resolved[name] = (c.hexsha, subject, ts)
                pending.pop(name)

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


_GFM_ALERT_RE = re.compile(
    r"^\[!(NOTE|TIP|IMPORTANT|WARNING|CAUTION)\]\s*", re.IGNORECASE
)


class _GfmAlertProcessor(markdown.treeprocessors.Treeprocessor):
    """Render GFM alerts — `> [!NOTE]` / `[!TIP]` / `[!IMPORTANT]` /
    `[!WARNING]` / `[!CAUTION]` — as styled callout cards the way
    GitHub does.

    Walks every `<blockquote>` after parsing. If the first paragraph
    begins with the alert marker, the marker is stripped from the
    text, the element is rewritten to a `<div class="markdown-alert
    markdown-alert-<kind>">`, and a labelled title row is prepended.
    Standard blockquotes (no marker) pass through untouched.
    """

    def run(self, root):  # type: ignore[no-untyped-def]
        from xml.etree.ElementTree import Element

        for blockquote in list(root.iter("blockquote")):
            first = next(iter(blockquote), None)
            if first is None or first.tag != "p" or not first.text:
                continue
            match = _GFM_ALERT_RE.match(first.text)
            if match is None:
                continue
            kind = match.group(1).lower()
            first.text = first.text[match.end():].lstrip() or None
            if not first.text and not list(first):
                blockquote.remove(first)
            blockquote.tag = "div"
            blockquote.set("class", f"markdown-alert markdown-alert-{kind}")
            title = Element("p", {"class": "markdown-alert-title"})
            title.text = kind.capitalize()
            blockquote.insert(0, title)


class _GfmAlertExtension(markdown.Extension):
    def extendMarkdown(self, md: markdown.Markdown) -> None:
        md.treeprocessors.register(_GfmAlertProcessor(md), "gfm-alerts", priority=8)


# Tags produced by python-markdown extensions (fenced_code, tables, toc,
# GFM alerts) that must survive sanitization, plus common inline markup.
_MARKDOWN_TAGS: frozenset[str] = frozenset(
    {
        "h1", "h2", "h3", "h4", "h5", "h6",
        "p", "br", "hr",
        "ul", "ol", "li",
        "blockquote", "div",
        "pre", "code",
        "a", "img",
        "strong", "em", "del",
        "table", "thead", "tbody", "tr", "td", "th",
        "span",
    }
)

# Attribute allowlist per tag.  `class` is needed on span/div/pre/code for
# pygments token classes and GFM alert classes; `id` on headings for toc
# anchor targets; `href`/`rel` on anchors; `src`/`alt` on images.
_MARKDOWN_ATTRIBUTES: dict[str, set[str]] = {
    "span": {"class"},
    "div": {"class"},
    "pre": {"class"},
    "code": {"class"},
    "h1": {"id"}, "h2": {"id"}, "h3": {"id"},
    "h4": {"id"}, "h5": {"id"}, "h6": {"id"},
    "a": {"href", "rel"},
    "img": {"src", "alt"},
}


def render_markdown(text: str) -> str:
    """Render Markdown to sanitized HTML.

    Raw HTML and unsafe URI schemes (javascript:, data:) are stripped so
    README content from synced upstreams cannot execute scripts in the
    browser. Structural markup produced by the fenced_code, tables, toc,
    and GFM alert extensions is preserved.
    """
    md = markdown.Markdown(
        extensions=["fenced_code", "tables", "toc", _GfmAlertExtension()]
    )
    raw_html = md.convert(text)
    return nh3.clean(
        raw_html,
        tags=_MARKDOWN_TAGS,
        clean_content_tags={"script", "style"},
        attributes=_MARKDOWN_ATTRIBUTES,
        url_schemes={"http", "https", "mailto"},
        link_rel=None,
    )


_IMAGE_EXTENSIONS = frozenset(
    [".png", ".jpg", ".jpeg", ".gif", ".webp", ".svg", ".ico", ".bmp", ".avif"]
)


def _is_image(name: str) -> bool:
    """File-extension check — used by render_blob so an image binary blob
    can be previewed inline (via the /raw/ endpoint) instead of falling
    back to the generic 'binary file' card."""
    lower = name.lower()
    return any(lower.endswith(ext) for ext in _IMAGE_EXTENSIONS)


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
            is_image=False,
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
            is_image=False,
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
            is_image=_is_image(blob.name),
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
            is_image=_is_image(blob.name),
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
        is_image=False,
        highlighted_html=html,
        raw_text=text,
    )


from gitcabin.web.pygments_css import pygments_stylesheet as pygments_stylesheet


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


def _highlight_per_line(filename: str, text: str) -> list[str] | None:
    """Highlight `text` with pygments and return one HTML string per line.

    `nowrap=True` strips the `<div><pre>` wrapper so callers can splice
    the colored spans into their own per-line cells. Returns None if no
    lexer matches the filename — caller falls back to plain text.
    """
    try:
        lexer = guess_lexer_for_filename(filename, text)
    except ClassNotFound:
        return None
    formatter = HtmlFormatter(nowrap=True)
    body = highlight(text, lexer, formatter)
    return body.split("\n")


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

    # Pygments-highlight the file once so each blame line can reuse the
    # tokenization in its own cell. Done outside the per-line loop because
    # tokenization is file-level (a string can span lines, etc).
    raw = node.data_stream.read()
    try:
        decoded = raw.decode()
    except UnicodeDecodeError:
        decoded = raw.decode("utf-8", errors="replace")
    highlighted_lines = _highlight_per_line(path.rsplit("/", 1)[-1], decoded)

    out: list[BlameLine] = []
    last_sha: str | None = None
    line_no = 0
    run_index = -1
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
            if is_run_start:
                run_index += 1
            highlighted = (
                highlighted_lines[line_no - 1]
                if highlighted_lines is not None and line_no - 1 < len(highlighted_lines)
                else None
            )
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
                    run_index=run_index,
                    highlighted_html=highlighted,
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
