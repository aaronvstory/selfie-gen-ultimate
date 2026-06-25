"""
Shared model metadata for video generation models.

The model list is loaded from models.json (next to this file) so it can be
updated without touching source code.  Supports two formats:
  - New (endpoint-only): models list contains endpoint strings
  - Legacy (dict): models list contains dicts with name, endpoint, etc.
Falls back to a hardcoded list only when models.json is missing.
"""

import json
import os
import logging
import re
from typing import Optional

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Vague API names — these are too generic to show in the dropdown.
# When the API returns one of these, we prefer the curated name from models.json.
# ---------------------------------------------------------------------------
_VAGUE_NAMES = frozenset({"kling video", "minimax video", "hunyuan video"})

# ---------------------------------------------------------------------------
# Hardcoded fallback — used ONLY when models.json is missing
# ---------------------------------------------------------------------------
_FALLBACK_MODELS = [
    {"name": "Kling 2.5 Turbo Standard", "endpoint": "fal-ai/kling-video/v2.5-turbo/standard/image-to-video", "duration_default": 10},
    {"name": "Kling 2.5 Turbo Pro", "endpoint": "fal-ai/kling-video/v2.5-turbo/pro/image-to-video", "duration_default": 10},
    {"name": "MiniMax Video", "endpoint": "fal-ai/minimax-video/image-to-video", "duration_default": 10},
]


def _endpoint_to_short_name(endpoint: str) -> str:
    """Derive a readable display name from endpoint string as offline fallback.

    Examples:
        fal-ai/kling-video/v3/pro/image-to-video → Kling Video v3 Pro
        fal-ai/minimax-video/image-to-video → MiniMax Video
        fal-ai/hunyuan-video/v1.5/image-to-video → Hunyuan Video v1.5
    """
    if not endpoint:
        return "Unknown Model"

    # Remove prefix/suffix
    parts = (
        endpoint.replace("fal-ai/", "")
        .replace("/image-to-video", "")
        .replace("/video-to-video", "")
    )

    components = [p for p in parts.split("/") if p]
    if not components:
        return endpoint

    display_parts = []
    for comp in components:
        cleaned = comp.replace("-", " ").replace("_", " ")
        # Version number: keep lowercase v, capitalize rest
        if re.match(r"^v\d", cleaned):
            # e.g. "v2.5 turbo" → "v2.5 Turbo"
            sub_parts = cleaned.split()
            result = sub_parts[0]  # keep version as-is
            for sp in sub_parts[1:]:
                result += " " + sp.title()
            display_parts.append(result)
        elif cleaned.lower() == "o1":
            display_parts.append("O1")
        else:
            display_parts.append(cleaned.title())

    name = " ".join(display_parts)

    # Fix common brand capitalization
    name = name.replace("Minimax", "MiniMax")
    name = name.replace("Kling Video", "Kling Video")
    name = name.replace("Hunyuan Video", "Hunyuan Video")

    return name.strip()


def _load_models_from_file() -> list:
    """Load model list from models.json next to this file.

    Handles two formats:
      - New: models is a list of endpoint strings
      - Legacy: models is a list of dicts with name + endpoint

    Returns the models list on success, or _FALLBACK_MODELS if the file is
    missing, malformed, or empty.
    """
    models_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "models.json")
    if not os.path.exists(models_path):
        logger.debug("models.json not found at %s — using fallback list", models_path)
        return _FALLBACK_MODELS

    try:
        with open(models_path, "r", encoding="utf-8") as fh:
            data = json.load(fh)
        models_raw = data.get("models", [])
        user_notes = data.get("user_notes", {})

        if not models_raw:
            logger.warning("models.json has no 'models' list — using fallback")
            return _FALLBACK_MODELS

        models = []
        for entry in models_raw:
            if isinstance(entry, str):
                # Endpoint-only string (e.g. browse-added models)
                models.append({
                    "name": _endpoint_to_short_name(entry),
                    "endpoint": entry,
                    "user_notes": user_notes.get(entry, ""),
                })
            elif isinstance(entry, dict):
                # Dict with name + endpoint (+ optional release, notes, etc.)
                if entry.get("endpoint"):
                    ep = entry["endpoint"]
                    # Derive name from endpoint if not provided
                    if not entry.get("name"):
                        entry["name"] = _endpoint_to_short_name(ep)
                    # Carry over user_notes from the map
                    if ep in user_notes and "user_notes" not in entry:
                        entry["user_notes"] = user_notes[ep]
                    models.append(entry)

        if not models:
            logger.warning("models.json has no valid entries — using fallback")
            return _FALLBACK_MODELS

        logger.debug("Loaded %d models from models.json", len(models))
        return models
    except (json.JSONDecodeError, OSError) as exc:
        logger.warning("Failed to load models.json (%s) — using fallback", exc)
        return _FALLBACK_MODELS


