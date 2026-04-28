# ABOUTME: Issue writer — each create is one commit on refs/issues/local/<n>.
# ABOUTME: Commits form an append-only log; the tree at the tip is the issue's current state.

from __future__ import annotations

import subprocess
from dataclasses import dataclass
from enum import StrEnum

from pydantic import BaseModel, ConfigDict

from testgit.storage.counter import Counter
from testgit.storage.repo import BareRepo

# Locally-created issues live under refs/issues/local/<n> until a future sync
# step assigns them an upstream-authoritative number and moves them to
# refs/issues/<n>. The number lives only in the ref name (not in any file
# inside the tree) so renumbering is a single `git update-ref`.
LOCAL_ISSUE_REF_PREFIX = "refs/issues/local"


class IssueState(StrEnum):
    """Mirrors GitHub's IssueState enum (just OPEN/CLOSED for now)."""

    OPEN = "OPEN"
    CLOSED = "CLOSED"


class IssueDocument(BaseModel):
    """The on-disk schema for `issue.json` inside an issue ref's tree.

    Number is deliberately absent — it's the ref name, and keeping it in only
    one place is what makes a future GitHub-authoritative renumbering on sync
    a single `git update-ref` (no payload rewrite). `extra='ignore'` keeps us
    forward-compatible with the older format that did include `number`.
    """

    model_config = ConfigDict(extra="ignore")

    title: str
    body: str
    author: str
    state: IssueState


@dataclass(frozen=True, slots=True)
class Issue:
    """A persisted issue, returned from the writer for use by GraphQL resolvers.

    Combines the IssueDocument fields with metadata derived from git itself:
    the number from the ref name, and the ISO-8601 timestamps from the commit
    log. Kept as a separate type from IssueDocument because these extras
    aren't in the file — they're computed from the surrounding git state.
    """

    number: int
    title: str
    body: str
    author: str
    state: IssueState
    created_at: str
    updated_at: str


class CommentDocument(BaseModel):
    """The on-disk schema for `comments/<NNNN>.json` inside an issue tree.

    Author and body are all that lives in the blob — the comment number is the
    filename, and the timestamp is the commit's author date. Same forward-compat
    contract as IssueDocument: extra fields are ignored.
    """

    model_config = ConfigDict(extra="ignore")

    body: str
    author: str


@dataclass(frozen=True, slots=True)
class Comment:
    """A comment on an issue.

    `number` is sequential within the issue (1-based, ordered by creation).
    `created_at` is the ISO-8601 author date of the commit that introduced
    the comment blob.
    """

    number: int
    body: str
    author: str
    created_at: str


def create_issue(repo: BareRepo, *, title: str, body: str, author: str) -> Issue:
    """Persist a new issue to refs/issues/local/<n> and return its Issue record.

    The first event in the issue's log is "create" — the commit message and
    author/date encode that. Future events (comment, label, close) will append
    additional commits to the same ref.
    """
    number = Counter(repo, "issues").next()

    # 1. Hash the issue.json blob into the object database. The pydantic
    #    model produces canonical JSON with stable field order, which keeps
    #    blob hashes diff-able across writes.
    doc = IssueDocument(title=title, body=body, author=author, state=IssueState.OPEN)
    payload = doc.model_dump_json(indent=2)
    blob_sha = repo.run_git("hash-object", "-w", "--stdin", input=payload + "\n").strip()

    # 2. Build a tree containing just the issue.json blob. Future events that
    #    add comments/ or events/ entries will produce richer trees.
    tree_input = f"100644 blob {blob_sha}\tissue.json\n"
    tree_sha = repo.run_git("mktree", input=tree_input).strip()

    # 3. Wrap in a commit. Author identity comes from the issue's author so
    #    `git log refs/issues/local/<n>` reads as a real audit trail.
    #    No parent — this is the first event in the log; subsequent events
    #    will pass -p <previous_event>.
    commit_sha = _commit_with_identity(
        repo,
        tree_sha,
        message=f"create: {title}",
        author_name=author,
        author_email=f"{author}@testgit.local",
    )

    # 4. Create the ref. We use update-ref with the zero-OID sentinel so two
    #    racing creates can't both claim the same number — though Counter's
    #    own CAS already prevents that, this is defense in depth.
    ref = f"{LOCAL_ISSUE_REF_PREFIX}/{number}"
    repo.run_git(
        "update-ref",
        ref,
        commit_sha,
        "0000000000000000000000000000000000000000",
    )

    # 5. Read back so callers get the full Issue record including timestamps,
    #    avoiding any drift between the synthesized record and what list_issues
    #    will return for the same ref.
    return _read_issue(repo, ref, number)


