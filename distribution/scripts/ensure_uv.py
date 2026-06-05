#!/usr/bin/env python3
"""Ensure the ``uv`` package manager is available — bootstrapping it if absent.

The end user is NON-TECHNICAL and never opens a terminal: they double-click a
launcher. So when uv isn't installed we must install it for them, silently, the
same way ``scripts/win_resolve_python.bat`` auto-installs Python. This module is
the uv analogue of that resolver.

Strategy (per OS), all best-effort and idempotent:

  * Already on PATH or in a standard install dir -> done (print the path).
  * Windows -> the official standalone installer via PowerShell:
        powershell -ExecutionPolicy Bypass -c "irm https://astral.sh/uv/install.ps1 | iex"
    (drops uv in %USERPROFILE%\\.local\\bin). Falls back to ``winget install
    --id=astral-sh.uv`` if PowerShell/irm is blocked.
  * macOS / Linux -> the official install script:
        curl -LsSf https://astral.sh/uv/install.sh | sh
    (drops uv in ~/.local/bin). Falls back to ``brew install uv`` on macOS.

On success prints the resolved uv path to stdout (last line) and exits 0. On
failure prints a diagnostic to stderr and exits 1 — the CALLER decides whether
to fall back to the legacy pip path (the launchers do: a uv-bootstrap failure
falls back to pip, never bricks the launch).

CLI:
    python scripts/ensure_uv.py [--print-path] [--quiet]
"""

from __future__ import annotations

import argparse
import os
import shutil
import subprocess
import sys
from pathlib import Path


def _candidate_uv_paths() -> list[Path]:
    home = Path.home()
    userprofile = Path(os.environ.get("USERPROFILE", str(home)))
    if sys.platform == "win32":
        return [
            home / ".local" / "bin" / "uv.exe",
            userprofile / ".local" / "bin" / "uv.exe",
            home / ".cargo" / "bin" / "uv.exe",
        ]
    return [
        home / ".local" / "bin" / "uv",
        Path("/opt/homebrew/bin/uv"),
        Path("/usr/local/bin/uv"),
        home / ".cargo" / "bin" / "uv",
    ]


def find_uv() -> str | None:
    """Return a usable uv path (PATH first, then standard install dirs)."""
    found = shutil.which("uv")
    if found:
        return found
    for c in _candidate_uv_paths():
        if c.exists():
            return str(c)
    return None


def _log(msg: str, *, quiet: bool) -> None:
    if not quiet:
        print(f"  ensure-uv: {msg}", file=sys.stderr, flush=True)


def _run(cmd: list[str], *, timeout: int = 600) -> bool:
    # Route the installer's child stdout to STDERR. main() uses stdout as the
    # `--print-path` transport (a launcher captures exactly one path line), and
    # on a cold machine powershell/winget/curl|sh/brew print banners + progress
    # to stdout that would otherwise corrupt that capture (CodeRabbit Major).
    # Installer chatter is diagnostic, so stderr is the right sink anyway.
    # Choose a SAFE stdout sink for the installer's chatter that is NOT this
    # process's stdout (the `--print-path` transport). Passing sys.stderr
    # directly raises io.UnsupportedOperation when sys.stderr has no real fileno
    # (frozen exe / pythonw GUI), which would DISABLE the uv bootstrap entirely
    # rather than just route output (gemini HIGH, PR #73). Use the real stderr
    # fd when it exists; otherwise discard to DEVNULL so the install STILL RUNS.
    out_sink = subprocess.DEVNULL
    try:
        fd = sys.stderr.fileno()
        if fd is not None and fd >= 0:
            out_sink = sys.stderr
    except Exception:  # noqa: BLE001 — no usable stderr fd; DEVNULL it is
        out_sink = subprocess.DEVNULL
    try:
        proc = subprocess.run(cmd, timeout=timeout, stdout=out_sink)
        return proc.returncode == 0
    except (OSError, subprocess.SubprocessError):
        # A genuine launch/exec failure means uv wasn't installed by this path.
        return False


def _install_windows(quiet: bool) -> bool:
    # 1) Official standalone installer (PowerShell irm | iex).
    _log("installing uv via the official PowerShell installer...", quiet=quiet)
    ps = (
        "$ErrorActionPreference='Stop';"
        "irm https://astral.sh/uv/install.ps1 | iex"
    )
    if _run(["powershell", "-NoProfile", "-ExecutionPolicy", "Bypass", "-Command", ps]):
        if find_uv():
            return True
    # 2) winget fallback.
    _log("PowerShell installer didn't yield uv; trying winget...", quiet=quiet)
    if _run([
        "winget", "install", "--id=astral-sh.uv", "-e",
        "--accept-source-agreements", "--accept-package-agreements",
        "--silent",
    ]):
        if find_uv():
            return True
    return False


def _install_unix(quiet: bool) -> bool:
    # 1) Official install script (curl | sh).
    _log("installing uv via the official install script...", quiet=quiet)
    sh = "curl -LsSf https://astral.sh/uv/install.sh | sh"
    if _run(["sh", "-c", sh]):
        if find_uv():
            return True
    # 2) Homebrew fallback on macOS.
    if sys.platform == "darwin" and shutil.which("brew"):
        _log("install script didn't yield uv; trying brew...", quiet=quiet)
        if _run(["brew", "install", "uv"]):
            if find_uv():
                return True
    return False


def ensure_uv(*, quiet: bool = False) -> str | None:
    """Return a usable uv path, installing uv if necessary. None on failure."""
    existing = find_uv()
    if existing:
        return existing
    ok = _install_windows(quiet) if sys.platform == "win32" else _install_unix(quiet)
    return find_uv() if ok else None


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Ensure uv is installed.")
    parser.add_argument(
        "--print-path",
        action="store_true",
        help="print ONLY the resolved uv path to stdout (for launcher capture)",
    )
    parser.add_argument("--quiet", action="store_true")
    args = parser.parse_args(argv)

    path = ensure_uv(quiet=args.quiet)
    if path is None:
        _log(
            "could not find or install uv; the launcher will fall back to the "
            "legacy pip path.",
            quiet=False,
        )
        return 1
    if args.print_path:
        print(path)
    else:
        _log(f"uv ready at {path}", quiet=args.quiet)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
