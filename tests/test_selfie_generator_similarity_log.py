"""Guard the user-facing similarity log line on selfie generation.

Background: the polynomial mapping in ``similarity_engine`` deliberately
spreads ArcFace cosine distances 0.0-0.68 across scores 100-80% (per
``similarity/CLAUDE.md`` — do not replace the curve). Without surfacing the
underlying cosine distance, a 99% reading on a freshly AI-generated selfie is
indistinguishable from a degenerate fallback. These tests pin the new format:

    [Model] Similarity: 99% (cosine_distance=0.083, threshold=0.68, models=ArcFace+Facenet512)

…and ensure the line gracefully falls back when diagnostics are missing.
"""
import unittest

from selfie_generator import SelfieGenerator


class FormatSimilarityDiagnosticsTests(unittest.TestCase):
    def test_full_diagnostics_renders_distance_threshold_and_models(self):
        diag = {
            "raw_cosine_distance": 0.0832,
            "per_model_distances": {"ArcFace": 0.08, "Facenet512": 0.09},
        }
        suffix = SelfieGenerator._format_similarity_diagnostics(diag)
        self.assertIn("cosine_distance=0.083", suffix)
        self.assertIn("threshold=0.68", suffix)
        # Models are sorted for deterministic output.
        self.assertIn("models=ArcFace+Facenet512", suffix)
        # Suffix is parenthesized and starts with a leading space so it composes
        # cleanly after "Similarity: NN%".
        self.assertTrue(suffix.startswith(" ("))
        self.assertTrue(suffix.endswith(")"))

    def test_missing_diagnostics_returns_empty_string(self):
        """When DeepFace is unavailable the engine returns no diagnostics —
        the log line must still render as plain `Similarity: NN%`."""
        self.assertEqual(SelfieGenerator._format_similarity_diagnostics(None), "")
        self.assertEqual(SelfieGenerator._format_similarity_diagnostics({}), "")

    def test_diagnostics_without_distance_omits_distance_clause(self):
        """If only per_model is present (degenerate engine path), still render models."""
        diag = {"per_model_distances": {"ArcFace": 0.1}}
        suffix = SelfieGenerator._format_similarity_diagnostics(diag)
        self.assertNotIn("cosine_distance", suffix)
        self.assertIn("models=ArcFace", suffix)

    def test_distance_without_per_model_renders_threshold_only(self):
        diag = {"raw_cosine_distance": 0.5}
        suffix = SelfieGenerator._format_similarity_diagnostics(diag)
        self.assertIn("cosine_distance=0.500", suffix)
        self.assertIn("threshold=0.68", suffix)
        self.assertNotIn("models=", suffix)

    def test_non_dict_diagnostics_returns_empty(self):
        """Be defensive against unexpected upstream types."""
        self.assertEqual(SelfieGenerator._format_similarity_diagnostics([]), "")
        self.assertEqual(SelfieGenerator._format_similarity_diagnostics("not a dict"), "")

    def test_threshold_constant_matches_engine(self):
        """RAW_DISTANCE_THRESHOLD is the public constant the log line cites;
        if it ever drifts from the engine's internal threshold the log misleads."""
        from face_similarity import RAW_DISTANCE_THRESHOLD
        # Engine threshold is documented in similarity/CLAUDE.md as 0.68 (ArcFace
        # official). Keep both in sync.
        self.assertEqual(RAW_DISTANCE_THRESHOLD, 0.68)


if __name__ == "__main__":
    unittest.main()
