# Contributing to gitcabin

Thanks for considering a contribution. gitcabin is a small project — happy to review issues, ideas, and patches.

## Before you start

- **File an issue first for non-trivial changes.** A short "I'd like to add X" issue saves everyone time vs. a large PR that goes the wrong direction.
- **Tiny fixes** (typos, doc tweaks, obvious bug repros) — go straight to a PR.
- gitcabin is opinionated about scope. The goal is "small enough that one person can hold the whole thing in their head"; features that pull the project toward "GitHub Enterprise but free" will get pushed back on.

## Local setup

```sh
uv sync                                    # install deps + editable gitcabin
uv run pytest                              # tests
uv run ruff check . && uv run ruff format --check .
docker compose watch                       # full stack with autoreload
```

Python 3.14+. The repo uses [uv](https://docs.astral.sh/uv/) for dependency management and [ruff](https://docs.astral.sh/ruff/) for lint + format. CI runs the same commands.

## Pull requests

- **One concern per PR.** Bug fix + refactor + new feature in the same PR is hard to review and hard to revert.
- **Tests required for new behavior** and for bug fixes that have a reproducible failing case. Match the existing test style — pytest, real storage where possible (the conftest fixture sandboxes data per-test under `tmp_path`), no mocked-database tests.
- **Lint clean before pushing.** `uv run ruff check .` and `uv run ruff format --check .` must pass.
- **Keep diffs minimal.** Don't reformat unrelated files; don't rename variables in code you're not otherwise touching.

## Commit messages

This project uses [Conventional Commits](https://www.conventionalcommits.org/) so [Release Please](https://github.com/googleapis/release-please) can produce a changelog and version bump automatically. Commit subject format:

```
<type>(<optional scope>): <short summary>

<optional body>
```

Common types: `feat`, `fix`, `refactor`, `perf`, `docs`, `test`, `chore`, `ci`, `build`, `style`. A `feat:` triggers a minor version bump pre-1.0 (a patch bump after 1.0); a `fix:` always triggers a patch. Anything else lands in the changelog without bumping the version.

Examples:

```
feat(graphql): expose viewer.email in the schema
fix(rest): return 404 for missing repo at /api/v3/repos/:owner/:name
docs(installation): document the loopback alias workaround
chore: bump uv_build pin
```

If you're not sure what type fits, `chore:` is a safe default for low-impact changes.

## Releases

Releases are automated. Merging a PR with one or more `feat:`/`fix:` commits triggers Release Please to open a release PR with an updated `CHANGELOG.md` and version bump in `pyproject.toml`. Merging *that* PR creates a Git tag and a GitHub Release, which triggers a multi-arch Docker image build to `ghcr.io/alltuner/gitcabin`.

Maintainers — don't tag releases manually; let Release Please drive it.

## Security issues

Please don't open public issues for security vulnerabilities. See [SECURITY.md](SECURITY.md) for the disclosure process.

## Code of Conduct

By contributing you agree to abide by the [Code of Conduct](CODE_OF_CONDUCT.md).
