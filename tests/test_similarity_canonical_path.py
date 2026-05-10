from unittest import mock

import face_similarity as fs


def test_face_similarity_details_exposes_diagnostics():
    fs._ENGINE = None
    fs._ENGINE_ERROR = None

    class _Engine:
        def compare_images(self, _a, _b):
            return {
                "match": True,
                "score": 91.2,
                "error": None,
                "diagnostics": {
                    "mode": "normalized_crop",
                    "model_name": "ArcFace",
                    "detector_backend": "retinaface",
                    "face_counts": {"ref": 1, "target": 1},
                    "selected_face_boxes": {"ref": {"x": 1, "y": 2, "w": 3, "h": 4}, "target": {"x": 5, "y": 6, "w": 7, "h": 8}},
                    "selected_face_confidence": {"ref": 0.9, "target": 0.8},
                    "crop_dimensions": {"ref": {"normalized_w": 224, "normalized_h": 224}, "target": {"normalized_w": 224, "normalized_h": 224}},
                    "raw_cosine_distance": 0.11,
                    "fallback_reason": None,
                },
            }

    with mock.patch("face_similarity._get_engine", return_value=_Engine()):
        details = fs.compute_face_similarity_details("a.png", "b.png")

    assert details["score"] == 91
    assert details["pass"] is True
    assert details["diagnostics"]["mode"] == "normalized_crop"
    assert details["diagnostics"]["raw_cosine_distance"] == 0.11


def test_standalone_engine_is_canonical_shim():
    import similarity.src.engine as shim
    import similarity_engine as canonical

    assert shim.FaceEngine is canonical.FaceEngine
