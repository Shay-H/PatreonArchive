"""FlareSolverr-backed Patreon sync service.

Mirrors cinebingers_service.py: background thread, job-state dict keyed by
creator name, direct DB upserts (no intermediate files).
"""
from __future__ import annotations

import logging
import os
import threading
from dataclasses import asdict, dataclass
from datetime import datetime, timedelta, timezone

from sqlalchemy import select

from app.config import settings
from app.database import SessionLocal
from app.errors import AuthError
from app.models import Attachment, Creator, Link, Post
from app.patreon_scraper import (
    _cookie_list,
    download_attachment,
    fetch_post,
    scrape_creator,
    source_path_for,
)
from app.parser import normalize_post
from app.tags import get_or_create_tag as _get_or_create_tag

_log = logging.getLogger("uvicorn.error")


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

def _get_or_create_creator(session, name: str, archive_mode: str | None = None) -> Creator:
    creator = session.scalar(select(Creator).where(Creator.name == name))
    if not creator:
        creator = Creator(name=name, source_type="patreon", archive_mode=archive_mode or "video")
        session.add(creator)
        session.flush()
    elif archive_mode and creator.archive_mode != archive_mode:
        creator.archive_mode = archive_mode
        session.flush()
    return creator


def _safe_filename(name: str) -> str:
    """Strip any path components from a Patreon-supplied filename."""
    base = os.path.basename((name or "").replace("\\", "/")).strip()
    return base or "attachment"


def _download_post_attachments(creator: Creator, post: Post) -> None:
    """Download any not-yet-archived attachments for a content-mode post."""
    cookie_str = settings.patreon_cookie
    post_key = post.patreon_post_id or str(post.id)
    for att in post.attachments:
        if att.downloaded and att.local_path and os.path.exists(att.local_path):
            continue
        dest = settings.raw_root / creator.name / "attachments" / post_key / _safe_filename(att.filename)
        try:
            local_path, size = download_attachment(att.remote_url, dest, cookie_str)
            att.local_path = str(local_path)
            att.size_bytes = size or att.size_bytes
            att.downloaded = True
        except Exception as exc:  # never fail the whole sync over one file
            _log.warning("Attachment download failed (%s): %s", att.remote_url, exc)


def backfill_attachments(creator_name: str, on_progress=None) -> dict:
    """Download every not-yet-archived attachment for an existing creator.

    The incremental scrape stops at the first page of already-known posts, so it
    won't revisit old posts to fetch attachments that were never downloaded (or
    failed under the old &amp;-corrupted-URL bug). This walks all stored posts and
    downloads anything still missing. Safe to re-run — already-archived files are
    skipped.
    """
    downloaded = 0
    failed = 0
    with SessionLocal() as db:
        creator = db.scalar(select(Creator).where(Creator.name == creator_name))
        if not creator:
            return {"creator": creator_name, "downloaded": 0, "failed": 0, "error": "unknown creator"}

        posts = db.scalars(select(Post).where(Post.creator_id == creator.id)).all()
        total = len(posts)
        for idx, post in enumerate(posts, start=1):
            pending = [a for a in post.attachments if not (a.downloaded and a.local_path)]
            if not pending:
                continue
            _download_post_attachments(creator, post)
            for a in pending:
                if a.downloaded:
                    downloaded += 1
                else:
                    failed += 1
            db.flush()
            if on_progress and idx % 10 == 0:
                on_progress(f"{idx}/{total} posts — {downloaded} files archived, {failed} failed")
        db.commit()

    return {"creator": creator_name, "downloaded": downloaded, "failed": failed}


def _load_known_post_ids(creator_name: str) -> set[str]:
    with SessionLocal() as db:
        creator = db.scalar(select(Creator).where(Creator.name == creator_name))
        if not creator:
            return set()
        rows = db.scalars(
            select(Post.patreon_post_id).where(Post.creator_id == creator.id)
        ).all()
        return {r for r in rows if r}


