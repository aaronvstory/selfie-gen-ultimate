"""Frozen-build bridge to the first-run ML side venv.

The bundled Windows .exe (built via distribution/kling_gui_bundled.spec)
deliberately ships WITHOUT the heavy ML stack (torch / tensorflow / mediapipe /
deepface / retinaface / opencv). Those are ~4-8GB and their native DLLs are
fragile under PyInstaller. Instead they are installed ONCE into a side venv on
first use of a feature that needs them, and that feature runs as a SUBPROCESS
using the side venv's python.exe — so the frozen CPython never imports a
pip-installed torch (the ABI/DLL hell we're avoiding).

This module is import-safe everywhere (no heavy imports at module load) and is
a no-op when NOT frozen (the dev/source install already has a full venv with
the ML stack, so the in-process import path in face_crop_tab / face_similarity
works as before).

Layout of the side venv (Windows only — the bundled exe is Windows-only):

    %LocalAppData%\\selfie-gen-ultimate\\ml-venv\\Scripts\\python.exe

Public API:
    is_frozen_bundle() -> bool
    ml_venv_dir() -> Path
    ml_venv_python() -> Path
    ml_stack_ready() -> bool
    ensure_ml_stack(log=print) -> bool      # resolve python + create venv + pip install
    run_in_ml_venv(args, **kw) -> CompletedProcess
"""
from __future__ import annotations

import os
import subprocess
from pathlib import Path
from typing import Callable, List, Optional, Sequence

import path_utils

# The packages the side venv must provide. Kept in sync with the heavy half of
# requirements.txt; the resolver/pip install pulls exact pins from
# requirements.txt at install time, this list is only the readiness probe.
_ML_PROBE_IMPORTS = ("cv2", "numpy", "retinaface", "deepface")

_ML_VENV_DIRNAME = "ml-venv"


def is_frozen_bundle() -> bool:
    """True only in the PyInstaller bundle. Source/dev installs return False
    so all the existing in-process ML import paths stay unchanged."""
    return bool(path_utils.is_frozen())


def ml_venv_dir() -> Path:
    """Side-venv root under the user data dir (per-user, survives app updates)."""
    return Path(path_utils.get_user_data_dir()) / _ML_VENV_DIRNAME


def ml_venv_python() -> Path:
    """Path to the side venv's python.exe (Windows layout)."""
    return ml_venv_dir() / "Scripts" / "python.exe"


def ml_stack_ready() -> bool:
    """True if the side venv exists AND can import the ML probe modules.

    Spawns the side-venv python once to import the probe set (up to 120s). If
    the venv is absent we short-circuit without spawning.

    THREADING: this can block for up to 120s on a cold/slow disk. NEVER call it
    on the Tkinter main thread — run it in a worker thread (the GUI tabs already
    do their ML work off the main thread) or it will freeze the UI.
    """
    py = ml_venv_python()
    if not py.exists():
        return False
    probe = "import " + ", ".join(_ML_PROBE_IMPORTS)
    try:
        rc = subprocess.run(
            [str(py), "-c", probe],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            timeout=120,
        ).returncode
    except (OSError, subprocess.SubprocessError):
        return False
    return rc == 0


def _bundled(*parts: str) -> Path:
    """Path to a file bundled into the exe (_MEIPASS at runtime)."""
    return Path(path_utils.get_resource_dir()).joinpath(*parts)


def _write_filtered_requirements(req: Path, exclude_prefix: str) -> Path:
    """Write a temp requirements file dropping lines starting with
    ``exclude_prefix`` (case-insensitive). Used to install everything EXCEPT
    mediapipe first, so mediapipe can go in separately with --no-deps."""
    import tempfile

    out_lines = []
    for line in req.read_text(encoding="utf-8").splitlines():
        if line.strip().lower().startswith(exclude_prefix.lower()):
            continue
        out_lines.append(line)
    fd, tmp = tempfile.mkstemp(suffix=".txt", prefix="ml_req_")
    with os.fdopen(fd, "w", encoding="utf-8") as fh:
        fh.write("\n".join(out_lines) + "\n")
    return Path(tmp)


