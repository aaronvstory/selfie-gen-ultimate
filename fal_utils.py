"""Shared fal.ai utilities: upload, queue submit, poll, download."""

import os
import re
import time
import threading
import requests
import logging
import tempfile
from pathlib import Path
from typing import Optional, Callable, Tuple, Any
from PIL import Image, ImageOps
import io
import base64

logger = logging.getLogger(__name__)

ProgressCallback = Optional[Callable[[str, str], None]]  # (message, level)

# Freeimage.host API key from environment (optional)
_FREEIMAGE_KEY = os.getenv("FREEIMAGE_API_KEY", "")
_FAL_CLIENT_IMPORT_ERROR = ""
try:
    import fal_client  # type: ignore
except Exception as exc:  # pragma: no cover - tested via runtime fallback behavior
    fal_client = None
    _FAL_CLIENT_IMPORT_ERROR = str(exc)

_BALANCE_LOCK_MARKERS = (
    "exhausted balance",
    "user is locked",
    "insufficient credits",
    "insufficient balance",
    "quota",
    "payment required",
)


def _extract_http_error_detail(resp: requests.Response, limit: int = 500) -> str:
    """Return a readable error detail from an HTTP response.

    Detects fal.ai's `content_policy_violation` 422 and replaces the
    verbose dump with a one-line actionable message. Detection is
    generic: any model whose content checker returns a
    `type == "content_policy_violation"` entry triggers it (GPT Image 2
    Edit is the typical hit; other models that adopt the same checker
    payload will work too).
    """
    try:
        data = resp.json()
        if isinstance(data, dict) and isinstance(data.get("detail"), list):
            for entry in data["detail"]:
                if not isinstance(entry, dict):
                    continue
                if entry.get("type") != "content_policy_violation":
                    continue
                field = ""
                loc = entry.get("loc")
                if isinstance(loc, list) and loc:
                    last = loc[-1]
                    if isinstance(last, (str, int)):
                        field = str(last)
                target = f" in `{field}`" if field else ""
                return (
                    f"Content policy violation{target}: the model's "
                    "content checker flagged the request. Edit the "
                    "Selfie prompt (or the input image) to remove the "
                    "language it objected to -- common triggers are "
                    "explicit identity / forensic-imaging keywords "
                    "(e.g. 'PRNU', 'noiseprint', 'demosaicing'). Other "
                    "selfie models with softer checkers may still "
                    "accept the same prompt."
                )[:limit]
        if isinstance(data, dict):
            if "detail" in data:
                return str(data["detail"])[:limit]
            if "error" in data:
                return str(data["error"])[:limit]
            if "message" in data:
                return str(data["message"])[:limit]
        return str(data)[:limit]
    except Exception:
        return (resp.text or "").strip()[:limit]


def _sleep_with_cancel(
    delay_seconds: float,
    cancel_event: Optional[threading.Event],
    progress_cb: ProgressCallback = None,
) -> bool:
    """Sleep for delay seconds; return True if cancelled while sleeping."""
    if cancel_event is None:
        time.sleep(delay_seconds)
        return False
    if cancel_event.wait(timeout=max(0.0, float(delay_seconds))):
        if progress_cb:
            progress_cb("Generation cancelled", "warning")
        return True
    return False


def _prepare_image_for_upload(image_path: str, max_size: int = 1200) -> Tuple[bytes, Image.Image]:
    """Normalize image orientation/mode/size and return jpeg bytes + decoded PIL copy."""
    with Image.open(image_path) as source_img:
        img = ImageOps.exif_transpose(source_img)
        if img.width > max_size or img.height > max_size:
            img.thumbnail((max_size, max_size), Image.Resampling.LANCZOS)

        if img.mode in ("RGBA", "LA", "P"):
            background = Image.new("RGB", img.size, (255, 255, 255))
            if img.mode == "P":
                img = img.convert("RGBA")
            background.paste(
                img,
                mask=img.split()[-1] if img.mode in ("RGBA", "LA") else None,
            )
            img = background
        elif img.mode != "RGB":
            img = img.convert("RGB")

        buffer = io.BytesIO()
        img.save(buffer, format="JPEG", quality=85, optimize=True)
        jpeg_bytes = buffer.getvalue()

    decoded = Image.open(io.BytesIO(jpeg_bytes))
    decoded.load()
    return jpeg_bytes, decoded


