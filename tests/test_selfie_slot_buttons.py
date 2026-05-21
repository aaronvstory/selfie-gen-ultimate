"""Slot 1/2/3 selector style assertion.

After the tk.Button → ttk.Button migration the slot buttons swap visual
state via ``configure(style=…)`` instead of ``config(bg=…, fg=…)`` —
ttk widgets don't accept raw bg/fg, the active/inactive look is baked
into TTK_BTN_SLOT_ACTIVE / TTK_BTN_SLOT_INACTIVE styles configured in
main_window._setup_ui. Lock the swap so a future refactor that
accidentally goes back to per-button color edits surfaces here.
"""
import unittest

from kling_gui.tabs.selfie_tab import SelfieTab
from kling_gui.theme import TTK_BTN_SLOT_ACTIVE, TTK_BTN_SLOT_INACTIVE


class _FakeVar:
    def __init__(self, value):
        self._value = value

    def get(self):
        return self._value


class _FakeButton:
    def __init__(self):
        self.configure_calls = []

    def configure(self, **kwargs):
        self.configure_calls.append(kwargs)

    # Some ttk callers route through .config() instead — accept both.
    config = configure


class SelfieSlotButtonStyleTests(unittest.TestCase):
    def test_active_and_inactive_slot_buttons_swap_via_ttk_style(self):
        tab = SelfieTab.__new__(SelfieTab)
        tab._selfie_slot_var = _FakeVar(2)
        tab._slot_buttons = [_FakeButton(), _FakeButton(), _FakeButton()]

        tab._update_selfie_slot_button_colors()

        # Each button should have received exactly one configure call
        # with style= set to the right active/inactive style.
        for i, btn in enumerate(tab._slot_buttons, start=1):
            self.assertEqual(
                len(btn.configure_calls), 1,
                f"slot {i}: expected 1 configure call",
            )
            style = btn.configure_calls[-1].get("style")
            expected = (
                TTK_BTN_SLOT_ACTIVE if i == tab._selfie_slot_var.get()
                else TTK_BTN_SLOT_INACTIVE
            )
            self.assertEqual(
                style, expected,
                f"slot {i} ({'active' if i == 2 else 'inactive'}) "
                f"must use {expected}",
            )

        # Negative: no bg/fg keys should leak through — that would
        # indicate someone reintroduced the raw tk.Button pattern.
        for btn in tab._slot_buttons:
            kw = btn.configure_calls[-1]
            self.assertNotIn(
                "bg", kw, "ttk slot buttons must not receive bg=",
            )
            self.assertNotIn(
                "fg", kw, "ttk slot buttons must not receive fg=",
            )


if __name__ == "__main__":
    unittest.main()
