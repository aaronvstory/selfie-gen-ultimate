"""Auto-detect NVIDIA GPU and install matching CuPy wheel on first launch.

Goal: on a CUDA host, the rPPG injector should pick up CuPy automatically
so its CPU-only fallback message ("CuPy unavailable (ModuleNotFoundError);
frame math stays on CPU") never fires. The injector already has a working
GPU path gated on ``import cupy`` succeeding — we just need to put CuPy
in the venv on the right machines.

This script is invoked from the Windows + macOS launcher chains right
before the GUI is launched. It is idempotent, cached via a JSON stamp,
and concurrency-safe via a mkdir-based lock (same pattern as the
existing ``.launcher_state/setup.lock``).

Per-platform behaviour:

* **Windows**: nvidia-smi parsing → cupy-cuda12x / cupy-cuda13x PLUS the
  explicit ``nvidia-*`` CUDA component wheels (nvrtc, cublas, ...) from PyPI
  (avoids requiring a system CUDA Toolkit). NOTE: the ``[ctk]`` extra is NOT
  used — it is a no-op on cupy 13.6.0; see ``_CUDA_TO_NVIDIA_WHEELS``.
* **macOS / Linux without nvidia-smi**: short-circuit to "no_nvidia",
  stamp the result, never retry until the stamp is wiped. CuPy has no
  Metal backend; the M1/M2 path stays CPU.

Stamp lives at ``.launcher_state/gpu_status.json``. Fields:

    {
        "checked_at": "2026-05-27T12:34:56Z",
        "result": "gpu_ready" | "no_nvidia" | "install_failed"
                  | "cached_no_nvidia",
        "driver_version": "555.52" | null,
        "cuda_major": 12 | null,
        "cupy_package": "cupy-cuda12x" | null,
        "cupy_version": "13.3.0" | null,
        "attempts": 0,           # only meaningful for install_failed
        "last_error": "..."      # only on install_failed
    }

Cache policy:

* ``gpu_ready``: cached forever. Re-probe ``import cupy`` next launch in
  case the venv was wiped; if probe succeeds → keep, else demote to
  ``install_failed`` and retry.
* ``no_nvidia``: cached for 30 days (so a user who later installs a GPU
  gets picked up). Stamp's ``checked_at`` drives the TTL.
* ``install_failed``: retry up to 3 launches with exponential
  ``attempts``, then stop until the user manually clears the stamp.

Opt-out: set ``KLING_SKIP_GPU_BOOTSTRAP=1`` in the environment. The
script exits 0 with a "GPU: skipped (KLING_SKIP_GPU_BOOTSTRAP=1)" log
line. The stamp is NOT written, so unsetting the var re-enables checks.

Concurrency: two launchers starting simultaneously must not both run
``pip install`` against the shared venv. The script acquires
``.launcher_state/gpu_bootstrap.lock`` (mkdir atomic) before any pip
work; the second launcher prints "GPU bootstrap already running;
waiting..." and blocks. Released as soon as the install finishes or
the script exits.

STDLIB-ONLY CONTRACT (v2.20): this module is imported by the uv bootstrap
chain (scripts/uv_torch_select.py -> uv_sync_deps.py) which runs with the
SYSTEM Python BEFORE `uv sync` has materialized the project env. It (and that
chain) MUST therefore stay standard-library-only — adding a third-party import
here (e.g. psutil) would break dependency provisioning on a fresh install. Keep
the GPU detection in pure stdlib (subprocess/nvidia-smi), as it already is.
"""
from __future__ import annotations

import argparse
import datetime as _dt
import json
import os
import re
import shutil
import subprocess
import sys
import time
from pathlib import Path
from typing import Optional


REPO_ROOT = Path(__file__).resolve().parent.parent
STATE_DIR = REPO_ROOT / ".launcher_state"
STAMP_PATH = STATE_DIR / "gpu_status.json"
LOCK_PATH = STATE_DIR / "gpu_bootstrap.lock"

NO_NVIDIA_TTL_DAYS = 30
INSTALL_FAILED_MAX_ATTEMPTS = 3

# Round-2 review fix: keep the stale-lock window STRICTLY LARGER than
# the pip-install subprocess timeout. If they're equal (the original
# 600 = 600), a slow but legitimate first-time CuPy install on a thin
# line could hit ~LOCK_STALE_SECONDS while pip is still working, and
# a second launcher arriving at that moment would `rmdir` the live
# lock and kick off a parallel pip install into the same venv. Margin
# of 300s gives plenty of headroom for pip's own warm-down +
# clock-skew between processes.
# 2400s (40 min): cupy + the explicit nvidia-* CUDA component wheels pull the
# full CUDA Toolkit components — for CUDA 13 that's ~2-3GB, NOT the "~500MB" an earlier
# comment claimed. A real RTX 4090 box hit the old 900s cap mid-download and
# stamped install_failed, so CuPy never landed and rPPG silently stayed on CPU
# (verified 2026-06-03). 40 min covers 2-3GB on a slow connection; the
# heartbeat keeps the console alive so it never looks frozen.
PIP_INSTALL_TIMEOUT_SECONDS = 2400
LOCK_STALE_SECONDS = PIP_INSTALL_TIMEOUT_SECONDS + 300
# probe_cupy now forces a real nvrtc kernel compile; on a fresh install the JIT
# cache is empty and the first compile (NVRTC + PTX) can take 1-2 min. Generous
# cap so a cold-cache compile isn't mistaken for a broken GPU (code-review HIGH
# PR #72). Still well under LOCK_STALE so a probe can't outlive the install lock.
PROBE_TIMEOUT_SECONDS = 180

# Map CUDA major → PyPI package name. Per CuPy 14+ install docs the
# stable wheels are cupy-cuda12x and cupy-cuda13x; older 11.x is no
# longer the current-stable target. CUDA 11.x on a current driver
# *would* downgrade us to a legacy CuPy pin which is out of scope —
# log CPU fallback instead.
#
# PIN to the CuPy 13.x line (`>=13.6,<14`): CuPy 14.x is compiled against
# numpy>=2.0 and FAILS to import under our numpy<2 face-stack pin
# (ImportError: numpy.core.multiarray failed to import). CuPy 13.6.0 is the
# last numpy-1.x-compatible release.
#
# NOTE the `[ctk]` extra is GONE (2026-06-03): it is a NO-OP on cupy 13.6.0 —
# `cupy-cuda13x` only ``Requires: fastrlock, numpy``, so `[ctk]` pulled NO
# nvidia component wheels. That is exactly why a friend's CUDA-12.9 RTX 4090
# install ran every rPPG on CPU (~20 min/iter): nvrtc was never installed, so
# the injector could not compile a kernel. The nvidia component wheels are now
# installed EXPLICITLY via _CUDA_TO_NVIDIA_WHEELS below (mirrors the uv path's
# cu121/cu128 extras). Verified: CuPy 13.6.0 + numpy 1.26.4 + TF 2.16.2 import
# together and the GPU kernel compiles. (See project_cupy_numpy2_conflict +
# project_gpu_rppg_nvrtc_ctk_noop.)
_CUDA_TO_CUPY = {
    12: "cupy-cuda12x>=13.6,<14",
    13: "cupy-cuda13x>=13.6,<14",
}

# Explicit NVIDIA CUDA component wheels per CUDA major — the REAL replacement
# for the no-op `[ctk]`. cupy's import-time kernel compile needs nvrtc + the
# math libs it dispatches to. Specs mirror pyproject.toml's cu121/cu128 extras
# VERBATIM (a parity test asserts they stay equal so the pip and uv paths can't
# drift). Component versions are NOT all the CUDA major — NVIDIA versions each
# wheel independently (cufft 12.x, curand 10.x, cusolver 12.x, cusparse 12.x
# even on CUDA 13), so DO NOT "tidy" these to a single major or pip will report
# "no matching distribution". The `-cu12` packages publish only 12.x; the
# un-suffixed packages are the CUDA-13 line.
_CUDA_TO_NVIDIA_WHEELS = {
    12: (
        "nvidia-cuda-nvrtc-cu12",
        "nvidia-cuda-runtime-cu12",
        "nvidia-cublas-cu12",
        "nvidia-cufft-cu12",
        "nvidia-curand-cu12",
        "nvidia-cusolver-cu12",
        "nvidia-cusparse-cu12",
        "nvidia-nvjitlink-cu12",
    ),
    13: (
        "nvidia-cuda-nvrtc>=13.3,<14",
        "nvidia-cuda-runtime>=13.3,<14",
        "nvidia-cublas>=13.5,<14",
        "nvidia-cufft>=12.3,<13",
        "nvidia-curand>=10.4,<11",
        "nvidia-cusolver>=12.2,<13",
        "nvidia-cusparse>=12.8,<13",
        "nvidia-nvjitlink>=13.3,<14",
    ),
}


