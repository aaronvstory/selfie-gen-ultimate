"""
Runtime dependency health check and repair for Kling UI startup.
"""

from __future__ import annotations

import argparse
import importlib
import subprocess
import sys
from typing import Callable

from kling_gui.ml_backend_env import ensure_ml_backend_env

ImportFn = Callable[[str], object]
RuntimeProbeFn = Callable[[], tuple[object | None, str]]

REPAIR_PACKAGES = [
    "tensorflow==2.16.2",
    "protobuf==4.25.3",
    "tf-keras==2.16.0",
    "retina-face==0.0.17",
    "deepface==0.0.92",
]

if sys.platform == "win32":
    REPAIR_PACKAGES.insert(1, "tensorflow-intel==2.16.2")


# CUDA-aware torch wheels can fail to load on machines with missing /
# mismatched cuDNN, missing cudart64_*.dll, or NVIDIA drivers stale relative
# to the wheel's compiled CUDA version. Production code does not use
# `torch.cuda.*` (verified by `grep -rn "torch\.cuda\." --include="*.py"` —
# only test fixtures reference it). CPU-only torch is functionally
# equivalent for this app, so when the CUDA wheel fails import or backend
# init we transparently fall back to CPU-only wheels.
#
# Fingerprint substrings used to identify CUDA-import-failure mode (see
# `_torch_cuda_load_failure_signature`). These are matched case-insensitive
# against the exception chain raised by `import torch` or `torch.zeros(1)`.
# Drawn from real-world failure traces on Windows nvidia + Linux mismatched-
# driver setups.
_TORCH_CUDA_FAILURE_SIGNATURES = (
    "cudart",
    "cudnn",
    "cublas",
    "cusparse",
    "cusolver",   # subagent PR #55 round 5 MED — linear algebra; shipped w/torch
    "cufft",      # subagent PR #55 round 5 MED — FFT; shipped w/torch
    "nvrtc",
    "nvjpeg",     # subagent PR #55 round 5 MED — torchvision image codecs
    "nvtx",       # subagent PR #55 round 5 MED — NVTX profiling
    "nccl",
    "cuda runtime",
    "cuda driver",
    "libnvrtc",
    "no cuda gpus are available",  # not a failure per se, but ok to keep CPU
    "torch not compiled with cuda enabled",
)

# PyPI index URL for CPU-only torch wheels. Documented at
# https://pytorch.org/get-started/locally/ ("CPU" option).
_TORCH_CPU_INDEX_URL = "https://download.pytorch.org/whl/cpu"


def _default_retinaface_runtime_probe() -> tuple[object | None, str]:
    from kling_gui.tabs.face_crop_tab import _load_retinaface

    return _load_retinaface()


def _torch_cuda_load_failure_signature(exc: BaseException) -> str:
    """Return a CUDA failure signature substring if ``exc``'s chain matches,
    else empty string. Walks the full ``__cause__`` / ``__context__`` chain
    so we catch failures that surface deep inside ``torch._C`` or DLL load."""
    seen: set[int] = set()
    cur: BaseException | None = exc
    while cur is not None and id(cur) not in seen:
        seen.add(id(cur))
        msg = f"{type(cur).__name__}: {cur}".lower()
        for sig in _TORCH_CUDA_FAILURE_SIGNATURES:
            if sig in msg:
                return sig
        cur = cur.__cause__ or cur.__context__
    return ""


