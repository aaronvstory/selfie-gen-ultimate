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
    # Relative dummy paths — these are only fed to ImageEntry's
    # os.path.splitext/abspath; nothing touches the filesystem. Avoids
    # Ruff S108 "insecure tmp file usage" lint on /tmp/* literals.
    entry = ImageEntry(path="anything.png", source_type="video")
    assert entry.is_video is True


def test_is_video_true_for_video_extensions_regardless_of_type():
    # Belt + suspenders: catches files whose source_type was misclassified
    # but whose extension is unambiguously a video.
    for ext in (".mp4", ".mov", ".webm", ".mkv", ".avi"):
        entry = ImageEntry(path=f"clip{ext}", source_type="input")
        assert entry.is_video is True, f"missed: {ext}"


def test_is_video_false_for_image_files():
    for ext in (".png", ".jpg", ".jpeg", ".webp"):
        entry = ImageEntry(path=f"img{ext}", source_type="input")
        assert entry.is_video is False, f"false positive: {ext}"


# ──────────────────────────────────────────────────────────────────────
# Display tag
# ──────────────────────────────────────────────────────────────────────


def test_video_entry_renders_video_tag():
    entry = ImageEntry(path="x.mp4", source_type="video")
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
    """Second call for the same path must hit the cache, not re-decode.

    Cache key is (path, mtime) per the M1 fix on 2eb16f37 — in-place
    video regen invalidates the cache. Test seeds the cache with the
    (path, mtime) tuple to match production behavior.
    """
    from kling_gui import carousel_widget
    import os
    # Need a real file so os.path.getmtime returns a deterministic value.
    video_path = tmp_path / "cached.mp4"
    video_path.write_bytes(b"fake")
    mtime = os.path.getmtime(str(video_path))
    sentinel = object()
    monkeypatch.setitem(
        carousel_widget._VIDEO_THUMB_CACHE,
        (str(video_path), mtime),
        sentinel,
    )
    assert carousel_widget._extract_video_first_frame(str(video_path)) is sentinel


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

    Two video discovery passes (per H1 fix on PR #43 review):
      1) find_video_groups for .mp4 (preserves Kling base_stem grouping)
      2) Direct extension scan for .mov/.webm/.mkv/.avi (not covered by 1)
    """
    from kling_gui.video_discovery import find_video_groups
    extra_video_exts = {".mov", ".webm", ".mkv", ".avi"}
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
        except OSError:
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
        # Pass 2: non-mp4 video extensions
        for fname in entries:
            ext = os.path.splitext(fname)[1].lower()
            if ext not in extra_video_exts:
                continue
            full = os.path.join(folder, fname)
            if not os.path.isfile(full):
                continue
            real = os.path.realpath(full)
            if real in loaded_real:
                continue
            session.add_image(full, "video", make_active=False)
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


def test_rescan_surfaces_all_five_video_extensions(tmp_path):
    """H1 from code review: the original rescan only used find_video_groups
    which hard-filters on .mp4. Users with .mov/.webm/.mkv/.avi clips were
    silently dropped, contradicting the is_video contract and the user-quoted
    "load in everything" direction. Two-pass discovery must surface all five.
    """
    img = tmp_path / "anchor.png"
    _write_minimal_png(img)
    # One of each video extension we advertise
    for stem, ext in [
        ("clip1", ".mp4"),
        ("clip2", ".mov"),
        ("clip3", ".webm"),
        ("clip4", ".mkv"),
        ("clip5", ".avi"),
    ]:
        (tmp_path / f"{stem}{ext}").write_bytes(b"fakempeg")

    session = ImageSession()
    session.add_image(str(img), "input", make_active=False)

    new_imgs, new_vids = _replicate_rescan(
        session,
        folders={str(tmp_path)},
        valid_image_exts={".png"},
    )
    assert new_imgs == 0
    assert new_vids == 5, (
        f"Expected all 5 video formats to surface, got {new_vids}. "
        "Regression of the H1 video-format-coverage fix."
    )
    found_exts = {
        os.path.splitext(e.path)[1].lower()
        for e in session.images if e.is_video
    }
    assert found_exts == {".mp4", ".mov", ".webm", ".mkv", ".avi"}


def test_rescan_dedups_video_across_both_discovery_passes(tmp_path):
    """An .mp4 that find_video_groups DOES surface must not be re-added by
    the extension-scan second pass. realpath dedup guards against that.
    """
    img = tmp_path / "anchor.png"
    _write_minimal_png(img)
    (tmp_path / "front_k25tStd_p1_1.mp4").write_bytes(b"fake")  # Kling-shaped
    session = ImageSession()
    session.add_image(str(img), "input", make_active=False)
    _, new_vids = _replicate_rescan(
        session,
        folders={str(tmp_path)},
        valid_image_exts={".png"},
    )
    # .mp4 is in the find_video_groups vocabulary AND in our extra_video_exts
    # would NOT be (it's intentionally excluded from extra_video_exts). Result:
    # exactly one video added, not two.
    assert new_vids == 1


def test_liveness_image_exts_match_valid_extensions():
    """C2 (code-review on 4ddb0252): the liveness classifier MUST share
    its image-extension set with the load-path's VALID_EXTENSIONS in
    path_utils. Drift between the two = a session that the load-path
    would successfully load gets classified DEAD and pruned. Identity
    test catches this as a single-line failure."""
    from path_utils import VALID_EXTENSIONS
    from kling_gui.session_manager import _LIVENESS_IMAGE_EXTS
    assert _LIVENESS_IMAGE_EXTS is VALID_EXTENSIONS or _LIVENESS_IMAGE_EXTS == VALID_EXTENSIONS, (
        "Liveness image-extensions must match path_utils.VALID_EXTENSIONS. "
        f"Diff: liveness={_LIVENESS_IMAGE_EXTS - VALID_EXTENSIONS}, "
        f"valid={VALID_EXTENSIONS - _LIVENESS_IMAGE_EXTS}"
    )


def test_liveness_video_exts_match_image_state():
    """C2 sibling: the video-extension set must mirror image_state._VIDEO_EXTENSIONS
    (the source of truth for ImageEntry.is_video). Adding a new format on
    one side without the other breaks the lockstep."""
    from kling_gui.image_state import _VIDEO_EXTENSIONS
    from kling_gui.session_manager import _LIVENESS_VIDEO_EXTS
    assert _LIVENESS_VIDEO_EXTS is _VIDEO_EXTENSIONS or _LIVENESS_VIDEO_EXTS == _VIDEO_EXTENSIONS


def test_foreign_path_detection_windows_path_on_posix():
    """C1 (code-review on 4ddb0252): the user's mesh has 2 macOS + 1 Windows
    machines. Sessions saved on Windows carry C:\\... paths. On macOS those
    paths register as missing AND have no dirname, so the old classifier
    swept them as DEAD — silent data loss when the user pulled the branch
    on macOS and ran Prune.

    The foreign-path detector must catch all common Windows path shapes."""
    from kling_gui.session_manager import _is_foreign_path
    import os
    if os.name == "nt":
        pytest.skip("Windows-on-POSIX detection — current host is Windows")
    # Drive-letter paths
    assert _is_foreign_path(r"C:\Users\me\proj\front.png")
    assert _is_foreign_path(r"D:/foo/bar.jpg")  # mixed sep
    assert _is_foreign_path(r"Z:\very_long_path\with_underscores\x.mp4")
    # UNC paths
    assert _is_foreign_path(r"\\server\share\file.png")
    # Any backslash in body
    assert _is_foreign_path(r"some\windowsy\rel\path.png")
    # NOT foreign — local POSIX
    assert not _is_foreign_path("/Users/me/file.png")
    assert not _is_foreign_path("/Volumes/External/clip.mp4")
    # Edge: empty / falsy
    assert not _is_foreign_path("")


def test_foreign_path_detection_posix_path_on_windows():
    """CodeRabbit critical on 253a9b4 — the inverse case was broken.
    On Windows, ``ntpath.isabs("/Users/alice/x")`` returns True, so the
    earlier predicate ``startswith("/") and not isabs`` was always False
    and POSIX paths from a macOS session were never flagged as foreign
    on a Windows host. Same silent-data-loss risk as the macOS case if
    Prune ran on the Windows machine.
    """
    from kling_gui.session_manager import _is_foreign_path
    import os
    if os.name != "nt":
        pytest.skip("POSIX-on-Windows detection — current host is POSIX")
    # POSIX absolute paths — foreign on Windows
    assert _is_foreign_path("/Users/alice/proj/front.png")
    assert _is_foreign_path("/home/bob/x.jpg")
    assert _is_foreign_path("/tmp/y.mp4")
    assert _is_foreign_path("/Volumes/External/clip.mp4")
    # NOT foreign — native Windows shapes
    assert not _is_foreign_path(r"C:\Users\alice\proj\front.png")
    assert not _is_foreign_path("D:/data/file.png")  # forward-slash variant
    assert not _is_foreign_path(r"\\server\share\file.png")  # UNC
    # Drive-relative ``\Users\...`` is Windows-native too (rare in
    # this project but possible from `subst` mounts / network shares).
    # Code-review on 706466f hardened the detector against this case.
    assert not _is_foreign_path(r"\Users\alice\x.png")
    # Edge: empty / relative
    assert not _is_foreign_path("")
    assert not _is_foreign_path("relative/path.png")


def test_session_with_all_foreign_paths_classifies_live(tmp_path):
    """End-to-end safety: a session whose images all live in C:\\Users on
    a Windows host must classify LIVE when opened on macOS — Prune would
    otherwise silently delete the user's work."""
    from kling_gui.session_manager import session_liveness
    import os, json
    if os.name == "nt":
        pytest.skip("foreign-path detection direction is OS-specific")
    sess = tmp_path / "win_session.json"
    sess.write_text(json.dumps({
        "session": {
            "images": [
                {"path": r"C:\Users\me\proj\front.png", "source_type": "input"},
                {"path": r"C:\Users\me\proj\gen-images\selfie.png",
                 "source_type": "selfie"},
            ]
        }
    }))
    info = session_liveness(str(sess))
    assert info["live"] is True
    assert info["foreign_os"] is True


