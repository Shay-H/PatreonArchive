from __future__ import annotations

import shlex
import shutil
import subprocess
import sys
import threading
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from typing import Callable
from pathlib import Path
import os

from sqlalchemy import select
from sqlalchemy.orm import Session, sessionmaker

from app.config import settings
from app.config import ensure_metadata_only_config_file
from app.cleanup import prune_raw_media
from app.errors import AuthError
from app.tags import get_or_create_tag as _get_or_create_tag
from app.database import SessionLocal
from app.models import Creator, Link, Post, Tag
from app.parser import normalize_post, parse_post_info_file
from app.cinebingers_scraper import scrape_all_cinebingers_videos, export_videos_to_json_files


@dataclass
class SyncJobState:
    creator: str
    status: str = "idle"
    started_at: str | None = None
    updated_at: str | None = None
    finished_at: str | None = None
    downloaded_posts: int = 0
    imported_posts: int = 0
    last_message: str = ""
    error: str | None = None
    running_pid: int | None = None
    fetched: bool = False
    source_type: str = "patreon"  # "patreon" or "cinebingers"


_job_lock = threading.Lock()
_sync_jobs: dict[str, SyncJobState] = {}


def _find_post_info_files(root: Path) -> list[Path]:
    # patreon-dl v3 writes post-api.json per post; older layouts used post_info*.
    candidates = list(root.rglob("post-api.json"))
    if not candidates:
        candidates = list(root.rglob("post_info*.json"))
    if not candidates:
        candidates = list(root.rglob("post_info*"))
    return [path for path in candidates if path.is_file()]


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _normalize_creator_key(creator: str) -> str:
    return creator.strip().lower()


def _get_or_create_job(creator: str) -> SyncJobState:
    key = _normalize_creator_key(creator)
    with _job_lock:
        job = _sync_jobs.get(key)
        if not job:
            job = SyncJobState(creator=creator)
            _sync_jobs[key] = job
        return job


def get_sync_job_snapshot(creator: str) -> dict:
    key = _normalize_creator_key(creator)
    with _job_lock:
        job = _sync_jobs.get(key)
        if not job:
            job = SyncJobState(creator=creator)
        elif job.status in {"queued", "running"} and job.running_pid:
            try:
                os.kill(job.running_pid, 0)
            except OSError:
                job.status = "failed"
                job.error = job.error or "Background sync process exited unexpectedly"
                job.last_message = job.last_message or "Background sync process exited unexpectedly"
                job.finished_at = job.finished_at or _utc_now()
                job.running_pid = None
        return asdict(job)


def _job_is_active(job: SyncJobState) -> bool:
    return job.status in {"queued", "running"}


def _update_job(creator: str, **changes) -> None:
    key = _normalize_creator_key(creator)
    with _job_lock:
        job = _sync_jobs.setdefault(key, SyncJobState(creator=creator))
        for field_name, value in changes.items():
            setattr(job, field_name, value)
        job.updated_at = _utc_now()


def _record_message(creator: str, message: str) -> None:
    _update_job(creator, last_message=message)


def _notify_scheduler(source: str, *, ok: bool, error: str | None = None, auth: bool = False) -> None:
    """Report a sync outcome to the scheduler (lazy import breaks a cycle)."""
    try:
        from app.scheduler import on_sync_result
        on_sync_result(source, ok=ok, error=error, auth=auth)
    except Exception:
        pass


def _count_posts_from_log_line(line: str, seen_ids: set[str]) -> int:
    if "Save post info #" in line:
        post_id = line.split("Save post info #", 1)[1].split()[0].strip()
        if post_id not in seen_ids:
            seen_ids.add(post_id)
            return 1
    return 0


