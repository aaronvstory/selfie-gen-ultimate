import unittest
from unittest import mock

import numpy as np

import similarity_engine as se


class SimilarityEngineTests(unittest.TestCase):
    def setUp(self):
        se.FaceEngine._instance = None

    def test_compare_images_prefers_normalized_mode_and_has_diagnostics(self):
        engine = se.FaceEngine()

        norm = {
            "mode": engine.MODE_NORMALIZED_CROP,
            "score": 92.5,
            "match": True,
            "error": None,
            "diagnostics": engine._base_mode_diag(engine.MODE_NORMALIZED_CROP, "a.png", "b.png"),
        }
        norm["diagnostics"]["face_counts"] = {"ref": 1, "target": 1}

        full = {
            "mode": engine.MODE_FULL_IMAGE_ALIGN,
            "score": 90.0,
            "match": True,
            "error": None,
            "diagnostics": engine._base_mode_diag(engine.MODE_FULL_IMAGE_ALIGN, "a.png", "b.png"),
        }

        existing = {
            "mode": engine.MODE_EXISTING,
            "score": 88.0,
            "match": True,
            "error": None,
            "diagnostics": engine._base_mode_diag(engine.MODE_EXISTING, "a.png", "b.png"),
        }

        with mock.patch.object(engine, "validate_image_file", return_value=None), \
            mock.patch.object(engine, "_compare_normalized_crop", return_value=norm), \
            mock.patch.object(engine, "_compare_full_image_align", return_value=full) as full_cmp, \
            mock.patch.object(engine, "_compare_existing", return_value=existing) as exist_cmp:
            result = engine.compare_images("a.png", "b.png")

        self.assertIsNone(result["error"])
        self.assertAlmostEqual(result["score"], 92.5)
        self.assertTrue(result["match"])
        self.assertEqual(result["diagnostics"]["mode"], engine.MODE_NORMALIZED_CROP)
        self.assertEqual(len(result["diagnostics"]["mode_results"]), 1)
        self.assertFalse(full_cmp.called)
        self.assertFalse(exist_cmp.called)

    def test_compare_images_falls_back_to_full_image_when_normalized_fails(self):
        engine = se.FaceEngine()

        full = {
            "mode": engine.MODE_FULL_IMAGE_ALIGN,
            "score": 91.0,
            "match": True,
            "error": None,
            "diagnostics": engine._base_mode_diag(engine.MODE_FULL_IMAGE_ALIGN, "a.png", "b.png"),
        }

        with mock.patch.object(engine, "validate_image_file", return_value=None), \
            mock.patch.object(engine, "_compare_normalized_crop", side_effect=ValueError("normalized failed")), \
            mock.patch.object(engine, "_compare_full_image_align", return_value=full), \
            mock.patch.object(engine, "_compare_existing", side_effect=ValueError("existing failed")) as exist_cmp:
            result = engine.compare_images("a.png", "b.png")

        self.assertIsNone(result["error"])
        self.assertEqual(result["diagnostics"]["mode"], engine.MODE_FULL_IMAGE_ALIGN)
        errors = [x for x in result["diagnostics"]["mode_results"] if x.get("error")]
        self.assertTrue(any(e["mode"] == engine.MODE_NORMALIZED_CROP for e in errors))
        self.assertEqual(len(result["diagnostics"]["mode_results"]), 2)
        self.assertFalse(exist_cmp.called)

    def test_compare_images_backend_runtime_error_uses_opencv_fallback(self):
        engine = se.FaceEngine()

        fallback = {
            "mode": engine.MODE_FALLBACK,
            "score": 95.0,
            "match": True,
            "error": None,
            "diagnostics": engine._base_mode_diag(engine.MODE_FALLBACK, "a.png", "b.png"),
        }

        runtime_err = ValueError("A KerasTensor cannot be used as input to a TensorFlow function")

        with mock.patch.object(engine, "validate_image_file", return_value=None), \
            mock.patch.object(engine, "_compare_normalized_crop", side_effect=runtime_err), \
            mock.patch.object(engine, "_compare_full_image_align", side_effect=runtime_err), \
            mock.patch.object(engine, "_compare_existing", side_effect=runtime_err), \
            mock.patch.object(engine, "_compare_with_opencv_fallback", return_value=fallback) as fallback_mock:
            result = engine.compare_images("a.png", "b.png")

        self.assertIsNone(result["error"])
        self.assertEqual(result["diagnostics"]["mode"], engine.MODE_FALLBACK)
        self.assertTrue(fallback_mock.called)

    def test_compare_images_diagnostic_matrix_runs_all_modes_after_success(self):
        engine = se.FaceEngine()
        norm = {
            "mode": engine.MODE_NORMALIZED_CROP,
            "score": 92.5,
            "match": True,
            "error": None,
            "diagnostics": engine._base_mode_diag(engine.MODE_NORMALIZED_CROP, "a.png", "b.png"),
        }
        full = {
            "mode": engine.MODE_FULL_IMAGE_ALIGN,
            "score": 90.0,
            "match": True,
            "error": None,
            "diagnostics": engine._base_mode_diag(engine.MODE_FULL_IMAGE_ALIGN, "a.png", "b.png"),
        }
        existing = {
            "mode": engine.MODE_EXISTING,
            "score": 89.0,
            "match": True,
            "error": None,
            "diagnostics": engine._base_mode_diag(engine.MODE_EXISTING, "a.png", "b.png"),
        }
        with mock.patch.object(engine, "validate_image_file", return_value=None), \
            mock.patch.object(engine, "_compare_normalized_crop", return_value=norm), \
            mock.patch.object(engine, "_compare_full_image_align", return_value=full), \
            mock.patch.object(engine, "_compare_existing", return_value=existing):
            result = engine.compare_images("a.png", "b.png", diagnostic_matrix=True)

        self.assertIsNone(result["error"])
        self.assertEqual(result["diagnostics"]["mode"], engine.MODE_NORMALIZED_CROP)
        self.assertEqual(len(result["diagnostics"]["mode_results"]), 3)

    def test_select_prominent_face_is_deterministic_on_area_tie(self):
        engine = se.FaceEngine()
        faces = [
            {"facial_area": {"x": 80, "y": 80, "w": 40, "h": 40}, "confidence": 0.95},
            {"facial_area": {"x": 20, "y": 20, "w": 40, "h": 40}, "confidence": 0.90},
        ]
        chosen = engine._select_prominent_face(faces, "img", image_shape=(200, 200))
        self.assertEqual(chosen["facial_area"]["x"], 80)

    def test_compare_full_image_align_passes_real_image_shape_to_selector(self):
        engine = se.FaceEngine()
        reps1 = [{"embedding": [1.0, 0.0], "facial_area": {"x": 1, "y": 2, "w": 10, "h": 12}}]
        reps2 = [{"embedding": [1.0, 0.0], "facial_area": {"x": 3, "y": 4, "w": 8, "h": 9}}]

        with mock.patch.object(engine, "_represent_full_image_with_detection", side_effect=[reps1, reps2]), \
            mock.patch("similarity_engine.cv2.imread", side_effect=[np.zeros((200, 300, 3), dtype=np.uint8), np.zeros((120, 220, 3), dtype=np.uint8)]), \
            mock.patch.object(engine, "_select_prominent_face", side_effect=[reps1[0], reps2[0]]) as selector:
            result = engine._compare_full_image_align("a.png", "b.png")

        self.assertIsNone(result["error"])
        self.assertEqual(selector.call_args_list[0].args[2], (300, 200))
        self.assertEqual(selector.call_args_list[1].args[2], (220, 120))

    def test_compare_existing_skips_failed_target_embedding_and_uses_valid_face(self):
        engine = se.FaceEngine()
        faces1 = [{"face": "src", "facial_area": {"x": 0, "y": 0, "w": 10, "h": 10}}]
        faces2 = [
            {"face": "bad-target", "facial_area": {"x": 0, "y": 0, "w": 8, "h": 8}},
            {"face": "good-target", "facial_area": {"x": 2, "y": 2, "w": 8, "h": 8}},
        ]

        def _fake_repr(face):
            if face == "src":
                return np.asarray([1.0, 0.0], dtype=float)
            if face == "bad-target":
                raise ValueError("bad crop")
            return np.asarray([1.0, 0.0], dtype=float)

        with mock.patch.object(engine, "_extract_faces", side_effect=[faces1, faces2]), \
            mock.patch("similarity_engine.cv2.imread", side_effect=[np.zeros((100, 100, 3), dtype=np.uint8), np.zeros((120, 120, 3), dtype=np.uint8)]), \
            mock.patch.object(engine, "_represent_face", side_effect=_fake_repr):
            result = engine._compare_existing("a.png", "b.png")

        self.assertIsNone(result["error"])
        self.assertTrue(result["match"])
        self.assertGreaterEqual(result["score"], 99.0)
        self.assertEqual(result["diagnostics"]["selected_face_boxes"]["target"]["x"], 2)

    def test_score_from_distance_guards_threshold_out_of_range(self):
        engine = se.FaceEngine()
        original = engine.threshold
        try:
            engine.threshold = 0.0
            score_low, match_low = engine._score_from_distance(0.1)
            self.assertIsInstance(score_low, float)
            self.assertIsInstance(match_low, bool)

            engine.threshold = 1.0
            score_high, match_high = engine._score_from_distance(0.9)
            self.assertIsInstance(score_high, float)
            self.assertIsInstance(match_high, bool)
        finally:
            engine.threshold = original

    def test_class_level_defaults_available_without_init(self):
        raw = object.__new__(se.FaceEngine)
        self.assertEqual(raw.model_name, "ArcFace")
        self.assertEqual(raw.detector_backend, "retinaface")
        self.assertEqual(raw.threshold, 0.68)
        self.assertEqual(raw.normalized_face_size, (224, 224))
        self.assertEqual(raw.normalized_face_padding, 0.30)
        self.assertEqual(raw.MODE_EXISTING, "existing")


if __name__ == "__main__":
    unittest.main()
