# ABOUTME: Issue writer — each create is one commit on refs/issues/local/<n>.
# ABOUTME: Commits form an append-only log; the tree at the tip is the issue's current state.

from __future__ import annotations

import os
import subprocess
from dataclasses import dataclass
from enum import StrEnum

from git import Blob, Commit, Tree
from git.exc import BadName
from pydantic import BaseModel, ConfigDict

from gitcabin.storage.counter import Counter
from gitcabin.storage.repo import BareRepo

# Locally-created issues live under refs/issues/local/<n> until a future sync
# step assigns them an upstream-authoritative number and moves them to
# refs/issues/<n>. The number lives only in the ref name (not in any file
# inside the tree) so renumbering is a single `git update-ref`.
LOCAL_ISSUE_REF_PREFIX = "refs/issues/local"

# GitHub-authoritative issues — pulled from upstream — live under refs/issues/<n>
# directly. The number IS GitHub's issue number; sync code writes here, not under
# .../local. The two namespaces never collide.
ISSUE_REF_PREFIX = "refs/issues"


class IssueState(StrEnum):
    """Mirrors GitHub's IssueState enum (just OPEN/CLOSED for now)."""

    OPEN = "OPEN"
    CLOSED = "CLOSED"


class Provenance(StrEnum):
    """Where a stored item came from relative to GitHub.

    LOCAL_ONLY items live only in gitcabin and have no upstream counterpart.
    SYNCED_FROM_GITHUB items were pulled from GitHub; upstream is canonical.
    SYNCED_BIDIR items were created locally and successfully pushed; upstream
    has them too.
    """

    LOCAL_ONLY = "LOCAL_ONLY"
    SYNCED_FROM_GITHUB = "SYNCED_FROM_GITHUB"
    SYNCED_BIDIR = "SYNCED_BIDIR"


class IssueDocument(BaseModel):
    """The on-disk schema for `issue.json` inside an issue ref's tree.

    Number is deliberately absent — it's the ref name, and keeping it in only
    one place is what makes a future GitHub-authoritative renumbering on sync
    a single `git update-ref` (no payload rewrite). `extra='ignore'` keeps us
    forward-compatible with the older format that did include `number`.

    `provenance`, `gh_issue_id`, and `gh_author_id` default to LOCAL_ONLY /
    None so older payloads that predate the sync subsystem load with the
    right semantics. `gh_author_id` is the stable numeric `user.id` GitHub
    assigns; sync writes it from the upstream payload and matches against
    it (rather than the login string) when reconciling renamed users.
    """

    model_config = ConfigDict(extra="ignore")

    title: str
    body: str
    author: str
    state: IssueState
    provenance: Provenance = Provenance.LOCAL_ONLY
    gh_issue_id: int | None = None
    gh_author_id: int | None = None


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
    provenance: Provenance
    gh_issue_id: int | None
    gh_author_id: int | None


class CommentDocument(BaseModel):
    """The on-disk schema for `comments/<NNNN>.json` inside an issue tree.

    Author and body are all that lives in the blob — the comment number is the
    filename, and the timestamp is the commit's author date. Same forward-compat
    contract as IssueDocument: extra fields are ignored.

    `provenance`, `gh_comment_id`, and `gh_author_id` default to LOCAL_ONLY
    / None so older payloads that predate the sync subsystem load with the
    right semantics. `gh_author_id` is the stable numeric `user.id` from
    GitHub for rename-stable identity matching.
    """

    model_config = ConfigDict(extra="ignore")

    body: str
    author: str
    provenance: Provenance = Provenance.LOCAL_ONLY
    gh_comment_id: int | None = None
    gh_author_id: int | None = None


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
    provenance: Provenance
    gh_comment_id: int | None
    gh_author_id: int | None


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
        author_email=f"{author}@gitcabin.local",
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
    return _read_issue_at(repo.repo.commit(ref), number)


