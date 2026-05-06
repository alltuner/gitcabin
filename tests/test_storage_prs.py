# ABOUTME: Tests for gitcabin.storage.prs — synced pull-request storage.
# ABOUTME: Real bare repos in tmp_path; round-trip via git plumbing where useful.

from __future__ import annotations

import json
from pathlib import Path

import pytest

from gitcabin.storage.issues import Provenance
from gitcabin.storage.prs import (
    PR_REF_PREFIX,
    Pr,
    PrDocument,
    PrState,
    get_synced_pr,
    import_pr,
    import_pr_comment,
    list_synced_pr_comments,
    list_synced_prs,
)
from gitcabin.storage.repo import BareRepo


@pytest.fixture
def repo(tmp_path: Path) -> BareRepo:
    return BareRepo.open_or_init(tmp_path / "octo" / "hello.git")


def test_import_pr_writes_to_refs_prs(repo: BareRepo) -> None:
    import_pr(
        repo,
        number=42,
        title="Add tests",
        body="b",
        author="alice",
        state=PrState.OPEN,
        head_ref="alice:feature",
        base_ref="main",
        is_draft=False,
        gh_pr_id=12345,
    )
    sha = repo.run_git("rev-parse", f"{PR_REF_PREFIX}/42").strip()
    assert sha


def test_import_pr_persists_full_document(repo: BareRepo) -> None:
    import_pr(
        repo,
        number=42,
        title="Add tests",
        body="my body",
        author="alice",
        state=PrState.OPEN,
        head_ref="alice:feature",
        base_ref="main",
        is_draft=True,
        gh_pr_id=12345,
    )
    raw = repo.run_git("cat-file", "-p", f"{PR_REF_PREFIX}/42:pr.json")
    payload = json.loads(raw)
    assert payload == {
        "title": "Add tests",
        "body": "my body",
        "author": "alice",
        "state": "OPEN",
        "head_ref": "alice:feature",
        "base_ref": "main",
        "is_draft": True,
        "provenance": "SYNCED_FROM_GITHUB",
        "gh_pr_id": 12345,
    }


def test_get_synced_pr_returns_none_when_absent(repo: BareRepo) -> None:
    assert get_synced_pr(repo, 1) is None


def test_get_synced_pr_returns_pr_after_import(repo: BareRepo) -> None:
    import_pr(
        repo,
        number=42,
        title="t",
        body="b",
        author="alice",
        state=PrState.MERGED,
        head_ref="x",
        base_ref="main",
        is_draft=False,
        gh_pr_id=900,
    )
    pr = get_synced_pr(repo, 42)
    assert isinstance(pr, Pr)
    assert pr.number == 42
    assert pr.state is PrState.MERGED
    assert pr.head_ref == "x"
    assert pr.base_ref == "main"


def test_re_import_replaces_pr_json_but_preserves_comments(repo: BareRepo) -> None:
    import_pr(
        repo,
        number=1,
        title="v1",
        body="",
        author="alice",
        state=PrState.OPEN,
        head_ref="x",
        base_ref="main",
        is_draft=False,
        gh_pr_id=1,
    )
    import_pr_comment(repo, pr_number=1, body="c1", author="bob", gh_comment_id=555)

    import_pr(
        repo,
        number=1,
        title="v2",
        body="",
        author="alice",
        state=PrState.MERGED,
        head_ref="x",
        base_ref="main",
        is_draft=False,
        gh_pr_id=1,
    )

    pr = get_synced_pr(repo, 1)
    assert pr is not None
    assert pr.title == "v2"
    assert pr.state is PrState.MERGED
    # Comment survived.
    [comment] = list_synced_pr_comments(repo, 1)
    assert comment.body == "c1"


def test_list_synced_prs_returns_each_in_number_order(repo: BareRepo) -> None:
    import_pr(
        repo,
        number=5,
        title="five",
        body="",
        author="x",
        state=PrState.OPEN,
        head_ref="a",
        base_ref="main",
        is_draft=False,
        gh_pr_id=51,
    )
    import_pr(
        repo,
        number=2,
        title="two",
        body="",
        author="x",
        state=PrState.OPEN,
        head_ref="a",
        base_ref="main",
        is_draft=False,
        gh_pr_id=21,
    )
    prs = list_synced_prs(repo)
    assert [p.number for p in prs] == [2, 5]


def test_pr_document_loads_legacy_payload_with_extra_fields_ignored() -> None:
    legacy = (
        '{"title": "t", "body": "b", "author": "a", "state": "OPEN", '
        '"head_ref": "x", "base_ref": "main", "future_field": "ignored"}'
    )
    doc = PrDocument.model_validate_json(legacy)
    assert doc.title == "t"
    assert doc.is_draft is False  # default
    assert doc.provenance is Provenance.SYNCED_FROM_GITHUB  # default


def test_import_pr_comment_returns_none_when_pr_ref_missing(repo: BareRepo) -> None:
    assert (
        import_pr_comment(
            repo, pr_number=99, body="hi", author="alice", gh_comment_id=1
        )
        is None
    )


def test_pr_comment_uses_gh_id_filename(repo: BareRepo) -> None:
    import_pr(
        repo,
        number=1,
        title="t",
        body="",
        author="alice",
        state=PrState.OPEN,
        head_ref="x",
        base_ref="main",
        is_draft=False,
        gh_pr_id=11,
    )
    import_pr_comment(repo, pr_number=1, body="reply", author="bob", gh_comment_id=999)
    raw = repo.run_git("cat-file", "-p", f"{PR_REF_PREFIX}/1:comments/999.json")
    payload = json.loads(raw)
    assert payload == {
        "body": "reply",
        "author": "bob",
        "provenance": "SYNCED_FROM_GITHUB",
        "gh_comment_id": 999,
        "gh_author_id": None,
    }
