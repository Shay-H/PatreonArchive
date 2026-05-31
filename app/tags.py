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

    Idempotent: returns the number of tags renamed or merged. Posts pointing at
    a duplicate (e.g. "Action" when "action" already exists) are repointed at the
    surviving lowercase tag and the duplicate is deleted.
    """
    changed = 0
    for tag in session.scalars(select(Tag)).all():
        norm = normalize_tag(tag.name)
        if norm == tag.name:
            continue

        survivor = session.scalar(select(Tag).where(Tag.name == norm))
        if survivor and survivor.id != tag.id:
            for post in list(tag.posts):
                if survivor not in post.tags:
                    post.tags.append(survivor)
                post.tags.remove(tag)
            session.delete(tag)
        else:
            tag.name = norm
        changed += 1

    if changed:
        session.flush()
    return changed
