# ABOUTME: One-shot seed script: produces a diverse set of demo repos under ./data/projects.
# ABOUTME: Run with `uv run python scripts/seed_demo.py [--reset]` from the project root.

from __future__ import annotations

import argparse
import os
import shutil
import subprocess
from collections.abc import Iterable
from dataclasses import dataclass, field
from pathlib import Path

from gitcabin.storage.issues import add_comment, close_issue, create_issue
from gitcabin.storage.repo import BareRepo

ROOT = Path(__file__).resolve().parent.parent
DATA_DIR = ROOT / "data"
PROJECTS_DIR = DATA_DIR / "projects"


# ---- low-level helpers -------------------------------------------------- #


def _hash_blob(bare: BareRepo, content: bytes) -> str:
    """Stream a blob into the object database; bytes-safe (binary OK)."""
    result = subprocess.run(
        ["git", "hash-object", "-w", "--stdin"],
        cwd=bare.path,
        input=content,
        capture_output=True,
        check=True,
    )
    return result.stdout.decode().strip()


def _build_tree(bare: BareRepo, files: dict[str, str]) -> str:
    """Recursively assemble a tree from path → text via git mktree.

    Content is taken as Python str and UTF-8 encoded at hash time so unicode
    in demo files (smart quotes, prompt glyphs) round-trips through the
    object database without forcing every constant to be ASCII-only.
    """
    own: dict[str, str] = {}
    nested: dict[str, dict[str, str]] = {}
    for path, content in files.items():
        if "/" in path:
            head, _, tail = path.partition("/")
            nested.setdefault(head, {})[tail] = content
        else:
            own[path] = content
    entries: list[str] = []
    for name, content in own.items():
        entries.append(f"100644 blob {_hash_blob(bare, content.encode())}\t{name}")
    for dirname, sub_files in nested.items():
        sub_sha = _build_tree(bare, sub_files)
        entries.append(f"040000 tree {sub_sha}\t{dirname}")
    return bare.run_git("mktree", input="\n".join(entries) + "\n").strip()


def _commit(
    bare: BareRepo,
    files: dict[str, str],
    *,
    message: str,
    author_name: str,
    author_email: str,
    parent: str | None,
    when: str,
) -> str:
    """Create a commit with explicit author/committer dates so the demo
    timeline looks realistic instead of every commit landing in the same second.
    """
    tree_sha = _build_tree(bare, files)
    args = [
        "git",
        "-c",
        f"user.name={author_name}",
        "-c",
        f"user.email={author_email}",
        "commit-tree",
        tree_sha,
        "-m",
        message,
    ]
    if parent:
        args[-2:-2] = ["-p", parent]
    env = os.environ.copy()
    env["GIT_AUTHOR_DATE"] = when
    env["GIT_COMMITTER_DATE"] = when
    result = subprocess.run(
        args, cwd=bare.path, capture_output=True, text=True, check=True, env=env
    )
    return result.stdout.strip()


def _set_ref(bare: BareRepo, ref: str, sha: str) -> None:
    bare.run_git("update-ref", ref, sha)


# ---- demo data structures ---------------------------------------------- #


@dataclass(frozen=True)
class Commit:
    files: dict[str, str]
    message: str
    when: str
    author_name: str = "Demo Author"
    author_email: str = "demo@gitcabin.local"


@dataclass(frozen=True)
class Branch:
    """A side-branch off a specific main-line commit index."""

    name: str
    fork_at: int  # 0-based index into main_commits
    commits: list[Commit]


@dataclass(frozen=True)
class Tag:
    name: str
    at: int  # 0-based index into main_commits


@dataclass(frozen=True)
class Issue:
    title: str
    body: str = ""
    author: str = "demo"
    closed: bool = False
    comments: list[tuple[str, str]] = field(default_factory=list)  # (author, body)


@dataclass(frozen=True)
class Repo:
    owner: str
    name: str
    description: str
    main_commits: list[Commit]
    branches: list[Branch] = field(default_factory=list)
    tags: list[Tag] = field(default_factory=list)
    issues: list[Issue] = field(default_factory=list)


# ---- demo content ------------------------------------------------------ #


HELLO_README = """# hello-world

A tiny welcome repo. Used to demonstrate that the dashboard renders
Markdown READMEs, syntax-highlighted source files, and the commit log.

## Usage

```python
from hello import greet
greet("world")
```

## Why this exists

This is the canonical first repo on every git host. Naming it
`hello-world` makes the dashboard look populated without having to
explain anything.
"""

