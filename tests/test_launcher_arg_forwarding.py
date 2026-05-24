"""Static-text guards on launcher arg forwarding (PR #49).

The 5 launcher links between user-double-click and ``gui_launcher.py`` must
each pass user args through. A regression at any single link silently drops
``--workspace NAME`` (and any future flag) and the GUI launches in default
workspace — defeating the isolation the user just asked for.

Each test reads the launcher file's raw bytes and asserts the forwarding
token (``"$@"`` for bash/sh/command, ``%*`` for batch) appears in the
EXPECTED place. Modeled on ``test_similarity_launcher_resolver.py``.
"""

from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]


def _read_text(path: str) -> str:
    """Read with universal-newline normalization so CRLF batch files work."""
    return (REPO_ROOT / path).read_text(encoding="utf-8")


def test_root_run_gui_command_forwards_args():
    """run_gui.command (root compat wrapper) must forward to launchers/run_gui.command."""
    src = _read_text("run_gui.command")
    assert 'exec "${TARGET}" "$@"' in src, (
        "root run_gui.command dropped arg forwarding — "
        "user --workspace flag would be lost at the first launcher link."
    )


def test_root_run_gui_bat_forwards_args():
    """run_gui.bat (root compat wrapper) must forward %* to launchers/run_gui.bat."""
    src = _read_text("run_gui.bat")
    assert 'call "%TARGET%" %*' in src, (
        "root run_gui.bat dropped %* forwarding — "
        "user --workspace flag would be lost at the first launcher link."
    )


def test_run_gui_sh_forwards_args_to_gui_launcher():
    """run_gui.sh (called by macOS canonical launcher) must forward "$@" to gui_launcher.py."""
    src = _read_text("run_gui.sh")
    assert 'exec "${PYTHON_BIN}" -u "${ROOT_DIR}/gui_launcher.py" "$@"' in src, (
        "run_gui.sh dropped arg forwarding into gui_launcher.py — "
        "argparse in gui_launcher.py would never see --workspace."
    )


def test_macos_canonical_run_gui_command_forwards_args():
    """launchers/macos/run_gui.command must forward "$@" to run_gui.sh."""
    src = _read_text("launchers/macos/run_gui.command")
    # The call line should have "$@" after run_gui.sh; check both quoted-string variants.
    assert '"${ROOT_DIR}/run_gui.sh" "$@"' in src, (
        "macOS canonical launcher dropped arg forwarding to run_gui.sh — "
        "args would be lost between the .command and the shell script."
    )


def test_windows_canonical_run_gui_bat_forwards_args():
    """launchers/windows/run_gui.bat must forward %* to gui_launcher.py."""
    src = _read_text("launchers/windows/run_gui.bat")
    assert '"%VENV_PYTHON%" -u "%GUI_SCRIPT%" %*' in src, (
        "Windows canonical launcher dropped %* forwarding to gui_launcher.py — "
        "argparse would never see --workspace."
    )


def test_windows_bat_has_no_dev_null():
    """Tripwire: a linter in some checkouts silently rewrites Windows `>nul` to
    POSIX `/dev/null` in .bat files (project memory: project_linter_dev_null_in_bat).
    That breaks every redirect on Windows. This test catches the substitution
    before it ships."""
    for path in ("run_gui.bat", "launchers/windows/run_gui.bat",
                 "run_cli.bat", "launchers/windows/run_cli.bat"):
        src = _read_text(path)
        assert "/dev/null" not in src, (
            f"{path}: contains '/dev/null' — the linter substituted POSIX redirects "
            f"into a Windows .bat. Fix via Python byte-level write + immediate git add."
        )


def test_macos_canonical_uses_setup_lock_serialization():
    """run_gui.sh holds a bootstrap mutex (PR #49) so concurrent launches
    don't race on pip/venv setup. The lock MUST be released before exec'ing
    the GUI so multiple windows can run concurrently."""
    src = _read_text("run_gui.sh")
    # Acquire pattern: mkdir-based atomic lock
    assert "mkdir " in src and "setup.lock" in src, (
        "run_gui.sh missing setup-lock mkdir-based acquire — "
        "concurrent first launches could corrupt the venv via pip races."
    )
    # Release before exec: rmdir must appear AFTER the dep-setup block
    # AND BEFORE the final exec gui_launcher.py
    lock_release_idx = src.rfind('rmdir "${LOCK_PATH}"')
    exec_idx = src.rfind('exec "${PYTHON_BIN}"')
    assert lock_release_idx > 0, "missing rmdir release of setup.lock"
    assert lock_release_idx < exec_idx, (
        "setup.lock release must happen BEFORE exec gui_launcher.py, "
        "otherwise the lock is held for the entire GUI lifetime and "
        "siblings never get to run."
    )


def test_windows_canonical_uses_setup_lock_serialization():
    """launchers/windows/run_gui.bat mirrors the macOS lock via md/rd."""
    src = _read_text("launchers/windows/run_gui.bat")
    # Acquire
    assert ':acquire_setup_lock' in src
    assert 'md "%SETUP_LOCK%"' in src, "missing md-based atomic lock acquire"


def test_windows_setup_lock_released_on_every_path_to_launch():
    """PR #49 H1 (review finding): the stamp-skip fast path uses ``goto :launch``
    which originally bypassed the ``rd`` release at the bottom of the file,
    leaking the lock for the entire GUI lifetime and blocking siblings forever.

    Verify EVERY ``goto :launch`` (and the fall-through to the ``:launch``
    label) is preceded by a ``rd /S /Q "%SETUP_LOCK%"`` release. Parses the
    .bat as plain text and walks line-by-line — a future regression that
    introduces a new ``goto :launch`` without a release will trip this guard.
    """
    src = _read_text("launchers/windows/run_gui.bat")
    lines = src.splitlines()

    # Find every "goto :launch" line and every ":launch" label line.
    # For each, walk backwards through the preceding lines (skipping comments
    # and blanks) until we either hit a `rd ... %SETUP_LOCK%` or a `:setup_lock_acquired`
    # label (acquire point — anything between this and the goto MUST have
    # released, OR the goto must be the release itself's fall-through).
    def _check_release_precedes(target_idx, context):
        for i in range(target_idx - 1, -1, -1):
            stripped = lines[i].strip().lower()
            if not stripped or stripped.startswith("rem"):
                continue
            if 'rd /s /q "%setup_lock%"' in stripped:
                return  # release found before target
            if stripped == ":setup_lock_acquired":
                # Acquired but no release between — bug.
                raise AssertionError(
                    f"{context}: reached :setup_lock_acquired without a "
                    f"`rd /S /Q \"%SETUP_LOCK%\"` release in between. "
                    f"This is the H1 regression — fast path leaks the lock."
                )
            # Otherwise keep walking back.
        raise AssertionError(
            f"{context}: no setup_lock release found above; lock would "
            f"leak indefinitely."
        )

    for idx, line in enumerate(lines):
        stripped = line.strip().lower()
        if stripped == "goto :launch":
            _check_release_precedes(idx, f"line {idx+1} (goto :launch)")
        elif stripped == ":launch":
            _check_release_precedes(idx, f"line {idx+1} (:launch label fall-through)")
