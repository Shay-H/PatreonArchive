import json
import logging
import os
import threading

from fastapi import Depends, FastAPI, Form, HTTPException, Query, Request
from fastapi.responses import (
    FileResponse,
    HTMLResponse,
    JSONResponse,
    PlainTextResponse,
    RedirectResponse,
    Response,
)
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from starlette.middleware.sessions import SessionMiddleware
from sqlalchemy import and_, desc, func, or_, select, text, exists
from sqlalchemy.orm import Session, joinedload, selectinload, aliased

from app.auth import authenticate, is_public_path, require_user
from app.config import settings, get_session_secret
from app.database import SessionLocal, engine
from app.models import Attachment, Base, Creator, Favourite, Post, Tag, User, post_tags
from app.parser import extract_can_view, extract_rich_html, is_patreon_url
from app.sanitize import sanitize_html
from app.schemas import (
    AttachmentOut,
    CreatorOut,
    PostDetail,
    PostSummary,
    SyncResult,
    TagOut,
    TrackedPost,
    TrackUpdate,
)
from app.sync_service import get_sync_job_snapshot, start_sync_job
from app.cinebingers_service import start_cinebingers_sync, get_job_snapshot as get_cinebingers_snapshot
from app.tagger import start_tagging, get_tag_job_snapshot
from app.tags import lowercase_existing_tags
from app.scheduler import (
    start_scheduler,
    get_scheduler_status,
    resume_scheduler,
    run_scheduler_now,
)

# Archive modes a creator can be in (see Creator.archive_mode in app/models.py).
# TEXT_MODES are the two whose artifact is the post body rather than a
# watchable link: they render sanitized HTML, skip the require-links listing
# filter, and get no TMDB movie/tv/genre tags.
TEXT_MODES = ("content", "links")
ARCHIVE_MODES = ("video",) + TEXT_MODES

app = FastAPI(title=settings.app_name)
app.mount("/static", StaticFiles(directory="app/static"), name="static")
templates = Jinja2Templates(directory="app/templates")
_sync_guard = threading.Lock()


@app.middleware("http")
async def auth_gate(request: Request, call_next):
    """Require a logged-in session for everything except the public allowlist.

    Unauthenticated API calls get a 401 (so the SPA can react); any other path
    redirects to the login page. Registered before SessionMiddleware below so
    that SessionMiddleware ends up outermost and request.session is populated
    by the time this runs.
    """
    if is_public_path(request.url.path) or request.session.get("user_id"):
        return await call_next(request)
    if request.url.path.startswith("/api/"):
        return JSONResponse({"detail": "Not authenticated"}, status_code=401)
    return RedirectResponse(url="/login", status_code=303)


# Added last → outermost middleware, so request.session exists inside auth_gate.
app.add_middleware(
    SessionMiddleware,
    secret_key=get_session_secret(),
    session_cookie="archive_session",
    max_age=settings.session_max_age,
    same_site="lax",
    https_only=settings.session_cookie_secure,
)


