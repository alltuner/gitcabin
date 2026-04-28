# ABOUTME: Tests for the addComment GraphQL mutation (gh issue comment).
# ABOUTME: End-to-end: HTTP POST -> resolver -> comment blob written -> visible via Issue.comments.

from __future__ import annotations

from fastapi.testclient import TestClient

from testgit.ids import issue_id, repo_id

# Mirrors api/queries_comments.go's CommentCreate mutation. gh selects
# `commentEdge { node { url } }` — that URL is what gets printed back.
GH_ADD_COMMENT = """
mutation CommentCreate($input: AddCommentInput!) {
  addComment(input: $input) {
    commentEdge { node { url body author { login } } }
  }
}
"""

CREATE_ISSUE = """
mutation IssueCreate($input: CreateIssueInput!) {
  createIssue(input: $input) { issue { id } }
}
"""

ISSUE_WITH_COMMENTS = """
query($owner: String!, $repo: String!, $number: Int!) {
  repository(owner: $owner, name: $repo) {
    issue(number: $number) {
      comments(first: 100) {
        totalCount
        nodes { id body url author { login } createdAt }
      }
    }
  }
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


def test_add_comment_returns_node_url(client: TestClient) -> None:
    iid = _create(client, "octocat", "hello", "for comment")
    payload = _post(
        client,
        GH_ADD_COMMENT,
        {"input": {"subjectId": iid, "body": "first reply"}},
    )

    assert "errors" not in payload, payload
    node = payload["data"]["addComment"]["commentEdge"]["node"]
    # gh prints `node.url` after a successful comment; mirror gh.com's format
    # so users see something useful even though we don't host it ourselves.
    assert node["url"].startswith("http://github.localhost/octocat/hello/issues/1#issuecomment-")
    assert node["body"] == "first reply"
    assert node["author"]["login"] == "david"


def test_add_comment_is_visible_via_issue_comments(client: TestClient) -> None:
    iid = _create(client, "octocat", "hello", "for comments")
    _post(client, GH_ADD_COMMENT, {"input": {"subjectId": iid, "body": "one"}})
    _post(client, GH_ADD_COMMENT, {"input": {"subjectId": iid, "body": "two"}})

    payload = _post(client, ISSUE_WITH_COMMENTS, {"owner": "octocat", "repo": "hello", "number": 1})
    comments = payload["data"]["repository"]["issue"]["comments"]
    assert comments["totalCount"] == 2
    assert [c["body"] for c in comments["nodes"]] == ["one", "two"]
    assert all(c["author"]["login"] == "david" for c in comments["nodes"])
    assert all(c["createdAt"] for c in comments["nodes"])


def test_add_comment_returns_error_for_unknown_subject(client: TestClient) -> None:
    bogus = issue_id("octocat", "hello", 999)
    payload = _post(client, GH_ADD_COMMENT, {"input": {"subjectId": bogus, "body": "ghost"}})
    assert payload.get("errors")


def test_comments_empty_for_new_issue(client: TestClient, init_repo) -> None:
    init_repo("octocat", "hello")
    _create(client, "octocat", "hello", "no comments yet")

    payload = _post(client, ISSUE_WITH_COMMENTS, {"owner": "octocat", "repo": "hello", "number": 1})
    comments = payload["data"]["repository"]["issue"]["comments"]
    assert comments["totalCount"] == 0
    assert comments["nodes"] == []
