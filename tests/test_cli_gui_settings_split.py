"""CLI ⇄ GUI settings split (PR #96 round 2, 2026-06-11).

The CLI automation pipeline and the Tkinter GUI share ONE kling_config.json.
Historically the pipeline read the GUI's own keys (current_model /
model_display_name / current_prompt_slot / video_duration), so changing the
automation model clobbered the GUI selection and vice versa. The split gives
the CLI its own keys (cli_video_model / cli_video_model_display_name /
cli_kling_prompt_slot / cli_video_duration) with fallback to the GUI keys for
configs from before the split.

DELIBERATE NAMING: the cli_* keys must NOT start with "automation_" — the
manifest fingerprints every automation_* key
(automation/manifest.py::_build_config_fingerprint) and the video model is
deliberately non-fingerprinted (resuming a manifest with a different model is
legal — see the --batch --model override comment in kling_automation_ui.py).

Prompt slot CONTENT (saved_prompts / negative_prompts) stays SHARED by design:
editing prompt text in the GUI must reflect in the CLI and vice versa. Only
the slot POINTER and the model selection are per-surface.
"""
from __future__ import annotations

import pytest

from automation.config import (
    AUTOMATION_DEFAULTS,
    resolve_cli_kling_prompt_slot,
    resolve_cli_video_duration,
    resolve_cli_video_model,
)
from kling_automation_ui import (
    KlingAutomationUI,
    RECOMMENDED_DEFAULTS_VERSION,
    RECOMMENDED_KLING_PROMPT_SLOT_1,
)


STANDARD = "fal-ai/kling-video/v2.5-turbo/standard/image-to-video"
PRO = "fal-ai/kling-video/v2.5-turbo/pro/image-to-video"


# --------------------------------------------------------------------------
# Resolvers: fallback to the GUI keys, preference for the cli_* keys
# --------------------------------------------------------------------------

def test_video_model_resolver_falls_back_to_gui_keys():
    cfg = {"current_model": PRO, "model_display_name": "Kling 2.5 Turbo Pro"}
    assert resolve_cli_video_model(cfg) == (PRO, "Kling 2.5 Turbo Pro")


def test_video_model_resolver_prefers_cli_keys():
    cfg = {
        "current_model": PRO,
        "model_display_name": "Kling 2.5 Turbo Pro",
        "cli_video_model": STANDARD,
        "cli_video_model_display_name": "Kling 2.5 Turbo Standard",
    }
    assert resolve_cli_video_model(cfg) == (STANDARD, "Kling 2.5 Turbo Standard")


def test_video_model_resolver_never_borrows_gui_display_for_cli_endpoint():
    """cli endpoint set but no cli display name: the display must NOT fall
    back to the GUI's model_display_name (that names a DIFFERENT model)."""
    cfg = {
        "current_model": PRO,
        "model_display_name": "Kling 2.5 Turbo Pro",
        "cli_video_model": STANDARD,
    }
    endpoint, display = resolve_cli_video_model(cfg)
    assert endpoint == STANDARD
    assert display != "Kling 2.5 Turbo Pro"


def test_video_model_resolver_blank_cli_value_falls_back():
    cfg = {"cli_video_model": "   ", "current_model": PRO}
    assert resolve_cli_video_model(cfg)[0] == PRO


def test_prompt_slot_resolver_fallback_and_preference():
    assert resolve_cli_kling_prompt_slot({"current_prompt_slot": 2}) == 2
    assert resolve_cli_kling_prompt_slot(
        {"current_prompt_slot": 2, "cli_kling_prompt_slot": 6}
    ) == 6
    assert resolve_cli_kling_prompt_slot({}) == 1
    assert resolve_cli_kling_prompt_slot({}, default=4) == 4


def test_prompt_slot_resolver_rejects_garbage_and_out_of_range():
    assert resolve_cli_kling_prompt_slot({"cli_kling_prompt_slot": "abc"}, default=4) == 4
    assert resolve_cli_kling_prompt_slot({"cli_kling_prompt_slot": 99}, default=4) == 4
    assert resolve_cli_kling_prompt_slot({"current_prompt_slot": "7"}) == 7


