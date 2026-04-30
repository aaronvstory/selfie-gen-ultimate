from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Dict, List


AUTOMATION_DEFAULTS: Dict[str, Any] = {
    "automation_manifest_name": "automation_manifest.json",
    "automation_front_names": ["front.png", "front.jpg", "front.jpeg"],
    "automation_skip_completed": True,
    "automation_skip_if_selfie_exists": True,
    "automation_skip_if_video_exists": True,
    "automation_max_cases_per_run": "5",  # 1 | 5 | 10 | all
    "automation_allow_reprocess": False,
    "automation_reprocess_mode": "skip",  # skip | overwrite | increment
    "automation_front_expand_enabled": True,
    "automation_front_expand_provider": "auto",  # auto | bfl | fal
    "automation_front_expand_mode": "document_3x4",  # document_3x4 | percent
    "automation_front_expand_percent": 30,
    "automation_front_edge_seal_enabled": True,
    "automation_front_edge_seal_px": 12,
    "automation_front_output_name": "front-expanded.png",
    "automation_extract_enabled": True,
    "automation_extract_output_name": "extracted.png",
    "automation_crop_multiplier": 1.5,
    "automation_selfie_enabled": True,
    "automation_selfie_models": ["openai/gpt-image-2/edit"],
    "automation_selfie_model_policy": "first_pass",  # first_pass | all
    "automation_selfie_prompt_mode": "existing_config",
    "automation_selfie_max_attempts_per_model": 1,
    "automation_similarity_threshold": 80,
    "automation_selfie_expand_enabled": True,
    "automation_selfie_expand_provider": "auto",
    "automation_selfie_expand_mode": "percent",  # percent | centered_3x4
    "automation_selfie_expand_percent": 30,
    "automation_selfie_expand_edge_seal_enabled": False,
    "automation_video_enabled": True,
    "automation_video_aspect_ratio": "3:4",
    "automation_video_use_existing_prompt": True,
    "automation_oldcam_enabled": True,
    "automation_oldcam_version": "v8",
    "automation_oldcam_required": False,
}


@dataclass(frozen=True)
class AutomationConfig:
    values: Dict[str, Any]

    def get(self, key: str, default: Any = None) -> Any:
        return self.values.get(key, default)

    @property
    def manifest_name(self) -> str:
        return str(self.values["automation_manifest_name"])

    @property
    def front_names(self) -> List[str]:
        raw = self.values.get("automation_front_names", [])
        return [str(name).lower() for name in raw]


def merge_automation_defaults(config: Dict[str, Any]) -> Dict[str, Any]:
    merged = dict(config)
    for key, value in AUTOMATION_DEFAULTS.items():
        if key not in merged:
            merged[key] = value
    return merged


def from_app_config(config: Dict[str, Any]) -> AutomationConfig:
    return AutomationConfig(values=merge_automation_defaults(config))
