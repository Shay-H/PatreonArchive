"""
Ad-hoc script to import cinejump posts from downloaded post-api.json files into the DB.
Extracts: title, content, tags, published_at, Google Drive / embed links.
Safe to re-run — skips posts already in the DB by patreon_post_id.
"""

import json
import re
import sys
from datetime import datetime
from pathlib import Path

# ── ensure we can import the app package ──────────────────────────────────────
sys.path.insert(0, str(Path(__file__).parent))

from app.database import SessionLocal
from app.models import Creator, Link, Post, Tag

RAW_DIR = Path("data/raw/cinejump")
CREATOR_NAME = "cinejump"

# ── helpers ───────────────────────────────────────────────────────────────────

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


def parse_post_api(path: Path) -> dict | None:
    try:
        raw = path.read_text(encoding="utf-8", errors="replace")
        payload = json.loads(raw)
    except Exception as exc:
        print(f"  SKIP (parse error): {path}  — {exc}")
        return None

    data = payload.get("data", {})
    if data.get("type") != "post":
        return None

    attrs = data.get("attributes", {})
    included = payload.get("included", [])

    # title
    title = (attrs.get("title") or "").strip() or "(untitled)"

    # content: prefer rendered content, fall back to ProseMirror JSON
    content = (attrs.get("content") or "").strip()
    if not content:
        content = extract_text_from_doc(attrs.get("content_json_string"))

    # published_at
    published_at: datetime | None = None
    raw_date = attrs.get("published_at") or attrs.get("created_at")
    if raw_date:
        try:
            published_at = datetime.fromisoformat(raw_date.replace("Z", "+00:00"))
        except ValueError:
            pass

    # patreon post id & url
    post_id = str(data.get("id") or "").strip() or None
    post_url = (attrs.get("url") or "").strip() or None

    # tags from included array
    tags: list[str] = []
    for item in included:
        if item.get("type") == "post_tag":
            val = (item.get("attributes", {}).get("value") or "").strip()
            if val:
                tags.append(val)

    # links: embed url (Google Drive etc.) + any URLs in content
    links: set[str] = set()

    embed = attrs.get("embed")
    if isinstance(embed, dict):
        url = (embed.get("url") or "").strip()
        if url.startswith("http"):
            links.add(url)

    url_re = re.compile(r"https?://[^\s)\]>'\"]+")
    for url in url_re.findall(content):
        links.add(url)

    return {
        "post_id": post_id,
        "title": title,
        "content": content,
        "published_at": published_at,
        "post_url": post_url,
        "tags": sorted(set(tags), key=str.lower),
        "links": sorted(links),
        "source_path": str(path),
        "raw_json": json.dumps(payload, ensure_ascii=False),
    }


# ── main import ───────────────────────────────────────────────────────────────

def main():
    api_files = sorted(RAW_DIR.rglob("post-api.json"))
    print(f"Found {len(api_files)} post-api.json files under {RAW_DIR}")

    if not api_files:
        print("Nothing to import. Have the posts been downloaded?")
        return

    db = SessionLocal()
    try:
        # get or create creator
        creator = db.query(Creator).filter_by(name=CREATOR_NAME).first()
        if not creator:
            creator = Creator(name=CREATOR_NAME)
            db.add(creator)
            db.flush()
            print(f"Created creator '{CREATOR_NAME}' (id={creator.id})")
        else:
            print(f"Using existing creator '{CREATOR_NAME}' (id={creator.id})")

        # collect existing patreon_post_ids to skip duplicates
        existing_ids: set[str] = {
            row[0]
            for row in db.query(Post.patreon_post_id)
            .filter(Post.creator_id == creator.id, Post.patreon_post_id.isnot(None))
            .all()
        }
        print(f"  {len(existing_ids)} posts already in DB — will skip duplicates")

        tag_cache: dict[str, Tag] = {t.name: t for t in db.query(Tag).all()}

        imported = skipped = errors = 0

        for path in api_files:
            parsed = parse_post_api(path)
            if parsed is None:
                errors += 1
                continue

            pid = parsed["post_id"]
            if pid and pid in existing_ids:
                skipped += 1
                continue

            post = Post(
                creator_id=creator.id,
                patreon_post_id=pid,
                title=parsed["title"],
                content=parsed["content"],
                post_url=parsed["post_url"],
                published_at=parsed["published_at"],
                source_path=parsed["source_path"],
                raw_json=parsed["raw_json"],
            )
            db.add(post)
            db.flush()  # get post.id

            # tags
            for tag_name in parsed["tags"]:
                tag = tag_cache.get(tag_name)
                if not tag:
                    tag = Tag(name=tag_name)
                    db.add(tag)
                    db.flush()
                    tag_cache[tag_name] = tag
                if tag not in post.tags:
                    post.tags.append(tag)

            # links
            for url in parsed["links"]:
                db.add(Link(post_id=post.id, url=url))

            if pid:
                existing_ids.add(pid)

            imported += 1
            if imported % 100 == 0:
                db.commit()
                print(f"  ... {imported} imported so far")

        db.commit()
        print(f"\nDone. Imported: {imported}  |  Skipped: {skipped}  |  Errors: {errors}")

    except Exception:
        db.rollback()
        raise
    finally:
        db.close()


if __name__ == "__main__":
    main()
