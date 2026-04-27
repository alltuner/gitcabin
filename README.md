# testgit

A tiny self-hosted GitHub clone driven by the official `gh` CLI, with all metadata stored in git itself — no separate database.

## Concept

- gh has built-in support for arbitrary hosts via `GH_HOST`. The hostname `github.localhost` is special: gh sends to `http://api.github.localhost/` (REST) and `http://api.github.localhost/graphql` (GraphQL), so HTTPS is not required for local dev.
- Issues, PRs, and counters live in side refs of the bare git repo (`refs/issues/*`, `refs/prs/*`, `refs/meta/*`). Code lives in normal `refs/heads/*` and `refs/tags/*`. The two namespaces never collide.
- The HTTP API server is the only writer of metadata refs. Plain `git clone`/`git push` only see code.

## Running

Bind to `api.github.localhost` (the hostname gh resolves for `github.localhost`):

```sh
uv run testgit
```

Defaults to port 80, which requires root on Unix. Override with `TESTGIT_PORT` and front it with a port-forwarder if you'd rather not run as root.

Then point gh at it:

```sh
echo "any-token" | gh auth login --hostname github.localhost --with-token
GH_HOST=github.localhost gh auth status
```

## Development

```sh
uv sync           # install deps + editable testgit
uv run pytest     # tests
uv run ruff check . && uv run ruff format --check .
```