def _resolve_base_python(log: Callable[[str], None]) -> Optional[str]:
    """Find a supported (3.9-3.12) Python to BUILD the side venv from.

    Reuses the shared resolver's detection philosophy. We can't `call` the .bat
    and read its env back from a child process, so we replicate just the
    detection half here in Python: try the `py` launcher (3.11/3.12/3.10/3.9),
    then `python` on PATH, each version-gated. If none is found we shell out to
    the bundled resolver .bat which ALSO knows how to auto-install Python 3.12
    (winget -> python.org); after it runs we re-probe the py launcher.
    """
    gate = (
        "import sys; raise SystemExit("
        "0 if (3,9) <= sys.version_info[:2] < (3,13) else 2)"
    )

    def _ok(cmd: List[str]) -> bool:
        try:
            return subprocess.run(
                cmd + ["-c", gate],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                timeout=30,
            ).returncode == 0
        except (OSError, subprocess.SubprocessError):
            return False

    for ver in ("3.11", "3.12", "3.10", "3.9"):
        if _ok(["py", f"-{ver}"]):
            log(f"Using py -{ver} to build the ML environment.")
            return f"py -{ver}"
    if _ok(["python"]):
        log("Using python on PATH to build the ML environment.")
        return "python"

    # Nothing found — let the bundled resolver auto-install Python 3.12, then
    # re-probe. The resolver creates a repo venv we don't use here, but its
    # install side-effect (winget / python.org) is what we want.
    resolver = _bundled("scripts", "win_resolve_python.bat")
    if resolver.exists():
        log("No supported Python found — running auto-install (this is one-time)...")
        try:
            # Minimal env contract the resolver needs; point its throwaway venv
            # at a temp dir so it doesn't collide with our side venv.
            env = dict(os.environ)
            tmp_root = str(ml_venv_dir().parent / "_resolver_tmp")
            env.setdefault("ROOT_DIR", tmp_root)
            env.setdefault("VENV_DIR", os.path.join(tmp_root, "venv"))
            env.setdefault("VENV_PYTHON", os.path.join(tmp_root, "venv", "Scripts", "python.exe"))
            env.setdefault("STATE_DIR", os.path.join(tmp_root, ".launcher_state"))
            env.setdefault("LOG_FILE", os.path.join(tmp_root, "resolver.log"))
            env.setdefault("LAUNCH_TS", "ml-bridge")
            os.makedirs(env["STATE_DIR"], exist_ok=True)
            # Non-zero exit is deliberately tolerated: the resolver may fail to
            # create its throwaway repo venv yet still have auto-installed
            # Python (winget/python.org), which is all we want here. We re-probe
            # the py launcher below to decide success.
            subprocess.run(["cmd", "/c", str(resolver)], env=env, timeout=900)
        except (OSError, subprocess.SubprocessError) as exc:
            log(f"Auto-install step failed: {exc}")
        finally:
            # The resolver creates a throwaway venv at tmp_root/venv that we
            # never use (we build our own ml-venv). Remove it so it doesn't
            # linger on disk (code-review H2).
            import shutil
            shutil.rmtree(tmp_root, ignore_errors=True)
    for ver in ("3.12", "3.11"):
        if _ok(["py", f"-{ver}"]):
            log(f"Python {ver} now available via the py launcher.")
            return f"py -{ver}"
    return None


