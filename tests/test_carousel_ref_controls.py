import os
import tempfile
import unittest
from types import SimpleNamespace
from unittest import mock

from kling_gui.carousel_widget import ImageCarousel
from kling_gui.image_state import ImageSession


class _FakeButton:
    def __init__(self):
        self.calls = []

    def config(self, **kwargs):
        self.calls.append(kwargs)


class _FakeCanvas:
    def delete(self, *_args, **_kwargs):
        return None

    def winfo_width(self):
        return 400

    def winfo_height(self):
        return 300

    def create_text(self, *_args, **_kwargs):
        return None

    def create_image(self, *_args, **_kwargs):
        return None


class CarouselRefControlsTests(unittest.TestCase):
    def test_ref_and_compare_config_called_with_stable_widths(self):
        tab = ImageCarousel.__new__(ImageCarousel)
        tab.canvas = _FakeCanvas()
        tab.remove_btn = _FakeButton()
        tab.compare_btn = _FakeButton()
        tab.prev_btn = _FakeButton()
        tab.next_btn = _FakeButton()
        tab._ref_btn = _FakeButton()
        tab.counter_label = _FakeButton()
        tab.info_label = _FakeButton()
        tab.meta_label = _FakeButton()
        tab.sim_label = _FakeButton()
        tab.fas_label = _FakeButton()
        tab._show_image_on_canvas = lambda *_args, **_kwargs: None

        with tempfile.TemporaryDirectory() as tmpdir:
            image_path = os.path.join(tmpdir, "front.png")
            with open(image_path, "wb") as handle:
                handle.write(b"x")
            session = ImageSession()
            session.add_image(image_path, "input", make_active=True)
            tab.image_session = session
            tab._update_panel()

        self.assertTrue(any("text" in call and call["text"] in {"★ Ref", "★ Clear"} for call in tab._ref_btn.calls))
        self.assertTrue(any("state" in call for call in tab.compare_btn.calls))

    def test_ref_button_lights_when_active_image_is_effective_ref(self):
        tab = ImageCarousel.__new__(ImageCarousel)
        tab.canvas = _FakeCanvas()
        tab.remove_btn = _FakeButton()
        tab.compare_btn = _FakeButton()
        tab.prev_btn = _FakeButton()
        tab.next_btn = _FakeButton()
        tab._ref_btn = _FakeButton()
        tab.counter_label = _FakeButton()
        tab.info_label = _FakeButton()
        tab.meta_label = _FakeButton()
        tab.sim_label = _FakeButton()
        tab.fas_label = _FakeButton()
        tab._show_image_on_canvas = lambda *_args, **_kwargs: None

        with tempfile.TemporaryDirectory() as tmpdir:
            image_path = os.path.join(tmpdir, "front.png")
            with open(image_path, "wb") as handle:
                handle.write(b"x")
            session = ImageSession()
            session.add_image(image_path, "input", make_active=True)
            tab.image_session = session
            tab._update_panel()

        has_ref = any(call.get("text") == "★ Ref" for call in tab._ref_btn.calls)
        has_active_color = any(call.get("bg") == "#E5C100" for call in tab._ref_btn.calls)
        self.assertTrue(has_ref)
        self.assertTrue(has_active_color)

    def test_toggle_ref_sets_and_clears_manual_ref(self):
        tab = ImageCarousel.__new__(ImageCarousel)
        tab.log = mock.Mock()
        tab._calc_all_similarity = mock.Mock()
        session = ImageSession()
        with tempfile.TemporaryDirectory() as tmpdir:
            p1 = os.path.join(tmpdir, "a.png")
            p2 = os.path.join(tmpdir, "b.png")
            open(p1, "wb").close()
            open(p2, "wb").close()
            session.add_image(p1, "input", make_active=True)
            session.add_image(p2, "input", make_active=True)
            tab.image_session = session

            tab._toggle_sim_ref()
            self.assertEqual(session.similarity_ref_index, session.current_index)

            tab._toggle_sim_ref()
            self.assertEqual(session.similarity_ref_index, -1)

    def test_calc_all_similarity_no_name_error_on_ref_only(self):
        tab = ImageCarousel.__new__(ImageCarousel)
        tab._sim_lock = mock.Mock()
        tab._sim_busy = False
        tab.after = lambda *_args, **_kwargs: None
        tab._sim_log = lambda *_args, **_kwargs: None
        tab.image_session = ImageSession()

        with tempfile.TemporaryDirectory() as tmpdir:
            p1 = os.path.join(tmpdir, "front.png")
            open(p1, "wb").close()
            tab.image_session.add_image(p1, "input", make_active=True)
            tab._calc_all_similarity(reason="test")

    def test_show_image_on_canvas_passes_canvas_as_photoimage_master(self):
        try:
            from PIL import Image
        except Exception as exc:  # pragma: no cover - defensive skip
            self.skipTest(f"Pillow unavailable: {exc}")

        tab = ImageCarousel.__new__(ImageCarousel)
        tab.log = mock.Mock()
        tab.image_session = SimpleNamespace(active_entry=None)
        canvas = _FakeCanvas()

        with tempfile.TemporaryDirectory() as tmpdir:
            image_path = os.path.join(tmpdir, "front.jpeg")
            Image.new("RGB", (32, 24), color="red").save(image_path, "JPEG")
            with mock.patch("PIL.ImageTk.PhotoImage", return_value=object()) as photo_ctor:
                ok = tab._show_image_on_canvas(canvas, image_path, "_photo")
            self.assertTrue(ok)
            self.assertTrue(photo_ctor.called)
            self.assertIs(photo_ctor.call_args.kwargs.get("master"), canvas)

    def test_on_add_image_skips_and_logs_when_preflight_fails(self):
        tab = ImageCarousel.__new__(ImageCarousel)
        tab.image_session = mock.Mock()
        tab.log = mock.Mock()
        tab.winfo_toplevel = lambda: None

        with mock.patch("kling_gui.carousel_widget.select_open_files", return_value=["C:/tmp/front.jpeg"]):
            with mock.patch("kling_gui.carousel_widget.preflight_image_path", return_value=(False, "bad image")):
                tab._on_add_image()

        tab.image_session.add_image.assert_not_called()
        messages = [call.args[0] for call in tab.log.call_args_list]
        self.assertTrue(any("Skipped carousel add" in msg for msg in messages))
        self.assertFalse(any("Added to carousel" in msg for msg in messages))


