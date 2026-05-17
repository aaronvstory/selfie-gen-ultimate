"""Static-text guards on oldcam-testing/run_ab_test.command.

Mirrors tests/test_similarity_launcher_resolver.py for the A/B harness
launcher. The launcher claims a 3.9 <= ver < 3.13 support range via its
_python_supported probe; the PATH fallback must actually cover that
range so macOS setups with only python3.9 or python3.10 don't crash with
a false "No supported Python found" (the bug Codex caught on PR #33).
"""
from __future__ import annotations

import re
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
LAUNCHER = "oldcam-testing/run_ab_test.command"

VERSION_PROBE_RE = r"\(3, ?9\) <= sys\.version_info\[:2\] < \(3, ?13\)"


def _read(path: str) -> str:
    return (REPO_ROOT / path).read_text(encoding="utf-8")


def test_launcher_exists():
    assert (REPO_ROOT / LAUNCHER).is_file(), f"missing launcher: {LAUNCHER}"


def test_python_supported_helper_present():
    """The version-range gate must exist and match the documented range."""
    src = _read(LAUNCHER)
    assert "_python_supported()" in src, "missing _python_supported() helper"
    assert re.search(VERSION_PROBE_RE, src), (
        "version probe drifted from the documented '3.9 <= ver < 3.13' range; "
        "if you intentionally widened/narrowed it, update this test."
    )


def test_every_venv_candidate_is_version_gated():
    """Each venv-bin probe must call _python_supported (CLAUDE.md Rule 9).

    The launcher uses a `for candidate in ... ; do` loop pattern, so a
    single _python_supported textual invocation covers all four venvs.
    We assert the loop body contains the gate AND lists all four venvs,
    rather than counting textual occurrences (which would force a
    non-loop refactor to pass).
    """
    src = _read(LAUNCHER)
    # The override path before the loop:
    assert re.search(
        r'SELFIEGEN_PYTHON.*?_python_supported "\$\{SELFIEGEN_PYTHON\}"',
        src,
        re.DOTALL,
    ), "SELFIEGEN_PYTHON override path missing _python_supported gate"
    # The loop body must invoke _python_supported on the candidate:
    loop_match = re.search(
        r"for candidate in(.+?)done", src, re.DOTALL
    )
    assert loop_match, "missing `for candidate in ...; do ... done` venv loop"
    loop_body = loop_match.group(0)
    assert '_python_supported "$candidate"' in loop_body, (
        "venv candidate loop must call _python_supported on $candidate; "
        "stale wrong-version venvs (e.g. python3.14 .venv) would slip through."
    )
    # And the loop must enumerate all four prescribed venv locations:
    for venv in (".venv-macos", ".venv311", "venv/bin/python", ".venv/bin/python"):
        assert venv in loop_body, (
            f"venv candidate `{venv}` missing from the resolver loop body"
        )


def test_venv_candidate_order_macos_first():
    """.venv-macos is the macOS-prescribed venv name; .venv311 second per Rule 6."""
    src = _read(LAUNCHER)
    idx_macos = src.find(".venv-macos/bin/python")
    idx_311 = src.find(".venv311/bin/python")
    idx_venv = src.find("venv/bin/python\"")  # the bare "venv" probe
    idx_dot = src.find(".venv/bin/python")
    assert idx_macos > 0, "missing .venv-macos candidate"
    assert idx_311 > 0, "missing .venv311 candidate (CLAUDE.md Rule 6)"
    assert idx_venv > 0, "missing shared venv candidate"
    assert idx_dot > 0, "missing local .venv candidate"
    assert idx_macos < idx_311 < idx_venv < idx_dot, (
        ".venv-macos must lead, then .venv311, then venv, then .venv — "
        "out-of-order chain regressed."
    )


def test_path_fallback_covers_full_supported_range():
    """PATH probe must include python3.9..python3.12 per Codex P2 review on PR #33.

    The launcher's _python_supported gate accepts 3.9 <= ver < 3.13, but
    the PATH fallback originally only probed python3.11/3.12/python3/python.
    On macOS setups exposing only python3.10 (no python3 shim) this
    misreports "No supported Python found". Lock all four versioned probes.
    """
    src = _read(LAUNCHER)
    for ver in ("python3.11", "python3.12", "python3.10", "python3.9"):
        assert f" {ver} " in src or f" {ver}\n" in src or f"{ver} " in src, (
            f"PATH fallback missing {ver} — would silently fail on a macOS "
            "setup that only ships this versioned binary. See Codex review "
            "on PR #33 commit 4ec2129b."
        )


def test_path_fallback_prefers_python311():
    """python3.11 must be the first versioned probe (CLAUDE.md Rule 6 — Tk on Homebrew)."""
    src = _read(LAUNCHER)
    idx_311 = src.find("python3.11")
    idx_312 = src.find("python3.12")
    idx_310 = src.find("python3.10")
    idx_39 = src.find("python3.9")
    # Find occurrences inside the PATH probe loop specifically (not in comments).
    # The "for bin in" line establishes the chain.
    m = re.search(r"for bin in (.+?);\s*do", src)
    assert m, "PATH probe loop not found"
    chain = m.group(1)
    chain_list = chain.split()
    assert chain_list[0] == "python3.11", (
        f"python3.11 must be the first PATH probe (got {chain_list[0]}); "
        "CLAUDE.md Rule 6 — only python3.11 ships with bundled _tkinter on Homebrew."
    )
    # 3.11 first, then 3.12 (CLAUDE.md preferred order), then descending.
    assert chain_list[:4] == ["python3.11", "python3.12", "python3.10", "python3.9"], (
        f"PATH probe chain head changed: {chain_list[:4]}; "
        "expected ['python3.11','python3.12','python3.10','python3.9']."
    )


def test_launcher_uses_strict_bash_flags():
    """`set -euo pipefail` per CLAUDE.md Rule 10."""
    src = _read(LAUNCHER)
    assert "set -euo pipefail" in src, (
        "launcher must use `set -euo pipefail`; CLAUDE.md Rule 10 requires "
        "matching strict flags across launcher siblings."
    )


def test_no_arg_invocation_exits_with_usage():
    """No-arg path prints usage and exits 2 (matches run_ab_test.bat semantics)."""
    src = _read(LAUNCHER)
    # Look for the explicit `exit 2` after the usage block.
    assert re.search(r'Usage:.*?exit 2', src, re.DOTALL), (
        "no-arg usage block must end with `exit 2` to match the Windows "
        "run_ab_test.bat behavior (which uses `exit /b 2`)."
    )