def list_issues(repo: BareRepo) -> list[Issue]:
    """Return every locally-numbered issue, sorted by number ascending.

    Walks `refs/issues/local/*` via `git for-each-ref`. The ref's tip tree
    holds `issue.json`, which deserializes back to an Issue record.
    """
    output = repo.run_git(
        "for-each-ref",
        "--format=%(refname)",
        f"{LOCAL_ISSUE_REF_PREFIX}/*",
    )
    refs = [line.strip() for line in output.splitlines() if line.strip()]

    issues: list[Issue] = []
    for ref in refs:
        # Last path component is the issue number; the ref's tip tree holds
        # issue.json with the rest.
        number = int(ref.rsplit("/", 1)[-1])
        issues.append(_read_issue(repo, ref, number))

    issues.sort(key=lambda i: i.number)
    return issues


def get_issue(repo: BareRepo, number: int) -> Issue | None:
    """Return the issue at refs/issues/local/<number>, or None if absent."""
    ref = f"{LOCAL_ISSUE_REF_PREFIX}/{number}"
    if _rev_parse(repo, ref) is None:
        return None
    return _read_issue(repo, ref, number)


def close_issue(repo: BareRepo, *, number: int, actor: str) -> Issue | None:
    """Append a CLOSED-state event to refs/issues/local/<number>.

    Returns the refreshed Issue, or None if the issue doesn't exist. Closing
    an already-closed issue is a no-op (no commit appended) so this is safe
    to call repeatedly without polluting the log.
    """
    ref = f"{LOCAL_ISSUE_REF_PREFIX}/{number}"
    current_tip = _rev_parse(repo, ref)
    if current_tip is None:
        return None

    raw = repo.run_git("cat-file", "-p", f"{ref}:issue.json")
    doc = IssueDocument.model_validate_json(raw)
    if doc.state is IssueState.CLOSED:
        return _read_issue(repo, ref, number)

    closed_doc = doc.model_copy(update={"state": IssueState.CLOSED})
    new_payload = closed_doc.model_dump_json(indent=2)
    new_blob_sha = repo.run_git("hash-object", "-w", "--stdin", input=new_payload + "\n").strip()

    # Replace just issue.json; preserve any other top-level entries (e.g.
    # the comments/ subtree) so closing an issue with comments doesn't drop them.
    entries = _read_tree(repo, ref)
    new_entries = [
        _TreeEntry(mode=e.mode, type=e.type, sha=new_blob_sha, name=e.name)
        if e.name == "issue.json"
        else e
        for e in entries
    ]
    new_tree_sha = _write_tree(repo, new_entries)

    commit_sha = _commit_with_identity(
        repo,
        new_tree_sha,
        message=f"close: {doc.title}",
        author_name=actor,
        author_email=f"{actor}@testgit.local",
        parents=(current_tip,),
    )

    # CAS: only advance if the tip hasn't moved underneath us. A racing close
    # would land here too and the loser gets CalledProcessError, which is the
    # right outcome — the close is the user's action and ambiguity is bug-shaped.
    repo.run_git("update-ref", ref, commit_sha, current_tip)

    return _read_issue(repo, ref, number)