def ensure_ml_stack(log: Callable[[str], None] = print) -> bool:
    """Create the side venv (if needed) and pip-install the ML requirements.

    Idempotent: returns True immediately if the stack is already importable.
    Returns False (without raising) if it cannot complete, so callers can show
    a friendly message and keep the rest of the GUI usable.
    """
    if not is_frozen_bundle():
        # Source install: the project's own venv already has the ML stack.
        return True
    if ml_stack_ready():
        return True

    venv = ml_venv_dir()
    py = ml_venv_python()
    if not py.exists():
        base = _resolve_base_python(log)
        if not base:
            log("Could not find or install a supported Python (3.9-3.12).")
            return False
        log("Creating the one-time ML environment (this can take a few minutes)...")
        try:
            # base is "py -3.x" (two tokens) or "python" (one token).
            create_cmd = base.split() + ["-m", "venv", str(venv)]
            subprocess.run(create_cmd, timeout=300, check=True)
        except (OSError, subprocess.SubprocessError) as exc:
            log(f"Failed to create the ML environment: {exc}")
            return False

    req = _bundled("requirements.txt")
    if not req.exists():
        log("Bundled requirements.txt missing — cannot install the ML stack.")
        return False

    log("Installing the ML dependencies (torch, tensorflow, deepface, ...).")
    log("First run only — expect 5-15 minutes depending on your connection.")
    try:
        subprocess.run([str(py), "-m", "pip", "install", "--upgrade", "pip"],
                       timeout=300)
        # mediapipe MUST be installed with --no-deps, exactly like the launchers
        # (docs/windows-launcher-and-sash-rules.md rule 6). Letting pip resolve
        # mediapipe's deps conflicts with the pinned TensorFlow protobuf range
        # and breaks the install (code-review H3). So: (1) install everything
        # EXCEPT mediapipe from a filtered requirements file, then (2) install
        # mediapipe pinned with --no-deps.
        filtered = _write_filtered_requirements(req, exclude_prefix="mediapipe")
        try:
            rc = subprocess.run(
                [str(py), "-m", "pip", "install", "--only-binary", ":all:",
                 "-r", str(filtered)],
                timeout=3600,
            ).returncode
            if rc != 0:
                log("Binary-only install failed; retrying without that constraint.")
                rc = subprocess.run(
                    [str(py), "-m", "pip", "install", "-r", str(filtered)],
                    timeout=3600,
                ).returncode
        finally:
            try:
                os.remove(filtered)
            except OSError:
                pass
        # mediapipe separately, --no-deps, pinned to match the launchers.
        subprocess.run(
            [str(py), "-m", "pip", "install", "--no-deps", "mediapipe==0.10.35"],
            timeout=1800,
        )
        if rc != 0:
            log("pip install reported errors — retrying core face stack pinned.")
            subprocess.run(
                [str(py), "-m", "pip", "install", "--no-cache-dir",
                 "tensorflow==2.16.2", "protobuf==4.25.3", "tf-keras==2.16.0",
                 "retina-face==0.0.17", "deepface==0.0.92", "opencv-python"],
                timeout=3600,
            )
    except (OSError, subprocess.SubprocessError) as exc:
        log(f"ML dependency install failed: {exc}")
        return False

    ok = ml_stack_ready()
    log("ML environment ready." if ok else "ML environment still incomplete.")
    return ok


def _run_json(args: Sequence[str], log: Callable[[str], None]) -> Optional[dict]:
    """Ensure the ML stack, run the JSON runner in the side venv, parse the
    single JSON line it prints. Returns the parsed dict, or None if the stack
    isn't available / the subprocess produced no parseable JSON.

    The JSON runner writes exactly one JSON line to stdout (progress -> stderr),
    so we parse the LAST non-empty stdout line defensively.
    """
    import json as _json

    if not ensure_ml_stack(log=log):
        log("ML environment is not available; cannot run this feature.")
        return None
    try:
        cp = run_in_ml_venv(
            ["-m", "tools.ml_json_runner", *args],
            cwd=path_utils.get_resource_dir(),
        )
    except (FileNotFoundError, OSError, subprocess.SubprocessError) as exc:
        log(f"ML subprocess failed to start: {exc}")
        return None
    out = (cp.stdout or "").strip()
    if not out:
        log(f"ML subprocess produced no output (exit {cp.returncode}). "
            f"stderr tail: {(cp.stderr or '')[-200:]}")
        return None
    last = out.splitlines()[-1]
    try:
        return _json.loads(last)
    except ValueError:
        log(f"ML subprocess output was not JSON: {last[:200]!r}")
        return None


def run_crop_json(
    input_path: str,
    output_path: str,
    multiplier: float = 1.5,
    log: Callable[[str], None] = print,
) -> Optional[dict]:
    """Frozen-mode headless face crop via the side venv."""
    return _run_json(
        ["crop", "--input", input_path, "--output", output_path,
         "--multiplier", str(multiplier)],
        log,
    )


def run_similarity_json(
    ref_path: str,
    target_path: str,
    log: Callable[[str], None] = print,
) -> Optional[dict]:
    """Frozen-mode headless face similarity via the side venv. Returns the same
    dict shape as face_similarity.compute_face_similarity_details, or None."""
    return _run_json(
        ["similarity", "--ref", ref_path, "--target", target_path],
        log,
    )


def run_in_ml_venv(
    args: Sequence[str],
    *,
    timeout: Optional[float] = None,
    capture_output: bool = True,
    cwd: Optional[str] = None,
) -> subprocess.CompletedProcess:
    """Run `args` (a python-module/script arg list) using the side-venv python.

    Example:
        run_in_ml_venv(["-m", "similarity.src.cli", "--a", a, "--b", b])

    Raises FileNotFoundError if the side venv python is absent (caller should
    ensure_ml_stack() first).
    """
    py = ml_venv_python()
    if not py.exists():
        raise FileNotFoundError(f"ML side-venv python not found: {py}")
    return subprocess.run(
        [str(py), *args],
        timeout=timeout,
        capture_output=capture_output,
        text=True,
        cwd=cwd,
    )
