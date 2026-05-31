"""Prune downloaded media from data/raw that the app doesn't need.

The running app serves entirely from the SQLite DB; data/raw exists only as
patreon-dl's download/working area. patreon-dl skips already-downloaded posts
via its status cache (the .patreon-dl directories), NOT by checking which files
are present on disk, so deleting downloaded media here does not trigger
re-downloads on the next sync.
"""
from __future__ import annotations

import sys
from pathlib import Path
from typing import Callable

from app.config import settings

# Image media patreon-dl saves alongside post info. Extend with video/audio
# suffixes here if a non-metadata-only config is ever used.
PRUNE_EXTENSIONS = {".jpg", ".jpeg", ".png", ".gif", ".webp", ".bmp"}

# Never touch patreon-dl's status caches — they keep syncs incremental.
_CACHE_DIR = ".patreon-dl"


def prune_raw_media(
    creator: str | None = None,
    on_output: Callable[[str], None] | None = None,
) -> dict:
    """Delete image media under data/raw (optionally scoped to one creator).

    Preserves the .patreon-dl status caches and all non-image files (post_info
    JSON/txt, etc.). Returns a summary dict.
    """
    root = settings.raw_root / creator if creator else settings.raw_root

    removed_files = 0
    freed_bytes = 0

    if root.exists():
        for path in root.rglob("*"):
            if not path.is_file():
                continue
            if path.suffix.lower() not in PRUNE_EXTENSIONS:
                continue
            if _CACHE_DIR in path.parts:
                continue
            try:
                size = path.stat().st_size
                path.unlink()
            except OSError as exc:
                if on_output:
                    on_output(f"Skip {path}: {exc}")
                continue
            removed_files += 1
            freed_bytes += size

    freed_mb = round(freed_bytes / 1_048_576, 1)
    if on_output:
        on_output(f"Pruned {removed_files} media files, freed {freed_mb} MB")

    return {
        "removed_files": removed_files,
        "freed_bytes": freed_bytes,
        "freed_mb": freed_mb,
    }


if __name__ == "__main__":
    target = sys.argv[1] if len(sys.argv) > 1 else None
    result = prune_raw_media(target, on_output=print)
    print(
        f"Done: removed {result['removed_files']} files, "
        f"freed {result['freed_mb']} MB"
    )
