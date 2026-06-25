"""Per-model resolution selection + token-cost estimation (v2.43).

Covers the data model (models.json resolution_options/token_pricing), the
model_metadata helpers + estimate_cost_usd math (pinned to fal's verified
prices), the CLI per-surface resolution resolver, and root/distribution parity.
"""

import json
import sys
import unittest
from pathlib import Path

_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_ROOT))


SEEDANCE_ENDPOINTS = [
    "fal-ai/bytedance/seedance/v1/pro/image-to-video",
    "fal-ai/bytedance/seedance/v1/pro/fast/image-to-video",
    "fal-ai/bytedance/seedance/v1.5/pro/image-to-video",
    "bytedance/seedance-2.0/image-to-video",
    "bytedance/seedance-2.0/fast/image-to-video",
    "bytedance/seedance-2.0/mini/image-to-video",
]


class ResolutionMetadataTests(unittest.TestCase):
    def test_all_seedance_have_resolution_options_and_token_pricing(self):
        from model_metadata import get_resolution_options, get_token_pricing
        for ep in SEEDANCE_ENDPOINTS:
            opts = get_resolution_options(ep)
            self.assertTrue(opts, f"{ep} should have resolution_options")
            self.assertIn("480p", opts)
            self.assertIn("720p", opts)
            self.assertIsNotNone(get_token_pricing(ep), f"{ep} needs token_pricing")

    def test_resolution_caps_are_correct(self):
        from model_metadata import get_resolution_options
        # 2.0 base is the ONLY tier with 4k.
        self.assertIn("4k", get_resolution_options("bytedance/seedance-2.0/image-to-video"))
        # Fast + Mini cap at 720p (no 1080p, no 4k).
        for ep in (
            "bytedance/seedance-2.0/fast/image-to-video",
            "bytedance/seedance-2.0/mini/image-to-video",
        ):
            opts = get_resolution_options(ep)
            self.assertNotIn("1080p", opts)
            self.assertNotIn("4k", opts)
        # v1 family + 1.5 go to 1080p, no 4k.
        for ep in (
            "fal-ai/bytedance/seedance/v1/pro/image-to-video",
            "fal-ai/bytedance/seedance/v1.5/pro/image-to-video",
        ):
            opts = get_resolution_options(ep)
            self.assertIn("1080p", opts)
            self.assertNotIn("4k", opts)

    def test_resolution_defaults_match_fal(self):
        from model_metadata import get_resolution_default
        self.assertEqual(get_resolution_default("fal-ai/bytedance/seedance/v1/pro/image-to-video"), "1080p")
        self.assertEqual(get_resolution_default("fal-ai/bytedance/seedance/v1/pro/fast/image-to-video"), "1080p")
        self.assertEqual(get_resolution_default("fal-ai/bytedance/seedance/v1.5/pro/image-to-video"), "720p")
        self.assertEqual(get_resolution_default("bytedance/seedance-2.0/image-to-video"), "720p")

    def test_non_resolution_models_return_empty(self):
        from model_metadata import get_resolution_options, get_token_pricing
        ep = "fal-ai/kling-video/v2.5-turbo/standard/image-to-video"
        self.assertEqual(get_resolution_options(ep), [])
        self.assertIsNone(get_token_pricing(ep))


class EstimateCostTests(unittest.TestCase):
    """Pinned to the fal-verified table (720p is exact; 480p ~2% soft)."""

    def _cost(self, ep, res, dur, audio=False):
        from model_metadata import estimate_cost_usd
        return estimate_cost_usd(ep, res, dur, audio=audio)

    def test_seedance_2_0_table(self):
        ep = "bytedance/seedance-2.0/image-to-video"
        self.assertAlmostEqual(self._cost(ep, "720p", 10), 3.02, delta=0.05)
        self.assertAlmostEqual(self._cost(ep, "480p", 10), 1.34, delta=0.05)
        self.assertAlmostEqual(self._cost(ep, "1080p", 10), 6.80, delta=0.05)
        # 4k uses the discounted $8/1M rate, not $14.
        self.assertAlmostEqual(self._cost(ep, "4k", 10), 15.55, delta=0.10)

    def test_480p_is_cheaper_than_720p(self):
        ep = "bytedance/seedance-2.0/image-to-video"
        self.assertLess(self._cost(ep, "480p", 10), self._cost(ep, "720p", 10))

    def test_seedance_2_0_fast_and_mini_tiers(self):
        # 2.0 Fast = $11.2/1M (20% cheaper than base $14); Mini = $7/1M.
        # Pins the descending tier ladder base > Fast > Mini so a copy-paste of
        # the base rate onto Fast (the bug code-review caught) can't return.
        fast = "bytedance/seedance-2.0/fast/image-to-video"
        mini = "bytedance/seedance-2.0/mini/image-to-video"
        base = "bytedance/seedance-2.0/image-to-video"
        self.assertAlmostEqual(self._cost(fast, "720p", 10), 2.42, delta=0.05)
        self.assertAlmostEqual(self._cost(mini, "720p", 10), 1.51, delta=0.05)
        # Tier ladder at a fixed res: base > Fast > Mini.
        self.assertGreater(self._cost(base, "720p", 10), self._cost(fast, "720p", 10))
        self.assertGreater(self._cost(fast, "720p", 10), self._cost(mini, "720p", 10))

    def test_1_5_pro_audio_doubles_cost(self):
        ep = "fal-ai/bytedance/seedance/v1.5/pro/image-to-video"
        off = self._cost(ep, "720p", 10, audio=False)
        on = self._cost(ep, "720p", 10, audio=True)
        self.assertAlmostEqual(off, 0.26, delta=0.03)
        self.assertAlmostEqual(on, off * 2, delta=0.01)

    def test_cost_scales_linearly_with_duration(self):
        ep = "bytedance/seedance-2.0/image-to-video"
        c5 = self._cost(ep, "720p", 5)
        c10 = self._cost(ep, "720p", 10)
        self.assertAlmostEqual(c10, c5 * 2, delta=0.02)

    def test_flat_priced_model_returns_none(self):
        self.assertIsNone(self._cost("fal-ai/kling-video/v2.5-turbo/standard/image-to-video", "720p", 10))

    def test_unknown_resolution_returns_none(self):
        # A model with token_pricing but an unmapped res label -> None (caller
        # falls back to flat pricing) rather than a crash.
        self.assertIsNone(self._cost("bytedance/seedance-2.0/image-to-video", "240p", 10))


