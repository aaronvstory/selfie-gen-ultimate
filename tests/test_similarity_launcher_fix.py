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
    assert after_args[0] == 2500
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
    with mock.patch.object(module.KlingGUIWindow, "_classify_similarity_runtime_log", return_value="failure"), mock.patch.object(
        module.messagebox, "showerror"
    ) as showerror_mock:
        window._check_similarity_early_exit(
            process=proc,
            launcher_name="run_gui.command",
            runtime_log_path="/tmp/sim/launcher_runtime.log",
            crash_log_path="/tmp/sim/crash.log",
            show_dialog=True,
            final_check=True,
        )

    assert any("exited immediately" in msg for msg, _ in logs)
    showerror_mock.assert_called_once()


def test_similarity_early_exit_helper_suppresses_false_failure_on_success_markers():
    module = importlib.import_module("kling_gui.main_window")
    window = module.KlingGUIWindow.__new__(module.KlingGUIWindow)
    logs = []
    window._log = lambda message, level="info": logs.append((message, level))
    window.root = mock.Mock()

    proc = _FakeProc(poll_result=255)
    with mock.patch.object(module.KlingGUIWindow, "_classify_similarity_runtime_log", return_value="success"), mock.patch.object(
        module.messagebox, "showerror"
    ) as showerror_mock:
        window._check_similarity_early_exit(
            process=proc,
            launcher_name="run_gui.bat",
            runtime_log_path="/tmp/sim/launcher_runtime.log",
            crash_log_path="/tmp/sim/crash.log",
            show_dialog=True,
            launch_label="run_gui.bat (via cmd.exe)",
        )

    assert any("startup markers were detected" in msg for msg, _ in logs)
    assert not any("exited immediately" in msg for msg, _ in logs)
    showerror_mock.assert_not_called()


def test_similarity_early_exit_helper_stages_retry_before_final_failure():
    module = importlib.import_module("kling_gui.main_window")
    window = module.KlingGUIWindow.__new__(module.KlingGUIWindow)
    window._log = lambda *_args, **_kwargs: None
    window.root = mock.Mock()

    proc = _FakeProc(poll_result=255)
    with mock.patch.object(module.KlingGUIWindow, "_classify_similarity_runtime_log", return_value="unknown"), mock.patch.object(
        module.messagebox, "showerror"
    ) as showerror_mock:
        window._check_similarity_early_exit(
            process=proc,
            launcher_name="run_gui.bat",
            runtime_log_path="/tmp/sim/launcher_runtime.log",
            crash_log_path="/tmp/sim/crash.log",
            show_dialog=True,
            launch_label="run_gui.bat (via cmd.exe)",
            final_check=False,
        )

    window.root.after.assert_called_once()
    retry_args = window.root.after.call_args[0]
    assert retry_args[0] == 3000
    showerror_mock.assert_not_called()


def test_similarity_fallback_commands_windows_prefers_py_versions():
    module = importlib.import_module("kling_gui.main_window")
    commands = module.KlingGUIWindow._similarity_fallback_commands("Windows")
    assert commands[:4] == [
        ["py", "-3.12", "main.py"],
        ["py", "-3.11", "main.py"],
        ["py", "-3.10", "main.py"],
        ["py", "-3.9", "main.py"],
    ]
    assert commands[-2:] == [["python", "main.py"], ["python3", "main.py"]]


def test_similarity_fallback_commands_non_windows_uses_python_only():
    module = importlib.import_module("kling_gui.main_window")
    commands = module.KlingGUIWindow._similarity_fallback_commands("Darwin")
    assert commands == [["python", "main.py"], ["python3", "main.py"]]


