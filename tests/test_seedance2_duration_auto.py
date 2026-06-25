"""Regression: a live schema whose ``duration`` enum contains the
non-numeric value ``"auto"`` must NOT crash the video config panel.

Seedance 2.0 (fal-ai ``bytedance/seedance-2.0/image-to-video``, Apr 2026)
is the first model whose OpenAPI ``duration`` enum looks like
``["auto", "4", "5", ... "15"]`` with ``default="auto"``. The old code
built the duration dropdown with ``[f"{int(d)}s" for d in enum]`` and the
capability extractor used ``sorted(int(d) for d in enum)`` — both raised
``ValueError: invalid literal for int() with base 10: 'auto'``.

In ``ConfigPanel.update_parameter_visibility`` that ValueError was caught
by the broad ``except`` and rendered to the user as the misleading
``⚠ Schema fetch failed - check logs`` banner (plus ``Limited params``),
even though the schema fetch itself fully succeeded. The fix filters the
enum to its numeric values so "auto" is dropped from the numeric-only UI.

These tests instantiate ``ConfigPanel`` via ``__new__`` (no Tk) and drive
``update_parameter_visibility`` with fake widgets + a stub schema manager,
asserting the dropdown gets 4s..15s and the diagnostic banner never shows
the failure text. A second test covers ``extract_capabilities`` directly.
"""

import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from model_schema_manager import ModelParameter  # noqa: E402

# The exact duration enum the live fal.ai Seedance 2.0 schema returns.
SEEDANCE2_DURATION_ENUM = [
    "auto", "4", "5", "6", "7", "8", "9", "10", "11", "12", "13", "14", "15",
]


def _seedance2_schema():
    """Param-name -> ModelParameter, mirroring the real Seedance 2.0 schema."""
    return {
        "image_url": ModelParameter("image_url", "string", True, "start frame"),
        "prompt": ModelParameter("prompt", "string", True, "prompt"),
        "duration": ModelParameter(
            "duration", "string", False, "duration",
            default="auto", enum=list(SEEDANCE2_DURATION_ENUM),
        ),
        "aspect_ratio": ModelParameter(
            "aspect_ratio", "string", False, "aspect",
            default="auto", enum=["auto", "21:9", "16:9", "1:1", "9:16"],
        ),
        "resolution": ModelParameter(
            "resolution", "string", False, "res",
            default="720p", enum=["480p", "720p", "1080p", "4k"],
        ),
        "generate_audio": ModelParameter("generate_audio", "boolean", False, "audio", default=True),
        "end_image_url": ModelParameter("end_image_url", "string", False, "end frame"),
    }


class _Var:
    def __init__(self, value=None):
        self._v = value

    def set(self, v):
        self._v = v

    def get(self):
        return self._v


class _RecordingWidget:
    """Stand-in widget that records the kwargs of each .config() call."""

    def __init__(self):
        self.config_calls = []

    def config(self, **kw):
        self.config_calls.append(kw)

    configure = config

    @property
    def last_text(self):
        for call in reversed(self.config_calls):
            if "text" in call:
                return call["text"]
        return None

    @property
    def last_values(self):
        for call in reversed(self.config_calls):
            if "values" in call:
                return call["values"]
        return None


class _StubSchemaManager:
    """Returns the Seedance 2.0 schema regardless of endpoint."""

    def __init__(self, *a, **kw):
        self._schema = _seedance2_schema()

    def get_supported_parameters(self, endpoint):
        return set(self._schema.keys())

    def get_parameter_info(self, endpoint, name):
        return self._schema.get(name)