def test_prune_dead_sessions_paths_kwarg_uses_explicit_set(tmp_path):
    """H2 (code-review on 4ddb0252): the prune must accept an explicit
    path list so the caller (Session Manager dialog) can pass the set
    computed at refresh time. Without this, a folder going unreachable
    between refresh and click would silently flip live→dead and get
    swept along with the user's intended targets."""
    from kling_gui.session_manager import prune_dead_sessions
    import json
    sessions_dir = tmp_path / "sessions"
    sessions_dir.mkdir()
    # Two dead-looking sessions on disk:
    p1 = sessions_dir / "a.json"
    p1.write_text(json.dumps({"session": {"images": [
        {"path": str(tmp_path / "gone1" / "x.png"), "source_type": "input"}]}}))
    p2 = sessions_dir / "b.json"
    p2.write_text(json.dumps({"session": {"images": [
        {"path": str(tmp_path / "gone2" / "x.png"), "source_type": "input"}]}}))

    # Caller only wants p1 pruned, even though both ARE dead — explicit
    # paths must be honored as the source of truth.
    deleted = prune_dead_sessions(str(tmp_path), paths=[str(p1)])
    assert deleted == [str(p1)]
    assert not p1.exists()
    assert p2.exists(), "explicit paths must NOT prune p2 even though it's also dead"


