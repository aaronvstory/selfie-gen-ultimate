"""Cross-platform Tk file/folder dialog wrappers with safe lifecycle handling."""

from __future__ import annotations

import sys
import tkinter as tk
from tkinter import filedialog
from typing import Any, Callable, Optional


def _prepare_root(root: tk.Tk) -> None:
    """Prepare a temporary hidden root before opening a modal dialog."""
    root.withdraw()
    root.update_idletasks()
    if sys.platform == "darwin":
        # macOS can occasionally leave dialogs unfocused or spinning unless the
        # hidden root is briefly raised/focused before opening the dialog.
        try:
            root.lift()
            root.attributes("-topmost", True)
            root.focus_force()
            root.after_idle(lambda: root.attributes("-topmost", False))
        except Exception:
            # Best-effort only; dialog still attempted.
            pass


def _run_dialog(
    dialog_fn: Callable[..., Any],
    *,
    parent: Optional[tk.Misc] = None,
    **kwargs: Any,
) -> Any:
    """Run a filedialog call with either existing parent or ephemeral root."""
    if parent is not None:
        kwargs.setdefault("parent", parent)
        return dialog_fn(**kwargs)

    temp_root = tk.Tk()
    try:
        _prepare_root(temp_root)
        kwargs.setdefault("parent", temp_root)
        return dialog_fn(**kwargs)
    finally:
        try:
            temp_root.destroy()
        except Exception:
            pass


def select_directory(*, parent: Optional[tk.Misc] = None, **kwargs: Any) -> Optional[str]:
    """Open a directory picker and normalize cancel to None."""
    result = _run_dialog(filedialog.askdirectory, parent=parent, **kwargs)
    return str(result) if result else None


def select_open_file(*, parent: Optional[tk.Misc] = None, **kwargs: Any) -> Optional[str]:
    """Open a single-file picker and normalize cancel to None."""
    result = _run_dialog(filedialog.askopenfilename, parent=parent, **kwargs)
    return str(result) if result else None


def select_open_files(*, parent: Optional[tk.Misc] = None, **kwargs: Any) -> list[str]:
    """Open a multi-file picker and normalize cancel to []."""
    result = _run_dialog(filedialog.askopenfilenames, parent=parent, **kwargs)
    if not result:
        return []
    return [str(path) for path in result]


def select_save_file(*, parent: Optional[tk.Misc] = None, **kwargs: Any) -> Optional[str]:
    """Open a save-file picker and normalize cancel to None."""
    result = _run_dialog(filedialog.asksaveasfilename, parent=parent, **kwargs)
    return str(result) if result else None
