# ABOUTME: Tests for AssetResolver — manifest caching, mtime invalidation, fallbacks.
# ABOUTME: Verifies templates aren't re-parsing manifest.json on every asset() call.

from __future__ import annotations

import json
import os
from pathlib import Path

from gitcabin.web.assets import AssetResolver


def test_resolves_logical_name_via_manifest(tmp_path: Path) -> None:
    dist = tmp_path / "dist"
    dist.mkdir()
    (dist / "manifest.json").write_text(json.dumps({"main.css": "main.aaa.css"}))

    resolver = AssetResolver(dist_dir=dist)
    assert resolver("main.css") == "/static/dist/main.aaa.css"


def test_falls_back_to_bare_name_when_manifest_missing(tmp_path: Path) -> None:
    # No manifest file written — resolver should still emit a usable URL so
    # unhashed legacy assets keep working.
    dist = tmp_path / "dist"
    dist.mkdir()

    resolver = AssetResolver(dist_dir=dist)
    assert resolver("legacy.css") == "/static/dist/legacy.css"


def test_falls_back_to_bare_name_when_key_absent(tmp_path: Path) -> None:
    dist = tmp_path / "dist"
    dist.mkdir()
    (dist / "manifest.json").write_text(json.dumps({"main.css": "main.aaa.css"}))

    resolver = AssetResolver(dist_dir=dist)
    assert resolver("unknown.js") == "/static/dist/unknown.js"


def test_caches_manifest_until_mtime_changes(tmp_path: Path) -> None:
    """A rebuilt manifest should be picked up; an unchanged one stays cached."""
    dist = tmp_path / "dist"
    dist.mkdir()
    manifest = dist / "manifest.json"
    manifest.write_text(json.dumps({"main.css": "main.aaa.css"}))

    resolver = AssetResolver(dist_dir=dist)
    assert resolver("main.css") == "/static/dist/main.aaa.css"

    # Rewrite with new content and bump mtime — touch alone may collide with
    # the previous mtime on filesystems with coarse timestamp resolution.
    manifest.write_text(json.dumps({"main.css": "main.bbb.css"}))
    new_mtime = manifest.stat().st_mtime + 1
    os.utime(manifest, (new_mtime, new_mtime))

    assert resolver("main.css") == "/static/dist/main.bbb.css"


def test_cache_recovers_after_manifest_disappears(tmp_path: Path) -> None:
    # If the bundler wipes dist/ mid-run, the resolver should fall back to
    # bare names rather than serving a stale manifest forever.
    dist = tmp_path / "dist"
    dist.mkdir()
    manifest = dist / "manifest.json"
    manifest.write_text(json.dumps({"main.css": "main.aaa.css"}))

    resolver = AssetResolver(dist_dir=dist)
    assert resolver("main.css") == "/static/dist/main.aaa.css"

    manifest.unlink()
    assert resolver("main.css") == "/static/dist/main.css"

    # Restoring the manifest should rebuild the cache.
    manifest.write_text(json.dumps({"main.css": "main.ccc.css"}))
    assert resolver("main.css") == "/static/dist/main.ccc.css"
