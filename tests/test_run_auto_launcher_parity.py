"""Static-text guards: the automation BATCH launchers delegate to the canonical
CLI launcher chain (so they inherit the full v2.17 dependency bootstrap) and
inject ``--batch``; the CLI chain forwards user args end-to-end.

Why this matters: ``run_auto.bat`` used to be a STANDALONE fossil with its own
primitive ``pip install -r requirements.txt`` (no ``-c constraints.txt``, no
mediapipe ``--no-deps`` + matplotlib runtime deps, no GPU torch selection, no
health-gated stamp). That meant the "fully automated batch" path silently:

* pulled numpy 2.x and broke TensorFlow (the recurring v2.10/v2.13/v2.16 bug),
* hit the mediapipe-matplotlib gap that forced ``-NORPPG`` every run,
* never GPU-accelerated rPPG/oldcam/similarity, and
* cached a broken venv as healthy forever (unconditional ``auto_*.ok`` stamp).

Converging it onto the canonical ``run_cli`` chain fixes all four at once. These
tests pin that the wrappers stay thin (delegate, not re-implement) so a future
edit can't silently re-introduce the divergent install.
"""

from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]


def _read_text(rel: str) -> str:
    # universal-newline read so CRLF .bat files compare cleanly
    return (REPO_ROOT / rel).read_text(encoding="utf-8")


# --------------------------------------------------------------------------- #
# run_auto wrappers delegate + inject --batch (not a standalone installer).   #
# --------------------------------------------------------------------------- #
def test_run_auto_bat_delegates_to_cli_chain_with_batch():
    src = _read_text("run_auto.bat")
    assert 'call "%TARGET%" --batch %*' in src, (
        "run_auto.bat must delegate to the canonical CLI chain with --batch %* "
        "(thin wrapper), not re-implement dependency install."
    )
    assert 'set "TARGET=%ROOT_DIR%launchers\\run_cli.bat"' in src, (
        "run_auto.bat must target launchers\\run_cli.bat (the canonical chain)."
    )


def test_run_auto_command_delegates_to_cli_chain_with_batch():
    src = _read_text("run_auto.command")
    assert 'exec "${TARGET}" --batch "$@"' in src, (
        "run_auto.command must delegate to the canonical CLI chain with "
        '--batch "$@" (thin wrapper), not re-implement dependency install.'
    )
    assert 'TARGET="${ROOT_DIR}/launchers/run_cli.command"' in src, (
        "run_auto.command must target launchers/run_cli.command (canonical chain)."
    )


def test_run_auto_bat_does_not_reimplement_pip_install():
    """A standalone `pip install -r requirements.txt` in run_auto.bat is the
    exact fossil we removed; its presence means the wrapper went divergent
    again (no constraints / no mediapipe --no-deps / no GPU torch)."""
    src = _read_text("run_auto.bat")
    assert "pip install" not in src.lower(), (
        "run_auto.bat re-introduced its own pip install — it must delegate to "
        "the canonical chain instead so constraints/mediapipe/GPU/health all apply."
    )


def test_run_auto_bat_no_dev_null():
    """Tripwire: the project's linter rewrites Windows `>nul` to POSIX
    `/dev/null` in .bat files (memory: project_linter_dev_null_in_bat)."""
    assert "/dev/null" not in _read_text("run_auto.bat")


# --------------------------------------------------------------------------- #
# CLI chain forwards user args (so --batch reaches kling_automation_ui.py).   #
# --------------------------------------------------------------------------- #
def test_root_run_cli_bat_forwards_args():
    assert 'call "%TARGET%" %*' in _read_text("run_cli.bat"), (
        "root run_cli.bat dropped %* forwarding — --batch would never reach the "
        "canonical launcher."
    )


def test_intermediate_run_cli_bat_forwards_args():
    """The middle link launchers/run_cli.bat must forward %* too -- it's in the
    --batch forwarding chain (run_auto.bat -> THIS -> windows/run_cli.bat) and a
    dropped %* here breaks --batch with no other test catching it (HIGH-pinned
    by code-review MEDIUM-5, PR #69)."""
    assert 'call "%ROOT_DIR%\\launchers\\windows\\run_cli.bat" %*' in _read_text("launchers/run_cli.bat"), (
        "launchers/run_cli.bat (intermediate wrapper) dropped %* forwarding."
    )


def test_windows_canonical_run_cli_bat_forwards_args():
    assert '"%VENV_PYTHON%" -u "%CLI_SCRIPT%" %*' in _read_text("launchers/windows/run_cli.bat"), (
        "launchers/windows/run_cli.bat dropped %* forwarding to "
        "kling_automation_ui.py — argparse would never see --batch."
    )


