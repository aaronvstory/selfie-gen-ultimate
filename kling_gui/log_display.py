"""
Log Display Widget - Scrolling text log with color-coded messages.
"""

import sys
import tkinter as tk
from tkinter import ttk
from datetime import datetime

# Local mono font (matches FONT_MONO in theme.py; this widget has its own
# isolated palette duplicate so it gets its own font constant too).
FONT_MONO = "Menlo" if sys.platform == "darwin" else "Consolas"

# Panel font sizes. Windows Consolas renders noticeably LARGER than macOS Menlo
# at the same point size, so the panel looked oversized on Windows (user
# feedback 2026-06-04). Use a smaller base on Windows; keep the macOS size the
# user said is fine. (base, milestone-bold, error-bold)
if sys.platform == "darwin":
    _FONT_SIZE, _FONT_SIZE_MILESTONE, _FONT_SIZE_ERR = 11, 12, 11
else:
    _FONT_SIZE, _FONT_SIZE_MILESTONE, _FONT_SIZE_ERR = 9, 10, 9


# Color palette matching LoopVideo dark theme
COLORS = {
    "bg_main": "#2D2D30",
    "bg_panel": "#3C3C41",
    "text_light": "#DCDCDC",
    "text_dim": "#B4B4B4",
    "accent_blue": "#6496FF",
    "success": "#64FF64",
    "error": "#FF6464",
    "warning": "#FFA500",
    # Verbose mode colors
    "upload": "#00CED1",      # Dark cyan for upload messages
    "task": "#87CEEB",        # Sky blue for task creation
    "progress": "#FFD700",    # Gold for progress/waiting
    "debug": "#808080",       # Gray for debug info
    "resize": "#DDA0DD",      # Plum for resize operations
    "download": "#98FB98",    # Pale green for downloads
    "api": "#DA70D6",         # Orchid for API calls
    # Stage-completion milestone lines (✅ RPPG DONE, ✅ OLDCAM v13 DONE,
    # ✅ ALL POSTPROCESS DONE) — bright cyan + bold so they stand out
    # against the routine info/success/warning chatter.
    "milestone": "#00FFFF",
    # ❌ RPPG FAILED-style high-priority error banners — vivid red
    # rendered bold so the user sees the failure even with the log
    # autoscrolled to the bottom mid-run.
    "error_bold": "#FF3030",
}