# ---------------------------------------------------------------------------
# Public model list — imported by config_panel and other modules
# ---------------------------------------------------------------------------
MODEL_METADATA = _load_models_from_file()


# ---------------------------------------------------------------------------
# Display-name helper
# ---------------------------------------------------------------------------

def get_model_display_name(model: dict) -> str:
    """Build the dropdown label for a model.

    Name priority:
      1. model["name"] — user-curated from models.json (always preferred)
      2. model["api_display_name"] — but ONLY if not vague
      3. _endpoint_to_short_name() — offline fallback

    Pricing priority:
      1. model["pricing_info"] from live API → "$X.XX/10s"
      2. model["est_cost_10s"] legacy → "~$X.XX"

    Examples:
        "Kling 3.0 Pro (Feb 2026), $2.24/10s"
        "Kling 3.0 Pro (Feb 2026)"
        "MiniMax Video (2024), $0.50/video"
    """
    # Name: prefer models.json name, fall back to non-vague API name, then offline
    name = model.get("name", "")
    if not name:
        api_name = model.get("api_display_name", "")
        if api_name and api_name.strip().lower() not in _VAGUE_NAMES:
            name = api_name
        else:
            name = _endpoint_to_short_name(model.get("endpoint", ""))

    release = model.get("release", "")

    # Pricing: prefer live API → curated models.json pricing_fallback →
    # legacy est_cost_10s. The pricing_fallback tier (added 2026-06-25) means
    # curated models (e.g. Seedance, which is token-priced and converted to a
    # per-second figure in models.json) show their price in the dropdown even
    # BEFORE the live fal API enriches pricing_info — and offline.
    def _fmt_price(p: dict) -> str:
        # Use `is None`, not falsiness: a genuine $0.00 (free) model has
        # unit_price == 0, which is falsy — `not p.get(...)` would drop its
        # price and render the model with no cost label.
        if not p or p.get("unit_price") is None:
            return ""
        unit = p.get("unit", "")
        price = p["unit_price"]
        if unit == "second":
            return f"${price * 10:.2f}/10s"
        if unit == "video":
            return f"${price:.2f}/video"
        if unit == "image":
            return f"${price:.2f}/image"
        return f"${price:.2f}/{unit}" if unit else f"${price:.2f}"

    cost_str = _fmt_price(model.get("pricing_info", {}))
    if not cost_str:
        cost_str = _fmt_price(model.get("pricing_fallback", {}))
    if not cost_str and model.get("est_cost_10s"):
        cost_str = f"~{model['est_cost_10s']}"

    # Assemble: "Name (release), cost"
    if release and cost_str:
        return f"{name} ({release}), {cost_str}"
    if release:
        return f"{name} ({release})"
    if cost_str:
        return f"{name}, {cost_str}"
    return name


# ---------------------------------------------------------------------------
# Convenience lookups (unchanged API)
# ---------------------------------------------------------------------------

def get_model_by_endpoint(endpoint: str):
    """Return a copy of the model dict matching `endpoint`, or None."""
    for model in MODEL_METADATA:
        if model.get("endpoint") == endpoint:
            return model.copy()
    return None


def get_duration_options(endpoint: str) -> list:
    """Valid duration values (seconds) for the given endpoint."""
    model = get_model_by_endpoint(endpoint)
    if model:
        return model.get("duration_options", [5, 10])
    return [5, 10]


def get_duration_default(endpoint: str) -> int:
    """Default duration (seconds) for the given endpoint."""
    model = get_model_by_endpoint(endpoint)
    if model:
        return model.get("duration_default", 10)
    return 10


# Standard 16:9 pixel dimensions per resolution label. Used by the token-cost
# estimator. 720p (1280x720) and 1080p (1920x1080) reproduce fal's published
# per-second prices exactly; 480p (854x480) is the standard 16:9 480 and is
# accurate to ~2% (fal does not publish exact 480p dims). aspect_ratio="auto"
# can pick non-16:9 dims at runtime, so this is an ESTIMATE — the real bill uses
# the actual returned frame size.
_RESOLUTION_DIMS = {
    "480p": (854, 480),
    "720p": (1280, 720),
    "1080p": (1920, 1080),
    "4k": (3840, 2160),
}


def get_resolution_options(endpoint: str) -> list:
    """Valid resolution labels for the endpoint, or [] when the model exposes
    no user-selectable resolution (callers should then hide/disable the control
    rather than send a `resolution` the live schema would just drop)."""
    model = get_model_by_endpoint(endpoint)
    if model:
        options = model.get("resolution_options")
        if isinstance(options, list):
            return [str(o) for o in options]
    return []


def get_resolution_default(endpoint: str) -> str:
    """Default resolution label for the endpoint (falls back to '720p')."""
    model = get_model_by_endpoint(endpoint)
    if model:
        default = model.get("resolution_default")
        if default:
            return str(default)
        options = model.get("resolution_options")
        if isinstance(options, list) and options:
            return str(options[0])
    return "720p"


