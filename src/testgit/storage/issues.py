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
    # Use rev-parse --verify --quiet to detect the missing-ref case; checking
    # by string against for-each-ref output would be O(n) per lookup.
    result = subprocess.run(
        ["git", "rev-parse", "--verify", "--quiet", ref],
        cwd=repo.path,
        capture_output=True,
        text=True,
        check=False,
    )
    if result.returncode != 0:
        return None
    return _read_issue(repo, ref, number)


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
    repo: BareRepo, tree_sha: str, *, message: str, author_name: str, author_email: str
) -> str:
    """commit-tree with an explicit author/committer identity.

    Setting identity via -c overrides any process-level git config and works
    in containers where no git config is provisioned.
    """
    args = [
        "-c",
        f"user.name={author_name}",
        "-c",
        f"user.email={author_email}",
        "commit-tree",
        tree_sha,
        "-m",
        message,
    ]
    result = subprocess.run(
        ["git", *args],
        cwd=repo.path,
        capture_output=True,
        text=True,
        check=True,
    )
    return result.stdout.strip()
