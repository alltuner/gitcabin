# ABOUTME: Tests for the GraphQL Repository.issues connection (gh issue list).
# ABOUTME: Mirrors the IssueList query gh sends; reads are backed by real on-disk refs.

from __future__ import annotations

from fastapi.testclient import TestClient

from gitcabin.ids import repo_id

ISSUE_LIST_QUERY = """
query IssueList(
  $owner: String!,
  $repo: String!,
  $limit: Int,
  $endCursor: String,
  $states: [IssueState!] = OPEN
) {
  repository(owner: $owner, name: $repo) {
    hasIssuesEnabled
    issues(
      first: $limit,
      after: $endCursor,
      states: $states,
      orderBy: {field: CREATED_AT, direction: DESC}
    ) {
      totalCount
      nodes {
        number
        title
        url
        state
        updatedAt
        labels(first: 100) {
          nodes { id name description color }
          totalCount
        }
        stateReason
      }
      pageInfo { hasNextPage endCursor }
    }
  }
}
"""

CREATE_ISSUE = """
mutation IssueCreate($input: CreateIssueInput!) {
  createIssue(input: $input) {
    issue { id }
  }
}
"""


def _post(client: TestClient, query: str, variables: dict) -> dict:
    response = client.post("/graphql", json={"query": query, "variables": variables})
    assert response.status_code == 200
    return response.json()


def _create(client: TestClient, owner: str, name: str, title: str) -> None:
    payload = _post(
        client,
        CREATE_ISSUE,
        {"input": {"repositoryId": repo_id(owner, name), "title": title}},
    )
    assert "errors" not in payload, payload


def test_issue_list_on_empty_repo(client: TestClient, init_repo) -> None:
    init_repo("octocat", "hello")
    payload = _post(client, ISSUE_LIST_QUERY, {"owner": "octocat", "repo": "hello", "limit": 30})

    assert "errors" not in payload, payload
    issues = payload["data"]["repository"]["issues"]
    assert issues["totalCount"] == 0
    assert issues["nodes"] == []
    assert issues["pageInfo"]["hasNextPage"] is False


def test_issue_list_returns_null_repository_when_repo_absent(client: TestClient) -> None:
    # Strict Query.repository: a missing bare repo means the whole repository
    # field is null, and the issues connection isn't reachable.
    payload = _post(client, ISSUE_LIST_QUERY, {"owner": "nope", "repo": "absent", "limit": 30})
    assert "errors" not in payload, payload
    assert payload["data"]["repository"] is None


def test_issue_list_returns_created_issues(client: TestClient) -> None:
    _create(client, "octocat", "hello", "first")
    _create(client, "octocat", "hello", "second")
    _create(client, "octocat", "hello", "third")

    payload = _post(client, ISSUE_LIST_QUERY, {"owner": "octocat", "repo": "hello", "limit": 30})
    issues = payload["data"]["repository"]["issues"]
    assert issues["totalCount"] == 3
    titles = [n["title"] for n in issues["nodes"]]
    assert set(titles) == {"first", "second", "third"}
    # Each node carries the fields gh selects (subset check, exact set varies
    # by what the resolver populates today).
    sample = issues["nodes"][0]
    assert "number" in sample and "url" in sample and "state" in sample
    assert "updatedAt" in sample and "labels" in sample and "stateReason" in sample
    assert sample["labels"]["nodes"] == []
    assert sample["labels"]["totalCount"] == 0


def test_issue_list_respects_limit(client: TestClient) -> None:
    for i in range(5):
        _create(client, "octocat", "hello", f"issue-{i}")

    payload = _post(client, ISSUE_LIST_QUERY, {"owner": "octocat", "repo": "hello", "limit": 2})
    issues = payload["data"]["repository"]["issues"]
    # totalCount reflects all matching issues; nodes is bounded by limit.
    assert issues["totalCount"] == 5
    assert len(issues["nodes"]) == 2
    assert issues["pageInfo"]["hasNextPage"] is True
