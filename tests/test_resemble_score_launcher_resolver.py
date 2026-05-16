"""Static-text guards on the resemble-score launchers' Python resolver.

Mirrors tests/test_similarity_launcher_resolver.py. resemble-score is a
standalone subproject with its own four launchers; per CLAUDE.md macOS Rules
9 & 10 every venv candidate must be version-gated, .venv311 must be tried
ahead of .venv, python3.11 must lead the macOS fallback chain, the
post-resolve gate must remain as defense-in-depth, and sibling .command
files must share `set -euo pipefail`.

These are static-text regex assertions (no subprocess) so they run fast
under the repo-root pytest and catch resolver-pattern regressions.
"""
import re
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]

VERSION_PROBE_BASH = r"\(3, ?9\) <= sys\.version_info\[:2\] < \(3, ?13\)"
VERSION_PROBE_BAT = r"\(3,9\) <= sys\.version_info\[:2\] < \(3,13\)"

MACOS = ("resemble-score/run_gui.command", "resemble-score/run_cli.command")
BATS = ("resemble-score/run_gui.bat", "resemble-score/run_cli.bat")


def _read(path: str) -> str:
    return (REPO_ROOT / path).read_text(encoding="utf-8")


def test_macos_launchers_validate_each_candidate():
    for path in MACOS:
        src = _read(path)
        assert "_python_supported()" in src, f"{path}: helper missing"
        # 4 candidate venvs + 2 auto-create guards + post-resolve gate.
        assert src.count("_python_supported") >= 7, (
            f"{path}: _python_supported count="
            f"{src.count('_python_supported')} (<7) — every venv candidate "
            "must be version-gated or a stale wrong-version venv is returned."
        )


def test_macos_launchers_include_venv311_candidate():
    for path in MACOS:
        assert ".venv311" in _read(path), (
            f"{path}: missing .venv311 candidate (CLAUDE.md Rule 6/9)"
        )


def test_macos_launchers_prefer_python311_in_fallback_chain():
    for path in MACOS:
        src = _read(path)
        idx_311 = src.find("command -v python3.11")
        idx_312 = src.find("command -v python3.12")
        assert idx_311 > 0 and idx_312 > 0, (
            f"{path}: fallback chain must reference python3.11 and python3.12"
        )
        assert idx_311 < idx_312, (
            f"{path}: python3.12 precedes python3.11 — Homebrew python3.12+ "
            "ships without _tkinter, breaking the GUI launcher."
        )


def test_macos_launchers_venv311_precedes_venv_in_local_fallback():
    """CodeRabbit caught this on similarity/v14: .venv311 must be tried
    before .venv in the SCRIPT_DIR fallback (Rule 9)."""
    for path in MACOS:
        src = _read(path)
        idx_311 = src.find('".venv311/bin/python"')
        idx_venv = src.find('".venv/bin/python"')
        assert idx_311 > 0 and idx_venv > 0, f"{path}: local fallbacks missing"
        assert idx_311 < idx_venv, (
            f"{path}: .venv local fallback precedes .venv311 (Rule 9)"
        )


def test_macos_launchers_keep_post_resolve_gate():
    for path in MACOS:
        src = _read(path)
        assert re.search(VERSION_PROBE_BASH, src), (
            f"{path}: post-resolve version expression missing"
        )
        assert "SELFIEGEN_PYTHON" in src
        assert "requires 3.9-3.12" in src or "supported range 3.9-3.12" in src


def test_macos_launchers_guard_auto_create_venv():
    for path in MACOS:
        assert "brew install python@3.11" in _read(path), (
            f"{path}: missing auto-create guard — an unsupported `command -v` "
            "python would silently create a broken venv."
        )


def test_macos_launchers_use_euo_pipefail_parity():
    """CLAUDE.md Rule 10: new sibling .command files must share
    `set -euo pipefail` from the start."""
    for path in MACOS:
        src = _read(path)
        assert "set -euo pipefail" in src, (
            f"{path}: missing `set -euo pipefail` (Rule 10 sibling parity)"
        )


def test_windows_bats_use_check_py_subroutine():
    for path in BATS:
        src = _read(path)
        assert ":check_py" in src, f"{path}: missing :check_py subroutine"
        # SELFIEGEN_VENV_DIR, root venv, root .venv311, root .venv,
        # local .venv311, local .venv, + 1 post-create check. >=5 safe lower bound.
        assert src.count("call :check_py") >= 5, (
            f"{path}: call :check_py count={src.count('call :check_py')} (<5)"
        )


def test_windows_bats_include_venv311_candidate():
    for path in BATS:
        assert ".venv311" in _read(path), (
            f"{path}: Windows launcher missing .venv311 candidate"
        )


def test_windows_bats_keep_post_resolve_gate():
    for path in BATS:
        src = _read(path)
        assert re.search(VERSION_PROBE_BAT, src), (
            f"{path}: post-resolve version probe missing from .bat"
        )
        assert "SELFIEGEN_PYTHON" in src


def test_all_four_launchers_share_supported_range():
    seen = set()
    for path in MACOS + BATS:
        src = _read(path)
        m = re.search(r"\(3, ?9\) <= sys\.version_info\[:2\] < \(3, ?13\)", src)
        assert m, f"{path}: no version-range expression found"
        seen.add(re.sub(r"\s+", "", m.group(0)))
    assert len(seen) == 1, f"version range diverged across launchers: {seen}"
