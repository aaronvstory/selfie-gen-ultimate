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


def test_windows_recovery_pip_uses_caret4_for_line_continuation_inside_block():
    """Inside `()` parenthesized blocks, the Windows batch parser runs two
    passes. ``^^`` collapses to ``^`` during parse-1, then execute-1 sees
    ``echo foo ^`` and treats the surviving ``^`` as line-continuation,
    printing NOTHING. To emit a literal ``^`` for the user (so the multi-
    line `pip install` command they copy-paste is actually a valid multi-
    line shell command), you need ``^^^^``.

    Gemini PR #55 round 2 HIGH: the previous ``^^`` would cause the user
    to see a broken pip command with no line-continuation carets, making
    the manual recovery hint un-copy-pasteable.
    """
    src = _read(WIN_BAT)
    # Find both manual-recovery blocks (cached-stamp path + fresh-install path).
    # Each ends with `deepface==0.0.92` after a line-continuation chain.
    # Assert that the pip-install line continuation uses ^^^^ not ^^.
    pip_line_idx = src.find('-m pip install --force-reinstall ^')
    assert pip_line_idx > 0, "Couldn't find the pip install manual-recovery line"
    # Both blocks should use four-caret continuation.
    assert '-m pip install --force-reinstall ^^^^' in src, (
        "Inside `()` blocks Windows batch needs ^^^^ to print a literal ^; "
        "current code uses ^^ which renders as nothing after the parser's "
        "two passes. Manual recovery hint shipped to the user becomes a "
        "broken single-line pip command that fails."
    )
    # And the trailing wheel-spec lines.
    assert 'tensorflow==2.16.2 ^^^^' in src
    assert 'protobuf==4.25.3 ^^^^' in src
    assert 'retina-face==0.0.17 ^^^^' in src
    # No stray ^^ (two caret) line-continuations should remain.
    # (We allow ^^ in escape sequences like `^^(` but those don't end a line.)
    lines = src.splitlines()
    for i, line in enumerate(lines, start=1):
        # Look for `^^` followed by end-of-line (possibly trailing whitespace).
        stripped = line.rstrip()
        if stripped.endswith('^^') and not stripped.endswith('^^^^'):
            raise AssertionError(
                f"Line {i} ends with ^^ (two carets) — inside `()` blocks "
                f"this renders as nothing. Use ^^^^ for line-continuation: {line!r}"
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


def test_windows_launcher_first_run_dep_install_banner_present():
    """The Windows launcher must show a "FIRST-RUN DEP INSTALL — expect
    5 to 15 minutes" banner BEFORE the pip-install block on the full-sync
    path. The friend who kicked off this PR killed run_gui.bat at ~10min
    during the CUDA wheel download, thinking it was frozen — the banner
    sets expectations up front so the panic-kill doesn't happen.
    """
    src = _read(WIN_BAT)
    # Banner must appear AFTER the cached-stamp `goto :launch` and BEFORE
    # the first pip-install call. Locate both anchors and assert the
    # banner text falls between them.
    cached_path_end = src.find("goto :launch")
    assert cached_path_end > 0
    first_pip_install = src.find('-m pip install --upgrade pip')
    assert first_pip_install > cached_path_end
    between = src[cached_path_end:first_pip_install]

    assert "FIRST-RUN DEP INSTALL" in between, (
        "First-run dep-install banner missing from full-sync path. "
        "Users must be told to expect a 5-15 min wait so they don't "
        "kill the process thinking it's frozen."
    )
    assert "expect 5 to 15 minutes" in between
    assert "torch wheels" in between
    assert "subsequent launches skip" in between


def test_macos_launcher_first_run_dep_install_banner_present():
    """Symmetric requirement on `run_gui.sh` — banner before
    `setup_macos.sh` runs, gated on the absence of REQUIREMENTS_STAMP
    (so the banner only shows on actual first install, not every launch).
    """
    src = _read(MAC_SH)
    setup_call_idx = src.find('"${ROOT_DIR}/setup_macos.sh"')
    assert setup_call_idx > 0, "Couldn't find setup_macos.sh invocation in run_gui.sh"
    # Banner must appear somewhere before the setup call.
    pre_setup = src[:setup_call_idx]
    assert "FIRST-RUN DEP INSTALL" in pre_setup, (
        "First-run dep-install banner missing from run_gui.sh."
    )
    assert "expect 5 to 15 minutes" in pre_setup
    # And it must be gated on REQUIREMENTS_STAMP absence so it doesn't
    # show on every launch.
    assert "REQUIREMENTS_STAMP" in pre_setup, (
        "Banner must be conditional on REQUIREMENTS_STAMP absence — "
        "showing it every launch is noise."
    )
    # Sanity: banner block uses `[[ ! -f ... ]]` gate
    assert "[[ ! -f \"${REQUIREMENTS_STAMP}\" ]]" in pre_setup, (
        "Banner gate must use [[ ! -f \"${REQUIREMENTS_STAMP}\" ]] — "
        "the bash idiom checked elsewhere in run_gui.sh."
    )


def test_windows_launcher_writes_per_launch_diag_snapshot():
    """Each launch must record Python / pip / OS / GPU info to
    ``%LOG_FILE%`` so users have something concrete to attach when
    reporting issues (user's explicit ask: "proper logging each launch
    so we can diagnose issues easier"). Tests the static-text shape;
    runtime correctness verified by manual launch.
    """
    src = _read(WIN_BAT)
    # All four diag lines present
    assert "diag-py %DIAG_PY%" in src, (
        "Missing per-launch Python version diag line in Windows BAT"
    )
    assert "diag-pip %DIAG_PIP%" in src
    assert "diag-os %DIAG_OS%" in src
    assert "diag-gpu %DIAG_GPU%" in src
    # `nvidia-smi -L` is the canonical GPU detection on Windows nvidia.
    assert "nvidia-smi -L" in src
    # Each diag line must use `>>` to APPEND, not `>` to overwrite
    # (else later launches lose earlier diagnostic context).
    for marker in ("diag-py", "diag-pip", "diag-os", "diag-gpu"):
        # Find the echo line for this marker and check the redirect
        # operator is `>>"%LOG_FILE%"` before the echo (per CLAUDE.md
        # Windows hard rule 3).
        idx = src.find(f"echo [%LAUNCH_TS%] {marker}")
        assert idx > 0, f"Missing echo line for {marker}"
        line_start = src.rfind("\n", 0, idx) + 1
        line = src[line_start:idx + 80]
        assert '>>"%LOG_FILE%"' in line, (
            f"diag-{marker} echo must use `>>` before echo to append "
            f"(CLAUDE.md Win rule 3); got: {line!r}"
        )


def test_macos_launcher_writes_per_launch_diag_snapshot():
    """Symmetric requirement on `run_gui.sh` — diag-py / diag-pip /
    diag-os lines appended to the diagnostic log. GPU detection skipped
    on macOS (Apple Silicon / Intel Macs don't ship with nvidia-smi)."""
    src = _read(MAC_SH)
    assert "diag-py %s" in src
    assert "diag-pip %s" in src
    assert "diag-os %s" in src
    # uname -a is the canonical macOS OS-version probe
    assert "uname -a" in src
    # Appends, not overwrites
    assert ">> \"${LOCK_DIR}/launch.log\"" in src or ">>\"${LOCK_DIR}/launch.log\"" in src, (
        "macOS diag-snapshot must append to launch.log (>> redirect)"
    )


def test_mediapipe_pin_matches_across_launchers_and_requirements():
    """Mediapipe is special-cased in BOTH launchers (installed separately
    via ``--no-deps`` after the rest of requirements.txt syncs). Both
    launchers hardcode the pin, so a future bump of ``mediapipe==X`` in
    ``requirements.txt`` will silently leave the launchers installing
    the OLD version — a real drift hazard that's hard to catch at
    runtime because both versions import fine (the bug surfaces as
    "why is feature Y broken on macOS but not Windows?").

    Polish-sweep regression: assert the pinned version matches across
    all three sources. Future bumps must update all three together OR
    refactor to read from requirements.txt at install time (deferred).
    """
    import re

    req = (REPO_ROOT / "requirements.txt").read_text(encoding="utf-8")
    req_match = re.search(r"^mediapipe==([\d.]+)$", req, re.MULTILINE)
    assert req_match, "requirements.txt is missing the `mediapipe==X.Y.Z` pin"
    req_version = req_match.group(1)

    bat = (REPO_ROOT / "launchers" / "windows" / "run_gui.bat").read_text(encoding="utf-8")
    bat_match = re.search(r'MEDIAPIPE_SPEC=mediapipe==([\d.]+)', bat)
    assert bat_match, "BAT is missing the `MEDIAPIPE_SPEC=mediapipe==X.Y.Z` assignment"
    bat_version = bat_match.group(1)

    sh = (REPO_ROOT / "setup_macos.sh").read_text(encoding="utf-8")
    sh_match = re.search(r'mediapipe==([\d.]+)', sh)
    assert sh_match, "setup_macos.sh is missing the `mediapipe==X.Y.Z` pin"
    sh_version = sh_match.group(1)

    assert req_version == bat_version == sh_version, (
        f"mediapipe pin drift: requirements.txt={req_version}, "
        f"launchers/windows/run_gui.bat={bat_version}, "
        f"setup_macos.sh={sh_version}. Both launchers install mediapipe "
        f"separately via --no-deps; a mismatched pin silently installs "
        f"the wrong version. Update all three together or refactor to "
        f"read the version from requirements.txt at install time."
    )


def test_windows_bat_python_version_gate_has_no_unescaped_parens_in_if_block():
    """The Python version gate at launchers/windows/run_gui.bat lines 67-103
    lives inside `if not exist "%VENV_PYTHON%" ( ... )`. Inside an if-block,
    cmd's parser counts every `(` and `)` it sees to determine where the
    block ends — unquoted parens close the outer block early and the next
    character ( a `.` from the version-info string or message text) is then
    parsed at top-level, emitting "`. was unexpected at this time.`" and
    aborting the launcher before the diag snapshot ever runs.

    This bug was REPRODUCED on Windows 11 25H2 (build 26200) on 2026-05-28
    during the PR #55 Windows verification hand-off; the buggy form was
    introduced by commit 8c93641 ("pre-handoff Python version validation")
    which the macOS box could not test on Windows. The fix caret-escapes
    every `(`/`)` inside the version-probe args and the user-facing echo
    text.

    Mirrors tests/test_resemble_score_launcher_resolver.py's
    test_no_literal_parens_inside_no_python_error_echo, which only covered
    the resemble-score/ launcher pair — extending the check to the main
    launcher closes the regression gap that shipped this bug.
    """
    import re

    src = _read(WIN_BAT)
    # Locate the Python version gate block (begins at the first
    # `python -c "import sys; raise SystemExit(0 if` line).
    probe_idx = src.find('python -c "import sys; raise SystemExit')
    assert probe_idx > 0, "Couldn't find Python version probe in BAT"

    # Window of the gate: 30 lines worth around the probe.
    window_start = src.rfind('\n', 0, probe_idx) + 1
    # End at next top-level rem section (after the gate).
    window_end = src.find('\nrem ---', probe_idx)
    assert window_end > probe_idx, "Couldn't bound version-gate block"
    block = src[window_start:window_end]

    # 1. Every `(` and `)` inside the python -c "..." args must be `^(` / `^)`.
    #    The version probe string is "raise SystemExit(0 if (3,9) <= ... )".
    #    Each of these 5 parens needs ^ in front when inside an if-block.
    probe_line = next(
        (ln for ln in block.splitlines() if "raise SystemExit" in ln),
        "",
    )
    assert "^(" in probe_line and "^)" in probe_line, (
        "Python version probe inside `if not exist` block has unescaped "
        "parens: cmd's nested-block parser closes the outer block early, "
        "crashing the launcher with `. was unexpected at this time.` "
        "Caret-escape every ( and ) in the python -c args. "
        f"Offending line: {probe_line!r}"
    )

    # 2. Every echo line inside this if-block that references "mediapipe"
    #    or "3.13+" must caret-escape its parens too.
    for line in block.splitlines():
        stripped = line.strip()
        if not stripped.startswith("echo"):
            continue
        if "mediapipe wheels" in stripped or "3.13+" in stripped:
            # Must use ^( and ^) for any parens in the message.
            # Bare ( or ) without preceding ^ is a parser hazard.
            bare_paren = re.search(r"(?<!\^)[()]", stripped)
            assert bare_paren is None, (
                f"echo line inside `if not exist` block has unescaped paren "
                f"-> parser crashes the launcher. Caret-escape it. "
                f"Line: {stripped!r}"
            )


def test_windows_bat_validates_python_version_before_venv_create():
    """Subagent PR #55 pre-handoff HIGH: the Windows BAT must validate the
    system Python version BEFORE `python -m venv` runs. Mediapipe==0.10.35
    (pinned in requirements.txt) ships wheels for Python 3.9-3.12 only;
    on Python 3.13+, venv-creation succeeds but the later mediapipe
    install fails mid-sync with a less-actionable error.

    macOS setup_macos.sh validates the equivalent range. This regression
    test pins the parity so the gap can't sneak back in.
    """
    src = _read(WIN_BAT)
    # The Python version check uses the same pattern as
    # similarity/run_gui.bat:63 — sys.version_info tuple comparison.
    # Hand-off verification 2026-05-28: the parens inside the version-probe
    # string MUST be caret-escaped (`^(`/`^)`) because the probe lives inside
    # an `if not exist (...)` block and cmd's nested-block parser would
    # otherwise close the outer block early, crashing the launcher with
    # `. was unexpected at this time.` Accept either bare or caret-escaped
    # form here so this test pins the SEMANTIC contract (range gate exists);
    # the dedicated `test_windows_bat_python_version_gate_has_no_unescaped_parens_in_if_block`
    # below pins the caret-escape contract.
    assert (
        "(3,9) <= sys.version_info[:2] < (3,13)" in src
        or "(3, 9) <= sys.version_info[:2] < (3, 13)" in src
        or "^(3,9^) ^<= sys.version_info[:2] ^< ^(3,13^)" in src
    ), (
        "Windows BAT must run `sys.version_info` range check before venv "
        "creation. Pattern: `python -c \"import sys; raise SystemExit^(0 if "
        "^(3,9^) ^<= sys.version_info[:2] ^< ^(3,13^) else 2^)\"`"
    )
    # The check must come BEFORE the venv-creation block (else it runs
    # too late to prevent a broken venv from being created). Accept either
    # bare or caret-escaped form for the position lookup.
    version_check_idx = src.find("(3,9) <= sys.version_info[:2] < (3,13)")
    if version_check_idx < 0:
        version_check_idx = src.find("^(3,9^) ^<= sys.version_info[:2] ^< ^(3,13^)")
    venv_create_idx = src.find('python -m venv "%VENV_DIR%"')
    assert version_check_idx > 0 and venv_create_idx > version_check_idx, (
        "Python version check must appear BEFORE the venv-create call. "
        "Otherwise a wrong-version venv gets created, then later steps fail "
        "with confusing errors."
    )
    # Failure message must name the supported range
    assert "3.9-3.12" in src, (
        "Version-check failure message must name the supported range so "
        "users know what to install"
    )
    # Must point users at python.org for the install
    assert "python.org" in src, (
        "Version-check failure message must point users at python.org/downloads"
    )


def test_face_crop_deps_missing_status_surfaces_underlying_error():
    """Polish-sweep (PR #55): the dep-missing status toast must surface
    the actual ``FACE_DEPS_ERROR`` (typically cv2 or numpy ImportError)
    + the platform-specific recovery hint, instead of the old
    "(see warning)" indirection which forced users to hunt through
    scrollback to find the real failure reason.
    """
    src = (REPO_ROOT / "kling_gui" / "tabs" / "face_crop_tab.py").read_text(encoding="utf-8")
    # Old wording removed
    assert '"Face Crop deps missing (see warning)"' not in src, (
        "Old indirected error wording lingers — should be replaced with "
        "the specific exception detail in the toast."
    )
    # New wording present
    assert 'Face Crop deps missing: {err_detail}' in src, (
        "Status label must surface the underlying FACE_DEPS_ERROR detail"
    )
    # Recovery hint reuses the platform-specific helper
    assert '_platform_face_repair_recovery_hint()' in src, (
        "Dep-missing path must reuse the per-platform recovery hint helper"
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
