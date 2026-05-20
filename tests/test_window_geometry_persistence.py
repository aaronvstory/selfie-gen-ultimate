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

    def test_no_save_when_geometry_unchanged_but_only_for_geometry_reason(self):
        """Some platforms (notably macOS Tk) fire Configure for focus
        changes that don't actually move/resize the window. The
        debounced save MUST early-return when geometry == last-saved
        AND the reason is "geometry". For reason="sash" the guard MUST
        NOT apply because sash drags often happen without the root
        window resizing."""
        src = _read_main_window()
        self.assertIn("_last_saved_geometry", src)
        # The geometry-guard branch is reason-scoped.
        self.assertRegex(
            src,
            r'reason\s*==\s*"geometry"\s+and\s+current\s*==\s*self\._last_saved_geometry',
            "Geometry-unchanged guard must apply only for "
            "reason=geometry — sash drags must always save.",
        )

    def test_init_seeds_debounce_state(self):
        """__init__ MUST seed _layout_save_after_id and
        _last_saved_geometry AND _layout_save_reason — otherwise the
        first Configure or sash-release event crashes on AttributeError."""
        src = _read_main_window()
        self.assertIn("self._layout_save_after_id: Optional[str] = None", src)
        self.assertIn('self._last_saved_geometry: str = ""', src)
        self.assertIn('self._layout_save_reason: str = "geometry"', src)


class SashDragPersistenceTests(unittest.TestCase):
    """Sash-drag live persistence (GPT-5.5 gap finding on PR #43).

    The original root-Configure debounce only persisted whole-window
    resizes. Drop-zone / queue / prompt-split / log / log-drop-split
    sash drags happen WITHOUT root geometry changing, so they need
    a separate event source. <ButtonRelease-1> on each PanedWindow
    is the canonical Tk hook — emitted when the user releases the
    sash drag handle on both Windows and macOS Aqua.
    """

    def test_all_five_panes_bound(self):
        """ALL FIVE PanedWindows in the app MUST have a
        <ButtonRelease-1> binding. Missing any one means the user's
        sash drag on THAT pane silently fails to persist."""
        src = _read_main_window()
        # The single loop in __init__ iterates the 5 attr names.
        for pane_attr in (
            "main_paned",
            "top_h_paned",
            "bottom_paned",
            "right_paned",
            "log_drop_paned",
        ):
            # Each name appears in the binding loop (string literal in
            # the tuple).
            self.assertRegex(
                src,
                rf'"{pane_attr}"',
                f"Sash-drag binding missing for {pane_attr}",
            )

    def test_button_release_binding_pattern(self):
        """The binding must use <ButtonRelease-1> (NOT <B1-Motion>
        — that would fire mid-drag and thrash the debounce). Must
        use add='+' so it coexists with any future sash handlers."""
        src = _read_main_window()
        self.assertRegex(
            src,
            r'_pane\.bind\(\s*\n?\s*"<ButtonRelease-1>",\s*self\._on_sash_release,\s*add="\+"',
            "Sash bindings must use <ButtonRelease-1> + add='+'.",
        )

    def test_sash_release_handler_routes_into_debounce(self):
        """_on_sash_release MUST call _schedule_layout_save with
        reason='sash' so the geometry-unchanged guard is skipped."""
        src = _read_main_window()
        self.assertIn("def _on_sash_release", src)
        self.assertIn("def _schedule_layout_save", src)
        # The sash handler routes through the shared scheduler.
        start = src.index("def _on_sash_release")
        end = src.index("\n    def ", start + 10)
        body = src[start:end]
        self.assertIn('self._schedule_layout_save(reason="sash")', body)

    def test_schedule_layout_save_tracks_reason(self):
        """_schedule_layout_save MUST set self._layout_save_reason so
        the debounced callback knows whether the geometry guard applies.
        Without this the sash-reason save would fall back to the
        default ('geometry') and could be silently dropped."""
        src = _read_main_window()
        start = src.index("def _schedule_layout_save")
        end = src.index("\n    def ", start + 10)
        body = src[start:end]
        self.assertIn("self._layout_save_reason = reason", body)
        # And the after-cancel + reschedule pattern lives here, not in
        # _on_root_configure (so both reasons share the debounce).
        self.assertIn("self.root.after_cancel", body)
        self.assertIn("self._save_layout_debounced", body)

    def test_macos_cross_platform_documented(self):
        """The implementation explicitly notes that <ButtonRelease-1>
        is cross-platform — this comment is the only signal future
        maintainers have that the macOS case was considered. Lock the
        comment in place so a 'cleanup' pass doesn't strip it."""
        src = _read_main_window()
        # The comment block above the sash-binding loop documents the
        # platform reasoning. Look for the canonical mention.
        self.assertRegex(
            src,
            r"cross-platform.*Tk.*both Windows and macOS",
            "Sash-binding implementation must document macOS support "
            "explicitly — Tk PanedWindow behavior varies by platform.",
        )


if __name__ == "__main__":
    unittest.main()
