"""Daily auto-refetch scheduler.

Runs a background thread that, once a day at AUTO_SYNC_HOUR, re-syncs every
known creator (routing the cinebingers creator to its scraper) and then runs a
tagging pass. State is persisted to data/scheduler_state.json so it survives
restarts.

If a sync fails because of auth (expired Patreon cookie / cinebingers session),
the schedule pauses itself and records which source failed; the frontend reads
that via /api/scheduler/status and prompts for a refresh. A successful manual
sync of that source clears the failure and un-pauses.
"""
from __future__ import annotations

import json
import threading
import time
from datetime import datetime, timezone

from sqlalchemy import select

from app.config import settings
from app.database import SessionLocal
from app.models import Creator

_state_lock = threading.Lock()
_started = False
_running = False  # a daily run is in progress


# ---------------------------------------------------------------------------
# Persisted state
# ---------------------------------------------------------------------------

def _state_path():
    return settings.data_root / "scheduler_state.json"


def _default_state() -> dict:
    return {
        "paused": False,
        "last_run": None,
        "last_status": None,     # "ok" | "failed" | "running"
        "last_message": "",
        "auth_failure": None,    # {"source", "message", "at"} or None
    }


def _load_state() -> dict:
    path = _state_path()
    if path.exists():
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
            if isinstance(data, dict):
                return {**_default_state(), **data}
        except Exception:
            pass
    return _default_state()


def _save_state(state: dict) -> None:
    _state_path().write_text(json.dumps(state, indent=2), encoding="utf-8")


def _update_state(**changes) -> dict:
    with _state_lock:
        state = _load_state()
        state.update(changes)
        _save_state(state)
        return state


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


# ---------------------------------------------------------------------------
# Auth-failure handling (called from the sync job runners)
# ---------------------------------------------------------------------------

_AUTH_MARKERS = {
    "patreon": (
        "patreon_cookie is not configured", "401", "403", "unauthorized",
        "forbidden", "not logged in", "log in to", "authentication failed",
        "session expired", "invalid session", "please login",
    ),
    "cinebingers": (
        "patreon login", "session may have expired", "401", "403",
        "forbidden", "authenticate", "login",
    ),
}


def _looks_like_auth(source: str, message: str | None) -> bool:
    m = (message or "").lower()
    return any(marker in m for marker in _AUTH_MARKERS.get(source, ()))


def on_sync_result(source: str, ok: bool, error: str | None = None, auth: bool = False) -> None:
    """Record the outcome of a sync (manual or scheduled).

    On success, clears any recorded auth failure for that source (and un-pauses
    if it was the cause). On an auth failure, records it and pauses the schedule.
    """
    with _state_lock:
        state = _load_state()
        failure = state.get("auth_failure")

        if ok:
            if failure and failure.get("source") == source:
                state["auth_failure"] = None
                state["paused"] = False
                _save_state(state)
            return

        if auth or _looks_like_auth(source, error):
            state["auth_failure"] = {
                "source": source,
                "message": error or "Authentication failed",
                "at": _utc_now(),
            }
            state["paused"] = True
            _save_state(state)


# ---------------------------------------------------------------------------
# Public API (used by main.py endpoints)
# ---------------------------------------------------------------------------

def get_scheduler_status() -> dict:
    state = _load_state()
    return {
        "enabled": settings.auto_sync_enabled,
        "hour": settings.auto_sync_hour,
        "running": _running,
        "paused": state.get("paused", False),
        "last_run": state.get("last_run"),
        "last_status": state.get("last_status"),
        "last_message": state.get("last_message", ""),
        "auth_failure": state.get("auth_failure"),
    }


def resume_scheduler() -> dict:
    """User action: clear the auth failure and un-pause."""
    _update_state(paused=False, auth_failure=None)
    return get_scheduler_status()


def run_scheduler_now() -> dict:
    """Trigger a daily run immediately (ignores the schedule, not the lock)."""
    if not _running:
        threading.Thread(target=_run_daily, daemon=True).start()
    return get_scheduler_status()


def start_scheduler() -> None:
    global _started
    if _started or not settings.auto_sync_enabled:
        return
    _started = True
    threading.Thread(target=_loop, daemon=True).start()


# ---------------------------------------------------------------------------
# The daily run
# ---------------------------------------------------------------------------

def _discover_creators() -> list[str]:
    with SessionLocal() as db:
        return [c.name for c in db.scalars(select(Creator).order_by(Creator.name)).all()]


def _wait(snapshot_fn, *, timeout: int = 7200) -> dict:
    """Poll a job snapshot until it completes/fails or times out."""
    deadline = time.time() + timeout
    snap = snapshot_fn()
    while snap.get("status") not in ("completed", "failed") and time.time() < deadline:
        time.sleep(5)
        snap = snapshot_fn()
    return snap


def _run_daily() -> None:
    global _running
    if _running:
        return
    _running = True
    _update_state(last_status="running", last_run=_utc_now(), last_message="Daily sync started")

    # Imported lazily to avoid an import cycle (those modules notify us back).
    from app.sync_service import start_sync_job, get_sync_job_snapshot
    from app.cinebingers_service import start_cinebingers_sync, get_job_snapshot as cb_snapshot
    from app.tagger import start_tagging, get_tag_job_snapshot

    try:
        for name in _discover_creators():
            if _load_state().get("paused"):
                _update_state(last_status="failed", last_message="Paused before completing")
                return

            if name == settings.cinebingers_creator_name:
                start_cinebingers_sync(name)
                _wait(cb_snapshot)
            else:
                start_sync_job(name, run_fetch=True)
                _wait(lambda: get_sync_job_snapshot(name))

            # An auth failure during the sync pauses us (via on_sync_result).
            if _load_state().get("paused"):
                _update_state(last_status="failed", last_message=f"Auth failure during '{name}' sync")
                return

        # Tagging pass so newly imported posts get type/genre tags.
        start_tagging()
        _wait(get_tag_job_snapshot)

        _update_state(last_status="ok", last_message="Daily sync complete")
    except Exception as exc:  # never let the scheduler thread die
        _update_state(last_status="failed", last_message=f"Scheduler error: {exc}")
    finally:
        _running = False


def _is_due() -> bool:
    now = datetime.now()
    if now.hour != settings.auto_sync_hour:
        return False
    last = _load_state().get("last_run")
    if last:
        try:
            if datetime.fromisoformat(last).astimezone().date() == now.date():
                return False  # already ran today
        except ValueError:
            pass
    return True


def _loop() -> None:
    while True:
        try:
            if settings.auto_sync_enabled and not _load_state().get("paused") and _is_due():
                _run_daily()
        except Exception:
            pass
        time.sleep(60)
