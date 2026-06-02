#!/usr/bin/env python3
"""One-shot, idempotent patcher: escape unescaped parens inside cmd ``(...)``
blocks across the v2.17 launcher fleet.

Background: several Windows ``.bat`` launchers had ``echo`` lines INSIDE an
``if (...)`` block whose text contained a literal ``(`` / ``)`` (e.g.
``echo ... ran gpu_bootstrap (CuPy) before ...``, ``(3.9-3.12)``,
``(matplotlib/opencv-contrib/sounddevice)``). cmd.exe reads the ``)`` as the
block's closing paren, then tries to run the trailing words as a command and
dies with ``<word> was unexpected at this time.`` (exit 255). For rPPG this
meant ``run_rppg.bat`` crashed before launching the injector -> ``-NORPPG`` on
every run.

This script escapes those parens (``^(`` / ``^)``) in place. It is byte-level
and idempotent: lines already escaped are left untouched, line endings are
preserved (no CRLF<->LF flip), and re-running it is a no-op. Run it from inside
an install dir; it patches the launchers relative to the repo root.

Usage (normally invoked by patch_launcher_parens.bat, but safe to run directly):
    python scripts/patch_launcher_parens.py [REPO_ROOT]
"""

from __future__ import annotations

import sys
from pathlib import Path

_PYORG = "https://www.python.org/downloads/release/python-3119/"

# Each entry: (UNescaped substring, escaped replacement). Applied only when the
# unescaped form is present AND the escaped form's de-escape == the unescaped
# form (so we never double-escape). Byte-exact, CRLF-safe.
_MEDIAPIPE = [
    (
        "MediaPipe runtime deps (matplotlib/opencv-contrib/sounddevice) install failed.",
        "MediaPipe runtime deps ^(matplotlib/opencv-contrib/sounddevice^) install failed.",
    ),
]
_PYRANGE = [
    (
        f"No supported Python (3.9-3.12) found. Install python3.11 ({_PYORG}) and retry.",
        f"No supported Python ^(3.9-3.12^) found. Install python3.11 ^({_PYORG}^) and retry.",
    ),
    ("No supported Python (3.9-3.12) found.", "No supported Python ^(3.9-3.12^) found."),
    (
        "outside supported range 3.9-3.12 (resolver bug; please file an issue).",
        "outside supported range 3.9-3.12 ^(resolver bug; please file an issue^).",
    ),
]
_RPPG = [
    (
        ">>\"%LOG_FILE%\" echo [INFO] ran gpu_bootstrap (CuPy) before rPPG injector",
        ">>\"%LOG_FILE%\" echo [INFO] ran gpu_bootstrap ^(CuPy^) before rPPG injector",
    ),
    (
        ">>\"%LOG_FILE%\" echo [INFO] rPPG import diagnostic (%~1):",
        ">>\"%LOG_FILE%\" echo [INFO] rPPG import diagnostic ^(%~1^):",
    ),
]
_BUILD = [
    ("kling_config.json (next to the exe)", "kling_config.json ^(next to the exe^)"),
    ("Windows 10 or later (64-bit)", "Windows 10 or later ^(64-bit^)"),
]

# relative path -> list of (old, new)
FIX_MAP = {
    "rPPG/run_rppg.bat": _RPPG,
    "build_gui_exe.bat": _BUILD,
    "similarity/run_gui.bat": _PYRANGE,
}
for _v in (9, 10, 11):
    FIX_MAP[f"oldcam-v{_v}/oldcam_launcher.bat"] = _MEDIAPIPE + _PYRANGE
for _v in (14, 15, 24):
    FIX_MAP[f"oldcam-v{_v}/oldcam_launcher.bat"] = _PYRANGE


def _deescape(s: str) -> str:
    return s.replace("^(", "(").replace("^)", ")")


def patch_file(path: Path, subs) -> tuple[int, list[str]]:
    """Return (num_substitutions_applied, notes). Byte-level, CRLF-preserving."""
    if not path.exists():
        return 0, [f"  - skip (not found): {path.name}"]
    data = path.read_bytes()
    cr = data.count(b"\r\n")
    lone_lf = data.count(b"\n") - cr
    applied = 0
    notes = []
    for old, new in subs:
        ob = old.encode("utf-8")
        nb = new.encode("utf-8")
        # idempotency + safety: only swap when the unescaped form is present and
        # the replacement is genuinely the escaped variant of it.
        if ob in data and _deescape(new) == old:
            data = data.replace(ob, nb)
            applied += 1
    if applied:
        new_cr = data.count(b"\r\n")
        new_lone = data.count(b"\n") - new_cr
        if new_lone != lone_lf:
            return 0, [f"  ! ABORTED {path.name}: line-ending change detected; not written"]
        path.write_bytes(data)
        notes.append(f"  + patched {path.name} ({applied} line(s))")
    else:
        notes.append(f"  = already clean: {path.name}")
    return applied, notes


def main(argv=None) -> int:
    argv = sys.argv[1:] if argv is None else argv
    if argv:
        root = Path(argv[0]).resolve()
    else:
        # default: repo root = parent of this script's dir (scripts/..)
        root = Path(__file__).resolve().parents[1]
    print(f"Patching launcher parens under: {root}")
    total = 0
    for rel, subs in sorted(FIX_MAP.items()):
        n, notes = patch_file(root / rel, subs)
        total += n
        for line in notes:
            print(line)
    if total:
        print(f"\nDone. Applied {total} fix(es). rPPG + launchers will no longer crash on the "
              f"'<word> was unexpected at this time.' parse error.")
    else:
        print("\nNothing to patch — all launchers already escaped (safe to re-run).")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
