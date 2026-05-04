import kling_automation_ui
import types
import pytest


def test_main_routes_auto_mode(monkeypatch):
    calls = {"auto": 0}

    class DummyApp:
        def __init__(self, legacy_pauses=False):
            self.legacy_pauses = legacy_pauses

        def run_auto_mode(self):
            calls["auto"] += 1

        def launch_gui(self):
            raise AssertionError("gui should not run")

        def run_manual_video_mode(self):
            raise AssertionError("manual mode should not run")

        def run(self):
            raise AssertionError("default run should not execute")

    monkeypatch.setattr(kling_automation_ui, "KlingAutomationUI", DummyApp)
    fake_dep = types.SimpleNamespace(run_dependency_check=lambda auto_mode=True, enforce_all=False: True)
    monkeypatch.setitem(__import__("sys").modules, "dependency_checker", fake_dep)
    kling_automation_ui.main(["--auto"])
    assert calls["auto"] == 1


def test_main_routes_manual_mode(monkeypatch):
    calls = {"manual": 0}

    class DummyApp:
        def __init__(self, legacy_pauses=False):
            self.legacy_pauses = legacy_pauses

        def run_auto_mode(self):
            raise AssertionError("auto should not run")

        def launch_gui(self):
            raise AssertionError("gui should not run")

        def run_manual_video_mode(self):
            calls["manual"] += 1

        def run(self):
            raise AssertionError("default run should not execute")

    monkeypatch.setattr(kling_automation_ui, "KlingAutomationUI", DummyApp)
    fake_dep = types.SimpleNamespace(run_dependency_check=lambda auto_mode=True, enforce_all=False: True)
    monkeypatch.setitem(__import__("sys").modules, "dependency_checker", fake_dep)
    kling_automation_ui.main(["--manual-video"])
    assert calls["manual"] == 1


def test_main_passes_legacy_pause_switch(monkeypatch):
    observed = {"legacy": None}

    class DummyApp:
        def __init__(self, legacy_pauses=False):
            observed["legacy"] = legacy_pauses

        def run_auto_mode(self):
            raise AssertionError("auto should not run")

        def launch_gui(self):
            raise AssertionError("gui should not run")

        def run_manual_video_mode(self):
            raise AssertionError("manual should not run")

        def run(self):
            return

    monkeypatch.setattr(kling_automation_ui, "KlingAutomationUI", DummyApp)
    fake_dep = types.SimpleNamespace(run_dependency_check=lambda auto_mode=True, enforce_all=False: True)
    monkeypatch.setitem(__import__("sys").modules, "dependency_checker", fake_dep)
    kling_automation_ui.main(["--legacy-pauses"])
    assert observed["legacy"] is True


def test_main_passes_legacy_pause_switch_via_env(monkeypatch):
    observed = {"legacy": None}

    class DummyApp:
        def __init__(self, legacy_pauses=False):
            observed["legacy"] = legacy_pauses

        def run_auto_mode(self):
            raise AssertionError("auto should not run")

        def launch_gui(self):
            raise AssertionError("gui should not run")

        def run_manual_video_mode(self):
            raise AssertionError("manual should not run")

        def run(self):
            return

    monkeypatch.setattr(kling_automation_ui, "KlingAutomationUI", DummyApp)
    fake_dep = types.SimpleNamespace(run_dependency_check=lambda auto_mode=True, enforce_all=False: True)
    monkeypatch.setitem(__import__("sys").modules, "dependency_checker", fake_dep)
    monkeypatch.setenv("KLING_LEGACY_PAUSES", "1")
    kling_automation_ui.main([])
    assert observed["legacy"] is True


def test_main_verbose_startup_enforces_all(monkeypatch):
    called = {"kwargs": None}

    def fake_run_dependency_check(*args, **kwargs):
        called["kwargs"] = kwargs
        return True

    fake_dep = types.SimpleNamespace(run_dependency_check=fake_run_dependency_check)
    monkeypatch.setitem(__import__("sys").modules, "dependency_checker", fake_dep)

    class DummyApp:
        def __init__(self, legacy_pauses=False):
            self.legacy_pauses = legacy_pauses

        def run_auto_mode(self):
            return

        def launch_gui(self):
            return

        def run_manual_video_mode(self):
            return

        def run(self):
            return

    monkeypatch.setattr(kling_automation_ui, "KlingAutomationUI", DummyApp)
    kling_automation_ui.main(["--auto", "--verbose-startup"])
    assert called["kwargs"] is not None
    assert called["kwargs"].get("enforce_all") is True


def test_main_dependency_check_failure_exits(monkeypatch):
    def fake_run_dependency_check(*args, **kwargs):
        return False

    fake_dep = types.SimpleNamespace(run_dependency_check=fake_run_dependency_check)
    monkeypatch.setitem(__import__("sys").modules, "dependency_checker", fake_dep)

    class DummyApp:
        def __init__(self, legacy_pauses=False):
            self.legacy_pauses = legacy_pauses

        def run_auto_mode(self):
            raise AssertionError("run_auto_mode should not be called")

    monkeypatch.setattr(kling_automation_ui, "KlingAutomationUI", DummyApp)
    with pytest.raises(SystemExit) as exc:
        kling_automation_ui.main(["--auto", "--verbose-startup"])
    assert exc.value.code == 1


def test_main_skips_dependency_check_when_env_set(monkeypatch):
    called = {"count": 0}

    def fake_run_dependency_check(*args, **kwargs):
        called["count"] += 1
        return True

    fake_dep = types.SimpleNamespace(run_dependency_check=fake_run_dependency_check)
    monkeypatch.setitem(__import__("sys").modules, "dependency_checker", fake_dep)

    class DummyApp:
        def __init__(self, legacy_pauses=False):
            self.legacy_pauses = legacy_pauses

        def run_auto_mode(self):
            return

        def launch_gui(self):
            return

        def run_manual_video_mode(self):
            return

        def run(self):
            return

    monkeypatch.setattr(kling_automation_ui, "KlingAutomationUI", DummyApp)
    monkeypatch.setenv("KLING_SKIP_PY_STARTUP_DEP_CHECK", "1")
    kling_automation_ui.main([])
    assert called["count"] == 0