def test_video_duration_resolver():
    assert resolve_cli_video_duration({"video_duration": 5}) == 5
    assert resolve_cli_video_duration({"video_duration": 5, "cli_video_duration": 10}) == 10
    assert resolve_cli_video_duration({}) == 10
    assert resolve_cli_video_duration({"cli_video_duration": "junk"}) == 10


# --------------------------------------------------------------------------
# Read sites honor the split
# --------------------------------------------------------------------------

def _bare_ui(config: dict) -> KlingAutomationUI:
    ui = KlingAutomationUI.__new__(KlingAutomationUI)
    ui.config = config
    ui.automation_root_folder = config.get("automation_root_folder", "")
    return ui


def test_run_settings_rows_show_cli_model_when_set():
    cfg = dict(AUTOMATION_DEFAULTS)
    cfg.update(
        {
            "current_model": PRO,
            "model_display_name": "Kling 2.5 Turbo Pro",
            "current_prompt_slot": 2,
            "cli_video_model": STANDARD,
            "cli_video_model_display_name": "Kling 2.5 Turbo Standard",
            "cli_kling_prompt_slot": 5,
        }
    )
    ui = _bare_ui(cfg)
    rows = {label: value for label, value, _ in ui._run_settings_rows()}
    assert "Kling 2.5 Turbo Standard" in rows["Video model"]
    assert "slot 5" in rows["Video model"]
    assert "Pro" not in rows["Video model"]


def test_run_settings_rows_fall_back_to_gui_model():
    cfg = dict(AUTOMATION_DEFAULTS)
    cfg.update({"current_model": PRO, "model_display_name": "Kling 2.5 Turbo Pro",
                "current_prompt_slot": 3})
    ui = _bare_ui(cfg)
    rows = {label: value for label, value, _ in ui._run_settings_rows()}
    assert "Kling 2.5 Turbo Pro" in rows["Video model"]
    assert "slot 3" in rows["Video model"]


def test_pipeline_video_factory_uses_cli_model(tmp_path, monkeypatch):
    """The default video_factory must build the generator from the CLI keys
    when present (the GUI's current_model must not leak into automation)."""
    import automation.pipeline as pmod

    captured: dict = {}

    class FakeGen:
        def __init__(self, **kwargs):
            captured.update(kwargs)

    monkeypatch.setattr(pmod, "FalAIKlingGenerator", FakeGen)
    cfg = dict(AUTOMATION_DEFAULTS)
    cfg.update(
        {
            "falai_api_key": "k",
            "automation_root_folder": str(tmp_path),
            "current_model": PRO,
            "model_display_name": "Kling 2.5 Turbo Pro",
            "current_prompt_slot": 2,
            "cli_video_model": STANDARD,
            "cli_video_model_display_name": "Kling 2.5 Turbo Standard",
            "cli_kling_prompt_slot": 4,
        }
    )
    from automation.config import from_app_config
    from automation.manifest import AutomationManifest

    manifest = AutomationManifest.create_or_load(
        tmp_path / "automation_manifest.json", tmp_path, cfg
    )
    runner = pmod.AutoPipelineRunner(cfg, from_app_config(cfg), manifest)
    runner.deps.video_factory()
    assert captured["model_endpoint"] == STANDARD
    assert captured["model_display_name"] == "Kling 2.5 Turbo Standard"
    assert captured["prompt_slot"] == 4


# --------------------------------------------------------------------------
# Silent one-time migration (v<7 -> v7): automation keys fresh, GUI untouched
# --------------------------------------------------------------------------

def _migration_ui(extra: dict) -> tuple[KlingAutomationUI, list]:
    cfg = {
        "automation_recommended_defaults_version": 6,
        "automation_rppg_enabled": False,
        "automation_oldcam_version": ["v24"],
        "automation_selfie_prompts": {"1": "", "3": ""},
        # GUI-owned keys with sentinel values that must survive byte-identical:
        "current_model": "gui/sentinel-model",
        "model_display_name": "GUI Sentinel Model",
        "current_prompt_slot": 2,
        "video_duration": 5,
        "cfg_scale_value": 0.55,
        "lock_end_frame": False,
        "outpaint_composite_mode": "feathered",
        "saved_prompts": {"2": "user authored GUI prompt"},
        "negative_prompts": {},
    }
    cfg.update(extra)
    ui = _bare_ui(cfg)
    saves: list = []
    ui.save_config = lambda: saves.append(True)
    return ui, saves


