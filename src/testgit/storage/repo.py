# ABOUTME: Thin wrapper over a bare git repo directory; shells out to git plumbing.
# ABOUTME: No pygit2/dulwich — git's CLI is the spec, and shelling out keeps deps minimal.

from __future__ import annotations

import subprocess
from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True, slots=True)
class BareRepo:
    """A handle to a bare git repository at `path`.

    The bare repo is the source of truth for everything — code refs
    (refs/heads/*, refs/tags/*) and metadata refs (refs/issues/*, refs/prs/*,
    refs/meta/*). All git operations run with cwd=path so we never have to
    worry about GIT_DIR.
    """

    path: Path

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
            subprocess.check_call(
                ["git", "init", "--bare", "--quiet", "--initial-branch=main", str(path)]
            )
            return cls(path=path)

        # Already exists — confirm it's a bare repo before trusting it.
        result = subprocess.run(
            ["git", "rev-parse", "--is-bare-repository"],
            cwd=path,
            capture_output=True,
            text=True,
            check=False,
        )
        if result.returncode != 0:
            # Path exists but isn't a git repo at all — initialize it.
            subprocess.check_call(
                ["git", "init", "--bare", "--quiet", "--initial-branch=main", str(path)]
            )
            return cls(path=path)

        if result.stdout.strip() != "true":
            raise ValueError(f"{path} exists but is not a bare repo")

        return cls(path=path)

    @classmethod
    def open(cls, path: Path) -> BareRepo | None:
        """Return a handle if `path` is an existing bare repo, else None.

        Strictly read-only: never initializes. Use `open_or_init` when the
        caller is allowed to create the repo.
        """
        path = Path(path)
        if not path.is_dir():
            return None
        result = subprocess.run(
            ["git", "rev-parse", "--is-bare-repository"],
            cwd=path,
            capture_output=True,
            text=True,
            check=False,
        )
        if result.returncode != 0 or result.stdout.strip() != "true":
            return None
        return cls(path=path)

    def run_git(self, *args: str, input: str | None = None) -> str:
        """Run a git command with cwd=self.path and return stdout (text).

        Raises CalledProcessError on non-zero exit so callers don't have to
        check return codes manually. Pass `input` for stdin (used by
        plumbing commands like `git hash-object -w --stdin`).
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
