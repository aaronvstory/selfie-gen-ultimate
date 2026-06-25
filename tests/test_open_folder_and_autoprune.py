"""Top-bar 'Open Folder' button + auto-prune-default-ON (v2.44).

Source-level guards (a live Tk root isn't available in CI), consistent with the
existing test_pr41_*.py approach.
"""

import sys
import unittest
from pathlib import Path

_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_ROOT))


class AutoPruneDefaultTests(unittest.TestCase):
    """Auto-prune must default ON (2026-06-25 mandate): BOTH the checkbox
    display default and the prune-logic pre-check read True when the config key
    is absent, so a config that never set it still prunes."""

    def _read(self, rel):
        return (_ROOT / rel).read_text(encoding="utf-8")

    def test_both_read_sites_default_true_root(self):
        src = self._read("kling_gui/main_window.py")
        # No read site may default this key to False (would silently keep prune off).
        self.assertNotIn('get("session_autoprune_enabled", False)', src)
        # Exactly the checkbox-var + the prune pre-check default to True.
        self.assertGreaterEqual(
            src.count('get("session_autoprune_enabled", True)'), 2,
            "both the checkbox default and the prune pre-check must default True",
        )

    def test_both_read_sites_default_true_dist(self):
        src = self._read("distribution/kling_gui/main_window.py")
        self.assertNotIn('get("session_autoprune_enabled", False)', src)
        self.assertGreaterEqual(src.count('get("session_autoprune_enabled", True)'), 2)

    def test_session_manager_inner_gate_defaults_true(self):
        # code-reviewer PR #115: a SECOND gate in session_manager.maybe_autoprune
        # _on_launch must also default True, else the prune silently no-ops for
        # existing users (whose config lacks the key) even though the UI shows ON.
        for rel in ("kling_gui/session_manager.py", "distribution/kling_gui/session_manager.py"):
            src = self._read(rel)
            self.assertNotIn('get("session_autoprune_enabled", False)', src, rel)

    def test_autoprune_actually_runs_when_key_absent(self):
        # BEHAVIORAL guard (not just source text): with NO key in config, the
        # function must set ran=True (i.e. not early-return). This is the exact
        # bug the source-text tests alone would miss.
        import tempfile
        from kling_gui.session_manager import maybe_autoprune_on_launch
        with tempfile.TemporaryDirectory() as d:
            # Empty config (no session_autoprune_enabled key) -> must RUN.
            result = maybe_autoprune_on_launch(d, {})
            self.assertTrue(
                result.get("ran"),
                "auto-prune must run by default when the config key is absent",
            )
            # Explicit False still disables it.
            result_off = maybe_autoprune_on_launch(d, {"session_autoprune_enabled": False})
            self.assertFalse(result_off.get("ran"))


class OpenFolderButtonTests(unittest.TestCase):
    """The top-bar 'Open Folder' button + its handler must exist and be wired to
    the folder-scan session-load flow (root + dist)."""

    def _read(self, rel):
        return (_ROOT / rel).read_text(encoding="utf-8")

    def test_button_and_handler_present_root(self):
        src = self._read("kling_gui/main_window.py")
        # The handler method exists...
        self.assertIn("def _on_open_folder_as_session", src)
        # ...is wired to a header button command...
        self.assertIn("header_open_folder", src)
        self.assertIn("Open Folder", src)
        # ...and reuses the real folder-scan builder + the normal session-load path.
        self.assertRegex(
            src,
            r"_on_open_folder_as_session[\s\S]{0,800}?build_session_from_folder",
        )
        self.assertRegex(
            src,
            r"_on_open_folder_as_session[\s\S]{0,2400}?_on_session_loaded\(",
        )

    def test_button_and_handler_present_dist(self):
        src = self._read("distribution/kling_gui/main_window.py")
        self.assertIn("def _on_open_folder_as_session", src)
        self.assertIn("header_open_folder", src)


if __name__ == "__main__":
    unittest.main()
