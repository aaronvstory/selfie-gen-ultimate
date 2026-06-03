"""Real-subprocess GPU tests for the rPPG injector + the gpu_bootstrap probe.

These REAL-RUN the deep code path (spawn the injector as a subprocess, force a
CuPy nvrtc kernel compile) rather than grepping source text — the source-text
gap is exactly what shipped the friend's 20-min-CPU bug (the injector SAID it
registered DLL dirs but a probe that skipped them false-negatived). See
feedback_tdd_real_import_probe_not_just_text.

GPU-gated: skipped unless ``RUN_GPU_RPPG_TEST=1`` AND CuPy + a CUDA device are
actually importable on this box. On CI / no-GPU boxes they no-op (the assertions
would be meaningless without hardware). Run locally on the RTX box with:

    RUN_GPU_RPPG_TEST=1 venv/Scripts/python -m pytest tests/test_rppg_gpu_subprocess.py -q
"""

import os
import subprocess
import sys

import pytest

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
INJECTOR = os.path.join(REPO_ROOT, "rPPG", "rppg_injector.py")
SCRIPTS_DIR = os.path.join(REPO_ROOT, "scripts")


def _gpu_available() -> bool:
    """True only when a real CuPy GPU kernel can compile on this box."""
    try:
        if SCRIPTS_DIR not in sys.path:
            sys.path.insert(0, SCRIPTS_DIR)
        from cuda_dll_paths import register_cuda_dll_dirs

        register_cuda_dll_dirs()
        import cupy as cp  # noqa: F401
        from cupyx.scipy.ndimage import gaussian_filter as g

        g(cp.zeros((4, 4), dtype=cp.float32), 1.0)
        return True
    except Exception:
        return False


_SKIP = not (os.environ.get("RUN_GPU_RPPG_TEST") == "1" and _gpu_available())
pytestmark = pytest.mark.skipif(
    _SKIP, reason="set RUN_GPU_RPPG_TEST=1 on a CuPy-capable GPU box to run"
)


def _run_injector_help(cwd: str, env=None):
    """Run ``rppg_injector.py --help`` and return its combined output.

    --help triggers the module-level GPU init (the DLL registration + CuPy
    probe + the 'GPU backend:' line) WITHOUT doing any real injection work, so
    it's a fast, side-effect-free way to assert the GPU path engaged.
    """
    proc = subprocess.run(
        [sys.executable, INJECTOR, "--help"],
        cwd=cwd,
        capture_output=True,
        text=True,
        errors="replace",
        timeout=120,
        env=env,
    )
    return (proc.stdout or "") + "\n" + (proc.stderr or "")


def test_injector_subprocess_reports_gpu_on_path_with_spaces(tmp_path):
    """The friend's exact failure mode: the injector runs as a SUBPROCESS from
    a path WITH SPACES (his install is under 'Desktop\\AI Gen\\...'). On a GPU
    box it must report 'GPU backend: CuPy ... device(s)', NOT 'unavailable'.
    This guards the regression where the DLL dirs weren't registered in the
    child and nvrtc couldn't load.
    """
    spaced = tmp_path / "dir with spaces" / "run from here"
    spaced.mkdir(parents=True)
    out = _run_injector_help(cwd=str(spaced))
    assert "GPU backend: CuPy" in out, out
    assert "device(s)" in out, out
    assert "CuPy unavailable" not in out, out


def test_injector_force_cpu_surfaces_detail():
    """RPPG_FORCE_CPU=1 must report 'unavailable' AND surface the detail string
    (proves the error-surfacing fix: the bare '(RuntimeError)' that stranded
    the friend now carries the message)."""
    env = dict(os.environ)
    env["RPPG_FORCE_CPU"] = "1"
    out = _run_injector_help(cwd=REPO_ROOT, env=env)
    assert "CuPy unavailable" in out, out
    # Assert the COLON-separated "(Type: message)" format, not just that the
    # message substring appears somewhere — that distinguishes the new surfaced
    # format from a regression back to the bare "(RuntimeError)" (code-review
    # HIGH #3 PR #72).
    assert "(RuntimeError: RPPG_FORCE_CPU=1)" in out, out


def test_honest_probe_passes_on_gpu_box():
    """gpu_bootstrap.probe_cupy must return a version on this GPU box — proving
    the probe registers the nvidia DLL dirs before compiling a kernel (the §5
    false-negative guard: a probe that skipped registration would return None
    even here and stamp install_failed)."""
    if SCRIPTS_DIR not in sys.path:
        sys.path.insert(0, SCRIPTS_DIR)
    import gpu_bootstrap

    version = gpu_bootstrap.probe_cupy(sys.executable)
    assert version is not None, "honest probe false-negatived on a working GPU box"
    assert version.startswith("13."), version