# ---------------------------------------------------------------------------
# v2.17 torch-selection constants (see select_torch_install / resolve_torch_mode)
# ---------------------------------------------------------------------------
# Bumping INSTALLER_VERSION invalidates every launcher dep stamp (the launchers
# fold --print-stamp-token into their stamp keys), forcing a fresh resync after
# an installer-logic change even when requirements.txt/constraints.txt are
# untouched. Bump this whenever the install BEHAVIOUR changes, not the dep set.
INSTALLER_VERSION = "2.17.1"

# Map a detected CUDA major -> a PyTorch-SUPPORTED wheel index URL.
#
# IMPORTANT (review feedback 2026-06-02): do NOT infer a wheel tag from the
# CUDA major arithmetically (e.g. "12.6 -> cu126"). The wheel TAG must be one
# PyTorch actually publishes on download.pytorch.org/whl/<tag>. The nvidia-smi
# header reports the driver's MAX supported CUDA runtime (e.g. "CUDA Version:
# 12.6"), and the CUDA runtime is backward-compatible, so a PyTorch-published
# cu121 wheel runs fine on any 12.x driver. We therefore pick, per CUDA major,
# ONE conservative tag that PyTorch documents as a stable compute-platform
# option and that ships wheels for torch>=2.2 (our requirements pin):
#   * 12.x driver -> cu121  (broadest 12.x compatibility; published since 2.2)
#   * 13.x driver -> cu128  (the 13.x-era index PyTorch publishes)
# The values are VERIFIED-CURRENT PyTorch index URLs, not computed. If PyTorch
# drops/renames a tag, the safe outcome is a failed CUDA install -> automatic
# CPU fallback (select_torch_install probes torch.cuda.is_available() and falls
# back), never a broken launch. test_gpu_bootstrap asserts the tag SHAPE
# (whl/cuNNN) + parity with _CUDA_TO_CUPY so a typo'd/drifted tag fails CI.
_TORCH_CUDA_INDEX = {
    12: "https://download.pytorch.org/whl/cu121",
    13: "https://download.pytorch.org/whl/cu128",
}

# CPU-only wheel index. Mirrors dependency_health_check._TORCH_CPU_INDEX_URL --
# kept as a local constant so gpu_bootstrap.py stays importable on a partial
# tree where dependency_health_check isn't on the path.
_TORCH_CPU_INDEX_URL = "https://download.pytorch.org/whl/cpu"

# PyPI fallback so the PyTorch index (which doesn't host torch's runtime deps:
# filelock, sympy, networkx, jinja2, fsspec) can still resolve them. Same fix
# as dependency_health_check.run_torch_cpu_fallback.
_PYPI_INDEX_URL = "https://pypi.org/simple"


