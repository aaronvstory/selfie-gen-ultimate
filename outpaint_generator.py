"""Outpaint (expand) images using fal.ai or BFL Expand API."""

import os
import math
import time
import base64
import logging
import tempfile
import threading
from io import BytesIO
from typing import Optional, Callable, Tuple, Dict
from pathlib import Path
from PIL import Image, ImageOps, ImageDraw
from outpaint_geometry import (
    compute_centered_aspect_expand_plan,
    compute_provider_caps,
)
from automation.config import get_outpaint_fal_timeout_seconds
# Imported as a module (not `from fal_utils import ...`) so _poll_outpaint_with_retry
# resolves fal_queue_submit/poll through the module at call time — tests
# monkeypatch ``fal_utils.fal_queue_submit`` etc., and that must take effect.
# fal_utils does NOT import outpaint_generator, so there is no import cycle.
import fal_utils

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


def _poll_outpaint_with_retry(
    gen,
    *,
    endpoint: str,
    payload: dict,
    timeout_seconds: int,
    cancel_event=None,
    adj: Tuple[int, int, int, int] = (0, 0, 0, 0),
    max_submit_attempts: int = 2,
):
    """Submit an outpaint job + poll, resubmitting ONCE on a transient failure.

    Module-level (not a method) so it can be unit-tested in isolation by
    monkeypatching ``fal_queue_submit`` / ``fal_queue_poll``. On terminal
    failure it stores the REAL provider detail on
    ``gen._last_outpaint_error_detail`` and reports it; on user cancel it
    returns ``None`` silently. Returns the poll result (a truthy dict, possibly
    the aspect-ratio sentinel) on success, else ``None``.
    """
    adj_left, adj_right, adj_top, adj_bottom = adj
    for submit_attempt in range(1, max_submit_attempts + 1):
        gen._report(
            f"Submitting outpaint (L={adj_left} R={adj_right} "
            f"T={adj_top} B={adj_bottom})...",
            "task",
        )
        result = fal_utils.fal_queue_submit(
            gen.api_key, endpoint, payload, gen._progress_callback
        )
        if not result:
            gen._report("Failed to submit outpaint job", "error")
            return None
        status_url = result.get("status_url")
        if not status_url:
            gen._report("No status URL in response", "error")
            return None
        request_id = str(result.get("request_id", "") or "")
        safe_request = request_id[-8:] if request_id else "unknown"
        gen._report(
            f"Queue watch: provider=fal endpoint={endpoint} "
            f"req=*{safe_request} timeout={timeout_seconds}s",
            "debug",
        )
        gen._report("Waiting for outpaint...", "progress")
        poll_error: dict = {}
        final = fal_utils.fal_queue_poll(
            gen.api_key,
            status_url,
            gen._progress_callback,
            max_wait_seconds=timeout_seconds,
            cancel_event=cancel_event,
            provider="fal",
            endpoint=endpoint,
            request_id=request_id,
            operation_name="Outpaint",
            error_sink=poll_error,
        )
        if final:
            return final
        if cancel_event is not None and cancel_event.is_set():
            return None
        detail = poll_error.get("detail") or "reason=fal_failed_or_timed_out"
        if poll_error.get("retryable") and submit_attempt < max_submit_attempts:
            gen._report(
                f"Outpaint attempt {submit_attempt} failed ({detail}); retrying once...",
                "warning",
            )
            continue
        gen._set_last_outpaint_error_detail(detail)
        gen._report(f"Outpaint failed: {detail}", "error")
        return None
    return None


