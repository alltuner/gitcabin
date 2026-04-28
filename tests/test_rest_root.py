# ABOUTME: Tests for the REST API root endpoint.
# ABOUTME: gh auth status hits this and reads the X-OAuth-Scopes header; the body is ignored.

import pytest
from fastapi.testclient import TestClient

# gh dials `/` for github.localhost (which it serves over HTTP without an /api/v3
# prefix) and `/api/v3/` for any other host (real GHES URL shape). The app must
# answer the same way at both so a sidecar TLS deploy with a real hostname works.
ROOT_PATHS = ("/", "/api/v3/")


@pytest.mark.parametrize("path", ROOT_PATHS)
def test_root_returns_200(client: TestClient, path: str) -> None:
    response = client.get(path)
    assert response.status_code == 200


@pytest.mark.parametrize("path", ROOT_PATHS)
def test_root_advertises_oauth_scopes_header(client: TestClient, path: str) -> None:
    response = client.get(path)
    scopes = response.headers.get("X-OAuth-Scopes", "")
    granted = {s.strip() for s in scopes.split(",") if s.strip()}
    # gh's auth status checks for at least these scopes when validating a token.
    assert {"repo", "read:org", "gist"}.issubset(granted)
