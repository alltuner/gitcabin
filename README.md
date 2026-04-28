# testgit

A tiny self-hosted GitHub clone driven by the official `gh` CLI, with all metadata stored in git itself — no separate database.

## Concept

- gh has built-in support for arbitrary hosts via `GH_HOST`. The hostname `github.localhost` is special: gh sends to `http://api.github.localhost/` (REST) and `http://api.github.localhost/graphql` (GraphQL), so HTTPS is not required for local dev.
- Issues, PRs, and counters live in side refs of the bare git repo (`refs/issues/*`, `refs/prs/*`, `refs/meta/*`). Code lives in normal `refs/heads/*` and `refs/tags/*`. The two namespaces never collide.
- The HTTP API server is the only writer of metadata refs. Plain `git clone`/`git push` only see code.

## Running with Docker (recommended)

The container runs uvicorn unprivileged on port 8000 and Compose publishes that on host port 80 — which is the port gh expects for `github.localhost`. Source is bind-mounted, so edits on the host trigger autoreload inside the container.

```sh
docker compose up --build      # first time
docker compose up              # subsequent runs
```

Then point gh at it:

```sh
echo "any-token" | gh auth login --hostname github.localhost --with-token
GH_HOST=github.localhost gh auth status
```

Stop with `docker compose down` (or Ctrl-C if running in the foreground).

## Running natively (no gh)

```sh
uv run testgit
```

Listens on `127.0.0.1:8000`. This bypasses Docker and is useful for direct probing with curl / httpie, but gh won't reach it — gh dials port 80, not 8000, when `GH_HOST=github.localhost`.

## Development

```sh
uv sync                                    # install deps + editable testgit
uv run pytest                              # tests
uv run ruff check . && uv run ruff format --check .
```
