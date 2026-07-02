from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Dict, Optional, Tuple


@dataclass(frozen=True)
class ProviderCaps:
    max_canvas_dim: int
    max_canvas_mp: float
    max_per_side: int


FAL_CAPS = ProviderCaps(max_canvas_dim=1536, max_canvas_mp=2.0, max_per_side=700)
BFL_CAPS = ProviderCaps(max_canvas_dim=2048, max_canvas_mp=1.5, max_per_side=2048)


def compute_provider_caps(provider: str) -> ProviderCaps:
    lowered = provider.lower().strip()
    if lowered == "bfl":
        return BFL_CAPS
    if lowered == "fal":
        return FAL_CAPS
    raise ValueError(f"Unknown provider: {provider}")


def _safe_scale_for_percent_expand(orig_w: int, orig_h: int, p: float, caps: ProviderCaps) -> float:
    factor = 1.0 + 2.0 * p
    mp_limit_px = caps.max_canvas_mp * 1_000_000.0
    return min(
        1.0,
        caps.max_canvas_dim / max(orig_w * factor, 1),
        caps.max_canvas_dim / max(orig_h * factor, 1),
        math.sqrt(mp_limit_px / max(orig_w * orig_h * factor * factor, 1.0)),
    )


def compute_percent_expand_plan(
    orig_w: int,
    orig_h: int,
    expand_percent: float,
    caps: ProviderCaps,
) -> Dict[str, int]:
    if orig_w <= 0 or orig_h <= 0:
        raise ValueError("orig_w and orig_h must be positive integers")
    p = max(0.0, float(expand_percent) / 100.0)
    scale = _safe_scale_for_percent_expand(orig_w=orig_w, orig_h=orig_h, p=p, caps=caps)
    upload_w = max(1, math.floor(orig_w * scale))
    upload_h = max(1, math.floor(orig_h * scale))
    target_canvas_w = int(round(upload_w * (1.0 + 2.0 * p)))
    target_canvas_h = int(round(upload_h * (1.0 + 2.0 * p)))
    target_expand_w = max(0, target_canvas_w - upload_w)
    target_expand_h = max(0, target_canvas_h - upload_h)
    left = min(caps.max_per_side, target_expand_w // 2)
    right = min(caps.max_per_side, target_expand_w - left)
    top = min(caps.max_per_side, target_expand_h // 2)
    bottom = min(caps.max_per_side, target_expand_h - top)
    canvas_w = upload_w + left + right
    canvas_h = upload_h + top + bottom
    return {
        "upload_w": upload_w,
        "upload_h": upload_h,
        "left": left,
        "right": right,
        "top": top,
        "bottom": bottom,
        "canvas_w": canvas_w,
        "canvas_h": canvas_h,
        "scale_pct": int(round(scale * 100)),
    }


def resolve_border_strategy(config: Optional[dict], has_fal_key: bool) -> str:
    """Pick the full-res border engine.

    ``config['outpaint_border_strategy']`` wins if set to a known value
    ("bria" | "edge_extend" | "ai"). Otherwise default to "bria" (best quality)
    when a fal key is available, else "edge_extend" (free, offline, no key).
    """
    known = {"bria", "edge_extend", "ai"}
    val = ""
    if isinstance(config, dict):
        val = str(config.get("outpaint_border_strategy", "") or "").strip().lower()
    if val in known:
        # "bria"/"ai" need a fal key; fall back to the free engine without one.
        if val in {"bria", "ai"} and not has_fal_key:
            return "edge_extend"
        return val
    return "bria" if has_fal_key else "edge_extend"


def compute_full_res_expand_plan(
    orig_w: int,
    orig_h: int,
    expand_percent: float,
    caps: ProviderCaps,
    target_aspect: Optional[Tuple[int, int]] = None,
) -> Dict[str, int]:
    """Plan a *full-resolution* expand.

    Unlike :func:`compute_percent_expand_plan` (which returns margins in the
    downscaled provider coordinate system and discards the original's real
    resolution), this returns BOTH:

    - ``full_left/right/top/bottom`` + ``full_canvas_w/h`` — margins at the
      ORIGINAL image's native resolution. The final composited canvas is built
      at these dimensions and the untouched original is hard-pasted into the
      center, so it stays pixel-perfect.
    - ``left/right/top/bottom`` + ``upload_w/h`` + ``canvas_w/h`` — the same
      geometry scaled DOWN by ``scale`` to fit the provider caps. These are what
      we actually send to the outpaint provider (it only generates the borders;
      resolution there doesn't matter because we upscale just the border strips
      on the way back).

    ``scale`` (0<scale<=1) is ``upload_dim / orig_dim`` — the factor the border
    strips are upscaled by (``1/scale``) during full-res assembly.

    When ``target_aspect`` is given (e.g. ``(3, 4)``), the expand is a *zoom-out
    to that aspect*: apply ``expand_percent`` uniformly on all sides first (the
    "snapped from further away" effect the user wants), then grow only the
    deficient axis until the canvas hits the target aspect exactly. Without
    ``target_aspect`` it is a plain symmetric percentage expand.
    """
    if orig_w <= 0 or orig_h <= 0:
        raise ValueError("orig_w and orig_h must be positive integers")
    p = max(0.0, float(expand_percent) / 100.0)

    # --- 1. Full-resolution target canvas ---
    if target_aspect is not None:
        tw, th = target_aspect
        if tw <= 0 or th <= 0:
            raise ValueError("Invalid target aspect ratio.")
        target_ratio = tw / th  # width / height
        base_w = orig_w * (1.0 + 2.0 * p)
        base_h = orig_h * (1.0 + 2.0 * p)
        if base_w / base_h > target_ratio:
            # too wide for the target -> grow height
            final_w = base_w
            final_h = base_w / target_ratio
        else:
            # too tall (or exact) -> grow width
            final_h = base_h
            final_w = base_h * target_ratio
        full_canvas_w = int(round(final_w))
        full_canvas_h = int(round(final_h))
    else:
        full_canvas_w = int(round(orig_w * (1.0 + 2.0 * p)))
        full_canvas_h = int(round(orig_h * (1.0 + 2.0 * p)))

    # Never let rounding make the canvas smaller than the original.
    full_canvas_w = max(full_canvas_w, orig_w)
    full_canvas_h = max(full_canvas_h, orig_h)

    full_expand_w = full_canvas_w - orig_w
    full_expand_h = full_canvas_h - orig_h
    full_left = full_expand_w // 2
    full_right = full_expand_w - full_left
    full_top = full_expand_h // 2
    full_bottom = full_expand_h - full_top
    # recompute canvas from the split so full_canvas == orig + margins exactly
    full_canvas_w = orig_w + full_left + full_right
    full_canvas_h = orig_h + full_top + full_bottom

    # --- 2. Provider-coordinate (downscaled) plan ---
    # Fit the FULL canvas inside the caps. This is the scale the provider works
    # at; the original center is composited at full res regardless.
    mp_limit_px = caps.max_canvas_mp * 1_000_000.0
    scale = min(
        1.0,
        caps.max_canvas_dim / max(full_canvas_w, 1),
        caps.max_canvas_dim / max(full_canvas_h, 1),
        math.sqrt(mp_limit_px / max(full_canvas_w * full_canvas_h, 1.0)),
    )
    upload_w = max(1, int(round(orig_w * scale)))
    upload_h = max(1, int(round(orig_h * scale)))
    left = min(caps.max_per_side, int(round(full_left * scale)))
    right = min(caps.max_per_side, int(round(full_right * scale)))
    top = min(caps.max_per_side, int(round(full_top * scale)))
    bottom = min(caps.max_per_side, int(round(full_bottom * scale)))
    canvas_w = upload_w + left + right
    canvas_h = upload_h + top + bottom

    return {
        "full_left": full_left,
        "full_right": full_right,
        "full_top": full_top,
        "full_bottom": full_bottom,
        "full_canvas_w": full_canvas_w,
        "full_canvas_h": full_canvas_h,
        "upload_w": upload_w,
        "upload_h": upload_h,
        "left": left,
        "right": right,
        "top": top,
        "bottom": bottom,
        "canvas_w": canvas_w,
        "canvas_h": canvas_h,
        "scale_pct": int(round(scale * 100)),
    }


def compute_centered_aspect_expand_plan(
    orig_w: int,
    orig_h: int,
    target_aspect: Tuple[int, int],
    caps: ProviderCaps,
) -> Dict[str, int]:
    if orig_w <= 0 or orig_h <= 0:
        raise ValueError("orig_w and orig_h must be positive integers")
    target_w_ratio, target_h_ratio = target_aspect
    if target_w_ratio <= 0 or target_h_ratio <= 0:
        raise ValueError("Invalid target aspect ratio.")

    max_mp_px = int(caps.max_canvas_mp * 1_000_000)
    best: Optional[Dict[str, int]] = None

    for canvas_h in range(caps.max_canvas_dim, 0, -1):
        canvas_w = max(1, int(round(canvas_h * target_w_ratio / target_h_ratio)))
        if canvas_w > caps.max_canvas_dim:
            continue
        if canvas_w * canvas_h > max_mp_px:
            continue

        upload_scale = min(1.0, canvas_w / max(orig_w, 1), canvas_h / max(orig_h, 1))
        upload_w = max(1, int(math.floor(orig_w * upload_scale)))
        upload_h = max(1, int(math.floor(orig_h * upload_scale)))
        if upload_w > canvas_w or upload_h > canvas_h:
            continue

        left = max(0, (canvas_w - upload_w) // 2)
        right = max(0, canvas_w - upload_w - left)
        top = max(0, (canvas_h - upload_h) // 2)
        bottom = max(0, canvas_h - upload_h - top)
        if any(margin > caps.max_per_side for margin in (left, right, top, bottom)):
            continue

        candidate = {
            "upload_w": upload_w,
            "upload_h": upload_h,
            "left": left,
            "right": right,
            "top": top,
            "bottom": bottom,
            "canvas_w": canvas_w,
            "canvas_h": canvas_h,
            "_area": canvas_w * canvas_h,
        }
        if best is None or int(candidate["_area"]) > int(best["_area"]):
            best = candidate

    if best is None:
        upload_w = max(1, min(orig_w, caps.max_canvas_dim))
        upload_h = max(1, min(orig_h, caps.max_canvas_dim))
        max_pixels = int(caps.max_canvas_mp * 1_000_000)
        if upload_w * upload_h > max_pixels:
            factor = math.sqrt(max_pixels / float(upload_w * upload_h))
            upload_w = max(1, int(math.floor(upload_w * factor)))
            upload_h = max(1, int(math.floor(upload_h * factor)))
        canvas_scale = min(upload_w / max(orig_w, 1), upload_h / max(orig_h, 1), 1.0)
        return {
            "upload_w": upload_w,
            "upload_h": upload_h,
            "left": 0,
            "right": 0,
            "top": 0,
            "bottom": 0,
            "canvas_w": upload_w,
            "canvas_h": upload_h,
            "scale_pct": int(round(canvas_scale * 100)),
        }

    canvas_scale = min(best["upload_w"] / max(orig_w, 1), best["upload_h"] / max(orig_h, 1), 1.0)

    return {
        "upload_w": int(best["upload_w"]),
        "upload_h": int(best["upload_h"]),
        "left": int(best["left"]),
        "right": int(best["right"]),
        "top": int(best["top"]),
        "bottom": int(best["bottom"]),
        "canvas_w": int(best["canvas_w"]),
        "canvas_h": int(best["canvas_h"]),
        "scale_pct": int(round(canvas_scale * 100)),
    }
