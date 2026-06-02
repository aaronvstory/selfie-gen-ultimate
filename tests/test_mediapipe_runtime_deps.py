"""v2.17: the test class that WOULD HAVE CAUGHT the recurring Windows
rPPG-failure bug before shipping.

Root cause (failed the friend across v2.13/v2.15/v2.16): mediapipe is installed
with --no-deps (to keep pip from pulling numpy 2.x), so its RUNTIME deps are
absent. `mediapipe.tasks.python.vision` (the FaceLandmarker the rPPG injector +
oldcam use) imports matplotlib AT MODULE LOAD and uses opencv-contrib-python +
sounddevice. A bare `import mediapipe` PASSED, so every install gate thought
mediapipe was fine — then the real import crashed with "No module named
'matplotlib'" and rPPG fell back to -NORPPG on EVERY run.

Why TDD missed it: every prior dep test was source-text / AST / monkeypatched —
NONE imported the deep Tasks-API symbol in a real venv. These two layers fix
that:

1. Source-text guard (always runs, fast): every install site that installs
   mediapipe --no-deps MUST also install matplotlib/opencv-contrib/sounddevice.
   This catches a regression the instant someone adds a new --no-deps site
   without the runtime deps.
2. Real-import probe (env-gated RUN_MEDIAPIPE_IMPORT_TEST=1): actually imports
   `from mediapipe.tasks.python import vision; vision.FaceLandmarker` in the
   current interpreter — the exact import that crashed. Run in CI / pre-ship
   against the real venv.
"""
from __future__ import annotations

import glob
import os
import re
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent

# The three runtime deps mediapipe.tasks needs that --no-deps skips. setup_macos.sh
# has always installed these; the Windows side did not (the bug).
_MP_RUNTIME = ("matplotlib", "opencv-contrib-python", "sounddevice")


def _read(rel: str) -> str:
    return (REPO_ROOT / rel).read_text(encoding="utf-8", errors="replace")


# --- Layer 1: source-text guards (always on) --------------------------------

# Every install site that does a mediapipe --no-deps install. If a NEW site is
# added it must be added here too (and must install the runtime deps).
_MEDIAPIPE_NODEPS_SITES = [
    "launchers/windows/run_gui.bat",
    "launchers/windows/run_cli.bat",
    "rPPG/run_rppg.bat",
    "setup_macos.sh",
    "build_gui_exe.bat",
    # oldcam v9/v10/v11 use the mediapipe FaceLandmarker; v7/v8/v12+ do not
    # install mediapipe --no-deps (no landmark feature or different stack).
    "oldcam-v9/oldcam_launcher.bat",
    "oldcam-v10/oldcam_launcher.bat",
    "oldcam-v11/oldcam_launcher.bat",
    "oldcam-v9/macOS/oldcam.command",
    "oldcam-v10/macOS/oldcam.command",
    "oldcam-v11/macOS/oldcam.command",
]


@pytest.mark.parametrize("site", _MEDIAPIPE_NODEPS_SITES)
def test_mediapipe_nodeps_site_also_installs_runtime_deps(site):
    """Any launcher/script that installs mediapipe --no-deps MUST also install
    matplotlib + opencv-contrib-python + sounddevice, or mediapipe.tasks crashes
    at runtime (the recurring -NORPPG bug)."""
    src = _read(site)
    assert "--no-deps" in src and "mediapipe" in src, (
        f"{site}: expected a mediapipe --no-deps install here"
    )
    for dep in _MP_RUNTIME:
        assert dep in src, (
            f"{site}: installs mediapipe --no-deps but never installs {dep!r} — "
            f"mediapipe.tasks.python.vision will crash at import (the -NORPPG bug)."
        )


def test_run_repair_installs_mediapipe_runtime_deps():
    """dependency_health_check.run_repair (the in-app + launcher repair path)
    must install the mediapipe runtime deps so repairing a broken venv actually
    fixes rPPG, not just re-installs the bare --no-deps wheel."""
    src = _read("dependency_health_check.py")
    for dep in _MP_RUNTIME:
        assert dep in src, f"run_repair must install {dep!r} for mediapipe.tasks"


def test_health_probe_uses_deep_mediapipe_import():
    """check_runtime_dependencies must probe the DEEP Tasks-API symbol, not a
    bare `import mediapipe` (which passes even when matplotlib is missing)."""
    src = _read("dependency_health_check.py")
    assert "mediapipe.tasks.python.vision" in src
    assert "FaceLandmarker" in src


def test_rppg_launcher_deep_gate_and_runtime_deps():
    """run_rppg.bat: deep Tasks-API import gate + installs the runtime deps in
    its self-heal sync."""
    src = _read("rPPG/run_rppg.bat")
    assert "from mediapipe.tasks.python" in src and "FaceLandmarker" in src, (
        "run_rppg.bat must deep-probe the mediapipe Tasks API"
    )
    for dep in _MP_RUNTIME:
        assert dep in src, f"run_rppg.bat self-heal must install {dep!r}"


