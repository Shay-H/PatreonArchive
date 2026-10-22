"""Collections for homelib, the home Foundry map library.

homelib polls GET /api/homelib/collections to learn which library sections
exist and which module manifests belong to each, so new packs and new
creators are picked up without touching the home machine.

Configured by data/homelib/collections.json:

    {"collections": [
        {"name": "Maps", "post_id": 19055},
        {"name": "Scenes", "manifests": "czepeku-scenes.txt"},
        {"name": "Extras", "links": ["https://example.com/module.json"]}
    ]}

A collection's links come from any mix of a tracked post's off-site links
(post_id), a text file of manifest URLs in data/homelib/ (manifests, one per
line, # comments allowed) and an inline list (links).
"""
import json
import logging
from pathlib import Path

from sqlalchemy import select
from sqlalchemy.orm import Session, joinedload

from app import config
from app.models import Post
from app.parser import is_patreon_url

_log = logging.getLogger(__name__)


class CollectionsConfigError(ValueError):
    """collections.json is unreadable or malformed."""


def collections_dir() -> Path:
    return config.settings.data_root / "homelib"


def read_manifest_list(path: Path) -> list[str]:
    lines = path.read_text(encoding="utf-8-sig").splitlines()
    return [line.strip() for line in lines if line.strip() and not line.strip().startswith("#")]


def _post_links(db: Session, post_id: int) -> list[str]:
    post = db.scalar(select(Post).where(Post.id == post_id).options(joinedload(Post.links)))
    if not post:
        _log.warning("homelib collection refers to missing post %s", post_id)
        return []
    links = sorted(post.links, key=lambda link: link.id)
    return [link.url for link in links if not is_patreon_url(link.url)]


def _list_file(name: str) -> list[str]:
    base = collections_dir().resolve()
    path = (base / name).resolve()
    if base not in path.parents:
        raise CollectionsConfigError(f"manifest list {name!r} is outside {base}")
    if not path.is_file():
        _log.warning("homelib manifest list %s not found", path)
        return []
    return read_manifest_list(path)


def resolve_collections(db: Session) -> list[dict]:
    """Every configured collection with its manifest links, in config order."""
    config = collections_dir() / "collections.json"
    if not config.is_file():
        return []
    try:
        entries = json.loads(config.read_text(encoding="utf-8-sig"))["collections"]
    except (ValueError, KeyError, TypeError) as e:
        raise CollectionsConfigError(f"{config}: {e}") from e

    out = []
    for entry in entries:
        name = (entry.get("name") or "").strip() if isinstance(entry, dict) else ""
        if not name:
            raise CollectionsConfigError(f"{config}: every collection needs a name")
        links: list[str] = []
        if entry.get("post_id") is not None:
            links += _post_links(db, int(entry["post_id"]))
        if entry.get("manifests"):
            links += _list_file(entry["manifests"])
        links += [str(u) for u in entry.get("links") or []]
        out.append({"name": name, "links": list(dict.fromkeys(links))})  # dedupe, keep order
    return out