def _upsert_post(session, creator: Creator, vanity: str, wrapped: dict) -> Post:
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

    if post is not None and _is_inaccessible(wrapped):
        # Lost access (e.g. a lapsed pledge): Patreon now serves a teaser.
        # Every path that re-saves a known post lands here, including the
        # creator scrape, which re-imports the newest page on each sync.
        # Keep the archived copy instead of overwriting it.
        return post

    if not post:
        post = Post(creator_id=creator.id, source_path=sp)
        session.add(post)
        title_changed = True
    else:
        title_changed = post.title != parsed["title"]

    post.patreon_post_id = parsed["post_id"]
    post.title = parsed["title"]
    post.content = parsed["content"]
    post.post_url = parsed["post_url"]
    post.published_at = parsed["published_at"]
    post.raw_json = parsed["raw_json"]
    post.source_path = sp
    post.can_view = parsed["can_view"]

    # Tags. Content-archive posts carry the creator's own Patreon tags, which
    # are authoritative: replace, so the scrape both captures them and clears
    # any stale film/genre tags an earlier run applied. Video posts keep the
    # add-only policy so the TMDB auto-tagger's movie/tv/genre tags survive.
    if creator.archive_mode in ("content", "links"):
        post.tags = [_get_or_create_tag(session, t) for t in parsed["tags"]]
    else:
        existing_tag_names = {t.name for t in post.tags}
        for tag_name in parsed["tags"]:
            tag = _get_or_create_tag(session, tag_name)
            if tag.name not in existing_tag_names:
                post.tags.append(tag)
                existing_tag_names.add(tag.name)
        if title_changed:
            post.auto_tagged_at = None

    # Links: replace only when changed.
    desired = list(dict.fromkeys(parsed["links"]))
    if {lnk.url for lnk in post.links} != set(desired):
        post.links = [Link(url=u) for u in desired]

    # Attachments: add new files by remote_url; refresh metadata on existing
    # rows but preserve any already-downloaded local file.
    existing_by_url = {a.remote_url: a for a in post.attachments}
    for meta in parsed.get("attachments", []):
        att = existing_by_url.get(meta["remote_url"])
        if att is None:
            post.attachments.append(Attachment(
                filename=meta["filename"],
                kind=meta["kind"],
                mimetype=meta["mimetype"],
                remote_url=meta["remote_url"],
                size_bytes=meta["size_bytes"],
            ))
        else:
            att.filename = meta["filename"]
            att.kind = meta["kind"]
            att.mimetype = meta["mimetype"]
            if meta["size_bytes"] is not None:
                att.size_bytes = meta["size_bytes"]

    return post


# ---------------------------------------------------------------------------
# Tracked-post refresh
# ---------------------------------------------------------------------------

def _fingerprint(post: Post) -> tuple:
    """What "the post changed" means: body, title, links or attached files.

    Deliberately ignores tag edits and Patreon's own metadata churn, so a
    cosmetic re-tag upstream doesn't get reported to the user as new content.
    """
    return (
        post.title,
        post.content,
        tuple(sorted(lnk.url for lnk in post.links)),
        tuple(sorted(att.remote_url for att in post.attachments)),
    )


def _is_inaccessible(wrapped: dict) -> bool:
    """True when Patreon served the post but won't show us its contents.

    This is what a lapsed pledge looks like: the post still exists, but
    current_user_can_view flips to false and the body comes back as a teaser
    or empty. Overwriting the archive with that would destroy the copy we are
    keeping it for.
    """
    attrs = (wrapped.get("data") or {}).get("attributes") or {}
    return attrs.get("current_user_can_view") is False


def _due_for_check(post: Post, now: datetime) -> bool:
    """Respect the post's own refresh interval (0 hours = check every sync)."""
    interval = post.refresh_interval_hours or 0
    if interval <= 0 or post.last_checked_at is None:
        return True
    last = post.last_checked_at
    if last.tzinfo is None:  # SQLite hands these back naive
        last = last.replace(tzinfo=timezone.utc)
    return (now - last) >= timedelta(hours=interval)


def _stop_tracking(post: Post, reason: str, now: datetime) -> None:
    """Retire a post from tracking while keeping everything already archived."""
    post.tracked = False
    post.tracking_stopped_reason = reason
    post.last_checked_at = now
    _log.info("Stopped tracking post %s: %s", post.patreon_post_id, reason)


