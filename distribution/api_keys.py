from __future__ import annotations

import os
from dataclasses import dataclass, field
from typing import Any, Dict, Iterable, List, Tuple


@dataclass(frozen=True)
class ApiKeySpec:
    config_key: str
    label: str
    url: str
    instruction: str
    required_at_start: bool = False
    # Environment variable name(s) to fall back to when this key is missing from
    # the saved config, checked IN ORDER (first non-empty wins). Multiple aliases
    # because users store the same key under different conventional names — e.g.
    # fal.ai is FAL_KEY (its SDK's native name) OR the common FAL_API_KEY
    # ("…_API_KEY" suffix, matching OPENROUTER_API_KEY). A saved config value
    # ALWAYS wins — the env var is a fallback for an empty key only.
    env_vars: Tuple[str, ...] = field(default_factory=tuple)

    @property
    def env_var(self) -> str:
        """Back-compat: the primary (first) env var name, or '' if none."""
        return self.env_vars[0] if self.env_vars else ""


# NOTHING is required at startup (user direction 2026-06-04): a user may only
# want rPPG / Oldcam (which need no key at all), or only fal.ai (the default
# provider for every flow). The first-launch dialog is purely informational and
# never blocks. BFL in particular is NOT needed — it powers a couple of optional
# selfie/outpaint models; fal.ai is the default everywhere.
API_KEY_SPECS: List[ApiKeySpec] = [
    ApiKeySpec(
        config_key="falai_api_key",
        label="Fal.ai",
        url="https://fal.ai/dashboard/keys",
        instruction="Create a key in fal.ai dashboard, then paste it here.",
        # FAL_KEY (fal SDK native) + FAL_API_KEY (the common …_API_KEY form the
        # user actually had set — that's why auto-detect missed it before).
        env_vars=("FAL_KEY", "FAL_API_KEY"),
    ),
    ApiKeySpec(
        config_key="bfl_api_key",
        label="BFL",
        url="https://api.bfl.ai/",
        instruction="Create a BFL API key for BFL-powered selfie/outpaint models.",
        env_vars=("BFL_API_KEY", "BFL_KEY"),
    ),
    ApiKeySpec(
        config_key="openrouter_api_key",
        label="OpenRouter",
        url="https://openrouter.ai/keys",
        instruction="Create an OpenRouter key for Prep tab vision analysis models.",
        env_vars=("OPENROUTER_API_KEY", "OPENROUTER_KEY"),
    ),
    ApiKeySpec(
        config_key="freeimage_api_key",
        label="Freeimage",
        url="https://freeimage.host/page/api",
        instruction="Create a Freeimage API key for image upload URL fallback.",
        env_vars=("FREEIMAGE_API_KEY", "FREEIMAGE_KEY"),
    ),
]


def ensure_key_fields(config: Dict[str, Any]) -> bool:
    """Ensure all expected API key fields exist in config.

    Structural normalization ONLY: a missing/None key becomes "". Does NOT read
    env vars (that's apply_env_key_fallback, which is in-memory and not
    persisted) so the saved kling_config.json never silently gains a key value.

    Args:
        config: Configuration dictionary to normalize.

    Returns:
        True if any missing fields were added, else False.
    """
    changed = False
    for spec in API_KEY_SPECS:
        if spec.config_key not in config or config[spec.config_key] is None:
            config[spec.config_key] = ""
            changed = True
    return changed


def env_key_optout_list(config: Dict[str, Any]) -> List[str]:
    """Safely read the persisted ``_env_key_optout`` list from a config dict.

    Returns a plain list of config_key strings the user explicitly cleared.
    A hand-edited / corrupted config could store this as a string, dict, int,
    etc.; coercing those via ``list()``/``set()`` would silently iterate
    characters or raise TypeError (gemini, PR #73). Only an actual list of
    strings is honoured; anything else yields [] (the safe default = env
    fallback active). The single accessor every read site should use.
    """
    val = config.get("_env_key_optout")
    if not isinstance(val, list):
        return []
    return [k for k in val if isinstance(k, str)]


