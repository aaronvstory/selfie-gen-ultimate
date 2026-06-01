"""Upscale cropped face images using fal.ai super-resolution models."""

import os
import logging
from typing import Optional, Callable

logger = logging.getLogger(__name__)


class CropUpscaler:
    """Upscale images using fal.ai super-resolution endpoints.

    Providers:
      - crystal  : Crystal Upscaler (portraits, 2x, creativity=0)
      - aura_sr  : Aura SR v2 (fast, 4x, overlapping tiles)
    """

    PROVIDERS = {
        "crystal": {
            "endpoint": "fal-ai/crystal-upscaler",
        },
        "aura_sr": {
            "endpoint": "fal-ai/aura-sr",
        },
    }

    def __init__(self, falai_api_key: str = "", freeimage_key: str = ""):
        self._falai_api_key = falai_api_key
        self._freeimage_key = freeimage_key
        self._progress_callback: Optional[Callable[[str, str], None]] = None

    def set_progress_callback(self, cb: Callable[[str, str], None]):
        self._progress_callback = cb

    def _report(self, msg: str, level: str = "info"):
        if self._progress_callback:
            self._progress_callback(msg, level)

    def upscale(
        self,
        image_path: str,
        output_path: str,
        provider: str = "crystal",
        scale_factor: int = 2,
        creativity: float = 0.0,
        resemblance: float = 0.9,
    ) -> Optional[str]:
        """Upscale an image.

        Args:
            image_path: Path to the source image.
            output_path: Where to save the upscaled result.
            provider: "crystal" or "aura_sr".
            scale_factor: Crystal only — 2 or 4.
            creativity: Crystal only — 0.0 to 1.0, AI detail hallucination.
            resemblance: Crystal only — 0.0 to 1.0, fidelity to original.

        Returns:
            Absolute path to upscaled image, or None on failure.
        """
        from fal_utils import (
            upload_reference_image,
            fal_queue_submit,
            fal_queue_poll,
            fal_download_file,
        )

        if not self._falai_api_key:
            self._report("fal.ai API key not set", "error")
            return None

        prov = self.PROVIDERS.get(provider)
        if not prov:
            self._report(f"Unknown upscale provider: {provider}", "error")
            return None

        # Upload source image
        self._report("Uploading image for upscaling...", "upload")
        image_url, _, provider = upload_reference_image(
            image_path=image_path,
            fal_api_key=self._falai_api_key,
            max_size=2048,
            progress_cb=self._progress_callback,
            freeimage_api_key=self._freeimage_key,
        )
        if not image_url:
            self._report("Failed to upload image", "error")
            return None
        if provider:
            self._report(f"Reference upload provider: {provider}", "upload")

        endpoint = prov["endpoint"]

        if provider == "crystal":
            payload = {
                "image_url": image_url,
                "scale_factor": scale_factor,
                "creativity": creativity,
                "resemblance": resemblance,
            }
        else:
            payload = {
                "image_url": image_url,
                "upscale_factor": 4,
                "overlapping_tiles": True,
                "checkpoint": "v2",
            }

        self._report(f"Submitting {provider} upscale job...", "task")
        result = fal_queue_submit(
            self._falai_api_key, endpoint, payload, self._progress_callback
        )
        if not result:
            self._report("Failed to submit upscale job", "error")
            return None

        status_url = result.get("status_url")
        if not status_url:
            self._report("No status URL in response", "error")
            return None

        self._report("Waiting for upscale...", "progress")
        final = fal_queue_poll(
            self._falai_api_key, status_url, self._progress_callback,
            max_wait_seconds=180,
        )
        if not final:
            self._report("Upscale failed or timed out", "error")
            return None

        # Extract image URL — different models return different structures
        image_url_result = None
        if "image" in final and isinstance(final["image"], dict):
            image_url_result = final["image"].get("url")
        if not image_url_result:
            images = final.get("images", [])
            if images:
                image_url_result = (
                    images[0].get("url") if isinstance(images[0], dict) else images[0]
                )
        if not image_url_result:
            # Some endpoints return output at top level
            image_url_result = final.get("url")
        if not image_url_result:
            self._report("No image URL in upscale result", "error")
            return None

        self._report("Downloading upscaled result...", "download")
        os.makedirs(os.path.dirname(output_path) or ".", exist_ok=True)
        if fal_download_file(image_url_result, output_path, self._progress_callback):
            self._report(f"Saved: {os.path.basename(output_path)}", "success")
            return os.path.abspath(output_path)

        self._report("Download failed", "error")
        return None
