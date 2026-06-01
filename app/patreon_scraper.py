"""FlareSolverr-backed Patreon posts scraper.

Fetches posts from the Patreon JSON API via FlareSolverr (which solves
Cloudflare managed challenges that block bare HTTP requests from datacenter
IPs). Designed as a drop-in replacement for patreon-dl when Cloudflare
challenge-walls the VPS IP.

Flow per creator:
  1. Fetch the campaign page via FlareSolverr to extract the campaign_id.
  2. Paginate the /api/posts JSON endpoint (with auth cookies) via FlareSolverr.
  3. Stop paginating once a full page of already-known post IDs is found.
  4. Return raw wrapped payloads compatible with parser.normalize_post.

Note: FlareSolverr *sessions* (a warm browser kept across requests) are NOT
used. A session ignores the per-request auth cookies, so /api/posts returns
403; each request is issued statelessly with the cookies attached.
"""
from __future__ import annotations

import json
import re
import time
from pathlib import Path
from typing import Callable

import requests

from app.errors import AuthError

_FLARESOLVERR_URL = "http://flaresolverr:8191/v1"

_POST_FIELDS = (
    "title,content,content_json_string,url,published_at,"
    "current_user_can_view,is_paid,embed,post_type"
)

# Retry transient FlareSolverr failures (timeouts, connection blips, an
# occasional unsolved challenge). Auth failures are never retried.
_MAX_RETRIES = 3
_BACKOFF_BASE_SECONDS = 5


def _cookie_list(cookie_str: str) -> list[dict]:
    result = []
    for part in cookie_str.split(";"):
        part = part.strip()
        if "=" in part:
            k, v = part.split("=", 1)
            result.append({"name": k.strip(), "value": v.strip()})
    return result


def _fs_post(payload: dict, timeout_s: float) -> dict:
    """Low-level FlareSolverr POST. Raises RuntimeError if unreachable."""
    try:
        resp = requests.post(_FLARESOLVERR_URL, json=payload, timeout=timeout_s)
        resp.raise_for_status()
        return resp.json()
    except requests.ConnectionError as exc:
        raise RuntimeError(
            "Cannot reach FlareSolverr at flaresolverr:8191 — is the container running?"
        ) from exc


def _fs_get_once(url: str, cookies: list[dict], timeout_ms: int) -> dict:
    """Issue a single GET through FlareSolverr, return the solution dict."""
    payload = {
        "cmd": "request.get",
        "url": url,
        "maxTimeout": timeout_ms,
        "cookies": cookies,
    }
    result = _fs_post(payload, timeout_ms / 1000 + 30)

    if result.get("status") != "ok":
        raise RuntimeError(f"FlareSolverr: {result.get('message', result.get('status'))}")

    solution = result["solution"]
    http_status = solution.get("status")
    body = solution.get("response", "")

    if http_status == 401 or "patreon.com/login" in body.lower():
        raise AuthError("Patreon returned 401 / login redirect — PATREON_COOKIE is expired")
    if http_status == 403:
        if "cf-mitigated" in body or "just a moment" in body.lower():
            raise RuntimeError("Cloudflare challenge not solved by FlareSolverr")
        raise AuthError("Patreon returned 403 — PATREON_COOKIE may be expired or invalid")
    if http_status != 200:
        raise RuntimeError(f"Patreon returned HTTP {http_status} for {url}")

    return solution


def _fs_get(url: str, cookies: list[dict], timeout_ms: int = 60000) -> dict:
    """GET through FlareSolverr with retry/backoff on transient failures.

    AuthError (expired/invalid cookie) is re-raised immediately — retrying
    cannot fix it. RuntimeError and request errors are retried.
    """
    last_exc: Exception | None = None
    for attempt in range(_MAX_RETRIES):
        try:
            return _fs_get_once(url, cookies, timeout_ms)
        except AuthError:
            raise
        except (requests.RequestException, RuntimeError) as exc:
            last_exc = exc
            if attempt < _MAX_RETRIES - 1:
                time.sleep(_BACKOFF_BASE_SECONDS * (2 ** attempt))
    assert last_exc is not None
    raise last_exc


def _extract_json_body(html: str) -> str:
    """Strip browser-rendered HTML wrapper from a JSON API response."""
    # Chrome renders bare JSON as <html><body><pre>...</pre></body></html>
    m = re.search(r"<pre[^>]*>(.*)</pre>", html, re.DOTALL)
    if m:
        return m.group(1)
    # Fallback: find outermost JSON object/array
    start = html.find("{")
    end = html.rfind("}")
    if start != -1 and end > start:
        return html[start : end + 1]
    raise ValueError("Cannot locate JSON in FlareSolverr response body")


