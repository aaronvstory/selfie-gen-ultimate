"""Unit tests for scripts/uv_torch_select.py — the launch-time GPU→uv-extra
decision (v2.20).

Mirrors the monkeypatch style of tests/test_gpu_bootstrap.py: the decision is
PURE (given a fake nvidia dict + platform), so the full table is testable with
no GPU. Parity with gpu_bootstrap._TORCH_CUDA_INDEX is asserted so a drift in
one map without the other fails CI.
"""

from __future__ import annotations

import importlib
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT / "scripts"))

uv_torch_select = importlib.import_module("uv_torch_select")
gpu_bootstrap = importlib.import_module("gpu_bootstrap")


def _set_platform(monkeypatch, plat: str) -> None:
    monkeypatch.setattr(uv_torch_select.sys, "platform", plat)


def test_macos_always_cpu_extra(monkeypatch):
    """macOS must NEVER select a CUDA extra — it has no CUDA. cpu extra (torch
    falls back to the PyPI MPS wheel). Hard-returns before any nvidia probe."""
    _set_platform(monkeypatch, "darwin")

    # Even if detect_nvidia were (wrongly) to report a GPU, mac stays cpu.
    monkeypatch.setattr(
        uv_torch_select, "detect_nvidia", lambda: {"cuda_major": 13, "driver_version": "9"}
    )
    extra, reason = uv_torch_select.resolve_extra()
    assert extra == "cpu"
    assert "macOS" in reason or "cpu" in reason.lower()


def test_windows_nvidia_cuda13_selects_cu128(monkeypatch):
    _set_platform(monkeypatch, "win32")
    monkeypatch.setattr(
        uv_torch_select,
        "detect_nvidia",
        lambda: {"cuda_major": 13, "driver_version": "591.86"},
    )
    extra, _ = uv_torch_select.resolve_extra()
    assert extra == "cu128"


def test_windows_nvidia_cuda12_selects_cu121(monkeypatch):
    _set_platform(monkeypatch, "win32")
    monkeypatch.setattr(
        uv_torch_select,
        "detect_nvidia",
        lambda: {"cuda_major": 12, "driver_version": "550.00"},
    )
    extra, _ = uv_torch_select.resolve_extra()
    assert extra == "cu121"


def test_windows_no_nvidia_selects_cpu(monkeypatch):
    _set_platform(monkeypatch, "win32")
    monkeypatch.setattr(uv_torch_select, "detect_nvidia", lambda: None)
    extra, _ = uv_torch_select.resolve_extra()
    assert extra == "cpu"


def test_windows_unmapped_cuda_major_selects_cpu(monkeypatch):
    """An NVIDIA box reporting CUDA 11.x (no wheel in our map) must degrade to
    CPU, never error."""
    _set_platform(monkeypatch, "win32")
    monkeypatch.setattr(
        uv_torch_select,
        "detect_nvidia",
        lambda: {"cuda_major": 11, "driver_version": "470.00"},
    )
    extra, _ = uv_torch_select.resolve_extra()
    assert extra == "cpu"


def test_gpu_bootstrap_unavailable_defaults_cpu(monkeypatch):
    """If gpu_bootstrap couldn't be imported, resolve_extra defaults to cpu."""
    monkeypatch.setattr(uv_torch_select, "detect_nvidia", None)
    monkeypatch.setattr(uv_torch_select, "resolve_torch_mode", None)
    extra, reason = uv_torch_select.resolve_extra()
    assert extra == "cpu"
    assert "unavailable" in reason.lower()


def test_extra_map_parity_with_gpu_bootstrap_cuda_index():
    """uv_torch_select._CUDA_MAJOR_TO_EXTRA must stay in lockstep with
    gpu_bootstrap._TORCH_CUDA_INDEX: every CUDA major mapped to a torch CUDA
    index must also map to a uv extra, and the extra's cuNNN tag must match the
    index URL's tag (12->cu121, 13->cu128). A drift in one map without the other
    would silently mis-route torch."""
    for cuda_major, index_url in gpu_bootstrap._TORCH_CUDA_INDEX.items():
        assert cuda_major in uv_torch_select._CUDA_MAJOR_TO_EXTRA, (
            f"CUDA major {cuda_major} has a torch index but no uv extra"
        )
        extra = uv_torch_select._CUDA_MAJOR_TO_EXTRA[cuda_major]
        # extra is e.g. "cu128"; index_url ends with the same "cuNNN" tag.
        assert index_url.rstrip("/").endswith(extra), (
            f"extra {extra} does not match torch index {index_url} for CUDA "
            f"{cuda_major}"
        )


def test_print_extra_emits_only_extra_name(monkeypatch, capsys):
    """--print-extra must print ONLY the extra name (a launcher captures it)."""
    _set_platform(monkeypatch, "win32")
    monkeypatch.setattr(uv_torch_select, "detect_nvidia", lambda: None)
    rc = uv_torch_select.main(["--print-extra"])
    assert rc == 0
    out = capsys.readouterr().out.strip()
    assert out == "cpu", f"expected bare 'cpu', got {out!r}"
