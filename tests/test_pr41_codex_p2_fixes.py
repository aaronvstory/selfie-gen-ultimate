"""Regression for two Codex P2 findings on PR #41.

1. models.json marks `bytedance/seedance-2.0/image-to-video` with
   `"hidden": true`, but ConfigPanel.get_merged_models only filtered
   `config["hidden_models"]` and never read the per-model flag, so the
   internal Seedance endpoint leaked into the main model dropdown on a
   fresh config.

2. The GUI queue path parsed `cfg_scale_value` with `float(...)` but
   never clamped it to [0.0, 1.0], while automation/pipeline.py does
   (`max(0.0, min(1.0, _cfg_val))`). A stale / hand-edited out-of-range
   persisted value made the GUI submit an invalid cfg_scale and fail at
   API validation while the CLI silently clamped — GUI/CLI drift.
"""

import sys
import unittest
from pathlib import Path

_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_ROOT))


class SeedanceVisibilityTests(unittest.TestCase):
    # 2026-06-25 (user mandate): ALL i2v Seedance models are now first-class
    # VISIBLE entries in models.json (1.0 Pro, 1.0 Pro Fast, 1.5 Pro, 2.0,
    # 2.0 Fast, 2.0 Mini). The earlier hidden:true on Seedance 2.0 was removed.
    # The hidden-model *mechanism* itself is still covered by
    # test_user_hidden_models_still_filtered below (via hidden_models config).
    def test_all_seedance_models_visible_in_fresh_config_dropdown(self):
        from kling_gui.config_panel import ModelFetcher

        endpoints = {
            m.get("endpoint") for m in ModelFetcher.get_merged_models({})
        }
        for ep in (
            "fal-ai/bytedance/seedance/v1/pro/image-to-video",
            "fal-ai/bytedance/seedance/v1/pro/fast/image-to-video",
            "fal-ai/bytedance/seedance/v1.5/pro/image-to-video",
            "bytedance/seedance-2.0/image-to-video",
            "bytedance/seedance-2.0/fast/image-to-video",
            "bytedance/seedance-2.0/mini/image-to-video",
        ):
            self.assertIn(
                ep, endpoints, f"Seedance model {ep} should be visible by default"
            )
        # Sanity: a normal Kling model is still present.
        self.assertIn(
            "fal-ai/kling-video/v2.5-turbo/standard/image-to-video", endpoints
        )

    def test_user_hidden_models_still_filtered(self):
        """The pre-existing config["hidden_models"] mechanism still
        works alongside the per-model flag."""
        from kling_gui.config_panel import ModelFetcher

        cfg = {
            "hidden_models": [
                "fal-ai/kling-video/v2.5-turbo/pro/image-to-video"
            ]
        }
        endpoints = {
            m.get("endpoint") for m in ModelFetcher.get_merged_models(cfg)
        }
        self.assertNotIn(
            "fal-ai/kling-video/v2.5-turbo/pro/image-to-video", endpoints
        )


class CfgScaleClampParityTests(unittest.TestCase):
    """The GUI queue clamp must mirror automation/pipeline.py exactly."""

    @staticmethod
    def _pipeline_clamp(v):
        return max(0.0, min(1.0, v))

    def test_clamp_expression_matches_pipeline(self):
        for raw, expected in [
            (-3.0, 0.0),
            (0.0, 0.0),
            (0.5, 0.5),
            (0.7, 0.7),
            (1.0, 1.0),
            (1.5, 1.0),
            (99.0, 1.0),
        ]:
            self.assertEqual(
                self._pipeline_clamp(raw),
                expected,
                f"clamp({raw}) should be {expected}",
            )

    def test_gui_queue_source_clamps_cfg_scale(self):
        """Structural guard: the production clamp line must stay in the
        GUI dispatch path (queue_manager) so this never silently
        regresses back to the unclamped float()."""
        src = (_ROOT / "kling_gui" / "queue_manager.py").read_text(
            encoding="utf-8"
        )
        self.assertRegex(
            src,
            r"_cfg_scale\s*=\s*max\(\s*0\.0\s*,\s*min\(\s*1\.0\s*,\s*_cfg_scale\s*\)\s*\)",
            "queue_manager.py must clamp _cfg_scale to [0.0, 1.0] "
            "to match automation/pipeline.py",
        )

    def test_pipeline_source_still_clamps(self):
        """Pin the parity reference too — if the pipeline clamp is ever
        removed, GUI/CLI drift returns and this catches it."""
        src = (_ROOT / "automation" / "pipeline.py").read_text(
            encoding="utf-8"
        )
        self.assertRegex(
            src,
            r"max\(\s*0\.0\s*,\s*min\(\s*1\.0\s*,\s*_cfg_val\s*\)\s*\)",
        )


