import pytest
from pathlib import Path

from automation.config import get_outpaint_fal_timeout_seconds, merge_automation_defaults
from kling_automation_ui import KlingAutomationUI


def test_cli_has_automation_menu():
    assert hasattr(KlingAutomationUI, "run_automation_menu")
    assert hasattr(KlingAutomationUI, "_edit_automation_settings_quick")
    assert hasattr(KlingAutomationUI, "_edit_automation_settings")


def test_pause_continue_respects_legacy_flag(monkeypatch):
    ui = KlingAutomationUI.__new__(KlingAutomationUI)
    ui.legacy_pauses = False
    called = {"count": 0}
    monkeypatch.setattr("builtins.input", lambda *args, **kwargs: called.__setitem__("count", called["count"] + 1))
    ui.pause_continue()
    assert called["count"] == 0
    ui.legacy_pauses = True
    ui.pause_continue()
    assert called["count"] == 1


def test_pause_review_always_prompts(monkeypatch):
    ui = KlingAutomationUI.__new__(KlingAutomationUI)
    ui.legacy_pauses = False
    called = {"count": 0}
    monkeypatch.setattr("builtins.input", lambda *args, **kwargs: called.__setitem__("count", called["count"] + 1))
    ui.pause_review()
    assert called["count"] == 1


def test_cli_branding_text_updated():
    src = (Path(__file__).resolve().parent.parent / "kling_automation_ui.py").read_text(encoding="utf-8")
    assert "SELFIE GEN ULTIMATE" in src
    assert "FAL.AI VIDEO GENERATOR" not in src
    assert "keys fal=" in src
    assert "provider=" in src


def test_load_config_defaults_to_kling_standard_and_slot4(tmp_path):
    ui = KlingAutomationUI.__new__(KlingAutomationUI)
    ui.config_file = str(tmp_path / "missing.json")
    cfg = ui.load_config()
    assert cfg["current_model"] == "fal-ai/kling-video/v2.5-turbo/standard/image-to-video"
    assert cfg["model_display_name"] == "Kling 2.5 Turbo Standard"
    assert cfg["current_prompt_slot"] == 4
    # Default slot-1 prompt is now the minimal-motion prompt (PR #2):
    # the end-frame lock does the return-to-pose mechanically, so the
    # prompt only needs to request a subtle slow head movement.
    assert "smallest believable range" in cfg["saved_prompts"]["1"].lower()
    assert "profile view" in cfg["negative_prompts"]["1"].lower()
    assert cfg["cfg_scale_value"] == 0.7
    assert cfg["lock_end_frame"] is True
    merged = merge_automation_defaults(cfg)
    assert merged["automation_selfie_prompt_slot"] == 3
    assert str(merged["automation_selfie_prompts"]["3"]).strip()


def test_dry_run_ignores_corrupt_manifest(tmp_path, monkeypatch, capsys):
    ui = KlingAutomationUI.__new__(KlingAutomationUI)
    ui.config = {"automation_front_names": ["front.png"]}
    ui.automation_root_folder = str(tmp_path)
    ui._automation_manifest_path = lambda: tmp_path / "automation_manifest.json"
    ui.print_red = lambda _x: None
    ui.print_yellow = lambda _x: None
    ui.display_header = lambda: None

    case_dir = tmp_path / "case"
    case_dir.mkdir()
    (case_dir / "front.png").write_bytes(b"x")
    (tmp_path / "automation_manifest.json").write_text("{ bad json", encoding="utf-8")

    monkeypatch.setattr("builtins.input", lambda *args, **kwargs: "")
    ui._dry_run_automation()
    output = capsys.readouterr().out
    assert "Warning: existing manifest unreadable or schema-mismatched" in output


def test_dry_run_uses_collect_case_snapshot_counts(tmp_path, monkeypatch, capsys):
    ui = KlingAutomationUI.__new__(KlingAutomationUI)
    ui.config = {"automation_front_names": ["front.png"]}
    ui.automation_root_folder = str(tmp_path)
    ui._automation_manifest_path = lambda: tmp_path / "automation_manifest.json"
    ui.print_red = lambda _x: None
    ui.print_yellow = lambda _x: None
    ui.display_header = lambda: None

    case_dir = tmp_path / "case"
    case_dir.mkdir()
    (case_dir / "front.png").write_bytes(b"x")

    monkeypatch.setattr(
        ui,
        "_collect_case_snapshot",
        lambda records, manifest: (
            [],
                {
                    "discovered": len(records),
                    "completed_total": 1,
                    "skipped_complete": 1,
                    "pending": 2,
                "manual_review": 3,
                "failed": 4,
                "existing_videos_selfies": 0,
                "will_run": 2,
            },
            records[:1],
        ),
    )
    monkeypatch.setattr("builtins.input", lambda *args, **kwargs: "")
    ui._dry_run_automation()
    output = capsys.readouterr().out
    assert "completed total: 1" in output
    assert "skipped complete: 1" in output
    assert "pending: 2" in output
    assert "failed/manual_review: 7" in output


