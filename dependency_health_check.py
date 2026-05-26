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
    "nvrtc",
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

    # Torch probe: import + force backend init via a tiny op so a broken
    # CUDA wheel surfaces here (a successful `import torch` doesn't always
    # touch CUDA; the trap is delayed until first op or `.cuda()` call).
    # If import is fine but the CUDA sub-stack is broken, we can transparently
    # fall back to CPU-only torch in `run_repair` — the app doesn't use
    # `torch.cuda.*` (verified by ripgrep on the production tree).
    try:
        torch_module = importer("torch")
    except Exception as exc:
        sig = _torch_cuda_load_failure_signature(exc)
        if sig:
            failures.append(f"torch_cuda_failure:{sig}: {type(exc).__name__}: {exc}")
        else:
            failures.append(f"torch import failed: {type(exc).__name__}: {exc}")
    else:
        # `torch.zeros(1)` forces eager initialization. On a broken CUDA
        # install this is where the CUDART / cuDNN DLL load fails. CPU-only
        # wheels handle this op trivially.
        try:
            torch_module.zeros(1)
        except Exception as exc:
            sig = _torch_cuda_load_failure_signature(exc)
            if sig:
                failures.append(f"torch_cuda_failure:{sig}: {type(exc).__name__}: {exc}")
            else:
                failures.append(f"torch runtime probe failed: {type(exc).__name__}: {exc}")

    if not failures:
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


def run_torch_cpu_fallback() -> tuple[bool, str]:
    """Force-reinstall torch from the CPU-only wheel index.

    Returns ``(success, message)``. Same shape as ``run_repair``. Called by
    ``run_repair`` when ``check_runtime_dependencies`` flagged a CUDA load
    failure, so users on broken-CUDA Windows nvidia setups end up with a
    working CPU-only torch instead of the launcher dead-ending.
    """
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
        "torch",
    ]
    completed = subprocess.run(cmd, capture_output=True, text=True, check=False)
    if completed.returncode == 0:
        return True, "torch CPU-only fallback install completed"
    stderr = (completed.stderr or "").strip()
    stdout = (completed.stdout or "").strip()
    details = stderr if stderr else stdout
    if details:
        details = details.splitlines()[-1]
    return False, f"torch CPU fallback failed (code {completed.returncode}): {details}"


def run_repair(failures: list[str] | None = None) -> tuple[bool, str]:
    """Attempt deterministic repair of the face dependency stack.

    If ``failures`` is provided and contains any ``torch_cuda_failure:`` entry,
    the CPU-only torch fallback runs FIRST (cheap, ~50MB) so the subsequent
    face-stack reinstall doesn't pull TF wheels into a broken CUDA torch
    environment. Without the failures list, behaves identically to prior
    versions (face-stack-only repair) for back-compat with any external
    caller that invokes ``run_repair()`` with no args.
    """
    messages: list[str] = []

    if failures and _failures_indicate_torch_cuda_break(failures):
        cuda_ok, cuda_msg = run_torch_cpu_fallback()
        messages.append(cuda_msg)
        if not cuda_ok:
            # CUDA fallback failed — bubble up so the launcher emits an
            # actionable error rather than running TF reinstall on top of
            # a still-broken torch.
            return False, "; ".join(messages)

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
        return True, "; ".join(messages)

    stderr = (completed.stderr or "").strip()
    stdout = (completed.stdout or "").strip()
    details = stderr if stderr else stdout
    if details:
        details = details.splitlines()[-1]
    messages.append(f"repair failed (code {completed.returncode}): {details}")
    return False, "; ".join(messages)


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


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Validate/repair GUI runtime dependencies.")
    parser.add_argument("--mode", choices=("check", "repair"), default="check")
    args = parser.parse_args(argv)

    ok, failures = check_runtime_dependencies()
    if args.mode == "check":
        if ok:
            print("[dep-health] OK")
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
    if not repaired:
        return 1

    ok_after, failures_after = verify_in_fresh_process()
    if ok_after:
        print("[dep-health] Repair verification passed")
        return 0

    print("[dep-health] Repair verification failed:")
    for failure in failures_after:
        print(f"[dep-health] {failure}")
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
