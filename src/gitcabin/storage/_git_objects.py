# ABOUTME: Low-level git object helpers shared by issue, PR, and sync storage.
# ABOUTME: Wraps commit-tree, mktree, and tree walking so callers don't repeat the plumbing.

from __future__ import annotations

import os
import subprocess
from dataclasses import dataclass

from git import Blob, Commit, Tree
from git.exc import BadName

from gitcabin.storage.repo import BareRepo


def load_commit(repo: BareRepo, ref: str) -> Commit | None:
    """Resolve `ref` to a Commit, or None if the ref doesn't exist."""
    try:
        return repo.repo.commit(ref)
    except (BadName, ValueError):
        return None


def read_blob(blob: Blob) -> str:
    """Decode a GitPython blob's contents as UTF-8 text."""
    return blob.data_stream.read().decode()


def subtree_or_none(tree: Tree, name: str) -> Tree | None:
    """Return the named subtree under `tree`, or None if absent."""
    try:
        return tree[name]
    except KeyError:
        return None


@dataclass(frozen=True, slots=True)
class TreeEntry:
    mode: str
    type: str
    sha: str
    name: str


def entries_of(tree: Tree | None) -> list[TreeEntry]:
    """Materialize a tree's direct entries into mktree-friendly tuples.

    `tree=None` is treated as an empty tree — convenient for the "subtree
    didn't exist yet" case in add_comment.
    """
    if tree is None:
        return []
    out: list[TreeEntry] = []
    for entry in tree:
        # GitPython yields entry.mode as an int; mktree wants the 6-digit
        # octal form ("100644", "040000").
        out.append(
            TreeEntry(mode=f"{entry.mode:06o}", type=entry.type, sha=entry.hexsha, name=entry.name)
        )
    return out


def write_tree(repo: BareRepo, entries: list[TreeEntry]) -> str:
    """Materialize a tree object from `entries` via `git mktree`."""
    body = "".join(f"{e.mode} {e.type} {e.sha}\t{e.name}\n" for e in entries)
    return repo.run_git("mktree", input=body).strip()


def commit_tree(
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


def comment_created_at(repo: BareRepo, ref: str, name: str) -> str | None:
    """Return the ISO-8601 author date of the commit that first added `comments/<name>`.

    Comments are append-only so there's exactly one commit that added each
    comment file; --diff-filter=A picks it out without scanning history beyond
    the first match. GitPython's iter_commits supports `paths=` but doesn't
    expose --diff-filter, so we keep this as a shell-out.

    Prefer `comment_authored_dates` when you need timestamps for many comments
    on the same ref — it does one git-log walk instead of one per comment.
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


def comment_authored_dates(repo: BareRepo, ref: str) -> dict[str, str]:
    """Return {filename: ISO-8601 author date} for every comments/<name> file added on `ref`.

    One `git log --diff-filter=A` invocation, regardless of how many comments
    are on the issue — replaces the per-comment subprocess that
    `comment_created_at` does. Output shape:

        @@@<iso-date>
        comments/0001.json
        comments/0002.json
        @@@<iso-date>
        comments/0003.json
        ...

    Each `@@@`-prefixed line carries the date for the comment files that
    follow until the next `@@@`. Lines that aren't a date marker and don't
    look like a comment file (e.g. `issue.json`) are ignored.
    """
    output = repo.run_git(
        "log",
        "--diff-filter=A",
        "--reverse",
        "--name-only",
        "--format=@@@%aI",
        ref,
    )
    out: dict[str, str] = {}
    current_date: str | None = None
    for line in output.splitlines():
        if line.startswith("@@@"):
            current_date = line[3:].strip()
            continue
        if current_date and line.startswith("comments/") and line.endswith(".json"):
            name = line[len("comments/") :]
            # `setdefault` so the earliest add wins if a comment file is ever
            # re-added on a later commit — matches `comment_created_at`'s
            # `--reverse` head-of-list semantics.
            out.setdefault(name, current_date)
    return out