def test_broken_symlink_folder_classifies_live(tmp_path):
    """M1 (code-review on 4ddb0252): a broken symlink (e.g. unmounted
    external drive) returns False from os.path.isdir but True from
    os.path.lexists. Treat as LIVE so re-plugging the drive recovers
    the session."""
    from kling_gui.session_manager import session_liveness
    import os, json
    if os.name == "nt":
        pytest.skip("symlink semantics differ on Windows")
    # Create a symlink to a directory then delete the target
    real_target = tmp_path / "real_drive"
    real_target.mkdir()
    link = tmp_path / "mounted"
    link.symlink_to(real_target)
    real_target.rmdir()  # link now dangling

    sess = tmp_path / "s.json"
    sess.write_text(json.dumps({"session": {"images": [
        {"path": str(link / "front.png"), "source_type": "input"}]}}))
    info = session_liveness(str(sess))
    assert info["live"] is True, (
        "broken symlink (sleeping drive / dropped mount) must classify "
        "LIVE so re-plugging restores the session"
    )


def test_session_liveness_classifies_empty_folder_as_dead(tmp_path):
    """A session whose saved paths all point at missing files AND whose
    surveyed folders have no recoverable image/video must classify dead.
    User scenario: folders were renamed (e.g. organized/ → FOR_APPLICATION_-_…),
    leaving 70 of 74 sessions pointing at empty/missing paths."""
    from kling_gui.session_manager import session_liveness
    sessions_dir = tmp_path / "sessions"
    sessions_dir.mkdir()
    # Saved image refers to a non-existent path; the folder exists but is empty.
    dead_folder = tmp_path / "deadfolder"
    dead_folder.mkdir()
    import json
    sess_path = sessions_dir / "dead.json"
    sess_path.write_text(json.dumps({
        "session": {"images": [{"path": str(dead_folder / "missing.png"),
                                "source_type": "input"}]},
    }))
    info = session_liveness(str(sess_path))
    assert info["live"] is False
    assert info["saved_images"] == 1
    assert info["missing"] == 1
    assert info["rescan_imgs"] == 0
    assert info["rescan_vids"] == 0


