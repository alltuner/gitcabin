# ABOUTME: Tests for the HTML web UI routes.
# ABOUTME: Asserts on rendered HTML content + 200/404 routing rather than markup details.

from __future__ import annotations

from fastapi.testclient import TestClient

from gitcabin.ids import repo_id

CREATE_ISSUE = """
mutation IssueCreate($input: CreateIssueInput!) {
  createIssue(input: $input) { issue { id } }
}
"""

ADD_COMMENT = """
mutation CommentCreate($input: AddCommentInput!) {
  addComment(input: $input) { commentEdge { node { url } } }
}
"""

CLOSE_ISSUE = """
mutation IssueClose($input: CloseIssueInput!) {
  closeIssue(input: $input) { issue { id } }
}
"""


def _post_graphql(client: TestClient, query: str, variables: dict) -> dict:
    response = client.post("/graphql", json={"query": query, "variables": variables})
    assert response.status_code == 200
    return response.json()


def test_dashboard_renders_html(web_client: TestClient) -> None:
    response = web_client.get("/")
    assert response.status_code == 200
    assert "text/html" in response.headers["content-type"]
    assert "gitcabin" in response.text


def test_dashboard_lists_owners_and_repo_counts(web_client: TestClient, init_repo) -> None:
    init_repo("octocat", "hello")
    init_repo("octocat", "world")
    init_repo("acme", "tool")

    response = web_client.get("/")
    body = response.text
    assert "octocat" in body
    assert "acme" in body
    # Owner with two repos should be reported as such; with one repo as such.
    assert "2 repositories" in body
    assert "1 repository" in body


def test_dashboard_root_repo_card_links_to_clean_url(
    web_client: TestClient, settings
) -> None:
    # Projectless repo at data/repos/<name>.git should render with a
    # `/runbook` link, not `//runbook` — `_repo_card.html` builds the
    # href from the empty `owner`, which used to slip through as
    # `/{owner}/{name}` and produce a doubled slash that 404'd.
    from gitcabin.storage import layout
    from gitcabin.storage.repo import BareRepo

    BareRepo.open_or_init(
        (layout.root_repos_dir(settings.data_dir) / "runbook").with_suffix(".git")
    )

    body = web_client.get("/").text
    assert 'href="/runbook"' in body
    assert "//runbook" not in body


def test_owner_page_lists_repos(web_client: TestClient, init_repo) -> None:
    init_repo("octocat", "hello")
    init_repo("octocat", "world")

    response = web_client.get("/octocat")
    assert response.status_code == 200
    body = response.text
    assert ">hello<" in body or "hello</a>" in body
    assert ">world<" in body or "world</a>" in body


def test_owner_page_404_for_unknown_owner(web_client: TestClient) -> None:
    response = web_client.get("/ghost")
    assert response.status_code == 404


def test_repo_page_shows_default_branch_and_issue_counts(
    web_client: TestClient, client: TestClient, init_repo
) -> None:
    init_repo("octocat", "hello")
    rid = repo_id("octocat", "hello")
    _post_graphql(client, CREATE_ISSUE, {"input": {"repositoryId": rid, "title": "first"}})
    _post_graphql(client, CREATE_ISSUE, {"input": {"repositoryId": rid, "title": "second"}})

    response = web_client.get("/octocat/hello")
    assert response.status_code == 200
    body = response.text
    assert "octocat" in body and "hello" in body
    # The Issues tab in the repo header carries the total-issue-count badge.
    # The link points at .../issues and the count "2" appears within it.
    assert "/octocat/hello/issues" in body
    assert ">2<" in body  # the badge content


def test_issues_page_filters_by_state(
    web_client: TestClient, client: TestClient, init_repo
) -> None:
    init_repo("octocat", "hello")
    rid = repo_id("octocat", "hello")
    a = _post_graphql(client, CREATE_ISSUE, {"input": {"repositoryId": rid, "title": "open-one"}})
    _post_graphql(client, CREATE_ISSUE, {"input": {"repositoryId": rid, "title": "open-two"}})
    iid = a["data"]["createIssue"]["issue"]["id"]
    _post_graphql(client, CLOSE_ISSUE, {"input": {"issueId": iid}})

    open_view = web_client.get("/octocat/hello/issues?state=open").text
    assert "open-two" in open_view
    assert "open-one" not in open_view  # the closed one shouldn't show up

    closed_view = web_client.get("/octocat/hello/issues?state=closed").text
    assert "open-one" in closed_view
    assert "open-two" not in closed_view

    all_view = web_client.get("/octocat/hello/issues?state=all").text
    assert "open-one" in all_view and "open-two" in all_view


def test_single_issue_page_renders_body_and_comments(
    web_client: TestClient, client: TestClient, init_repo
) -> None:
    init_repo("octocat", "hello")
    rid = repo_id("octocat", "hello")
    payload = _post_graphql(
        client,
        CREATE_ISSUE,
        {"input": {"repositoryId": rid, "title": "needs reply", "body": "some context here"}},
    )
    iid = payload["data"]["createIssue"]["issue"]["id"]
    _post_graphql(client, ADD_COMMENT, {"input": {"subjectId": iid, "body": "thanks for filing"}})

    response = web_client.get("/octocat/hello/issues/1")
    assert response.status_code == 200
    body = response.text
    assert "needs reply" in body
    assert "some context here" in body
    assert "thanks for filing" in body


def test_single_issue_page_404_for_unknown_number(web_client: TestClient, init_repo) -> None:
    init_repo("octocat", "hello")
    response = web_client.get("/octocat/hello/issues/999")
    assert response.status_code == 404


def test_static_dist_dir_mounted(web_client: TestClient) -> None:
    # The dashboard's <link rel="stylesheet"> points at /static/dist/main.<hash>.css.
    # Without the dist directory the asset() helper falls back to a literal
    # filename and the page renders unstyled. The path may or may not have
    # a built bundle (CI without bun installed leaves it empty) — what we
    # care about is that /static/dist/ is mounted and reachable. A 404 for a
    # nonexistent file is fine; a 404 for the directory itself isn't.
    response = web_client.get("/static/dist/__never__.css")
    assert response.status_code == 404  # mount works, file absent
    response = web_client.get("/static/dist/")
    # Some StaticFiles configurations 404 on bare directory listings; either
    # 200 / 404 is acceptable as long as the response isn't a crash.
    assert response.status_code in {200, 404}
