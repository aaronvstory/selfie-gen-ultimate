"""
Runtime dependency health check and repair for Kling UI startup.
"""

from __future__ import annotations

import argparse
import importlib
import os
import subprocess
import sys
from typing import Callable

from kling_gui.ml_backend_env import ensure_ml_backend_env

ImportFn = Callable[[str], object]
RuntimeProbeFn = Callable[[], tuple[object | None, str]]

# numpy FIRST + hard-pinned 1.26.4: TF 2.16.2 (ml-dtypes~=0.3.1) is built
# against numpy 1.26.x and breaks at import with "numpy.core._multiarray_umath
# failed to import" under numpy 2.x. Without numpy here, a --force-reinstall of
# deepface/retina-face could pull numpy 2.x and silently break TF — the failure
# a user hit on v2.9. Pin it explicitly + first so the repair can't regress it.
#
# Built once via a pure helper (gemini MED @37): the previous
# ``REPAIR_PACKAGES.insert(...)`` at import mutated the module-level list, which
# is NOT idempotent — a module reload (or a second import in a test harness)
# would insert tensorflow-intel twice. The helper below splices it in
# declaratively so re-import always yields the same list.
_BASE_REPAIR_PACKAGES = [
    "numpy==1.26.4",
    "tensorflow==2.16.2",
    "protobuf==4.25.3",
    "tf-keras==2.16.0",
    "retina-face==0.0.17",
    "deepface==0.0.92",
    # v2.17: scipy + absl-py are part of the COMPLETE runtime set (rPPG +
    # mediapipe deps) and were previously only repaired via rPPG self-heal.
    # Both declare clean numpy-compatible deps and do NOT pull numpy 2.x, so
    # they're safe as plain ==-style pins here (UNLIKE mediapipe, which must
    # stay --no-deps — see run_repair's dedicated mediapipe step).
    "scipy>=1.11,<2",
    "absl-py>=2.3,<3",
]

# v2.17: mediapipe is repaired via a DEDICATED --no-deps step (see run_repair),
# never as a plain REPAIR_PACKAGES entry. A bare `mediapipe==0.10.35` would let
# pip resolve its transitive deps and pull numpy 2.x back in, re-breaking TF
# 2.16.2 (the exact v2.10/v2.13 fresh-install bug). Pinned here so the launcher
# echo blocks + the run_repair step share one source of truth.
_MEDIAPIPE_SPEC = "mediapipe==0.10.35"


def _with_win_tensorflow_intel(base: "list[str]") -> "list[str]":
    """On win32, splice ``tensorflow-intel`` in right after the ``tensorflow``
    pin, deriving its version from that pin so the two never drift.

    gemini MED: hardcoding ``index("tensorflow==2.16.2")`` AND a literal
    ``"tensorflow-intel==2.16.2"`` meant a future TF bump had to be made in two
    places or the splice would either raise ValueError (index miss) or pin a
    stale intel version. Locate the ``tensorflow==`` entry by prefix and reuse
    its exact version string. If no ``tensorflow==`` pin exists, return a copy
    unchanged rather than raising. Pure function — re-import always yields the
    same list (the idempotency the @37 finding asked for)."""
    for i, pkg in enumerate(base):
        # Tolerant match (gemini MED): a requirement may carry surrounding
        # whitespace or differing case ("  TensorFlow==2.16.2"). Normalize
        # before the prefix test, but splice the ORIGINAL entry's version so
        # the spliced intel pin matches whatever the tensorflow line declares.
        normalized = pkg.strip().lower()
        if normalized.startswith("tensorflow==") and not normalized.startswith(
            "tensorflow-intel=="
        ):
            version = pkg.strip().split("==", 1)[1].strip()
            return base[: i + 1] + [f"tensorflow-intel=={version}"] + base[i + 1 :]
    return list(base)


if sys.platform == "win32":
    REPAIR_PACKAGES = _with_win_tensorflow_intel(_BASE_REPAIR_PACKAGES)
else:
    REPAIR_PACKAGES = list(_BASE_REPAIR_PACKAGES)


def _constraints_path() -> "str | None":
    """Resolve the repo-root ``constraints.txt`` (frozen-aware).

    Returned path is passed via ``pip install -c`` so the repair's
    --force-reinstall can't let a transitive deepface/retina-face resolve
    upgrade numpy past 1.x. Returns None if the file can't be located, so
    the caller degrades to the (still-pinned) REPAIR_PACKAGES list rather
    than crashing on a partial tree.
    """
    import os

    candidates: list = []
    # Narrow except (GPT review, PR #65): only swallow the EXPECTED failures —
    # path_utils absent (ImportError) or get_app_dir misbehaving
    # (AttributeError/TypeError/OSError) — so a genuinely unexpected error isn't
    # silently hidden, dropping the -c safety. The __file__ fallback below still
    # runs regardless.
    try:
        from path_utils import get_app_dir, is_frozen  # local import: optional

        if callable(is_frozen) and is_frozen() and callable(get_app_dir):
            app_dir = get_app_dir()
            # get_app_dir() can return None on a partial/odd layout — guard so
            # os.path.join(None, ...) can't raise TypeError.
            if app_dir:
                candidates.append(os.path.join(app_dir, "constraints.txt"))
    except (ImportError, AttributeError, TypeError, OSError):
        pass

    candidates.append(os.path.join(os.path.dirname(os.path.abspath(__file__)), "constraints.txt"))
    for cand in candidates:
        if cand and os.path.isfile(cand):
            return cand
    return None


