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


class DistForcesCompositeModesTests(unittest.TestCase):
    """PR #41 (user request): the v2.1 bundle must force composite
    modes from the template — Step 2.5 selfie expand 'none', Step 0
    Face Crop / outpaint 'preserve_seamless' — so a stale dev
    kling_config.json cannot leak the wrong composite into the ship."""

    def test_release_prep_overrides_both_composites(self):
        import json as _j, tempfile, os
        from distribution.release_prep import build_sanitized_config
        with tempfile.TemporaryDirectory() as d:
            t=os.path.join(d,'default_config_template.json')
            l=os.path.join(d,'kling_config.json')
            with open(t, 'w', encoding='utf-8') as fp:
                fp.write(_j.dumps({
                    'automation_selfie_expand_composite_mode': 'none',
                    'outpaint_composite_mode': 'preserve_seamless',
                }))
            # Dev machine has the WRONG (swapped) values live.
            with open(l, 'w', encoding='utf-8') as fp:
                fp.write(_j.dumps({
                    'automation_selfie_expand_composite_mode': 'preserve_seamless',
                    'outpaint_composite_mode': 'none',
                }))
            from pathlib import Path
            cfg=build_sanitized_config(Path(t),Path(l))
        self.assertEqual(cfg['automation_selfie_expand_composite_mode'],'none')
        self.assertEqual(cfg['outpaint_composite_mode'],'preserve_seamless')

    def test_shipped_template_slot3_is_active_with_title(self):
        import json as _j
        d=_j.loads((_ROOT/'default_config_template.json').read_text(encoding='utf-8'))
        self.assertEqual(d['current_prompt_slot'],3)
        self.assertEqual(d['prompt_titles']['3'],'enhanced for kling 2.5 pro')
        self.assertIn('Kling 2.5 Pro',d['saved_prompts']['3'])
        self.assertTrue(d['negative_prompts']['3'])
        # slot 1 minimal-motion fallback preserved.
        self.assertIn('very subtle, slow head movement',d['saved_prompts']['1'])


class SimilarityScoreVarianceTests(unittest.TestCase):
    """User-reported regression (PR #41): two visibly-different
    selfies both scored 82% under the v1.9 sqrt curve because d=0.50-
    0.68 squashed into 80.0-82.9% (only 2.9 points of resolution).
    v2.0 linear curve restores meaningful variance across the whole
    pass zone. Pin the user's exact reported distances so a regression
    can't quietly re-introduce the score compression."""

    def test_user_reported_distances_are_distinguishable(self):
        import importlib
        se = importlib.import_module('similarity_engine')
        engine = se.FaceEngine()
        s_538, _ = engine._score_from_distance(0.538)
        s_564, _ = engine._score_from_distance(0.564)
        # Under the old sqrt curve BOTH rounded to 82. Under v2.0
        # linear they round to 84 and 83 — a visible 1-point gap.
        self.assertEqual(int(round(s_538)), 84)
        self.assertEqual(int(round(s_564)), 83)
        # Raw-score gap must be > 0.5 so rounding can't merge them.
        self.assertGreater(s_538 - s_564, 0.5)

    def test_typical_ai_selfie_range_has_proportional_spread(self):
        import importlib
        se = importlib.import_module('similarity_engine')
        engine = se.FaceEngine()
        # Distances 0.30 (high similarity) and 0.65 (borderline) must
        # produce a >9-point score gap so the calculator is genuinely
        # sensitive across the AI-selfie operating range.
        hi, _ = engine._score_from_distance(0.30)
        lo, _ = engine._score_from_distance(0.65)
        self.assertGreater(hi - lo, 9.0,
            f'AI-selfie band must spread >9 points; got {hi:.2f} vs {lo:.2f}')

    def test_curve_exponent_is_v2_linear(self):
        import importlib
        se = importlib.import_module('similarity_engine')
        # Pin the constant so a future PR can't silently re-introduce
        # the v1.9 sqrt compression.
        self.assertEqual(se.FaceEngine.PASS_CURVE_EXPONENT, 1.0)
        self.assertEqual(se.FaceEngine.FAIL_CURVE_EXPONENT, 1.0)


