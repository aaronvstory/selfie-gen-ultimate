"""Unit tests for scripts/uv_sync_deps.py + scripts/ensure_uv.py (v2.20).

These exercise the ORCHESTRATION + fallback wiring with monkeypatched
sub-steps (no real uv install / network), the same way the launcher dep tests
mock pip. The real install path is covered by the env-gated fresh-sync test in
test_uv_lock_imports.py.
"""

from __future__ import annotations

import importlib
import os
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT / "scripts"))

uv_sync_deps = importlib.import_module("uv_sync_deps")
ensure_uv = importlib.import_module("ensure_uv")


# --------------------------------------------------------------------------
# uv_sync_deps fallback contract
# --------------------------------------------------------------------------

def test_missing_lock_falls_back_to_pip(tmp_path):
    """No uv.lock in the project -> exit 3 (pip fallback), no uv work."""
    rc = uv_sync_deps.main(["--project", str(tmp_path), "--quiet"])
    assert rc == uv_sync_deps.FALLBACK_TO_PIP == 3


def test_uv_unavailable_falls_back_to_pip(tmp_path, monkeypatch):
    """uv.lock present but uv can't be installed -> exit 3 (pip fallback)."""
    (tmp_path / "uv.lock").write_text("# fake lock\n", encoding="utf-8")
    monkeypatch.setattr(ensure_uv, "ensure_uv", lambda *, quiet=False: None)
    rc = uv_sync_deps.main(["--project", str(tmp_path), "--quiet"])
    assert rc == 3


def test_sync_success_returns_zero(tmp_path, monkeypatch):
    """uv present + sync produces an env interpreter -> exit 0."""
    (tmp_path / "uv.lock").write_text("# fake lock\n", encoding="utf-8")
    monkeypatch.setattr(ensure_uv, "ensure_uv", lambda *, quiet=False: "uv")

    uv_torch_select = importlib.import_module("uv_torch_select")
    # Fake a successful sync: create the env interpreter where uv_sync_deps
    # looks for it, and have the selector return 0.
    canonical = "venv" if sys.platform == "win32" else ".venv-macos"
    if sys.platform == "win32":
        py = tmp_path / canonical / "Scripts" / "python.exe"
    else:
        py = tmp_path / canonical / "bin" / "python"
    py.parent.mkdir(parents=True, exist_ok=True)
    py.write_text("", encoding="utf-8")
    monkeypatch.setattr(uv_torch_select, "main", lambda argv=None: 0)

    # Clear any inherited UV_PROJECT_ENVIRONMENT so the canonical default is used.
    monkeypatch.delenv("UV_PROJECT_ENVIRONMENT", raising=False)
    rc = uv_sync_deps.main(["--project", str(tmp_path), "--quiet"])
    assert rc == 0


def test_sync_no_env_materialized_falls_back(tmp_path, monkeypatch):
    """Selector returns 0 but no interpreter exists -> pip fallback (exit 3)."""
    (tmp_path / "uv.lock").write_text("# fake lock\n", encoding="utf-8")
    monkeypatch.setattr(ensure_uv, "ensure_uv", lambda *, quiet=False: "uv")
    uv_torch_select = importlib.import_module("uv_torch_select")
    monkeypatch.setattr(uv_torch_select, "main", lambda argv=None: 0)
    monkeypatch.delenv("UV_PROJECT_ENVIRONMENT", raising=False)
    rc = uv_sync_deps.main(["--project", str(tmp_path), "--quiet"])
    assert rc == 3


def test_selector_crash_falls_back_to_pip(tmp_path, monkeypatch):
    """CodeRabbit Major: a crash in uv_torch_select.main must degrade to the
    pip fallback (exit 3), NOT bubble out and break the launch."""
    (tmp_path / "uv.lock").write_text("# fake lock\n", encoding="utf-8")
    monkeypatch.setattr(ensure_uv, "ensure_uv", lambda *, quiet=False: "uv")
    uv_torch_select = importlib.import_module("uv_torch_select")

    def _boom(argv=None):
        raise RuntimeError("selector exploded")

    monkeypatch.setattr(uv_torch_select, "main", _boom)
    monkeypatch.delenv("UV_PROJECT_ENVIRONMENT", raising=False)
    rc = uv_sync_deps.main(["--project", str(tmp_path), "--quiet"])
    assert rc == 3


