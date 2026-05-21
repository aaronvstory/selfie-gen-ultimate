from pathlib import Path

from PIL import Image

from outpaint_generator import OutpaintGenerator
import outpaint_generator


def _build_source_image(width: int, height: int) -> Image.Image:
    src = Image.new("RGB", (width, height))
    for y in range(height):
        for x in range(width):
            src.putpixel((x, y), ((x * 17) % 256, (y * 29) % 256, ((x + y) * 13) % 256))
    return src


def test_read_int_env_invalid_falls_back_to_default(monkeypatch):
    monkeypatch.setenv("BFL_MAX_WAIT_SECONDS", "not-an-int")
    monkeypatch.setenv("BFL_EXPAND_MAX_WAIT_SECONDS", "also-bad")
    assert outpaint_generator._read_int_env("BFL_MAX_WAIT_SECONDS", "BFL_EXPAND_MAX_WAIT_SECONDS", 30) == 30


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

    # B2) all four seam corners are blended (outside-only corner coverage)
    corner_points = [
        (margin_left - 1, margin_top - 1),
        (margin_left + src.width, margin_top - 1),
        (margin_left - 1, margin_top + src.height),
        (margin_left + src.width, margin_top + src.height),
    ]
    for px in corner_points:
        assert after.getpixel(px) != before.getpixel(px)

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


def test_preflight_high_res_respects_caps(tmp_path: Path):
    src_path = tmp_path / "hires.png"
    Image.new("RGB", (2720, 4032), (120, 30, 210)).save(src_path)

    max_size, adj_l, adj_r, adj_t, adj_b, sim_w, sim_h = OutpaintGenerator._preflight_size(
        image_path=str(src_path),
        expand_left=700,
        expand_right=700,
        expand_top=700,
        expand_bottom=700,
        max_dim=1536,
        max_mp=2.0,
    )
    canvas_w = sim_w + adj_l + adj_r
    canvas_h = sim_h + adj_t + adj_b

    assert max_size >= 256
    assert canvas_w <= 1536
    assert canvas_h <= 1536
    assert (canvas_w * canvas_h) <= 2_000_000
    assert adj_l >= 0 and adj_r >= 0 and adj_t >= 0 and adj_b >= 0


def test_preflight_preserves_zero_margins(tmp_path: Path):
    src_path = tmp_path / "asym.png"
    Image.new("RGB", (2100, 1600), (55, 90, 30)).save(src_path)

    _max_size, adj_l, adj_r, adj_t, adj_b, sim_w, sim_h = OutpaintGenerator._preflight_size(
        image_path=str(src_path),
        expand_left=0,
        expand_right=700,
        expand_top=0,
        expand_bottom=300,
        max_dim=1536,
        max_mp=2.0,
    )
    canvas_w = sim_w + adj_l + adj_r
    canvas_h = sim_h + adj_t + adj_b
    assert adj_l == 0
    assert adj_t == 0
    assert canvas_w <= 1536
    assert canvas_h <= 1536
    assert (canvas_w * canvas_h) <= 2_000_000


def _configure_fake_fal(monkeypatch, source_size=(320, 240), uploaded_size=(300, 220), downloaded_size=(440, 320)):
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


def test_fal_payload_keeps_zoom_out_zero(monkeypatch, tmp_path: Path):
    import fal_utils

    gen = OutpaintGenerator(api_key="x")
    src_path = tmp_path / "input.png"
    Image.new("RGB", (320, 240), (5, 6, 7)).save(src_path)
    uploaded_img = Image.new("RGB", (300, 220), (8, 9, 10))
    captured = {}

    def fake_upload_reference_image(**_kwargs):
        return "https://example.com/ref.jpg", uploaded_img, "fal"

    def fake_queue_submit(_key, _endpoint, payload, _cb):
        captured["payload"] = payload
        return {"status_url": "https://example.com/status", "request_id": "req_1234"}

    def fake_queue_poll(*_args, **_kwargs):
        return {"images": [{"url": "https://example.com/result.png"}]}

    def fake_download_file(_url, output_path, _cb):
        Image.new("RGB", (460, 340), (10, 20, 30)).save(output_path)
        return True

    monkeypatch.setattr(fal_utils, "upload_reference_image", fake_upload_reference_image)
    monkeypatch.setattr(fal_utils, "fal_queue_submit", fake_queue_submit)
    monkeypatch.setattr(fal_utils, "fal_queue_poll", fake_queue_poll)
    monkeypatch.setattr(fal_utils, "fal_download_file", fake_download_file)
    monkeypatch.setattr(gen, "_composite_onto_result", lambda *_args, **_kwargs: None)

    out = gen.outpaint(
        image_path=str(src_path),
        output_folder=str(tmp_path),
        expand_left=10,
        expand_right=11,
        expand_top=12,
        expand_bottom=13,
        provider="fal",
        composite_mode="feathered",
    )
    assert out is not None
    assert captured["payload"]["zoom_out_percentage"] == 0