def list_issues(repo: BareRepo) -> list[Issue]:
    """Return every locally-numbered issue, sorted by number ascending.

    Walks GitPython's reference list, filtered to the local issue namespace.
    Each ref's tip commit holds `issue.json`, which deserializes back to an
    Issue record.
    """
    issues: list[Issue] = []
    for ref in repo.repo.refs:
        # Reference paths look like "refs/issues/local/<n>"; everything else
        # (heads/, tags/, meta/) is unrelated.
        if not ref.path.startswith(f"{LOCAL_ISSUE_REF_PREFIX}/"):
            continue
        number = int(ref.path.rsplit("/", 1)[-1])
        issues.append(_read_issue_at(ref.commit, number))
    issues.sort(key=lambda i: i.number)
    return issues


def get_issue(repo: BareRepo, number: int) -> Issue | None:
    """Return the issue at refs/issues/local/<number>, or None if absent."""
    commit = _load_commit(repo, _ref_for(number))
    if commit is None:
        return None
    return _read_issue_at(commit, number)


def close_issue(repo: BareRepo, *, number: int, actor: str) -> Issue | None:
    """Append a CLOSED-state event to refs/issues/local/<number>.

    Returns the refreshed Issue, or None if the issue doesn't exist. Closing
    an already-closed issue is a no-op (no commit appended) so this is safe
    to call repeatedly without polluting the log.
    """
    return _set_issue_state(
        repo, _ref_for(number), number, IssueState.CLOSED, actor=actor, verb="close"
    )


def reopen_issue(repo: BareRepo, *, number: int, actor: str) -> Issue | None:
    """Append an OPEN-state event to refs/issues/local/<number>.

    Symmetric counterpart to close_issue. Reopening an already-open issue is
    a no-op (no commit appended) so the UI's reopen button is safe to spam.
    """
    return _set_issue_state(
        repo, _ref_for(number), number, IssueState.OPEN, actor=actor, verb="reopen"
    )


def close_any_issue(repo: BareRepo, *, number: int, actor: str) -> Issue | None:
    """Close an issue in either namespace, preferring the synced ref if both exist.

    Resolves the same way as get_any_issue: synced wins on collision. The
    state flip lands locally only — pushing the closed state back to GitHub
    is a sync operation handled separately.
    """
    if _load_commit(repo, f"{ISSUE_REF_PREFIX}/{number}") is not None:
        return _set_issue_state(
            repo,
            f"{ISSUE_REF_PREFIX}/{number}",
            number,
            IssueState.CLOSED,
            actor=actor,
            verb="close",
        )
    return close_issue(repo, number=number, actor=actor)


def reopen_any_issue(repo: BareRepo, *, number: int, actor: str) -> Issue | None:
    """Reopen an issue in either namespace, preferring the synced ref if both exist."""
    if _load_commit(repo, f"{ISSUE_REF_PREFIX}/{number}") is not None:
        return _set_issue_state(
            repo,
            f"{ISSUE_REF_PREFIX}/{number}",
            number,
            IssueState.OPEN,
            actor=actor,
            verb="reopen",
        )
    return reopen_issue(repo, number=number, actor=actor)


def update_any_issue(
    repo: BareRepo, *, number: int, title: str, body: str, actor: str
) -> Issue | None:
    """Update title and body on an issue in either namespace.

    Returns the refreshed Issue, or None if the issue doesn't exist.
    No-op (no commit appended) if neither field changed.
    """
    synced_ref = f"{ISSUE_REF_PREFIX}/{number}"
    local_ref = _ref_for(number)
    if _load_commit(repo, synced_ref) is not None:
        ref = synced_ref
    elif _load_commit(repo, local_ref) is not None:
        ref = local_ref
    else:
        return None

    current = _load_commit(repo, ref)
    if current is None:
        return None
    doc = IssueDocument.model_validate_json(_read_blob(current.tree["issue.json"]))
    if doc.title == title and doc.body == body:
        return _read_issue_at(current, number)

    new_doc = doc.model_copy(update={"title": title, "body": body})
    new_payload = new_doc.model_dump_json(indent=2)
    new_blob_sha = repo.run_git("hash-object", "-w", "--stdin", input=new_payload + "\n").strip()

    new_entries = [
        _TreeEntry(mode="100644", type="blob", sha=new_blob_sha, name="issue.json")
        if e.name == "issue.json"
        else e
        for e in _entries_of(current.tree)
    ]
    new_tree_sha = _write_tree(repo, new_entries)
    commit_sha = _commit_with_identity(
        repo,
        new_tree_sha,
        message=f"edit: {title}",
        author_name=actor,
        author_email=f"{actor}@gitcabin.local",
        parents=(current.hexsha,),
    )
    repo.run_git("update-ref", ref, commit_sha, current.hexsha)
    return _read_issue_at(repo.repo.commit(ref), number)


