"""
Auto-tagger for posts.

Tags applied:
  - "movie" or "tv"        — from title pattern (episode numbers)
  - "anime"                — TV shows with JP origin on TMDB, or known anime list
  - genre names            — from TMDB (Action, Drama, Sci-Fi, etc.)

Requires TMDB_API_KEY in .env for genre lookup. Without it, only movie/tv/anime
tags are applied using pattern matching.
"""
from __future__ import annotations

import re
import threading
import time
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from functools import lru_cache

import requests
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.database import SessionLocal
from app.models import Creator, Post, Tag
from app.tags import get_or_create_tag as _get_or_create_tag

# ---------------------------------------------------------------------------
# Title parsing
# ---------------------------------------------------------------------------

# Matches titles that end with episode numbers, e.g.
#   "Cowboy Bebop 110-112"  "Star Trek: TNG 425-426"  "The Wire 101"
_EP_PATTERN = re.compile(r'\s+\d{2,3}(?:[,&\s-]+\d{2,3})*\s*(?:OVA\s*\d+[&,\d\s]*)?\s*$', re.I)
# SxxExx (e.g. "S02E09"), bare "S2"/"Season 2", or NxM ("2x09") episode codes.
_SEASON_PATTERN = re.compile(r'\bS\d{1,2}E\d{1,3}\b|\bS\d+\b|\bSeason\s+\d+\b|\b\d{1,2}x\d{1,3}\b', re.I)

# Known anime titles (show name only, lowercased) — used as fallback when TMDB
# doesn't have origin-country data.
_KNOWN_ANIME: set[str] = {
    "cowboy bebop", "attack on titan", "chainsaw man", "demon slayer",
    "food wars", "frieren", "one punch man", "my hero academia",
    "fullmetal alchemist", "death note", "naruto", "bleach", "dragon ball",
    "jujutsu kaisen", "sword art online", "hunter x hunter", "one piece",
}


def _is_tv(title: str) -> bool:
    return bool(_EP_PATTERN.search(title) or _SEASON_PATTERN.search(title))


def _clean_show_name(title: str) -> str:
    """Strip episode codes and parentheticals to get the bare show name."""
    name = _EP_PATTERN.sub("", title).strip()
    name = _SEASON_PATTERN.sub("", name).strip(" :-")
    name = re.sub(r"\([^)]*\)", "", name)          # drop "(Finale)", "(2024)", etc.
    name = re.sub(r"\s{2,}", " ", name)
    return name.strip(" :-")


def _search_title(title: str, tv: bool) -> str:
    """Title cleaned for a TMDB search (drops the recurring 'Watchalong')."""
    base = _clean_show_name(title) if tv else title
    return re.sub(r"\bwatchalong!?\b", "", base, flags=re.I).strip(" :-")


# ---------------------------------------------------------------------------
# TMDB helpers
# ---------------------------------------------------------------------------

TMDB_BASE = "https://api.themoviedb.org/3"
_GENRE_CACHE: dict[str, dict[int, str]] = {}   # "movie"/"tv" -> {id: name}
_SESSION = requests.Session()
_SESSION.headers["Accept"] = "application/json"


def _tmdb_get(path: str, api_key: str, **params) -> dict | None:
    try:
        r = _SESSION.get(
            f"{TMDB_BASE}{path}",
            params={"api_key": api_key, **params},
            timeout=8,
        )
        r.raise_for_status()
        return r.json()
    except Exception:
        return None


def _load_genres(api_key: str, media_type: str) -> dict[int, str]:
    if media_type in _GENRE_CACHE:
        return _GENRE_CACHE[media_type]
    data = _tmdb_get(f"/genre/{media_type}/list", api_key, language="en-US")
    mapping = {g["id"]: g["name"] for g in (data or {}).get("genres", [])}
    _GENRE_CACHE[media_type] = mapping
    return mapping


def _lookup_tmdb(title: str, is_tv_show: bool, api_key: str) -> dict | None:
    """Return first TMDB result dict, or None."""
    media_type = "tv" if is_tv_show else "movie"
    data = _tmdb_get(f"/search/{media_type}", api_key, query=title, language="en-US", page=1)
    results = (data or {}).get("results", [])
    return results[0] if results else None


