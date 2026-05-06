# ABOUTME: PR storage — synced PRs at refs/prs/<gh>; local drafts at refs/prs/local/<n>.
# ABOUTME: Same shape as issues plus head/base/draft/merged; comment subtree shared.

from __future__ import annotations

import os
import subprocess
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path

from git import Commit
from git.exc import BadName
from pydantic import BaseModel, ConfigDict

from gitcabin.storage.counter import Counter
from gitcabin.storage.issues import (
    Comment,
    CommentDocument,
    Provenance,
    _comment_created_at,
    _entries_of,
    _list_comments_at,
    _load_commit,
    _read_blob,
    _subtree_or_none,
    _TreeEntry,
    _write_tree,
)
from gitcabin.storage.repo import BareRepo

# Synced (GitHub-authoritative) PRs live under refs/prs/<gh_number>. PRs
# drafted locally that haven't been pushed yet live under refs/prs/local/<n>
# with a counter-allocated number; sync push renumbers them onto the
# upstream-authoritative slot once GitHub assigns one. The two namespaces
# never collide: refs/prs/<n> contains only synced PRs, refs/prs/local/<n>
# only local ones.
PR_REF_PREFIX = "refs/prs"
LOCAL_PR_REF_PREFIX = "refs/prs/local"


class PrState(StrEnum):
    """PR state. GitHub treats merged as a special case of closed; we surface
    it as a first-class state for display purposes."""

    OPEN = "OPEN"
    CLOSED = "CLOSED"
    MERGED = "MERGED"


class PrDocument(BaseModel):
    """The on-disk schema for `pr.json` inside a synced PR ref's tree.

    Number is absent for the same reason as IssueDocument — it lives in the
    ref name. Same forward-compat contract: `extra='ignore'` keeps older or
    newer payloads loading.
    """

    model_config = ConfigDict(extra="ignore")

    title: str
    body: str
    author: str
    state: PrState
    head_ref: str
    base_ref: str
    is_draft: bool = False
    provenance: Provenance = Provenance.SYNCED_FROM_GITHUB
    gh_pr_id: int | None = None


@dataclass(frozen=True, slots=True, kw_only=True)
class Pr:
    """A persisted pull request, returned from the writer for resolvers."""

    number: int
    title: str
    body: str
    author: str
    state: PrState
    head_ref: str
    base_ref: str
    is_draft: bool
    created_at: str
    updated_at: str
    provenance: Provenance
    gh_pr_id: int | None = None


def import_pr(
    repo: BareRepo,
    *,
    number: int,
    title: str,
    body: str,
    author: str,
    state: PrState,
    head_ref: str,
    base_ref: str,
    is_draft: bool,
    gh_pr_id: int,
    provenance: Provenance = Provenance.SYNCED_FROM_GITHUB,
    authored_at: str | None = None,
) -> Pr:
    """Persist a synced PR at refs/prs/<number>.

    Re-imports replace pr.json but preserve any other tree entries (notably
    the comments/ subtree the comment importer adds). authored_at controls
    the commit's author and committer dates so the on-disk log carries the
    upstream timeline.
    """
    doc = PrDocument(
        title=title,
        body=body,
        author=author,
        state=state,
        head_ref=head_ref,
        base_ref=base_ref,
        is_draft=is_draft,
        provenance=provenance,
        gh_pr_id=gh_pr_id,
    )
    payload = doc.model_dump_json(indent=2)
    blob_sha = repo.run_git("hash-object", "-w", "--stdin", input=payload + "\n").strip()

    ref = f"{PR_REF_PREFIX}/{number}"
    parent = _load_commit(repo, ref)

    new_entry = _TreeEntry(mode="100644", type="blob", sha=blob_sha, name="pr.json")
    if parent is None:
        new_entries: list[_TreeEntry] = [new_entry]
    else:
        existing = _entries_of(parent.tree)
        new_entries = [new_entry if e.name == "pr.json" else e for e in existing]
        if not any(e.name == "pr.json" for e in new_entries):
            new_entries.append(new_entry)
    tree_sha = _write_tree(repo, new_entries)

    parents: tuple[str, ...] = (parent.hexsha,) if parent is not None else ()
    commit_sha = _commit(
        repo,
        tree_sha,
        message=f"sync pr: {title}",
        author_name=author,
        author_email=f"{author}@gitcabin.local",
        parents=parents,
        authored_at=authored_at,
    )
    repo.run_git("update-ref", ref, commit_sha)
    return _read_pr_at(repo.repo.commit(ref), number, repo)


