# ABOUTME: Tests for viewer_can_* GraphQL fields driven by gitcabin.permissions.
# ABOUTME: Exercises both the field values and the closeIssue mutation's permission gate.

from __future__ import annotations

from collections.abc import Callable

from fastapi.testclient import TestClient

from gitcabin.ids import issue_id, repo_id
from gitcabin.storage.issues import (
    IssueState,
    add_comment,
    create_issue,
    import_comment,
    import_issue,
)
from gitcabin.storage.repo import BareRepo
from gitcabin.sync.config import SyncConfig, write_config

VIEWER_CAN_QUERY = """
query Q($owner: String!, $name: String!, $number: Int!) {
  repository(owner: $owner, name: $name) {
    issue(number: $number) {
      number
      author { login }
      viewerDidAuthor
      viewerCanUpdate
      viewerCanCloseOrReopen
      comments(first: 50) {
        nodes {
          author { login }
          viewerDidAuthor
          viewerCanUpdate
          viewerCanDelete
        }
      }
    }
  }
}
"""

CLOSE_ISSUE = """
mutation Close($input: CloseIssueInput!) {
  closeIssue(input: $input) { issue { id state } }
}
"""


def _post(client: TestClient, query: str, variables: dict) -> dict:
    response = client.post("/graphql", json={"query": query, "variables": variables})
    assert response.status_code == 200
    return response.json()


def _viewer_state(client: TestClient, owner: str, name: str, number: int) -> dict:
    payload = _post(
        client,
        VIEWER_CAN_QUERY,
        {"owner": owner, "name": name, "number": number},
    )
    assert "errors" not in payload, payload
    return payload["data"]["repository"]["issue"]


# ---- viewer authored: full edit affordances --------------------------- #


def test_viewer_authored_local_issue_has_full_affordances(
    client: TestClient,
    init_repo: Callable[[str, str], BareRepo],
) -> None:
    repo = init_repo("octocat", "hello")
    create_issue(repo, title="mine", body="b", author="david")
    add_comment(repo, number=1, body="my comment", author="david")

    issue = _viewer_state(client, "octocat", "hello", 1)
    assert issue["viewerDidAuthor"] is True
    assert issue["viewerCanUpdate"] is True
    assert issue["viewerCanCloseOrReopen"] is True

    [comment] = issue["comments"]["nodes"]
    assert comment["viewerDidAuthor"] is True
    assert comment["viewerCanUpdate"] is True
    assert comment["viewerCanDelete"] is True


# ---- viewer not author: synced issue from someone else -------------- #


def test_synced_issue_authored_by_other_viewer_cannot_edit(
    client: TestClient,
    settings,  # for data_dir
    init_repo: Callable[[str, str], BareRepo],
) -> None:
    repo = init_repo("octocat", "hello")
    # Link the repo to GitHub with viewer_repo_role=READ — the viewer is a
    # plain reader on the upstream repo, no triage / admin powers.
    write_config(
        repo,
        SyncConfig(
            gh_owner="octocat",
            gh_name="hello",
            gh_viewer_login="david",
            viewer_repo_role="READ",
        ),
    )
    import_issue(
        repo,
        number=42,
        title="not mine",
        body="b",
        author="alice",
        state=IssueState.OPEN,
        gh_issue_id=900,
    )
    import_comment(
        repo,
        issue_number=42,
        body="alice's comment",
        author="alice",
        gh_comment_id=1234,
    )

    issue = _viewer_state(client, "octocat", "hello", 42)
    assert issue["viewerDidAuthor"] is False
    assert issue["viewerCanUpdate"] is False
    assert issue["viewerCanCloseOrReopen"] is False  # READ role, not author

    [comment] = issue["comments"]["nodes"]
    assert comment["viewerDidAuthor"] is False
    assert comment["viewerCanUpdate"] is False
    assert comment["viewerCanDelete"] is False  # not ADMIN


