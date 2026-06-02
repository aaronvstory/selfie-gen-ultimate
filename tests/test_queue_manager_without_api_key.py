"""Regression: the QueueManager must initialize even WITHOUT a fal.ai API key.

Bug (2026-06-03, friend + user both hit it): `_init_generator` returned early
when `falai_api_key` was empty, leaving `self.queue_manager = None`. Every queue
action + every LOCAL re-run (rPPG / Oldcam / Loop) then failed with "Queue
manager not initialized" — the user couldn't even test rPPG without first
configuring a generation key, even though rPPG is a local post-process that
never touches the Kling generator.

Fix: build the generator only when a key is present (else None + a targeted
warning), but ALWAYS create the queue manager. These tests drive
`MainWindow._init_generator` against a lightweight stub (no Tk window needed)
so the contract is locked.
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

_REPO_ROOT = Path(__file__).resolve().parent.parent
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))


class _Stub:
    """Minimal stand-in exposing only what _init_generator reads/writes."""

    def __init__(self, config):
        self.config = config
        self.generator = None
        self.queue_manager = None
        self.logs = []

    # _init_generator uses these callbacks/attrs:
    def _log(self, msg, level="info"):
        self.logs.append((level, msg))

    def _log_thread_safe(self, msg, level="info"):
        self.logs.append((level, msg))

    def _update_queue_display_thread_safe(self):
        pass

    def _on_item_complete(self, item):
        pass

    # _init_generator triggers the in-app GPU bootstrap; no-op in the stub
    # (the dedicated test overrides this to count the call).
    def _start_gpu_bootstrap_async(self):
        pass


def _run_init_generator(config):
    from kling_gui import main_window as mw

    if not mw.HAS_GENERATOR:
        pytest.skip("generator backend unavailable in this env")
    stub = _Stub(config)
    # Bind the real method to the stub and run it.
    mw.KlingGUIWindow._init_generator(stub)
    return stub, mw


def test_queue_manager_created_without_api_key():
    """No fal.ai key -> generator is None, but the queue manager IS created so
    local re-runs (rPPG/Oldcam/Loop) work."""
    stub, _mw = _run_init_generator({"falai_api_key": ""})
    assert stub.queue_manager is not None, (
        "queue_manager must be created even without a fal.ai key — otherwise "
        "rPPG/Oldcam re-runs fail with 'Queue manager not initialized'"
    )
    assert stub.generator is None, "no key -> no live generator"
    # The user must get a helpful, non-fatal warning, not silence.
    joined = " ".join(m for _lvl, m in stub.logs).lower()
    assert "re-run" in joined or "rerun" in joined or "generation disabled" in joined


def test_queue_manager_created_with_api_key():
    """A present key -> both the generator and queue manager are created."""
    stub, _mw = _run_init_generator({
        "falai_api_key": "test-key-123",
        "freeimage_api_key": "",
        "verbose_logging": False,
    })
    assert stub.queue_manager is not None
    # Generator may still be None if FalAIKlingGenerator construction raised on a
    # fake key, but the queue manager must exist regardless (the whole point).


def test_generator_construction_failure_still_creates_queue_manager(monkeypatch):
    """If FalAIKlingGenerator() raises (bad key, network), the queue manager
    must STILL be created so local re-runs survive a generator-init failure."""
    from kling_gui import main_window as mw

    if not mw.HAS_GENERATOR:
        pytest.skip("generator backend unavailable")

    def _boom(*a, **k):
        raise RuntimeError("simulated generator init failure")

    monkeypatch.setattr(mw, "FalAIKlingGenerator", _boom)
    stub = _Stub({"falai_api_key": "key"})
    mw.KlingGUIWindow._init_generator(stub)
    assert stub.queue_manager is not None
    assert stub.generator is None


def test_process_queue_with_none_generator_fails_item_gracefully():
    """C1 regression (code-review 2026-06-03): the QueueManager is now created
    with generator=None for key-less users. If such a user drops a file to
    GENERATE, the _process_queue worker must NOT crash on
    `self.generator.update_prompt_slot()` — it must fail the item with a clear
    "add a key" message and keep the worker alive."""
    import sys

    if str(_REPO_ROOT) not in sys.path:
        sys.path.insert(0, str(_REPO_ROOT))
    from kling_gui.queue_manager import QueueManager, QueueItem

    logs = []
    completed = []
    qm = QueueManager(
        generator=None,
        config_getter=lambda: {"current_prompt_slot": 1, "current_model": "x"},
        log_callback=lambda m, lvl="info": logs.append((lvl, m)),
        queue_update_callback=lambda: None,
        processing_complete_callback=lambda item: completed.append(item),
    )
    # Seed one pending item directly + drive ONE worker pass (no thread/network).
    item = QueueItem(path="C:/fake/clip.mp4")
    item.status = "pending"
    qm.items = [item]
    qm._stop_flag = False

    # Run the worker but stop it after the first item so it doesn't loop.
    orig = qm._get_next_pending
    calls = {"n": 0}

    def _one(*a, **k):
        calls["n"] += 1
        return orig(*a, **k) if calls["n"] == 1 else None

    qm._get_next_pending = _one
    qm._process_queue()  # must NOT raise

    assert item.status == "failed"
    assert item.error_message and "key" in item.error_message.lower()
    assert completed and completed[0] is item
    joined = " ".join(m for _lvl, m in logs).lower()
    assert "api key" in joined


def test_init_generator_triggers_gpu_bootstrap_async():
    """v2.17: _init_generator must kick off the in-app GPU (CuPy) bootstrap so
    rPPG GPU acceleration is automatic regardless of launch method (the bug:
    CuPy install lived only in run_gui.bat's post-dep-sync step, so a direct
    launch or a fresh-install rPPG-before-sync-finished left CuPy uninstalled
    -> rPPG silently on CPU)."""
    from kling_gui import main_window as mw

    if not mw.HAS_GENERATOR:
        pytest.skip("generator backend unavailable")

    called = {"n": 0}
    stub = _Stub({"falai_api_key": ""})
    stub._start_gpu_bootstrap_async = lambda: called.__setitem__("n", called["n"] + 1)
    mw.KlingGUIWindow._init_generator(stub)
    assert called["n"] == 1, "_init_generator must call _start_gpu_bootstrap_async()"


def test_start_gpu_bootstrap_async_is_nonblocking_and_safe(monkeypatch):
    """The async bootstrap must spawn a daemon thread and never raise into the
    UI, even if gpu_bootstrap import/run fails. Honours KLING_SKIP_GPU_BOOTSTRAP."""
    from kling_gui import main_window as mw

    if not mw.HAS_GENERATOR:
        pytest.skip("generator backend unavailable")

    # Opt-out path: must early-return, spawn nothing.
    monkeypatch.setenv("KLING_SKIP_GPU_BOOTSTRAP", "1")
    stub = _Stub({})
    mw.KlingGUIWindow._start_gpu_bootstrap_async(stub)  # must not raise

    # Normal path: spawns a daemon thread; we just assert it returns promptly
    # and doesn't raise (the worker is best-effort and swallows everything).
    monkeypatch.delenv("KLING_SKIP_GPU_BOOTSTRAP", raising=False)
    stub2 = _Stub({})
    mw.KlingGUIWindow._start_gpu_bootstrap_async(stub2)  # must not raise