HELLO_PY = '''def greet(name: str) -> str:
    """Return a friendly greeting."""
    return f"Hello, {name}!"


if __name__ == "__main__":
    print(greet("world"))
'''

HELLO_LICENSE = """MIT License

Copyright (c) 2026 gitcabin demo

Permission is hereby granted, free of charge, to any person obtaining a copy
of this software and associated documentation files (the "Software"), to deal
in the Software without restriction, including without limitation the rights
to use, copy, modify, merge, publish, distribute, sublicense, and/or sell
copies of the Software, and to permit persons to whom the Software is
furnished to do so, subject to the following conditions:

The above copyright notice and this permission notice shall be included in all
copies or substantial portions of the Software.
"""


API_README = """# api-server

A Python web service skeleton. Demonstrates a multi-file Python project
with a `src/` layout, tests, and `pyproject.toml`.

## Quickstart

```sh
uv sync
uv run uvicorn app.main:create_app --reload
```

## Endpoints

| Method | Path        | Notes                |
|--------|-------------|----------------------|
| GET    | `/health`   | liveness probe       |
| GET    | `/widgets`  | list all widgets     |
| POST   | `/widgets`  | create a new widget  |
"""

API_PYPROJECT = """[project]
name = "api-server"
version = "0.2.0"
requires-python = ">=3.13"
dependencies = ["fastapi>=0.115", "uvicorn>=0.30"]
"""

API_MAIN_PY = """from fastapi import FastAPI

from app.routes import widgets


def create_app() -> FastAPI:
    app = FastAPI(title="api-server")
    app.include_router(widgets.router)

    @app.get("/health")
    def health() -> dict[str, str]:
        return {"status": "ok"}

    return app
"""

API_WIDGETS_PY = """from fastapi import APIRouter
from pydantic import BaseModel

router = APIRouter(prefix="/widgets")


class Widget(BaseModel):
    id: str
    name: str
    color: str


_FAKE_DB: list[Widget] = []


@router.get("/")
def list_widgets() -> list[Widget]:
    return _FAKE_DB


@router.post("/")
def create_widget(w: Widget) -> Widget:
    _FAKE_DB.append(w)
    return w
"""

API_TEST = """from fastapi.testclient import TestClient

from app.main import create_app


def test_health() -> None:
    client = TestClient(create_app())
    assert client.get("/health").json() == {"status": "ok"}
"""


WIDGET_README = """# widget-store

A tiny TypeScript SPA skeleton. Used here to show off TypeScript and JSON
syntax highlighting alongside Python repos.
"""

WIDGET_PKG = """{
  \"name\": \"widget-store\",
  \"version\": \"0.1.0\",
  \"type\": \"module\",
  \"scripts\": {
    \"build\": \"tsc\",
    \"start\": \"node dist/index.js\"
  },
  \"dependencies\": {
    \"hono\": \"^4.6.0\"
  }
}
"""

WIDGET_INDEX_TS = """import { Hono } from \"hono\";

interface Widget {
  id: string;
  name: string;
  color: string;
}

const app = new Hono();
const widgets: Widget[] = [];

app.get(\"/widgets\", (c) => c.json(widgets));
app.post(\"/widgets\", async (c) => {
  const body = await c.req.json<Widget>();
  widgets.push(body);
  return c.json(body, 201);
});

export default app;
"""

WIDGET_TSCONFIG = """{
  \"compilerOptions\": {
    \"target\": \"ES2022\",
    \"module\": \"ESNext\",
    \"strict\": true,
    \"outDir\": \"dist\"
  },
  \"include\": [\"src/**/*\"]
}
"""


DOTFILES_README = """# dotfiles

My personal config — vim, zsh, git, tmux. Bring-your-own-stow.

## Layout

```
.zshrc          # interactive shell
.vimrc          # editor
.gitconfig      # git identity + aliases
.tmux.conf      # multiplexer
```
"""

DOTFILES_ZSHRC = """# .zshrc — interactive zsh

export EDITOR=vim
export PAGER=less

setopt SHARE_HISTORY
setopt HIST_IGNORE_DUPS

alias ll='ls -lah'
alias gs='git status'
alias gd='git diff'

# Keep prompt simple.
PS1='%F{cyan}%~%f %F{magenta}❯%f '
"""

DOTFILES_VIMRC = """\" Minimal vim config.
syntax on
set number
set relativenumber
set tabstop=4
set shiftwidth=4
set expandtab
set incsearch
set ignorecase smartcase
"""