@app.on_event("startup")
def startup() -> None:
    Base.metadata.create_all(bind=engine)

    if settings.database_url.startswith("sqlite"):
        with engine.begin() as conn:
            fts_exists = conn.execute(text(
                "SELECT 1 FROM sqlite_master WHERE type='table' AND name='posts_fts'"
            )).first() is not None
            conn.execute(
                text(
                    """
                    CREATE VIRTUAL TABLE IF NOT EXISTS posts_fts
                    USING fts5(title, content, content='posts', content_rowid='id');
                    """
                )
            )
            if not fts_exists:
                # External-content FTS starts empty, so any post that already
                # existed is absent from the index. The AFTER UPDATE trigger
                # then issues a 'delete' for an unindexed rowid, which SQLite
                # reports as "database disk image is malformed" on the next
                # query. Seed the index once, at creation, so it can't happen.
                conn.execute(text("INSERT INTO posts_fts(posts_fts) VALUES('rebuild')"))
            conn.execute(text("""CREATE TRIGGER IF NOT EXISTS posts_ai AFTER INSERT ON posts BEGIN
                INSERT INTO posts_fts(rowid, title, content) VALUES (new.id, new.title, new.content);
            END;"""))
            conn.execute(text("""CREATE TRIGGER IF NOT EXISTS posts_ad AFTER DELETE ON posts BEGIN
                INSERT INTO posts_fts(posts_fts, rowid, title, content) VALUES('delete', old.id, old.title, old.content);
            END;"""))
            conn.execute(text("""CREATE TRIGGER IF NOT EXISTS posts_au AFTER UPDATE ON posts BEGIN
                INSERT INTO posts_fts(posts_fts, rowid, title, content) VALUES('delete', old.id, old.title, old.content);
                INSERT INTO posts_fts(rowid, title, content) VALUES (new.id, new.title, new.content);
            END;"""))

    # Add the can_view column (if missing) and backfill access flags from
    # raw_json once, so locked posts can be hidden by default.
    if settings.database_url.startswith("sqlite"):
        try:
            with engine.begin() as conn:
                cols = [row[1] for row in conn.execute(text("PRAGMA table_info(posts)"))]
                if "can_view" not in cols:
                    conn.execute(text("ALTER TABLE posts ADD COLUMN can_view BOOLEAN NOT NULL DEFAULT 1"))
                    locked_ids = []
                    candidates = conn.execute(text(
                        "SELECT id, raw_json FROM posts WHERE raw_json LIKE '%current_user_can_view%'"
                    ))
                    for pid, raw in candidates:
                        try:
                            if extract_can_view(json.loads(raw)) is False:
                                locked_ids.append(pid)
                        except Exception:
                            pass
                    if locked_ids:
                        ids = ",".join(str(int(i)) for i in locked_ids)
                        conn.execute(text(f"UPDATE posts SET can_view=0 WHERE id IN ({ids})"))
        except Exception as exc:
            logging.getLogger("uvicorn.error").warning("can_view migration skipped: %s", exc)

    # Add source_type to creators (if missing) and backfill the cinebingers row.
    if settings.database_url.startswith("sqlite"):
        try:
            with engine.begin() as conn:
                cols = [row[1] for row in conn.execute(text("PRAGMA table_info(creators)"))]
                if "source_type" not in cols:
                    conn.execute(text(
                        "ALTER TABLE creators ADD COLUMN source_type VARCHAR(50) NOT NULL DEFAULT 'patreon'"
                    ))
                    conn.execute(
                        text("UPDATE creators SET source_type = 'cinebingers' WHERE name = :name"),
                        {"name": settings.cinebingers_creator_name},
                    )
        except Exception as exc:
            logging.getLogger("uvicorn.error").warning("source_type migration skipped: %s", exc)

    # Add archive_mode to creators (if missing). Existing creators are all video
    # archives, so the 'video' default backfills them correctly.
    if settings.database_url.startswith("sqlite"):
        try:
            with engine.begin() as conn:
                cols = [row[1] for row in conn.execute(text("PRAGMA table_info(creators)"))]
                if "archive_mode" not in cols:
                    conn.execute(text(
                        "ALTER TABLE creators ADD COLUMN archive_mode VARCHAR(20) NOT NULL DEFAULT 'video'"
                    ))
        except Exception as exc:
            logging.getLogger("uvicorn.error").warning("archive_mode migration skipped: %s", exc)

    # Add the tracking columns to posts (if missing). Existing rows default to
    # untracked with no check/update history, which is correct.
    if settings.database_url.startswith("sqlite"):
        try:
            with engine.begin() as conn:
                cols = [row[1] for row in conn.execute(text("PRAGMA table_info(posts)"))]
                if "tracked" not in cols:
                    conn.execute(text(
                        "ALTER TABLE posts ADD COLUMN tracked BOOLEAN NOT NULL DEFAULT 0"
                    ))
                if "last_checked_at" not in cols:
                    conn.execute(text("ALTER TABLE posts ADD COLUMN last_checked_at DATETIME NULL"))
                if "content_updated_at" not in cols:
                    conn.execute(text("ALTER TABLE posts ADD COLUMN content_updated_at DATETIME NULL"))
                if "refresh_interval_hours" not in cols:
                    conn.execute(text(
                        "ALTER TABLE posts ADD COLUMN refresh_interval_hours INTEGER NOT NULL DEFAULT 0"
                    ))
                if "tracking_stopped_reason" not in cols:
                    conn.execute(text(
                        "ALTER TABLE posts ADD COLUMN tracking_stopped_reason VARCHAR(200) NULL"
                    ))
        except Exception as exc:
            logging.getLogger("uvicorn.error").warning("tracking migration skipped: %s", exc)

    # Add auto_tagged_at to posts (if missing). Null means the shared
    # movie/tv/genre auto-tagger has never processed this row yet.
    if settings.database_url.startswith("sqlite"):
        try:
            with engine.begin() as conn:
                cols = [row[1] for row in conn.execute(text("PRAGMA table_info(posts)"))]
                if "auto_tagged_at" not in cols:
                    conn.execute(text("ALTER TABLE posts ADD COLUMN auto_tagged_at DATETIME NULL"))
        except Exception as exc:
            logging.getLogger("uvicorn.error").warning("auto_tagged_at migration skipped: %s", exc)

    # One-time normalization: lowercase any legacy mixed-case tags.
    # Best-effort — a migration hiccup must never block app startup.
    try:
        with SessionLocal() as db:
            changed = lowercase_existing_tags(db)
            if changed:
                db.commit()
    except Exception as exc:
        logging.getLogger("uvicorn.error").warning(
            "Tag lowercase migration skipped: %s", exc
        )

    # Start the daily auto-refetch scheduler (no-op if disabled in config).
    start_scheduler()


