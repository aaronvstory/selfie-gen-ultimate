"""Workspace marker lifecycle (PR #49).

Markers are best-effort liveness pings that let any process enumerate "what's
active in workspace X". They survive orderly close (deleted) but a kill -9
leaves them in place — the 24h stale-cleanup catches those at next launch.

Tests use monkeypatch to redirect ``path_utils._user_data_root`` to a tmp_path
so markers don't pollute the real user-data dir during pytest.
"""

import json
import os
import time

import path_utils
from kling_gui import workspace_markers as wm


def _setup_tmp_root(monkeypatch, tmp_path):
    """Point path_utils at a tmp tree and clear cached env."""
    monkeypatch.setattr(path_utils, "_user_data_root", lambda: str(tmp_path))
    monkeypatch.setenv("KLING_WORKSPACE", "default")
    monkeypatch.setenv("KLING_INSTANCE_ID", "test-iid")
    path_utils._INSTANCE_ID_CACHE = None


def test_register_and_release_roundtrip(monkeypatch, tmp_path):
    _setup_tmp_root(monkeypatch, tmp_path)
    runtime_dir = path_utils.ensure_runtime_dirs("default", "test-iid")
    marker = wm.register_instance("default", "test-iid", runtime_dir)
    assert marker is not None
    assert os.path.isfile(marker)
    with open(marker, "r", encoding="utf-8") as f:
        payload = json.load(f)
    assert payload["instance_id"] == "test-iid"
    assert payload["workspace"] == "default"
    assert payload["pid"] == os.getpid()
    assert payload["runtime_dir"] == runtime_dir

    wm.release_instance(marker)
    assert not os.path.isfile(marker)


def test_release_is_idempotent_on_missing(monkeypatch, tmp_path):
    """release_instance must NOT raise if the marker is already gone
    (kill -9 + atexit + next-launch cleanup_stale_markers can all race)."""
    _setup_tmp_root(monkeypatch, tmp_path)
    runtime_dir = path_utils.ensure_runtime_dirs("default", "test-iid")
    marker = wm.register_instance("default", "test-iid", runtime_dir)
    assert marker is not None
    os.remove(marker)
    # Must not raise.
    wm.release_instance(marker)
    wm.release_instance(None)  # also handles None gracefully


def test_list_active_excludes_dead_pid_markers(monkeypatch, tmp_path):
    """Markers whose PID is no longer running are excluded from
    list_active_instances so a kill -9'd predecessor doesn't pollute the
    active count forever. Round-2 H-2: PID probe, not mtime — uses PID
    999999999 (well outside any OS's plausible pid range)."""
    _setup_tmp_root(monkeypatch, tmp_path)
    path_utils.ensure_runtime_dirs("default", "live-iid")
    fresh = wm.register_instance("default", "live-iid", str(tmp_path / "live"))

    # Plant a marker with a clearly-dead pid
    dead_path = os.path.join(path_utils.get_workspace_markers_dir("default"), "dead.json")
    with open(dead_path, "w", encoding="utf-8") as f:
        json.dump({"instance_id": "dead", "workspace": "default", "pid": 999999999,
                   "started_at": "2020-01-01T00:00:00", "cwd": "/", "runtime_dir": "/"}, f)

    active = wm.list_active_instances("default")
    ids = {m["instance_id"] for m in active}
    assert "live-iid" in ids
    assert "dead" not in ids, "dead-pid marker leaked into active list"
    wm.release_instance(fresh)


def test_cleanup_stale_markers_removes_only_dead(monkeypatch, tmp_path):
    """cleanup_stale_markers sweeps dead-pid markers (round-2 H-2: PID probe,
    not mtime). A still-running fresh marker MUST survive even if it's
    been alive for arbitrarily long."""
    _setup_tmp_root(monkeypatch, tmp_path)
    path_utils.ensure_runtime_dirs("default", "live-iid")
    fresh = wm.register_instance("default", "live-iid", str(tmp_path / "live"))
    markers_dir = path_utils.get_workspace_markers_dir("default")

    dead_path = os.path.join(markers_dir, "dead.json")
    with open(dead_path, "w", encoding="utf-8") as f:
        json.dump({"instance_id": "dead", "workspace": "default", "pid": 999999999,
                   "started_at": "2020-01-01T00:00:00", "cwd": "/", "runtime_dir": "/"}, f)

    removed = wm.cleanup_stale_markers("default")
    assert removed == 1
    assert not os.path.exists(dead_path)
    assert os.path.isfile(fresh)  # fresh untouched (alive PID = this process)
    wm.release_instance(fresh)


def test_old_marker_with_alive_pid_is_kept(monkeypatch, tmp_path):
    """PR #49 H-2 (round-2 review finding): the earlier 24h-mtime cutoff
    deleted markers from sessions running >24h (overnight oldcam batches,
    long rPPG queues). New logic uses a PID probe — a marker whose PID is
    still alive must stay regardless of mtime.

    Use the test runner's own PID for the alive probe (guaranteed alive
    for the duration of the test). Back-date the marker's mtime to 30
    days ago to prove mtime alone no longer triggers deletion.
    """
    _setup_tmp_root(monkeypatch, tmp_path)
    markers_dir = path_utils.get_workspace_markers_dir("default")
    os.makedirs(markers_dir, exist_ok=True)
    long_running = os.path.join(markers_dir, "long-running-iid.json")
    with open(long_running, "w", encoding="utf-8") as f:
        json.dump({
            "instance_id": "long-running-iid",
            "workspace": "default",
            "pid": os.getpid(),  # the pytest process itself — alive
            "started_at": "2020-01-01T00:00:00",
            "cwd": str(tmp_path),
            "runtime_dir": str(tmp_path / "x"),
        }, f)
    # Back-date so the OLD logic would have swept it
    old_time = time.time() - (30 * 24 * 60 * 60)
    os.utime(long_running, (old_time, old_time))

    # cleanup_stale_markers MUST NOT remove the long-running marker
    removed = wm.cleanup_stale_markers("default")
    assert removed == 0, "PID-alive marker was deleted despite old mtime"
    assert os.path.exists(long_running)

    # list_active_instances MUST include the long-running marker
    active = wm.list_active_instances("default")
    ids = {m["instance_id"] for m in active}
    assert "long-running-iid" in ids, (
        "PID-alive marker excluded from active list despite old mtime — "
        "H-2 regression"
    )


