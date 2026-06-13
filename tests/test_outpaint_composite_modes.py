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

    ok = gen._composite_onto_result(
        str(output_path),
        src,
        margin_left,
        margin_right,
        margin_top,
        margin_bottom,
        "png",
        "preserve_seamless",
    )
    # PR fix/step0-composite-and-rppg-v2.5: returns bool now (True on
    # successful composite, False on any bail-out branch).
    assert ok is True
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
    monkeypatch.setattr(gen, "_composite_onto_result", lambda *_args, **_kwargs: True)

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


def test_fal_uses_composite_source_for_composite(monkeypatch, tmp_path: Path):
    """PR #53 round 10: REVERTED rounds 5..9. The composite paste source
    is `composite_source` (the downscaled uploaded image), NOT the
    full-res original — and the margins are the preflight-adjusted
    values (`adj_left/right/top/bottom`), NOT the user-requested
    full-res margins. This is main's known-good provider-coordinate
    geometry that the round-5..9 experiment broke (paste source +
    margins must match the coordinate system fal generated for, or
    the matchTemplate +-15px search window silently misaligns).

    For source <= max upload size, preflight does NOT downscale so
    adj margins == requested margins AND composite_source size ==
    source size. To test the geometry contract WHERE IT MATTERS,
    use a source large enough to trigger preflight scaling — then
    assert adj margins are SMALLER than requested and composite_source
    is SMALLER than the source.
    """
    gen = OutpaintGenerator(api_key="x")
    src_path = tmp_path / "input.png"
    # Source big enough to trigger preflight scale-down at the
    # fal envelope (1536px / 2.0 MP defaults).
    Image.new("RGB", (3024, 4032), (1, 2, 3)).save(src_path)
    _source_img, _uploaded_img = _configure_fake_fal(
        monkeypatch,
        source_size=(3024, 4032),
        uploaded_size=(682, 910),  # what preflight downscale produces
        downloaded_size=(1304, 1532),  # = composite_source + adj margins
    )

    captured = {}

    def fake_composite(
        output_path, orig, margin_left, margin_right,
        margin_top, margin_bottom, output_format, composite_mode,
    ):
        captured["orig_size"] = orig.size
        captured["margins"] = (margin_left, margin_right, margin_top, margin_bottom)
        captured["mode"] = composite_mode
        captured["output_path"] = output_path
        return True

    monkeypatch.setattr(gen, "_composite_onto_result", fake_composite)

    out = gen.outpaint(
        image_path=str(src_path),
        output_folder=str(tmp_path),
        expand_left=700,
        expand_right=700,
        expand_top=700,
        expand_bottom=700,
        provider="fal",
        composite_mode="feathered",
        edge_seal_px=0,
    )

    assert out is not None
    # composite_source = uploaded_processed_img.size (downscaled by
    # preflight to fit the 1536px envelope). NOT full-res.
    assert captured["orig_size"] == _uploaded_img.size
    # Adjusted margins are STRICTLY SMALLER than the user-requested
    # 700px on each side — preflight scaled them down proportionally.
    # We don't pin a specific value because that's preflight math
    # we'd duplicate fragilely; we just assert the scale-down happened.
    assert all(0 < m < 700 for m in captured["margins"]), (
        f"Expected adjusted margins < 700; got {captured['margins']}. "
        "Composite is no longer in provider-coordinate space."
    )
    # And composite_source + adj margins should match the downloaded
    # canvas (the geometry contract that makes alignment work).
    cs_w, cs_h = captured["orig_size"]
    adj_l, adj_r, adj_t, adj_b = captured["margins"]
    assert cs_w + adj_l + adj_r == 1304
    assert cs_h + adj_t + adj_b == 1532
    assert captured["mode"] == "feathered"


def test_fal_edge_seal_upload_only_uses_unsealed_composite_source(monkeypatch, tmp_path: Path):
    """Edge seal applies only to the upload copy. The composite source
    must be the unsealed `processed_img` — round 10 reverted to
    provider-coordinate compositing, so we assert `processed_img.size`
    here (NOT the full-res source). For a 320x240 source that doesn't
    trigger preflight scale-down, processed_img.size == source size.
    """
    gen = OutpaintGenerator(api_key="x")
    src_path = tmp_path / "input.png"
    Image.new("RGB", (320, 240), (25, 35, 45)).save(src_path)
    _source_img, _uploaded_img = _configure_fake_fal(
        monkeypatch,
        source_size=(320, 240),
        uploaded_size=(320, 240),  # no downscale needed at this size
        downloaded_size=(380, 280),  # = 320 + 30 + 30 x 240 + 20 + 20
    )

    captured = {}

    def fake_composite(
        output_path, orig, margin_left, margin_right,
        margin_top, margin_bottom, output_format, composite_mode,
    ):
        captured["orig_size"] = orig.size
        captured["sample_pixel"] = orig.getpixel((0, 0))
        return True

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
    # processed_img (or uploaded_processed_img) — at this size it's
    # the same as the source size since no preflight scale-down.
    assert captured["orig_size"] == (320, 240)
    # Pixel matches the unsealed source: composite was NOT given
    # the sealed upload copy.
    assert captured["sample_pixel"] == (25, 35, 45)