def _asset_version() -> str:
    """Cache-busting token: newest mtime across the static assets.

    Appended as ?v=<token> so browsers refetch JS/CSS whenever they change,
    instead of serving a stale heuristically-cached copy after a deploy.
    """
    static_dir = "app/static"
    try:
        latest = max(
            os.path.getmtime(os.path.join(static_dir, f))
            for f in os.listdir(static_dir)
            if f.endswith((".js", ".css"))
        )
        return str(int(latest))
    except (OSError, ValueError):
        return "0"


def get_db():
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()


@app.get("/", response_class=HTMLResponse)
def index(request: Request, db: Session = Depends(get_db)):
    uid = request.session.get("user_id")
    user = db.get(User, uid) if uid else None
    return templates.TemplateResponse(
        "index.html",
        {
            "request": request,
            "app_name": settings.app_name,
            "asset_version": _asset_version(),
            "username": user.username if user else "",
        },
    )


@app.get("/tracked", response_class=HTMLResponse)
def tracked_page(request: Request, db: Session = Depends(get_db)):
    """Manage page: what is tracked, how often, and what stopped."""
    uid = request.session.get("user_id")
    user = db.get(User, uid) if uid else None
    return templates.TemplateResponse(
        "tracked.html",
        {
            "request": request,
            "app_name": settings.app_name,
            "asset_version": _asset_version(),
            "username": user.username if user else "",
        },
    )


@app.get("/login", response_class=HTMLResponse)
def login_page(request: Request):
    if request.session.get("user_id"):
        return RedirectResponse(url="/", status_code=303)
    return templates.TemplateResponse(
        "login.html",
        {"request": request, "app_name": settings.app_name, "asset_version": _asset_version(), "error": ""},
    )


