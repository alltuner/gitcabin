# ABOUTME: Tests for the IssueByNumber query (gh issue view).
# ABOUTME: Pulls a single issue by number via Repository.issueOrPullRequest.

from __future__ import annotations

from fastapi.testclient import TestClient

from gitcabin.ids import repo_id

# Mirrors what gh sends from pkg/cmd/issue/shared/lookup.go::IssueByNumber.
# We replicate the smaller subset of fields gh's IssueGraphQL/PullRequestGraphQL
# expansions produce — enough to prove the union and field selection work.
ISSUE_VIEW_QUERY = """
query IssueByNumber($owner: String!, $repo: String!, $number: Int!) {
  repository(owner: $owner, name: $repo) {
    hasIssuesEnabled
    issue: issueOrPullRequest(number: $number) {
      __typename
      ... on Issue {
        number
        title
        body
        state
        url
        createdAt
        author { login }
        labels(first: 100) { nodes { name } totalCount }
        assignees(first: 100) { nodes { login } totalCount }
        stateReason
      }
      ... on PullRequest {
        number
        title
      }
    }
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


def test_issue_view_returns_existing_issue(client: TestClient) -> None:
    _post(
        client,
        CREATE_ISSUE,
        {
            "input": {
                "repositoryId": repo_id("octocat", "hello"),
                "title": "Findable",
                "body": "find me",
            }
        },
    )

    payload = _post(client, ISSUE_VIEW_QUERY, {"owner": "octocat", "repo": "hello", "number": 1})

    assert "errors" not in payload, payload
    issue = payload["data"]["repository"]["issue"]
    assert issue["__typename"] == "Issue"
    assert issue["number"] == 1
    assert issue["title"] == "Findable"
    assert issue["body"] == "find me"
    assert issue["state"] == "OPEN"
    assert issue["author"]["login"] == "david"
    assert issue["labels"]["nodes"] == []
    assert issue["assignees"]["nodes"] == []
    assert issue["createdAt"]


def test_issue_view_returns_null_for_unknown_number(client: TestClient, init_repo) -> None:
    init_repo("octocat", "hello")
    payload = _post(client, ISSUE_VIEW_QUERY, {"owner": "octocat", "repo": "hello", "number": 99})
    assert "errors" not in payload, payload
    assert payload["data"]["repository"]["issue"] is None
