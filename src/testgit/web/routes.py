# ABOUTME: HTML routes for the web UI. Read-only views over data/repos and side refs.
# ABOUTME: Lives in its own FastAPI app (testgit.web.app) — separate process from gh's API.

from __future__ import annotations

from pathlib import Path

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import HTMLResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

from testgit.config import Settings
from testgit.storage.issues import (
    IssueState,
    get_issue,
    list_comments,
    list_issues,
)
from testgit.storage.repo import BareRepo

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
        try:
            default_branch = bare.repo.head.reference.name
        except TypeError, ValueError:
            default_branch = None
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

    return router


def mount_static(app) -> None:  # type: ignore[no-untyped-def]
    """Attach /static/* to a FastAPI app. Called once from create_app."""
    app.mount("/static", StaticFiles(directory=str(_WEB_DIR / "static")), name="static")
