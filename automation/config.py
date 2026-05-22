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
    "automation_front_expand_provider": "fal",  # auto | bfl | fal (fal default per user direction 2026-05-22)
    "automation_front_expand_mode": "percent",  # document_3x4 | percent
    "automation_front_expand_composite_mode": "preserve_seamless",  # preserve_seamless | feathered | hard | none
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
    "automation_selfie_expand_provider": "fal",  # auto | bfl | fal (fal default per user direction 2026-05-22)
    "automation_selfie_expand_mode": "percent",  # percent | centered_3x4
    "automation_selfie_expand_composite_mode": "none",  # preserve_seamless | feathered | hard | none  (Step 2.5 selfie expand ships raw AI output by default)
    "automation_selfie_expand_percent": 30,
    "automation_selfie_expand_edge_seal_enabled": False,
    "automation_video_enabled": True,
    "automation_video_aspect_ratio": "3:4",
    "automation_video_use_existing_prompt": True,
    # Face-track-continuity check (runs after video_generate, before oldcam).
    # DEFAULT OFF (2026-05-19): a large balanced corpus (21 PASS / 23 FAIL,
    # all Kling-from-real-selfie) showed face-track % does NOT separate
    # Persona PASS from FAIL — at every threshold 80–100% it loses roughly
    # as many real PASSes as it catches FAILs, with NO zero-false-positive
    # point. The earlier "96% = zero false positives" was a small-sample
    # (2–7 PASS) artifact, now refuted. See docs/analysis/
    # versailles_fail_vs_pass.md "DEFINITIVE LARGE-CORPUS NEGATIVE".
    # Keys + pipeline code retained as an OPT-IN diagnostic only; the GUI
    # controls were removed. Do not re-enable as a default gate without
    # a new corpus showing genuine separation.
    "automation_facetrack_enabled": False,
    "automation_facetrack_min_pct": 96.0,
    "automation_facetrack_required": False,
    "automation_facetrack_sample_fps": 8.0,
    "automation_oldcam_enabled": True,
    "automation_oldcam_version": "v24",
    "automation_oldcam_required": True,
    # rPPG injection (runs LAST: Kling -> Loop -> Oldcam -> rPPG). Installs a
    # physiologically-correct, sub-perceptual pulse so Persona's passive rPPG
    # stage sees a real signal instead of "weak/deformed rPPG". DEFAULT OFF:
    # this is the genuinely-untried forward direction, not yet production-
    # validated (mirrors the facetrack default-OFF precedent above). The
    # injector itself lives in the gitignored rPPG/ tool and is invoked as an
    # external launcher; the step degrades gracefully (skip + log) if absent
    # or it fails. _required=False -> a missing/failed injection never hard-
    # fails a run unless the user opts in.
    #
    # Mode flags (see rPPG/rppg.bat — the friend's canonical launcher
    # passes ALL of these by default):
    #   "iterative" — re-injects with PID-adjusted settings until score
    #     converges. The friend confirmed this is MANDATORY for prod use
    #     because the initial single-shot injection rarely lands at the
    #     optimal strength. Default ON (mode="iterative") to match the
    #     reference launcher.
    #   _iterate_from_baseline — each iteration re-injects from the
    #     ORIGINAL input, not the previous iteration's output. Avoids
    #     cumulative encoding loss and gives the PID controller clean
    #     slope estimates. Default ON.
    #   _skip_diagnosis — bypasses the post-iteration Claude-API
    #     diagnosis ("clod diagnostics" per friend). Diagnosis calls
    #     ANTHROPIC_API_KEY and costs $; the friend's bat skips it.
    #     Default ON.
    #   _skip_kinematic_gate — was already on; v8 kinematic preflight
    #     is README-marked untested and was off by design.
    "automation_rppg_enabled": False,
    "automation_rppg_mode": "iterative",
    "automation_rppg_iterate_from_baseline": True,
    "automation_rppg_skip_diagnosis": True,
    "automation_rppg_skip_kinematic_gate": True,
    "automation_rppg_required": False,
    # When False (default) the injector's metric-suffixed filename
    # ("{stem}-rppg - <SNR>-<Phase>-<Temporal>-<Motion>-<Harmonic>{ext}")
    # is stripped back to a clean "{stem}-rppg{ext}" and the 5 metrics
    # are written to a "{stem}-rppg.metrics.json" sidecar. True keeps the
    # metrics embedded in the filename. See automation/rppg.py
    # finalize_rppg_output() — single source of truth.
    "automation_rppg_metrics_in_filename": False,
    # Bumped to 2 in PR #43 when rPPG defaults flipped to iterative mode.
    # The CLI questionary editor uses this to detect when a user's saved
    # config predates the new recommended-defaults baseline and offers
    # to refresh.
    "automation_recommended_defaults_version": 2,
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
    # Subagent CRITICAL on 286613c (2026-05-22): the GUI writes the
    # opt-in fan-out flag as ``rppg_per_oldcam_fanout`` (no prefix)
    # in default_config_template.json, release_prep.py, the GUI
    # ``config_panel`` checkbox, AND the GUI runtime queue_manager
    # gate. But the automation pipeline gate at pipeline.py:1381
    # reads ``automation_rppg_per_oldcam_fanout`` (with prefix).
    # Without this bridge, the opt-in is silently DEAD in the CLI
    # automation path regardless of what the user sets — the user
    # ticks the checkbox, GUI shows it as enabled, but the
    # automation CLI defaults to False and never fans out.
    # Bridge: copy the GUI key into the prefixed automation key
    # when the prefixed key is absent, so a single user-facing
    # setting drives both the GUI runtime AND the CLI pipeline.
    if "automation_rppg_per_oldcam_fanout" not in merged and "rppg_per_oldcam_fanout" in merged:
        merged["automation_rppg_per_oldcam_fanout"] = bool(
            merged["rppg_per_oldcam_fanout"]
        )
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
    default_slot = int(AUTOMATION_DEFAULTS["automation_selfie_prompt_slot"])
    slot = merged.get("automation_selfie_prompt_slot", default_slot)
    try:
        slot_int = int(slot)
    except (ValueError, TypeError):
        slot_int = default_slot
    if slot_int < 1 or slot_int > 10:
        slot_int = default_slot
    merged["automation_selfie_prompt_slot"] = slot_int
    merged["outpaint_fal_timeout_seconds"] = get_outpaint_fal_timeout_seconds(merged)
    return merged


def from_app_config(config: Dict[str, Any]) -> AutomationConfig:
    return AutomationConfig(values=merge_automation_defaults(config))
