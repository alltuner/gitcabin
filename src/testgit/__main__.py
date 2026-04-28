# ABOUTME: Module entry point: `python -m testgit` or `uv run testgit` starts the server.
# ABOUTME: Binds an unprivileged port; Docker Compose republishes it on host port 80 for gh.

from __future__ import annotations

from pathlib import Path

import uvicorn

# gh sends to http://api.github.localhost/ — port 80, hardcoded — when GH_HOST
# is github.localhost. Binding directly here would need root, so we listen on
# an unprivileged port and let `compose.yml` publish 80 -> 8000 via Docker.
# Plain `uv run testgit` (outside Docker) is fine for unit-level probing on
# 127.0.0.1:8000 but won't be reachable as `api.github.localhost` from gh.
HOST = "127.0.0.1"
PORT = 8000


def main() -> None:
    # Watch only the package source for reload. Without an explicit reload_dirs,
    # uvicorn defaults to the current working directory, which is the uv
    # workspace root here and triggers reloads on unrelated edits.
    src_dir = str(Path(__file__).resolve().parent)
    uvicorn.run(
        "testgit.app:create_app",
        host=HOST,
        port=PORT,
        factory=True,
        reload=True,
        reload_dirs=[src_dir],
    )


if __name__ == "__main__":
    main()