@app.post("/api/login")
def login(
    request: Request,
    username: str = Form(...),
    password: str = Form(...),
    db: Session = Depends(get_db),
):
    user = authenticate(db, username.strip(), password)
    if not user:
        return templates.TemplateResponse(
            "login.html",
            {
                "request": request,
                "app_name": settings.app_name,
                "asset_version": _asset_version(),
                "error": "Incorrect username or password.",
            },
            status_code=401,
        )
    request.session["user_id"] = user.id
    return RedirectResponse(url="/", status_code=303)


@app.post("/api/logout")
def logout(request: Request):
    request.session.clear()
    return RedirectResponse(url="/login", status_code=303)


@app.get("/favicon.ico", include_in_schema=False)
def favicon():
    return Response(status_code=204)


@app.get("/api/health")
def health():
    return {"status": "ok"}


@app.get("/api/config-status")
def config_status():
    return {
        "patreon_cookie_configured": bool(settings.patreon_cookie.strip()),
        "patreon_dl_command": settings.patreon_dl_command,
    }


@app.get("/api/scheduler/status")
def scheduler_status():
    return get_scheduler_status()


@app.post("/api/scheduler/resume")
def scheduler_resume():
    """Clear a recorded auth failure and un-pause the daily schedule."""
    return resume_scheduler()


@app.post("/api/scheduler/run-now", status_code=202)
def scheduler_run_now():
    """Trigger a daily auto-sync immediately (e.g. to test)."""
    return run_scheduler_now()


@app.get("/api/sync-status/{creator}")
def sync_status(creator: str, db: Session = Depends(get_db)):
    snapshot = get_sync_job_snapshot(creator)
    imported_posts_db = db.scalar(
        select(func.count(Post.id)).join(Creator).where(Creator.name == creator)
    ) or 0

    snapshot["imported_posts_db"] = imported_posts_db
    return snapshot


@app.post("/api/sync/{creator}", response_model=SyncResult, status_code=202)
def sync(creator: str, fetch: bool = Query(default=True)):
    if fetch and settings.patreon_fetch_disabled:
        fetch = False
    job = start_sync_job(creator, run_fetch=fetch)
    if job.get("status") == "running":
        return {"creator": creator, "downloaded": False, "imported_posts": 0}

    return {"creator": creator, "downloaded": False, "imported_posts": 0}


@app.post("/api/cinebingers/sync", status_code=202)
def cinebingers_sync():
    """Start a background scrape of cinebingers.ca and import into the database."""
    return start_cinebingers_sync()


@app.get("/api/cinebingers/status")
def cinebingers_status():
    """Return the current state of the cinebingers sync job."""
    return get_cinebingers_snapshot()


@app.post("/api/patreon/sync/{creator}", status_code=202)
def patreon_scrape(creator: str, mode: str | None = Query(default=None)):
    """Start a FlareSolverr-backed Patreon scrape for creator and import into the database.

    mode ("video" | "content" | "links") sets the creator's archive_mode before
    scraping; omit it to leave an existing creator's mode unchanged. "links"
    archives the post body and its URLs without downloading any files.
    """
    from app.patreon_service import start_patreon_scrape
    if mode is not None and mode not in ARCHIVE_MODES:
        raise HTTPException(
            status_code=400,
            detail=f"mode must be one of: {', '.join(sorted(ARCHIVE_MODES))}",
        )
    return start_patreon_scrape(creator, archive_mode=mode)


@app.get("/api/patreon/status/{creator}")
def patreon_scrape_status(creator: str):
    """Return the current state of the FlareSolverr Patreon scrape job for creator."""
    from app.patreon_service import get_patreon_scrape_snapshot
    return get_patreon_scrape_snapshot(creator)


@app.get("/api/sync-log/{creator}", response_class=PlainTextResponse)
def sync_log(creator: str):
    """Return the full log for a creator sync job."""
    log_path = settings.data_root / f"sync_{creator.strip().lower()}.log"
    if not log_path.exists():
        return PlainTextResponse("No log file found.", status_code=404)
    return PlainTextResponse(log_path.read_text(encoding="utf-8", errors="replace"))