def _is_balance_lock_error(message: str) -> bool:
    """Return True when message indicates account/balance lock state."""
    lowered = str(message or "").lower()
    return any(marker in lowered for marker in _BALANCE_LOCK_MARKERS)


def _fal_upload_jpeg_bytes(jpeg_bytes: bytes, api_key: str) -> str:
    """Upload bytes through fal client using sync APIs and return public URL."""
    if fal_client is None:
        raise RuntimeError(f"fal_client unavailable: {_FAL_CLIENT_IMPORT_ERROR or 'not installed'}")

    api_key = (api_key or "").strip()
    if api_key:
        os.environ["FAL_KEY"] = api_key

    temp_path = ""
    try:
        with tempfile.NamedTemporaryFile(delete=False, suffix=".jpg") as tmp:
            temp_path = tmp.name
            tmp.write(jpeg_bytes)

        for method_name in ("upload_file", "upload_image", "upload"):
            method = getattr(fal_client, method_name, None)
            if not callable(method):
                continue
            result = method(temp_path)
            if isinstance(result, str) and result.strip():
                return result.strip()
            if isinstance(result, dict):
                for key in ("url", "file_url"):
                    value = result.get(key)
                    if isinstance(value, str) and value.strip():
                        return value.strip()
        raise RuntimeError("fal_client upload returned no URL")
    finally:
        if temp_path and os.path.exists(temp_path):
            try:
                os.remove(temp_path)
            except OSError:
                pass


def upload_to_freeimage(
    image_path: str,
    max_size: int = 1200,
    progress_cb: ProgressCallback = None,
    api_key: Optional[str] = None,
) -> Tuple[Optional[str], Optional[Image.Image]]:
    """Upload image to freeimage.host, return (public_url, processed_pil_image).

    Resizes image if larger than max_size on longest side.
    Converts transparent images to RGB JPEG before upload.
    Returns (None, None) on failure.

    The returned PIL image is the exact image that was JPEG-encoded and uploaded,
    useful for downstream compositing without re-reading from disk.

    Args:
        api_key: Explicit freeimage key. Falls back to FREEIMAGE_API_KEY env var.
    """
    key = api_key or _FREEIMAGE_KEY
    if not key:
        if progress_cb:
            progress_cb("FREEIMAGE_API_KEY not set — set via environment or config", "error")
        return None, None

    try:
        jpeg_bytes, img = _prepare_image_for_upload(image_path=image_path, max_size=max_size)

        image_base64 = base64.b64encode(jpeg_bytes).decode("utf-8")

        if progress_cb:
            progress_cb(
                f"Uploading {Path(image_path).name} to freeimage.host...", "upload"
            )

        response = requests.post(
            "https://freeimage.host/api/1/upload",
            data={
                "key": key,
                "action": "upload",
                "source": image_base64,
                "format": "json",
            },
            timeout=30,
        )

        if response.status_code == 200:
            result = response.json()
            if result.get("status_code") == 200:
                url = result["image"]["url"]
                if progress_cb:
                    progress_cb(f"Uploaded: {url}", "upload")
                return url, img
            detail = str(result.get("status_txt") or result.get("error") or result)[:500]
            logger.error("Upload failed: API status_code=%s detail=%s", result.get("status_code"), detail)
            if progress_cb:
                progress_cb(f"Upload failed: {detail}", "error")
            return None, None

        detail = _extract_http_error_detail(response)
        logger.error("Upload failed: HTTP %s — %s", response.status_code, detail)
        if progress_cb:
            progress_cb(f"Upload failed: HTTP {response.status_code} — {detail}", "error")
        return None, None

    except Exception as e:
        logger.error("Upload error: %s", e)
        if progress_cb:
            progress_cb(f"Upload error: {e}", "error")
        return None, None