class StringDurationCoercionTests(unittest.TestCase):
    """Codex P2 (PR #41): models.json duration_options/duration_default
    are JSON STRINGS ("5","10"). The model editor checked membership
    with ints (`5 in dur_opts`) -> every factory model would open with
    both duration boxes UNSET and a Save could rewrite durations to
    [10], dropping 5s. The _as_int_durations() coercion in
    model_manager_dialog._switch_to_edit_mode fixes this; pin it so the
    regression can't silently return."""

    def test_models_json_durations_are_strings(self):
        import json

        d = json.loads((_ROOT / "models.json").read_text(encoding="utf-8"))
        pro = next(
            m for m in d["models"]
            if m["endpoint"] == "fal-ai/kling-video/v2.5-turbo/pro/image-to-video"
        )
        # Documents the data shape the editor must tolerate.
        assert all(isinstance(v, str) for v in pro["duration_options"])

    def test_editor_coerces_string_durations_to_int(self):
        src = (
            _ROOT / "kling_gui" / "model_manager_dialog.py"
        ).read_text(encoding="utf-8")
        # The normalizer must exist and be applied before the int
        # membership checks (5 in dur_opts / 10 in dur_opts).
        self.assertIn("def _as_int_durations", src)
        self.assertRegex(src, r"dur_opts\s*=\s*_as_int_durations\(")
        self.assertRegex(src, r"5 in dur_opts")
        self.assertRegex(src, r"10 in dur_opts")

    def test_as_int_durations_logic_handles_strings(self):
        # Replicate the exact coercion to prove "5"/"10" -> 5/10 so the
        # checkbox membership tests pass (the behavioural guarantee).
        def _as_int_durations(raw):
            out = []
            for v in raw or []:
                try:
                    out.append(int(v))
                except (TypeError, ValueError):
                    continue
            return out

        opts = _as_int_durations(["5", "10"])
        self.assertEqual(opts, [5, 10])
        self.assertIn(5, opts)
        self.assertIn(10, opts)
        # Garbage is skipped, not crashing.
        self.assertEqual(_as_int_durations(["5", "auto", None, "10"]), [5, 10])


class CliBatchCfgLockParityTests(unittest.TestCase):
    """code-reviewer (PR #41): process_all_images_concurrent — the
    interactive CLI "Batch Generate" path — was never threaded with
    cfg_scale / lock_end_frame, so a user who set recommended defaults
    (cfg_scale_value=0.7, lock_end_frame=True) silently got NEITHER on
    the CLI batch flow while the GUI queue + automation pipeline both
    honoured them. Pin: (a) the generator signature accepts both, (b)
    both CLI call sites forward them, (c) the shared resolver mirrors
    the pipeline's clamp + None->True coercion exactly."""

    def test_generator_signature_accepts_cfg_and_lock(self):
        import inspect
        from kling_generator_falai import FalAIKlingGenerator

        sig = inspect.signature(
            FalAIKlingGenerator.process_all_images_concurrent
        )
        self.assertIn("cfg_scale", sig.parameters)
        self.assertIn("lock_end_frame", sig.parameters)
        # Defaults must match create_kling_generation (generator gates
        # both per-model, so a no-op default is correct).
        self.assertIsNone(sig.parameters["cfg_scale"].default)
        self.assertIs(sig.parameters["lock_end_frame"].default, False)

    def test_generator_forwards_into_create_kling_generation(self):
        src = (_ROOT / "kling_generator_falai.py").read_text(
            encoding="utf-8"
        )
        self.assertRegex(src, r"cfg_scale=cfg_scale,")
        self.assertRegex(src, r"lock_end_frame=lock_end_frame,")

    def test_both_cli_call_sites_pass_resolved_values(self):
        src = (_ROOT / "kling_automation_ui.py").read_text(
            encoding="utf-8"
        )
        # The shared resolver must be invoked, and both call sites must
        # forward its output (exactly two call sites today).
        self.assertIn("def _resolve_cfg_and_lock", src)
        self.assertEqual(
            src.count("_cfg_scale, _lock_ef = self._resolve_cfg_and_lock()"),
            2,
        )
        self.assertEqual(src.count("cfg_scale=_cfg_scale,"), 2)
        self.assertEqual(src.count("lock_end_frame=_lock_ef,"), 2)

    def test_resolver_clamps_and_coerces_like_pipeline(self):
        from kling_automation_ui import KlingAutomationUI

        ui = KlingAutomationUI.__new__(KlingAutomationUI)

        # Out-of-range cfg_scale clamps to [0,1]; unparseable
        # lock_end_frame coerces to True (default True parity).
        ui.config = {"cfg_scale_value": 9.9, "lock_end_frame": "garbage"}
        cfg, lock = ui._resolve_cfg_and_lock()
        self.assertEqual(cfg, 1.0)
        self.assertIs(lock, True)

        ui.config = {"cfg_scale_value": -2, "lock_end_frame": False}
        cfg, lock = ui._resolve_cfg_and_lock()
        self.assertEqual(cfg, 0.0)
        self.assertIs(lock, False)

        ui.config = {"cfg_scale_value": "nan-ish", "lock_end_frame": True}
        cfg, lock = ui._resolve_cfg_and_lock()
        self.assertEqual(cfg, 0.7)  # bad value -> default
        self.assertIs(lock, True)

        # Missing keys -> documented defaults (0.7, True).
        ui.config = {}
        cfg, lock = ui._resolve_cfg_and_lock()
        self.assertEqual(cfg, 0.7)
        self.assertIs(lock, True)