DOTFILES_GITCONFIG = """[user]
\tname = Demo User
\temail = demo@gitcabin.local
[alias]
\tst = status
\tco = checkout
\tcm = commit -m
\tlg = log --oneline --graph --decorate
[init]
\tdefaultBranch = main
"""


NOTES_README = """# notes

A markdown-only journal. Mostly here to test that long Markdown documents
render well, including headings, lists, code, and tables.
"""

NOTES_TASKS = """# Tasks

## In progress

- [ ] Wire up syntax highlighting for diff views
- [ ] Make the issue list filterable by author
- [ ] Sketch GitHub sync

## Done

- [x] Switch to Tailwind CDN
- [x] Add custom logo
- [x] Tear out cgit container
"""

NOTES_IDEAS = """# Ideas

> A scratchpad for things that might or might not happen.

1. **Search** — full-text across issue titles and bodies, plus a query
   parser so `is:open author:david` from the URL bar Just Works.
2. **Pull request equivalents** — refs/prs/local/<n> mirroring the issue
   side-ref pattern, but with `head_ref` and `base_ref` in the document.
3. **Webhooks-out** — fan out events on every write so a future GitHub-sync
   reactor can subscribe.
"""


RUNBOOK_README = """# runbook

On-call documentation for the gitcabin demo. One file per scenario.

See:
- [`incident.md`](./incident.md) — when the API container crash-loops
- [`recovery.md`](./recovery.md) — restoring the bare-repo volume
"""

RUNBOOK_INCIDENT = """# Incident: API container crash-loop

## Symptoms

`docker compose ps` shows `gitcabin` exiting with status 1 every few seconds.
The dashboard at port 8080 still works (different process); only gh's API
calls fail.

## Diagnosis

1. `docker compose logs gitcabin | tail -50` — almost always reveals a
   missing dependency or a broken import after a recent edit.
2. If `granian` itself fails to bind, the host port is already taken — check
   for a stray `docker compose down` that didn't actually clean up.

## Mitigation

```sh
docker compose down gitcabin
docker compose up -d --build gitcabin
```

## Root cause

Usually a forgotten `from .x import y` after a refactor. Run pytest before
pushing.
"""

RUNBOOK_RECOVERY = """# Recovery: restoring data/projects

The bare repos under `data/projects/` are the ground truth for everything —
code, issues (`refs/issues/local/*`), counters (`refs/meta/counters`).
Recovery means recovering that directory.

## Backup

```sh
tar -czf gitcabin-data-$(date +%Y%m%d).tar.gz data/
```

## Restore

```sh
docker compose down
tar -xzf gitcabin-data-20260427.tar.gz
docker compose up -d
```

There is no DB to recover separately. The git repos are the DB.
"""


GHOST_README = """# abandoned

Yep, that's it. One commit. No issues. Mostly here to verify the
dashboard's empty-ish-repo state still looks reasonable.
"""


# ---- the catalog -------------------------------------------------------- #


