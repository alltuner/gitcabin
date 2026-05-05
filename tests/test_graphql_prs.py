# ABOUTME: Tests for the PullRequest GraphQL surface — list, single, comments, viewer_can_*.
# ABOUTME: Real bare repos populated via storage helpers; resolves through the live schema.

from __future__ import annotations

from collections.abc import Callable

from fastapi.testclient import TestClient

from gitcabin.storage.prs import (
    PrState,
    create_local_pr,
    import_pr,
    import_pr_comment,
)
from gitcabin.storage.repo import BareRepo
from gitcabin.sync.config import SyncConfig, write_config

PR_LIST = """
query L($owner: String!, $name: String!) {
  repository(owner: $owner, name: $name) {
    pullRequests(first: 100) {
      totalCount
      nodes { number title state merged isDraft headRefName baseRefName }
    }
  }
}
"""

PR_SINGLE = """
query S($owner: String!, $name: String!, $number: Int!) {
  repository(owner: $owner, name: $name) {
    pullRequest(number: $number) {
      number title state merged
      author { login }
      viewerDidAuthor
      viewerCanUpdate
      viewerCanCloseOrReopen
      comments(first: 50) {
        nodes {
          body
          author { login }
          viewerCanUpdate
          viewerCanDelete
        }
      }
    }
  }
}
"""

ISSUE_OR_PR = """
query IoP($owner: String!, $name: String!, $number: Int!) {
  repository(owner: $owner, name: $name) {
    issueOrPullRequest(number: $number) {
      __typename
      ... on PullRequest { number title }
      ... on Issue { number title }
    }
  }
}
"""


def _post(client: TestClient, query: str, variables: dict) -> dict:
    response = client.post("/graphql", json={"query": query, "variables": variables})
    assert response.status_code == 200
    return response.json()


def test_pull_requests_list_returns_synced_prs(
    client: TestClient,
    init_repo: Callable[[str, str], BareRepo],
) -> None:
    repo = init_repo("octocat", "hello")
    import_pr(
        repo,
        number=10,
        title="add feature",
        body="b",
        author="alice",
        state=PrState.OPEN,
        head_ref="alice:feature",
        base_ref="main",
        is_draft=False,
        gh_pr_id=110,
    )
    import_pr(
        repo,
        number=11,
        title="merged one",
        body="b",
        author="alice",
        state=PrState.MERGED,
        head_ref="alice:fix",
        base_ref="main",
        is_draft=False,
        gh_pr_id=111,
    )

    payload = _post(client, PR_LIST, {"owner": "octocat", "name": "hello"})
    assert "errors" not in payload, payload
    prs = payload["data"]["repository"]["pullRequests"]
    assert prs["totalCount"] == 2
    by_number = {n["number"]: n for n in prs["nodes"]}
    assert by_number[10]["state"] == "OPEN"
    assert by_number[10]["merged"] is False
    assert by_number[10]["headRefName"] == "alice:feature"
    assert by_number[11]["state"] == "CLOSED"  # merged maps to CLOSED on the wire
    assert by_number[11]["merged"] is True


