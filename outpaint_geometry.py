from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Dict, Tuple


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
    p = max(0.0, float(expand_percent) / 100.0)
    scale = _safe_scale_for_percent_expand(orig_w=orig_w, orig_h=orig_h, p=p, caps=caps)
    upload_w = max(1, math.floor(orig_w * scale))
    upload_h = max(1, math.floor(orig_h * scale))
    left = min(caps.max_per_side, max(0, round(upload_w * p)))
    right = min(caps.max_per_side, max(0, round(upload_w * p)))
    top = min(caps.max_per_side, max(0, round(upload_h * p)))
    bottom = min(caps.max_per_side, max(0, round(upload_h * p)))
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
    target_w_ratio, target_h_ratio = target_aspect
    if target_w_ratio <= 0 or target_h_ratio <= 0:
        raise ValueError("Invalid target aspect ratio.")

    max_mp_px = int(caps.max_canvas_mp * 1_000_000)
    canvas_w = caps.max_canvas_dim
    canvas_h = int(canvas_w * target_h_ratio / target_w_ratio)
    if canvas_h > caps.max_canvas_dim:
        canvas_h = caps.max_canvas_dim
        canvas_w = int(canvas_h * target_w_ratio / target_h_ratio)

    if canvas_w * canvas_h > max_mp_px:
        mp_scale = math.sqrt(max_mp_px / float(canvas_w * canvas_h))
        canvas_w = max(1, int(canvas_w * mp_scale))
        canvas_h = max(1, int(canvas_h * mp_scale))

    canvas_scale = min(canvas_w / max(orig_w, 1), canvas_h / max(orig_h, 1), 1.0)
    upload_w = max(1, int(orig_w * canvas_scale))
    upload_h = max(1, int(orig_h * canvas_scale))

    left = max(0, (canvas_w - upload_w) // 2)
    right = max(0, canvas_w - upload_w - left)
    top = max(0, (canvas_h - upload_h) // 2)
    bottom = max(0, canvas_h - upload_h - top)

    if caps.max_per_side > 0:
        left = min(left, caps.max_per_side)
        right = min(right, caps.max_per_side)
        top = min(top, caps.max_per_side)
        bottom = min(bottom, caps.max_per_side)
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
        "scale_pct": int(round(canvas_scale * 100)),
    }
