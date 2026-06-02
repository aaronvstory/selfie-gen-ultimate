"""v2.17: lock the verified Kling pricing + capability metadata in models.json.

The model tooltips show pricing from the LIVE fal.ai API, but that has been
observed returning wrong/stale numbers. models.json now carries a
``pricing_fallback`` per priced Kling model (verified per-second, audio off)
that the tooltip uses when the live quote is unavailable. These tests pin the
verified numbers + the capability fields (end-frame param, negative_prompt /
cfg_scale support) so a future edit can't silently drift them.

Verified reference (audio off, the cheaper tier), 2026-06-03:
  2.5 Turbo Standard $0.042/s | 2.5 Turbo Pro $0.07/s
  3.0 (V3) Standard  $0.084/s | 3.0 (V3) Pro   $0.112/s | O1 $0.112/s
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
_MODELS = json.loads((REPO_ROOT / "models.json").read_text(encoding="utf-8"))
_BY_EP = {m["endpoint"]: m for m in _MODELS["models"]}

# endpoint -> verified per-second price (audio off)
_EXPECTED_PRICE = {
    "fal-ai/kling-video/v2.5-turbo/standard/image-to-video": 0.042,
    "fal-ai/kling-video/v2.5-turbo/pro/image-to-video": 0.07,
    "fal-ai/kling-video/v3/standard/image-to-video": 0.084,
    "fal-ai/kling-video/v3/pro/image-to-video": 0.112,
    "fal-ai/kling-video/o1/image-to-video": 0.112,
}


@pytest.mark.parametrize("endpoint,price", list(_EXPECTED_PRICE.items()))
def test_pricing_fallback_matches_verified(endpoint, price):
    m = _BY_EP[endpoint]
    fb = m.get("pricing_fallback")
    assert fb, f"{endpoint}: missing pricing_fallback"
    assert fb.get("unit") == "second", f"{endpoint}: unit must be 'second'"
    assert abs(fb.get("unit_price", 0) - price) < 1e-9, (
        f"{endpoint}: pricing_fallback {fb.get('unit_price')} != verified {price}"
    )


def test_v3_keeps_negative_prompt_and_cfg():
    """The decisive 3.0 fact: V3 (Standard + Pro) KEEPS negative_prompt + cfg_scale
    (unlike O3, which drops both). If this flips, the tooltip + handoff are wrong."""
    for ep in (
        "fal-ai/kling-video/v3/standard/image-to-video",
        "fal-ai/kling-video/v3/pro/image-to-video",
    ):
        m = _BY_EP[ep]
        assert m["supports_negative_prompt"] is True, f"{ep} must keep negative_prompt"
        assert m["supports_cfg_scale"] is True, f"{ep} must keep cfg_scale"


def test_end_frame_params_correct():
    """2.5 Pro uses tail_image_url; V3 uses end_image_url; 2.5 Standard has none."""
    assert _BY_EP["fal-ai/kling-video/v2.5-turbo/pro/image-to-video"]["end_image_param"] == "tail_image_url"
    assert _BY_EP["fal-ai/kling-video/v3/standard/image-to-video"]["end_image_param"] == "end_image_url"
    assert _BY_EP["fal-ai/kling-video/v2.5-turbo/standard/image-to-video"]["end_image_param"] is None


def test_user_notes_carry_verified_prices():
    """The recommended models' tooltips must state the verified 10s price so a
    user sees the right number even before the live API responds."""
    un = _MODELS["user_notes"]
    assert "$0.70 per 10s" in un["fal-ai/kling-video/v2.5-turbo/pro/image-to-video"]
    assert "$0.84 per 10s" in un["fal-ai/kling-video/v3/standard/image-to-video"]


def _import_config_panel():
    import sys

    if str(REPO_ROOT) not in sys.path:
        sys.path.insert(0, str(REPO_ROOT))
    # Skip only on a genuine import/Tk-display failure (catch ImportError +
    # tkinter.TclError for headless CI — code-review). A broad `except
    # Exception` would mask a real tooltip-logic regression as a "skip".
    try:
        import tkinter as _tk
        from kling_gui import config_panel as cp
    except ImportError:
        pytest.skip("config_panel import unavailable (no Tk)")
        return None
    except _tk.TclError:  # type: ignore[name-defined]
        pytest.skip("no Tk display (headless CI)")
        return None
    return cp


def _stub_with(model):
    class _Stub:
        models = [model]

        class _Combo:
            @staticmethod
            def current():
                return 0
        model_combo = _Combo()
    return _Stub()


def test_tooltip_uses_pricing_fallback_when_live_missing():
    """With no live pricing_info, the verified fallback price is rendered +
    tagged as a verified reference."""
    cp = _import_config_panel()
    notes = cp.ConfigPanel._get_current_model_notes(_stub_with({
        "endpoint": "fal-ai/kling-video/v2.5-turbo/pro/image-to-video",
        "name": "Kling 2.5 Turbo Pro",
        "pricing_fallback": {"unit": "second", "unit_price": 0.07},
        "user_notes": "x",
    }))
    assert "$0.70/10s" in notes or "$0.70" in notes
    assert "verified reference" in notes.lower()


def test_tooltip_prefers_fallback_over_stale_live_price():
    """Codex P2: when a model has a verified pricing_fallback, the tooltip must
    use IT even if a (stale/wrong) nonzero live pricing_info is present."""
    cp = _import_config_panel()
    notes = cp.ConfigPanel._get_current_model_notes(_stub_with({
        "endpoint": "fal-ai/kling-video/v2.5-turbo/pro/image-to-video",
        "name": "Kling 2.5 Turbo Pro",
        "pricing_info": {"unit": "second", "unit_price": 0.99},   # stale/wrong live quote
        "pricing_fallback": {"unit": "second", "unit_price": 0.07},
        "user_notes": "x",
    }))
    assert "$0.70" in notes, "must prefer the verified $0.07/s fallback"
    assert "$0.99" not in notes and "$9.90" not in notes, "must NOT use the stale live price"


def test_tooltip_survives_non_dict_pricing_fields():
    """Gemini/Codex: pricing_info / pricing_fallback that aren't dicts (list,
    str, null from a bad API response or hand-edited JSON) must not crash the
    tooltip — they're coerced to {}."""
    cp = _import_config_panel()
    for bad in ([], "oops", None, 42):
        notes = cp.ConfigPanel._get_current_model_notes(_stub_with({
            "endpoint": "x", "name": "X",
            "pricing_info": bad, "pricing_fallback": bad,
            "user_notes": "x",
        }))
        assert isinstance(notes, str)  # no crash