GUI_SENTINELS = {
    "current_model": "gui/sentinel-model",
    "model_display_name": "GUI Sentinel Model",
    "current_prompt_slot": 2,
    "video_duration": 5,
    "cfg_scale_value": 0.55,
    "lock_end_frame": False,
    "outpaint_composite_mode": "feathered",
}


def test_auto_upgrade_applies_v7_to_automation_keys_only():
    ui, saves = _migration_ui({})
    ui._auto_upgrade_recommended_defaults()
    c = ui.config
    assert c["automation_recommended_defaults_version"] == RECOMMENDED_DEFAULTS_VERSION
    assert c["automation_rppg_enabled"] is True
    assert c["automation_oldcam_version"] == ["v13"]
    assert c["automation_front_expand_provider"] == "fal"
    # CLI per-surface keys seeded with the recommended model/slot:
    assert c["cli_video_model"] == STANDARD
    assert c["cli_video_model_display_name"] == "Kling 2.5 Turbo Standard"
    assert c["cli_kling_prompt_slot"] == 4
    assert c["cli_video_duration"] == 10
    # GUI-owned keys byte-identical:
    for key, sentinel in GUI_SENTINELS.items():
        assert c[key] == sentinel, f"migration must not touch GUI key {key}"
    # User-authored shared prompt text preserved:
    assert c["saved_prompts"]["2"] == "user authored GUI prompt"
    assert saves, "migration must persist"


def test_auto_upgrade_seeds_only_empty_shared_prompt_slots():
    ui, _ = _migration_ui({"saved_prompts": {"4": "MY custom slot 4 text"}})
    ui._auto_upgrade_recommended_defaults()
    c = ui.config
    # Authored slot 4 untouched; empty slot 1 seeded with the recommendation.
    assert c["saved_prompts"]["4"] == "MY custom slot 4 text"
    assert c["saved_prompts"]["1"] == RECOMMENDED_KLING_PROMPT_SLOT_1


def test_auto_upgrade_at_current_version_only_seeds_cli_keys():
    """A config already at the current version must NOT be re-baselined —
    but it gets the cli_* selection seeded from the GUI values once
    (behavior-preserving: the resolvers were falling back to exactly these),
    so the per-surface split applies to pre-split v7 configs too (Codex P2)."""
    ui, saves = _migration_ui({"automation_recommended_defaults_version": RECOMMENDED_DEFAULTS_VERSION})
    ui._auto_upgrade_recommended_defaults()
    c = ui.config
    assert c["cli_video_model"] == "gui/sentinel-model"
    assert c["cli_video_model_display_name"] == "GUI Sentinel Model"
    assert c["cli_kling_prompt_slot"] == 2
    assert c["cli_video_duration"] == 5
    # No baseline reset: the user's automation settings survive.
    assert c["automation_rppg_enabled"] is False
    assert c["automation_oldcam_version"] == ["v24"]
    for key, sentinel in GUI_SENTINELS.items():
        assert c[key] == sentinel
    assert saves


def test_auto_upgrade_is_a_full_noop_when_cli_keys_already_exist():
    ui, saves = _migration_ui(
        {
            "automation_recommended_defaults_version": RECOMMENDED_DEFAULTS_VERSION,
            "cli_video_model": STANDARD,
            "cli_video_model_display_name": "Kling 2.5 Turbo Standard",
            "cli_kling_prompt_slot": 6,
            "cli_video_duration": 10,
        }
    )
    before = dict(ui.config)
    ui._auto_upgrade_recommended_defaults()
    assert ui.config == before
    assert not saves


def test_load_config_missing_version_key_marks_config_for_migration(tmp_path):
    """A config from before recommended-defaults versioning must NOT inherit
    the fresh-install version stamp from default_config — that would mark it
    'already current' and silently skip the one-time migration (Codex P2)."""
    import json

    cfg_file = tmp_path / "kling_config.json"
    cfg_file.write_text(json.dumps({"falai_api_key": "k"}), encoding="utf-8")
    ui = KlingAutomationUI.__new__(KlingAutomationUI)
    ui.config_file = str(cfg_file)
    merged = ui.load_config()
    assert merged["automation_recommended_defaults_version"] == 0


