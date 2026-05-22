from __future__ import annotations

from dataclasses import dataclass
import math
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional

from PIL import Image, ImageOps

from automation.config import (
    AutomationConfig,
    DEFAULT_SELFIE_PROMPT,
    get_outpaint_fal_timeout_seconds,
)
from automation.discovery import CaseRecord, detect_existing_outputs
from automation.logger import build_safe_config_snapshot, create_automation_logger
from automation.manifest import AutomationManifest, now_iso
from automation.oldcam import (
    _version_key as _oldcam_version_key,
    discover_oldcam_versions,
    ensure_oldcam_dependencies,
    run_oldcam_all,
)
from automation.rppg import is_rppg_artifact, run_rppg
from face_crop_service import extract_portrait_crop
from face_similarity import compute_face_similarity_details
from kling_generator_falai import FalAIKlingGenerator
from outpaint_geometry import compute_percent_expand_plan, compute_provider_caps
from outpaint_generator import OutpaintGenerator
from selfie_generator import SelfieGenerator


ProgressCB = Optional[Callable[[str, str], None]]


@dataclass
class PipelineDeps:
    outpaint_factory: Callable[[], OutpaintGenerator]
    selfie_factory: Callable[[], SelfieGenerator]
    video_factory: Callable[[], FalAIKlingGenerator]


