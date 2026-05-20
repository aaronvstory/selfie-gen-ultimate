"""Tests for the Video Inspector V1 feature.

Coverage:
    MetadataTests          — filename parsing (regex round-trips,
                             negative phase, simna, full chain).
    DiscoveryTests         — folder scan, stem-collision regression,
                             precedence (rPPG > oldcam > raw).
    VideoFrameTests        — VideoFrame lifecycle with mocked cv2/PIL.
    CarouselOverlayTests   — carousel wiring: setters, button, overlay
                             via call-history pattern.
    ModalStructuralTests   — VideoInspectorModal source-regex checks
                             + geometry persistence on a __new__'d
                             instance.
    MainWindowWiringTests  — main_window.py source-regex checks.

NO test in this file requires a live Tk root, real cv2 frames, or
real video files. We mock cv2.VideoCapture, PIL.Image, and PIL.ImageTk
where needed.
"""

from __future__ import annotations

import os
import re
import tempfile
import threading
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from kling_gui.video_discovery import (
    VideoGroup,
    all_videos_for_image,
    clear_video_discovery_cache,
    find_video_for_image,
    find_video_groups,
)
from kling_gui.video_metadata import (
    RppgMetrics,
    VideoMetadata,
    load_sidecar_metrics,
    parse_kling_segment,
    parse_oldcam_segment,
    parse_rppg_segment,
    parse_similarity_from_stem,
    parse_video_filename,
)


# ──────────────────────────────────────────────────────────────────────
# Step 1 — MetadataTests
# ──────────────────────────────────────────────────────────────────────


