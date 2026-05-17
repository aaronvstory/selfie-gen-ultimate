"""Regression guard on the kling_gui main window's Treeview.Heading hover map.

Mirror of resemble-score/tests/test_theme.py for the main app's
Treeview styling. The original bug: style.configure() set the resting
palette but had no style.map() entries for ('active', …) or ('pressed', …),
so ttk's clam fallback painted a near-white hover background under the
light heading text — the active sort column became unreadable on hover.

Uses static-text source inspection (no Tk root) so it stays headless-safe
on every CI surface. The companion resemble-score test
(resemble-score/tests/test_theme.py) drives a real ttk style to assert
the equivalent contract for that subproject; together they cover both
GUIs that share the bug class.
"""
from __future__ import annotations

import inspect
import re


def _setup_ui_source() -> str:
    from kling_gui.main_window import KlingGUIWindow
    return inspect.getsource(KlingGUIWindow._setup_ui)


def test_treeview_heading_style_map_present():
    """style.map("Treeview.Heading", …) must exist with active + pressed."""
    src = _setup_ui_source()
    # Locate the Treeview.Heading style.map block (one of several style.map
    # calls in _setup_ui — anchor by the symbolic name).
    match = re.search(
        r'style\.map\(\s*"Treeview\.Heading"\s*,(.+?)\)\s*\n',
        src,
        re.DOTALL,
    )
    assert match, (
        'style.map("Treeview.Heading", …) is missing from KlingGUIWindow._setup_ui. '
        "Without an explicit hover/pressed map, ttk's clam fallback paints "
        "a near-white background under the light heading text and the active "
        "sort column becomes unreadable. See PR #33."
    )
    body = match.group(1)
    assert '"active"' in body or "'active'" in body, (
        'Treeview.Heading style.map missing "active" state — hover would '
        "fall through to the OS-default light background."
    )
    assert '"pressed"' in body or "'pressed'" in body, (
        'Treeview.Heading style.map missing "pressed" state — clicking a '
        "column header to sort would flash the OS-default press color."
    )


def test_treeview_heading_active_uses_accent_foreground():
    """Active/pressed fg routes through COLORS["accent_blue"], not magic hex.

    Pinning the fg to a named palette color (rather than a literal like
    "#ffffff" or "blue") means a future palette refactor cannot silently
    desaturate the hovered sort column.
    """
    src = _setup_ui_source()
    match = re.search(
        r'style\.map\(\s*"Treeview\.Heading"\s*,(.+?)\)\s*\n',
        src,
        re.DOTALL,
    )
    assert match, "Treeview.Heading style.map block not found"
    body = match.group(1)
    # Must use the named palette entry — magic hex literals are a smell.
    assert 'COLORS["accent_blue"]' in body or "COLORS['accent_blue']" in body, (
        "Treeview.Heading active/pressed foreground should use "
        'COLORS["accent_blue"], not a hardcoded hex literal — keeps the '
        "hover contrast in sync with the rest of the palette."
    )


def test_treeview_heading_active_background_is_named_palette():
    """Hover background must be a palette constant, not a literal color."""
    src = _setup_ui_source()
    match = re.search(
        r'style\.map\(\s*"Treeview\.Heading"\s*,(.+?)\)\s*\n',
        src,
        re.DOTALL,
    )
    assert match, "Treeview.Heading style.map block not found"
    body = match.group(1)
    # Look for COLORS[...] under the 'background' key. We don't pin to a
    # specific key (bg_hover vs bg_input) so the test stays robust to
    # palette tuning — just guard against raw hex.
    bg_section = re.search(r"background\s*=\s*\[(.+?)\]", body, re.DOTALL)
    assert bg_section, "Treeview.Heading style.map has no background= entry"
    bg_body = bg_section.group(1)
    # Find tuples like ("active", COLORS["x"]) or ("pressed", COLORS["x"]).
    assert "COLORS[" in bg_body, (
        "Treeview.Heading active/pressed background must use COLORS[...] "
        f"(palette constant), got: {bg_body.strip()}"
    )
    # And it must NOT be a raw hex literal.
    raw_hex = re.findall(r'"#[0-9a-fA-F]{3,8}"', bg_body)
    assert not raw_hex, (
        f"Treeview.Heading hover background uses raw hex literal(s) {raw_hex}; "
        "use a COLORS[...] palette constant instead so palette refactors "
        "stay coherent."
    )


def test_treeview_heading_base_relief_is_flat():
    """Base style.configure must set relief=flat so the hover sunken stands out."""
    src = _setup_ui_source()
    # The Treeview.Heading style.configure block (NOT the .map block).
    match = re.search(
        r'style\.configure\(\s*\n?\s*"Treeview\.Heading"\s*,(.+?)\)\s*\n',
        src,
        re.DOTALL,
    )
    assert match, "Treeview.Heading style.configure block not found"
    body = match.group(1)
    assert 'relief="flat"' in body or "relief='flat'" in body, (
        'Treeview.Heading base style must set relief="flat" so the pressed-state '
        "sunken relief reads as a visual response (no relief contrast otherwise)."
    )
