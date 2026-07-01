"""Full-resolution expand: geometry + composite pixel-fidelity tests.

The whole point of the full-res path is that the ORIGINAL image survives at
native resolution (pixel-perfect center), while only the generated borders are
upscaled/soft. These tests assert that contract without any network call.
"""
import numpy as np
import pytest
from PIL import Image

from outpaint_geometry import (
    FAL_CAPS,
    BFL_CAPS,
    compute_full_res_expand_plan,
)


# ── geometry ─────────────────────────────────────────────────────────────


def test_wendy_landscape_to_3x4_matches_photoshop():
    # Real example: 4080x3060 landscape, 30% zoom-out -> ~6528x8704 (Photoshop
    # manual redo was 6499x8664).
    p = compute_full_res_expand_plan(4080, 3060, 30, FAL_CAPS, (3, 4))
    assert p["full_canvas_w"] == 6528
    assert p["full_canvas_h"] == 8704
    assert p["full_left"] == p["full_right"] == 1224
    assert p["full_top"] == p["full_bottom"] == 2822
    # exact 3:4
    assert abs(p["full_canvas_w"] / p["full_canvas_h"] - 3 / 4) < 1e-3


@pytest.mark.parametrize(
    "w,h",
    [(4080, 3060), (2000, 3000), (2000, 2000), (400, 300), (1000, 4000)],
)
@pytest.mark.parametrize("caps", [FAL_CAPS, BFL_CAPS])
def test_3x4_always_hits_aspect_and_fits_caps(w, h, caps):
    p = compute_full_res_expand_plan(w, h, 30, caps, (3, 4))
    # exact target aspect at full res
    assert abs(p["full_canvas_w"] / p["full_canvas_h"] - 3 / 4) < 5e-3
    # canvas == orig + margins
    assert p["full_canvas_w"] == w + p["full_left"] + p["full_right"]
    assert p["full_canvas_h"] == h + p["full_top"] + p["full_bottom"]
    # provider canvas within caps (+1 rounding slack)
    assert p["canvas_w"] <= caps.max_canvas_dim + 1
    assert p["canvas_h"] <= caps.max_canvas_dim + 1
    assert p["canvas_w"] * p["canvas_h"] <= caps.max_canvas_mp * 1e6 * 1.02


def test_percentage_fullres_no_aspect():
    p = compute_full_res_expand_plan(4080, 3060, 30, FAL_CAPS, None)
    assert p["full_canvas_w"] == 6528  # 4080 * 1.6
    assert p["full_canvas_h"] == 4896  # 3060 * 1.6


def test_zero_percent_pure_aspect():
    # 0% zoom-out -> only the deficient axis grows to reach 3:4.
    p = compute_full_res_expand_plan(4080, 3060, 0, FAL_CAPS, (3, 4))
    assert p["full_left"] == p["full_right"] == 0
    assert p["full_top"] > 0 and p["full_bottom"] > 0


# ── composite fidelity (no network) ──────────────────────────────────────


def _make_original(path, w, h):
    """A sharp original: unique per-pixel-ish pattern so any resample shows."""
    arr = np.zeros((h, w, 3), dtype=np.uint8)
    xs = (np.arange(w) % 256).astype(np.uint8)
    ys = (np.arange(h) % 256).astype(np.uint8)
    arr[:, :, 0] = xs[None, :]
    arr[:, :, 1] = ys[:, None]
    arr[:, :, 2] = 128
    Image.fromarray(arr).save(path)


def _fake_provider_output(path, plan):
    """Simulate what the provider returns: a canvas at provider size with a
    downscaled center + solid-colored borders (we don't care about border
    content — only the center must be discarded, not used)."""
    cw = plan["left"] + plan["upload_w"] + plan["right"]
    ch = plan["top"] + plan["upload_h"] + plan["bottom"]
    canvas = Image.new("RGB", (cw, ch), (10, 200, 40))  # green borders
    # a gray center block where the (downscaled) original would sit
    center = Image.new("RGB", (plan["upload_w"], plan["upload_h"]), (90, 90, 90))
    canvas.paste(center, (plan["left"], plan["top"]))
    canvas.save(path)


@pytest.mark.parametrize(
    "w,h,aspect",
    [(4080, 3060, (3, 4)), (1200, 1600, (3, 4)), (800, 600, None)],
)
def test_center_is_pixel_perfect(tmp_path, w, h, aspect):
    from outpaint_generator import OutpaintGenerator

    plan = compute_full_res_expand_plan(w, h, 30, FAL_CAPS, aspect)
    orig_p = str(tmp_path / "orig.png")
    raw_p = str(tmp_path / "raw.png")
    out_p = str(tmp_path / "out.png")
    _make_original(orig_p, w, h)
    _fake_provider_output(raw_p, plan)

    gen = OutpaintGenerator(api_key="x")
    ok = gen._composite_fullres(
        raw_p, orig_p, out_p, plan, "png", "hard"
    )
    assert ok
    out = Image.open(out_p).convert("RGB")
    # canvas dims == full-res plan
    assert out.size == (plan["full_canvas_w"], plan["full_canvas_h"])

    # center rect [full_left:+w, full_top:+h] is byte-identical to the original
    fl, ft = plan["full_left"], plan["full_top"]
    center = out.crop((fl, ft, fl + w, ft + h))
    orig = Image.open(orig_p).convert("RGB")
    assert np.array_equal(np.array(center), np.array(orig)), (
        "original center was not preserved pixel-perfect"
    )


def test_preserve_seamless_keeps_center_sharp(tmp_path):
    """The seam-ring blend must NOT alter the original's interior."""
    from outpaint_generator import OutpaintGenerator

    w, h = 1200, 1600
    plan = compute_full_res_expand_plan(w, h, 30, FAL_CAPS, (3, 4))
    orig_p = str(tmp_path / "orig.png")
    raw_p = str(tmp_path / "raw.png")
    out_p = str(tmp_path / "out.png")
    _make_original(orig_p, w, h)
    _fake_provider_output(raw_p, plan)

    gen = OutpaintGenerator(api_key="x")
    assert gen._composite_fullres(
        raw_p, orig_p, out_p, plan, "png", "preserve_seamless"
    )
    out = Image.open(out_p).convert("RGB")
    fl, ft = plan["full_left"], plan["full_top"]
    center = out.crop((fl, ft, fl + w, ft + h))
    orig = Image.open(orig_p).convert("RGB")
    assert np.array_equal(np.array(center), np.array(orig))


if __name__ == "__main__":
    import sys
    sys.exit(pytest.main([__file__, "-q"]))
