import os
import os
import tempfile
import unittest

from kling_gui.image_state import ImageEntry, ImageSession
from kling_gui.tabs.expand_tab import ExpandTab


class CanonicalSimilarityReferenceTests(unittest.TestCase):
    def _touch(self, folder: str, name: str) -> str:
        path = os.path.join(folder, name)
        with open(path, "wb") as handle:
            handle.write(b"x")
        return path

    def test_prefers_newest_crop_then_newest_front_then_first_input(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            plain = self._touch(tmpdir, "plain_input.png")
            front = self._touch(tmpdir, "front_image.png")
            crop = self._touch(tmpdir, "subject_crop.jpg")
            selfie = self._touch(tmpdir, "selfie_sim80_001.png")

            session = ImageSession()
            session.add_image(plain, "input")
            session.add_image(front, "input")
            session.add_image(crop, "input")
            session.add_image(selfie, "selfie")

            self.assertEqual(session.effective_similarity_ref_entry.path, crop)

            os.remove(crop)
            self.assertEqual(session.effective_similarity_ref_entry.path, front)

            os.remove(front)
            self.assertEqual(session.effective_similarity_ref_entry.path, plain)

    def test_falls_back_to_first_existing_input(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            first = self._touch(tmpdir, "a.png")
            second = self._touch(tmpdir, "b.png")
            session = ImageSession()
            session.add_image(first, "input")
            session.add_image(second, "input")
            self.assertEqual(session.effective_similarity_ref_entry.path, first)

    def test_respects_similarity_ref_over_fallback(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            plain = self._touch(tmpdir, "plain.png")
            crop_old = self._touch(tmpdir, "old_crop.jpg")
            front_new = self._touch(tmpdir, "front_new.png")
            selfie = self._touch(tmpdir, "selfie_sim88_001.png")
            session = ImageSession()
            session.add_image(plain, "input")
            session.add_image(crop_old, "input")
            session.add_image(front_new, "input")
            session.add_image(selfie, "selfie")
            
            # Before explicit ref, should pick newest crop
            self.assertEqual(session.effective_similarity_ref_entry.path, crop_old)
            
            # Explicitly set ref to selfie
            session.set_similarity_ref(session.count - 1)  # selfie (non-input)
            self.assertEqual(session.effective_similarity_ref_entry.path, selfie)
            
            # Clear explicit ref, should go back to fallback
            session.set_similarity_ref(-1)
            self.assertEqual(session.effective_similarity_ref_entry.path, crop_old)

    def test_effective_ref_source_key_transitions(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            crop = self._touch(tmpdir, "subject_crop.jpg")
            front = self._touch(tmpdir, "front.png")
            selfie = self._touch(tmpdir, "selfie_sim88_001.png")
            session = ImageSession()
            session.add_image(front, "input")
            session.add_image(crop, "input")
            session.add_image(selfie, "selfie")

            ref_entry, source_key = session.get_effective_similarity_ref()
            self.assertIsNotNone(ref_entry)
            self.assertEqual(ref_entry.path, crop)
            self.assertEqual(source_key, "auto_crop")

            session.set_similarity_ref(session.count - 1)
            ref_entry, source_key = session.get_effective_similarity_ref()
            self.assertIsNotNone(ref_entry)
            self.assertEqual(ref_entry.path, selfie)
            self.assertEqual(source_key, "manual_star_ref")

            session.set_similarity_ref(-1)
            ref_entry, source_key = session.get_effective_similarity_ref()
            self.assertIsNotNone(ref_entry)
            self.assertEqual(ref_entry.path, crop)
            self.assertEqual(source_key, "auto_crop")


class Expand25CandidateCompositionTests(unittest.TestCase):
    def _entry(self, folder: str, name: str, source_type: str):
        path = os.path.join(folder, name)
        with open(path, "wb") as handle:
            handle.write(b"x")
        return ImageEntry(path=path, source_type=source_type)

    def test_includes_active_non_selfie_once(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            selfie = self._entry(tmpdir, "selfie_sim81_001.png", "selfie")
            active_input = self._entry(tmpdir, "front_crop.jpg", "input")
            entries = ExpandTab.compose_candidate_entries([selfie], active_input)
            self.assertEqual([e.path for e in entries], [selfie.path, active_input.path])

    def test_dedupes_when_active_already_in_selfie_list(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            selfie = self._entry(tmpdir, "selfie_sim85_001.png", "selfie")
            entries = ExpandTab.compose_candidate_entries([selfie], selfie)
            self.assertEqual([e.path for e in entries], [selfie.path])


class Expand25SimilarityLabelTests(unittest.TestCase):
    def test_expand_complete_uses_na_when_score_missing(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            result_path = os.path.join(tmpdir, "expanded.png")
            with open(result_path, "wb") as handle:
                handle.write(b"x")

            tab = ExpandTab.__new__(ExpandTab)
            tab.image_session = ImageSession()
            tab._expanded_paths = []
            tab._refresh_expanded_list = lambda: None
            tab.log = lambda *_args, **_kwargs: None

            tab._on_single_expand_complete(
                source_entry=None,
                result_path=result_path,
                score=None,
                passed=None,
                ops={},
            )

            entry = tab.image_session.active_entry
            self.assertIsNotNone(entry)
            self.assertEqual(entry.similarity, "n/a")
            self.assertIsNone(entry.similarity_score)


class Expand25ConfigTests(unittest.TestCase):
    class _FakeGetVar:
        def __init__(self, value):
            self._value = value

        def get(self):
            return self._value

    def test_get_config_updates_includes_composite_mode(self):
        tab = ExpandTab.__new__(ExpandTab)
        tab._auto_switch_var = self._FakeGetVar(True)
        tab._expand_mode_var = self._FakeGetVar("percentage")
        tab._pct_var = self._FakeGetVar(30)
        tab._top_var = self._FakeGetVar(100)
        tab._bottom_var = self._FakeGetVar(100)
        tab._left_var = self._FakeGetVar(120)
        tab._right_var = self._FakeGetVar(120)
        tab._format_var = self._FakeGetVar("png")
        tab._composite_mode_var = self._FakeGetVar("preserve_seamless")
        tab._provider_var = self._FakeGetVar("bfl")

        updates = tab.get_config_updates()
        self.assertEqual(updates["outpaint_composite_mode"], "preserve_seamless")
        self.assertEqual(updates["automation_selfie_expand_composite_mode"], "preserve_seamless")


class OutpaintRouteWiringTests(unittest.TestCase):
    def test_step0_and_step25_call_shared_outpaint_with_margins_and_mode(self):
        root = os.path.dirname(os.path.dirname(__file__))
        step0_path = os.path.join(root, "kling_gui", "tabs", "face_crop_tab.py")
        step25_path = os.path.join(root, "kling_gui", "tabs", "expand_tab.py")

        with open(step0_path, "r", encoding="utf-8") as handle:
            step0_text = handle.read()
        with open(step25_path, "r", encoding="utf-8") as handle:
            step25_text = handle.read()

        for text in (step0_text, step25_text):
            self.assertIn("gen.outpaint(", text)
            self.assertIn("expand_left=", text)
            self.assertIn("expand_right=", text)
            self.assertIn("expand_top=", text)
            self.assertIn("expand_bottom=", text)
            self.assertIn("composite_mode=", text)


if __name__ == "__main__":
    unittest.main()
