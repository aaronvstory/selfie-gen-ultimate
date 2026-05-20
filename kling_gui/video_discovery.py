"""Folder-scoped video discovery and image<->video association.

A single source image can produce many derived videos:

    front.png                                     (the image)
    front_k25tStd_p4_1.mp4                        (Kling raw)
    front_k25tStd_p4_1-oldcam-v8.mp4              (Oldcam variant)
    front_k25tStd_p4_1-oldcam-v24.mp4             (different Oldcam)
    front_k25tStd_p4_1-oldcam-v24-rppg - ...mp4   (rPPG-injected)

A "video group" gathers all derivatives keyed by the *exact* image stem
(``front``). Exact equality — NOT ``startswith`` — is the locking
invariant: a folder containing ``front.png`` AND ``front_extra.png``
must produce two separate groups, and ``find_video_for_image(front.png)``
must NOT return ``front_extra_..._k25tStd_p1_1.mp4``.

Discovery is non-recursive (the carousel's "work folder" is always one
directory). Scan order is stable so the modal listbox doesn't reshuffle
on every reopen.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Dict, List, Optional, Tuple

from .video_metadata import VideoMetadata, parse_video_filename


# Per-image cache for find_video_for_image. The carousel calls it on
# every <Configure> redraw and every session change; without caching
# that's an iterdir() per resize tick — measurable UI stutter on
# folders with many videos. Key: (resolved folder str, image stem,
# folder mtime); value: the chosen Path or None. Invalidates
# automatically when files appear/disappear (mtime changes).
#
# THREAD-SAFETY: this cache is TK-THREAD-ONLY. All current callers
# (carousel._show_image_on_canvas, modal listbox population) run on
# the Tk main thread, so the dict mutation in the LRU-eviction path
# (``_BEST_VIDEO_CACHE.pop(next(iter(_BEST_VIDEO_CACHE)))``) is
# safe — but check-then-pop is NOT atomic, so if a future caller
# wires this from a background thread (e.g. a similarity recalc
# worker), a ``threading.Lock`` must be added around the read +
# eviction. Code-reviewer Important (PR #43, post-79802bc self-review).
_BEST_VIDEO_CACHE: Dict[Tuple[str, str, float], Optional[Path]] = {}
_BEST_VIDEO_CACHE_MAX = 256  # rough cap — carousel cycles through ~dozens of images


@dataclass
class VideoGroup:
    """All videos derived from a single source image stem."""

    base_stem: str
    image_path: Optional[Path]
    videos: List[VideoMetadata] = field(default_factory=list)

    def sort_videos(self) -> None:
        """Sort in pipeline-progression order: raw Kling first, then
        oldcam variants by ascending version, then rPPG variants last.
        Within the same processing tier, by (model_short, slot, take).
        Stable so reordering doesn't shuffle the modal listbox each open.
        """
        self.videos.sort(key=_video_sort_key)


def _video_sort_key(m: VideoMetadata) -> tuple:
    """Sort key encoding 'most processed last' progression.

    Tie-break on raw Kling variants: ``take`` is more significant
    than ``slot``. The plan spec is "highest take, then highest slot",
    so a later take wins regardless of slot. We list ``take`` before
    ``slot`` in the tuple so ties on (has_rppg, oldcam_v, model_short)
    are decided by take first, slot second.
    """
    return (
        m.has_rppg,                       # raw, looped, oldcam, then rppg
        m.oldcam_version or 0,            # within tier, ascending version
        # _looped sits between raw kling and oldcam — it's a downstream
        # of raw but a precursor of oldcam (queue_manager loops the
        # kling clip BEFORE oldcam runs on the looped version). For a
        # NON-oldcam group (oldcam_version=0), looped > raw. For oldcam
        # groups this is dominated by oldcam_version above.
        # PR #43 / Codex P1 (looped-variant discovery fix).
        m.is_looped,
        m.model_short or "",
        m.take or 0,                       # tie-break primary: highest take wins
        m.slot or 0,                       # tie-break secondary: highest slot
        str(m.path),
    )


# Image extensions we recognise as carousel sources. PNG dominates the
# pipeline but JPGs slip in from face-crop / user drops.
_IMAGE_EXTS = (".png", ".jpg", ".jpeg")


def find_video_groups(folder: Path) -> List[VideoGroup]:
    """Scan ``folder`` (non-recursive) for ``*.mp4``; group by base_stem.

    Each group's ``image_path`` is set to the matching source image if
    one is present in the same folder, else ``None``. Videos within
    each group are sorted via ``VideoGroup.sort_videos``. Groups
    themselves are sorted by base_stem for stable listbox order.

    Returns an empty list if the folder doesn't exist or isn't a dir.
    """
    folder = Path(folder)
    if not folder.is_dir():
        return []

    groups: Dict[str, VideoGroup] = {}
    images: Dict[str, Path] = {}

    # Single-pass walk (Gemini PR #43, bot pass on 2a32f938): the previous
    # two iterdir() passes doubled the syscall cost on big folders. Sorted
    # iteration is still required so multiple matching image extensions
    # (front.png AND front.jpg) resolve deterministically — the last write
    # to images[stem] wins, and sorted order picks the last extension by
    # ASCII order ('.jpeg' < '.jpg' < '.png'). The video walk has no
    # determinism dependency (group lookup is keyed by base_stem).
    for entry in sorted(folder.iterdir(), key=lambda p: p.name):
        if not entry.is_file():
            continue
        ext = entry.suffix.lower()
        if ext == ".mp4":
            meta = parse_video_filename(entry)
            key = meta.base_stem
            if key not in groups:
                groups[key] = VideoGroup(base_stem=key, image_path=None)
            groups[key].videos.append(meta)
        elif ext in _IMAGE_EXTS:
            images[entry.stem] = entry

    # Associate source images by EXACT stem match. CodeRabbit PR #43
    # determinism guarantee: only image stems present in the saved
    # iteration order can win the association, and the dict-update above
    # preserves that order.
    for stem, image_path in images.items():
        if stem in groups:
            groups[stem].image_path = image_path

    for g in groups.values():
        g.sort_videos()

    return [groups[k] for k in sorted(groups.keys())]


def all_videos_for_image(image_path: Path) -> List[VideoMetadata]:
    """All videos in the image's folder whose base_stem equals
    ``image_path.stem``. Sorted via the standard group ordering.
    """
    image_path = Path(image_path)
    folder = image_path.parent
    target = image_path.stem
    if not folder.is_dir():
        return []
    matches: List[VideoMetadata] = []
    for entry in sorted(folder.iterdir(), key=lambda p: p.name):
        if not entry.is_file() or entry.suffix.lower() != ".mp4":
            continue
        meta = parse_video_filename(entry)
        if meta.base_stem == target:
            matches.append(meta)
    matches.sort(key=_video_sort_key)
    return matches


def find_video_for_image(image_path: Path) -> Optional[Path]:
    """Return the "best" video derived from this image, or None.

    Used by the carousel overlay to decide whether to draw the play
    badge. Selection picks the most-processed variant:
        1) any rPPG variant (highest oldcam version)
        2) any oldcam variant (highest version)
        3) raw Kling (highest take, then highest slot)

    Matching is via EXACT ``base_stem == image_path.stem`` equality
    (delegated to ``all_videos_for_image``) — never prefix-match.
    The stem-collision regression case (``front.png`` vs
    ``front_extra_..._k25tStd_p1_1.mp4``) depends on this exactness,
    so a user-renamed mp4 outside the pipeline naming convention will
    NOT be auto-associated. That is intentional in V1: false-positive
    associations are worse than a missing play badge.

    Cached by (folder, image_stem, folder_mtime) — the carousel
    invokes this on every redraw, and without caching that's an
    iterdir() per Configure event. Cache auto-invalidates when files
    are added/removed (folder mtime changes).
    """
    image_path = Path(image_path)
    folder = image_path.parent
    try:
        if not folder.is_dir():
            return None
        folder_mtime = folder.stat().st_mtime
    except OSError:
        return None

    cache_key = (str(folder.resolve()), image_path.stem, folder_mtime)
    if cache_key in _BEST_VIDEO_CACHE:
        return _BEST_VIDEO_CACHE[cache_key]

    matches = all_videos_for_image(image_path)
    # "Most processed wins" — the sort key already orders raw < oldcam <
    # rPPG (ascending) so the last element is the most-processed.
    result: Optional[Path] = matches[-1].path if matches else None

    # LRU-ish eviction so a long carousel session doesn't grow forever.
    if len(_BEST_VIDEO_CACHE) >= _BEST_VIDEO_CACHE_MAX:
        # Drop the first-inserted entry (Python 3.7+ dicts preserve
        # insertion order, so iter(dict) yields oldest first).
        _BEST_VIDEO_CACHE.pop(next(iter(_BEST_VIDEO_CACHE)))
    _BEST_VIDEO_CACHE[cache_key] = result
    return result


def clear_video_discovery_cache() -> None:
    """Drop the find_video_for_image cache. Test helper; callers can
    also use this if they know the folder changed without an mtime
    bump (rare — atomic writes typically update parent mtime)."""
    _BEST_VIDEO_CACHE.clear()
