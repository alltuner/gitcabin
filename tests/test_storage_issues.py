# ABOUTME: Tests for the issue writer that persists each create as a real ref.
# ABOUTME: Real bare repos in tmp_path; commits are inspected via plain git plumbing.

from __future__ import annotations

import json
from pathlib import Path

import pytest

from gitcabin.storage.issues import (
    Comment,
    CommentDocument,
    Issue,
    IssueDocument,
    IssueState,
    Provenance,
    add_comment,
    close_issue,
    create_issue,
    get_any_issue,
    get_issue,
    import_comment,
    import_issue,
    list_all_issues,
    list_any_comments,
    list_comments,
    list_issues,
    list_synced_issues,
)
from gitcabin.storage.repo import BareRepo


@pytest.fixture
def repo(tmp_path: Path) -> BareRepo:
    return BareRepo.open_or_init(tmp_path / "octocat" / "hello.git")


def test_create_issue_returns_issue_with_number_one(repo: BareRepo) -> None:
    issue = create_issue(repo, title="First", body="Hello", author="david")
    assert isinstance(issue, Issue)
    assert issue.number == 1
    assert issue.title == "First"
    assert issue.body == "Hello"
    assert issue.author == "david"
    assert issue.state is IssueState.OPEN


def test_create_issue_increments_number(repo: BareRepo) -> None:
    a = create_issue(repo, title="a", body="", author="david")
    b = create_issue(repo, title="b", body="", author="david")
    c = create_issue(repo, title="c", body="", author="david")
    assert [a.number, b.number, c.number] == [1, 2, 3]


def test_create_issue_writes_local_ref(repo: BareRepo) -> None:
    issue = create_issue(repo, title="ref check", body="", author="david")
    # Refs for not-yet-synced issues live under refs/issues/local/<n>; the
    # bare ref refs/issues/<n> is reserved for issues whose number is
    # authoritative (i.e. mirrored from upstream GitHub later on).
    sha = repo.run_git("rev-parse", f"refs/issues/local/{issue.number}").strip()
    assert sha, "refs/issues/local/<n> must point to a commit"


def test_create_issue_tree_contains_issue_json(repo: BareRepo) -> None:
    issue = create_issue(repo, title="Title", body="Body text", author="david")
    raw = repo.run_git("cat-file", "-p", f"refs/issues/local/{issue.number}:issue.json")
    payload = json.loads(raw)
    # Number is intentionally absent — it lives only in the ref name so a
    # future GitHub-authoritative renumbering on sync is a single ref move.
    assert payload == {
        "title": "Title",
        "body": "Body text",
        "author": "david",
        "state": "OPEN",
        "provenance": "LOCAL_ONLY",
        "gh_issue_id": None,
    }


def test_create_issue_commit_carries_author_metadata(repo: BareRepo) -> None:
    # The commit's author identity comes from the issue's author. This is
    # what makes `git log refs/issues/local/N` readable as an event log:
    # each entry shows who did what at what time.
    issue = create_issue(repo, title="t", body="b", author="alice")
    author_line = repo.run_git(
        "log", "-1", "--format=%an <%ae>", f"refs/issues/local/{issue.number}"
    ).strip()
    assert author_line.startswith("alice <")


def test_list_issues_returns_empty_for_fresh_repo(repo: BareRepo) -> None:
    assert list_issues(repo) == []


def test_list_issues_returns_all_creates_sorted_by_number(repo: BareRepo) -> None:
    create_issue(repo, title="first", body="", author="david")
    create_issue(repo, title="second", body="", author="david")
    create_issue(repo, title="third", body="", author="david")

    issues = list_issues(repo)
    assert [i.number for i in issues] == [1, 2, 3]
    assert [i.title for i in issues] == ["first", "second", "third"]


def test_list_issues_preserves_state_and_author(repo: BareRepo) -> None:
    create_issue(repo, title="t", body="b", author="alice")
    [issue] = list_issues(repo)
    assert isinstance(issue, Issue)
    assert issue.author == "alice"
    assert issue.state is IssueState.OPEN
    assert issue.body == "b"


def test_get_issue_returns_the_named_issue(repo: BareRepo) -> None:
    create_issue(repo, title="one", body="", author="david")
    create_issue(repo, title="two", body="", author="david")

    issue = get_issue(repo, 2)
    assert issue is not None
    assert issue.number == 2
    assert issue.title == "two"


