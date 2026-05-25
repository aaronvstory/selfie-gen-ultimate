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


def test_fal_realistic_underflow_at_user_bug_ratio(monkeypatch, tmp_path: Path):
    """PR #53 round 2 — subagent M6: the existing always-composite test
    uses a 5x upscale (200x150 -> 1040x680) which is far beyond fal.ai's
    realistic 1-2% clamp. Add a test at the EXACT user-bug ratio
    (downloaded=1520x1136, target=1535x1151 = 0.98x scale on each axis,
    per the user's log line). Confirms the always-composite contract
    holds at the realistic regression ratio.
    """
    gen = OutpaintGenerator(api_key="x")
    src_path = tmp_path / "input.jpg"
    # Source large enough that user margins drive most of the canvas
    # (mirrors the user's actual 903x677 source).
    Image.new("RGB", (903, 677), (100, 100, 100)).save(src_path)
    _stub_fal_for_outpaint(
        monkeypatch,
        source_size=(903, 677),
        uploaded_size=(903, 677),
        downloaded_size=(1520, 1136),  # exact user-bug underflow shape
    )

    captured = {}

    def fake_composite(
        output_path, orig, margin_left, margin_right,
        margin_top, margin_bottom, output_format, composite_mode,
    ):
        captured["called"] = True
        captured["orig_size"] = orig.size
        captured["margins"] = (margin_left, margin_right, margin_top, margin_bottom)
        from PIL import Image as _Img
        with _Img.open(output_path) as dl:
            captured["on_disk_size"] = dl.size
        return True

    monkeypatch.setattr(gen, "_composite_onto_result", fake_composite)

    # User-requested margins from the log: L=525 R=525 T=393 B=393
    out = gen.outpaint(
        image_path=str(src_path),
        output_folder=str(tmp_path),
        expand_left=525,
        expand_right=525,
        expand_top=393,
        expand_bottom=393,
        provider="fal",
        composite_mode="preserve_seamless",
        edge_seal_px=0,
    )
    assert out is not None
    assert captured.get("called") is True
    # Full-res original is the paste source
    assert captured["orig_size"] == (903, 677)
    # Full-res margins
    assert captured["margins"] == (525, 525, 393, 393)
    # On-disk canvas resized to FULL final dims: 903+525+525 = 1953
    # wide, 677+393+393 = 1463 tall. The fal output (1520x1136, the
    # underflow shape) has been upscaled to the full canvas before
    # composite — the matchTemplate alignment can now lock cleanly.
    assert captured["on_disk_size"] == (1953, 1463)


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


def test_composite_mode_none_short_circuits_before_source_reopen(
    monkeypatch, tmp_path: Path,
):
    """PR #53 round 9 — Codex P2: ``composite_mode="none"`` explicitly
    asks for raw provider output. The always-composite path that
    reopens ``image_path`` to build ``orig_full`` is irrelevant for
    "none" mode, so a source-reopen failure (file moved between
    upload and download) must NOT delete the perfectly-good
    downloaded output. The "none" branch short-circuits BEFORE the
    reopen.
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
        # First two calls (preflight + _prepare_processed_image) succeed.
        # If outpaint() reaches the 3rd open on src_path the test fails
        # because the "none" short-circuit should have fired by then.
        if str(fp) == str(src_path):
            call_counter["n"] += 1
            if call_counter["n"] >= 3:
                raise OSError(
                    "Source reopen should NOT happen for composite_mode='none'"
                )
        return orig_open(fp, *args, **kwargs)

    monkeypatch.setattr("outpaint_generator.Image.open", fake_open)

    out = gen.outpaint(
        image_path=str(src_path),
        output_folder=str(tmp_path),
        expand_left=40,
        expand_right=40,
        expand_top=20,
        expand_bottom=20,
        provider="fal",
        composite_mode="none",
        edge_seal_px=0,
    )

    assert out is not None
    import os as _os
    assert _os.path.exists(out), (
        "Downloaded output should still exist on disk for "
        "composite_mode='none' — was the early-return missing?"
    )


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
