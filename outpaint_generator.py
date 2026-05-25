"""Outpaint (expand) images using fal.ai or BFL Expand API."""

import os
import math
import time
import base64
import logging
import tempfile
import threading
from io import BytesIO
from typing import Optional, Callable, Tuple
from pathlib import Path
from PIL import Image, ImageOps, ImageDraw
from outpaint_geometry import (
    compute_centered_aspect_expand_plan,
    compute_provider_caps,
)
from automation.config import get_outpaint_fal_timeout_seconds

logger = logging.getLogger(__name__)

# Safe env int loader: primary first, then fallback, then default.
def _read_int_env(primary: str, fallback: str, default: int) -> int:
    for key in (primary, fallback):
        raw = os.environ.get(key)
        if raw is None or str(raw).strip() == "":
            continue
        try:
            return int(raw)
        except (TypeError, ValueError):
            logger.warning("Invalid %s=%r; ignoring and trying fallback/default", key, raw)
    return default


def _read_single_env_int(key: str, default: int) -> int:
    """Single-key safe int loader. Mirrors _read_int_env but for keys
    that don't have a fallback alias. PR #53 round 3 — CodeRabbit
    flagged module-import-time `int(os.environ.get(...))` calls that
    crash module load on a malformed env var, disabling outpaint
    entirely. Falls back to *default* on TypeError/ValueError.

    PR #53 round 8 (CodeRabbit): also rejects non-positive values
    (``0``, negatives) — these are nonsensical for size/MP caps and
    would silently disable the pre-flight clamp downstream.
    """
    raw = os.environ.get(key)
    if raw is None or str(raw).strip() == "":
        return default
    try:
        parsed = int(raw)
    except (TypeError, ValueError):
        logger.warning("Invalid %s=%r; using default %d", key, raw, default)
        return default
    if parsed <= 0:
        logger.warning(
            "Non-positive %s=%r; using default %d", key, raw, default,
        )
        return default
    return parsed


def _read_single_env_float(key: str, default: float) -> float:
    """Single-key safe float loader. Same rationale as
    _read_single_env_int — and PR #53 round 8 also rejects NaN /
    +/-inf / non-positive values so a malformed env can never
    propagate as a math-poisoning cap downstream.
    """
    raw = os.environ.get(key)
    if raw is None or str(raw).strip() == "":
        return default
    try:
        parsed = float(raw)
    except (TypeError, ValueError):
        logger.warning("Invalid %s=%r; using default %g", key, raw, default)
        return default
    if not math.isfinite(parsed) or parsed <= 0:
        logger.warning(
            "Non-finite or non-positive %s=%r; using default %g",
            key, raw, default,
        )
        return default
    return parsed


# BFL polling limits (shared with selfie_generator pattern)
_BFL_MAX_WAIT_SECONDS = _read_int_env("BFL_MAX_WAIT_SECONDS", "BFL_EXPAND_MAX_WAIT_SECONDS", 30)
_BFL_POLL_INTERVAL = 5
_BFL_MAX_CONSECUTIVE_ERRORS = 3
_BFL_EXPAND_URL = "https://api.bfl.ai/v1/flux-pro-1.0-expand"
# BFL Expand output limits — BFL recommends ≤2MP for best results (help.bfl.ai).
# Override via env: BFL_EXPAND_MAX_DIM, BFL_EXPAND_MAX_MP
_BFL_MAX_CANVAS_DIM = _read_single_env_int("BFL_EXPAND_MAX_DIM", 2048)
_BFL_MAX_CANVAS_MP = _read_single_env_float("BFL_EXPAND_MAX_MP", 1.5)


