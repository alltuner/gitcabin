# Web assets

The dashboard's CSS and JS are bundled by [bun](https://bun.sh) from sources in `web-src/` and written to `src/gitcabin/web/static/dist/` as content-hashed files. No CDN dependency, no runtime fetch from third parties; everything ships in the docker image.

## Layout

```
web-src/                          tracked input
├── package.json                  bun deps (htmx, tailwindcss, all dev-only)
├── bun.lock                      pinned versions
├── build.ts                      bundler entrypoint
└── src/
    ├── main.ts                   imports htmx + htmx-ext-preload
    └── styles.css                @import "tailwindcss" + custom rules

src/gitcabin/web/static/dist/     gitignored output
├── manifest.json                 logical name → hashed filename
├── main.<sha256>.css             Tailwind 4 + custom rules, minified
└── main.<bunhash>.js             htmx + extension, minified
```

## How it gets to the user

1. `bun run build` (or `bun run watch` during dev) emits hashed files plus `manifest.json` into `static/dist/`.
2. FastAPI's static handler serves anything under `/static/` directly. Files under `/static/dist/` get `Cache-Control: public, max-age=31536000, immutable` because their URL is content-addressed — same content, same URL, forever.
3. Templates write `{{ asset('main.css') }}` (Jinja2 global, registered in `routes.py`). The `AssetResolver` in `gitcabin.web.assets` reads `manifest.json` per render and returns `/static/dist/main.<hash>.css`.
4. Browsers cache aggressively because the URL changes whenever the content does — perfect cache hit rate without staleness.

## htmx + preload

`main.ts` imports two things and exits:

```ts
import "htmx.org";
import "htmx-ext-preload";
```

The base template opts the whole document into the extension and sets the preload trigger:

```html
<body hx-ext="preload" preload="mouseover">
```

`htmx-ext-preload` uses `getClosestAttribute` when initialising candidate elements (anything with `[href]`, `[hx-get]`, `[data-hx-get]`), so the `preload="mouseover"` on body inherits down to every link in the tree without per-link annotation. Hovering a link for ~100 ms triggers a prefetch; the next click renders from cache.

## Building

Local development (host-side):

```sh
cd web-src
bun install                       # one-time
bun run watch                     # rebuild on every web-src/ change
```

In another terminal:

```sh
docker compose watch              # syncs ./src into the container
```

Compose's watch rules sync `static/dist/` without restarting granian, since the manifest is re-read on every render. Python source changes still restart granian normally.

Production (image build):

```sh
docker compose build              # multi-stage Dockerfile runs bun + uv
```

The first stage uses `oven/bun:1`, runs `bun install --frozen-lockfile` and `CLEAN=1 bun run build`, then the runtime stage `COPY --from=assets`.

## Versioning policy

| File | Cache strategy | Why |
|---|---|---|
| `/static/dist/manifest.json` | default short cache | small, must be fresh so old URLs don't leak |
| `/static/dist/<hashed>.{css,js}` | `max-age=1y, immutable` | filename IS the version; never changes for a given content |
| `/static/<other>` | default | only legacy assets land here; treat as ephemeral |

If you need to invalidate everything, change the underlying content and rebuild — a new hash means a new URL means a new cache entry.

## Why bun (and not webpack / vite / esbuild / parcel)

- **One binary, zero runtime config.** `bun install` + `bun run build` is the entire pipeline; no `webpack.config.js`, no plugin trees.
- **Native CSS handling via Tailwind 4 CLI.** No PostCSS plugin chains.
- **Fast.** Sub-second cold builds; sub-100 ms incremental on watch.
- **No JS runtime cost.** The bundle ships htmx + extension, ~50 KB minified. Bun's runtime isn't shipped — only its bundler runs.

If we ever need a richer build (TypeScript type checks, image processing, etc.), we revisit. For now the pipeline is small enough that anyone reading `web-src/build.ts` understands the whole thing.