def test_no_other_nodeps_mediapipe_site_is_unguarded():
    """Discovery guard: scan ALL launchers/scripts for a mediapipe --no-deps
    install and fail if one exists that this test doesn't already cover (so a
    future un-guarded site can't silently reintroduce the bug)."""
    patterns = ["**/*.bat", "**/*.sh", "**/*.command"]
    nodeps_re = re.compile(r"--no-deps", re.I)
    covered = {str((REPO_ROOT / s).resolve()) for s in _MEDIAPIPE_NODEPS_SITES}
    offenders = []
    # Skip any virtualenv dir — bundled launcher scripts inside site-packages
    # are not ours. Match venv/.venv/.venv311/.venv-macos etc. (code-review:
    # the prior "/venv/" check missed the common .venv* names).
    def _in_venv(path: str) -> bool:
        # Match a path SEGMENT that is a virtualenv dir. Only the canonical
        # names + the .venv* family — NOT a bare "venv-*" prefix, which could
        # be the repo root itself if cloned into e.g. "venv-kling" (Gemini
        # 2026-06-03). A real venv dir is "venv" or ".venv"/".venv311"/etc.
        norm = path.replace("\\", "/")
        return any(
            seg == "venv" or seg == ".venv" or seg.startswith(".venv")
            for seg in norm.split("/")
        )

    for pat in patterns:
        for f in glob.glob(str(REPO_ROOT / pat), recursive=True):
            if ".launcher_state" in f or _in_venv(f):
                continue
            try:
                txt = Path(f).read_text(encoding="utf-8", errors="replace")
            except OSError:
                continue
            if "mediapipe" in txt and nodeps_re.search(txt):
                rf = str(Path(f).resolve())
                if rf in covered:
                    continue
                if not all(dep in txt for dep in _MP_RUNTIME):
                    offenders.append(f)
    assert not offenders, (
        "These files install mediapipe --no-deps without the matplotlib/"
        f"opencv-contrib/sounddevice runtime deps (rPPG -NORPPG bug): {offenders}"
    )


# --- Layer 2: real-import probe (env-gated) ---------------------------------

@pytest.mark.skipif(
    os.environ.get("RUN_MEDIAPIPE_IMPORT_TEST") != "1",
    reason="set RUN_MEDIAPIPE_IMPORT_TEST=1 to run the real mediapipe.tasks import probe",
)
def test_real_mediapipe_tasks_import():
    """The EXACT import that crashed the friend. Env-gated because it needs the
    full venv. Run pre-ship: RUN_MEDIAPIPE_IMPORT_TEST=1 <venv>/python -m pytest
    tests/test_mediapipe_runtime_deps.py -k real_mediapipe -q"""
    from mediapipe.tasks.python import vision  # noqa: F401

    assert vision.FaceLandmarker is not None


def test_rppg_injector_registers_cuda_dll_dirs_and_force_cpu():
    """v2.17: the injector must (a) register the pip-installed NVIDIA CUDA DLL
    dirs before importing cupy (os.add_dll_directory + PATH) so nvrtc loads and
    the GPU kernel compiles — without this, CuPy imports but the first kernel
    compile fails 'Could not find nvrtc64_*.dll' and rPPG silently falls to CPU
    even on a good GPU box (verified RTX 4090 2026-06-03); and (b) honour
    RPPG_FORCE_CPU=1 as a benchmarking/escape-hatch."""
    src = (REPO_ROOT / "rPPG" / "rppg_injector.py").read_text(encoding="utf-8", errors="replace")
    assert "_register_cuda_dll_dirs" in src
    assert "add_dll_directory" in src
    assert 'os.environ["PATH"]' in src or "os.environ['PATH']" in src
    assert "RPPG_FORCE_CPU" in src
    # The helper must run BEFORE `import cupy`.
    assert src.index("_register_cuda_dll_dirs()") < src.index("import cupy as _cp")


@pytest.mark.parametrize("v", ["v9", "v10", "v11"])
def test_oldcam_cached_stamp_probe_deep_validates_mediapipe(v):
    """Codex P2 (2026-06-03): the oldcam v9/v10/v11 cached-stamp skip probe must
    deep-validate the mediapipe Tasks API (MP_VALIDATE_CMD / FaceLandmarker),
    not just `import cv2, numpy, mediapipe` — a bare import passes even when
    matplotlib (a mediapipe.tasks runtime dep) is missing, so a stale stamp
    would skip the sync and Oldcam then crashes on FaceLandmarker."""
    src = _read(f"oldcam-{v}/oldcam_launcher.bat")
    # The cached-stamp block must run MP_VALIDATE_CMD before clearing NEED_PIP.
    assert "%MP_VALIDATE_CMD%" in src
    # And must NOT trust a bare `import cv2, numpy, mediapipe` alone as the gate.
    assert 'if not errorlevel 1 "%PYTHON_CMD%" -c "%MP_VALIDATE_CMD%"' in src
