"""Regression tests for the Kling video download write site.

Covers two bugs fixed together:

1. ROOT CAUSE — the download wrote via a raw ``open(output_path, "wb")`` without
   creating the parent directory first. When the target dir (e.g. ``gen-images/``)
   did not exist, ``open`` raised ``FileNotFoundError`` (Errno 2) and the run
   failed even though fal.ai produced the video. The fix mkdir's the parent at
   the write site (and defensively in the output-folder branch).

2. ERROR SWALLOWED — on a final download failure the code did ``return None``
   without calling ``_set_last_error``, so the GUI surfaced a generic
   "Generation failed" instead of the real reason. The fix sets the detailed
   error on both download-phase failure exits (write/exception AND HTTP-status
   exhaustion).

These tests drive the real ``create_kling_generation`` with the network boundary
mocked, mirroring the style of ``tests/test_stability_improvements.py``.
"""

import os
import tempfile
import unittest
from unittest import mock

from PIL import Image

from kling_generator_falai import FalAIKlingGenerator
from kling_gui.queue_manager import QueueManager


class _FakeResponse:
    """Minimal stand-in for a ``requests.Response``."""

    def __init__(self, status_code=200, payload=None, content=b""):
        self.status_code = status_code
        self._payload = payload
        self.content = content
        self.text = "" if payload is None else str(payload)

    def json(self):
        if self._payload is None:
            raise ValueError("no payload")
        return self._payload


def _make_source_image(tmpdir):
    image_path = os.path.join(tmpdir, "selfie-expanded.png")
    Image.new("RGB", (64, 64), (0, 128, 255)).save(image_path)
    return image_path


def _build_generator():
    generator = FalAIKlingGenerator(api_key="fal-key", verbose=False)
    # Skip schema validation (no network / no disk cache needed for the test).
    generator.schema_manager.validate_parameters = lambda _endpoint, payload: payload
    return generator


def _fake_post_submit(*_args, **_kwargs):
    """fal.ai submit response — provides request_id + status/result URLs."""
    return _FakeResponse(
        status_code=200,
        payload={
            "request_id": "req-test-123",
            "status_url": "https://queue.fal.run/test/status",
            "response_url": "https://queue.fal.run/test/result",
        },
    )


