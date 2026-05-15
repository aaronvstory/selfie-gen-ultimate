"""Static-text guards on the similarity launchers' Python resolver.

Background: the macOS Similarity launcher previously short-circuited on
`[ -x "$candidate/bin/python" ]` without validating the interpreter version.
On a machine with a stale `.venv/` pointing at python3.14, the resolver returned
that bad python and the post-resolve gate rejected it with "Unsupported Python
version. Similarity requires Python 3.9-3.12." — confusing because supported
pythons WERE installed, just never tried.

These tests keep the per-candidate version probe in place and prevent regressions
on the candidate ordering (`.venv311` must appear, python3.11 must be the first
fallback per CLAUDE.md macOS rule).
"""
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]

VERSION_PROBE_RE = r"\(3, ?9\) <= sys\.version_info\[:2\] < \(3, ?13\)"


def _read(path: str) -> str:
    return (REPO_ROOT / path).read_text(encoding="utf-8")


def test_macos_gui_launcher_validates_each_candidate():
    """Every venv candidate in the macOS GUI launcher must be version-gated."""
    src = _read("similarity/run_gui.command")
    # Helper definition + invocations on each candidate + post-resolve gate.
    assert "_python_supported()" in src
    # 4 candidate venvs (root venv, root .venv311, root .venv, local .venv)
    # each call _python_supported, plus 2 auto-create guards, plus 1 gate.
    assert src.count("_python_supported") >= 7, (
        f"_python_supported invocations dropped (count={src.count('_python_supported')}); "
        "every candidate venv must be guarded or stale wrong-version venvs will be returned silently."
    )


def test_macos_cli_launcher_validates_each_candidate():
    src = _read("similarity/run_cli.command")
    assert "_python_supported()" in src
    assert src.count("_python_supported") >= 7


def test_macos_launchers_include_venv311_candidate():
    """`.venv311/` is the macOS-prescribed venv name (CLAUDE.md:86) — must be tried."""
    for path in ("similarity/run_gui.command", "similarity/run_cli.command"):
        src = _read(path)
        assert ".venv311" in src, f"{path}: missing .venv311 candidate (CLAUDE.md:86 prescribes this venv name on macOS)"


def test_macos_launchers_prefer_python311_in_fallback_chain():
    """CLAUDE.md:86 — only python3.11 ships with bundled _tkinter from Homebrew."""
    for path in ("similarity/run_gui.command", "similarity/run_cli.command"):
        src = _read(path)
        # Fallback chain pattern: command -v python3.11 || command -v python3.12 ...
        idx_311 = src.find("command -v python3.11")
        idx_312 = src.find("command -v python3.12")
        assert idx_311 > 0 and idx_312 > 0, f"{path}: expected fallback chain referencing python3.11 and python3.12"
        assert idx_311 < idx_312, (
            f"{path}: python3.12 appears before python3.11 in fallback chain — "
            "macOS Homebrew python3.12+ ships without _tkinter, breaking the GUI launcher."
        )


def test_macos_launchers_keep_post_resolve_gate():
    """Defense-in-depth: even after per-candidate gating, keep the final version gate."""
    import re
    for path in ("similarity/run_gui.command", "similarity/run_cli.command"):
        src = _read(path)
        assert re.search(VERSION_PROBE_RE, src), f"{path}: post-resolve version expression missing"
        # Tailored error messaging — distinguish override vs resolver bug.
        assert "SELFIEGEN_PYTHON" in src
        assert "supported range 3.9-3.12" in src or "requires 3.9-3.12" in src


def test_macos_launchers_guard_auto_create_venv():
    """Auto-create path must validate `pybin` BEFORE `python -m venv` to prevent
    creating a born-broken venv that re-trips the gate on the next launch."""
    for path in ("similarity/run_gui.command", "similarity/run_cli.command"):
        src = _read(path)
        # The error message that the auto-create guard prints when pybin is unsupported.
        assert "brew install python@3.11" in src, (
            f"{path}: missing auto-create guard — if `command -v` resolves to an unsupported "
            "python (e.g., python3.14), the resolver would silently create a broken venv."
        )


def test_windows_bat_launcher_uses_check_py_subroutine():
    """Windows .bat must route every candidate through :check_py to inherit
    the version probe — direct `if exist` blocks would re-introduce the bug."""
    src = _read("similarity/run_gui.bat")
    assert ":check_py" in src, "missing :check_py subroutine"
    # 4 candidate paths (SELFIEGEN_VENV_DIR, REPO_ROOT/venv, REPO_ROOT/.venv311, REPO_ROOT/.venv,
    # local .venv) each `call :check_py`, plus 2 post-create checks. >=5 is a safe lower bound.
    assert src.count("call :check_py") >= 5, (
        f"call :check_py count={src.count('call :check_py')} — expected at least 5 candidates"
    )


def test_windows_bat_launcher_includes_venv311_candidate():
    """Mirror the macOS .venv311 preference on Windows for cross-platform parity."""
    src = _read("similarity/run_gui.bat")
    assert ".venv311" in src, "Windows launcher missing .venv311 candidate"


def test_windows_bat_launcher_keeps_post_resolve_gate():
    import re
    src = _read("similarity/run_gui.bat")
    # Note: cmd uses comma-separated tuples without spaces inside `(3,9)`.
    assert re.search(r"\(3,9\) <= sys\.version_info\[:2\] < \(3,13\)", src), (
        "post-resolve version probe missing from .bat"
    )
    assert "SELFIEGEN_PYTHON" in src


def test_all_three_launchers_keep_supported_range_consistent():
    """All three launchers must share the same supported version range (3.9-3.12)."""
    import re
    paths = [
        "similarity/run_gui.command",
        "similarity/run_cli.command",
        "similarity/run_gui.bat",
    ]
    seen_ranges = set()
    for path in paths:
        src = _read(path)
        # Both forms: "(3, 9) <= ... < (3, 13)" (bash) and "(3,9) <= ... < (3,13)" (cmd).
        m = re.search(r"\(3, ?9\) <= sys\.version_info\[:2\] < \(3, ?13\)", src)
        assert m, f"{path}: no version-range expression found"
        # Normalize for cross-platform comparison.
        seen_ranges.add(re.sub(r"\s+", "", m.group(0)))
    assert len(seen_ranges) == 1, f"version range diverged across launchers: {seen_ranges}"
