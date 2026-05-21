"""Tests for the rPPG progress tracker + adaptive deadline.

PR #43 / user feedback ("show progress like we do for oldcam" + friend's
"hope you're not still going to use some arbitrary timeout"):
``_RppgProgressTracker`` parses iterative-mode markers from the
injector's stdout, emits user-friendly progress, and provides a
deadline-extender callback so the wall clock ratchets forward as long
as the injector keeps making progress.
"""

from __future__ import annotations

import unittest
from unittest import mock

from automation.rppg import (
    _RPPG_DONE_RE,
    _RPPG_GPU_RE,
    _RPPG_ITER_RE,
    _RppgProgressTracker,
)


class RegexAnchorsTests(unittest.TestCase):
    """Lock the regex anchors so a future refactor can't silently break
    parser behavior. These match against the injector's actual stdout
    format verified in rPPG/rppg_injector.py."""

    def test_iter_re_matches_canonical_form(self):
        # Injector emits "  Iteration N/M" with 2 leading spaces.
        # We .strip() before matching so leading whitespace doesn't
        # break the parse.
        for s in ["Iteration 1/10", "Iteration 5/5", "Iteration 12/20 starting"]:
            self.assertIsNotNone(
                _RPPG_ITER_RE.match(s), f"should match: {s!r}",
            )

    def test_iter_re_rejects_lowercase(self):
        # Case-sensitive on purpose — "iteration" lowercase is too
        # common in unrelated chatter to safely match.
        self.assertIsNone(_RPPG_ITER_RE.match("iteration 1/10"))
        self.assertIsNone(_RPPG_ITER_RE.match("itera 1/10"))

    def test_done_re_matches_all_convergence_forms(self):
        for s in [
            "All targets met at iteration 4!",
            "Stopping to avoid over-processing",
            "Best iteration: 3",
            "Plateau stop",
            "Converged",
        ]:
            self.assertIsNotNone(
                _RPPG_DONE_RE.match(s), f"should match: {s!r}",
            )

    def test_gpu_re_captures_backend_string(self):
        # Both detected and unavailable forms surface to the user.
        m = _RPPG_GPU_RE.match("GPU backend: CuPy 12.0 on 1 device(s)")
        self.assertIsNotNone(m)
        assert m is not None
        self.assertEqual(m.group(1), "CuPy 12.0 on 1 device(s)")
        m = _RPPG_GPU_RE.match("GPU backend: CuPy unavailable (ImportError)")
        self.assertIsNotNone(m)


class ProgressTrackerEmissionTests(unittest.TestCase):
    """Verify the (message, level) calls that surface to the GUI/CLI
    report-cb. The "user-friendly" mode (verbose=False) is the
    default: only the synthesized progress lines fire at "info", raw
    injector chatter goes to "debug"."""

    def _tracker(self, *, verbose: bool):
        calls = []
        t = _RppgProgressTracker(
            report_cb=lambda msg, lvl: calls.append((lvl, msg)),
            verbose=verbose,
        )
        return t, calls

    def test_iter_line_emits_friendly_progress(self):
        t, calls = self._tracker(verbose=False)
        t.on_line("  Iteration 3/10")
        # User-friendly synthesized line; correct % rounding.
        self.assertIn(
            ("info", "rPPG iteration 3/10 (~30%)"),
            calls,
        )

    def test_iter_line_updates_internal_state(self):
        t, _ = self._tracker(verbose=False)
        t.on_line("  Iteration 5/10")
        self.assertEqual(t._iter_current, 5)
        self.assertEqual(t._iter_max, 10)

    def test_gpu_line_always_info_even_in_quiet_mode(self):
        """User wants to see whether their RTX 4090 was actually used.
        Even with verbose=False, the GPU detection line surfaces."""
        t, calls = self._tracker(verbose=False)
        t.on_line("GPU backend: CuPy 12.0 on 1 device(s)")
        self.assertIn(
            ("info", "rPPG backend — CuPy 12.0 on 1 device(s)"),
            calls,
        )

    def test_convergence_line_emits_with_iter_context(self):
        t, calls = self._tracker(verbose=False)
        t.on_line("  Iteration 3/10")
        calls.clear()
        t.on_line("All targets met at iteration 4!")
        # Should reference the last-seen iter (3) and include the
        # injector's line as a label.
        msg = calls[-1][1]
        self.assertIn("iteration 3", msg)
        self.assertIn("All targets met", msg)

    def test_non_marker_line_goes_to_debug_when_quiet(self):
        t, calls = self._tracker(verbose=False)
        t.on_line("  Extracting facial ROIs from frames...")
        self.assertEqual(
            calls[-1], ("debug", "  Extracting facial ROIs from frames..."),
        )

    def test_non_marker_line_goes_to_info_when_verbose(self):
        t, calls = self._tracker(verbose=True)
        t.on_line("  Extracting facial ROIs from frames...")
        self.assertEqual(
            calls[-1], ("info", "  Extracting facial ROIs from frames..."),
        )

    def test_verbose_mode_also_emits_raw_iter_line(self):
        """Verbose users see BOTH the friendly progress AND the raw
        injector line. Non-verbose only sees friendly."""
        t, calls = self._tracker(verbose=True)
        t.on_line("  Iteration 3/10")
        msgs = [m for (_lvl, m) in calls]
        # Friendly version present...
        self.assertIn("rPPG iteration 3/10 (~30%)", msgs)
        # ...plus the raw line as another info line.
        self.assertIn("  Iteration 3/10", msgs)


