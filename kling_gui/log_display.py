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
}


class LogDisplay(tk.Frame):
    """Scrolling log display with color-coded messages."""

    def __init__(self, parent, **kwargs):
        super().__init__(parent, bg=COLORS["bg_panel"], **kwargs)

        # Create header
        header = tk.Label(
            self,
            text="PROCESSING LOG",
            font=("Segoe UI", 9, "bold"),
            bg=COLORS["bg_panel"],
            fg=COLORS["text_light"]
        )
        header.pack(fill=tk.X, padx=5, pady=(4, 1))

        # Create text widget with scrollbar
        self.text_frame = tk.Frame(self, bg=COLORS["bg_main"])
        self.text_frame.pack(fill=tk.BOTH, expand=True, padx=5, pady=4)

        self.scrollbar = ttk.Scrollbar(self.text_frame)
        self.scrollbar.pack(side=tk.RIGHT, fill=tk.Y)

        self.text = tk.Text(
            self.text_frame,
            wrap=tk.WORD,
            bg=COLORS["bg_main"],
            fg=COLORS["text_light"],
            font=(FONT_MONO, 9),
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

    def clear(self):
        """Clear all log messages."""
        self.text.config(state=tk.NORMAL)
        self.text.delete(1.0, tk.END)
        self.text.config(state=tk.DISABLED)
