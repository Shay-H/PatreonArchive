from __future__ import annotations

import html
import json
import re
from datetime import datetime
from pathlib import Path
from urllib.parse import urlparse

# '<' is excluded alongside '>' so a bare URL that ends an HTML paragraph
# ("… https://example.com/pack</p>") doesn't swallow the closing tag. None of
# the excluded characters are legal unencoded in a URL.
URL_REGEX = re.compile(r"https?://[^\s<)\]>'\"]+")


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


# Map common extensions / mimetypes to a coarse display category.
_EBOOK_EXTS = {".epub", ".mobi", ".azw", ".azw3", ".fb2", ".cbz", ".cbr"}
_TEXT_EXTS = {".txt", ".md", ".rtf", ".csv", ".doc", ".docx", ".odt"}


def classify_attachment(filename: str | None, mimetype: str | None) -> str:
    """Coarse category for a downloaded file: pdf | ebook | text | json | image | other."""
    name = (filename or "").lower()
    mime = (mimetype or "").lower()
    ext = ""
    if "." in name:
        ext = name[name.rfind("."):]

    if ext == ".pdf" or "pdf" in mime:
        return "pdf"
    if ext == ".json" or "json" in mime:
        return "json"
    if ext in _EBOOK_EXTS or "epub" in mime or "mobi" in mime:
        return "ebook"
    if ext in _TEXT_EXTS or mime.startswith("text/") or "word" in mime:
        return "text"
    if mime.startswith("image/") or ext in {".png", ".jpg", ".jpeg", ".gif", ".webp"}:
        return "image"
    return "other"


def _ref_keys(post: dict, rel_name: str) -> list[tuple[str, str]]:
    """({type}, {id}) pairs referenced by post.relationships[rel_name].data."""
    rel = (post.get("relationships") or {}).get(rel_name)
    if not isinstance(rel, dict):
        return []
    data = rel.get("data")
    items = [data] if isinstance(data, dict) else data if isinstance(data, list) else []
    return [
        (str(it.get("type")), str(it.get("id")))
        for it in items
        if isinstance(it, dict) and it.get("id") is not None
    ]


def extract_attachments(data: dict, included: list[dict]) -> list[dict]:
    """Pull attached files referenced by a post out of the JSON-API `included` array.

    Newer posts reference files via relationships.attachments_media → `media`
    items (attributes: download_url, file_name, mimetype, size_bytes). Older posts
    use relationships.attachments → `attachment` items (attributes: name, url).
    Both shapes are normalized to the same dict.
    """
    by_key = {(str(it.get("type")), str(it.get("id"))): it for it in included if isinstance(it, dict)}
    wanted = _ref_keys(data, "attachments_media") + _ref_keys(data, "attachments")

    results: list[dict] = []
    seen_urls: set[str] = set()
    for key in wanted:
        item = by_key.get(key)
        if not item:
            continue
        attrs = item.get("attributes") or {}
        remote_url = _first_string(attrs.get("download_url"), attrs.get("url"))
        if not remote_url or remote_url in seen_urls:
            continue
        seen_urls.add(remote_url)
        filename = _first_string(attrs.get("file_name"), attrs.get("name")) or "attachment"
        mimetype = _first_string(attrs.get("mimetype")) or None
        size = attrs.get("size_bytes")
        results.append({
            "filename": filename,
            "mimetype": mimetype,
            "remote_url": remote_url,
            "size_bytes": int(size) if isinstance(size, (int, float)) else None,
            "kind": classify_attachment(filename, mimetype),
        })
    return results


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


# ProseMirror node types Patreon uses in content_json_string, mapped to the
# block-level HTML tag they render as. `heading` (variable level) and the inline
# `text`/`hardBreak`/`horizontalRule` nodes are handled specially below.
_PM_BLOCK_TAGS = {
    "paragraph": "p",
    "blockquote": "blockquote",
    "bulletList": "ul",
    "orderedList": "ol",
    "listItem": "li",
    "codeBlock": "pre",
}