def upload_reference_image(
    image_path: str,
    fal_api_key: str,
    max_size: int = 1200,
    progress_cb: ProgressCallback = None,
    freeimage_api_key: Optional[str] = None,
) -> Tuple[Optional[str], Optional[Image.Image], Optional[str]]:
    """Upload reference image using fal CDN first, then fallback to freeimage."""
    try:
        if progress_cb:
            progress_cb(f"Preparing {Path(image_path).name} for upload...", "upload")
        jpeg_bytes, processed_img = _prepare_image_for_upload(image_path=image_path, max_size=max_size)
    except Exception as exc:
        logger.error("Failed to prepare image for upload: %s", exc)
        if progress_cb:
            progress_cb(f"Image preparation failed: {exc}", "error")
        return None, None, None

    try:
        if progress_cb:
            progress_cb("Uploading via fal CDN...", "upload")
        fal_url = _fal_upload_jpeg_bytes(jpeg_bytes=jpeg_bytes, api_key=fal_api_key)
        if progress_cb:
            progress_cb("Uploaded via fal CDN", "upload")
        return fal_url, processed_img, "fal_cdn"
    except Exception as fal_exc:
        fal_msg = str(fal_exc or "fal CDN upload failed")
        if _is_balance_lock_error(fal_msg):
            logger.error("fal CDN upload blocked by account state: %s", fal_msg)
            if progress_cb:
                progress_cb(f"fal CDN upload blocked: {fal_msg}", "error")
            return None, None, None
        logger.warning("fal CDN upload failed; falling back to freeimage: %s", fal_msg)
        if progress_cb:
            progress_cb(f"fal CDN upload failed, falling back: {fal_msg}", "warning")

    freeimage_url, freeimage_img = upload_to_freeimage(
        image_path=image_path,
        max_size=max_size,
        progress_cb=progress_cb,
        api_key=freeimage_api_key,
    )
    if freeimage_url:
        if progress_cb:
            progress_cb("Uploaded via freeimage fallback", "upload")
        return freeimage_url, freeimage_img, "freeimage"

    return None, None, None


