"""Regression for the PR #41 UI-polish round (user requests):

1. Prompt editor font unified — positive box must use the SAME font
   size as the negative box (user prefers the larger negative font).
   apply_ui_config now defaults to 10 and applies the resolved font to
   BOTH widgets.
2. Step 2.5 selfie expand composite default = "none" (raw AI output).
3. Filter help text replaced by an inline ⓘ HoverTooltip (no more
   multi-line wrapping label eating vertical space).
4. Step 2.5 Expand re-targets the ACTIVE carousel image on live
   carousel navigation (not only on tab-switch).

Structural/source assertions where a live Tk root would be required —
consistent with the existing test_pr41_codex_p2_fixes.py approach.
"""

import sys
import unittest
from pathlib import Path

_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_ROOT))


class PromptFontUnifiedTests(unittest.TestCase):
    def test_apply_ui_config_defaults_font_to_10(self):
        src = (_ROOT / "kling_gui" / "config_panel.py").read_text(
            encoding="utf-8"
        )
        # Default must be 10 (matches the negative editor), not the old 9.
        self.assertRegex(
            src,
            r'config_panel\.get\(\s*"prompt_preview_font_size",\s*10\s*\)',
        )

    def test_apply_ui_config_sets_both_editors(self):
        src = (_ROOT / "kling_gui" / "config_panel.py").read_text(
            encoding="utf-8"
        )
        # The negative editor's font is locked to the positive one.
        self.assertIn("negative_prompt_preview.config(font=", src)
        self.assertRegex(src, r"_resolved_font\s*=\s*\(FONT_FAMILY")

    def test_ui_config_defaults_in_main_window_are_10(self):
        src = (_ROOT / "kling_gui" / "main_window.py").read_text(
            encoding="utf-8"
        )
        # Both the UI_CONFIG_DEFAULTS dict and the save path must be 10
        # so neither a fresh install nor a save re-pins it to 9.
        self.assertEqual(
            src.count('"prompt_preview_font_size": 10'), 2
        )
        self.assertNotIn('"prompt_preview_font_size": 9', src)


class CompositeNoneDefaultTests(unittest.TestCase):
    def test_automation_config_default_is_none(self):
        from automation.config import merge_automation_defaults

        merged = merge_automation_defaults({})
        self.assertEqual(
            merged["automation_selfie_expand_composite_mode"], "none"
        )
        # Front expand is independent and stays preserve_seamless.
        self.assertEqual(
            merged["automation_front_expand_composite_mode"],
            "preserve_seamless",
        )

    def test_template_default_is_none(self):
        import json

        d = json.loads(
            (_ROOT / "default_config_template.json").read_text(
                encoding="utf-8"
            )
        )
        self.assertEqual(
            d["automation_selfie_expand_composite_mode"], "none"
        )
        # Step 0 Face Crop composite stays preserve_seamless.
        self.assertEqual(
            d["outpaint_composite_mode"], "preserve_seamless"
        )

    def test_expand_tab_fallback_default_is_none(self):
        src = (_ROOT / "kling_gui" / "tabs" / "expand_tab.py").read_text(
            encoding="utf-8"
        )
        self.assertRegex(
            src,
            r'self\.config\.get\(\s*"outpaint_composite_mode",\s*"none"\s*\)',
        )
        # The out-of-range guard must also fall back to "none".
        self.assertRegex(
            src,
            r'composite_value not in self\._composite_mode_labels:\s*\n'
            r'\s*composite_value = "none"',
        )


class FilterTooltipTests(unittest.TestCase):
    def test_filter_help_is_a_hover_icon_not_a_wrapped_label(self):
        src = (_ROOT / "kling_gui" / "config_panel.py").read_text(
            encoding="utf-8"
        )
        # The old multi-line help label / rD2 frame must be gone.
        self.assertNotIn("rD2 = tk.Frame", src)
        # Replaced by an ⓘ HoverTooltip wired to filter_info_icon.
        self.assertIn("self.filter_info_icon", src)
        self.assertIn("HoverTooltip(\n            self.filter_info_icon", src)


class ExpandLiveActiveImageTests(unittest.TestCase):
    def test_session_change_refreshes_expand_when_tab_visible(self):
        src = (_ROOT / "kling_gui" / "main_window.py").read_text(
            encoding="utf-8"
        )
        # _on_image_session_changed must re-target Step 2.5 to the
        # active carousel image while the Expand tab (index 3) is shown.
        self.assertIn("def _on_image_session_changed", src)
        self.assertRegex(
            src,
            r"_on_image_session_changed[\s\S]{0,800}?"
            r"notebook\.select\(\)\)\s*==\s*3[\s\S]{0,200}?"
            r"refresh_from_active_carousel\(\)",
        )


if __name__ == "__main__":
    unittest.main()
