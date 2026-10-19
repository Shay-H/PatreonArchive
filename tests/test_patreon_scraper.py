"""Unit tests for app.patreon_scraper pure helpers.

Network paths (FlareSolverr calls) are not exercised here; these cover the
per-post `included` filtering that prevents tag cross-contamination.
"""
from pathlib import Path

import pytest

import app.patreon_scraper as scraper
from app.errors import AuthError
from app.parser import normalize_post
from app.patreon_scraper import _fetch_posts_page, _included_for_post, download_attachment


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
            "user_defined_tags": {"data": [{"type": "post_tag", "id": t} for t in tag_ids]}
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
            "user_defined_tags": {"data": [{"type": "post_tag", "id": "t1"}]},
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


# ---------------------------------------------------------------------------
# Posts query requests attachments
# ---------------------------------------------------------------------------

def test_posts_query_requests_attachment_fields(monkeypatch):
    captured = {}

    def fake_fs_get(url, cookies, *a, **k):
        captured["url"] = url
        return {"response": "<pre>{\"data\": []}</pre>"}

    monkeypatch.setattr(scraper, "_fs_get", fake_fs_get)
    _fetch_posts_page("camp1", [])
    url = captured["url"]
    assert "include=user_defined_tags,attachments_media,attachments" in url
    assert "fields[media]=download_url,file_name,mimetype,size_bytes,state" in url


# ---------------------------------------------------------------------------
# download_attachment
# ---------------------------------------------------------------------------

class _FakeResp:
    def __init__(self, *, status=200, headers=None, chunks=(b"data",), text=""):
        self.status_code = status
        self.headers = headers or {}
        self._chunks = chunks
        self.text = text

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def raise_for_status(self):
        if self.status_code >= 400:
            raise RuntimeError(f"HTTP {self.status_code}")

    def iter_content(self, chunk_size=65536):
        return iter(self._chunks)


def test_download_attachment_streams_to_disk(monkeypatch, tmp_path):
    monkeypatch.setattr(
        scraper.requests, "get",
        lambda *a, **k: _FakeResp(chunks=(b"hello ", b"world")),
    )
    dest = tmp_path / "sub" / "file.pdf"
    path, size = download_attachment("https://cdn/x", dest, "session_id=abc")
    assert path == dest
    assert dest.read_bytes() == b"hello world"
    assert size == 11
    assert not dest.with_suffix(".pdf.part").exists()  # temp file cleaned up


def test_download_attachment_auth_error(monkeypatch, tmp_path):
    monkeypatch.setattr(scraper.requests, "get", lambda *a, **k: _FakeResp(status=403))
    with pytest.raises(AuthError):
        download_attachment("https://cdn/x", tmp_path / "f.bin", "c=1")


def test_download_attachment_cloudflare_fallback(monkeypatch, tmp_path):
    calls = {"n": 0}

    def fake_get(url, **k):
        calls["n"] += 1
        if url == "https://www.patreon.com/file?h=1":
            # First hit: a Cloudflare challenge page.
            return _FakeResp(
                status=403,
                headers={"content-type": "text/html"},
                text="<html>Just a moment... cf-mitigated</html>",
            )
        return _FakeResp(chunks=(b"PDFDATA",))  # resolved CDN URL streams fine

    monkeypatch.setattr(scraper.requests, "get", fake_get)
    monkeypatch.setattr(
        scraper, "_resolve_via_flaresolverr",
        lambda url, cookies: "https://cdn.patreon.com/resolved",
    )
    dest = tmp_path / "f.pdf"
    path, size = download_attachment("https://www.patreon.com/file?h=1", dest, "c=1")
    assert dest.read_bytes() == b"PDFDATA"
    assert calls["n"] == 2  # challenge, then resolved download


# ---------------------------------------------------------------------------
# FlareSolverr error reporting
# ---------------------------------------------------------------------------

class _Resp:
    """Minimal stand-in for requests.Response."""

    def __init__(self, status_code: int, payload=None, text: str = ""):
        self.status_code = status_code
        self._payload = payload
        self.text = text

    def json(self):
        if self._payload is None:
            raise ValueError("no json")
        return self._payload


def test_fs_post_surfaces_flaresolverr_message(monkeypatch):
    """A failed solve must report its reason, not a bare 'HTTP 500 for url'."""
    monkeypatch.setattr(
        scraper.requests,
        "post",
        lambda *a, **kw: _Resp(500, {
            "status": "error",
            "message": "Error: Error solving the challenge. Timeout after 60.0 seconds.",
        }),
    )
    with pytest.raises(RuntimeError) as exc:
        scraper._fs_post({"cmd": "request.get"}, 10)
    assert "Timeout after 60.0 seconds" in str(exc.value)


def test_fs_post_falls_back_to_body_text(monkeypatch):
    monkeypatch.setattr(
        scraper.requests, "post", lambda *a, **kw: _Resp(502, None, text="bad gateway")
    )
    with pytest.raises(RuntimeError) as exc:
        scraper._fs_post({"cmd": "request.get"}, 10)
    assert "502" in str(exc.value) and "bad gateway" in str(exc.value)


def test_fs_get_explains_a_persistent_challenge_timeout(monkeypatch):
    """After exhausting retries, the operator gets the cause, not the raw error."""
    def _boom(*a, **kw):
        raise RuntimeError("FlareSolverr HTTP 500: Error solving the challenge. Timeout after 60.0 seconds.")

    monkeypatch.setattr(scraper, "_fs_get_once", _boom)
    monkeypatch.setattr(scraper.time, "sleep", lambda *_: None)
    with pytest.raises(RuntimeError) as exc:
        scraper._fs_get("https://www.patreon.com/c/czepeku/posts", [])
    assert "could not clear Cloudflare" in str(exc.value)