def test_load_config_fresh_install_is_already_current(tmp_path):
    ui = KlingAutomationUI.__new__(KlingAutomationUI)
    ui.config_file = str(tmp_path / "does-not-exist.json")
    merged = ui.load_config()
    assert merged["automation_recommended_defaults_version"] == RECOMMENDED_DEFAULTS_VERSION


def test_load_config_existing_version_value_is_preserved(tmp_path):
    import json

    cfg_file = tmp_path / "kling_config.json"
    cfg_file.write_text(
        json.dumps({"automation_recommended_defaults_version": 6}), encoding="utf-8"
    )
    ui = KlingAutomationUI.__new__(KlingAutomationUI)
    ui.config_file = str(cfg_file)
    merged = ui.load_config()
    assert merged["automation_recommended_defaults_version"] == 6


# --------------------------------------------------------------------------
# Explicit ⭐ reset no longer clobbers the GUI model / slot pointer
# --------------------------------------------------------------------------

def test_apply_recommended_defaults_leaves_gui_model_keys_alone(monkeypatch):
    ui, _ = _migration_ui({})
    ui._confirm = lambda *a, **k: True
    ui.pause_continue = lambda *a, **k: None
    ui._read_max_cases_setting = lambda: "all"
    ui.config["automation_max_cases_per_run"] = "all"
    ui._format_oldcam_versions = lambda *a, **k: "v13"
    ui.save_config = lambda: None
    ui._apply_recommended_automation_defaults()
    c = ui.config
    assert c["current_model"] == "gui/sentinel-model"
    assert c["model_display_name"] == "GUI Sentinel Model"
    assert c["current_prompt_slot"] == 2
    assert c["cli_video_model"] == STANDARD
    assert c["cli_kling_prompt_slot"] == 4


# --------------------------------------------------------------------------
# Compare-with-GUI: rows + one-press adopt (one-directional)
# --------------------------------------------------------------------------

def test_comparison_rows_mark_differs_and_same():
    cfg = dict(AUTOMATION_DEFAULTS)
    cfg.update(
        {
            "current_model": PRO,
            "model_display_name": "Kling 2.5 Turbo Pro",
            "current_prompt_slot": 4,
            "cli_video_model": STANDARD,
            "cli_video_model_display_name": "Kling 2.5 Turbo Standard",
            "cli_kling_prompt_slot": 4,
            "saved_prompts": {"4": "shared text"},
            "negative_prompts": {},
        }
    )
    ui = _bare_ui(cfg)
    rows = {r["id"]: r for r in ui._gui_cli_comparison_rows()}
    assert rows["video_model"]["status"] == "differs"
    assert rows["kling_prompt_slot"]["status"] == "same"
    # Shared rows exist and are flagged shared (single value, both surfaces):
    assert rows["kling_prompt_text"]["status"] == "shared"
    assert rows["cfg_scale"]["status"] == "shared"


def test_adopt_gui_settings_is_one_directional():
    cfg = dict(AUTOMATION_DEFAULTS)
    cfg.update(
        {
            "current_model": PRO,
            "model_display_name": "Kling 2.5 Turbo Pro",
            "current_prompt_slot": 2,
            "video_duration": 5,
            "cli_video_model": STANDARD,
            "cli_video_model_display_name": "Kling 2.5 Turbo Standard",
            "cli_kling_prompt_slot": 6,
        }
    )
    ui = _bare_ui(cfg)
    ui.save_config = lambda: None
    gui_before = {k: cfg[k] for k in ("current_model", "model_display_name", "current_prompt_slot", "video_duration")}
    ui._adopt_gui_settings(["video_model", "kling_prompt_slot"])
    c = ui.config
    assert c["cli_video_model"] == PRO
    assert c["cli_video_model_display_name"] == "Kling 2.5 Turbo Pro"
    assert c["cli_video_duration"] == 5
    assert c["cli_kling_prompt_slot"] == 2
    for key, val in gui_before.items():
        assert c[key] == val, f"adopt must never write GUI key {key}"