def test_session_liveness_classifies_alive_via_rescan_video(tmp_path):
    """If the saved images are all missing but a VIDEO in the same folder
    can be surfaced by the rescan, the session is NOT dead — we'd lose
    the video association. Covers the user's actual workflow where image
    files get pruned but oldcam/rPPG videos remain."""
    from kling_gui.session_manager import session_liveness
    sessions_dir = tmp_path / "sessions"
    sessions_dir.mkdir()
    folder = tmp_path / "live"
    folder.mkdir()
    (folder / "clip.mp4").write_bytes(b"fake")  # video only — image is gone
    import json
    sess_path = sessions_dir / "live.json"
    sess_path.write_text(json.dumps({
        "session": {"images": [{"path": str(folder / "gone.png"),
                                "source_type": "input"}]},
    }))
    info = session_liveness(str(sess_path))
    assert info["live"] is True, "video-only folder must survive prune"
    assert info["rescan_vids"] == 1


def test_session_liveness_classifies_alive_via_existing_saved_image(tmp_path):
    """Belt-test: a saved image that still exists keeps the session live
    even if the folder contains nothing else."""
    from kling_gui.session_manager import session_liveness
    sessions_dir = tmp_path / "sessions"
    sessions_dir.mkdir()
    folder = tmp_path / "alive"
    folder.mkdir()
    img = folder / "front.png"
    img.write_bytes(b"\x89PNG\r\n\x1a\n")  # PNG signature — file just needs to EXIST
    import json
    sess_path = sessions_dir / "alive.json"
    sess_path.write_text(json.dumps({
        "session": {"images": [{"path": str(img), "source_type": "input"}]},
    }))
    info = session_liveness(str(sess_path))
    assert info["live"] is True
    assert info["missing"] == 0


def test_session_liveness_unreadable_session_classified_live(tmp_path):
    """A corrupt/unreadable JSON must NOT be classified dead — we don't
    want to silently prune a file the user could repair."""
    from kling_gui.session_manager import session_liveness
    sess_path = tmp_path / "broken.json"
    sess_path.write_text("not valid json {{{")
    info = session_liveness(str(sess_path))
    assert info["live"] is True, "broken JSON must be conservatively kept"


