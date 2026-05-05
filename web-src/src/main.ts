// ABOUTME: Entry point for the bundled JS — boots htmx + the preload extension and the theme switcher.
// ABOUTME: bun-bundled into ../../src/gitcabin/web/static/dist/main.<hash>.js.

// htmx is the only client-side framework gitcabin's dashboard runs. The
// preload extension fetches links on hover/focus so the next-page click
// feels instant. Both ship in the same bundle; the runtime container needs
// no Node, no network fetch, nothing beyond what `bun run build` produced.
//
// Per-element configuration is done in HTML, not JS:
//   <body hx-ext="preload" preload="mouseover">
//
// htmx-ext-preload's init walks descendants for [href]/[hx-get] and resolves
// the preload value via `getClosestAttribute`, so the body-level `preload`
// attribute applies to every link in the tree without any per-anchor markup.

import "htmx.org";
import "htmx-ext-preload";

// ---- theme switcher --------------------------------------------------- //
// Three-state preference: 'light' | 'dark' | 'system'. Persisted in
// localStorage so the FOUC-blocker in _base.html can apply it synchronously
// before the bundle loads. This module wires the click handlers and keeps
// the active-button indicator in sync.
//
// 'system' means: follow prefers-color-scheme right now AND keep following it
// if the OS preference flips while the page is open — hence the matchMedia
// listener below.

type Theme = "light" | "dark" | "system";

const STORAGE_KEY = "theme";
const mql = window.matchMedia("(prefers-color-scheme: dark)");

function readTheme(): Theme {
  const v = localStorage.getItem(STORAGE_KEY);
  return v === "light" || v === "dark" || v === "system" ? v : "system";
}

function applyTheme(theme: Theme): void {
  const isDark = theme === "dark" || (theme === "system" && mql.matches);
  document.documentElement.classList.toggle("dark", isDark);
  // Mirror the active state onto the button group. The data attribute is
  // styled via Tailwind's data-[theme-active] variant in _base.html.
  for (const btn of document.querySelectorAll<HTMLElement>("[data-theme]")) {
    if (btn.dataset.theme === theme) {
      btn.setAttribute("data-theme-active", "");
    } else {
      btn.removeAttribute("data-theme-active");
    }
  }
}

function setTheme(theme: Theme): void {
  localStorage.setItem(STORAGE_KEY, theme);
  applyTheme(theme);
}

// Wire click handlers once per page lifetime. The header isn't replaced by
// htmx swaps (hx-target="main"), so the buttons persist and need handlers
// attached only at initial DOM ready.
document.addEventListener("DOMContentLoaded", () => {
  applyTheme(readTheme());
  for (const btn of document.querySelectorAll<HTMLElement>("[data-theme]")) {
    btn.addEventListener("click", () => {
      const t = btn.dataset.theme as Theme | undefined;
      if (t === "light" || t === "dark" || t === "system") setTheme(t);
    });
  }
});

// Follow OS preference changes while in 'system' mode. No-op when the user
// has explicitly chosen light or dark.
mql.addEventListener("change", () => {
  if (readTheme() === "system") applyTheme("system");
});