def test_get_issue_returns_none_for_unknown_number(repo: BareRepo) -> None:
    assert get_issue(repo, 999) is None


def test_get_issue_returns_none_when_repo_has_no_issues(repo: BareRepo) -> None:
    assert get_issue(repo, 1) is None


def test_legacy_issue_json_with_extra_number_field_loads(repo: BareRepo) -> None:
    # Older writes embedded `number` inside issue.json. We've stopped writing
    # it, but existing data must keep loading — the IssueDocument model uses
    # extra='ignore' specifically to keep the on-disk format forward-compatible.
    legacy_payload = json.dumps(
        {
            "number": 7,
            "title": "Legacy",
            "body": "from older format",
            "author": "david",
            "state": "OPEN",
        },
        indent=2,
    )
    blob_sha = repo.run_git("hash-object", "-w", "--stdin", input=legacy_payload + "\n").strip()
    tree_sha = repo.run_git("mktree", input=f"100644 blob {blob_sha}\tissue.json\n").strip()
    commit_sha = repo.run_git(
        "-c",
        "user.name=test",
        "-c",
        "user.email=test@example.com",
        "commit-tree",
        tree_sha,
        "-m",
        "synthetic legacy",
    ).strip()
    repo.run_git("update-ref", "refs/issues/local/7", commit_sha)

    issue = get_issue(repo, 7)
    assert issue is not None
    assert issue.title == "Legacy"
    assert issue.body == "from older format"


def test_issue_carries_iso_timestamps(repo: BareRepo) -> None:
    # gh's IssueList query selects updatedAt; gh issue view also wants
    # createdAt. Both come from git commit metadata as ISO-8601 strings,
    # which need to round-trip through GraphQL untouched.
    issue = create_issue(repo, title="t", body="", author="david")
    assert issue.created_at
    assert issue.updated_at
    # ISO-8601 always starts with 4-digit year and a dash.
    assert issue.created_at[:5].endswith("-")
    # Today's create has only one event, so created_at == updated_at.
    assert issue.created_at == issue.updated_at


# ---- close_issue -------------------------------------------------------- #


def test_close_issue_flips_state_to_closed(repo: BareRepo) -> None:
    create_issue(repo, title="t", body="b", author="alice")
    closed = close_issue(repo, number=1, actor="alice")
    assert closed.state is IssueState.CLOSED
    assert get_issue(repo, 1).state is IssueState.CLOSED


def test_close_issue_appends_a_commit_to_the_ref(repo: BareRepo) -> None:
    # The whole point of one-commit-per-event is that closing leaves a
    # second commit on refs/issues/local/<n>. `git log --count` confirms it.
    create_issue(repo, title="t", body="", author="alice")
    before = repo.run_git("rev-list", "--count", "refs/issues/local/1").strip()
    close_issue(repo, number=1, actor="alice")
    after = repo.run_git("rev-list", "--count", "refs/issues/local/1").strip()
    assert int(before) == 1
    assert int(after) == 2


def test_close_issue_advances_updated_at_but_not_created_at(repo: BareRepo) -> None:
    issue = create_issue(repo, title="t", body="", author="alice")
    closed = close_issue(repo, number=1, actor="alice")
    # created_at points at the create commit, updated_at at the close commit.
    assert closed.created_at == issue.created_at
    # We can't assert strict inequality (commits can land in the same second),
    # but updated_at must be ≥ the original updated_at.
    assert closed.updated_at >= issue.updated_at


def test_close_issue_returns_none_for_unknown_number(repo: BareRepo) -> None:
    assert close_issue(repo, number=999, actor="alice") is None


def test_close_issue_close_commit_carries_actor(repo: BareRepo) -> None:
    # The actor is whoever ran the close — independent of the original
    # author. The commit's author identity records that.
    create_issue(repo, title="t", body="", author="alice")
    close_issue(repo, number=1, actor="bob")
    author_line = repo.run_git("log", "-1", "--format=%an <%ae>", "refs/issues/local/1").strip()
    assert author_line.startswith("bob <")


# ---- add_comment / list_comments ---------------------------------------- #


def test_list_comments_is_empty_for_new_issue(repo: BareRepo) -> None:
    create_issue(repo, title="t", body="", author="alice")
    assert list_comments(repo, 1) == []