def run_downloader(creator: str, on_output: Callable[[str], None] | None = None) -> bool:
    if not settings.patreon_cookie:
        raise AuthError("PATREON_COOKIE is not configured in .env")

    creator_output = settings.raw_root / creator
    creator_output.mkdir(parents=True, exist_ok=True)

    # Accept a full URL directly, or build one from the vanity.
    # Patreon uses two URL styles:
    #   https://www.patreon.com/creatorname/posts   (legacy)
    #   https://www.patreon.com/c/creatorname/posts (current)
    # We try the modern /c/ form first; patreon-dl will follow redirects
    # if the creator still uses the legacy form.
    if creator.startswith("http://") or creator.startswith("https://"):
        target_url = creator
    elif "/" in creator:  # e.g. "c/cinejump" entered manually
        target_url = f"https://www.patreon.com/{creator}/posts"
    else:
        # Try modern URL first — patreon-dl handles 302 redirects to legacy URLs too
        target_url = f"https://www.patreon.com/c/{creator}/posts"

    configured_command = shlex.split(settings.patreon_dl_command)
    run_args = ["-c", settings.patreon_cookie, "-o", str(creator_output), "-y"]
    metadata_config_file = ensure_metadata_only_config_file()
    if metadata_config_file:
        run_args.extend(["-C", metadata_config_file])
    elif settings.patreon_dl_config_file:
        run_args.extend(["-C", settings.patreon_dl_config_file])

    run_args.append(target_url)

    candidate_commands: list[list[str]] = [configured_command + run_args]
    if configured_command and configured_command[0].lower() == "patreon-dl":
        candidate_commands.append(["npx", "--yes", "patreon-dl", *run_args])

    last_not_found: FileNotFoundError | None = None
    for command in candidate_commands:
        executable = command[0]
        resolved_executable = shutil.which(executable)
        if resolved_executable is None:
            last_not_found = FileNotFoundError(f"Executable not found: {executable}")
            continue

        run_command = command
        if sys.platform.startswith("win") and resolved_executable.lower().endswith((".cmd", ".bat")):
            run_command = ["cmd", "/c", *command]

        process = subprocess.Popen(
            run_command,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            encoding="utf-8",
            errors="replace",
            bufsize=1,
            universal_newlines=True,
        )

        _update_job(creator, running_pid=process.pid)

        assert process.stdout is not None
        recent_lines: list[str] = []
        for line in process.stdout:
            clean_line = line.rstrip()
            if clean_line:
                recent_lines.append(clean_line)
                if on_output:
                    on_output(clean_line)

        return_code = process.wait()
        if return_code == 0:
            return True

        # Distinguish auth failures (expired/invalid cookie) from other errors
        # so the scheduler can pause and the UI can prompt for a refresh.
        tail = "\n".join(recent_lines[-80:]).lower()
        auth_markers = ("401", "403", "unauthorized", "forbidden", "not logged in",
                        "log in to", "authentication failed", "session expired",
                        "invalid session", "please login")
        if any(marker in tail for marker in auth_markers):
            raise AuthError(f"patreon-dl auth failure (exit code {return_code})")
        raise RuntimeError(f"patreon-dl failed: process exited with code {return_code}")

    raise RuntimeError(
        "patreon-dl command not found. Set PATREON_DL_COMMAND in .env to a valid command, for example 'patreon-dl' or 'npx patreon-dl'."
    ) from last_not_found


def scrape_cinebingers(
    creator: str,
    on_output: Callable[[str], None] | None = None,
    cookies: dict[str, str] | None = None,
    request_delay: float = 1.0,
) -> bool:
    """
    Scrape videos from cinebingers.ca and save them as post_info files.
    
    Args:
        creator: Creator/collection name (used as subdirectory)
        on_output: Optional callback for progress messages
        cookies: Optional cookies dict (for authentication if needed)
        request_delay: Delay in seconds between HTTP requests (default: 1.0)
    
    Returns:
        True if scraping was successful
    """
    creator_output = settings.raw_root / creator
    creator_output.mkdir(parents=True, exist_ok=True)
    
    try:
        if on_output:
            on_output("Starting cinebingers.ca scrape...")
        
        # Scrape all videos with rate limiting
        videos = scrape_all_cinebingers_videos(
            cookies=cookies,
            request_delay=request_delay,
            on_page_complete=lambda page, count: (
                on_output(f"Scraped page {page}, found {count} videos") if on_output else None
            )
        )
        
        if on_output:
            on_output(f"Total videos found: {len(videos)}")
        
        # Export to JSON files
        exported_files = export_videos_to_json_files(videos, creator_output)
        
        if on_output:
            on_output(f"Exported {len(exported_files)} video files to {creator_output}")
        
        return True
    except Exception as exc:
        if on_output:
            on_output(f"Error during scraping: {exc}")
        raise RuntimeError(f"cinebingers.ca scraping failed: {exc}") from exc


def _get_or_create_creator(session: Session, creator_name: str) -> Creator:
    creator = session.scalar(select(Creator).where(Creator.name == creator_name))
    if creator:
        return creator

    creator = Creator(name=creator_name)
    session.add(creator)
    session.flush()
    return creator


def _upsert_post(session: Session, creator: Creator, parsed: dict) -> None:
    post = session.scalar(
        select(Post).where(
            Post.creator_id == creator.id,
            Post.source_path == parsed["source_path"],
        )
    )

    if not post and parsed["post_id"]:
        post = session.scalar(
            select(Post).where(
                Post.creator_id == creator.id,
                Post.patreon_post_id == parsed["post_id"],
            )
        )

    if not post:
        post = Post(creator_id=creator.id, source_path=parsed["source_path"])
        session.add(post)

    post.patreon_post_id = parsed["post_id"]
    post.title = parsed["title"]
    post.content = parsed["content"]
    post.post_url = parsed["post_url"]
    post.published_at = parsed["published_at"]
    post.raw_json = parsed["raw_json"]
    post.source_path = parsed["source_path"]
    post.can_view = parsed["can_view"]

    # Tags are add-only: union the raw Patreon tags onto whatever is already
    # there. The tagger adds genre/type tags out-of-band, so a plain re-sync must
    # NOT replace the collection (that wiped those tags). Add-only also emits no
    # DELETEs, avoiding the stale "expected to delete N rows" crash on re-import.
    existing_tag_names = {tag.name for tag in post.tags}
    for tag_name in parsed["tags"]:
        tag = _get_or_create_tag(session, tag_name)
        if tag.name not in existing_tag_names:
            post.tags.append(tag)
            existing_tag_names.add(tag.name)

    # Links are fully derived from the raw JSON, so replace them only when the
    # set actually changed (avoids per-sync delete/insert churn across all posts).
    desired_links = list(dict.fromkeys(parsed["links"]))
    if {link.url for link in post.links} != set(desired_links):
        post.links = [Link(url=url) for url in desired_links]


