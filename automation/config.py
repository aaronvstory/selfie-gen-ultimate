from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Dict, List, Mapping


DEFAULT_SELFIE_PROMPT = (
    "Generate an ultra-realistic, front-facing selfie taken inside a parked car from the driver’s seat perspective. "
    "Camera held at arm’s length, centered framing with head-and-shoulders composition, eye-level angle, slightly wide "
    "perspective typical of a smartphone front camera (24–28mm equivalent focal length).\n\n"
    "Strong natural sunlight entering from the right side (passenger side window), creating a sharp vertical band of "
    "light across the center of the face and torso, with the opposite side in softer shadow. High contrast lighting with "
    "crisp highlight falloff and defined shadow edges. Subtle catchlights and reflections visible in the eyes (or eyewear, "
    "if present) from bright outdoor light.\n\n"
    "Interior shows a clean, modern car cabin with beige upholstery, visible headrests, and soft-touch surfaces. "
    "Background includes rear seats and side windows. Through the windows: bright sunny environment with clear blue sky, "
    "palm trees, and landscaped roadside or parking area. The entire scene, including the background, is in sharp focus.\n\n"
    "Color temperature is warm and natural daylight. Image has standard smartphone exposure characteristics, preserving "
    "highlight and shadow detail without looking overprocessed. Deep depth of field with both the subject and the background "
    "remaining sharp. Slight lens distortion at edges consistent with a wide-angle front-facing camera.\n\n"
    "Captured as an unedited, raw smartphone photo with realistic detail rendering. Natural tonal variation and imperfect "
    "exposure balance, absolutely not studio-lit.\n\n"
    "Realism and Identity instructions: Strictly preserve the exact facial structure, identity, age, and features of the "
    "input subject. Maintain natural skin texture with visible pores and micro-imperfections, subtle uneven tones, and no "
    "skin smoothing or airbrushing. Avoid overly clean or sterile rendering.\n\n"
    "Do not apply any beauty filters, AI smoothing, waxy or plastic appearance, hyper-polished or CGI look, unrealistically "
    "perfect lighting or textures, excessive sharpening, artificial skin softening, portrait mode blur, artificial bokeh, "
    "or shallow depth of field. ONLY PUT GLASSES IF THE ORIGINAL IMAGE HAS GLASSES ON, OTHERWISE, DO NOT ADD ANY EYEWEAR."
)

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
    "automation_front_expand_provider": "bfl",  # auto | bfl | fal
    "automation_front_expand_mode": "percent",  # document_3x4 | percent
    "automation_front_expand_percent": 70,
    "automation_front_expand_passes": 2,  # 1 | 2
    "automation_front_edge_seal_enabled": False,
    "automation_front_edge_seal_px": 12,
    "automation_front_output_name": "front-expanded.png",
    "automation_extract_enabled": True,
    "automation_extract_output_name": "extracted.png",
    "automation_crop_multiplier": 1.5,
    "automation_selfie_enabled": True,
    "automation_selfie_models": ["fal-ai/nano-banana-2/edit"],
    "automation_selfie_prompt_slot": 3,
    "automation_selfie_prompts": {
        "1": DEFAULT_SELFIE_PROMPT,
        "2": "",
        "3": DEFAULT_SELFIE_PROMPT,
        "4": "",
        "5": "",
        "6": "",
        "7": "",
        "8": "",
        "9": "",
        "10": "",
    },
    "automation_selfie_model_policy": "first_pass",  # first_pass | all
    "automation_selfie_prompt_mode": "wildcards",
    "automation_selfie_max_attempts_per_model": 1,
    "automation_similarity_threshold": 80,
    "automation_selfie_expand_enabled": True,
    "automation_selfie_expand_provider": "bfl",
    "automation_selfie_expand_mode": "percent",  # percent | centered_3x4
    "automation_selfie_expand_percent": 30,
    "automation_selfie_expand_edge_seal_enabled": False,
    "automation_video_enabled": True,
    "automation_video_aspect_ratio": "3:4",
    "automation_video_use_existing_prompt": True,
    "automation_oldcam_enabled": True,
    "automation_oldcam_version": "v8",
    "automation_oldcam_required": True,
    "automation_recommended_defaults_version": 1,
    "automation_verbose_logging": True,
    "automation_log_max_bytes": 2097152,
    "automation_log_backup_count": 5,
    "outpaint_fal_timeout_seconds": 150,
}


def get_outpaint_fal_timeout_seconds(
    config_or_mapping: Mapping[str, Any],
    default: int = 150,
    min_seconds: int = 30,
    max_seconds: int = 300,
) -> int:
    """Normalize outpaint timeout seconds from config-like mappings."""
    try:
        raw = config_or_mapping.get("outpaint_fal_timeout_seconds", default)
        value = int(raw)
    except (ValueError, TypeError, AttributeError):
        value = default
    return max(min_seconds, min(max_seconds, value))


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
    prompts = merged.get("automation_selfie_prompts")
    if not isinstance(prompts, dict):
        merged["automation_selfie_prompts"] = dict(AUTOMATION_DEFAULTS["automation_selfie_prompts"])
    else:
        for slot, default_prompt in AUTOMATION_DEFAULTS["automation_selfie_prompts"].items():
            prompts.setdefault(slot, default_prompt if slot in {"1", "3"} else "")
        if not str(prompts.get("1", "")).strip():
            prompts["1"] = AUTOMATION_DEFAULTS["automation_selfie_prompts"]["1"]
        if not str(prompts.get("3", "")).strip():
            prompts["3"] = AUTOMATION_DEFAULTS["automation_selfie_prompts"]["3"]
    slot = merged.get("automation_selfie_prompt_slot", 3)
    try:
        slot_int = int(slot)
    except (ValueError, TypeError):
        slot_int = 3
    if slot_int < 1 or slot_int > 10:
        slot_int = 3
    merged["automation_selfie_prompt_slot"] = slot_int
    merged["outpaint_fal_timeout_seconds"] = get_outpaint_fal_timeout_seconds(merged)
    return merged


def from_app_config(config: Dict[str, Any]) -> AutomationConfig:
    return AutomationConfig(values=merge_automation_defaults(config))
