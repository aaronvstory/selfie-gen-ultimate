"""Register the pip/uv-installed NVIDIA CUDA component DLL dirs on Windows.

Single source of truth (stdlib-only) for adding the
``site-packages/nvidia/<component>/bin`` directories to the Windows DLL search
path BEFORE ``import cupy``. Used by BOTH:

* ``rPPG/rppg_injector.py`` (the rPPG GPU frame-math path), and
* ``scripts/gpu_bootstrap.probe_cupy`` (the GUI's "is GPU ready?" probe).

Why this MUST be shared (not duplicated): the GUI probe spawns a fresh Python
subprocess and, until 2026-06-03, did ``import cupy`` WITHOUT registering these
dirs. On Python 3.8+ Windows a bare ``PATH`` entry is IGNORED for an extension
module's dependent DLLs — only ``os.add_dll_directory()`` works — so the probe's
kernel compile failed (``Could not find nvrtc64_*.dll``) EVEN ON a box where the
wheels were correctly installed. That false-negative would stamp ``install_failed``
and strand the user on CPU. Having the probe call the SAME registration the
injector uses keeps the two in lockstep.

CuPy's CUDA-component wheels drop their DLLs under
``site-packages/nvidia/<...>/bin`` (cu13 layout: ``nvidia/cu13/bin/x86_64``;
cu12 layout: ``nvidia/cuda_nvrtc/bin``). This module is CUDA-major agnostic — the
glob matches both. No-op off-Windows / when the dirs don't exist.

STDLIB-ONLY by contract: ``probe_cupy`` runs this in a fresh subprocess BEFORE
``import cupy``, and the launcher bootstrap chain imports it with the SYSTEM
Python before ``uv sync`` materialises the env. A third-party import here would
break GPU provisioning on a fresh install.
"""

import glob
import os
import shutil
import site
import sys

# Module-level list that RETAINS the os.add_dll_directory() handles for the
# whole process lifetime. CRITICAL (code-review 2026-06-03): on Windows the
# DLL search-path entry only stays active while the returned handle is alive —
# if it's GC'd, Python calls RemoveDllDirectory and nvrtc can no longer be
# found. Discarding the handle made the GPU fix flaky (it only worked because
# GC hadn't run yet). Keep them here so the entries persist until exit.
_CUDA_DLL_DIR_HANDLES = []

# Module-level set of dirs ALREADY registered this process, keyed by their
# normalized form (normcase+normpath). Persists ACROSS calls so a second
# register_cuda_dll_dirs() (e.g. the injector's module-level call followed by a
# later explicit call) does NOT re-add a handle or re-prepend PATH — that leaked
# DLL-dir handles + grew PATH unboundedly in a long-lived GUI process
# (CodeRabbit/Gemini PR #72). The per-call `seen` set only de-duped within one
# call; this is the cross-call guard.
_REGISTERED_CUDA_DLL_DIRS = set()


def _norm_key(path):
    """Normalized comparison key for a path (case + separators + quotes)."""
    return os.path.normcase(os.path.normpath(path.strip().strip('"')))


def _candidate_site_packages():
    """Site-packages dirs to scan for an ``nvidia/`` component tree."""
    sp_dirs = []
    if hasattr(site, "getsitepackages"):
        try:
            sp_dirs.extend(site.getsitepackages())
        except Exception:  # noqa: BLE001 — some embeds raise; fall through
            pass
    if hasattr(site, "getusersitepackages"):
        try:
            sp_dirs.append(site.getusersitepackages())
        except Exception:  # noqa: BLE001
            pass
    # Fallback for a virtualenv on Python < 3.11 where getsitepackages() is
    # absent: derive from sys.prefix (code-review MEDIUM-4 — the old
    # os.path.dirname(os.__file__) pointed at the stdlib Lib/ dir, NOT
    # Lib/site-packages, so the NVIDIA DLL dirs were never registered there and
    # CuPy's kernel compile failed). Windows venv layout = <prefix>/Lib/site-packages.
    sp_dirs.append(os.path.join(sys.prefix, "Lib", "site-packages"))
    return sp_dirs