class DeadlineExtenderTests(unittest.TestCase):
    """Verify the adaptive-timeout behavior: every new iteration
    extends the deadline by ~90s; non-iter lines return 0; same-iter
    repeats return 0 (so the injector emitting the same line twice
    doesn't double-extend)."""

    def test_new_iter_extends_by_90s(self):
        t = _RppgProgressTracker()
        self.assertEqual(t.deadline_extender("  Iteration 1/10"), 90)

    def test_same_iter_no_extension(self):
        t = _RppgProgressTracker()
        t.deadline_extender("  Iteration 3/10")
        # Same iter — injector might emit the marker more than once.
        # No additional bump.
        self.assertEqual(t.deadline_extender("  Iteration 3/10"), 0)

    def test_lower_iter_no_extension(self):
        """Defense against weird ordering (e.g. injector restart) —
        an iter LOWER than what we've already seen doesn't extend."""
        t = _RppgProgressTracker()
        t.deadline_extender("  Iteration 5/10")
        self.assertEqual(t.deadline_extender("  Iteration 3/10"), 0)

    def test_higher_iter_extends(self):
        t = _RppgProgressTracker()
        t.deadline_extender("  Iteration 3/10")
        self.assertEqual(t.deadline_extender("  Iteration 4/10"), 90)

    def test_non_iter_lines_return_zero(self):
        t = _RppgProgressTracker()
        for s in [
            "  Extracting facial ROIs...",
            "  Test Result: FAIL",
            "GPU backend: CuPy unavailable",
            "Running ffmpeg encode...",
        ]:
            self.assertEqual(t.deadline_extender(s), 0, f"line: {s!r}")


class StreamerCallOrderTests(unittest.TestCase):
    """Regression for the call-order bug both CodeRabbit and Codex
    flagged on 91af11f: ``deadline_extender`` MUST run BEFORE
    ``on_line`` in the streamer. Otherwise on_line updates
    ``_iter_current`` and the extender then sees "already seen iter"
    and returns 0, defeating the entire "no arbitrary timeout"
    contract.
    """

    def test_streamer_calls_extender_before_on_line(self):
        """Source-regex lock: the streamer must call deadline_extender
        BEFORE on_line for every line. A future refactor that reorders
        them silently breaks adaptive timeout."""
        from pathlib import Path
        src = (Path(__file__).resolve().parent.parent /
               "automation" / "rppg.py").read_text(encoding="utf-8")
        # Locate stream_subprocess_with_timeout body.
        start = src.index("def stream_subprocess_with_timeout")
        end = src.index("\ndef ", start + 10)
        body = src[start:end]
        # Find the line positions of the calls.
        ext_pos = body.index("deadline_extender(text)")
        on_line_pos = body.index("on_line(text)")
        self.assertLess(
            ext_pos, on_line_pos,
            "deadline_extender MUST run BEFORE on_line — otherwise the "
            "tracker's on_line updates _iter_current first and the "
            "extender then returns 0 for the same iter marker, "
            "defeating the adaptive-timeout contract.",
        )

    def test_streamer_accumulates_per_iter_budget(self):
        """The streamer must ACCUMULATE per-iter budget (deadline +=
        extra) rather than rebasing via max(deadline, now + extra).
        With the max() form, a single 90s bump capped at the existing
        budget never grows the deadline beyond the initial timeout —
        the comment promised accumulation but the code didn't deliver."""
        from pathlib import Path
        src = (Path(__file__).resolve().parent.parent /
               "automation" / "rppg.py").read_text(encoding="utf-8")
        start = src.index("def stream_subprocess_with_timeout")
        end = src.index("\ndef ", start + 10)
        body = src[start:end]
        # Must be ABSOLUTE accumulation (``deadline + extra``), not
        # the broken ``max(deadline, now + extra)`` form. The
        # accumulation may be capped by min(...) to bound runaway
        # subprocesses (Gemini MEDIUM on 9d9a473), but the additive
        # form remains.
        self.assertIn("deadline + extra", body)
        self.assertNotIn("deadline = max(deadline, time.monotonic() + extra", body)
        # Verify the cumulative-extension cap is in place: a stuck
        # subprocess that emits the same iteration marker forever
        # must NOT push the deadline indefinitely.
        self.assertIn("max_deadline", body)
        # Updated 2026-05-21 per Gemini MEDIUM on 0f5c5f3: the
        # accumulation now floors at ``now + extra`` so a marker
        # arriving when the deadline has already drifted close to
        # ``now`` still buys real headroom. The shape is:
        #   min(max(deadline + extra, now + extra), max_deadline)
        self.assertIn("max_deadline", body)
        self.assertRegex(
            body,
            r"min\(\s*max\(deadline \+ extra, time\.monotonic\(\) \+ extra\),\s*max_deadline",
        )


if __name__ == "__main__":
    unittest.main()
