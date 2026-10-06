// ABOUTME: Entry point for the bundled JS — prefetches hovered links and persists the theme choice.
// ABOUTME: bun-bundled into ../../src/gitcabin/web/static/dist/main.<hash>.js.

// gitcabin's dashboard has no client-side framework: pages are server
// rendered and navigation is plain browser navigation. This file only
// adds the two things HTML and CSS can't do on their own.

// ---- link prefetch ---------------------------------------------------- //
// Hovering, focusing or touching a link inside a `data-prefetch` container
// adds a <link rel="prefetch"> for its URL, so the click that follows is a
// normal navigation served from the browser cache. `_render` sets
// `Cache-Control: private, max-age=10` so the prefetched page is reusable.

const prefetched = new Set<string>();

function prefetchLink(event: Event): void {
  if (!(event.target instanceof Element)) return;
  const link = event.target.closest<HTMLAnchorElement>("[data-prefetch] a[href]");
  if (!link || link.hasAttribute("download")) return;
  if (link.target && link.target !== "_self") return;
  const connection = (navigator as { connection?: { saveData?: boolean } }).connection;
  if (connection?.saveData) return;

  const url = new URL(link.href);
  if (url.origin !== location.origin) return;
  // Hash-only links point at the page already loaded.
  if (url.pathname === location.pathname && url.search === location.search) return;
  const target = url.pathname + url.search;
  if (prefetched.has(target)) return;
  prefetched.add(target);

  const hint = document.createElement("link");
  hint.rel = "prefetch";
  hint.href = target;
  document.head.append(hint);
}

for (const type of ["mouseover", "focusin", "touchstart"]) {
  document.addEventListener(type, prefetchLink, { passive: true });
}

// ---- theme persistence ------------------------------------------------ //
// The theme switcher itself is browser-native: DaisyUI's theme-controller
// pattern uses CSS `:root:has(input[value=...]:checked)` to swap palette
// variables when a radio is ticked, and the matching `dark:` Tailwind
// variant in styles.css mirrors the same trigger. JS only handles what
// CSS can't: reading + writing localStorage, and keeping the data-theme
// attribute on <html> in sync so the variant follows the stored choice.

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
