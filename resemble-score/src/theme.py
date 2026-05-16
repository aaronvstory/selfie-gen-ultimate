"""Shared dark-mode color constants for the ttk GUI.

Plain ttk (no customtkinter) to keep the dependency surface minimal — this is
a small utility, not the main app.
"""

from __future__ import annotations

BG = "#1e1f22"
BG_PANEL = "#26282c"
FG = "#e6e6e6"
FG_MUTED = "#9aa0a6"
ACCENT = "#4f8cff"
WINNER_BG = "#1f4d2a"
WINNER_FG = "#7ee787"
ERROR_FG = "#ff7b72"
ROW_ALT = "#23252a"

FONT_FAMILY = "Segoe UI"
FONT = (FONT_FAMILY, 10)
FONT_BOLD = (FONT_FAMILY, 10, "bold")
FONT_TITLE = (FONT_FAMILY, 14, "bold")


def apply_dark_ttk(root) -> None:
    """Apply a dark ttk theme in-place. Best-effort; never raises."""
    try:
        from tkinter import ttk

        style = ttk.Style(root)
        try:
            style.theme_use("clam")
        except Exception:
            pass
        root.configure(bg=BG)
        style.configure(".", background=BG, foreground=FG, font=FONT)
        style.configure("TFrame", background=BG)
        style.configure("Panel.TFrame", background=BG_PANEL)
        style.configure("TLabel", background=BG, foreground=FG)
        style.configure(
            "Title.TLabel", background=BG, foreground=FG, font=FONT_TITLE
        )
        style.configure(
            "Muted.TLabel", background=BG, foreground=FG_MUTED
        )
        style.configure("TButton", background=BG_PANEL, foreground=FG)
        style.map(
            "TButton",
            background=[("active", ACCENT)],
            foreground=[("active", "#ffffff")],
        )
        style.configure("TCheckbutton", background=BG, foreground=FG)
        style.configure(
            "Treeview",
            background=BG_PANEL,
            fieldbackground=BG_PANEL,
            foreground=FG,
            rowheight=24,
        )
        style.configure(
            "Treeview.Heading",
            background=BG,
            foreground=FG,
            font=FONT_BOLD,
        )
        style.map("Treeview", background=[("selected", ACCENT)])
    except Exception:
        # GUI theming is cosmetic; a failure here must not block the app.
        pass
