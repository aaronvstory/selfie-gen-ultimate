from __future__ import annotations

import importlib
import sys
import types
import unittest
from typing import Any, ClassVar
from unittest.mock import patch


class _DeepFaceRecorder:
    extract_calls: ClassVar[list[dict[str, Any]]] = []
    represent_calls: ClassVar[list[dict[str, Any]]] = []

    @classmethod
    def reset(cls) -> None:
        cls.extract_calls = []
        cls.represent_calls = []

    @staticmethod
    def build_model(model_name: str):
        return model_name

    @classmethod
    def extract_faces(cls, **kwargs):
        cls.extract_calls.append(kwargs)
        if kwargs["img_path"] == "image-a.jpg":
            return [
                {"face": "small-a", "facial_area": {"w": 10, "h": 10}},
                {"face": "large-a", "facial_area": {"w": 30, "h": 25}},
            ]
        if kwargs["img_path"] == "image-b.jpg":
            return [
                {"face": "small-b", "facial_area": {"w": 12, "h": 12}},
                {"face": "large-b", "facial_area": {"w": 40, "h": 20}},
            ]
        raise AssertionError(f"Unexpected image path: {kwargs['img_path']}")

    # v1.8 normalized-crop mode passes a numpy ndarray as img_path (the
    # cropped+aligned face), not a stringy key. The engine derives the crop
    # box from the face's w/h, so different faces (large-a 30x25 vs large-b
    # 40x20) yield different ndarray shapes — and the same face compared to
    # itself yields identical shapes. Key embeddings off shape so that
    # `compare_images(a, a)` produces identical embeddings → distance 0 → 100%
    # while `compare_images(a, b)` produces distinct embeddings.
    @classmethod
    def represent(cls, **kwargs):
        cls.represent_calls.append(kwargs)
        string_keyed = {
            "large-a": [1.0, 0.0, 0.0],
            "large-b": [0.8, 0.6, 0.0],
            "image-a.jpg": [1.0, 0.0, 0.0],
            "image-b.jpg": [0.8, 0.6, 0.0],
        }
        img = kwargs.get("img_path")
        if isinstance(img, str) and img in string_keyed:
            return [{"embedding": string_keyed[img]}]
        if hasattr(img, "shape"):
            # Engine padding is 0.30; for the test stubs above (cvtColor and
            # resize are no-ops, source image is 256x256), large-a (w=30,h=25)
            # produces a (33, 39, 3) crop and large-b (w=40,h=20) produces a
            # (26, 52, 3) crop. Map those shapes to the original ref/target
            # embeddings; equal shapes → identical embedding (so a→a scores 100).
            shape = tuple(int(d) for d in img.shape)
            if shape == (33, 39, 3):  # large-a
                return [{"embedding": [1.0, 0.0, 0.0]}]
            if shape == (26, 52, 3):  # large-b
                return [{"embedding": [0.8, 0.6, 0.0]}]
            # Unknown shape — deterministic fingerprint so equal arrays still
            # map to equal embeddings.
            seed = (hash(shape) % 1000) * 0.001
            return [{"embedding": [1.0, seed, 0.0]}]
        return [{"embedding": [1.0, 0.0, 0.0]}]

def _build_deepface_module() -> types.ModuleType:
    deepface_module = types.ModuleType("deepface")
    deepface_module.DeepFace = _DeepFaceRecorder
    return deepface_module


