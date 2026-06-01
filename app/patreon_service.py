"""FlareSolverr-backed Patreon sync service.

Mirrors cinebingers_service.py: background thread, job-state dict keyed by
creator name, direct DB upserts (no intermediate files).
"""
from __future__ import annotations

import threading
from dataclasses import asdict, dataclass
from datetime import datetime, timezone

from sqlalchemy import select

from app.database import SessionLocal
from app.errors import AuthError
from app.models import Creator, Link, Post
from app.patreon_scraper import scrape_creator, source_path_for
from app.parser import normalize_post
from app.tags import get_or_create_tag as _get_or_create_tag


# ---------------------------------------------------------------------------
# Job state
# ---------------------------------------------------------------------------

@dataclass
class PatreonScrapeState:
    status: str = "idle"          # idle | running | completed | failed
    started_at: str | None = None
    finished_at: str | None = None
    fetched: int = 0
    imported: int = 0
    last_message: str = ""
    error: str | None = None


_lock = threading.Lock()
_jobs: dict[str, PatreonScrapeState] = {}


def get_patreon_scrape_snapshot(creator_name: str) -> dict:
    key = creator_name.strip().lower()
    with _lock:
        return asdict(_jobs.get(key, PatreonScrapeState()))


def _update(creator_name: str, **kw) -> None:
    key = creator_name.strip().lower()
    with _lock:
        job = _jobs.setdefault(key, PatreonScrapeState())
        for k, v in kw.items():
            setattr(job, k, v)


# ---------------------------------------------------------------------------
# DB helpers
# ---------------------------------------------------------------------------

def _get_or_create_creator(session, name: str) -> Creator:
    creator = session.scalar(select(Creator).where(Creator.name == name))
    if not creator:
        creator = Creator(name=name, source_type="patreon")
        session.add(creator)
        session.flush()
    return creator


def _load_known_post_ids(creator_name: str) -> set[str]:
    with SessionLocal() as db:
        creator = db.scalar(select(Creator).where(Creator.name == creator_name))
        if not creator:
            return set()
        rows = db.scalars(
            select(Post.patreon_post_id).where(Post.creator_id == creator.id)
        ).all()
        return {r for r in rows if r}


def _upsert_post(session, creator: Creator, vanity: str, wrapped: dict) -> None:
    """Upsert one API-scraped post using the same logic as sync_service._upsert_post."""
    post_id = str(wrapped.get("data", {}).get("id", "")).strip() or None
    sp = str(source_path_for(vanity, post_id or "unknown"))
    parsed = normalize_post(source_path_for(vanity, post_id or "unknown"), wrapped)

    # Find existing by source_path, then by patreon_post_id.
    post = session.scalar(
        select(Post).where(Post.creator_id == creator.id, Post.source_path == sp)
    )
    if not post and parsed["post_id"]:
        post = session.scalar(
            select(Post).where(
                Post.creator_id == creator.id,
                Post.patreon_post_id == parsed["post_id"],
            )
        )

    if not post:
        post = Post(creator_id=creator.id, source_path=sp)
        session.add(post)

    post.patreon_post_id = parsed["post_id"]
    post.title = parsed["title"]
    post.content = parsed["content"]
    post.post_url = parsed["post_url"]
    post.published_at = parsed["published_at"]
    post.raw_json = parsed["raw_json"]
    post.source_path = sp
    post.can_view = parsed["can_view"]

    # Tags: add-only (same policy as sync_service to preserve tagger tags).
    existing_tag_names = {t.name for t in post.tags}
    for tag_name in parsed["tags"]:
        tag = _get_or_create_tag(session, tag_name)
        if tag.name not in existing_tag_names:
            post.tags.append(tag)
            existing_tag_names.add(tag.name)

    # Links: replace only when changed.
    desired = list(dict.fromkeys(parsed["links"]))
    if {lnk.url for lnk in post.links} != set(desired):
        post.links = [Link(url=u) for u in desired]


# ---------------------------------------------------------------------------
# Background runner
# ---------------------------------------------------------------------------

def _notify_scheduler(*, ok: bool, error: str | None = None, auth: bool = False) -> None:
    try:
        from app.scheduler import on_sync_result
        on_sync_result("patreon", ok=ok, error=error, auth=auth)
    except Exception:
        pass


def _run(creator_name: str) -> None:
    _update(
        creator_name,
        status="running",
        started_at=datetime.now(timezone.utc).isoformat(),
        finished_at=None,
        fetched=0,
        imported=0,
        last_message="Loading known posts…",
        error=None,
    )

    try:
        known_ids = _load_known_post_ids(creator_name)
        _update(creator_name, last_message=f"{len(known_ids)} posts already in DB")

        def progress(msg: str) -> None:
            _update(creator_name, last_message=msg)

        posts = scrape_creator(
            vanity=creator_name,
            known_post_ids=known_ids,
            on_progress=progress,
        )

        _update(creator_name, fetched=len(posts), last_message=f"Fetched {len(posts)} posts — writing to DB…")

        imported = 0
        with SessionLocal() as db:
            creator = _get_or_create_creator(db, creator_name)
            for wrapped in posts:
                _upsert_post(db, creator, creator_name, wrapped)
                imported += 1
            db.commit()

        _update(
            creator_name,
            status="completed",
            finished_at=datetime.now(timezone.utc).isoformat(),
            imported=imported,
            last_message=f"Done — {imported} posts upserted",
        )
        _notify_scheduler(ok=True)

    except Exception as exc:
        _update(
            creator_name,
            status="failed",
            finished_at=datetime.now(timezone.utc).isoformat(),
            error=str(exc),
            last_message=str(exc),
        )
        _notify_scheduler(ok=False, error=str(exc), auth=isinstance(exc, AuthError))


def start_patreon_scrape(creator_name: str) -> dict:
    """Start a background FlareSolverr scrape for creator_name. Returns immediately."""
    key = creator_name.strip().lower()
    with _lock:
        existing = _jobs.get(key)
        if existing and existing.status == "running":
            return asdict(existing)
        job = _jobs.setdefault(key, PatreonScrapeState())
        job.status = "running"
        job.last_message = "Queued…"

    threading.Thread(target=_run, args=(creator_name,), daemon=True).start()
    return get_patreon_scrape_snapshot(creator_name)
