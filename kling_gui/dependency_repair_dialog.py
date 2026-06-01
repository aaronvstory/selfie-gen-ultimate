"""In-app dependency repair modal — zero-terminal recovery for the face stack.

Background: on a fresh install the face stack (TensorFlow / RetinaFace) can
land broken — most often numpy 2.x sneaking past the pins and breaking TF
2.16.2 at import ("numpy.core._multiarray_umath failed to import"). Until now
the GUI only PRINTED a terminal command for the user to copy-paste, which is
useless for the non-technical users this app targets ("they dunno what a
terminal even is").

This module shows a modal progress dialog and runs the SAME deterministic
repair the launcher uses (``dependency_health_check.run_repair`` +
``verify_in_fresh_process``) on a background thread, then lets the caller
retry the import — all without the user ever touching a shell.

The public entry point is :func:`run_face_stack_repair`. It is intentionally
defensive: any import/runtime failure degrades to ``False`` (caller falls back
to the manual hint) rather than crashing the GUI.
"""

from __future__ import annotations

import threading
import tkinter as tk
from tkinter import ttk
from typing import Callable, Optional


def _safe_log(log: Optional[Callable[[str, str], None]], msg: str, level: str = "info") -> None:
    """Call the GUI log callback if present; never raise."""
    try:
        if callable(log):
            log(msg, level)
    except Exception:
        pass


