import unittest

from kling_gui.layout_utils import parse_geometry_size
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
        # v5.3 (intentional, user feedback 2026-05-21): clamp ranges
        # widened to PHYSICAL usability floors instead of aesthetic
        # percentages, so saved values aren't silently bumped back
        # toward the defaults on every launch. Source of truth:
        # kling_gui/layout_utils.py::sanitize_sash_layout().
        self.assertGreaterEqual(sash["sash_dropzone"], 200)
        self.assertLessEqual(sash["sash_dropzone"], int(900 * 0.85))
        self.assertGreaterEqual(sash["sash_prompt_split"], 400)
        self.assertLessEqual(sash["sash_prompt_split"], int(1100 * 0.80))
        self.assertGreaterEqual(sash["sash_queue"], 200)
        self.assertLessEqual(sash["sash_queue"], int(1100 * 0.50))
        self.assertGreaterEqual(sash["sash_log"], 80)
        self.assertLessEqual(sash["sash_log"], int(900 * 0.60))
        # log_drop_split clamped relative to right_section_w with
        # 150px floor on both sides.
        clamped_queue = sash["sash_queue"]
        right_w = max(400, 1100 - clamped_queue)
        self.assertGreaterEqual(sash["sash_log_drop_split"], 150)
        self.assertLessEqual(sash["sash_log_drop_split"], right_w - 150)

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

        # Ranges at root_width=1100:
        #   queue: 22-32% = 242-352, default 25% = 275
        #   prompt_split: 54-64% of 1100 = 594-704
        #   log_drop_split (= LOG width; drop zone = remainder): the drop zone
        #     is bounded to a narrow 190–250px band so it can't hog width.
        #     At queue=286 → right_section=814 → log must be in
        #     [814-250, 814-190] = [564, 624], default 814-210 = 604.
        # Pick a log_drop_split already inside the band so nothing changes.
        sash, changed_sash = sanitize_sash_layout(
            sash_dropzone=500,
            sash_prompt_split=620,
            sash_queue=286,
            sash_log=150,
            sash_log_drop_split=604,
            root_width=1100,
            root_height=900,
        )
        self.assertFalse(changed_sash)
        self.assertEqual(sash["sash_dropzone"], 500)
        self.assertEqual(sash["sash_prompt_split"], 620)
        self.assertEqual(sash["sash_queue"], 286)
        self.assertEqual(sash["sash_log"], 150)
        self.assertEqual(sash["sash_log_drop_split"], 604)

        # And a too-small log_drop_split (drop zone too WIDE) gets clamped up
        # into the band — this is the recurring "drop zone too wide" fix.
        wide_drop, wide_changed = sanitize_sash_layout(
            sash_dropzone=500,
            sash_prompt_split=620,
            sash_queue=286,
            sash_log=150,
            sash_log_drop_split=400,  # would make drop zone 814-400=414px wide
            root_width=1100,
            root_height=900,
        )
        self.assertTrue(wide_changed)
        self.assertGreaterEqual(wide_drop["sash_log_drop_split"], 564)
        self.assertLessEqual(814 - wide_drop["sash_log_drop_split"], 250)


class ParseGeometrySizeTests(unittest.TestCase):
    """Guards on the geometry parser used by main_window's pre-sash clamp.

    Bug fixed 2026-05-20: the pre-sash sanitize was using
    ``sanitized_window["width"]`` (the ui_config default of 1100)
    instead of the ACTUAL width the window was about to open at
    (saved geometry, e.g. 1331). That clamped saved sash positions
    down to fit 1100 px, then ``_persist_layout_corrections_if_needed``
    flushed the clamped values back to disk — the user's actual
    layout was permanently lost on every relaunch.
    """

    def test_parses_geometry_with_position(self):
        self.assertEqual(parse_geometry_size("1331x950+97+52", 1100, 950), (1331, 950))

    def test_parses_geometry_without_position(self):
        self.assertEqual(parse_geometry_size("1280x720", 800, 600), (1280, 720))

    def test_empty_string_falls_back(self):
        self.assertEqual(parse_geometry_size("", 1100, 950), (1100, 950))

    def test_malformed_falls_back(self):
        self.assertEqual(parse_geometry_size("garbage", 1100, 950), (1100, 950))
        self.assertEqual(parse_geometry_size("1280", 800, 600), (800, 600))

    def test_non_string_falls_back(self):
        self.assertEqual(parse_geometry_size(None, 1100, 950), (1100, 950))  # type: ignore[arg-type]


class PreSashClampUsesActualGeometryTests(unittest.TestCase):
    """End-to-end regression for the 2026-05-20 sash-bug fix.

    Mimics the bug scenario: window_geometry="1331x950+97+52" (saved
    from a wide window) + ui_config["window"]["width"]=1100 (the
    untouched default). Asserts that when we pass the geometry-parsed
    width to sanitize_sash_layout, the user's saved sash_queue=417
    survives — which it does NOT when given ui_config's 1100 because
    the 22-32% range at 1100w caps queue at 352.
    """

    def test_pre_sash_with_geometry_width_preserves_user_choice(self):
        """v5.3: clamp widened to physical usability floors (200px) so
        the user's sash positions survive regardless of which width
        the clamp is applied against. Previously the pre-sash clamp
        used the ui_config width (1100) which produced a 32% ceiling
        of ~352, capping the user's 417 DOWN. The fix that landed in
        2026-05-20 was to use the saved-geometry width (1331); v5.3
        makes the ranges so wide that BOTH widths preserve the user's
        choice — closing the regression at the source.
        """
        saved_geometry = "1331x950+97+52"
        ui_config_width = 1100
        ui_config_height = 950
        user_saved_sashes = dict(
            sash_dropzone=560, sash_prompt_split=704, sash_queue=417,
            sash_log=167, sash_log_drop_split=613,
        )

        # Both paths now preserve the user's value (clamp is generous).
        old_result, _ = sanitize_sash_layout(
            **user_saved_sashes,
            root_width=ui_config_width, root_height=ui_config_height,
        )
        self.assertEqual(old_result["sash_queue"], 417, (
            "v5.3 clamp ceiling is 50% of width; at 1100w the cap is "
            "550, so 417 survives. (Previously the 32% cap forced "
            "this down to ~352.)"
        ))

        actual_w, actual_h = parse_geometry_size(
            saved_geometry, ui_config_width, ui_config_height,
        )
        new_result, _ = sanitize_sash_layout(
            **user_saved_sashes,
            root_width=actual_w, root_height=actual_h,
        )
        self.assertEqual(new_result["sash_queue"], 417, (
            "User's saved sash_queue=417 must survive at actual width "
            "1331 too (50% cap = 665)."
        ))


if __name__ == "__main__":
    unittest.main()