def test_settings_editor_updates_selected_values(tmp_path, monkeypatch):
    ui = KlingAutomationUI.__new__(KlingAutomationUI)
    ui.config = {
        "automation_manifest_name": "automation_manifest.json",
        "automation_skip_completed": True,
        "automation_skip_if_selfie_exists": True,
        "automation_skip_if_video_exists": True,
        "automation_allow_reprocess": False,
        "automation_reprocess_mode": "skip",
        "automation_front_expand_enabled": True,
        "automation_front_expand_provider": "bfl",
        "automation_front_expand_mode": "percent",
        "automation_front_expand_composite_mode": "preserve_seamless",
        "automation_front_expand_percent": 30,
        "automation_front_expand_passes": 1,
        "automation_front_edge_seal_enabled": False,
        "automation_front_edge_seal_px": 12,
        "automation_front_output_name": "front-expanded.png",
        "automation_extract_enabled": True,
        "automation_extract_output_name": "extracted.png",
        "automation_crop_multiplier": 1.5,
        "automation_selfie_enabled": True,
        "automation_selfie_models": ["fal-ai/nano-banana-2/edit"],
        "automation_selfie_model_policy": "first_pass",
        "automation_selfie_max_attempts_per_model": 1,
        "automation_similarity_threshold": 80,
        "automation_selfie_expand_enabled": True,
        "automation_selfie_expand_provider": "bfl",
        "automation_selfie_expand_mode": "percent",
        "automation_selfie_expand_composite_mode": "preserve_seamless",
        "automation_selfie_expand_percent": 30,
        "automation_video_enabled": True,
        "automation_video_aspect_ratio": "3:4",
        "automation_video_use_existing_prompt": True,
        "automation_oldcam_enabled": True,
        "automation_oldcam_version": "all",
        "automation_oldcam_required": True,
    }
    ui.automation_root_folder = str(tmp_path)
    ui.print_red = lambda _x: None
    ui.save_config = lambda: None

    responses = iter(
        [
            str(tmp_path),  # root
            "",  # manifest
            "5",  # max cases
            "", "", "",  # skip toggles
            "y",  # allow reprocess
            "increment",  # mode
            "", "", "", "",  # front enabled/provider/mode/composite
            "40",  # front pct
            "2",  # front passes
            "", "", "",  # edge + output
            "", "", "", "",  # extract/crop/selfie enabled/models
            "", "",  # model policy/attempts
            "85",  # threshold
            "", "", "", "", "",  # selfie expand settings
            "", "", "", "",  # video + oldcam
            "",  # oldcam required
            "",  # one extra keep for final boolean/input alignment
            "",  # final pause
        ]
    )
    def _next_or_blank(*args, **kwargs):
        try:
            return next(responses)
        except StopIteration:
            return ""

    monkeypatch.setattr("builtins.input", _next_or_blank)
    ui._edit_automation_settings_quick()
    assert ui.config["automation_reprocess_mode"] == "increment"
    assert ui.config["automation_front_expand_percent"] == 40
    assert ui.config["automation_front_expand_passes"] == 2
    assert ui.config["automation_max_cases_per_run"] == "5"


def test_manifest_path_sanitizes_name(tmp_path):
    ui = KlingAutomationUI.__new__(KlingAutomationUI)
    ui.automation_root_folder = str(tmp_path)
    ui.config = {"automation_manifest_name": "../escape.json"}
    manifest_path = ui._automation_manifest_path()
    assert manifest_path == tmp_path / "escape.json"


def test_dry_run_handles_missing_root(monkeypatch):
    ui = KlingAutomationUI.__new__(KlingAutomationUI)
    ui.config = {"automation_front_names": ["front.png"]}
    ui.automation_root_folder = "Z:/definitely_missing_path_for_test"
    ui.print_red = lambda _x: None
    monkeypatch.setattr("builtins.input", lambda *args, **kwargs: "")
    ui._dry_run_automation()


