# ABOUTME: Module entry point: `gitcabin` runs the server, `gitcabin sync ...` dispatches CLI.
# ABOUTME: The server binds an unprivileged port; Docker Compose republishes it on 80 for gh.

from __future__ import annotations

import sys
from pathlib import Path

from granian import Granian
from granian.constants import Interfaces

# gh sends to http://api.github.localhost/ — port 80, hardcoded — when GH_HOST
# is github.localhost. Binding directly here would need root, so we listen on
# an unprivileged port and let `compose.yml` publish 80 -> 8000 via Docker.
# Plain `uv run gitcabin` (outside Docker) is fine for unit-level probing on
# 127.0.0.1:8000 but won't be reachable as `api.github.localhost` from gh.
HOST = "127.0.0.1"
PORT = 8000


def main() -> None:
    # If invoked as `gitcabin sync ...`, dispatch to the sync CLI; otherwise
    # default to running the server. Keeps `uv run gitcabin` working as before.
    if len(sys.argv) >= 2 and sys.argv[1] == "sync":
        from gitcabin.cli import main as cli_main

        sys.exit(cli_main(sys.argv[2:]))

    # Watch only the package source for reload. Without an explicit reload path,
    # granian defaults to the current working directory, which is the uv
    # workspace root here and triggers reloads on unrelated edits.
    src_dir = Path(__file__).resolve().parent
    Granian(
        target="gitcabin.app:create_app",
        factory=True,
        interface=Interfaces.ASGI,
        address=HOST,
        port=PORT,
        reload=True,
        reload_paths=[src_dir],
    ).serve()


if __name__ == "__main__":
    main()
