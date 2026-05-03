import types
import unittest
from unittest import mock

import numpy as np

import similarity_engine as se


class SimilarityEngineTests(unittest.TestCase):
    def test_compare_images_uses_best_target_face_match(self):
        engine = se.FaceEngine()
        faces1 = [{"face": "src_face", "facial_area": {"w": 10, "h": 10}}]
        faces2 = [
            {"face": "wrong_face", "facial_area": {"w": 10, "h": 10}},
            {"face": "right_face", "facial_area": {"w": 9, "h": 9}},
        ]

        def fake_represent(face_crop):
            if face_crop == "src_face":
                return np.asarray([1.0, 0.0], dtype=float)
            if face_crop == "right_face":
                return np.asarray([1.0, 0.0], dtype=float)
            return np.asarray([0.0, 1.0], dtype=float)

        with mock.patch.object(engine, "validate_image_file", return_value=None), \
            mock.patch.object(se.DeepFace, "extract_faces", side_effect=[faces1, faces2]), \
            mock.patch.object(engine, "_represent_face", side_effect=fake_represent):
            result = engine.compare_images("source.png", "target.png")

        self.assertIsNone(result["error"])
        self.assertGreaterEqual(result["score"], 99.0)
        self.assertTrue(result["match"])

    def test_compare_images_no_face_returns_error(self):
        engine = se.FaceEngine()
        with mock.patch.object(engine, "validate_image_file", return_value=None), \
            mock.patch.object(se.DeepFace, "extract_faces", side_effect=[[], []]):
            result = engine.compare_images("source.png", "target.png")

        self.assertIsNotNone(result["error"])
        self.assertEqual(result["score"], 0.0)
        self.assertFalse(result["match"])

    def test_compare_images_backend_runtime_error_falls_back_to_opencv(self):
        engine = se.FaceEngine()
        with mock.patch.object(engine, "validate_image_file", return_value=None), \
            mock.patch.object(
                se.DeepFace,
                "extract_faces",
                side_effect=ValueError("A KerasTensor cannot be used as input to a TensorFlow function"),
            ), \
            mock.patch.object(engine, "extract_face", return_value=0.9), \
            mock.patch.object(
                engine,
                "_represent_face",
                side_effect=[
                    np.asarray([1.0, 0.0], dtype=float),
                    np.asarray([1.0, 0.0], dtype=float),
                ],
            ):
            result = engine.compare_images("source.png", "target.png")

        self.assertIsNone(result["error"])
        self.assertTrue(result["match"])
        self.assertGreaterEqual(result["score"], 99.0)


if __name__ == "__main__":
    unittest.main()
