"""Pre-run MAIN-settings table tests (2026-06-11).

The option-1 flow must surface rPPG state + the EXACT oldcam version list
(+ models/providers/blend modes/passes/crop factor) BEFORE a run is
approved — a real batch run burned by silently running ALL oldcam versions
with NO rPPG. _run_settings_rows is the single source for the Rich table,
the plain headless variant, and the quick editor's re-render.
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from kling_automation_ui import KlingAutomationUI  # noqa: E402


def _make_ui(config=None, root="C:/fake-root"):
    ui = KlingAutomationUI.__new__(KlingAutomationUI)
    ui.config = {
        "automation_rppg_enabled": False,
        "automation_loop_enabled": False,
        "automation_oldcam_version": ["v13"],
        "automation_oldcam_required": True,
        "automation_selfie_models": ["fal-ai/nano-banana-2/edit"],
        "automation_selfie_prompt_slot": 3,
        "automation_selfie_prompts": {"3": "test prompt"},
        "automation_front_expand_provider": "fal",
        "automation_front_expand_mode": "percent",
        "automation_front_expand_composite_mode": "preserve_seamless",
        "automation_front_expand_percent": 70,
        "automation_front_expand_passes": 2,
        "automation_selfie_expand_provider": "fal",
        "automation_selfie_expand_mode": "percent",
        "automation_selfie_expand_composite_mode": "none",
        "automation_selfie_expand_percent": 30,
        "automation_crop_multiplier": 1.5,
        "automation_similarity_threshold": 80,
        "automation_reprocess_mode": "skip",
        "model_display_name": "Kling 2.5 Turbo Standard",
        "current_prompt_slot": 4,
        "saved_prompts": {"4": "video prompt"},
        "negative_prompts": {"4": "negative prompt"},
        **(config or {}),
    }
    ui.automation_root_folder = root
    ui._read_max_cases_setting = lambda: "5"
    ui._resolve_provider = lambda x: "fal" if x in ("fal", "auto") else x
    ui._selfie_model_label_map = lambda: {"fal-ai/nano-banana-2/edit": "Nano Banana 2 Edit"}
    return ui


def _rows_dict(ui):
    return {label: (value, style) for label, value, style in ui._run_settings_rows()}


def test_rppg_off_is_loud_red():
    rows = _rows_dict(_make_ui())
    value, style = rows["rPPG injection"]
    assert "OFF" in value and "no pulse" in value
    assert style == "bold red"


def test_rppg_on_is_green():
    rows = _rows_dict(_make_ui({"automation_rppg_enabled": True}))
    value, style = rows["rPPG injection"]
    assert value == "ON"
    assert style == "bold green"


def test_oldcam_exact_versions_listed():
    rows = _rows_dict(_make_ui({"automation_oldcam_version": ["v13", "v24"]}))
    value, _ = rows["Oldcam versions"]
    assert "v13, v24" in value
    assert "required" in value


def test_oldcam_all_is_expanded_and_flagged_yellow():
    """'all' must show the actual discovered versions (the user pays per
    version) and stand out."""
    ui = _make_ui({"automation_oldcam_version": ["all"]})
    value, style = _rows_dict(ui)["Oldcam versions"]
    assert value.startswith("all (")
    assert "v13" in value  # repo ships v7..v24 — at least v13 discovered
    assert style == "bold yellow"


def test_oldcam_none_selected_is_red():
    ui = _make_ui({"automation_oldcam_version": []})
    value, style = _rows_dict(ui)["Oldcam versions"]
    assert "none selected" in value
    assert style == "bold red"


def test_multi_model_flags_fan_out():
    ui = _make_ui({"automation_selfie_models": [
        "fal-ai/nano-banana-2/edit", "openai/gpt-image-2/edit",
    ]})
    value, style = _rows_dict(ui)["Selfie model(s)"]
    assert "FAN-OUT" in value
    assert style == "bold yellow"


def test_front_expand_row_shows_passes_and_blend():
    rows = _rows_dict(_make_ui())
    value, _ = rows["Step 0 front expand"]
    assert "run 2x" in value
    assert "blend=preserve_seamless" in value
    assert "fal" in value


def test_selfie_expand_row_shows_blend_none():
    rows = _rows_dict(_make_ui())
    value, _ = rows["Step 2.5 selfie expand"]
    assert "blend=none" in value


def test_crop_factor_row_present():
    rows = _rows_dict(_make_ui())
    assert rows["Step 0 crop factor"][0] == "1.5"


def test_plain_variant_prints_all_rows(capsys):
    ui = _make_ui()
    ui._print_run_settings_plain()
    out = capsys.readouterr().out
    assert "rPPG injection" in out
    assert "Oldcam versions" in out
    assert "Loop (ping-pong)" in out
    assert "Video model" in out
    assert "Step 0 front expand" in out
    assert "Step 2.5 selfie expand" in out
