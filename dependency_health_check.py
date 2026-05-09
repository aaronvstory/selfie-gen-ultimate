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


def _default_retinaface_runtime_probe() -> tuple[object | None, str]:
    from kling_gui.tabs.face_crop_tab import _load_retinaface

    return _load_retinaface()


def check_runtime_dependencies(
    importer: ImportFn = importlib.import_module,
    runtime_probe: RuntimeProbeFn = _default_retinaface_runtime_probe,
) -> tuple[bool, list[str]]:
    """Validate the runtime face-stack imports used by the GUI."""
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

    if not failures:
        try:
            retinaface_cls, retinaface_error = runtime_probe()
            if retinaface_cls is None:
                failures.append(f"retinaface runtime loader failed: {retinaface_error}")
        except Exception as exc:
            failures.append(f"retinaface runtime probe failed: {type(exc).__name__}: {exc}")

    return len(failures) == 0, failures


def run_repair() -> tuple[bool, str]:
    """Attempt deterministic repair of the face dependency stack."""
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
        return True, "repair install completed"

    stderr = (completed.stderr or "").strip()
    stdout = (completed.stdout or "").strip()
    details = stderr if stderr else stdout
    if details:
        details = details.splitlines()[-1]
    return False, f"repair failed (code {completed.returncode}): {details}"


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

    repaired, message = run_repair()
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