def test_adopt_subset_only_touches_selected_rows():
    cfg = dict(AUTOMATION_DEFAULTS)
    cfg.update(
        {
            "current_model": PRO,
            "model_display_name": "Kling 2.5 Turbo Pro",
            "current_prompt_slot": 2,
            "cli_video_model": STANDARD,
            "cli_kling_prompt_slot": 6,
        }
    )
    ui = _bare_ui(cfg)
    ui.save_config = lambda: None
    ui._adopt_gui_settings(["kling_prompt_slot"])
    assert ui.config["cli_kling_prompt_slot"] == 2
    assert ui.config["cli_video_model"] == STANDARD  # untouched


# --------------------------------------------------------------------------
# Per-case front-changed guard (adversarial review M1, round 7)
# --------------------------------------------------------------------------

def _front_guard_fixture(tmp_path):
    import automation.pipeline as pmod
    from automation.discovery import CaseRecord
    from automation.manifest import AutomationManifest
    from automation.config import from_app_config

    cfg = dict(AUTOMATION_DEFAULTS)
    cfg["automation_root_folder"] = str(tmp_path)
    manifest = AutomationManifest.create_or_load(
        tmp_path / "automation_manifest.json", tmp_path, cfg
    )
    case_dir = tmp_path / "u1"
    case_dir.mkdir()
    old_front = case_dir / "front.jpg"
    old_front.write_bytes(b"a")
    new_front = case_dir / "scan-id_photo.jpg"
    new_front.write_bytes(b"b")
    manifest.ensure_case("u1", case_dir, old_front)
    with manifest.lock:
        manifest.data["cases"]["u1"]["status"] = "complete"
    runner = pmod.AutoPipelineRunner(cfg, from_app_config(cfg), manifest)
    return runner, manifest, CaseRecord, case_dir, old_front, new_front


def test_front_changed_guard_resets_completed_case(tmp_path):
    """A front_names/front_globs change that re-selects a DIFFERENT file in
    the same folder must reset the case — its recorded outputs came from
    another source image and 'already complete' would deliver wrong-source
    results (the per-case replacement for the old whole-manifest
    fingerprint rebuild on discovery-key changes)."""
    runner, manifest, CaseRecord, case_dir, _old, new_front = _front_guard_fixture(tmp_path)
    rec = CaseRecord(case_dir=case_dir, front_path=new_front, relative_key="u1")
    runner._reset_case_if_front_changed(rec)
    entry = manifest.data["cases"]["u1"]
    assert entry["status"] == "pending"
    assert entry["front_path"] == str(new_front)
    assert not manifest.case_is_complete_and_valid("u1")


def test_front_changed_guard_noop_when_front_unchanged(tmp_path):
    runner, manifest, CaseRecord, case_dir, old_front, _new = _front_guard_fixture(tmp_path)
    rec = CaseRecord(case_dir=case_dir, front_path=old_front, relative_key="u1")
    runner._reset_case_if_front_changed(rec)
    assert manifest.data["cases"]["u1"]["status"] == "complete"


def test_front_changed_guard_noop_for_brand_new_case(tmp_path):
    runner, manifest, CaseRecord, case_dir, _old, new_front = _front_guard_fixture(tmp_path)
    rec = CaseRecord(case_dir=case_dir, front_path=new_front, relative_key="never-seen")
    runner._reset_case_if_front_changed(rec)  # must not raise or create state
    assert "never-seen" not in manifest.data["cases"]


# --------------------------------------------------------------------------
# Menu structure: grouped main menu + quick-edit coverage (single source lists)
# --------------------------------------------------------------------------

def test_main_menu_choices_grouped_without_duplicates():
    ui = _bare_ui(dict(AUTOMATION_DEFAULTS))
    values = [v for _label, v in ui._main_menu_choice_pairs()]
    # The flattened menu: run actions + settings + tools, each exactly once.
    for expected in ("run", "scan", "quick", "settings", "manual", "gui", "maintenance", "q"):
        assert values.count(expected) == 1, f"expected exactly one {expected!r} entry"
    # Retired duplicates must be gone:
    assert "path" not in values  # root folder now lives in Quick settings
    # Round 3: dry run is a pre-run approval option, not a top-level action.
    assert "dry_run" not in values
    # Every value the group spec references must exist (and vice versa).
    grouped_values = [v for _t, vs in ui._MAIN_MENU_GROUPS for v in vs]
    assert sorted(grouped_values) == sorted(values)


