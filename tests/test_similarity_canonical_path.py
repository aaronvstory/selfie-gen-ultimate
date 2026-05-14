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


def test_face_similarity_details_passes_through_per_model_distances():
    fs._ENGINE = None
    fs._ENGINE_ERROR = None

    class _Engine:
        def compare_images(self, _a, _b):
            return {
                "match": True,
                "score": 88.0,
                "error": None,
                "diagnostics": {
                    "mode": "normalized_crop",
                    "model_name": "ArcFace",
                    "detector_backend": "retinaface",
                    "face_counts": {"ref": 1, "target": 1},
                    "selected_face_boxes": {"ref": None, "target": None},
                    "selected_face_confidence": {"ref": None, "target": None},
                    "crop_dimensions": {"ref": None, "target": None},
                    "raw_cosine_distance": 0.21,
                    "per_model_distances": {"ArcFace": 0.21, "Facenet512": 0.18},
                    "anti_spoofing": {
                        "ref": {"status": "ok", "spoof_detected": False, "faces": [{"is_real": True, "antispoof_score": 0.92}]},
                        "target": {"status": "ok", "spoof_detected": False, "faces": [{"is_real": True, "antispoof_score": 0.88}]},
                    },
                    "fallback_reason": None,
                },
            }

    with mock.patch("face_similarity._get_engine", return_value=_Engine()):
        details = fs.compute_face_similarity_details("a.png", "b.png")

    assert details["score"] == 88
    assert details["pass"] is True
    assert details["diagnostics"]["per_model_distances"] == {"ArcFace": 0.21, "Facenet512": 0.18}
    assert details["diagnostics"]["anti_spoofing"]["ref"]["spoof_detected"] is False


def test_face_similarity_details_passes_through_spoof_warning_in_diagnostics():
    fs._ENGINE = None
    fs._ENGINE_ERROR = None

    class _Engine:
        def compare_images(self, _a, _b):
            return {
                "match": True,
                "score": 85.0,
                "error": None,  # FAS is log-only — does NOT set error
                "diagnostics": {
                    "mode": "normalized_crop",
                    "anti_spoofing": {
                        "ref": {"status": "ok", "spoof_detected": True, "faces": [{"is_real": False, "antispoof_score": 0.11}]},
                        "target": {"status": "ok", "spoof_detected": False, "faces": [{"is_real": True, "antispoof_score": 0.91}]},
                    },
                },
            }

    with mock.patch("face_similarity._get_engine", return_value=_Engine()):
        details = fs.compute_face_similarity_details("a.png", "b.png")

    # Score gates pass/fail; FAS warning surfaces only in diagnostics.
    assert details["score"] == 85
    assert details["pass"] is True
    assert details["error"] is None
    assert details["diagnostics"]["anti_spoofing"]["ref"]["spoof_detected"] is True


def test_parse_bool_handles_python_bools():
    assert fs._parse_bool(True) is True
    assert fs._parse_bool(False) is False


def test_parse_bool_handles_string_truthy():
    for v in ("true", "TRUE", "True", "1", "yes", "YES", "on", "ON"):
        assert fs._parse_bool(v) is True, f"expected True for {v!r}"


def test_parse_bool_handles_string_falsy():
    # Critical: "false" must NOT evaluate to True (the bug CodeRabbit caught).
    for v in ("false", "FALSE", "False", "0", "no", "NO", "off", "OFF"):
        assert fs._parse_bool(v) is False, f"expected False for {v!r}"


def test_parse_bool_returns_none_for_unrecognized():
    for v in ("garbage", "", None, 42, [], {}):
        assert fs._parse_bool(v) is None, f"expected None for {v!r}"