def test_add_comment_returns_comment_with_metadata(repo: BareRepo) -> None:
    create_issue(repo, title="t", body="", author="alice")
    comment = add_comment(repo, number=1, body="first reply", author="bob")
    assert isinstance(comment, Comment)
    assert comment.body == "first reply"
    assert comment.author == "bob"
    assert comment.created_at  # ISO-8601 from git


def test_add_comment_assigns_sequential_numbers(repo: BareRepo) -> None:
    create_issue(repo, title="t", body="", author="alice")
    a = add_comment(repo, number=1, body="one", author="bob")
    b = add_comment(repo, number=1, body="two", author="bob")
    c = add_comment(repo, number=1, body="three", author="bob")
    assert [a.number, b.number, c.number] == [1, 2, 3]


def test_list_comments_returns_all_comments_in_order(repo: BareRepo) -> None:
    create_issue(repo, title="t", body="", author="alice")
    add_comment(repo, number=1, body="one", author="bob")
    add_comment(repo, number=1, body="two", author="bob")
    add_comment(repo, number=1, body="three", author="bob")

    comments = list_comments(repo, 1)
    assert [c.number for c in comments] == [1, 2, 3]
    assert [c.body for c in comments] == ["one", "two", "three"]


def test_add_comment_writes_blob_under_comments_subtree(repo: BareRepo) -> None:
    # Comments live at comments/<NNNN>.json inside the issue's tree. A future
    # reader (e.g. cgit, or a tree-walking sync) can discover them by listing
    # the comments/ subdir without needing the API.
    create_issue(repo, title="t", body="", author="alice")
    add_comment(repo, number=1, body="hello", author="bob")
    raw = repo.run_git("cat-file", "-p", "refs/issues/local/1:comments/0001.json")
    payload = json.loads(raw)
    assert payload == {
        "body": "hello",
        "author": "bob",
        "provenance": "LOCAL_ONLY",
        "gh_comment_id": None,
    }


def test_add_comment_advances_issue_updated_at(repo: BareRepo) -> None:
    issue = create_issue(repo, title="t", body="", author="alice")
    add_comment(repo, number=1, body="hi", author="bob")
    refreshed = get_issue(repo, 1)
    assert refreshed.updated_at >= issue.updated_at
    assert refreshed.created_at == issue.created_at


def test_add_comment_returns_none_for_unknown_issue(repo: BareRepo) -> None:
    assert add_comment(repo, number=999, body="x", author="bob") is None


def test_add_comment_preserves_issue_state_and_payload(repo: BareRepo) -> None:
    # Adding a comment must not stomp on issue.json — title/body/state stay put.
    create_issue(repo, title="my title", body="my body", author="alice")
    add_comment(repo, number=1, body="comment", author="bob")
    refreshed = get_issue(repo, 1)
    assert refreshed.title == "my title"
    assert refreshed.body == "my body"
    assert refreshed.state is IssueState.OPEN


def test_add_comment_after_close_still_works(repo: BareRepo) -> None:
    # gh allows commenting on a closed issue. The state should remain CLOSED
    # but the comment still appends.
    create_issue(repo, title="t", body="", author="alice")
    close_issue(repo, number=1, actor="alice")
    add_comment(repo, number=1, body="post-close", author="bob")
    refreshed = get_issue(repo, 1)
    assert refreshed.state is IssueState.CLOSED
    assert [c.body for c in list_comments(repo, 1)] == ["post-close"]


# ---- provenance + gh ids ------------------------------------------------ #


def test_create_issue_defaults_to_local_only_provenance(repo: BareRepo) -> None:
    issue = create_issue(repo, title="t", body="b", author="alice")
    assert issue.provenance is Provenance.LOCAL_ONLY
    assert issue.gh_issue_id is None


def test_create_issue_persists_provenance_to_disk(repo: BareRepo) -> None:
    create_issue(repo, title="t", body="b", author="alice")
    raw = repo.run_git("cat-file", "-p", "refs/issues/local/1:issue.json")
    payload = json.loads(raw)
    assert payload["provenance"] == "LOCAL_ONLY"
    assert payload["gh_issue_id"] is None


def test_issue_document_loads_legacy_without_provenance_field() -> None:
    legacy = '{"title": "t", "body": "b", "author": "a", "state": "OPEN"}'
    doc = IssueDocument.model_validate_json(legacy)
    assert doc.provenance is Provenance.LOCAL_ONLY
    assert doc.gh_issue_id is None


