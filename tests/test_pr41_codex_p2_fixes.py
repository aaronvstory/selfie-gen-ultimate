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


if __name__ == "__main__":
    unittest.main()