def test_find_dead_sessions_returns_only_dead(tmp_path):
    """End-to-end: with a mix of live and dead sessions, only the dead
    ones come back. Tests the integration of list_sessions →
    session_liveness → find_dead_sessions on a real filesystem."""
    from kling_gui.session_manager import find_dead_sessions
    import json
    sessions_dir = tmp_path / "sessions"
    sessions_dir.mkdir()

    # Dead — folder doesn't exist
    (sessions_dir / "dead1.json").write_text(json.dumps({
        "session": {"images": [{"path": str(tmp_path / "gone" / "x.png"),
                                "source_type": "input"}]},
    }))
    # Live — saved image actually exists
    live_dir = tmp_path / "live"
    live_dir.mkdir()
    (live_dir / "x.png").write_bytes(b"\x89PNG")
    (sessions_dir / "live1.json").write_text(json.dumps({
        "session": {"images": [{"path": str(live_dir / "x.png"),
                                "source_type": "input"}]},
    }))
    # Live — saved image gone but folder has a video
    video_dir = tmp_path / "video_only"
    video_dir.mkdir()
    (video_dir / "clip.mp4").write_bytes(b"fake")
    (sessions_dir / "live2.json").write_text(json.dumps({
        "session": {"images": [{"path": str(video_dir / "gone.png"),
                                "source_type": "input"}]},
    }))

    dead = find_dead_sessions(str(tmp_path))
    dead_names = sorted(os.path.basename(r.path) for r in dead)
    assert dead_names == ["dead1.json"], (
        f"Expected only dead1.json to be classified dead; got {dead_names}"
    )


def test_prune_dead_sessions_deletes_dead_keeps_live(tmp_path):
    """End-to-end on a real filesystem: prune wipes exactly the dead set,
    leaves live + corrupt sessions untouched."""
    from kling_gui.session_manager import prune_dead_sessions
    import json
    sessions_dir = tmp_path / "sessions"
    sessions_dir.mkdir()

    # Dead
    dead_path = sessions_dir / "dead.json"
    dead_path.write_text(json.dumps({
        "session": {"images": [{"path": str(tmp_path / "ghost" / "x.png"),
                                "source_type": "input"}]},
    }))
    # Live
    live_dir = tmp_path / "kept"
    live_dir.mkdir()
    (live_dir / "x.png").write_bytes(b"\x89PNG")
    live_path = sessions_dir / "live.json"
    live_path.write_text(json.dumps({
        "session": {"images": [{"path": str(live_dir / "x.png"),
                                "source_type": "input"}]},
    }))

    deleted = prune_dead_sessions(str(tmp_path))
    assert len(deleted) == 1
    assert not dead_path.exists()
    assert live_path.exists(), "live session must survive prune"


def test_video_thumb_async_helper_present_and_signature():
    """Gemini PR #43 finding (cv2 blocking on Tk): the carousel video branch
    must NOT call _extract_video_first_frame synchronously on the Tk thread
    for cold paths. The async helper offloads to a daemon thread and triggers
    a re-render via widget.after(0, ...) when the frame lands. Source-asserted
    so a refactor that drops the async path is caught.
    """
    src = (Path(__file__).resolve().parent.parent / "kling_gui" / "carousel_widget.py").read_text()
    assert "def _extract_video_first_frame_async" in src
    assert "_VIDEO_THUMB_PENDING" in src
    assert "_extract_video_first_frame_async(" in src, (
        "the carousel video render branch must invoke the async helper "
        "(not the sync _extract_video_first_frame) for cold paths"
    )


def test_video_thumb_async_no_double_spawn(tmp_path, monkeypatch):
    """Asynchronous decode must not spawn a second worker for a path with
    an in-flight decode (avoids thundering-herd on a busy folder)."""
    from kling_gui import carousel_widget
    # Pretend a decode is already in flight
    monkeypatch.setattr(
        carousel_widget, "_VIDEO_THUMB_PENDING",
        {"some/clip.mp4"},
        raising=True,
    )
    started = carousel_widget._extract_video_first_frame_async(
        "some/clip.mp4",
        widget=None,            # never touched because we return early
        on_done=lambda _img: None,
    )
    assert started is False


def test_video_thumb_async_cache_hit_returns_false(tmp_path, monkeypatch):
    """If the cache already has the frame (keyed by (path, mtime) per M1),
    async helper should NOT spawn a worker — caller can just read the
    cache directly."""
    from kling_gui import carousel_widget
    import os
    video_path = tmp_path / "clip.mp4"
    video_path.write_bytes(b"fake")
    mtime = os.path.getmtime(str(video_path))
    sentinel = object()
    monkeypatch.setattr(
        carousel_widget, "_VIDEO_THUMB_CACHE",
        {(str(video_path), mtime): sentinel},
        raising=True,
    )
    started = carousel_widget._extract_video_first_frame_async(
        str(video_path),
        widget=None,
        on_done=lambda _img: None,
    )
    assert started is False


