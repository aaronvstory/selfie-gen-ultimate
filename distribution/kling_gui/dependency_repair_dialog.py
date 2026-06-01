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

    # ── Thread-safety: queue + main-thread poller (GPT review, PR #65) ───
    # The worker thread does ONLY blocking work (run_repair, verify) and
    # communicates back by putting plain string/bool events on a Queue. It
    # NEVER touches Tk — no `top.after`, no widget access (calling top.after
    # from a worker thread is itself unsafe on some Tk builds). A poller that
    # runs ON THE MAIN THREAD (scheduled via top.after BEFORE the worker
    # starts) drains the queue and does all UI mutation. Exception messages
    # are formatted to strings INSIDE the worker before enqueueing, so we
    # never close over an `exc` variable (Python clears it after the except
    # block, which would lose the real error in a deferred callback).
    import queue as _queue

    events: "_queue.Queue" = _queue.Queue()

    def _worker() -> None:
        try:
            repaired, message = run_repair(failures=failures)
            events.put(("log", f"Face Crop: repair step — {message}", "info" if repaired else "error"))
            events.put(("status", "Verifying the fix…"))
            verified, vfailures = verify_in_fresh_process()
            if not verified and vfailures:
                events.put(("log", "Face Crop: verification still reports: " + "; ".join(vfailures), "error"))
            events.put(("done", bool(repaired and verified)))
        except Exception as exc:  # pragma: no cover - defensive
            events.put(("log", f"Face Crop: repair crashed ({type(exc).__name__}: {exc})", "error"))
            events.put(("done", False))

    def _finish(ok: bool) -> None:
        # Runs on the Tk main thread. Set `done` FIRST so even if a later
        # widget call raises, the caller is already unblocked.
        result["ok"] = ok
        result["done"] = True
        try:
            bar.stop()
        except Exception:
            pass
        if ok:
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

    def _poll() -> None:
        # Main-thread queue drain. Reschedules itself every 100ms until the
        # worker emits a ("done", ok) event, then finishes once.
        drained_done = None
        try:
            while True:
                evt = events.get_nowait()
                kind = evt[0]
                if kind == "log":
                    _safe_log(log, evt[1], evt[2])
                elif kind == "status":
                    try:
                        status_var.set(evt[1])
                    except Exception:
                        pass
                elif kind == "done":
                    drained_done = evt[1]
        except _queue.Empty:
            pass
        if drained_done is not None:
            _finish(bool(drained_done))
            return
        if not result["done"]:
            try:
                top.after(100, _poll)
            except Exception:
                # Window gone before done — unblock the caller.
                result["done"] = True

    # Start the main-thread poller BEFORE the worker so every Tk call happens
    # on the main thread; the worker only ever touches the queue.
    try:
        top.after(0, _poll)
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
        # If wait_window can't run, pump the event loop until done. 50ms sleep
        # caps this at ~20 Hz instead of busy-spinning a core. The poller above
        # still drains the queue + calls _finish on these update() ticks.
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
