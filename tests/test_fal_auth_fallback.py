"""v2.26: auth-fallback contract — 401/403 trigger the Bearer retry, 422 does NOT.

Root cause regression: PR #81 v2.25 routed Kontext Max selfie requests
to the FLUX queue, which rejected `aspect_ratio: "4:5"` with a 422
validation error. The pre-v2.26 `_get_with_auth_fallback` treated 422 as
an auth issue (per the original "some endpoints reject Key" workaround)
and retried with Bearer auth — the Bearer attempt then returned 401 with
the misleading body `{"detail": "bearer: unable to decode issuer"}`,
which is what the user saw.

The real fix: limit the auth-fallback to bona-fide auth failures
(401 / 403) so a 422 validation error passes through with the actual
validation message intact.
"""

from __future__ import annotations

import json
import unittest
from unittest import mock

import requests

import fal_utils


def _resp(status_code: int, body: dict | None = None) -> requests.Response:
    """Build a stub Response with the requested status + JSON body."""
    r = requests.Response()
    r.status_code = status_code
    body = body or {}
    r._content = json.dumps(body).encode("utf-8")
    r.headers["Content-Type"] = "application/json"
    return r


class AuthFallbackStatusCodeTests(unittest.TestCase):
    """The CRITICAL contract: _AUTH_FALLBACK_STATUS lists only 401, 403.

    Listing 422 (the v2.24/v2.25 behaviour) hid Kontext Max's
    validation errors behind a misleading 'bearer: unable to decode
    issuer' message.
    """

    def test_auth_fallback_set_excludes_422(self):
        self.assertNotIn(422, fal_utils._AUTH_FALLBACK_STATUS)

    def test_auth_fallback_set_includes_401_and_403(self):
        self.assertIn(401, fal_utils._AUTH_FALLBACK_STATUS)
        self.assertIn(403, fal_utils._AUTH_FALLBACK_STATUS)


class GetAuthFallbackBehaviourTests(unittest.TestCase):
    """GET: 401/403 retry with Bearer; 422 passes through; 200 no retry."""

    def test_get_401_retries_with_bearer(self):
        calls: list[str] = []

        def fake_get(url, headers, timeout):
            calls.append(headers.get("Authorization", ""))
            if calls[-1].startswith("Key "):
                return _resp(401, {"detail": "no"})
            return _resp(200, {"ok": True})

        with mock.patch("fal_utils.requests.get", side_effect=fake_get):
            r = fal_utils._get_with_auth_fallback(
                "https://queue.fal.run/x", {"Authorization": "Key abc"}
            )
        self.assertEqual(r.status_code, 200)
        self.assertEqual(
            calls,
            ["Key abc", "Bearer abc"],
            "first attempt must be Key, second Bearer",
        )

    def test_get_422_does_NOT_retry(self):
        """The CRITICAL regression. 422 is validation, not auth — passing
        through with the original error message lets the caller surface
        what's actually wrong (e.g. unsupported aspect_ratio).
        """
        calls: list[str] = []

        def fake_get(url, headers, timeout):
            calls.append(headers.get("Authorization", ""))
            return _resp(422, {"detail": [{"msg": "aspect_ratio invalid"}]})

        with mock.patch("fal_utils.requests.get", side_effect=fake_get):
            r = fal_utils._get_with_auth_fallback(
                "https://queue.fal.run/x", {"Authorization": "Key abc"}
            )
        self.assertEqual(r.status_code, 422)
        self.assertEqual(
            calls, ["Key abc"],
            "422 must NOT trigger a Bearer retry — it's validation, "
            "not auth. Retrying masks the real error.",
        )
        # And the body must still carry the validation detail.
        self.assertIn("aspect_ratio invalid", r.text)

    def test_get_200_returns_immediately(self):
        calls: list[str] = []

        def fake_get(url, headers, timeout):
            calls.append(headers.get("Authorization", ""))
            return _resp(200, {"ok": True})

        with mock.patch("fal_utils.requests.get", side_effect=fake_get):
            r = fal_utils._get_with_auth_fallback(
                "https://queue.fal.run/x", {"Authorization": "Key abc"}
            )
        self.assertEqual(r.status_code, 200)
        self.assertEqual(calls, ["Key abc"])


class PostAuthFallbackBehaviourTests(unittest.TestCase):
    """POST: same contract as GET — 401/403 retry, 422 pass through."""

    def test_post_401_retries_with_bearer(self):
        calls: list[str] = []

        def fake_post(url, headers, json, timeout):
            calls.append(headers.get("Authorization", ""))
            if calls[-1].startswith("Key "):
                return _resp(401, {"detail": "no"})
            return _resp(200, {"ok": True})

        with mock.patch("fal_utils.requests.post", side_effect=fake_post):
            r = fal_utils._post_with_auth_fallback(
                "https://queue.fal.run/x", {"Authorization": "Key abc"}, {}
            )
        self.assertEqual(r.status_code, 200)
        self.assertEqual(calls, ["Key abc", "Bearer abc"])

    def test_post_422_does_NOT_retry(self):
        calls: list[str] = []

        def fake_post(url, headers, json, timeout):
            calls.append(headers.get("Authorization", ""))
            return _resp(422, {"detail": "validation"})

        with mock.patch("fal_utils.requests.post", side_effect=fake_post):
            r = fal_utils._post_with_auth_fallback(
                "https://queue.fal.run/x", {"Authorization": "Key abc"}, {}
            )
        self.assertEqual(r.status_code, 422)
        self.assertEqual(calls, ["Key abc"])


