# ABOUTME: Manifest-backed resolver for content-hashed static bundles produced by bun.
# ABOUTME: Used as a Jinja2 global so templates write asset('main.css'), not bare filenames.

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path

# /static is mounted by routes.mount_static onto src/gitcabin/web/static. The
# bundler writes its hashed output under static/dist/, so the URL prefix the
# template emits is always /static/dist/<file>.
STATIC_DIST_PREFIX = "/static/dist/"


@dataclass(frozen=True, slots=True)
class AssetResolver:
    """Reads bun's manifest.json once and resolves logical names to URLs.

    Templates use this via a Jinja global:

        <link rel="stylesheet" href="{{ asset('main.css') }}">

    Logical names (`main.css`, `main.js`) are stable across builds; the
    hashed filename behind each one changes whenever the content does.
    Browsers cache aggressively because the URL itself is the version.
    """

    dist_dir: Path

    def __call__(self, name: str) -> str:
        manifest = self._manifest()
        # Fall back to the bare name if the manifest doesn't know about it —
        # that lets us serve unhashed legacy assets out of static/ while we
        # migrate templates over to bundled equivalents.
        return STATIC_DIST_PREFIX + manifest.get(name, name)

    def _manifest(self) -> dict[str, str]:
        # Read the manifest fresh on every call. The cost is one ~100B file
        # read per page render — well below the noise floor — and it means
        # rebuilding the bundle while the server is running picks up the new
        # hashes without a restart. Production deploys aren't churning enough
        # for this to matter.
        try:
            return json.loads((self.dist_dir / "manifest.json").read_text())
        except FileNotFoundError:
            return {}
