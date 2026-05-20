"""Regression test for unknown-frame-count playback (Codex P2 PR #43).

Some containers/codecs don't report ``cv2.CAP_PROP_FRAME_COUNT``;
``VideoFrame.get_frame_count()`` returns 0. The prior ``_tick``
substituted ``total = 1`` then wrapped ``_master_frame % 1 = 0`` always
— so the master clock pinned at frame 0 and the video could never play
through. Fix: treat 0 as "unknown" and advance unboundedly.
"""

from __future__ import annotations

from pathlib import Path
import unittest


def _read_video_inspector_source() -> str:
    return (
        Path(__file__).resolve().parent.parent
        / "kling_gui" / "video_inspector.py"
    ).read_text(encoding="utf-8")


class UnknownFrameCountTests(unittest.TestCase):
    def test_tick_treats_zero_frame_count_as_unknown_length(self):
        """_tick must NOT substitute total=1 when get_frame_count() is 0.
        That substitution combined with modulo wrap pinned _master_frame
        at 0 forever, freezing playback on unknown-length containers."""
        src = _read_video_inspector_source()
        # Locate _tick body.
        start = src.index("def _tick(self) -> None:")
        end = src.index("\n    def ", start + 10)
        body = src[start:end]
        # The old broken pattern.
        self.assertNotIn(
            "total = primary.get_frame_count() or 1", body,
            "Prior 'or 1' substitution treats unknown-length videos as "
            "1-frame loops. Use the explicit 'if total > 0' branch.",
        )
        # The new correct pattern: explicit branching on total > 0.
        self.assertRegex(
            body,
            r"total\s*=\s*primary\.get_frame_count\(\)\s*\n\s*if total\s*>\s*0:",
            "Frame-count handling must branch explicitly on total > 0 "
            "so 0 means 'unknown' (advance unboundedly), not '1-frame "
            "loop' (modulo pins master_frame to 0).",
        )

    def test_master_frame_advances_unboundedly_when_total_unknown(self):
        """In the total==0 branch the master clock just adds step;
        the modulo wrap MUST NOT apply (it would still pin at 0)."""
        src = _read_video_inspector_source()
        start = src.index("def _tick(self) -> None:")
        end = src.index("\n    def ", start + 10)
        body = src[start:end]
        # The else branch — unconstrained addition.
        self.assertRegex(
            body,
            r"else:\s*\n\s*self\._master_frame\s*=\s*self\._master_frame\s*\+\s*step",
        )


class RenderUsesLiveCanvasCenterTests(unittest.TestCase):
    """Codex P1 (3273246454) on bdead49: ``_render_pil_image`` was
    placing frames at the FIXED ``_DISPLAY_W/H`` center (240, 135)
    instead of the LIVE ``self._canvas_w/h`` center. After any modal
    resize, every playback tick re-anchored the frame to the
    pre-resize position, so videos appeared offset in larger/smaller
    slots even though the aspect-fit resize logic was using live
    dimensions correctly.

    Lock that the render path uses live coords + that ``_show_error``
    + the ``clear()`` placeholder do the same.
    """

    def test_render_pil_image_uses_self_canvas_w_h(self):
        src = _read_video_inspector_source()
        # Locate _render_pil_image body and assert it dereferences
        # self._canvas_w / self._canvas_h for the draw center.
        start = src.index("def _render_pil_image")
        end = src.index("\n    def ", start + 10)
        body = src[start:end]
        self.assertRegex(
            body, r"self\._canvas_w\s*//\s*2",
            "_render_pil_image must use self._canvas_w//2 (live width) "
            "for the draw center, not the fixed _DISPLAY_W constant.",
        )
        self.assertRegex(
            body, r"self\._canvas_h\s*//\s*2",
            "_render_pil_image must use self._canvas_h//2 (live height) "
            "for the draw center, not the fixed _DISPLAY_H constant.",
        )
        # Negative-lock: the OLD pattern must be gone from this body.
        self.assertNotRegex(
            body, r"_DISPLAY_W\s*//\s*2",
            "_render_pil_image must NOT use _DISPLAY_W // 2 — that's "
            "the bug being fixed.",
        )

    def test_show_error_uses_live_canvas_center(self):
        src = _read_video_inspector_source()
        start = src.index("def _show_error")
        end = src.index("\n    def ", start + 10)
        body = src[start:end]
        # Both text items must be positioned relative to the LIVE
        # canvas center.
        self.assertRegex(
            body, r"self\._canvas_w\s*//\s*2",
            "_show_error must use live canvas width for centering.",
        )