def test_quick_edit_choices_cover_every_field_and_groups_are_consistent():
    ui = _bare_ui(dict(AUTOMATION_DEFAULTS))
    ui.automation_root_folder = ""
    pairs = ui._quick_edit_choice_pairs()
    values = [v for _label, v in pairs]
    # One entry per FIELD (round 3); the prompt entries open the full-text
    # slot BROWSER (round 6) so slot+text merged into one row per kind.
    for expected in ("front_provider", "front_blend", "front_percent", "front_passes", "crop",
                     "selfie_models", "selfie_prompt", "similarity",
                     "sexp_provider", "sexp_blend", "sexp_percent",
                     "video_model", "kling_prompt",
                     "rppg", "loop", "oldcam",
                     "batch_max", "batch_reprocess", "root",
                     "prompts", "all", "done"):
        assert expected in values, f"quick edit lost {expected!r}"
    # The chronological group spec must cover exactly the pairs (a new entry
    # added to one but not the other would silently vanish from the menu).
    grouped_values = [v for _t, vs in ui._QUICK_EDIT_GROUPS for v in vs]
    assert sorted(grouped_values) == sorted(values)
    # Labels carry current values (the editor has no separate table anymore).
    labels = {v: label for label, v in pairs}
    assert "fal" in labels["front_provider"]
    assert "80" in labels["similarity"]


# --------------------------------------------------------------------------
# Menu DISPATCH wiring: every value must reach its handler (a typo'd id in
# the choice list or dispatch chain would otherwise ship a dead menu entry)
# --------------------------------------------------------------------------

def _wired_ui(tmp_path=None):
    ui = _bare_ui(dict(AUTOMATION_DEFAULTS))
    ui.automation_root_folder = str(tmp_path) if tmp_path else ""
    calls: list = []

    def _stub(name):
        return lambda *a, **k: calls.append(name)

    for name in (
        "_run_resume_automation", "_scan_automation_cases", "_dry_run_automation",
        "_quick_edit_settings", "_settings_hub_menu", "_run_manual_kling_menu",
        "launch_gui", "_maintenance_menu", "_select_automation_root",
        "display_header", "_render_run_settings_table",
        "_edit_automation_settings", "configure_advanced_video_settings",
        "configure_api_provider_settings", "_show_full_prompts",
        "_compare_gui_settings_menu", "_apply_recommended_automation_defaults",
        "check_dependencies", "pause_review", "save_config",
    ):
        setattr(ui, name, _stub(name))
    ui._use_legacy_prompt_ui = lambda: False
    return ui, calls


@pytest.mark.parametrize(
    "choice,expected",
    [
        ("scan", "_scan_automation_cases"),
        ("quick", "_quick_edit_settings"),
        ("settings", "_settings_hub_menu"),
        ("manual", "_run_manual_kling_menu"),
        ("gui", "launch_gui"),
        ("maintenance", "_maintenance_menu"),
    ],
)
def test_main_menu_dispatch_reaches_handler(choice, expected):
    ui, calls = _wired_ui()
    ui._q_menu = lambda *a, **k: choice
    result = ui._run_configuration_menu_questionary_iteration()
    assert expected in calls
    assert result is None


def test_main_menu_run_offers_root_picker_when_unset():
    ui, calls = _wired_ui()
    ui._q_menu = lambda *a, **k: "run"
    ui._run_configuration_menu_questionary_iteration()
    assert "_select_automation_root" in calls
    assert "_run_resume_automation" not in calls  # root still unset -> no run


def test_main_menu_run_dispatches_with_root(tmp_path):
    ui, calls = _wired_ui(tmp_path)
    ui._q_menu = lambda *a, **k: "run"
    ui._run_configuration_menu_questionary_iteration()
    assert "_run_resume_automation" in calls


def test_main_menu_quit_exits():
    ui, _calls = _wired_ui()
    ui._q_menu = lambda *a, **k: "q"
    with pytest.raises(SystemExit):
        ui._run_configuration_menu_questionary_iteration()


