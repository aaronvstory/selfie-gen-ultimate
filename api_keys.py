from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Dict, Iterable, List


@dataclass(frozen=True)
class ApiKeySpec:
    config_key: str
    label: str
    url: str
    instruction: str
    required_at_start: bool = False


API_KEY_SPECS: List[ApiKeySpec] = [
    ApiKeySpec(
        config_key="falai_api_key",
        label="Fal.ai",
        url="https://fal.ai/dashboard/keys",
        instruction="Create a key in fal.ai dashboard, then paste it here.",
        required_at_start=True,
    ),
    ApiKeySpec(
        config_key="bfl_api_key",
        label="BFL",
        url="https://api.bfl.ai/",
        instruction="Create a BFL API key for BFL-powered selfie/outpaint models.",
    ),
    ApiKeySpec(
        config_key="openrouter_api_key",
        label="OpenRouter",
        url="https://openrouter.ai/keys",
        instruction="Create an OpenRouter key for Prep tab vision analysis models.",
    ),
    ApiKeySpec(
        config_key="freeimage_api_key",
        label="Freeimage",
        url="https://freeimage.host/page/api",
        instruction="Create a Freeimage API key for image upload URL fallback.",
    ),
]


def ensure_key_fields(config: Dict[str, Any]) -> bool:
    """Ensure all expected API key fields exist in config.

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
