# ABOUTME: Tests for crash-safe / resumable behavior of gitcabin.sync.push.
# ABOUTME: Forces failures at each step in the protocol and verifies retry doesn't double-publish.

from __future__ import annotations

import json
from collections.abc import Callable
from pathlib import Path

import pytest

from gitcabin.storage.issues import (
    LOCAL_ISSUE_REF_PREFIX,
    Provenance,
    add_comment,
    create_issue,
    get_synced_issue,
    list_synced_comments,
)
from gitcabin.storage.repo import BareRepo
from gitcabin.sync.config import SyncConfig
from gitcabin.sync.gh import GhClient
from gitcabin.sync.pending import read_pending
from gitcabin.sync.push import push_local_issues


@pytest.fixture
def repo(tmp_path: Path) -> BareRepo:
    return BareRepo.open_or_init(tmp_path / "octo" / "hello.git")


@pytest.fixture
def config() -> SyncConfig:
    return SyncConfig(
        gh_owner="octo", gh_name="hello", gh_viewer_login="alice-on-github"
    )


class _CountingGh:
    """gh fake that tallies POSTs by path so retry-double-post is observable."""

    def __init__(
        self,
        *,
        first_issue_number: int = 41,
        first_comment_id: int = 8_100_000_000,
        fail_after_issue_post: bool = False,
        fail_after_comment_index: int | None = None,
    ) -> None:
        self.calls: list[tuple[list[str], str | None]] = []
        self.issues_posted = 0
        self.comments_posted = 0
        self.next_issue_number = first_issue_number
        self.next_issue_id = 9_000_000
        self.next_comment_id = first_comment_id
        self.fail_after_issue_post = fail_after_issue_post
        self.fail_after_comment_index = fail_after_comment_index

    def __call__(self, argv: list[str], *, stdin: str | None = None) -> str:
        self.calls.append((list(argv), stdin))
        method = self._method(argv)
        path = argv[-1]
        body = json.loads(stdin) if stdin else {}

        if method == "POST" and path.endswith("/issues"):
            self.issues_posted += 1
            number = self.next_issue_number
            self.next_issue_number += 1
            gh_id = self.next_issue_id
            self.next_issue_id += 1
            response = json.dumps(
                {
                    "number": number,
                    "id": gh_id,
                    "title": body.get("title", ""),
                    "body": body.get("body", ""),
                    "state": "open",
                    "created_at": "2026-05-04T10:00:00Z",
                    "user": {"login": "alice-on-github"},
                }
            )
            if self.fail_after_issue_post:
                # Hand the response back to the runner first — push records the
                # gh_number into pending — then *next* call (the comment POST)
                # raises. We simulate that by storing the response and raising
                # on the *next* invocation rather than this one.
                self._poison_next = True
            return response

        if method == "POST" and "/issues/" in path and path.endswith("/comments"):
            if getattr(self, "_poison_next", False):
                # Crash before any comment POST lands; pending state has only
                # the issue record at this point.
                raise RuntimeError("simulated crash after issue POST")
            if (
                self.fail_after_comment_index is not None
                and self.comments_posted == self.fail_after_comment_index
            ):
                raise RuntimeError("simulated crash mid-comment-loop")
            self.comments_posted += 1
            issue_number = int(path.rsplit("/", 2)[-2])
            cid = self.next_comment_id
            self.next_comment_id += 1
            return json.dumps(
                {
                    "id": cid,
                    "body": body.get("body", ""),
                    "issue_url": f"https://api.github.com/repos/octo/hello/issues/{issue_number}",
                    "created_at": "2026-05-04T10:00:01Z",
                    "user": {"login": "alice-on-github"},
                }
            )

        raise AssertionError(f"unhandled fake gh call: {argv}")

    @staticmethod
    def _method(argv: list[str]) -> str:
        if "-X" in argv:
            return argv[argv.index("-X") + 1]
        return "GET"


