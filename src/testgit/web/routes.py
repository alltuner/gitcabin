# ABOUTME: HTML routes for the web UI. Read-only views over data/repos and side refs.
# ABOUTME: Lives in its own FastAPI app (testgit.web.app) — separate process from gh's API.

from __future__ import annotations

from html import escape as html_escape
from pathlib import Path

from fastapi import APIRouter, Form, HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse, Response
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

from testgit.config import Settings
from testgit.storage.issues import (
    IssueState,
    add_comment,
    close_issue,
    get_issue,
    list_comments,
    list_issues,
    reopen_issue,
)
from testgit.storage.repo import BareRepo
from testgit.web import code

_WEB_DIR = Path(__file__).parent
_templates = Jinja2Templates(directory=str(_WEB_DIR / "templates"))


def _render(request: Request, settings: Settings, template: str, **ctx: object) -> HTMLResponse:
    """Render a template with the always-needed context (request, viewer)."""
    return _templates.TemplateResponse(
        request,
        template,
        {"viewer_login": settings.viewer_login, **ctx},
    )


def _list_owners(settings: Settings) -> list[dict[str, object]]:
    """Walk data_dir/repos/ and return one entry per owner directory."""
    repos_root = settings.data_dir / "repos"
    if not repos_root.is_dir():
        return []
    owners: list[dict[str, object]] = []
    for owner_dir in sorted(repos_root.iterdir()):
        if not owner_dir.is_dir():
            continue
        repo_count = sum(
            1
            for entry in owner_dir.iterdir()
            if entry.name.endswith(".git") and BareRepo.open(entry) is not None
        )
        owners.append({"login": owner_dir.name, "repo_count": repo_count})
    return owners


def _list_repos(settings: Settings, owner: str) -> list[dict[str, object]]:
    """List repos under data_dir/repos/<owner>/, sorted by pushed_at desc."""
    owner_dir = settings.data_dir / "repos" / owner
    if not owner_dir.is_dir():
        return []
    out: list[dict[str, object]] = []
    for entry in sorted(owner_dir.iterdir()):
        if not entry.name.endswith(".git"):
            continue
        bare = BareRepo.open(entry)
        if bare is None:
            continue
        out.append(
            {
                "name": entry.name[:-4],
                "description": None,
                "pushed_at": _repo_pushed_at(bare),
            }
        )
    out.sort(key=lambda r: r["pushed_at"], reverse=True)
    return out


def _repo_pushed_at(bare: BareRepo) -> str:
    """ISO timestamp for the latest commit on any branch, or the dir mtime."""
    from datetime import UTC, datetime

    commits = list(bare.repo.iter_commits("--all"))
    if not commits:
        return datetime.fromtimestamp(bare.path.stat().st_mtime, tz=UTC).isoformat()
    return max(c.authored_datetime for c in commits).isoformat()


def _open_repo(settings: Settings, owner: str, name: str) -> BareRepo:
    """Resolve a (owner, name) URL segment pair to a BareRepo or 404."""
    bare = BareRepo.open(settings.data_dir / "repos" / owner / f"{name}.git")
    if bare is None:
        raise HTTPException(status_code=404, detail="repo not found")
    return bare


def _path_crumbs(path: str) -> list[dict[str, str]]:
    """Build breadcrumb segments for a slash-separated path.

    Each entry is {"name": "src", "path": "src"} so the template can build
    `/{owner}/{name}/tree/{ref}/{path}` links incrementally.
    """
    crumbs: list[dict[str, str]] = []
    if not path:
        return crumbs
    accumulated = ""
    for segment in path.split("/"):
        if not segment:
            continue
        accumulated = f"{accumulated}/{segment}" if accumulated else segment
        crumbs.append({"name": segment, "path": accumulated})
    return crumbs