def test_fal_uses_uploaded_processed_image_for_composite(monkeypatch, tmp_path: Path):
    gen = OutpaintGenerator(api_key="x")
    src_path = tmp_path / "input.png"
    Image.new("RGB", (320, 240), (1, 2, 3)).save(src_path)
    _source_img, uploaded_img = _configure_fake_fal(
        monkeypatch,
        source_size=(320, 240),
        uploaded_size=(300, 220),
        downloaded_size=(460, 340),
    )

    captured = {}

    def fake_composite(output_path, orig, margin_left, margin_right, margin_top, margin_bottom, output_format, composite_mode):
        captured["orig_size"] = orig.size
        captured["margins"] = (margin_left, margin_right, margin_top, margin_bottom)
        captured["mode"] = composite_mode
        captured["output_path"] = output_path

    monkeypatch.setattr(gen, "_composite_onto_result", fake_composite)

    out = gen.outpaint(
        image_path=str(src_path),
        output_folder=str(tmp_path),
        expand_left=30,
        expand_right=30,
        expand_top=20,
        expand_bottom=20,
        provider="fal",
        composite_mode="feathered",
        edge_seal_px=0,
    )

    assert out is not None
    assert captured["orig_size"] == uploaded_img.size
    assert captured["mode"] == "feathered"


def test_fal_edge_seal_upload_only_uses_unsealed_composite_source(monkeypatch, tmp_path: Path):
    gen = OutpaintGenerator(api_key="x")
    src_path = tmp_path / "input.png"
    Image.new("RGB", (320, 240), (25, 35, 45)).save(src_path)
    _source_img, _uploaded_img = _configure_fake_fal(
        monkeypatch,
        source_size=(320, 240),
        uploaded_size=(310, 230),
        downloaded_size=(460, 340),
    )

    captured = {}

    def fake_composite(output_path, orig, margin_left, margin_right, margin_top, margin_bottom, output_format, composite_mode):
        captured["orig_size"] = orig.size
        captured["sample_pixel"] = orig.getpixel((0, 0))

    monkeypatch.setattr(gen, "_composite_onto_result", fake_composite)

    out = gen.outpaint(
        image_path=str(src_path),
        output_folder=str(tmp_path),
        expand_left=30,
        expand_right=30,
        expand_top=20,
        expand_bottom=20,
        provider="fal",
        composite_mode="feathered",
        edge_seal_px=8,
    )
    assert out is not None
    # Unsealed source path should keep local processed dimensions, not uploaded sealed variant.
    assert captured["orig_size"] == (320, 240)


def test_fal_underflow_guard_skips_composite(monkeypatch, tmp_path: Path):
    gen = OutpaintGenerator(api_key="x")
    src_path = tmp_path / "input.png"
    Image.new("RGB", (640, 480), (90, 60, 40)).save(src_path)
    _source_img, _uploaded_img = _configure_fake_fal(
        monkeypatch,
        source_size=(640, 480),
        uploaded_size=(640, 480),
        downloaded_size=(200, 150),
    )

    called = {"composite": 0}

    def fake_composite(*_args, **_kwargs):
        called["composite"] += 1

    monkeypatch.setattr(gen, "_composite_onto_result", fake_composite)

    out = gen.outpaint(
        image_path=str(src_path),
        output_folder=str(tmp_path),
        expand_left=200,
        expand_right=200,
        expand_top=100,
        expand_bottom=100,
        provider="fal",
        composite_mode="preserve_seamless",
        edge_seal_px=0,
    )
    assert out is not None
    assert called["composite"] == 0


def test_fal_dimension_read_failure_skips_composite(monkeypatch, tmp_path: Path):
    import fal_utils

    gen = OutpaintGenerator(api_key="x")
    src_path = tmp_path / "input.png"
    Image.new("RGB", (640, 480), (90, 60, 40)).save(src_path)
    _source_img, _uploaded_img = _configure_fake_fal(
        monkeypatch,
        source_size=(640, 480),
        uploaded_size=(640, 480),
        downloaded_size=(960, 680),
    )

    called = {"composite": 0}
    logs = []

    def fake_composite(*_args, **_kwargs):
        called["composite"] += 1

    def capture_log(message: str, level: str = "info"):
        logs.append((level, message))

    orig_open = Image.open
    downloaded = {"path": None}

    def fake_download_file(_url, output_path, _cb):
        downloaded["path"] = str(output_path)
        Image.new("RGB", (960, 680), (10, 20, 30)).save(output_path)
        return True

    def fake_open(fp, *args, **kwargs):
        if downloaded["path"] is not None and str(fp) == downloaded["path"]:
            raise OSError("simulated read failure")
        return orig_open(fp, *args, **kwargs)

    gen.set_progress_callback(capture_log)
    monkeypatch.setattr(gen, "_composite_onto_result", fake_composite)
    monkeypatch.setattr(fal_utils, "fal_download_file", fake_download_file)
    monkeypatch.setattr("outpaint_generator.Image.open", fake_open)

    out = gen.outpaint(
        image_path=str(src_path),
        output_folder=str(tmp_path),
        expand_left=160,
        expand_right=160,
        expand_top=100,
        expand_bottom=100,
        provider="fal",
        composite_mode="preserve_seamless",
        edge_seal_px=0,
    )

    assert out is not None
    assert called["composite"] == 0
    assert any(level == "warning" and "Could not read downloaded output dimensions" in message for level, message in logs)