def _client_for(fake: _CountingGh) -> GhClient:
    runner: Callable[..., str] = fake
    return GhClient(runner=runner)


def test_crash_after_issue_post_does_not_re_post_on_retry(
    repo: BareRepo, config: SyncConfig
) -> None:
    create_issue(repo, title="t", body="", author="david")
    add_comment(repo, number=1, body="reply", author="david")

    crashing = _CountingGh(fail_after_issue_post=True)
    with pytest.raises(RuntimeError, match="simulated crash"):
        push_local_issues(repo, _client_for(crashing), config)

    # The issue POST went through; pending state now holds the upstream slot.
    assert crashing.issues_posted == 1
    pending = read_pending(repo)
    assert f"{LOCAL_ISSUE_REF_PREFIX}/1" in pending.issues
    assert pending.issues[f"{LOCAL_ISSUE_REF_PREFIX}/1"].gh_number == 41

    # Local ref is still in place because cleanup hasn't run yet.
    assert repo.run_git("for-each-ref", LOCAL_ISSUE_REF_PREFIX) != ""

    # Retry: a fresh runner that no longer crashes. Critically, this run must
    # NOT POST another issue — it should reuse the gh_number from pending.
    healthy = _CountingGh(first_issue_number=99)  # different number-space; if
    # the retry POSTs again, the synced ref would land at 99, not 41.
    pushed = push_local_issues(repo, _client_for(healthy), config)
    assert healthy.issues_posted == 0  # the load-bearing assertion
    assert healthy.comments_posted == 1  # comments did need to go up

    # Final state lands at the *original* gh_number (41), not the retry's 99.
    assert len(pushed) == 1
    assert pushed[0].number == 41
    issue = get_synced_issue(repo, 41)
    assert issue is not None
    assert issue.provenance is Provenance.SYNCED_BIDIR

    # Pending entry cleared after the protocol completed.
    assert read_pending(repo).issues == {}
    # Local ref retired.
    assert repo.run_git("for-each-ref", LOCAL_ISSUE_REF_PREFIX) == ""


def test_crash_mid_comment_loop_does_not_double_post_earlier_comments(
    repo: BareRepo, config: SyncConfig
) -> None:
    # Three comments — crash after the second one POSTs, before the third.
    create_issue(repo, title="t", body="", author="david")
    add_comment(repo, number=1, body="c1", author="david")
    add_comment(repo, number=1, body="c2", author="david")
    add_comment(repo, number=1, body="c3", author="david")

    crashing = _CountingGh(fail_after_comment_index=2)
    with pytest.raises(RuntimeError, match="mid-comment-loop"):
        push_local_issues(repo, _client_for(crashing), config)

    # Two comments made it upstream before the crash.
    assert crashing.comments_posted == 2
    pending = read_pending(repo).issues[f"{LOCAL_ISSUE_REF_PREFIX}/1"]
    assert [c.local_index for c in pending.comments] == [0, 1]
    saved_ids = [c.gh_id for c in pending.comments]

    # Retry: only the third comment should POST. The first two are reused.
    # Allocate the retry's comment-ids well above the first attempt's range so
    # a collision (which would silently overwrite an earlier comment blob,
    # since blobs are keyed by gh_comment_id) is impossible.
    healthy = _CountingGh(first_issue_number=999, first_comment_id=9_900_000_000)
    push_local_issues(repo, _client_for(healthy), config)
    assert healthy.issues_posted == 0
    assert healthy.comments_posted == 1  # only the third

    comments = list_synced_comments(repo, 41)
    bodies = [c.body for c in comments]
    assert sorted(bodies) == ["c1", "c2", "c3"]
    # The first two carry the gh_ids minted in the *original* attempt.
    saved_ids_set = set(saved_ids)
    matched = [c.gh_comment_id for c in comments if c.gh_comment_id in saved_ids_set]
    assert sorted(matched) == sorted(saved_ids)
