from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Dict, List, Mapping, Optional, Tuple


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
    # Optional fnmatch glob patterns for the per-folder front image, matched on
    # the lowercased filename IN ADDITION to automation_front_names. Empty by
    # default = exact-name behavior unchanged. Lets real-world batches whose
    # input is not literally "front.jpg" (e.g. "*id_photo*.jpg") be discovered
    # without renaming source files. See discovery.discover_case_folders.
    "automation_front_globs": [],
    "automation_skip_completed": True,
    "automation_skip_if_selfie_exists": True,
    "automation_skip_if_video_exists": True,
    "automation_max_cases_per_run": "5",  # 1 | 5 | 10 | all
    "automation_allow_reprocess": False,
    "automation_reprocess_mode": "skip",  # skip | overwrite | increment
    "automation_front_expand_enabled": True,
    "automation_front_expand_provider": "fal",  # auto | bfl | fal (fal default per user direction 2026-05-22)
    "automation_front_expand_mode": "percent",  # document_3x4 | percent
    "automation_front_expand_composite_mode": "preserve_seamless",  # preserve_seamless | feathered | hard | none | black_fill
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
    # Selfie generation dimensions. 864x1152 is EXACT 3:4 (864/1152 = 0.75).
    # The pipeline passes these to SelfieGenerator.generate(); without them it
    # falls back to the generator's 720x1280 (9:16) default, which then survives
    # the ratio-preserving percent expand AND Kling (which follows the input
    # image's aspect ratio), producing 9:16 video. Generating the selfie at a
    # true 3:4 keeps the whole chain 3:4 end-to-end. nano-banana snaps to its
    # nearest supported aspect label (3:4 is supported) via _closest_aspect_ratio.
    "automation_selfie_width": 864,
    "automation_selfie_height": 1152,
    "automation_similarity_threshold": 80,
    "automation_selfie_expand_enabled": True,
    "automation_selfie_expand_provider": "fal",  # auto | bfl | fal (fal default per user direction 2026-05-22)
    "automation_selfie_expand_mode": "percent",  # percent | centered_3x4
    "automation_selfie_expand_composite_mode": "none",  # preserve_seamless | feathered | hard | none | black_fill  (Step 2.5 selfie expand ships raw AI output by default)
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
    # Ping-pong loop step (Kling -> rPPG -> Loop -> Crush -> Oldcam, Phase E
    # order mirrored from the GUI queue). DEFAULT OFF per user mandate 2026-06-11;
    # graceful-skip when ffmpeg is missing (never hard-fails a case).
    "automation_loop_enabled": False,
    # Quality-crush step: re-encode at 720p/480p / CRF 35 to mimic WhatsApp
    # transcoding. Associated with higher Persona pass rates. Runs after Loop,
    # before Oldcam, so the compression artefact carries through.
    #
    # Multi-resolution (2026-06-18): ``automation_crush_resolutions`` is the
    # canonical LIST of selected tiers (["720p"], ["480p"], ["720p","480p"],
    # or [] for off), fanned out exactly like the Oldcam version list. The
    # legacy boolean ``automation_crush_enabled`` is still honoured for
    # back-compat via automation.video_crush.normalize_crush_resolutions
    # (True → ["480p"], the pre-multi behaviour). Fresh default: 720p ON.
    "automation_crush_resolutions": ["720p"],
    "automation_crush_enabled": False,
    "automation_crush_required": False,
    # Adversarial-attack (AA) step: re-encode through the aa-video subproject's
    # ISOLATED uv venv (its deps conflict with the main numpy<2 stack — see
    # automation/video_aa.py). Runs after Crush, before Oldcam, so each selected
    # attack-pipeline's output fans through Oldcam (mirrors the crush-tier
    # fan-out). ``automation_aa_attacks`` is the canonical LIST of selected
    # pipelines (["prime"], ["prime","scenario1"], or [] for off); the legacy
    # boolean ``automation_aa_enabled`` is honoured for back-compat via
    # automation.video_aa.normalize_aa_attacks (True → ["prime"]). DEFAULT OFF
    # (empty list) — opt-in, authorized detector-research use only.
    "automation_aa_attacks": [],
    "automation_aa_enabled": False,
    "automation_aa_required": False,
    "automation_aa_strength": 0.5,
    "automation_aa_generator": "generic",
    "automation_oldcam_enabled": True,
    # Canonical form is a LIST of versions (multi-select, 2026-06-11):
    # ["v13", "v24"], ["all"] (symbolic — expanded at runtime), or [] (none).
    # Legacy single-string values ("v24", "all") are coerced via
    # automation.oldcam.normalize_oldcam_versions everywhere this is read.
    # Default v13 per user mandate 2026-06-11 (quick-start "best results"
    # combo is rPPG + oldcam v13; previous default was v24).
    "automation_oldcam_version": ["v13"],
    "automation_oldcam_required": True,
    # rPPG injection (Phase E: Kling -> rPPG -> Loop -> Oldcam). Installs a
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
    # Landmark-detection stride for the rPPG injector. Per its own
    # --landmark-stride documentation, running MediaPipe face landmark
    # detection only every Nth frame (with the ROIStabilizer carrying
    # the shape between detections) gives a 3-5x reduction in per-frame
    # detection cost at "negligible quality loss on mostly-still faces."
    #
    # Default reverted from 3 to 1 in fix/step0-composite-and-rppg-v2.5
    # after PR #52 shipped 3 + a snapshot-race regression that produced
    # unplayable -rppg.mp4 outputs. The snapshot race itself is now
    # fixed (rPPG/rppg_injector.py:_snapshot_validates) and the playability
    # gate (automation/rppg.py::_is_playable_video) catches future
    # regressions, but until we have local smoke-test proof that
    # stride>1 is safe on real Kling output the slow-but-correct default
    # is the right ship state. Power users can set
    # ``automation_rppg_landmark_stride`` to 3 (or the GUI alias
    # ``rppg_landmark_stride``) to opt back into the speedup. (Once the
    # auto-NVIDIA bootstrap activates CuPy on a CUDA host the per-frame
    # mediapipe cost matters far less anyway.)
    "automation_rppg_landmark_stride": 1,
    "automation_rppg_required": False,
    # When False (default) the injector's metric-suffixed filename
    # ("{stem}-rppg - <SNR>-<Phase>-<Temporal>-<Motion>-<Harmonic>{ext}")
    # is stripped back to a clean "{stem}-rppg{ext}" and the 5 metrics
    # are written to a "{stem}-rppg.metrics.json" sidecar. True keeps the
    # metrics embedded in the filename. See automation/rppg.py
    # finalize_rppg_output() — single source of truth.
    "automation_rppg_metrics_in_filename": False,
    # The CLI questionary editor (kling_automation_ui.py) uses this to
    # detect when a user's saved config predates the current
    # recommended-defaults baseline and offers to refresh. MUST stay in
    # lockstep with ``kling_automation_ui.RECOMMENDED_DEFAULTS_VERSION``
    # — the smoke test ``test_apply_recommended_defaults_keys`` asserts
    # both end up at the same value when defaults are applied.
    # History:
    #   v2 (2026-05-19, PR #43): rPPG defaults flipped to iterative
    #   v3 (2026-05-19): added automation_rppg_metrics_in_filename
    #   v4 (2026-05-19): minimal-motion default prompt + cfg_scale
    #   v5 (2026-05-20): rPPG iterative + companion flags split
    #   v6 (2026-05-27, PR #54): rPPG landmark-stride default 3 -> 1
    #     (quality-first; v5 users carrying stride=3 from the v2.5
    #     speedup pass get prompted to refresh)
    #   v7 (2026-06-11, CLI UX overhaul): rPPG recommended ON, oldcam
    #     ["v13"] (multi-select list form), loop OFF (new step), provider
    #     fal for both expand steps ("fal.ai for everything").
    "automation_recommended_defaults_version": 9,
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

    @property
    def front_globs(self) -> List[str]:
        raw = self.values.get("automation_front_globs", []) or []
        return [str(pat).lower() for pat in raw if str(pat).strip()]


