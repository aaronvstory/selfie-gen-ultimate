#!/usr/bin/env python3
"""Launch-time GPU-aware ``uv sync`` for the torch/CuPy extra (v2.20).

Under uv, the full pinned face stack is locked cross-platform in ``uv.lock``,
but torch (and CuPy) come through three MUTUALLY EXCLUSIVE extras — ``cpu`` /
``cu121`` / ``cu128`` — because uv markers are STATIC (platform/arch) and can't
express "this Windows box has a *working* NVIDIA GPU". That GPU-presence split
is a RUNTIME fact, so it's decided here, at launch, and handed to uv as
``uv sync --extra <X>``.

This module is the uv-native replacement for ``gpu_bootstrap.select_torch_install``
(which did the same job via raw ``pip install --index-url``). It deliberately
REUSES the proven, unit-tested PURE decision logic from gpu_bootstrap —
``detect_nvidia()`` (nvidia-smi parse) and ``resolve_torch_mode()`` (the
mac-never-CUDA / NVIDIA→CUDA / else→CPU table) — so there is ONE decision table,
not two. Only the INSTALL mechanism changes (uv extra vs pip index).

Decision → extra mapping:

    resolve_torch_mode mode | cuda_major | uv extra
    ------------------------|------------|---------
    mac_default             |     —      | cpu   (torch falls back to PyPI MPS
                            |            |        wheel; the cpu index is
                            |            |        win/linux-gated in pyproject)
    cuda                    |     12     | cu121
    cuda                    |     13     | cu128
    cpu                     |     —      | cpu

After a ``cuda`` sync we run ``torch.cuda.is_available()`` in the synced env; if
a CUDA build is runtime-broken (missing DLLs / driver mismatch / AV quarantine),
we re-sync ``--extra cpu`` — mirroring select_torch_install's probe + CPU
fallback exactly. torch is only used by DeepFace's anti-spoofing classifier and
CUDA only affects SPEED (production never calls torch.cuda.*), so every failure
path degrades to a working CPU env, never a broken launch. This script ALWAYS
exits 0.

CLI:
    python scripts/uv_torch_select.py [--project DIR] [--print-extra] [--quiet]

    --print-extra : print ONLY the resolved extra name (cpu/cu121/cu128) and
                    exit, running no sync. Lets a launcher capture the choice
                    for its own ``uv sync`` invocation (or for a dep stamp).
    (no flag)     : run the full ``uv sync --extra <X>`` (+ CUDA probe/fallback).
"""

from __future__ import annotations

import argparse
import os
import shutil
import subprocess
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent

# Reuse the PURE, unit-tested decision logic from the existing GPU subsystem.
# These are import-only (no I/O at import), so this stays cheap to load.
sys.path.insert(0, str(REPO_ROOT / "scripts"))
try:
    from gpu_bootstrap import detect_nvidia, resolve_torch_mode
except Exception:  # pragma: no cover - gpu_bootstrap should always be present
    detect_nvidia = None  # type: ignore[assignment]
    resolve_torch_mode = None  # type: ignore[assignment]


# Map a (mode, cuda_major) decision to a uv extra name. Single source of truth;
# tested by tests/test_uv_torch_select.py and kept parity with
# gpu_bootstrap._TORCH_CUDA_INDEX (12->cu121, 13->cu128).
_CUDA_MAJOR_TO_EXTRA = {12: "cu121", 13: "cu128"}


def _log(msg: str, *, quiet: bool = False) -> None:
    if not quiet:
        print(f"  uv-torch: {msg}", flush=True)


def resolve_extra() -> tuple[str, str]:
    """Return ``(extra, reason)`` — the uv extra to sync for this machine.

    PURE-ish: the only I/O is the nvidia-smi probe inside detect_nvidia (on
    non-mac). macOS hard-returns ``cpu`` before any GPU probe (mac never CUDA).
    Falls back to ``cpu`` if gpu_bootstrap is somehow unavailable.
    """
    if detect_nvidia is None or resolve_torch_mode is None:
        return ("cpu", "gpu_bootstrap unavailable; defaulting to CPU torch")

    is_darwin = sys.platform == "darwin"
    nvidia = None if is_darwin else detect_nvidia()
    decision = resolve_torch_mode(platform_is_darwin=is_darwin, nvidia=nvidia)
    mode = decision["mode"]

    if mode == "mac_default":
        return ("cpu", "macOS: cpu extra (torch resolves to PyPI MPS/CPU wheel)")
    if mode == "cuda":
        cuda_major = decision.get("cuda_major")
        extra = _CUDA_MAJOR_TO_EXTRA.get(cuda_major)
        if extra is not None:
            return (extra, decision["reason"])
        # NVIDIA present but unmapped CUDA major -> CPU (matches resolve_torch_mode,
        # which already returns mode=cpu in that case; this is belt-and-suspenders).
        return ("cpu", f"CUDA major {cuda_major} unmapped; CPU torch")
    return ("cpu", decision.get("reason", "no NVIDIA; CPU torch"))