class MetadataTests(unittest.TestCase):
    def test_parse_kling_segment_basic(self):
        residual, model, slot, take = parse_kling_segment(
            "front_crop_simna_001_k25tStd_p4_1"
        )
        self.assertEqual(residual, "front_crop_simna_001")
        self.assertEqual(model, "k25tStd")
        self.assertEqual(slot, 4)
        self.assertEqual(take, 1)

    def test_parse_kling_segment_no_tail(self):
        residual, model, slot, take = parse_kling_segment("user-named-video")
        self.assertEqual(residual, "user-named-video")
        self.assertIsNone(model)
        self.assertIsNone(slot)
        self.assertIsNone(take)

    def test_parse_oldcam_segment(self):
        residual, v = parse_oldcam_segment("clip_k25tStd_p1_1-oldcam-v24")
        self.assertEqual(residual, "clip_k25tStd_p1_1")
        self.assertEqual(v, 24)

    def test_parse_oldcam_segment_missing(self):
        residual, v = parse_oldcam_segment("clip_k25tStd_p1_1")
        self.assertEqual(residual, "clip_k25tStd_p1_1")
        self.assertIsNone(v)

    def test_parse_rppg_segment_with_positive_metrics(self):
        residual, has_rppg, metrics = parse_rppg_segment(
            "clip_k25tStd_p1_1-oldcam-v24-rppg - 13.08-7.8-0.70-0.03-0.46"
        )
        self.assertEqual(residual, "clip_k25tStd_p1_1-oldcam-v24")
        self.assertTrue(has_rppg)
        self.assertIsNotNone(metrics)
        # mypy-ish narrowing
        assert metrics is not None
        self.assertAlmostEqual(metrics.snr, 13.08)
        self.assertAlmostEqual(metrics.phase, 7.8)
        self.assertAlmostEqual(metrics.temporal, 0.70)

    def test_parse_rppg_segment_with_negative_phase(self):
        """The injector embeds negative phase as ``-75.5`` which collides
        with the ``-`` separator, producing a ``--`` in the suffix.
        Canonical splitter (automation.rppg.parse_metric_suffix) must
        reassemble correctly."""
        residual, has_rppg, metrics = parse_rppg_segment(
            "clip_k25tStd_p1_1-oldcam-v24-rppg - 7.72--75.5-0.79-0.06-0.35"
        )
        self.assertTrue(has_rppg)
        self.assertIsNotNone(metrics)
        assert metrics is not None
        self.assertAlmostEqual(metrics.snr, 7.72)
        self.assertAlmostEqual(metrics.phase, -75.5)
        self.assertAlmostEqual(metrics.harmonic, 0.35)

    def test_parse_rppg_segment_bare(self):
        residual, has_rppg, metrics = parse_rppg_segment(
            "clip_k25tStd_p1_1-rppg"
        )
        self.assertEqual(residual, "clip_k25tStd_p1_1")
        self.assertTrue(has_rppg)
        self.assertIsNone(metrics)

    def test_parse_similarity_score(self):
        score, na = parse_similarity_from_stem("front_crop_sim87_001")
        self.assertEqual(score, 87)
        self.assertFalse(na)

    def test_parse_similarity_simna(self):
        """selfie_generator emits literal 'simna' for no-face / no-match
        (selfie_generator.py:434). No underscore between sim and na."""
        score, na = parse_similarity_from_stem("front_crop_simna_001")
        self.assertIsNone(score)
        self.assertTrue(na)

    def test_parse_similarity_missing(self):
        score, na = parse_similarity_from_stem("user-renamed")
        self.assertIsNone(score)
        self.assertFalse(na)

    def test_parse_video_filename_real_harness_fixture(self):
        """oldcam-testing/rppg_harness.py:51 ships this exact name."""
        m = parse_video_filename(
            Path("front_crop_nano-banana-2-edit_sim87_001_k25tStd_p4_1.mp4")
        )
        self.assertEqual(m.base_stem, "front_crop_nano-banana-2-edit_sim87_001")
        self.assertEqual(m.model_short, "k25tStd")
        self.assertEqual(m.slot, 4)
        self.assertEqual(m.take, 1)
        self.assertEqual(m.similarity, 87)
        self.assertFalse(m.has_rppg)
        self.assertIsNone(m.oldcam_version)

    def test_parse_video_filename_full_chain_with_metrics(self):
        m = parse_video_filename(
            Path(
                "front_crop_nano-banana-2-edit_sim87_001_k25tStd_p4_1"
                "-oldcam-v24-rppg - 13.08-7.8-0.70-0.03-0.46.mp4"
            )
        )
        self.assertEqual(m.model_short, "k25tStd")
        self.assertEqual(m.oldcam_version, 24)
        self.assertTrue(m.has_rppg)
        self.assertIsNotNone(m.rppg_metrics)
        assert m.rppg_metrics is not None
        self.assertAlmostEqual(m.rppg_metrics.snr, 13.08)
        self.assertEqual(m.rppg_metrics_source, "filename")
        self.assertEqual(m.raw_suffixes, ["oldcam-v24", "rppg"])

    def test_parse_video_filename_unparseable_falls_back(self):
        m = parse_video_filename(Path("random-user-named.mp4"))
        self.assertEqual(m.base_stem, "random-user-named")
        self.assertIsNone(m.model_short)
        self.assertIsNone(m.oldcam_version)
        self.assertFalse(m.has_rppg)
        self.assertEqual(m.raw_suffixes, [])

    def test_parse_video_filename_looped_variant(self):
        """Codex P1 PR #43 (3272768118): the queue manager actively
        produces ``..._looped.mp4`` after Kling generation. The Kling-
        tail regex used to anchor at end-of-stem so _looped suffixes
        slipped past unparsed, breaking discovery's base_stem match.
        After the fix _looped is stripped BEFORE Kling parsing."""
        m = parse_video_filename(Path("front_k25tStd_p4_1_looped.mp4"))
        self.assertEqual(m.base_stem, "front")
        self.assertEqual(m.model_short, "k25tStd")
        self.assertEqual(m.slot, 4)
        self.assertEqual(m.take, 1)
        self.assertTrue(m.is_looped)
        self.assertIn("looped", m.raw_suffixes)

    def test_parse_video_filename_looped_oldcam_rppg_chain(self):
        """Full pipeline chain on a looped variant — every downstream
        stage's tail must strip cleanly so base_stem ends at the source
        image stem."""
        m = parse_video_filename(Path(
            "front_sim87_001_k25tStd_p4_1_looped-oldcam-v24"
            "-rppg - 13.08-7.8-0.70-0.03-0.46.mp4"
        ))
        self.assertEqual(m.base_stem, "front_sim87_001")
        self.assertTrue(m.is_looped)
        self.assertEqual(m.oldcam_version, 24)
        self.assertTrue(m.has_rppg)
        self.assertEqual(m.similarity, 87)
        # Pipeline-order suffix trail.
        self.assertEqual(m.raw_suffixes, ["looped", "oldcam-v24", "rppg"])

    def test_parse_video_filename_no_looped_marker_when_absent(self):
        """is_looped must be False when no _looped tail is present —
        otherwise the discovery sort key would mis-rank non-looped
        variants."""
        m = parse_video_filename(Path("front_k25tStd_p4_1.mp4"))
        self.assertFalse(m.is_looped)
        self.assertNotIn("looped", m.raw_suffixes)

    def test_load_sidecar_metrics_canonical_nested_schema(self):
        """Real producer (automation.rppg.finalize_rppg_output) writes
        the metrics NESTED under a "metrics" key alongside "source"
        and "order" siblings. My first impl only read top-level keys
        which silently dropped every real sidecar (Codex PR #43 P2,
        finding 3272968651).
        """
        with tempfile.TemporaryDirectory() as td:
            folder = Path(td)
            video = folder / "clip-rppg.mp4"
            video.touch()
            sidecar = folder / "clip-rppg.metrics.json"
            sidecar.write_text(
                '{"source": "clip-rppg - 11.89-25.9-0.81-0.04-0.42.mp4", '
                '"metrics": {"snr": 11.89, "phase": 25.9, "temporal": 0.81, '
                '"motion": 0.04, "harmonic": 0.42}, '
                '"order": ["snr", "phase", "temporal", "motion", "harmonic"]}',
                encoding="utf-8",
            )
            metrics = load_sidecar_metrics(video)
            self.assertIsNotNone(metrics)
            assert metrics is not None
            self.assertAlmostEqual(metrics.snr, 11.89)
            self.assertAlmostEqual(metrics.phase, 25.9)
            self.assertAlmostEqual(metrics.harmonic, 0.42)

    def test_load_sidecar_metrics_legacy_flat_schema(self):
        """Backward compatibility: a hand-written / third-party sidecar
        with flat top-level keys still parses. We prefer nested but
        fall through to top-level if "metrics" isn't a dict."""
        with tempfile.TemporaryDirectory() as td:
            folder = Path(td)
            video = folder / "clip-rppg.mp4"
            video.touch()
            sidecar = folder / "clip-rppg.metrics.json"
            sidecar.write_text(
                '{"snr": 7.72, "phase": -75.5, "temporal": 0.79, '
                '"motion": 0.06, "harmonic": 0.35}',
                encoding="utf-8",
            )
            metrics = load_sidecar_metrics(video)
            self.assertIsNotNone(metrics)
            assert metrics is not None
            self.assertAlmostEqual(metrics.phase, -75.5)

    def test_load_sidecar_metrics_handles_dotted_base_stem(self):
        """Codex PR #43 P2 (3273308452): filenames containing internal
        dots (e.g. 'front.v1_clip-rppg.mp4') silently dropped their
        sidecar because the prior with_suffix('').with_suffix(...)
        chain stripped everything past the last internal dot. The
        producer writes 'front.v1_clip-rppg.metrics.json' but lookup
        was searching for 'front.metrics.json'. Fix: use with_name
        + .stem so the full base stem is preserved.
        """
        with tempfile.TemporaryDirectory() as td:
            folder = Path(td)
            # Filename with TWO dots in the base stem before .mp4.
            video = folder / "front.v1_clip-rppg.mp4"
            video.touch()
            # Producer's exact filename pattern.
            sidecar = folder / "front.v1_clip-rppg.metrics.json"
            sidecar.write_text(
                '{"source": "front.v1_clip-rppg - 11.89.mp4", '
                '"metrics": {"snr": 11.89, "phase": 25.9, '
                '"temporal": 0.81, "motion": 0.04, "harmonic": 0.42}, '
                '"order": ["snr","phase","temporal","motion","harmonic"]}',
                encoding="utf-8",
            )
            metrics = load_sidecar_metrics(video)
            self.assertIsNotNone(
                metrics,
                "Sidecar with dotted base stem must still be found — "
                "regression for the with_suffix('') stem-collapse bug.",
            )
            assert metrics is not None
            self.assertAlmostEqual(metrics.snr, 11.89)

    def test_load_sidecar_metrics_missing(self):
        with tempfile.TemporaryDirectory() as td:
            video = Path(td) / "clip-rppg.mp4"
            video.touch()
            self.assertIsNone(load_sidecar_metrics(video))

    def test_load_sidecar_metrics_malformed(self):
        with tempfile.TemporaryDirectory() as td:
            folder = Path(td)
            video = folder / "clip-rppg.mp4"
            video.touch()
            (folder / "clip-rppg.metrics.json").write_text(
                "{not valid json", encoding="utf-8"
            )
            self.assertIsNone(load_sidecar_metrics(video))


