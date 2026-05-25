"""Regression tests for the PR fix/step0-composite-and-rppg-v2.5
preserve-seamless always-on contract.

These complement tests/test_outpaint_composite_modes.py:
- `test_fal_underflow_upscales_and_composites` covers the always-composite
  path on slight underflow.
- `test_fal_dimension_read_failure_rejects_output` covers the strict-reject
  path on unreadable downloads.

The tests here cover:
- `_composite_onto_result` returns the documented bool.
- Preserve modes that hit the "Original doesn't fit" guard reject the
  output (no silent raw return).
- Source-image reopen failure also rejects the output.
"""

from pathlib import Path

from PIL import Image

import outpaint_generator
from outpaint_generator import OutpaintGenerator


def _stub_fal_for_outpaint(
    monkeypatch,
    *,
    source_size=(320, 240),
    uploaded_size=(320, 240),
    downloaded_size=(440, 320),
):
    import fal_utils

    source_img = Image.new("RGB", source_size, (40, 70, 100))
    uploaded_img = Image.new("RGB", uploaded_size, (80, 20, 60))

    def fake_upload_reference_image(**_kwargs):
        return "https://example.com/ref.jpg", uploaded_img, "fal"

    def fake_queue_submit(*_args, **_kwargs):
        return {"status_url": "https://example.com/status", "request_id": "req_1234"}

    def fake_queue_poll(*_args, **_kwargs):
        return {"images": [{"url": "https://example.com/result.png"}]}

    def fake_download_file(_url, output_path, _cb):
        Image.new("RGB", downloaded_size, (10, 20, 30)).save(output_path)
        return True

    monkeypatch.setattr(fal_utils, "upload_reference_image", fake_upload_reference_image)
    monkeypatch.setattr(fal_utils, "fal_queue_submit", fake_queue_submit)
    monkeypatch.setattr(fal_utils, "fal_queue_poll", fake_queue_poll)
    monkeypatch.setattr(fal_utils, "fal_download_file", fake_download_file)
    return source_img, uploaded_img


def test_composite_onto_result_returns_bool(tmp_path: Path):
    """`_composite_onto_result` must return True on success / False on
    every bail-out branch — caller relies on the return value to
    decide whether to reject the output in preserve mode.
    """
    gen = OutpaintGenerator(api_key="x")

    # Success: hard mode applied
    src = Image.new("RGB", (40, 30), (10, 20, 30))
    margins = (20, 20, 15, 15)
    canvas_w = src.width + margins[0] + margins[1]
    canvas_h = src.height + margins[2] + margins[3]
    output = tmp_path / "ok.png"
    raw = Image.new("RGB", (canvas_w, canvas_h), (200, 0, 0))
    raw.paste(src, (margins[0], margins[2]))
    raw.save(output)
    assert gen._composite_onto_result(
        str(output), src, *margins, "png", "hard",
    ) is True

    # Bail-out: mode="none" returns False (no composite applied).
    none_path = tmp_path / "none.png"
    Image.new("RGB", (canvas_w, canvas_h), (50, 50, 50)).save(none_path)
    assert gen._composite_onto_result(
        str(none_path), src, *margins, "png", "none",
    ) is False

    # Bail-out: original doesn't fit. AI canvas SMALLER than original
    # source — the safety guard short-circuits.
    too_small_path = tmp_path / "too_small.png"
    Image.new("RGB", (10, 10), (0, 0, 0)).save(too_small_path)
    assert gen._composite_onto_result(
        str(too_small_path), src, *margins, "png", "preserve_seamless",
    ) is False


def test_preserve_mode_rejects_when_composite_fails(monkeypatch, tmp_path: Path):
    """Preserve mode + composite failure must return None and delete
    the orphan output — the previous silent "raw output" return shipped
    a non-composited result that downstream stages then propagated as
    if it were a valid preserve-seamless deliverable.
    """
    gen = OutpaintGenerator(api_key="x")
    src_path = tmp_path / "input.png"
    Image.new("RGB", (320, 240), (5, 5, 5)).save(src_path)
    _stub_fal_for_outpaint(
        monkeypatch,
        source_size=(320, 240),
        uploaded_size=(320, 240),
        downloaded_size=(440, 320),
    )

    saw_output_path = {"path": None}

    def failing_composite(
        output_path, orig, margin_left, margin_right,
        margin_top, margin_bottom, output_format, composite_mode,
    ):
        saw_output_path["path"] = output_path
        return False

    monkeypatch.setattr(gen, "_composite_onto_result", failing_composite)

    out = gen.outpaint(
        image_path=str(src_path),
        output_folder=str(tmp_path),
        expand_left=60,
        expand_right=60,
        expand_top=40,
        expand_bottom=40,
        provider="fal",
        composite_mode="preserve_seamless",
        edge_seal_px=0,
    )

    assert out is None
    detail = gen.get_last_outpaint_error_detail()
    assert detail and "composite_failed" in detail
    # Orphan output file is deleted.
    import os
    assert saw_output_path["path"] is not None
    assert not os.path.exists(saw_output_path["path"])