def test_close_issue_preserves_provenance_and_gh_id(repo: BareRepo) -> None:
    create_issue(repo, title="t", body="", author="alice")
    closed = close_issue(repo, number=1, actor="alice")
    assert closed.provenance is Provenance.LOCAL_ONLY
    assert closed.gh_issue_id is None


def test_add_comment_defaults_to_local_only_provenance(repo: BareRepo) -> None:
    create_issue(repo, title="t", body="", author="alice")
    comment = add_comment(repo, number=1, body="hi", author="bob")
    assert comment.provenance is Provenance.LOCAL_ONLY
    assert comment.gh_comment_id is None


def test_add_comment_persists_provenance_to_disk(repo: BareRepo) -> None:
    create_issue(repo, title="t", body="", author="alice")
    add_comment(repo, number=1, body="hi", author="bob")
    raw = repo.run_git("cat-file", "-p", "refs/issues/local/1:comments/0001.json")
    payload = json.loads(raw)
    assert payload["provenance"] == "LOCAL_ONLY"
    assert payload["gh_comment_id"] is None


def test_comment_document_loads_legacy_without_provenance_field() -> None:
    legacy = '{"body": "b", "author": "a"}'
    doc = CommentDocument.model_validate_json(legacy)
    assert doc.provenance is Provenance.LOCAL_ONLY
    assert doc.gh_comment_id is None


# ---- list_synced_issues / list_all_issues / get_any_issue ------------- #


def test_list_synced_issues_returns_only_synced_namespace(repo: BareRepo) -> None:
    create_issue(repo, title="local-1", body="", author="alice")
    import_issue(
        repo,
        number=42,
        title="synced-42",
        body="",
        author="alice",
        state=IssueState.OPEN,
        gh_issue_id=900,
    )

    synced = list_synced_issues(repo)
    assert [i.number for i in synced] == [42]
    assert synced[0].provenance is Provenance.SYNCED_FROM_GITHUB


def test_list_all_issues_returns_synced_first_then_local(repo: BareRepo) -> None:
    create_issue(repo, title="local-1", body="", author="alice")
    create_issue(repo, title="local-2", body="", author="alice")
    import_issue(
        repo,
        number=42,
        title="synced-42",
        body="",
        author="alice",
        state=IssueState.OPEN,
        gh_issue_id=900,
    )

    combined = list_all_issues(repo)
    titles = [i.title for i in combined]
    assert titles == ["synced-42", "local-1", "local-2"]


def test_get_any_issue_prefers_synced_over_local(repo: BareRepo) -> None:
    # Local issue 1 created first.
    create_issue(repo, title="local one", body="", author="alice")
    # Then a synced issue lands at number 1 too (rare collision).
    import_issue(
        repo,
        number=1,
        title="synced one",
        body="",
        author="bob",
        state=IssueState.OPEN,
        gh_issue_id=999,
    )

    issue = get_any_issue(repo, 1)
    assert issue is not None
    assert issue.title == "synced one"
    assert issue.provenance is Provenance.SYNCED_FROM_GITHUB


def test_get_any_issue_falls_back_to_local_when_synced_missing(
    repo: BareRepo,
) -> None:
    create_issue(repo, title="just local", body="", author="alice")
    issue = get_any_issue(repo, 1)
    assert issue is not None
    assert issue.title == "just local"
    assert issue.provenance is Provenance.LOCAL_ONLY


def test_list_any_comments_uses_synced_namespace_when_synced_issue_exists(
    repo: BareRepo,
) -> None:
    import_issue(
        repo,
        number=42,
        title="t",
        body="",
        author="alice",
        state=IssueState.OPEN,
        gh_issue_id=900,
    )
    import_comment(
        repo,
        issue_number=42,
        body="from-gh",
        author="alice",
        gh_comment_id=12345,
    )

    comments = list_any_comments(repo, 42)
    assert [c.body for c in comments] == ["from-gh"]


def test_list_any_comments_falls_back_to_local_when_synced_absent(
    repo: BareRepo,
) -> None:
    create_issue(repo, title="t", body="", author="alice")
    add_comment(repo, number=1, body="local-c", author="alice")

    comments = list_any_comments(repo, 1)
    assert [c.body for c in comments] == ["local-c"]