def apply_env_key_fallback(config: Dict[str, Any]) -> List[str]:
    """In-memory prefill of empty API keys from their environment variables.

    For each key spec with an ``env_var``: if the config value is empty/blank
    AND the env var is set, copy the env value into the in-memory config so the
    app just works without nagging. A saved (non-empty) config value ALWAYS
    overrides — the env var is a fallback for a missing key only.

    Deliberately MUTATES ``config`` in place but does NOT mark it dirty for save:
    callers must NOT persist on the strength of this, so the env value is
    re-read every launch (env stays the source of truth; rotating the env var
    takes effect immediately; the saved config file never silently gains the
    secret). Returns the list of config_keys that were filled from env (for an
    optional one-line "loaded from environment" log).

    Args:
        config: Configuration dictionary to prefill in place.

    Returns:
        List of config_key names that were populated from their env var.
    """
    # Keys the user EXPLICITLY cleared to empty in the GUI/CLI opt OUT of the
    # env fallback, persistently — otherwise clearing a key whose env var is
    # still set would silently re-prefill on the next launch, so "leave blank to
    # clear" wouldn't survive a restart (CodeRabbit, PR #73). The opt-out list
    # IS persisted (it's a plain config key, not an env-prefill marker).
    optout = set(env_key_optout_list(config))
    filled: List[str] = []
    for spec in API_KEY_SPECS:
        if not spec.env_vars:
            continue
        if spec.config_key in optout:
            continue  # user deliberately cleared this — respect it
        if str(config.get(spec.config_key, "") or "").strip():
            continue  # user-saved value wins
        # Try each accepted env var name in order; first non-empty wins.
        for name in spec.env_vars:
            env_val = os.environ.get(name, "")
            if env_val and env_val.strip():
                config[spec.config_key] = env_val.strip()
                filled.append(spec.config_key)
                break
    return filled


def _spec_for(config_key: str) -> "ApiKeySpec | None":
    """The ApiKeySpec for a config_key, or None if unknown."""
    for spec in API_KEY_SPECS:
        if spec.config_key == config_key:
            return spec
    return None


def resolve_api_key(config: Dict[str, Any], config_key: str) -> str:
    """Resolve an API key value: saved config first, then env-var aliases.

    The single accessor any code should use when it needs a key value and wants
    the env-var auto-detect to apply (e.g. a standalone CLI/GUI inspector that
    runs outside the apply_env_key_fallback'd config). A non-empty saved config
    value wins; otherwise each env alias for the key (FAL_KEY / FAL_API_KEY, …)
    is tried in order. Returns "" if nothing is set. Fixes the
    "only checked FAL_KEY, user has FAL_API_KEY" gap (code-review Codex P2 #73).
    """
    saved = str(config.get(config_key, "") or "").strip()
    if saved:
        return saved
    # Honor a persisted "user explicitly cleared this key" opt-out, exactly like
    # apply_env_key_fallback does — otherwise the CLI/GUI key inspectors would
    # bypass the clear and resolve the env alias anyway, so "clear key" wouldn't
    # stick for the inspector paths (code-review, PR #73).
    if config_key in set(env_key_optout_list(config)):
        return ""
    spec = _spec_for(config_key)
    if spec:
        for name in spec.env_vars:
            val = os.environ.get(name, "")
            if val and val.strip():
                return val.strip()
    return ""


def key_is_set(config: Dict[str, Any], config_key: str) -> bool:
    """Check whether a key exists and has a non-empty value.

    Args:
        config: Configuration dictionary.
        config_key: Key name to inspect.

    Returns:
        True when the value is non-empty after trimming; otherwise False.
    """
    return bool(str(config.get(config_key, "") or "").strip())


def key_status(config: Dict[str, Any], config_key: str) -> str:
    """Return a display status label for a key.

    Args:
        config: Configuration dictionary.
        config_key: Key name to inspect.

    Returns:
        "added" when key is set, otherwise "missing".
    """
    return "added" if key_is_set(config, config_key) else "missing"


def required_missing_specs(config: Dict[str, Any]) -> List[ApiKeySpec]:
    """List required key specs that are currently missing.

    Args:
        config: Configuration dictionary.

    Returns:
        List of required ApiKeySpec entries without configured values.
    """
    return [spec for spec in API_KEY_SPECS if spec.required_at_start and not key_is_set(config, spec.config_key)]


def status_lines(config: Dict[str, Any]) -> List[str]:
    """Build human-readable key status lines.

    Args:
        config: Configuration dictionary.

    Returns:
        Formatted lines for all key specs with status and requirement flag.
    """
    lines: List[str] = []
    for spec in API_KEY_SPECS:
        required = " required at startup" if spec.required_at_start else " optional"
        lines.append(f"{spec.label}: {key_status(config, spec.config_key)} ({required})")
    return lines


def non_required_missing_specs(config: Dict[str, Any]) -> Iterable[ApiKeySpec]:
    """Yield optional key specs that are currently missing.

    Args:
        config: Configuration dictionary.

    Yields:
        Optional ApiKeySpec entries without configured values.
    """
    for spec in API_KEY_SPECS:
        if not spec.required_at_start and not key_is_set(config, spec.config_key):
            yield spec
