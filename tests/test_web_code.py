# ABOUTME: Tests for the web UI's code-browser routes (Slice 2).
# ABOUTME: Seeds the bare repo with real commits, then asserts on rendered HTML.

from __future__ import annotations

import subprocess

import pytest
from fastapi.testclient import TestClient

from gitcabin.storage.repo import BareRepo


def _hash_blob(bare: BareRepo, content: bytes) -> str:
    """Hash a (possibly binary) blob into the repo's object database."""
    result = subprocess.run(
        ["git", "hash-object", "-w", "--stdin"],
        cwd=bare.path,
        input=content,
        capture_output=True,
        check=True,
    )
    return result.stdout.decode().strip()


def _build_tree(bare: BareRepo, files: dict[str, bytes]) -> str:
    """Build a (possibly nested) tree from path → bytes. Returns root tree sha.

    `git mktree` only takes flat entries (one level deep), so nested paths are
    grouped per directory and built leaf-first, with each subtree referenced
    by its sha in the parent.
    """
    own: dict[str, bytes] = {}
    nested: dict[str, dict[str, bytes]] = {}
    for path, content in files.items():
        if "/" in path:
            head, _, tail = path.partition("/")
            nested.setdefault(head, {})[tail] = content
        else:
            own[path] = content
    entries: list[str] = []
    for name, content in own.items():
        entries.append(f"100644 blob {_hash_blob(bare, content)}\t{name}")
    for dirname, sub_files in nested.items():
        sub_sha = _build_tree(bare, sub_files)
        entries.append(f"040000 tree {sub_sha}\t{dirname}")
    return bare.run_git("mktree", input="\n".join(entries) + "\n").strip()


def _seed_repo(bare: BareRepo, files: dict[str, str], message: str = "initial") -> str:
    """Commit `files` (path → text) on refs/heads/main, parented to current main if any.

    Threading the parent matters for the commits-log test: without it, each
    successive seed orphans the prior commit and `iter_commits('main')` only
    sees the most recent.
    """
    tree_sha = _build_tree(bare, {p: c.encode() for p, c in files.items()})
    args = [
        "-c",
        "user.name=Tester",
        "-c",
        "user.email=tester@example.com",
        "commit-tree",
        tree_sha,
        "-m",
        message,
    ]
    parent = subprocess.run(
        ["git", "rev-parse", "--verify", "--quiet", "refs/heads/main"],
        cwd=bare.path,
        capture_output=True,
        text=True,
        check=False,
    )
    if parent.returncode == 0:
        args[args.index("commit-tree") + 1 : args.index("commit-tree") + 1] = [
            "-p",
            parent.stdout.strip(),
        ]
    commit_sha = bare.run_git(*args).strip()
    bare.run_git("update-ref", "refs/heads/main", commit_sha)
    return commit_sha


@pytest.fixture
def seeded(settings, init_repo) -> tuple[BareRepo, str]:
    """A bare repo with a tiny tree (README.md + src/main.py) committed on main."""
    bare = init_repo("octocat", "hello")
    sha = _seed_repo(
        bare,
        {
            "README.md": "# hello\n\nThis is **markdown**.",
            "src/main.py": "def main() -> None:\n    print('hi')\n",
        },
    )
    return bare, sha


def test_repo_overview_renders_readme_and_tree(
    web_client: TestClient, seeded: tuple[BareRepo, str]
) -> None:
    response = web_client.get("/octocat/hello")
    assert response.status_code == 200
    body = response.text
    # Top-level entries: README.md (blob), src (tree).
    assert "README.md" in body
    assert "src" in body
    # README.md is rendered as Markdown (the **markdown** becomes <strong>).
    assert "<strong>markdown</strong>" in body


def test_tree_subdirectory(web_client: TestClient, seeded: tuple[BareRepo, str]) -> None:
    response = web_client.get("/octocat/hello/tree/main/src")
    assert response.status_code == 200
    body = response.text
    assert "main.py" in body
    # Crumbs let the user walk back to the root tree.
    assert "/octocat/hello/tree/main" in body


def test_tree_404_for_unknown_ref(web_client: TestClient, seeded: tuple[BareRepo, str]) -> None:
    response = web_client.get("/octocat/hello/tree/no-such-branch")
    assert response.status_code == 404


def test_tree_404_for_unknown_path(web_client: TestClient, seeded: tuple[BareRepo, str]) -> None:
    response = web_client.get("/octocat/hello/tree/main/does/not/exist")
    assert response.status_code == 404


def test_blob_renders_highlighted(web_client: TestClient, seeded: tuple[BareRepo, str]) -> None:
    response = web_client.get("/octocat/hello/blob/main/src/main.py")
    assert response.status_code == 200
    body = response.text
    # Pygments HtmlFormatter wraps tokens in span elements with classes — the
    # exact class names are stable enough to assert on. We don't pin colours.
    assert 'class="hl"' in body or "highlight" in body
    # The actual source line shows up after tokenization.
    assert "def" in body and "main" in body


def test_blob_404_when_path_is_a_tree(web_client: TestClient, seeded: tuple[BareRepo, str]) -> None:
    # /blob/ on a directory must 404 (gh.com mirrors this).
    response = web_client.get("/octocat/hello/blob/main/src")
    assert response.status_code == 404


def test_commits_page_lists_commits(web_client: TestClient, seeded: tuple[BareRepo, str]) -> None:
    bare, _ = seeded
    _seed_repo(bare, {"README.md": "# hello v2"}, message="bump readme")
    response = web_client.get("/octocat/hello/commits/main")
    assert response.status_code == 200
    body = response.text
    assert "bump readme" in body
    assert "initial" in body


def test_commit_view_shows_metadata_and_changed_files(
    web_client: TestClient, seeded: tuple[BareRepo, str]
) -> None:
    _, sha = seeded
    response = web_client.get(f"/octocat/hello/commit/{sha}")
    assert response.status_code == 200
    body = response.text
    assert sha in body
    assert "Tester" in body
    # Initial commit reports every file as added.
    assert "README.md" in body
    assert "src/main.py" in body


def test_pygments_stylesheet_is_served(web_client: TestClient) -> None:
    response = web_client.get("/highlight.css")
    assert response.status_code == 200
    assert "text/css" in response.headers["content-type"]
    # The stylesheet defines styles scoped under the .hl class our formatter uses.
    assert ".hl" in response.text


def test_repo_overview_handles_empty_repo(web_client: TestClient, init_repo, settings) -> None:
    # Fresh bare repo, no commits — overview should render without raising
    # and tell the user the repo is empty.
    init_repo("octocat", "scratch")
    response = web_client.get("/octocat/scratch")
    assert response.status_code == 200
    assert "this repository is empty" in response.text.lower()


def test_blob_viewer_handles_binary(web_client: TestClient, seeded: tuple[BareRepo, str]) -> None:
    bare, _ = seeded
    # Add a binary blob — NUL bytes trip the binary heuristic.
    blob_sha = _hash_blob(bare, b"\x00\x01\x02\x03")
    tree_sha = bare.run_git("mktree", input=f"100644 blob {blob_sha}\tdata.bin\n").strip()
    commit_sha = bare.run_git(
        "-c",
        "user.name=Tester",
        "-c",
        "user.email=tester@example.com",
        "commit-tree",
        tree_sha,
        "-m",
        "add binary",
    ).strip()
    bare.run_git("update-ref", "refs/heads/main", commit_sha)

    response = web_client.get("/octocat/hello/blob/main/data.bin")
    assert response.status_code == 200
    assert "Binary file" in response.text