def test_settings_editor_rejects_invalid_max_cases(tmp_path, monkeypatch):
    ui = KlingAutomationUI.__new__(KlingAutomationUI)
    ui.config = {
        "automation_manifest_name": "automation_manifest.json",
        "automation_max_cases_per_run": "5",
        "automation_skip_completed": True,
        "automation_skip_if_selfie_exists": True,
        "automation_skip_if_video_exists": True,
        "automation_allow_reprocess": False,
        "automation_reprocess_mode": "skip",
        "automation_front_expand_enabled": True,
        "automation_front_expand_provider": "bfl",
        "automation_front_expand_mode": "percent",
        "automation_front_expand_percent": 30,
        "automation_front_expand_passes": 2,
        "automation_front_edge_seal_enabled": False,
        "automation_front_edge_seal_px": 12,
        "automation_front_output_name": "front-expanded.png",
        "automation_extract_enabled": True,
        "automation_extract_output_name": "extracted.png",
        "automation_crop_multiplier": 1.5,
        "automation_selfie_enabled": True,
        "automation_selfie_models": ["fal-ai/nano-banana-2/edit"],
        "automation_selfie_model_policy": "first_pass",
        "automation_selfie_max_attempts_per_model": 1,
        "automation_similarity_threshold": 80,
        "automation_selfie_expand_enabled": True,
        "automation_selfie_expand_provider": "bfl",
        "automation_selfie_expand_mode": "percent",
        "automation_selfie_expand_percent": 30,
        "automation_video_enabled": True,
        "automation_video_aspect_ratio": "3:4",
        "automation_video_use_existing_prompt": True,
        "automation_oldcam_enabled": True,
        "automation_oldcam_version": "all",
        "automation_oldcam_required": True,
    }
    ui.automation_root_folder = str(tmp_path)
    ui.print_red = lambda _x: None
    ui.save_config = lambda: None
    responses = iter([str(tmp_path), "", "8"] + [""] * 40)
    monkeypatch.setattr("builtins.input", lambda *args, **kwargs: next(responses, ""))
    ui._edit_automation_settings()
    assert ui.config["automation_max_cases_per_run"] == "5"


def test_collect_case_snapshot_applies_max_cases_after_filters(tmp_path):
    ui = KlingAutomationUI.__new__(KlingAutomationUI)
    ui.config = {
        "automation_front_names": ["front.png"],
        "automation_skip_if_selfie_exists": True,
        "automation_skip_if_video_exists": True,
        "automation_max_cases_per_run": "1",
    }
    ui._read_max_cases_setting = lambda: "1"
    root = tmp_path
    for name in ("a", "b", "c"):
        case_dir = root / name
        case_dir.mkdir(parents=True)
        (case_dir / "front.png").write_bytes(b"x")
    (root / "a" / "gen-videos").mkdir()
    (root / "a" / "gen-videos" / "x.mp4").write_bytes(b"x")
    records = [
        type("Rec", (), {"relative_key": "a", "front_path": root / "a" / "front.png", "case_dir": root / "a"}),
        type("Rec", (), {"relative_key": "b", "front_path": root / "b" / "front.png", "case_dir": root / "b"}),
        type("Rec", (), {"relative_key": "c", "front_path": root / "c" / "front.png", "case_dir": root / "c"}),
    ]
    rows, counts, runnable = ui._collect_case_snapshot(records, manifest=None)
    assert len(rows) == 3
    assert counts["pending"] == 3
    assert counts["will_run"] == 1
    assert len(runnable) == 1


def test_collect_case_snapshot_existing_video_still_runnable(tmp_path):
    ui = KlingAutomationUI.__new__(KlingAutomationUI)
    ui.config = {
        "automation_front_names": ["front.png"],
        "automation_skip_if_selfie_exists": True,
        "automation_skip_if_video_exists": True,
        "automation_max_cases_per_run": "all",
    }
    ui._read_max_cases_setting = lambda: "all"
    root = tmp_path
    case_dir = root / "a"
    (case_dir / "gen-videos").mkdir(parents=True)
    (case_dir / "front.png").write_bytes(b"x")
    (case_dir / "gen-videos" / "x.mp4").write_bytes(b"x")
    record = type("Rec", (), {"relative_key": "a", "front_path": case_dir / "front.png", "case_dir": case_dir})

    _rows, counts, runnable = ui._collect_case_snapshot([record], manifest=None)
    assert counts["pending"] == 1
    assert len(runnable) == 1


def test_main_menu_path_input_sets_root_and_scans(tmp_path, monkeypatch):
    ui = KlingAutomationUI.__new__(KlingAutomationUI)
    ui.config = {}
    ui.automation_root_folder = ""
    ui.save_config = lambda: None
    ui.display_header = lambda: None
    ui.display_configuration_menu = lambda: None
    ui.print_red = lambda _x: None
    ui.print_yellow = lambda _x: None
    called = {"scan": False}
    ui._scan_automation_cases = lambda: called.__setitem__("scan", True)
    responses = iter([str(tmp_path), "q"])
    monkeypatch.setattr("builtins.input", lambda *args, **kwargs: next(responses))
    with pytest.raises(SystemExit):
        ui.run_configuration_menu()
    assert called["scan"] is True
    assert ui.automation_root_folder == str(tmp_path)


def test_select_automation_root_browse_primary(tmp_path, monkeypatch):
    ui = KlingAutomationUI.__new__(KlingAutomationUI)
    ui.config = {}
    ui.automation_root_folder = ""
    ui.save_config = lambda: None
    ui.print_red = lambda _x: None
    ui.print_yellow = lambda _x: None
    called = {"scan": False}
    ui._scan_automation_cases = lambda: called.__setitem__("scan", True)
    responses = iter([""])
    monkeypatch.setattr("builtins.input", lambda *args, **kwargs: next(responses))
    monkeypatch.setattr("kling_automation_ui.select_directory_cli_safe", lambda **kwargs: str(tmp_path))
    ui._select_automation_root()
    assert ui.automation_root_folder == str(tmp_path)
    assert called["scan"] is True