def _uv_exe() -> str | None:
    """Locate the uv executable. Prefer PATH; fall back to the common
    standalone-install dirs uv uses on each OS (the end user never adds it to
    PATH manually — the launcher bootstraps it there)."""
    found = shutil.which("uv")
    if found:
        return found
    candidates = []
    home = Path.home()
    if sys.platform == "win32":
        candidates += [
            home / ".local" / "bin" / "uv.exe",
            Path(os.environ.get("USERPROFILE", str(home))) / ".local" / "bin" / "uv.exe",
        ]
    else:
        candidates += [
            home / ".local" / "bin" / "uv",
            Path("/opt/homebrew/bin/uv"),
            Path("/usr/local/bin/uv"),
        ]
    for c in candidates:
        if c.exists():
            return str(c)
    return None


def _run_uv_sync(uv: str, project: Path, extra: str, *, quiet: bool) -> int:
    """``uv sync --extra <extra>`` in ``project``. Returns uv's exit code.

    --no-default-groups keeps dev/test groups out of the end-user env.
    --frozen would forbid re-resolution; we DON'T use it here so a launcher can
    self-heal if the lock drifted — but the committed lock should make this a
    fast no-op when already in sync (uv verifies the lock, not just presence).
    """
    cmd = [uv, "sync", "--no-default-groups", "--extra", extra]
    _log(f"running: uv sync --extra {extra}", quiet=quiet)
    try:
        proc = subprocess.run(cmd, cwd=str(project))
        return proc.returncode
    except (OSError, subprocess.SubprocessError) as exc:
        _log(f"uv sync failed to launch: {exc!r}", quiet=quiet)
        return 1


def _synced_python(project: Path) -> Path:
    """Path to the interpreter inside the uv project env (.venv by default,
    overridable via UV_PROJECT_ENVIRONMENT)."""
    env_dir = os.environ.get("UV_PROJECT_ENVIRONMENT")
    base = Path(env_dir) if env_dir else (project / ".venv")
    if sys.platform == "win32":
        return base / "Scripts" / "python.exe"
    return base / "bin" / "python"


def _probe_torch_cuda(python_exe: Path) -> tuple[bool, bool]:
    """Return ``(import_ok, cuda_available)`` for torch in the synced env.
    Conservative ``(False, False)`` on any subprocess failure."""
    # If `import torch` raises, the process exits non-zero and we never print
    # OK — so reaching the print lines IS the success signal.
    src = (
        "import torch;"
        "print('IMPORT_OK');"
        "print('CUDA=' + str(bool(torch.cuda.is_available())))"
    )
    try:
        proc = subprocess.run(
            [str(python_exe), "-c", src],
            capture_output=True,
            text=True,
            errors="replace",
            timeout=120,
        )
    except (OSError, subprocess.SubprocessError):
        return (False, False)
    if proc.returncode != 0:
        return (False, False)
    out = proc.stdout or ""
    return ("IMPORT_OK" in out, "CUDA=True" in out)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="GPU-aware uv sync for torch/CuPy.")
    parser.add_argument("--project", default=str(REPO_ROOT), help="uv project dir")
    parser.add_argument(
        "--print-extra",
        action="store_true",
        help="print the resolved extra (cpu/cu121/cu128) and exit; no sync",
    )
    parser.add_argument("--quiet", action="store_true")
    args = parser.parse_args(argv)

    extra, reason = resolve_extra()

    if args.print_extra:
        # Stdout MUST be exactly the extra name (a launcher captures it).
        print(extra)
        return 0

    _log(f"torch extra = {extra} ({reason})", quiet=args.quiet)

    uv = _uv_exe()
    if uv is None:
        _log("uv not found on PATH or standard dirs; skipping (launcher bootstraps uv)")
        return 0  # never block launch

    project = Path(args.project)
    rc = _run_uv_sync(uv, project, extra, quiet=args.quiet)
    if rc != 0:
        # A failed sync of a CUDA extra: try CPU as a last resort so the user
        # still gets a working (CPU) env rather than a hard failure.
        if extra != "cpu":
            _log(f"uv sync --extra {extra} failed (rc={rc}); falling back to --extra cpu")
            _run_uv_sync(uv, project, "cpu", quiet=args.quiet)
        return 0

    # Verify a CUDA sync actually yields a working CUDA runtime; fall back to
    # CPU torch if not (broken DLLs / driver mismatch / AV quarantine).
    if extra in ("cu121", "cu128"):
        py = _synced_python(project)
        import_ok, cuda_available = _probe_torch_cuda(py)
        if not import_ok:
            _log("CUDA torch synced but import failed; falling back to --extra cpu")
            _run_uv_sync(uv, project, "cpu", quiet=args.quiet)
        elif not cuda_available:
            _log(
                "CUDA torch synced but torch.cuda.is_available() is False "
                "(broken DLLs / driver mismatch); falling back to --extra cpu"
            )
            _run_uv_sync(uv, project, "cpu", quiet=args.quiet)
        else:
            _log("CUDA torch verified (torch.cuda.is_available() == True)", quiet=args.quiet)

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
