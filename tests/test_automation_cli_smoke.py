from kling_automation_ui import KlingAutomationUI


def test_cli_has_automation_menu():
    assert hasattr(KlingAutomationUI, "run_automation_menu")


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