def assert_numpy_pinned(
    version_reader: "Callable[[str], str]" = None,
) -> "str | None":
    """Return a failure string if the installed numpy is >= 2, else None.

    numpy 2.x is the root cause of the v2.10 fresh-install Face Crop break:
    TensorFlow 2.16.2 (ml-dtypes~=0.3.1) is built against numpy 1.26.x and
    fails at import with "numpy.core._multiarray_umath failed to import"
    under numpy 2.x. The import-probe in check_runtime_dependencies CAN miss
    this when numpy itself still imports but TF's C-extension breaks later,
    so we additionally assert the resolved version directly. Parameterized
    on ``version_reader`` for testability (monkeypatch to simulate 2.x).
    """
    import importlib.metadata as _md

    if version_reader is None:
        version_reader = _md.version
    try:
        raw = version_reader("numpy")
    except _md.PackageNotFoundError:
        # numpy not installed at all → the cv2/numpy import probe covers it.
        return None
    except Exception as exc:
        return f"numpy version lookup failed: {type(exc).__name__}: {exc}"
    try:
        major = int(str(raw).split(".", 1)[0])
    except Exception:
        return f"numpy version unparseable: {raw!r}"
    if major >= 2:
        return (
            f"numpy too new: {raw} (need <2; TF 2.16.2 breaks with "
            "'numpy.core._multiarray_umath failed to import' under numpy 2.x)"
        )
    return None


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

    for module_name in ("cv2", "numpy", "scipy", "absl"):
        try:
            importer(module_name)
        except Exception as exc:
            failures.append(f"{module_name} import failed: {type(exc).__name__}: {exc}")

    # v2.17: mediapipe is part of the COMPLETE runtime set (Face Crop / oldcam
    # landmark path). A bare `import mediapipe` PASSES even when the Tasks API
    # is missing/broken, so probe the deeper Tasks-API symbol the app actually
    # uses (FaceLandmarker) — mirrors setup_macos.sh's MP_VALIDATE_CMD. This is
    # the structural hole that let a partial venv (no mediapipe/scipy/absl) get
    # cached as "healthy": only rPPG's own self-heal caught it before.
    try:
        importer("mediapipe")
        vision_mod = importer("mediapipe.tasks.python.vision")
        if not hasattr(vision_mod, "FaceLandmarker"):
            failures.append(
                "mediapipe Tasks API incomplete: "
                "mediapipe.tasks.python.vision.FaceLandmarker missing"
            )
    except Exception as exc:
        failures.append(f"mediapipe import failed: {type(exc).__name__}: {exc}")

    # Direct version assert: numpy can import fine yet still be 2.x (which
    # breaks TF 2.16.2's C-extension on a later call, not at numpy import).
    # Catching it here forces the launcher to repair the venv instead of
    # caching it as healthy — the v2.10 fresh-install Face Crop bug.
    #
    # Read the version from DISK METADATA (importlib.metadata.version), NOT by
    # importing numpy: in exactly this broken numpy-2-vs-TF state, importing
    # numpy can itself be unsafe/crash, and we must not make the health probe
    # depend on the very import it's checking (GPT review, PR #65). Metadata is
    # read straight from the installed dist-info on disk. The injected
    # ``version_reader`` parameter on assert_numpy_pinned keeps it unit-testable
    # without touching the real environment.
    numpy_failure = assert_numpy_pinned()
    if numpy_failure:
        failures.append(numpy_failure)

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
    # errors="replace": pip can emit non-UTF-8 bytes (a dependency's package
    # metadata, a localized OS error) on stdout/stderr; with text=True a bare
    # decode would raise UnicodeDecodeError and abort the health repair instead
    # of surfacing the pip failure detail. Replace undecodable bytes so the
    # repair flow stays robust.
    completed = subprocess.run(
        cmd, capture_output=True, text=True, errors="replace", check=False
    )
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

    # numpy==1.26.4 is pinned as an explicit top-level entry in REPAIR_PACKAGES,
    # so pip's resolver holds numpy at exactly 1.26.4 even though deepface /
    # retina-face declare an open numpy requirement — that exact pin is the real
    # protection against numpy 2.x breaking TF 2.16.2's import. (Every entry is
    # a ==pin, so --upgrade is a no-op and --upgrade-strategy only-if-needed
    # would be inert alongside --force-reinstall; both are deliberately omitted.
    # Code-review H1, PR #61.) --force-reinstall ensures a clean reinstall over a
    # half-broken stack; --no-cache-dir avoids a corrupted wheel cache.
    # -c constraints.txt: even though every REPAIR_PACKAGES entry is a ==pin,
    # --force-reinstall re-resolves the transitive deps of deepface /
    # retina-face (which declare an OPEN numpy upper bound), so without the
    # constraints file a force-reinstall could still pull numpy 2.x back in.
    # Threading the constraints through makes numpy 2.x un-selectable. Degrade
    # to the bare pinned list if the file can't be located on a partial tree.
    constraints = _constraints_path()
    constraint_args = ["-c", constraints] if constraints else []
    cmd = [
        sys.executable,
        "-m",
        "pip",
        "install",
        "--force-reinstall",
        "--no-cache-dir",
        *constraint_args,
        *REPAIR_PACKAGES,
    ]
    # errors="replace": see run_torch_cpu_fallback — keep the repair robust
    # against non-UTF-8 pip output rather than crashing on decode.
    completed = subprocess.run(
        cmd, capture_output=True, text=True, errors="replace", check=False
    )
    if completed.returncode == 0:
        messages.append("repair install completed")
        face_ok = True
    else:
        details = _extract_pip_failure_detail(completed)
        messages.append(f"repair failed (code {completed.returncode}): {details}")
        face_ok = False

    # v2.17: mediapipe repair as a SEPARATE --no-deps step. mediapipe must NOT
    # go through the REPAIR_PACKAGES force-reinstall (which re-resolves deps)
    # because its declared deps would pull numpy 2.x and re-break TF 2.16.2.
    # --no-deps installs the mediapipe wheel alone; the numpy<2 / opencv caps
    # already satisfied by the face-stack install above remain intact. -c
    # constraints is still threaded as belt-and-suspenders.
    mp_cmd = [
        sys.executable,
        "-m",
        "pip",
        "install",
        "--force-reinstall",
        "--no-cache-dir",
        "--no-deps",
        *constraint_args,
        _MEDIAPIPE_SPEC,
    ]
    mp_completed = subprocess.run(
        mp_cmd, capture_output=True, text=True, errors="replace", check=False
    )
    if mp_completed.returncode == 0:
        messages.append("mediapipe --no-deps repair completed")
        mediapipe_ok = True
    else:
        mp_details = _extract_pip_failure_detail(mp_completed)
        messages.append(
            f"mediapipe repair failed (code {mp_completed.returncode}): {mp_details}"
        )
        mediapipe_ok = False

    return (cuda_ok and face_ok and mediapipe_ok), "; ".join(messages)