def update_any_comment(
    repo: BareRepo,
    *,
    issue_number: int,
    comment_number: int,
    body: str,
    actor: str,
) -> Comment | None:
    """Replace `body` on a comment under either namespace.

    `comment_number` matches the int the Comment dataclass exposes — for local
    comments that's the sequential 1-based index; for synced it's the GitHub
    comment id. The on-disk filename is `<comment_number>.json` in both cases.

    Returns the refreshed Comment, or None if the issue or comment is absent.
    """
    ref = _resolve_issue_ref(repo, issue_number)
    if ref is None:
        return None
    current = _load_commit(repo, ref)
    if current is None:
        return None
    subtree = _subtree_or_none(current.tree, "comments")
    if subtree is None:
        return None

    target = _find_comment_entry(subtree, comment_number)
    if target is None:
        return None
    name = target.name

    doc = CommentDocument.model_validate_json(_read_blob(target))
    if doc.body == body:
        # No change; surface the current state without a no-op commit.
        return _comment_from_entry(repo, ref, name, doc)

    new_doc = doc.model_copy(update={"body": body})
    new_payload = new_doc.model_dump_json(indent=2)
    new_blob_sha = repo.run_git("hash-object", "-w", "--stdin", input=new_payload + "\n").strip()

    new_subtree_entries = [
        _TreeEntry(mode="100644", type="blob", sha=new_blob_sha, name=name)
        if e.name == name
        else _TreeEntry(mode=f"{e.mode:06o}", type=e.type, sha=e.hexsha, name=e.name)
        for e in subtree
    ]
    new_subtree_sha = _write_tree(repo, new_subtree_entries)
    new_top = _splice_comments_into_top(_entries_of(current.tree), new_subtree_sha)
    new_top_sha = _write_tree(repo, new_top)

    commit_sha = _commit_with_identity(
        repo,
        new_top_sha,
        message=f"edit comment by {actor}",
        author_name=actor,
        author_email=f"{actor}@gitcabin.local",
        parents=(current.hexsha,),
    )
    repo.run_git("update-ref", ref, commit_sha, current.hexsha)
    return _comment_from_entry(repo, ref, name, new_doc)


def delete_any_comment(
    repo: BareRepo, *, issue_number: int, comment_number: int, actor: str
) -> bool:
    """Delete a comment from an issue in either namespace.

    Returns True on success, False if the issue or comment doesn't exist.
    The deleted blob remains reachable via git history; only the current
    tree drops it.
    """
    ref = _resolve_issue_ref(repo, issue_number)
    if ref is None:
        return False
    current = _load_commit(repo, ref)
    if current is None:
        return False
    subtree = _subtree_or_none(current.tree, "comments")
    if subtree is None:
        return False

    target = _find_comment_entry(subtree, comment_number)
    if target is None:
        return False
    name = target.name

    new_subtree_entries = [
        _TreeEntry(mode=f"{e.mode:06o}", type=e.type, sha=e.hexsha, name=e.name)
        for e in subtree
        if e.name != name
    ]
    if new_subtree_entries:
        new_subtree_sha = _write_tree(repo, new_subtree_entries)
        new_top = _splice_comments_into_top(_entries_of(current.tree), new_subtree_sha)
    else:
        # Subtree empties out — drop the comments/ entry entirely so the tree
        # doesn't carry an empty directory.
        new_top = [e for e in _entries_of(current.tree) if e.name != "comments"]
    new_top_sha = _write_tree(repo, new_top)

    commit_sha = _commit_with_identity(
        repo,
        new_top_sha,
        message=f"delete comment by {actor}",
        author_name=actor,
        author_email=f"{actor}@gitcabin.local",
        parents=(current.hexsha,),
    )
    repo.run_git("update-ref", ref, commit_sha, current.hexsha)
    return True