def test_fal_underflow_resizes_to_expected_provider_canvas(monkeypatch, tmp_path: Path):
    """PR #53 round 10 (REVERTED rounds 5..9): the always-composite
    contract is preserved, but the resize target is the PROVIDER
    expected canvas (composite_source + adj margins), NOT the full-
    res target. fal.ai routinely clamps 1-2% smaller than preflight;
    resizing to the small expected canvas keeps the matchTemplate
    +-15px alignment window valid. Rounds 5..9 incorrectly resized
    to a full-res target (3.4x upscale in the user's reproducer) and
    the composite silently misaligned.

    Underflow scenario: source 640x480 (no preflight scale needed
    since < 1536px envelope), requested margins 200/200/100/100,
    expected canvas 1040x680. Fake fal returns 1020x660 (a small
    underflow). The resize MUST target 1040x680 (the expected
    canvas), NOT some larger full-res-derived value.
    """
    gen = OutpaintGenerator(api_key="x")
    src_path = tmp_path / "input.png"
    Image.new("RGB", (640, 480), (90, 60, 40)).save(src_path)
    _source_img, _uploaded_img = _configure_fake_fal(
        monkeypatch,
        source_size=(640, 480),
        uploaded_size=(640, 480),  # no preflight downscale
        downloaded_size=(1020, 660),  # 1.5-3% underflow vs expected 1040x680
    )

    captured = {}

    def fake_composite(
        output_path, orig, margin_left, margin_right,
        margin_top, margin_bottom, output_format, composite_mode,
    ):
        captured["called"] = True
        captured["orig_size"] = orig.size
        captured["margins"] = (margin_left, margin_right, margin_top, margin_bottom)
        # Image on disk must already have been Lanczos-resized to the
        # PROVIDER expected canvas BEFORE composite is invoked.
        from PIL import Image as _Img
        with _Img.open(output_path) as dl:
            captured["on_disk_size"] = dl.size
        return True

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
    assert captured.get("called") is True
    # composite_source (640x480 — no preflight scale at this size).
    assert captured["orig_size"] == (640, 480)
    # Adjusted margins (== requested since no preflight scale).
    assert captured["margins"] == (200, 200, 100, 100)
    # On-disk resized to PROVIDER expected canvas (composite_source
    # + adj margins): 640+200+200 = 1040 x 480+100+100 = 680. NOT a
    # full-res target — the rounds-5..9 bug was resizing this to a
    # much larger canvas which broke alignment.
    assert captured["on_disk_size"] == (1040, 680)


def test_fal_dimension_read_failure_rejects_output(monkeypatch, tmp_path: Path):
    """PR fix/step0-composite-and-rppg-v2.5: a genuinely unreadable
    downloaded file is now treated as IO failure — return None,
    delete the unreadable file, set error_detail. Previously the code
    skipped composite but still returned the broken file path as
    "success", which corrupted downstream stages.
    """
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
        return True

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

    assert out is None
    assert called["composite"] == 0
    # Error_detail set so callers can surface the failure to the user.
    detail = gen.get_last_outpaint_error_detail()
    assert detail and "download_unreadable" in detail
    # The unreadable file is deleted (not left as a corruption hazard
    # for downstream consumers that might glob for it).
    if downloaded["path"]:
        import os as _os
        assert not _os.path.exists(downloaded["path"])
    # Error log surfaces the read failure.
    assert any(
        level == "error" and "Could not read downloaded outpaint output" in message
        for level, message in logs
    )


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


def _assert_no_network(monkeypatch):
    """Make every fal/BFL network primitive explode if called.

    black_fill must spend ZERO API credits, so any call into the upload /
    queue / poll / download primitives is a contract violation.
    """
    import fal_utils

    def boom(*_args, **_kwargs):  # pragma: no cover - only fires on a bug
        raise AssertionError("black_fill must not call any network primitive")

    monkeypatch.setattr(fal_utils, "upload_reference_image", boom)
    monkeypatch.setattr(fal_utils, "fal_queue_submit", boom)
    monkeypatch.setattr(fal_utils, "fal_queue_poll", boom)
    monkeypatch.setattr(fal_utils, "fal_download_file", boom)


