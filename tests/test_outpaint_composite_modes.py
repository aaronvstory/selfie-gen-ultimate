from pathlib import Path

from PIL import Image

from outpaint_generator import OutpaintGenerator


def _build_source_image(width: int, height: int) -> Image.Image:
    src = Image.new("RGB", (width, height))
    for y in range(height):
        for x in range(width):
            src.putpixel((x, y), ((x * 17) % 256, (y * 29) % 256, ((x + y) * 13) % 256))
    return src


def test_preserve_seamless_exact_center_and_outside_ring_blend(tmp_path: Path):
    gen = OutpaintGenerator(api_key="x")
    src = _build_source_image(40, 30)
    margin_left = 40
    margin_right = 40
    margin_top = 40
    margin_bottom = 40
    canvas_w = src.width + margin_left + margin_right
    canvas_h = src.height + margin_top + margin_bottom

    raw = Image.new("RGB", (canvas_w, canvas_h), (10, 25, 180))
    raw.paste(src, (margin_left, margin_top))
    # Corrupt one center pixel so exact hard-preserve still has meaningful work.
    raw.putpixel((margin_left + src.width // 2, margin_top + src.height // 2), (200, 30, 30))
    output_path = tmp_path / "preserve.png"
    raw.save(output_path)
    before = raw.copy()

    gen._composite_onto_result(
        str(output_path),
        src,
        margin_left,
        margin_right,
        margin_top,
        margin_bottom,
        "png",
        "preserve_seamless",
    )
    after = Image.open(output_path).convert("RGB")

    # A) center exactness
    center_box = (margin_left, margin_top, margin_left + src.width, margin_top + src.height)
    assert after.crop(center_box).tobytes() == src.tobytes()

    # B) outside seam ring changed
    seam_px = gen._PRESERVE_SEAM_BLEND_PX
    top_ring_before = before.crop((margin_left, margin_top - seam_px, margin_left + src.width, margin_top))
    top_ring_after = after.crop((margin_left, margin_top - seam_px, margin_left + src.width, margin_top))
    assert top_ring_before.tobytes() != top_ring_after.tobytes()

    # C) far outside seam ring unchanged
    assert after.getpixel((0, 0)) == before.getpixel((0, 0))


def test_legacy_modes_none_hard_feathered_still_work(tmp_path: Path):
    gen = OutpaintGenerator(api_key="x")
    src = _build_source_image(20, 14)
    margins = (16, 16, 12, 12)
    out_w = src.width + margins[0] + margins[1]
    out_h = src.height + margins[2] + margins[3]

    # none
    none_path = tmp_path / "none.png"
    none_img = Image.new("RGB", (out_w, out_h), (50, 60, 70))
    none_img.save(none_path)
    before_none = none_img.tobytes()
    gen._composite_onto_result(str(none_path), src, *margins, "png", "none")
    assert Image.open(none_path).convert("RGB").tobytes() == before_none

    # hard
    hard_path = tmp_path / "hard.png"
    hard_img = Image.new("RGB", (out_w, out_h), (90, 80, 20))
    hard_img.paste(src, (margins[0], margins[2]))
    hard_img.putpixel((margins[0] + src.width // 2, margins[2] + src.height // 2), (1, 2, 3))
    hard_img.save(hard_path)
    gen._composite_onto_result(str(hard_path), src, *margins, "png", "hard")
    hard_after = Image.open(hard_path).convert("RGB")
    hard_box = (margins[0], margins[2], margins[0] + src.width, margins[2] + src.height)
    assert hard_after.crop(hard_box).tobytes() == src.tobytes()

    # feathered
    feathered_path = tmp_path / "feathered.png"
    feathered_img = Image.new("RGB", (out_w, out_h), (20, 140, 60))
    feathered_img.paste(src, (margins[0], margins[2]))
    feathered_img.putpixel((margins[0] + src.width // 2, margins[2] + src.height // 2), (5, 6, 7))
    feathered_img.save(feathered_path)
    gen._composite_onto_result(str(feathered_path), src, *margins, "png", "feathered")
    feathered_after = Image.open(feathered_path).convert("RGB")
    # Ensure feathered mode still blends center away from the corrupted input value.
    center_x = margins[0] + src.width // 2
    center_y = margins[2] + src.height // 2
    assert feathered_after.getpixel((center_x, center_y)) != (5, 6, 7)