def _resolve_issue_ref(repo: BareRepo, number: int) -> str | None:
    """Return the ref name for `number`, preferring synced over local."""
    synced = f"{ISSUE_REF_PREFIX}/{number}"
    if _load_commit(repo, synced) is not None:
        return synced
    local = _ref_for(number)
    if _load_commit(repo, local) is not None:
        return local
    return None


def _find_comment_entry(subtree: object, comment_number: int):
    """Find the tree entry for a given comment_number, regardless of filename style.

    Local comments are written as 4-digit zero-padded names (`0001.json`) by
    add_comment; synced comments are written as the raw GitHub id
    (`1234567890.json`) by import_comment. Both decode back to the same int,
    so the lookup walks the subtree and matches on the parsed number.
    """
    for entry in subtree:  # type: ignore[union-attr]
        if entry.type != "blob" or not entry.name.endswith(".json"):
            continue
        try:
            n = _comment_number_from_name(entry.name)
        except ValueError:
            continue
        if n == comment_number:
            return entry
    return None


def _splice_comments_into_top(
    entries: list[_TreeEntry], new_subtree_sha: str
) -> list[_TreeEntry]:
    """Replace the comments/ entry in a top-level tree, preserving order."""
    out: list[_TreeEntry] = []
    seen = False
    for entry in entries:
        if entry.name == "comments":
            out.append(
                _TreeEntry(mode="040000", type="tree", sha=new_subtree_sha, name="comments")
            )
            seen = True
        else:
            out.append(entry)
    if not seen:
        out.append(
            _TreeEntry(mode="040000", type="tree", sha=new_subtree_sha, name="comments")
        )
    return out


def _comment_from_entry(repo: BareRepo, ref: str, name: str, doc: CommentDocument) -> Comment:
    return Comment(
        number=_comment_number_from_name(name),
        body=doc.body,
        author=doc.author,
        created_at=_comment_created_at(repo, ref, name) or "",
        provenance=doc.provenance,
        gh_comment_id=doc.gh_comment_id,
        gh_author_id=doc.gh_author_id,
    )


def _set_issue_state(
    repo: BareRepo,
    ref: str,
    number: int,
    new_state: IssueState,
    *,
    actor: str,
    verb: str,
) -> Issue | None:
    """Shared body for close/reopen — append a state-flip commit if the
    current state differs, otherwise no-op.
    """
    current = _load_commit(repo, ref)
    if current is None:
        return None

    doc = IssueDocument.model_validate_json(_read_blob(current.tree["issue.json"]))
    if doc.state is new_state:
        return _read_issue_at(current, number)

    updated_doc = doc.model_copy(update={"state": new_state})
    new_payload = updated_doc.model_dump_json(indent=2)
    new_blob_sha = repo.run_git("hash-object", "-w", "--stdin", input=new_payload + "\n").strip()

    # Replace just issue.json; preserve any other top-level entries (e.g.
    # the comments/ subtree) so closing an issue with comments doesn't drop them.
    new_entries = [
        _TreeEntry(mode="100644", type="blob", sha=new_blob_sha, name=e.name)
        if e.name == "issue.json"
        else e
        for e in _entries_of(current.tree)
    ]
    new_tree_sha = _write_tree(repo, new_entries)

    commit_sha = _commit_with_identity(
        repo,
        new_tree_sha,
        message=f"{verb}: {doc.title}",
        author_name=actor,
        author_email=f"{actor}@gitcabin.local",
        parents=(current.hexsha,),
    )

    # CAS: only advance if the tip hasn't moved underneath us. A racing writer
    # would land here too and the loser gets CalledProcessError, which is the
    # right outcome — the state-flip is the user's action and ambiguity is bug-shaped.
    repo.run_git("update-ref", ref, commit_sha, current.hexsha)

    return _read_issue_at(repo.repo.commit(ref), number)