class TestFaceEngine(unittest.TestCase):
    def setUp(self) -> None:
        # src.engine is a thin shim; the real implementation is similarity_engine
        # at the repo root. If a prior test already imported similarity_engine,
        # `from deepface import DeepFace` binds the *real* DeepFace at module
        # load time, and the sys.modules patch in this setUp won't dislodge it.
        # Pop both modules so re-importing src.engine forces a fresh import of
        # similarity_engine against our deepface/cv2 stubs.
        self._original_engine_module = sys.modules.pop("src.engine", None)
        self._original_similarity_engine_module = sys.modules.pop("similarity_engine", None)
        self.addCleanup(self._restore_engine_module)

        import numpy as np

        cv2_module = types.ModuleType("cv2")
        # Engine code now requires a non-None ndarray from imread (it composes
        # crops and runs preprocessing before handing off to DeepFace). A
        # 256x256 RGB array is large enough that face_bbox crops (whose w/h
        # come from the DeepFace stub: up to 40x25) stay inside bounds, so the
        # normalized_crop mode succeeds on the first attempt and the engine
        # short-circuits without re-running the other modes.
        _stub_image = np.zeros((256, 256, 3), dtype=np.uint8)
        cv2_module.imread = lambda *_args, **_kwargs: _stub_image.copy()
        cv2_module.imwrite = lambda *_args, **_kwargs: True
        cv2_module.resize = lambda image, *_args, **_kwargs: image
        cv2_module.cvtColor = lambda image, *_args, **_kwargs: image
        cv2_module.COLOR_BGR2RGB = 4
        cv2_module.COLOR_RGB2BGR = 3
        cv2_module.INTER_LINEAR = 1
        cv2_module.INTER_CUBIC = 2
        cv2_module.INTER_AREA = 3
        cv2_module.dnn = types.SimpleNamespace(
            readNetFromCaffe=lambda *_args, **_kwargs: object(),
            blobFromImage=lambda *_args, **_kwargs: object(),
        )

        deepface_patcher = patch.dict(
            sys.modules,
            {
                "deepface": _build_deepface_module(),
                "cv2": cv2_module,
            },
        )
        deepface_patcher.start()
        self.addCleanup(deepface_patcher.stop)

        self.engine_module = importlib.import_module("src.engine")
        self.engine_module.FaceEngine._instance = None
        _DeepFaceRecorder.reset()

    def _restore_engine_module(self) -> None:
        sys.modules.pop("src.engine", None)
        if self._original_engine_module is not None:
            sys.modules["src.engine"] = self._original_engine_module
        sys.modules.pop("similarity_engine", None)
        if self._original_similarity_engine_module is not None:
            sys.modules["similarity_engine"] = self._original_similarity_engine_module

    def test_compare_images_uses_largest_face_and_embeddings(self) -> None:
        engine = self.engine_module.FaceEngine()
        # v1.8 added ArcFace + Facenet512 ensemble averaging, which would call
        # represent() 4x and confuse the call-order-keyed embedding stub. The
        # behavior under test (largest-face selection + skip-detector embedding)
        # doesn't depend on the ensemble, so disable it for a clean assertion.
        engine.use_ensemble = False

        with patch.object(engine, "validate_image_file"):
            result = engine.compare_images("image-a.jpg", "image-b.jpg")

        self.assertIsNone(result["error"])
        self.assertTrue(result["match"])
        # v1.9 recalibrated easing curve (exponent 0.5): cosine distance 0.2
        # (between [1,0,0] and [0.8,0.6,0]) now maps to ~89.15. v1.8 (exponent
        # 2.5) mapped this to ~99.06; v1.9 spreads AI-edit-typical distances
        # 0.05-0.20 across 95-89% so 99% no longer pegs and obscures variance.
        # See similarity/CLAUDE.md "Key Mathematical Decision".
        self.assertAlmostEqual(result["score"], 89.15, places=2)
        self.assertEqual(len(_DeepFaceRecorder.extract_calls), 2)
        self.assertEqual(len(_DeepFaceRecorder.represent_calls), 2)
        self.assertTrue(
            all(call["detector_backend"] == "skip" for call in _DeepFaceRecorder.represent_calls)
        )

    def test_identical_embeddings_map_to_full_score(self) -> None:
        engine = self.engine_module.FaceEngine()
        engine.use_ensemble = False
        with patch.object(engine, "validate_image_file"):
            result = engine.compare_images("image-a.jpg", "image-a.jpg")

        self.assertIsNone(result["error"])
        self.assertTrue(result["match"])
        self.assertEqual(result["score"], 100.0)

    def test_shutdown_falls_back_when_cancel_futures_is_unsupported(self) -> None:
        engine = self.engine_module.FaceEngine()

        class ExecutorStub:
            def __init__(self) -> None:
                self.calls = []

            def shutdown(self, wait=False, cancel_futures=False):
                self.calls.append((wait, cancel_futures))
                if cancel_futures:
                    raise TypeError("cancel_futures unsupported")

        executor = ExecutorStub()
        engine._executor = executor

        engine.shutdown()

        self.assertEqual(executor.calls, [(False, True), (False, False)])
        self.assertIsNone(engine._executor)


if __name__ == "__main__":
    unittest.main()
