"""Static-text guards on the launcher dependency health-check loop.

Background: a friend on Windows nvidia ran ``run_gui.bat`` once, the
launcher reported a successful install (CUDA-aware torch pulled in many
GB), the GUI launched, and the Face Crop tab immediately showed
"RetinaFace/TensorFlow import failed. Run run_gui.bat for automatic
dependency repair." Re-running ``run_gui.bat`` did nothing because the
launcher's ``deps_*.ok`` stamp short-circuited every subsequent launch,
skipping the dependency health check entirely. Result: an infinite
re-run loop with no recovery action the user could take.

These tests pin two contracts that prevent that class of bug:

  1. The Windows launcher MUST run the dep health probe on the
     cached-stamp path, not just on the stamp-miss path.
  2. The macOS launcher (``run_gui.sh``) MUST clear the health stamp
     BEFORE attempting repair, so a crash-during-repair leaves a
     "needs re-check" signal for the next launch.
  3. Both launchers MUST emit an actionable manual-recovery hint on
     repair failure rather than telling users to "re-run the launcher"
     — which is the exact instruction the bug-reported infinite loop
     was already following.

Pure static text checks — no subprocess execution, so the test runs
quickly on every CI platform.
"""

from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
WIN_BAT = REPO_ROOT / "launchers" / "windows" / "run_gui.bat"
MAC_SH = REPO_ROOT / "run_gui.sh"


def _read(path: Path) -> str:
    return path.read_text(encoding="utf-8")


# ────────────────────────────────────────────────────────────────────
# Windows launcher: cached-stamp path must still run the health probe.
# ────────────────────────────────────────────────────────────────────


def test_windows_cached_stamp_path_runs_health_probe():
    """The ``if exist "%STAMP%"`` branch must invoke ``--mode check``."""
    src = _read(WIN_BAT)
    # Locate the cached-stamp branch and assert the health probe lives inside it.
    stamp_branch_start = src.find('if exist "%STAMP%" (')
    assert stamp_branch_start > 0, "Windows BAT no longer has an `if exist %STAMP%` cached-stamp branch"
    # The block ends at the next top-level `goto :launch` after that branch.
    goto_launch = src.find("goto :launch", stamp_branch_start)
    assert goto_launch > stamp_branch_start, "Couldn't locate end of stamp branch"
    stamp_block = src[stamp_branch_start:goto_launch]
    assert "--mode check" in stamp_block, (
        "Cached-stamp branch must invoke `dependency_health_check.py --mode check` so a "
        "broken-but-stamped install gets re-validated on every launch. Without this, "
        "users hit an infinite `re-run run_gui.bat` loop with no recovery action."
    )


def test_windows_cached_stamp_failure_clears_stamp_before_repair():
    """When the cached-stamp health probe fails, the stamp must be cleared
    BEFORE repair runs so a crash-during-repair leaves a clean slate.
    """
    src = _read(WIN_BAT)
    stamp_branch_start = src.find('if exist "%STAMP%" (')
    goto_launch = src.find("goto :launch", stamp_branch_start)
    stamp_block = src[stamp_branch_start:goto_launch]

    # The `--mode check` failure handler must `del` the stamp before `--mode repair`.
    check_call = stamp_block.find("--mode check")
    repair_call = stamp_block.find("--mode repair", check_call)
    assert check_call > 0 and repair_call > check_call
    between_check_and_repair = stamp_block[check_call:repair_call]
    assert 'del "%STATE_DIR%\\deps_*.ok"' in between_check_and_repair, (
        "Cached-stamp failure path must `del deps_*.ok` BEFORE invoking --mode repair. "
        "Without it, a repair that crashes / is killed leaves a stale stamp; next launch "
        "skips revalidation and the user is stuck with the same broken install."
    )


