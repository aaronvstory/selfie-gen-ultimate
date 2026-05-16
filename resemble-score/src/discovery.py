"""Folder scan + oldcam-vN / Kling classification.

Oldcam outputs are named ``<stem>-oldcam-v<N><ext>`` by
``kling_gui/queue_manager.py::_build_oldcam_output_path`` (suffix
``-oldcam-<version>``). Everything else with a video extension is treated as
the original Kling (or other) render. The user picks which to score; we never
guess intent beyond the filename.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

from .client import VIDEO_EXTS

ORIGINAL_GROUP = "Original/Kling"

# Matches the exact suffix queue_manager produces, e.g. "...-oldcam-v14.mp4".
# Extensions kept in sync with client.VIDEO_EXTS.
_EXT_ALT = "|".join(sorted(e.lstrip(".") for e in VIDEO_EXTS))
OLDCAM_RE = re.compile(rf"-oldcam-v(\d+)\.(?:{_EXT_ALT})$", re.IGNORECASE)


@dataclass(frozen=True)
class VideoItem:
    """One discovered video and its classification."""

    path: Path
    group: str  # ORIGINAL_GROUP or "Oldcam vN"
    version: Optional[int]  # N for oldcam, None otherwise

    @property
    def name(self) -> str:
        return self.path.name


def classify(path: Path) -> VideoItem:
    """Classify a single video path by filename convention."""
    m = OLDCAM_RE.search(path.name)
    if m:
        version = int(m.group(1))
        return VideoItem(path=path, group=f"Oldcam v{version}", version=version)
    return VideoItem(path=path, group=ORIGINAL_GROUP, version=None)


def group_sort_key(item: VideoItem) -> tuple:
    """Sort: Original/Kling first, then Oldcam ascending by version, then name.

    ``version is None`` → group rank 0 (Original); oldcam → rank 1 then version.
    """
    if item.version is None:
        return (0, 0, item.name.lower())
    return (1, item.version, item.name.lower())


def discover(root: Path, recursive: bool) -> list[VideoItem]:
    """Return classified videos under ``root``.

    Unreadable / odd-named entries are skipped per-entry (no traceback) so a
    single bad file never aborts a scan. Results are sorted by
    :func:`group_sort_key`.
    """
    root = Path(root)
    if not root.is_dir():
        raise NotADirectoryError(f"Not a folder: {root}")

    items: list[VideoItem] = []
    walker = root.rglob("*") if recursive else root.iterdir()
    for entry in walker:
        try:
            if not entry.is_file():
                continue
            if entry.suffix.lower() not in VIDEO_EXTS:
                continue
        except OSError:
            # Reserved/odd names or permission issues — skip, keep scanning.
            continue
        items.append(classify(entry))

    items.sort(key=group_sort_key)
    return items
