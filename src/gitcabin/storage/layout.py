# ABOUTME: Disk-layout resolvers — every repo lives under data/projects/<owner>/<name>.git.
# ABOUTME: Mirrors GitHub's user/org model: a repo always has an owner segment.

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from gitcabin.storage.repo import BareRepo


@dataclass(frozen=True, slots=True)
class RepoLocation:
    """A bare repo's identity in the storage tree.

    `project` is the owner directory under data/projects/. `name` is the
    .git dir name minus the suffix. `path` is the absolute path to the
    .git directory.
    """

    project: str
    name: str
    path: Path

    @property
    def url_path(self) -> str:
        """The URL fragment after the leading slash — `<project>/<repo>`."""
        return f"{self.project}/{self.name}"


def projects_dir(data_dir: Path) -> Path:
    """Where repos live — `<project>/<name>.git`, one project per subdir."""
    return data_dir / "projects"


def _git_dir_to_repo_name(entry: Path) -> str | None:
    """Strip the `.git` suffix; return None if the entry isn't a usable bare repo."""
    if not entry.name.endswith(".git"):
        return None
    if BareRepo.open(entry) is None:
        return None
    return entry.name[: -len(".git")]


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


def find_repo(data_dir: Path, project: str, name: str) -> RepoLocation | None:
    """Locate a single repo by (project, name); None if absent."""
    path = (projects_dir(data_dir) / project / name).with_suffix(".git")
    if BareRepo.open(path) is None:
        return None
    return RepoLocation(project=project, name=name, path=path)


def open_repo(data_dir: Path, project: str, name: str) -> BareRepo | None:
    """Resolve a (project, name) pair to a BareRepo handle, or None."""
    location = find_repo(data_dir, project, name)
    if location is None:
        return None
    return BareRepo.open(location.path)