def check_runtime_dependencies(
    importer: ImportFn = importlib.import_module,
    runtime_probe: RuntimeProbeFn = _default_retinaface_runtime_probe,
) -> tuple[bool, list[str]]:
    """Validate the runtime face-stack imports used by the GUI.

    Reports torch-CUDA-load failures with a stable ``torch_cuda_failure:<sig>``
    prefix so ``run_repair`` can detect them and trigger the CPU-only torch
    fallback. Pure-import failures (no CUDA signature) use the legacy
    ``torch import failed`` prefix and go through normal repair.
    """
    ensure_ml_backend_env()
    failures: list[str] = []

    try:
        tf_module = importer("tensorflow")
    except Exception as exc:
        failures.append(f"tensorflow import failed: {type(exc).__name__}: {exc}")
    else:
        tf_version = getattr(tf_module, "__version__", None)
        if not tf_version:
            failures.append("tensorflow missing __version__ (broken namespace install)")
        try:
            importer("tensorflow.compat.v2")
        except Exception as exc:
            failures.append(f"tensorflow.compat.v2 import failed: {type(exc).__name__}: {exc}")

    try:
        importer("tf_keras")
    except Exception as exc:
        failures.append(f"tf_keras import failed: {type(exc).__name__}: {exc}")

    try:
        try:
            importer("retinaface.RetinaFace")
        except Exception:
            retinaface_module = importer("retinaface")
            getattr(retinaface_module, "RetinaFace")
    except Exception as exc:
        failures.append(f"retinaface import failed: {type(exc).__name__}: {exc}")

    for module_name in ("cv2", "numpy"):
        try:
            importer(module_name)
        except Exception as exc:
            failures.append(f"{module_name} import failed: {type(exc).__name__}: {exc}")

    # Torch probe: import + force CUDA runtime init so a broken CUDA wheel
    # surfaces here (a successful `import torch` doesn't always touch the
    # CUDA sub-stack; the trap is delayed until first `.cuda()` call or
    # first CUDA op). If import is fine but CUDA is broken, we transparently
    # fall back to CPU-only torch in `run_repair` — production doesn't use
    # `torch.cuda.*` (verified by ripgrep on the production tree).
    #
    # Subagent PR #55 round 5 MED: the previous probe `torch.zeros(1)`
    # defaults to `device='cpu'` and never touches CUDA, so it did NOT
    # surface deferred-CUDA-init failures (the dominant non-import-time
    # mode). `torch.cuda.is_available()` is the canonical eager-init call
    # — it lazily loads the CUDA runtime DLLs the first time it's called,
    # which is when a broken cudart64_*.dll surfaces. The function returns
    # False gracefully on CPU-only torch, so the probe is safe on both
    # build variants.
    try:
        torch_module = importer("torch")
    except Exception as exc:
        sig = _torch_cuda_load_failure_signature(exc)
        if sig:
            failures.append(f"torch_cuda_failure:{sig}: {type(exc).__name__}: {exc}")
        else:
            failures.append(f"torch import failed: {type(exc).__name__}: {exc}")
    else:
        # CUDA init probe + build-vs-runtime mismatch detection.
        #
        # CodeRabbit PR #55 round 8 Major: ``cuda.is_available()`` returns
        # False in TWO scenarios that look identical at the API surface:
        #   1. CPU-only torch build (no CUDA support compiled in) — EXPECTED,
        #      not a failure. ``torch.version.cuda`` is ``None``.
        #   2. CUDA-built torch wheel BUT runtime DLLs can't load (missing
        #      cudart64_*.dll, driver too old, AV quarantine) — FAILURE.
        #      ``torch.version.cuda`` is a string like ``"12.1"`` but
        #      ``is_available()`` returns False after logging a warning.
        #
        # Without disambiguation, scenario 2 silently passes the probe and
        # the launcher never triggers the CPU fallback. Distinguish via
        # ``torch.version.cuda``: only flag as ``torch_cuda_failure:`` if
        # the build advertises CUDA support but the runtime says no.
        try:
            cuda_ns = getattr(torch_module, "cuda", None)
            if cuda_ns is not None and callable(getattr(cuda_ns, "is_available", None)):
                # Side effect: loads CUDA runtime DLLs the first time it's
                # called. Catches the DLL-load-exception class of failure
                # in the except branch below.
                cuda_available = cuda_ns.is_available()
                # Build-vs-runtime mismatch check. Use `is not None`
                # (not truthiness) for module check, matching the
                # `if cuda_ns is not None` style on the prior line —
                # subagent round 8 MED caught a lazy-import shim that
                # overrides ``__bool__`` to return False would bypass
                # the check under the old `if version_ns` truthiness test.
                version_ns = getattr(torch_module, "version", None)
                build_cuda_version = (
                    getattr(version_ns, "cuda", None)
                    if version_ns is not None
                    else None
                )
                if build_cuda_version and not cuda_available:
                    failures.append(
                        f"torch_cuda_failure:build_runtime_mismatch: "
                        f"torch built with CUDA {build_cuda_version} but "
                        f"cuda.is_available() returned False at runtime "
                        f"(broken DLLs, driver mismatch, or AV quarantine)"
                    )
        except Exception as exc:
            sig = _torch_cuda_load_failure_signature(exc)
            if sig:
                failures.append(f"torch_cuda_failure:{sig}: {type(exc).__name__}: {exc}")
            else:
                failures.append(f"torch runtime probe failed: {type(exc).__name__}: {exc}")

    # Run the RetinaFace runtime probe whenever no NON-CUDA failures
    # blocked us. Codex PR #55 round-2 P2 (#PRRT_kwDOSQUnmM6FPwqp): the
    # previous gate `if not failures` skipped the runtime probe whenever
    # `torch_cuda_failure:*` was present. That class of failure is then
    # explicitly tolerated by `main()`'s partial-success path (GUI is
    # launchable on CPU torch), so it must NOT mask the RetinaFace probe.
    # Otherwise on a combined-failure machine (broken CUDA torch + broken
    # TensorFlow/Keras/RetinaFace loader at runtime), the launcher would
    # treat repair as successful and open the GUI into the very Face Crop
    # broken state this PR is meant to eliminate.
    non_cuda_failures = [
        f for f in failures if not f.startswith("torch_cuda_failure:")
    ]
    if not non_cuda_failures:
        try:
            retinaface_cls, retinaface_error = runtime_probe()
            if retinaface_cls is None:
                failures.append(f"retinaface runtime loader failed: {retinaface_error}")
        except Exception as exc:
            failures.append(f"retinaface runtime probe failed: {type(exc).__name__}: {exc}")

    return len(failures) == 0, failures