def fal_queue_submit(
    api_key: str,
    endpoint: str,
    payload: dict,
    progress_cb: ProgressCallback = None,
) -> Optional[dict]:
    """Submit a job to fal.ai queue API.

    Returns dict with 'request_id' and 'status_url', or None on failure.
    Retries up to 3 times on transient errors.
    """
    url = f"https://queue.fal.run/{endpoint}"
    headers = {
        "Authorization": f"Key {api_key}",
        "Content-Type": "application/json",
    }

    max_retries = 3
    for attempt in range(max_retries):
        try:
            response = _post_with_auth_fallback(url, headers, payload, timeout=30)

            if response.status_code == 429:
                logger.warning("Rate limited — waiting 30 s before retry")
                if progress_cb:
                    progress_cb("Rate limited — waiting 30 s...", "warning")
                if _sleep_with_cancel(30, cancel_event=None, progress_cb=progress_cb):
                    return None
                continue

            elif response.status_code == 503:
                logger.warning("Service unavailable — retrying")
                if progress_cb:
                    progress_cb(
                        f"Service unavailable, retrying ({attempt + 1}/{max_retries})...",
                        "warning",
                    )
                if _sleep_with_cancel(10, cancel_event=None, progress_cb=progress_cb):
                    return None
                continue

            elif response.status_code == 402:
                logger.error("Payment required — insufficient fal.ai credits")
                if progress_cb:
                    progress_cb("Insufficient fal.ai credits", "error")
                return None

            elif response.status_code != 200:
                detail = _extract_http_error_detail(response)
                is_final_attempt = attempt >= (max_retries - 1)
                if is_final_attempt:
                    logger.error("Queue submit failed: %s — %s", response.status_code, detail)
                    if progress_cb:
                        progress_cb(
                            f"Submit failed: HTTP {response.status_code} — {detail}",
                            "error",
                        )
                else:
                    logger.warning(
                        "Queue submit attempt %d/%d failed: HTTP %s — %s",
                        attempt + 1,
                        max_retries,
                        response.status_code,
                        detail,
                    )
                    if progress_cb:
                        progress_cb(
                            f"Submit retrying ({attempt + 1}/{max_retries}) after HTTP {response.status_code}: {detail}",
                            "warning",
                        )
                if attempt < max_retries - 1:
                    if _sleep_with_cancel(5, cancel_event=None, progress_cb=progress_cb):
                        return None
                    continue
                return None

            result = response.json()
            request_id = result.get("request_id")
            status_url = result.get("status_url")

            if not request_id or not status_url:
                logger.error("No request_id or status_url in response")
                if progress_cb:
                    progress_cb("No request_id/status_url in response", "error")
                if attempt < max_retries - 1:
                    if _sleep_with_cancel(5, cancel_event=None, progress_cb=progress_cb):
                        return None
                    continue
                return None

            if progress_cb:
                progress_cb(f"Task created: {request_id}", "task")
            return result

        except requests.exceptions.Timeout:
            logger.warning("Request timeout (attempt %d/%d)", attempt + 1, max_retries)
            if attempt < max_retries - 1:
                if _sleep_with_cancel(10, cancel_event=None, progress_cb=progress_cb):
                    return None
                continue
            if progress_cb:
                progress_cb("Submit timed out", "error")
            return None

        except requests.exceptions.ConnectionError as e:
            logger.warning("Connection error: %s (attempt %d/%d)", e, attempt + 1, max_retries)
            if attempt < max_retries - 1:
                if _sleep_with_cancel(10, cancel_event=None, progress_cb=progress_cb):
                    return None
                continue
            if progress_cb:
                progress_cb(f"Connection error: {e}", "error")
            return None

        except Exception as e:
            logger.error("Unexpected submit error: %s", e)
            if attempt < max_retries - 1:
                if _sleep_with_cancel(5, cancel_event=None, progress_cb=progress_cb):
                    return None
                continue
            if progress_cb:
                progress_cb(f"Submit error: {e}", "error")
            return None

    return None


