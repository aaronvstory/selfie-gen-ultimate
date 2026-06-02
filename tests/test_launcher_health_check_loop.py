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
RESOLVER_BAT = REPO_ROOT / "scripts" / "win_resolve_python.bat"
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
    # The block ends at the next top-level full-sync section. Anchor on the
    # unique-per-section rem comment rather than `goto :launch` — round-4
    # subagent (PR #55) LOW-2 caught that `find("goto :launch")` is brittle
    # to a future BAT refactor that adds an earlier `goto :launch` (e.g.
    # for an early-exit case), which would silently shrink the extracted
    # block and let the assertions pass vacuously. The
    # `rem --- Full dep sync` opener is unique to the section immediately
    # AFTER the cached-stamp block's `goto :launch` and is structurally
    # stable across edits. Symmetric with the
    # `test_windows_launcher_first_run_dep_install_banner_present` fix.
    stamp_block_end = src.find("rem --- Full dep sync", stamp_branch_start)
    assert stamp_block_end > stamp_branch_start, (
        "Couldn't locate end of stamp branch — `rem --- Full dep sync` "
        "anchor missing. If the BAT comment structure changed, update "
        "this anchor too."
    )
    stamp_block = src[stamp_branch_start:stamp_block_end]
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
    # Anchor on the unique full-sync rem-block comment for stability
    # (round-4 subagent LOW-2, symmetric with the banner test fix).
    stamp_block_end = src.find("rem --- Full dep sync", stamp_branch_start)
    assert stamp_block_end > stamp_branch_start
    stamp_block = src[stamp_branch_start:stamp_block_end]

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

    Gemini PR #55 round-2 MED (#3313646920, #3313836602): ``REPAIR_PACKAGES``
    is mutated at module load time based on ``sys.platform`` — on non-
    Windows runs, ``tensorflow-intel==2.16.2`` is NOT in the list, so a
    test that derives expected packages purely from the module variable
    silently skips checking the Windows BAT for ``tensorflow-intel``.
    Use a DUAL-GUARD approach: derive from the implementation for
    automatic coverage of future additions, AND hard-pin the expected
    Windows superset so the BAT is always checked for every Windows
    package regardless of the test host's platform.
    """
    from dependency_health_check import REPAIR_PACKAGES

    bat_src = _read(WIN_BAT)
    sh_src = _read(MAC_SH)

    # Hard-pinned expected supersets — these are the packages each
    # platform's manual recovery hint MUST mention, independent of the
    # test host's `sys.platform`. Synced from `dependency_health_check.py`
    # `REPAIR_PACKAGES` definition + the Windows `tensorflow-intel`
    # insertion. Updating REPAIR_PACKAGES requires updating these too —
    # the live-derived loop below catches *additions* automatically; this
    # hardcoded list catches the platform-conditional case.
    EXPECTED_WINDOWS_PACKAGES = [
        "numpy==1.26.4",  # pinned first so a --force-reinstall can't pull numpy 2.x
        "tensorflow==2.16.2",
        "tensorflow-intel==2.16.2",  # Windows-only, must be on the Win BAT
        "protobuf==4.25.3",
        "tf-keras==2.16.0",
        "retina-face==0.0.17",
        "deepface==0.0.92",
        # v2.17: scipy + absl-py joined REPAIR_PACKAGES (the complete runtime
        # set). mediapipe is NOT here — it's repaired via a dedicated --no-deps
        # step (see dependency_health_check.run_repair) and is intentionally
        # excluded from REPAIR_PACKAGES so pip can't pull its numpy-2.x deps.
        "scipy>=1.11,<2",
        "absl-py>=2.3,<3",
    ]
    EXPECTED_MACOS_PACKAGES = [
        pkg for pkg in EXPECTED_WINDOWS_PACKAGES if "tensorflow-intel" not in pkg
    ]

    # First guard: hard-pinned expected lists. These run regardless of the
    # test host's platform and catch the conditional-import case Gemini
    # flagged.
    for pkg in EXPECTED_WINDOWS_PACKAGES:
        assert pkg in bat_src, (
            f"Windows BAT manual recovery missing EXPECTED Windows package "
            f"{pkg!r}. This list is hardcoded ({__file__}) so the check "
            f"runs regardless of test host platform; update both this list "
            f"AND `dependency_health_check.REPAIR_PACKAGES` together."
        )
    for pkg in EXPECTED_MACOS_PACKAGES:
        assert pkg in sh_src, (
            f"run_gui.sh manual recovery missing EXPECTED macOS package "
            f"{pkg!r}. Same hardcoded-list rule as above."
        )

    # Second guard: live REPAIR_PACKAGES derivation. Catches any NEW
    # package added to the list at runtime even before the hardcoded
    # superset is updated. The two guards together = "no drift in either
    # direction" — addition catches → live check; removal/conditional
    # catches → hardcoded check.
    for pkg in REPAIR_PACKAGES:
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

    # Anti-circularity check: if EXPECTED_WINDOWS_PACKAGES diverges from
    # what REPAIR_PACKAGES would produce on a Windows host (live + the
    # tensorflow-intel insertion), the dual-guard becomes inconsistent.
    # Derive the "Windows-equivalent" set from REPAIR_PACKAGES + the
    # win32 conditional + compare.
    repair_pkgs_set = set(REPAIR_PACKAGES)
    if "tensorflow-intel==2.16.2" not in repair_pkgs_set:
        # We're on a non-Windows test host; add the Windows-only package
        # so the comparison is platform-agnostic.
        repair_pkgs_set.add("tensorflow-intel==2.16.2")
    assert repair_pkgs_set == set(EXPECTED_WINDOWS_PACKAGES), (
        f"EXPECTED_WINDOWS_PACKAGES in this test has diverged from "
        f"`dependency_health_check.REPAIR_PACKAGES` (+ Windows insertion). "
        f"hardcoded={set(EXPECTED_WINDOWS_PACKAGES)}, live+win={repair_pkgs_set}. "
        f"Update both lists together."
    )


def test_face_crop_tab_build_ui_static_warning_uses_recovery_hint():
    """The Face Crop tab renders a static dependency-missing warning label
    at the top of the tab when HAS_FACE_DEPS is False. Round-2 subagent
    MED finding (2026-05-28): the OLD label said "Auto-repair via
    run_gui.bat", which is exactly the message that created the friend's
    infinite re-run loop — re-running the launcher with a stale
    `deps_*.ok` stamp would silently skip the broken-dep check.

    Pin: the static label must reference the new recovery-hint helper,
    NOT a bare `run_gui.bat`/`run_gui.command`/`run_gui.sh` mention as
    the primary remediation. The recovery hint includes the
    stamp-delete step that breaks the loop.
    """
    src = (REPO_ROOT / "kling_gui" / "tabs" / "face_crop_tab.py").read_text(
        encoding="utf-8"
    )

    # Locate the _build_ui method.
    build_ui_idx = src.find("    def _build_ui(self):")
    assert build_ui_idx > 0, "Couldn't find _build_ui method"
    # Bound the search to just the dependency-warning block at the top
    # (next ~30 lines of _build_ui, before the source_frame).
    block_end = src.find("        # ── Source", build_ui_idx)
    assert block_end > build_ui_idx, "Couldn't bound the dep-warning block"
    block = src[build_ui_idx:block_end]

    # The dead recovery-launcher function must have been removed entirely
    # — leaving it as dead code invites a future caller to reintroduce
    # the broken pattern.
    assert "_platform_gui_repair_launcher" not in src, (
        "Dead function `_platform_gui_repair_launcher` should be removed "
        "after the round-2 migration to `_platform_face_repair_recovery_hint`. "
        "Otherwise a future caller might wire the broken pattern back in."
    )

    # The static warning must use the new recovery-hint helper.
    assert "_platform_face_repair_recovery_hint" in block, (
        "Face Crop tab's static dependency-missing warning label still uses "
        "the old recovery wording. Replace with `_platform_face_repair_recovery_hint()` "
        "so the static label carries the same stamp-delete step as the "
        "toast in _run_crop_internal. Round-2 subagent MED, PR #55."
    )

    # And the label TEXT (not just incidental references in comments) must
    # NOT use the old "Auto-repair via run_gui.*" phrasing that created the
    # friend's infinite re-run loop. Strip comments before checking so the
    # block can still discuss the old wording in code comments.
    code_lines = [
        ln for ln in block.splitlines()
        if not ln.lstrip().startswith("#")
    ]
    code_only = "\n".join(code_lines)
    forbidden = "Auto-repair via run_gui"
    assert forbidden not in code_only, (
        f"Face Crop tab's static warning still contains {forbidden!r} in "
        f"executable code (label text or string literal); "
        f"this is the wording that created the friend's infinite re-run loop. "
        f"Use the recovery-hint helper instead."
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
    # Banner must appear AFTER the cached-stamp block exits and BEFORE
    # the first pip-install call. Anchor on the comment that opens the
    # full-sync rem-block (`--- Full dep sync ---`) rather than the first
    # `goto :launch` occurrence — round-3 subagent (PR #55) L2 caught
    # that `find("goto :launch")` is brittle: any future BAT refactor
    # that adds an earlier `goto :launch` (e.g. for an early-exit case)
    # would shift the anchor and let the banner assertion pass vacuously
    # if the banner ended up outside the new window. The rem-block
    # comment is unique to the full-sync section and stable across edits.
    full_sync_anchor = src.find("rem --- Full dep sync")
    assert full_sync_anchor > 0, (
        "Couldn't find the `rem --- Full dep sync` anchor in the BAT — "
        "the test needs a stable per-section anchor; if the BAT comment "
        "structure changed, update this anchor too."
    )
    first_pip_install = src.find('-m pip install --upgrade pip', full_sync_anchor)
    assert first_pip_install > full_sync_anchor, (
        "Couldn't find pip-install after the full-sync anchor — the "
        "section structure may have changed."
    )
    between = src[full_sync_anchor:first_pip_install]

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
    """cmd's nested-block parser counts every `(`/`)` to find where an
    `if (...)` block ends; an unescaped paren inside the block closes it
    early and the launcher crashes with `. was unexpected at this time.`
    (REPRODUCED on Windows 11 25H2 during the PR #55 hand-off).

    As of the v2.9 auto-Python-bootstrap PR, the Python detection +
    version gate moved OUT of run_gui.bat's `if not exist "%VENV_PYTHON%"`
    block and into the shared resolver scripts/win_resolve_python.bat, which
    uses FLAT goto subroutines (no if/else paren-blocks) precisely so the
    (3,9)/(3,13) literals can never close an enclosing block. This test now
    guards that the resolver keeps every paren-bearing echo caret-escaped
    and routes the probe through the flat :pyres_check subroutine. The
    dedicated, more thorough resolver guards live in
    tests/test_win_python_resolver.py.
    """
    import re

    src = _read(RESOLVER_BAT)

    # The version probe must exist (the gate didn't vanish, it relocated).
    probe_line = next(
        (ln for ln in src.splitlines() if "raise SystemExit" in ln),
        "",
    )
    assert probe_line, "version probe missing from the shared resolver"

    # Every echo line with a literal ( or ) must caret-escape it. Exception:
    # `echo(` (no space) is the *safe* blank-line idiom — its `(` is part of
    # the command token, not a message paren — so exclude it.
    for line in src.splitlines():
        stripped = line.strip()
        if not stripped.lower().startswith("echo"):
            continue
        if stripped == "echo(":
            continue
        body = stripped[5:] if stripped.lower().startswith("echo(") else stripped[4:]
        bare_paren = re.search(r"(?<!\^)[()]", body)
        assert bare_paren is None, (
            f"resolver echo line has an unescaped paren -> parser hazard. "
            f"Caret-escape it. Line: {stripped!r}"
        )

    # The probe must live in a flat-goto :pyres_check (no if/else paren-block).
    m = re.search(r"(?m)^:pyres_check$", src)
    assert m, "resolver missing flat :pyres_check subroutine"
    block = src[m.start():]
    assert ") else (" not in block, (
        ":pyres_check reintroduced an if/else paren-block — the version-probe "
        "parens will prematurely close it (the `was unexpected at this time` "
        "crash)."
    )


def test_windows_bat_validates_python_version_before_venv_create():
    """The Windows path MUST validate the system Python version BEFORE a venv
    is created. Mediapipe==0.10.35 (pinned in requirements.txt) ships wheels
    for Python 3.9-3.12 only; on 3.13+ venv-creation succeeds but the later
    mediapipe install fails mid-sync with a less-actionable error.

    As of the v2.9 auto-Python-bootstrap PR this gate lives in the shared
    resolver scripts/win_resolve_python.bat (called by both run_gui.bat and
    run_cli.bat), and the resolver only runs `-m venv` AFTER resolving a
    version-gated interpreter. This test pins that contract on the resolver.
    """
    src = _read(RESOLVER_BAT)

    # Range gate present (bare cmd form — resolver uses flat subroutines so
    # no caret-escaping is needed there).
    assert "(3,9) <= sys.version_info[:2] < (3,13)" in src, (
        "shared resolver must version-gate candidates to 3.9-3.12 before use"
    )

    # The version gate must be applied BEFORE the venv is created. The
    # interpreter is resolved (and gated) higher up; `-m venv` runs only in
    # the create step that follows.
    gate_idx = src.find("(3,9) <= sys.version_info[:2] < (3,13)")
    venv_create_idx = src.find('-m venv "%VENV_DIR%"')
    assert gate_idx > 0 and venv_create_idx > gate_idx, (
        "version gate must appear before the `-m venv` create call so a "
        "wrong-version interpreter never builds the venv"
    )

    # Failure messaging must name the range + point at python.org.
    assert "3.9-3.12" in src, "resolver must name the supported range"
    assert "python.org" in src, "resolver must point users at python.org"

    # And the main launchers must actually delegate to it.
    gui = _read(WIN_BAT)
    assert "win_resolve_python.bat" in gui, (
        "run_gui.bat must call the shared resolver"
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


# ────────────────────────────────────────────────────────────────────
# v2.11: numpy-2.x re-entry guard — constraints file threaded through every
# pip install + stamp written only when the health probe confirms OK.
# Background: a fresh v2.10 Windows install pulled numpy 2.x (deepface's open
# `numpy>=1.14.0` upper bound + numpy 2.x win wheels), breaking TF 2.16.2 at
# Face Crop import. The numpy<2 cap lived ONLY in requirements.txt, so it
# governed one pip call but not the bootstrap, the --no-deps mediapipe install,
# or the repair. These tests pin the fix: -c constraints.txt on EVERY pip call.
# ────────────────────────────────────────────────────────────────────

CONSTRAINTS_FILE = REPO_ROOT / "constraints.txt"


def test_constraints_file_caps_numpy_and_opencv():
    """constraints.txt must cap numpy <2 and all opencv variants <4.12."""
    assert CONSTRAINTS_FILE.is_file(), "constraints.txt missing at repo root"
    txt = CONSTRAINTS_FILE.read_text(encoding="utf-8")
    assert "numpy>=1.26,<2" in txt or "numpy<2" in txt, (
        "constraints.txt must cap numpy <2 (TF 2.16.2 breaks under numpy 2.x)"
    )
    for variant in ("opencv-python", "opencv-python-headless", "opencv-contrib-python"):
        assert f"{variant}<4.12" in txt, (
            f"constraints.txt must cap {variant} <4.12 (4.12+ declares numpy>=2)"
        )


@pytest.mark.parametrize(
    "bat_rel",
    ["launchers/windows/run_gui.bat", "launchers/windows/run_cli.bat"],
)
def test_windows_launchers_pass_constraints_to_every_pip_install(bat_rel):
    """Every project-dep `pip install` in BOTH Windows launchers must carry the
    constraints flag so a transitive resolve (deepface→numpy) can't upgrade
    numpy past 1.x. The flag is the guarded `!CC!` variable, set once per
    :INSTALL_REQUIREMENTS to `-c "%CONSTRAINTS_FILE%"` only when the file exists
    (GPT review, PR #65 — graceful when constraints.txt is absent)."""
    src = (REPO_ROOT / bat_rel).read_text(encoding="utf-8")
    assert r'CONSTRAINTS_FILE=%ROOT_DIR%\constraints.txt' in src, (
        f"{bat_rel} must define CONSTRAINTS_FILE pointing at constraints.txt"
    )
    # The guard: CC is set to the (single-quoted, space-safe) -c flag only if
    # the constraints file exists.
    assert 'if exist "%CONSTRAINTS_FILE%" set "CC=-c "%CONSTRAINTS_FILE%""' in src, (
        f"{bat_rel} must define the guarded CC constraints flag"
    )

    # Every pip-install line that installs project deps (excludes `--upgrade pip`
    # self-update and the manual-recovery ECHO lines which are literal text).
    install_lines = [
        ln.strip()
        for ln in src.splitlines()
        if "-m pip install" in ln
        and "--upgrade pip" not in ln
        and not ln.lstrip().lower().startswith("echo")
    ]
    assert install_lines, f"No real pip-install lines found in {bat_rel}"
    for ln in install_lines:
        assert "!CC!" in ln or '-c "%CONSTRAINTS_FILE%"' in ln, (
            f"{bat_rel}: pip install line missing the constraints flag: {ln!r}"
        )


def test_windows_launcher_gates_stamp_on_health_ok():
    """The deps_*.ok stamp must be written ONLY when health is confirmed OK.

    The v2.10 bug: the stamp was written unconditionally after the health
    block, so a venv where numpy 2.x was re-pulled (probe FAILED but repair
    re-verify subprocess passed, then a later resolve re-broke it) got cached
    as healthy. Now an explicit HEALTH_OK flag guards the write.
    """
    src = _read(WIN_BAT)
    assert 'set "HEALTH_OK="' in src, "Launcher must initialise a HEALTH_OK flag"
    assert 'set "HEALTH_OK=1"' in src, "Launcher must set HEALTH_OK on a clean probe/repair"
    assert "if defined HEALTH_OK (" in src, (
        "Stamp write must be guarded by `if defined HEALTH_OK`"
    )
    # The stamp write must sit inside the HEALTH_OK guard, before the else.
    guard_idx = src.find("if defined HEALTH_OK (")
    stamp_write_idx = src.find('>>"%STAMP%" echo %LAUNCH_TS%', guard_idx)
    else_idx = src.find(") else (", guard_idx)
    assert guard_idx > 0 and stamp_write_idx > guard_idx, (
        "Stamp write must appear after the `if defined HEALTH_OK` guard"
    )
    assert else_idx > stamp_write_idx, (
        "Stamp write must be INSIDE the HEALTH_OK guard (before its else branch)"
    )


def test_setup_macos_passes_constraints_to_every_pip_install():
    """The macOS bootstrap must mirror the constraints threading via the
    set -u-safe CONSTRAINTS_ARG array (gemini MED, PR #65) — an existence-guarded
    array, expanded as "${CONSTRAINTS_ARG[@]+...}" so a missing constraints.txt
    degrades gracefully instead of erroring/aborting under set -u."""
    setup_sh = REPO_ROOT / "setup_macos.sh"
    src = setup_sh.read_text(encoding="utf-8")
    assert 'CONSTRAINTS_FILE="${ROOT_DIR}/constraints.txt"' in src, (
        "setup_macos.sh must define CONSTRAINTS_FILE"
    )
    assert "CONSTRAINTS_ARG=()" in src and 'CONSTRAINTS_ARG=(-c "${CONSTRAINTS_FILE}")' in src, (
        "setup_macos.sh must build a guarded CONSTRAINTS_ARG array"
    )
    install_lines = [
        ln.strip()
        for ln in src.splitlines()
        if "-m pip install" in ln and "--upgrade pip" not in ln
    ]
    assert install_lines, "No real pip-install lines found in setup_macos.sh"
    for ln in install_lines:
        assert '"${CONSTRAINTS_ARG[@]+"${CONSTRAINTS_ARG[@]}"}"' in ln, (
            f"setup_macos.sh pip install missing the set -u-safe constraints "
            f"array expansion: {ln!r}"
        )


def test_face_crop_tab_auto_invokes_in_app_repair():
    """The Face Crop import-failure path must auto-invoke the in-app repair
    (no terminal) instead of only printing a manual command."""
    src = (REPO_ROOT / "kling_gui" / "tabs" / "face_crop_tab.py").read_text(encoding="utf-8")
    assert "_attempt_in_app_repair" in src, (
        "Face Crop must call _attempt_in_app_repair() on dependency failure"
    )
    repair_mod = REPO_ROOT / "kling_gui" / "dependency_repair_dialog.py"
    assert repair_mod.is_file(), "kling_gui/dependency_repair_dialog.py must exist"
    rsrc = repair_mod.read_text(encoding="utf-8")
    assert "def run_face_stack_repair" in rsrc
    assert "run_repair" in rsrc and "verify_in_fresh_process" in rsrc, (
        "Repair dialog must call run_repair + verify_in_fresh_process"
    )