# Inline text marks -> wrapping tag. `link` is handled separately (needs href).
_PM_MARK_TAGS = {
    "bold": "strong",
    "strong": "strong",
    "italic": "em",
    "em": "em",
    "underline": "u",
    "strike": "s",
    "strikethrough": "s",
    "code": "code",
}


def _pm_render_node(node) -> str:
    if not isinstance(node, dict):
        return ""
    ntype = node.get("type")

    if ntype == "text":
        out = html.escape(node.get("text", ""))
        for mark in node.get("marks") or []:
            mtype = mark.get("type")
            if mtype == "link":
                href = html.escape((mark.get("attrs") or {}).get("href", ""), quote=True)
                out = f'<a href="{href}">{out}</a>'
            elif mtype in _PM_MARK_TAGS:
                tag = _PM_MARK_TAGS[mtype]
                out = f"<{tag}>{out}</{tag}>"
        return out

    if ntype == "hardBreak":
        return "<br>"
    if ntype == "horizontalRule":
        return "<hr>"

    inner = "".join(_pm_render_node(child) for child in node.get("content") or [])

    if ntype == "heading":
        level = (node.get("attrs") or {}).get("level", 2)
        if not (isinstance(level, int) and 1 <= level <= 6):
            level = 2
        return f"<h{level}>{inner}</h{level}>"
    if ntype in _PM_BLOCK_TAGS:
        tag = _PM_BLOCK_TAGS[ntype]
        return f"<{tag}>{inner}</{tag}>"

    # doc root or any unknown container: emit children, drop the wrapper.
    return inner


def prosemirror_to_html(json_string: str | None) -> str:
    """Render Patreon's ProseMirror content_json_string to HTML.

    Preserves paragraphs, lists, blockquotes, rules, line breaks, and inline
    marks (bold/italic/underline/strike/link). Output is still untrusted and
    MUST be passed through sanitize_html before reaching the browser.
    """
    if not json_string:
        return ""
    try:
        doc = json.loads(json_string)
    except (json.JSONDecodeError, TypeError):
        return ""
    return "".join(_pm_render_node(child) for child in doc.get("content") or [])


def extract_rich_html(payload: dict) -> str:
    """Best rich-HTML body for a post, unsanitized.

    Prefers Patreon's pre-rendered HTML `content` field; falls back to rendering
    the ProseMirror `content_json_string`. Returns "" when the post has neither
    (e.g. video posts whose body is empty). Callers must sanitize the result.
    """
    attrs = (payload.get("data") or {}).get("attributes") or {}
    content_html = (attrs.get("content") or "").strip()
    if content_html:
        return content_html
    return prosemirror_to_html(attrs.get("content_json_string"))


# Patreon's own hosts. Links back into Patreon (the post itself, the creator
# page, tier-upgrade prompts, "see my other post" cross-links, inline image
# CDN URLs) are archive noise for a links-mode creator, whose value is the
# off-site material the post points at. They are still stored — the API splits
# them out at serve time so the external links lead.
_PATREON_HOSTS = ("patreon.com", "patreonusercontent.com", "patreon.co")

# Trailing sentence punctuation swept up by URL_REGEX when a bare URL ends a
# sentence. ')' / ']' / '>' / quotes are already excluded by the pattern.
_URL_TRAILING_PUNCT = ".,;:!?"

_HREF_RE = re.compile(r"""<a\b[^>]*?\bhref\s*=\s*["']([^"']+)["']""", re.IGNORECASE)


def is_patreon_url(url: str) -> bool:
    """True when the URL points back at Patreon itself (any subdomain)."""
    try:
        host = (urlparse(url).hostname or "").lower()
    except ValueError:
        return False
    return any(host == h or host.endswith("." + h) for h in _PATREON_HOSTS)