def fal_queue_poll(
    api_key: str,
    status_url: str,
    progress_cb: ProgressCallback = None,
    max_wait_seconds: int = 600,
    cancel_event: Optional[threading.Event] = None,
    provider: str = "fal",
    endpoint: str = "",
    request_id: str = "",
    operation_name: str = "Operation",
) -> Optional[dict]:
    """Poll fal.ai queue until completion.

    Uses the same exponential backoff as kling_generator_falai.py:
      - First 2 min: 5 s polls
      - Next 3 min: 10 s polls
      - After 5 min: 15 s polls

    Args:
        api_key: fal.ai API key.
        status_url: Queue status endpoint from initial submit response.
        progress_cb: Optional callback for user-facing status messages.
        max_wait_seconds: Maximum wall-clock seconds to poll before giving up.
            Default 600 (10 min) for video gen.  Callers doing image gen
            should pass a shorter value (e.g. 120 s).
        cancel_event: Optional threading.Event checked before each sleep.
            If set, polling returns None immediately.
        provider: Provider label for diagnostics.
        endpoint: Model endpoint id for diagnostics.
        request_id: Request id for diagnostics (last 8 chars shown).
        operation_name: Human-readable operation label used in status text.

    Returns the final result dict (output/data/images key) or None on failure.
    """
    status_headers = {"Authorization": f"Key {api_key}"}
    max_attempts = 240
    base_delay = 5
    consecutive_errors = 0
    max_consecutive_errors = 10
    start_time = time.monotonic()
    last_status = "UNKNOWN"
    safe_request = request_id[-8:] if request_id else "unknown"

    for attempt in range(1, max_attempts + 1):
        # Hard wall-clock timeout
        elapsed_s = time.monotonic() - start_time
        if elapsed_s >= max_wait_seconds:
            elapsed_min = int(elapsed_s / 60)
            logger.error("Polling timed out after %d s (%d min)", int(elapsed_s), elapsed_min)
            if progress_cb:
                progress_cb(
                    (
                        f"{operation_name} timeout ({provider}) after {int(elapsed_s)}s "
                        f"(cap={int(max_wait_seconds)}s, status={last_status}, "
                        f"endpoint={endpoint or 'unknown'}, req=*{safe_request}) "
                        f"reason=provider_timeout"
                    ),
                    "error",
                )
            return None

        # Cancellation check
        if cancel_event is not None and cancel_event.is_set():
            if progress_cb:
                progress_cb(
                    (
                        f"{operation_name} aborted by user ({provider}) "
                        f"(status={last_status}, endpoint={endpoint or 'unknown'}, req=*{safe_request}) "
                        f"reason=user_aborted"
                    ),
                    "warning",
                )
            return None

        # Backoff schedule
        if attempt <= 24:
            delay = base_delay
        elif attempt <= 60:
            delay = 10
        else:
            delay = 15

        if _sleep_with_cancel(delay, cancel_event=cancel_event, progress_cb=progress_cb):
            if progress_cb:
                progress_cb(
                    (
                        f"{operation_name} aborted by user ({provider}) "
                        f"(status={last_status}, endpoint={endpoint or 'unknown'}, req=*{safe_request}) "
                        f"reason=user_aborted"
                    ),
                    "warning",
                )
            return None

        # Periodic progress update
        if attempt % 12 == 0:
            elapsed_s_full = int(time.monotonic() - start_time)
            elapsed = int(elapsed_s_full / 60)
            if progress_cb:
                # Full diagnostic blob FIRST, at debug (terminal + file only).
                # It must precede the progress_update line below: a non-
                # progress_update log() ends the in-place row, so emitting the
                # debug line AFTER would close the row and the next heartbeat
                # would start a fresh one (re-spamming the panel). Debug-then-
                # progress_update keeps the growing row as the last line.
                progress_cb(
                    (
                        f"Still waiting... {elapsed} min elapsed "
                        f"[provider={provider} endpoint={endpoint or 'unknown'} req=*{safe_request} "
                        f"attempt={attempt} status={last_status} elapsed={elapsed_s_full}s cap={int(max_wait_seconds)}s]"
                    ),
                    "debug",
                )
                # v2.26 (user feedback 2026-06-07): show countdown
                # `elapsed/total` so the user can see how close we are to
                # the timeout instead of an open-ended counter. Same
                # progress_update level — the GUI still overwrites in
                # place.
                progress_cb(
                    f"{operation_name} — {last_status or 'IN_PROGRESS'} "
                    f"({elapsed_s_full}s / {int(max_wait_seconds)}s)",
                    "progress_update",
                )

        try:
            resp = _get_with_auth_fallback(status_url, status_headers, timeout=30)

            if resp.status_code == 404:
                logger.error("Job not found (404) — request may have expired")
                if progress_cb:
                    progress_cb("Job not found (404)", "error")
                return None

            elif resp.status_code == 429:
                logger.warning("Rate limited during polling — waiting 30 s")
                if _sleep_with_cancel(30, cancel_event=cancel_event, progress_cb=progress_cb):
                    return None
                continue

            elif resp.status_code == 503:
                consecutive_errors += 1
                if consecutive_errors >= max_consecutive_errors:
                    logger.error("Too many service errors — giving up")
                    if progress_cb:
                        progress_cb("Too many service errors", "error")
                    return None
                if _sleep_with_cancel(10, cancel_event=cancel_event, progress_cb=progress_cb):
                    return None
                continue

            elif resp.status_code not in (200, 202):
                consecutive_errors += 1
                detail = _extract_http_error_detail(resp)
                logger.warning(
                    "Polling returned HTTP %s (attempt %d/%d): %s",
                    resp.status_code, attempt, max_attempts, detail,
                )
                if progress_cb:
                    progress_cb(
                        f"Polling HTTP {resp.status_code}: {detail}",
                        "warning",
                    )
                if consecutive_errors >= max_consecutive_errors:
                    logger.error("Too many errors (%d) — giving up", consecutive_errors)
                    if progress_cb:
                        progress_cb(f"Too many errors, giving up", "error")
                    return None
                continue

            consecutive_errors = 0
            data = resp.json()
            status = data.get("status")
            if isinstance(status, str) and status:
                last_status = status

            if status in ("IN_QUEUE", "IN_PROGRESS"):
                continue

            elif status == "COMPLETED":
                if progress_cb:
                    progress_cb("Generation complete", "success")

                # Try to extract result — handle response_url indirection
                result = _extract_result(data, status_headers, progress_cb)
                return result

            elif status in ("FAILED", "ERROR"):
                error_msg = data.get("error", "Unknown error")
                logger.error("Generation failed: %s", error_msg)
                if progress_cb:
                    progress_cb(f"Generation failed: {error_msg}", "error")
                return None

            else:
                logger.debug("Unknown status: %s", status)

        except requests.exceptions.Timeout:
            logger.warning("Poll timeout on attempt %d", attempt)
            consecutive_errors += 1
            if consecutive_errors >= max_consecutive_errors:
                if progress_cb:
                    progress_cb("Too many poll timeouts", "error")
                return None

        except Exception as e:
            logger.error("Poll error on attempt %d: %s", attempt, e)
            consecutive_errors += 1
            if consecutive_errors >= max_consecutive_errors:
                if progress_cb:
                    progress_cb(f"Poll error: {e}", "error")
                return None

    logger.error("Polling timed out after %d attempts", max_attempts)
    if progress_cb:
        progress_cb("Polling timed out", "error")
    return None


