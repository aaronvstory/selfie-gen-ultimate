from __future__ import annotations

from typing import Any, Dict, List

from api_keys import API_KEY_SPECS, ApiKeySpec, key_status


def startup_prompt_specs() -> List[ApiKeySpec]:
    """Return startup-prompt key specs in fixed launch order."""
    key_order = {"falai_api_key": 0, "bfl_api_key": 1}
    selected = [spec for spec in API_KEY_SPECS if spec.config_key in key_order]
    return sorted(selected, key=lambda spec: key_order.get(spec.config_key, 99))


def startup_status_lines(config: Dict[str, Any]) -> List[str]:
    """Return launch-time key status lines for Fal.ai and BFL."""
    lines: List[str] = []
    for spec in startup_prompt_specs():
        lines.append(f"{spec.label}: {key_status(config, spec.config_key)}")
    return lines


def missing_startup_specs(config: Dict[str, Any]) -> List[ApiKeySpec]:
    """Return startup-prompt key specs that are currently missing."""
    return [
        spec
        for spec in startup_prompt_specs()
        if not str(config.get(spec.config_key, "")).strip()
    ]
