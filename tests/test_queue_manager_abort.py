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
        # Own session/group on POSIX (matching the real rPPG/Oldcam launches) so
        # _kill_process_tree's self-group guard doesn't (correctly) refuse to
        # killpg pytest's own group, which would leave the grandchild alive and
        # fail this test on macOS/Linux (Codex, PR #73).
        start_new_session=(sys.platform != "win32"),
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
        # os.kill(pid, 0) raises if the pid no longer exists — BUT on POSIX a
        # killed process can briefly linger as an unreaped ZOMBIE, for which
        # os.kill(pid, 0) still SUCCEEDS even though it's dead (Codex P2, PR #73).
        # Accept a zombie (Linux /proc state 'Z') as "gone".
        import os as _os

        def _is_dead_or_zombie(pid):
            try:
                _os.kill(pid, 0)
            except OSError:
                return True  # no such pid
            # Process responds to signal-0; check for zombie on Linux.
            try:
                with open(f"/proc/{pid}/stat", "r", encoding="utf-8") as _sf:
                    # field 3 (after "pid (comm)") is the state char.
                    state = _sf.read().rsplit(")", 1)[1].split()[0]
                    return state == "Z"
            except OSError:
                # /proc absent (macOS) — can't be a zombie check; treat the
                # signal-0 success as still-alive (loop will retry).
                return False

        gone = False
        for _ in range(50):
            if _is_dead_or_zombie(child_pid):
                gone = True
                break
            time.sleep(0.1)
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


def test_rerun_oldcam_only_clears_abort_event_and_reports_abort(tmp_path, monkeypatch):
    """The standalone re-run worker must (a) CLEAR a stale _abort_event at start
    (so one aborted re-run doesn't make every later re-run abort instantly —
    Codex P2, PR #73), and (b) when an abort fires mid-stage, call
    completion_callback(False, ..., 'Aborted by user') instead of marching on
    into later stages. Runs the worker synchronously by patching Thread to run
    the target inline."""
    qm, logs = _make_qm()
    src = tmp_path / "clip.mp4"
    src.write_bytes(b"fake-mp4")

    # Pre-set a STALE abort event (as if a prior re-run was aborted).
    qm._abort_event.set()
    assert qm._abort_requested() is True

    # Config: rPPG on so the worker enters the rPPG stage; Oldcam selected.
    monkeypatch.setattr(qm, "get_config", lambda: {"loop_videos": False})
    monkeypatch.setattr(qm, "_rppg_enabled", lambda: True)
    monkeypatch.setattr(qm, "_get_oldcam_versions_to_run", lambda: ["v24"])
    monkeypatch.setattr(qm, "_get_selected_oldcam_versions", lambda: ["v24"])

    # Track whether the event was cleared by the time the first stage runs, and
    # then RE-set it to simulate the user aborting during the rPPG stage.
    seen = {"cleared_at_stage": None}

    def _fake_rppg(video, item):
        # The worker should have cleared the stale event before reaching here.
        seen["cleared_at_stage"] = not qm._abort_requested()
        qm._abort_event.set()  # user aborts mid-rPPG
        return None  # rPPG returns None (like a real abort/skip)

    monkeypatch.setattr(qm, "_rppg_video", _fake_rppg)
    # If the abort guard fails, these would run — make them loud.
    monkeypatch.setattr(qm, "_loop_video", lambda *a, **k: pytest.fail("loop ran after abort"))
    monkeypatch.setattr(qm, "_oldcam_video", lambda *a, **k: pytest.fail("oldcam ran after abort"))

    # Run the worker inline instead of in a daemon thread.
    import threading as _t

    class _InlineThread:
        def __init__(self, target=None, **k):
            self._target = target

        def start(self):
            self._target()

        def is_alive(self):
            return False

    monkeypatch.setattr(_t, "Thread", _InlineThread)

    results = []
    qm.rerun_oldcam_only(str(src), completion_callback=lambda *a: results.append(a))

    assert seen["cleared_at_stage"] is True, (
        "worker must clear the stale _abort_event before the first stage")
    assert results, "completion_callback must be called"
    ok, _src, out, err = results[-1]
    assert ok is False and out is None, "aborted re-run must report failure"
    assert err == "Aborted by user", f"expected abort message, got {err!r}"