def _unwrap_payload(data: dict) -> dict:
    """Unwrap nested output/data wrappers to find the payload with images/video.

    fal.ai responses may nest the actual result under 'output' or 'data' keys.
    This helper drills down to the innermost dict containing 'images' or 'video'.
    """
    if not isinstance(data, dict):
        return data
    if "video" in data or "images" in data:
        return data
    if "output" in data and isinstance(data.get("output"), dict):
        return _unwrap_payload(data["output"])
    if "data" in data and isinstance(data.get("data"), dict):
        return _unwrap_payload(data["data"])
    return data


def _extract_result(
    status_result: dict,
    status_headers: dict,
    progress_cb: ProgressCallback = None,
) -> Optional[dict]:
    """Extract the final result payload from a COMPLETED status response.

    Handles all the response structure variants seen in production:
    - output.video.url / output.images
    - video.url
    - data.video.url / data.images
    - response_url indirection (fetches the actual result)
    Returns the raw result dict so callers can inspect keys they care about.
    """
    # Structures 1-3: output / direct / data wrappers
    unwrapped = _unwrap_payload(status_result)
    if unwrapped is not status_result or "images" in unwrapped or "video" in unwrapped:
        return unwrapped

    # Structure 4: response_url indirection
    response_url = status_result.get("response_url")
    if response_url:
        if progress_cb:
            progress_cb("Fetching result from response_url...", "api")
        try:
            r = _get_with_auth_fallback(response_url, status_headers, timeout=30)
            if r.status_code == 200:
                result_data = r.json()
                # Check for API-level errors inside result
                if "error" in result_data:
                    logger.error("API error in response_url: %s", result_data["error"])
                    if progress_cb:
                        progress_cb(f"API error: {result_data['error']}", "error")
                    return None
                if "detail" in result_data:
                    detail = result_data["detail"]
                    if isinstance(detail, list):
                        for err in detail:
                            logger.error("Validation error: %s", err.get("msg", err))
                    else:
                        logger.error("API detail: %s", detail)
                    if progress_cb:
                        progress_cb("API validation error in result", "error")
                    return None
                # Unwrap nested wrappers in the fetched result too
                return _unwrap_payload(result_data)
            else:
                detail = _extract_http_error_detail(r)
                logger.error(
                    "response_url returned HTTP %s: %s",
                    r.status_code, detail,
                )
                # v2.28: self-heal hook for the future-model aspect-ratio
                # rejection case. When 422 carries an aspect_ratio
                # validation error, attach the accepted set to a
                # sentinel dict the caller (selfie_generator) can
                # inspect — and DO NOT emit the noisy "response_url
                # failed" error message; the caller will log the
                # retry attempt instead.
                aspect_allowed = parse_aspect_ratio_validation_error(r)
                if aspect_allowed:
                    return {
                        "__aspect_ratio_rejected__": True,
                        "allowed": sorted(aspect_allowed),
                        "detail": detail,
                    }
                if progress_cb:
                    progress_cb(
                        f"response_url failed: HTTP {r.status_code} — {detail}",
                        "error",
                    )
                return None
        except Exception as e:
            logger.warning("Failed to fetch response_url: %s", e)
            if progress_cb:
                progress_cb(f"Failed to fetch result: {e}", "error")
            return None

    logger.error("Could not extract result from COMPLETED response")
    if progress_cb:
        progress_cb("Could not extract result from completed response", "error")
    return None


