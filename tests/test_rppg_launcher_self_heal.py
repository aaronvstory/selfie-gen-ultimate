"""Static-text tests for rPPG/run_rppg.bat self-heal path.

Friend-zip incident 2026-05-27: the user shared a v2.6 personal zip
to a friend whose venv was built before scipy/mediapipe joined
requirements.txt. The wrapper's import-check block ECHOED a "Sync:"
command and EXITED rather than actually running it, so rPPG silently
failed on every launch with a dead-end error.

Per ChatGPT v2.7 steering #5: lock these properties of the .bat with
static-text assertions so a future refactor can't silently regress
back to the echo-only behaviour. We don't subprocess-run the .bat
(no point on a Linux CI runner) — the assertions check exact substrings
in the source.
"""
from __future__ import annotations

from pathlib import Path

import pytest

_BAT_PATH = Path(__file__).resolve().parent.parent / "rPPG" / "run_rppg.bat"


@pytest.fixture(scope="module")
def bat_source() -> str:
    """Read the .bat as text, normalising CRLF → LF so test patterns
    are simple. The eol invariant itself is verified by the macOS
    portability gate (scripts/check_macos_portability.sh) — not this
    test."""
    raw = _BAT_PATH.read_bytes()
    return raw.decode("ascii", errors="replace").replace("\r\n", "\n")


def test_missing_deps_actually_invokes_pip_install(bat_source: str):
    """The missing-deps branch MUST invoke pip via the resolved
    !PYTHON_BIN! — NOT merely echo a "Sync:" command. Friend-zip
    regression guard 2026-05-27.

    Updated PR #54: the self-heal now mirrors the launcher's MediaPipe
    contract (filter mediapipe out, install the rest, then mediapipe
    separately with --no-deps), so the pip target is the filtered req
    file rather than requirements.txt directly. The invariant under test
    is unchanged: pip is ACTUALLY run via the resolved interpreter."""
    assert '"!PYTHON_BIN!" -m pip install -r "%RPPG_REQ_FILTERED%"' in bat_source, (
        "self-heal branch must ACTUALLY run pip install via the "
        "resolved !PYTHON_BIN!. The friend-zip bug was that the prior "
        "block only echoed the sync command without running it."
    )


def test_self_heal_installs_mediapipe_no_deps(bat_source: str):
    """P1 (codex PR #54): MediaPipe MUST install with --no-deps
    (Hard Rule #6). Installing the full requirements.txt with normal
    dependency resolution lets pip pull MediaPipe's own deps and break
    the TF/protobuf/numpy stack. The self-heal must filter mediapipe out
    of the main install and install it separately, pinned + --no-deps —
    mirroring launchers/windows/run_gui.bat :INSTALL_REQUIREMENTS."""
    assert 'findstr /V /I /B "mediapipe"' in bat_source, (
        "self-heal must filter mediapipe out of the bulk pip install"
    )
    # The mediapipe spec is read dynamically FROM requirements.txt (not a
    # hardcoded literal) so the self-heal can't drift when the pin is bumped.
    assert 'findstr /I /R "^[ ]*mediapipe" "%REPO_ROOT%\\requirements.txt"' in bat_source, (
        "self-heal must read the mediapipe pin from requirements.txt"
    )
    assert '-m pip install --no-deps "!RPPG_MEDIAPIPE_SPEC!"' in bat_source, (
        "self-heal must install the dynamically-read mediapipe pin with "
        "--no-deps (Hard Rule #6)"
    )


def test_missing_deps_does_not_silently_exit_with_echo_only(bat_source: str):
    """Belt-test for the regression: ensure no `echo Sync: ... pip ...`
    followed immediately by `exit /b 1` without an intervening pip
    invocation. We grep for the prior bad pattern."""
    bad = (
        '  echo   ERROR: repo venv missing cv2/numpy/mediapipe/scipy.\n'
        '  echo   Sync:'
    )
    assert bad not in bat_source, (
        "the legacy echo-Sync-then-exit dead-end pattern must NOT be "
        "present anywhere in run_rppg.bat"
    )


