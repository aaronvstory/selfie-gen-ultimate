#!/usr/bin/env python3
"""Granular per-module import diagnostic for the rPPG self-heal path.

Usage:
    python scripts/rppg_import_diag.py [mod1 mod2 ...]

With no args it checks the rPPG core set (cv2, numpy, mediapipe, scipy, absl).
For EACH module it attempts a real ``import`` (not just ``find_spec``) and reports
the outcome on ONE line, so the rPPG launcher — and, through it, the main GUI
log — can tell the user EXACTLY which module is broken and WHY, instead of the
useless "Core imports missing" summary the v2.13 launcher emitted.

Output (stdout, one line per module + a numpy-version line + a final verdict):

    [rppg-diag] OK      cv2          (4.11.0)
    [rppg-diag] MISSING mediapipe    ModuleNotFoundError: No module named 'mediapipe'
    [rppg-diag] BROKEN  numpy        ImportError: numpy.core.umath failed to import
    [rppg-diag] numpy-version: 2.4.1  <-- WARNING: numpy>=2 breaks TensorFlow/OpenCV
    [rppg-diag] RESULT: 2 module(s) not importable: mediapipe, numpy

Why this exists (v2.16 logging overhaul): the friend's v2.13 rPPG log showed
only "Core imports missing" / "Self-heal pip install did not satisfy imports"
with no module name — because the launcher's import check swallowed stderr
(``>nul 2>&1``) and the one ``find_spec`` diagnostic it had printed to the
console only, never to rppg.log (the sink the friend actually read). This helper
makes the per-module detail a first-class, tee-able output, and distinguishes
the two failure modes the old check conflated:

  * MISSING — the module isn't installed at all (find_spec is None). pip didn't
    land it (skipped, network failure, wrong venv).
  * BROKEN  — the module IS installed (find_spec found it) but ``import`` raises.
    This is the numpy-2.x-ABI class ("numpy.core.umath failed to import") that a
    pip install reports as success while the import still fails — exactly the
    friend's "ran pip, still missing" signature.

The numpy-version line is logged unconditionally so a numpy 2.x regression is
visible at a glance even when every import happens to succeed.

Exit code: 0 if every checked module imports, 1 otherwise — so the launcher can
branch on it directly.
"""

from __future__ import annotations

import importlib
import importlib.util
import sys

# The rPPG injector's hard import set. Keep in lockstep with the import-check
# line in rPPG/run_rppg.bat and the modules rppg_injector.py imports at top.
CORE_MODULES = ["cv2", "numpy", "mediapipe", "scipy", "absl"]


def _module_version(mod: object) -> str:
    """Best-effort version string for a freshly imported module."""
    for attr in ("__version__", "version", "VERSION"):
        val = getattr(mod, attr, None)
        if isinstance(val, str) and val:
            return val
    return "?"


def diagnose(modules: list[str]) -> int:
    """Import each module, print a per-module verdict, return failure count."""
    failed: list[str] = []
    for name in modules:
        # find_spec distinguishes "not installed" from "installed but import
        # raises" — the two failure modes the old launcher conflated.
        try:
            spec = importlib.util.find_spec(name)
        except (ImportError, ValueError) as exc:
            # A parent package that itself fails to import surfaces here.
            print(f"[rppg-diag] BROKEN  {name:<12} {type(exc).__name__}: {exc}")
            failed.append(name)
            continue

        if spec is None:
            print(
                f"[rppg-diag] MISSING {name:<12} "
                "not installed (pip did not land it in this venv)"
            )
            failed.append(name)
            continue

        try:
            mod = importlib.import_module(name)
        except Exception as exc:  # noqa: BLE001 — any import-time error is a finding
            # Installed but import raises: the numpy-2.x-ABI class lives here
            # ("numpy.core.umath failed to import"). Report the real exception.
            print(f"[rppg-diag] BROKEN  {name:<12} {type(exc).__name__}: {exc}")
            failed.append(name)
            continue

        print(f"[rppg-diag] OK      {name:<12} ({_module_version(mod)})")

    # numpy version is the single most diagnostic fact for this app's recurring
    # fresh-install failure (numpy 2.x breaks TF 2.16.2 + OpenCV). Log it always.
    try:
        import numpy  # noqa: PLC0415 — intentional late import for the version probe

        ver = getattr(numpy, "__version__", "?")
        try:
            major = int(str(ver).split(".", 1)[0])
        except (ValueError, IndexError):
            major = -1
        warn = (
            "  <-- WARNING: numpy>=2 breaks TensorFlow 2.16.2 / OpenCV "
            "(reinstall with -c constraints.txt)"
            if major >= 2
            else ""
        )
        print(f"[rppg-diag] numpy-version: {ver}{warn}")
    except Exception as exc:  # noqa: BLE001 — numpy itself broken; already flagged above
        print(f"[rppg-diag] numpy-version: unavailable ({type(exc).__name__}: {exc})")

    if failed:
        print(
            f"[rppg-diag] RESULT: {len(failed)} module(s) not importable: "
            + ", ".join(failed)
        )
    else:
        print("[rppg-diag] RESULT: all core modules import OK")
    return 1 if failed else 0


def main(argv: list[str]) -> int:
    modules = argv[1:] or CORE_MODULES
    return diagnose(modules)


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