def test_bfl_pending_timeout_sets_reason_and_logs(monkeypatch, tmp_path: Path):
    gen = OutpaintGenerator(api_key="x", bfl_api_key="bfl-key")
    src_path = tmp_path / "input.png"
    Image.new("RGB", (320, 240), (10, 20, 30)).save(src_path)

    logs = []

    def capture_log(message: str, level: str = "info"):
        logs.append((level, message))

    class _Resp:
        def __init__(self, payload):
            self._payload = payload

        def raise_for_status(self):
            return None

        def json(self):
            return self._payload

    poll_payload = {"status": "Pending", "id": "task-xyz"}

    def fake_post(*_args, **_kwargs):
        return _Resp({"polling_url": "https://poll.example", "id": "task-xyz"})

    def fake_get(*_args, **_kwargs):
        return _Resp(poll_payload)

    ticks = iter([0, 6, 12, 18, 24, 30, 36, 42])

    def fake_monotonic():
        return next(ticks)

    monkeypatch.setattr("requests.post", fake_post)
    monkeypatch.setattr("requests.get", fake_get)
    monkeypatch.setattr(outpaint_generator.time, "sleep", lambda _sec: None)
    monkeypatch.setattr(outpaint_generator.time, "monotonic", fake_monotonic)

    gen.set_progress_callback(capture_log)
    out = gen.outpaint(
        image_path=str(src_path),
        output_folder=str(tmp_path),
        expand_left=80,
        expand_right=80,
        expand_top=60,
        expand_bottom=60,
        provider="bfl",
        composite_mode="preserve_seamless",
    )

    assert out is None
    detail = gen.get_last_outpaint_error_detail()
    assert "reason=pending_timeout" in detail
    assert "task=task-xyz" in detail
    assert any("BFL Expand timed out after" in message and "reason=pending_timeout" in message for _, message in logs)


# ──────────────────────────────────────────────────────────────────────
# Phase C of polish/v2.3 (2026-05-22) — LANCZOS ring blend + ±16px
# underflow tolerance.
# ──────────────────────────────────────────────────────────────────────


def test_underflow_tolerance_constant_is_16px():
    """The tolerance value is locked in so a future edit can't silently
    raise it (corrupting preserved pixels) or drop it (re-introducing
    the regression where 12px shortfalls disabled the composite)."""
    assert OutpaintGenerator._UNDERFLOW_TOLERANCE_PX == 16


def test_seam_ring_resize_uses_lanczos_not_bilinear():
    """Phase C: the 5 seam-ring band resizes in
    _composite_onto_result.preserve_seamless were swapped from
    BILINEAR to LANCZOS for sharper edge gradients. Locked via source
    so a refactor that re-introduces BILINEAR on the ring fails
    here."""
    src = (Path(__file__).resolve().parent.parent / "outpaint_generator.py").read_text(encoding='utf-8')
    # No BILINEAR remaining in the ring-blend bands (top/bottom/left/right strips).
    # All 5 resize calls in those bands should be LANCZOS.
    # Count occurrences: there were 5 BILINEAR calls in the ring code.
    # After Phase C, we expect 0 in the ring-blend strips.
    # (The preflight thumbnail still uses LANCZOS — line 191 — which is fine.)
    # Easier check: count "Resampling.LANCZOS" — file should have >= 6 now (5 strip resizes + 1 preflight).
    lanczos_count = src.count("Resampling.LANCZOS")
    assert lanczos_count >= 6, (
        f"expected >=6 LANCZOS resamplings post-Phase C, found {lanczos_count}"
    )
    # And no BILINEAR remains in the file (the 5 strip calls were the
    # only ones — confirm).
    bilinear_count = src.count("Resampling.BILINEAR")
    assert bilinear_count == 0, (
        f"expected 0 BILINEAR resamplings post-Phase C, found {bilinear_count}"
    )


def test_underflow_gate_relaxed_with_tolerance_in_source():
    """Phase C: the new underflow gate computes short_w/short_h, only
    disables composite when shortage exceeds tolerance, and emits a
    warning log when shortage is within tolerance. Source-asserted
    so a future edit reverting to the strict ANY-shortage gate fails
    this test."""
    src = (Path(__file__).resolve().parent.parent / "outpaint_generator.py").read_text(encoding='utf-8')
    # The relaxed gate creates short_w/short_h variables...
    assert "short_w = max(0, expected_canvas_w - downloaded_w)" in src
    assert "short_h = max(0, expected_canvas_h - downloaded_h)" in src
    # ...and only disables composite when within_tolerance is False.
    assert "if underflow and not within_tolerance:" in src
    # ...and emits a "composite applied with proportional margin rescale"
    # log when tolerance is hit.
    assert "composite applied with proportional margin rescale" in src