def test_self_heal_uses_resolved_python_not_hardcoded_path(bat_source: str):
    """The self-heal pip command MUST use !PYTHON_BIN! (the resolved
    interpreter) — NOT a hardcoded %REPO_ROOT%\\venv\\Scripts\\pip.
    The hardcoded path doesn't honour SELFIEGEN_PYTHON overrides or
    fallback to .venv311 / .venv on hosts where the canonical venv
    isn't `venv/`."""
    # The hardcoded path appears in the ERROR-message echo above (for
    # human readability) — that's fine. But the EXECUTION must use
    # !PYTHON_BIN!. Grep for the bad pattern: pip with the hardcoded
    # path as an EXECUTABLE (i.e., first non-whitespace token of a line).
    for line in bat_source.splitlines():
        stripped = line.lstrip()
        if stripped.startswith('"%REPO_ROOT%\\venv\\Scripts\\pip"'):
            pytest.fail(
                "Self-heal must invoke pip via the resolved "
                "!PYTHON_BIN! (...-m pip install...), NOT via a "
                "hardcoded venv-relative pip executable. Offending "
                f"line: {line!r}"
            )


def test_self_heal_re_runs_import_check_after_pip(bat_source: str):
    """After the pip install, the .bat MUST re-run the import check.
    Without this, a partial-success pip (e.g. mediapipe install
    failed but the others worked) would still launch the injector
    which would crash on the missing module."""
    # Two consecutive import-check lines are the proxy for "checked,
    # tried to fix, re-checked." Count them.
    count = bat_source.count(
        '"!PYTHON_BIN!" -c "import cv2, numpy, mediapipe, scipy"'
    )
    assert count >= 2, (
        f"expected >=2 occurrences of the import check (one before "
        f"pip, one after self-heal + one after lock-wait fast-path), "
        f"found {count}"
    )


def test_self_heal_honours_kling_no_pause(bat_source: str):
    """The self-heal error paths use %PAUSE% (which expands to nothing
    when KLING_NO_PAUSE=1 is set by the GUI subprocess invoker).
    Belt-and-suspenders so a fresh-venv GUI launch never blocks on a
    hidden stdin."""
    assert "%PAUSE%" in bat_source
    assert "if defined KLING_NO_PAUSE" in bat_source


def test_self_heal_concurrency_lock_present(bat_source: str):
    """Two concurrent GUI launches must not both run pip against the
    shared venv. The self-heal uses a dedicated mkdir-based
    .launcher_state/rppg_setup.lock, separate from setup.lock (which
    is released before the GUI launches) and gpu_bootstrap.lock
    (different concern)."""
    assert "RPPG_SETUP_LOCK" in bat_source, (
        "concurrent rPPG launches must serialize pip install via a "
        "dedicated mkdir lock"
    )
    assert "rppg_setup.lock" in bat_source


def test_self_heal_lock_wait_is_bounded(bat_source: str):
    """HIGH/P2 (gemini+codex PR #54): the mkdir-lock acquire loop only
    cleared locks via `forfiles /D -1`, which matches locks >=1 DAY old.
    A lock left by a sibling that crashed earlier the SAME day would hang
    the loop forever. The loop MUST carry a retry counter that force-breaks
    the lock after a bounded wait and ultimately gives up rather than
    deadlocking the launcher."""
    assert "RPPG_LOCK_TRIES" in bat_source, (
        "lock-acquire loop must bound its wait with a retry counter so a "
        "same-day stale lock cannot deadlock the launcher"
    )
    assert "set /a RPPG_LOCK_TRIES+=1" in bat_source


def test_self_heal_diagnostic_lists_missing_modules(bat_source: str):
    """If pip succeeds but the imports still fail, the error message
    must name WHICH modules are still missing — not just repeat the
    generic 4-module list."""
    # The diagnostic uses importlib.util.find_spec to list missing
    # modules by name. Look for the find_spec usage.
    assert "importlib.util.find_spec" in bat_source, (
        "diagnostic must enumerate the specific missing modules; the "
        "single-line python -c uses importlib.util.find_spec"
    )


def test_no_dev_null_in_bat(bat_source: str):
    """Linter tripwire (per memory project_linter_dev_null_in_bat):
    a linter in this checkout silently rewrites Windows `>nul` to
    POSIX `/dev/null` on .bat files. Catches the regression at
    pytest time."""
    assert "/dev/null" not in bat_source, (
        "run_rppg.bat must use Windows `>nul` syntax, never POSIX "
        "`/dev/null` — a checkout-local linter silently rewrites the "
        "former to the latter and breaks every redirection in the file"
    )
