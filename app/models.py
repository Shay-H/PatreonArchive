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

    source_path: Mapped[str] = mapped_column(String(1000))
    raw_json: Mapped[str] = mapped_column(Text, default="{}")

    # False when Patreon reported the user couldn't view the post (locked /
    # no access). These are hidden from listings by default.
    can_view: Mapped[bool] = mapped_column(Boolean, default=True, server_default="1", index=True)

    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), onupdate=func.now())

    creator: Mapped["Creator"] = relationship(back_populates="posts")
    tags: Mapped[list["Tag"]] = relationship(secondary=post_tags, back_populates="posts")
    links: Mapped[list["Link"]] = relationship(back_populates="post", cascade="all, delete-orphan")


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
