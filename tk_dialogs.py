"""Cross-platform Tk file/folder dialog wrappers with safe lifecycle handling."""

from __future__ import annotations

import logging
import os
import subprocess
import sys
import time
import traceback
import tkinter as tk
from tkinter import filedialog
from typing import Any, Callable, Optional

logger = logging.getLogger("kling_picker")


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
    dialog_name = getattr(dialog_fn, "__name__", repr(dialog_fn))
    started = time.perf_counter()
    logger.info(
        "picker_start dialog=%s backend=%s platform=%s parented=%s cli_safe=%s",
        dialog_name,
        "tk",
        sys.platform,
        parent is not None,
        cli_safe,
    )

    def _elapsed_ms() -> int:
        return int((time.perf_counter() - started) * 1000)

    def _log_end(status: str) -> None:
        logger.info("picker_end dialog=%s backend=%s status=%s elapsed_ms=%d", dialog_name, "tk", status, _elapsed_ms())

    if parent is not None:
        try:
            kwargs.setdefault("parent", parent)
            result = dialog_fn(**kwargs)
            _log_end("cancel" if not result else "success")
            return result
        except tk.TclError:
            logger.error(
                "picker_error dialog=%s backend=%s error_type=TclError elapsed_ms=%d traceback=%s",
                dialog_name,
                "tk",
                _elapsed_ms(),
                traceback.format_exc(),
            )
            raise
        except Exception:
            logger.error(
                "picker_error dialog=%s backend=%s error_type=Exception elapsed_ms=%d traceback=%s",
                dialog_name,
                "tk",
                _elapsed_ms(),
                traceback.format_exc(),
            )
            raise

    try:
        temp_root = tk.Tk()
    except tk.TclError as exc:
        logger.error(
            "picker_error dialog=%s backend=%s error_type=TclError phase=create_root elapsed_ms=%d traceback=%s",
            dialog_name,
            "tk",
            _elapsed_ms(),
            traceback.format_exc(),
        )
        if cli_safe:
            _debug(f"dialog root creation failed ({type(exc).__name__}): {exc}")
            _log_end("fallback_none")
            return None
        raise
    try:
        _prepare_root_for_cli(temp_root)
        kwargs.setdefault("parent", temp_root)
        try:
            result = dialog_fn(**kwargs)
            _log_end("cancel" if not result else "success")
            return result
        except tk.TclError as exc:
            logger.error(
                "picker_error dialog=%s backend=%s error_type=TclError phase=dialog elapsed_ms=%d traceback=%s",
                dialog_name,
                "tk",
                _elapsed_ms(),
                traceback.format_exc(),
            )
            if cli_safe:
                _debug(f"dialog failed ({type(exc).__name__}): {exc}")
                _log_end("fallback_none")
                return None
            raise
        except Exception as exc:
            logger.error(
                "picker_error dialog=%s backend=%s error_type=Exception phase=dialog elapsed_ms=%d traceback=%s",
                dialog_name,
                "tk",
                _elapsed_ms(),
                traceback.format_exc(),
            )
            if cli_safe:
                _debug(f"dialog failed ({type(exc).__name__}): {exc}")
                _log_end("fallback_none")
                return None
            raise
    finally:
        try:
            temp_root.destroy()
        except tk.TclError as exc:
            _debug(f"root destroy failed ({type(exc).__name__}): {exc}")
            logger.error(
                "picker_error dialog=%s backend=%s error_type=TclError phase=destroy_root traceback=%s",
                dialog_name,
                "tk",
                traceback.format_exc(),
            )


def select_directory_cli_safe(*, title: str = "Select Folder") -> Optional[str]:
    """Select a directory for CLI flows with a native macOS backend when available."""
    if sys.platform != "darwin":
        return select_directory(title=title)

    started = time.perf_counter()
    logger.info(
        "picker_start dialog=%s backend=%s platform=%s parented=%s cli_safe=%s",
        "choose_folder",
        "osascript",
        sys.platform,
        False,
        True,
    )
    try:
        result = subprocess.run(
            [
                "osascript",
                "-e",
                f'POSIX path of (choose folder with prompt "{title.replace(chr(34), chr(39))}")',
            ],
            capture_output=True,
            text=True,
            timeout=120,
            check=False,
        )
    except Exception:
        logger.error(
            "picker_error dialog=%s backend=%s error_type=Exception phase=invoke elapsed_ms=%d traceback=%s",
            "choose_folder",
            "osascript",
            int((time.perf_counter() - started) * 1000),
            traceback.format_exc(),
        )
        logger.info(
            "picker_end dialog=%s backend=%s status=%s elapsed_ms=%d",
            "choose_folder",
            "osascript",
            "fallback_none",
            int((time.perf_counter() - started) * 1000),
        )
        return None

    elapsed_ms = int((time.perf_counter() - started) * 1000)
    stdout = (result.stdout or "").strip()
    stderr = (result.stderr or "").strip()
    if result.returncode == 0 and stdout:
        logger.info(
            "picker_end dialog=%s backend=%s status=%s elapsed_ms=%d",
            "choose_folder",
            "osascript",
            "success",
            elapsed_ms,
        )
        return stdout

    is_cancel = "User canceled" in stderr or "cancel" in stderr.lower()
    logger.info(
        "picker_end dialog=%s backend=%s status=%s elapsed_ms=%d",
        "choose_folder",
        "osascript",
        "cancel" if is_cancel else "fallback_none",
        elapsed_ms,
    )
    if not is_cancel:
        logger.error(
            "picker_error dialog=%s backend=%s error_type=ProcessError phase=returncode rc=%d stderr=%s",
            "choose_folder",
            "osascript",
            result.returncode,
            stderr,
        )
    return None


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