def test_pull_request_single_includes_comments_and_viewer_fields(
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
    import_pr(
        repo,
        number=10,
        title="alice's pr",
        body="",
        author="alice",
        state=PrState.OPEN,
        head_ref="alice:f",
        base_ref="main",
        is_draft=False,
        gh_pr_id=110,
    )
    import_pr_comment(
        repo, pr_number=10, body="alice review", author="alice", gh_comment_id=2001
    )

    payload = _post(
        client, PR_SINGLE, {"owner": "octocat", "name": "hello", "number": 10}
    )
    assert "errors" not in payload, payload
    pr = payload["data"]["repository"]["pullRequest"]
    assert pr["number"] == 10
    assert pr["author"]["login"] == "alice"
    assert pr["viewerDidAuthor"] is False
    assert pr["viewerCanUpdate"] is False  # never edit content not yours
    assert pr["viewerCanCloseOrReopen"] is False  # READ role, not author

    [comment] = pr["comments"]["nodes"]
    assert comment["body"] == "alice review"
    assert comment["author"]["login"] == "alice"
    assert comment["viewerCanUpdate"] is False
    assert comment["viewerCanDelete"] is False  # not ADMIN


def test_pull_request_admin_role_can_close_others_pr(
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
    import_pr(
        repo,
        number=10,
        title="alice's pr",
        body="",
        author="alice",
        state=PrState.OPEN,
        head_ref="alice:f",
        base_ref="main",
        is_draft=False,
        gh_pr_id=110,
    )

    payload = _post(
        client, PR_SINGLE, {"owner": "octocat", "name": "hello", "number": 10}
    )
    pr = payload["data"]["repository"]["pullRequest"]
    assert pr["viewerCanUpdate"] is False  # never — content edit
    assert pr["viewerCanCloseOrReopen"] is True  # ADMIN moderation


def test_issue_or_pull_request_resolves_to_pull_request_when_pr_exists(
    client: TestClient,
    init_repo: Callable[[str, str], BareRepo],
) -> None:
    repo = init_repo("octocat", "hello")
    import_pr(
        repo,
        number=42,
        title="a pr",
        body="",
        author="alice",
        state=PrState.OPEN,
        head_ref="x",
        base_ref="main",
        is_draft=False,
        gh_pr_id=900,
    )
    payload = _post(client, ISSUE_OR_PR, {"owner": "octocat", "name": "hello", "number": 42})
    assert "errors" not in payload, payload
    item = payload["data"]["repository"]["issueOrPullRequest"]
    assert item["__typename"] == "PullRequest"
    assert item["number"] == 42


def test_issue_or_pull_request_falls_back_to_issue_when_no_pr(
    client: TestClient,
    init_repo: Callable[[str, str], BareRepo],
) -> None:
    from gitcabin.storage.issues import IssueState, import_issue

    repo = init_repo("octocat", "hello")
    import_issue(
        repo,
        number=42,
        title="just an issue",
        body="",
        author="alice",
        state=IssueState.OPEN,
        gh_issue_id=1,
    )

    payload = _post(client, ISSUE_OR_PR, {"owner": "octocat", "name": "hello", "number": 42})
    item = payload["data"]["repository"]["issueOrPullRequest"]
    assert item["__typename"] == "Issue"
    assert item["title"] == "just an issue"


# ---- local PR surfacing ------------------------------------------------ #


def test_pull_requests_list_includes_local_drafts(
    client: TestClient,
    init_repo: Callable[[str, str], BareRepo],
) -> None:
    repo = init_repo("octocat", "hello")
    create_local_pr(
        repo,
        title="local draft",
        body="WIP",
        author="david",
        head_ref="david:wip",
        base_ref="main",
        is_draft=True,
    )
    import_pr(
        repo,
        number=42,
        title="synced one",
        body="",
        author="alice",
        state=PrState.OPEN,
        head_ref="alice:f",
        base_ref="main",
        is_draft=False,
        gh_pr_id=900,
    )

    payload = _post(client, PR_LIST, {"owner": "octocat", "name": "hello"})
    assert "errors" not in payload, payload
    prs = payload["data"]["repository"]["pullRequests"]
    assert prs["totalCount"] == 2
    by_title = {n["title"]: n for n in prs["nodes"]}
    assert "synced one" in by_title and "local draft" in by_title
    assert by_title["local draft"]["isDraft"] is True


def test_pull_request_lookup_finds_local_pr_by_number(
    client: TestClient,
    init_repo: Callable[[str, str], BareRepo],
) -> None:
    repo = init_repo("octocat", "hello")
    pr = create_local_pr(
        repo,
        title="my draft",
        body="describe",
        author="david",
        head_ref="david:wip",
        base_ref="main",
    )
    payload = _post(
        client, PR_SINGLE, {"owner": "octocat", "name": "hello", "number": pr.number}
    )
    assert "errors" not in payload, payload
    item = payload["data"]["repository"]["pullRequest"]
    assert item["number"] == pr.number
    assert item["author"]["login"] == "david"
    # Author is the local viewer (default 'david' from Settings) so all
    # affordances are open.
    assert item["viewerDidAuthor"] is True
    assert item["viewerCanCloseOrReopen"] is True
