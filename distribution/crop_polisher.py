"""Polish cropped face images — remove text, watermarks, seals via AI editing."""

import os
import time
import base64
import logging
from io import BytesIO
from typing import Optional, Callable

logger = logging.getLogger(__name__)


class CropPolisher:
    """Remove document artifacts from face crops using instruction-based AI editing.

    Supports two providers:
      - fal.ai  (FLUX.2 Edit endpoint)
      - BFL     (FLUX Kontext Pro REST API)
    """

    FAL_ENDPOINT = "fal-ai/flux-2-pro/edit"
    BFL_API_URL = "https://api.bfl.ai/v1/flux-kontext-pro"

    def __init__(
        self,
        falai_api_key: str = "",
        bfl_api_key: str = "",
        freeimage_key: str = "",
    ):
        self._falai_api_key = falai_api_key
        self._bfl_api_key = bfl_api_key
        self._freeimage_key = freeimage_key
        self._progress_callback: Optional[Callable[[str, str], None]] = None

    def set_progress_callback(self, cb: Callable[[str, str], None]):
        self._progress_callback = cb

    def _report(self, msg: str, level: str = "info"):
        if self._progress_callback:
            self._progress_callback(msg, level)

    def polish(
        self,
        image_path: str,
        output_path: str,
        provider: str = "bfl",
        prompt: str = "",
        strength: float = 0.8,
    ) -> Optional[str]:
        """Polish a face crop image, removing document artifacts.

        Args:
            image_path: Path to the cropped face image.
            output_path: Where to save the polished result.
            provider: "bfl" or "fal".
            prompt: Instruction prompt for the AI editor.
            strength: fal.ai only — edit strength 0.0 to 1.0.

        Returns:
            Absolute path to polished image, or None on failure.
        """
        if provider == "bfl":
            ok = self._polish_bfl(image_path, output_path, prompt)
        else:
            ok = self._polish_fal(image_path, output_path, prompt, strength)
        return os.path.abspath(output_path) if ok else None

    # ── fal.ai path ──────────────────────────────────────────────────

    def _polish_fal(self, image_path: str, output_path: str, prompt: str, strength: float = 0.8) -> bool:
        from fal_utils import (
            upload_to_freeimage,
            fal_queue_submit,
            fal_queue_poll,
            fal_download_file,
        )

        if not self._falai_api_key:
            self._report("fal.ai API key not set", "error")
            return False

        # Upload source image
        self._report("Uploading crop for polishing...", "upload")
        image_url, _ = upload_to_freeimage(
            image_path,
            max_size=1200,
            progress_cb=self._progress_callback,
            api_key=self._freeimage_key,
        )
        if not image_url:
            self._report("Failed to upload image", "error")
            return False

        payload = {
            "image_urls": [image_url],
            "prompt": prompt,
            "num_images": 1,
            "output_format": "png",
            "strength": strength,
        }

        self._report("Submitting FLUX.2 Edit job...", "task")
        result = fal_queue_submit(
            self._falai_api_key, self.FAL_ENDPOINT, payload, self._progress_callback
        )
        if not result:
            self._report("Failed to submit edit job", "error")
            return False

        status_url = result.get("status_url")
        if not status_url:
            self._report("No status URL in response", "error")
            return False

        self._report("Waiting for FLUX.2 Edit...", "progress")
        final = fal_queue_poll(
            self._falai_api_key, status_url, self._progress_callback,
            max_wait_seconds=120,
        )
        if not final:
            self._report("Edit failed or timed out", "error")
            return False

        # v2.28: defensive guard for the selfie-side aspect-ratio
        # self-heal sentinel. Crop polishing doesn't send aspect_ratio
        # today, but a future model swap could — surface a clear
        # error instead of "No images in result".
        if isinstance(final, dict) and final.get("__aspect_ratio_rejected__"):
            allowed = final.get("allowed") or []
            self._report(
                f"Edit model rejected aspect_ratio "
                f"(accepted: {', '.join(allowed) or '<empty>'})",
                "error",
            )
            return False

        # Extract image URL from result
        images = final.get("images", [])
        if not images:
            self._report("No images in result", "error")
            return False

        img_url = images[0].get("url") if isinstance(images[0], dict) else images[0]
        if not img_url:
            self._report("No image URL in result", "error")
            return False

        self._report("Downloading polished result...", "download")
        os.makedirs(os.path.dirname(output_path) or ".", exist_ok=True)
        if fal_download_file(img_url, output_path, self._progress_callback):
            self._report(f"Saved: {os.path.basename(output_path)}", "success")
            return True

        self._report("Download failed", "error")
        return False

    # ── BFL path ─────────────────────────────────────────────────────

    def _polish_bfl(self, image_path: str, output_path: str, prompt: str) -> bool:
        import requests
        from PIL import Image, ImageOps

        if not self._bfl_api_key:
            self._report("BFL API key not set", "error")
            return False

        # Encode image as raw base64 (no data URI prefix)
        self._report("Encoding image for BFL...", "upload")
        try:
            img = Image.open(image_path)
            img = ImageOps.exif_transpose(img)
            if img.mode in ("RGBA", "P", "LA"):
                img = img.convert("RGB")
            img.thumbnail((1024, 1024), Image.LANCZOS)
            buf = BytesIO()
            img.save(buf, format="JPEG", quality=90)
            image_b64 = base64.b64encode(buf.getvalue()).decode("utf-8")
        except Exception as exc:
            self._report(f"Failed to encode image: {exc}", "error")
            return False

        # Submit
        headers = {
            "x-key": self._bfl_api_key,
            "Content-Type": "application/json",
        }
        payload = {
            "prompt": prompt,
            "input_image": image_b64,
            "output_format": "png",
        }

        self._report("Submitting to BFL Kontext Pro...", "task")
        try:
            resp = requests.post(self.BFL_API_URL, json=payload, headers=headers, timeout=60)
            resp.raise_for_status()
            submit_data = resp.json()
        except requests.exceptions.HTTPError as exc:
            body = ""
            try:
                body = exc.response.text[:300]
            except Exception:
                pass
            self._report(f"BFL submit failed ({exc.response.status_code}): {body}", "error")
            return False
        except Exception as exc:
            self._report(f"BFL submit failed: {exc}", "error")
            return False

        polling_url = submit_data.get("polling_url")
        task_id = submit_data.get("id", "")
        if not polling_url:
            # Check for immediate result
            result_obj = submit_data.get("result")
            sample_url = (
                result_obj.get("sample") if isinstance(result_obj, dict) else None
            ) or submit_data.get("sample")
            if sample_url:
                return self._bfl_download(sample_url, output_path)
            self._report(f"No polling_url in BFL response: {submit_data}", "error")
            return False

        self._report(f"BFL task {task_id} queued, polling...", "task")

        # Poll (max 24 polls × 5s = 120s)
        self._report("Waiting for BFL polish...", "progress")
        max_polls = 24
        for i in range(max_polls):
            time.sleep(5)
            try:
                poll_resp = requests.get(
                    polling_url, headers={"x-key": self._bfl_api_key}, timeout=30
                )
                poll_resp.raise_for_status()
                poll_data = poll_resp.json()
            except Exception as exc:
                self._report(f"BFL poll error: {exc}", "warning")
                continue

            status = poll_data.get("status", "").lower()
            if status in ("ready", "succeeded"):
                result_obj = poll_data.get("result")
                sample_url = (
                    result_obj.get("sample") if isinstance(result_obj, dict) else None
                ) or poll_data.get("sample")
                if sample_url:
                    return self._bfl_download(sample_url, output_path)
                self._report(f"BFL result missing sample URL: {poll_data}", "error")
                return False
            elif status in ("error", "failed"):
                err_msg = poll_data.get("error", "Unknown BFL error")
                self._report(f"BFL polish failed: {err_msg}", "error")
                return False
            else:
                if i % 4 == 0:
                    self._report(f"BFL status: {status} (poll {i+1}/{max_polls})...", "progress")

        self._report("BFL polish timed out", "error")
        return False

    def _bfl_download(self, url: str, output_path: str) -> bool:
        import requests
        import tempfile

        self._report("Downloading BFL result...", "download")
        try:
            out_dir = os.path.dirname(output_path) or "."
            os.makedirs(out_dir, exist_ok=True)
            resp = requests.get(url, stream=True, timeout=120)
            resp.raise_for_status()
            fd, tmp_path = tempfile.mkstemp(dir=out_dir, suffix=".tmp")
            try:
                with os.fdopen(fd, "wb") as f:
                    for chunk in resp.iter_content(chunk_size=65536):
                        f.write(chunk)
                os.replace(tmp_path, output_path)
            except BaseException:
                try:
                    os.unlink(tmp_path)
                except OSError:
                    pass
                raise
            self._report(f"Saved: {os.path.basename(output_path)}", "success")
            return True
        except Exception as exc:
            self._report(f"BFL download failed: {exc}", "error")
            return False
