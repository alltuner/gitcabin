# ABOUTME: Shared pytest fixtures.
# ABOUTME: Provides a FastAPI TestClient bound to a fresh app instance per test.

from collections.abc import Iterator

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from testgit.app import create_app


@pytest.fixture
def app() -> FastAPI:
    return create_app()


@pytest.fixture
def client(app: FastAPI) -> Iterator[TestClient]:
    with TestClient(app) as c:
        yield c
