# ABOUTME: Module entry point: `python -m testgit` or `uv run testgit` starts the server.
# ABOUTME: Defaults to api.github.localhost:80 — the host gh sends to for github.localhost.

from __future__ import annotations

import os

import uvicorn


def main() -> None:
    host = os.environ.get("TESTGIT_HOST", "api.github.localhost")
    port = int(os.environ.get("TESTGIT_PORT", "80"))
    uvicorn.run("testgit.app:create_app", host=host, port=port, factory=True, reload=False)


if __name__ == "__main__":
    main()
