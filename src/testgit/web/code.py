# ABOUTME: Code-browser helpers — resolve refs/paths, render blobs, find README.
# ABOUTME: Used by the web UI; pure read-only walks over the GitPython object graph.

from __future__ import annotations

from dataclasses import dataclass

import markdown
from git import Blob, Commit, Tree
from git.exc import BadName
from pygments import highlight
from pygments.formatters import HtmlFormatter
from pygments.lexers import get_lexer_by_name, guess_lexer_for_filename
from pygments.util import ClassNotFound

from testgit.storage.repo import BareRepo

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
    """One row in a file-listing view (a tree subdir or a blob)."""

    name: str
    type: str  # "tree" or "blob"
    sha: str
    size: int | None  # bytes for blobs, None for trees
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
    diff_html: str | None = None
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


def list_tree_entries(tree: Tree) -> list[TreeEntry]:
    """List a tree's direct children. Trees come first (alphabetical), then blobs."""
    trees: list[TreeEntry] = []
    blobs: list[TreeEntry] = []
    for entry in tree:
        if entry.type == "tree":
            trees.append(TreeEntry(name=entry.name, type="tree", sha=entry.hexsha, size=None))
        else:
            blobs.append(TreeEntry(name=entry.name, type="blob", sha=entry.hexsha, size=entry.size))
    trees.sort(key=lambda e: e.name)
    blobs.sort(key=lambda e: e.name)
    return trees + blobs


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
    """Decode and syntax-highlight a blob, with size and binary fallbacks."""
    raw = blob.data_stream.read()
    if len(raw) > MAX_BLOB_RENDER_BYTES:
        return RenderedBlob(
            name=blob.name,
            size=blob.size,
            is_binary=False,
            is_too_large=True,
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
            highlighted_html=None,
            raw_text=None,
        )

    # Pick a lexer by filename when we can; fall back to plain text.
    try:
        lexer = guess_lexer_for_filename(blob.name, text)
    except ClassNotFound:
        lexer = get_lexer_by_name("text")
    formatter = HtmlFormatter(linenos="table", cssclass="hl", wrapcode=True)
    html = highlight(text, lexer, formatter)
    return RenderedBlob(
        name=blob.name,
        size=blob.size,
        is_binary=False,
        is_too_large=False,
        highlighted_html=html,
        raw_text=text,
    )


def pygments_stylesheet() -> str:
    """The CSS Pygments needs for the `.hl` class our formatter emits."""
    return HtmlFormatter(cssclass="hl").get_style_defs(".hl")


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
    diff_html: str | None = None
    diff_truncated = False

    if commit.parents:
        diffs = commit.parents[0].diff(commit, create_patch=True)
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
        # Stitch every per-file patch together for a single highlighted block.
        # Cap total size; oversized diffs (e.g. lockfile bumps) just print the
        # changed-files list with a "diff omitted" notice.
        chunks: list[bytes] = []
        running = 0
        for d in diffs:
            patch = d.diff if isinstance(d.diff, bytes) else (d.diff or b"")
            running += len(patch)
            if running > MAX_DIFF_RENDER_BYTES:
                diff_truncated = True
                break
            if patch:
                chunks.append(patch)
        if chunks and not diff_truncated:
            diff_html = _render_diff_html(b"\n".join(chunks).decode(errors="replace"))
        elif diff_truncated:
            diff_html = None
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
        diff_html=diff_html,
        diff_truncated=diff_truncated,
    )


def _render_diff_html(patch: str) -> str:
    """Highlight a unified diff with Pygments' diff lexer."""
    lexer = get_lexer_by_name("diff")
    formatter = HtmlFormatter(cssclass="hl", wrapcode=True)
    return highlight(patch, lexer, formatter)


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
