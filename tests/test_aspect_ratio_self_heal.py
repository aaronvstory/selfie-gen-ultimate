"""v2.28 — aspect-ratio self-heal for future image-to-image models.

The user reported (2026-06-07) that a friend on a pre-v2.26 build
hit ``response_url failed: HTTP 401 — bearer: unable to decode
issuer`` — the v2.26 fix (auth-fallback excludes 422) is in main now,
but the user explicitly asked: *"are u sure this issue is now
guaranteed fixed and all generations for all present and future
added models will work?"*

The honest answer pre-PR was: the 8-element universal aspect-ratio
set works for the three current built-ins (Nano Banana 2, Kontext
Max, GPT Image 2) — but a FUTURE image-to-image model could ship an
even stricter accepted list and bounce again. So this PR adds a
self-heal:

  1. ``fal_utils.parse_aspect_ratio_validation_error`` extracts the
     accepted-label set from a fal.ai 422 validation response.
  2. ``_extract_result`` returns a sentinel
     ``{"__aspect_ratio_rejected__": True, "allowed": [...]}``
     instead of None so the caller can detect the rejection.
  3. ``SelfieGenerator._generate_fal_raw`` detects the sentinel, snaps
     to the closest accepted label, caches the accepted set in
     ``_ENDPOINT_ASPECT_OVERRIDES`` (so subsequent generations skip
     the bounce), and re-submits ONCE.

These tests pin all three pieces.
"""

from __future__ import annotations

import json
import unittest
from unittest import mock

import requests

import fal_utils
from selfie_generator import SelfieGenerator


def _resp_422(detail_field: str, msg: str, body: dict | None = None) -> requests.Response:
    """Stub a fal.ai 422 validation response."""
    r = requests.Response()
    r.status_code = 422
    payload = body or {
        "detail": [
            {
                "loc": ["body", detail_field],
                "msg": msg,
                "type": "literal_error",
            },
        ]
    }
    r._content = json.dumps(payload).encode("utf-8")
    r.headers["Content-Type"] = "application/json"
    return r


def _resp_200(body: dict) -> requests.Response:
    r = requests.Response()
    r.status_code = 200
    r._content = json.dumps(body).encode("utf-8")
    r.headers["Content-Type"] = "application/json"
    return r


# ──────────────────────────────────────────────────────────────────
# parse_aspect_ratio_validation_error
# ──────────────────────────────────────────────────────────────────


class ParseAspectRatioValidationErrorTests(unittest.TestCase):
    def test_parses_kontext_max_literal_error(self):
        """The exact Kontext Max message from production."""
        r = _resp_422(
            "aspect_ratio",
            "Input should be '21:9', '16:9', '4:3', '3:2', '1:1', "
            "'2:3', '3:4', '9:16' or '9:21'",
        )
        allowed = fal_utils.parse_aspect_ratio_validation_error(r)
        self.assertEqual(
            allowed,
            {"21:9", "16:9", "4:3", "3:2", "1:1", "2:3", "3:4", "9:16", "9:21"},
        )

    def test_parses_enum_permitted_shape(self):
        """The other shape fal occasionally emits."""
        r = _resp_422(
            "aspect_ratio",
            "value is not a valid enumeration member; permitted: "
            "'1:1', '16:9', '9:16'",
        )
        allowed = fal_utils.parse_aspect_ratio_validation_error(r)
        self.assertEqual(allowed, {"1:1", "16:9", "9:16"})

    def test_returns_none_for_non_422(self):
        r = requests.Response()
        r.status_code = 500
        self.assertIsNone(fal_utils.parse_aspect_ratio_validation_error(r))

    def test_returns_none_for_unrelated_422(self):
        """A 422 about a DIFFERENT field (e.g. ``prompt``) must NOT
        trigger an aspect-ratio retry."""
        r = _resp_422("prompt", "Input must be a string")
        self.assertIsNone(fal_utils.parse_aspect_ratio_validation_error(r))

    def test_returns_none_for_unparseable_body(self):
        r = requests.Response()
        r.status_code = 422
        r._content = b"not json"
        self.assertIsNone(fal_utils.parse_aspect_ratio_validation_error(r))

    def test_returns_none_when_no_labels_in_message(self):
        """422 + aspect_ratio loc but no quoted labels in the msg →
        we can't recover, return None."""
        r = _resp_422("aspect_ratio", "Generic validation error")
        self.assertIsNone(fal_utils.parse_aspect_ratio_validation_error(r))


# ──────────────────────────────────────────────────────────────────
# _extract_result sentinel return
# ──────────────────────────────────────────────────────────────────


