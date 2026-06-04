#!/usr/bin/env python3
"""
rPPG Detection and Manipulation Script - v5.0
2026-Ready: Hemoglobin absorption modeling, ICA/POS/CHROM hybrid extraction,
PTT phase offsets, segmented SNR, advanced phase coherence, HRV simulation,
thermal signature simulation, micro-expression timing.

Based on 2025-2026 IEEE/CVPR papers and iBeta ISO 30107-3 standards.
"""

import cv2
import numpy as np
import mediapipe as mp
from scipy import signal
from scipy.signal import butter, filtfilt, find_peaks, hilbert
from scipy.ndimage import gaussian_filter1d
from scipy.optimize import minimize
try:
    from sklearn.decomposition import FastICA
    HAS_SKLEARN = True
except ImportError:
    HAS_SKLEARN = False
from typing import Tuple, List, Dict, Optional, Any
from dataclasses import dataclass, field, asdict, replace
import warnings
import os
import logging
import sys
from contextlib import contextmanager
import random
from collections import deque
import urllib.request
import subprocess
import json
import shutil
from datetime import datetime
import tempfile

from face_kinematics import (
    score_face_kinematics,
    print_gate_banner,
    DEFAULT_PASS_THRESHOLD as KINEMATIC_PASS_THRESHOLD,
)

warnings.filterwarnings('ignore')
os.environ['TF_CPP_MIN_LOG_LEVEL'] = '3'
logging.getLogger('tensorflow').setLevel(logging.ERROR)

# absl is only used to quiet TensorFlow/mediapipe C++ log spam — it is NOT
# essential to the actual rPPG injection. Guard the import so a venv missing
# absl (mediapipe is installed --no-deps, which skips its absl-py~=2.3, and the
# TF-side absl can fail to land on a fresh install) degrades to "noisier logs"
# instead of crashing the entire rPPG step with an unguarded ImportError — the
# friend's v2.15 "rPPG fails, everything else works" bug. absl-py is also now
# pinned in requirements.txt so this fallback should rarely fire.
try:
    import absl.logging
    absl.logging.set_verbosity(absl.logging.ERROR)
except Exception:  # noqa: BLE001 — absl absent/broken must never break rPPG
    pass

# ---------------------------------------------------------------------------
# v6 optional modules — spectrum scorer (analyzer side), pulse library
# (injection side), IDKit classifier (controller fitness, used externally).
# All imports guarded so v5 stays operational if any v6 file is missing.
# ---------------------------------------------------------------------------
try:
    from v6_spectrum_scorer import score_spectrum as _v6_score_spectrum
    V6_SPECTRUM_AVAILABLE = True
except ImportError:
    _v6_score_spectrum = None
    V6_SPECTRUM_AVAILABLE = False

try:
    from v6_real_pulse_library import (
        generate_pulse_from_library as _v6_generate_pulse,
        blend_pulse as _v6_blend_pulse,
    )
    V6_PULSE_LIBRARY_AVAILABLE = True
except ImportError:
    _v6_generate_pulse = None
    _v6_blend_pulse = None
    V6_PULSE_LIBRARY_AVAILABLE = False


# ---------------------------------------------------------------------------
# GPU backend (CuPy) for per-frame pixel math
# ---------------------------------------------------------------------------
# When CuPy + a working CUDA device are available, the four hotspot methods
# (compute_skin_validity, apply_roi_pulse_modulation, simulate_thermal_signature,
# smooth_roi_boundaries) run on GPU; when not, everything stays on NumPy+OpenCV
# with byte-for-byte the same behaviour as before. The xp_of/to_gpu/to_cpu
# helpers let those methods accept either backend's arrays without branching.

# Register the pip/uv-installed NVIDIA CUDA component DLL dirs on the Windows
# DLL search path BEFORE importing cupy. The logic lives in the shared
# stdlib-only helper scripts/cuda_dll_paths.py so this injector and the GUI's
# gpu_bootstrap.probe_cupy register the EXACT same dirs (no drift — the probe
# false-negatived on a correctly-installed box when it skipped this, stranding
# the user on CPU; see scripts/cuda_dll_paths.py docstring). Without it, cupy
# imports but the first GPU kernel compile fails with "Could not find
# nvrtc64_*.dll", so CuPy reports unavailable and rPPG silently falls back to
# CPU even on a good GPU box (verified 2026-06-03 on an RTX 4090). The helper
# is CUDA-major agnostic (cu13 nvidia/cu13/bin/x86_64 + cu12 nvidia/cuda_nvrtc/
# bin) and a no-op off-Windows. Guarded so a zip missing the helper degrades to
# CPU instead of crashing the whole rPPG step.
try:
    # realpath (not abspath) so a symlinked rppg_injector.py still resolves the
    # real repo-root scripts/ dir for the shared helper import (gemini PR #72).
    _scripts_dir = os.path.join(
        os.path.dirname(os.path.dirname(os.path.realpath(__file__))), "scripts"
    )
    # Temporarily front of sys.path ONLY for this import, then remove it so we
    # don't permanently shadow stdlib/3rd-party modules with anything in
    # scripts/ (gemini MEDIUM PR #72). Once imported, the module is cached in
    # sys.modules, so the path entry is no longer needed.
    _added_scripts_dir = _scripts_dir not in sys.path
    if _added_scripts_dir:
        sys.path.insert(0, _scripts_dir)
    try:
        from cuda_dll_paths import (
            register_cuda_dll_dirs as _register_cuda_dll_dirs,
            clear_cupy_kernel_cache as _clear_cupy_kernel_cache,
            is_nvrtc_compile_error as _is_nvrtc_compile_error,
            summarize_nvrtc_compile_error as _summarize_nvrtc_compile_error,
            cuda_include_dirs as _cuda_include_dirs,
        )
    finally:
        if _added_scripts_dir:
            try:
                sys.path.remove(_scripts_dir)
            except ValueError:
                pass
except Exception:  # noqa: BLE001 — helper absent/unimportable => CPU fallback
    def _register_cuda_dll_dirs():
        return []

    def _clear_cupy_kernel_cache():
        return None

    def _is_nvrtc_compile_error(err):
        name = type(err).__name__.lower()
        return "compile" in name or "nvrtc" in name

    def _summarize_nvrtc_compile_error(err, limit=300):
        detail = str(err).strip().replace("\n", " ")
        return (detail[:limit] + "…") if len(detail) > limit else detail

    def _cuda_include_dirs():
        return []

# Guard the RUNTIME call too, not just the import: the helper now wraps its own
# filesystem touches, but a belt-and-suspenders try/except here guarantees that
# ANY unexpected failure (a future regression, an exotic FS error) degrades to
# CPU rather than crashing the whole rPPG step before the CuPy fallback below
# (external review PR #72).
try:
    _register_cuda_dll_dirs()
except Exception:  # noqa: BLE001 — registration must never break rPPG import
    pass
def _init_cupy_backend():
    """Import CuPy, verify a CUDA device, and force a real nvrtc kernel compile.

    Returns ``(cp_module, gaussian_filter)``; raises on any failure. Factored
    out so the CompileException recovery path can call it twice — once, then once
    more after clearing a stale JIT cache (see the recovery block below). Forcing
    a real COMPILE here (not just ``zeros()``/``getDeviceCount()``) makes a
    missing-nvrtc / stale-cache failure surface NOW, at import, instead of deep
    in the first frame op.
    """
    import cupy as cp
    from cupyx.scipy.ndimage import gaussian_filter as gf
    cp.zeros(1, dtype=cp.float32)  # device probe: raises if no CUDA device
    gf(cp.zeros((4, 4), dtype=cp.float32), 1.0)  # force nvrtc kernel compile
    return cp, gf


def _dump_gpu_error_sidecar(err):
    """Write the FULL CuPy error + traceback to a discoverable file; return its
    path (or None).

    The console log shows ONE summarized line (the actual ``error:``); the
    complete multi-line nvrtc compile log goes here so the exact cause is never
    lost to truncation again (the friend's CUDA-13 compile failure was hidden
    behind a 200-char head-truncation that only captured the benign opening
    remark). Diagnostics must never crash rPPG, so everything is wrapped.
    """
    try:
        import datetime
        import tempfile
        import traceback
        path = os.path.join(tempfile.gettempdir(), "rppg_gpu_compile_error.log")
        with open(path, "w", encoding="utf-8", errors="replace") as fh:
            fh.write(f"# rPPG GPU backend init failure — {datetime.datetime.now().isoformat()}\n")
            fh.write(f"# exception type: {type(err).__name__}\n\n")
            fh.write(str(err))
            fh.write("\n\n--- traceback ---\n")
            fh.write("".join(
                traceback.format_exception(type(err), err, err.__traceback__)
            ))
        return path
    except Exception:  # noqa: BLE001 — diagnostics must never crash rPPG
        return None


try:
    if os.environ.get("RPPG_FORCE_CPU") == "1":
        # Escape hatch: force the CPU path even when a working GPU is present
        # (benchmarking, or a workaround if a specific CuPy/driver combo
        # misbehaves). Raise so the normal CPU-fallback branch below runs.
        raise RuntimeError("RPPG_FORCE_CPU=1")
    try:
        _cp, _cp_gaussian_filter = _init_cupy_backend()
    except Exception as _first_gpu_err:
        # CuPy imported and a device exists, but the nvrtc kernel COMPILE failed.
        # The #1 cause on a box whose nvidia DLLs load correctly is a STALE JIT
        # kernel cache left by a previous CUDA toolkit/driver: the friend
        # upgraded CUDA 12.9 -> 13.x (driver 610.47) and the cubins cached under
        # ~/.cupy no longer compile/load (cpp_dialect.h remark #20200-D was just
        # the benign head of the log). Clear the cache and retry the compile ONCE
        # before falling back to CPU. A non-compile failure (no device / missing
        # module) can't be helped by a cache wipe — re-raise it immediately.
        if not _is_nvrtc_compile_error(_first_gpu_err):
            raise
        # TWO independent causes of an nvrtc compile failure on a box whose
        # nvidia DLLs load fine, recovered together before the CPU fallback:
        #
        # 1. MISSING CUDA RUNTIME HEADERS (the friend's actual bug, root-caused
        #    2026-06-04). CuPy 13.x compiles kernels by #include-ing the CUDA
        #    Runtime headers, and auto-detects them ONLY when the
        #    nvidia-cuda-runtime wheel's version EXACTLY matches the major.minor
        #    nvrtc reports. Our >=13.3,<14 pins let nvrtc + runtime float to
        #    DIFFERENT minors independently; when they skew, CuPy finds ZERO
        #    include dirs → "cannot open cuda_runtime.h" surfaced behind a benign
        #    cpp_dialect.h remark. FIX: point CUPY_CUDA_INCLUDE_PATH at the real
        #    on-disk nvidia/<...>/include dir (which always exists regardless of
        #    the version label) and retry, so a future skew self-heals.
        # 2. STALE JIT CACHE (a previous CUDA toolkit's cubins after a driver
        #    upgrade). FIX: clear ~/.cupy/kernel_cache and retry.
        # Apply BOTH, then retry the compile ONCE.
        try:
            _inc_dirs = _cuda_include_dirs()
        except Exception:  # noqa: BLE001
            _inc_dirs = []
        # Force CuPy to use the real on-disk CUDA include dir even when its own
        # wheel-version-match detector returned [] (the skew case). CuPy builds
        # the nvrtc -I flags in cupy.cuda.compiler._get_extra_include_dir_opts
        # (memoized) from _environment._get_include_dir_from_conda_or_wheel. We
        # WRAP that detector so it appends our discovered include dirs, then
        # clear the memoize cache so the retry recomputes the -I flags. There is
        # NO CUPY_CUDA_INCLUDE_PATH env var — this monkeypatch is the only way to
        # inject the path into an already-imported CuPy (root cause 2026-06-04).
        _inc_applied = ""
        if _inc_dirs:
            try:
                import cupy._environment as _cenv  # noqa: WPS433
                import cupy.cuda.compiler as _ccompiler  # noqa: WPS433
                if not getattr(_cenv, "_rppg_include_patched", False):
                    _orig_get_inc = _cenv._get_include_dir_from_conda_or_wheel

                    def _patched_get_inc(major, minor, _orig=_orig_get_inc,
                                         _extra=tuple(_inc_dirs)):
                        dirs = list(_orig(major, minor))
                        for d in _extra:
                            if d not in dirs:
                                dirs.append(d)
                        return dirs

                    _cenv._get_include_dir_from_conda_or_wheel = _patched_get_inc
                    _cenv._rppg_include_patched = True
                # _get_extra_include_dir_opts is @cupy._util.memoize'd, so its
                # cached -I tuple won't see the patch. cupy.clear_memo() is
                # CuPy's official reset for ALL memoized funcs — use it so the
                # retry recomputes the include flags with our forced dir.
                import cupy as _cupy_mod  # noqa: WPS433
                if hasattr(_cupy_mod, "clear_memo"):
                    _cupy_mod.clear_memo()
                _ = _ccompiler  # keep the import referenced (module side effects)
                _inc_applied = os.pathsep.join(_inc_dirs)
            except Exception:  # noqa: BLE001 — include patch is best-effort
                pass
        try:
            _cleared = _clear_cupy_kernel_cache()
        except Exception:  # noqa: BLE001 — cache wipe must never crash rPPG
            _cleared = None
        print(f"GPU backend: nvrtc kernel compile failed "
              f"({type(_first_gpu_err).__name__}: "
              f"{_summarize_nvrtc_compile_error(_first_gpu_err)}); "
              f"forced CUDA include dir ({_inc_applied or 'none found'}); "
              f"cleared CuPy JIT cache ({_cleared or 'nothing to clear'}); "
              f"retrying once…")
        _cp, _cp_gaussian_filter = _init_cupy_backend()
    GPU_AVAILABLE = True
    print(f"GPU backend: CuPy {_cp.__version__} on "
          f"{_cp.cuda.runtime.getDeviceCount()} device(s)")
except Exception as _cupy_err:
    _cp = None
    _cp_gaussian_filter = None
    GPU_AVAILABLE = False
    # Surface the actual failure detail, not just the class name. The bare
    # "(RuntimeError)" log stranded a friend's RTX 4080 on CPU for 20 min/iter
    # with no clue WHY. For an nvrtc CompileException, pull the real ``error:``
    # line (NOT the benign opening #pragma-message remark a head-truncate would
    # capture) and dump the FULL log to a sidecar file; for everything else keep
    # the short head-truncated message.
    if _is_nvrtc_compile_error(_cupy_err):
        _cupy_detail = _summarize_nvrtc_compile_error(_cupy_err)
        _sidecar = _dump_gpu_error_sidecar(_cupy_err)
        _sidecar_suffix = f" [full log: {_sidecar}]" if _sidecar else ""
    else:
        _cupy_detail = str(_cupy_err).strip().replace("\n", " ")
        if len(_cupy_detail) > 200:
            _cupy_detail = _cupy_detail[:200] + "…"
        _sidecar_suffix = ""
    # Omit the ": <detail>" entirely when the exception has no message, so the
    # log reads "(RuntimeError)" not a dangling "(RuntimeError: )" (gemini PR #72).
    _detail_suffix = f": {_cupy_detail}" if _cupy_detail else ""
    print(f"GPU backend: CuPy unavailable ({type(_cupy_err).__name__}"
          f"{_detail_suffix}); frame math stays on CPU.{_sidecar_suffix}")


def xp_of(arr):
    """Return the numerical backend module (cp or np) matching *arr*."""
    if GPU_AVAILABLE and isinstance(arr, _cp.ndarray):
        return _cp
    return np


def _snapshot_validates(tmp_snapshot: str, source_iter: str) -> bool:
    """True if *tmp_snapshot* is a complete copy of *source_iter*.

    Used by the iter-best snapshot adoption flow (search for
    ``_BEST_SNAPSHOT_NAME``). A bad copy here previously shipped as the
    final deliverable -- the post-loop final-copy from ``best_path``
    captured a torn mp4 with broken H.264 NAL units. We reject the
    snapshot if it differs in bytes from the source OR can't be opened
    by OpenCV OR reports a different frame count.

    Cheap pre-check (size) catches truncated copies without touching
    the file decoder; the cv2 reopen is the safety net.
    """
    try:
        if os.path.getsize(tmp_snapshot) != os.path.getsize(source_iter):
            return False
    except OSError:
        return False
    cap_tmp = cap_src = None
    try:
        cap_tmp = cv2.VideoCapture(tmp_snapshot)
        if not cap_tmp.isOpened():
            return False
        cap_src = cv2.VideoCapture(source_iter)
        if not cap_src.isOpened():
            return False
        n_tmp = int(cap_tmp.get(cv2.CAP_PROP_FRAME_COUNT))
        n_src = int(cap_src.get(cv2.CAP_PROP_FRAME_COUNT))
        if n_tmp <= 0 or n_tmp != n_src:
            return False
        return True
    except Exception:
        return False
    finally:
        for cap in (cap_tmp, cap_src):
            if cap is not None:
                try:
                    cap.release()
                except Exception:
                    pass


def to_gpu(arr):
    """Upload a NumPy array to GPU when CuPy is available; pass through otherwise."""
    if GPU_AVAILABLE and arr is not None and not isinstance(arr, _cp.ndarray):
        return _cp.asarray(arr)
    return arr


def to_cpu(arr):
    """Download a CuPy array to NumPy; pass through when already on host."""
    if GPU_AVAILABLE and isinstance(arr, _cp.ndarray):
        return _cp.asnumpy(arr)
    return arr


def gaussian_blur(arr, ksize, sigma):
    """Backend-agnostic 2D Gaussian blur.

    CPU path uses cv2.GaussianBlur for parity with the pre-GPU pipeline.
    GPU path uses cupyx.scipy.ndimage.gaussian_filter, which is sigma-driven
    (ksize is not a concept there). For 3-D arrays (H,W,C) the blur is
    applied on the spatial dims only.

    When *sigma* is 0, the effective sigma is derived from *ksize* using
    OpenCV's formula (sigma = 0.3*((ksize-1)*0.5 - 1) + 0.8). Without this
    translation, the CuPy path would do a no-op blur and diverge from the
    CPU path - which is what the earlier parity check caught.
    """
    if sigma <= 0:
        sigma = 0.3 * ((ksize - 1) * 0.5 - 1) + 0.8
    if GPU_AVAILABLE and isinstance(arr, _cp.ndarray):
        if arr.ndim == 3:
            return _cp_gaussian_filter(arr, sigma=(sigma, sigma, 0))
        return _cp_gaussian_filter(arr, sigma=sigma)
    return cv2.GaussianBlur(arr, (ksize, ksize), sigma)


def bilateral_approx(arr):
    """Edge-preserving smoothing: true bilateral on CPU, Gaussian approx on GPU.

    cv2.bilateralFilter has no direct CuPy equivalent. On the GPU path we
    substitute a sigma=1.5 Gaussian, which gives similar smoothing at minor
    cost to edge preservation - acceptable because the result is blended
    at only 30% weight inside ROIs in smooth_roi_boundaries.
    """
    if GPU_AVAILABLE and isinstance(arr, _cp.ndarray):
        return _cp_gaussian_filter(arr.astype(_cp.float32), sigma=(1.5, 1.5, 0))
    return cv2.bilateralFilter(arr.astype(np.uint8), 5, 15, 15).astype(np.float32)

# ---------------------------------------------------------------------------
# 2026 target metrics (updated from v4.1)
# ---------------------------------------------------------------------------
target_snr_min = 4.0               # dB, reduced from 10.0 for realism
target_snr_max = 12.0              # dB, reduced from 25.0 to avoid over-processing flags
target_phase_coherence = 18.0      # degrees, tightened from 25.0 per 2026 KYC standards
target_temporal_consistency = 0.85  # NEW: segment-to-segment SNR stability
target_motion_artifacts_min = 0.03  # v5.10c: raised from 0.02; run-14 motion 0.022–0.029 was razor-thin above old floor with no guard buffer
target_motion_artifacts = 0.15     # NEW: max acceptable motion artifact ratio
target_harmonic_alignment = 0.7    # NEW: natural harmonic presence score

# Pulse strength parameters
base_strength = 0.005              # reduced from 0.015 for subtlety
max_strength = 0.04                # reduced from 0.12

# HRV simulation target
HRV_SDNN_TARGET_MS = 50.0         # ms, standard deviation of NN intervals

# Randomise target heart rate (lower range for resting adult)
randomHr = random.uniform(75.0, 99.0)

DEFAULT_FACE_LANDMARKER_MODEL_URL = (
    "https://storage.googleapis.com/mediapipe-models/face_landmarker/"
    "face_landmarker/float16/latest/face_landmarker.task"
)

CODEC_ENCODER_MAP_CPU = {
    'h264': 'libx264',
    'hevc': 'libx265',
    'mpeg4': 'mpeg4',
    'mjpeg': 'mjpeg',
    'vp8': 'libvpx',
    'vp9': 'libvpx-vp9',
    'av1': 'libaom-av1',
}

# NVENC overrides -- used when the local ffmpeg reports the matching GPU
# encoder. Detected lazily via `detect_nvenc_encoders()` below.
CODEC_ENCODER_MAP_GPU = {
    'h264': 'h264_nvenc',
    'hevc': 'hevc_nvenc',
    'av1': 'av1_nvenc',
}

ENCODER_PROFILE_MAP = {
    'libx264': {'baseline', 'main', 'high', 'high10', 'high422', 'high444'},
    'libx265': {'main', 'main10', 'mainstillpicture'},
    'h264_nvenc': {'baseline', 'main', 'high', 'high444'},
    'hevc_nvenc': {'main', 'main10'},
    'av1_nvenc': {'main'},
}

NVENC_ENCODERS: Optional[set] = None


def detect_nvenc_encoders() -> set:
    """Probe `ffmpeg -encoders` once; return the set of *_nvenc encoders the
    local binary advertises.  Cached for subsequent calls.
    """
    global NVENC_ENCODERS
    if NVENC_ENCODERS is not None:
        return NVENC_ENCODERS
    found = set()
    try:
        result = subprocess.run(
            ['ffmpeg', '-hide_banner', '-encoders'],
            capture_output=True, text=True, check=True, timeout=10,
        )
        for line in result.stdout.splitlines():
            parts = line.strip().split()
            if len(parts) >= 2 and parts[1].endswith('_nvenc'):
                found.add(parts[1])
    except (FileNotFoundError, subprocess.CalledProcessError, subprocess.TimeoutExpired):
        pass
    NVENC_ENCODERS = found
    return found


def select_encoder(codec_name: Optional[str]) -> Optional[str]:
    """Return the preferred ffmpeg encoder name for *codec_name*.

    Prefers NVENC when the local ffmpeg reports it; falls back to software.
    """
    if not codec_name:
        return None
    gpu = detect_nvenc_encoders()
    gpu_enc = CODEC_ENCODER_MAP_GPU.get(codec_name)
    if gpu_enc and gpu_enc in gpu:
        return gpu_enc
    return CODEC_ENCODER_MAP_CPU.get(codec_name)

# ROI landmarks optimised for blood flow (MediaPipe 468/478).
# Each list is a bilaterally symmetric set of boundary landmarks for the
# named anatomical region.  create_stabilized_mask wraps them in a convex
# hull before filling, so listing order is irrelevant -- the hull will
# enclose the full extent of the points.
ROI_LANDMARKS = {
    # Forehead: upper boundary = face-oval hairline arc (temple to temple).
    # Lower boundary = upper eyebrow contour + glabella midline.
    'forehead': [
        # Hairline arc: subject-left temple -> midline -> subject-right temple
        21, 54, 103, 67, 109, 10, 338, 297, 332, 284, 251,
        # Upper eyebrow line, subject-left (below hairline)
        70, 63, 105, 66, 107,
        # Glabella midline (between brows)
        9, 8,
        # Upper eyebrow line, subject-right
        336, 296, 334, 293, 300,
    ],
    # Left cheek (subject's left).  Lateral = face oval, superior = lower
    # orbital rim, medial = nasolabial fold, inferior = mid-jaw.
    'left_cheek': [
        # Lateral face oval (temple down to jaw)
        234, 93, 132, 58, 172,
        # Infraorbital rim (just below lower eyelid)
        229, 230, 231, 232, 233,
        # Medial / nasolabial boundary
        131, 49, 102, 64, 203, 206,
        # Mid-cheek / zygomatic anchors
        50, 117, 118, 119, 205, 36,
    ],
    # Right cheek (subject's right).  Mirror of left_cheek.
    'right_cheek': [
        454, 323, 361, 288, 397,
        449, 450, 448, 452, 453,
        360, 279, 331, 294, 423, 426,
        280, 346, 347, 348, 425, 266,
    ],
    # Chin: below the lower lip, bounded by the jaw line.
    'chin': [
        # Lower lip border (subject-left -> midline -> subject-right)
        84, 181, 91, 146, 17, 314, 405, 321, 375,
        # Jaw outline (subject-left -> chin tip -> subject-right)
        150, 149, 176, 148, 152, 377, 400, 378, 379,
        # Mentolabial sulcus and chin midline
        18, 200, 199, 175,
    ],
    # Nose: bridge + tip, kept tight to avoid cheek contamination.
    'nose': [
        # Bridge midline (glabella down to sub-tip)
        8, 168, 6, 197, 195, 5, 4, 1, 2, 19, 94,
        # Upper bridge sides
        122, 351,
        # Mid-bridge sides
        45, 275,
        # Columella / upper lip root
        98, 327,
    ],
}

# Pulse Transit Time offsets in radians (blood wave propagation down the face)
PTT_PHASE_OFFSETS = {
    'forehead': (0.0, 0.0),
    'nose': (0.05, 0.10),
    'left_cheek': (0.12, 0.18),
    'right_cheek': (0.12, 0.18),
    'chin': (0.22, 0.30),
}


# ---------------------------------------------------------------------------
# Tunable pulse parameters (Layer 2 of the intelligent iteration system)
# ---------------------------------------------------------------------------

@dataclass
class PulseParams:
    """All tunable knobs for the pulse injection pipeline.

    Defaults mirror the v5 hand-tuned behaviour.  The GuardedController
    mutates these based on the KNOB_REGISTRY and observed metric deltas.
    """
    # --- Core injection amplitude ---
    strength: float = base_strength                # overall pulse strength

    # --- Cardiac waveform shape (derive_roi_pulse) ---
    # v5.7: defaults lowered (was h2=0.4, h3=0.15) to keep the operating
    # point off the steep slope regime where small h2/h3 increments produce
    # disproportionate phase swings. With the corrected phase slopes (200
    # for h2, 100 for h3) PASS_SAFETY_MARGIN now sees the cost correctly,
    # but a tighter starting amplitude is also a sensible defence in depth.
    h2_amp: float = 0.2                            # 2nd harmonic (dicrotic notch)
    h2_phase: float = 0.8                          # 2nd harmonic phase offset (rad)
    h3_amp: float = 0.08                           # 3rd harmonic (minor vascular)
    stochastic_jitter: float = 0.015               # per-ROI random jitter (pre-norm)
    pulse_smoothing_sigma: float = 0.8             # gaussian smoothing (notch-preserving)

    # --- Shared envelope (generate_base_cardiac_signal) ---
    resp_amp: float = 0.10                         # respiratory sinus arrhythmia depth
    micro_exp_min: float = 0.01                    # micro-expression amplitude floor
    micro_exp_max: float = 0.03                    # micro-expression amplitude ceiling
    mayer_amp: float = 0.01                        # Mayer-wave vasomotor amplitude

    # --- PTT distribution (optimize_phase_offsets) ---
    ptt_spread: float = 1.0                        # multiplier on PTT_PHASE_OFFSETS ranges

    # --- Hb absorption (apply_roi_pulse_modulation) ---
    hb_g_mult: float = 120.0                       # green-channel additive scaler
    hb_r_mult: float = 80.0                        # red-channel additive scaler
    hb_b_mult: float = 20.0                        # blue-channel additive scaler (inverse)
    hb_mask_blur_size: int = 15                    # soft-mask Gaussian kernel (odd)

    # --- Motion artifact shaping ---
    roi_motion_noise: float = 0.35                 # in-ROI sensor noise sigma (pixel-level, weak motion effect)
    outside_noise_sigma: float = 0.5               # outside-ROI sensor noise (cosmetic)
    envelope_burst_prob: float = 0.0               # per-sample probability of an additive random-sign Gaussian burst (sigma 2 samples, 2x peak) - motion-artifact lever that deliberately reduces SNR rather than amplifying it

    # --- v6 pulse-source selection ---
    # 'synthetic' = v5 sin+h2+h3 harmonic construction in derive_roi_pulse.
    # 'real'      = waveform sampled from v6_real_pulse_library (more realistic
    #               PPG morphology: proper dicrotic notch, HRV).
    # 'blend'     = linear mix of synthetic + library, weighted by pulse_blend_weight.
    pulse_source: str = 'synthetic'                # 'synthetic' | 'real' | 'blend'
    pulse_blend_weight: float = 0.5                # 0=pure synthetic, 1=pure library (only used in blend mode)

    # --- v6 output bitrate multiplier ---
    # The pipe-encode step targets source bitrate by default (v5.6). For low-
    # bitrate AI-gen inputs this can quantise away the rPPG modulation. A
    # multiplier >1 expands the encode bitrate so the injection survives.
    output_bitrate_mult: float = 1.0

    def snapshot(self) -> Dict[str, Any]:
        """Return a JSON-serialisable copy of the knob values."""
        return asdict(self)


