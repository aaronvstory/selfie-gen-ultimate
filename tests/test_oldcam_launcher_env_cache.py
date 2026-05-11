from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]


def test_run_oldcam_defaults_to_v9_and_uses_stamp_cache():
    text = (REPO_ROOT / "run_oldcam.bat").read_text(encoding="utf-8")
    assert "oldcam-v9\\launcher.py" in text
    assert ".launcher_state" in text
    assert "oldcam_v9_" in text
    assert 'findstr /I /R "^[0-9A-F][0-9A-F]"' in text
    assert 'set "PY_ID=%PY_ID: =_%"' in text
    assert 'if "%NEED_PIP%"=="0" (' in text
    assert "Dependencies unchanged. Skipping pip install." in text
    assert 'findstr /V /I /R "^[ ]*mediapipe"' in text
    assert '-m pip install --no-deps "%MEDIAPIPE_SPEC%"' in text


def test_oldcam_local_launchers_keep_version_specific_targets():
    v7 = (REPO_ROOT / "oldcam-v7" / "oldcam_launcher.bat").read_text(encoding="utf-8")
    v8 = (REPO_ROOT / "oldcam-v8" / "oldcam_launcher.bat").read_text(encoding="utf-8")
    v9 = (REPO_ROOT / "oldcam-v9" / "oldcam_launcher.bat").read_text(encoding="utf-8")
    v10 = (REPO_ROOT / "oldcam-v10" / "oldcam_launcher.bat").read_text(encoding="utf-8")
    assert "oldcam_v7_" in v7
    assert "oldcam_v8_" in v8
    assert "%REPO_ROOT%\\venv\\Scripts\\python.exe" in v7
    assert "%REPO_ROOT%\\venv\\Scripts\\python.exe" in v8
    assert 'set "HAD_ERRORS="' in v7
    assert 'set "HAD_ERRORS="' in v8
    assert '"%PYTHON_CMD%" -c "import cv2, numpy" >nul 2>nul' in v7
    assert '"%PYTHON_CMD%" -m pip install -r "%SCRIPT_DIR%requirements.txt" >nul 2>nul' in v7
    assert 'call "%PYTHON_CMD%" "%SCRIPT_DIR%oldcam.py" "%~1" %EXTRA_ARGS%' in v7
    assert '"%PYTHON_CMD%" -c "import cv2, numpy" >nul 2>nul' in v8
    assert '"%PYTHON_CMD%" -m pip install -r "%SCRIPT_DIR%requirements.txt" >nul 2>nul' in v8
    assert 'call "%PYTHON_CMD%" "%SCRIPT_DIR%oldcam.py" "%~1" %EXTRA_ARGS%' in v8
    assert 'set "PY_ID=%PY_ID:/=_%"' in v7
    assert 'set "PY_ID=%PY_ID: =_%"' in v7
    assert 'set "PY_ID=%PY_ID:/=_%"' in v8
    assert 'set "PY_ID=%PY_ID: =_%"' in v8
    assert 'findstr /V /I /R "^[ ]*mediapipe"' in v9
    assert 'findstr /V /I /R "^[ ]*mediapipe"' in v10
    assert '-m pip install --no-deps "%MEDIAPIPE_SPEC%"' in v9
    assert '-m pip install --no-deps "%MEDIAPIPE_SPEC%"' in v10


def test_oldcam_macos_requirements_hash_has_missing_fallback():
    v7 = (REPO_ROOT / "oldcam-v7" / "macOS" / "oldcam.command").read_text(encoding="utf-8")
    v8 = (REPO_ROOT / "oldcam-v8" / "macOS" / "oldcam.command").read_text(encoding="utf-8")
    assert 'REQ_HASH="$(shasum -a 256 "$SCRIPT_DIR/requirements.txt" 2>/dev/null | awk \'{print $1}\')"' in v7
    assert '[ -n "$REQ_HASH" ] || REQ_HASH="missing"' in v7
    assert 'REQ_HASH="$(shasum -a 256 "$SCRIPT_DIR/requirements.txt" 2>/dev/null | awk \'{print $1}\')"' in v8
    assert '[ -n "$REQ_HASH" ] || REQ_HASH="missing"' in v8


def test_oldcam_macos_uses_repo_root_state_dir_and_file_picker():
    v7 = (REPO_ROOT / "oldcam-v7" / "macOS" / "oldcam.command").read_text(encoding="utf-8")
    v8 = (REPO_ROOT / "oldcam-v8" / "macOS" / "oldcam.command").read_text(encoding="utf-8")
    for text in (v7, v8):
        assert "find_repo_root()" in text
        assert 'STATE_DIR="$REPO_ROOT/.launcher_state"' in text
        assert "pick_files()" in text
        assert "choose file with prompt" in text
        assert 'if [ "$#" -eq 0 ]; then' in text


def test_oldcam_macos_v9_v10_install_mediapipe_separately():
    v9 = (REPO_ROOT / "oldcam-v9" / "macOS" / "oldcam.command").read_text(encoding="utf-8")
    v10 = (REPO_ROOT / "oldcam-v10" / "macOS" / "oldcam.command").read_text(encoding="utf-8")
    for text in (v9, v10):
        assert "grep -vi '^[[:space:]]*mediapipe'" in text
        assert '-m pip install --no-deps "mediapipe>=0.10.14"' in text