def build_router(settings: Settings) -> APIRouter:
    router = APIRouter()

    # /static is mounted via the parent app; here we register only data routes.

    @router.get("/", include_in_schema=False)
    def root(request: Request) -> HTMLResponse:
        return _render(
            request,
            settings,
            "dashboard.html",
            owners=_list_owners(settings),
        )

    @router.get("/highlight.css", include_in_schema=False)
    def pygments_css() -> Response:
        # Registered before `/{owner}` so the catch-all doesn't swallow it
        # (FastAPI matches in declaration order). Single shared stylesheet for
        # syntax-highlighted blob views; cached cheaply by the browser.
        return Response(content=code.pygments_stylesheet(), media_type="text/css")

    @router.get("/{owner}", include_in_schema=False)
    def owner_page(request: Request, owner: str) -> HTMLResponse:
        owner_dir = settings.data_dir / "repos" / owner
        if not owner_dir.is_dir():
            raise HTTPException(status_code=404, detail="owner not found")
        return _render(
            request,
            settings,
            "owner.html",
            owner=owner,
            repos=_list_repos(settings, owner),
        )

    @router.get("/{owner}/{name}", include_in_schema=False)
    def repo_page(request: Request, owner: str, name: str) -> HTMLResponse:
        bare = _open_repo(settings, owner, name)
        all_issues = list_issues(bare)
        open_count = sum(1 for i in all_issues if i.state is IssueState.OPEN)
        default_branch = code.head_ref_name(bare)

        # Repo overview shows the file tree at HEAD plus a rendered README
        # (when one exists). With no commits yet, both are absent.
        head_commit = code.resolve_ref(bare, "HEAD") if default_branch else None
        entries: list[code.TreeEntry] = []
        readme_html: str | None = None
        head_short_sha: str | None = None
        if head_commit is not None:
            entries = code.list_tree_entries(head_commit.tree)
            readme_blob = code.find_readme(head_commit.tree)
            if readme_blob is not None:
                raw = readme_blob.data_stream.read()
                try:
                    text = raw.decode()
                except UnicodeDecodeError:
                    text = ""
                if readme_blob.name.lower().endswith((".md", ".markdown")):
                    readme_html = code.render_markdown(text)
                else:
                    readme_html = f"<pre>{html_escape(text)}</pre>"
            head_short_sha = head_commit.hexsha[:7]

        return _render(
            request,
            settings,
            "repo.html",
            owner=owner,
            name=name,
            description=None,
            open_issue_count=open_count,
            total_issue_count=len(all_issues),
            default_branch=default_branch,
            entries=entries,
            readme_html=readme_html,
            head_short_sha=head_short_sha,
            ref=default_branch or "HEAD",
            crumb_segments=[],
            path="",
        )

    @router.get("/{owner}/{name}/tree/{ref}", include_in_schema=False)
    @router.get("/{owner}/{name}/tree/{ref}/{path:path}", include_in_schema=False)
    def tree_page(
        request: Request, owner: str, name: str, ref: str, path: str = ""
    ) -> HTMLResponse:
        bare = _open_repo(settings, owner, name)
        commit = code.resolve_ref(bare, ref)
        if commit is None:
            # Empty repo (no commits at all) — render the empty overview
            # rather than a hard 404, so users land somewhere useful.
            if code.is_empty_repo(bare):
                return _render(
                    request,
                    settings,
                    "empty_repo.html",
                    owner=owner,
                    name=name,
                    section="tree",
                )
            raise HTTPException(status_code=404, detail="ref not found")
        node = code.walk_tree_at_path(commit, path)
        if node is None:
            raise HTTPException(status_code=404, detail="path not found")
        # Hitting /tree/ on a blob path is a 404; gh.com mirrors this.
        if node.type != "tree":
            raise HTTPException(status_code=404, detail="not a tree")
        return _render(
            request,
            settings,
            "tree.html",
            owner=owner,
            name=name,
            ref=ref,
            path=path,
            entries=code.list_tree_entries(node),
            crumb_segments=_path_crumbs(path),
        )

    @router.get("/{owner}/{name}/blob/{ref}/{path:path}", include_in_schema=False)
    def blob_page(request: Request, owner: str, name: str, ref: str, path: str) -> HTMLResponse:
        bare = _open_repo(settings, owner, name)
        commit = code.resolve_ref(bare, ref)
        if commit is None:
            raise HTTPException(status_code=404, detail="ref not found")
        node = code.walk_tree_at_path(commit, path)
        if node is None or node.type != "blob":
            raise HTTPException(status_code=404, detail="blob not found")
        rendered = code.render_blob(node)
        return _render(
            request,
            settings,
            "blob.html",
            owner=owner,
            name=name,
            ref=ref,
            path=path,
            rendered=rendered,
            crumb_segments=_path_crumbs(path),
        )

    @router.get("/{owner}/{name}/commits/{ref}", include_in_schema=False)
    def commits_page(request: Request, owner: str, name: str, ref: str) -> HTMLResponse:
        bare = _open_repo(settings, owner, name)
        commit = code.resolve_ref(bare, ref)
        if commit is None:
            if code.is_empty_repo(bare):
                return _render(
                    request,
                    settings,
                    "empty_repo.html",
                    owner=owner,
                    name=name,
                    section="commits",
                )
            raise HTTPException(status_code=404, detail="ref not found")
        return _render(
            request,
            settings,
            "commits.html",
            owner=owner,
            name=name,
            ref=ref,
            commits=code.list_commits(commit, max_count=100),
        )

    @router.get("/{owner}/{name}/commit/{sha}", include_in_schema=False)
    def commit_page(request: Request, owner: str, name: str, sha: str) -> HTMLResponse:
        bare = _open_repo(settings, owner, name)
        commit = code.resolve_ref(bare, sha)
        if commit is None:
            raise HTTPException(status_code=404, detail="commit not found")
        return _render(
            request,
            settings,
            "commit.html",
            owner=owner,
            name=name,
            detail=code.commit_detail(commit),
        )

    @router.get("/{owner}/{name}/branches", include_in_schema=False)
    def branches_page(request: Request, owner: str, name: str) -> HTMLResponse:
        bare = _open_repo(settings, owner, name)
        return _render(
            request,
            settings,
            "branches.html",
            owner=owner,
            name=name,
            branches=code.list_branches(bare),
            tags=code.list_tags(bare),
            default_branch=code.head_ref_name(bare),
        )

    @router.get("/{owner}/{name}/blame/{ref}/{path:path}", include_in_schema=False)
    def blame_page(request: Request, owner: str, name: str, ref: str, path: str) -> HTMLResponse:
        bare = _open_repo(settings, owner, name)
        lines = code.blame_blob(bare, ref, path)
        if lines is None:
            raise HTTPException(status_code=404, detail="blob not found")
        return _render(
            request,
            settings,
            "blame.html",
            owner=owner,
            name=name,
            ref=ref,
            path=path,
            lines=lines,
            crumb_segments=_path_crumbs(path),
        )

    @router.get("/{owner}/{name}/issues", include_in_schema=False)
    def issues_page(request: Request, owner: str, name: str, state: str = "open") -> HTMLResponse:
        bare = _open_repo(settings, owner, name)
        all_issues = list_issues(bare)
        open_count = sum(1 for i in all_issues if i.state is IssueState.OPEN)
        closed_count = len(all_issues) - open_count
        if state == "open":
            shown = [i for i in all_issues if i.state is IssueState.OPEN]
        elif state == "closed":
            shown = [i for i in all_issues if i.state is IssueState.CLOSED]
        else:
            state = "all"
            shown = list(all_issues)
        # Newest first matches gh's default and feels right for issue lists.
        shown.sort(key=lambda i: i.updated_at, reverse=True)
        return _render(
            request,
            settings,
            "issues.html",
            owner=owner,
            name=name,
            issues=shown,
            state=state,
            open_count=open_count,
            closed_count=closed_count,
            total_count=len(all_issues),
        )

    @router.get("/{owner}/{name}/issues/{number}", include_in_schema=False)
    def issue_page(request: Request, owner: str, name: str, number: int) -> HTMLResponse:
        bare = _open_repo(settings, owner, name)
        issue = get_issue(bare, number)
        if issue is None:
            raise HTTPException(status_code=404, detail="issue not found")
        comments = list_comments(bare, number)
        return _render(
            request,
            settings,
            "issue.html",
            owner=owner,
            name=name,
            issue=issue,
            comments=comments,
        )

    @router.post("/{owner}/{name}/issues/{number}/comments", include_in_schema=False)
    def add_comment_action(
        owner: str, name: str, number: int, body: str = Form(...)
    ) -> RedirectResponse:
        bare = _open_repo(settings, owner, name)
        if body.strip():
            # Empty bodies are silently ignored — the form's required attribute
            # already covers the common case; this handles paste-and-trim.
            if add_comment(bare, number=number, body=body, author=settings.viewer_login) is None:
                raise HTTPException(status_code=404, detail="issue not found")
        # POST/redirect/GET so a refresh doesn't re-submit. 303 ensures the
        # follow-up request is a GET regardless of the original method.
        return RedirectResponse(url=f"/{owner}/{name}/issues/{number}", status_code=303)

    @router.post("/{owner}/{name}/issues/{number}/close", include_in_schema=False)
    def close_action(owner: str, name: str, number: int) -> RedirectResponse:
        bare = _open_repo(settings, owner, name)
        if close_issue(bare, number=number, actor=settings.viewer_login) is None:
            raise HTTPException(status_code=404, detail="issue not found")
        return RedirectResponse(url=f"/{owner}/{name}/issues/{number}", status_code=303)

    @router.post("/{owner}/{name}/issues/{number}/reopen", include_in_schema=False)
    def reopen_action(owner: str, name: str, number: int) -> RedirectResponse:
        bare = _open_repo(settings, owner, name)
        if reopen_issue(bare, number=number, actor=settings.viewer_login) is None:
            raise HTTPException(status_code=404, detail="issue not found")
        return RedirectResponse(url=f"/{owner}/{name}/issues/{number}", status_code=303)

    return router


def mount_static(app) -> None:  # type: ignore[no-untyped-def]
    """Attach /static/* to a FastAPI app. Called once from create_app."""
    app.mount("/static", StaticFiles(directory=str(_WEB_DIR / "static")), name="static")
