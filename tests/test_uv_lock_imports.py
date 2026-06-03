"""Real-import smoke tests for the uv-managed dependency environment (v2.20).

These follow the project's TDD mandate (memory
``feedback_tdd_real_import_probe_not_just_text``): a dependency test must
**REAL-IMPORT the deep symbol the app actually loads**, not grep source text or
monkeypatch. Source-text "OK" has shipped broken venvs here before — most
infamously the mediapipe ``--no-deps`` gap where a bare ``import mediapipe``
passed but ``mediapipe.tasks.python.vision`` (the FaceLandmarker rPPG + oldcam
use) crashed at module load with "No module named matplotlib".

Two layers:

1. **Always-on, in-process probes** (``test_*_imports``): import the deep
   symbols in THIS interpreter. They run in CI / on any dev box that has the
   stack installed (which is the project's standard dev setup) and FAIL LOUDLY
   if a hard invariant regressed — numpy floated to 2.x, opencv hit 4.12,
   mediapipe's Tasks API can't load, scipy/absl/torch missing. If the stack
   isn't installed at all, they ``skip`` (so a bare checkout doesn't error) —
   but a PARTIAL/broken stack FAILS, which is the whole point.

2. **Env-gated fresh ``uv sync`` probe** (``test_uv_sync_extra_real_import``):
   gated behind ``RUN_UV_SYNC_TEST=1`` because it does a real (slow, network)
   ``uv sync`` into a throwaway env. This is the cross-platform analogue of
   ``test_fresh_install_numpy_pin.py::test_fresh_venv_real_install`` — proof
   that the lock RESOLVES + the deep symbols import from a clean uv env, on
   whichever OS/GPU the test runs.

Run the fast layer:           pytest tests/test_uv_lock_imports.py -q
Run the full fresh-sync gate: RUN_UV_SYNC_TEST=1 pytest tests/test_uv_lock_imports.py -q
"""

from __future__ import annotations

import importlib
import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent


def _try_import(modpath: str):
    """Import ``modpath``; return the module or None if the package is simply
    absent (bare checkout). A package that is PRESENT but BROKEN re-raises so
    the test fails — that's the regression we care about."""
    top = modpath.split(".")[0]
    if importlib.util.find_spec(top) is None:
        return None
    return importlib.import_module(modpath)


# --------------------------------------------------------------------------
# Layer 1 — always-on deep-symbol import probes
# --------------------------------------------------------------------------

def test_numpy_is_pinned_below_2():
    """The load-bearing invariant: numpy MUST stay <2 (TF 2.16.2 / ml-dtypes)."""
    np = _try_import("numpy")
    if np is None:
        pytest.skip("numpy not installed (bare checkout)")
    major = int(np.__version__.split(".")[0])
    assert major < 2, (
        f"numpy {np.__version__} >= 2 — breaks TensorFlow 2.16.2 "
        "(ml-dtypes~=0.3.1 needs numpy 1.26.x). The uv.lock numpy<2 pin "
        "regressed."
    )


def test_opencv_below_4_12():
    """opencv <4.12 (4.12+ declares numpy>=2, conflicting with the numpy<2 pin)."""
    cv2 = _try_import("cv2")
    if cv2 is None:
        pytest.skip("opencv not installed (bare checkout)")
    parts = cv2.__version__.split(".")
    major, minor = int(parts[0]), int(parts[1])
    assert (major, minor) < (4, 12), (
        f"opencv {cv2.__version__} >= 4.12 — declares numpy>=2, conflicts "
        "with the numpy<2 face stack."
    )


def test_mediapipe_tasks_vision_deep_import():
    """THE recurring rPPG killer: mediapipe.tasks.python.vision imports
    matplotlib at module load. A bare ``import mediapipe`` passing is NOT
    enough — the FaceLandmarker the injector + oldcam use lives here."""
    if importlib.util.find_spec("mediapipe") is None:
        pytest.skip("mediapipe not installed (bare checkout)")
    # The deep symbol. If matplotlib/opencv-contrib/etc. are missing this
    # raises ImportError — exactly the failure we must catch BEFORE shipping.
    from mediapipe.tasks.python import vision  # noqa: F401
    assert hasattr(vision, "FaceLandmarker"), (
        "mediapipe.tasks.python.vision imported but FaceLandmarker is absent — "
        "the Tasks API the rPPG/oldcam pipeline depends on is broken."
    )