def run_face_stack_repair(
    parent: tk.Misc,
    log: Optional[Callable[[str, str], None]] = None,
    failures: Optional[list] = None,
) -> bool:
    """Show a modal progress dialog and repair the face dependency stack.

    Runs ``dependency_health_check.run_repair(failures)`` followed by
    ``verify_in_fresh_process()`` on a background thread so the Tk event loop
    stays responsive (the indeterminate progress bar keeps animating). Blocks
    the caller until the repair finishes (via a nested ``wait_window``), then
    returns whether the stack is healthy again.

    On success it also clears the cached RetinaFace class in
    ``kling_gui.tabs.face_crop_tab`` so a subsequent ``_load_retinaface()``
    re-imports against the freshly-repaired packages.

    Returns ``True`` if the repair + fresh-process verification both passed,
    ``False`` otherwise (including if the repair machinery can't be imported).
    """
    try:
        from dependency_health_check import run_repair, verify_in_fresh_process
    except Exception as exc:  # pragma: no cover - degraded tree
        _safe_log(log, f"Face Crop: in-app repair unavailable ({type(exc).__name__}: {exc})", "error")
        return False

    _safe_log(log, "Face Crop: starting automatic dependency repair (no terminal needed)…", "info")

    # --- Build the modal --------------------------------------------------
    try:
        top = tk.Toplevel(parent)
    except Exception:
        # No usable Tk parent (headless / teardown) — repair without a UI.
        return _repair_headless(run_repair, verify_in_fresh_process, log, failures)

    top.title("Repairing image dependencies")
    top.transient(parent.winfo_toplevel() if hasattr(parent, "winfo_toplevel") else parent)
    top.resizable(False, False)
    # Theme is optional — fall back to plain Tk defaults if it can't import.
    try:
        from .theme import COLORS, FONT_FAMILY

        bg = COLORS.get("bg_panel", "#2b2b2b")
        fg = COLORS.get("text_primary", "#e8e8e8")
        font_family = FONT_FAMILY
    except Exception:
        bg, fg, font_family = "#2b2b2b", "#e8e8e8", "TkDefaultFont"

    top.configure(bg=bg)
    frame = tk.Frame(top, bg=bg, padx=24, pady=20)
    frame.pack(fill="both", expand=True)

    tk.Label(
        frame,
        text="Repairing image-processing dependencies",
        bg=bg,
        fg=fg,
        font=(font_family, 12, "bold"),
        anchor="w",
        justify="left",
    ).pack(fill="x")

    tk.Label(
        frame,
        text=(
            "The face-detection libraries need a one-time fix.\n"
            "This runs automatically and may take 2–5 minutes.\n"
            "You don't need to type anything — just wait."
        ),
        bg=bg,
        fg=fg,
        font=(font_family, 10),
        anchor="w",
        justify="left",
    ).pack(fill="x", pady=(8, 14))

    bar = ttk.Progressbar(frame, mode="indeterminate", length=360)
    bar.pack(fill="x")
    try:
        bar.start(12)
    except Exception:
        pass

    status_var = tk.StringVar(value="Reinstalling packages…")
    tk.Label(
        frame,
        textvariable=status_var,
        bg=bg,
        fg=fg,
        font=(font_family, 9),
        anchor="w",
        justify="left",
    ).pack(fill="x", pady=(10, 0))

    # Disable the window close button while repairing — closing mid-pip would
    # leave a half-installed venv (the exact failure mode we're fixing).
    top.protocol("WM_DELETE_WINDOW", lambda: None)

    result = {"ok": False, "done": False}

    # ── Thread-safety (gemini HIGH, PR #65) ──────────────────────────────
    # `log` updates the GUI LogDisplay text widget and `status_var.set` touches
    # a Tk var — BOTH are unsafe to call from the worker thread. Every
    # UI-touching call from `_worker` is marshalled onto the Tk main loop via
    # `_post`, which wraps `top.after` in try/except so a destroyed window
    # (parent closed mid-repair) can't raise out of the worker. And the worker's
    # `finally` calls `_unblock()` DIRECTLY (not via after) as a guaranteed
    # last resort — if `top.after` is dead, the main thread's fallback spin-loop
    # still sees `result["done"]` flip and exits rather than hanging forever.
    def _post(fn) -> None:
        try:
            top.after(0, fn)
        except Exception:
            pass

    def _worker() -> None:
        ok = False
        try:
            repaired, message = run_repair(failures=failures)
            _post(lambda: _safe_log(log, f"Face Crop: repair step — {message}", "info" if repaired else "error"))
            _post(lambda: status_var.set("Verifying the fix…"))
            verified, vfailures = verify_in_fresh_process()
            ok = bool(repaired and verified)
            if not verified and vfailures:
                _post(lambda: _safe_log(log, "Face Crop: verification still reports: " + "; ".join(vfailures), "error"))
        except Exception as exc:  # pragma: no cover - defensive
            _post(lambda: _safe_log(log, f"Face Crop: repair crashed ({type(exc).__name__}: {exc})", "error"))
            ok = False
        finally:
            result["ok"] = ok
            # Try to run _finish on the main thread; if scheduling fails (window
            # already gone), flip `done` directly so wait_window/the spin-loop
            # fallback unblocks and the GUI never hangs.
            try:
                top.after(0, _finish)
            except Exception:
                result["done"] = True

    def _finish() -> None:
        # Runs on the Tk main thread. Set `done` FIRST so even if a later
        # widget call raises, the caller is already unblocked.
        result["done"] = True
        try:
            bar.stop()
        except Exception:
            pass
        if result["ok"]:
            _reset_retinaface_cache()
            _safe_log(log, "Face Crop: dependencies repaired successfully — retrying…", "success")
        else:
            _safe_log(log, "Face Crop: automatic repair did not fully succeed.", "error")
        try:
            top.grab_release()
        except Exception:
            pass
        try:
            top.destroy()
        except Exception:
            pass

    threading.Thread(target=_worker, daemon=True).start()

    # Modal: grab focus + block the caller until _finish destroys the window.
    try:
        top.grab_set()
    except Exception:
        pass
    try:
        parent.wait_window(top)
    except Exception:
        # If wait_window can't run, pump the event loop until the worker flags
        # done. `time.sleep(0.05)` per iteration caps this at ~20 Hz instead of
        # busy-spinning a CPU core (gemini MEDIUM, PR #65).
        import time

        while not result["done"]:
            try:
                parent.update()
            except Exception:
                break
            time.sleep(0.05)

    return bool(result["ok"])


def _repair_headless(run_repair, verify_in_fresh_process, log, failures) -> bool:
    """Run the repair with no UI (no usable Tk parent). Used as a fallback."""
    try:
        repaired, message = run_repair(failures=failures)
        _safe_log(log, f"Face Crop: repair step — {message}", "info" if repaired else "error")
        verified, _ = verify_in_fresh_process()
        ok = bool(repaired and verified)
        if ok:
            _reset_retinaface_cache()
        return ok
    except Exception as exc:  # pragma: no cover
        _safe_log(log, f"Face Crop: headless repair crashed ({type(exc).__name__}: {exc})", "error")
        return False


def _reset_retinaface_cache() -> None:
    """Clear the cached RetinaFace class so the next load re-imports cleanly."""
    try:
        from .tabs import face_crop_tab

        face_crop_tab._RETINAFACE_CLASS = None
        face_crop_tab._RETINAFACE_ERROR = ""
    except Exception:
        pass
