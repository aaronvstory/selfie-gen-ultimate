"""Regression tests for the v2.16 logging overhaul.

Covers the three layers that surface granular failure detail to the user:

1. ``log_utils.format_exception_detail`` / ``format_exception_traceback`` — the
   shared helper that replaces bare ``str(e)`` at the tab/generator/queue catch
   sites (so empty-message exceptions still name their type).
2. ``scripts/rppg_import_diag.py`` — the per-module rPPG import diagnostic that
   names WHICH dependency is MISSING vs BROKEN and flags numpy 2.x.
3. ``queue_manager._is_rppg_setup_diag`` — the classifier that promotes the
   launcher's self-heal diagnostics to the user-facing panel (warning/error)
   instead of letting them fall through to a hidden ``debug`` line.
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import pytest

_ROOT = Path(__file__).resolve().parent.parent


# --------------------------------------------------------------------------- #
# log_utils
# --------------------------------------------------------------------------- #
def test_format_exception_detail_includes_type_and_message():
    from log_utils import format_exception_detail

    try:
        raise ValueError("bad input")
    except ValueError as exc:
        assert format_exception_detail(exc) == "ValueError: bad input"


def test_format_exception_detail_never_empty_for_blank_message():
    """A bare str(exc) is '' for several exception types (TimeoutError,
    some OSError subclasses). The helper must still name the type so the
    user learns WHAT kind of failure occurred."""
    from log_utils import format_exception_detail

    try:
        raise TimeoutError()
    except TimeoutError as exc:
        assert str(exc) == ""  # the exact gap this helper closes
        assert format_exception_detail(exc) == "TimeoutError"


def test_format_exception_detail_collapses_multiline_message():
    """A multi-line exception message must collapse to a single line so a
    panel entry doesn't fragment across rows (code-review LOW)."""
    from log_utils import format_exception_detail

    try:
        raise ValueError("line one\nline two\n  indented three")
    except ValueError as exc:
        out = format_exception_detail(exc)
        assert "\n" not in out
        assert out == "ValueError: line one line two indented three"


def test_format_exception_traceback_contains_stack():
    from log_utils import format_exception_traceback

    try:
        raise RuntimeError("kaboom")
    except RuntimeError as exc:
        tb = format_exception_traceback(exc)
        assert "RuntimeError" in tb
        assert "kaboom" in tb
        assert "Traceback" in tb


# --------------------------------------------------------------------------- #
# scripts/rppg_import_diag.py
# --------------------------------------------------------------------------- #
def _run_diag(*modules: str) -> subprocess.CompletedProcess:
    return subprocess.run(
        [sys.executable, str(_ROOT / "scripts" / "rppg_import_diag.py"), *modules],
        capture_output=True,
        text=True,
    )


def test_rppg_diag_reports_ok_module():
    """A definitely-importable stdlib module reports OK with a version-ish
    tail and exits 0."""
    proc = _run_diag("json")
    assert proc.returncode == 0, proc.stdout + proc.stderr
    assert "[rppg-diag] OK" in proc.stdout
    assert "json" in proc.stdout


def test_rppg_diag_names_missing_module_and_exits_nonzero():
    """A module that does not exist must be reported MISSING by NAME and
    the process must exit non-zero so the launcher can branch on it. This
    is the core of the friend's complaint: name the failing import."""
    proc = _run_diag("definitely_not_a_real_module_xyz")
    assert proc.returncode == 1
    assert "MISSING" in proc.stdout
    assert "definitely_not_a_real_module_xyz" in proc.stdout
    # The verdict line must enumerate the failing module by name.
    assert "RESULT:" in proc.stdout
    assert "definitely_not_a_real_module_xyz" in proc.stdout.split("RESULT:")[-1]


def test_rppg_diag_emits_numpy_version_line():
    """The numpy version is logged unconditionally — it's the single most
    diagnostic fact for this app's recurring numpy-2.x fresh-install bug."""
    proc = _run_diag("json")
    assert "[rppg-diag] numpy-version:" in proc.stdout


def test_rppg_diag_default_module_set_is_the_rppg_core():
    """With no args the helper checks the rPPG core import set so the
    launcher can call it bare."""
    from scripts.rppg_import_diag import CORE_MODULES

    assert CORE_MODULES == ["cv2", "numpy", "mediapipe", "scipy", "absl"]


def test_rppg_diag_absl_is_optional_not_fatal():
    """absl is GUARDED in rppg_injector.py, so a missing absl must NOT fail the
    gate (exit code) — it's reported but rPPG still runs (Codex P2, PR #67).
    Essential deps stay fatal."""
    from scripts.rppg_import_diag import (
        ESSENTIAL_MODULES,
        OPTIONAL_MODULES,
        diagnose,
    )

    assert OPTIONAL_MODULES == ["absl"]
    assert ESSENTIAL_MODULES == ["cv2", "numpy", "mediapipe", "scipy"]
    # A present essential (json stands in) + a missing OPTIONAL must exit 0.
    import scripts.rppg_import_diag as diag_mod

    saved = diag_mod.OPTIONAL_MODULES
    try:
        diag_mod.OPTIONAL_MODULES = ["a_missing_optional_xyz"]
        assert diagnose(["json", "a_missing_optional_xyz"]) == 0
    finally:
        diag_mod.OPTIONAL_MODULES = saved
    # A missing ESSENTIAL must exit 1.
    assert diagnose(["an_essential_missing_xyz", "json"]) == 1


# --------------------------------------------------------------------------- #
# queue_manager._is_rppg_setup_diag
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize(
    "line",
    [
        "[rppg-diag] MISSING mediapipe    not installed",
        "[rppg-diag] numpy-version: 2.4.1  <-- WARNING",
        "[rppg-diag] OK      cv2          (4.11.0)",  # success lines surface too
        "  WARN: rPPG deps missing -- syncing repo requirements before retry...",
        "  ERROR: rPPG deps still missing after pip sync (see detail above).",
        "  Installing MediaPipe separately with --no-deps: mediapipe==0.10.35",
        "  OK: rPPG deps installed.",
    ],
)
def test_is_rppg_setup_diag_matches_launcher_diagnostics(line: str):
    from kling_gui.queue_manager import _is_rppg_setup_diag

    assert _is_rppg_setup_diag(line.strip())


@pytest.mark.parametrize(
    "line",
    [
        "Iteration 3/10 complete",
        "Test Result: FAIL",
        "Loading MediaPipe model...",
        "some unrelated injector progress line",
    ],
)
def test_is_rppg_setup_diag_ignores_normal_progress(line: str):
    from kling_gui.queue_manager import _is_rppg_setup_diag

    assert not _is_rppg_setup_diag(line.strip())
