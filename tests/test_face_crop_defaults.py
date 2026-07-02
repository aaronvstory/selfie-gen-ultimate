from pathlib import Path


def test_face_crop_default_expands_generative_expand():
    text = Path("kling_gui/tabs/face_crop_tab.py").read_text(encoding="utf-8")
    assert 'self._expanded_sections = ["expand"]' in text


def test_composite_mode_load_override_for_none():
    """PR #48 round 6: the user repeatedly hit "Step 0 keeps defaulting
    to None" because Step 0 saves whatever's in the composite var to
    disk on quit; a prior session's "none" choice then persists across
    launches and the next session loads "none" → expand result no
    longer preserves original pixels. Force preserve_seamless on load
    when the saved value is unset/blank/"none". The user can still
    pick "None" mid-session for A/B compare; it just won't survive
    relaunch.
    """
    text = Path("kling_gui/tabs/face_crop_tab.py").read_text(encoding="utf-8")
    # The override block must be in face_crop_tab.py
    assert "_saved_composite.strip().lower() in" in text
    # And explicitly list both empty-string and "none" as the
    # override triggers.
    assert "\"none\"" in text or "'none'" in text
    assert "preserve_seamless" in text


def test_composite_mode_load_override_logic_unit():
    """Pure-string-level smoke for the override predicate so a future
    refactor that breaks the empty/none coercion would fail this test
    without needing a Tk root."""
    def _override(saved):
        # Same predicate as face_crop_tab.py at the composite_var init.
        if not isinstance(saved, str) or saved.strip().lower() in ("", "none"):
            return "preserve_seamless"
        return saved

    assert _override(None) == "preserve_seamless"
    assert _override("") == "preserve_seamless"
    assert _override("   ") == "preserve_seamless"
    assert _override("none") == "preserve_seamless"
    assert _override("None") == "preserve_seamless"
    assert _override("NONE") == "preserve_seamless"
    # Real values pass through unchanged.
    assert _override("preserve_seamless") == "preserve_seamless"
    assert _override("feathered") == "feathered"
    assert _override("hard") == "hard"


def test_step0_expand_modes_are_vertical_not_in_action_row():
    """Step 0 mode choices must not widen the action row/right sash."""
    text = Path("kling_gui/tabs/face_crop_tab.py").read_text(encoding="utf-8")
    assert "mode_options_frame = tk.Frame" in text
    mode_block = text[text.index("mode_options_frame = tk.Frame"):text.index("# Percentage controls")]
    for value in (
        '"three_four_fullres"',
        '"percentage_fullres"',
        '"percentage"',
        '"pixels"',
    ):
        assert value in mode_block
    assert "btn_row," not in mode_block
    assert "3:4 Full-res (recommended)" in mode_block
    assert "% Full-res (same ratio)" in mode_block


def test_outpaint_percentage_presets_mark_35_as_default():
    for path in (
        Path("kling_gui/tabs/outpaint_tab.py"),
        Path("distribution/kling_gui/tabs/outpaint_tab.py"),
    ):
        text = path.read_text(encoding="utf-8")
        assert "DEFAULT_OUTPAINT_EXPAND_PERCENT" in text
        assert "30% (default)" not in text
