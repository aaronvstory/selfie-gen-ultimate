import unittest

from selfie_generator import SelfieGenerator


class SelfieGeneratorTimeoutTests(unittest.TestCase):
    # v2.26 bounds (user mandate 2026-06-07): "300s is too much for one
    # selfie... should be 120 seconds max". DEFAULT dropped 300 → 120
    # (the user's primary ask), MAX kept higher (180) so slow models
    # like GPT Image 2 Edit (observed at 137s on a successful run in
    # the user's PR #82 log) still complete — subagent CRITICAL on
    # PR #82 round 1.
    def test_default_timeout_when_missing_or_invalid(self):
        self.assertEqual(SelfieGenerator.sanitize_poll_timeout_seconds(None), 120)
        self.assertEqual(SelfieGenerator.sanitize_poll_timeout_seconds("bad"), 120)

    def test_timeout_bounds_are_clamped(self):
        # Below MIN clamps UP to 60.
        self.assertEqual(SelfieGenerator.sanitize_poll_timeout_seconds(5), 60)
        # Above MAX clamps DOWN to 180 (was 1800 pre-v2.26).
        self.assertEqual(SelfieGenerator.sanitize_poll_timeout_seconds(7200), 180)
        # The exact OLD default (300s) now clamps to the new MAX (180s).
        self.assertEqual(SelfieGenerator.sanitize_poll_timeout_seconds(300), 180)
        # 137s — the actual GPT Image 2 Edit observed completion time —
        # MUST still be allowed (the v2.26 round-1 bug was this getting
        # clamped to 120 and killing the working path).
        self.assertEqual(SelfieGenerator.sanitize_poll_timeout_seconds(137), 137)

    def test_timeout_within_bounds_is_kept(self):
        # 90 is between MIN (60) and MAX (180), passes through.
        self.assertEqual(SelfieGenerator.sanitize_poll_timeout_seconds(90), 90)
        # 120 (the new DEFAULT) is mid-range — passes through.
        self.assertEqual(SelfieGenerator.sanitize_poll_timeout_seconds(120), 120)
        # Exactly MAX is allowed.
        self.assertEqual(SelfieGenerator.sanitize_poll_timeout_seconds(180), 180)


if __name__ == "__main__":
    unittest.main()
