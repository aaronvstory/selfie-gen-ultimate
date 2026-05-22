"""Static text-grep regression: confirm the 7 tight ttk button styles in
``kling_gui/main_window.py`` reference the ``mac_padding(...)`` helper, not
a hard-coded tuple.

Without this guard, a future refactor that re-pastes a literal
``padding=(6, 3),`` into one of these style blocks would silently regress
the macOS hit-target fix from commit 6cb2e505. The same defensive pattern
is used for the similarity launcher resolver
(``tests/test_similarity_launcher_resolver.py``) — no subprocess, no Tk
root, just bytes-on-disk grep.
"""

from pathlib import Path

import pytest

_MAIN_WINDOW = Path(__file__).resolve().parents[1] / "kling_gui" / "main_window.py"

# The style names whose `padding=...` lines were migrated to
# `mac_padding(...)` in commit 6cb2e505.
_TIGHT_STYLES = [
    "TTK_BTN_SLOT_ACTIVE",
    "TTK_BTN_SLOT_INACTIVE",
    "TTK_BTN_SUCCESS_COMPACT",
    "TTK_BTN_DANGER_COMPACT",
    "TTK_BTN_COMPACT",
    '"CarouselRefActive.TButton"',
    '"CarouselRefInactive.TButton"',
]


@pytest.fixture(scope="module")
def source() -> str:
    return _MAIN_WINDOW.read_text(encoding="utf-8")


def test_mac_padding_is_imported(source: str):
    """The helper must be imported from .theme, not just referenced."""
    assert "mac_padding" in source
    # Should appear in the import block (somewhere before the first usage).
    import_idx = source.find("from .theme import")
    first_usage_idx = source.find("mac_padding(")
    assert import_idx != -1, "no .theme import block found"
    assert first_usage_idx != -1, "no mac_padding(...) usage found"
    assert import_idx < first_usage_idx, (
        "mac_padding used before being imported"
    )


@pytest.mark.parametrize("style_name", _TIGHT_STYLES)
def test_style_uses_mac_padding(source: str, style_name: str):
    """For each tight style, the style.configure(...) block must call
    ``mac_padding(...)`` (not a hard-coded ``padding=(N, M),``) within
    ~600 bytes of the style-name marker INSIDE a style.configure call.

    We anchor on the ``style.configure(`` form so the test ignores the
    import block (where the style names also appear). We don't pin
    specific tuples — those are tunable. The contract this test enforces
    is: "tight style declarations route through the per-platform helper."
    """
    # Walk every occurrence of the style name; require at least ONE
    # follows a `style.configure(` and contains mac_padding(...) within
    # 600 bytes after.
    found_any_configure_block = False
    start = 0
    while True:
        idx = source.find(style_name, start)
        if idx == -1:
            break
        # Look back ~80 chars to see if this occurrence is the first
        # positional arg to a style.configure(...) call.
        prefix = source[max(0, idx - 80):idx]
        if "style.configure(" in prefix:
            found_any_configure_block = True
            window = source[idx:idx + 600]
            assert "mac_padding(" in window, (
                f"{style_name}: style.configure(...) block does not call "
                f"mac_padding(...). Hard-coded padding tuple is a regression "
                f"of commit 6cb2e505.\n\nWindow:\n{window[:400]}"
            )
        start = idx + 1
    assert found_any_configure_block, (
        f"{style_name}: no style.configure(...) call found at all — did "
        f"the style get renamed?"
    )


def test_no_hardcoded_tight_padding(source: str):
    """The literal ``padding=(6, 3),`` and ``padding=(7, 4),`` tuples
    that ``mac_padding`` replaced must NOT reappear in main_window.py."""
    # NOTE: this is intentionally tight. If a future commit adds a new
    # style with these exact tuples that ISN'T meant to use mac_padding,
    # add an exception comment here.
    forbidden = ["padding=(6, 3),", "padding=(7, 4),"]
    offenders = [needle for needle in forbidden if needle in source]
    assert not offenders, (
        f"hard-coded tight padding tuples reappeared in main_window.py: "
        f"{offenders!r} — route through mac_padding((default), (macos)) "
        f"instead."
    )
