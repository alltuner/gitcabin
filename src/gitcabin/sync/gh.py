# ABOUTME: Wrapper around `gh api` that the sync layer uses to talk to GitHub.
# ABOUTME: All sync goes through this client so tests can fake subprocess cleanly.

from __future__ import annotations

import json
import subprocess
from dataclasses import dataclass
from typing import Protocol


class GhRunner(Protocol):
    """Callable that runs `gh <argv>` and returns stdout (text).

    Real implementations shell out via subprocess; tests substitute a
    deterministic stand-in. `stdin` is forwarded for endpoints that accept
    a request body via `gh api --input -`.
    """

    def __call__(self, argv: list[str], *, stdin: str | None = None) -> str: ...


def _default_runner(argv: list[str], *, stdin: str | None = None) -> str:
    """Shell out to `gh` with the given args and return stdout (text).

    Raises subprocess.CalledProcessError on non-zero exit. Higher layers decide
    which errors are recoverable — at this layer we don't have the context to
    distinguish "you're not authenticated" from "the network is down."
    """
    result = subprocess.run(
        ["gh", *argv],
        input=stdin,
        capture_output=True,
        text=True,
        check=True,
    )
    return result.stdout


@dataclass(frozen=True, slots=True)
class GhClient:
    """Bound to a single host; issues `gh api` calls and decodes JSON responses.

    `host` lands on `--hostname <host>`. The runner is the seam tests use to
    inject canned responses without touching subprocess.
    """

    host: str = "github.com"
    runner: GhRunner = _default_runner

    def get_json(self, path: str, *, paginate: bool = False) -> object:
        """GET `path` and decode the body as JSON.

        With `paginate=True`, gh follows Link headers and concatenates the
        resulting JSON arrays into a single array. Use it for any list endpoint
        that might exceed 30 items.
        """
        argv = ["api", "--hostname", self.host]
        if paginate:
            argv.append("--paginate")
        argv.append(path)
        return json.loads(self.runner(argv))

    def post_json(self, path: str, body: dict[str, object]) -> object:
        """POST `body` (encoded as JSON) to `path` and decode the response.

        Goes through `gh api -X POST --input -`, which reads the request body
        from stdin — robust against arbitrary-content fields like issue bodies
        with newlines, quotes, and unicode that `-f field=value` can mangle.
        """
        argv = ["api", "--hostname", self.host, "-X", "POST", "--input", "-", path]
        raw = self.runner(argv, stdin=json.dumps(body))
        return json.loads(raw)

    def patch_json(self, path: str, body: dict[str, object]) -> object:
        """PATCH `body` (encoded as JSON) to `path` and decode the response.

        Same shape as post_json but with `-X PATCH`. Used to flip an issue's
        state (open ↔ closed) or update other mutable fields on existing items.
        """
        argv = ["api", "--hostname", self.host, "-X", "PATCH", "--input", "-", path]
        raw = self.runner(argv, stdin=json.dumps(body))
        return json.loads(raw)


def gh_login(client: GhClient) -> str:
    """Return the gh-side login on the client's host.

    Wraps the `/user` endpoint, which returns the authenticated user's payload.
    Raises RuntimeError if the response shape doesn't match what we expect —
    this is a "did gh change its output format?" guard, not a routine error.
    """
    payload = client.get_json("user")
    if not isinstance(payload, dict) or "login" not in payload:
        raise RuntimeError(f"unexpected /user response: {payload!r}")
    return str(payload["login"])