@app.post("/api/tag", status_code=202)
def tag_posts():
    """Start a background job that tags all posts with genre/type tags."""
    return start_tagging()


@app.get("/api/tag/status")
def tag_status():
    """Return the current state of the tagging job."""
    return get_tag_job_snapshot()


@app.get("/api/tags", response_model=list[TagOut])
def list_tags(db: Session = Depends(get_db)):
    tags = db.scalars(select(Tag).order_by(Tag.name.asc())).all()
    return [TagOut(name=t.name) for t in tags]


@app.get("/api/creators", response_model=list[CreatorOut])
def list_creators(db: Session = Depends(get_db)):
    creators = db.scalars(select(Creator).order_by(Creator.name.asc())).all()
    return [
        CreatorOut(name=c.name, source_type=c.source_type, archive_mode=c.archive_mode)
        for c in creators
    ]


@app.get("/api/posts", response_model=list[PostSummary])
def list_posts(
    q: str | None = Query(default=None),
    creator: str | None = Query(default=None),
    tag: str | None = Query(default=None),
    limit: int = Query(default=50, ge=1, le=500),
    offset: int = Query(default=0, ge=0),
    include_locked: bool = Query(default=False),
    require_links: bool = Query(default=True),
    db: Session = Depends(get_db),
):
    stmt = (
        select(Post)
        .options(joinedload(Post.creator), joinedload(Post.tags), selectinload(Post.attachments))
        .order_by(desc(Post.published_at), Post.id)
    )

    # require_links and the creator filter both need a Creator join.
    if require_links or creator:
        stmt = stmt.join(Post.creator)

    filters = []
    if not include_locked:
        # Hide posts Patreon couldn't access (locked / no view permission).
        filters.append(Post.can_view.is_(True))

    if require_links:
        # Hide content-less *video* posts (polls, announcements, Q&A threads,
        # thank-yous): anything watchable has at least one link. Content- and
        # links-mode creators are exempt — their value is the body, not a
        # watchable link.
        filters.append(or_(Creator.archive_mode.in_(TEXT_MODES), Post.links.any()))

    if creator:
        filters.append(Creator.name == creator)

    if tag:
        tag_names = [t.strip() for t in tag.split(',') if t.strip()]
        for tag_name in tag_names:
            filters.append(
                exists().where(
                    and_(
                        post_tags.c.post_id == Post.id,
                        post_tags.c.tag_id == Tag.id,
                        Tag.name.ilike(f"%{tag_name}%"),
                    )
                )
            )

    if q:
        needle = f"%{q}%"
        filters.append(Post.title.ilike(needle))

    if filters:
        stmt = stmt.where(and_(*filters))

    posts = db.scalars(stmt.limit(limit).offset(offset)).unique().all()

    return [_post_summary(p) for p in posts]


def _post_summary(p: Post) -> PostSummary:
    return PostSummary(
        id=p.id,
        creator=p.creator.name,
        archive_mode=p.creator.archive_mode,
        title=p.title,
        content_preview=(p.content[:300] + "...") if len(p.content) > 300 else p.content,
        post_url=p.post_url,
        published_at=p.published_at,
        tags=[t.name for t in p.tags],
        attachment_count=len(p.attachments),
        tracked=p.tracked,
        content_updated_at=p.content_updated_at,
    )


@app.get("/api/favourites", response_model=list[PostSummary])
def list_favourites(user: User = Depends(require_user), db: Session = Depends(get_db)):
    """Posts the logged-in user has favourited, most-recently-starred first."""
    stmt = (
        select(Post)
        .join(Favourite, Favourite.post_id == Post.id)
        .options(joinedload(Post.creator), joinedload(Post.tags), selectinload(Post.attachments))
        .where(Favourite.user_id == user.id)
        .order_by(desc(Favourite.created_at), desc(Post.id))
    )
    posts = db.scalars(stmt).unique().all()
    return [_post_summary(p) for p in posts]


