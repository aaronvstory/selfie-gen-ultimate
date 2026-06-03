"""Regression tests for the GUI Abort button (v2.21).

The user could only stop a long-running rPPG job (10 iterations / 20+ minutes on
CPU) by force-quitting the whole app. The Abort button signals the QueueManager
to (a) set an abort Event the rPPG/Oldcam stream loops poll every ≤1s and (b)
kill the active subprocess immediately so the user doesn't wait out the run.

These tests drive ``QueueManager`` directly with stub callbacks (no Tk window) so
the abort contract is locked without launching the GUI.
"""
from __future__ import annotations

import subprocess
import sys
import threading
import time
from pathlib import Path

import pytest

_REPO_ROOT = Path(__file__).resolve().parent.parent
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

from kling_gui.queue_manager import QueueManager  # noqa: E402


def _make_qm():
    """A QueueManager with no-op callbacks and no generator (key-less mode)."""
    logs = []
    qm = QueueManager(
        generator=None,
        config_getter=lambda: {},
        log_callback=lambda msg, level="info": logs.append((level, msg)),
        queue_update_callback=lambda: None,
        processing_complete_callback=lambda item: None,
    )
    return qm, logs


def test_abort_sets_event_and_pauses():
    """abort_current_job must set the abort Event and pause the queue."""
    qm, _ = _make_qm()
    assert not qm._abort_requested()
    qm.abort_current_job()
    assert qm._abort_requested(), "abort_current_job must set the abort Event"
    assert qm.is_paused, "abort must pause the queue so it doesn't roll on"


def test_abort_kills_live_published_subprocess():
    """When a subprocess handle is published, abort_current_job must kill it."""
    qm, _ = _make_qm()
    # A real child that would otherwise run 30s.
    proc = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(30)"])
    try:
        qm._publish_active_subprocess(proc)
        assert proc.poll() is None, "child should be running before abort"
        qm.abort_current_job()
        # kill() is async on Windows; give it a moment to reap.
        for _ in range(50):
            if proc.poll() is not None:
                break
            time.sleep(0.1)
        assert proc.poll() is not None, "abort_current_job must kill the child"
    finally:
        if proc.poll() is None:
            proc.kill()
            proc.wait(timeout=5)


def test_abort_no_active_subprocess_is_noop_safe():
    """abort with nothing running must not raise (button is harmless when idle)."""
    qm, _ = _make_qm()
    qm._publish_active_subprocess(None)
    qm.abort_current_job()  # must not raise
    assert qm._abort_requested()


def test_abort_does_not_kill_already_finished_handle():
    """A stale (finished) handle must NOT be re-killed — abort only acts on a
    live process (poll() is None). Guards against killing an unrelated PID that
    the OS may have recycled."""
    qm, _ = _make_qm()
    proc = subprocess.Popen([sys.executable, "-c", "pass"])
    proc.wait(timeout=10)
    assert proc.poll() is not None
    qm._publish_active_subprocess(proc)
    # Should simply set the event without attempting a kill on the dead handle.
    qm.abort_current_job()
    assert qm._abort_requested()


def test_abort_event_cleared_per_item_semantics():
    """A new item must start with a clear abort slate (the worker clears the
    Event at item start). We can't run the full worker here, but we assert the
    Event is a real threading.Event that .clear() resets."""
    qm, _ = _make_qm()
    qm.abort_current_job()
    assert qm._abort_requested()
    qm._abort_event.clear()
    assert not qm._abort_requested(), "clearing the Event must reset abort state"


def test_handle_item_abort_requeues_not_fails():
    """code-review Codex P2: aborting mid-item must re-queue the item (pending),
    NOT mark it failed and NOT mark it done. The user can then Resume to re-run
    it cleanly instead of getting a half-finished output marked complete."""
    qm, logs = _make_qm()

    class _Item:
        filename = "clip.mp4"
        status = "processing"
        stage = "rppg"
        stage_percent = 42
        output_path = None

    item = _Item()
    qm._handle_item_abort(item)
    assert item.status == "pending", "aborted item must be re-queued, not failed/done"
    assert item.stage == "queued"
    assert item.stage_percent == 0
    assert qm.is_running is False
    # An abort is a user choice — it must NOT be logged as an error.
    assert any("Aborted" in m for _lvl, m in logs)
    assert not any(lvl in ("error", "error_bold") for lvl, _m in logs)
