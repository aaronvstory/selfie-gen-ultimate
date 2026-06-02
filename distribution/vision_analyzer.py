"""Vision analysis via OpenRouter API (chat completions with image input)."""

import os
import base64
import requests
import logging
from typing import Optional, Callable

logger = logging.getLogger(__name__)


def _extract_openrouter_error_detail(exc: requests.RequestException) -> str:
    """Pull the human-readable reason out of an OpenRouter error response.

    ``str(exc)`` for an HTTP error is just "<code> Client Error ... for url".
    The actionable reason (e.g. "Insufficient credits", "model not found",
    "content policy", rate-limit window) lives in the JSON body. Return a
    truncated, single-line version of it, or "" if no usable body is present.
    """
    resp = getattr(exc, "response", None)
    if resp is None:
        return ""
    try:
        data = resp.json()
        if isinstance(data, dict):
            err = data.get("error")
            if isinstance(err, dict):
                msg = err.get("message") or err.get("code")
                if msg:
                    return str(msg)[:300]
            if data.get("message"):
                return str(data["message"])[:300]
            return str(data)[:300]
    except Exception:  # noqa: BLE001 — body not JSON; fall back to raw text
        pass
    text = (getattr(resp, "text", "") or "").strip()
    return text[:300]


class VisionAnalyzer:
    """Analyze portrait images using OpenRouter vision models."""

    ENDPOINT = "https://openrouter.ai/api/v1/chat/completions"
    DEFAULT_SYSTEM_PROMPT = (
        "You are a JSON API. You only output valid JSON. Never write explanations, "
        "never use markdown, never use code blocks, never say anything outside the JSON.\n\n"
        "Analyze the portrait image and return exactly this JSON structure with "
        "descriptive phrase values — never single words:\n"
        '{"hair":"...","skin":"...","eyes":"...","face_shape":"...","age_range":"...",'
        '"gender":"...","clothing":"...","expression":"..."}\n\n'
        "Rules:\n"
        "- Output MUST start with { and end with }\n"
        "- No text before or after the JSON\n"
        "- No markdown fences, no commentary\n"
        "- All values must be descriptive phrases\n"
        '- clothing: if not visible, write "casual everyday outfit"\n'
        '- gender: write only "man" or "woman"'
    )
    DEFAULT_USER_PROMPT = (
        "Analyze this portrait and return the JSON."
    )

    # Field hints used by build_json_system_prompt() for dynamic schemas
    _FIELD_HINTS = {
        "hair": "hair color, length, and style",
        "skin": "skin tone and complexion",
        "eyes": "eye color and shape",
        "face_shape": "face shape (oval, round, square, etc.)",
        "age_range": "estimated age range (e.g. 'early to mid-twenties')",
        "gender": "write only 'man' or 'woman'",
        "clothing": "visible clothing description; if not visible write 'casual everyday outfit'",
        "expression": "facial expression",
    }

    @staticmethod
    def build_json_system_prompt(required_fields: list) -> str:
        """Build a system prompt requesting JSON with exactly these fields.

        Fields present in _FIELD_HINTS get descriptive guidance; unknown fields
        get a hint derived from the field name.
        """
        schema_parts = []
        for f in required_fields:
            hint = VisionAnalyzer._FIELD_HINTS.get(f, f.replace("_", " "))
            schema_parts.append(f'"{f}":"<{hint}>"')
        schema = "{" + ",".join(schema_parts) + "}"

        return (
            "You are a JSON API. You only output valid JSON. Never write explanations, "
            "never use markdown, never use code blocks, never say anything outside the JSON.\n\n"
            f"Analyze the portrait image and return exactly this JSON structure with "
            f"descriptive phrase values — never single words:\n{schema}\n\n"
            "Rules:\n"
            "- Output MUST start with { and end with }\n"
            "- No text before or after the JSON\n"
            "- No markdown fences, no commentary\n"
            "- All values must be descriptive phrases"
        )

    def __init__(
        self,
        api_key: str,
        model: str = "bytedance-seed/seed-1.6-flash",
        system_prompt: Optional[str] = None,
        user_prompt: Optional[str] = None,
    ):
        self.api_key = api_key
        self.model = model
        self.system_prompt = (
            system_prompt.strip() if system_prompt and system_prompt.strip() else self.DEFAULT_SYSTEM_PROMPT
        )
        self.user_prompt = (
            user_prompt.strip() if user_prompt and user_prompt.strip() else self.DEFAULT_USER_PROMPT
        )
        self._progress_callback: Optional[Callable[[str, str], None]] = None

    def set_progress_callback(self, cb: Callable[[str, str], None]):
        self._progress_callback = cb

    def _report(self, msg: str, level: str = "info"):
        if self._progress_callback:
            self._progress_callback(msg, level)

    def analyze_image(self, image_path: str) -> Optional[dict]:
        """Analyze a portrait image, return dict with 'prompt' key.

        Sends image as base64 data URI to OpenRouter chat completions.
        Returns {"prompt": "..."} or None on failure.
        """
        self._report(f"Reading image: {os.path.basename(image_path)}", "upload")

        # Read, apply EXIF rotation, and encode image as base64
        try:
            from PIL import Image, ImageOps
            from io import BytesIO
            img = Image.open(image_path)
            img = ImageOps.exif_transpose(img)
            buf = BytesIO()
            img.save(buf, format="JPEG", quality=95)
            image_data = base64.b64encode(buf.getvalue()).decode("utf-8")
        except OSError as e:
            self._report(f"Cannot read image: {e}", "error")
            logger.error("VisionAnalyzer read error: %s", e)
            return None

        # Always JPEG after exif_transpose re-encode above
        mime = "image/jpeg"

        data_uri = f"data:{mime};base64,{image_data}"

        self._report(f"Sending to {self.model}...", "api")

        payload = {
            "model": self.model,
            "messages": [
                {"role": "system", "content": self.system_prompt},
                {
                    "role": "user",
                    "content": [
                        {
                            "type": "image_url",
                            "image_url": {"url": data_uri},
                        },
                        {
                            "type": "text",
                            "text": self.user_prompt,
                        },
                    ],
                },
            ],
            "max_tokens": 500,
            "temperature": 0.7,
        }

        headers = {
            "Authorization": f"Bearer {self.api_key}",
            "Content-Type": "application/json",
        }

        try:
            resp = requests.post(
                self.ENDPOINT, json=payload, headers=headers, timeout=30
            )
            resp.raise_for_status()
            result = resp.json()
            prompt = result["choices"][0]["message"]["content"].strip()
            self._report("Analysis complete", "success")
            return {"prompt": prompt}
        except requests.RequestException as e:
            # str(e) on a raise_for_status() HTTPError gives only
            # "400 Client Error ... for url" — OpenRouter puts the ACTUAL
            # reason (insufficient credits, invalid model, content policy,
            # rate limit) in the response BODY, which str(e) drops. Surface
            # a truncated body so the Prep error is actionable (v2.16).
            detail = _extract_openrouter_error_detail(e)
            message = f"API error: {e}"
            if detail:
                message = f"{message} — {detail}"
            self._report(message, "error")
            logger.error("VisionAnalyzer error: %s", message)
            return None
        except (KeyError, IndexError) as e:
            self._report(f"Unexpected response format: {e}", "error")
            logger.error("VisionAnalyzer parse error: %s", e)
            return None
