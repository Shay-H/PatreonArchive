"""
Cinebingers service — scrapes cinebingers.ca and writes directly to the database.
No intermediate JSON files needed.
"""
from __future__ import annotations

import json
import threading
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path

from sqlalchemy import select

from app.cinebingers_scraper import CinebingersVideo, scrape_all_cinebingers_videos
from app.config import settings
from app.database import SessionLocal
from app.errors import AuthError
from app.models import Creator, Link, Post, Tag
from app.tags import get_or_create_tag as _get_or_create_tag


def _notify_scheduler(*, ok: bool, error: str | None = None, auth: bool = False) -> None:
    """Report a cinebingers sync outcome to the scheduler (lazy import)."""
    try:
        from app.scheduler import on_sync_result
        on_sync_result("cinebingers", ok=ok, error=error, auth=auth)
    except Exception:
        pass

# ---------------------------------------------------------------------------
# Job state tracking
# ---------------------------------------------------------------------------

@dataclass
class CinebingersJobState:
    status: str = "idle"          # idle | running | completed | failed
    started_at: str | None = None
    finished_at: str | None = None
    scraped: int = 0
    imported: int = 0
    current_page: int = 0
    last_message: str = ""
    error: str | None = None


_job_lock = threading.Lock()
_job_state = CinebingersJobState()


def get_job_snapshot() -> dict:
    with _job_lock:
        return asdict(_job_state)


def _update(**changes) -> None:
    with _job_lock:
        for k, v in changes.items():
            setattr(_job_state, k, v)


# ---------------------------------------------------------------------------
# Cookie loading
# ---------------------------------------------------------------------------

def _load_known_video_ids(creator_name: str) -> set[str]:
    """Return the set of cinebingers video IDs already imported for this creator."""
    with SessionLocal() as db:
        creator = db.scalar(select(Creator).where(Creator.name == creator_name))
        if not creator:
            return set()
        rows = db.scalars(
            select(Post.patreon_post_id).where(Post.creator_id == creator.id)
        ).all()
        return {r for r in rows if r}


def _load_cookies() -> dict[str, str] | None:
    """Load cinebingers session cookies (JSON dict of cookie name -> value).

    Reads the configured path (data/cinebingers_cookies.json by default), then
    falls back to the legacy .env.cinebingers location for backward compat.
    """
    candidates = [settings.cinebingers_cookies_file, Path(".env.cinebingers")]
    for path in candidates:
        if path.exists():
            try:
                data = json.loads(path.read_text(encoding="utf-8"))
                if isinstance(data, dict):
                    return data
            except Exception:
                pass
    return None


# ---------------------------------------------------------------------------
# DB helpers
# ---------------------------------------------------------------------------

def _get_or_create_creator(session, name: str) -> Creator:
    creator = session.scalar(select(Creator).where(Creator.name == name))
    if not creator:
        creator = Creator(name=name)
        session.add(creator)
        session.flush()
    return creator


def _upsert_video(session, creator: Creator, video: CinebingersVideo) -> None:
    """Insert or update a cinebingers video as a Post row."""
    source_path = f"cinebingers/{video.video_id}"

    post = session.scalar(
        select(Post).where(
            Post.creator_id == creator.id,
            Post.source_path == source_path,
        )
    )
    if not post:
        post = Post(creator_id=creator.id, source_path=source_path)
        session.add(post)

    post.patreon_post_id = video.video_id
    post.title = video.title
    post.content = ""
    post.can_view = True  # scraped cinebingers videos are always viewable
    post.post_url = video.cinebingers_url
    post.published_at = None
    post.raw_json = json.dumps({
        "id": video.video_id,
        "title": video.title,
        "youtube_url": video.youtube_url,
        "cinebingers_url": video.cinebingers_url,
        "source": "cinebingers.ca",
    })

    post.tags = [_get_or_create_tag(session, "cinebingers")]
    post.links = [Link(url=video.youtube_url)]


# ---------------------------------------------------------------------------
# Main service function (runs in background thread)
# ---------------------------------------------------------------------------

def _run(creator_name: str) -> None:
    _update(
        status="running",
        started_at=datetime.now(timezone.utc).isoformat(),
        finished_at=None,
        scraped=0,
        imported=0,
        current_page=0,
        last_message="Loading cookies…",
        error=None,
    )

    try:
        cookies = _load_cookies()
        if cookies:
            _update(last_message=f"Using {len(cookies)} cookies")
        else:
            _update(last_message="No cookies found — may fail on protected pages")

        # Already-imported video IDs, so pagination can stop once it reaches a
        # page of all-known videos (nothing newer beyond it).
        known_ids = _load_known_video_ids(creator_name)

        all_videos: list[CinebingersVideo] = []

        def on_page(page: int, count: int) -> None:
            _update(current_page=page, scraped=len(all_videos) + count, last_message=f"Page {page}: {count} videos")

        all_videos = scrape_all_cinebingers_videos(
            cookies=cookies,
            request_delay=1.0,
            on_page_complete=on_page,
            known_ids=known_ids,
        )

        _update(scraped=len(all_videos), last_message=f"Scraped {len(all_videos)} videos — writing to DB…")

        imported = 0
        with SessionLocal() as db:
            creator = _get_or_create_creator(db, creator_name)
            for video in all_videos:
                _upsert_video(db, creator, video)
                imported += 1
            db.commit()

        _update(
            status="completed",
            finished_at=datetime.now(timezone.utc).isoformat(),
            imported=imported,
            last_message=f"Done — {imported} videos in database",
        )
        _notify_scheduler(ok=True)

    except Exception as exc:
        _update(
            status="failed",
            finished_at=datetime.now(timezone.utc).isoformat(),
            error=str(exc),
            last_message=str(exc),
        )
        _notify_scheduler(ok=False, error=str(exc), auth=isinstance(exc, AuthError))


def start_cinebingers_sync(creator_name: str = "cinebingers") -> dict:
    """
    Start a background sync of cinebingers.ca into the database.
    Returns immediately with the current job state.
    """
    with _job_lock:
        if _job_state.status == "running":
            return asdict(_job_state)
        # Mark running synchronously so a caller that immediately polls the
        # snapshot (e.g. the scheduler's _wait) doesn't see a stale "completed".
        _job_state.status = "running"
        _job_state.last_message = "Queued…"

    thread = threading.Thread(target=_run, args=(creator_name,), daemon=True)
    thread.start()
    return get_job_snapshot()
