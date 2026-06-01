"""Unit tests for app.patreon_scraper pure helpers.

Network paths (FlareSolverr calls) are not exercised here; these cover the
per-post `included` filtering that prevents tag cross-contamination.
"""
from pathlib import Path

from app.parser import normalize_post
from app.patreon_scraper import _included_for_post


_PAGE_INCLUDED = [
    {"type": "post_tag", "id": "t1", "attributes": {"value": "Action"}},
    {"type": "post_tag", "id": "t2", "attributes": {"value": "Horror"}},
]


def _post(post_id: str, tag_ids: list[str]) -> dict:
    return {
        "id": post_id,
        "type": "post",
        "attributes": {"title": f"Post {post_id}"},
        "relationships": {
            "post_tags": {"data": [{"type": "post_tag", "id": t} for t in tag_ids]}
        },
    }


def test_included_for_post_isolates_items():
    inc_a = _included_for_post(_post("1", ["t1"]), _PAGE_INCLUDED)
    inc_b = _included_for_post(_post("2", ["t2"]), _PAGE_INCLUDED)
    assert [i["id"] for i in inc_a] == ["t1"]
    assert [i["id"] for i in inc_b] == ["t2"]


def test_included_for_post_handles_missing_relationships():
    assert _included_for_post({"id": "1"}, _PAGE_INCLUDED) == []
    assert _included_for_post({"id": "1", "relationships": {}}, _PAGE_INCLUDED) == []


def test_included_for_post_deduplicates():
    post = {
        "relationships": {
            "post_tags": {"data": [{"type": "post_tag", "id": "t1"}]},
            "also_tags": {"data": [{"type": "post_tag", "id": "t1"}]},
        }
    }
    assert [i["id"] for i in _included_for_post(post, _PAGE_INCLUDED)] == ["t1"]


def test_no_tag_cross_contamination_end_to_end():
    """A post on a multi-post page must get only its own tags, not the page's."""
    post = _post("1", ["t1"])
    envelope = {"data": post, "included": _included_for_post(post, _PAGE_INCLUDED)}
    parsed = normalize_post(Path("patreon_api/x/1"), envelope)
    assert parsed["tags"] == ["action"]  # NOT ["action", "horror"]
