"""Shared theme constants and helpers for the Kling GUI."""

import sys
import time
import tkinter as tk
from tkinter import ttk
from typing import Callable, Dict


IS_MACOS = sys.platform == "darwin"

# Global font — change this one line to switch the entire UI typeface.
# Tk on macOS does not reliably resolve Windows font names.
FONT_FAMILY = "Helvetica" if IS_MACOS else "Segoe UI"
EMOJI_FONT_FAMILY = "Apple Color Emoji" if IS_MACOS else "Segoe UI Emoji"

# Unified color palette
COLORS = {
    # Base backgrounds
    "bg_main": "#2D2D30",
    "bg_panel": "#3C3C41",
    "bg_input": "#464649",
    "bg_hover": "#505055",

    # Text
    "text_light": "#DCDCDC",
    "text_dim": "#B4B4B4",
    "text_dark": "#111111",

    # Accents
    "accent_blue": "#6496FF",
    "border": "#5A5A5E",

    # Status
    "success": "#64FF64",
    "error": "#FF6464",
    "warning": "#FFA500",

    # Buttons
    "btn_green": "#329632",
    "btn_red": "#B43232",

    # Drop zone specific
    "bg_drop": "#464649",
    "drop_valid": "#329632",
    "drop_invalid": "#963232",

    # Verbose log colors
    "upload": "#00CED1",
    "task": "#87CEEB",
    "progress": "#FFD700",
    "debug": "#808080",
    "resize": "#DDA0DD",
    "download": "#98FB98",
    "api": "#DA70D6",

    # Config panel extras
    "text_unsupported": "#666666",
    "bg_unsupported": "#3A3A3A",
    "warning_light": "#FFB347",
}

# Native macOS Tk buttons can ignore dark backgrounds, so dark text keeps labels readable.
BUTTON_TEXT_COLOR = "#000000" if IS_MACOS else COLORS["text_light"]
BUTTON_FILLED_TEXT_COLOR = "#000000" if IS_MACOS else COLORS["text_light"]
BUTTON_DISABLED_TEXT_COLOR = "#666666"

# ttk button style names (applied in main_window style setup)
TTK_BTN_PRIMARY = "Primary.TButton"
TTK_BTN_SECONDARY = "Secondary.TButton"
TTK_BTN_DANGER = "Danger.TButton"
TTK_BTN_DANGER_COMPACT = "DangerCompact.TButton"
TTK_BTN_SUCCESS = "Success.TButton"
TTK_BTN_SUCCESS_COMPACT = "SuccessCompact.TButton"
TTK_BTN_COMPACT = "Compact.TButton"
TTK_BTN_TAB_NAV = "TabNav.TButton"

_DEBOUNCE_LAST_CALL: Dict[str, float] = {}


def debounce_command(command: Callable[[], None], key: str, interval_ms: int = 180) -> Callable[[], None]:
    """Wrap a command so rapid repeated clicks invoke it once per interval."""
    min_interval = max(0.03, float(interval_ms) / 1000.0)

    def _wrapped():
        now = time.monotonic()
        last = _DEBOUNCE_LAST_CALL.get(key, 0.0)
        if now - last < min_interval:
            return
        _DEBOUNCE_LAST_CALL[key] = now
        command()

    return _wrapped


def create_action_button(parent, text: str, command, style: str = TTK_BTN_SECONDARY, **kwargs):
    """Create cross-platform action button with macOS click-safe hitbox."""
    if IS_MACOS:
        width = kwargs.pop("width", None)
        state = kwargs.pop("state", tk.NORMAL)
        palette = {
            TTK_BTN_PRIMARY: (COLORS["accent_blue"], BUTTON_FILLED_TEXT_COLOR),
            TTK_BTN_SUCCESS: (COLORS["btn_green"], BUTTON_FILLED_TEXT_COLOR),
            TTK_BTN_SUCCESS_COMPACT: (COLORS["btn_green"], BUTTON_FILLED_TEXT_COLOR),
            TTK_BTN_DANGER: (COLORS["btn_red"], BUTTON_FILLED_TEXT_COLOR),
            TTK_BTN_DANGER_COMPACT: (COLORS["btn_red"], BUTTON_FILLED_TEXT_COLOR),
            TTK_BTN_SECONDARY: (COLORS["bg_input"], BUTTON_TEXT_COLOR),
            TTK_BTN_COMPACT: (COLORS["bg_input"], BUTTON_TEXT_COLOR),
            TTK_BTN_TAB_NAV: (COLORS["bg_input"], BUTTON_TEXT_COLOR),
        }
        bg, fg = palette.get(style, (COLORS["bg_input"], BUTTON_TEXT_COLOR))
        button = tk.Button(
            parent,
            text=text,
            command=command,
            state=state,
            font=(FONT_FAMILY, 10, "bold"),
            bg=bg,
            fg=fg,
            activebackground=COLORS["bg_hover"],
            activeforeground=fg,
            highlightbackground=bg,
            highlightcolor=bg,
            highlightthickness=0,
            relief=tk.FLAT,
            bd=0,
            padx=12,
            pady=8,
            cursor="hand2",
        )
        if width is not None:
            button.config(width=width)
        return button
    return ttk.Button(parent, text=text, command=command, style=style, **kwargs)


def apply_macos_button_fix(button) -> None:
    """Make an EXISTING raw ``tk.Button`` honor its color and be
    reliably clickable on macOS.

    No-op on Windows/Linux (``tk.Button`` already behaves there). On
    macOS Aqua a ``tk.Button``'s rendered color comes from
    ``highlightbackground``, NOT ``bg`` — so a button created with only
    ``bg=`` shows up plain/grey. And a non-zero ``highlightthickness``
    draws a focus ring that shrinks the clickable interior, producing
    the "have to wiggle the cursor and click several times" hit-area
    bug. This mirrors the create_action_button macOS path for buttons
    that (for layout reasons) are still built as raw ``tk.Button``:
    point ``highlightbackground``/``highlightcolor`` at the button's
    own ``bg`` and zero the thickness. The button's existing
    ``fg``/padding are left untouched (no contrast regression).
    Safe to call unconditionally; silently ignores non-tk widgets.
    """
    if not IS_MACOS:
        return
    try:
        fill = button.cget("bg")
        button.config(
            highlightbackground=fill,
            highlightcolor=fill,
            highlightthickness=0,
            relief="flat",
            cursor="hand2",
        )
    except Exception:
        # Never let a cosmetic tweak break widget construction.
        pass