def test_full_res_reopen_applies_exif_transpose(monkeypatch, tmp_path: Path):
    """PR #53 round 1 — Codex P1: a portrait phone photo with EXIF
    Orientation=6 (rotate 270 CW) is stored as a landscape file but
    must render portrait. preflight + _prepare_processed_image both
    apply ImageOps.exif_transpose, so the upload + provider canvas are
    sized in post-rotation coords. The full-res reopen for the composite
    paste source must do the same, otherwise orig_full is the wrong
    dimensions vs the downloaded fal canvas and the resize math goes
    sideways.
    """
    from PIL import Image as _Img, ExifTags

    gen = OutpaintGenerator(api_key="x")
    src_path = tmp_path / "input.jpg"
    # 200x300 portrait stored as 300x200 landscape with Orientation=6.
    landscape = _Img.new("RGB", (300, 200), (10, 20, 30))
    exif = landscape.getexif()
    orientation_tag = next(
        k for k, v in ExifTags.TAGS.items() if v == "Orientation"
    )
    exif[orientation_tag] = 6  # rotate 270 CW => effective 200x300
    landscape.save(src_path, exif=exif.tobytes())

    _stub_fal_for_outpaint(
        monkeypatch,
        source_size=(200, 300),
        uploaded_size=(200, 300),
        downloaded_size=(440, 540),
    )

    captured = {}

    def fake_composite(
        output_path, orig, margin_left, margin_right,
        margin_top, margin_bottom, output_format, composite_mode,
    ):
        captured["orig_size"] = orig.size
        return True

    monkeypatch.setattr(gen, "_composite_onto_result", fake_composite)

    out = gen.outpaint(
        image_path=str(src_path),
        output_folder=str(tmp_path),
        expand_left=40,
        expand_right=40,
        expand_top=60,
        expand_bottom=60,
        provider="fal",
        composite_mode="preserve_seamless",
        edge_seal_px=0,
    )

    assert out is not None
    # Post-EXIF-transpose dims: 200x300 (portrait), NOT the stored
    # 300x200 landscape. If the bug were still present the assertion
    # would see (300, 200) here.
    assert captured["orig_size"] == (200, 300)


def test_source_reopen_failure_rejects_output(monkeypatch, tmp_path: Path):
    """If the source image can't be re-opened for the full-res
    composite (deleted between download and the composite step), the
    new behaviour rejects the output and surfaces an error — not a
    silent raw output.

    The source must be readable at preflight + upload time
    (preflight/upload would have failed loud earlier). The narrow
    window we test is "vanished between upload and the post-download
    re-open".
    """
    gen = OutpaintGenerator(api_key="x")
    src_path = tmp_path / "input.png"
    Image.new("RGB", (320, 240), (5, 5, 5)).save(src_path)
    _stub_fal_for_outpaint(
        monkeypatch,
        source_size=(320, 240),
        uploaded_size=(320, 240),
        downloaded_size=(440, 320),
    )

    orig_open = Image.open
    call_counter = {"n": 0}

    def fake_open(fp, *args, **kwargs):
        # Let everything succeed until the post-download re-open of
        # the source image (the 3rd time outpaint() calls Image.open on
        # src_path: preflight, _prepare_processed_image, then the new
        # full-res re-open). Count source-path opens specifically.
        if str(fp) == str(src_path):
            call_counter["n"] += 1
            if call_counter["n"] >= 3:
                raise OSError("simulated source deletion mid-pass")
        return orig_open(fp, *args, **kwargs)

    monkeypatch.setattr("outpaint_generator.Image.open", fake_open)

    out = gen.outpaint(
        image_path=str(src_path),
        output_folder=str(tmp_path),
        expand_left=60,
        expand_right=60,
        expand_top=40,
        expand_bottom=40,
        provider="fal",
        composite_mode="preserve_seamless",
        edge_seal_px=0,
    )

    assert out is None
    detail = gen.get_last_outpaint_error_detail()
    assert detail and "orig_reopen_failed" in detail