def get_synced_pr(repo: BareRepo, number: int) -> Pr | None:
    """Return the synced PR at refs/prs/<n>, or None if absent."""
    commit = _load_commit(repo, f"{PR_REF_PREFIX}/{number}")
    if commit is None:
        return None
    return _read_pr_at(commit, number, repo)


def list_synced_prs(repo: BareRepo) -> list[Pr]:
    """Return every synced PR at refs/prs/<n>, sorted by number ascending.

    Skips refs/prs/local/<n> entries — they're walked separately by
    list_local_prs. The int(suffix) parse is what distinguishes them:
    `local/3` doesn't parse as an int, so it falls out of this listing.
    """
    out: list[Pr] = []
    prefix = f"{PR_REF_PREFIX}/"
    for ref in repo.repo.refs:
        if not ref.path.startswith(prefix):
            continue
        try:
            number = int(ref.path.removeprefix(prefix))
        except ValueError:
            continue
        out.append(_read_pr_at(ref.commit, number, repo))
    out.sort(key=lambda p: p.number)
    return out


def create_local_pr(
    repo: BareRepo,
    *,
    title: str,
    body: str,
    author: str,
    head_ref: str,
    base_ref: str,
    is_draft: bool = False,
) -> Pr:
    """Persist a new local-only PR at refs/prs/local/<n>.

    Number is allocated from the "prs" Counter ref — independent of the
    issues counter, so a project can have local issue #3 and local PR #3
    without collision until a sync renumbers them onto upstream slots.

    `head_ref` and `base_ref` are the branch labels the PR points at.
    gitcabin doesn't push code; the head branch must already exist on
    GitHub before a sync push of this PR can succeed (GitHub returns 422
    otherwise). That constraint is documented in docs/github-sync.md.
    """
    number = Counter(repo, "prs").next()
    doc = PrDocument(
        title=title,
        body=body,
        author=author,
        state=PrState.OPEN,
        head_ref=head_ref,
        base_ref=base_ref,
        is_draft=is_draft,
        provenance=Provenance.LOCAL_ONLY,
        gh_pr_id=None,
    )
    payload = doc.model_dump_json(indent=2)
    blob_sha = repo.run_git("hash-object", "-w", "--stdin", input=payload + "\n").strip()
    tree_sha = repo.run_git("mktree", input=f"100644 blob {blob_sha}\tpr.json\n").strip()

    commit_sha = _commit(
        repo,
        tree_sha,
        message=f"create pr: {title}",
        author_name=author,
        author_email=f"{author}@gitcabin.local",
    )
    ref = f"{LOCAL_PR_REF_PREFIX}/{number}"
    # Zero-OID expected-old enforces "this ref must not yet exist"; the
    # Counter's CAS already prevents number reuse but this is defense in
    # depth, mirroring the create_issue pattern.
    repo.run_git("update-ref", ref, commit_sha, "0000000000000000000000000000000000000000")
    return _read_pr_at(repo.repo.commit(ref), number, repo)


def list_local_prs(repo: BareRepo) -> list[Pr]:
    """Return every local-only PR at refs/prs/local/<n>, sorted by number."""
    out: list[Pr] = []
    prefix = f"{LOCAL_PR_REF_PREFIX}/"
    for ref in repo.repo.refs:
        if not ref.path.startswith(prefix):
            continue
        try:
            number = int(ref.path.removeprefix(prefix))
        except ValueError:
            continue
        out.append(_read_pr_at(ref.commit, number, repo))
    out.sort(key=lambda p: p.number)
    return out


