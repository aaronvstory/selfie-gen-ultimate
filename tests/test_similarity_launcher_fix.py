import importlib
import os
from pathlib import Path
from unittest import mock

import pytest


class _FakeProc:
    def __init__(self, pid: int = 999, poll_result: int | None = None):
        self.pid = pid
        self._poll_result = poll_result

    def poll(self):
        return self._poll_result


def test_darwin_parent_launch_uses_bash_and_schedules_after_probe():
    module = importlib.import_module("kling_gui.main_window")
    window = module.KlingGUIWindow.__new__(module.KlingGUIWindow)
    logs = []
    window._log = lambda message, level="info": logs.append((message, level))
    window._resolve_similarity_dir = lambda: "/tmp/similarity"
    window._similarity_launcher_name = lambda: "run_gui.command"
    window.root = mock.Mock()

    proc = _FakeProc(poll_result=1)
    with mock.patch("os.path.isdir", return_value=True), mock.patch(
        "os.path.isfile", return_value=True
    ), mock.patch("platform.system", return_value="Darwin"), mock.patch(
        "subprocess.Popen", return_value=proc
    ) as popen_mock, mock.patch.object(module.messagebox, "showerror") as showerror_mock:
        launched = window._launch_similarity_gui(show_dialog=True)

    assert launched is True
    args, kwargs = popen_mock.call_args
    assert args[0] == ["/bin/bash", os.path.join("/tmp/similarity", "run_gui.command")]
    assert kwargs["env"]["SIMILARITY_LAUNCHED_BY_MAIN"] == "1"
    window.root.after.assert_called_once()
    after_args = window.root.after.call_args[0]
    assert after_args[0] == 800
    assert callable(after_args[1])
    assert not any("exited immediately" in msg for msg, _ in logs)
    showerror_mock.assert_not_called()


def test_similarity_early_exit_helper_logs_and_shows_dialog():
    module = importlib.import_module("kling_gui.main_window")
    window = module.KlingGUIWindow.__new__(module.KlingGUIWindow)
    logs = []
    window._log = lambda message, level="info": logs.append((message, level))
    window.root = object()

    proc = _FakeProc(poll_result=3)
    with mock.patch.object(module.messagebox, "showerror") as showerror_mock:
        window._check_similarity_early_exit(
            process=proc,
            launcher_name="run_gui.command",
            runtime_log_path="/tmp/sim/launcher_runtime.log",
            crash_log_path="/tmp/sim/crash.log",
            show_dialog=True,
        )

    assert any("exited immediately" in msg for msg, _ in logs)
    showerror_mock.assert_called_once()


def test_similarity_early_exit_helper_noop_when_still_running():
    module = importlib.import_module("kling_gui.main_window")
    window = module.KlingGUIWindow.__new__(module.KlingGUIWindow)
    logs = []
    window._log = lambda message, level="info": logs.append((message, level))

    proc = _FakeProc(poll_result=None)
    with mock.patch.object(module.messagebox, "showerror") as showerror_mock:
        window._check_similarity_early_exit(
            process=proc,
            launcher_name="run_gui.command",
            runtime_log_path="/tmp/sim/launcher_runtime.log",
            crash_log_path="/tmp/sim/crash.log",
            show_dialog=True,
        )

    assert logs == []
    showerror_mock.assert_not_called()


def test_similarity_entrypoint_preserves_original_exception_if_crash_log_fails(capsys: pytest.CaptureFixture[str]):
    module = importlib.import_module("similarity.main")

    with mock.patch.object(module, "main", side_effect=ValueError("boom")), mock.patch.object(
        module, "_write_crash_log", side_effect=RuntimeError("disk full")
    ):
        with pytest.raises(ValueError, match="boom"):
            module._run_with_crash_logging()

    err = capsys.readouterr().err
    assert "Crash logging failed" in err
    assert "disk full" in err


def test_command_scripts_have_conditional_interactive_logging():
    root = Path(__file__).resolve().parents[1]
    gui_command = (root / "similarity" / "run_gui.command").read_text(encoding="utf-8")
    cli_command = (root / "similarity" / "run_cli.command").read_text(encoding="utf-8")

    for script_text in (gui_command, cli_command):
        assert "SIMILARITY_LAUNCHED_BY_MAIN" in script_text
        assert "tee -a" in script_text


def test_run_cli_bat_has_conditional_parent_and_direct_invocation():
    root = Path(__file__).resolve().parents[1]
    cli_bat = (root / "similarity" / "run_cli.bat").read_text(encoding="utf-8")
    assert "if \"%SIMILARITY_LAUNCHED_BY_MAIN%\"==\"\" (" in cli_bat
    assert "python main.py --cli" in cli_bat
    assert "python main.py --cli >> \"%LOG_FILE%\" 2>&1" in cli_bat
