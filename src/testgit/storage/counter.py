# ABOUTME: Monotonic id allocator backed by a single ref (refs/meta/counters).
# ABOUTME: Uses CAS via `git update-ref REF NEW OLD` so concurrent allocators stay correct.

from __future__ import annotations

import subprocess
import threading
from dataclasses import dataclass
from pathlib import Path

from testgit.storage.repo import BareRepo

# Process-local lock per (repo_path, counter_name). CAS via git update-ref
# is what makes the counter correct across processes (different workers,
# different containers writing to a shared volume), but inside one process
# it's both faster and friendlier to git to serialize threads with a real
# Lock. Without this, eight threads racing produced enough subprocess churn
# to blow past any reasonable retry bound.
_locks: dict[tuple[Path, str], threading.Lock] = {}
_locks_guard = threading.Lock()


def _lock_for(repo_path: Path, name: str) -> threading.Lock:
    key = (repo_path, name)
    with _locks_guard:
        lock = _locks.get(key)
        if lock is None:
            lock = threading.Lock()
            _locks[key] = lock
    return lock


# Single ref shared by all named counters in a repo. The ref points at a
# commit whose tree holds one blob per counter (e.g. "issues" → "42"). Sharing
# one ref means one CAS contention point, but that's fine: counters are tiny
# operations and contention is rare in practice.
COUNTERS_REF = "refs/meta/counters"

# git's "no current object" sentinel for update-ref CAS. Passing this as the
# expected-old-value to update-ref means "succeed only if the ref does not
# currently exist."
ZERO_OID = "0000000000000000000000000000000000000000"

# Bound on retries. With 8 threads each allocating 10 ids we observe < 20
# retries total in practice; 50 is generous for any reasonable workload and
# small enough that a real bug (a CAS that *never* succeeds) surfaces quickly.
MAX_RETRIES = 50


@dataclass(frozen=True, slots=True)
class Counter:
    """A monotonic id allocator stored under one entry in refs/meta/counters."""

    repo: BareRepo
    name: str

    def next(self) -> int:
        """Allocate and return the next int. Raises RuntimeError on CAS exhaustion."""
        # Serialize intra-process callers; CAS is reserved for cross-process
        # contention (other workers / containers sharing the data volume).
        with _lock_for(self.repo.path, self.name):
            for _ in range(MAX_RETRIES):
                current_commit = self._current_commit()
                current_value = self._read_value(current_commit)
                new_value = current_value + 1

                # Build the new tree: take the existing tree (or empty) and
                # replace this counter's blob with the new value.
                new_tree = self._build_tree(current_commit, new_value)
                new_commit = self._commit_tree(new_tree, current_commit, new_value)

                # CAS: succeeds only if the ref still points where we expected.
                # If a concurrent allocator advanced it, retry from the top.
                if self._try_update_ref(new_commit, current_commit):
                    return new_value
        raise RuntimeError(f"counter {self.name!r}: could not allocate after {MAX_RETRIES} retries")

    def _current_commit(self) -> str | None:
        """SHA of the current counters commit, or None if the ref doesn't exist."""
        result = subprocess.run(
            ["git", "rev-parse", "--verify", "--quiet", COUNTERS_REF],
            cwd=self.repo.path,
            capture_output=True,
            text=True,
            check=False,
        )
        if result.returncode != 0:
            return None
        return result.stdout.strip()

    def _read_value(self, commit: str | None) -> int:
        """Read the current value for this counter, or 0 if it has none yet."""
        if commit is None:
            return 0
        result = subprocess.run(
            ["git", "cat-file", "-p", f"{commit}:{self.name}"],
            cwd=self.repo.path,
            capture_output=True,
            text=True,
            check=False,
        )
        if result.returncode != 0:
            # Counter doesn't exist in the tree yet (first allocation for
            # this name even though the ref exists for other counters).
            return 0
        return int(result.stdout.strip())

    def _build_tree(self, parent_commit: str | None, new_value: int) -> str:
        """Produce a tree SHA that mirrors the parent's tree but with this counter updated."""
        # Hash a blob containing the new value.
        blob_sha = self.repo.run_git("hash-object", "-w", "--stdin", input=f"{new_value}\n").strip()

        # Collect existing entries (if any), drop the one we're replacing,
        # add ours, and feed the result to mktree.
        entries: list[str] = []
        if parent_commit is not None:
            ls = self.repo.run_git("ls-tree", parent_commit)
            for line in ls.splitlines():
                # Each ls-tree line: "<mode> <type> <sha>\t<name>"
                _, _, entry_name = line.partition("\t")
                if entry_name != self.name:
                    entries.append(line)
        entries.append(f"100644 blob {blob_sha}\t{self.name}")
        mktree_input = "\n".join(entries) + "\n"
        return self.repo.run_git("mktree", input=mktree_input).strip()

    def _commit_tree(self, tree_sha: str, parent_commit: str | None, new_value: int) -> str:
        """Wrap the tree in a commit so the ref has history (one commit per allocation)."""
        args = ["commit-tree", tree_sha, "-m", f"counter {self.name} -> {new_value}"]
        if parent_commit is not None:
            args.extend(["-p", parent_commit])
        # Identity is set explicitly so the commit succeeds even when the
        # process inherits no git config (e.g. inside a fresh container).
        env_args = [
            "-c",
            "user.name=testgit",
            "-c",
            "user.email=testgit@localhost",
        ]
        result = subprocess.run(
            ["git", *env_args, *args],
            cwd=self.repo.path,
            capture_output=True,
            text=True,
            check=True,
        )
        return result.stdout.strip()

    def _try_update_ref(self, new_commit: str, expected_old: str | None) -> bool:
        """CAS-update the counters ref. Returns False if the expected value didn't match."""
        old = expected_old if expected_old is not None else ZERO_OID
        result = subprocess.run(
            ["git", "update-ref", COUNTERS_REF, new_commit, old],
            cwd=self.repo.path,
            capture_output=True,
            text=True,
            check=False,
        )
        return result.returncode == 0
