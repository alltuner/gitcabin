# ABOUTME: Tests for the createIssue GraphQL mutation.
# ABOUTME: End-to-end: HTTP POST -> resolver -> real bare repo -> ref written -> response shape.

from __future__ import annotations

from pathlib import Path

from fastapi.testclient import TestClient

from testgit.config import Settings
from testgit.ids import decode_issue_id, repo_id
from testgit.storage.repo import BareRepo

# Mirrors the exact mutation gh sends from api/queries_issue.go::IssueCreate.
GH_CREATE_ISSUE = """
mutation IssueCreate($input: CreateIssueInput!) {
  createIssue(input: $input) {
    issue {
      id
      url
    }
  }
}
"""


def _post(client: TestClient, query: str, variables: dict) -> dict:
    response = client.post("/graphql", json={"query": query, "variables": variables})
    assert response.status_code == 200
    return response.json()


def test_create_issue_returns_id_and_url(client: TestClient) -> None:
    payload = _post(
        client,
        GH_CREATE_ISSUE,
        {
            "input": {
                "repositoryId": repo_id("octocat", "hello"),
                "title": "First issue",
                "body": "Hello world",
            }
        },
    )

    assert "errors" not in payload, payload
    issue = payload["data"]["createIssue"]["issue"]
    assert issue["id"]
    assert issue["url"] == "http://github.localhost/octocat/hello/issues/1"

    # The id must round-trip back to the coords we passed in.
    coords = decode_issue_id(issue["id"])
    assert coords is not None
    assert coords.owner == "octocat"
    assert coords.name == "hello"
    assert coords.number == 1


def test_create_issue_persists_to_a_real_ref(client: TestClient, settings: Settings) -> None:
    # The mutation must write a refs/issues/local/<n> ref to the bare repo
    # under settings.data_dir. This proves we're not just composing a response
    # in memory.
    _post(
        client,
        GH_CREATE_ISSUE,
        {
            "input": {
                "repositoryId": repo_id("octocat", "hello"),
                "title": "Persistence test",
                "body": "",
            }
        },
    )

    bare = settings.data_dir / "repos" / "octocat" / "hello.git"
    assert bare.is_dir(), "bare repo must be initialized on first issue"
    repo = BareRepo.open_or_init(bare)
    sha = repo.run_git("rev-parse", "refs/issues/local/1").strip()
    assert sha


def test_create_issue_increments_across_calls(client: TestClient) -> None:
    # Two creates against the same repo must allocate sequential numbers.
    rid = repo_id("octocat", "hello")
    _post(client, GH_CREATE_ISSUE, {"input": {"repositoryId": rid, "title": "a"}})
    _post(client, GH_CREATE_ISSUE, {"input": {"repositoryId": rid, "title": "b"}})

    # Inspect the third creation to confirm it gets number 3 in the URL.
    payload = _post(client, GH_CREATE_ISSUE, {"input": {"repositoryId": rid, "title": "c"}})
    url = payload["data"]["createIssue"]["issue"]["url"]
    assert url.endswith("/issues/3")


def test_create_issue_rejects_unknown_repository_id(client: TestClient) -> None:
    # A bogus id (not produced by repo_id) must fail cleanly with a GraphQL
    # error, not a 500 / unhandled exception.
    payload = _post(
        client,
        GH_CREATE_ISSUE,
        {"input": {"repositoryId": "not-a-real-id", "title": "x"}},
    )
    assert payload.get("errors")
    assert payload["data"] is None or payload["data"].get("createIssue") is None


def test_create_issue_writes_to_settings_data_dir_only(
    client: TestClient, settings: Settings, tmp_path: Path
) -> None:
    # Sanity check that the test fixture's data_dir override actually
    # contains the writes — i.e. the resolver isn't accidentally writing to
    # the host's /app/data or anywhere else.
    _post(
        client,
        GH_CREATE_ISSUE,
        {"input": {"repositoryId": repo_id("octocat", "hello"), "title": "scoped"}},
    )
    assert (settings.data_dir / "repos" / "octocat" / "hello.git").is_dir()
    # tmp_path should contain settings.data_dir (it's tmp_path / "data" by fixture)
    assert tmp_path in settings.data_dir.parents
