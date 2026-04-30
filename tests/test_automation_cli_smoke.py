from kling_automation_ui import KlingAutomationUI


def test_cli_has_automation_menu():
    assert hasattr(KlingAutomationUI, "run_automation_menu")
    assert hasattr(KlingAutomationUI, "_dry_run_automation")

