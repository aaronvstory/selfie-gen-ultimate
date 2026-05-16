#!/usr/bin/env python3
"""Cross-platform launcher for Oldcam V15 inside the Creative Suite."""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

import oldcam

# tk_dialogs.py lives at the repo root, but this launcher runs with cwd=
# oldcam-v15/, so only that dir is on sys.path[0]. Without this bootstrap
# `from tk_dialogs import select_open_files` raises ModuleNotFoundError at
# import time (the exact trap CLAUDE.md flags for standalone subprojects;
# fixed for similarity/ in commit afe0540b).
_REPO_ROOT = str(Path(__file__).resolve().parent.parent)
if _REPO_ROOT not in sys.path:
    sys.path.insert(0, _REPO_ROOT)

try:
    from tk_dialogs import select_open_files
except ImportError:  # pragma: no cover - depends on host Python Tk support
    select_open_files = None


MEDIA_FILETYPES = [
    ("Media files", "*.mp4 *.mov *.avi *.mkv *.webm *.m4v *.jpg *.jpeg *.png *.bmp *.webp"),
    ("All files", "*.*"),
]


def choose_files() -> list[str]:
    # Use the shared tk_dialogs wrapper, never raw tkinter.filedialog: it
    # handles the fragile macOS Tk root lifecycle (ephemeral root + osascript
    # on darwin) per CLAUDE.md / AGENTS.md rule 3. parent=None → CLI-safe path.
    if select_open_files is None:
        return []
    return select_open_files(
        title="Select media files for Oldcam V15",
        filetypes=MEDIA_FILETYPES,
    )


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Launch Oldcam V15 with a file picker when no input files are provided."
    )
    parser.add_argument("inputs", nargs="*", help="Optional input files")
    parser.add_argument(
        "extra_args",
        nargs=argparse.REMAINDER,
        help="Optional oldcam arguments after --",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)

    inputs = list(args.inputs)
    extra_args = list(args.extra_args)
    if extra_args[:1] == ["--"]:
        extra_args = extra_args[1:]

    if not inputs:
        inputs = choose_files()
        if not inputs:
            print("No files selected.")
            return 0

    return oldcam.main([*inputs, *extra_args]) or 0


if __name__ == "__main__":
    raise SystemExit(main())