def test_select_automation_root_typed_quotes(tmp_path, monkeypatch):
    ui = KlingAutomationUI.__new__(KlingAutomationUI)
    ui.config = {}
    ui.automation_root_folder = ""
    ui.save_config = lambda: None
    ui.print_red = lambda _x: None
    ui.print_yellow = lambda _x: None
    responses = iter(["2", f"\"{tmp_path}\"", ""])
    monkeypatch.setattr("builtins.input", lambda *args, **kwargs: next(responses))
    ui._select_automation_root()
    assert ui.automation_root_folder == str(tmp_path)


def test_select_automation_root_browse_fallback_to_typed(tmp_path, monkeypatch):
    ui = KlingAutomationUI.__new__(KlingAutomationUI)
    ui.config = {}
    ui.automation_root_folder = ""
    ui.save_config = lambda: None
    ui.print_red = lambda _x: None
    ui.print_yellow = lambda _x: None
    responses = iter(["", str(tmp_path), ""])
    monkeypatch.setattr("builtins.input", lambda *args, **kwargs: next(responses))

    def _raise(**kwargs):
        raise RuntimeError("tk unavailable")

    monkeypatch.setattr("kling_automation_ui.select_directory_cli_safe", _raise)
    ui._select_automation_root()
    assert ui.automation_root_folder == str(tmp_path)


def test_select_automation_root_logs_browse_fallback(tmp_path, monkeypatch):
    ui = KlingAutomationUI.__new__(KlingAutomationUI)
    ui.config = {}
    ui.automation_root_folder = ""
    ui.save_config = lambda: None
    ui.print_red = lambda _x: None
    ui.print_yellow = lambda _x: None
    ui._scan_automation_cases = lambda: None
    responses = iter(["", str(tmp_path)])
    monkeypatch.setattr("builtins.input", lambda *args, **kwargs: next(responses))

    info_logs = []
    warning_logs = []
    monkeypatch.setattr("kling_automation_ui.logging.info", lambda message, *args, **kwargs: info_logs.append(message))
    monkeypatch.setattr("kling_automation_ui.logging.warning", lambda message, *args, **kwargs: warning_logs.append(message))

    def _raise(**kwargs):
        raise RuntimeError("tk unavailable")

    monkeypatch.setattr("kling_automation_ui.select_directory_cli_safe", _raise)
    ui._select_automation_root()

    assert any("automation_root_picker_browse_attempt" in entry for entry in info_logs)
    assert any("automation_root_typed_fallback_prompt" in entry for entry in info_logs)
    assert any("automation_root_picker_browse_error" in entry for entry in warning_logs)


def test_collect_case_snapshot_respects_skip_completed_false(tmp_path):
    ui = KlingAutomationUI.__new__(KlingAutomationUI)
    ui.config = {
        "automation_front_names": ["front.png"],
        "automation_skip_completed": False,
        "automation_skip_if_selfie_exists": False,
        "automation_skip_if_video_exists": False,
        "automation_max_cases_per_run": "all",
    }
    ui._read_max_cases_setting = lambda: "all"
    root = tmp_path
    case_dir = root / "a"
    case_dir.mkdir(parents=True)
    (case_dir / "front.png").write_bytes(b"x")
    record = type("Rec", (), {"relative_key": "a", "front_path": case_dir / "front.png", "case_dir": case_dir})
    manifest = type(
        "M",
        (),
        {
            "data": {"cases": {"a": {"status": "complete"}}},
            "case_is_complete_and_valid": lambda self, _k: True,
        },
    )()
    _rows, counts, runnable = ui._collect_case_snapshot([record], manifest=manifest)
    assert counts["skipped_complete"] == 0
    assert counts["pending"] == 1
    assert len(runnable) == 1


def test_collect_case_snapshot_manual_review_similarity_unavailable_is_retryable(tmp_path):
    ui = KlingAutomationUI.__new__(KlingAutomationUI)
    ui.config = {
        "automation_front_names": ["front.png"],
        "automation_skip_completed": True,
        "automation_skip_if_selfie_exists": True,
        "automation_skip_if_video_exists": True,
        "automation_max_cases_per_run": "all",
    }
    ui._read_max_cases_setting = lambda: "all"
    case_dir = tmp_path / "a"
    case_dir.mkdir(parents=True)
    (case_dir / "front.png").write_bytes(b"x")
    record = type("Rec", (), {"relative_key": "a", "front_path": case_dir / "front.png", "case_dir": case_dir})
    manifest = type(
        "M",
        (),
        {
            "data": {
                "cases": {
                    "a": {
                        "status": "manual_review",
                        "steps": {"similarity_gate": {"error": "similarity unavailable: backend error"}},
                    }
                }
            },
            "case_is_complete_and_valid": lambda self, _k: False,
        },
    )()
    _rows, counts, runnable = ui._collect_case_snapshot([record], manifest=manifest)
    assert counts["pending"] == 1
    assert counts["manual_review"] == 0
    assert len(runnable) == 1