def list_all_prs(repo: BareRepo) -> list[Pr]:
    """Return every PR across both namespaces, synced first then local.

    Mirrors list_all_issues' ordering: published items appear before drafts,
    so the GraphQL Repository.pullRequests connection reads naturally for a
    user looking at their PR queue.
    """
    return list_synced_prs(repo) + list_local_prs(repo)


def get_any_pr(repo: BareRepo, number: int) -> Pr | None:
    """Return the PR with `number`, preferring synced over local on collision.

    Same dispatch as get_any_issue — synced wins because it carries upstream
    provenance, while a local PR with the same number is a draft that hasn't
    been pushed.
    """
    synced = get_synced_pr(repo, number)
    if synced is not None:
        return synced
    commit = _load_commit(repo, f"{LOCAL_PR_REF_PREFIX}/{number}")
    if commit is None:
        return None
    return _read_pr_at(commit, number, repo)


def import_pr_comment(
    repo: BareRepo,
    *,
    pr_number: int,
    body: str,
    author: str,
    gh_comment_id: int,
    gh_author_id: int | None = None,
    provenance: Provenance = Provenance.SYNCED_FROM_GITHUB,
    authored_at: str | None = None,
) -> Comment | None:
    """Persist a synced comment on a PR at refs/prs/<n>:comments/<gh_id>.json.

    Returns None if the PR ref doesn't exist — caller is expected to have
    pulled the PR first. Re-importing the same gh_comment_id replaces the
    blob in place, mirroring the issue-comment behavior.
    """
    ref = f"{PR_REF_PREFIX}/{pr_number}"
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
    seen = False
    for entry in top_entries:
        if entry.name == "comments":
            new_top.append(
                _TreeEntry(mode="040000", type="tree", sha=new_subtree_sha, name="comments")
            )
            seen = True
        else:
            new_top.append(entry)
    if not seen:
        new_top.append(
            _TreeEntry(mode="040000", type="tree", sha=new_subtree_sha, name="comments")
        )
    new_top_sha = _write_tree(repo, new_top)

    commit_sha = _commit(
        repo,
        new_top_sha,
        message=f"sync pr-comment by {author}",
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


def list_synced_pr_comments(repo: BareRepo, pr_number: int) -> list[Comment]:
    """Return every comment on the synced PR at refs/prs/<n>, ordered by id."""
    return _list_comments_at(repo, f"{PR_REF_PREFIX}/{pr_number}")


# ---- internals -------------------------------------------------------- #


def _read_pr_at(commit: Commit, number: int, repo: BareRepo) -> Pr:
    doc = PrDocument.model_validate_json(_read_blob(commit.tree["pr.json"]))
    created_at, updated_at = _read_pr_timestamps(commit)
    _ = repo  # kept in the signature for future use (consistency with _read_issue_at)
    return Pr(
        number=number,
        title=doc.title,
        body=doc.body,
        author=doc.author,
        state=doc.state,
        head_ref=doc.head_ref,
        base_ref=doc.base_ref,
        is_draft=doc.is_draft,
        created_at=created_at,
        updated_at=updated_at,
        provenance=doc.provenance,
        gh_pr_id=doc.gh_pr_id,
    )


def _read_pr_timestamps(tip: Commit) -> tuple[str, str]:
    """(created_at, updated_at) for a PR ref. Same logic as _read_timestamps in issues.py."""
    root = tip
    while root.parents:
        root = root.parents[0]
    return (root.authored_datetime.isoformat(), tip.authored_datetime.isoformat())


def _commit(
    repo: BareRepo,
    tree_sha: str,
    *,
    message: str,
    author_name: str,
    author_email: str,
    parents: tuple[str, ...] = (),
    authored_at: str | None = None,
) -> str:
    """commit-tree wrapper with explicit identity, mirrors issues._commit_with_identity."""
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
        cwd=Path(repo.path),
        capture_output=True,
        text=True,
        check=True,
        env=env,
    )
    return result.stdout.strip()


_ = (BadName,)  # re-export-friendly; keeps the import non-stale for future use
