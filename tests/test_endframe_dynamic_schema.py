"""Payload smoke tests for the dynamic per-model API schema +
end-frame lock + cfg_scale (PR #2).

Verifies the EXACT request body create_kling_generation builds for each
roster model from model_metadata.get_model_capabilities — the single
source of truth the GUI + dispatcher share. The spec's verification
matrix:

  * v2.5-turbo/standard : image_url + negative_prompt + cfg_scale,
                          NO tail/end frame
  * v2.5-turbo/pro      : + tail_image_url == image_url (lock on)
  * o3/standard         : image_url + end_image_url, NO negative_prompt,
                          NO cfg_scale
  * seedance-2.0        : image_url + end_image_url, NO neg, NO cfg
                          (endpoint keeps the bytedance/ prefix — no
                          "fal-ai/" — must NOT be "corrected")

No network, no real upload: freeimage upload + duplicate check are
mocked, schema validation is identity (so we see the true cap-driven
payload), and requests.post is intercepted to capture the body and
abort before any HTTP.
"""

import sys
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from kling_generator_falai import FalAIKlingGenerator  # noqa: E402


def _capture_payload(endpoint, *, lock_end_frame=True, cfg_scale=0.7,
                      negative_prompt="no blur"):
    """Return the request body create_kling_generation would POST.

    The dispatcher's submit loop catches exceptions (retry logic), so a
    raising mock would be swallowed. Instead the mock captures the body
    and returns a 4xx-status response object, which makes the submit
    path bail cleanly without network. We only care about the payload
    that was assembled, not the (mocked) HTTP outcome.
    """
    gen = FalAIKlingGenerator(api_key="test-key", model_endpoint=endpoint)
    captured = {}

    def _fake_post(url, headers=None, json=None, timeout=None):
        captured["payload"] = json
        resp = mock.MagicMock()
        resp.status_code = 400  # non-retry client error -> clean bail
        resp.text = "captured"
        resp.json.return_value = {"detail": "captured"}
        return resp

    with mock.patch.object(
        gen, "upload_to_freeimage", return_value="https://img.example/x.png"
    ), mock.patch.object(
        gen, "check_duplicate_exists", return_value=False
    ), mock.patch.object(
        gen.schema_manager, "validate_parameters",
        side_effect=lambda _ep, params: dict(params),  # identity
    ), mock.patch(
        "kling_generator_falai.requests.post", side_effect=_fake_post
    ), mock.patch(
        # The submit loop sleeps between retries on a 4xx; we don't care
        # about the (mocked) HTTP outcome, only the assembled payload —
        # no-op the sleeps so the suite stays fast and CI-deterministic.
        "kling_generator_falai.time.sleep", lambda *_a, **_k: None
    ):
        gen.create_kling_generation(
            character_image_path="C:/nope/img.png",
            output_folder="C:/nope/out",
            custom_prompt="a person",
            negative_prompt=negative_prompt,
            duration=10,
            cfg_scale=cfg_scale,
            lock_end_frame=lock_end_frame,
            skip_duplicate_check=True,
        )
    assert "payload" in captured, "requests.post was never reached"
    return captured["payload"]


def test_v25_turbo_standard_payload():
    """Standard: image_url + negative_prompt + cfg_scale; NO end frame
    (end_image_param is None for this model)."""
    p = _capture_payload("fal-ai/kling-video/v2.5-turbo/standard/image-to-video")
    assert p["image_url"] == "https://img.example/x.png"
    assert p.get("negative_prompt") == "no blur"
    assert p.get("cfg_scale") == 0.7
    assert "tail_image_url" not in p
    assert "end_image_url" not in p
    assert "start_image_url" not in p  # this model uses image_url


def test_v25_turbo_pro_locks_tail_to_start():
    """Pro: end param is tail_image_url; with lock_end_frame it must
    equal the start image url (mechanical return-to-pose)."""
    p = _capture_payload("fal-ai/kling-video/v2.5-turbo/pro/image-to-video")
    assert p["image_url"] == "https://img.example/x.png"
    assert p.get("tail_image_url") == p["image_url"]
    assert p.get("negative_prompt") == "no blur"
    assert p.get("cfg_scale") == 0.7
    assert "end_image_url" not in p


def test_v25_turbo_pro_no_lock_omits_tail():
    """Pro with lock_end_frame=False and no explicit end url -> the
    tail param must NOT be set."""
    p = _capture_payload(
        "fal-ai/kling-video/v2.5-turbo/pro/image-to-video",
        lock_end_frame=False,
    )
    assert "tail_image_url" not in p


def test_v3_pro_uses_start_image_url_and_end_image_url():
    """Kling v3 renamed the start param to start_image_url and uses
    end_image_url; it keeps negative_prompt + cfg_scale (fetch-verified)."""
    p = _capture_payload("fal-ai/kling-video/v3/pro/image-to-video")
    assert p.get("start_image_url") == "https://img.example/x.png"
    assert "image_url" not in p  # v3 does NOT use image_url
    assert p.get("end_image_url") == p["start_image_url"]
    assert p.get("negative_prompt") == "no blur"
    assert p.get("cfg_scale") == 0.7


def test_o3_standard_drops_negative_and_cfg():
    """O3 (Apr-2026 migration) removed negative_prompt + cfg_scale and
    uses image_url + end_image_url. The dispatcher must NOT send the
    dropped params even though the caller passed them."""
    p = _capture_payload("fal-ai/kling-video/o3/standard/image-to-video")
    assert p["image_url"] == "https://img.example/x.png"
    assert p.get("end_image_url") == p["image_url"]
    assert "negative_prompt" not in p
    assert "cfg_scale" not in p
    assert "start_image_url" not in p
    assert "tail_image_url" not in p


def test_seedance_endpoint_prefix_and_caps():
    """Seedance keeps the bytedance/ prefix (NOT fal-ai/) and has no
    negative_prompt / cfg_scale; end frame is end_image_url."""
    p = _capture_payload("bytedance/seedance-2.0/image-to-video")
    assert p["image_url"] == "https://img.example/x.png"
    assert p.get("end_image_url") == p["image_url"]
    assert "negative_prompt" not in p
    assert "cfg_scale" not in p


def test_get_model_capabilities_is_single_source_and_safe():
    """The capability helper is the single source the GUI + dispatcher
    share. Verified models return their real flags; unknown / legacy
    models degrade to the conservative defaults (no end-frame, no cfg,
    no neg) so an unflagged model can never KeyError or send a param
    the API will reject."""
    from model_metadata import get_model_capabilities

    pro = get_model_capabilities("fal-ai/kling-video/v2.5-turbo/pro/image-to-video")
    assert pro["start_image_param"] == "image_url"
    assert pro["end_image_param"] == "tail_image_url"
    assert pro["supports_negative_prompt"] is True
    assert pro["supports_cfg_scale"] is True

    o3 = get_model_capabilities("fal-ai/kling-video/o3/standard/image-to-video")
    assert o3["end_image_param"] == "end_image_url"
    assert o3["supports_negative_prompt"] is False
    assert o3["supports_cfg_scale"] is False

    # Unknown / unflagged -> conservative defaults, fully populated.
    unknown = get_model_capabilities("totally/made-up/endpoint")
    assert unknown == {
        "start_image_param": "image_url",
        "end_image_param": None,
        "supports_negative_prompt": False,
        "supports_cfg_scale": False,
    }