# Knob -> metric effect registry.
# 'effects' values are hand-authored Δmetric per +1.0 Δknob (in knob-native units).
# These are seed values for the GuardedController; Layer 4 regression refines them
# once ≥2 real observations exist for a (knob, metric) pair.
# Metric axes: snr, phase, temporal, motion, harmonic.
KNOB_REGISTRY: Dict[str, Dict[str, Any]] = {
    'strength': {
        'min': 0.0015, 'max': max_strength, 'step': 0.0015,
        'primary_metric': 'snr',
        'effects': {'snr': 850.0, 'phase': -8000.0, 'temporal': 8.0,
                    'motion': -20.0, 'harmonic': 6.0},
        'description': 'Overall pulse strength (Hb modulation amplitude). v5.10e: phase slope corrected -2500→-8000. v5.10h: motion slope corrected -5→-20. Cycle-3 iter-1 strength +0.001835 → motion 0.051→0.015 (Δ=-0.036); registry -5 predicted -0.009 (4× off). Using -20 so the calibration motion-prediction guard can block oversized strength bumps that would crash motion below floor.',
    },
    'h2_amp': {
        'min': 0.0, 'max': 0.5, 'step': 0.05,
        'primary_metric': 'harmonic',
        'effects': {'snr': -0.5, 'phase': 200.0, 'temporal': -0.05,
                    'motion': 0.05, 'harmonic': 0.6},
        'description': '2nd harmonic amplitude (dicrotic notch depth); max capped to avoid peaky visible waveform. v5.7: phase slope corrected from +1.0 to +200 after diag memory observed h2_amp moves driving cross-ROI phase coherence by 100+ deg per +0.05 step across 3 runs. The hand-authored 1.0 was orders of magnitude too small, letting PASS_SAFETY_MARGIN soft-veto miss huge phase costs and producing the recurring "phase never recovers" pattern.',
    },
    'h3_amp': {
        'min': 0.0, 'max': 0.4, 'step': 0.03,
        'primary_metric': 'harmonic',
        'effects': {'snr': -0.3, 'phase': 300.0, 'temporal': -0.03,
                    'motion': 0.03, 'harmonic': 0.3},
        'description': '3rd harmonic amplitude (minor vascular component). v5.7: phase slope corrected from +0.5 to +100 - run-8 observed +53.7 deg phase from a +0.03 step (~1790 deg/unit). v5.10h: phase slope corrected +100→+300. Cycle-1 iter-2 observed +11.8 deg from +0.03 step (~393 deg/unit, 4x the registry value). Using +300 as conservative midpoint; the 3x damp cap will limit blended slopes that deviate beyond that early on.',
    },
    'resp_amp': {
        'min': 0.0, 'max': 0.15, 'step': 0.02,
        'primary_metric': 'temporal',
        'effects': {'snr': -0.8, 'phase': 600.0, 'temporal': +0.6,
                    'motion': 0.2, 'harmonic': 0.1},
        'description': 'Respiratory sinus arrhythmia depth (envelope AM); max capped to avoid visible breathing modulation. v5.8: phase slope corrected from +1.5 to +600. v5.10: temporal slope sign flipped -0.6->+0.6. v5.10h: reverted earlier sign flip attempt (-300). Both reducing AND increasing resp_amp caused large positive phase deltas across cycles 2 and 5 — the effect is nonlinear or stochastic, so +600 (large positive cost) is the safer prior that prevents the controller from treating resp_amp as a phase-safe knob.',
    },
    'mayer_amp': {
        'min': 0.0, 'max': 0.05, 'step': 0.005,
        'primary_metric': 'temporal',
        'effects': {'snr': 3.0, 'phase': 3000.0, 'temporal': 0.2,
                    'motion': 0.02, 'harmonic': 0.0},
        'description': 'Mayer-wave (slow vasomotor) envelope amplitude. v5.9: phase slope corrected from -5.0 to +3000 (run-10 observed +16.2 deg from a +0.005 step, ~3240 deg/unit, vs registry predicting -0.025 deg). Mayer waves apparently couple into cross-ROI phase coherence the same way h2/h3/resp_amp do - amplitude knobs that modulate the cardiac waveform have outsize phase impact and the registry was wrong on both magnitude AND sign for this one. Until the cause is understood, treat mayer_amp UP as a strong phase-cost knob like its harmonic siblings.',
    },
    'pulse_smoothing_sigma': {
        'min': 0.3, 'max': 1.6, 'step': 0.1,
        'primary_metric': 'temporal',
        'effects': {'snr': -3.0, 'phase': -80.0, 'temporal': -0.1,
                    'motion': 0.05, 'harmonic': -0.14},
        'description': 'Gaussian smoothing sigma on derived pulse (higher sigma = lower SNR + better phase; sign corrected per observed iter 4 behaviour). v5.10d: phase slope corrected -4→-15. v5.10h: phase slope corrected -15→-80. Cycle-4 iter-1: sigma -0.1 → phase +10.6° (registry predicted +1.5° at -15, actual ~-106 deg/unit, 7× off). Using -80 as conservative estimate; with this value a 1-step sigma pick predicts +8° phase risk which the soft-veto will block when phase slack is thin.',
    },
    'stochastic_jitter': {
        'min': 0.0, 'max': 0.04, 'step': 0.003,
        'primary_metric': 'temporal',
        'effects': {'snr': 50.0, 'phase': +3000.0, 'temporal': 30.0,
                    'motion': -3.0, 'harmonic': 10.0},
        'description': 'Per-ROI random jitter added before normalisation; phase/motion signs corrected per observed iter 2 behaviour (jitter reduces phase error and motion-artifact fraction). v5.5: SNR slope corrected from +300 to +50. v5.10d: phase slope SIGN FLIPPED -400→+1000 (runs 13-15 showed jitter consistently increasing phase). v5.10h: phase slope +1000→+3000. Cycle-2 iter-3 observed +26.8° from +0.003 step (~8933 deg/unit); with 3× damp cap the blended slope settles near 3000 after 2 obs anyway — raising the registry to 3000 makes the first-pick cost estimate accurate and prevents accidental upward jitter picks when phase slack is thin.',
    },
    'ptt_spread': {
        'min': 0.4, 'max': 1.6, 'step': 0.1,
        'primary_metric': 'phase',
        'effects': {'snr': -0.2, 'phase': 5.6, 'temporal': 0.5,
                    'motion': -0.5, 'harmonic': 0.9},
        'description': 'Multiplier on PTT phase-offset spread across ROIs. v5.10h: motion slope corrected -0.1->-0.5. Cycle-5 iter-4 ptt_spread +0.1 drove motion 0.055->0.010 (registry predicted -0.01, actual -0.045, ~4.5x off). Using -0.5 as conservative correction so the soft-veto blocks ptt_spread UP picks when motion has thin margin.',
    },
    'roi_motion_noise': {
        'min': 0.0, 'max': 0.6, 'step': 0.05,
        'primary_metric': 'snr',
        'effects': {'snr': -1.1, 'phase': 11.0, 'temporal': 0.3,
                    'motion': 0.0, 'harmonic': -0.1},
        'description': 'In-ROI sensor noise sigma (pixel-level). v5.3: motion slope corrected to 0.0 - per-pixel Gaussian noise averages out across the ROI mean used by extract_rgb_signals, so this knob does NOT actually move motion_artifacts. Kept as a cosmetic grain knob; max capped to avoid visible ROI grain.',
    },
    'envelope_burst_prob': {
        'min': 0.0, 'max': 0.04, 'step': 0.005,
        'primary_metric': 'motion',
        'effects': {'snr': -2000.0, 'phase': -200.0, 'temporal': -20.0,
                    'motion': 6.0, 'harmonic': -2.0},
        'description': 'Per-sample probability of an additive random-sign Gaussian burst (sigma=2, amp=4x pulse peak in v5.3, was 2x). v5.3 bumped amp to 4x because synthetic tests confirmed amp=2x never lifted motion_artifacts off 0.000; all cross-effect slopes in this row doubled in lockstep so the controller risk model still tracks. Sweet spot prob 0.02-0.04 lands motion in-band at 0.05-0.10; max capped at 0.04 to avoid the high-prob saturation regime where bursts overlap and the median envelope rises along with them, defeating the 3x-median threshold.',
    },
    'hb_g_mult': {
        'min': 60.0, 'max': 180.0, 'step': 10.0,
        'primary_metric': 'snr',
        'effects': {'snr': 0.08, 'phase': -0.25, 'temporal': 0.0015,
                    'motion': -0.0005, 'harmonic': 0.001},
        'description': 'Green-channel Hb absorption multiplier (dominant SNR driver)',
    },
}


def clamp_to_registry(knob: str, value: float) -> float:
    """Clamp *value* into the KNOB_REGISTRY bounds for *knob*."""
    spec = KNOB_REGISTRY.get(knob)
    if spec is None:
        return value
    return float(max(spec['min'], min(spec['max'], value)))

# NumPy compatibility shim for trapezoid/trapz
_integrate = getattr(np, 'trapezoid', None) or getattr(np, 'trapz', None)


# ---------------------------------------------------------------------------
# Utility functions
# ---------------------------------------------------------------------------

def generate_output_path(input_path: str) -> str:
    """Generate output path with _dm# suffix, incrementing past any prior run.

    A successful iterative run renames its output to include a metric
    suffix (e.g. 'kling_dm1 - 11.19-3.3-0.57-0.00-0.85.mp4'), so a naive
    `os.path.exists('kling_dm1.mp4')` check would return False on the next
    invocation and produce a colliding _dm1 again. Match the bare form AND
    the metric-suffixed form when checking for collisions. Iter-extra
    files ('<base>_dm{N}_iter{M} - <metrics>.mp4') are NOT counted - their
    basenames start with '<base>_dm{N}_iter', not '<base>_dm{N} -'.
    """
    base_path, ext = os.path.splitext(input_path)
    directory = os.path.dirname(base_path) or '.'
    base_name = os.path.basename(base_path)
    try:
        entries = os.listdir(directory)
    except (FileNotFoundError, OSError):
        entries = []
    counter = 1
    while True:
        prefix = f"{base_name}_dm{counter}"
        collision = any(
            f == prefix + ext or f.startswith(prefix + ' - ')
            for f in entries
        )
        if not collision:
            return f"{base_path}_dm{counter}{ext}"
        counter += 1


def format_metric_suffix(metrics: Dict[str, float]) -> str:
    """Format the 5 metrics as a hyphenated suffix for filename embedding.

    Example: {'snr': 11.19, 'phase': 3.3, ...} -> '11.19-3.3-0.57-0.00-0.85'.
    Order matches the per-iteration log line: SNR / Phase / Temporal / Motion / Harmonic.
    """
    return (
        f"{metrics['snr']:.2f}-"
        f"{metrics['phase']:.1f}-"
        f"{metrics['temporal']:.2f}-"
        f"{metrics['motion']:.2f}-"
        f"{metrics['harmonic']:.2f}"
    )


def add_metric_suffix(path: str, metrics: Dict[str, float]) -> str:
    """Insert ' - <metric-suffix>' before the file extension on *path*.

    'output/kling_dm1.mp4' + metrics -> 'output/kling_dm1 - 11.19-3.3-0.57-0.00-0.85.mp4'.
    """
    base, ext = os.path.splitext(path)
    return f"{base} - {format_metric_suffix(metrics)}{ext}"


def create_temp_video_path(reference_path: str, suffix: str = '.tmp') -> str:
    """Create a temp video path alongside the reference video."""
    _, ext = os.path.splitext(reference_path)
    temp_dir = os.path.dirname(os.path.abspath(reference_path)) or None
    with tempfile.NamedTemporaryFile(delete=False, suffix=f"{suffix}{ext}", dir=temp_dir) as temp_file:
        return temp_file.name


def probe_video_stream_settings(video_path: str) -> Dict[str, Optional[str]]:
    """Read source video/container settings via ffprobe when available."""
    settings = {
        'codec_name': None,
        'bit_rate': None,
        'pix_fmt': None,
        'profile': None,
        'format_name': None,
        'has_audio': False,
        'width': None,
        'height': None,
    }

    cmd = [
        'ffprobe',
        '-v', 'error',
        '-show_entries', 'stream=index,codec_type,codec_name,bit_rate,pix_fmt,profile,width,height:format=format_name',
        '-of', 'json',
        video_path,
    ]

    try:
        result = subprocess.run(cmd, capture_output=True, text=True, check=True)
        data = json.loads(result.stdout or '{}')
    except (FileNotFoundError, subprocess.CalledProcessError, json.JSONDecodeError):
        return settings

    streams = data.get('streams', [])
    video_stream = next((stream for stream in streams if stream.get('codec_type') == 'video'), {})
    settings['codec_name'] = video_stream.get('codec_name')
    settings['bit_rate'] = video_stream.get('bit_rate')
    settings['pix_fmt'] = video_stream.get('pix_fmt')
    settings['profile'] = video_stream.get('profile')
    settings['width'] = video_stream.get('width')
    settings['height'] = video_stream.get('height')
    settings['has_audio'] = any(stream.get('codec_type') == 'audio' for stream in streams)
    settings['format_name'] = data.get('format', {}).get('format_name')
    return settings


def normalize_encoder_profile(profile: Optional[str], encoder: str) -> Optional[str]:
    """Map source profile names to values accepted by the target FFmpeg encoder."""
    if not profile:
        return None

    normalized = profile.strip().lower().replace('-', '').replace('_', '').replace(' ', '')
    profile_aliases = {
        'constrainedbaseline': 'baseline',
        'baseline': 'baseline',
        'main': 'main',
        'high': 'high',
        'high10': 'high10',
        'high422': 'high422',
        'high444predictive': 'high444',
        'high444': 'high444',
        'main10': 'main10',
        'mainstillpicture': 'mainstillpicture',
    }

    mapped = profile_aliases.get(normalized)
    if mapped in ENCODER_PROFILE_MAP.get(encoder, set()):
        return mapped
    return None


def build_deband_only_filter() -> Tuple[str, str]:
    """Build a very light deband-only FFmpeg filter (applied BEFORE rPPG manipulation)."""
    filter_complex = "[0:v]deband=1thr=0.02:2thr=0.02:3thr=0.02:range=16:blur=1[final_output]"
    return filter_complex, '[final_output]'


def build_post_rppg_filter_chain(include_grain: bool, width: Optional[int] = None,
                                  height: Optional[int] = None) -> Tuple[str, str]:
    """Build the post-rPPG FFmpeg filter graph (applied AFTER rPPG manipulation).

    v5 change: Deband is no longer included here -- it runs as a separate pre-step.
    This chain handles curves, unsharp, CAS, and optional grain overlay.
    """
    if include_grain:
        grain_scale = ''
        if width and height:
            grain_scale = f"scale={width}:{height},"
        filter_complex = (
            "[debanded]curves=m='0/0.016 0.2/0.17 0.8/0.82 1/1'[curved];"
            "[curved]unsharp=9:9:0.5[clarity];"
            "[clarity]cas=strength=0.2[base_ready];"
            f"[1:v]{grain_scale}format=yuv420p[grain_scaled];"
            "[base_ready][grain_scaled]blend=all_mode=overlay:all_opacity=0.2:shortest=1:repeatlast=0:eof_action=endall[final_output]"
        )
        return filter_complex, '[final_output]'

    filter_complex = (
        "[0:v]curves=m='0/0.012 0.2/0.18 0.8/0.82 1/0.99'[curved];"
        "[curved]unsharp=9:9:0.5[clarity];"
        "[clarity]cas=strength=0.2[final_output]"
    )
    return filter_complex, '[final_output]'


def build_full_filter_chain(include_grain: bool, width: Optional[int] = None,
                            height: Optional[int] = None) -> Tuple[str, str]:
    """Legacy combined filter chain (deband + curves + unsharp + CAS + grain).

    Used only when --legacy-pipeline is active to match v4.1 behaviour.
    """
    if include_grain:
        grain_scale = ''
        if width and height:
            grain_scale = f"scale={width}:{height},"
        filter_complex = (
            "[0:v]deband=1thr=0.02:2thr=0.02:3thr=0.02:range=16:blur=1[debanded];"
            "[debanded]curves=m='0/0.05 0.2/0.15 0.8/0.85 1/0.95'[curved];"
            "[curved]unsharp=9:9:0.5[clarity];"
            "[clarity]cas=strength=0.3[base_ready];"
            f"[1:v]{grain_scale}format=yuv420p[grain_scaled];"
            "[base_ready][grain_scaled]blend=all_mode=overlay:all_opacity=0.2:shortest=1:repeatlast=0:eof_action=endall[final_output]"
        )
        return filter_complex, '[final_output]'

    filter_complex = (
        "[0:v]deband=1thr=0.02:2thr=0.02:3thr=0.02:range=16:blur=1[debanded];"
        "[debanded]curves=m='0/0.05 0.2/0.15 0.8/0.85 1/0.95'[curved];"
        "[curved]unsharp=9:9:0.5[clarity];"
        "[clarity]cas=strength=0.3[final_output]"
    )
    return filter_complex, '[final_output]'


def _run_ffmpeg_encode(temp_video_path: str, source_video_path: str, output_path: str,
                       filter_builder, grain_video_path: Optional[str] = None,
                       enable_fx: bool = True,
                       remove_input_on_success: bool = True,
                       keep_input_on_failure: bool = False) -> bool:
    """Shared FFmpeg encoding logic used by both deband pre-pass and post-rPPG pass."""
    settings = probe_video_stream_settings(source_video_path)
    codec_name = settings.get('codec_name')
    encoder = select_encoder(codec_name)

    if not encoder:
        if keep_input_on_failure:
            print(f"FFmpeg codec match unavailable for source codec '{codec_name}'. Skipping FFmpeg pass.")
            return False
        print(
            f"FFmpeg codec match unavailable for source codec '{codec_name}'. "
            f"Keeping temporary output encoding at: {output_path}"
        )
        shutil.move(temp_video_path, output_path)
        return False

    is_nvenc = encoder.endswith('_nvenc')

    cmd = ['ffmpeg', '-y']
    # NVDEC for decode + nv12 output keeps frames on the GPU when no filters
    # run, and decodes in system memory with nv12 layout when they do -- safe
    # in both paths. Only applied when we know the downstream encoder is
    # NVENC so software-only builds stay on the default CPU path.
    if is_nvenc:
        cmd.extend(['-hwaccel', 'cuda', '-hwaccel_output_format', 'nv12'])
    cmd.extend(['-i', temp_video_path])

    include_grain = bool(grain_video_path and os.path.exists(grain_video_path))
    if grain_video_path and not include_grain:
        print(f"Grain video not found, skipping grain overlay: {grain_video_path}")
    applied_grain = enable_fx and include_grain

    if applied_grain:
        cmd.extend(['-stream_loop', '-1', '-i', grain_video_path])

    if settings.get('has_audio'):
        cmd.extend(['-i', source_video_path])

    if enable_fx:
        # Handle filter builders that don't take include_grain (e.g. deband-only)
        try:
            filter_complex, video_map = filter_builder(
                include_grain=applied_grain,
                width=settings.get('width'),
                height=settings.get('height'),
            )
        except TypeError:
            filter_complex, video_map = filter_builder()
        cmd.extend(['-filter_complex', filter_complex, '-map', video_map])
    else:
        cmd.extend(['-map', '0:v:0'])

    cmd.extend(['-c:v', encoder])

    if is_nvenc:
        # p5 = balanced speed/quality; vbr rate-control matches the source
        # bit-rate constraint. These flags are ignored by software encoders
        # so are gated on is_nvenc to keep the CPU path unchanged.
        cmd.extend(['-preset', 'p5', '-tune', 'hq', '-rc', 'vbr'])

    bit_rate = settings.get('bit_rate')
    if bit_rate and str(bit_rate).isdigit():
        cmd.extend(['-b:v', str(bit_rate)])

    pix_fmt = settings.get('pix_fmt')
    if pix_fmt:
        cmd.extend(['-pix_fmt', pix_fmt])

    profile = normalize_encoder_profile(settings.get('profile'), encoder)
    if profile:
        cmd.extend(['-profile:v', profile])

    if settings.get('has_audio'):
        audio_input_index = 2 if applied_grain else 1
        cmd.extend(['-map', f'{audio_input_index}:a?', '-c:a', 'copy'])
    else:
        cmd.append('-an')

    if output_path.lower().endswith(('.mp4', '.mov')):
        cmd.extend(['-movflags', '+faststart'])

    if applied_grain:
        cmd.append('-shortest')

    cmd.append(output_path)

    try:
        subprocess.run(cmd, check=True, capture_output=True, text=True)
        if remove_input_on_success and os.path.abspath(temp_video_path) != os.path.abspath(output_path):
            os.remove(temp_video_path)
        return True
    except FileNotFoundError as exc:
        if keep_input_on_failure:
            print(f"FFmpeg pass failed and will be skipped: {exc}")
            return False
        print(f"FFmpeg finalization failed, keeping temporary output encoding: {exc}")
        if os.path.exists(output_path):
            os.remove(output_path)
        shutil.move(temp_video_path, output_path)
        return False
    except subprocess.CalledProcessError as exc:
        stderr = (exc.stderr or '').strip()
        if stderr:
            print(f"FFmpeg stderr:\n{stderr}")
        if keep_input_on_failure:
            print(f"FFmpeg pass failed and will be skipped: {exc}")
            return False
        print(f"FFmpeg finalization failed, keeping temporary output encoding: {exc}")
        if os.path.exists(output_path):
            os.remove(output_path)
        shutil.move(temp_video_path, output_path)
        return False


def _spawn_ffmpeg_pipe_encode(source_settings_video_path: str, output_path: str,
                               width: int, height: int, fps: float,
                               enable_fx: bool = False,
                               grain_video_path: Optional[str] = None,
                               legacy_pipeline: bool = False,
                               output_bitrate_mult: float = 1.0,
                               ) -> Tuple[subprocess.Popen, tempfile.SpooledTemporaryFile,
                                          List[str]]:
    """Spawn an ffmpeg subprocess that consumes raw bgr24 frames from stdin
    and writes a source-matching encoded output in a single lossy pass.

    Replaces the old two-stage cv2.VideoWriter(mp4v) + ffmpeg finalize path,
    which wasted one encode pass per call. The returned process is the
    caller's responsibility: write each frame's raw bytes to proc.stdin
    (uint8 BGR contiguous), then close stdin and wait. Returns
    (proc, stderr_buffer, cmd) - the buffer captures ffmpeg's stderr to a
    spool file so a successful run produces no terminal noise but a
    failure can be diagnosed post-mortem by reading the buffer.
    """
    settings = probe_video_stream_settings(source_settings_video_path)
    codec_name = settings.get('codec_name')
    encoder = select_encoder(codec_name)
    if not encoder:
        # Fall back to a universally-available encoder so the pipe never
        # silently fails on an unrecognised source codec.
        encoder = 'libx264'
    is_nvenc = encoder.endswith('_nvenc')

    cmd: List[str] = ['ffmpeg', '-y', '-loglevel', 'error']

    # Raw video input from stdin
    cmd.extend([
        '-f', 'rawvideo',
        '-pixel_format', 'bgr24',
        '-video_size', f'{int(width)}x{int(height)}',
        '-framerate', f'{float(fps):.6f}',
        '-i', '-',
    ])

    include_grain = bool(grain_video_path and os.path.exists(grain_video_path))
    if grain_video_path and not include_grain:
        print(f"Grain video not found, skipping grain overlay: {grain_video_path}")
    applied_grain = enable_fx and include_grain

    if applied_grain:
        cmd.extend(['-stream_loop', '-1', '-i', grain_video_path])

    has_audio = bool(settings.get('has_audio'))
    if has_audio:
        cmd.extend(['-i', source_settings_video_path])

    if enable_fx:
        if legacy_pipeline:
            filter_complex, video_map = build_full_filter_chain(
                include_grain=applied_grain,
                width=int(width), height=int(height),
            )
        else:
            filter_complex, video_map = build_post_rppg_filter_chain(
                include_grain=applied_grain,
                width=int(width), height=int(height),
            )
        cmd.extend(['-filter_complex', filter_complex, '-map', video_map])
    else:
        cmd.extend(['-map', '0:v:0'])

    cmd.extend(['-c:v', encoder])

    if is_nvenc:
        # p5 = balanced speed/quality; vbr rate-control matches the source
        # bit-rate constraint. Same params used by the file-input encoder.
        cmd.extend(['-preset', 'p5', '-tune', 'hq', '-rc', 'vbr'])

    bit_rate = settings.get('bit_rate')
    if bit_rate and str(bit_rate).isdigit():
        # v6: optional multiplier so the user can override low source bitrates
        # (AI-generated inputs especially) that quantise away the subtle
        # rPPG modulation. mult=1.0 preserves v5 source-matching behaviour.
        effective_br = int(int(bit_rate) * max(0.1, float(output_bitrate_mult)))
        cmd.extend(['-b:v', str(effective_br)])
        if abs(output_bitrate_mult - 1.0) > 1e-6:
            cmd.extend(['-maxrate', str(int(effective_br * 1.5)),
                        '-bufsize', str(int(effective_br * 2))])

    pix_fmt = settings.get('pix_fmt')
    if pix_fmt:
        cmd.extend(['-pix_fmt', pix_fmt])

    profile = normalize_encoder_profile(settings.get('profile'), encoder)
    if profile:
        cmd.extend(['-profile:v', profile])

    if has_audio:
        audio_input_index = 2 if applied_grain else 1
        cmd.extend(['-map', f'{audio_input_index}:a?', '-c:a', 'copy'])
    else:
        cmd.append('-an')

    if output_path.lower().endswith(('.mp4', '.mov')):
        cmd.extend(['-movflags', '+faststart'])

    if applied_grain:
        cmd.append('-shortest')

    cmd.append(output_path)

    # SpooledTemporaryFile keeps ffmpeg's stderr in memory until ~1 MB then
    # spills to disk; that avoids both terminal-spam on success and
    # pipe-full deadlocks on long runs (subprocess.PIPE has a small kernel
    # buffer that can stall the writer if not actively drained).
    stderr_buffer = tempfile.SpooledTemporaryFile(max_size=1024 * 1024, mode='w+b')
    proc = subprocess.Popen(
        cmd,
        stdin=subprocess.PIPE,
        stderr=stderr_buffer,
        stdout=subprocess.DEVNULL,
    )
    return proc, stderr_buffer, cmd


def finalize_video_with_source_quality(temp_video_path: str, source_video_path: str, output_path: str,
                                       grain_video_path: Optional[str] = None,
                                       enable_final_fx: bool = True,
                                       remove_input_on_success: bool = True,
                                       keep_input_on_failure: bool = False,
                                       legacy_pipeline: bool = False) -> bool:
    """Encode processed frames to match the source codec/bitrate/container.

    v5: uses post-rPPG filter chain by default (no deband -- that's a separate pre-step).
    Set legacy_pipeline=True to use the v4.1 combined chain.
    """
    builder = build_full_filter_chain if legacy_pipeline else build_post_rppg_filter_chain
    ok = _run_ffmpeg_encode(
        temp_video_path, source_video_path, output_path,
        filter_builder=builder,
        grain_video_path=grain_video_path,
        enable_fx=enable_final_fx,
        remove_input_on_success=remove_input_on_success,
        keep_input_on_failure=keep_input_on_failure,
    )
    if ok:
        settings = probe_video_stream_settings(source_video_path)
        print(
            f"Finalized output: codec={settings.get('codec_name')}, "
            f"bitrate={settings.get('bit_rate') or 'unknown'}, "
            f"format={settings.get('format_name') or 'unknown'}, "
            f"final_fx={'on' if enable_final_fx else 'off'}, "
            f"grain={'on' if grain_video_path and os.path.exists(str(grain_video_path)) else 'off'}"
        )
    return ok


def apply_deband_preprocess(video_path: str, source_video_path: str) -> Optional[str]:
    """Apply a very light deband filter as the first processing step (v5 pipeline).

    Returns the path to the debanded intermediate video, or None on failure.
    """
    debanded_path = create_temp_video_path(video_path, suffix='.deband')
    ok = _run_ffmpeg_encode(
        video_path, source_video_path, debanded_path,
        filter_builder=build_deband_only_filter,
        grain_video_path=None,
        enable_fx=True,
        remove_input_on_success=False,
        keep_input_on_failure=True,
    )
    if ok and os.path.exists(debanded_path):
        print("Deband pre-processing applied successfully.")
        return debanded_path
    if os.path.exists(debanded_path):
        os.remove(debanded_path)
    print("Deband pre-processing failed, continuing with original input.")
    return None


@contextmanager
def suppress_stdout_stderr():
    """Context manager to suppress stdout and stderr."""
    with open(os.devnull, 'w') as devnull:
        old_stdout = sys.stdout
        old_stderr = sys.stderr
        sys.stdout = devnull
        sys.stderr = devnull
        try:
            yield
        finally:
            sys.stdout = old_stdout
            sys.stderr = old_stderr


# ---------------------------------------------------------------------------
# ROI Stabilizer (enhanced with velocity prediction from suggestion set 5)
# ---------------------------------------------------------------------------

class ROIStabilizer:
    """Temporal landmark stabilizer with Kalman-like velocity prediction."""

    def __init__(self, stabilization_frames: int = 15):
        self.landmark_history = deque(maxlen=stabilization_frames)
        self.velocity_history = deque(maxlen=stabilization_frames)

    def add_landmarks(self, landmarks) -> None:
        if landmarks is not None:
            if hasattr(landmarks, 'landmark'):
                arr = np.array([[lm.x, lm.y, lm.z] for lm in landmarks.landmark], dtype=np.float32)
            else:
                arr = np.asarray(landmarks, dtype=np.float32)

            if self.landmark_history:
                prev = self.landmark_history[-1]
                if prev.shape == arr.shape:
                    self.velocity_history.append(arr - prev)
            self.landmark_history.append(arr)

    def has_history(self) -> bool:
        return bool(self.landmark_history)

    def get_stabilized_landmarks(self) -> Optional[np.ndarray]:
        if not self.landmark_history:
            return None

        if len(self.landmark_history) < 3:
            return self.landmark_history[-1].copy()

        reference_shape = self.landmark_history[0].shape
        aligned = [arr for arr in self.landmark_history if arr.shape == reference_shape]
        if not aligned:
            return None

        weights = np.exp(np.linspace(-1.0, 0.0, len(aligned), dtype=np.float32))
        weights /= weights.sum()

        stabilized = np.zeros(reference_shape, dtype=np.float32)
        for arr, w in zip(aligned, weights):
            stabilized += arr * w

        # Light velocity-based prediction to reduce lag
        if self.velocity_history:
            mean_vel = np.mean(list(self.velocity_history), axis=0)
            if mean_vel.shape == reference_shape:
                stabilized += mean_vel * 0.1

        return stabilized

    def create_stabilized_mask(self, frame_shape: Tuple[int, ...], landmark_indices: List[int]) -> np.ndarray:
        mask = np.zeros(frame_shape[:2], dtype=np.uint8)
        landmark_array = self.get_stabilized_landmarks()
        if landmark_array is None:
            return mask

        height, width = frame_shape[:2]
        points = []
        for idx in landmark_indices:
            if idx >= len(landmark_array):
                continue
            x_norm = landmark_array[idx][0]
            y_norm = landmark_array[idx][1]
            if not np.isfinite(x_norm) or not np.isfinite(y_norm):
                continue
            x = int(np.clip(x_norm, 0.0, 1.0) * (width - 1))
            y = int(np.clip(y_norm, 0.0, 1.0) * (height - 1))
            points.append([x, y])

        if len(points) >= 3:
            pts = np.array(points, dtype=np.int32)
            # Convex hull guarantees a simple (non-self-intersecting) polygon
            # regardless of the order landmarks are listed in ROI_LANDMARKS,
            # and produces a fill that spans the full extent of the points.
            hull = cv2.convexHull(pts)
            cv2.fillPoly(mask, [hull], 255)

        return mask


# ---------------------------------------------------------------------------
# AdvancedRPPGInjector
# ---------------------------------------------------------------------------

