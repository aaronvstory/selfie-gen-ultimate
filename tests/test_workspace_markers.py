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


def test_list_active_excludes_stale(monkeypatch, tmp_path):
    """Stale markers (mtime > 24h) are excluded from list_active_instances
    so a kill -9'd predecessor doesn't pollute the active count forever."""
    _setup_tmp_root(monkeypatch, tmp_path)
    path_utils.ensure_runtime_dirs("default", "live-iid")
    fresh = wm.register_instance("default", "live-iid", str(tmp_path / "live"))

    # Plant a stale marker by hand
    stale_path = os.path.join(path_utils.get_workspace_markers_dir("default"), "stale.json")
    with open(stale_path, "w", encoding="utf-8") as f:
        json.dump({"instance_id": "stale", "workspace": "default", "pid": 99999,
                   "started_at": "2020-01-01T00:00:00", "cwd": "/", "runtime_dir": "/"}, f)
    # Back-date its mtime to 30 days ago
    old_time = time.time() - (30 * 24 * 60 * 60)
    os.utime(stale_path, (old_time, old_time))

    active = wm.list_active_instances("default")
    ids = {m["instance_id"] for m in active}
    assert "live-iid" in ids
    assert "stale" not in ids, "stale marker leaked into active list"
    wm.release_instance(fresh)


def test_cleanup_stale_markers_removes_only_stale(monkeypatch, tmp_path):
    _setup_tmp_root(monkeypatch, tmp_path)
    path_utils.ensure_runtime_dirs("default", "live-iid")
    fresh = wm.register_instance("default", "live-iid", str(tmp_path / "live"))
    markers_dir = path_utils.get_workspace_markers_dir("default")

    stale_path = os.path.join(markers_dir, "stale.json")
    with open(stale_path, "w", encoding="utf-8") as f:
        json.dump({"instance_id": "stale", "workspace": "default", "pid": 99999,
                   "started_at": "2020-01-01T00:00:00", "cwd": "/", "runtime_dir": "/"}, f)
    old_time = time.time() - (30 * 24 * 60 * 60)
    os.utime(stale_path, (old_time, old_time))

    removed = wm.cleanup_stale_markers("default")
    assert removed == 1
    assert not os.path.exists(stale_path)
    assert os.path.isfile(fresh)  # fresh untouched
    wm.release_instance(fresh)


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