def _get_genre_names(genre_ids: list[int], media_type: str, api_key: str) -> list[str]:
    mapping = _load_genres(api_key, media_type)
    return [mapping[gid] for gid in genre_ids if gid in mapping]


def _is_anime(result: dict, show_name: str) -> bool:
    """Check TMDB result for Japanese animation, or fall back to known list."""
    if result:
        origin = result.get("origin_country", [])
        genre_ids = result.get("genre_ids", [])
        # 16 = Animation genre on TMDB
        if "JP" in origin and 16 in genre_ids:
            return True
    return show_name.lower() in _KNOWN_ANIME


# ---------------------------------------------------------------------------
# Core tagging function
# ---------------------------------------------------------------------------

def compute_tags(title: str, api_key: str = "") -> list[str]:
    """
    Return a list of tag names for the given title.
    Always returns at least ["movie"] or ["tv"].
    """
    tv = _is_tv(title)
    tags: list[str] = ["tv" if tv else "movie"]

    if not api_key:
        # Pattern-only: check known anime for TV shows
        if tv and _clean_show_name(title).lower() in _KNOWN_ANIME:
            tags.append("anime")
        return tags

    media_type = "tv" if tv else "movie"
    lookup_title = _search_title(title, tv)
    result = _lookup_tmdb(lookup_title, tv, api_key)

    if result:
        genre_ids = result.get("genre_ids", [])
        genre_names = _get_genre_names(genre_ids, media_type, api_key)
        tags.extend(g.lower() for g in genre_names)
        if tv and _is_anime(result, lookup_title):
            if "anime" not in tags:
                tags.append("anime")
    else:
        # No TMDB result — still check known anime list
        if tv and lookup_title.lower() in _KNOWN_ANIME:
            tags.append("anime")

    return list(dict.fromkeys(tags))  # deduplicate, preserve order


# ---------------------------------------------------------------------------
# Job state
# ---------------------------------------------------------------------------

@dataclass
class TagJobState:
    status: str = "idle"
    started_at: str | None = None
    finished_at: str | None = None
    processed: int = 0
    total: int = 0
    last_message: str = ""
    error: str | None = None


_job_lock = threading.Lock()
_job = TagJobState()


def get_tag_job_snapshot() -> dict:
    with _job_lock:
        return asdict(_job)


def _update(**kw):
    with _job_lock:
        for k, v in kw.items():
            setattr(_job, k, v)


# ---------------------------------------------------------------------------
# Background tagging runner
# ---------------------------------------------------------------------------

def _run_tagging(api_key: str) -> None:
    _update(
        status="running",
        started_at=datetime.now(timezone.utc).isoformat(),
        finished_at=None,
        processed=0,
        error=None,
        last_message="Starting…",
    )
    try:
        with SessionLocal() as db:
            posts = db.scalars(select(Post)).all()
            total = len(posts)
            _update(total=total, last_message=f"Tagging {total} posts…")

            for i, post in enumerate(posts):
                computed = compute_tags(post.title, api_key)
                # Merge: add type/genre tags onto the post's existing tags
                # (lowercased) rather than replacing them.
                names = {t.name.lower() for t in post.tags} | set(computed)
                post.tags = [_get_or_create_tag(db, t) for t in sorted(names)]

                if (i + 1) % 50 == 0:
                    db.commit()
                    _update(processed=i + 1, last_message=f"Tagged {i+1}/{total}…")

                # Rate-limit TMDB calls: ~40 req/s allowed, we do 1 per post
                if api_key:
                    time.sleep(0.05)

            db.commit()
            _update(
                status="completed",
                finished_at=datetime.now(timezone.utc).isoformat(),
                processed=total,
                last_message=f"Done — {total} posts tagged",
            )
    except Exception as exc:
        _update(
            status="failed",
            finished_at=datetime.now(timezone.utc).isoformat(),
            error=str(exc),
            last_message=str(exc),
        )


def start_tagging() -> dict:
    """Start background tagging job. Returns immediately with job state."""
    from app.config import settings
    with _job_lock:
        if _job.status == "running":
            return asdict(_job)
    thread = threading.Thread(target=_run_tagging, args=(settings.tmdb_api_key,), daemon=True)
    thread.start()
    return get_tag_job_snapshot()
