import unittest

from kling_gui.main_window import UI_CONFIG_DEFAULTS, sanitize_saved_geometry, sanitize_sash_layout, sanitize_window_layout


class LayoutSizingTests(unittest.TestCase):
    def test_default_history_panel_rows_increased_for_step3_layout(self):
        history_defaults = UI_CONFIG_DEFAULTS.get("history_panel", {})
        self.assertGreaterEqual(int(history_defaults.get("visible_rows", 0)), 10)

    def test_window_config_clamps_oversized_values(self):
        window, geometry, changed = sanitize_window_layout(
            window_config={"width": 2200, "height": 1800, "min_width": 1900, "min_height": 1500},
            saved_geometry="",
            screen_width=1512,
            screen_height=982,
        )
        self.assertTrue(changed)
        self.assertLessEqual(window["width"], int(1512 * 0.95))
        self.assertLessEqual(window["height"], int(982 * 0.90))
        self.assertLessEqual(window["min_width"], int(1512 * 0.82))
        self.assertLessEqual(window["min_height"], int(982 * 0.78))
        self.assertEqual(geometry, "")

    def test_saved_geometry_too_tall_gets_capped(self):
        geometry = sanitize_saved_geometry(
            saved_geometry="2100x1800+12+12",
            min_width=760,
            min_height=620,
            max_width=1400,
            max_height=880,
        )
        self.assertEqual(geometry, "1400x880+12+12")

    def test_pathological_sash_values_clamp_to_bounds(self):
        sash, changed = sanitize_sash_layout(
            sash_dropzone=9999,
            sash_prompt_split=10,
            sash_queue=5,
            sash_log=8888,
            sash_log_drop_split=9999,
            root_width=1100,
            root_height=900,
        )
        self.assertTrue(changed)
        self.assertGreaterEqual(sash["sash_dropzone"], 320)
        self.assertLessEqual(sash["sash_dropzone"], int(900 * 0.75))
        self.assertGreaterEqual(sash["sash_prompt_split"], int(1100 * 0.50))
        self.assertLessEqual(sash["sash_prompt_split"], int(1100 * 0.62))
        # v5: bumped from 20-30% / 22% to 22-32% / 26% (carousel) and from
        # 42-62% / 52% to 55-78% / 68% (log_drop split) per user request to
        # widen carousel + give log more space at the dropzone's expense.
        self.assertGreaterEqual(sash["sash_queue"], max(200, int(1100 * 0.22)))
        self.assertLessEqual(sash["sash_queue"], int(1100 * 0.32))
        self.assertGreaterEqual(sash["sash_log"], 110)
        self.assertLessEqual(sash["sash_log"], int(900 * 0.42))
        # log_drop_split is clamped relative to the right section, not the full window.
        clamped_queue = sash["sash_queue"]
        right_w = 1100 - clamped_queue
        self.assertGreaterEqual(sash["sash_log_drop_split"], max(220, int(right_w * 0.55)))
        self.assertLessEqual(sash["sash_log_drop_split"], int(right_w * 0.78))

    def test_sane_values_remain_unchanged(self):
        window, geometry, changed_window = sanitize_window_layout(
            window_config={"width": 1100, "height": 900, "min_width": 760, "min_height": 620},
            saved_geometry="1100x880+300+20",
            screen_width=1600,
            screen_height=1000,
        )
        self.assertFalse(changed_window)
        self.assertEqual(window["width"], 1100)
        self.assertEqual(window["height"], 900)
        self.assertEqual(window["min_width"], 760)
        self.assertEqual(window["min_height"], 620)
        self.assertEqual(geometry, "1100x880+300+20")

        # v5 ranges at root_width=1100:
        #   queue: 22-32% = 242-352, default 26% = 286
        #   prompt_split: 54-64% of 1100 = 594-704
        #   log_drop_split: right_section = 1100 - clamped_queue
        #     at queue=286 → right=814 → 55-78% = 447-635
        sash, changed_sash = sanitize_sash_layout(
            sash_dropzone=500,
            sash_prompt_split=620,
            sash_queue=286,
            sash_log=150,
            sash_log_drop_split=520,
            root_width=1100,
            root_height=900,
        )
        self.assertFalse(changed_sash)
        self.assertEqual(sash["sash_dropzone"], 500)
        self.assertEqual(sash["sash_prompt_split"], 620)
        self.assertEqual(sash["sash_queue"], 286)
        self.assertEqual(sash["sash_log"], 150)
        self.assertEqual(sash["sash_log_drop_split"], 520)


if __name__ == "__main__":
    unittest.main()
