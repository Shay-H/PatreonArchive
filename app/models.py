from datetime import datetime

from sqlalchemy import Boolean, Column, DateTime, ForeignKey, Integer, String, Table, Text, UniqueConstraint, func
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship


class Base(DeclarativeBase):
    pass


post_tags = Table(
    "post_tags",
    Base.metadata,
    Column("post_id", ForeignKey("posts.id", ondelete="CASCADE"), primary_key=True),
    Column("tag_id", ForeignKey("tags.id", ondelete="CASCADE"), primary_key=True),
)


class Creator(Base):
    __tablename__ = "creators"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    name: Mapped[str] = mapped_column(String(255), unique=True, index=True)
    # Integration type: "patreon" | "cinebingers" | any future type.
    # Drives scheduler routing — add a new value when adding a new integration.
    source_type: Mapped[str] = mapped_column(String(50), default="patreon", server_default="patreon")
    # What we archive / how posts are presented, independent of source_type:
    #   "video"   — value is a watchable link; listings hide content-less posts,
    #               and the TMDB genre/type tagger runs.
    #   "content" — the post body (and attached files) is the artifact; bodies are
    #               rendered as sanitized HTML, attachments are downloaded, and the
    #               require-links filter / TMDB tagger are skipped.
    #   "links"   — same as "content" minus the bytes: the body is archived as
    #               sanitized HTML and every URL the post references is stored,
    #               but attachments are only ever recorded as metadata, never
    #               downloaded. For creators whose files are far too large to
    #               mirror (map packs, VTT bundles) and whose real payload is
    #               the off-site links in the post.
    archive_mode: Mapped[str] = mapped_column(String(20), default="video", server_default="video")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())

    posts: Mapped[list["Post"]] = relationship(back_populates="creator", cascade="all, delete-orphan")


class Post(Base):
    __tablename__ = "posts"
    __table_args__ = (UniqueConstraint("creator_id", "source_path", name="uq_post_creator_source_path"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    creator_id: Mapped[int] = mapped_column(ForeignKey("creators.id", ondelete="CASCADE"), index=True)

    patreon_post_id: Mapped[str | None] = mapped_column(String(64), index=True)
    title: Mapped[str] = mapped_column(String(500), default="(untitled)")
    content: Mapped[str] = mapped_column(Text, default="")
    post_url: Mapped[str | None] = mapped_column(String(1000))
    published_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), index=True)
    auto_tagged_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), index=True)

    source_path: Mapped[str] = mapped_column(String(1000))
    raw_json: Mapped[str] = mapped_column(Text, default="{}")

    # False when Patreon reported the user couldn't view the post (locked /
    # no access). These are hidden from listings by default.
    can_view: Mapped[bool] = mapped_column(Boolean, default=True, server_default="1", index=True)

    # Tracked posts are re-fetched individually on every sync and their saved
    # copy refreshed. The incremental scrape stops at the first page of
    # already-known posts, so a living post that keeps gaining content (a
    # "master post" index, a running changelog) would otherwise be frozen at
    # whatever it said the day it was first imported.
    tracked: Mapped[bool] = mapped_column(Boolean, default=False, server_default="0", index=True)
    # When the tracking refresh last looked at this post, whether or not
    # anything had changed.
    last_checked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    # When the refresh last found the body, links or attachments actually
    # different from the stored copy. Null means "never seen it change".
    content_updated_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), index=True)
    # How often to re-check, in hours. 0 means "every sync".
    refresh_interval_hours: Mapped[int] = mapped_column(Integer, default=0, server_default="0")
    # Why tracking stopped by itself, e.g. the pledge lapsed and Patreon no
    # longer serves the post. Set when the refresh auto-untracks a post so the
    # manage page can explain it; cleared when the user tracks it again. The
    # archived copy is always kept — losing access never deletes what we have.
    tracking_stopped_reason: Mapped[str | None] = mapped_column(String(200))

    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), onupdate=func.now())

    creator: Mapped["Creator"] = relationship(back_populates="posts")
    tags: Mapped[list["Tag"]] = relationship(secondary=post_tags, back_populates="posts")
    links: Mapped[list["Link"]] = relationship(back_populates="post", cascade="all, delete-orphan")
    attachments: Mapped[list["Attachment"]] = relationship(
        back_populates="post", cascade="all, delete-orphan"
    )


class Tag(Base):
    __tablename__ = "tags"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    name: Mapped[str] = mapped_column(String(255), unique=True, index=True)

    posts: Mapped[list[Post]] = relationship(secondary=post_tags, back_populates="tags")


class Link(Base):
    __tablename__ = "links"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    post_id: Mapped[int] = mapped_column(ForeignKey("posts.id", ondelete="CASCADE"), index=True)
    url: Mapped[str] = mapped_column(String(2000), index=True)

    post: Mapped[Post] = relationship(back_populates="links")


class User(Base):
    """A login account. Created via `python -m app.manage_users` — there is no
    public sign-up. Passwords are stored as pbkdf2 hashes (see app/auth.py)."""

    __tablename__ = "users"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    username: Mapped[str] = mapped_column(String(150), unique=True, index=True)
    password_hash: Mapped[str] = mapped_column(String(255))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())

    favourites: Mapped[list["Favourite"]] = relationship(
        back_populates="user", cascade="all, delete-orphan"
    )


class Favourite(Base):
    """A post a user has starred. One row per (user, post)."""

    __tablename__ = "favourites"
    __table_args__ = (UniqueConstraint("user_id", "post_id", name="uq_favourite_user_post"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), index=True)
    post_id: Mapped[int] = mapped_column(ForeignKey("posts.id", ondelete="CASCADE"), index=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())

    user: Mapped["User"] = relationship(back_populates="favourites")
    post: Mapped["Post"] = relationship()


class Attachment(Base):
    """A file attached to a content-mode post (pdf, ebook, txt, json, …).

    remote_url is the Patreon download URL; local_path is set once the bytes
    have been archived to disk under data/raw/<creator>/<post_id>/. For
    links-mode creators the bytes are never fetched, so local_path stays NULL
    and downloaded stays False — the row is a pointer to the file on Patreon.
    """

    __tablename__ = "attachments"
    __table_args__ = (UniqueConstraint("post_id", "remote_url", name="uq_attachment_post_url"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    post_id: Mapped[int] = mapped_column(ForeignKey("posts.id", ondelete="CASCADE"), index=True)

    filename: Mapped[str] = mapped_column(String(500), default="attachment")
    # Coarse category for display/filtering: pdf | ebook | text | json | image | other.
    kind: Mapped[str] = mapped_column(String(20), default="other")
    mimetype: Mapped[str | None] = mapped_column(String(255))
    remote_url: Mapped[str] = mapped_column(String(2000))
    local_path: Mapped[str | None] = mapped_column(String(1000))
    size_bytes: Mapped[int | None] = mapped_column(Integer)
    downloaded: Mapped[bool] = mapped_column(Boolean, default=False, server_default="0")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())

    post: Mapped[Post] = relationship(back_populates="attachments")