def _now_iso() -> str:
    return _dt.datetime.now(_dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _log(msg: str, *, quiet: bool = False) -> None:
    """Print a one-line status. ``quiet`` mode (used for cached
    ``gpu_ready`` and ``cached_no_nvidia``) suppresses the line so
    repeat-launch noise stays minimal. The interesting cases (first
    detection, install, install_failed) always print."""
    if quiet:
        return
    print(f"  GPU: {msg}", flush=True)


class _PipResult:
    """Minimal stand-in for subprocess.CompletedProcess (returncode/stdout/
    stderr) so callers that read those three attrs work unchanged.

    NOTE (code-review M1): _run_pip_with_heartbeat merges stderr INTO stdout
    (stderr=subprocess.STDOUT), so on results it produces ``stderr`` is ALWAYS
    "" and all output lives in ``stdout``. This is intentional (we want one
    combined stream for the ERROR:-line extractor). A caller must NOT rely on
    ``stderr`` being populated separately — read ``stdout`` for all output."""

    def __init__(self, returncode: int, stdout: str, stderr: str) -> None:
        self.returncode = returncode
        self.stdout = stdout
        self.stderr = stderr  # always "" from _run_pip_with_heartbeat (merged)


def _run_pip_with_heartbeat(
    cmd: list, *, timeout: int, label: str = "install"
) -> _PipResult:
    """Run a long pip command, printing an elapsed-time heartbeat every ~20s so
    the launcher console never looks frozen during a multi-GB GPU-wheel
    download (the user-reported "10 min of silence" on the CuPy/torch CUDA
    install). Captures output like subprocess.run(capture_output=True) and
    returns a _PipResult. Falls back gracefully on timeout/OS error.

    The heartbeat reads from a wall clock derived from time.monotonic() ticks
    (NOT Date.now/new Date — those are unavailable here); we only ever measure
    *elapsed* time, never absolute time, so this is resume/replay safe.
    """
    import threading

    try:
        proc = subprocess.Popen(
            cmd,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            errors="replace",
        )
    except OSError as exc:
        return _PipResult(1, "", f"pip subprocess error: {exc!r}")

    start = time.monotonic()
    stop = threading.Event()

    def _beat() -> None:
        # Print every 20s; first beat at 20s so quick installs stay quiet.
        while not stop.wait(20):
            elapsed = int(time.monotonic() - start)
            _log(f"...{label} still running ({elapsed//60}m {elapsed%60}s elapsed)")

    beater = threading.Thread(target=_beat, daemon=True)
    beater.start()
    try:
        out, _ = proc.communicate(timeout=timeout)
        rc = proc.returncode
    except subprocess.TimeoutExpired:
        proc.kill()
        try:
            out, _ = proc.communicate(timeout=10)
        except Exception:
            out = ""
        rc = 1
        out = (out or "") + f"\nERROR: {label} exceeded {timeout}s timeout"
    finally:
        stop.set()
    return _PipResult(rc, out or "", "")


def _load_stamp() -> Optional[dict]:
    # EAFP (gemini MEDIUM PR #54): skip a pre-flight STAMP_PATH.exists() — on a
    # restricted/corrupted FS exists() itself can raise OSError and crash the
    # bootstrap. Just read and catch OSError (FileNotFoundError is a subclass).
    try:
        data = json.loads(STAMP_PATH.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return None
    # gemini MEDIUM (PR #54): a corrupted or hand-edited stamp could be valid
    # JSON but a non-dict (list/str/number). Returning it would crash later
    # callers that do stamp.get(...). Treat non-dict payloads as "no stamp".
    return data if isinstance(data, dict) else None


def _write_stamp(payload: dict) -> None:
    """Persist the bootstrap result stamp. Round-2 review fix: filesystem
    failures (disk full, read-only mount, permission denied on the
    state dir) MUST degrade silently to CPU mode rather than crashing
    the bootstrap and blocking GUI launch. The launcher chain treats
    a non-zero exit from this script as fatal; an unhandled OSError
    here would block legitimate users whose `.launcher_state/` got
    chmod-restricted by an antivirus quarantine or similar."""
    try:
        STATE_DIR.mkdir(parents=True, exist_ok=True)
        payload.setdefault("checked_at", _now_iso())
        # Record the installer version that produced this stamp so a later
        # bootstrap can tell whether a capped install_failed came from an OLDER,
        # now-fixed installer and re-attempt (Codex P2 PR #72 — without this, a
        # user who exhausted the 3-attempt cap on the broken [ctk] installer
        # stays on CPU forever even after the fix ships, because bootstrap()
        # returns at the capped-stamp check before re-installing). Direct
        # assignment, NOT setdefault (gemini MEDIUM PR #72): the stamp MUST
        # reflect the installer WRITING it — a caller-supplied stale value would
        # silently break the capped-failure-reset comparison.
        payload["installer_version"] = INSTALLER_VERSION
        # Atomic write (gemini MEDIUM PR #54): on non-NVIDIA hosts the
        # stamp is written OUTSIDE the mkdir-lock, so two launchers can
        # write concurrently. A bare write_text() can interleave and
        # leave a half-written / corrupt JSON that the next _load_stamp()
        # rejects (or worse, mis-parses). Write to a PID-unique temp in
        # the SAME dir (so os.replace is a same-filesystem atomic rename,
        # not a cross-device copy) then os.replace onto the final path —
        # last-writer-wins with no torn reads. A fixed ".tmp" name would
        # just move the race onto the temp file, hence the PID suffix.
        tmp_path = STAMP_PATH.with_suffix(f".tmp.{os.getpid()}")
        try:
            tmp_path.write_text(json.dumps(payload, indent=2), encoding="utf-8")
            os.replace(tmp_path, STAMP_PATH)
        finally:
            # If os.replace succeeded the temp is already gone; this only
            # cleans up a temp orphaned by a write/replace failure.
            try:
                tmp_path.unlink()
            except OSError:
                pass
    except OSError as exc:
        # The next launch will re-detect + re-attempt; no stamp = no
        # cache, which is the correct degraded behaviour.
        _log(f"could not persist stamp ({type(exc).__name__}: {exc}); "
             "next launch will re-check")


def _stamp_age_days(stamp: dict) -> float:
    raw = stamp.get("checked_at")
    if not raw:
        return float("inf")
    try:
        when = _dt.datetime.strptime(raw, "%Y-%m-%dT%H:%M:%SZ").replace(
            tzinfo=_dt.timezone.utc
        )
    except (TypeError, ValueError):
        return float("inf")
    return (_dt.datetime.now(_dt.timezone.utc) - when).total_seconds() / 86400.0


def _is_windows() -> bool:
    return os.name == "nt"


def _resolve_nvidia_smi() -> Optional[str]:
    """Locate the ``nvidia-smi`` executable, or None if absent.

    Code-review HIGH (PR #54): a bare ``["nvidia-smi"]`` only works when
    the binary is on PATH. The NVIDIA Windows driver installer does NOT
    reliably add it, so on a perfectly good CUDA box the bare invocation
    FileNotFounds and the whole GPU path silently no-ops. Fall back to the
    canonical Windows install locations before giving up: the modern driver
    drops ``nvidia-smi.exe`` in ``System32``; the legacy layout puts it under
    ``NVIDIA Corporation\\NVSMI``. On POSIX, PATH lookup is authoritative.
    """
    found = shutil.which("nvidia-smi")
    if found:
        return found
    if not _is_windows():
        return None
    candidates = []
    sysroot = os.environ.get("SystemRoot") or r"C:\Windows"
    candidates.append(Path(sysroot) / "System32" / "nvidia-smi.exe")
    for env in ("ProgramFiles", "ProgramW6432", "ProgramFiles(x86)"):
        base = os.environ.get(env)
        if base:
            candidates.append(
                Path(base) / "NVIDIA Corporation" / "NVSMI" / "nvidia-smi.exe"
            )
    for cand in candidates:
        try:
            if cand.is_file():
                return str(cand)
        except OSError:
            continue
    return None


def _smi_query_driver_and_name(exe: str) -> Optional[tuple]:
    """``(driver_version, gpu_name)`` via the STABLE ``--query-gpu`` interface.

    ``nvidia-smi --query-gpu=driver_version,name --format=csv,noheader`` has been
    stable since ~2016 and is INDEPENDENT of the free-form header layout. We use
    it for GPU-presence + driver + NAME detection so a header redesign (see
    ``_parse_smi_header_cuda_major``) can never make a real GPU vanish. The name
    (e.g. "NVIDIA GeForce RTX 4090 Laptop GPU") feeds the GUI's "✅ GPU detected"
    banner. Returns ``(driver, name)`` on success — ``name`` may be ``None`` if
    the field is blank — or ``None`` when no usable GPU/driver is found.
    """
    try:
        proc = subprocess.run(
            [exe, "--query-gpu=driver_version,name", "--format=csv,noheader"],
            capture_output=True,
            text=True,
            errors="replace",
            timeout=10,
        )
    except (FileNotFoundError, subprocess.TimeoutExpired, OSError):
        return None
    if proc.returncode != 0:
        return None
    for line in (proc.stdout or "").splitlines():
        # CSV row: "610.47, NVIDIA GeForce RTX 4090 Laptop GPU"
        parts = [p.strip() for p in line.split(",", 1)]
        ver = parts[0] if parts else ""
        if ver and re.match(r"^[0-9][0-9.]*$", ver):
            name = parts[1] if len(parts) > 1 and parts[1] else None
            return (ver, name)
    return None


def _parse_smi_header_cuda_major(header: str) -> Optional[int]:
    """CUDA major from the free-form ``nvidia-smi`` header (no-flag output).

    Accepts BOTH header layouts NVIDIA has shipped:

    * Legacy (driver ≤ ~570): ``... CUDA Version: 12.6``
    * New (driver 610+, 2026): ``... CUDA UMD Version: 13.3`` — the older
      ``Driver Version:`` / ``CUDA Version:`` strings were DROPPED entirely in
      this redesign, which silently knocked detect_nvidia down to CPU on a
      perfectly good RTX 4090 box (verified 2026-06-04, driver 610.47).

    We match the NEW ``CUDA UMD Version:`` first as the authoritative runtime
    field on the new layout. (The two regexes are independent — the ``CUDA
    Version:`` pattern does NOT match inside ``CUDA UMD Version:`` because the
    literal ``UMD `` between ``CUDA `` and ``Version:`` breaks it — so order is
    about *preference* when a future header somehow carries both, not about
    avoiding a false match.) Returns the major int, or None if neither field is
    present.
    """
    m_umd = re.search(r"CUDA UMD Version:\s*([0-9]+)\.([0-9]+)", header)
    if m_umd:
        return int(m_umd.group(1))
    m_cuda = re.search(r"CUDA Version:\s*([0-9]+)\.([0-9]+)", header)
    if m_cuda:
        return int(m_cuda.group(1))
    return None


# Driver-branch → CUDA-runtime major, used ONLY as a last-resort fallback when
# BOTH header CUDA fields are absent but a GPU is confirmed present. The driver
# floors below are conservative lower bounds for each CUDA major's earliest
# Windows driver branch (NVIDIA's driver→CUDA table). A GPU we can SEE must
# never drop to CPU just because we couldn't read the CUDA string — picking a
# CUDA extra that's slightly off still degrades to CPU via the torch.cuda probe,
# whereas guessing CPU here strands a real GPU silently (the bug we're fixing).
_DRIVER_BRANCH_TO_CUDA_MAJOR = (
    (580, 13),  # 580+ driver branch ships the CUDA 13.x runtime
    (525, 12),  # 525+ driver branch ships the CUDA 12.x runtime
)


def _cuda_major_from_driver(driver_version: str) -> Optional[int]:
    """Best-effort CUDA major from a driver version string (fallback only)."""
    try:
        branch = int(driver_version.split(".", 1)[0])
    except (ValueError, AttributeError):
        return None
    for floor, major in _DRIVER_BRANCH_TO_CUDA_MAJOR:
        if branch >= floor:
            return major
    return None


def detect_nvidia() -> Optional[dict]:
    """Return {'driver_version', 'cuda_major'} for the local NVIDIA GPU,
    or None when there's no NVIDIA GPU / driver.

    GPU presence + driver version come from the STABLE
    ``--query-gpu=driver_version`` interface (layout-independent). The CUDA
    major is parsed from the free-form header, accepting BOTH the legacy
    ``CUDA Version:`` and the new (driver 610+) ``CUDA UMD Version:`` fields;
    if neither is present we fall back to a driver-branch→CUDA-major table so
    a visible GPU is never silently dropped to CPU.

    Robust to: missing executable (Linux/macOS without NVIDIA, or a Windows
    box where nvidia-smi isn't on PATH), non-zero exit, header redesigns.
    All hard failures → None.
    """
    exe = _resolve_nvidia_smi()
    if exe is None:
        return None

    # 1) Stable presence + driver + name probe. If this fails, no usable GPU.
    driver_and_name = _smi_query_driver_and_name(exe)
    if driver_and_name is None:
        return None
    driver_version, gpu_name = driver_and_name

    # 2) CUDA major from the header (both layouts), then driver-branch fallback.
    # Shorter timeout than the presence probe (code-review MEDIUM #4): presence
    # is already confirmed, so this second call only needs to read the header.
    # A 5s cap keeps the worst-case bootstrap latency from doubling to ~20s if
    # nvidia-smi is sluggish right after a driver update.
    try:
        proc = subprocess.run(
            [exe],
            capture_output=True,
            text=True,
            errors="replace",
            timeout=5,
        )
        header = proc.stdout or "" if proc.returncode == 0 else ""
    except (FileNotFoundError, subprocess.TimeoutExpired, OSError):
        header = ""

    cuda_major = _parse_smi_header_cuda_major(header)
    if cuda_major is None:
        cuda_major = _cuda_major_from_driver(driver_version)
    if cuda_major is None:
        # GPU present but we genuinely can't tell the CUDA major. resolve_torch_mode
        # treats an unmapped/None major as CPU, which is the safe outcome.
        return {
            "driver_version": driver_version,
            "cuda_major": None,
            "gpu_name": gpu_name,
        }

    return {
        "driver_version": driver_version,
        "cuda_major": cuda_major,
        "gpu_name": gpu_name,
    }


def probe_cupy(python_exe: str) -> Optional[str]:
    """Run a real CuPy operation in the target venv. Return the
    detected CuPy version on success; None on any failure.

    Per the steering note: stamp ``gpu_ready`` only when a real GPU op
    succeeds, not just on import. A broken/mismatched-CUDA CuPy install
    can import cleanly and only blow up on first device access.
    """
    # Code-review MEDIUM (PR #54): emit a sentinel-prefixed version line and
    # parse THAT, not the last stdout line. CuPy's first device init can print
    # JIT-cache / deprecation notices to stdout, and any library in the import
    # chain may print after us — taking [-1] blindly would stamp a garbage
    # ``cupy_version`` like "UserWarning: ...". The sentinel makes the parse
    # robust to trailing noise.
    #
    # The probe MUST do two things the old probe didn't (2026-06-03):
    #   1. Register the NVIDIA DLL dirs via the SHARED helper
    #      (scripts/cuda_dll_paths.py) BEFORE ``import cupy`` — otherwise the
    #      kernel compile below fails with "Could not find nvrtc64_*.dll" on a
    #      correctly-installed box (Windows ignores PATH for an ext module's
    #      dependent DLLs), giving a FALSE NEGATIVE → install_failed → the user
    #      is stranded on CPU. Same registration the rPPG injector uses.
    #   2. Force a real nvrtc KERNEL COMPILE (gaussian_filter), not just
    #      asarray/getDeviceCount. The old probe never touched nvrtc, so it
    #      reported "GPU ready" on a box where rPPG's kernels could never
    #      compile — a false POSITIVE the GUI showed the user (the friend's
    #      "CuPy ready" line while every rPPG silently ran on CPU). Compiling
    #      here makes the GUI status match the injector's reality.
    # The helper dir is this file's own directory (scripts/); inject it so the
    # fresh probe subprocess can import it regardless of cwd. Written as a real
    # multi-line program (newline-joined) — a ``;``-joined one-liner can't carry
    # a compound ``try:`` block (SyntaxError), which would itself false-negative.
    # realpath (not abspath) so a symlinked gpu_bootstrap.py still resolves the
    # real scripts/ dir for the probe subprocess's helper import (gemini PR #72).
    _scripts_dir = os.path.dirname(os.path.realpath(__file__))
    # The probe registers the nvidia DLL dirs, then forces a REAL nvrtc kernel
    # compile. On the FIRST compile failure it wipes a possibly-stale on-disk JIT
    # cache (left by a prior CUDA toolkit/driver — the friend's 12.9 -> 13.x
    # upgrade) and retries ONCE, mirroring the rPPG injector's recovery so the
    # GUI "GPU ready" status matches the injector's reality AND a stale cache
    # doesn't false-negative into a needless reinstall. Only a compile that
    # STILL fails after the cache wipe exits non-zero -> probe None (correct).
    probe_src = "\n".join(
        (
            "import sys",
            "sys.path.insert(0, " + repr(_scripts_dir) + ")",
            "_clear = None",
            # Import the LOAD-BEARING register fn on its OWN: a stale
            # cuda_dll_paths.py lacking clear_cupy_kernel_cache must NOT also
            # drop register (a combined import fails on the first missing name),
            # else the probe false-negatives on a good box (code-review).
            "try:",
            "    from cuda_dll_paths import register_cuda_dll_dirs",
            "    register_cuda_dll_dirs()",
            "except Exception:",
            "    pass",
            "try:",
            "    from cuda_dll_paths import clear_cupy_kernel_cache as _clear",
            "except Exception:",
            "    pass",
            "import cupy as cp",
            "from cupyx.scipy.ndimage import gaussian_filter as _g",
            "x = cp.asarray([1, 2, 3])",
            "_ = cp.asnumpy(x)",
            "_ = cp.cuda.runtime.getDeviceCount()",
            "def _compile():",
            "    return _g(cp.zeros((4, 4), dtype=cp.float32), 1.0)",
            "try:",
            "    _compile()",
            "except Exception:",
            "    try:",
            "        if _clear is not None:",
            "            _clear()",
            "    except Exception:",
            "        pass",
            "    _compile()",
            "print('CUPYVER=' + cp.__version__)",
        )
    )
    try:
        proc = subprocess.run(
            [python_exe, "-c", probe_src],
            capture_output=True,
            text=True,
            errors="replace",
            # Generous timeout (was 30s): the probe now does a REAL nvrtc kernel
            # COMPILE, and on a fresh install the CuPy JIT cache is EMPTY, so the
            # first compile (NVRTC + PTX assembly) can take 1-2 min. The friend's
            # box is exactly this case (just-installed nvidia wheels). A 30s cap
            # would TimeoutExpired -> probe None -> install_failed -> stranded on
            # CPU again, defeating the whole fix (code-review HIGH #2/#4 PR #72).
            timeout=PROBE_TIMEOUT_SECONDS,
        )
    except (FileNotFoundError, subprocess.TimeoutExpired, OSError):
        return None
    if proc.returncode != 0:
        return None
    for line in reversed((proc.stdout or "").splitlines()):
        line = line.strip()
        if line.startswith("CUPYVER="):
            return line[len("CUPYVER="):] or None
    return None


def _resolve_constraints_path() -> Optional[str]:
    """Locate the repo-root ``constraints.txt`` relative to this file.

    Threaded into the CuPy/nvidia install so a transitive resolve can't pull
    numpy 2.x (the numpy<2 face-stack invariant). Returns None if absent (the
    install still runs unconstrained — better than failing to provision GPU).
    """
    # EAFP (gemini MEDIUM PR #72): attempt to open the file directly inside the
    # try rather than a pre-flight is_file() check — resolve()/open() can raise
    # on a restricted FS / symlink loop, and any OSError must degrade to "absent"
    # (unconstrained install) rather than crashing the GPU bootstrap. The context
    # manager closes the probe handle immediately.
    try:
        candidate = Path(__file__).resolve().parent.parent / "constraints.txt"
        with open(candidate):
            return str(candidate)
    except OSError:
        return None


def install_cupy(python_exe: str, cuda_major: int) -> tuple[bool, str]:
    """``pip install`` CuPy + its NVIDIA component wheels for the CUDA major.

    Returns ``(success, message)``. ``message`` is either the CuPy
    version on success or the pip stderr tail on failure.

    Installs cupy AND the explicit nvidia-* component wheels (nvrtc, cublas,
    ...) in ONE pip call: one resolver pass picks mutually-compatible versions,
    one heartbeat subprocess, and an atomic success/fail so a partial install
    can never be stamped ``gpu_ready``. The nvidia wheels are the REAL
    replacement for the no-op ``[ctk]`` extra — without nvrtc, cupy imports but
    can't compile a kernel and rPPG silently runs on CPU (the friend's bug).

    Uses ``--only-binary :all:`` so we never try to compile from sdist
    (slow, requires NVCC). ``--no-input`` so an unexpected prompt never
    blocks the launcher chain. ``-c constraints.txt`` keeps numpy<2.
    """
    pkg = _CUDA_TO_CUPY.get(cuda_major)
    nvidia_pkgs = _CUDA_TO_NVIDIA_WHEELS.get(cuda_major)
    if pkg is None or nvidia_pkgs is None:
        return (False, f"unsupported CUDA major {cuda_major}; need 12 or 13")
    cmd = [
        python_exe, "-m", "pip", "install",
        "--only-binary", ":all:",
        "--no-input",
    ]
    constraints_path = _resolve_constraints_path()
    if constraints_path:
        cmd += ["-c", constraints_path]
    else:
        # numpy<2 is a NON-NEGOTIABLE project invariant (cupy 14.x / numpy 2.x
        # break the TF 2.16.2 face stack). If constraints.txt went missing
        # (e.g. a mis-packaged zip), make that VISIBLE rather than silently
        # resolving an unconstrained install (code-review MEDIUM PR #72). The
        # cupy `<14` pin in the spec is still the primary guard.
        _log("WARNING: constraints.txt not found — installing CuPy without the "
             "numpy<2 constraint file (cupy <14 pin still applies)")
    cmd.append(pkg)
    cmd.extend(nvidia_pkgs)
    # Heartbeat-wrapped: the CuPy + nvidia component wheels are ~1.5-2.5GB; a
    # plain blocking subprocess.run prints nothing until done, which looked
    # frozen to users (reported "10 min of silence"). _run_pip_with_heartbeat
    # prints an elapsed-time line every 20s.
    proc = _run_pip_with_heartbeat(
        cmd, timeout=PIP_INSTALL_TIMEOUT_SECONDS, label="CuPy + CUDA components install"
    )
    if proc.returncode != 0:
        # Code-review MEDIUM (PR #54): surface pip's own ``ERROR:`` lines
        # rather than a blind last-400-chars tail. pip prints the actual
        # cause (no matching wheel, resolver conflict, hash mismatch) on
        # ``ERROR:``-prefixed lines, but follows them with a generic hint
        # block — a raw tail often captures only the hint and drops the
        # diagnosis. Prefer the ERROR lines; fall back to the tail.
        combined = f"{proc.stdout or ''}\n{proc.stderr or ''}"
        err_lines = [
            ln.strip()
            for ln in combined.splitlines()
            if ln.strip().upper().startswith("ERROR:")
        ]
        if err_lines:
            return (False, " | ".join(err_lines)[-400:])
        tail = (proc.stderr or proc.stdout or "")
        return (False, tail[-400:].strip() or f"pip exit {proc.returncode}")
    version = probe_cupy(python_exe)
    if version is None:
        return (False, "installed but probe failed (cupy import or GPU op crashed)")
    return (True, version)


# ---------------------------------------------------------------------------
# v2.17 torch hardware selection
# ---------------------------------------------------------------------------
def resolve_torch_mode(
    *, platform_is_darwin: bool, nvidia: Optional[dict]
) -> dict:
    """Decide which torch wheel variant to install. PURE: no I/O, no subprocess.

    Returns a dict::

        {
            "mode": "mac_default" | "cuda" | "cpu",
            "index_url": str | None,        # None for mac_default
            "extra_index_url": str | None,  # PyPI fallback for cuda/cpu
            "cuda_major": int | None,
            "reason": str,
        }

    Rules (user mandate 2026-06-02 — confidence: mac-never-CUDA 10/10,
    Windows NVIDIA-vs-CPU 9/10):

      * **macOS** -> ``mac_default``. Hard-returns BEFORE looking at ``nvidia``
        so a Mac can NEVER select a CUDA wheel (no CUDA on Apple Silicon /
        Metal; the default PyPI wheel is the MPS/CPU build). This is the
        highest-confidence rule and must stay first.
      * **NVIDIA present + CUDA major has a wheel index** -> ``cuda``.
      * **else** (no NVIDIA, broken nvidia-smi, or an unsupported CUDA major
        like 10/11/14+) -> ``cpu``.

    Being pure makes the whole decision table unit-testable without a GPU
    (mirrors the existing detect_nvidia-monkeypatch style in
    tests/test_gpu_bootstrap.py).
    """
    if platform_is_darwin:
        return {
            "mode": "mac_default",
            "index_url": None,
            "extra_index_url": None,
            "cuda_major": None,
            "reason": "macOS: default PyPI wheel (MPS/CPU); never CUDA",
        }

    if nvidia is not None:
        cuda_major = nvidia.get("cuda_major")
        index_url = _TORCH_CUDA_INDEX.get(cuda_major)
        if index_url is not None:
            return {
                "mode": "cuda",
                "index_url": index_url,
                "extra_index_url": _PYPI_INDEX_URL,
                "cuda_major": cuda_major,
                "reason": (
                    f"NVIDIA driver {nvidia.get('driver_version')} / "
                    f"CUDA {cuda_major}.x -> {index_url}"
                ),
            }
        # NVIDIA present but CUDA major unsupported by our wheel map.
        return {
            "mode": "cpu",
            "index_url": _TORCH_CPU_INDEX_URL,
            "extra_index_url": _PYPI_INDEX_URL,
            "cuda_major": cuda_major,
            "reason": (
                f"NVIDIA present but CUDA {cuda_major}.x has no torch wheel "
                "index in our map (need 12.x or 13.x); CPU torch"
            ),
        }

    return {
        "mode": "cpu",
        "index_url": _TORCH_CPU_INDEX_URL,
        "extra_index_url": _PYPI_INDEX_URL,
        "cuda_major": None,
        "reason": "no NVIDIA detected; CPU torch",
    }


def _probe_torch_cuda(python_exe: str) -> tuple[bool, bool, "str | None"]:
    """Probe the installed torch in the target venv.

    Returns ``(import_ok, cuda_available, build_cuda_version)``:
      * ``import_ok``      — ``import torch`` succeeded.
      * ``cuda_available`` — ``torch.cuda.is_available()`` (canonical eager
        CUDA-runtime init; lazily loads the CUDA DLLs, surfacing a broken
        cudart). False on CPU-only builds and on broken-CUDA builds alike.
      * ``build_cuda_version`` — ``torch.version.cuda`` (e.g. ``"12.1"`` for a
        CUDA build, ``None`` for a CPU build). Lets the caller tell "already
        the right build, skip the reinstall" from "wrong build, reinstall".

    All three are conservative on any subprocess failure: ``(False, False,
    None)``. Mirrors the probe rationale in
    dependency_health_check.check_runtime_dependencies.
    """
    probe_src = (
        "import torch;"
        "print('TORCHCUDA=' + str(bool(torch.cuda.is_available())));"
        "print('TORCHBUILD=' + str(getattr(torch.version, 'cuda', None)));"
        "print('TORCHOK=1')"
    )
    try:
        proc = subprocess.run(
            [python_exe, "-c", probe_src],
            capture_output=True,
            text=True,
            errors="replace",
            timeout=60,
        )
    except (FileNotFoundError, subprocess.TimeoutExpired, OSError):
        return (False, False, None)
    if proc.returncode != 0:
        return (False, False, None)
    out = proc.stdout or ""
    import_ok = "TORCHOK=1" in out
    cuda_available = "TORCHCUDA=True" in out
    build_cuda_version = None
    for line in out.splitlines():
        line = line.strip()
        if line.startswith("TORCHBUILD="):
            val = line[len("TORCHBUILD="):]
            build_cuda_version = None if val in ("None", "") else val
    return (import_ok, cuda_available, build_cuda_version)


def _log_macos_mps(python_exe: str) -> None:
    """Log whether Apple Metal (MPS) acceleration is available, separately from
    install success. MPS-unavailable is NOT a failure — it just means torch
    runs on CPU on this Mac. Best-effort: any probe error is swallowed.
    """
    probe_src = (
        "import torch;"
        "b = getattr(torch.backends, 'mps', None);"
        "print('MPS=' + str(bool(b and b.is_available())))"
    )
    try:
        proc = subprocess.run(
            [python_exe, "-c", probe_src],
            capture_output=True, text=True, errors="replace", timeout=60,
        )
    except (FileNotFoundError, subprocess.TimeoutExpired, OSError):
        return
    out = proc.stdout or ""
    if "MPS=True" in out:
        _log("torch: Apple MPS acceleration available")
    elif "MPS=False" in out:
        _log("torch: MPS unavailable -- CPU torch (not a failure)")


def select_torch_install(
    python_exe: str,
    torch_spec: str,
    *,
    constraints_path: Optional[str] = None,
) -> tuple[bool, str]:
    """Install the hardware-appropriate torch wheel for this machine.

    Fully AUTOMATIC — no prompts, no user choice. Detection drives everything:
    NVIDIA found -> CUDA torch; macOS -> default (MPS/CPU); else -> CPU torch.

    IDEMPOTENT (user mandate 2026-06-02 "no torch selection unless absolutely
    needed"): probes the ALREADY-INSTALLED torch first and skips the reinstall
    when its build already matches the resolved mode. The plain
    ``-r requirements.txt`` install lands the default PyPI wheel, which IS the
    right build on a CPU box and on macOS — so on those machines this is a
    cheap no-op probe, NOT a redundant multi-GB re-download. A reinstall fires
    only when the installed build is wrong for the hardware (CPU wheel on an
    NVIDIA box -> install CUDA; or torch missing/broken).

    Returns ``(ok, message)`` — same shape as ``install_cupy`` / ``run_repair``.
    Always best-effort: torch is only used by DeepFace's anti-spoofing
    classifier and CUDA only affects speed, not correctness (production never
    calls ``torch.cuda.*``), so a failure returns ``(False, msg)`` and the
    launcher proceeds.
    """
    nvidia = detect_nvidia()
    decision = resolve_torch_mode(
        platform_is_darwin=(sys.platform == "darwin"), nvidia=nvidia
    )
    mode = decision["mode"]
    _log(f"torch: {decision['reason']}")

    import_ok, cuda_available, build_cuda = _probe_torch_cuda(python_exe)

    if mode == "mac_default":
        if import_ok:
            _log_macos_mps(python_exe)
            return (True, "torch already present (macOS default wheel)")
        # torch missing/broken — let pip install the default wheel.
        # Build the command append-style (constraints before the spec),
        # consistent with _install_torch_from_index — code-review C2 flagged
        # the prior cmd[4:4] splice as fragile (silently wrong if the literal
        # is ever reordered).
        cmd = [python_exe, "-m", "pip", "install", "--no-input"]
        if constraints_path:
            cmd += ["-c", constraints_path]
        cmd.append(torch_spec)
        ok, msg = _run_torch_pip(cmd, "macOS default")
        if ok:
            _log_macos_mps(python_exe)
        return (ok, msg)

    if mode == "cpu":
        # The default PyPI wheel IS a CPU build (build_cuda is None). If torch
        # already imports as a CPU build, we're done — no reinstall needed.
        if import_ok and build_cuda is None:
            return (True, "torch already CPU build (no reinstall needed)")
        # Wrong build (a CUDA wheel on a now-CPU box) or torch missing/broken.
        return _install_torch_from_index(
            python_exe, torch_spec, decision, constraints_path
        )

    # mode == "cuda"
    if import_ok and cuda_available and build_cuda is not None:
        # Already a working CUDA build — nothing to do.
        return (True, f"torch already CUDA build {build_cuda} (no reinstall needed)")
    # CPU wheel on an NVIDIA box, or a broken CUDA build -> install CUDA torch.
    ok, msg = _install_torch_from_index(
        python_exe, torch_spec, decision, constraints_path
    )
    if not ok:
        return (ok, msg)
    # Verify CUDA actually works; fall back to CPU torch if the runtime is
    # broken (missing DLLs / driver mismatch / AV quarantine).
    import_ok, cuda_available, _ = _probe_torch_cuda(python_exe)
    if not import_ok:
        return (False, "CUDA torch installed but import failed at probe")
    if not cuda_available:
        _log(
            "CUDA torch installed but torch.cuda.is_available() is False "
            "(broken DLLs / driver mismatch); falling back to CPU torch"
        )
        return _fallback_cpu_torch(python_exe, torch_spec, constraints_path)
    return (True, "torch cuda install completed")


def _install_torch_from_index(
    python_exe: str, torch_spec: str, decision: dict, constraints_path: Optional[str]
) -> tuple[bool, str]:
    """Force-reinstall torch from the resolved cuda/cpu wheel index.

    TWO-STEP to guarantee the chosen index wins (code-review Codex P2): a single
    ``pip install --index-url <cuda> --extra-index-url pypi torch>=2.2,<3`` lets
    pip consider BOTH indexes and pick the best *version* — so a newer plain
    PyPI torch can BEAT the cu121 wheel, silently installing CPU torch on an
    NVIDIA box and defeating GPU selection. Instead:

      1. Install torch ALONE from the wheel index with ``--no-deps`` and NO
         extra-index, so ONLY that index is consulted and its torch wheel
         (cu121/cu128/cpu) is the one that lands.
      2. Run pip again for the SAME torch_spec WITH the PyPI extra-index but
         WITHOUT --force-reinstall/--no-deps: torch is already satisfied by
         step 1, so pip leaves the CUDA wheel in place and only resolves +
         installs torch's MISSING dependencies from PyPI. This pulls torch's
         OWN declared deps (no hardcoded list to drift — code-review 2026-06-03)
         while the wheel index can't beat the already-installed CUDA build.

    -c constraints on both passes so a transitive resolve can't pull numpy 2.x.
    """
    constraint = ["-c", constraints_path] if constraints_path else []

    # Step 1: torch ONLY, from the resolved index, no competing PyPI index.
    step1 = [
        python_exe, "-m", "pip", "install",
        "--upgrade", "--force-reinstall", "--no-cache-dir", "--no-input",
        "--no-deps",
        "--index-url", decision["index_url"],
        *constraint,
        torch_spec,
    ]
    ok, msg = _run_torch_pip(step1, f"{decision['mode']} (wheel)")
    if not ok:
        return (ok, msg)

    # Step 2: resolve torch's OWN deps from PyPI. torch is already installed
    # (step 1), so without --force-reinstall pip keeps the CUDA wheel and just
    # fills in the missing deps it declares — no hardcoded dep list. The wheel
    # index is listed first so any torch-namespaced runtime libs still prefer
    # it; PyPI (extra-index) supplies filelock/sympy/networkx/etc.
    step2 = [
        python_exe, "-m", "pip", "install",
        "--no-cache-dir", "--no-input",
        "--index-url", decision["index_url"],
        "--extra-index-url", decision["extra_index_url"],
        *constraint,
        torch_spec,
    ]
    ok2, msg2 = _run_torch_pip(step2, f"{decision['mode']} (runtime deps)")
    if not ok2:
        # torch wheel is in place; deps resolve failed. Surface it — the
        # caller's probe reveals whether torch is actually importable.
        return (False, f"torch wheel ok but runtime-deps install failed: {msg2}")
    return (True, f"torch {decision['mode']} install completed")


def _run_torch_pip(cmd: list, label: str) -> tuple[bool, str]:
    """Run a torch pip install command; surface ERROR: lines on failure.

    Same ERROR-line-preferring failure-detail extraction as ``install_cupy``.
    """
    # Heartbeat-wrapped: CUDA torch wheels are ~2GB; print elapsed every 20s
    # so a long download never looks frozen (the user-reported silence).
    proc = _run_pip_with_heartbeat(
        cmd, timeout=PIP_INSTALL_TIMEOUT_SECONDS, label=f"torch {label} install"
    )
    if proc.returncode != 0:
        combined = f"{proc.stdout or ''}\n{proc.stderr or ''}"
        err_lines = [
            ln.strip()
            for ln in combined.splitlines()
            if ln.strip().upper().startswith("ERROR:")
        ]
        if err_lines:
            return (False, " | ".join(err_lines)[-400:])
        tail = (proc.stderr or proc.stdout or "")
        return (False, tail[-400:].strip() or f"pip exit {proc.returncode}")
    return (True, f"torch {label} install completed")


def _fallback_cpu_torch(
    python_exe: str, torch_spec: str, constraints_path: Optional[str]
) -> tuple[bool, str]:
    """Reinstall torch from the CPU index after a broken-CUDA probe.

    Prefers dependency_health_check.run_torch_cpu_fallback (bot-reviewed,
    pins to the installed public version), but that helper installs into the
    CURRENT interpreter via ``sys.executable``. When the launcher invokes
    gpu_bootstrap with the venv python as ``python_exe`` (the normal case)
    those coincide; if they differ we install directly so the CPU torch lands
    in the right venv.
    """
    if python_exe == sys.executable:
        try:
            from dependency_health_check import run_torch_cpu_fallback
            return run_torch_cpu_fallback()
        except Exception:
            pass  # fall through to the inline install
    cmd = [
        python_exe, "-m", "pip", "install",
        "--upgrade", "--force-reinstall", "--no-cache-dir", "--no-input",
        "--index-url", _TORCH_CPU_INDEX_URL,
        "--extra-index-url", _PYPI_INDEX_URL,
    ]
    if constraints_path:
        cmd += ["-c", constraints_path]
    cmd.append(torch_spec)
    return _run_torch_pip(cmd, "cpu-fallback")


def compute_stamp_token(constraints_path: Optional[str] = None) -> str:
    """Return a deterministic token for the launchers to fold into their dep
    stamp keys, so a GPU-mode change or installer bump invalidates the stamp.

    Combines: INSTALLER_VERSION, platform tag, resolved torch mode, cuda_major,
    and a short sha of constraints.txt (if locatable).

    GPU mode is read from the CACHED ``gpu_status.json`` stamp (written by the
    CuPy bootstrap) when present, so this stays a cheap file read on the hot
    per-launch path — it does NOT run nvidia-smi every launch. Only when no
    cache exists yet (first launch) do we fall back to a live detect_nvidia()
    (bounded to a 10s timeout). When a user later installs/removes a GPU, the
    CuPy bootstrap refreshes gpu_status.json, which flips this token, which
    invalidates the dep stamp, which re-runs the full sync + select-torch.

    KNOWN one-launch lag (code-review H3, accepted): on the FIRST launch after
    a user PHYSICALLY removes the GPU, this reads the stale cached cuda_major
    (still 12/13) before the CuPy bootstrap refreshes the stamp, so the token
    — and thus the dep stamp — does NOT change that launch, and select-torch
    isn't re-run to downgrade CUDA torch -> CPU torch. This degrades silently
    (CUDA init just fails and torch uses CPU; production never calls
    torch.cuda.*), and the NEXT launch (stamp refreshed) corrects it. We accept
    the one-launch lag rather than pay an nvidia-smi probe on every cached
    launch. GPU-ADD is fine immediately: no cache claims a GPU, so the first
    branch's `cuda_major` stays None until the bootstrap installs + stamps.
    """
    import hashlib

    cuda_major = None
    stamp = _load_stamp()
    if stamp is not None and "cuda_major" in stamp:
        # Cached: trust the GPU bootstrap's recorded view (cheap).
        cuda_major = stamp.get("cuda_major")
    else:
        # First launch / no cache: one live probe.
        nvidia = detect_nvidia()
        cuda_major = nvidia.get("cuda_major") if nvidia else None

    decision = resolve_torch_mode(
        platform_is_darwin=(sys.platform == "darwin"),
        nvidia=({"cuda_major": cuda_major, "driver_version": None}
                if cuda_major is not None else None),
    )

    constraints_sha = "none"
    path = constraints_path or (REPO_ROOT / "constraints.txt")
    try:
        data = Path(path).read_bytes()
        constraints_sha = hashlib.sha256(data).hexdigest()[:12]
    except OSError:
        pass
    return "-".join([
        INSTALLER_VERSION,
        sys.platform,
        str(decision["mode"]),
        str(decision["cuda_major"]),
        constraints_sha,
    ])


def _acquire_lock(quiet: bool = False) -> bool:
    """mkdir-based atomic lock. Returns True when acquired. False
    only when a sibling launcher's lock is still active AND we time
    out waiting for it (shouldn't happen in practice; we wait up to
    LOCK_STALE_SECONDS).

    Round-2 review fix: filesystem failures creating the state dir or
    the lock dir (permission denied, read-only mount) MUST degrade
    to "lock not acquired" rather than crashing. The caller treats
    `False` as "fall back to CPU this launch" — same outcome as a
    sibling holding the lock too long, which is the documented
    contract."""
    try:
        STATE_DIR.mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        _log(f"could not create state dir ({type(exc).__name__}: {exc}); "
             "falling back to CPU this launch")
        return False
    waited = 0
    waited_logged = False
    while True:
        try:
            LOCK_PATH.mkdir(parents=False, exist_ok=False)
            return True
        except FileExistsError:
            pass
        except OSError as exc:
            # Other filesystem failure (permission denied, parent dir
            # vanished mid-flight, etc.) — treat as un-acquirable.
            _log(f"could not create lock dir ({type(exc).__name__}: {exc}); "
                 "falling back to CPU this launch")
            return False
        # Check stale
        try:
            age = time.time() - LOCK_PATH.stat().st_mtime
        except OSError:
            age = 0
        if age > LOCK_STALE_SECONDS:
            _log(f"removing stale gpu_bootstrap.lock (age={int(age)}s)", quiet=quiet)
            try:
                LOCK_PATH.rmdir()
            except OSError:
                pass
            continue
        if not waited_logged:
            _log("bootstrap already running in another launcher; waiting...", quiet=quiet)
            waited_logged = True
        time.sleep(2)
        waited += 2
        if waited > LOCK_STALE_SECONDS:
            return False


def _release_lock() -> None:
    try:
        LOCK_PATH.rmdir()
    except OSError:
        pass


def bootstrap(python_exe: str, *, quiet_if_cached: bool = False) -> str:
    """Top-level entry. Returns the final result string."""
    # Hard opt-out — neither writes nor reads the stamp.
    if os.environ.get("KLING_SKIP_GPU_BOOTSTRAP") == "1":
        _log("skipped (KLING_SKIP_GPU_BOOTSTRAP=1)")
        return "skipped"

    stamp = _load_stamp()

    # Cached gpu_ready: verify probe still works (venv could be wiped).
    if stamp and stamp.get("result") == "gpu_ready":
        version = probe_cupy(python_exe)
        if version is not None:
            _log(
                f"NVIDIA ready -- CuPy {version} on CUDA {stamp.get('cuda_major')}",
                quiet=quiet_if_cached,
            )
            return "gpu_ready"
        # CuPy missing/broken since last check; fall through to re-install.

    # Cached no_nvidia: honour TTL.
    if stamp and stamp.get("result") == "no_nvidia":
        if _stamp_age_days(stamp) < NO_NVIDIA_TTL_DAYS:
            _log("CPU mode (no NVIDIA cached; clear stamp to re-check)",
                 quiet=quiet_if_cached)
            return "cached_no_nvidia"

    # Cached install_failed: respect retry cap — UNLESS the cap was reached by a
    # DIFFERENT (older) installer. A changed INSTALLER_VERSION means the install
    # logic itself changed (e.g. this PR's explicit nvidia-wheel install replacing
    # the no-op [ctk]), so the old failures are no longer predictive — give the
    # new installer a fresh set of attempts (Codex P2 PR #72: the stranded-upgrade
    # case, exactly the friend if he'd exhausted the cap on the broken installer).
    if stamp and stamp.get("result") == "install_failed":
        capped = stamp.get("attempts", 0) >= INSTALL_FAILED_MAX_ATTEMPTS
        same_installer = stamp.get("installer_version") == INSTALLER_VERSION
        if capped and same_installer:
            _log(
                f"install failed {stamp['attempts']} times; "
                "not retrying. Clear .launcher_state/gpu_status.json to retry."
            )
            return "install_failed"
        if capped and not same_installer:
            _log(
                "previous GPU install failures were from an older installer "
                f"({stamp.get('installer_version')} -> {INSTALLER_VERSION}); "
                "retrying with the updated installer"
            )

    # Active detection.
    info = detect_nvidia()
    if info is None:
        _write_stamp({
            "result": "no_nvidia",
            "driver_version": None,
            "cuda_major": None,
            "cupy_package": None,
            "cupy_version": None,
        })
        _log("CPU mode (no NVIDIA found)")
        return "no_nvidia"

    # M1 (subagent MEDIUM): unsupported CUDA major (10, 11, 14+) must NOT
    # enter the install-retry loop — install_cupy would return False
    # three launches in a row before the cap fires, polluting the user's
    # log with "CuPy install failed" messages that aren't going to
    # resolve until CuPy ships a wheel for that CUDA major (or never,
    # for 11.x). Short-circuit to a permanent no_nvidia stamp with a
    # descriptive cuda_major + driver_version so a future debugger can
    # see "GPU was there, just unsupported by current CuPy."
    if info["cuda_major"] not in _CUDA_TO_CUPY:
        _write_stamp({
            "result": "no_nvidia",
            "driver_version": info["driver_version"],
            "cuda_major": info["cuda_major"],
            "cupy_package": None,
            "cupy_version": None,
            "last_error": (
                f"CUDA {info['cuda_major']}.x detected but no matching "
                f"CuPy wheel in current stable (need 12.x or 13.x)"
            ),
        })
        _log(
            f"CPU mode -- NVIDIA driver {info['driver_version']} present "
            f"but CUDA {info['cuda_major']}.x has no current-stable CuPy "
            "wheel (CuPy ships 12.x and 13.x). Stamped as no_nvidia."
        )
        return "no_nvidia"

    # NVIDIA present + CUDA supported — acquire lock + install.
    if not _acquire_lock():
        _log("lock acquisition timed out; falling back to CPU this launch")
        return "lock_timeout"
    try:
        # CodeRabbit major (PR #54 round 1): re-check the stamp for a
        # FRESH gpu_ready BEFORE doing any install work. A sibling
        # launcher may have just installed CuPy successfully while we
        # were waiting on the lock — in that case we should short-
        # circuit to gpu_ready (after probing to confirm) instead of
        # running a redundant pip install. This is separate from the
        # H1 attempts-counter re-read below (which only triggers on
        # the install-failure path).
        fresh_stamp = _load_stamp()
        if fresh_stamp and fresh_stamp.get("result") == "gpu_ready":
            version = probe_cupy(python_exe)
            if version is not None:
                _log(
                    f"NVIDIA ready -- CuPy {version} on CUDA "
                    f"{fresh_stamp.get('cuda_major')} (installed by "
                    "sibling launcher while we were waiting)",
                    quiet=quiet_if_cached,
                )
                return "gpu_ready"
        # P3 (codex PR #54): re-check the install-failure cap against the
        # FRESH in-lock stamp too. The pre-lock cap check (above) can be
        # passed by two concurrent launchers each holding a stale
        # attempts=N-1 view; both then serialize on the lock. Without this
        # second check, the launcher that acquires the lock AFTER a sibling
        # already wrote the cap-reaching attempts count would still run a
        # redundant install_cupy and bump attempts past the cap. Honour the
        # sibling's freshly-written failure count and bail here.
        if (
            fresh_stamp
            and fresh_stamp.get("result") == "install_failed"
            and fresh_stamp.get("attempts", 0) >= INSTALL_FAILED_MAX_ATTEMPTS
            # ...but only if those failures came from the CURRENT installer
            # (Codex P2 PR #72) — an installer-version change resets the cap.
            and fresh_stamp.get("installer_version") == INSTALLER_VERSION
        ):
            _log(
                f"install failed {fresh_stamp['attempts']} times "
                "(updated by sibling launcher); not retrying. Clear "
                ".launcher_state/gpu_status.json to retry."
            )
            return "install_failed"
        # H1 (subagent HIGH): re-read the stamp from disk INSIDE the lock
        # before computing prior_attempts. Two concurrent launchers can
        # both load the stamp pre-lock with attempts=1, both fall
        # through the cap check, then both serialize on the lock. The
        # second to acquire the lock would otherwise read its in-memory
        # stamp (still attempts=1) and write attempts=2, clobbering
        # process A's attempts=2 → counter sticks at 2 instead of
        # reaching the 3-attempt cap. Re-reading fresh inside the lock
        # gives process B the post-A view (attempts=2) and lets it
        # write attempts=3 correctly. ``fresh_stamp`` above already
        # holds the in-lock re-read used by the gpu_ready check; we
        # reuse it here so we don't issue a redundant stat+read.
        locked_stamp = fresh_stamp
        _log(
            f"NVIDIA driver {info['driver_version']} / CUDA "
            f"{info['cuda_major']}.x detected -- installing "
            f"{_CUDA_TO_CUPY[info['cuda_major']]} "
            "(one-time GPU setup, downloads ~1-2GB of CUDA wheels; typically "
            "2-10 min, longer on a slow connection). A heartbeat line prints "
            "every 20s so you know it's not frozen..."
        )
        ok, msg = install_cupy(python_exe, info["cuda_major"])
        if ok:
            _write_stamp({
                "result": "gpu_ready",
                "driver_version": info["driver_version"],
                "cuda_major": info["cuda_major"],
                "gpu_name": info.get("gpu_name"),
                "cupy_package": _CUDA_TO_CUPY[info["cuda_major"]],
                "cupy_version": msg,
                "attempts": 0,
            })
            _log(f"CuPy {msg} ready -- rPPG injector will use GPU")
            return "gpu_installed_now"
        # Read attempts from the locked re-load so concurrent retries
        # increment monotonically. BUT reset to 0 when the prior failures came
        # from a different installer (Codex P2 PR #72) — otherwise the carried
        # count would re-cap immediately and the fixed installer would get only
        # one shot (or zero) instead of a fresh INSTALL_FAILED_MAX_ATTEMPTS.
        prior = locked_stamp or {}
        prior_attempts = (
            prior.get("attempts", 0)
            if prior.get("installer_version") == INSTALLER_VERSION
            else 0
        )
        _write_stamp({
            "result": "install_failed",
            "driver_version": info["driver_version"],
            "cuda_major": info["cuda_major"],
            "cupy_package": _CUDA_TO_CUPY[info["cuda_major"]],
            "cupy_version": None,
            "attempts": prior_attempts + 1,
            "last_error": msg,
        })
        _log(
            f"CuPy install failed ({msg[:120]}) -- "
            "falling back to CPU. Will retry next launch."
        )
        return "install_failed"
    finally:
        _release_lock()


def main(argv: Optional[list] = None) -> int:
    parser = argparse.ArgumentParser(
        description=(
            "Detect NVIDIA GPU + auto-install matching CuPy, and select the "
            "hardware-appropriate torch wheel. Idempotent + cached. Called "
            "from the Windows + macOS launchers right before the GUI launches."
        ),
    )
    parser.add_argument(
        "--python-exe", default=sys.executable,
        help="Path to the target venv python. Defaults to the running "
             "interpreter (when invoked by the launcher this IS the venv).",
    )
    parser.add_argument(
        "--quiet-if-cached", action="store_true",
        help="Suppress 'NVIDIA ready' / 'CPU mode cached' lines when the "
             "stamp is current. The interesting cases (first detection, "
             "install, failure) always print. Recommended for launcher "
             "wiring so repeat launches stay clean.",
    )
    parser.add_argument(
        "--print-stamp-token", action="store_true",
        help="Print ONLY a deterministic token (INSTALLER_VERSION + platform "
             "+ resolved torch mode + cuda_major + constraints sha) and exit. "
             "Launchers fold this into their dep stamp key so a GPU-mode "
             "change or installer bump invalidates the stamp. No pip work.",
    )
    parser.add_argument(
        "--select-torch", metavar="TORCH_SPEC", default=None,
        help="Install the hardware-appropriate torch wheel for TORCH_SPEC "
             "(e.g. 'torch>=2.2,<3') and exit. NVIDIA->CUDA index, else CPU "
             "index, macOS->default. Always exits 0 (best-effort).",
    )
    parser.add_argument(
        "--constraints", default=None,
        help="Path to constraints.txt, threaded into the torch pip install "
             "and the stamp-token sha. Defaults to repo-root constraints.txt "
             "when present.",
    )
    args = parser.parse_args(argv)

    constraints = args.constraints
    if constraints is None:
        default_c = REPO_ROOT / "constraints.txt"
        if default_c.is_file():
            constraints = str(default_c)

    # --print-stamp-token: pure, no install. Print the token to stdout (the
    # ONLY stdout line) so the launcher can capture it directly.
    if args.print_stamp_token:
        print(compute_stamp_token(constraints))
        return 0

    # --select-torch: hardware-appropriate torch install, then exit 0.
    if args.select_torch is not None:
        _result_ok, msg = select_torch_install(
            args.python_exe, args.select_torch, constraints_path=constraints
        )
        del _result_ok  # best-effort: status logged, never blocks the launcher
        _log(f"torch select: {msg}")
        # Best-effort like the CuPy path — never block the launcher on a
        # torch install hiccup (CPU torch already landed via -r requirements).
        return 0

    bootstrap(args.python_exe, quiet_if_cached=args.quiet_if_cached)
    # H2 (subagent HIGH): always exit 0. GPU bootstrap is best-effort —
    # any failure (no NVIDIA, install crashed, lock timed out, etc.)
    # MUST fall back to CPU silently and let the GUI launch normally.
    # The launcher chain treats a non-zero exit as fatal, so a buggy
    # "return 1 on install_failed" would block legitimate launches on
    # transient pip failures. The prior `0 if ... else 0` ternary
    # signalled this intent but read as a bug; reduce to a single
    # return so a future contributor can't accidentally flip the
    # `else` branch.
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
