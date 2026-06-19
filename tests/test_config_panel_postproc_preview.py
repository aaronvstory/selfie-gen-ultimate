"""Headless tests for the config-panel post-processing preview + fan-out mode.

Instantiate ConfigPanel via __new__ (no Tk) and exercise the pure preview /
mode methods directly — the same pattern as test_config_panel_ui_height_sync.
"""

import importlib


def _panel(config: dict):
    module = importlib.import_module("kling_gui.config_panel")
    panel = module.ConfigPanel.__new__(module.ConfigPanel)
    panel.config = config
    return panel


def test_preview_powerset_counts_extra_variants():
    panel = _panel({
        "rppg_enabled": True,
        "aa_attacks": ["prime"],
        "oldcam_versions": ["v13"],
        "crush_resolutions": [],
        "loop_videos": False,
        "postproc_fanout_mode": "separate_and_combined",
    })
    line = panel._pipeline_preview_text()
    assert line.startswith("Kling → ")
    assert "powerset" in line
    assert "+ 6 more variants" in line


def test_preview_combined_only_single_chain():
    panel = _panel({
        "rppg_enabled": True,
        "oldcam_versions": ["v13"],
        "aa_attacks": [],
        "crush_resolutions": [],
        "loop_videos": False,
        "postproc_fanout_mode": "combined_only",
    })
    line = panel._pipeline_preview_text()
    assert "more variant" not in line
    assert "(combined only)" in line


def test_preview_empty_when_nothing_enabled():
    panel = _panel({
        "rppg_enabled": False,
        "aa_attacks": [],
        "oldcam_versions": [],
        "crush_resolutions": [],
        "loop_videos": False,
    })
    assert panel._pipeline_preview_text() == "Kling (no post-processing)"


def test_fanout_mode_display_roundtrip():
    module = importlib.import_module("kling_gui.config_panel")
    cls = module.ConfigPanel
    for value, display in cls._FANOUT_DISPLAY.items():
        assert cls._FANOUT_DISPLAY_TO_VALUE[display] == value
