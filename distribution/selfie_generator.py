"""Generate selfie-style portraits using fal.ai identity image models."""

import os
import random
import logging
import re
import threading
import json
from typing import Optional, Callable, Dict, List, Any
from pathlib import Path

logger = logging.getLogger(__name__)

class SelfieGenerator:
    """Generate selfie images using fal.ai image-to-image identity models."""

    DEFAULT_ENDPOINT = "fal-ai/nano-banana-2/edit"
    HANDOFF_JSON_KEYS = (
        "hair",
        "skin",
        "eyes",
        "face_shape",
        "age_range",
        "gender",
        "clothing",
        "expression",
    )
    DEFAULT_POLL_TIMEOUT_SECONDS = 300
    MIN_POLL_TIMEOUT_SECONDS = 60
    MAX_POLL_TIMEOUT_SECONDS = 1800
    @staticmethod
    def resolve_wildcards(template: str) -> str:
        """Resolve {opt1|opt2|opt3} blocks by randomly picking one option per block."""
        def _pick(match):
            options = [o.strip() for o in match.group(1).split("|") if o.strip()]
            return random.choice(options) if options else ""
        return re.sub(r"\{([^{}]+)\}", _pick, template)

    AVAILABLE_MODELS = [
        {
            "endpoint": "openai/gpt-image-2/edit",
            "label": "GPT Image 2 Edit",
            "slug": "gpt-image-2-edit",
            "provider": "fal",
            "api_url": "https://fal.ai/models/openai/gpt-image-2/edit/api",
        },
        {
            "endpoint": "fal-ai/nano-banana-2/edit",
            "label": "Nano Banana 2 Edit",
            "slug": "nano-banana-2-edit",
            "provider": "fal",
            "api_url": "https://fal.ai/models/fal-ai/nano-banana-2/edit/api",
        },
    ]
    _SELFIE_MODELS_FILE = Path(__file__).resolve().parent / "models.json"
    _FALLBACK_SELFIE_MODELS = [dict(item) for item in AVAILABLE_MODELS]

    def __init__(self, api_key: str, freeimage_key: Optional[str] = None, bfl_api_key: Optional[str] = None):
        self.api_key = api_key
        self._freeimage_key = freeimage_key
        self._bfl_api_key = bfl_api_key or ""
        self._progress_callback: Optional[Callable[[str, str], None]] = None
        self._cancel_event: Optional["threading.Event"] = None
        self.__class__._refresh_available_models()

    def set_progress_callback(self, cb: Callable[[str, str], None]):
        self._progress_callback = cb

    def set_cancel_event(self, event: "threading.Event") -> None:
        self._cancel_event = event

    def _report(self, msg: str, level: str = "info"):
        if self._progress_callback:
            self._progress_callback(msg, level)

    @classmethod
    def get_available_models(cls) -> List[Dict[str, str]]:
        """Return supported selfie model metadata for UI rendering."""
        cls._refresh_available_models()
        return [dict(model) for model in cls.AVAILABLE_MODELS]

    @classmethod
    def _refresh_available_models(cls) -> None:
        """Load selfie model metadata from models.json with fallback."""
        try:
            with cls._SELFIE_MODELS_FILE.open("r", encoding="utf-8") as handle:
                payload = json.load(handle)
            models_raw = payload.get("selfie_models", [])
            validated: List[Dict[str, str]] = []
            for item in models_raw:
                if not isinstance(item, dict):
                    continue
                endpoint = str(item.get("endpoint", "")).strip()
                label = str(item.get("label", endpoint)).strip()
                if not endpoint:
                    continue
                validated.append(
                    {
                        "endpoint": endpoint,
                        "label": label or endpoint,
                        "slug": str(item.get("slug", "")).strip(),
                        "provider": str(item.get("provider", "fal")).strip() or "fal",
                        "api_url": str(item.get("api_url", "")).strip(),
                    }
                )
            if validated:
                cls.AVAILABLE_MODELS = validated
                return
        except Exception as exc:
            logger.warning("Failed loading selfie_models from models.json: %s", exc)
        cls.AVAILABLE_MODELS = [dict(item) for item in cls._FALLBACK_SELFIE_MODELS]

    @classmethod
    def get_model_label(cls, endpoint: str) -> str:
        cls._refresh_available_models()
        for model in cls.AVAILABLE_MODELS:
            if model["endpoint"] == endpoint:
                return model["label"]
        return endpoint

    @classmethod
    def normalize_handoff_identity(
        cls, payload: Any, required_fields: Optional[List[str]] = None
    ) -> Optional[Dict[str, str]]:
        """Normalize Step 1 JSON payload into the expected identity field map.

        Args:
            payload: Parsed JSON dict from Vision analysis.
            required_fields: If given, validate these fields instead of HANDOFF_JSON_KEYS.
        """
        if not isinstance(payload, dict):
            return None
        fields = required_fields or list(cls.HANDOFF_JSON_KEYS)
        normalized: Dict[str, str] = {}
        for key in fields:
            value = payload.get(key)
            if value is None:
                return None
            normalized[key] = str(value).strip()
        return normalized

    @classmethod
    def _model_short_name(cls, endpoint: str) -> str:
        cls._refresh_available_models()
        for model in cls.AVAILABLE_MODELS:
            if model["endpoint"] == endpoint:
                slug = model.get("slug", "")
                if slug:
                    return re.sub(r"[^a-z0-9\-]+", "-", slug.lower()).strip("-")
        fallback = endpoint.split("/")[-1].lower()
        return re.sub(r"[^a-z0-9\-]+", "-", fallback).strip("-")

    @staticmethod
    def _closest_aspect_ratio(width: int, height: int) -> str:
        target = width / max(1, height)
        options = {
            "21:9": 21 / 9,
            "16:9": 16 / 9,
            "3:2": 3 / 2,
            "4:3": 4 / 3,
            "5:4": 5 / 4,
            "1:1": 1.0,
            "4:5": 4 / 5,
            "3:4": 3 / 4,
            "2:3": 2 / 3,
            "9:16": 9 / 16,
        }
        return min(options.items(), key=lambda kv: abs(kv[1] - target))[0]

    @staticmethod
    def _next_indexed_output_path(output_folder: str, prefix: str) -> str:
        max_index = 0
        for name in os.listdir(output_folder):
            if not name.lower().endswith(".png"):
                continue
            if not name.startswith(prefix):
                continue
            suffix = os.path.splitext(name)[0][len(prefix):]
            if suffix.isdigit():
                max_index = max(max_index, int(suffix))
        next_index = max_index + 1
        output_path = os.path.join(output_folder, f"{prefix}{next_index:03d}.png")
        while os.path.exists(output_path):
            next_index += 1
            output_path = os.path.join(output_folder, f"{prefix}{next_index:03d}.png")
        return output_path

    @staticmethod
    def _format_similarity_diagnostics(diag: Optional[Dict[str, Any]]) -> str:
        """Render the ' (cosine_distance=..., threshold=..., models=...)' suffix.

        The polynomial mapping in ``similarity_engine`` deliberately spreads
        ArcFace cosine distances 0.0-0.68 across scores 100-80 (per
        ``similarity/CLAUDE.md``). Without surfacing the underlying distance
        the user cannot tell whether a 99% reading is a real match or a
        degenerate fallback — this helper exposes the raw signal.

        Returns "" when diagnostics are absent (e.g., DeepFace unavailable),
        so the user-facing log line gracefully falls back to bare percentage.
        """
        if not isinstance(diag, dict):
            return ""
        from face_similarity import RAW_DISTANCE_THRESHOLD
        parts: List[str] = []
        dist = diag.get("raw_cosine_distance")
        if isinstance(dist, (int, float)):
            parts.append(f"cosine_distance={float(dist):.3f}")
            parts.append(f"threshold={RAW_DISTANCE_THRESHOLD:.2f}")
        per_model = diag.get("per_model_distances")
        if isinstance(per_model, dict) and per_model:
            parts.append(f"models={'+'.join(sorted(per_model.keys()))}")
        return f" ({', '.join(parts)})" if parts else ""

    def _compute_similarity_result(
        self,
        source_image_path: str,
        generated_image_path: str,
    ) -> Dict[str, Any]:
        """Compute face similarity and return the full details dict (score + diagnostics).

        Delegates to the standalone ``face_similarity`` module. Returns the dict so
        callers can surface the raw cosine distance + per-model breakdown alongside
        the mapped score — without that visibility, a 99% reading on a polynomial-
        mapped score (deliberate per ``similarity/CLAUDE.md``) looks indistinguishable
        from a degenerate fallback.
        """
        from face_similarity import compute_face_similarity_details
        return compute_face_similarity_details(
            source_image_path, generated_image_path, report_cb=self._report
        )

    @classmethod
    def _build_payload(
        cls,
        model_endpoint: str,
        prompt: str,
        image_url: str,
        id_weight: float,
        width: int,
        height: int,
        seed: int,
    ) -> dict:
        if model_endpoint == "openai/gpt-image-2/edit":
            return {
                "prompt": prompt,
                "image_urls": [image_url],
                "image_size": {"width": width, "height": height},
                "quality": "high",
                "num_images": 1,
                "output_format": "png",
            }

        if model_endpoint == "fal-ai/nano-banana-2/edit":
            return {
                "prompt": prompt,
                "image_urls": [image_url],
                "num_images": 1,
                "aspect_ratio": cls._closest_aspect_ratio(width, height),
                "resolution": "1K",
                "output_format": "png",
                "seed": seed,
            }

        raise ValueError(f"Unsupported selfie model endpoint: {model_endpoint}")

    def _is_cancelled(self) -> bool:
        return self._cancel_event is not None and self._cancel_event.is_set()

    @classmethod
    def sanitize_poll_timeout_seconds(cls, value: Optional[int]) -> int:
        """Clamp poll timeout seconds to safe bounds."""
        try:
            timeout = int(value)
        except (TypeError, ValueError):
            timeout = cls.DEFAULT_POLL_TIMEOUT_SECONDS
        timeout = max(cls.MIN_POLL_TIMEOUT_SECONDS, timeout)
        timeout = min(cls.MAX_POLL_TIMEOUT_SECONDS, timeout)
        return timeout

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
        """Run the fal.ai generation pipeline. Returns True on success."""
        if self._is_cancelled():
            self._report("Generation cancelled", "warning")
            return False

        from fal_utils import (
            upload_reference_image,
            fal_queue_submit,
            fal_queue_poll,
            fal_download_file,
        )

        self._report("Uploading identity reference...", "upload")
        image_url, _, upload_provider = upload_reference_image(
            image_path, progress_cb=self._progress_callback,
            fal_api_key=self.api_key,
            freeimage_api_key=self._freeimage_key,
        )
        if not image_url:
            self._report("Failed to upload image", "error")
            return False
        if upload_provider:
            self._report(f"Reference upload provider: {upload_provider}", "upload")

        payload = self._build_payload(
            model_endpoint=resolved_endpoint,
            prompt=prompt,
            image_url=image_url,
            id_weight=id_weight,
            width=width,
            height=height,
            seed=actual_seed,
        )

        self._report(
            f"Submitting to {resolved_label} ({resolved_endpoint}, seed={actual_seed})...",
            "task",
        )

        result = fal_queue_submit(
            self.api_key, resolved_endpoint, payload, self._progress_callback
        )
        if not result:
            self._report("Failed to submit job", "error")
            return False

        status_url = result.get("status_url")
        if not status_url:
            self._report("No status URL in response", "error")
            return False

        self._report(
            f"Waiting for generation... (timeout {poll_timeout_seconds}s)",
            "progress",
        )
        final = fal_queue_poll(
            self.api_key, status_url, self._progress_callback,
            max_wait_seconds=poll_timeout_seconds,
            cancel_event=self._cancel_event,
        )
        if not final:
            self._report("Generation failed or timed out", "error")
            return False

        images = final.get("images", [])
        if not images:
            self._report("No images in result", "error")
            return False

        image_url_result = images[0].get("url") if isinstance(images[0], dict) else images[0]
        if not image_url_result:
            self._report("No image URL in result", "error")
            return False

        self._report("Downloading result...", "download")
        return fal_download_file(image_url_result, temp_output_path, self._progress_callback)

    def generate(
        self,
        image_path: str,
        prompt: str,
        output_folder: str,
        id_weight: float = 1.0,
        width: int = 720,
        height: int = 1280,
        seed: int = -1,
        model_endpoint: str = "",
        model_label: str = "",
        poll_timeout_seconds: Optional[int] = None,
    ) -> Optional[str]:
        """Generate a selfie from an identity reference image.

        Uses fal.ai model endpoints configured in AVAILABLE_MODELS.

        Returns:
            Absolute path to generated image, or None on failure.
        """
        actual_seed = random.randint(0, 2**32 - 1) if seed == -1 else seed
        resolved_endpoint = model_endpoint or self.DEFAULT_ENDPOINT
        resolved_label = model_label or self.get_model_label(resolved_endpoint)
        timeout_seconds = self.sanitize_poll_timeout_seconds(poll_timeout_seconds)

        os.makedirs(output_folder, exist_ok=True)
        stem = Path(image_path).stem
        model_short = self._model_short_name(resolved_endpoint)
        temp_output_path = os.path.join(
            output_folder, f"{stem}_{model_short}_tmp_{actual_seed}.png"
        )
        temp_counter = 1
        while os.path.exists(temp_output_path):
            temp_counter += 1
            temp_output_path = os.path.join(
                output_folder, f"{stem}_{model_short}_tmp_{actual_seed}_{temp_counter}.png"
            )

        success = self._generate_fal_raw(
            image_path=image_path,
            prompt=prompt,
            temp_output_path=temp_output_path,
            resolved_endpoint=resolved_endpoint,
            resolved_label=resolved_label,
            id_weight=id_weight,
            width=width,
            height=height,
            actual_seed=actual_seed,
            poll_timeout_seconds=timeout_seconds,
        )

        if not success:
            return None

        # Shared post-processing: similarity + final rename
        self._report(
            f"[{resolved_label}] Compare pair: ref={os.path.basename(image_path)} "
            f"target={os.path.basename(temp_output_path)}",
            "debug",
        )
        sim_details = self._compute_similarity_result(image_path, temp_output_path)
        similarity = None if sim_details.get("error") else sim_details.get("score")
        if similarity is None:
            self._report(f"[{resolved_label}] Similarity: n/a", "info")
            similarity_tag = "simna"
        else:
            sim_suffix = self._format_similarity_diagnostics(sim_details.get("diagnostics"))
            self._report(f"[{resolved_label}] Similarity: {similarity}%{sim_suffix}", "info")
            similarity_tag = f"sim{similarity}"

        final_prefix = f"{stem}_{model_short}_{similarity_tag}_"
        final_output_path = self._next_indexed_output_path(output_folder, final_prefix)
        try:
            os.replace(temp_output_path, final_output_path)
        except Exception:
            try:
                os.remove(temp_output_path)
            except Exception:
                pass
            self._report("Download finalization failed", "error")
            return None

        self._report(f"Saved: {os.path.basename(final_output_path)}", "success")
        return final_output_path