def add_comment(repo: BareRepo, *, number: int, body: str, author: str) -> Comment | None:
    """Append a comment to refs/issues/local/<number>.

    Comments live at comments/<NNNN>.json with NNNN sequential within the issue.
    Returns the new Comment, or None if the issue doesn't exist.
    """
    ref = _ref_for(number)
    current = _load_commit(repo, ref)
    if current is None:
        return None

    existing_subtree = _entries_of(_subtree_or_none(current.tree, "comments"))
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
    top_entries = _entries_of(current.tree)
    new_top_entries: list[_TreeEntry] = []
    seen_comments = False
    for entry in top_entries:
        if entry.name == "comments":
            new_top_entries.append(
                _TreeEntry(mode="040000", type="tree", sha=new_subtree_sha, name="comments")
            )
            seen_comments = True
        else:
            new_top_entries.append(entry)
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
        author_email=f"{author}@gitcabin.local",
        parents=(current.hexsha,),
    )
    repo.run_git("update-ref", ref, commit_sha, current.hexsha)

    created_at = _comment_created_at(repo, ref, comment_name) or ""
    return Comment(
        number=next_number,
        body=body,
        author=author,
        created_at=created_at,
        provenance=doc.provenance,
        gh_comment_id=doc.gh_comment_id,
        gh_author_id=doc.gh_author_id,
    )


def list_comments(repo: BareRepo, number: int) -> list[Comment]:
    """Return every comment on a local issue, ordered by number ascending.

    Empty list if the issue doesn't exist or has no comments yet.
    """
    return _list_comments_at(repo, _ref_for(number))


# ---- read helpers (object graph) --------------------------------------- #


def _ref_for(number: int) -> str:
    return f"{LOCAL_ISSUE_REF_PREFIX}/{number}"


def _load_commit(repo: BareRepo, ref: str) -> Commit | None:
    """Resolve `ref` to a Commit, or None if the ref doesn't exist."""
    try:
        return repo.repo.commit(ref)
    except BadName, ValueError:
        return None


def _read_blob(blob: Blob) -> str:
    """Decode a GitPython blob's contents as UTF-8 text."""
    return blob.data_stream.read().decode()


def _subtree_or_none(tree: Tree, name: str) -> Tree | None:
    """Return the named subtree under `tree`, or None if absent."""
    try:
        return tree[name]
    except KeyError:
        return None


def _entries_of(tree: Tree | None) -> list[_TreeEntry]:
    """Materialize a tree's direct entries into mktree-friendly tuples.

    `tree=None` is treated as an empty tree — convenient for the "subtree
    didn't exist yet" case in add_comment.
    """
    if tree is None:
        return []
    out: list[_TreeEntry] = []
    for entry in tree:
        # GitPython yields entry.mode as an int; mktree wants the 6-digit
        # octal form ("100644", "040000").
        out.append(
            _TreeEntry(mode=f"{entry.mode:06o}", type=entry.type, sha=entry.hexsha, name=entry.name)
        )
    return out


def _read_issue_at(commit: Commit, number: int) -> Issue:
    """Build an Issue from a commit pointing at an issue ref tip."""
    doc = IssueDocument.model_validate_json(_read_blob(commit.tree["issue.json"]))
    created_at, updated_at = _read_timestamps(commit)
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
        provenance=doc.provenance,
        gh_issue_id=doc.gh_issue_id,
        gh_author_id=doc.gh_author_id,
    )


