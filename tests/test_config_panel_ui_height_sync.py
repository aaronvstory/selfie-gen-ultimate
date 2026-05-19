"""Regression: apply_ui_config must keep the negative-prompt toggle's
"restore to full height" target in sync with the ui_config height.

Bug (code-reviewer finding, PR #41): apply_ui_config fires ~50ms after
launch and resizes the positive prompt box to the ui_config height
(default 6), but _positive_prompt_full_height stayed hardcoded at the
construction-time 12. The negative-prompt show/hide toggle restores the
box to _positive_prompt_full_height, so the FIRST model toggle snapped
the box from the configured 6 back up to 12 — a visible jump and the
PR #41 height fix was masked on fresh installs.

These tests instantiate ConfigPanel via __new__ (no Tk) and drive only
apply_ui_config with a fake .config()-able text widget — the exact
surface the bug lives on.
"""

import importlib
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))


class _FakeText:
    """Minimal stand-in for the tk.Text prompt_preview widget.

    Only `.config(**kw)` is exercised by apply_ui_config; record the
    last height applied so the test can assert what the live widget got.
    """

    def __init__(self, height):
        self._height = height

    def config(self, **kw):
        if "height" in kw:
            self._height = kw["height"]

    def cget(self, key):
        if key == "height":
            return self._height
        raise KeyError(key)


def _make_panel():
    module = importlib.import_module("kling_gui.config_panel")
    panel = module.ConfigPanel.__new__(module.ConfigPanel)
    # Mirror the construction-time invariants apply_ui_config relies on.
    panel.prompt_preview = _FakeText(12)
    panel._positive_prompt_full_height = 12
    panel._positive_prompt_split_height = 7
    panel._neg_visible = False
    return panel


class ApplyUiConfigHeightSyncTests(unittest.TestCase):
    def test_full_height_tracks_ui_config_height(self):
        """A ui_config height >= split height becomes the new full-height
        target so the neg toggle restores to it, not the stale 12."""
        panel = _make_panel()
        panel.apply_ui_config(
            {"config_panel": {"prompt_preview_height": 9}}
        )
        self.assertEqual(panel.prompt_preview.cget("height"), 9)
        self.assertEqual(panel._positive_prompt_full_height, 9)

    def test_default_height_no_longer_masked_by_stale_12(self):
        """With the default ui_config (height 6) the toggle target must
        match the live box (6), not jump to the construction-time 12."""
        panel = _make_panel()
        panel.apply_ui_config(
            {"config_panel": {"prompt_preview_height": 6}}
        )
        self.assertEqual(panel.prompt_preview.cget("height"), 6)
        self.assertEqual(panel._positive_prompt_full_height, 6)

    def test_clamped_tiny_height_stays_consistent_no_jump(self):
        """An extreme ui_config height is clamped to the floor (4) and
        the full-height target tracks that SAME clamped value. The point
        of the fix is jump-free consistency: whatever height the box is
        shown at with the negative half hidden is exactly what the
        toggle restores to — never a snap back to the stale 12."""
        panel = _make_panel()
        panel.apply_ui_config(
            {"config_panel": {"prompt_preview_height": 3}}
        )
        self.assertEqual(panel.prompt_preview.cget("height"), 4)
        # Restore target == live height -> toggling the negative half
        # off returns the box to exactly where it already was.
        self.assertEqual(panel._positive_prompt_full_height, 4)

    def test_empty_ui_config_is_a_noop(self):
        panel = _make_panel()
        panel.apply_ui_config({})
        self.assertEqual(panel.prompt_preview.cget("height"), 12)
        self.assertEqual(panel._positive_prompt_full_height, 12)


if __name__ == "__main__":
    unittest.main()
