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
    """First-run config seed must be gated on a one-time `.seeded` marker (NOT
    on the live config's absence — a bundle already ships a sanitized default,
    codex P2), and must write the marker so re-launches never re-seed over the
    user's edits."""
    src = _read()
    assert 'if not exist "%SEED_MARKER%"' in src, (
        "START.bat must gate the seed on the one-time .seeded marker"
    )
    assert "%APP_SUPPORT%\\kling_config.json" in src, (
        "START.bat must seed from the bundled _user_state\\app_support snapshot"
    )
    assert '"%SEED_MARKER%" echo seeded' in src, (
        "START.bat must write the .seeded marker after a successful seed"
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


def test_user_facing_launchers_have_wmic_timestamp_fallback():
    """wmic is removed on modern Windows 11, so the wmic-based timestamp banner
    yields blank launch-log timestamps there. The user-facing launchers
    (START.bat + run_gui.bat + run_cli.bat) must fall back to PowerShell's
    Get-Date when wmic produced nothing (gemini MED, PR #66)."""
    for rel in ("START.bat", "launchers/windows/run_gui.bat", "launchers/windows/run_cli.bat"):
        src = (REPO_ROOT / rel).read_text(encoding="utf-8", errors="replace")
        assert "wmic os get LocalDateTime" in src, f"{rel}: lost the wmic fast path"
        assert "Get-Date -Format" in src, (
            f"{rel}: missing the PowerShell timestamp fallback for wmic-less Win11"
        )


def test_start_bat_drive_root_guard():
    r"""START.bat must NOT strip the trailing backslash when run from a drive
    root (D:\ -> D: is drive-relative and breaks path joins) — gemini HIGH."""
    src = _read()
    assert '":\\"' in src and "SCRIPT_DIR:~-2" in src, (
        "START.bat must guard the trailing-backslash strip against a drive root"
    )


def test_start_bat_validates_extracted_venv():
    """START.bat must probe the extracted venv interpreter and remove it on
    failure (a tarball from another machine can carry a stale pyvenv.cfg base
    path) so run_gui.bat rebuilds rather than handing off a broken venv — codex P1."""
    src = _read()
    assert "import sys" in src and "errorlevel 1" in src, (
        "START.bat must probe the extracted venv before trusting it"
    )


def test_start_bat_ps_fallback_is_backquoted():
    """The wmic-less PowerShell timestamp fallback must be back-quoted under
    `usebackq` or it never executes (falls through to locale time) — codex P3."""
    src = _read()
    assert "in (`powershell" in src, (
        "START.bat usebackq PowerShell fallback must be back-quoted to execute"
    )


def test_start_bat_seed_gated_on_marker_not_config_absence():
    """A shipped bundle already contains a sanitized default kling_config.json,
    so gating the snapshot seed on that file's ABSENCE meant the richer
    _user_state snapshot was never installed (codex P2). The seed must be gated
    on a one-time `.seeded` marker so the snapshot wins on first run, and later
    runs never re-clobber the user's edits."""
    src = _read()
    assert "SEED_MARKER" in src and ".seeded" in src, (
        "START.bat must gate the config seed on a one-time .seeded marker"
    )
    # The seed must be driven by the marker, not by the live config's absence.
    assert 'if not exist "%SEED_MARKER%"' in src, (
        "START.bat seed must be gated on the .seeded marker, not kling_config.json absence"
    )
