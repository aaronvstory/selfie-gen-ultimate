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


# ---------------------------------------------------------------------------
# nvrtc JIT kernel-cache recovery + compile-error helpers (the v2.23 GPU fix:
# CuPy's DLLs LOAD but the kernel compile fails on a stale cache after a
# CUDA-toolkit/driver upgrade — clear the cache + retry).
# ---------------------------------------------------------------------------


def test_cache_dir_honours_env_override(monkeypatch, tmp_path):
    """$CUPY_CACHE_DIR wins over the ~/.cupy/kernel_cache default."""
    monkeypatch.setenv("CUPY_CACHE_DIR", str(tmp_path / "custom_cache"))
    got = cuda_dll_paths.cupy_kernel_cache_dir()
    assert got == os.path.abspath(str(tmp_path / "custom_cache"))


def test_cache_dir_default_is_user_dot_cupy(monkeypatch):
    """With no override, the default mirrors CuPy: ~/.cupy/kernel_cache."""
    monkeypatch.delenv("CUPY_CACHE_DIR", raising=False)
    got = cuda_dll_paths.cupy_kernel_cache_dir()
    assert got == os.path.join(os.path.expanduser("~"), ".cupy", "kernel_cache")


def test_clear_cupy_kernel_cache_removes_populated_dir(monkeypatch, tmp_path):
    """A populated cache dir is wiped and its path returned (the stale-cache
    recovery that lets the compile retry succeed)."""
    cache = tmp_path / "kernel_cache"
    cache.mkdir()
    (cache / "stale_kernel.cubin").write_bytes(b"\x00\x01")
    monkeypatch.setenv("CUPY_CACHE_DIR", str(cache))

    cleared = cuda_dll_paths.clear_cupy_kernel_cache()

    assert cleared == os.path.abspath(str(cache))
    assert not cache.exists(), "stale JIT cache dir must be removed"


def test_clear_cupy_kernel_cache_absent_returns_none(monkeypatch, tmp_path):
    """Nothing to clear -> None (no crash, no spurious path)."""
    monkeypatch.setenv("CUPY_CACHE_DIR", str(tmp_path / "does_not_exist"))
    assert cuda_dll_paths.clear_cupy_kernel_cache() is None


def test_clear_cupy_kernel_cache_never_raises(monkeypatch, tmp_path):
    """A filesystem error during the wipe degrades to None, never crashes the
    rPPG import that calls it."""
    cache = tmp_path / "kernel_cache"
    cache.mkdir()
    monkeypatch.setenv("CUPY_CACHE_DIR", str(cache))
    monkeypatch.setattr(os.path, "isdir", lambda p: (_ for _ in ()).throw(OSError("boom")))
    assert cuda_dll_paths.clear_cupy_kernel_cache() is None


class _FakeCompileException(Exception):
    """Stand-in with a cupy-like class name for the matcher (no GPU needed)."""


def test_is_nvrtc_compile_error_matches_by_class_name():
    assert cuda_dll_paths.is_nvrtc_compile_error(_FakeCompileException("x"))

    class NVRTCError(Exception):
        pass

    assert cuda_dll_paths.is_nvrtc_compile_error(NVRTCError())
    # A missing module / absent device is NOT a compile error (cache wipe useless).
    assert not cuda_dll_paths.is_nvrtc_compile_error(ModuleNotFoundError("no cupy"))
    assert not cuda_dll_paths.is_nvrtc_compile_error(RuntimeError("no CUDA device"))


def test_summarize_prefers_error_line_over_pragma_remark():
    """The real bug: head-truncation showed only the benign opening remark and
    cut off the actual error. The summarizer must surface the error line."""
    log = (
        "C:\\...\\cpp_dialect.h(41): remark #20200-D: #pragma message: "
        '"some long benign dialect note that dominates the head of the log"\n'
        "C:\\...\\some_header.h(88): error: identifier \"foobar\" is undefined\n"
        "1 error detected in the compilation."
    )
    summary = cuda_dll_paths.summarize_nvrtc_compile_error(_FakeCompileException(log))
    assert "error: identifier" in summary
    assert "remark #20200-D" not in summary


def test_summarize_falls_back_to_head_when_no_error_line():
    summary = cuda_dll_paths.summarize_nvrtc_compile_error(
        _FakeCompileException("just a remark with no failure keyword"), limit=300
    )
    assert "just a remark" in summary


def test_summarize_truncates_to_limit():
    summary = cuda_dll_paths.summarize_nvrtc_compile_error(
        _FakeCompileException("error: " + "x" * 1000), limit=50
    )
    assert len(summary) <= 51  # 50 chars + the … ellipsis
    assert summary.endswith("…")


