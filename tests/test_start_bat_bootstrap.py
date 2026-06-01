"""Static guards for the Windows one-click bootstrap START.bat (v2.14).

START.bat is the cross-platform sibling of the macOS START.command: a portable
single-double-click launcher (used from the SSD / a shipped folder) that seeds
config from a bundled _user_state snapshot, optionally extracts a pre-built
venv, then hands off to the canonical run_gui.bat. These guards pin the
contract so a future edit can't silently break the portable launch path.
"""

from __future__ import annotations

from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
START_BAT = REPO_ROOT / "START.bat"


def test_start_bat_exists():
    assert START_BAT.is_file(), "START.bat (Windows one-click bootstrap) must exist at repo root"


def _read() -> str:
    return START_BAT.read_text(encoding="utf-8", errors="replace")


def test_start_bat_no_posix_devnull():
    """A .bat must use `>nul`, never POSIX `/dev/null` (a linter has substituted
    this before — see project memory)."""
    assert "/dev/null" not in _read(), "START.bat must not contain POSIX /dev/null"


def test_start_bat_crlf_line_endings():
    """Windows batch files require CRLF or every line breaks."""
    raw = START_BAT.read_bytes()
    crlf = raw.count(b"\r\n")
    lf_only = raw.count(b"\n") - crlf
    assert crlf > 0, "START.bat must have CRLF line endings"
    assert lf_only == 0, f"START.bat has {lf_only} LF-only line(s); must be all CRLF"


def test_start_bat_seeds_config_without_clobbering():
    """First-run config seed must be guarded on the live config NOT existing,
    so re-launches never overwrite a user's real config."""
    src = _read()
    assert 'if not exist "%SCRIPT_DIR%\\kling_config.json"' in src, (
        "START.bat must only seed config when no live kling_config.json exists"
    )
    assert "%APP_SUPPORT%\\kling_config.json" in src, (
        "START.bat must seed from the bundled _user_state\\app_support snapshot"
    )


def test_start_bat_hands_off_to_run_gui():
    """START.bat must delegate to the canonical run_gui.bat (not reimplement
    Python resolution / venv build / health check)."""
    src = _read()
    assert "run_gui.bat" in src, "START.bat must hand off to run_gui.bat"
    assert "launchers\\windows\\run_gui.bat" in src, (
        "START.bat should prefer the canonical launchers/windows/run_gui.bat"
    )


def test_start_bat_optional_prebuilt_venv():
    """START.bat should extract a bundled venv-windows.tar when present to skip
    the slow first install — the Windows analog of venv-macos.tar."""
    src = _read()
    assert "venv-windows.tar" in src, (
        "START.bat should support the optional pre-built venv-windows.tar fast path"
    )
