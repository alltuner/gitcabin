# Palette

gitcabin uses a four-color cabin palette. The hexes are anchored; everything else (Tailwind ramps, dark-mode behavior, semantic roles) is derived from them.

## Anchors

| Token           | Hex       | HSL                 | Role                                    |
| --------------- | --------- | ------------------- | --------------------------------------- |
| `dark-wine`     | `#7b1818` | `hsl(0 67% 29%)`    | destructive, error, closed-as-not-done  |
| `amber-honey`   | `#e5ad00` | `hsl(45 100% 45%)`  | highlight, focus, active-tab, unread    |
| `fern`          | `#597a48` | `hsl(100 26% 38%)`  | accent, links, open issues, primary CTA |
| `charcoal-blue` | `#233d4d` | `hsl(203 37% 22%)`  | neutral surface + ink (replaces `gray`) |

## Where it lives

The palette is defined as Tailwind 4 theme tokens in [`web-src/src/styles.css`](../web-src/src/styles.css) under an `@theme` block. Each anchor expands to an 11-step ramp (`-50` … `-950`) so utilities like `bg-fern-100`, `text-charcoal-blue-900`, `border-dark-wine-600` all work directly.

The default Tailwind `gray` ramp is **retargeted** to the charcoal-blue values inside the same `@theme` block. That way every existing `bg-gray-*` / `text-gray-*` / `border-gray-*` utility picks up the cabin neutral with no per-template churn. Reading templates, `gray` and `charcoal-blue` are interchangeable.

## Semantic mapping

When picking a class for new markup, prefer the role over the anchor name:

- **Links / accent / open-state** → `fern-700` light, `fern-400` dark.
- **Closed / merged / completed** → `dark-wine-600` / `dark-wine-700`.
- **Active tab / focus ring / highlight** → `amber-honey-500`.
- **Surfaces / ink / borders** → `gray-*` (= charcoal-blue) ramp; pick step by elevation, same convention as Tailwind defaults.
- **Destructive button / error text** → `dark-wine-600` / `dark-wine-700`.

Examples in tree:

- Internal links — `text-fern-700 hover:underline dark:text-fern-400`
- Open-issue dot, default-branch badge — `text-fern-600`, `bg-fern-100 text-fern-800`
- Closed-issue dot — `text-dark-wine-600`, `bg-dark-wine-700 text-white`
- Active repo tab — `border-b-2 border-amber-honey-500`
- Comment / submit button — `bg-fern-700 hover:bg-fern-800`
- Form focus ring — `focus:ring-fern-500`

## Dark mode

The palette is theme-agnostic; light vs. dark is selected via Tailwind's `dark:` variant on a class basis (`<html class="dark">`), not `prefers-color-scheme`. The theme switcher in `_base.html` toggles that class; `localStorage.theme` persists `light` / `dark` / `system`.

Custom CSS that renders inside dark mode (e.g. the pygments `.hl` blocks and the `table.blame` styles in `styles.css`) uses `:where(.dark) …` selectors so it stays in lockstep with the toggle. Don't introduce new `@media (prefers-color-scheme: dark)` rules — they desync from the switcher.

## Other formats

For tools that want the palette in a different shape:

```
Dark Wine     #7b1818  rgb(123, 24, 24)   hsl(0   67% 29%)
Amber Honey   #e5ad00  rgb(229, 173, 0)   hsl(45  100% 45%)
Fern          #597a48  rgb(89, 122, 72)   hsl(100 26% 38%)
Charcoal Blue #233d4d  rgb(35, 61, 77)    hsl(203 37% 22%)
```

CSV: `7b1818,e5ad00,597a48,233d4d`

JSON: `{"Dark Wine":"7b1818","Amber Honey":"e5ad00","Fern":"597a48","Charcoal Blue":"233d4d"}`