def _clean_url(url: str) -> str | None:
    """Normalize one candidate URL, or None if it isn't a usable http(s) link."""
    candidate = html.unescape((url or "").strip()).rstrip(_URL_TRAILING_PUNCT)
    if not candidate:
        return None
    try:
        parts = urlparse(candidate)
    except ValueError:
        return None
    if parts.scheme not in {"http", "https"} or not parts.netloc:
        return None
    return candidate


def _prosemirror_link_hrefs(json_string: str | None) -> list[str]:
    """hrefs carried by `link` marks in Patreon's ProseMirror body.

    Hyperlinked text ("Download the map pack here") keeps its URL only in the
    mark, never in the text, so a text-only scan loses exactly the links a
    links-mode archive exists to capture.
    """
    if not json_string:
        return []
    try:
        doc = json.loads(json_string)
    except (json.JSONDecodeError, TypeError):
        return []

    found: list[str] = []

    def walk(node):
        if isinstance(node, dict):
            for mark in node.get("marks") or []:
                if isinstance(mark, dict) and mark.get("type") == "link":
                    href = (mark.get("attrs") or {}).get("href")
                    if isinstance(href, str):
                        found.append(href)
            for child in node.get("content") or []:
                walk(child)
        elif isinstance(node, list):
            for child in node:
                walk(child)

    walk(doc)
    return found


def extract_all_links(attrs: dict) -> list[str]:
    """Every outbound URL a post references, deduped and sorted.

    Pulls from all four places Patreon hides a link: <a href> in the rendered
    HTML body, bare URLs in the body text, `link` marks in the ProseMirror
    source, and the post's embed. URL_REGEX alone (the old behaviour) saw only
    the second, so hyperlinked text and bodies that arrive as ProseMirror-only
    came back empty.
    """
    content_html = attrs.get("content") or ""
    pm_json = attrs.get("content_json_string")

    candidates: list[str] = []
    candidates += _HREF_RE.findall(content_html)
    candidates += URL_REGEX.findall(content_html)
    candidates += _prosemirror_link_hrefs(pm_json)
    candidates += URL_REGEX.findall(extract_text_from_doc(pm_json))

    embed = attrs.get("embed")
    if isinstance(embed, dict):
        candidates.append(embed.get("url") or "")

    cleaned = {url for url in (_clean_url(c) for c in candidates) if url}
    return sorted(cleaned)


def _normalize_json_api(path: Path, payload: dict) -> dict:
    """Normalize patreon-dl's post-api.json (Patreon JSON-API) format."""
    data = payload.get("data", {})
    attrs = data.get("attributes", {})
    included = payload.get("included", [])

    title = html.unescape((attrs.get("title") or "").strip()) or "(untitled)"
    content = (attrs.get("content") or "").strip() or extract_text_from_doc(attrs.get("content_json_string"))
    post_id = str(data.get("id") or "").strip() or None
    post_url = (attrs.get("url") or "").strip() or None
    published_at = _parse_datetime(attrs.get("published_at") or attrs.get("created_at"))

    tags = [
        (item.get("attributes", {}).get("value") or "").strip()
        for item in included
        if item.get("type") == "post_tag"
    ]

    return {
        "post_id": post_id,
        "title": title,
        "content": content,
        "post_url": post_url,
        "published_at": published_at,
        "tags": sorted({t.lower() for t in tags if t}),
        "links": extract_all_links(attrs),
        "attachments": extract_attachments(data, included),
        "can_view": extract_can_view(payload),
        "raw_json": json.dumps(payload, ensure_ascii=False),
        "source_path": str(path),
    }


def normalize_post(path: Path, payload: dict) -> dict:
    # patreon-dl v3 writes post-api.json in Patreon's JSON-API format.
    data = payload.get("data")
    if isinstance(data, dict) and data.get("type") == "post":
        return _normalize_json_api(path, payload)

    title = html.unescape(_first_string(payload.get("title"), payload.get("name"), payload.get("post_title")) or "") or "(untitled)"

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
        "attachments": [],
        "can_view": extract_can_view(payload),
        "raw_json": json.dumps(payload, ensure_ascii=False),
        "source_path": str(path),
    }
