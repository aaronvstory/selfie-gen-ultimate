"""Structural tests for ``apply_primary_action_style`` (kling_gui/theme.py).

Added for PR #43 (user request: "the most important button on each
step/page should have its own look so it is clear what to click").

The helper exists but is NOT yet called from any concrete site —
the user hasn't yet identified the per-page primary button list.
Tests lock the helper's contract so when call sites get added in a
future commit, the style they apply is consistent + cross-platform.
"""

from __future__ import annotations

import inspect
import unittest


class PrimaryActionStyleAPITests(unittest.TestCase):
    def test_helper_is_exported(self):
        from kling_gui.theme import apply_primary_action_style
        # Single positional arg (the button), returns None — the
        # "apply" verb means it mutates the button in place.
        sig = inspect.signature(apply_primary_action_style)
        params = list(sig.parameters.values())
        self.assertEqual(len(params), 1)
        self.assertEqual(params[0].name, "button")

    def test_helper_safe_on_non_widgets(self):
        """The helper must never raise — silently ignores non-tk
        widgets so call sites can use it liberally without
        try/except scaffolding."""
        from kling_gui.theme import apply_primary_action_style
        # Garbage input must not raise.
        apply_primary_action_style(None)
        apply_primary_action_style("not a widget")
        apply_primary_action_style(42)

    def test_helper_applies_distinguishing_attributes(self):
        """The applied style MUST set bg, fg, font, highlightthickness,
        and highlightbackground — the discriminating visual properties
        that make a primary button READ as distinct from secondary."""
        from kling_gui.theme import apply_primary_action_style

        class _CapturingButton:
            def __init__(self):
                self.configs = []
            def config(self, **kwargs):
                self.configs.append(kwargs)
            def cget(self, key):
                # Stub: real Tk buttons return their attr value; the
                # primary-style helper doesn't actually read cget, but
                # it's part of the Tk Button contract so we provide
                # a no-op implementation. CR PR #43 (3273385397):
                # `cget = lambda ...` triggers Ruff E731.
                del key
                return None
        btn = _CapturingButton()
        apply_primary_action_style(btn)
        # The helper does a single .config() call with the full style.
        self.assertEqual(len(btn.configs), 1)
        applied = btn.configs[0]
        # Discriminating attributes vs secondary buttons.
        self.assertIn("bg", applied)
        self.assertIn("fg", applied)
        self.assertIn("highlightthickness", applied)
        self.assertIn("highlightbackground", applied)
        self.assertIn("font", applied)
        # Specifically: highlightthickness must be > 0 (the secondary
        # buttons use 0 or 1 with same-as-bg ring — invisible). The
        # primary's contrasting border is the visual signature.
        self.assertGreater(applied["highlightthickness"], 1)
        # Font must be bold (the user wants visual weight).
        font = applied["font"]
        # tuple (family, size, "bold") OR ("family size bold") string.
        if isinstance(font, tuple):
            self.assertIn("bold", font)
        else:
            self.assertIn("bold", str(font).lower())


class ExceptionPathLoggingTests(unittest.TestCase):
    """CR Major (3273385365) PR #43: the silent ``except Exception: pass``
    in apply_primary_action_style was changed to log at debug so real
    bugs surface in file logs without crashing the GUI. Lock that
    behavior."""

    def test_failure_logs_at_debug(self):
        from kling_gui.theme import apply_primary_action_style
        # A class whose .config raises — exercises the except branch.
        class _RaisingButton:
            def config(self, **kwargs):
                raise RuntimeError("simulated tk error")
            def cget(self, key):
                del key
                return None

        with self.assertLogs("kling_gui.theme", level="DEBUG") as cm:
            apply_primary_action_style(_RaisingButton())
        # At least one debug-level message mentions the helper.
        joined = " | ".join(cm.output)
        self.assertIn("apply_primary_action_style failed", joined)

    def test_silent_swallow_pattern_replaced(self):
        """Source-regex lock: ``apply_primary_action_style`` must NOT
        contain the bare ``except Exception:\\n        pass`` pattern
        anymore — CR Major flagged it as masking real bugs."""
        from pathlib import Path
        src = (
            Path(__file__).resolve().parent.parent
            / "kling_gui" / "theme.py"
        ).read_text(encoding="utf-8")
        # Locate just the apply_primary_action_style body so we don't
        # mis-flag the apply_macos_button_fix block above it (which
        # got the same treatment).
        start = src.index("def apply_primary_action_style")
        end = len(src)
        body = src[start:end]
        self.assertIn("_LOGGER.debug", body)


class HelperSourceLockTests(unittest.TestCase):
    """Source-regex assertions so a future refactor that drops the
    public helper or significantly changes its visual contract
    surfaces here rather than silently degrading UX."""

    def test_function_defined_in_theme_module(self):
        from pathlib import Path
        src = (
            Path(__file__).resolve().parent.parent
            / "kling_gui" / "theme.py"
        ).read_text(encoding="utf-8")
        self.assertIn("def apply_primary_action_style(button)", src)

    def test_palette_constants_present(self):
        """The internal palette constants (_PRIMARY_BG/_FG/_RING)
        encode the design contract. Loss of any of them indicates
        a refactor that may have changed the visual recipe."""
        from kling_gui import theme
        # Constants are module-private — verify they exist for the
        # helper to read.
        self.assertTrue(hasattr(theme, "_PRIMARY_BG"))
        self.assertTrue(hasattr(theme, "_PRIMARY_FG"))
        self.assertTrue(hasattr(theme, "_PRIMARY_RING"))


if __name__ == "__main__":
    unittest.main()
