# ABOUTME: Single-blob ref read/write helper for sync metadata refs.
# ABOUTME: Each write hashes a JSON payload, builds a one-entry tree, and advances the ref.

from __future__ import annotations

from gitcabin.storage._git_objects import load_commit
from gitcabin.storage.repo import BareRepo

# Sync-side commits all carry this synthetic identity so the audit log on
# refs/meta/* clearly attributes them to the sync subsystem rather than to
# whichever user happened to trigger the write.
_SYNC_AUTHOR_NAME = "gitcabin-sync"
_SYNC_AUTHOR_EMAIL = "sync@gitcabin.local"


def read_meta_blob(repo: BareRepo, ref: str, filename: str) -> str | None:
    """Return the text contents of `<ref>:<filename>`, or None if the ref is absent."""
    commit = load_commit(repo, ref)
    if commit is None:
        return None
    blob = commit.tree[filename]
    return blob.data_stream.read().decode()


def write_meta_blob(
    repo: BareRepo, ref: str, filename: str, payload: str, *, message: str
) -> None:
    """Hash `payload` as a blob, wrap it in a one-entry tree, commit on top of the
    current tip (if any), and advance `ref`.

    The commit identity is the synthetic gitcabin-sync user — these refs are
    machine-managed metadata, not user-attributable history.
    """
    blob_sha = repo.run_git("hash-object", "-w", "--stdin", input=payload + "\n").strip()
    tree_sha = repo.run_git("mktree", input=f"100644 blob {blob_sha}\t{filename}\n").strip()

    args: list[str] = [
        "-c",
        f"user.name={_SYNC_AUTHOR_NAME}",
        "-c",
        f"user.email={_SYNC_AUTHOR_EMAIL}",
        "commit-tree",
        tree_sha,
        "-m",
        message,
    ]
    parent = load_commit(repo, ref)
    if parent is not None:
        args += ["-p", parent.hexsha]

    commit_sha = repo.run_git(*args).strip()
    repo.run_git("update-ref", ref, commit_sha)
