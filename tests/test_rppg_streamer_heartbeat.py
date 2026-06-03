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

    Round-13 review fix (Gemini MEDIUM 3327562370): the prior version was
    STILL vacuous. The streamer blocks on ``line_q.get(timeout=min(remaining,
    1.0))`` = 1.0s; the child slept only 0.5s before emitting, so the line
    landed at 0.5s and the queue ``get`` returned it WITHOUT ever raising
    ``_queue.Empty`` — the heartbeat branch was never reached, ``received``
    stayed empty, and the ``for t in received`` loop never executed (a
    no-op pass). Fix: the child now stays silent for 1.5s (> the 1.0s queue
    timeout) BEFORE its first line, so the queue ``get`` times out at least
    once and at least one heartbeat fires during the silent window. We then
    assert ``received`` is non-empty (the heartbeat actually ran) AND that
    every heartbeat fired strictly BEFORE the first line — robust to
    interpreter-startup jitter (extra startup time only adds more pre-line
    heartbeats, never a post-line one).
    """
    received = []
    first_line_time = None

    def _on_line(_line):
        nonlocal first_line_time
        if first_line_time is None:
            first_line_time = time.monotonic()

    # Child sleeps 1.5s (> the streamer's 1.0s queue-get timeout, so the
    # heartbeat branch is guaranteed to run during the silent window),
    # emits a line, then sleeps 1.5s so the streamer has time to observe
    # any spurious post-line heartbeat.
    code = (
        "import sys, time; "
        "time.sleep(1.5); "
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
    assert len(received) > 0, (
        "heartbeat never fired during the 1.5s silent window — the test "
        "would vacuously pass without this guard (gemini MEDIUM PR #54)"
    )
    for t in received:
        assert t < first_line_time, (
            f"heartbeat fired at {t} AFTER the first line arrived at "
            f"{first_line_time}; the silence-after-first-line logic is broken"
        )


def test_progress_predicate_classifies_banner_vs_injector_lines():
    """Unit test for ``is_rppg_progress_line`` (codex P2 PR #54): launcher
    banner lines must NOT be treated as progress; real injector lines must
    be — even with leading indent or ANSI colour codes."""
    pred = rppg_module.is_rppg_progress_line
    # Launcher banners (2-space indent) — NOT progress.
    assert not pred("  Launching rppg_injector.py")
    assert not pred("  Python: C:\\repo\\venv\\Scripts\\python.exe")
    assert not pred("  OK: rPPG deps installed.")
    assert not pred("  Installing MediaPipe separately...")
    # Warm-up noise the injector itself prints — also NOT progress (the
    # heartbeat should keep firing through it).
    assert not pred("Extracting facial ROIs")
    assert not pred("I0000 12.34 5678 gl_context.cc:357] init")
    # GPU backend line is printed at IMPORT time, before the silent
    # MediaPipe/ROI warm-up — so it must NOT silence the heartbeat
    # (codex P2 PR #54 round 14). It's still surfaced via on_line; it
    # just doesn't count as "progress has started".
    assert not pred("GPU backend: CuPy 12.3 on 1 device(s)")
    assert not pred("GPU backend: CuPy unavailable")
    # Genuine progress lines — ARE progress (heartbeat silences here).
    assert pred("Iteration 1/10")
    assert pred("Processing frame 30/120")
    assert pred("\x1b[1;97m  Iteration 2/10 \x1b[0m"), (
        "ANSI-wrapped, indented Iteration line must still count as progress"
    )


def test_heartbeat_survives_launcher_banner_lines():
    """Codex P2 (PR #54): when rPPG launches via run_rppg.bat/.sh the
    wrapper emits banner lines ("  Launching rppg_injector.py") BEFORE the
    injector's multi-minute silent warm-up. With the default silence-on-
    any-line behaviour those banners killed the heartbeat before the gap
    it exists to cover even began. Passing ``is_rppg_progress_line`` as the
    silence predicate must keep the heartbeat alive across banner lines and
    fire it during the silent gap, only silencing once a real "Iteration
    N/M" line lands."""
    received = []
    iter_line_time = None

    def _on_line(line):
        nonlocal iter_line_time
        if iter_line_time is None and line.strip().startswith("Iteration"):
            iter_line_time = time.monotonic()

    # Banner line first (must NOT silence the heartbeat), then a 1.5s silent
    # gap (> the 1.0s queue-get timeout, so a heartbeat is guaranteed to
    # fire), then a real Iteration line (which DOES silence it).
    code = (
        "import sys, time; "
        "sys.stdout.write('  Launching rppg_injector.py\\n'); sys.stdout.flush(); "
        "time.sleep(1.5); "
        "sys.stdout.write('Iteration 1/10\\n'); sys.stdout.flush(); "
        "time.sleep(0.3)"
    )
    cmd = [sys.executable, "-c", code]
    rc, _lines = rppg_module.stream_subprocess_with_timeout(
        cmd,
        cwd=os.getcwd(),
        timeout_seconds=30,
        on_line=_on_line,
        on_heartbeat=lambda elapsed: received.append(time.monotonic()),
        heartbeat_interval_seconds=0.3,
        heartbeat_silence_predicate=rppg_module.is_rppg_progress_line,
    )
    assert rc == 0
    assert iter_line_time is not None, "the Iteration line was never received"
    assert len(received) > 0, (
        "heartbeat never fired during the silent gap AFTER the launcher "
        "banner — the banner silenced it prematurely (codex P2 regression)"
    )
    for t in received:
        assert t < iter_line_time, (
            f"heartbeat at {t} fired AFTER the Iteration line at "
            f"{iter_line_time}; predicate-based silence is broken"
        )


def test_heartbeat_survives_gpu_backend_line():
    """Codex P2 (PR #54 round 14): the injector prints ``GPU backend: ...``
    at IMPORT time — before the multi-minute MediaPipe/baseline-ROI warm-up.
    Round-14 round-1 keyed heartbeat-silence off a regex set that INCLUDED
    the GPU line, so it silenced the heartbeat immediately and reopened the
    exact silent gap the heartbeat covers. The GPU line must be surfaced but
    must NOT silence the heartbeat — only a real Iteration/frame/score line
    should."""
    received = []
    iter_line_time = None

    def _on_line(line):
        nonlocal iter_line_time
        if iter_line_time is None and line.strip().startswith("Iteration"):
            iter_line_time = time.monotonic()

    code = (
        "import sys, time; "
        "sys.stdout.write('GPU backend: CuPy unavailable\\n'); sys.stdout.flush(); "
        "time.sleep(1.5); "
        "sys.stdout.write('Iteration 1/10\\n'); sys.stdout.flush(); "
        "time.sleep(0.3)"
    )
    cmd = [sys.executable, "-c", code]
    rc, _lines = rppg_module.stream_subprocess_with_timeout(
        cmd,
        cwd=os.getcwd(),
        timeout_seconds=30,
        on_line=_on_line,
        on_heartbeat=lambda elapsed: received.append(time.monotonic()),
        heartbeat_interval_seconds=0.3,
        heartbeat_silence_predicate=rppg_module.is_rppg_progress_line,
    )
    assert rc == 0
    assert iter_line_time is not None, "the Iteration line was never received"
    assert len(received) > 0, (
        "heartbeat never fired during the silent gap AFTER the GPU backend "
        "line — the GPU banner silenced it prematurely (codex P2 round 14)"
    )
    for t in received:
        assert t < iter_line_time, (
            f"heartbeat at {t} fired AFTER the Iteration line at {iter_line_time}"
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


def test_abort_event_kills_child_and_raises_timeout():
    """v2.21 (GUI Abort button): setting ``abort_event`` mid-run must kill the
    child within ≤1s and raise ``subprocess.TimeoutExpired`` so the caller takes
    the graceful-skip (-NORPPG) path. Without abort the child would run ~10s."""
    import threading

    abort = threading.Event()
    # Child runs for 10s with no output, so the ONLY way the streamer returns
    # quickly is the abort poll (not EOF, not the 30s timeout).
    code = "import time; time.sleep(10)"
    cmd = [sys.executable, "-c", code]

    # Fire the abort ~0.5s after launch from a side thread.
    threading.Timer(0.5, abort.set).start()

    start = time.monotonic()
    with pytest.raises(subprocess.TimeoutExpired):
        rppg_module.stream_subprocess_with_timeout(
            cmd,
            cwd=os.getcwd(),
            timeout_seconds=30,
            abort_event=abort,
        )
    elapsed = time.monotonic() - start
    # Must return promptly after the abort fires (~0.5s + ≤1s poll + reap),
    # NOT wait out the child's 10s sleep or the 30s timeout.
    assert elapsed < 5.0, (
        f"abort took {elapsed:.1f}s; expected <5s (poll interval is ≤1s)"
    )


def test_on_process_start_receives_live_popen():
    """v2.21: ``on_process_start`` must be called once with the live Popen so
    the GUI queue can publish it for the Abort button. The handle must be a real
    process (has a pid) at the moment of the callback."""
    seen = []

    def _capture(proc):
        seen.append(proc.pid)

    rc, lines = rppg_module.stream_subprocess_with_timeout(
        [sys.executable, "-c", "print('done')"],
        cwd=os.getcwd(),
        timeout_seconds=30,
        on_process_start=_capture,
    )
    assert rc == 0
    assert lines == ["done"]
    assert len(seen) == 1 and isinstance(seen[0], int), (
        "on_process_start must fire exactly once with a live Popen (pid)"
    )


def test_on_process_start_exception_does_not_break_stream():
    """A buggy on_process_start callback must NOT crash the stream (same
    guarantee as the heartbeat/deadline_extender swallows)."""
    def _broken(_proc):
        raise RuntimeError("intentional")

    rc, lines = rppg_module.stream_subprocess_with_timeout(
        [sys.executable, "-c", "print('ok')"],
        cwd=os.getcwd(),
        timeout_seconds=30,
        on_process_start=_broken,
    )
    assert rc == 0
    assert lines == ["ok"]


def test_no_abort_event_is_back_compat():
    """Omitting abort_event (the default None) must not change behaviour —
    a normal short run still returns (0, [...])."""
    rc, lines = rppg_module.stream_subprocess_with_timeout(
        [sys.executable, "-c", "print('hi')"],
        cwd=os.getcwd(),
        timeout_seconds=30,
    )
    assert rc == 0
    assert lines == ["hi"]


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
