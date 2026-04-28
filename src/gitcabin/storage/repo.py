# ABOUTME: Thin wrapper over a bare git repo directory.
# ABOUTME: Reads go through GitPython's object graph; plumbing writes shell out via repo.git.

from __future__ import annotations

import subprocess
from dataclasses import dataclass, field
from pathlib import Path

from git import Repo


@dataclass(frozen=True, slots=True)
class BareRepo:
    """A handle to a bare git repository at `path`.

    The bare repo is the source of truth for everything — code refs
    (refs/heads/*, refs/tags/*) and metadata refs (refs/issues/*, refs/prs/*,
    refs/meta/*). Reads use the GitPython object graph (`self.repo.commit(...)`,
    `commit.tree[...]`); plumbing-style writes (hash-object, mktree, commit-tree,
    update-ref) go through `run_git` because GitPython doesn't add value over
    shelling out for those.
    """

    path: Path
    repo: Repo = field(compare=False, hash=False, repr=False)

    @classmethod
    def open_or_init(cls, path: Path) -> BareRepo:
        """Return a handle, creating a bare repo at `path` if it doesn't exist.

        Idempotent: calling on an existing bare repo is a no-op. Raises
        ValueError if `path` exists but is a non-bare git repo, because that
        indicates a misconfiguration the caller needs to know about — we'd
        otherwise silently start writing metadata refs to someone's working tree.
        """
        path = Path(path)
        if not path.exists():
            path.mkdir(parents=True)
            repo = Repo.init(path, bare=True, initial_branch="main")
            return cls(path=path, repo=repo)

        try:
            repo = Repo(path)
        except Exception:
            # Path exists but isn't a git repo at all — initialize it.
            repo = Repo.init(path, bare=True, initial_branch="main")
            return cls(path=path, repo=repo)

        if not repo.bare:
            raise ValueError(f"{path} exists but is not a bare repo")

        return cls(path=path, repo=repo)

    @classmethod
    def open(cls, path: Path) -> BareRepo | None:
        """Return a handle if `path` is an existing bare repo, else None.

        Strictly read-only: never initializes. Use `open_or_init` when the
        caller is allowed to create the repo.
        """
        path = Path(path)
        if not path.is_dir():
            return None
        try:
            repo = Repo(path)
        except Exception:
            return None
        if not repo.bare:
            return None
        return cls(path=path, repo=repo)

    def run_git(self, *args: str, input: str | None = None) -> str:
        """Run a git plumbing command and return stdout (text).

        Used for plumbing writes that GitPython doesn't wrap usefully —
        hash-object -w --stdin, mktree, commit-tree with parents, and CAS
        update-ref REF NEW OLD all need raw stdin or precise exit-code
        semantics that subprocess.run handles cleanly. Reads should go
        through the object graph (`self.repo.commit(...)`, `commit.tree[...]`)
        instead.
        """
        result = subprocess.run(
            ["git", *args],
            cwd=self.path,
            input=input,
            capture_output=True,
            text=True,
            check=True,
        )
        return result.stdout
