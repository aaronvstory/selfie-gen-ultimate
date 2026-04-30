from kling_automation_ui import KlingAutomationUI


def test_cli_has_automation_menu():
    assert hasattr(KlingAutomationUI, "run_automation_menu")
    assert hasattr(KlingAutomationUI, "_edit_automation_settings_quick")


def test_dry_run_ignores_corrupt_manifest(tmp_path, monkeypatch):
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
        "automation_front_expand_provider": "auto",
        "automation_front_expand_mode": "document_3x4",
        "automation_front_expand_percent": 30,
        "automation_front_edge_seal_enabled": True,
        "automation_front_edge_seal_px": 12,
        "automation_front_output_name": "front-expanded.png",
        "automation_extract_enabled": True,
        "automation_extract_output_name": "extracted.png",
        "automation_crop_multiplier": 1.5,
        "automation_selfie_enabled": True,
        "automation_selfie_models": ["openai/gpt-image-2/edit"],
        "automation_selfie_model_policy": "first_pass",
        "automation_selfie_max_attempts_per_model": 1,
        "automation_similarity_threshold": 80,
        "automation_selfie_expand_enabled": True,
        "automation_selfie_expand_provider": "auto",
        "automation_selfie_expand_mode": "percent",
        "automation_selfie_expand_percent": 30,
        "automation_video_enabled": True,
        "automation_video_aspect_ratio": "3:4",
        "automation_video_use_existing_prompt": True,
        "automation_oldcam_enabled": True,
        "automation_oldcam_version": "v8",
        "automation_oldcam_required": False,
    }
    ui.automation_root_folder = str(tmp_path)
    ui.print_red = lambda _x: None
    ui.save_config = lambda: None

    responses = iter(
        [
            str(tmp_path),  # root
            "",  # manifest
            "", "", "",  # skip toggles
            "y",  # allow reprocess
            "increment",  # mode
            "", "", "",  # front enabled/provider/mode
            "40",  # front pct
            "", "", "",  # edge + output
            "", "", "", "",  # extract/crop/selfie enabled/models
            "", "",  # model policy/attempts
            "85",  # threshold
            "", "", "", "",  # selfie expand settings
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
