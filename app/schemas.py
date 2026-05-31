from datetime import datetime

from pydantic import BaseModel


class PostSummary(BaseModel):
    id: int
    creator: str
    title: str
    content_preview: str
    post_url: str | None
    published_at: datetime | None
    tags: list[str]


class PostDetail(PostSummary):
    content: str
    links: list[str]


class SyncResult(BaseModel):
    creator: str
    downloaded: bool
    imported_posts: int


class TagOut(BaseModel):
    name: str


class CreatorOut(BaseModel):
    name: str
