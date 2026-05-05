# ABOUTME: Tests for gitcabin.combined — the Host-header ASGI dispatcher.
# ABOUTME: Hits both branches (api.* -> API, anything else -> dashboard) and lifespan.

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from gitcabin.combined import create_app
from gitcabin.config import Settings


@pytest.fixture
def combined_client(settings: Settings) -> TestClient:
    return TestClient(create_app(settings))


def test_api_root_routes_to_api_when_host_is_api_prefixed(
    combined_client: TestClient,
) -> None:
    # gh's `auth status` does GET / against api.<configured-host>. The
    # response body is irrelevant ({} per the API root); the X-OAuth-Scopes
    # header is what `gh` reads. Its presence is the signal we hit the API.
    response = combined_client.get("/", headers={"Host": "api.github.localhost"})
    assert response.status_code == 200
    assert response.headers["x-oauth-scopes"] == "repo, read:org, gist"
    assert response.json() == {}


def test_root_routes_to_dashboard_for_browser_host(
    combined_client: TestClient,
) -> None:
    # A browser hitting `http://localhost:8080/` sends `Host: localhost:8080`.
    # The dashboard owns this path and serves an HTML page.
    response = combined_client.get("/", headers={"Host": "localhost:8080"})
    assert response.status_code == 200
    assert response.headers["content-type"].startswith("text/html")


def test_api_path_under_api_subdomain_reaches_graphql(
    combined_client: TestClient,
) -> None:
    response = combined_client.post(
        "/graphql",
        headers={"Host": "api.github.localhost"},
        json={"query": "{ viewer { login } }"},
    )
    assert response.status_code == 200
    payload = response.json()
    assert "errors" not in payload, payload
    assert payload["data"]["viewer"]["login"]


def test_api_v3_path_under_api_subdomain(combined_client: TestClient) -> None:
    # Both bare and /api/v3 shapes should land on the API regardless of Host;
    # the dispatcher routes by Host, the API itself mounts both shapes.
    response = combined_client.get(
        "/api/v3", headers={"Host": "api.github.localhost"}
    )
    assert response.status_code == 200


def test_dashboard_request_with_no_api_prefix(combined_client: TestClient) -> None:
    # `Host: gitcabin-dashboard:8000` (the in-network service name) should
    # route to the dashboard. Anything that doesn't start with `api.` does.
    response = combined_client.get(
        "/", headers={"Host": "gitcabin-dashboard:8000"}
    )
    assert response.status_code == 200
    assert response.headers["content-type"].startswith("text/html")


def test_host_match_is_case_insensitive(combined_client: TestClient) -> None:
    # HTTP host matching is case-insensitive per RFC 3986. The dispatcher
    # lowercases before comparing, so an uppercase Host still routes correctly.
    response = combined_client.get("/", headers={"Host": "API.github.localhost"})
    assert response.status_code == 200
    assert response.headers["x-oauth-scopes"] == "repo, read:org, gist"


def test_lifespan_handshake_completes_without_error(settings: Settings) -> None:
    # The TestClient runs the full lifespan handshake at __enter__ /
    # __exit__. If our trivial implementation is broken, the context-manager
    # entry would hang or raise.
    with TestClient(create_app(settings)) as client:
        # Quick sanity check that the app is actually responsive after
        # startup completed.
        response = client.get("/", headers={"Host": "api.github.localhost"})
        assert response.status_code == 200