class OutpaintGenerator:
    """Expand images using fal.ai outpaint."""

    ENDPOINT = "fal-ai/image-apps-v2/outpaint"
    # Bria Expand: a PURPOSE-BUILT background-expansion model. Unlike the generic
    # outpaint endpoint (which duplicates a centered ID card into the borders),
    # Bria understands subject-vs-background and extends the surroundings — even
    # continuing a hand holding the card — without re-drawing the document. Takes
    # canvas_size + original_image_size + original_image_location for exact
    # placement, which gives us known paste geometry for the full-res composite.
    BRIA_ENDPOINT = "fal-ai/bria/expand"
    _BRIA_MAX_CANVAS_DIM = _read_single_env_int("BRIA_EXPAND_MAX_DIM", 2400)

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
        self._cancel_event: Optional[threading.Event] = None
        self._last_outpaint_error_detail: str = ""

    def set_progress_callback(self, cb: Callable[[str, str], None]):
        self._progress_callback = cb

    def set_cancel_event(self, event: Optional[threading.Event]) -> None:
        self._cancel_event = event

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
        full_res_plan: Optional[Dict[str, int]] = None,
        border_strategy: str = "edge_extend",
    ) -> Optional[str]:
        """Outpaint (expand) an image.

        When ``full_res_plan`` (from
        :func:`outpaint_geometry.compute_full_res_expand_plan`) is supplied, the
        provider is asked ONLY for the downscaled borders (the plan's
        ``left/right/top/bottom``); its raw output is then reassembled at the
        original's native resolution by :meth:`_composite_fullres` — the
        untouched original is hard-pasted into the center by known geometry (no
        matchTemplate), so it stays pixel-perfect. ``expand_*`` are ignored in
        this mode (the plan drives geometry). See the module docstring of
        ``outpaint_geometry`` for the coordinate systems.

        Args:
            image_path: Path to input image
            output_folder: Where to save output
            expand_left/right/top/bottom: Pixels to expand per side (legacy path).
            prompt: Optional guidance prompt
            output_format: Output format ("png" or "jpg")
            composite_mode: "preserve_seamless" (outside-only seam blend + exact center),
                "feathered" (legacy 3px blend), "hard" (pixel-perfect),
                "none" (raw AI output — still calls the provider), or
                "black_fill" (paste the original onto a solid BLACK canvas with
                NO provider call — instant, free, deterministic; the expansion
                regions are pure black instead of AI-generated content)
            output_path: If provided, use this exact path instead of generating one
            poll_timeout_seconds: Maximum seconds to wait for async poll completion.
            cancel_event: Optional event to abort waiting and return early.
            full_res_plan: Optional full-res expand plan (see above).

        Returns:
            Absolute path to expanded image, or None on failure.
        """
        effective_cancel_event = cancel_event if cancel_event is not None else self._cancel_event

        # Full-res dispatch. black_fill is handled INSIDE _outpaint_full_res
        # (it builds a full-res black canvas from the plan) so that a
        # full_res_plan + black_fill still honors the plan's geometry instead of
        # falling through to the legacy path with 0/140px margins (code-review
        # CRITICAL: that shipped a no-border or fixed-140px result silently).
        if full_res_plan is not None:
            return self._outpaint_full_res(
                image_path=image_path,
                output_folder=output_folder,
                full_res_plan=full_res_plan,
                prompt=prompt,
                output_format=output_format,
                composite_mode=composite_mode,
                output_path=output_path,
                provider=provider,
                edge_seal_px=edge_seal_px,
                edge_seal_color=edge_seal_color,
                poll_timeout_seconds=poll_timeout_seconds,
                cancel_event=effective_cancel_event,
                border_strategy=border_strategy,
            )
        return self._outpaint_provider(
            image_path=image_path,
            output_folder=output_folder,
            expand_left=expand_left,
            expand_right=expand_right,
            expand_top=expand_top,
            expand_bottom=expand_bottom,
            prompt=prompt,
            output_format=output_format,
            composite_mode=composite_mode,
            output_path=output_path,
            provider=provider,
            document_mode=document_mode,
            edge_seal_px=edge_seal_px,
            edge_seal_color=edge_seal_color,
            poll_timeout_seconds=poll_timeout_seconds,
            cancel_event=effective_cancel_event,
        )

    @staticmethod
    def _auto_expanded_path(
        image_path: str, output_folder: str, output_format: str
    ) -> str:
        """Auto-name ``<stem>-expanded.<ext>`` with ``_vN`` collision suffixes.

        Shared by the legacy and full-res paths so neither silently overwrites
        an existing output when ``output_path`` is omitted.
        """
        stem = Path(image_path).stem
        ext = f".{output_format}"
        path = os.path.join(output_folder, f"{stem}-expanded{ext}")
        counter = 1
        while os.path.exists(path):
            path = os.path.join(output_folder, f"{stem}-expanded_v{counter}{ext}")
            counter += 1
        return path

    def _outpaint_provider(
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
        """Legacy provider-coordinate outpaint (the original ``outpaint`` body).

        Sends ``expand_*`` pixel margins to the provider and composites in the
        downscaled provider coordinate system via ``_composite_onto_result``.
        """
        self._set_last_outpaint_error_detail("")

        # black_fill: a no-API local mode. Instead of asking a provider to
        # generate the expansion regions, paste the original onto a solid
        # black canvas sized exactly like the AI path would have produced.
        # Short-circuit BEFORE any provider selection / upload / queue call
        # so it stays instant, free, and deterministic (and never touches
        # the BFL/fal credentials). Geometry mirrors the fal path: the
        # original is scaled to the same simulated-upload size the AI path
        # uses, then black margins are added — so swapping black_fill in/out
        # changes only what fills the borders, not the framing.
        if (composite_mode or "").strip().lower() == "black_fill":
            return self._black_fill_expand(
                image_path=image_path,
                output_folder=output_folder,
                expand_left=expand_left,
                expand_right=expand_right,
                expand_top=expand_top,
                expand_bottom=expand_bottom,
                output_format=output_format,
                output_path=output_path,
                document_mode=document_mode,
            )

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

        timeout_seconds = get_outpaint_fal_timeout_seconds(
            {"outpaint_fal_timeout_seconds": poll_timeout_seconds}
        )

        # Submit + poll, with ONE automatic resubmit on a transient provider
        # failure (5xx / fal's generic "Failed to generate ..." 422). The real
        # error detail is carried out of fal_queue_poll via error_sink and
        # surfaced verbatim instead of the old generic
        # "reason=fal_failed_or_timed_out" (which discarded the actual cause).
        final = _poll_outpaint_with_retry(
            self,
            endpoint=self.ENDPOINT,
            payload=payload,
            timeout_seconds=timeout_seconds,
            cancel_event=cancel_event,
            adj=(adj_left, adj_right, adj_top, adj_bottom),
        )
        if not final:
            # The helper already reported the failure and stored the real
            # detail (or returned silently on user cancel).
            return None

        # v2.28: SelfieGenerator's aspect-ratio self-heal sentinel
        # propagates through ``fal_queue_poll``. Outpaint doesn't
        # currently send aspect_ratio, but a future strict outpaint
        # model could 422 on it — surface a clear error instead of
        # the misleading "No images in result".
        if isinstance(final, dict) and final.get("__aspect_ratio_rejected__"):
            allowed = final.get("allowed") or []
            msg = (
                f"Outpaint model rejected aspect_ratio "
                f"(accepted: {', '.join(allowed) or '<empty>'}); "
                "try a different expansion size."
            )
            self._set_last_outpaint_error_detail(msg)
            self._report(msg, "error")
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
            output_path = self._auto_expanded_path(
                image_path, output_folder, output_format
            )

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
        # asks for the raw provider output. Short-circuit before the
        # always-composite path so a download + raw return stays cheap.
        if composite_mode == "none":
            self._report(
                "Composite: none — using raw AI output "
                f"({downloaded_w}x{downloaded_h})",
                "progress",
            )
            return output_path

        # Always-composite contract (PR fix/step0-composite-and-rppg-v2.5):
        # never silently skip composite on a small underflow — that
        # shipped a non-composited raw result and broke preserve_seamless
        # on every pass because fal.ai routinely clamps 1-2% smaller
        # than preflight. Resize the downloaded fal output to the
        # provider's EXPECTED canvas (composite_source + adj_* margins
        # = exactly what fal generated for) so the composite's
        # matchTemplate alignment can lock cleanly. Then composite with
        # the downscaled provider source + adjusted margins.
        #
        # PR #53 round 10 REVERTED an earlier rounds-5..9 experiment
        # that paste the FULL-RES original (reopen image_path, compute
        # final_canvas_w = orig_full.width + EXPAND_*, upscale fal
        # output to that, paste full-res orig at full-res margins).
        # That mixed two coordinate systems — fal generated around
        # (downscaled_image, adjusted_margins) but we pasted around
        # (full_res_image, full_res_margins). On a 3024x4032 source
        # with 700px margins it produced a 3.4x upscale of fal output
        # (1296x1520 -> 4424x5432), and the matchTemplate +-15px
        # search window cannot recover from a coordinate-system delta
        # of hundreds of pixels — the composite silently misaligned to
        # the math-default placement. User manual smoke caught it.
        # Provider-coordinate compositing is the known-good geometry
        # main shipped with for months; we restore it here while
        # keeping the always-composite + bool-return contracts.
        self._report(
            (
                "Fal composite downloaded result: "
                f"actual={downloaded_w}x{downloaded_h} "
                f"expected={expected_canvas_w}x{expected_canvas_h}"
            ),
            "debug",
        )

        if (downloaded_w, downloaded_h) != (expected_canvas_w, expected_canvas_h):
            try:
                # Intentional EXIF drop on the resize-and-save: the
                # .convert("RGB") loses any EXIF the provider returned,
                # and we don't propagate it through. This matches the
                # composite-side save path (the composite always
                # rewrites RGB pixels without EXIF). Subagent L5
                # round 11 — flagged for clarity, not a bug.
                with Image.open(output_path) as dl:
                    dl_rgb = dl.convert("RGB")
                    resized = dl_rgb.resize(
                        (expected_canvas_w, expected_canvas_h),
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
                        f"Composite: resized fal output "
                        f"{downloaded_w}x{downloaded_h} -> "
                        f"{expected_canvas_w}x{expected_canvas_h} (Lanczos) "
                        "to match provider-coordinate expected canvas"
                    ),
                    "info",
                )
            except Exception as exc:
                self._report(
                    f"Could not resize fal output to expected canvas: {exc}",
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

        # Composite in provider/upload coordinate system: the same
        # composite_source + adjusted margins fal was given. This is
        # main's geometry, restored after the round-5..9 full-res
        # experiment caused user-reported misalignment.
        composite_ok = self._composite_onto_result(
            output_path, composite_source,
            adj_left, adj_right, adj_top, adj_bottom,
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

    # ── full-resolution expand (borders low-res, original native) ────────

    def _outpaint_full_res(
        self,
        image_path: str,
        output_folder: str,
        full_res_plan: Dict[str, int],
        prompt: str = "",
        output_format: str = "png",
        composite_mode: str = "preserve_seamless",
        output_path: Optional[str] = None,
        provider: Optional[str] = None,
        edge_seal_px: int = 0,
        edge_seal_color: Tuple[int, int, int] = (220, 220, 220),
        poll_timeout_seconds: int = 150,
        cancel_event: Optional[threading.Event] = None,
        border_strategy: str = "edge_extend",
    ) -> Optional[str]:
        """Full-resolution expand — original kept 1:1, borders added.

        ``border_strategy``:
          - ``"bria"``: use the purpose-built Bria Expand model to generate
            photorealistic borders (extends backgrounds AND a hand holding the
            card, never duplicates the ID), then hard-paste the untouched full-res
            original on top at Bria's known placement. Best quality; costs ~$0.04.
          - ``"edge_extend"``: build the borders ALGORITHMICALLY from the
            original's own outer background pixels (replicate outward + grain +
            blur). No provider call — deterministic, instant, free, and the ID can
            NEVER appear in the border. The free/offline fallback.
          - ``"ai"`` (legacy): the generic outpaint endpoint. NOTE: it duplicates
            a centered ID into the borders — kept only for back-compat; prefer
            "bria".

        Either way the ORIGINAL center is hard-pasted at native resolution by known
        geometry (no matchTemplate) so it stays byte-for-byte identical.
        """
        self._set_last_outpaint_error_detail("")
        # black_fill: deterministic solid-black borders at the plan's full-res
        # geometry, original hard-pasted on top. No provider, honors the plan.
        if (composite_mode or "").strip().lower() == "black_fill":
            return self._black_fill_full_res(
                image_path, output_folder, full_res_plan,
                output_format, output_path,
            )
        if border_strategy == "edge_extend":
            return self._edge_extend_full_res(
                image_path, output_folder, full_res_plan,
                output_format, output_path, composite_mode,
            )
        if border_strategy == "bria":
            return self._bria_expand_full_res(
                image_path, output_folder, full_res_plan,
                output_format, output_path, composite_mode,
                prompt, poll_timeout_seconds, cancel_event,
            )
        dl_left = int(full_res_plan["left"])
        dl_right = int(full_res_plan["right"])
        dl_top = int(full_res_plan["top"])
        dl_bottom = int(full_res_plan["bottom"])
        if dl_left + dl_right + dl_top + dl_bottom <= 0:
            self._report(
                "Full-res plan has zero margins — nothing to expand.", "error"
            )
            self._set_last_outpaint_error_detail("fullres_zero_margins")
            return None

        # Resolve the final output path first so we can name a temp for the
        # provider's raw (downscaled-canvas) output.
        if output_path is None:
            output_path = self._auto_expanded_path(
                image_path, output_folder, output_format
            )
        os.makedirs(os.path.dirname(os.path.abspath(output_path)), exist_ok=True)

        fd, raw_path = tempfile.mkstemp(
            suffix=f".{output_format}", prefix="fullres_raw_"
        )
        os.close(fd)
        try:
            # Provider generates the borders in the downscaled coordinate
            # system. composite_mode="none" -> we get the raw canvas back
            # (downscaled center + generated borders); we discard its center.
            self._report(
                f"Full-res expand: provider borders at scale "
                f"{full_res_plan.get('scale_pct', '?')}% "
                f"(L={dl_left} R={dl_right} T={dl_top} B={dl_bottom})",
                "info",
            )
            provider_out = self._outpaint_provider(
                image_path=image_path,
                output_folder=output_folder,
                expand_left=dl_left,
                expand_right=dl_right,
                expand_top=dl_top,
                expand_bottom=dl_bottom,
                prompt=prompt,
                output_format=output_format,
                composite_mode="none",
                output_path=raw_path,
                provider=provider,
                document_mode=False,
                edge_seal_px=edge_seal_px,
                edge_seal_color=edge_seal_color,
                poll_timeout_seconds=poll_timeout_seconds,
                cancel_event=cancel_event,
            )
            if not provider_out:
                # error detail already set by the provider path
                return None

            if not self._composite_fullres(
                raw_path, image_path, output_path,
                full_res_plan, output_format, composite_mode,
            ):
                self._set_last_outpaint_error_detail("fullres_composite_failed")
                return None
            return output_path
        finally:
            try:
                if os.path.exists(raw_path):
                    os.remove(raw_path)
            except OSError:
                pass

    def _bria_expand_full_res(
        self,
        image_path: str,
        output_folder: str,
        plan: Dict[str, int],
        output_format: str,
        output_path: Optional[str],
        composite_mode: str,
        prompt: str,
        poll_timeout_seconds: int,
        cancel_event: Optional[threading.Event],
    ) -> Optional[str]:
        """Full-res expand using Bria Expand for photorealistic borders.

        Bria centers the (downscaled) original in a ``canvas_size`` and generates
        the surrounding pixels — extending backgrounds and even a hand holding the
        card, without duplicating the document. We place the original at a known
        ``original_image_location`` so we can then upscale Bria's output to the
        full-res canvas and hard-paste the untouched original on top (pixel-perfect
        ID + real generated borders).
        """
        from fal_utils import (
            upload_reference_image, fal_queue_submit, fal_queue_poll,
            fal_download_file,
        )

        if output_path is None:
            output_path = self._auto_expanded_path(
                image_path, output_folder, output_format
            )
        os.makedirs(os.path.dirname(os.path.abspath(output_path)), exist_ok=True)

        # Provider-coordinate geometry from the plan (already capped to fit).
        up_w = int(plan["upload_w"])
        up_h = int(plan["upload_h"])
        p_left = int(plan["left"])
        p_top = int(plan["top"])
        cw = p_left + up_w + int(plan["right"])
        ch = p_top + up_h + int(plan["bottom"])
        # Bria area cap is 5000x5000; our provider canvas is already small.
        if cw * ch <= 0 or (cw - up_w) + (ch - up_h) <= 0:
            self._report("Bria plan has zero expansion.", "error")
            self._set_last_outpaint_error_detail("bria_zero_margins")
            return None

        self._report(
            f"Bria Expand: canvas {cw}x{ch}, original {up_w}x{up_h} "
            f"at ({p_left},{p_top})", "info",
        )
        try:
            image_url, _, _ = upload_reference_image(
                image_path=image_path, fal_api_key=self.api_key,
                max_size=max(up_w, up_h), progress_cb=self._progress_callback,
                freeimage_api_key=self._freeimage_key,
            )
        except Exception as exc:
            self._report(f"Bria upload failed: {exc}", "error")
            self._set_last_outpaint_error_detail(f"bria_upload:{type(exc).__name__}")
            return None
        if not image_url:
            self._set_last_outpaint_error_detail("bria_upload_failed")
            return None

        payload: Dict[str, object] = {
            "image_url": image_url,
            "canvas_size": [cw, ch],
            "original_image_size": [up_w, up_h],
            "original_image_location": [p_left, p_top],
        }
        if prompt and prompt.strip():
            payload["prompt"] = prompt.strip()

        submit = fal_queue_submit(
            self.api_key, self.BRIA_ENDPOINT, payload, self._progress_callback
        )
        if not submit or not submit.get("status_url"):
            self._set_last_outpaint_error_detail("bria_submit_failed")
            return None
        result = fal_queue_poll(
            self.api_key, submit["status_url"], self._progress_callback,
            max_wait_seconds=poll_timeout_seconds, cancel_event=cancel_event,
            provider="fal", endpoint=self.BRIA_ENDPOINT,
            request_id=submit.get("request_id", ""), operation_name="Bria Expand",
        )
        if not result:
            self._set_last_outpaint_error_detail("bria_poll_failed")
            return None
        # Bria returns a single image under "image" (singular).
        img_obj = result.get("image") or (
            (result.get("images") or [{}])[0] if result.get("images") else {}
        )
        result_url = img_obj.get("url") if isinstance(img_obj, dict) else None
        if not result_url:
            self._set_last_outpaint_error_detail("bria_no_image")
            return None

        fd, raw_path = tempfile.mkstemp(suffix=f".{output_format}", prefix="bria_raw_")
        os.close(fd)
        try:
            if not fal_download_file(result_url, raw_path, self._progress_callback):
                self._set_last_outpaint_error_detail("bria_download_failed")
                return None
            # Bria placed the original at (p_left,p_top) in a (cw,ch) canvas — the
            # SAME provider coordinate system _composite_fullres expects. Reuse it
            # to upscale Bria's borders to full-res and hard-paste the pristine
            # original on top.
            if not self._composite_fullres(
                raw_path, image_path, output_path, plan, output_format,
                composite_mode,
            ):
                self._set_last_outpaint_error_detail("bria_composite_failed")
                return None
            return output_path
        finally:
            try:
                if os.path.exists(raw_path):
                    os.remove(raw_path)
            except OSError:
                pass

    def _black_fill_full_res(
        self,
        image_path: str,
        output_folder: str,
        plan: Dict[str, int],
        output_format: str,
        output_path: Optional[str],
    ) -> Optional[str]:
        """Full-res black-border expand: solid black canvas at the plan's full
        geometry with the untouched original hard-pasted in the center."""
        try:
            fl = int(plan["full_left"])
            ft = int(plan["full_top"])
            fcw = int(plan["full_canvas_w"])
            fch = int(plan["full_canvas_h"])
            if output_path is None:
                output_path = self._auto_expanded_path(
                    image_path, output_folder, output_format
                )
            os.makedirs(os.path.dirname(os.path.abspath(output_path)), exist_ok=True)
            with Image.open(image_path) as _o:
                orig = ImageOps.exif_transpose(_o).convert("RGB")
            ow, oh = orig.size
            # Rebuild from the actual original so orig+margins == canvas.
            fcw = ow + fl + int(plan["full_right"])
            fch = oh + ft + int(plan["full_bottom"])
            canvas = Image.new("RGB", (fcw, fch), (0, 0, 0))
            canvas.paste(orig, (fl, ft))
            save_kwargs = (
                {"quality": 95}
                if output_format.lower() in ("jpg", "jpeg")
                else {}
            )
            canvas.save(output_path, **save_kwargs)
            self._report(
                f"Black-fill full-res: {fcw}x{fch} "
                f"(original {ow}x{oh} kept pixel-perfect)", "info",
            )
            return output_path
        except Exception as exc:
            self._report(f"Black-fill full-res failed: {exc}", "error")
            self._set_last_outpaint_error_detail(
                f"black_fill_fullres:{type(exc).__name__}"
            )
            return None

    def _edge_extend_full_res(
        self,
        image_path: str,
        output_folder: str,
        plan: Dict[str, int],
        output_format: str,
        output_path: Optional[str],
        composite_mode: str,
    ) -> Optional[str]:
        """Deterministic full-res expand — no provider, cannot duplicate the ID.

        Build the border by replicating the original's OUTER edge pixels outward
        (mode="edge" pad), add matched film grain so it isn't smooth banding, blur
        it into an out-of-focus surface, then hard-paste the untouched original on
        top. Because only the single outer row/col propagates, the card interior
        never appears in the border. A hand at the edge streaks softly outward
        (acceptable "further away" look) instead of being hard-cut.
        """
        try:
            import numpy as np
            from PIL import ImageFilter

            fl = int(plan["full_left"])
            fr = int(plan["full_right"])
            ft = int(plan["full_top"])
            fb = int(plan["full_bottom"])
            if output_path is None:
                output_path = self._auto_expanded_path(
                    image_path, output_folder, output_format
                )
            os.makedirs(os.path.dirname(os.path.abspath(output_path)), exist_ok=True)

            with Image.open(image_path) as _o:
                orig = ImageOps.exif_transpose(_o).convert("RGB")
            ow, oh = orig.size
            arr = np.asarray(orig)

            # 1. Edge-replicate pad to the full canvas. Only the outer edge
            #    row/col propagates, so the card never repeats.
            padded = np.pad(arr, ((ft, fb), (fl, fr), (0, 0)), mode="edge")

            # 2. Soften the border into out-of-focus background. Edge-replicate
            #    leaves thin 1px streaks; a big-radius full-res GaussianBlur only
            #    partly hides them. Downscale → light blur → upscale dissolves the
            #    streaks far better (they average out at low res) AND is cheaper.
            #    The result reads as natural bokeh falloff. The original center is
            #    hard-pasted sharp on top afterward, so softening the border costs
            #    nothing there.
            canvas = Image.fromarray(padded)
            fcw, fch = canvas.size
            small = canvas.resize(
                (max(1, fcw // 12), max(1, fch // 12)), Image.Resampling.LANCZOS
            )
            small = small.filter(ImageFilter.GaussianBlur(radius=6))
            barr = np.asarray(
                small.resize((fcw, fch), Image.Resampling.LANCZOS)
            ).astype(np.float32)

            # background noise amplitude: std of a small corner patch minus its
            # own blur (high-freq only). Corners of an ID photo are background.
            cs = max(8, min(ow, oh) // 20)
            corner = arr[:cs, :cs].astype(np.float32)
            corner_hf = corner - np.asarray(
                Image.fromarray(arr[:cs, :cs]).filter(
                    ImageFilter.GaussianBlur(radius=2)
                )
            ).astype(np.float32)
            noise_amp = float(np.clip(corner_hf.std(), 1.0, 12.0))
            # deterministic noise (no RNG — repeatable): a hash-free fixed pattern
            yy, xx = np.mgrid[0:fch, 0:fcw]
            noise = (np.sin(xx * 12.9898 + yy * 78.233) * 43758.5453)
            noise = (noise - np.floor(noise) - 0.5) * 2.0  # ~[-1,1]
            noise = noise[:, :, None] * noise_amp
            gmask = np.ones((fch, fcw, 1), dtype=np.float32)
            gmask[ft:ft + oh, fl:fl + ow, :] = 0.0
            out = np.clip(barr + noise * gmask * 0.6, 0, 255).astype(np.uint8)
            result = Image.fromarray(out)

            # 3. Paste the pristine original. For preserve/feathered modes,
            #    feather the outer ring so the edge blends into the soft border
            #    (interior stays byte-for-byte); else hard-paste.
            if composite_mode in ("preserve_seamless", "feathered"):
                self._feather_paste_original(result, orig, fl, ft, ow, oh)
            else:
                result.paste(orig, (fl, ft))

            save_kwargs = (
                {"quality": 95}
                if output_format.lower() in ("jpg", "jpeg")
                else {}
            )
            result.save(output_path, **save_kwargs)
            self._report(
                f"Edge-extend full-res: {fcw}x{fch} "
                f"(original {ow}x{oh} kept pixel-perfect, no provider call)",
                "info",
            )
            return output_path
        except Exception as exc:
            self._report(f"Edge-extend full-res failed: {exc}", "error")
            self._set_last_outpaint_error_detail(
                f"edge_extend:{type(exc).__name__}"
            )
            return None

    def _composite_fullres(
        self,
        raw_provider_path: str,
        original_path: str,
        output_path: str,
        plan: Dict[str, int],
        output_format: str,
        composite_mode: str,
    ) -> bool:
        """Assemble the final full-res canvas from the raw provider output.

        Geometry is deterministic (we chose the margins) so no matchTemplate is
        needed. Steps:
          1. Open the raw provider output; the original center occupies
             ``[left:left+upload_w, top:top+upload_h]`` in provider coords.
          2. Crop the four generated border strips (outside that center rect)
             and Lanczos-upscale each to its full-res margin size.
          3. Build a ``full_canvas_w x full_canvas_h`` canvas, place the strips,
             then hard-paste the UNTOUCHED full-res original at (full_left,
             full_top). Optionally seam-blend the thin ring for preserve modes.
        """
        try:
            fl = int(plan["full_left"])
            fr = int(plan["full_right"])
            ft = int(plan["full_top"])
            fb = int(plan["full_bottom"])
            fcw = int(plan["full_canvas_w"])
            fch = int(plan["full_canvas_h"])
            p_left = int(plan["left"])
            p_top = int(plan["top"])
            up_w = int(plan["upload_w"])
            up_h = int(plan["upload_h"])

            with Image.open(original_path) as _o:
                orig_full = ImageOps.exif_transpose(_o).convert("RGB")
            ow, oh = orig_full.size

            # Sanity: the plan was computed for this original.
            if fcw != ow + fl + fr or fch != oh + ft + fb:
                # Rebuild margins from the actual original so a stale plan
                # (e.g. dims changed) still produces orig+margins == canvas.
                fcw = ow + fl + fr
                fch = oh + ft + fb

            with Image.open(raw_provider_path) as _r:
                raw = ImageOps.exif_transpose(_r).convert("RGB")
            rw, rh = raw.size

            # The provider may clamp 1-2% off the requested canvas. Recover the
            # actual per-side margins in provider coords by proportional split,
            # and the actual center rect, so strip crops never go out of bounds.
            req_cw = p_left + up_w + int(plan["right"])
            req_ch = p_top + up_h + int(plan["bottom"])
            sx = rw / max(req_cw, 1)
            sy = rh / max(req_ch, 1)
            a_left = int(round(p_left * sx))
            a_top = int(round(p_top * sy))
            a_cw = int(round(up_w * sx))
            a_ch = int(round(up_h * sy))
            a_right_x = min(rw, a_left + a_cw)
            a_bottom_y = min(rh, a_top + a_ch)
            a_left = max(0, min(a_left, rw - 1))
            a_top = max(0, min(a_top, rh - 1))

            canvas = Image.new("RGB", (fcw, fch))

            def _upscale_paste(box, dest_box):
                """Crop provider region *box* and resize into *dest_box*."""
                bx0, by0, bx1, by1 = box
                if bx1 <= bx0 or by1 <= by0:
                    return
                strip = raw.crop((bx0, by0, bx1, by1))
                dx0, dy0, dx1, dy1 = dest_box
                dw, dh = max(1, dx1 - dx0), max(1, dy1 - dy0)
                canvas.paste(
                    strip.resize((dw, dh), Image.Resampling.LANCZOS), (dx0, dy0)
                )

            # Top band (full width), bottom band (full width), then left/right
            # side bands (between top and bottom). Full-res dest coords:
            #   original center occupies [fl:fl+ow, ft:ft+oh].
            _upscale_paste((0, 0, rw, a_top), (0, 0, fcw, ft))                     # top
            _upscale_paste((0, a_bottom_y, rw, rh), (0, ft + oh, fcw, fch))        # bottom
            _upscale_paste((0, a_top, a_left, a_bottom_y), (0, ft, fl, ft + oh))   # left
            _upscale_paste((a_right_x, a_top, rw, a_bottom_y),
                           (fl + ow, ft, fcw, ft + oh))                            # right

            # Hard-paste the original. In this path (Bria / generic AI) the
            # provider was told the EXACT placement (original_image_location), so
            # the generated border already meets the card at this rectangle — a
            # hard paste is seamless. Feathering here would cross-fade the
            # original against the provider's slightly-different rendering of the
            # same region and produce a ghosted double-edge (worse). Feathering
            # is applied only in the edge-extend path, where the border is
            # derived from the original's own pixels and therefore matches.
            canvas.paste(orig_full, (fl, ft))

            save_kwargs = (
                {"quality": 95}
                if output_format.lower() in ("jpg", "jpeg")
                else {}
            )
            canvas.save(output_path, **save_kwargs)
            self._report(
                f"Full-res composite: {fcw}x{fch} "
                f"(original {ow}x{oh} kept pixel-perfect)",
                "info",
            )
            return True
        except Exception as exc:
            self._report(f"Full-res composite failed: {exc}", "error")
            self._set_last_outpaint_error_detail(
                f"fullres_composite:{type(exc).__name__}"
            )
            return False

    @staticmethod
    def _adaptive_feather_width(arr, ow: int, oh: int):
        """Choose a feather width from the detail near the original's edge.

        ``arr`` is the original as a float32 HxWx3 array. Returns
        ``(feather_px, detail)``. Plain edge (low detail) -> wide feather; busy
        edge (hand / texture / clutter) -> narrow feather (~2-8px) so real detail
        isn't smeared. Mapping: detail<=6 -> ~24px, detail>=40 -> ~3px.
        """
        import numpy as np

        band = int(max(4, min(32, min(ow, oh) // 20)))
        edges = np.concatenate([
            arr[:band].reshape(-1, 3),
            arr[-band:].reshape(-1, 3),
            arr[:, :band].reshape(-1, 3),
            arr[:, -band:].reshape(-1, 3),
        ])
        detail = float(edges.std())
        lo_d, hi_d, wide, narrow = 6.0, 40.0, 24.0, 3.0
        frac = float(np.clip((detail - lo_d) / (hi_d - lo_d), 0.0, 1.0))
        feather = wide + (narrow - wide) * frac
        feather = int(max(2, min(round(feather), min(ow, oh) // 6)))
        return feather, detail

    def _feather_paste_original(
        self,
        canvas: "Image.Image",
        orig_full: "Image.Image",
        left: int,
        top: int,
        ow: int,
        oh: int,
    ) -> None:
        """Paste the original with an ADAPTIVE feathered outer ring.

        The interior stays 100% original (byte-for-byte); only the outermost
        ``feather`` px cross-fade into the already-placed generated border, so the
        boundary isn't a hard rectangle. The feather WIDTH is chosen from the
        detail near the original's edge (photographer feedback):

          - plain / smooth edge (blank surface) -> WIDER feather (up to ~24px):
            nothing to smear, so a soft fall-off looks best.
          - busy / textured edge (hand, patterned surface, clutter) -> NARROW
            feather (~2-8px): a wide fall-off there smears real detail and
            ghosts, so we only soften the 1px hard seam.
        """
        try:
            import numpy as np

            arr = np.asarray(orig_full.convert("RGB")).astype(np.float32)
            feather, detail = self._adaptive_feather_width(arr, ow, oh)
            self._report(
                f"Adaptive feather: edge_detail={detail:.1f} -> {feather}px",
                "debug",
            )

            # Per-pixel alpha: 255 in the interior, ramping to 0 over the outer
            # `feather` px (distance-to-nearest-edge). Full opacity everywhere
            # past the ring — the original interior is byte-for-byte preserved.
            yy = np.minimum(np.arange(oh), oh - 1 - np.arange(oh))
            xx = np.minimum(np.arange(ow), ow - 1 - np.arange(ow))
            dist = np.minimum(xx[None, :], yy[:, None]).astype(np.float32)
            t = np.clip(dist / max(feather, 1), 0.0, 1.0)
            ramp = t * t * (3 - 2 * t)               # smoothstep 0..1
            alpha = np.where(dist >= feather, 1.0, ramp)
            mask = Image.fromarray(
                (np.clip(alpha, 0, 1) * 255).astype(np.uint8)
            ).convert("L")

            canvas.paste(orig_full, (left, top), mask)
        except Exception as exc:  # feathering is cosmetic — fall back to hard paste
            self._report(f"Feathered paste failed, hard-pasting: {exc}", "debug")
            canvas.paste(orig_full, (left, top))

    def _blend_seam_ring(
        self,
        canvas: "Image.Image",
        orig_full: "Image.Image",
        left: int,
        top: int,
        ow: int,
        oh: int,
        composite_mode: str,
    ) -> None:
        """Feather a thin ring just OUTSIDE the pasted original in-place.

        Blurs the seam region so the boundary to the (softer) generated border
        isn't a hard line, then re-pastes the pristine ``orig_full`` so the
        original's interior stays byte-for-byte untouched.
        """
        try:
            from PIL import ImageFilter

            ring = self._PRESERVE_SEAM_BLEND_PX if composite_mode == "preserve_seamless" else 3
            ring = max(1, int(ring))
            cw, ch = canvas.size
            blurred = canvas.filter(ImageFilter.GaussianBlur(radius=ring / 2))
            mask = Image.new("L", (cw, ch), 0)
            draw = ImageDraw.Draw(mask)
            # Ring = band from ring px outside the original up to the original
            # edge. Inner rect (the original) stays mask 0.
            ox0, oy0, ox1, oy1 = left, top, left + ow, top + oh
            r_x0 = max(0, ox0 - ring); r_y0 = max(0, oy0 - ring)
            r_x1 = min(cw, ox1 + ring); r_y1 = min(ch, oy1 + ring)
            draw.rectangle([r_x0, r_y0, r_x1 - 1, r_y1 - 1], fill=140)
            draw.rectangle([ox0, oy0, ox1 - 1, oy1 - 1], fill=0)
            mask = mask.filter(ImageFilter.GaussianBlur(radius=ring / 2))
            canvas.paste(blurred, (0, 0), mask)
            # Re-assert the pristine original (blur bleed never wins over it).
            canvas.paste(orig_full, (ox0, oy0))
        except Exception as exc:  # blending is cosmetic — never fail the expand
            self._report(f"Seam-ring blend skipped: {exc}", "debug")

    # ── black_fill (no-API local expand) ─────────────────────────────────

    def _black_fill_expand(
        self,
        image_path: str,
        output_folder: str,
        expand_left: int,
        expand_right: int,
        expand_top: int,
        expand_bottom: int,
        output_format: str,
        output_path: Optional[str] = None,
        document_mode: bool = False,
    ) -> Optional[str]:
        """Expand by pasting the original onto a solid BLACK canvas.

        No provider call: the borders are filled with pure black instead of
        AI-generated content. Geometry matches the fal path so the framing is
        identical to a real expand — only the fill differs. Returns the output
        path, or None on failure.
        """
        try:
            # Resolve margins the same way the fal path would, so the framing
            # is identical to a real expand. document_mode recomputes a
            # centered 3:4 plan; otherwise honor the caller's margins.
            if document_mode:
                try:
                    with Image.open(image_path) as src_img:
                        src_w, src_h = ImageOps.exif_transpose(src_img).size
                    plan = compute_centered_aspect_expand_plan(
                        orig_w=src_w,
                        orig_h=src_h,
                        target_aspect=(3, 4),
                        caps=compute_provider_caps("fal"),
                    )
                    expand_left = int(plan["left"])
                    expand_right = int(plan["right"])
                    expand_top = int(plan["top"])
                    expand_bottom = int(plan["bottom"])
                except Exception as exc:
                    # Fail rather than fall through: the automation pipeline
                    # passes empty margins in document mode (the percent plan
                    # is skipped), so swallowing this would silently produce a
                    # ZERO-expansion canvas that looks like a successful
                    # "expand". A real failure must propagate to the caller.
                    self._report(
                        f"black_fill document-mode planning failed: {exc}", "error"
                    )
                    self._set_last_outpaint_error_detail(
                        f"black_fill_document_plan_failed:{type(exc).__name__}"
                    )
                    return None

            # Use the SAME preflight scaling the fal path uses so the
            # original sits at the same simulated-upload size + the margins
            # are in the same coordinate system. This keeps black_fill output
            # geometry identical to what an AI expand would have framed.
            max_size, adj_l, adj_r, adj_t, adj_b, sim_w, sim_h = self._preflight_size(
                image_path, expand_left, expand_right, expand_top, expand_bottom
            )
            base_img = self._prepare_processed_image(
                image_path=image_path, max_size=max_size
            )
            # _prepare_processed_image thumbnails to max_size; sim_w/sim_h is
            # the geometry preflight planned around. Align them exactly so the
            # canvas math below can't drift by a rounding pixel. Close the
            # pre-resize handle so a long automation loop doesn't leak PIL
            # decoders.
            try:
                if base_img.size != (sim_w, sim_h):
                    resized = base_img.resize((sim_w, sim_h), Image.Resampling.LANCZOS)
                    base_img.close()
                    base_img = resized

                canvas_w = sim_w + adj_l + adj_r
                canvas_h = sim_h + adj_t + adj_b
                self._report(
                    (
                        "black_fill: building black canvas "
                        f"{canvas_w}x{canvas_h} (orig≈{sim_w}x{sim_h}, "
                        f"margins L={adj_l} R={adj_r} T={adj_t} B={adj_b}) — no API call"
                    ),
                    "progress",
                )

                canvas = Image.new("RGB", (canvas_w, canvas_h), (0, 0, 0))
                canvas.paste(base_img, (adj_l, adj_t))
            finally:
                base_img.close()

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

            save_kwargs = (
                {"quality": 95}
                if output_format.lower() in ("jpg", "jpeg")
                else {}
            )
            canvas.save(output_path, **save_kwargs)
            self._report(
                f"black_fill saved: {os.path.basename(output_path)}", "success"
            )
            return output_path
        except Exception as exc:
            self._report(f"black_fill expand failed: {exc}", "error")
            self._set_last_outpaint_error_detail(
                f"black_fill_failed:{type(exc).__name__}"
            )
            return None

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

        # Defensive: black_fill never calls a provider. outpaint() short-
        # circuits it before reaching here, but a direct/test caller of
        # _bfl_outpaint with black_fill must also stay API-free.
        if (composite_mode or "").strip().lower() == "black_fill":
            return self._black_fill_expand(
                image_path=image_path,
                output_folder=output_folder,
                expand_left=expand_left,
                expand_right=expand_right,
                expand_top=expand_top,
                expand_bottom=expand_bottom,
                output_format=output_format,
                output_path=output_path,
            )

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

        if not fal_utils.fal_download_file(sample_url, output_path, self._report):
            self._report("BFL download failed", "error")
            return None

        # 6. Post-download dimension check.
        # Gemini PR #53 round 13: align with the fal-path fail-fast at
        # line ~559 — silently falling back to expected dimensions on
        # an unreadable download (a) returns a corrupt file to the
        # caller in the composite_mode="none" short-circuit and (b)
        # produces a misleading `bfl_composite_failed` later when the
        # composite step re-opens the file. An unreadable file is an
        # IO failure, not a dimension underflow; fail loudly.
        try:
            with Image.open(output_path) as dl_img:
                actual_w, actual_h = dl_img.size
            self._report(
                f"BFL output: {actual_w}x{actual_h} "
                f"(expected {expected_w}x{expected_h})",
                "debug",
            )
        except Exception as exc:
            self._report(
                f"Could not read downloaded BFL outpaint output: {exc}",
                "error",
            )
            self._set_last_outpaint_error_detail(
                f"bfl_download_unreadable:{type(exc).__name__}"
            )
            try:
                os.unlink(output_path)
            except OSError:
                pass
            return None

        # 6a. composite_mode="none" short-circuit (parity with fal path,
        # PR #53 round 11 subagent M2). Skip the resize-and-composite
        # block entirely — user asked for raw provider output.
        if composite_mode == "none":
            self._report(
                "Composite: none — using raw AI output "
                f"({actual_w}x{actual_h})",
                "progress",
            )
            return output_path

        # 6b. Resize to expected canvas if BFL underflowed (parity with
        # fal path, PR #53 round 11 subagent M2). BFL clamps less than
        # fal in practice but the matchTemplate ±15px alignment window
        # in _composite_onto_result is still vulnerable to a coordinate-
        # system mismatch on the rare occasions BFL DOES clamp. Cheap
        # insurance, identical shape to the fal-side resize at the
        # always-composite block above.
        if (actual_w, actual_h) != (expected_w, expected_h):
            try:
                with Image.open(output_path) as dl:
                    dl_rgb = dl.convert("RGB")
                    resized = dl_rgb.resize(
                        (expected_w, expected_h),
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
                        f"BFL composite: resized output "
                        f"{actual_w}x{actual_h} -> {expected_w}x{expected_h} "
                        "(Lanczos) to match provider-coordinate expected canvas"
                    ),
                    "info",
                )
            except Exception as exc:
                self._report(
                    f"Could not resize BFL output to expected canvas: {exc}",
                    "error",
                )
                self._set_last_outpaint_error_detail(
                    f"bfl_resize_failed:{type(exc).__name__}"
                )
                try:
                    os.unlink(output_path)
                except OSError:
                    pass
                return None

        # 7. Composite: paste original sharp pixels over AI center.
        # Same preserve-contract check as the fal.ai path (PR
        # fix/step0-composite-and-rppg-v2.5): if the composite fails
        # for any preserve mode, reject the output instead of silently
        # shipping a non-composited result. Both paths use the same
        # provider-coordinate composite source (round 10 revert) —
        # downscaled `processed_img` + preflight-adjusted margins.
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
