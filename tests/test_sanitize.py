"""Tests for app.sanitize.sanitize_html.

The dangerous-content assertions hold in both modes (nh3 installed or the
strip-all fallback). The "allowed markup survives" check only applies when nh3
is present, so it is skipped otherwise.
"""
import pytest

from app.sanitize import sanitize_html

try:
    import nh3  # noqa: F401
    _HAS_NH3 = True
except ImportError:
    _HAS_NH3 = False


def test_empty_input():
    assert sanitize_html("") == ""
    assert sanitize_html(None) == ""


def test_script_is_removed():
    out = sanitize_html("<p>hi</p><script>alert(1)</script>")
    assert "<script" not in out
    assert "alert(1)" not in out


def test_event_handlers_and_js_urls_removed():
    out = sanitize_html('<a href="javascript:evil()" onclick="evil()">x</a>')
    assert "onclick" not in out
    assert "javascript:" not in out


@pytest.mark.skipif(not _HAS_NH3, reason="rich-HTML preservation requires nh3")
def test_allowed_markup_survives():
    out = sanitize_html("<p>Hello <strong>world</strong></p><h2>Heading</h2><ul><li>a</li></ul>")
    assert "<strong>" in out
    assert "<h2>" in out
    assert "<li>" in out


@pytest.mark.skipif(not _HAS_NH3, reason="rich-HTML preservation requires nh3")
def test_safe_link_kept_with_rel():
    out = sanitize_html('<a href="https://example.com">link</a>')
    assert 'href="https://example.com"' in out
