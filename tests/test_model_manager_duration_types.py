"""Regression: ModelManagerDialog must read string duration values.

Codex P2, PR #41: models.json now persists ``duration_options`` /
``duration_default`` as JSON strings (``"5"``/``"10"``). The editor and
its save paths work purely in ints (``_DURATION_CHOICES = [5, 10]``).
``_switch_to_edit_mode`` checked ``5 in dur_opts`` / ``10 in dur_opts``
against the raw (string) list, so ``5 in ["5","10"]`` was False: every
factory model opened with BOTH duration checkboxes unticked, and a
normal Save then silently collapsed durations to ``[10]`` — dropping
5s support the user never intended to remove.

These tests instantiate the dialog via ``__new__`` (no Tk) and drive
only the duration read/save surface with fake vars, asserting:
  * string durations tick the right boxes;
  * a no-touch read -> save round-trip preserves BOTH durations;
  * junk values are tolerated, not crashed on.
"""

import importlib
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))


class _Var:
    """tk.*Var stand-in: holds a value, .set()/.get()."""

    def __init__(self, value=None):
        self._v = value

    def set(self, v):
        self._v = v

    def get(self):
        return self._v


class _Widget:
    """No-op stand-in for any .config()/.pack*/.delete/.insert/.get widget."""

    def config(self, **kw):
        pass

    def pack(self, *a, **kw):
        pass

    def pack_forget(self, *a, **kw):
        pass

    def delete(self, *a, **kw):
        pass

    def insert(self, *a, **kw):
        pass

    def get(self, *a, **kw):
        return ""


def _make_dialog(model):
    module = importlib.import_module("kling_gui.model_manager_dialog")
    dlg = module.ModelManagerDialog.__new__(module.ModelManagerDialog)

    dlg._all_models = [model]
    dlg._custom_models = []
    dlg._config = {}
    dlg._edit_mode = False
    dlg._edit_index = None

    # Duration vars (the surface under test)
    dlg._dur_5_var = _Var(False)
    dlg._dur_10_var = _Var(False)
    dlg._default_dur_var = _Var("10")

    # Other vars/widgets _switch_to_edit_mode / _save_edit poke at.
    for name in (
        "_name_var", "_endpoint_var", "_release_var",
    ):
        setattr(dlg, name, _Var(""))
    for name in (
        "_right_frame", "_add_btn", "_save_edit_btn", "_endpoint_entry",
        "_endpoint_hint", "_notes_text", "_test_result_text",
    ):
        setattr(dlg, name, _Widget())

    return dlg, module


class DurationStringReadTests(unittest.TestCase):
    def test_string_duration_options_tick_both_boxes(self):
        model = {
            "name": "Kling 2.5",
            "endpoint": "fal-ai/kling-video/v2.5-turbo/pro/image-to-video",
            "duration_options": ["5", "10"],
            "duration_default": "10",
            "_factory": True,
        }
        dlg, _ = _make_dialog(model)
        dlg._switch_to_edit_mode(0)
        self.assertTrue(
            dlg._dur_5_var.get(),
            "5s box must be ticked for duration_options=['5','10']",
        )
        self.assertTrue(dlg._dur_10_var.get())
        self.assertEqual(dlg._default_dur_var.get(), "10")

    def test_int_duration_options_still_work(self):
        model = {
            "name": "legacy",
            "endpoint": "x/y",
            "duration_options": [5, 10],
            "duration_default": 10,
            "_factory": True,
        }
        dlg, _ = _make_dialog(model)
        dlg._switch_to_edit_mode(0)
        self.assertTrue(dlg._dur_5_var.get())
        self.assertTrue(dlg._dur_10_var.get())

    def test_single_string_duration(self):
        model = {
            "name": "ten only",
            "endpoint": "x/y",
            "duration_options": ["10"],
            "duration_default": "10",
            "_factory": True,
        }
        dlg, _ = _make_dialog(model)
        dlg._switch_to_edit_mode(0)
        self.assertFalse(dlg._dur_5_var.get())
        self.assertTrue(dlg._dur_10_var.get())

    def test_junk_duration_value_is_tolerated(self):
        model = {
            "name": "junk",
            "endpoint": "x/y",
            "duration_options": ["5", "bogus", None, "10"],
            "duration_default": "not-a-number",
            "_factory": True,
        }
        dlg, _ = _make_dialog(model)
        dlg._switch_to_edit_mode(0)  # must not raise
        self.assertTrue(dlg._dur_5_var.get())
        self.assertTrue(dlg._dur_10_var.get())
        self.assertEqual(dlg._default_dur_var.get(), "10")  # safe fallback


class DurationRoundTripTests(unittest.TestCase):
    def test_read_then_save_preserves_both_durations(self):
        """The original bug's real damage: open a string-duration factory
        model and Save without touching durations -> they must NOT
        collapse to [10]."""
        model = {
            "name": "Kling 2.5",
            "endpoint": "fal-ai/kling-video/v2.5-turbo/pro/image-to-video",
            "duration_options": ["5", "10"],
            "duration_default": "10",
            "_factory": True,
        }
        dlg, _ = _make_dialog(model)

        # Save path needs the name var populated; the post-write listbox
        # refresh is irrelevant to the duration-write assertion, so stub
        # it (its real impl needs the full Tk listbox surface).
        dlg._name_var.set("Kling 2.5")
        dlg._rebuild_model_list = lambda *a, **k: None
        dlg._switch_to_edit_mode(0)
        dlg._save_edit()

        self.assertEqual(
            sorted(model["duration_options"]), [5, 10],
            "round-trip Save must preserve both durations, not collapse "
            "to [10]",
        )
        self.assertIn(model["duration_default"], (5, 10))


if __name__ == "__main__":
    unittest.main()
