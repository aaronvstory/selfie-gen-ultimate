"""Tests for ``stream_subprocess_with_timeout`` heartbeat + env vars
added in v2.7.

The injector takes ~7-8 min on CPU between "Launching rppg_injector.py"
and its first stdout line (MediaPipe model load + baseline ROI
extraction happen silently). v2.7 fixed two things:

1. PYTHONUNBUFFERED=1 in the subprocess env so the child's ``print``
   calls flush immediately instead of buffering to a 4KB block.
2. An optional ``on_heartbeat`` callback that fires every N seconds
   while the child has produced ZERO stdout lines, and stops the
   instant the first line arrives.

These tests exercise the streamer with a controlled tiny script (an
inline ``python -c "...time.sleep(...); print(...)"``) so the timing
is bounded — no need to wait minutes for a real injector.
"""
from __future__ import annotations

import os
import subprocess
import sys
import time

import pytest

from automation import rppg as rppg_module


def _run_silent_then_emit(duration_silent: float, then_emit: str):
    """Return the python child invocation used in tests: sleep
    ``duration_silent`` seconds, then ``print(then_emit)``."""
    code = (
        "import sys, time; "
        f"time.sleep({duration_silent}); "
        f"sys.stdout.write({then_emit!r} + chr(10)); "
        "sys.stdout.flush()"
    )
    return [sys.executable, "-c", code]


def test_streamer_passes_pythonunbuffered_to_child(monkeypatch, tmp_path):
    """The subprocess env must include PYTHONUNBUFFERED=1 so the
    child's stdout buffers don't sit unflushed for minutes. Capture
    the env via a monkey-patched subprocess.Popen and assert."""
    captured = {}

    real_popen = subprocess.Popen

    def _spy_popen(cmd, **kwargs):
        captured["env"] = kwargs.get("env")
        return real_popen(cmd, **kwargs)

    monkeypatch.setattr(rppg_module.subprocess, "Popen", _spy_popen)

    # A trivial child that exits immediately.
    rc, _ = rppg_module.stream_subprocess_with_timeout(
        [sys.executable, "-c", "print('hi')"],
        cwd=str(tmp_path),
        timeout_seconds=30,
    )
    assert rc == 0
    assert captured["env"] is not None
    assert captured["env"].get("PYTHONUNBUFFERED") == "1", (
        "PYTHONUNBUFFERED=1 must ride in the subprocess env so the "
        "injector's prints flush immediately (v2.7 fix)."
    )
    # KLING_NO_PAUSE preserved (pre-existing contract).
    assert captured["env"].get("KLING_NO_PAUSE") == "1"


def test_heartbeat_fires_during_silent_window():
    """Run a child that's silent for 2s before its first line. With
    heartbeat_interval=0.5s we expect ~3-4 heartbeats during the
    silent window, then the heartbeat stops once the first line lands."""
    received = []
    cmd = _run_silent_then_emit(duration_silent=2.0, then_emit="first line")
    rc, lines = rppg_module.stream_subprocess_with_timeout(
        cmd,
        cwd=os.getcwd(),
        timeout_seconds=30,
        on_heartbeat=lambda elapsed: received.append(elapsed),
        heartbeat_interval_seconds=0.5,
    )
    assert rc == 0
    assert lines == ["first line"]
    # In 2s of silence with 0.5s interval we expect at least 2
    # heartbeats. We don't assert an exact count because timing jitter
    # on a loaded CI box can produce 2-4. The important property is
    # ``len > 0`` (heartbeat fired during the silent window).
    assert len(received) >= 2, (
        f"expected >=2 heartbeats during the 2s silent window, got {received}"
    )
    # And the LAST heartbeat must have fired BEFORE the first line
    # landed (i.e. all elapsed values < ~2.5s including the print
    # latency).
    assert all(t < 3.0 for t in received), (
        f"heartbeats should only fire during silence; got {received}"
    )


