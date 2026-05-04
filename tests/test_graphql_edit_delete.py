# ABOUTME: Tests for the updateIssue / updateIssueComment / deleteIssueComment mutations.
# ABOUTME: Covers both author and non-author paths so the can_edit gates are exercised.

from __future__ import annotations

from collections.abc import Callable

from fastapi.testclient import TestClient

from gitcabin.ids import comment_id, issue_id, repo_id
from gitcabin.storage.issues import (
    IssueState,
    add_comment,
    create_issue,
    import_comment,
    import_issue,
    list_any_comments,
)
from gitcabin.storage.repo import BareRepo
from gitcabin.sync.config import SyncConfig, write_config

UPDATE_ISSUE = """
mutation U($input: UpdateIssueInput!) {
  updateIssue(input: $input) { issue { id title body } }
}
"""

UPDATE_COMMENT = """
mutation U($input: UpdateIssueCommentInput!) {
  updateIssueComment(input: $input) { issueComment { id body } }
}
"""

DELETE_COMMENT = """
mutation D($input: DeleteIssueCommentInput!) {
  deleteIssueComment(input: $input) { clientMutationId }
}
"""


def _post(client: TestClient, query: str, variables: dict) -> dict:
    response = client.post("/graphql", json={"query": query, "variables": variables})
    assert response.status_code == 200
    return response.json()


# ---- updateIssue -------------------------------------------------------- #


def test_update_issue_author_can_edit_title_and_body(
    client: TestClient,
    init_repo: Callable[[str, str], BareRepo],
) -> None:
    repo = init_repo("octocat", "hello")
    create_issue(repo, title="old", body="old body", author="david")

    iid = issue_id("octocat", "hello", 1)
    payload = _post(
        client,
        UPDATE_ISSUE,
        {"input": {"id": iid, "title": "new", "body": "new body"}},
    )
    assert "errors" not in payload, payload
    issue = payload["data"]["updateIssue"]["issue"]
    assert issue["title"] == "new"
    assert issue["body"] == "new body"


def test_update_issue_rejects_non_author(
    client: TestClient,
    init_repo: Callable[[str, str], BareRepo],
) -> None:
    repo = init_repo("octocat", "hello")
    write_config(
        repo,
        SyncConfig(
            gh_owner="octocat",
            gh_name="hello",
            gh_viewer_login="david",
            viewer_repo_role="ADMIN",  # Even ADMIN cannot edit content not theirs.
        ),
    )
    import_issue(
        repo,
        number=42,
        title="alice's issue",
        body="b",
        author="alice",
        state=IssueState.OPEN,
        gh_issue_id=900,
    )

    iid = issue_id("octocat", "hello", 42)
    payload = _post(client, UPDATE_ISSUE, {"input": {"id": iid, "body": "tampered"}})
    assert "errors" in payload
    assert any("cannot edit" in e["message"] for e in payload["errors"])


def test_update_issue_partial_update_preserves_other_fields(
    client: TestClient,
    init_repo: Callable[[str, str], BareRepo],
) -> None:
    repo = init_repo("octocat", "hello")
    create_issue(repo, title="t", body="original body", author="david")

    iid = issue_id("octocat", "hello", 1)
    payload = _post(client, UPDATE_ISSUE, {"input": {"id": iid, "title": "new title"}})
    assert "errors" not in payload, payload
    issue = payload["data"]["updateIssue"]["issue"]
    assert issue["title"] == "new title"
    assert issue["body"] == "original body"


# ---- updateIssueComment ------------------------------------------------ #


def test_update_comment_author_can_edit(
    client: TestClient,
    init_repo: Callable[[str, str], BareRepo],
) -> None:
    repo = init_repo("octocat", "hello")
    create_issue(repo, title="t", body="", author="david")
    add_comment(repo, number=1, body="old body", author="david")

    cid = comment_id("octocat", "hello", 1, 1)
    payload = _post(client, UPDATE_COMMENT, {"input": {"id": cid, "body": "new body"}})
    assert "errors" not in payload, payload
    assert payload["data"]["updateIssueComment"]["issueComment"]["body"] == "new body"


def test_update_comment_rejects_non_author_even_admin(
    client: TestClient,
    init_repo: Callable[[str, str], BareRepo],
) -> None:
    repo = init_repo("octocat", "hello")
    write_config(
        repo,
        SyncConfig(
            gh_owner="octocat",
            gh_name="hello",
            gh_viewer_login="david",
            viewer_repo_role="ADMIN",
        ),
    )
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
        body="alice wrote this",
        author="alice",
        gh_comment_id=12345,
    )

    cid = comment_id("octocat", "hello", 42, 12345)
    payload = _post(client, UPDATE_COMMENT, {"input": {"id": cid, "body": "tampered"}})
    assert "errors" in payload
    assert any("cannot edit comment" in e["message"] for e in payload["errors"])


# ---- deleteIssueComment ------------------------------------------------ #


def test_delete_comment_author_succeeds(
    client: TestClient,
    init_repo: Callable[[str, str], BareRepo],
) -> None:
    repo = init_repo("octocat", "hello")
    create_issue(repo, title="t", body="", author="david")
    add_comment(repo, number=1, body="mine", author="david")

    cid = comment_id("octocat", "hello", 1, 1)
    payload = _post(client, DELETE_COMMENT, {"input": {"id": cid}})
    assert "errors" not in payload, payload

    # Storage should reflect the deletion.
    assert list_any_comments(repo, 1) == []


def test_delete_comment_admin_can_delete_others_comment(
    client: TestClient,
    init_repo: Callable[[str, str], BareRepo],
) -> None:
    repo = init_repo("octocat", "hello")
    write_config(
        repo,
        SyncConfig(
            gh_owner="octocat",
            gh_name="hello",
            gh_viewer_login="david",
            viewer_repo_role="ADMIN",
        ),
    )
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
        body="off-topic",
        author="alice",
        gh_comment_id=999,
    )

    cid = comment_id("octocat", "hello", 42, 999)
    payload = _post(client, DELETE_COMMENT, {"input": {"id": cid}})
    assert "errors" not in payload, payload
    assert list_any_comments(repo, 42) == []


def test_delete_comment_non_admin_rejects_others_comment(
    client: TestClient,
    init_repo: Callable[[str, str], BareRepo],
) -> None:
    repo = init_repo("octocat", "hello")
    write_config(
        repo,
        SyncConfig(
            gh_owner="octocat",
            gh_name="hello",
            gh_viewer_login="david",
            viewer_repo_role="WRITE",  # not ADMIN
        ),
    )
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
        repo, issue_number=42, body="alice's", author="alice", gh_comment_id=999
    )

    cid = comment_id("octocat", "hello", 42, 999)
    payload = _post(client, DELETE_COMMENT, {"input": {"id": cid}})
    assert "errors" in payload
    assert any("cannot delete" in e["message"] for e in payload["errors"])
    # Comment is still there.
    assert len(list_any_comments(repo, 42)) == 1


# ---- helpers ---------------------------------------------------------- #


def test_repo_id_round_trip_is_unaffected(
    client: TestClient,
) -> None:
    # Sanity check that id-encoding work in this commit didn't break the repo
    # round-trip; the create_issue mutation path uses repo_id.
    rid = repo_id("octocat", "hello")
    payload = _post(
        client,
        """mutation C($input: CreateIssueInput!) {
            createIssue(input: $input) { issue { id title } }
        }""",
        {"input": {"repositoryId": rid, "title": "round-trip check"}},
    )
    assert "errors" not in payload, payload
    assert payload["data"]["createIssue"]["issue"]["title"] == "round-trip check"
