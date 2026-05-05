# ABOUTME: HTML routes for the web UI. Read-only views over data/projects and side refs.
# ABOUTME: Lives in its own FastAPI app (gitcabin.web.app) — separate process from gh's API.

from __future__ import annotations

import mimetypes
from html import escape as html_escape
from pathlib import Path
from posixpath import basename as posix_basename

from fastapi import APIRouter, Form, HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse, Response
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

from gitcabin.config import Settings
from gitcabin.storage.issues import (
    IssueState,
    add_comment,
    close_issue,
    get_issue,
    list_comments,
    list_issues,
    reopen_issue,
)
from gitcabin.storage import layout
from gitcabin.storage.repo import BareRepo
from gitcabin.web import code
from gitcabin.web.assets import AssetResolver
from gitcabin.web.format import (
    file_icon,
    pretty_date,
    ref_label,
    relative_time,
    short_sha,
)

_WEB_DIR = Path(__file__).parent
_DIST_DIR = _WEB_DIR / "static" / "dist"
_templates = Jinja2Templates(directory=str(_WEB_DIR / "templates"))
# Templates write `{{ asset('main.css') }}` and get back `/static/dist/main.<hash>.css`.
# Reading the manifest happens on every call (the file is tiny and rebuilds while
# the server runs pick up new hashes without a restart).
_templates.env.globals["asset"] = AssetResolver(dist_dir=_DIST_DIR)


# Filters for git-metadata polish — see gitcabin.web.format.
_templates.env.filters["relative_time"] = relative_time
_templates.env.filters["short_sha"] = short_sha
_templates.env.filters["file_icon"] = file_icon
_templates.env.filters["ref_label"] = ref_label
_templates.env.filters["pretty_date"] = pretty_date


def _repo_ctx(bare: BareRepo, issues: list | None = None) -> dict[str, object]:
    """Repo-level context shared by every page that renders _repo_header.html.

    Currently the issue counts so the Issues tab badge stays the same across
    Code / Issues / Commits / Branches / blob / blame / commit / single-issue
    pages of the same repo. Spread into _render kwargs by every repo-page
    handler. Add more repo-wide facts here as they become widely useful.

    Pages that already loaded the issues list can pass it in to avoid a
    second list_issues() walk.
    """
    if issues is None:
        issues = list_issues(bare)
    return {
        "total_issue_count": len(issues),
        "open_issue_count": sum(i.state is IssueState.OPEN for i in issues),
    }


def _render(request: Request, settings: Settings, template: str, **ctx: object) -> HTMLResponse:
    """Render a template with the always-needed context (request, viewer).

    `Cache-Control: private, max-age=10` lets htmx-ext-preload's prefetched
    response actually be reused on the subsequent click — without any
    Cache-Control header the browser won't keep the prefetched body in cache,
    and the click triggers a fresh network round-trip. 10 seconds is long
    enough to bridge a hover → click and short enough that mutations the user
    just made (close issue, post comment) don't render as stale on the next
    page view. `private` keeps shared caches (proxies) from holding the
    response, since gitcabin doesn't yet have a per-user identity model.
    """
    response = _templates.TemplateResponse(
        request,
        template,
        {"viewer_login": settings.viewer_login, **ctx},
    )
    response.headers["Cache-Control"] = "private, max-age=10"
    return response


def _list_owners(settings: Settings) -> list[dict[str, object]]:
    """Walk data_dir/projects/ and return one entry per project directory."""
    projects_root = layout.projects_dir(settings.data_dir)
    if not projects_root.is_dir():
        return []
    owners: list[dict[str, object]] = []
    for project_dir in sorted(projects_root.iterdir()):
        if not project_dir.is_dir():
            continue
        repo_count = sum(
            1
            for entry in project_dir.iterdir()
            if entry.name.endswith(".git") and BareRepo.open(entry) is not None
        )
        owners.append({"login": project_dir.name, "repo_count": repo_count})
    return owners