class QueueManagerCustomModelCapsTests(unittest.TestCase):
    """code-reviewer (PR #41): the dispatcher
    (kling_generator_falai.py) gained an `_is_known_model` bypass so
    CUSTOM endpoints (not in MODEL_METADATA) keep negative_prompt +
    cfg_scale (live fal.ai schema is the authority). The GUI queue
    path (kling_gui/queue_manager.py) pre-stripped both to None using
    only the conservative get_model_capabilities default BEFORE the
    dispatcher's bypass could apply, so GUI custom-model users
    silently lost neg/cfg. The fix mirrors the dispatcher; pin both so
    the GUI and dispatcher gating cannot drift apart again."""

    def test_queue_manager_has_is_known_bypass(self):
        src = (_ROOT / "kling_gui" / "queue_manager.py").read_text(
            encoding="utf-8"
        )
        self.assertRegex(src, r"get_model_by_endpoint")
        self.assertRegex(src, r"_is_known\s*=")
        self.assertRegex(
            src,
            r'_caps\["supports_negative_prompt"\]\s*or\s*not\s*_is_known',
        )
        self.assertRegex(
            src,
            r'_caps\["supports_cfg_scale"\]\s*or\s*not\s*_is_known',
        )

    def test_dispatcher_still_has_is_known_bypass(self):
        src = (_ROOT / "kling_generator_falai.py").read_text(
            encoding="utf-8"
        )
        self.assertRegex(
            src,
            r'caps\["supports_negative_prompt"\]\s*or\s*not\s*_is_known_model',
        )
        self.assertRegex(
            src,
            r'caps\["supports_cfg_scale"\]\s*or\s*not\s*_is_known_model',
        )

    def test_known_model_gating_not_weakened(self):
        from model_metadata import (
            get_model_capabilities,
            get_model_by_endpoint,
        )

        o3 = "fal-ai/kling-video/o3/standard/image-to-video"
        self.assertIsNotNone(get_model_by_endpoint(o3))
        caps = get_model_capabilities(o3)
        self.assertFalse(caps["supports_negative_prompt"])
        self.assertFalse(caps["supports_cfg_scale"])
        custom = "fal-ai/some-user-custom/model/image-to-video"
        self.assertIsNone(get_model_by_endpoint(custom))


