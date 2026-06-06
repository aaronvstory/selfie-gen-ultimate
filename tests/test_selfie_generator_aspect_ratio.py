"""PR #82 HIGH-2 (Codex P2, QC subagent 2026-06-07): the v2.26
universal-aspect-ratio set added ``9:21`` (Kontext Max accepts it,
mirror of ``21:9``) but Nano Banana 2's schema doesn't list it. A
saved ultra-tall canvas (e.g. 900x2100) snapped to ``9:21`` and the
DEFAULT model (Nano Banana 2) 422'd at submit time.

These tests pin the conservative fix: the universal set must contain
ONLY labels every current built-in accepts. Right now that's the
8-element intersection (``21:9`` / ``16:9`` / ``3:2`` / ``4:3`` /
``1:1`` / ``3:4`` / ``2:3`` / ``9:16``). Ultra-tall sizes snap to
``9:16`` instead — a small fidelity loss, but no 422 on the default
model.
"""

from __future__ import annotations

import unittest

from selfie_generator import SelfieGenerator


class UniversalAspectRatioSetTests(unittest.TestCase):
    def test_set_excludes_9_21_kontext_max_only(self):
        """``9:21`` was a Kontext Max addition that Nano Banana 2
        rejects with a 422. It MUST stay out of the universal set."""
        self.assertNotIn("9:21", SelfieGenerator._UNIVERSAL_ASPECT_RATIOS)

    def test_set_excludes_4_5_and_5_4_kontext_max_rejects(self):
        """``4:5`` and ``5:4`` were dropped in v2.26 because Kontext
        Max rejected them. Must stay out so they can't reappear via
        a refactor."""
        self.assertNotIn("4:5", SelfieGenerator._UNIVERSAL_ASPECT_RATIOS)
        self.assertNotIn("5:4", SelfieGenerator._UNIVERSAL_ASPECT_RATIOS)

    def test_set_includes_full_kontext_max_minus_9_21(self):
        """The 8 labels Kontext Max's schema lists MINUS ``9:21``
        must all be present. Kontext Max's full error message:
        "Input should be '21:9', '16:9', '4:3', '3:2', '1:1', '2:3',
        '3:4', '9:16' or '9:21'"."""
        expected = {"21:9", "16:9", "4:3", "3:2", "1:1", "2:3", "3:4", "9:16"}
        self.assertEqual(set(SelfieGenerator._UNIVERSAL_ASPECT_RATIOS.keys()), expected)


class ClosestAspectRatioBehaviourTests(unittest.TestCase):
    def test_ultra_tall_900x2100_snaps_to_9_16_not_9_21(self):
        """The original Codex P2 scenario: a user with a saved
        900x2100 ultra-tall canvas would previously snap to ``9:21``
        and 422 on Nano Banana 2. After the fix, this snaps to
        ``9:16`` — closer than ``2:3`` (0.667) or ``3:4`` (0.75) to
        the target 9/21 ≈ 0.428.

        9:16 = 0.5625 — distance 0.135
        2:3  = 0.667  — distance 0.239
        3:4  = 0.75   — distance 0.322
        """
        self.assertEqual(SelfieGenerator._closest_aspect_ratio(900, 2100), "9:16")

    def test_square_snaps_to_1_1(self):
        self.assertEqual(SelfieGenerator._closest_aspect_ratio(1024, 1024), "1:1")

    def test_portrait_768x1344_snaps_to_9_16(self):
        """The default selfie canvas — 768x1344 → ratio 0.571."""
        self.assertEqual(SelfieGenerator._closest_aspect_ratio(768, 1344), "9:16")

    def test_landscape_1920x1080_snaps_to_16_9(self):
        self.assertEqual(SelfieGenerator._closest_aspect_ratio(1920, 1080), "16:9")

    def test_ultrawide_snaps_to_21_9(self):
        self.assertEqual(SelfieGenerator._closest_aspect_ratio(2100, 900), "21:9")


if __name__ == "__main__":
    unittest.main()
