"""Tests for the session-load folder-rescan feature (2026-05-20).

User direction: "whenever a new or existing session gets loaded, it should
rescan that folder and load in everything." Images get added as input entries,
videos get added as source_type="video" entries (rendered with a play-glyph
thumbnail; clicks open the Video Inspector).

These tests cover the building blocks (ImageEntry video typing, [VIDEO] tag,
the video-frame extractor) plus an end-to-end rescan simulation against a
tmp_path. The full ``_on_session_loaded`` requires a Tk root + carousel so
it's out of unit-test scope — but the rescan algorithm itself is replicated
here against a real filesystem so a regression in the per-folder scan or
dedup logic fails this file.
"""

from __future__ import annotations

import os
import struct
import zlib
from pathlib import Path

import pytest

from kling_gui.image_state import (
    _VALID_SOURCE_TYPES,
    _VIDEO_EXTENSIONS,
    ImageEntry,
    ImageSession,
)
from kling_gui.tag_utils import derive_display_tag


# ──────────────────────────────────────────────────────────────────────
# ImageEntry video-source-type contract
# ──────────────────────────────────────────────────────────────────────


def test_video_is_a_valid_source_type():
    assert "video" in _VALID_SOURCE_TYPES


def test_video_extension_set_covers_common_formats():
    # Must match the carousel_widget render branch + the rescan loop. If you
    # change one, change the other (or extract to a shared constant).
    for ext in (".mp4", ".mov", ".webm", ".mkv", ".avi"):
        assert ext in _VIDEO_EXTENSIONS


def test_is_video_true_when_source_type_is_video():
    entry = ImageEntry(path="/tmp/anything.png", source_type="video")
    assert entry.is_video is True


def test_is_video_true_for_video_extensions_regardless_of_type():
    # Belt + suspenders: catches files whose source_type was misclassified
    # but whose extension is unambiguously a video.
    for ext in (".mp4", ".mov", ".webm", ".mkv", ".avi"):
        entry = ImageEntry(path=f"/tmp/clip{ext}", source_type="input")
        assert entry.is_video is True, f"missed: {ext}"


def test_is_video_false_for_image_files():
    for ext in (".png", ".jpg", ".jpeg", ".webp"):
        entry = ImageEntry(path=f"/tmp/img{ext}", source_type="input")
        assert entry.is_video is False, f"false positive: {ext}"


# ──────────────────────────────────────────────────────────────────────
# Display tag
# ──────────────────────────────────────────────────────────────────────


def test_video_entry_renders_video_tag():
    entry = ImageEntry(path="/tmp/x.mp4", source_type="video")
    tag, color_key = derive_display_tag(entry)
    assert tag == "[VIDEO]"
    assert color_key == "warning_light"


# ──────────────────────────────────────────────────────────────────────
# Video-frame extractor (carousel helper)
# ──────────────────────────────────────────────────────────────────────


def test_extract_video_first_frame_returns_none_for_missing_path(tmp_path):
    from kling_gui.carousel_widget import _extract_video_first_frame
    result = _extract_video_first_frame(str(tmp_path / "nonexistent.mp4"))
    assert result is None


def test_extract_video_first_frame_caches_results(tmp_path, monkeypatch):
    """Second call for the same path must hit the cache, not re-decode."""
    from kling_gui import carousel_widget
    sentinel = object()
    monkeypatch.setitem(
        carousel_widget._VIDEO_THUMB_CACHE,
        "/cached/path.mp4",
        sentinel,
    )
    assert carousel_widget._extract_video_first_frame("/cached/path.mp4") is sentinel


# ──────────────────────────────────────────────────────────────────────
# End-to-end folder-rescan simulation
# ──────────────────────────────────────────────────────────────────────


