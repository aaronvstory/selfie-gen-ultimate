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
    # v2.11: pip install now carries -c constraints.txt (numpy-2 guard).
    assert '"!PYTHON_BIN!" -m pip install -c "%REPO_ROOT%\\constraints.txt" -r "%RPPG_REQ_FILTERED%"' in bat_source, (
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
    # v2.13: the mediapipe spec is read dynamically FROM requirements.txt via the
    # scripts/read_requirement_spec.py PARSER, NOT findstr. The old
    # `findstr /I /R /C:"^[ ]*mediapipe"` had its anchor carets mangled inside
    # the for/f backtick context and matched a COMMENT line ("# mediapipe ..."),
    # which pip then choked on (InvalidMarker), failing rPPG. The parser skips
    # comment lines and only returns the real `mediapipe==` requirement.
    assert "read_requirement_spec.py" in bat_source, (
        "self-heal must read the mediapipe spec via scripts/read_requirement_spec.py"
    )
    # The parser is invoked for the SPEC; a comment-matching findstr must NOT be
    # used for spec extraction anymore.
    assert 'findstr /I /R /C:^"^[ ]*mediapipe^"' not in bat_source, (
        "self-heal must NOT use the comment-matching findstr regex for the "
        "mediapipe SPEC (it matched a comment line — v2.13 bug). Use the parser."
    )
    # CRITICAL (code-review HIGH, PR #65): the for/f inner command MUST be
    # wrapped in `cmd /c "..."`. A bare caret-quoted first token (^"...^") makes
    # for/f's tokenizer error out and capture NOTHING, so the dynamic read
    # silently no-ops and only the fallback fires (pin would drift on a bump).
    # Verified live: the cmd /c wrapper captures `mediapipe==0.10.35`; the bare
    # form captures nothing. Pin the wrapper so the no-op can't regress.
    assert "for /f" in bat_source and "read_requirement_spec.py" in bat_source
    parser_line = next(
        ln for ln in bat_source.splitlines()
        if "for /f" in ln and "read_requirement_spec.py" in ln
    )
    assert "cmd /c" in parser_line, (
        "the for/f parser invocation must wrap the inner command in `cmd /c \"...\"` "
        f"or it captures nothing and the dynamic read no-ops. Got: {parser_line!r}"
    )
    # v2.11: --no-deps mediapipe install carries -c constraints.txt too.
    assert '-m pip install --no-deps -c "%REPO_ROOT%\\constraints.txt" "!RPPG_MEDIAPIPE_SPEC!"' in bat_source, (
        "self-heal must install the parsed mediapipe pin with --no-deps "
        "(Hard Rule #6)"
    )
    # And there must be a hard fallback so RPPG_MEDIAPIPE_SPEC is never empty.
    assert 'if not defined RPPG_MEDIAPIPE_SPEC set "RPPG_MEDIAPIPE_SPEC=mediapipe==0.10.35"' in bat_source, (
        "self-heal must fall back to mediapipe==0.10.35 if the parser returns nothing"
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
    """After the pip install, the .bat MUST re-verify imports. Without
    this, a partial-success pip (e.g. mediapipe install failed but the
    others worked) would still launch the injector which would crash on
    the missing module.

    v2.16: the pre-pip gate + lock-wait fast-path still use the inline
    `import ...` check, but the POST-self-heal re-verify is now done via
    `call :rppg_diag_tee` (which runs scripts/rppg_import_diag.py and
    branches on RPPG_DIAG_EXIT) so the failing module is named and logged.
    Assert BOTH layers exist."""
    # The pre-pip gate + the post-lock-wait fast-path use the inline check.
    inline_count = bat_source.count(
        '"!PYTHON_BIN!" -c "import cv2, numpy, mediapipe, scipy, absl"'
    )
    assert inline_count >= 2, (
        f"expected >=2 occurrences of the inline import check (the pre-pip "
        f"gate + the post-lock-wait fast-path), found {inline_count}"
    )
    # The post-self-heal re-verify is the granular diagnostic, branched on.
    assert "call :rppg_diag_tee" in bat_source, (
        "the post-self-heal re-verify must run the granular diagnostic via "
        ":rppg_diag_tee so the failing module is named"
    )
    assert "if !RPPG_DIAG_EXIT! neq 0 (" in bat_source, (
        "the .bat must branch on the diagnostic exit code (RPPG_DIAG_EXIT) "
        "so a still-broken import aborts before launching the injector"
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
    """If pip succeeds but the imports still fail, the launcher must name
    WHICH modules are still missing — not just repeat the generic list.

    v2.16: the inline `find_spec` one-liner (which printed to the console
    only and never to rppg.log, so the friend's pasted log showed no module
    name) is replaced by scripts/rppg_import_diag.py, invoked through
    :rppg_diag_tee which mirrors EVERY diagnostic line to BOTH the console
    and rppg.log. Assert the launcher calls the helper and tees its output."""
    assert "rppg_import_diag.py" in bat_source, (
        "the post-self-heal diagnostic must run scripts/rppg_import_diag.py "
        "to enumerate each module's OK/MISSING/BROKEN state by name"
    )
    # The tee subroutine must write the diagnostic to BOTH sinks: the
    # console (-> GUI stream) AND the log file (the sink the friend read).
    diag_body = bat_source.split(":rppg_diag_tee", 1)[-1].split(":rppg_sync_deps", 1)[0]
    assert 'type "!RPPG_DIAG_TMP!"' in diag_body, (
        "the diagnostic must echo to the console so the GUI subprocess "
        "stream captures it"
    )
    assert 'type "!RPPG_DIAG_TMP!" >>"%LOG_FILE%"' in diag_body, (
        "the diagnostic must ALSO append to rppg.log — the friend's v2.13 "
        "bug was that the per-module detail went to the console only and "
        "the rppg.log he pasted showed a useless 'Core imports missing'"
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


def test_self_heal_import_check_includes_absl(bat_source: str):
    """v2.15: rppg_injector.py does an `import absl.logging`, but mediapipe is
    installed --no-deps (skipping its absl-py~=2.3) so absl can be absent on a
    fresh venv. The self-heal import-check must include `absl` so a missing
    absl is detected + triggers the re-install, instead of passing and letting
    the injector crash → -NORPPG (the friend's 'rPPG fails, everything else
    works' bug)."""
    assert "import cv2, numpy, mediapipe, scipy, absl" in bat_source, (
        "run_rppg.bat self-heal import-check must include absl"
    )
    # The old check (without absl) must be gone.
    assert '"!PYTHON_BIN!" -c "import cv2, numpy, mediapipe, scipy" ' not in bat_source, (
        "the absl-less import-check must be replaced everywhere"
    )


def test_absl_pinned_in_requirements_and_constraints():
    """absl-py must be an explicit top-level pin (not just transitive) in both
    requirements.txt and constraints.txt so a fresh venv always installs it
    (v2.15)."""
    from pathlib import Path

    root = Path(__file__).resolve().parent.parent
    reqs = (root / "requirements.txt").read_text(encoding="utf-8")
    cons = (root / "constraints.txt").read_text(encoding="utf-8")
    assert "absl-py~=2.3" in reqs, "requirements.txt must pin absl-py explicitly"
    assert "absl-py>=2.3,<3" in cons, "constraints.txt must pin absl-py"


def test_rppg_injector_absl_import_is_guarded():
    """rppg_injector.py's absl import must be wrapped in try/except so a missing
    absl degrades to noisier logs instead of crashing the whole rPPG step
    (belt-and-suspenders for the v2.15 absl fix)."""
    from pathlib import Path

    src = (Path(__file__).resolve().parent.parent / "rPPG" / "rppg_injector.py").read_text(
        encoding="utf-8", errors="replace"
    )
    # The import + set_verbosity must sit inside a try block.
    assert "try:\n    import absl.logging" in src, (
        "rppg_injector.py must guard `import absl.logging` in a try/except"
    )
    assert "except Exception:" in src