def test_similarity_ref_button_disabled_on_video_source():
    """H3 (code-review 2026-05-20): the ★ Ref button must be DISABLED when
    the active carousel entry is a video. Without this, a user could click
    Ref on a video item, sending a .mp4 path through compute_face_similarity_details
    on the next recalc. Source-asserted so a regression fails fast.
    """
    src = (Path(__file__).resolve().parent.parent / "kling_gui" / "carousel_widget.py").read_text()
    assert "entry.is_video" in src
    assert "state=tk.DISABLED" in src
    # Composite: the specific Ref-disable path must be present
    assert 'if entry is not None and entry.is_video:' in src, (
        "_update_panel must gate the Ref button on entry.is_video (H3)"
    )


def test_similarity_recalc_refuses_video_ref():
    """H3 second-line defense: _calc_all_similarity must short-circuit with
    a clear log when ref.is_video, even if a stale manual ref slipped past
    the UI gate."""
    src = (Path(__file__).resolve().parent.parent / "kling_gui" / "carousel_widget.py").read_text()
    assert "if ref.is_video:" in src, (
        "_calc_all_similarity must refuse to use a video as the similarity "
        "reference and log a clear message (H3 belt+suspenders gate)"
    )


def test_session_load_translates_indices_through_skip_map():
    """H2 (code-review 2026-05-20): session-load skips missing files,
    shifting all subsequent saved indices down by one. The translation
    map must rewrite current_index / reference_index / similarity_ref_index
    so a saved ref-index at position 5 still points at the right entry
    after entries 2 and 3 were skipped on disk.
    """
    src = (Path(__file__).resolve().parent.parent / "kling_gui" / "main_window.py").read_text()
    assert "saved_to_new" in src, "H2 fix must build a saved_idx -> new_idx map"
    assert "_translate" in src, "H2 fix must apply the map to restored indices"
    # Specifically: each of the three indices must be translated
    assert "_translate(session_data.get(\"current_index\"" in src
    assert "_translate(session_data.get(\"reference_index\"" in src
    assert "_translate(session_data.get(\"similarity_ref_index\"" in src


def test_similarity_targets_filter_excludes_videos():
    """Codex P1 bot finding on c58dc394: _calc_all_similarity filtered only
    `source_type != "input"`, which let video entries through to
    compute_face_similarity_details → validate_image_file → warning noise on
    every recompute. The targets filter must also exclude `e.is_video`.

    Asserted on the source so a regression that drops the gate fails fast.
    """
    src = (Path(__file__).resolve().parent.parent / "kling_gui" / "carousel_widget.py").read_text()
    # The exact gate string from the fix
    assert "not e.is_video and e is not ref" in src, (
        "carousel_widget._calc_all_similarity must filter out video entries "
        "from similarity targets (regression of the Codex P1 fix on c58dc394)."
    )


def test_compare_panel_video_branch_uses_extract_helper(monkeypatch, tmp_path):
    """H2 from code review: compare_panel.Image.open() on a video path would
    raise UnidentifiedImageError and fall to the error-text branch. The
    video-aware branch must route through _extract_video_first_frame instead.

    We don't construct a real ComparePanel widget (heavy Tk setup) — instead
    we read the source and assert the video gate is present, so a refactor
    that drops it fails this test.
    """
    src = (Path(__file__).resolve().parent.parent / "kling_gui" / "compare_panel.py").read_text()
    assert "_extract_video_first_frame" in src, (
        "compare_panel.py must import and use the carousel's video-frame "
        "extractor for video entries (H2 fix)."
    )
    assert 'getattr(entry, "is_video", False)' in src, (
        "compare_panel.py must gate the render on entry.is_video before "
        "calling PIL.Image.open() (H2 fix)."
    )


# ──────────────────────────────────────────────────────────────────────
# Post-queue carousel rescan (Phase B of polish/v2.3, 2026-05-22)
# ──────────────────────────────────────────────────────────────────────