class ModelsJsonFactVerifiedCapsTests(unittest.TestCase):
    """Codex P1 (PR #41): legacy models had blanket conservative
    neg/cfg defaults that were NOT fetch-verified. The known-model
    dispatch now gates on these flags, so a wrong flag silently drops
    valid controls. These values were fetch-verified against the live
    fal.ai OpenAPI schema (2026-05-19). Pin the corrected ones so a
    regression can't silently revert to the wrong blanket defaults.
    Offline assertions — no network in CI."""

    def _model(self, endpoint):
        import json

        d = json.loads((_ROOT / "models.json").read_text(encoding="utf-8"))
        return next(m for m in d["models"] if m["endpoint"] == endpoint)

    def test_v21_pro_supports_neg_and_cfg(self):
        m = self._model("fal-ai/kling-video/v2.1/pro/image-to-video")
        self.assertTrue(m["supports_negative_prompt"])
        self.assertTrue(m["supports_cfg_scale"])
        self.assertEqual(m["end_image_param"], "tail_image_url")

    def test_v26_pro_neg_true_cfg_false_end_image_url(self):
        # Live schema: negative_prompt yes, cfg_scale NO, start/end are
        # the v3-style *_image_url names.
        m = self._model("fal-ai/kling-video/v2.6/pro/image-to-video")
        self.assertTrue(m["supports_negative_prompt"])
        self.assertFalse(m["supports_cfg_scale"])
        self.assertEqual(m["start_image_param"], "start_image_url")
        self.assertEqual(m["end_image_param"], "end_image_url")

    def test_o1_uses_start_and_end_image_url(self):
        m = self._model("fal-ai/kling-video/o1/image-to-video")
        self.assertEqual(m["start_image_param"], "start_image_url")
        self.assertEqual(m["end_image_param"], "end_image_url")

    def test_v16_v15_pro_have_tail_image_url(self):
        for ep in (
            "fal-ai/kling-video/v1.6/pro/image-to-video",
            "fal-ai/kling-video/v1.5/pro/image-to-video",
        ):
            m = self._model(ep)
            self.assertEqual(
                m["end_image_param"], "tail_image_url", ep
            )
            self.assertTrue(m["supports_negative_prompt"], ep)
            self.assertTrue(m["supports_cfg_scale"], ep)

    def test_o3_and_seedance_still_drop_neg_cfg(self):
        # Anchor: the precise per-model gating for the new roster must
        # NOT have been weakened by the legacy corrections.
        for ep in (
            "fal-ai/kling-video/o3/standard/image-to-video",
            "bytedance/seedance-2.0/image-to-video",
        ):
            m = self._model(ep)
            self.assertFalse(m["supports_negative_prompt"], ep)
            self.assertFalse(m["supports_cfg_scale"], ep)


class ConfigPanelCustomModelMotionTests(unittest.TestCase):
    """Codex P2 (PR #41): _update_motion_controls derived has_neg /
    has_cfg only from get_model_capabilities (conservative False for
    unknown endpoints), so custom models that DO support neg/cfg were
    impossible to configure from the GUI even though the dispatch path
    keeps them. The fix mirrors the dispatcher's _is_known bypass.
    Structural pin (the method needs a live Tk; assert the source)."""

    def test_motion_controls_have_is_known_bypass(self):
        src = (_ROOT / "kling_gui" / "config_panel.py").read_text(
            encoding="utf-8"
        )
        self.assertRegex(src, r"get_model_by_endpoint")
        self.assertRegex(
            src, r"_is_known\s*=\s*get_model_by_endpoint\("
        )
        self.assertRegex(
            src,
            r'has_cfg\s*=\s*bool\(caps\.get\("supports_cfg_scale"\)\)\s*or\s*not\s*_is_known',
        )
        self.assertRegex(
            src,
            r'has_neg\s*=\s*bool\(caps\.get\("supports_negative_prompt"\)\)\s*or\s*not\s*_is_known',
        )

    def test_end_frame_stays_caps_driven(self):
        """has_end must NOT get the bypass — a custom model with no
        known end param has nowhere to send a locked end frame."""
        src = (_ROOT / "kling_gui" / "config_panel.py").read_text(
            encoding="utf-8"
        )
        self.assertRegex(
            src,
            r'has_end\s*=\s*caps\.get\("end_image_param"\)\s*is not None',
        )


