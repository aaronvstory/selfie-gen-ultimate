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


@pytest.mark.parametrize(
    "line",
    [
        "[rppg-diag] BROKEN  mediapipe    (required) ModuleNotFoundError: No module named 'x'",
        "[rppg-diag] MISSING scipy        (required) not installed",
        "[rppg-diag] RESULT: 1 required module(s) not importable: mediapipe",
        "[rppg-diag] numpy-version: 2.4.1  <-- WARNING: numpy>=2 breaks ...",
        "     Still missing: scipy",
    ],
)
def test_failure_detail_line_surfaces_failures(line: str):
    """Only FAILING diagnostic markers are surfaced on a failure (CodeRabbit
    Major, PR #67)."""
    from kling_gui.queue_manager import _is_rppg_failure_detail_line

    assert _is_rppg_failure_detail_line(line.strip())


@pytest.mark.parametrize(
    "line",
    [
        "[rppg-diag] OK      cv2          (4.11.0)",
        "[rppg-diag] RESULT: all required modules import OK",
        "[rppg-diag] numpy-version: 1.26.4",  # no WARNING -> not a failure
        "[rppg-diag] NOTE: optional module(s) unavailable ...",
    ],
)
def test_failure_detail_line_ignores_success_and_ok(line: str):
    """A failure that happens AFTER imports succeed must NOT echo OK/all-clear
    lines as if they explained it."""
    from kling_gui.queue_manager import _is_rppg_failure_detail_line

    assert not _is_rppg_failure_detail_line(line.strip())


# --------------------------------------------------------------------------- #
# queue_manager._extract_rppg_failed_modules — friendly 1-line summary
# --------------------------------------------------------------------------- #
def test_extract_failed_modules_from_per_module_lines():
    """The user-facing summary names the failing module(s) concisely — not a
    raw dump (user feedback, PR #67)."""
    from kling_gui.queue_manager import _extract_rppg_failed_modules

    lines = [
        "[rppg-diag] OK      cv2          (4.11.0)",
        "[rppg-diag] BROKEN  mediapipe    (required) ModuleNotFoundError: No module named matplotlib",
        "[rppg-diag] RESULT: 1 required module(s) not importable: mediapipe",
    ]
    assert _extract_rppg_failed_modules(lines) == "mediapipe (broken)"


def test_extract_failed_modules_multiple_missing():
    from kling_gui.queue_manager import _extract_rppg_failed_modules

    lines = [
        "[rppg-diag] MISSING numpy (required) not installed",
        "[rppg-diag] MISSING scipy (required) not installed",
    ]
    assert _extract_rppg_failed_modules(lines) == "numpy (missing), scipy (missing)"


def test_extract_failed_modules_result_only_fallback():
    """When only the RESULT verdict is present, fall back to its module list."""
    from kling_gui.queue_manager import _extract_rppg_failed_modules

    lines = ["[rppg-diag] RESULT: 2 required module(s) not importable: numpy, scipy"]
    assert _extract_rppg_failed_modules(lines) == "numpy, scipy"


def test_extract_failed_modules_returns_empty_when_unnameable():
    """A post-import crash with no nameable module returns '' so the caller
    shows only the log-file pointer (no misleading guess)."""
    from kling_gui.queue_manager import _extract_rppg_failed_modules

    assert _extract_rppg_failed_modules(["some traceback", "another line"]) == ""
    assert _extract_rppg_failed_modules([]) == ""


# --------------------------------------------------------------------------- #
# v2.22 panel-friendliness: terminal stays data-rich, GUI panel stays friendly
# --------------------------------------------------------------------------- #
def test_verbose_rppg_banner_lines_are_panel_noise():
    """User direction 2026-06-04: the GUI 'processing log' panel should be
    friendly while the TERMINAL stays data-rich. The rPPG LAUNCHER banner lines
    (from run_rppg.bat — these never reach the rPPG progress tracker) are
    classified as panel-noise by queue_manager._is_panel_noise, so _on_rppg_line
    demotes them to 'debug' — hidden from the PANEL but (with the stdout handler
    at DEBUG) still flowing to the terminal + file.

    The INJECTOR-emitted noise (MediaPipe-CPU trio, Pipeline:, corrector
    version, "Using MediaPipe model") is NOT in _is_panel_noise — it's handled
    by the tracker's _RPPG_SUPPRESS/_RPPG_KEEP patterns (single source of truth;
    see test_rppg_progress_tracker). Double-filtering here previously let the
    noise filter override the tracker's KEEP for the corrector line (HIGH PR #73).
    """
    from kling_gui.queue_manager import _is_panel_noise

    launcher_noise = [
        r"Python: shared root venv -- C:\foo\venv\Scripts\python.exe",
        "Checking GPU acceleration for rPPG (CuPy)...",
    ]
    for line in launcher_noise:
        assert _is_panel_noise(line), f"launcher banner should be panel-noise: {line!r}"

    # Injector lines must NOT be _is_panel_noise (the tracker owns their fate).
    injector_lines = [
        "MediaPipe GPU delegate unavailable (NotImplementedError: ...)",
        "ImageCloneCalculator: GPU processing is disabled in build flags",
        r"Using MediaPipe Tasks FaceLandmarker model: C:\foo\face_landmarker.task [CPU]",
        "Pipeline: v5 (deband-first)",
        "rPPG Corrector v5.10+spectrum (v6 modules active)",
    ]
    for line in injector_lines:
        assert not _is_panel_noise(line), (
            f"injector line must NOT be _is_panel_noise (tracker handles it): {line!r}"
        )


def test_real_rppg_progress_lines_stay_in_panel():
    """The user-facing progress + GPU banner + injector device line must NOT be
    classified as panel-noise — they stay visible in the GUI panel."""
    from kling_gui.queue_manager import _is_panel_noise

    keep = [
        "rPPG iter 1/10 frame 72/241 (~25%)",
        "rPPG backend — CuPy 13.6.0 on 1 device(s)",
        "rPPG iteration 2/10 starting",
        "Target heart rate: 81.2 bpm",
    ]
    for line in keep:
        assert not _is_panel_noise(line), f"should stay in panel: {line!r}"
