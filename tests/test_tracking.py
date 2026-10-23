"""Tests for post tracking: change detection, refresh intervals, and the
lost-access path (a lapsed pledge must never cost us the archived copy)."""
import importlib
from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy.orm import sessionmaker


@pytest.fixture()
def env(tmp_path, monkeypatch):
    monkeypatch.setenv("DATABASE_URL", f"sqlite:///{tmp_path / 'test.db'}")
    monkeypatch.setenv("PATREON_COOKIE", "session_id=abc")

    import app.config
    importlib.reload(app.config)
    import app.database
    importlib.reload(app.database)
    import app.patreon_service
    svc = importlib.reload(app.patreon_service)

    from app.models import Base
    Base.metadata.create_all(bind=app.database.engine)
    return svc, sessionmaker(bind=app.database.engine)


def _envelope(post_id="900", title="Master Post", body="<p>v1</p>", can_view=True):
    return {
        "data": {
            "id": post_id,
            "type": "post",
            "attributes": {
                "title": title,
                "content": body,
                "url": f"https://www.patreon.com/posts/{post_id}",
                "published_at": "2026-01-01T00:00:00.000+00:00",
                "current_user_can_view": can_view,
            },
            "relationships": {},
        },
        "included": [],
    }


def _seed(Session, **post_kw):
    from app.models import Creator, Post
    with Session() as db:
        creator = Creator(name="czepeku", source_type="patreon", archive_mode="links")
        db.add(creator)
        db.flush()
        post = Post(
            creator_id=creator.id,
            patreon_post_id="900",
            title="Master Post",
            content="<p>v1</p>",
            source_path="patreon_api/czepeku/900",
            raw_json="{}",
            tracked=True,
            **post_kw,
        )
        db.add(post)
        db.commit()
        return post.id


# --- change detection -------------------------------------------------------

def test_refresh_records_an_update_when_content_changes(env, monkeypatch):
    svc, Session = env
    post_id = _seed(Session)
    monkeypatch.setattr(svc, "fetch_post", lambda pid, ck: _envelope(body="<p>v2 — new maps</p>"))

    result = svc.refresh_tracked_posts()
    assert result == {"checked": 1, "updated": 1, "failed": 0, "skipped": 0, "stopped": 0}

    from app.models import Post
    with Session() as db:
        post = db.get(Post, post_id)
        assert post.content == "<p>v2 — new maps</p>"
        assert post.content_updated_at is not None
        assert post.last_checked_at is not None
        assert post.tracked is True


def test_refresh_leaves_content_updated_at_unset_when_nothing_changed(env, monkeypatch):
    svc, Session = env
    post_id = _seed(Session)
    monkeypatch.setattr(svc, "fetch_post", lambda pid, ck: _envelope())

    result = svc.refresh_tracked_posts()
    assert result["checked"] == 1 and result["updated"] == 0

    from app.models import Post
    with Session() as db:
        post = db.get(Post, post_id)
        assert post.content_updated_at is None   # nothing changed
        assert post.last_checked_at is not None  # but we did look


# --- lost access ------------------------------------------------------------

def test_lost_access_keeps_the_archived_copy_and_stops_tracking(env, monkeypatch):
    """A lapsed pledge returns the post unviewable and empty. Keep what we have."""
    svc, Session = env
    post_id = _seed(Session)
    monkeypatch.setattr(
        svc, "fetch_post", lambda pid, ck: _envelope(body="", can_view=False)
    )

    result = svc.refresh_tracked_posts()
    assert result["stopped"] == 1 and result["updated"] == 0

    from app.models import Post
    with Session() as db:
        post = db.get(Post, post_id)
        assert post.content == "<p>v1</p>", "archived copy must survive losing access"
        assert post.can_view is True, "stored access flag must not be downgraded"
        assert post.tracked is False
        assert "pledge" in post.tracking_stopped_reason.lower()


def test_creator_scrape_never_overwrites_with_a_locked_teaser(env):
    """The creator scrape re-saves the newest page of posts on every sync.
    After a pledge lapses those come back locked; the archive must keep the
    copy it has rather than replace it with the teaser."""
    svc, Session = env
    post_id = _seed(Session)
    from sqlalchemy import select
    from app.models import Creator, Post
    with Session() as db:
        creator = db.scalar(select(Creator))
        svc._upsert_post(db, creator, "czepeku", _envelope(body="<p>teaser</p>", can_view=False))
        db.commit()
    with Session() as db:
        post = db.get(Post, post_id)
        assert post.content == "<p>v1</p>"
        assert post.can_view is True

    # With access, a re-save still picks up edits.
    with Session() as db:
        creator = db.scalar(select(Creator))
        svc._upsert_post(db, creator, "czepeku", _envelope(body="<p>v2</p>"))
        db.commit()
    with Session() as db:
        assert db.get(Post, post_id).content == "<p>v2</p>"


def test_deleted_post_stops_tracking_but_keeps_the_copy(env, monkeypatch):
    svc, Session = env
    post_id = _seed(Session)
    monkeypatch.setattr(svc, "fetch_post", lambda pid, ck: None)

    assert svc.refresh_tracked_posts()["stopped"] == 1

    from app.models import Post
    with Session() as db:
        post = db.get(Post, post_id)
        assert post.content == "<p>v1</p>"
        assert post.tracked is False


def test_account_wide_auth_failure_does_not_untrack_anything(env, monkeypatch):
    """An expired cookie is not the same as losing access to one post."""
    svc, Session = env
    post_id = _seed(Session)

    from app.errors import AuthError

    def _expired(pid, ck):
        raise AuthError("Patreon returned 403 — PATREON_COOKIE may be expired")

    monkeypatch.setattr(svc, "fetch_post", _expired)
    with pytest.raises(AuthError):
        svc.refresh_tracked_posts()

    from app.models import Post
    with Session() as db:
        post = db.get(Post, post_id)
        assert post.tracked is True, "a bad cookie must not retire the whole library"
        assert post.tracking_stopped_reason is None


