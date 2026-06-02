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
        # Step 2.5 reads ONLY its section-specific key, with "none"
        # as the ship default. The previous back-compat fallback to
        # the shared ``outpaint_composite_mode`` was the source of
        # Step 0 silently being clobbered with Step 2.5's "none"
        # across sessions (PR #48 round 3 fix).
        self.assertRegex(
            src,
            r'self\.config\.get\(\s*\n?\s*"automation_selfie_expand_composite_mode",\s*"none",\s*\)',
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

    def test_release_prep_force_outpaint_provider_to_template_value(self):
        """User direction 2026-05-22 final: outpaint_provider default is
        "fal" everywhere — switching providers is a one-click dropdown
        change so the silent default should be "fal" out of the box.

        The earlier macOS revert was over-broad — it rolled back BOTH
        the Phase A "fal-first" default AND the Phase C
        LANCZOS+16px-tolerance composite tweaks. Only the latter caused
        the visible expand-quality regression (and was correctly rolled
        back in d48bbc8). The provider default was incorrectly bundled
        with that revert and is restored here.

        Lock: a dev kling_config.json carrying "bfl" from a prior tuned
        session must NOT leak that value into the shipped bundle. The
        bundle gets the template's "fal" via the OVERRIDE in
        build_sanitized_config."""
        import json as _j, tempfile, os
        from distribution.release_prep import build_sanitized_config
        from pathlib import Path
        with tempfile.TemporaryDirectory() as d:
            t = os.path.join(d, 'default_config_template.json')
            l = os.path.join(d, 'kling_config.json')
            with open(t, 'w', encoding='utf-8') as fp:
                fp.write(_j.dumps({
                    'loop_videos': False,
                    'outpaint_provider': 'fal',
                }))
            # Dev kling_config still has "bfl" from prior tuned sessions.
            with open(l, 'w', encoding='utf-8') as fp:
                fp.write(_j.dumps({'outpaint_provider': 'bfl'}))
            cfg = build_sanitized_config(Path(t), Path(l))
        self.assertEqual(
            cfg.get('outpaint_provider'),
            'fal',
            "Shipped bundle MUST force outpaint_provider to 'fal' "
            "regardless of the dev kling_config.json value (user "
            "direction 2026-05-22 final).",
        )

    def test_shipped_template_default_slot_and_titles(self):
        import json as _j
        d=_j.loads((_ROOT/'default_config_template.json').read_text(encoding='utf-8'))
        # v2.17 (user direction 2026-06-03): the default selected slot is now 5,
        # the "head turn 35 degrees v3" bidirectional liveness rotation prompt.
        self.assertEqual(d['current_prompt_slot'], 5)
        self.assertEqual(d['prompt_titles']['5'], 'head turn 35 degrees v3')
        self.assertIn('35-degree', d['saved_prompts']['5'])
        self.assertIn('bidirectional liveness head rotation', d['saved_prompts']['5'])
        self.assertTrue(d['negative_prompts']['5'])
        # slot 3 content preserved (still selectable, just no longer the default):
        # the "enhanced for Kling 2.5 Pro" 30° prompt (locked 2026-05-22).
        self.assertEqual(d['prompt_titles']['3'], 'enhanced for Kling 2.5 Pro')
        self.assertIn('three-quarter view', d['saved_prompts']['3'])
        self.assertIn('30 degrees', d['saved_prompts']['3'])
        self.assertNotIn('40 degrees', d['saved_prompts']['3'])
        self.assertTrue(d['negative_prompts']['3'])
        # slot 1 minimal-motion fallback preserved.
        self.assertIn('very subtle, slow head movement',d['saved_prompts']['1'])

    def test_v23_ship_defaults_loop_off_and_provider_fal(self):
        """polish/v2.3 ship defaults locked: loop OFF + outpaint_provider
        "fal" (user direction 2026-05-22 final).

        The earlier revert was over-broad: only the macOS LANCZOS +
        16px-tolerance composite tweaks needed rolling back (rolled
        back in d48bbc8). The provider default itself stays "fal"
        because switching providers is a one-click dropdown change
        and the silent default should be "fal" out of the box."""
        import json as _j
        d = _j.loads((_ROOT/'default_config_template.json').read_text(encoding='utf-8'))
        self.assertEqual(d['loop_videos'], False)
        self.assertEqual(
            d.get('outpaint_provider'),
            'fal',
            "outpaint_provider MUST be 'fal' in the template (user "
            "direction 2026-05-22 final). One-click dropdown switches "
            "to BFL when the user wants it.",
        )

    def test_v23_three_independent_expand_prompts(self):
        """Phase G of polish/v2.3 (2026-05-22): Step 0 face-crop,
        Step 2.5 selfie, and the standalone Outpaint tab each have
        their OWN expand-prompt config key. Default template ships
        all three with the same starting value so first-launch
        behaviour matches the prior single-shared-key design."""
        import json as _j
        d = _j.loads((_ROOT/'default_config_template.json').read_text(encoding='utf-8'))
        # All three keys must exist in the shipped template.
        for key in ('face_crop_expand_prompt', 'selfie_expand_prompt', 'outpaint_tab_prompt'):
            self.assertIn(key, d, f"template missing Phase G key {key!r}")
        # And each tab's source must wire its editor to the right key.
        fc_src = (_ROOT / "kling_gui" / "tabs" / "face_crop_tab.py").read_text(encoding="utf-8")
        self.assertIn('self.config.get("face_crop_expand_prompt")', fc_src)
        self.assertIn('updates["face_crop_expand_prompt"]', fc_src)

        ex_src = (_ROOT / "kling_gui" / "tabs" / "expand_tab.py").read_text(encoding="utf-8")
        self.assertIn('self.config.get("selfie_expand_prompt")', ex_src)
        self.assertIn('"selfie_expand_prompt":', ex_src)

        op_src = (_ROOT / "kling_gui" / "tabs" / "outpaint_tab.py").read_text(encoding="utf-8")
        self.assertIn('self.config.get("outpaint_tab_prompt")', op_src)
        self.assertIn('"outpaint_tab_prompt":', op_src)

    def test_v23_section_prompts_respect_explicit_empty_string(self):
        """Codex P1 on 0967564 (2026-05-22): the Phase G fallback
        chain ``section or legacy or \"\"`` treated an explicitly-saved
        empty string as missing and silently substituted the legacy
        shared prompt. R4 fix: use key-presence semantics
        (isinstance check) so an empty string survives as a valid
        intentional value.

        Source-asserted in all 6 fallback sites so a regression
        that reintroduces ``or`` truthiness fails this test."""
        # Pipeline Step 0 + Step 2.5
        pp_src = (_ROOT / "automation" / "pipeline.py").read_text(encoding="utf-8")
        # Both sections must use isinstance check on the section key.
        self.assertGreaterEqual(
            pp_src.count('isinstance(_section, str)'),
            2,
            "automation/pipeline.py must use isinstance() check at "
            "BOTH Step 0 and Step 2.5 to preserve empty-string section prompts",
        )
        # The forbidden truthiness chain must NOT be back.
        for forbidden in (
            'self.config.get("face_crop_expand_prompt")\n                        or self.config.get("outpaint_prompt"',
            'self.config.get("selfie_expand_prompt")\n                    or self.config.get("outpaint_prompt"',
        ):
            self.assertNotIn(
                forbidden, pp_src,
                "automation/pipeline.py regressed to `or` truthiness fallback",
            )
        # GUI tabs: face_crop_tab + outpaint_tab use isinstance.
        fc_src = (_ROOT / "kling_gui" / "tabs" / "face_crop_tab.py").read_text(encoding="utf-8")
        self.assertIn('isinstance(_section_prompt, str)', fc_src)
        op_src = (_ROOT / "kling_gui" / "tabs" / "outpaint_tab.py").read_text(encoding="utf-8")
        self.assertIn('isinstance(_section_prompt, str)', op_src)
        # expand_tab uses isinstance OR routes through the named helper.
        ex_src = (_ROOT / "kling_gui" / "tabs" / "expand_tab.py").read_text(encoding="utf-8")
        self.assertIn('isinstance(_section_prompt, str)', ex_src)
        self.assertIn('def _fallback_selfie_expand_prompt', ex_src)
        # Codex P2 on 6080445 (2026-05-22): the defensive ``except``
        # fallback at the queue-submit path MUST also route through
        # the named helper, otherwise an explicitly-saved empty
        # ``selfie_expand_prompt`` is silently replaced by
        # ``outpaint_prompt`` whenever ``_prompt_text`` is unavailable
        # — the exact regression R4 fixed on the happy path.
        self.assertNotIn(
            'cfg.get("selfie_expand_prompt")\n                or cfg.get("outpaint_prompt"',
            ex_src,
            "expand_tab.py except-fallback regressed to `or` truthiness",
        )
        self.assertIn(
            'prompt = self._fallback_selfie_expand_prompt()',
            ex_src,
            "expand_tab.py except-fallback must route through helper",
        )

    def test_v23_extra_prompt_slots_shipped(self):
        """polish/v2.3: saved_prompts slots 5+6 and
        selfie_wildcard_saved_prompts slots 4+5+6 ship populated so a
        fresh-clone user sees the user's full Windows prompt collection,
        not just the original 4 slots."""
        import json as _j
        d = _j.loads((_ROOT/'default_config_template.json').read_text(encoding='utf-8'))
        for slot in ('5', '6'):
            self.assertTrue(
                d['saved_prompts'][slot].strip(),
                f"saved_prompts[{slot}] must be populated in template",
            )
        for slot in ('4', '5', '6'):
            self.assertTrue(
                d['selfie_wildcard_saved_prompts'][slot].strip(),
                f"selfie_wildcard_saved_prompts[{slot}] must be populated",
            )
        # And the prompt_titles row reflects user-set titles, not blanks
        for slot in ('3', '4', '5', '6'):
            self.assertTrue(
                d['prompt_titles'][slot].strip(),
                f"prompt_titles[{slot}] must be set",
            )


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

    def test_build_prompt_panel_reapplies_motion_controls(self):
        """Regression for 3cc93ef: when ConfigPanel is constructed with
        build_prompt=False (main_window's split layout — prompt panel
        built externally), the __init__-time _load_config call to
        _update_motion_controls runs BEFORE _negative_prompt_section
        exists. The first visibility update no-ops; without an
        explicit re-call after build_prompt_panel creates the section,
        the negative-prompt half stays hidden on startup for every
        model that supports it.

        Static-text check: build_prompt_panel must call
        _update_motion_controls right after _load_prompt_config so
        the visibility update sees the now-created section."""
        from pathlib import Path
        src = (
            Path(__file__).resolve().parent.parent
            / "kling_gui" / "config_panel.py"
        ).read_text(encoding="utf-8")
        # Locate the build_prompt_panel method body.
        start = src.index("def build_prompt_panel")
        end = src.index("\n    def ", start + 10)
        body = src[start:end]
        # The re-call must appear AFTER _load_prompt_config in the
        # same method. Regex catches either string-literal or
        # variable-derived current_model.
        self.assertIn("self._load_prompt_config()", body)
        load_idx = body.index("self._load_prompt_config()")
        # _update_motion_controls must appear after the load call.
        post = body[load_idx:]
        self.assertIn("_update_motion_controls(", post)
        self.assertIn("current_model", post)


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
        bypass. Verified in three layers:
          (a) metadata: the model IS known + caps says end is None
          (b) source: the fallback elif requires `not _is_known_model`
          (c) AST: the elif test references `_is_known_model`
        Layer (c) makes the guard structurally explicit so a future
        edit that removes the condition fails this test rather than
        silently regressing the invariant (subagent finding, PR #41).
        """
        from model_metadata import get_model_capabilities, get_model_by_endpoint

        ep = "fal-ai/kling-video/v2.5-turbo/standard/image-to-video"
        self.assertIsNotNone(get_model_by_endpoint(ep))  # (a) known
        self.assertIsNone(
            get_model_capabilities(ep)["end_image_param"]
        )

        # (b) source regex — the fallback branch literally requires
        # `not _is_known_model`.
        import re
        gen_src = (_ROOT / "kling_generator_falai.py").read_text(
            encoding="utf-8"
        )
        self.assertRegex(
            gen_src,
            r"elif\s+end_image_url\s+and\s+not\s+_is_known_model:",
        )

        # (c) AST — walk the dispatcher module, find the fallback
        # elif, and confirm its test expression contains a Name
        # `_is_known_model` inside a `not` UnaryOp. This catches any
        # whitespace / formatting change that the source regex might
        # miss while still failing if the condition itself is removed.
        import ast as _ast
        tree = _ast.parse(gen_src)
        found = False
        for node in _ast.walk(tree):
            if isinstance(node, _ast.If):
                test_str = _ast.unparse(node.test)
                if (
                    "end_image_url" in test_str
                    and "not _is_known_model" in test_str
                ):
                    found = True
                    break
        self.assertTrue(
            found,
            "Custom-endpoint end_image_url fallback must be guarded "
            "by `not _is_known_model` to preserve precise per-model "
            "gating for known models whose end_image_param is None.",
        )


class ReleasePrepMalformedConfigTests(unittest.TestCase):
    """Codex P2 (PR #41): build_sanitized_config used to call
    `dict(config.get(key) or {})` blindly, which raised ValueError when
    a user-edited kling_config.json had a string/list at saved_prompts,
    negative_prompts, or prompt_titles instead of a dict — aborting the
    release build. The fix isinstance-guards the live value and falls
    back to {}."""

    def _run(self, live_overrides):
        import json as _j, os, tempfile
        from pathlib import Path
        from distribution.release_prep import build_sanitized_config
        with tempfile.TemporaryDirectory() as d:
            t = os.path.join(d, "default_config_template.json")
            l = os.path.join(d, "kling_config.json")
            with open(t, "w", encoding="utf-8") as fp:
                fp.write(_j.dumps({
                    "saved_prompts": {"1": "TEMPLATE PROMPT"},
                    "negative_prompts": {"1": "TEMPLATE NEG"},
                    "prompt_titles": {"1": "TEMPLATE TITLE"},
                    "current_prompt_slot": 1,
                }))
            base = {"current_prompt_slot": 1}
            base.update(live_overrides)
            with open(l, "w", encoding="utf-8") as fp:
                fp.write(_j.dumps(base))
            return build_sanitized_config(Path(t), Path(l))

    def test_saved_prompts_as_string_does_not_crash(self):
        cfg = self._run({"saved_prompts": "garbage-string"})
        # Falls back to {} then receives the forced template values.
        self.assertEqual(cfg["saved_prompts"]["1"], "TEMPLATE PROMPT")

    def test_negative_prompts_as_list_does_not_crash(self):
        cfg = self._run({"negative_prompts": ["bad", "shape"]})
        self.assertEqual(cfg["negative_prompts"]["1"], "TEMPLATE NEG")

    def test_prompt_titles_as_int_does_not_crash(self):
        cfg = self._run({"prompt_titles": 42})
        self.assertEqual(cfg["prompt_titles"]["1"], "TEMPLATE TITLE")


class ReleasePrepLockEndFrameTemplateTests(unittest.TestCase):
    """Codex P2 (PR #41): lock_end_frame was hardcoded True in
    build_sanitized_config, so a template explicitly setting
    lock_end_frame:false still shipped True. Fix makes it
    template-driven via _parse_bool with None -> True (matches the
    queue_manager + pipeline canonical-default coercion)."""

    def _run(self, template_lock):
        import json as _j, os, tempfile
        from pathlib import Path
        from distribution.release_prep import build_sanitized_config
        with tempfile.TemporaryDirectory() as d:
            t = os.path.join(d, "default_config_template.json")
            l = os.path.join(d, "kling_config.json")
            tmpl = {}
            if template_lock != "__omit__":
                tmpl["lock_end_frame"] = template_lock
            with open(t, "w", encoding="utf-8") as fp:
                fp.write(_j.dumps(tmpl))
            with open(l, "w", encoding="utf-8") as fp:
                fp.write(_j.dumps({"lock_end_frame": True}))
            return build_sanitized_config(Path(t), Path(l))

    def test_template_true_ships_true(self):
        self.assertIs(self._run(True)["lock_end_frame"], True)

    def test_template_false_ships_false(self):
        # The point of the fix: a template explicitly false must NOT
        # be silently overridden back to True.
        self.assertIs(self._run(False)["lock_end_frame"], False)

    def test_template_missing_defaults_to_true(self):
        # No lock_end_frame key in template -> canonical default True.
        self.assertIs(self._run("__omit__")["lock_end_frame"], True)

    def test_template_string_true_parses(self):
        # _parse_bool handles "true"/"false" strings.
        self.assertIs(self._run("false")["lock_end_frame"], False)
        self.assertIs(self._run("true")["lock_end_frame"], True)


class UpdateMotionControlsFailSafeTests(unittest.TestCase):
    """Full-diff subagent finding (PR #41): _update_motion_controls
    wrapped get_model_by_endpoint in a try/except with
    `_is_known = True` as the fail-safe — which silently graded an
    unknown model as known-with-no-caps and DISABLED the cfg + neg
    controls. The intentional design (matching the dispatcher + queue
    bypass philosophy) is the opposite: an endpoint we can't classify
    must default to CUSTOM (False) so the live schema decides and the
    controls stay enabled. Pin both the source AND the runtime
    behaviour via the actual except path."""

    def test_source_fail_safe_is_false(self):
        src = (_ROOT / "kling_gui" / "config_panel.py").read_text(
            encoding="utf-8"
        )
        # The except branch must set _is_known to False, not True.
        # Walk via AST so whitespace can't trick the test.
        import ast as _ast
        tree = _ast.parse(src)
        found = False
        for node in _ast.walk(tree):
            if isinstance(node, _ast.Try):
                for handler in node.handlers:
                    for stmt in handler.body:
                        if (
                            isinstance(stmt, _ast.Assign)
                            and len(stmt.targets) == 1
                            and isinstance(stmt.targets[0], _ast.Name)
                            and stmt.targets[0].id == "_is_known"
                            and isinstance(stmt.value, _ast.Constant)
                        ):
                            if stmt.value.value is False:
                                found = True
                                break
                            if stmt.value.value is True:
                                self.fail(
                                    "_is_known fail-safe is True — "
                                    "graders unknown models as known-no-caps "
                                    "and DISABLES cfg + neg controls. The "
                                    "intentional design is False (treat as "
                                    "custom / let live schema decide)."
                                )
        self.assertTrue(
            found,
            "Could not locate the `_is_known = False` fail-safe in any "
            "except handler of config_panel.py — the design invariant "
            "is unverifiable.",
        )

    def test_runtime_except_branch_disables_neither_control(self):
        """Exercise the except path with a callable that raises and
        confirm has_neg + has_cfg both end up True (controls enabled)
        when the lookup fails. Mirrors lines 1949-1962 of config_panel
        with caps stubbed so we only exercise the fail-safe branch,
        not the unrelated caps-fetch path (which also calls
        get_model_by_endpoint internally and would short-circuit the
        test via the OUTER except in _update_motion_controls)."""
        # Caps stub: a known model with NO neg/cfg support (e.g. o3).
        # If the fail-safe is wrong (True), has_cfg/has_neg would both
        # be False — controls disabled. With the correct fail-safe
        # (False -> treat as unknown / custom), the bypass kicks in
        # and both controls are enabled regardless of caps.
        caps = {
            "supports_cfg_scale": False,
            "supports_negative_prompt": False,
        }

        def _lookup_raises(_ep):
            raise RuntimeError("simulated runtime fault")

        # Re-implement just the try/except + derivation from
        # _update_motion_controls — kept in sync via the AST test
        # above (test_source_fail_safe_is_false).
        try:
            _is_known = _lookup_raises("some-unknown-custom-endpoint") is not None
        except Exception:
            _is_known = False  # MUST match config_panel.py fail-safe
        has_cfg = bool(caps.get("supports_cfg_scale")) or not _is_known
        has_neg = bool(caps.get("supports_negative_prompt")) or not _is_known
        # Fail-safe -> custom -> controls ENABLED.
        self.assertTrue(has_cfg, "cfg control must stay enabled on fail-safe")
        self.assertTrue(has_neg, "neg control must stay enabled on fail-safe")


class Expand25SingleSelectRedesignTests(unittest.TestCase):
    """User-requested PR #41 redesign of Step 2.5:
    1. Candidate listbox is SINGLE-select; clicking a row navigates the
       carousel to that path (forward coupling).
    2. "Select All" / "Select Passing" buttons + their callbacks are
       gone — single-select makes them meaningless.
    3. Inline tk.Text prompt editor backed by outpaint_prompt (same key
       the dispatch already reads). Persists via get_config_updates.
    4. _on_expand_selected uses image_session.active_image_path
       instead of multi-target list selection; button reads "Expand
       Active Image"."""

    def _src(self):
        return (_ROOT / "kling_gui" / "tabs" / "expand_tab.py").read_text(
            encoding="utf-8"
        )

    def test_listbox_is_single_select(self):
        src = self._src()
        # Candidate listbox is SINGLE. (The Expanded Outputs listbox
        # right below it stays EXTENDED — different widget, user may
        # want to send multiple expanded results to Step 3.)
        self.assertRegex(
            src,
            r"self\._candidate_list\s*=\s*tk\.Listbox\("
            r"[\s\S]{0,400}?selectmode=tk\.SINGLE",
        )

    def test_listbox_click_handler_wired(self):
        src = self._src()
        # ListboxSelect binds to _on_candidate_clicked which navigates
        # image_session.navigate_to(idx) for the row's path.
        self.assertIn('"<<ListboxSelect>>", self._on_candidate_clicked', src)
        self.assertIn("def _on_candidate_clicked", src)
        self.assertIn("self.image_session.navigate_to(i)", src)

    def test_select_all_and_passing_buttons_removed(self):
        src = self._src()
        self.assertNotIn('text="Select All"', src)
        self.assertNotIn('text="Select Passing"', src)
        # Their callbacks are dead-code-deleted too.
        self.assertNotIn("def _select_all_candidates", src)
        self.assertNotIn("def _select_passing_candidates", src)

    def test_prompt_editor_widget_exists_and_loads_config_key(self):
        src = self._src()
        # tk.Text widget on the tab, backed by selfie_expand_prompt
        # (Phase G of polish/v2.3, 2026-05-22). Pre-Phase-G all three
        # expand-section editors shared the legacy ``outpaint_prompt``
        # key; now each editor has its own with a back-compat
        # fallback to the shared key.
        self.assertIn("self._prompt_text = tk.Text(", src)
        # Loaded from config at construction time via the new
        # section-specific key, with the legacy shared key as
        # back-compat fallback.
        self.assertIn('self.config.get("selfie_expand_prompt")', src)
        self.assertIn('self.config.get("outpaint_prompt", "")', src)

    def test_prompt_text_is_read_at_dispatch_time(self):
        """The live editor value (not the stale cfg blob) must be what
        ships to fal.ai / BFL — so a just-edited prompt actually takes
        effect on the next Expand click without saving first."""
        src = self._src()
        # _on_expand_selected reads from self._prompt_text first.
        self.assertRegex(
            src,
            r'prompt\s*=\s*self\._prompt_text\.get\("1\.0",\s*"end-1c"\)',
        )

    def test_expand_button_text_is_active_image(self):
        src = self._src()
        # Construction-time text.
        self.assertIn('text="Expand Active Image"', src)
        # Busy-toggle resume text.
        self.assertIn('else "Expand Active Image"', src)
        # Old "Expand Selected" wording is gone.
        self.assertNotIn('text="Expand Selected"', src)
        self.assertNotIn('else "Expand Selected"', src)

    def test_expand_targets_active_image_not_listbox_selection(self):
        src = self._src()
        # _on_expand_selected reads from image_session.active_image_path,
        # not _get_selected_candidate_entries() any more.
        self.assertRegex(
            src,
            r"def _on_expand_selected\(self\):"
            r"[\s\S]{0,500}?"
            r"active_path\s*=\s*self\.image_session\.active_image_path",
        )

    def test_get_config_updates_persists_outpaint_prompt(self):
        """Saving the panel writes the live editor value back so it
        survives a restart. Phase G (2026-05-22): the key is now
        ``selfie_expand_prompt`` (section-specific), not the legacy
        shared ``outpaint_prompt``. Updates flow ONLY to the new key
        so a Step 2.5 edit can't silently override Step 0 or the
        Outpaint tab."""
        src = self._src()
        # The dict literal must include the section-specific key
        # wired to self._prompt_text.get(...).
        self.assertRegex(
            src,
            r'"selfie_expand_prompt":\s*\(\s*'
            r'self\._prompt_text\.get\(\s*"1\.0",\s*"end-1c"\s*\)',
        )


class Expand25NonCandidateFallbackTests(unittest.TestCase):
    """Codex P1 on commit 4eebb81 (PR #41): when the active carousel
    image isn't in the selfie-candidate list (a crop / front original /
    any non-selfie navigated to in the carousel), the redesigned
    _on_expand_selected previously built an ad-hoc namedtuple that
    lacked the ImageEntry API (update_similarity / set_similarity_override
    / similarity_override). Passing it to _approve_override_if_needed
    raised AttributeError before expansion could start. The fix uses
    image_session.active_entry directly — a real ImageEntry with the
    full method surface."""

    def test_fallback_uses_image_session_active_entry(self):
        src = (
            _ROOT / "kling_gui" / "tabs" / "expand_tab.py"
        ).read_text(encoding="utf-8")
        # The fragile _AdHoc namedtuple path is gone.
        self.assertNotIn("_AdHoc", src)
        self.assertNotIn("namedtuple as _nt", src)
        # The fallback uses the live session entry.
        self.assertRegex(
            src,
            r"entry\s*=\s*self\.image_session\.active_entry",
        )

    def test_active_entry_supports_override_api(self):
        """Sanity: verify the real ImageEntry has the methods
        _approve_override_if_needed needs (so the fix is semantically
        correct, not just syntactically replacing one path with another)."""
        import importlib
        ist = importlib.import_module("kling_gui.image_state")
        entry_cls = getattr(ist, "ImageEntry")
        # The methods/attrs used inside _approve_override_if_needed.
        for attr in (
            "update_similarity",
            "set_similarity_override",
            "similarity_override",
            "similarity",
            "similarity_score",
            "path",
            "filename",
        ):
            self.assertTrue(
                hasattr(entry_cls, attr) or attr in entry_cls.__annotations__,
                f"ImageEntry must expose {attr!r}",
            )


class FallbackSchemaPreservesV3PayloadTests(unittest.TestCase):
    """Codex P1 on commit 516396b (PR #41): _get_fallback_schema only
    included image_url + prompt + duration + aspect_ratio. When fal.ai
    schema fetch failed (429/5xx) for a v3-family endpoint that uses
    start_image_url, validate_parameters' core-field gate passed
    (image_url + prompt were both in the fallback) so the precise
    filter ran and dropped start_image_url, sending the request to
    fal.ai with no start image -> generation reliably failed.

    Fix is two layers:
      (A) Fallback schema now includes start_image_url + end_image_url
          + tail_image_url + negative_prompt + cfg_scale as a union of
          all roster keys.
      (B) Fallback dict is tagged with _FALLBACK_SENTINEL so
          validate_parameters detects it and skips filtering entirely
          (returning the payload unfiltered; live fal.ai is the
          authority)."""

    def test_fallback_schema_includes_v3_start_image_url(self):
        from model_schema_manager import ModelSchemaManager
        m = ModelSchemaManager(api_key="test-key-not-used")
        schema = m._get_fallback_schema()
        # Layer (A): all the v3-family param names are present.
        for key in (
            "image_url",
            "start_image_url",
            "end_image_url",
            "tail_image_url",
            "negative_prompt",
            "cfg_scale",
            "prompt",
            "duration",
            "aspect_ratio",
        ):
            self.assertIn(key, schema, f"fallback must include {key!r}")

    def test_fallback_schema_is_tagged_with_sentinel(self):
        from model_schema_manager import ModelSchemaManager
        m = ModelSchemaManager(api_key="test-key-not-used")
        schema = m._get_fallback_schema()
        # Layer (B): the sentinel marks this schema as non-authoritative.
        self.assertIn(m._FALLBACK_SENTINEL, schema)
        self.assertTrue(schema[m._FALLBACK_SENTINEL])

    def test_validate_parameters_sends_v3_payload_unfiltered_on_fallback(self):
        """The bug in production: a v3 payload with start_image_url +
        end_image_url + negative_prompt + cfg_scale must round-trip
        intact through validate_parameters when the schema is the
        fallback (i.e. live fetch failed)."""
        from model_schema_manager import ModelSchemaManager
        from unittest import mock
        m = ModelSchemaManager(api_key="test-key-not-used")
        # Force get_model_schema to return the fallback (no live fetch).
        fallback = m._get_fallback_schema()
        with mock.patch.object(
            m, "get_model_schema", return_value=fallback
        ):
            payload = {
                "start_image_url": "https://example.com/start.png",
                "end_image_url": "https://example.com/end.png",
                "prompt": "a person turning their head",
                "negative_prompt": "profile view, fast motion",
                "cfg_scale": 0.7,
                "duration": 10,
                "aspect_ratio": "9:16",
            }
            result = m.validate_parameters(
                "fal-ai/kling-video/v3/pro/image-to-video", payload
            )
        # Every required key must survive — the bug was silently
        # dropping start_image_url here.
        self.assertEqual(result.get("start_image_url"), payload["start_image_url"])
        self.assertEqual(result.get("end_image_url"), payload["end_image_url"])
        self.assertEqual(result.get("prompt"), payload["prompt"])
        self.assertEqual(result.get("negative_prompt"), payload["negative_prompt"])
        self.assertEqual(result.get("cfg_scale"), payload["cfg_scale"])

    def test_validate_parameters_with_tail_image_url_on_fallback(self):
        """Sister case: v2.5-turbo-pro uses tail_image_url (not
        end_image_url). Must also survive the fallback path."""
        from model_schema_manager import ModelSchemaManager
        from unittest import mock
        m = ModelSchemaManager(api_key="test-key-not-used")
        with mock.patch.object(
            m,
            "get_model_schema",
            return_value=m._get_fallback_schema(),
        ):
            payload = {
                "image_url": "https://example.com/img.png",
                "tail_image_url": "https://example.com/img.png",
                "prompt": "head turn",
                "cfg_scale": 0.7,
            }
            result = m.validate_parameters(
                "fal-ai/kling-video/v2.5-turbo/pro/image-to-video",
                payload,
            )
        self.assertEqual(result.get("tail_image_url"), payload["tail_image_url"])
        self.assertEqual(result.get("image_url"), payload["image_url"])
        self.assertEqual(result.get("cfg_scale"), payload["cfg_scale"])

    def test_live_schema_still_does_precise_filtering(self):
        """Make sure the fix doesn't regress the LIVE-schema path —
        when get_model_schema returns a real authoritative schema (no
        sentinel), validate_parameters MUST still filter unknown
        params (otherwise we ship bogus keys to fal.ai)."""
        from model_schema_manager import ModelSchemaManager, ModelParameter
        from unittest import mock
        m = ModelSchemaManager(api_key="test-key-not-used")
        # Synthetic LIVE schema (no sentinel) — only image_url + prompt.
        live_schema = {
            "image_url": ModelParameter("image_url", "string", True, ""),
            "prompt": ModelParameter("prompt", "string", True, ""),
        }
        with mock.patch.object(
            m, "get_model_schema", return_value=live_schema
        ):
            result = m.validate_parameters(
                "fal-ai/some-known-endpoint",
                {
                    "image_url": "u",
                    "prompt": "p",
                    "bogus_key": "should_be_filtered",
                },
            )
        self.assertNotIn("bogus_key", result)
        self.assertEqual(result["image_url"], "u")
        self.assertEqual(result["prompt"], "p")


if __name__ == "__main__":
    unittest.main()
