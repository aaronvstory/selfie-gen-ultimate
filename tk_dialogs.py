"""Cross-platform Tk file/folder dialog wrappers with safe lifecycle handling."""

from __future__ import annotations

import os
import sys
import tkinter as tk
from tkinter import filedialog
from typing import Any, Callable, Optional


def _debug_enabled() -> bool:
    return os.getenv("KLING_CLI_PICKER_DEBUG", "0") == "1"


def _debug(message: str) -> None:
    if _debug_enabled():
        print(f"[cli-picker] {message}", file=sys.stderr)


def _prepare_root_for_cli(root: tk.Tk) -> None:
    """Prepare an ephemeral root for non-parented CLI picker dialogs."""
    root.withdraw()
    root.update_idletasks()


def _run_dialog(
    dialog_fn: Callable[..., Any],
    *,
    parent: Optional[tk.Misc] = None,
    cli_safe: bool = False,
    **kwargs: Any,
) -> Any:
    """Run a filedialog call with either existing parent or ephemeral root."""
    if parent is not None:
        kwargs.setdefault("parent", parent)
        return dialog_fn(**kwargs)

    temp_root = tk.Tk()
    try:
        _prepare_root_for_cli(temp_root)
        kwargs.setdefault("parent", temp_root)
        try:
            return dialog_fn(**kwargs)
        except (tk.TclError, Exception) as exc:
            if cli_safe:
                _debug(f"dialog failed ({type(exc).__name__}): {exc}")
                return None
            raise
    finally:
        try:
            temp_root.destroy()
        except Exception:
            pass


def select_directory(*, parent: Optional[tk.Misc] = None, **kwargs: Any) -> Optional[str]:
    """Open a directory picker and normalize cancel to None."""
    result = _run_dialog(
        filedialog.askdirectory,
        parent=parent,
        cli_safe=parent is None,
        **kwargs,
    )
    return str(result) if result else None


def select_open_file(*, parent: Optional[tk.Misc] = None, **kwargs: Any) -> Optional[str]:
    """Open a single-file picker and normalize cancel to None."""
    result = _run_dialog(
        filedialog.askopenfilename,
        parent=parent,
        cli_safe=parent is None,
        **kwargs,
    )
    return str(result) if result else None


def select_open_files(*, parent: Optional[tk.Misc] = None, **kwargs: Any) -> list[str]:
    """Open a multi-file picker and normalize cancel to []."""
    result = _run_dialog(
        filedialog.askopenfilenames,
        parent=parent,
        cli_safe=parent is None,
        **kwargs,
    )
    if not result:
        return []
    return [str(path) for path in result]


def select_save_file(*, parent: Optional[tk.Misc] = None, **kwargs: Any) -> Optional[str]:
    """Open a save-file picker and normalize cancel to None."""
    result = _run_dialog(
        filedialog.asksaveasfilename,
        parent=parent,
        cli_safe=parent is None,
        **kwargs,
    )
    return str(result) if result else None