def test_scan_folders_for_new_media_helper_exists_and_is_callable():
    """Phase B extracted ``_scan_folders_for_new_media`` from the inline
    session-load block so the post-queue rescan can share the same
    scanning logic. The source must define the helper + the public
    rescan method that wraps it."""
    src = (Path(__file__).resolve().parent.parent / "kling_gui" / "main_window.py").read_text()
    assert "def _scan_folders_for_new_media(self, folders)" in src, (
        "Phase B helper _scan_folders_for_new_media must exist on KlingGUIWindow"
    )
    assert "def _rescan_session_folder_for_new_media(self)" in src, (
        "Phase B public method _rescan_session_folder_for_new_media must exist"
    )


def test_on_item_complete_schedules_post_queue_rescan():
    """The QueueManager fires _on_item_complete on the worker thread.
    The rescan touches Tk widgets so it MUST be scheduled via
    root.after(0) onto the main thread. Source-asserted so a refactor
    that drops the after() call fails this test."""
    src = (Path(__file__).resolve().parent.parent / "kling_gui" / "main_window.py").read_text()
    # Must call rescan via root.after on the GUI thread.
    assert "self.root.after(0, self._rescan_session_folder_for_new_media)" in src, (
        "Phase B: _on_item_complete must schedule the rescan via root.after(0)"
    )
    # Must guard against the test-stub case where .root isn't bound.
    assert 'hasattr(self, "root")' in src, (
        "Phase B: rescan scheduling must guard against missing .root attribute "
        "(unit tests construct minimal stubs without it)"
    )


def test_session_load_rescan_uses_shared_helper():
    """The pre-existing session-load rescan block (2026-05-20) was
    refactored to call the new shared helper so both paths use the
    same scanning rules. Don't let a future edit re-introduce the
    inline scan loop and let the two paths drift."""
    src = (Path(__file__).resolve().parent.parent / "kling_gui" / "main_window.py").read_text()
    # The pre-existing rescan block must call the shared helper.
    assert "self._scan_folders_for_new_media(folders)" in src, (
        "session-load rescan must call the shared helper, not inline a copy"
    )


def test_scan_folders_for_new_media_picks_up_new_video(tmp_path, monkeypatch):
    """End-to-end smoke for the shared helper. Build a session with one
    image, drop a new mp4 into its folder, call the helper, and assert
    the video shows up as source_type="video" in the session."""
    from kling_gui.image_state import ImageSession
    from kling_gui import main_window as mw

    # Create an image and a video in tmp_path
    img_path = tmp_path / "existing.png"
    # Tiny valid PNG (1x1 transparent)
    img_path.write_bytes(
        b"\x89PNG\r\n\x1a\n" + struct.pack(">I", 13) + b"IHDR"
        + struct.pack(">II", 1, 1) + b"\x08\x06\x00\x00\x00"
        + struct.pack(">I", zlib.crc32(
            b"IHDR" + struct.pack(">II", 1, 1) + b"\x08\x06\x00\x00\x00"
        ))
        + struct.pack(">I", 10)
        + b"IDAT" + b"\x78\x9c\x62\x00\x00\x00\x00\x05\x00\x01"
        + struct.pack(">I", 0)
        + struct.pack(">I", 0) + b"IEND" + struct.pack(">I", 0xae426082)
    )
    vid_path = tmp_path / "new_oldcam_v8.mp4"
    vid_path.write_bytes(b"\x00" * 1024)  # not a real mp4, just needs ext

    session = ImageSession()
    session.add_image(str(img_path), "input", make_active=True)

    # Build a minimal stub that has just the attributes the helper uses.
    class Stub:
        pass
    stub = Stub()
    stub.image_session = session
    # Bind the helper as if it were on the class.
    helper = mw.KlingGUIWindow._scan_folders_for_new_media.__get__(stub)
    added_imgs, added_vids = helper({str(tmp_path)})
    assert added_vids == 1, f"expected 1 new video, got {added_vids}"
    # The new entry must be a video
    paths = [(e.path, e.source_type) for e in session.images]
    assert (str(vid_path), "video") in paths


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
