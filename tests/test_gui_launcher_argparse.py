"""gui_launcher.py argparse + workspace bootstrap (PR #49).

Verifies the bootstrap path in ``gui_launcher._resolve_workspace_and_instance``:
  1. ``parse_known_args`` (NOT ``parse_args``) so PyInstaller bootloader can
     inject ``--multiprocessing-fork`` and similar without raising SystemExit.
  2. Sets ``KLING_WORKSPACE`` and ``KLING_INSTANCE_ID`` env vars so the
     subsequent ``KlingGUIWindow()`` constructor (in main_window.py) sees
     the same identity via ``path_utils.get_workspace()`` / ``get_instance_id()``.
  3. Invalid workspace name → logs and falls back to ``"default"``, never raises.
  4. Honors ``KLING_WORKSPACE`` env when no CLI flag is given.
"""

import os
import sys

import gui_launcher
import path_utils


def _reset_state(monkeypatch):
    monkeypatch.delenv("KLING_WORKSPACE", raising=False)
    monkeypatch.delenv("KLING_INSTANCE_ID", raising=False)
    monkeypatch.delenv("KLING_ALLOW_SHARED_WORKSPACE", raising=False)
    path_utils._INSTANCE_ID_CACHE = None


def test_default_when_no_flag(monkeypatch, tmp_path):
    _reset_state(monkeypatch)
    monkeypatch.setattr(path_utils, "_user_data_root", lambda: str(tmp_path))
    monkeypatch.setattr(sys, "argv", ["gui_launcher"])
    ws, iid, runtime = gui_launcher._resolve_workspace_and_instance()
    assert ws == "default"
    assert iid is not None and iid == os.environ["KLING_INSTANCE_ID"]
    assert runtime is not None
    assert "runtime" in runtime
    assert os.path.isdir(runtime)


def test_named_workspace_flag(monkeypatch, tmp_path):
    _reset_state(monkeypatch)
    monkeypatch.setattr(path_utils, "_user_data_root", lambda: str(tmp_path))
    monkeypatch.setattr(sys, "argv", ["gui_launcher", "--workspace", "shoot-a"])
    ws, iid, runtime = gui_launcher._resolve_workspace_and_instance()
    assert ws == "shoot-a"
    assert os.environ["KLING_WORKSPACE"] == "shoot-a"
    assert runtime is not None
    assert "shoot-a" in runtime


def test_invalid_workspace_falls_back_to_default(monkeypatch, tmp_path):
    _reset_state(monkeypatch)
    monkeypatch.setattr(path_utils, "_user_data_root", lambda: str(tmp_path))
    monkeypatch.setattr(sys, "argv", ["gui_launcher", "--workspace", "../escape"])
    ws, iid, runtime = gui_launcher._resolve_workspace_and_instance()
    assert ws == "default", f"invalid name should fall back to default; got {ws!r}"


def test_env_var_used_when_no_flag(monkeypatch, tmp_path):
    """KLING_WORKSPACE env should be used when --workspace flag is absent."""
    _reset_state(monkeypatch)
    monkeypatch.setenv("KLING_WORKSPACE", "from-env")
    monkeypatch.setattr(path_utils, "_user_data_root", lambda: str(tmp_path))
    monkeypatch.setattr(sys, "argv", ["gui_launcher"])
    ws, iid, runtime = gui_launcher._resolve_workspace_and_instance()
    assert ws == "from-env"


def test_unknown_args_do_not_crash(monkeypatch, tmp_path):
    """parse_known_args (NOT parse_args) so PyInstaller bootloader flags pass through."""
    _reset_state(monkeypatch)
    monkeypatch.setattr(path_utils, "_user_data_root", lambda: str(tmp_path))
    monkeypatch.setattr(sys, "argv", [
        "gui_launcher",
        "--multiprocessing-fork", "fd=5",  # PyInstaller bootloader-style
        "--workspace", "x",
        "some-positional-arg",
    ])
    ws, iid, runtime = gui_launcher._resolve_workspace_and_instance()
    assert ws == "x"


def test_allow_shared_workspace_flag_sets_env(monkeypatch, tmp_path):
    _reset_state(monkeypatch)
    monkeypatch.setattr(path_utils, "_user_data_root", lambda: str(tmp_path))
    monkeypatch.setattr(sys, "argv", [
        "gui_launcher", "--workspace", "y", "--allow-shared-workspace",
    ])
    gui_launcher._resolve_workspace_and_instance()
    assert os.environ.get("KLING_ALLOW_SHARED_WORKSPACE") == "1"


def test_resolved_crash_log_path_routes_to_runtime(monkeypatch, tmp_path):
    """When workspace bootstrap succeeded, crash log goes to per-instance dir."""
    _reset_state(monkeypatch)
    monkeypatch.setattr(path_utils, "_user_data_root", lambda: str(tmp_path))
    monkeypatch.setattr(sys, "argv", ["gui_launcher", "--workspace", "crashy"])
    gui_launcher._resolve_workspace_and_instance()
    log_path = gui_launcher._resolved_crash_log_path()
    assert "instances" in log_path
    assert "crashy" in log_path
    # Parent dir was auto-created
    assert os.path.isdir(os.path.dirname(log_path))


def test_resolved_crash_log_path_falls_back_when_no_workspace_env(monkeypatch):
    """When no KLING_WORKSPACE env is set, fall back to legacy shared path.

    PR #49 M3 (review finding): the original test only asserted on basename,
    which would PASS even if the path was wrongly routed to a per-instance
    runtime dir via residual ``_INSTANCE_ID_CACHE`` from an earlier test.
    Tighten the assertion to verify the path is NOT a runtime/instances/...
    path, and clear the cache first to ensure determinism.
    """
    monkeypatch.delenv("KLING_WORKSPACE", raising=False)
    monkeypatch.delenv("KLING_INSTANCE_ID", raising=False)
    path_utils._INSTANCE_ID_CACHE = None
    log_path = gui_launcher._resolved_crash_log_path()
    # Legacy path: <user_data_root or app_dir>/crash_log.txt
    assert log_path.endswith("crash_log.txt")
    # CRITICAL: must NOT be a per-instance runtime path
    normalized = log_path.replace("\\", "/")
    assert "/runtime/instances/" not in normalized, (
        f"crash log routed to per-instance dir despite no KLING_WORKSPACE env: "
        f"{log_path!r} — residual cache leaked from an earlier test?"
    )
