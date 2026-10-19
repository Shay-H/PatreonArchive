"""Tests for incremental auto-tagging behavior."""
import importlib
from datetime import datetime, timezone

import pytest
from sqlalchemy.orm import sessionmaker


@pytest.fixture()
def modules(tmp_path, monkeypatch):
    db_file = tmp_path / "test.db"
    monkeypatch.setenv("DATABASE_URL", f"sqlite:///{db_file}")
    monkeypatch.setenv("TMDB_API_KEY", "")

    import app.config
    importlib.reload(app.config)
    import app.database
    importlib.reload(app.database)
    import app.tagger
    tagger = importlib.reload(app.tagger)

    from app.models import Base
    Base.metadata.create_all(bind=app.database.engine)

    Session = sessionmaker(bind=app.database.engine)
    return tagger, Session


def test_tag_untagged_posts_skips_completed_posts(modules, monkeypatch):
    tagger, Session = modules

    from app.models import Creator, Post
    from app.tags import get_or_create_tag

    with Session() as db:
        creator = Creator(name="cinebingers", source_type="cinebingers", archive_mode="video")
        db.add(creator)
        db.flush()

        already_done = Post(
            creator_id=creator.id,
            title="Already Tagged",
            source_path="p1",
            auto_tagged_at=datetime(2026, 1, 1, tzinfo=timezone.utc),
        )
        already_done.tags.append(get_or_create_tag(db, "movie"))

        needs_tags = Post(
            creator_id=creator.id,
            title="Needs Tags",
            source_path="p2",
        )

        db.add_all([already_done, needs_tags])
        db.commit()

    calls = []

    def fake_compute_tags(title: str, api_key: str = "") -> list[str]:
        calls.append(title)
        return ["movie", "drama"]

    monkeypatch.setattr(tagger, "compute_tags", fake_compute_tags)

    assert tagger.tag_untagged_posts("cinebingers", api_key="") == 1
    assert calls == ["Needs Tags"]

    with Session() as db:
        posts = {post.source_path: post for post in db.query(Post).all()}
        # SQLite hands back DateTime(timezone=True) columns without tzinfo, so
        # compare the wall-clock value — the point is that an already-tagged
        # post's timestamp was left untouched.
        assert posts["p1"].auto_tagged_at.replace(tzinfo=timezone.utc) == datetime(
            2026, 1, 1, tzinfo=timezone.utc
        )
        assert {tag.name for tag in posts["p1"].tags} == {"movie"}
        assert posts["p2"].auto_tagged_at is not None
        assert {tag.name for tag in posts["p2"].tags} == {"movie", "drama"}
