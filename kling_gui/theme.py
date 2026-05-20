"""Shared theme constants and helpers for the Kling GUI."""

import sys
import logging
import time
import tkinter as tk
from tkinter import ttk
from typing import Callable, Dict


IS_MACOS = sys.platform == "darwin"

_LOGGER = logging.getLogger(__name__)

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
    """Create a cross-platform action button that holds its tint on macOS.

    Returns a ``ttk.Button`` on every platform. The Tk root MUST have
    ``style.theme_use("clam")`` set before this is called — main_window
    does this in ``_setup_ui`` ahead of any button creation.

    Why not ``tk.Button`` on macOS:
        On macOS Aqua, a ``tk.Button`` is rendered by the native HIView
        widget. The HIView ignores ``bg`` and only honors
        ``highlightbackground`` on the *initial* paint — after the very
        first event (focus, click, drag), HIView re-renders the button
        with the default Aqua bezel (white background, black text) and
        the tint is permanently lost. This was the long-running
        "buttons go from tinted → plain after one click" bug.

        ``ttk.Button`` under the ``clam`` theme is drawn entirely by
        Tk's clam rendering code path (HIView is bypassed), so the
        configured ``background``/``foreground`` colors stick through
        every event. Verified visually on Sonoma — same buttons that
        used to revert now stay tinted across click, focus, drag,
        and window resize.

    Click reliability:
        The original concern that drove the ``tk.Button`` path was
        macOS hit-area issues with ``highlightthickness=1`` shrinking
        the clickable interior. That issue is specific to ``tk.Button``
        — ``ttk.Button`` doesn't draw an HIView focus ring at all, so
        the full button bounds are clickable.
    """
    return ttk.Button(parent, text=text, command=command, style=style, **kwargs)


def apply_macos_button_fix(button) -> None:
    """Best-effort polish for an EXISTING raw ``tk.Button`` on macOS.

    No-op on Windows/Linux. On macOS this sets ``highlightbackground``/
    ``highlightcolor`` to the button's own ``bg``, zeroes the focus
    ring, drops the native bezel, and sets a hand cursor.

    WARNING — TINT LIMITATION:
        On macOS Aqua a ``tk.Button`` is HIView-rendered. The HIView
        only honors ``highlightbackground`` on the *initial* paint —
        after the very first event (focus, click, drag), it falls
        back to the default white-bezel-with-black-text Aqua
        appearance. **No raw ``tk.Button`` can hold a colored tint
        on macOS after the first event** regardless of how
        ``highlightthickness``/``highlightbackground``/``bg`` are
        configured. Verified Sonoma 2026-05-20.

        If you need a button whose tint survives clicks, use
        ``create_action_button`` (returns a ``ttk.Button`` under the
        ``clam`` theme, which bypasses HIView entirely).

    What this helper still does usefully on macOS: removes the bevel,
    sets the hand cursor, points the focus-ring color at the button
    fill so the FIRST-PAINT appearance matches the intended tint
    (which is at least the experience the user sees on launch before
    they interact). Safe to call unconditionally; silently ignores
    non-tk widgets.
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
        # CR PR #43 (3273385365): log at debug so real bugs
        # surface in file logs without surfacing as user-visible
        # errors. The fallback (raw tk.Button without the fix)
        # still renders, just without the macOS click-area tweak.
        _LOGGER.debug(
            "apply_macos_button_fix failed", exc_info=True,
        )


# Primary-action button color palette. Used by
# ``apply_primary_action_style`` to mark the SINGLE most-important
# action on a given step/page so users can immediately spot what to
# click next. Goal (per user request, PR #43): the primary action
# should READ as distinctly different from secondary buttons without
# being garish or breaking the existing dark-theme palette.
#
# Chosen palette: accent-blue fill (same as TTK_BTN_PRIMARY) with a
# brighter outline border, bold typeface bumped from 9pt to 10pt,
# slightly increased padding. The border is the discriminator -
# secondary buttons have ``highlightthickness=1`` with a same-as-bg
# border (invisible); primary buttons get a CONTRASTING 2px border
# in BUTTON_FILLED_TEXT_COLOR (the same color as the button's
# foreground text), creating a 'stamped' look without color clash.
_PRIMARY_BG = COLORS["accent_blue"]
_PRIMARY_FG = BUTTON_FILLED_TEXT_COLOR
_PRIMARY_RING = BUTTON_FILLED_TEXT_COLOR  # contrasting border


def apply_primary_action_style(button) -> None:
    """Mark a raw ``tk.Button`` as the PRIMARY action on its page.

    Differentiates the single "most important" button on a step/page
    from the surrounding secondary actions so users immediately know
    what to click next (per user request, PR #43).

    Visual recipe (cross-platform):
      * accent-blue fill (matches TTK_BTN_PRIMARY palette)
      * BUTTON_FILLED_TEXT_COLOR foreground (high contrast on blue)
      * 2px contrasting outline border (the visual discriminator -
        secondary buttons use ``highlightthickness=1`` with a
        same-as-bg border that doesn't read; the primary's
        contrasting border reads as a "stamped" outline)
      * 10pt bold typeface (1pt larger than secondary 9pt bold)
      * Slightly more horizontal padding (12 vs 8) for visual weight

    macOS-aware: on Aqua, ``highlightbackground`` IS the rendered
    color, so the border-as-discriminator needs special handling.
    We use ``highlightbackground`` for the ring (same as Win) but
    DO NOT zero ``highlightthickness`` - keeping the ring visible
    while accepting the slightly smaller hit-area trade-off. Click
    reliability is preserved by the bigger padx/pady.

    Safe to call unconditionally on any ``tk.Button`` AFTER it's
    been created (call this LAST so it overrides any baseline
    ``apply_macos_button_fix`` styling). Silently ignores non-tk
    widgets so it can be sprinkled liberally.

    Usage::

        btn = tk.Button(parent, text="Continue", command=..., ...)
        apply_macos_button_fix(btn)        # baseline
        apply_primary_action_style(btn)    # override to primary

    To revert a button to secondary later (e.g. when the step it
    primaried completes), recreate or restyle with the original
    secondary palette - there's intentionally no 'unmark' helper to
    discourage flickering style changes.
    """
    try:
        button.config(
            bg=_PRIMARY_BG,
            fg=_PRIMARY_FG,
            activebackground=COLORS["bg_hover"],
            activeforeground=_PRIMARY_FG,
            disabledforeground=BUTTON_DISABLED_TEXT_COLOR,
            highlightbackground=_PRIMARY_RING,
            highlightcolor=_PRIMARY_RING,
            highlightthickness=2,
            relief=tk.FLAT,
            bd=0,
            font=(FONT_FAMILY, 10, "bold"),
            padx=12,
            pady=6,
            cursor="hand2",
        )
    except Exception:
        # Never let a cosmetic tweak break widget construction.
        # CR PR #43 (3273385365): log at debug so real bugs
        # surface in file logs without crashing the GUI. The
        # fallback (button keeps whatever style it had before)
        # is harmless - primary buttons just look the same as
        # secondary buttons until the next style pass.
        _LOGGER.debug(
            "apply_primary_action_style failed", exc_info=True,
        )
