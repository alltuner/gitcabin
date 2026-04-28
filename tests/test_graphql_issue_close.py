# ABOUTME: Tests for the closeIssue GraphQL mutation (gh issue close).
# ABOUTME: End-to-end: HTTP POST -> resolver -> ref appended -> response shape.

from __future__ import annotations

from fastapi.testclient import TestClient

from gitcabin.ids import issue_id, repo_id

# Mirrors api/queries_issue.go's IssueClose mutation. The selection set is
# whatever gh's mutation struct asks for — `issue { id }` is enough.
GH_CLOSE_ISSUE = """
mutation IssueClose($input: CloseIssueInput!) {
  closeIssue(input: $input) {
    issue { id state }
  }
}
"""

CREATE_ISSUE = """
mutation IssueCreate($input: CreateIssueInput!) {
  createIssue(input: $input) { issue { id } }
}
"""


def _post(client: TestClient, query: str, variables: dict) -> dict:
    response = client.post("/graphql", json={"query": query, "variables": variables})
    assert response.status_code == 200
    return response.json()


def _create(client: TestClient, owner: str, name: str, title: str) -> str:
    payload = _post(
        client,
        CREATE_ISSUE,
        {"input": {"repositoryId": repo_id(owner, name), "title": title}},
    )
    assert "errors" not in payload, payload
    return payload["data"]["createIssue"]["issue"]["id"]


def test_close_issue_flips_state(client: TestClient) -> None:
    iid = _create(client, "octocat", "hello", "Closeable")

    payload = _post(client, GH_CLOSE_ISSUE, {"input": {"issueId": iid}})
    assert "errors" not in payload, payload
    issue = payload["data"]["closeIssue"]["issue"]
    assert issue["id"] == iid
    assert issue["state"] == "CLOSED"


def test_close_issue_accepts_state_reason(client: TestClient) -> None:
    # gh sends stateReason="COMPLETED" or "NOT_PLANNED". Until we wire labels,
    # the field is accepted-and-ignored — the schema must still validate.
    iid = _create(client, "octocat", "hello", "with reason")
    payload = _post(
        client,
        GH_CLOSE_ISSUE,
        {"input": {"issueId": iid, "stateReason": "NOT_PLANNED"}},
    )
    assert "errors" not in payload, payload
    assert payload["data"]["closeIssue"]["issue"]["state"] == "CLOSED"


def test_close_issue_returns_error_for_unknown_id(client: TestClient) -> None:
    bogus = issue_id("octocat", "hello", 999)
    payload = _post(client, GH_CLOSE_ISSUE, {"input": {"issueId": bogus}})
    assert payload.get("errors")


def test_close_issue_is_visible_via_subsequent_view(client: TestClient) -> None:
    # After closing, gh's IssueByNumber lookup should report state=CLOSED.
    iid = _create(client, "octocat", "hello", "round-trip")
    _post(client, GH_CLOSE_ISSUE, {"input": {"issueId": iid}})

    view_query = """
    query($owner: String!, $repo: String!, $number: Int!) {
      repository(owner: $owner, name: $repo) {
        issue(number: $number) { state }
      }
    }
    """
    payload = _post(client, view_query, {"owner": "octocat", "repo": "hello", "number": 1})
    assert payload["data"]["repository"]["issue"]["state"] == "CLOSED"
