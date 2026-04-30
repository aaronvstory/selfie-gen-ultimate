from pathlib import Path

from PIL import Image

from outpaint_generator import OutpaintGenerator
from outpaint_geometry import BFL_CAPS, compute_centered_aspect_expand_plan


def test_edge_seal_changes_copy_only(tmp_path: Path):
    src = Image.new("RGB", (100, 80), (128, 128, 128))
    src_path = tmp_path / "input.png"
    src.save(src_path)

    generator = OutpaintGenerator(api_key="x")
    processed = generator._prepare_processed_image(str(src_path), max_size=1200)
    sealed = generator._edge_seal_copy(processed, edge_seal_px=8, color=(220, 220, 220))

    center_before = processed.getpixel((50, 40))
    center_after = sealed.getpixel((50, 40))
    border_before = processed.getpixel((0, 0))
    border_after = sealed.getpixel((0, 0))

    assert center_before == center_after
    assert border_before != border_after


def test_provider_override_bfl_requires_key(tmp_path: Path):
    src = Image.new("RGB", (50, 50), (100, 100, 100))
    src_path = tmp_path / "in.png"
    src.save(src_path)
    out_dir = tmp_path / "out"
    out_dir.mkdir()

    generator = OutpaintGenerator(api_key="x", bfl_api_key=None)
    result = generator.outpaint(
        image_path=str(src_path),
        output_folder=str(out_dir),
        provider="bfl",
    )
    assert result is None


def test_document_mode_geometry_plan_is_portrait():
    plan = compute_centered_aspect_expand_plan(1200, 700, (3, 4), BFL_CAPS)
    assert plan["canvas_h"] > plan["canvas_w"]


def test_prepare_processed_image_flattens_alpha_with_white_matte(tmp_path: Path):
    src = Image.new("RGBA", (2, 1), (0, 0, 0, 0))
    src.putpixel((1, 0), (10, 20, 30, 255))
    src_path = tmp_path / "rgba.png"
    src.save(src_path)

    generator = OutpaintGenerator(api_key="x")
    processed = generator._prepare_processed_image(str(src_path), max_size=1200)

    # Transparent pixel should flatten to white matte, not black.
    assert processed.getpixel((0, 0)) == (255, 255, 255)
    # Opaque pixel preserved.
    assert processed.getpixel((1, 0)) == (10, 20, 30)


def test_prepare_processed_image_is_provider_path_consistent(tmp_path: Path):
    src = Image.new("RGBA", (32, 16), (200, 100, 50, 128))
    src_path = tmp_path / "rgba2.png"
    src.save(src_path)

    generator = OutpaintGenerator(api_key="x")
    fal_prep = generator._prepare_processed_image(str(src_path), max_size=1200)
    bfl_prep = generator._prepare_processed_image(str(src_path), max_size=1200)

    assert fal_prep.size == bfl_prep.size
    assert fal_prep.tobytes() == bfl_prep.tobytes()