# ──────────────────────────────────────────────────────────────────────
# Step 2 — DiscoveryTests
# ──────────────────────────────────────────────────────────────────────


class DiscoveryTests(unittest.TestCase):
    def setUp(self):
        # Cache is global; clear between tests so a previous test's
        # tmp_path doesn't influence the current one (even though mtime
        # differs in practice, this is belt-and-suspenders).
        clear_video_discovery_cache()

    def _make_folder(self, td: str, *names: str) -> Path:
        folder = Path(td)
        for n in names:
            (folder / n).touch()
        return folder

    def test_find_video_groups_groups_by_base_stem(self):
        with tempfile.TemporaryDirectory() as td:
            folder = self._make_folder(
                td,
                "front.png",
                "front_k25tStd_p4_1.mp4",
                "front_k25tStd_p4_1-oldcam-v8.mp4",
                "front_k25tStd_p4_1-oldcam-v24.mp4",
            )
            groups = find_video_groups(folder)
            self.assertEqual(len(groups), 1)
            g = groups[0]
            self.assertEqual(g.base_stem, "front")
            self.assertIsNotNone(g.image_path)
            assert g.image_path is not None
            self.assertEqual(g.image_path.name, "front.png")
            self.assertEqual(len(g.videos), 3)

    def test_find_video_groups_stem_collision_regression(self):
        """The bug we're guarding against: 'front.png' must NOT swallow
        'front_extra_..._k25tStd_p1_1.mp4'. Exact base_stem equality
        only, no startswith."""
        with tempfile.TemporaryDirectory() as td:
            folder = self._make_folder(
                td,
                "front.png",
                "front_k25tStd_p4_1.mp4",
                "front_extra.png",
                "front_extra_k25tStd_p1_1.mp4",
            )
            groups = find_video_groups(folder)
            keys = {g.base_stem for g in groups}
            self.assertEqual(keys, {"front", "front_extra"})
            front_grp = next(g for g in groups if g.base_stem == "front")
            self.assertEqual(len(front_grp.videos), 1)
            self.assertEqual(
                front_grp.videos[0].path.name, "front_k25tStd_p4_1.mp4"
            )

    def test_find_video_for_image_selects_most_processed(self):
        """Precedence rule: rPPG > oldcam > raw Kling."""
        with tempfile.TemporaryDirectory() as td:
            folder = self._make_folder(
                td,
                "front.png",
                "front_k25tStd_p4_1.mp4",
                "front_k25tStd_p4_1-oldcam-v24.mp4",
                "front_k25tStd_p4_1-oldcam-v24-rppg - 13.08-7.8-0.70-0.03-0.46.mp4",
            )
            chosen = find_video_for_image(folder / "front.png")
            self.assertIsNotNone(chosen)
            assert chosen is not None
            self.assertIn("rppg", chosen.name)

    def test_find_video_for_image_orphan_returns_none(self):
        with tempfile.TemporaryDirectory() as td:
            folder = self._make_folder(td, "front_extra.png")
            self.assertIsNone(find_video_for_image(folder / "front_extra.png"))

    def test_find_video_for_image_collision_isolation(self):
        """The stem-collision regression — front.png must not return
        front_extra's video."""
        with tempfile.TemporaryDirectory() as td:
            folder = self._make_folder(
                td,
                "front.png",
                "front_extra.png",
                "front_extra_k25tStd_p1_1.mp4",
            )
            self.assertIsNone(find_video_for_image(folder / "front.png"))
            fp = find_video_for_image(folder / "front_extra.png")
            self.assertIsNotNone(fp)
            assert fp is not None
            self.assertEqual(fp.name, "front_extra_k25tStd_p1_1.mp4")

    def test_all_videos_for_image_sorts_by_progression(self):
        with tempfile.TemporaryDirectory() as td:
            folder = self._make_folder(
                td,
                "front_k25tStd_p4_1-oldcam-v24-rppg - 13.08-7.8-0.70-0.03-0.46.mp4",
                "front_k25tStd_p4_1.mp4",
                "front_k25tStd_p4_1-oldcam-v8.mp4",
                "front_k25tStd_p4_1-oldcam-v24.mp4",
            )
            ordered = all_videos_for_image(folder / "front.png")
            names = [v.path.name for v in ordered]
            # Raw < oldcam (ascending version) < rPPG
            self.assertEqual(names[0], "front_k25tStd_p4_1.mp4")
            self.assertIn("oldcam-v8", names[1])
            self.assertIn("oldcam-v24", names[2])
            self.assertTrue(names[-1].endswith("0.46.mp4"))

    def test_find_video_groups_non_recursive(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            (root / "sub").mkdir()
            (root / "sub" / "front_k25tStd_p1_1.mp4").touch()
            self.assertEqual(find_video_groups(root), [])

    def test_raw_kling_tie_break_take_dominates_slot(self):
        """Selector contract (plan): raw-Kling tie-break is 'highest
        take, then highest slot'. So slot=2 take=5 must beat
        slot=4 take=1 — take wins. CodeRabbit PR #43 flagged the
        original (slot-before-take) ordering as a contract violation."""
        with tempfile.TemporaryDirectory() as td:
            folder = self._make_folder(
                td,
                "front.png",
                "front_k25tStd_p4_1.mp4",   # slot=4 take=1
                "front_k25tStd_p2_5.mp4",   # slot=2 take=5 — should win
            )
            chosen = find_video_for_image(folder / "front.png")
            self.assertIsNotNone(chosen)
            assert chosen is not None
            self.assertEqual(chosen.name, "front_k25tStd_p2_5.mp4")

    def test_looped_variant_sorts_above_raw_kling(self):
        """Codex PR #43 P1: _looped variants are a downstream stage
        between raw Kling and oldcam. For a group with no oldcam/rPPG,
        the looped variant should be the "best" (most-processed) pick.
        """
        with tempfile.TemporaryDirectory() as td:
            folder = self._make_folder(
                td,
                "front.png",
                "front_k25tStd_p4_1.mp4",          # raw
                "front_k25tStd_p4_1_looped.mp4",   # looped — should win
            )
            chosen = find_video_for_image(folder / "front.png")
            self.assertIsNotNone(chosen)
            assert chosen is not None
            self.assertEqual(chosen.name, "front_k25tStd_p4_1_looped.mp4")

    def test_oldcam_variant_sorts_above_looped(self):
        """A looped+oldcam chain MUST sort above a bare looped (oldcam
        is downstream of looping). Defends against a regression where
        is_looped accidentally dominates oldcam_version in the sort key."""
        with tempfile.TemporaryDirectory() as td:
            folder = self._make_folder(
                td,
                "front.png",
                "front_k25tStd_p4_1.mp4",
                "front_k25tStd_p4_1_looped.mp4",
                "front_k25tStd_p4_1_looped-oldcam-v24.mp4",  # should win
            )
            chosen = find_video_for_image(folder / "front.png")
            self.assertIsNotNone(chosen)
            assert chosen is not None
            self.assertEqual(
                chosen.name,
                "front_k25tStd_p4_1_looped-oldcam-v24.mp4",
            )

    def test_find_video_for_image_caches_within_mtime(self):
        """Carousel calls this on every redraw — without caching, an
        iterdir() runs per resize tick. Cache hits on the same
        (folder, image, mtime) tuple should NOT re-scan."""
        from unittest.mock import patch
        with tempfile.TemporaryDirectory() as td:
            folder = self._make_folder(
                td,
                "front.png",
                "front_k25tStd_p4_1.mp4",
            )
            # Prime the cache.
            chosen_first = find_video_for_image(folder / "front.png")
            # Now patch all_videos_for_image to detect a re-scan.
            with patch(
                "kling_gui.video_discovery.all_videos_for_image"
            ) as mock_scan:
                mock_scan.return_value = []
                chosen_second = find_video_for_image(folder / "front.png")
            self.assertEqual(chosen_first, chosen_second)
            # all_videos_for_image was NOT called (cache hit).
            mock_scan.assert_not_called()


# ──────────────────────────────────────────────────────────────────────
# Step 3 — VideoFrameTests
# ──────────────────────────────────────────────────────────────────────


# Module-import-time skip if cv2 / numpy / PIL are missing. The
# carousel test pattern uses skipTest in setUp; we use module-level
# skip via a guard import so collection itself stays cheap.
try:
    import cv2 as _cv2_check  # noqa: F401
    import numpy as _np_check  # noqa: F401
    _CV2_OK = True
except ImportError:
    _CV2_OK = False

try:
    from PIL import Image as _PIL_Image_check  # noqa: F401
    _PIL_OK = True
except ImportError:
    _PIL_OK = False


class _FakeCap:
    """Mock for cv2.VideoCapture. Returns N constant fake frames then EOF."""

    def __init__(self, n_frames: int = 10, fps: float = 25.0):
        self._n = n_frames
        self._fps = fps
        self._pos = 0
        self.released = False
        self.set_calls = []

    def read(self):
        if self._pos >= self._n:
            return (False, None)
        import numpy as np
        self._pos += 1
        return (True, np.zeros((4, 4, 3), dtype=np.uint8))

    def get(self, prop):
        import cv2
        if prop == cv2.CAP_PROP_FRAME_COUNT:
            return self._n
        if prop == cv2.CAP_PROP_FPS:
            return self._fps
        if prop == cv2.CAP_PROP_POS_FRAMES:
            return self._pos
        return 0

    def set(self, prop, val):
        self.set_calls.append((prop, val))
        import cv2
        if prop == cv2.CAP_PROP_POS_FRAMES:
            self._pos = int(val)
            return True
        return False

    def release(self):
        self.released = True

    def isOpened(self):
        return not self.released


@unittest.skipUnless(_CV2_OK and _PIL_OK, "cv2/numpy/PIL not installed")
class VideoFrameTests(unittest.TestCase):
    """VideoFrame lifecycle WITHOUT a real Tk root.

    We construct via __new__, hand-attach a stub canvas, then exercise
    the public API. cv2.VideoCapture is mocked to never touch a real
    file on disk.
    """

    def _make_frame(self):
        from kling_gui.video_inspector import VideoFrame
        f = VideoFrame.__new__(VideoFrame)
        # Manual state bootstrap (bypassing __init__ which would need Tk root).
        f._title = ""
        f._log_callback = None
        f._video_path = None
        f._cv2_cap = None
        f._cv2 = None
        f._frame_count = 0
        f._fps = 25.0
        f._current_frame = -1
        f._photo = None
        f._overlay_drawer = None
        # Live canvas dimensions (PR #43 bdead49 — _render_pil_image
        # and _show_error now read these instead of _DISPLAY_W/H).
        # Seed to the constants so existing tests behave identically.
        from kling_gui.video_inspector import _DISPLAY_W, _DISPLAY_H
        f._canvas_w = _DISPLAY_W
        f._canvas_h = _DISPLAY_H
        # _canvas_dims is the atomic snapshot for the decoder thread
        # (post-79802bc self-review fix). Seed it to match.
        f._canvas_dims = (_DISPLAY_W, _DISPLAY_H)
        # Generation-id locking (new in the PR-43 race fix).
        f._generation_id = 0
        f._stop_event = threading.Event()
        f._cap_lock = threading.Lock()
        import queue
        f._frame_request = queue.Queue(maxsize=1)
        f._decoder_thread = None
        # Stub the Tk-derived widget methods we use.
        f._canvas = mock.MagicMock()
        f._open_external_btn = mock.MagicMock()
        # winfo_exists used by guards — always True for the test.
        f.winfo_exists = mock.MagicMock(return_value=True)
        # after() should run callbacks SYNCHRONOUSLY for the test so we
        # don't actually need a Tk event loop.
        f.after = lambda _ms, fn, *args: fn(*args) if callable(fn) else None
        return f

    def test_load_success_path(self):
        f = self._make_frame()
        with mock.patch("cv2.VideoCapture", return_value=_FakeCap()):
            with mock.patch(
                "kling_gui.video_inspector.VideoFrame._decoder_loop",
                lambda self, *args, **kwargs: None,
            ):
                ok = f.load(Path("does-not-matter.mp4"))
        self.assertTrue(ok)
        self.assertTrue(f.is_loaded())
        self.assertEqual(f.get_frame_count(), 10)
        self.assertEqual(f.get_fps(), 25.0)
        # Clean up so subsequent tests don't see the leftover thread.
        f.clear()

    def test_load_fails_when_cv2_capture_returns_unopened(self):
        f = self._make_frame()
        bad_cap = _FakeCap()
        bad_cap.released = True  # forces isOpened() == False
        with mock.patch("cv2.VideoCapture", return_value=bad_cap):
            ok = f.load(Path("broken.mp4"))
        self.assertFalse(ok)
        self.assertFalse(f.is_loaded())
        # Error text was drawn AND open-externally was enabled (we have
        # a path on hand even on failure).
        f._open_external_btn.config.assert_any_call(state=mock.ANY)

    def test_clear_signals_stop_and_detaches_capture(self):
        """clear() signals stop, detaches self._cv2_cap, and drops the
        PhotoImage GC anchor. It does NOT call cap.release() directly
        — Codex P1 (3273416655) on PR #43: that races a mid-flight
        cap.read() on the decoder thread. The worker now owns release
        via its finally block (see test_decoder_loop_releases_cap_in_finally).
        """
        f = self._make_frame()
        with mock.patch("cv2.VideoCapture", return_value=_FakeCap()):
            with mock.patch(
                "kling_gui.video_inspector.VideoFrame._decoder_loop",
                lambda self, *args, **kwargs: None,
            ):
                f.load(Path("ok.mp4"))
        cap = f._cv2_cap
        f.clear()
        self.assertIsNone(f._cv2_cap)
        self.assertIsNone(f._photo)
        self.assertTrue(f._stop_event.is_set())
        # CRITICAL: clear() must NOT have called cap.release(). That
        # would race mid-flight reads on the worker thread. The cap is
        # held by the worker until it exits its finally block.
        assert cap is not None
        self.assertFalse(
            cap.released,
            "clear() must NOT call cap.release() — racing the decoder "
            "thread's cap.read() can crash OpenCV (Codex PR #43 P1).",
        )

    def test_decoder_loop_releases_cap_in_finally(self):
        """Codex P1 (3273416655) on PR #43: the cap is released by
        the decoder thread on exit, NOT by the Tk-thread clear().
        Drive _decoder_loop with a stop_event already set so it exits
        immediately, then verify cap.released became True via the
        finally block."""
        from kling_gui.video_inspector import VideoFrame
        f = self._make_frame()
        cap = _FakeCap()
        stop_event = threading.Event()
        stop_event.set()  # forces immediate while-loop exit
        import queue
        rq = queue.Queue(maxsize=1)
        # Pre-import cv2 module ref + a dummy Image-stub via
        # _decoder_loop's lazy PIL import that the no-frame path skips.
        import cv2 as _cv2
        # Invoke the real loop; it must release cap in its finally.
        VideoFrame._decoder_loop(
            f, generation_id=1, stop_event=stop_event,
            request_queue=rq, cap=cap, cv2_mod=_cv2,
        )
        self.assertTrue(
            cap.released,
            "Decoder thread MUST release cap in its finally block — "
            "this is the serialization guarantee that prevents the "
            "close-during-read crash.",
        )

    def test_render_pil_image_drops_stale_generation(self):
        """Generation-locking guard: a frame posted by an old decoder
        thread (after a newer load() has already incremented the
        generation) must be dropped — never reach canvas/PhotoImage
        construction. This is the GPT-5.5-flagged race protection."""
        f = self._make_frame()
        # Simulate: we're currently at generation 7, but a stale worker
        # from generation 3 just woke up and posted a frame.
        f._generation_id = 7
        with mock.patch(
            "kling_gui.video_inspector.tk.PhotoImage"
        ) as mock_photo:
            f._render_pil_image(mock.MagicMock(), frame_index=0, generation_id=3)
        # Canvas was NOT touched and PhotoImage was NOT built.
        mock_photo.assert_not_called()
        f._canvas.delete.assert_not_called()
        f._canvas.create_image.assert_not_called()

    def test_clear_bumps_generation_so_pending_callbacks_are_invalidated(self):
        """clear() must bump the generation counter so any after-callback
        the worker has already queued sees a mismatched gen and aborts."""
        f = self._make_frame()
        f._generation_id = 5
        # Simulate a load-cap so clear has something to release.
        f._cv2_cap = _FakeCap()
        f.clear()
        self.assertGreater(f._generation_id, 5)

    def test_decoder_loop_skips_seek_on_sequential_reads(self):
        """Sequential playback shouldn't call cap.set(POS_FRAMES) on
        every frame — that's O(N) from the nearest keyframe for
        H.264/H.265 codecs (Gemini PR #43 finding). Only seek when
        the next requested frame is >1 frame away from the cap's
        current position."""
        import cv2 as _cv2
        from kling_gui.video_inspector import VideoFrame
        f = self._make_frame()
        cap = _FakeCap(n_frames=20)
        # _FakeCap.set/get track POS_FRAMES; spy on set_calls.
        f._cap_lock = threading.Lock()
        # Stage 3 sequential frame requests, then sentinel to exit.
        f._frame_request.put_nowait(0)
        stop = threading.Event()
        # Drive _decoder_loop directly in this thread (no daemon).
        # We need the loop to consume the queue then exit cleanly.
        def feeder():
            # Wait a moment so loop pulls frame 0, then push 1, 2, 3.
            import time
            time.sleep(0.05)
            f._frame_request.put(1)
            time.sleep(0.05)
            f._frame_request.put(2)
            time.sleep(0.05)
            f._frame_request.put(3)
            time.sleep(0.05)
            f._frame_request.put(-1)  # sentinel exit
        threading.Thread(target=feeder, daemon=True).start()
        # Run the real loop — it will exit when it sees -1.
        VideoFrame._decoder_loop(
            f, generation_id=1, stop_event=stop,
            request_queue=f._frame_request, cap=cap, cv2_mod=_cv2,
        )
        # Frame 0 always seeks (current=0, requested=0, but the loop
        # treats first call as a fresh open and may seek). Frames 1, 2,
        # 3 are sequential from cv2's perspective — should NOT seek.
        # We assert AT MOST one set() call in total (the optional
        # initial seek). Without the optimization, this would be 4.
        seeks = [c for c in cap.set_calls if c[0] == _cv2.CAP_PROP_POS_FRAMES]
        self.assertLessEqual(
            len(seeks), 1,
            f"Sequential playback issued {len(seeks)} seeks; expected ≤1. "
            f"calls={cap.set_calls}",
        )


# ──────────────────────────────────────────────────────────────────────
# Step 5 — CarouselOverlayTests
# ──────────────────────────────────────────────────────────────────────


class CarouselOverlayTests(unittest.TestCase):
    """Verify the carousel's overlay/setter wiring via call-history
    pattern (mirrors tests/test_carousel_ref_controls.py)."""

    def test_setters_register_callbacks(self):
        from kling_gui.carousel_widget import ImageCarousel
        tab = ImageCarousel.__new__(ImageCarousel)
        tab._on_video_callback = None
        tab._on_video_inspector_toolbar_cb = None
        cb_a = mock.Mock()
        cb_b = mock.Mock()
        tab.set_on_video(cb_a)
        tab.set_on_video_toolbar(cb_b)
        self.assertIs(tab._on_video_callback, cb_a)
        self.assertIs(tab._on_video_inspector_toolbar_cb, cb_b)

    def test_open_video_inspector_invokes_toolbar_callback(self):
        from kling_gui.carousel_widget import ImageCarousel
        tab = ImageCarousel.__new__(ImageCarousel)
        tab._on_video_inspector_toolbar_cb = None
        cb = mock.Mock()
        tab.set_on_video_toolbar(cb)
        tab._on_open_video_inspector()
        cb.assert_called_once_with()

    def test_video_button_exists_in_source(self):
        from kling_gui import carousel_widget
        src = Path(carousel_widget.__file__).read_text(encoding="utf-8")
        # Button declared via tk.Button(...) with text="Videos"
        self.assertIn("self.video_inspector_btn = tk.Button", src)
        self.assertIn('text="Videos"', src)
        # macOS button fix MUST be applied (per project memory).
        self.assertRegex(
            src, r"apply_macos_button_fix\(self\.video_inspector_btn\)"
        )

    def test_overlay_block_references_find_video_for_image(self):
        from kling_gui import carousel_widget
        src = Path(carousel_widget.__file__).read_text(encoding="utf-8")
        self.assertIn("from .video_discovery import find_video_for_image", src)
        # The overlay block uses tag_bind (scoped) not canvas.bind (global)
        # so we don't conflict with existing <Button-3> binding.
        self.assertRegex(
            src, r"canvas\.tag_bind\(\s*bg_id,\s*\"<Button-1>\""
        )


# ──────────────────────────────────────────────────────────────────────
# Step 4 — ModalStructuralTests
# ──────────────────────────────────────────────────────────────────────


class ModalStructuralTests(unittest.TestCase):
    def test_modal_class_and_factory_present(self):
        from kling_gui import video_inspector
        src = Path(video_inspector.__file__).read_text(encoding="utf-8")
        self.assertIn(
            "class VideoInspectorModal(tk.Toplevel)", src
        )
        self.assertIn("def open_video_inspector(", src)

    def test_modal_uses_transient_not_global_grab(self):
        """grab_set_global() steals system focus on macOS Sonoma; this
        modal must NEVER use it. transient(parent) is required."""
        from kling_gui import video_inspector
        src = Path(video_inspector.__file__).read_text(encoding="utf-8")
        self.assertIn("self.transient(parent)", src)
        # The string can appear in an anti-pattern comment; reject only
        # actual call sites (paren-prefixed).
        self.assertNotIn("grab_set_global()", src)
        self.assertNotIn("self.grab_set_global", src)

    def test_modal_after_cancels_master_timer_on_destroy(self):
        """Master after-job must be cancelled in destroy() BEFORE
        super().destroy() invalidates self. Otherwise: leaked ticks."""
        from kling_gui import video_inspector
        src = Path(video_inspector.__file__).read_text(encoding="utf-8")
        self.assertIn("self.after_cancel", src)

    def test_modal_geometry_config_key(self):
        from kling_gui import video_inspector
        src = Path(video_inspector.__file__).read_text(encoding="utf-8")
        self.assertIn("video_inspector_window", src)

    def test_shift_double_click_handler_returns_break(self):
        """Critical: ``_on_listbox_shift_double`` MUST return ``"break"``
        so Tk halts the binding chain. Without it,
        ``<Shift-Double-Button-1>`` propagates to ``<Double-Button-1>``
        which calls the slot-A load — silently overwriting the slot B
        the user actually asked for.

        We assert BOTH the return value (behavioural) AND the source
        annotation (so a future refactor that drops the return value
        can't silently regress).
        """
        from kling_gui.video_inspector import VideoInspectorModal
        modal = VideoInspectorModal.__new__(VideoInspectorModal)
        # Stub the only attribute _load_selection_into reads. Make
        # _load_selection_into itself a no-op so we don't need a real
        # listbox.
        modal._load_selection_into = mock.MagicMock()
        result = modal._on_listbox_shift_double(_event=mock.MagicMock())
        # Behavioural: handler routed to B and returned the break.
        modal._load_selection_into.assert_called_once_with("B")
        self.assertEqual(
            result, "break",
            "Shift+DoubleClick handler must return 'break' to halt Tk's "
            "binding chain — otherwise it ALSO triggers the plain "
            "<Double-Button-1> handler (slot A), silently overwriting "
            "the user's intended slot B load.",
        )

    def test_shift_double_click_handler_annotated_as_returning_str(self):
        """Source-regex lock: the handler signature must annotate ``-> str``
        and contain ``return "break"``. Catches refactors that change
        the signature back to ``-> None`` without realizing they're
        breaking the Tk event-chain halt."""
        from kling_gui import video_inspector
        src = Path(video_inspector.__file__).read_text(encoding="utf-8")
        # Pin both the signature and the explicit return.
        self.assertRegex(
            src,
            r"def _on_listbox_shift_double\(self,\s*_event\)\s*->\s*str:",
            "_on_listbox_shift_double must be annotated -> str.",
        )
        # Locate the function body and check for the return.
        start = src.index("def _on_listbox_shift_double")
        end = src.index("\n    def ", start + 10)
        body = src[start:end]
        self.assertIn(
            'return "break"', body,
            "_on_listbox_shift_double must `return \"break\"` to halt "
            "Tk's event-chain propagation.",
        )

    def test_listbox_binds_macos_secondary_click_variants(self):
        """Codex PR #43 P2 (3273366968): macOS Tk reports trackpad
        secondary-click as ``<Button-2>`` OR ``<Control-Button-1>`` —
        NOT ``<Button-3>``. Binding only Button-3 makes the advertised
        right-click-to-B path silently fail on macOS. ALL THREE
        sequences MUST be bound to ``_on_listbox_right_click``."""
        from kling_gui import video_inspector
        src = Path(video_inspector.__file__).read_text(encoding="utf-8")
        # All three bindings must point at the same handler so the
        # cross-platform parity is real.
        for seq in ("<Button-3>", "<Button-2>", "<Control-Button-1>"):
            self.assertRegex(
                src,
                rf'self\._listbox\.bind\(\s*"{seq}",\s*self\._on_listbox_right_click',
                f"Listbox must bind {seq} to _on_listbox_right_click "
                f"for cross-platform parity (macOS Tk fallback).",
            )

    def test_persist_geometry_writes_config_and_calls_save(self):
        """Lightweight behavioural test: __new__'d modal + stubbed
        winfo_* methods => _persist_geometry writes the expected dict."""
        from kling_gui.video_inspector import VideoInspectorModal
        modal = VideoInspectorModal.__new__(VideoInspectorModal)
        modal._config = {}
        modal._GEOMETRY_KEY = "video_inspector_window"
        modal._save_config_fn = mock.Mock()
        modal.winfo_width = mock.MagicMock(return_value=1200)
        modal.winfo_height = mock.MagicMock(return_value=600)
        modal.winfo_rootx = mock.MagicMock(return_value=100)
        modal.winfo_rooty = mock.MagicMock(return_value=50)
        modal._persist_geometry()
        self.assertEqual(
            modal._config["video_inspector_window"],
            {"w": 1200, "h": 600, "x": 100, "y": 50},
        )
        modal._save_config_fn.assert_called_once_with()


# ──────────────────────────────────────────────────────────────────────
# Step 6 — MainWindowWiringTests
# ──────────────────────────────────────────────────────────────────────


class MainWindowWiringTests(unittest.TestCase):
    def test_main_window_wires_video_callbacks(self):
        from kling_gui import main_window
        src = Path(main_window.__file__).read_text(encoding="utf-8")
        # Carousel callbacks wired
        self.assertIn("self.carousel.set_on_video(", src)
        self.assertIn("self.carousel.set_on_video_toolbar(", src)
        # Singleton ref present
        self.assertIn("self._video_inspector_window", src)
        # Factory called from inside _open_video_inspector
        self.assertIn("def _open_video_inspector", src)
        self.assertIn("open_video_inspector(", src)


if __name__ == "__main__":
    unittest.main()
