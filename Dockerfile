# Multi-stage:
#   stage 1 (assets) — bun bundles htmx + tailwind into hashed JS/CSS.
#   stage 2 (runtime) — python:3.14-slim with the bundled assets and the app.
#
# `docker compose up --watch` reuses the runtime stage and syncs ./src on top
# for live reload (and streams the container's stdout). The bundler is run on
# the host during dev (bun run watch); the image build is what produces the
# prod-shaped bundle.

# ---- stage 1: assets ---------------------------------------------------- #
#
# The bun stage mirrors the host project layout exactly — web-src/ alongside
# src/gitcabin/web/templates/ — so styles.css's @source globs resolve to real
# files. If the layout drifts, Tailwind silently emits a CSS with no utility
# classes and the dashboard renders unstyled.

FROM oven/bun:1 AS assets
WORKDIR /work

# build.ts shells out to `uv run python scripts/dump_pygments_css.py`
# during the bundle (pygments tokens get inlined into the Tailwind output),
# so the bun stage needs uv on PATH. Pull it from the official image —
# fastest way, no compile step. The uv binary is self-contained; it brings
# its own python at first use.
COPY --from=ghcr.io/astral-sh/uv:latest /uv /usr/local/bin/uv

# Cache deps in a separate layer. bun.lock pins everything we install.
COPY web-src/package.json web-src/bun.lock ./web-src/
RUN cd web-src && bun install --frozen-lockfile

# Bundler input plus the project metadata + source `uv run` needs. The
# templates are what Tailwind 4's @source directive scans (the bun stage
# doesn't run the Python app, but tailwindcss needs the HTML to know which
# utility classes to emit). pyproject.toml + uv.lock + src/ + scripts/ are
# what `uv run scripts/dump_pygments_css.py` resolves at bundle time. All
# of this stays in stage 1 — the runtime image only copies the bundled
# CSS/JS out of /work/src/gitcabin/web/static/dist/.
COPY web-src/build.ts web-src/src/ ./web-src/
COPY web-src/src/ ./web-src/src/
COPY pyproject.toml uv.lock README.md LICENSE ./
COPY src/ ./src/
COPY scripts/ ./scripts/

# Build the bundle. `CLEAN=1 bun run build` writes hashed files + manifest.json
# to ./src/gitcabin/web/static/dist/ — the runtime stage copies from there.
RUN mkdir -p ./src/gitcabin/web/static && cd web-src && bun run build

# ---- stage 2: runtime --------------------------------------------------- #

FROM python:3.14-slim

# git is required at runtime: the storage layer shells out to git plumbing
# (hash-object, mktree, commit-tree, update-ref) for every metadata write.
# gh is required when sync runs inside the container (`docker compose exec
# gitcabin gitcabin sync …`); the user's host-side ~/.config/gh is bind-
# mounted in compose so the container's gh inherits the same auth token.
# python:3.14-slim doesn't include either.
RUN apt-get update && \
    apt-get install -y --no-install-recommends ca-certificates curl gnupg git && \
    install -d -m 0755 /etc/apt/keyrings && \
    curl -fsSL https://cli.github.com/packages/githubcli-archive-keyring.gpg \
        -o /etc/apt/keyrings/githubcli-archive-keyring.gpg && \
    chmod go+r /etc/apt/keyrings/githubcli-archive-keyring.gpg && \
    echo "deb [arch=$(dpkg --print-architecture) signed-by=/etc/apt/keyrings/githubcli-archive-keyring.gpg] https://cli.github.com/packages stable main" \
        > /etc/apt/sources.list.d/github-cli.list && \
    apt-get update && \
    apt-get install -y --no-install-recommends gh && \
    apt-get purge -y --auto-remove curl gnupg && \
    rm -rf /var/lib/apt/lists/*

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
# autoreload, use `docker compose up --watch` — Compose syncs source into
# the container, restarts the service on each change without rebuilding,
# and streams stdout so the access log entries land in your terminal.
CMD ["uv", "run", "--no-dev", "granian", \
     "--interface", "asgi", "--factory", \
     "--access-log", \
     "--host", "0.0.0.0", "--port", "8000", \
     "gitcabin.combined:create_app"]