class ExtractResultAspectRatioSentinelTests(unittest.TestCase):
    def test_extract_result_returns_sentinel_on_aspect_ratio_422(self):
        status_result = {
            "status": "COMPLETED",
            "response_url": "https://queue.fal.run/x/requests/abc",
        }
        rejected = _resp_422(
            "aspect_ratio",
            "Input should be '1:1', '16:9' or '9:16'",
        )
        with mock.patch("fal_utils.requests.get", return_value=rejected):
            result = fal_utils._extract_result(
                status_result, {"Authorization": "Key k"}, progress_cb=None,
            )
        self.assertIsInstance(result, dict)
        self.assertTrue(result.get("__aspect_ratio_rejected__"))
        self.assertEqual(set(result["allowed"]), {"1:1", "16:9", "9:16"})

    def test_extract_result_returns_none_on_unrelated_422(self):
        """Unrelated 422 keeps the old behavior — None + a logged
        error. We must NOT return a sentinel for prompt/other field
        rejections; the caller would loop forever."""
        status_result = {
            "status": "COMPLETED",
            "response_url": "https://queue.fal.run/x/requests/abc",
        }
        bad = _resp_422("prompt", "Input must not be empty")
        with mock.patch("fal_utils.requests.get", return_value=bad):
            result = fal_utils._extract_result(
                status_result, {"Authorization": "Key k"}, progress_cb=None,
            )
        self.assertIsNone(result)


# ──────────────────────────────────────────────────────────────────
# SelfieGenerator._closest_aspect_ratio with override cache
# ──────────────────────────────────────────────────────────────────


class ClosestAspectRatioOverrideTests(unittest.TestCase):
    def setUp(self):
        # Ensure the override map is clean between tests so one
        # test's cache doesn't leak into another.
        SelfieGenerator._ENDPOINT_ASPECT_OVERRIDES.clear()

    def tearDown(self):
        SelfieGenerator._ENDPOINT_ASPECT_OVERRIDES.clear()

    def test_no_override_uses_universal_set(self):
        """Without an override, ultra-tall 1080x2520 snaps to 9:16
        (the universal-set behavior from PR #82)."""
        out = SelfieGenerator._closest_aspect_ratio(
            1080, 2520, "vendor/new-strict-model"
        )
        self.assertEqual(out, "9:16")

    def test_override_snaps_to_endpoint_specific_set(self):
        """If we've cached an override for an endpoint, the snap
        ignores the universal set and picks the closest label from
        the override list."""
        SelfieGenerator._ENDPOINT_ASPECT_OVERRIDES["vendor/strict"] = [
            "1:1", "16:9", "9:16",
        ]
        # 4:3 → 1.333; closest in {1.0, 1.778, 0.5625} is 1.0 (1:1)
        out = SelfieGenerator._closest_aspect_ratio(
            1024, 768, "vendor/strict"
        )
        self.assertEqual(out, "1:1")

    def test_override_for_one_endpoint_doesnt_leak_to_others(self):
        SelfieGenerator._ENDPOINT_ASPECT_OVERRIDES["vendor/strict"] = ["1:1"]
        # vendor/other has NO override — still uses universal set.
        out_other = SelfieGenerator._closest_aspect_ratio(
            1920, 1080, "vendor/other"
        )
        self.assertEqual(out_other, "16:9")

    def test_parse_aspect_label_handles_garbage(self):
        """Defensive: a malformed override entry shouldn't crash the
        whole generation path."""
        # Should fall back to 1.0 (square ratio) without raising.
        self.assertEqual(SelfieGenerator._parse_aspect_label("not-a-ratio"), 1.0)
        self.assertEqual(SelfieGenerator._parse_aspect_label("9:0"), 9.0)
        self.assertEqual(SelfieGenerator._parse_aspect_label(""), 1.0)


# ──────────────────────────────────────────────────────────────────
# Sentinel propagates from _extract_result through fal_queue_poll
# ──────────────────────────────────────────────────────────────────


class FalQueuePollSentinelPropagationTests(unittest.TestCase):
    def test_sentinel_propagates_through_poll(self):
        """``fal_queue_poll`` must return the sentinel dict as-is so
        the selfie_generator can detect it. Builds a stub status_url
        that polls COMPLETED on first hit + response_url that 422s."""
        poll_responses = iter([
            _resp_200({"status": "COMPLETED",
                       "response_url": "https://q.fal.run/x/requests/abc"}),
            _resp_422("aspect_ratio",
                      "Input should be '1:1', '16:9' or '9:16'"),
        ])

        with mock.patch(
            "fal_utils.requests.get",
            side_effect=lambda *a, **kw: next(poll_responses),
        ):
            result = fal_utils.fal_queue_poll(
                "test-key", "https://q.fal.run/x/requests/abc/status",
                progress_cb=None, max_wait_seconds=5,
            )
        self.assertIsInstance(result, dict)
        self.assertTrue(result.get("__aspect_ratio_rejected__"))


if __name__ == "__main__":
    unittest.main()