def test_windows_repair_failure_emits_actionable_recovery_hint():
    """The repair-failure branch must NOT tell users to re-run the launcher —
    that's the exact loop the friend got stuck in. Instead it must point
    at manual recovery steps (force-reinstall command, venv deletion).
    """
    src = _read(WIN_BAT)
    # The fresh-install repair failure branch ("ERROR: Automatic dependency repair FAILED")
    fail_idx = src.find("ERROR: Automatic dependency repair FAILED")
    assert fail_idx > 0, "BAT missing the 'Automatic dependency repair FAILED' branch"
    # 2000-char window after the marker should contain the manual recovery hint.
    window = src[fail_idx : fail_idx + 4000]
    assert "Delete the venv folder" in window or "rd /S /Q" in window, (
        "Repair-failure branch must include a 'delete venv folder' recovery hint"
    )
    assert "pip install --force-reinstall" in window, (
        "Repair-failure branch must include the explicit `pip install --force-reinstall` "
        "command users can copy/paste to recover manually"
    )
    assert "tensorflow==2.16.2" in window and "tf-keras==2.16.0" in window, (
        "The pip-install recovery hint must pin tensorflow + tf-keras versions explicitly"
    )


def test_windows_launcher_writes_per_launch_diag_log():
    """Every launch must append a diagnostic line so users can see what
    happened across multiple runs (required for the user's report:
    'ensure we get proper logging each launch so we can diagnose issues').
    """
    src = _read(WIN_BAT)
    assert 'health-probe START' in src, "Missing per-launch health-probe START log line"
    assert 'health-probe OK' in src or 'health-probe FAIL' in src, (
        "Missing health-probe outcome log lines"
    )
    # The diagnostic log file must be configured early (LOG_FILE is set
    # at the top of the BAT).
    assert 'LOG_FILE=%STATE_DIR%\\launch.log' in src, (
        "LOG_FILE must point to a stable per-launch log under .launcher_state/"
    )


# ────────────────────────────────────────────────────────────────────
# macOS launcher: health probe + stamp invalidation contracts.
# ────────────────────────────────────────────────────────────────────


def test_macos_health_probe_runs_unconditionally():
    """``run_gui.sh`` must invoke ``--mode check`` on every launch (the old
    shape skipped when REQUIREMENTS_STAMP == HEALTH_STAMP, which masked
    failures introduced after the stamp was last written)."""
    src = _read(MAC_SH)
    assert '--mode check' in src
    # The dispatch must NOT be guarded by `_run_health=0 -> skip` shape.
    assert '_run_health=0' not in src, (
        "Old conditional-skip logic detected. `_run_health=0` short-circuit was the "
        "source of the stamp-locks-in-broken-state class of bug."
    )


def test_macos_health_failure_clears_stamp_before_repair():
    src = _read(MAC_SH)
    # Find the failure branch and check for stamp invalidation before repair.
    fail_idx = src.find('Runtime health probe FAILED')
    assert fail_idx > 0, "Missing the 'Runtime health probe FAILED' branch"
    window = src[fail_idx:fail_idx + 2500]
    rm_idx = window.find('rm -f "${HEALTH_STAMP}"')
    repair_idx = window.find('--mode repair')
    assert rm_idx > 0 and repair_idx > rm_idx, (
        "macOS launcher must `rm -f HEALTH_STAMP` BEFORE running `--mode repair`. "
        "Without this, a crash during repair leaves a stamp matching the broken "
        "state; next launch incorrectly skips revalidation."
    )


def test_macos_repair_failure_emits_actionable_recovery_hint():
    src = _read(MAC_SH)
    assert 'ERROR: Automatic dependency repair FAILED' in src
    # The recovery hint must include venv deletion + force-reinstall command.
    assert 'rm -rf' in src and '.venv-macos' in src, (
        "macOS repair-fail branch must include the `rm -rf .venv-macos` recovery hint"
    )
    assert 'pip install --force-reinstall' in src, (
        "macOS repair-fail branch must include the `pip install --force-reinstall` command"
    )
    assert 'tensorflow==2.16.2' in src and 'tf-keras==2.16.0' in src


