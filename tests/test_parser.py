"""Unit tests for app.parser.normalize_post.

normalize_post is the seam both the patreon-dl import path and the
FlareSolverr scraper feed into, so its JSON-API handling is covered here.
"""
from datetime import datetime, timezone
from pathlib import Path

from app.parser import normalize_post


def _envelope(attrs: dict, post_id: str = "123", included: list | None = None) -> dict:
    return {
        "data": {
            "id": post_id,
            "type": "post",
            "attributes": attrs,
            "relationships": {},
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