class CliResolutionResolverTests(unittest.TestCase):
    def test_per_surface_key_wins(self):
        from automation.config import resolve_cli_video_resolution
        cfg = {"cli_video_resolution": "480p", "resolution": "720p"}
        self.assertEqual(resolve_cli_video_resolution(cfg), "480p")

    def test_falls_back_to_shared_resolution(self):
        from automation.config import resolve_cli_video_resolution
        self.assertEqual(resolve_cli_video_resolution({"resolution": "1080p"}), "1080p")

    def test_default_when_neither_present(self):
        from automation.config import resolve_cli_video_resolution
        self.assertEqual(resolve_cli_video_resolution({}), "720p")

    def test_blank_values_fall_through(self):
        from automation.config import resolve_cli_video_resolution
        self.assertEqual(resolve_cli_video_resolution({"cli_video_resolution": "", "resolution": ""}), "720p")

    def test_explicit_null_resolution_returns_default_not_none_string(self):
        # Gemini PR #114: a JSON null -> None; str(None) == "None" is a truthy
        # bogus value. Must return the default, not "None".
        from automation.config import resolve_cli_video_resolution
        self.assertEqual(resolve_cli_video_resolution({"resolution": None}), "720p")
        self.assertEqual(resolve_cli_video_resolution({"cli_video_resolution": None, "resolution": None}), "720p")


class EstimateClampTests(unittest.TestCase):
    """estimate_cost_usd must not quote a price for a resolution the model
    can't produce (CodeRabbit PR #114)."""

    def _cost(self, ep, res, dur=10):
        from model_metadata import estimate_cost_usd
        return estimate_cost_usd(ep, res, dur)

    def test_1080p_on_720p_capped_model_returns_none(self):
        # Fast + Mini cap at 720p.
        self.assertIsNone(self._cost("bytedance/seedance-2.0/fast/image-to-video", "1080p"))
        self.assertIsNone(self._cost("bytedance/seedance-2.0/mini/image-to-video", "1080p"))

    def test_4k_only_on_base_2_0(self):
        self.assertIsNotNone(self._cost("bytedance/seedance-2.0/image-to-video", "4k"))
        self.assertIsNone(self._cost("bytedance/seedance-2.0/fast/image-to-video", "4k"))
        self.assertIsNone(self._cost("fal-ai/bytedance/seedance/v1.5/pro/image-to-video", "4k"))


class RootDistParityTests(unittest.TestCase):
    def test_models_json_resolution_fields_match_root_and_dist(self):
        root = json.loads((_ROOT / "models.json").read_text(encoding="utf-8"))
        dist = json.loads((_ROOT / "distribution" / "models.json").read_text(encoding="utf-8"))

        def res_map(doc):
            return {
                m["endpoint"]: (m.get("resolution_options"), m.get("resolution_default"), m.get("token_pricing"))
                for m in doc["models"]
            }

        self.assertEqual(res_map(root), res_map(dist),
                         "models.json resolution/token_pricing must match root <-> distribution")

    def test_template_has_cli_video_resolution_both(self):
        for rel in ("default_config_template.json", "distribution/default_config_template.json"):
            doc = json.loads((_ROOT / rel).read_text(encoding="utf-8"))
            self.assertEqual(doc.get("cli_video_resolution"), "720p", f"{rel} missing cli_video_resolution")


if __name__ == "__main__":
    unittest.main()
