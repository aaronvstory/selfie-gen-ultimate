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
    changed = False
    for spec in API_KEY_SPECS:
        if spec.config_key not in config or config[spec.config_key] is None:
            config[spec.config_key] = ""
            changed = True
    return changed


def key_is_set(config: Dict[str, Any], config_key: str) -> bool:
    return bool(str(config.get(config_key, "") or "").strip())


def key_status(config: Dict[str, Any], config_key: str) -> str:
    return "added" if key_is_set(config, config_key) else "missing"


def required_missing_specs(config: Dict[str, Any]) -> List[ApiKeySpec]:
    return [spec for spec in API_KEY_SPECS if spec.required_at_start and not key_is_set(config, spec.config_key)]


def status_lines(config: Dict[str, Any]) -> List[str]:
    lines: List[str] = []
    for spec in API_KEY_SPECS:
        required = " required at startup" if spec.required_at_start else " optional"
        lines.append(f"{spec.label}: {key_status(config, spec.config_key)} ({required})")
    return lines


def non_required_missing_specs(config: Dict[str, Any]) -> Iterable[ApiKeySpec]:
    for spec in API_KEY_SPECS:
        if not spec.required_at_start and not key_is_set(config, spec.config_key):
            yield spec