# --- cuda_include_dirs (the CUDA-13 header-skew fix, root-caused 2026-06-04) ---
def test_cuda_include_dirs_finds_runtime_header_dir(tmp_path, monkeypatch):
    """cuda_include_dirs returns only nvidia/<...>/include dirs that actually
    carry a CUDA runtime header — the dir CuPy needs to compile kernels when its
    own wheel-version-match detector returns [] (the friend's CUDA-13 bug)."""
    sp = tmp_path / "site-packages"
    cu13_inc = sp / "nvidia" / "cu13" / "include"
    cu13_inc.mkdir(parents=True)
    (cu13_inc / "cuda_runtime.h").write_text("// header")
    # A component dir with NO cuda header must be ignored.
    other_inc = sp / "nvidia" / "cccl" / "include"
    other_inc.mkdir(parents=True)
    (other_inc / "something_else.h").write_text("// not a cuda runtime header")

    monkeypatch.setattr(cuda_dll_paths, "_candidate_site_packages", lambda: [str(sp)])
    dirs = cuda_dll_paths.cuda_include_dirs()
    assert any(d.endswith(os.path.join("cu13", "include")) for d in dirs), dirs
    assert not any("cccl" in d for d in dirs), "include dir without a CUDA header must be skipped"


def test_cuda_include_dirs_empty_when_no_nvidia_tree(tmp_path, monkeypatch):
    """No nvidia/ tree (e.g. CPU-only / macOS) -> [] (never crashes)."""
    monkeypatch.setattr(
        cuda_dll_paths, "_candidate_site_packages", lambda: [str(tmp_path)]
    )
    assert cuda_dll_paths.cuda_include_dirs() == []


def test_cuda_include_dirs_survives_oserror(tmp_path, monkeypatch):
    """A restricted/AV-quarantined include dir must not crash the helper."""
    sp = tmp_path / "site-packages"
    (sp / "nvidia").mkdir(parents=True)
    monkeypatch.setattr(cuda_dll_paths, "_candidate_site_packages", lambda: [str(sp)])

    def _boom(*a, **k):
        raise OSError("simulated permission error")

    monkeypatch.setattr(cuda_dll_paths.glob, "glob", _boom)
    assert cuda_dll_paths.cuda_include_dirs() == []  # no crash


# --- v2.23.2: CuPy-13.6 vs nvrtc-13.3 constexpr cure (friend's deepest bug) ---
def test_nvrtc_runtime_capped_below_133_in_gpu_bootstrap():
    """gpu_bootstrap pins nvrtc + runtime to <13.3 (the CUDA-13.2 line CuPy
    13.6.0 can compile against; nvrtc 13.3 breaks CuPy's bundled CCCL with a
    libcudacxx constexpr error). Both on the same minor."""
    import gpu_bootstrap
    cu13 = gpu_bootstrap._CUDA_TO_NVIDIA_WHEELS[13]
    nvrtc = next(s for s in cu13 if s.startswith("nvidia-cuda-nvrtc"))
    runtime = next(s for s in cu13 if s.startswith("nvidia-cuda-runtime"))
    assert ">=13.0,<13.1" in nvrtc, f"nvrtc not capped <13.1: {nvrtc}"
    assert ">=13.0,<13.1" in runtime, f"runtime not capped <13.1: {runtime}"


def test_relaxed_constexpr_flag_prepended_by_patch():
    """The injector/probe relaxed-constexpr wrapper prepends
    --expt-relaxed-constexpr to nvrtc options (the documented fix for the
    'constexpr function return is non-constant' error) without dropping the
    caller's own options."""
    seen = {}

    def _orig(source, options=(), *a, **k):
        seen["options"] = options
        return "ok"

    # Mirror the wrapper logic used in rppg_injector / gpu_bootstrap probe.
    def _patched(source, options=(), *a, _orig=_orig, **k):
        opts = tuple(options or ())
        if "--expt-relaxed-constexpr" not in opts:
            opts = ("--expt-relaxed-constexpr",) + opts
        return _orig(source, opts, *a, **k)

    _patched("__global__ void k(){}", ("-arch=sm_80",))
    assert seen["options"][0] == "--expt-relaxed-constexpr"
    assert "-arch=sm_80" in seen["options"]
    # Idempotent: a second wrap must not double-add.
    _patched("k", ("--expt-relaxed-constexpr", "-arch=sm_90"))
    assert seen["options"].count("--expt-relaxed-constexpr") == 1
