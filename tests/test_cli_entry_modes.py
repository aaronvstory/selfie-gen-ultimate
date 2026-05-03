import kling_automation_ui
import types


def test_main_routes_auto_mode(monkeypatch):
    calls = {"auto": 0}

    class DummyApp:
        def __init__(self):
            pass

        def run_auto_mode(self):
            calls["auto"] += 1

        def launch_gui(self):
            raise AssertionError("gui should not run")

        def run_manual_video_mode(self):
            raise AssertionError("manual mode should not run")

        def run(self):
            raise AssertionError("default run should not execute")

    monkeypatch.setattr(kling_automation_ui, "KlingAutomationUI", DummyApp)
    fake_dep = types.SimpleNamespace(run_dependency_check=lambda auto_mode=True: None)
    monkeypatch.setitem(__import__("sys").modules, "dependency_checker", fake_dep)
    kling_automation_ui.main(["--auto"])
    assert calls["auto"] == 1


def test_main_routes_manual_mode(monkeypatch):
    calls = {"manual": 0}

    class DummyApp:
        def __init__(self):
            pass

        def run_auto_mode(self):
            raise AssertionError("auto should not run")

        def launch_gui(self):
            raise AssertionError("gui should not run")

        def run_manual_video_mode(self):
            calls["manual"] += 1

        def run(self):
            raise AssertionError("default run should not execute")

    monkeypatch.setattr(kling_automation_ui, "KlingAutomationUI", DummyApp)
    fake_dep = types.SimpleNamespace(run_dependency_check=lambda auto_mode=True: None)
    monkeypatch.setitem(__import__("sys").modules, "dependency_checker", fake_dep)
    kling_automation_ui.main(["--manual-video"])
    assert calls["manual"] == 1
