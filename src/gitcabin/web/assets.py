# ABOUTME: Manifest-backed resolver for content-hashed static bundles produced by bun.
# ABOUTME: Used as a Jinja2 global so templates write asset('main.css'), not bare filenames.

from __future__ import annotations

import json
from pathlib import Path

# /static is mounted by routes.mount_static onto src/gitcabin/web/static. The
# bundler writes its hashed output under static/dist/, so the URL prefix the
# template emits is always /static/dist/<file>.
STATIC_DIST_PREFIX = "/static/dist/"


class AssetResolver:
    """Reads bun's manifest.json and resolves logical names to URLs.

    Templates use this via a Jinja global:

        <link rel="stylesheet" href="{{ asset('main.css') }}">

    Logical names (`main.css`, `main.js`) are stable across builds; the
    hashed filename behind each one changes whenever the content does.
    Browsers cache aggressively because the URL itself is the version.

    The parsed manifest is cached on the instance and invalidated when the
    file's mtime changes — rebuilds during a running server still pick up
    new hashes without a restart, while warm renders avoid re-reading and
    re-parsing the file for every asset() call in a template.
    """

    __slots__ = ("dist_dir", "_cache")

    def __init__(self, dist_dir: Path) -> None:
        self.dist_dir = dist_dir
        self._cache: tuple[float, dict[str, str]] | None = None

    def __call__(self, name: str) -> str:
        manifest = self._manifest()
        # Fall back to the bare name if the manifest doesn't know about it —
        # that lets us serve unhashed legacy assets out of static/ while we
        # migrate templates over to bundled equivalents.
        return STATIC_DIST_PREFIX + manifest.get(name, name)

    def _manifest(self) -> dict[str, str]:
        path = self.dist_dir / "manifest.json"
        try:
            mtime = path.stat().st_mtime
        except FileNotFoundError:
            self._cache = None
            return {}
        if self._cache is None or self._cache[0] != mtime:
            self._cache = (mtime, json.loads(path.read_text()))
        return self._cache[1]
