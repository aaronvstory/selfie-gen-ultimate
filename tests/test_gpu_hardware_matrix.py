"""Cross-hardware matrix: prove the GPU detection → torch/CuPy decision is
correct AND non-crashing across the machines this app actually ships to.

User question (2026-06-04): "will it detect and work on any other nvidia GPUs,
also Apple Silicon mac we won't break right (CPU only there), and different
driver versions etc?"

This drives the REAL pipeline — detect_nvidia() (with a faked nvidia-smi) →
resolve_torch_mode() → uv_torch_select.resolve_extra() — for every hardware /
driver / OS combination and asserts:

  * Various NVIDIA cards + driver branches (legacy header AND new 610+ header)
    map to a CUDA build (cu121/cu128) — no card is silently dropped to CPU.
  * Apple Silicon (darwin) NEVER selects CUDA and NEVER even probes nvidia-smi
    (mac has no CUDA; torch resolves to the PyPI MPS/CPU wheel).
  * No-GPU / no-driver / broken-nvidia-smi boxes degrade cleanly to CPU.
  * Every path returns a valid decision dict (never crashes / returns garbage).
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

_REPO_ROOT = Path(__file__).resolve().parent.parent
_SCRIPTS = _REPO_ROOT / "scripts"
for p in (str(_REPO_ROOT), str(_SCRIPTS)):
    if p not in sys.path:
        sys.path.insert(0, p)

import gpu_bootstrap  # noqa: E402

# uv_torch_select only exists on the uv line (it maps the same decision to a uv
# --extra). On the pip line it's absent — the equivalent decision lives in
# gpu_bootstrap.resolve_torch_mode, which we test directly. Import optionally so
# this matrix runs on BOTH lines; the uv-extra assertions skip when it's absent.
try:
    import uv_torch_select  # noqa: E402
except ModuleNotFoundError:
    uv_torch_select = None  # type: ignore[assignment]


class _Proc:
    def __init__(self, stdout, rc=0):
        self.stdout = stdout
        self.returncode = rc


def _fake_nvidia_smi(monkeypatch, *, driver_query, header):
    """Patch _resolve_nvidia_smi + subprocess.run to simulate a given GPU."""
    def _run(cmd, *a, **k):
        if any(isinstance(c, str) and c.startswith("--query-gpu") for c in cmd):
            if driver_query is None:
                return _Proc("", rc=1)
            return _Proc(driver_query, rc=0)
        if header is None:
            return _Proc("", rc=1)
        return _Proc(header, rc=0)

    monkeypatch.setattr(gpu_bootstrap, "_resolve_nvidia_smi", lambda: "nvidia-smi")
    monkeypatch.setattr(gpu_bootstrap.subprocess, "run", _run)


def _legacy_header(driver, cuda):
    return (
        f"NVIDIA-SMI {driver}       Driver Version: {driver}       "
        f"CUDA Version: {cuda}      \n"
    )


def _new610_header(driver, cuda_umd):
    return (
        f"| NVIDIA-SMI {driver}     KMD Version: {driver}     "
        f"CUDA UMD Version: {cuda_umd}   |\n"
    )


# (label, driver_query, header, expected_cuda_major, expected_uv_extra)
_NVIDIA_CASES = [
    # --- legacy header (driver ≤ ~570, e.g. the friend's CUDA 12.9 box) ----
    ("RTX 4080 / 576.80 / CUDA 12.9 (legacy)", "576.80\n",
     _legacy_header("576.80", "12.9"), 12, "cu121"),
    ("RTX 3090 / 535.98 / CUDA 12.2 (legacy)", "535.98\n",
     _legacy_header("535.98", "12.2"), 12, "cu121"),
    ("GTX 1660 / 528.49 / CUDA 12.0 (legacy)", "528.49\n",
     _legacy_header("528.49", "12.0"), 12, "cu121"),
    ("Datacenter A100 / 565.57 / CUDA 12.7 (legacy)", "565.57\n",
     _legacy_header("565.57", "12.7"), 12, "cu121"),
    ("Early CUDA-13 / 580.00 / CUDA 13.0 (legacy)", "580.00\n",
     _legacy_header("580.00", "13.0"), 13, "cu128"),
    # --- new 610+ header (the redesign that broke this box) ----------------
    ("RTX 4090 Laptop / 610.47 / CUDA UMD 13.3 (new)", "610.47\n",
     _new610_header("610.47", "13.3"), 13, "cu128"),
    ("Future RTX 50xx / 615.10 / CUDA UMD 13.5 (new)", "615.10\n",
     _new610_header("615.10", "13.5"), 13, "cu128"),
    ("New-header but CUDA 12.x / 600.12 / CUDA UMD 12.8", "600.12\n",
     _new610_header("600.12", "12.8"), 12, "cu121"),
]


@pytest.mark.parametrize("label,dq,hdr,exp_major,exp_extra", _NVIDIA_CASES)
def test_nvidia_card_maps_to_cuda(monkeypatch, label, dq, hdr, exp_major, exp_extra):
    """Every real NVIDIA card/driver maps to a CUDA build — none dropped to CPU."""
    _fake_nvidia_smi(monkeypatch, driver_query=dq, header=hdr)
    nv = gpu_bootstrap.detect_nvidia()
    assert nv is not None, f"{label}: detect_nvidia must SEE the GPU"
    assert nv["cuda_major"] == exp_major, f"{label}: wrong CUDA major"

    decision = gpu_bootstrap.resolve_torch_mode(platform_is_darwin=False, nvidia=nv)
    assert decision["mode"] == "cuda", f"{label}: must select a CUDA torch build"

    # And (uv line only) the uv path picks the matching extra via the same logic.
    if uv_torch_select is not None:
        monkeypatch.setattr(uv_torch_select.sys, "platform", "win32", raising=False)
        monkeypatch.setattr(uv_torch_select, "detect_nvidia", lambda: nv)
        extra, _reason = uv_torch_select.resolve_extra()
        assert extra == exp_extra, f"{label}: uv extra should be {exp_extra}, got {extra}"


def test_cuda_major_fallback_when_header_unreadable(monkeypatch):
    """New driver, GPU visible, but the header has NO CUDA field at all —
    the driver-branch fallback still picks CUDA (never drops a real GPU)."""
    _fake_nvidia_smi(
        monkeypatch,
        driver_query="611.00\n",
        header="| NVIDIA-SMI 611.00   KMD Version: 611.00   (no cuda string)  |\n",
    )
    nv = gpu_bootstrap.detect_nvidia()
    assert nv == {"driver_version": "611.00", "cuda_major": 13}  # 611 ≥ 580 → 13


# --- Apple Silicon mac -----------------------------------------------------

def test_apple_silicon_never_cuda_never_probes_smi(monkeypatch):
    """darwin MUST resolve to the 'cpu' extra (PyPI MPS/CPU wheel) and MUST NOT
    even call nvidia-smi — mac has no CUDA. A regression here would try a CUDA
    sync on a Mac (guaranteed failure)."""
    # resolve_torch_mode hard-returns mac_default BEFORE looking at nvidia.
    decision = gpu_bootstrap.resolve_torch_mode(platform_is_darwin=True, nvidia=None)
    assert decision["mode"] == "mac_default"
    assert decision["cuda_major"] is None

    # uv line only: resolve_extra on darwin must return 'cpu' and must never
    # invoke detect_nvidia (we make detect_nvidia explode to prove it).
    if uv_torch_select is not None:
        monkeypatch.setattr(uv_torch_select.sys, "platform", "darwin", raising=False)
        monkeypatch.setattr(
            uv_torch_select, "detect_nvidia",
            lambda: pytest.fail("detect_nvidia called on darwin — mac must never probe CUDA"),
        )
        extra, reason = uv_torch_select.resolve_extra()
        assert extra == "cpu", f"darwin must pick the cpu extra, got {extra}"


def test_apple_silicon_even_with_a_gpu_dict_stays_cpu():
    """Defensive: even if a (bogus) nvidia dict were passed on darwin, the
    mac-first rule must still win and return mac_default (never CUDA)."""
    decision = gpu_bootstrap.resolve_torch_mode(
        platform_is_darwin=True, nvidia={"driver_version": "999", "cuda_major": 13}
    )
    assert decision["mode"] == "mac_default"


# --- no-GPU / broken boxes -------------------------------------------------

def test_no_nvidia_smi_returns_none(monkeypatch):
    """No nvidia-smi at all (CPU-only PC, Linux without driver) → None → CPU."""
    monkeypatch.setattr(gpu_bootstrap, "_resolve_nvidia_smi", lambda: None)
    assert gpu_bootstrap.detect_nvidia() is None
    decision = gpu_bootstrap.resolve_torch_mode(platform_is_darwin=False, nvidia=None)
    assert decision["mode"] == "cpu"


def test_nvidia_smi_present_but_query_fails(monkeypatch):
    """nvidia-smi resolves but the driver query returns nothing (no GPU bound /
    driver half-installed) → None → CPU (no false positive)."""
    _fake_nvidia_smi(monkeypatch, driver_query=None, header=_new610_header("610.47", "13.3"))
    assert gpu_bootstrap.detect_nvidia() is None


def test_unsupported_cuda_major_degrades_to_cpu():
    """An NVIDIA card reporting a CUDA major with no torch wheel index
    (e.g. a hypothetical 11.x or 14.x) degrades to CPU, not a crash."""
    for major in (11, 14, 15):
        decision = gpu_bootstrap.resolve_torch_mode(
            platform_is_darwin=False,
            nvidia={"driver_version": "999.99", "cuda_major": major},
        )
        assert decision["mode"] == "cpu", f"CUDA {major} should degrade to CPU"


def test_gpu_present_unknown_cuda_returns_none_major(monkeypatch):
    """GPU visible, no CUDA field, ancient driver below the fallback floor →
    cuda_major None (kept, not lost) → resolve_torch_mode → CPU."""
    _fake_nvidia_smi(
        monkeypatch,
        driver_query="450.80\n",
        header="| NVIDIA-SMI 450.80  (very old, no cuda string) |\n",
    )
    nv = gpu_bootstrap.detect_nvidia()
    assert nv == {"driver_version": "450.80", "cuda_major": None}
    decision = gpu_bootstrap.resolve_torch_mode(platform_is_darwin=False, nvidia=nv)
    assert decision["mode"] == "cpu"
