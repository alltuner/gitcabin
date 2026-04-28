# testgit

A tiny self-hosted GitHub clone driven by the official `gh` CLI, with all metadata stored in git itself — no separate database.

## Concept

- gh has built-in support for arbitrary hosts via `GH_HOST`. The hostname `github.localhost` is special: gh sends to `http://api.github.localhost/` (REST) and `http://api.github.localhost/graphql` (GraphQL), so HTTPS is not required for local dev.
- Issues, PRs, and counters live in side refs of the bare git repo (`refs/issues/*`, `refs/prs/*`, `refs/meta/*`). Code lives in normal `refs/heads/*` and `refs/tags/*`. The two namespaces never collide.
- The HTTP API server is the only writer of metadata refs. Plain `git clone`/`git push` only see code.

## Running with Docker (recommended)

The container runs granian unprivileged on port 8000 and Compose publishes that on host port 80 — which is the port gh expects for `github.localhost`.

For the dev loop with autoreload, use Compose Watch:

```sh
docker compose watch           # builds, runs, and reloads on source edits
```

Watch syncs `./src` into the container and restarts the service on each save. A change to `pyproject.toml` or `Dockerfile` triggers a full rebuild.

For a one-shot run without reload:

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

## Browsing the data with cgit

The compose stack also runs a read-only `cgit` web UI (lighttpd-backed, ~80 MB image) on port 8080 that scans `./data/repos/` for bare repos:

```sh
open http://localhost:8080/
```

Drill into a repo to see refs and commits. Because we don't write to `refs/heads/main`, the default summary page is empty — to inspect issue history, point at the side ref directly: `http://localhost:8080/octocat/hello/log/?h=refs/issues/local/1`.

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
