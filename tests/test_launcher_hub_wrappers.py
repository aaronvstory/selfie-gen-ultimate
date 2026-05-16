from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]


def _read(path: str) -> str:
    return (REPO_ROOT / path).read_text(encoding="utf-8")


def test_launcher_hub_wrappers_delegate_targets():
    wrappers = {
        "launchers/windows/run_similarity_gui.bat": r'call "%ROOT_DIR%\similarity\run_gui.bat" %*',
        "launchers/windows/run_similarity_cli.bat": r'call "%ROOT_DIR%\similarity\run_cli.bat" %*',
        "launchers/windows/run_oldcam_v8.bat": r'call "%ROOT_DIR%\oldcam-v8\oldcam_launcher.bat" %*',
        "launchers/windows/run_oldcam_v7.bat": r'call "%ROOT_DIR%\oldcam-v7\oldcam_launcher.bat" %*',
        "launchers/windows/run_oldcam_v12.bat": r'call "%ROOT_DIR%\oldcam-v12\oldcam_launcher.bat" %*',
        "launchers/windows/run_oldcam_v13.bat": r'call "%ROOT_DIR%\oldcam-v13\oldcam_launcher.bat" %*',
        "launchers/windows/run_oldcam_v14.bat": r'call "%ROOT_DIR%\oldcam-v14\oldcam_launcher.bat" %*',
        "launchers/windows/run_oldcam.bat": r'call "%ROOT_DIR%\launchers\windows\run_oldcam_v14.bat" %*',
        "launchers/macos/run_similarity_gui.command": 'exec "$ROOT_DIR/similarity/run_gui.command" "$@"',
        "launchers/macos/run_similarity_cli.command": 'exec "$ROOT_DIR/similarity/run_cli.command" "$@"',
        "launchers/macos/run_oldcam_v8.command": 'exec "$ROOT_DIR/oldcam-v8/macOS/oldcam.command" "$@"',
        "launchers/macos/run_oldcam_v7.command": 'exec "$ROOT_DIR/oldcam-v7/macOS/oldcam.command" "$@"',
        "launchers/macos/run_oldcam_v12.command": 'exec "$ROOT_DIR/oldcam-v12/macOS/oldcam.command" "$@"',
        "launchers/macos/run_oldcam_v13.command": 'exec "$ROOT_DIR/oldcam-v13/macOS/oldcam.command" "$@"',
        "launchers/macos/run_oldcam_v14.command": 'exec "$ROOT_DIR/oldcam-v14/macOS/oldcam.command" "$@"',
        "launchers/macos/run_oldcam.command": 'exec "$ROOT_DIR/launchers/macos/run_oldcam_v14.command" "$@"',
        "launchers/run_similarity_gui.bat": r'call "%ROOT_DIR%\launchers\windows\run_similarity_gui.bat" %*',
        "launchers/run_similarity_cli.bat": r'call "%ROOT_DIR%\launchers\windows\run_similarity_cli.bat" %*',
        "launchers/run_oldcam_v8.bat": r'call "%ROOT_DIR%\launchers\windows\run_oldcam_v8.bat" %*',
        "launchers/run_oldcam_v7.bat": r'call "%ROOT_DIR%\launchers\windows\run_oldcam_v7.bat" %*',
        "launchers/run_oldcam_v12.bat": r'call "%ROOT_DIR%\launchers\windows\run_oldcam_v12.bat" %*',
        "launchers/run_oldcam_v13.bat": r'call "%ROOT_DIR%\launchers\windows\run_oldcam_v13.bat" %*',
        "launchers/run_oldcam_v14.bat": r'call "%ROOT_DIR%\launchers\windows\run_oldcam_v14.bat" %*',
        "launchers/run_oldcam.bat": r'call "%ROOT_DIR%\launchers\windows\run_oldcam.bat" %*',
        "launchers/run_similarity_gui.command": 'exec "$ROOT_DIR/launchers/macos/run_similarity_gui.command" "$@"',
        "launchers/run_similarity_cli.command": 'exec "$ROOT_DIR/launchers/macos/run_similarity_cli.command" "$@"',
        "launchers/run_oldcam_v8.command": 'exec "$ROOT_DIR/launchers/macos/run_oldcam_v8.command" "$@"',
        "launchers/run_oldcam_v7.command": 'exec "$ROOT_DIR/launchers/macos/run_oldcam_v7.command" "$@"',
        "launchers/run_oldcam_v12.command": 'exec "$ROOT_DIR/launchers/macos/run_oldcam_v12.command" "$@"',
        "launchers/run_oldcam_v13.command": 'exec "$ROOT_DIR/launchers/macos/run_oldcam_v13.command" "$@"',
        "launchers/run_oldcam_v14.command": 'exec "$ROOT_DIR/launchers/macos/run_oldcam_v14.command" "$@"',
        "launchers/run_oldcam.command": 'exec "$ROOT_DIR/launchers/macos/run_oldcam.command" "$@"',
    }
    for path, marker in wrappers.items():
        text = _read(path)
        assert marker in text


def test_launcher_hub_windows_wrappers_preserve_exit_codes():
    for script in (
        "launchers/windows/run_similarity_gui.bat",
        "launchers/windows/run_similarity_cli.bat",
        "launchers/windows/run_oldcam_v7.bat",
        "launchers/windows/run_oldcam_v8.bat",
        "launchers/windows/run_oldcam_v12.bat",
        "launchers/windows/run_oldcam_v13.bat",
        "launchers/windows/run_oldcam_v14.bat",
        "launchers/windows/run_oldcam.bat",
        "launchers/run_similarity_gui.bat",
        "launchers/run_similarity_cli.bat",
        "launchers/run_oldcam_v7.bat",
        "launchers/run_oldcam_v8.bat",
        "launchers/run_oldcam_v12.bat",
        "launchers/run_oldcam_v13.bat",
        "launchers/run_oldcam_v14.bat",
        "launchers/run_oldcam.bat",
    ):
        text = _read(script)
        assert 'set "EXIT_CODE=%ERRORLEVEL%"' in text
        assert "exit /b %EXIT_CODE%" in text


def test_windows_root_launchers_install_mediapipe_with_no_deps():
    gui = _read("launchers/windows/run_gui.bat")
    cli = _read("launchers/windows/run_cli.bat")
    for text in (gui, cli):
        assert 'findstr /V /I /B "mediapipe"' in text
        assert '-m pip install --no-deps "%MEDIAPIPE_SPEC%"' in text
        assert "ERROR: Dependency bootstrap failed." in text
