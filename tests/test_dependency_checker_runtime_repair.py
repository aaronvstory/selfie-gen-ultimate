import types
from unittest import mock

import dependency_checker as dc


def _make_dep(name: str, import_name: str, required: bool, installed: bool, runtime_issue: str | None):
    return dc.Dependency(
        name=name,
        import_name=import_name,
        pip_name=name.lower(),
        required=required,
        description=name,
        installed=installed,
        version="1.0.0",
        runtime_issue=runtime_issue,
    )


def test_strict_check_triggers_runtime_repair_and_recovers(monkeypatch):
    class FakeChecker:
        call_count = 0

        def __init__(self):
            FakeChecker.call_count += 1
            if FakeChecker.call_count == 1:
                self.python_deps = [
                    _make_dep("TF-Keras", "tf_keras", False, True, "cannot import name 'runtime_version'"),
                ]
            else:
                self.python_deps = [
                    _make_dep("TF-Keras", "tf_keras", False, True, None),
                ]
            self.external_tools = []
            self.YELLOW = ""
            self.CYAN = ""
            self.GRAY = ""
            self.GREEN = ""
            self.RED = ""
            self.MAGENTA = ""
            self.RESET = ""
            self.WHITE = ""

        def check_all(self):
            if FakeChecker.call_count == 1:
                return 1, 0, 0, 1
            return 1, 0, 1, 0

        def display_status(self):
            return None

        def display_summary(self, req_ok, req_missing, opt_ok, opt_missing):
            return req_missing == 0

        def get_missing_pip_packages(self):
            if FakeChecker.call_count == 1:
                return [self.python_deps[0]]
            return []

    monkeypatch.setattr(dc, "DependencyChecker", FakeChecker)
    run_calls = []

    def _fake_run(cmd, **kwargs):
        run_calls.append(cmd)
        return types.SimpleNamespace(returncode=0)

    monkeypatch.setattr(dc.subprocess, "run", _fake_run)

    ok = dc.run_dependency_check(auto_mode=True, enforce_all=True, install_external_tools=False)
    assert ok is True
    assert any("dependency_health_check.py" in " ".join(map(str, call)) for call in run_calls)


def test_runtime_repair_packages_include_protobuf_pin():
    import dependency_health_check as dhc

    assert any(pkg.startswith("protobuf==") for pkg in dhc.REPAIR_PACKAGES)