def test_black_fill_no_api_black_margins_exact_center(monkeypatch, tmp_path: Path):
    """black_fill pastes the original onto a solid black canvas with no
    provider call. The center is the exact (preflight-sized) original and
    the expansion margins are pure black."""
    _assert_no_network(monkeypatch)
    gen = OutpaintGenerator(api_key="x", freeimage_key="y", bfl_api_key="z")

    src_path = tmp_path / "input.png"
    # Small enough that preflight applies no scale → margins pass through.
    _build_source_image(120, 90).save(src_path)
    out_path = tmp_path / "bf.png"

    margins = dict(expand_left=40, expand_right=40, expand_top=30, expand_bottom=30)
    out = gen.outpaint(
        image_path=str(src_path),
        output_folder=str(tmp_path),
        output_path=str(out_path),
        provider="fal",
        composite_mode="black_fill",
        output_format="png",
        **margins,
    )
    assert out == str(out_path)
    result = Image.open(out_path).convert("RGB")
    # No preflight scale at 120x90 → canvas is orig + margins exactly.
    assert result.size == (120 + 80, 90 + 60)  # 200 x 150

    # Center is the exact original (hard paste, no blend).
    center = result.crop((40, 30, 40 + 120, 30 + 90))
    assert center.tobytes() == _build_source_image(120, 90).tobytes()

    # All four margins are pure black.
    assert result.getpixel((0, 0)) == (0, 0, 0)
    assert result.getpixel((result.width - 1, 0)) == (0, 0, 0)
    assert result.getpixel((0, result.height - 1)) == (0, 0, 0)
    assert result.getpixel((result.width - 1, result.height - 1)) == (0, 0, 0)
    # A pixel just outside the center on each side is black.
    assert result.getpixel((39, 75)) == (0, 0, 0)   # left margin
    assert result.getpixel((160, 75)) == (0, 0, 0)  # right margin
    assert result.getpixel((100, 29)) == (0, 0, 0)  # top margin
    assert result.getpixel((100, 120)) == (0, 0, 0)  # bottom margin


def test_black_fill_works_with_bfl_provider_without_api(monkeypatch, tmp_path: Path):
    """Selecting provider='bfl' with black_fill still skips the network —
    the short-circuit fires before any provider dispatch."""
    _assert_no_network(monkeypatch)
    gen = OutpaintGenerator(api_key="x", freeimage_key="y", bfl_api_key="z")
    src_path = tmp_path / "in.png"
    _build_source_image(64, 48).save(src_path)
    out_path = tmp_path / "bf_bfl.png"

    out = gen.outpaint(
        image_path=str(src_path),
        output_folder=str(tmp_path),
        output_path=str(out_path),
        provider="bfl",
        composite_mode="black_fill",
        output_format="png",
        expand_left=20, expand_right=20, expand_top=16, expand_bottom=16,
    )
    assert out == str(out_path)
    result = Image.open(out_path).convert("RGB")
    assert result.size == (64 + 40, 48 + 32)
    assert result.getpixel((0, 0)) == (0, 0, 0)


def test_black_fill_direct_bfl_outpaint_stays_local(monkeypatch, tmp_path: Path):
    """Defensive: a direct _bfl_outpaint(black_fill) caller also stays API-free."""
    _assert_no_network(monkeypatch)
    gen = OutpaintGenerator(api_key="x", bfl_api_key="z")
    src_path = tmp_path / "in.png"
    _build_source_image(50, 50).save(src_path)
    out_path = tmp_path / "bf_direct.png"

    out = gen._bfl_outpaint(
        image_path=str(src_path),
        output_folder=str(tmp_path),
        expand_left=10, expand_right=10, expand_top=10, expand_bottom=10,
        prompt="",
        output_format="png",
        composite_mode="black_fill",
        output_path=str(out_path),
    )
    assert out == str(out_path)
    assert Image.open(out_path).convert("RGB").getpixel((0, 0)) == (0, 0, 0)


def test_black_fill_document_mode_plan_failure_returns_none(monkeypatch, tmp_path: Path):
    """If document-mode 3:4 planning raises, black_fill must FAIL rather than
    fall through with zero margins (which would silently produce a
    no-expansion canvas that looks like success)."""
    _assert_no_network(monkeypatch)
    gen = OutpaintGenerator(api_key="x")
    src_path = tmp_path / "in.png"
    _build_source_image(60, 80).save(src_path)
    out_path = tmp_path / "bf_doc.png"

    def boom_plan(*_args, **_kwargs):
        raise RuntimeError("simulated plan failure")

    monkeypatch.setattr("outpaint_generator.compute_centered_aspect_expand_plan", boom_plan)

    out = gen.outpaint(
        image_path=str(src_path),
        output_folder=str(tmp_path),
        output_path=str(out_path),
        provider="fal",
        composite_mode="black_fill",
        document_mode=True,
        output_format="png",
        # In document mode the pipeline passes empty margins (0); the plan is
        # what supplies them. If the plan fails we must NOT ship a 0-margin file.
        expand_left=0, expand_right=0, expand_top=0, expand_bottom=0,
    )
    assert out is None
    assert not out_path.exists(), "no zero-margin output should be written on plan failure"
    assert "black_fill_document_plan_failed" in gen.get_last_outpaint_error_detail()
