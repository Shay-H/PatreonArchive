"""Shared tag helpers.

All tag names are normalized to lowercase so the same concept doesn't show up
as both "Action" and "action". Creation goes through get_or_create_tag; existing
rows are migrated once at startup via lowercase_existing_tags.
"""
from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models import Tag


def normalize_tag(name: str) -> str:
    return name.strip().lower()


def get_or_create_tag(session: Session, name: str) -> Tag:
    norm = normalize_tag(name)
    tag = session.scalar(select(Tag).where(Tag.name == norm))
    if not tag:
        tag = Tag(name=norm)
        session.add(tag)
        session.flush()
    return tag


def lowercase_existing_tags(session: Session) -> int:
    """Lowercase all existing tag names, merging any that collide.

    Idempotent. Returns the number of tags renamed or merged. Tags are grouped by
    their normalized name; one survivor is kept per group (preferring a row that
    is already lowercase), every other tag in the group has its posts repointed at
    the survivor and is then deleted, and finally the survivor is renamed. Deletes
    are flushed before the rename so two rows never momentarily share a name (which
    would trip the UNIQUE constraint on tags.name).
    """
    groups: dict[str, list[Tag]] = {}
    for tag in session.scalars(select(Tag)).all():
        groups.setdefault(normalize_tag(tag.name), []).append(tag)

    changed = 0
    for norm, tags in groups.items():
        if len(tags) == 1 and tags[0].name == norm:
            continue  # already normalized, nothing to do

        survivor = next((t for t in tags if t.name == norm), tags[0])
        duplicates = [t for t in tags if t is not survivor]

        for dup in duplicates:
            for post in list(dup.posts):
                if survivor not in post.tags:
                    post.tags.append(survivor)
                post.tags.remove(dup)
            session.delete(dup)
            changed += 1

        if duplicates:
            session.flush()  # apply deletes before freeing the name

        if survivor.name != norm:
            survivor.name = norm
            changed += 1
            session.flush()

    return changed
