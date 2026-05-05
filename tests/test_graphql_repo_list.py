# ABOUTME: Tests for the repositoryOwner GraphQL resolver (gh repo list).
# ABOUTME: Walks data/projects/<owner>/*.git on disk; mirrors the query gh repo list sends.

from __future__ import annotations

from fastapi.testclient import TestClient

# Mirrors the query template gh's pkg/cmd/repo/list/http.go builds. defaultFields
# = nameWithOwner, description, isPrivate, isFork, isArchived, createdAt, pushedAt.
REPO_LIST_QUERY = """
query RepositoryList(
  $owner: String!,
  $perPage: Int!,
  $endCursor: String,
  $privacy: RepositoryPrivacy,
  $fork: Boolean
) {
  repositoryOwner(login: $owner) {
    login
    repositories(
      first: $perPage,
      after: $endCursor,
      privacy: $privacy,
      isFork: $fork,
      ownerAffiliations: OWNER,
      orderBy: { field: PUSHED_AT, direction: DESC }
    ) {
      nodes {
        nameWithOwner
        description
        isPrivate
        isFork
        isArchived
        createdAt
        pushedAt
        visibility
      }
      totalCount
      pageInfo { hasNextPage endCursor }
    }
  }
}
"""


def _post(client: TestClient, query: str, variables: dict) -> dict:
    response = client.post("/graphql", json={"query": query, "variables": variables})
    assert response.status_code == 200
    return response.json()


def test_repository_owner_returns_null_for_absent_owner(client: TestClient) -> None:
    # No repos under data/projects/ghost/ — strict mode says null, same
    # contract as Query.repository on a missing repo.
    payload = _post(client, REPO_LIST_QUERY, {"owner": "ghost", "perPage": 30})
    assert "errors" not in payload, payload
    assert payload["data"]["repositoryOwner"] is None


def test_repository_owner_returns_empty_connection_for_owner_with_no_repos(
    client: TestClient, settings
) -> None:
    # Owner directory exists but has no repos. The owner is still resolvable
    # (login is just the directory name), and the connection is empty.
    (settings.data_dir / "projects" / "octocat").mkdir(parents=True)
    payload = _post(client, REPO_LIST_QUERY, {"owner": "octocat", "perPage": 30})
    assert "errors" not in payload, payload
    owner = payload["data"]["repositoryOwner"]
    assert owner is not None
    assert owner["login"] == "octocat"
    assert owner["repositories"]["totalCount"] == 0
    assert owner["repositories"]["nodes"] == []


def test_repository_owner_lists_existing_repos(client: TestClient, init_repo) -> None:
    init_repo("octocat", "hello")
    init_repo("octocat", "world")

    payload = _post(client, REPO_LIST_QUERY, {"owner": "octocat", "perPage": 30})
    assert "errors" not in payload, payload
    owner = payload["data"]["repositoryOwner"]
    assert owner["login"] == "octocat"
    repos = owner["repositories"]
    assert repos["totalCount"] == 2
    name_set = {n["nameWithOwner"] for n in repos["nodes"]}
    assert name_set == {"octocat/hello", "octocat/world"}

    sample = repos["nodes"][0]
    assert sample["isPrivate"] is False
    assert sample["isFork"] is False
    assert sample["isArchived"] is False
    assert sample["visibility"] == "PUBLIC"
    # ISO-8601-shaped strings (4-digit year, dash separator).
    assert sample["createdAt"][:5].endswith("-")
    assert sample["pushedAt"][:5].endswith("-")


def test_repository_owner_respects_per_page(client: TestClient, init_repo) -> None:
    for n in ("a", "b", "c", "d", "e"):
        init_repo("octocat", n)

    payload = _post(client, REPO_LIST_QUERY, {"owner": "octocat", "perPage": 2})
    repos = payload["data"]["repositoryOwner"]["repositories"]
    assert repos["totalCount"] == 5
    assert len(repos["nodes"]) == 2
    assert repos["pageInfo"]["hasNextPage"] is True


VIEWER_REPO_LIST_QUERY = """
query RepositoryListViewer($perPage: Int!, $endCursor: String) {
  repositoryOwner: viewer {
    login
    repositories(
      first: $perPage,
      after: $endCursor,
      ownerAffiliations: OWNER,
      orderBy: { field: PUSHED_AT, direction: DESC }
    ) {
      nodes { nameWithOwner }
      totalCount
      pageInfo { hasNextPage endCursor }
    }
  }
}
"""


def test_viewer_repositories_lists_repos_under_viewer_login(client: TestClient, init_repo) -> None:
    # gh repo list (no owner arg) rewrites the query to `repositoryOwner: viewer`,
    # so User must expose a `repositories` connection of the same shape as
    # RepositoryOwner.repositories. This is the path that lights up `gh repo list`
    # without arguments.
    init_repo("david", "scratch")
    init_repo("david", "notes")
    init_repo("octocat", "hello")  # different owner — should not appear

    payload = _post(client, VIEWER_REPO_LIST_QUERY, {"perPage": 30})
    assert "errors" not in payload, payload
    owner = payload["data"]["repositoryOwner"]
    assert owner["login"] == "david"
    repos = owner["repositories"]
    assert repos["totalCount"] == 2
    assert {n["nameWithOwner"] for n in repos["nodes"]} == {
        "david/scratch",
        "david/notes",
    }


def test_viewer_repositories_empty_when_viewer_dir_absent(client: TestClient) -> None:
    # No data/projects/david/ — viewer still resolves (it's the configured
    # login), the connection is just empty.
    payload = _post(client, VIEWER_REPO_LIST_QUERY, {"perPage": 30})
    assert "errors" not in payload, payload
    repos = payload["data"]["repositoryOwner"]["repositories"]
    assert repos["totalCount"] == 0
    assert repos["nodes"] == []


def test_repository_owner_ignores_non_bare_directories(
    client: TestClient, init_repo, settings
) -> None:
    # data/projects/<owner>/ may grow non-repo entries (logs, lockfiles, etc.)
    # over time. Only directories ending in `.git` and verifying as bare repos
    # should appear in the connection.
    init_repo("octocat", "hello")
    (settings.data_dir / "projects" / "octocat" / "not-a-repo").mkdir()
    (settings.data_dir / "projects" / "octocat" / "stray.txt").write_text("ignore me")

    payload = _post(client, REPO_LIST_QUERY, {"owner": "octocat", "perPage": 30})
    repos = payload["data"]["repositoryOwner"]["repositories"]
    assert repos["totalCount"] == 1
    assert repos["nodes"][0]["nameWithOwner"] == "octocat/hello"
