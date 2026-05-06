# ABOUTME: Repo-wide timestamp helpers (created_at / pushed_at) read from git refs.
# ABOUTME: Single source for the web list view and the GraphQL Repository resolvers.

from __future__ import annotations

import subprocess
from datetime import UTC, datetime

from gitcabin.storage.repo import BareRepo


def repo_timestamps(bare: BareRepo) -> tuple[str, str]:
    """Return (created_at, pushed_at) as ISO-8601 strings.

    pushed_at is the latest commit author date across all branches: a
    one-shot `git log --max-count=1` returns it in O(1). created_at is the
    oldest reachable commit's author date: `--max-parents=0` filters to
    root commits (typically one per repo) and we take the oldest among
    them. This stays O(roots) which is effectively O(1) for real repos.

    `--reverse --max-count=1` does NOT work for finding the oldest commit:
    git applies --max-count during traversal (newest-first) and only then
    reverses, so the result is still the newest commit.

    With no commits at all (a fresh `git init`), both timestamps fall back
    to the bare directory's mtime so the field still has a real value
    callers can render.
    """
    try:
        newest = bare.run_git(
            "log", "--all", "--max-count=1", "--format=%aI"
        ).strip()
        # Root-commit dates, one per line; pick the smallest. There's
        # almost always exactly one (the initial commit) but a repo could
        # have multiple roots from grafted branches.
        roots = bare.run_git(
            "log", "--all", "--max-parents=0", "--format=%aI"
        ).strip()
    except subprocess.CalledProcessError:
        newest = roots = ""
    if not newest:
        mtime = datetime.fromtimestamp(bare.path.stat().st_mtime, tz=UTC).isoformat()
        return (mtime, mtime)
    oldest = min(roots.splitlines()) if roots else newest
    return (oldest, newest)


def repo_pushed_at(bare: BareRepo) -> str:
    """ISO timestamp for the latest commit on any branch, or the dir mtime."""
    return repo_timestamps(bare)[1]
