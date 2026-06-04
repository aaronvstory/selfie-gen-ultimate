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


def test_kill_process_tree_kills_child_process():
    """Codex P1 (PR #73): the rPPG/Oldcam paths run through a .bat wrapper that
    spawns a python child. abort must kill the WHOLE tree, not just the wrapper,
    or the injector keeps burning GPU after Abort. Simulate it: a parent python
    that spawns a long-lived grandchild; tree-kill must take BOTH down."""
    qm, _ = _make_qm()
    # Parent spawns a child that sleeps 60s, then the parent itself sleeps 60s.
    # Killing only the parent handle would orphan the child on a naive kill().
    parent_src = (
        "import subprocess, sys, time; "
        "c = subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(60)']); "
        "print(c.pid, flush=True); "
        "time.sleep(60)"
    )
    proc = subprocess.Popen(
        [sys.executable, "-c", parent_src],
        stdout=subprocess.PIPE, text=True,
    )
    try:
        child_pid = int(proc.stdout.readline().strip())
        qm._kill_process_tree(proc)
        for _ in range(50):
            if proc.poll() is not None:
                break
            time.sleep(0.1)
        assert proc.poll() is not None, "parent must be killed"
        # The grandchild must also be gone (the whole point of the tree-kill).
        # os.kill(pid, 0) raises if the pid no longer exists.
        import os as _os
        gone = False
        for _ in range(50):
            try:
                _os.kill(child_pid, 0)
                time.sleep(0.1)
            except OSError:
                gone = True
                break
        assert gone, "child process must be killed by the tree-kill, not orphaned"
    finally:
        if proc.poll() is None:
            proc.kill()
            proc.wait(timeout=5)


def test_kill_process_tree_on_dead_handle_is_safe():
    """tree-kill on an already-exited process must not raise."""
    qm, _ = _make_qm()
    proc = subprocess.Popen([sys.executable, "-c", "pass"])
    proc.wait(timeout=10)
    qm._kill_process_tree(proc)  # must be a no-op, no raise


def test_on_active_subprocess_change_fires():
    """The Abort button is enabled only while a job runs (Codex P1/CodeRabbit
    Major, PR #73): publishing a proc fires the hook with True, clearing it
    fires False."""
    qm, _ = _make_qm()
    events = []
    qm.on_active_subprocess_change = lambda running: events.append(running)

    class _FakeProc:
        pid = 12345

        def poll(self):
            return None

    qm._publish_active_subprocess(_FakeProc())
    qm._publish_active_subprocess(None)
    assert events == [True, False]


def test_kill_process_tree_never_killpgs_own_group(monkeypatch):
    """CRITICAL (Gemini + Codex P1, PR #73): on POSIX, if a child shares the
    GUI's process group, os.killpg(getpgid(pid)) would SIGKILL the GUI + test
    runner themselves. _kill_process_tree MUST guard: only killpg a group that
    differs from our own. Simulate the dangerous case (child's pgid == our pgid)
    and assert killpg is NEVER called."""
    if sys.platform == "win32":
        import pytest as _pytest
        _pytest.skip("killpg guard is POSIX-only; Windows uses taskkill")
    import os as _os
    qm, _ = _make_qm()

    class _Proc:
        pid = 999999

        def poll(self):
            return None  # 'still running' so the kill path runs

        def kill(self):
            pass

    killpg_calls = []
    monkeypatch.setattr(_os, "getpgid", lambda pid: _os.getpgrp())  # SAME group
    monkeypatch.setattr(_os, "killpg", lambda pgid, sig: killpg_calls.append(pgid))
    qm._kill_process_tree(_Proc())
    assert killpg_calls == [], "must NOT killpg our own process group (suicide)"


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


def test_handle_item_abort_requeues_completed_item():
    """code-review Codex P2 (PR #73): _process_queue marks the item 'completed'
    right after Kling generation RETURNS, BEFORE the post-processing stages
    (rPPG/Loop/Oldcam). An Abort DURING those stages reaches _handle_item_abort
    with status=='completed'; the old processing-only guard silently no-op'd
    while the log still claimed 're-queued' — the row stayed done and Resume
    couldn't pick it up. A completed item must now also be re-queued."""
    qm, _logs = _make_qm()

    class _Item:
        filename = "clip.mp4"
        status = "completed"  # already flipped to completed before post-proc
        stage = "oldcam"
        stage_percent = 80
        output_path = None
        resume_kling_output = None
        resume_from_existing = False

    item = _Item()
    qm._handle_item_abort(item)
    assert item.status == "pending", "completed-then-aborted item must re-queue"
    assert item.stage == "queued"


def test_handle_item_abort_arms_resume_when_kling_output_exists(tmp_path):
    """When the Kling video already exists at abort time (abort during
    post-processing), the handler must arm resume_from_existing so Resume skips
    re-generating Kling and continues post-processing from the existing file
    (Codex P2, PR #73). When NO output exists yet (abort during Kling gen),
    resume must stay OFF so Resume does a clean full re-run."""
    qm, logs = _make_qm()
    kling = tmp_path / "clip_kling.mp4"
    kling.write_bytes(b"fake-mp4")

    class _Item:
        filename = "clip.mp4"
        status = "completed"
        stage = "rppg"
        stage_percent = 10
        output_path = None
        resume_kling_output = str(kling)
        resume_from_existing = False

    item = _Item()
    qm._handle_item_abort(item)
    assert item.resume_from_existing is True
    assert any("continue from the existing Kling" in m for _lvl, m in logs)

    # No Kling output on disk -> resume must NOT arm (clean full re-run on Resume)
    qm2, logs2 = _make_qm()

    class _Item2:
        filename = "clip2.mp4"
        status = "processing"
        stage = "kling"
        stage_percent = 5
        output_path = None
        resume_kling_output = None
        resume_from_existing = False

    item2 = _Item2()
    qm2._handle_item_abort(item2)
    assert item2.resume_from_existing is False
    assert any("re-run it" in m for _lvl, m in logs2)
