# ABOUTME: Tests for the REST API root endpoint.
# ABOUTME: gh auth status hits this and reads the X-OAuth-Scopes header; the body is ignored.

from fastapi.testclient import TestClient


def test_root_returns_200(client: TestClient) -> None:
    response = client.get("/")
    assert response.status_code == 200


def test_root_advertises_oauth_scopes_header(client: TestClient) -> None:
    response = client.get("/")
    scopes = response.headers.get("X-OAuth-Scopes", "")
    granted = {s.strip() for s in scopes.split(",") if s.strip()}
    # gh's auth status checks for at least these scopes when validating a token.
    assert {"repo", "read:org", "gist"}.issubset(granted)
