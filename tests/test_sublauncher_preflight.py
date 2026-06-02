"""v2.17: guard that every sub-project launcher (oldcam / similarity /
resemble) calls the canonical shared-venv preflight helper BEFORE its own
minimal install. Without the preflight, a sub-launcher can run against a
half-complete shared venv and fail with a weird ImportError instead of one
canonical repair (review feedback 2026-06-02, "Gipps").

Source-text guards (the established style for launcher invariants — see
tests/test_launcher_health_check_loop.py).
"""
from __future__ import annotations

import glob
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent


def _read(rel: str) -> str:
    return (REPO_ROOT / rel).read_text(encoding="utf-8", errors="replace")


def test_preflight_helpers_exist():
    assert (REPO_ROOT / "scripts" / "win_preflight_shared_venv.bat").is_file()
    assert (REPO_ROOT / "scripts" / "preflight_shared_venv.sh").is_file()


def test_win_preflight_helper_runs_canonical_health_check():
    src = _read("scripts/win_preflight_shared_venv.bat")
    # Must invoke the canonical health script in BOTH check and repair modes.
    assert "dependency_health_check.py" in src
    assert "--mode check" in src
    assert "--mode repair" in src
    # Best-effort: must honour the opt-out + never hard-fail the caller.
    assert "SELFIEGEN_SKIP_PREFLIGHT" in src
    # No POSIX /dev/null leaked into a .bat (the linter trap).
    assert "/dev/null" not in src
    # Codex P2: only repair the SHARED root venv, not a SELFIEGEN_PYTHON
    # override / local venv. Must gate on a shared-venv match before repair.
    assert "_PF_SHARED" in src and "venv\\Scripts\\python.exe" in src
    assert "if not defined _PF_SHARED goto :_pf_done" in src


def test_sh_preflight_helper_runs_canonical_health_check():
    src = _read("scripts/preflight_shared_venv.sh")
    assert "dependency_health_check.py" in src
    assert "--mode check" in src
    assert "--mode repair" in src
    assert "SELFIEGEN_SKIP_PREFLIGHT" in src
    # The function the launchers source + call.
    assert "selfiegen_preflight_shared_venv()" in src
    # Codex P2: only repair the SHARED root venv (.venv-macos/venv/.venv*),
    # not a user override / local env. Must gate before repair.
    assert ".venv-macos" in src and "_shared" in src


_OLDCAM_BATS = sorted(glob.glob(str(REPO_ROOT / "oldcam-v*" / "oldcam_launcher.bat")))
_OLDCAM_CMDS = sorted(glob.glob(str(REPO_ROOT / "oldcam-v*" / "macOS" / "oldcam.command")))
_SIMRES_BATS = [
    "similarity/run_gui.bat", "similarity/run_cli.bat",
    "resemble-score/run_gui.bat", "resemble-score/run_cli.bat",
]
_SIMRES_CMDS = [
    "similarity/run_gui.command", "similarity/run_cli.command",
    "resemble-score/run_gui.command", "resemble-score/run_cli.command",
]


def test_found_all_oldcam_launchers():
    # Floor guard (code-review M4): 10 oldcam versions ship today, but a magic
    # ==10 breaks the instant v25 is added. Assert a non-zero floor + equal
    # .bat/.command counts (every version must ship BOTH); the per-launcher
    # parametrized tests below verify each found file calls the preflight.
    assert len(_OLDCAM_BATS) >= 10, _OLDCAM_BATS
    assert len(_OLDCAM_CMDS) == len(_OLDCAM_BATS), (
        f"each oldcam version needs BOTH a .bat and a .command: "
        f"{len(_OLDCAM_BATS)} bats vs {len(_OLDCAM_CMDS)} commands"
    )


@pytest.mark.parametrize("bat", _OLDCAM_BATS + [str(REPO_ROOT / p) for p in _SIMRES_BATS])
def test_windows_sublauncher_calls_preflight(bat):
    src = Path(bat).read_text(encoding="utf-8", errors="replace")
    assert "win_preflight_shared_venv.bat" in src, (
        f"{bat}: missing the shared-venv preflight call — a partial shared "
        f"venv would launch into an ImportError instead of one canonical repair."
    )


@pytest.mark.parametrize("cmd", _OLDCAM_CMDS + [str(REPO_ROOT / p) for p in _SIMRES_CMDS])
def test_macos_sublauncher_calls_preflight(cmd):
    src = Path(cmd).read_text(encoding="utf-8", errors="replace")
    assert "preflight_shared_venv.sh" in src, f"{cmd}: missing preflight source"
    assert "selfiegen_preflight_shared_venv" in src, f"{cmd}: missing preflight call"