def test_collect_case_snapshot_manual_review_low_similarity_stays_manual_review(tmp_path):
    ui = KlingAutomationUI.__new__(KlingAutomationUI)
    ui.config = {
        "automation_front_names": ["front.png"],
        "automation_skip_completed": True,
        "automation_skip_if_selfie_exists": True,
        "automation_skip_if_video_exists": True,
        "automation_max_cases_per_run": "all",
    }
    ui._read_max_cases_setting = lambda: "all"
    case_dir = tmp_path / "a"
    case_dir.mkdir(parents=True)
    (case_dir / "front.png").write_bytes(b"x")
    record = type("Rec", (), {"relative_key": "a", "front_path": case_dir / "front.png", "case_dir": case_dir})
    manifest = type(
        "M",
        (),
        {
            "data": {
                "cases": {
                    "a": {
                        "status": "manual_review",
                        "steps": {"similarity_gate": {"error": "similarity 72 below threshold 80"}},
                    }
                }
            },
            "case_is_complete_and_valid": lambda self, _k: False,
        },
    )()
    _rows, counts, runnable = ui._collect_case_snapshot([record], manifest=manifest)
    assert counts["pending"] == 0
    assert counts["manual_review"] == 1
    assert len(runnable) == 0


def test_settings_editor_selfie_model_menu_maps_to_both(tmp_path, monkeypatch):
    ui = KlingAutomationUI.__new__(KlingAutomationUI)
    ui.config = {
        "automation_manifest_name": "automation_manifest.json",
        "automation_max_cases_per_run": "5",
        "automation_skip_completed": True,
        "automation_skip_if_selfie_exists": True,
        "automation_skip_if_video_exists": True,
        "automation_allow_reprocess": False,
        "automation_reprocess_mode": "skip",
        "automation_front_expand_enabled": True,
        "automation_front_expand_provider": "bfl",
        "automation_front_expand_mode": "percent",
        "automation_front_expand_percent": 30,
        "automation_front_expand_passes": 2,
        "automation_front_edge_seal_enabled": False,
        "automation_front_edge_seal_px": 12,
        "automation_front_output_name": "front-expanded.png",
        "automation_extract_enabled": True,
        "automation_extract_output_name": "extracted.png",
        "automation_crop_multiplier": 1.5,
        "automation_selfie_enabled": True,
        "automation_selfie_models": ["fal-ai/nano-banana-2/edit"],
        "automation_selfie_model_policy": "first_pass",
        "automation_selfie_max_attempts_per_model": 1,
        "automation_similarity_threshold": 80,
        "automation_selfie_expand_enabled": True,
        "automation_selfie_expand_provider": "bfl",
        "automation_selfie_expand_mode": "percent",
        "automation_selfie_expand_percent": 30,
        "automation_video_enabled": True,
        "automation_video_aspect_ratio": "3:4",
        "automation_video_use_existing_prompt": True,
        "automation_oldcam_enabled": True,
        "automation_oldcam_version": "all",
        "automation_oldcam_required": True,
    }
    ui.automation_root_folder = str(tmp_path)
    ui.print_red = lambda _x: None
    ui.save_config = lambda: None

    def _input_router(prompt="", *args, **kwargs):
        if "Choose model set" in prompt:
            return "3"
        if "Similarity threshold" in prompt:
            return "80"
        if "Press Enter" in prompt:
            return ""
        if "Automation root path" in prompt:
            return str(tmp_path)
        return ""

    monkeypatch.setattr("builtins.input", _input_router)
    ui._edit_automation_settings()
    assert ui.config["automation_selfie_models"] == ["fal-ai/nano-banana-2/edit", "openai/gpt-image-2/edit"]