def resolve_cli_video_model(config: Mapping[str, Any]) -> Tuple[Optional[str], Optional[str]]:
    """(endpoint, display_name) of the video model the CLI automation
    pipeline should use. Either element may be None on the fallback path —
    identical to the raw ``config.get`` reads this replaced; callers
    (FalAIKlingGenerator, displays) already handle None.

    The CLI keeps its own selection (``cli_video_model`` /
    ``cli_video_model_display_name``) so changing the automation model never
    overwrites the GUI's ``current_model`` — and vice versa (per-surface
    split, 2026-06-11). Configs from before the split fall back to the shared
    GUI keys. When the CLI endpoint is set but its display name is not, the
    endpoint doubles as the display — borrowing the GUI's display name would
    label a DIFFERENT model.

    DELIBERATELY not ``automation_*``-prefixed: the manifest fingerprints
    every ``automation_*`` key and the model is deliberately
    non-fingerprinted (resuming with a different model is legal — see the
    ``--batch --model`` override).
    """
    endpoint = str(config.get("cli_video_model") or "").strip()
    if endpoint:
        display = str(config.get("cli_video_model_display_name") or "").strip()
        return endpoint, (display or endpoint)
    return config.get("current_model"), config.get("model_display_name")


def resolve_cli_kling_prompt_slot(config: Mapping[str, Any], default: int = 1) -> int:
    """The Kling prompt SLOT POINTER for the CLI pipeline (1-10).

    Falls back to the GUI's ``current_prompt_slot`` for pre-split configs.
    Only the pointer is per-surface — the slot CONTENT
    (``saved_prompts`` / ``negative_prompts``) stays shared by design so a
    prompt edited in the GUI is the same prompt the CLI runs.
    """
    raw = config.get("cli_kling_prompt_slot")
    if raw in (None, ""):
        raw = config.get("current_prompt_slot", default)
    try:
        slot = int(raw)
    except (TypeError, ValueError):
        return int(default)
    return slot if 1 <= slot <= 10 else int(default)


