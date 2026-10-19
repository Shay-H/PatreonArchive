"""Unit tests for app.parser.normalize_post.

normalize_post is the seam both the patreon-dl import path and the
FlareSolverr scraper feed into, so its JSON-API handling is covered here.
"""
import json
from datetime import datetime, timezone
from pathlib import Path

from app.parser import classify_attachment, is_patreon_url, normalize_post


def _envelope(
    attrs: dict,
    post_id: str = "123",
    included: list | None = None,
    relationships: dict | None = None,
) -> dict:
    return {
        "data": {
            "id": post_id,
            "type": "post",
            "attributes": attrs,
            "relationships": relationships or {},
        },
        "included": included or [],
    }


def test_json_api_basic_fields():
    parsed = normalize_post(
        Path("patreon_api/x/123"),
        _envelope({
            "title": "Test Post",
            "content": "<p>Hello world</p>",
            "url": "https://www.patreon.com/posts/test-123",
            "published_at": "2026-01-02T03:04:05.000+00:00",
            "current_user_can_view": True,
        }),
    )
    assert parsed["post_id"] == "123"
    assert parsed["title"] == "Test Post"
    assert "Hello world" in parsed["content"]
    assert parsed["post_url"] == "https://www.patreon.com/posts/test-123"
    assert parsed["published_at"] == datetime(2026, 1, 2, 3, 4, 5, tzinfo=timezone.utc)
    assert parsed["can_view"] is True


def test_locked_post_can_view_false():
    parsed = normalize_post(
        Path("p/1"),
        _envelope({"title": "Locked", "current_user_can_view": False}),
    )
    assert parsed["can_view"] is False


def test_untitled_falls_back():
    parsed = normalize_post(Path("p/1"), _envelope({"content": "body only"}))
    assert parsed["title"] == "(untitled)"


def test_content_html_preserved():
    """Content-mode bodies must keep their HTML (sanitized later, not at parse)."""
    parsed = normalize_post(
        Path("p/1"),
        _envelope({"title": "Essay", "content": "<p>Hi <b>there</b></p>"}),
    )
    assert parsed["content"] == "<p>Hi <b>there</b></p>"


def test_attachments_modern_media_reference():
    parsed = normalize_post(
        Path("p/1"),
        _envelope(
            {"title": "With file"},
            relationships={
                "attachments_media": {"data": [{"type": "media", "id": "9"}]},
            },
            included=[
                {
                    "type": "media",
                    "id": "9",
                    "attributes": {
                        "download_url": "https://www.patreon.com/file?h=9",
                        "file_name": "book.epub",
                        "mimetype": "application/epub+zip",
                        "size_bytes": 2048,
                    },
                },
            ],
        ),
    )
    assert parsed["attachments"] == [
        {
            "filename": "book.epub",
            "mimetype": "application/epub+zip",
            "remote_url": "https://www.patreon.com/file?h=9",
            "size_bytes": 2048,
            "kind": "ebook",
        }
    ]


def test_attachments_legacy_attachment_reference():
    parsed = normalize_post(
        Path("p/1"),
        _envelope(
            {"title": "With legacy file"},
            relationships={
                "attachments": {"data": [{"type": "attachment", "id": "5"}]},
            },
            included=[
                {
                    "type": "attachment",
                    "id": "5",
                    "attributes": {"name": "notes.pdf", "url": "https://www.patreon.com/file?h=5"},
                },
            ],
        ),
    )
    assert len(parsed["attachments"]) == 1
    att = parsed["attachments"][0]
    assert att["filename"] == "notes.pdf"
    assert att["remote_url"] == "https://www.patreon.com/file?h=5"
    assert att["kind"] == "pdf"


def test_attachments_empty_when_none_referenced():
    parsed = normalize_post(Path("p/1"), _envelope({"title": "No files"}))
    assert parsed["attachments"] == []


def test_classify_attachment_categories():
    assert classify_attachment("a.pdf", None) == "pdf"
    assert classify_attachment(None, "application/pdf") == "pdf"
    assert classify_attachment("data.json", None) == "json"
    assert classify_attachment("book.epub", None) == "ebook"
    assert classify_attachment("notes.txt", "text/plain") == "text"
    assert classify_attachment("pic.png", None) == "image"
    assert classify_attachment("thing.bin", None) == "other"


def test_tags_extracted_lowercased_and_sorted():
    parsed = normalize_post(
        Path("p/1"),
        _envelope(
            {"title": "Tagged"},
            included=[
                {"type": "post_tag", "id": "t2", "attributes": {"value": "Sci-Fi"}},
                {"type": "post_tag", "id": "t1", "attributes": {"value": "Action"}},
            ],
        ),
    )
    assert parsed["tags"] == ["action", "sci-fi"]


# ---------------------------------------------------------------------------
# Link extraction (links-mode archives live or die on this)
# ---------------------------------------------------------------------------

def test_links_from_prosemirror_link_marks():
    """A hyperlink's URL lives only in the mark, never in the text."""
    doc = json.dumps({
        "type": "doc",
        "content": [{
            "type": "paragraph",
            "content": [{
                "type": "text",
                "text": "Download the map pack",
                "marks": [{"type": "link", "attrs": {"href": "https://drive.google.com/d/abc"}}],
            }],
        }],
    })
    parsed = normalize_post(
        Path("p/1"), _envelope({"title": "Maps", "content_json_string": doc})
    )
    assert parsed["links"] == ["https://drive.google.com/d/abc"]


def test_links_from_rendered_html_hrefs():
    parsed = normalize_post(
        Path("p/1"),
        _envelope({
            "title": "Maps",
            "content": '<p>Grab it <a href="https://dropbox.com/s/pack.zip">here</a></p>',
        }),
    )
    assert parsed["links"] == ["https://dropbox.com/s/pack.zip"]


def test_links_merge_every_source_and_dedupe():
    doc = json.dumps({
        "type": "doc",
        "content": [{
            "type": "paragraph",
            "content": [{
                "type": "text",
                "text": "mirror",
                "marks": [{"type": "link", "attrs": {"href": "https://dropbox.com/s/pack.zip"}}],
            }],
        }],
    })
    parsed = normalize_post(
        Path("p/1"),
        _envelope({
            "title": "Maps",
            "content": '<p>See <a href="https://dropbox.com/s/pack.zip">here</a> '
                       'or https://example.com/mirror</p>',
            "content_json_string": doc,
            "embed": {"url": "https://youtube.com/watch?v=abc"},
        }),
    )
    assert parsed["links"] == [
        "https://dropbox.com/s/pack.zip",
        "https://example.com/mirror",
        "https://youtube.com/watch?v=abc",
    ]


def test_links_strip_trailing_sentence_punctuation():
    parsed = normalize_post(
        Path("p/1"),
        _envelope({"title": "x", "content": "Get it at https://example.com/pack."}),
    )
    assert parsed["links"] == ["https://example.com/pack"]


def test_links_ignore_non_http_schemes():
    parsed = normalize_post(
        Path("p/1"),
        _envelope({
            "title": "x",
            "content": '<a href="mailto:me@example.com">mail</a>'
                       '<a href="/posts/123">relative</a>',
        }),
    )
    assert parsed["links"] == []


def test_is_patreon_url_covers_subdomains_only():
    assert is_patreon_url("https://www.patreon.com/posts/123")
    assert is_patreon_url("https://c10.patreonusercontent.com/4/img.png")
    assert not is_patreon_url("https://drive.google.com/d/abc")
    assert not is_patreon_url("https://notpatreon.com/x")