def _included_for_post(post: dict, page_included: list[dict]) -> list[dict]:
    """Return only the `included` items this post references.

    Patreon's /api/posts response returns one shared `included` array for the
    whole page. Each post links to its own items via relationships[*].data
    ({type, id} references). Returning the whole page array per post would make
    parser._normalize_json_api attribute every post's tags to every post and
    bloat each row's raw_json, so filter down to this post's references.
    """
    by_key = {(it.get("type"), str(it.get("id"))): it for it in page_included}
    result: list[dict] = []
    seen: set[tuple] = set()
    for rel in (post.get("relationships") or {}).values():
        data = rel.get("data") if isinstance(rel, dict) else None
        refs = [data] if isinstance(data, dict) else data if isinstance(data, list) else []
        for ref in refs:
            if not isinstance(ref, dict):
                continue
            key = (ref.get("type"), str(ref.get("id")))
            if key in by_key and key not in seen:
                result.append(by_key[key])
                seen.add(key)
    return result


def get_campaign_id(vanity: str, cookies: list[dict]) -> str:
    """Fetch the creator's posts page and extract campaign_id from __NEXT_DATA__."""
    url = f"https://www.patreon.com/c/{vanity}/posts"
    solution = _fs_get(url, cookies)
    html = solution["response"]

    m = re.search(r'<script[^>]+id="__NEXT_DATA__"[^>]*>(.*?)</script>', html, re.DOTALL)
    if not m:
        raise RuntimeError(f"__NEXT_DATA__ not found on {url}")

    nd = json.loads(m.group(1))
    camp_data = (
        nd.get("props", {})
        .get("pageProps", {})
        .get("bootstrapEnvelope", {})
        .get("pageBootstrap", {})
        .get("campaign", {})
        .get("data", {})
    )
    campaign_id = str(camp_data.get("id", "")).strip()
    if not campaign_id:
        raise RuntimeError(f"Could not extract campaign_id from {url}")
    return campaign_id


def _fetch_posts_page(
    campaign_id: str, cookies: list[dict], cursor: str | None = None
) -> dict:
    """Fetch one page of posts from the Patreon JSON API."""
    params = (
        f"filter[campaign_id]={campaign_id}"
        "&filter[contains_exclusive_posts]=true"
        "&sort=-published_at"
        f"&fields[post]={_POST_FIELDS}"
        "&include=post_tags"
        "&fields[post_tag]=value"
        "&page[count]=50"
    )
    if cursor:
        params += f"&page[cursor]={cursor}"

    url = f"https://www.patreon.com/api/posts?{params}"
    solution = _fs_get(url, cookies)
    return json.loads(_extract_json_body(solution["response"]))


def scrape_creator(
    vanity: str,
    known_post_ids: set[str],
    on_progress: Callable[[str], None] | None = None,
) -> list[dict]:
    """Fetch all new posts for a creator.

    Returns a list of dicts, each in the same JSON-API envelope format that
    patreon-dl writes to post-api.json, so parser.normalize_post can process
    them unchanged.

    Stops paginating once a full page is entirely within known_post_ids.
    """
    from app.config import settings

    cookie_str = settings.patreon_cookie
    if not cookie_str:
        raise AuthError("PATREON_COOKIE is not configured in .env")
    cookies = _cookie_list(cookie_str)

    if on_progress:
        on_progress(f"Resolving campaign ID for '{vanity}'…")
    campaign_id = get_campaign_id(vanity, cookies)
    if on_progress:
        on_progress(f"Campaign ID: {campaign_id}")

    all_posts: list[dict] = []
    cursor: str | None = None
    page = 1

    while True:
        if on_progress:
            on_progress(f"Fetching posts page {page}…")

        raw = _fetch_posts_page(campaign_id, cookies, cursor)
        posts = raw.get("data", [])
        page_included = raw.get("included", [])

        if not posts:
            break

        if on_progress:
            on_progress(f"Page {page}: {len(posts)} posts")

        for post in posts:
            # Wrap in the same envelope as post-api.json (with only this
            # post's own included items) so normalize_post works.
            all_posts.append({"data": post, "included": _included_for_post(post, page_included)})

        # Incremental stop: all posts on this page already imported → nothing older to find.
        page_ids = {str(p.get("id", "")) for p in posts}
        if page_ids and page_ids.issubset(known_post_ids):
            if on_progress:
                on_progress(f"All posts on page {page} already known — done")
            break

        next_cursor = (
            raw.get("meta", {}).get("pagination", {}).get("cursors", {}).get("next")
        )
        if not next_cursor:
            break

        cursor = next_cursor
        page += 1

    return all_posts


def source_path_for(vanity: str, post_id: str) -> Path:
    """Virtual source_path for API-scraped posts (no real file needed)."""
    return Path(f"patreon_api/{vanity}/{post_id}")