class AutoPipelineRunner:
    _DEFAULT_OUTPAINT_COMPOSITE_MODE = "preserve_seamless"
    _VALID_OUTPAINT_COMPOSITE_MODES = {"preserve_seamless", "feathered", "hard", "none"}

    def __init__(
        self,
        config: Dict[str, Any],
        automation_config: AutomationConfig,
        manifest: AutomationManifest,
        progress_cb: ProgressCB = None,
        deps: Optional[PipelineDeps] = None,
    ):
        self.config = config
        self.automation = automation_config
        self.manifest = manifest
        self.progress_cb = progress_cb
        self.deps = deps or PipelineDeps(
            outpaint_factory=lambda: OutpaintGenerator(
                api_key=self.config.get("falai_api_key", ""),
                freeimage_key=self.config.get("freeimage_api_key"),
                bfl_api_key=self.config.get("bfl_api_key"),
            ),
            selfie_factory=lambda: SelfieGenerator(
                api_key=self.config.get("falai_api_key", ""),
                freeimage_key=self.config.get("freeimage_api_key"),
                bfl_api_key=self.config.get("bfl_api_key"),
            ),
            video_factory=lambda: FalAIKlingGenerator(
                api_key=self.config.get("falai_api_key", ""),
                verbose=self.config.get("verbose_logging", False),
                model_endpoint=self.config.get("current_model"),
                model_display_name=self.config.get("model_display_name"),
                prompt_slot=int(self.config.get("current_prompt_slot", 1)),
                freeimage_key=self.config.get("freeimage_api_key"),
            ),
        )
        self.last_case_results: Dict[str, Dict[str, Any]] = {}
        self.logger, self.log_path = create_automation_logger(self.config, self.config.get("automation_root_folder"))
        self.verbose_logging = bool(self.config.get("automation_verbose_logging", self.config.get("verbose_logging", True)))

    def _read_int(
        self,
        key: str,
        default: int,
        issues: Optional[List[str]] = None,
        *,
        min_value: Optional[int] = None,
        max_value: Optional[int] = None,
    ) -> int:
        raw = self.automation.get(key, default)
        try:
            value = int(raw)
        except (TypeError, ValueError):
            if issues is not None:
                issues.append(f"{key} must be an integer.")
            return default
        if min_value is not None and value < min_value:
            if issues is not None:
                issues.append(f"{key} must be >= {min_value}.")
            return default
        if max_value is not None and value > max_value:
            if issues is not None:
                issues.append(f"{key} must be <= {max_value}.")
            return default
        return value

    def _read_float(
        self,
        key: str,
        default: float,
        issues: Optional[List[str]] = None,
        *,
        min_value: Optional[float] = None,
        max_value: Optional[float] = None,
    ) -> float:
        raw = self.automation.get(key, default)
        try:
            value = float(raw)
        except (TypeError, ValueError):
            if issues is not None:
                issues.append(f"{key} must be a number.")
            return default
        if not math.isfinite(value):
            if issues is not None:
                issues.append(f"{key} must be a finite number.")
            return default
        if min_value is not None and value < min_value:
            if issues is not None:
                issues.append(f"{key} must be >= {min_value}.")
            return default
        if max_value is not None and value > max_value:
            if issues is not None:
                issues.append(f"{key} must be <= {max_value}.")
            return default
        return value

    def _read_bool(self, key: str, default: bool) -> bool:
        """Read an automation_* config flag as a strict bool.

        Reuses face_similarity._parse_bool (already the canonical helper for
        config-string-to-bool coercion) so a JSON value of "false" is treated
        as False, not True. Raw bool(...) on a non-empty string returns True
        — that's the bug coderabbit flagged on PR #19 for
        automation_similarity_require_fas_pass, which would have silently
        enabled strict spoof gating whenever the user typed "false" in the
        config file.
        """
        from face_similarity import _parse_bool
        raw = self.automation.get(key, default)
        parsed = _parse_bool(raw)
        return parsed if parsed is not None else bool(default)

    def _report(self, message: str, level: str = "info") -> None:
        if self.progress_cb:
            self.progress_cb(message, level)
        if self.verbose_logging:
            if level == "error":
                self.logger.error(message)
            elif level == "warning":
                self.logger.warning(message)
            else:
                self.logger.info(message)

    def _effective_reprocess_mode(self) -> str:
        if not self.automation.get("automation_allow_reprocess", False):
            return "skip"
        mode = str(self.automation.get("automation_reprocess_mode", "skip")).lower()
        return mode if mode in {"skip", "overwrite", "increment"} else "skip"

    @staticmethod
    def _next_increment_path(path: Path) -> Path:
        if not path.exists():
            return path
        idx = 1
        while True:
            candidate = path.with_name(f"{path.stem}_v{idx}{path.suffix}")
            if not candidate.exists():
                return candidate
            idx += 1

    def _policy_meta(self, step_name: str, reused_existing: bool, mode: str) -> Dict[str, Any]:
        return {
            "reprocess_mode": mode,
            "reused_existing": reused_existing,
            "step": step_name,
        }

    def _set_active_step(self, case_entry: Dict[str, Any], step_name: Optional[str]) -> None:
        case_entry["active_step"] = step_name
        self.manifest.save_atomic()

    def _resolve_outpaint_provider(self, configured_provider: str) -> str:
        normalized = str(configured_provider or "auto").strip().lower()
        if normalized in {"bfl", "fal"}:
            return normalized
        if self.config.get("bfl_api_key"):
            return "bfl"
        return "fal"

    def _resolve_composite_mode(self, stage: str) -> str:
        stage_key = f"automation_{stage}_expand_composite_mode"
        configured_mode = str(
            self.automation.get(
                stage_key,
                self.config.get("outpaint_composite_mode", self._DEFAULT_OUTPAINT_COMPOSITE_MODE),
            )
        ).strip().lower()
        if configured_mode in self._VALID_OUTPAINT_COMPOSITE_MODES:
            return configured_mode
        return self._DEFAULT_OUTPAINT_COMPOSITE_MODE

    def resolve_provider_summary(self) -> Dict[str, str]:
        # Fallback default flipped to "fal" 2026-05-22: a live config
        # without these keys still gets the v2.3 ship default
        # (user direction final). The DEFAULTS dict in
        # automation/config.py was updated to match; this fallback
        # only kicks in when neither the live config nor the merged
        # defaults have the key set (rare path, but consistent).
        front_configured = str(self.automation.get("automation_front_expand_provider", "fal")).lower()
        selfie_configured = str(self.automation.get("automation_selfie_expand_provider", "fal")).lower()
        return {
            "front_configured": front_configured,
            "front_resolved": self._resolve_outpaint_provider(front_configured),
            "selfie_configured": selfie_configured,
            "selfie_resolved": self._resolve_outpaint_provider(selfie_configured),
        }

    def resolve_selfie_prompt(self) -> Dict[str, Any]:
        slot = str(self.automation.get("automation_selfie_prompt_slot", 1))
        prompts = self.automation.get("automation_selfie_prompts", {}) or {}
        prompt = str(prompts.get(slot, "") or "").strip()
        if prompt:
            source = f"slot:{slot}"
        else:
            prompt = DEFAULT_SELFIE_PROMPT
            source = "default_seeded_prompt"
        return {"slot": slot, "prompt": prompt, "source": source}

    def _finalize_case(self, case_entry: Dict[str, Any], final_status: str) -> str:
        status_value = "complete" if final_status == "completed" else final_status
        case_entry["status"] = status_value
        self._set_active_step(case_entry, None)
        self.manifest.save_atomic()
        return final_status

    def validate_configuration(self) -> List[str]:
        issues: List[str] = []
        fal_key = str(self.config.get("falai_api_key", "")).strip()
        bfl_key = str(self.config.get("bfl_api_key", "")).strip()
        video_enabled = bool(self.automation.get("automation_video_enabled", True))
        selfie_enabled = bool(self.automation.get("automation_selfie_enabled", True))
        front_expand_enabled = bool(self.automation.get("automation_front_expand_enabled", True))
        selfie_expand_enabled = bool(self.automation.get("automation_selfie_expand_enabled", True))
        provider_summary = self.resolve_provider_summary()
        front_provider = provider_summary["front_configured"]
        selfie_provider = provider_summary["selfie_configured"]

        if video_enabled and not fal_key:
            issues.append("Missing falai_api_key in config (required for Kling video step).")
        if selfie_enabled and not fal_key:
            issues.append("Missing falai_api_key in config (required for selfie generation).")

        if front_expand_enabled:
            if front_provider == "fal" and not fal_key:
                issues.append("Missing falai_api_key for front expand provider=fal.")
            if front_provider == "bfl" and not bfl_key:
                issues.append("Missing bfl_api_key for front expand provider=bfl.")
            if front_provider == "auto" and not fal_key and not bfl_key:
                issues.append("Missing falai_api_key/bfl_api_key for front expand provider=auto.")

        if selfie_expand_enabled:
            if selfie_provider == "fal" and not fal_key:
                issues.append("Missing falai_api_key for selfie expand provider=fal.")
            if selfie_provider == "bfl" and not bfl_key:
                issues.append("Missing bfl_api_key for selfie expand provider=bfl.")
            if selfie_provider == "auto" and not fal_key and not bfl_key:
                issues.append("Missing falai_api_key/bfl_api_key for selfie expand provider=auto.")

        similarity_threshold = self._read_int("automation_similarity_threshold", 80, issues, min_value=0, max_value=100)
        front_mode = str(self.automation.get("automation_front_expand_mode", "percent")).lower()
        front_composite_mode = self._resolve_composite_mode("front")
        selfie_composite_mode = self._resolve_composite_mode("selfie")
        raw_front_composite_mode = str(
            self.automation.get(
                "automation_front_expand_composite_mode",
                self.config.get("outpaint_composite_mode", self._DEFAULT_OUTPAINT_COMPOSITE_MODE),
            )
        ).strip().lower()
        raw_selfie_composite_mode = str(
            self.automation.get(
                "automation_selfie_expand_composite_mode",
                self.config.get("outpaint_composite_mode", self._DEFAULT_OUTPAINT_COMPOSITE_MODE),
            )
        ).strip().lower()
        if raw_front_composite_mode not in self._VALID_OUTPAINT_COMPOSITE_MODES:
            issues.append(
                "automation_front_expand_composite_mode must be one of: preserve_seamless, feathered, hard, none."
            )
        if raw_selfie_composite_mode not in self._VALID_OUTPAINT_COMPOSITE_MODES:
            issues.append(
                "automation_selfie_expand_composite_mode must be one of: preserve_seamless, feathered, hard, none."
            )
        front_expand_percent = (
            self._read_int("automation_front_expand_percent", 30, issues, min_value=0)
            if front_mode == "percent"
            else 30
        )
        selfie_expand_percent = self._read_int("automation_selfie_expand_percent", 30, issues, min_value=0)
        crop_multiplier = self._read_float("automation_crop_multiplier", 1.5, issues, min_value=0.01)
        selfie_attempts = self._read_int("automation_selfie_max_attempts_per_model", 1, issues, min_value=1)
        front_passes = self._read_int("automation_front_expand_passes", 2, issues)
        if front_passes not in {1, 2}:
            issues.append("automation_front_expand_passes must be 1 or 2.")
        # Keep variables consumed so validation checks stay explicit and deterministic.
        _ = (
            similarity_threshold,
            front_expand_percent,
            selfie_expand_percent,
            crop_multiplier,
            selfie_attempts,
            front_composite_mode,
            selfie_composite_mode,
        )
        if self.automation.get("automation_oldcam_required", False) and not self.automation.get("automation_oldcam_enabled", True):
            issues.append("automation_oldcam_required=true requires automation_oldcam_enabled=true.")
        # Symmetric with the oldcam rule above (Codex P2, PR #39): the CLI
        # advanced setup asks rppg enabled/required independently, so a
        # user can save automation_rppg_required=true while
        # automation_rppg_enabled=false. Without this, Step 8 records the
        # rPPG step "skipped" and the case still finalizes complete — the
        # "required" policy silently becomes a no-op. Reject the
        # contradictory combination at config-validation time, exactly as
        # oldcam does.
        if self._read_bool("automation_rppg_required", False) and not self._read_bool("automation_rppg_enabled", False):
            issues.append("automation_rppg_required=true requires automation_rppg_enabled=true.")
        if self.automation.get("automation_oldcam_required", False):
            repo_root = Path(__file__).resolve().parent.parent
            versions = discover_oldcam_versions(repo_root)
            deps_ok, deps_error = ensure_oldcam_dependencies()
            configured_version = str(self.automation.get("automation_oldcam_version", "all")).lower()
            available_versions = {str(v).lower() for v in versions}
            if configured_version == "all":
                if not available_versions:
                    issues.append("Oldcam required with version=all but no usable oldcam versions were discovered.")
            elif configured_version not in available_versions:
                issues.append(
                    f"Oldcam required but configured version {configured_version} is unavailable. "
                    f"Available: {', '.join(sorted(available_versions)) or '(none)'}."
                )
            if not deps_ok:
                issues.append(f"Oldcam required but dependencies are not ready: {deps_error or 'unknown dependency error'}.")
        return issues

    def run(self, cases: List[CaseRecord]) -> Dict[str, int]:
        self.logger.info("automation run start")
        self.logger.info("automation config snapshot: %s", build_safe_config_snapshot(self.config, self.config.get("automation_root_folder")))
        self.logger.info("provider summary: %s", self.resolve_provider_summary())
        self.logger.info(
            "selection summary: selfie_models=%s selfie_prompt_slot=%s video_model=%s kling_prompt_slot=%s",
            self.automation.get("automation_selfie_models"),
            self.automation.get("automation_selfie_prompt_slot", 1),
            self.config.get("model_display_name") or self.config.get("current_model"),
            self.config.get("current_prompt_slot", 1),
        )
        validation_issues = self.validate_configuration()
        if validation_issues:
            self.logger.error("automation validation failed: %s", validation_issues)
            raise ValueError("Configuration validation failed: " + "; ".join(validation_issues))

        stats = {"completed": 0, "failed": 0, "manual_review": 0, "skipped": 0}
        for case in cases:
            self.logger.info("case start: %s", case.relative_key)
            self.manifest.ensure_case(case.relative_key, case.case_dir, case.front_path)
            if self.automation.get("automation_skip_completed", True) and self.manifest.case_is_complete_and_valid(case.relative_key):
                stats["skipped"] += 1
                self.last_case_results[case.relative_key] = {"status": "skipped", "reason": "already complete"}
                self.logger.info("case skipped complete: %s", case.relative_key)
                continue
            try:
                final_status = self._run_case(case)
            except Exception as exc:
                self._report(f"[{case.relative_key}] case failed: {exc}", "error")
                self.logger.exception("case failed with exception: %s", case.relative_key)
                cases_map = self.manifest.data.setdefault("cases", {})
                case_state = cases_map.setdefault(case.relative_key, {})
                active_step = case_state.get("active_step")
                if active_step:
                    try:
                        self.manifest.update_step(case.relative_key, active_step, "failed", error=str(exc))
                    except (OSError, IOError) as update_error:
                        self.logger.exception(
                            "failed to mark step as failed for case=%s step=%s: %s",
                            case.relative_key,
                            active_step,
                            update_error,
                        )
                else:
                    case_state.setdefault("errors", []).append(
                        {"step": "run", "error": str(exc), "at": now_iso()}
                    )
                case_state["status"] = "failed"
                case_state["active_step"] = None
                case_state["updated_at"] = now_iso()
                self.manifest.save_atomic()
                stats["failed"] += 1
                self.last_case_results[case.relative_key] = {"status": "failed", "reason": str(exc)}
                continue

            stats[final_status] = stats.get(final_status, 0) + 1
            existing_result = self.last_case_results.get(case.relative_key, {})
            self.last_case_results[case.relative_key] = {
                "status": final_status,
                "reason": str(existing_result.get("reason", "")),
            }
            self.logger.info("case end: %s status=%s", case.relative_key, final_status)
        self.logger.info("automation run complete stats=%s", stats)
        return stats

    def _run_case(self, case: CaseRecord) -> str:
        case_dir = case.case_dir
        case_key = case.relative_key
        existing = detect_existing_outputs(case_dir)
        case_entry = self.manifest.data["cases"][case_key]
        manifest_steps = case_entry.get("steps", {}) if isinstance(case_entry.get("steps"), dict) else {}
        manifest_selfie_output = manifest_steps.get("selfie_generate", {}).get("output")
        if manifest_selfie_output:
            manifest_selfie_path = Path(manifest_selfie_output)
            if manifest_selfie_path.exists() and manifest_selfie_path.is_file():
                existing = existing.__class__(
                    front_expanded=existing.front_expanded,
                    extracted=existing.extracted,
                    selfie_candidate=manifest_selfie_path,
                    video_candidate=existing.video_candidate,
                )
        manifest_video_output = manifest_steps.get("video_generate", {}).get("output")
        if manifest_video_output:
            manifest_video_path = Path(manifest_video_output)
            if manifest_video_path.exists() and manifest_video_path.is_file() and manifest_video_path.suffix.lower() == ".mp4":
                existing = existing.__class__(
                    front_expanded=existing.front_expanded,
                    extracted=existing.extracted,
                    selfie_candidate=existing.selfie_candidate,
                    video_candidate=manifest_video_path,
                )
        case_entry["status"] = "running"
        self._set_active_step(case_entry, None)
        self.manifest.save_atomic()

        outpaint = self.deps.outpaint_factory()
        outpaint.set_progress_callback(self.progress_cb)
        reprocess_mode = self._effective_reprocess_mode()
        front_provider = str(self.automation.get("automation_front_expand_provider", "fal")).lower()
        selfie_provider = str(self.automation.get("automation_selfie_expand_provider", "fal")).lower()
        front_composite_mode = self._resolve_composite_mode("front")
        selfie_composite_mode = self._resolve_composite_mode("selfie")
        resolved_front_provider = self._resolve_outpaint_provider(front_provider)
        resolved_selfie_provider = self._resolve_outpaint_provider(selfie_provider)
        case_entry["policy"] = {
            "reprocess_mode": reprocess_mode,
            "front_provider_configured": front_provider,
            "front_provider_resolved": resolved_front_provider,
            "selfie_provider_configured": selfie_provider,
            "selfie_provider_resolved": resolved_selfie_provider,
        }
        self.manifest.save_atomic()
        self.logger.info(
            "case %s providers front=%s->%s selfie=%s->%s",
            case_key,
            front_provider,
            resolved_front_provider,
            selfie_provider,
            resolved_selfie_provider,
        )

        # Step 1: front expand
        front_expanded = existing.front_expanded or (case_dir / self.automation.get("automation_front_output_name", "front-expanded.png"))
        configured_front_passes = self._read_int("automation_front_expand_passes", 2)
        front_passes = configured_front_passes if configured_front_passes in {1, 2} else 2
        if self.automation.get("automation_front_expand_enabled", True):
            current_step = self.manifest.get_step(case_key, "front_expand")
            existing_front_step_output = current_step.get("output")
            if (
                reprocess_mode == "skip"
                and current_step.get("status") == "complete"
                and existing_front_step_output
                and Path(existing_front_step_output).exists()
            ):
                front_expanded = Path(existing_front_step_output)
                self.manifest.update_step(
                    case_key,
                    "front_expand",
                    "complete",
                    output=str(front_expanded),
                    meta={
                        **self._policy_meta("front_expand", True, reprocess_mode),
                        "configured_passes": front_passes,
                        "executed_passes": 0,
                        "composite_mode": front_composite_mode,
                    },
                )
            elif reprocess_mode == "skip" and front_expanded and Path(front_expanded).exists():
                self.manifest.update_step(
                    case_key,
                    "front_expand",
                    "complete",
                    output=str(front_expanded),
                    meta={
                        **self._policy_meta("front_expand", True, reprocess_mode),
                        "configured_passes": front_passes,
                        "executed_passes": 0,
                        "composite_mode": front_composite_mode,
                    },
                )
            else:
                target_output = Path(front_expanded)
                if reprocess_mode == "increment":
                    target_output = self._next_increment_path(target_output)
                front_is_document = self.automation.get("automation_front_expand_mode") == "document_3x4"
                front_expand_kwargs: Dict[str, Any] = {}
                if not front_is_document:
                    pct = self._read_int("automation_front_expand_percent", 30)
                    with Image.open(case.front_path) as _img:
                        width, height = ImageOps.exif_transpose(_img).size
                    plan = compute_percent_expand_plan(
                        width,
                        height,
                        pct,
                        compute_provider_caps(resolved_front_provider),
                    )
                    front_expand_kwargs = {
                        "expand_left": int(plan["left"]),
                        "expand_right": int(plan["right"]),
                        "expand_top": int(plan["top"]),
                        "expand_bottom": int(plan["bottom"]),
                    }
                    self.logger.info("case %s front expand geometry width=%s height=%s pct=%s plan=%s", case_key, width, height, pct, plan)
                self._set_active_step(case_entry, "front_expand")
                self.manifest.update_step(case_key, "front_expand", "running")
                result = None
                front_input_path = str(case.front_path)
                executed_passes = 0
                # For 2-pass mode: pass 1 must NOT overwrite pass 2's
                # output path (the prior `output_path=None` auto-name
                # in pass 1 produced `<stem>-expanded.png`, exactly
                # what pass 2's forced `target_output` was; pass 2
                # silently clobbered pass 1's file). Plan a distinct
                # intermediate path for pass 1 so both stages are
                # retained on disk and the downstream contract
                # (target_output = `front-expanded.png`) holds.
                # PR #48 round 4 subagent M3.
                _pass1_intermediate: Optional[str] = None
                if front_passes == 2:
                    _stem = target_output.stem
                    _ext = target_output.suffix or ".png"
                    _pass1_intermediate = str(
                        target_output.with_name(f"{_stem}-stage1{_ext}")
                    )
                for pass_index in range(front_passes):
                    if pass_index == front_passes - 1:
                        pass_output = str(target_output)
                    elif _pass1_intermediate:
                        pass_output = _pass1_intermediate
                    else:
                        pass_output = None
                    # Phase G of polish/v2.3 (2026-05-22): Step 0
                    # face-crop expand uses its OWN prompt key. Falls
                    # back to the legacy shared ``outpaint_prompt`` so
                    # CLI runs on configs without the new key still
                    # work. Pre-Phase-G this dispatch passed no prompt
                    # at all (default empty string), so the section-
                    # specific key is a strict improvement.
                    # Codex P1 on 0967564 (2026-05-22): use key-
                    # presence semantics — a user who saves an
                    # intentionally EMPTY ``face_crop_expand_prompt``
                    # must NOT see the legacy shared
                    # ``outpaint_prompt`` re-substituted. ``or "" or``
                    # treated empty as missing, breaking section-
                    # specific prompt independence. The helper below
                    # only falls back when the key is absent or
                    # non-str; an empty string is a valid intentional
                    # value.
                    _section = self.config.get("face_crop_expand_prompt")
                    if isinstance(_section, str):
                        front_expand_prompt = _section
                    else:
                        front_expand_prompt = str(
                            self.config.get("outpaint_prompt", "") or ""
                        )
                    result = outpaint.outpaint(
                        image_path=front_input_path,
                        output_folder=str(case_dir),
                        output_path=pass_output,
                        provider=resolved_front_provider,
                        composite_mode=front_composite_mode,
                        document_mode=front_is_document,
                        edge_seal_px=int(self.automation.get("automation_front_edge_seal_px", 12))
                        if self.automation.get("automation_front_edge_seal_enabled", True)
                        else 0,
                        poll_timeout_seconds=get_outpaint_fal_timeout_seconds(self.config),
                        prompt=front_expand_prompt,
                        **front_expand_kwargs,
                    )
                    if not result:
                        self.manifest.update_step(
                            case_key,
                            "front_expand",
                            "failed",
                            error=f"front expansion failed on pass {pass_index + 1}",
                            meta={
                                "configured_passes": front_passes,
                                "executed_passes": executed_passes,
                            },
                        )
                        return self._finalize_case(case_entry, "failed")
                    front_input_path = result
                    executed_passes += 1
                # Clean up the pass-1 intermediate now that pass 2 has
                # consumed it. Leaving it on disk pollutes the case dir
                # AND triggers session_manager's outpaint-classifier on
                # any later GUI load (the stage1 file would appear as a
                # sibling generation in the carousel). Per subagent
                # a659d166 M1 on PR #48 round 5. Best-effort delete:
                # not fatal if it fails — the downstream contract
                # depends only on `target_output`.
                if front_passes == 2 and _pass1_intermediate:
                    _stage1 = Path(_pass1_intermediate)
                    if _stage1.exists():
                        try:
                            _stage1.unlink()
                        except OSError:
                            self.logger.debug(
                                "case %s: failed to unlink stage1 intermediate %s",
                                case_key,
                                _pass1_intermediate,
                            )
                self.manifest.update_step(
                    case_key,
                    "front_expand",
                    "complete",
                    output=str(result),
                    meta={
                        **self._policy_meta("front_expand", False, reprocess_mode),
                        "configured_passes": front_passes,
                        "executed_passes": executed_passes,
                        "composite_mode": front_composite_mode,
                    },
                )
                front_expanded = Path(result)
        else:
            self.manifest.update_step(case_key, "front_expand", "skipped", output=str(front_expanded))

        # Step 2: extract portrait from original front
        extracted_path = case_dir / self.automation.get("automation_extract_output_name", "extracted.png")
        extract_meta: Dict[str, Any]
        extract_step = self.manifest.get_step(case_key, "extract_portrait")
        existing_extract_output = extract_step.get("output")
        extraction_skipped = False
        extraction_reused = False
        if (
            not self.automation.get("automation_extract_enabled", True)
        ):
            extraction_skipped = True
            manifest_extract_path = Path(existing_extract_output) if existing_extract_output else None
            discovered_extract_path = existing.extracted if existing.extracted and existing.extracted.exists() else None
            default_extract_path = extracted_path if extracted_path.exists() else None
            resolved_extract = (
                (manifest_extract_path if manifest_extract_path and manifest_extract_path.exists() else None)
                or discovered_extract_path
                or default_extract_path
            )
            if resolved_extract is not None:
                extracted_path = resolved_extract
            self.manifest.update_step(case_key, "extract_portrait", "skipped", output=str(extracted_path))
            extract_meta = {}
        elif (
            reprocess_mode == "skip"
            and extract_step.get("status") == "complete"
            and existing_extract_output
            and Path(existing_extract_output).exists()
        ):
            extracted_path = Path(existing_extract_output)
            extract_meta = extract_step.get("meta") or {}
            self.manifest.update_step(
                case_key,
                "extract_portrait",
                "complete",
                output=str(extracted_path),
                meta={**extract_meta, **self._policy_meta("extract_portrait", True, reprocess_mode)},
            )
            extraction_reused = True
        elif reprocess_mode == "skip" and extracted_path.exists():
            extract_meta = extract_step.get("meta") or {}
            self.manifest.update_step(
                case_key,
                "extract_portrait",
                "complete",
                output=str(extracted_path),
                meta={**extract_meta, **self._policy_meta("extract_portrait", True, reprocess_mode)},
            )
            extraction_reused = True
        else:
            target_extract_path = extracted_path
            if reprocess_mode == "increment":
                target_extract_path = self._next_increment_path(extracted_path)
            self._set_active_step(case_entry, "extract_portrait")
            self.manifest.update_step(case_key, "extract_portrait", "running")
            extract_meta = extract_portrait_crop(
                input_path=str(case.front_path),
                output_path=str(target_extract_path),
                crop_multiplier=self._read_float("automation_crop_multiplier", 1.5),
                progress_cb=self.progress_cb,
            )
            extracted_path = target_extract_path
        if extraction_skipped:
            if not extracted_path.exists():
                self.manifest.update_step(
                    case_key,
                    "similarity_gate",
                    "manual_review",
                    error="portrait extraction disabled and extracted image missing",
                    meta={"extracted_path": str(extracted_path)},
                )
                return self._finalize_case(case_entry, "manual_review")
        else:
            self.manifest.update_step(
                case_key,
                "extract_portrait",
                "complete",
                output=str(extracted_path),
                meta={
                    "confidence": extract_meta.get("confidence"),
                    "crop_box": extract_meta.get("crop_box"),
                    "extractor": extract_meta.get("extractor"),
                    **self._policy_meta("extract_portrait", extraction_reused, reprocess_mode),
                },
            )

        # Step 3/4: selfie + similarity gate
        selfie_enabled = bool(self.automation.get("automation_selfie_enabled", True))
        if not selfie_enabled:
            self.manifest.update_step(
                case_key,
                "selfie_generate",
                "manual_review",
                error="selfie generation disabled by automation_selfie_enabled=false",
                meta=self._policy_meta("selfie_generate", False, reprocess_mode),
            )
            self.manifest.update_step(
                case_key,
                "similarity_gate",
                "manual_review",
                error="similarity gate skipped because selfie generation is disabled",
                meta={"threshold": self._read_int("automation_similarity_threshold", 80)},
            )
            return self._finalize_case(case_entry, "manual_review")

        selfie = self.deps.selfie_factory()
        selfie.set_progress_callback(self.progress_cb)
        selfie_folder = case_dir / "gen-images"
        selfie_folder.mkdir(exist_ok=True)
        model_endpoints = list(self.automation.get("automation_selfie_models", ["fal-ai/nano-banana-2/edit"]))
        selfie_prompt_ctx = self.resolve_selfie_prompt()
        threshold = self._read_int("automation_similarity_threshold", 80)
        require_fas_pass = self._read_bool("automation_similarity_require_fas_pass", False)
        max_attempts = max(1, self._read_int("automation_selfie_max_attempts_per_model", 1))
        self.logger.info(
            "case %s selfie config models=%s prompt_slot=%s prompt_source=%s threshold=%s",
            case_key,
            model_endpoints,
            selfie_prompt_ctx["slot"],
            selfie_prompt_ctx["source"],
            threshold,
        )

        best_path: Optional[str] = str(existing.selfie_candidate) if (
            reprocess_mode == "skip"
            and existing.selfie_candidate
            and self.automation.get("automation_skip_if_selfie_exists", True)
        ) else None
        best_score = -1
        best_similarity_meta: Dict[str, Any] = {
            "score": None,
            "threshold": threshold,
            "match": None,
            "error": None,
            "diagnostics": None,
        }
        self._set_active_step(case_entry, "selfie_generate")
        self.manifest.update_step(case_key, "selfie_generate", "running")
        if best_path:
            score_info = compute_face_similarity_details(str(extracted_path), best_path, report_cb=self.progress_cb)
            best_score = int(score_info.get("score", 0))
            best_similarity_meta = {
                "score": score_info.get("score"),
                "threshold": threshold,
                "match": score_info.get("match"),
                "error": score_info.get("error"),
                "diagnostics": score_info.get("diagnostics"),
            }
            self._report(f"[{case_key}] Reused existing selfie: {Path(best_path).name}", "info")
        else:
            for endpoint in model_endpoints:
                for _attempt in range(max_attempts):
                    generated = selfie.generate(
                        image_path=str(extracted_path),
                        prompt=selfie_prompt_ctx["prompt"],
                        output_folder=str(selfie_folder),
                        model_endpoint=endpoint,
                    )
                    if not generated:
                        continue
                    score_info = compute_face_similarity_details(str(extracted_path), generated, report_cb=self.progress_cb)
                    score = int(score_info.get("score", 0))
                    if score > best_score:
                        best_score = score
                        best_path = generated
                        best_similarity_meta = {
                            "score": score_info.get("score"),
                            "threshold": threshold,
                            "match": score_info.get("match"),
                            "error": score_info.get("error"),
                            "diagnostics": score_info.get("diagnostics"),
                        }
                    if self.automation.get("automation_selfie_model_policy", "first_pass") == "first_pass" and score >= threshold:
                        break
                if self.automation.get("automation_selfie_model_policy", "first_pass") == "first_pass" and best_score >= threshold:
                    break

        if not best_path:
            self.manifest.update_step(case_key, "selfie_generate", "failed", error="selfie generation failed")
            return self._finalize_case(case_entry, "failed")
        self.manifest.update_step(
            case_key,
            "selfie_generate",
            "complete",
            output=best_path,
            meta={
                "best_score": best_score,
                "selfie_prompt_slot": selfie_prompt_ctx["slot"],
                "selfie_prompt_source": selfie_prompt_ctx["source"],
                **self._policy_meta("selfie_generate", bool(existing.selfie_candidate and best_path == str(existing.selfie_candidate)), reprocess_mode),
            },
        )
        diag = best_similarity_meta.get("diagnostics") if isinstance(best_similarity_meta, dict) else None
        if not isinstance(diag, dict):
            diag = {}
        self.logger.info(
            "case %s similarity summary score=%s mode=%s distance=%s fallback=%s per_model=%s",
            case_key,
            best_similarity_meta.get("score"),
            diag.get("mode"),
            diag.get("raw_cosine_distance"),
            diag.get("fallback_reason"),
            diag.get("per_model_distances"),
        )
        # Route through summarize_fas_pair so the gate decision uses the same
        # verdict the GUI/CLI shows the user. The strict-pass gate ONLY blocks
        # on verdict="fail" (both sides have ok FAS data + at least one spoof
        # flagged). Verdict "unavailable" (one side missing FAS) NEVER blocks —
        # there's nothing to enforce against.
        fas_pair = None
        ref_spoof = None
        tgt_spoof = None
        spoof_blocking = False
        try:
            from similarity_engine import FaceEngine as _FaceEngine
            fas_pair = _FaceEngine.summarize_fas_pair(diag if isinstance(diag, dict) else None)
        except Exception:
            fas_pair = None
        if isinstance(fas_pair, dict):
            verdict = fas_pair.get("verdict")
            ref_status = fas_pair.get("ref_status")
            tgt_status = fas_pair.get("target_status")
            # Pull raw spoof flags for log readability (when ok-status).
            fas = diag.get("anti_spoofing") if isinstance(diag, dict) else None
            if isinstance(fas, dict):
                ref_fas = fas.get("ref") if isinstance(fas.get("ref"), dict) else {}
                tgt_fas = fas.get("target") if isinstance(fas.get("target"), dict) else {}
                ref_spoof = (ref_fas or {}).get("spoof_detected")
                tgt_spoof = (tgt_fas or {}).get("spoof_detected")
            if verdict == "fail":
                if require_fas_pass:
                    self.logger.warning(
                        "case %s anti-spoofing FAILED ref=%s target=%s (require_fas_pass=true; routing to manual_review)",
                        case_key,
                        ref_spoof,
                        tgt_spoof,
                    )
                    spoof_blocking = True
                else:
                    self.logger.warning(
                        "case %s anti-spoofing advisory ref=%s target=%s (log-only; gate unchanged)",
                        case_key,
                        ref_spoof,
                        tgt_spoof,
                    )
            elif verdict == "unavailable":
                self.logger.info(
                    "case %s anti-spoofing not assessable (ref_status=%s target_status=%s) — gate unchanged",
                    case_key,
                    ref_status,
                    tgt_status,
                )
        if self.verbose_logging:
            self.logger.debug("case %s similarity diagnostics=%s", case_key, best_similarity_meta)
        if spoof_blocking:
            spoof_reason = f"anti-spoofing failed (ref_spoof={ref_spoof}, target_spoof={tgt_spoof})"
            self.manifest.update_step(
                case_key,
                "similarity_gate",
                "manual_review",
                output=best_path,
                error=spoof_reason,
                meta=best_similarity_meta,
            )
            self.last_case_results[case_key] = {"status": "manual_review", "reason": spoof_reason}
            return self._finalize_case(case_entry, "manual_review")
        if best_similarity_meta.get("error"):
            similarity_error = str(best_similarity_meta.get("error"))
            self.manifest.update_step(
                case_key,
                "similarity_gate",
                "manual_review",
                output=best_path,
                error=f"similarity unavailable: {similarity_error}",
                meta=best_similarity_meta,
            )
            self.last_case_results[case_key] = {"status": "manual_review", "reason": f"similarity unavailable: {similarity_error}"}
            self.logger.warning("case %s similarity unavailable: %s", case_key, similarity_error)
            return self._finalize_case(case_entry, "manual_review")
        if best_score < threshold:
            self.manifest.update_step(
                case_key,
                "similarity_gate",
                "manual_review",
                output=best_path,
                error=f"similarity {best_score} below threshold {threshold}",
                meta=best_similarity_meta,
            )
            self.last_case_results[case_key] = {"status": "manual_review", "reason": f"similarity {best_score} below threshold {threshold}"}
            return self._finalize_case(case_entry, "manual_review")
        self.manifest.update_step(
            case_key,
            "similarity_gate",
            "complete",
            output=best_path,
            meta=best_similarity_meta,
        )

        # Step 5: selfie expand
        final_still = best_path
        if self.automation.get("automation_selfie_expand_enabled", True):
            selfie_expand_step = self.manifest.get_step(case_key, "selfie_expand")
            selfie_expand_output = selfie_expand_step.get("output")
            if (
                reprocess_mode == "skip"
                and selfie_expand_step.get("status") == "complete"
                and selfie_expand_output
                and Path(selfie_expand_output).exists()
            ):
                final_still = selfie_expand_output
                self.manifest.update_step(
                    case_key,
                    "selfie_expand",
                    "complete",
                    output=selfie_expand_output,
                    meta={
                        **self._policy_meta("selfie_expand", True, reprocess_mode),
                        "composite_mode": selfie_composite_mode,
                    },
                )
            else:
                pct = self._read_int("automation_selfie_expand_percent", 30)
                with Image.open(best_path) as _img:
                    width, height = ImageOps.exif_transpose(_img).size
                plan = compute_percent_expand_plan(
                    width,
                    height,
                    pct,
                    compute_provider_caps(resolved_selfie_provider),
                )
                margins = {
                    "left": int(plan["left"]),
                    "right": int(plan["right"]),
                    "top": int(plan["top"]),
                    "bottom": int(plan["bottom"]),
                }
                self.logger.info("case %s selfie expand geometry width=%s height=%s pct=%s plan=%s", case_key, width, height, pct, plan)
                self._set_active_step(case_entry, "selfie_expand")
                self.manifest.update_step(case_key, "selfie_expand", "running")
                expanded_output = case_dir / "gen-images" / f"{Path(best_path).stem}-expanded.png"
                if reprocess_mode == "increment":
                    expanded_output = self._next_increment_path(expanded_output)
                # Phase G: Step 2.5 selfie expand uses its OWN prompt
                # key. Codex P1 on 0967564: key-presence semantics,
                # NOT truthiness — an explicitly-saved empty string
                # is a valid intentional value (user clearing the
                # prompt) and must NOT be silently replaced by the
                # legacy shared prompt.
                _section = self.config.get("selfie_expand_prompt")
                if isinstance(_section, str):
                    selfie_expand_prompt = _section
                else:
                    selfie_expand_prompt = str(
                        self.config.get("outpaint_prompt", "") or ""
                    )
                expanded_result = outpaint.outpaint(
                    image_path=best_path,
                    output_folder=str(case_dir / "gen-images"),
                    output_path=str(expanded_output),
                    provider=resolved_selfie_provider,
                    composite_mode=selfie_composite_mode,
                    document_mode=self.automation.get("automation_selfie_expand_mode") == "centered_3x4",
                    expand_left=margins["left"],
                    expand_right=margins["right"],
                    expand_top=margins["top"],
                    expand_bottom=margins["bottom"],
                    edge_seal_px=0,
                    poll_timeout_seconds=get_outpaint_fal_timeout_seconds(self.config),
                    prompt=selfie_expand_prompt,
                )
                if expanded_result:
                    final_still = expanded_result
                    self.manifest.update_step(
                        case_key,
                        "selfie_expand",
                        "complete",
                        output=expanded_result,
                        meta={
                            **self._policy_meta("selfie_expand", False, reprocess_mode),
                            "composite_mode": selfie_composite_mode,
                        },
                    )
                else:
                    self.manifest.update_step(case_key, "selfie_expand", "failed", error="selfie expand failed")
                    return self._finalize_case(case_entry, "failed")
        else:
            self.manifest.update_step(
                case_key,
                "selfie_expand",
                "skipped",
                output=best_path,
                meta={
                    **self._policy_meta("selfie_expand", False, reprocess_mode),
                    "composite_mode": selfie_composite_mode,
                },
            )

        # Step 6: video generation
        if self.automation.get("automation_video_enabled", True):
            skipped_existing_video = False
            if (
                reprocess_mode == "skip"
                and self.automation.get("automation_skip_if_video_exists", True)
                and existing.video_candidate
            ):
                skipped_existing_video = True
                self.manifest.update_step(
                    case_key,
                    "video_generate",
                    "skipped",
                    output=str(existing.video_candidate),
                    meta=self._policy_meta("video_generate", True, reprocess_mode),
                )
                if not self.automation.get("automation_oldcam_enabled", True):
                    self.manifest.update_step(case_key, "oldcam", "skipped", error="oldcam disabled")
                    # Pre-rPPG this short-circuited to completed (video
                    # reused + oldcam off = nothing left). rPPG can now be
                    # the ONLY enabled post-process on a reused video, so
                    # only finalize early when rPPG is also disabled —
                    # otherwise fall through to Step 8, which picks up the
                    # reused video from the video_generate step output.
                    if not self._read_bool("automation_rppg_enabled", False):
                        return self._finalize_case(case_entry, "completed")
            if not skipped_existing_video:
                video = self.deps.video_factory()
                video.set_progress_callback(self.progress_cb)
                self._set_active_step(case_entry, "video_generate")
                self.manifest.update_step(case_key, "video_generate", "running")
                video_output_dir = case_dir / "gen-videos"
                video_output_dir.mkdir(exist_ok=True)
                # CLI parity with the GUI queue: pass the negative prompt
                # + cfg_scale + end-frame lock from the SAME config keys
                # the GUI writes (negative_prompts / cfg_scale_value /
                # lock_end_frame). The dispatcher gates each on the
                # selected model's capabilities (get_model_capabilities —
                # single source of truth), so an o3/seedance run silently
                # drops the unsupported ones exactly as the GUI does.
                _slot = str(self.config.get("current_prompt_slot", 1))
                # _read_bool (not raw .get) so a string "false" in the
                # automation config disables prompt/negative reuse as the
                # user intended — raw bool("false") is truthy (CodeRabbit,
                # PR #41). It IS an automation_* key, so self._read_bool's
                # self.automation source is correct here.
                _use_existing = self._read_bool(
                    "automation_video_use_existing_prompt", True
                )
                try:
                    _cfg_val = float(self.config.get("cfg_scale_value", 0.7))
                except (TypeError, ValueError):
                    _cfg_val = 0.7
                # lock_end_frame is a GUI (kling_config) key, NOT an
                # automation_* key — read it from self.config via the
                # canonical _parse_bool (NOT self._read_bool, which only
                # looks in self.automation and would always return the
                # default here).
                from face_similarity import _parse_bool as _pb
                _lock_ef = _pb(self.config.get("lock_end_frame", True))
                if _lock_ef is None:
                    _lock_ef = True
                output_video = video.create_kling_generation(
                    character_image_path=final_still,
                    output_folder=str(video_output_dir),
                    custom_prompt=self.config.get("saved_prompts", {}).get(_slot)
                    if _use_existing
                    else None,
                    negative_prompt=(
                        self.config.get("negative_prompts", {}).get(_slot) or None
                    )
                    if _use_existing
                    else None,
                    duration=int(self.config.get("video_duration", 10)),
                    aspect_ratio=self.automation.get("automation_video_aspect_ratio", "3:4"),
                    resolution=self.config.get("resolution", "720p"),
                    seed=int(self.config.get("seed", -1)),
                    camera_fixed=bool(self.config.get("camera_fixed", False)),
                    generate_audio=bool(self.config.get("generate_audio", False)),
                    cfg_scale=max(0.0, min(1.0, _cfg_val)),
                    lock_end_frame=bool(_lock_ef),
                    use_source_folder=False,
                )
                if not output_video:
                    self.manifest.update_step(case_key, "video_generate", "failed", error="video generation failed")
                    return self._finalize_case(case_entry, "failed")
                if reprocess_mode == "increment":
                    out_video_path = Path(output_video)
                    inc_video_path = self._next_increment_path(out_video_path)
                    if inc_video_path != out_video_path:
                        out_video_path.replace(inc_video_path)
                        output_video = str(inc_video_path)
                self.manifest.update_step(
                    case_key,
                    "video_generate",
                    "complete",
                    output=output_video,
                    meta=self._policy_meta("video_generate", False, reprocess_mode),
                )
        else:
            self.manifest.update_step(case_key, "video_generate", "skipped", output=None)

        # Step 6.5: face-track-continuity gate (Kling source).
        # Validated zero-false-positive reject filter — see
        # docs/analysis/versailles_fail_vs_pass.md. Runs before oldcam so a
        # source unlikely to pass Persona is caught BEFORE spending the
        # oldcam pass + a Persona attempt. Advisory by default
        # (automation_facetrack_required=False -> manual_review, never a
        # hard fail); degrades to a non-blocking skip if cv2/mediapipe or
        # the landmarker model is unavailable.
        if self._read_bool("automation_facetrack_enabled", True):
            ft_manifest_video = self.manifest.get_step(case_key, "video_generate").get("output")
            ft_video_path = Path(ft_manifest_video) if ft_manifest_video else None
            if not (ft_video_path and ft_video_path.exists() and ft_video_path.suffix.lower() == ".mp4"):
                if existing.video_candidate:
                    ft_video_path = Path(existing.video_candidate)
            if ft_video_path and ft_video_path.exists() and ft_video_path.suffix.lower() == ".mp4":
                from automation.face_track_gate import measure_face_track

                self._set_active_step(case_entry, "facetrack_gate")
                self.manifest.update_step(case_key, "facetrack_gate", "running")
                ft_min = self._read_float(
                    "automation_facetrack_min_pct", 96.0, min_value=0.0, max_value=100.0
                )
                ft_fps = self._read_float(
                    "automation_facetrack_sample_fps", 8.0, min_value=1.0, max_value=30.0
                )
                self._report(
                    f"face-track gate: checking Kling source "
                    f"(min {ft_min}% @ {ft_fps}fps)…", "info"
                )
                ft = measure_face_track(
                    str(ft_video_path),
                    Path(__file__).resolve().parent.parent,
                    sample_fps=ft_fps,
                    min_track_pct=ft_min,
                )
                if not ft.available:
                    self._report(
                        f"face-track gate: skipped ({ft.reason})", "info"
                    )
                    self.manifest.update_step(
                        case_key, "facetrack_gate", "skipped",
                        error=ft.reason, meta=ft.to_meta(),
                    )
                elif ft.passed:
                    self._report(
                        f"face-track gate: PASS {ft.track_pct}% "
                        f"(>= {ft_min}% threshold)", "info"
                    )
                    self.manifest.update_step(
                        case_key, "facetrack_gate", "complete",
                        output=str(ft_video_path), meta=ft.to_meta(),
                    )
                else:
                    ft_required = self._read_bool("automation_facetrack_required", False)
                    self._report(
                        f"face-track gate: FAIL {ft.track_pct}% < {ft_min}% "
                        f"-> {'failed (oldcam skipped)' if ft_required else 'manual_review'} "
                        f"(likely fails Persona — regenerate the Kling source)",
                        "warning",
                    )
                    status = "failed" if ft_required else "manual_review"
                    self.manifest.update_step(
                        case_key, "facetrack_gate", status,
                        output=str(ft_video_path), error=ft.reason,
                        meta={**ft.to_meta(), "required": ft_required},
                    )
                    self.last_case_results[case_key] = {
                        "status": status, "reason": ft.reason,
                    }
                    return self._finalize_case(case_entry, status)
            else:
                self.manifest.update_step(
                    case_key, "facetrack_gate", "skipped",
                    error="no mp4 video to gate",
                )
        else:
            self.manifest.update_step(
                case_key, "facetrack_gate", "skipped", error="facetrack gate disabled"
            )

        # Step 7-pre: rPPG-first pass (Phase E of polish/v2.3,
        # 2026-05-22). When rPPG is enabled, run injection on the raw
        # video_generate output BEFORE Oldcam so each Oldcam version
        # derives from the rPPG'd base. The injector is a graceful-skip
        # target: if the rPPG/ tool is missing, the launcher returns
        # None and we keep the raw video as Oldcam's input. The legacy
        # "rPPG on every Oldcam output" fan-out from the prior order
        # is preserved behind the ``automation_rppg_per_oldcam_fanout``
        # opt-in flag, applied in Step 8 below (default OFF).
        rppg_base_path: Optional[Path] = None
        if self._read_bool("automation_rppg_enabled", False):
            video_out = self.manifest.get_step(case_key, "video_generate").get("output")
            video_out_path = Path(video_out) if video_out else None
            if (
                video_out_path
                and video_out_path.exists()
                and video_out_path.suffix.lower() == ".mp4"
                and not is_rppg_artifact(video_out_path)
            ):
                rppg_mode = str(self.automation.get("automation_rppg_mode") or "iterative").strip().lower()
                iterative = rppg_mode == "iterative"
                iterate_from_baseline = self._read_bool("automation_rppg_iterate_from_baseline", True)
                skip_diagnosis = self._read_bool("automation_rppg_skip_diagnosis", True)
                skip_kinematic_gate = self._read_bool("automation_rppg_skip_kinematic_gate", True)
                keep_metrics = self._read_bool("automation_rppg_metrics_in_filename", False)
                injected = run_rppg(
                    video_path=video_out_path,
                    repo_root=Path(__file__).resolve().parent.parent,
                    progress_cb=self.progress_cb,
                    keep_metrics=keep_metrics,
                    iterative=iterative,
                    iterate_from_baseline=iterate_from_baseline,
                    skip_diagnosis=skip_diagnosis,
                    skip_kinematic_gate=skip_kinematic_gate,
                )
                if injected and injected.exists():
                    rppg_base_path = injected
                    self.logger.info(
                        "case %s rppg-first: %s -> %s",
                        case_key, video_out_path.name, injected.name,
                    )

        # Step 7: optional oldcam pass
        if self.automation.get("automation_oldcam_enabled", True):
            # Source video for Oldcam: prefer the rPPG-first injected
            # base (Phase E above), else the raw video_generate output,
            # else any existing candidate from disk.
            if rppg_base_path is not None and rppg_base_path.exists():
                selected_video_path = rppg_base_path
            else:
                manifest_video = self.manifest.get_step(case_key, "video_generate").get("output")
                manifest_video_path = Path(manifest_video) if manifest_video else None
                if (
                    manifest_video_path
                    and manifest_video_path.exists()
                    and manifest_video_path.suffix.lower() == ".mp4"
                ):
                    selected_video_path = manifest_video_path
                elif existing.video_candidate:
                    selected_video_path = Path(existing.video_candidate)
                else:
                    selected_video_path = None
            if selected_video_path and selected_video_path.exists() and selected_video_path.suffix.lower() == ".mp4":
                self.logger.info("case %s oldcam readiness=ready version=%s required=%s", case_key, self.automation.get("automation_oldcam_version", "v12"), bool(self.automation.get("automation_oldcam_required", False)))
                self._set_active_step(case_entry, "oldcam")
                self.manifest.update_step(case_key, "oldcam", "running")
                # Run EVERY selected version (run_oldcam_all). The manifest
                # step carries one canonical ``output`` (highest version,
                # back-compat) but ALL per-version paths are stashed in
                # meta["all_outputs"] so Step 8 can fan rPPG over each —
                # there is no privileged "primary" (parity with the GUI
                # queue). Plain -oldcam-vN files are kept (non-destructive).
                oldcam_all = run_oldcam_all(
                    video_path=selected_video_path,
                    version_setting=str(self.automation.get("automation_oldcam_version", "v12")),
                    repo_root=Path(__file__).resolve().parent.parent,
                    progress_cb=self.progress_cb,
                )
                oldcam_output = (
                    max(oldcam_all, key=lambda iv: _oldcam_version_key(iv[0]))[1]
                    if oldcam_all
                    else None
                )
                if oldcam_output:
                    self.logger.info("case %s oldcam output=%s", case_key, oldcam_output)
                    self.manifest.update_step(
                        case_key,
                        "oldcam",
                        "complete",
                        output=str(oldcam_output),
                        meta={
                            **self._policy_meta("oldcam", False, reprocess_mode),
                            "all_outputs": [str(p) for _v, p in oldcam_all],
                        },
                    )
                else:
                    required = bool(self.automation.get("automation_oldcam_required", False))
                    self.logger.warning("case %s oldcam failed required=%s", case_key, required)
                    fail_status = "failed" if required else "skipped"
                    self.manifest.update_step(
                        case_key,
                        "oldcam",
                        fail_status,
                        error="oldcam failed or unavailable",
                        meta={**self._policy_meta("oldcam", False, reprocess_mode), "required": required},
                    )
                    if required:
                        return self._finalize_case(case_entry, "failed")
            else:
                required = bool(self.automation.get("automation_oldcam_required", False))
                self.logger.warning("case %s oldcam readiness=not-ready required=%s", case_key, required)
                reason = "missing or non-mp4 video for oldcam"
                self.manifest.update_step(
                    case_key,
                    "oldcam",
                    "failed" if required else "skipped",
                    error=reason,
                    meta={"required": required},
                )
                if required:
                    return self._finalize_case(case_entry, "failed")
        else:
            self.manifest.update_step(case_key, "oldcam", "skipped", error="oldcam disabled")

        # Step 8: optional per-Oldcam rPPG fan-out + final
        # bookkeeping. The PRIMARY rPPG injection already ran in
        # Step 7-pre above (Phase E of polish/v2.3, 2026-05-22): new
        # order is Kling -> rPPG -> Loop -> Oldcam, with rPPG
        # injected on the raw video_generate output BEFORE Oldcam so
        # every Oldcam version derives from a single rPPG'd base.
        #
        # This Step 8 block now handles two cases:
        #   1. Default flow (``automation_rppg_per_oldcam_fanout=False``):
        #      records the pre-Oldcam rPPG result via the already-
        #      injected fast path (``already`` branch) — no further
        #      injection runs.
        #   2. Opt-in legacy fan-out (``automation_rppg_per_oldcam_fanout=True``):
        #      injects each per-version Oldcam output, in addition to
        #      the pre-Oldcam base injection. Slower; preserves the
        #      old per-Oldcam file set on top of the new base.
        #
        # DEFAULT OFF; _required=False means a missing tool / failed
        # injection is a graceful skip, never a hard-fail (mirrors the
        # facetrack-gate precedent). The injector lives in the rPPG/
        # directory (committed in-tree as of Phase D, 2026-05-22),
        # invoked as an external launcher (.bat on Windows, .sh
        # elsewhere via resolve_rppg_launcher).
        if self._read_bool("automation_rppg_enabled", False):
            oldcam_step = self.manifest.get_step(case_key, "oldcam")
            oldcam_out = oldcam_step.get("output")
            oldcam_all = list((oldcam_step.get("meta") or {}).get("all_outputs") or [])
            video_out = self.manifest.get_step(case_key, "video_generate").get("output")
            required = self._read_bool("automation_rppg_required", False)
            keep_metrics = self._read_bool("automation_rppg_metrics_in_filename", False)
            # Iterative+companion flags. PR #43 / friend feedback: the
            # rPPG injector's iterative mode is mandatory for production
            # — the initial single-shot injection rarely lands at the
            # optimal strength. Default ON via automation_rppg_mode.
            rppg_mode = str(self.automation.get("automation_rppg_mode") or "iterative").strip().lower()
            iterative = rppg_mode == "iterative"
            iterate_from_baseline = self._read_bool("automation_rppg_iterate_from_baseline", True)
            skip_diagnosis = self._read_bool("automation_rppg_skip_diagnosis", True)
            skip_kinematic_gate = self._read_bool("automation_rppg_skip_kinematic_gate", True)

            # Phase E of polish/v2.3 (2026-05-22): the BASE rPPG pass
            # already ran BEFORE Step 7 (Oldcam), so each oldcam output
            # is built on top of the rPPG'd base — the base injection
            # need not be repeated here. The legacy "fan rPPG over
            # every Oldcam output" path is preserved behind the
            # ``automation_rppg_per_oldcam_fanout`` opt-in flag below
            # (default OFF; user-direction 2026-05-22).
            #
            # When the flag is OFF (default), candidates is just the
            # base injection target — but rPPG already ran on it in
            # the pre-Oldcam pass, so the file on disk is an rPPG
            # artifact and the existing "drop already-injected" guard
            # will move it into ``already`` and record it as the final
            # deliverable. That is the intended behaviour: one rPPG'd
            # base, every Oldcam version built on it, no per-Oldcam
            # fan-out.
            #
            # Back-compat note (still applies): a manifest whose
            # oldcam step completed BEFORE meta["all_outputs"] existed
            # has only the legacy single ``oldcam.output``. We include
            # ``oldcam_out`` in the source list whenever
            # ``all_outputs`` is empty — the seen-set dedups it if it
            # also happens to equal a video/all_outputs entry.
            per_oldcam_fanout = self._read_bool(
                "automation_rppg_per_oldcam_fanout", False
            )
            oldcam_sources = oldcam_all if oldcam_all else ([oldcam_out] if oldcam_out else [])
            candidates: List[Path] = []
            seen: set = set()
            # Phase E: substitute the rPPG'd base from the pre-Oldcam
            # pass so the already-injected guard recognises it as a
            # deliverable WITHOUT re-running injection. Without this
            # substitution, the candidate list would contain the raw
            # ``video_out`` (which is NOT an rPPG artifact) and the
            # downstream loop would inject it again, producing the
            # same ``<stem>-rppg.mp4`` output but wasting one full
            # iterative-injection cycle.
            base_candidate = (
                str(rppg_base_path) if rppg_base_path and rppg_base_path.exists()
                else video_out
            )
            # Always include the base candidate. Include oldcam_sources
            # ONLY when the legacy per-Oldcam fan-out is opted-in.
            raw_sources = [base_candidate]
            if per_oldcam_fanout:
                raw_sources.extend(oldcam_sources)
            for raw in raw_sources:
                if not raw:
                    continue
                p = Path(raw)
                key = str(p)
                if key in seen or not p.exists():
                    continue
                seen.add(key)
                candidates.append(p)

            # Drop any candidate that is ALREADY an rPPG artifact —
            # re-injecting would double-inject (-rppg-rppg) and compound
            # the pulse out of the non-negotiable sub-perceptual range.
            # (Codex P2, PR #39.) An already-injected file IS a final
            # deliverable; it just doesn't get re-run.
            already = [p for p in candidates if is_rppg_artifact(p)]
            to_inject = [p for p in candidates if not is_rppg_artifact(p)]

            if not candidates:
                self.logger.warning("case %s rppg readiness=not-ready required=%s", case_key, required)
                self.manifest.update_step(
                    case_key,
                    "rppg",
                    "failed" if required else "skipped",
                    error="no input video for rPPG",
                    meta={"required": required},
                )
                if required:
                    return self._finalize_case(case_entry, "failed")
            elif not to_inject:
                # Every candidate is already injected — nothing to do but
                # record the (highest/last) one as the final deliverable.
                final = str(already[-1])
                self.logger.info(
                    "case %s rppg: all %d candidate(s) already injected; "
                    "recording %s",
                    case_key,
                    len(already),
                    already[-1].name,
                )
                self.manifest.update_step(
                    case_key,
                    "rppg",
                    "complete",
                    output=final,
                    meta={**self._policy_meta("rppg", True, reprocess_mode), "already_injected": True},
                )
                return self._finalize_case(case_entry, "completed")
            else:
                self.logger.info(
                    "case %s rppg readiness=ready required=%s fan-out=%d",
                    case_key,
                    required,
                    len(to_inject),
                )
                self._set_active_step(case_entry, "rppg")
                self.manifest.update_step(case_key, "rppg", "running")
                # Track success/failure PER candidate. ``to_inject`` is
                # ordered base -> oldcam ascending. A naive "collect
                # successes, headline produced[-1]" silently masks a
                # partial fan-out failure: if the base injects but an
                # oldcam injection fails, produced[-1] becomes the base
                # *-rppg and the case is marked complete exposing a
                # non-oldcam clip — and with rppg_required=true a partial
                # failure is reported as success. (Codex P2, PR #40.)
                produced: List[str] = []          # all successful outputs
                failed_inputs: List[str] = []     # candidates with no output
                # Map src -> output so the headline can prefer the
                # HIGHEST oldcam that actually succeeded (not blindly the
                # last attempted). to_inject order == priority order.
                produced_for: dict = {}
                # Phase E (2026-05-22): seed ``produced`` with the rPPG
                # output from the pre-Oldcam pass when it exists. The
                # base rPPG'd file is a legitimate deliverable but
                # already exists on disk (handled via the ``already``
                # branch above when the only candidate IS the base).
                # When per-Oldcam fan-out is on, including it in
                # ``produced`` keeps ``all_outputs`` complete and lets
                # downstream code see the full set of injected files.
                if rppg_base_path is not None and rppg_base_path.exists():
                    produced.append(str(rppg_base_path))
                for src in to_inject:
                    out = run_rppg(
                        video_path=src,
                        repo_root=Path(__file__).resolve().parent.parent,
                        progress_cb=self.progress_cb,
                        keep_metrics=keep_metrics,
                        iterative=iterative,
                        iterate_from_baseline=iterate_from_baseline,
                        skip_diagnosis=skip_diagnosis,
                        skip_kinematic_gate=skip_kinematic_gate,
                        verbose=self.verbose_logging,
                    )
                    if out and out.exists():
                        produced.append(str(out))
                        produced_for[str(src)] = str(out)
                    else:
                        failed_inputs.append(Path(src).name)

                partial = bool(produced) and bool(failed_inputs)
                if produced and not (required and failed_inputs):
                    # Headline = the rPPG of the LAST candidate that
                    # actually succeeded (candidates are base -> oldcam
                    # ascending, so the last success is the
                    # highest-priority real deliverable — never a clip
                    # that silently skipped its oldcam injection).
                    # Subagent CRITICAL on 69dee05: when Phase E's
                    # pre-Oldcam rPPG succeeded AND every per-Oldcam
                    # fan-out attempt failed, ``produced`` is non-empty
                    # (seeded with ``rppg_base_path`` above) but
                    # ``produced_for`` is empty (only fan-out
                    # injections populate it). The bare ``next(gen)``
                    # then raised an uncaught ``StopIteration`` and
                    # crashed the case. Fall back to ``produced[-1]``
                    # (the seeded base, which IS the right deliverable
                    # for that scenario: every Oldcam fanout fail
                    # leaves the rPPG'd base as the only real output).
                    headline = next(
                        (
                            produced_for[str(s)]
                            for s in reversed(to_inject)
                            if str(s) in produced_for
                        ),
                        produced[-1],
                    )
                    status_note = ""
                    if partial:
                        status_note = (
                            f" (PARTIAL: {len(failed_inputs)} of "
                            f"{len(to_inject)} candidate(s) failed: "
                            f"{', '.join(failed_inputs)})"
                        )
                    self.logger.info(
                        "case %s rppg outputs=%d headline=%s%s",
                        case_key,
                        len(produced),
                        Path(headline).name,
                        status_note,
                    )
                    self.manifest.update_step(
                        case_key,
                        "rppg",
                        "complete",
                        output=headline,
                        meta={
                            **self._policy_meta("rppg", False, reprocess_mode),
                            "all_outputs": produced,
                            "failed_inputs": failed_inputs,
                            "partial": partial,
                        },
                    )
                else:
                    # Either nothing was produced, OR required=true and at
                    # least one candidate failed — a required fan-out must
                    # be ALL-or-fail so a missing oldcam deliverable is
                    # never reported as success.
                    if not produced:
                        err = "rPPG injection produced no output"
                    else:
                        err = (
                            f"rPPG required but {len(failed_inputs)} of "
                            f"{len(to_inject)} candidate(s) failed: "
                            f"{', '.join(failed_inputs)}"
                        )
                    self.logger.warning(
                        "case %s rppg failed required=%s (%s)",
                        case_key,
                        required,
                        err,
                    )
                    self.manifest.update_step(
                        case_key,
                        "rppg",
                        "failed" if required else "skipped",
                        error=err,
                        meta={
                            **self._policy_meta("rppg", False, reprocess_mode),
                            "required": required,
                            "all_outputs": produced,
                            "failed_inputs": failed_inputs,
                        },
                    )
                    if required:
                        return self._finalize_case(case_entry, "failed")
        else:
            self.manifest.update_step(case_key, "rppg", "skipped", error="rPPG disabled")

        return self._finalize_case(case_entry, "completed")