def _all_repos() -> list[Repo]:
    return [
        Repo(
            owner="octocat",
            name="hello-world",
            description="A tiny welcome project.",
            main_commits=[
                Commit(
                    files={"README.md": HELLO_README, "LICENSE": HELLO_LICENSE},
                    message="Initial commit",
                    when="2026-01-04T10:00:00",
                ),
                Commit(
                    files={
                        "README.md": HELLO_README,
                        "LICENSE": HELLO_LICENSE,
                        "hello.py": HELLO_PY,
                    },
                    message="Add hello.py",
                    when="2026-01-08T14:22:00",
                ),
            ],
            tags=[Tag(name="v0.1.0", at=1)],
            issues=[
                Issue(
                    title="Greeting should support exclamation flag",
                    body="`greet('world')` always ends with `!`. Add a `loud=False` arg.",
                    author="alice",
                    comments=[
                        ("david", "Good catch. Will pick this up after v0.1.0."),
                        ("alice", "Thanks!"),
                    ],
                ),
                Issue(
                    title="Document MIT license in the README",
                    body="The repo ships a LICENSE file but the README doesn't mention it.",
                    author="bob",
                    closed=True,
                    comments=[("david", "Fixed in 1.0.0.")],
                ),
            ],
        ),
        Repo(
            owner="acme",
            name="api-server",
            description="A Python web service skeleton.",
            main_commits=[
                Commit(
                    files={"README.md": API_README, "pyproject.toml": API_PYPROJECT},
                    message="Bootstrap project",
                    when="2026-02-01T09:00:00",
                    author_name="acme-bot",
                    author_email="bot@acme.local",
                ),
                Commit(
                    files={
                        "README.md": API_README,
                        "pyproject.toml": API_PYPROJECT,
                        "src/app/__init__.py": "",
                        "src/app/main.py": API_MAIN_PY,
                    },
                    message="Add FastAPI app factory",
                    when="2026-02-02T11:30:00",
                ),
                Commit(
                    files={
                        "README.md": API_README,
                        "pyproject.toml": API_PYPROJECT,
                        "src/app/__init__.py": "",
                        "src/app/main.py": API_MAIN_PY,
                        "src/app/routes/__init__.py": "",
                        "src/app/routes/widgets.py": API_WIDGETS_PY,
                    },
                    message="Add widgets router",
                    when="2026-02-04T16:45:00",
                ),
                Commit(
                    files={
                        "README.md": API_README,
                        "pyproject.toml": API_PYPROJECT,
                        "src/app/__init__.py": "",
                        "src/app/main.py": API_MAIN_PY,
                        "src/app/routes/__init__.py": "",
                        "src/app/routes/widgets.py": API_WIDGETS_PY,
                        "tests/__init__.py": "",
                        "tests/test_health.py": API_TEST,
                    },
                    message="Add health-check test",
                    when="2026-02-07T10:15:00",
                ),
            ],
            branches=[
                Branch(
                    name="feature/widget-update-endpoint",
                    fork_at=2,
                    commits=[
                        Commit(
                            files={
                                "README.md": API_README,
                                "pyproject.toml": API_PYPROJECT,
                                "src/app/__init__.py": "",
                                "src/app/main.py": API_MAIN_PY,
                                "src/app/routes/__init__.py": "",
                                "src/app/routes/widgets.py": API_WIDGETS_PY
                                + (
                                    '\n\n@router.put("/{wid}")\n'
                                    "def update_widget(wid: str, w: Widget) -> Widget:\n"
                                    "    return w\n"
                                ),
                            },
                            message="WIP: PUT /widgets/{id}",
                            when="2026-02-09T13:00:00",
                        ),
                    ],
                ),
            ],
            tags=[Tag(name="v0.2.0", at=3)],
            issues=[
                Issue(
                    title="500 on POST /widgets when body is empty",
                    body="Curl with `-d ''` returns a stack trace instead of a 422.",
                    author="alice",
                    comments=[
                        ("david", "Looks like Pydantic isn't catching the empty body."),
                    ],
                ),
                Issue(
                    title="Add a Dockerfile",
                    body=(
                        "Right now the README says `uv run uvicorn ...`; want a "
                        "containerized path too."
                    ),
                    author="bob",
                ),
                Issue(
                    title="Pin uvicorn version range",
                    body="Floating major has bitten us before.",
                    author="carol",
                    closed=True,
                    comments=[("david", "Pinned to ~0.30.")],
                ),
            ],
        ),
        Repo(
            owner="acme",
            name="widget-store",
            description="TypeScript SPA skeleton.",
            main_commits=[
                Commit(
                    files={
                        "README.md": WIDGET_README,
                        "package.json": WIDGET_PKG,
                        "tsconfig.json": WIDGET_TSCONFIG,
                    },
                    message="Bootstrap TypeScript project",
                    when="2026-03-01T08:00:00",
                ),
                Commit(
                    files={
                        "README.md": WIDGET_README,
                        "package.json": WIDGET_PKG,
                        "tsconfig.json": WIDGET_TSCONFIG,
                        "src/index.ts": WIDGET_INDEX_TS,
                    },
                    message="Add Hono router",
                    when="2026-03-03T15:00:00",
                ),
            ],
            issues=[
                Issue(
                    title="ESM import error on Node 18",
                    body=(
                        '`Cannot use import statement outside a module` — needs `"type": "module"`.'
                    ),
                    author="alice",
                    closed=True,
                    comments=[("david", "Fixed in package.json.")],
                ),
            ],
        ),
        Repo(
            owner="david",
            name="dotfiles",
            description="Personal config: zsh, vim, git, tmux.",
            main_commits=[
                Commit(
                    files={
                        "README.md": DOTFILES_README,
                        ".zshrc": DOTFILES_ZSHRC,
                    },
                    message="Add zshrc",
                    when="2025-12-01T18:00:00",
                ),
                Commit(
                    files={
                        "README.md": DOTFILES_README,
                        ".zshrc": DOTFILES_ZSHRC,
                        ".vimrc": DOTFILES_VIMRC,
                    },
                    message="Add vimrc",
                    when="2025-12-05T19:30:00",
                ),
                Commit(
                    files={
                        "README.md": DOTFILES_README,
                        ".zshrc": DOTFILES_ZSHRC,
                        ".vimrc": DOTFILES_VIMRC,
                        ".gitconfig": DOTFILES_GITCONFIG,
                    },
                    message="Add gitconfig",
                    when="2025-12-12T20:00:00",
                ),
            ],
            issues=[],
        ),
        Repo(
            owner="david",
            name="notes",
            description="Markdown-only journal.",
            main_commits=[
                Commit(
                    files={"README.md": NOTES_README, "tasks.md": NOTES_TASKS},
                    message="Start the journal",
                    when="2026-04-01T07:00:00",
                ),
                Commit(
                    files={
                        "README.md": NOTES_README,
                        "tasks.md": NOTES_TASKS,
                        "ideas.md": NOTES_IDEAS,
                    },
                    message="Add ideas",
                    when="2026-04-15T07:30:00",
                ),
            ],
            issues=[
                Issue(
                    title="Reminder: write up the sync design",
                    body="Bidirectional, GitHub-authoritative numbers, locally-numbered temp IDs.",
                    author="david",
                ),
            ],
        ),
        Repo(
            owner="acme",
            name="runbook",
            description="On-call documentation.",
            main_commits=[
                Commit(
                    files={
                        "README.md": RUNBOOK_README,
                        "incident.md": RUNBOOK_INCIDENT,
                        "recovery.md": RUNBOOK_RECOVERY,
                    },
                    message="Initial runbook",
                    when="2026-04-10T11:00:00",
                ),
            ],
        ),
        Repo(
            owner="ghost",
            name="abandoned",
            description="Single-commit, no-issue repo.",
            main_commits=[
                Commit(
                    files={"README.md": GHOST_README},
                    message="Initial commit",
                    when="2024-08-22T03:14:00",
                    author_name="ghost",
                    author_email="ghost@ether.local",
                ),
            ],
        ),
    ]


