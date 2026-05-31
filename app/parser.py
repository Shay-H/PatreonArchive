from __future__ import annotations

import json
import re
from datetime import datetime
from pathlib import Path
from urllib.parse import urlparse

URL_REGEX = re.compile(r"https?://[^\s)\]>'\"]+")


def parse_post_info_file(path: Path) -> dict:
    text = path.read_text(encoding="utf-8", errors="ignore").strip()
    if not text:
        return {}

    if text.startswith("{") or text.startswith("["):
        try:
            payload = json.loads(text)
            if isinstance(payload, dict):
                return payload
            return {"value": payload}
        except json.JSONDecodeError:
            pass

    return {"raw_text": text}


def _first_string(*values: object) -> str:
    for value in values:
        if isinstance(value, str) and value.strip():
            return value.strip()
    return ""


def _parse_datetime(value: object) -> datetime | None:
    if not isinstance(value, str) or not value.strip():
        return None

    candidate = value.strip().replace("Z", "+00:00")
    try:
        return datetime.fromisoformat(candidate)
    except ValueError:
        return None


def extract_tags(payload: dict) -> list[str]:
    tags = payload.get("tags")
    output: list[str] = []

    if isinstance(tags, list):
        for item in tags:
            if isinstance(item, str) and item.strip():
                output.append(item.strip())
            elif isinstance(item, dict):
                name = _first_string(item.get("name"), item.get("value"), item.get("tag"))
                if name:
                    output.append(name)

    return sorted({name.strip().lower() for name in output if name.strip()})


def extract_links(content: str, payload: dict) -> list[str]:
    links = set(URL_REGEX.findall(content or ""))

    for key in ("url", "postUrl", "post_url", "canonicalUrl"):
        value = payload.get(key)
        if isinstance(value, str) and value.startswith("http"):
            links.add(value)

    embed = payload.get("embed")
    if isinstance(embed, dict):
        value = embed.get("url")
        if isinstance(value, str) and value.startswith("http"):
            links.add(value)

    clean_links: list[str] = []
    for link in sorted(links):
        try:
            parts = urlparse(link)
            if parts.scheme in {"http", "https"} and parts.netloc:
                clean_links.append(link)
        except Exception:
            continue

    return clean_links


def extract_can_view(payload: dict) -> bool:
    """Whether Patreon reported the post as viewable by the user.

    Locked posts arrive in Patreon's JSON-API format with
    data.attributes.current_user_can_view = false. Accessible posts use a
    flattened format with no such flag, so default to True (viewable).
    """
    data = payload.get("data")
    if isinstance(data, dict):
        attrs = data.get("attributes")
        if isinstance(attrs, dict) and "current_user_can_view" in attrs:
            return bool(attrs["current_user_can_view"])
    if "current_user_can_view" in payload:
        return bool(payload["current_user_can_view"])
    return True


def extract_text_from_doc(json_string: str | None) -> str:
    """Pull plain text out of Patreon's ProseMirror content_json_string."""
    if not json_string:
        return ""
    try:
        doc = json.loads(json_string)
    except (json.JSONDecodeError, TypeError):
        return ""

    parts: list[str] = []

    def walk(node):
        if isinstance(node, dict):
            if node.get("type") == "text":
                parts.append(node.get("text", ""))
            for child in node.get("content", []):
                walk(child)
        elif isinstance(node, list):
            for child in node:
                walk(child)

    walk(doc)
    return " ".join(parts).strip()


def _normalize_json_api(path: Path, payload: dict) -> dict:
    """Normalize patreon-dl's post-api.json (Patreon JSON-API) format."""
    data = payload.get("data", {})
    attrs = data.get("attributes", {})
    included = payload.get("included", [])

    title = (attrs.get("title") or "").strip() or "(untitled)"
    content = (attrs.get("content") or "").strip() or extract_text_from_doc(attrs.get("content_json_string"))
    post_id = str(data.get("id") or "").strip() or None
    post_url = (attrs.get("url") or "").strip() or None
    published_at = _parse_datetime(attrs.get("published_at") or attrs.get("created_at"))

    tags = [
        (item.get("attributes", {}).get("value") or "").strip()
        for item in included
        if item.get("type") == "post_tag"
    ]

    links: set[str] = set(URL_REGEX.findall(content or ""))
    embed = attrs.get("embed")
    if isinstance(embed, dict):
        url = (embed.get("url") or "").strip()
        if url.startswith("http"):
            links.add(url)

    return {
        "post_id": post_id,
        "title": title,
        "content": content,
        "post_url": post_url,
        "published_at": published_at,
        "tags": sorted({t.lower() for t in tags if t}),
        "links": sorted(links),
        "can_view": extract_can_view(payload),
        "raw_json": json.dumps(payload, ensure_ascii=False),
        "source_path": str(path),
    }


def normalize_post(path: Path, payload: dict) -> dict:
    # patreon-dl v3 writes post-api.json in Patreon's JSON-API format.
    data = payload.get("data")
    if isinstance(data, dict) and data.get("type") == "post":
        return _normalize_json_api(path, payload)

    title = _first_string(payload.get("title"), payload.get("name"), payload.get("post_title")) or "(untitled)"

    content = _first_string(
        payload.get("content"),
        payload.get("post_content"),
        payload.get("teaser"),
        payload.get("raw_text"),
    )

    post_id = _first_string(payload.get("id"), payload.get("postId"), payload.get("post_id")) or None
    post_url = _first_string(payload.get("url"), payload.get("postUrl"), payload.get("post_url")) or None
    published_at = _parse_datetime(payload.get("publishedAt") or payload.get("published_at") or payload.get("publishDate"))

    return {
        "post_id": post_id,
        "title": title,
        "content": content,
        "post_url": post_url,
        "published_at": published_at,
        "tags": extract_tags(payload),
        "links": extract_links(content, payload),
        "can_view": extract_can_view(payload),
        "raw_json": json.dumps(payload, ensure_ascii=False),
        "source_path": str(path),
    }
