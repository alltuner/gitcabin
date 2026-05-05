# Project conventions

This file records project-specific rules so they don't have to be re-stated each session. Read it before doing frontend or template work.

## Frontend

### SVG icons live in `src/gitcabin/web/templates/icons/`

**Every SVG icon used by a template lives in its own file** under `templates/icons/<name>.html`. Templates never inline `<svg>...</svg>` blocks — always include the icon file.

- Each icon file emits a complete `<svg>` whose `class` attribute is `{{ icon_class | default('<sensible default>') }}` so callers can override size + color via `{% with icon_class = "..." %}{% include ... %}{% endwith %}`.
- Naming: `<topic>_<state>.html` (e.g. `issue_open.html`, `issue_closed.html`, `theme_sun.html`). Use lowercase, snake_case, no `icon_` prefix (the directory already conveys that).
- New icon? Add a new file. Don't grow a single shared file with many icons.

Why: a single edit to an icon file propagates everywhere; restyling, swapping a glyph, or fixing a viewBox is one diff. Inline SVGs scatter the change across templates.

#### Tree-view icons are vendored from Material Icon Theme (MIT)

`file.html`, `folder.html`, `symlink.html`, and every `file_<type>.html` originate from the [Material Icon Theme](https://github.com/material-extensions/vscode-material-icon-theme) (MIT-licensed) at commit `1d6af33e3ec7b561691dcab718f6abdac40c75d7`. They carry brand colors as embedded `fill="#…"`, so `text-*` Tailwind utilities won't recolor them — only `h-*`/`w-*` sizing applies. `symlink.html` is a small derivative built on top of `document.svg` with a shortcut-arrow overlay.

When adding a new file-type icon, prefer pulling from the same source (pin the same commit) and keep the ABOUTME line citing the source filename so future edits know where to refresh from.

The chrome icons (cabin, issue_*, theme_*, upstream, branch, chevron_*) remain hand-rolled monochrome glyphs that take `currentColor` — those live outside the tree view and are recolored by callers.

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

## Python

### Compose paths with pathlib, never with strings

Every filesystem path is a `pathlib.Path`. Compose with `/` for directory segments and `Path.with_suffix(".ext")` for adding an extension. Do **not** mix in `f"{name}.ext"`, string concatenation, or `os.path.join`.

```python
# good
path = (settings.data_dir / "projects" / project / name).with_suffix(".git")

# bad — f-string for the trailing segment defeats the convention
path = settings.data_dir / "projects" / project / f"{name}.git"
```

Ditto for any path-shaped value (sync targets, CSS bundle locations, …). If a function accepts a path, type-annotate it as `Path`, not `str`.

## Storage

- Bare repos at `data/projects/<owner>/<name>.git`. Metadata refs (`refs/issues/*`, `refs/prs/*`, `refs/meta/*`) live in the same bare repo as the code.
- Storage operations go through `gitcabin.storage.*`. Plumbing-level writes use `BareRepo.run_git("hash-object", …)` etc.; reads use the GitPython object graph.

## Sync (GitHub mirror)

- Configured per-repo in `refs/meta/sync` via `SyncConfig`. Carries `gh_owner`, `gh_name`, `gh_viewer_login`, etc.
- The `gh` CLI is the only thing that talks to github.com. `gitcabin.sync.gh.GhClient` wraps `gh api` with an injectable runner so tests can fake it.
- Outbound sync auto-pushes the head branch via `git push` using `gh auth git-credential` before posting a local PR.

## Tests

- All tests under `tests/`. Run with `uv run --no-dev pytest -q`.
- Web tests assert on rendered HTML content (e.g., `assert ">2<" in body` for an issue count badge), not on markup details. Refactors that change source structure but preserve rendered output should not need test changes.