def _list_repos(settings: Settings, owner: str) -> list[dict[str, object]]:
    """List repos under data_dir/projects/<project>/, sorted by pushed_at desc.

    Each entry carries an optional `upstream` dict ({"owner", "name"}) when
    the repo has a sync config — surfaced on the card so the user can see
    at a glance which repos mirror a GitHub upstream.
    """
    project_dir = layout.projects_dir(settings.data_dir) / owner
    if not project_dir.is_dir():
        return []
    from gitcabin.sync.config import read_config as read_sync_config

    out: list[dict[str, object]] = []
    for entry in sorted(project_dir.iterdir()):
        if not entry.name.endswith(".git"):
            continue
        bare = BareRepo.open(entry)
        if bare is None:
            continue
        sync = read_sync_config(bare)
        upstream = (
            {"owner": sync.gh_owner, "name": sync.gh_name} if sync is not None else None
        )
        out.append(
            {
                "name": entry.name[: -len(".git")],
                "description": None,
                "pushed_at": _repo_pushed_at(bare),
                "upstream": upstream,
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


def _open_repo(settings: Settings, project: str, name: str) -> BareRepo:
    """Resolve a (project, name) pair to a BareRepo or 404."""
    bare = layout.open_repo(settings.data_dir, project, name)
    if bare is None:
        raise HTTPException(status_code=404, detail="repo not found")
    return bare


def _split_ref_path(bare: BareRepo, rest: str) -> tuple[str, str] | None:
    """Disambiguate `<ref>/<path>` where the ref itself may contain slashes.

    Tree / blob / blame / raw / download URLs all use `<ref>/<path>` after
    the verb. Branches like `feature/widget-update` or `release/2.0` are
    common, so we can't pin the ref to a single segment. Mirror GitHub's
    parsing: walk the rest from longest to shortest prefix, returning the
    first prefix that resolves to a git ref. Empty `rest` returns None.
    """
    if not rest:
        return None
    segments = rest.split("/")
    for i in range(len(segments), 0, -1):
        candidate = "/".join(segments[:i])
        if code.resolve_ref(bare, candidate) is not None:
            return candidate, "/".join(segments[i:])
    return None


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

    # ---- view-body helpers (parameterized on project) ------------------- #
    # Each page is a `_<view>(request, project, name, ...)` body called by
    # the route handlers below. Repos always live at
    # `data/projects/<project>/<name>.git`; the URL shape mirrors that —
    # `/{owner}/{name}/...`.

    def _render_repo_overview(
        request: Request, project: str, name: str
    ) -> HTMLResponse:
        bare = _open_repo(settings, project, name)
        default_branch = code.head_ref_name(bare)
        head_commit = code.resolve_ref(bare, "HEAD") if default_branch else None
        entries: list[code.TreeEntry] = []
        readme_html: str | None = None
        head_short_sha: str | None = None
        if head_commit is not None:
            entries = code.enrich_with_last_commits(
                head_commit, code.list_tree_entries(head_commit.tree)
            )
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
            owner=project,
            name=name,
            description=None,
            default_branch=default_branch,
            entries=entries,
            readme_html=readme_html,
            head_short_sha=head_short_sha,
            ref=default_branch or "HEAD",
            crumb_segments=[],
            path="",
            branches=code.list_branches(bare),
            tags=code.list_tags(bare),
            **_repo_ctx(bare),
        )

    def _render_tree(
        request: Request, project: str, name: str, ref: str, path: str = ""
    ) -> HTMLResponse:
        bare = _open_repo(settings, project, name)
        commit = code.resolve_ref(bare, ref)
        if commit is None:
            if code.is_empty_repo(bare):
                return _render(
                    request,
                    settings,
                    "empty_repo.html",
                    owner=project,
                    name=name,
                    section="tree",
                    **_repo_ctx(bare),
                )
            raise HTTPException(status_code=404, detail="ref not found")
        node = code.walk_tree_at_path(commit, path)
        if node is None:
            raise HTTPException(status_code=404, detail="path not found")
        if node.type != "tree":
            raise HTTPException(status_code=404, detail="not a tree")
        return _render(
            request,
            settings,
            "tree.html",
            owner=project,
            name=name,
            ref=ref,
            path=path,
            entries=code.enrich_with_last_commits(
                commit, code.list_tree_entries(node), prefix=path
            ),
            crumb_segments=_path_crumbs(path),
            branches=code.list_branches(bare),
            tags=code.list_tags(bare),
            default_branch=code.head_ref_name(bare),
            **_repo_ctx(bare),
        )

    def _render_blob(
        request: Request, project: str, name: str, ref: str, path: str
    ) -> HTMLResponse:
        bare = _open_repo(settings, project, name)
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
            owner=project,
            name=name,
            ref=ref,
            path=path,
            rendered=rendered,
            crumb_segments=_path_crumbs(path),
            branches=code.list_branches(bare),
            tags=code.list_tags(bare),
            default_branch=code.head_ref_name(bare),
            **_repo_ctx(bare),
        )

    def _serve_blob_bytes(
        project: str, name: str, ref: str, path: str, *, attachment: bool
    ) -> Response:
        """Serve a blob's raw bytes — inline (raw view) or as a download.

        For raw, text-shaped files are served as `text/plain; charset=utf-8`
        so browsers display them inline rather than offering to download. For
        downloads, force `application/octet-stream` regardless of file type
        and add `Content-Disposition: attachment` with the basename.
        """
        bare = _open_repo(settings, project, name)
        commit = code.resolve_ref(bare, ref)
        if commit is None:
            raise HTTPException(status_code=404, detail="ref not found")
        node = code.walk_tree_at_path(commit, path)
        if node is None or node.type != "blob":
            raise HTTPException(status_code=404, detail="blob not found")
        data: bytes = node.data_stream.read()
        filename = posix_basename(path) or name
        if attachment:
            media_type = "application/octet-stream"
            disposition = f'attachment; filename="{filename}"'
        else:
            guessed, _ = mimetypes.guess_type(filename)
            if guessed is None or guessed.startswith("text/"):
                media_type = "text/plain; charset=utf-8"
            else:
                media_type = guessed
            disposition = f'inline; filename="{filename}"'
        return Response(
            content=data,
            media_type=media_type,
            headers={"Content-Disposition": disposition},
        )

    def _render_commits(
        request: Request, project: str, name: str, ref: str
    ) -> HTMLResponse:
        bare = _open_repo(settings, project, name)
        commit = code.resolve_ref(bare, ref)
        if commit is None:
            if code.is_empty_repo(bare):
                return _render(
                    request,
                    settings,
                    "empty_repo.html",
                    owner=project,
                    name=name,
                    section="commits",
                    **_repo_ctx(bare),
                )
            raise HTTPException(status_code=404, detail="ref not found")
        commits = code.list_commits(commit, max_count=100)
        # Group by authored date (ISO prefix), preserving the
        # newest-first order list_commits already gives us.
        commit_groups: list[tuple[str, list[code.CommitSummary]]] = []
        for c in commits:
            day = c.authored_at[:10]
            if commit_groups and commit_groups[-1][0] == day:
                commit_groups[-1][1].append(c)
            else:
                commit_groups.append((day, [c]))
        return _render(
            request,
            settings,
            "commits.html",
            owner=project,
            name=name,
            ref=ref,
            commit_groups=commit_groups,
            branches=code.list_branches(bare),
            tags=code.list_tags(bare),
            default_branch=code.head_ref_name(bare),
            **_repo_ctx(bare),
        )

    def _render_commit(
        request: Request, project: str, name: str, sha: str
    ) -> HTMLResponse:
        bare = _open_repo(settings, project, name)
        commit = code.resolve_ref(bare, sha)
        if commit is None:
            raise HTTPException(status_code=404, detail="commit not found")
        return _render(
            request,
            settings,
            "commit.html",
            owner=project,
            name=name,
            detail=code.commit_detail(commit),
            **_repo_ctx(bare),
        )

    def _render_branches(
        request: Request, project: str, name: str
    ) -> HTMLResponse:
        bare = _open_repo(settings, project, name)
        return _render(
            request,
            settings,
            "branches.html",
            owner=project,
            name=name,
            branches=code.list_branches(bare),
            tags=code.list_tags(bare),
            default_branch=code.head_ref_name(bare),
            **_repo_ctx(bare),
        )

    def _render_blame(
        request: Request, project: str, name: str, ref: str, path: str
    ) -> HTMLResponse:
        bare = _open_repo(settings, project, name)
        lines = code.blame_blob(bare, ref, path)
        if lines is None:
            raise HTTPException(status_code=404, detail="blob not found")
        return _render(
            request,
            settings,
            "blame.html",
            owner=project,
            name=name,
            ref=ref,
            path=path,
            lines=lines,
            crumb_segments=_path_crumbs(path),
            branches=code.list_branches(bare),
            tags=code.list_tags(bare),
            default_branch=code.head_ref_name(bare),
            **_repo_ctx(bare),
        )

    def _render_issues(
        request: Request, project: str, name: str, state: str = "open"
    ) -> HTMLResponse:
        bare = _open_repo(settings, project, name)
        all_issues = list_issues(bare)
        open_count = sum(i.state is IssueState.OPEN for i in all_issues)
        closed_count = len(all_issues) - open_count
        if state == "open":
            shown = [i for i in all_issues if i.state is IssueState.OPEN]
        elif state == "closed":
            shown = [i for i in all_issues if i.state is IssueState.CLOSED]
        else:
            state = "all"
            shown = list(all_issues)
        shown.sort(key=lambda i: i.updated_at, reverse=True)
        return _render(
            request,
            settings,
            "issues.html",
            owner=project,
            name=name,
            issues=shown,
            state=state,
            open_count=open_count,
            closed_count=closed_count,
            total_count=len(all_issues),
            **_repo_ctx(bare, all_issues),
        )

    def _render_issue(
        request: Request, project: str, name: str, number: int
    ) -> HTMLResponse:
        bare = _open_repo(settings, project, name)
        issue = get_issue(bare, number)
        if issue is None:
            raise HTTPException(status_code=404, detail="issue not found")
        comments = list_comments(bare, number)
        return _render(
            request,
            settings,
            "issue.html",
            owner=project,
            name=name,
            issue=issue,
            comments=comments,
            **_repo_ctx(bare),
        )

    # ---- top-level routes ----------------------------------------------- #

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
        # Registered before `/{owner}` so the catch-all doesn't swallow it.
        return Response(content=code.pygments_stylesheet(), media_type="text/css")

    @router.get("/{owner}", include_in_schema=False)
    def owner_page(request: Request, owner: str) -> HTMLResponse:
        if not (layout.projects_dir(settings.data_dir) / owner).is_dir():
            raise HTTPException(status_code=404, detail="not found")
        return _render(
            request,
            settings,
            "owner.html",
            owner=owner,
            repos=_list_repos(settings, owner),
        )

    @router.get("/{owner}/{name}", include_in_schema=False)
    def project_overview(
        request: Request, owner: str, name: str
    ) -> HTMLResponse:
        return _render_repo_overview(request, project=owner, name=name)

    def _resolve_ref_url(
        owner: str, name: str, rest: str, *, require_path: bool
    ) -> tuple[BareRepo, str, str]:
        """Open the repo and split `<ref>/<path>` from the URL tail.

        404s if the ref doesn't resolve, or if `require_path` is set and
        the URL didn't include a path after the ref (blob / blame / raw /
        download all need a path; tree doesn't).
        """
        bare = _open_repo(settings, owner, name)
        pair = _split_ref_path(bare, rest)
        if pair is None:
            raise HTTPException(status_code=404, detail="ref not found")
        ref, path = pair
        if require_path and not path:
            raise HTTPException(status_code=404, detail="path required")
        return bare, ref, path

    @router.get("/{owner}/{name}/tree/{rest:path}", include_in_schema=False)
    def project_tree(
        request: Request, owner: str, name: str, rest: str
    ) -> HTMLResponse:
        _, ref, path = _resolve_ref_url(owner, name, rest, require_path=False)
        return _render_tree(request, project=owner, name=name, ref=ref, path=path)

    @router.get("/{owner}/{name}/blob/{rest:path}", include_in_schema=False)
    def project_blob(
        request: Request, owner: str, name: str, rest: str
    ) -> HTMLResponse:
        _, ref, path = _resolve_ref_url(owner, name, rest, require_path=True)
        return _render_blob(request, project=owner, name=name, ref=ref, path=path)

    @router.get("/{owner}/{name}/raw/{rest:path}", include_in_schema=False)
    def project_raw(owner: str, name: str, rest: str) -> Response:
        _, ref, path = _resolve_ref_url(owner, name, rest, require_path=True)
        return _serve_blob_bytes(
            project=owner, name=name, ref=ref, path=path, attachment=False
        )

    @router.get("/{owner}/{name}/download/{rest:path}", include_in_schema=False)
    def project_download(owner: str, name: str, rest: str) -> Response:
        _, ref, path = _resolve_ref_url(owner, name, rest, require_path=True)
        return _serve_blob_bytes(
            project=owner, name=name, ref=ref, path=path, attachment=True
        )

    @router.get("/{owner}/{name}/commits/{ref:path}", include_in_schema=False)
    def project_commits(
        request: Request, owner: str, name: str, ref: str
    ) -> HTMLResponse:
        return _render_commits(request, project=owner, name=name, ref=ref)

    @router.get("/{owner}/{name}/commit/{sha}", include_in_schema=False)
    def project_commit(
        request: Request, owner: str, name: str, sha: str
    ) -> HTMLResponse:
        return _render_commit(request, project=owner, name=name, sha=sha)

    @router.get("/{owner}/{name}/branches", include_in_schema=False)
    def project_branches(request: Request, owner: str, name: str) -> HTMLResponse:
        return _render_branches(request, project=owner, name=name)

    @router.get("/{owner}/{name}/blame/{rest:path}", include_in_schema=False)
    def project_blame(
        request: Request, owner: str, name: str, rest: str
    ) -> HTMLResponse:
        _, ref, path = _resolve_ref_url(owner, name, rest, require_path=True)
        return _render_blame(request, project=owner, name=name, ref=ref, path=path)

    @router.get("/{owner}/{name}/issues", include_in_schema=False)
    def project_issues(
        request: Request, owner: str, name: str, state: str = "open"
    ) -> HTMLResponse:
        return _render_issues(request, project=owner, name=name, state=state)

    @router.get("/{owner}/{name}/issues/{number}", include_in_schema=False)
    def project_issue(
        request: Request, owner: str, name: str, number: int
    ) -> HTMLResponse:
        return _render_issue(request, project=owner, name=name, number=number)

    # ---- POST actions --------------------------------------------------- #

    def _post_action_response(
        request: Request, project: str, name: str, number: int
    ) -> Response:
        """Return the post-mutation view of an issue.

        For htmx-driven submits (`HX-Request: true`) re-render the issue
        page directly so the client can swap `<main>` without a redirect
        round-trip — the GET that a 303 would trigger can hit the browser
        cache (`_render` sets `private, max-age=10`) and serve a stale
        pre-mutation page, which is what made the close/reopen state appear
        to require a manual refresh.

        `HX-Push-Url` rewrites the address bar back to the canonical
        `/.../issues/{number}` so the URL stays clean even though the
        request went to `/close`, `/reopen`, or `/comments`.

        Non-htmx submits (curl, JS off) still get the 303 to the issue
        page — the browser-cache staleness is acceptable there since the
        round-trip already drops them on a fresh URL.
        """
        url = f"/{project}/{name}/issues/{number}"
        if request.headers.get("HX-Request") == "true":
            response = _render_issue(
                request, project=project, name=name, number=number
            )
            response.headers["HX-Push-Url"] = url
            return response
        return RedirectResponse(url=url, status_code=303)

    def _do_add_comment(
        request: Request, project: str, name: str, number: int, body: str
    ) -> Response:
        bare = _open_repo(settings, project, name)
        if body.strip():
            if (
                add_comment(bare, number=number, body=body, author=settings.viewer_login)
                is None
            ):
                raise HTTPException(status_code=404, detail="issue not found")
        return _post_action_response(request, project, name, number)

    def _do_close(
        request: Request, project: str, name: str, number: int
    ) -> Response:
        bare = _open_repo(settings, project, name)
        if close_issue(bare, number=number, actor=settings.viewer_login) is None:
            raise HTTPException(status_code=404, detail="issue not found")
        return _post_action_response(request, project, name, number)

    def _do_reopen(
        request: Request, project: str, name: str, number: int
    ) -> Response:
        bare = _open_repo(settings, project, name)
        if reopen_issue(bare, number=number, actor=settings.viewer_login) is None:
            raise HTTPException(status_code=404, detail="issue not found")
        return _post_action_response(request, project, name, number)

    @router.post(
        "/{owner}/{name}/issues/{number}/comments", include_in_schema=False
    )
    def project_add_comment(
        request: Request,
        owner: str,
        name: str,
        number: int,
        body: str = Form(...),
    ) -> Response:
        return _do_add_comment(request, owner, name, number, body)

    @router.post("/{owner}/{name}/issues/{number}/close", include_in_schema=False)
    def project_close(
        request: Request, owner: str, name: str, number: int
    ) -> Response:
        return _do_close(request, owner, name, number)

    @router.post("/{owner}/{name}/issues/{number}/reopen", include_in_schema=False)
    def project_reopen(
        request: Request, owner: str, name: str, number: int
    ) -> Response:
        return _do_reopen(request, owner, name, number)

    return router


class _ImmutableStaticFiles(StaticFiles):
    """StaticFiles that flags content-hashed bundles as immutable.

    The bundler writes hashed filenames under static/dist/ — main.<hash>.js,
    main.<hash>.css. Same content always produces the same filename, so we
    can promise the browser the response will never change. `immutable`
    tells the cache it doesn't even need to revalidate; `max-age=31536000`
    is the conventional one-year window. Files outside dist/ keep the
    default StaticFiles short-cache behavior.
    """

    async def get_response(self, path: str, scope):  # type: ignore[no-untyped-def]
        response = await super().get_response(path, scope)
        if path.startswith("dist/"):
            response.headers["Cache-Control"] = "public, max-age=31536000, immutable"
        return response


def mount_static(app) -> None:  # type: ignore[no-untyped-def]
    """Attach /static/* to a FastAPI app. Called once from create_app."""
    app.mount(
        "/static",
        _ImmutableStaticFiles(directory=str(_WEB_DIR / "static")),
        name="static",
    )