# Auth-fallback STATUS CODES: 401 (unauthorized) + 403 (forbidden) are
# bona-fide auth failures where retrying with a different scheme makes
# sense. 422 was included in the v2.24 list, but v2.26 evidence (Kontext
# Max submit on 2026-06-07) showed 422 is a VALIDATION error (e.g.
# unsupported aspect_ratio value) — retrying with Bearer doesn't fix it,
# just masks the real error message with "bearer: unable to decode
# issuer" from the Bearer attempt. Real symptoms surface only when the
# fallback list excludes 422.
_AUTH_FALLBACK_STATUS = (401, 403)


# v2.28 PR (future-model self-heal): parse the accepted aspect-ratio
# set out of a fal.ai 422 validation message so the caller can retry
# with a corrected label. fal.ai's validation errors come in two
# shapes seen in production:
#   1. literal_error: {"detail":[{"loc":["body","aspect_ratio"],
#                                  "msg":"Input should be '21:9', '16:9'..."}]}
#   2. enum_error:    {"detail":[{"loc":["body","aspect_ratio"],
#                                  "msg":"value is not a valid enumeration member;
#                                         permitted: '21:9', '16:9', ..."}]}
# Both put the accepted labels as quoted strings in the message body.
# We return the parsed labels (or None when the response isn't an
# aspect_ratio validation error). Callers can then snap to the
# closest accepted label and re-submit.
_ASPECT_RATIO_LABEL_PATTERN = re.compile(r"'([0-9]+:[0-9]+)'")


def parse_aspect_ratio_validation_error(
    response: requests.Response,
) -> Optional[set]:
    """Return the accepted aspect-ratio label set when ``response`` is
    a 422 validation error pointing at ``aspect_ratio``, else None.

    Safe to call on any response: a non-422, or a 422 that's about a
    different field, returns None.
    """
    if response is None or response.status_code != 422:
        return None
    try:
        body = response.json()
    except ValueError:
        return None
    detail = body.get("detail") if isinstance(body, dict) else None
    if not isinstance(detail, list):
        return None
    for err in detail:
        if not isinstance(err, dict):
            continue
        loc = err.get("loc") or []
        if "aspect_ratio" not in loc:
            continue
        msg = err.get("msg", "")
        if not isinstance(msg, str):
            continue
        labels = set(_ASPECT_RATIO_LABEL_PATTERN.findall(msg))
        if labels:
            return labels
    return None


