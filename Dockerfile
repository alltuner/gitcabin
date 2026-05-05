# Single-stage image: small enough that layering doesn't pay off, and we want
# `uv run` available at runtime so the dev workflow inside the container matches
# the one outside.

FROM python:3.14-slim

# git is required at runtime: the storage layer shells out to git plumbing
# (hash-object, mktree, commit-tree, update-ref) for every metadata write.
# python:3.14-slim doesn't include it.
RUN apt-get update && apt-get install -y --no-install-recommends git && rm -rf /var/lib/apt/lists/*

# Pull uv from its official image — fastest way to get the binary, no compile
# step, version pinned by the tag we choose.
COPY --from=ghcr.io/astral-sh/uv:latest /uv /usr/local/bin/uv

# Non-root user. uid 1000 matches the typical first-user uid on Linux hosts
# so bind-mounted files keep sane ownership both ways. macOS hosts don't care
# about uid mapping (Docker Desktop translates), so this is harmless there.
RUN useradd --create-home --uid 1000 app

WORKDIR /app
RUN chown app:app /app
USER app

# Copy the bits uv_build needs to build the gitcabin wheel: pyproject (deps +
# build config), README (project.readme), LICENSE (project.license file), and
# the package source. The src/ tree gets overlaid by a bind mount in compose
# for live reload, but we still need it at build time so `uv sync` can install
# gitcabin editable.
COPY --chown=app:app pyproject.toml README.md LICENSE ./
COPY --chown=app:app src/ ./src/

# Pre-create the data directory so a host bind mount inherits its ownership
# (uid 1000). Without this, Docker creates the empty bind-mount target as
# root and the server can't write its bare repos.
RUN mkdir -p /app/data && chown app:app /app/data

# UV_LINK_MODE=copy avoids hardlinks across the layer-cache boundary, which
# uv otherwise warns about inside Docker. UV_PROJECT_ENVIRONMENT pins the
# venv inside the image so it doesn't drift to /tmp or similar.
ENV UV_LINK_MODE=copy \
    UV_PROJECT_ENVIRONMENT=/app/.venv \
    PATH="/app/.venv/bin:${PATH}"

# --no-dev: skip pytest/ruff/httpx in the runtime image. Tests run on the host.
RUN uv sync --no-dev

EXPOSE 8000

# Production-shaped CMD: no --reload. For dev autoreload, use
# `docker compose watch` — Compose syncs source into the container and
# restarts the service on each change without rebuilding.
CMD ["uv", "run", "--no-dev", "granian", \
     "--interface", "asgi", "--factory", \
     "--host", "0.0.0.0", "--port", "8000", \
     "gitcabin.app:create_app"]
