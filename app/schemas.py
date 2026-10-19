from datetime import datetime

from pydantic import BaseModel


class AttachmentOut(BaseModel):
    id: int
    filename: str
    kind: str
    size_bytes: int | None
    downloaded: bool


class PostSummary(BaseModel):
    id: int
    creator: str
    archive_mode: str
    title: str
    content_preview: str
    post_url: str | None
    published_at: datetime | None
    tags: list[str]
    attachment_count: int = 0
    # Tracking: whether every sync re-checks this post, and when its content
    # was last seen to change (null = never observed changing).
    tracked: bool = False
    content_updated_at: datetime | None = None


class PostDetail(PostSummary):
    content: str
    # Sanitized HTML body, populated for content- and links-mode posts
    # (empty for video, which renders plain text).
    content_html: str = ""
    # Outbound links, split so the off-site ones lead: `links` holds everything
    # that points somewhere other than Patreon (Drive folders, storefronts,
    # map packs, videos), `patreon_links` the self-referential remainder.
    links: list[str]
    patreon_links: list[str] = []
    attachments: list[AttachmentOut] = []
    # When the tracking refresh last looked at this post at all.
    last_checked_at: datetime | None = None


class TrackedPost(BaseModel):
    """A post on the tracking manage page."""

    id: int
    creator: str
    title: str
    post_url: str | None
    tracked: bool
    refresh_interval_hours: int
    last_checked_at: datetime | None
    content_updated_at: datetime | None
    # Set only on posts tracking gave up on (pledge lapsed, post withdrawn).
    tracking_stopped_reason: str | None = None


class TrackUpdate(BaseModel):
    """Body of PATCH /api/posts/{id}/track."""

    refresh_interval_hours: int


class SyncResult(BaseModel):
    creator: str
    downloaded: bool
    imported_posts: int


class TagOut(BaseModel):
    name: str


class CreatorOut(BaseModel):
    name: str
    source_type: str
    archive_mode: str
