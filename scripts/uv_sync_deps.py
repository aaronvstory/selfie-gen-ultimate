#!/usr/bin/env python3
"""Canonical uv dependency sync — the ONE routine every launcher calls (v2.20).

This is the uv-native realization of the v2.17 thesis: "one canonical
per-platform installer all launchers call, so nothing is ever a divergent
subset." Under uv that whole job collapses to:

    1. ensure uv is installed         (scripts/ensure_uv.py)
    2. pick the GPU/OS-appropriate torch extra   (scripts/uv_torch_select.py)
    3. uv sync --extra <that>         (resolves the WHOLE locked set in one shot)
    4. probe + CPU-fallback if a CUDA build is runtime-broken   (uv_torch_select)

Because uv.lock pins the complete set (numpy<2, TF 2.16.2, mediapipe + its full
runtime deps, scipy, absl, opencv<4.12, ...), a single `uv sync` produces a
COMPLETE env — there is no mediapipe `--no-deps` gap, no per-launcher subset, no
constraints.txt threading. The lock IS the constraint.

The GUI / CLI / batch launchers and the shared sub-launcher preflight all call
this. Sub-launchers therefore never install their own subset — they run against
the same project env (the "no sub-launcher mini-install" invariant, preserved).

Exit codes:
    0 — env is synced + ready (CUDA verified or cleanly fell back to CPU)
    3 — uv unavailable / sync failed AND the caller should FALL BACK to the
        legacy pip path. (Distinct from 1 so a launcher can tell "use pip
        instead" from a generic crash.)

ALWAYS prefer returning 0 with a working env. Code 3 only when uv genuinely
can't produce one, so the launcher's pip fallback can take over — we never
brick a launch.

CLI:
    python scripts/uv_sync_deps.py [--project DIR] [--quiet]
"""

from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT / "scripts"))

FALLBACK_TO_PIP = 3


def _log(msg: str, *, quiet: bool) -> None:
    if not quiet:
        print(f"  uv-sync: {msg}", flush=True)