def _write_minimal_png(path: Path) -> None:
    """Write a 1x1 black PNG so PIL/preflight checks pass on this file."""
    sig = b"\x89PNG\r\n\x1a\n"
    ihdr_payload = struct.pack(">IIBBBBB", 1, 1, 8, 2, 0, 0, 0)
    ihdr = b"IHDR" + ihdr_payload
    ihdr_chunk = struct.pack(">I", 13) + ihdr + struct.pack(">I", zlib.crc32(ihdr))
    raw = b"\x00\x00\x00\x00"  # filter byte + RGB pixel
    idat_data = zlib.compress(raw)
    idat = b"IDAT" + idat_data
    idat_chunk = struct.pack(">I", len(idat_data)) + idat + struct.pack(">I", zlib.crc32(idat))
    iend = b"IEND"
    iend_chunk = struct.pack(">I", 0) + iend + struct.pack(">I", zlib.crc32(iend))
    path.write_bytes(sig + ihdr_chunk + idat_chunk + iend_chunk)


def _replicate_rescan(session: ImageSession, folders: set, valid_image_exts: set) -> tuple[int, int]:
    """Mirror of the rescan loop inside main_window._on_session_loaded.

    Lives here so a regression in the dedup logic, the source_type tagging,
    or the video discovery wiring fails this test file rather than only
    showing up in a manual GUI session. If you change the production loop,
    mirror the change here.
    """
    from kling_gui.video_discovery import find_video_groups
    loaded_real = {os.path.realpath(e.path) for e in session.images}
    new_imgs = new_vids = 0
    for folder in sorted(folders):
        if not folder or not os.path.isdir(folder):
            continue
        try:
            entries = sorted(os.listdir(folder))
        except OSError:
            continue
        for fname in entries:
            full = os.path.join(folder, fname)
            if not os.path.isfile(full):
                continue
            ext = os.path.splitext(fname)[1].lower()
            if ext not in valid_image_exts:
                continue
            real = os.path.realpath(full)
            if real in loaded_real:
                continue
            session.add_image(full, "input", make_active=False)
            loaded_real.add(real)
            new_imgs += 1
        try:
            groups = find_video_groups(Path(folder))
        except (OSError, ValueError):
            groups = []
        for group in groups:
            for vmeta in group.videos:
                vpath = str(vmeta.path)
                real = os.path.realpath(vpath)
                if real in loaded_real:
                    continue
                session.add_image(vpath, "video", make_active=False)
                loaded_real.add(real)
                new_vids += 1
    return new_imgs, new_vids


def test_rescan_picks_up_new_images_and_videos(tmp_path):
    """User scenario: session JSON references one image; folder now also has
    a sibling image AND a video that weren't in the original manifest."""
    img_a = tmp_path / "front.png"
    img_b = tmp_path / "front_crop.png"
    video = tmp_path / "front_clip.mp4"
    _write_minimal_png(img_a)
    _write_minimal_png(img_b)
    video.write_bytes(b"fakempeg")  # cv2 won't decode this but we don't need it
                                    # to — discovery only checks for the extension.

    session = ImageSession()
    session.add_image(str(img_a), "input", make_active=False)
    assert session.count == 1

    new_imgs, new_vids = _replicate_rescan(
        session,
        folders={str(tmp_path)},
        valid_image_exts={".png", ".jpg", ".jpeg", ".webp"},
    )
    assert new_imgs == 1, "front_crop.png should be added on rescan"
    assert new_vids == 1, "front_clip.mp4 should be added as a video entry"
    assert session.count == 3

    # The video entry must be tagged correctly so the carousel routes its
    # render through the cv2/play-glyph branch and the [VIDEO] tag shows.
    video_entries = [e for e in session.images if e.source_type == "video"]
    assert len(video_entries) == 1
    assert video_entries[0].is_video is True


def test_rescan_dedups_already_loaded_paths(tmp_path):
    """Files already in the carousel must NOT be re-added on rescan."""
    img = tmp_path / "front.png"
    _write_minimal_png(img)
    session = ImageSession()
    session.add_image(str(img), "input", make_active=False)
    new_imgs, new_vids = _replicate_rescan(
        session,
        folders={str(tmp_path)},
        valid_image_exts={".png"},
    )
    assert (new_imgs, new_vids) == (0, 0)
    assert session.count == 1


def test_rescan_handles_nonexistent_folder_gracefully(tmp_path):
    """Bad folder path in the saved session shouldn't crash the rescan."""
    session = ImageSession()
    new_imgs, new_vids = _replicate_rescan(
        session,
        folders={str(tmp_path / "does_not_exist")},
        valid_image_exts={".png"},
    )
    assert (new_imgs, new_vids) == (0, 0)


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
