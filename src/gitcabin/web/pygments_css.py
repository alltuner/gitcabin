# ABOUTME: Standalone pygments-token CSS generator — no GitPython / FastAPI / gitcabin imports.
# ABOUTME: scripts/dump_pygments_css.py calls this from the bun docker stage where only pygments is installed.

from __future__ import annotations

import functools

from pygments.formatters import HtmlFormatter

_PYGMENTS_LIGHT_STYLE = "default"
_PYGMENTS_DARK_STYLE = "github-dark"

# Three CSS selector branches that must all carry the dark token rules,
# mirroring the @custom-variant dark in styles.css. Keep these in sync.
_DARK_TRIGGER_PREFIXES = (
    "[data-theme=dark] .hl",
    ":root:has(input.theme-controller[value=dark]:checked) .hl",
)
_DARK_MEDIA_PREFIX = (
    ":root:not([data-theme=light])"
    ":not(:has(input.theme-controller[value=light]:checked)) .hl"
)


def _scoped_style_defs(formatter: HtmlFormatter, prefix: str) -> str:
    """Return pygments rules whose selectors actually start with `prefix`.

    `HtmlFormatter.get_style_defs(prefix)` prefixes the `.hl`-scoped rules
    but leaves a few helpers unprefixed (`pre {}`, `td.linenos .normal {}`,
    `span.linenos {}`). Those bare rules carry the chosen style's hard-
    coded colors and bleed across themes (the dark style's `td.linenos`
    background ends up applying in light mode too). We drop any rule whose
    selector doesn't start with our prefix.
    """
    out: list[str] = []
    for line in formatter.get_style_defs(prefix).splitlines():
        stripped = line.lstrip()
        if not stripped or stripped.startswith(prefix):
            out.append(line)
    return "\n".join(out)


@functools.lru_cache(maxsize=1)
def pygments_stylesheet() -> str:
    """The CSS Pygments needs for the `.hl` class our formatter emits.

    Bundles two themes — a light one for default surfaces and a dark one
    that activates whenever the page is in dark mode (explicit data-theme,
    DaisyUI's theme-controller :has() selector, or prefers-color-scheme
    fallback in `system` mode). The dark rules are emitted with three
    different prefix selectors so they fire under each of those triggers.

    Cached at module level — output only changes when pygments itself is
    upgraded, so recomputing per request is wasted work.
    """
    chunks: list[str] = []
    light = HtmlFormatter(style=_PYGMENTS_LIGHT_STYLE, cssclass="hl")
    chunks.append(_scoped_style_defs(light, ".hl"))

    dark = HtmlFormatter(style=_PYGMENTS_DARK_STYLE, cssclass="hl")
    for prefix in _DARK_TRIGGER_PREFIXES:
        chunks.append(_scoped_style_defs(dark, prefix))
    chunks.append(
        f"@media (prefers-color-scheme: dark) {{\n"
        f"{_scoped_style_defs(dark, _DARK_MEDIA_PREFIX)}\n"
        f"}}"
    )
    return "\n\n".join(chunks)