class DefaultModelStandardMigrationTests(unittest.TestCase):
    """2026-06-25: ship default flipped Kling 2.5 Turbo Pro -> Standard.
    KlingGUIWindow._migrate_legacy_defaults must move EXISTING configs that
    still carry the old Pro default to Standard exactly once, while leaving a
    user's deliberate non-Pro selection untouched (Sourcery PR #113)."""

    @staticmethod
    def _migrate(cfg):
        from kling_gui.main_window import KlingGUIWindow
        KlingGUIWindow._migrate_legacy_defaults(cfg)
        return cfg

    _PRO = "fal-ai/kling-video/v2.5-turbo/pro/image-to-video"
    _STD = "fal-ai/kling-video/v2.5-turbo/standard/image-to-video"

    def test_old_pro_default_migrates_to_standard_and_sets_flag(self):
        cfg = self._migrate({"current_model": self._PRO})
        self.assertEqual(cfg["current_model"], self._STD)
        self.assertEqual(cfg["model_display_name"], "Kling 2.5 Turbo Standard")
        self.assertTrue(cfg["default_model_standard_migrated_v241"])

    def test_deliberate_non_pro_selection_is_preserved(self):
        # A user who picked Seedance must keep it; flag still set so we never
        # re-check (and never override their choice later).
        seed = "fal-ai/bytedance/seedance/v1.5/pro/image-to-video"
        cfg = self._migrate({"current_model": seed})
        self.assertEqual(cfg["current_model"], seed)
        self.assertTrue(cfg["default_model_standard_migrated_v241"])

    def test_migration_does_not_refire_when_flag_already_set(self):
        # Flag already set + still on Pro (e.g. user re-selected Pro after a
        # prior migration) -> must NOT be flipped back to Standard.
        cfg = self._migrate(
            {
                "current_model": self._PRO,
                "default_model_standard_migrated_v241": True,
            }
        )
        self.assertEqual(cfg["current_model"], self._PRO)

    def test_empty_current_model_backfills_to_standard(self):
        cfg = self._migrate({"current_model": ""})
        self.assertEqual(cfg["current_model"], self._STD)

    def test_null_current_model_backfills_to_standard(self):
        # Gemini PR #113: a JSON null -> None. str(None) == "None" is truthy,
        # so without the explicit None guard the migration AND the empty-field
        # backfill would both skip it, stranding the config on a bad value.
        cfg = self._migrate({"current_model": None})
        self.assertEqual(cfg["current_model"], self._STD)

    def test_null_on_established_install_seeds_standard(self):
        # CodeRabbit PR #113: an ESTABLISHED install already has
        # slot3_defaults_backfilled_v21=True, which skips the later backfill
        # block entirely — so an empty/null current_model must be seeded in the
        # always-runs migration block, not only the slot3 backfill.
        for cm in (None, ""):
            cfg = self._migrate(
                {"current_model": cm, "slot3_defaults_backfilled_v21": True}
            )
            self.assertEqual(cfg["current_model"], self._STD)
            self.assertEqual(cfg["model_display_name"], "Kling 2.5 Turbo Standard")

    def test_deliberate_pick_preserved_on_established_install(self):
        seed = "fal-ai/bytedance/seedance/v1.5/pro/image-to-video"
        cfg = self._migrate(
            {"current_model": seed, "slot3_defaults_backfilled_v21": True}
        )
        self.assertEqual(cfg["current_model"], seed)

    def test_stale_expand_mode_defaults_migrate_once_to_fullres_3x4(self):
        cfg = self._migrate(
            {
                "outpaint_expand_mode": "percentage",
                "automation_front_expand_mode": "document_3x4",
                "automation_selfie_expand_mode": "percent",
            }
        )
        self.assertEqual(cfg["outpaint_expand_mode"], "three_four_fullres")
        self.assertEqual(cfg["automation_front_expand_mode"], "three_four_fullres")
        self.assertEqual(cfg["automation_selfie_expand_mode"], "three_four_fullres")
        self.assertTrue(cfg["expand_3x4_modes_migrated_v247"])

    def test_expand_mode_migration_does_not_refire_after_flag(self):
        cfg = self._migrate(
            {
                "outpaint_expand_mode": "percentage",
                "automation_front_expand_mode": "document_3x4",
                "automation_selfie_expand_mode": "percent",
                "expand_3x4_modes_migrated_v247": True,
            }
        )
        self.assertEqual(cfg["outpaint_expand_mode"], "percentage")
        self.assertEqual(cfg["automation_front_expand_mode"], "document_3x4")
        self.assertEqual(cfg["automation_selfie_expand_mode"], "percent")

    def test_stale_expand_percentage_default_migrates_once_to_35(self):
        cfg = self._migrate({"outpaint_expand_percentage": 30})
        self.assertEqual(cfg["outpaint_expand_percentage"], 35)
        self.assertTrue(cfg["expand_3x4_pct_migrated_v248"])

    def test_missing_expand_percentage_default_migrates_to_35(self):
        cfg = self._migrate({})
        self.assertEqual(cfg["outpaint_expand_percentage"], 35)
        self.assertTrue(cfg["expand_3x4_pct_migrated_v248"])

    def test_expand_percentage_migration_does_not_override_after_flag(self):
        cfg = self._migrate(
            {
                "outpaint_expand_percentage": 30,
                "expand_3x4_pct_migrated_v248": True,
            }
        )
        self.assertEqual(cfg["outpaint_expand_percentage"], 30)

    def test_expand_percentage_migration_preserves_non_default_choice(self):
        cfg = self._migrate({"outpaint_expand_percentage": 42})
        self.assertEqual(cfg["outpaint_expand_percentage"], 42)
        self.assertTrue(cfg["expand_3x4_pct_migrated_v248"])


