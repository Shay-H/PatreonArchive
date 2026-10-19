"""HTML sanitization for content-mode post bodies.

Patreon serves post bodies as rich HTML. For content creators we keep that
formatting, but it must be sanitized before it ever reaches the browser so a
malicious/compromised post can't inject scripts. Uses nh3 (a maintained Rust
sanitizer); if nh3 is unavailable we fall back to stripping all markup so we
never emit unsanitized HTML.
"""
from __future__ import annotations

import html as _html
import re

# Allowlist tuned for written content: headings, emphasis, lists, quotes,
# links, images, and code. Anything else (script, iframe, style, on* handlers)
# is dropped by nh3.
_ALLOWED_TAGS = {
    "p", "br", "hr", "span", "div",
    "a", "img",
    "ul", "ol", "li",
    "h1", "h2", "h3", "h4", "h5", "h6",
    "blockquote", "pre", "code",
    "strong", "b", "em", "i", "u", "s", "del",
    "table", "thead", "tbody", "tr", "th", "td",
}

# Note: do NOT list "rel" for <a> here — nh3 panics if it's both in the
# attribute allowlist and managed by link_rel below. link_rel adds it for us.
_ALLOWED_ATTRS = {
    "a": {"href", "title", "target"},
    "img": {"src", "alt", "title"},
}

try:
    import nh3  # type: ignore

    def sanitize_html(value: str | None) -> str:
        if not value:
            return ""
        return nh3.clean(
            value,
            tags=_ALLOWED_TAGS,
            attributes=_ALLOWED_ATTRS,
            link_rel="noopener noreferrer nofollow",
        )

except ImportError:  # pragma: no cover - exercised only without nh3 installed
    _TAG_RE = re.compile(r"<[^>]+>")

    def sanitize_html(value: str | None) -> str:
        if not value:
            return ""
        # No sanitizer available: degrade to escaped plain text rather than
        # risk emitting active markup.
        return _html.escape(_TAG_RE.sub("", value)).strip()
