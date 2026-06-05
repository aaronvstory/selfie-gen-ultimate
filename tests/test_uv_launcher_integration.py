"""Guard tests for the v2.20 uv fast-path wired into the launchers.

Mirrors the style of tests/test_launcher_health_check_loop.py: source-text
assertions over the .bat files proving the uv path is wired correctly and the
pip fallback is preserved. (The actual install is covered by the env-gated
fresh-sync test in test_uv_lock_imports.py.)
"""

from __future__ import annotations

from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent


def _read(rel: str) -> str:
    return (REPO_ROOT / rel).read_text(encoding="utf-8", errors="replace")


WINDOWS_LAUNCHERS = [
    "launchers/windows/run_gui.bat",
    "launchers/windows/run_cli.bat",
]


def test_win_uv_sync_helper_exists_and_is_crlf():
    p = REPO_ROOT / "scripts" / "win_uv_sync.bat"
    assert p.is_file(), "scripts/win_uv_sync.bat missing"
    data = p.read_bytes()
    # .bat MUST be CRLF (LF-only breaks every command). CR count == LF count.
    assert data.count(b"\r") == data.count(b"\n") and data.count(b"\r") > 0, (
        "win_uv_sync.bat is not uniformly CRLF"
    )
    # The /dev/null linter trap: a Windows .bat must never contain /dev/null.
    assert b"/dev/null" not in data, "win_uv_sync.bat contains POSIX /dev/null"


def test_win_uv_helper_calls_orchestrator_and_honours_pip_optout():
    src = _read("scripts/win_uv_sync.bat")
    assert "uv_sync_deps.py" in src, "helper must call scripts/uv_sync_deps.py"
    assert "KLING_USE_PIP" in src, "helper must honour KLING_USE_PIP opt-out"
    # Must gate on uv.lock + the orchestrator script existing (graceful absence).
    assert "uv.lock" in src
    # Must set UV_SYNCED so the caller can branch.
    assert "UV_SYNCED" in src


@pytest.mark.parametrize("bat", WINDOWS_LAUNCHERS)
def test_windows_launcher_calls_uv_fast_path(bat):
    src = _read(bat)
    assert "win_uv_sync.bat" in src, f"{bat} must call the uv fast-path helper"
    # On uv success it must skip to launch (not fall into the pip block).
    assert "UV_SYNCED" in src and "goto :launch" in src, (
        f"{bat} must skip to :launch when UV_SYNCED is set"
    )


@pytest.mark.parametrize("bat", WINDOWS_LAUNCHERS)
def test_windows_launcher_preserves_pip_fallback(bat):
    """The legacy pip path MUST remain intact below the uv block — the uv path
    is a fast-path, not a replacement (rollback safety)."""
    src = _read(bat)
    # The pip install subroutine + constraints threading must still be present.
    assert "INSTALL_REQUIREMENTS" in src, f"{bat} lost its pip install path"
    assert "constraints" in src.lower(), f"{bat} lost constraints.txt threading"


def test_uv_sync_deps_sets_generous_http_timeout():
    """uv's 30s default HTTP timeout aborts multi-GB CUDA-wheel extraction.
    uv_sync_deps must raise UV_HTTP_TIMEOUT (the failure the fresh-sync test
    caught on this box)."""
    src = _read("scripts/uv_sync_deps.py")
    assert "UV_HTTP_TIMEOUT" in src, "uv_sync_deps must raise UV_HTTP_TIMEOUT"


def test_preflight_helpers_try_uv_fast_path():
    """The shared sub-launcher preflight (oldcam/similarity/resemble) must try
    the uv fast-path so those sub-apps provision the FULL shared env via uv —
    still installing no divergent subset (they funnel through the same canonical
    uv sync). The pip health-check/repair stays as the fallback below it."""
    win = _read("scripts/win_preflight_shared_venv.bat")
    sh = _read("scripts/preflight_shared_venv.sh")
    assert "win_uv_sync.bat" in win, "win preflight must try the uv fast-path"
    assert "uv_sync.sh" in sh, "sh preflight must try the uv fast-path"
    # Fallback intact: the canonical health probe/repair must still be present.
    assert "dependency_health_check" in win and "--mode" in win
    assert "dependency_health_check" in sh and "--mode" in sh


def test_run_auto_inherits_uv_via_delegation():
    """run_auto.bat is a thin wrapper over the CLI chain — it must NOT install
    deps itself; it inherits the uv fast-path by delegating to run_cli.bat."""
    src = _read("run_auto.bat")
    assert "run_cli.bat" in src, "run_auto must delegate to the CLI launcher chain"
    # It must NOT carry its own pip install (the v2.19 thin-wrapper invariant).
    assert "pip install" not in src, "run_auto must not install deps directly"
