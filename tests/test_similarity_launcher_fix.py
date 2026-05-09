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


def test_darwin_parent_launch_uses_bash_and_fails_on_early_exit():
    module = importlib.import_module("kling_gui.main_window")
    window = module.KlingGUIWindow.__new__(module.KlingGUIWindow)
    window.root = object()
    logs = []
    window._log = lambda message, level="info": logs.append((message, level))
    window._resolve_similarity_dir = lambda: "/tmp/similarity"
    window._similarity_launcher_name = lambda: "run_gui.command"

    proc = _FakeProc(poll_result=1)
    with mock.patch("os.path.isdir", return_value=True), mock.patch(
        "os.path.isfile", return_value=True
    ), mock.patch("platform.system", return_value="Darwin"), mock.patch(
        "subprocess.Popen", return_value=proc
    ) as popen_mock, mock.patch.object(
        module.messagebox, "showerror"
    ) as showerror_mock, mock.patch(
        "time.sleep", return_value=None
    ):
        launched = window._launch_similarity_gui(show_dialog=True)

    assert launched is False
    args, kwargs = popen_mock.call_args
    assert args[0] == ["/bin/bash", os.path.join("/tmp/similarity", "run_gui.command")]
    assert kwargs["env"]["SIMILARITY_LAUNCHED_BY_MAIN"] == "1"
    assert any("exited immediately" in msg for msg, _ in logs)
    showerror_mock.assert_called_once()


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