def refresh_tracked_posts(creator_name: str | None = None, on_progress=None) -> dict:
    """Re-fetch every due tracked post and refresh the stored copy.

    Scoped to one creator when creator_name is given. Only Patreon-sourced
    posts can be refreshed this way — a tracked cinebingers post is counted as
    skipped rather than failed, since its integration has no per-post fetch.

    Losing access never costs us the archive. When a pledge lapses, Patreon
    keeps serving the post but marks it unviewable and empties the body; we
    detect that, keep the stored copy untouched, and stop tracking the post
    with a reason the manage page can show. A whole-account auth failure is
    different — that aborts the pass so one expired cookie can't quietly
    untrack the entire library.

    One post's transient failure never aborts the pass: a FlareSolverr blip
    leaves that row untouched and the rest still refresh.
    """
    cookie_str = settings.patreon_cookie
    if not cookie_str:
        raise AuthError("PATREON_COOKIE is not configured in .env")
    cookies = _cookie_list(cookie_str)

    checked = updated = failed = skipped = stopped = 0
    now = datetime.now(timezone.utc)
    with SessionLocal() as db:
        stmt = select(Post).join(Creator).where(Post.tracked.is_(True))
        if creator_name:
            stmt = stmt.where(Creator.name == creator_name)
        posts = db.scalars(stmt).all()
        total = len(posts)

        for idx, post in enumerate(posts, start=1):
            if post.creator.source_type != "patreon" or not post.patreon_post_id:
                skipped += 1
                continue
            if not _due_for_check(post, now):
                skipped += 1
                continue

            if on_progress:
                on_progress(f"Checking tracked post {idx}/{total}: {post.title[:60]}")
            try:
                wrapped = fetch_post(post.patreon_post_id, cookies)
            except AuthError:
                # The cookie, not this post. Commit what we have and stop:
                # treating this as lost access would untrack everything.
                db.commit()
                raise
            except Exception as exc:  # transient fetch failure — try again next sync
                _log.warning("Tracked refresh failed (%s): %s", post.patreon_post_id, exc)
                failed += 1
                continue

            if wrapped is None:
                _stop_tracking(post, "Patreon no longer serves this post", now)
                stopped += 1
                continue

            if _is_inaccessible(wrapped):
                _stop_tracking(
                    post,
                    "No longer accessible — pledge may have ended. Archived copy kept.",
                    now,
                )
                stopped += 1
                continue

            before = _fingerprint(post)
            refreshed = _upsert_post(db, post.creator, post.creator.name, wrapped)
            db.flush()
            refreshed.last_checked_at = now
            if _fingerprint(refreshed) != before:
                refreshed.content_updated_at = now
                updated += 1
                if on_progress:
                    on_progress(f"Updated: {refreshed.title[:60]}")
                # A content-mode creator mirrors files, so pick up anything new.
                if post.creator.archive_mode == "content":
                    _download_post_attachments(post.creator, refreshed)
            checked += 1

        db.commit()

    return {
        "checked": checked,
        "updated": updated,
        "failed": failed,
        "skipped": skipped,
        "stopped": stopped,
    }


# ---------------------------------------------------------------------------
# Tracked refresh as a background job
# ---------------------------------------------------------------------------
#
# One tracked post costs a full FlareSolverr challenge solve (~20s), so a
# manual "check all now" over a handful of posts runs well past any sane HTTP
# timeout. Same shape as the scrape job: start, poll, read the snapshot.

@dataclass
class TrackedRefreshState:
    status: str = "idle"          # idle | running | completed | failed
    started_at: str | None = None
    finished_at: str | None = None
    checked: int = 0
    updated: int = 0
    failed: int = 0
    skipped: int = 0
    stopped: int = 0
    last_message: str = ""
    error: str | None = None


_tracked_lock = threading.Lock()
_tracked_job = TrackedRefreshState()


def get_tracked_refresh_snapshot() -> dict:
    with _tracked_lock:
        return asdict(_tracked_job)


def _tracked_update(**kw) -> None:
    with _tracked_lock:
        for k, v in kw.items():
            setattr(_tracked_job, k, v)


