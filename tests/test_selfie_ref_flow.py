import os
import tempfile
import unittest

from selfie_generator import SelfieGenerator


class _FakeSelfieGenerator(SelfieGenerator):
    def __init__(self):
        super().__init__(api_key="fake")
        self.sim_calls = []
        self._sim_scores = [91, 86]

    def _generate_fal_raw(
        self,
        image_path: str,
        prompt: str,
        temp_output_path: str,
        resolved_endpoint: str,
        resolved_label: str,
        id_weight: float,
        width: int,
        height: int,
        actual_seed: int,
        poll_timeout_seconds: int,
    ) -> bool:
        with open(temp_output_path, "wb") as handle:
            handle.write(b"x")
        return True

    def _compute_similarity_result(
        self,
        source_image_path: str,
        generated_image_path: str,
    ):
        self.sim_calls.append((source_image_path, generated_image_path))
        score = self._sim_scores[len(self.sim_calls) - 1]
        return {
            "score": score,
            "pass": score >= 80,
            "error": None,
            "match": True,
            "diagnostics": {
                "raw_cosine_distance": 0.1,
                "per_model_distances": {"ArcFace": 0.1},
                "ref_path": source_image_path,
                "target_path": generated_image_path,
            },
        }


class SelfieReferenceFlowTests(unittest.TestCase):
    def test_similarity_uses_same_reference_path_across_multiple_generations(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            ref_path = os.path.join(tmpdir, "subject_crop.jpg")
            with open(ref_path, "wb") as handle:
                handle.write(b"ref")

            generator = _FakeSelfieGenerator()
            first = generator.generate(
                image_path=ref_path,
                prompt="test",
                output_folder=tmpdir,
                model_endpoint="openai/gpt-image-2/edit",
                model_label="Model A",
                seed=7,
            )
            second = generator.generate(
                image_path=ref_path,
                prompt="test",
                output_folder=tmpdir,
                model_endpoint="fal-ai/nano-banana-2/edit",
                model_label="Model B",
                seed=9,
            )

            self.assertIsNotNone(first)
            self.assertIsNotNone(second)
            self.assertEqual(len(generator.sim_calls), 2)
            self.assertEqual(generator.sim_calls[0][0], ref_path)
            self.assertEqual(generator.sim_calls[1][0], ref_path)
            self.assertNotEqual(generator.sim_calls[0][1], generator.sim_calls[1][1])
            self.assertIn("_sim91_", os.path.basename(first))
            self.assertIn("_sim86_", os.path.basename(second))


if __name__ == "__main__":
    unittest.main()