def test_apply_recommended_automation_defaults_updates_stale_config(tmp_path, monkeypatch):
    ui = KlingAutomationUI.__new__(KlingAutomationUI)
    ui.config = {
        "automation_front_expand_provider": "fal",
        "automation_front_expand_mode": "document_3x4",
        "automation_front_expand_composite_mode": "hard",
        "automation_front_expand_percent": 22,
        "automation_front_expand_passes": 1,
        "automation_front_edge_seal_enabled": True,
        "automation_selfie_expand_provider": "fal",
        "automation_selfie_expand_mode": "centered_3x4",
        "automation_selfie_expand_composite_mode": "feathered",
        "automation_selfie_expand_percent": 25,
        "automation_selfie_expand_edge_seal_enabled": True,
        "automation_selfie_models": ["openai/gpt-image-2/edit"],
        "automation_selfie_prompt_slot": 3,
        "automation_selfie_prompts": {"1": "", "3": "legacy"},
        "current_model": "fal-ai/kling-video/v2.5-turbo/pro/image-to-video",
        "model_display_name": "Kling 2.5 Turbo Pro",
        "current_prompt_slot": 2,
        "saved_prompts": {"1": "", "2": "legacy video prompt"},
        "automation_similarity_threshold": 75,
        "automation_video_enabled": False,
        "automation_oldcam_enabled": False,
        "automation_oldcam_version": "v8",
        "automation_oldcam_required": False,
        "automation_max_cases_per_run": "all",
        "falai_api_key": "keep-fal-key",
        "bfl_api_key": "keep-bfl-key",
        "automation_root_folder": str(tmp_path),
    }
    ui.automation_root_folder = str(tmp_path)
    ui._read_max_cases_setting = lambda: str(ui.config.get("automation_max_cases_per_run", ""))
    ui._ensure_selfie_prompt_slots = KlingAutomationUI._ensure_selfie_prompt_slots.__get__(ui, KlingAutomationUI)
    saved = {"count": 0}
    ui.save_config = lambda: saved.__setitem__("count", saved["count"] + 1)
    monkeypatch.setattr("builtins.input", lambda *args, **kwargs: "")

    ui._apply_recommended_automation_defaults()
    assert saved["count"] == 1
    # v7 (Codex P2, PR #96): the recommended baseline resets EVERY
    # behavior-affecting stage toggle.
    assert ui.config["automation_front_expand_enabled"] is True
    assert ui.config["automation_extract_enabled"] is True
    assert ui.config["automation_selfie_enabled"] is True
    assert ui.config["automation_selfie_expand_enabled"] is True
    assert ui.config["automation_rppg_per_oldcam_fanout"] is False
    # v7 (2026-06-11): provider fal for BOTH expand steps ("fal.ai for
    # everything", user mandate).
    assert ui.config["automation_front_expand_provider"] == "fal"
    assert ui.config["automation_front_expand_mode"] == "percent"
    assert ui.config["automation_front_expand_composite_mode"] == "preserve_seamless"
    assert ui.config["automation_front_expand_percent"] == 70
    assert ui.config["automation_front_expand_passes"] == 2
    assert ui.config["automation_selfie_expand_provider"] == "fal"
    assert ui.config["automation_selfie_expand_mode"] == "percent"
    # Ship default for Step 2.5 selfie expand is "none" (raw AI output)
    # — see automation/config.py + default_config_template.json + the
    # release_prep force-override. apply_recommended_defaults must
    # match (CodeRabbit, PR #41).
    assert ui.config["automation_selfie_expand_composite_mode"] == "none"
    assert ui.config["automation_selfie_expand_percent"] == 30
    assert ui.config["automation_selfie_models"] == ["fal-ai/nano-banana-2/edit"]
    assert ui.config["automation_selfie_prompt_slot"] == 3
    assert "parked car" in ui.config["automation_selfie_prompts"]["1"].lower()
    assert "parked car" in ui.config["automation_selfie_prompts"]["3"].lower()
    # Per-surface split (PR #96 round 2): the recommended model/slot land on
    # the CLI's own keys; the GUI's selection (current_model / display /
    # current_prompt_slot) must survive byte-identical.
    assert ui.config["cli_video_model"] == "fal-ai/kling-video/v2.5-turbo/standard/image-to-video"
    assert ui.config["cli_video_model_display_name"] == "Kling 2.5 Turbo Standard"
    assert ui.config["cli_kling_prompt_slot"] == 4
    assert ui.config["current_model"] == "fal-ai/kling-video/v2.5-turbo/pro/image-to-video"
    assert ui.config["model_display_name"] == "Kling 2.5 Turbo Pro"
    assert ui.config["current_prompt_slot"] == 2
    # Recommended-defaults now seed the minimal-motion prompt + negative
    # + motion knobs (PR #2).
    assert "smallest believable range" in ui.config["saved_prompts"]["1"].lower()
    assert "profile view" in ui.config["negative_prompts"]["1"].lower()
    assert ui.config["cfg_scale_value"] == 0.7
    assert ui.config["lock_end_frame"] is True
    assert ui.config["automation_similarity_threshold"] == 80
    assert ui.config["automation_video_enabled"] is True
    assert ui.config["automation_oldcam_enabled"] is True
    # v7: oldcam v13 only, canonical multi-select list form.
    assert ui.config["automation_oldcam_version"] == ["v13"]
    assert ui.config["automation_oldcam_required"] is True
    # v7: loop is a real pipeline step now; recommended baseline ships OFF.
    assert ui.config["automation_loop_enabled"] is False
    # v7 (user decision 2026-06-11): the recommended GATE is ON — "rPPG +
    # oldcam v13 = best results"; a real batch run burned because rPPG
    # silently stayed off. The iterative-mode baseline (PR #43) still
    # applies. The GUI's own opt-in default is unaffected.
    assert ui.config["automation_rppg_enabled"] is True
    assert ui.config["automation_rppg_mode"] == "iterative"
    assert ui.config["automation_rppg_iterate_from_baseline"] is True
    assert ui.config["automation_rppg_skip_diagnosis"] is True
    assert ui.config["automation_rppg_skip_kinematic_gate"] is True
    # Round-3 subagent CRITICAL on PR #54: the apply-defaults function
    # MUST write stride=1 so the v5 -> v6 migration actually flips the
    # knob it was bumped for. Without this assertion the prior version-
    # bump test passed but the stride stayed at whatever the user had
    # saved (3 for v5 users from the v2.5 speedup pass).
    assert ui.config["automation_rppg_landmark_stride"] == 1, (
        "v6 apply-defaults must reset landmark_stride to 1 — that's "
        "the entire reason the version bumped from 5 to 6"
    )
    # Version bumped 6 -> 7 in the 2026-06-11 CLI UX overhaul (rPPG ON,
    # oldcam ["v13"], loop OFF, fal everywhere). Drives the "apply
    # recommended defaults" yellow prompt for v6 users.
    from kling_automation_ui import RECOMMENDED_DEFAULTS_VERSION
    assert RECOMMENDED_DEFAULTS_VERSION == 7
    assert ui.config["automation_recommended_defaults_version"] == 7
    # Round-3 review fix (PR #54): catch the lockstep miss where
    # `kling_automation_ui.RECOMMENDED_DEFAULTS_VERSION` was bumped
    # but `automation.config.AUTOMATION_DEFAULTS` stayed stale. Both
    # constants must agree — a brand-new user (whose initial config
    # is seeded from AUTOMATION_DEFAULTS) would otherwise immediately
    # trip the CLI's "apply recommended defaults?" prompt because the
    # seeded value would be N-1 of the target. Asserting they match
    # here locks the synchronization invariant.
    from automation.config import AUTOMATION_DEFAULTS
    assert AUTOMATION_DEFAULTS["automation_recommended_defaults_version"] == \
        RECOMMENDED_DEFAULTS_VERSION, (
        "automation.config.AUTOMATION_DEFAULTS and "
        "kling_automation_ui.RECOMMENDED_DEFAULTS_VERSION must stay "
        "in lockstep — a fresh-install config would otherwise immediately "
        "trip the 'apply defaults?' prompt because the seeded baseline "
        "is N-1 of the target."
    )
    assert ui.config["automation_max_cases_per_run"] == "all"
    assert ui.config["falai_api_key"] == "keep-fal-key"
    assert ui.config["bfl_api_key"] == "keep-bfl-key"
    assert ui.config["automation_root_folder"] == str(tmp_path)


