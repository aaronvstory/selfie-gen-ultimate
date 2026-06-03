"""Unit tests for scripts/cuda_dll_paths.py — the shared NVIDIA DLL-dir helper.

These run WITHOUT a GPU (pure logic + monkeypatched filesystem), so CI guards
the idempotence + crash-safety contracts the external review (PR #72) flagged:
a second register call must not add a second handle, and a filesystem error on
one root must never crash rPPG import.
"""
from __future__ import annotations

import os
import sys
from pathlib import Path

import pytest

_SCRIPTS_DIR = Path(__file__).resolve().parent.parent / "scripts"
if str(_SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(_SCRIPTS_DIR))

import cuda_dll_paths  # noqa: E402


@pytest.fixture(autouse=True)
def _reset_module_state(monkeypatch):
    """Each test starts with a clean module-level registry + handle list so the
    idempotence assertions are deterministic regardless of prior imports."""
    monkeypatch.setattr(cuda_dll_paths, "_REGISTERED_CUDA_DLL_DIRS", set())
    monkeypatch.setattr(cuda_dll_paths, "_CUDA_DLL_DIR_HANDLES", [])
    yield


def _make_fake_nvidia_tree(tmp_path: Path) -> Path:
    """Build a site-packages/nvidia/cu13/bin/x86_64 + cuda_nvrtc/bin layout."""
    sp = tmp_path / "site-packages"
    (sp / "nvidia" / "cu13" / "bin" / "x86_64").mkdir(parents=True)
    (sp / "nvidia" / "cuda_nvrtc" / "bin").mkdir(parents=True)
    return sp


@pytest.mark.skipif(
    not hasattr(os, "add_dll_directory"),
    reason="add_dll_directory is Windows-only; the helper is a no-op elsewhere",
)
def test_register_is_idempotent_across_calls(tmp_path, monkeypatch):
    """A second register_cuda_dll_dirs() must NOT add a second handle or report
    the dir as newly registered (external review PR #72 — handle leak + PATH
    growth in a long-lived GUI process)."""
    sp = _make_fake_nvidia_tree(tmp_path)
    monkeypatch.setattr(cuda_dll_paths, "_candidate_site_packages", lambda: [str(sp)])
    # add_dll_directory would reject a non-real dir handle on a fake tree; stub it.
    monkeypatch.setattr(os, "add_dll_directory", lambda d: object())

    first = cuda_dll_paths.register_cuda_dll_dirs()
    handles_after_first = len(cuda_dll_paths._CUDA_DLL_DIR_HANDLES)
    second = cuda_dll_paths.register_cuda_dll_dirs()

    assert first, "first call should register the cu13/bin/x86_64 + cuda_nvrtc/bin dirs"
    assert second == [], "second call must register nothing (idempotent)"
    assert len(cuda_dll_paths._CUDA_DLL_DIR_HANDLES) == handles_after_first, (
        "second call must not append more os.add_dll_directory handles"
    )


@pytest.mark.skipif(
    not hasattr(os, "add_dll_directory"),
    reason="add_dll_directory is Windows-only",
)
def test_register_skips_bin_parent_when_x86_64_child_exists(tmp_path, monkeypatch):
    """cu13 ships DLLs in bin/x86_64; the bin parent must be skipped (no wasted
    handle/PATH entry). cu12's cuda_nvrtc/bin has no x86_64 child so it's kept."""
    sp = _make_fake_nvidia_tree(tmp_path)
    monkeypatch.setattr(cuda_dll_paths, "_candidate_site_packages", lambda: [str(sp)])
    monkeypatch.setattr(os, "add_dll_directory", lambda d: object())

    registered = [os.path.normcase(os.path.normpath(d)) for d in cuda_dll_paths.register_cuda_dll_dirs()]
    cu13_bin = os.path.normcase(os.path.normpath(str(sp / "nvidia" / "cu13" / "bin")))
    cu13_x86 = os.path.normcase(os.path.normpath(str(sp / "nvidia" / "cu13" / "bin" / "x86_64")))
    cu12_bin = os.path.normcase(os.path.normpath(str(sp / "nvidia" / "cuda_nvrtc" / "bin")))

    assert cu13_x86 in registered, "cu13 bin/x86_64 (where the DLLs are) must register"
    assert cu13_bin not in registered, "cu13 bin parent must be skipped (DLLs in x86_64 child)"
    assert cu12_bin in registered, "cu12 cuda_nvrtc/bin (no x86_64 child) must register"


@pytest.mark.skipif(
    not hasattr(os, "add_dll_directory"),
    reason="add_dll_directory is Windows-only",
)
def test_register_survives_filesystem_error_on_a_root(tmp_path, monkeypatch):
    """A filesystem error (antivirus quarantine, restricted ACL) on one root
    must be swallowed — the helper must never crash rPPG import (external
    review PR #72). Worst case: that root is skipped."""
    sp = _make_fake_nvidia_tree(tmp_path)
    monkeypatch.setattr(cuda_dll_paths, "_candidate_site_packages", lambda: [str(sp)])
    monkeypatch.setattr(os, "add_dll_directory", lambda d: object())

    real_isdir = os.path.isdir

    def _boom_isdir(p):
        if "nvidia" in str(p):
            raise OSError("simulated restricted filesystem")
        return real_isdir(p)

    monkeypatch.setattr(os.path, "isdir", _boom_isdir)
    # Must NOT raise — degrades to an empty result.
    result = cuda_dll_paths.register_cuda_dll_dirs()
    assert result == [], "a root that raises OSError must be skipped, not crash"


def test_register_is_noop_without_add_dll_directory(monkeypatch):
    """Off-Windows (no os.add_dll_directory) the helper is a clean no-op."""
    monkeypatch.delattr(os, "add_dll_directory", raising=False)
    assert cuda_dll_paths.register_cuda_dll_dirs() == []
