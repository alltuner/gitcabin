# ABOUTME: Regression tests for render_markdown's HTML sanitization.
# ABOUTME: Verifies XSS vectors are stripped and legitimate content is preserved.

from __future__ import annotations

from gitcabin.web.code import render_markdown


def test_script_tag_stripped() -> None:
    result = render_markdown("# Hi\n<script>alert(1)</script>")
    assert "<script" not in result
    assert "alert(1)" not in result


def test_javascript_uri_stripped_from_href() -> None:
    result = render_markdown("[x](javascript:alert(2))")
    assert "javascript:" not in result


def test_data_uri_stripped_from_href() -> None:
    result = render_markdown("[x](data:text/html,<h1>hi</h1>)")
    assert "data:" not in result


def test_inline_event_handler_stripped() -> None:
    result = render_markdown('<p onclick="alert(3)">text</p>')
    assert "onclick" not in result


def test_heading_preserved() -> None:
    result = render_markdown("# Hello")
    assert "<h1" in result
    assert "Hello" in result


def test_code_block_preserved() -> None:
    result = render_markdown("```python\nprint('hi')\n```")
    assert "<code" in result
    assert "print" in result


def test_table_preserved() -> None:
    result = render_markdown("| A | B |\n|---|---|\n| 1 | 2 |")
    assert "<table" in result
    assert "<td" in result


def test_https_link_preserved() -> None:
    result = render_markdown("[example](https://example.com)")
    assert 'href="https://example.com"' in result


def test_mailto_link_preserved() -> None:
    result = render_markdown("[email](mailto:user@example.com)")
    assert 'href="mailto:user@example.com"' in result