def test_synced_issue_with_triage_role_can_close_but_not_edit(
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
            viewer_repo_role="TRIAGE",
        ),
    )
    import_issue(
        repo,
        number=42,
        title="not mine",
        body="",
        author="alice",
        state=IssueState.OPEN,
        gh_issue_id=900,
    )

    issue = _viewer_state(client, "octocat", "hello", 42)
    assert issue["viewerDidAuthor"] is False
    assert issue["viewerCanUpdate"] is False  # never — content edit requires authorship
    assert issue["viewerCanCloseOrReopen"] is True  # TRIAGE moderation


def test_synced_issue_with_admin_role_can_delete_others_comments(
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
        body="alice's",
        author="alice",
        gh_comment_id=1234,
    )

    issue = _viewer_state(client, "octocat", "hello", 42)
    [comment] = issue["comments"]["nodes"]
    assert comment["viewerCanUpdate"] is False  # never — content edit
    assert comment["viewerCanDelete"] is True  # ADMIN can delete others'


# ---- mutation enforcement -------------------------------------------- #


def test_close_issue_rejects_non_author_without_role(
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
            viewer_repo_role="READ",
        ),
    )
    import_issue(
        repo,
        number=42,
        title="not mine",
        body="",
        author="alice",
        state=IssueState.OPEN,
        gh_issue_id=900,
    )

    iid = issue_id("octocat", "hello", 42)
    payload = _post(client, CLOSE_ISSUE, {"input": {"issueId": iid}})
    # Strawberry surfaces PermissionError as a GraphQL error, not as data.
    assert "errors" in payload
    assert any("cannot change state" in e["message"] for e in payload["errors"])


def test_close_issue_allows_triage_role_on_other_authors_issue(
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
            viewer_repo_role="TRIAGE",
        ),
    )
    import_issue(
        repo,
        number=42,
        title="alice's",
        body="",
        author="alice",
        state=IssueState.OPEN,
        gh_issue_id=900,
    )

    iid = issue_id("octocat", "hello", 42)
    payload = _post(client, CLOSE_ISSUE, {"input": {"issueId": iid}})
    assert "errors" not in payload, payload
    assert payload["data"]["closeIssue"]["issue"]["state"] == "CLOSED"


def test_close_issue_local_issue_authored_by_viewer_works_unchanged(
    client: TestClient,
    init_repo: Callable[[str, str], BareRepo],
) -> None:
    # Regression check: the existing happy path (viewer creates + closes their
    # own local issue) keeps working with permission enforcement on.
    repo_id_str = repo_id("octocat", "hello")
    create_payload = _post(
        client,
        """mutation Create($input: CreateIssueInput!) {
            createIssue(input: $input) { issue { id } }
        }""",
        {"input": {"repositoryId": repo_id_str, "title": "mine"}},
    )
    iid = create_payload["data"]["createIssue"]["issue"]["id"]

    payload = _post(client, CLOSE_ISSUE, {"input": {"issueId": iid}})
    assert "errors" not in payload, payload
    assert payload["data"]["closeIssue"]["issue"]["state"] == "CLOSED"


def test_repository_issues_returns_synced_and_local_combined(
    client: TestClient,
    init_repo: Callable[[str, str], BareRepo],
) -> None:
    repo = init_repo("octocat", "hello")
    create_issue(repo, title="local one", body="", author="david")
    import_issue(
        repo,
        number=42,
        title="synced 42",
        body="",
        author="alice",
        state=IssueState.OPEN,
        gh_issue_id=900,
    )

    Q = """
    query L($owner: String!, $name: String!) {
      repository(owner: $owner, name: $name) {
        issues(first: 100) { totalCount nodes { number title } }
      }
    }
    """
    payload = _post(client, Q, {"owner": "octocat", "name": "hello"})
    assert "errors" not in payload, payload
    issues = payload["data"]["repository"]["issues"]
    assert issues["totalCount"] == 2
    titles = {n["title"] for n in issues["nodes"]}
    assert titles == {"local one", "synced 42"}
