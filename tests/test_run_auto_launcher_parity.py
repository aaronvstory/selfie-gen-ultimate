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
    """run_auto.command must be mode 100755 in the index so macOS users can
    double-click it straight out of a fresh clone / extracted zip."""
    import subprocess

    out = subprocess.run(
        ["git", "ls-files", "--stage", "run_auto.command"],
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
    ).stdout.strip()
    assert out, "run_auto.command is not tracked by git"
    mode = out.split()[0]
    assert mode == "100755", f"run_auto.command must be exec (100755), got {mode}"