def get_token_pricing(endpoint: str) -> Optional[dict]:
    """The model's token_pricing block, or None for flat per-second/clip models."""
    model = get_model_by_endpoint(endpoint)
    if model:
        tp = model.get("token_pricing")
        if isinstance(tp, dict):
            return tp
    return None


def estimate_cost_usd(
    endpoint: str,
    resolution: Optional[str] = None,
    duration: Optional[int] = None,
    audio: bool = False,
) -> Optional[float]:
    """Estimated USD cost of one clip for a token-priced model.

    tokens = (width * height * fps * duration) / 1024 ; cost = tokens/1e6 * rate.
    The 4k rate (rate_per_million_4k) applies for resolution == "4k" when set;
    audio_rate_multiplier applies when ``audio`` is True and the model defines it
    (only Seedance 1.5 Pro). Returns None when the model has no token_pricing OR
    the resolution dims are unknown — callers then fall back to the flat
    pricing_fallback (e.g. unit_price * duration).
    """
    tp = get_token_pricing(endpoint)
    if not tp:
        return None
    res = str(resolution or get_resolution_default(endpoint))
    # Clamp to what THIS model actually offers: estimating e.g. 1080p on a
    # 720p-capped Seedance tier (Fast/Mini) would quote a price the model can't
    # produce (CodeRabbit, PR #114). When the requested res isn't in the model's
    # options, return None so callers fall back / show nothing rather than lie.
    options = get_resolution_options(endpoint)
    if options and res not in options:
        return None
    dims = _RESOLUTION_DIMS.get(res)
    if not dims:
        return None
    try:
        dur = int(duration) if duration is not None else get_duration_default(endpoint)
    except (TypeError, ValueError):
        dur = get_duration_default(endpoint)
    try:
        dur = int(dur)
    except (TypeError, ValueError):
        dur = 10
    if dur <= 0:
        dur = get_duration_default(endpoint) or 10
    fps = tp.get("fps", 24) or 24
    width, height = dims
    tokens = (width * height * fps * dur) / 1024.0
    rate = tp.get("rate_per_million")
    if res == "4k" and tp.get("rate_per_million_4k") is not None:
        rate = tp.get("rate_per_million_4k")
    if rate is None:
        return None
    cost = (tokens / 1_000_000.0) * float(rate)
    if audio:
        mult = tp.get("audio_rate_multiplier")
        if mult:
            cost *= float(mult)
    return round(cost, 2)


# Conservative defaults for any model whose capability flags are absent
# (legacy entries, custom user models, an unknown endpoint): no end-frame
# lock, no cfg_scale, no negative_prompt -> the dispatcher simply omits
# those params, which is always safe. start defaults to "image_url"
# (the most common Kling/fal convention). All capability flags live in
# models.json (single source of truth — dispatcher AND GUI read them
# only through this helper, never re-deriving).
_CAPABILITY_DEFAULTS = {
    "start_image_param": "image_url",
    "end_image_param": None,
    "supports_negative_prompt": False,
    "supports_cfg_scale": False,
}


def get_model_capabilities(endpoint: str) -> dict:
    """Return the per-model API capability flags for *endpoint*.

    Keys: ``start_image_param`` (str), ``end_image_param`` (str | None),
    ``supports_negative_prompt`` (bool), ``supports_cfg_scale`` (bool).
    Always returns a fully-populated dict — unknown/legacy/custom models
    get the conservative defaults above so callers never KeyError and an
    unflagged model degrades to a plain prompt+image submit.
    """
    caps = dict(_CAPABILITY_DEFAULTS)
    model = get_model_by_endpoint(endpoint)
    if model:
        for key in _CAPABILITY_DEFAULTS:
            # Override the default only when the key is explicitly present
            # in models.json. A JSON ``null`` (-> Python None) is a valid,
            # intentional value for ``end_image_param`` ("no end frame"),
            # so honour it rather than falling back to the default.
            if key in model:
                caps[key] = model[key]
    return caps


# ---------------------------------------------------------------------------
# Prompt length limits (from fal.ai OpenAPI schemas)
# ---------------------------------------------------------------------------

# Pattern-based limits: (substring_in_endpoint, max_chars)
_PROMPT_LIMITS = [
    ("minimax", 2000),
    ("kling-video/v3", None),     # v3 has no documented limit
    ("kling-video", 2500),        # v2.x / v1.x / O1
    ("hunyuan", 2500),            # not in schema but safe default
]

# Default fallback
_DEFAULT_PROMPT_LIMIT = 2500


def get_prompt_limit(endpoint: str) -> int:
    """Return the maximum prompt length (chars) for the given model endpoint.

    Falls back to _DEFAULT_PROMPT_LIMIT (2500) for unknown models.
    """
    ep = endpoint.lower()
    for pattern, limit in _PROMPT_LIMITS:
        if pattern in ep:
            return limit
    return _DEFAULT_PROMPT_LIMIT
