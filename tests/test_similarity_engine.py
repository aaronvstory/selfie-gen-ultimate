import unittest
from unittest import mock

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
            mock.patch.object(engine, "_compare_full_image_align", return_value=full), \
            mock.patch.object(engine, "_compare_existing", return_value=existing):
            result = engine.compare_images("a.png", "b.png")

        self.assertIsNone(result["error"])
        self.assertAlmostEqual(result["score"], 92.5)
        self.assertTrue(result["match"])
        self.assertEqual(result["diagnostics"]["mode"], engine.MODE_NORMALIZED_CROP)
        self.assertEqual(len(result["diagnostics"]["mode_results"]), 3)

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
            mock.patch.object(engine, "_compare_existing", side_effect=ValueError("existing failed")):
            result = engine.compare_images("a.png", "b.png")

        self.assertIsNone(result["error"])
        self.assertEqual(result["diagnostics"]["mode"], engine.MODE_FULL_IMAGE_ALIGN)
        errors = [x for x in result["diagnostics"]["mode_results"] if x.get("error")]
        self.assertTrue(any(e["mode"] == engine.MODE_NORMALIZED_CROP for e in errors))

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

    def test_select_prominent_face_is_deterministic_on_area_tie(self):
        engine = se.FaceEngine()
        faces = [
            {"facial_area": {"x": 80, "y": 80, "w": 40, "h": 40}, "confidence": 0.95},
            {"facial_area": {"x": 20, "y": 20, "w": 40, "h": 40}, "confidence": 0.90},
        ]
        chosen = engine._select_prominent_face(faces, "img", image_shape=(200, 200))
        self.assertEqual(chosen["facial_area"]["x"], 80)


if __name__ == "__main__":
    unittest.main()