def _failures_indicate_torch_cuda_break(failures: list[str]) -> bool:
    """True if any failure message has the ``torch_cuda_failure:`` prefix."""
    return any(f.startswith("torch_cuda_failure:") for f in failures)


def _extract_pip_failure_detail(completed: subprocess.CompletedProcess) -> str:
    """Return the most useful one-line summary of a failed pip subprocess.

    Gemini PR #55 round 5 MED: ``details.splitlines()[-1]`` can easily mask
    the actual error when pip prints a trailing warning (e.g.
    ``WARNING: You are using pip version X; ...``). Searches stderr+stdout
    for the first line starting with ``ERROR:`` (pip's canonical error
    prefix) and falls back to the last line of stderr (or stdout) only
    when no explicit error line is found.
    """
    stderr = (completed.stderr or "").strip()
    stdout = (completed.stdout or "").strip()
    for source in (stderr, stdout):
        for line in source.splitlines():
            if line.startswith("ERROR:"):
                return line
    fallback = stderr if stderr else stdout
    if fallback:
        return fallback.splitlines()[-1]
    return ""


def _installed_torch_version() -> str | None:
    """Return the currently-installed torch's PUBLIC version (string), or None.

    Reads package metadata from disk via ``importlib.metadata`` so it does
    NOT trigger an actual ``import torch`` — the whole point of the CPU
    fallback is that the installed torch is broken at import time, so any
    code path that tries to import it would defeat the version probe.

    Strips the PEP 440 local version identifier (anything from ``+`` onwards,
    e.g. ``+cu121``, ``+cu118``, ``+cpu``). The CPU wheel index at
    ``download.pytorch.org/whl/cpu`` only hosts public/un-suffixed wheels —
    pinning to a CUDA-local version like ``torch==2.5.1+cu121`` would fail
    against that index, defeating the entire CPU-fallback purpose in
    exactly the Windows nvidia scenario it's meant to repair.

    Gemini PR #55 round-2 MED (#3313903515): pin the reinstall so the CPU
    fallback doesn't silently upgrade torch to whatever the wheel index
    advertises.

    Gemini PR #55 round-2 HIGH (#PRRT_kwDOSQUnmM6FPccQ) + Codex P2
    (#PRRT_kwDOSQUnmM6FPdVZ): on Windows-CUDA installs, the broken torch
    metadata reports e.g. ``2.5.1+cu121``. Strip the ``+...`` suffix so
    we pin to the PUBLIC ``2.5.1`` which IS available on the CPU index.
    """
    try:
        import importlib.metadata as _md
        raw = _md.version("torch")
    except Exception:
        return None
    if not raw:
        return None
    # PEP 440 local version identifier separator. Everything after `+` is
    # the local segment (build tag, CUDA suffix, etc) and is not present
    # on the public wheel index. Keep only the public base version.
    base, _, _local = raw.partition("+")
    return base or None