class LogDisplay(tk.Frame):
    """Scrolling log display with color-coded messages."""

    def __init__(self, parent, **kwargs):
        super().__init__(parent, bg=COLORS["bg_panel"], **kwargs)
        # Line index of the current in-place "progress_update" row, or None.
        self._progress_line = None

        # Create header
        header = tk.Label(
            self,
            text="PROCESSING LOG",
            font=("Segoe UI", 10, "bold"),
            bg=COLORS["bg_panel"],
            fg=COLORS["text_light"]
        )
        header.pack(fill=tk.X, padx=5, pady=(4, 1))

        # Create text widget with scrollbar
        self.text_frame = tk.Frame(self, bg=COLORS["bg_main"])
        self.text_frame.pack(fill=tk.BOTH, expand=True, padx=5, pady=4)

        self.scrollbar = ttk.Scrollbar(self.text_frame)
        self.scrollbar.pack(side=tk.RIGHT, fill=tk.Y)

        # Font sizing: user feedback PR fix/rppg-failure-visibility —
        # the original 9pt was "very hard to read." 11pt sits in the
        # comfortable Tk mono range without forcing a layout overhaul.
        self.text = tk.Text(
            self.text_frame,
            wrap=tk.WORD,
            bg=COLORS["bg_main"],
            fg=COLORS["text_light"],
            font=(FONT_MONO, _FONT_SIZE),
            state=tk.DISABLED,
            yscrollcommand=self.scrollbar.set,
            padx=4,
            pady=4,
            borderwidth=0,
            highlightthickness=0
        )
        self.text.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)
        self.scrollbar.config(command=self.text.yview)

        # Configure text tags for colors
        self.text.tag_configure("info", foreground=COLORS["accent_blue"])
        self.text.tag_configure("success", foreground=COLORS["success"])
        self.text.tag_configure("error", foreground=COLORS["error"])
        self.text.tag_configure("warning", foreground=COLORS["warning"])
        self.text.tag_configure("timestamp", foreground=COLORS["text_dim"])
        # Verbose mode tags
        self.text.tag_configure("upload", foreground=COLORS["upload"])
        self.text.tag_configure("task", foreground=COLORS["task"])
        self.text.tag_configure("progress", foreground=COLORS["progress"])
        self.text.tag_configure("debug", foreground=COLORS["debug"])
        self.text.tag_configure("resize", foreground=COLORS["resize"])
        self.text.tag_configure("download", foreground=COLORS["download"])
        self.text.tag_configure("api", foreground=COLORS["api"])
        # Stage-completion milestones — bold + slightly larger so the
        # ✅ RPPG DONE / ✅ OLDCAM v13 DONE / ✅ ALL POSTPROCESS DONE
        # lines stand out in a crowded log.
        self.text.tag_configure(
            "milestone",
            foreground=COLORS["milestone"],
            font=(FONT_MONO, _FONT_SIZE_MILESTONE, "bold"),
        )
        # ❌ RPPG FAILED-style banners — bold red so the user can spot a
        # silent-failure path even when the log autoscrolled past it.
        self.text.tag_configure(
            "error_bold",
            foreground=COLORS["error_bold"],
            font=(FONT_MONO, _FONT_SIZE_ERR, "bold"),
        )

    def log(self, message: str, level: str = "info"):
        """
        Add a log message with timestamp.

        Args:
            message: The message to log
            level: One of "info", "success", "error", "warning"
        """
        timestamp = datetime.now().strftime("[%H:%M:%S]")

        self.text.config(state=tk.NORMAL)

        # Insert timestamp
        self.text.insert(tk.END, timestamp + " ", "timestamp")

        # Insert message with level color
        self.text.insert(tk.END, message + "\n", level)

        # Auto-scroll to bottom
        self.text.see(tk.END)

        self.text.config(state=tk.DISABLED)
        # A normal log() ends any in-progress overwriting line — the next
        # update_line() starts fresh below it instead of replacing this one.
        self._progress_line = None

    def update_line(self, message: str, level: str = "progress"):
        """Show a single IN-PLACE updating line (e.g. live frame progress).

        The first call inserts a timestamped line and remembers its line index.
        Subsequent calls REPLACE that line's text instead of adding a new one —
        so '… 25% … 50% … 100%' grows on one row rather than spamming the panel.
        Any normal log() call (or clear()) ends the overwriting line so the next
        update_line() starts a fresh one.
        """
        timestamp = datetime.now().strftime("[%H:%M:%S]")
        self.text.config(state=tk.NORMAL)
        line_idx = getattr(self, "_progress_line", None)
        try:
            if line_idx is not None:
                # Overwrite the existing progress line in full.
                self.text.delete(f"{line_idx} linestart", f"{line_idx} lineend")
                self.text.insert(f"{line_idx} linestart", timestamp + " ", "timestamp")
                self.text.insert(f"{line_idx} lineend", message, level)
            else:
                # Start a fresh progress line; remember its (integer) line number
                # so a later log() that appends below doesn't shift our target.
                self.text.insert(tk.END, timestamp + " ", "timestamp")
                self.text.insert(tk.END, message + "\n", level)
                # index of the line we just wrote (END now points past the
                # trailing newline, so step back one line).
                self._progress_line = self.text.index("end-2c").split(".")[0] + ".0"
        except tk.TclError:
            # Index drifted (e.g. panel cleared mid-run) — fall back to a plain
            # append and reset the tracker.
            self._progress_line = None
            self.text.insert(tk.END, timestamp + " " + message + "\n", level)
        self.text.see(tk.END)
        self.text.config(state=tk.DISABLED)

    def clear(self):
        """Clear all log messages."""
        self.text.config(state=tk.NORMAL)
        self.text.delete(1.0, tk.END)
        self.text.config(state=tk.DISABLED)
        self._progress_line = None
