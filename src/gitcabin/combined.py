# ABOUTME: Combined ASGI app — Host-header dispatcher that fronts API + dashboard.
# ABOUTME: Lets the compose stack expose a single port instead of two.

# `Host: api.github.localhost` (cab traffic via gh's HTTP_PROXY) routes to the
# REST + GraphQL API. Anything else (browser hitting `localhost:8080`) routes
# to the HTML dashboard. One container, one port, no privileged binding.
#
# Why dispatch by Host and not by path: gh's auth-status check is
# `GET http://api.github.localhost/`. The path is `/`, the same as the
# dashboard's homepage. Path-based routing would conflict; Host-based
# routing doesn't (cab + gh always send `Host: api.X`, browsers don't).

from __future__ import annotations

from typing import Any

from gitcabin.app import create_app as create_api_app
from gitcabin.config import Settings
from gitcabin.web.app import create_app as create_web_app


def create_app(settings: Settings | None = None) -> Any:
    """Build a single ASGI app that dispatches by Host header.

    Returns a callable conforming to the ASGI interface that forwards each
    HTTP request to either the API or the web app depending on whether the
    `Host:` header begins with `api.`. Lifespan messages are answered with
    a trivial handshake — neither sub-app has startup/shutdown hooks today,
    so there's nothing to forward.
    """
    settings = settings or Settings.from_env()
    api = create_api_app(settings)
    web = create_web_app(settings)

    async def dispatcher(scope: dict[str, Any], receive: Any, send: Any) -> None:
        if scope["type"] == "http":
            await _route_http(scope, receive, send, api, web)
            return
        if scope["type"] == "lifespan":
            await _trivial_lifespan(receive, send)
            return
        # WebSocket etc — gitcabin doesn't use them today. Default to the
        # dashboard so future additions there don't fall through to a 404.
        await web(scope, receive, send)

    return dispatcher


async def _route_http(
    scope: dict[str, Any],
    receive: Any,
    send: Any,
    api: Any,
    web: Any,
) -> None:
    """Dispatch a single HTTP request based on its Host header."""
    host = _host_from_scope(scope).lower()
    target = api if host.startswith("api.") else web
    await target(scope, receive, send)


def _host_from_scope(scope: dict[str, Any]) -> str:
    """Extract the Host header from an ASGI scope. Empty string if absent."""
    for k, v in scope.get("headers", ()):
        if k == b"host":
            # Host headers are ASCII per RFC 7230; latin-1 round-trips bytes
            # without raising on stray non-ASCII (which would be malformed
            # but we don't want a spurious decode error).
            return v.decode("latin-1")
    return ""


async def _trivial_lifespan(receive: Any, send: Any) -> None:
    """Answer the ASGI lifespan handshake without invoking either sub-app.

    Neither the API nor the dashboard has startup/shutdown hooks today, so
    "forwarding" lifespan to one or both apps would be a no-op anyway. If
    that changes, this function should grow into a proper multiplexer that
    runs both apps' lifespans in parallel. Until then, simpler is better.
    """
    while True:
        message = await receive()
        if message["type"] == "lifespan.startup":
            await send({"type": "lifespan.startup.complete"})
        elif message["type"] == "lifespan.shutdown":
            await send({"type": "lifespan.shutdown.complete"})
            return
