# ABOUTME: Tests for the web UI's branches/tags listing, blame, and diff rendering.
# ABOUTME: Reuses test_web_code's seeding helpers; ref creation goes through git plumbing.

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from gitcabin.storage.repo import BareRepo

from .test_web_code import _seed_repo


@pytest.fixture
def history(init_repo) -> tuple[BareRepo, str, str]:
    """Repo with two commits on main and a feature branch off the first.

    Returns (bare, first_sha, second_sha) so tests can address either commit.
    """
    bare = init_repo("octocat", "hello")
    first = _seed_repo(bare, {"README.md": "# v1\n", "src/main.py": "print('a')\n"})
    second = _seed_repo(bare, {"README.md": "# v2\n", "src/main.py": "print('b')\n"}, "v2")
    # Branch off first commit so the branches page has more than just main.
    bare.run_git("update-ref", "refs/heads/feature", first)
    # Tag the second commit so the tags listing has an entry.
    bare.run_git("update-ref", "refs/tags/v1", second)
    return bare, first, second


def test_branches_page_lists_heads_and_tags(
    web_client: TestClient, history: tuple[BareRepo, str, str]
) -> None:
    response = web_client.get("/octocat/hello/branches")
    assert response.status_code == 200
    body = response.text
    # main is the default — labelled as such by the template.
    assert "main" in body
    assert "feature" in body
    assert "v1" in body  # the tag
    assert "default" in body  # the pill on main


def test_commit_view_renders_unified_diff(
    web_client: TestClient, history: tuple[BareRepo, str, str]
) -> None:
    _, _, second = history
    response = web_client.get(f"/octocat/hello/commit/{second}")
    assert response.status_code == 200
    body = response.text
    # Diffs render as a structured table with diff-add / diff-remove rows.
    assert 'class="diff-table"' in body
    assert "diff-add" in body or "diff-remove" in body
    # The actual diff content is present somewhere in the page.
    assert "v2" in body


def test_commit_view_initial_commit_has_no_diff(
    web_client: TestClient, history: tuple[BareRepo, str, str]
) -> None:
    # The first commit has no parent, so there's no diff to render — the page
    # still renders the changed-files list.
    _, first, _ = history
    response = web_client.get(f"/octocat/hello/commit/{first}")
    assert response.status_code == 200
    body = response.text
    assert "src/main.py" in body
    assert "README.md" in body


def test_blame_view_attributes_each_line(
    web_client: TestClient, history: tuple[BareRepo, str, str]
) -> None:
    response = web_client.get("/octocat/hello/blame/main/README.md")
    assert response.status_code == 200
    body = response.text
    # The most recent commit on main owns the README at HEAD.
    _, _, second = history
    assert second[:7] in body
    assert "v2" in body  # the file's content


def test_blame_404_for_missing_path(
    web_client: TestClient, history: tuple[BareRepo, str, str]
) -> None:
    response = web_client.get("/octocat/hello/blame/main/no-such-file")
    assert response.status_code == 404


def test_blob_page_links_to_blame(
    web_client: TestClient, history: tuple[BareRepo, str, str]
) -> None:
    response = web_client.get("/octocat/hello/blob/main/README.md")
    assert "/octocat/hello/blame/main/README.md" in response.text


def test_diff_truncates_when_oversized(web_client: TestClient, init_repo) -> None:
    bare = init_repo("octocat", "huge")
    # Initial commit so there's a parent for the second commit.
    _seed_repo(bare, {"README.md": "ok\n"})
    # Second commit drops a 1.2 MB blob — bigger than MAX_DIFF_RENDER_BYTES.
    big = "x" * 1_200_000
    _seed_repo(bare, {"README.md": "ok\n", "huge.txt": big}, "add huge")

    response = web_client.get(f"/octocat/huge/commit/{bare.run_git('rev-parse', 'main').strip()}")
    assert response.status_code == 200
    assert "Diff too large" in response.text