def test_apply_recommended_automation_defaults_sets_max_cases_to_1_if_invalid(tmp_path, monkeypatch):
    ui = KlingAutomationUI.__new__(KlingAutomationUI)
    ui.config = {
        "automation_max_cases_per_run": "bad",
        "automation_selfie_prompts": {"1": ""},
        "saved_prompts": {"1": ""},
    }
    ui.automation_root_folder = str(tmp_path)
    ui._read_max_cases_setting = KlingAutomationUI._read_max_cases_setting.__get__(ui, KlingAutomationUI)
    ui._ensure_selfie_prompt_slots = KlingAutomationUI._ensure_selfie_prompt_slots.__get__(ui, KlingAutomationUI)
    ui.save_config = lambda: None
    monkeypatch.setattr("builtins.input", lambda *args, **kwargs: "")
    ui._apply_recommended_automation_defaults()
    assert ui.config["automation_max_cases_per_run"] == "1"


def test_outpaint_timeout_normalization_helper():
    assert get_outpaint_fal_timeout_seconds({}) == 150
    assert get_outpaint_fal_timeout_seconds({"outpaint_fal_timeout_seconds": "abc"}) == 150
    assert get_outpaint_fal_timeout_seconds({"outpaint_fal_timeout_seconds": 5}) == 30
    assert get_outpaint_fal_timeout_seconds({"outpaint_fal_timeout_seconds": 999}) == 300
    assert get_outpaint_fal_timeout_seconds({"outpaint_fal_timeout_seconds": "180"}) == 180