@app.post("/api/favourites/{post_id}", status_code=204)
def add_favourite(post_id: int, user: User = Depends(require_user), db: Session = Depends(get_db)):
    if not db.get(Post, post_id):
        raise HTTPException(status_code=404, detail="Post not found")
    already = db.scalar(
        select(Favourite).where(Favourite.user_id == user.id, Favourite.post_id == post_id)
    )
    if not already:
        db.add(Favourite(user_id=user.id, post_id=post_id))
        db.commit()
    return Response(status_code=204)


@app.delete("/api/favourites/{post_id}", status_code=204)
def remove_favourite(post_id: int, user: User = Depends(require_user), db: Session = Depends(get_db)):
    fav = db.scalar(
        select(Favourite).where(Favourite.user_id == user.id, Favourite.post_id == post_id)
    )
    if fav:
        db.delete(fav)
        db.commit()
    return Response(status_code=204)


def _tracked_out(p: Post) -> TrackedPost:
    return TrackedPost(
        id=p.id,
        creator=p.creator.name,
        title=p.title,
        post_url=p.post_url,
        tracked=p.tracked,
        refresh_interval_hours=p.refresh_interval_hours,
        last_checked_at=p.last_checked_at,
        content_updated_at=p.content_updated_at,
        tracking_stopped_reason=p.tracking_stopped_reason,
    )


@app.get("/api/tracked", response_model=list[TrackedPost])
def list_tracked(
    include_stopped: bool = Query(default=True),
    db: Session = Depends(get_db),
):
    """Posts being tracked, plus (by default) ones tracking gave up on.

    The stopped ones are listed so a lapsed pledge is visible rather than
    silently dropping out of the page.
    """
    condition = Post.tracked.is_(True)
    if include_stopped:
        condition = or_(condition, Post.tracking_stopped_reason.is_not(None))
    posts = db.scalars(
        select(Post)
        .options(joinedload(Post.creator))
        .where(condition)
        .order_by(desc(Post.tracked), desc(Post.content_updated_at), Post.title)
    ).unique().all()
    return [_tracked_out(p) for p in posts]


@app.post("/api/posts/{post_id}/track", status_code=204)
def track_post(
    post_id: int,
    interval_hours: int = Query(default=0, ge=0, le=24 * 30),
    db: Session = Depends(get_db),
):
    """Mark a post for re-checking on every sync.

    Tracking is a property of the post, not of the viewer: it changes what the
    scraper fetches, so one user tracking a post refreshes it for everyone.
    Re-tracking clears any earlier auto-stop reason — the user is saying they
    expect access again.
    """
    post = db.get(Post, post_id)
    if not post:
        raise HTTPException(status_code=404, detail="Post not found")
    if post.creator.source_type != "patreon":
        raise HTTPException(
            status_code=400,
            detail="Only Patreon posts can be tracked — other sources have no per-post fetch",
        )
    post.tracked = True
    post.refresh_interval_hours = interval_hours
    post.tracking_stopped_reason = None
    db.commit()
    return Response(status_code=204)


@app.patch("/api/posts/{post_id}/track", status_code=204)
def set_track_interval(post_id: int, body: TrackUpdate, db: Session = Depends(get_db)):
    """Change how often a tracked post is re-checked (0 = every sync)."""
    if not 0 <= body.refresh_interval_hours <= 24 * 30:
        raise HTTPException(status_code=400, detail="interval must be 0–720 hours")
    post = db.get(Post, post_id)
    if not post:
        raise HTTPException(status_code=404, detail="Post not found")
    post.refresh_interval_hours = body.refresh_interval_hours
    db.commit()
    return Response(status_code=204)