def add_comment(repo: BareRepo, *, number: int, body: str, author: str) -> Comment | None:
    """Append a comment to refs/issues/local/<number>.

    Comments live at comments/<NNNN>.json with NNNN sequential within the issue.
    Returns the new Comment, or None if the issue doesn't exist.
    """
    ref = f"{LOCAL_ISSUE_REF_PREFIX}/{number}"
    current_tip = _rev_parse(repo, ref)
    if current_tip is None:
        return None

    existing_subtree = _read_subtree(repo, ref, "comments")
    existing_numbers = sorted(_comment_number_from_name(e.name) for e in existing_subtree)
    next_number = (existing_numbers[-1] + 1) if existing_numbers else 1

    doc = CommentDocument(body=body, author=author)
    payload = doc.model_dump_json(indent=2)
    blob_sha = repo.run_git("hash-object", "-w", "--stdin", input=payload + "\n").strip()

    comment_name = f"{next_number:04d}.json"
    new_subtree_entries = [
        *existing_subtree,
        _TreeEntry(mode="100644", type="blob", sha=blob_sha, name=comment_name),
    ]
    new_subtree_sha = _write_tree(repo, new_subtree_entries)

    # Splice the new comments/ subtree into the top-level tree, preserving
    # the existing issue.json entry. If comments/ didn't exist before, append it.
    top_entries = _read_tree(repo, ref)
    new_top_entries: list[_TreeEntry] = []
    seen_comments = False
    for e in top_entries:
        if e.name == "comments":
            new_top_entries.append(
                _TreeEntry(mode="040000", type="tree", sha=new_subtree_sha, name="comments")
            )
            seen_comments = True
        else:
            new_top_entries.append(e)
    if not seen_comments:
        new_top_entries.append(
            _TreeEntry(mode="040000", type="tree", sha=new_subtree_sha, name="comments")
        )
    new_top_sha = _write_tree(repo, new_top_entries)

    commit_sha = _commit_with_identity(
        repo,
        new_top_sha,
        message=f"comment: by {author}",
        author_name=author,
        author_email=f"{author}@testgit.local",
        parents=(current_tip,),
    )
    repo.run_git("update-ref", ref, commit_sha, current_tip)

    created_at = _comment_created_at(repo, ref, comment_name) or ""
    return Comment(number=next_number, body=body, author=author, created_at=created_at)


def list_comments(repo: BareRepo, number: int) -> list[Comment]:
    """Return every comment on the issue, ordered by number ascending.

    Empty list if the issue doesn't exist or has no comments yet.
    """
    ref = f"{LOCAL_ISSUE_REF_PREFIX}/{number}"
    if _rev_parse(repo, ref) is None:
        return []
    entries = _read_subtree(repo, ref, "comments")
    comments: list[Comment] = []
    for entry in entries:
        if entry.type != "blob" or not entry.name.endswith(".json"):
            continue
        n = _comment_number_from_name(entry.name)
        raw = repo.run_git("cat-file", "-p", entry.sha)
        doc = CommentDocument.model_validate_json(raw)
        created_at = _comment_created_at(repo, ref, entry.name) or ""
        comments.append(Comment(number=n, body=doc.body, author=doc.author, created_at=created_at))
    comments.sort(key=lambda c: c.number)
    return comments


def _read_issue(repo: BareRepo, ref: str, number: int) -> Issue:
    raw = repo.run_git("cat-file", "-p", f"{ref}:issue.json")
    doc = IssueDocument.model_validate_json(raw)
    created_at, updated_at = _read_timestamps(repo, ref)
    # `number` comes from the ref name (the authoritative source), not from
    # the payload — older files may carry it but newer ones don't.
    return Issue(
        number=number,
        title=doc.title,
        body=doc.body,
        author=doc.author,
        state=doc.state,
        created_at=created_at,
        updated_at=updated_at,
    )