def run_torch_cpu_fallback() -> tuple[bool, str]:
    """Force-reinstall torch from the CPU-only wheel index.

    Returns ``(success, message)``. Same shape as ``run_repair``. Called by
    ``run_repair`` when ``check_runtime_dependencies`` flagged a CUDA load
    failure, so users on broken-CUDA Windows nvidia setups end up with a
    working CPU-only torch instead of the launcher dead-ending.

    Gemini PR #55 round 4 HIGH: bare ``--index-url`` restricts pip to
    search **only** the PyTorch CPU wheel index — which doesn't host
    torch's runtime deps (``filelock``, ``sympy``, ``networkx``, etc).
    Adding ``--extra-index-url https://pypi.org/simple`` lets pip fall
    back to PyPI for those, so the install actually resolves.

    Gemini PR #55 round-2 MED (#3313903515): pin the reinstall to the
    currently-installed torch version so the CPU fallback doesn't silently
    upgrade torch to whatever the wheel index advertises. If the metadata
    probe fails (e.g. torch's dist-info was deleted), fall back to unpinned
    ``torch`` — better an upgraded install than no install.
    """
    torch_ver = _installed_torch_version()
    torch_spec = f"torch=={torch_ver}" if torch_ver else "torch"
    cmd = [
        sys.executable,
        "-m",
        "pip",
        "install",
        "--upgrade",
        "--force-reinstall",
        "--no-cache-dir",
        "--index-url",
        _TORCH_CPU_INDEX_URL,
        "--extra-index-url",
        "https://pypi.org/simple",
        torch_spec,
    ]
    completed = subprocess.run(cmd, capture_output=True, text=True, check=False)
    if completed.returncode == 0:
        return True, "torch CPU-only fallback install completed"
    details = _extract_pip_failure_detail(completed)
    return False, f"torch CPU fallback failed (code {completed.returncode}): {details}"


def run_repair(failures: list[str] | None = None) -> tuple[bool, str]:
    """Attempt deterministic repair of the face dependency stack.

    If ``failures`` is provided and contains any ``torch_cuda_failure:`` entry,
    the CPU-only torch fallback runs FIRST (cheap, ~50MB) so the subsequent
    face-stack reinstall doesn't pull TF wheels into a broken CUDA torch
    environment. Without the failures list, behaves identically to prior
    versions (face-stack-only repair) for back-compat with any external
    caller that invokes ``run_repair()`` with no args.

    Codex PR #55 round 4 P2: face-stack repair runs UNCONDITIONALLY, even
    when the CPU fallback fails. The face stack (TF/tf-keras/retinaface)
    is independently repairable — if download.pytorch.org is blocked or
    flaky, the user can still get a working face_crop / video path. Only
    if BOTH the CPU fallback AND the face-stack install fail does
    ``run_repair`` return False. The combined message records both
    outcomes so the launcher's diagnostic log is clear.
    """
    messages: list[str] = []
    cuda_ok = True  # Treated as "no fallback needed" when no CUDA failure.

    if failures and _failures_indicate_torch_cuda_break(failures):
        cuda_ok, cuda_msg = run_torch_cpu_fallback()
        messages.append(cuda_msg)
        # Do NOT early-return on cuda_ok=False — the face stack is
        # independently repairable. The combined success/failure status
        # is computed from BOTH outcomes at the end of the method.

    cmd = [
        sys.executable,
        "-m",
        "pip",
        "install",
        "--upgrade",
        "--force-reinstall",
        "--no-cache-dir",
        *REPAIR_PACKAGES,
    ]
    completed = subprocess.run(cmd, capture_output=True, text=True, check=False)
    if completed.returncode == 0:
        messages.append("repair install completed")
        face_ok = True
    else:
        details = _extract_pip_failure_detail(completed)
        messages.append(f"repair failed (code {completed.returncode}): {details}")
        face_ok = False

    return (cuda_ok and face_ok), "; ".join(messages)


def verify_in_fresh_process() -> tuple[bool, list[str]]:
    """Run check mode in a new interpreter to avoid stale import cache after repair."""
    cmd = [sys.executable, __file__, "--mode", "check"]
    completed = subprocess.run(cmd, capture_output=True, text=True, check=False)
    if completed.returncode == 0:
        return True, []

    failures: list[str] = []
    for line in (completed.stdout or "").splitlines():
        marker = "[dep-health] "
        if line.startswith(marker):
            message = line[len(marker) :].strip()
            if message and message not in {"FAILED"}:
                failures.append(message)
    if not failures:
        stderr = (completed.stderr or "").strip()
        if stderr:
            failures.append(stderr.splitlines()[-1])
    return False, failures