def test_python_arg_targets_callers_venv(tmp_path, monkeypatch):
    """CodeRabbit Major ×3: --python must make uv target the CALLER's venv dir,
    not the canonical default. Passing a .venv311 interpreter must set
    UV_PROJECT_ENVIRONMENT to that .venv311 dir."""
    (tmp_path / "uv.lock").write_text("# fake lock\n", encoding="utf-8")
    monkeypatch.setattr(ensure_uv, "ensure_uv", lambda *, quiet=False: "uv")
    uv_torch_select = importlib.import_module("uv_torch_select")

    captured = {}

    def _capture(argv=None):
        captured["env"] = os.environ.get("UV_PROJECT_ENVIRONMENT")
        return 0

    monkeypatch.setattr(uv_torch_select, "main", _capture)
    monkeypatch.delenv("UV_PROJECT_ENVIRONMENT", raising=False)

    # Caller resolved a NON-canonical venv (.venv311).
    if sys.platform == "win32":
        caller_py = tmp_path / ".venv311" / "Scripts" / "python.exe"
    else:
        caller_py = tmp_path / ".venv311" / "bin" / "python"
    caller_py.parent.mkdir(parents=True, exist_ok=True)
    caller_py.write_text("", encoding="utf-8")
    # venv_dir_for_python now requires a real pyvenv.cfg at the root (so a system
    # python like /usr/bin/python3 can't be mistaken for a venv — gemini HIGH).
    (tmp_path / ".venv311" / "pyvenv.cfg").write_text("home = x\n", encoding="utf-8")

    uv_sync_deps.main(
        ["--project", str(tmp_path), "--python", str(caller_py), "--quiet"]
    )
    assert captured["env"] == str(tmp_path / ".venv311"), (
        f"uv targeted {captured['env']!r}, expected the caller's .venv311 dir"
    )


def test_venv_dir_for_python_derivation(tmp_path):
    """The interpreter-path -> venv-root derivation handles both layouts AND
    refuses a non-venv (no pyvenv.cfg), so a system python can't be mistaken for
    a venv root (gemini HIGH, PR #73)."""
    assert uv_sync_deps.venv_dir_for_python(None) is None
    assert uv_sync_deps.venv_dir_for_python("") is None

    # A REAL venv layout (Scripts/bin + a pyvenv.cfg at root) -> derives root.
    sub = "Scripts" if sys.platform == "win32" else "bin"
    exe = "python.exe" if sys.platform == "win32" else "python"
    venv_root = tmp_path / "venv"
    (venv_root / sub).mkdir(parents=True)
    (venv_root / "pyvenv.cfg").write_text("home = x\n", encoding="utf-8")
    py = venv_root / sub / exe
    py.write_text("", encoding="utf-8")
    got = uv_sync_deps.venv_dir_for_python(str(py))
    assert got is not None and got.name == "venv"

    # A SYSTEM python (bin/ child but NO pyvenv.cfg at the parent's parent) must
    # NOT be mistaken for a venv — the whole point of the gemini HIGH fix.
    sysroot = tmp_path / "usr"
    (sysroot / "bin").mkdir(parents=True)
    sys_py = sysroot / "bin" / "python3"
    sys_py.write_text("", encoding="utf-8")
    assert uv_sync_deps.venv_dir_for_python(str(sys_py)) is None, (
        "system python (no pyvenv.cfg) must not resolve to a venv root"
    )

    # A path whose parent is neither Scripts nor bin -> None (not a venv layout).
    assert uv_sync_deps.venv_dir_for_python("/opt/python/python3") is None


# --------------------------------------------------------------------------
# ensure_uv discovery
# --------------------------------------------------------------------------

def test_find_uv_prefers_path(monkeypatch):
    monkeypatch.setattr(ensure_uv.shutil, "which", lambda name: "/usr/bin/uv")
    assert ensure_uv.find_uv() == "/usr/bin/uv"


def test_ensure_uv_returns_existing_without_install(monkeypatch):
    """If uv already exists, ensure_uv must NOT attempt an install."""
    monkeypatch.setattr(ensure_uv, "find_uv", lambda: "/already/here/uv")
    called = {"install": False}

    def _boom(quiet):  # pragma: no cover - must not run
        called["install"] = True
        return True

    monkeypatch.setattr(ensure_uv, "_install_windows", _boom)
    monkeypatch.setattr(ensure_uv, "_install_unix", _boom)
    assert ensure_uv.ensure_uv() == "/already/here/uv"
    assert called["install"] is False


def test_ensure_uv_main_print_path(monkeypatch, capsys):
    monkeypatch.setattr(ensure_uv, "ensure_uv", lambda *, quiet=False: "/x/uv")
    rc = ensure_uv.main(["--print-path"])
    assert rc == 0
    assert capsys.readouterr().out.strip() == "/x/uv"


def test_ensure_uv_main_failure_exit_1(monkeypatch):
    monkeypatch.setattr(ensure_uv, "ensure_uv", lambda *, quiet=False: None)
    assert ensure_uv.main(["--quiet"]) == 1


def test_bootstrap_logs_survive_none_stdout_pythonw(monkeypatch):
    """Under pythonw.exe (GUI, no console) sys.stdout/stderr is None. EVERY
    stdlib-bootstrap _log must NOT raise — a bare print() would AttributeError
    and crash the dep-bootstrap chain mid-provision (CodeRabbit + the round-6
    gpu_bootstrap/ensure_uv fixes; uv_sync_deps + uv_torch_select were the
    remaining gaps — PR #73)."""
    uv_torch_select = importlib.import_module("uv_torch_select")
    monkeypatch.setattr(sys, "stdout", None)
    monkeypatch.setattr(sys, "stderr", None)
    # None of these may raise.
    uv_sync_deps._log("x", quiet=False)
    ensure_uv._log("x", quiet=False)
    uv_torch_select._log("x")
