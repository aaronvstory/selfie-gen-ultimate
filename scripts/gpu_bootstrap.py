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

* **Windows**: nvidia-smi parsing → cupy-cuda12x / cupy-cuda13x with the
  ``[ctk]`` extra to pull CUDA component wheels from PyPI (avoids
  requiring a system CUDA Toolkit; CuPy 14+ official guidance).
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
PIP_INSTALL_TIMEOUT_SECONDS = 900   # 15 min: covers the +500 MB CuPy
                                    # wheel + CUDA component fetches.
LOCK_STALE_SECONDS = PIP_INSTALL_TIMEOUT_SECONDS + 300

# Map CUDA major → PyPI package name. Per CuPy 14+ install docs the
# stable wheels are cupy-cuda12x and cupy-cuda13x; older 11.x is no
# longer the current-stable target. CUDA 11.x on a current driver
# *would* downgrade us to a legacy CuPy pin which is out of scope —
# log CPU fallback instead. Append [ctk] to pull CUDA component
# wheels (matches the official "Installing CuPy with CUDA from PyPI"
# path that doesn't require a system CUDA Toolkit).
_CUDA_TO_CUPY = {
    12: "cupy-cuda12x[ctk]",
    13: "cupy-cuda13x[ctk]",
}


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


def detect_nvidia() -> Optional[dict]:
    """Run ``nvidia-smi`` and return {'driver_version', 'cuda_major'}
    or None when there's no NVIDIA GPU / driver.

    nvidia-smi's no-flag output includes a ``CUDA Version: 12.6`` field
    in the header. We parse that rather than guessing from the driver
    version because the driver→CUDA-runtime table changes per release
    and the header is canonical.

    Robust to: missing executable (Linux/macOS without NVIDIA, or a
    Windows box where nvidia-smi isn't on PATH), non-zero exit,
    unexpected output. All failures → None.
    """
    exe = _resolve_nvidia_smi()
    if exe is None:
        return None
    try:
        proc = subprocess.run(
            [exe],
            capture_output=True,
            text=True,
            errors="replace",
            timeout=10,
        )
    except (FileNotFoundError, subprocess.TimeoutExpired, OSError):
        return None
    if proc.returncode != 0:
        return None
    out = proc.stdout or ""
    # Driver version: "Driver Version: 555.52"
    m_drv = re.search(r"Driver Version:\s*([0-9.]+)", out)
    # CUDA version: "CUDA Version: 12.6" (header, not per-GPU)
    m_cuda = re.search(r"CUDA Version:\s*([0-9]+)\.([0-9]+)", out)
    if not m_drv or not m_cuda:
        return None
    return {
        "driver_version": m_drv.group(1),
        "cuda_major": int(m_cuda.group(1)),
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
    probe_src = (
        "import sys; import cupy as cp;"
        "x = cp.asarray([1, 2, 3]);"
        "_ = cp.asnumpy(x);"
        "_ = cp.cuda.runtime.getDeviceCount();"
        "print('CUPYVER=' + cp.__version__)"
    )
    try:
        proc = subprocess.run(
            [python_exe, "-c", probe_src],
            capture_output=True,
            text=True,
            errors="replace",
            timeout=30,
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


def install_cupy(python_exe: str, cuda_major: int) -> tuple[bool, str]:
    """``pip install`` the right CuPy wheel for the detected CUDA major.

    Returns ``(success, message)``. ``message`` is either the CuPy
    version on success or the pip stderr tail on failure.

    Uses ``--only-binary :all:`` so we never try to compile from sdist
    (slow, requires NVCC). ``--no-input`` so an unexpected prompt never
    blocks the launcher chain.
    """
    pkg = _CUDA_TO_CUPY.get(cuda_major)
    if pkg is None:
        return (False, f"unsupported CUDA major {cuda_major}; need 12 or 13")
    cmd = [
        python_exe, "-m", "pip", "install",
        "--only-binary", ":all:",
        "--no-input",
        pkg,
    ]
    try:
        proc = subprocess.run(
            cmd, capture_output=True, text=True,
            errors="replace",
            timeout=PIP_INSTALL_TIMEOUT_SECONDS,
        )
    except (subprocess.TimeoutExpired, OSError) as exc:
        return (False, f"pip subprocess error: {exc!r}")
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

    # Cached install_failed: respect retry cap.
    if stamp and stamp.get("result") == "install_failed":
        if stamp.get("attempts", 0) >= INSTALL_FAILED_MAX_ATTEMPTS:
            _log(
                f"install failed {stamp['attempts']} times; "
                "not retrying. Clear .launcher_state/gpu_status.json to retry."
            )
            return "install_failed"

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
            "(one-time, may take ~30-120s)..."
        )
        ok, msg = install_cupy(python_exe, info["cuda_major"])
        if ok:
            _write_stamp({
                "result": "gpu_ready",
                "driver_version": info["driver_version"],
                "cuda_major": info["cuda_major"],
                "cupy_package": _CUDA_TO_CUPY[info["cuda_major"]],
                "cupy_version": msg,
                "attempts": 0,
            })
            _log(f"CuPy {msg} ready -- rPPG injector will use GPU")
            return "gpu_installed_now"
        # Read attempts from the locked re-load so concurrent retries
        # increment monotonically.
        prior_attempts = (locked_stamp or {}).get("attempts", 0)
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
            "Detect NVIDIA GPU + auto-install matching CuPy. Idempotent + "
            "cached. Called from the Windows + macOS launchers right before "
            "the GUI is launched."
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
    args = parser.parse_args(argv)
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