class KlingDownloadMkdirTests(unittest.TestCase):
    """Edit A/C: the download auto-creates a missing output directory."""

    def test_download_creates_missing_output_dir(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            image_path = _make_source_image(tmpdir)
            # Target a gen-images/ subfolder that does NOT exist yet — the exact
            # condition from the bug report (base dir present, gen-images missing).
            missing_output = os.path.join(tmpdir, "gen-images")
            self.assertFalse(os.path.isdir(missing_output))

            fake_video_bytes = b"FAKE-MP4-BYTES" * 16

            def fake_get(url, *_args, **_kwargs):
                if url == "https://queue.fal.run/test/status":
                    return _FakeResponse(200, payload={"status": "COMPLETED"})
                if url == "https://queue.fal.run/test/result":
                    return _FakeResponse(
                        200, payload={"video": {"url": "https://media.test/v.mp4"}}
                    )
                # The media download URL.
                return _FakeResponse(200, content=fake_video_bytes)

            generator = _build_generator()
            with mock.patch.object(
                generator, "upload_to_freeimage",
                return_value="https://v3.fal.media/files/test.jpg",
            ), mock.patch(
                "kling_generator_falai.requests.post", side_effect=_fake_post_submit
            ), mock.patch(
                "kling_generator_falai.requests.get", side_effect=fake_get
            ), mock.patch(
                "kling_generator_falai.time.sleep", return_value=None
            ):
                result = generator.create_kling_generation(
                    character_image_path=image_path,
                    output_folder=missing_output,
                    custom_prompt="turn head left then right",
                    skip_duplicate_check=True,
                    duration=5,
                )

            # The directory was auto-created and the video file landed in it.
            self.assertTrue(os.path.isdir(missing_output))
            self.assertIsNotNone(result)
            self.assertTrue(os.path.isfile(result))
            self.assertEqual(os.path.dirname(result), missing_output)
            with open(result, "rb") as fh:
                self.assertEqual(fh.read(), fake_video_bytes)


class KlingDownloadErrorSurfacingTests(unittest.TestCase):
    """Edit B/B2: a download failure surfaces a real error, not the generic one."""

    def test_write_failure_sets_detailed_last_error(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            image_path = _make_source_image(tmpdir)
            output_dir = os.path.join(tmpdir, "gen-images")

            def fake_get(url, *_args, **_kwargs):
                if url == "https://queue.fal.run/test/status":
                    return _FakeResponse(200, payload={"status": "COMPLETED"})
                if url == "https://queue.fal.run/test/result":
                    return _FakeResponse(
                        200, payload={"video": {"url": "https://media.test/v.mp4"}}
                    )
                return _FakeResponse(200, content=b"bytes")

            generator = _build_generator()
            # Force the write to fail on every attempt so the retry loop exhausts.
            with mock.patch.object(
                generator, "upload_to_freeimage",
                return_value="https://v3.fal.media/files/test.jpg",
            ), mock.patch(
                "kling_generator_falai.requests.post", side_effect=_fake_post_submit
            ), mock.patch(
                "kling_generator_falai.requests.get", side_effect=fake_get
            ), mock.patch(
                "kling_generator_falai.time.sleep", return_value=None
            ), mock.patch(
                "builtins.open", side_effect=OSError("disk on fire")
            ):
                result = generator.create_kling_generation(
                    character_image_path=image_path,
                    output_folder=output_dir,
                    custom_prompt="turn head left then right",
                    skip_duplicate_check=True,
                    duration=5,
                )

            self.assertIsNone(result)
            self.assertTrue(generator.last_error_message)
            self.assertIn("Download failed after", generator.last_error_message)
            # The real OS reason is preserved, not swallowed.
            self.assertIn("disk on fire", generator.last_error_message)

    def test_http_status_exhaustion_sets_detailed_last_error(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            image_path = _make_source_image(tmpdir)
            output_dir = os.path.join(tmpdir, "gen-images")

            def fake_get(url, *_args, **_kwargs):
                if url == "https://queue.fal.run/test/status":
                    return _FakeResponse(200, payload={"status": "COMPLETED"})
                if url == "https://queue.fal.run/test/result":
                    return _FakeResponse(
                        200, payload={"video": {"url": "https://media.test/v.mp4"}}
                    )
                # The media download keeps returning a non-200 → retries exhaust.
                return _FakeResponse(500, content=b"")

            generator = _build_generator()
            with mock.patch.object(
                generator, "upload_to_freeimage",
                return_value="https://v3.fal.media/files/test.jpg",
            ), mock.patch(
                "kling_generator_falai.requests.post", side_effect=_fake_post_submit
            ), mock.patch(
                "kling_generator_falai.requests.get", side_effect=fake_get
            ), mock.patch(
                "kling_generator_falai.time.sleep", return_value=None
            ):
                result = generator.create_kling_generation(
                    character_image_path=image_path,
                    output_folder=output_dir,
                    custom_prompt="turn head left then right",
                    skip_duplicate_check=True,
                    duration=5,
                )

            self.assertIsNone(result)
            self.assertTrue(generator.last_error_message)
            self.assertIn("Failed to download after", generator.last_error_message)
            self.assertIn("HTTP 500", generator.last_error_message)

    def test_queue_manager_surfaces_real_error_not_generic(self):
        """The GUI message helper returns the detailed error, not the fallback."""
        manager = QueueManager.__new__(QueueManager)
        generator = _build_generator()
        generator._set_last_error(
            "Download failed after 3 attempts: [Errno 2] No such file or directory"
        )
        manager.generator = generator
        self.assertEqual(
            manager._get_generation_error_message(),
            "Download failed after 3 attempts: [Errno 2] No such file or directory",
        )
        # And when empty, it falls back to the generic string (unchanged behavior).
        generator.last_error_message = None
        self.assertEqual(manager._get_generation_error_message(), "Generation failed")


if __name__ == "__main__":
    unittest.main()
