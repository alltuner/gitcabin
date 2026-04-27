# ABOUTME: Tests for the GraphQL `viewer` query.
# ABOUTME: gh auth status sends `{ viewer { login } }` to confirm a token resolves to a user.

from fastapi.testclient import TestClient


def test_viewer_returns_login(client: TestClient) -> None:
    response = client.post("/graphql", json={"query": "query { viewer { login } }"})

    assert response.status_code == 200
    payload = response.json()
    assert "errors" not in payload, payload
    assert payload["data"]["viewer"]["login"] == "david"


def test_viewer_login_is_configurable() -> None:
    # Recreate the app with a custom login to confirm the value flows from
    # Settings into the resolver, rather than being a hardcoded literal.
    from testgit.app import create_app
    from testgit.config import Settings

    custom_app = create_app(Settings(viewer_login="alice"))
    with TestClient(custom_app) as c:
        response = c.post("/graphql", json={"query": "query { viewer { login } }"})

    assert response.status_code == 200
    assert response.json()["data"]["viewer"]["login"] == "alice"
