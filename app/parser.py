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


def normalize_post(path: Path, payload: dict) -> dict:
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
        "raw_json": json.dumps(payload, ensure_ascii=False),
        "source_path": str(path),
    }