def test_transient_fetch_error_leaves_the_post_tracked(env, monkeypatch):
    svc, Session = env
    post_id = _seed(Session)

    def _boom(pid, ck):
        raise RuntimeError("FlareSolverr HTTP 500: challenge timeout")

    monkeypatch.setattr(svc, "fetch_post", _boom)
    assert svc.refresh_tracked_posts()["failed"] == 1

    from app.models import Post
    with Session() as db:
        assert db.get(Post, post_id).tracked is True


# --- refresh interval -------------------------------------------------------

def test_interval_skips_a_post_checked_too_recently(env, monkeypatch):
    svc, Session = env
    recent = datetime.now(timezone.utc) - timedelta(hours=2)
    _seed(Session, refresh_interval_hours=24, last_checked_at=recent)
    monkeypatch.setattr(svc, "fetch_post", lambda pid, ck: pytest.fail("should not fetch"))

    result = svc.refresh_tracked_posts()
    assert result["skipped"] == 1 and result["checked"] == 0


def test_interval_elapsed_means_the_post_is_checked(env, monkeypatch):
    svc, Session = env
    stale = datetime.now(timezone.utc) - timedelta(hours=30)
    _seed(Session, refresh_interval_hours=24, last_checked_at=stale)
    monkeypatch.setattr(svc, "fetch_post", lambda pid, ck: _envelope())

    assert svc.refresh_tracked_posts()["checked"] == 1


def test_zero_interval_means_every_sync(env, monkeypatch):
    svc, Session = env
    _seed(Session, refresh_interval_hours=0, last_checked_at=datetime.now(timezone.utc))
    monkeypatch.setattr(svc, "fetch_post", lambda pid, ck: _envelope())

    assert svc.refresh_tracked_posts()["checked"] == 1


# --- API --------------------------------------------------------------------

@pytest.fixture()
def client(tmp_path, monkeypatch):
    from fastapi.testclient import TestClient

    monkeypatch.setenv("DATABASE_URL", f"sqlite:///{tmp_path / 'api.db'}")
    monkeypatch.setenv("SESSION_SECRET", "test-secret")
    monkeypatch.setenv("SESSION_COOKIE_SECURE", "false")

    import app.config
    importlib.reload(app.config)
    import app.database
    importlib.reload(app.database)
    import app.auth
    importlib.reload(app.auth)
    import app.main
    main = importlib.reload(app.main)

    from app.auth import hash_password
    from app.models import Base, Creator, Post, User
    Base.metadata.create_all(bind=app.database.engine)

    Session = sessionmaker(bind=app.database.engine)
    # Seed inside the TestClient context: startup() installs the posts_fts
    # index and its triggers, and rows inserted before that exist outside the
    # index, which makes the first UPDATE corrupt the FTS query.
    with TestClient(main.app) as c:
        with Session() as db:
            db.add(User(username="t", password_hash=hash_password("pw")))
            patreon = Creator(name="czepeku", source_type="patreon", archive_mode="links")
            cine = Creator(name="cinebingers", source_type="cinebingers", archive_mode="video")
            db.add_all([patreon, cine])
            db.flush()
            db.add(Post(creator_id=patreon.id, patreon_post_id="900",
                        title="Master Post", source_path="p1"))
            db.add(Post(creator_id=cine.id, title="A video", source_path="p2"))
            db.commit()

        c.post("/api/login", data={"username": "t", "password": "pw"})
        yield c


def test_track_untrack_round_trip(client):
    assert client.get("/api/tracked").json() == []

    assert client.post("/api/posts/1/track").status_code == 204
    rows = client.get("/api/tracked").json()
    assert len(rows) == 1
    assert rows[0]["title"] == "Master Post" and rows[0]["tracked"] is True

    assert client.delete("/api/posts/1/track").status_code == 204
    assert client.get("/api/tracked").json() == []


def test_track_rejects_non_patreon_sources(client):
    res = client.post("/api/posts/2/track")
    assert res.status_code == 400
    assert "Patreon" in res.json()["detail"]


def test_interval_can_be_set_at_track_time_and_patched(client):
    client.post("/api/posts/1/track?interval_hours=24")
    assert client.get("/api/tracked").json()[0]["refresh_interval_hours"] == 24

    assert client.patch(
        "/api/posts/1/track", json={"refresh_interval_hours": 168}
    ).status_code == 204
    assert client.get("/api/tracked").json()[0]["refresh_interval_hours"] == 168


def test_post_summary_exposes_tracking_state(client):
    client.post("/api/posts/1/track")
    post = client.get("/api/posts/1").json()
    assert post["tracked"] is True
    assert post["content_updated_at"] is None


def test_stopped_posts_stay_listed_until_dismissed(client):
    """A lapsed pledge should be visible on the manage page, not vanish."""
    import app.database
    from app.models import Post
    Session = sessionmaker(bind=app.database.engine)
    with Session() as db:
        post = db.get(Post, 1)
        post.tracked = False
        post.tracking_stopped_reason = "No longer accessible — pledge may have ended."
        db.commit()

    rows = client.get("/api/tracked").json()
    assert len(rows) == 1 and rows[0]["tracked"] is False
    assert "pledge" in rows[0]["tracking_stopped_reason"]

    assert client.get("/api/tracked?include_stopped=false").json() == []

    client.delete("/api/posts/1/track")
    assert client.get("/api/tracked").json() == []