# ---- seeding ------------------------------------------------------------ #


def _seed_repo(repo: Repo, *, reset: bool) -> str:
    bare_path = (PROJECTS_DIR / repo.owner / repo.name).with_suffix(".git")
    if bare_path.exists():
        if not reset:
            return f"skip {repo.owner}/{repo.name} (already exists)"
        shutil.rmtree(bare_path)

    bare = BareRepo.open_or_init(bare_path)

    # Build the main branch first so branches can fork off it.
    main_shas: list[str] = []
    parent: str | None = None
    for c in repo.main_commits:
        sha = _commit(
            bare,
            c.files,
            message=c.message,
            author_name=c.author_name,
            author_email=c.author_email,
            parent=parent,
            when=c.when,
        )
        main_shas.append(sha)
        parent = sha
    if main_shas:
        _set_ref(bare, "refs/heads/main", main_shas[-1])

    for branch in repo.branches:
        side_parent = main_shas[branch.fork_at]
        for c in branch.commits:
            side_parent = _commit(
                bare,
                c.files,
                message=c.message,
                author_name=c.author_name,
                author_email=c.author_email,
                parent=side_parent,
                when=c.when,
            )
        _set_ref(bare, f"refs/heads/{branch.name}", side_parent)

    for tag in repo.tags:
        _set_ref(bare, f"refs/tags/{tag.name}", main_shas[tag.at])

    # Issues: writes go through the same storage helpers the API uses, so the
    # side-ref shape stays in sync with what the running app produces.
    for issue in repo.issues:
        stored = create_issue(bare, title=issue.title, body=issue.body, author=issue.author)
        for author, body in issue.comments:
            add_comment(bare, number=stored.number, body=body, author=author)
        if issue.closed:
            close_issue(bare, number=stored.number, actor="david")

    return f"seeded {repo.owner}/{repo.name}"


def main(argv: Iterable[str] | None = None) -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--reset",
        action="store_true",
        help="Wipe each repo before re-seeding (default: skip existing).",
    )
    args = parser.parse_args(list(argv) if argv is not None else None)

    PROJECTS_DIR.mkdir(parents=True, exist_ok=True)
    for repo in _all_repos():
        print(_seed_repo(repo, reset=args.reset))


if __name__ == "__main__":
    main()
