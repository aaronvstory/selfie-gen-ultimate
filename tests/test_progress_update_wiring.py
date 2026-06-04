"""v2.23: unified growing progress line wired into video / loop / selfie /
outpaint (same in-place "progress_update" mechanism rPPG + Oldcam already use).

These tests assert the SOURCE lines emit at the progress_update level (the GUI's
LogDisplay.update_line overwrites a single in-place row for that level). They do
NOT exercise real network calls — fal_queue_poll's poll loop is driven with a
mocked HTTP layer so the heartbeat branch fires deterministically.
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

_ROOT = Path(__file__).resolve().parent.parent
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))


def test_fal_queue_poll_emits_progress_update_heartbeat(monkeypatch):
    """The shared fal.ai poll loop (selfie + outpaint) emits a short
    '<operation> — <status> (Ns)' line at progress_update on each ~60s heartbeat,
    plus the full diagnostic blob at debug (terminal/file only)."""
    import fal_utils

    calls = []

    def _cb(msg, level):
        calls.append((level, msg))

    # Make every poll return IN_PROGRESS so the loop keeps spinning, and make
    # sleep a no-op + advance a fake clock so the heartbeat (attempt % 12 == 0)
    # fires quickly, then bail out after a couple heartbeats via cancel.
    import threading

    cancel = threading.Event()

    class _Resp:
        status_code = 200

        @staticmethod
        def json():
            return {"status": "IN_PROGRESS"}

    monkeypatch.setattr(fal_utils, "_get_with_auth_fallback", lambda *a, **k: _Resp())
    monkeypatch.setattr(fal_utils.time, "sleep", lambda *a, **k: None)

    # Stop after the first heartbeat by setting cancel inside the cb.
    def _cb_then_cancel(msg, level):
        calls.append((level, msg))
        if level == "progress_update":
            cancel.set()

    fal_utils.fal_queue_poll(
        api_key="k",
        status_url="http://example/status",
        progress_cb=_cb_then_cancel,
        max_wait_seconds=600,
        cancel_event=cancel,
        operation_name="Selfie",
    )

    pu = [m for lvl, m in calls if lvl == "progress_update"]
    assert pu, "fal_queue_poll must emit at least one progress_update heartbeat"
    assert pu[0].startswith("Selfie — "), f"unexpected progress line: {pu[0]!r}"
    assert "s)" in pu[0], "heartbeat should show elapsed seconds"
    # The full diagnostic blob is debug (terminal/file only, not the panel).
    assert any(lvl == "debug" and "Still waiting" in m for lvl, m in calls)


def test_selfie_submit_line_is_progress_update():
    """selfie_generator starts the in-place row at submit (progress_update)."""
    import inspect
    import selfie_generator

    src = inspect.getsource(selfie_generator.SelfieGenerator._generate_fal_raw)
    assert '"progress_update"' in src
    assert 'operation_name="Selfie"' in src


def test_video_heartbeat_is_progress_update_and_ungated():
    """Video gen heartbeat emits 'Video gen — …' at progress_update, NOT behind
    the verbose-only log_verbose path (so the user sees the gen is alive)."""
    import inspect
    import kling_generator_falai

    src = inspect.getsource(
        kling_generator_falai.FalAIKlingGenerator.create_kling_generation
    )
    assert "Video gen —" in src
    assert '"progress_update"' in src


def test_loop_encoding_line_is_progress_update():
    """The loop-video status line is a progress_update in-place row."""
    import inspect
    from kling_gui import queue_manager

    src = inspect.getsource(queue_manager.QueueManager._loop_video)
    assert '"progress_update"' in src
    assert "Loop — encoding" in src


def test_queue_progress_callback_routes_progress_update_unconditionally():
    """The generator progress_callback must route progress_update straight to the
    panel (self.log) and gate only OTHER levels on verbose — so the video
    growing row shows even with Verbose Mode off."""
    import inspect
    from kling_gui import queue_manager

    src = inspect.getsource(queue_manager.QueueManager._generate_video)
    assert 'if level == "progress_update":' in src
    assert "self.log(message, \"progress_update\")" in src


def test_rppg_pct_pattern_matches_iterative_and_single_pass():
    """_RPPG_PCT_PAT must drive the queue progress bar for BOTH the iterative
    line 'rPPG iter 3/10 50% (frame 144/242)' AND the single-pass line
    'rPPG 47% (frame 114/242)'. The single-pass form (empty iter_label in
    automation/rppg) previously didn't match, leaving the bar stuck at 0
    (code-review MEDIUM, PR #73)."""
    from kling_gui.queue_manager import QueueManager

    pat = QueueManager._RPPG_PCT_PAT
    m_iter = pat.search("rPPG iter 3/10 50% (frame 144/242)")
    assert m_iter is not None
    assert (m_iter.group(1), m_iter.group(2), m_iter.group(3)) == ("3", "10", "50")

    m_single = pat.search("rPPG 47% (frame 114/242)")
    assert m_single is not None, "single-pass progress line must match"
    assert m_single.group(1) is None and m_single.group(2) is None
    assert m_single.group(3) == "47"