def test_start_processing_drains_items_queued_during_rerun(tmp_path, monkeypatch):
    """Codex P2 (PR #73): the round-7 start_processing mutual-exclusion guard
    (reject while a standalone re-run thread is alive) must NOT strand items
    enqueued DURING a re-run. The re-run worker's finally drains pending work,
    and the guard lets the re-run thread itself through (current_thread)."""
    qm, logs = _make_qm()
    src = tmp_path / "clip.mp4"
    src.write_bytes(b"fake-mp4")

    monkeypatch.setattr(qm, "get_config", lambda: {"loop_videos": False})
    monkeypatch.setattr(qm, "_rppg_enabled", lambda: False)
    monkeypatch.setattr(qm, "_get_oldcam_versions_to_run", lambda: ["v24"])
    monkeypatch.setattr(qm, "_get_selected_oldcam_versions", lambda: ["v24"])
    # Oldcam "succeeds" trivially; while it runs, the user enqueues a pending item.
    out = tmp_path / "out.mp4"; out.write_bytes(b"x")

    class _Item:
        status = "pending"

    def _fake_oldcam(video, item):
        # Simulate the user adding a queue item mid-rerun.
        with qm.lock:
            qm.items.append(_Item())
        return str(out)

    monkeypatch.setattr(qm, "_oldcam_video", _fake_oldcam)
    monkeypatch.setattr(qm, "_last_oldcam_run_summary", {"outputs": [str(out)]}, raising=False)

    started = {"n": 0}
    real_start = qm.start_processing

    def _counting_start():
        started["n"] += 1
        # Don't actually spawn a worker thread in the test; just record the call.

    monkeypatch.setattr(qm, "start_processing", _counting_start)

    # Run the rerun worker inline.
    import threading as _t

    class _InlineThread:
        def __init__(self, target=None, name=None, **k):
            self._target = target
        def start(self):
            self._target()
        def is_alive(self):
            return False

    monkeypatch.setattr(_t, "Thread", _InlineThread)
    qm.rerun_oldcam_only(str(src), completion_callback=lambda *a: None)

    assert started["n"] >= 1, (
        "items enqueued during the re-run must trigger start_processing in the "
        "re-run worker's finally (not stranded pending forever)"
    )


def test_start_processing_not_blocked_by_a_just_exited_worker():
    """Codex P2 (PR #73): start_processing must NOT reject when is_running is
    False just because a previous worker_thread object is still technically
    is_alive() (the thread set is_running=False on an empty queue and is about
    to return). The is_running flag (lock-guarded) is the authoritative guard;
    a stale-but-alive thread ref must not strand a newly-enqueued item."""
    qm, _ = _make_qm()

    # Simulate a previous worker that is still 'alive' but already done
    # (is_running already cleared, as on the empty-queue exit).
    class _StillAlive:
        def is_alive(self):
            return True

        def start(self):
            pass

    qm.worker_thread = _StillAlive()
    qm.is_running = False
    qm.is_paused = False

    started = {"n": 0}
    import threading as _t

    class _InlineThread:
        def __init__(self, target=None, daemon=None, **k):
            started["n"] += 1

        def start(self):
            pass

    import unittest.mock as _mock
    with _mock.patch.object(_t, "Thread", _InlineThread):
        qm.start_processing()
    assert started["n"] == 1, (
        "start_processing must launch a worker even when a just-exited worker "
        "thread is still is_alive() (is_running=False is authoritative)"
    )


def test_start_processing_resets_is_running_if_thread_start_fails(monkeypatch):
    """If worker_thread.start() raises (OS resource limit / interpreter
    shutdown), is_running must roll back to False — otherwise the queue is
    permanently wedged (thinks it's busy, no worker) until app restart
    (gemini, PR #73)."""
    qm, logs = _make_qm()
    qm.is_running = False
    qm.is_paused = False

    import threading as _t

    class _FailingThread:
        def __init__(self, target=None, daemon=None, **k):
            pass

        def start(self):
            raise RuntimeError("can't start new thread")

    monkeypatch.setattr(_t, "Thread", _FailingThread)
    qm.start_processing()  # must not raise
    assert qm.is_running is False, "is_running must reset after a failed start"
    assert qm.worker_thread is None
    assert any(lvl == "error" for lvl, _m in logs), "the failure should be logged"