@pytest.mark.skipif(
    os.name != "nt",
    reason="Test asserts Windows backslash path joins; os.path.join uses / on POSIX even when other inputs use backslashes",
)
def test_windows_launcher_uses_comspec_then_fallback():
    module = importlib.import_module("kling_gui.main_window")
    window = module.KlingGUIWindow.__new__(module.KlingGUIWindow)
    logs = []
    window._log = lambda message, level="info": logs.append((message, level))
    window._resolve_similarity_dir = lambda: r"F:\claude\selfie-gen-ultimate\similarity"
    window._similarity_launcher_name = lambda: "run_gui.bat"
    window.root = mock.Mock()

    first_error = OSError("stub fail")
    fallback_proc = _FakeProc(pid=1001, poll_result=None)
    with mock.patch("os.path.isdir", return_value=True), mock.patch(
        "os.path.isfile", return_value=True
    ), mock.patch("platform.system", return_value="Windows"), mock.patch.dict(
        os.environ, {"ComSpec": r"C:\Windows\System32\cmd.exe"}, clear=False
    ), mock.patch(
        "subprocess.Popen", side_effect=[first_error, fallback_proc]
    ) as popen_mock, mock.patch.object(module.messagebox, "showerror") as showerror_mock:
        launched = window._launch_similarity_gui(show_dialog=True)

    assert launched is True
    assert popen_mock.call_count == 2
    first_call_args, first_call_kwargs = popen_mock.call_args_list[0]
    second_call_args, second_call_kwargs = popen_mock.call_args_list[1]
    assert first_call_args[0] == [
        r"C:\Windows\System32\cmd.exe",
        "/c",
        r"F:\claude\selfie-gen-ultimate\similarity\run_gui.bat",
    ]
    assert first_call_kwargs.get("cwd") == r"F:\claude\selfie-gen-ultimate\similarity"
    assert first_call_kwargs.get("env", {}).get("SIMILARITY_LAUNCHED_BY_MAIN") == "1"
    assert first_call_kwargs.get("env", {}).get("TF_USE_LEGACY_KERAS") == "1"
    assert first_call_kwargs.get("env", {}).get("KERAS_BACKEND") == "tensorflow"
    assert second_call_args[0] == ["py", "-3.12", "main.py"]
    assert second_call_kwargs.get("cwd") == r"F:\claude\selfie-gen-ultimate\similarity"
    showerror_mock.assert_not_called()


def test_launch_failure_aggregates_attempt_errors_in_dialog():
    module = importlib.import_module("kling_gui.main_window")
    window = module.KlingGUIWindow.__new__(module.KlingGUIWindow)
    window._log = lambda *_args, **_kwargs: None
    window._resolve_similarity_dir = lambda: r"F:\claude\selfie-gen-ultimate\similarity"
    window._similarity_launcher_name = lambda: "run_gui.bat"
    window.root = mock.Mock()

    with mock.patch("os.path.isdir", return_value=True), mock.patch(
        "os.path.isfile", return_value=False
    ), mock.patch("platform.system", return_value="Windows"), mock.patch(
        "subprocess.Popen", side_effect=OSError("all failed")
    ), mock.patch.object(module.messagebox, "showerror") as showerror_mock:
        launched = window._launch_similarity_gui(show_dialog=True)

    assert launched is False
    showerror_mock.assert_called_once()
    error_message = showerror_mock.call_args.args[1]
    assert "Attempts:" in error_message
    assert "Similarity launcher missing" in error_message
    assert "py -3.12 main.py" in error_message
    assert "python main.py" in error_message


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
    assert "\"!PYTHON_BIN!\" main.py --cli" in cli_bat
    assert "\"!PYTHON_BIN!\" main.py --cli >> \"%LOG_FILE%\" 2>&1" in cli_bat


def test_similarity_launcher_scripts_use_shared_venv_priority_and_stamps():
    root = Path(__file__).resolve().parents[1]
    source_scripts = [
        root / "similarity" / "run_gui.bat",
        root / "similarity" / "run_cli.bat",
    ]
    optional_dist_scripts = [
        root / "dist" / "selfie-gen-ultimate" / "similarity" / "run_gui.bat",
        root / "dist" / "selfie-gen-ultimate" / "similarity" / "run_cli.bat",
    ]
    for script in source_scripts:
        text = script.read_text(encoding="utf-8")
        assert "%REPO_ROOT%\\venv\\Scripts\\python.exe" in text
        assert ".launcher_state" in text
        assert "Unsupported Python version" in text
    for script in optional_dist_scripts:
        if not script.exists():
            continue
        text = script.read_text(encoding="utf-8")
        assert "%REPO_ROOT%\\venv\\Scripts\\python.exe" in text
        assert ".launcher_state" in text


def test_run_gui_command_running_message_precedes_main_launch():
    root = Path(__file__).resolve().parents[1]
    text = (root / "similarity" / "run_gui.command").read_text(encoding="utf-8")
    assert text.index('echo "[5/5] Running..."') < text.index('"$PYTHON_BIN" main.py')