def register_cuda_dll_dirs():
    """Add the NVIDIA CUDA component DLL dirs to the Windows DLL search path.

    Idempotent and safe to call multiple times (handles + PATH entries are
    de-duplicated). Returns the list of directories registered this call (for
    logging/tests); empty on non-Windows or when no ``nvidia/`` tree is present.
    """
    if not hasattr(os, "add_dll_directory"):
        return []
    # De-dup roots (order-preserving): getsitepackages() and the sys.prefix
    # fallback often resolve to the SAME site-packages dir, which would
    # otherwise glob + isdir the identical nvidia/ tree twice (gemini PR #72).
    roots = list(dict.fromkeys(
        os.path.abspath(os.path.join(sp, "nvidia"))
        for sp in _candidate_site_packages()
        if sp
    ))
    registered = []
    for root in roots:
        # EVERY filesystem touch (isdir/glob/abspath) is wrapped: an
        # antivirus quarantine / restricted-ACL / symlink-loop on one root must
        # NOT crash rPPG import — skip that root and continue. Worst case: no
        # dirs registered → CPU fallback, never a crash (external review PR #72).
        try:
            if not os.path.isdir(root):
                continue
            # glob.escape the install ROOT (the user's path may contain glob
            # metacharacters like [ ] — e.g. "C:\\...[backup]\\...") while
            # leaving the wildcard PATTERN unescaped (gemini PR #72).
            escaped_root = glob.escape(root)
            candidates = []
            for binglob in ("*/bin/x86_64", "*/bin", "*/lib/x64"):
                candidates.extend(glob.glob(os.path.join(escaped_root, binglob)))
        except OSError:
            continue
        for d in candidates:
            try:
                d = os.path.abspath(d)
                if not os.path.isdir(d):
                    continue
                # cu13 ships DLLs in bin/x86_64, not the bin parent. When a
                # matched ".../bin" has an x86_64 child, the DLLs are in the
                # child (already registered by the earlier glob) — skip the
                # parent. cu12 has no x86_64 child (DLLs live in bin), so keep it.
                if os.path.basename(d) == "bin" and os.path.isdir(
                    os.path.join(d, "x86_64")
                ):
                    continue
            except OSError:
                continue
            key = _norm_key(d)
            # Cross-call idempotence: if this dir was already registered in this
            # process, do NOT re-add the handle, re-prepend PATH, or report it as
            # newly-registered (external review PR #72).
            if key in _REGISTERED_CUDA_DLL_DIRS:
                continue
            _REGISTERED_CUDA_DLL_DIRS.add(key)
            try:
                # RETAIN the handle (module-level list) — the DLL dir entry is
                # removed when this object is GC'd, so a discarded handle makes
                # the nvrtc fix flaky (code-review CRITICAL).
                _CUDA_DLL_DIR_HANDLES.append(os.add_dll_directory(d))
            except OSError:
                pass
            # add_dll_directory lets cupy load nvrtc64_*.dll, but nvrtc ITSELF
            # then loads nvrtc-builtins64_*.dll via the plain PATH env (it's a C
            # lib, not a Python ext), so the dir must ALSO be on os.environ['PATH']
            # or the first kernel compile throws "failed to open
            # nvrtc-builtins64_*.dll". Normalize (case + separators + quotes +
            # surrounding whitespace) on BOTH sides before the membership test so
            # a differently-formatted existing entry can't cause a re-prepend
            # (gemini PR #72). The _REGISTERED set above already prevents a
            # second add for THIS process; this guards a pre-existing PATH entry.
            existing = {
                _norm_key(p)
                for p in os.environ.get("PATH", "").split(os.pathsep)
                if p.strip()
            }
            if key not in existing:
                os.environ["PATH"] = d + os.pathsep + os.environ.get("PATH", "")
            registered.append(d)
    return registered


# ---------------------------------------------------------------------------
# nvrtc JIT kernel-cache recovery + compile-error helpers
# ---------------------------------------------------------------------------
# Registering the DLL dirs (above) gets nvrtc to LOAD. The NEXT failure layer is
# the kernel COMPILE itself raising a CuPy CompileException even though the DLLs
# load fine. On a box whose nvidia wheels are correct, the #1 cause is a STALE
# on-disk JIT cache: cubins/PTX compiled against a PREVIOUS CUDA toolkit/driver
# that no longer load after the user upgrades their driver (the friend went CUDA
# 12.9 -> 13.x / driver 610.47, and CuPy then failed to compile its first kernel
# — cpp_dialect.h remark #20200-D was just the benign head of the log). Wiping
# the cache forces a clean recompile. These helpers are stdlib-only so BOTH the
# rPPG injector AND the GUI's gpu_bootstrap.probe_cupy (which runs in a fresh
# subprocess BEFORE `import cupy`) can call them with no third-party import.


def cupy_kernel_cache_dir():
    """Return CuPy's on-disk JIT kernel-cache directory (may not exist).

    Mirrors CuPy's own resolution order: ``$CUPY_CACHE_DIR`` when set, else
    ``~/.cupy/kernel_cache``. stdlib-only.
    """
    env = os.environ.get("CUPY_CACHE_DIR")
    if env and env.strip():
        return os.path.abspath(os.path.expanduser(env.strip()))
    return os.path.join(os.path.expanduser("~"), ".cupy", "kernel_cache")


