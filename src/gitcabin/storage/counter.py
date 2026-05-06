# ABOUTME: Monotonic id allocator backed by a single ref (refs/meta/counters).
# ABOUTME: Uses pygit2's reference transaction for an atomic locked update.

from __future__ import annotations

import threading
from dataclasses import dataclass
from pathlib import Path

import pygit2

from gitcabin.storage.repo import BareRepo

# Process-local lock per (repo_path, counter_name). The on-disk ref lock that
# pygit2's transaction acquires is what makes the counter correct across
# processes (different workers, different containers writing to a shared
# volume), but inside one process it's both faster and friendlier to the
# refdb to serialize threads with a real Lock.
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
# one ref means one contention point, but that's fine: counters are tiny
# operations and contention is rare in practice.
COUNTERS_REF = "refs/meta/counters"

# Bound on retries. Cross-process contention loses the ref lock and we retry;
# within a single process the threading.Lock above already serializes us.
MAX_RETRIES = 50

_COUNTER_SIG_NAME = "gitcabin"
_COUNTER_SIG_EMAIL = "gitcabin@localhost"


@dataclass(frozen=True, slots=True)
class Counter:
    """A monotonic id allocator stored under one entry in refs/meta/counters."""

    repo: BareRepo
    name: str

    def next(self) -> int:
        """Allocate and return the next int. Raises RuntimeError on lock exhaustion."""
        # Serialize intra-process callers; the on-disk ref lock is reserved for
        # cross-process contention (other workers / containers sharing the data
        # volume).
        with _lock_for(self.repo.path, self.name):
            pg = pygit2.Repository(str(self.repo.path))
            for _ in range(MAX_RETRIES):
                try:
                    return self._allocate(pg)
                except pygit2.GitError:
                    # Another process holds the ref lock — retry.
                    continue
        raise RuntimeError(f"counter {self.name!r}: could not allocate after {MAX_RETRIES} retries")

    def _allocate(self, pg: pygit2.Repository) -> int:
        """One attempt: lock the ref, compute the next value, commit the txn."""
        txn = pg.transaction()
        txn.lock_ref(COUNTERS_REF)

        tip_oid = pg.references[COUNTERS_REF].target if COUNTERS_REF in pg.references else None
        parent_commit = pg.get(tip_oid) if tip_oid is not None else None
        new_value = self._read_value(parent_commit) + 1

        # Hash the new value as a blob.
        blob_oid = pg.create_blob(f"{new_value}\n".encode())

        # Build the new tree: copy the existing tree (if any) and replace the
        # entry for self.name.
        tb = pg.TreeBuilder(parent_commit.tree) if parent_commit is not None else pg.TreeBuilder()
        tb.insert(self.name, blob_oid, pygit2.enums.FileMode.BLOB)
        tree_oid = tb.write()

        # Identity is set explicitly so the commit succeeds even when the
        # process inherits no git config (e.g. inside a fresh container).
        sig = pygit2.Signature(_COUNTER_SIG_NAME, _COUNTER_SIG_EMAIL)
        parents = [tip_oid] if tip_oid is not None else []
        commit_oid = pg.create_commit(
            None,  # don't update any ref directly — the txn does it
            sig,
            sig,
            f"counter {self.name} -> {new_value}",
            tree_oid,
            parents,
        )

        txn.set_target(COUNTERS_REF, commit_oid)
        txn.commit()
        return new_value

    def _read_value(self, commit: pygit2.Commit | None) -> int:
        """Read the current value for this counter, or 0 if it has none yet."""
        if commit is None:
            return 0
        try:
            entry = commit.tree[self.name]
        except KeyError:
            # Counter doesn't exist in the tree yet (first allocation for
            # this name even though the ref exists for other counters).
            return 0
        return int(entry.data.decode().strip())
