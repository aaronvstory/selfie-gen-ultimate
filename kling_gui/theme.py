"""Shared theme constants and helpers for the Kling GUI."""

import os
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
# Cross-platform monospace family. macOS has Menlo since 10.6 (always
# available); Windows has Consolas since Vista. Hardcoded "Consolas"
# elsewhere in the codebase fell back to a default mono on macOS that
# rendered poorly. Use this constant for any listbox / log / code-style
# widget instead of bare "Consolas".
FONT_MONO = "Menlo" if IS_MACOS else "Consolas"

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
# Main-workflow primary action — accent-blue fill, contrasting border,
# slightly larger typography + padding so users instantly spot "what to
# click next" on each step. Distinct from TTK_BTN_PRIMARY (which is the
# generic blue used everywhere). Applied to: Detect Face & Crop / Add to
# Carousel / Expand Image (step 0), Generate Selfie (step 2), Expand
# Active Image (step 2.5), Start — Using Carousel Image (step 3).
TTK_BTN_WORKFLOW = "Workflow.TButton"
TTK_BTN_SLOT_ACTIVE = "SlotActive.TButton"
TTK_BTN_SLOT_INACTIVE = "SlotInactive.TButton"

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


# ── macOS hit-target sizing helpers ────────────────────────────────────
#
# Long-running issue: on macOS Aqua, ttk.Button styles with very tight
# vertical padding (e.g. SLOT at (6, 3), COMPACT at (8, 4)) and the
# tiny raw tk.Checkbutton/tk.Radiobutton widgets often need 2-10 clicks
# before a click registers. The visible button area is fine; the click
# target inside it is the problem. PR #40 / commit b3bc7398 moved every
# action button to ttk under clam (fixing the tint reversion and the
# focus-ring shrink), but didn't enlarge padding — and these tight
# styles still under-shoot the macOS pointer event resolution.
#
# Strategy: bump only the TIGHT styles on macOS. Leave PRIMARY/SECONDARY/
# WORKFLOW alone (already (10, 6) / (14, 7)) so layout proportions don't
# shift. Windows path is a true no-op: same tuples in, same tuples out.

def mac_padding(default: tuple, macos: tuple) -> tuple:
    """Return ``macos`` on darwin, ``default`` everywhere else.

    Centralizes the per-platform padding split so every tight ttk
    button style and the raw tk widgets share the same rule, without
    scattered ``if IS_MACOS`` branches at every style declaration.
    """
    return macos if IS_MACOS else default


def macos_widget_pad() -> dict:
    """Return ``{'padx': N, 'pady': M}`` on darwin, ``{}`` elsewhere.

    Spread into raw ``tk.Checkbutton`` / ``tk.Radiobutton`` /
    ``tk.Menubutton`` constructors as ``**macos_widget_pad()`` to grow
    the macOS hit target without disturbing Windows layout.
    """
    if not IS_MACOS:
        return {}
    return {"padx": 6, "pady": 3}


# ── Opt-in click diagnostics ───────────────────────────────────────────
#
# Enabled when env var ``KLING_DEBUG_CLICKS=1``. Bind to a specific
# widget via ``attach_click_diagnostics(widget, label="Expand Image")``
# when investigating a missed-click report. No-op by default.

CLICK_DEBUG = os.environ.get("KLING_DEBUG_CLICKS") == "1"


def attach_click_diagnostics(widget, label: str = "") -> None:
    """Log press/release coords + widget bounds when CLICK_DEBUG is on.

    Off unless ``KLING_DEBUG_CLICKS=1`` was in the environment at
    import time. The helper is left in tree so future investigations
    can wire it up case-by-case; do NOT call it by default.

    Note: ``CLICK_DEBUG`` is captured ONCE at module import. Toggling
    the env var at runtime has no effect — restart the app to flip
    the flag. The test suite uses ``monkeypatch.setattr(theme,
    "CLICK_DEBUG", True)`` as a test-only escape hatch; that path is
    not available to end users.
    """
    if not CLICK_DEBUG:
        return

    def _on_press(ev):
        w = ev.widget
        # All Tk-side reads must be guarded — a click immediately
        # before widget destruction can fire the bound callback against
        # a half-torn-down widget (TclError on macOS). Per Gemini
        # review on PR #48.
        try:
            name = label or w.winfo_name()
        except Exception:
            name = label or "<destroyed>"
        try:
            bounds = (w.winfo_width(), w.winfo_height())
        except Exception:
            bounds = ("?", "?")
        _LOGGER.warning(
            "[click-debug] press %s @ (%d,%d) widget=%s bounds=%s",
            name, ev.x, ev.y, w, bounds,
        )

    def _on_release(ev):
        try:
            name = label or ev.widget.winfo_name()
        except Exception:
            name = label or "<destroyed>"
        _LOGGER.warning(
            "[click-debug] release %s @ (%d,%d)",
            name, ev.x, ev.y,
        )

    widget.bind("<ButtonPress-1>", _on_press, add="+")
    widget.bind("<ButtonRelease-1>", _on_release, add="+")


