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


def test_p_align_center_preserved() -> None:
    # GitHub-style hero blocks rely on <p align="center"> for the centered
    # logo + shields layout. Stripping it (the original allowlist did) made
    # local rendering visually drift from github.com.
    result = render_markdown('<p align="center">hi</p>')
    assert 'align="center"' in result


def test_img_width_height_preserved() -> None:
    # Real READMEs commonly write <img width="500"> on logos. Stripping it
    # left the image at its natural dimensions, which is usually too big.
    result = render_markdown(
        '<img src="https://example.com/x.png" alt="x" width="500" height="120">'
    )
    assert 'width="500"' in result
    assert 'height="120"' in result


def test_anchor_target_preserved() -> None:
    result = render_markdown('<a href="https://example.com" target="_blank">x</a>')
    assert 'target="_blank"' in result


def test_gfm_alert_title_class_preserved() -> None:
    # The _GfmAlertExtension emits <p class="markdown-alert-title">Warning</p>
    # so the dashboard's CSS can style the title row distinctly. The class
    # attribute on <p> must survive sanitization.
    result = render_markdown("> [!WARNING]\n> Watch out.")
    assert 'class="markdown-alert-title"' in result


def test_details_summary_preserved() -> None:
    # Collapsibles show up in real READMEs ("<details><summary>Click</summary>...").
    result = render_markdown("<details><summary>Click</summary>Hidden.</details>")
    assert "<details" in result
    assert "<summary" in result


def test_kbd_preserved() -> None:
    result = render_markdown("Press <kbd>Ctrl</kbd>.")
    assert "<kbd>" in result
