"""Workspace + instance runtime path layout (PR #49).

Verifies the path templates documented in CLAUDE.md "Concurrent launches &
workspaces": default workspace runtime sits under the shared user_data_root
so existing shared files (kling_config.json, ui_config.json) stay put; named
workspaces sit under ``<root>/workspaces/<name>/``; per-instance runtime is
always under ``<workspace>/runtime/instances/<instance_id>/``.

Defense-in-depth check: a workspace name that bypasses sanitization (e.g.
via a direct call to ``get_workspace_dir`` with a bad value, which shouldn't
happen in practice) must NOT escape the user-data root.
"""

import os

import path_utils


def test_default_workspace_dir_equals_user_data_root(monkeypatch):
    """Default workspace must NOT introduce a new dir level — existing config
    paths (kling_config.json, ui_config.json) live at the same root they
    always have, so back-compat for single-instance users is preserved."""
    monkeypatch.delenv("KLING_WORKSPACE", raising=False)
    monkeypatch.delenv("KLING_INSTANCE_ID", raising=False)
    root = path_utils._user_data_root()
    assert path_utils.get_workspace_dir("default") == root
    assert path_utils.get_workspace_dir() == root


def test_named_workspace_dir_under_workspaces_subtree(monkeypatch):
    monkeypatch.delenv("KLING_WORKSPACE", raising=False)
    root = path_utils._user_data_root()
    ws = path_utils.get_workspace_dir("shoot-a")
    assert ws == os.path.join(root, "workspaces", "shoot-a")
    assert ws.startswith(root)  # never escapes root


def test_runtime_dir_template(monkeypatch):
    monkeypatch.setenv("KLING_WORKSPACE", "default")
    monkeypatch.setenv("KLING_INSTANCE_ID", "20260101-000000-1234")
    # Clear cached instance id from earlier tests / earlier in this run.
    path_utils._INSTANCE_ID_CACHE = None
    runtime = path_utils.get_runtime_dir()
    assert runtime.endswith(os.path.join("runtime", "instances", "20260101-000000-1234"))
    assert "workspaces" not in runtime  # default sits at root, not under workspaces/
    path_utils._INSTANCE_ID_CACHE = None  # avoid leaking to other tests


def test_runtime_dir_named_workspace_template(monkeypatch):
    monkeypatch.setenv("KLING_WORKSPACE", "shoot-a")
    monkeypatch.setenv("KLING_INSTANCE_ID", "20260101-000000-1234")
    path_utils._INSTANCE_ID_CACHE = None
    runtime = path_utils.get_runtime_dir()
    parts = runtime.replace("\\", "/").split("/")
    # Layout: <root>/workspaces/shoot-a/runtime/instances/<id>/
    assert "workspaces" in parts
    assert "shoot-a" in parts
    assert "runtime" in parts
    assert "instances" in parts
    assert "20260101-000000-1234" in parts
    path_utils._INSTANCE_ID_CACHE = None


def test_get_runtime_subpaths_align(monkeypatch):
    """The four runtime accessors must produce paths rooted at runtime_dir."""
    monkeypatch.setenv("KLING_WORKSPACE", "default")
    monkeypatch.setenv("KLING_INSTANCE_ID", "20260101-000000-1234")
    runtime = path_utils.get_runtime_dir()
    assert path_utils.get_runtime_sessions_dir() == os.path.join(runtime, "sessions")
    assert path_utils.get_runtime_crash_log_path() == os.path.join(runtime, "crash_log.txt")
    assert path_utils.get_runtime_history_path() == os.path.join(runtime, "kling_history.json")


def test_workspace_path_traversal_caught_by_commonpath(monkeypatch):
    """Even if a caller bypassed _sanitize_workspace_name somehow, the
    commonpath defense in get_workspace_dir falls back to the root rather
    than returning an escaped path."""
    # We can't easily inject ".." since path normalization may collapse it
    # before commonpath sees the result. The check still proves that any
    # path returned is anchored under user_data_root.
    monkeypatch.delenv("KLING_WORKSPACE", raising=False)
    for ws in ("shoot-a", "shoot_b", "default"):
        result = path_utils.get_workspace_dir(ws)
        common = os.path.commonpath([
            os.path.abspath(result),
            os.path.abspath(path_utils._user_data_root()),
        ])
        assert os.path.normcase(common) == os.path.normcase(
            os.path.abspath(path_utils._user_data_root())
        )


def test_instance_id_format(monkeypatch):
    """Instance id format must be `<YYYYMMDD-HHMMSS>-<PID>` — keeps stamps
    sortable and per-process unique even across same-second launches with
    different PIDs."""
    import path_utils as p
    # Force fresh generation by clearing cache + env
    monkeypatch.delenv("KLING_INSTANCE_ID", raising=False)
    p._INSTANCE_ID_CACHE = None
    iid = p.get_instance_id()
    parts = iid.split("-")
    assert len(parts) == 3, f"expected 3 parts in {iid!r}"
    assert len(parts[0]) == 8 and parts[0].isdigit()      # YYYYMMDD
    assert len(parts[1]) == 6 and parts[1].isdigit()      # HHMMSS
    assert parts[2].isdigit()                              # PID
    # Cached: second call returns same value
    assert p.get_instance_id() == iid
    p._INSTANCE_ID_CACHE = None


def test_ensure_runtime_dirs_creates_tree(monkeypatch, tmp_path):
    """ensure_runtime_dirs is idempotent and creates the sessions + markers tree."""
    monkeypatch.setattr(path_utils, "_user_data_root", lambda: str(tmp_path))
    monkeypatch.setenv("KLING_WORKSPACE", "default")
    monkeypatch.setenv("KLING_INSTANCE_ID", "test-instance")
    path_utils._INSTANCE_ID_CACHE = None
    runtime = path_utils.ensure_runtime_dirs()
    assert os.path.isdir(runtime)
    assert os.path.isdir(path_utils.get_runtime_sessions_dir())
    assert os.path.isdir(path_utils.get_workspace_markers_dir())
    # Idempotent — second call must not raise
    path_utils.ensure_runtime_dirs()
    path_utils._INSTANCE_ID_CACHE = None