def test_macos_launcher_writes_per_launch_diag_log():
    src = _read(MAC_SH)
    assert 'health-probe START' in src
    assert 'LAUNCH_DIAG_LOG' in src and 'launch.log' in src


# ────────────────────────────────────────────────────────────────────
# Common contract: per-platform recovery hint helper.
# ────────────────────────────────────────────────────────────────────


def test_face_crop_tab_exposes_per_platform_recovery_hint(monkeypatch):
    """``_platform_face_repair_recovery_hint`` must exist and return per-OS hints.

    Uses ``monkeypatch.setattr`` (subagent round 1 HIGH): pytest restores
    automatically on test teardown including under exceptions, where the
    prior bare ``try/finally`` could miss restoration on ``BaseException``.
    """
    import platform as _platform

    from kling_gui.tabs.face_crop_tab import _platform_face_repair_recovery_hint

    monkeypatch.setattr(_platform, "system", lambda: "Windows")
    win = _platform_face_repair_recovery_hint()
    assert "run_gui.bat" in win
    assert "deps_*.ok" in win, "Windows hint must mention the deps_*.ok stamp deletion"

    monkeypatch.setattr(_platform, "system", lambda: "Darwin")
    mac = _platform_face_repair_recovery_hint()
    assert "run_gui.sh" in mac
    assert ".venv-macos" in mac

    monkeypatch.setattr(_platform, "system", lambda: "Linux")
    lin = _platform_face_repair_recovery_hint()
    assert "run_gui.sh" in lin


def test_recovery_hint_uses_del_not_rd_for_file_glob(monkeypatch):
    """The Windows hint must use ``del`` (file deletion) NOT ``rd`` (directory
    removal) for the ``deps_*.ok`` glob.

    Subagent round 1 CRITICAL: ``deps_*.ok`` is a file pattern; ``rd /S /Q``
    errors out with "The system cannot find the file specified." when the
    user copy-pastes the recovery hint — recreating the exact dead-end the
    PR is fixing. This test pins the correct command verb.
    """
    import platform as _platform

    from kling_gui.tabs.face_crop_tab import _platform_face_repair_recovery_hint

    monkeypatch.setattr(_platform, "system", lambda: "Windows")
    win = _platform_face_repair_recovery_hint()
    assert "del /Q" in win or "del " in win, (
        f"Windows hint must use `del` for file glob; got: {win!r}"
    )
    assert "rd /S /Q .launcher_state" not in win and "rd /S /Q deps_" not in win, (
        f"Windows hint uses `rd` for a file glob — see subagent CRITICAL round 1. Hint: {win!r}"
    )


def test_macos_recovery_hint_health_stamp_path_matches_run_gui_sh(monkeypatch):
    """The macOS recovery hint MUST point at the same HEALTH_STAMP path that
    ``run_gui.sh`` actually invalidates.

    Subagent round 1 CRITICAL: the original hint pointed at
    ``.launcher_state/health.sha256``, but ``run_gui.sh:7`` declares
    ``HEALTH_STAMP="${ROOT_DIR}/.venv-macos/.health.sha256"``. ``rm -f`` on
    the non-existent path silently no-ops; the actual stamp keeps short-
    circuiting on the next launch — recreating the loop the PR is fixing,
    on the macOS side this time. This test catches future drift between
    the hint and the actual stamp path.
    """
    import platform as _platform

    from kling_gui.tabs.face_crop_tab import _platform_face_repair_recovery_hint

    monkeypatch.setattr(_platform, "system", lambda: "Darwin")
    mac = _platform_face_repair_recovery_hint()

    # Grep `run_gui.sh` for the authoritative HEALTH_STAMP path component.
    sh_src = MAC_SH.read_text(encoding="utf-8")
    # Source defines HEALTH_STAMP="${ROOT_DIR}/.venv-macos/.health.sha256"
    # — the path component after ${ROOT_DIR}/ is what the hint must reference.
    assert '${ROOT_DIR}/.venv-macos/.health.sha256' in sh_src, (
        "run_gui.sh's HEALTH_STAMP definition changed — update this test "
        "AND the macOS recovery hint together."
    )
    assert '.venv-macos/.health.sha256' in mac, (
        f"macOS recovery hint must reference the real HEALTH_STAMP path "
        f"(.venv-macos/.health.sha256). Got: {mac!r}"
    )


