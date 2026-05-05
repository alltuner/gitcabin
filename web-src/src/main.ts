// ABOUTME: Entry point for the bundled JS — boots htmx + the preload extension.
// ABOUTME: bun-bundled into ../../src/gitcabin/web/static/dist/main.<hash>.js.

// htmx is the only client-side script gitcabin's dashboard runs. The preload
// extension fetches links on hover/focus so the next-page click feels instant.
// Both ship in the same bundle; the runtime container needs no Node, no
// network fetch, nothing beyond what `bun run build` produced.
//
// Per-element configuration is done in HTML, not JS:
//   <body hx-ext="preload" preload="mouseover">
//
// htmx-ext-preload's init walks descendants for [href]/[hx-get] and resolves
// the preload value via `getClosestAttribute`, so the body-level `preload`
// attribute applies to every link in the tree without any per-anchor markup.

import "htmx.org";
import "htmx-ext-preload";
