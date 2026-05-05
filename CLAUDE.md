# Project conventions

This file records project-specific rules so they don't have to be re-stated each session. Read it before doing frontend or template work.

## Frontend

### SVG icons live in `src/gitcabin/web/templates/icons/`

**Every SVG icon used by a template lives in its own file** under `templates/icons/<name>.html`. Templates never inline `<svg>...</svg>` blocks — always include the icon file.

- Each icon file emits a complete `<svg>` whose `class` attribute is `{{ icon_class | default('<sensible default>') }}` so callers can override size + color via `{% with icon_class = "..." %}{% include ... %}{% endwith %}`.
- Naming: `<topic>_<state>.html` (e.g. `issue_open.html`, `issue_closed.html`, `theme_sun.html`). Use lowercase, snake_case, no `icon_` prefix (the directory already conveys that).
- New icon? Add a new file. Don't grow a single shared file with many icons.

Why: a single edit to an icon file propagates everywhere; restyling, swapping a glyph, or fixing a viewBox is one diff. Inline SVGs scatter the change across templates.

### Reusable template chunks live in their own `_*.html` files

Beyond icons, anything reused across pages is its own template:

- Layout pieces — `_repo_header.html`, `_repo_title.html`, `_repo_tabs.html`, `_issue_state_filter.html`.
- List rows — `_issue_row.html`, `_commit_row.html`, `_branch_row.html`, `_tag_row.html`, `_owner_card.html`, `_repo_card.html`. Parents loop and `{% include %}` per iteration; the row partial reads the loop var (`issue`, `c`, `ref`, …) plus surrounding context (`owner`, `name`).
- Wrapper macros — `_macros.html` exports `ui.bordered_list()` and `ui.empty_state()` block macros for the bordered-list container and the centered empty-state card. Templates `{% import "_macros.html" as ui %}` and use `{% call ui.bordered_list() %}…{% endcall %}`.

Rule: if a piece of markup is duplicated in two places, lift it to a partial / macro before the third copy lands.

### Tailwind 4 + DaisyUI

- Bundle pipeline: `web-src/build.ts` runs `scripts/dump_pygments_css.py` (writes `web-src/src/highlight.css`), then `bunx @tailwindcss/cli` against `web-src/src/styles.css`. Output goes to `src/gitcabin/web/static/dist/` with a content hash, manifest at `manifest.json`.
- DaisyUI 5 is enabled via `@plugin "daisyui"` in `styles.css`. Themes: `light --default, dark --prefersdark`.
- Theme switching is **CSS-only via `:has()`** — three radios (light / system / dark) where light + dark carry `class="theme-controller"`. The `dark:` Tailwind variant matches all three triggers (data-theme attribute, theme-controller `:has()`, `prefers-color-scheme` fallback). JS in `main.ts` only persists the choice in localStorage and mirrors it onto `data-theme` for FOUC prevention.
- Pygments tokens are bundled. Light = `default`, dark = `github-dark`. The dark rules are emitted with three prefix selectors so they fire under each dark trigger. Bare unprefixed pygments rules (`pre {}`, `td.linenos {}`) are stripped — they leak across themes.

### Build commands

```sh
cd web-src && bun run build       # one-shot, leaves dist/ contents on disk
cd web-src && CLEAN=1 bun run build   # remove orphans, full rebuild (CI / Docker)
cd web-src && bun run watch       # dev: rebuild on change
```

`uv run --no-dev pytest -q` runs the test suite.

## Storage

- Bare repos at `data/repos/<owner>/<name>.git`. Metadata refs (`refs/issues/*`, `refs/prs/*`, `refs/meta/*`) live in the same bare repo as the code.
- Storage operations go through `gitcabin.storage.*`. Plumbing-level writes use `BareRepo.run_git("hash-object", …)` etc.; reads use the GitPython object graph.

## Sync (GitHub mirror)

- Configured per-repo in `refs/meta/sync` via `SyncConfig`. Carries `gh_owner`, `gh_name`, `gh_viewer_login`, etc.
- The `gh` CLI is the only thing that talks to github.com. `gitcabin.sync.gh.GhClient` wraps `gh api` with an injectable runner so tests can fake it.
- Outbound sync auto-pushes the head branch via `git push` using `gh auth git-credential` before posting a local PR.

## Tests

- All tests under `tests/`. Run with `uv run --no-dev pytest -q`.
- Web tests assert on rendered HTML content (e.g., `assert ">2<" in body` for an issue count badge), not on markup details. Refactors that change source structure but preserve rendered output should not need test changes.