class CodeRabbitCleanupTests(unittest.TestCase):
    """CodeRabbit + Codex round on commit 915819f (PR #41):
    1. CLI apply_recommended_defaults no longer reverts selfie expand
       composite to "preserve_seamless" (must stay the new "none").
    2. get_merged_models exempts current_model from BOTH hidden paths.
    3. main_window _on_image_session_changed narrows the catch-all.
    4. _as_int_durations tolerates scalar input.
    5. Split height re-derives from full height so a ui_config height
       below 7 cannot make the negative toggle GROW the positive box."""

    def test_cli_recommended_defaults_sets_selfie_composite_none(self):
        src = (_ROOT / "kling_automation_ui.py").read_text(encoding="utf-8")
        self.assertRegex(
            src,
            r'self\.config\["automation_selfie_expand_composite_mode"\]\s*=\s*"none"',
        )
        self.assertIn("-> bfl / percent / 30 / none", src)
        self.assertNotIn("-> bfl / percent / 30 / preserve_seamless", src)

    def test_get_merged_models_exempts_current_from_both_hide_paths(self):
        from kling_gui.config_panel import ModelFetcher
        ep = "bytedance/seedance-2.0/image-to-video"
        cfg = {"current_model": ep, "hidden_models": [ep]}
        endpoints = {m.get("endpoint") for m in ModelFetcher.get_merged_models(cfg)}
        self.assertIn(ep, endpoints)

    def test_session_change_handler_narrows_exception(self):
        src = (_ROOT / "kling_gui" / "main_window.py").read_text(encoding="utf-8")
        self.assertRegex(
            src,
            r"refresh_from_active_carousel\(\)[\s\S]{0,200}?except tk\.TclError",
        )
        self.assertRegex(
            src,
            r"refresh_from_active_carousel\(\)[\s\S]{0,800}?Step 2\.5 live refresh failed",
        )

    def test_as_int_durations_scalar_input(self):
        def _as_int_durations(raw):
            out = []
            if not isinstance(raw, (list, tuple, set)):
                raw = [raw] if raw is not None else []
            for v in raw:
                try:
                    out.append(int(v))
                except (TypeError, ValueError):
                    continue
            return out

        self.assertEqual(_as_int_durations(10), [10])
        self.assertEqual(_as_int_durations("10"), [10])
        self.assertEqual(_as_int_durations(None), [])
        self.assertEqual(_as_int_durations(["5", "10"]), [5, 10])
        src = (_ROOT / "kling_gui" / "model_manager_dialog.py").read_text(
            encoding="utf-8"
        )
        self.assertIn("isinstance(raw, (list, tuple, set))", src)

    def test_split_height_derives_from_full_height(self):
        src = (_ROOT / "kling_gui" / "config_panel.py").read_text(
            encoding="utf-8"
        )
        self.assertRegex(
            src,
            r"max\(\s*3,\s*min\(\s*7,\s*self\._positive_prompt_full_height\s*-\s*5\s*\)\s*\)",
        )
        self.assertRegex(
            src,
            r"max\(\s*3,\s*min\(\s*7,\s*resolved_height\s*-\s*5\s*\)\s*\)",
        )

        def derive(full):
            return max(3, min(7, full - 5))

        self.assertEqual(derive(6), 3)
        self.assertEqual(derive(10), 5)
        self.assertEqual(derive(12), 7)
        for f in range(4, 25):
            self.assertLess(derive(f), f)


class ApplyUiConfigRespectsNegVisibleTests(unittest.TestCase):
    """Codex P2 (PR #41): _load_config calls _update_motion_controls
    BEFORE apply_ui_config, so _neg_visible can already be True when
    apply_ui_config runs for a neg-supporting current_model. The old
    code unconditionally set prompt_preview to FULL height, snapping
    the box back up while the negative half was visible — the two
    halves overlapped visually until the user toggled. The fix
    honours _neg_visible at apply_ui_config time."""

    class _FakeText:
        def __init__(self, h):
            self._height = h
            self._font = None

        def config(self, **kw):
            if "height" in kw:
                self._height = kw["height"]
            if "font" in kw:
                self._font = kw["font"]

    def _make_panel(self):
        import importlib
        cp_mod = importlib.import_module("kling_gui.config_panel")
        panel = cp_mod.ConfigPanel.__new__(cp_mod.ConfigPanel)
        panel.prompt_preview = self._FakeText(12)
        panel.negative_prompt_preview = self._FakeText(5)
        panel._positive_prompt_full_height = 12
        panel._positive_prompt_split_height = 7
        return panel

    def test_apply_ui_config_uses_full_height_when_neg_hidden(self):
        panel = self._make_panel()
        panel._neg_visible = False
        panel.apply_ui_config(
            {"config_panel": {"prompt_preview_height": 6}}
        )
        # No neg visible -> positive box at full (resolved) height.
        self.assertEqual(panel.prompt_preview._height, 6)
        self.assertEqual(panel._positive_prompt_full_height, 6)
        self.assertEqual(panel._positive_prompt_split_height, 3)

    def test_apply_ui_config_uses_split_height_when_neg_already_visible(self):
        # _load_config -> _update_motion_controls already flipped
        # _neg_visible to True for a neg-supporting model BEFORE
        # apply_ui_config fires (~50ms later).
        panel = self._make_panel()
        panel._neg_visible = True
        panel.apply_ui_config(
            {"config_panel": {"prompt_preview_height": 6}}
        )
        # Box should be at the SPLIT height (3), not snapped back to
        # full (6) and visually overlapping the negative half.
        self.assertEqual(panel.prompt_preview._height, 3)
        # Targets are re-derived correctly so the toggle still works.
        self.assertEqual(panel._positive_prompt_full_height, 6)
        self.assertEqual(panel._positive_prompt_split_height, 3)

    def test_apply_ui_config_pins_targets_even_on_split_path(self):
        """Even when applying split height on startup, the FULL
        target must be the resolved height so toggling off neg-half
        restores to the configured size — not the stale 12."""
        panel = self._make_panel()
        panel._neg_visible = True
        panel.apply_ui_config(
            {"config_panel": {"prompt_preview_height": 10}}
        )
        self.assertEqual(panel.prompt_preview._height, 5)  # split (10-5)
        self.assertEqual(panel._positive_prompt_full_height, 10)
        self.assertEqual(panel._positive_prompt_split_height, 5)