class CarouselFasSummaryTests(unittest.TestCase):
    """Coverage for ImageCarousel._fas_summary_from_diag — the Processing Log
    string that summarizes liveness per side.

    Regression test for coderabbit Major finding on PR #19: the prior
    implementation read raw ref_score / target_score (the antispoof_score
    whose meaning depends on is_real) and rendered them as "% real"
    unconditionally — so a spoofed image with antispoof_score=0.99 logged
    as "99% real". After the fix we use ref_real_conf / target_real_conf
    which the engine has already inverted when needed.
    """

    def test_spoofed_side_logs_low_real_percentage(self) -> None:
        diag = {"anti_spoofing": {
            "ref": {
                "status": "ok",
                "spoof_detected": True,
                "faces": [{"is_real": False, "antispoof_score": 0.99}],
            },
            "target": {
                "status": "ok",
                "spoof_detected": False,
                "faces": [{"is_real": True, "antispoof_score": 0.97}],
            },
        }}
        result = ImageCarousel._fas_summary_from_diag(diag)
        # ref was 99% confident SPOOF → real_conf ≈ 0.01 → "1.0% real"
        self.assertIn("ref=1.0% real", result)
        # target was 97% confident REAL → real_conf ≈ 0.97 → "97.0% real"
        self.assertIn("target=97.0% real", result)
        self.assertIn("liveness=advisory", result)

    def test_both_real_logs_high_real_percentages(self):
        diag = {"anti_spoofing": {
            "ref": {
                "status": "ok",
                "spoof_detected": False,
                "faces": [{"is_real": True, "antispoof_score": 0.95}],
            },
            "target": {
                "status": "ok",
                "spoof_detected": False,
                "faces": [{"is_real": True, "antispoof_score": 0.92}],
            },
        }}
        result = ImageCarousel._fas_summary_from_diag(diag)
        self.assertIn("ref=95.0% real", result)
        self.assertIn("target=92.0% real", result)
        self.assertIn("liveness=PASS", result)

    def test_unavailable_returns_empty_string(self):
        self.assertEqual(ImageCarousel._fas_summary_from_diag(None), "")
        self.assertEqual(ImageCarousel._fas_summary_from_diag({}), "")


if __name__ == "__main__":
    unittest.main()