@app.delete("/api/posts/{post_id}/track", status_code=204)
def untrack_post(post_id: int, db: Session = Depends(get_db)):
    """Stop tracking. Clears the auto-stop reason too, so an untracked post
    disappears from the manage page instead of lingering as a stopped row."""
    post = db.get(Post, post_id)
    if post and (post.tracked or post.tracking_stopped_reason):
        post.tracked = False
        post.tracking_stopped_reason = None
        db.commit()
    return Response(status_code=204)


@app.post("/api/tracked/refresh", status_code=202)
def refresh_tracked(creator: str | None = Query(default=None)):
    """Start a tracked-post re-check now, without waiting for the next sync.

    Returns immediately — each post costs a Cloudflare solve, so poll
    /api/tracked/refresh/status for progress.
    """
    from app.patreon_service import start_tracked_refresh
    return start_tracked_refresh(creator)


@app.get("/api/tracked/refresh/status")
def refresh_tracked_status():
    """Current state of the tracked-post refresh job."""
    from app.patreon_service import get_tracked_refresh_snapshot
    return get_tracked_refresh_snapshot()


@app.get("/api/posts/{post_id}", response_model=PostDetail)
def get_post(post_id: int, db: Session = Depends(get_db)):
    post = db.scalar(
        select(Post)
        .where(Post.id == post_id)
        .options(
            joinedload(Post.creator),
            joinedload(Post.tags),
            joinedload(Post.links),
            joinedload(Post.attachments),
        )
    )
    if not post:
        return PostDetail(
            id=0,
            creator="",
            archive_mode="video",
            title="Not found",
            content_preview="",
            content="",
            content_html="",
            post_url=None,
            published_at=None,
            tags=[],
            links=[],
            patreon_links=[],
            attachments=[],
        )

    is_text_mode = post.creator.archive_mode in TEXT_MODES
    # For content and links creators, rebuild the rich HTML body from raw_json
    # (Patreon's pre-rendered `content` HTML, or its ProseMirror
    # content_json_string). The stored `content` column is plain text, so
    # sanitizing it directly would collapse the post into one unformatted
    # block. Fall back to plain content if the post has no rich source. Video
    # posts keep using <pre> plain text.
    content_html = ""
    if is_text_mode:
        rich = ""
        if post.raw_json:
            try:
                rich = extract_rich_html(json.loads(post.raw_json))
            except (ValueError, TypeError):
                rich = ""
        content_html = sanitize_html(rich or post.content)
    return PostDetail(
        id=post.id,
        creator=post.creator.name,
        archive_mode=post.creator.archive_mode,
        title=post.title,
        content_preview=(post.content[:300] + "...") if len(post.content) > 300 else post.content,
        content=post.content,
        content_html=content_html,
        post_url=post.post_url,
        published_at=post.published_at,
        tags=[t.name for t in post.tags],
        attachment_count=len(post.attachments),
        tracked=post.tracked,
        content_updated_at=post.content_updated_at,
        last_checked_at=post.last_checked_at,
        links=[link.url for link in post.links if not is_patreon_url(link.url)],
        patreon_links=[link.url for link in post.links if is_patreon_url(link.url)],
        attachments=[
            AttachmentOut(
                id=a.id,
                filename=a.filename,
                kind=a.kind,
                size_bytes=a.size_bytes,
                downloaded=a.downloaded,
            )
            for a in post.attachments
        ],
    )


@app.get("/api/posts/{post_id}/attachments/{attachment_id}")
def download_post_attachment(post_id: int, attachment_id: int, db: Session = Depends(get_db)):
    """Serve an archived attachment file from disk."""
    att = db.scalar(
        select(Attachment).where(
            Attachment.id == attachment_id, Attachment.post_id == post_id
        )
    )
    if not att:
        raise HTTPException(status_code=404, detail="Attachment not found")
    if not att.downloaded or not att.local_path or not os.path.exists(att.local_path):
        raise HTTPException(status_code=404, detail="Attachment file not archived")
    return FileResponse(
        att.local_path,
        filename=att.filename,
        media_type=att.mimetype or "application/octet-stream",
    )
