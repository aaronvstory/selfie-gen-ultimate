"""Structural tests for live window-geometry persistence (PR #43).

User report: manually resizing the window during a session does NOT
stick between relaunches. Root cause: only ``_save_layout`` fired (via
``_on_close``); any exit path that skipped ``_on_close`` (crash, kill,
ALT-F4 on some platforms) lost the user's chosen geometry.

Fix: a debounced ``<Configure>`` binding on ``self.root`` saves the
layout to ``kling_config.json`` ~800ms after the user stops dragging.

These are SOURCE-REGEX tests (no live Tk root needed). They lock the
critical wiring so a future refactor can't silently regress it.
"""

from __future__ import annotations

from pathlib import Path

import unittest


def _read_main_window() -> str:
    return Path(
        Path(__file__).resolve().parent.parent / "kling_gui" / "main_window.py"
    ).read_text(encoding="utf-8")


class WindowGeometryPersistenceTests(unittest.TestCase):
    def test_configure_binding_exists(self):
        """Root MUST bind <Configure> to a handler. Without this, manual
        resizes are only saved at close-time → user choice lost on any
        non-clean exit."""
        src = _read_main_window()
        # We use add="+" so the binding doesn't clobber any future
        # additional Configure handler on root.
        self.assertRegex(
            src,
            r'self\.root\.bind\(\s*"<Configure>",\s*self\._on_root_configure',
            "Live-resize persistence requires a root <Configure> binding.",
        )

    def test_configure_handler_filters_to_root_only(self):
        """Configure propagates up from every descendant — the handler
        MUST ignore non-root widgets or every child resize triggers a
        spurious save (and we'd thrash the JSON file)."""
        src = _read_main_window()
        self.assertIn("def _on_root_configure", src)
        # The handler compares str(event.widget) == str(self.root) to
        # filter; a stricter check would assert that comparison literally.
        self.assertRegex(
            src,
            r"str\(event\.widget\)\s*!=\s*str\(self\.root\)",
            "Configure handler must filter to root-only events.",
        )

    def test_debounce_via_after_cancel(self):
        """800ms debounce: a fast resize-drag would otherwise call
        _save_layout 60+ times. Cancel-then-reschedule pattern is the
        only thing that prevents JSON-write storms."""
        src = _read_main_window()
        self.assertIn("_layout_save_after_id", src)
        self.assertIn("self.root.after_cancel", src)
        # The handler reschedules via self.root.after(800, ...).
        self.assertRegex(
            src,
            r"self\.root\.after\(\s*800,\s*self\._save_layout_debounced",
            "Debounce delay should be ~800ms — coalesces a drag without "
            "feeling laggy on a deliberate final resize.",
        )

    def test_debounced_save_writes_config(self):
        """The debounced handler MUST call _save_layout AND _save_config.
        _save_layout alone only mutates the in-memory dict; _save_config
        is what writes JSON to disk."""
        src = _read_main_window()
        self.assertIn("def _save_layout_debounced", src)
        # Locate the function body and check both calls land in it.
        # Splice on the next def to bound the slice.
        start = src.index("def _save_layout_debounced")
        end = src.index("\n    def ", start + 10)
        body = src[start:end]
        self.assertIn("self._save_layout()", body)
        self.assertIn("self._save_config()", body)

    def test_no_save_when_geometry_unchanged(self):
        """Some platforms (notably macOS Tk) fire Configure for focus
        changes that don't actually move/resize the window. The
        debounced save MUST early-return when geometry == last-saved,
        otherwise we'd write the JSON every time the user clicks the
        title bar."""
        src = _read_main_window()
        self.assertIn("_last_saved_geometry", src)

    def test_init_seeds_debounce_state(self):
        """__init__ MUST seed _layout_save_after_id and
        _last_saved_geometry — otherwise the first Configure event
        crashes on AttributeError."""
        src = _read_main_window()
        self.assertIn("self._layout_save_after_id: Optional[str] = None", src)
        self.assertIn('self._last_saved_geometry: str = ""', src)


if __name__ == "__main__":
    unittest.main()