def _read_timestamps(repo: BareRepo, ref: str) -> tuple[str, str]:
    """Return (created_at, updated_at) as ISO-8601 strings.

    created_at is the root commit's author date (the create event); updated_at
    is the tip's. With one commit per issue today they're identical, but as
    soon as we append events they'll diverge.
    """
    # `git log --reverse --format=%aI <ref>` lists every commit's author date
    # from oldest to newest. First line is created_at, last is updated_at.
    out = repo.run_git("log", "--reverse", "--format=%aI", ref).splitlines()
    if not out:
        # Defensive: a ref that exists but has no commits should be impossible,
        # but if it ever happens, return a deterministic value rather than
        # crashing.
        return ("1970-01-01T00:00:00+00:00", "1970-01-01T00:00:00+00:00")
    return (out[0], out[-1])


def _commit_with_identity(
    repo: BareRepo,
    tree_sha: str,
    *,
    message: str,
    author_name: str,
    author_email: str,
    parents: tuple[str, ...] = (),
) -> str:
    """commit-tree with an explicit author/committer identity.

    Setting identity via -c overrides any process-level git config and works
    in containers where no git config is provisioned. `parents` chains this
    commit onto prior events on the same ref — empty for a create, one parent
    for every later append.
    """
    args = [
        "-c",
        f"user.name={author_name}",
        "-c",
        f"user.email={author_email}",
        "commit-tree",
        tree_sha,
    ]
    for parent in parents:
        args += ["-p", parent]
    args += ["-m", message]
    result = subprocess.run(
        ["git", *args],
        cwd=repo.path,
        capture_output=True,
        text=True,
        check=True,
    )
    return result.stdout.strip()


# ---- tree manipulation helpers ----------------------------------------- #


@dataclass(frozen=True, slots=True)
class _TreeEntry:
    mode: str
    type: str
    sha: str
    name: str


def _rev_parse(repo: BareRepo, ref: str) -> str | None:
    """Return the commit sha at `ref`, or None if `ref` doesn't exist."""
    result = subprocess.run(
        ["git", "rev-parse", "--verify", "--quiet", ref],
        cwd=repo.path,
        capture_output=True,
        text=True,
        check=False,
    )
    if result.returncode != 0:
        return None
    return result.stdout.strip()


def _read_tree(repo: BareRepo, ref: str) -> list[_TreeEntry]:
    """List the top-level entries of the tree at `ref`."""
    return _parse_ls_tree(repo.run_git("ls-tree", ref))


def _read_subtree(repo: BareRepo, ref: str, subtree_path: str) -> list[_TreeEntry]:
    """List entries inside `<ref>:<subtree_path>`, or [] if the subtree is absent."""
    result = subprocess.run(
        ["git", "ls-tree", f"{ref}:{subtree_path}"],
        cwd=repo.path,
        capture_output=True,
        text=True,
        check=False,
    )
    if result.returncode != 0:
        return []
    return _parse_ls_tree(result.stdout)


def _parse_ls_tree(output: str) -> list[_TreeEntry]:
    entries: list[_TreeEntry] = []
    for line in output.splitlines():
        if not line:
            continue
        # ls-tree default format: "<mode> SP <type> SP <sha> TAB <name>"
        meta, name = line.split("\t", 1)
        mode, type_, sha = meta.split()
        entries.append(_TreeEntry(mode=mode, type=type_, sha=sha, name=name))
    return entries


def _write_tree(repo: BareRepo, entries: list[_TreeEntry]) -> str:
    """Materialize a tree object from `entries` via `git mktree`."""
    body = "".join(f"{e.mode} {e.type} {e.sha}\t{e.name}\n" for e in entries)
    return repo.run_git("mktree", input=body).strip()


def _comment_number_from_name(name: str) -> int:
    """`0001.json` -> 1. Caller should have already filtered to *.json entries."""
    return int(name.removesuffix(".json"))


def _comment_created_at(repo: BareRepo, ref: str, name: str) -> str | None:
    """Return the ISO-8601 author date of the commit that first added `comments/<name>`.

    Comments are append-only so there's exactly one commit that added each
    comment file; --diff-filter=A picks it out without scanning history beyond
    the first match.
    """
    out = repo.run_git(
        "log",
        "--diff-filter=A",
        "--reverse",
        "--format=%aI",
        ref,
        "--",
        f"comments/{name}",
    ).splitlines()
    return out[0] if out else None
