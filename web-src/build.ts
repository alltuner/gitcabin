// ABOUTME: Bun build script — bundles JS + Tailwind CSS into hashed files + a manifest.
// ABOUTME: Run with `bun run build` from web-src/. Output: ../src/gitcabin/web/static/dist/.

import { createHash } from "node:crypto";
import { mkdir, readFile, rm, writeFile } from "node:fs/promises";
import { resolve } from "node:path";
import { $ } from "bun";

// Output lives next to the existing static/style.css that we're replacing.
// FastAPI mounts /static -> .../web/static/, so /static/dist/<file> is reachable
// without any new mount.
const ROOT = import.meta.dir;
const OUT = resolve(ROOT, "../src/gitcabin/web/static/dist");

// Incremental by default: keep the dist dir intact so `bun run --watch` plays
// nicely with `docker compose watch` (deleting + recreating files would
// trigger a flurry of restarts). `CLEAN=1 bun run build` removes orphans for
// the periodic full rebuild. Production image builds run with CLEAN.
if (process.env.CLEAN === "1") {
  await rm(OUT, { recursive: true, force: true });
}
await mkdir(OUT, { recursive: true });

// ---- JS: bun's native bundler ------------------------------------------ //

const jsResult = await Bun.build({
  entrypoints: [resolve(ROOT, "src/main.ts")],
  outdir: OUT,
  // [name].[hash].[ext] gives us cache-busting filenames the manifest can map.
  naming: "[name].[hash].[ext]",
  minify: true,
});
if (!jsResult.success) {
  console.error("JS bundle failed:");
  for (const log of jsResult.logs) console.error(log);
  process.exit(1);
}

// ---- CSS: Tailwind 4 CLI ------------------------------------------------ //

// Tailwind's bun integration is still experimental; running the CLI keeps the
// pipeline boring and well-documented. We compile to a temp file, hash the
// content, then rename to the final cache-busted filename.
const cssTmp = resolve(OUT, "_main.tmp.css");
await $`bunx @tailwindcss/cli -i ${resolve(ROOT, "src/styles.css")} -o ${cssTmp} --minify`.quiet();

const cssBytes = await readFile(cssTmp);
const cssHash = createHash("sha256").update(cssBytes).digest("hex").slice(0, 16);
const cssFile = `main.${cssHash}.css`;
await writeFile(resolve(OUT, cssFile), cssBytes);
await rm(cssTmp);

// ---- manifest ---------------------------------------------------------- //

// Logical name -> hashed filename. The Python side reads this once at startup
// and exposes an asset() Jinja global that templates use:
//   <link rel="stylesheet" href="{{ asset('main.css') }}">
const manifest: Record<string, string> = { "main.css": cssFile };
for (const out of jsResult.outputs) {
  const fname = out.path.split("/").pop()!;
  // Bun's [name].[hash].[ext] pattern produces e.g. main.abc123.js.
  // Strip the hash piece to get the logical key (main.js).
  const logical = fname.replace(/\.[a-z0-9]+(\.\w+)$/i, "$1");
  manifest[logical] = fname;
}
await writeFile(resolve(OUT, "manifest.json"), JSON.stringify(manifest, null, 2) + "\n");

console.log("built static bundle:");
for (const [k, v] of Object.entries(manifest)) console.log(`  ${k.padEnd(12)} -> ${v}`);
