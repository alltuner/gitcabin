# ABOUTME: Shared pytest fixtures.
# ABOUTME: FastAPI TestClient + per-test data_dir under tmp_path so storage stays sandboxed.

from collections.abc import Callable, Iterator
from pathlib import Path

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from gitcabin.app import create_app
from gitcabin.config import Settings
from gitcabin.storage.repo import BareRepo
from gitcabin.web.app import create_app as create_web_app


@pytest.fixture
def settings(tmp_path: Path) -> Settings:
    # Per-test data_dir under tmp_path so writes by one test never bleed
    # into another. Without this the fixture-data tests still pass, but
    # storage-touching tests would race on the same /app/data path.
    return Settings(data_dir=tmp_path / "data")


@pytest.fixture
def app(settings: Settings) -> FastAPI:
    return create_app(settings)


@pytest.fixture
def client(app: FastAPI) -> Iterator[TestClient]:
    with TestClient(app) as c:
        yield c


@pytest.fixture
def web_app(settings: Settings) -> FastAPI:
    """Separate FastAPI app for the HTML dashboard.

    The web UI runs in its own process in production; tests build it the same
    way so the API and dashboard test surfaces stay strictly separated.
    """
    return create_web_app(settings)


@pytest.fixture
def web_client(web_app: FastAPI) -> Iterator[TestClient]:
    with TestClient(web_app) as c:
        yield c


@pytest.fixture
def init_repo(settings: Settings) -> Callable[[str, str], BareRepo]:
    """Returns a callable that pre-initializes a bare repo at <data_dir>/repos/<owner>/<name>.git.

    Resolvers under strict-mode (Query.repository) return None unless the
    repo exists on disk. Tests that exercise read paths use this to set up
    that precondition.
    """

    def _init(owner: str, name: str) -> BareRepo:
        return BareRepo.open_or_init(settings.data_dir / "repos" / owner / f"{name}.git")

    return _init