def venv_dir_for_python(python_exe: str | None) -> Path | None:
    """Derive the venv ROOT dir from an interpreter path, so uv targets the
    SAME env the CALLER resolved (not always the canonical default).

    Windows layout: <venv>/Scripts/python.exe  -> parent.parent
    POSIX  layout : <venv>/bin/python           -> parent.parent

    Returns None if the path is empty / doesn't look like a venv interpreter.
    Code-review (CodeRabbit Major ×3): the callers (preflight, run_*.sh) pass a
    specific interpreter (`venv` / `.venv311` / `.venv` / `.venv-macos`), but the
    orchestrator used to hard-default UV_PROJECT_ENVIRONMENT to the canonical
    dir -- so uv could provision the canonical ``venv`` while the launcher then
    ran the stale ``.venv311`` interpreter (false UV_SYNCED, skipped repair).
    Deriving the env
    from the caller's python keeps both sides on ONE env.
    """
    if not python_exe:
        return None
    p = Path(python_exe)
    # <venv>/Scripts/python.exe  or  <venv>/bin/python
    if p.parent.name in ("Scripts", "bin"):
        return p.parent.parent
    return None


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Canonical uv dependency sync.")
    parser.add_argument("--project", default=str(REPO_ROOT))
    parser.add_argument(
        "--python",
        default=None,
        help="caller's resolved interpreter; uv targets ITS venv dir so both "
        "sides stay on one env (falls back to the canonical venv if omitted)",
    )
    parser.add_argument("--quiet", action="store_true")
    args = parser.parse_args(argv)
    quiet = args.quiet
    project = Path(args.project)

    # Point uv at the venv the CALLER resolved (derived from --python), so the
    # GUI launches the SAME interpreter uv provisioned — no dual-venv, no false
    # "ready" on a different env (CodeRabbit Major ×3). Precedence:
    #   1. an explicit UV_PROJECT_ENVIRONMENT (tests / power users) wins;
    #   2. else the caller's --python venv dir;
    #   3. else the canonical default (Windows venv\ , macOS .venv-macos\).
    if "UV_PROJECT_ENVIRONMENT" not in os.environ:
        caller_env = venv_dir_for_python(args.python)
        if caller_env is not None:
            os.environ["UV_PROJECT_ENVIRONMENT"] = str(caller_env)
        else:
            canonical = "venv" if sys.platform == "win32" else ".venv-macos"
            os.environ["UV_PROJECT_ENVIRONMENT"] = str(project / canonical)

    # uv's default HTTP timeout is 30s — far too short for the multi-GB CUDA /
    # torch wheels this project pulls (nvidia-cusolver, torch+cuXXX, etc.). On a
    # normal home connection extracting a 2GB wheel easily exceeds 30s, and uv
    # aborts the WHOLE sync with "network timeout" (the failure the fresh-sync
    # tests caught). The legacy pip path used a 40-min install timeout for the
    # same reason. Raise it generously (15 min/connection) unless the user set
    # their own. This is the uv analogue of PIP_INSTALL_TIMEOUT_SECONDS.
    os.environ.setdefault("UV_HTTP_TIMEOUT", "900")

    lock = project / "uv.lock"
    if not lock.is_file():
        _log("no uv.lock in project; falling back to pip path", quiet=quiet)
        return FALLBACK_TO_PIP

    # 1) ensure uv. A bootstrap failure -> pip fallback (never block launch).
    try:
        import ensure_uv
    except Exception as exc:  # pragma: no cover
        _log(f"ensure_uv import failed ({exc!r}); pip fallback", quiet=quiet)
        return FALLBACK_TO_PIP
    uv = ensure_uv.ensure_uv(quiet=quiet)
    if uv is None:
        _log("uv unavailable and could not be installed; pip fallback", quiet=quiet)
        return FALLBACK_TO_PIP

    # 2)+3)+4) GPU-aware select + sync + CUDA probe/fallback. uv_torch_select
    # does the full sequence and always returns 0 (it degrades to CPU on any
    # CUDA problem). We treat ITS failure-to-run as a pip fallback only if the
    # sync produced no env at all.
    try:
        import uv_torch_select
    except Exception as exc:  # pragma: no cover
        _log(f"uv_torch_select import failed ({exc!r}); pip fallback", quiet=quiet)
        return FALLBACK_TO_PIP

    # Wrap the selector — it's the last external step before launch. A crash
    # there (selector bug, transient subprocess/OS error) must degrade to the
    # pip fallback, NOT bubble out and break the launch (CodeRabbit Major).
    try:
        rc = uv_torch_select.main(
            (["--quiet"] if quiet else []) + ["--project", str(project)]
        )
    except Exception as exc:
        _log(f"uv torch selector crashed ({exc!r}); pip fallback", quiet=quiet)
        return FALLBACK_TO_PIP
    # Verify an env actually materialized. UV_PROJECT_ENVIRONMENT is always set
    # above, so the `else` is only reached in a standalone/test call that didn't
    # pre-set it — use the SAME canonical default as uv_torch_select._synced_python
    # (venv\ / .venv-macos), NOT a bare .venv, or this gate would probe a
    # different dir than the sync targeted and falsely pip-fall-back on Windows
    # (round-2 review HIGH #1).
    env_dir = os.environ.get("UV_PROJECT_ENVIRONMENT")
    if env_dir:
        base = Path(env_dir)
    else:
        base = project / ("venv" if sys.platform == "win32" else ".venv-macos")
    py = (
        base / "Scripts" / "python.exe"
        if sys.platform == "win32"
        else base / "bin" / "python"
    )
    if rc != 0 or not py.exists():
        _log("uv sync did not produce a usable env; pip fallback", quiet=quiet)
        return FALLBACK_TO_PIP

    _log("dependencies synced via uv (env ready)", quiet=quiet)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