def test_root_run_cli_command_forwards_args():
    assert 'exec "${TARGET}" "$@"' in _read_text("run_cli.command"), (
        "root run_cli.command dropped arg forwarding."
    )


def test_macos_canonical_run_cli_command_forwards_args():
    assert '"${ROOT_DIR}/run_cli.sh" "$@"' in _read_text("launchers/macos/run_cli.command"), (
        "launchers/macos/run_cli.command dropped arg forwarding to run_cli.sh."
    )


def test_run_cli_sh_forwards_args_to_automation_ui():
    assert 'exec "${PYTHON_BIN}" -u "${ROOT_DIR}/kling_automation_ui.py" "$@"' in _read_text("run_cli.sh"), (
        "run_cli.sh dropped arg forwarding into kling_automation_ui.py — "
        "argparse would never see --batch."
    )


# --------------------------------------------------------------------------- #
# Exec bit on the macOS double-click entry.                                   #
# --------------------------------------------------------------------------- #
def test_run_auto_command_is_executable_in_git():
    """run_auto.command must be mode 100755 in the index so macOS users can run
    it straight out of a fresh clone / extracted zip.

    Resilient to environments without git / outside a repo (EAFP per Sourcery +
    Gemini MEDIUM, PR #69): prefer the git index mode, fall back to the on-disk
    exec bit on POSIX, and skip cleanly if neither is available."""
    import os
    import subprocess

    try:
        out = subprocess.run(
            ["git", "ls-files", "--stage", "run_auto.command"],
            cwd=REPO_ROOT,
            capture_output=True,
            text=True,
            errors="replace",  # Windows localized codepage safety (Gemini MEDIUM, PR #69)
        ).stdout.strip()
    except (FileNotFoundError, OSError, subprocess.SubprocessError):
        out = ""

    if out:
        mode = out.split()[0]
        assert mode == "100755", f"run_auto.command must be exec (100755), got {mode}"
        return

    # No git / not tracked here: on POSIX, check the on-disk exec bit instead.
    target = REPO_ROOT / "run_auto.command"
    if os.name == "posix" and target.exists():
        assert os.stat(target).st_mode & 0o111, "run_auto.command is not executable on disk"


# --------------------------------------------------------------------------- #
# Headless launchers must NOT pause/read on failure (cron/Task Scheduler hang).#
# --------------------------------------------------------------------------- #
def test_run_auto_bat_sets_noninteractive():
    src = _read_text("run_auto.bat")
    assert 'set "KLING_NONINTERACTIVE=1"' in src, (
        "run_auto.bat must set KLING_NONINTERACTIVE=1 so the delegated launcher "
        "chain skips its `pause` on failure (else an unattended batch hangs)."
    )


def test_run_auto_command_sets_noninteractive():
    src = _read_text("run_auto.command")
    assert "export KLING_NONINTERACTIVE=1" in src, (
        "run_auto.command must export KLING_NONINTERACTIVE=1 so the chain skips "
        "its `read -r -p` on failure (else an unattended launchd/cron job hangs)."
    )


def test_windows_cli_chain_pauses_are_guarded():
    """Every `pause` in the Windows CLI launcher chain must be guarded by
    KLING_NONINTERACTIVE so a headless --batch run never blocks on a keypress."""
    import re

    # Include the intermediate wrapper launchers/run_cli.bat -- it's in the
    # --batch chain (run_auto.bat -> THIS -> launchers/windows/run_cli.bat), so a
    # future `pause` creeping in there would hang a headless run too
    # (code-review M2, PR #69).
    for rel in ("run_cli.bat", "launchers/run_cli.bat", "launchers/windows/run_cli.bat"):
        src = _read_text(rel)
        for m in re.finditer(r"(?m)^([ \t]*)(.*\bpause\b.*)$", src):
            line = m.group(2).strip()
            # Allowed: the guarded form, or a comment mentioning pause.
            if line.startswith("rem") or line.startswith("::"):
                continue
            assert "if not defined KLING_NONINTERACTIVE pause" in line or "pause" not in re.sub(
                r"if not defined KLING_NONINTERACTIVE pause", "", line
            ), f"{rel}: unguarded `pause` -> headless batch would hang: {line!r}"


def test_macos_cli_chain_reads_are_guarded():
    """Every interactive `read -r -p` in the macOS CLI launcher chain must be
    guarded by KLING_NONINTERACTIVE."""
    for rel in ("run_cli.command", "launchers/macos/run_cli.command", "run_auto.command"):
        src = _read_text(rel)
        for raw in src.splitlines():
            line = raw.strip()
            if line.startswith("#"):
                continue
            if "read -r -p" in line:
                assert "KLING_NONINTERACTIVE" in line, (
                    f"{rel}: unguarded `read -r -p` -> headless batch would hang: {line!r}"
                )
