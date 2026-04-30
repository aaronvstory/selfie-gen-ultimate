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