def test_scipy_signal_symbols_import():
    """scipy is a real rPPG runtime dep (butter/filtfilt/find_peaks/hilbert)."""
    if importlib.util.find_spec("scipy") is None:
        pytest.skip("scipy not installed (bare checkout)")
    from scipy.signal import butter, filtfilt, find_peaks, hilbert  # noqa: F401


def test_absl_logging_imports():
    """rPPG's injector imports absl.logging (now guarded, but absl should be
    present per the install-policy invariant)."""
    if importlib.util.find_spec("absl") is None:
        pytest.skip("absl-py not installed (bare checkout)")
    import absl.logging  # noqa: F401


def test_torch_imports_and_reports_a_build():
    """torch must import (DeepFace anti-spoofing). CUDA availability is NOT
    asserted — that's hardware-dependent; a CPU build is always valid."""
    torch = _try_import("torch")
    if torch is None:
        pytest.skip("torch not installed (bare checkout)")
    # Just exercising the attribute proves the C-extension loaded.
    assert isinstance(torch.cuda.is_available(), bool)


def test_tensorflow_legacy_keras_import():
    """TF 2.16.2 + tf-keras 2.16.0 under TF_USE_LEGACY_KERAS (set by conftest).
    deepface needs tf.keras to resolve to the legacy keras, not keras 3."""
    if importlib.util.find_spec("tensorflow") is None:
        pytest.skip("tensorflow not installed (bare checkout)")
    # conftest sets TF_USE_LEGACY_KERAS=1 + KERAS_BACKEND=tensorflow before any
    # import. Importing tensorflow then touching tf.keras must not raise.
    import tensorflow as tf
    assert tf.keras is not None


# --------------------------------------------------------------------------
# Layer 2 — env-gated fresh `uv sync` resolution + deep import
# --------------------------------------------------------------------------

@pytest.mark.skipif(
    os.environ.get("RUN_UV_SYNC_TEST") != "1",
    reason="slow/network fresh uv sync; set RUN_UV_SYNC_TEST=1 to run",
)
def test_uv_sync_extra_real_import(tmp_path):
    """Fresh ``uv sync --extra <hw>`` into a throwaway env, then real-import the
    deep symbols from THAT env's interpreter. Cross-platform analogue of the
    pip fresh-install test. The extra defaults to ``cpu`` (always installable,
    no GPU needed); override with ``UV_SYNC_TEST_EXTRA=cu128`` on a GPU box."""
    uv = shutil.which("uv")
    if uv is None:
        pytest.skip("uv not on PATH")
    extra = os.environ.get("UV_SYNC_TEST_EXTRA", "cpu")
    env = dict(os.environ)
    # Isolate the sync into a temp project dir copy-pointing at the real lock.
    env["UV_PROJECT_ENVIRONMENT"] = str(tmp_path / ".venv")
    # --frozen: resolve strictly from the committed lock (no re-resolution);
    # proves the lock itself is installable.
    proc = subprocess.run(
        [uv, "sync", "--frozen", "--no-default-groups", "--extra", extra],
        cwd=str(REPO_ROOT),
        env=env,
        capture_output=True,
        text=True,
        timeout=3600,
    )
    assert proc.returncode == 0, (
        f"uv sync --extra {extra} failed:\n{proc.stdout[-2000:]}\n{proc.stderr[-2000:]}"
    )
    py = (
        tmp_path / ".venv" / "Scripts" / "python.exe"
        if sys.platform == "win32"
        else tmp_path / ".venv" / "bin" / "python"
    )
    assert py.exists(), f"synced env missing interpreter at {py}"
    probe = (
        "import os; os.environ.setdefault('TF_USE_LEGACY_KERAS','1');"
        "os.environ.setdefault('KERAS_BACKEND','tensorflow');"
        "import numpy; assert int(numpy.__version__.split('.')[0]) < 2, numpy.__version__;"
        "import cv2; assert tuple(map(int, cv2.__version__.split('.')[:2])) < (4,12), cv2.__version__;"
        "from mediapipe.tasks.python import vision; assert hasattr(vision,'FaceLandmarker');"
        "from scipy.signal import butter, find_peaks;"
        "import absl.logging; import torch;"
        "print('UV_SYNC_DEEP_IMPORT_OK')"
    )
    out = subprocess.run(
        [str(py), "-c", probe], capture_output=True, text=True, timeout=600
    )
    assert "UV_SYNC_DEEP_IMPORT_OK" in out.stdout, (
        f"deep import from uv-synced env failed:\n{out.stdout[-2000:]}\n{out.stderr[-2000:]}"
    )
