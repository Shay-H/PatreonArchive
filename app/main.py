import threading

from fastapi import Depends, FastAPI, HTTPException, Query, Request
from fastapi.responses import HTMLResponse, PlainTextResponse, Response
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from sqlalchemy import and_, desc, func, or_, select, text, exists
from sqlalchemy.orm import Session, joinedload, aliased

from app.config import settings
from app.database import SessionLocal, engine
from app.models import Base, Creator, Post, Tag, post_tags
from app.schemas import CreatorOut, PostDetail, PostSummary, SyncResult, TagOut
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

app = FastAPI(title=settings.app_name)
app.mount("/static", StaticFiles(directory="app/static"), name="static")
templates = Jinja2Templates(directory="app/templates")
_sync_guard = threading.Lock()


@app.on_event("startup")
def startup() -> None:
    Base.metadata.create_all(bind=engine)

    if settings.database_url.startswith("sqlite"):
        with engine.begin() as conn:
            conn.execute(
                text(
                    """
                    CREATE VIRTUAL TABLE IF NOT EXISTS posts_fts
                    USING fts5(title, content, content='posts', content_rowid='id');
                    """
                )
            )
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

    # One-time normalization: lowercase any legacy mixed-case tags.
    with SessionLocal() as db:
        changed = lowercase_existing_tags(db)
        if changed:
            db.commit()

    # Start the daily auto-refetch scheduler (no-op if disabled in config).
    start_scheduler()


@app.get("/", response_class=HTMLResponse)
def index(request: Request):
    return templates.TemplateResponse("index.html", {"request": request, "app_name": settings.app_name})


@app.get("/favicon.ico", include_in_schema=False)
def favicon():
    return Response(status_code=204)


def get_db():
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()


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
    return [CreatorOut(name=c.name) for c in creators]


@app.get("/api/posts", response_model=list[PostSummary])
def list_posts(
    q: str | None = Query(default=None),
    creator: str | None = Query(default=None),
    tag: str | None = Query(default=None),
    limit: int = Query(default=50, ge=1, le=500),
    offset: int = Query(default=0, ge=0),
    db: Session = Depends(get_db),
):
    stmt = select(Post).options(joinedload(Post.creator), joinedload(Post.tags)).order_by(desc(Post.published_at), Post.id)

    filters = []
    if creator:
        stmt = stmt.join(Post.creator)
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

    return [
        PostSummary(
            id=p.id,
            creator=p.creator.name,
            title=p.title,
            content_preview=(p.content[:300] + "...") if len(p.content) > 300 else p.content,
            post_url=p.post_url,
            published_at=p.published_at,
            tags=[t.name for t in p.tags],
        )
        for p in posts
    ]


@app.get("/api/posts/{post_id}", response_model=PostDetail)
def get_post(post_id: int, db: Session = Depends(get_db)):
    post = db.scalar(
        select(Post)
        .where(Post.id == post_id)
        .options(joinedload(Post.creator), joinedload(Post.tags), joinedload(Post.links))
    )
    if not post:
        return PostDetail(
            id=0,
            creator="",
            title="Not found",
            content_preview="",
            content="",
            post_url=None,
            published_at=None,
            tags=[],
            links=[],
        )

    return PostDetail(
        id=post.id,
        creator=post.creator.name,
        title=post.title,
        content_preview=(post.content[:300] + "...") if len(post.content) > 300 else post.content,
        content=post.content,
        post_url=post.post_url,
        published_at=post.published_at,
        tags=[t.name for t in post.tags],
        links=[link.url for link in post.links],
    )
