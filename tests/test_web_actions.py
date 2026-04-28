# ABOUTME: Tests for the web UI's write actions — comment, close, reopen.
# ABOUTME: Each action POSTs and 303-redirects back to the issue page.

from __future__ import annotations

from fastapi.testclient import TestClient

from gitcabin.ids import repo_id

CREATE_ISSUE = """
mutation IssueCreate($input: CreateIssueInput!) {
  createIssue(input: $input) { issue { id } }
}
"""


def _create(client: TestClient, owner: str, name: str, title: str) -> None:
    response = client.post(
        "/graphql",
        json={
            "query": CREATE_ISSUE,
            "variables": {"input": {"repositoryId": repo_id(owner, name), "title": title}},
        },
    )
    assert response.status_code == 200
    assert "errors" not in response.json()


def test_comment_form_posts_and_redirects(
    web_client: TestClient, client: TestClient, init_repo
) -> None:
    init_repo("octocat", "hello")
    _create(client, "octocat", "hello", "needs replies")

    response = web_client.post(
        "/octocat/hello/issues/1/comments",
        data={"body": "first reply from the dashboard"},
        follow_redirects=False,
    )
    assert response.status_code == 303
    assert response.headers["location"] == "/octocat/hello/issues/1"

    # The follow-up GET shows the new comment.
    follow = web_client.get("/octocat/hello/issues/1")
    assert "first reply from the dashboard" in follow.text


def test_empty_comment_is_ignored(web_client: TestClient, client: TestClient, init_repo) -> None:
    init_repo("octocat", "hello")
    _create(client, "octocat", "hello", "no chat")

    # Body of just whitespace doesn't append a comment but still redirects.
    response = web_client.post(
        "/octocat/hello/issues/1/comments",
        data={"body": "   \n\t  "},
        follow_redirects=False,
    )
    assert response.status_code == 303

    follow = web_client.get("/octocat/hello/issues/1")
    assert "No comments yet" in follow.text


def test_close_action_flips_state(web_client: TestClient, client: TestClient, init_repo) -> None:
    init_repo("octocat", "hello")
    _create(client, "octocat", "hello", "let's close this")

    response = web_client.post("/octocat/hello/issues/1/close", follow_redirects=False)
    assert response.status_code == 303

    follow = web_client.get("/octocat/hello/issues/1")
    # The header pill switches to "Closed".
    assert "Closed" in follow.text
    # And the form's button switches to Reopen.
    assert "Reopen issue" in follow.text


def test_reopen_action_flips_back(web_client: TestClient, client: TestClient, init_repo) -> None:
    init_repo("octocat", "hello")
    _create(client, "octocat", "hello", "ping pong")

    web_client.post("/octocat/hello/issues/1/close")
    web_client.post("/octocat/hello/issues/1/reopen")

    follow = web_client.get("/octocat/hello/issues/1")
    assert "Open" in follow.text
    assert "Close issue" in follow.text


def test_action_404_for_unknown_issue(web_client: TestClient, init_repo) -> None:
    init_repo("octocat", "hello")
    response = web_client.post("/octocat/hello/issues/999/close")
    assert response.status_code == 404


def test_no_signed_in_chrome(web_client: TestClient) -> None:
    # The dashboard is anonymous — no auth session, no "signed in as" pill.
    # If it ever shows up again, we want to know.
    response = web_client.get("/")
    assert "signed in as" not in response.text