def test_dead_pid_marker_is_swept(monkeypatch, tmp_path):
    """The flip side: a marker with a PID that's definitely NOT running
    (use a very high pid that the OS won't assign for years) must be
    swept by cleanup_stale_markers."""
    _setup_tmp_root(monkeypatch, tmp_path)
    markers_dir = path_utils.get_workspace_markers_dir("default")
    os.makedirs(markers_dir, exist_ok=True)
    dead_marker = os.path.join(markers_dir, "dead-iid.json")
    # PID 999999999 is well outside any plausible OS pid range
    with open(dead_marker, "w", encoding="utf-8") as f:
        json.dump({
            "instance_id": "dead-iid",
            "workspace": "default",
            "pid": 999999999,
            "started_at": "2026-01-01T00:00:00",
            "cwd": str(tmp_path),
            "runtime_dir": str(tmp_path / "x"),
        }, f)
    # Even with a fresh mtime, dead-PID marker should be swept
    removed = wm.cleanup_stale_markers("default")
    assert removed == 1
    assert not os.path.exists(dead_marker)


def test_cleanup_removes_orphan_runtime_dir_when_safe(monkeypatch, tmp_path):
    """PR #49 round-2 (Gemini review): when cleanup_stale_markers sweeps a
    dead-pid marker, it ALSO rmtrees the corresponding runtime/instances/<id>/
    dir if that dir contains only the expected ephemeral files (avoids
    accumulating dead-session dirs forever)."""
    _setup_tmp_root(monkeypatch, tmp_path)
    markers_dir = path_utils.get_workspace_markers_dir("default")
    os.makedirs(markers_dir, exist_ok=True)

    # Build a fake orphaned runtime dir with the expected ephemerals
    orphan_runtime = tmp_path / "runtime" / "instances" / "orphan-iid"
    orphan_runtime.mkdir(parents=True)
    (orphan_runtime / "crash_log.txt").write_text("crash text", encoding="utf-8")
    (orphan_runtime / "kling_history.json").write_text("[]", encoding="utf-8")
    (orphan_runtime / "sessions").mkdir()
    (orphan_runtime / "sessions" / "proj_autosave.json").write_text("{}", encoding="utf-8")

    dead_marker = os.path.join(markers_dir, "orphan-iid.json")
    with open(dead_marker, "w", encoding="utf-8") as f:
        json.dump({
            "instance_id": "orphan-iid",
            "workspace": "default",
            "pid": 999999999,
            "started_at": "2020-01-01T00:00:00",
            "cwd": "/",
            "runtime_dir": str(orphan_runtime),
        }, f)

    removed = wm.cleanup_stale_markers("default")
    assert removed == 1
    assert not os.path.exists(dead_marker)
    assert not orphan_runtime.exists(), "orphan runtime dir not cleaned up"


def test_cleanup_preserves_runtime_dir_with_unexpected_content(monkeypatch, tmp_path):
    """Safety check: if a runtime dir contains anything BEYOND the expected
    ephemerals (e.g. user dropped a manual save in there), the rmtree is
    skipped to prevent data loss. The marker is still removed."""
    _setup_tmp_root(monkeypatch, tmp_path)
    markers_dir = path_utils.get_workspace_markers_dir("default")
    os.makedirs(markers_dir, exist_ok=True)

    runtime_dir = tmp_path / "runtime" / "instances" / "user-data-iid"
    runtime_dir.mkdir(parents=True)
    (runtime_dir / "crash_log.txt").write_text("crash", encoding="utf-8")
    # Unexpected file — user dropped something they care about
    (runtime_dir / "manual-backup.zip").write_bytes(b"important data")

    dead_marker = os.path.join(markers_dir, "user-data-iid.json")
    with open(dead_marker, "w", encoding="utf-8") as f:
        json.dump({
            "instance_id": "user-data-iid",
            "workspace": "default",
            "pid": 999999999,
            "started_at": "2020-01-01T00:00:00",
            "cwd": "/",
            "runtime_dir": str(runtime_dir),
        }, f)

    removed = wm.cleanup_stale_markers("default")
    assert removed == 1
    assert not os.path.exists(dead_marker)
    # Runtime dir survives because manual-backup.zip is unexpected
    assert runtime_dir.exists()
    assert (runtime_dir / "manual-backup.zip").exists(), "user data destroyed"


def test_corrupt_marker_is_skipped(monkeypatch, tmp_path):
    """A torn write (e.g. process killed mid-write) leaves a malformed JSON
    marker. list_active_instances must skip it rather than crash."""
    _setup_tmp_root(monkeypatch, tmp_path)
    path_utils.ensure_runtime_dirs("default", "live")
    fresh = wm.register_instance("default", "live", str(tmp_path / "x"))
    markers_dir = path_utils.get_workspace_markers_dir("default")
    bad_path = os.path.join(markers_dir, "corrupt.json")
    with open(bad_path, "w", encoding="utf-8") as f:
        f.write("{ not valid json")

    active = wm.list_active_instances("default")
    ids = {m["instance_id"] for m in active}
    assert "live" in ids
    assert "corrupt" not in ids
    wm.release_instance(fresh)
