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


def test_windows_release_setup_lock_retries_on_failure():
    """PR #51 round-3 (H1 from PR #49 round-3 subagent, finally addressed):
    the :release_setup_lock subroutine must retry the rd after a 2s sleep
    when the first attempt fails. Common cause is a transient handle held
    by Windows Defender / Search Indexer / Explorer during file scan.

    Without the retry, a single AV scan during dep-setup would leave the
    lock dir for the full 24h forfiles-stale window, blocking every sibling
    launch in between. Behavior verified manually on Windows by holding an
    open file handle inside the lock dir during the rd call.

    This test asserts the static structure:
      1. First rd attempt + early-exit on success
      2. ping -n 3 sleep (~2s)
      3. Retry rd + early-exit on success
      4. Log warning to %LOG_FILE% if still failing after retry
    """
    src = _read_text("launchers/windows/run_gui.bat")
    # Find the :release_setup_lock block
    label_idx = src.find('\n:release_setup_lock\n')
    assert label_idx > 0, "missing :release_setup_lock label"
    # Take a generous window after the label (subroutine ends at next :LABEL)
    next_label_idx = src.find('\n:', label_idx + 1)
    if next_label_idx < 0:
        next_label_idx = len(src)
    block = src[label_idx:next_label_idx]

    # Must contain at least 3 `rd` attempts (initial + 2 retries).
    # Round-3 review (subagent M1): bumped from 2 attempts to 3 because
    # Defender deep-scans can hold handles 5-30s; the original 2s window
    # was insufficient. New budget: 2s + 4s = 6s total before WARN.
    rd_count = block.count('rd /S /Q "%SETUP_LOCK%"')
    assert rd_count >= 3, (
        f":release_setup_lock has only {rd_count} `rd` attempts — H1 retry "
        f"regression. The subroutine must retry TWICE (initial + 2 retries) "
        f"with progressively longer sleeps (2s + 4s)."
    )
    # Must have at least 3 early-exit guards (one after each rd)
    early_exit_count = block.count('if not exist "%SETUP_LOCK%" exit /b 0')
    assert early_exit_count >= 3, (
        f"only {early_exit_count} early-exit guards — H1 regression. Each "
        f"rd attempt should be followed by `if not exist ... exit /b 0`."
    )
    # Must have both the 2s and 4s sleeps between attempts
    assert 'ping -n 3 127.0.0.1' in block, (
        "missing 2s sleep between rd attempts 1 and 2 — H1 regression"
    )
    assert 'ping -n 5 127.0.0.1' in block, (
        "missing 4s sleep between rd attempts 2 and 3 — H1 retry-budget "
        "regression (the round-3 subagent finding about Defender deep-scan "
        "holding handles 5-30s is the reason for the longer second sleep)"
    )
    # Must log a warning to LOG_FILE when retry also fails
    assert '%LOG_FILE%' in block, (
        "missing diagnostic log on persistent rd failure — user has no "
        "breadcrumb to find the stuck lock"
    )
    assert 'WARN' in block or 'warn' in block.lower(), (
        "log message doesn't carry a WARN tag"
    )


def test_windows_setup_lock_released_on_every_path_to_launch():
    """PR #49 H1 (round-1 review finding): the stamp-skip fast path uses
    ``goto :launch`` which originally bypassed the ``rd`` release at the
    bottom of the file, leaking the lock for the entire GUI lifetime and
    blocking siblings forever.

    Round 2 expanded the guard: it also walks every ``exit /b`` and
    ``goto :DEPENDENCY_FAIL`` (the six error-exit paths between
    :setup_lock_acquired and :launch that originally leaked the lock too
    — dep-bootstrap failures are exactly where first launches break, and
    a 24h stuck lock there would make the concurrent-workspaces feature
    unreachable from the documented entry point).

    Acceptable release forms (both end in the lock dir being gone):
      - direct ``rd /S /Q "%SETUP_LOCK%"`` inline
      - ``call :release_setup_lock`` subroutine call (round-2 refactor)
    """
    src = _read_text("launchers/windows/run_gui.bat")
    lines = src.splitlines()

    def _is_release(line: str) -> bool:
        s = line.strip().lower()
        return (
            'rd /s /q "%setup_lock%"' in s
            or 'call :release_setup_lock' in s
        )

    def _check_release_precedes(target_idx, context):
        for i in range(target_idx - 1, -1, -1):
            stripped = lines[i].strip().lower()
            if not stripped or stripped.startswith("rem"):
                continue
            if _is_release(lines[i]):
                return  # release found before target
            if stripped == ":setup_lock_acquired":
                # Acquired but no release between — bug.
                raise AssertionError(
                    f"{context}: reached :setup_lock_acquired without a "
                    f"`rd /S /Q \"%SETUP_LOCK%\"` or `call :release_setup_lock` "
                    f"release in between. This is the H1 regression."
                )
            # Otherwise keep walking back.
        raise AssertionError(
            f"{context}: no setup_lock release found above; lock would "
            f"leak indefinitely."
        )

    # Find the byte index of :setup_lock_acquired so we only guard paths
    # that come AFTER it (i.e. paths inside the locked region).
    acquired_line_idx = next(
        (i for i, ln in enumerate(lines) if ln.strip().lower() == ":setup_lock_acquired"),
        None,
    )
    assert acquired_line_idx is not None, "missing :setup_lock_acquired label"

    # Find the byte index of :launch (the label declaration, not the goto).
    launch_label_idx = next(
        (i for i, ln in enumerate(lines) if ln.strip().lower() == ":launch"),
        None,
    )
    assert launch_label_idx is not None, "missing :launch label"

    # All exit/goto-out points between :setup_lock_acquired and :launch
    # must release the lock first.
    for idx, line in enumerate(lines):
        if idx <= acquired_line_idx or idx >= launch_label_idx:
            continue
        stripped = line.strip().lower()
        # Match: `exit /b ...`, `goto :launch`, `goto :dependency_fail`
        if (
            stripped.startswith("exit /b")
            or stripped == "goto :launch"
            or stripped == "goto :dependency_fail"
        ):
            _check_release_precedes(idx, f"line {idx+1} ({stripped!r})")

    # Also check the :launch fall-through itself
    _check_release_precedes(launch_label_idx, f"line {launch_label_idx+1} (:launch fall-through)")

    # AND the :DEPENDENCY_FAIL block exit, even though it's outside the lock
    # region positionally — it's reachable via `goto` from inside. The
    # subroutine call inside it is the safety net.
    depfail_idx = next(
        (i for i, ln in enumerate(lines) if ln.strip().lower() == ":dependency_fail"),
        None,
    )
    if depfail_idx is not None:
        # Verify a release appears between :dependency_fail and the block's exit.
        block_lines = lines[depfail_idx:]
        # Find the first exit /b in this block
        rel_exit_idx = next(
            (i for i, ln in enumerate(block_lines) if ln.strip().lower().startswith("exit /b")),
            None,
        )
        assert rel_exit_idx is not None, ":DEPENDENCY_FAIL block has no exit"
        # Look for a release call in the lines leading up to that exit
        preceding = block_lines[: rel_exit_idx]
        assert any(_is_release(ln) for ln in preceding), (
            ":DEPENDENCY_FAIL block exits without releasing setup.lock — "
            "round-2 H-1 regression."
        )
