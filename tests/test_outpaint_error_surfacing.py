"""Outpaint / fal error surfacing + retry-once regression coverage.

A real batch case failed with only::

    Outpaint failed or timed out (reason=fal_failed_or_timed_out)

while the actual cause — a fal HTTP 422 carrying
``image_url: Failed to generate outpainted image: ...`` — was truncated
mid-string and then discarded. These tests lock the fixes:

  * the full validation ``msg`` survives (no mid-repr truncation),
  * retryability is classified correctly,
  * a transient provider failure triggers exactly one resubmit,
  * a non-retryable failure surfaces the real detail and does NOT resubmit.
"""
from __future__ import annotations

import json

import fal_utils
from fal_utils import (
    _classify_http_retryable,
    _extract_http_error_detail,
    _format_validation_detail_list,
)


class _FakeResp:
    def __init__(self, payload, status_code=422, text=""):
        self._payload = payload
        self.status_code = status_code
        self.text = text

    def json(self):
        if self._payload is None:
            raise ValueError("no json")
        return self._payload


def test_validation_list_detail_is_extracted_in_full():
    long_msg = "Failed to generate outpainted image: " + ("x" * 400)
    resp = _FakeResp(
        {"detail": [{"loc": ["body", "image_url"], "msg": long_msg, "type": "value_error"}]}
    )
    out = _extract_http_error_detail(resp)
    # The real human message survives, prefixed by the offending field, NOT a
    # raw dict repr, and NOT truncated at the old 500-char limit.
    assert out.startswith("image_url: Failed to generate outpainted image:")
    assert long_msg in out
    assert "{'loc'" not in out  # not a dict dump


def test_multiple_validation_entries_joined():
    resp = _FakeResp(
        {"detail": [
            {"loc": ["body", "image_url"], "msg": "bad url"},
            {"loc": ["body", "expand_top"], "msg": "too large"},
        ]}
    )
    out = _extract_http_error_detail(resp)
    assert "image_url: bad url" in out
    assert "expand_top: too large" in out


def test_content_policy_violation_still_special_cased():
    resp = _FakeResp(
        {"detail": [{"loc": ["body", "prompt"], "msg": "blocked", "type": "content_policy_violation"}]}
    )
    out = _extract_http_error_detail(resp)
    assert out.startswith(fal_utils.CONTENT_POLICY_PREFIX)


def test_format_validation_detail_list_drops_scope_token():
    assert _format_validation_detail_list(
        [{"loc": ["body", "image_url"], "msg": "x"}]
    ) == "image_url: x"


def test_retryable_classification():
    # 5xx -> retryable
    assert _classify_http_retryable(500, "boom") is True
    assert _classify_http_retryable(503, "busy") is True
    # generic 422 generation hiccup -> retryable
    assert _classify_http_retryable(422, "image_url: Failed to generate outpainted image") is True
    # content-policy 422 -> NOT retryable (deterministic)
    assert _classify_http_retryable(
        422, fal_utils.CONTENT_POLICY_PREFIX + " in `prompt`: ..."
    ) is False
    # other 4xx -> not retryable
    assert _classify_http_retryable(404, "missing") is False


# --- generator-level: retry-once + real detail ----------------------------

def _make_generator():
    from outpaint_generator import OutpaintGenerator

    gen = OutpaintGenerator.__new__(OutpaintGenerator)
    gen.api_key = "k"
    gen._freeimage_key = ""
    gen._bfl_api_key = ""
    gen._progress_callback = None
    gen._last_outpaint_error_detail = ""
    return gen


def test_retryable_failure_resubmits_once(monkeypatch):
    """A retryable poll failure triggers exactly one resubmit, then succeeds."""
    import outpaint_generator as ogmod

    submits = {"n": 0}

    def _fake_submit(api_key, endpoint, payload, cb):
        submits["n"] += 1
        return {"status_url": "http://x/status", "request_id": "req123"}

    def _fake_poll(api_key, status_url, cb, **kwargs):
        sink = kwargs.get("error_sink")
        if submits["n"] == 1:
            if sink is not None:
                sink.update(detail="image_url: Failed to generate outpainted image",
                            status_code=422, kind="http_error", retryable=True)
            return None
        return {"images": [{"url": "http://x/out.png"}]}

    monkeypatch.setattr(fal_utils, "fal_queue_submit", _fake_submit)
    monkeypatch.setattr(fal_utils, "fal_queue_poll", _fake_poll)

    final = ogmod._poll_outpaint_with_retry(  # type: ignore[attr-defined]
        _make_generator(),
        endpoint="fal-ai/image-apps-v2/outpaint",
        payload={"image_url": "http://x/in.png"},
        timeout_seconds=10,
        cancel_event=None,
        adj=(1, 2, 3, 4),
    )
    assert submits["n"] == 2
    assert isinstance(final, dict) and final.get("images")


def test_non_retryable_failure_surfaces_detail_no_resubmit(monkeypatch):
    import outpaint_generator as ogmod

    submits = {"n": 0}

    def _fake_submit(api_key, endpoint, payload, cb):
        submits["n"] += 1
        return {"status_url": "http://x/status", "request_id": "req123"}

    def _fake_poll(api_key, status_url, cb, **kwargs):
        sink = kwargs.get("error_sink")
        if sink is not None:
            sink.update(detail=fal_utils.CONTENT_POLICY_PREFIX + " in `prompt`: blocked",
                        status_code=422, kind="http_error", retryable=False)
        return None

    monkeypatch.setattr(fal_utils, "fal_queue_submit", _fake_submit)
    monkeypatch.setattr(fal_utils, "fal_queue_poll", _fake_poll)

    gen = _make_generator()
    final = ogmod._poll_outpaint_with_retry(  # type: ignore[attr-defined]
        gen,
        endpoint="fal-ai/image-apps-v2/outpaint",
        payload={"image_url": "http://x/in.png"},
        timeout_seconds=10,
        cancel_event=None,
        adj=(1, 2, 3, 4),
    )
    assert final is None
    assert submits["n"] == 1  # no resubmit on a deterministic failure
    assert fal_utils.CONTENT_POLICY_PREFIX in gen.get_last_outpaint_error_detail()