def _is_cuda_only_failure(failures: list[str]) -> bool:
    """True iff ``failures`` is non-empty AND every entry is a
    ``torch_cuda_failure:`` (i.e. the GUI-launchable partial-success
    state — face stack healthy, only CUDA broken).
    """
    return bool(failures) and all(
        f.startswith("torch_cuda_failure:") for f in failures
    )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Validate/repair GUI runtime dependencies.")
    parser.add_argument("--mode", choices=("check", "repair"), default="check")
    args = parser.parse_args(argv)

    ok, failures = check_runtime_dependencies()
    if args.mode == "check":
        if ok:
            print("[dep-health] OK")
            return 0
        # Codex PR #55 round-7 P2 (#PRRT_kwDOSQUnmM6FQIkt): when ONLY
        # `torch_cuda_failure:*` is present, the GUI is still launchable
        # (face stack healthy, app's prod code doesn't use torch.cuda.*).
        # The previous form returned 1 here, which made BOTH launchers
        # re-enter `--mode repair` on EVERY launch — force-reinstalling
        # the face stack (~50MB pip churn) every time. For users whose
        # CPU-torch fallback can't reach download.pytorch.org (corporate
        # firewall, ISP block), this was a per-launch wait with no
        # forward progress.
        #
        # Return 0 with explicit WARN lines so the launcher's health
        # probe accepts the state without triggering repair. Re-detection
        # still works: if CUDA gets fixed, the next check returns clean
        # `[dep-health] OK` + exit 0; if a NEW non-CUDA failure appears,
        # `_is_cuda_only_failure` returns False and we exit 1 properly.
        # RetinaFace runtime probe still runs under CUDA-only failure
        # (round-7 fix), so face-stack breakage IS detected here.
        if _is_cuda_only_failure(failures):
            print("[dep-health] WARN: torch CUDA broken; face stack OK; GUI launchable on CPU")
            for failure in failures:
                print(f"[dep-health] WARN: {failure}")
            return 0
        print("[dep-health] FAILED")
        for failure in failures:
            print(f"[dep-health] {failure}")
        return 1

    # repair mode
    if ok:
        print("[dep-health] Already healthy")
        return 0

    print("[dep-health] Initial check failed:")
    for failure in failures:
        print(f"[dep-health] {failure}")

    repaired, message = run_repair(failures=failures)
    print(f"[dep-health] {message}")

    # Subagent PR #55 round 5 HIGH: Codex P2 made `run_repair` continue the
    # face-stack repair even when the CPU-torch fallback fails. But the old
    # `if not repaired: return 1` collapsed the partial-success case back to
    # a binary failure — the launcher's "REPAIR FAILED" branch would then
    # exit before launching the GUI, even though face_crop / video would
    # work fine on the now-repaired face stack (and the app doesn't use
    # torch.cuda.* in production).
    #
    # Always verify in a fresh process. If the fresh probe comes back clean
    # OR clean-except-for-`torch_cuda_failure:`-prefixed failures, the GUI
    # is launchable — exit 0 with a warning. The launcher's diagnostic log
    # captures the combined repair message either way.
    ok_after, failures_after = verify_in_fresh_process()
    if ok_after:
        print("[dep-health] Repair verification passed")
        return 0

    # Partial success — face stack is now healthy but torch CUDA still
    # broken (probably because download.pytorch.org was blocked/flaky).
    non_cuda_failures = [
        f for f in failures_after if not f.startswith("torch_cuda_failure:")
    ]
    if not non_cuda_failures:
        print(
            "[dep-health] Repair verification: face stack healthy; "
            "torch CUDA still broken (CPU fallback could not reach "
            "download.pytorch.org). GUI is launchable — face_crop and "
            "video paths work fine on CPU."
        )
        for failure in failures_after:
            print(f"[dep-health] WARN: {failure}")
        return 0

    print("[dep-health] Repair verification failed:")
    for failure in failures_after:
        print(f"[dep-health] {failure}")
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
