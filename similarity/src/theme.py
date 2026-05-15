"""Industrial / brutalist palette + font helpers for the standalone GUI.

Lightweight constants module — no JSON theme file, no CTk theme override.
gui.py imports the colors and applies them per-widget via fg_color=,
text_color=, border_color=, hover_color=, font= overrides.

Design directive: minimal and professional, not cheesy. Hairline borders,
flat fills, no shadows, no gradients, no animations beyond the existing
progress bar. Numbers are the loud element; everything else is quiet
supporting metadata.
"""

from __future__ import annotations

import customtkinter as ctk

# ── Surface tones (near-black to mid-gray) ─────────────────────────────
BG_DEEP = "#0E0F12"        # Window background — near-black, not pitch black
BG_PANEL = "#1A1C20"       # Zone frames, hero card surface
BG_PANEL_HI = "#22252A"    # Elevated surfaces, button hover

# ── Borders (1px hairlines) ─────────────────────────────────────────────
BORDER = "#2E3238"         # Hairline panel borders, dropzone outline
BORDER_HI = "#3D434B"      # Active/hover borders

# ── Text hierarchy ─────────────────────────────────────────────────────
TEXT = "#E6E8EB"           # Primary
TEXT_DIM = "#8A9099"       # Secondary, captions
TEXT_MUTE = "#5C636C"      # Tertiary, placeholders, status N/A

# ── Accent (CTA / hero highlights) ─────────────────────────────────────
ACCENT = "#D4D6DA"         # Brutalist white-gray
ACCENT_HI = "#FFFFFF"      # Hover/active accent

# ── Status semantics ──────────────────────────────────────────────────
OK = "#5DB075"             # Match / Real / pass
WARN = "#D9A441"           # Warnings (rarely used now)
FAIL = "#E5484D"           # No-match / Spoof / fail
INFO = "#7AA2C8"           # Information

# ── Typography helpers ─────────────────────────────────────────────────
# Use platform-native mono fallbacks so we don't ship custom fonts.
# Cascadia on Windows, SF Mono / Menlo on macOS, DejaVu Sans Mono on Linux.
MONO_FAMILIES = ("Cascadia Mono", "SF Mono", "Menlo", "DejaVu Sans Mono", "Consolas", "Courier New")
SANS_FAMILIES = ("Inter", "Segoe UI Variable", "SF Pro Display", "Helvetica Neue", "Segoe UI", "sans-serif")


def mono_font(size: int = 12, weight: str = "normal") -> ctk.CTkFont:
    """Return a CTkFont with the first available mono family at the given size."""
    return ctk.CTkFont(family=_first_available(MONO_FAMILIES), size=size, weight=weight)


def sans_font(size: int = 12, weight: str = "normal") -> ctk.CTkFont:
    """Return a CTkFont with the first available sans family at the given size."""
    return ctk.CTkFont(family=_first_available(SANS_FAMILIES), size=size, weight=weight)


def _first_available(families: tuple) -> str:
    """Pick the first family that exists on this system; fall back to the last
    entry (which is always a generic fallback like 'sans-serif' or 'Courier New')."""
    try:
        import tkinter as tk
        import tkinter.font as tkfont
        # We need a root for font.families(); CTk creates one on first use,
        # but during module-import time the user may not have built the GUI
        # yet. Defer to default if tkinter root isn't ready.
        root = tk._default_root  # type: ignore[attr-defined]
        if root is None:
            return families[0]
        installed = set(tkfont.families(root))
        for fam in families:
            if fam in installed:
                return fam
    except Exception:
        pass
    return families[0]
