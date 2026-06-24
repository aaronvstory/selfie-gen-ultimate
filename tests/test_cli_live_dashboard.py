"""Live automation dashboard rendering guards.

Regression coverage for the "garbled CLI" bug: the pinned "Automation Live
Progress" panel rendered WIDER than the terminal, so the terminal soft-wrapped
each line while rich.Live counted only the logical (unwrapped) lines for its
cursor-up — it miscounted the panel height, failed to overwrite the prior
frame, and stacked overlapping copies at shrinking widths.

The fix pins the panel to a width clamped to the live terminal each render
tick. These tests lock that contract:
  * the width helper clamps the detected terminal size into a sane band,
  * the panel honors an explicit width with expand=False,
  * no rendered line ever exceeds the requested width (the wrap-overflow that
    broke Live's accounting).
"""
from __future__ import annotations

import re
import shutil

from rich.console import Console

_ANSI_RE = re.compile(r"\x1b\[[0-9;]*m")

import kling_automation_ui
from kling_automation_ui import (
    KlingAutomationUI,
    _DASHBOARD_WIDTH_MAX,
    _DASHBOARD_WIDTH_MIN,
    _dashboard_panel_width,
)


def _sample_panel(width):
    return KlingAutomationUI._build_dashboard_panel(
        total=3,
        counts={"completed": 1, "failed": 1, "manual_review": 0, "skipped": 0},
        current_case="HEATHER_A-BOWMAN-12051978",
        current_step="1 front expand",
        similarity="-",
        last_output="front_crop_2_nano-banana-2-edit_sim88_001-expanded_k25tStd_p5_1.mp4",
        error_reason="Outpaint failed or timed out (reason=fal_failed_or_timed_out)",
        events=[
            ("22:42:37", "info", "Queue watch: provider=fal endpoint=fal-ai/image-apps-v2/outpaint"),
            ("22:43:01", "error", "response_url failed: HTTP 422 — a very long detail " * 4),
        ],
        footer="[p] pause after current case · [a] abort after current step",
        queue_lines=["[ >> ] HEATHER_A-BOWMAN-12051978"],
        next_step="2 extract portrait",
        step_pos="step 1/10",
        step_progress="rPPG iter 5/10 99% (frame 240/241)",
        width=width,
    )


def test_dashboard_width_clamps_into_band(monkeypatch):
    def _fake_size(cols):
        return lambda fallback=(100, 24): shutil.os.terminal_size((cols, 24))

    # Wide terminal -> capped at MAX.
    monkeypatch.setattr(shutil, "get_terminal_size", _fake_size(400))
    assert _dashboard_panel_width() == _DASHBOARD_WIDTH_MAX

    # Tiny terminal -> floored at MIN.
    monkeypatch.setattr(shutil, "get_terminal_size", _fake_size(10))
    assert _dashboard_panel_width() == _DASHBOARD_WIDTH_MIN

    # Normal terminal -> detected width minus a small margin.
    monkeypatch.setattr(shutil, "get_terminal_size", _fake_size(120))
    assert _dashboard_panel_width() == 118


def test_dashboard_width_survives_detection_failure(monkeypatch):
    def _boom(fallback=(100, 24)):
        raise OSError("no tty")

    monkeypatch.setattr(shutil, "get_terminal_size", _boom)
    # Falls back to the internal default (100) minus margin, still in band.
    w = _dashboard_panel_width()
    assert _DASHBOARD_WIDTH_MIN <= w <= _DASHBOARD_WIDTH_MAX


def test_panel_honors_explicit_width():
    panel = _sample_panel(96)
    assert panel.width == 96
    assert panel.expand is False


def test_panel_default_width_is_none_for_legacy_callers():
    # Existing callers/tests that don't pass width keep the old content-sized
    # behavior (no fixed width).
    panel = _sample_panel(None)
    assert panel.width is None


def test_rendered_lines_never_exceed_requested_width():
    """The core invariant: even with very long event/output text, every
    rendered line fits within the requested width. A line wider than the
    terminal is exactly what desynced rich.Live and caused stacking."""
    target = 100
    panel = _sample_panel(target)
    # Render on a console MUCH wider than the panel so any overflow is the
    # panel's own doing, not console truncation.
    console = Console(width=240, force_terminal=True)
    with console.capture() as cap:
        console.print(panel)
    for line in cap.get().splitlines():
        visible = _ANSI_RE.sub("", line)
        assert len(visible) <= target, f"line exceeds width {target}: {len(visible)}"
