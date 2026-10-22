"""Tests for login/session auth and per-user favourites.

Covers the password hashing helpers and an end-to-end round-trip against a
temporary SQLite database: unauthenticated access is blocked, login works, and
favourites persist per user.
"""
import importlib

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from app.auth import hash_password, verify_password


# --- Password hashing -------------------------------------------------------

def test_password_round_trip():
    stored = hash_password("correct horse battery staple")
    assert verify_password("correct horse battery staple", stored)
    assert not verify_password("wrong password", stored)


def test_password_hash_is_salted():
    # Two hashes of the same password differ (random per-hash salt).
    a = hash_password("hunter2")
    b = hash_password("hunter2")
    assert a != b
    assert verify_password("hunter2", a)
    assert verify_password("hunter2", b)


def test_verify_rejects_garbage():
    assert not verify_password("x", "not-a-valid-hash")
    assert not verify_password("x", "")


# --- End-to-end auth + favourites ------------------------------------------

@pytest.fixture()
def client(tmp_path, monkeypatch):
    """A TestClient wired to a throwaway SQLite DB with one seeded user."""
    db_file = tmp_path / "test.db"
    monkeypatch.setenv("DATABASE_URL", f"sqlite:///{db_file}")
    monkeypatch.setenv("SESSION_SECRET", "test-secret")
    # Pin the cookie to non-secure: TestClient speaks http://testserver, so a
    # developer's real .env setting SESSION_COOKIE_SECURE=true (correct in
    # production, behind nginx TLS) would stop the session cookie from ever
    # coming back and fail every logged-in assertion below.
    monkeypatch.setenv("SESSION_COOKIE_SECURE", "false")
    monkeypatch.setenv("FEED_TOKEN", "feed-secret")

    # Reload config + database so they pick up the patched env, then main.
    import app.config
    importlib.reload(app.config)
    import app.database
    importlib.reload(app.database)
    import app.auth
    importlib.reload(app.auth)
    import app.main
    main = importlib.reload(app.main)

    from app.models import Base, Creator, Link, Post, User
    Base.metadata.create_all(bind=app.database.engine)

    Session = sessionmaker(bind=app.database.engine)
    with Session() as db:
        db.add(User(username="tester", password_hash=hash_password("pw")))
        creator = Creator(name="cre", source_type="patreon", archive_mode="content")
        db.add(creator)
        db.flush()
        post = Post(creator_id=creator.id, title="A post", content="body", source_path="p1")
        db.add(post)
        db.flush()
        db.add(Link(post_id=post.id, url="https://example.com/a/module.json"))
        db.add(Link(post_id=post.id, url="https://www.patreon.com/posts/123"))
        db.add(Link(post_id=post.id, url="https://example.com/b/module.json"))
        db.commit()

    with TestClient(main.app) as c:
        yield c


def test_requires_auth(client):
    # No session → API is 401, health stays public.
    assert client.get("/api/posts").status_code == 401
    assert client.get("/api/health").status_code == 200


def test_login_and_favourite_round_trip(client):
    # Wrong password rejected.
    bad = client.post("/api/login", data={"username": "tester", "password": "nope"})
    assert bad.status_code == 401

    # Correct login (TestClient follows the 303 back to "/").
    ok = client.post("/api/login", data={"username": "tester", "password": "pw"})
    assert ok.status_code == 200

    # Empty to start.
    assert client.get("/api/favourites").json() == []

    # Favourite the seeded post (id 1).
    assert client.post("/api/favourites/1").status_code == 204
    favs = client.get("/api/favourites").json()
    assert [p["id"] for p in favs] == [1]

    # Idempotent add, then remove.
    assert client.post("/api/favourites/1").status_code == 204
    assert client.delete("/api/favourites/1").status_code == 204
    assert client.get("/api/favourites").json() == []

    # Favouriting a missing post → 404.
    assert client.post("/api/favourites/9999").status_code == 404


def test_logout_clears_session(client):
    client.post("/api/login", data={"username": "tester", "password": "pw"})
    assert client.get("/api/favourites").status_code == 200
    client.post("/api/logout")
    assert client.get("/api/favourites").status_code == 401


# --- Feed token ---------------------------------------------------------------

FEED_AUTH = {"Authorization": "Bearer feed-secret"}


def test_feed_token_reads_post_links(client):
    resp = client.get("/api/posts/1/links", headers=FEED_AUTH)
    assert resp.status_code == 200
    body = resp.json()
    assert body["post_id"] == 1
    # Insertion order, Patreon self-links dropped.
    assert body["links"] == [
        "https://example.com/a/module.json",
        "https://example.com/b/module.json",
    ]


def test_feed_token_is_scoped_to_links_endpoint(client):
    assert client.get("/api/posts", headers=FEED_AUTH).status_code == 401
    assert client.get("/api/posts/1", headers=FEED_AUTH).status_code == 401
    assert client.delete("/api/posts/1/track", headers=FEED_AUTH).status_code == 401


def test_feed_rejects_missing_or_wrong_token(client):
    assert client.get("/api/posts/1/links").status_code == 401
    bad = {"Authorization": "Bearer nope"}
    assert client.get("/api/posts/1/links", headers=bad).status_code == 401


def test_feed_unknown_post_is_404(client):
    assert client.get("/api/posts/9999/links", headers=FEED_AUTH).status_code == 404


def test_links_endpoint_works_with_session(client):
    client.post("/api/login", data={"username": "tester", "password": "pw"})
    assert client.get("/api/posts/1/links").status_code == 200