def _read_timestamps(tip: Commit) -> tuple[str, str]:
    """Return (created_at, updated_at) as ISO-8601 strings.

    created_at is the root commit's author date (the create event); updated_at
    is the tip's. With one commit per issue today they're identical, but as
    soon as we append events they'll diverge.
    """
    # Walk the parent chain to the root; that's the create event. The tip
    # is the most recent event on the ref.
    root = tip
    while root.parents:
        root = root.parents[0]
    return (root.authored_datetime.isoformat(), tip.authored_datetime.isoformat())


def _comment_number_from_name(name: str) -> int:
    """`0001.json` -> 1. Caller should have already filtered to *.json entries."""
    return int(name.removesuffix(".json"))


def _comment_created_at(repo: BareRepo, ref: str, name: str) -> str | None:
    """Return the ISO-8601 author date of the commit that first added `comments/<name>`.

    Comments are append-only so there's exactly one commit that added each
    comment file; --diff-filter=A picks it out without scanning history beyond
    the first match. GitPython's iter_commits supports `paths=` but doesn't
    expose --diff-filter, so we keep this as a shell-out.
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


# ---- write helpers (plumbing shell-outs) ------------------------------ #


@dataclass(frozen=True, slots=True)
class _TreeEntry:
    mode: str
    type: str
    sha: str
    name: str


def _write_tree(repo: BareRepo, entries: list[_TreeEntry]) -> str:
    """Materialize a tree object from `entries` via `git mktree`."""
    body = "".join(f"{e.mode} {e.type} {e.sha}\t{e.name}\n" for e in entries)
    return repo.run_git("mktree", input=body).strip()


def _commit_with_identity(
    repo: BareRepo,
    tree_sha: str,
    *,
    message: str,
    author_name: str,
    author_email: str,
    parents: tuple[str, ...] = (),
    authored_at: str | None = None,
) -> str:
    """commit-tree with an explicit author/committer identity.

    Setting identity via -c overrides any process-level git config and works
    in containers where no git config is provisioned. `parents` chains this
    commit onto prior events on the same ref — empty for a create, one parent
    for every later append. `authored_at` (ISO-8601 / RFC-3339) sets the
    commit's author and committer dates so synced items can carry the
    upstream timeline; default None lets git stamp the current time.
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
    env: dict[str, str] | None = None
    if authored_at is not None:
        env = {**os.environ, "GIT_AUTHOR_DATE": authored_at, "GIT_COMMITTER_DATE": authored_at}
    result = subprocess.run(
        ["git", *args],
        cwd=repo.path,
        capture_output=True,
        text=True,
        check=True,
        env=env,
    )
    return result.stdout.strip()


# ---- sync inbound (import) --------------------------------------------- #


def import_issue(
    repo: BareRepo,
    *,
    number: int,
    title: str,
    body: str,
    author: str,
    state: IssueState,
    gh_issue_id: int,
    gh_author_id: int | None = None,
    provenance: Provenance = Provenance.SYNCED_FROM_GITHUB,
    authored_at: str | None = None,
) -> Issue:
    """Persist an issue with an externally-assigned number (e.g. from GitHub).

    Bypasses the local Counter — `number` is GitHub's issue number, used as-is.
    Writes to refs/issues/<number>, distinct from refs/issues/local/<n> where
    locally-numbered drafts live. If the ref already exists (re-pull), this
    replaces issue.json but preserves any other tree entries (notably the
    comments/ subtree the comment importer adds in a later commit).

    `authored_at` controls the commit's author + committer dates so the
    on-disk log matches the upstream timeline. Pass GitHub's `created_at`.

    `gh_author_id` is GitHub's stable numeric user.id for the issue author —
    persisted alongside the login so renames upstream are still recognisable
    after the next pull.
    """
    doc = IssueDocument(
        title=title,
        body=body,
        author=author,
        state=state,
        provenance=provenance,
        gh_issue_id=gh_issue_id,
        gh_author_id=gh_author_id,
    )
    payload = doc.model_dump_json(indent=2)
    blob_sha = repo.run_git("hash-object", "-w", "--stdin", input=payload + "\n").strip()

    ref = f"{ISSUE_REF_PREFIX}/{number}"
    parent = _load_commit(repo, ref)

    new_entry = _TreeEntry(mode="100644", type="blob", sha=blob_sha, name="issue.json")
    if parent is None:
        new_entries: list[_TreeEntry] = [new_entry]
    else:
        # Re-import: replace issue.json, keep everything else (e.g. comments/).
        existing = _entries_of(parent.tree)
        new_entries = [new_entry if e.name == "issue.json" else e for e in existing]
        if not any(e.name == "issue.json" for e in new_entries):
            new_entries.append(new_entry)
    tree_sha = _write_tree(repo, new_entries)

    parents: tuple[str, ...] = (parent.hexsha,) if parent is not None else ()
    commit_sha = _commit_with_identity(
        repo,
        tree_sha,
        message=f"sync: {title}",
        author_name=author,
        author_email=f"{author}@gitcabin.local",
        parents=parents,
        authored_at=authored_at,
    )
    repo.run_git("update-ref", ref, commit_sha)
    return _read_issue_at(repo.repo.commit(ref), number)