def test_automation_status_lines_include_front_passes(tmp_path):
    ui = KlingAutomationUI.__new__(KlingAutomationUI)
    ui.config = {
        "automation_max_cases_per_run": "5",
        "falai_api_key": "x",
        "bfl_api_key": "y",
        "automation_front_expand_mode": "percent",
        "automation_front_expand_percent": 70,
        "automation_front_expand_passes": 2,
        "automation_front_expand_provider": "bfl",
        "automation_front_expand_composite_mode": "preserve_seamless",
        "automation_selfie_expand_mode": "percent",
        "automation_selfie_expand_percent": 30,
        "automation_selfie_expand_provider": "bfl",
        "automation_selfie_expand_composite_mode": "preserve_seamless",
        "automation_selfie_models": ["fal-ai/nano-banana-2/edit"],
        "automation_selfie_prompt_slot": 1,
        "automation_selfie_prompts": {"1": "x"},
        "automation_similarity_threshold": 80,
        "current_model": "m",
        "current_prompt_slot": 1,
        "automation_oldcam_version": "all",
        "automation_oldcam_required": False,
        "automation_recommended_defaults_version": 1,
        "automation_verbose_logging": True,
    }
    ui.automation_root_folder = str(tmp_path)
    ui._read_max_cases_setting = lambda: "5"
    ui._resolve_provider = lambda _x: "bfl"
    ui._oldcam_readiness_status = lambda: "ready(v7,v8)"
    ui._selfie_model_label_map = lambda: {}
    ui._ensure_selfie_prompt_slots = lambda: None
    lines = ui._automation_status_lines()
    assert any("passes=2" in line for line in lines)
    assert any("composite=" in line for line in lines)


def test_merge_defaults_includes_facetrack_gate_keys(tmp_path):
    """Face-track keys must still be present (retained as an opt-in
    diagnostic), but DEFAULT OFF: a large balanced corpus showed
    face-track % does not separate Persona PASS/FAIL — see
    docs/analysis/versailles_fail_vs_pass.md "DEFINITIVE LARGE-CORPUS
    NEGATIVE"."""
    merged = merge_automation_defaults({})
    assert merged["automation_facetrack_enabled"] is False
    assert merged["automation_facetrack_min_pct"] == 96.0
    assert merged["automation_facetrack_required"] is False
    assert merged["automation_facetrack_sample_fps"] == 8.0


def test_merge_defaults_includes_rppg_keys(tmp_path):
    """rPPG injection keys must be present and DEFAULT OFF: rPPG is the
    genuinely-untried forward direction (sub-perceptual pulse so Persona's
    passive rPPG stage sees a real signal). Off + non-required => the step
    is opt-in only and a missing/failed injection never hard-fails a run.

    Mode default flipped from "inject" to "iterative" in PR #43 — the
    friend who wrote the injector confirmed iterative is mandatory for
    production (initial single-shot rarely lands at optimal strength).
    Companion flags (iterate-from-baseline, skip-diagnosis,
    skip-kinematic-gate) all default ON to mirror rPPG/rppg.bat.
    See docs/rppg-wiring.md."""
    merged = merge_automation_defaults({})
    assert merged["automation_rppg_enabled"] is False
    assert merged["automation_rppg_mode"] == "iterative"
    assert merged["automation_rppg_iterate_from_baseline"] is True
    assert merged["automation_rppg_skip_diagnosis"] is True
    assert merged["automation_rppg_skip_kinematic_gate"] is True
    assert merged["automation_rppg_required"] is False


def test_automation_status_lines_include_facetrack_indicator(tmp_path):
    """Preflight summary surfaces the face-track gate indicator so the
    operator sees gate state before a run."""
    ui = KlingAutomationUI.__new__(KlingAutomationUI)
    ui.config = {
        "automation_max_cases_per_run": "5",
        "falai_api_key": "x", "bfl_api_key": "y",
        "automation_front_expand_mode": "percent",
        "automation_front_expand_percent": 70,
        "automation_front_expand_passes": 1,
        "automation_front_expand_provider": "bfl",
        "automation_front_expand_composite_mode": "preserve_seamless",
        "automation_selfie_expand_mode": "percent",
        "automation_selfie_expand_percent": 30,
        "automation_selfie_expand_provider": "bfl",
        "automation_selfie_expand_composite_mode": "preserve_seamless",
        "automation_selfie_models": ["fal-ai/nano-banana-2/edit"],
        "automation_selfie_prompt_slot": 1,
        "automation_selfie_prompts": {"1": "x"},
        "automation_similarity_threshold": 80,
        "current_model": "m", "current_prompt_slot": 1,
        "automation_oldcam_version": "v24",
        "automation_oldcam_required": False,
        "automation_facetrack_enabled": True,
        "automation_facetrack_min_pct": 96.0,
        "automation_facetrack_required": False,
        "automation_recommended_defaults_version": 1,
        "automation_verbose_logging": True,
    }
    ui.automation_root_folder = str(tmp_path)
    ui._read_max_cases_setting = lambda: "5"
    ui._resolve_provider = lambda _x: "bfl"
    ui._oldcam_readiness_status = lambda: "ready(v24)"
    ui._selfie_model_label_map = lambda: {}
    ui._ensure_selfie_prompt_slots = lambda: None
    lines = ui._automation_status_lines()
    ft = [ln for ln in lines if "facetrack_gate=" in ln]
    assert ft, f"no facetrack indicator in {lines}"
    assert "on" in ft[0] and "96.0%" in ft[0] and "advisory" in ft[0]