class OutpaintGenerator:
    """Expand images using fal.ai outpaint."""

    ENDPOINT = "fal-ai/image-apps-v2/outpaint"

    # Empirical safe limits — fal.ai clamped 2782x3448 → 1232x1536 in testing.
    # Override via env: FAL_OUTPAINT_MAX_DIM, FAL_OUTPAINT_MAX_MP. Use the
    # safe helpers so a malformed env var falls back to the default instead
    # of crashing module import (CodeRabbit PR #53 round 3).
    _MAX_CANVAS_DIM = _read_single_env_int("FAL_OUTPAINT_MAX_DIM", 1536)
    _MAX_CANVAS_MP = _read_single_env_float("FAL_OUTPAINT_MAX_MP", 2.0)
    _PRESERVE_SEAM_BLEND_PX = 24
    _PRESERVE_SEAM_BLEND_STRENGTH = 0.55

    @staticmethod
    def _preflight_size(
        image_path: str,
        expand_left: int,
        expand_right: int,
        expand_top: int,
        expand_bottom: int,
        max_dim: int = 0,
        max_mp: float = 0.0,
    ) -> Tuple[int, int, int, int, int, int, int]:
        """Compute upload max_size + adjusted margins so total canvas fits API limits.

        Args:
            max_dim: Per-axis pixel cap (0 = use fal.ai class default).
            max_mp: Megapixel cap (0 = use fal.ai class default).

        Returns (max_size, adj_L, adj_R, adj_T, adj_B, simulated_img_w, simulated_img_h).
        """
        with Image.open(image_path) as img:
            img_t = ImageOps.exif_transpose(img)
            orig_w, orig_h = img_t.size

        MAX_DIM = max_dim if max_dim > 0 else OutpaintGenerator._MAX_CANVAS_DIM
        MAX_MP = max_mp if max_mp > 0 else OutpaintGenerator._MAX_CANVAS_MP

        def simulate_thumbnail(w: int, h: int, max_sz: int) -> Tuple[int, int]:
            if w > max_sz or h > max_sz:
                ratio = max_sz / max(w, h)
                return math.floor(w * ratio), math.floor(h * ratio)
            return w, h

        def scale_margin(m: int, s: float) -> int:
            return 0 if m == 0 else max(1, round(m * s))

        # Start at max_size=2048
        max_size = 2048
        img_w, img_h = simulate_thumbnail(orig_w, orig_h, max_size)

        canvas_w = img_w + expand_left + expand_right
        canvas_h = img_h + expand_top + expand_bottom

        # Deterministic scale: single min() across all constraints
        scale = min(
            MAX_DIM / canvas_w if canvas_w > MAX_DIM else 1.0,
            MAX_DIM / canvas_h if canvas_h > MAX_DIM else 1.0,
            math.sqrt(MAX_MP * 1_000_000 / (canvas_w * canvas_h))
            if (canvas_w * canvas_h) > MAX_MP * 1_000_000
            else 1.0,
            1.0,
        )

        if scale >= 1.0:
            return max_size, expand_left, expand_right, expand_top, expand_bottom, img_w, img_h

        # Scale from the simulated upload dimensions so preflight remains aligned
        # with the actual image that will be uploaded to fal.
        new_max_size = max(256, math.floor(max(img_w, img_h) * scale))
        adj_l = scale_margin(expand_left, scale)
        adj_r = scale_margin(expand_right, scale)
        adj_t = scale_margin(expand_top, scale)
        adj_b = scale_margin(expand_bottom, scale)

        # Deterministic correction: re-simulate and enforce MAX_DIM then MAX_MP
        img_w2, img_h2 = simulate_thumbnail(orig_w, orig_h, new_max_size)

        # Enforce MAX_DIM per axis (using original requested margins for ratio)
        h_sum = expand_left + expand_right
        v_sum = expand_top + expand_bottom
        if h_sum > 0 and (img_w2 + adj_l + adj_r) > MAX_DIM:
            s = (MAX_DIM - img_w2) / h_sum
            adj_l, adj_r = scale_margin(expand_left, s), scale_margin(expand_right, s)
        if v_sum > 0 and (img_h2 + adj_t + adj_b) > MAX_DIM:
            s = (MAX_DIM - img_h2) / v_sum
            adj_t, adj_b = scale_margin(expand_top, s), scale_margin(expand_bottom, s)

        # Enforce MAX_MP on the current (post-dim-correction) margins
        canvas_w2 = img_w2 + adj_l + adj_r
        canvas_h2 = img_h2 + adj_t + adj_b
        if (canvas_w2 * canvas_h2) > MAX_MP * 1_000_000:
            mp_scale = math.sqrt(MAX_MP * 1_000_000 / (canvas_w2 * canvas_h2))
            adj_l = 0 if adj_l == 0 else max(1, round(adj_l * mp_scale))
            adj_r = 0 if adj_r == 0 else max(1, round(adj_r * mp_scale))
            adj_t = 0 if adj_t == 0 else max(1, round(adj_t * mp_scale))
            adj_b = 0 if adj_b == 0 else max(1, round(adj_b * mp_scale))

        return new_max_size, adj_l, adj_r, adj_t, adj_b, img_w2, img_h2

    def __init__(self, api_key: str, freeimage_key: Optional[str] = None,
                 bfl_api_key: Optional[str] = None):
        self.api_key = api_key
        self._freeimage_key = freeimage_key
        self._bfl_api_key = bfl_api_key or ""
        self._progress_callback: Optional[Callable[[str, str], None]] = None
        self._last_outpaint_error_detail: str = ""

    def set_progress_callback(self, cb: Callable[[str, str], None]):
        self._progress_callback = cb

    def get_last_outpaint_error_detail(self) -> str:
        """Last error detail for latest serial outpaint/_bfl_outpaint call on this instance; not concurrency-safe."""
        return self._last_outpaint_error_detail

    @staticmethod
    def format_error_detail(detail: str) -> str:
        if not detail:
            return "Outpaint failed"
        reason = ""
        for token in detail.split():
            if token.startswith("reason="):
                reason = token.split("=", 1)[1].strip().lower()
                break
        reason_map = {
            "pending_timeout": "Outpaint failed (provider timed out)",
            "provider_failed": "Outpaint failed (provider returned failure)",
            "poll_error_limit": "Outpaint failed (provider polling errors)",
            "fal_failed_or_timed_out": "Outpaint failed (provider failed or timed out)",
        }
        return reason_map.get(reason, f"Outpaint failed ({detail})")

    def _set_last_outpaint_error_detail(self, detail: str) -> None:
        self._last_outpaint_error_detail = detail

    def _report(self, msg: str, level: str = "info"):
        if self._progress_callback:
            self._progress_callback(msg, level)

    def _normalize_image_for_upload(
        self,
        image_path: str,
        max_size: int,
    ) -> Image.Image:
        with Image.open(image_path) as img:
            img = ImageOps.exif_transpose(img)
            if img.width > max_size or img.height > max_size:
                img.thumbnail((max_size, max_size), Image.Resampling.LANCZOS)
            if img.mode in ("RGBA", "LA"):
                rgba = img.convert("RGBA")
                matte = Image.new("RGBA", rgba.size, self._ALPHA_MATTE_RGB + (255,))
                img = Image.alpha_composite(matte, rgba).convert("RGB")
            elif img.mode == "P":
                rgba = img.convert("RGBA")
                matte = Image.new("RGBA", rgba.size, self._ALPHA_MATTE_RGB + (255,))
                img = Image.alpha_composite(matte, rgba).convert("RGB")
            elif img.mode != "RGB":
                img = img.convert("RGB")
            return img.copy()

    def _prepare_processed_image(self, image_path: str, max_size: int) -> Image.Image:
        return self._normalize_image_for_upload(image_path, max_size)

    @staticmethod
    def _edge_seal_copy(src_img: Image.Image, edge_seal_px: int, color: Tuple[int, int, int]) -> Image.Image:
        sealed = src_img.copy().convert("RGB")
        if edge_seal_px <= 0:
            return sealed
        width, height = sealed.size
        if width <= 2 or height <= 2:
            return sealed
        seal_px = min(edge_seal_px, max(1, min(width, height) // 3))
        draw = ImageDraw.Draw(sealed)
        draw.rectangle([0, 0, width - 1, height - 1], outline=color, width=seal_px)
        return sealed

    def outpaint(
        self,
        image_path: str,
        output_folder: str,
        expand_left: int = 140,
        expand_right: int = 140,
        expand_top: int = 140,
        expand_bottom: int = 140,
        prompt: str = "",
        output_format: str = "png",
        composite_mode: str = "preserve_seamless",
        output_path: Optional[str] = None,
        provider: Optional[str] = None,
        document_mode: bool = False,
        edge_seal_px: int = 0,
        edge_seal_color: Tuple[int, int, int] = (220, 220, 220),
        poll_timeout_seconds: int = 150,
        cancel_event: Optional[threading.Event] = None,
    ) -> Optional[str]:
        """Outpaint (expand) an image.

        Args:
            image_path: Path to input image
            output_folder: Where to save output
            expand_left: Pixels to expand on the left
            expand_right: Pixels to expand on the right
            expand_top: Pixels to expand on the top
            expand_bottom: Pixels to expand on the bottom
            prompt: Optional guidance prompt
            output_format: Output format ("png" or "jpg")
            composite_mode: "preserve_seamless" (outside-only seam blend + exact center),
                "feathered" (legacy 3px blend), "hard" (pixel-perfect),
                or "none" (raw AI output)
            output_path: If provided, use this exact path instead of generating one
            poll_timeout_seconds: Maximum seconds to wait for async poll completion.
            cancel_event: Optional event to abort waiting and return early.

        Returns:
            Absolute path to expanded image, or None on failure.
        """
        self._set_last_outpaint_error_detail("")
        selected_provider = (provider or "auto").strip().lower()
        if selected_provider not in {"auto", "bfl", "fal"}:
            self._report(f"Invalid provider override: {provider}", "error")
            return None

        use_bfl = selected_provider == "bfl" or (selected_provider == "auto" and bool(self._bfl_api_key))
        if selected_provider == "bfl" and not self._bfl_api_key:
            self._report("Provider override set to bfl but no BFL key configured.", "error")
            return None

        if document_mode:
            try:
                with Image.open(image_path) as src_img:
                    src_w, src_h = ImageOps.exif_transpose(src_img).size
                caps = compute_provider_caps("bfl" if use_bfl else "fal")
                plan = compute_centered_aspect_expand_plan(
                    orig_w=src_w,
                    orig_h=src_h,
                    target_aspect=(3, 4),
                    caps=caps,
                )
                expand_left = int(plan["left"])
                expand_right = int(plan["right"])
                expand_top = int(plan["top"])
                expand_bottom = int(plan["bottom"])
                self._report(
                    f"Document mode plan -> L={expand_left} R={expand_right} T={expand_top} B={expand_bottom}",
                    "debug",
                )
            except Exception as exc:
                self._report(f"Document mode planning failed: {exc}", "warning")

        # Auto-select provider: BFL Expand if key available, else fal.ai
        if use_bfl:
            self._report("Using BFL Expand (FLUX Pro 1.0)", "info")
            return self._bfl_outpaint(
                image_path, output_folder,
                expand_left, expand_right, expand_top, expand_bottom,
                prompt, output_format, composite_mode,
                output_path=output_path,
                edge_seal_px=edge_seal_px,
                edge_seal_color=edge_seal_color,
                cancel_event=cancel_event,
            )
        self._report("Using fal.ai outpaint", "info")

        from fal_utils import (
            upload_reference_image,
            fal_queue_submit,
            fal_queue_poll,
            fal_download_file,
        )

        # Pre-flight: compute safe upload size + margins
        max_upload_size, adj_left, adj_right, adj_top, adj_bottom, sim_w, sim_h = (
            self._preflight_size(image_path, expand_left, expand_right, expand_top, expand_bottom)
        )
        pre_canvas_w = sim_w + adj_left + adj_right
        pre_canvas_h = sim_h + adj_top + adj_bottom

        self._report(
            f"Pre-flight: upload_max={max_upload_size}px, "
            f"img\u2248{sim_w}x{sim_h}, margins L={adj_left} R={adj_right} T={adj_top} B={adj_bottom}, "
            f"canvas\u2248{pre_canvas_w}x{pre_canvas_h} "
            f"(safe envelope: {self._MAX_CANVAS_DIM}px / {self._MAX_CANVAS_MP}MP)",
            "debug",
        )
        if max_upload_size < 2048:
            self._report(
                f"Pre-flight: scaled down to fit API limits "
                f"(margins L={expand_left}\u2192{adj_left} R={expand_right}\u2192{adj_right} "
                f"T={expand_top}\u2192{adj_top} B={expand_bottom}\u2192{adj_bottom})",
                "progress",
            )

        # Upload with pre-flight max_size.
        # Edge seal, when enabled, is only applied to upload copy.
        self._report("Uploading image for outpainting...", "upload")
        processed_img = self._prepare_processed_image(image_path=image_path, max_size=max_upload_size)
        upload_path = image_path
        temp_upload_path = None
        if edge_seal_px > 0:
            sealed_upload = self._edge_seal_copy(processed_img, edge_seal_px, edge_seal_color)
            fd, temp_upload_path = tempfile.mkstemp(suffix=".jpg")
            os.close(fd)
            sealed_upload.save(temp_upload_path, format="JPEG", quality=90)
            upload_path = temp_upload_path
            self._report(f"Applied upload-only edge seal ({edge_seal_px}px).", "debug")

        image_url = None
        uploaded_processed_img = None
        uploaded_provider = None
        try:
            image_url, uploaded_processed_img, uploaded_provider = upload_reference_image(
                image_path=upload_path,
                fal_api_key=self.api_key,
                max_size=max_upload_size,
                progress_cb=self._progress_callback,
                freeimage_api_key=self._freeimage_key,
            )
        finally:
            if temp_upload_path and os.path.exists(temp_upload_path):
                try:
                    os.remove(temp_upload_path)
                except OSError:
                    pass
        if not image_url:
            self._report("Failed to upload image", "error")
            return None
        if uploaded_provider:
            self._report(f"Reference upload provider: {uploaded_provider}", "upload")

        if edge_seal_px > 0:
            composite_source = processed_img
            self._report(
                "Composite source: unsealed processed image (edge seal upload-only, intentionally excluded from final paste)",
                "debug",
            )
        elif uploaded_processed_img is not None:
            composite_source = uploaded_processed_img.convert("RGB")
            self._report("Composite source: uploaded_processed_img (exact decoded upload)", "debug")
        else:
            composite_source = processed_img
            self._report("Composite source: local processed image fallback (no uploaded_processed_img)", "debug")

        expected_canvas_w = composite_source.width + adj_left + adj_right
        expected_canvas_h = composite_source.height + adj_top + adj_bottom
        self._report(
            (
                "Fal composite precheck: "
                f"requested=L{expand_left}/R{expand_right}/T{expand_top}/B{expand_bottom} "
                f"adjusted=L{adj_left}/R{adj_right}/T{adj_top}/B{adj_bottom} "
                f"upload_max={max_upload_size} "
                f"composite_source={composite_source.width}x{composite_source.height} "
                f"expected_canvas={expected_canvas_w}x{expected_canvas_h}"
            ),
            "debug",
        )

        # Build payload — zoom_out_percentage=0 prevents hidden 20% default shrink
        payload = {
            "image_url": image_url,
            "expand_left": adj_left,
            "expand_right": adj_right,
            "expand_top": adj_top,
            "expand_bottom": adj_bottom,
            "zoom_out_percentage": 0,
            "num_images": 1,
            "output_format": output_format,
        }
        if prompt.strip():
            payload["prompt"] = prompt.strip()

        self._report(
            f"Submitting outpaint (L={adj_left} R={adj_right} "
            f"T={adj_top} B={adj_bottom})...",
            "task",
        )

        # Submit to queue
        result = fal_queue_submit(
            self.api_key, self.ENDPOINT, payload, self._progress_callback
        )
        if not result:
            self._report("Failed to submit outpaint job", "error")
            return None

        status_url = result.get("status_url")
        if not status_url:
            self._report("No status URL in response", "error")
            return None
        request_id = str(result.get("request_id", "") or "")
        safe_request = request_id[-8:] if request_id else "unknown"
        timeout_seconds = get_outpaint_fal_timeout_seconds(
            {"outpaint_fal_timeout_seconds": poll_timeout_seconds}
        )
        self._report(
            f"Queue watch: provider=fal endpoint={self.ENDPOINT} req=*{safe_request} timeout={timeout_seconds}s",
            "debug",
        )

        self._report("Waiting for outpaint...", "progress")
        final = fal_queue_poll(
            self.api_key,
            status_url,
            self._progress_callback,
            max_wait_seconds=timeout_seconds,
            cancel_event=cancel_event,
            provider="fal",
            endpoint=self.ENDPOINT,
            request_id=request_id,
            operation_name="Outpaint",
        )
        if not final:
            if cancel_event is not None and cancel_event.is_set():
                return None
            detail = "reason=fal_failed_or_timed_out"
            self._set_last_outpaint_error_detail(detail)
            self._report(f"Outpaint failed or timed out ({detail})", "error")
            return None

        # Extract image URL from result
        images = final.get("images", [])
        if not images:
            self._report("No images in result", "error")
            return None

        image_url_result = images[0].get("url") if isinstance(images[0], dict) else images[0]
        if not image_url_result:
            self._report("No image URL in result", "error")
            return None

        # Build output path (unique) — skip if caller provided one
        os.makedirs(output_folder, exist_ok=True)
        if output_path is None:
            stem = Path(image_path).stem
            ext = f".{output_format}"
            output_path = os.path.join(output_folder, f"{stem}-expanded{ext}")
            counter = 1
            while os.path.exists(output_path):
                output_path = os.path.join(
                    output_folder, f"{stem}-expanded_v{counter}{ext}"
                )
                counter += 1

        # Best-effort cost estimate from fal.ai pricing catalog
        try:
            from model_schema_manager import ModelSchemaManager
            mgr = ModelSchemaManager(self.api_key)
            pricing = mgr.get_model_pricing(self.ENDPOINT)
            if pricing:
                unit_price = pricing.get("unit_price")
                unit = pricing.get("unit", "request")
                if unit_price is not None:
                    self._report(f"fal.ai cost: ~${unit_price:.4f}/{unit}", "info")
        except Exception:
            pass

        self._report("Downloading result...", "download")
        if not fal_download_file(image_url_result, output_path, self._progress_callback):
            self._report("Download failed", "error")
            return None

        # Read the downloaded dimensions. A genuinely unreadable file
        # (PIL raises) is an IO failure, not underflow — reject it.
        try:
            with Image.open(output_path) as downloaded_img:
                downloaded_w, downloaded_h = downloaded_img.size
        except Exception as exc:
            self._report(
                f"Could not read downloaded outpaint output: {exc}", "error"
            )
            self._set_last_outpaint_error_detail(
                f"download_unreadable:{type(exc).__name__}"
            )
            try:
                os.unlink(output_path)
            except OSError:
                pass
            return None

        # PR #53 round 9 — Codex P2: `composite_mode="none"` explicitly
        # asks for the raw provider output. The always-composite path
        # below reopens `image_path` to build `orig_full`; if that
        # reopen failed (source moved or cleaned up between upload and
        # download), the code would delete the perfectly-good downloaded
        # output and return None — a regression for raw-output mode
        # which never needed the source image in the first place.
        # Short-circuit here so "none" mode goes back to its pre-PR
        # behavior: download, accept, done.
        if composite_mode == "none":
            self._report(
                "Composite: none — using raw AI output "
                f"({downloaded_w}x{downloaded_h})",
                "progress",
            )
            return output_path

        # Always-composite contract (PR fix/step0-composite-and-rppg-v2.5):
        # the previous code silently returned the raw fal output whenever
        # the provider returned a canvas smaller than preflight expected
        # (fal.ai's silent clamp produces 1-2% underflow on most calls).
        # The "preserve seamless" contract is the entire point of the
        # composite — skipping it ships a non-composited result and
        # corrupts every downstream stage. Per user: always do the
        # composite; resize to whatever fal could deliver.
        #
        # Strategy: composite in the FULL-RES coordinate system, not the
        # downscaled one. We load the original full-res image as the paste
        # source, compute the full-res target canvas from the user's
        # original (un-adjusted) margins, and resize the fal output up to
        # match. This way:
        #   * the pasted center pixels are byte-identical to the user's
        #     input (no double downscale → upscale round-trip),
        #   * the matchTemplate alignment runs on the full-res image,
        #   * the surrounding generated area is one Lanczos pass off the
        #     fal output (visually fine).
        # EXIF orientation: preflight + _prepare_processed_image both
        # apply ImageOps.exif_transpose so the upload + provider canvas
        # are sized in the post-rotation coordinate system. The full-res
        # original must use the SAME normalization or a phone-camera
        # portrait photo would have orig_full sized in the wrong axis vs
        # the downloaded canvas (Codex P1, PR #53). Mirrors line 76 + 189.
        try:
            with Image.open(image_path) as src:
                # ImageOps.exif_transpose returns a NEW image (not a
                # view into `src`), and .convert("RGB") returns
                # another new image — both materialise pixels, so the
                # trailing .copy() that the round-1 fix added is
                # redundant. Removed per Gemini PR #53 round 9 nit.
                orig_full = ImageOps.exif_transpose(src).convert("RGB")
        except Exception as exc:
            self._report(
                f"Could not re-open source image for full-res composite: {exc}",
                "error",
            )
            self._set_last_outpaint_error_detail(
                f"orig_reopen_failed:{type(exc).__name__}"
            )
            try:
                os.unlink(output_path)
            except OSError:
                pass
            return None

        # Preserve the unclamped dimensions so the M1 clamp log can
        # show "<unclamped> -> <clamped>" cleanly. PR #53 round 6
        # (reviewer): the prior log reconstructed the unclamped width
        # via `int(_final_mp * 1_000_000 / max(1, final_canvas_h))`,
        # which is mathematically correct but unnecessarily indirect.
        unclamped_w = orig_full.width + expand_left + expand_right
        unclamped_h = orig_full.height + expand_top + expand_bottom
        final_canvas_w = unclamped_w
        final_canvas_h = unclamped_h
        # Subagent M1 round 5: clamp the final canvas to a sane envelope
        # so a user that types huge pixel margins doesn't OOM the
        # process via PIL's LANCZOS resize (it allocates the
        # destination buffer up-front). 100 MP is comfortably bigger
        # than any realistic Step 0 expand (which targets fal's
        # 1536px / 2.0 MP envelope after preflight scale-down anyway)
        # but small enough that the resize completes in a couple of
        # seconds on the macOS CPU path.
        #
        # When the canvas is clamped, the orig_full + scaled margins
        # math must still hold so the composite's matchTemplate
        # alignment works — so we proportionally downscale orig_full
        # AND the margins together. The center-pixel preservation
        # contract is then held against the scaled original (still
        # higher quality than the previous downscaled-upload paste,
        # just not raw input quality at extreme margin sizes).
        _FINAL_CANVAS_MP_CAP = 100.0
        _final_mp = (unclamped_w * unclamped_h) / 1_000_000.0
        composite_margins = (
            expand_left, expand_right, expand_top, expand_bottom,
        )
        if _final_mp > _FINAL_CANVAS_MP_CAP:
            # math is already imported at module top; no need for a
            # local-alias re-import (Gemini PR #53 round 7 cleanup).
            _scale = math.sqrt(_FINAL_CANVAS_MP_CAP / _final_mp)
            scaled_orig_w = max(1, int(orig_full.width * _scale))
            scaled_orig_h = max(1, int(orig_full.height * _scale))
            try:
                orig_full = orig_full.resize(
                    (scaled_orig_w, scaled_orig_h),
                    Image.Resampling.LANCZOS,
                )
            except Exception as exc:
                self._report(
                    f"Could not scale orig_full to clamped envelope: {exc}",
                    "error",
                )
                self._set_last_outpaint_error_detail(
                    f"orig_clamp_failed:{type(exc).__name__}"
                )
                try:
                    os.unlink(output_path)
                except OSError:
                    pass
                return None
            composite_margins = (
                max(0, int(expand_left * _scale)),
                max(0, int(expand_right * _scale)),
                max(0, int(expand_top * _scale)),
                max(0, int(expand_bottom * _scale)),
            )
            final_canvas_w = scaled_orig_w + composite_margins[0] + composite_margins[1]
            final_canvas_h = scaled_orig_h + composite_margins[2] + composite_margins[3]
            self._report(
                (
                    f"Final canvas {unclamped_w}x{unclamped_h} "
                    f"({_final_mp:.1f} MP) -> {final_canvas_w}x{final_canvas_h} "
                    f"({_FINAL_CANVAS_MP_CAP:.0f} MP envelope clamp); "
                    f"orig_full + margins proportionally scaled by "
                    f"{_scale:.3f}. If you need the full-margin canvas "
                    "at original resolution, reduce the expand margins "
                    "or chain a second pass (Run 2x)."
                ),
                "warning",
            )
        self._report(
            (
                "Fal composite downloaded result: "
                f"actual={downloaded_w}x{downloaded_h} "
                f"provider_send_target={expected_canvas_w}x{expected_canvas_h} "
                f"final_canvas={final_canvas_w}x{final_canvas_h}"
            ),
            "debug",
        )

        if (downloaded_w, downloaded_h) != (final_canvas_w, final_canvas_h):
            try:
                with Image.open(output_path) as dl:
                    dl_rgb = dl.convert("RGB")
                    resized = dl_rgb.resize(
                        (final_canvas_w, final_canvas_h),
                        Image.Resampling.LANCZOS,
                    )
                save_kwargs = (
                    {"quality": 95}
                    if output_format.lower() in ("jpg", "jpeg")
                    else {}
                )
                resized.save(output_path, **save_kwargs)
                self._report(
                    (
                        f"Composite: upscaled fal output "
                        f"{downloaded_w}x{downloaded_h} -> "
                        f"{final_canvas_w}x{final_canvas_h} (Lanczos) to "
                        "preserve original-pixel composite contract"
                    ),
                    "info",
                )
            except Exception as exc:
                self._report(
                    f"Could not resize fal output to final canvas: {exc}",
                    "error",
                )
                self._set_last_outpaint_error_detail(
                    f"resize_failed:{type(exc).__name__}"
                )
                try:
                    os.unlink(output_path)
                except OSError:
                    pass
                return None

        # composite_margins matches orig_full (both untouched by default,
        # both scaled together if the canvas was clamped above for M1).
        composite_ok = self._composite_onto_result(
            output_path, orig_full,
            composite_margins[0], composite_margins[1],
            composite_margins[2], composite_margins[3],
            output_format, composite_mode,
        )

        if not composite_ok and composite_mode in {
            "preserve_seamless", "hard", "feathered",
        }:
            self._report(
                "Composite FAILED for preserve mode — output rejected. "
                "The pre-composited fal output has been removed; re-run "
                "the expand or check upstream image dimensions.",
                "error",
            )
            self._set_last_outpaint_error_detail("composite_failed")
            try:
                os.unlink(output_path)
            except OSError:
                pass
            return None

        return output_path

    # ── Shared composite ─────────────────────────────────────────────────

    def _composite_onto_result(
        self,
        output_path: str,
        orig: Image.Image,
        margin_left: int,
        margin_right: int,
        margin_top: int,
        margin_bottom: int,
        output_format: str,
        composite_mode: str,
    ) -> bool:
        """Composite *orig* over the AI output at *output_path*.

        Returns ``True`` when the composite was applied (preserve contract
        held) and ``False`` when any bail-out branch was hit
        (mode="none" placement guard, "Original doesn't fit", or an
        unexpected exception). Callers MUST treat ``False`` as a failed
        pass for preserve modes — see the PR fix/step0-composite-and-rppg-v2.5
        addition in :meth:`outpaint`.
        """
        if composite_mode == "none":
            self._report("Composite: none — using raw AI output", "progress")
            return False

        try:
            from PIL import ImageFilter, ImageDraw

            self._report(f"Compositing original over AI result (mode={composite_mode})...", "debug")
            # Close the source handle before later result_img.save() writes
            # back to the same path — Gemini PR #53 round 5 HIGH. On
            # Windows the open handle from PIL's lazy decoder can cause
            # PermissionError when the same path is reopened for write.
            with Image.open(output_path) as _src:
                result_img = _src.convert("RGB")
            orig_rgb = orig.convert("RGB")

            # --- 1. INITIAL MATH ESTIMATE ---
            expected_w = orig.width + margin_left + margin_right
            expected_h = orig.height + margin_top + margin_bottom
            actual_w, actual_h = result_img.size

            if (actual_w == expected_w) and (actual_h == expected_h):
                math_left, math_top = margin_left, margin_top
            else:
                total_h_margin = actual_w - orig.width
                total_v_margin = actual_h - orig.height
                h_sum = margin_left + margin_right
                v_sum = margin_top + margin_bottom
                math_left = round(total_h_margin * margin_left / h_sum) if h_sum > 0 else total_h_margin // 2
                math_top = round(total_v_margin * margin_top / v_sum) if v_sum > 0 else total_v_margin // 2

            # --- 2. EXACT ALIGNMENT (Fixing VAE Shift) ---
            paste_left, paste_top = math_left, math_top
            try:
                import cv2
                import numpy as np

                orig_cv = cv2.cvtColor(np.array(orig_rgb), cv2.COLOR_RGB2BGR)
                res_cv = cv2.cvtColor(np.array(result_img), cv2.COLOR_RGB2BGR)

                search_margin = 15
                search_x1 = max(0, math_left - search_margin)
                search_y1 = max(0, math_top - search_margin)
                search_x2 = min(res_cv.shape[1], math_left + orig.width + search_margin)
                search_y2 = min(res_cv.shape[0], math_top + orig.height + search_margin)

                search_area = res_cv[search_y1:search_y2, search_x1:search_x2]
                if (
                    search_area.shape[0] >= orig_cv.shape[0]
                    and search_area.shape[1] >= orig_cv.shape[1]
                ):
                    match = cv2.matchTemplate(search_area, orig_cv, cv2.TM_CCOEFF_NORMED)
                    _, _, _, max_loc = cv2.minMaxLoc(match)
                    paste_left = search_x1 + max_loc[0]
                    paste_top = search_y1 + max_loc[1]
                    if (paste_left != math_left) or (paste_top != math_top):
                        self._report(
                            f"Auto-aligned paste shifted by X:{paste_left-math_left} Y:{paste_top-math_top}px to fix VAE drift",
                            "debug",
                        )
                else:
                    self._report(
                        "Auto-align skipped (search window smaller than original), using mathematical placement",
                        "warning",
                    )
            except Exception as e:
                self._report(
                    f"Auto-align unavailable ({e}), falling back to mathematical placement",
                    "warning",
                )

            # Safety guard — if the placement rect doesn't fit inside the
            # AI result, the composite would crash on .paste(). This is a
            # FAILURE for preserve modes: returning False signals the
            # caller to reject the output (we no longer silently ship
            # raw AI output as "success" — see the PR
            # fix/step0-composite-and-rppg-v2.5 caller change).
            if (
                (paste_left < 0)
                or (paste_top < 0)
                or (paste_left + orig.width > actual_w)
                or (paste_top + orig.height > actual_h)
            ):
                self._report(
                    "Original doesn't fit in AI result — composite ABORTED "
                    f"(orig={orig.width}x{orig.height}, "
                    f"AI canvas={actual_w}x{actual_h}, "
                    f"placement=({paste_left},{paste_top}))",
                    "error",
                )
                return False

            paste_right = paste_left + orig.width
            paste_bottom = paste_top + orig.height
            scaled_before_composite = (actual_w != expected_w) or (actual_h != expected_h)
            self._report(
                f"Composite placement rect=({paste_left},{paste_top})..({paste_right},{paste_bottom}), "
                f"scaled_before_composite={scaled_before_composite}",
                "debug",
            )

            def _calc_boundary_discontinuity(img: Image.Image) -> dict:
                metrics = {}

                def _avg_abs_diff(pairs):
                    if not pairs:
                        return None
                    total = 0
                    count = 0
                    for a, b in pairs:
                        total += abs(a[0] - b[0]) + abs(a[1] - b[1]) + abs(a[2] - b[2])
                        count += 3
                    return round(total / count, 4) if count else None

                top_pairs = []
                if paste_top > 0:
                    for x in range(paste_left, paste_right):
                        top_pairs.append((img.getpixel((x, paste_top)), img.getpixel((x, paste_top - 1))))
                metrics["top"] = _avg_abs_diff(top_pairs)

                bottom_pairs = []
                if paste_bottom < actual_h:
                    for x in range(paste_left, paste_right):
                        bottom_pairs.append((img.getpixel((x, paste_bottom - 1)), img.getpixel((x, paste_bottom))))
                metrics["bottom"] = _avg_abs_diff(bottom_pairs)

                left_pairs = []
                if paste_left > 0:
                    for y in range(paste_top, paste_bottom):
                        left_pairs.append((img.getpixel((paste_left, y)), img.getpixel((paste_left - 1, y))))
                metrics["left"] = _avg_abs_diff(left_pairs)

                right_pairs = []
                if paste_right < actual_w:
                    for y in range(paste_top, paste_bottom):
                        right_pairs.append((img.getpixel((paste_right - 1, y)), img.getpixel((paste_right, y))))
                metrics["right"] = _avg_abs_diff(right_pairs)
                return metrics

            seam_debug_enabled = (
                os.environ.get("OUTPAINT_SEAM_DEBUG", "0").strip().lower()
                in {"1", "true", "yes", "on"}
            )

            # --- 3. APPLY COMPOSITE MODE ---
            if composite_mode == "hard":
                result_img.paste(orig_rgb, (paste_left, paste_top))
                self._report("Hard composite applied (no feather)", "progress")
            elif composite_mode == "preserve_seamless":
                seam_blend_px = max(1, int(self._PRESERVE_SEAM_BLEND_PX))
                seam_blend_strength = float(self._PRESERVE_SEAM_BLEND_STRENGTH)
                seam_blend_strength = max(0.0, min(1.0, seam_blend_strength))
                self._report(
                    (
                        "Preserve seamless blend: "
                        f"seam_blend_px={seam_blend_px} "
                        f"strength={seam_blend_strength:.2f} "
                        f"debug_metrics={seam_debug_enabled}"
                    ),
                    "debug",
                )
                before_metrics = _calc_boundary_discontinuity(result_img) if seam_debug_enabled else None

                left_ring_x0 = max(0, paste_left - seam_blend_px)
                right_ring_x1 = min(actual_w, paste_right + seam_blend_px)
                top_ring_y0 = max(0, paste_top - seam_blend_px)
                bottom_ring_y1 = min(actual_h, paste_bottom + seam_blend_px)
                self._report(
                    (
                        "Preserve seamless ring bounds: "
                        f"x={left_ring_x0}..{right_ring_x1} "
                        f"y={top_ring_y0}..{bottom_ring_y1}"
                    ),
                    "debug",
                )

                overlay = Image.new("RGB", result_img.size, (0, 0, 0))
                alpha = Image.new("L", result_img.size, 0)
                alpha_draw = ImageDraw.Draw(alpha)
                blur_radius = max(1, seam_blend_px // 4)

                def _alpha_value(ratio: float) -> int:
                    return int(255 * seam_blend_strength * max(0.0, min(1.0, ratio)))

                def _build_top_bottom_band(is_top: bool, band_w: int, band_h: int) -> Image.Image:
                    band = Image.new("RGB", (band_w, band_h))
                    inner_x = paste_left - left_ring_x0
                    center_w = max(0, min(orig.width, band_w - inner_x))

                    y0 = 0 if is_top else (orig.height - 1)

                    if center_w > 0:
                        center_strip = orig_rgb.crop((0, y0, orig.width, y0 + 1)).resize(
                            (center_w, band_h), Image.Resampling.BILINEAR
                        )
                        band.paste(center_strip, (inner_x, 0))

                    if inner_x > 0:
                        left_strip = orig_rgb.crop((0, y0, 1, y0 + 1)).resize(
                            (inner_x, band_h), Image.Resampling.BILINEAR
                        )
                        band.paste(left_strip, (0, 0))

                    right_fill_x0 = inner_x + center_w
                    if right_fill_x0 < band_w:
                        right_strip = orig_rgb.crop((orig.width - 1, y0, orig.width, y0 + 1)).resize(
                            (band_w - right_fill_x0, band_h), Image.Resampling.BILINEAR
                        )
                        band.paste(right_strip, (right_fill_x0, 0))

                    return band.filter(ImageFilter.GaussianBlur(radius=blur_radius))

                # Top band (outside-only, full ring width including corners)
                top_h = paste_top - top_ring_y0
                top_w = right_ring_x1 - left_ring_x0
                if top_h > 0 and top_w > 0:
                    top_band = _build_top_bottom_band(is_top=True, band_w=top_w, band_h=top_h)
                    overlay.paste(top_band, (left_ring_x0, top_ring_y0))
                    for row in range(top_h):
                        ratio = (row + 1) / top_h
                        alpha_draw.line(
                            [(left_ring_x0, top_ring_y0 + row), (right_ring_x1 - 1, top_ring_y0 + row)],
                            fill=_alpha_value(ratio),
                        )

                # Bottom band (outside-only, full ring width including corners)
                bottom_h = bottom_ring_y1 - paste_bottom
                bottom_w = right_ring_x1 - left_ring_x0
                if bottom_h > 0 and bottom_w > 0:
                    bottom_band = _build_top_bottom_band(is_top=False, band_w=bottom_w, band_h=bottom_h)
                    overlay.paste(bottom_band, (left_ring_x0, paste_bottom))
                    for row in range(bottom_h):
                        ratio = (bottom_h - row) / bottom_h
                        alpha_draw.line(
                            [(left_ring_x0, paste_bottom + row), (right_ring_x1 - 1, paste_bottom + row)],
                            fill=_alpha_value(ratio),
                        )

                # Left strip (outside-only center band)
                left_w = paste_left - left_ring_x0
                if left_w > 0 and paste_bottom > paste_top:
                    strip = orig_rgb.crop((0, 0, 1, orig.height)).resize(
                        (left_w, orig.height), Image.Resampling.BILINEAR
                    )
                    strip = strip.filter(ImageFilter.GaussianBlur(radius=blur_radius))
                    overlay.paste(strip, (left_ring_x0, paste_top))
                    for col in range(left_w):
                        ratio = (col + 1) / left_w
                        alpha_draw.line(
                            [(left_ring_x0 + col, paste_top), (left_ring_x0 + col, paste_bottom - 1)],
                            fill=_alpha_value(ratio),
                        )

                # Right strip (outside-only center band)
                right_w = right_ring_x1 - paste_right
                if right_w > 0 and paste_bottom > paste_top:
                    strip = orig_rgb.crop((orig.width - 1, 0, orig.width, orig.height)).resize(
                        (right_w, orig.height), Image.Resampling.BILINEAR
                    )
                    strip = strip.filter(ImageFilter.GaussianBlur(radius=blur_radius))
                    overlay.paste(strip, (paste_right, paste_top))
                    for col in range(right_w):
                        ratio = (right_w - col) / right_w
                        alpha_draw.line(
                            [(paste_right + col, paste_top), (paste_right + col, paste_bottom - 1)],
                            fill=_alpha_value(ratio),
                        )

                # Blend only outside seam ring, then hard-paste center for exact preservation.
                result_img.paste(overlay, (0, 0), alpha)
                result_img.paste(orig_rgb, (paste_left, paste_top))
                self._report("Preserve seamless exact center preserved=True (final hard paste)", "debug")
                if seam_debug_enabled:
                    after_metrics = _calc_boundary_discontinuity(result_img)
                    preserved_exact = (
                        result_img.crop((paste_left, paste_top, paste_right, paste_bottom)).tobytes()
                        == orig_rgb.tobytes()
                    )
                    self._report(f"Preserve seamless exact center preserved={preserved_exact}", "debug")
                    self._report(
                        f"Boundary discontinuity avg abs RGB diff before={before_metrics} after={after_metrics}",
                        "debug",
                    )
            else:
                if composite_mode != "feathered":
                    self._report(
                        f"Unknown composite mode '{composite_mode}', falling back to feathered",
                        "warning",
                    )
                feather_px = 3
                mask = Image.new("L", orig.size, 0)
                ImageDraw.Draw(mask).rectangle(
                    [
                        feather_px,
                        feather_px,
                        orig.width - feather_px - 1,
                        orig.height - feather_px - 1,
                    ],
                    fill=255,
                )
                mask = mask.filter(ImageFilter.GaussianBlur(radius=feather_px))
                result_img.paste(orig_rgb, (paste_left, paste_top), mask=mask)
                self._report(f"Tight feathered blend applied (feather={feather_px}px)", "progress")

            save_kwargs = {"quality": 95} if output_format.lower() in ("jpg", "jpeg") else {}
            result_img.save(output_path, **save_kwargs)
            self._report(f"Saved: {os.path.basename(output_path)}", "success")
            return True

        except Exception as e:
            self._report(
                f"Composite step failed ({e}); preserve contract NOT held.",
                "error",
            )
            return False

    # ── BFL Expand provider ──────────────────────────────────────────────

    def _bfl_download(self, url: str, output_path: str) -> bool:
        """Download a BFL result image to disk (atomic: temp file + rename)."""
        import requests
        import tempfile

        self._report("Downloading BFL result...", "download")
        try:
            resp = requests.get(url, stream=True, timeout=120)
            resp.raise_for_status()
            out_dir = os.path.dirname(output_path) or "."
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
            return True
        except Exception as exc:
            self._report(f"BFL download failed: {exc}", "error")
            return False

    def _bfl_outpaint(
        self,
        image_path: str,
        output_folder: str,
        expand_left: int,
        expand_right: int,
        expand_top: int,
        expand_bottom: int,
        prompt: str,
        output_format: str,
        composite_mode: str,
        output_path: Optional[str] = None,
        edge_seal_px: int = 0,
        edge_seal_color: Tuple[int, int, int] = (220, 220, 220),
        cancel_event: Optional[threading.Event] = None,
    ) -> Optional[str]:
        """Outpaint via BFL Expand (FLUX Pro 1.0). Returns output path or None."""
        import requests
        self._set_last_outpaint_error_detail("")

        def _summarize_poll_payload(payload: dict) -> str:
            safe = {}
            for key in ("status", "error", "message", "eta", "queue_position", "id"):
                if key in payload and payload.get(key) not in (None, ""):
                    val = str(payload.get(key))
                    safe[key] = val[:160]
            return str(safe) if safe else "{}"

        # 1. Preflight: shrink input + margins so total canvas fits BFL's MP limit.
        #    Without this, BFL silently clamps (e.g. 1536x2048 → 1088x1456).
        max_upload, adj_l, adj_r, adj_t, adj_b, sim_w, sim_h = (
            self._preflight_size(
                image_path, expand_left, expand_right, expand_top, expand_bottom,
                max_dim=_BFL_MAX_CANVAS_DIM, max_mp=_BFL_MAX_CANVAS_MP,
            )
        )
        expected_w = sim_w + adj_l + adj_r
        expected_h = sim_h + adj_t + adj_b
        if max_upload < 2048:
            # Read original dims for scale reporting
            with Image.open(image_path) as _tmp:
                _tmp_t = ImageOps.exif_transpose(_tmp)
                _orig_max = max(_tmp_t.size)
            eff_scale = max_upload / _orig_max if _orig_max > 0 else 1.0
            self._report(
                f"BFL Expand: scaled {eff_scale:.2f}x to fit MP limit",
                "progress",
            )

        # 2. Encode: EXIF transpose → RGB → thumbnail(max_upload) → JPEG q=90 → base64
        self._report("Encoding image for BFL Expand...", "upload")
        try:
            img = self._prepare_processed_image(image_path=image_path, max_size=max_upload)
            processed_img = img.copy()  # Sacred pixels for composite
            img_w, img_h = img.size

            # 16-pixel snap: BFL requires canvas dims on 16px grid
            raw_w = img_w + adj_l + adj_r
            raw_h = img_h + adj_t + adj_b
            snapped_w = (raw_w // 16) * 16
            snapped_h = (raw_h // 16) * 16

            delta_w = raw_w - snapped_w
            if delta_w > 0:
                cut_r = min(adj_r, delta_w)
                adj_r -= cut_r
                adj_l = max(0, adj_l - (delta_w - cut_r))

            delta_h = raw_h - snapped_h
            if delta_h > 0:
                cut_b = min(adj_b, delta_h)
                adj_b -= cut_b
                adj_t = max(0, adj_t - (delta_h - cut_b))

            # Update expected dims after snap
            expected_w = img_w + adj_l + adj_r
            expected_h = img_h + adj_t + adj_b

            upload_img = img
            if edge_seal_px > 0:
                upload_img = self._edge_seal_copy(img, edge_seal_px, edge_seal_color)
                self._report(f"Applied upload-only edge seal ({edge_seal_px}px).", "debug")

            buf = BytesIO()
            upload_img.save(buf, format="JPEG", quality=90)
            image_b64 = base64.b64encode(buf.getvalue()).decode("utf-8")
        except Exception as exc:
            self._report(f"Failed to encode image: {exc}", "error")
            return None

        self._report(
            f"BFL Expand: img={img_w}x{img_h}, margins L={adj_l} R={adj_r} T={adj_t} B={adj_b}, "
            f"expected canvas={expected_w}x{expected_h}",
            "debug",
        )

        # 3. Submit to BFL
        headers = {
            "x-key": self._bfl_api_key,
            "Content-Type": "application/json",
        }
        payload = {
            "image": image_b64,
            "top": adj_t,
            "bottom": adj_b,
            "left": adj_l,
            "right": adj_r,
            "steps": 50,
            "output_format": "jpeg" if output_format.lower() in ("jpg", "jpeg") else "png",
        }
        if prompt.strip():
            payload["prompt"] = prompt.strip()

        self._report(
            f"Submitting to BFL Expand (L={adj_l} R={adj_r} T={adj_t} B={adj_b})...",
            "task",
        )
        try:
            resp = requests.post(_BFL_EXPAND_URL, json=payload, headers=headers, timeout=60)
            resp.raise_for_status()
            submit_data = resp.json()
        except requests.exceptions.HTTPError as exc:
            body = ""
            try:
                body = exc.response.text[:300]
            except Exception:
                pass
            self._report(f"BFL submit failed ({exc.response.status_code}): {body}", "error")
            return None
        except Exception as exc:
            self._report(f"BFL submit failed: {exc}", "error")
            return None

        # Log cost/MP info from submit response if available
        for key in ("input_mp", "output_mp", "cost"):
            val = submit_data.get(key)
            if val is not None:
                self._report(f"BFL {key}: {val}", "debug")

        polling_url = submit_data.get("polling_url")
        task_id = submit_data.get("id", "")
        if not polling_url:
            # Check for immediate result
            result_obj = submit_data.get("result")
            sample_url = (
                result_obj.get("sample") if isinstance(result_obj, dict) else None
            ) or submit_data.get("sample")
            if not sample_url:
                self._report(f"No polling_url in BFL response: {submit_data}", "error")
                return None
            # Will download below after building output_path
            polling_url = None
            poll_data = submit_data
        else:
            self._report(f"BFL task {task_id} queued, polling...", "task")
            poll_data = None

        # 4. Poll for result
        if polling_url:
            self._report("Waiting for BFL Expand...", "progress")
            poll_start = time.monotonic()
            poll_num = 0
            consecutive_errors = 0

            while True:
                if cancel_event is not None and cancel_event.is_set():
                    self._report("Expand aborted by user (provider=bfl) reason=user_aborted", "warning")
                    return None
                elapsed_s = int(time.monotonic() - poll_start)

                if elapsed_s >= _BFL_MAX_WAIT_SECONDS:
                    last_status = "unknown"
                    if isinstance(poll_data, dict):
                        last_status = str(poll_data.get("status", "unknown"))
                    detail = (
                        f"reason=pending_timeout task={task_id or 'unknown'} "
                        f"elapsed={elapsed_s}s last_status={last_status} poll_count={poll_num}"
                    )
                    self._set_last_outpaint_error_detail(detail)
                    if isinstance(poll_data, dict):
                        self._report(
                            f"BFL last poll snapshot: {_summarize_poll_payload(poll_data)}",
                            "debug",
                        )
                    self._report(f"BFL Expand timed out after {elapsed_s}s ({detail})", "error")
                    return None

                if cancel_event is not None and cancel_event.wait(timeout=_BFL_POLL_INTERVAL):
                    self._report("Expand aborted by user (provider=bfl) reason=user_aborted", "warning")
                    return None
                if cancel_event is None:
                    time.sleep(_BFL_POLL_INTERVAL)
                poll_num += 1

                try:
                    poll_resp = requests.get(
                        polling_url, headers={"x-key": self._bfl_api_key}, timeout=30,
                    )
                    poll_resp.raise_for_status()
                    poll_data = poll_resp.json()
                    consecutive_errors = 0
                except Exception as exc:
                    consecutive_errors += 1
                    self._report(f"BFL poll error ({consecutive_errors}): {exc}", "warning")
                    if consecutive_errors >= _BFL_MAX_CONSECUTIVE_ERRORS:
                        detail = (
                            f"reason=poll_error_limit task={task_id or 'unknown'} "
                            f"poll_errors={consecutive_errors}"
                        )
                        self._set_last_outpaint_error_detail(detail)
                        self._report(
                            f"BFL polling aborted after {consecutive_errors} consecutive errors ({detail})",
                            "error",
                        )
                        return None
                    continue

                status = poll_data.get("status", "")
                status_lower = status.lower()
                if status_lower in ("ready", "succeeded"):
                    break
                elif status_lower in ("error", "failed"):
                    err_msg = poll_data.get("error", "Unknown BFL error")
                    detail = (
                        f"reason=provider_failed task={task_id or 'unknown'} "
                        f"status={status or 'unknown'} error={str(err_msg)[:160]}"
                    )
                    self._set_last_outpaint_error_detail(detail)
                    self._report(
                        f"BFL Expand failed: {err_msg} ({detail})",
                        "error",
                    )
                    return None
                else:
                    if poll_num % 6 == 0:
                        self._report(
                            f"BFL status: {status} (poll {poll_num}, {elapsed_s}s elapsed)...",
                            "progress",
                        )

        # Log cost/MP from poll result at info level
        if poll_data:
            billing_parts = []
            cost = poll_data.get("cost")
            in_mp = poll_data.get("input_mp")
            out_mp = poll_data.get("output_mp")
            if cost is not None:
                billing_parts.append(f"cost={cost} credits")
            if in_mp is not None:
                billing_parts.append(f"input={in_mp}MP")
            if out_mp is not None:
                billing_parts.append(f"output={out_mp}MP")
            if billing_parts:
                self._report(f"BFL billing: {', '.join(billing_parts)}", "info")

        # Extract sample URL
        result_obj = poll_data.get("result") if poll_data else None
        sample_url = (
            result_obj.get("sample") if isinstance(result_obj, dict) else None
        ) or (poll_data.get("sample") if poll_data else None)
        if not sample_url:
            self._report(f"BFL result missing sample URL: {poll_data}", "error")
            return None

        # 5. Build output path — skip if caller provided one
        os.makedirs(output_folder, exist_ok=True)
        if output_path is None:
            stem = Path(image_path).stem
            ext = f".{output_format}"
            output_path = os.path.join(output_folder, f"{stem}-expanded{ext}")
            counter = 1
            while os.path.exists(output_path):
                output_path = os.path.join(
                    output_folder, f"{stem}-expanded_v{counter}{ext}",
                )
                counter += 1

        if not self._bfl_download(sample_url, output_path):
            self._report("BFL download failed", "error")
            return None

        # 6. Post-download dimension check
        try:
            with Image.open(output_path) as dl_img:
                actual_w, actual_h = dl_img.size
            self._report(
                f"BFL output: {actual_w}x{actual_h} "
                f"(expected {expected_w}x{expected_h})",
                "debug",
            )
            if (actual_w, actual_h) != (expected_w, expected_h):
                self._report(
                    f"BFL dimension mismatch! Expected {expected_w}x{expected_h}, "
                    f"got {actual_w}x{actual_h} — composite will adjust paste coords",
                    "warning",
                )
        except Exception as exc:
            self._report(f"Could not verify output dimensions: {exc}", "warning")

        # 7. Composite: paste original sharp pixels over AI center.
        # Same preserve-contract check as the fal.ai path (PR
        # fix/step0-composite-and-rppg-v2.5): if the composite fails
        # for any preserve mode, reject the output instead of silently
        # shipping a non-composited result. The full-res-original
        # upscale path (applied to fal.ai above) could also be wired
        # here as a future enhancement, but BFL doesn't exhibit the
        # underflow behaviour fal.ai does, so the current downscaled
        # composite source is acceptable for now.
        composite_ok = self._composite_onto_result(
            output_path, processed_img, adj_l, adj_r, adj_t, adj_b,
            output_format, composite_mode,
        )
        if not composite_ok and composite_mode in {
            "preserve_seamless", "hard", "feathered",
        }:
            self._report(
                "BFL composite FAILED for preserve mode — output rejected.",
                "error",
            )
            self._set_last_outpaint_error_detail("bfl_composite_failed")
            try:
                os.unlink(output_path)
            except OSError:
                pass
            return None
        return output_path
    _ALPHA_MATTE_RGB = (255, 255, 255)