class SeedanceAutoDurationTest(unittest.TestCase):
    def _make_panel(self):
        import kling_gui.config_panel as cp

        panel = cp.ConfigPanel.__new__(cp.ConfigPanel)
        panel.config = {"falai_api_key": "stub-key", "video_duration": 10}
        panel._resolution_model_aware = True

        panel.duration_combo = _RecordingWidget()
        panel.duration_var = _Var("10s")
        panel.aspect_ratio_combo = _RecordingWidget()
        panel.aspect_ratio_var = _Var("16:9")
        panel.resolution_combo = _RecordingWidget()
        panel.resolution_var = _Var("720p")
        panel.seed_entry = _RecordingWidget()
        panel.random_seed_checkbox = _RecordingWidget()
        panel.camera_fixed_checkbox = _RecordingWidget()
        panel.generate_audio_checkbox = _RecordingWidget()
        panel.video_settings_info = _RecordingWidget()
        panel.schema_diagnostic_label = _RecordingWidget()
        return panel, cp

    def test_auto_duration_does_not_trigger_schema_fetch_failed(self):
        panel, cp = self._make_panel()
        # Patch the lazily-imported collaborators at their source modules.
        import model_schema_manager as msm
        import api_keys
        orig_mgr = msm.ModelSchemaManager
        orig_resolve = api_keys.resolve_api_key
        msm.ModelSchemaManager = _StubSchemaManager
        api_keys.resolve_api_key = lambda cfg, key: "stub-key"
        try:
            panel.update_parameter_visibility("bytedance/seedance-2.0/image-to-video")
        finally:
            msm.ModelSchemaManager = orig_mgr
            api_keys.resolve_api_key = orig_resolve

        # The banner must NOT show the failure text — the crash is gone.
        banner = panel.schema_diagnostic_label.last_text or ""
        self.assertNotIn("Schema fetch failed", banner)

        # Duration dropdown is numeric-only: "auto" dropped, 4s..15s kept.
        values = list(panel.duration_combo.last_values or [])
        self.assertEqual(values, [f"{n}s" for n in range(4, 16)])
        self.assertNotIn("auto", values)
        self.assertNotIn("autos", values)

    def test_extract_capabilities_filters_non_numeric_duration(self):
        import model_schema_manager as msm

        mgr = msm.ModelSchemaManager.__new__(msm.ModelSchemaManager)
        schema = _seedance2_schema()
        mgr.get_model_schema = lambda *a, **kw: schema  # type: ignore[assignment]

        caps = mgr.extract_capabilities("bytedance/seedance-2.0/image-to-video")
        # No ValueError, "auto" filtered, numeric seconds preserved & sorted.
        self.assertEqual(caps["duration_options"], list(range(4, 16)))
        self.assertEqual(caps["resolution_options"], ["480p", "720p", "1080p", "4k"])
        self.assertTrue(caps["supports_audio"])


class DistributionMirrorSyncTest(unittest.TestCase):
    """The fix is mirrored into the committed ``distribution/`` tree. That
    copy uses package-relative imports and can't be imported standalone, so
    instead of duplicating the behavioral test we assert the EAFP duration
    filter is present in BOTH trees — a drift guard (Sourcery suggestion).
    """

    ROOT = Path(__file__).resolve().parent.parent

    def _read(self, rel):
        return (self.ROOT / rel).read_text(encoding="utf-8", errors="replace")

    def test_config_panel_filter_mirrored(self):
        markers = [
            "numeric_durations.append(int(_d))",
            "except (TypeError, ValueError):",
            "numeric_durations.sort()",
        ]
        for tree in ("kling_gui/config_panel.py",
                     "distribution/kling_gui/config_panel.py"):
            src = self._read(tree)
            for m in markers:
                self.assertIn(m, src, f"{tree} missing EAFP duration filter: {m!r}")

    def test_schema_manager_filter_mirrored(self):
        markers = ['_numeric.append(int(_d))', 'caps["duration_options"] = sorted(_numeric)']
        for tree in ("model_schema_manager.py", "distribution/model_schema_manager.py"):
            src = self._read(tree)
            for m in markers:
                self.assertIn(m, src, f"{tree} missing EAFP duration filter: {m!r}")


if __name__ == "__main__":
    unittest.main()
