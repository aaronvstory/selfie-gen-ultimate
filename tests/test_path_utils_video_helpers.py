"""Regression tests for the video-extension helpers added in
PR #53 round 10.

These cover the public `path_utils.is_video_path` predicate that
GUI ingest sites use to surface a friendly "videos go in Step 3"
message instead of the cryptic generic "unsupported extension"
preflight rejection.
"""

import path_utils


def test_video_extensions_is_well_known_set():
    """The exported set must include the common video formats users
    actually drop into the image zone by mistake (.mp4 is the most
    common — Kling produces those).
    """
    expected_minimum = {".mp4", ".mov", ".webm", ".mkv", ".avi"}
    assert expected_minimum.issubset(path_utils.VIDEO_EXTENSIONS)


def test_is_video_path_recognises_common_formats():
    assert path_utils.is_video_path("clip.mp4") is True
    assert path_utils.is_video_path("clip.MP4") is True   # case-insensitive
    assert path_utils.is_video_path("/abs/path/x.mov") is True
    assert path_utils.is_video_path("relative/y.webm") is True
    assert path_utils.is_video_path("z.mkv") is True
    assert path_utils.is_video_path("w.avi") is True
    assert path_utils.is_video_path("foo.m4v") is True


def test_is_video_path_rejects_images_and_others():
    assert path_utils.is_video_path("front.jpg") is False
    assert path_utils.is_video_path("front.JPEG") is False
    assert path_utils.is_video_path("face.png") is False
    assert path_utils.is_video_path("photo.webp") is False
    assert path_utils.is_video_path("doc.pdf") is False
    assert path_utils.is_video_path("noext") is False


def test_is_video_path_handles_falsy_input():
    """Defensive: None / empty paths return False rather than raise.
    Ingest paths can hit this with sentinel values during error
    recovery; the helper must not become a crash source itself.
    """
    assert path_utils.is_video_path("") is False
    assert path_utils.is_video_path(None) is False  # type: ignore[arg-type]


def test_video_extensions_matches_image_state_constant():
    """Sanity-check: the path_utils.VIDEO_EXTENSIONS exported here
    is a SUPERSET of kling_gui.image_state._VIDEO_EXTENSIONS so the
    two never silently disagree about what counts as a video. If
    image_state ever adds a new extension, this test surfaces the
    drift.
    """
    try:
        from kling_gui.image_state import _VIDEO_EXTENSIONS as _legacy
    except ImportError:
        import pytest
        pytest.skip("kling_gui.image_state not importable in this environment")
    # Every extension image_state recognises as video must also be
    # in the public path_utils set. path_utils may have MORE (e.g.
    # .m4v) — that's fine.
    assert set(_legacy).issubset(path_utils.VIDEO_EXTENSIONS), (
        f"image_state has extensions not in path_utils.VIDEO_EXTENSIONS: "
        f"{set(_legacy) - path_utils.VIDEO_EXTENSIONS}"
    )