def test_heartbeat_silences_after_first_line():
    """Once the child has emitted ANY line, the heartbeat must stop —
    the per-iteration progress is now the user-visible signal and
    additional heartbeats would just be noise.

    Round-10 review fix (Gemini MEDIUM): the prior version used a 3.0s
    heartbeat interval against a child that ran only ~1.5s total, so the
    heartbeat could NEVER fire regardless of whether the silence logic
    worked — a vacuous pass. This version uses a SMALL interval (0.3s)
    and a child that stays silent for 0.5s BEFORE emitting, so heartbeats
    are guaranteed to fire during the silent startup window. We then
    assert that every heartbeat fired strictly BEFORE the first line was
    received — robust to interpreter-startup jitter (extra startup time
    only adds more pre-line heartbeats, never a post-line one).
    """
    received = []
    first_line_time = None

    def _on_line(_line):
        nonlocal first_line_time
        if first_line_time is None:
            first_line_time = time.monotonic()

    # Child sleeps 0.5s (silent window → heartbeats fire), emits a line,
    # then sleeps 1.5s so the streamer has time to observe any spurious
    # post-line heartbeat.
    code = (
        "import sys, time; "
        "time.sleep(0.5); "
        "sys.stdout.write('early\\n'); sys.stdout.flush(); "
        "time.sleep(1.5)"
    )
    cmd = [sys.executable, "-c", code]
    rppg_module.stream_subprocess_with_timeout(
        cmd,
        cwd=os.getcwd(),
        timeout_seconds=30,
        on_line=_on_line,
        on_heartbeat=lambda elapsed: received.append(time.monotonic()),
        heartbeat_interval_seconds=0.3,
    )
    assert first_line_time is not None, "first line was never received"
    for t in received:
        assert t < first_line_time, (
            f"heartbeat fired at {t} AFTER the first line arrived at "
            f"{first_line_time}; the silence-after-first-line logic is broken"
        )


def test_no_heartbeat_callback_means_no_silent_window_logging():
    """Passing on_heartbeat=None (the default) must NOT crash + must
    NOT log anything during silent windows. Existing automation paths
    don't always wire a heartbeat — back-compat must hold."""
    cmd = _run_silent_then_emit(duration_silent=0.5, then_emit="done")
    rc, lines = rppg_module.stream_subprocess_with_timeout(
        cmd, cwd=os.getcwd(), timeout_seconds=30,
    )
    assert rc == 0
    assert lines == ["done"]


def test_tracker_anchor_start_time_aligns_with_subprocess_launch():
    """CodeRabbit minor (PR #54 round 1): the heartbeat reports
    minutes since subprocess launch, but ``tracker.elapsed_str()``
    defaults to anchoring on first stdout line. The completion
    banner would then say "1m 20s elapsed" for a job whose heartbeat
    just logged "7 min elapsed" — visually contradictory.

    Fix: ``_RppgProgressTracker.anchor_start_time(t)`` accepts a
    caller-supplied launch timestamp. ``run_rppg`` anchors it to
    ``time.monotonic()`` BEFORE the streamer fires so both timers
    measure from the same origin.
    """
    tracker = rppg_module._RppgProgressTracker(report_cb=None)
    # Anchor 3.5 minutes ago.
    import time
    anchor = time.monotonic() - 210.0
    tracker.anchor_start_time(anchor)
    elapsed = tracker.elapsed_str()
    # Format is "Xm Ys"; we just verify it reports ~3 minutes, not "?"
    assert elapsed != "?", (
        "after anchor_start_time(), elapsed_str must report real "
        "elapsed time even before the first stdout line lands"
    )
    assert "m " in elapsed, (
        f"expected 'Xm Ys' format for an anchor 3.5 min ago, got {elapsed!r}"
    )
    # Heartbeat would log mins=int(210/60)=3; tracker should agree.
    assert elapsed.startswith("3m "), (
        f"tracker.elapsed_str() ({elapsed!r}) must report the same "
        "minutes the heartbeat would compute from the same anchor"
    )


def test_heartbeat_exception_does_not_kill_subprocess(monkeypatch):
    """A buggy heartbeat callback must not bring down the subprocess
    wait — same guarantee as the deadline_extender exception swallow."""
    cmd = _run_silent_then_emit(duration_silent=1.0, then_emit="ok")

    def _broken_heartbeat(elapsed):
        raise RuntimeError("intentional test failure")

    rc, lines = rppg_module.stream_subprocess_with_timeout(
        cmd,
        cwd=os.getcwd(),
        timeout_seconds=30,
        on_heartbeat=_broken_heartbeat,
        heartbeat_interval_seconds=0.3,
    )
    # The streamer must have completed normally even though every
    # heartbeat call raised.
    assert rc == 0
    assert lines == ["ok"]