def verify_in_fresh_process() -> tuple[bool, list[str]]:
    """Run check mode in a new interpreter to avoid stale import cache after repair."""
    # Frozen-aware (code-review, PR #65): in a PyInstaller/frozen build
    # ``sys.executable`` is the app itself and ``__file__`` is inside the
    # bundle (not a runnable script), so ``[sys.executable, __file__, ...]``
    # would re-launch the GUI instead of running the health check. Resolve a
    # real Python interpreter for the subprocess. The repo doesn't currently
    # ship a frozen .exe, but this keeps the function correct if it ever does.
    if getattr(sys, "frozen", False):
        import shutil

        py = (
            shutil.which("python")
            or shutil.which("python3")
            or shutil.which("py")
        )
        if not py:
            # No standalone interpreter to re-exec — skip the fresh-process
            # verify rather than relaunching the app. The in-process check
            # already ran; report success so the caller doesn't loop.
            return True, []
        cmd = [py, os.path.abspath(__file__), "--mode", "check"]
    else:
        cmd = [sys.executable, __file__, "--mode", "check"]
    completed = subprocess.run(
        cmd, capture_output=True, text=True, errors="replace", check=False
    )
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
    # Round 8 (`611a9d7`) moved the partial-success classification into
    # `--mode check` itself (`_is_cuda_only_failure` → exit 0 with WARN).
    # `verify_in_fresh_process` runs the same `--mode check` subprocess,
    # so when only `torch_cuda_failure:*` remain, the subprocess now exits
    # 0 → `verify_in_fresh_process` returns `(True, [])` → the post-verify
    # `if ok_after: return 0` branch fires.
    #
    # Gemini PR #55 round-8 MED (#PRRT_kwDOSQUnmM6FQaDB) correctly
    # observed: the previous `non_cuda_failures` filter block at this
    # spot is now unreachable in practice — any path that would have
    # produced `(False, [<only torch_cuda_failure>])` from
    # `verify_in_fresh_process` is structurally impossible after the
    # round-8 exit-code change. Simplified to a single
    # success-or-fail branch on the verify result.
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
