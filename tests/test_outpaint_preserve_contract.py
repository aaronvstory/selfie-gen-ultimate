"""Regression tests for the PR fix/step0-composite-and-rppg-v2.5
preserve-seamless always-on contract.

These complement tests/test_outpaint_composite_modes.py:
- `test_fal_underflow_resizes_to_expected_provider_canvas` covers the
  always-composite path on slight underflow (provider-coord resize).
- `test_fal_dimension_read_failure_rejects_output` covers the strict-reject
  path on unreadable downloads.

The tests here cover:
- `_composite_onto_result` returns the documented bool.
- Preserve modes that hit the "Original doesn't fit" guard reject the
  output (no silent raw return).
- `composite_mode="none"` short-circuits and accepts raw provider output.

PR #53 round 10 REVERTED the rounds-5..9 full-res-original composite
path that broke alignment in user manual smoke. Tests that asserted
the full-res behavior (test_full_res_reopen_applies_exif_transpose,
test_fal_realistic_underflow_at_user_bug_ratio, test_source_reopen_
failure_rejects_output) have been removed because the code paths
they exercised no longer exist. The provider-coord behavior they
implicitly preserve is now asserted in
test_outpaint_composite_modes.py.
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
        # Match provider expected canvas exactly (no resize needed) so
        # the test isolates the composite-failure path.
        downloaded_size=(320 + 60 + 60, 240 + 40 + 40),
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


def test_composite_mode_none_short_circuits_after_readability_check(
    monkeypatch, tmp_path: Path,
):
    """PR #53 round 9 — Codex P2 + round 10 revert: ``composite_mode=
    "none"`` returns raw provider output without invoking any
    composite logic. Under round-10's provider-coord revert, there's
    no longer a source reopen to short-circuit against, but the
    early-return still matters: it skips the resize-to-expected
    branch and the composite call entirely.

    Confirm:
    - composite_mode="none" returns the downloaded path
    - _composite_onto_result is NOT called
    - the file on disk is the raw provider output (no resize applied)
    """
    gen = OutpaintGenerator(api_key="x")
    src_path = tmp_path / "input.png"
    Image.new("RGB", (320, 240), (5, 5, 5)).save(src_path)
    _stub_fal_for_outpaint(
        monkeypatch,
        source_size=(320, 240),
        uploaded_size=(320, 240),
        # Deliberately MISMATCH the provider expected canvas so the
        # resize branch WOULD fire if composite_mode were anything
        # other than "none".
        downloaded_size=(123, 456),
    )

    composite_calls = {"n": 0}

    def fake_composite(*_args, **_kwargs):
        composite_calls["n"] += 1
        return True

    monkeypatch.setattr(gen, "_composite_onto_result", fake_composite)

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
    assert _os.path.exists(out)
    # The raw provider output (mismatched size) is preserved exactly —
    # NOT resized to expected canvas — because none-mode early-returns
    # before the resize branch.
    with Image.open(out) as dl:
        assert dl.size == (123, 456)
    # And the composite was NEVER called.
    assert composite_calls["n"] == 0
