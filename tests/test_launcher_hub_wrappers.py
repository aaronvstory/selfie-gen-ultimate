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
        "launchers/windows/run_oldcam_v15.bat": r'call "%ROOT_DIR%\oldcam-v15\oldcam_launcher.bat" %*',
        "launchers/windows/run_oldcam_v24.bat": r'call "%ROOT_DIR%\oldcam-v24\oldcam_launcher.bat" %*',
        "launchers/windows/run_oldcam.bat": r'call "%ROOT_DIR%\launchers\windows\run_oldcam_v24.bat" %*',
        "launchers/macos/run_similarity_gui.command": 'exec "$ROOT_DIR/similarity/run_gui.command" "$@"',
        "launchers/macos/run_similarity_cli.command": 'exec "$ROOT_DIR/similarity/run_cli.command" "$@"',
        "launchers/macos/run_oldcam_v8.command": 'exec "$ROOT_DIR/oldcam-v8/macOS/oldcam.command" "$@"',
        "launchers/macos/run_oldcam_v7.command": 'exec "$ROOT_DIR/oldcam-v7/macOS/oldcam.command" "$@"',
        "launchers/macos/run_oldcam_v12.command": 'exec "$ROOT_DIR/oldcam-v12/macOS/oldcam.command" "$@"',
        "launchers/macos/run_oldcam_v13.command": 'exec "$ROOT_DIR/oldcam-v13/macOS/oldcam.command" "$@"',
        "launchers/macos/run_oldcam_v14.command": 'exec "$ROOT_DIR/oldcam-v14/macOS/oldcam.command" "$@"',
        "launchers/macos/run_oldcam_v15.command": 'exec "$ROOT_DIR/oldcam-v15/macOS/oldcam.command" "$@"',
        "launchers/macos/run_oldcam_v24.command": 'exec "$ROOT_DIR/oldcam-v24/macOS/oldcam.command" "$@"',
        "launchers/macos/run_oldcam.command": 'exec "$ROOT_DIR/launchers/macos/run_oldcam_v24.command" "$@"',
        "launchers/run_similarity_gui.bat": r'call "%ROOT_DIR%\launchers\windows\run_similarity_gui.bat" %*',
        "launchers/run_similarity_cli.bat": r'call "%ROOT_DIR%\launchers\windows\run_similarity_cli.bat" %*',
        "launchers/run_oldcam_v8.bat": r'call "%ROOT_DIR%\launchers\windows\run_oldcam_v8.bat" %*',
        "launchers/run_oldcam_v7.bat": r'call "%ROOT_DIR%\launchers\windows\run_oldcam_v7.bat" %*',
        "launchers/run_oldcam_v12.bat": r'call "%ROOT_DIR%\launchers\windows\run_oldcam_v12.bat" %*',
        "launchers/run_oldcam_v13.bat": r'call "%ROOT_DIR%\launchers\windows\run_oldcam_v13.bat" %*',
        "launchers/run_oldcam_v14.bat": r'call "%ROOT_DIR%\launchers\windows\run_oldcam_v14.bat" %*',
        "launchers/run_oldcam_v15.bat": r'call "%ROOT_DIR%\launchers\windows\run_oldcam_v15.bat" %*',
        "launchers/run_oldcam_v24.bat": r'call "%ROOT_DIR%\launchers\windows\run_oldcam_v24.bat" %*',
        "launchers/run_oldcam.bat": r'call "%ROOT_DIR%\launchers\windows\run_oldcam.bat" %*',
        "launchers/run_similarity_gui.command": 'exec "$ROOT_DIR/launchers/macos/run_similarity_gui.command" "$@"',
        "launchers/run_similarity_cli.command": 'exec "$ROOT_DIR/launchers/macos/run_similarity_cli.command" "$@"',
        "launchers/run_oldcam_v8.command": 'exec "$ROOT_DIR/launchers/macos/run_oldcam_v8.command" "$@"',
        "launchers/run_oldcam_v7.command": 'exec "$ROOT_DIR/launchers/macos/run_oldcam_v7.command" "$@"',
        "launchers/run_oldcam_v12.command": 'exec "$ROOT_DIR/launchers/macos/run_oldcam_v12.command" "$@"',
        "launchers/run_oldcam_v13.command": 'exec "$ROOT_DIR/launchers/macos/run_oldcam_v13.command" "$@"',
        "launchers/run_oldcam_v14.command": 'exec "$ROOT_DIR/launchers/macos/run_oldcam_v14.command" "$@"',
        "launchers/run_oldcam_v15.command": 'exec "$ROOT_DIR/launchers/macos/run_oldcam_v15.command" "$@"',
        "launchers/run_oldcam_v24.command": 'exec "$ROOT_DIR/launchers/macos/run_oldcam_v24.command" "$@"',
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
        "launchers/windows/run_oldcam_v15.bat",
        "launchers/windows/run_oldcam_v24.bat",
        "launchers/windows/run_oldcam.bat",
        "launchers/run_similarity_gui.bat",
        "launchers/run_similarity_cli.bat",
        "launchers/run_oldcam_v7.bat",
        "launchers/run_oldcam_v8.bat",
        "launchers/run_oldcam_v12.bat",
        "launchers/run_oldcam_v13.bat",
        "launchers/run_oldcam_v14.bat",
        "launchers/run_oldcam_v15.bat",
        "launchers/run_oldcam_v24.bat",
        "launchers/run_oldcam.bat",
    ):
        text = _read(script)
        assert 'set "EXIT_CODE=%ERRORLEVEL%"' in text
        assert "exit /b %EXIT_CODE%" in text


def test_macos_v14_v15_v24_command_wrappers_use_strict_set_flags():
    """CLAUDE.md macOS Rule 10 — .command sibling launchers in the v14, v15
    and v24 chains must all use `set -euo pipefail`. v7-v13 are intentionally
    NOT covered here per docs/oldcam-wiring.md §9 (known-defect carve-out)."""
    for path in (
        "launchers/macos/run_oldcam_v14.command",
        "launchers/run_oldcam_v14.command",
        "launchers/macos/run_oldcam_v15.command",
        "launchers/run_oldcam_v15.command",
        "launchers/macos/run_oldcam_v24.command",
        "launchers/run_oldcam_v24.command",
    ):
        text = _read(path)
        assert "set -euo pipefail" in text, (
            f"{path}: Rule 10 violation — must use `set -euo pipefail` for "
            "parity with the algorithm-layer oldcam-vN/macOS/oldcam.command."
        )


def test_windows_root_launchers_install_mediapipe_with_no_deps():
    gui = _read("launchers/windows/run_gui.bat")
    cli = _read("launchers/windows/run_cli.bat")
    for text in (gui, cli):
        assert 'findstr /V /I /B "mediapipe"' in text
        # mediapipe install carries the guarded constraints flag (v2.11 numpy-2
        # guard) so even the --no-deps step can't let a later resolve upgrade
        # numpy >=2. !CC! is set to -c "%CONSTRAINTS_FILE%" when the file exists.
        assert '-m pip install --no-deps !CC! "%MEDIAPIPE_SPEC%"' in text
        assert "ERROR: Dependency bootstrap failed." in text
