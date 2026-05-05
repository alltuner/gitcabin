# ABOUTME: Tests for the GraphQL Query.repository resolver.
# ABOUTME: Mirrors the IssueRepositoryInfo query gh sends before any issue or PR operation.

from fastapi.testclient import TestClient

# This is the exact query gh sends from api/queries_repo.go::IssueRepoInfo.
# It's the smallest repository selection in gh's source, so it's the right
# floor for our resolver: every repo-aware command queries at least this much.
ISSUE_REPO_INFO_QUERY = """
query IssueRepositoryInfo($owner: String!, $name: String!) {
  repository(owner: $owner, name: $name) {
    id
    name
    owner { login }
    hasIssuesEnabled
    viewerPermission
  }
}
"""


def post_graphql(client: TestClient, query: str, variables: dict) -> dict:
    response = client.post("/graphql", json={"query": query, "variables": variables})
    assert response.status_code == 200
    return response.json()


def test_repository_resolver_returns_owner_and_name(client: TestClient, init_repo) -> None:
    init_repo("octocat", "hello")
    payload = post_graphql(client, ISSUE_REPO_INFO_QUERY, {"owner": "octocat", "name": "hello"})

    assert "errors" not in payload, payload
    repo = payload["data"]["repository"]
    assert repo is not None
    assert repo["name"] == "hello"
    assert repo["owner"]["login"] == "octocat"


def test_repository_resolver_returns_null_for_absent_repo(client: TestClient) -> None:
    # No init_repo call: data/projects/octocat/hello.git doesn't exist on
    # disk. Strict mode means Repository.repository must return null, not
    # invent a fixture. gh's IssueRepoInfo treats null as NOT_FOUND.
    payload = post_graphql(client, ISSUE_REPO_INFO_QUERY, {"owner": "octocat", "name": "hello"})
    assert "errors" not in payload, payload
    assert payload["data"]["repository"] is None


def test_repository_resolver_exposes_issue_capability_flags(client: TestClient, init_repo) -> None:
    # gh's IssueRepoInfo selects hasIssuesEnabled (bool) and viewerPermission
    # (a string-shaped enum). gh treats viewerPermission == "READ" as
    # "you can't write" — for a self-hosted clone the owner is always full
    # admin, so we lock the value here to keep that contract obvious.
    init_repo("octocat", "hello")
    payload = post_graphql(client, ISSUE_REPO_INFO_QUERY, {"owner": "octocat", "name": "hello"})
    repo = payload["data"]["repository"]
    assert repo["hasIssuesEnabled"] is True
    assert repo["viewerPermission"] == "ADMIN"


def test_repository_view_query(client: TestClient, init_repo) -> None:
    init_repo("octocat", "hello")
    # Mirrors what `gh repo view octocat/hello` actually sends. gh's RepositoryGraphQL
    # builder rewrites the "owner" field selector into `owner{id,login}`, so
    # User must expose an id alongside login. description must exist on Repository.
    repo_view_query = """
    query RepositoryInfo($owner: String!, $name: String!) {
      repository(owner: $owner, name: $name) {
        name
        owner { id login }
        description
      }
    }
    """
    payload = post_graphql(client, repo_view_query, {"owner": "octocat", "name": "hello"})

    assert "errors" not in payload, payload
    repo = payload["data"]["repository"]
    assert repo["name"] == "hello"
    assert repo["owner"]["login"] == "octocat"
    assert isinstance(repo["owner"]["id"], str) and repo["owner"]["id"]
    # description is nullable, but the field must exist on the type
    assert "description" in repo


def test_repository_view_json_fields(client: TestClient, init_repo) -> None:
    # `gh repo view --json url,defaultBranchRef,nameWithOwner` selects the
    # fields that gh's RepositoryFields exporter exposes. Each is on
    # GitHub's Repository type, so we have to expose them too — even when
    # the values come from local git state instead of an external API.
    init_repo("octocat", "hello")
    query = """
    query($owner: String!, $name: String!) {
      repository(owner: $owner, name: $name) {
        url
        nameWithOwner
        defaultBranchRef { name }
      }
    }
    """
    payload = post_graphql(client, query, {"owner": "octocat", "name": "hello"})
    assert "errors" not in payload, payload
    repo = payload["data"]["repository"]
    assert repo["url"] == "http://github.localhost/octocat/hello"
    assert repo["nameWithOwner"] == "octocat/hello"
    # Fresh bare init defaults the symbolic HEAD to refs/heads/main.
    assert repo["defaultBranchRef"]["name"] == "main"


def test_repository_id_is_stable_across_calls(client: TestClient, init_repo) -> None:
    # The id is opaque to gh but must be stable for the same (owner, name)
    # pair so it can be used as a foreign key on issues/PRs across requests.
    init_repo("octocat", "hello")
    a = post_graphql(client, ISSUE_REPO_INFO_QUERY, {"owner": "octocat", "name": "hello"})
    b = post_graphql(client, ISSUE_REPO_INFO_QUERY, {"owner": "octocat", "name": "hello"})
    assert a["data"]["repository"]["id"] == b["data"]["repository"]["id"]
