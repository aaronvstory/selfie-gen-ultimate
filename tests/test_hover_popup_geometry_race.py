"""PR #83 HIGH-1 (Gemini round 2, 3 inline comments, QC subagent
2026-06-07): the hover-popup anti-flash check ``_cursor_over_popup``
read ``popup.winfo_width()`` / ``popup.winfo_height()`` without a
fallback. On the FIRST call (immediately after the popup is mapped),
those return 1 because the WM hasn't yet finished geometry negotiation.
With w=h=1 the bbox check always says "cursor outside" → popup
destroys → canvas re-enters → 500ms later the popup re-appears → the
flash loop the rest of PR #83 was trying to kill comes RIGHT back.

Fix: fall back to ``winfo_reqwidth()`` / ``winfo_reqheight()`` (the
requested size, available BEFORE the WM maps the widget) when the
mapped values are <=1. These tests source-pin the fix in BOTH
locations (compare_panel + carousel_widget) AND their distribution
mirrors so a refactor that drops the fallback fails fast.
"""

from __future__ import annotations

from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parent.parent


def _read(rel: str) -> str:
    return (REPO_ROOT / rel).read_text(encoding="utf-8")


def test_compare_panel_cursor_over_popup_uses_reqwidth_fallback():
    src = _read("kling_gui/compare_panel.py")
    assert "winfo_reqwidth()" in src, (
        "compare_panel._cursor_over_popup must fall back to "
        "winfo_reqwidth() when winfo_width() returns <=1 — without "
        "this, the very first anti-flash check fails the bbox test "
        "and the flash loop returns (Gemini PR #83 HIGH-1)."
    )
    assert "winfo_reqheight()" in src, (
        "compare_panel._cursor_over_popup must fall back to "
        "winfo_reqheight() when winfo_height() returns <=1."
    )


def test_carousel_widget_cursor_over_popup_uses_reqwidth_fallback():
    src = _read("kling_gui/carousel_widget.py")
    assert "winfo_reqwidth()" in src, (
        "carousel_widget._cursor_over_popup must fall back to "
        "winfo_reqwidth() when winfo_width() returns <=1 "
        "(Gemini PR #83 HIGH-1, mirror of compare_panel fix)."
    )
    assert "winfo_reqheight()" in src


def test_distribution_mirror_compare_panel_has_fallback():
    src = _read("distribution/kling_gui/compare_panel.py")
    assert "winfo_reqwidth()" in src, (
        "distribution mirror of compare_panel must carry the same "
        "fallback — release zips ship from distribution/, so a fix "
        "that only lands in the working tree won't reach end users."
    )
    assert "winfo_reqheight()" in src


def test_distribution_mirror_carousel_widget_has_fallback():
    src = _read("distribution/kling_gui/carousel_widget.py")
    assert "winfo_reqwidth()" in src
    assert "winfo_reqheight()" in src
