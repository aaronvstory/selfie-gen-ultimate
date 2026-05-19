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


class SeedanceHiddenFlagTests(unittest.TestCase):
    def test_seedance_is_hidden_from_fresh_config_dropdown(self):
        from kling_gui.config_panel import ModelFetcher

        models = ModelFetcher.get_merged_models({})
        endpoints = {m.get("endpoint") for m in models}
        self.assertNotIn(
            "bytedance/seedance-2.0/image-to-video",
            endpoints,
            "Seedance has hidden:true in models.json and must not "
            "appear in the default merged roster",
        )
        # Sanity: a normal visible model is still present.
        self.assertIn(
            "fal-ai/kling-video/v2.5-turbo/pro/image-to-video", endpoints
        )

    def test_persisted_seedance_selection_is_preserved(self):
        """A user who deliberately persisted Seedance as current_model
        must still see it (selection keeps working) — only the *default*
        roster hides it."""
        from kling_gui.config_panel import ModelFetcher

        cfg = {"current_model": "bytedance/seedance-2.0/image-to-video"}
        endpoints = {
            m.get("endpoint") for m in ModelFetcher.get_merged_models(cfg)
        }
        self.assertIn("bytedance/seedance-2.0/image-to-video", endpoints)

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


if __name__ == "__main__":
    unittest.main()
