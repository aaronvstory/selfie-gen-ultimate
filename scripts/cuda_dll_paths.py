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
import site
import sys

# Module-level list that RETAINS the os.add_dll_directory() handles for the
# whole process lifetime. CRITICAL (code-review 2026-06-03): on Windows the
# DLL search-path entry only stays active while the returned handle is alive —
# if it's GC'd, Python calls RemoveDllDirectory and nvrtc can no longer be
# found. Discarding the handle made the GPU fix flaky (it only worked because
# GC hadn't run yet). Keep them here so the entries persist until exit.
_CUDA_DLL_DIR_HANDLES = []


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
    roots = [
        os.path.join(sp, "nvidia") for sp in _candidate_site_packages() if sp
    ]
    registered = []
    seen = set()
    for root in roots:
        if not os.path.isdir(root):
            continue
        # Any bin dir under nvidia/* (cu13: nvidia/cu13/bin/x86_64;
        # cu12: nvidia/cuda_nvrtc/bin; some wheels ship lib/x64 too).
        # glob.escape the install ROOT (the user's path may contain glob
        # metacharacters like [ ] — e.g. an install under "C:\...[backup]\...")
        # while leaving the wildcard PATTERN unescaped (gemini MEDIUM PR #72).
        escaped_root = glob.escape(root)
        for binglob in ("*/bin/x86_64", "*/bin", "*/lib/x64"):
            for d in glob.glob(os.path.join(escaped_root, binglob)):
                d = os.path.abspath(d)
                if d in seen or not os.path.isdir(d):
                    continue
                # cu13 ships DLLs in bin/x86_64, not the bin parent. When a
                # matched ".../bin" has an x86_64 child, the DLLs are in the
                # child (already registered by the earlier glob) — skip the
                # parent to avoid a wasted handle + PATH entry. cu12 has no
                # x86_64 child (DLLs live directly in bin), so it's kept.
                if os.path.basename(d) == "bin" and os.path.isdir(
                    os.path.join(d, "x86_64")
                ):
                    continue
                seen.add(d)
                try:
                    # RETAIN the handle (module-level list) — the DLL dir entry
                    # is removed when this object is GC'd, so a discarded handle
                    # makes the nvrtc fix flaky (code-review CRITICAL).
                    _CUDA_DLL_DIR_HANDLES.append(os.add_dll_directory(d))
                except OSError:
                    pass
                # add_dll_directory lets cupy load nvrtc64_*.dll, but nvrtc
                # ITSELF then loads nvrtc-builtins64_*.dll via the plain PATH
                # env (it's a C lib, not a Python ext), so the dir must ALSO be
                # on os.environ['PATH'] or the first kernel compile throws
                # CompileException "failed to open nvrtc-builtins64_*.dll".
                # Compare case-insensitively so a second call (or a PATH entry
                # added with different casing) can't double-prepend the same dir
                # and grow PATH unboundedly across restarts (code-review CRITICAL
                # PR #72 — Windows paths are case-insensitive).
                existing = {
                    os.path.normcase(p)
                    for p in os.environ.get("PATH", "").split(os.pathsep)
                }
                if os.path.normcase(d) not in existing:
                    os.environ["PATH"] = d + os.pathsep + os.environ.get("PATH", "")
                registered.append(d)
    return registered


if __name__ == "__main__":  # tiny manual smoke: print what got registered
    dirs = register_cuda_dll_dirs()
    print("registered_cuda_dll_dirs=" + repr(dirs))