def test_settings_hub_dispatches_every_entry():
    ui, calls = _wired_ui()
    # Restore the REAL hub under test; everything it calls stays stubbed.
    ui._settings_hub_menu = KlingAutomationUI._settings_hub_menu.__get__(ui, KlingAutomationUI)
    seq = iter(["quick", "all", "advanced", "keys", "prompts", "compare", "reset", "back"])
    ui._q_menu = lambda *a, **k: next(seq)
    ui._settings_hub_menu()
    for expected in (
        "_quick_edit_settings", "_edit_automation_settings",
        "configure_advanced_video_settings", "configure_api_provider_settings",
        "_show_full_prompts", "_compare_gui_settings_menu",
        "_apply_recommended_automation_defaults",
    ):
        assert expected in calls, f"settings hub never dispatched {expected!r}"


def test_maintenance_menu_dispatches_every_entry(tmp_path):
    ui, calls = _wired_ui()
    ui._maintenance_menu = KlingAutomationUI._maintenance_menu.__get__(ui, KlingAutomationUI)
    ui._automation_manifest_path = lambda: tmp_path / "automation_manifest.json"
    seq = iter(["deps", "manifest", "back"])
    ui._q_menu = lambda *a, **k: next(seq)
    ui._maintenance_menu()
    assert "check_dependencies" in calls


def test_quick_editor_toggle_and_done_round_trip():
    ui, calls = _wired_ui()
    ui._quick_edit_settings = KlingAutomationUI._quick_edit_settings.__get__(ui, KlingAutomationUI)
    ui.config["automation_rppg_enabled"] = False
    ui._format_oldcam_versions = lambda *a, **k: "v13"
    ui._read_max_cases_setting = lambda: "5"
    seq = iter(["rppg", "done"])
    ui._q_select = lambda *a, **k: next(seq)
    ui._quick_edit_settings()
    assert ui.config["automation_rppg_enabled"] is True, "toggle must flip the value"
    assert "save_config" in calls


# --------------------------------------------------------------------------
# Dry-run cost estimator (round 8: dry run must EARN its menu slot)
# --------------------------------------------------------------------------

def _cost_ui(models, video_price=0.04, selfie_price=0.08):
    ui = _bare_ui(dict(AUTOMATION_DEFAULTS))
    ui.config["automation_selfie_models"] = models
    ui.config["cli_video_model"] = STANDARD
    ui.config["cli_video_duration"] = 10
    ui.fetch_model_pricing = lambda ep: (
        selfie_price if "banana" in str(ep) else (video_price if "kling" in str(ep) else None)
    )
    return ui


def test_cost_estimate_single_model_math():
    rows = dict(_cost_ui(["fal-ai/nano-banana-2/edit"])._estimate_batch_cost_rows(5))
    assert "$0.08" in rows["Selfie image(s)"]
    assert "$0.40" in rows["Kling video"]  # $0.04/sec × 10s
    assert "≈ $0.48" in rows["Per case"]
    assert "≈ $2.40" in rows["Batch (5 case(s))"]


def test_cost_estimate_fanout_multiplies_video_too():
    """Multi-model fan-out runs one FULL chain per model — the video cost
    multiplies, not just the selfie cost."""
    rows = dict(_cost_ui(["fal-ai/nano-banana-2/edit", "fal-ai/nano-banana-2/edit"])._estimate_batch_cost_rows(1))
    assert "$0.16" in rows["Selfie image(s)"]
    assert "$0.80" in rows["Kling video"]
    assert "≈ $0.96" in rows["Per case"]


def test_cost_estimate_unknown_price_says_so():
    ui = _cost_ui(["openai/gpt-image-2/edit"])  # no price for this one
    rows = dict(ui._estimate_batch_cost_rows(2))
    assert "n/a" in rows["Selfie image(s)"]
    # Incomplete estimate is a LOWER bound, flagged with ≥ not ≈.
    assert "≥" in rows["Per case"]


# --------------------------------------------------------------------------
# ASCII banner constant
# --------------------------------------------------------------------------

def test_banner_ascii_fits_a_79_column_terminal():
    from kling_automation_ui import _BANNER_ASCII

    lines = _BANNER_ASCII.splitlines()
    assert lines, "banner must not be empty"
    assert all(len(line) <= 79 for line in lines), "banner must fit 79 cols"
