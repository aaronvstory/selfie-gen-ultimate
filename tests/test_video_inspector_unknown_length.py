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


if __name__ == "__main__":
    unittest.main()