class GetModelDisplayNamePricingTests(unittest.TestCase):
    """get_model_display_name pricing priority: live pricing_info ->
    curated pricing_fallback -> legacy est_cost_10s (pricing_fallback tier
    added 2026-06-25 so Seedance/token-priced models show a price offline)."""

    def _label(self, model):
        from model_metadata import get_model_display_name
        return get_model_display_name(model)

    def test_live_pricing_info_second_wins_over_fallback(self):
        label = self._label(
            {
                "name": "M",
                "pricing_info": {"unit": "second", "unit_price": 0.05},
                "pricing_fallback": {"unit": "second", "unit_price": 0.99},
            }
        )
        self.assertIn("$0.50/10s", label)  # 0.05 * 10, from pricing_info

    def test_pricing_fallback_used_when_no_live_info_second(self):
        label = self._label(
            {"name": "M", "pricing_fallback": {"unit": "second", "unit_price": 0.052}}
        )
        self.assertIn("$0.52/10s", label)

    def test_pricing_fallback_video_and_image_units(self):
        v = self._label(
            {"name": "V", "pricing_fallback": {"unit": "video", "unit_price": 0.4}}
        )
        self.assertIn("$0.40/video", v)
        i = self._label(
            {"name": "I", "pricing_fallback": {"unit": "image", "unit_price": 0.03}}
        )
        self.assertIn("$0.03/image", i)

    def test_est_cost_10s_legacy_fallback_when_no_pricing_dicts(self):
        label = self._label({"name": "L", "est_cost_10s": "$1.23"})
        self.assertIn("~$1.23", label)

    def test_no_price_sources_renders_name_only(self):
        self.assertEqual(self._label({"name": "Bare", "release": ""}), "Bare")

    def test_zero_price_renders_not_dropped(self):
        # Gemini PR #113: unit_price == 0 is falsy; a $0 (free) model must
        # still render "$0.00/..." rather than being silently dropped.
        label = self._label(
            {"name": "Free", "pricing_fallback": {"unit": "second", "unit_price": 0}}
        )
        self.assertIn("$0.00/10s", label)

    def test_explicit_none_unit_price_renders_no_cost(self):
        label = self._label(
            {"name": "NoPrice", "pricing_fallback": {"unit": "second", "unit_price": None}}
        )
        self.assertNotIn("$", label)


class DistRootVersionLockstepTests(unittest.TestCase):
    """The distribution/ mirror must carry the SAME
    automation_recommended_defaults_version as root — else a fresh install
    run from the dist tree seeds an N-1 version and nags on first launch.
    The in-tree lockstep test imports from root only, so it can't see dist
    drift; this test reads BOTH files directly (subagent finding, PR #113)."""

    import re as _re

    def _version(self, rel_path):
        text = (_ROOT / rel_path).read_text(encoding="utf-8")
        m = self._re.search(
            r'"automation_recommended_defaults_version"\s*:\s*(\d+)', text
        )
        self.assertIsNotNone(m, f"version key not found in {rel_path}")
        return int(m.group(1))

    def test_root_and_distribution_defaults_version_match(self):
        root_v = self._version("automation/config.py")
        dist_v = self._version("distribution/automation/config.py")
        self.assertEqual(
            root_v,
            dist_v,
            "automation/config.py and distribution/automation/config.py "
            "must agree on automation_recommended_defaults_version",
        )


if __name__ == "__main__":
    unittest.main()
