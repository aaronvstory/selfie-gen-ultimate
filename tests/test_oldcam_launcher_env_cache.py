from pathlib import Path


def test_run_oldcam_defaults_to_v8_and_uses_stamp_cache():
    text = Path("run_oldcam.bat").read_text(encoding="utf-8")
    assert "oldcam-v8\\launcher.py" in text
    assert ".launcher_state" in text
    assert "oldcam_v8_" in text


def test_oldcam_local_launchers_keep_version_specific_targets():
    v7 = Path("oldcam-v7/oldcam_launcher.bat").read_text(encoding="utf-8")
    v8 = Path("oldcam-v8/oldcam_launcher.bat").read_text(encoding="utf-8")
    assert "oldcam_v7_" in v7
    assert "oldcam_v8_" in v8
    assert "%REPO_ROOT%\\venv\\Scripts\\python.exe" in v7
    assert "%REPO_ROOT%\\venv\\Scripts\\python.exe" in v8