class ExtractResultSurfacesValidationErrorTests(unittest.TestCase):
    """v2.26 PR #82 round 1, subagent H2: the actual production failure
    mode that motivated this PR was `_extract_result` → response_url →
    422. The unit tests above verify the helper behaviour but not the
    full chain. This test asserts that when `_extract_result` receives
    a status_result with a `response_url` that returns 422, the user
    sees the VALIDATION DETAIL (not the misleading bearer-decode
    error).
    """

    def test_extract_result_surfaces_422_validation_detail(self):
        """422 from the response_url must reach the progress callback
        intact — not get masked by a Bearer retry that returns 'bearer:
        unable to decode issuer'.
        """
        validation_body = {
            "detail": [
                {
                    "type": "literal_error",
                    "loc": ["body", "aspect_ratio"],
                    "msg": (
                        "Input should be '21:9', '16:9', '4:3', '3:2', "
                        "'1:1', '2:3', '3:4', '9:16' or '9:21'"
                    ),
                    "input": "4:5",
                },
            ]
        }
        status_result = {
            "status": "COMPLETED",
            "response_url": (
                "https://queue.fal.run/fal-ai/flux-pro/requests/test-id"
            ),
        }
        status_headers = {"Authorization": "Key abc"}

        # Capture progress callback messages so we can assert on the
        # human-readable text.
        messages: list[tuple[str, str]] = []

        def progress_cb(msg: str, level: str) -> None:
            messages.append((msg, level))

        calls: list[str] = []

        def fake_get(url, headers, timeout):
            calls.append(headers.get("Authorization", ""))
            return _resp(422, validation_body)

        with mock.patch("fal_utils.requests.get", side_effect=fake_get):
            result = fal_utils._extract_result(
                status_result, status_headers, progress_cb
            )

        # Must NOT retry with Bearer (the v2.26 fix).
        self.assertEqual(
            calls, ["Key abc"],
            "422 must not trigger a Bearer retry; the v2.26 root cause",
        )
        # Result is None (no image; the request failed).
        self.assertIsNone(result)
        # The validation detail must appear in at least one error-level
        # progress message — that's what the user reads in the GUI.
        error_msgs = [m for m, lvl in messages if lvl == "error"]
        self.assertTrue(error_msgs, "expected at least one error message")
        joined = " | ".join(error_msgs)
        self.assertIn(
            "422", joined,
            f"422 status code must appear in user-facing error; got: {joined}",
        )
        # And the bearer-decode error MUST NOT appear (that's the
        # masking we fixed).
        self.assertNotIn(
            "bearer: unable to decode issuer", joined,
            f"misleading bearer error leaked; got: {joined}",
        )


class AuthValueTypeGuardTests(unittest.TestCase):
    """Gemini PR #82 MED-1 (4 inline comments across fal_utils + dist
    mirrors): a caller passing ``headers={"Authorization": None}`` (or
    any non-string sentinel) used to AttributeError on the
    ``auth_value.startswith("Key ")`` check. The guard wraps the
    comparison in ``isinstance(auth_value, str)`` so a non-string
    Authorization header passes through with the original response,
    not a crash."""

    def test_get_handles_non_string_authorization(self):
        def fake_get(url, headers, timeout):
            return _resp(401, {"detail": "no"})

        with mock.patch("fal_utils.requests.get", side_effect=fake_get):
            # Caller hands a None Authorization — must NOT crash, must
            # NOT retry (no "Key " prefix to swap), must return the 401.
            r = fal_utils._get_with_auth_fallback(
                "https://queue.fal.run/x", {"Authorization": None}
            )
        self.assertEqual(r.status_code, 401)

    def test_post_handles_non_string_authorization(self):
        def fake_post(url, headers, json, timeout):
            return _resp(401, {"detail": "no"})

        with mock.patch("fal_utils.requests.post", side_effect=fake_post):
            r = fal_utils._post_with_auth_fallback(
                "https://queue.fal.run/x", {"Authorization": None}, {}
            )
        self.assertEqual(r.status_code, 401)

    def test_get_handles_missing_authorization(self):
        def fake_get(url, headers, timeout):
            return _resp(401, {"detail": "no"})

        with mock.patch("fal_utils.requests.get", side_effect=fake_get):
            # No Authorization header at all → headers.get returns "" →
            # starts-with check is False → no retry. Belt+suspenders
            # case to ensure the existing "" default still works.
            r = fal_utils._get_with_auth_fallback(
                "https://queue.fal.run/x", {}
            )
        self.assertEqual(r.status_code, 401)


if __name__ == "__main__":
    unittest.main()
