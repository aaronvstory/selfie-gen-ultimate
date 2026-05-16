"""Static-text guards on the Oldcam V14 launchers' Python resolver.

Background: every oldcam algorithm launcher (v7-v13) shipped with a `resolve_py`
that short-circuited on `[ -x "$candidate/bin/python" ]` without validating the
interpreter version. On a machine with a stale `.venv/` pointing at python3.14,
the resolver returned that bad python and `pip install` failed because
`numpy<2` has no python3.13+ wheels — confusing failure mode because supported
pythons WERE installed, just never tried.

v14 is the first oldcam version to adopt the `_python_supported()` /
`:check_py` pattern that similarity got in afe0540b. These tests pin the
pattern in place so the next oldcam version (v15+) doesn't regress by
copy-pasting from v7-v13. See `docs/oldcam-wiring.md §1` for the canonical
template instruction and §9 for the v7-v13 known-defect carve-out.
"""
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]

VERSION_PROBE_RE_BASH = r"\(3, ?9\) <= sys\.version_info\[:2\] < \(3, ?13\)"
VERSION_PROBE_RE_CMD = r"\(3,9\) <= sys\.version_info\[:2\] < \(3,13\)"


def _read(path: str) -> str:
    return (REPO_ROOT / path).read_text(encoding="utf-8")


# --- macOS algorithm launcher ---


def test_macos_algorithm_launcher_defines_python_supported_helper():
    """Per CLAUDE.md macOS Rule 9 — every venv candidate must be version-gated."""
    src = _read("oldcam-v14/macOS/oldcam.command")
    assert "_python_supported()" in src, (
        "missing _python_supported() helper — without it, a stale .venv "
        "pointing at python3.14 is silently picked and numpy<2 install fails."
    )


def test_macos_algorithm_launcher_gates_every_candidate():
    """Every venv candidate path must invoke _python_supported."""
    src = _read("oldcam-v14/macOS/oldcam.command")
    # Helper definition + invocations on each candidate (root venv, .venv311,
    # root .venv, local .venv) + auto-create guard + post-resolve gate.
    # Lower bound 6 (4 candidates + auto-create + post-resolve).
    count = src.count("_python_supported")
    assert count >= 6, (
        f"_python_supported invocations dropped (count={count}); "
        "every candidate venv must be guarded."
    )


def test_macos_algorithm_launcher_uses_strict_set_flags():
    """CLAUDE.md macOS Rule 10 — `.command` and `.sh` siblings use set -euo pipefail."""
    src = _read("oldcam-v14/macOS/oldcam.command")
    assert "set -euo pipefail" in src, (
        "Rule 10 violation: oldcam-v14/macOS/oldcam.command does not use "
        "set -euo pipefail. Sibling launchers must share strict mode."
    )


def test_macos_algorithm_launcher_prefers_python311_first():
    """CLAUDE.md macOS Rule 6 — Homebrew python3.11 is the only build with bundled _tkinter."""
    src = _read("oldcam-v14/macOS/oldcam.command")
    idx_311 = src.find("command -v python3.11")
    idx_312 = src.find("command -v python3.12")
    assert idx_311 > 0 and idx_312 > 0, (
        "macOS Rule 6: expected fallback chain referencing python3.11 and python3.12"
    )
    assert idx_311 < idx_312, (
        "python3.12 appears before python3.11 in fallback — macOS Homebrew "
        "python3.12+ ships without _tkinter (and tests under Rule 6 also "
        "prefer python3.11 for the cleaner numpy<2 wheel coverage)."
    )


def test_macos_algorithm_launcher_keeps_post_resolve_gate():
    """Defense-in-depth: even with per-candidate gating, keep the final version probe."""
    import re

    src = _read("oldcam-v14/macOS/oldcam.command")
    assert re.search(VERSION_PROBE_RE_BASH, src), "post-resolve version probe missing"
    assert "SELFIEGEN_PYTHON" in src
    assert "requires 3.9-3.12" in src or "supported range 3.9-3.12" in src


# --- macOS delegate launchers (Rule 10 set-flag parity) ---


def test_macos_hub_and_platform_launchers_use_strict_set_flags():
    """CLAUDE.md macOS Rule 10 — the launcher chain's `.command` siblings
    must all use `set -euo pipefail`. Mismatches silently change error
    handling between launch paths."""
    for path in (
        "launchers/macos/run_oldcam_v14.command",
        "launchers/run_oldcam_v14.command",
    ):
        src = _read(path)
        assert "set -euo pipefail" in src, (
            f"{path}: Rule 10 violation — must use `set -euo pipefail` for "
            "parity with the algorithm-layer .command. Found different set flags."
        )


# --- Windows algorithm launcher ---


def test_windows_bat_launcher_uses_check_py_subroutine():
    """Windows .bat must route every candidate through :check_py to inherit
    the version probe — direct `if exist` blocks would re-introduce the bug."""
    src = _read("oldcam-v14/oldcam_launcher.bat")
    assert ":check_py" in src, "missing :check_py subroutine"
    # 4 candidate paths (SELFIEGEN_VENV_DIR, REPO_ROOT/venv, REPO_ROOT/.venv311,
    # REPO_ROOT/.venv, local .venv) each `call :check_py`, plus 1 post-create.
    count = src.count("call :check_py")
    assert count >= 5, (
        f"call :check_py count={count} — expected at least 5 candidates"
    )


def test_windows_bat_launcher_includes_venv311_candidate():
    """Mirror the macOS .venv311 preference on Windows for cross-platform parity."""
    src = _read("oldcam-v14/oldcam_launcher.bat")
    assert ".venv311" in src, "Windows launcher missing .venv311 candidate"


def test_windows_bat_launcher_keeps_post_resolve_gate():
    """Defense-in-depth version gate (catches SELFIEGEN_PYTHON pointing at unsupported python)."""
    import re

    src = _read("oldcam-v14/oldcam_launcher.bat")
    assert re.search(VERSION_PROBE_RE_CMD, src), (
        "post-resolve version probe missing from .bat"
    )
    assert "SELFIEGEN_PYTHON" in src


# --- Cross-launcher version-range consistency ---


def test_all_v14_launchers_share_supported_version_range():
    """All three v14 launchers (Mac algorithm, Mac wrappers, Windows bat) must
    agree on the supported version range so the user-facing error messages
    stay accurate across platforms."""
    import re

    bash_src = _read("oldcam-v14/macOS/oldcam.command")
    cmd_src = _read("oldcam-v14/oldcam_launcher.bat")

    bash_m = re.search(VERSION_PROBE_RE_BASH, bash_src)
    cmd_m = re.search(VERSION_PROBE_RE_CMD, cmd_src)
    assert bash_m and cmd_m, "version-range expression missing in one launcher"

    bash_normalized = re.sub(r"\s+", "", bash_m.group(0))
    cmd_normalized = re.sub(r"\s+", "", cmd_m.group(0))
    assert bash_normalized == cmd_normalized, (
        f"version range diverged across launchers — bash: {bash_normalized!r}, "
        f"cmd: {cmd_normalized!r}. They must match so the user-facing error "
        "message is the same on both platforms."
    )