def get_synced_issue(repo: BareRepo, number: int) -> Issue | None:
    """Return the synced issue at refs/issues/<number>, or None if absent."""
    commit = _load_commit(repo, f"{ISSUE_REF_PREFIX}/{number}")
    if commit is None:
        return None
    return _read_issue_at(commit, number)


def import_comment(
    repo: BareRepo,
    *,
    issue_number: int,
    body: str,
    author: str,
    gh_comment_id: int,
    gh_author_id: int | None = None,
    provenance: Provenance = Provenance.SYNCED_FROM_GITHUB,
    authored_at: str | None = None,
) -> Comment | None:
    """Persist a synced comment at refs/issues/<n>:comments/<gh_comment_id>.json.

    Returns None if the issue ref doesn't exist — the caller is expected to
    have pulled the issue first. Re-importing the same gh_comment_id replaces
    the blob in place rather than duplicating, so GitHub-side edits survive
    a re-pull cleanly.

    The on-disk filename is the GitHub comment id (a stable, unique, large
    integer) rather than a sequential number — that's what makes the
    in-place update work without filename ambiguity.
    """
    ref = f"{ISSUE_REF_PREFIX}/{issue_number}"
    parent = _load_commit(repo, ref)
    if parent is None:
        return None

    doc = CommentDocument(
        body=body,
        author=author,
        provenance=provenance,
        gh_comment_id=gh_comment_id,
        gh_author_id=gh_author_id,
    )
    payload = doc.model_dump_json(indent=2)
    blob_sha = repo.run_git("hash-object", "-w", "--stdin", input=payload + "\n").strip()

    name = f"{gh_comment_id}.json"
    new_blob = _TreeEntry(mode="100644", type="blob", sha=blob_sha, name=name)

    existing_subtree = _entries_of(_subtree_or_none(parent.tree, "comments"))
    new_subtree_entries = [new_blob if e.name == name else e for e in existing_subtree]
    if not any(e.name == name for e in new_subtree_entries):
        new_subtree_entries.append(new_blob)
    new_subtree_sha = _write_tree(repo, new_subtree_entries)

    top_entries = _entries_of(parent.tree)
    new_top: list[_TreeEntry] = []
    seen_comments = False
    for entry in top_entries:
        if entry.name == "comments":
            new_top.append(
                _TreeEntry(mode="040000", type="tree", sha=new_subtree_sha, name="comments")
            )
            seen_comments = True
        else:
            new_top.append(entry)
    if not seen_comments:
        new_top.append(
            _TreeEntry(mode="040000", type="tree", sha=new_subtree_sha, name="comments")
        )
    new_top_sha = _write_tree(repo, new_top)

    commit_sha = _commit_with_identity(
        repo,
        new_top_sha,
        message=f"sync comment by {author}",
        author_name=author,
        author_email=f"{author}@gitcabin.local",
        parents=(parent.hexsha,),
        authored_at=authored_at,
    )
    repo.run_git("update-ref", ref, commit_sha)

    created_at = _comment_created_at(repo, ref, name) or (authored_at or "")
    return Comment(
        number=gh_comment_id,
        body=body,
        author=author,
        created_at=created_at,
        provenance=provenance,
        gh_comment_id=gh_comment_id,
        gh_author_id=gh_author_id,
    )