class DecoderUsesAtomicCanvasDimsTests(unittest.TestCase):
    """Code-reviewer Important (PR #43, post-79802bc self-review):
    the decoder thread reads ``self._canvas_w`` + ``self._canvas_h``
    to compute the aspect-fit resize target. Two separate attribute
    reads can interleave with a Tk-thread write between them, so a
    resize that lands mid-iteration could leak (new_w, old_h) into
    one frame's resize math and produce a single mis-stretched
    frame on the transition tick.

    Fix: decoder reads from ``self._canvas_dims`` (a tuple) instead.
    The Tk thread assigns the tuple atomically in
    ``_on_canvas_resize``; the decoder reads it via a single dict
    lookup (atomic under the GIL). Lock the pattern so a future
    refactor doesn't silently unpack the tuple back into two
    separate ``self._canvas_*`` reads.
    """

    def test_decoder_reads_atomic_dims_tuple(self):
        src = _read_video_inspector_source()
        start = src.index("def _decoder_loop")
        end = src.index("\n    def ", start + 10)
        body = src[start:end]
        # Atomic-snapshot pattern: read self._canvas_dims into a local.
        self.assertIn("canvas_dims = self._canvas_dims", body)
        # Negative-lock against the prior buggy pattern (two separate
        # reads of self._canvas_w / self._canvas_h in the decoder).
        self.assertNotRegex(
            body,
            r"target_w\s*=\s*self\._canvas_w\b",
            "Decoder must NOT read self._canvas_w / self._canvas_h "
            "directly — race-prone. Read self._canvas_dims tuple.",
        )

    def test_resize_handler_writes_atomic_dims_tuple(self):
        """The Tk thread MUST write the tuple atomically in
        _on_canvas_resize so the decoder's single-tuple read sees a
        consistent (w, h) pair."""
        src = _read_video_inspector_source()
        start = src.index("def _on_canvas_resize")
        end = src.index("\n    def ", start + 10)
        body = src[start:end]
        self.assertRegex(
            body,
            r"self\._canvas_dims\s*=\s*\(event\.width,\s*event\.height\)",
            "Tk thread must write self._canvas_dims as a tuple so "
            "the decoder's single dict-lookup gets a consistent "
            "(w, h) snapshot.",
        )


class ResizeRecenterUsesPriorCanvasSizeTests(unittest.TestCase):
    """Codex P2 (3273246460) on bdead49: ``_on_canvas_resize`` used
    ``_DISPLAY_W/H`` as the "previous center" baseline for every
    Configure event. That only matches reality for the FIRST resize
    from the initial 480×270. Subsequent resizes computed offsets
    from the wrong origin, walking items across the canvas.

    Lock that the offset baseline is now the PRIOR canvas size
    (``old_w/old_h``), not the fixed constants.
    """

    def test_recenter_old_center_uses_prior_canvas_dims(self):
        src = _read_video_inspector_source()
        start = src.index("def _on_canvas_resize")
        end = src.index("\n    def ", start + 10)
        body = src[start:end]
        # The fix: old_cx = old_w // 2 (NOT _DISPLAY_W // 2).
        self.assertRegex(
            body, r"old_cx\s*=\s*old_w\s*//\s*2",
            "_on_canvas_resize must base old-center on PRIOR canvas "
            "dims (old_w//2), not the fixed _DISPLAY_W constant.",
        )
        self.assertRegex(
            body, r"old_cy\s*=\s*old_h\s*//\s*2",
        )
        # Negative-lock the old buggy pattern.
        self.assertNotRegex(
            body,
            r"old_cx\s*=\s*_DISPLAY_W\s*//\s*2",
            "_on_canvas_resize MUST NOT use _DISPLAY_W // 2 for the "
            "old-center — that was the multi-resize drift bug.",
        )


if __name__ == "__main__":
    unittest.main()