def _get_with_auth_fallback(url: str, headers: dict, timeout: int = 30) -> requests.Response:
    """GET with auth fallback for fal queue endpoints.

    Some fal queue/result URLs may reject `Key` auth while accepting `Bearer`.
    We keep `Key` as primary and retry once with `Bearer` on AUTH failures
    only (401 / 403). 422 validation errors pass through unmasked — see
    `_AUTH_FALLBACK_STATUS` comment for the v2.26 rationale.
    """
    resp = requests.get(url, headers=headers, timeout=timeout)
    auth_value = headers.get("Authorization", "")
    # Gemini PR #82 MED-1: a caller passing
    # ``headers={"Authorization": None}`` (or any non-string sentinel)
    # would AttributeError on ``.startswith``. Guard explicitly.
    if (
        resp.status_code in _AUTH_FALLBACK_STATUS
        and isinstance(auth_value, str)
        and auth_value.startswith("Key ")
    ):
        bearer_headers = dict(headers)
        bearer_headers["Authorization"] = auth_value.replace("Key ", "Bearer ", 1)
        return requests.get(url, headers=bearer_headers, timeout=timeout)
    return resp


def _post_with_auth_fallback(url: str, headers: dict, payload: dict, timeout: int = 30) -> requests.Response:
    """POST with auth fallback for fal queue submit endpoints.

    Same contract as `_get_with_auth_fallback` — retry only on 401 / 403
    (auth failures), never on 422 (validation).
    """
    resp = requests.post(url, headers=headers, json=payload, timeout=timeout)
    auth_value = headers.get("Authorization", "")
    # Gemini PR #82 MED-1: a caller passing
    # ``headers={"Authorization": None}`` (or any non-string sentinel)
    # would AttributeError on ``.startswith``. Guard explicitly.
    if (
        resp.status_code in _AUTH_FALLBACK_STATUS
        and isinstance(auth_value, str)
        and auth_value.startswith("Key ")
    ):
        bearer_headers = dict(headers)
        bearer_headers["Authorization"] = auth_value.replace("Key ", "Bearer ", 1)
        return requests.post(url, headers=bearer_headers, json=payload, timeout=timeout)
    return resp


def fal_download_file(
    url: str,
    output_path: str,
    progress_cb: ProgressCallback = None,
) -> bool:
    """Download a file from URL to output_path using streaming.

    Returns True on success, False on failure.
    """
    max_retries = 3
    for attempt in range(max_retries):
        try:
            with requests.get(url, stream=True, timeout=120) as resp:
                if resp.status_code != 200:
                    logger.warning(
                        "Download failed: HTTP %s (attempt %d/%d)",
                        resp.status_code, attempt + 1, max_retries,
                    )
                    if attempt < max_retries - 1:
                        time.sleep(5)
                        continue
                    if progress_cb:
                        progress_cb(
                            f"Download failed: HTTP {resp.status_code}", "error"
                        )
                    return False

                out_dir = os.path.dirname(os.path.abspath(output_path))
                os.makedirs(out_dir, exist_ok=True)
                tmp_path = output_path + ".tmp"
                try:
                    with open(tmp_path, "wb") as f:
                        for chunk in resp.iter_content(chunk_size=8192):
                            if chunk:
                                f.write(chunk)
                    os.replace(tmp_path, output_path)
                except BaseException:
                    # Clean up partial temp file on any failure
                    if os.path.exists(tmp_path):
                        os.remove(tmp_path)
                    raise

            file_size_mb = os.path.getsize(output_path) / (1024 * 1024)
            if progress_cb:
                progress_cb(f"Downloaded: {file_size_mb:.2f} MB", "download")
            logger.info("Downloaded %s (%.2f MB)", output_path, file_size_mb)
            return True

        except Exception as e:
            logger.warning(
                "Download error (attempt %d/%d): %s", attempt + 1, max_retries, e
            )
            if attempt < max_retries - 1:
                time.sleep(5)
                continue
            if progress_cb:
                progress_cb(f"Download error: {e}", "error")
            return False

    return False