def resolve_cli_video_duration(config: Mapping[str, Any], default: int = 10) -> int:
    """Video duration (seconds) for the CLI pipeline, per-surface like the
    model itself (a GUI model with a different native duration must not bleed
    into automation runs). Falls back to the shared ``video_duration``."""
    raw = config.get("cli_video_duration")
    if raw in (None, ""):
        raw = config.get("video_duration", default)
    try:
        return int(raw)
    except (TypeError, ValueError):
        return int(default)


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
    # Multi-resolution crush (2026-06-18): collapse the canonical list +
    # legacy boolean into the single canonical ``automation_crush_resolutions``
    # list BEFORE the default-fill loop, so a saved config that only carries
    # the legacy ``automation_crush_enabled`` boolean migrates correctly
    # (True → ['480p'] preserves its old 480p output; absent → fresh ['720p']
    # default). Computed from the ORIGINAL config keys (merged is still a copy
    # of `config` at this point — the default-fill below would otherwise mask
    # "key absent"). Import here keeps module import light + cycle-safe.
    from automation.video_crush import normalize_crush_resolutions

    _crush_kwargs: Dict[str, Any] = {}
    if "automation_crush_resolutions" in merged:
        _crush_kwargs["resolutions"] = merged["automation_crush_resolutions"]
    if "automation_crush_enabled" in merged:
        _crush_kwargs["legacy_enabled"] = merged["automation_crush_enabled"]
    merged["automation_crush_resolutions"] = normalize_crush_resolutions(**_crush_kwargs)
    # Keep the legacy boolean coherent for any reader still gating on it.
    merged["automation_crush_enabled"] = bool(merged["automation_crush_resolutions"])
    # AA attacks (2026-06-18): same list↔legacy-bool collapse as crush, via the
    # single source of truth in automation.video_aa. A saved config carrying only
    # the legacy ``automation_aa_enabled`` boolean migrates (True → ['prime']);
    # absent stays [] (AA is opt-in, default off). Computed from ORIGINAL keys
    # before the default-fill loop below.
    from automation.video_aa import normalize_aa_attacks

    _aa_kwargs: Dict[str, Any] = {}
    if "automation_aa_attacks" in merged:
        _aa_kwargs["attacks"] = merged["automation_aa_attacks"]
    if "automation_aa_enabled" in merged:
        _aa_kwargs["legacy_enabled"] = merged["automation_aa_enabled"]
    # Only override when at least one source key was present; otherwise leave it
    # to the default-fill loop (which sets the [] default) so a brand-new config
    # doesn't get a spurious ['prime'] from normalize_aa_attacks's bare default.
    if _aa_kwargs:
        merged["automation_aa_attacks"] = normalize_aa_attacks(**_aa_kwargs)
        merged["automation_aa_enabled"] = bool(merged["automation_aa_attacks"])
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
    # Coerce legacy single-string oldcam selections ("v24", "all") to the
    # canonical list form so every downstream consumer (pipeline, manifest
    # fingerprint, UI) sees one shape. Import here keeps module import light
    # and is cycle-safe (oldcam.py imports nothing from config).
    from automation.oldcam import normalize_oldcam_versions

    merged["automation_oldcam_version"] = normalize_oldcam_versions(
        merged.get("automation_oldcam_version", AUTOMATION_DEFAULTS["automation_oldcam_version"])
    )
    merged["outpaint_fal_timeout_seconds"] = get_outpaint_fal_timeout_seconds(merged)
    return merged


def from_app_config(config: Dict[str, Any]) -> AutomationConfig:
    return AutomationConfig(values=merge_automation_defaults(config))