def _run_tracked_refresh(creator_name: str | None) -> None:
    _tracked_update(
        status="running",
        started_at=datetime.now(timezone.utc).isoformat(),
        finished_at=None,
        checked=0, updated=0, failed=0, skipped=0, stopped=0,
        last_message="Starting…",
        error=None,
    )
    try:
        result = refresh_tracked_posts(
            creator_name, on_progress=lambda msg: _tracked_update(last_message=msg)
        )
        summary = (
            f"{result['checked']} checked, {result['updated']} updated"
            + (f", {result['stopped']} stopped (access lost)" if result["stopped"] else "")
            + (f", {result['failed']} failed" if result["failed"] else "")
        )
        _tracked_update(
            status="completed",
            finished_at=datetime.now(timezone.utc).isoformat(),
            last_message=summary,
            **result,
        )
    except Exception as exc:
        _tracked_update(
            status="failed",
            finished_at=datetime.now(timezone.utc).isoformat(),
            error=str(exc),
            last_message=str(exc),
        )
        _notify_scheduler(ok=False, error=str(exc), auth=isinstance(exc, AuthError))


def start_tracked_refresh(creator_name: str | None = None) -> dict:
    """Kick off a tracked-post refresh in the background. Returns immediately."""
    with _tracked_lock:
        if _tracked_job.status == "running":
            return asdict(_tracked_job)
        _tracked_job.status = "running"
        _tracked_job.last_message = "Queued…"

    threading.Thread(
        target=_run_tracked_refresh, args=(creator_name,), daemon=True
    ).start()
    return get_tracked_refresh_snapshot()


# ---------------------------------------------------------------------------
# Background runner
# ---------------------------------------------------------------------------

def _notify_scheduler(*, ok: bool, error: str | None = None, auth: bool = False) -> None:
    try:
        from app.scheduler import on_sync_result
        on_sync_result("patreon", ok=ok, error=error, auth=auth)
    except Exception:
        pass


def _run(creator_name: str, archive_mode: str | None = None) -> None:
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
            creator = _get_or_create_creator(db, creator_name, archive_mode=archive_mode)
            effective_mode = creator.archive_mode
            # Only "content" mirrors the bytes. "links" archives the body and
            # the URLs and leaves attachments as metadata-only rows — its
            # creators publish files far too large to keep a copy of.
            downloads_files = effective_mode == "content"
            is_video = effective_mode == "video"
            for idx, wrapped in enumerate(posts, start=1):
                post = _upsert_post(db, creator, creator_name, wrapped)
                if downloads_files:
                    db.flush()  # ensure post.id for attachment paths
                    _update(creator_name, last_message=f"Downloading attachments… ({idx}/{len(posts)})")
                    _download_post_attachments(creator, post)
                imported += 1
            db.commit()

        # Refresh tracked posts. The scrape above only sees new posts, so a
        # post that keeps growing after publication is caught here or nowhere.
        # Never let it fail the sync — the new posts are already committed.
        try:
            _update(creator_name, last_message="Checking tracked posts for updates…")
            tracked = refresh_tracked_posts(creator_name, on_progress=progress)
            if tracked["checked"]:
                _log.info("Tracked refresh for %s: %s", creator_name, tracked)
        except Exception as exc:
            _log.warning("Tracked refresh skipped for %s: %s", creator_name, exc)

        # Auto-tag the just-imported posts with TMDB genre/type tags — video only.
        # Content and links creators get no movie/tv/genre tags. Scoped to
        # untagged posts; never let tagging fail the sync.
        if is_video:
            try:
                from app.tagger import tag_untagged_posts
                _update(creator_name, last_message="Tagging newly imported posts…")
                tag_untagged_posts(creator_name)
            except Exception:
                pass

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


def start_patreon_scrape(creator_name: str, archive_mode: str | None = None) -> dict:
    """Start a background FlareSolverr scrape for creator_name. Returns immediately.

    archive_mode, when given ("video" | "content" | "links"), is applied to the
    creator before scraping, so a newly added content Patreon downloads its
    attachments and a links Patreon pointedly does not.
    """
    key = creator_name.strip().lower()
    with _lock:
        existing = _jobs.get(key)
        if existing and existing.status == "running":
            return asdict(existing)
        job = _jobs.setdefault(key, PatreonScrapeState())
        job.status = "running"
        job.last_message = "Queued…"

    threading.Thread(target=_run, args=(creator_name, archive_mode), daemon=True).start()
    return get_patreon_scrape_snapshot(creator_name)