def test_launcher_manual_recovery_pip_command_matches_repair_packages():
    """Both launcher manual-recovery blocks must list EVERY pinned package
    from ``dependency_health_check.REPAIR_PACKAGES`` — drift between the
    auto-repair contract and the human-readable manual recovery hint
    silently introduces version mismatches.

    CodeRabbit round 1 Major: the Windows BAT's manual recovery only
    listed tensorflow + tf-keras + retina-face, but REPAIR_PACKAGES also
    pins protobuf, deepface, and (Windows-only) tensorflow-intel. A user
    who followed the manual recovery would end up with a different state
    than ``--mode repair`` produces.
    """
    from dependency_health_check import REPAIR_PACKAGES

    bat_src = _read(WIN_BAT)
    sh_src = _read(MAC_SH)

    for pkg in REPAIR_PACKAGES:
        # Manual recovery on Windows mirrors REPAIR_PACKAGES exactly,
        # including the win32 conditional `tensorflow-intel==2.16.2`.
        assert pkg in bat_src, (
            f"Windows BAT manual recovery missing REPAIR_PACKAGES entry: {pkg!r}. "
            f"Drift between auto-repair and manual hint silently introduces "
            f"version mismatches when users follow the manual fallback."
        )

    # The macOS sh manual recovery skips Windows-only tensorflow-intel.
    for pkg in REPAIR_PACKAGES:
        if "tensorflow-intel" in pkg:
            continue
        assert pkg in sh_src, (
            f"run_gui.sh manual recovery missing REPAIR_PACKAGES entry: {pkg!r}"
        )


def test_face_crop_tab_recovery_hints_have_no_backticks(monkeypatch):
    """Hint strings must be literal copy-pasteable text.

    CodeRabbit round 1 Major: original hints wrapped
    ``dependency_health_check.py --mode repair`` in backticks. Bash/zsh
    interpret backticks as command substitution; a user copy-pasting
    the hint into their shell would get a NameError or "command not
    found" before pip ever ran. No backticks.
    """
    import platform as _platform

    from kling_gui.tabs.face_crop_tab import _platform_face_repair_recovery_hint

    for system_name in ("Windows", "Darwin", "Linux"):
        monkeypatch.setattr(_platform, "system", lambda s=system_name: s)
        hint = _platform_face_repair_recovery_hint()
        assert "`" not in hint, (
            f"{system_name} hint contains a backtick — bash/zsh would interpret "
            f"it as command substitution and break copy-paste. Hint: {hint!r}"
        )


def test_face_crop_tab_error_log_no_longer_loops_on_relaunch():
    """The Face Crop import-failure log must NOT instruct users to 're-run
    the launcher' alone — that's the friend's infinite loop. Instead it
    must point at the new recovery hint (which the launcher's runtime
    health probe will satisfy automatically on next launch, but if the
    user is bypassing the launcher, the hint provides manual steps).
    """
    src = (REPO_ROOT / "kling_gui" / "tabs" / "face_crop_tab.py").read_text(encoding="utf-8")
    assert "Run run_gui.bat for automatic dependency repair" not in src, (
        "Old misleading toast still present. That message created the infinite "
        "re-run loop the friend got stuck in. Use _platform_face_repair_recovery_hint() "
        "for actionable manual steps instead."
    )
    assert "_platform_face_repair_recovery_hint" in src