def list_synced_comments(repo: BareRepo, issue_number: int) -> list[Comment]:
    """Return every comment on the synced issue at refs/issues/<n>, ordered by id."""
    return _list_comments_at(repo, f"{ISSUE_REF_PREFIX}/{issue_number}")


def list_synced_issues(repo: BareRepo) -> list[Issue]:
    """Return every issue at refs/issues/<n> (the synced namespace), sorted by number.

    Distinct from list_issues, which walks refs/issues/local/<n>. Callers that
    want both namespaces unified should use list_all_issues.
    """
    issues: list[Issue] = []
    prefix = f"{ISSUE_REF_PREFIX}/"
    for ref in repo.repo.refs:
        if not ref.path.startswith(prefix):
            continue
        # Skip refs/issues/local/<n> — that's the local namespace, walked
        # separately by list_issues.
        if ref.path.startswith(LOCAL_ISSUE_REF_PREFIX + "/"):
            continue
        try:
            number = int(ref.path.removeprefix(prefix))
        except ValueError:
            continue
        issues.append(_read_issue_at(ref.commit, number))
    issues.sort(key=lambda i: i.number)
    return issues


def list_all_issues(repo: BareRepo) -> list[Issue]:
    """Return every issue across both namespaces, sorted with synced first.

    Synced issues appear before local-only ones so a viewer reading the list
    sees what's published before what's still in draft. Within each namespace,
    sorted by number ascending.
    """
    return list_synced_issues(repo) + list_issues(repo)


def get_any_issue(repo: BareRepo, number: int) -> Issue | None:
    """Return the issue with `number`, preferring the synced namespace.

    For repos linked to GitHub, the synced ref is what callers usually want —
    a synced issue numbered `n` carries upstream provenance, while a local
    issue numbered `n` is a draft that hasn't been pushed. If both exist
    (rare; only happens before push), synced wins.
    """
    synced = get_synced_issue(repo, number)
    if synced is not None:
        return synced
    return get_issue(repo, number)


def list_any_comments(repo: BareRepo, issue_number: int) -> list[Comment]:
    """Return comments for an issue from whichever namespace it lives in.

    Mirrors get_any_issue — checks the synced namespace first, falling back
    to local. Callers that already know which namespace to use should call
    list_comments or list_synced_comments directly.
    """
    if _load_commit(repo, f"{ISSUE_REF_PREFIX}/{issue_number}") is not None:
        return list_synced_comments(repo, issue_number)
    return list_comments(repo, issue_number)


def _list_comments_at(repo: BareRepo, ref: str) -> list[Comment]:
    """Walk the comments/ subtree at `ref` and materialize each blob as a Comment."""
    commit = _load_commit(repo, ref)
    if commit is None:
        return []
    subtree = _subtree_or_none(commit.tree, "comments")
    if subtree is None:
        return []
    comments: list[Comment] = []
    for entry in subtree:
        if entry.type != "blob" or not entry.name.endswith(".json"):
            continue
        n = _comment_number_from_name(entry.name)
        doc = CommentDocument.model_validate_json(_read_blob(entry))
        created_at = _comment_created_at(repo, ref, entry.name) or ""
        comments.append(
            Comment(
                number=n,
                body=doc.body,
                author=doc.author,
                created_at=created_at,
                provenance=doc.provenance,
                gh_comment_id=doc.gh_comment_id,
                gh_author_id=doc.gh_author_id,
            )
        )
    comments.sort(key=lambda c: c.number)
    return comments
