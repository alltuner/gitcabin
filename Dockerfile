# Multi-stage:
#   stage 1 (assets) — bun bundles htmx + tailwind into hashed JS/CSS.
#   stage 2 (runtime) — python:3.14-slim with the bundled assets and the app.
#
# `docker compose watch` reuses the runtime stage and bind-mounts ./src on top
# for live reload. The bundler is run on the host during dev (bun run watch);
# the image build is what produces the prod-shaped bundle.

# ---- stage 1: assets ---------------------------------------------------- #
#
# The bun stage mirrors the host project layout exactly — web-src/ alongside
# src/gitcabin/web/templates/ — so styles.css's @source globs resolve to real
# files. If the layout drifts, Tailwind silently emits a CSS with no utility
# classes and the dashboard renders unstyled.

FROM oven/bun:1 AS assets
WORKDIR /work

# Cache deps in a separate layer. bun.lock pins everything we install.
COPY web-src/package.json web-src/bun.lock ./web-src/
RUN cd web-src && bun install --frozen-lockfile

# Bundler input + the templates Tailwind 4's @source directive scans. The
# templates are read-only here (this stage doesn't run the Python app), but
# tailwindcss needs to see their HTML to know which utility classes to emit.
COPY web-src/build.ts web-src/src/ ./web-src/
COPY web-src/src/ ./web-src/src/
COPY src/gitcabin/web/templates/ ./src/gitcabin/web/templates/

# Build the bundle. `CLEAN=1 bun run build` writes hashed files + manifest.json
# to ./src/gitcabin/web/static/dist/ — the runtime stage copies from there.
RUN mkdir -p ./src/gitcabin/web/static && cd web-src && bun run build

# ---- stage 2: runtime --------------------------------------------------- #

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

# Bundled assets from the bun stage — hashed JS/CSS plus the manifest.json
# the asset() helper reads at template-render time.
COPY --from=assets --chown=app:app /work/src/gitcabin/web/static/dist/ ./src/gitcabin/web/static/dist/

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

# Production-shaped CMD: no --reload. --access-log on so requests are
# visible in `docker compose logs` (granian defaults to off). For dev
# autoreload, use `docker compose watch` — Compose syncs source into the
# container and restarts the service on each change without rebuilding.
CMD ["uv", "run", "--no-dev", "granian", \
     "--interface", "asgi", "--factory", \
     "--access-log", \
     "--host", "0.0.0.0", "--port", "8000", \
     "gitcabin.combined:create_app"]