def clear_cupy_kernel_cache():
    """Delete CuPy's on-disk JIT kernel cache. Return the path cleared, or None.

    Best-effort and fully wrapped: a locked / in-use / restricted cache dir must
    NEVER crash the caller — worst case the subsequent compile retry recompiles
    into the same dir. Returns None when there was nothing to clear.
    """
    cache_dir = cupy_kernel_cache_dir()
    try:
        if os.path.isdir(cache_dir):
            shutil.rmtree(cache_dir, ignore_errors=True)
            return cache_dir
    except OSError:
        pass
    return None


def cuda_include_dirs():
    """site-packages ``nvidia/<...>/include`` dirs that hold cuda_runtime.h etc.

    CuPy 13.x compiles kernels by ``#include``-ing the CUDA Runtime headers, and
    auto-detects them ONLY when the ``nvidia-cuda-runtime`` wheel version exactly
    matches the ``major.minor`` nvrtc reports (cupy._environment
    ._get_include_dir_from_conda_or_wheel). If nvrtc and the runtime wheel drift
    to different minors (our >=13.3,<14 pins let them float independently), CuPy
    finds ZERO include dirs and nvrtc compile fails with a cpp_dialect.h /
    missing-header CompileException — the friend's exact bug. This returns the
    real on-disk include dir(s) so the injector/probe can set
    ``CUPY_CUDA_INCLUDE_PATH`` explicitly and retry, regardless of version skew.
    cu13 layout: ``nvidia/cu13/include``; cu12: ``nvidia/<component>/include``.
    Order-preserving + de-duped; empty off-Windows / when absent. stdlib-only.
    """
    found = []
    seen = set()
    roots = list(dict.fromkeys(
        os.path.abspath(os.path.join(sp, "nvidia"))
        for sp in _candidate_site_packages()
        if sp
    ))
    for root in roots:
        try:
            if not os.path.isdir(root):
                continue
            escaped_root = glob.escape(root)
            # cu13 ships one consolidated include dir; cu12 ships per-component.
            candidates = glob.glob(os.path.join(escaped_root, "*", "include"))
        except OSError:
            continue
        for d in candidates:
            try:
                if not os.path.isdir(d):
                    continue
                # Only include dirs that actually carry a CUDA runtime header,
                # so we don't point CuPy at an unrelated component's include.
                has_hdr = (
                    os.path.isfile(os.path.join(d, "cuda_runtime.h"))
                    or os.path.isfile(os.path.join(d, "cuda.h"))
                )
                if not has_hdr:
                    continue
                key = _norm_key(d)
                if key in seen:
                    continue
                seen.add(key)
                found.append(os.path.abspath(d))
            except OSError:
                continue
    return found


def is_nvrtc_compile_error(err):
    """True when *err* is an nvrtc/JIT COMPILE failure (cache-clear + retry may
    fix it), as opposed to a missing module / absent CUDA device (which a cache
    wipe can't help). Matched by class name so the caller need not import the
    cupy exception types (cupy may be only partially importable at that point).
    """
    name = type(err).__name__.lower()
    return "compile" in name or "nvrtc" in name


def summarize_nvrtc_compile_error(err, limit=300):
    """Pull the meaningful nvrtc ``error:`` line(s) out of a CompileException.

    An nvrtc compile log OPENS with benign ``#pragma message`` REMARKS (e.g.
    ``cpp_dialect.h(41): remark #20200-D``). Head-truncating the raw string (the
    pre-2026-06-04 behaviour) therefore showed ONLY those remarks and cut off the
    actual ``error:`` line — which is exactly what hid the friend's real cause.
    Prefer lines naming an error/fatal over remark/pragma noise; fall back to the
    head when none are found.
    """
    raw = str(err).strip().replace("\r", "")
    lines = [ln.strip() for ln in raw.split("\n") if ln.strip()]
    keyed = [
        ln for ln in lines
        if ("error" in ln.lower() or "fatal" in ln.lower())
        and "remark" not in ln.lower()
    ]
    detail = " | ".join(keyed) if keyed else " ".join(lines)
    if len(detail) > limit:
        detail = detail[:limit] + "…"
    return detail


if __name__ == "__main__":  # tiny manual smoke: print what got registered
    dirs = register_cuda_dll_dirs()
    print("registered_cuda_dll_dirs=" + repr(dirs))
