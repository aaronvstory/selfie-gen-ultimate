"""Regression guards on resemble-score/src/theme.py.

The original bug: Treeview.Heading was style.configure()'d but had no
style.map() entries for ('active', ...) or ('pressed', ...). ttk's clam
fallback paints a near-white background under the light heading text on
both Windows and macOS, making the active sort column unreadable while
the user is hovering or clicking. PR #33 fixed that — these tests keep
the fix in place across future palette refactors.

Skipped automatically when no display / Tk is available (headless CI).
"""

from __future__ import annotations

import pytest

tk = pytest.importorskip("tkinter")
ttk = pytest.importorskip("tkinter.ttk")

from src import theme  # noqa: E402  (after importorskip)


@pytest.fixture
def root():
    try:
        r = tk.Tk()
    except tk.TclError:
        pytest.skip("no display / Tk unavailable")
    r.withdraw()
    yield r
    try:
        r.destroy()
    except tk.TclError:
        pass


def test_selected_fg_constant_exported():
    """SELECTED_FG must exist and be distinct from FG.

    Pure white reads ~1 contrast step crisper on ACCENT than FG (#e6e6e6),
    so the two are kept separate. If a future palette tweak conflates them,
    selected-row text on the Treeview silently desaturates.
    """
    assert hasattr(theme, "SELECTED_FG"), "SELECTED_FG palette constant missing"
    assert theme.SELECTED_FG.lower() == "#ffffff", (
        f"SELECTED_FG = {theme.SELECTED_FG!r}; expected pure white. If you "
        "intentionally changed this, update the test + ensure contrast on ACCENT."
    )
    assert theme.SELECTED_FG != theme.FG, (
        "SELECTED_FG must stay distinct from FG so selected text doesn't "
        "follow palette tweaks that desaturate the resting foreground."
    )


def test_apply_dark_ttk_sets_treeview_heading_active_map(root):
    """Hovering a sort column must NOT inherit the OS-default light background."""
    theme.apply_dark_ttk(root)
    style = ttk.Style(root)

    bg_map = dict(style.map("Treeview.Heading", "background"))
    fg_map = dict(style.map("Treeview.Heading", "foreground"))

    assert "active" in bg_map, (
        "Treeview.Heading missing ('active', <bg>) map; ttk's clam fallback "
        "will paint a near-white hover background under the light heading text. "
        "See PR #33."
    )
    assert "pressed" in bg_map, (
        "Treeview.Heading missing ('pressed', <bg>) map; clicking a column "
        "header to sort will flash the OS-default press color."
    )
    assert "active" in fg_map and "pressed" in fg_map, (
        "Treeview.Heading foreground map missing 'active' or 'pressed' entry"
    )

    # The hover background must be in the dark palette, not the clam default
    # near-white (#dcdad5 or similar). Allow any dark panel color we ship.
    active_bg = bg_map["active"].lower()
    assert active_bg.startswith("#") and active_bg != "#ffffff", (
        f"Treeview.Heading active background = {active_bg!r}; expected a "
        "dark-palette color, not white/light fallback."
    )
    # And the active foreground must contrast against that — ACCENT in our
    # palette. If a refactor sets fg=bg the column disappears.
    assert fg_map["active"].lower() != active_bg, (
        "Treeview.Heading active fg == bg (column would be invisible on hover)"
    )


def test_apply_dark_ttk_uses_selected_fg_for_treeview_selection(root):
    """Treeview ('selected', fg) must route through SELECTED_FG, not magic hex."""
    theme.apply_dark_ttk(root)
    style = ttk.Style(root)

    fg_map = dict(style.map("Treeview", "foreground"))
    assert "selected" in fg_map, "Treeview missing ('selected', <fg>) map"
    assert fg_map["selected"].lower() == theme.SELECTED_FG.lower(), (
        f"Treeview selected fg = {fg_map['selected']!r}; expected to match "
        f"theme.SELECTED_FG ({theme.SELECTED_FG!r}). A hardcoded hex literal "
        "regressed the iter #1 Sourcery fix."
    )


def test_apply_dark_ttk_is_idempotent_and_swallows_errors(root):
    """Calling apply_dark_ttk twice must not raise, and a bad root must not raise."""
    # Twice on the same root — clam style merge should be a no-op the 2nd pass.
    theme.apply_dark_ttk(root)
    theme.apply_dark_ttk(root)

    # Synthetic broken root: docstring promises best-effort, never raises.
    class _BrokenRoot:
        def configure(self, **kwargs):
            raise RuntimeError("synthetic")

    theme.apply_dark_ttk(_BrokenRoot())  # must not raise