def sync_creator(session: Session, creator_name: str, run_fetch: bool = True) -> dict:
    downloaded = False
    if run_fetch:
        downloaded = run_downloader(creator_name)

    creator_root = settings.raw_root / creator_name
    post_info_files = _find_post_info_files(creator_root)

    creator = _get_or_create_creator(session, creator_name)
    imported = 0

    for info_file in post_info_files:
        payload = parse_post_info_file(info_file)
        parsed = normalize_post(info_file, payload)
        _upsert_post(session, creator, parsed)
        imported += 1

    return {"creator": creator_name, "downloaded": downloaded, "imported_posts": imported}


def start_sync_job(creator_name: str, run_fetch: bool = True, source_type: str = "patreon") -> dict:
    key = _normalize_creator_key(creator_name)

    with _job_lock:
        existing = _sync_jobs.get(key)
        if existing and _job_is_active(existing):
            return asdict(existing)

        job = SyncJobState(
            creator=creator_name,
            status="queued",
            started_at=_utc_now(),
            updated_at=_utc_now(),
            downloaded_posts=0,
            imported_posts=0,
            last_message="Queued for sync",
            error=None,
            running_pid=None,
            fetched=False,
            source_type=source_type,
        )
        _sync_jobs[key] = job

    thread = threading.Thread(target=_run_sync_job, args=(creator_name, run_fetch, source_type), daemon=True)
    thread.start()
    return asdict(job)


def _run_sync_job(creator_name: str, run_fetch: bool, source_type: str = "patreon") -> None:
    key = _normalize_creator_key(creator_name)

    _update_job(creator_name, status="running", last_message="Starting sync")
    seen_post_ids: set[str] = set()

    log_path = settings.data_root / f"sync_{_normalize_creator_key(creator_name)}.log"

    try:
        log_file = open(log_path, "a", encoding="utf-8", buffering=1)
        log_file.write(f"\n=== Sync started {_utc_now()} ===\n")

        def handle_output(line: str) -> None:
            _record_message(creator_name, line)
            log_file.write(line + "\n")
            log_file.flush()
            increment = _count_posts_from_log_line(line, seen_post_ids)
            if increment:
                with _job_lock:
                    job = _sync_jobs[key]
                    job.downloaded_posts += increment
                    job.updated_at = _utc_now()

        downloaded = False
        if run_fetch:
            if source_type == "cinebingers":
                downloaded = scrape_cinebingers(creator_name, on_output=handle_output)
            else:
                downloaded = run_downloader(creator_name, on_output=handle_output)
        # The fetch subprocess (patreon-dl) has now exited. Clear running_pid so
        # the snapshot check doesn't see "running + dead pid" during the import
        # phase and falsely report "Background sync process exited unexpectedly".
        _update_job(creator_name, fetched=downloaded, running_pid=None)

        creator_root = settings.raw_root / creator_name
        post_info_files = _find_post_info_files(creator_root)

        imported = 0
        with SessionLocal() as db_session:
            creator = _get_or_create_creator(db_session, creator_name)
            for info_file in post_info_files:
                payload = parse_post_info_file(info_file)
                parsed = normalize_post(info_file, payload)
                _upsert_post(db_session, creator, parsed)
                imported += 1

            db_session.commit()

        if settings.prune_raw_media_after_sync:
            try:
                prune_raw_media(creator_name, on_output=handle_output)
            except Exception as prune_exc:  # never fail a sync over cleanup
                handle_output(f"WARNING: media prune failed: {prune_exc}")

        _update_job(
            creator_name,
            status="completed",
            downloaded_posts=max(_sync_jobs[key].downloaded_posts, len(post_info_files) if downloaded else imported),
            imported_posts=imported,
            last_message="Sync complete",
            finished_at=_utc_now(),
            running_pid=None,
        )
        _notify_scheduler("patreon", ok=True)
    except Exception as exc:
        _update_job(
            creator_name,
            status="failed",
            error=str(exc),
            last_message=str(exc),
            finished_at=_utc_now(),
            running_pid=None,
        )
        log_file.write(f"ERROR: {exc}\n")
        _notify_scheduler("patreon", ok=False, error=str(exc), auth=isinstance(exc, AuthError))
    finally:
        log_file.write(f"=== Sync ended {_utc_now()} ===\n")
        log_file.close()
