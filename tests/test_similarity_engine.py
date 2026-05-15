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
        engine.use_ensemble = False  # keep mock count to 2 (primary only)
        reps1 = [{"embedding": [1.0, 0.0], "facial_area": {"x": 1, "y": 2, "w": 10, "h": 12}}]
        reps2 = [{"embedding": [1.0, 0.0], "facial_area": {"x": 3, "y": 4, "w": 8, "h": 9}}]

        with mock.patch.object(engine, "_represent_full_image_with_detection_with_model", side_effect=[reps1, reps2]), \
            mock.patch("similarity_engine.cv2.imread", side_effect=[np.zeros((200, 300, 3), dtype=np.uint8), np.zeros((120, 220, 3), dtype=np.uint8)]), \
            mock.patch.object(engine, "_select_prominent_face", side_effect=[reps1[0], reps2[0]]) as selector:
            result = engine._compare_full_image_align("a.png", "b.png")

        self.assertIsNone(result["error"])
        self.assertEqual(selector.call_args_list[0].args[2], (300, 200))
        self.assertEqual(selector.call_args_list[1].args[2], (220, 120))

    def test_compare_existing_skips_failed_target_embedding_and_uses_valid_face(self):
        engine = se.FaceEngine()
        engine.use_ensemble = False  # keep test focused on original per-target retry logic
        faces1 = [{"face": "src", "facial_area": {"x": 0, "y": 0, "w": 10, "h": 10}}]
        faces2 = [
            {"face": "bad-target", "facial_area": {"x": 0, "y": 0, "w": 8, "h": 8}},
            {"face": "good-target", "facial_area": {"x": 2, "y": 2, "w": 8, "h": 8}},
        ]

        def _fake_repr(face, _model=None):
            if face == "src":
                return np.asarray([1.0, 0.0], dtype=float)
            if face == "bad-target":
                raise ValueError("bad crop")
            return np.asarray([1.0, 0.0], dtype=float)

        with mock.patch.object(engine, "_extract_faces", side_effect=[faces1, faces2]), \
            mock.patch("similarity_engine.cv2.imread", side_effect=[np.zeros((100, 100, 3), dtype=np.uint8), np.zeros((120, 120, 3), dtype=np.uint8)]), \
            mock.patch.object(engine, "_represent_face_with_model", side_effect=_fake_repr):
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
        self.assertEqual(raw.secondary_model_name, "Facenet512")
        self.assertTrue(raw.use_ensemble)
        self.assertTrue(raw.anti_spoofing)

    def test_polynomial_curve_at_distance_zero_returns_100(self):
        engine = se.FaceEngine()
        score, match = engine._score_from_distance(0.0)
        self.assertAlmostEqual(score, 100.0)
        self.assertTrue(match)

    def test_polynomial_curve_at_threshold_returns_80(self):
        engine = se.FaceEngine()
        score, match = engine._score_from_distance(engine.threshold)
        self.assertAlmostEqual(score, 80.0)
        self.assertTrue(match)

    def test_polynomial_curve_at_distance_one_returns_zero(self):
        engine = se.FaceEngine()
        score, match = engine._score_from_distance(1.0)
        self.assertAlmostEqual(score, 0.0)
        self.assertFalse(match)

    def test_polynomial_curve_just_above_threshold_drops_below_80(self):
        engine = se.FaceEngine()
        score, match = engine._score_from_distance(engine.threshold + 0.01)
        self.assertLess(score, 80.0)
        self.assertFalse(match)

    def test_v1_9_curve_spreads_ai_edit_distances_across_meaningful_band(self):
        """v1.9 PASS_CURVE_EXPONENT=0.5 calibration check.

        AI-generated selfies (Nano Banana 2 Edit, FLUX Kontext, etc.) typically
        produce cosine distances 0.05-0.15 against the source crop. v1.8's
        exponent 2.5 mapped that whole band to 99-100% — useless for grading.
        v1.9 spreads it across 95-91%. Pin the reference points so future
        recalibration is intentional, not accidental.
        """
        engine = se.FaceEngine()
        # (distance, expected_score) — derived from 80 + 20*(1 - sqrt(d/0.68))
        # at threshold=0.68. Tolerances reflect rounding for human-readable
        # comments; the math is exact, the tolerance is for documentation drift.
        cases = [
            (0.00, 100.00),  # identical: pegged
            (0.05,  94.58),  # AI edit, near-perfect identity preservation
            (0.10,  92.33),
            (0.15,  90.61),
            (0.20,  89.15),  # visible variance, still clearly same person
            (0.30,  86.72),
            (0.50,  82.85),
            (0.68,  80.00),  # ArcFace official threshold
        ]
        for distance, expected in cases:
            with self.subTest(distance=distance):
                score, match = engine._score_from_distance(distance)
                self.assertAlmostEqual(score, expected, places=2,
                    msg=f"distance={distance} expected ~{expected}, got {score:.2f}")
                self.assertTrue(match)

    def test_ensemble_distance_pair_averages_two_models(self):
        engine = se.FaceEngine()
        engine.use_ensemble = True
        engine.secondary_model_name = "Facenet512"

        def _fake(face_input, model_name):
            if model_name == "ArcFace":
                return np.asarray([1.0, 0.0], dtype=float) if face_input == "src" \
                    else np.asarray([0.8, 0.6], dtype=float)
            return np.asarray([1.0, 0.0], dtype=float) if face_input == "src" \
                else np.asarray([0.6, 0.8], dtype=float)

        with mock.patch.object(engine, "_represent_face_with_model", side_effect=_fake):
            avg, per_model = engine._ensemble_distance_pair("src", "tgt")
        # ArcFace dist = 1 - 0.8 = 0.2; Facenet512 dist = 1 - 0.6 = 0.4; avg = 0.3
        self.assertAlmostEqual(avg, 0.3, places=4)
        self.assertEqual(set(per_model.keys()), {"ArcFace", "Facenet512"})

    def test_ensemble_disabled_returns_primary_only(self):
        engine = se.FaceEngine()
        engine.use_ensemble = False
        with mock.patch.object(
            engine,
            "_represent_face_with_model",
            return_value=np.asarray([1.0, 0.0], dtype=float),
        ) as m:
            avg, per_model = engine._ensemble_distance_pair("a", "b")
        self.assertEqual(m.call_count, 2)  # primary only on each face
        self.assertEqual(set(per_model.keys()), {"ArcFace"})
        self.assertAlmostEqual(avg, 0.0)

    def test_ensemble_secondary_failure_falls_back_to_primary(self):
        engine = se.FaceEngine()
        engine.use_ensemble = True

        def _fake(face_input, model_name):
            if model_name == "Facenet512":
                raise RuntimeError("secondary boom")
            return np.asarray([1.0, 0.0], dtype=float)

        with mock.patch.object(engine, "_represent_face_with_model", side_effect=_fake):
            avg, per_model = engine._ensemble_distance_pair("a", "b")
        self.assertAlmostEqual(avg, 0.0)
        self.assertEqual(set(per_model.keys()), {"ArcFace"})

    def test_anti_spoofing_check_detects_spoof(self):
        engine = se.FaceEngine()
        faces = [{"facial_area": {"x": 0, "y": 0, "w": 10, "h": 10},
                  "is_real": False, "antispoof_score": 0.12}]
        result = engine._check_anti_spoofing(faces, "img")
        self.assertIsNotNone(result)
        assert result is not None  # for type-checker
        self.assertTrue(result["spoof_detected"])

    def test_anti_spoofing_check_passes_real(self):
        engine = se.FaceEngine()
        faces = [{"facial_area": {"x": 0, "y": 0, "w": 10, "h": 10},
                  "is_real": True, "antispoof_score": 0.94}]
        result = engine._check_anti_spoofing(faces, "img")
        self.assertIsNotNone(result)
        assert result is not None
        self.assertFalse(result["spoof_detected"])

    def test_compare_existing_caches_reference_embeddings_with_ensemble(self):
        """Regression test: with ensemble on, the reference face must be embedded
        exactly once per model (2 calls total), not 2*N times where N=number of
        target faces. Catches the perf bug flagged by CodeRabbit + Gemini in PR #19."""
        engine = se.FaceEngine()
        engine.use_ensemble = True
        faces1 = [{"face": "src", "facial_area": {"x": 0, "y": 0, "w": 10, "h": 10}}]
        faces2 = [
            {"face": f"tgt{i}", "facial_area": {"x": i, "y": i, "w": 8, "h": 8}}
            for i in range(5)
        ]

        calls = []
        def _fake_repr(face_input, model_name):
            calls.append((face_input, model_name))
            return np.asarray([1.0, 0.0], dtype=float)

        with mock.patch.object(engine, "_extract_faces", side_effect=[faces1, faces2]), \
            mock.patch("similarity_engine.cv2.imread",
                       side_effect=[np.zeros((100, 100, 3), dtype=np.uint8),
                                    np.zeros((120, 120, 3), dtype=np.uint8)]), \
            mock.patch.object(engine, "_represent_face_with_model", side_effect=_fake_repr):
            result = engine._compare_existing("a.png", "b.png")

        ref_calls = [c for c in calls if c[0] == "src"]
        self.assertEqual(
            len(ref_calls), 2,
            f"Reference embedded {len(ref_calls)} times; expected 2 (primary + secondary). Calls: {ref_calls}",
        )
        target_calls = [c for c in calls if c[0] != "src"]
        self.assertEqual(
            len(target_calls), 10,
            f"Got {len(target_calls)} target embedding calls; expected 5 targets * 2 models = 10.",
        )
        self.assertIsNone(result["error"])
        self.assertTrue(result["match"])

    def test_anti_spoofing_absent_key_returns_not_active_status(self):
        """When DeepFace returned faces but no is_real key (FAS disabled at extract
        time), we now return a sentinel dict with status='not_active' instead of
        None — so UIs can render a consistent 'not assessed' message rather than
        treating one side as unknown."""
        engine = se.FaceEngine()
        faces = [{"facial_area": {"x": 0, "y": 0, "w": 10, "h": 10}}]
        result = engine._check_anti_spoofing(faces, "img")
        self.assertEqual(result["status"], "not_active")
        self.assertIsNone(result["spoof_detected"])

    def test_anti_spoofing_no_faces_returns_no_face_status(self):
        """Empty faces list (detector found nothing) → status='no_face'.
        This is what caused the asymmetric ref-only / target-only renderings."""
        engine = se.FaceEngine()
        result = engine._check_anti_spoofing([], "img")
        self.assertEqual(result["status"], "no_face")

    def test_summarize_fas_pair_both_real_returns_pass(self):
        diag = {"anti_spoofing": {
            "ref": {"status": "ok", "spoof_detected": False, "faces": []},
            "target": {"status": "ok", "spoof_detected": False, "faces": []},
        }}
        s = se.FaceEngine.summarize_fas_pair(diag)
        self.assertEqual(s["verdict"], "pass")
        self.assertEqual(s["color_hint"], "green")

    def test_summarize_fas_pair_one_side_spoof_returns_fail(self):
        diag = {"anti_spoofing": {
            "ref": {"status": "ok", "spoof_detected": True, "faces": []},
            "target": {"status": "ok", "spoof_detected": False, "faces": []},
        }}
        s = se.FaceEngine.summarize_fas_pair(diag)
        self.assertEqual(s["verdict"], "fail")
        self.assertEqual(s["color_hint"], "amber")
        self.assertIn("ref", s["message"])

    def test_summarize_fas_pair_asymmetric_returns_unavailable(self):
        """The bug case: ref has FAS data, target lost it (e.g., no face
        detected on one side). Previously rendered as 'target only' or 'ref
        only' — now consistently 'unavailable' so the user gets the same
        message every time."""
        diag = {"anti_spoofing": {
            "ref": {"status": "ok", "spoof_detected": False, "faces": []},
            "target": {"status": "no_face", "spoof_detected": None, "faces": []},
        }}
        s = se.FaceEngine.summarize_fas_pair(diag)
        self.assertEqual(s["verdict"], "unavailable")
        self.assertEqual(s["color_hint"], "muted")
        self.assertIn("target=no_face", s["message"])

    def test_summarize_fas_pair_legacy_shape_returns_unavailable(self):
        """Backward-compat: dicts WITHOUT the new 'status' key (e.g., from
        external mocks or pre-fix test fixtures) fall back to unavailable
        rather than crashing or producing partial verdicts."""
        diag = {"anti_spoofing": {
            "ref": {"spoof_detected": False, "faces": []},
            "target": {"spoof_detected": False, "faces": []},
        }}
        s = se.FaceEngine.summarize_fas_pair(diag)
        # No 'status' key in the legacy shape → treated as missing.
        self.assertEqual(s["verdict"], "unavailable")

    def test_summarize_fas_pair_no_diag_returns_unavailable(self):
        for diag in (None, {}, {"anti_spoofing": None}, {"anti_spoofing": "garbage"}):
            s = se.FaceEngine.summarize_fas_pair(diag)
            self.assertEqual(s["verdict"], "unavailable", f"failed on {diag!r}")

    # ── Score-semantics regression coverage (v4 fix) ─────────────────────
    # The DeepFace antispoof_score is the model's confidence in its is_real
    # verdict, NOT real-ness. score=0.99 with is_real=False means "99% sure
    # this is a SPOOF". Renderers used to display that as "99% real" — these
    # tests lock the new derived `real_conf` field so that bug can never
    # silently come back.

    def test_summarize_fas_pair_real_conf_inverted_when_spoofed(self):
        """is_real=False, antispoof_score=0.99 → real_conf must be ≈0.01,
        is_real boolean must be False. This is the DL-detected-as-spoof case
        that the v4 fix exists for.
        """
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
        s = se.FaceEngine.summarize_fas_pair(diag)
        self.assertEqual(s["verdict"], "fail")
        self.assertIs(s["ref_is_real"], False)
        self.assertIs(s["target_is_real"], True)
        # ref was 99% confident SPOOF → real_conf ≈ 0.01
        self.assertIsNotNone(s["ref_real_conf"])
        self.assertLess(s["ref_real_conf"], 0.05)
        # target was 97% confident REAL → real_conf ≈ 0.97
        self.assertIsNotNone(s["target_real_conf"])
        self.assertGreater(s["target_real_conf"], 0.95)

    def test_summarize_fas_pair_real_conf_passthrough_when_real(self):
        """is_real=True, antispoof_score=0.97 → real_conf should be ≈0.97
        (passthrough), is_real boolean should be True."""
        diag = {"anti_spoofing": {
            "ref": {
                "status": "ok",
                "spoof_detected": False,
                "faces": [{"is_real": True, "antispoof_score": 0.97}],
            },
            "target": {
                "status": "ok",
                "spoof_detected": False,
                "faces": [{"is_real": True, "antispoof_score": 0.93}],
            },
        }}
        s = se.FaceEngine.summarize_fas_pair(diag)
        self.assertEqual(s["verdict"], "pass")
        self.assertIs(s["ref_is_real"], True)
        self.assertIs(s["target_is_real"], True)
        self.assertAlmostEqual(s["ref_real_conf"], 0.97, places=2)
        self.assertAlmostEqual(s["target_real_conf"], 0.93, places=2)

    def test_summarize_fas_pair_includes_is_real_booleans(self):
        """All four new keys must always be present in the returned dict, even
        for unavailable verdicts — renderers should be able to read them
        without KeyError."""
        for diag in (None, {}, {"anti_spoofing": {}}):
            s = se.FaceEngine.summarize_fas_pair(diag)
            for key in ("ref_real_conf", "target_real_conf", "ref_is_real", "target_is_real"):
                self.assertIn(key, s, f"missing {key} for diag={diag!r}")
                self.assertIsNone(s[key], f"{key} should be None for diag={diag!r}")

    def test_side_real_confidence_returns_min_across_faces(self):
        """When multiple faces are present, the most pessimistic real_conf
        wins (the least-confidently-real face is the most informative)."""
        side = {
            "status": "ok",
            "faces": [
                {"is_real": True, "antispoof_score": 0.99},   # real_conf=0.99
                {"is_real": False, "antispoof_score": 0.80},  # real_conf=0.20
                {"is_real": True, "antispoof_score": 0.95},   # real_conf=0.95
            ],
        }
        # Min real_conf = 0.20 from the spoofed face.
        self.assertAlmostEqual(se.FaceEngine._side_real_confidence(side), 0.20, places=3)

    def test_side_real_confidence_skips_faces_without_is_real(self):
        """Faces that lack the is_real key (FAS not run on them) must be
        excluded from the min — otherwise their absence would taint the
        confidence floor."""
        side = {
            "status": "ok",
            "faces": [
                {"antispoof_score": 0.50},  # no is_real → skipped
                {"is_real": True, "antispoof_score": 0.85},  # real_conf=0.85
            ],
        }
        self.assertAlmostEqual(se.FaceEngine._side_real_confidence(side), 0.85, places=3)

    def test_side_is_real_any_spoof_returns_false(self):
        side = {"faces": [
            {"is_real": True, "antispoof_score": 0.95},
            {"is_real": False, "antispoof_score": 0.99},
        ]}
        self.assertIs(se.FaceEngine._side_is_real(side), False)

    def test_side_is_real_all_real_returns_true(self):
        side = {"faces": [
            {"is_real": True, "antispoof_score": 0.95},
            {"is_real": True, "antispoof_score": 0.88},
        ]}
        self.assertIs(se.FaceEngine._side_is_real(side), True)

    def test_side_is_real_no_records_returns_none(self):
        self.assertIsNone(se.FaceEngine._side_is_real({"faces": []}))
        self.assertIsNone(se.FaceEngine._side_is_real({"faces": [{"antispoof_score": 0.5}]}))


if __name__ == "__main__":
    unittest.main()