class AdvancedRPPGInjector:
    """rPPG analyzer with CHROM + ICA + POS hybrid extraction,
    segmented SNR, and multi-dimensional phase coherence."""

    def __init__(self, face_model_path: Optional[str] = None,
                 landmark_stride: int = 1):
        self._landmark_backend = None
        self.face_mesh = None
        self.face_landmarker = None
        self.face_landmarker_model_path = None
        self._explicit_face_model_path = face_model_path

        self._init_face_landmark_detector()

        self.roi_landmarks = ROI_LANDMARKS
        self.roi_stabilizer = ROIStabilizer(stabilization_frames=15)
        # Run MediaPipe landmark detection only on every Nth frame; the
        # ROIStabilizer carries the shape between detections. Landmarks drift
        # <2 px/frame on a reasonably still face, so stride=3-5 gives a
        # measurable wall-clock saving at negligible quality cost.
        self.landmark_stride = max(1, int(landmark_stride))
        self._stride_counter = 0

    def reset_landmark_state(self) -> None:
        """Clear stride counter and stabilizer state.

        Called at the start of every video read (both apply_phase_aligned_pulses
        and extract_rgb_signals) so per-video detection state does not leak
        between input and output reads.
        """
        self._stride_counter = 0
        self.roi_stabilizer = ROIStabilizer(stabilization_frames=15)

    # ----- MediaPipe face-landmark detection (dual-backend, same as v4.1) -----

    def _resolve_face_landmarker_model_path(self) -> Optional[str]:
        if self._explicit_face_model_path and os.path.exists(self._explicit_face_model_path):
            return self._explicit_face_model_path

        env_model = os.environ.get('MEDIAPIPE_FACE_LANDMARKER_MODEL')
        if env_model and os.path.exists(env_model):
            return env_model

        candidate_paths = [
            'face_landmarker.task',
            'face_landmarker_v2_with_blendshapes.task',
            os.path.join('models', 'face_landmarker.task'),
            os.path.join('models', 'face_landmarker_v2_with_blendshapes.task'),
            os.path.join('input', 'face_landmarker.task'),
            os.path.join('input', 'face_landmarker_v2_with_blendshapes.task'),
        ]
        for candidate in candidate_paths:
            if os.path.exists(candidate):
                return candidate

        downloaded = self._auto_download_face_landmarker_model()
        if downloaded and os.path.exists(downloaded):
            return downloaded
        return None

    def _auto_download_face_landmarker_model(self) -> Optional[str]:
        url = os.environ.get('MEDIAPIPE_FACE_LANDMARKER_MODEL_URL', DEFAULT_FACE_LANDMARKER_MODEL_URL)
        destination = os.path.join('models', 'face_landmarker.task')
        destination_dir = os.path.dirname(destination) or '.'
        temp_path = destination + '.download'

        try:
            os.makedirs(destination_dir, exist_ok=True)
            print(f"FaceLandmarker model not found locally. Downloading from: {url}")
            with urllib.request.urlopen(url, timeout=90) as response, open(temp_path, 'wb') as out_file:
                while True:
                    chunk = response.read(1024 * 1024)
                    if not chunk:
                        break
                    out_file.write(chunk)

            if not os.path.exists(temp_path) or os.path.getsize(temp_path) == 0:
                return None

            os.replace(temp_path, destination)
            print(f"Downloaded FaceLandmarker model to: {destination}")
            return destination
        except Exception as exc:
            print(f"Auto-download of FaceLandmarker model failed: {exc}")
            print("You can place a .task model manually or use --face-model <path>.")
            return None
        finally:
            if os.path.exists(temp_path):
                try:
                    os.remove(temp_path)
                except OSError:
                    pass

    def _init_face_landmark_detector(self) -> None:
        if hasattr(mp, 'solutions') and hasattr(mp.solutions, 'face_mesh'):
            with suppress_stdout_stderr():
                self.mp_face_mesh = mp.solutions.face_mesh
                self.face_mesh = self.mp_face_mesh.FaceMesh(
                    static_image_mode=False,
                    max_num_faces=1,
                    refine_landmarks=True,
                    min_detection_confidence=0.5,
                    min_tracking_confidence=0.5,
                )
            self._landmark_backend = 'solutions'
            return

        model_path = self._resolve_face_landmarker_model_path()
        if not model_path:
            raise RuntimeError(
                "MediaPipe in this environment does not expose 'mp.solutions'. "
                "Place a FaceLandmarker model at one of: "
                "'face_landmarker.task', 'face_landmarker_v2_with_blendshapes.task', "
                "'models/face_landmarker.task', or set MEDIAPIPE_FACE_LANDMARKER_MODEL, "
                "or pass --face-model <path>."
            )

        # Try GPU delegate first; fall back to CPU on any failure. Windows
        # pip wheels often lack a GPU-enabled build so init can raise
        # RuntimeError at create_from_options time, not construction.
        #
        # macOS arm64 (2026-05-22): the GPU delegate SUCCEEDS at
        # create_from_options but SIGABRTs deep in
        # ImageCloneCalculator::Process during frame inference (the GL
        # context handoff between the Metal-backed delegate and the
        # mediapipe scheduler thread is unstable). Skip the GPU attempt
        # entirely on darwin so we land on the CPU path immediately,
        # avoiding the uncatchable abort that would crash the whole
        # rPPG subprocess. CPU is the "slower approach" the Windows
        # session intentionally validated as the macOS workflow.
        # `sys` is already imported at module level (line 28); use it
        # directly. (Subagent MEDIUM cleanup -- removed the redundant
        # `import sys as _sys_for_darwin` local alias.)
        delegate_used = 'CPU'
        self.face_landmarker = None
        _try_gpu = sys.platform != "darwin"
        try:
            if not _try_gpu:
                raise RuntimeError("macOS arm64 GPU delegate is unstable; using CPU")
            with suppress_stdout_stderr():
                gpu_base = mp.tasks.BaseOptions(
                    model_asset_path=model_path,
                    delegate=mp.tasks.BaseOptions.Delegate.GPU,
                )
                gpu_options = mp.tasks.vision.FaceLandmarkerOptions(
                    base_options=gpu_base,
                    running_mode=mp.tasks.vision.RunningMode.IMAGE,
                    num_faces=1,
                    min_face_detection_confidence=0.5,
                    min_face_presence_confidence=0.5,
                    min_tracking_confidence=0.5,
                    output_face_blendshapes=False,
                    output_facial_transformation_matrixes=False,
                )
                self.face_landmarker = mp.tasks.vision.FaceLandmarker.create_from_options(gpu_options)
            delegate_used = 'GPU'
        except Exception as gpu_exc:
            if _try_gpu:
                print(f"MediaPipe GPU delegate unavailable ({type(gpu_exc).__name__}: {gpu_exc}); using CPU.")
            else:
                print("MediaPipe: forcing CPU delegate on macOS arm64 (GPU path SIGABRTs in frame inference).")
            with suppress_stdout_stderr():
                base_options = mp.tasks.BaseOptions(model_asset_path=model_path)
                options = mp.tasks.vision.FaceLandmarkerOptions(
                    base_options=base_options,
                    running_mode=mp.tasks.vision.RunningMode.IMAGE,
                    num_faces=1,
                    min_face_detection_confidence=0.5,
                    min_face_presence_confidence=0.5,
                    min_tracking_confidence=0.5,
                    output_face_blendshapes=False,
                    output_facial_transformation_matrixes=False,
                )
                self.face_landmarker = mp.tasks.vision.FaceLandmarker.create_from_options(options)

        self.face_landmarker_model_path = model_path
        self._landmark_backend = 'tasks'
        print(f"Using MediaPipe Tasks FaceLandmarker model: {model_path} [{delegate_used}]")

    def _detect_landmarks_array(self, rgb_frame: np.ndarray) -> Optional[np.ndarray]:
        if self._landmark_backend == 'solutions':
            with suppress_stdout_stderr():
                results = self.face_mesh.process(rgb_frame)
            if not results.multi_face_landmarks:
                return None
            return np.array(
                [[lm.x, lm.y, lm.z] for lm in results.multi_face_landmarks[0].landmark],
                dtype=np.float32,
            )

        if self._landmark_backend == 'tasks':
            with suppress_stdout_stderr():
                mp_image = mp.Image(image_format=mp.ImageFormat.SRGB, data=rgb_frame)
                result = self.face_landmarker.detect(mp_image)
            if not result.face_landmarks:
                return None
            return np.array(
                [[lm.x, lm.y, lm.z] for lm in result.face_landmarks[0]],
                dtype=np.float32,
            )

        return None

    # ----- ROI extraction -----

    def extract_facial_rois(self, frame: np.ndarray) -> Tuple[Dict[str, np.ndarray], Dict[str, np.ndarray]]:
        # Only run MediaPipe detection on stride-matched frames; between those
        # the stabilizer's history keeps serving the most recent shape.
        if self._stride_counter % self.landmark_stride == 0:
            rgb_frame = cv2.cvtColor(frame, cv2.COLOR_BGR2RGB)
            landmarks = self._detect_landmarks_array(rgb_frame)
            if landmarks is not None:
                self.roi_stabilizer.add_landmarks(landmarks)
        self._stride_counter += 1

        if not self.roi_stabilizer.has_history():
            return {}, {}

        rois = {}
        roi_masks = {}
        for roi_name, landmark_indices in self.roi_landmarks.items():
            mask = self.roi_stabilizer.create_stabilized_mask(frame.shape, landmark_indices)
            if np.any(mask):
                roi_pixels = cv2.bitwise_and(frame, frame, mask=mask)
                rois[roi_name] = roi_pixels
                roi_masks[roi_name] = mask

        return rois, roi_masks

    def extract_rgb_signals(self, video_path: str, fps: float = None) -> Tuple[Dict[str, Dict[str, np.ndarray]], float]:
        self.reset_landmark_state()
        cap = cv2.VideoCapture(video_path)

        if fps is None:
            fps = float(cap.get(cv2.CAP_PROP_FPS))
        if fps <= 0:
            fps = 30.0

        roi_signals = {roi: {'R': [], 'G': [], 'B': []} for roi in self.roi_landmarks}

        while True:
            ret, frame = cap.read()
            if not ret:
                break

            rois, _ = self.extract_facial_rois(frame)

            for roi_name, roi_img in rois.items():
                if roi_img.size > 0:
                    gray_mask = cv2.cvtColor(roi_img, cv2.COLOR_BGR2GRAY) > 0
                    if np.any(gray_mask):
                        mean_rgb = cv2.mean(roi_img, mask=gray_mask.astype(np.uint8))
                        roi_signals[roi_name]['B'].append(mean_rgb[0])
                        roi_signals[roi_name]['G'].append(mean_rgb[1])
                        roi_signals[roi_name]['R'].append(mean_rgb[2])

        cap.release()

        for roi_name in roi_signals:
            for ch in ['R', 'G', 'B']:
                roi_signals[roi_name][ch] = np.array(roi_signals[roi_name][ch])

        return roi_signals, fps

    # ----- Signal extraction methods -----

    def apply_chrom_method(self, rgb_signals: Dict[str, np.ndarray]) -> np.ndarray:
        """CHROM method for motion-robust rPPG extraction."""
        R, G, B = rgb_signals['R'], rgb_signals['G'], rgb_signals['B']
        if len(R) == 0:
            return np.array([])

        R = R - np.mean(R)
        G = G - np.mean(G)
        B = B - np.mean(B)

        X = 3 * R - 2 * G
        Y = 1.5 * R + G - 1.5 * B

        alpha = np.std(X) / (np.std(Y) + 1e-8)
        return X - alpha * Y

    def apply_pos_method(self, rgb_signals: Dict[str, np.ndarray]) -> np.ndarray:
        """POS (Plane-Orthogonal-to-Skin) method (Wang et al., 2017)."""
        R, G, B = rgb_signals['R'], rgb_signals['G'], rgb_signals['B']
        if len(R) == 0:
            return np.array([])

        # Temporal normalisation
        Rn = R / (np.mean(R) + 1e-8)
        Gn = G / (np.mean(G) + 1e-8)
        Bn = B / (np.mean(B) + 1e-8)

        S1 = Gn - Bn
        S2 = Gn + Bn - 2.0 * Rn

        alpha = np.std(S1) / (np.std(S2) + 1e-8)
        return S1 + alpha * S2

    def apply_ica_method(self, rgb_signals: Dict[str, np.ndarray]) -> np.ndarray:
        """ICA-based signal extraction using FastICA on demeaned RGB channels.

        Falls back to CHROM if scikit-learn is not installed.
        """
        if not HAS_SKLEARN:
            return self.apply_chrom_method(rgb_signals)

        R, G, B = rgb_signals['R'], rgb_signals['G'], rgb_signals['B']
        if len(R) < 30:
            return np.array([])

        try:
            ica_input = np.vstack([
                R - np.mean(R),
                G - np.mean(G),
                B - np.mean(B),
            ]).T

            ica = FastICA(n_components=3, whiten='unit-variance', max_iter=500)
            sources = ica.fit_transform(ica_input)

            # Select the component with max spectral power in the physiological range
            best_idx = np.argmax([self._psd_band_power(sources[:, i]) for i in range(3)])
            return sources[:, best_idx]
        except Exception:
            return self.apply_chrom_method(rgb_signals)

    def _psd_band_power(self, sig: np.ndarray, fs: float = 30.0) -> float:
        """Power in the 0.7-4.0 Hz physiological band."""
        if len(sig) < 30:
            return 0.0
        f, psd = signal.welch(sig, fs=fs, nperseg=min(len(sig), 128))
        band = (f >= 0.7) & (f <= 4.0)
        if not np.any(band) or _integrate is None:
            return 0.0
        return float(_integrate(psd[band], f[band]))

    def extract_hybrid_signal(self, rgb_signals: Dict[str, np.ndarray]) -> np.ndarray:
        """Hybrid CHROM + ICA + POS signal extraction.

        Weights: 0.4 ICA, 0.35 CHROM, 0.25 POS (ICA best for motion rejection).
        Falls back gracefully if any method fails.
        """
        chrom = self.apply_chrom_method(rgb_signals)
        pos = self.apply_pos_method(rgb_signals)
        ica = self.apply_ica_method(rgb_signals)

        # Sign-align POS and ICA to CHROM (reference) before blending.
        # POS normalises by per-channel mean (R/mean(R)) while CHROM uses
        # demeaning (R - mean(R)), so POS can extract opposite-polarity
        # signals for ROIs with different mean RGB balance (e.g. forehead/chin
        # vs cheeks/nose).  Flip sign if anti-correlated.
        if len(chrom) > 0:
            ref_len = len(chrom)
            if len(pos) > 0:
                n = min(len(pos), ref_len)
                if np.corrcoef(pos[:n], chrom[:n])[0, 1] < 0:
                    pos = -pos
            if len(ica) > 0:
                n = min(len(ica), ref_len)
                if np.corrcoef(ica[:n], chrom[:n])[0, 1] < 0:
                    ica = -ica

        signals = []
        weights = []

        if len(ica) > 0:
            signals.append(ica)
            weights.append(0.4)
        if len(chrom) > 0:
            signals.append(chrom)
            weights.append(0.35)
        if len(pos) > 0:
            signals.append(pos)
            weights.append(0.25)

        if not signals:
            return np.array([])

        # Align lengths
        min_len = min(len(s) for s in signals)
        signals = [s[:min_len] for s in signals]

        # Normalise each signal before blending
        normed = []
        for s in signals:
            std = np.std(s) + 1e-8
            normed.append((s - np.mean(s)) / std)

        total_w = sum(weights)
        blended = np.zeros(min_len)
        for s, w in zip(normed, weights):
            blended += s * (w / total_w)

        return blended

    # ----- Filters -----

    def bandpass_filter(self, signal_data: np.ndarray, lowcut: float = 0.7,
                        highcut: float = 4.0, fs: float = 30.0, order: int = 4) -> np.ndarray:
        if len(signal_data) < 3 * order:
            return signal_data
        nyquist = fs / 2
        low = lowcut / nyquist
        high = highcut / nyquist
        b, a = butter(order, [low, high], btype='band')
        return filtfilt(b, a, signal_data)

    # ----- SNR computation -----

    def compute_snr(self, sig: np.ndarray, fs: float = 30.0) -> float:
        """Basic global SNR in dB (kept as a helper)."""
        if len(sig) < fs * 4:
            return 0.0

        nper = int(min(len(sig), fs * 4))
        f, Pxx = signal.welch(sig, fs=fs, window='hann', nperseg=nper, noverlap=nper // 2, detrend='constant')
        f, Pxx = f[f > 0], Pxx[f > 0]

        sig_band = (f >= 0.7) & (f <= 4.0)
        noise_band = ((f >= 0.2) & (f < 0.7)) | ((f > 4.0) & (f <= 15.0))

        if _integrate is None:
            return 0.0
        P_sig = _integrate(Pxx[sig_band], f[sig_band]) if np.any(sig_band) else 0.0
        P_noise = _integrate(Pxx[noise_band], f[noise_band]) if np.any(noise_band) else 1e-8

        if P_sig <= 0 or P_noise <= 0:
            return 0.0
        return 10.0 * np.log10(P_sig / P_noise)

    def compute_segmented_snr(self, signal_data: np.ndarray, fs: float = 30.0,
                               segment_length: float = 4.0) -> Dict:
        """Segmented SNR with motion artifact detection and temporal consistency.

        Returns a dict with: snr, motion_artifacts, temporal_consistency, segment_snr_stdev.

        Uses 4-second segments (was 2s) for reliable spectral estimation (~4-5 beat
        cycles per segment at typical HR).

        Motion artifact detection uses Hilbert envelope outliers instead of
        frame-to-frame diff (which falsely flags normal cardiac oscillation).
        """
        if len(signal_data) < fs * segment_length:
            return {'snr': 0.0, 'motion_artifacts': 1.0, 'temporal_consistency': 0.0, 'segment_snr_stdev': 0.0}

        segment_samples = int(fs * segment_length)
        num_segments = len(signal_data) // segment_samples

        snr_values = []

        for i in range(num_segments):
            start = i * segment_samples
            end = start + segment_samples
            segment = signal_data[start:end]

            f, Pxx = signal.welch(segment, fs=fs, nperseg=min(len(segment), 128))
            sig_band = (f >= 0.7) & (f <= 4.0)
            noise_band = ((f >= 0.2) & (f < 0.7)) | ((f > 4.0) & (f <= 15.0))

            if np.any(sig_band) and np.any(noise_band) and _integrate is not None:
                P_sig = _integrate(Pxx[sig_band], f[sig_band])
                P_noise = _integrate(Pxx[noise_band], f[noise_band])
                if P_noise > 0 and P_sig > 0:
                    snr_values.append(10.0 * np.log10(P_sig / P_noise))

        # Motion artifact detection via Hilbert envelope outliers.
        # Real motion artifacts cause sudden amplitude spikes; normal cardiac
        # oscillation does not.  Flag frames where envelope > 3x median.
        analytic = hilbert(signal_data)
        envelope = np.abs(analytic)
        median_env = np.median(envelope) + 1e-8
        motion_fraction = float(np.mean(envelope > 3.0 * median_env))

        avg_snr = float(np.mean(snr_values)) if snr_values else 0.0
        snr_std = float(np.std(snr_values)) if snr_values else 0.0
        temporal_consistency = 1.0 - snr_std / (abs(avg_snr) + 1e-8) if snr_values else 0.0

        return {
            'snr': avg_snr,
            'motion_artifacts': motion_fraction,
            'temporal_consistency': max(0.0, min(1.0, temporal_consistency)),
            'segment_snr_stdev': snr_std,
        }

    # ----- Phase coherence -----

    def compute_phase_coherence(self, signals: List[np.ndarray], fs: float = 30.0) -> float:
        """Phase coherence in degrees via cross-spectral density at the cardiac peak.

        Uses scipy.signal.csd to get the phase relationship at the dominant
        frequency directly, avoiding Hilbert instantaneous-phase drift that
        inflates the metric on broadband signals.
        """
        if len(signals) < 2:
            return 0.0

        min_len = min(len(s) for s in signals)
        if min_len < 30:
            return 0.0

        nperseg = min(min_len, 256)
        truncated = [s[:min_len] for s in signals]

        # Find shared dominant cardiac frequency from averaged PSD
        avg_psd = None
        f_ref = None
        for s in truncated:
            f, psd = signal.welch(s, fs=fs, nperseg=nperseg)
            if avg_psd is None:
                avg_psd = psd.copy()
                f_ref = f
            else:
                avg_psd += psd
        avg_psd /= len(truncated)

        valid = (f_ref >= 0.7) & (f_ref <= 4.0)
        if not np.any(valid):
            return 0.0
        valid_indices = np.where(valid)[0]
        peak_local = np.argmax(avg_psd[valid])
        peak_idx = valid_indices[peak_local]

        # Pairwise cross-spectral phase at the cardiac peak
        phase_diffs = []
        for i in range(len(truncated)):
            for j in range(i + 1, len(truncated)):
                f_csd, Pxy = signal.csd(truncated[i], truncated[j], fs=fs, nperseg=nperseg)
                phase_diff = abs(np.angle(Pxy[peak_idx]))
                phase_diffs.append(phase_diff)

        if not phase_diffs:
            return 0.0
        return float(np.mean(phase_diffs) * 180.0 / np.pi)

    def compute_advanced_phase_coherence(self, signals: List[np.ndarray], fs: float = 30.0) -> Dict:
        """Multi-dimensional phase analysis: cross-correlation, physiological sync, harmonic alignment."""
        empty = {'coherence': 0.0, 'physiological_sync': 0.0, 'harmonic_alignment': 0.0, 'overall_score': 0.0}
        if len(signals) < 2:
            return empty

        filtered = [self.bandpass_filter(s, 0.7, 4.0, fs) for s in signals if len(s) > 0]
        if len(filtered) < 2:
            return empty

        min_len = min(len(s) for s in filtered)
        if min_len == 0:
            return empty
        truncated = [s[:min_len] for s in filtered]

        normed = []
        for s in truncated:
            std = np.std(s) + 1e-8
            normed.append((s - np.mean(s)) / std)

        # 1. Pairwise cross-correlation peaks
        corr_peaks = []
        for i in range(len(normed)):
            for j in range(i + 1, len(normed)):
                corr = np.correlate(normed[i], normed[j], mode='full')
                peak = float(np.max(np.abs(corr)) / len(normed[i]))
                corr_peaks.append(peak)
        avg_coherence = float(np.mean(corr_peaks)) if corr_peaks else 0.0

        # 2. Physiological synchronisation (heart-rate matching across ROIs)
        hr_values = []
        for s in normed:
            f, psd = signal.welch(s, fs=fs, nperseg=min(len(s), 128))
            valid = (f >= 0.7) & (f <= 4.0)
            if np.any(valid):
                peak_idx = np.argmax(psd[valid])
                hr_values.append(float(f[valid][peak_idx] * 60.0))
        hr_consistency = 1.0 - (np.std(hr_values) / (np.mean(hr_values) + 1e-8)) if hr_values else 0.0

        # 3. Harmonic alignment (natural 2nd harmonic should be 10-50% of fundamental)
        # Compare amplitude ratios (sqrt of PSD) since harmonic content in
        # physiology and our injection is defined in amplitude domain.
        # Target: 0.3 amplitude ratio → score 1.0, decays linearly outside [0.1, 0.5].
        harmonic_scores = []
        for s in normed:
            f, psd = signal.welch(s, fs=fs, nperseg=min(len(s), 128))
            valid = (f >= 0.7) & (f <= 4.0)
            if np.any(valid):
                fund_power = np.max(psd[valid])
                fund_freq = f[valid][np.argmax(psd[valid])]
                harmonic_freq = 2.0 * fund_freq
                harm_idx = np.argmin(np.abs(f - harmonic_freq))
                if harm_idx < len(psd):
                    # Amplitude ratio = sqrt(power ratio)
                    amp_ratio = np.sqrt(psd[harm_idx] / (fund_power + 1e-8))
                    # Score: 1.0 when ratio is 0.3, decaying away from that
                    score = max(0.0, 1.0 - abs(amp_ratio - 0.3) / 0.3) if amp_ratio <= 1.0 else 0.0
                    harmonic_scores.append(score)
        harmonic_alignment = float(np.mean(harmonic_scores)) if harmonic_scores else 0.0

        overall = avg_coherence * 0.4 + max(0.0, hr_consistency) * 0.4 + harmonic_alignment * 0.2
        return {
            'coherence': avg_coherence,
            'physiological_sync': max(0.0, float(hr_consistency)),
            'harmonic_alignment': harmonic_alignment,
            'overall_score': overall,
        }

    # ----- Phase info extraction -----

    def extract_phase_info(self, signal_data: np.ndarray, fs: float = 30.0) -> Dict:
        if len(signal_data) == 0:
            return {'phase': np.array([]), 'frequency': 0.0, 'amplitude': 0.0}

        analytic = hilbert(signal_data)
        phase = np.angle(analytic)
        amplitude = np.abs(analytic)

        f, psd = signal.welch(signal_data, fs=fs, nperseg=min(len(signal_data), 256))
        valid_range = (f >= 0.7) & (f <= 4.0)
        if np.any(valid_range):
            dominant_freq = f[valid_range][np.argmax(psd[valid_range])]
        else:
            dominant_freq = randomHr / 60.0

        return {
            'phase': phase,
            'frequency': dominant_freq,
            'amplitude': float(np.mean(amplitude)),
        }

    # ----- Existing-pulse alignment (boost mode) -----

    def detect_pulse_alignment(self, results: Dict,
                                min_baseline_snr_db: float = 2.0,
                                hr_consistency_tolerance_bpm: float = 10.0,
                                hr_consistency_min_snr_db: float = 4.0
                                ) -> Optional[Dict[str, Any]]:
        """Extract HR + per-ROI starting phase from a baseline analysis.

        Returns None when detection is unreliable - e.g. baseline SNR too
        low, detected HR outside physiological range, no ROI results, or
        cross-ROI HR disagreement larger than the tolerance - so callers
        can fall back to random pulse generation.

        Used by --boost-existing-pulse mode: instead of injecting a pulse
        at a random heart rate that has to overpower the natural rPPG
        signal already in the video, we inject a pulse that matches the
        natural signal's HR and starting phase per ROI. Constructive
        interference amplifies the existing signal rather than competing
        with it.

        v5.7: added the HR-consistency gate. Run-7 saw boost-mode trigger
        a phase spike when ROI HRs disagreed - our synthetic pulse used
        the best-ROI's HR for ALL ROIs, which dephased against any ROI
        whose natural pulse was at a different rate. The gate checks
        every ROI with at least hr_consistency_min_snr_db SNR has its HR
        within tolerance of the reference (best-SNR) ROI; if not, we
        cannot reliably align across ROIs and fall back.
        """
        roi_results = results.get('roi_results', {})
        if not roi_results:
            return None

        # Pick the highest-SNR ROI as the reference for HR. Other ROIs
        # might have detected slightly different dominant frequencies
        # under noise; the best-SNR ROI's reading is most reliable.
        best_roi_name, best_roi = max(
            roi_results.items(),
            key=lambda kv: kv[1].get('snr', -float('inf')),
        )
        best_snr = float(best_roi.get('snr', -float('inf')))
        if best_snr < min_baseline_snr_db:
            return None

        detected_hr = float(best_roi.get('heart_rate', 0.0))
        if not (40.0 <= detected_hr <= 180.0):  # physiological sanity
            return None

        # Cross-ROI HR consistency check. If any reasonably-clean ROI has
        # an HR more than tolerance BPM away from the reference, the
        # baseline does not have a single coherent rhythm and aligning a
        # synthetic pulse to one ROI's HR will dephase against the others.
        for roi_name, roi_data in roi_results.items():
            roi_snr = float(roi_data.get('snr', -float('inf')))
            if roi_snr < hr_consistency_min_snr_db:
                continue
            other_hr = float(roi_data.get('heart_rate', 0.0))
            if not np.isfinite(other_hr) or other_hr <= 0:
                continue
            if abs(other_hr - detected_hr) > hr_consistency_tolerance_bpm:
                return None

        # Per-ROI starting phase from the Hilbert analytic phase at sample
        # 0. Used as the absolute starting offset for the synthetic pulse
        # (when align_to_zero=True is passed to generate_base_cardiac_signal).
        roi_phases: Dict[str, float] = {}
        for roi_name, roi_data in roi_results.items():
            phase_info = roi_data.get('phase_info', {})
            phase_arr = phase_info.get('phase')
            if phase_arr is None:
                continue
            try:
                phase0 = float(phase_arr[0])
            except (TypeError, IndexError):
                continue
            if not np.isfinite(phase0):
                continue
            roi_phases[roi_name] = phase0

        if not roi_phases:
            return None

        return {
            'target_hr': detected_hr,
            'roi_phases': roi_phases,
            'reference_roi': best_roi_name,
            'baseline_snr_db': best_snr,
        }

    # ----- Face-coherence scoring (geometry + eye stability) -----

    def compute_face_coherence_scores(self, video_path: str) -> Dict[str, float]:
        """Landmark-stability metrics independent of the rPPG pulse.

        geometry:        mean cosine similarity of the flattened landmark
                         vector between consecutive frames.  Drops when the
                         face warps or jumps between frames.
        eye_variations:  eye-center positional variance across the whole
                         video, normalised to a 0-1 score (1 = rock-steady).
                         Uses MediaPipe iris centres (indices 468, 473) when
                         refine_landmarks=True provides them, otherwise falls
                         back to eye-corner averages.

        Returns zeros if fewer than two frames yield landmarks.  Ported from
        utils/validate.py but built on the existing MediaPipe backend so no
        extra dependency or model file is required.
        """
        cap = cv2.VideoCapture(video_path)
        if not cap.isOpened():
            return {'geometry': 0.0, 'eye_variations': 0.0}

        width = int(cap.get(cv2.CAP_PROP_FRAME_WIDTH)) or 1
        height = int(cap.get(cv2.CAP_PROP_FRAME_HEIGHT)) or 1

        landmarks_list: list = []
        eye_centers: list = []

        while True:
            ret, frame = cap.read()
            if not ret:
                break
            rgb = cv2.cvtColor(frame, cv2.COLOR_BGR2RGB)
            lm = self._detect_landmarks_array(rgb)
            if lm is None:
                continue

            # Flattened 2D landmark vector for frame-to-frame coherence
            lm_px = lm[:, :2].copy()
            lm_px[:, 0] *= width
            lm_px[:, 1] *= height
            landmarks_list.append(lm_px.flatten())

            # Eye centre: iris centres (refined) or eye-corner midpoints
            if lm.shape[0] >= 478:
                right_iris = lm_px.reshape(-1, 2)[468]
                left_iris = lm_px.reshape(-1, 2)[473]
                eye_center = (right_iris + left_iris) * 0.5
            else:
                pts = lm_px.reshape(-1, 2)
                # 33/133 = left eye corners, 362/263 = right eye corners
                eye_center = np.mean(pts[[33, 133, 362, 263]], axis=0)
            eye_centers.append(eye_center)

        cap.release()

        if len(landmarks_list) < 2:
            return {'geometry': 0.0, 'eye_variations': 0.0}

        sims = []
        for i in range(1, len(landmarks_list)):
            a = landmarks_list[i - 1]
            b = landmarks_list[i]
            denom = float(np.linalg.norm(a) * np.linalg.norm(b))
            if denom > 0:
                sims.append(float(np.dot(a, b) / denom))
        geometry = float(np.mean(sims)) if sims else 0.0

        eye_arr = np.asarray(eye_centers, dtype=np.float64)
        eye_variance = float(np.var(eye_arr, axis=0).mean())
        eye_score = max(0.0, 1.0 - eye_variance / 100.0)

        return {'geometry': geometry, 'eye_variations': eye_score}

    # ----- Full video analysis -----

    def analyze_video(self, video_path: str,
                      roi_signals: Optional[Dict[str, Dict[str, np.ndarray]]] = None,
                      fps: Optional[float] = None) -> Dict:
        """Perform complete rPPG analysis using hybrid extraction and 2026 metrics.

        When *roi_signals* is provided (typically the cache returned by
        apply_phase_aligned_pulses), the expensive extract_rgb_signals pass -
        which re-reads the video and re-runs MediaPipe per frame - is skipped.
        The cache must follow the {roi: {'R','G','B': np.ndarray}} shape.
        """
        if roi_signals is None:
            print("Extracting facial ROIs and RGB signals...")
            roi_signals, fps = self.extract_rgb_signals(video_path)
        else:
            print("Using cached RGB signals from frame-processing pass.")
            if fps is None:
                cap = cv2.VideoCapture(video_path)
                fps = float(cap.get(cv2.CAP_PROP_FPS))
                cap.release()
                if fps <= 0:
                    fps = 30.0

        for roi_name, rgb_data in roi_signals.items():
            g = rgb_data['G']
            if len(g) > 0:
                print(f"  {roi_name}: mean={np.mean(g):.2f}, std={np.std(g):.2f}, "
                      f"min={np.min(g):.2f}, max={np.max(g):.2f}")
            else:
                print(f"  {roi_name}: No signal extracted")

        roi_results = {}
        all_filtered_signals = []

        for roi_name, rgb_data in roi_signals.items():
            if all(len(rgb_data[ch]) > 0 for ch in ['R', 'G', 'B']):
                # Per-channel raw-signal stats (for IterationHistory JSON export)
                rgb_stats = {}
                for ch in ('R', 'G', 'B'):
                    arr = rgb_data[ch]
                    rgb_stats[ch] = {
                        'mean': float(np.mean(arr)),
                        'std': float(np.std(arr)),
                        'min': float(np.min(arr)),
                        'max': float(np.max(arr)),
                    }

                # Use hybrid extraction (CHROM + ICA + POS)
                hybrid_signal = self.extract_hybrid_signal(rgb_data)

                if len(hybrid_signal) > 0:
                    filtered = self.bandpass_filter(hybrid_signal, fs=fps)
                    all_filtered_signals.append(filtered)

                    # Segmented SNR
                    seg_snr = self.compute_segmented_snr(filtered, fs=fps)
                    phase_info = self.extract_phase_info(filtered, fs=fps)

                    roi_results[roi_name] = {
                        'signal': filtered,
                        'snr': seg_snr['snr'],
                        'segmented_snr': seg_snr,
                        'phase_info': phase_info,
                        'heart_rate': phase_info['frequency'] * 60,
                        'rgb_stats': rgb_stats,
                    }

        if all_filtered_signals:
            phase_coherence_deg = self.compute_phase_coherence(all_filtered_signals, fs=fps)
            advanced_phase = self.compute_advanced_phase_coherence(all_filtered_signals, fs=fps)
            avg_snr = float(np.mean([r['snr'] for r in roi_results.values()]))
            best_roi = max(roi_results.items(), key=lambda x: x[1]['snr'])
            best_hr = best_roi[1]['heart_rate']

            # Aggregate temporal consistency and motion artifacts
            avg_temporal = float(np.mean([
                r['segmented_snr']['temporal_consistency'] for r in roi_results.values()
            ]))
            avg_motion = float(np.mean([
                r['segmented_snr']['motion_artifacts'] for r in roi_results.values()
            ]))

            # v6: FFT spectrum-realism score per ROI, averaged.
            # Cheap (~ms per ROI), only adds a row in the metric panel; does not
            # gate the v5 pass/fail. Stored in results so _rollup_metrics can
            # expose it as the 6th fitness signal.
            spectrum_realism = 0.0
            spectrum_per_roi = {}
            if V6_SPECTRUM_AVAILABLE and _v6_score_spectrum is not None:
                realism_vals = []
                for roi_name, roi_data in roi_results.items():
                    sig = roi_data.get('signal')
                    if sig is None or len(sig) < 16:
                        continue
                    try:
                        s = _v6_score_spectrum(sig, fps)
                        spectrum_per_roi[roi_name] = s.as_dict()
                        realism_vals.append(s.realism)
                        # Stash on the ROI dict too for export/inspection
                        roi_data['spectrum_realism'] = float(s.realism)
                    except Exception:
                        pass
                if realism_vals:
                    spectrum_realism = float(np.mean(realism_vals))

            passes = (
                target_snr_min <= avg_snr <= target_snr_max
                and phase_coherence_deg <= target_phase_coherence
                and avg_temporal >= target_temporal_consistency
                and target_motion_artifacts_min <= avg_motion <= target_motion_artifacts
                and advanced_phase.get('harmonic_alignment', 0.0) >= target_harmonic_alignment
            )

            return {
                'roi_results': roi_results,
                'global_snr': avg_snr,
                'phase_coherence': phase_coherence_deg,
                'advanced_phase': advanced_phase,
                'heart_rate': best_hr,
                'fps': fps,
                'temporal_consistency': avg_temporal,
                'motion_artifacts': avg_motion,
                'spectrum_realism': spectrum_realism,
                'spectrum_per_roi': spectrum_per_roi,
                'passes_test': passes,
            }
        else:
            return {
                'roi_results': {},
                'global_snr': 0.0,
                'phase_coherence': 180.0,
                'advanced_phase': {'coherence': 0.0, 'physiological_sync': 0.0,
                                   'harmonic_alignment': 0.0, 'overall_score': 0.0},
                'heart_rate': 0.0,
                'fps': fps,
                'temporal_consistency': 0.0,
                'motion_artifacts': 1.0,
                'spectrum_realism': 0.0,
                'spectrum_per_roi': {},
                'passes_test': False,
            }

    # ----- Visualisation (kept for --analyze mode) -----

    def visualize_analysis(self, results: Dict):
        import matplotlib.pyplot as plt

        fig, axes = plt.subplots(3, 1, figsize=(12, 10))

        ax1 = axes[0]
        for roi_name, roi_data in results['roi_results'].items():
            sig = roi_data['signal']
            t = np.arange(len(sig)) / results['fps']
            ax1.plot(t, sig, label=f"{roi_name} (SNR: {roi_data['snr']:.1f}dB)")
        ax1.set_xlabel('Time (s)')
        ax1.set_ylabel('rPPG Signal')
        ax1.set_title('rPPG Signals from Different Facial ROIs')
        ax1.legend()
        ax1.grid(True, alpha=0.3)

        ax2 = axes[1]
        for roi_name, roi_data in results['roi_results'].items():
            sig = roi_data['signal']
            if len(sig) > 0:
                f, psd = signal.welch(sig, fs=results['fps'], nperseg=min(len(sig), 256))
                ax2.semilogy(f * 60, psd, label=roi_name)
        ax2.axvspan(42, 240, alpha=0.2, color='green', label='Physiological range')
        ax2.set_xlabel('Frequency (bpm)')
        ax2.set_ylabel('Power Spectral Density')
        ax2.set_title('Frequency Analysis')
        ax2.legend()
        ax2.grid(True, alpha=0.3)
        ax2.set_xlim([0, 300])

        ax3 = axes[2]
        ax3.axis('off')
        adv = results.get('advanced_phase', {})
        summary = (
            f"  Global Metrics:\n"
            f"  - Average SNR: {results['global_snr']:.2f} dB (target: {target_snr_min}-{target_snr_max} dB)\n"
            f"  - Phase Coherence: {results['phase_coherence']:.1f} deg (target: <= {target_phase_coherence} deg)\n"
            f"  - Temporal Consistency: {results.get('temporal_consistency', 0):.2f} (target: >= {target_temporal_consistency})\n"
            f"  - Motion Artifacts: {results.get('motion_artifacts', 0):.2f} (target: {target_motion_artifacts_min}-{target_motion_artifacts})\n"
            f"  - Harmonic Alignment: {adv.get('harmonic_alignment', 0):.2f} (target: >= {target_harmonic_alignment})\n"
            f"  - Heart Rate: {results['heart_rate']:.1f} bpm\n"
            f"  - Test Result: {'PASS' if results['passes_test'] else 'FAIL'}\n\n"
            f"  ROI-specific SNR:"
        )
        for roi_name, roi_data in results['roi_results'].items():
            summary += f"\n  - {roi_name}: {roi_data['snr']:.2f} dB"

        ax3.text(0.05, 0.95, summary, transform=ax3.transAxes,
                 fontsize=11, verticalalignment='top', fontfamily='monospace',
                 bbox=dict(boxstyle='round', facecolor='wheat', alpha=0.5))

        plt.tight_layout()
        plt.savefig('rppg_analysis_results.png', dpi=150, bbox_inches='tight')
        plt.show()


# ---------------------------------------------------------------------------
# Intelligent iteration support (Layers 1, 3, 4)
# ---------------------------------------------------------------------------

# Core v5 metrics + v6 spectrum_realism (only present when v6_spectrum_scorer
# is importable). The controller iterates over METRIC_KEYS for risk/benefit
# scoring, so a missing scorer must not advertise the extra metric.
METRIC_KEYS = ('snr', 'phase', 'temporal', 'motion', 'harmonic')
if V6_SPECTRUM_AVAILABLE:
    METRIC_KEYS = METRIC_KEYS + ('spectrum_realism',)

# v6: spectrum_realism pass threshold. Real videos score ~0.94-0.97 baseline;
# a well-injected AI video scores similarly. Below 0.85 indicates the
# extracted signal's spectrum is genuinely degraded (smeared peak,
# leaked DC, lost harmonic, etc.) — that's what we want the controller to
# avoid.
target_spectrum_realism = 0.85
# Score-function weight. User asked for high weight so v6-mode-on vs
# v6-mode-off shows a real behavioural difference. ~comparable to harmonic
# in the v5 score scale (-15 worst to +5 best).
V6_SPECTRUM_SCORE_WEIGHT = 25.0

# --- Iteration-tuning constants (v5.3) ---
# Three changes address the recurring pattern where iter 1 overshoots SNR
# and phase while collapsing motion_artifacts to 0.00, leaving iter 2-3 with
# no meaningful work left to do.
#
# A) Strength calibration: gain 0.5 -> 0.2, with a ±1 dB deadband and a
#    per-iteration delta cap at one registry step. See iterative_enhancement.
# B) Secondary-metric phase: for the first MAX_SECONDARY_PHASE_ITERATIONS
#    iterations, the controller is restricted to knobs whose primary_metric
#    is in SECONDARY_METRICS (temporal/harmonic/motion). SNR/phase knobs
#    unlock either when those three metrics pass or when the cap is hit.
# C) Motion floor guard: if motion < target_motion_artifacts_min at the
#    start of an iteration, bump the first knob in MOTION_GUARD_KNOBS that
#    has room to climb, before the controller picks.
#
# All three are disabled together by passing legacy_iteration_tuning=True
# (CLI: --legacy-iteration-tuning) to restore v5.2 behaviour.
SECONDARY_METRICS = ('temporal', 'harmonic', 'motion')
MAX_SECONDARY_PHASE_ITERATIONS = 3
STRENGTH_CALIBRATION_GAIN = 0.2
STRENGTH_CALIBRATION_DEADBAND_DB = 1.0
# v5.4: calibrator now targets target_snr_max - this offset (was
# target_snr_min + 2.0). With a tightened SNR band the lower-third comfort
# fired calibration on every healthy mid-to-high SNR baseline; tracking the
# upper end keeps already-good baselines alone. The user's empirical note:
# at SNR ~= snr_max the injected pulse becomes visible to the naked eye, so
# we want the calibrator's anchor sitting just below that visibility line.
STRENGTH_CALIBRATION_COMFORT_BELOW_MAX_DB = 1.0
# v5.4: cap controller step_multiplier at this value. v5.3 used (1, 3, 8,
# 20) for fast gap closure, but 4 runs of evidence showed x3+ multipliers
# consistently misfire when regression-blended slopes have drifted from
# registry defaults: h3_amp x8 in run-3, pulse_smoothing_sigma x3 and
# hb_g_mult x3 in run-4 each moved their target metric the wrong way
# despite a positive predicted benefit. Smaller steps let the next
# iteration's observation correct slope drift before damage compounds.
MAX_STEP_MULTIPLIER = 2
# v5.5: dynamic strength bounds for iterative runs. Initial strength and the
# per-run cap on strength are computed from baseline SNR + the registry's
# slope estimate, so a high-baseline video (e.g. kling at SNR=9.57, already
# in band) starts at strength.min instead of the 1.5x base seed that was
# overshooting. 0.7 on the initial means we land short of comfort and let
# the calibrator close the remaining gap over 1-2 iterations rather than
# overshooting on iter 1. 1.2 on the max gives a 20% headroom buffer for
# slope-estimation error.
DYNAMIC_STRENGTH_INITIAL_FRAC = 0.7
DYNAMIC_STRENGTH_MAX_SAFETY_FACTOR = 1.2
# v5.8/5.9: phase-emergency revert. Two trigger conditions, either of which
# unlocks the secondary phase gate; the second also reverts the LAST iter's
# knob change.
#   1. SEVERE: phase_now > PHASE_EMERGENCY_MULTIPLIER * target_phase_coherence.
#      Phase has drifted well past target with no obvious recent cause -
#      unlock the gate so primary phase knobs become eligible for corrective
#      search. v5.9 lowered the multiplier from 2.0 to 1.5 because 2x of
#      typical user targets (18 deg) was 36 deg, which the canonical run-10
#      blowup at 26.92 deg never reached, so the original threshold never
#      actually fired in practice.
#   2. REGRESSION: phase_now > target_phase_coherence (failing) AND last
#      iter caused a regression > PHASE_EMERGENCY_REGRESSION_FRAC of target.
#      Triggers a REVERT of the prior iter's knob change - cleaner than
#      letting the controller corrective-search, which historically destroys
#      other metrics in the process (memory: 10 runs of "phase metric never
#      recovers once spiked early"). The fraction-of-target form scales with
#      the user's tightness preferences.
PHASE_EMERGENCY_MULTIPLIER = 1.5
PHASE_EMERGENCY_REGRESSION_FRAC = 0.3
# v5.8: no-benefit-fallback breakthrough. The controller falls back to a
# least-impact knob when every meaningful candidate would breach
# PASS_SAFETY_MARGIN; that's a wasted iter. After this many CONSECUTIVE
# fallbacks, the next iter's controller call expands its margin to the
# breakthrough value to force a productive (if higher-risk) pick.
MAX_NO_BENEFIT_FALLBACK_ITERS = 0  # v5.10: was 1; run-11 showed breakthrough fired too late (iter 2 vs 1)
BREAKTHROUGH_PASS_SAFETY_MARGIN = 0.60  # v5.10b: was 0.40; run-12/13 showed 0.40 still insufficient
# v5.3: only envelope_burst_prob actually moves motion_artifacts. A synthetic
# test (clean pulse + bandpass + segmented SNR) showed roi_motion_noise stays
# motion=0.0000 even at sigma=10 because per-pixel Gaussian noise averages out
# across the ROI mean used by extract_rgb_signals. envelope_burst_prob now uses
# burst_amp_mult=4.0 (was 2.0) so the lever has actual reach within its bounds.
MOTION_GUARD_KNOBS = ('envelope_burst_prob',)


def _rollup_metrics(results: Dict) -> Dict[str, float]:
    """Extract the rollup metrics from an analyze_video result.

    v5 metrics (always present): snr, phase, temporal, motion, harmonic.
    v6 metric (present when v6_spectrum_scorer module is importable):
        spectrum_realism — FFT power-band realism score in [0, 1]. Higher is
        more pulse-like. Informational by default; the GuardedController does
        not target it as a pass/fail metric unless explicitly configured.
    """
    adv = results.get('advanced_phase', {})
    out = {
        'snr': float(results.get('global_snr', 0.0)),
        'phase': float(results.get('phase_coherence', 180.0)),
        'temporal': float(results.get('temporal_consistency', 0.0)),
        'motion': float(results.get('motion_artifacts', 1.0)),
        'harmonic': float(adv.get('harmonic_alignment', 0.0)),
    }
    if 'spectrum_realism' in results:
        out['spectrum_realism'] = float(results.get('spectrum_realism', 0.0))
    return out


def _per_roi_metrics(results: Dict) -> Dict[str, Dict[str, float]]:
    """Pull per-ROI metric snapshot (SNR, HR, segmented motion/temporal, rgb_stats)."""
    out = {}
    for roi_name, roi_data in results.get('roi_results', {}).items():
        seg = roi_data.get('segmented_snr', {})
        phase_info = roi_data.get('phase_info', {}) or {}
        out[roi_name] = {
            'snr': float(roi_data.get('snr', 0.0)),
            'heart_rate': float(roi_data.get('heart_rate', 0.0)),
            'motion': float(seg.get('motion_artifacts', 0.0)),
            'temporal': float(seg.get('temporal_consistency', 0.0)),
            'segment_snr_stdev': float(seg.get('segment_snr_stdev', 0.0)),
            'dominant_freq_hz': float(phase_info.get('frequency', 0.0)),
            'rgb_stats': roi_data.get('rgb_stats', {}),
        }
    return out


class IterationHistory:
    """Structured record of every iteration's knob settings and measured metrics.

    Layer 1 of the intelligent-iteration system.  Stored records feed the
    GuardedController's decision logic (Layer 3) and the regression-based
    adaptive step sizes (Layer 4).

    Each record has:
      - iteration:       int, 0 = baseline
      - params:          PulseParams snapshot (dict)
      - metrics:         5 rollup metrics (snr, phase, temporal, motion, harmonic)
      - per_roi:         per-ROI metric + RGB dict
      - delta_baseline:  metric diff vs. baseline (absent for baseline record)
      - delta_prev:      metric diff vs. previous iteration (absent for iter 0/1)
      - knob_change:     {knob_name, prev, new, delta} describing what the
                          controller changed this iteration (absent for baseline)
      - rationale:       human-readable reason for the change (string)
    """

    def __init__(self) -> None:
        self.records: List[Dict[str, Any]] = []
        # Top-level run metadata (e.g. iterate_from_baseline / boost_existing_pulse
        # flags + detected pulse alignment). Optional - empty dict means default
        # cumulative-iteration with random-pulse generation.
        self.metadata: Dict[str, Any] = {}

    def record(self, iteration: int, params: 'PulseParams', results: Dict,
               knob_change: Optional[Dict[str, Any]] = None,
               rationale: str = '',
               strength_calibration: Optional[Dict[str, Any]] = None,
               motion_guard: Optional[Dict[str, Any]] = None,
               phase_gate: Optional[Dict[str, Any]] = None,
               phase_emergency_revert: Optional[Dict[str, Any]] = None,
               breakthrough_margin: Optional[float] = None) -> Dict[str, Any]:
        metrics = _rollup_metrics(results)
        entry = {
            'iteration': iteration,
            'params': params.snapshot(),
            'metrics': metrics,
            'per_roi': _per_roi_metrics(results),
        }
        if iteration >= 1 and self.records:
            base = self.records[0]['metrics']
            prev = self.records[-1]['metrics']
            entry['delta_baseline'] = {k: metrics[k] - base[k] for k in METRIC_KEYS}
            entry['delta_prev'] = {k: metrics[k] - prev[k] for k in METRIC_KEYS}
        if knob_change:
            entry['knob_change'] = knob_change
        if strength_calibration:
            entry['strength_calibration'] = strength_calibration
        if motion_guard:
            entry['motion_guard'] = motion_guard
        if phase_gate:
            entry['phase_gate'] = phase_gate
        if phase_emergency_revert:
            entry['phase_emergency_revert'] = phase_emergency_revert
        if breakthrough_margin is not None:
            entry['breakthrough_margin'] = float(breakthrough_margin)
        if rationale:
            entry['rationale'] = rationale
        self.records.append(entry)
        return entry

    def baseline(self) -> Optional[Dict[str, Any]]:
        return self.records[0] if self.records else None

    def latest(self) -> Optional[Dict[str, Any]]:
        return self.records[-1] if self.records else None

    def observations_for(self, knob: str, metric: str) -> List[Tuple[float, float]]:
        """Return list of (knob_value, metric_value) pairs from all recorded
        iterations -- used by the regression slope estimator (Layer 4)."""
        points: List[Tuple[float, float]] = []
        for rec in self.records:
            params = rec.get('params', {})
            metrics = rec.get('metrics', {})
            if knob in params and metric in metrics:
                points.append((float(params[knob]), float(metrics[metric])))
        return points

    def to_dict(self) -> Dict[str, Any]:
        d: Dict[str, Any] = {
            'metric_keys': list(METRIC_KEYS),
            'records': self.records,
        }
        if self.metadata:
            d['metadata'] = self.metadata
        return d

    def export_json(self, path: str) -> None:
        with open(path, 'w', encoding='utf-8') as fp:
            json.dump(self.to_dict(), fp, indent=2, default=float)

    def export_metrics_summary(self, path: str) -> None:
        """Write a human-readable sidecar with just the 5 core metrics per
        iteration, so the iteration history can be skimmed at a glance.
        """
        data = self.to_dict()
        records = data.get('records', [])
        targets = (
            f"Targets: SNR {target_snr_min}-{target_snr_max} dB | "
            f"Phase <= {target_phase_coherence} deg | "
            f"Temporal >= {target_temporal_consistency} | "
            f"Motion {target_motion_artifacts_min}-{target_motion_artifacts} | "
            f"Harmonic >= {target_harmonic_alignment}"
        )
        lines = [
            f"# Iteration metrics summary ({len(records)} records)",
            f"# {targets}",
            '\t'.join(['Stage', 'SNR', 'Phase', 'Temp', 'Motion', 'Harm']),
        ]
        for rec in records:
            it = rec.get('iteration', '?')
            m = rec.get('metrics', {}) or {}
            if it == 0:
                stage = "Iter 0 baseline (pre-injection)"
            else:
                parts = [f"Iter {it}"]
                sc = rec.get('strength_calibration') or {}
                if sc:
                    parts.append(
                        f"strength: {sc.get('prev', '?'):.5f} -> "
                        f"{sc.get('new', '?'):.5f}"
                    )
                kc = rec.get('knob_change') or {}
                if kc:
                    parts.append(
                        f"{kc.get('knob', '?')}: "
                        f"{kc.get('prev', '?')} -> {kc.get('new', '?')}"
                    )
                elif not sc:
                    parts.append(rec.get('rationale', ''))
                stage = ' | '.join(parts)
            lines.append('\t'.join([
                stage,
                f"{m.get('snr', 0.0):.2f}",
                f"{m.get('phase', 0.0):.2f}",
                f"{m.get('temporal', 0.0):.3f}",
                f"{m.get('motion', 0.0):.3f}",
                f"{m.get('harmonic', 0.0):.3f}",
            ]))
        with open(path, 'w', encoding='utf-8') as fp:
            fp.write('\n'.join(lines) + '\n')


# -------------------- Layer 4: regression-refined slopes --------------------

def estimate_slope(history: IterationHistory, knob: str, metric: str) -> float:
    """Return Δmetric/Δknob slope.

    Uses least-squares linear regression across all history records when
    >=2 *distinct* knob values are observed; otherwise falls back to the
    hand-authored KNOB_REGISTRY effects slope.
    """
    fallback = float(KNOB_REGISTRY.get(knob, {}).get('effects', {}).get(metric, 0.0))
    pts = history.observations_for(knob, metric)
    if len(pts) < 2:
        return fallback
    xs = np.array([p[0] for p in pts], dtype=np.float64)
    ys = np.array([p[1] for p in pts], dtype=np.float64)
    if np.ptp(xs) < 1e-9:
        return fallback
    # np.polyfit(deg=1) returns [slope, intercept]
    try:
        slope = float(np.polyfit(xs, ys, 1)[0])
    except (np.linalg.LinAlgError, ValueError):
        return fallback
    # Blend regression slope with fallback proportional to sample count
    # (confidence grows with observations; caps at 80% regression weight).
    w_reg = min(0.8, 0.4 + 0.1 * (len(pts) - 2))
    # v5.10 fast-track: amplitude knobs with only 2 observations that already
    # deviate >1.5x from the registry are trusted immediately. Diagnostic
    # memory shows h2/h3/resp/mayer slopes are 2-3x under-calibrated after
    # just 2 obs; the default slow ramp (caps at 0.8 after 8 samples) lets
    # the blowout compound for several more iterations before self-correcting.
    _FAST_TRACK_KNOBS = frozenset({'h2_amp', 'h3_amp', 'resp_amp', 'mayer_amp'})
    if (len(pts) == 2
            and knob in _FAST_TRACK_KNOBS
            and abs(fallback) > 1e-6
            and abs(slope / fallback) > 1.5):
        w_reg = 0.95
    blended = slope * w_reg + fallback * (1.0 - w_reg)
    # v5.10b early-obs damping: with <4 history points, a single outlier
    # observation can flip the blended slope's sign or balloon its magnitude,
    # causing the controller to see the knob as helpful when it's actually
    # harmful (run-13: stochastic_jitter and mayer_amp both flipped sign with
    # 1-2 history points, producing phase blowouts the registry predicted as
    # reductions). Two guards hold the estimate in check until 4+ obs exist.
    if len(pts) < 4 and abs(fallback) > 1e-6:
        # Guard 1 – sign flip: blended flipped sign vs. registry → discard
        # regression and use registry (a 1-2-point sign flip is almost always
        # noise; don't act on it until enough observations confirm the reversal).
        if (blended * fallback) < 0:
            return fallback
        # Guard 2 – magnitude cap: clamp blended to 3× registry so an outlier
        # obs can't produce a large slope that soft-vetos every competitor.
        # v5.10e: lowered from 5× to 3× — run-16 strength was 4.4× off and
        # fell through the old 5× threshold, causing a +23.4° phase blowup.
        max_abs = 3.0 * abs(fallback)
        if abs(blended) > max_abs:
            blended = max_abs if blended > 0 else -max_abs
    return blended


# -------------------- Layer 3: guarded controller --------------------

class GuardedController:
    """Chooses one knob change per iteration.

    Strategy:
      1. Classify each metric as PASS or FAIL relative to its target.
      2. For every knob candidate, estimate the expected Δ on every metric
         (regression-refined slope * candidate step).
      3. Score each (knob, direction) pair:
           + benefit  = gap closed on FAIL metrics
           - risk    = predicted regression of PASS metrics past their threshold
      4. Return the best-scoring candidate, with step size bounded by
         KNOB_REGISTRY limits and how close we already are to the knob's edge.
    """

    # Maximum fraction of a passing metric's remaining slack that a
    # candidate may consume toward its failure threshold before the
    # controller piles on extra risk weighting (soft-veto).  0.15 = no
    # more than 15% of headroom eaten per iteration.
    PASS_SAFETY_MARGIN = 0.15
    # Multiplier applied to the over-margin portion of slack consumption.
    # Large enough to dominate benefit when the violation is substantial,
    # small enough that a marginal overrun plus high benefit can still win.
    PASS_SAFETY_PENALTY = 5.0
    # Benefit below this threshold is treated as effectively zero for log
    # purposes. The controller may still pick such a knob (it's the
    # least-impact candidate when all meaningful moves would breach the
    # PASS_SAFETY_MARGIN), but the rationale string will say so explicitly
    # rather than misleadingly claiming it's "targeting" the knob's
    # registered primary_metric.
    NO_BENEFIT_EPSILON = 0.01

    def __init__(self, targets: Dict[str, Any]) -> None:
        self.targets = targets

    # --- metric classification ---

    def metric_gap(self, metric: str, value: float) -> float:
        """Return a signed gap from target -- POSITIVE means we need to move
        the metric UP, NEGATIVE means it's currently beyond target (too high)
        or already comfortably passing.  Zero means on-target."""
        t = self.targets
        if metric == 'snr':
            lo, hi = t['snr_min'], t['snr_max']
            if value < lo:
                return lo - value
            if value > hi:
                return hi - value      # negative -> need to decrease
            return 0.0
        if metric == 'phase':
            return t['phase'] - value   # negative if currently too high
        if metric == 'motion':
            lo, hi = t['motion_min'], t['motion_max']
            if value < lo:
                return lo - value       # positive -> need more motion
            if value > hi:
                return hi - value       # negative -> need less
            return 0.0
        if metric == 'temporal':
            return t['temporal'] - value
        if metric == 'harmonic':
            return t['harmonic'] - value
        if metric == 'spectrum_realism':
            return t.get('spectrum_realism', 0.0) - value  # higher = better
        return 0.0

    def passes(self, metric: str, value: float) -> bool:
        t = self.targets
        if metric == 'snr':
            return t['snr_min'] <= value <= t['snr_max']
        if metric == 'phase':
            return value <= t['phase']
        if metric == 'motion':
            return t['motion_min'] <= value <= t['motion_max']
        if metric == 'temporal':
            return value >= t['temporal']
        if metric == 'harmonic':
            return value >= t['harmonic']
        if metric == 'spectrum_realism':
            return value >= t.get('spectrum_realism', 0.0)
        return True

    def fail_direction(self, metric: str, value: float) -> int:
        """+1 = need metric higher, -1 = need metric lower, 0 = passing."""
        gap = self.metric_gap(metric, value)
        if self.passes(metric, value):
            return 0
        return 1 if gap > 0 else -1

    # --- candidate generation ---

    def choose_next_change(self, params: 'PulseParams',
                           current_metrics: Dict[str, float],
                           history: IterationHistory,
                           allowed_knobs: Optional[set] = None,
                           pass_safety_margin_override: Optional[float] = None,
                           ) -> Tuple[Optional[str], Optional[float], str, Optional[Dict[str, Any]]]:
        """Return (knob_name, new_value, rationale, details).

        *details* is a dict with numeric fields describing the winning
        candidate: benefit, risk, score, step_multiplier, primary_metric.
        It is None when no change was made.

        When *allowed_knobs* is provided, only knobs in the set are considered.
        This is used by the secondary-metric phase gate (v5.3) to keep the
        controller focused on temporal/harmonic/motion knobs during the first
        few iterations before SNR/phase knobs unlock.
        """
        failing = {m: v for m, v in current_metrics.items() if not self.passes(m, v)}
        if not failing:
            return None, None, 'All metrics passing; no change.', None

        # Directional targets per metric
        directions = {m: self.fail_direction(m, current_metrics[m]) for m in current_metrics}

        # Precompute how much "slack" each passing metric has before it would fail
        # -- used to turn predicted deltas into risk scores.
        slack = {}
        for m, v in current_metrics.items():
            if not self.passes(m, v):
                continue
            if m == 'snr':
                slack[m] = min(v - self.targets['snr_min'],
                               self.targets['snr_max'] - v)
            elif m == 'phase':
                slack[m] = self.targets['phase'] - v
            elif m == 'motion':
                slack[m] = min(v - self.targets['motion_min'],
                               self.targets['motion_max'] - v)
            elif m == 'temporal':
                slack[m] = v - self.targets['temporal']
            elif m == 'harmonic':
                slack[m] = v - self.targets['harmonic']
            elif m == 'spectrum_realism':
                slack[m] = v - self.targets.get('spectrum_realism', 0.0)

        best: Tuple[float, Optional[str], Optional[float], str, Optional[Dict[str, Any]]] = (
            -float('inf'), None, None, '', None,
        )

        # Step multipliers: v5.3 used (1, 3, 8, 20) for fast gap closure; v5.4
        # caps at MAX_STEP_MULTIPLIER (default 2) because larger multipliers
        # repeatedly moved the target metric the WRONG WAY across multiple
        # runs - blended-slope estimation error gets amplified when steps are
        # large. Smaller steps slow gap closure but let the next iteration's
        # observation correct any drift before damage compounds.
        step_multipliers = tuple(range(1, MAX_STEP_MULTIPLIER + 1))

        for knob, spec in KNOB_REGISTRY.items():
            if allowed_knobs is not None and knob not in allowed_knobs:
                continue
            # v5.10b: lock mayer_amp out of controller eligibility when phase
            # is within 5° of target. Its +3000 phase slope makes any movement
            # a coin-flip at close range; the revert path cannot recover if the
            # resulting spike consumes the remaining iterations.
            if (knob == 'mayer_amp'
                    and abs(current_metrics.get('phase', 0.0)
                            - self.targets['phase']) < 5.0):
                continue
            cur = float(getattr(params, knob))
            step = float(spec['step'])
            # For each sign and each step-size multiplier, score a candidate.
            for sign in (+1, -1):
                # v5.10b: block reductions of h2_amp/h3_amp when harmonic is
                # already failing. Both have positive harmonic slopes; moving
                # them down while harmonic is below target compounds the deficit
                # without helping any other failing metric.
                if (knob in ('h2_amp', 'h3_amp')
                        and sign == -1
                        and self.metric_gap(
                            'harmonic',
                            current_metrics.get('harmonic', 0.0)) > 0):
                    continue
                # v5.10f: block sigma INCREASES when motion is below floor.
                # Higher sigma smooths the pulse envelope, suppressing the burst
                # variance that the motion guard's burst_prob bump just created.
                # Run-18: motion guard fired then controller raised sigma 0.8→1.0
                # in the same iter, motion crashed to 0.013 and SNR exploded to 17.5.
                if (knob == 'pulse_smoothing_sigma'
                        and sign == +1
                        and current_metrics.get('motion', 1.0)
                            < self.targets['motion_min']):
                    continue
                # v5.10g: block roi_motion_noise REDUCTIONS when SNR is above the
                # mid-upper threshold (snr_min + 60% of band = 10.0 dB with default
                # 7-12 band). Run-19 iter-4: roi_motion_noise picked down while SNR
                # was 10.87, causing phase regression. Its primary_metric=snr means
                # the controller uses it as an SNR-reducing lever, but with SNR
                # already comfortably in-band there's no benefit and real phase risk.
                if (knob == 'roi_motion_noise'
                        and sign == -1
                        and current_metrics.get('snr', 0.0) > (
                            self.targets['snr_min']
                            + 0.6 * (self.targets['snr_max'] - self.targets['snr_min'])
                        )):
                    continue
                for mult in step_multipliers:
                    new_val = clamp_to_registry(knob, cur + sign * step * mult)
                    if abs(new_val - cur) < 1e-12:
                        continue
                    delta_knob = new_val - cur

                    benefit = 0.0
                    risk = 0.0
                    for metric in METRIC_KEYS:
                        slope = estimate_slope(history, knob, metric)
                        predicted_delta = slope * delta_knob
                        direction = directions[metric]
                        if direction != 0:
                            gap = self.metric_gap(metric, current_metrics[metric])
                            # Reward closing the gap, capped so we don't overshoot.
                            helpful = max(0.0, min(abs(gap), predicted_delta * direction))
                            benefit += helpful / max(abs(gap), 1e-6)
                            # Soft penalty for overshoot: anything predicted to push
                            # past the target contributes to risk.
                            overshoot = max(0.0, predicted_delta * direction - abs(gap))
                            risk += overshoot / max(abs(gap), 1e-6) * 0.3
                            # Wrong-direction harm: the knob is predicted to push a
                            # currently-failing metric *further* from target.  This
                            # was silently ignored by the old helpful/overshoot pair
                            # (which capped harmful predictions at 0 benefit) and
                            # let envelope_burst_prob get picked while SNR was below
                            # floor, dragging SNR even lower.  Full weight.
                            harm = max(0.0, -predicted_delta * direction)
                            risk += harm / max(abs(gap), 1e-6) * 1.0
                        else:
                            # Direction-aware: only count drift that moves the
                            # passing metric TOWARD its failure threshold.
                            # phase passes when <= target: rising is bad.
                            # temporal/harmonic pass when >= target: falling is bad.
                            # snr/motion sit inside a band: either wall matters,
                            # so use abs.
                            if metric == 'phase':
                                toward_fail = max(0.0, predicted_delta)
                            elif metric in ('temporal', 'harmonic', 'spectrum_realism'):
                                # higher-is-better; falling toward target is bad
                                toward_fail = max(0.0, -predicted_delta)
                            else:
                                toward_fail = abs(predicted_delta)
                            m_slack = max(slack.get(metric, 1e-6), 1e-6)
                            usage = toward_fail / m_slack
                            risk += usage
                            # Soft-veto: once a candidate would consume more
                            # than PASS_SAFETY_MARGIN of a passing metric's
                            # slack in one step, pile on extra risk so only
                            # high-benefit candidates can still win.
                            # v5.8: caller may pass a wider margin via
                            # pass_safety_margin_override (breakthrough mode
                            # after a no-benefit fallback iter).
                            effective_margin = (
                                pass_safety_margin_override
                                if pass_safety_margin_override is not None
                                else self.PASS_SAFETY_MARGIN
                            )
                            # v5.10c per-metric dynamic expansion: when phase
                            # has large slack, loosen its soft-veto margin so
                            # temporal/harmonic knobs aren't blocked by a phase
                            # cost that won't approach failure for several iters.
                            # Run-14: phase at 8.9° (9.1° below target) blocked
                            # every temporal candidate even with breakthrough=0.6.
                            # Expansion capped at BREAKTHROUGH_PASS_SAFETY_MARGIN.
                            if metric == 'phase':
                                _phase_reg_t = (PHASE_EMERGENCY_REGRESSION_FRAC
                                                * self.targets['phase'])
                                _phase_slack = slack.get('phase', 0.0)
                                if _phase_slack > _phase_reg_t:
                                    effective_margin = min(
                                        BREAKTHROUGH_PASS_SAFETY_MARGIN,
                                        effective_margin
                                        * (_phase_slack / max(_phase_reg_t, 1e-6))
                                    )
                            if usage > effective_margin:
                                risk += self.PASS_SAFETY_PENALTY * (
                                    usage - effective_margin
                                )

                    score = benefit - 0.6 * risk
                    primary = spec['primary_metric']
                    if primary in failing:
                        score += 0.25
                    # Mild preference for smaller steps when benefit is similar
                    # (favours controlled nudges over big swings).
                    score -= 0.003 * (mult - 1)
                    if score > best[0]:
                        rationale = (
                            f"{knob} {cur:.4g} -> {new_val:.4g} (x{mult} step)  "
                            f"primary={primary}, benefit={benefit:.2f}, risk={risk:.2f}"
                        )
                        details = {
                            'benefit': float(benefit),
                            'risk': float(risk),
                            'score': float(score),
                            'step_multiplier': int(mult),
                            'primary_metric': primary,
                        }
                        best = (score, knob, new_val, rationale, details)

        if best[1] is None:
            return None, None, 'No beneficial knob change found.', None

        # When the winning candidate has near-zero benefit, the controller is
        # picking a least-impact knob because every meaningful candidate would
        # disturb a passing metric more than it would help a failing one. The
        # "primary={metric}" label on the per-loop rationale string then reads
        # as if the controller is "targeting" that metric, which it is not -
        # primary is a static registry tag. Replace the rationale to make the
        # stuck state explicit.
        score_w, knob_w, new_val_w, rationale_w, details_w = best

        # v5.10d: block negative-score picks. score < 0 means net harm > net
        # benefit even with breakthrough margin — applying the change is
        # counterproductive. Return a no-op with no_benefit_fallback=True so
        # the breakthrough counter increments and the next iter gets a wider
        # margin. Run-15 iter-3: score=-0.27 was applied and caused +10.9° phase.
        if score_w < 0:
            cur_w = float(getattr(params, knob_w))
            neg_rationale = (
                f"{knob_w} {cur_w:.4g} -> {new_val_w:.4g} "
                f"BLOCKED (score={score_w:.3f} < 0; "
                f"benefit={details_w['benefit']:.2f}, "
                f"risk*0.6={0.6 * details_w['risk']:.2f}). "
                f"No-op; breakthrough widens margin next iter."
            )
            neg_details = dict(details_w)
            neg_details['no_benefit_fallback'] = True
            neg_details['blocked_negative_score'] = True
            return None, None, neg_rationale, neg_details

        if details_w and details_w['benefit'] < self.NO_BENEFIT_EPSILON:
            cur_w = float(getattr(params, knob_w))
            rationale_w = (
                f"{knob_w} {cur_w:.4g} -> {new_val_w:.4g} "
                f"(x{details_w['step_multiplier']} step)  "
                f"NO POSITIVE-BENEFIT CANDIDATE; least-impact pick "
                f"(risk={details_w['risk']:.2f}). All meaningful candidates "
                f"would disturb a passing metric beyond PASS_SAFETY_MARGIN."
            )
            details_w = dict(details_w)
            details_w['no_benefit_fallback'] = True
        return knob_w, new_val_w, rationale_w, details_w


# ---------------------------------------------------------------------------
# PhaseAlignedRPPGManipulator
# ---------------------------------------------------------------------------

class PhaseAlignedRPPGManipulator:
    """Manipulator with hemoglobin absorption modeling, PTT phase offsets,
    HRV biological pulse, thermal signature, and PID-like iterative control."""

    def __init__(self, analyzer: AdvancedRPPGInjector):
        self.analyzer = analyzer

    def analyze_roi_phases(self, video_path: str) -> Tuple[Dict, Dict]:
        print("Analyzing existing phase relationships...")
        results = self.analyzer.analyze_video(video_path)
        phase_data = {}
        for roi_name, roi_info in results['roi_results'].items():
            phase_data[roi_name] = roi_info['phase_info']
        return phase_data, results

    # ----- PTT-based phase offsets -----

    def optimize_phase_offsets(self, target_hr: float = randomHr,
                                 params: Optional['PulseParams'] = None) -> Dict[str, float]:
        """Simulate Pulse Transit Time: blood reaches the chin ~70ms after forehead.

        A 'perfect' phase coherence is now a FAIL -- we need 'natural' coherence.
        Phase spread of ~0.1 to 0.3 radians across the face, scaled by
        params.ptt_spread (knob).
        """
        spread = float(params.ptt_spread) if params is not None else 1.0
        offsets = {}
        for roi_name, (lo, hi) in PTT_PHASE_OFFSETS.items():
            if lo == hi == 0.0:
                offsets[roi_name] = 0.0
            else:
                offsets[roi_name] = random.uniform(lo * spread, hi * spread)
        return offsets

    # ----- Biological pulse generation with HRV -----

    def generate_base_cardiac_signal(self, duration: float, fps: float, target_hr: float,
                                      strength: float,
                                      params: Optional['PulseParams'] = None,
                                      sdnn_ms: float = HRV_SDNN_TARGET_MS,
                                      align_to_zero: bool = False) -> Tuple[np.ndarray, np.ndarray]:
        """Generate one shared cardiac signal (HRV, respiratory, micro-expressions).

        Returns (base_phase, base_envelope) so that per-ROI pulses can be derived
        by applying only the PTT phase offset -- all other physiological components
        are shared across ROIs, which is what real blood flow looks like.

        Envelope components are modulated by *params* knobs:
          resp_amp    -- RSA depth
          micro_exp_* -- micro-expression amplitude range
          mayer_amp   -- vasomotor low-freq amplitude
        """
        if params is None:
            params = PulseParams(strength=strength)

        n_frames = int(duration * fps)
        time_array = np.arange(n_frames) / fps
        mean_freq = target_hr / 60.0

        # HRV: slowly-varying frequency modulation (shared across body)
        hrv_noise = np.random.randn(n_frames) * (sdnn_ms / 1000.0) * mean_freq
        hrv_noise = gaussian_filter1d(hrv_noise, sigma=fps * 2.0)
        inst_freq = np.clip(mean_freq + hrv_noise, 0.67, 2.5)

        # Integrate instantaneous frequency to get base phase
        base_phase = np.cumsum(inst_freq / fps) * 2.0 * np.pi
        # Boost mode aligns the synthetic pulse to detected per-ROI starting
        # phases, which are absolute phase values at t=0. Subtracting
        # base_phase[0] makes the carrier start at phase 0 so the per-ROI
        # offset becomes the absolute starting phase.
        if align_to_zero and base_phase.size > 0:
            base_phase = base_phase - base_phase[0]

        # Respiratory sinus arrhythmia (cardio-respiratory coupling)
        resp_freq = random.uniform(0.2, 0.4)
        envelope = 1.0 + float(params.resp_amp) * np.sin(2.0 * np.pi * resp_freq * time_array)

        # Micro-expression timing
        beat_interval_frames = int(fps / mean_freq)
        micro_exp = np.zeros(n_frames)
        gauss_width = min(10, beat_interval_frames // 3)
        if gauss_width >= 3:
            gauss_window = np.exp(-0.5 * np.linspace(-2, 2, gauss_width) ** 2)
            lo = float(params.micro_exp_min)
            hi = max(float(params.micro_exp_max), lo)
            for start in range(0, n_frames - gauss_width, beat_interval_frames):
                amp = random.uniform(lo, hi)
                micro_exp[start:start + gauss_width] += gauss_window * amp
        envelope += micro_exp

        # Mayer waves
        noise_freq = random.uniform(0.08, 0.15)
        envelope += float(params.mayer_amp) * np.sin(2.0 * np.pi * noise_freq * time_array)

        return base_phase, envelope

    def derive_roi_pulse(self, base_phase: np.ndarray, envelope: np.ndarray,
                          phase_offset: float, strength: float,
                          params: Optional['PulseParams'] = None,
                          library_pulse: Optional[np.ndarray] = None) -> np.ndarray:
        """Derive a single ROI's pulse from the shared cardiac signal + PTT offset.

        Synthetic mode (v5, library_pulse=None):
          - Fundamental (H1): systolic upstroke
          - H2 (params.h2_amp, params.h2_phase): dicrotic-notch proxy
          - H3 (params.h3_amp): minor vascular harmonic

        v6 modes (library_pulse provided):
          - 'real'  : use library waveform directly as the base shape, with
                      PTT shifted in the time domain (phase_offset converted to
                      a sample shift based on the dominant beat period).
          - 'blend' : linear mix of synthetic harmonics and library shape,
                      weighted by params.pulse_blend_weight.
        """
        if params is None:
            params = PulseParams(strength=strength)

        phase = base_phase + phase_offset

        # v5 synthetic harmonic construction (always computed; cheap)
        synth_pulse = np.sin(phase)
        synth_pulse += float(params.h2_amp) * np.sin(2.0 * phase + float(params.h2_phase))
        synth_pulse += float(params.h3_amp) * np.sin(3.0 * phase)

        # v6 library-waveform path
        ps = getattr(params, 'pulse_source', 'synthetic')
        if library_pulse is not None and ps in ('real', 'blend'):
            # Phase offset -> time-domain sample shift. base_phase represents
            # cumulative phase in radians; one full cycle = 2*pi. The mean
            # samples-per-cycle can be approximated from base_phase span.
            samples_per_cycle = max(2.0, len(base_phase) / max(
                (float(base_phase[-1]) - float(base_phase[0])) / (2.0 * np.pi), 0.5
            ))
            shift = int(round(phase_offset / (2.0 * np.pi) * samples_per_cycle))
            lib_shifted = np.roll(library_pulse[:len(synth_pulse)], shift)
            # Pad/truncate length match
            if lib_shifted.size < synth_pulse.size:
                lib_shifted = np.pad(lib_shifted, (0, synth_pulse.size - lib_shifted.size))

            if ps == 'real':
                pulse = lib_shifted
            else:  # 'blend'
                w = float(np.clip(getattr(params, 'pulse_blend_weight', 0.5), 0.0, 1.0))
                pulse = w * lib_shifted + (1.0 - w) * synth_pulse
        else:
            pulse = synth_pulse

        # Apply shared envelope (respiratory + micro-expressions + Mayer)
        pulse *= envelope

        # Tiny per-ROI stochastic jitter (keeps ROIs slightly different, not identical)
        pulse += float(params.stochastic_jitter) * np.random.randn(len(pulse))

        # Light smoothing -- low sigma preserves the dicrotic notch detail.
        sigma = max(float(params.pulse_smoothing_sigma), 0.1)
        pulse = gaussian_filter1d(pulse, sigma=sigma)
        peak = np.max(np.abs(pulse))
        if peak > 0:
            pulse = pulse / peak * strength

        # Sporadic envelope bursts -- ADDITIVE (not multiplicative) with random
        # sign.  Applied AFTER peak-normalisation.  Design rationale:
        #   - additive so bursts do not amplify the clean pulse at burst peaks
        #     (the previous multiplicative 6x design coherently boosted pulse
        #     energy at burst sites, behaving as an SNR *amplifier* instead of
        #     a motion lever)
        #   - random sign so adjacent bursts do not constructively reinforce
        #     the pulse frequency
        #   - sigma 2 samples (~67 ms @ 30fps) so each burst carries enough
        #     energy through the 0.7-4 Hz bandpass to push Hilbert envelope
        #     above the 3x-median motion_artifacts threshold, while still being
        #     brief enough that the median envelope stays near baseline
        #   - peak amplitude 2x pulse peak: strong enough to register as an
        #     envelope outlier after extraction, not so strong that a single
        #     burst dominates the spectrum
        prob = float(params.envelope_burst_prob)
        if prob > 0.0:
            n = len(pulse)
            hits = np.random.random(n) < prob
            burst_sigma = 2.0
            # v5.3: amp 2.0 -> 4.0 after a synthetic test confirmed bursts
            # at amp=2.0 never lift motion_artifacts off 0.000 across the
            # entire prob range; amp=4.0 + prob=0.03 lands motion in-band
            # at ~0.08. The registry's motion slope and SNR cost have been
            # doubled in lockstep so the controller's risk model still tracks.
            burst_amp_mult = 4.0
            half_w = int(3 * burst_sigma)
            xx_full = np.arange(-half_w, half_w + 1, dtype=pulse.dtype)
            gauss_full = np.exp(-0.5 * (xx_full / burst_sigma) ** 2)
            pulse_peak = float(np.max(np.abs(pulse))) or 1.0
            for idx in np.flatnonzero(hits):
                start = max(0, idx - half_w)
                end = min(n, idx + half_w + 1)
                g_start = start - (idx - half_w)
                g_end = g_start + (end - start)
                sign = 1.0 if np.random.random() < 0.5 else -1.0
                pulse[start:end] = pulse[start:end] + (
                    sign * burst_amp_mult * pulse_peak * gauss_full[g_start:g_end]
                )

        return pulse

    # ----- Skin validity / specular protection -----

    @staticmethod
    def compute_skin_validity(source_pixels: np.ndarray) -> np.ndarray:
        """Compute per-pixel validity map protecting specular highlights, dark areas,
        and eye glints from receiving pulse modulation.

        Pixels too bright (>235) or too dark (<30) get validity 0.1.
        """
        xp = xp_of(source_pixels)
        if source_pixels.size == 0:
            return xp.empty((0,), dtype=np.float32)

        blue = source_pixels[:, 0].astype(np.float32)
        green = source_pixels[:, 1].astype(np.float32)
        red = source_pixels[:, 2].astype(np.float32)
        luminance = 0.299 * red + 0.587 * green + 0.114 * blue

        validity = xp.where((luminance > 235) | (luminance < 30), 0.1, 1.0).astype(np.float32)

        # Additional soft rolloff near clipping (from v4.1 highlight rolloff)
        lum_pressure = xp.exp(-((255.0 - luminance) / 24.0) ** 2)
        grn_pressure = xp.exp(-((255.0 - green) / 18.0) ** 2)
        pressure = xp.maximum(lum_pressure, grn_pressure)
        highlight_rolloff = xp.clip(1.0 - pressure, 0.0, 1.0)

        return validity * highlight_rolloff

    # ----- Hemoglobin absorption modulation (replaces v4.1 multiplicative) -----

    @staticmethod
    def apply_roi_pulse_modulation(frame_float: np.ndarray, source_frame: np.ndarray,
                                    mask: np.ndarray, pulse_value: float,
                                    params: 'PulseParams',
                                    roi_name: str = '') -> None:
        """Hemoglobin Absorption Modulation (2026).

        Real blood flow doesn't just scale the green channel -- it shifts the hue toward
        yellow-green while decreasing blue reflectance. Uses additive modulation
        simulating absorption rather than multiplicative scaling.

        Hb vector: Green ~1.0x, Red ~0.45x, Blue ~-0.1x (inverse).

        Uses a Gaussian-blurred mask as a spatial weight so pulse amplitude
        fades toward ROI edges -- prevents hard-edge flicker that degrades
        temporal consistency.

        Also injects a small broadband sensor-like noise INSIDE the ROI.  The
        Hilbert-envelope-based motion_artifacts metric is computed on the
        extracted in-ROI signal, so noise added here is what actually drives
        that metric above its 0.02 synthetic-clean floor.  Magnitude is
        controlled by params.roi_motion_noise.
        """
        xp = xp_of(frame_float)
        mask_bool = mask > 0
        if not bool(xp.any(mask_bool)):
            return

        # Soft mask: Gaussian-blurred weights (0..1) fade pulse at ROI edges.
        blur_k = max(3, int(params.hb_mask_blur_size) | 1)   # force odd
        soft_mask = gaussian_blur(mask.astype(np.float32), blur_k, 0)
        soft_max = float(soft_mask.max())
        if soft_max > 0:
            soft_mask = soft_mask / soft_max
        soft_weights = soft_mask[mask_bool]

        source_pixels = source_frame[mask_bool]
        validity = PhaseAlignedRPPGManipulator.compute_skin_validity(source_pixels) * soft_weights

        # Hemoglobin modulation strengths per channel.
        # Multipliers scaled so that strength=0.05 yields ~6px green delta on
        # a mean-green ~130 face, matching the magnitude that rPPG extractors
        # need to detect while preserving the Hb absorption ratio (G >> R > |B|).
        hb_g = pulse_value * validity
        hb_r = pulse_value * 0.45 * validity
        hb_b = -pulse_value * 0.10 * validity

        modulated = frame_float[mask_bool].copy()

        # Additive modulation (simulating absorption)
        modulated[:, 1] = xp.clip(modulated[:, 1] + hb_g * params.hb_g_mult, 0, 255)  # Green
        modulated[:, 2] = xp.clip(modulated[:, 2] + hb_r * params.hb_r_mult, 0, 255)  # Red
        modulated[:, 0] = xp.clip(modulated[:, 0] + hb_b * params.hb_b_mult, 0, 255)  # Blue

        # In-ROI sensor-like noise (feeds the motion-artifact detector).
        noise_sigma = float(params.roi_motion_noise)
        if noise_sigma > 0.0:
            # Per-channel per-pixel noise, weighted by soft-mask so it fades at edges.
            noise = xp.random.normal(0.0, noise_sigma, modulated.shape).astype(np.float32)
            noise *= soft_weights[:, None]
            modulated = xp.clip(modulated + noise, 0, 255)

        frame_float[mask_bool] = modulated

    # ----- Thermal signature simulation -----

    @staticmethod
    def simulate_thermal_signature(frame_float: np.ndarray, pulse_value: float,
                                    mask: np.ndarray) -> np.ndarray:
        """Simulate infrared thermal blood-flow patterns (using red channel as proxy).

        2026 KYC detectors also check for IR thermal patterns correlated with blood flow.
        Thermal effect is strongest at the centre of each ROI and decays outward.
        """
        xp = xp_of(frame_float)
        mask_bool = mask > 0
        if not bool(xp.any(mask_bool)):
            return frame_float

        thermal_effect = pulse_value * 0.8  # slightly less than visible

        y_coords, x_coords = xp.where(mask_bool)
        if y_coords.shape[0] == 0:
            return frame_float

        center_y = xp.mean(y_coords)
        center_x = xp.mean(x_coords)

        # Vectorised distance computation
        distances = xp.sqrt((y_coords - center_y) ** 2 + (x_coords - center_x) ** 2)
        gradient = xp.exp(-distances / 50.0)  # 50-pixel decay

        # Apply thermal effect primarily to red channel
        red_channel = frame_float[y_coords, x_coords, 2].copy()
        red_channel += thermal_effect * gradient * 25.0
        frame_float[y_coords, x_coords, 2] = xp.clip(red_channel, 0, 255)

        return frame_float

    # ----- ROI boundary smoothing -----

    def smooth_roi_boundaries(self, frame: np.ndarray, roi_masks: Dict[str, np.ndarray],
                                params: Optional['PulseParams'] = None) -> np.ndarray:
        """Smooth ROI boundaries with increased bilateralFilter sigma (v5 update).

        Optional cosmetic outside-ROI noise (params.outside_noise_sigma) is added
        for visual realism only -- it does NOT affect the motion_artifacts metric
        (which is computed on in-ROI extracted signals).  The motion metric is
        driven by params.roi_motion_noise in apply_roi_pulse_modulation.
        """
        xp = xp_of(frame)
        combined_mask = xp.zeros(frame.shape[:2], dtype=np.float32)
        for mask in roi_masks.values():
            combined_mask = xp.maximum(combined_mask, mask.astype(np.float32) / 255.0)

        blurred_mask = gaussian_blur(combined_mask, 11, 5)

        # v5: increased bilateralFilter sigma values (was d=3, sigma=5 in v4.1).
        # On the GPU path, bilateral_approx substitutes a sigma=1.5 Gaussian.
        smoothed = bilateral_approx(frame)

        blend_factor = 0.3
        mask_3d = blurred_mask[:, :, np.newaxis]
        frame = frame * (1.0 - blend_factor * mask_3d) + smoothed * blend_factor * mask_3d

        # Cosmetic outside-ROI sensor noise (doesn't influence metrics).
        outside_sigma = float(params.outside_noise_sigma) if params is not None else 0.5
        if outside_sigma > 0.0:
            noise = xp.random.normal(0.0, outside_sigma, frame.shape).astype(np.float32)
            frame = frame + noise * (1.0 - mask_3d)
        frame = xp.clip(frame, 0.0, 255.0)

        return frame

    # ----- Main pulse application pipeline -----

    def apply_phase_aligned_pulses(self, video_path: str, output_path: str,
                                    target_hr: float = randomHr, strength: float = base_strength,
                                    grain_video_path: Optional[str] = None,
                                    enable_final_fx: bool = True,
                                    source_settings_video_path: Optional[str] = None,
                                    legacy_pipeline: bool = False,
                                    params: Optional['PulseParams'] = None,
                                    pulse_alignment: Optional[Dict[str, Any]] = None) -> Optional[Dict[str, Any]]:
        """Apply phase-aligned hemoglobin-modeled pulses with the v5 pipeline:
        1. Deband pre-process (unless legacy mode)
        2. rPPG manipulation (Hb modulation + thermal signature)
        3. Post-rPPG FFmpeg chain (curves, unsharp, CAS, grain)

        *params* carries all tunable knobs.  When omitted, a default
        PulseParams(strength=strength) is constructed for backwards compatibility.
        """
        if params is None:
            params = PulseParams(strength=strength)
        else:
            # Keep strength and params.strength consistent (params is authoritative).
            strength = float(params.strength)

        # When pulse_alignment is supplied (boost mode), use the detected
        # per-ROI starting phases directly so the synthetic pulse adds
        # constructively to the existing rPPG signal. Otherwise fall back to
        # random PTT offsets within the natural-looking range.
        if pulse_alignment is not None and pulse_alignment.get('roi_phases'):
            detected = pulse_alignment['roi_phases']
            phase_offsets = {
                roi: float(detected.get(roi, 0.0))
                for roi in PTT_PHASE_OFFSETS
            }
            print(
                f"Boost mode: using detected per-ROI phases (HR={pulse_alignment.get('target_hr', target_hr):.1f} BPM, "
                f"reference ROI={pulse_alignment.get('reference_roi','?')}, "
                f"baseline SNR={pulse_alignment.get('baseline_snr_db', 0):.1f} dB)"
            )
        else:
            phase_offsets = self.optimize_phase_offsets(target_hr, params=params)
        print("PTT phase offsets (radians):", {k: f"{v:.3f}" for k, v in phase_offsets.items()})
        print(
            f"Pulse strength: {strength:.6f} | "
            f"Hb model: G +/-{strength * params.hb_g_mult:.1f}, "
            f"R +/-{strength * 0.45 * params.hb_r_mult:.1f}, "
            f"B +/-{strength * 0.1 * params.hb_b_mult:.2f}"
        )

        # --- Step 1: Deband pre-process ---
        # Skipped when enable_final_fx=False (e.g. during iterative mode,
        # which runs its own deband once before the first iteration).
        working_path = video_path
        deband_temp = None
        if not legacy_pipeline and enable_final_fx:
            debanded = apply_deband_preprocess(video_path, source_settings_video_path or video_path)
            if debanded:
                working_path = debanded
                deband_temp = debanded

        # --- Step 2: rPPG frame manipulation ---
        self.analyzer.reset_landmark_state()
        cap = cv2.VideoCapture(working_path)
        fps = float(cap.get(cv2.CAP_PROP_FPS))
        if fps <= 0:
            fps = 30.0
        width = int(cap.get(cv2.CAP_PROP_FRAME_WIDTH))
        height = int(cap.get(cv2.CAP_PROP_FRAME_HEIGHT))
        total_frames = int(cap.get(cv2.CAP_PROP_FRAME_COUNT))
        duration = total_frames / fps

        # Generate ONE shared cardiac signal, then derive per-ROI pulses via PTT offset
        # In boost mode, shrink HRV so the synthetic pulse stays in phase with
        # the detected pulse over the video's duration. With default SDNN=50ms
        # the synthetic and detected pulses dephase by ~95deg over 15s; at
        # 5ms the drift is ~10deg, well within constructive-interference range.
        boost_mode = pulse_alignment is not None and pulse_alignment.get('roi_phases')
        sdnn_ms_effective = 5.0 if boost_mode else HRV_SDNN_TARGET_MS
        base_phase, envelope = self.generate_base_cardiac_signal(
            duration, fps, target_hr, strength, params=params,
            sdnn_ms=sdnn_ms_effective,
            align_to_zero=bool(boost_mode),
        )

        # v6: optional library-based pulse waveform. Generated once for the
        # whole clip, then passed to derive_roi_pulse where PTT shift + envelope
        # modulation still apply. When pulse_source='synthetic' (default) this
        # stays None and v5 behaviour is preserved.
        library_pulse = None
        ps = getattr(params, 'pulse_source', 'synthetic')
        if ps in ('real', 'blend') and V6_PULSE_LIBRARY_AVAILABLE:
            try:
                library_pulse = _v6_generate_pulse(
                    target_hr_bpm=float(target_hr),
                    duration_s=float(duration),
                    output_fps=float(fps),
                    seed=None,           # fresh selection each call
                    prefer_real=True,
                )
                print(f"  v6 pulse source: {ps} "
                      f"(blend_weight={getattr(params, 'pulse_blend_weight', 0.5)})")
            except Exception as exc:
                print(f"  v6 pulse library failed ({type(exc).__name__}: {exc}); "
                      f"falling back to synthetic.")
                library_pulse = None
        elif ps in ('real', 'blend') and not V6_PULSE_LIBRARY_AVAILABLE:
            print("  v6 pulse library not importable; falling back to synthetic.")

        roi_pulses = {}
        for roi_name, phase_offset in phase_offsets.items():
            roi_pulses[roi_name] = self.derive_roi_pulse(
                base_phase, envelope, phase_offset, strength, params=params,
                library_pulse=library_pulse,
            )

        # v5.6: pipe modulated frames straight to ffmpeg's stdin instead of
        # routing through cv2.VideoWriter(mp4v) + a second ffmpeg re-encode.
        # The old two-stage path quantised every frame TWICE (mp4v lossy +
        # ffmpeg lossy), which produced a measurable cache-vs-output metric
        # gap (e.g. SNR 11.18 vs --analyze SNR 7.24 on the same file).
        # Single-pass NVENC/x264 with source-matching codec/bitrate/profile
        # collapses that gap to one quantisation.
        ffmpeg_proc, ffmpeg_stderr, ffmpeg_cmd = _spawn_ffmpeg_pipe_encode(
            source_settings_video_path or video_path,
            output_path,
            width=width, height=height, fps=fps,
            enable_fx=enable_final_fx,
            grain_video_path=grain_video_path,
            legacy_pipeline=legacy_pipeline,
            output_bitrate_mult=float(getattr(params, 'output_bitrate_mult', 1.0)),
        )

        # Per-ROI per-channel mean RGB cache, accumulated from the MODULATED
        # frames. analyze_video() consumes this directly so it no longer
        # re-reads the output video and re-runs MediaPipe per frame. Matches
        # the dict shape that extract_rgb_signals returns (BGR via cv2.mean
        # order; keys 'R','G','B' so CHROM/POS math is unchanged).
        rgb_cache: Dict[str, Dict[str, List[float]]] = {
            roi: {'R': [], 'G': [], 'B': []} for roi in self.analyzer.roi_landmarks
        }

        frame_idx = 0
        broken_pipe = False
        while True:
            ret, frame = cap.read()
            if not ret:
                break

            rois, roi_masks = self.analyzer.extract_facial_rois(frame)

            if roi_masks:
                # Upload frame + masks to GPU (no-ops on CPU-only builds).
                source_frame = to_gpu(frame.astype(np.float32))
                frame_float = source_frame.copy()
                roi_masks_bk = {n: to_gpu(m) for n, m in roi_masks.items()}

                for roi_name, mask in roi_masks_bk.items():
                    if roi_name in roi_pulses and frame_idx < len(roi_pulses[roi_name]):
                        pv = float(roi_pulses[roi_name][frame_idx])

                        # Hemoglobin absorption modulation
                        self.apply_roi_pulse_modulation(frame_float, source_frame, mask, pv,
                                                         params, roi_name)

                        # Thermal signature simulation
                        frame_float = self.simulate_thermal_signature(frame_float, pv, mask)

                frame_float = self.smooth_roi_boundaries(frame_float, roi_masks_bk, params=params)

                # Per-ROI RGB mean cache (from modulated frame).
                xp = xp_of(frame_float)
                for roi_name, mask in roi_masks_bk.items():
                    mask_bool = mask > 0
                    if not bool(xp.any(mask_bool)):
                        continue
                    region = frame_float[mask_bool]
                    if region.shape[0] == 0:
                        continue
                    mean_bgr = to_cpu(region.mean(axis=0))
                    rgb_cache[roi_name]['B'].append(float(mean_bgr[0]))
                    rgb_cache[roi_name]['G'].append(float(mean_bgr[1]))
                    rgb_cache[roi_name]['R'].append(float(mean_bgr[2]))

                frame = to_cpu(xp.clip(frame_float, 0, 255).astype(xp.uint8))

            # Frame must be C-contiguous uint8 BGR (cv2 default) so .tobytes()
            # produces the rawvideo byte sequence ffmpeg expects.
            try:
                ffmpeg_proc.stdin.write(np.ascontiguousarray(frame).tobytes())
            except BrokenPipeError:
                # ffmpeg died mid-stream; bail out of the loop and surface
                # whatever it logged below.
                broken_pipe = True
                break
            frame_idx += 1

            if frame_idx % int(fps) == 0:
                print(f"Processing frame {frame_idx}/{total_frames}")

        cap.release()

        # Close stdin and wait for ffmpeg to flush. On error, dump captured
        # stderr so the user can see what went wrong instead of silently
        # producing a malformed file.
        try:
            ffmpeg_proc.stdin.close()
        except (BrokenPipeError, OSError):
            pass
        rc = ffmpeg_proc.wait()
        if rc != 0 or broken_pipe:
            ffmpeg_stderr.seek(0)
            stderr_text = ffmpeg_stderr.read().decode('utf-8', errors='replace').strip()
            ffmpeg_stderr.close()
            raise RuntimeError(
                f"ffmpeg encode pipe exited with code {rc}.\n"
                f"Command: {' '.join(ffmpeg_cmd)}\n"
                f"Stderr:\n{stderr_text}"
            )
        ffmpeg_stderr.close()

        roi_signals_out: Dict[str, Dict[str, np.ndarray]] = {
            roi: {ch: np.array(vals, dtype=np.float32) for ch, vals in channels.items()}
            for roi, channels in rgb_cache.items()
        }

        # Clean up deband temp (used only when legacy_pipeline=False AND FX on)
        if deband_temp and os.path.exists(deband_temp):
            os.remove(deband_temp)

        print(f"Phase-aligned pulses applied. Output: {output_path}")

        # Return the pre-encode RGB cache. With single-pass encoding the cache
        # vs --analyze gap is now ~1-2 dB SNR (was ~4 dB with two-stage), but
        # callers that need exact post-encode metrics should still re-analyse
        # the output file.
        return {
            'roi_signals': roi_signals_out,
            'fps': fps,
            'final_fx_applied': bool(enable_final_fx),
        }

    # ----- Console formatting helpers -----

    @staticmethod
    def _c(text: str, code: str) -> str:
        """Wrap *text* in an ANSI colour escape.  Codes: R=red, G=green,
        Y=yellow, C=cyan, W=white-bold, DIM=dim, RESET=reset."""
        codes = {
            'R': '\033[91m', 'G': '\033[92m', 'Y': '\033[93m',
            'C': '\033[96m', 'W': '\033[1;97m', 'DIM': '\033[2m',
            'RESET': '\033[0m',
        }
        return f"{codes.get(code, '')}{text}\033[0m"

    @staticmethod
    def _metric_colour(value: float, target: float, lower_is_better: bool = False) -> str:
        """Return an ANSI colour code letter for a metric value vs its target."""
        if lower_is_better:
            return 'G' if value <= target else ('Y' if value <= target * 1.5 else 'R')
        return 'G' if value >= target else ('Y' if value >= target * 0.7 else 'R')

    def _snr_colour(self, snr: float) -> str:
        if target_snr_min <= snr <= target_snr_max:
            return 'G'
        if snr < target_snr_min * 0.5 or snr > target_snr_max * 1.3:
            return 'R'
        return 'Y'

    @staticmethod
    def _motion_colour(motion: float) -> str:
        """Motion artifacts have both a floor (too clean = synthetic) and ceiling."""
        if target_motion_artifacts_min <= motion <= target_motion_artifacts:
            return 'G'
        if motion > target_motion_artifacts * 1.5 or motion < target_motion_artifacts_min * 0.5:
            return 'R'
        return 'Y'

    def _extract_metrics(self, results: dict) -> dict:
        """Pull the metrics from an analyze_video result dict.

        v6: spectrum_realism is included only when v6_spectrum_scorer was
        importable AND the analyzer actually computed a value. The controller's
        METRIC_KEYS gates on V6_SPECTRUM_AVAILABLE so we don't advertise a
        metric the controller can't act on.
        """
        adv = results.get('advanced_phase', {})
        out = {
            'snr': results['global_snr'],
            'phase': results['phase_coherence'],
            'temporal': results.get('temporal_consistency', 0.0),
            'motion': results.get('motion_artifacts', 1.0),
            'harmonic': adv.get('harmonic_alignment', 0.0),
        }
        if V6_SPECTRUM_AVAILABLE and 'spectrum_realism' in results:
            out['spectrum_realism'] = float(results.get('spectrum_realism', 0.0))
        return out

    def _print_metrics_line(self, label: str, m: dict, prefix: str = '') -> None:
        """Print a single-line metrics summary with colour."""
        v_snr = f"{m['snr']:.2f}"
        v_phase = f"{m['phase']:.1f}"
        v_temp = f"{m['temporal']:.2f}"
        v_mot = f"{m['motion']:.2f}"
        v_harm = f"{m['harmonic']:.2f}"

        snr_c = self._snr_colour(m['snr'])
        phase_c = self._metric_colour(m['phase'], target_phase_coherence, lower_is_better=True)
        temp_c = self._metric_colour(m['temporal'], target_temporal_consistency)
        mot_c = self._motion_colour(m['motion'])
        harm_c = self._metric_colour(m['harmonic'], target_harmonic_alignment)

        line = (
            f"{prefix}{label}  "
            f"SNR: {self._c(v_snr, snr_c)} dB  |  "
            f"Phase: {self._c(v_phase, phase_c)} deg  |  "
            f"Temporal: {self._c(v_temp, temp_c)}  |  "
            f"Motion: {self._c(v_mot, mot_c)}  |  "
            f"Harmonic: {self._c(v_harm, harm_c)}"
        )
        if 'spectrum_realism' in m:
            v_sr = f"{m['spectrum_realism']:.2f}"
            sr_c = self._metric_colour(m['spectrum_realism'], target_spectrum_realism)
            line += f"  |  Spectrum: {self._c(v_sr, sr_c)}"
        print(line)

    def _print_comparison_table(self, baseline: dict,
                                 iter_files: Dict[int, Dict[str, Any]],
                                 best_iter: int) -> None:
        """Print every-iteration metrics table with per-cell colour coding.

        Columns are Stage / SNR / Phase / Temp / Motion / Harm. Each numeric
        cell is colour-coded the same way as the per-iteration log line
        (SNR: in-band green, edges yellow, far red; Phase: lower is better;
        Motion: in-band hump; Temporal/Harmonic: above-target green). The
        best iteration is marked with a "(best)" suffix on its label so it
        appears once, in its natural position in the iteration order.
        """
        c = self._c
        print(c("\n" + "=" * 72, 'W'))
        print(c("  ITERATIVE ENHANCEMENT SUMMARY", 'W'))
        print(c("=" * 72, 'W'))

        # v6 spectrum column appears when v6_spectrum_scorer is loaded AND the
        # baseline metric dict actually has it (skipped on empty/error cases).
        show_spectrum = (
            V6_SPECTRUM_AVAILABLE and 'spectrum_realism' in baseline
        )

        # Targets row (as a reminder above the table).
        targets_str = (
            f"  Targets: SNR {target_snr_min}-{target_snr_max} dB | "
            f"Phase <= {target_phase_coherence} deg | "
            f"Temporal >= {target_temporal_consistency} | "
            f"Motion {target_motion_artifacts_min}-{target_motion_artifacts} | "
            f"Harmonic >= {target_harmonic_alignment}"
        )
        if show_spectrum:
            targets_str += f" | Spectrum >= {target_spectrum_realism}"
        print(c(targets_str, 'DIM'))

        # Column widths chosen so coloured numerics line up under the header.
        # ANSI escapes are zero visible width, so we pad the formatted number
        # string before applying colour and rely on the terminal interpreting
        # the escape codes.
        W_STAGE = 16
        W_SNR = 7
        W_PHASE = 7
        W_TEMP = 6
        W_MOT = 7
        W_HARM = 6
        W_SPEC = 10  # v6 — extra padding so 'Spectrum' header has breathing room from 'Harm'

        header = (
            f"  {'Stage':<{W_STAGE}}"
            f"{'SNR':>{W_SNR}}"
            f"{'Phase':>{W_PHASE}}"
            f"{'Temp':>{W_TEMP}}"
            f"{'Motion':>{W_MOT}}"
            f"{'Harm':>{W_HARM}}"
        )
        total_w = W_STAGE + W_SNR + W_PHASE + W_TEMP + W_MOT + W_HARM
        if show_spectrum:
            header += f"{'Spectrum':>{W_SPEC}}"
            total_w += W_SPEC
        print(c(header, 'DIM'))
        print(c("  " + "-" * total_w, 'DIM'))

        def _cell(value: float, fmt: str, width: int, colour: str) -> str:
            s = f"{value:{fmt}}".rjust(width)
            return self._c(s, colour)

        def _row(label: str, m: dict) -> None:
            snr_c = self._snr_colour(m['snr'])
            phase_c = self._metric_colour(m['phase'], target_phase_coherence,
                                           lower_is_better=True)
            temp_c = self._metric_colour(m['temporal'], target_temporal_consistency)
            mot_c = self._motion_colour(m['motion'])
            harm_c = self._metric_colour(m['harmonic'], target_harmonic_alignment)
            row = (
                f"  {label:<{W_STAGE}}"
                f"{_cell(m['snr'],      '.2f', W_SNR,   snr_c)}"
                f"{_cell(m['phase'],    '.1f', W_PHASE, phase_c)}"
                f"{_cell(m['temporal'], '.2f', W_TEMP,  temp_c)}"
                f"{_cell(m['motion'],   '.2f', W_MOT,   mot_c)}"
                f"{_cell(m['harmonic'], '.2f', W_HARM,  harm_c)}"
            )
            if show_spectrum and 'spectrum_realism' in m:
                sr_c = self._metric_colour(m['spectrum_realism'], target_spectrum_realism)
                row += _cell(m['spectrum_realism'], '.2f', W_SPEC, sr_c)
            print(row)

        # Baseline row, then every iteration in order. Best iteration is
        # identified by a "(best)" suffix so it isn't listed separately.
        _row("Baseline", baseline)
        for it in sorted(iter_files.keys()):
            label = f"Iter {it}" + (" (best)" if it == best_iter else "")
            _row(label, iter_files[it]['metrics'])

        print(c("  " + "-" * total_w, 'DIM'))

    # ----- Intelligent iterative enhancement (4-layer system) -----

    def iterative_enhancement(self, video_path: str, output_path: str,
                               target_hr: float = randomHr, max_iterations: int = 10,
                               grain_video_path: Optional[str] = None,
                               enable_final_fx: bool = True,
                               source_settings_video_path: Optional[str] = None,
                               legacy_pipeline: bool = False,
                               legacy_iteration_tuning: bool = False,
                               keep_iterations: str = 'prompt',
                               iterate_from_baseline: bool = False,
                               boost_existing_pulse: bool = False,
                               pulse_source: str = 'synthetic',
                               pulse_blend_weight: float = 0.5,
                               output_bitrate_mult: float = 1.0,
                               min_strength_floor: float = 0.005) -> Optional[Dict[str, Any]]:
        """Iteratively enhance the video using the intelligent 4-layer system.

        Layer 1: IterationHistory records every (params, metrics, per-ROI, RGB).
        Layer 2: KNOB_REGISTRY declares knob bounds, primary metrics, and seed
                 effect slopes.
        Layer 3: GuardedController picks a single knob change per iteration,
                 maximising failing-metric gap closure minus passing-metric
                 risk.
        Layer 4: Regression-refined slopes blend history observations with
                 registry defaults once >=2 distinct knob values are seen.

        Pipeline:
          1. Measure input baseline and record in history
          2. Iterative rPPG-only manipulation under controller guidance
          3. Post-rPPG FFmpeg chain ONCE on the best-scoring iteration result
             (only if enable_final_fx=True; otherwise a plain source-matching
             re-encode)
          4. Export iteration history JSON and print comparison table

        Note: the deband pre-pass is disabled by default. It still runs in
        --legacy-pipeline mode for backwards compatibility with v4.1 output.
        """
        c = self._c

        # Stable filename for the "best so far" iter snapshot. Re-copied
        # every time a new best is picked (see _is_new_best block below).
        # Cleaned up alongside the numbered temp files at end-of-run.
        #
        # IMPORTANT cwd contract: this is a BARE RELATIVE path. The
        # subprocess that runs this injector is always launched with
        # `cwd=rPPG/` (confirmed at the three known invocation paths:
        # GUI queue → kling_gui/queue_manager.py::_rppg_video,
        # automation pipeline → automation/rppg.py::run_rppg with
        # `cwd=str(launcher.parent)`, and oldcam-testing harness →
        # oldcam-testing/rppg_harness.py with the same `launcher.parent`
        # cwd). The post-loop cleanup at line ~4636 compares this name
        # against `output_path` (absolute) and `video_path` (absolute);
        # they never compare equal, so the snapshot is deleted
        # unconditionally — which is correct because the snapshot is
        # intermediate. Don't make `_BEST_SNAPSHOT_NAME` resolve to the
        # same file as `output_path` without also updating the cleanup
        # guard to handle the absolute/relative comparison properly.
        #
        # Subagent M1b PR #53 round 9: the bare name used to be
        # "best_iteration_snapshot.mp4" — shared across ALL concurrent
        # rPPG runs because cwd=rPPG/ is shared by all GUI workspaces
        # (PR #49 workspace isolation does not extend to subprocess
        # cwd). Two concurrent runs on different inputs could then
        # collide: run A's `prior_snapshot_good=True` picks up run B's
        # snapshot, and the round-5 "keep prior good snapshot" branch
        # commits the WRONG video as best_path. Now include the
        # process PID + a per-input hash so each run gets its own
        # snapshot filename. Users typically run rPPG one at a time
        # (it's a 5-10 min iterative process) so this is mostly
        # defense-in-depth, but the cost is one extra string format.
        _video_id = hash(os.path.abspath(video_path)) & 0xFFFFFF
        _BEST_SNAPSHOT_NAME = (
            f"best_iteration_snapshot_{os.getpid()}_{_video_id:06x}.mp4"
        )

        current_path = video_path
        deband_temp = None
        if legacy_pipeline:
            debanded = apply_deband_preprocess(video_path, source_settings_video_path or video_path)
            if debanded:
                current_path = debanded
                deband_temp = debanded
                print(c("Deband pre-pass complete (legacy pipeline).", 'C'))

        # --- Step 2: Input baseline measurement ---
        print(c("\n--- Input Baseline ---", 'W'))
        baseline_results = self.analyzer.analyze_video(current_path)
        baseline = self._extract_metrics(baseline_results)
        self._print_metrics_line("Baseline", baseline, prefix="  ")
        baseline_face = self.analyzer.compute_face_coherence_scores(current_path)
        print(c(
            f"  Baseline face: geometry={baseline_face['geometry']:.4f}  "
            f"eye_variations={baseline_face['eye_variations']:.4f}",
            'DIM',
        ))

        # --- Controller + history setup ---
        history = IterationHistory()
        targets = {
            'snr_min': target_snr_min,
            'snr_max': target_snr_max,
            'phase': target_phase_coherence,
            'temporal': target_temporal_consistency,
            'motion_min': target_motion_artifacts_min,
            'motion_max': target_motion_artifacts,
            'harmonic': target_harmonic_alignment,
            'spectrum_realism': target_spectrum_realism,  # v6, high-weight fitness signal
        }
        controller = GuardedController(targets)

        # --- Boost-existing-pulse mode (optional) ---
        # When --boost-existing-pulse is on, detect the pulse signature already
        # present in the baseline (HR + per-ROI starting phases) and align the
        # synthetic injection to it. The synthetic pulse then adds
        # constructively to the existing rPPG signal instead of competing
        # with it. detection can fail (low baseline SNR, out-of-range HR,
        # etc.) and falls back to the random-pulse path.
        pulse_alignment = None
        if boost_existing_pulse:
            pulse_alignment = self.analyzer.detect_pulse_alignment(baseline_results)
            if pulse_alignment is None:
                print(c(
                    "  Boost mode requested but baseline pulse detection was "
                    "unreliable (SNR too low or HR out of physiological range). "
                    "Falling back to random-pulse generation.",
                    'Y',
                ))
            else:
                print(c(
                    f"  Boost mode active: detected HR={pulse_alignment['target_hr']:.1f} BPM "
                    f"from {pulse_alignment['reference_roi']} (baseline SNR "
                    f"{pulse_alignment['baseline_snr_db']:.2f} dB). Synthetic pulse "
                    f"will align with detected per-ROI phases.",
                    'C',
                ))
                # Override target_hr for the rest of the run.
                target_hr = float(pulse_alignment['target_hr'])

        # Persist run mode + alignment in iteration history JSON so the
        # diagnoser knows how to interpret the trajectory.
        history.metadata = {
            'iterate_from_baseline': bool(iterate_from_baseline),
            'boost_existing_pulse': bool(boost_existing_pulse),
            'pulse_alignment': pulse_alignment,  # None if disabled or failed
        }

        # Initial params: warm-start strength gives the controller a sensible seed.
        #   v5.2 used 3.5x base_strength to close the SNR gap in fewer steps,
        #   but combined with the v5.3 softer calibration this consistently
        #   overshoots SNR to ~20 dB on iter 1 and crashes Motion to 0.000.
        #   v5.3 default: 1.5x base_strength so iter 1 lands closer to the
        #   comfort point (~10 dB) and leaves room for iter 2-3 to fine-tune.
        #   Also seeds envelope_burst_prob at 0.02 so motion lands in-band
        #   on iter 1 instead of crashing and waiting for the floor guard
        #   to ramp it up step by step.
        # legacy_iteration_tuning=True restores the v5.2 seed (3.5x, no burst).
        # v5.5: dynamic strength bounds. For iterative runs (non-legacy), the
        # initial strength and the strength.max for THIS RUN are computed from
        # the measured baseline SNR + registry's slope estimate. A high-SNR
        # baseline (close to or past target) starts at strength.min instead of
        # the old 1.5x base seed that was systematically overshooting. The
        # per-run max is restored to the registry default at the end.
        original_strength_max = KNOB_REGISTRY['strength']['max']
        if legacy_iteration_tuning:
            params = PulseParams(strength=base_strength * 3.5)
        else:
            strength_min = float(KNOB_REGISTRY['strength']['min'])
            slope_snr = abs(float(KNOB_REGISTRY['strength']['effects']['snr'])) or 1.0
            comfort_local = (
                target_snr_max - STRENGTH_CALIBRATION_COMFORT_BELOW_MAX_DB
            )

            # Dynamic max: predicted strength at which SNR would hit the
            # visibility ceiling, with a safety multiplier for slope error.
            delta_to_ceiling = max(0.0, target_snr_max - baseline['snr'])
            dynamic_max = max(
                strength_min,
                delta_to_ceiling / slope_snr * DYNAMIC_STRENGTH_MAX_SAFETY_FACTOR,
            )
            dynamic_max = min(dynamic_max, original_strength_max)

            # Dynamic initial: target the calibrator's comfort point but stay
            # short by INITIAL_FRAC so the calibrator has room to close the
            # remaining gap. If baseline already sits at or above comfort,
            # start at min_strength_floor and let other knobs do the work.
            #
            # v6 min-strength floor: the registry strength_min (0.0015) drove
            # the dynamic calculator below the uint8 pixel-quantisation zone
            # (modulation amplitude < 0.5 pixel value), which made the saved
            # output bytes nearly identical to the input. Floor at
            # min_strength_floor so the per-pixel modulation always survives
            # the float32 -> uint8 quantisation at ffmpeg input.
            effective_strength_floor = max(strength_min, float(min_strength_floor))
            delta_to_comfort = comfort_local - baseline['snr']
            if delta_to_comfort <= 0:
                dynamic_initial = effective_strength_floor
            else:
                dynamic_initial = max(
                    effective_strength_floor,
                    delta_to_comfort / slope_snr * DYNAMIC_STRENGTH_INITIAL_FRAC,
                )
                dynamic_initial = min(dynamic_initial, dynamic_max)

            # Override registry max for the duration of this run; restored in
            # the cleanup block at the very end of this method.
            KNOB_REGISTRY['strength']['max'] = dynamic_max

            params = PulseParams(
                strength=dynamic_initial,
                envelope_burst_prob=0.025,  # v5.10h: raised from 0.02
                stochastic_jitter=0.015,    # v5.10h: reverted from 0.018 trial (cycle-8: controller reduced it back, disrupting phase dynamics)
                pulse_source=pulse_source,
                pulse_blend_weight=pulse_blend_weight,
                output_bitrate_mult=output_bitrate_mult,
            )
            print(c(
                f"  Dynamic strength bounds: initial={dynamic_initial:.5f}, "
                f"max={dynamic_max:.5f} (registry default max={original_strength_max:.5f}); "
                f"baseline SNR={baseline['snr']:.2f} dB, comfort target={comfort_local:.1f} dB",
                'C',
            ))
        history.record(0, params, baseline_results, rationale='baseline (pre-injection)')

        best_path = None
        best_iter = 0
        best_score = -float('inf')
        best_metrics = dict(baseline)
        best_params = params

        # All iteration temp files are now retained until the end-of-run
        # picker resolves which to finalize and which to discard. Prior
        # behaviour deleted non-best files mid-loop; with --keep-iterations
        # users can choose to keep additional iterations, so we defer all
        # cleanup until the picker has run.
        iter_files: Dict[int, Dict[str, Any]] = {}

        iteration = 0
        current_metrics = dict(baseline)   # feed baseline into first controller call

        # Plateau tracking: once marginal gains stop justifying further
        # knob moves, break early.  Each extra iteration tends to add
        # visible chroma-pulse prominence, so unbounded search was
        # producing over-processed output.
        plateau_counter = 0
        prev_best_score = best_score
        PLATEAU_THRESHOLD = 0.3
        PLATEAU_PATIENCE = 2

        # v5.8: count consecutive no_benefit_fallback iters so the next
        # controller call can break out via an expanded PASS_SAFETY_MARGIN.
        consecutive_no_benefit = 0

        # v5.10 Pattern 1: track consecutive emergency reverts; circuit-breaks
        # by forcing mayer_amp→0 when reverts fail to resolve phase.
        consecutive_emergency_reverts = 0

        # v5.10 Pattern 5: track consecutive motion guard fires; escalates
        # burst_prob step to 2x when single steps can't keep pace with free-fall.
        consecutive_motion_guard_fires = 0

        # v5.10 Pattern 7: SNR spike pre-emption. When SNR jumps >5 dB in one
        # iteration (e.g. after mayer_amp→0), this holds the dB overshoot above
        # comfort so the NEXT iteration can pre-emptively reduce strength before
        # the controller picks. Strength calibrator (gain=0.2, 1-step/iter cap)
        # is too slow to recover within the same run without this.
        snr_spike_reduction_needed = 0.0

        # v5.10d: revert high-water mark. When a phase_emergency_revert iter is
        # also the run's best state, further iters typically degrade from that
        # peak. Tighten plateau patience to 1 so the run stops after the first
        # non-improving post-revert iter rather than burning 2 more iterations.
        revert_high_water_mark = False

        while iteration < max_iterations:
            iter_num = iteration + 1
            print(c(f"\n{'=' * 50}", 'C'))
            print(c(f"  Iteration {iter_num}/{max_iterations}", 'W'))
            print(c(f"{'=' * 50}", 'C'))

            # --- Phase-emergency revert (v5.8/v5.9) ---
            # Two independent triggers: SEVERE (phase well past target) and
            # REGRESSION (phase failing AND last iter caused it). Either
            # unlocks the secondary phase gate. REGRESSION additionally
            # reverts the prior iter's knob change - cleaner recovery than
            # corrective search, which historically destroys other metrics.
            phase_now = current_metrics.get('phase', 0.0)
            last_phase_delta = 0.0
            if history.records:
                last_phase_delta = float(
                    (history.records[-1].get('delta_prev') or {}).get('phase', 0.0)
                )

            phase_severe = (
                not legacy_iteration_tuning
                and phase_now > PHASE_EMERGENCY_MULTIPLIER * target_phase_coherence
            )
            phase_regression_threshold = (
                PHASE_EMERGENCY_REGRESSION_FRAC * target_phase_coherence
            )
            phase_regression_caused = (
                not legacy_iteration_tuning
                and phase_now > target_phase_coherence
                and last_phase_delta > phase_regression_threshold
            )
            # v5.10c proactive revert: also fire when phase is within one
            # regression-threshold of failing and the last iter caused a large
            # regression. The existing condition requires phase to ALREADY be
            # failing, but run-14 showed the damage compounds before crossing
            # the boundary: iter-2 mayer pick pushed phase 8.9→16.9° (threshold
            # 5.4°, target 18°); at iter-3 start phase=16.9° < 18° so the check
            # didn't fire. Iter-3 then added +7.9° uncontested. A proactive
            # revert at iter-3 start would have undone the mayer pick first.
            phase_regression_proactive = (
                not legacy_iteration_tuning
                and phase_now > (target_phase_coherence - phase_regression_threshold)
                and phase_now <= target_phase_coherence
                and last_phase_delta > phase_regression_threshold
            )
            phase_emergency = (phase_severe or phase_regression_caused
                               or phase_regression_proactive)

            phase_emergency_revert = None
            _revert_trigger = (
                'regression_proactive' if phase_regression_proactive
                else 'regression_caused'
            )
            if (phase_regression_caused or phase_regression_proactive) and len(history.records) >= 2:
                last_record = history.records[-1]
                last_kc = last_record.get('knob_change')
                if last_kc and 'knob' in last_kc and 'prev' in last_kc:
                    knob_revert = last_kc['knob']
                    revert_value = float(last_kc['prev'])
                    cur_val = float(getattr(params, knob_revert, revert_value))
                    if abs(cur_val - revert_value) > 1e-12:
                        params = replace(params, **{knob_revert: revert_value})
                        phase_emergency_revert = {
                            'knob': knob_revert,
                            'prev': cur_val,
                            'new': revert_value,
                            'delta': revert_value - cur_val,
                            'phase_at_trigger': float(phase_now),
                            'reverted_iter': last_record.get('iteration'),
                            'reverted_phase_delta': last_phase_delta,
                            'trigger': _revert_trigger,
                        }
                        _phase_desc = (
                            f"near-failing ({phase_now:.1f} within "
                            f"{phase_regression_threshold:.1f}° of target)"
                            if phase_regression_proactive
                            else f"failing ({phase_now:.1f} > target {target_phase_coherence:.1f})"
                        )
                        print(c(
                            f"  Phase emergency revert [{_revert_trigger}]: "
                            f"{knob_revert} {cur_val:.4g} -> {revert_value:.4g}  "
                            f"(phase {_phase_desc}; iter "
                            f"{last_record.get('iteration')} caused "
                            f"+{last_phase_delta:.1f}° > "
                            f"{phase_regression_threshold:.1f}° threshold)",
                            'R',
                        ))
                        # v5.10b cascade: also undo the companion strength
                        # calibration delta from the same reverted iter. Run-13
                        # showed that leaving the orphaned strength delta in place
                        # after a revert causes SNR collapse later in the run
                        # (iter-3 strength −0.000284, calibrated for the spike
                        # state, depressed SNR to 9.43 after iter-5 reverted
                        # mayer_amp back to its pre-spike value).
                        last_sc = last_record.get('strength_calibration')
                        if last_sc and abs(float(last_sc.get('delta', 0.0))) > 1e-9:
                            sc_prev = float(last_sc['prev'])
                            cur_strength = float(params.strength)
                            if abs(cur_strength - sc_prev) > 1e-9:
                                params = replace(params, strength=sc_prev)
                                phase_emergency_revert['strength_reverted'] = {
                                    'prev': cur_strength,
                                    'new': sc_prev,
                                    'delta': sc_prev - cur_strength,
                                }
                                print(c(
                                    f"  Phase emergency revert (cascade): strength "
                                    f"{cur_strength:.5f} -> {sc_prev:.5f}  "
                                    f"(undoing iter {last_record.get('iteration')} "
                                    f"calibration delta "
                                    f"{float(last_sc.get('delta', 0.0)):+.5f})",
                                    'R',
                                ))
                                # v5.10f: cascade revert just restored strength to
                                # the pre-spike value; clear any pending SNR spike
                                # pre-emption so it doesn't fire on top of the revert
                                # and under-cut the restored strength a second time.
                                snr_spike_reduction_needed = 0.0
            elif phase_severe and not history.records:
                # Severe trigger without history: just unlock; no revert.
                pass

            # v5.10 Pattern 1: track consecutive reverts and circuit-break when
            # they're not resolving phase. Counter increments when a revert fired
            # this iter, resets when phase is no longer in emergency.
            if phase_emergency_revert is not None:
                consecutive_emergency_reverts += 1
            elif not phase_emergency:
                consecutive_emergency_reverts = 0

            # Circuit-breaker: after 2+ consecutive reverts that still haven't
            # fixed phase, force mayer_amp→0. Diagnostic memory shows Δmayer=-0.01
            # can drop phase by 42.8° in one step, making it the strongest
            # single-lever recovery move available.
            if (not legacy_iteration_tuning
                    and consecutive_emergency_reverts >= 2
                    and phase_now > target_phase_coherence
                    and float(params.mayer_amp) > 0.0
                    and phase_emergency_revert is None):
                old_mayer = float(params.mayer_amp)
                params = replace(params, mayer_amp=0.0)
                print(c(
                    f"  Phase circuit-breaker: mayer_amp {old_mayer:.4g} -> 0.0 "
                    f"({consecutive_emergency_reverts} consecutive emergency reverts "
                    f"without recovery; phase={phase_now:.1f} > target={target_phase_coherence:.1f})",
                    'R',
                ))
                phase_emergency_revert = {
                    'knob': 'mayer_amp',
                    'prev': old_mayer,
                    'new': 0.0,
                    'delta': -old_mayer,
                    'phase_at_trigger': float(phase_now),
                    'reverted_iter': None,
                    'reverted_phase_delta': 0.0,
                    'trigger': 'consecutive_revert_circuit_breaker',
                    'consecutive_reverts': consecutive_emergency_reverts,
                }
                consecutive_emergency_reverts = 0  # reset; circuit-breaker is fresh intervention

            # --- (B) Secondary-metric phase gate ---
            # For the first MAX_SECONDARY_PHASE_ITERATIONS iterations, restrict
            # the controller to knobs whose primary_metric is temporal,
            # harmonic, or motion. SNR/phase knobs unlock when those three
            # metrics all pass OR when the cap is hit. This prevents iter 1
            # from being dominated by strength/hb_g_mult which crush Motion.
            secondary_done = all(
                controller.passes(m, current_metrics.get(m, 0.0))
                for m in SECONDARY_METRICS
            )
            # v5.8: phase emergency unlocks the secondary gate so primary
            # phase knobs (strength/ptt_spread/hb_g_mult) become eligible
            # even during the early iters. Combined with the revert above
            # this gives the controller every available lever for recovery.
            in_secondary_phase = (
                not legacy_iteration_tuning
                and iteration < MAX_SECONDARY_PHASE_ITERATIONS
                and not secondary_done
                and not phase_emergency
            )
            if in_secondary_phase:
                allowed_knobs = {
                    name for name, spec in KNOB_REGISTRY.items()
                    if spec['primary_metric'] in SECONDARY_METRICS
                }
                phase_gate = {
                    'phase': 'secondary',
                    'allowed_knobs': sorted(allowed_knobs),
                    'reason': f'iter {iter_num} <= {MAX_SECONDARY_PHASE_ITERATIONS} and secondary metrics not all passing',
                }
                print(c(
                    f"  Phase gate: secondary (iter {iter_num}/{MAX_SECONDARY_PHASE_ITERATIONS}) - "
                    f"only {'/'.join(SECONDARY_METRICS)} knobs available",
                    'DIM',
                ))
            else:
                allowed_knobs = None
                if legacy_iteration_tuning:
                    phase_gate = None
                elif phase_emergency:
                    phase_gate = {
                        'phase': 'unlocked',
                        'reason': f'phase emergency ({phase_now:.1f} deg > {PHASE_EMERGENCY_MULTIPLIER * target_phase_coherence:.1f}); all knobs available for recovery',
                    }
                else:
                    phase_gate = {
                        'phase': 'unlocked',
                        'reason': ('secondary metrics all passing'
                                   if secondary_done
                                   else 'iteration cap reached'),
                    }

            # --- (C) Motion floor guard ---
            # If motion has been pushed below target_motion_artifacts_min by
            # an earlier iteration's clean-pulse injection, reactively bump
            # a motion-raising knob BEFORE the controller picks. Both the
            # guard move and the controller move are recorded in history.
            motion_guard = None
            motion_now = current_metrics.get('motion', 1.0)
            _motion_guard_fired = False
            if not legacy_iteration_tuning and motion_now < target_motion_artifacts_min:
                for motion_knob in MOTION_GUARD_KNOBS:
                    spec = KNOB_REGISTRY[motion_knob]
                    cur = float(getattr(params, motion_knob))
                    # v5.10 Pattern 5: escalate to 2x step when guard fires
                    # consecutively without motion recovering (free-fall detection).
                    # Run-11 showed motion 0.134->0.005 monotonic; single step=0.005
                    # cannot keep pace with that rate of fall.
                    step_mult = 2 if consecutive_motion_guard_fires >= 2 else 1
                    guard_step = float(spec['step']) * step_mult
                    new_val = clamp_to_registry(motion_knob, cur + guard_step)
                    if abs(new_val - cur) > 1e-9:
                        prev_val = cur
                        params = replace(params, **{motion_knob: new_val})
                        motion_guard = {
                            'knob': motion_knob,
                            'prev': prev_val,
                            'new': float(new_val),
                            'delta': float(new_val) - prev_val,
                            'motion_value': float(motion_now),
                            'floor': float(target_motion_artifacts_min),
                            'step_multiplier': step_mult,
                        }
                        suffix = (
                            f" (escalated x{step_mult}; guard fire #{consecutive_motion_guard_fires + 1})"
                            if step_mult > 1 else ""
                        )
                        print(c(
                            f"  Motion floor guard: {motion_knob} {prev_val:.4g} -> {new_val:.4g}  "
                            f"(motion {motion_now:.4f} < floor {target_motion_artifacts_min}){suffix}",
                            'Y',
                        ))
                        _motion_guard_fired = True
                        break
            consecutive_motion_guard_fires = (
                consecutive_motion_guard_fires + 1 if _motion_guard_fired else 0
            )

            # --- (A1) SNR spike pre-emption (v5.10 Pattern 7) ---
            # When the previous iteration caused a >5 dB SNR spike (e.g. after
            # mayer_amp→0 dropping low-freq drift), apply a pre-emptive strength
            # reduction BEFORE the normal calibration. The calibrator (gain=0.2,
            # 1-step/iter cap) cannot recover a 7 dB overshoot within a run;
            # this applies up to 3 steps to make a meaningful dent. Skips if
            # motion is below floor (reducing strength suppresses burst amplitude
            # and would compound the motion crash).
            if (not legacy_iteration_tuning
                    and snr_spike_reduction_needed > 0.0
                    and not (motion_now < target_motion_artifacts_min)):
                _slope_snr = float(KNOB_REGISTRY['strength']['effects']['snr']) or 1.0
                _raw_delta = -(snr_spike_reduction_needed / _slope_snr)
                _max_step = float(KNOB_REGISTRY['strength']['step'])
                _spike_delta = max(-3.0 * _max_step, min(-_max_step, _raw_delta))
                _new_strength = clamp_to_registry(
                    'strength', float(params.strength) + _spike_delta
                )
                if abs(_new_strength - float(params.strength)) > 1e-6:
                    print(c(
                        f"  SNR spike pre-emption: strength {params.strength:.5f} -> "
                        f"{_new_strength:.5f} (overshoot {snr_spike_reduction_needed:.1f} dB "
                        f"detected last iter; applying before calibration/controller)",
                        'Y',
                    ))
                    params = replace(params, strength=_new_strength)
                snr_spike_reduction_needed = 0.0

            # --- (A) Adaptive strength calibration (softer, deadbanded, capped) ---
            # v5.2 used gain=0.5 unconditionally, which converted a 3 dB SNR
            # gap into a 4x strength bump on iter 1 - dragging SNR past comfort
            # and crushing motion to 0.00 in a single step. v5.3: gain 0.2,
            # ±1 dB deadband, per-iteration delta capped at one registry step.
            snr_now = current_metrics.get('snr', 0.0)
            # v5.4: comfort tracks snr_max - offset (default 9 dB with current
            # band). Legacy mode keeps the v5.2 lower-third anchor.
            if legacy_iteration_tuning:
                snr_comfort = target_snr_min + 2.0
            else:
                snr_comfort = (
                    target_snr_max - STRENGTH_CALIBRATION_COMFORT_BELOW_MAX_DB
                )
            snr_err = snr_comfort - snr_now
            strength_spec = KNOB_REGISTRY['strength']
            slope_snr = float(strength_spec['effects']['snr']) or 1.0
            if legacy_iteration_tuning:
                gain = 0.5
                deadband = 0.0
                cap_delta = False
            else:
                gain = STRENGTH_CALIBRATION_GAIN
                deadband = STRENGTH_CALIBRATION_DEADBAND_DB
                cap_delta = True

            # v5.4: block strength REDUCTIONS when motion is below floor.
            # Reducing strength shrinks pulse_peak, which scales burst amplitude
            # (burst_amp_mult * pulse_peak in generate_base_cardiac_signal); the
            # smaller bursts then contribute less envelope variance, defeating
            # the motion floor guard that fired one block above. Strength
            # INCREASES (snr_err > 0) are still allowed because they raise
            # burst amplitude in lockstep, helping both metrics.
            block_strength_reduction = (
                not legacy_iteration_tuning
                and motion_now < target_motion_artifacts_min
                and snr_err < 0
            )
            # v5.10h: also block strength INCREASES that are predicted to crash
            # motion below floor. Cycle-3 iter-1: strength +0.001835 drove motion
            # 0.051→0.015 (4× the registry slope prediction). With corrected slope
            # -20, predicted Δmotion = -20 × Δstrength; if motion_now + Δmotion
            # would fall below floor, block the increase and record why.
            if (not legacy_iteration_tuning
                    and snr_err > 0
                    and not block_strength_reduction):
                _raw_str_delta = gain * snr_err / slope_snr
                _str_step = float(strength_spec['step']) * (2 if abs(snr_err) > 2.0 else 1)
                _capped_str_delta = max(-_str_step, min(_str_step, _raw_str_delta))
                _slope_motion = float(strength_spec['effects'].get('motion', 0.0))
                _pred_motion = motion_now + _slope_motion * _capped_str_delta
                if _pred_motion < target_motion_artifacts_min:
                    block_strength_reduction = True  # reuse flag — skips increase too
                    print(c(
                        f"  Strength calibration blocked (motion prediction): "
                        f"strength +{_capped_str_delta:.5f} would push motion "
                        f"{motion_now:.4f} -> {_pred_motion:.4f} < floor "
                        f"{target_motion_artifacts_min}",
                        'Y',
                    ))
            # v5.7: skip calibration entirely when baseline SNR is already in
            # band AND current SNR has not drifted more than 2 dB from
            # baseline. Without this, the calibrator chases comfort=snr_max-1
            # even when the natural baseline is healthy mid-band, slowly
            # creeping strength upward iter-by-iter and dragging SNR away
            # from where it started. Run-8 saw SNR drop 9.55 -> 7.61 over 3
            # iters of P-controller chasing comfort=11.0.
            baseline_snr_in_band = (
                target_snr_min <= baseline['snr'] <= target_snr_max
            )
            snr_drift_from_baseline = abs(snr_now - baseline['snr'])
            skip_calibration_creep = (
                not legacy_iteration_tuning
                and baseline_snr_in_band
                and snr_drift_from_baseline <= 2.0
            )

            strength_calibration = None
            # v5.10g: skip calibration entirely when a phase-emergency revert
            # fires this iter. The cascade revert already restored strength to
            # the pre-spike value; running the calibrator on top risks partially
            # undoing that restoration (run-19 iter-5 added +0.0003 on a revert
            # iter when SNR err was small). Let the revert ride alone.
            if not legacy_iteration_tuning and phase_emergency_revert is not None:
                strength_calibration = {
                    'prev': float(params.strength),
                    'new': float(params.strength),
                    'delta': 0.0,
                    'skipped_reason': 'phase_emergency_revert',
                }
            elif block_strength_reduction:
                print(c(
                    f"  Strength calibration blocked: motion {motion_now:.4f} below "
                    f"floor ({target_motion_artifacts_min}); reducing strength would "
                    f"suppress burst envelope variance and undo the motion guard.",
                    'Y',
                ))
                strength_calibration = {
                    'prev': float(params.strength),
                    'new': float(params.strength),
                    'delta': 0.0,
                    'snr_err': float(snr_err),
                    'gain': float(gain),
                    'deadband_db': float(deadband),
                    'capped': bool(cap_delta),
                    'skipped_reason': 'motion_below_floor',
                }
            elif skip_calibration_creep:
                # Only print on first iter where this gate fires, to avoid
                # log spam. Detect by checking whether prior iters have
                # `skipped_reason: snr_already_settled` recorded - cheap if
                # uncommon.
                strength_calibration = {
                    'prev': float(params.strength),
                    'new': float(params.strength),
                    'delta': 0.0,
                    'snr_err': float(snr_err),
                    'gain': float(gain),
                    'deadband_db': float(deadband),
                    'capped': bool(cap_delta),
                    'skipped_reason': 'snr_already_settled',
                }
            elif abs(snr_err) >= deadband:
                raw_delta = gain * snr_err / slope_snr
                if cap_delta:
                    # v5.10b: allow 2-step correction when the gap is >2 dB.
                    # Single-step recovery from large gaps takes too many iters
                    # (run-12 suggestion). Default 1-step preserved for small errs.
                    step_cap = 2 if abs(snr_err) > 2.0 else 1
                    max_step = float(strength_spec['step']) * step_cap
                    strength_delta = max(-max_step, min(max_step, raw_delta))
                else:
                    strength_delta = raw_delta
                new_strength = clamp_to_registry(
                    'strength', float(params.strength) + strength_delta
                )
                if abs(new_strength - float(params.strength)) > 1e-6:
                    prev_strength = float(params.strength)
                    params = replace(params, strength=new_strength)
                    strength_calibration = {
                        'prev': prev_strength,
                        'new': float(new_strength),
                        'delta': float(new_strength) - prev_strength,
                        'snr_err': float(snr_err),
                        'gain': float(gain),
                        'deadband_db': float(deadband),
                        'capped': bool(cap_delta),
                    }
                    print(
                        f"  Strength calibration: {prev_strength:.5f} -> "
                        f"{new_strength:.5f}  (SNR err {snr_err:+.2f} dB, "
                        f"gain={gain}, deadband={deadband:.1f} dB)"
                    )

            # --- Controller picks the next flavor knob change ---
            # Skip the controller entirely when a phase-emergency revert
            # happened above: the revert IS this iter's intervention, and
            # piling a controller pick on top compounds the change in a way
            # the next-iter measurements can't cleanly attribute. Let the
            # revert ride alone and re-evaluate next iter.
            knob_change = None
            breakthrough_margin_used = None
            # v5.10h guard-only mode: when the motion guard just fired (motion
            # below floor) AND phase is currently passing, skip the controller
            # pick and let the burst_prob guard bump ride alone. Cycle-6 pattern:
            # controller picked mayer/sigma/ptt_spread ON TOP of the guard, spiking
            # phase and losing the best-state metrics reached by the prior revert.
            _guard_only_mode = (
                not legacy_iteration_tuning
                and _motion_guard_fired
                and controller.passes('phase', phase_now)
            )
            if phase_emergency_revert is not None:
                knob_name = None
                new_value = None
                rationale = (
                    f"Phase emergency: reverted {phase_emergency_revert['knob']} "
                    f"({phase_emergency_revert['prev']:.4g} -> {phase_emergency_revert['new']:.4g}); "
                    f"controller pick skipped this iter."
                )
                details = None
                consecutive_no_benefit = 0
                print(f"  Controller: {c(rationale, 'R')}")
            elif _guard_only_mode:
                knob_name = None
                new_value = None
                rationale = (
                    "Guard-only mode: motion below floor AND phase passing; "
                    "controller pick skipped to preserve state while burst guard recovers motion."
                )
                details = None
                print(f"  Controller: {c(rationale, 'Y')}")
            else:
                # v5.8: after MAX_NO_BENEFIT_FALLBACK_ITERS consecutive
                # fallback iters, expand PASS_SAFETY_MARGIN to break out.
                margin_override = None
                if (not legacy_iteration_tuning
                        and consecutive_no_benefit >= MAX_NO_BENEFIT_FALLBACK_ITERS):
                    margin_override = BREAKTHROUGH_PASS_SAFETY_MARGIN
                    breakthrough_margin_used = margin_override
                    print(c(
                        f"  Breakthrough mode: PASS_SAFETY_MARGIN expanded "
                        f"{controller.PASS_SAFETY_MARGIN} -> {margin_override} "
                        f"after {consecutive_no_benefit} no_benefit_fallback iter(s)",
                        'C',
                    ))

                knob_name, new_value, rationale, details = controller.choose_next_change(
                    params, current_metrics, history,
                    allowed_knobs=allowed_knobs,
                    pass_safety_margin_override=margin_override,
                )

                if knob_name is not None and new_value is not None:
                    prev_val = float(getattr(params, knob_name))
                    params = replace(params, **{knob_name: new_value})
                    knob_change = {
                        'knob': knob_name,
                        'prev': prev_val,
                        'new': float(new_value),
                        'delta': float(new_value) - prev_val,
                    }
                    if details:
                        knob_change.update(details)
                    print(f"  Controller: {c(rationale, 'C')}")
                else:
                    print(f"  Controller: {c(rationale, 'Y')}")

                # Track consecutive fallback streak for the next iter's
                # breakthrough-margin decision.
                is_fallback = bool(details and details.get('no_benefit_fallback'))
                if is_fallback:
                    consecutive_no_benefit += 1
                else:
                    consecutive_no_benefit = 0

            # Show tuned knobs that differ from defaults (keeps the log readable).
            # Numeric fields use abs(delta)>epsilon; non-numeric (e.g. v6
            # pulse_source string) use != equality.
            default_params = PulseParams()
            non_default = {}
            for k, v in params.snapshot().items():
                default_v = getattr(default_params, k, v)
                if isinstance(v, (int, float)) and isinstance(default_v, (int, float)):
                    if abs(v - default_v) > 1e-9:
                        non_default[k] = v
                elif v != default_v:
                    non_default[k] = v
            if non_default:
                def _fmt(val):
                    return f"{val:.4g}" if isinstance(val, (int, float)) else str(val)
                tuned_str = ', '.join(f"{k}={_fmt(v)}" for k, v in non_default.items())
                print(f"  Tuned knobs: {c(tuned_str, 'DIM')}")

            # --- Apply params to video ---
            temp_output = f"temp_iteration_{iteration}.mp4"
            pulse_result = self.apply_phase_aligned_pulses(
                current_path,
                temp_output,
                target_hr,
                params.strength,
                grain_video_path=None,
                enable_final_fx=False,
                source_settings_video_path=source_settings_video_path or video_path,
                legacy_pipeline=legacy_pipeline,
                params=params,
                pulse_alignment=pulse_alignment,
            )

            # All iter files retained for the picker; cleanup runs at end.
            # When --iterate-from-baseline is on, current_path stays pinned to
            # the original input across iterations so each iter is a clean
            # standalone test of THIS iter's params (no cumulative compounding
            # of pulse signal across iterations). Default behaviour is
            # cumulative: each iter feeds the previous iter's output.
            # iter_output_path always points at THIS iter's modulated file,
            # independent of how current_path advances — so the picker /
            # finalize stage can find the actual modulated bytes even in
            # iterate-from-baseline mode (where current_path stays at source).
            iter_output_path = temp_output
            if not iterate_from_baseline:
                current_path = temp_output
            iteration = iter_num

            # --- Analyse this iteration's output + record ---
            # Reuse the per-ROI RGB cache accumulated during frame processing
            # so analyze_video skips the video re-read + per-frame MediaPipe.
            if (pulse_result and not pulse_result.get('final_fx_applied', True)
                    and pulse_result.get('roi_signals')):
                results = self.analyzer.analyze_video(
                    iter_output_path,
                    roi_signals=pulse_result['roi_signals'],
                    fps=pulse_result.get('fps'),
                )
            else:
                results = self.analyzer.analyze_video(iter_output_path)
            m = self._extract_metrics(results)
            self._print_metrics_line(f"  Iter {iteration}", m, prefix="")
            history.record(iteration, params, results,
                           knob_change=knob_change, rationale=rationale,
                           strength_calibration=strength_calibration,
                           motion_guard=motion_guard,
                           phase_gate=phase_gate,
                           phase_emergency_revert=phase_emergency_revert,
                           breakthrough_margin=breakthrough_margin_used)

            # v5.10 Pattern 7: SNR spike detection.
            # current_metrics still holds the PREVIOUS iteration's values here;
            # m holds the newly measured ones. Compute the delta before updating.
            _snr_prev = current_metrics.get('snr', 0.0)
            _snr_now = m.get('snr', 0.0)
            _snr_iter_delta = _snr_now - _snr_prev
            if not legacy_iteration_tuning and _snr_iter_delta > 5.0:
                _comfort = target_snr_max - STRENGTH_CALIBRATION_COMFORT_BELOW_MAX_DB
                snr_spike_reduction_needed = max(0.0, _snr_now - _comfort)
                if snr_spike_reduction_needed > 0.0:
                    print(c(
                        f"  SNR spike detected: {_snr_prev:.2f} -> {_snr_now:.2f} dB "
                        f"(+{_snr_iter_delta:.1f} dB); will pre-empt strength next iter",
                        'Y',
                    ))

            # v5.10 Pattern 3: mayer_amp / SNR drift hypothesis logging.
            # If mayer_amp changed this iter and SNR moved by >3 dB, the
            # low-freq drift hypothesis (mayer removal recovers temporal
            # stability AND apparent SNR simultaneously) may be at play.
            if not legacy_iteration_tuning and abs(_snr_iter_delta) > 3.0:
                _mayer_changed = (
                    (knob_change and knob_change.get('knob') == 'mayer_amp')
                    or (phase_emergency_revert
                        and phase_emergency_revert.get('knob') == 'mayer_amp')
                )
                if _mayer_changed:
                    print(c(
                        f"  [mayer/SNR] mayer_amp={params.mayer_amp:.4g}, "
                        f"SNR delta={_snr_iter_delta:+.2f} dB — check temporal "
                        f"delta to validate low-freq drift hypothesis",
                        'DIM',
                    ))

            current_metrics = dict(m)

            # --- Score: balance gap closure across all 5 metrics ---
            score = self._score_all_metrics(m)

            # Track every iteration's output file + metrics so the end-of-run
            # picker can resolve which ones to finalize.
            iter_files[iteration] = {
                'path': iter_output_path,
                'metrics': dict(m),
                'score': float(score),
            }

            # "Would-be-best" — the score IS better than the current
            # best, but we still have to successfully adopt the
            # snapshot before we commit the new best_* tuple. See
            # _adopted_this_iter below.
            _is_new_best = score > best_score
            # PR #53 round 8 (CodeRabbit): best_score, best_path,
            # best_iter, best_metrics, best_params are committed
            # TOGETHER and ONLY when the new snapshot is actually
            # accepted. Previously best_score advanced before
            # snapshot adoption succeeded, so a rejected/torn
            # snapshot would leave best_score pointing at the
            # rejected iter's score — poisoning the next iter's
            # `score > best_score` comparison + the plateau-
            # detection logic that reads best_score. The
            # `_adopted_this_iter` local tracks whether we
            # genuinely promoted this iter; the revert high-water
            # mark gate uses THAT, not the raw `_is_new_best`
            # (which only said "score would beat the current
            # best", not "we successfully adopted it").
            _adopted_this_iter = False
            if _is_new_best:
                # Snapshot the new best iter to a stable name so the
                # post-loop face-coherence + final copy can find it
                # even if interim cleanup prunes the numbered
                # temp_iteration_N.mp4 file before the loop ends.
                #
                # Use tmp-copy + validate + atomic replace so a torn
                # copy never overwrites the previous good snapshot.
                # Root cause: shutil.copy2 on an mp4 that may still
                # have an open writer (or be partially flushed)
                # captures a NAL-incomplete file. Previously we
                # adopted it directly; the post-loop final-copy then
                # produced a structurally broken -rppg.mp4 (PR #52
                # regression — ffprobe Invalid NAL unit size errors
                # on the delivered file). The tmp+validate+os.replace
                # flow guarantees we only ever adopt a snapshot that
                # matches the source iter file's frame count and byte
                # length.
                tmp_snapshot = _BEST_SNAPSHOT_NAME + ".tmp.mp4"
                # Has a prior good snapshot been written this run? If yes
                # and the new copy fails validation, we MUST point
                # best_path at the prior snapshot (NOT at the unflushed
                # iter file we just rejected the snapshot of — subagent
                # H3 round 1). The prior_snapshot_good flag is set after
                # the first successful adoption and never cleared.
                prior_snapshot_good = bool(
                    os.path.exists(_BEST_SNAPSHOT_NAME)
                )

                def _commit_best(_path, _score=score, _iter=iteration,
                                 _metrics=m, _params=params):
                    """Commit all best_* state in one atomic step so
                    best_score never advances without a matching
                    best_path / best_iter / best_metrics update.

                    Defaults (``_score=score`` etc.) are bound at def
                    TIME, not at call time — this captures THIS iter's
                    values per the snapshot semantics we want (the
                    closure is rebuilt every iter via the enclosing
                    `if _is_new_best:` block).

                    Exception safety (subagent M2 PR #53 round 9):
                    materialise all per-iter copies UPFRONT, then do
                    the 6 nonlocal assignments together. Previously
                    the `dict(_metrics)` allocation happened mid-
                    sequence, so a MemoryError between two assignments
                    could leave best_score advanced but best_metrics
                    stale — half-committed state that's exactly what
                    the round-8 atomicity fix was designed to prevent.
                    """
                    nonlocal best_score, best_path, best_iter
                    nonlocal best_metrics, best_params
                    nonlocal _adopted_this_iter
                    _new_metrics = dict(_metrics)  # may raise; do it first
                    best_score = _score
                    best_path = _path
                    best_iter = _iter
                    best_metrics = _new_metrics
                    best_params = _params
                    _adopted_this_iter = True

                try:
                    if os.path.exists(iter_output_path):
                        shutil.copy2(iter_output_path, tmp_snapshot)
                        if _snapshot_validates(tmp_snapshot, iter_output_path):
                            os.replace(tmp_snapshot, _BEST_SNAPSHOT_NAME)
                            _commit_best(_BEST_SNAPSHOT_NAME)
                        else:
                            try:
                                os.unlink(tmp_snapshot)
                            except OSError:
                                pass
                            # Snapshot torn. If a previous good snapshot
                            # exists on disk, KEEP best_score AND
                            # best_path/best_iter/best_metrics/best_params
                            # all unchanged — the previous iteration's
                            # win stays canonical. Do NOT promote this
                            # iteration: its output is corrupt by
                            # definition.
                            if prior_snapshot_good:
                                print(c(
                                    f"  Warning: best-iter {iteration} "
                                    f"snapshot validation failed (torn "
                                    f"copy); keeping previous good "
                                    f"snapshot at iter "
                                    f"{best_iter}.",
                                    'Y',
                                ))
                            else:
                                # No prior snapshot — first iter and
                                # already torn. We have nothing better
                                # to fall back to; commit the direct
                                # iter path so SOMETHING is best_path.
                                # The playability gate at end-of-run
                                # will reject it if final output is
                                # corrupt.
                                _commit_best(iter_output_path)
                                print(c(
                                    f"  WARNING: best-iter {iteration} "
                                    f"snapshot validation failed AND no "
                                    f"prior snapshot exists; using "
                                    f"unflushed iter path. Final output "
                                    f"may be corrupt; playability gate "
                                    f"will catch it.",
                                    'R',
                                ))
                    else:
                        # iter_output_path itself doesn't exist — no
                        # source for a snapshot. CodeRabbit PR #53
                        # round 12: NEVER commit a missing path as
                        # best_path (the downstream finalize+copy
                        # would crash on a stale reference). Either
                        # keep the prior good snapshot or hard-fail.
                        if prior_snapshot_good:
                            print(c(
                                f"  Warning: best-iter {iteration} source "
                                f"file missing on disk; keeping previous "
                                f"good snapshot at iter {best_iter}.",
                                'Y',
                            ))
                            # _adopted_this_iter stays False.
                        else:
                            # No prior snapshot AND no source file —
                            # there is no recoverable best at this
                            # point. Raising is the loud, correct
                            # signal (vs silently committing a path
                            # that points at nothing).
                            # CodeRabbit PR #53 round 13: restore the
                            # narrowed registry value before re-raising
                            # so a subsequent run in the same process
                            # (GUI worker) doesn't inherit the dynamic
                            # bound from this aborted run.
                            KNOB_REGISTRY['strength']['max'] = original_strength_max
                            raise FileNotFoundError(
                                f"Best iteration output missing and no "
                                f"prior snapshot exists: "
                                f"{iter_output_path!r} (iter {iteration})"
                            )
                except OSError as exc:
                    print(c(
                        f"  Warning: could not snapshot best iter "
                        f"{iteration} ({type(exc).__name__}: {exc}); "
                        f"using direct path (vulnerable to mid-loop "
                        f"cleanup).",
                        'Y',
                    ))
                    try:
                        if os.path.exists(tmp_snapshot):
                            os.unlink(tmp_snapshot)
                    except OSError:
                        pass
                    if prior_snapshot_good:
                        # Same logic as above: prefer prior good
                        # snapshot over an OSError'd new copy.
                        # _adopted_this_iter stays False.
                        pass
                    elif os.path.exists(iter_output_path):
                        # OSError on copy, but the source iter file
                        # IS present on disk — adopt the direct path
                        # (vulnerable to mid-loop cleanup as warned,
                        # but at least it points at a real file).
                        _commit_best(iter_output_path)
                    else:
                        # Same as the inner else above: no prior
                        # snapshot AND no source file. Hard-fail.
                        # CodeRabbit PR #53 round 13: restore the
                        # narrowed registry value before re-raising
                        # (see twin restore-before-raise at the inner
                        # FileNotFoundError site ~30 lines above).
                        KNOB_REGISTRY['strength']['max'] = original_strength_max
                        raise FileNotFoundError(
                            f"Best iteration output missing after copy "
                            f"failure and no prior snapshot exists: "
                            f"{iter_output_path!r} (iter {iteration})"
                        ) from exc

            # v5.10d: update revert high-water mark.
            # PR #53 round 8: use _adopted_this_iter, NOT _is_new_best.
            # The high-water mark should only update when we actually
            # ADOPTED the new best — a "would-be-best" iter whose
            # snapshot validation failed didn't actually contribute.
            if phase_emergency_revert is not None and _adopted_this_iter:
                revert_high_water_mark = True
            elif phase_emergency_revert is None:
                revert_high_water_mark = False

            # --- Early exit: all 5 metrics pass ---
            all_pass = all(controller.passes(k, v) for k, v in m.items())
            if all_pass:
                print(c(f"  All targets met at iteration {iteration}!", 'G'))
                break

            # --- Early exit: 4/5 pass with near-miss on the one failing metric ---
            # v5.10g: run-19 iter-3 reached 4/5 pass (temporal slightly short) then
            # iter-4 degraded everything. If 4 metrics pass and the one failing is
            # within a tight absolute gap, stop rather than risking another pick.
            # Near-miss thresholds per metric (absolute gap to threshold):
            _NEAR_MISS = {'snr': 0.8, 'phase': 2.0, 'temporal': 0.04,
                          'motion': 0.008, 'harmonic': 0.04}
            if not legacy_iteration_tuning:
                _failing = {k: v for k, v in m.items()
                            if not controller.passes(k, v)}
                if len(_failing) == 1:
                    _fk, _fv = next(iter(_failing.items()))
                    _gap = abs(controller.metric_gap(_fk, _fv))
                    if _gap <= _NEAR_MISS.get(_fk, float('inf')):
                        print(c(
                            f"  Near-all-pass stop: 4/5 metrics pass; "
                            f"{_fk} gap={_gap:.4g} within near-miss threshold "
                            f"{_NEAR_MISS[_fk]}. Stopping to lock in best state.",
                            'G',
                        ))
                        break

            # --- Plateau stop: visibility guard ---
            # If the best score hasn't improved by PLATEAU_THRESHOLD in
            # PLATEAU_PATIENCE consecutive iterations, stop.  Every extra
            # iteration adds visible pulse prominence, so marginal gains
            # aren't worth the cosmetic cost.
            # v5.10d: tighten patience to 1 when last revert was the high-water
            # mark — additional iters predictably degrade from that peak.
            _effective_patience = 1 if revert_high_water_mark else PLATEAU_PATIENCE
            improvement = best_score - prev_best_score
            if improvement < PLATEAU_THRESHOLD:
                plateau_counter += 1
                if plateau_counter >= _effective_patience:
                    _stop_reason = (
                        "Revert high-water: best state was post-revert and "
                        "subsequent iter didn't improve"
                        if revert_high_water_mark
                        else f"Score plateau: {plateau_counter} iterations without "
                             f"meaningful gain (< {PLATEAU_THRESHOLD})"
                    )
                    print(c(f"  {_stop_reason}. Stopping to avoid over-processing.", 'Y'))
                    break
            else:
                plateau_counter = 0
            prev_best_score = best_score

        if best_path is None:
            # Fall back to the last iteration's modulated output rather than
            # current_path — in iterate-from-baseline mode current_path is the
            # source video, which would silently save a no-op output.
            if iter_files:
                last_iter = max(iter_files.keys())
                best_path = iter_files[last_iter]['path']
                best_iter = last_iter
            else:
                best_path = current_path
                best_iter = max_iterations

        # --- Export iteration history as JSON ---
        # Saved to <script_dir>/iteration_history/ so the bot can reference
        # past runs quickly without hunting through output folders.  Done
        # BEFORE the comparison table and final FFmpeg so a later crash
        # can't prevent the history from hitting disk.
        history_dir = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'iteration_history')
        os.makedirs(history_dir, exist_ok=True)
        stamp = datetime.now().strftime('%Y%m%d_%H%M%S')
        stem = os.path.splitext(os.path.basename(output_path))[0]
        history_path = os.path.join(history_dir, f'{stem}_{stamp}_iteration_history.json')
        try:
            history.export_json(history_path)
            print(c(f"Iteration history saved: {history_path}", 'G'))
        except Exception as exc:
            print(c(f"Could not write iteration history: {type(exc).__name__}: {exc}", 'R'))

        summary_path = history_path.replace('_iteration_history.json',
                                            '_metrics_summary.tsv')
        try:
            history.export_metrics_summary(summary_path)
            print(c(f"Metrics summary saved: {summary_path}", 'G'))
        except Exception as exc:
            print(c(f"Could not write metrics summary: {type(exc).__name__}: {exc}", 'R'))

        # --- Print comparison table + final knob diff ---
        # Table now shows every completed iteration with per-cell colour
        # coding (same scheme as the per-iteration log line). The best
        # iteration is marked inline with "(best)" and not listed twice.
        self._print_comparison_table(baseline, iter_files, best_iter)
        self._print_final_knob_summary(best_params)

        # Face-coherence before/after (informational, no tuning knobs feed it).
        try:
            final_face = self.analyzer.compute_face_coherence_scores(best_path)
        except Exception as exc:
            print(c(f"  Face coherence scoring failed: {type(exc).__name__}: {exc}", 'R'))
            final_face = {'geometry': 0.0, 'eye_variations': 0.0}
        g_delta = final_face['geometry'] - baseline_face['geometry']
        e_delta = final_face['eye_variations'] - baseline_face['eye_variations']
        print(c(
            f"  Face coherence:  geometry {baseline_face['geometry']:.4f} -> "
            f"{final_face['geometry']:.4f} ({g_delta:+.4f})  |  "
            f"eye_variations {baseline_face['eye_variations']:.4f} -> "
            f"{final_face['eye_variations']:.4f} ({e_delta:+.4f})",
            'DIM',
        ))

        # --- Iteration picker: prompt for any extras BEFORE finalize ---
        # User has just seen the all-iterations table; they pick now and the
        # finalize step writes best + their picks together. Empty input
        # means "no extras"; non-TTY stdin auto-skips so scripted runs don't
        # block.
        extra_picks = self._resolve_keep_iterations(
            keep_iterations, best_iter, iter_files,
        )
        extra_picks.discard(best_iter)  # best is always finalized below

        # --- Step 3: Post-rPPG FFmpeg chain on best + each picked extra ---
        if enable_final_fx or grain_video_path:
            print(c("\nApplying final FFmpeg filter chain...", 'C'))
            finalize_video_with_source_quality(
                best_path,
                source_settings_video_path or video_path,
                output_path,
                grain_video_path=grain_video_path,
                enable_final_fx=enable_final_fx,
                legacy_pipeline=legacy_pipeline,
            )
        else:
            if best_path != output_path:
                shutil.copy2(best_path, output_path)

        # Rename best output with the metric suffix.
        # 'output/kling_dm1.mp4' -> 'output/kling_dm1 - 11.19-3.3-0.57-0.00-0.85.mp4'
        final_output_path = add_metric_suffix(output_path, best_metrics)
        if final_output_path != output_path and os.path.exists(output_path):
            os.replace(output_path, final_output_path)

        for it_num in sorted(extra_picks):
            info = iter_files.get(it_num)
            if info is None or not os.path.exists(info['path']):
                print(c(f"  iter {it_num}: source file missing, skipping.", 'Y'))
                continue
            derived_path = self._derive_iter_output_path(output_path, it_num)
            if enable_final_fx or grain_video_path:
                finalize_video_with_source_quality(
                    info['path'],
                    source_settings_video_path or video_path,
                    derived_path,
                    grain_video_path=grain_video_path,
                    enable_final_fx=enable_final_fx,
                    legacy_pipeline=legacy_pipeline,
                )
            else:
                shutil.copy2(info['path'], derived_path)
            # Each extra gets its own iteration's metric suffix.
            extra_final = add_metric_suffix(derived_path, info['metrics'])
            if extra_final != derived_path and os.path.exists(derived_path):
                os.replace(derived_path, extra_final)
            print(c(f"  Saved iter {it_num} -> {extra_final}", 'G'))

        print(c(f"\nOutput: {final_output_path}", 'W'))
        print(c(f"Best iteration: {best_iter}/{max_iterations}", 'G'))

        # Clean up all remaining iteration temp files + deband.
        # Files for kept (best + extra-picked) iters were removed by finalize
        # on success; this catches the unkept ones plus any defensive misses.
        cleanup_paths = [info['path'] for info in iter_files.values()]
        cleanup_paths.append(deband_temp)
        # The best-iter snapshot maintained during the loop (paired with the
        # snapshot logic at the _is_new_best site). Already copied to
        # output_path by the finalize step above; safe to remove. Also clean
        # up the validate-staging .tmp.mp4 in case the last iter's snapshot
        # was rejected by _snapshot_validates and the tmp leaked.
        cleanup_paths.append(_BEST_SNAPSHOT_NAME)
        cleanup_paths.append(_BEST_SNAPSHOT_NAME + ".tmp.mp4")
        for f in cleanup_paths:
            if f and f != output_path and f != video_path and os.path.exists(f):
                try:
                    os.remove(f)
                except OSError:
                    pass

        # Restore the registry's strength.max in case it was overridden by the
        # dynamic-bounds block above. Single-pass --inject doesn't read this
        # field but other code paths or future iterative runs do, so we leave a
        # clean registry behind.
        KNOB_REGISTRY['strength']['max'] = original_strength_max

        # Return the data the CLI needs to drive Claude diagnosis + caller
        # bookkeeping. history_path is the JSON exported earlier this method;
        # it stays at its original location after the output rename.
        return {
            'best_metrics': dict(best_metrics),
            'best_iter': int(best_iter),
            'history_path': history_path if os.path.exists(history_path) else None,
            'output_path': final_output_path,
        }

    @staticmethod
    def _score_all_metrics(m: Dict[str, float]) -> float:
        """Iteration scoring that rewards *just-passing* metrics over
        *maximised* metrics, while making below-floor SNR or motion
        dominate the score so they can never win.

        Post-ROI-fix (v5.1) tuning:
          - SNR below-floor penalty doubled (2.0 -> 4.0 per dB); phase+harm
            bonuses can no longer mask a failing SNR.
          - Motion below-floor is now proportional to (floor - value)/floor
            on a 10-pt scale, not a tiny linear distance.
          - +3.0 bonus when all 5 metrics pass, so a slightly-worse-on-one
            but all-green iteration beats a better-on-one but one-failing.

        v5.2 change: phase/temporal/harmonic rewards are now FLAT once the
        metric passes (no over-achievement bonus). Previously the scorer paid
        up to 10 points for grinding phase from 14 deg to 5 deg and up to 2
        points for pushing temporal/harmonic above threshold, which gave the
        controller an incentive to keep disturbing green-zone metrics. The
        all-pass +3.0 bonus + per-metric +1.0 flat bonus still make any
        all-green iteration strictly preferable to any almost-green one.
        """
        s = 0.0

        # SNR: triangular reward peaking at snr_min+2 dB (= 10 dB with the
        # default 8 dB floor).  Above the comfort peak, reward falls off and
        # goes negative past snr_min+6 (~14 dB), which is where the injected
        # waveform starts to look obviously louder than ambient rPPG.
        snr = m['snr']
        snr_comfort = target_snr_min + 2.0
        if snr < target_snr_min:
            s -= (target_snr_min - snr) * 4.0
        elif snr > target_snr_max:
            s -= (snr - target_snr_max) * 4.0
        else:
            s += max(-3.0, 2.0 - abs(snr - snr_comfort) * 0.5)

        # Phase: flat +1.0 when passing; proportional penalty otherwise.
        # No reward for driving phase below the threshold - that only
        # burns slack the controller needs for other metrics.
        if m['phase'] <= target_phase_coherence:
            s += 1.0
        else:
            s += max(-30.0, target_phase_coherence - m['phase'])

        # Temporal: flat +1.0 when passing; capped penalty otherwise.
        if m['temporal'] >= target_temporal_consistency:
            s += 1.0
        else:
            s -= min(10.0, (target_temporal_consistency - m['temporal']) * 10.0)

        # Harmonic: flat +1.0 when passing; capped penalty otherwise.
        if m['harmonic'] >= target_harmonic_alignment:
            s += 1.0
        else:
            s -= min(10.0, (target_harmonic_alignment - m['harmonic']) * 10.0)

        # Motion: hump reward inside band; proportional penalty outside.
        motion = m['motion']
        if target_motion_artifacts_min <= motion <= target_motion_artifacts:
            s += min(motion - target_motion_artifacts_min,
                     target_motion_artifacts - motion) * 20.0
        elif motion < target_motion_artifacts_min:
            # Scale by floor so motion=0 -> -10, motion=0.016 -> -2.
            s -= (target_motion_artifacts_min - motion) / target_motion_artifacts_min * 10.0
        else:
            s -= (motion - target_motion_artifacts) / target_motion_artifacts * 10.0

        # v6 spectrum_realism (high-weight when available). Passing >= 0.85
        # contributes +5; below 0.85 contributes up to -V6_SPECTRUM_SCORE_WEIGHT.
        # Skipped when v6_spectrum_scorer is unavailable (key absent).
        if 'spectrum_realism' in m:
            sr = float(m['spectrum_realism'])
            if sr >= target_spectrum_realism:
                # Capped reward so it can't dwarf the all-pass bonus, but
                # still rewards genuinely high realism.
                s += 5.0 + min(2.0, (sr - target_spectrum_realism) * 10.0)
            else:
                # Strong penalty so the controller actively avoids picks
                # that smear the spectrum.
                s -= (target_spectrum_realism - sr) * V6_SPECTRUM_SCORE_WEIGHT

        # All-pass bonus so a fully-green iteration always beats one with
        # any metric still failing, even if the failing iteration scores
        # higher individual bonuses elsewhere.
        if (target_snr_min <= snr <= target_snr_max
                and m['phase'] <= target_phase_coherence
                and m['temporal'] >= target_temporal_consistency
                and m['harmonic'] >= target_harmonic_alignment
                and target_motion_artifacts_min <= motion <= target_motion_artifacts):
            s += 3.0
        return s

    def _print_final_knob_summary(self, params: 'PulseParams') -> None:
        """One-line summary of the final tuned knob values (vs defaults).

        Handles numeric fields (epsilon compare) and non-numeric fields
        (e.g. v6 pulse_source string) separately.
        """
        default = PulseParams()
        changes = []
        for k, v in params.snapshot().items():
            dv = getattr(default, k, v)
            if isinstance(v, (int, float)) and isinstance(dv, (int, float)):
                if abs(v - dv) > 1e-9:
                    changes.append(f"{k}={v:.4g} (was {dv:.4g})")
            elif v != dv:
                changes.append(f"{k}={v} (was {dv})")
        if not changes:
            return
        print(self._c("  Final knob diff: " + ', '.join(changes), 'DIM'))

    @staticmethod
    def _derive_iter_output_path(output_path: str, iter_num: int) -> str:
        """For 'output/foo.mp4' + iter 3, return 'output/foo_iter3.mp4'."""
        base, ext = os.path.splitext(output_path)
        return f"{base}_iter{iter_num}{ext}"

    def _resolve_keep_iterations(self, spec: str, best_iter: int,
                                  iter_files: Dict[int, Dict[str, Any]]
                                  ) -> set:
        """Resolve the --keep-iterations spec into a set of iter numbers.

        Modes:
          * 'prompt' (default): interactive picker using the table the user
            has just seen on screen; empty input keeps only best, non-TTY
            stdin auto-resolves to none.
          * 'none': only best is saved; no prompt.
          * 'all': every iteration is saved alongside best; no prompt.

        Unknown values silently fall back to 'prompt' rather than failing
        an expensive run on a CLI typo.
        """
        spec_norm = (spec or 'prompt').strip().lower()
        if spec_norm == 'none':
            return set()
        if spec_norm == 'all':
            return set(iter_files.keys())
        return self._prompt_for_iterations(best_iter, iter_files)

    def _prompt_for_iterations(self, best_iter: int,
                               iter_files: Dict[int, Dict[str, Any]]) -> set:
        """CSV-entry prompt for additional iterations to save.

        The comparison table has already been printed, so this prompt
        doesn't repeat the data - it just asks for picks. Non-TTY stdin
        (scripted runs, CI) auto-skips with a brief notice so the run
        doesn't block on input().
        """
        c = self._c
        if not sys.stdin.isatty():
            print(c("  Non-interactive stdin; saving only the best iteration.", 'Y'))
            return set()

        try:
            user_in = input(
                "\n  Save additional iteration(s)? "
                "[comma-separated numbers, or just press Enter to skip]: "
            ).strip()
        except (EOFError, KeyboardInterrupt):
            print()
            return set()

        if not user_in:
            return set()

        picks: set = set()
        for tok in user_in.split(','):
            tok = tok.strip()
            if not tok:
                continue
            try:
                n = int(tok)
            except ValueError:
                print(c(f"  Ignoring invalid token: {tok!r}", 'Y'))
                continue
            if n not in iter_files:
                print(c(f"  Iter {n} not available; ignoring.", 'Y'))
                continue
            picks.add(n)
        return picks


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def main():
    import argparse
    parser = argparse.ArgumentParser(
        description='rPPG Corrector v5.0 - 2026-Ready Detection & Manipulation'
    )
    parser.add_argument('video_path', nargs='?', default=None,
                        help='Path to input video (required for --analyze / --inject / --iterative)')
    parser.add_argument('--diagnose', metavar='HISTORY_JSON', default=None,
                        help='Diagnose a saved iteration_history JSON via Claude. Pairs with the matching _metrics_summary.tsv if present. Requires ANTHROPIC_API_KEY in the environment. When set, all other flags except --diagnose-model are ignored and the tool exits after writing the diagnosis to stdout.')
    parser.add_argument('--diagnose-model', default='claude-opus-4-7',
                        help='Claude model to use for --diagnose and the auto-diagnosis after --inject (default claude-opus-4-7).')
    parser.add_argument('--skip-diagnosis', action='store_true',
                        help='Skip the automatic Claude diagnosis after --inject --iterative runs. Diagnosis is on by default and uses --diagnose-model. Has no effect on --analyze (which never triggers diagnosis) or on single-pass --inject (no iteration history to send).')
    parser.add_argument('--analyze', action='store_true', help='Analyze video for rPPG')
    parser.add_argument('--inject', '--manipulate', action='store_true', dest='inject',
                        help='Inject phase-aligned rPPG pulses into the video. '
                             '(--manipulate is the v5 spelling; both still work.)')
    parser.add_argument('--iterative', action='store_true', help='Use iterative PID enhancement')
    parser.add_argument('--output', default=None, help='Output path (auto-generated if not specified)')
    parser.add_argument('--target-hr', type=float, default=randomHr, help='Target heart rate in bpm')
    parser.add_argument('--strength', type=float, default=base_strength, help='Pulse signal strength')
    parser.add_argument('--face-model', default=None, help='Path to MediaPipe FaceLandmarker .task model')
    parser.add_argument('--grain-video', default=None, help='Optional film grain video for overlay')
    parser.add_argument('--enable-final-fx', action='store_true',
                        help='Enable the post-rPPG FFmpeg filter chain (curves/unsharp/CAS). Off by default because these filters degrade the injected pulse signal (particularly phase coherence via curves remapping). The re-encode to source-matching codec/bitrate still runs regardless.')
    parser.add_argument('--legacy-pipeline', action='store_true',
                        help='Use v4.1 combined FFmpeg pipeline (deband+curves+unsharp+CAS+grain in one pass)')
    parser.add_argument('--landmark-stride', type=int, default=1,
                        help='Run MediaPipe face detection only every Nth frame; the ROIStabilizer carries the shape between detections. Default 1 (every frame). Try 3-5 for a 3-5x reduction in per-frame detection cost at negligible quality loss on mostly-still faces.')
    parser.add_argument('--legacy-iteration-tuning', action='store_true',
                        help='Revert to v5.2 iteration behaviour: aggressive strength calibration (gain 0.5, no deadband), no secondary-metric phase gate, no motion floor guard. Default v5.3 behaviour softens iter 1 so iter 2-3 can contribute real gap closure.')
    parser.add_argument('--keep-iterations', default='prompt',
                        choices=['prompt', 'none', 'all'],
                        help='Save additional iterations alongside the best one. "prompt" (default) shows the all-iterations table and asks for a comma-separated list of iter numbers; press Enter to save only best. "none" skips the prompt and saves only best. "all" skips the prompt and saves every iteration. Extras land in the output folder with an _iterN suffix on the filename. Non-TTY stdin auto-resolves to "none" so scripted runs do not block.')
    parser.add_argument('--iterate-from-baseline', action='store_true',
                        help='In iterative mode, every iteration applies its params to the ORIGINAL input video instead of the previous iteration\'s output. Eliminates cumulative encoding loss across iterations and gives the controller cleaner slope estimates (each iter\'s metrics reflect only that iter\'s params, not all prior cumulated changes). Off by default (cumulative behaviour preserved for back-to-back testing).')
    parser.add_argument('--boost-existing-pulse', action='store_true',
                        help='In iterative mode, detect the existing rPPG signal in the baseline input (heart rate + per-ROI starting phases) and align the synthetic pulse injection to it. Synthetic pulse adds CONSTRUCTIVELY to the natural signal already in the video, amplifying it instead of competing with it. Falls back to random-pulse generation when baseline detection is unreliable (low SNR, out-of-range HR). Off by default for back-to-back testing.')
    parser.add_argument('--pulse-source', default='synthetic',
                        choices=['synthetic', 'real', 'blend'],
                        help='v6 injection waveform source. "synthetic" (default) uses the v5 sin+h2+h3 harmonic construction. "real" uses a template from v6_real_pulse_library (synthetic-realistic PPG morphology with proper dicrotic notch + HRV; can be augmented with real .npy traces dropped in v6_pulse_bank/). "blend" mixes synthetic + library at --pulse-blend-weight.')
    parser.add_argument('--pulse-blend-weight', type=float, default=0.5,
                        help='Blend weight when --pulse-source=blend. 0.0 = pure synthetic, 1.0 = pure library. Default 0.5. Ignored otherwise.')
    parser.add_argument('--output-bitrate-mult', type=float, default=1.0,
                        help='Multiplier on the source video bitrate at output encode time. Default 1.0 matches v5 source-matching behaviour. Use 2.0-4.0 when injecting into low-bitrate AI-gen inputs where the default match strips out the subtle rPPG modulation. Confirmed cache-vs-output gap: in-memory metrics show clean injection but --analyze on the saved file shows near-baseline metrics.')
    parser.add_argument('--min-strength', type=float, default=0.005,
                        help='Minimum strength floor for the dynamic strength calculator. v5 floor was strength_min (0.0015) which produces peak per-pixel green-channel modulation of only +/-0.18 - below the uint8 quantisation step at ffmpeg-input, so the modulation vanishes from the saved file even though it shows in cached metrics. Default 0.005 keeps peak modulation at +/-0.6 (~1 pixel value). Set lower for ultra-subtle injection, higher for stronger.')
    parser.add_argument('--from-history', default=None,
                        help='Path to a previous run\'s iteration_history JSON. The PulseParams of the iteration selected by --use-iter (default: first iteration with a knob_change) will be loaded and applied as a single-shot injection on the new input video. Implies non-iterative inject mode; cannot be combined with --iterative. CLI flags like --pulse-source/--min-strength/--output-bitrate-mult are taken from the history record by default and only overridden if you explicitly pass them.')
    parser.add_argument('--use-iter', type=int, default=None,
                        help='Iteration number to replay from --from-history. Defaults to the first iteration with a knob_change (skipping baseline). Use this to replay a specific best iteration recorded in your idkit_verdicts.csv (e.g. --use-iter 2).')
    parser.add_argument('--skip-kinematic-gate', action='store_true',
                        help='Bypass the v8 facial-kinematic preflight gate. By default --analyze and --inject runs are gated on a head-pose-jerk + blink-distribution score; videos below threshold are hard-skipped before the rPPG pipeline runs. Use this flag during calibration or to force-process a borderline video.')
    parser.add_argument('--kinematic-threshold', type=float, default=KINEMATIC_PASS_THRESHOLD,
                        help=f'Pass threshold for the kinematic gate (0..1). Default {KINEMATIC_PASS_THRESHOLD}. Higher = stricter.')
    args = parser.parse_args()

    # --- Diagnose mode short-circuits everything else ---
    if args.diagnose:
        from rppg_diagnose import diagnose
        sys.exit(diagnose(args.diagnose, model=args.diagnose_model))

    if args.video_path is None:
        parser.error('video_path is required (or pass --diagnose <history.json>)')

    # v8 facial-kinematic preflight gate. Only runs for --analyze / --inject
    # (other modes either have no video or already short-circuited above).
    # Fail-closed on signal, fail-open on infra (no model, unreadable, etc).
    if (args.analyze or args.inject) and not args.skip_kinematic_gate:
        gate_res = score_face_kinematics(
            args.video_path,
            pass_threshold=args.kinematic_threshold,
            model_path=args.face_model,
        )
        print_gate_banner(gate_res, video_label=args.video_path)
        if not gate_res.passed:
            print("[GATE] Hard-skipping rPPG pipeline — kinematic score below "
                  f"threshold ({args.kinematic_threshold:.2f}).")
            print("[GATE] Use --skip-kinematic-gate to force-process this video.")
            sys.exit(2)

    if args.output is None:
        args.output = generate_output_path(args.video_path)

    _version_parts = ['v5.10']
    if V6_SPECTRUM_AVAILABLE:
        _version_parts.append('spectrum')
    if V6_PULSE_LIBRARY_AVAILABLE:
        _version_parts.append('pulse-lib')
    print(f"rPPG Corrector {'+'.join(_version_parts)}"
          + (" (v6 modules active)" if len(_version_parts) > 1 else ""))
    print(f"Target heart rate: {randomHr:.1f} bpm")
    print(f"Output: {args.output}")
    print(f"Pipeline: {'legacy (v4.1)' if args.legacy_pipeline else 'v5 (deband-first)'}")
    print(f"Targets: SNR {target_snr_min}-{target_snr_max} dB, "
          f"Phase <= {target_phase_coherence} deg, "
          f"Temporal >= {target_temporal_consistency}, "
          f"Motion {target_motion_artifacts_min}-{target_motion_artifacts}, "
          f"Harmonic >= {target_harmonic_alignment}")

    enable_post_fx = args.enable_final_fx

    analyzer = AdvancedRPPGInjector(face_model_path=args.face_model,
                                    landmark_stride=args.landmark_stride)

    if args.analyze:
        print("\nAnalyzing video for rPPG signals...")
        results = analyzer.analyze_video(args.video_path)
        adv = results.get('advanced_phase', {})
        print("\n=== rPPG Analysis Results (v5.0) ===")
        print(f"Global SNR: {results['global_snr']:.2f} dB (target: {target_snr_min}-{target_snr_max} dB)")
        print(f"Phase Coherence: {results['phase_coherence']:.1f} deg (target: <= {target_phase_coherence} deg)")
        print(f"Temporal Consistency: {results.get('temporal_consistency', 0):.2f} (target: >= {target_temporal_consistency})")
        print(f"Motion Artifacts: {results.get('motion_artifacts', 0):.2f} (target: {target_motion_artifacts_min}-{target_motion_artifacts})")
        print(f"Harmonic Alignment: {adv.get('harmonic_alignment', 0):.2f} (target: >= {target_harmonic_alignment})")
        print(f"Physiological Sync: {adv.get('physiological_sync', 0):.2f}")
        if 'spectrum_realism' in results:
            print(f"Spectrum Realism: {results['spectrum_realism']:.3f} (v6 informational - higher = more pulse-like FFT shape)")
        print(f"Heart Rate: {results['heart_rate']:.1f} bpm")
        print(f"Test Result: {'PASS' if results['passes_test'] else 'FAIL'}")
        print("\nROI-specific results:")
        for roi_name, roi_data in results['roi_results'].items():
            seg = roi_data.get('segmented_snr', {})
            print(f"  {roi_name}: SNR={roi_data['snr']:.2f} dB, HR={roi_data['heart_rate']:.1f} bpm, "
                  f"motion={seg.get('motion_artifacts', 0):.2f}, temporal={seg.get('temporal_consistency', 0):.2f}")
        analyzer.visualize_analysis(results)
        return

    # ── v6 --from-history: load PulseParams from a previous run's JSON ──
    # Replays a specific iteration's exact knob values as a single-shot
    # injection. Conflicts with --iterative; non-iterative inject only.
    history_params = None
    if args.from_history:
        if args.iterative:
            print("[ERROR] --from-history cannot be combined with --iterative. "
                  "It applies a fixed set of knob values from a previous run; "
                  "iteration would immediately overwrite them.")
            sys.exit(1)
        if not args.inject:
            print("[ERROR] --from-history requires --inject.")
            sys.exit(1)

        hist_path = os.path.abspath(args.from_history)
        if not os.path.isfile(hist_path):
            print(f"[ERROR] History JSON not found: {hist_path}")
            sys.exit(1)

        try:
            with open(hist_path, 'r', encoding='utf-8') as f:
                hist = json.load(f)
        except Exception as exc:
            print(f"[ERROR] Could not read history JSON: "
                  f"{type(exc).__name__}: {exc}")
            sys.exit(1)

        records = hist.get('records', [])
        if not records:
            print(f"[ERROR] History JSON has no records: {hist_path}")
            sys.exit(1)

        # Pick the iteration to replay
        if args.use_iter is not None:
            target = next(
                (r for r in records if int(r.get('iteration', -1)) == int(args.use_iter)),
                None,
            )
            if target is None:
                avail = sorted({int(r.get('iteration', -1)) for r in records})
                print(f"[ERROR] iteration {args.use_iter} not found in history. "
                      f"Available iterations: {avail}")
                sys.exit(1)
        else:
            # Default: first iteration with a knob_change (skips iter 0 baseline)
            target = next(
                (r for r in records
                 if int(r.get('iteration', 0)) > 0 and r.get('knob_change')),
                records[0],
            )

        replay_iter = int(target.get('iteration', 0))
        replay_params_dict = target.get('params', {})
        if not replay_params_dict:
            print(f"[ERROR] iteration {replay_iter} has no params block.")
            sys.exit(1)

        # Build a PulseParams from the recorded dict. Use the dataclass field
        # set as the filter so unknown keys (future schema additions) are
        # ignored gracefully.
        default_pp = PulseParams()
        valid_fields = {f for f in default_pp.snapshot().keys()}
        filtered = {k: v for k, v in replay_params_dict.items() if k in valid_fields}
        history_params = replace(default_pp, **filtered)

        print(f"\nReplaying iteration {replay_iter} from {os.path.basename(hist_path)}")
        print(f"  Recorded metrics at that iter: "
              f"SNR={target.get('metrics', {}).get('snr', 0):.2f} "
              f"phase={target.get('metrics', {}).get('phase', 0):.1f} "
              f"temporal={target.get('metrics', {}).get('temporal', 0):.3f} "
              f"harmonic={target.get('metrics', {}).get('harmonic', 0):.3f}")
        print(f"  Loaded params: strength={history_params.strength:.5f}, "
              f"h2_amp={history_params.h2_amp}, h3_amp={history_params.h3_amp}, "
              f"mayer_amp={history_params.mayer_amp}, "
              f"resp_amp={history_params.resp_amp}, "
              f"envelope_burst_prob={history_params.envelope_burst_prob}, "
              f"pulse_source={history_params.pulse_source}, "
              f"output_bitrate_mult={history_params.output_bitrate_mult}")

    if args.inject:
        manipulator = PhaseAlignedRPPGManipulator(analyzer)
        if args.iterative:
            iter_result = manipulator.iterative_enhancement(
                args.video_path,
                args.output,
                args.target_hr,
                grain_video_path=args.grain_video,
                enable_final_fx=enable_post_fx,
                source_settings_video_path=args.video_path,
                legacy_pipeline=args.legacy_pipeline,
                legacy_iteration_tuning=args.legacy_iteration_tuning,
                keep_iterations=args.keep_iterations,
                iterate_from_baseline=args.iterate_from_baseline,
                boost_existing_pulse=args.boost_existing_pulse,
                pulse_source=args.pulse_source,
                pulse_blend_weight=args.pulse_blend_weight,
                output_bitrate_mult=args.output_bitrate_mult,
                min_strength_floor=args.min_strength,
            )

            # Auto-diagnose: send the iteration history to Claude for analysis
            # unless --skip-diagnosis was passed. Runs after all output files
            # are written and the run summary has printed.
            if (not args.skip_diagnosis
                    and iter_result
                    and iter_result.get('history_path')):
                print()  # blank line before diagnosis section
                try:
                    from rppg_diagnose import diagnose
                    diagnose(iter_result['history_path'],
                             model=args.diagnose_model)
                except ImportError as exc:
                    print(f"\nClaude diagnosis skipped: {exc}", file=sys.stderr)
                except Exception as exc:
                    # Diagnosis is opportunistic — never let it kill the run.
                    print(f"\nClaude diagnosis failed: {type(exc).__name__}: {exc}",
                          file=sys.stderr)
        else:
            print("\nAnalyzing input video...")
            original_results = analyzer.analyze_video(args.video_path)
            adv_orig = original_results.get('advanced_phase', {})
            print(f"Current - SNR: {original_results['global_snr']:.2f} dB, "
                  f"Phase: {original_results['phase_coherence']:.1f} deg, "
                  f"Temporal: {original_results.get('temporal_consistency', 0):.2f}, "
                  f"Motion: {original_results.get('motion_artifacts', 0):.2f}, "
                  f"Harmonic: {adv_orig.get('harmonic_alignment', 0):.2f}")
            orig_face = analyzer.compute_face_coherence_scores(args.video_path)
            print(f"Current face - geometry: {orig_face['geometry']:.4f}, "
                  f"eye_variations: {orig_face['eye_variations']:.4f}")

            # Build a params object so v6 pulse-source flags reach derive_roi_pulse.
            # The non-iterative path otherwise constructs a default PulseParams
            # inside apply_phase_aligned_pulses, which would force synthetic mode.
            # When --from-history was used, replay those exact knob values
            # instead. CLI flags still override the loaded params if explicitly
            # passed (argparse defaults are NOT considered explicit; this is
            # the common "load from JSON, run on a new video" workflow).
            if history_params is not None:
                _params = history_params
            else:
                _params = PulseParams(
                    strength=args.strength,
                    pulse_source=args.pulse_source,
                    pulse_blend_weight=args.pulse_blend_weight,
                    output_bitrate_mult=args.output_bitrate_mult,
                )
            manipulator.apply_phase_aligned_pulses(
                args.video_path,
                args.output,
                args.target_hr,
                args.strength,
                grain_video_path=args.grain_video,
                enable_final_fx=enable_post_fx,
                source_settings_video_path=args.video_path,
                legacy_pipeline=args.legacy_pipeline,
                params=_params,
            )

            print("\nAnalyzing enhanced video...")
            enhanced_results = analyzer.analyze_video(args.output)
            adv_enh = enhanced_results.get('advanced_phase', {})
            print(f"Enhanced - SNR: {enhanced_results['global_snr']:.2f} dB, "
                  f"Phase: {enhanced_results['phase_coherence']:.1f} deg, "
                  f"Temporal: {enhanced_results.get('temporal_consistency', 0):.2f}, "
                  f"Motion: {enhanced_results.get('motion_artifacts', 0):.2f}, "
                  f"Harmonic: {adv_enh.get('harmonic_alignment', 0):.2f}")

            # Rename the single-pass output with the metric suffix to match
            # the iterative path's naming convention.
            final_metrics = manipulator._extract_metrics(enhanced_results)
            renamed_output = add_metric_suffix(args.output, final_metrics)
            if renamed_output != args.output and os.path.exists(args.output):
                os.replace(args.output, renamed_output)
                args.output = renamed_output
                print(f"\nOutput renamed: {renamed_output}")

            enh_face = analyzer.compute_face_coherence_scores(args.output)
            g_delta = enh_face['geometry'] - orig_face['geometry']
            e_delta = enh_face['eye_variations'] - orig_face['eye_variations']
            print(f"Enhanced face - geometry: {enh_face['geometry']:.4f} "
                  f"({g_delta:+.4f}), eye_variations: {enh_face['eye_variations']:.4f} "
                  f"({e_delta:+.4f})")
            print(f"Test Result: {'PASS' if enhanced_results['passes_test'] else 'FAIL'}")


if __name__ == "__main__":
    main()
