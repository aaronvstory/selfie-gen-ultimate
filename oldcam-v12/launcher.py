#!/usr/bin/env python3
"""Cross-platform launcher for Oldcam V12 inside the Creative Suite."""

from __future__ import annotations

import argparse

import oldcam

try:
    import tkinter as tk
    from tkinter import filedialog
except ImportError:  # pragma: no cover - depends on host Python Tk support
    tk = None
    filedialog = None


MEDIA_FILETYPES = [
    ("Media files", "*.mp4 *.mov *.avi *.mkv *.webm *.m4v *.jpg *.jpeg *.png *.bmp *.webp"),
    ("All files", "*.*"),
]


def choose_files() -> list[str]:
    if tk is None or filedialog is None:
        return []
    root = tk.Tk()
    root.withdraw()
    root.attributes("-topmost", True)
    try:
        selected = filedialog.askopenfilenames(
            title="Select media files for Oldcam V12",
            filetypes=MEDIA_FILETYPES,
        )
        return list(selected)
    finally:
        root.destroy()


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Launch Oldcam V12 with a file picker when no input files are provided."
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
