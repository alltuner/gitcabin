# ABOUTME: Issue writer — each create is one commit on refs/issues/local/<n>.
# ABOUTME: Commits form an append-only log; the tree at the tip is the issue's current state.

from __future__ import annotations

import json
import subprocess
from dataclasses import dataclass
from enum import StrEnum

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


@dataclass(frozen=True, slots=True)
class Issue:
    """A persisted issue, returned from the writer for use by GraphQL resolvers."""

    number: int
    title: str
    body: str
    author: str
    state: IssueState


def create_issue(repo: BareRepo, *, title: str, body: str, author: str) -> Issue:
    """Persist a new issue to refs/issues/local/<n> and return its Issue record.

    The first event in the issue's log is "create" — the commit message and
    author/date encode that. Future events (comment, label, close) will append
    additional commits to the same ref.
    """
    number = Counter(repo, "issues").next()
    issue = Issue(number=number, title=title, body=body, author=author, state=IssueState.OPEN)

    # 1. Hash the issue.json blob into the object database.
    payload = json.dumps(
        {
            "number": issue.number,
            "title": issue.title,
            "body": issue.body,
            "author": issue.author,
            "state": issue.state.value,
        },
        indent=2,
    )
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
        message=f"create: {issue.title}",
        author_name=author,
        author_email=f"{author}@testgit.local",
    )

    # 4. Create the ref. We use update-ref with the zero-OID sentinel so two
    #    racing creates can't both claim the same number — though Counter's
    #    own CAS already prevents that, this is defense in depth.
    repo.run_git(
        "update-ref",
        f"{LOCAL_ISSUE_REF_PREFIX}/{number}",
        commit_sha,
        "0000000000000000000000000000000000000000",
    )
    return issue


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
    payload = json.loads(raw)
    # The number in the file should match the ref name; we trust the ref name
    # as authoritative and pass `number` in so renumbering on sync is just a
    # ref move (no payload rewrite needed).
    return Issue(
        number=number,
        title=payload["title"],
        body=payload["body"],
        author=payload["author"],
        state=IssueState(payload["state"]),
    )


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
