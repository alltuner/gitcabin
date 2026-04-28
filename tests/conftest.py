# ABOUTME: Shared pytest fixtures.
# ABOUTME: FastAPI TestClient + per-test data_dir under tmp_path so storage stays sandboxed.

from collections.abc import Iterator
from pathlib import Path

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from testgit.app import create_app
from testgit.config import Settings


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