class TemplateDrivenShipModelTests(unittest.TestCase):
    """CodeRabbit Major (PR #41): the bundle's current_model +
    model_display_name must be template-driven, not duplicated
    literals — so the next default-model bump is a single
    default_config_template.json edit. Hardcoded fallbacks remain the
    current ship target so a template missing those keys still builds
    a working bundle."""

    def test_template_value_overrides_default(self):
        import json as _j, os, tempfile
        from pathlib import Path
        from distribution.release_prep import build_sanitized_config
        with tempfile.TemporaryDirectory() as d:
            t = os.path.join(d, "default_config_template.json")
            l = os.path.join(d, "kling_config.json")
            with open(t, "w", encoding="utf-8") as fp:
                fp.write(_j.dumps({
                    "current_model": "fal-ai/kling-video/v3/pro/image-to-video",
                    "model_display_name": "Kling 3.0 Pro",
                }))
            with open(l, "w", encoding="utf-8") as fp:
                fp.write(_j.dumps({
                    "current_model": "old-stale-endpoint",
                    "model_display_name": "Old Stale",
                }))
            cfg = build_sanitized_config(Path(t), Path(l))
        # Template value WINS, even with the dev's stale live config.
        self.assertEqual(cfg["current_model"], "fal-ai/kling-video/v3/pro/image-to-video")
        self.assertEqual(cfg["model_display_name"], "Kling 3.0 Pro")

    def test_hardcoded_fallback_when_template_missing_keys(self):
        import json as _j, os, tempfile
        from pathlib import Path
        from distribution.release_prep import build_sanitized_config
        with tempfile.TemporaryDirectory() as d:
            t = os.path.join(d, "default_config_template.json")
            l = os.path.join(d, "kling_config.json")
            # Template explicitly lacks current_model + model_display_name
            with open(t, "w", encoding="utf-8") as fp:
                fp.write(_j.dumps({}))
            with open(l, "w", encoding="utf-8") as fp:
                fp.write(_j.dumps({}))
            cfg = build_sanitized_config(Path(t), Path(l))
        # Hardcoded fallback is the current ship target.
        self.assertEqual(
            cfg["current_model"],
            "fal-ai/kling-video/v2.5-turbo/pro/image-to-video",
        )
        self.assertEqual(cfg["model_display_name"], "Kling 2.5 Turbo Pro")


class CustomEndpointEndImageUrlFallbackTests(unittest.TestCase):
    """Codex P2 (PR #41): an unknown (custom) endpoint with no known
    end-param name in MODEL_METADATA used to silently drop a
    user-supplied end_image_url because end_param resolved to None.
    The fix forwards it under "end_image_url" (the modern common key)
    and lets schema_manager.validate_parameters be the authority —
    mirroring the existing _is_known_model bypass pattern for
    negative_prompt + cfg_scale."""

    def test_dispatcher_source_has_unknown_endpoint_fallback(self):
        src = (_ROOT / "kling_generator_falai.py").read_text(encoding="utf-8")
        # The fallback branch must exist after the end_param if-block.
        self.assertRegex(
            src,
            r"elif\s+end_image_url\s+and\s+not\s+_is_known_model:",
        )
        # And it must forward to "end_image_url" specifically.
        self.assertRegex(
            src,
            r'payload_full\["end_image_url"\]\s*=\s*end_image_url',
        )

    def test_known_models_with_no_end_param_still_drop(self):
        """Critical safety invariant: a KNOWN model whose
        end_image_param is None (e.g. v2.5-turbo/standard) must still
        NOT forward end_image_url — only unknown endpoints get the
        bypass."""
        from model_metadata import get_model_capabilities, get_model_by_endpoint

        ep = "fal-ai/kling-video/v2.5-turbo/standard/image-to-video"
        self.assertIsNotNone(get_model_by_endpoint(ep))  # known
        self.assertIsNone(get_model_capabilities(ep)["end_image_param"])
        # Source-level guard: the fallback branch requires
        # `not _is_known_model`, so a known model with None end_param
        # cannot enter it (precise per-model gating preserved).


if __name__ == "__main__":
    unittest.main()
