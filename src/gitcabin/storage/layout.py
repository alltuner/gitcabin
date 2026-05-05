# ABOUTME: Disk-layout resolvers — maps URL segments to bare-repo paths.
# ABOUTME: Two trees coexist: data/repos/<name>.git for root, data/projects/<project>/<name>.git for nested.

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Literal

from gitcabin.storage.repo import BareRepo


@dataclass(frozen=True, slots=True)
class RepoLocation:
    """A bare repo's identity in the storage tree.

    `project` is None for repos at `data/repos/<name>.git` (root, projectless)
    and the project directory name otherwise. `name` is the .git dir name
    minus the suffix. `path` is the absolute path to the .git directory.
    """

    project: str | None
    name: str
    path: Path

    @property
    def url_path(self) -> str:
        """The URL fragment after the leading slash — `<repo>` or `<project>/<repo>`."""
        if self.project is None:
            return self.name
        return f"{self.project}/{self.name}"


def root_repos_dir(data_dir: Path) -> Path:
    """Where projectless repos live — flat directory of `<name>.git` entries."""
    return data_dir / "repos"


def projects_dir(data_dir: Path) -> Path:
    """Where project-grouped repos live — `<project>/<name>.git`, one project per subdir."""
    return data_dir / "projects"


def _git_dir_to_repo_name(entry: Path) -> str | None:
    """Strip the `.git` suffix; return None if the entry isn't a usable bare repo."""
    if not entry.name.endswith(".git"):
        return None
    if BareRepo.open(entry) is None:
        return None
    return entry.name[: -len(".git")]


def list_root_repos(data_dir: Path) -> list[RepoLocation]:
    """Every `<name>.git` directly under data/repos/, sorted by name."""
    base = root_repos_dir(data_dir)
    if not base.is_dir():
        return []
    out: list[RepoLocation] = []
    for entry in sorted(base.iterdir(), key=lambda e: e.name):
        if not entry.is_dir():
            continue
        name = _git_dir_to_repo_name(entry)
        if name is None:
            continue
        out.append(RepoLocation(project=None, name=name, path=entry))
    return out


def list_projects(data_dir: Path) -> list[str]:
    """Names of all subdirectories under data/projects/, sorted."""
    base = projects_dir(data_dir)
    if not base.is_dir():
        return []
    return sorted(entry.name for entry in base.iterdir() if entry.is_dir())


def list_repos_in_project(data_dir: Path, project: str) -> list[RepoLocation]:
    """Every `<name>.git` under data/projects/<project>/, sorted by name."""
    project_dir = projects_dir(data_dir) / project
    if not project_dir.is_dir():
        return []
    out: list[RepoLocation] = []
    for entry in sorted(project_dir.iterdir(), key=lambda e: e.name):
        if not entry.is_dir():
            continue
        name = _git_dir_to_repo_name(entry)
        if name is None:
            continue
        out.append(RepoLocation(project=project, name=name, path=entry))
    return out


def find_repo(
    data_dir: Path, project: str | None, name: str
) -> RepoLocation | None:
    """Locate a single repo by (project, name); None if absent."""
    if project is None:
        path = (root_repos_dir(data_dir) / name).with_suffix(".git")
    else:
        path = (projects_dir(data_dir) / project / name).with_suffix(".git")
    if BareRepo.open(path) is None:
        return None
    return RepoLocation(project=project, name=name, path=path)


def open_repo(data_dir: Path, project: str | None, name: str) -> BareRepo | None:
    """Resolve a (project, name) pair to a BareRepo handle, or None."""
    location = find_repo(data_dir, project, name)
    if location is None:
        return None
    return BareRepo.open(location.path)


def resolve_segment(
    data_dir: Path, segment: str
) -> Literal["root_repo", "project"] | None:
    """What does a single URL segment refer to?

    - "root_repo": `data/repos/<segment>.git` exists (a projectless repo)
    - "project":   `data/projects/<segment>/` exists (a project directory)
    - None:        neither — caller raises 404

    Calling code uses this to dispatch `/{seg}` requests between the
    root-repo overview and the project page without an extra route.
    """
    if find_repo(data_dir, project=None, name=segment) is not None:
        return "root_repo"
    if (projects_dir(data_dir) / segment).is_dir():
        return "project"
    return None
