// ABOUTME: Entry point for the bundled JS — boots htmx + the preload extension and persists the theme choice.
// ABOUTME: bun-bundled into ../../src/gitcabin/web/static/dist/main.<hash>.js.

// htmx is the only client-side framework gitcabin's dashboard runs. The
// preload extension fetches links on hover/focus so the next-page click
// feels instant. Both ship in the same bundle; the runtime container needs
// no Node, no network fetch, nothing beyond what `bun run build` produced.
//
// Per-element configuration is done in HTML, not JS:
//   <body hx-ext="preload" preload="mouseover">

import "htmx.org";
import "htmx-ext-preload";

// ---- theme persistence ------------------------------------------------ //
// The theme switcher itself is browser-native: DaisyUI's theme-controller
// pattern uses CSS `:root:has(input[value=...]:checked)` to swap palette
// variables when a radio is ticked, and the matching `dark:` Tailwind
// variant in styles.css mirrors the same trigger. JS only handles what
// CSS can't: reading + writing localStorage, and keeping the data-theme
// attribute on <html> in sync so the variant works after htmx swaps.

const STORAGE_KEY = "theme";

function syncDataTheme(value: string): void {
  // Explicit choices map to a data-theme attribute the FOUC blocker can
  // pick up on the next reload. "system" clears the attribute so DaisyUI's
  // --prefersdark modifier (CSS-only) takes over.
  if (value === "light" || value === "dark") {
    document.documentElement.setAttribute("data-theme", value);
  } else {
    document.documentElement.removeAttribute("data-theme");
  }
}

document.addEventListener("DOMContentLoaded", () => {
  const stored = localStorage.getItem(STORAGE_KEY) ?? "system";
  const radio = document.querySelector<HTMLInputElement>(
    `input[name="theme"][value="${stored}"]`,
  );
  if (radio) radio.checked = true;
  syncDataTheme(stored);

  for (const input of document.querySelectorAll<HTMLInputElement>(
    'input[name="theme"]',
  )) {
    input.addEventListener("change", () => {
      if (input.checked) {
        localStorage.setItem(STORAGE_KEY, input.value);
        syncDataTheme(input.value);
      }
    });
  }
});
